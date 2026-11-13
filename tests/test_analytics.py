from datetime import date, timedelta, timezone

import pytest

from core import analytics
from core.analytics import Rate
from core.application_stage import Stage
from core.timeutil import DateRange, range_for
from tests.conftest import at, seed_job
from tests.fixture_data import NOW, build

UTC = timezone.utc


@pytest.fixture
def world(db, svc):
    ids = build(db, svc)
    return db, svc, ids


def run(db, rng, tz=UTC):
    return analytics.compute(analytics.load_data(db), rng, tz=tz, now=NOW)


def test_all_time_totals(world):
    db, _, _ = world
    a = run(db, range_for("all"))
    # document productivity (attempt results)
    assert (a.imported, a.generated, a.failed, a.skipped_duplicates, a.processed) == (10, 8, 1, 1, 10)
    assert a.ready_not_applied == 1  # Echo only
    # application activity
    assert a.submitted == 7
    assert a.awaiting_reply == 1  # Delta; archived/finished/replied ones are not "awaiting"
    assert a.responses == 5  # A, B, C, I, J  (H's auto-ack and withdrawal are not replies)
    assert a.positive_responses == 2  # A, C
    assert a.rejections == 2  # B, C  (J was reopened)
    # interviews
    assert (a.screening_invites, a.technical_invites, a.final_invites, a.offers) == (2, 1, 1, 1)
    assert (a.screening_done, a.technical_done, a.final_done) == (2, 2, 0)


def test_all_time_conversion_rates(world):
    db, _, _ = world
    a = run(db, range_for("all"))
    assert a.cohort_size == 7
    assert (a.reply.num, a.reply.den) == (5, 7)
    assert (a.screening.num, a.screening.den) == (2, 7)
    assert (a.technical.num, a.final.num, a.offer.num) == (1, 1, 1)
    assert (a.generation_success.num, a.generation_success.den) == (8, 9)
    assert (a.submission.num, a.submission.den) == (7, 8)  # 8 generated, 7 got applied to
    assert [(s.name, s.count) for s in a.funnel] == [
        ("Applied", 7), ("Replied", 5), ("Screening", 2), ("Technical", 1), ("Final", 1), ("Offer", 1)
    ]
    assert a.funnel[2].of_previous == Rate(2, 5) and a.funnel[2].of_applied == Rate(2, 7)
    assert a.reply.text == "71%"


def test_time_to_first_response(world):
    db, _, _ = world
    a = run(db, range_for("all"))
    assert sorted(round(x) for x in a.days_to_response) == [1, 2, 3, 7, 11]
    assert a.median_days_to_response == pytest.approx(3.0)
    assert a.mean_days_to_response == pytest.approx(4.8)


def test_period_counts_vs_cohort_rates(world):
    db, _, _ = world
    jan = run(db, range_for("custom", custom=(date(2026, 1, 1), date(2026, 1, 31)), tz=UTC))
    feb = run(db, range_for("custom", custom=(date(2026, 2, 1), date(2026, 2, 28)), tz=UTC))
    assert (jan.imported, jan.generated, jan.failed, jan.skipped_duplicates, jan.processed) == (8, 6, 1, 1, 8)
    assert (jan.submitted, jan.responses, jan.rejections) == (6, 4, 1)
    assert (feb.imported, feb.generated, feb.submitted, feb.responses, feb.rejections) == (2, 2, 1, 1, 1)
    # period event counts: the final interview and offer happened in February ...
    assert (jan.final_invites, jan.offers) == (0, 0)
    assert (feb.final_invites, feb.offers) == (1, 1)
    # ... but cohort rates follow the January applications forward in time
    assert jan.cohort_size == 6 and (jan.final.num, jan.offer.num) == (1, 1)
    assert jan.reply == Rate(4, 6)
    # snapshots do not depend on the selected range
    assert jan.awaiting_reply == feb.awaiting_reply == 1
    assert jan.ready_not_applied == feb.ready_not_applied == 1


def test_trend_series(world):
    db, _, _ = world
    jan = run(db, range_for("custom", custom=(date(2026, 1, 1), date(2026, 1, 31)), tz=UTC))
    s = jan.applications_series
    assert len(s.labels) == 31 and s.bucket == "day" and sum(s.values) == 6
    assert s.values[5] == 1 and s.values[6] == 1  # Jan 6 and Jan 7
    assert sum(jan.replies_series.values) == 4
    # interviews by week (Mon-start): 12 Jan, 18 Jan(Sun->week of 12th), 21 Jan, 25 Jan(week of 19th)
    iv = jan.interviews_series
    assert sum(iv.values) == 4 and iv.bucket == "week"
    assert {k: v for k, v in zip(iv.labels, iv.values) if v} == {"Jan 12": 2, "Jan 19": 2}
    m = run(db, range_for("all")).monthly_series
    assert dict(zip(m.labels, m.values)) == {"Jan 2026": 6, "Feb 2026": 1}


def test_long_ranges_aggregate_weekly(world):
    db, _, _ = world
    a = run(db, range_for("all"))
    assert a.applications_series.bucket == "week" or len(a.applications_series.labels) <= 92
    assert sum(a.applications_series.values) == 7


def test_empty_database_has_no_division_by_zero(db):
    a = run(db, range_for("last_30", today=date(2026, 2, 20), tz=UTC))
    assert a.submitted == 0 and a.cohort_size == 0
    for r in (a.reply, a.screening, a.technical, a.final, a.offer, a.generation_success, a.submission):
        assert r.value is None and r.text == "—" and r.detail == "no data"
    assert a.median_days_to_response is None
    assert all(s.of_previous.value is None for s in a.funnel)
    assert a.monthly_series.values == [0] * 12 or sum(a.monthly_series.values) == 0


def test_local_calendar_boundaries(db, svc):
    pst = timezone(timedelta(hours=-8))
    jid = seed_job(db, "Late", created=at(2026, 2, 28, 1), generated=at(2026, 2, 28, 2))
    # 03:00 UTC on 1 Mar == 19:00 on 28 Feb in UTC-8: a *February* application locally
    svc.mark_applied(jid, when=at(2026, 3, 1, 3))
    feb28 = range_for("custom", custom=(date(2026, 2, 28), date(2026, 2, 28)), tz=pst)
    mar1 = range_for("custom", custom=(date(2026, 3, 1), date(2026, 3, 1)), tz=pst)
    assert run(db, feb28, pst).submitted == 1
    assert run(db, mar1, pst).submitted == 0
    # in UTC the same instant is 1 March
    assert run(db, range_for("custom", custom=(date(2026, 3, 1), date(2026, 3, 1)), tz=UTC)).submitted == 1
    assert run(db, range_for("custom", custom=(date(2026, 2, 28), date(2026, 2, 28)), tz=UTC)).submitted == 0


def test_presets_use_local_midnights():
    pst = timezone(timedelta(hours=-8))
    today = date(2026, 3, 1)
    r = range_for("today", today=today, tz=pst)
    assert r.start == at(2026, 3, 1, 8) and r.end == at(2026, 3, 2, 8)  # 00:00 local == 08:00Z
    assert range_for("yesterday", today=today, tz=pst).start == at(2026, 2, 28, 8)
    assert range_for("last_7", today=today, tz=UTC).start == at(2026, 2, 23, 0)
    assert range_for("last_30", today=today, tz=UTC).start == at(2026, 1, 31, 0)
    this = range_for("this_month", today=date(2026, 3, 15), tz=UTC)
    assert (this.start, this.end) == (at(2026, 3, 1, 0), at(2026, 3, 16, 0))
    prev = range_for("prev_month", today=date(2026, 1, 10), tz=UTC)  # across a year boundary
    assert (prev.first_day, prev.last_day) == (date(2025, 12, 1), date(2025, 12, 31))
    assert prev.contains(at(2025, 12, 31, 23, 59)) and not prev.contains(at(2026, 1, 1, 0, 0))
    swapped = range_for("custom", custom=(date(2026, 3, 5), date(2026, 3, 1)), tz=UTC)
    assert swapped.first_day == date(2026, 3, 1)
    assert range_for("all") == DateRange(None, None, "All time")


def test_dst_day_is_not_24_hours():
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    r = range_for("custom", custom=(date(2026, 3, 8), date(2026, 3, 8)), tz=ny)  # spring forward
    assert (r.end - r.start) == timedelta(hours=23)


def test_archived_counts_in_history_not_in_snapshots(world):
    db, svc, ids = world
    before = run(db, range_for("all"))
    svc.archive([ids["D"]])  # D was the only one awaiting a reply
    after = run(db, range_for("all"))
    assert after.submitted == before.submitted  # history unchanged
    assert before.awaiting_reply == 1 and after.awaiting_reply == 0  # snapshot drops it


def test_stage_correction_voiding_fixes_analytics(world):
    db, svc, ids = world
    d = ids["D"]
    svc.set_stage(d, Stage.TECHNICAL, when=at(2026, 2, 1))  # mis-click
    assert run(db, range_for("all")).technical_invites == 2
    bad = [e for e in db.effective_events(d) if e.type == "stage_changed"][0]
    svc.void_event(bad.id)
    assert run(db, range_for("all")).technical_invites == 1


def test_status_mix_and_top_companies(world):
    db, _, _ = world
    a = run(db, range_for("all"))
    # the 7 applications submitted: A accepted offer, B/C rejected, D waiting, H withdrawn, I replied (neutral), J active/no reply... (J had a rejection response)
    mix = dict(a.status_mix)
    assert sum(mix.values()) == 7 and mix["Offer"] == 1 and mix["Rejected"] == 2 and mix["Withdrawn / closed"] == 1
    assert mix["Waiting"] == 1 and mix["Replied"] == 2  # I (neutral reply) and J (reopened after a rejection)
    assert len(a.top_companies) == 6 and all(n == 1 for _c, n in a.top_companies)  # 7 companies, list capped at 6
