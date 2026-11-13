"""The extension's autofill, run in a real Chromium (Qt WebEngine) against hand-written Greenhouse-style forms.

The fixtures approximate the markup of classic and script-built Greenhouse forms; they are not copies of live
pages, so these tests prove the logic and the safety rules, not that every employer form is understood.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from core import apply_kit
from core.settings_store import ProfileSettings

pytest.importorskip("PySide6.QtWebEngineCore")
from PySide6.QtCore import QEventLoop, QTimer, QUrl  # noqa: E402
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
AUTOFILL = (ROOT / "extension" / "autofill.js").read_text(encoding="utf-8")
FIXTURES = Path(__file__).parent / "js" / "fixtures"

PROFILE = ProfileSettings(name="Mary Ann van der Berg", email="mary@example.com", phone="(555) 010-0000",
                          location="Austin, TX", linkedin_url="https://linkedin.com/in/mary",
                          work_authorization="Yes", needs_sponsorship="No")
KIT = {"job": {"id": "j1", "company": "Acme", "role": "Engineer"}, "profile": apply_kit.profile_payload(PROFILE)}


def _file(name: str, mime: str) -> dict:
    return {"kind": "x", "name": name, "mime": mime, "base64": base64.b64encode(b"data:" + name.encode()).decode()}


FILES = {
    "pdf": _file("M_Resume.pdf", "application/pdf"),
    "resume": _file("M_Resume.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "cover": _file("M_Cover_Letter.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
}


class Browser:
    def __init__(self, qapp) -> None:
        self.profile = QWebEngineProfile(qapp)  # off the record: nothing is written to disk
        self.page = QWebEnginePage(self.profile, qapp)

    def run(self, script: str):
        loop, box = QEventLoop(), []
        self.page.runJavaScript(script, 0, lambda value: (box.append(value), loop.quit()))
        QTimer.singleShot(15000, loop.quit)
        loop.exec()
        assert box, "the page did not answer"
        return box[0]

    def load(self, fixture: str, url: str) -> None:
        loop = QEventLoop()
        self.page.loadFinished.connect(lambda ok: loop.quit())
        QTimer.singleShot(20000, loop.quit)
        self.page.setHtml((FIXTURES / fixture).read_text(encoding="utf-8"), QUrl(url))
        loop.exec()

    def fill(self, kit=KIT, files=FILES) -> dict:
        return json.loads(self.run(
            f"{AUTOFILL}\n;JSON.stringify(globalThis.__appStudioAutofill.fill(document, {json.dumps(kit)}, {json.dumps(files)}))"))


@pytest.fixture
def browser(qapp):
    b = Browser(qapp)
    yield b
    b.page.deleteLater()
    b.profile.deleteLater()


@pytest.fixture
def classic(browser):
    browser.load("greenhouse_classic.html", "https://boards.greenhouse.io/acme/jobs/1")
    return browser


@pytest.fixture
def modern(browser):
    browser.load("greenhouse_react.html", "https://job-boards.greenhouse.io/acme/jobs/2")
    return browser


def test_classic_form_is_filled_from_the_profile(classic):
    report = classic.fill()
    assert report["matched"] and report["site"] == "greenhouse"
    values = json.loads(classic.run("JSON.stringify(Object.fromEntries(['first_name','last_name','email','phone','question_1','question_2','question_3'].map(i=>[i,document.getElementById(i).value])))"))
    assert values["first_name"] == "Mary Ann" and values["last_name"] == "van der Berg"
    assert values["email"] == "mary@example.com" and values["phone"] == "(555) 010-0000"
    assert values["question_1"] == "https://linkedin.com/in/mary"
    assert values["question_2"] == ""  # no website saved: left empty, not invented
    assert values["question_3"] == "1"  # "Yes" to work authorization from Settings
    assert classic.run("document.querySelector('input[name=sponsor]:checked').value") == "0"  # "No" to sponsorship


def test_files_are_attached_in_a_format_the_form_accepts(classic):
    report = classic.fill()
    names = classic.run("JSON.stringify([...document.querySelectorAll('input[type=file]')].map(i=>i.files.length?i.files[0].name:''))")
    assert json.loads(names) == ["M_Resume.pdf", "M_Cover_Letter.docx"]  # the PDF resume, the DOCX cover letter
    assert sorted(report["files"]) == ["Cover letter: M_Cover_Letter.docx", "Resume: M_Resume.pdf"]


def test_nothing_is_submitted_and_unknown_or_sensitive_questions_are_left(classic):
    report = classic.fill()
    assert classic.run("!!window.submitted || !!window.clicked") is False  # never clicks or submits
    assert classic.run("document.getElementById('gender').value") == ""  # self-identification is never answered
    assert any("Gender" in item for item in report["skipped"])
    assert not any("Gender" in item for item in report["needs_you"])
    assert "Why do you want to work at Acme?" in report["needs_you"]  # a free-text question is the person's job
    assert classic.run("document.getElementById('question_5').value") == ""


def test_existing_answers_are_never_overwritten(classic):
    classic.run("document.getElementById('first_name').value = 'Typed'; 1")
    classic.fill()
    assert classic.run("document.getElementById('first_name').value") == "Typed"
    classic.run("document.getElementById('resume').files.length")  # still readable after a second pass
    again = classic.fill()
    assert again["files"] == []  # files already attached are left alone


def test_blank_eligibility_answers_are_not_guessed(classic):
    kit = {**KIT, "profile": {**KIT["profile"], "work_authorization": "", "needs_sponsorship": ""}}
    report = classic.fill(kit)
    assert classic.run("document.getElementById('question_3').value") == ""
    assert classic.run("document.querySelector('input[name=sponsor]:checked') === null") is True
    assert any("visa sponsorship" in item for item in report["needs_you"])


def test_controlled_inputs_see_the_change(modern):
    report = modern.fill()
    state = json.loads(modern.run("JSON.stringify(window.state)"))
    assert state["first_name"] == "Mary Ann" and state["phone"] == "(555) 010-0000"  # real input events were fired
    assert state["email"] == "typed@by.me"  # what the person typed stays
    assert state["q_github"] == ""  # no GitHub saved
    assert any(item.startswith("Location") for item in report["needs_you"])  # type-ahead needs a human click
    assert modern.run("document.getElementById('candidate-location').value") == ""


def test_a_file_the_form_does_not_accept_is_not_forced(modern):
    report = modern.fill()
    names = json.loads(modern.run("JSON.stringify([...document.querySelectorAll('input[type=file]')].map(i=>i.files.length?i.files[0].name:''))"))
    assert names == ["M_Resume.pdf", ""]  # accept=".pdf": the DOCX cover letter is not attached
    assert "Cover letter (attach it yourself)" in report["needs_you"]
    assert modern.run("!!window.clicked") is False


def test_other_sites_and_pages_without_a_form_are_untouched(browser):
    browser.load("greenhouse_classic.html", "https://example.com/careers/1")
    report = browser.fill()
    assert report == {**report, "matched": False, "reason": "unsupported_site"}
    assert browser.run("document.getElementById('first_name').value") == ""
    browser.load("greenhouse_react.html", "https://job-boards.greenhouse.io/acme")
    browser.run("document.querySelector('form').remove(); 1")
    assert browser.fill()["reason"] == "no_form"
