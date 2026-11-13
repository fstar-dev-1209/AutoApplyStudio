"""Upgrade of a real v1 database (the pre-tracking schema) must lose nothing."""

import json
import sqlite3

from core.application_db import ApplicationDatabase
from core.applications import ApplicationService
from core.migrations import LATEST_VERSION

V1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY, url TEXT NOT NULL, company TEXT NOT NULL DEFAULT '', role_title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
    origin_url TEXT NOT NULL DEFAULT '', job_description TEXT NOT NULL DEFAULT '', output_path TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '', skip_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, completed_at TEXT NOT NULL DEFAULT '', failed_stage TEXT NOT NULL DEFAULT '',
    retry_from TEXT NOT NULL DEFAULT '', claude_reuse_tab INTEGER NOT NULL DEFAULT 0,
    application_stage TEXT NOT NULL DEFAULT '', applied_at TEXT NOT NULL DEFAULT '',
    archived_at TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0,
    logs_json TEXT NOT NULL DEFAULT '[]', generated_files_json TEXT NOT NULL DEFAULT '[]', resume_json TEXT);
CREATE TABLE job_events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE);
INSERT INTO meta VALUES ('schema_version', '1');
"""

ROWS = [
    # id, company, status, legacy stage, applied_at, skip_reason
    ("j-done", "Acme", "Done", "Documents ready", "", ""),
    ("j-applied", "Beta", "Done", "Applied", "2025-11-03T15:00:00+00:00", ""),
    ("j-screen", "Gamma", "Done", "Screening", "2025-11-04 10:00:00", ""),
    ("j-rej", "Delta", "Done", "Rejected", "2025-11-05T10:00:00+00:00", ""),
    ("j-ghost", "Eps", "Done", "Ghosted", "2025-10-01T10:00:00+00:00", ""),
    ("j-dup", "Acme", "Skipped", "", "", "Company folder already exists: E:/Remote/Me/Acme"),
    ("j-fail", "Zeta", "Failed", "", "", ""),
    ("j-q", "Eta", "Queued", "", "", ""),
]


def make_v1(path):
    con = sqlite3.connect(path)
    con.executescript(V1_SCHEMA)
    for jid, company, status, legacy, applied, skip in ROWS:
        con.execute(
            "INSERT INTO jobs(id,url,company,role_title,status,created_at,updated_at,completed_at,application_stage,"
            "applied_at,skip_reason,output_path,generated_files_json,resume_json,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, f"https://x/{jid}", company, "Engineer", status, "2025-10-30T09:00:00+00:00",
             "2025-11-06T09:00:00+00:00", "2025-10-31T09:00:00+00:00" if status == "Done" else "", legacy, applied,
             skip, f"E:/Remote/Me/{company}/Engineer", json.dumps([f"E:/Remote/Me/{company}/Engineer/r.docx"]),
             json.dumps({"company_name": company}), "my notes"),
        )
    # legacy v1 events (old payload shapes)
    con.execute("INSERT INTO job_events(job_id,event_type,occurred_at,payload_json) VALUES "
                "('j-applied','applied','2025-11-03T15:00:00+00:00','{\"applied_at\": \"2025-11-03T15:00:00+00:00\"}')")
    con.execute("INSERT INTO job_events(job_id,event_type,occurred_at,payload_json) VALUES "
                "('j-screen','stage_changed','2025-11-04T11:00:00+00:00','{\"stage\": \"Screening\"}')")
    con.commit()
    con.close()


def test_v1_database_upgrades_without_data_loss(tmp_path):
    path = tmp_path / "studio.db"
    make_v1(path)
    db = ApplicationDatabase(path)
    assert db.schema_version() == LATEST_VERSION
    assert (tmp_path / "studio.pre-v2.bak").exists()  # safety copy of the v1 file

    jobs = {j.id: j for j in db.all_jobs()}
    assert set(jobs) == {r[0] for r in ROWS}  # every job still there
    done = jobs["j-done"]
    assert done.output_path == "E:/Remote/Me/Acme/Engineer"  # existing output paths valid
    assert done.generated_files == ["E:/Remote/Me/Acme/Engineer/r.docx"]
    assert done.resume_json == {"company_name": "Acme"} and done.notes == "my notes"
    assert done.application_stage == "Documents ready"  # legacy column preserved verbatim

    # generated != applied
    assert (done.app_stage, done.applied_at) == ("not_applied", "")
    assert jobs["j-applied"].app_stage == "applied" and jobs["j-applied"].applied_at.startswith("2025-11-03T15:00")
    assert jobs["j-screen"].app_stage == "screening"
    assert (jobs["j-rej"].outcome, jobs["j-rej"].app_stage, jobs["j-rej"].response_status) == (
        "rejected", "applied", "negative")
    assert jobs["j-ghost"].app_stage == "applied" and jobs["j-ghost"].outcome == ""
    assert jobs["j-q"].app_stage == "not_applied"

    # event history reconstructed for analytics, tagged as recovered
    types = {j: [e.type for e in db.effective_events(j)] for j in jobs}
    assert "docs_generated" in types["j-done"] and "imported" in types["j-done"]
    assert "skipped_duplicate" in types["j-dup"]
    assert "generation_failed" in types["j-fail"]
    assert types["j-applied"].count("applied") == 1  # the legacy event was reused, not duplicated
    assert db.raw_events("j-done")[-1]["payload"].get("backfilled") is True

    # Applications screen sees the right rows straight away
    svc = ApplicationService(db)
    visible = {r.job.id for r in svc.list_applications()}
    assert visible == {"j-done", "j-applied", "j-screen", "j-rej", "j-ghost"}
    db.close()


def test_migration_is_idempotent_and_survives_restart(tmp_path):
    path = tmp_path / "studio.db"
    make_v1(path)
    db = ApplicationDatabase(path)
    n = len(db.raw_events())
    db.close()
    for _ in range(2):  # reopen twice: no re-run, no duplicate events
        db = ApplicationDatabase(path)
        assert db.schema_version() == LATEST_VERSION and len(db.raw_events()) == n
        db.close()


def test_v3_adds_salary_columns_and_keeps_existing_jobs(tmp_path):
    path = tmp_path / "studio.db"
    make_v1(path)
    db = ApplicationDatabase(path)
    old = db.get("j-done")
    assert (old.salary_text, old.salary_min, old.salary_max, old.salary_period) == ("", 0, 0, "")
    old.salary_text, old.salary_min, old.salary_max, old.salary_period = "$120K - $150K/yr", 120000, 150000, "year"
    old.salary_currency = "USD"
    db.upsert(old)
    again = db.get("j-done")
    assert (again.salary_text, again.salary_min, again.salary_max, again.salary_period, again.salary_currency) == (
        "$120K - $150K/yr", 120000, 150000, "year", "USD")
    assert again.output_path == "E:/Remote/Me/Acme/Engineer"
    db.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "studio.db"
    make_v1(path)
    import core.application_db as appdb

    def boom(db):
        db.conn.execute("ALTER TABLE jobs ADD COLUMN app_stage TEXT NOT NULL DEFAULT 'not_applied'")
        raise RuntimeError("disk full")

    monkeypatch.setattr(appdb, "MIGRATIONS", [(2, boom)])
    try:
        ApplicationDatabase(path)
    except RuntimeError:
        pass
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    version = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    con.close()
    assert "app_stage" not in cols and version == "1"  # all-or-nothing


def test_v4_adds_the_chat_columns_without_touching_existing_rows(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(V1_SCHEMA)
    con.execute("INSERT INTO jobs(id, url, status, created_at, updated_at) VALUES ('j1','https://x','Done','2026-01-01','2026-01-01')")
    con.commit()
    con.close()

    db = ApplicationDatabase(path)
    try:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(jobs)")}
        assert {"chat_url", "chat_provider"} <= cols
        row = db._conn.execute("SELECT url, chat_url, chat_provider FROM jobs WHERE id='j1'").fetchone()
        assert (row["url"], row["chat_url"], row["chat_provider"]) == ("https://x", "", "")
        assert db.schema_version() == LATEST_VERSION
    finally:
        db.close()
