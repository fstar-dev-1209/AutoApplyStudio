"""Embedded web chat (Claude or ChatGPT) with a persistent session."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from core.chat_browser_profile import chat_web_profile, clear_chat_browser_data
from core.chat_sites import CLAUDE, SITES
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


class ClaudeChatView(QWidget):
    """Despite the historical name this hosts whichever chat site the settings choose (``site_key``)."""

    session_cleared = Signal()

    def __init__(self, parent=None, site_key: str = CLAUDE, proxy_credentials=None) -> None:
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
        self.clear_session_btn = ghost_button(C.CLAUDE_CLEAR_SESSION)
        toolbar.addWidget(self.new_chat_btn)
        toolbar.addWidget(self.reload_btn)
        toolbar.addWidget(self.clear_session_btn)
        toolbar.addStretch(1)
        self.open_home_btn = primary_button(f"Open {self.site.home.split('//')[1].rstrip('/')}")
        toolbar.addWidget(self.open_home_btn)
        root.addLayout(toolbar)

        frame = QFrame()
        frame.setObjectName("SurfaceCard")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)

        self.browser = _KeepAliveView()
        page = self._new_page()
        self.browser.setPage(page)
        fl.addWidget(self.browser)
        root.addWidget(frame, stretch=1)

        hint = muted_label(C.CLAUDE_HINT)
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.service = ChatService(page, self, site=self.site)
        self.service.activity.connect(self._on_activity)

        self.new_chat_btn.clicked.connect(self._open_new_chat)
        self.reload_btn.clicked.connect(self.browser.reload)
        self.clear_session_btn.clicked.connect(self._clear_session)
        self.open_home_btn.clicked.connect(self._open_home)
        self.browser.loadFinished.connect(self._on_load_finished)

        self.browser.load(QUrl(self.site.home))

    def _new_page(self) -> QWebEnginePage:
        page = QWebEnginePage(chat_web_profile(self.site.key), self.browser)
        page.proxyAuthenticationRequired.connect(self._on_proxy_auth)
        return page

    def _on_proxy_auth(self, _url, authenticator, _proxy_host) -> None:
        user, password = self._proxy_credentials() if self._proxy_credentials else ("", "")
        if user:
            authenticator.setUser(user)
            authenticator.setPassword(password)

    def page(self) -> QWebEnginePage:
        return self.browser.page()

    def chat_service(self) -> ChatService:
        return self.service

    def _open_new_chat(self) -> None:
        self.browser.load(QUrl(self.site.new_chat))

    def _open_home(self) -> None:
        self.browser.load(QUrl(self.site.home))

    def _clear_session(self) -> None:
        label = self.site.label
        if not ask_yes_no(self, f"Clear {label} session",
                          f"This removes saved cookies and signs you out of {label} in Auto Apply Studio. "
                          "You will need to sign in again."):
            return
        clear_chat_browser_data(self.site.key)
        page = self._new_page()
        self.browser.setPage(page)
        self.service = ChatService(page, self, site=self.site)
        self.service.activity.connect(self._on_activity)
        self.browser.load(QUrl(self.site.home))
        self.status_label.setText(C.CLAUDE_STATUS_SIGNED_OUT)
        self.session_cleared.emit()
        show_info(self, f"Clear {label} session", f"{label} session cleared.")

    def _on_load_finished(self, ok: bool) -> None:
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
