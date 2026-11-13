"""Application tracking operations (applied / stage / outcome / messages / interviews / actions).

Every public mutator runs in one transaction: it appends events, edits the supporting row
(message / interview / action) and refreshes the summary columns on ``jobs`` from the event
log, then notifies subscribers once. Nothing here is triggered by document generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import Any, Callable, Iterable

from core.application_db import LIGHT_COLUMNS, ApplicationDatabase
from core.application_stage import (
    STAGE_LABELS,
    Outcome,
    Response,
    Stage,
    stage_from_value,
)
from core.lifecycle import EV, EVENT_LABELS, OUTCOME_EVENT, effective_events, replay, waiting_label
from core.models import JobItem, JobStatus
from core.timeutil import DateRange, format_local, now_iso, parse_iso, to_iso, utc_now

INTERVIEW_KINDS = [
    ("screening", "Screening"),
    ("technical", "Technical"),
    ("hiring_manager", "Hiring manager"),
    ("final", "Final"),
    ("other", "Other"),
]
INTERVIEW_STATUSES = ["Scheduled", "Completed", "Cancelled", "Rescheduled"]
KIND_STAGE = {"screening": Stage.SCREENING, "technical": Stage.TECHNICAL, "final": Stage.FINAL}

MESSAGE_KINDS = [
    ("outreach", "Recruiter outreach"),
    ("interview_invite", "Interview invitation"),
    ("update", "Status update"),
    ("request_info", "Request for information"),
    ("rejection", "Rejection"),
    ("offer", "Offer"),
    ("automated", "Automated acknowledgement"),
    ("other", "Other"),
]
SENTIMENTS = [("positive", "Positive"), ("neutral", "Neutral"), ("negative", "Negative")]
DEFAULT_GHOST_DAYS = 14


class ApplicationError(ValueError):
    """A lifecycle operation that is not allowed in the current state."""


@dataclass
class AppRow:
    job: JobItem
    next_action: dict[str, Any] | None = None
    response_label: str = ""
    docs_ok: bool = False
    upcoming_interviews: int = 0


@dataclass
class TimelineEntry:
    event_id: int
    type: str
    label: str
    when: datetime
    detail: str = ""
    notes: str = ""
    edited: bool = False
    voided: bool = False
    original_when: datetime | None = None


@dataclass
class AppFilter:
    search: str = ""
    applied: bool | None = None  # True applied, False not applied, None any
    stage: Stage | None = None
    outcome: str = ""  # "" any | "active" | an Outcome value
    response: str = ""  # "" any | waiting | replied | positive | negative | no_response
    date_field: str = "applied"  # applied | activity
    date_range: DateRange | None = None
    include_archived: bool = False
    only_archived: bool = False


SORT_KEYS = ("company", "role", "applied", "stage", "response", "activity", "next_action", "docs")


def _stage_rank_for_sort(job: JobItem) -> int:
    return stage_from_value(job.app_stage).rank


class ApplicationService:
    def __init__(self, db: ApplicationDatabase, ghost_after_days: int = DEFAULT_GHOST_DAYS) -> None:
        self.db = db
        self.ghost_after_days = ghost_after_days
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------ plumbing

    def subscribe(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _changed(self) -> None:
        """Tell the listeners. They are screen code, and this is also called from the apply bot's own thread (when
        it marks a job applied), so the window sets ``deliver`` to run them on the screen's thread. Running screen
        code from another thread freezes the whole window."""
        def tell() -> None:
            for cb in list(self._listeners):
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    pass

        deliver = getattr(self, "deliver", None)
        if deliver is not None:
            deliver(tell)
        else:
            tell()

    def _job(self, job_id: str) -> JobItem:
        job = self.db.get(job_id)
        if job is None:
            raise ApplicationError("Job not found.")
        return job

    def _event(self, job_id: str, etype: str, when: str | datetime | None = None, payload=None, notes="") -> int:
        if isinstance(when, datetime):
            when = to_iso(when)
        return self.db.add_event(job_id, etype, payload, occurred_at=when or "", notes=notes)

    def _facts(self, job_id: str):
        return replay(self.db.effective_events(job_id))

    # ---------------------------------------------------------- lifecycle

    def mark_applied(self, job_id: str, when: str | datetime | None = None, note: str = "") -> bool:
        """Record that the application was actually submitted. Returns False if already applied."""
        with self.db.transaction():
            job = self._job(job_id)
            if job.applied_at:
                return False
            stamp = _as_iso(when)
            self._event(job_id, EV.APPLIED, stamp, {"applied_at": stamp}, note)
            self.db.refresh_summary(job_id)
        self._changed()
        return True

    def add_manual(
        self,
        company: str,
        role: str,
        url: str = "",
        source: str = "Manual",
        applied_on: str | datetime | None = None,
        stage: Stage = Stage.NOT_APPLIED,
        notes: str = "",
    ) -> str:
        """Track a job the user found or applied to outside the generation queue; returns its id.

        Nothing is generated and nothing is marked applied unless ``applied_on``/``stage`` say so.
        """
        company, role = company.strip(), role.strip()
        if not company:
            raise ApplicationError("Company is required.")
        job = JobItem.create(url.strip(), source=source.strip() or "Manual")
        if not job.url:
            job.url = job.origin_url = f"manual:{job.id}"
        job.company, job.role_title, job.notes = company, role, notes.strip()
        job.status = JobStatus.DONE  # appears in Applications; has no generated files
        job.stage = "Added manually"
        job.completed_at = now_iso()
        job.sort_order = self.db.max_sort_order() + 1
        with self.db.transaction():
            self.db.upsert(job)
            self._event(job.id, EV.IMPORTED, None, {"manual": True}, "Added manually")
            if stage != Stage.NOT_APPLIED:
                self._set_stage_inner(job.id, stage, _as_iso(applied_on), "", "manual")
            elif applied_on:
                self._event(job.id, EV.APPLIED, _as_iso(applied_on), {"applied_at": _as_iso(applied_on)}, "")
            self.db.refresh_summary(job.id)
        self._changed()
        return job.id

    def unmark_applied(self, job_id: str, reason: str = "marked applied by mistake") -> None:
        with self.db.transaction():
            facts = self._facts(job_id)
            if facts.stage.rank > Stage.APPLIED.rank or facts.outcome:
                raise ApplicationError(
                    "This application has progressed. Correct the stage instead of un-applying."
                )
            for e in self.db.effective_events(job_id):
                if e.type in (EV.APPLIED, EV.STAGE_CHANGED):
                    self._void(e.id, e.job_id, reason)
            self.db.refresh_summary(job_id)
        self._changed()

    def set_stage(
        self,
        job_id: str,
        stage: Stage,
        when: str | datetime | None = None,
        note: str = "",
        source: str = "manual",
    ) -> bool:
        """Move to ``stage`` (any direction: stages are a summary, not a forced path)."""
        if stage == Stage.NOT_APPLIED:
            self.unmark_applied(job_id)
            return True
        with self.db.transaction():
            changed = self._set_stage_inner(job_id, stage, _as_iso(when), note, source)
            if changed:
                self.db.refresh_summary(job_id)
        if changed:
            self._changed()
        return changed

    def _set_stage_inner(self, job_id: str, stage: Stage, stamp: str, note: str, source: str) -> bool:
        job = self._job(job_id)
        current = stage_from_value(job.app_stage)
        if current == stage and job.applied_at:
            return False
        if not job.applied_at:
            self._event(job_id, EV.APPLIED, stamp, {"applied_at": stamp, "implicit": True})
            if stage == Stage.APPLIED:
                return True
            current = Stage.APPLIED
        self._event(
            job_id, EV.STAGE_CHANGED, stamp, {"from": current.value, "to": stage.value, "source": source}, note
        )
        return True

    def set_outcome(
        self, job_id: str, outcome: Outcome, when: str | datetime | None = None, note: str = ""
    ) -> None:
        with self.db.transaction():
            self._set_outcome_inner(job_id, outcome, _as_iso(when), note)
            self.db.refresh_summary(job_id)
        self._changed()

    def _set_outcome_inner(self, job_id: str, outcome: Outcome, stamp: str, note: str) -> None:
        job = self._job(job_id)
        if outcome == Outcome.ACCEPTED and stage_from_value(job.app_stage).rank < Stage.OFFER.rank:
            self._set_stage_inner(job_id, Stage.OFFER, stamp, "", "auto: offer accepted")
        if job.outcome == outcome.value:
            raise ApplicationError(f"Already marked {outcome.label}.")
        self._event(job_id, OUTCOME_EVENT[outcome], stamp, {"from_stage": job.app_stage}, note)

    def reopen(self, job_id: str, when: str | datetime | None = None, note: str = "") -> None:
        with self.db.transaction():
            job = self._job(job_id)
            if not job.outcome:
                raise ApplicationError("This application has no final outcome to reopen.")
            self._event(job_id, EV.REOPENED, _as_iso(when), {"previous": job.outcome}, note)
            self.db.refresh_summary(job_id)
        self._changed()

    def set_hold(self, job_id: str, hold: bool, note: str = "") -> None:
        with self.db.transaction():
            job = self._job(job_id)
            if job.on_hold == hold:
                return
            self._event(job_id, EV.ON_HOLD if hold else EV.RESUMED, None, {}, note)
            self.db.refresh_summary(job_id)
        self._changed()

    def add_note(self, job_id: str, text: str, when: str | datetime | None = None) -> None:
        text = text.strip()
        if not text:
            raise ApplicationError("Note is empty.")
        with self.db.transaction():
            self._job(job_id)
            self._event(job_id, EV.NOTE, _as_iso(when), {}, text)
            self.db.refresh_summary(job_id)
        self._changed()

    def log_followup_sent(self, job_id: str, when: str | datetime | None = None, note: str = "") -> None:
        stamp = _as_iso(when)
        with self.db.transaction() as conn:
            self._job(job_id)
            self._event(job_id, EV.FOLLOWUP_SENT, stamp, {}, note)
            row = conn.execute(
                "SELECT id FROM job_actions WHERE job_id=? AND kind='follow_up' AND done_at='' "
                "ORDER BY due_date='' , due_date, id LIMIT 1",
                (job_id,),
            ).fetchone()
            if row:
                conn.execute("UPDATE job_actions SET done_at=? WHERE id=?", (stamp, row["id"]))
            self.db.refresh_summary(job_id)
        self._changed()

    def set_notes(self, job_id: str, text: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE jobs SET notes=? WHERE id=?", (text.strip(), job_id))
        self._changed()

    def set_duplicate_override(self, job_id: str, allow: bool) -> None:
        """Explicit, per-job opt-out of the one-company-folder rule (default stays enforced)."""
        with self.db.transaction() as conn:
            self._job(job_id)
            conn.execute("UPDATE jobs SET duplicate_override=? WHERE id=?", (1 if allow else 0, job_id))
            self._event(job_id, EV.DUPLICATE_OVERRIDE, None, {"allow": allow})
        self._changed()

    def archive(self, job_ids: Iterable[str], archived: bool = True) -> int:
        n = 0
        with self.db.transaction() as conn:
            for jid in job_ids:
                job = self.db.get(jid)
                if job is None or bool(job.archived_at) == archived:
                    continue
                conn.execute("UPDATE jobs SET archived_at=? WHERE id=?", (now_iso() if archived else "", jid))
                self._event(jid, EV.ARCHIVED if archived else EV.UNARCHIVED)
                n += 1
        if n:
            self._changed()
        return n

    # ----------------------------------------------------------- bulk (safe)

    def bulk_mark_applied(self, job_ids: Iterable[str], when=None) -> tuple[int, int]:
        """Mark applied where documents exist and the job is not already applied/closed."""
        done = skipped = 0
        stamp = _as_iso(when)
        with self.db.transaction():
            for jid in job_ids:
                job = self.db.get(jid)
                if job is None or job.applied_at or job.outcome or job.status != JobStatus.DONE:
                    skipped += 1
                    continue
                self._event(jid, EV.APPLIED, stamp, {"applied_at": stamp})
                self.db.refresh_summary(jid)
                done += 1
        if done:
            self._changed()
        return done, skipped

    def bulk_set_stage(self, job_ids: Iterable[str], stage: Stage, when=None) -> tuple[int, int]:
        """Forward-only stage moves on active applications; anything else is skipped."""
        done = skipped = 0
        stamp = _as_iso(when)
        with self.db.transaction():
            for jid in job_ids:
                job = self.db.get(jid)
                if job is None or job.outcome or stage.rank <= stage_from_value(job.app_stage).rank:
                    skipped += 1
                    continue
                if self._set_stage_inner(jid, stage, stamp, "", "bulk"):
                    self.db.refresh_summary(jid)
                    done += 1
        if done:
            self._changed()
        return done, skipped

    def bulk_set_outcome(self, job_ids: Iterable[str], outcome: Outcome, when=None) -> tuple[int, int]:
        done = skipped = 0
        stamp = _as_iso(when)
        with self.db.transaction():
            for jid in job_ids:
                job = self.db.get(jid)
                if job is None or job.outcome:
                    skipped += 1
                    continue
                self._set_outcome_inner(jid, outcome, stamp, "")
                self.db.refresh_summary(jid)
                done += 1
        if done:
            self._changed()
        return done, skipped

    # -------------------------------------------------- auditable corrections

    def _void(self, event_id: int, job_id: str, reason: str) -> None:
        self.db.add_event(job_id, EV.CORRECTION, {"void": True, "reason": reason}, corrects_id=event_id)

    def _target(self, event_id: int) -> dict[str, Any]:
        conn = self.db.conn
        row = conn.execute("SELECT * FROM job_events WHERE id=?", (event_id,)).fetchone()
        if row is None or row["event_type"] in (EV.CORRECTION, EV.IMPORTED):
            raise ApplicationError("This entry cannot be corrected.")
        return dict(row)

    def edit_event(
        self,
        event_id: int,
        occurred_at: str | datetime | None = None,
        notes: str | None = None,
        reason: str = "",
    ) -> None:
        """Amend an entry's time and/or notes. The original row is never modified."""
        with self.db.transaction():
            row = self._target(event_id)
            payload: dict[str, Any] = {"reason": reason}
            if occurred_at is not None:
                payload["occurred_at"] = _as_iso(occurred_at)
            if notes is not None:
                payload["notes"] = notes
            self.db.add_event(row["job_id"], EV.CORRECTION, payload, corrects_id=event_id)
            self.db.refresh_summary(row["job_id"])
        self._changed()

    def void_event(self, event_id: int, reason: str = "entered in error") -> None:
        with self.db.transaction():
            row = self._target(event_id)
            self._void(event_id, row["job_id"], reason)
            self.db.refresh_summary(row["job_id"])
        self._changed()

    def restore_event(self, event_id: int) -> None:
        with self.db.transaction():
            row = self._target(event_id)
            self.db.add_event(row["job_id"], EV.CORRECTION, {"restore": True}, corrects_id=event_id)
            self.db.refresh_summary(row["job_id"])
        self._changed()

    # ------------------------------------------------------------- messages

    def record_message(
        self,
        job_id: str,
        *,
        received_at: str | datetime | None = None,
        sender: str = "",
        kind: str = "other",
        sentiment: str = "neutral",
        summary: str = "",
        body: str = "",
        follow_up_required: bool = False,
    ) -> int:
        stamp = _as_iso(received_at)
        with self.db.transaction() as conn:
            job = self._job(job_id)
            cur = conn.execute(
                "INSERT INTO messages(job_id, received_at, sender, kind, sentiment, summary, body, "
                "follow_up_required, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, stamp, sender.strip(), kind, sentiment, summary.strip(), body, int(follow_up_required),
                 now_iso(), now_iso()),
            )
            mid = int(cur.lastrowid)
            self._message_event(job_id, mid, stamp, kind, sentiment, sender, summary)
            applied = bool(job.applied_at)
            if kind == "rejection" and not job.outcome:
                self._set_outcome_inner(job_id, Outcome.REJECTED, stamp, "")
            elif kind == "offer":
                self._set_stage_inner(job_id, Stage.OFFER, stamp, "", "auto: offer message")
            elif (
                applied
                and (sentiment == "positive" or kind in ("outreach", "interview_invite"))
                and sentiment != "negative"
                and kind not in ("automated", "rejection")
                and stage_from_value(job.app_stage).rank < Stage.RECRUITER.rank
            ):
                self._set_stage_inner(job_id, Stage.RECRUITER, stamp, "", "auto: recruiter replied")
            if follow_up_required:
                self._add_action_inner(
                    job_id, "follow_up", f"Reply to {sender.strip() or 'recruiter'}: {summary.strip()[:60]}".strip(), ""
                )
            self.db.refresh_summary(job_id)
        self._changed()
        return mid

    def _message_event(self, job_id, mid, stamp, kind, sentiment, sender, summary, edited=False) -> int:
        return self._event(
            job_id,
            EV.MESSAGE,
            stamp,
            {"message_id": mid, "kind": kind, "sentiment": sentiment, "sender": sender.strip(), "edited": edited},
            summary.strip(),
        )

    def update_message(self, message_id: int, **fields: Any) -> None:
        allowed = {"received_at", "sender", "kind", "sentiment", "summary", "body", "follow_up_required"}
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                raise ApplicationError("Message not found.")
            new = {k: row[k] for k in allowed}
            for k, v in fields.items():
                if k in allowed:
                    new[k] = _as_iso(v) if k == "received_at" else v
            conn.execute(
                "UPDATE messages SET received_at=?, sender=?, kind=?, sentiment=?, summary=?, body=?, "
                "follow_up_required=?, updated_at=? WHERE id=?",
                (new["received_at"], new["sender"], new["kind"], new["sentiment"], new["summary"], new["body"],
                 int(bool(new["follow_up_required"])), now_iso(), message_id),
            )
            # Void the superseded timeline entry and append the amended one (history is kept).
            for e in self.db.effective_events(row["job_id"]):
                if e.type == EV.MESSAGE and e.payload.get("message_id") == message_id:
                    self._void(e.id, row["job_id"], "message edited")
            self._message_event(
                row["job_id"], message_id, new["received_at"], new["kind"], new["sentiment"],
                new["sender"], new["summary"], edited=True,
            )
            self.db.refresh_summary(row["job_id"])
        self._changed()

    def list_messages(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            "SELECT * FROM messages WHERE job_id=? ORDER BY received_at DESC, id DESC", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ interviews

    def schedule_interview(
        self,
        job_id: str,
        *,
        kind: str = "screening",
        round_no: int | None = None,
        scheduled_at: str | datetime | None = None,
        tz_name: str = "",
        interviewer: str = "",
        meeting_url: str = "",
        notes: str = "",
        invited_at: str | datetime | None = None,
    ) -> int:
        invited = _as_iso(invited_at)
        sched = _as_iso(scheduled_at) if scheduled_at else ""
        with self.db.transaction() as conn:
            job = self._job(job_id)
            if round_no is None:
                n = conn.execute(
                    "SELECT COUNT(*) FROM interviews WHERE job_id=? AND kind=?", (job_id, kind)
                ).fetchone()[0]
                round_no = int(n) + 1
            cur = conn.execute(
                "INSERT INTO interviews(job_id, kind, round_no, scheduled_at, timezone, interviewer, meeting_url, "
                "status, notes, invited_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'Scheduled', ?,?,?,?)",
                (job_id, kind, round_no, sched, tz_name, interviewer.strip(), meeting_url.strip(), notes,
                 invited, now_iso(), now_iso()),
            )
            iid = int(cur.lastrowid)
            self._event(
                job_id, EV.INTERVIEW_SCHEDULED, invited,
                {"interview_id": iid, "kind": kind, "round": round_no, "scheduled_at": sched, "timezone": tz_name},
                notes,
            )
            self._advance_for_interview(job, kind, invited)
            self.db.refresh_summary(job_id)
        self._changed()
        return iid

    def _advance_for_interview(self, job: JobItem, kind: str, stamp: str) -> None:
        current = stage_from_value(self._job(job.id).app_stage)
        target = KIND_STAGE.get(kind, Stage.RECRUITER)
        if not job.outcome and (not job.applied_at or target.rank > current.rank):
            self._set_stage_inner(job.id, target, stamp, "", "auto: interview")

    def update_interview(self, interview_id: int, **fields: Any) -> None:
        allowed = {
            "kind", "round_no", "scheduled_at", "timezone", "interviewer", "meeting_url",
            "status", "result", "notes", "completed_at",
        }
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM interviews WHERE id=?", (interview_id,)).fetchone()
            if row is None:
                raise ApplicationError("Interview not found.")
            old = dict(row)
            new = {k: old[k] for k in allowed}
            for k, v in fields.items():
                if k in allowed:
                    new[k] = _as_iso(v) if k in ("scheduled_at", "completed_at") and v else (v or "")
            if new["status"] not in INTERVIEW_STATUSES:
                raise ApplicationError("Unknown interview status.")
            job_id = old["job_id"]
            status_changed = new["status"] != old["status"]
            if new["status"] == "Completed" and not new["completed_at"]:
                new["completed_at"] = now_iso()
            conn.execute(
                "UPDATE interviews SET kind=?, round_no=?, scheduled_at=?, timezone=?, interviewer=?, meeting_url=?, "
                "status=?, result=?, notes=?, completed_at=?, updated_at=? WHERE id=?",
                (new["kind"], int(new["round_no"] or 1), new["scheduled_at"], new["timezone"], new["interviewer"],
                 new["meeting_url"], new["status"], new["result"], new["notes"], new["completed_at"],
                 now_iso(), interview_id),
            )
            base = {"interview_id": interview_id, "kind": new["kind"], "round": new["round_no"]}
            if status_changed:
                if old["status"] == "Completed":
                    for e in self.db.effective_events(job_id):
                        if e.type == EV.INTERVIEW_COMPLETED and e.payload.get("interview_id") == interview_id:
                            self._void(e.id, job_id, "interview reopened")
                if new["status"] == "Completed":
                    self._event(job_id, EV.INTERVIEW_COMPLETED, new["completed_at"], base, new["result"])
                    job = self._job(job_id)
                    self._advance_for_interview(job, new["kind"], new["completed_at"])
                elif new["status"] == "Cancelled":
                    self._event(job_id, EV.INTERVIEW_CANCELLED, None, base, new["notes"])
                elif new["status"] == "Rescheduled":
                    self._event(job_id, EV.INTERVIEW_RESCHEDULED, None,
                                {**base, "from": old["scheduled_at"], "to": new["scheduled_at"]}, new["notes"])
            elif any(new[k] != old[k] for k in ("scheduled_at", "timezone", "interviewer", "meeting_url", "kind")):
                self._event(job_id, EV.INTERVIEW_UPDATED, None,
                            {**base, "before": {k: old[k] for k in allowed if old[k] != new[k]},
                             "after": {k: new[k] for k in allowed if old[k] != new[k]}}, "")
            self.db.refresh_summary(job_id)
        self._changed()

    def delete_interview(self, interview_id: int, reason: str = "entered in error") -> None:
        """Remove a mistaken interview record; its timeline entries are voided, not erased."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT job_id FROM interviews WHERE id=?", (interview_id,)).fetchone()
            if row is None:
                return
            for e in self.db.effective_events(row["job_id"]):
                if e.payload.get("interview_id") == interview_id and e.type.startswith("interview_"):
                    self._void(e.id, row["job_id"], reason)
            conn.execute("DELETE FROM interviews WHERE id=?", (interview_id,))
            self.db.refresh_summary(row["job_id"])
        self._changed()

    def list_interviews(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            "SELECT * FROM interviews WHERE job_id=? ORDER BY CASE WHEN scheduled_at='' THEN 1 ELSE 0 END, "
            "scheduled_at, id",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- actions

    def _add_action_inner(self, job_id: str, kind: str, description: str, due: str) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO job_actions(job_id, kind, description, due_date, created_at) VALUES (?,?,?,?,?)",
            (job_id, kind, description.strip(), due, now_iso()),
        )
        self._event(job_id, EV.ACTION_ADDED, None, {"action_id": int(cur.lastrowid), "kind": kind, "due": due},
                    description.strip())
        return int(cur.lastrowid)

    def add_action(self, job_id: str, description: str, due: date | str | None = None, kind: str = "next_action") -> int:
        if not description.strip():
            raise ApplicationError("Describe the action.")
        due_s = due.isoformat() if isinstance(due, date) else (due or "")
        with self.db.transaction():
            self._job(job_id)
            aid = self._add_action_inner(job_id, kind, description, due_s)
            self.db.refresh_summary(job_id)
        self._changed()
        return aid

    def set_next_action(self, job_id: str, description: str, due: date | str | None = None) -> int | None:
        """Replace the open 'next action' (one at a time); empty text just clears it."""
        due_s = due.isoformat() if isinstance(due, date) else (due or "")
        with self.db.transaction() as conn:
            self._job(job_id)
            conn.execute("DELETE FROM job_actions WHERE job_id=? AND kind='next_action' AND done_at=''", (job_id,))
            aid = None
            if description.strip():
                aid = self._add_action_inner(job_id, "next_action", description, due_s)
            self.db.refresh_summary(job_id)
        self._changed()
        return aid

    def complete_action(self, action_id: int, done: bool = True) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM job_actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                return
            conn.execute("UPDATE job_actions SET done_at=? WHERE id=?", (now_iso() if done else "", action_id))
            if done:
                self._event(row["job_id"], EV.ACTION_DONE, None, {"action_id": action_id}, row["description"])
            self.db.refresh_summary(row["job_id"])
        self._changed()

    def list_actions(self, job_id: str, include_done: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM job_actions WHERE job_id=?"
        if not include_done:
            sql += " AND done_at=''"
        rows = self.db.conn.execute(sql + " ORDER BY done_at != '', due_date='', due_date, id", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- reads

    def timeline(self, job_id: str, include_voided: bool = False) -> list[TimelineEntry]:
        events = effective_events(self.db.raw_events(job_id), keep_voided=include_voided)
        out = []
        for e in events:
            if e.type in (EV.ARCHIVED, EV.UNARCHIVED, EV.DUPLICATE_OVERRIDE) and not include_voided:
                continue
            out.append(
                TimelineEntry(
                    e.id, e.type, EVENT_LABELS.get(e.type, e.type.replace("_", " ").title()), e.when,
                    _event_detail(e), e.notes, e.edited, e.voided, e.original_when,
                )
            )
        return out

    def get_row(self, job_id: str, now: datetime | None = None) -> AppRow | None:
        job = self.db.get(job_id)
        if job is None:
            return None
        return self._rows([job], now or utc_now())[0]

    def _rows(self, jobs: list[JobItem], now: datetime) -> list[AppRow]:
        ids = [j.id for j in jobs]
        nxt: dict[str, dict[str, Any]] = {}
        upcoming: dict[str, int] = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            conn = self.db.conn
            for r in conn.execute(
                f"SELECT * FROM job_actions WHERE done_at='' AND job_id IN ({marks}) "
                "ORDER BY due_date='', due_date, id", ids,
            ):
                nxt.setdefault(r["job_id"], dict(r))
            for r in conn.execute(
                f"SELECT job_id, COUNT(*) n FROM interviews WHERE status IN ('Scheduled','Rescheduled') "
                f"AND scheduled_at >= ? AND job_id IN ({marks}) GROUP BY job_id", [to_iso(now), *ids],
            ):
                upcoming[r["job_id"]] = int(r["n"])
        rows = []
        for j in jobs:
            rows.append(
                AppRow(
                    j,
                    nxt.get(j.id),
                    self.response_label(j, now),
                    _docs_present(j),
                    upcoming.get(j.id, 0),
                )
            )
        return rows

    def response_label(self, job: JobItem, now: datetime | None = None) -> str:
        return waiting_label(
            applied=bool(job.applied_at), outcome=job.outcome, on_hold=job.on_hold, response=job.response_status,
            applied_at=job.applied_at, now=now or utc_now(), ghost_after_days=self.ghost_after_days,
            last_activity_at=job.last_activity_at,
        )

    def list_applications(
        self,
        flt: AppFilter | None = None,
        sort: str = "activity",
        descending: bool = True,
        tz: tzinfo | None = None,
        now: datetime | None = None,
    ) -> list[AppRow]:
        flt = flt or AppFilter()
        now = now or utc_now()
        clauses = ["(status = 'Done' OR applied_at != '' OR app_stage != 'not_applied')"]
        params: list[Any] = []
        if flt.only_archived:
            clauses.append("archived_at != ''")
        elif not flt.include_archived:
            clauses.append("archived_at = ''")
        if flt.search.strip():
            q = f"%{flt.search.strip().lower()}%"
            clauses.append("(LOWER(company) LIKE ? OR LOWER(role_title) LIKE ?)")
            params += [q, q]
        if flt.applied is True:
            clauses.append("applied_at != ''")
        elif flt.applied is False:
            clauses.append("applied_at = ''")
        if flt.stage is not None:
            clauses.append("app_stage = ?")
            params.append(flt.stage.value)
        if flt.outcome == "active":
            clauses.append("outcome = ''")
        elif flt.outcome:
            clauses.append("outcome = ?")
            params.append(flt.outcome)
        sql = f"SELECT {LIGHT_COLUMNS} FROM jobs WHERE " + " AND ".join(clauses)
        jobs = [self.db._row_to_job(r) for r in self.db.conn.execute(sql, params).fetchall()]

        if flt.date_range is not None and (flt.date_range.start or flt.date_range.end):
            attr = "applied_at" if flt.date_field == "applied" else "last_activity_at"
            jobs = [j for j in jobs if flt.date_range.contains(parse_iso(getattr(j, attr)))]
        rows = self._rows(jobs, now)
        if flt.response:
            rows = [r for r in rows if _response_matches(flt.response, r)]
        return _sort_rows(rows, sort, descending)

    def ready_count(self) -> int:
        """Documents generated but not yet applied (cheap: no rows are materialised)."""
        row = self.db.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE archived_at='' AND status='Done' AND applied_at='' AND outcome=''"
        ).fetchone()
        return int(row[0])

    def count_by_stage(self) -> dict[str, int]:
        rows = self.db.conn.execute(
            "SELECT app_stage, COUNT(*) FROM jobs WHERE archived_at='' AND "
            "(status='Done' OR applied_at != '' OR app_stage != 'not_applied') GROUP BY app_stage"
        ).fetchall()
        return {r[0]: int(r[1]) for r in rows}


# --------------------------------------------------------------------- helpers


def _as_iso(when: str | datetime | None) -> str:
    if when is None or when == "":
        return now_iso()
    if isinstance(when, datetime):
        return to_iso(when)
    dt = parse_iso(when)
    if dt is None:
        raise ApplicationError(f"Not a valid date/time: {when!r}")
    return to_iso(dt)


def _docs_present(job: JobItem) -> bool:
    from pathlib import Path

    return bool(job.generated_files) and any(Path(p).is_file() for p in job.generated_files)


def _response_matches(kind: str, row: AppRow) -> bool:
    j = row.job
    if not j.applied_at or j.outcome:
        return False
    label = row.response_label
    return {
        "waiting": label == "Waiting",
        "no_response": label == "No response",
        "replied": j.response_status in (Response.POSITIVE.value, Response.NEUTRAL.value),
        "positive": j.response_status == Response.POSITIVE.value,
        "negative": j.response_status == Response.NEGATIVE.value,
    }.get(kind, True)


def _sort_rows(rows: list[AppRow], key: str, descending: bool) -> list[AppRow]:
    def k(r: AppRow):
        j = r.job
        if key == "company":
            return (j.company or "").casefold()
        if key == "role":
            return (j.role_title or "").casefold()
        if key == "applied":
            return j.applied_at or ""
        if key == "stage":
            return (_stage_rank_for_sort(j), j.outcome)
        if key == "response":
            return r.response_label
        if key == "next_action":
            return (r.next_action or {}).get("due_date") or "9999"
        if key == "docs":
            return (r.docs_ok, j.status.value)
        return j.last_activity_at or j.updated_at or ""

    blank = ("", "9999")
    has_value = lambda r: key not in ("applied", "next_action") or k(r) not in blank  # noqa: E731
    present = [r for r in rows if has_value(r)]
    absent = [r for r in rows if not has_value(r)]  # blanks always sort last
    present.sort(key=k, reverse=descending)
    return present + absent


def _event_detail(e) -> str:
    p = e.payload
    t = e.type
    if t == EV.STAGE_CHANGED:
        to = stage_from_value(p.get("to"))
        frm = stage_from_value(p.get("from")) if p.get("from") else None
        src = p.get("source", "")
        extra = f" (from {frm.label})" if frm and frm != Stage.NOT_APPLIED else ""
        return f"{STAGE_LABELS[to]}{extra}" + (" · automatic" if src.startswith("auto") else "")
    if t == EV.MESSAGE:
        kinds = dict(MESSAGE_KINDS)
        who = p.get("sender") or "Recruiter"
        return f"{who} · {kinds.get(p.get('kind'), 'Message')} · {p.get('sentiment', 'neutral')}"
    if t.startswith("interview_"):
        kinds = dict(INTERVIEW_KINDS)
        base = f"{kinds.get(p.get('kind'), 'Interview')} round {p.get('round', 1)}"
        if t == EV.INTERVIEW_SCHEDULED and p.get("scheduled_at"):
            base += f" · {format_local(p['scheduled_at'])}"
        return base
    if t in (EV.REJECTED, EV.WITHDRAWN, EV.CLOSED, EV.ACCEPTED) and p.get("from_stage"):
        return f"at {STAGE_LABELS[stage_from_value(p['from_stage'])]}"
    if t == EV.ACTION_ADDED and p.get("due"):
        return f"due {p['due']}"
    if t == EV.DOCS_GENERATED and p.get("backfilled"):
        return "recovered from earlier data"
    return ""
