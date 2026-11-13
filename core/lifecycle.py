"""Event-sourced application lifecycle.

``job_events`` is append-only. Corrections are new events of type ``correction`` that point
at the event they amend (``corrects_id``) and either void it or override its time / notes.
``effective_events`` applies corrections; ``replay`` folds the effective events into
``Facts`` - the single definition of an application's current state, used both to refresh
the summary columns on ``jobs`` and by analytics.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from core.application_stage import (
    LEGACY_STAGE_MAP,
    Outcome,
    Response,
    Stage,
    outcome_from_value,
    stage_from_value,
)
from core.timeutil import parse_iso


class EV:
    IMPORTED = "imported"
    DOCS_GENERATED = "docs_generated"
    GENERATION_FAILED = "generation_failed"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    SKIPPED = "skipped"
    FILES_REGENERATED = "files_regenerated"
    APPLIED = "applied"
    AUTOFILLED = "autofilled"
    APPLY_BLOCKED = "apply_blocked"  # the bot stopped on this job (needs you / human check / failed)
    APPLY_DRY_RUN = "apply_dry_run"  # the bot filled and checked the form but did not send it
    STAGE_CHANGED = "stage_changed"
    MESSAGE = "message_received"
    MESSAGE_EDITED = "message_edited"
    INTERVIEW_SCHEDULED = "interview_scheduled"
    INTERVIEW_COMPLETED = "interview_completed"
    INTERVIEW_CANCELLED = "interview_cancelled"
    INTERVIEW_RESCHEDULED = "interview_rescheduled"
    INTERVIEW_UPDATED = "interview_updated"
    FOLLOWUP_SENT = "followup_sent"
    ACTION_ADDED = "action_added"
    ACTION_DONE = "action_completed"
    NOTE = "note"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    CLOSED = "position_closed"
    ACCEPTED = "offer_accepted"
    REOPENED = "reopened"
    ON_HOLD = "on_hold"
    RESUMED = "resumed"
    ARCHIVED = "archived"
    UNARCHIVED = "unarchived"
    DUPLICATE_OVERRIDE = "duplicate_override"
    CORRECTION = "correction"


TERMINAL_EVENTS = {
    EV.REJECTED: Outcome.REJECTED,
    EV.WITHDRAWN: Outcome.WITHDRAWN,
    EV.CLOSED: Outcome.CLOSED,
    EV.ACCEPTED: Outcome.ACCEPTED,
}
OUTCOME_EVENT = {v: k for k, v in TERMINAL_EVENTS.items()}

EVENT_LABELS = {
    EV.IMPORTED: "Job added to queue",
    EV.DOCS_GENERATED: "Documents generated",
    EV.GENERATION_FAILED: "Document generation failed",
    EV.SKIPPED_DUPLICATE: "Skipped: company already exists",
    EV.SKIPPED: "Skipped",
    EV.FILES_REGENERATED: "Documents regenerated",
    EV.APPLIED: "Application submitted",
    EV.AUTOFILLED: "Application form filled (not submitted)",
    EV.APPLY_BLOCKED: "Auto-apply stopped: needs you",
    EV.APPLY_DRY_RUN: "Auto-apply dry run (form filled, not sent)",
    EV.STAGE_CHANGED: "Stage changed",
    EV.MESSAGE: "Recruiter message received",
    EV.MESSAGE_EDITED: "Message record edited",
    EV.INTERVIEW_SCHEDULED: "Interview scheduled",
    EV.INTERVIEW_COMPLETED: "Interview completed",
    EV.INTERVIEW_CANCELLED: "Interview cancelled",
    EV.INTERVIEW_RESCHEDULED: "Interview rescheduled",
    EV.INTERVIEW_UPDATED: "Interview details edited",
    EV.FOLLOWUP_SENT: "Follow-up sent",
    EV.ACTION_ADDED: "Next action set",
    EV.ACTION_DONE: "Next action completed",
    EV.NOTE: "Note",
    EV.REJECTED: "Rejection received",
    EV.WITHDRAWN: "Application withdrawn",
    EV.CLOSED: "Position closed",
    EV.ACCEPTED: "Offer accepted",
    EV.REOPENED: "Application reopened",
    EV.ON_HOLD: "Put on hold",
    EV.RESUMED: "Resumed from hold",
    EV.ARCHIVED: "Archived",
    EV.UNARCHIVED: "Restored from archive",
    EV.DUPLICATE_OVERRIDE: "Duplicate-company rule overridden",
}


@dataclass
class Event:
    id: int
    job_id: str
    type: str
    when: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    edited: bool = False  # a correction changed its time or notes
    original_when: datetime | None = None
    voided: bool = False  # only present when requested via keep_voided


def row_to_raw(row: Any) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}
    return {
        "id": int(row["id"]),
        "job_id": row["job_id"],
        "type": row["event_type"],
        "occurred_at": row["occurred_at"],
        "payload": payload if isinstance(payload, dict) else {},
        "notes": row["notes"] or "",
        "corrects_id": row["corrects_id"],
    }


def effective_events(raw: Iterable[dict[str, Any]], keep_voided: bool = False) -> list[Event]:
    """Apply corrections; return events sorted by (time, id). Voided events are dropped
    unless ``keep_voided`` (audit views), in which case they carry ``voided=True``."""
    raw = sorted(raw, key=lambda r: r["id"])
    corrections: dict[int, list[dict[str, Any]]] = {}
    base: list[dict[str, Any]] = []
    for r in raw:
        if r["type"] == EV.CORRECTION and r.get("corrects_id") is not None:
            corrections.setdefault(int(r["corrects_id"]), []).append(r)
        else:
            base.append(r)
    out: list[Event] = []
    for r in base:
        when = parse_iso(r["occurred_at"])
        if when is None:
            continue
        original = when
        notes = r["notes"]
        payload = dict(r["payload"])
        edited = False
        voided = False
        for c in corrections.get(r["id"], []):
            cp = c["payload"]
            if cp.get("void"):
                voided = True
            if cp.get("restore"):
                voided = False
            if cp.get("occurred_at"):
                new = parse_iso(cp["occurred_at"])
                if new is not None:
                    when, edited = new, True
            if "notes" in cp:
                notes, edited = str(cp["notes"]), True
        if voided and not keep_voided:
            continue
        out.append(Event(r["id"], r["job_id"], r["type"], when, payload, notes, edited, original, voided))
    out.sort(key=lambda e: (e.when, e.id))
    return out


@dataclass
class Facts:
    stage: Stage = Stage.NOT_APPLIED
    applied_at: datetime | None = None
    outcome: Outcome | None = None
    outcome_at: datetime | None = None
    on_hold: bool = False
    response: Response = Response.NONE
    first_reach: dict[Stage, datetime] = field(default_factory=dict)
    first_response_at: datetime | None = None
    first_positive_at: datetime | None = None
    docs_generated_at: datetime | None = None
    last_activity_at: datetime | None = None

    @property
    def applied(self) -> bool:
        return self.applied_at is not None

    def reached(self, stage: Stage) -> datetime | None:
        """When the application first reached ``stage`` *or any later stage*."""
        times = [t for s, t in self.first_reach.items() if s.rank >= stage.rank]
        return min(times) if times else None


def _min(a: datetime | None, b: datetime) -> datetime:
    return b if a is None or b < a else a


def _payload_stage(e: Event) -> Stage | None:
    to = e.payload.get("to")
    if not to:
        return None  # legacy-format (v1) stage events are informational only
    try:
        return Stage(to)
    except ValueError:
        return None


def replay(events: Iterable[Event]) -> Facts:
    f = Facts()
    positive_msg = neutral_msg = False
    last_msg_negative = False
    for e in events:
        t = e.type
        if t not in (EV.ARCHIVED, EV.UNARCHIVED, EV.DUPLICATE_OVERRIDE, EV.MESSAGE_EDITED, EV.AUTOFILLED,
                     EV.APPLY_BLOCKED, EV.APPLY_DRY_RUN):
            f.last_activity_at = e.when if f.last_activity_at is None else max(f.last_activity_at, e.when)
        if t == EV.DOCS_GENERATED:
            f.docs_generated_at = f.docs_generated_at or e.when
        elif t == EV.APPLIED:
            # legacy rows carry the real submit time in the payload; an auditable edit overrides it
            when = e.when if e.edited else (parse_iso(e.payload.get("applied_at")) or e.when)
            f.applied_at = when if f.applied_at is None else min(f.applied_at, when)
            if f.stage == Stage.NOT_APPLIED:
                f.stage = Stage.APPLIED
            f.first_reach[Stage.APPLIED] = _min(f.first_reach.get(Stage.APPLIED), when)
        elif t == EV.STAGE_CHANGED:
            to = _payload_stage(e)
            if to is None:
                continue
            f.stage = to
            if to != Stage.NOT_APPLIED:
                f.first_reach[to] = _min(f.first_reach.get(to), e.when)
                if f.applied_at is None:
                    f.applied_at = e.when
                if to.rank >= Stage.RECRUITER.rank:
                    f.first_response_at = _min(f.first_response_at, e.when)
                    f.first_positive_at = _min(f.first_positive_at, e.when)
        elif t == EV.MESSAGE:
            kind = e.payload.get("kind", "")
            sentiment = e.payload.get("sentiment", "neutral")
            if kind == "automated":
                continue
            f.first_response_at = _min(f.first_response_at, e.when)
            last_msg_negative = sentiment == "negative"
            if sentiment == "positive":
                positive_msg = True
                f.first_positive_at = _min(f.first_positive_at, e.when)
            elif sentiment == "neutral":
                neutral_msg = True
        elif t == EV.INTERVIEW_SCHEDULED:
            f.first_response_at = _min(f.first_response_at, e.when)
            f.first_positive_at = _min(f.first_positive_at, e.when)
            positive_msg = True
        elif t in TERMINAL_EVENTS:
            f.outcome = TERMINAL_EVENTS[t]
            f.outcome_at = e.when
            if t == EV.REJECTED:
                f.first_response_at = _min(f.first_response_at, e.when)
        elif t == EV.REOPENED:
            f.outcome = None
            f.outcome_at = None
        elif t == EV.ON_HOLD:
            f.on_hold = True
        elif t == EV.RESUMED:
            f.on_hold = False

    if f.outcome == Outcome.REJECTED or last_msg_negative and not positive_msg:
        f.response = Response.NEGATIVE if f.first_response_at else Response.NONE
    elif positive_msg or f.stage.rank >= Stage.RECRUITER.rank:
        f.response = Response.POSITIVE
    elif neutral_msg:
        f.response = Response.NEUTRAL
    return f


def legacy_stage_target(label: str) -> tuple[Stage, Outcome | None]:
    return LEGACY_STAGE_MAP.get(label, (Stage.NOT_APPLIED, None))


def waiting_label(
    *,
    applied: bool,
    outcome: str,
    on_hold: bool,
    response: str,
    applied_at: str,
    now: datetime,
    ghost_after_days: int = 14,
    last_activity_at: str = "",
) -> str:
    """Human summary of the employer-response dimension for one application."""
    out = outcome_from_value(outcome)
    if out:
        return out.label
    if not applied:
        return "—"
    if on_hold:
        return "On hold"
    if response == Response.NEGATIVE.value:
        return "Negative reply"
    if response in (Response.POSITIVE.value, Response.NEUTRAL.value):
        return "Replied"
    since = parse_iso(last_activity_at) or parse_iso(applied_at)
    if since is not None and now - since >= timedelta(days=ghost_after_days):
        return "No response"
    return "Waiting"


__all__ = [
    "EV",
    "Event",
    "Facts",
    "effective_events",
    "replay",
    "row_to_raw",
    "waiting_label",
    "stage_from_value",
]
