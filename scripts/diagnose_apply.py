#!/usr/bin/env python3
"""Dry-run the Greenhouse apply flow on ONE real posting. Nothing is ever sent.

Run from the repo root:
    .venv\\Scripts\\python scripts\\diagnose_apply.py https://job-boards.greenhouse.io/<company>/jobs/<id> [--resume path.pdf]

It opens the bot browser (the one with your proxy and sign-ins), reads the application form, prints every field with what the bot decided
to do about it and why, fills the form, checks it, saves a screenshot, and stops. Open questions are reported as
"needs an answer" here because no chat is attached. Read the printout: a field it misreads, or a question it should have
answered, is exactly what to fix before switching auto apply on.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.apply_facts import DEFAULTS  # noqa: E402
from core.browser_host import BrowserConfig, BrowserHost  # noqa: E402
from core.form_answers import Context  # noqa: E402
from core.form_filler import Files  # noqa: E402
from core.greenhouse import GreenhouseApplier  # noqa: E402
from core.paths import config_dir  # noqa: E402
from core.settings_store import SettingsStore  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1].startswith("-"):
        print(__doc__)
        return 2
    url = argv[1]
    resume = argv[argv.index("--resume") + 1] if "--resume" in argv else ""
    store = SettingsStore()
    s = store.settings
    user, password = store.proxy_credentials()
    host = BrowserHost(BrowserConfig(profile_dir=config_dir() / "browser" / "bot", channel=s.bot_browser, proxy=s.web_proxy,
                                     proxy_user=user, proxy_password=password))
    ctx = Context(profile=s.profile, facts={**DEFAULTS, **s.apply_facts}, chat_enabled=False,
                  has_resume=bool(resume), has_cover=False)
    shots = ROOT / "scripts" / "_diagnose_apply"
    applier = GreenhouseApplier(shots_dir=shots, notify=lambda t, b: print(f"[alert] {t}: {b}"))
    host.start()
    try:
        out = host.run(lambda c: applier.apply(c, url, form_ctx=ctx, files=Files(resume=resume), submit=False,
                                               tag="diagnose"), timeout=600)
    finally:
        host.stop()

    print(f"\nResult: {out.state}  -  {out.reason}")
    print(f"\n{'Field':52} {'Kind':9} {'Req':3} {'Decision':7} {'Source':8} Value / reason")
    for p in out.plan:
        detail = p["value"] if p["action"] == "fill" else p["reason"]
        print(f"{p['label'][:50]:52} {p['kind']:9} {'*' if p['required'] else '':3} {p['action']:7} {p['source']:8} {detail[:70]}")
    if out.needs:
        print("\nStopped because:")
        for n in out.needs:
            print("  -", n)
    if out.screenshot:
        print(f"\nScreenshot: {out.screenshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
