#!/usr/bin/env python3
"""Diagnose Claude page scraping vs JSON parsing (run from repo root)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.claude_chat_scripts import CLAUDE_EXTRACT_JSON_JS, CLAUDE_PAGE_STATE_JS
from core.json_extract import find_resume_json_string

SAMPLE_RESUME = {
    "company_name": "Peraton",
    "role_title": "Senior Site Reliability Engineer",
    "summary": "SRE with 8+ years.",
    "cover_letter": "Dear Hiring Team,\n\nParagraph one.\n\nParagraph two.",
    "skill_groups": [{"category": "Cloud", "items": ["AWS", "EKS"]}],
    "experience": [
        {
            "company": "Tenable",
            "role_title": "Senior SRE",
            "dates": "Mar 2022 - Present",
            "bullets": ["Built burn-rate alerts.", "Cut p99 latency 45%."],
        }
    ],
}


def _sample_json_compact() -> str:
    return json.dumps(SAMPLE_RESUME, ensure_ascii=False)


def _html_fixture(name: str, body: str) -> str:
    import html

    safe_body = html.escape(body) if "<" not in body[:20] else body
    return f"""<!DOCTYPE html><html><body>
<nav data-testid="sidebar">sidebar</nav>
<main>
  <div data-testid="conversation">
    <div data-testid="user-message"><div>{'x' * 500} experience in job desc</div></div>
    <div data-testid="conversation-turn">
      <div data-testid="assistant-message" class="font-claude-message">
        {safe_body}
      </div>
    </div>
  </div>
  <div contenteditable="true" data-testid="chat-input"></div>
</main>
</body></html>"""


def _json_in_textarea() -> str:
    j = _sample_json_compact()
    if "</textarea>" in j.lower():
        j = j.replace("</textarea>", "< /textarea>")
    return f'<textarea readonly rows="20" cols="80">{j}</textarea>'


FIXTURES: dict[str, str] = {
    "raw_json_textarea": _html_fixture("raw", _json_in_textarea()),
    "raw_json_pre": _html_fixture("raw", f"<pre>{_sample_json_compact()}</pre>"),
    "markdown_fence": _html_fixture(
        "fence",
        "<p>Here is your tailored resume JSON:</p>"
        f"<pre>```json\n{_sample_json_compact()}\n```</pre>",
    ),
    "prose_then_pre": _html_fixture(
        "prose",
        "<p>I have generated the following JSON for this role.</p>"
        + f"<pre>{_sample_json_compact()}</pre>",
    ),
    "broken_innerText_fence": _html_fixture(
        "broken",
        "<p>```json</p><p>"
        + _sample_json_compact()[:200]
        + "...</p><p>```</p>",
    ),
    "no_json_prose_only": _html_fixture(
        "prose_only",
        "<p>" + ("Summary: Senior engineer with experience in SRE. " * 200) + "</p>",
    ),
}


def run_python_parser_tests() -> None:
    print("=== Python find_resume_json_string ===\n")
    compact = _sample_json_compact()
    cases = [
        ("compact only", compact),
        ("markdown fence", f"Intro\n\n```json\n{compact}\n```\n"),
        ("prose prefix", f"Here you go:\n{compact}"),
        ("truncated no close brace", compact[:4000]),
    ]
    for label, text in cases:
        found = find_resume_json_string(text)
        ok = len(found) > 500
        print(f"  [{label}] in={len(text):,} out={len(found):,} ok={ok}")
        if not ok and len(text) > 500:
            print(f"    preview: {text[:120]!r}...")


def run_webengine_tests() -> int:
    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView

    app = QApplication.instance() or QApplication(sys.argv)
    view = QWebEngineView()
    loop = QEventLoop()
    results: dict[str, dict] = {}
    pending = list(FIXTURES.items())
    tmp = ROOT / "scripts" / "_diag_html"
    tmp.mkdir(exist_ok=True)

    def run_next() -> None:
        if not pending:
            loop.quit()
            return
        name, html = pending.pop(0)
        path = tmp / f"{name}.html"
        path.write_text(html, encoding="utf-8")

        def on_load(ok: bool) -> None:
            try:
                view.loadFinished.disconnect(on_load)
            except TypeError:
                pass
            if not ok:
                results[name] = {"error": "load failed"}
                QTimer.singleShot(0, run_next)
                return

            def after_extract(extract_raw: object) -> None:
                def after_state(state_raw: object) -> None:
                    try:
                        ex = json.loads(str(extract_raw or "{}"))
                    except json.JSONDecodeError:
                        ex = {}
                    try:
                        st = json.loads(str(state_raw or "{}"))
                    except json.JSONDecodeError:
                        st = {}
                    reply = str(st.get("replyText") or "")
                    plain = str(ex.get("plainText") or "")
                    js_text = str(ex.get("text") or "")
                    py_from_reply = find_resume_json_string(reply)
                    py_from_plain = find_resume_json_string(plain)
                    results[name] = {
                        "longestAssistant": st.get("longestAssistant"),
                        "js_extract_len": ex.get("length"),
                        "reply_len": len(reply),
                        "plain_len": len(plain),
                        "py_reply": len(py_from_reply),
                        "py_plain": len(py_from_plain),
                        "js_text_len": len(js_text),
                        "reply_head": reply[:80].replace("\n", " "),
                    }
                    QTimer.singleShot(0, run_next)

                view.page().runJavaScript(CLAUDE_PAGE_STATE_JS, after_state)

            view.page().runJavaScript(CLAUDE_EXTRACT_JSON_JS, after_extract)

        view.loadFinished.connect(on_load)
        view.load(QUrl.fromLocalFile(str(path.resolve())))

    print("\n=== WebEngine CLAUDE_EXTRACT_JSON_JS + PAGE_STATE (local HTML fixtures) ===\n")
    QTimer.singleShot(0, run_next)
    loop.exec()
    view.deleteLater()

    for name, data in results.items():
        print(f"  fixture={name}")
        for k, v in data.items():
            print(f"    {k}: {v}")
        print()
    return 0


def main() -> int:
    run_python_parser_tests()
    try:
        return run_webengine_tests()
    except Exception as exc:  # noqa: BLE001
        print(f"\nWebEngine diagnostics skipped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
