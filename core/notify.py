"""Alerts and status reports that reach you: in the app, and optionally on Discord or Telegram.

Messages are written once, in Telegram's small HTML subset (see ``report_format``): Telegram gets them as they are,
Discord gets the same message as markdown. If Telegram refuses the formatting, the plain text is sent instead."""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections.abc import Callable

import httpx

from core.report_format import alert as format_alert
from core.report_format import to_discord, to_plain

_DISCORD = re.compile(r"^https://(discord|discordapp)\.com/api/webhooks/\d+/[\w-]+/?$")
DEDUPE_SECONDS = 600
REPORT_GAP_SECONDS = 1.1  # Telegram asks for at most about one message a second to one chat
TELEGRAM_TEXT_LIMIT = 3900  # a message holds 4096 characters
TELEGRAM_CAPTION_LIMIT = 1024
NO_PREVIEW = json.dumps({"is_disabled": True})  # a link in a message must not unfold into a big preview card


def valid_discord_webhook(url: str) -> bool:
    return bool(_DISCORD.match((url or "").strip()))


class Notifier:
    """``send`` never raises and never blocks: network posts happen on a short-lived thread. The same alert is not
    repeated within ten minutes, so a stuck job cannot flood a phone."""

    def __init__(
        self,
        discord_webhook: Callable[[], str] = lambda: "",
        telegram: Callable[[], tuple[str, str]] = lambda: ("", ""),  # (bot token, chat id)
        in_app: Callable[[str, str], None] | None = None,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        background: bool = True,
    ) -> None:
        self._discord, self._telegram, self._in_app = discord_webhook, telegram, in_app
        self._client = client
        self._clock = clock
        self._background = background
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.failures: list[str] = []  # the last few delivery problems, for the Auto Apply page
        self._reports: queue.Queue[tuple[str, tuple[str, bytes] | None]] = queue.Queue()
        self._report_thread: threading.Thread | None = None

    def send(self, title: str, body: str = "") -> bool:
        """True when the alert was released (False when it repeats one sent recently)."""
        key = f"{title}|{body}"
        now = self._clock()
        with self._lock:
            if now - self._last.get(key, -1e9) < DEDUPE_SECONDS:
                return False
            self._last[key] = now
        if self._in_app:
            try:
                self._in_app(title, body)
            except Exception:  # noqa: BLE001
                pass
        if self._background:
            threading.Thread(target=self._deliver, args=(title, body), daemon=True).start()
        else:
            self._deliver(title, body)
        return True

    def can_report(self) -> bool:
        """True when a status report has somewhere to go (Telegram or Discord is set up)."""
        token, chat = (x.strip() for x in self._telegram())
        return bool((token and chat) or (self._discord() or "").strip())

    def report(self, text: str, document: tuple[str, bytes] | None = None) -> bool:
        """A status report for the person who runs the bot. Unlike ``send`` it is not an alert: it is not shown in
        the app, it is never held back as a repeat, and reports go out in the order they were made, about a second
        apart. ``document`` is (file name, bytes), sent to Telegram only. Never raises, never blocks."""
        if not self.can_report():
            return False
        if not self._background:
            self._deliver_report(text, document)
            return True
        self._reports.put((text, document))
        with self._lock:
            if self._report_thread is None or not self._report_thread.is_alive():
                self._report_thread = threading.Thread(target=self._report_loop, name="reports", daemon=True)
                self._report_thread.start()
        return True

    def _report_loop(self) -> None:
        while True:
            try:
                text, document = self._reports.get(timeout=30)
            except queue.Empty:
                return  # idle: the thread ends and the next report starts a new one
            self._deliver_report(text, document)
            time.sleep(REPORT_GAP_SECONDS)

    def _deliver_report(self, text: str, document: tuple[str, bytes] | None) -> None:
        client = self._client or httpx.Client(timeout=30)
        hook = (self._discord() or "").strip()
        if hook:
            if not valid_discord_webhook(hook):
                self._fail("The Discord webhook address is not valid.")
            else:
                self._post(client, hook, {"json": {"content": to_discord(text)[:1900]}}, "Discord")
        token, chat = (x.strip() for x in self._telegram())
        if token and chat:
            self._telegram_message(client, f"https://api.telegram.org/bot{token}", chat, text, document)

    def _telegram_message(self, client: httpx.Client, base: str, chat: str, text: str,
                          document: tuple[str, bytes] | None) -> None:
        """One Telegram message: formatted text, or a file with the text as its caption. Telegram answers 400 when it
        cannot read the formatting; the same message is then sent as plain text rather than lost."""
        if document is not None:
            name, content = document
            files = {"document": (name, content, "text/plain; charset=utf-8")}
            status = self._post(client, f"{base}/sendDocument", {
                "data": {"chat_id": chat, "caption": text[:TELEGRAM_CAPTION_LIMIT], "parse_mode": "HTML"},
                "files": files}, "Telegram", retry=True, tolerate=(400,))
            if status == 400:
                self._post(client, f"{base}/sendDocument", {
                    "data": {"chat_id": chat, "caption": to_plain(text)[:TELEGRAM_CAPTION_LIMIT]}, "files": files},
                    "Telegram", retry=True)
            return
        if len(text) > TELEGRAM_TEXT_LIMIT:  # cutting formatted text could leave a tag open
            self._post(client, f"{base}/sendMessage", {"data": {
                "chat_id": chat, "text": to_plain(text)[:TELEGRAM_TEXT_LIMIT], "link_preview_options": NO_PREVIEW}},
                "Telegram", retry=True)
            return
        status = self._post(client, f"{base}/sendMessage", {"data": {
            "chat_id": chat, "text": text, "parse_mode": "HTML", "link_preview_options": NO_PREVIEW}},
            "Telegram", retry=True, tolerate=(400,))
        if status == 400:
            self._post(client, f"{base}/sendMessage", {"data": {
                "chat_id": chat, "text": to_plain(text)[:TELEGRAM_TEXT_LIMIT], "link_preview_options": NO_PREVIEW}},
                "Telegram", retry=True)

    def _deliver(self, title: str, body: str) -> None:
        self._deliver_report(format_alert(title, body).text, None)

    def _post(self, client: httpx.Client, url: str, kwargs: dict, where: str, retry: bool = False,
              tolerate: tuple[int, ...] = ()) -> int:
        """Post once (twice when asked to slow down). Returns the HTTP status, 0 when the service was not reached.
        A status in ``tolerate`` is returned without being recorded as a failure: the caller has a second try."""
        try:
            resp = client.post(url, **kwargs)
            if resp.status_code == 429 and retry:  # told to slow down: wait as long as asked (at most half a minute), once
                try:
                    wait = float(resp.json().get("parameters", {}).get("retry_after", 5))
                except (ValueError, TypeError, AttributeError):
                    wait = 5.0
                time.sleep(min(30.0, max(1.0, wait)) if self._background else 0)
                resp = client.post(url, **kwargs)
            if resp.status_code >= 300 and resp.status_code not in tolerate:
                self._fail(f"{where} refused the message (HTTP {resp.status_code}).")
            return resp.status_code
        except httpx.HTTPError as exc:
            self._fail(f"Could not reach {where}: {type(exc).__name__}.")  # never include the URL: it holds a secret
            return 0

    def _fail(self, message: str) -> None:
        self.failures = (self.failures + [message])[-5:]
