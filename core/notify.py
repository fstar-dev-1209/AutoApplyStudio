"""Alerts that reach you when the bot needs you: in the app, and optionally on Discord or Telegram."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable

import httpx

_DISCORD = re.compile(r"^https://(discord|discordapp)\.com/api/webhooks/\d+/[\w-]+/?$")
DEDUPE_SECONDS = 600


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

    def _deliver(self, title: str, body: str) -> None:
        text = (f"{title}\n{body}" if body else title)[:1800]
        client = self._client or httpx.Client(timeout=10)
        hook = (self._discord() or "").strip()
        if hook:
            if not valid_discord_webhook(hook):
                self._fail("The Discord webhook address is not valid.")
            else:
                self._post(client, hook, {"json": {"content": text}}, "Discord")
        token, chat = (x.strip() for x in self._telegram())
        if token and chat:
            self._post(client, f"https://api.telegram.org/bot{token}/sendMessage",
                       {"data": {"chat_id": chat, "text": text}}, "Telegram")

    def _post(self, client: httpx.Client, url: str, kwargs: dict, where: str) -> None:
        try:
            resp = client.post(url, **kwargs)
            if resp.status_code >= 300:
                self._fail(f"{where} refused the message (HTTP {resp.status_code}).")
        except httpx.HTTPError as exc:
            self._fail(f"Could not reach {where}: {type(exc).__name__}.")  # never include the URL: it holds a secret

    def _fail(self, message: str) -> None:
        self.failures = (self.failures + [message])[-5:]
