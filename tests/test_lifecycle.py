import pytest

from core.application_stage import Outcome, Stage
from core.applications import AppFilter, ApplicationError
from core.lifecycle import EV, effective_events, replay
from core.timeutil import parse_iso, to_iso
from tests.conftest import at, seed_job


def facts(db, jid):
    return replay(db.effective_events(jid))


def test_generation_is_not_an_application(db, svc):
    jid = seed_job(db)
    job = db.get(jid)
    assert job.applied_at == "" and job.app_stage == "not_applied"
    rows = svc.list_applications()
    assert [r.job.id for r in rows] == [jid]  # visible in Applications automatically
    assert svc.list_applications(AppFilter(applied=True)) == []


def test_mark_applied_records_date_and_is_idempotent(db, svc):
    jid = seed_job(db)
    assert svc.mark_applied(jid, when=at(2026, 1, 8, 14)) is True
    assert svc.mark_applied(jid, when=at(2026, 1, 9)) is False
    job = db.get(jid)
    assert parse_iso(job.applied_at) == at(2026, 1, 8, 14)
    assert job.app_stage == Stage.APPLIED.value


def test_setting_a_later_stage_implies_applied(db, svc):
    jid = seed_job(db)
    svc.set_stage(jid, Stage.SCREENING, when=at(2026, 1, 12))
    job = db.get(jid)
    assert job.app_stage == "screening"
    assert parse_iso(job.applied_at) == at(2026, 1, 12)
    types = [e.type for e in db.effective_events(jid)]
    assert types.count(EV.APPLIED) == 1


def test_stages_can_be_skipped(db, svc):
    jid = seed_job(db)
    svc.mark_applied(jid, when=at(2026, 1, 6))
    svc.set_stage(jid, Stage.TECHNICAL, when=at(2026, 1, 20))
    f = facts(db, jid)
    assert Stage.SCREENING not in f.first_reach
    assert f.reached(Stage.SCREENING) == at(2026, 1, 20)  # "reached or beyond"


def test_multiple_technical_interviews(db, svc):
    jid = seed_job(db)
    svc.mark_applied(jid, when=at(2026, 1, 6))
    i1 = svc.schedule_interview(jid, kind="technical", scheduled_at=at(2026, 1, 20, 15), tz_name="America/New_York",
                                invited_at=at(2026, 1, 15))
    i2 = svc.schedule_interview(jid, kind="technical", scheduled_at=at(2026, 1, 27, 15), invited_at=at(2026, 1, 22))
    rows = svc.list_interviews(jid)
    assert [r["round_no"] for r in rows] == [1, 2]
    svc.update_interview(i1, status="Completed", result="Passed", completed_at=at(2026, 1, 20, 16))
    svc.update_interview(i2, status="Completed", completed_at=at(2026, 1, 27, 16))
    done = [e for e in db.effective_events(jid) if e.type == EV.INTERVIEW_COMPLETED]
    assert len(done) == 2
    assert db.get(jid).app_stage == "technical"  # auto-advance, once


def test_reverting_a_completed_interview_voids_its_completion(db, svc):
    jid = seed_job(db)
    svc.mark_applied(jid, when=at(2026, 1, 6))
    iid = svc.schedule_interview(jid, kind="screening")
    svc.update_interview(iid, status="Completed")
    svc.update_interview(iid, status="Scheduled")
    assert not [e for e in db.effective_events(jid) if e.type == EV.INTERVIEW_COMPLETED]


def test_rejection_without_interview_and_at_a_later_stage(db, svc):
    early = seed_job(db, "Early")
    svc.mark_applied(early, when=at(2026, 1, 6))
    svc.set_outcome(early, Outcome.REJECTED, when=at(2026, 1, 9))
    late = seed_job(db, "Late")
    svc.set_stage(late, Stage.FINAL, when=at(2026, 1, 20))
    svc.set_outcome(late, Outcome.REJECTED, when=at(2026, 2, 1))
    e, l = db.get(early), db.get(late)
    assert (e.outcome, e.app_stage, e.response_status) == ("rejected", "applied", "negative")
    assert (l.outcome, l.app_stage) == ("rejected", "final")  # stage kept: rejected at final
    rej = [x for x in db.effective_events(late) if x.type == EV.REJECTED][0]
    assert rej.payload["from_stage"] == "final"


def test_reopen_and_accept(db, svc):
    jid = seed_job(db)
    svc.mark_applied(jid, when=at(2026, 1, 6))
    svc.set_outcome(jid, Outcome.WITHDRAWN, when=at(2026, 1, 7))
    with pytest.raises(ApplicationError):
        svc.set_outcome(jid, Outcome.WITHDRAWN)
    svc.reopen(jid, when=at(2026, 1, 8))
    assert db.get(jid).outcome == ""
    svc.set_outcome(jid, Outcome.ACCEPTED, when=at(2026, 3, 1))
    job = db.get(jid)
    assert (job.outcome, job.app_stage) == ("accepted", "offer")
    with pytest.raises(ApplicationError):
        svc.reopen(seed_job(db, "Nope"))


def test_manual_stage_correction_is_auditable(db, svc):
    jid = seed_job(db)
    svc.set_stage(jid, Stage.TECHNICAL, when=at(2026, 1, 10))  # mistake
    bad = [e for e in db.effective_events(jid) if e.type == EV.STAGE_CHANGED][0]
    svc.set_stage(jid, Stage.SCREENING, when=at(2026, 1, 11))  # correct the summary
    assert db.get(jid).app_stage == "screening"
    svc.void_event(bad.id, "wrong round")  # and fix the history so analytics are right
    f = facts(db, jid)
    assert Stage.TECHNICAL not in f.first_reach and f.stage == Stage.SCREENING
    raw = {r["id"]: r for r in db.raw_events(jid)}
    assert raw[bad.id]["type"] == EV.STAGE_CHANGED  # original untouched
    assert any(r["type"] == EV.CORRECTION and r["corrects_id"] == bad.id for r in raw.values())
    svc.restore_event(bad.id)
    assert Stage.TECHNICAL in facts(db, jid).first_reach


def test_edit_event_changes_time_not_the_original_row(db, svc):
    jid = seed_job(db)
    svc.mark_applied(jid, when=at(2026, 1, 8))
    ev = [e for e in db.effective_events(jid) if e.type == EV.APPLIED][0]
    svc.edit_event(ev.id, occurred_at=at(2026, 1, 7), notes="applied via referral")
    job = db.get(jid)
    assert parse_iso(job.applied_at) == at(2026, 1, 7)  # the correction drives the summary
    eff = [e for e in effective_events(db.raw_events(jid)) if e.type == EV.APPLIED][0]
    assert eff.when == at(2026, 1, 7) and eff.edited and eff.notes == "applied via referral"
    orig = [r for r in db.raw_events(jid) if r["id"] == ev.id][0]
    assert parse_iso(orig["occurred_at"]) == at(2026, 1, 8)


def test_positive_and_negative_messages(db, svc):
    pos, neg, auto = seed_job(db, "P"), seed_job(db, "N"), seed_job(db, "A")
    for j in (pos, neg, auto):
        svc.mark_applied(j, when=at(2026, 1, 6))
    svc.record_message(pos, received_at=at(2026, 1, 9), sender="Sam", kind="outreach", sentiment="positive",
                       summary="Wants a call")
    svc.record_message(neg, received_at=at(2026, 1, 9), sender="HR", kind="rejection", sentiment="negative",
                       summary="Moving on")
    svc.record_message(auto, received_at=at(2026, 1, 6), kind="automated", summary="We got your application")
    p, n, a = db.get(pos), db.get(neg), db.get(auto)
    assert (p.response_status, p.app_stage) == ("positive", "recruiter_contact")
    assert (n.response_status, n.outcome) == ("negative", "rejected")
    assert a.response_status == "none" and a.first_response_at == ""  # an auto-ack is not a reply
    assert len(svc.list_messages(pos)) == 1


def test_followups_and_next_action(db, svc):
    jid = seed_job(db)
    svc.mark_applied(jid, when=at(2026, 1, 6))
    svc.set_next_action(jid, "Email recruiter", "2026-01-20")
    svc.set_next_action(jid, "Email hiring manager", "2026-01-18")  # replaces
    open_ = svc.list_actions(jid, include_done=False)
    assert [a["description"] for a in open_] == ["Email hiring manager"]
    f1 = svc.add_action(jid, "Follow up", "2026-02-01", kind="follow_up")
    svc.log_followup_sent(jid, when=at(2026, 2, 1), note="sent email")
    assert any(a["id"] == f1 and a["done_at"] for a in svc.list_actions(jid))
    assert [e.type for e in db.effective_events(jid)].count(EV.FOLLOWUP_SENT) == 1
    row = svc.list_applications()[0]
    assert row.next_action["description"] == "Email hiring manager"
    svc.complete_action(row.next_action["id"])
    assert svc.list_applications()[0].next_action is None


def test_unapply_only_before_progress(db, svc):
    jid = seed_job(db)
    svc.mark_applied(jid)
    svc.unmark_applied(jid)
    assert db.get(jid).applied_at == "" and db.get(jid).app_stage == "not_applied"
    svc.set_stage(jid, Stage.SCREENING)
    with pytest.raises(ApplicationError):
        svc.unmark_applied(jid)


def test_bulk_updates_are_safe(db, svc):
    a, b = seed_job(db, "A"), seed_job(db, "B")
    failed = seed_job(db, "F", status=__import__("core.models", fromlist=["JobStatus"]).JobStatus.FAILED,
                      generated=None)
    assert svc.bulk_mark_applied([a, b, failed]) == (2, 1)
    assert svc.bulk_set_stage([a, b], Stage.SCREENING) == (2, 0)
    assert svc.bulk_set_stage([a, b], Stage.APPLIED) == (0, 2)  # never silently moves backwards
    svc.set_outcome(a, Outcome.REJECTED)
    assert svc.bulk_set_stage([a], Stage.TECHNICAL) == (0, 1)  # closed applications are skipped


def test_archive_keeps_history_and_hides_row(db, svc):
    jid = seed_job(db)
    svc.mark_applied(jid)
    assert svc.archive([jid]) == 1
    assert svc.list_applications() == []
    assert len(svc.list_applications(AppFilter(include_archived=True))) == 1
    assert len(svc.list_applications(AppFilter(only_archived=True))) == 1
    assert len(db.effective_events(jid)) > 2
    db.delete([jid])  # applied jobs are history: never deleted by queue cleanup
    assert db.get(jid) is not None
    svc.archive([jid], archived=False)
    assert len(svc.list_applications()) == 1


def test_filters_and_sorting(db, svc):
    a, b, c = seed_job(db, "Alpha", "SRE"), seed_job(db, "Beta", "Dev"), seed_job(db, "Gamma", "Dev")
    svc.mark_applied(a, when=at(2026, 1, 6))
    svc.set_stage(b, Stage.SCREENING, when=at(2026, 1, 9))
    assert {r.job.company for r in svc.list_applications(AppFilter(search="dev"))} == {"Beta", "Gamma"}
    assert [r.job.company for r in svc.list_applications(AppFilter(applied=False))] == ["Gamma"]
    assert [r.job.company for r in svc.list_applications(AppFilter(stage=Stage.SCREENING))] == ["Beta"]
    assert [r.job.company for r in svc.list_applications(sort="company", descending=False)] == ["Alpha", "Beta", "Gamma"]
    by_applied = [r.job.company for r in svc.list_applications(sort="applied", descending=False)]
    assert by_applied[-1] == "Gamma"  # never-applied sorts last
    assert [r.job.company for r in svc.list_applications(AppFilter(response="waiting"), now=at(2026, 1, 10))] == ["Alpha"]
    ghosted = svc.list_applications(AppFilter(response="no_response"), now=at(2026, 3, 1))
    assert [r.job.company for r in ghosted] == ["Alpha"]
    assert to_iso(at(2026, 1, 6)) == db.get(a).applied_at
