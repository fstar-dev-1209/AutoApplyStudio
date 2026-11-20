"""Non-blocking Claude reply wait using QTimer (keeps the UI responsive)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from core.chat_sites import PAGE_BLOCKS, block_message
from core.claude_limits import MIN_RESUME_JSON_SNIPPET_CHARS
from core.json_extract import (
    JsonExtractError,
    classify_claude_reply,
    extract_usable_resume,
    find_resume_json_string,
)
from core.resume_finalize import finalize_resume_object
from core.settings_store import AppSettings
from ui.claude_chat_service import CLAUDE_REPLY_WAIT_SEC, ClaudeChatService


# One follow-up when the model answers in prose or with JSON that does not parse. Not sent a second time.
NOTHING_AFTER_IDLE_POLLS = 40  # about a minute stopped with nothing readable: the JSON is asked for again

JSON_ONLY_FOLLOWUP = (
    "That reply cannot be used. Reply again with ONLY one raw JSON object for this resume. "
    "No markdown fences, no explanation, and no questions."
)


@dataclass
class ClaudeGenResult:
    resume: dict[str, Any] | None
    error: str
    blocked: str = ""  # login | challenge | limit when the page needs a person


class ClaudeWaitCoordinator(QObject):
    """Polls for resume JSON without nested minute-long QEventLoops."""

    finished = Signal(object)  # ClaudeGenResult

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._poll)
        self._service: ClaudeChatService | None = None
        self._settings: AppSettings | None = None
        self._meta: dict[str, str] = {}
        self._deadline = 0.0
        self._last_log = 0.0
        self._stable_hash = ""
        self._stable_since = 0.0
        self._stable_plain_len = 0
        self._stable_plain_since = 0.0
        self._text_before = ""
        self._copy_clicks = 0
        self._prose_warned = False
        self._progress = None
        self._idle_streak = 0
        self._continued = False
        self._asked_for_json = False
        self._in_poll = False
        self._done = False

    def start(
        self,
        service: ClaudeChatService,
        settings: AppSettings,
        meta: dict[str, str],
        text_before: str,
        progress=None,
    ) -> None:
        self._service = service
        self._settings = settings
        self._meta = meta
        self._text_before = text_before or ""
        self._progress = progress
        self._deadline = time.monotonic() + CLAUDE_REPLY_WAIT_SEC
        self._last_log = 0.0
        self._stable_hash = ""
        self._stable_since = 0.0
        self._stable_plain_len = 0
        self._stable_plain_since = 0.0
        self._copy_clicks = 0
        self._prose_warned = False
        self._idle_streak = 0
        self._continued = False
        self._asked_for_json = False
        self._done = False
        label = service.label
        self._note(
            f"Waiting for {label} to finish responding "
            f"(often 5+ minutes; up to {CLAUDE_REPLY_WAIT_SEC // 60} minutes). "
            f"You can stay on Queue — copy JSON from {label} or use Import JSON anytime."
        )
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _note(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    def _poll(self) -> None:
        if self._in_poll or self._done:
            return
        self._in_poll = True
        try:
            self._poll_once()
        finally:
            self._in_poll = False

    def _poll_once(self) -> None:
        service = self._service
        if service is None or self._settings is None:
            self._finish(ClaudeGenResult(None, "Claude wait was not started correctly."))
            return

        if time.monotonic() >= self._deadline:
            dump = getattr(service, "dump_page_outline", None)
            if dump:
                dump()  # what the page looked like when nothing was found (structure only)
            minutes = max(1, CLAUDE_REPLY_WAIT_SEC // 60)
            self._finish(
                ClaudeGenResult(
                    None,
                    f"Timed out after {minutes} minutes. Use Import JSON with the {service.label} reply, "
                    f"or Extract from {service.label} in the inspector.",
                )
            )
            return

        manual = getattr(service, "_manual_reply", None)
        if manual:
            self._complete_with_text(manual, f"Using JSON captured from the {service.label} tab.")
            return

        state, err = service.run_page_state()
        if err:
            return
        blocked = str(state.get("blocked") or "")
        if blocked in PAGE_BLOCKS:
            if hasattr(service, "last_block"):
                service.last_block = blocked
            self._finish(ClaudeGenResult(None, block_message(service.label, blocked), blocked))
            return

        generating = bool(state.get("isGenerating"))
        longest = int(state.get("longestAssistant") or 0)
        reply = str(state.get("replyText") or state.get("fullTurnText") or "")
        if generating:
            # The stop control is up. A JSON-looking block here is still the reply being written, or the prompt's
            # schema quoted in thinking text. It is not the answer on either site.
            self._idle_streak = 0
            self._stable_hash = ""
        else:
            self._idle_streak += 1

        if time.monotonic() - self._last_log > 20:
            hint = "streaming" if generating else "parsing"
            self._note(
                f"Waiting for {service.label} to finish "
                f"({longest:,} chars visible, {hint})."
            )
            self._last_log = time.monotonic()

        if generating:
            return

        if state.get("hasContinue") and not self._continued:
            click = getattr(service, "click_continue", None)
            if click is not None:
                self._continued = True
                self._idle_streak = 0
                self._stable_hash = ""
                self._note(f"Clicked Continue in {service.label} and waiting for the rest of the reply.")
                try:
                    click()
                except Exception as exc:  # noqa: BLE001
                    self._finish(ClaudeGenResult(None, str(exc)))
                return

        self._copy_clicks += 1
        # Copy is clicked only once the reply has stopped, and only the text that click places on the clipboard counts.
        # every other idle look (ChatGPT's reply text is only reachable through its Copy button now)
        use_clipboard = bool(service.site.poll_clipboard) and self._copy_clicks % 2 == 0
        text = service.try_capture_resume_json(use_clipboard=use_clipboard) or ""
        copied = str(getattr(service, "last_copied_text", "") or "")
        if copied and not reply:
            reply, longest = copied, max(longest, len(copied))
        if not text or len(text) < MIN_RESUME_JSON_SNIPPET_CHARS:
            text = find_resume_json_string(reply) or text
        if self._is_previous_reply(text):
            text = ""

        usable = ""
        if text and len(text) >= MIN_RESUME_JSON_SNIPPET_CHARS:
            try:
                extract_usable_resume(text)
                usable = text
            except JsonExtractError:
                usable = ""

        if not usable:
            kind = classify_claude_reply(reply or text)
            # A reply that is not the JSON (text only, or JSON cut short) is asked for again once. A copied reply counts
            # whatever its length; one read from the page only when it is long enough to be a finished reply.
            if self._idle_streak >= 2 and (longest >= 2000 or copied) and kind in {"prose_only", "broken_json"}:
                self._ask_for_json(service, kind)
            elif self._idle_streak >= NOTHING_AFTER_IDLE_POLLS and not reply and not text:
                # stopped, and nothing to read at all: ask once more for the JSON instead of waiting out the clock
                self._ask_for_json(service, "prose_only")
            return

        # Two idle polls, then the same JSON unchanged for a moment: that is the finished reply.
        digest = f"{len(usable)}:{usable[:80]}:{usable[-80:]}"
        if digest != self._stable_hash:
            self._stable_hash = digest
            self._stable_since = time.monotonic()
            return
        if self._idle_streak >= 2 and time.monotonic() - self._stable_since >= 1.5:
            self._complete_with_text(usable, "Resume JSON found — continuing export.")

    def _is_previous_reply(self, text: str) -> bool:
        before = self._text_before
        if not before or not text:
            return False
        if text == before:
            return True
        return text.startswith(before) and len(text) - len(before) < 200

    def _ask_for_json(self, service, kind: str) -> None:
        if self._asked_for_json or getattr(service, "send_prompt", None) is None:
            if kind == "broken_json":
                message = (
                    f"The JSON in {service.label} is still incomplete after one retry. "
                    f"Copy the full block from {service.label}, or use Import JSON."
                )
            else:
                message = (
                    f"{service.label} finished with a text reply, not resume JSON. "
                    "Update your prompt to require ONLY a single JSON object (no prose), "
                    f"or copy the JSON from {service.label} and use Import JSON."
                )
            self._finish(ClaudeGenResult(None, message))
            return
        self._asked_for_json = True
        self._idle_streak = 0
        if hasattr(service, "last_copied_text"):
            service.last_copied_text = ""  # the next reply is read on its own
        self._stable_hash = ""
        self._note(f"Asking {service.label} once for the raw JSON only.")
        try:
            err = service.send_prompt(JSON_ONLY_FOLLOWUP)
        except Exception as exc:  # noqa: BLE001
            self._finish(ClaudeGenResult(None, str(exc)))
            return
        if err:
            self._finish(ClaudeGenResult(None, str(err)))

    def _complete_with_text(self, text: str, log_msg: str) -> None:
        self._note(log_msg)
        try:
            obj = extract_usable_resume(text)
            validated = finalize_resume_object(
                dict(obj),
                self._settings,
                company=self._meta.get("company", ""),
                role_title=self._meta.get("role_title", ""),
                job_url=self._meta.get("job_url", ""),
            )
            self._finish(ClaudeGenResult(validated, ""))
        except Exception as exc:  # noqa: BLE001
            self._finish(ClaudeGenResult(None, str(exc)))

    def _finish(self, result: ClaudeGenResult) -> None:
        if self._done:
            return
        self._done = True
        self.stop()
        self.finished.emit(result)
