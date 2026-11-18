"""Two chat pages on one account: resumes on one, application-form questions on the other, taking turns only at the
moment of pasting (the clipboard is shared)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.chat_sites import CHATGPT, SITES

pytest.importorskip("PySide6.QtWebEngineCore")

from tests.chat_fakes import FakeBrowser  # noqa: E402
from ui.chat_service import ChatService  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


@pytest.fixture
def fake(qapp):
    b = FakeBrowser(qapp)
    yield b
    b.close()


@pytest.fixture(autouse=True)
def _no_owner_left_behind():
    yield
    ChatService._paste_owner = None


def test_questions_have_their_own_page_that_shares_the_sign_in(qapp, qtbot):
    from ui.claude_chat_view import ClaudeChatView

    view = ClaudeChatView(site_key=CHATGPT)
    qtbot.addWidget(view)
    view.show()
    resumes, questions = view.chat_service(), view.question_service()
    assert resumes is not questions and resumes._page is not questions._page
    assert view.browser is not view.ask_browser
    # one profile: signing in on one page is signing in on the other
    assert view.browser.page().profile() is view.ask_browser.page().profile()
    # each service finds its own browser, so each can be shown and pasted into
    assert resumes._chat_view() is view.browser and questions._chat_view() is view.ask_browser
    assert view.browser.isVisible() and view.ask_browser.isVisible()  # side by side, both on screen


def test_clearing_the_session_gives_both_pages_a_fresh_service(qapp, qtbot, monkeypatch):
    from ui import claude_chat_view as module

    monkeypatch.setattr(module, "clear_chat_browser_data", lambda key: None)
    monkeypatch.setattr(module, "ask_yes_no", lambda *a, **k: True)
    monkeypatch.setattr(module, "show_info", lambda *a, **k: None)
    view = module.ClaudeChatView(site_key=CHATGPT)
    qtbot.addWidget(view)
    before = view.chat_service(), view.question_service()
    view._clear_session()
    after = view.chat_service(), view.question_service()
    assert after[0] is not before[0] and after[1] is not before[1]
    assert after[0]._chat_view() is view.browser and after[1]._chat_view() is view.ask_browser


def test_only_one_page_pastes_at_a_time(fake):
    a, b = ChatService(fake.page, site=SITES[CHATGPT]), ChatService(fake.page, site=SITES[CHATGPT])
    seen: list[bool] = []

    def paste(prompt):
        seen.append(ChatService.paste_in_progress())
        # while this page pastes, the other is told to wait and does not touch the clipboard
        seen.append("must wait" in b._wake_send_button("other prompt"))
        return ""

    a._paste_prompt = paste  # type: ignore[method-assign]
    b._paste_prompt = lambda prompt: pytest.fail("the second page pasted while the first held the clipboard")  # type: ignore[method-assign]
    assert not ChatService.paste_in_progress()
    assert a._wake_send_button("prompt") == ""
    assert seen == [True, True]
    assert not ChatService.paste_in_progress()  # released, so the other page can have its turn
    b._paste_prompt = lambda prompt: ""  # type: ignore[method-assign]
    assert b._wake_send_button("other prompt") == ""


def test_the_turn_is_released_even_when_pasting_fails(fake):
    a = ChatService(fake.page, site=SITES[CHATGPT])

    def boom(prompt):
        raise RuntimeError("page went away")

    a._paste_prompt = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        a._wake_send_button("prompt")
    assert not ChatService.paste_in_progress()


def test_a_resume_waits_for_the_other_page_to_finish_pasting():
    window = SimpleNamespace(_claude_begin_deferred=False, _pending_claude=None)
    ChatService._paste_owner = object()  # type: ignore[assignment]  # the question page is pasting
    MainWindow._claude_begin_on_main(window)  # nothing started, and the worker is told to ask again
    assert window._claude_begin_deferred is True


def test_a_question_waits_for_the_resume_page_to_finish_pasting(fake):
    resumes, questions = ChatService(fake.page, site=SITES[CHATGPT]), ChatService(fake.page, site=SITES[CHATGPT])
    asker = SimpleNamespace(service=questions, active=False)
    window = SimpleNamespace(
        _asker=asker, claude_view=SimpleNamespace(chat_service=lambda: resumes),
        _question_service=lambda: questions, _ask_waiter=None)
    ChatService._paste_owner = resumes
    assert MainWindow._start_ask(window, {"deliver": None}) is False  # the caller tries again in a few seconds


def test_resumes_no_longer_wait_for_questions_to_be_answered():
    """The old rule held the resume queue while a form question was open; separate pages make that unnecessary."""
    from core.queue_engine import QueueEngine

    assert not hasattr(QueueEngine, "_yield_to_questions")
    assert not hasattr(MainWindow, "_questions_want_the_chat")


def test_an_idle_page_that_grew_too_large_is_replaced_and_a_busy_one_is_not(qapp, qtbot, monkeypatch):
    import ui.claude_chat_view as chat_view
    from ui.claude_chat_view import ClaudeChatView

    view = ClaudeChatView(site_key=CHATGPT)
    qtbot.addWidget(view)
    monkeypatch.setattr(chat_view, "private_mb", lambda pid: 3000)
    resumes, questions = view.chat_service(), view.question_service()
    old_resume_page, old_question_page = resumes._page, questions._page
    questions._busy = True  # answering a question right now
    assert view.recycle_large_pages() == ["resumes"]
    assert resumes._page is view.browser.page() and resumes._page is not old_resume_page
    assert questions._page is old_question_page
    questions._busy = False
    monkeypatch.setattr(chat_view, "private_mb", lambda pid: 400)
    assert view.recycle_large_pages() == []  # small pages are left alone


def test_the_questions_page_pastes_as_plain_text(qapp, qtbot):
    from ui.claude_chat_view import ClaudeChatView

    view = ClaudeChatView(site_key=CHATGPT)
    qtbot.addWidget(view)
    assert view.question_service().plain_paste is True
    assert view.chat_service().plain_paste is False
