"""Application questions asked in a ChatGPT tab of the bot browser (core.chrome_chat), against a stand-in tab."""

from __future__ import annotations

import json

from core.chat_questions import END, START
from core.chat_sites import CHATGPT, SITES
from core.chrome_chat import ChromeChat

SITE = SITES[CHATGPT]
PROMPT = "Write it plainly.\n\nQuestion: Do you have 5+ years of Python?\nReply between the markers."


class FakeTab:
    """What the ChatGPT page shows, step by step: ready, the typed text, then the reply to our question."""

    def __init__(self, *, blocked="", reply=f"{START}Yes{END}", sends_take=True):
        self.url = "about:blank"
        self.blocked, self.reply, self.sends_take = blocked, reply, sends_take
        self.box, self.sent, self.closed, self.reads = "", "", False, 0
        self.keyboard = self
        self.typed: list[str] = []

    # playwright-like surface
    def goto(self, url, **kw):
        self.url = url

    def is_closed(self):
        return self.closed

    def bring_to_front(self):
        pass

    def close(self):
        self.closed = True

    def locator(self, css):
        return self

    @property
    def first(self):
        return self

    def click(self, timeout=0):
        pass

    def insert_text(self, text):
        self.typed.append(text)
        self.box += text

    def evaluate(self, script):
        if script == SITE.page_state_js:
            return json.dumps({"hasComposer": not self.blocked, "loggedIn": not self.blocked, "blocked": self.blocked,
                               "composerLength": len(self.box), "canSend": bool(self.box), "attachments": 0,
                               "isGenerating": False})
        if script == SITE.click_send_js:
            if self.sends_take:
                self.sent, self.box = self.box, ""
                self.url = "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"
            return json.dumps({"ok": True})
        if script == SITE.last_reply_js:
            self.reads += 1
            page = "Earlier text"
            if self.sent:
                page += "\nYou said:\n" + self.sent + "\nChatGPT said:\n" + self.reply
            return json.dumps({"pageText": page, "text": self.reply if self.sent else "", "generating": False})
        raise AssertionError("unexpected script")


class FakeContext:
    def __init__(self, tab):
        self.tab = tab

    def new_page(self):
        return self.tab


def chat_for(tab):
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    chat = ChromeChat(sleep=sleep, clock=lambda: clock["t"])
    chat.bind(FakeContext(tab))
    return chat


def test_a_question_is_typed_into_a_new_chat_with_its_context_and_answered():
    tab = FakeTab()
    chat = chat_for(tab)
    result = chat.ask(PROMPT, context_prompt="JOB DESCRIPTION: build things")
    assert result.error == "" and result.answer == "Yes"
    assert tab.typed[0].startswith("JOB DESCRIPTION: build things") and tab.typed[0].endswith(PROMPT)
    assert result.chat_url == "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"
    # the next question of the form continues in that chat, without the context
    tab.reply = f"{START}No{END}"
    second = chat.ask(PROMPT.replace("Python", "Rust"), chat_url=result.chat_url)
    assert second.answer == "No" and tab.typed[1].startswith("Write it plainly")
    chat.close()
    assert tab.closed


def test_a_human_check_or_the_usage_limit_is_reported_with_the_usual_words():
    result = chat_for(FakeTab(blocked="challenge")).ask(PROMPT)
    assert "human-verification check" in result.error
    assert "usage limit" in chat_for(FakeTab(blocked="limit")).ask(PROMPT).error


def test_a_message_that_is_never_sent_is_reported():
    result = chat_for(FakeTab(sends_take=False)).ask(PROMPT)
    assert "did not send the message" in result.error


def test_a_reply_without_markers_is_reported_and_its_text_kept():
    result = chat_for(FakeTab(reply="I would say yes.")).ask(PROMPT)
    assert "without the answer markers" in result.error and "I would say yes." in result.raw


def test_without_a_browser_the_question_fails_cleanly():
    chat = ChromeChat()
    assert "did not respond" in chat.ask(PROMPT).error
