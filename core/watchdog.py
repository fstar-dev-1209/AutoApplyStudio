"""A small separate process that tells you on Telegram when Auto Apply Studio stops or stops responding.

It never starts the app again: it only reports. The app starts it at launch (see app_main) and writes a heartbeat file
every few seconds from its window's own thread, so a frozen window shows as a heartbeat that has gone quiet. When the
app exits, the watchdog says whether it was closed from its window or stopped on its own, then ends too.

    python -m core.watchdog --pid <the app's process id>
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from core.proc_info import free_memory_mb, is_running

HEARTBEAT_FILE = "heartbeat.json"
BEAT_SECONDS = 20  # how often the app writes the heartbeat
FROZEN_AFTER = 180  # a heartbeat this old (in seconds) while the app is still running means its window is frozen
CHECK_SECONDS = 15


def write_heartbeat(path: Path, pid: int, started: float, *, closed: bool = False, now: float | None = None) -> None:
    """Called by the app. Written to a temporary file first, so the watchdog never reads half a file."""
    data = {"pid": pid, "started": started, "at": time.time() if now is None else now, "closed": closed}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def read_heartbeat(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _span(seconds: float) -> str:
    minutes = int(max(0, seconds) // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


class Watchdog:
    def __init__(self, pid: int, heartbeat: Path, send: Callable[[str, str], object], *,
                 now: Callable[[], float] = time.time, alive: Callable[[int], bool] = is_running,
                 free_mb: Callable[[], int] = free_memory_mb) -> None:
        self.pid, self.heartbeat, self.send = pid, heartbeat, send
        self._now, self._alive, self._free_mb = now, alive, free_mb
        self._started = now()
        self._frozen = False

    def _memory(self) -> str:
        mb = self._free_mb()
        return f" Free memory on the server: {mb:,} MB." if mb else ""

    def check(self) -> bool:
        """One look. False once the app has exited (it has been reported, and the watch is over)."""
        now = self._now()
        beat = read_heartbeat(self.heartbeat)
        ours = beat.get("pid") == self.pid
        started = float(beat.get("started") or self._started) if ours else self._started
        last = float(beat.get("at") or 0) if ours else 0.0
        clock = datetime.fromtimestamp(now).strftime("%H:%M")
        if not self._alive(self.pid):
            if ours and beat.get("closed"):
                self.send("Auto Apply Studio was closed",
                          f"Closed from its window at {clock}, after running {_span(now - started)}. "
                          "It will not start again on its own.")
            else:
                quiet = f" Last sign of life {_span(now - last)} before that." if last else ""
                self.send("Auto Apply Studio stopped",
                          f"It stopped on its own at {clock}, after running {_span(now - started)}.{quiet}"
                          f"{self._memory()} It will not start again on its own: start it with "
                          "Run Auto Apply Studio.bat.")
            return False
        if last and now - last > FROZEN_AFTER and not self._frozen:
            self._frozen = True
            self.send("Auto Apply Studio is not responding",
                      f"Its window has not answered for {_span(now - last)} (since "
                      f"{datetime.fromtimestamp(last).strftime('%H:%M')}). Nothing is being applied while it is "
                      f"frozen.{self._memory()} Windows may close it.")
        elif last and now - last <= FROZEN_AFTER and self._frozen:
            self._frozen = False
            self.send("Auto Apply Studio is responding again", f"Its window answers again ({clock}).")
        return True

    def run(self, sleep: Callable[[float], None] = time.sleep) -> None:
        while self.check():
            sleep(CHECK_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    args = parser.parse_args(argv)

    from core.notify import Notifier
    from core.paths import config_dir
    from core.secrets import load_secret

    folder = config_dir()

    def setting(name: str) -> str:
        # Read straight from settings.json: the watchdog must never load (and possibly rewrite) the app's settings.
        try:
            return str(json.loads((folder / "settings.json").read_text(encoding="utf-8")).get(name) or "")
        except (OSError, ValueError):
            return ""

    secrets = folder / "credentials.dat"
    notifier = Notifier(discord_webhook=lambda: load_secret("notify_discord_webhook", secrets),
                        telegram=lambda: (load_secret("notify_telegram_token", secrets), setting("notify_telegram_chat")),
                        background=False)
    Watchdog(args.pid, folder / HEARTBEAT_FILE, notifier.send).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
