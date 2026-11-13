"""Auto Apply: what the bot is doing, what needs you, what it has done, and the facts it may state for you."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.apply_facts import (
    DEFAULTS,
    FACTS,
    clean_facts,
    clean_form_questions,
    norm_question,
    questions_from_attempt,
    store_manual_answers,
)
from core.ats import classify as classify_link
from core.apply_runner import AttemptStore
from core.chat_questions import DEFAULT_ANSWER_PROMPT
from core.discovery import DiscoveryService
from core.settings_store import SettingsStore, yes_no
from core.timeutil import format_local
from ui.widgets import (
    RowTable,
    badge,
    danger_button,
    ghost_button,
    kpi_card,
    company_delegate,
    muted_label,
    page_title,
    primary_button,
    row_action,
    section_title,
)

STATE_LABEL = {
    "submitted": ("Sent", "success"), "dry_run": ("Dry run", "info"), "needs_you": ("Needs you", "warning"),
    "human_check": ("Verification", "warning"), "expired": ("Closed", "neutral"), "failed": ("Failed", "danger"),
    "running": ("Running", "info"), "dismissed": ("Dismissed", "neutral"),
}
FOUND_LABEL = {"queued": ("Queued", "success"), "unsupported": ("Other site", "neutral"), "duplicate": ("Duplicate", "neutral"),
               "filtered": ("Filtered", "neutral"), "unresolved": ("No link yet", "warning")}
MODES = (("off", "Off"), ("dry_run", "Dry run (fill and check, never send)"), ("auto", "Auto (send applications)"))


class AutoApplyView(QWidget):
    open_browser_requested = Signal()
    run_discovery_requested = Signal()
    mode_requested = Signal(str)
    kill_requested = Signal()
    resume_requested = Signal()
    apply_now_requested = Signal(str, bool)  # job id, and whether to press Submit
    dismiss_requested = Signal(int)
    applied_requested = Signal(str, int)  # job id, attempt id
    facts_saved = Signal()

    def __init__(self, store: SettingsStore, attempts: AttemptStore, found: DiscoveryService, *,
                 readonly: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ContentPane")
        self.store, self.attempts, self.found, self.readonly = store, attempts, found, readonly

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 22, 28, 22)
        outer.setSpacing(14)
        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(6)
        title_box.addWidget(page_title("Auto Apply"))
        title_box.addWidget(muted_label(
            "Retry runs a job again. Answer is only for a choice you want to pick yourself. "
            "Ready to submit is a dry run waiting for Send."))
        head.addLayout(title_box, 1)
        outer.addLayout(head)

        # ---- status tiles
        tiles = QGridLayout()
        tiles.setHorizontalSpacing(12)
        self._browser_card, self.browser_value, self.browser_sub = kpi_card("Bot browser", icon="external", kind="info")
        self._disc_card, self.disc_value, self.disc_sub = kpi_card("Finding jobs", icon="search", kind="info")
        self._apply_card, self.apply_value, self.apply_sub = kpi_card("Applying", icon="send", kind="success")
        self._today_card, self.today_value, self.today_sub = kpi_card("Sent today", icon="check", kind="success")
        for i, card in enumerate((self._browser_card, self._disc_card, self._apply_card, self._today_card)):
            tiles.addWidget(card, 0, i)
        outer.addLayout(tiles)

        # ---- controls
        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.mode = QComboBox()
        for value, label in MODES:
            self.mode.addItem(label, value)
        self.mode.activated.connect(lambda _i: self.mode_requested.emit(self.mode.currentData()))
        self.browser_btn = ghost_button("Open bot browser", "external")
        self.browser_btn.setToolTip("Opens the browser the bot uses. Sign in to Jobright and Outlook in it once.")
        self.browser_btn.clicked.connect(self.open_browser_requested)
        self.discover_btn = ghost_button("Check Jobright now", "refresh")
        self.discover_btn.clicked.connect(self.run_discovery_requested)
        self.kill_btn = danger_button("Stop now")
        self.kill_btn.setToolTip("Stops applying immediately. An application in progress is abandoned before it is sent.")
        self.kill_btn.clicked.connect(self._toggle_kill)
        self._killed = False
        self.mode_label = QLabel("Applying:")
        controls.addWidget(self.mode_label)
        controls.addWidget(self.mode)
        controls.addStretch(1)
        for w in (self.browser_btn, self.discover_btn, self.kill_btn):
            controls.addWidget(w)
        outer.addLayout(controls)
        self.problem = muted_label("")
        self.problem.setVisible(False)
        outer.addWidget(self.problem)
        self.disc_detail = muted_label("")  # what the last Jobright read saw, so "found nothing" has a visible reason
        self.disc_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.disc_detail.setVisible(False)
        outer.addWidget(self.disc_detail)

        # ---- tabs
        self.tabs = QTabWidget()
        self.queue_table = self._job_table(["Job", "Status", "What happened", ""])
        self.ready_table = self._job_table(["Job", "Checked", "What happened", ""])
        self.history_table = self._table(["When", "Company", "Role", "Result", "Detail"], 4)
        self.found_table = self._table(["Seen", "Company", "Title", "Result", "Detail"], 4)
        self.tabs.addTab(self.queue_table, "To apply")
        self.tabs.addTab(self.ready_table, "Ready to submit")
        self.tabs.addTab(self.history_table, "History")
        self.tabs.addTab(self.found_table, "Found on Jobright")
        self.answers_tab = self._build_answers()
        self.tabs.addTab(self.answers_tab, "Your answers")
        outer.addWidget(self.tabs, 1)

        if readonly:  # a viewer reads the results; the bot itself is driven from the controller
            for w in (self.mode, self.browser_btn, self.discover_btn, self.kill_btn):
                w.setEnabled(False)
                w.setVisible(False)
            for w in (self.mode_label, self._browser_card, self._disc_card, self._apply_card, self._today_card):
                w.setVisible(False)
        self._timer = QTimer(self)
        self._timer.setInterval(4000)
        self._timer.timeout.connect(self.refresh)

    # ------------------------------------------------------------------ build

    def _job_table(self, headers: list[str]) -> RowTable:
        t = self._table(headers, 2)
        t.setItemDelegateForColumn(0, company_delegate(t))
        t.setWordWrap(False)
        t.setTextElideMode(Qt.TextElideMode.ElideRight)
        t.verticalHeader().setDefaultSectionSize(52)
        header = t.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        t.setColumnWidth(0, 280)
        t.setColumnWidth(1, 148)
        t.setColumnWidth(3, 220)
        return t

    @staticmethod
    def _tab_page(table: QWidget, note: str) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(muted_label(note))
        lay.addWidget(table, 1)
        return page

    @staticmethod
    def _table(headers: list[str], stretch_col: int) -> RowTable:
        t = RowTable(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setShowGrid(False)
        t.horizontalHeader().setSectionResizeMode(stretch_col, QHeaderView.ResizeMode.Stretch)
        return t

    def _build_answers(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setSpacing(12)
        lay.addWidget(muted_label(
            "Pick an answer from each list. These are the choices Greenhouse forms use, and a saved choice is matched "
            "to that form's own wording. Anything left empty is never guessed: a required question without an answer "
            "stops that application and asks you in a dialog. A question a company wrote itself is added at the "
            "bottom, with that form's options, the first time an application stops on it. Your name, email, phone, "
            "links and work authorization come from Settings."))
        # These five live in your profile (Settings > Personal information); they are editable here too, so everything
        # a form asks for can be filled in on this one page.
        lay.addWidget(section_title("From your profile (shared with Settings)"))
        prof = QFormLayout()
        lay.addLayout(prof)
        self._profile_widgets: dict[str, QLineEdit | QComboBox] = {}
        for attr, label in (("linkedin_url", "LinkedIn address"), ("github_url", "GitHub address"),
                            ("website_url", "Website / portfolio"), ("work_authorization", "Legally authorized to work in the US"),
                            ("needs_sponsorship", "Needs visa sponsorship now or later")):
            if attr in {"work_authorization", "needs_sponsorship"}:
                w: QLineEdit | QComboBox = QComboBox()
                for text, value in (("(not set)", ""), ("Yes", "Yes"), ("No", "No")):
                    w.addItem(text, value)
            else:
                w = QLineEdit()
                w.setPlaceholderText("https://")
            self._profile_widgets[attr] = w
            prof.addRow(label, w)
        self.profile_note = muted_label("")
        lay.addWidget(self.profile_note)
        self._fact_widgets: dict[str, QLineEdit | QComboBox] = {}
        group, form = "", None
        for spec in FACTS:
            if spec.group != group:
                group = spec.group
                lay.addWidget(section_title(group))
                form = QFormLayout()
                form.setLabelAlignment(form.labelAlignment())
                lay.addLayout(form)
            default = DEFAULTS.get(spec.key, "")
            if spec.choices:
                w: QLineEdit | QComboBox = QComboBox()
                w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                w.setMinimumContentsLength(28)
                w.addItem(f"(default: {default})" if default else "(not set)", "")  # blank means the default is used
                for c in spec.choices:
                    w.addItem(c, c)
            else:
                w = QLineEdit()
                w.setPlaceholderText(f"default: {default}" if default else spec.hint)
            w.setToolTip(spec.hint)
            self._fact_widgets[spec.key] = w
            form.addRow(spec.label, w)
        lay.addWidget(section_title("Questions a company asked"))
        lay.addWidget(muted_label(
            "When an application stops on a multiple-choice question that is not in the lists above, that question "
            "is added here with the form's own options. Pick one and save. Later applications reuse it."))
        self._asked_empty = muted_label("None yet.")
        lay.addWidget(self._asked_empty)
        self._asked_form = QFormLayout()
        self._asked_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        lay.addLayout(self._asked_form)
        self._asked_widgets: dict[str, QComboBox | QLineEdit] = {}
        self._asked_specs: dict[str, dict] = {}
        self._asked_sig: list | None = None
        lay.addWidget(section_title("How open questions are answered"))
        lay.addWidget(muted_label("Sent with every open question to the chat that wrote the resume for that job. "
                                  "Leave empty to use the default."))
        self.answer_prompt = QPlainTextEdit()
        self.answer_prompt.setPlaceholderText(DEFAULT_ANSWER_PROMPT)
        self.answer_prompt.setMinimumHeight(110)
        lay.addWidget(self.answer_prompt)
        self.save_answers = primary_button("Save answers")
        self.save_answers.clicked.connect(self.save_facts)
        row = QHBoxLayout()
        row.addStretch(1)
        self.saved_note = muted_label("")
        row.addWidget(self.saved_note)
        row.addWidget(self.save_answers)
        lay.addLayout(row)
        lay.addStretch(1)
        scroll.setWidget(body)
        self.load_facts()
        if self.readonly:
            self.save_answers.setEnabled(False)
        return scroll

    # ------------------------------------------------------------------ facts

    def load_facts(self) -> None:
        profile = self.store.settings.profile
        locked = self.store.data_locked  # with an app password and not yet unlocked, the profile cannot be edited
        for attr, w in self._profile_widgets.items():
            value = getattr(profile, attr, "")
            if isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, w.findData(value)))
            else:
                w.setText(value)
            w.setEnabled(not locked and not self.readonly)
        self.profile_note.setText("Unlock the app (Settings) to edit these." if locked else "")
        facts = self.store.settings.apply_facts
        for key, w in self._fact_widgets.items():
            value = facts.get(key, "")
            if isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, w.findData(value)))
            else:
                w.setText(value)
        self.answer_prompt.setPlainText(self.store.settings.answer_prompt)
        self._sync_asked()

    def _sync_asked(self) -> None:
        questions = list(self.store.settings.form_questions or [])
        sig = [(q.get("label"), tuple(q.get("options") or [])) for q in questions]
        if sig == self._asked_sig:
            return
        pending = {key: (w.currentData() if isinstance(w, QComboBox) else w.text()) or ""
                   for key, w in self._asked_widgets.items()}
        while self._asked_form.rowCount():
            self._asked_form.removeRow(0)
        self._asked_widgets = {}
        self._asked_specs = {}
        self._asked_sig = sig
        self._asked_empty.setVisible(not questions)
        locked = self.store.data_locked or self.readonly
        for q in questions:
            key = norm_question(str(q.get("label") or ""))
            if not key:
                continue
            options = [str(opt) for opt in (q.get("options") or [])]
            chosen = q.get("answer") or pending.get(key) or ""
            if options:
                w = QComboBox()
                w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                w.setMinimumContentsLength(28)
                w.addItem("(not set)", "")
                for opt in options:
                    w.addItem(opt, opt)
                w.setCurrentIndex(max(0, w.findData(chosen)))
            else:
                w = QLineEdit()
                w.setText(str(chosen))
            w.setEnabled(not locked)
            self._asked_widgets[key] = w
            self._asked_specs[key] = q
            self._asked_form.addRow(str(q.get("label") or ""), w)

    def save_facts(self) -> None:
        raw = {k: (w.currentData() if isinstance(w, QComboBox) else w.text()) for k, w in self._fact_widgets.items()}
        if not self.store.data_locked:
            profile = self.store.settings.profile
            for attr, w in self._profile_widgets.items():
                text = w.currentData() if isinstance(w, QComboBox) else w.text().strip()
                setattr(profile, attr, yes_no(text) if attr in {"work_authorization", "needs_sponsorship"} else text)
        self.store.settings.apply_facts = clean_facts(raw)
        if self._asked_sig is not None:
            raw_q = []
            for key, w in self._asked_widgets.items():
                spec = dict(self._asked_specs[key])
                spec["answer"] = (w.currentData() if isinstance(w, QComboBox) else w.text().strip()) or ""
                raw_q.append(spec)
            self.store.settings.form_questions = clean_form_questions(raw_q)
        self.store.settings.answer_prompt = self.answer_prompt.toPlainText().strip()
        self.store.save()
        self.saved_note.setText("Saved")
        self.facts_saved.emit()

    # ------------------------------------------------------------------ live state

    def set_status(self, *, browser_running: bool, discovery, apply, killed: bool, mode: str, cap: int,
                   problems: list[str]) -> None:
        self.browser_value.setText("Open" if browser_running else "Closed")
        self.browser_sub.setText("signed-in session ready" if browser_running else "opens when needed")
        self.disc_value.setText(_pretty(discovery.state))
        self.disc_sub.setText(discovery.message[:60])
        self._disc_card.setToolTip(discovery.message)
        detail = getattr(discovery, "detail", [])
        extra = "\n".join(line[:300] for line in detail[:8])
        self._disc_card.setToolTip((discovery.message + ("\n" + extra if extra else "")).strip())
        self.disc_detail.setVisible(False)
        self.apply_value.setText(_pretty(apply.state))
        self.apply_sub.setText((apply.current or apply.message)[:60])
        self.today_value.setText(f"{apply.submitted_today} / {cap}")
        self.today_sub.setText("daily limit")
        self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
        self._killed = killed
        self.kill_btn.setText("Resume" if killed else "Stop now")
        self.problem.setVisible(bool(problems))
        self.problem.setText("  ".join(problems))

    def _toggle_kill(self) -> None:
        (self.resume_requested if self._killed else self.kill_requested).emit()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    # ------------------------------------------------------------------ tables

    def refresh(self) -> None:
        rows = self.attempts.open_jobs()
        ready = [r for r in rows if (r.get("state") or "") == "dry_run"]
        waiting = [r for r in rows if (r.get("state") or "") != "dry_run"]
        self._fill_queue(waiting)
        self._fill_ready(ready)
        self._fill_history()
        self._fill_found()
        self._sync_asked()
        self.tabs.setTabText(0, f"To apply ({len(waiting)})" if waiting else "To apply")
        self.tabs.setTabText(1, f"Ready to submit ({len(ready)})" if ready else "Ready to submit")

    def _fill_queue(self, rows: list[dict]) -> None:
        t = self.queue_table
        t.setRowCount(len(rows))
        for r, a in enumerate(rows):
            state = a.get("state") or ""
            supported = classify_link(a.get("origin_url") or a.get("url") or "").supported
            label, kind = _queue_status(state, supported)
            reason = a.get("reason") or ("Not tried yet" if not state else "")
            when = format_local(a.get("finished_at"))
            t.setItem(r, 0, _job_item(a.get("company") or "", a.get("role_title") or ""))
            t.setCellWidget(r, 1, _badge_cell(label, kind))
            t.setItem(r, 2, _item(reason, tip=f"{when}\n{reason}".strip()))
            t.setCellWidget(r, 3, self._queue_actions(a, supported))
            t.setRowHeight(r, 52)

    def _fill_ready(self, rows: list[dict]) -> None:
        t = self.ready_table
        t.setRowCount(len(rows))
        for r, a in enumerate(rows):
            reason = a.get("reason") or "Filled and checked"
            when = format_local(a.get("finished_at"))
            t.setItem(r, 0, _job_item(a.get("company") or "", a.get("role_title") or ""))
            t.setItem(r, 1, _item(when))
            t.setItem(r, 2, _item(reason, tip=reason))
            buttons = []
            if not self.readonly:
                submit = _text_button("Submit", primary=True)
                submit.setFixedWidth(84)
                submit.setToolTip("Open this form in the bot browser and send it.")
                submit.clicked.connect(lambda _=False, j=a["job_id"]: self.apply_now_requested.emit(j, True))
                buttons.append(submit)
            buttons.append(self._more_button(a))
            t.setCellWidget(r, 3, _action_cell(buttons))
            t.setRowHeight(r, 52)

    def _queue_actions(self, row: dict, supported: bool) -> QWidget:
        buttons = []
        if not self.readonly and supported:
            retry = _text_button("Retry", primary=True)
            retry.setFixedWidth(72)
            retry.setToolTip("Try this application again. You do not have to answer anything first.")
            retry.clicked.connect(lambda _=False, j=row["job_id"]: self.apply_now_requested.emit(j, self._will_submit()))
            buttons.append(retry)
            questions = [q for q in questions_from_attempt(
                row, self.store.settings.form_questions, self.store.settings.apply_facts) if not q.get("locked")]
            if questions:
                answer = _text_button("Answer")
                answer.setFixedWidth(78)
                answer.setToolTip("Optional. A dropdown lists that question's choices. Retry works without this.")
                answer.clicked.connect(lambda _=False, item=dict(row): self._answer(item))
                buttons.append(answer)
            buttons.append(self._more_button(row))
            return _action_cell(buttons)
        if self.readonly:
            return _action_cell([self._open_button(row.get("url") or "")])
        return _action_cell([self._more_button(row)])

    def _more_button(self, row: dict):
        button = row_action("more", "Open the posting, the screenshot, or mark the job")
        button.clicked.connect(lambda _=False, item=dict(row), anchor=button: self._more_menu(item, anchor))
        return button

    def _more_menu(self, row: dict, anchor) -> None:
        menu = QMenu(self)
        url = row.get("url") or ""
        if url:
            menu.addAction("Open the posting", lambda u=url: QDesktopServices.openUrl(QUrl(u)))
        shot = row.get("screenshot") or ""
        if shot and Path(shot).is_file():
            menu.addAction("See where it stopped", lambda p=shot: QDesktopServices.openUrl(QUrl.fromLocalFile(p)))
        attempt_id = row.get("attempt_id")
        if attempt_id and not self.readonly:
            menu.addSeparator()
            menu.addAction("I applied myself", lambda j=row["job_id"], i=int(attempt_id): self.applied_requested.emit(j, i))
            menu.addAction("Do not apply", lambda i=int(attempt_id): self.dismiss_requested.emit(i))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _answer(self, row: dict) -> None:
        """Optional. Saves whatever you picked and leaves Retry as the way to run the job again."""
        questions = questions_from_attempt(row, self.store.settings.form_questions, self.store.settings.apply_facts)
        if not questions:
            return
        dialog = AnswerDialog(str(row.get("company") or ""), str(row.get("role_title") or ""), questions, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        picked = dialog.answers()
        if not picked:
            return
        self.store.settings.form_questions = store_manual_answers(self.store.settings.form_questions, picked)
        self.store.save()
        self._asked_sig = None
        self.load_facts()

    def _will_submit(self) -> bool:
        return self.mode.currentData() == "auto"

    def _open_button(self, url: str):
        button = row_action("external", "Open the posting")
        button.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
        return button

    def _fill_history(self) -> None:
        t = self.history_table
        rows = self.attempts.recent(200)
        t.setRowCount(len(rows))
        for r, a in enumerate(rows):
            t.setItem(r, 0, _item(format_local(a["started_at"])))
            t.setItem(r, 1, _item(a["company"]))
            t.setItem(r, 2, _item(a["role_title"]))
            label, kind = STATE_LABEL.get(a["state"], (a["state"], "neutral"))
            t.setCellWidget(r, 3, _badge_cell(label, kind))
            t.setItem(r, 4, _item(a["reason"], tip=a["reason"]))
        for c in (0, 1, 2):
            t.resizeColumnToContents(c)
        t.setColumnWidth(3, 130)  # the result badge sits in a cell widget, which resizeColumnToContents does not measure

    def _fill_found(self) -> None:
        t = self.found_table
        rows = self.found.recent(limit=200)
        t.setRowCount(len(rows))
        for r, d in enumerate(rows):
            t.setItem(r, 0, _item(format_local(d["first_seen_at"])))
            t.setItem(r, 1, _item(d["company"]))
            t.setItem(r, 2, _item(d["title"]))
            label, kind = FOUND_LABEL.get(d["state"], (d["state"], "neutral"))
            t.setCellWidget(r, 3, _badge_cell(label, kind))
            t.setItem(r, 4, _item(d["reason"] or d["ats"], tip=d["apply_url"]))
        for c in (0, 1, 2):
            t.resizeColumnToContents(c)
        t.setColumnWidth(3, 130)  # the badge sits in a cell widget, which resizeColumnToContents does not measure


class AnswerDialog(QDialog):
    """The questions one application could not answer. Try again uses whatever you type here."""

    def __init__(self, company: str, role: str, questions: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Optional answers")
        self.setMinimumWidth(640)
        self._editors: dict[str, QComboBox | QLineEdit] = {}
        self._options: dict[str, list[str]] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)
        who = " — ".join(part for part in (company, role) if part)
        root.addWidget(QLabel(who or "This application"))
        root.addWidget(muted_label(
            "Optional. Leave a question unset and use Retry on the row to try the application again as it is. "
            "A dropdown lists the choices for that question. Save keeps what you pick for the next try."))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        form = QVBoxLayout(body)
        form.setSpacing(14)
        for question in questions:
            form.addWidget(self._block(question))
        scroll.setWidget(body)
        scroll.setMinimumHeight(min(420, 80 + 90 * max(1, len(questions))))
        root.addWidget(scroll, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = ghost_button("Cancel")
        cancel.clicked.connect(self.reject)
        save = primary_button("Save answers")
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        root.addLayout(buttons)

    def _block(self, question: dict) -> QWidget:
        block = QWidget()
        lay = QVBoxLayout(block)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        label = QLabel(question.get("label") or question.get("reason") or "")
        label.setWordWrap(True)
        lay.addWidget(label)
        reason = question.get("reason") or ""
        if reason and reason != question.get("label"):
            lay.addWidget(muted_label(reason))
        if question.get("locked"):
            lay.addWidget(muted_label("Left blank. This asks whether the submission is automated."))
            return block
        options = list(question.get("options") or [])
        if options:
            editor: QComboBox | QLineEdit = QComboBox()
            editor.addItem("Select an answer", "")
            for opt in options:
                editor.addItem(opt, opt)
            current = question.get("answer") or ""
            if current:
                editor.setCurrentIndex(max(0, editor.findData(current)))
        else:
            editor = QLineEdit()
            editor.setText(question.get("answer") or "")
            editor.setPlaceholderText("Your answer")
        key = question.get("label") or ""
        if key:
            self._editors[key] = editor
            self._options[key] = options
        lay.addWidget(editor)
        return block

    def answers(self) -> list[tuple[str, str, list[str]]]:
        picked = []
        for label, editor in self._editors.items():
            text = editor.currentData() if isinstance(editor, QComboBox) else editor.text()
            text = " ".join(str(text or "").split())
            if text:
                picked.append((label, text, self._options.get(label) or []))
        return picked


def _text_button(text: str, *, primary: bool = False) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("PrimaryButton" if primary else "GhostButton")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFixedHeight(30)
    return button


def _queue_status(state: str, supported: bool) -> tuple[str, str]:
    if not supported:
        return "Other site", "neutral"
    if not state:
        return "Not tried", "neutral"
    if state == "needs_you":
        return "Needs answers", "warning"
    if state == "human_check":
        return "Check in browser", "warning"
    if state == "failed":
        return "Failed", "danger"
    if state == "dismissed":
        return "Skipped", "neutral"
    if state == "running":
        return "Running", "info"
    if state == "expired":
        return "Closed", "neutral"
    return STATE_LABEL.get(state, (state, "neutral"))


def _action_cell(buttons: list) -> QWidget:
    cell = QWidget()
    lay = QHBoxLayout(cell)
    lay.setContentsMargins(4, 0, 4, 0)
    lay.setSpacing(2)
    for button in buttons:
        lay.addWidget(button)
    lay.addStretch(1)
    return cell


def _job_item(company: str, role: str) -> QTableWidgetItem:
    item = QTableWidgetItem(company or "Unknown company")
    item.setData(Qt.ItemDataRole.UserRole + 1, role)
    item.setToolTip("\n".join(part for part in (company, role) if part))
    return item


def _item(text: str, tip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(str(text or ""))
    if tip:
        item.setToolTip(tip)
    return item


def _badge_cell(text: str, kind: str) -> QWidget:
    cell = QWidget()
    lay = QHBoxLayout(cell)
    lay.setContentsMargins(6, 0, 6, 0)
    lay.addWidget(badge(text, kind))
    lay.addStretch(1)
    return cell


def _pretty(state: str) -> str:
    return state.replace("_", " ").capitalize()
