"""Job Queue: URL intake, batch controls, live status, dense table and a details strip."""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QSizePolicy,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.files import file_refs, output_folder
from core.models import JobItem, JobStatus
from core.salary import format_salary
from core.scraper import parse_url_list
from core.timeutil import format_local
from ui import copy as C
from ui import system, theme
from ui.job_steps import progress_pct
from ui.widgets import (
    badge,
    chip_button,
    circle_icon,
    clear_layout,
    copy_button,
    field_label,
    ghost_button,
    muted_label,
    RowTable,
    page_title,
    paint_row_hover,
    primary_button,
    row_action,
    search_field,
    success_button,
)

FILTERS = [
    ("All", None),
    ("Queued", JobStatus.QUEUED),
    ("Processing", "processing"),
    ("Needs JD", JobStatus.NEEDS_JD),
    ("Done", JobStatus.DONE),
    ("Skipped", JobStatus.SKIPPED),
    ("Failed", JobStatus.FAILED),
]

# key, label, glyph, colour role
STAT_ROLES = [
    ("done", "Done", "check", "success"), ("processing", "Processing", "refresh", "accent"),
    ("needs", "Needs JD", "alert", "warning"), ("skipped", "Skipped", "skip", "muted"),
    ("failed", "Failed", "x", "danger"), ("remaining", "Remaining", "clock", "muted"),
]
C_CHECK, C_NUM, C_COMPANY, C_ROLE, C_STATUS, C_STAGE, C_SOURCE, C_ACTIONS = range(8)


def _role_color(role: str) -> str:
    return {"success": "#2FD072", "accent": "#3B82F6", "warning": "#F5B83D", "danger": "#EF5350",
            "muted": "#7C8699"}[role]


class _CheckDelegate(QStyledItemDelegate):
    """Row checkbox, drawn from the table's checked set (independent of the active row)."""

    def __init__(self, is_checked, parent=None) -> None:
        super().__init__(parent)
        self._is_checked = is_checked

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        paint_row_hover(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        if opt.widget:
            opt.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        c = theme.current()
        checked = self._is_checked(index.row())
        size = 14
        r = QRect(option.rect.left() + (option.rect.width() - size) // 2,
                  option.rect.top() + (option.rect.height() - size) // 2, size, size)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(c.accent if checked else c.text_secondary), 1.4))
        painter.setBrush(QColor(c.accent) if checked else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(r, 4, 4)
        if checked:
            painter.setPen(QPen(QColor("#FFFFFF"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            o = r.topLeft()
            painter.drawPolyline([o + QPoint(4, 8), o + QPoint(7, 11), o + QPoint(12, 5)])
        painter.restore()


class _QueueTable(RowTable):
    """A click in the checkbox column toggles the check only; any other click makes the row active."""

    check_toggled = Signal(int)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and index.column() == C_CHECK:
            self.check_toggled.emit(index.row())
            return
        super().mousePressEvent(event)


class QueueView(QWidget):
    add_jobs = Signal()  # keyboard shortcut / legacy: focus the intake box
    add_urls = Signal(list, bool)  # urls, allow_duplicate_companies
    start = Signal()
    pause = Signal()
    stop = Signal()
    selection_changed = Signal(str)
    filters_changed = Signal()
    sync_files = Signal(list)  # restore missing documents of these jobs from the database
    export_csv_selected = Signal(list)
    mark_applied = Signal(list)
    retry_job = Signal(str)
    skip_job = Signal(str)
    allow_duplicate = Signal(str)
    remove_jobs = Signal(list)
    show_in_applications = Signal(str)

    PAGE_SIZE = 50

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ContentPane")
        self._jobs: list[JobItem] = []
        self._filter = None
        self._search = ""
        self._page_offset = 0
        self._page_total = 0
        self._all_jobs_for_stats: list[JobItem] = []
        self._filter_buttons: dict[str, QPushButton] = {}
        self._stat_labels: dict[str, QLabel] = {}
        self._counts: dict[str, int] = {}
        self._connected = True
        self._checked: set[str] = set()  # bulk selection (checkboxes); the active row is the table selection

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 0, 14, 10)
        root.setSpacing(10)

        # ---- header: title left, search right
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(page_title("Job Queue"))
        title_box.addWidget(muted_label("Paste job links, generate tailored resumes and cover letters with Claude"))
        header.addLayout(title_box, 1)
        self.search = search_field("Search...")
        self._add_shortcut_hint(self.search)
        self.search.setMinimumHeight(38)
        self.search.textChanged.connect(self._on_search)
        self.filter_btn = ghost_button("", "filter")
        self.filter_btn.setObjectName("IconButton")
        self.filter_btn.setFixedSize(38, 38)
        self.filter_btn.setToolTip("Filter by status")
        self.filter_btn.clicked.connect(self._filter_menu)
        self.side_header = QWidget()
        sh = QHBoxLayout(self.side_header)
        sh.setContentsMargins(10, 2, 14, 10)
        sh.setSpacing(8)
        sh.addWidget(self.search, 1)
        sh.addWidget(self.filter_btn)
        self.api_pill = QLabel("")  # connection now lives in the sidebar; kept for API compatibility
        self.api_pill.hide()
        root.addLayout(header)

        # ---- intake + batch controls
        intake_row = QHBoxLayout()
        intake_row.setSpacing(12)
        card = QFrame()
        card.setObjectName("IntakeCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 8, 14, 8)
        cl.setSpacing(2)
        t = QLabel("Paste job links (one per line)")
        t.setStyleSheet("font-weight: 650; font-size: 14px; background: transparent;")
        title_row = QHBoxLayout()
        title_row.addWidget(t, 1)
        cl.addLayout(title_row)
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("https://jobs.example.com/post/123\nhttps://careers.company.com/job/456")
        self.input.setFixedHeight(40)
        cl.addWidget(self.input)
        self.dup_box = QCheckBox("Allow companies I already have")
        self.dup_box.setToolTip("Overrides the one-folder-per-company rule for the links you add now. "
                                "Off by default: existing companies are skipped.")
        cl.addWidget(self.dup_box, alignment=Qt.AlignmentFlag.AlignRight)
        intake_row.addWidget(card, 1)
        add_col = QVBoxLayout()
        add_col.setSpacing(8)
        self.add_btn = primary_button("Add to Queue", "plus")
        self.add_btn.clicked.connect(self._submit_urls)
        self.clear_btn = ghost_button("Clear", "external")
        self.clear_btn.clicked.connect(self.input.clear)
        add_col.addWidget(self.add_btn)
        add_col.addWidget(self.clear_btn)
        add_col.addStretch(1)
        intake_row.addLayout(add_col)
        run_row = QHBoxLayout()
        run_row.setSpacing(8)
        self.start_btn = success_button("Start", "play")
        self.start_btn.clicked.connect(self.start.emit)
        self.pause_btn = ghost_button("Pause", "pause")
        self.pause_btn.clicked.connect(self.pause.emit)
        self.stop_btn = ghost_button("Stop", "stop")
        self.stop_btn.clicked.connect(self.stop.emit)
        for b in (self.start_btn, self.pause_btn, self.stop_btn):
            run_row.addWidget(b)
        self.viewer_mode = False
        self.worker_label = QLabel("")  # viewer: what the controller is doing, instead of run buttons
        self.worker_label.setObjectName("Muted")
        self.worker_label.hide()
        run_row.addWidget(self.worker_label)
        run_wrap = QVBoxLayout()
        run_wrap.addLayout(run_row)
        run_wrap.addStretch(1)
        intake_row.addLayout(run_wrap)
        root.addLayout(intake_row)

        # ---- progress
        prog = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximum(100)
        prog.addWidget(self.progress_bar, 1)
        self.progress_pct = QLabel("")
        self.progress_pct.setStyleSheet("font-size: 12.5px;")
        self.progress_pct.setMinimumWidth(120)
        self.progress_pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        prog.addWidget(self.progress_pct)
        root.addLayout(prog)

        # ---- stats: coloured circle icons, coloured labels
        stats = QHBoxLayout()
        stats.setSpacing(42)
        for key, label, glyph, role in STAT_ROLES:
            cell = QHBoxLayout()
            cell.setSpacing(9)
            ic = QLabel()
            ic.setPixmap(circle_icon(glyph, _role_color(role), 28))
            ic.setFixedSize(28, 28)
            name = QLabel(label)
            name.setStyleSheet(f"color: {_role_color(role)}; font-size: 14px; font-weight: 500;")
            value = QLabel("0")
            value.setStyleSheet("font-size: 14px; font-weight: 500;")
            self._stat_labels[key] = value
            cell.addWidget(ic)
            cell.addWidget(name)
            cell.addWidget(value)
            stats.addLayout(cell)
        stats.addStretch(1)
        root.addLayout(stats)

        # ---- filter chips
        frow = QHBoxLayout()
        frow.setSpacing(8)
        for label, key in FILTERS:
            btn = chip_button(label)
            btn.setChecked(key is None)
            btn.clicked.connect(lambda checked=False, k=key, b=btn: self._set_filter(k, b))
            self._filter_buttons[label] = btn
            frow.addWidget(btn)
        frow.addStretch(1)
        root.addLayout(frow)

        self.selection_bar = QFrame()
        self.selection_bar.setObjectName("SelectionBar")
        sb = QHBoxLayout(self.selection_bar)
        sb.setContentsMargins(12, 5, 12, 5)
        self.selection_label = QLabel("")
        sb.addWidget(self.selection_label)
        sb.addStretch(1)
        self.regen_btn = ghost_button(C.QUEUE_REGENERATE, "refresh")
        self.regen_btn.setToolTip("Rebuild documents that were deleted from disk, from the data stored in the app. "
                                  "Files that still exist are never overwritten.")
        self.regen_btn.clicked.connect(lambda: self.sync_files.emit(self.selected_job_ids()))
        self.csv_btn = ghost_button(C.QUEUE_EXPORT_CSV)
        self.csv_btn.clicked.connect(lambda: self.export_csv_selected.emit(self.selected_job_ids()))
        self.applied_btn = ghost_button("Mark applied", "check")
        self.applied_btn.clicked.connect(lambda: self.mark_applied.emit(self.selected_job_ids()))
        self.clear_checks_btn = ghost_button("Clear")
        self.clear_checks_btn.clicked.connect(self._clear_checks)
        for b in (self.regen_btn, self.applied_btn, self.csv_btn, self.clear_checks_btn):
            sb.addWidget(b)
        self.selection_bar.hide()
        root.addWidget(self.selection_bar)

        # ---- table
        self.table_card = QFrame()
        self.table_card.setObjectName("TableCard")
        tc = QVBoxLayout(self.table_card)
        tc.setContentsMargins(6, 4, 6, 4)
        tc.setSpacing(0)
        self.table = _QueueTable(0, 8)
        self.table.setHorizontalHeaderLabels(["☐", "#", "Company", "Role Title", "Status", "Stage", "Source", "Actions"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)  # active row -> inspector
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.setItemDelegateForColumn(C_CHECK, _CheckDelegate(self._row_checked, self.table))
        self.table.check_toggled.connect(self._toggle_check)
        hv = self.table.horizontalHeader()
        hv.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hv.sectionClicked.connect(self._header_clicked)
        for col, mode, width in (
            (C_CHECK, QHeaderView.ResizeMode.Fixed, 38), (C_NUM, QHeaderView.ResizeMode.Fixed, 56),
            (C_COMPANY, QHeaderView.ResizeMode.Interactive, 190), (C_ROLE, QHeaderView.ResizeMode.Stretch, 0),
            (C_STATUS, QHeaderView.ResizeMode.Fixed, 108), (C_STAGE, QHeaderView.ResizeMode.Interactive, 130),
            (C_SOURCE, QHeaderView.ResizeMode.Interactive, 96), (C_ACTIONS, QHeaderView.ResizeMode.Fixed, 104),
        ):
            hv.setSectionResizeMode(col, mode)
            if width:
                self.table.setColumnWidth(col, width)
        self.table.itemSelectionChanged.connect(self._emit_selection)
        tc.addWidget(self.table)

        self.empty = QFrame()
        self.empty.setObjectName("EmptyState")
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(30, 40, 30, 40)
        el.setSpacing(6)
        self.empty_title = QLabel(C.QUEUE_EMPTY_TITLE)
        self.empty_title.setObjectName("EmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body = muted_label("Paste job URLs above and press Add to Queue.")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(self.empty_title)
        el.addWidget(body)
        tc.addWidget(self.empty)
        root.addWidget(self.table_card, 1)

        # ---- details strip (Job Details | Activity Log | Generated Files)
        self.strip = self._build_strip()
        root.addWidget(self.strip)

        pager = QHBoxLayout()
        self.page_label = muted_label("")
        pager.addWidget(self.page_label, 1)
        self.prev_page_btn = ghost_button(C.QUEUE_PAGE_PREV)
        self.prev_page_btn.clicked.connect(self._prev_page)
        self.next_page_btn = ghost_button(C.QUEUE_PAGE_NEXT)
        self.next_page_btn.clicked.connect(self._next_page)
        pager.addWidget(self.prev_page_btn)
        pager.addWidget(self.next_page_btn)
        self.pager = QWidget()
        self.pager.setLayout(pager)
        pager.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.pager)

        self.set_worker_state("idle")

    # -------------------------------------------------------------- details strip

    def _build_strip(self) -> QFrame:
        card = QFrame()
        card.setObjectName("SurfaceCard")
        card.setFixedHeight(170)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 2, 10, 8)
        self.strip_tabs = QTabWidget()
        lay.addWidget(self.strip_tabs)

        details = QWidget()
        g = QGridLayout(details)
        g.setContentsMargins(10, 8, 10, 0)
        g.setHorizontalSpacing(40)
        g.setVerticalSpacing(6)
        self._kv: dict[str, QLabel] = {}
        self._current_url = ""

        def cell(col: int, row: int, key: str, label: str) -> None:
            box = QVBoxLayout()
            box.setSpacing(0)
            box.addWidget(field_label(label))
            val = QLabel("—")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            val.setStyleSheet("font-weight: 600;")
            val.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            if key == "url":
                line = QHBoxLayout()
                line.setSpacing(6)
                line.addWidget(val)
                line.addWidget(copy_button("Copy job URL", lambda: self._current_url))
                line.addStretch(1)
                box.addLayout(line)
            else:
                box.addWidget(val, alignment=Qt.AlignmentFlag.AlignLeft)
            g.addLayout(box, row, col)
            self._kv[key] = val

        cell(0, 0, "company", "Company")
        cell(0, 1, "role", "Role Title")
        cell(0, 2, "source", "Source")
        cell(1, 0, "url", "Job URL")
        cell(1, 1, "status", "Status")
        cell(1, 2, "stage", "Stage")
        cell(2, 0, "salary", "Salary")
        self._kv["url"].setOpenExternalLinks(True)
        prog = QVBoxLayout()
        prog.setSpacing(2)
        top = QHBoxLayout()
        top.addWidget(field_label("Progress"), 1)
        self.strip_pct = QLabel("")
        self.strip_pct.setStyleSheet("font-weight: 650;")
        top.addWidget(self.strip_pct)
        prog.addLayout(top)
        self.strip_bar = QProgressBar()
        self.strip_bar.setTextVisible(False)
        self.strip_bar.setMaximum(100)
        prog.addWidget(self.strip_bar)
        for key, label in (("added", "Added"), ("updated", "Last update")):
            box = QVBoxLayout()
            box.setSpacing(0)
            box.addWidget(field_label(label))
            self._kv[key] = QLabel("—")
            self._kv[key].setStyleSheet("font-weight: 600;")
            box.addWidget(self._kv[key])
            prog.addLayout(box)
        g.addLayout(prog, 0, 2, 3, 1)
        g.setColumnStretch(0, 2)
        g.setColumnStretch(1, 3)
        g.setColumnStretch(2, 2)
        self.strip_tabs.addTab(details, "Job Details")

        self.strip_log = QPlainTextEdit()
        self.strip_log.setReadOnly(True)
        self.strip_log.setStyleSheet("QPlainTextEdit { border: none; background: transparent; }")
        self.strip_tabs.addTab(self.strip_log, "Activity Log")

        files = QWidget()
        self.strip_files = QHBoxLayout(files)
        self.strip_files.setContentsMargins(10, 12, 10, 0)
        self.strip_files.setSpacing(10)
        self.strip_tabs.addTab(files, "Generated Files")
        self._fill_strip(None)
        return card

    def _fill_strip(self, job: JobItem | None) -> None:
        if job is None:
            self._current_url = ""
            for v in self._kv.values():
                v.setText("—")
            self._kv["status"].setStyleSheet("font-weight: 600;")
            self.strip_pct.setText("")
            self.strip_bar.setValue(0)
            self.strip_log.setPlainText("Select a job to see its activity.")
            clear_layout(self.strip_files)
            self.strip_files.addWidget(muted_label("Select a job to see its generated files."))
            self.strip_files.addStretch(1)
            return
        url = job.origin_url or job.url
        self._current_url = url
        self._kv["company"].setText(job.company or "—")
        self._kv["role"].setText(job.role_title or "—")
        self._kv["source"].setText(job.source or "—")
        self._kv["salary"].setText(format_salary(job.salary_text, job.salary_min, job.salary_max,
                                                 job.salary_period, job.salary_currency) or "—")
        self._kv["url"].setText(f'<a style="color: {theme.current().accent};" href="{url}">{url}</a>')
        self._kv["status"].setText(job.status.value)
        self._kv["status"].setStyleSheet(theme.badge_style(theme.STATUS_KIND.get(job.status.value, "neutral")))
        self._kv["stage"].setText(job.stage or "—")
        pct = progress_pct(job)
        self.strip_pct.setText(f"{pct}%")
        self.strip_bar.setValue(pct)
        self._kv["added"].setText(format_local(job.created_at, "%b %d, %Y · %H:%M") or "—")
        self._kv["updated"].setText(format_local(job.updated_at, "%b %d, %Y · %H:%M") or "—")
        self.strip_log.setPlainText("\n".join(job.logs[-60:]) if job.logs else "No activity recorded yet.")
        clear_layout(self.strip_files)
        refs = [r for r in file_refs(job) if r.path]
        if not refs:
            self.strip_files.addWidget(muted_label("No files generated yet."))
        for ref in refs:
            b = ghost_button(ref.label + ("" if ref.exists else "  (missing)"), "external")
            b.setEnabled(ref.exists)
            b.clicked.connect(lambda _=False, p=ref.path: system.open_path(p))
            self.strip_files.addWidget(b)
        folder = output_folder(job)
        if folder:
            fb = ghost_button("Open folder", "folder-open")
            fb.setEnabled(folder.exists())
            fb.clicked.connect(lambda _=False, f=folder: system.open_folder(f))
            self.strip_files.addWidget(fb)
        self.strip_files.addStretch(1)

    # ------------------------------------------------------------------- intake

    @staticmethod
    def _add_shortcut_hint(edit) -> None:
        """Right-aligned "Ctrl+F" hint inside the search field (as in the product reference)."""
        lay = QHBoxLayout(edit)
        lay.setContentsMargins(0, 0, 12, 0)
        lay.addStretch(1)
        hint = QLabel("Ctrl+F")
        hint.setStyleSheet("color: #7C8699; font-size: 12px; background: transparent;")
        hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay.addWidget(hint)
        edit.setClearButtonEnabled(False)
        edit.setTextMargins(0, 0, 54, 0)
        edit.textChanged.connect(lambda t: hint.setVisible(not t))

    def focus_input(self) -> None:
        self.input.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _submit_urls(self) -> None:
        valid, _dups, _invalid = parse_url_list(self.input.toPlainText())
        if not valid:
            system.toast(self, "No valid job links found. Paste one URL per line.", "warn")
            return
        self.add_urls.emit(valid, self.dup_box.isChecked())

    def clear_input(self) -> None:
        self.input.clear()

    # ------------------------------------------------------------- compat/state

    def set_theme_light(self, light: bool) -> None:
        self._fill_strip(next((j for j in self._jobs if j.id == self.selected_job_id()), None))

    def set_api_connected(self, connected: bool) -> None:
        self._connected = connected

    def focus_search(self) -> None:
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def set_viewer_mode(self, on: bool) -> None:
        """A viewer cannot run generation: the run buttons give way to the controller's state."""
        self.viewer_mode = on
        for b in (self.start_btn, self.pause_btn, self.stop_btn):
            b.setVisible(not on)
        self.worker_label.setVisible(on)

    def set_worker_state(self, state: str) -> None:
        if self.viewer_mode:
            self.worker_label.setText(f"Controller: {state}")
            return
        running = state == "running"
        paused = state == "paused"
        self.start_btn.setText("Resume" if paused else "Start")
        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.stop_btn.setEnabled(running or paused)

    def filter_key(self):
        return self._filter

    def search_text(self) -> str:
        return self._search

    def page_offset(self) -> int:
        return self._page_offset

    def set_page(self, jobs: list[JobItem], total: int, all_jobs_for_stats: list[JobItem] | None = None,
                 counts: dict[str, int] | None = None) -> None:
        current = self.selected_job_id()
        self._jobs = list(jobs)
        self._page_total = total
        if all_jobs_for_stats is not None:
            self._all_jobs_for_stats = list(all_jobs_for_stats)
        self._counts = counts or _counts(self._all_jobs_for_stats or self._jobs)
        self._rebuild_table(current)

    def _job_id_at(self, row: int) -> str:
        item = self.table.item(row, C_COMPANY) if 0 <= row < self.table.rowCount() else None
        return (item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def selected_job_id(self) -> str:
        """The active row: the job the inspector and details strip follow."""
        sel = self.table.selectionModel()
        rows = [i.row() for i in sel.selectedRows()] if sel is not None else []
        return self._job_id_at(rows[0]) if rows else ""

    def selected_job_ids(self) -> list[str]:
        """Jobs for bulk actions: the checked rows, or just the active row when nothing is checked."""
        checked = [self._job_id_at(r) for r in range(self.table.rowCount())]
        checked = [j for j in checked if j and j in self._checked]
        if checked:
            return checked
        active = self.selected_job_id()
        return [active] if active else []

    def checked_job_ids(self) -> list[str]:
        return [j for j in (self._job_id_at(r) for r in range(self.table.rowCount())) if j in self._checked]

    def _row_checked(self, row: int) -> bool:
        return self._job_id_at(row) in self._checked

    def _toggle_check(self, row: int) -> None:
        job_id = self._job_id_at(row)
        if not job_id:
            return
        self._checked.symmetric_difference_update({job_id})
        self._checks_changed()

    def _clear_checks(self) -> None:
        self._checked.clear()
        self._checks_changed()

    def _checks_changed(self) -> None:
        n = len(self.checked_job_ids())
        self.selection_label.setText(C.QUEUE_SELECTION.format(n=n))
        self.selection_bar.setVisible(n >= 1)
        hdr = self.table.horizontalHeaderItem(C_CHECK)
        if hdr is not None:
            hdr.setText("☑" if n and n == self.table.rowCount() else "☐")
        self.table.viewport().update()

    def select_row(self, row: int) -> None:
        """Make ``row`` the active row (the checkboxes are untouched)."""
        self.table.clearSelection()
        self.table.selectRow(row)
        self.table.setCurrentCell(row, C_COMPANY, QItemSelectionModel.SelectionFlag.NoUpdate)
        self._emit_selection()

    def _on_search(self, text: str) -> None:
        self._search = text.strip().lower()
        self._page_offset = 0
        self.filters_changed.emit()

    def _set_filter(self, key, button: QPushButton) -> None:
        self._filter = key
        for btn in self._filter_buttons.values():
            btn.setChecked(btn is button)
        self._page_offset = 0
        self.filters_changed.emit()

    def _filter_menu(self) -> None:
        menu = QMenu(self)
        for label, key in FILTERS:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(key == self._filter)
            act.triggered.connect(lambda _=False, k=key, b=self._filter_buttons[label]: self._set_filter(k, b))
        menu.exec(self.filter_btn.mapToGlobal(self.filter_btn.rect().bottomLeft()))

    def _prev_page(self) -> None:
        self._page_offset = max(0, self._page_offset - self.PAGE_SIZE)
        self.filters_changed.emit()

    def _next_page(self) -> None:
        if self._page_offset + self.PAGE_SIZE < self._page_total:
            self._page_offset += self.PAGE_SIZE
            self.filters_changed.emit()

    def _header_clicked(self, col: int) -> None:
        if col != C_CHECK:
            return
        ids = {self._job_id_at(r) for r in range(self.table.rowCount())}
        if len(self.checked_job_ids()) < len(ids):
            self._checked |= ids
        else:
            self._checked -= ids
        self._checks_changed()

    # -------------------------------------------------------------------- table

    def _update_stats(self, counts: dict[str, int], total: int) -> None:
        for key, value in counts.items():
            if key in self._stat_labels:
                self._stat_labels[key].setText(str(value))
        finished = counts["done"] + counts["skipped"]
        pct = int(round(100 * finished / total)) if total else 0
        self.progress_bar.setValue(pct)
        self.progress_pct.setText(f"{finished} / {total}  ({pct}%)" if total else C.QUEUE_WAITING)
        chip_counts = {"All": total, "Queued": counts["remaining"], "Processing": counts["processing"],
                       "Needs JD": counts["needs"], "Done": counts["done"], "Skipped": counts["skipped"],
                       "Failed": counts["failed"]}
        for label, btn in self._filter_buttons.items():
            btn.setText(f"{label} ({chip_counts.get(label, 0)})")

    def _rebuild_table(self, current: str) -> None:
        jobs = self._jobs
        counts = self._counts
        total_all = sum(counts.get(k, 0) for k in ("done", "skipped", "needs", "failed", "processing", "remaining"))
        self._update_stats(counts, total_all)
        match_total = self._page_total
        pages = max(1, (match_total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page_num = (self._page_offset // self.PAGE_SIZE) + 1 if match_total else 1
        self.page_label.setText(C.QUEUE_PAGE_STATUS.format(page=page_num, pages=pages, total=match_total))
        self.pager.setVisible(match_total > self.PAGE_SIZE)
        self.prev_page_btn.setEnabled(self._page_offset > 0)
        self.next_page_btn.setEnabled(self._page_offset + self.PAGE_SIZE < match_total)

        if total_all == 0 or not jobs:
            self.table.hide()
            self.empty.show()
            self.empty_title.setText(C.QUEUE_EMPTY_TITLE if total_all == 0 else C.QUEUE_NO_MATCH_TITLE)
            self._fill_strip(None)
            self.selection_changed.emit("")
            return
        self.empty.hide()
        self.table.show()
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setRowCount(len(jobs))
        self.table.verticalHeader().setDefaultSectionSize(theme.row_height())
        self._checked &= {j.id for j in jobs}  # checks never outlive their rows
        for row, job in enumerate(jobs):
            self.table.setItem(row, C_CHECK, QTableWidgetItem(""))
            num = QTableWidgetItem(str(self._page_offset + row + 1))
            num.setForeground(QColor(theme.current().text_secondary))
            self.table.setItem(row, C_NUM, num)
            company = QTableWidgetItem(job.company or "—")
            company.setData(Qt.ItemDataRole.UserRole, job.id)
            company.setToolTip(job.company or job.url)
            self.table.setItem(row, C_COMPANY, company)
            title = QTableWidgetItem(job.role_title or "—")
            title.setToolTip(job.role_title or job.url)
            self.table.setItem(row, C_ROLE, title)
            self.table.setItem(row, C_STATUS, QTableWidgetItem(""))
            self.table.setCellWidget(row, C_STATUS, _wrap(badge(
                job.status.value, theme.STATUS_KIND.get(job.status.value, "neutral"))))
            stage = QTableWidgetItem(job.stage or "")
            stage.setToolTip(job.error or job.stage or "")
            self.table.setItem(row, C_STAGE, stage)
            self.table.setItem(row, C_SOURCE, QTableWidgetItem(job.source or ""))
            self.table.setItem(row, C_ACTIONS, QTableWidgetItem(""))
            self.table.setCellWidget(row, C_ACTIONS, self._actions(job))
        cur_row = next((r for r, j in enumerate(jobs) if j.id == current), -1)
        if cur_row >= 0:
            self.table.selectRow(cur_row)
            self.table.setCurrentCell(cur_row, C_COMPANY, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.table.blockSignals(False)
        self.table.setUpdatesEnabled(True)
        self._emit_selection()

    def _actions(self, job: JobItem) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        h.setSpacing(2)
        folder = output_folder(job)
        fb = row_action("folder-open", "Open output folder", "text")
        fb.setEnabled(bool(folder and folder.exists()))
        fb.clicked.connect(lambda _=False, f=folder: f and system.open_folder(f))
        resume = next((r for r in file_refs(job) if r.kind == "resume"), None)
        rb = row_action("doc", "Open resume", "text")
        rb.setEnabled(bool(resume and resume.exists))
        rb.clicked.connect(lambda _=False, p=resume.path if resume else "": system.open_path(p))
        mb = row_action("more", "More actions", "text")
        mb.clicked.connect(lambda _=False, j=job.id, b=mb: self._menu(j, b.mapToGlobal(b.rect().bottomLeft())))
        for b in (fb, rb, mb):
            h.addWidget(b)
        return w

    def _menu(self, job_id: str, global_pos) -> None:
        job = next((j for j in self._jobs if j.id == job_id), None)
        if job is None:
            return
        menu = QMenu(self)
        for ref in file_refs(job):
            a = menu.addAction(f"Open {ref.label}")
            a.setEnabled(ref.exists)
            a.triggered.connect(lambda _=False, p=ref.path: system.open_path(p))
        menu.addAction("Open job posting", lambda: system.open_url(job.origin_url or job.url))
        if job.status == JobStatus.DONE:
            restore = menu.addAction("Restore missing files", lambda: self.sync_files.emit([job_id]))
            restore.setEnabled(bool(job.resume_json) and any(r.path and not r.exists for r in file_refs(job)))
        folder = output_folder(job)
        if folder:
            menu.addAction("Copy output path", lambda: system.copy_text(str(folder)))
        menu.addSeparator()
        if job.status == JobStatus.DONE:
            act = menu.addAction("Mark as applied", lambda: self.mark_applied.emit([job_id]))
            act.setEnabled(not job.applied_at)
            menu.addAction("Show in Applications", lambda: self.show_in_applications.emit(job_id))
        if job.status == JobStatus.FAILED:
            menu.addAction("Retry", lambda: self.retry_job.emit(job_id))
        if job.status == JobStatus.SKIPPED and "company folder" in (job.skip_reason or "").lower():
            menu.addAction("Allow this company again && process", lambda: self.allow_duplicate.emit(job_id))
        if job.status in {JobStatus.QUEUED, JobStatus.NEEDS_JD, JobStatus.FAILED}:
            menu.addAction("Skip", lambda: self.skip_job.emit(job_id))
        if job.status in {JobStatus.QUEUED, JobStatus.NEEDS_JD, JobStatus.FAILED, JobStatus.SKIPPED}:
            menu.addAction("Remove from queue", lambda: self.remove_jobs.emit([job_id]))
        menu.exec(global_pos)

    def _context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is not None:
            job_id = self.table.item(item.row(), C_COMPANY).data(Qt.ItemDataRole.UserRole)
            self._menu(job_id, self.table.viewport().mapToGlobal(pos))

    def _emit_selection(self) -> None:
        self._checks_changed()
        cur = self.selected_job_id()
        self._fill_strip(next((j for j in self._jobs if j.id == cur), None))
        self.selection_changed.emit(cur)


def _wrap(widget: QWidget) -> QWidget:
    holder = QWidget()
    holder.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    lay = QHBoxLayout(holder)
    lay.setContentsMargins(6, 0, 4, 0)
    lay.addWidget(widget)
    lay.addStretch(1)
    return holder


def _counts(jobs: list[JobItem]) -> dict[str, int]:
    out = {"done": 0, "skipped": 0, "needs": 0, "failed": 0, "processing": 0, "remaining": 0}
    for j in jobs:
        if j.status == JobStatus.DONE:
            out["done"] += 1
        elif j.status == JobStatus.SKIPPED:
            out["skipped"] += 1
        elif j.status == JobStatus.NEEDS_JD:
            out["needs"] += 1
        elif j.status == JobStatus.FAILED:
            out["failed"] += 1
        elif j.status in {JobStatus.EXTRACTING, JobStatus.GENERATING, JobStatus.EXPORTING}:
            out["processing"] += 1
        elif j.status == JobStatus.QUEUED:
            out["remaining"] += 1
    return out
