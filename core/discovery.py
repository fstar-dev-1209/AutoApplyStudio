"""Turning postings found by a discovery source (Jobright) into queue jobs, once each.

A source hands over :class:`Listing`s. For every one this decides, and records, what became of it:

``queued``       a new queue job was created (the normal pipeline takes it from there)
``unsupported``  its application site is not one the bot handles; kept in the list, never generated
``duplicate``    the same posting is already known (another source, another link spelling, or already in the queue)
``filtered``     your own block rules (company, title words) or the company rules (already applied) rejected it
``unresolved``   no application link yet; the reader retries it when it has one
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core import ats
from core.application_db import ApplicationDatabase
from core.company_match import normalize_company
from core.models import JobItem, JobStatus, QueueStore
from core.salary import parse_salary

QUEUED = "queued"
UNSUPPORTED = "unsupported"
DUPLICATE = "duplicate"
FILTERED = "filtered"
UNRESOLVED = "unresolved"


@dataclass
class Listing:
    external_id: str  # the source's own id for the posting
    title: str = ""
    company: str = ""
    location: str = ""
    source_url: str = ""  # the page on the source
    apply_url: str = ""  # the employer's application page (the original posting), when known
    posted_at: str = ""
    description: str = ""  # when the source already shows the posting text, the app does not have to fetch it
    salary_text: str = ""


@dataclass
class DiscoveryRules:
    blocked_companies: list[str] = field(default_factory=list)
    exclude_title_words: list[str] = field(default_factory=list)
    min_salary: int = 0  # yearly dollars; 0 = do not filter on pay

    @classmethod
    def from_text(cls, companies: str, words: str, min_salary: int = 0) -> "DiscoveryRules":
        split = lambda s: [p.strip() for p in re.split(r"[,\n;]", s or "") if p.strip()]  # noqa: E731
        return cls(split(companies), split(words), max(0, int(min_salary or 0)))

    def reject_reason(self, listing: Listing) -> str:
        company = listing.company.casefold()
        for blocked in self.blocked_companies:
            if blocked.casefold() and blocked.casefold() in company:
                return f"Company on your block list: {blocked}"
        title = listing.title.casefold()
        for word in self.exclude_title_words:
            if word and re.search(rf"(?<!\w){re.escape(word.casefold())}(?!\w)", title):
                return f"Title contains '{word}'"
        if self.min_salary:
            top = posted_maximum(listing.salary_text)
            if top and top < self.min_salary:
                return f"Posted pay tops out at ${top:,} a year, below your minimum of ${self.min_salary:,}"
        return ""


@dataclass
class IngestReport:
    seen: int = 0  # already known and settled: ignored
    queued: int = 0
    unsupported: int = 0
    duplicate: int = 0
    filtered: int = 0
    unresolved: int = 0
    capped: int = 0  # of the filtered ones: good postings left out because the batch was full (not a rule)
    expired: int = 0  # jobs of an earlier batch that were still waiting and were dropped for this one
    listed: int = 0  # postings the source showed in this search (set by the runner)
    recent: int = 0  # of those, posted within the lookback (set by the runner)
    reasons: dict[str, int] = field(default_factory=dict)  # why Greenhouse postings were not queued, by kind
    queued_job_ids: list[str] = field(default_factory=list)

    @property
    def greenhouse(self) -> int:
        """New postings whose application site the bot handles (queued, or left out for a stated reason)."""
        return self.queued + self.duplicate + self.filtered

    @property
    def new(self) -> int:
        return self.queued + self.unsupported + self.duplicate + self.filtered + self.unresolved


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def posted_maximum(salary_text: str) -> int:
    """The top of the pay a posting shows, in dollars a year; 0 unless the text clearly gives one.

    A job is only left out for its pay when that is certain, so these are all "unknown" (0): no amount, not dollars, a
    small number with no unit ("$30 - $35"), and a single open-ended figure ("$60K+", "from $60K")."""
    from core.salary import _period, parse_salary  # late: keeps this module's imports light

    pay = parse_salary(salary_text)
    if not pay.high or pay.currency not in ("", "USD"):
        return 0
    period = _period(pay.text)
    if not period and pay.high < 1000:
        return 0  # the unit was guessed, not stated
    if pay.low == pay.high and re.search(r"\+|\bfrom\b|starting|minimum|at least|\bover\b|\babove\b", pay.text, re.I):
        return 0  # a floor, not a ceiling
    factor = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}.get(period or "year", 1)
    return int(pay.high * factor)


def reason_kind(state: str, reason: str) -> str:
    """A short, countable name for why a posting was not queued (for the status report)."""
    if state == DUPLICATE:
        return "same posting already known"
    for start, kind in (("Already applied", "company already applied to"),
                        ("Company folder already exists", "company folder already exists"),
                        ("Another job at", "another job at the company is queued"),
                        ("Company on your block list", "company on your block list"),
                        ("Title contains", "title word you reject"),
                        ("Posted pay tops out", "pay below your minimum"),
                        ("Over this batch", "over this hour's limit")):
        if reason.startswith(start):
            return kind
    return reason[:40] or "other"


def newest_first(listings: Iterable[Listing], now: datetime | None = None) -> list[Listing]:
    """The freshest posting first. A posting with no readable date is taken as brand new (the list it came from
    is itself newest first); equal dates keep the order they came in."""
    from core.jobright import to_datetime  # late: jobright imports this module

    now = now or datetime.now(timezone.utc)

    def age_key(listing: Listing) -> float:
        posted = to_datetime(listing.posted_at, now) if listing.posted_at else None
        return -(posted or now).timestamp()

    return sorted(listings, key=age_key)  # sorted() is stable


EXPIRED = "Expired: not generated before the next batch of fresh jobs arrived"


class DiscoveryService:
    def __init__(
        self,
        store: QueueStore,
        rules: Callable[[], DiscoveryRules] = DiscoveryRules,
        company_check: Callable[[str], str] | None = None,
        supported: Callable[[], frozenset[str]] = lambda: ats.SUPPORTED,
    ) -> None:
        """``company_check(company)`` returns a reason to skip a company (already applied, folder exists) or ''."""
        self.store = store
        self.db: ApplicationDatabase = store.database
        self._rules = rules
        self._company_check = company_check
        self._supported = supported

    # ------------------------------------------------------------------ ingest

    def ingest(
        self,
        source: str,
        listings: Iterable[Listing],
        *,
        limit: int | None = None,
        over_limit_ids: Iterable[str] = (),
        ignore_ids: Iterable[str] = (),
        now: datetime | None = None,
    ) -> IngestReport:
        """Queue the new, acceptable postings: freshest first, at most ``limit`` of them.

        Every posting this does not queue is recorded with the reason, so it is never looked at again: a posting
        that did not fit in this batch is not kept for a later one (by then it would be old news).
        ``over_limit_ids`` are postings the caller already knows did not fit (it never opened their pages).
        ``ignore_ids`` are queue jobs that are being dropped and so must not count as "this company is queued"."""
        report = IngestReport()
        known_keys = self._known_keys()
        companies = self.queue_company_keys(ignore_ids)
        over = set(over_limit_ids)
        new_jobs: list[JobItem] = []
        rules = self._rules()
        for listing in newest_first(listings, now):
            if not listing.external_id:
                continue
            row = self._row(source, listing.external_id)
            if row is not None and row["state"] != UNRESOLVED:
                report.seen += 1
                continue
            state, reason, link = self._decide(listing, rules, known_keys, companies)
            if state in {QUEUED, UNRESOLVED} and (
                    listing.external_id in over or (state == QUEUED and limit is not None and len(new_jobs) >= limit)):
                state = FILTERED
                reason = (f"Over this batch's limit of {limit} new jobs (freshest first)" if limit is not None
                          else "Over this batch's limit (freshest first)")
                report.capped += 1
            if state in {FILTERED, DUPLICATE}:
                kind = reason_kind(state, reason)
                report.reasons[kind] = report.reasons.get(kind, 0) + 1
            job: JobItem | None = None
            if state == QUEUED:
                job = self._make_job(source, listing)
                new_jobs.append(job)
                known_keys.add(link.key)
                company = normalize_company(listing.company)
                if company:
                    companies.add(company)
                report.queued_job_ids.append(job.id)
            self._record(source, listing, link, state, reason, job.id if job else "", existing=row)
            setattr(report, state, getattr(report, state) + 1)
        if new_jobs:
            self.store.add_many(new_jobs)
        return report

    def _decide(self, listing: Listing, rules: DiscoveryRules, known: set[str],
                companies: set[str] | None = None) -> tuple[str, str, ats.AtsLink]:
        """What becomes of a posting. ``companies`` holds the (normalised) companies that already have a job in the
        queue or were taken earlier in this batch: a second job at one of them would only be skipped later."""
        if not listing.apply_url.strip():
            return UNRESOLVED, "No application link yet", ats.AtsLink("")
        link = ats.classify(listing.apply_url)
        if not link.ats:
            return UNSUPPORTED, "Not a known application site", link
        if link.ats not in self._supported():
            return UNSUPPORTED, f"{link.ats.title()} is not automated yet", link
        if link.key and link.key in known:
            return DUPLICATE, "Already known", link
        why = rules.reject_reason(listing)
        if why:
            return FILTERED, why, link
        if self._company_check and listing.company.strip():
            why = self._company_check(listing.company)
            if why:
                return FILTERED, why, link
        if companies is not None and normalize_company(listing.company) in companies:
            return FILTERED, f"Another job at {listing.company.strip()} is already in the queue", link
        return QUEUED, "", link

    def _make_job(self, source: str, listing: Listing) -> JobItem:
        job = JobItem.create(listing.apply_url.strip(), source=source)
        job.company = listing.company.strip()
        job.role_title = listing.title.strip()
        job.job_description = listing.description.strip()
        pay = parse_salary(listing.salary_text)  # keeps Jobright's wording and fills the numbers when they are clear
        job.salary_text, job.salary_min, job.salary_max = pay.text, pay.low, pay.high
        job.salary_period, job.salary_currency = pay.period, pay.currency
        return job

    # ------------------------------------------------------------------ storage

    def _row(self, source: str, external_id: str):
        return self.db.conn.execute(
            "SELECT * FROM discovered_jobs WHERE source = ? AND external_id = ?", (source, external_id)
        ).fetchone()

    def _record(self, source: str, listing: Listing, link: ats.AtsLink, state: str, reason: str, job_id: str,
                existing) -> None:
        values = (listing.title, listing.company, listing.location, listing.source_url, listing.apply_url,
                  link.ats, link.key, listing.posted_at, state, reason, job_id)
        with self.db.transaction() as conn:
            if existing is None:
                conn.execute(
                    "INSERT INTO discovered_jobs(title, company, location, source_url, apply_url, ats, ats_key, "
                    "posted_at, state, reason, job_id, source, external_id, first_seen_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*values, source, listing.external_id, _now()),
                )
            else:
                conn.execute(
                    "UPDATE discovered_jobs SET title=?, company=?, location=?, source_url=?, apply_url=?, ats=?, "
                    "ats_key=?, posted_at=?, state=?, reason=?, job_id=? WHERE id=?",
                    (*values, existing["id"]),
                )

    # ------------------------------------------------------------------ batches: what is waiting, what is stale

    def waiting_jobs(self) -> list[JobItem]:
        """Jobs discovery queued that have not started: nothing generated yet, nobody working on them."""
        mine = {r["job_id"] for r in self.db.conn.execute("SELECT job_id FROM discovered_jobs WHERE job_id != ''")}
        return [j for j in self.store.jobs()
                if j.id in mine and j.status == JobStatus.QUEUED and j.resume_json is None and not j.archived_at]

    def queue_company_keys(self, ignore_ids: Iterable[str] = ()) -> set[str]:
        """Normalised companies that already have a job in the queue (skipped ones were never produced: they do not
        count). ``ignore_ids`` leaves out jobs that are about to be dropped."""
        skip = set(ignore_ids)
        return {key for j in self.store.jobs()
                if j.status != JobStatus.SKIPPED and j.id not in skip and (key := normalize_company(j.company))}

    def expire(self, job_ids: Iterable[str], reason: str = EXPIRED) -> int:
        """Drop waiting jobs of an earlier batch. They stay in the queue as Skipped with the reason (so you can see
        what was dropped, and Retry or re-add one by hand), and are never picked up again."""
        dropped = 0
        for job_id in job_ids:
            job = self.store.get(job_id)
            if job is None or job.status != JobStatus.QUEUED or job.resume_json is not None:
                continue  # it started in the meantime, or already has a resume: not stale
            job.skip_reason = reason
            job.set_status(JobStatus.SKIPPED, "Expired")
            job.log(reason)
            self.store.update(job)
            self.store.add_event(job_id, "skipped", {"reason": reason, "expired": True})
            dropped += 1
        return dropped

    def _known_keys(self) -> set[str]:
        """Identities of every posting already discovered or sitting in the queue (whatever its link looked like)."""
        keys = {r["ats_key"] for r in self.db.conn.execute(
            "SELECT ats_key FROM discovered_jobs WHERE ats_key != '' AND state IN (?, ?)", (QUEUED, DUPLICATE))}
        for r in self.db.conn.execute("SELECT url, origin_url FROM jobs"):
            for link in (r["url"], r["origin_url"]):
                key = ats.classify(link).key if link else ""
                if key:
                    keys.add(key)
        return keys

    # ------------------------------------------------------------------ reading back

    def recent(self, state: str | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM discovered_jobs", ()
        if state:
            sql, args = sql + " WHERE state = ?", (state,)
        rows = self.db.conn.execute(sql + " ORDER BY first_seen_at DESC, id DESC LIMIT ?", (*args, limit))
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self.db.conn.execute("SELECT state, COUNT(*) AS n FROM discovered_jobs GROUP BY state")
        return {r["state"]: r["n"] for r in rows}

    def known_ids(self, source: str) -> set[str]:
        """Postings from ``source`` already settled (queued, set aside or rejected): the reader stops at these."""
        rows = self.db.conn.execute(
            "SELECT external_id FROM discovered_jobs WHERE source = ? AND state != ?", (source, UNRESOLVED))
        return {r["external_id"] for r in rows}

    def unresolved(self, source: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM discovered_jobs WHERE source = ? AND state = ?", (source, UNRESOLVED))
        return [dict(r) for r in rows]
