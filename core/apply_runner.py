"""The auto-apply loop: pick a finished job, apply to it in the browser, record exactly what happened."""

from __future__ import annotations

import json
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core import ats
from core.resume_finalize import employers_outside_history, has_experience
from core.apply_answers import make_answer_fn
from core.apply_facts import DEFAULTS, answered_form_questions, merge_form_questions
from core.applications import ApplicationService
from core.company_match import applied_matches, normalize_company
from core.discovery_runner import within_hours
from core.form_answers import Context
from core.form_filler import Files
from core.greenhouse import (
    DRY_RUN, FAILED, HUMAN_CHECK, NEEDS_YOU, SUBMITTED, ApplyOutcome, GreenhouseApplier,
)
from core.lifecycle import EV
from core.models import JobItem, JobStatus, QueueStore
from core.settings_store import GENERATION_CLAUDE_CHAT, AppSettings

OFF, IDLE, RUNNING, WAITING, STOPPED = "off", "idle", "running", "waiting", "stopped"
RETRY_FAILED_AFTER = timedelta(minutes=30)
MAX_ATTEMPTS_PER_JOB = 3


@dataclass
class ApplyStatus:
    state: str = OFF
    message: str = ""
    current: str = ""
    submitted_today: int = 0
    last_outcome: str = ""
    next_run: str = ""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class AttemptStore:
    """Rows of ``apply_attempts``."""

    def __init__(self, store: QueueStore) -> None:
        self.db = store.database

    def start(self, job_id: str, adapter: str, mode: str, now: datetime) -> int:
        with self.db.transaction() as conn:
            return conn.execute(
                "INSERT INTO apply_attempts(job_id, started_at, adapter, mode, state) VALUES (?,?,?,?, 'running')",
                (job_id, _iso(now), adapter, mode)).lastrowid

    def close_interrupted(self, now: datetime) -> int:
        """Attempts still marked running when the app starts were cut off by a restart or crash: mark them failed,
        so they neither show as in progress forever nor block the job (a failed job is retried later)."""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE apply_attempts SET state='failed', finished_at=?, reason=? WHERE state='running'",
                (_iso(now), "The app stopped while this attempt was running.")).rowcount

    def finish(self, attempt_id: int, outcome: ApplyOutcome, now: datetime) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE apply_attempts SET finished_at=?, state=?, reason=?, plan_json=?, needs_json=?, screenshot=?, "
                "confirmation=?, url=? WHERE id=?",
                (_iso(now), outcome.state, outcome.reason, json.dumps(outcome.plan), json.dumps(outcome.needs),
                 outcome.screenshot, outcome.confirmation, outcome.url, attempt_id))

    def for_job(self, job_id: str) -> list[dict]:
        rows = self.db.conn.execute("SELECT * FROM apply_attempts WHERE job_id = ? ORDER BY id", (job_id,))
        return [dict(r) for r in rows]

    def by_job(self) -> dict[str, list[dict]]:
        """Every job's attempts in one query (oldest first), for checking many jobs at once."""
        out: dict[str, list[dict]] = {}
        for r in self.db.conn.execute("SELECT * FROM apply_attempts ORDER BY id"):
            out.setdefault(r["job_id"], []).append(dict(r))
        return out

    def recent(self, limit: int = 100, state: str | None = None) -> list[dict]:
        sql, args = "SELECT a.*, j.company, j.role_title FROM apply_attempts a JOIN jobs j ON j.id = a.job_id", ()
        if state:
            sql, args = sql + " WHERE a.state = ?", (state,)
        return [dict(r) for r in self.db.conn.execute(sql + " ORDER BY a.id DESC LIMIT ?", (*args, limit))]

    def needs_you(self) -> list[dict]:
        """Jobs whose latest attempt stopped for you and that you have not dealt with yet."""
        rows = self.db.conn.execute(
            "SELECT a.*, j.company, j.role_title, j.url FROM apply_attempts a JOIN jobs j ON j.id = a.job_id "
            "WHERE a.state IN ('needs_you', 'human_check') AND a.resolved_at = '' AND j.applied_at = '' "
            "AND a.id = (SELECT MAX(id) FROM apply_attempts WHERE job_id = a.job_id) ORDER BY a.id DESC")
        return [dict(r) for r in rows]

    def resolve(self, attempt_id: int, now: datetime) -> None:
        """You dealt with it (or want another try): the bot may take the job again."""
        with self.db.transaction() as conn:
            conn.execute("UPDATE apply_attempts SET resolved_at = ? WHERE id = ?", (_iso(now), attempt_id))

    def dismiss(self, attempt_id: int, now: datetime) -> None:
        """You do not want the bot to try this job: it is settled and never picked again."""
        with self.db.transaction() as conn:
            conn.execute("UPDATE apply_attempts SET resolved_at = ?, state = 'dismissed' WHERE id = ?",
                         (_iso(now), attempt_id))

    def submitted_between(self, start: datetime, end: datetime) -> int:
        row = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM apply_attempts WHERE state='submitted' AND finished_at >= ? AND finished_at < ?",
            (_iso(start), _iso(end))).fetchone()
        return int(row["n"])

    def open_jobs(self) -> list[dict]:
        """Finished jobs that have not been applied to, each with its latest attempt when it has one."""
        rows = self.db.conn.execute(
            "SELECT j.id AS job_id, j.company, j.role_title, j.url, j.origin_url, "
            "a.id AS attempt_id, a.state, a.reason, a.finished_at, a.resolved_at, "
            "a.plan_json, a.needs_json, a.screenshot, a.mode "
            "FROM jobs j LEFT JOIN apply_attempts a ON a.id = ("
            "SELECT MAX(id) FROM apply_attempts WHERE job_id = j.id) "
            "WHERE j.status = 'Done' AND j.applied_at = '' AND j.archived_at = '' "
            "ORDER BY COALESCE(a.finished_at, j.updated_at) DESC")
        return [dict(r) for r in rows]


def pick_files(job: JobItem) -> Files:
    """The resume (PDF preferred) and cover letter among the job's generated documents that exist on disk."""
    paths = [Path(p) for p in job.generated_files if Path(p).is_file()]
    cover = next((p for p in paths if "cover" in p.name.lower()), None)
    others = [p for p in paths if p != cover]
    resume = next((p for p in others if p.suffix.lower() == ".pdf"), None) or next(
        (p for p in others if p.suffix.lower() in {".docx", ".doc"}), None)
    return Files(resume=str(resume) if resume else "", cover=str(cover) if cover else "")


class ApplyRunner:
    def __init__(
        self,
        store: QueueStore,
        apps: ApplicationService,
        settings: Callable[[], AppSettings],
        host: Callable[[], object],
        *,
        ask_chat=None,
        alert: Callable[[str, str], None] = lambda title, body: None,
        applier_factory: Callable[..., object] | None = None,
        shots_dir: Path | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        pause: Callable[[float, float], None] = lambda lo, hi: time.sleep(random.uniform(lo, hi)),
        rng: random.Random | None = None,
        mail=None,
        debug_log: Callable[[str], None] = lambda text: None,
        save_settings: Callable[[], None] | None = None,
    ) -> None:
        self.store, self.apps, self._settings, self._host = store, apps, settings, host
        self.attempts = AttemptStore(store)
        self._ask_chat, self._alert = ask_chat, alert
        self._factory = applier_factory or self._default_applier
        self._shots, self._now, self._pause = shots_dir, now, pause
        self._rng = rng or random.Random()
        self._mail = mail  # object with snapshot(context) / fetch(context, before, company), or None
        self._debug_log = debug_log
        self._save_settings = save_settings
        self.status = ApplyStatus()
        self._kill = threading.Event()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._manual: deque[tuple[str, bool]] = deque()
        self._manual_lock = threading.Lock()

    # ------------------------------------------------------------------ thread and kill switch

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._kill.clear()
        try:
            self.attempts.close_interrupted(self._now())
        except Exception:  # noqa: BLE001 - housekeeping must not stop the bot from starting
            pass
        self._thread = threading.Thread(target=self._loop, name="auto-apply", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=15)

    def kill(self) -> None:
        """Stop applying right now: the job in progress is abandoned before its Submit click."""
        self._kill.set()
        self.status.state, self.status.message = STOPPED, "Stopped by you. Press Resume to continue."

    def resume(self) -> None:
        self._kill.clear()
        self._wake.set()

    @property
    def killed(self) -> bool:
        return self._kill.is_set()

    def wake(self) -> None:
        self._wake.set()

    def request_now(self, job_id: str, *, submit: bool) -> None:
        """Apply to this job on the next loop turn, even when auto apply is off or outside the active hours."""
        if self.killed:
            self.resume()
        with self._manual_lock:
            self._manual = deque(item for item in self._manual if item[0] != job_id)
            self._manual.append((job_id, submit))
        self.wake()

    def _manual_waiting(self) -> bool:
        with self._manual_lock:
            return bool(self._manual)

    def _take_manual(self) -> ApplyOutcome | None:
        while True:
            with self._manual_lock:
                if not self._manual:
                    return None
                job_id, submit = self._manual.popleft()
            job = self.store.get(job_id)
            if job is None or job.applied_at or job.archived_at:
                continue
            for row in reversed(self.attempts.for_job(job.id)):
                if row["state"] in {"needs_you", "human_check"} and not row["resolved_at"]:
                    self.attempts.resolve(int(row["id"]), self._now())
                    break
            return self.apply_to(job, self._settings(), submit)

    def _loop(self) -> None:
        while not self._stop.is_set():
            worked = False
            try:
                worked = self.run_next() is not None
            except Exception as exc:  # noqa: BLE001 - the loop itself must survive anything
                self.status.state, self.status.message = IDLE, f"Error: {type(exc).__name__}: {exc}"
            delay = 1 if self._manual_waiting() else (self._rng.uniform(45, 120) if worked else 20)
            self._wake.wait(delay)
            self._wake.clear()

    # ------------------------------------------------------------------ choosing

    def _tz_day(self, s: AppSettings) -> tuple[datetime, datetime]:
        tz = ZoneInfo(s.bot_timezone)
        local = self._now().astimezone(tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    def eligible(self, job: JobItem, s: AppSettings, *, history: list[dict] | None = None,
                 applied: set[str] | None = None) -> str:
        """'' when the bot may try this job now, else why not (used by tests and the Auto Apply page).

        ``history`` (this job's attempts) and ``applied`` (normalised names of companies already applied to) can be
        passed in by a caller that checks many jobs, so each check does not query or scan everything again."""
        link = ats.classify(job.origin_url or job.url)
        if not link.supported:
            return "Not an application site the bot handles"
        if job.status != JobStatus.DONE or job.applied_at or job.archived_at:
            return "Not waiting to be applied to"
        if not pick_files(job).resume:
            return "No resume file on disk"
        if job.resume_json and not has_experience(job.resume_json):
            return "The resume has no work experience; generate it again"
        if s.apply_mode == "auto" and not (s.profile.work_history or "").strip():
            return "Add your real work history in Settings before auto-applying"
        if job.resume_json and employers_outside_history(job.resume_json, s.profile.work_history):
            return "The resume names an employer that is not in your work history; generate it again"
        history = self.attempts.for_job(job.id) if history is None else history
        if any(a["state"] in {"submitted", "expired", "dismissed"} for a in history):
            return "Already handled"
        if s.apply_mode == "dry_run" and any(a["state"] == "dry_run" for a in history):
            return "Dry run already done"
        if len(history) >= MAX_ATTEMPTS_PER_JOB:
            return "Too many attempts"
        last = history[-1] if history else None
        if last and last["state"] in {"needs_you", "human_check"} and not last["resolved_at"]:
            return "Waiting for you"
        if last and last["state"] == "failed":
            finished = datetime.fromisoformat(last["finished_at"] or last["started_at"])
            if self._now() - finished < RETRY_FAILED_AFTER:
                return "Failed recently; retrying later"
        if job.company and not job.duplicate_override:
            if applied is not None:
                if normalize_company(job.company) in applied:
                    return "Already applied to this company"
            elif applied_matches(self.store, job.company, exclude_id=job.id):
                return "Already applied to this company"
        return ""

    def pick_next(self, s: AppSettings) -> JobItem | None:
        jobs = self.store.jobs()
        applied = {normalize_company(j.company) for j in jobs if j.applied_at and j.company} - {""}
        history = self.attempts.by_job()
        done = [j for j in jobs if j.status == JobStatus.DONE and not j.applied_at]
        done.sort(key=lambda j: j.completed_at or j.updated_at, reverse=True)  # newest first: speed matters
        for job in done:
            if not self.eligible(job, s, history=history.get(job.id, []), applied=applied):
                return job
        return None

    # ------------------------------------------------------------------ one application

    def run_next(self) -> ApplyOutcome | None:
        manual = self._take_manual()
        if manual is not None:
            return manual
        s = self._settings()
        if s.apply_mode == "off":
            self.status.state, self.status.message = OFF, "Auto apply is switched off."
            return None
        if self.killed:
            return None
        if not within_hours(self._now(), s.bot_timezone, s.active_start_hour, s.active_end_hour):
            self.status.state, self.status.message = WAITING, "Outside the active hours."
            return None
        submit = s.apply_mode == "auto"
        start, end = self._tz_day(s)
        done_today = self.attempts.submitted_between(start, end)
        self.status.submitted_today = done_today
        if submit and done_today >= s.apply_daily_cap:
            self.status.state, self.status.message = WAITING, f"Daily limit of {s.apply_daily_cap} reached."
            return None
        job = self.pick_next(s)
        if job is None:
            self.status.state, self.status.message = IDLE, "Nothing waiting to be applied to."
            return None
        return self.apply_to(job, s, submit)

    def apply_to(self, job: JobItem, s: AppSettings, submit: bool) -> ApplyOutcome:
        label = f"{job.company or 'Company'} - {job.role_title or 'Role'}"
        self.status.state, self.status.current, self.status.message = RUNNING, label, f"Applying to {label}"
        attempt = self.attempts.start(job.id, "greenhouse", "auto" if submit else "dry_run", self._now())
        try:
            outcome = self._run_in_browser(job, s, submit)
        except Exception as exc:  # noqa: BLE001 - a browser failure is recorded against this job
            outcome = ApplyOutcome(FAILED, f"{type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}")
        self.attempts.finish(attempt, outcome, self._now())
        self._record(job, outcome, submit, label)
        self.status.current = ""
        self.status.last_outcome = f"{label}: {outcome.state}"
        if self.killed:  # a stop pressed mid-application must still read as stopped afterwards
            self.status.state, self.status.message = STOPPED, "Stopped by you. Press Resume to continue."
        else:
            self.status.state, self.status.message = IDLE, self.status.last_outcome
        return outcome

    def _run_in_browser(self, job: JobItem, s: AppSettings, submit: bool) -> ApplyOutcome:
        files = pick_files(job)
        chat = self._ask_chat if (self._ask_chat is not None and s.generation_mode == GENERATION_CLAUDE_CHAT) else None
        pending: list[tuple[str, list[str]]] = []
        form_ctx = Context(
            profile=s.profile, facts={**DEFAULTS, **s.apply_facts}, chat_enabled=chat is not None,
            cover_letter_text=str((job.resume_json or {}).get("cover_letter") or ""), resume=dict(job.resume_json or {}),
            has_resume=bool(files.resume), has_cover=bool(files.cover),
            form_answers=answered_form_questions(s.form_questions))
        answer = None
        if chat is not None:
            answer = make_answer_fn(job, chat, style_prompt=s.answer_prompt,
                                    on_conversation=lambda url, jid=job.id: self._remember_chat(jid, url))
        applier = self._factory(answer=answer, shots_dir=self._shots, notify=self._alert,
                                should_stop=self._kill.is_set, mail=self._mail, debug_log=self._debug_log)

        def remember(label: str, options: list[str]) -> None:
            pending.append((label, options))

        applier.remember = remember  # type: ignore[attr-defined]
        url = job.origin_url or job.url
        host = self._host()
        try:
            return host.run(lambda ctx: applier.apply(ctx, url, form_ctx=form_ctx, files=files, submit=submit,
                                                      tag=job.id[:8], company=job.company, role=job.role_title),
                            timeout=1800)
        finally:
            self._keep_questions(s, pending)

    def _default_applier(self, *, answer, shots_dir, notify, should_stop, mail, debug_log=lambda text: None):
        return GreenhouseApplier(
            answer=answer, shots_dir=shots_dir, notify=notify, should_stop=should_stop, debug_log=debug_log,
            snapshot_mail=mail.snapshot if mail else None, fetch_code=mail.fetch if mail else None)

    def _keep_questions(self, settings: AppSettings, pending: list[tuple[str, list[str]]]) -> None:
        """A required choice with no saved answer is added to Your answers, with the options the form offered."""
        if not pending:
            return
        merged = merge_form_questions(settings.form_questions, pending)
        if merged == list(settings.form_questions or []):
            return
        settings.form_questions = merged
        if self._save_settings is not None:
            try:
                self._save_settings()
            except OSError:
                pass

    def _remember_chat(self, job_id: str, url: str) -> None:
        job = self.store.get(job_id)
        if job is not None and job.chat_url != url:
            job.chat_url = url
            self.store.update(job)

    # ------------------------------------------------------------------ recording

    def _record(self, job: JobItem, outcome: ApplyOutcome, submit: bool, label: str) -> None:
        payload = {"state": outcome.state, "reason": outcome.reason, "screenshot": outcome.screenshot}
        if outcome.state == SUBMITTED:
            self.apps.mark_applied(job.id, note="Submitted automatically by the apply bot")
            return
        if outcome.state == DRY_RUN:
            self.store.add_event(job.id, EV.APPLY_DRY_RUN, payload)
            return
        self.store.add_event(job.id, EV.APPLY_BLOCKED, payload)
        if outcome.state in {NEEDS_YOU, HUMAN_CHECK}:  # failures retry by themselves; a closed posting needs no one
            self._alert(f"Needs you: {label}", outcome.reason or "The application needs your attention.")
