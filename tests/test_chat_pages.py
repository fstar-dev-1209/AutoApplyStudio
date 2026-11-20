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


def test_questions_have_their_own_page_signed_in_to_their_own_account(qapp, qtbot):
    from ui.claude_chat_view import ClaudeChatView

    view = ClaudeChatView(site_key=CHATGPT)
    qtbot.addWidget(view)
    view.show()
    resumes, questions = view.chat_service(), view.question_service()
    assert resumes is not questions and resumes._page is not questions._page
    assert view.browser is not view.ask_browser
    # two accounts: each page has its own saved profile (its own sign-in), and says which one it is
    assert view.browser.page().profile() is not view.ask_browser.page().profile()
    assert questions.label == "ChatGPT (questions account)" and resumes.label == "ChatGPT"
    # each service finds its own browser, so each can be shown and pasted into
    assert resumes._chat_view() is view.browser and questions._chat_view() is view.ask_browser
    assert view.browser.isVisible() and view.ask_browser.isVisible()  # side by side, both on screen


def test_each_account_is_signed_out_on_its_own_without_closing_its_page(qapp, qtbot, monkeypatch):
    from ui import claude_chat_view as module

    signed_out, wiped = [], []
    monkeypatch.setattr(module, "sign_out", lambda profile: signed_out.append(profile))
    monkeypatch.setattr(module, "request_wipe", lambda key, account="": wiped.append((key, account)))
    monkeypatch.setattr(module, "ask_yes_no", lambda *a, **k: True)
    monkeypatch.setattr(module, "show_info", lambda *a, **k: None)
    view = module.ClaudeChatView(site_key=CHATGPT)
    qtbot.addWidget(view)
    resumes, questions = view.chat_service(), view.question_service()
    pages = view.browser.page(), view.ask_browser.page()
    view._clear_session("questions")
    assert signed_out == [pages[1].profile()] and wiped == [(CHATGPT, "questions")]
    view._clear_session("resumes")
    assert signed_out[-1] is pages[0].profile() and wiped[-1] == (CHATGPT, "")
    # the pages and services are the same objects: nothing was deleted under them
    assert (view.browser.page(), view.ask_browser.page()) == pages
    assert view.chat_service() is resumes and view.question_service() is questions


def test_a_signed_out_account_starts_from_empty_files_at_the_next_start(tmp_path, monkeypatch):
    import core.chat_browser_profile as profiles

    monkeypatch.setattr(profiles, "config_dir", lambda: tmp_path)
    root = profiles.chat_browser_root(CHATGPT)
    (root / "storage").mkdir(parents=True)
    (root / "storage" / "Cookies").write_text("old sign-in")
    profiles.request_wipe(CHATGPT)
    profiles._profiles.pop(CHATGPT, None)
    profile = profiles.chat_web_profile(CHATGPT)
    try:
        assert not (root / "storage" / "Cookies").exists() and not (tmp_path / "browser" / "chatgpt.wipe").exists()
    finally:
        profiles._profiles.pop(CHATGPT, None)
        profile.deleteLater()


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



def test_send_counts_only_when_our_text_is_in_the_box(qapp, qtbot):
    from ui.claude_chat_view import ClaudeChatView

    view = ClaudeChatView(site_key=CHATGPT)
    qtbot.addWidget(view)
    svc = view.question_service()
    svc._run_page_state = lambda: ({"canSend": True, "composerLength": 1, "attachments": 0}, "")
    assert svc._wait_send_enabled(0.6, 50)[2] is False  # a button on an empty box is not a pasted prompt
    svc._run_page_state = lambda: ({"canSend": True, "composerLength": 18000, "attachments": 0}, "")
    assert svc._wait_send_enabled(0.6, 50)[2] is True


def test_the_two_accounts_are_kept_in_separate_folders(tmp_path, monkeypatch):
    import core.chat_browser_profile as profiles

    monkeypatch.setattr(profiles, "config_dir", lambda: tmp_path)
    assert profiles.chat_browser_root(CHATGPT) == tmp_path / "browser" / "chatgpt"
    assert profiles.chat_browser_root(CHATGPT, "questions") == tmp_path / "browser" / "chatgpt-questions"


def test_without_the_questions_pane_only_the_resume_page_is_shown_and_loaded(qapp, qtbot):
    from ui.claude_chat_view import ClaudeChatView

    view = ClaudeChatView(site_key=CHATGPT, questions_pane=False)
    qtbot.addWidget(view)
    view.show()
    assert view.browser.isVisible() and not view.ask_browser.isVisible()
    assert view.ask_browser.url().toString() in ("", "about:blank")  # never loads the chat
    assert not view.clear_questions_btn.isVisible()


def test_signing_in_again_after_a_sign_out_keeps_the_new_sign_in(tmp_path, monkeypatch):
    import core.chat_browser_profile as profiles

    monkeypatch.setattr(profiles, "config_dir", lambda: tmp_path)
    profiles.request_wipe(CHATGPT)
    profiles.cancel_wipe(CHATGPT)
    assert not (tmp_path / "browser" / "chatgpt.wipe").exists()
