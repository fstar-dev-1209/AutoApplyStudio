"""Ask a question inside a job's existing chat conversation and wait for the fenced answer (UI thread, timer polled)."""

from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal

from core.chat_questions import END, START, answer_after_prompt, count_marked_answers, extract_marked_answer
from core.chat_sites import PAGE_BLOCKS, block_message
from ui.chat_service import ChatService

POLL_MS = 1000
STABLE_POLLS = 2  # the reply must be unchanged for this many polls (about 2 s) after generation stops
# Then, with no markers at all, give up on it. Not sooner than half a minute: ChatGPT shows a short "thinking" line
# before the answer, and the page does not always say that it is still writing.
UNMARKED_POLLS = 30
QUIET_POLLS = 75  # about 75 s of an unchanging conversation with no new answer and nothing being written


@dataclass
class AskResult:
    answer: str = ""
    error: str = ""
    chat_url: str = ""  # the conversation the answer came from (a new one when the saved one could not be used)
    used_saved_chat: bool = False
    raw: str = ""


class ChatAsker(QObject):
    """One question at a time. ``start`` returns False (and does nothing) when the chat page is in use."""

    finished = Signal(object)  # AskResult

    def __init__(self, service: ChatService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self.service = service
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._active = False
        self._in_poll = False
        self._last_seen: dict = {}

    @property
    def active(self) -> bool:
        return self._active

    def start(self, question_prompt: str, *, chat_url: str = "", context_prompt: str = "",
              timeout_sec: int = 300) -> bool:
        svc = self._service
        if self._active or svc.busy:
            return False
        self._active = True
        svc._busy = True  # the same single-page guard generation uses
        self._result = AskResult()
        self._deadline = time.monotonic() + timeout_sec
        self._base_turns = 0
        self._base_text = ""
        self._base_answers = 0
        self._base_page_answers = 0
        self._base_page_text = ""
        self._last_page = ""
        self._quiet = 0
        self._last_text = ""
        self._same = 0
        self._prompt = question_prompt
        self._tail_last, self._tail_same = "", 0
        try:
            err = self._open_conversation(chat_url, context_prompt)
            if err:
                return self._fail(err)
            message = question_prompt if self._result.used_saved_chat else (
                (context_prompt.strip() + "\n\n" if context_prompt.strip() else "") + question_prompt)
            base, err = svc.run_last_reply()
            if err:
                return self._fail(err)
            self._base_turns = int(base.get("turns") or 0)
            self._base_text = str(base.get("text") or "")
            self._base_answers = count_marked_answers(self._base_text)
            self._base_page_answers = count_marked_answers(str(base.get("pageText") or ""))
            self._base_page_text = str(base.get("pageText") or "")
            send_err = svc.send_prompt(message)
            if send_err:
                return self._fail(send_err)
        except Exception as exc:  # noqa: BLE001
            return self._fail(str(exc))
        self._timer.start()
        return True

    # ------------------------------------------------------------------ steps

    def _open_conversation(self, chat_url: str, context_prompt: str) -> str:
        svc = self._service
        label = svc.label
        target = svc.site.clean_chat_url(chat_url) if chat_url else ""
        if target:
            # Already on this conversation: wait for the reload to finish. Opening it again reloads the page.
            if svc.conversation_url() != target and not svc.load_url(target):
                # A slow load (the proxy, or the app just started) is waited out, then tried once more, before the
                # question is given up: a form is open and waiting for this answer.
                if not self._wait_until_loaded(60) and not svc.load_url(target) and not self._wait_until_loaded(45):
                    return f"{label} is still loading, so this question was not sent again."
            if svc.conversation_url() == target:
                err = svc._wait_for_composer(timeout_sec=45)
                if err:
                    return err  # signed out, or still loading: do not open a second page on top of this one
                self._result.used_saved_chat = True
                self._result.chat_url = svc.conversation_url()
                return self._check_signed_in()
        if not context_prompt.strip():
            return f"The saved {label} conversation could not be opened and there is no job context to start a new one."
        err = svc._ensure_new_chat() or svc._wait_for_composer(timeout_sec=60)
        return err or self._check_signed_in()

    def _wait_until_loaded(self, seconds: float) -> bool:
        svc = self._service
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if svc._document_ready() == "complete":
                return True
            svc._pump_events(1500)
        return svc._document_ready() == "complete"

    def _check_signed_in(self) -> str:
        state, err = self._service.run_page_state()
        if err:
            return err
        if not state.get("loggedIn"):
            return self._service._sign_in_message(state)
        return ""

    # ------------------------------------------------------------------ polling

    def _poll(self) -> None:
        """One look at the page. Reading the page runs a nested event loop, during which this timer fires again;
        a long conversation can take over a second to read, so a second look must never start inside the first
        (the looks would pile up and none would finish before the deadline)."""
        if self._in_poll:
            return
        self._in_poll = True
        try:
            self._poll_once()
        finally:
            self._in_poll = False

    def _poll_once(self) -> None:
        svc = self._service
        if time.monotonic() >= self._deadline:
            self._log_timeout()
            self._fail(f"Timed out waiting for {svc.label} to answer.")
            return
        data, err = svc.run_last_reply()
        self._last_seen = {"err": err, "turns": data.get("turns"), "base_turns": self._base_turns,
                           "generating": data.get("generating"), "same": self._same,
                           "tail": str(data.get("text") or "")[-160:], "fallback": data.get("fallback"),
                           "page_answers": count_marked_answers(str(data.get("pageText") or "")),
                           "base_page_answers": self._base_page_answers,
                           "marker_path": str(data.get("markerPath") or "")[:500]}
        if err:
            return  # a navigation in flight; try again next tick
        blocked = str(data.get("blocked") or "")
        if blocked in PAGE_BLOCKS:
            self._fail(block_message(svc.label, blocked))
            return
        text = str(data.get("text") or "")
        changed = bool(text) and text != self._base_text
        if data.get("fallback"):
            # The whole page's text was read, earlier answers included: only one more answer than before is ours.
            changed = changed and count_marked_answers(text) > self._base_answers
        fresh = int(data.get("turns") or 0) > self._base_turns or changed
        page = str(data.get("pageText") or "")
        after = answer_after_prompt(page, self._prompt) if page and page != self._base_page_text else ""
        if after and not data.get("generating"):
            # The reply to OUR question is on the page, whatever the reader made of it (see answer_after_prompt).
            self._tail_same = self._tail_same + 1 if after == self._tail_last else 1
            self._tail_last = after
            if self._tail_same >= STABLE_POLLS:
                self._succeed(after, f"{START}{after}{END}")
                return
        else:
            self._tail_last, self._tail_same = "", 0
        if (not fresh or not extract_marked_answer(text)) and count_marked_answers(page) > self._base_page_answers:
            # The reply element could not be picked out, but the conversation on screen holds one more fenced
            # answer than before the question was sent: that answer is ours.
            fresh, text = True, page
        if not fresh and not data.get("generating") and page and page != self._base_page_text:
            # The conversation changed (our question is in it) but no new fenced answer has come, and nothing is
            # being written. After a while that is a reply without the markers, or no reply at all: say so now, so
            # the question is asked once more, instead of holding the open form for the full five minutes.
            self._quiet = self._quiet + 1 if page == self._last_page else 0
            self._last_page = page
            if self._quiet >= QUIET_POLLS:
                self._result.raw = page[-2000:]
                self._fail(f"{svc.label} answered without the answer markers.")
                return
        if not fresh or data.get("generating"):
            self._same = 0
            self._last_text = text
            return
        self._same = self._same + 1 if text == self._last_text else 0
        self._last_text = text
        answer = extract_marked_answer(text)
        if answer and self._same >= STABLE_POLLS:
            self._succeed(answer, text)
        elif (not answer and START not in text and END not in text and self._same >= UNMARKED_POLLS
              and not data.get("fallback")):
            self._result.raw = text
            self._fail(f"{svc.label} answered without the answer markers.")

    def _log_timeout(self) -> None:
        """What the page looked like at the last look, so a reader that missed an answer can be fixed."""
        try:
            from core.paths import config_dir

            with (config_dir() / "chat_debug.log").open("a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] question timed out; last look: {self._last_seen}\n")
        except Exception:  # noqa: BLE001 - diagnostics must never get in the way
            pass

    # ------------------------------------------------------------------ completion

    def _succeed(self, answer: str, raw: str) -> None:
        self._result.answer, self._result.raw = answer, raw
        self._finish()

    def _fail(self, error: str) -> bool:
        """Finish with an error. Returns True: the request was accepted and its outcome is delivered by ``finished``."""
        self._result.error = error
        self._finish()
        return True

    def _finish(self) -> None:
        self._timer.stop()
        svc = self._service
        if not self._result.error and not self._result.chat_url:
            self._result.chat_url = svc.conversation_url()
        svc.finish_generation()
        self._active = False
        self.finished.emit(self._result)
