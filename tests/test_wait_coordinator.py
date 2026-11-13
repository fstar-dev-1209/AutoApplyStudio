"""Taking the chat's reply only when it is the answer: not the prompt's schema quoted in "thinking" text."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.settings_store import AppSettings
from tests.resume_fixtures import make_resume

pytest.importorskip("PySide6.QtWebEngineCore")
from ui.claude_wait_coordinator import ClaudeWaitCoordinator  # noqa: E402

SCHEMA_ECHO = json.dumps({**make_resume((2, 2)), "company_name": "hiring company from JD, or empty string if absent"})
REAL = json.dumps({**make_resume((2, 2)), "company_name": "Acme"})


class FakeService:
    """What the coordinator reads from the chat page, set by each test."""

    def __init__(self, require_idle: bool):
        self.label = "ChatGPT" if require_idle else "Claude"
        self.site = SimpleNamespace(require_idle=require_idle, poll_clipboard=False)
        self.text, self.generating, self._manual_reply = "", False, None
        self.blocked = ""
        self.has_continue = False
        self.sent: list[str] = []
        self.continued = 0
        self.followup_text: str | None = None

    def try_capture_resume_json(self, use_clipboard=False):
        return self.text

    def run_page_state(self):
        return {
            "isGenerating": self.generating,
            "longestAssistant": len(self.text),
            "replyText": self.text,
            "fullTurnText": self.text,
            "blocked": self.blocked,
            "hasContinue": self.has_continue,
        }, ""

    def send_prompt(self, prompt: str) -> str:
        self.sent.append(prompt)
        if self.followup_text is not None:
            self.text = self.followup_text
            self.generating = False
        return ""

    def click_continue(self) -> None:
        self.continued += 1
        self.has_continue = False


def drive(qapp, service: FakeService):
    coordinator = ClaudeWaitCoordinator()
    done: list = []
    coordinator.finished.connect(done.append)
    coordinator.start(service, AppSettings(), {"company": "Acme", "role_title": "Engineer", "job_url": ""}, "")
    coordinator.stop()  # polled by hand below, not by the timer

    def poll():
        coordinator._poll()
        if coordinator._stable_hash:
            coordinator._stable_since -= 5  # as if the reply had been unchanged for a few seconds
        coordinator._poll()

    return coordinator, done, poll


def test_a_reply_that_is_still_being_written_is_not_taken_on_a_site_that_thinks_aloud(qapp):
    service = FakeService(require_idle=True)
    _, done, poll = drive(qapp, service)
    service.text, service.generating = REAL, True  # a complete-looking block while the chat is still working
    poll()
    assert done == []
    service.generating = False  # now it has finished
    poll()
    assert len(done) == 1 and done[0].error == "" and done[0].resume["company_name"] == "Acme"


def test_the_prompts_schema_quoted_while_generating_is_waited_through_on_every_site(qapp):
    service = FakeService(require_idle=False)  # even where finished-ness is not required, a schema echo is not an answer
    _, done, poll = drive(qapp, service)
    service.text, service.generating = SCHEMA_ECHO, True
    poll()
    assert done == []
    service.text, service.generating = REAL, False  # the real answer arrives, and the stop control is gone
    poll()
    assert len(done) == 1 and done[0].resume["company_name"] == "Acme"


def test_a_finished_reply_that_is_only_the_schema_fails_with_a_clear_message(qapp):
    service = FakeService(require_idle=True)
    _, done, poll = drive(qapp, service)
    service.text, service.generating = SCHEMA_ECHO, False
    poll()
    assert len(done) == 1 and done[0].resume is None
    assert "placeholder" in done[0].error and "empty string" in done[0].error


def test_a_reply_is_not_taken_until_generation_has_stopped(qapp):
    service = FakeService(require_idle=False)  # both sites wait; a stable block under the stop button is not the answer
    _, done, poll = drive(qapp, service)
    service.text, service.generating = REAL, True
    poll()
    assert done == []
    service.generating = False
    poll()
    assert len(done) == 1 and done[0].error == ""


def test_json_already_on_the_clipboard_is_not_this_jobs_resume(qapp):
    from PySide6.QtWidgets import QApplication

    service = FakeService(require_idle=True)
    QApplication.clipboard().setText(json.dumps({**make_resume((2, 2)), "company_name": "Other Co"}))
    _, done, poll = drive(qapp, service)
    poll()
    assert done == []
    service.text, service.generating = REAL, False
    poll()
    assert len(done) == 1 and done[0].resume["company_name"] == "Acme"


def test_a_usage_limit_ends_the_wait(qapp):
    service = FakeService(require_idle=True)
    service.blocked = "limit"
    service.text, service.generating = REAL, False
    _, done, poll = drive(qapp, service)
    poll()
    assert len(done) == 1 and done[0].resume is None and done[0].blocked == "limit"
    assert "usage limit" in done[0].error


def test_a_prose_reply_is_asked_once_for_the_json(qapp):
    service = FakeService(require_idle=True)
    service.text = "Here is a tailored resume in prose. " * 80
    service.generating = False
    service.followup_text = REAL
    _, done, poll = drive(qapp, service)
    poll()
    assert done == [] and len(service.sent) == 1 and "JSON" in service.sent[0]
    poll()
    assert len(done) == 1 and done[0].error == "" and done[0].resume["company_name"] == "Acme"
    assert len(service.sent) == 1


def test_a_second_prose_reply_stops(qapp):
    service = FakeService(require_idle=True)
    service.text = "Here is a tailored resume in prose. " * 80
    service.generating = False
    _, done, poll = drive(qapp, service)
    poll()
    assert len(service.sent) == 1 and done == []
    poll()
    assert len(done) == 1 and done[0].resume is None
    assert "text reply" in done[0].error
    assert len(service.sent) == 1


def test_continue_is_clicked_once_and_the_reply_is_read_again(qapp):
    service = FakeService(require_idle=True)
    service.has_continue = True
    service.text, service.generating = REAL, False
    _, done, poll = drive(qapp, service)
    poll()
    assert service.continued == 1 and done == []
    poll()
    assert len(done) == 1 and done[0].error == "" and service.continued == 1
