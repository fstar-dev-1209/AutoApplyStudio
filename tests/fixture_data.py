"""A representative job search used to verify analytics end to end (all times UTC)."""

from core.application_stage import Outcome, Stage
from core.models import JobStatus
from tests.conftest import at, seed_job

NOW = at(2026, 2, 20, 12)


def build(db, svc):
    ids = {}
    # A: the full journey, two technical rounds, offer, accepted
    a = ids["A"] = seed_job(db, "Alpha", "Staff Engineer", created=at(2026, 1, 5, 9), generated=at(2026, 1, 5, 10))
    svc.mark_applied(a, when=at(2026, 1, 6))
    svc.record_message(a, received_at=at(2026, 1, 9), sender="Sam", kind="outreach", sentiment="positive",
                       summary="Intro call")
    s1 = svc.schedule_interview(a, kind="screening", scheduled_at=at(2026, 1, 12, 15), invited_at=at(2026, 1, 10))
    svc.update_interview(s1, status="Completed", completed_at=at(2026, 1, 12, 16))
    t1 = svc.schedule_interview(a, kind="technical", scheduled_at=at(2026, 1, 18, 15), invited_at=at(2026, 1, 15))
    svc.update_interview(t1, status="Completed", completed_at=at(2026, 1, 18, 16))
    t2 = svc.schedule_interview(a, kind="technical", scheduled_at=at(2026, 1, 25, 15), invited_at=at(2026, 1, 20))
    svc.update_interview(t2, status="Completed", completed_at=at(2026, 1, 25, 16))
    svc.set_stage(a, Stage.FINAL, when=at(2026, 2, 2))
    svc.set_stage(a, Stage.OFFER, when=at(2026, 2, 10))
    svc.set_outcome(a, Outcome.ACCEPTED, when=at(2026, 2, 12))
    # B: rejected without any interview
    b = ids["B"] = seed_job(db, "Bravo", "Backend Engineer", created=at(2026, 1, 5, 9), generated=at(2026, 1, 5, 10))
    svc.mark_applied(b, when=at(2026, 1, 7))
    svc.set_outcome(b, Outcome.REJECTED, when=at(2026, 1, 14))
    # C: screening, later rejected
    c = ids["C"] = seed_job(db, "Charlie", "Platform Engineer", created=at(2026, 1, 6, 9), generated=at(2026, 1, 6, 10))
    svc.mark_applied(c, when=at(2026, 1, 8))
    svc.record_message(c, received_at=at(2026, 1, 19), sender="Kim", kind="interview_invite", sentiment="positive",
                       summary="Screening invite")
    s2 = svc.schedule_interview(c, kind="screening", scheduled_at=at(2026, 1, 21, 15), invited_at=at(2026, 1, 20))
    svc.update_interview(s2, status="Completed", completed_at=at(2026, 1, 21, 16))
    svc.set_outcome(c, Outcome.REJECTED, when=at(2026, 2, 1))
    # D: applied, still waiting
    d = ids["D"] = seed_job(db, "Delta", "SRE", created=at(2026, 1, 20, 9), generated=at(2026, 1, 20, 10))
    svc.mark_applied(d, when=at(2026, 1, 21))
    # E: documents ready, not applied
    ids["E"] = seed_job(db, "Echo", "Developer", created=at(2026, 2, 3, 9), generated=at(2026, 2, 3, 10))
    # F: generation failed
    ids["F"] = seed_job(db, "Foxtrot", "Developer", created=at(2026, 1, 7, 9), generated=None,
                        status=JobStatus.FAILED, failed=at(2026, 1, 7, 10))
    # G: skipped duplicate company
    g = ids["G"] = seed_job(db, "Alpha", "Other Role", created=at(2026, 1, 9, 9), generated=None,
                            status=JobStatus.SKIPPED)
    db.add_event(g, "skipped_duplicate", {}, occurred_at="2026-01-09T10:00:00+00:00")
    # H: auto-acknowledgement only (not a reply), then withdrawn
    h = ids["H"] = seed_job(db, "Hotel", "Engineer", created=at(2026, 1, 10, 9), generated=at(2026, 1, 10, 10))
    svc.mark_applied(h, when=at(2026, 1, 11))
    svc.record_message(h, received_at=at(2026, 1, 11, 1), kind="automated", summary="Application received")
    svc.set_outcome(h, Outcome.WITHDRAWN, when=at(2026, 1, 30))
    # I: neutral reply; archived afterwards
    i = ids["I"] = seed_job(db, "India", "Engineer", created=at(2026, 1, 11, 9), generated=at(2026, 1, 11, 10))
    svc.mark_applied(i, when=at(2026, 1, 12))
    svc.record_message(i, received_at=at(2026, 1, 13), sender="Ops", kind="update", sentiment="neutral",
                       summary="Reviewing")
    svc.archive([i])
    # J: rejected, then reopened (counts as active, not as a rejection)
    j = ids["J"] = seed_job(db, "Juliet", "Engineer", created=at(2026, 2, 5, 9), generated=at(2026, 2, 5, 10))
    svc.mark_applied(j, when=at(2026, 2, 6))
    svc.set_outcome(j, Outcome.REJECTED, when=at(2026, 2, 8))
    svc.reopen(j, when=at(2026, 2, 12))
    return ids
