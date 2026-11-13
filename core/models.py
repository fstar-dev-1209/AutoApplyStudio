"""Job queue models and persistence."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class JobStatus(str, Enum):
    QUEUED = "Queued"
    EXTRACTING = "Extracting"
    GENERATING = "Generating"
    EXPORTING = "Exporting"
    NEEDS_JD = "Needs JD"
    DONE = "Done"
    SKIPPED = "Skipped"
    FAILED = "Failed"


ACTIVE_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.EXTRACTING,
    JobStatus.GENERATING,
    JobStatus.EXPORTING,
}

TERMINAL_STATUSES = {
    JobStatus.DONE,
    JobStatus.SKIPPED,
    JobStatus.FAILED,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobItem:
    id: str
    url: str
    company: str = ""
    role_title: str = ""
    status: JobStatus = JobStatus.QUEUED
    stage: str = "Waiting"
    source: str = ""
    origin_url: str = ""
    job_description: str = ""
    output_path: str = ""
    error: str = ""
    skip_reason: str = ""
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    completed_at: str = ""
    logs: list[str] = field(default_factory=list)
    generated_files: list[str] = field(default_factory=list)
    resume_json: dict[str, Any] | None = None
    failed_stage: str = ""
    retry_from: str = ""  # "export" | "generation" | ""
    claude_reuse_tab: bool = False  # retry generation without opening a new chat
    application_stage: str = ""
    applied_at: str = ""
    archived_at: str = ""
    notes: str = ""
    sort_order: int = 0
    # Recruitment tracking: read-only snapshot of the summary columns. These are written
    # only by core.applications.ApplicationService (never by queue-worker upserts).
    app_stage: str = "not_applied"
    outcome: str = ""
    outcome_at: str = ""
    response_status: str = "none"
    on_hold: bool = False
    last_activity_at: str = ""
    first_response_at: str = ""
    # Explicit user override of the one-company-folder duplicate rule (queue-owned).
    duplicate_override: bool = False
    # Pay as shown in the posting (``salary_text``) and, when it could be read, a parsed range.
    salary_text: str = ""
    salary_min: int = 0
    salary_max: int = 0
    salary_period: str = ""  # year | month | week | day | hour | ""
    salary_currency: str = ""
    # The web-chat conversation that produced this job's resume, so later questions (application form
    # answers) can continue in the same context. ``chat_provider`` is "claude" or "chatgpt".
    chat_url: str = ""
    chat_provider: str = ""

    @property
    def applied(self) -> bool:
        return bool(self.applied_at)

    @staticmethod
    def create(url: str, source: str = "") -> JobItem:
        return JobItem(id=str(uuid.uuid4()), url=url.strip(), origin_url=url.strip(), source=source)

    def touch(self) -> None:
        self.updated_at = _utc_now()

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]
        self.touch()

    def set_status(self, status: JobStatus, stage: str | None = None) -> None:
        self.status = status
        if stage is not None:
            self.stage = stage
        self.touch()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], recover: bool = True) -> JobItem:
        """`recover` resets mid-flight statuses; only for imports, never for live reads of the store."""
        status_raw = data.get("status", JobStatus.QUEUED.value)
        try:
            status = JobStatus(status_raw)
        except ValueError:
            status = JobStatus.QUEUED
        # Jobs that were mid-flight should return to queued or needs JD on restore
        if recover and status in {JobStatus.EXTRACTING, JobStatus.GENERATING, JobStatus.EXPORTING}:
            if data.get("job_description"):
                status = JobStatus.QUEUED
                stage = "Restored — ready to generate"
            else:
                status = JobStatus.QUEUED
                stage = "Restored — awaiting processing"
        else:
            stage = data.get("stage", "Waiting")
        return cls(
            id=str(data.get("id") or uuid.uuid4()),
            url=str(data.get("url") or ""),
            company=str(data.get("company") or ""),
            role_title=str(data.get("role_title") or ""),
            status=status,
            stage=stage,
            source=str(data.get("source") or ""),
            origin_url=str(data.get("origin_url") or data.get("url") or ""),
            job_description=str(data.get("job_description") or ""),
            output_path=str(data.get("output_path") or ""),
            error=str(data.get("error") or ""),
            skip_reason=str(data.get("skip_reason") or ""),
            created_at=str(data.get("created_at") or _utc_now()),
            updated_at=str(data.get("updated_at") or _utc_now()),
            completed_at=str(data.get("completed_at") or ""),
            logs=list(data.get("logs") or []),
            generated_files=list(data.get("generated_files") or []),
            resume_json=data.get("resume_json") if isinstance(data.get("resume_json"), dict) else None,
            failed_stage=str(data.get("failed_stage") or ""),
            retry_from=str(data.get("retry_from") or ""),
            claude_reuse_tab=bool(data.get("claude_reuse_tab")),
            application_stage=str(data.get("application_stage") or ""),
            applied_at=str(data.get("applied_at") or ""),
            archived_at=str(data.get("archived_at") or ""),
            notes=str(data.get("notes") or ""),
            sort_order=int(data.get("sort_order") or 0),
            app_stage=str(data.get("app_stage") or "not_applied"),
            outcome=str(data.get("outcome") or ""),
            outcome_at=str(data.get("outcome_at") or ""),
            response_status=str(data.get("response_status") or "none"),
            on_hold=bool(data.get("on_hold")),
            last_activity_at=str(data.get("last_activity_at") or ""),
            first_response_at=str(data.get("first_response_at") or ""),
            duplicate_override=bool(data.get("duplicate_override")),
            salary_text=str(data.get("salary_text") or ""),
            salary_min=int(data.get("salary_min") or 0),
            salary_max=int(data.get("salary_max") or 0),
            salary_period=str(data.get("salary_period") or ""),
            salary_currency=str(data.get("salary_currency") or ""),
            chat_url=str(data.get("chat_url") or ""),
            chat_provider=str(data.get("chat_provider") or ""),
        )


@dataclass
class BatchProgress:
    total: int = 0
    done: int = 0
    skipped: int = 0
    needs_jd: int = 0
    failed: int = 0
    remaining: int = 0
    processing: int = 0

    @classmethod
    def from_jobs(cls, jobs: list[JobItem]) -> BatchProgress:
        done = skipped = needs_jd = failed = processing = remaining = 0
        for job in jobs:
            if job.status == JobStatus.DONE:
                done += 1
            elif job.status == JobStatus.SKIPPED:
                skipped += 1
            elif job.status == JobStatus.NEEDS_JD:
                needs_jd += 1
            elif job.status == JobStatus.FAILED:
                failed += 1
            elif job.status in {
                JobStatus.EXTRACTING,
                JobStatus.GENERATING,
                JobStatus.EXPORTING,
            }:
                processing += 1
            elif job.status == JobStatus.QUEUED:
                remaining += 1
        return cls(
            total=len(jobs),
            done=done,
            skipped=skipped,
            needs_jd=needs_jd,
            failed=failed,
            remaining=remaining,
            processing=processing,
        )


class QueueStore:
    """SQLite-backed job and application store (single source of truth)."""

    def __init__(self, db_path: Path, legacy_json: Path | None = None, *, read_only: bool = False) -> None:
        from core.application_db import ApplicationDatabase

        self.path = db_path
        self.read_only = read_only
        self._legacy_json = None if read_only else legacy_json
        self._db = ApplicationDatabase(db_path, read_only=read_only)
        self._listeners: list[Callable[[], None]] = []

    @property
    def database(self):
        return self._db

    def subscribe(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass

    def load(self) -> None:
        if self.read_only:
            return  # a viewer's copy: nothing is recovered or imported here; the controller does that
        if self._legacy_json and self._legacy_json.exists():
            self._db.migrate_from_json(self._legacy_json)
        self._db.recover_interrupted()

    def save(self) -> None:
        """No-op — SQLite commits on each upsert."""

    def jobs(self) -> list[JobItem]:
        return self._db.all_jobs()

    def query_page(
        self,
        *,
        search: str = "",
        status_filter: str | None = None,
        application_stage: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ):
        return self._db.query_page(
            search=search,
            status_filter=status_filter,
            application_stage=application_stage,
            offset=offset,
            limit=limit,
        )

    def summary_counts(self) -> dict[str, int]:
        return self._db.summary_counts()

    def get(self, job_id: str) -> JobItem | None:
        return self._db.get(job_id)

    def urls(self) -> set[str]:
        return {job.url.rstrip("/") for job in self.jobs()}

    def add(self, job: JobItem, *, notify: bool = True) -> None:
        self.add_many([job], notify=notify)

    def add_many(self, jobs: list[JobItem], *, notify: bool = True) -> None:
        with self._db.transaction():
            base = self._db.max_sort_order()
            for i, job in enumerate(jobs, start=1):
                job.sort_order = base + i
                self._db.upsert(job)
                self._db.add_event(
                    job.id,
                    "imported",
                    {"url": job.url, "duplicate_override": job.duplicate_override},
                    occurred_at=job.created_at,
                )
        if notify:
            self._notify()

    def update(self, job: JobItem) -> None:
        self._db.upsert(job)
        self._notify()

    def remove(self, job_ids: list[str]) -> None:
        self._db.delete(job_ids)
        self._notify()

    def progress(self) -> BatchProgress:
        return BatchProgress.from_jobs(self.jobs())

    def add_event(self, job_id: str, event_type: str, payload: dict | None = None) -> None:
        """Append a pipeline event and refresh the job's tracking summary."""
        with self._db.transaction():
            self._db.add_event(job_id, event_type, payload)
            self._db.refresh_summary(job_id)

    def list_events(self, job_id: str) -> list[dict]:
        return self._db.list_events(job_id)
