"""The web-chat driver (Claude and ChatGPT) against stand-in pages: page reading, generation, the conversation
address, and asking a question inside a job's saved conversation. See ``chat_fakes`` for what that does and
does not prove."""

from __future__ import annotations

import json

import pytest

from core.chat_questions import END, START, Question, build_question_prompt
from core.chat_sites import CHATGPT, CLAUDE, SITES
from tests.chat_fakes import CHAT_ID, FakeBrowser, urls
from tests.resume_fixtures import make_resume

pytest.importorskip("PySide6.QtWebEngineCore")
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402

from ui.chat_ask import AskResult, ChatAsker  # noqa: E402
from ui.chat_service import ChatService  # noqa: E402

FLAVORS = [CLAUDE, CHATGPT]
RESUME_JSON = json.dumps(make_resume((2, 2)))


@pytest.fixture
def fake(qapp):
    b = FakeBrowser(qapp)
    yield b
    b.close()


def _service(fake: FakeBrowser, flavor: str) -> ChatService:
    service = ChatService(fake.page, site=SITES[flavor])

    def load_url(url: str, timeout_ms: int = 60000) -> bool:
        # No network in tests: "navigating" to a conversation address loads the stand-in page there. A saved
        # conversation that no longer exists is redirected to the new-chat page by the real sites.
        gone = getattr(service, "gone", set())
        target = urls(flavor)["home"] if url in gone else url
        return fake.load(flavor, target)

    service.load_url = load_url  # type: ignore[method-assign]
    return service


def _wait_for(signal, start, timeout_ms=60000):
    loop, got = QEventLoop(), []
    signal.connect(lambda value: (got.append(value), loop.quit()))
    QTimer.singleShot(timeout_ms, loop.quit)
    start()
    if not got:
        loop.exec()
    assert got, "the asker never finished"
    return got[0]


# ------------------------------------------------------------------ reading the page

@pytest.mark.parametrize("flavor", FLAVORS)
def test_page_state_of_a_ready_signed_in_page(fake, flavor):
    assert fake.load(flavor, urls(flavor)["home"])
    state, err = _service(fake, flavor).run_page_state()
    assert not err
    assert state["loggedIn"] and state["hasComposer"] and not state["isGenerating"]


@pytest.mark.parametrize("flavor", FLAVORS)
def test_a_logged_out_page_is_not_signed_in(fake, flavor):
    assert fake.load(flavor, urls(flavor)["home"], state="logged_out")
    state, _ = _service(fake, flavor).run_page_state()
    assert not state["loggedIn"]


@pytest.mark.parametrize("flavor", FLAVORS)
def test_a_human_check_is_reported_as_such(fake, flavor):
    assert fake.load(flavor, urls(flavor)["home"], state="challenge")
    service = _service(fake, flavor)
    state, _ = service.run_page_state()
    assert state["blocked"] == "challenge" and not state["loggedIn"]
    assert "human-verification" in service._sign_in_message(state)


@pytest.mark.parametrize("flavor", FLAVORS)
def test_a_usage_limit_stops_before_anything_is_sent(fake, flavor):
    service = _service(fake, flavor)
    assert fake.load(flavor, urls(flavor)["home"], state="limit")
    state, _ = service.run_page_state()
    assert state["blocked"] == "limit" and not state["loggedIn"]
    reply, _ = service.run_last_reply()
    assert reply.get("blocked") == "limit"

    def load_limited(url: str, timeout_ms: int = 60000) -> bool:
        return fake.load(flavor, url, state="limit")

    service.load_url = load_limited  # type: ignore[method-assign]
    _before, err = service.begin_generation("Write the resume JSON for this job. " * 5)
    assert err and "usage limit" in err
    assert fake.sent() == [] and not service.busy


@pytest.mark.parametrize("flavor", FLAVORS)
def test_resume_json_is_found_in_the_assistant_reply_only(fake, flavor):
    fake.load(flavor, urls(flavor)["chat"])
    # the user's own message holds a JSON-looking snippet; only the assistant's reply may be taken
    fake.seed("user", 'Here is my old resume {"company_name": "Old", "skill_groups": [{"category": "x"}]}')
    fake.seed("assistant", "Sure, here it is:\n```json\n" + RESUME_JSON + "\n```")
    text, err = _service(fake, flavor)._extract_json_from_page()
    assert not err and json.loads(text)["name"] == "Test Person"


# ------------------------------------------------------------------ generation and the saved address

@pytest.mark.parametrize("flavor", FLAVORS)
def test_generation_returns_the_json_and_remembers_the_conversation(fake, flavor):
    service = _service(fake, flavor)
    fake.load(flavor, urls(flavor)["home"])
    fake.set_reply("(t) => 'Here is the resume:\\n```json\\n' + " + json.dumps(RESUME_JSON) + " + '\\n```'")
    # begin_generation opens a new chat itself; the stand-in loads the new-chat page again
    _before, err = service.begin_generation("Write the resume JSON for this job. " * 5)
    assert not err
    assert service.last_chat_url.endswith(CHAT_ID), "the conversation address must be captured after sending"
    assert SITES[flavor].is_chat_url(service.last_chat_url)
    text, wait_err = service._wait_for_new_assistant_reply(timeout_sec=60)
    service.finish_generation()
    assert not wait_err and json.loads(text)["name"] == "Test Person"


def test_chat_urls_are_recognised_per_site():
    claude, gpt = SITES[CLAUDE], SITES[CHATGPT]
    assert claude.clean_chat_url(f"https://claude.ai/chat/{CHAT_ID}?x=1#frag") == f"https://claude.ai/chat/{CHAT_ID}"
    assert gpt.clean_chat_url(f"https://chatgpt.com/c/{CHAT_ID}/") == f"https://chatgpt.com/c/{CHAT_ID}"
    assert gpt.is_chat_url(f"https://chatgpt.com/g/g-abc123/c/{CHAT_ID}")
    for site, url in ((claude, "https://claude.ai/new"), (claude, f"https://evil.example/chat/{CHAT_ID}"),
                      (gpt, "https://chatgpt.com/"), (gpt, f"https://chatgpt.com.evil.example/c/{CHAT_ID}")):
        assert site.clean_chat_url(url) == "" and not site.is_chat_url(url)


# ------------------------------------------------------------------ asking in a saved conversation

def _ask(service, **kwargs) -> AskResult:
    asker = ChatAsker(service)
    prompt = build_question_prompt(Question("Describe your background with RAG.", max_chars=300))
    return _wait_for(asker.finished, lambda: asker.start(prompt, **kwargs), timeout_ms=90000)


ANSWER_REPLY = ("(t) => 'Happy to help!\\n" + START + "I built retrieval pipelines at Acme and shipped them to "
                "production.' + '" + END + "\\nLet me know if you want it shorter.'")


@pytest.mark.parametrize("flavor", FLAVORS)
def test_a_question_is_answered_in_the_saved_conversation(fake, flavor):
    service = _service(fake, flavor)
    saved = urls(flavor)["chat"]
    fake.load(flavor, saved)
    # an earlier answer in the same conversation must not be mistaken for the new one
    fake.seed("assistant", f"Earlier.\n{START}An old answer.{END}")
    fake.set_reply(ANSWER_REPLY)

    result = _ask(service, chat_url=saved, context_prompt="JOB CONTEXT")
    assert not result.error, result.error
    assert result.used_saved_chat and result.chat_url == saved
    assert result.answer == "I built retrieval pipelines at Acme and shipped them to production."
    sent = fake.sent()
    assert len(sent) == 1 and "Describe your background with RAG." in sent[0]
    assert "JOB CONTEXT" not in sent[0], "a saved conversation already has the job context"
    assert not service.busy, "the chat page is released when the answer is in"


@pytest.mark.parametrize("flavor", FLAVORS)
def test_a_missing_conversation_starts_a_new_one_with_the_job_context(fake, flavor):
    service = _service(fake, flavor)
    saved = urls(flavor)["chat"]
    service.gone = {saved}  # type: ignore[attr-defined]
    fake.load(flavor, urls(flavor)["home"])
    fake.set_reply(ANSWER_REPLY)

    result = _ask(service, chat_url=saved, context_prompt="JOB DESCRIPTION: build RAG systems")
    assert not result.error, result.error
    assert not result.used_saved_chat
    assert SITES[flavor].is_chat_url(result.chat_url), "the new conversation's address is returned for saving"
    assert "JOB DESCRIPTION: build RAG systems" in fake.sent()[0]


def test_without_a_saved_conversation_or_context_it_says_so(fake):
    service = _service(fake, CHATGPT)
    fake.load(CHATGPT, urls(CHATGPT)["home"])
    result = _ask(service, chat_url="", context_prompt="")
    assert result.error and not result.answer
    assert not service.busy


def test_a_reply_without_markers_is_an_error_not_a_guess(fake):
    service = _service(fake, CHATGPT)
    saved = urls(CHATGPT)["chat"]
    fake.load(CHATGPT, saved)
    fake.set_reply("(t) => 'I would say I have a lot of experience with retrieval.'")
    result = _ask(service, chat_url=saved)
    assert "markers" in result.error and not result.answer
    assert "retrieval" in result.raw


def test_asking_is_refused_while_the_page_is_generating_a_resume(fake):
    service = _service(fake, CHATGPT)
    service._busy = True
    asker = ChatAsker(service)
    assert asker.start("question") is False
    assert not asker.active


def test_not_signed_in_is_reported(fake):
    service = _service(fake, CHATGPT)
    saved = urls(CHATGPT)["chat"]

    def load_logged_out(url: str, timeout_ms: int = 60000) -> bool:
        return fake.load(CHATGPT, url, state="logged_out")

    service.load_url = load_logged_out  # type: ignore[method-assign]
    fake.load(CHATGPT, saved, state="logged_out")
    result = _ask(service, chat_url=saved, context_prompt="ctx")
    assert result.error and "sign" in result.error.lower()


def test_both_sites_read_the_reply_with_its_copy_button_while_waiting():
    # ChatGPT no longer puts a reply's text into the page: its Copy button is how the reply is read
    assert SITES[CLAUDE].poll_clipboard is True and SITES[CHATGPT].poll_clipboard is True
    assert SITES[CLAUDE].require_idle and SITES[CHATGPT].require_idle


def test_json_already_on_the_clipboard_is_not_taken_as_the_reply(fake):
    from PySide6.QtWidgets import QApplication

    service = _service(fake, CLAUDE)
    fake.load(CLAUDE, urls(CLAUDE)["chat"])
    QApplication.clipboard().setText(RESUME_JSON)
    assert service._try_clipboard_via_copy_button() == ""


def test_a_disabled_send_button_is_not_treated_as_sent(fake):
    service = _service(fake, CHATGPT)
    fake.disable_send()
    fake.load(CHATGPT, urls(CHATGPT)["home"])
    _before, err = service.begin_generation("Write the resume JSON for this job. " * 5)
    assert err and "disabled" in err.lower()
    assert fake.sent() == []


def test_a_send_that_does_not_start_a_reply_fails(fake):
    service = _service(fake, CHATGPT)
    fake.hold_send()
    fake.load(CHATGPT, urls(CHATGPT)["home"])
    _before, err = service.begin_generation("Write the resume JSON for this job. " * 5)
    assert err and "did not start" in err
    assert not service.busy


def test_load_url_leaves_an_open_conversation_where_it_is(fake):
    service = ChatService(fake.page, site=SITES[CHATGPT])
    saved = urls(CHATGPT)["chat"]
    fake.load(CHATGPT, saved)
    calls: list[str] = []
    service._page.load = lambda url: calls.append(url.toString())  # type: ignore[method-assign]
    assert service.load_url(saved)
    assert calls == []


def test_another_question_in_the_open_conversation_does_not_reload_it(fake):
    service = _service(fake, CHATGPT)
    saved = urls(CHATGPT)["chat"]
    opened: list[str] = []
    real_load = service.load_url

    def tracking(url: str, timeout_ms: int = 60000) -> bool:
        opened.append(url)
        return real_load(url, timeout_ms)

    service.load_url = tracking  # type: ignore[method-assign]
    fake.load(CHATGPT, saved)
    fake.set_reply(ANSWER_REPLY)
    first = _ask(service, chat_url=saved, context_prompt="JOB CONTEXT")
    assert not first.error, first.error
    second = _ask(service, chat_url=saved, context_prompt="JOB CONTEXT")
    assert not second.error, second.error
    assert opened == [], "the conversation is already open, so neither question may reload it"


def test_a_question_is_not_asked_in_whatever_chat_is_already_open(fake):
    service = _service(fake, CHATGPT)
    other = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    saved = urls(CHATGPT)["chat"]
    opened: list[str] = []
    real_load = service.load_url

    def tracking(url: str, timeout_ms: int = 60000) -> bool:
        opened.append(url)
        return real_load(url, timeout_ms)

    service.load_url = tracking  # type: ignore[method-assign]
    fake.load(CHATGPT, other)
    fake.set_reply(ANSWER_REPLY)
    result = _ask(service, chat_url=saved, context_prompt="JOB CONTEXT")
    assert not result.error, result.error
    assert saved in opened
    assert result.used_saved_chat and result.chat_url == saved
    assert "JOB CONTEXT" not in fake.sent()[0]


@pytest.mark.parametrize("flavor", FLAVORS)
def test_continue_is_clicked_once(fake, flavor):
    service = _service(fake, flavor)
    fake.load(flavor, urls(flavor)["chat"])
    fake.run(
        "const b = document.createElement('button');"
        "b.setAttribute('aria-label', 'Continue generating');"
        "b.textContent = 'Continue';"
        "b.addEventListener('click', () => { window.__continued = 1; b.remove(); });"
        "document.body.appendChild(b); true"
    )
    state, err = service.run_page_state()
    assert not err and state["hasContinue"] is True
    service.click_continue()
    assert fake.run("window.__continued || 0") == 1
    state, _ = service.run_page_state()
    assert state["hasContinue"] is False


def test_a_hidden_chat_page_is_shown_before_the_prompt_is_pasted(qapp, qtbot):
    from ui.claude_chat_view import ClaudeChatView

    view = ClaudeChatView(site_key=CHATGPT)
    qtbot.addWidget(view)
    view.show()
    view.browser.hide()  # the Needs you page is in front, which is when Send stayed disabled
    assert not view.browser.isVisible()
    view.chat_service()._reveal_chat()
    assert view.browser.isVisible()


def test_the_chat_page_stays_active_when_its_widget_is_hidden(qapp, qtbot):
    from ui.claude_chat_view import ClaudeChatView

    view = ClaudeChatView(site_key=CHATGPT)
    qtbot.addWidget(view)
    view.show()
    view.hide()  # another page of the app takes the screen
    page = view.browser.page()
    assert page.lifecycleState().name == "Active" and page.isVisible()


# ------------------------------------------------------------------ ChatGPT layouts without author markers

SCHEMA_PROMPT = ('Write the resume as JSON: {"company_name": "hiring company from JD, or empty string if absent", '
                 '"experience": [{"company": "employer", "bullets": ["what you did"]}], '
                 '"skill_groups": [{"category": "name", "items": ["skill"]}]}')
LAYOUTS = ["chatgpt_headed", "chatgpt_bare"]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_reply_is_found_when_messages_carry_no_author_attribute(fake, layout):
    service = ChatService(fake.page, site=SITES[CHATGPT])
    fake.load(layout, urls(layout)["chat"])
    # the user's own message holds the schema from the prompt; only the reply may be taken
    fake.seed("user", "Write the resume JSON for this job." if layout == "chatgpt_bare" else SCHEMA_PROMPT)
    fake.seed("assistant", "Here you go:\n```json\n" + RESUME_JSON + "\n```")
    state, err = service.run_page_state()
    assert not err and state["longestAssistant"] > 200, state
    text, err = service._extract_json_from_page()
    assert not err and json.loads(text)["name"] == "Test Person"


def test_the_users_schema_is_never_returned_as_the_reply_when_the_layout_marks_turns_by_heading(fake):
    service = ChatService(fake.page, site=SITES[CHATGPT])
    fake.load("chatgpt_headed", urls("chatgpt_headed")["chat"])
    fake.seed("user", SCHEMA_PROMPT)  # no reply yet: nothing may be taken from the prompt
    text, _ = service._extract_json_from_page()
    assert text == ""


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_question_is_answered_in_a_layout_without_author_attributes(fake, layout):
    service = ChatService(fake.page, site=SITES[CHATGPT])
    saved = urls(layout)["chat"]

    def load_url(url: str, timeout_ms: int = 60000) -> bool:
        return fake.load(layout, url)

    service.load_url = load_url  # type: ignore[method-assign]
    fake.load(layout, saved)
    fake.set_reply(ANSWER_REPLY)
    result = _ask(service, chat_url=saved, context_prompt="JOB CONTEXT")
    assert not result.error, result.error
    assert result.answer == "I built retrieval pipelines at Acme and shipped them to production."


def test_when_nothing_is_found_the_pages_structure_is_saved_without_its_text(fake, tmp_path, monkeypatch):
    import core.paths

    monkeypatch.setattr(core.paths, "config_dir", lambda: tmp_path)
    service = ChatService(fake.page, site=SITES[CHATGPT])
    fake.load("chatgpt_bare", urls("chatgpt_bare")["chat"])
    fake.seed("assistant", "Secret reply text that must not be logged " * 3 + '{"company_name": "Acme"}')
    path = service.dump_page_outline()
    log = (tmp_path / "chat_debug.log").read_text(encoding="utf-8")
    assert path.endswith("chat_debug.log") and '"testids"' in log and "json_block_chain_from_inside_out" in log
    assert "Secret reply text" not in log
