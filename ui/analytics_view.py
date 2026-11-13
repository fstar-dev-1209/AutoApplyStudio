"""Analytics: how many bids, how many replies / screening / technical calls, and the percentages, as charts."""

from __future__ import annotations

from datetime import timedelta

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core import analytics
from core.application_db import ApplicationDatabase
from core.timeutil import PRESETS, DateRange, range_for
from ui.charts import FunnelChart, GroupedBarChart, LineChart
from ui.widgets import ghost_button, kpi_card, muted_label, page_title, section_title, set_icon

DEFINITIONS = (
    "How numbers are counted\n"
    "• Counts for a period are events that happened inside it (dated by when they happened, in your local "
    "calendar days).\n"
    "• Percentages follow the bids you sent in the period and ask what share ever replied or reached each "
    "stage, as of today. With nothing to divide by, a rate shows “—”, never 0%.\n"
    "• “Reached a stage” includes later stages, because stages can be skipped. Invitations count the first time an "
    "application reached that stage.\n"
    "• A reply is a recruiter message (not an automated acknowledgement), a stage advance, a scheduled interview "
    "or a rejection.\n"
    "• Archived applications still count in history; they are left out of the “right now” tiles. Reopened "
    "applications are counted once; a reopened rejection no longer counts as a rejection.\n"
    "• Document counts are attempt results, so a retry that succeeds adds one more generated."
)

GOOD, BAD, FLAT = "#2FD072", "#EF5350", "#7C8699"


def previous_range(rng: DateRange) -> DateRange | None:
    """The equally long period immediately before ``rng`` (None for unbounded ranges)."""
    if not (rng.first_day and rng.last_day):
        return None
    days = (rng.last_day - rng.first_day).days + 1
    return range_for("custom", custom=(rng.first_day - timedelta(days=days), rng.first_day - timedelta(days=1)))


def delta_text(cur: int, prev: int) -> tuple[str, str]:
    """(text, colour) for a change against the previous period."""
    if prev == 0:
        return ("new this period", GOOD) if cur else ("no change", FLAT)
    change = round(100 * (cur - prev) / prev)
    if change == 0:
        return "no change", FLAT
    return (f"▲ {change}%" if change > 0 else f"▼ {abs(change)}%"), (GOOD if change > 0 else BAD)


class AnalyticsView(QWidget):
    def __init__(self, db: ApplicationDatabase, service=None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ContentPane")
        self.db = db
        self._tiles: dict[str, tuple[QLabel, QLabel]] = {}
        self._stat: dict[str, tuple[QLabel, QLabel, QLabel]] = {}
        self.result: analytics.Analytics | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 0, 16, 8)
        outer.setSpacing(10)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 18, 0)  # line up with the cards, which sit left of the scrollbar
        box = QVBoxLayout()
        box.setSpacing(2)
        box.addWidget(page_title("Analytics"))
        self.subtitle = muted_label("Your job-search performance, computed from recorded activity.")
        box.addWidget(self.subtitle)
        head.addLayout(box, 1)
        self.range_combo = QComboBox()
        for key, label in PRESETS:
            self.range_combo.addItem(label, key)
        self.range_combo.setCurrentIndex(self.range_combo.findData("last_30"))
        self.range_combo.setMinimumWidth(140)
        self.from_edit = QDateEdit(QDate.currentDate().addDays(-30))
        self.to_edit = QDateEdit(QDate.currentDate())
        for e in (self.from_edit, self.to_edit):
            e.setCalendarPopup(True)
            e.setDisplayFormat("yyyy-MM-dd")
            e.dateChanged.connect(self.refresh)
            e.hide()
        self.range_combo.currentIndexChanged.connect(self._range_changed)
        refresh = ghost_button("Refresh", "refresh")
        refresh.clicked.connect(self.refresh)
        for w in (self.from_edit, self.to_edit, self.range_combo, refresh):
            head.addWidget(w, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(0, 0, 8, 12)
        self.body.setSpacing(12)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # ---- headline numbers: bids -> replies -> screening -> technical -> offers
        cards = QHBoxLayout()
        cards.setSpacing(10)
        for key, label, icon, kind in (
            ("submitted", "Bids sent", "send", "info"), ("responses", "Replies", "mail", "violet"),
            ("screening_invites", "Screening calls", "users", "warning"),
            ("technical_invites", "Technical calls", "calendar", "warning"), ("offers", "Offers", "award", "success"),
        ):
            card, value, sub = kpi_card(label, icon=icon, kind=kind)
            value.setStyleSheet("font-size: 30px; font-weight: 700;")
            sub.setStyleSheet("font-size: 12.5px; font-weight: 650;")
            delta = QLabel("")
            delta.setObjectName("KpiSub")
            card.layout().itemAt(0).layout().addWidget(delta)
            self._stat[key] = (value, sub, delta)
            cards.addWidget(card, 1)
        self.body.addLayout(cards)

        # ---- charts
        top = QHBoxLayout()
        top.setSpacing(12)
        self.line = LineChart()
        self.line.setMinimumHeight(250)
        self.funnel = FunnelChart()
        self.funnel.setMinimumHeight(250)
        top.addWidget(self._chart_card("Activity over time", self.line), 3)
        top.addWidget(self._chart_card("Conversion funnel (% of bids)", self.funnel), 2)
        self.body.addLayout(top)

        self.weekly = GroupedBarChart()
        self.monthly = GroupedBarChart()
        self.body.addWidget(self._chart_card("Weekly: bids sent, and how far those bids got", self.weekly))
        self.body.addWidget(self._chart_card("Monthly: bids sent, and how far those bids got", self.monthly))

        # ---- every metric, tucked away
        self.details_btn = ghost_button("Show all metrics", "chevron")
        self.details_btn.setCheckable(True)
        self.details_btn.toggled.connect(self._toggle_details)
        self.body.addWidget(self.details_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        self.details = QWidget()
        self.details_layout = QVBoxLayout(self.details)
        self.details_layout.setContentsMargins(0, 0, 0, 0)
        self.details_layout.setSpacing(8)
        self._group("Document productivity", [
            ("imported", "Jobs imported", "inbox", "info"), ("processed", "Jobs processed", "layers", "info"),
            ("generated", "Generated", "doc", "success"), ("failed", "Generation failed", "alert", "danger"),
            ("skipped_duplicates", "Skipped duplicates", "skip", "neutral"),
            ("ready_not_applied", "Ready, not applied", "briefcase-check", "warning")])
        self._group("Application activity", [
            ("submitted", "Applications submitted", "send", "info"), ("awaiting_reply", "Awaiting reply", "clock", "neutral"),
            ("responses", "Recruiter responses", "mail", "violet"), ("positive_responses", "Positive responses", "trend", "success"),
            ("rejections", "Rejections", "x", "danger"),
            ("bot_sent", "Sent by the bot", "send", "success")])
        self._group("Interviews", [
            ("screening_invites", "Screening invitations", "mail", "violet"),
            ("screening_done", "Screenings completed", "check", "success"),
            ("technical_invites", "Technical invitations", "mail", "warning"),
            ("technical_done", "Technicals completed", "check", "success"),
            ("final_invites", "Final interview invitations", "mail", "warning"), ("offers", "Offers", "award", "success")])
        self._group("Conversion", [
            ("generation_success", "Generation success", "doc", "success"),
            ("submission", "Application submission", "send", "info"), ("reply", "Reply rate", "mail", "violet"),
            ("screening", "Screening rate", "users", "violet"), ("technical", "Technical conversion", "users", "warning"),
            ("final", "Final conversion", "users", "warning"), ("offer", "Offer conversion", "award", "success"),
            ("days", "Median days to reply", "clock", "neutral")])
        note = muted_label(DEFINITIONS)
        note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details_layout.addWidget(note)
        self.details.hide()
        self.body.addWidget(self.details)
        self.body.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        if service is not None:
            service.subscribe(self.request_refresh)

    # ------------------------------------------------------------------ layout

    def _toggle_details(self, on: bool) -> None:
        self.details.setVisible(on)
        self.details_btn.setText("Hide all metrics" if on else "Show all metrics")
        set_icon(self.details_btn, "chevron-up" if on else "chevron", "text", 14)

    def _group(self, title: str, items: list[tuple[str, str, str, str]]) -> None:
        self.details_layout.addWidget(section_title(title))
        row = QGridLayout()
        row.setSpacing(10)
        cols = len(items) if len(items) <= 6 else 4  # no orphan tiles on the last row
        for i, (key, label, icon, kind) in enumerate(items):
            card, value, sub = kpi_card(label, icon=icon, kind=kind)
            self._tiles[key] = (value, sub)
            row.addWidget(card, i // cols, i % cols)
        for c in range(cols):
            row.setColumnStretch(c, 1)
        self.details_layout.addLayout(row)

    @staticmethod
    def _chart_card(title: str, chart: QWidget) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(6)
        lay.addWidget(section_title(title))
        lay.addWidget(chart, 1)
        return card

    # ------------------------------------------------------------------ data

    def _range_changed(self) -> None:
        custom = self.range_combo.currentData() == "custom"
        self.from_edit.setVisible(custom)
        self.to_edit.setVisible(custom)
        self.refresh()

    def request_refresh(self) -> None:
        if self.isVisible():
            self._timer.start(0)

    def current_range(self):
        return range_for(self.range_combo.currentData(),
                         custom=(self.from_edit.date().toPython(), self.to_edit.date().toPython()))

    def refresh(self, *_a) -> None:
        rng = self.current_range()
        data = analytics.load_data(self.db)
        res = analytics.compute(data, rng)
        prev_rng = previous_range(rng)
        prev = analytics.compute(data, prev_rng) if prev_rng else None
        self.result = res

        # ---- headline cards: count, share of bids, change vs the previous period
        n = res.submitted
        med = res.median_days_to_response
        pct = {
            "submitted": f"{res.awaiting_reply} awaiting a reply now",
            "responses": f"{res.reply.text} reply rate" + ("" if med is None else f" · median {med:.1f} days"),
            "screening_invites": f"{res.screening.text} of bids",
            "technical_invites": f"{res.technical.text} of bids",
            "offers": f"{res.offer.text} of bids",
        }
        for key, label in pct.items():
            value, sub, delta = self._stat[key]
            value.setText(str(getattr(res, key)))
            sub.setText(label)
            if prev is None:
                delta.setText("")
            else:
                text, color = delta_text(getattr(res, key), getattr(prev, key))
                delta.setText(f"{text}  vs previous period")
                delta.setStyleSheet(f"color: {color}; font-size: 11.5px; font-weight: 600;")
        self.subtitle.setText(f"{rng.label} · {n} bid{'s' if n != 1 else ''} sent in this period")

        # ---- charts
        apps, reps, inv = res.applications_series, res.replies_series, res.invites_series
        self.line.set_data(apps.labels, [("Bids", apps.values), ("Replies", reps.values), ("Interview calls", inv.values)])
        self.weekly.set_data(res.weekly.labels, res.weekly.rows())
        self.monthly.set_data(res.monthly_cohort.labels, res.monthly_cohort.rows())
        names = {"Applied": "Bids", "Replied": "Replies", "Screening": "Screening", "Technical": "Technical",
                 "Final": "Final", "Offer": "Offers"}
        self.funnel.set_data([
            (names.get(s.name, s.name), s.count, "100%" if i == 0 else f"{s.of_applied.text} of bids")
            for i, s in enumerate(res.funnel)])

        # ---- full metric list (collapsed by default)
        t = self._set
        t("imported", res.imported)
        t("processed", res.processed, "generated + failed + skipped")
        t("generated", res.generated)
        t("failed", res.failed)
        t("skipped_duplicates", res.skipped_duplicates)
        t("ready_not_applied", res.ready_not_applied, "right now")
        t("submitted", res.submitted)
        t("awaiting_reply", res.awaiting_reply, "right now")
        t("responses", res.responses)
        t("positive_responses", res.positive_responses)
        t("rejections", res.rejections)
        sent = analytics.bot_sent(self.db, rng)
        t("bot_sent", sent["total"], ", ".join(f"{k}: {v}" for k, v in sorted(sent.items()) if k != "total") or "none yet")
        t("screening_invites", res.screening_invites)
        t("screening_done", res.screening_done)
        t("technical_invites", res.technical_invites)
        t("technical_done", res.technical_done)
        t("final_invites", res.final_invites)
        t("offers", res.offers)
        for key in ("generation_success", "submission", "reply", "screening", "technical", "final", "offer"):
            rate = getattr(res, key)
            self._set(key, rate.text, rate.detail)
        self._set("days", "—" if med is None else f"{med:.1f}",
                  f"{len(res.days_to_response)} replies" if res.days_to_response else "no data")

    def _set(self, key: str, value, sub: str = "") -> None:
        v, s = self._tiles[key]
        v.setText(str(value))
        s.setText(sub)
