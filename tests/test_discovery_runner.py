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
    MAX_DETAILS_PER_RUN,
    WAITING_LOGIN,
    DiscoveryRunner,
    keep_posting_for_target,
    partition_listings,
    within_hours,
)
from core.models import QueueStore
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
            pause=lambda lo, hi: None, now=lambda: NOW, rng=random.Random(1),
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


def test_other_sites_do_not_use_up_the_page_budget(env):
    greenhouse = [posting(n, apply_url=GH.format(n=n)) for n in range(1, MAX_DETAILS_PER_RUN + 6)]
    others = [posting(100 + n, apply_url=f"https://jobs.lever.co/co/j{n}") for n in range(30)]
    env.page = others + greenhouse  # other sites come first and must not crowd out Greenhouse
    report = env.runner.run_once()
    assert report.unsupported == 30 and report.queued == MAX_DETAILS_PER_RUN
    assert env.detail_calls == [f"j{n}" for n in range(1, MAX_DETAILS_PER_RUN + 1)]
    rest = env.runner.run_once()
    assert rest.queued == 5 and rest.seen == 30 + MAX_DETAILS_PER_RUN
    assert env.detail_calls == [f"j{n}" for n in range(1, MAX_DETAILS_PER_RUN + 6)]


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
