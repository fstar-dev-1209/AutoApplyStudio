"""Link classification and the once-only intake of discovered postings."""

from __future__ import annotations

import pytest

from core import ats
from core.discovery import (
    DUPLICATE,
    FILTERED,
    QUEUED,
    UNRESOLVED,
    UNSUPPORTED,
    DiscoveryRules,
    DiscoveryService,
    Listing,
)
from core.models import JobItem, QueueStore


@pytest.mark.parametrize("url, expect", [
    ("https://boards.greenhouse.io/acme/jobs/4012345", ("greenhouse", "acme", "4012345", False)),
    ("https://job-boards.greenhouse.io/acme/jobs/4012345?gh_src=abc", ("greenhouse", "acme", "4012345", False)),
    ("https://job-boards.eu.greenhouse.io/acme/jobs/77", ("greenhouse", "acme", "77", False)),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345", ("greenhouse", "acme", "4012345", True)),
    ("https://careers.acme.com/open-roles?gh_jid=998877&utm=x", ("greenhouse", "careers.acme.com", "998877", True)),
    ("https://jobs.lever.co/acme/0a1b2c3d-1111-2222-3333-444455556666/apply", ("lever", "acme", "0a1b2c3d-1111-2222-3333-444455556666", False)),
    ("https://jobs.ashbyhq.com/acme/aaaa-bbbb", ("ashby", "acme", "aaaa-bbbb", False)),
    ("https://apply.workable.com/acme/j/ABC123DEF/", ("workable", "acme", "ABC123DEF", False)),
    ("https://ats.rippling.com/acme/jobs/9f8e7d", ("rippling", "acme", "9f8e7d", False)),
    ("https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Austin/Engineer_R-1234", ("workday", "acme", "Engineer_R-1234", False)),
])
def test_known_application_sites_are_recognised(url, expect):
    link = ats.classify(url)
    assert (link.ats, link.board, link.job_id, link.embedded) == expect


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/view/123", "https://example.com/careers/1", "", "not a url",
    "https://boards.greenhouse.io.evil.example/acme/jobs/1", "https://evil.example/boards.greenhouse.io/acme/jobs/1",
])
def test_unknown_or_lookalike_links_are_not_an_ats(url):
    assert ats.classify(url).ats == ""


def test_only_greenhouse_is_supported_for_now():
    assert ats.classify("https://boards.greenhouse.io/a/jobs/1").supported
    assert not ats.classify("https://jobs.lever.co/a/b").supported


def test_the_same_posting_has_one_identity_whatever_the_spelling():
    keys = {ats.classify(u).key for u in (
        "https://boards.greenhouse.io/Acme/jobs/55", "https://job-boards.greenhouse.io/acme/jobs/55?gh_src=zz",
    )}
    assert len(keys) == 1 and keys != {""}


GH = "https://boards.greenhouse.io/acme/jobs/{n}"


@pytest.fixture
def svc(tmp_path):
    store = QueueStore(tmp_path / "studio.db")
    service = DiscoveryService(store)
    yield service
    store.database.close()


def listing(n, **kw):
    return Listing(external_id=f"jr-{n}", title=kw.pop("title", "Backend Engineer"), company=kw.pop("company", "Acme"),
                   apply_url=kw.pop("apply_url", GH.format(n=n)), **kw)


def test_a_supported_posting_becomes_a_queue_job_once(svc):
    first = svc.ingest("jobright", [listing(1, description="We build things.", salary_text="$150k")])
    assert (first.queued, first.seen) == (1, 0)
    job = svc.store.get(first.queued_job_ids[0])
    assert (job.url, job.company, job.role_title, job.source) == (GH.format(n=1), "Acme", "Backend Engineer", "jobright")
    assert job.job_description == "We build things." and job.salary_text == "$150k"

    again = svc.ingest("jobright", [listing(1)])
    assert (again.queued, again.seen) == (0, 1)
    assert len(svc.store.jobs()) == 1


def test_unsupported_sites_are_kept_but_never_queued(svc):
    report = svc.ingest("jobright", [listing(1, apply_url="https://jobs.lever.co/acme/abc"),
                                     listing(2, apply_url="https://www.linkedin.com/jobs/view/1")])
    assert (report.queued, report.unsupported) == (0, 2)
    assert svc.store.jobs() == []
    rows = {r["external_id"]: r for r in svc.recent(UNSUPPORTED)}
    assert "not automated yet" in rows["jr-1"]["reason"] and rows["jr-2"]["ats"] == ""


def test_the_same_posting_from_another_listing_or_link_is_a_duplicate(svc):
    svc.ingest("jobright", [listing(1)])
    other = svc.ingest("jobright", [Listing(external_id="jr-other", title="Backend Engineer", company="Acme",
                                            apply_url="https://job-boards.greenhouse.io/acme/jobs/1?gh_src=x")])
    assert (other.queued, other.duplicate) == (0, 1)
    # and one already in the queue from the extension, before discovery ever saw it
    manual = JobItem.create("https://boards.greenhouse.io/zeta/jobs/9")
    svc.store.add_many([manual])
    third = svc.ingest("jobright", [listing(2, apply_url="https://boards.greenhouse.io/zeta/jobs/9", company="Zeta")])
    assert third.duplicate == 1 and len(svc.store.jobs()) == 2


def test_block_rules_and_company_checks_filter(svc):
    svc._rules = lambda: DiscoveryRules.from_text("Staffing Co; Bad Corp", "intern, manager")
    svc._company_check = lambda company: "Already applied" if company == "Applied Inc" else ""
    report = svc.ingest("jobright", [
        listing(1, company="Staffing Co Global"), listing(2, title="Engineering Manager"),
        listing(3, title="Software Intern"), listing(4, company="Applied Inc"),
        listing(5, title="Internal Tools Engineer"),  # "intern" must not match inside "Internal"
    ])
    assert (report.filtered, report.queued) == (4, 1)
    reasons = {r["external_id"]: r["reason"] for r in svc.recent(FILTERED)}
    assert "block list" in reasons["jr-1"] and "manager" in reasons["jr-2"] and reasons["jr-4"] == "Already applied"
    assert svc.recent(QUEUED)[0]["external_id"] == "jr-5"


def test_a_posting_without_a_link_waits_and_is_taken_when_the_link_arrives(svc):
    pending = svc.ingest("jobright", [listing(1, apply_url="")])
    assert pending.unresolved == 1 and svc.counts() == {UNRESOLVED: 1}
    assert [r["external_id"] for r in svc.unresolved("jobright")] == ["jr-1"]

    resolved = svc.ingest("jobright", [listing(1)])
    assert (resolved.queued, resolved.seen) == (1, 0)
    assert svc.counts() == {QUEUED: 1}  # the same row moved on; it is not counted twice


def test_unsupported_sites_become_queueable_when_support_is_added(svc):
    svc.ingest("jobright", [listing(1, apply_url="https://jobs.lever.co/acme/abc")])
    svc._supported = lambda: frozenset({"greenhouse", "lever"})
    fresh = svc.ingest("jobright", [listing(2, apply_url="https://jobs.lever.co/acme/def")])
    assert fresh.queued == 1  # new postings use the new support; old ones stay settled


def test_listings_without_an_id_are_ignored(svc):
    assert svc.ingest("jobright", [Listing(external_id="", apply_url=GH.format(n=1))]).new == 0


def test_same_company_tolerates_spelling_but_not_a_different_company():
    from core.company_match import same_company

    assert same_company("Prolific", "Prolific Academic Ltd") and same_company("Acme, Inc.", "ACME")
    assert same_company("", "Anything") and same_company("Anything", "")
    assert not same_company("GitLab", "Prolific") and not same_company("Peraton", "Etsy, Inc.")


def test_jobright_pay_is_kept_as_text_and_read_into_numbers_when_clear(svc):
    report = svc.ingest("jobright", [listing(1, salary_text="$150K/yr - $180K/yr")])
    job = svc.store.get(report.queued_job_ids[0])
    assert job.salary_text == "$150K/yr - $180K/yr"
    assert (job.salary_min, job.salary_max, job.salary_period) == (150000, 180000, "year")


def test_a_short_blurb_from_jobright_is_not_taken_as_the_posting():
    from core import jobright

    blurb = {"jobId": "a1", "jobTitle": "Dev", "companyName": "Acme", "jobSummary": "A summary. " * 45}  # ~490 characters
    full = {"jobId": "a2", "jobTitle": "Dev", "companyName": "Acme", "jobDescription": "The posting. " * 200}  # 2,600
    by_id = {l.external_id: l for l in jobright.extract_listings([blurb, full])}
    assert by_id["a1"].description == "" and len(by_id["a2"].description) > 2000
