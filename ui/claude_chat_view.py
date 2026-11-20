"""Embedded web chat (Claude or ChatGPT) with a persistent session."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFrame, QHBoxLayout, QSplitter, QVBoxLayout, QWidget

from core.chat_browser_profile import cancel_wipe, chat_web_profile, request_wipe, sign_out
from core.chat_sites import CLAUDE, SITES
from core.proc_info import private_mb
from ui import copy as C
from ui.chat_service import ChatService
from ui.dialogs import ask_yes_no, show_info
from ui.widgets import ghost_button, muted_label, page_title, primary_button


class _KeepAliveView(QWebEngineView):
    """The chat runs while another page of the app is on screen. A page that is hidden can be throttled or frozen, and a
    chat that streams its reply into a hidden page may show nothing until it is looked at, so the page is kept
    active and 'visible' to the browser engine whenever its widget is hidden."""

    def _keep_alive(self) -> None:
        page = self.page()
        if page is not None:
            page.setLifecycleState(QWebEnginePage.LifecycleState.Active)
            page.setVisible(True)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._keep_alive()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._keep_alive()


# A chat page grows for as long as it stays open (ChatGPT's page reached 3-3.6 GB on a 6 GB server and the app was
# closed for lack of memory). An idle page above this size is replaced by a fresh one, which frees all of it.
RECYCLE_ABOVE_MB = 1500
MEMORY_CHECK_MS = 60_000


QUESTIONS_ACCOUNT = "questions"  # the saved profile the application-questions page signs in with


class ClaudeChatView(QWidget):
    """Despite the historical name this hosts whichever chat site the settings choose (``site_key``)."""

    session_cleared = Signal()

    def __init__(self, parent=None, site_key: str = CLAUDE, proxy_credentials=None, questions_pane: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("ContentPane")
        self.site = SITES.get(site_key, SITES[CLAUDE])
        self._proxy_credentials = proxy_credentials  # callable returning (user, password), or None
        label = self.site.label

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(6)
        title_box.addWidget(page_title(label))
        title_box.addWidget(muted_label(f"Sign in once — the queue uses this session to generate tailored JSON "
                                        f"automatically ({label})."))
        header.addLayout(title_box, stretch=1)
        self.status_label = muted_label(C.CLAUDE_STATUS_LOADING)
        header.addWidget(self.status_label, alignment=Qt.AlignmentFlag.AlignTop)
        root.addLayout(header)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.new_chat_btn = ghost_button(C.CLAUDE_NEW_CHAT)
        self.reload_btn = ghost_button(C.CLAUDE_RELOAD)
        self.clear_session_btn = ghost_button("Sign out: resumes account")
        self.clear_questions_btn = ghost_button("Sign out: questions account")
        toolbar.addWidget(self.new_chat_btn)
        toolbar.addWidget(self.reload_btn)
        toolbar.addWidget(self.clear_session_btn)
        toolbar.addWidget(self.clear_questions_btn)
        toolbar.addStretch(1)
        self.open_home_btn = primary_button(f"Open {self.site.home.split('//')[1].rstrip('/')}")
        toolbar.addWidget(self.open_home_btn)
        root.addLayout(toolbar)

        frame = QFrame()
        frame.setObjectName("SurfaceCard")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)

        # Two pages, two accounts: the left writes resumes, the right answers the questions of application forms, each
        # signed in to its own account (QUESTIONS_ACCOUNT). Each does its own work, and one account's usage limit,
        # human check or sign-out never stops the other.
        self.browser = _KeepAliveView()
        page = self._new_page(self.browser)
        self.browser.setPage(page)
        self.ask_browser = _KeepAliveView()
        ask_page = self._new_page(self.ask_browser, QUESTIONS_ACCOUNT)
        self.ask_browser.setPage(ask_page)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        # The resume page keeps the wide layout the chat site shows at 768 px and more (that is what the page reader was
        # written for); the questions page may be narrower.
        self.resume_caption = muted_label("Resumes")
        self.questions_caption = muted_label("Application questions (second account)")
        self.splitter.addWidget(self._pane(self.resume_caption, self.browser, 780))
        self.splitter.addWidget(self._pane(self.questions_caption, self.ask_browser, 420))
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([900, 600])
        # When the application questions are asked in the bot browser (Settings: question_chat = chrome), the questions
        # pane is not shown and never loads the chat: it would only hold memory.
        self.questions_pane = questions_pane
        if not questions_pane:
            self.splitter.widget(1).hide()
        fl.addWidget(self.splitter)
        root.addWidget(frame, stretch=1)

        hint = muted_label(C.CLAUDE_HINT)
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.service = ChatService(page, self, site=self.site, view=self.browser)
        self.service.activity.connect(self._on_activity)
        self.ask_service = self._question_service_for(ask_page)

        self.new_chat_btn.clicked.connect(self._open_new_chat)
        self.reload_btn.clicked.connect(self.browser.reload)
        self.clear_session_btn.clicked.connect(lambda: self._clear_session("resumes"))
        self.clear_questions_btn.clicked.connect(lambda: self._clear_session("questions"))
        self.ask_browser.loadFinished.connect(lambda _ok: self._show_pane_status("questions"))
        self._memory_timer = QTimer(self)
        self._memory_timer.setInterval(MEMORY_CHECK_MS)
        self._memory_timer.timeout.connect(self.recycle_large_pages)
        self._memory_timer.start()
        self.open_home_btn.clicked.connect(self._open_home)
        self.browser.loadFinished.connect(self._on_load_finished)

        self.browser.load(QUrl(self.site.home))
        if questions_pane:
            self.ask_browser.load(QUrl(self.site.home))
        else:
            self.clear_questions_btn.hide()

    @staticmethod
    def _pane(caption: QWidget, view: QWidget, min_width: int) -> QWidget:
        box = QWidget()
        box.setMinimumWidth(min_width)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        caption.setContentsMargins(10, 6, 10, 6)
        lay.addWidget(caption)
        lay.addWidget(view, stretch=1)
        return box

    def _new_page(self, parent: QWidget, account: str = "") -> QWebEnginePage:
        # Each account has its own saved profile (cookies and storage): its own sign-in.
        page = QWebEnginePage(chat_web_profile(self.site.key, account), parent)
        page.proxyAuthenticationRequired.connect(self._on_proxy_auth)
        return page

    def _question_service_for(self, page: QWebEnginePage) -> ChatService:
        service = ChatService(page, self, site=self.site, view=self.ask_browser)
        service.plain_paste = True  # questions are pasted as plain text, so they never become a file
        service.label_suffix = " (questions account)"
        return service

    def _show_pane_status(self, name: str) -> None:
        """The pane's caption says whether its account is ready, signed out, or held by a check or the usage limit."""
        service, caption, title = ((self.service, self.resume_caption, "Resumes") if name == "resumes"
                                   else (self.ask_service, self.questions_caption, "Application questions (second account)"))
        state, err = service.run_page_state()
        if err:
            return
        blocked = str(state.get("blocked") or "")
        if state.get("loggedIn"):
            # signed in again after a sign-out: the files now hold this sign-in, so they are not removed at the next start
            cancel_wipe(self.site.key, "" if name == "resumes" else QUESTIONS_ACCOUNT)
        status = ("human check: complete it here" if blocked == "challenge" else "usage limit" if blocked == "limit"
                  else "signed in" if state.get("loggedIn") else "sign in here")
        caption.setText(f"{title}: {status}")

    def page_memory_mb(self) -> dict[str, int]:
        """Memory held by each chat page's browser process, in MB (0 when it cannot be read)."""
        return {"resumes": private_mb(self.browser.page().renderProcessPid()),
                "questions": private_mb(self.ask_browser.page().renderProcessPid())}

    def recycle_large_pages(self) -> list[str]:
        """Replace each idle page that has grown past RECYCLE_ABOVE_MB with a fresh one. Never while it is writing a
        resume or answering a question, nor while either page is pasting. Returns the panes that were replaced."""
        if ChatService.paste_in_progress():
            return []
        done = []
        sizes = self.page_memory_mb()
        for name, view, attr in (("resumes", self.browser, "service"), ("questions", self.ask_browser, "ask_service")):
            service: ChatService = getattr(self, attr)
            if sizes[name] < RECYCLE_ABOVE_MB or service.busy:
                continue
            old = view.page()
            page = self._new_page(view, QUESTIONS_ACCOUNT if name == "questions" else "")
            view.setPage(page)
            service.replace_page(page)
            page.load(QUrl(self.site.home))
            keep_alive = getattr(view, "_keep_alive", None)
            if keep_alive:
                keep_alive()  # a hidden page must not be throttled (see _KeepAliveView)
            old.deleteLater()  # its browser process ends with it, and its memory with it
            done.append(name)
            self._on_activity(f"The {name} page was reloaded to free memory ({sizes[name]:,} MB).")
        return done

    def _on_proxy_auth(self, _url, authenticator, _proxy_host) -> None:
        user, password = self._proxy_credentials() if self._proxy_credentials else ("", "")
        if user:
            authenticator.setUser(user)
            authenticator.setPassword(password)

    def page(self) -> QWebEnginePage:
        return self.browser.page()

    def chat_service(self) -> ChatService:
        """The page that writes resumes."""
        return self.service

    def question_service(self) -> ChatService:
        """The page that answers application-form questions."""
        return self.ask_service

    def _open_new_chat(self) -> None:
        self.browser.load(QUrl(self.site.new_chat))

    def _open_home(self) -> None:
        self.browser.load(QUrl(self.site.home))

    def _clear_session(self, which: str = "resumes") -> None:
        """Sign one account out (its saved cookies and storage are removed); the other stays signed in."""
        label = self.site.label
        whose = "resumes account" if which == "resumes" else "questions account"
        if not ask_yes_no(self, f"Sign out of the {label} {whose}",
                          f"This removes the saved sign-in of the {label} {whose} in Auto Apply Studio. "
                          "You will need to sign in again in that pane. The other account stays signed in."):
            return
        # The page stays open: its sign-in cookies are cleared now and its saved files at the next start. (Deleting the
        # profile under an open page crashed the app.)
        view, account = (self.browser, "") if which == "resumes" else (self.ask_browser, QUESTIONS_ACCOUNT)
        sign_out(view.page().profile())
        request_wipe(self.site.key, account)
        view.load(QUrl(self.site.home))
        if which == "resumes":
            self.status_label.setText(C.CLAUDE_STATUS_SIGNED_OUT)
        self.session_cleared.emit()
        show_info(self, f"Sign out of the {label} {whose}", f"Signed out of the {label} {whose}.")

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            self._show_pane_status("resumes")
        if not ok:
            self.status_label.setText(C.CLAUDE_STATUS_ERROR)
            return
        state, err = self.service.run_page_state()
        if err:
            self.status_label.setText(C.CLAUDE_STATUS_LOADING)
            return
        if state.get("loggedIn"):
            self.status_label.setText(C.CLAUDE_STATUS_READY)
        elif state.get("blocked") == "challenge":
            self.status_label.setText(f"{self.site.label} is asking for a human check — complete it here")
        else:
            self.status_label.setText(f"Sign in to {self.site.label} to enable automatic generation")

    def refresh_status(self) -> None:
        self._on_load_finished(True)

    def _on_activity(self, message: str) -> None:
        if message:
            self.status_label.setText(message)
