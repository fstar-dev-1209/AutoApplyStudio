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
from core.models import JobItem, QueueStore
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

    @classmethod
    def from_text(cls, companies: str, words: str) -> "DiscoveryRules":
        split = lambda s: [p.strip() for p in re.split(r"[,\n;]", s or "") if p.strip()]  # noqa: E731
        return cls(split(companies), split(words))

    def reject_reason(self, listing: Listing) -> str:
        company = listing.company.casefold()
        for blocked in self.blocked_companies:
            if blocked.casefold() and blocked.casefold() in company:
                return f"Company on your block list: {blocked}"
        title = listing.title.casefold()
        for word in self.exclude_title_words:
            if word and re.search(rf"(?<!\w){re.escape(word.casefold())}(?!\w)", title):
                return f"Title contains '{word}'"
        return ""


@dataclass
class IngestReport:
    seen: int = 0  # already known and settled: ignored
    queued: int = 0
    unsupported: int = 0
    duplicate: int = 0
    filtered: int = 0
    unresolved: int = 0
    queued_job_ids: list[str] = field(default_factory=list)

    @property
    def new(self) -> int:
        return self.queued + self.unsupported + self.duplicate + self.filtered + self.unresolved


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def ingest(self, source: str, listings: Iterable[Listing]) -> IngestReport:
        report = IngestReport()
        known_keys = self._known_keys()
        new_jobs: list[JobItem] = []
        rules = self._rules()
        for listing in listings:
            if not listing.external_id:
                continue
            row = self._row(source, listing.external_id)
            if row is not None and row["state"] != UNRESOLVED:
                report.seen += 1
                continue
            state, reason, link = self._decide(listing, rules, known_keys)
            job: JobItem | None = None
            if state == QUEUED:
                job = self._make_job(source, listing)
                new_jobs.append(job)
                known_keys.add(link.key)
                report.queued_job_ids.append(job.id)
            self._record(source, listing, link, state, reason, job.id if job else "", existing=row)
            setattr(report, state, getattr(report, state) + 1)
        if new_jobs:
            self.store.add_many(new_jobs)
        return report

    def _decide(self, listing: Listing, rules: DiscoveryRules, known: set[str]) -> tuple[str, str, ats.AtsLink]:
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
