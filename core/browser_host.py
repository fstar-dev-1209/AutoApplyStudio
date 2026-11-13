"""The browser the bot works in: your real Chrome (or Edge), signed in to Jobright and Outlook, behind your proxy.

Playwright's synchronous API belongs to the thread that started it, so one thread owns the browser and everything the
bot does with it (reading Jobright, filling forms, reading mail) is queued onto that thread with :meth:`run`.

Two ways to get the browser:

``attach``  start Chrome as an ordinary program with a debugging port and a profile folder of its own, then attach.
            Chrome carries no automation flags in this mode. Needs a proxy without a login (or no proxy).
``launch``  Playwright starts Chrome itself. Required when the proxy asks for a user name and password.

``auto`` picks ``launch`` for an authenticated proxy and ``attach`` otherwise.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class BrowserError(RuntimeError):
    """The browser could not be started, was closed, or stopped answering."""


@dataclass
class BrowserConfig:
    profile_dir: Path
    channel: str = "chrome"  # chrome | msedge
    mode: str = "auto"  # auto | attach | launch
    proxy: str = ""  # http://host:port or socks5://host:port
    proxy_user: str = ""
    proxy_password: str = ""
    headless: bool = False
    debug_port: int = 9333
    locale: str = "en-US"
    timezone_id: str = ""  # e.g. America/Chicago: keep it consistent with the proxy's country
    executable: str = ""  # explicit browser program; blank = find the installed one

    @property
    def effective_mode(self) -> str:
        if self.mode in {"attach", "launch"}:
            return self.mode
        return "launch" if (self.proxy and self.proxy_user) else "attach"


_CANDIDATES = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ],
    "msedge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/microsoft-edge",
    ],
}


def find_browser(channel: str, explicit: str = "") -> str:
    if explicit and Path(explicit).is_file():
        return explicit
    local = os.environ.get("LOCALAPPDATA", "")
    extra = [str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe")] if local and channel == "chrome" else []
    for path in [*_CANDIDATES.get(channel, []), *extra]:
        if Path(path).is_file():
            return path
    found = shutil.which("google-chrome" if channel == "chrome" else "microsoft-edge")
    if found:
        return found
    raise BrowserError(f"Could not find {channel} on this computer. Install it, or set its location in Settings.")


def browser_args(config: BrowserConfig) -> list[str]:
    """Command-line switches for the browser. None of them hide automation; they keep the session consistent."""
    args = [f"--lang={config.locale}", "--no-first-run", "--no-default-browser-check"]
    if config.proxy:
        args.append(f"--proxy-server={config.proxy}")
        # without this Chrome can reveal the computer's real address through WebRTC even behind a proxy
        args.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    return args


Task = Callable[[Any], Any]  # called on the browser thread with the Playwright BrowserContext


def browser_closed(exc: BaseException) -> bool:
    """True when Playwright is reporting that the page, the context, or Chrome itself is gone."""
    text = f"{type(exc).__name__} {exc}".lower()
    return "has been closed" in text or "target closed" in text


class BrowserHost:
    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self._tasks: queue.Queue[tuple[Task, Future] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._error is None)

    # ------------------------------------------------------------------ lifecycle

    def start(self, timeout: float = 90.0) -> None:
        with self._lock:
            if self.running:
                return
            self._error = None
            self._ready.clear()
            self._thread = threading.Thread(target=self._loop, name="browser-host", daemon=True)
            self._thread.start()
        if not self._ready.wait(timeout):
            raise BrowserError("The browser did not start in time.")
        if self._error is not None:
            raise BrowserError(str(self._error)) from self._error

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._tasks.put(None)
        thread.join(timeout=10)
        self._thread = None
        proc, self._proc = self._proc, None  # the thread was busy with a long task: do not leave Chrome behind
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def submit(self, task: Task) -> Future:
        fut: Future = Future()
        if not self.running:
            fut.set_exception(BrowserError("The browser is not running."))
            return fut
        self._tasks.put((task, fut))
        return fut

    def run(self, task: Task, timeout: float = 300.0) -> Any:
        """Run ``task(context)`` on the browser thread and return its result (or raise what it raised)."""
        fut = self.submit(task)
        try:
            return fut.result(timeout)
        except TimeoutError as exc:  # concurrent.futures.TimeoutError is TimeoutError on 3.11+
            fut.cancel()  # a task still waiting its turn is dropped, so abandoned work cannot pile up behind a long one
            self._unblock_browser()  # a call stuck in a dead browser never returns until Chrome itself is gone
            raise BrowserError("The browser took too long to answer.") from exc

    # ------------------------------------------------------------------ the browser thread

    def _loop(self) -> None:
        playwright = browser = context = None
        try:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            context, browser = self._open(playwright)
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
            self._ready.set()
            self._shutdown(playwright, browser, context)
            return
        self._ready.set()
        try:
            while True:
                item = self._tasks.get()
                if item is None:
                    break
                task, fut = item
                if not fut.set_running_or_notify_cancel():
                    continue
                if not self._alive(browser, context):
                    try:
                        context, browser = self._reopen(playwright, browser, context)
                    except BaseException as exc:  # noqa: BLE001
                        fut.set_exception(BrowserError(f"The bot browser closed and did not reopen. {exc}"))
                        self._error = exc
                        break
                try:
                    fut.set_result(task(context))
                except BaseException as exc:  # noqa: BLE001
                    fut.set_exception(exc)
                    if browser_closed(exc):
                        try:
                            context, browser = self._reopen(playwright, browser, context)
                        except BaseException as reopen_exc:  # noqa: BLE001
                            self._error = reopen_exc
                            break
        finally:
            self._shutdown(playwright, browser, context)
            # anything still waiting must not hang
            while not self._tasks.empty():
                item = self._tasks.get_nowait()
                if item is not None:
                    item[1].set_exception(BrowserError("The browser was closed."))

    def _open(self, playwright):
        cfg = self.config
        cfg.profile_dir.mkdir(parents=True, exist_ok=True)
        if cfg.effective_mode == "launch":
            proxy = None
            if cfg.proxy:
                proxy = {"server": cfg.proxy}
                if cfg.proxy_user:
                    proxy.update(username=cfg.proxy_user, password=cfg.proxy_password)
            kwargs: dict[str, Any] = dict(
                channel=cfg.channel, headless=cfg.headless, proxy=proxy, locale=cfg.locale, no_viewport=True,
                args=[a for a in browser_args(cfg) if not a.startswith("--proxy-server") and not a.startswith("--lang")],
            )
            if cfg.timezone_id:
                kwargs["timezone_id"] = cfg.timezone_id
            if cfg.executable:
                kwargs.pop("channel")
                kwargs["executable_path"] = cfg.executable
            return playwright.chromium.launch_persistent_context(str(cfg.profile_dir), **kwargs), None

        endpoint = f"http://127.0.0.1:{cfg.debug_port}"
        if not self._debug_port_answers(endpoint):
            self._proc = subprocess.Popen(
                [find_browser(cfg.channel, cfg.executable), f"--remote-debugging-port={cfg.debug_port}",
                 f"--user-data-dir={cfg.profile_dir}", *browser_args(cfg), *(["--headless=new"] if cfg.headless else [])],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 45
            while not self._debug_port_answers(endpoint):
                if time.monotonic() > deadline or self._proc.poll() is not None:
                    raise BrowserError(
                        "The browser did not open its debugging port. Close any Chrome that uses this profile "
                        "and try again.")
                time.sleep(0.4)
        browser = playwright.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context(locale=cfg.locale)
        return context, browser

    @staticmethod
    def _alive(browser, context) -> bool:
        try:
            handle = browser if browser is not None else context.browser
            return bool(handle and handle.is_connected())
        except Exception:  # noqa: BLE001
            return False

    def _unblock_browser(self) -> None:
        """End a Chrome that has stopped answering, so a stuck driver call can return."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _reopen(self, playwright, browser, context):
        self._unblock_browser()
        try:
            if browser is not None:
                browser.close()
            elif context is not None:
                context.close()
        except Exception:  # noqa: BLE001
            pass
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return self._open(playwright)

    @staticmethod
    def _debug_port_answers(endpoint: str) -> bool:
        try:
            return httpx.get(endpoint + "/json/version", timeout=1.5).status_code == 200
        except httpx.HTTPError:
            return False

    def _shutdown(self, playwright, browser, context) -> None:
        for closer in (
            (lambda: browser.close()) if browser is not None else (lambda: context.close() if context else None),
            (lambda: playwright.stop() if playwright else None),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 - already gone is fine
                pass
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
