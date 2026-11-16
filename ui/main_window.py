"""Auto Apply Studio main window shell."""

from __future__ import annotations

import sys

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QMetaObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import ats
from core.apply_runner import AttemptStore
from core.applications import ApplicationError, ApplicationService
from core.bot_services import BotServices
from core.chat_sites import site_for
from core.browser_intake import INTAKE_PORT, IntakeServer, IntakeService, get_token
from core.discovery import DiscoveryService
from core.claude_client import ChatPageBlocked, ClaudeError
from core.job_export import export_jobs_csv, sync_jobs_to_disk
from core.models import JobItem, JobStatus, QueueStore
from core.paths import config_dir
from core.profile_apply import apply_settings_profile
from core.queue_engine import QueueEngine, WorkerState
from core.salary import parse_salary
from core.scraper import detect_source
from core.settings_store import MODE_CONTROLLER, MODE_VIEWER, ProfileSettings, SettingsStore
from core.viewer_client import ControllerClient, RemoteApplicationService, RemoteQueue, ViewerSync
from core.viewer_files import FileMaterializer, make_path_mapper
from ui import copy as C
from ui import icons, system, theme
from ui.analytics_view import AnalyticsView
from ui.applications_view import ApplicationsPage
from ui.autoapply_view import AutoApplyView
from ui.chat_ask import AskResult, ChatAsker
from ui.chat_service import CLAUDE_REPLY_WAIT_SEC, ChatService
from ui.claude_chat_view import ClaudeChatView
from ui.page_renderer import PageRenderer
from ui.claude_wait_coordinator import ClaudeGenResult, ClaudeWaitCoordinator
from ui.dialogs import ask_yes_no, show_info
from ui.files_view import GeneratedFilesView
from ui.inspector import JobInspector
from ui.titlebar import TitleStrip, hit_test
from ui.manual_json_view import ManualJsonView
from ui.queue_view import QueueView
from ui.settings_view import SettingsView
from ui.theme import apply_theme, resolve_theme
from ui.toast import Toast
from ui.viewer_mode import ON_CONTROLLER, ViewerEngine, ViewerLink
from ui.widgets import nav_button, refresh_icons, set_icon

# page order in the stacked widget
PAGES = ["queue", "applications", "analytics", "files", "json", "settings", "claude", "autoapply"]
NAV = [
    ("queue", "Job Queue", "jobs"),
    ("autoapply", "Auto Apply", "send"),
    ("applications", "Applications", "applications"),
    ("analytics", "Analytics", "analytics"),
    ("files", "Generated Files", "files"),
    ("json", "Manual JSON", "json"),
    ("settings", "Settings", "settings"),
]


class MainWindow(QMainWindow):
    # Cross-thread UI updates
    engine_event = Signal(str, dict)
    intake_request = Signal(object)  # a job from the browser extension, handled on the UI thread
    ui_call = Signal(object)  # a function to run on the UI thread, sent from any other thread

    def __init__(self, settings_store: SettingsStore | None = None, db_dir: Path | None = None) -> None:
        super().__init__()
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowTitle(C.APP_NAME)
        self.resize(1440, 900)
        self.setMinimumSize(1100, 700)

        self.settings_store = settings_store or SettingsStore()
        apply_settings_profile(self.settings_store.settings)
        cfg = db_dir or config_dir()
        # standalone: everything here. controller: the same, plus it serves a viewer. viewer: shows and manages
        # the controller's data from a read-only copy; it never generates anything.
        self.chat_label = site_for(self.settings_store.settings.chat_provider).label  # "Claude" or "ChatGPT"
        self.mode = self.settings_store.settings.app_mode
        self.is_viewer = self.mode == MODE_VIEWER
        self.is_controller = self.mode == MODE_CONTROLLER
        self.viewer: ViewerLink | None = None
        self.remote_queue: RemoteQueue | None = None
        self.materializer: FileMaterializer | None = None
        ghost_days = self.settings_store.settings.ghost_after_days
        if self.is_viewer:
            # its own file: switching back to standalone never mixes the controller's data into local data
            self.queue_store = QueueStore(cfg / "replica.db", read_only=True)
            self.queue_store.load()
            self._setup_viewer_link(cfg, ghost_days)
        else:
            self.queue_store = QueueStore(cfg / "studio.db", legacy_json=cfg / "queue.json")
            self.queue_store.load()
            self.apps = ApplicationService(self.queue_store.database, ghost_after_days=ghost_days)

        self._claude_signed_in = False
        self._claude_wait = threading.Event()
        self._claude_begin_deferred = False
        self._claude_thread_result: dict | None = None
        self._claude_wait_error = ""
        self.engine_event.connect(self._on_engine_event)
        self._build_ui()
        if self.is_viewer:
            self.page_renderer = None  # no page fetching or Claude here
            self.engine = ViewerEngine(self.viewer)
            self.queue_view.set_viewer_mode(True)
            self.inspector.viewer_mode = True
        else:
            self.page_renderer = PageRenderer(self)
            self.engine = QueueEngine(
                self.queue_store,
                self.settings_store,
                on_event=lambda name, payload: self.engine_event.emit(name, payload),
                claude_chat_generate=self._claude_chat_generate,
                render_page=self.page_renderer.fetch,
            )
        self._apply_theme()
        self._wire()
        self.bot: BotServices | None = None
        if not self.is_viewer:
            self._setup_intake()
            self._setup_bot()
        self._refresh_queue()
        if self.queue_view.table.rowCount():
            self.queue_view.select_row(0)  # open with the inspector populated
        self._refresh_claude_status()
        self._update_sidebar_status()
        self._setup_shortcuts()
        self.ui_call.connect(self._run_ui_call)
        self.apps.deliver = self._deliver_on_ui  # the apply bot marks jobs applied from its own thread
        self.apps.subscribe(self._on_apps_changed)
        # QueueStore notifies from the worker thread: hop to the UI thread through the signal
        self.queue_store.subscribe(lambda: self.engine_event.emit("store_changed", {}))
        if self.is_viewer:
            self._start_viewer_link()

        # Periodic light refresh for progress while running
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._update_sidebar_status)
        self._timer.timeout.connect(self._update_autoapply)
        self._timer.start()

    # ------------------------------------------------------------------- layout

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(10, 8, 0, 10)
        outer.setSpacing(0)

        self.sidebar = QWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(216)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(12, 16, 12, 14)
        side.setSpacing(4)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(4, 2, 0, 16)
        brand_row.setSpacing(9)
        logo = QFrame()
        logo.setObjectName("Logo")
        logo.setFixedSize(38, 38)
        ll = QVBoxLayout(logo)
        ll.setContentsMargins(0, 0, 0, 0)
        mark = QLabel()
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setPixmap(icons.icon("briefcase-check", "#FFFFFF", 20).pixmap(20, 20))
        ll.addWidget(mark)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand = QLabel(C.APP_NAME)
        brand.setObjectName("BrandTitle")
        brand_sub = QLabel("Remote job applications")
        brand_sub.setObjectName("BrandSub")
        brand_text.addWidget(brand)
        brand_sub.hide()
        brand_row.addWidget(logo)
        brand_row.addLayout(brand_text, 1)
        side.addLayout(brand_row)

        self._nav: dict[str, QPushButton] = {}
        for key, label, icon in NAV:
            btn = nav_button(label, icon)
            btn.clicked.connect(lambda _=False, k=key: self._navigate(k))
            self._nav[key] = btn
            side.addWidget(btn)
        self.nav_queue = self._nav["queue"]
        # live counts inside the nav buttons
        self.queue_badge = QLabel("")
        self.queue_badge.setObjectName("NavCount")
        lay = QHBoxLayout(self.nav_queue)
        lay.setContentsMargins(0, 0, 10, 0)
        lay.addStretch(1)
        self.queue_badge.setFixedHeight(22)
        lay.addWidget(self.queue_badge, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.apps_badge = QLabel("")
        self.apps_badge.setObjectName("NavCount")
        lay2 = QHBoxLayout(self._nav["applications"])
        lay2.setContentsMargins(0, 0, 10, 0)
        lay2.addStretch(1)
        self.apps_badge.setFixedHeight(22)
        lay2.addWidget(self.apps_badge, alignment=Qt.AlignmentFlag.AlignVCenter)
        side.addStretch(1)

        footer = QFrame()
        footer.setObjectName("SidebarFooter")
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(4, 10, 4, 4)
        fl.setSpacing(8)
        self.claude_btn = QPushButton(f"  {self.chat_label}")
        self.claude_btn.setObjectName("NavButton")
        self.claude_btn.setProperty("active", False)
        self.claude_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.claude_btn.setToolTip(f"Open the {self.chat_label} web chat session used for generation")
        set_icon(self.claude_btn, "claude", "sidebar", 18)
        self.claude_btn.clicked.connect(lambda: self._navigate("claude"))
        self._nav["claude"] = self.claude_btn
        self.claude_btn.hide()
        card = QFrame()
        card.setObjectName("StatusCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.status_dot = QLabel()
        self.status_dot.setObjectName("StatusDotOff")
        top.addWidget(self.status_dot, alignment=Qt.AlignmentFlag.AlignVCenter)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.api_title = QLabel("Claude API")
        self.api_title.setObjectName("SidebarValue")
        self.api_state = QLabel("")
        self.api_state.setObjectName("Connected")
        titles.addWidget(self.api_title)
        titles.addWidget(self.api_state)
        top.addLayout(titles, 1)
        cl.addLayout(top)
        cl.addSpacing(8)
        out_row = QHBoxLayout()
        out_row.setSpacing(8)
        folder_ic = QLabel()
        folder_ic.setPixmap(icons.icon("folder-open", theme.current().sidebar_muted, 16).pixmap(16, 16))
        out_row.addWidget(folder_ic, alignment=Qt.AlignmentFlag.AlignTop)
        out_col = QVBoxLayout()
        out_col.setSpacing(0)
        out_lab = QLabel("Output folder")
        out_lab.setObjectName("SidebarMeta")
        self.output_status = QLabel("")
        self.output_status.setObjectName("SidebarValue")
        self.output_status.setWordWrap(True)
        out_col.addWidget(out_lab)
        out_col.addWidget(self.output_status)
        out_row.addLayout(out_col, 1)
        cl.addLayout(out_row)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        if self.is_viewer:
            card.setToolTip("Connection to the controller")
            card.mousePressEvent = lambda _e: self._navigate("settings")
        else:
            card.setToolTip(f"Open the {self.chat_label} web chat session used for generation")
            card.mousePressEvent = lambda _e: self._navigate("claude")
        fl.addWidget(card)
        side.addWidget(footer)
        outer.addWidget(self.sidebar)

        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(0)
        self.title_strip = TitleStrip(self)
        right_l.addWidget(self.title_strip)
        self.stack = QStackedWidget()
        right_l.addWidget(self.stack, stretch=1)
        outer.addWidget(right, stretch=1)

        # 0: queue (table + inspector)
        queue_page = QWidget()
        queue_page.setObjectName("Workspace")
        qp = QHBoxLayout(queue_page)
        qp.setContentsMargins(0, 0, 0, 0)
        qp.setSpacing(0)
        self.queue_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.queue_splitter.setHandleWidth(1)
        self.queue_view = QueueView()
        self.inspector = JobInspector(
            settings_store=self.settings_store,
            events_provider=self.queue_store.list_events,
        )
        side_col = QWidget()
        side_l = QVBoxLayout(side_col)
        side_l.setContentsMargins(0, 0, 0, 0)
        side_l.setSpacing(0)
        side_l.addWidget(self.queue_view.side_header)
        side_l.addWidget(self.inspector, stretch=1)
        side_col.setMinimumWidth(340)
        self.queue_splitter.addWidget(self.queue_view)
        self.queue_splitter.addWidget(side_col)
        self.queue_splitter.setStretchFactor(0, 7)
        self.queue_splitter.setStretchFactor(1, 3)
        self.queue_splitter.setSizes([1000, 360])
        qp.addWidget(self.queue_splitter)
        self.stack.addWidget(queue_page)

        # 1: applications
        self.apps_page = ApplicationsPage(self.apps)
        self.stack.addWidget(self.apps_page)

        # 2: analytics
        self.analytics_view = AnalyticsView(self.queue_store.database, self.apps)
        self.stack.addWidget(self.analytics_view)

        # 3: generated files
        root = self.settings_store.settings.resolved_output_root()
        self.files_view = GeneratedFilesView(root, db=self.queue_store.database)
        self.stack.addWidget(self.files_view)

        # 4: manual json
        self.manual_view = ManualJsonView(root, settings_store=self.settings_store)
        self.stack.addWidget(self.manual_view)

        # 5: settings
        self.settings_view = SettingsView(self.settings_store)
        self.stack.addWidget(self.settings_view)

        # 6: claude web chat (reached from the sidebar footer / sign-in prompts)
        if self.is_viewer:  # no Claude session in a viewer: keep the page slot so indexes stay the same
            self.claude_view = None
            self.stack.addWidget(QWidget())
        else:
            self.claude_view = ClaudeChatView(
                site_key=self.settings_store.settings.chat_provider,
                proxy_credentials=self.settings_store.proxy_credentials,
            )
            self.stack.addWidget(self.claude_view)

        # 7: auto apply (a viewer sees the controller's results but cannot drive the bot)
        self.autoapply_view = AutoApplyView(
            self.settings_store, AttemptStore(self.queue_store), DiscoveryService(self.queue_store),
            readonly=self.is_viewer)
        self.stack.addWidget(self.autoapply_view)

        self.statusBar().hide()
        self.statusBar().showMessage(C.STATUS_READY)
        self.toast = Toast(self)
        self._navigate("queue")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.title_strip.sync()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        strip = getattr(self, "title_strip", None)
        if strip is not None and event.type().name == "WindowStateChange":
            strip.sync()

    def nativeEvent(self, event_type, message):  # noqa: N802
        if sys.platform == "win32" and bytes(event_type) == b"windows_generic_MSG":
            try:
                result = hit_test(self, self.title_strip, int(message))
            except Exception:  # noqa: BLE001
                result = None
            if result is not None:
                return True, result
        return super().nativeEvent(event_type, message)

    def _tint_titlebar(self) -> None:
        """Frameless window: nothing to tint (kept so theme switching stays a single code path)."""

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        toast = getattr(self, "toast", None)
        if toast is not None and toast.isVisible():
            x = self.width() - toast.width() - 28
            y = self.height() - toast.height() - 28
            toast.move(max(16, x), max(16, y))

    def _wire(self) -> None:
        qv = self.queue_view
        qv.add_urls.connect(self._add_urls)
        qv.add_jobs.connect(self._focus_intake)
        qv.start.connect(self._start)
        qv.pause.connect(self._pause)
        qv.stop.connect(self._stop)
        qv.selection_changed.connect(self._on_select_job)
        qv.filters_changed.connect(self._refresh_queue)
        qv.sync_files.connect(self._sync_files)
        qv.export_csv_selected.connect(self._export_csv_selected)
        qv.mark_applied.connect(self._mark_applied)
        qv.retry_job.connect(lambda jid: self._retry_job(jid, "auto"))
        qv.skip_job.connect(self._skip_job)
        qv.allow_duplicate.connect(self._allow_duplicate)
        qv.remove_jobs.connect(self._remove_jobs)
        qv.show_in_applications.connect(self._show_in_applications)
        self.inspector.submit_jd.connect(self._submit_jd)
        self.inspector.retry_job.connect(self._retry_job)
        self.inspector.skip_job.connect(self._skip_job)
        self.inspector.allow_duplicate.connect(self._allow_duplicate)
        self.inspector.pause_queue.connect(self._pause)
        self.inspector.sync_files.connect(lambda jid: self._sync_files([jid]))
        self.files_view.sync_requested.connect(self._sync_files)
        self.inspector.closed.connect(lambda: self.queue_view.table.clearSelection())
        self.inspector.import_json.connect(self._import_resume_json)
        self.inspector.pull_claude_json.connect(self._pull_claude_json)
        self.inspector.copy_claude_json.connect(self._copy_claude_json)
        self.apps_page.view.open_queue_job.connect(self._show_in_queue)
        self.settings_view.saved.connect(self._on_settings_saved)
        if not self.is_viewer:
            self.claude_view.session_cleared.connect(self._refresh_claude_status)
            self.claude_view.browser.loadFinished.connect(lambda _ok: self._refresh_claude_status())
            self._claude_coordinator = ClaudeWaitCoordinator(self)
            self._claude_coordinator.finished.connect(self._on_claude_wait_finished)
            self._asker: ChatAsker | None = None
            self._ask_waiter = None

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self._focus_intake)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self._navigate("settings"))
        for i, (key, _l, _i) in enumerate(NAV, start=1):
            QShortcut(QKeySequence(f"Ctrl+{i}"), self, activated=lambda k=key: self._navigate(k))
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._escape)
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, activated=self._delete_selected)

    def _apply_theme(self) -> None:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        s = self.settings_store.settings
        colors = resolve_theme(s.theme, app)
        # Restyling the whole app and redrawing every table is slow (tens of seconds on a small server, long enough
        # for Windows to close a frozen window), so it is done only when the look really changed, and only for the
        # page on screen; the others redraw when they are opened.
        look = (colors.app_bg, colors.accent, s.density)
        changed = look != getattr(self, "_look", None)
        self._look = look
        if changed:
            if hasattr(self, "apps_page"):  # the app-wide restyle visits every widget alive; these are redrawn below
                self.apps_page.view.release_rows()
                self.files_view.release_rows()
                # Qt only queues the dropped widgets for deletion; until that runs the restyle still visits them all
                from PySide6.QtCore import QCoreApplication, QEvent

                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if app:
                apply_theme(app, colors, s.density)
            refresh_icons()
        self._tint_titlebar()
        self.queue_view.set_theme_light(colors.is_light)
        self._update_sidebar_status()
        if hasattr(self, "apps_page"):
            page = self.stack.currentWidget()
            if changed:
                if page is self.apps_page:
                    self.apps_page.refresh()
                elif page is self.files_view:
                    self.files_view.refresh()
                self.manual_view.refresh_theme()
            if page is self.analytics_view:
                self.analytics_view.refresh()
            self._refresh_queue()

    # --------------------------------------------------------------- navigation

    def _navigate(self, key: str) -> None:
        if key == "claude" and self.is_viewer:
            key = "settings"  # there is no Claude session in a viewer
        self.stack.setCurrentIndex(PAGES.index(key))
        icon_names = {k: i for k, _l, i in NAV} | {"claude": "claude"}
        for k, btn in self._nav.items():
            btn.setProperty("active", k == key)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            set_icon(btn, icon_names[k], "white" if k == key else "sidebar", 18)
        if key == "applications":
            self.apps_page.refresh()
        if key == "analytics":
            self.analytics_view.refresh()
        if key == "files":
            self.files_view.set_output_root(self.settings_store.settings.resolved_output_root())
        if key == "json":
            self.manual_view.set_output_root(self.settings_store.settings.resolved_output_root())
        if key == "claude":
            self.claude_view.refresh_status()
            self._refresh_claude_status()
        if key == "settings":
            self.settings_view.load_from_store()

    def current_page(self) -> str:
        return PAGES[self.stack.currentIndex()]

    def _focus_intake(self) -> None:
        self._navigate("queue")
        self.queue_view.focus_input()

    def _focus_search(self) -> None:
        page = self.current_page()
        target = {"queue": self.queue_view, "applications": self.apps_page, "files": self.files_view}.get(page)
        if target is not None:
            target.focus_search()

    def _show_in_applications(self, job_id: str) -> None:
        self._navigate("applications")
        self.apps_page.view.select_job(job_id)

    def _show_in_queue(self, job_id: str) -> None:
        job = self.queue_store.get(job_id)
        self._navigate("queue")
        if job:
            self.queue_view.search.setText(job.company)

    # ------------------------------------------------------------------ refresh

    def _queue_status_filter(self, key) -> str | None:
        if key is None:
            return None
        if key == "processing":
            return "processing"
        if isinstance(key, JobStatus):
            return key.value
        return None

    def _refresh_queue(self) -> None:
        if getattr(self, "_closing", False):
            return  # the database is closed (or closing); a late background event must not touch it
        status = self._queue_status_filter(self.queue_view.filter_key())
        page = self.queue_store.query_page(
            search=self.queue_view.search_text(),
            status_filter=status,
            offset=self.queue_view.page_offset(),
            limit=QueueView.PAGE_SIZE,
        )
        c = self.queue_store.summary_counts()
        counts = {k: c[k] for k in ("done", "skipped", "needs", "failed", "processing", "remaining")}
        self.queue_view.set_page(page.jobs, page.total, counts=counts)
        self.queue_badge.setText(str(c["total"]) if c["total"] else "")
        self.queue_badge.setVisible(bool(c["total"]))
        self.queue_view.set_worker_state(self.engine.state.value)
        self._update_apps_badge()

    def _update_apps_badge(self) -> None:
        ready = self.apps.ready_count()
        self.apps_badge.setText(str(ready) if ready else "")
        self.apps_badge.setVisible(bool(ready))
        self.apps_badge.setToolTip(f"{ready} with documents ready, not yet applied")

    def _deliver_on_ui(self, fn) -> None:
        """Run ``fn`` on the UI thread: at once when already there, otherwise queued to it."""
        if threading.current_thread() is threading.main_thread():
            fn()
        else:
            self.ui_call.emit(fn)

    @Slot(object)
    def _run_ui_call(self, fn) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - a failed refresh must not take the window down
            pass

    def _on_apps_changed(self) -> None:
        self._update_apps_badge()
        self._refresh_queue()

    def _on_store_changed(self) -> None:
        self.apps_page.view.request_refresh()
        self.analytics_view.request_refresh()

    def _update_sidebar_status(self) -> None:
        if self.is_viewer:
            ready = self.viewer.sync.connected
            self.api_title.setText("Controller")
            self.api_state.setText("Connected" if ready else "Offline")
            self.api_state.setToolTip(self.viewer.sync.error)
        elif self.settings_store.uses_claude_chat():
            ready = self._claude_signed_in
            self.api_title.setText(f"{self.chat_label} web chat")
            self.api_state.setText("Connected" if ready else "Sign in required")
        else:
            ready = self.settings_store.has_api_key()
            self.api_title.setText("Claude API")
            self.api_state.setText("Connected" if ready else "Key not set")
        self.queue_view.set_api_connected(ready)
        state_name = "Connected" if ready else "Disconnected"
        if self.api_state.objectName() != state_name:
            self.api_state.setObjectName(state_name)
            self.api_state.style().unpolish(self.api_state)
            self.api_state.style().polish(self.api_state)
        name = "StatusDot" if ready else "StatusDotOff"
        if self.status_dot.objectName() != name:
            self.status_dot.setObjectName(name)
            self.status_dot.style().unpolish(self.status_dot)
            self.status_dot.style().polish(self.status_dot)
        root = self.settings_store.settings.resolved_output_root()
        path = str(root)
        if len(path) > 24:
            path = "…" + path[-23:]
        self.output_status.setText(path)
        self.output_status.setWordWrap(False)
        self.output_status.setToolTip(str(root))

    # ------------------------------------------------------------------ queue

    def _enqueue_urls(self, urls: list[str], allow_duplicate_companies: bool) -> tuple[int, int]:
        """Add links that are not queued yet; returns (added, already queued)."""
        existing = self.queue_store.urls()
        fresh = [u for u in dict.fromkeys(urls) if u.rstrip("/") not in existing]
        jobs = []
        for url in fresh:
            job = JobItem.create(url, source=detect_source(url))
            job.duplicate_override = allow_duplicate_companies
            job.log("Added to the queue." + (" Duplicate-company rule overridden." if allow_duplicate_companies else ""))
            jobs.append(job)
        if jobs:
            self.queue_store.add_many(jobs)
        return len(jobs), len(urls) - len(jobs)

    def _add_urls(self, urls: list[str], allow_duplicate_companies: bool) -> None:
        if self.is_viewer:
            try:
                result = self.remote_queue.add_urls(urls, allow_duplicate_companies)
            except ApplicationError as exc:
                system.toast(self, str(exc), "warn")
                return
            added, skipped = int(result["added"]), int(result["skipped"])
        else:
            added, skipped = self._enqueue_urls(urls, allow_duplicate_companies)
        if not added:
            system.toast(self, "Those links are already in the queue.", "warn")
            return
        self.queue_view.clear_input()
        self._refresh_queue()
        msg = C.STATUS_ADDED.format(n=added) + (f" ({skipped} already queued)" if skipped else "")
        self.statusBar().showMessage(msg, 5000)
        system.toast(self, msg, "success")

    def _start(self) -> None:
        if self.is_viewer:
            show_info(self, C.NAV_QUEUE, ON_CONTROLLER)
            return
        if self.settings_store.uses_claude_chat():
            self._refresh_claude_status()
            if not self._claude_signed_in:
                if ask_yes_no(self, C.DLG_CLAUDE_SIGNIN_TITLE, C.DLG_CLAUDE_SIGNIN_BODY):
                    self._navigate("claude")
                return
        elif not self.settings_store.has_api_key():
            if not ask_yes_no(self, C.DLG_API_MISSING_TITLE, C.DLG_API_MISSING_BODY):
                self._navigate("settings")
                return
        queued = any(j.status == JobStatus.QUEUED for j in self.queue_store.jobs())
        if not queued and self.engine.state == WorkerState.IDLE:
            show_info(self, C.NAV_QUEUE, C.DLG_NO_QUEUED)
            return
        self.engine.start()
        self.queue_view.set_worker_state(self.engine.state.value)
        self.statusBar().showMessage(C.STATUS_STARTED, 3000)

    def _pause(self) -> None:
        if self.is_viewer:
            show_info(self, C.NAV_QUEUE, ON_CONTROLLER)
            return
        self.engine.pause()
        self.queue_view.set_worker_state(self.engine.state.value)
        self.statusBar().showMessage(C.STATUS_PAUSED, 3000)

    def _stop(self) -> None:
        if self.is_viewer:
            show_info(self, C.NAV_QUEUE, ON_CONTROLLER)
            return
        self.engine.stop()
        self.queue_view.set_worker_state(self.engine.state.value)
        self.statusBar().showMessage(C.STATUS_STOPPING, 3000)

    def _on_select_job(self, job_id: str) -> None:
        if not job_id:
            self.inspector.clear()
            return
        job = self.queue_store.get(job_id)
        if job:
            self.inspector.show_job(job)
        else:
            self.inspector.clear()

    def _remote_call(self, fn, *args):
        """Run a viewer action; the controller being unreachable or refusing is shown, never raised."""
        try:
            return fn(*args)
        except ApplicationError as exc:
            system.toast(self, str(exc), "warn")
            return None

    def _submit_jd(self, job_id: str, text: str) -> None:
        ok = self._remote_call(self.engine.submit_jd, job_id, text) if self.is_viewer \
            else self.engine.submit_jd(job_id, text)
        if ok:
            self._refresh_queue()
            job = self.queue_store.get(job_id)
            if job:
                self.inspector.show_job(job)
            self.statusBar().showMessage(C.STATUS_JD_SAVED, 4000)
            if self.engine.state == WorkerState.IDLE and not self.is_viewer:
                self.engine.start()

    def _retry_job(self, job_id: str, mode: str) -> None:
        if self.is_viewer:
            show_info(self, C.NAV_QUEUE, ON_CONTROLLER)
            return
        self.engine.retry_job(job_id, mode)
        self._refresh_queue()
        job = self.queue_store.get(job_id)
        if job:
            self.inspector.show_job(job)
        if self.engine.state == WorkerState.IDLE:
            self.engine.start()

    def _skip_job(self, job_id: str) -> None:
        if self.is_viewer:
            self._remote_call(self.engine.skip_job, job_id)
        else:
            self.engine.skip_job(job_id)
        self._refresh_queue()
        job = self.queue_store.get(job_id)
        if job:
            self.inspector.show_job(job)

    def _allow_duplicate(self, job_id: str) -> None:
        """Explicit per-job override of the one-folder-per-company rule, then process it."""
        if self.is_viewer:
            self._remote_call(self.remote_queue.allow_duplicate, job_id)
        elif self._requeue_duplicate(job_id) and self.engine.state == WorkerState.IDLE:
            if self.settings_store.uses_claude_chat() and not self._claude_signed_in:
                system.toast(self, f"Override saved. Press Start once {self.chat_label} is signed in.", "info")
            else:
                self.engine.start()
        self._refresh_queue()
        job = self.queue_store.get(job_id)
        if job:
            self.inspector.show_job(job)

    def _requeue_duplicate(self, job_id: str) -> bool:
        """Allow this company again; True when a skipped job went back into the queue."""
        job = self.queue_store.get(job_id)
        if job is None:
            return False
        self.apps.set_duplicate_override(job_id, True)
        job = self.queue_store.get(job_id)
        if job.status != JobStatus.SKIPPED:
            return False
        job.skip_reason = ""
        job.log("Duplicate-company rule overridden by user; re-queued.")
        job.set_status(JobStatus.QUEUED, "Queued (duplicate company allowed)")
        self.queue_store.update(job)
        return True

    def _removable_ids(self, job_ids: list[str]) -> list[str]:
        """Only jobs that never reached an application and are not mid-generation can be removed."""
        removable = []
        for job_id in job_ids:
            job = self.queue_store.get(job_id)
            if job and not job.applied_at and job.status in {
                JobStatus.QUEUED, JobStatus.NEEDS_JD, JobStatus.FAILED, JobStatus.SKIPPED,
            }:
                removable.append(job_id)
        return removable

    def _remove_jobs(self, job_ids: list[str]) -> None:
        removable = self._removable_ids(job_ids)
        if not removable:
            show_info(self, C.DLG_REMOVE_TITLE, C.DLG_REMOVE_NONE)
            return
        if not ask_yes_no(self, C.DLG_REMOVE_TITLE, C.DLG_REMOVE_CONFIRM.format(n=len(removable))):
            return
        if self.is_viewer:
            if self._remote_call(self.remote_queue.remove_jobs, removable) is None:
                return
        else:
            self.queue_store.remove(removable)
        self._refresh_queue()
        self.inspector.clear()

    def _delete_selected(self) -> None:
        if self.current_page() != "queue":
            return
        focus = self.focusWidget()
        if focus is not None and focus.__class__.__name__ in {"QLineEdit", "QTextEdit", "QPlainTextEdit"}:
            return
        ids = self.queue_view.selected_job_ids()
        if ids:
            self._remove_jobs(ids)

    def _escape(self) -> None:
        focus = self.focusWidget()
        if focus is not None:
            focus.clearFocus()

    def _sync_files(self, job_ids: list[str]) -> None:
        """Rebuild documents deleted from disk, from the resume JSON kept in the database."""
        if not job_ids:
            return
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            # a viewer's database copy is read-only: it only writes the files, and remembers the jobs as handled
            results = sync_jobs_to_disk(self.queue_store, self.settings_store.settings, job_ids,
                                        record=not self.is_viewer)
        finally:
            QGuiApplication.restoreOverrideCursor()
        if self.materializer is not None:
            self.materializer.mark_handled([r.job_id for r in results if r.ok])
        restored = sum(len(r.restored) for r in results)
        synced = sum(1 for r in results if r.ok and r.restored)
        failed = [r for r in results if not r.ok]
        untouched = sum(1 for r in results if r.ok and not r.restored)
        if failed:
            first = failed[0].message
            self.toast.show_message(C.QUEUE_REGENERATE_PARTIAL.format(files=restored, ok=synced, fail=len(failed))
                                    + f" {first}", kind="warn")
        elif restored:
            self.toast.show_message(C.QUEUE_REGENERATE_DONE.format(files=restored, ok=synced), kind="success")
        else:
            self.toast.show_message("Nothing to restore: every file is already on disk." if untouched else
                                    "Nothing to sync.", kind="info")
        self._refresh_queue()
        self.files_view.refresh()

    def _export_csv_selected(self, job_ids: list[str]) -> None:
        if not job_ids:
            return
        path, _ = QFileDialog.getSaveFileName(self, C.QUEUE_EXPORT_CSV, "applications.csv", "CSV (*.csv)")
        if not path:
            return
        jobs = [j for jid in job_ids if (j := self.queue_store.get(jid))]
        export_jobs_csv(Path(path), jobs)
        self.toast.show_message(C.QUEUE_CSV_SAVED.format(n=len(jobs)), kind="success")

    def _mark_applied(self, job_ids: list[str]) -> None:
        result = self._remote_call(self.apps.bulk_mark_applied, job_ids)
        if result is None:
            return
        done, skipped = result
        msg = f"Marked {done} as applied" + (f"; {skipped} skipped (no documents, or already applied)" if skipped else "")
        self.toast.show_message(msg, kind="success" if done else "warn")

    # ------------------------------------------------------------------- claude

    def _copy_claude_json(self) -> None:
        if self.is_viewer:
            show_info(self, C.NAV_QUEUE, ON_CONTROLLER)
            return
        from PySide6.QtWidgets import QApplication

        from ui.dialogs import show_error

        self._navigate("claude")
        service = self.claude_view.chat_service()
        text, err = service.capture_resume_json(use_clipboard=True)
        if err:
            show_error(self, C.INSPECTOR_COPY_CLAUDE_JSON, err)
            return
        QApplication.clipboard().setText(text)
        self.inspector.flash_copy_claude_json()
        self.toast.show_message(C.INSPECTOR_COPY_CLAUDE_JSON_OK, kind="success")

    def _pull_claude_json(self, job_id: str) -> None:
        if self.is_viewer:
            show_info(self, C.NAV_QUEUE, ON_CONTROLLER)
            return
        from ui.dialogs import show_error

        self._navigate("claude")
        service = self.claude_view.chat_service()
        text, err = service.capture_resume_json(use_clipboard=True)
        if err:
            show_error(self, C.INSPECTOR_PULL_CLAUDE, err)
            return
        if service.busy:
            service.set_manual_reply(text)
            self.toast.show_message(C.INSPECTOR_PULL_CLAUDE_BUSY, kind="success")
            return
        ok, message = self.engine.apply_resume_json(job_id, text)
        if not ok:
            show_error(self, C.INSPECTOR_PULL_CLAUDE, message)
            return
        self._refresh_queue()
        job = self.queue_store.get(job_id)
        if job:
            self.inspector.show_job(job)
        self.toast.show_message(C.INSPECTOR_PULL_CLAUDE_OK, kind="success")
        if self.engine.state == WorkerState.IDLE:
            self.engine.start()

    def _import_resume_json(self, job_id: str, json_text: str) -> None:
        if self.is_viewer:
            show_info(self, C.NAV_QUEUE, ON_CONTROLLER)
            return
        ok, message = self.engine.apply_resume_json(job_id, json_text)
        if not ok:
            from ui.dialogs import show_error

            show_error(self, C.INSPECTOR_IMPORT_JSON, message)
            return
        self._refresh_queue()
        job = self.queue_store.get(job_id)
        if job:
            self.inspector.show_job(job)
        self.toast.show_message(C.INSPECTOR_JSON_IMPORTED, kind="success")
        if self.engine.state == WorkerState.IDLE:
            self.engine.start()

    def _claude_chat_generate(self, prompt: str, meta: dict, settings) -> dict:
        """Called from the queue worker — waits on main-thread timer, not a frozen UI loop."""
        # Questions from application forms have their own chat page and do not wait for this one. The two pages share
        # the clipboard, so only the moment of pasting is taken in turns: wait (a few seconds at most) while the other
        # page is pasting, then start.
        deadline = time.monotonic() + 600
        self._pending_claude = (prompt, meta, settings)
        self._claude_wait.clear()
        self._claude_thread_result = None
        self._claude_wait_error = ""
        self._claude_blocked = ""
        while True:
            self._claude_begin_deferred = False
            ok = QMetaObject.invokeMethod(
                self,
                "_claude_begin_on_main",
                Qt.ConnectionType.BlockingQueuedConnection,
            )
            if not ok:
                raise ClaudeError("Could not start Claude on the UI thread.")
            if not self._claude_begin_deferred:
                break
            if time.monotonic() > deadline:
                raise ClaudeError(f"{self.chat_label} stayed busy pasting another message.")
            time.sleep(1)
        if self._claude_wait_error:
            self._raise_chat_failure()
        if not self._claude_wait.wait(timeout=CLAUDE_REPLY_WAIT_SEC + 180):
            self._claude_coordinator.stop()
            self.claude_view.chat_service().finish_generation()
            raise ClaudeError(
                f"Timed out after {CLAUDE_REPLY_WAIT_SEC // 60} minutes waiting for {self.chat_label} JSON."
            )
        if self._claude_wait_error:
            self._raise_chat_failure()
        if not self._claude_thread_result:
            raise ClaudeError(f"{self.chat_label} web chat did not return resume JSON.")
        return self._claude_thread_result

    def _raise_chat_failure(self) -> None:
        err = self._claude_wait_error or f"{self.chat_label} web chat did not return resume JSON."
        blocked = self._claude_blocked or getattr(self.claude_view.chat_service(), "last_block", "")
        self._claude_blocked = ""
        if blocked:
            raise ChatPageBlocked(err)
        raise ClaudeError(err)

    @Slot()
    def _claude_begin_on_main(self) -> None:
        if ChatService.paste_in_progress():
            self._claude_begin_deferred = True  # the other page is pasting: the worker asks again in a second
            return
        prompt, meta, settings = self._pending_claude
        service = self.claude_view.chat_service()
        job_id = self.engine.current_job_id

        def log_progress(message: str) -> None:
            if not job_id:
                return
            job = self.queue_store.get(job_id)
            if job:
                job.log(message)
                self.queue_store.update(job)
                self.engine_event.emit("job_updated", {"job_id": job_id})

        service.set_progress_handler(log_progress)
        reuse_tab = bool(meta.get("claude_reuse_tab"))
        if reuse_tab:
            # "Reuse the tab" means: pick up this job's reply again. That is only safe in this job's own conversation:
            # the page is shared (the apply bot moves it between jobs' chats), so what is on screen may belong to another job.
            saved = str(meta.get("saved_chat_url") or "")
            reuse_tab = bool(saved) and service.load_url(saved) and service.conversation_url() == saved
            if not reuse_tab:
                log_progress("This job's earlier conversation is not available: starting a new chat.")
        text_before, err = service.begin_generation(prompt, reuse_tab=reuse_tab)
        if err:
            self._claude_wait_error = err
            service.set_progress_handler(None)
            self._claude_wait.set()
            return
        self._claude_coordinator.start(
            service, settings, meta, text_before, progress=log_progress
        )

    @Slot(object)
    def _on_claude_wait_finished(self, result: ClaudeGenResult) -> None:
        service = self.claude_view.chat_service()
        service.set_progress_handler(None)
        service.finish_generation()
        meta = self._pending_claude[1]
        meta["chat_url"] = service.last_chat_url  # the engine saves it on the job (see QueueEngine)
        meta["chat_provider"] = service.site.key
        self._claude_blocked = result.blocked or ""
        if result.error or not result.resume:
            self._claude_wait_error = result.error or f"{self.chat_label} did not return resume JSON."
        else:
            self._claude_thread_result = result.resume
        self._claude_wait.set()

    # ----------------------------------------------------------------- auto apply

    def _setup_bot(self) -> None:
        self.bot = BotServices(
            self.queue_store, self.apps, self.settings_store, ask_chat=self.ask_chat,
            on_queued=lambda ids: self.engine_event.emit("bot_queued", {"ids": list(ids)}),
            in_app_alert=lambda title, body: self.engine_event.emit("bot_alert", {"title": title, "body": body}))
        v = self.autoapply_view
        v.open_browser_requested.connect(self._open_bot_browser)
        v.run_discovery_requested.connect(self.bot.discovery.run_soon)
        v.mode_requested.connect(self._set_apply_mode)
        v.kill_requested.connect(self.bot.apply.kill)
        v.resume_requested.connect(self.bot.apply.resume)
        v.apply_now_requested.connect(self._apply_now)
        v.dismiss_requested.connect(self._dismiss_attempt)
        v.applied_requested.connect(self._applied_by_hand)
        v.facts_saved.connect(lambda: self.toast.show_message("Answers saved.", kind="success"))
        if hasattr(self.engine, "retry_pause_seconds"):
            self.engine.retry_pause_seconds = 20.0
        if hasattr(self.engine, "reporter"):
            self.engine.reporter = self.bot.reporter  # how each resume went, to Telegram
        self.bot.start()
        # Jobs the bot queued earlier (before a restart, or while the chat was signed out) must not wait for the next
        # new job to arrive before they are generated: look once a minute and start the worker when it is idle.
        self._queue_kick = QTimer(self)
        self._queue_kick.setInterval(60_000)
        self._queue_kick.timeout.connect(self._kick_waiting_queue)
        self._queue_kick.start()

    def _kick_waiting_queue(self) -> None:
        try:
            s = self.settings_store.settings
            if self.is_viewer or not s.discovery_enabled or self.engine.state != WorkerState.IDLE:
                return
            if self.settings_store.data_locked:
                return
            self._requeue_failed_generations()
            if any(j.status == JobStatus.QUEUED and not j.archived_at for j in self.queue_store.jobs()):
                if self._maybe_auto_start():
                    self.statusBar().showMessage("Generating the jobs that were waiting in the queue.", 5000)
        except Exception:  # noqa: BLE001 - a housekeeping tick must never take the window down
            pass

    REQUEUE_AFTER_MIN = (20, 60, 180)  # a failed generation is tried again after these waits, then left for you

    def _requeue_failed_generations(self) -> None:
        """A chat that was slow, signed out for a moment, or over its limit fails every job sent to it. Those jobs
        are put back in the queue later instead of waiting for someone to press Retry on each one."""
        if not hasattr(self.engine, "retry_job"):
            return
        counts = self.__dict__.setdefault("_requeue_counts", {})
        now = datetime.now(timezone.utc)
        for job in self.queue_store.jobs():
            if job.status != JobStatus.FAILED or job.failed_stage != "generation" or job.archived_at:
                continue
            if not ats.classify(job.origin_url or job.url).supported:
                continue  # only the jobs the bot itself is working on
            done = counts.get(job.id, 0)
            if done >= len(self.REQUEUE_AFTER_MIN):
                continue
            try:
                failed_at = datetime.fromisoformat(job.updated_at)
            except (TypeError, ValueError):
                continue
            if failed_at.tzinfo is None:
                failed_at = failed_at.replace(tzinfo=timezone.utc)
            if now - failed_at >= timedelta(minutes=self.REQUEUE_AFTER_MIN[done]):
                counts[job.id] = done + 1
                self.engine.retry_job(job.id, "generation")

    def _open_bot_browser(self) -> None:
        """Opening the browser can take a while (it starts Chrome): never on the UI thread."""
        def work() -> None:
            try:
                self.bot.open_browser()
            except Exception as exc:  # noqa: BLE001
                self.engine_event.emit("bot_alert", {"title": "The bot browser did not open", "body": str(exc)})

        threading.Thread(target=work, name="open-bot-browser", daemon=True).start()
        self.toast.show_message("Opening the bot browser… sign in to Jobright and Outlook in it.", kind="info")

    def _set_apply_mode(self, mode: str) -> None:
        if mode == "auto" and not ask_yes_no(
                self, "Send applications automatically?",
                "The bot will submit applications to employers by itself, without asking you. Each one is checked "
                "first and anything it cannot answer honestly is held back for you. Continue?"):
            self.autoapply_view.refresh()
            return
        self.settings_store.settings.apply_mode = mode
        self.settings_store.save()
        self.bot.settings_changed()
        self.toast.show_message({"off": "Auto apply is off.", "dry_run": "Dry run: forms are filled and checked, never sent.",
                                 "auto": "Auto apply is on."}[mode], kind="info")

    def _apply_now(self, job_id: str, submit: bool) -> None:
        if submit and not ask_yes_no(
                self, "Submit this application?",
                "The bot will open this job in its browser, fill the form, and press Submit. "
                "If the employer then emails a security code, the bot reads that code from Outlook in the same "
                "browser and types it in once. A picture check is left for you to complete. Continue?"):
            return
        self.bot.apply.request_now(job_id, submit=submit)
        self.toast.show_message(
            "Submitting this application in the bot browser…" if submit
            else "Opening this application in the bot browser…", kind="info")
        self.autoapply_view.refresh()

    def _dismiss_attempt(self, attempt_id: int) -> None:
        self.bot.apply.attempts.dismiss(attempt_id, datetime.now(timezone.utc))
        self.autoapply_view.refresh()

    def _applied_by_hand(self, job_id: str, attempt_id: int) -> None:
        try:
            self.apps.mark_applied(job_id, note="Applied by hand after the bot stopped")
        except ApplicationError as exc:
            system.toast(self, str(exc), "warn")
            return
        self.bot.apply.attempts.resolve(attempt_id, datetime.now(timezone.utc))
        self.autoapply_view.refresh()
        self._refresh_queue()

    def _update_autoapply(self) -> None:
        view = self.autoapply_view
        if self.bot is None or not view.isVisible():
            return
        s = self.settings_store.settings
        problems = list(self.bot.notifier.failures[-2:])
        view.set_status(browser_running=self.bot.browser_running, discovery=self.bot.discovery.status,
                        apply=self.bot.apply.status, killed=self.bot.apply.killed, mode=s.apply_mode,
                        cap=s.apply_daily_cap, problems=problems)

    # ----------------------------------------------- questions in a job's chat

    def ask_chat(self, question_prompt: str, *, chat_url: str = "", context_prompt: str = "",
                 timeout_sec: int = 300, wait_for_page_sec: int = 1800) -> AskResult:
        """Ask a question in a chat conversation and return the answer. Safe to call from any thread but the UI's.

        Questions use their own chat page, one question at a time; resume generation is not held up by them. Waits
        (up to ``wait_for_page_sec``) only while that page is answering another question, or while the other page is
        in the middle of pasting (the clipboard is shared)."""
        if self.is_viewer or self.claude_view is None:
            return AskResult(error="This copy of the app does not use a chat.")
        return self._ask_chat_now(question_prompt, chat_url, context_prompt, timeout_sec, wait_for_page_sec)

    def _question_service(self) -> ChatService:
        """The chat page for application questions (the resume page when the view has no second one)."""
        make = getattr(self.claude_view, "question_service", None)
        return make() if callable(make) else self.claude_view.chat_service()

    def _ask_chat_now(self, question_prompt: str, chat_url: str, context_prompt: str, timeout_sec: int,
                      wait_for_page_sec: int) -> AskResult:
        done = threading.Event()
        box: dict[str, AskResult] = {}

        def deliver(result: AskResult) -> None:
            box["result"] = result
            done.set()

        args = {"deliver": deliver, "prompt": question_prompt, "chat_url": chat_url,
                "context": context_prompt, "timeout": timeout_sec}
        deadline = time.monotonic() + wait_for_page_sec
        while not self._call_on_ui(self._start_ask, args, wait=120):
            if time.monotonic() >= deadline:
                return AskResult(error="The chat page stayed busy; the question was not asked.")
            time.sleep(3)
        if not done.wait(timeout_sec + 180):
            return AskResult(error="The chat did not answer in time.")
        return box["result"]

    def _start_ask(self, args: dict) -> bool:
        service = self._question_service()
        if self._asker is None or self._asker.service is not service:
            self._asker = ChatAsker(service, self)
            self._asker.finished.connect(self._on_ask_finished)
        if self._asker.active or service.busy or (service is not self.claude_view.chat_service()
                                                  and ChatService.paste_in_progress()):
            return False
        self._ask_waiter = args["deliver"]
        if not self._asker.start(args["prompt"], chat_url=args["chat_url"], context_prompt=args["context"],
                                 timeout_sec=args["timeout"]):
            self._ask_waiter = None
            return False
        return True

    @Slot(object)
    def _on_ask_finished(self, result: AskResult) -> None:
        waiter, self._ask_waiter = self._ask_waiter, None
        if waiter is not None:
            waiter(result)

    @Slot()
    def _prepare_claude_tab(self) -> None:
        self._navigate("claude")

    def _refresh_claude_status(self) -> None:
        if self.is_viewer:
            return
        service = self.claude_view.chat_service()
        if service.busy:
            return
        state, err = service.run_page_state()
        if err:
            self._claude_signed_in = False
        else:
            self._claude_signed_in = bool(state.get("loggedIn"))
        self._update_sidebar_status()

    # ------------------------------------------------------- browser extension

    def _setup_intake(self) -> None:
        self._intake_pending = False
        self.intake_request.connect(self._on_intake_request)
        self.intake = IntakeServer(IntakeService(
            self.queue_store,
            token=lambda: get_token(self.settings_store.secret_path),
            add_job=self._intake_from_thread,
            auto_start=lambda: self.settings_store.settings.intake_auto_start,
            profile=lambda: self.settings_store.settings.profile,
            locked=lambda: self.settings_store.data_locked,
            apply_event=self._apply_event_from_thread,
            settings=lambda: self.settings_store.settings,
            worker_state=lambda: self.engine.state.value,
            command=self._controller_command,
            allowed_hosts=self._allowed_hosts,
            sync_enabled=lambda: self.is_controller,
        ), port=INTAKE_PORT, host=self._bind_host())
        self._apply_intake_settings()

    def _bind_host(self) -> str:
        """Only a controller listens beyond this computer, and only where its owner said to."""
        return self.settings_store.settings.intake_bind if self.is_controller else "127.0.0.1"

    def _allowed_hosts(self) -> set[str]:
        raw = self.settings_store.settings.intake_allowed_hosts
        return {h.strip().lower() for h in raw.replace(";", ",").split(",") if h.strip()}

    def _apply_intake_settings(self) -> None:
        s = self.settings_store.settings
        host = self._bind_host()
        if self.intake.running and self.intake.host != host:
            self.intake.stop()  # the address to listen on changed
        self.intake.host = host
        if s.intake_enabled:
            self.intake.start()
        else:
            self.intake.stop()
        self.settings_view.set_intake_status(self.intake.running, self.intake.port, self.intake.error)

    def _call_on_ui(self, fn, data, wait: float = 15.0):
        """From a server thread: run ``fn(data)`` on the UI thread and return its result, or raise what it raised."""
        holder = {"fn": fn, "data": data, "done": threading.Event(), "result": None, "error": None}
        self.intake_request.emit(holder)
        if not holder["done"].wait(wait):
            raise TimeoutError("The app did not respond.")
        if holder["error"] is not None:
            raise holder["error"]
        return holder["result"]

    def _intake_from_thread(self, data: dict) -> dict:
        """Called by the server thread: hand the job to the UI thread and wait for the answer."""
        try:
            return self._call_on_ui(self._intake_add, data)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc) or "The app did not respond."}

    def _apply_event_from_thread(self, job_id: str, event: str, report: dict) -> dict:
        """Autofill log / "I submitted it" confirmation, run on the UI thread like job intake."""
        try:
            return self._call_on_ui(self._intake_apply_event, (job_id, event, report))
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc) or "The app did not respond."}

    # ---- controller: changes a viewer asked for, applied on the UI thread

    def _controller_command(self, kind: str, name: str, args: list, kwargs: dict):
        return self._call_on_ui(self._run_command, (kind, name, args, kwargs), wait=60.0)

    def _run_command(self, data: tuple):
        kind, name, args, kwargs = data
        if kind == "app":
            return getattr(self.apps, name)(*args, **kwargs)
        return getattr(self, f"_cmd_{name}")(*args)

    def _cmd_skip_job(self, job_id: str) -> None:
        self.engine.skip_job(job_id)
        self._refresh_queue()

    def _cmd_remove_jobs(self, job_ids: list) -> int:
        removable = self._removable_ids([str(j) for j in job_ids])
        if removable:
            self.queue_store.remove(removable)
        self._refresh_queue()
        return len(removable)

    def _cmd_add_urls(self, urls: list, allow_duplicate_companies: bool) -> dict:
        clean = [u.strip() for u in urls if isinstance(u, str) and u.strip().lower().startswith(("http://", "https://"))]
        added, skipped = self._enqueue_urls(clean, bool(allow_duplicate_companies))
        started = self._maybe_auto_start() if added else False
        if added:
            self._intake_pending = not started
        self._refresh_queue()
        return {"added": added, "skipped": skipped + len(urls) - len(clean), "started": started}

    def _cmd_allow_duplicate(self, job_id: str) -> None:
        if self._requeue_duplicate(job_id):
            self._maybe_auto_start()
        self._refresh_queue()

    def _cmd_submit_jd(self, job_id: str, text: str) -> bool:
        ok = bool(self.engine.submit_jd(job_id, str(text)))
        if ok:
            self._maybe_auto_start()
        self._refresh_queue()
        return ok

    def _intake_apply_event(self, args: tuple) -> dict:
        job_id, event, report = args
        if event == "autofilled":
            self.queue_store.add_event(job_id, "autofilled", report)
            return {"status": "logged"}
        done = self.apps.mark_applied(job_id, note="Confirmed in the browser extension after filling the form.")
        if done:
            self._refresh_queue()
            self.toast.show_message("Marked as applied from the browser extension.", kind="success")
        return {"status": "applied" if done else "already_applied"}

    @Slot(object)
    def _on_intake_request(self, holder: dict) -> None:
        try:
            holder["result"] = holder["fn"](holder["data"])
        except Exception as exc:  # noqa: BLE001 - handed back to the server thread, which decides the reply
            holder["error"] = exc
        finally:
            holder["done"].set()

    def _intake_add(self, data: dict) -> dict:
        job = JobItem.create(data["url"], source=data.get("source") or detect_source(data["url"]))
        job.company = data.get("company", "")
        job.role_title = data.get("role_title", "")
        pay = parse_salary(data.get("salary", ""))
        job.salary_text, job.salary_min, job.salary_max = pay.text, pay.low, pay.high
        job.salary_period, job.salary_currency = pay.period, pay.currency
        job.job_description = data["description"]
        job.stage = "Ready to generate"
        job.log("Added from the browser extension with its job description.")
        self.queue_store.add_many([job])
        self._refresh_queue()
        started = self._maybe_auto_start()
        self._intake_pending = not started
        label = job.company or job.role_title or job.url
        self.toast.show_message(f"Added from browser: {label}" + (" — started" if started else ""), kind="success")
        return {"status": "added", "id": job.id, "started": started}

    def _maybe_auto_start(self) -> bool:
        """Start the worker for a newly arrived job, but never override a pause or a missing sign-in."""
        if not self.settings_store.settings.intake_auto_start or self.engine.state != WorkerState.IDLE:
            return False
        if self.settings_store.uses_claude_chat():
            if not self._claude_signed_in:
                return False
        elif not self.settings_store.has_api_key():
            return False
        self.engine.start()
        self.queue_view.set_worker_state(self.engine.state.value)
        return True

    # ------------------------------------------------------------------- viewer

    def _setup_viewer_link(self, cfg: Path, ghost_days: int) -> None:
        s = self.settings_store
        db = self.queue_store.database
        client = ControllerClient(lambda: s.settings.controller_url, s.controller_token)
        sync = ViewerSync(db, client, on_profile=lambda payload: self.viewer.accept_profile(payload))
        self.remote_queue = RemoteQueue(sync)
        self.viewer = ViewerLink(sync, self.remote_queue, self)
        self.apps = RemoteApplicationService(db, ghost_after_days=ghost_days)
        self.apps.attach(sync)
        self.materializer = FileMaterializer(cfg / "viewer_state.json")
        # The controller stores paths under its own output folder; this computer shows them under its own.
        db.path_mapper = make_path_mapper(lambda: self.viewer.controller_root, lambda: s.settings.resolved_output_root())
        self.viewer.profile_changed.connect(self._on_viewer_profile)
        self.viewer.data_changed.connect(self._on_viewer_data)
        self.viewer.status_changed.connect(self._on_viewer_status)

    def _start_viewer_link(self) -> None:
        self.viewer.start()

    @Slot()
    def _on_viewer_profile(self) -> None:
        """The controller's profile and export choices, used here so rebuilt documents match its documents."""
        import dataclasses

        payload = self.viewer.latest_profile
        if not payload:
            return
        s = self.settings_store.settings
        known = {f.name for f in dataclasses.fields(ProfileSettings)}
        s.profile = ProfileSettings(**{k: str(v) for k, v in payload.get("profile", {}).items() if k in known})
        s.export_pdf = bool(payload.get("export_pdf", True))
        s.export_cover_letter = bool(payload.get("export_cover_letter", True))
        apply_settings_profile(s)

    @Slot()
    def _on_viewer_data(self) -> None:
        """A newer copy of the controller's database was installed: refresh every screen from it."""
        self._refresh_queue()
        self._on_store_changed()
        selected = self.queue_view.selected_job_id()
        job = self.queue_store.get(selected) if selected else None
        if job:
            self.inspector.show_job(job)
        self._materialize_new_files()
        self.files_view.refresh()

    @Slot()
    def _on_viewer_status(self) -> None:
        sync = self.viewer.sync
        stamp = sync.last_ok.astimezone().strftime("%H:%M:%S") if sync.last_ok else ""
        self.settings_view.set_link_status(sync.connected, sync.error, stamp)
        self._update_sidebar_status()
        self.queue_view.set_worker_state(self.engine.state.value)

    def _materialize_new_files(self) -> None:
        """Rebuild the documents of resumes the controller finished since we last looked, on this computer."""
        s = self.settings_store.settings
        if not s.auto_sync_files or self.viewer.latest_profile is None:
            return  # without the controller's profile the rebuilt documents would not match its documents
        due = self.materializer.due(self.queue_store.database)
        if not due:
            return
        results = sync_jobs_to_disk(self.queue_store, s, due, record=False)
        self.materializer.record(results)
        written = sum(len(r.restored) for r in results)
        if written:
            self.toast.show_message(f"Saved {written} new file(s) to this computer.", kind="success")

    def _on_settings_saved(self) -> None:
        apply_settings_profile(self.settings_store.settings)
        if self.settings_store.settings.app_mode != self.mode:
            self.toast.show_message("Restart the app to switch mode.", kind="info")
        if self.is_viewer:
            self.viewer.sync_soon()  # new address or token: try straight away
        else:
            self._apply_intake_settings()
            if self.bot is not None:
                self.bot.settings_changed()
            self.autoapply_view.load_facts()
        self.apps.ghost_after_days = self.settings_store.settings.ghost_after_days
        self._apply_theme()
        root = self.settings_store.settings.resolved_output_root()
        self.files_view.set_output_root(root, refresh=self.current_page() == "files")
        self.manual_view.set_output_root(root)
        self._update_sidebar_status()
        self.statusBar().showMessage(C.STATUS_SETTINGS_APPLIED, 3000)
        self.toast.show_message(C.SETTINGS_SAVED, kind="success")

    @Slot(str, dict)
    def _on_engine_event(self, name: str, payload: dict) -> None:
        if getattr(self, "_closing", False):
            return
        if name == "claude_chat_started":
            self.toast.show_message(
                f"{self.chat_label} is generating in the background — open it from the sidebar to watch, "
                "or stay on Job Queue and use Import JSON when done.",
                kind="info",
            )
        if name == "store_changed":
            self._on_store_changed()
        if name == "bot_queued":
            n = len(payload.get("ids", []))
            self._refresh_queue()
            started = self._maybe_auto_start()
            self.toast.show_message(f"Found {n} new Greenhouse job{'s' if n != 1 else ''} on Jobright"
                                    + (" — generating" if started else ""), kind="success")
        if name == "bot_alert":
            self.toast.show_message(f"{payload.get('title', '')}: {payload.get('body', '')}"[:200], kind="warn")
        if name in {"job_updated", "job_started", "batch_finished", "state", "claude_chat_finished"}:
            self._refresh_queue()
            selected = self.queue_view.selected_job_id()
            if selected:
                job = self.queue_store.get(selected)
                if job:
                    self.inspector.show_job(job)
            if name == "state":
                self.queue_view.set_worker_state(payload.get("state", "idle"))
            if name in ("job_updated", "batch_finished"):
                # generated jobs become applications automatically (never marked applied)
                self.apps_page.view.request_refresh()
                self.analytics_view.request_refresh()
            if name == "batch_finished":
                if self._intake_pending and any(j.status == JobStatus.QUEUED for j in self.queue_store.jobs()):
                    self._intake_pending = False
                    self._maybe_auto_start()  # a job arrived just as the worker was finishing
                self.statusBar().showMessage(C.STATUS_BATCH_DONE, 5000)
                self.files_view.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True  # stop UI refreshes first; background threads may still emit while they wind down
        if self.is_viewer:
            self.viewer.stop()
        else:
            if self.engine.state in {WorkerState.RUNNING, WorkerState.PAUSED}:
                self.engine.stop()
            self.intake.stop()
            if self.bot is not None:
                self.bot.stop()
        self.queue_store.database.close()
        super().closeEvent(event)
