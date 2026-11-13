"""SQLite persistence for jobs, pipeline events and application tracking."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from core.lifecycle import Event, Facts, effective_events, replay, row_to_raw
from core.migrations import LATEST_VERSION, MIGRATIONS
from core.models import JobItem, JobStatus
from core.timeutil import now_iso, to_iso

SCHEMA_VERSION = LATEST_VERSION

# Columns owned by ApplicationService / the event log. The queue worker upserts a whole
# JobItem it loaded seconds (or minutes) earlier, so these are excluded from UPDATEs to
# keep a stale snapshot from overwriting tracking changes the user made in between.
TRACKING_COLUMNS = frozenset(
    {
        "applied_at",
        "archived_at",
        "notes",
        "application_stage",
        "app_stage",
        "outcome",
        "outcome_at",
        "response_status",
        "on_hold",
        "last_activity_at",
        "first_response_at",
    }
)


class _Result:
    """Fully materialised statement result (safe to read after the lock is released)."""

    def __init__(self, rows: list, lastrowid: int | None, rowcount: int) -> None:
        self._rows = rows
        self._pos = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list:
        rows, self._pos = self._rows[self._pos:], len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class _LockedConnection:
    """One sqlite3 connection is shared by the UI thread and the queue worker. Every statement runs
    under the database lock and is fetched before the lock is released, so a reader can never
    interleave with another thread's half-finished transaction or cursor."""

    def __init__(self, db: "ApplicationDatabase") -> None:
        self._db = db

    def execute(self, sql: str, params=()) -> _Result:
        with self._db._lock:
            cur = self._db._conn.execute(sql, params)
            rows = cur.fetchall() if cur.description is not None else []
            return _Result(rows, cur.lastrowid, cur.rowcount)


# Columns for list screens: everything except the large text/JSON blobs.
LIGHT_COLUMNS = (
    "id, url, company, role_title, status, stage, source, origin_url, output_path, error, skip_reason, created_at, "
    "updated_at, completed_at, failed_stage, retry_from, application_stage, applied_at, archived_at, notes, "
    "sort_order, generated_files_json, app_stage, outcome, outcome_at, response_status, on_hold, "
    "last_activity_at, first_response_at, duplicate_override, "
    "salary_text, salary_min, salary_max, salary_period, salary_currency, chat_url, chat_provider"
)


@dataclass
class JobPage:
    jobs: list[JobItem]
    total: int


class ReadOnlyReplicaError(RuntimeError):
    """A write was attempted on a viewer's copy of the controller's database."""


class ApplicationDatabase:
    """``read_only`` marks a viewer's replica: it is replaced wholesale by ``reload_from`` and never written
    (the connection itself refuses writes), because only the controller owns the real database."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        # Maps stored file paths to this computer's paths (viewer only; the controller keeps its own).
        self.path_mapper: Callable[[str], str] | None = None
        self._lock = threading.RLock()
        self._depth = 0
        self._boot = secrets.token_hex(4)  # changes on every start, so a restarted controller is never mistaken
        self._commits = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open()
        self._locked = _LockedConnection(self)
        self._init_schema()
        if read_only:
            self._conn.execute("PRAGMA query_only=ON")

    def _open(self) -> sqlite3.Connection:
        # autocommit: transactions are explicit (see ``transaction``)
        conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def conn(self) -> _LockedConnection:
        return self._locked

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- replication

    def change_token(self) -> str:
        """Changes whenever a write transaction commits (and on every restart). Two equal tokens from the same
        controller mean two equal databases, so a viewer can skip downloading."""
        with self._lock:
            return f"{self._boot}.{self._commits}"

    def snapshot_to(self, dest: Path) -> str:
        """Write a consistent single-file copy of the database to ``dest``; returns the token it matches."""
        with self._lock:
            token = self.change_token()
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal sidecar
            finally:
                target.close()
            return token

    def reload_from(self, source: Path) -> None:
        """Replace this database's contents with the file at ``source`` (it is moved into place)."""
        with self._lock:
            if self._depth:
                raise RuntimeError("Cannot replace the database inside a transaction.")
            self._conn.close()
            try:
                for suffix in ("-wal", "-shm"):
                    Path(str(self.path) + suffix).unlink(missing_ok=True)
                os.replace(source, self.path)
            finally:
                self._conn = self._open()
                if self.read_only:
                    self._conn.execute("PRAGMA query_only=ON")

    def finished_jobs_with_documents(self) -> list[tuple[str, list[str]]]:
        """(job id, stored file paths) of finished jobs whose resume JSON is kept; light, no blobs."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, generated_files_json FROM jobs WHERE status = ? AND resume_json IS NOT NULL "
                "AND resume_json != ''", (JobStatus.DONE.value,)).fetchall()
        out = []
        for row in rows:
            try:
                files = json.loads(row["generated_files_json"] or "[]")
            except json.JSONDecodeError:
                files = []
            out.append((row["id"], [self._map_path(p) for p in files]))
        return out

    def _map_path(self, path: str) -> str:
        return self.path_mapper(path) if self.path_mapper and path else path

    @contextmanager
    def transaction(self) -> Iterator[_LockedConnection]:
        """Atomic write scope; nests (inner scopes join the outer transaction)."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self._locked
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self._locked
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
                self._commits += 1
            finally:
                self._depth = 0

    # ------------------------------------------------------------------ schema

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    company TEXT NOT NULL DEFAULT '',
                    role_title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    origin_url TEXT NOT NULL DEFAULT '',
                    job_description TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    skip_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    failed_stage TEXT NOT NULL DEFAULT '',
                    retry_from TEXT NOT NULL DEFAULT '',
                    claude_reuse_tab INTEGER NOT NULL DEFAULT 0,
                    application_stage TEXT NOT NULL DEFAULT '',
                    applied_at TEXT NOT NULL DEFAULT '',
                    archived_at TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    logs_json TEXT NOT NULL DEFAULT '[]',
                    generated_files_json TEXT NOT NULL DEFAULT '[]',
                    resume_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_app_stage ON jobs(application_stage)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id)")
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')")
            self._migrate()

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        try:
            return int(row[0]) if row else 1
        except (TypeError, ValueError):
            return 1

    def _migrate(self) -> None:
        current = self.schema_version()
        pending = [(v, fn) for v, fn in MIGRATIONS if v > current]
        if not pending:
            return
        has_data = self._conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone() is not None
        if has_data:
            backup = self.path.with_name(f"{self.path.stem}.pre-v{pending[0][0]}.bak")
            if not backup.exists():
                dest = sqlite3.connect(str(backup))
                try:
                    self._conn.backup(dest)
                finally:
                    dest.close()
        for version, fn in pending:
            with self.transaction():
                fn(self)
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(version),),
                )

    def migrate_from_json(self, json_path: Path) -> int:
        if not json_path.exists():
            return 0
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        jobs = raw.get("jobs") if isinstance(raw, dict) else None
        if not isinstance(jobs, list):
            return 0
        imported = 0
        for item in jobs:
            if not isinstance(item, dict):
                continue
            job = JobItem.from_dict(item)
            if self.get(job.id) is None:
                with self.transaction():
                    self.upsert(job)
                    self.add_event(job.id, "imported", {"legacy_json": True}, occurred_at=job.created_at)
                    if job.status == JobStatus.DONE:
                        self.add_event(job.id, "docs_generated", {"backfilled": True},
                                       occurred_at=job.completed_at or job.updated_at)
                    self.refresh_summary(job.id)
                imported += 1
        if imported:
            backup = json_path.with_suffix(".json.bak")
            try:
                json_path.rename(backup)
            except OSError:
                pass
        return imported

    # -------------------------------------------------------------------- jobs

    def upsert(self, job: JobItem) -> None:
        row = self._job_to_row(job)
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        updates = ", ".join(
            f"{k}=excluded.{k}" for k in row.keys() if k != "id" and k not in TRACKING_COLUMNS
        )
        sql = f"INSERT INTO jobs ({cols}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}"
        with self.transaction() as conn:
            conn.execute(sql, row)

    def get(self, job_id: str) -> JobItem | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row is not None else None

    def delete(self, job_ids: list[str]) -> None:
        """Delete queue rows. Jobs that were ever applied to are history and are kept."""
        if not job_ids:
            return
        placeholders = ",".join("?" for _ in job_ids)
        with self.transaction() as conn:
            conn.execute(
                f"DELETE FROM jobs WHERE id IN ({placeholders}) AND applied_at = ''", job_ids
            )

    def all_jobs(self, include_archived: bool = True) -> list[JobItem]:
        sql = "SELECT * FROM jobs"
        if not include_archived:
            sql += " WHERE archived_at = ''"
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY sort_order ASC, created_at ASC").fetchall()
        return [self._row_to_job(r) for r in rows]

    def max_sort_order(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM jobs").fetchone()[0])

    def query_page(
        self,
        *,
        search: str = "",
        status_filter: str | None = None,
        application_stage: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> JobPage:
        clauses = ["1=1"]  # the queue shows every job; archiving only hides rows in Applications
        params: list[Any] = []
        if search:
            clauses.append(
                "(LOWER(company) LIKE ? OR LOWER(role_title) LIKE ? OR LOWER(url) LIKE ? OR LOWER(source) LIKE ?)"
            )
            q = f"%{search.lower()}%"
            params.extend([q, q, q, q])
        if status_filter == "processing":
            clauses.append("status IN (?, ?, ?)")
            params.extend([JobStatus.EXTRACTING.value, JobStatus.GENERATING.value, JobStatus.EXPORTING.value])
        elif status_filter:
            clauses.append("status = ?")
            params.append(status_filter)
        if application_stage:
            clauses.append("app_stage = ?")
            params.append(application_stage)
        where = " AND ".join(clauses)
        with self._lock:
            total = int(self._conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", params).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE {where} ORDER BY sort_order ASC, created_at ASC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        return JobPage(jobs=[self._row_to_job(r) for r in rows], total=total)

    def status_counts(self) -> dict[str, int]:
        """Queue-wide counts by processing status (one query, no row materialisation)."""
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def summary_counts(self) -> dict[str, int]:
        counts = self.status_counts()
        processing = sum(counts.get(s.value, 0) for s in (JobStatus.EXTRACTING, JobStatus.GENERATING, JobStatus.EXPORTING))
        with self._lock:
            applied = int(self._conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_at != ''").fetchone()[0])
        total = sum(counts.values())
        return {
            "total": total,
            "done": counts.get(JobStatus.DONE.value, 0),
            "skipped": counts.get(JobStatus.SKIPPED.value, 0),
            "needs": counts.get(JobStatus.NEEDS_JD.value, 0),
            "failed": counts.get(JobStatus.FAILED.value, 0),
            "processing": processing,
            "remaining": counts.get(JobStatus.QUEUED.value, 0),
            "applied": applied,
        }

    # ------------------------------------------------------------------ events

    def add_event(
        self,
        job_id: str,
        event_type: str,
        payload: dict | None = None,
        occurred_at: str = "",
        notes: str = "",
        corrects_id: int | None = None,
    ) -> int:
        when = to_iso_safe(occurred_at) if occurred_at else now_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO job_events(job_id, event_type, occurred_at, payload_json, notes, corrects_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, event_type, when, json.dumps(payload or {}), notes, corrects_id),
            )
            return int(cur.lastrowid)

    def raw_events(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if job_id is None:
                rows = self._conn.execute("SELECT * FROM job_events ORDER BY id").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
                ).fetchall()
        return [row_to_raw(r) for r in rows]

    def effective_events(self, job_id: str) -> list[Event]:
        return effective_events(self.raw_events(job_id))

    def all_effective_events(self) -> dict[str, list[Event]]:
        by_job: dict[str, list[dict[str, Any]]] = {}
        for r in self.raw_events():
            by_job.setdefault(r["job_id"], []).append(r)
        return {jid: effective_events(rows) for jid, rows in by_job.items()}

    def refresh_summary(self, job_id: str) -> Facts:
        """Recompute the tracking summary columns from the effective event history."""
        facts = replay(self.effective_events(job_id))

        def iso(dt) -> str:
            return to_iso(dt) if dt else ""

        with self.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET applied_at=?, app_stage=?, outcome=?, outcome_at=?, response_status=?, "
                "on_hold=?, last_activity_at=?, first_response_at=? WHERE id=?",
                (
                    iso(facts.applied_at),
                    facts.stage.value,
                    facts.outcome.value if facts.outcome else "",
                    iso(facts.outcome_at),
                    facts.response.value,
                    1 if facts.on_hold else 0,
                    iso(facts.last_activity_at),
                    iso(facts.first_response_at),
                    job_id,
                ),
            )
        return facts

    def list_events(self, job_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-first effective events as plain dicts (legacy inspector format)."""
        events = self.effective_events(job_id)[::-1][:limit]
        return [{"event_type": e.type, "occurred_at": to_iso(e.when), "payload": e.payload} for e in events]

    # -------------------------------------------------------------- row mapping

    def _job_to_row(self, job: JobItem) -> dict[str, Any]:
        return {
            "id": job.id,
            "url": job.url,
            "company": job.company,
            "role_title": job.role_title,
            "status": job.status.value,
            "stage": job.stage,
            "source": job.source,
            "origin_url": job.origin_url,
            "job_description": job.job_description,
            "output_path": job.output_path,
            "error": job.error,
            "skip_reason": job.skip_reason,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "completed_at": job.completed_at,
            "failed_stage": job.failed_stage,
            "retry_from": job.retry_from,
            "claude_reuse_tab": 1 if job.claude_reuse_tab else 0,
            "application_stage": job.application_stage,
            "applied_at": job.applied_at or "",
            "archived_at": job.archived_at or "",
            "notes": job.notes or "",
            "sort_order": job.sort_order,
            "logs_json": json.dumps(job.logs),
            "generated_files_json": json.dumps(job.generated_files),
            "resume_json": json.dumps(job.resume_json) if job.resume_json else None,
            "app_stage": job.app_stage or "not_applied",
            "outcome": job.outcome or "",
            "outcome_at": job.outcome_at or "",
            "response_status": job.response_status or "none",
            "on_hold": 1 if job.on_hold else 0,
            "last_activity_at": job.last_activity_at or "",
            "first_response_at": job.first_response_at or "",
            "duplicate_override": 1 if job.duplicate_override else 0,
            "salary_text": job.salary_text,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "salary_period": job.salary_period,
            "salary_currency": job.salary_currency,
            "chat_url": job.chat_url,
            "chat_provider": job.chat_provider,
        }

    def _row_to_job(self, row: sqlite3.Row) -> JobItem:
        def load(text: str | None, default: Any) -> Any:
            try:
                return json.loads(text) if text else default
            except json.JSONDecodeError:
                return default

        data = {k: row[k] for k in row.keys()}
        data["logs"] = load(data.get("logs_json"), [])
        data["generated_files"] = load(data.get("generated_files_json"), [])
        data["resume_json"] = load(data.get("resume_json"), None)
        data["claude_reuse_tab"] = bool(data.get("claude_reuse_tab"))
        data["on_hold"] = bool(data.get("on_hold"))
        data["duplicate_override"] = bool(data.get("duplicate_override"))
        data["generated_files"] = [self._map_path(p) for p in data["generated_files"]]
        data["output_path"] = self._map_path(data.get("output_path") or "")
        return JobItem.from_dict(data, recover=False)

    def recover_interrupted(self) -> int:
        """At startup nothing can be running: return jobs left mid-flight by a crash to the queue."""
        live = (JobStatus.EXTRACTING.value, JobStatus.GENERATING.value, JobStatus.EXPORTING.value)
        marks = ",".join("?" * len(live))
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE jobs SET status = ?, stage = CASE WHEN TRIM(COALESCE(job_description, '')) != '' "
                f"THEN 'Restored — ready to generate' ELSE 'Restored — awaiting processing' END "
                f"WHERE status IN ({marks})",
                (JobStatus.QUEUED.value, *live),
            )
        return cur.rowcount


def to_iso_safe(value: str) -> str:
    from core.timeutil import parse_iso

    dt = parse_iso(value)
    return to_iso(dt) if dt else value
