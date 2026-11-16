"""Which job goes first, for generation and for applying: the freshest posting, never the one finished last."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.apply_runner import ApplyRunner
from core.applications import ApplicationService
from core.job_order import application_order, generation_order, moment, posting_times
from core.models import JobItem, JobStatus, QueueStore
from core.queue_engine import QueueEngine
from core.settings_store import AppSettings, ProfileSettings, SettingsStore

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
GH = "https://boards.greenhouse.io/acme/jobs/{n}"


@pytest.fixture
def store(tmp_path):
    s = QueueStore(tmp_path / "studio.db")
    yield s
    s.database.close()


def add(store, n, *, posted=None, seen=None, status=JobStatus.QUEUED, completed="", discovered=True):
    """A queue job; with ``discovered`` it also has the row discovery leaves behind (posting date, first seen)."""
    job = JobItem.create(GH.format(n=n))
    job.company, job.role_title, job.status, job.completed_at = f"Co{n}", "Dev", status, completed
    store.add_many([job])
    if discovered:
        with store.database.transaction() as conn:
            conn.execute(
                "INSERT INTO discovered_jobs(title, company, location, source_url, apply_url, ats, ats_key, posted_at, "
                "state, reason, job_id, source, external_id, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("Dev", f"Co{n}", "", "", GH.format(n=n), "greenhouse", f"gh:{n}", posted or "", "queued", "", job.id,
                 "jobright", f"j{n}", (seen or NOW).isoformat()))
    return job


def hours_ago(h):
    return (NOW - timedelta(hours=h)).isoformat()


def test_jobs_found_by_discovery_go_freshest_posting_first(store):
    a, b, c = add(store, 1, posted=hours_ago(9)), add(store, 2, posted=hours_ago(1)), add(store, 3, posted=hours_ago(5))
    order = generation_order(store.jobs(), posting_times(store.database))
    assert [j.id for j in order] == [b.id, c.id, a.id]


def test_a_job_added_by_hand_goes_before_found_jobs_and_keeps_its_own_order(store):
    found = add(store, 1, posted=hours_ago(1))
    first, second = add(store, 2, discovered=False), add(store, 3, discovered=False)
    order = generation_order(store.jobs(), posting_times(store.database))
    assert [j.id for j in order] == [first.id, second.id, found.id]


def test_a_relative_date_is_read_against_when_it_was_first_seen_not_against_now(store):
    seen_long_ago = add(store, 1, posted="1 hour ago", seen=NOW - timedelta(days=2))
    seen_today = add(store, 2, posted="5 hours ago", seen=NOW)
    # "1 hour ago" two days ago is older than "5 hours ago" today, however long ago we are asking
    order = generation_order(store.jobs(), posting_times(store.database))
    assert [j.id for j in order] == [seen_today.id, seen_long_ago.id]


def test_a_found_job_with_no_posting_date_counts_from_when_it_was_first_seen(store):
    undated_recent = add(store, 1, posted="", seen=NOW)
    dated_old = add(store, 2, posted=hours_ago(30), seen=NOW)
    order = generation_order(store.jobs(), posting_times(store.database))
    assert [j.id for j in order] == [undated_recent.id, dated_old.id]


def test_applying_goes_by_the_posting_not_by_which_resume_finished_last(store):
    # The freshest posting was generated FIRST (so it finished earliest): it must still be applied to first.
    fresh = add(store, 1, posted=hours_ago(1), status=JobStatus.DONE, completed=hours_ago(3))
    older = add(store, 2, posted=hours_ago(6), status=JobStatus.DONE, completed=hours_ago(2))
    by_hand = add(store, 3, status=JobStatus.DONE, completed=hours_ago(0.5), discovered=False)
    order = application_order(store.jobs(), posting_times(store.database))
    assert [j.id for j in order] == [by_hand.id, fresh.id, older.id]  # a job with no posting date counts from its finish


def test_moment_reads_timestamps_and_never_raises():
    assert moment("2026-11-05T15:00:00+00:00") == NOW
    assert moment("2026-11-05T15:00:00") == NOW  # no zone: taken as UTC
    assert moment("") < NOW and moment("not a date") < NOW and moment(None) < NOW


def test_the_queue_worker_takes_the_freshest_waiting_job_next(tmp_path):
    settings = SettingsStore(tmp_path / "settings.json")
    store = QueueStore(tmp_path / "studio.db")
    try:
        engine = QueueEngine(store, settings)
        old, fresh = add(store, 1, posted=hours_ago(8)), add(store, 2, posted=hours_ago(1))
        assert engine._next_runnable().id == fresh.id
        by_hand = add(store, 3, discovered=False)
        assert engine._next_runnable().id == by_hand.id
        for job in (by_hand, fresh):
            job.status = JobStatus.DONE
            store.update(job)
        assert engine._next_runnable().id == old.id
        old.status = JobStatus.SKIPPED
        store.update(old)
        assert engine._next_runnable() is None
    finally:
        store.database.close()


def test_the_apply_bot_takes_the_freshest_posting_first(tmp_path):
    store = QueueStore(tmp_path / "studio.db")
    try:
        settings = AppSettings(apply_mode="auto", profile=ProfileSettings(name="Mary Ann Berg"))
        runner = ApplyRunner(store, ApplicationService(store.database), lambda: settings, lambda: None,
                             now=lambda: NOW)
        files = []
        for n in (1, 2):
            folder = tmp_path / f"job{n}"
            folder.mkdir()
            (folder / "Resume.pdf").write_bytes(b"x")
            files.append([str(folder / "Resume.pdf")])
        first = add(store, 1, posted=hours_ago(1), status=JobStatus.DONE, completed=hours_ago(4))
        second = add(store, 2, posted=hours_ago(7), status=JobStatus.DONE, completed=hours_ago(1))
        for job, paths in zip((first, second), files):
            job.generated_files = paths
            job.resume_json = {"experience": [{"company": "PayPal", "title": "Engineer"}]}
            store.update(job)
        assert runner.pick_next(settings).id == first.id
    finally:
        store.database.close()
