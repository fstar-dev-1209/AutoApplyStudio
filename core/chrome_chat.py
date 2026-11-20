"""Asking ChatGPT an application question in a tab of the bot's own Chrome.

The questions of one application are asked in a ChatGPT tab opened in the same Chrome that fills the form (the bot
browser), on that browser's own thread. Nothing here touches the app window: no clipboard, no window focus, no nested
waiting on the window's thread. The text is typed into the page through the browser itself, and the tab is closed when
the application is done, so its memory goes with it.

Sign in to the ChatGPT account for questions once in the bot browser (Auto Apply > Open the bot browser). The page is
read with the same scripts the embedded chat pages use (core.chatgpt_chat_scripts).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from core.chat_questions import answer_after_prompt, count_marked_answers, extract_marked_answer
from core.chat_sites import CHATGPT, PAGE_BLOCKS, SITES, block_message

POLL_SEC = 1.0
STABLE_POLLS = 2  # the answer must read the same this many times in a row
NO_MARKERS_AFTER_SEC = 45  # a reply that has stopped without the answer markers, for this long, is reported as such
_COMPOSER = ("#prompt-textarea:visible, div.ProseMirror[contenteditable='true']:visible, "
             "div[contenteditable='true'][role='textbox']:visible, textarea:visible")
# The Copy button under a reply ("Copy message" is the one under your own message). ChatGPT no longer puts the text of
# a reply into the page itself, so a reply is read the way a person would take it: Copy, then the clipboard.
_REPLY_COPY = "main button[aria-label='Copy']"
QUIET_BEFORE_COPY_SEC = 2.0  # the page must have stopped writing for this long before the reply is copied


@dataclass
class ChromeAskResult:
    answer: str = ""
    error: str = ""
    chat_url: str = ""
    raw: str = ""


class ChromeChat:
    """One application's ChatGPT tab. ``bind`` gives it the browser context; ``ask`` asks one question; ``close`` closes
    the tab. Every call runs on the browser's thread (inside the application's browser task)."""

    def __init__(self, *, label: str = "ChatGPT", log: Callable[[str], None] = lambda m: None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.site = SITES[CHATGPT]
        self.label = label
        self._log, self._sleep, self._clock = log, sleep, clock
        self._context = None
        self._page = None
        self._base_copies = 0  # Copy buttons under replies before our question was sent

    # ------------------------------------------------------------------ the tab

    def bind(self, context) -> None:
        self._context = context
        try:  # the reply is read from the clipboard after a click on its Copy button
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://chatgpt.com")
        except Exception:  # noqa: BLE001 - a stand-in context in tests, or permissions already set
            pass

    def close(self) -> None:
        page, self._page = self._page, None
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001 - the tab may already be gone with the browser
                pass

    def _tab(self):
        if self._page is None or self._page.is_closed():
            if self._context is None:
                raise RuntimeError("The bot browser is not open.")
            self._page = self._context.new_page()
        return self._page

    def _read(self, script: str) -> dict:
        raw = self._page.evaluate(script)
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ one question

    def ask(self, prompt: str, *, chat_url: str = "", context_prompt: str = "", timeout_sec: int = 300) -> ChromeAskResult:
        result = ChromeAskResult()
        try:
            page = self._tab()
            page.bring_to_front()  # the active tab of its window: never throttled while the answer is written
            target = self.site.clean_chat_url(chat_url) if chat_url else ""
            if target:
                if self.site.clean_chat_url(page.url) != target:
                    page.goto(target, wait_until="domcontentloaded", timeout=60000)
                message = prompt
            else:
                page.goto(self.site.new_chat, wait_until="domcontentloaded", timeout=60000)
                message = (context_prompt.strip() + "\n\n" if context_prompt.strip() else "") + prompt
            err = self._wait_ready(60)
            if err:
                result.error = err
                return result
            base = self._read(self.site.last_reply_js)
            base_page = str(base.get("pageText") or "")
            self._base_copies = self._copy_count()
            err = self._type(message) or self._send()
            if err:
                result.error = err
                return result
            return self._wait_answer(prompt, base_page, timeout_sec, result)
        except Exception as exc:  # noqa: BLE001 - a broken tab is this question's failure, never the application's
            result.error = f"{self.label} page did not respond ({type(exc).__name__}: {str(exc).splitlines()[0][:160]})"
            return result

    def _blocked(self, state: dict) -> str:
        blocked = str(state.get("blocked") or "")
        if blocked in PAGE_BLOCKS:
            return block_message(self.label, blocked)
        if state.get("hasComposer") is False and state.get("loggedIn") is False:
            return block_message(self.label, "login")
        return ""

    def _wait_ready(self, seconds: float) -> str:
        """'' once the message box is there and the account is signed in; else why not."""
        deadline = self._clock() + seconds
        state: dict = {}
        while self._clock() < deadline:
            try:
                state = self._read(self.site.page_state_js)
            except Exception:  # noqa: BLE001 - the page is still loading
                state = {}
            if state.get("blocked") in PAGE_BLOCKS:
                return block_message(self.label, str(state["blocked"]))
            if state.get("hasComposer") and state.get("loggedIn"):
                return ""
            self._sleep(POLL_SEC)
        return self._blocked(state) or f"{self.label} did not show its message box in time."

    def _type(self, message: str) -> str:
        """Type the message into the visible message box through the browser (no clipboard, no window focus)."""
        page = self._page
        box = page.locator(_COMPOSER).first
        box.click(timeout=15000)
        # Put in by the page's own editor (the script the app page uses): typing it key by key through the browser
        # left it garbled ("...OK.ReplyReply with..."). Typing is only the fallback.
        try:
            page.evaluate(self.site.insert_prompt_js.replace("%PROMPT%", json.dumps(message)))
        except Exception:  # noqa: BLE001
            pass
        if int(self._read(self.site.page_state_js).get("composerLength") or 0) < int(len(message) * 0.9):
            self._clear_box()
            box.click(timeout=15000)
            page.keyboard.insert_text(message)
        deadline = self._clock() + 20
        state: dict = {}
        while self._clock() < deadline:
            state = self._read(self.site.page_state_js)
            held = int(state.get("composerLength") or 0)
            if (held >= int(len(message) * 0.9) or int(state.get("attachments") or 0)) and state.get("canSend"):
                return ""
            self._sleep(0.5)
        return (f"{self.label} left Send disabled: the message box holds {int(state.get('composerLength') or 0):,} of "
                f"{len(message):,} characters.")

    def _clear_box(self) -> None:
        try:
            box = self._page.locator(_COMPOSER).first
            box.click(timeout=5000)
            self._page.keyboard.press("Control+A")
            self._page.keyboard.press("Delete")
        except Exception:  # noqa: BLE001
            pass

    def _copy_count(self) -> int:
        try:
            return self._page.locator(_REPLY_COPY).count()
        except Exception:  # noqa: BLE001
            return 0

    def _copy_reply(self) -> str:
        """The last reply's text, by a real click on its Copy button (a script click may not write the clipboard)."""
        button = self._page.locator(_REPLY_COPY).last
        button.scroll_into_view_if_needed(timeout=5000)
        button.click(timeout=5000)
        self._sleep(0.8)
        return str(self._page.evaluate("navigator.clipboard.readText()") or "")

    def _send(self) -> str:
        """Click Send, and once more if the message is still in the box a while later."""
        for attempt in range(2):
            sent = self._read(self.site.click_send_js)
            if not sent.get("ok"):
                return str(sent.get("error") or f"Could not send the message to {self.label}.")
            deadline = self._clock() + 20
            while self._clock() < deadline:
                self._sleep(0.7)
                state = self._read(self.site.page_state_js)
                blocked = self._blocked(state)
                if blocked:
                    return blocked
                if state.get("isGenerating") or int(state.get("composerLength") or 0) < 20:
                    return ""  # the box emptied, or the reply is being written: it was sent
            if attempt == 0:
                self._log(f"[chrome chat] the message was still in the box after Send: clicking again")
        return f"{self.label} did not send the message (it is still in the message box)."

    def _wait_answer(self, prompt: str, base_page: str, timeout_sec: float, result: ChromeAskResult) -> ChromeAskResult:
        deadline = self._clock() + timeout_sec
        last, same, stopped_since, quiet_since = "", 0, 0.0, 0.0
        base_answers = count_marked_answers(base_page)
        copied_without_markers = ""
        data: dict = {}
        while self._clock() < deadline:
            self._sleep(POLL_SEC)
            try:
                data = self._read(self.site.last_reply_js)
            except Exception:  # noqa: BLE001 - the page is moving to the conversation's own address
                continue
            if data.get("blocked") in PAGE_BLOCKS:
                result.error = block_message(self.label, str(data["blocked"]))
                return result
            page_text = str(data.get("pageText") or "")
            answer = answer_after_prompt(page_text, prompt)
            if not answer and count_marked_answers(page_text) > base_answers:
                answer = extract_marked_answer(str(data.get("text") or "")) or extract_marked_answer(page_text)
            generating = bool(data.get("generating"))
            # The reply's text is no longer in the page: once the page has stopped writing and our reply has its own
            # Copy button, the reply is copied.
            quiet_since = 0.0 if generating else (quiet_since or self._clock())
            if (not answer and quiet_since and self._clock() - quiet_since >= QUIET_BEFORE_COPY_SEC
                    and self._copy_count() > self._base_copies):
                try:
                    copied = self._copy_reply()
                except Exception:  # noqa: BLE001 - not clickable yet: the next look tries again
                    copied = ""
                answer = extract_marked_answer(copied)
                if answer:
                    result.answer, result.raw = answer, copied[-2000:]
                    result.chat_url = self.site.clean_chat_url(self._page.url)
                    return result
                if copied.strip():
                    if copied == copied_without_markers:  # the same unmarked reply twice: it is the whole reply
                        result.error = f"{self.label} answered without the answer markers."
                        result.raw = copied[-2000:]
                        result.chat_url = self.site.clean_chat_url(self._page.url)
                        return result
                    copied_without_markers = copied
            if answer and not generating:
                same = same + 1 if answer == last else 1
                last = answer
                if same >= STABLE_POLLS:
                    result.answer, result.raw = answer, page_text[-2000:]
                    result.chat_url = self.site.clean_chat_url(self._page.url)
                    return result
                continue
            same, last = 0, ""
            if generating or page_text == base_page:
                stopped_since = 0.0
                continue
            stopped_since = stopped_since or self._clock()
            if self._clock() - stopped_since > NO_MARKERS_AFTER_SEC:
                result.error = f"{self.label} answered without the answer markers."
                result.raw = page_text[-2000:]
                result.chat_url = self.site.clean_chat_url(self._page.url)
                return result
        result.error = f"Timed out waiting for {self.label} to answer."
        result.raw = str(data.get("pageText") or "")[-2000:]
        return result


__all__ = ["ChromeAskResult", "ChromeChat"]
