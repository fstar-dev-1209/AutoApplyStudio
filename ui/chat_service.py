"""Web chat automation (Claude, ChatGPT) on the Qt main thread."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from PySide6.QtCore import QEventLoop, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import QApplication

from core.chat_sites import CLAUDE, PAGE_BLOCKS, SITES, ChatSite, block_message
from core.claude_limits import MIN_RESUME_JSON_SNIPPET_CHARS
from core.json_extract import (
    JsonExtractError,
    classify_claude_reply,
    extract_usable_resume,
    find_resume_json_string,
)

ProgressFn = Callable[[str], None]

# A chat often needs 5+ minutes on large prompts; hard cap for one job.
CLAUDE_REPLY_WAIT_SEC = 600
CHAT_REPLY_WAIT_SEC = CLAUDE_REPLY_WAIT_SEC
LOGIN_GRACE_SEC = 6.0


@dataclass
class _GenResult:
    resume: dict[str, Any] | None
    error: str


class ChatService(QObject):
    """Drives one chat site (claude.ai or chatgpt.com) in an embedded browser. Must run on the GUI thread."""

    activity = Signal(str)

    # The system clipboard is shared by every chat page in the app. While one page pastes its prompt (the clipboard
    # holds that prompt until the page has taken it) no other page may start one: it would paste the wrong text.
    _paste_owner: ClassVar["ChatService | None"] = None

    @classmethod
    def paste_in_progress(cls) -> bool:
        """True while a chat page is in the middle of pasting; a message for another page must wait its turn."""
        return cls._paste_owner is not None

    def __init__(self, page: QWebEnginePage, parent: QObject | None = None, *, site: ChatSite | None = None,
                 view=None) -> None:
        super().__init__(parent)
        self._page = page
        self._view = view  # the browser widget showing this page, when it is not the parent's ``browser``
        # Paste as plain text (Ctrl+Shift+V): ChatGPT then takes the message as typing, never as a "Pasted text" file.
        # Set on the application-questions page (see ClaudeChatView).
        self.plain_paste = False
        self.site: ChatSite = site or SITES[CLAUDE]
        self._busy = False
        self._progress: ProgressFn | None = None
        self._manual_reply: str | None = None
        self._baseline_reply_len = 0
        self._reply_text_before = ""
        self._slot_result = _GenResult(None, "")
        # The conversation the last generation was written in (see ``capture_conversation_url``).
        self.last_chat_url = ""
        self.last_block = ""  # login | challenge | limit while the page needs a person, else ""
        self._assistant_before = 0

    @property
    def label(self) -> str:
        return self.site.label

    def replace_page(self, page: QWebEnginePage) -> None:
        """Drive a fresh page from now on (the old one is being recycled to free its memory; see ClaudeChatView)."""
        self._page = page

    @property
    def busy(self) -> bool:
        return self._busy

    def set_progress_handler(self, handler: ProgressFn | None) -> None:
        self._progress = handler

    def set_manual_reply(self, text: str) -> None:
        """Use when the user pulls JSON from the chat tab during a wait."""
        self._manual_reply = text.strip() or None

    # ------------------------------------------------------------------ conversation address

    def current_url(self) -> str:
        return self._page.url().toString()

    def conversation_url(self) -> str:
        """The address of the conversation on screen, or '' while it is still the new-chat page."""
        return self.site.clean_chat_url(self.current_url())

    def capture_conversation_url(self, timeout_sec: float = 20.0) -> str:
        """Wait for the site to move to the new conversation's own address, and remember it."""
        deadline = time.monotonic() + timeout_sec
        while True:
            url = self.conversation_url()
            if url:
                self.last_chat_url = url
                return url
            if time.monotonic() >= deadline:
                return ""
            self._pump_events(400)

    # ------------------------------------------------------------------ capturing resume JSON

    def pull_resume_json_from_page(self) -> tuple[str, str]:
        """Read resume JSON from the chat tab (copy button, DOM, then reply text)."""
        return self.capture_resume_json(use_clipboard=True)

    def capture_resume_json(self, use_clipboard: bool = True) -> tuple[str, str]:
        label = self.label
        text = self.try_capture_resume_json(use_clipboard=use_clipboard)
        if text:
            try:
                extract_usable_resume(text)
            except JsonExtractError as exc:
                return "", str(exc)
            return text, ""
        state, _ = self._run_page_state()
        reply = str(state.get("fullTurnText") or state.get("replyText") or "")
        kind = classify_claude_reply(reply)
        n = len(reply)
        if kind == "prose_only":
            return "", (
                f"The {label} reply ({n:,} characters) looks like formatted text, not JSON. "
                f"Ask {label} for raw JSON only, or copy the JSON block and use Import JSON."
            )
        if kind == "broken_json":
            return "", (
                f"Found JSON-like text ({n:,} characters) but it could not be parsed. "
                f"In the {label} tab, use Copy on the code block, then Import JSON in the inspector."
            )
        if n > 0:
            return "", (
                f"No valid resume JSON in the {label} tab ({n:,} characters visible, type={kind}). "
                "Open the chat that contains the completed reply, then try again or use Import JSON."
            )
        saved = self.dump_page_outline()
        return "", (
            f"No {label} reply visible. Open the {label} tab, select the chat with the completed "
            "response, then try Extract again or use Import JSON."
            + (f"\n\nThe page's structure (no text) was saved to {saved} so the reader can be fixed." if saved else "")
        )

    def dump_page_outline(self) -> str:
        """Write the page's structure (attribute names, counts, no content) to chat_debug.log; returns its path."""
        try:
            from core.chat_script_common import DOM_OUTLINE_JS
            from core.paths import config_dir

            raw, err = self._run_js(DOM_OUTLINE_JS, timeout_ms=15000)
            if err or not raw:
                return ""
            path = config_dir() / "chat_debug.log"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.site.key}\n{raw}\n\n")
            return str(path)
        except Exception:  # noqa: BLE001 - diagnostics must never get in the way
            return ""

    def try_capture_resume_json(self, use_clipboard: bool = True) -> str:
        if use_clipboard:
            found = self._try_clipboard_via_copy_button()
            if found:
                return found
        dom_text, err = self._extract_json_from_page()
        if dom_text:
            return dom_text
        state, state_err = self._run_page_state()
        if state_err:
            return ""
        reply = str(state.get("fullTurnText") or state.get("replyText") or "")
        return find_resume_json_string(reply)

    def _try_clipboard_via_copy_button(self) -> str:
        app = QApplication.instance()
        if app is None:
            return ""
        clipboard = app.clipboard()
        before = clipboard.text()
        self.try_click_copy_on_last_reply()

        loop = QEventLoop()
        QTimer.singleShot(800, loop.quit)
        loop.exec()
        clip = clipboard.text()
        # Only text the Copy click just put on the clipboard. Whatever was already there belongs to some other copy.
        if not clip or clip == before:
            return ""
        return find_resume_json_string(clip)

    def _note(self, message: str) -> None:
        self.activity.emit(message)
        if self._progress:
            self._progress(message)

    # ------------------------------------------------------------------ generation

    def begin_generation(self, prompt: str, *, reuse_tab: bool = False) -> tuple[str, str]:
        """Open chat (or reuse tab), send prompt. Returns (text_before, error message or '')."""
        label = self.label
        if self._busy:
            return "", f"{label} chat is already processing another job."
        self._busy = True
        self._manual_reply = None
        self.last_chat_url = ""
        self.last_block = ""
        try:
            if reuse_tab:
                self._note(
                    f"Reusing current {label} tab — looking for the existing reply (not resending the prompt)."
                )
                ready_err = self._wait_for_composer(timeout_sec=30)
                if ready_err:
                    self.finish_generation()
                    return "", ready_err
                before_text, _ = self._extract_json_from_page()
                self._reply_text_before = before_text or ""
                self.last_chat_url = self.conversation_url()
                existing = self.try_capture_resume_json(use_clipboard=self.site.poll_clipboard)
                if existing:
                    self._manual_reply = existing
                return self._reply_text_before, ""
            else:
                self._note(f"Opening new {label} chat ({len(prompt):,} characters to send).")
                nav_err = self._ensure_new_chat()
                if nav_err:
                    self.finish_generation()
                    return "", nav_err

            ready_err = self._wait_for_composer(timeout_sec=60)
            if ready_err:
                self.finish_generation()
                return "", ready_err

            state, err = self._run_page_state()
            if err:
                self.finish_generation()
                return "", err
            if not state.get("loggedIn"):
                self.finish_generation()
                return "", self._sign_in_message(state)

            before_text, _ = self._extract_json_from_page()
            self._reply_text_before = before_text or ""

            send_err = self._send_prompt(prompt)
            if send_err:
                self.finish_generation()
                return before_text or "", send_err
            self.capture_conversation_url()
            return self._reply_text_before, ""
        except Exception as exc:  # noqa: BLE001
            self.finish_generation()
            return "", str(exc)

    def _sign_in_message(self, state: dict[str, Any]) -> str:
        blocked = str(state.get("blocked") or "")
        if blocked not in PAGE_BLOCKS:
            blocked = "login"
        self.last_block = blocked
        return block_message(self.label, blocked)

    def click_continue(self) -> None:
        """Click the site's Continue control once, when a reply was cut off."""
        if self.site.click_continue_js:
            self._run_js(self.site.click_continue_js, timeout_ms=8000)

    def finish_generation(self) -> None:
        self._busy = False
        self._manual_reply = None

    def try_click_copy_on_last_reply(self) -> None:
        self._run_js(self.site.click_copy_js, timeout_ms=8000)

    @Slot(str)
    def generate_resume_json_slot(self, prompt: str) -> None:
        """Legacy synchronous path; prefer begin_generation + ClaudeWaitCoordinator."""
        _text_before, prep_err = self.begin_generation(prompt)
        if prep_err:
            self._slot_result = _GenResult(None, prep_err)
            return
        text, wait_err = self._wait_for_new_assistant_reply(timeout_sec=CHAT_REPLY_WAIT_SEC)
        self.finish_generation()
        if wait_err:
            self._slot_result = _GenResult(None, wait_err)
            return
        try:
            obj = extract_usable_resume(text)
        except JsonExtractError as exc:
            self._slot_result = _GenResult(None, str(exc))
            return
        self._slot_result = _GenResult(obj, "")

    def _wait_for_composer(self, timeout_sec: int = 60) -> str:
        deadline = time.monotonic() + timeout_sec
        login_since = 0.0
        while time.monotonic() < deadline:
            state, err = self._run_page_state()
            if err:
                # A reload has no page to read yet. Wait for it; starting another load would reload again.
                if self._navigation_error(err):
                    self._pump_events(500)
                    continue
                return err
            if state.get("blocked") in {"challenge", "limit"}:
                return self._sign_in_message(state)
            if state.get("loggedIn") and state.get("hasComposer"):
                self._note(f"{self.label} composer is ready.")
                return ""
            if state.get("blocked") == "login":
                # a login prompt can flash up while the page is still settling; only a lasting one means signed out
                login_since = login_since or time.monotonic()
                if time.monotonic() - login_since >= LOGIN_GRACE_SEC:
                    return self._sign_in_message(state)
            else:
                login_since = 0.0
            self._pump_events(500)
        return f"{self.label} composer did not become ready. Reload the {self.label} tab and try again."

    def _navigation_error(self, err: str) -> bool:
        text = (err or "").lower()
        return "did not respond" in text or "navigation" in text

    def _document_ready(self) -> str:
        """'complete' once the document has finished loading. '' while a reload is in progress."""
        raw, err = self._run_js("document.readyState", timeout_ms=4000)
        return "" if err else (raw or "")

    def _urls_match(self, current: str, wanted: str) -> bool:
        def norm(value: str) -> tuple[str, str]:
            parsed = QUrl((value or "").strip())
            host = parsed.host().lower()
            if host.startswith("www."):
                host = host[4:]
            return host, (parsed.path().rstrip("/") or "/")

        return bool(wanted) and norm(current) == norm(wanted)

    def load_url(self, url: str, timeout_ms: int = 60000) -> bool:
        """Open ``url`` and wait until the page has finished loading.

        A conversation that is already open, or a reload that is already in progress, is waited out. Loading it
        again is what made ChatGPT reload, finish, and reload once more for every question."""
        wait_sec = max(1, timeout_ms // 1000)
        if self._urls_match(self.current_url(), url):
            if self._document_ready() != "complete":
                self._note(f"Waiting for {self.label} to finish loading.")
            return self._wait_for_composer(timeout_sec=wait_sec) == ""
        if self._document_ready() != "complete":
            self._note(f"{self.label} is still loading — waiting before opening anything else.")
            if self._wait_for_composer(timeout_sec=wait_sec) == "" and self._urls_match(self.current_url(), url):
                return True
            if self._document_ready() != "complete":
                return False  # still reloading: the caller must not start a second navigation
        loop = QEventLoop()
        loaded = {"ok": False}

        def on_load(ok: bool) -> None:
            loaded["ok"] = ok
            loop.quit()

        self._page.loadFinished.connect(on_load)
        self._page.load(QUrl(url))
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        loop.exec()
        timer.stop()
        try:
            self._page.loadFinished.disconnect(on_load)
        except (TypeError, RuntimeError):
            pass
        self._pump_events(800)
        return loaded["ok"]

    def _ensure_new_chat(self) -> str:
        if not self.load_url(self.site.new_chat):
            ready = self._document_ready()
            return (f"Could not open a new {self.label} conversation "
                    f"(page at {self.current_url() or 'unknown'}, document {ready or 'not ready'}).")
        return ""

    def _clear_composer(self) -> None:
        """Empty the message box (text and any attached cards). ChatGPT keeps an unsent draft between visits, and a
        draft left from another job must never be sent as this job's prompt."""
        self._run_js(
            "(() => { const c = document.querySelector('#prompt-textarea, div.ProseMirror[contenteditable=\"true\"], "
            "textarea'); if (!c) return 'none'; const scope = c.closest('form') || document; "
            "scope.querySelectorAll('button').forEach(b => { const l = (b.getAttribute('aria-label') || '').toLowerCase(); "
            "if (/^remove|remove (file|attachment)|delete file/.test(l)) b.click(); }); c.focus(); "
            "if (c.tagName === 'TEXTAREA') { c.value = ''; c.dispatchEvent(new Event('input', {bubbles: true})); } "
            "else { const r = document.createRange(); r.selectNodeContents(c); const sel = window.getSelection(); "
            "if (sel) { sel.removeAllRanges(); sel.addRange(r); } document.execCommand('delete'); } return 'ok'; })()",
            timeout_ms=8000)

    def run_page_state(self) -> tuple[dict[str, Any], str]:
        return self._run_page_state()

    def _run_page_state(self) -> tuple[dict[str, Any], str]:
        raw, err = self._run_js(self.site.page_state_js, timeout_ms=20000)
        if err:
            return {}, err
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}, f"Could not read {self.label} page state."
        if not isinstance(data, dict):
            return {}, f"Unexpected {self.label} page state."
        return data, ""

    def run_last_reply(self) -> tuple[dict[str, Any], str]:
        """{turns, text, generating, blocked, hasComposer, url}: the last assistant message as plain text."""
        raw, err = self._run_js(self.site.last_reply_js, timeout_ms=20000)
        if err:
            return {}, err
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}, f"Could not read the {self.label} reply."
        return (data, "") if isinstance(data, dict) else ({}, f"Unexpected {self.label} reply.")

    def _extract_json_from_page(self) -> tuple[str, str]:
        raw, err = self._run_js(self.site.extract_json_js, timeout_ms=90000)
        if err:
            return "", err
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return "", f"Could not read {self.label} page content."

        blobs: list[str] = []
        js_text = str(data.get("text") or "")
        if js_text:
            blobs.append(js_text)
        plain = str(data.get("plainText") or "")
        if plain:
            blobs.append(plain)

        state, state_err = self._run_page_state()
        if not state_err:
            for key in ("fullTurnText", "replyText", "lastAssistantText"):
                reply = str(state.get(key) or "")
                if reply:
                    blobs.append(reply)

        best = ""
        for blob in blobs:
            found = find_resume_json_string(blob)
            if len(found) > len(best):
                best = found
        return best, ""

    def send_prompt(self, prompt: str) -> str:
        """Paste ``prompt`` into the composer and send it. Returns an error message or ''."""
        return self._send_prompt(prompt)

    def _send_prompt(self, prompt: str) -> str:
        ready = self._wait_for_composer(timeout_sec=45)
        if ready:
            return ready
        before_text, _ = self._extract_json_from_page()
        self._baseline_reply_len = len(before_text or "")
        self._reply_text_before = before_text or ""
        label = self.label

        self._note(f"Pasting prompt into {label}…")
        expected = len(prompt)
        min_ok = max(min(400, expected), int(expected * 0.45))
        # ChatGPT ignores scripted typing and leaves Send disabled. A real paste, with the chat page on
        # screen, is what turns the button on. Scripted text also leaves the box in a state where a later
        # click does nothing, so it is not used when the embedded page is here to take the paste.
        if self.site.key == "chatgpt" and self._chat_view() is not None:
            self._reveal_chat()
            before, before_err = self._run_page_state()
            if before_err:
                return before_err
            self._assistant_before = int(before.get("assistantCount") or 0)
            blocked = self._page_block(before)
            if blocked:
                return blocked
            wake_err = self._wake_send_button(prompt)
            if wake_err:
                return wake_err
        else:
            encoded = json.dumps(prompt)
            script = self.site.insert_prompt_js.replace("%PROMPT%", encoded)
            raw, err = self._run_js(script, timeout_ms=max(60000, min(180000, len(prompt) // 20)))
            if err:
                return err
            try:
                data = json.loads(raw or "{}")
            except json.JSONDecodeError:
                return f"{label} did not accept the prompt."
            if not data.get("ok"):
                return str(data.get("error") or f"Failed to paste prompt into {label}.")
            pasted_len = int(data.get("length") or 0)
            if pasted_len < min_ok and self.site.key != "chatgpt":
                return f"Only {pasted_len:,} of ~{expected:,} characters pasted into {label}. "
            self._pump_events(800)

            before, before_err = self._run_page_state()
            if before_err:
                return before_err
            self._assistant_before = int(before.get("assistantCount") or 0)
            blocked = self._page_block(before)
            if blocked:
                return blocked
            if self.site.key == "chatgpt" and (not before.get("canSend") or pasted_len < min_ok):
                wake_err = self._wake_send_button(prompt)
                if wake_err:
                    if pasted_len < min_ok:
                        return (f"Only {pasted_len:,} of ~{expected:,} characters pasted into {label}. "
                                f"{wake_err}")
                    return wake_err

        self._note(f"Sending message to {label}…")
        clicks = 0
        cleared = False
        waited_out_reload = False
        for _ in range(8):
            state, state_err = self._run_page_state()
            if state_err:
                if self._navigation_error(state_err) and not waited_out_reload:
                    waited_out_reload = True
                    ready = self._wait_for_composer(timeout_sec=30)
                    if ready:
                        return ready
                    continue  # the page came back: send into it, do not load it again
                return state_err
            blocked = self._page_block(state)
            if blocked:
                return blocked
            if self._reply_has_started(state, min_ok):
                self.last_block = ""
                self._note(f"{label} is generating a response.")
                return ""
            threshold = max(20, min_ok // 2)
            composer_full = self._composer_length(state) >= threshold
            if composer_full and clicks < 3:
                if not state.get("canSend"):
                    state, wait_err, _ok = self._wait_send_enabled(30, 500)
                    if wait_err:
                        return wait_err
                click_err = self._click_send()
                if click_err:
                    return click_err
                clicks += 1
                self._note(f"Sending message to {label} ({clicks}/3)…")
            elif not composer_full:
                cleared = True
                if clicks:
                    # Send took (the box emptied after our click). A new chat first moves to its own address and
                    # can take a while over a slow connection before the reply shows; wait for it, do not give up.
                    if self._wait_for_reply_start(min_ok, timeout_sec=60):
                        self.last_block = ""
                        self._note(f"{label} is generating a response.")
                        return ""
                    break
            self._pump_events(500)
        if cleared:
            return f"{label} cleared the composer but no reply started. Nothing was taken from the page."
        return f"{label} did not start a reply after Send. Nothing was taken from the page."

    def _wait_for_reply_start(self, min_ok: int, timeout_sec: float) -> bool:
        """True once the reply shows (generating, or a new assistant message) within ``timeout_sec``."""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            self._pump_events(700)
            state, err = self._run_page_state()
            if err:
                if self._navigation_error(err):
                    continue  # the page is moving to the new conversation's address
                return False
            if self._page_block(state):
                return False
            if state.get("isGenerating") or int(state.get("assistantCount") or 0) > self._assistant_before:
                return True
        return False

    def _page_block(self, state: dict[str, Any]) -> str:
        if str(state.get("blocked") or "") in PAGE_BLOCKS:
            return self._sign_in_message(state)
        return ""

    @staticmethod
    def _box_holds_paste(state: dict[str, Any]) -> bool:
        """The message box has the pasted prompt in it: a "Pasted text" card, or real text. An empty box reads as a
        single blank line (length 1), which is not a paste."""
        return int(state.get("attachments") or 0) > 0 or int(state.get("composerLength") or 0) >= 5

    @staticmethod
    def _composer_length(state: dict[str, Any]) -> int:
        """What the message box holds. ChatGPT turns a long paste into a "Pasted text" card and leaves the box's own
        text empty; that is a full box (the card is sent with the message), not a cleared one."""
        if int(state.get("attachments") or 0) > 0:
            return 10**9
        return int(state.get("composerLength") or 0)

    def _reply_has_started(self, state: dict[str, Any], min_ok: int) -> bool:
        """True once Send took: the stop control is up, or the composer cleared and a new reply is on screen."""
        if state.get("isGenerating"):
            return True
        threshold = max(20, min_ok // 2)
        cleared = self._composer_length(state) < threshold
        grew = int(state.get("assistantCount") or 0) > self._assistant_before
        return cleared and grew

    def _chat_view(self):
        """The embedded browser showing this page, when the chat tab owns the service."""
        browser = self._view if self._view is not None else getattr(self.parent(), "browser", None)
        if browser is None:
            return None
        try:
            same = browser.page() is self._page
        except RuntimeError:
            return None
        return browser if same else None

    def _reveal_chat(self) -> None:
        """Show the chat page. A hidden page cannot take a real paste, and Send stays disabled."""
        view = self._chat_view()
        if view is None:
            return
        window = view.window()
        navigate = getattr(window, "_navigate", None)
        if callable(navigate):
            navigate("claude")
        host = self.parent()
        if host is not None:
            host.show()
        view.show()
        window.show()
        window.raise_()
        window.activateWindow()
        view.setFocus()
        self._pump_events(200)

    def _wake_send_button(self, prompt: str) -> str:
        """Turn Send on by pasting with a real keystroke. Returns an error, or '' once the button is enabled.

        One page at a time: the clipboard is shared (see ``paste_in_progress``)."""
        owner = ChatService._paste_owner
        if owner is not None and owner is not self:
            return f"Another chat page is pasting right now; {self.label} must wait for the clipboard."
        ChatService._paste_owner = self
        try:
            return self._paste_prompt(prompt)
        finally:
            ChatService._paste_owner = owner

    def _paste_prompt(self, prompt: str) -> str:
        self._reveal_chat()
        view = self._chat_view()
        if view is None or not view.isVisible():
            return (f"{self.label} left Send disabled. The prompt is in the box, "
                    "but the page did not accept it as typing.")
        from PySide6.QtGui import QGuiApplication

        clipboard = QGuiApplication.clipboard()
        previous = clipboard.text()
        seen: dict[str, Any] = {}
        try:
            # Several rounds, a few seconds apart: the page is sometimes not ready to take text for a moment (still
            # settling after it opened, or another program took the clipboard in between).
            for attempt in range(3):
                if attempt:
                    self._pump_events(4000)
                state, err = self._run_page_state()
                if err:
                    return err
                # A paste that already landed (the text, or the "Pasted text" card ChatGPT makes of a long one) is never
                # pasted again: on a slow machine the card shows up many seconds late, and a second paste sends the whole
                # prompt twice in one message.
                holds = self._box_holds_paste(state)
                if holds and attempt == 0:
                    # nothing of ours is in the box yet, so this is a draft left over from earlier: clear it
                    self._note(f"Clearing an old draft from {self.label}'s message box.")
                    self._clear_composer()
                    self._pump_events(800)
                    state, err = self._run_page_state()
                    if err:
                        return err
                    holds = self._box_holds_paste(state)
                if not holds:
                    if clipboard.text() != prompt:
                        clipboard.setText(prompt)
                    # First the browser's own Paste command into the focused message box. It arrives as a real paste
                    # and needs no keyboard focus on the desktop, so it also works when the app is behind other
                    # windows or nobody is signed in to the screen (an unattended VPS).
                    if attempt == 0 and self._composer_has_focus():
                        self._page.triggerAction(QWebEnginePage.WebAction.PasteAndMatchStyle if self.plain_paste
                                                 else QWebEnginePage.WebAction.Paste)
                    else:
                        self._press_paste(view)
                # Leave the prompt on the clipboard until ChatGPT has taken the paste.
                state, err, ok = self._wait_send_enabled(45 if holds else 25, 300)
                if err:
                    return err
                seen = state
                if ok:
                    self._note(f"Prompt pasted into {self.label}.")
                    return ""
                if self._box_holds_paste(state):
                    break  # the paste took but Send never turned on: stop, do not paste it again
        finally:
            clipboard.setText(previous)
        detail = (f" (box holds {int(seen.get('composerLength') or 0):,} of {len(prompt):,} characters"
                  f"{', a reply is still being written' if seen.get('isGenerating') else ''})")
        return (f"{self.label} left Send disabled. The prompt is in the box, "
                f"but the page did not accept it as typing.{detail}")

    def _wait_send_enabled(self, seconds: float, pump_ms: int) -> tuple[dict[str, Any], str, bool]:
        """Poll until Send is enabled. A long paste becomes a "Pasted text" card first and Send follows a moment
        later, so once the card is there the wait is longer. Returns (last state, error, enabled)."""
        deadline = time.monotonic() + seconds
        extended = False
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            if pump_ms:
                self._pump_events(pump_ms)
            state, err = self._run_page_state()
            if err:
                return state, err, False
            if state.get("canSend"):
                return state, "", True
            if int(state.get("attachments") or 0) > 0 and not extended:
                extended = True
                deadline = max(deadline, time.monotonic() + 30)
        return state, "", False

    def _composer_has_focus(self) -> bool:
        """Focus the composer and select its own text. Never selects the page, which is what reloads ChatGPT."""
        raw, err = self._run_js(
            "(() => { const c = document.querySelector('#prompt-textarea, textarea, "
            "div.ProseMirror[contenteditable=\"true\"], div[contenteditable=\"true\"]'); "
            "if (!c) return 'missing'; c.focus(); "
            "if (c.select) c.select(); else { const range = document.createRange(); "
            "range.selectNodeContents(c); const sel = window.getSelection(); "
            "if (!sel) return 'missing'; sel.removeAllRanges(); sel.addRange(range); } "
            "const active = document.activeElement; "
            "return (active === c || (c.contains && c.contains(active))) ? 'ready' : 'unfocused'; })()",
            timeout_ms=10000,
        )
        return not err and raw == "ready"

    def _click_composer(self, view) -> bool:
        """A real click in the message box, so the following keys go there and not to the whole page."""
        raw, err = self._run_js(
            "(() => { const c = document.querySelector('#prompt-textarea, textarea, "
            "div.ProseMirror[contenteditable=\"true\"], div[contenteditable=\"true\"]'); "
            "if (!c) return ''; const r = c.getBoundingClientRect(); "
            "if (r.width < 4 || r.height < 4) return ''; "
            "return JSON.stringify({x: r.left + r.width / 2, y: r.top + Math.min(r.height / 2, 24)}); })()",
            timeout_ms=10000,
        )
        if err or not raw:
            return False
        try:
            pos = json.loads(raw)
        except json.JSONDecodeError:
            return False
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        target = view.focusProxy() or view
        point = QPoint(max(1, int(pos["x"])), max(1, int(pos["y"])))
        if point.x() >= target.width() or point.y() >= target.height():
            return self._composer_has_focus()
        try:
            QTest.mouseClick(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        except RuntimeError:
            return False
        self._pump_events(80)
        return self._composer_has_focus()

    def _press_paste(self, view) -> None:
        """Ctrl+A, Ctrl+V into the composer, so ChatGPT treats the prompt as typing."""
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        if not view.isVisible():
            return  # a hidden page must not receive keys; those keys would hit whatever is in front
        window = view.window()
        window.activateWindow()
        view.setFocus(Qt.FocusReason.MouseFocusReason)
        target = view.focusProxy() or view
        target.setFocus(Qt.FocusReason.MouseFocusReason)
        if not self._click_composer(view) and not self._composer_has_focus():
            return  # the message box is not focused: Ctrl+A would select the page and ChatGPT would reload
        self._pump_events(80)
        try:
            QTest.keyClick(target, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            self._pump_events(40)
            QTest.keyClick(target, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier
                           | (Qt.KeyboardModifier.ShiftModifier if self.plain_paste else Qt.KeyboardModifier.NoModifier))
        except RuntimeError:
            pass
        import sys
        if sys.platform == "win32":
            self._win_chord(target, 0x41)  # A, inside the focused message box
            self._pump_events(40)
            self._win_chord(target, 0x56, shift=self.plain_paste)  # V (Shift+V: as plain text)

    def _win_chord(self, widget, vk: int, *, shift: bool = False) -> None:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            hwnd = int(widget.window().winId())
        except (OverflowError, OSError, ValueError):
            return
        if not self._force_foreground(hwnd):
            return  # the keys would land in another window
        user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
        if shift:
            user32.keybd_event(0x10, 0, 0, 0)  # Shift down
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
        if shift:
            user32.keybd_event(0x10, 0, 2, 0)
        user32.keybd_event(0x11, 0, 2, 0)

    @staticmethod
    def _force_foreground(hwnd: int) -> bool:
        """Bring this app's window forward so the paste lands in ChatGPT."""
        import ctypes

        user32 = ctypes.windll.user32
        if user32.GetForegroundWindow() == hwnd:
            return True
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # restore
        else:
            user32.ShowWindow(hwnd, 5)  # show
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        # Attaching this thread to the foreground window's thread can lock the whole app when that
        # window's thread is the bot browser and the browser has stopped answering.
        return user32.GetForegroundWindow() == hwnd

    def _click_send(self) -> str:
        raw, err = self._run_js(self.site.click_send_js, timeout_ms=15000)
        if err:
            return err
        try:
            sent = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return f"Could not send message to {self.label}."
        if not sent.get("ok"):
            return str(sent.get("error") or f"Failed to send prompt to {self.label}.")
        return ""

    def _wait_for_new_assistant_reply(self, timeout_sec: int = CHAT_REPLY_WAIT_SEC) -> tuple[str, str]:
        label = self.label
        deadline = time.monotonic() + timeout_sec
        stable_hash = ""
        stable_since = 0.0
        last_log = 0.0
        text_before = self._reply_text_before or ""
        stable_plain_len = 0
        stable_plain_since = 0.0
        idle_streak = 0

        while time.monotonic() < deadline:
            if self._manual_reply:
                self._note(f"Using JSON extracted from the {label} tab.")
                return self._manual_reply, ""

            page, _ = self._run_page_state()
            blocked = self._page_block(page)
            if blocked:
                return "", blocked
            if page.get("isGenerating"):
                idle_streak = 0
                stable_hash = ""
            else:
                idle_streak += 1

            text, err = self._extract_json_from_page()
            if err:
                return "", err

            if time.monotonic() - last_log > 20:
                state, _ = self._run_page_state()
                longest = int(state.get("longestAssistant") or 0)
                generating = bool(state.get("isGenerating"))
                if text and len(text) >= MIN_RESUME_JSON_SNIPPET_CHARS:
                    self._note(f"Scanning {label} assistant reply… ({len(text):,} chars of resume JSON)")
                elif longest > 0:
                    hint = "still streaming" if generating else "parsing reply"
                    extra = ""
                    if longest >= 1500 and not text:
                        reply = str(state.get("replyText") or "")
                        extra = find_resume_json_string(reply)
                        if not extra:
                            extra = " (could not parse JSON yet)"
                        else:
                            extra = f" (parsed {len(extra):,} chars JSON)"
                    self._note(f"Scanning {label} assistant reply… ({longest:,} chars visible, {hint}){extra}")
                else:
                    self._note(f"Scanning {label} page… (no assistant reply visible yet)")
                last_log = time.monotonic()

            if not text or len(text) < MIN_RESUME_JSON_SNIPPET_CHARS:
                state, _ = self._run_page_state()
                plain_len = int(state.get("longestAssistant") or 0)
                if plain_len >= 1500 and not bool(state.get("isGenerating")):
                    reply = str(state.get("replyText") or state.get("fullTurnText") or "")
                    forced = find_resume_json_string(reply)
                    if forced:
                        text = forced
                if plain_len == stable_plain_len and plain_len >= 1500:
                    if stable_plain_since == 0.0:
                        stable_plain_since = time.monotonic()
                    elif time.monotonic() - stable_plain_since >= 2.0 and not text:
                        reply = str(state.get("replyText") or state.get("fullTurnText") or "")
                        text = find_resume_json_string(reply)
                else:
                    stable_plain_len = plain_len
                    stable_plain_since = 0.0
            if not text or len(text) < MIN_RESUME_JSON_SNIPPET_CHARS:
                self._pump_events(600)
                continue

            if text_before and text == text_before:
                self._pump_events(600)
                continue
            if text_before and text.startswith(text_before) and len(text) - len(text_before) < 200:
                self._pump_events(600)
                continue

            try:
                extract_usable_resume(text)
            except JsonExtractError:
                self._pump_events(600)
                continue

            if page.get("isGenerating") or idle_streak < 2:
                self._pump_events(600)
                continue

            digest = str(len(text)) + ":" + text[:120] + ":" + text[-120:]
            if digest == stable_hash:
                if time.monotonic() - stable_since >= 1.5:
                    self._note("Resume JSON found — continuing export.")
                    return text, ""
            else:
                stable_hash = digest
                stable_since = time.monotonic()

            self._pump_events(600)

        minutes = max(1, timeout_sec // 60)
        return "", (
            f"Timed out after {minutes} minutes waiting for {label}. Open the {label} tab, then click "
            "'Extract' in the inspector (or copy JSON and Import JSON)."
        )

    def _run_js(self, script: str, timeout_ms: int = 12000) -> tuple[str, str]:
        loop = QEventLoop()
        result: list[str] = []

        def finished(value: object) -> None:
            result.append("" if value is None else str(value))
            loop.quit()

        self._page.runJavaScript(script, finished)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        loop.exec()
        timer.stop()
        if not result:
            return "", f"{self.label} page did not respond (timeout or navigation in progress)."
        return result[0], ""

    def _pump_events(self, ms: int) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
