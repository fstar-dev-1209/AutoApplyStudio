#!/usr/bin/env python3
"""Record what Jobright's page really sends, so the reader can be checked against it.

Run from the repo root:   .venv\\Scripts\\python scripts\\capture_jobright.py

It opens the bot browser (the one signed in to Jobright), loads your job list, scrolls a little, and saves every JSON
response from Jobright's servers into scripts/_capture_jobright/. Then it prints what the reader makes of them.
The files can contain your account details: look through them before sharing any. Nothing is sent anywhere.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import jobright  # noqa: E402
from core.browser_host import BrowserConfig, BrowserHost  # noqa: E402
from core.paths import config_dir  # noqa: E402
from core.settings_store import SettingsStore  # noqa: E402

OUT = ROOT / "scripts" / "_capture_jobright"


def main() -> int:
    store = SettingsStore()
    s = store.settings
    user, password = store.proxy_credentials()
    host = BrowserHost(BrowserConfig(
        profile_dir=config_dir() / "browser" / "bot", proxy=s.web_proxy, proxy_user=user, proxy_password=password,
    ))
    OUT.mkdir(exist_ok=True)
    saved: list[tuple[str, object]] = []

    def task(context):
        page = next((p for p in context.pages if "jobright" in (p.url or "")), None) or context.new_page()

        def on_response(resp):
            try:
                if "jobright" in resp.url and "json" in (resp.headers.get("content-type") or "").lower():
                    saved.append((resp.url, resp.json()))
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_response)
        page.goto(jobright.DEFAULT_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        for _ in range(3):
            page.mouse.wheel(0, 1400)
            page.wait_for_timeout(3000)
        print("page state:", jobright.page_state(page) or "ok", "| address:", page.url)
        page.remove_listener("response", on_response)

    host.start()
    try:
        host.run(task, timeout=180)
    finally:
        host.stop()

    listings = []
    for i, (url, body) in enumerate(saved):
        name = re.sub(r"[^A-Za-z0-9]+", "_", url.split("//", 1)[-1])[:80]
        (OUT / f"{i:02d}_{name}.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
        listings += jobright.extract_listings(body)
    print(f"saved {len(saved)} responses to {OUT}")
    print(f"the reader found {len(listings)} jobs; first few:")
    for l in listings[:5]:
        print(f"  {l.external_id} | {l.title} | {l.company} | posted {l.posted_at!r} | apply {l.apply_url or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
