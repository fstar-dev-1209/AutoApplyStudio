"""Application detail inspector: overview, timeline, messages, interviews."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.application_stage import OUTCOME_LABELS, STAGE_ORDER, Outcome, Stage, outcome_from_value, stage_from_value
from core.applications import (
    INTERVIEW_KINDS,
    MESSAGE_KINDS,
    ApplicationError,
    ApplicationService,
)
from core.files import file_refs, output_folder
from core.lifecycle import EV
from core.salary import format_salary
from core.timeutil import format_in_zone, format_local, parse_iso, utc_now
from ui import icons, system, theme
from ui.application_dialogs import InterviewDialog, MessageDialog, WhenDialog
from ui.dialogs import ask_yes_no, show_warning
from ui.widgets import (
    avatar_pixmap,
    badge,
    clear_layout,
    field_label,
    ghost_button,
    muted_label,
    page_title,
    primary_button,
    row_action,
    section_title,
    set_icon,
    circle_icon,
    copy_button,
)

_MSG_KIND = dict(MESSAGE_KINDS)
_IV_KIND = dict(INTERVIEW_KINDS)


class ApplicationDetail(QWidget):
    """Full-page application view: header card, History / Messages / Interviews / Notes, side cards."""

    closed = Signal()  # "Back to Applications"

    def __init__(self, service: ApplicationService, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ContentPane")
        self.service = service
        self.job_id = ""
        self._building = False

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 0, 16, 12)
        root.setSpacing(12)

        self.empty = QFrame()
        self.empty.setObjectName("EmptyState")
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(20, 36, 20, 36)
        title = QLabel("No application selected")
        title.setObjectName("EmptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body = muted_label("Select a row to review its timeline, messages and interviews.")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(title)
        el.addWidget(body)
        root.addWidget(self.empty)

        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(12)

        # ---- page title + back link
        top = QHBoxLayout()
        tbox = QVBoxLayout()
        tbox.setSpacing(2)
        tbox.addWidget(page_title("Application Details"))
        self.back_btn = QPushButton("  Back to Applications")
        self.back_btn.setObjectName("LinkButton")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        set_icon(self.back_btn, "chevron-left", "accent", 14)
        self.back_btn.clicked.connect(self.closed.emit)
        tbox.addWidget(self.back_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        top.addLayout(tbox, 1)
        bl.addLayout(top)

        # ---- header card
        hero = QFrame()
        hero.setObjectName("Card")
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(18, 14, 18, 14)
        hl.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(12)
        self.avatar = QLabel()
        self.avatar.setFixedSize(46, 46)
        head.addWidget(self.avatar, alignment=Qt.AlignmentFlag.AlignVCenter)
        head_text = QVBoxLayout()
        head_text.setSpacing(1)
        self.company = QLabel("")
        self.company.setObjectName("PageTitle")
        self.company.setStyleSheet("font-size: 18px;")
        self.role = muted_label("")
        head_text.addWidget(self.company)
        head_text.addWidget(self.role)
        head.addLayout(head_text, 1)
        self.badges = QHBoxLayout()
        self.badges.setSpacing(6)
        head.addLayout(self.badges)
        self.stage_btn = primary_button("Update Stage", "trend")
        self.stage_menu = QMenu(self.stage_btn)
        self.stage_menu.aboutToShow.connect(self._fill_stage_menu)
        self.stage_btn.setMenu(self.stage_menu)
        head.addSpacing(8)
        head.addWidget(self.stage_btn)
        hl.addLayout(head)
        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.applied_meta = self._meta_chip(meta, "calendar")
        meta.addSpacing(18)
        self.updated_meta = self._meta_chip(meta, "clock")
        meta.addStretch(1)
        hl.addLayout(meta)
        bl.addWidget(hero)

        # ---- content: tabs (left) + side cards (right)
        content = QHBoxLayout()
        content.setSpacing(12)
        tabs_card = QFrame()
        tabs_card.setObjectName("Card")
        tcl = QVBoxLayout(tabs_card)
        tcl.setContentsMargins(14, 4, 14, 10)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("Segmented")
        self.tabs.tabBar().setUsesScrollButtons(False)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.addTab(self._build_timeline(), "History")
        self.tabs.addTab(self._build_messages(), "Messages")
        self.tabs.addTab(self._build_interviews(), "Interviews")
        self.tabs.addTab(self._build_notes(), "Notes")
        tcl.addWidget(self.tabs)
        content.addWidget(tabs_card, 1)

        side = QWidget()
        side.setFixedWidth(360)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(12)
        sl.addWidget(self._build_status_card())
        sl.addWidget(self._build_action_card())
        sl.addWidget(self._build_info_card())
        sl.addWidget(self._build_docs_card())
        sl.addStretch(1)
        content.addWidget(self._scroll(side, 376))
        bl.addLayout(content, 1)
        root.addWidget(self.body, 1)
        self.body.hide()

        service.subscribe(self._on_change)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)

    # --------------------------------------------------------------- plumbing

    @staticmethod
    def _meta_chip(row: QHBoxLayout, icon_name: str) -> QLabel:
        ic = QLabel()
        ic.setPixmap(icons.icon(icon_name, theme.current().text_secondary, 16).pixmap(16, 16))
        label = QLabel("")
        row.addWidget(ic)
        row.addWidget(label)
        return label

    @staticmethod
    def _scroll(widget: QWidget, width: int = 0) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(widget)
        if width:
            area.setFixedWidth(width)
        return area

    @staticmethod
    def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(9)
        lay.addWidget(section_title(title))
        return card, lay

    def _on_change(self) -> None:
        if self.job_id and self.isVisible():
            self._timer.start(0)  # defer: the sender widget may be rebuilt by this refresh

    def _safe(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ApplicationError as exc:
            show_warning(self, "Application", str(exc))
        return None

    def clear(self) -> None:
        self.job_id = ""
        self.body.hide()
        self.empty.show()

    def show_job(self, job_id: str) -> None:
        if not job_id:
            self.clear()
            return
        self.job_id = job_id
        self.empty.hide()
        self.body.show()
        self.refresh()

    # ------------------------------------------------------------------- cards

    def _build_status_card(self) -> QFrame:
        card, lay = self._card("Status")
        row = QHBoxLayout()
        self.applied_label = QLabel("")
        self.applied_btn = primary_button("Mark applied", "check")
        self.applied_btn.clicked.connect(lambda: self._safe(self.service.mark_applied, self.job_id))
        self.applied_date_btn = row_action("calendar", "Mark applied on a specific date…", "text")
        self.applied_date_btn.clicked.connect(self._mark_applied_on)
        row.addWidget(self.applied_label, 1)
        row.addWidget(self.applied_btn)
        row.addWidget(self.applied_date_btn)
        lay.addLayout(row)

        rows = QGridLayout()
        rows.setHorizontalSpacing(10)
        rows.setVerticalSpacing(8)
        rows.setColumnStretch(1, 1)
        self.stage_combo = QComboBox()
        for st in STAGE_ORDER:
            self.stage_combo.addItem(st.label, st.value)
        self.stage_combo.activated.connect(self._stage_chosen)
        rows.addWidget(field_label("Stage"), 0, 0)
        rows.addWidget(self.stage_combo, 0, 1)
        self.outcome_btn = ghost_button("Set final outcome…")
        menu = QMenu(self.outcome_btn)
        for o in Outcome:
            menu.addAction(o.label, lambda o=o: self._choose_outcome(o))
        self.outcome_btn.setMenu(menu)
        self.reopen_btn = ghost_button("Reopen application")
        self.reopen_btn.clicked.connect(self._reopen)
        rows.addWidget(field_label("Outcome"), 1, 0)
        outcome_cell = QHBoxLayout()
        outcome_cell.addWidget(self.outcome_btn, 1)
        outcome_cell.addWidget(self.reopen_btn, 1)
        rows.addLayout(outcome_cell, 1, 1)
        self.response_label = QLabel("")
        self.hold_box = QCheckBox("On hold")
        self.hold_box.clicked.connect(lambda on: self._safe(self.service.set_hold, self.job_id, on))
        rows.addWidget(field_label("Response"), 2, 0)
        resp_cell = QHBoxLayout()
        resp_cell.addWidget(self.response_label, 1)
        resp_cell.addWidget(self.hold_box)
        rows.addLayout(resp_cell, 2, 1)
        lay.addLayout(rows)
        return card

    def _build_action_card(self) -> QFrame:
        card, lay = self._card("Next Action")
        self.action_text = QLineEdit()
        self.action_text.setPlaceholderText("e.g. Prepare for technical interview")
        self.action_text.returnPressed.connect(self._save_action)
        lay.addWidget(self.action_text)
        arow = QHBoxLayout()
        self.action_nodate = QCheckBox("No date")
        self.action_date = QDateEdit(QDate.currentDate().addDays(2))
        self.action_date.setCalendarPopup(True)
        self.action_date.setDisplayFormat("yyyy-MM-dd")
        self.action_nodate.toggled.connect(lambda on: self.action_date.setDisabled(on))
        arow.addWidget(field_label("Due"))
        arow.addWidget(self.action_date, 1)
        arow.addWidget(self.action_nodate)
        lay.addLayout(arow)
        brow = QHBoxLayout()
        self.action_save = ghost_button("Save", "edit")
        self.action_save.clicked.connect(self._save_action)
        self.action_done = ghost_button("Mark done", "check")
        self.action_done.clicked.connect(self._complete_action)
        brow.addWidget(self.action_save)
        brow.addWidget(self.action_done)
        brow.addStretch(1)
        lay.addLayout(brow)

        lay.addWidget(field_label("Follow-ups"))
        self.followups = QVBoxLayout()
        self.followups.setSpacing(4)
        lay.addLayout(self.followups)
        frow = QHBoxLayout()
        sent = ghost_button("Follow-up sent", "send")
        sent.clicked.connect(self._followup_sent)
        remind = ghost_button("Add reminder", "clock")
        remind.clicked.connect(self._add_reminder)
        frow.addWidget(sent)
        frow.addWidget(remind)
        frow.addStretch(1)
        lay.addLayout(frow)
        return card

    def _build_info_card(self) -> QFrame:
        card, lay = self._card("Application Info")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(1, 1)
        self.info_id = QLabel("")
        self.info_source = QLabel("")
        self.info_salary = QLabel("")
        self.info_folder = QLabel("")
        self.info_folder.setWordWrap(True)
        self.url_label = QLabel("")
        self.url_label.setObjectName("Link")
        self.url_label.setOpenExternalLinks(True)
        copy_url = copy_button("Copy job URL", self._job_url)
        url_cell = QHBoxLayout()
        url_cell.addWidget(self.url_label, 1)
        url_cell.addWidget(copy_url)
        for r, (name, w) in enumerate((("Application ID", self.info_id), ("Source", self.info_source),
                                       ("Salary", self.info_salary), ("Output folder", self.info_folder))):
            grid.addWidget(field_label(name), r, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(w, r, 1)
        grid.addWidget(field_label("Job URL"), 4, 0)
        grid.addLayout(url_cell, 4, 1)
        lay.addLayout(grid)
        return card

    def _build_docs_card(self) -> QFrame:
        card, lay = self._card("Documents")
        self.files_box = QVBoxLayout()
        self.files_box.setSpacing(6)
        lay.addLayout(self.files_box)
        return card

    def _build_notes(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 4)
        lay.setSpacing(8)
        lay.addWidget(muted_label("Private notes about this role. Timeline notes are added from History."))
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("Private notes about this role")
        lay.addWidget(self.notes, 1)
        nrow = QHBoxLayout()
        nrow.addStretch(1)
        save_notes = primary_button("Save notes", "check")
        save_notes.clicked.connect(lambda: self._safe(self.service.set_notes, self.job_id, self.notes.toPlainText()))
        nrow.addWidget(save_notes)
        lay.addLayout(nrow)
        return page

    def _job_url(self) -> str:
        job = self.service.db.get(self.job_id)
        return (job.origin_url or job.url) if job else ""

    def _fill_stage_menu(self) -> None:
        self.stage_menu.clear()
        job = self.service.db.get(self.job_id)
        if job is None:
            return
        cur = stage_from_value(job.app_stage)
        for st in STAGE_ORDER[1:]:
            act = self.stage_menu.addAction(("✓  " if st == cur and job.applied_at else "      ") + st.label)
            act.triggered.connect(lambda _=False, st=st: self._safe(self.service.set_stage, self.job_id, st))
        self.stage_menu.addSeparator()
        if job.outcome:
            self.stage_menu.addAction("Reopen application", self._reopen)
        else:
            for o in Outcome:
                self.stage_menu.addAction(o.label, lambda o=o: self._choose_outcome(o))

    def _stage_chosen(self, _index: int) -> None:
        if self._building:
            return
        stage = Stage(self.stage_combo.currentData())
        self._safe(self.service.set_stage, self.job_id, stage)
        self.refresh()  # restore the combo if the change was refused

    def _mark_applied_on(self) -> None:
        dlg = WhenDialog("Mark as applied", note="", note_label="Note (optional)", parent=self)
        dlg.ask(lambda v: self._safe(self.service.mark_applied, self.job_id, v["when"], v.get("note", "")))

    def _choose_outcome(self, outcome: Outcome) -> None:
        dlg = WhenDialog(OUTCOME_LABELS[outcome], note="", note_label="Note (optional)", parent=self)
        dlg.ask(lambda v: self._safe(self.service.set_outcome, self.job_id, outcome, v["when"], v.get("note", "")))

    def _reopen(self) -> None:
        self._safe(self.service.reopen, self.job_id)

    def _save_action(self) -> None:
        due = None if self.action_nodate.isChecked() else self.action_date.date().toPython()
        self._safe(self.service.set_next_action, self.job_id, self.action_text.text(), due)

    def _complete_action(self) -> None:
        open_actions = [a for a in self.service.list_actions(self.job_id, include_done=False)
                        if a["kind"] == "next_action"]
        if open_actions:
            self._safe(self.service.complete_action, open_actions[0]["id"])

    def _followup_sent(self) -> None:
        dlg = WhenDialog("Follow-up sent", note="", note_label="What was sent", parent=self)
        dlg.ask(lambda v: self._safe(self.service.log_followup_sent, self.job_id, v["when"], v.get("note", "")))

    def _add_reminder(self) -> None:
        dlg = WhenDialog("Follow-up reminder", note="", note_label="Reminder", parent=self)
        dlg.ask(lambda v: self._safe(
            self.service.add_action, self.job_id, v.get("note") or "Follow up", v["when"].astimezone().date(),
            "follow_up"))

    # ----------------------------------------------------------------- timeline

    def _build_timeline(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 4)
        lay.setSpacing(8)
        add = QHBoxLayout()
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Add a note to the timeline…")
        self.note_edit.returnPressed.connect(self._add_note)
        add_btn = ghost_button("Add", "note")
        add_btn.clicked.connect(self._add_note)
        add.addWidget(self.note_edit, 1)
        add.addWidget(add_btn)
        lay.addLayout(add)
        self.timeline = QListWidget()
        self.timeline.setWordWrap(True)
        self.timeline.itemDoubleClicked.connect(lambda _i: self._edit_entry())
        self.timeline.itemSelectionChanged.connect(self._timeline_selection)
        lay.addWidget(self.timeline, 1)
        bar = QHBoxLayout()
        self.show_voided = QCheckBox("Show removed")
        self.show_voided.toggled.connect(lambda _on: self._fill_timeline())
        self.entry_edit = ghost_button("Edit entry", "edit")
        self.entry_edit.clicked.connect(self._edit_entry)
        self.entry_void = ghost_button("Remove", "trash")
        self.entry_void.clicked.connect(self._void_entry)
        bar.addWidget(self.show_voided)
        bar.addStretch(1)
        bar.addWidget(self.entry_edit)
        bar.addWidget(self.entry_void)
        lay.addLayout(bar)
        lay.addWidget(muted_label("Entries are never deleted: edits and removals are recorded as corrections."))
        return page

    def _add_note(self) -> None:
        text = self.note_edit.text().strip()
        if text:
            self._safe(self.service.add_note, self.job_id, text)
            self.note_edit.clear()

    def _selected_entry(self):
        item = self.timeline.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _timeline_selection(self) -> None:
        entry = self._selected_entry()
        editable = entry is not None and entry["type"] not in (EV.IMPORTED,)
        self.entry_edit.setEnabled(editable and not entry["voided"])
        self.entry_void.setEnabled(editable)
        self.entry_void.setText("Restore" if entry and entry["voided"] else "Remove")

    def _edit_entry(self) -> None:
        entry = self._selected_entry()
        if not entry or entry["voided"] or entry["type"] == EV.IMPORTED:
            return
        dlg = WhenDialog("Edit timeline entry", entry["when"], note=entry["notes"], note_label="Notes",
                         reason=True, parent=self)
        dlg.ask(lambda v: self._safe(
            self.service.edit_event, entry["id"], occurred_at=v["when"], notes=v.get("note", ""),
            reason=v.get("reason", "")))

    def _void_entry(self) -> None:
        entry = self._selected_entry()
        if not entry:
            return
        if entry["voided"]:
            self._safe(self.service.restore_event, entry["id"])
        elif ask_yes_no(self, "Remove entry", f"Remove “{entry['label']}” from the timeline and from analytics?\n"
                        "It stays in the audit history and can be restored."):
            self._safe(self.service.void_event, entry["id"])

    _EVENT_STYLE = {
        EV.APPLIED: ("send", "info"), EV.STAGE_CHANGED: ("trend", "violet"), EV.MESSAGE: ("mail", "violet"),
        EV.INTERVIEW_SCHEDULED: ("calendar", "warning"), EV.INTERVIEW_COMPLETED: ("check", "success"),
        EV.INTERVIEW_CANCELLED: ("x", "neutral"), EV.INTERVIEW_RESCHEDULED: ("calendar", "warning"),
        EV.INTERVIEW_UPDATED: ("edit", "neutral"), EV.DOCS_GENERATED: ("doc", "success"),
        EV.FILES_REGENERATED: ("doc", "neutral"), EV.GENERATION_FAILED: ("alert", "danger"),
        EV.FOLLOWUP_SENT: ("send", "info"), EV.ACTION_ADDED: ("clock", "neutral"), EV.ACTION_DONE: ("check", "success"),
        EV.NOTE: ("note", "neutral"), EV.REJECTED: ("x", "danger"), EV.WITHDRAWN: ("skip", "neutral"),
        EV.CLOSED: ("archive", "neutral"), EV.ACCEPTED: ("award", "success"), EV.REOPENED: ("refresh", "info"),
        EV.ON_HOLD: ("pause", "warning"), EV.RESUMED: ("play", "info"), EV.IMPORTED: ("plus", "neutral"),
        EV.SKIPPED_DUPLICATE: ("skip", "neutral"), EV.SKIPPED: ("skip", "neutral"),
        EV.APPLY_BLOCKED: ("alert", "warning"), EV.APPLY_DRY_RUN: ("check", "neutral"),
    }

    def _timeline_row(self, e) -> QWidget:
        icon_name, kind = self._EVENT_STYLE.get(e.type, ("dot", "neutral"))
        if e.type == EV.STAGE_CHANGED and "offer" in (e.detail or "").lower():
            icon_name, kind = "award", "success"
        fg, bg = theme.badge_colors("neutral" if e.voided else kind)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 6, 2, 6)
        h.setSpacing(10)
        chip = QLabel()
        chip.setFixedSize(28, 28)
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip.setStyleSheet(f"QLabel {{ background: {bg}; border-radius: 14px; }}")
        chip.setPixmap(icons.icon(icon_name, fg, 15).pixmap(15, 15))
        h.addWidget(chip, alignment=Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(3)
        flags = (" · edited" if e.edited else "") + (" · removed" if e.voided else "")
        title = QLabel(e.label)
        title.setStyleSheet("font-weight: 650;" + ("text-decoration: line-through;" if e.voided else ""))
        title.setWordWrap(True)
        col.addWidget(title)
        extra = " · ".join(x for x in (e.detail, e.notes) if x)
        if extra:
            sub = muted_label(extra)
            col.addWidget(sub)
        col.addStretch(1)
        h.addLayout(col, 1)
        when = muted_label(format_local(e.when, "%b %d, %Y · %H:%M") + flags)
        when.setStyleSheet("font-size: 11.5px;")
        h.addWidget(when, alignment=Qt.AlignmentFlag.AlignTop)
        return w

    def _fill_timeline(self) -> None:
        keep = self._selected_entry()
        self.timeline.clear()
        entries = self.service.timeline(self.job_id, include_voided=self.show_voided.isChecked())
        for e in reversed(entries):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, {
                "id": e.event_id, "type": e.type, "voided": e.voided, "notes": e.notes, "when": e.when,
                "label": e.label})
            row = self._timeline_row(e)
            hint = row.sizeHint()
            hint.setHeight(max(hint.height() + 10, 52))
            item.setSizeHint(hint)
            self.timeline.addItem(item)
            self.timeline.setItemWidget(item, row)
            if keep and keep["id"] == e.event_id:
                self.timeline.setCurrentItem(item)
        self._timeline_selection()

    # ----------------------------------------------------------------- messages

    def _build_messages(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 4)
        top = QHBoxLayout()
        top.addWidget(muted_label("Replies from recruiters — positive or not."), 1)
        add = primary_button("Record message", "message")
        add.clicked.connect(self._add_message)
        top.addWidget(add)
        lay.addLayout(top)
        self.msg_list = QListWidget()
        self.msg_list.setWordWrap(True)
        self.msg_list.itemDoubleClicked.connect(lambda _i: self._edit_message())
        lay.addWidget(self.msg_list, 1)
        bar = QHBoxLayout()
        bar.addStretch(1)
        edit = ghost_button("Edit message", "edit")
        edit.clicked.connect(self._edit_message)
        bar.addWidget(edit)
        lay.addLayout(bar)
        return page

    def _add_message(self) -> None:
        MessageDialog(parent=self).ask(lambda v: self._safe(self.service.record_message, self.job_id, **v))

    def _edit_message(self) -> None:
        item = self.msg_list.currentItem()
        if not item:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        MessageDialog(data, parent=self).ask(lambda v: self._safe(self.service.update_message, data["id"], **v))

    def _fill_messages(self) -> None:
        self.msg_list.clear()
        for m in self.service.list_messages(self.job_id):
            tone = {"positive": "▲ positive", "negative": "▼ negative"}.get(m["sentiment"], "● neutral")
            flag = "  · follow-up needed" if m["follow_up_required"] else ""
            text = (f"{format_local(m['received_at'])}   {m['sender'] or 'Unknown sender'} · "
                    f"{_MSG_KIND.get(m['kind'], 'Message')} · {tone}{flag}")
            if m["summary"]:
                text += f"\n{m['summary']}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, m)
            self.msg_list.addItem(item)

    # --------------------------------------------------------------- interviews

    def _build_interviews(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 4)
        top = QHBoxLayout()
        top.addWidget(muted_label("Any number of rounds per job."), 1)
        add = primary_button("Add interview", "calendar")
        add.clicked.connect(self._add_interview)
        top.addWidget(add)
        lay.addLayout(top)
        self.iv_list = QListWidget()
        self.iv_list.setWordWrap(True)
        self.iv_list.itemDoubleClicked.connect(lambda _i: self._edit_interview())
        lay.addWidget(self.iv_list, 1)
        bar = QHBoxLayout()
        self.iv_done = ghost_button("Mark completed", "check")
        self.iv_done.clicked.connect(lambda: self._iv_status("Completed"))
        self.iv_cancel = ghost_button("Cancel", "x")
        self.iv_cancel.clicked.connect(lambda: self._iv_status("Cancelled"))
        self.iv_link = ghost_button("Meeting link", "external")
        self.iv_link.clicked.connect(self._iv_open_link)
        edit = ghost_button("Edit", "edit")
        edit.clicked.connect(self._edit_interview)
        delete = ghost_button("Delete", "trash")
        delete.clicked.connect(self._delete_interview)
        for b in (self.iv_done, self.iv_cancel, self.iv_link):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(edit)
        bar.addWidget(delete)
        lay.addLayout(bar)
        return page

    def _selected_interview(self):
        item = self.iv_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _add_interview(self) -> None:
        def save(v: dict) -> None:
            status = v.pop("status")
            result = v.pop("result")
            iid = self._safe(self.service.schedule_interview, self.job_id, **{
                k: v[k] for k in ("kind", "round_no", "scheduled_at", "tz_name", "interviewer", "meeting_url",
                                  "notes")})
            if iid and (status != "Scheduled" or result):
                self._safe(self.service.update_interview, iid, status=status, result=result)

        InterviewDialog(parent=self).ask(save)

    def _edit_interview(self) -> None:
        iv = self._selected_interview()
        if not iv:
            return

        def save(v: dict) -> None:
            v["timezone"] = v.pop("tz_name")
            self._safe(self.service.update_interview, iv["id"], **v)

        InterviewDialog(iv, parent=self).ask(save)

    def _iv_status(self, status: str) -> None:
        iv = self._selected_interview()
        if iv:
            self._safe(self.service.update_interview, iv["id"], status=status)

    def _iv_open_link(self) -> None:
        iv = self._selected_interview()
        if iv and iv["meeting_url"]:
            system.open_url(iv["meeting_url"])

    def _delete_interview(self) -> None:
        iv = self._selected_interview()
        if iv and ask_yes_no(self, "Delete interview", "Delete this interview record? Its timeline entries are "
                             "removed from analytics but kept in the audit history."):
            self._safe(self.service.delete_interview, iv["id"])

    def _fill_interviews(self) -> None:
        self.iv_list.clear()
        for iv in self.service.list_interviews(self.job_id):
            when = format_in_zone(iv["scheduled_at"], iv["timezone"]) if iv["scheduled_at"] else "time not set"
            head = f"{_IV_KIND.get(iv['kind'], 'Interview')} · round {iv['round_no']}   {when}   [{iv['status']}]"
            sub = " · ".join(x for x in (iv["interviewer"], iv["result"], iv["meeting_url"]) if x)
            item = QListWidgetItem(head + (f"\n{sub}" if sub else ""))
            item.setData(Qt.ItemDataRole.UserRole, iv)
            self.iv_list.addItem(item)

    # ------------------------------------------------------------------ refresh

    def refresh(self) -> None:
        if not self.job_id:
            return
        row = self.service.get_row(self.job_id)
        if row is None:
            self.clear()
            self.closed.emit()
            return
        job = row.job
        self._building = True
        try:
            self.avatar.setPixmap(avatar_pixmap(job.company or "?", 46, 13))
            self.info_id.setText("APP-" + job.id.replace("-", "")[:6].upper())
            self.info_source.setText(job.source or "—")
            self.info_salary.setText(format_salary(job.salary_text, job.salary_min, job.salary_max,
                                                   job.salary_period, job.salary_currency) or "—")
            self.info_folder.setText(job.output_path or "—")
            self.company.setText(job.company or "Unknown company")
            self.role.setText(job.role_title or "Untitled role")
            clear_layout(self.badges)
            stage = stage_from_value(job.app_stage)
            self.badges.addWidget(badge(stage.label, theme.STAGE_KIND[stage.value]))
            out = outcome_from_value(job.outcome)
            if out:
                self.badges.addWidget(badge(out.label, theme.OUTCOME_KIND[out.value]))
            elif job.applied_at:
                self.badges.addWidget(badge(row.response_label, "neutral"))
            if job.archived_at:
                self.badges.addWidget(badge("Archived", "neutral"))

            applied_on = parse_iso(job.applied_at)
            from ui.applications_view import relative_time

            self.applied_meta.setText(f"Applied {format_local(applied_on, '%b %d, %Y')}" if applied_on
                                      else "Not applied yet")
            last = parse_iso(job.last_activity_at)
            self.updated_meta.setText(f"Last update  {relative_time(last, utc_now())}" if last else "No activity yet")

            self.applied_label.setText(f"Applied {format_local(applied_on, '%b %d, %Y')}" if applied_on
                                       else "Not applied yet")
            self.applied_btn.setVisible(not applied_on)
            self.applied_date_btn.setVisible(not applied_on)
            self.stage_combo.setCurrentIndex(self.stage_combo.findData(stage.value))
            self.outcome_btn.setVisible(not out)
            self.reopen_btn.setVisible(bool(out))
            self.response_label.setText(row.response_label)
            self.hold_box.setChecked(job.on_hold)
            self.hold_box.setEnabled(bool(applied_on) and not out)

            nxt = row.next_action if row.next_action and row.next_action["kind"] == "next_action" else None
            if not self.action_text.hasFocus():
                self.action_text.setText(nxt["description"] if nxt else "")
            if nxt and nxt["due_date"]:
                self.action_nodate.setChecked(False)
                self.action_date.setDate(QDate.fromString(nxt["due_date"], "yyyy-MM-dd"))
            else:
                self.action_nodate.setChecked(bool(nxt))
            self.action_done.setEnabled(nxt is not None)

            clear_layout(self.followups)
            open_followups = [a for a in self.service.list_actions(self.job_id, include_done=False)
                              if a["kind"] == "follow_up"]
            if not open_followups:
                self.followups.addWidget(muted_label("No follow-ups pending."))
            for a in open_followups:
                self.followups.addWidget(self._followup_row(a))

            url = job.origin_url or job.url
            self.url_label.setText(f'<a style="color: {theme.current().accent};" href="{url}">View posting</a>'
                                   if url and not url.startswith("manual:") else "—")
            self.url_label.setToolTip(url)
            self._fill_files(job)
            if not self.notes.hasFocus():
                self.notes.setPlainText(job.notes)
        finally:
            self._building = False
        self._fill_timeline()
        self._fill_messages()
        self._fill_interviews()

    def _followup_row(self, a: dict) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        due = f"  · due {a['due_date']}" if a["due_date"] else ""
        label = QLabel(a["description"] + due)
        label.setWordWrap(True)
        if a["due_date"] and a["due_date"] < date.today().isoformat():
            label.setStyleSheet(f"color: {theme.current().danger};")
        done = row_action("check", "Mark done", "success")
        done.clicked.connect(lambda _=False, i=a["id"]: self._safe(self.service.complete_action, i))
        h.addWidget(label, 1)
        h.addWidget(done)
        return w

    def _fill_files(self, job) -> None:
        clear_layout(self.files_box)
        for ref in file_refs(job):
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            dot = QLabel()
            dot.setPixmap(circle_icon("check" if ref.exists else "dot", "#2FD072" if ref.exists else theme.current().border, 16,
                                      "#06280F" if ref.exists else theme.current().border))
            h.addWidget(dot)
            name = QLabel(ref.label)
            status = QLabel("" if ref.exists else ("missing on disk" if ref.path else "not generated"))
            status.setObjectName("Muted")
            h.addWidget(name, 1)
            h.addWidget(status)
            op = row_action("external", f"Open {ref.label}", "text")
            op.setEnabled(ref.exists)
            op.clicked.connect(lambda _=False, p=ref.path: system.open_path(p))
            cp = copy_button("Copy path", lambda p=ref.path: p)
            cp.setEnabled(bool(ref.path))
            h.addWidget(op)
            h.addWidget(cp)
            self.files_box.addWidget(w)
        folder = output_folder(job)
        fb = ghost_button("Open folder", "folder-open")
        fb.setEnabled(bool(folder and folder.exists()))
        fb.clicked.connect(lambda: folder and system.open_folder(folder))
        self.files_box.addWidget(fb, alignment=Qt.AlignmentFlag.AlignLeft)
