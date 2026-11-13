"""Applications management: dense table, filters, bulk updates, quick actions + detail panel."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from PySide6.QtCore import QDate, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QStackedWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.application_stage import OUTCOME_LABELS, STAGE_ORDER, Outcome, Stage, outcome_from_value, stage_from_value
from core.applications import AppFilter, ApplicationError, ApplicationService, AppRow
from core.files import file_refs, output_folder
from core.timeutil import PRESETS, format_local, parse_iso, range_for, utc_now
from ui import system, theme
from ui.application_detail import ApplicationDetail
from ui.application_dialogs import ManualApplicationDialog, WhenDialog
from ui.dialogs import ask_yes_no
from ui.widgets import (
    badge,
    chip_button,
    company_delegate,
    ghost_button,
    kpi_card,
    muted_label,
    RowTable,
    page_title,
    primary_button,
    row_action,
    search_field,
)

COLUMNS = [
    ("Company / Role", "company"), ("Role", "role"), ("Docs", "docs"), ("Applied", "applied"),
    ("Stage", "stage"), ("Response", "response"), ("Last activity", "activity"), ("Next action", "next_action"),
    ("Follow-up", "next_action"), ("", None),
]
C_COMPANY, C_ROLE, C_DOCS, C_APPLIED, C_STAGE, C_RESP, C_ACT, C_NEXT, C_DUE, C_ACTIONS = range(10)
COL_WIDTHS = {C_COMPANY: 240, C_DOCS: 80, C_APPLIED: 110, C_STAGE: 140, C_RESP: 124, C_ACT: 90, C_NEXT: 190,
              C_DUE: 90, C_ACTIONS: 112}
ROW_HEIGHT = 54
HIDE_ORDER = (C_DOCS, C_DUE, C_ACT, C_NEXT)  # dropped first when the pane is narrow

CHIPS = [("all", "All"), ("not_applied", "Not applied"), ("applied", "Applied"), ("recruiter_contact", "Recruiter"),
         ("screening", "Screening"), ("technical", "Technical"), ("final", "Final"), ("offer", "Offer"),
         ("closed", "Closed")]

RESPONSE_FILTERS = [("", "Any response"), ("waiting", "Waiting"), ("no_response", "No response (ghosted)"),
                    ("replied", "Replied"), ("positive", "Positive"), ("negative", "Negative")]
RANGE_PRESETS = [("", "Any time")] + [p for p in PRESETS if p[0] != "all"]


def relative_time(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "—"
    delta = now - dt
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    if delta < timedelta(days=14):
        return f"{delta.days}d ago"
    return format_local(dt, "%b %d" if dt.year == now.year else "%b %d, %Y")


class ApplicationsView(QWidget):
    selection_changed = Signal(str)
    open_requested = Signal(str)  # a row was clicked / activated: show its detail page
    open_queue_job = Signal(str)

    def __init__(self, service: ApplicationService, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ContentPane")
        self.service = service
        self._chip = "all"
        self._sort = "activity"
        self._desc = True
        self._rows: list[AppRow] = []
        self._chip_buttons: dict[str, QPushButton] = {}
        self._suspend = False
        self._dirty = True

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 0, 16, 10)
        root.setSpacing(10)

        head = QHBoxLayout()
        box = QVBoxLayout()
        box.setSpacing(2)
        box.addWidget(page_title("Applications"))
        box.addWidget(muted_label("Track every application, reply and interview. Generating documents never marks a "
                                  "job as applied."))
        head.addLayout(box, 1)
        self.range_combo = self._combo(RANGE_PRESETS)
        self.range_combo.setMinimumWidth(130)
        self.range_combo.setToolTip("Only show applications in this period (by the date chosen under Filters)")
        self.custom_from = self._date_edit(-30)
        self.custom_to = self._date_edit(0)
        self.custom_from.hide()
        self.custom_to.hide()
        self.add_btn = primary_button("Add Application", "plus")
        self.add_btn.setToolTip("Track a job you found or applied to outside the queue")
        self.add_btn.clicked.connect(self._add_application)
        for w in (self.custom_from, self.custom_to, self.range_combo, self.add_btn):
            head.addWidget(w, alignment=Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        kpis = QHBoxLayout()
        kpis.setSpacing(10)
        self._kpi: dict[str, tuple[QLabel, QLabel]] = {}
        for key, label, icon, kind in (
            ("total", "Total", "layers", "info"), ("applied", "Applied", "send", "info"),
            ("replied", "Replies", "mail", "violet"), ("interviewing", "Interviews", "calendar", "warning"),
            ("offers", "Offers", "award", "success"),
        ):
            card, value, sub = kpi_card(label, icon=icon, kind=kind)
            self._kpi[key] = (value, sub)
            kpis.addWidget(card)
        root.addLayout(kpis)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        for key, label in CHIPS:
            b = chip_button(label, compact=True)
            b.setChecked(key == "all")
            b.clicked.connect(lambda _=False, k=key: self._set_chip(k))
            self._chip_buttons[key] = b
            chips.addWidget(b)
        chips.addStretch(1)
        root.addLayout(chips)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = search_field("Search company or job title  (Ctrl+F)")
        self.search.setMinimumWidth(240)
        self.search.textChanged.connect(self._filters_changed)
        bar.addWidget(self.search, 1)
        self.filters_btn = ghost_button("Filters", "filter")
        self.filters_btn.setCheckable(True)
        self.filters_btn.toggled.connect(lambda on: self.filter_row.setVisible(on))
        bar.addWidget(self.filters_btn)
        self.archived_box = QCheckBox("Archived only")
        self.archived_box.toggled.connect(self._filters_changed)
        bar.addWidget(self.archived_box)
        root.addLayout(bar)
        self.filter_row = QFrame()
        self.filter_row.setObjectName("Card")
        bar2 = QHBoxLayout(self.filter_row)
        bar2.setContentsMargins(10, 8, 10, 8)
        bar2.setSpacing(8)
        self.response_combo = self._combo(RESPONSE_FILTERS)
        self.outcome_combo = self._combo([("", "Any outcome")] + [(o.value, o.label) for o in Outcome])
        self.date_field = self._combo([("applied", "Applied date"), ("activity", "Last activity")])
        for w in (self.response_combo, self.outcome_combo, self.date_field):
            bar2.addWidget(w)
        bar2.addStretch(1)
        reset = ghost_button("Reset")
        reset.clicked.connect(self._reset_filters)
        bar2.addWidget(reset)
        self.filter_row.hide()
        root.addWidget(self.filter_row)

        self.selection_bar = QFrame()
        self.selection_bar.setObjectName("SelectionBar")
        sb = QHBoxLayout(self.selection_bar)
        sb.setContentsMargins(10, 5, 10, 5)
        self.selection_label = QLabel("")
        sb.addWidget(self.selection_label)
        sb.addStretch(1)
        self.bulk_applied = ghost_button("Mark applied", "check")
        self.bulk_applied.clicked.connect(self._bulk_applied)
        self.bulk_stage = ghost_button("Move to stage")
        stage_menu = QMenu(self.bulk_stage)
        for st in STAGE_ORDER[1:]:
            stage_menu.addAction(st.label, lambda st=st: self._bulk_stage(st))
        self.bulk_stage.setMenu(stage_menu)
        self.bulk_outcome = ghost_button("Close as")
        out_menu = QMenu(self.bulk_outcome)
        for o in Outcome:
            out_menu.addAction(o.label, lambda o=o: self._bulk_outcome(o))
        self.bulk_outcome.setMenu(out_menu)
        self.bulk_archive = ghost_button("Archive", "archive")
        self.bulk_archive.clicked.connect(self._bulk_archive)
        for b in (self.bulk_applied, self.bulk_stage, self.bulk_outcome, self.bulk_archive):
            sb.addWidget(b)
        self.selection_bar.hide()
        root.addWidget(self.selection_bar)

        card = QFrame()
        card.setObjectName("TableCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(4, 2, 4, 2)
        self.table = RowTable(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([c[0] for c in COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        hh = self.table.horizontalHeader()
        hh.setSectionsClickable(True)
        hh.setSortIndicatorShown(True)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.sectionClicked.connect(self._header_clicked)
        hh.setStretchLastSection(False)
        for col in range(len(COLUMNS)):
            fixed = col in (C_DOCS, C_APPLIED, C_ACT, C_DUE, C_ACTIONS)
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed if fixed else QHeaderView.ResizeMode.Interactive)
            if col in COL_WIDTHS:
                self.table.setColumnWidth(col, COL_WIDTHS[col])
        hh.setSectionResizeMode(C_COMPANY, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnHidden(C_ROLE, True)  # role is the second line of the company cell
        hh.setMinimumSectionSize(60)
        self.table.setItemDelegateForColumn(C_COMPANY, company_delegate(self.table))
        self.table.itemSelectionChanged.connect(self._selection)
        self.table.clicked.connect(self._row_clicked)
        self.table.itemActivated.connect(lambda _i: self._open_selected())
        cl.addWidget(self.table)

        self.empty = QFrame()
        self.empty.setObjectName("EmptyState")
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(30, 40, 30, 40)
        self.empty_title = QLabel("No applications yet")
        self.empty_title.setObjectName("EmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_body = muted_label("Jobs appear here automatically once their documents are generated.")
        self.empty_body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(self.empty_title)
        el.addWidget(self.empty_body)
        cl.addWidget(self.empty)
        root.addWidget(card, 1)
        self.count_label = muted_label("")
        root.addWidget(self.count_label)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        service.subscribe(self.request_refresh)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_columns()
        QTimer.singleShot(0, self._fit_columns)  # the table viewport settles after the layout pass

    def _fit_columns(self) -> None:
        """Drop low-priority columns when the pane is too narrow so nothing is clipped."""
        avail = self.table.viewport().width()
        hidden: set[int] = set()

        def need() -> int:
            return sum(w for c, w in COL_WIDTHS.items() if c not in hidden)

        for col in HIDE_ORDER:
            if need() <= avail:
                break
            hidden.add(col)
        for col in HIDE_ORDER:
            self.table.setColumnHidden(col, col in hidden)

    # ------------------------------------------------------------------ helpers

    def _combo(self, items) -> QComboBox:
        box = QComboBox()
        for value, label in items:
            box.addItem(label, value)
        box.currentIndexChanged.connect(self._filters_changed)
        return box

    def _date_edit(self, offset_days: int) -> QDateEdit:
        edit = QDateEdit(QDate.currentDate().addDays(offset_days))
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("yyyy-MM-dd")
        edit.dateChanged.connect(self._filters_changed)
        return edit

    def focus_search(self) -> None:
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def request_refresh(self) -> None:
        if not self.isVisible():
            self._dirty = True  # refreshed when shown; hidden views cost nothing while jobs are processing
            return
        self._timer.start(0)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._fit_columns)
        if self._dirty:
            self._dirty = False
            self.refresh()

    def selected_ids(self) -> list[str]:
        out = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), C_COMPANY)
            if item:
                out.append(item.data(Qt.ItemDataRole.UserRole))
        return out

    def selected_id(self) -> str:
        ids = self.selected_ids()
        return ids[0] if ids else ""

    def select_job(self, job_id: str) -> None:
        for r in range(self.table.rowCount()):
            if self.table.item(r, C_COMPANY).data(Qt.ItemDataRole.UserRole) == job_id:
                self.table.selectRow(r)
                self.open_requested.emit(job_id)
                return

    def _row_clicked(self, index) -> None:
        mods = QGuiApplication.keyboardModifiers()
        if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return  # extending a multi-selection for bulk actions
        if len(self.selected_ids()) == 1 and index.column() != C_ACTIONS:
            self._open_selected()

    def _open_selected(self) -> None:
        job_id = self.selected_id()
        if job_id:
            self.open_requested.emit(job_id)

    def _add_application(self) -> None:
        ManualApplicationDialog(parent=self).ask(self._add_application_done)

    def _add_application_done(self, values: dict) -> None:
        job_id = self._safe(self.service.add_manual, **values)
        if job_id:
            system.toast(self, "Application added", "success")
            self.select_job(job_id)

    # ------------------------------------------------------------------ filters

    def _set_chip(self, key: str) -> None:
        self._chip = key
        for k, b in self._chip_buttons.items():
            b.setChecked(k == key)
        self._filters_changed()

    def _filters_changed(self, *_a) -> None:
        if self._suspend:
            return
        self.bulk_archive.setText("Restore" if self.archived_box.isChecked() else "Archive")
        active = sum(1 for c in (self.response_combo, self.outcome_combo) if c.currentData())
        self.filters_btn.setText(f"Filters ({active})" if active else "Filters")
        custom = self.range_combo.currentData() == "custom"
        self.custom_from.setVisible(custom)
        self.custom_to.setVisible(custom)
        self.refresh()

    def _reset_filters(self) -> None:
        self._suspend = True
        for c in (self.response_combo, self.outcome_combo, self.range_combo):
            c.setCurrentIndex(0)
        self._suspend = False
        self._filters_changed()

    def current_filter(self) -> AppFilter:
        flt = AppFilter(search=self.search.text(), response=self.response_combo.currentData() or "",
                        date_field=self.date_field.currentData(), include_archived=False,
                        only_archived=self.archived_box.isChecked())
        if self._chip == "not_applied":
            flt.applied = False
            flt.outcome = "active"
        elif self._chip == "closed":
            flt.outcome = self.outcome_combo.currentData() or ""
        elif self._chip != "all":
            flt.stage = Stage(self._chip)
            flt.outcome = "active"
        if self.outcome_combo.currentData() and self._chip in ("all", "closed"):
            flt.outcome = self.outcome_combo.currentData()
        elif self._chip == "closed" and not flt.outcome:
            flt.outcome = "any_closed"
        preset = self.range_combo.currentData()
        if preset:
            custom = (self.custom_from.date().toPython(), self.custom_to.date().toPython())
            flt.date_range = range_for(preset, custom=custom)
        return flt

    # ------------------------------------------------------------------ refresh

    def refresh(self) -> None:
        flt = self.current_filter()
        closed_any = flt.outcome == "any_closed"
        if closed_any:
            flt.outcome = ""
        rows = self.service.list_applications(flt, sort=self._sort, descending=self._desc)
        if closed_any:
            rows = [r for r in rows if r.job.outcome]
        self._rows = rows
        self._update_overview()
        keep = set(self.selected_ids())
        now = utc_now()
        bar = self.table.verticalScrollBar().value()
        self.table.setUpdatesEnabled(False)
        blocker = QSignalBlocker(self.table)
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        for i, row in enumerate(rows):
            self._fill_row(i, row, now)
        blocker.unblock()
        self.table.setUpdatesEnabled(True)
        for i, row in enumerate(rows):
            if row.job.id in keep:
                self.table.selectRow(i)
        self.table.verticalScrollBar().setValue(bar)
        self.table.horizontalHeader().setSortIndicator(
            next((i for i, c in enumerate(COLUMNS) if c[1] == self._sort), -1),
            Qt.SortOrder.DescendingOrder if self._desc else Qt.SortOrder.AscendingOrder)
        self.table.setVisible(bool(rows))
        self.empty.setVisible(not rows)
        if not rows:
            filtered = bool(self.search.text() or self._chip != "all" or flt.date_range or flt.response)
            self.empty_title.setText("No matching applications" if filtered else "No applications yet")
        self.count_label.setText(f"{len(rows)} application{'s' if len(rows) != 1 else ''}")
        self._selection()

    def _update_overview(self) -> None:
        everything = self.service.list_applications(AppFilter(include_archived=False))
        total = len(everything)
        applied = [r for r in everything if r.job.applied_at]
        replied = [r for r in applied if r.job.response_status in ("positive", "neutral", "negative")]
        interviewing = [r for r in everything if r.job.app_stage in ("screening", "technical", "final")
                        and not r.job.outcome]
        offers = [r for r in everything if r.job.app_stage == "offer" and r.job.outcome != "rejected"]
        ready = [r for r in everything if not r.job.applied_at and not r.job.outcome]
        waiting = sum(1 for r in applied if r.response_label == "Waiting")
        up = sum(r.upcoming_interviews for r in everything)
        data = {
            "total": (total, f"{len(ready)} to apply"),
            "applied": (len(applied), f"{waiting} waiting"),
            "replied": (len(replied), f"{len(replied) * 100 // len(applied)}% of applied" if applied else "no data"),
            "interviewing": (len(interviewing), f"{up} upcoming"),
            "offers": (len(offers), ""),
        }
        for key, (n, sub) in data.items():
            self._kpi[key][0].setText(str(n))
            self._kpi[key][1].setText(sub)
        counts = {"all": total, "not_applied": len(ready), "closed": sum(1 for r in everything if r.job.outcome)}
        for st in STAGE_ORDER[1:]:
            counts[st.value] = sum(1 for r in everything if r.job.app_stage == st.value and not r.job.outcome)
        for key, label in CHIPS:
            self._chip_buttons[key].setText(f"{label}  {counts.get(key, 0)}")

    def _fill_row(self, i: int, row: AppRow, now: datetime) -> None:
        job = row.job
        c = theme.current()

        def text(col: int, value: str, tip: str = "", color: str = "") -> None:
            item = QTableWidgetItem(value)
            item.setToolTip(tip or value)
            if color:
                item.setForeground(self._qcolor(color))
            self.table.setItem(i, col, item)

        text(C_COMPANY, job.company or "—")
        self.table.item(i, C_COMPANY).setData(Qt.ItemDataRole.UserRole, job.id)
        self.table.item(i, C_COMPANY).setData(Qt.ItemDataRole.UserRole + 1, job.role_title or "")
        text(C_ROLE, job.role_title or "—")
        for col in (C_DOCS, C_STAGE, C_RESP):
            self.table.setItem(i, col, QTableWidgetItem(""))
        self._cell_badges(i, C_DOCS, [("Ready", "success")] if row.docs_ok else [("Missing", "warning")])
        applied_on = parse_iso(job.applied_at)
        if applied_on:
            text(C_APPLIED, format_local(applied_on, "%b %d, %Y"), "Applied " + format_local(applied_on))
        else:
            text(C_APPLIED, "Not applied", "Documents generated; not yet submitted", c.text_secondary)
        stage = stage_from_value(job.app_stage)
        out = outcome_from_value(job.outcome)
        if out:  # a closed application shows its outcome; the stage it reached is in the tooltip
            self._cell_badges(i, C_STAGE, [(out.label, theme.OUTCOME_KIND[out.value])])
            self.table.item(i, C_STAGE).setToolTip(f"{out.label} at stage: {stage.label}")
        else:
            self._cell_badges(i, C_STAGE, [(stage.label, theme.STAGE_KIND[stage.value])])
        resp_kind = {"Waiting": "neutral", "No response": "warning", "Replied": "success", "Negative reply": "danger",
                     "On hold": "violet"}.get(row.response_label)
        self._cell_badges(i, C_RESP, [(row.response_label, resp_kind)] if resp_kind else [])
        last = parse_iso(job.last_activity_at)
        text(C_ACT, relative_time(last, now), format_local(last) if last else "")
        nxt = row.next_action
        text(C_NEXT, nxt["description"] if nxt else "—")
        due = (nxt or {}).get("due_date", "")
        overdue = bool(due and due < date.today().isoformat())
        shown = date.fromisoformat(due).strftime("%b %d") if due else "—"
        text(C_DUE, shown, ("Overdue: " if overdue else "Due ") + due if due else "", c.danger if overdue else "")
        self.table.setCellWidget(i, C_ACTIONS, self._actions_widget(row))

    @staticmethod
    def _qcolor(hex_color: str):
        from PySide6.QtGui import QColor

        return QColor(hex_color)

    def _cell_badges(self, i: int, col: int, items: list[tuple[str, str]]) -> None:
        w = QWidget()
        w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        h = QHBoxLayout(w)
        h.setContentsMargins(6, 0, 4, 0)
        h.setSpacing(4)
        for label, kind in items:
            h.addWidget(badge(label, kind, dot=col in (C_STAGE, C_RESP)))
        h.addStretch(1)
        self.table.setCellWidget(i, col, w)

    def _actions_widget(self, row: AppRow) -> QWidget:
        job = row.job
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        h.setSpacing(0)
        mark = row_action("check", "Mark as applied", "success")
        mark.setEnabled(not job.applied_at and not job.outcome)
        mark.clicked.connect(lambda _=False, j=job.id: self._safe(self.service.mark_applied, j))
        stage = row_action("chevron", "Change stage / outcome", "text")
        stage.clicked.connect(lambda _=False, j=job.id, b=stage: self._stage_menu(j, b))
        folder = row_action("folder-open", "Open documents folder", "text")
        fold = output_folder(job)
        folder.setEnabled(bool(fold and fold.exists()))
        folder.clicked.connect(lambda _=False, f=fold: f and system.open_folder(f))
        more = row_action("more", "More: open resume / PDF / cover letter / job posting, notes, archive", "text")
        more.clicked.connect(lambda _=False, j=job.id, b=more: self._more_menu(j, b))
        for b in (mark, stage, folder, more):
            h.addWidget(b)
        return w

    # -------------------------------------------------------------------- menus

    def _safe(self, fn, *a, **k):
        try:
            return fn(*a, **k)
        except ApplicationError as exc:
            system.toast(self, str(exc), "warn")

    def _stage_menu(self, job_id: str, anchor: QToolButton) -> None:
        job = self.service.db.get(job_id)
        if job is None:
            return
        menu = QMenu(self)
        cur = stage_from_value(job.app_stage)
        for st in STAGE_ORDER[1:]:
            act = menu.addAction(("✓ " if st == cur and job.applied_at else "") + st.label)
            act.triggered.connect(lambda _=False, st=st: self._safe(self.service.set_stage, job_id, st))
        menu.addSeparator()
        if job.outcome:
            menu.addAction("Reopen application", lambda: self._safe(self.service.reopen, job_id))
        else:
            for o in Outcome:
                menu.addAction(o.label, lambda o=o: self._outcome_dialog(job_id, o))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _outcome_dialog(self, job_id: str, outcome: Outcome) -> None:
        WhenDialog(OUTCOME_LABELS[outcome], note="", note_label="Note (optional)", parent=self).ask(
            lambda v: self._safe(self.service.set_outcome, job_id, outcome, v["when"], v.get("note", "")))

    def _more_menu(self, job_id: str, anchor) -> None:
        self._build_more_menu(job_id).exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _build_more_menu(self, job_id: str) -> QMenu:
        job = self.service.db.get(job_id)
        menu = QMenu(self)
        if job is None:
            return menu
        for ref in file_refs(job):
            act = menu.addAction(f"Open {ref.label}")
            act.setEnabled(ref.exists)
            act.triggered.connect(lambda _=False, p=ref.path: system.open_path(p))
        folder = output_folder(job)
        menu.addAction("Copy output path", lambda: folder and system.copy_text(str(folder)))
        menu.addAction("Open job posting", lambda: system.open_url(job.origin_url or job.url))
        menu.addSeparator()
        menu.addAction("Mark applied on a date…", lambda: self._applied_on(job_id)).setEnabled(not job.applied_at)
        menu.addAction("Add note…", lambda: self._note_dialog(job_id))
        menu.addAction("Show in Job Queue", lambda: self.open_queue_job.emit(job_id))
        menu.addSeparator()
        if job.archived_at:
            menu.addAction("Restore from archive", lambda: self._safe(self.service.archive, [job_id], False))
        else:
            menu.addAction("Archive", lambda: self._safe(self.service.archive, [job_id]))
        return menu

    def _context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        job_id = self.table.item(item.row(), C_COMPANY).data(Qt.ItemDataRole.UserRole)
        self._build_more_menu(job_id).exec(self.table.viewport().mapToGlobal(pos))

    def _applied_on(self, job_id: str) -> None:
        WhenDialog("Mark as applied", note="", note_label="Note (optional)", parent=self).ask(
            lambda v: self._safe(self.service.mark_applied, job_id, v["when"], v.get("note", "")))

    def _note_dialog(self, job_id: str) -> None:
        WhenDialog("Add note", note="", note_label="Note", parent=self).ask(
            lambda v: v.get("note") and self._safe(self.service.add_note, job_id, v["note"], v["when"]))

    # ------------------------------------------------------------ sort / select

    def _header_clicked(self, col: int) -> None:
        key = COLUMNS[col][1]
        if not key:
            return
        if key == self._sort:
            self._desc = not self._desc
        else:
            self._sort, self._desc = key, key in ("activity", "applied")
        self.refresh()

    def _selection(self) -> None:
        ids = self.selected_ids()
        n = len(ids)
        self.selection_bar.setVisible(n > 1)
        self.selection_label.setText(f"{n} selected")
        self.selection_changed.emit(ids[0] if n == 1 else (ids[0] if n else ""))

    # --------------------------------------------------------------------- bulk

    def _report(self, verb: str, done: int, skipped: int) -> None:
        msg = f"{verb} {done} application{'s' if done != 1 else ''}"
        if skipped:
            msg += f"; skipped {skipped} that could not be changed safely"
        system.toast(self, msg, "success" if done else "warn")

    def _bulk_applied(self) -> None:
        result = self._safe(self.service.bulk_mark_applied, self.selected_ids())
        if result is not None:
            self._report("Marked", *result)

    def _bulk_stage(self, stage: Stage) -> None:
        result = self._safe(self.service.bulk_set_stage, self.selected_ids(), stage)
        if result is not None:
            self._report("Moved", *result)

    def _bulk_outcome(self, outcome: Outcome) -> None:
        ids = self.selected_ids()
        if ask_yes_no(self, "Close applications", f"Mark {len(ids)} application(s) as {outcome.label}?"):
            result = self._safe(self.service.bulk_set_outcome, ids, outcome)
            if result is not None:
                self._report("Closed", *result)

    def _bulk_archive(self) -> None:
        restore = self.archived_box.isChecked()
        n = self._safe(self.service.archive, self.selected_ids(), archived=not restore)
        if n is not None:
            system.toast(self, f"{'Restored' if restore else 'Archived'} {n} application(s)", "success")


class ApplicationsPage(QWidget):
    """List page and full-page application details, switched with a stack."""

    def __init__(self, service: ApplicationService, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Workspace")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.stack = QStackedWidget()
        self.view = ApplicationsView(service)
        self.detail = ApplicationDetail(service)
        self.detail_host = self.detail  # the page that is shown while an application is open
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.detail)
        lay.addWidget(self.stack)
        self.view.open_requested.connect(self._open)
        self.detail.closed.connect(self._back)

    def _open(self, job_id: str) -> None:
        self.detail.show_job(job_id)
        self.stack.setCurrentWidget(self.detail)

    def _back(self) -> None:
        self.stack.setCurrentWidget(self.view)
        self.view.table.clearSelection()
        self.view.refresh()

    def show_list(self) -> None:
        if self.stack.currentWidget() is not self.view:
            self._back()

    def refresh(self) -> None:
        self.view.refresh()
        self.detail.refresh()

    def focus_search(self) -> None:
        self.view.focus_search()
