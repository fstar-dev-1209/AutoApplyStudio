"""Restart recovery and transactional writes."""

import pytest

from core.application_db import ApplicationDatabase
from core.application_stage import Outcome, Stage
from core.applications import ApplicationError, ApplicationService
from core.models import JobItem, JobStatus, QueueStore
from tests.conftest import at, seed_job
from tests.fixture_data import build


def test_tracking_survives_restart(tmp_path):
    path = tmp_path / "studio.db"
    db = ApplicationDatabase(path)
    svc = ApplicationService(db)
    ids = build(db, svc)
    svc.set_next_action(ids["D"], "Ping recruiter", "2026-02-25")
    snapshot = {j.id: (j.app_stage, j.outcome, j.applied_at, j.response_status, j.archived_at)
                for j in db.all_jobs()}
    interviews = svc.list_interviews(ids["A"])
    db.close()

    db2 = ApplicationDatabase(path)
    svc2 = ApplicationService(db2)
    assert {j.id: (j.app_stage, j.outcome, j.applied_at, j.response_status, j.archived_at)
            for j in db2.all_jobs()} == snapshot
    assert svc2.list_interviews(ids["A"]) == interviews and len(interviews) == 3
    assert svc2.list_applications(sort="next_action", descending=False)[0].next_action["description"] == "Ping recruiter"
    assert [t.type for t in svc2.timeline(ids["A"])][-1] == "offer_accepted"
    db2.close()


def test_failed_operation_rolls_back_every_write(db, svc, monkeypatch):
    jid = seed_job(db)
    svc.mark_applied(jid)
    events_before = len(db.raw_events(jid))
    stage_before = db.get(jid).app_stage

    def boom(job_id):
        raise RuntimeError("disk error mid-operation")

    monkeypatch.setattr(db, "refresh_summary", boom)
    with pytest.raises(RuntimeError):
        # writes an interview row + two events (interview, stage change) before the refresh fails
        svc.schedule_interview(jid, kind="technical")
    monkeypatch.undo()
    assert len(db.raw_events(jid)) == events_before  # events rolled back
    assert svc.list_interviews(jid) == []  # supporting row rolled back
    assert db.get(jid).app_stage == stage_before
    svc.schedule_interview(jid, kind="technical")  # and the database is still usable
    assert len(svc.list_interviews(jid)) == 1


def test_domain_errors_leave_no_partial_writes(db, svc):
    jid = seed_job(db)
    svc.mark_applied(jid)
    svc.set_outcome(jid, Outcome.REJECTED)
    n = len(db.raw_events(jid))
    with pytest.raises(ApplicationError):
        svc.set_outcome(jid, Outcome.REJECTED)
    with pytest.raises(ApplicationError):
        svc.unmark_applied(jid)
    assert len(db.raw_events(jid)) == n


def test_queue_worker_upsert_cannot_clobber_tracking(db, svc):
    """The engine holds a stale JobItem while the user records progress."""
    jid = seed_job(db)
    stale = db.get(jid)
    svc.mark_applied(jid, when=at(2026, 1, 8))
    svc.set_stage(jid, Stage.SCREENING, when=at(2026, 1, 9))
    svc.set_notes(jid, "call went well")
    stale.log("late engine write")
    stale.stage = "Completed"
    db.upsert(stale)
    fresh = db.get(jid)
    assert fresh.app_stage == "screening" and fresh.applied_at and fresh.notes == "call went well"
    assert fresh.logs[-1].endswith("late engine write")  # queue-owned fields still saved


def test_queue_store_records_import_and_listeners_fire(tmp_path):
    store = QueueStore(tmp_path / "s.db")
    fired = []
    store.subscribe(lambda: fired.append(1))
    job = JobItem.create("https://jobs.example.com/a")
    store.add_many([job])
    assert fired and [e["event_type"] for e in store.list_events(job.id)] == ["imported"]
    svc = ApplicationService(store.database)
    seen = []
    svc.subscribe(lambda: seen.append(1))
    job.status = JobStatus.DONE
    store.update(job)
    svc.mark_applied(job.id)
    assert seen == [1]  # immediate refresh signal after a tracking change
    store.database.close()


def test_concurrent_readers_and_writers_do_not_interfere(db, svc):
    """The UI thread and the queue worker share one connection."""
    import threading

    from core import analytics

    ids = [seed_job(db, f"Co{i}") for i in range(6)]
    errors, stop = [], threading.Event()

    def reader():
        try:
            while not stop.is_set():
                svc.list_applications()
                analytics.load_data(db)
                for jid in ids:
                    svc.list_actions(jid)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for t in readers:
        t.start()
    try:
        for n in range(15):
            for jid in ids:
                svc.add_note(jid, f"note {n}")
                if n == 3:
                    svc.mark_applied(jid)
                if n == 6:
                    svc.set_stage(jid, Stage.SCREENING)
                job = db.get(jid)
                job.log("worker write")
                db.upsert(job)  # what the queue worker does
    finally:
        stop.set()
        for t in readers:
            t.join(timeout=10)
    assert not errors, errors
    for jid in ids:
        job = db.get(jid)
        assert job.app_stage == "screening" and job.applied_at
        assert [e.type for e in db.effective_events(jid)].count("note") == 15


def test_in_flight_status_survives_reads_and_resets_only_on_recover(tmp_path):
    from core.application_db import ApplicationDatabase
    from core.models import JobItem, JobStatus

    db = ApplicationDatabase(tmp_path / "s.db")
    job = JobItem(id="j1", url="https://x.example/1", job_description="jd")
    job.set_status(JobStatus.GENERATING, "Generating documents")
    db.upsert(job)

    got = db.get(job.id)
    assert got.status == JobStatus.GENERATING and got.stage == "Generating documents"
    assert db.all_jobs()[0].status == JobStatus.GENERATING

    assert db.recover_interrupted() == 1
    got = db.get(job.id)
    assert got.status == JobStatus.QUEUED and got.stage == "Restored — ready to generate"
