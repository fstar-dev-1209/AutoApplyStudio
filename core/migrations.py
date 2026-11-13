"""Versioned SQLite migrations.

Rules: migrations only add (columns, tables, indexes, events). Nothing is dropped, renamed or
rewritten in place, so an older database always upgrades without losing data, and every
migration is safe to re-run. Each runs inside a single transaction.

v1  original schema (jobs, job_events, meta)
v2  application tracking: summary columns on ``jobs``, event notes/corrections,
    interviews, messages, actions; backfill of the event history from v1 rows.
v3  salary columns (text, min, max, period, currency) captured by the browser extension.
v4  chat_url / chat_provider: the web-chat conversation that wrote each resume.
v5  discovered_jobs: every posting a discovery source (Jobright) has shown us, and what became of it.
v6  apply_attempts: each try of the auto-apply bot on a job (decisions, what stopped it, screenshot).
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Callable

from core.application_stage import LEGACY_STAGE_MAP, Stage
from core.lifecycle import EV, OUTCOME_EVENT
from core.timeutil import parse_iso, to_iso

if TYPE_CHECKING:
    from core.application_db import ApplicationDatabase

LATEST_VERSION = 6


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _norm(ts: str) -> str:
    dt = parse_iso(ts)
    return to_iso(dt) if dt else ts


def migrate_v2(db: "ApplicationDatabase") -> None:
    conn = db.conn
    for name, ddl in (
        ("app_stage", "TEXT NOT NULL DEFAULT 'not_applied'"),
        ("outcome", "TEXT NOT NULL DEFAULT ''"),
        ("outcome_at", "TEXT NOT NULL DEFAULT ''"),
        ("response_status", "TEXT NOT NULL DEFAULT 'none'"),
        ("on_hold", "INTEGER NOT NULL DEFAULT 0"),
        ("last_activity_at", "TEXT NOT NULL DEFAULT ''"),
        ("first_response_at", "TEXT NOT NULL DEFAULT ''"),
        ("duplicate_override", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _add_column(conn, "jobs", name, ddl)
    _add_column(conn, "job_events", "notes", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "job_events", "corrects_id", "INTEGER")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            kind TEXT NOT NULL DEFAULT 'screening',
            round_no INTEGER NOT NULL DEFAULT 1,
            scheduled_at TEXT NOT NULL DEFAULT '',
            timezone TEXT NOT NULL DEFAULT '',
            interviewer TEXT NOT NULL DEFAULT '',
            meeting_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Scheduled',
            result TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            invited_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            received_at TEXT NOT NULL,
            sender TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'other',
            sentiment TEXT NOT NULL DEFAULT 'neutral',
            summary TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            follow_up_required INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            kind TEXT NOT NULL DEFAULT 'next_action',
            description TEXT NOT NULL,
            due_date TEXT NOT NULL DEFAULT '',
            done_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    for ddl in (
        "CREATE INDEX IF NOT EXISTS idx_events_job_time ON job_events(job_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_events_type_time ON job_events(event_type, occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_events_corrects ON job_events(corrects_id)",
        "CREATE INDEX IF NOT EXISTS idx_interviews_job ON interviews(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_interviews_sched ON interviews(scheduled_at)",
        "CREATE INDEX IF NOT EXISTS idx_messages_job ON messages(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_actions_job ON job_actions(job_id, done_at)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_app_stage2 ON jobs(app_stage)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_applied ON jobs(applied_at)",
    ):
        conn.execute(ddl)

    _backfill_events(db)


def _backfill_events(db: "ApplicationDatabase") -> None:
    conn = db.conn
    have: dict[str, set[str]] = {}
    for r in conn.execute("SELECT job_id, event_type FROM job_events"):
        have.setdefault(r["job_id"], set()).add(r["event_type"])
    staged: dict[str, set[str]] = {}
    for r in conn.execute("SELECT job_id, payload_json FROM job_events WHERE event_type = ?", (EV.STAGE_CHANGED,)):
        try:
            to = json.loads(r["payload_json"] or "{}").get("to")
        except json.JSONDecodeError:
            to = None
        if to:
            staged.setdefault(r["job_id"], set()).add(to)

    def add(job_id: str, etype: str, when: str, payload: dict | None = None) -> None:
        body = dict(payload or {})
        body["backfilled"] = True
        conn.execute(
            "INSERT INTO job_events(job_id, event_type, occurred_at, payload_json) VALUES (?,?,?,?)",
            (job_id, etype, _norm(when), json.dumps(body)),
        )

    for r in conn.execute("SELECT * FROM jobs").fetchall():
        jid = r["id"]
        types = have.get(jid, set())
        updated = r["updated_at"] or r["created_at"]
        if EV.IMPORTED not in types:
            add(jid, EV.IMPORTED, r["created_at"])
        status = r["status"]
        if status == "Done" and EV.DOCS_GENERATED not in types:
            add(jid, EV.DOCS_GENERATED, r["completed_at"] or updated)
        elif status == "Failed" and EV.GENERATION_FAILED not in types:
            add(jid, EV.GENERATION_FAILED, updated, {"error": r["error"]})
        elif status == "Skipped" and not types & {EV.SKIPPED, EV.SKIPPED_DUPLICATE}:
            dup = "company folder" in (r["skip_reason"] or "").lower()
            add(jid, EV.SKIPPED_DUPLICATE if dup else EV.SKIPPED, updated, {"reason": r["skip_reason"]})

        stage, outcome = LEGACY_STAGE_MAP.get(r["application_stage"] or "", (Stage.NOT_APPLIED, None))
        if (r["applied_at"] or stage != Stage.NOT_APPLIED or outcome) and EV.APPLIED not in types:
            add(jid, EV.APPLIED, r["applied_at"] or updated, {"applied_at": r["applied_at"] or updated})
        if stage.rank > Stage.APPLIED.rank and stage.value not in staged.get(jid, set()):
            add(jid, EV.STAGE_CHANGED, updated, {"to": stage.value, "legacy": r["application_stage"]})
        if outcome and OUTCOME_EVENT[outcome] not in types:
            add(jid, OUTCOME_EVENT[outcome], updated, {"legacy": r["application_stage"]})
        if r["archived_at"] and EV.ARCHIVED not in types:
            add(jid, EV.ARCHIVED, r["archived_at"])
        db.refresh_summary(jid)


def migrate_v3(db: "ApplicationDatabase") -> None:
    """Salary captured from the posting: the original text plus a parsed range."""
    for name, ddl in (
        ("salary_text", "TEXT NOT NULL DEFAULT ''"),
        ("salary_min", "INTEGER NOT NULL DEFAULT 0"),
        ("salary_max", "INTEGER NOT NULL DEFAULT 0"),
        ("salary_period", "TEXT NOT NULL DEFAULT ''"),
        ("salary_currency", "TEXT NOT NULL DEFAULT ''"),
    ):
        _add_column(db.conn, "jobs", name, ddl)


def migrate_v4(db: "ApplicationDatabase") -> None:
    """The chat conversation each resume was written in, for follow-up questions about the same job."""
    for name, ddl in (
        ("chat_url", "TEXT NOT NULL DEFAULT ''"),
        ("chat_provider", "TEXT NOT NULL DEFAULT ''"),
    ):
        _add_column(db.conn, "jobs", name, ddl)


def migrate_v5(db: "ApplicationDatabase") -> None:
    """Jobs found by the discovery sources (Jobright), whether or not they became queue jobs."""
    db.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discovered_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            apply_url TEXT NOT NULL DEFAULT '',
            ats TEXT NOT NULL DEFAULT '',
            ats_key TEXT NOT NULL DEFAULT '',
            posted_at TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            state TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            job_id TEXT NOT NULL DEFAULT '',
            UNIQUE (source, external_id)
        )
        """
    )
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_discovered_key ON discovered_jobs(ats_key)")
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_discovered_state ON discovered_jobs(state, first_seen_at)")


def migrate_v6(db: "ApplicationDatabase") -> None:
    """One row per try of the auto-apply bot on a job: what it decided, what stopped it, the proof."""
    db.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS apply_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            adapter TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'running',
            reason TEXT NOT NULL DEFAULT '',
            plan_json TEXT NOT NULL DEFAULT '[]',
            needs_json TEXT NOT NULL DEFAULT '[]',
            screenshot TEXT NOT NULL DEFAULT '',
            confirmation TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            resolved_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_job ON apply_attempts(job_id, started_at)")
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_state ON apply_attempts(state, started_at)")


MIGRATIONS: list[tuple[int, Callable[["ApplicationDatabase"], None]]] = [
    (2, migrate_v2),
    (3, migrate_v3),
    (4, migrate_v4),
    (5, migrate_v5),
    (6, migrate_v6),
]
