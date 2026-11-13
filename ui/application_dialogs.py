"""Small non-blocking forms used by the application detail panel.

Dialogs are opened with ``open()`` (window-modal, no nested event loop), so queue progress
keeps updating while a form is on screen. Results arrive through ``accepted_values``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QDate, QDateTime, QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
)

from core.applications import INTERVIEW_KINDS, INTERVIEW_STATUSES, MESSAGE_KINDS, SENTIMENTS
from core.timeutil import parse_iso, tz_from_name, utc_now, utc_to_wall, wall_to_utc


def _combo(options: list[tuple[str, str]], current: str = "") -> QComboBox:
    box = QComboBox()
    for value, label in options:
        box.addItem(label, value)
    box.setCurrentIndex(max(0, box.findData(current)))
    return box


def dt_edit(value: datetime | None = None, tz_name: str = "") -> QDateTimeEdit:
    edit = QDateTimeEdit()
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("yyyy-MM-dd  HH:mm")
    wall = utc_to_wall(value or utc_now(), tz_name)
    edit.setDateTime(QDateTime(QDate(wall.year, wall.month, wall.day), QTime(wall.hour, wall.minute)))
    return edit


def edit_value(edit: QDateTimeEdit, tz_name: str = "") -> datetime:
    """Wall-clock value in ``tz_name`` (system local if empty) -> aware UTC."""
    q = edit.dateTime()
    naive = datetime(q.date().year(), q.date().month(), q.date().day(), q.time().hour(), q.time().minute())
    return wall_to_utc(naive, tz_name)


class _Form(QDialog):
    accepted_values = Signal(dict)

    def __init__(self, title: str, parent=None, width: int = 440) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(width)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)
        self.form = QFormLayout()
        self.form.setSpacing(9)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        outer.addLayout(self.form)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

    def values(self) -> dict[str, Any]:  # overridden
        return {}

    def _submit(self) -> None:
        self.accepted_values.emit(self.values())
        self.accept()

    def ask(self, callback: Callable[[dict], None]) -> "_Form":
        self.accepted_values.connect(callback)
        self.open()
        return self


class WhenDialog(_Form):
    """Pick a date/time (+ optional note). Used for applied date, outcomes and entry edits."""

    def __init__(self, title: str, when: datetime | None = None, *, note: str | None = "",
                 note_label: str = "Note", reason: bool = False, parent=None) -> None:
        super().__init__(title, parent, 400)
        self.when_edit = dt_edit(when)
        self.form.addRow("Date & time", self.when_edit)
        self.note_edit = None
        if note is not None:
            self.note_edit = QPlainTextEdit(note)
            self.note_edit.setFixedHeight(70)
            self.form.addRow(note_label, self.note_edit)
        self.reason_edit = None
        if reason:
            self.reason_edit = QLineEdit()
            self.reason_edit.setPlaceholderText("Why is this being changed? (kept in the audit trail)")
            self.form.addRow("Reason", self.reason_edit)

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {"when": edit_value(self.when_edit)}
        if self.note_edit is not None:
            out["note"] = self.note_edit.toPlainText().strip()
        if self.reason_edit is not None:
            out["reason"] = self.reason_edit.text().strip()
        return out


class MessageDialog(_Form):
    def __init__(self, initial: dict[str, Any] | None = None, parent=None) -> None:
        super().__init__("Edit message" if initial else "Record recruiter message", parent, 480)
        d = initial or {}
        self.when_edit = dt_edit(parse_iso(d.get("received_at")))
        self.sender = QLineEdit(d.get("sender", ""))
        self.sender.setPlaceholderText("Recruiter / sender name")
        self.kind = _combo(MESSAGE_KINDS, d.get("kind", "outreach"))
        self.sentiment = _combo(SENTIMENTS, d.get("sentiment", "neutral"))
        self.summary = QLineEdit(d.get("summary", ""))
        self.summary.setPlaceholderText("One-line summary")
        self.body = QPlainTextEdit(d.get("body", ""))
        self.body.setPlaceholderText("Optional: paste the message text")
        self.body.setFixedHeight(90)
        self.follow = QCheckBox("Follow-up / reply required")
        self.follow.setChecked(bool(d.get("follow_up_required")))
        for label, w in (("Received", self.when_edit), ("From", self.sender), ("Type", self.kind),
                         ("Tone", self.sentiment), ("Summary", self.summary), ("Message", self.body),
                         ("", self.follow)):
            self.form.addRow(label, w)
        if not initial:
            self.kind.currentIndexChanged.connect(self._suggest_tone)

    def _suggest_tone(self) -> None:
        tone = {"rejection": "negative", "interview_invite": "positive", "offer": "positive"}.get(
            self.kind.currentData())
        if tone:
            self.sentiment.setCurrentIndex(self.sentiment.findData(tone))

    def values(self) -> dict[str, Any]:
        return {
            "received_at": edit_value(self.when_edit),
            "sender": self.sender.text().strip(),
            "kind": self.kind.currentData(),
            "sentiment": self.sentiment.currentData(),
            "summary": self.summary.text().strip(),
            "body": self.body.toPlainText(),
            "follow_up_required": self.follow.isChecked(),
        }


def timezone_choices() -> list[str]:
    try:
        from zoneinfo import available_timezones

        return sorted(available_timezones())
    except Exception:  # noqa: BLE001
        return []


class InterviewDialog(_Form):
    def __init__(self, initial: dict[str, Any] | None = None, parent=None) -> None:
        super().__init__("Edit interview" if initial else "Add interview", parent, 480)
        d = initial or {}
        self.kind = _combo(INTERVIEW_KINDS, d.get("kind", "screening"))
        self.round = QSpinBox()
        self.round.setRange(1, 20)
        self.round.setValue(int(d.get("round_no") or 1))
        self.tz = QComboBox()
        self.tz.setEditable(True)
        self.tz.addItem("Local time", "")
        for name in timezone_choices():
            self.tz.addItem(name, name)
        self.tz.setCurrentIndex(max(0, self.tz.findData(d.get("timezone", ""))))
        self.has_time = QCheckBox("Date and time confirmed")
        sched = parse_iso(d.get("scheduled_at"))
        self.has_time.setChecked(bool(sched) or not initial)
        self.when_edit = dt_edit(sched, d.get("timezone", ""))
        self.has_time.toggled.connect(self.when_edit.setEnabled)
        self.when_edit.setEnabled(self.has_time.isChecked())
        self.interviewer = QLineEdit(d.get("interviewer", ""))
        self.url = QLineEdit(d.get("meeting_url", ""))
        self.url.setPlaceholderText("https://…")
        self.status = _combo([(s, s) for s in INTERVIEW_STATUSES], d.get("status", "Scheduled"))
        self.result = QLineEdit(d.get("result", ""))
        self.result.setPlaceholderText("Outcome, e.g. passed / feedback pending")
        self.notes = QPlainTextEdit(d.get("notes", ""))
        self.notes.setFixedHeight(70)
        for label, w in (("Type", self.kind), ("Round", self.round), ("", self.has_time), ("When", self.when_edit),
                         ("Time zone", self.tz), ("Interviewer", self.interviewer), ("Meeting link", self.url),
                         ("Status", self.status), ("Result", self.result), ("Notes", self.notes)):
            self.form.addRow(label, w)

    def tz_name(self) -> str:
        typed = self.tz.currentText().strip()
        if typed == "Local time":
            return ""
        return typed if tz_from_name(typed) else ""

    def values(self) -> dict[str, Any]:
        tz_name = self.tz_name()
        return {
            "kind": self.kind.currentData(),
            "round_no": self.round.value(),
            "scheduled_at": edit_value(self.when_edit, tz_name) if self.has_time.isChecked() else None,
            "tz_name": tz_name,
            "interviewer": self.interviewer.text().strip(),
            "meeting_url": self.url.text().strip(),
            "status": self.status.currentData(),
            "result": self.result.text().strip(),
            "notes": self.notes.toPlainText().strip(),
        }


class ManualApplicationDialog(_Form):
    """Track an application made outside the queue (nothing is generated)."""

    def __init__(self, parent=None) -> None:
        super().__init__("Add application", parent, 460)
        from core.application_stage import STAGE_ORDER

        self.company = QLineEdit()
        self.company.setPlaceholderText("Company")
        self.role = QLineEdit()
        self.role.setPlaceholderText("Role title")
        self.url = QLineEdit()
        self.url.setPlaceholderText("Job posting URL (optional)")
        self.source = QLineEdit("Manual")
        self.stage = _combo([(st.value, st.label) for st in STAGE_ORDER], "not_applied")
        self.when_edit = dt_edit()
        self.when_edit.setToolTip("Applied date (used when the stage is not 'Not applied')")
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(64)
        for label, w in (("Company", self.company), ("Role", self.role), ("Job URL", self.url),
                         ("Source", self.source), ("Stage", self.stage), ("Applied on", self.when_edit),
                         ("Notes", self.notes)):
            self.form.addRow(label, w)

    def _submit(self) -> None:
        if not self.company.text().strip():
            self.company.setFocus()
            self.company.setStyleSheet("border: 1px solid #EF5350;")
            return
        super()._submit()

    def values(self) -> dict[str, Any]:
        from core.application_stage import Stage

        stage = Stage(self.stage.currentData())
        return {
            "company": self.company.text(), "role": self.role.text(), "url": self.url.text(),
            "source": self.source.text(), "stage": stage, "notes": self.notes.toPlainText(),
            "applied_on": edit_value(self.when_edit) if stage != Stage.NOT_APPLIED else None,
        }
