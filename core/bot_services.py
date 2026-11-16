"""Everything the auto-apply feature needs, created once and started or stopped together.

Owns the bot browser, the Jobright discovery loop, the apply loop and the alert channel. Settings are read live (each
loop asks for the current settings), so changing them in the app takes effect without a restart; only a change to the
browser itself (proxy, browser program) closes and reopens it on next use.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from core.apply_runner import ApplyRunner
from core.applications import ApplicationService
from core.browser_host import BrowserConfig, BrowserHost
from core.company_match import applied_matches
from core.discovery import DiscoveryRules, DiscoveryService
from core.discovery_runner import DiscoveryRunner
from core.mail_code import OutlookMail
from core.models import QueueStore
from core.notify import Notifier
from core.reports import Reporter
from core.paths import company_folder_exists, config_dir
from core.settings_store import AppSettings, SettingsStore


class FileLog:
    """An append-only text log that keeps itself small (the older half is dropped past ``limit`` bytes)."""

    def __init__(self, path: Path, limit: int = 300_000) -> None:
        self.path, self.limit = path, limit
        self._lock = threading.Lock()

    def __call__(self, text: str) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(text.rstrip() + "\n")
                if self.path.stat().st_size > self.limit:
                    data = self.path.read_text(encoding="utf-8", errors="replace")
                    self.path.write_text(data[len(data) // 2:], encoding="utf-8")
            except OSError:
                pass  # a log that cannot be written must never stop the bot


def browser_config(settings: AppSettings, creds: tuple[str, str], profile_dir: Path) -> BrowserConfig:
    return BrowserConfig(
        profile_dir=profile_dir, channel=settings.bot_browser, proxy=settings.web_proxy,
        proxy_user=creds[0], proxy_password=creds[1], timezone_id=settings.bot_timezone)


class BotServices:
    def __init__(
        self,
        store: QueueStore,
        apps: ApplicationService,
        settings_store: SettingsStore,
        *,
        ask_chat=None,
        on_queued: Callable[[list[str]], None] = lambda ids: None,
        in_app_alert: Callable[[str, str], None] | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.settings_store = settings_store
        self.data_dir = data_dir or config_dir()
        self._host: BrowserHost | None = None
        self._host_lock = threading.Lock()

        self.notifier = Notifier(
            discord_webhook=lambda: settings_store.notify_secret("discord_webhook"),
            telegram=lambda: (settings_store.notify_secret("telegram_token"), settings_store.settings.notify_telegram_chat),
            in_app=in_app_alert)
        self.reporter = Reporter(self.notifier, lambda: settings_store.settings.notify_reports, self.data_dir)
        self.discovery_service = DiscoveryService(store, company_check=self._company_reason)
        self.store = store
        self.discovery = DiscoveryRunner(
            self.discovery_service, lambda: settings_store.settings, self.host, on_queued=on_queued,
            alert=self.notifier.send, on_report=self.reporter.discovery, log=FileLog(self.data_dir / "discovery.log"),
            debug_dir=self.data_dir / "discovery_debug")
        self.apply = ApplyRunner(
            store, apps, lambda: settings_store.settings, self.host, ask_chat=ask_chat, alert=self.notifier.send,
            shots_dir=self.data_dir / "apply_shots",
            mail=OutlookMail(log=FileLog(self.data_dir / "apply_debug.log"),
                             shot_path=str(self.data_dir / "apply_shots" / "outlook-no-code.png")),
            debug_log=FileLog(self.data_dir / "apply_debug.log"),
            save_settings=settings_store.save, reporter=self.reporter)

    # ------------------------------------------------------------------ the browser

    def host(self) -> BrowserHost:
        """The started bot browser; (re)created when its settings changed. Safe to call from any thread."""
        s = self.settings_store.settings
        wanted = browser_config(s, self.settings_store.proxy_credentials(), self.data_dir / "browser" / "bot")
        with self._host_lock:
            if self._host is not None and self._host.config != wanted:
                self._host.stop()
                self._host = None
            if self._host is None:
                self._host = BrowserHost(wanted)
            if not self._host.running:
                self._host.start()
            return self._host

    def open_browser(self) -> None:
        """Open the bot browser window so you can sign in to Jobright and Outlook (once)."""
        self.host()

    @property
    def browser_running(self) -> bool:
        return bool(self._host and self._host.running)

    # ------------------------------------------------------------------ loops

    def start(self) -> None:
        self.discovery.start()
        self.apply.start()

    def stop(self) -> None:
        self.apply.kill()  # an application in progress is abandoned before its Submit click, so closing is quick
        self.discovery.stop()
        self.apply.stop()
        with self._host_lock:
            if self._host is not None:
                self._host.stop()
                self._host = None

    def settings_changed(self) -> None:
        """Wake the loops so new settings apply now rather than at the next tick."""
        self.discovery.run_soon()
        self.apply.wake()

    # ------------------------------------------------------------------ rules shared with the queue

    def _company_reason(self, company: str) -> str:
        """Why a discovered posting should not enter the queue because of its company ('' = fine)."""
        s = self.settings_store.settings
        hits = applied_matches(self.store, company)
        if hits:
            return f"Already applied to {hits[0]['company']}"
        existing = company_folder_exists(s.resolved_output_root(), s.profile.name, company)
        return f"Company folder already exists: {existing}" if existing is not None else ""

    def refresh_rules(self) -> DiscoveryRules:
        s = self.settings_store.settings
        return DiscoveryRules.from_text(s.discovery_blocked_companies, s.discovery_exclude_words,
                                        s.salary_floor if s.discovery_skip_below_floor else 0)
