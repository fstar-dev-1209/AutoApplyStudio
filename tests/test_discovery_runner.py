"""The polling cycle: what it reads, queues, retries, alerts about, and when it is allowed to run."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from core import jobright
from core.discovery import DiscoveryService, Listing
from core.discovery_runner import (
    ERROR,
    HUMAN_CHECK,
    IDLE,
    WAITING_LOGIN,
    DiscoveryRunner,
    keep_posting_for_target,
    partition_listings,
    within_hours,
)
from core.models import JobStatus, QueueStore
from core.settings_store import AppSettings

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
GH = "https://boards.greenhouse.io/acme/jobs/{n}"


class FakeHost:
    """Runs browser tasks inline, with no browser."""

    def run(self, task, timeout=0):
        return task(None)


class Env:
    def __init__(self, tmp_path):
        self.store = QueueStore(tmp_path / "studio.db")
        self.service = DiscoveryService(self.store)
        self.settings = AppSettings(discovery_enabled=True)
        self.page: list[Listing] = []
        self.links: dict[str, str] = {}
        self.detail_text: dict[str, str] = {}
        self.detail_titles: dict[str, str] = {}
        self.detail_companies: dict[str, str] = {}
        self.detail_pay: dict[str, str] = {}
        self.alerts: list[tuple[str, str]] = []
        self.queued_batches: list[list[str]] = []
        self.error: Exception | None = None
        self.reads = []
        self.detail_calls: list[str] = []
        self.now = NOW

        def read(ctx, url, *, is_known, pause, debug_dir=None, max_age_hours=None):
            self.reads.append((url, set(is_known.__self__) if hasattr(is_known, "__self__") else None))
            if self.error:
                raise self.error
            return jobright.ReadResult(listings=list(self.page))

        def read_detail(ctx, listing, pause=None):
            # Stands in for opening the Jobright job page: the list only supplied the id.
            self.detail_calls.append(listing.external_id)
            return Listing(
                external_id=listing.external_id,
                title=self.detail_titles.get(listing.external_id, listing.title),
                company=self.detail_companies.get(listing.external_id, listing.company),
                location=listing.location, source_url=listing.source_url,
                apply_url=self.links.get(listing.external_id, listing.apply_url),
                posted_at=listing.posted_at,
                description=self.detail_text.get(listing.external_id, listing.description),
                salary_text=self.detail_pay.get(listing.external_id, listing.salary_text),
            )

        self.runner = DiscoveryRunner(
            self.service, lambda: self.settings, lambda: FakeHost(),
            on_queued=self.queued_batches.append, alert=lambda t, b: self.alerts.append((t, b)),
            read=read, read_detail=read_detail,
            pause=lambda lo, hi: None, now=lambda: self.now, rng=random.Random(1),
        )


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.store.database.close()


def posting(n, **kw):
    return Listing(external_id=f"j{n}", title="Backend Engineer", company=f"Co{n}", posted_at="2 hours ago",
                   source_url=f"https://jobright.ai/jobs/info/j{n}", **kw)


def test_only_an_applyable_link_opens_the_jobright_page(env):
    env.page = [
        posting(1, apply_url=GH.format(n=1)),
        posting(2, apply_url="https://jobs.lever.co/a/b"),
        posting(3, apply_url="https://careers.example.com/job/3"),
        posting(4, apply_url="https://careers.acme.com/jobs/4?gh_jid=44"),
    ]
    report = env.runner.run_once()
    assert (report.queued, report.unsupported) == (2, 2)
    assert env.detail_calls == ["j1", "j4"]  # Greenhouse, and a company page that embeds Greenhouse
    assert {j.url for j in env.store.jobs()} == {GH.format(n=1), "https://careers.acme.com/jobs/4?gh_jid=44"}


def test_a_page_opened_to_find_the_link_keeps_its_text_only_for_greenhouse(env):
    env.page = [posting(1), posting(2)]  # the list had no employer link for either
    env.links["j1"] = GH.format(n=1)
    env.links["j2"] = "https://jobs.ashbyhq.com/beta/abc"
    text = "Backend Engineer — Acme\n\nSummary\nBuild the APIs.\n\nResponsibilities\n• Design services"
    env.detail_text["j1"] = text
    env.detail_text["j2"] = text
    report = env.runner.run_once()
    assert env.detail_calls == ["j1", "j2"]
    assert (report.queued, report.unsupported) == (1, 1)
    job = env.store.jobs()[0]
    assert job.url == GH.format(n=1) and job.job_description == text
    assert env.service.counts()["unsupported"] == 1


def test_a_second_copy_of_the_same_greenhouse_job_is_not_opened(env):
    env.page = [posting(1, apply_url=GH.format(n=1)), posting(2, apply_url=GH.format(n=1))]
    report = env.runner.run_once()
    assert env.detail_calls == ["j1"]
    assert (report.queued, report.duplicate) == (1, 1)


def test_other_sites_do_not_use_up_the_batch(env):
    greenhouse = [posting(n, apply_url=GH.format(n=n)) for n in range(1, 46)]
    others = [posting(100 + n, apply_url=f"https://jobs.lever.co/co/j{n}") for n in range(30)]
    env.page = others + greenhouse  # other sites come first and must not crowd out Greenhouse
    report = env.runner.run_once()
    assert report.unsupported == 30 and report.queued == 20  # the default: 20 new jobs an hour
    assert env.detail_calls == [f"j{n}" for n in range(1, 21)]  # pages are opened only until the batch is full
    assert report.capped == 25


def test_postings_that_did_not_fit_are_dropped_not_kept_for_the_next_batch(env):
    env.page = [posting(n, apply_url=GH.format(n=n)) for n in range(1, 26)]
    first = env.runner.run_once()
    assert (first.queued, first.capped) == (20, 5)
    rows = {r["external_id"]: r for r in env.service.recent(limit=50)}
    assert rows["j25"]["state"] == "filtered" and "limit of 20" in rows["j25"]["reason"]
    env.now = datetime.now(timezone.utc) + timedelta(hours=1)
    second = env.runner.run_once()
    assert second.queued == 0 and second.seen == 25  # the five are not picked up an hour later
    assert len(env.store.jobs()) == 20


def test_the_freshest_postings_are_taken_even_when_they_are_last_on_the_page(env):
    env.settings.discovery_hourly_cap = 2
    stamp = lambda hours: (NOW - timedelta(hours=hours)).isoformat()  # noqa: E731
    env.page = [Listing(f"j{n}", title="Dev", company=f"Co{n}", apply_url=GH.format(n=n), posted_at=stamp(h))
                for n, h in [(1, 20), (2, 15), (3, 1), (4, 9), (5, 2)]]
    report = env.runner.run_once()
    assert report.queued == 2
    assert [j.url for j in env.store.jobs()] == [GH.format(n=3), GH.format(n=5)]  # 1 and 2 hours old, freshest first


def test_two_jobs_at_one_company_in_one_run_take_one_slot(env):
    env.page = [Listing("a", title="SRE", company="Acme, Inc.", apply_url=GH.format(n=1), posted_at="1 hour ago"),
                Listing("b", title="Backend", company="ACME", apply_url=GH.format(n=2), posted_at="2 hours ago"),
                Listing("c", title="Data", company="Other Co", apply_url=GH.format(n=3), posted_at="3 hours ago")]
    report = env.runner.run_once()
    assert (report.queued, report.filtered) == (2, 1)
    assert env.detail_calls == ["a", "c"]  # the second Acme job's page is never opened
    rows = {r["external_id"]: r for r in env.service.recent(limit=10)}
    assert "Another job at ACME is already in the queue" in rows["b"]["reason"]


def test_a_company_with_a_job_in_the_queue_is_not_queued_again_unless_that_job_was_dropped(env):
    env.page = [Listing("a", title="SRE", company="Acme", apply_url=GH.format(n=1), posted_at="1 hour ago")]
    env.runner.run_once()
    env.page = [Listing("b", title="Backend", company="Acme Inc", apply_url=GH.format(n=2), posted_at="1 hour ago")]
    assert env.runner.run_once().queued == 0
    env.service.expire([env.store.jobs()[0].id])
    env.page = [Listing("c", title="Data", company="Acme", apply_url=GH.format(n=3), posted_at="1 hour ago")]
    assert env.runner.run_once().queued == 1  # a skipped job does not hold its company


def _hour_later(env, minutes=60):
    env.now = datetime.now(timezone.utc) + timedelta(minutes=minutes)


def test_an_older_batch_still_waiting_is_dropped_when_a_fresh_one_arrives(env):
    env.page = [posting(n, apply_url=GH.format(n=n)) for n in range(1, 6)]
    env.runner.run_once()
    old = list(env.store.jobs())
    generating, done = old[0], old[1]
    generating.status = JobStatus.GENERATING
    env.store.update(generating)
    done.status, done.resume_json = JobStatus.DONE, {"company_name": "Co2"}
    env.store.update(done)
    _hour_later(env)
    env.page = [posting(n, apply_url=GH.format(n=n)) for n in range(6, 9)]
    report = env.runner.run_once()
    assert (report.queued, report.expired) == (3, 3)
    after = {j.id: j for j in env.store.jobs()}
    assert [after[j.id].status for j in old] == [
        JobStatus.GENERATING, JobStatus.DONE, JobStatus.SKIPPED, JobStatus.SKIPPED, JobStatus.SKIPPED]
    dropped = after[old[2].id]
    assert dropped.skip_reason.startswith("Expired") and dropped.stage == "Expired"
    assert "skipped" in [e["event_type"] for e in env.store.list_events(dropped.id)]


def test_nothing_is_dropped_when_nothing_fresher_arrives(env):
    env.page = [posting(n, apply_url=GH.format(n=n)) for n in range(1, 4)]
    env.runner.run_once()
    _hour_later(env)
    env.page = []
    report = env.runner.run_once()
    assert (report.queued, report.expired) == (0, 0)
    assert all(j.status == JobStatus.QUEUED for j in env.store.jobs())  # old, but nobody is waiting behind them


def test_a_batch_that_is_still_fresh_and_full_takes_nothing_and_loses_nothing(env):
    env.settings.discovery_hourly_cap = 3
    env.page = [posting(n, apply_url=GH.format(n=n)) for n in range(1, 4)]
    env.runner.run_once()
    env.page = [posting(n, apply_url=GH.format(n=n)) for n in range(4, 6)]
    env.now = datetime.now(timezone.utc) + timedelta(minutes=5)  # the same batch, five minutes on
    report = env.runner.run_once()
    assert report.queued == 0 and env.service.counts() == {"queued": 3}  # j4 and j5 were not recorded as dropped
    _hour_later(env)
    assert env.runner.run_once().queued == 2  # the next batch takes them (and replaces the three still waiting)


def test_the_batch_follows_the_hourly_cap_and_the_interval():
    s = AppSettings(discovery_hourly_cap=20, discovery_interval_minutes=60)
    assert s.discovery_batch_size() == 20
    s.discovery_interval_minutes = 10
    assert s.discovery_batch_size() == 4  # a sixth of an hour: 3.3, rounded up
    s.discovery_interval_minutes = 120
    assert s.discovery_batch_size() == 40
    assert AppSettings().discovery_interval_minutes == 60 and AppSettings().discovery_hourly_cap == 20
    assert AppSettings.from_dict({"discovery_hourly_cap": 0}).discovery_hourly_cap == 1


def test_a_blocked_greenhouse_job_is_not_opened(env):
    env.settings.discovery_blocked_companies = "Co1"
    env.page = [posting(1, apply_url=GH.format(n=1)), posting(2, apply_url=GH.format(n=2))]
    report = env.runner.run_once()
    assert (report.filtered, report.queued) == (1, 1)
    assert env.detail_calls == ["j2"]


def test_new_postings_with_links_are_queued_and_announced(env):
    env.page = [posting(1, apply_url=GH.format(n=1)), posting(2, apply_url="https://jobs.lever.co/a/b")]
    report = env.runner.run_once()
    assert (report.queued, report.unsupported) == (1, 1)
    assert env.runner.status.state == IDLE and env.runner.status.queued_total == 1
    assert len(env.queued_batches) == 1 and len(env.queued_batches[0]) == 1
    assert [j.url for j in env.store.jobs()] == [GH.format(n=1)]


def test_a_repeat_cycle_queues_nothing_twice(env):
    env.page = [posting(1, apply_url=GH.format(n=1))]
    env.runner.run_once()
    again = env.runner.run_once()
    assert again.queued == 0 and again.seen == 1
    assert len(env.store.jobs()) == 1 and len(env.queued_batches) == 1


def test_old_postings_are_dropped_before_anything_else(env):
    env.page = [Listing("old", title="Dev", company="Old Co", apply_url=GH.format(n=9),
                        posted_at=(NOW - timedelta(days=3)).isoformat()), posting(2, apply_url=GH.format(n=2))]
    report = env.runner.run_once()
    assert report.queued == 1 and env.service.counts() == {"queued": 1}


def test_the_jobright_page_is_what_gets_queued_not_the_list(env):
    env.page = [Listing("j1", title="Card title", company="Card Co", posted_at="2 hours ago",
                        source_url="https://jobright.ai/jobs/info/j1", apply_url=GH.format(n=9),
                        description="A summary from the list. " * 30, salary_text="$1")]
    env.detail_text["j1"] = "Backend Engineer — Acme\n\nSummary\nBuild the APIs.\n\nResponsibilities\n• Design services"
    env.detail_titles["j1"] = "Backend Engineer"
    env.detail_companies["j1"] = "Acme"
    env.detail_pay["j1"] = "$150K/yr - $180K/yr"
    env.links["j1"] = GH.format(n=1)
    report = env.runner.run_once()
    assert report.queued == 1
    job = env.store.jobs()[0]
    assert job.url == GH.format(n=1)
    assert (job.role_title, job.company) == ("Backend Engineer", "Acme")
    assert job.job_description.startswith("Backend Engineer — Acme") and "Design services" in job.job_description
    assert "list" not in job.job_description
    assert job.salary_text == "$150K/yr - $180K/yr"
    assert (job.salary_min, job.salary_max) == (150000, 180000)


def test_a_missing_employer_link_is_found_and_the_posting_then_queued(env):
    env.page = [posting(1)]  # the list carried no application link
    first = env.runner.run_once()
    assert first.queued == 0 and env.service.counts() == {"unresolved": 1}

    env.links["j1"] = GH.format(n=1)
    env.page = []  # nothing new on the list this time; the pending one is still retried
    second = env.runner.run_once()
    assert second.queued == 1 and env.service.counts() == {"queued": 1}


def test_a_link_that_cannot_be_found_stays_pending_without_failing_the_cycle(env):
    env.page = [posting(1)]
    env.runner.run_once()
    again = env.runner.run_once()
    assert env.runner.status.state == IDLE and again.queued == 0
    assert env.service.counts() == {"unresolved": 1}


def test_settings_rules_apply(env):
    env.settings.discovery_blocked_companies = "Co1"
    env.settings.discovery_exclude_words = "intern"
    env.page = [posting(1, apply_url=GH.format(n=1)), posting(2, apply_url=GH.format(n=2)),
                Listing("j3", title="Software Intern", company="Co3", posted_at="1 hour ago", apply_url=GH.format(n=3))]
    report = env.runner.run_once()
    assert (report.queued, report.filtered) == (1, 2)


@pytest.mark.parametrize("error, state", [
    (jobright.LoginRequired("sign in"), WAITING_LOGIN),
    (jobright.HumanCheck("captcha"), HUMAN_CHECK),
    (RuntimeError("boom"), ERROR),
])
def test_problems_are_reported_without_stopping_the_loop(env, error, state):
    env.error = error
    assert env.runner.run_once() is None
    assert env.runner.status.state == state
    env.error = None
    env.page = [posting(1, apply_url=GH.format(n=1))]
    assert env.runner.run_once().queued == 1  # and it recovers by itself


def test_a_lasting_problem_alerts_once_not_every_cycle(env):
    env.error = jobright.LoginRequired("Jobright is not signed in.")
    for _ in range(4):
        env.runner.run_once()
    assert len(env.alerts) == 1 and "sign in" in env.alerts[0][0]
    env.error = None
    env.runner.run_once()  # signed in again ...
    env.error = jobright.LoginRequired("again")
    env.runner.run_once()
    assert len(env.alerts) == 2  # ... so the next sign-out is news again


def test_the_visit_split_follows_the_list_link():
    gh = Listing("g", apply_url=GH.format(n=1))
    lever = Listing("l", apply_url="https://jobs.lever.co/a/b")
    missing = Listing("m")
    blocked = Listing("b", apply_url=GH.format(n=2))
    states = {"g": "queued", "l": "unsupported", "m": "unresolved", "b": "filtered"}
    decide = lambda listing: states[listing.external_id]
    to_open, to_record = partition_listings([gh, lever, missing, blocked], decide)
    assert [item.external_id for item in to_open] == ["g", "m"]
    assert [item.external_id for item in to_record] == ["l", "b"]


def test_text_from_a_lookup_visit_is_dropped_unless_the_link_is_applyable():
    detail = Listing("j", title="Dev", company="Acme", apply_url="https://jobs.lever.co/a/b",
                     description="Dev — Acme\n\nSummary\nBuild things.\n\nResponsibilities\n• Ship")
    kept = keep_posting_for_target(detail, frozenset({"greenhouse"}))
    assert kept.description == "" and kept.title == "Dev" and kept.apply_url.endswith("/a/b")
    same = keep_posting_for_target(
        Listing("j", apply_url=GH.format(n=1), description=detail.description), frozenset({"greenhouse"}))
    assert same.description == detail.description


def test_active_hours_follow_the_bot_clock_not_the_computers():
    at = lambda h: datetime(2026, 11, 5, h, 0, tzinfo=timezone.utc)  # noqa: E731
    # 15:00 UTC is 10:00 in New York (EST in November)
    assert within_hours(at(15), "America/New_York", 9, 18)
    assert not within_hours(at(15), "America/New_York", 11, 18)
    assert within_hours(at(3), "America/New_York", 22, 6)  # 22:00 yesterday: an overnight window wraps midnight
    assert within_hours(at(15), "America/New_York", 0, 24)


def test_the_delay_between_cycles_is_jittered_around_the_setting(env):
    env.settings.discovery_interval_minutes = 10
    delays = [env.runner._next_delay(env.settings) for _ in range(50)]
    assert all(480 <= d <= 720 for d in delays) and len(set(delays)) > 10


def test_what_the_last_read_saw_is_kept_for_the_page_and_the_log(tmp_path):
    e = Env(tmp_path)
    lines = []
    e.runner._log = lines.append
    e.page = [posting(1, apply_url=GH.format(n=1))]
    e.runner.run_once()
    assert e.runner.status.detail[0].startswith("Read 1 job(s)") and "within the last 24h" in e.runner.status.detail[0]
    assert len(lines) == 1 and "Read 1 job(s)" in lines[0]

    e.error = jobright.LoginRequired("Jobright is not signed in.")
    e.runner.run_once()
    assert "needs_login" in lines[-1] and "not signed in" in lines[-1]
    e.error = RuntimeError("boom")
    e.runner.run_once()
    assert any("RuntimeError: boom" in line for line in e.runner.status.detail)
    e.store.database.close()


def test_the_report_counts_what_was_listed_and_why_greenhouse_jobs_were_left_out(env):
    first = Listing("a", title="SRE", company="Acme", apply_url=GH.format(n=1), posted_at="1 hour ago")
    env.page = [first]
    env.runner.run_once()
    env.settings.discovery_blocked_companies = "Bad Corp"
    env.page = [
        Listing("b", title="Backend", company="Acme Inc", apply_url=GH.format(n=2), posted_at="2 hours ago"),
        Listing("c", title="Data", company="Bad Corp", apply_url=GH.format(n=3), posted_at="2 hours ago"),
        Listing("d", title="Same", company="Dup Co", apply_url=GH.format(n=1), posted_at="2 hours ago"),
        Listing("e", title="Other", company="Lever Co", apply_url="https://jobs.lever.co/a/b", posted_at="2 hours ago"),
        Listing("f", title="Ops", company="Fresh Co", apply_url=GH.format(n=6), posted_at="2 hours ago"),
        Listing("g", title="Old", company="Old Co", apply_url=GH.format(n=7), posted_at="5 days ago"),
    ]
    seen = []
    env.runner._on_report = seen.append
    report = env.runner.run_once()
    assert report is seen[0]
    assert (report.listed, report.recent) == (6, 5)  # the old one is not recent
    assert (report.queued, report.unsupported, report.greenhouse) == (1, 1, 4)
    assert report.reasons == {"another job at the company is queued": 1, "company on your block list": 1,
                              "same posting already known": 1}


def test_a_failing_report_hook_never_stops_the_search(env):
    def boom(report):
        raise RuntimeError("telegram is down")

    env.runner._on_report = boom
    env.page = [posting(1, apply_url=GH.format(n=1))]
    assert env.runner.run_once().queued == 1
