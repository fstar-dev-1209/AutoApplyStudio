#!/usr/bin/env python3
"""Check the chat drivers against the REAL, signed-in claude.ai / chatgpt.com page.

Run from the repo root:   .venv\\Scripts\\python scripts\\diagnose_chat.py chatgpt     (or: claude)

A window opens with the same saved sign-in the app uses. Sign in if needed, open any conversation that has at least
one reply, then press "Run checks". The printout shows what the drivers can see: composer, send/stop buttons,
assistant messages and the signed-in state. If a line says MISSING, that site changed its page and the selectors in
core/chatgpt_chat_scripts.py (or core/claude_chat_scripts.py) need a one-line fix. Nothing is sent to the chat.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SELECTOR_PROBES = {
    "chatgpt": {
        "composer": "#prompt-textarea, div[contenteditable='true'].ProseMirror",
        "send button": "button[data-testid='send-button']",
        "stop button": "button[data-testid='stop-button']",
        "assistant messages": "[data-message-author-role='assistant']",
        "user messages": "[data-message-author-role='user']",
        "copy button": "button[data-testid='copy-turn-action-button']",
        "log-in button (should be absent when signed in)": "[data-testid='login-button']",
    },
    "claude": {
        "composer": "div[contenteditable='true']",
        "send button": "button[aria-label*='Send' i]",
        "stop button": "button[aria-label*='Stop' i]",
        "assistant messages": "[data-testid='assistant-message'], .font-claude-message",
        "user messages": "[data-testid='user-message']",
        "copy button": "button[aria-label*='Copy' i]",
    },
}


def main() -> int:
    key = (sys.argv[1] if len(sys.argv) > 1 else "chatgpt").lower()
    if key not in SELECTOR_PROBES:
        print("usage: diagnose_chat.py [claude|chatgpt]")
        return 2

    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

    from core.chat_browser_profile import chat_web_profile
    from core.chat_sites import SITES

    site = SITES[key]
    app = QApplication(sys.argv)
    window = QWidget()
    window.setWindowTitle(f"Chat driver check - {site.label}")
    layout = QVBoxLayout(window)
    view = QWebEngineView()
    view.setPage(QWebEnginePage(chat_web_profile(key), view))
    run = QPushButton("Run checks (nothing is sent to the chat)")
    layout.addWidget(view, 1)
    layout.addWidget(run)
    window.resize(1200, 850)
    view.load(QUrl(site.home))

    def report(label: str, raw: object) -> None:
        try:
            data = json.loads(str(raw or "{}"))
        except json.JSONDecodeError:
            data = {"unreadable": str(raw)[:200]}
        print(f"\n--- {label}")
        for name, value in data.items():
            text = str(value)
            print(f"  {name}: {text[:140] + '...' if len(text) > 140 else text}")

    def probe() -> None:
        print(f"\n===== {site.label} at {view.url().toString()}")
        page = view.page()
        script = "JSON.stringify(Object.fromEntries(Object.entries(%s).map(([k, s]) => " \
                 "[k, document.querySelectorAll(s).length])))" % json.dumps(SELECTOR_PROBES[key])
        page.runJavaScript(script, lambda raw: report("elements found (count)", raw))
        page.runJavaScript(site.page_state_js, lambda raw: report("page state the app reads", raw))
        page.runJavaScript(site.last_reply_js, lambda raw: report("last reply the app reads", raw))
        print(f"  conversation address recognised: {bool(site.clean_chat_url(view.url().toString()))}")

    run.clicked.connect(probe)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
