"""The hourly loop: read the Jobright list, open only the jobs the bot can apply to, queue the freshest batch."""

from __future__ import annotations

import random
import threading
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core import ats, jobright
from core.company_match import normalize_company
from core.discovery import (
    QUEUED,
    UNRESOLVED,
    DiscoveryRules,
    DiscoveryService,
    IngestReport,
    Listing,
    newest_first,
)
from core.job_order import moment
from core.settings_store import AppSettings

SOURCE = "jobright"
MAX_DETAILS_PER_RUN = 40  # a safety stop: pages are opened freshest first until the batch is full, never past this

IDLE, RUNNING, OFF, WAITING_LOGIN, HUMAN_CHECK, ERROR, SLEEPING = (
    "idle", "running", "off", "needs_login", "human_check", "error", "outside_hours")


@dataclass
class DiscoveryStatus:
    state: str = OFF
    message: str = ""
    last_run: str = ""
    last_report: dict = field(default_factory=dict)
    next_run: str = ""
    runs: int = 0
    queued_total: int = 0
    detail: list[str] = field(default_factory=list)  # what the last read saw (also written to discovery.log)


def within_hours(now: datetime, tz: str, start: int, end: int) -> bool:
    """True when ``now`` falls inside [start, end) o'clock in ``tz``; 0-24 means always."""
    if start <= 0 and end >= 24:
        return True
    hour = now.astimezone(ZoneInfo(tz)).hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


def human_pause(low: float, high: float) -> None:
    time.sleep(random.uniform(low, high))


def partition_listings(listings: Iterable[Listing], decide: Callable[[Listing], str]) -> tuple[list[Listing], list[Listing]]:
    """Split new listings into pages to open and rows the list already settled.

    Open a Jobright page when the employer link is one the bot can apply to, or when the list had no employer
    link yet. Lever, Ashby, Workday, a normal company site, a blocked company and a duplicate are recorded
    from the list and never visited.
    """
    to_open: list[Listing] = []
    to_record: list[Listing] = []
    for listing in listings:
        if decide(listing) in {QUEUED, UNRESOLVED}:
            to_open.append(listing)
        else:
            to_record.append(listing)
    return to_open, to_record


def keep_posting_for_target(detail: Listing, supported: frozenset[str]) -> Listing:
    """The page we opened to learn a missing link. Keep its text only when the link is one we apply to."""
    if ats.classify(detail.apply_url).ats in supported:
        return detail
    return Listing(
        external_id=detail.external_id, title=detail.title, company=detail.company, location=detail.location,
        source_url=detail.source_url, apply_url=detail.apply_url, posted_at=detail.posted_at,
    )


class DiscoveryRunner:
    def __init__(
        self,
        service: DiscoveryService,
        settings: Callable[[], AppSettings],
        host: Callable[[], object],  # returns a started BrowserHost
        on_queued: Callable[[list[str]], None] = lambda ids: None,
        alert: Callable[[str, str], None] = lambda title, body: None,
        on_report: Callable[[IngestReport], None] = lambda report: None,
        *,
        read=jobright.read_new_listings,
        read_detail=jobright.read_detail,
        pause: Callable[[float, float], None] = human_pause,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        rng: random.Random | None = None,
        log: Callable[[str], None] = lambda text: None,
        debug_dir=None,
    ) -> None:
        self._log, self._debug_dir = log, debug_dir
        self.service, self._settings, self._host = service, settings, host
        self._on_queued, self._alert, self._on_report = on_queued, alert, on_report
        self._read, self._read_detail, self._pause, self._now = read, read_detail, pause, now
        self._rng = rng or random.Random()
        self.status = DiscoveryStatus()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._alerted: str = ""  # the problem already reported, so the loop does not repeat it

    # ------------------------------------------------------------------ thread

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=30)

    def run_soon(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            s = self._settings()
            delay = self._next_delay(s)
            if s.discovery_enabled and self._due(s):
                self.run_once()
            elif not s.discovery_enabled:
                self.status.state, self.status.message = OFF, "Discovery is switched off."
            self.status.next_run = datetime.fromtimestamp(time.time() + delay, tz=timezone.utc).isoformat()
            self._wake.wait(delay)
            self._wake.clear()

    def _next_delay(self, s: AppSettings) -> float:
        base = max(2, s.discovery_interval_minutes) * 60
        return base * self._rng.uniform(0.8, 1.2)  # never on an exact clock

    def _due(self, s: AppSettings) -> bool:
        if within_hours(self._now(), s.bot_timezone, s.active_start_hour, s.active_end_hour):
            return True
        self.status.state, self.status.message = SLEEPING, "Outside the active hours."
        return False

    # ------------------------------------------------------------------ one cycle

    def run_once(self) -> IngestReport | None:
        s = self._settings()
        self.status.state, self.status.message = RUNNING, "Reading Jobright…"
        try:
            report = self._cycle(s)
        except jobright.LoginRequired as exc:
            self.status.detail = list(getattr(exc, "trace", []))
            return self._problem(WAITING_LOGIN, str(exc), "Jobright needs you to sign in")
        except jobright.HumanCheck as exc:
            self.status.detail = list(getattr(exc, "trace", []))
            return self._problem(HUMAN_CHECK, str(exc), "Jobright is asking for a human check")
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not stop the loop
            self.status.detail = traceback.format_exc().strip().splitlines()[-6:]
            return self._problem(ERROR, f"{type(exc).__name__}: {exc}", "")
        self._alerted = ""
        self.status.state, self.status.message = IDLE, (
            f"{report.queued} queued, {report.unsupported} other sites"
            + (f", {report.capped} over the limit" if report.capped else "")
            + (f", {report.expired} old job(s) dropped" if report.expired else ""))
        self.status.last_run = self._now().isoformat()
        self.status.runs += 1
        self.status.queued_total += report.queued
        self.status.last_report = {k: getattr(report, k) for k in
                                   ("seen", "queued", "unsupported", "duplicate", "filtered", "unresolved",
                                    "capped", "expired")}
        if report.queued_job_ids:
            self._on_queued(report.queued_job_ids)
        try:
            self._on_report(report)
        except Exception:  # noqa: BLE001 - a status report must never stop the search
            pass
        return report

    def _cycle(self, s: AppSettings) -> IngestReport:
        host = self._host()
        url = s.discovery_url or jobright.DEFAULT_URL
        known = self.service.known_ids(SOURCE)
        result = host.run(lambda ctx: self._read(ctx, url, is_known=known.__contains__, pause=self._pause,
                                                 debug_dir=self._debug_dir, max_age_hours=s.discovery_max_age_hours), timeout=600)
        recent = [l for l in result.listings if jobright.is_recent(l, s.discovery_max_age_hours, self._now())]
        sample = "; ".join(f"{l.title[:28]} @ {l.posted_at[:19] or '?'}" for l in result.listings[:4])
        fresh = [l for l in recent if l.external_id not in known]
        listed = {l.external_id for l in fresh}
        pending = [Listing(external_id=r["external_id"], title=r["title"], company=r["company"], location=r["location"],
                           source_url=r["source_url"], posted_at=r["posted_at"], apply_url=r["apply_url"])
                   for r in self.service.unresolved(SOURCE) if r["external_id"] not in listed]
        rules = DiscoveryRules.from_text(s.discovery_blocked_companies, s.discovery_exclude_words,
                                         s.salary_floor if s.discovery_skip_below_floor else 0)
        self.service._rules = lambda: rules
        now = self._now()

        # One batch per cycle, freshest first. Jobs of an earlier batch that are still waiting are older than the
        # window; they are dropped (below) once there is something fresher to take their place.
        waiting = self.service.waiting_jobs()
        window = timedelta(minutes=s.discovery_window_minutes())
        stale = [j.id for j in waiting if now - moment(j.created_at) >= window]
        budget = max(0, s.discovery_batch_size() - (len(waiting) - len(stale)))
        if budget == 0:
            # The last batch is still fresh and fills the limit: nothing is taken (and nothing recorded as dropped),
            # so the postings seen now are looked at again by the next cycle.
            self._note([f"Read {len(result.listings)} job(s); the last batch ({len(waiting)} jobs) is still waiting "
                        f"and fills the limit of {s.discovery_batch_size()}, so nothing new was queued."])
            return IngestReport(seen=len([l for l in recent if l.external_id in known]),
                                listed=len(result.listings), recent=len(recent))
        known_keys = set(self.service._known_keys())
        companies = self.service.queue_company_keys(ignore_ids=stale)

        def decide(listing: Listing) -> str:
            state, _reason, link = self.service._decide(listing, rules, known_keys, companies)
            if state == QUEUED:
                if link.key:
                    known_keys.add(link.key)  # a second copy of the same posting in this list is not opened
                if normalize_company(listing.company):
                    companies.add(normalize_company(listing.company))  # nor a second job at the same company
            return state

        candidates = newest_first([*fresh, *pending], now)
        to_open, to_record = partition_listings(candidates, decide)
        opened: list[Listing] = []
        unopened: list[Listing] = []
        problems: list[str] = []
        supported = self.service._supported()
        taken = 0  # postings that will be queued: stop reading pages once the batch is full
        for index, listing in enumerate(to_open):
            if taken >= budget or len(opened) >= MAX_DETAILS_PER_RUN:
                unopened = to_open[index:]
                break
            knew_the_link = bool(listing.apply_url.strip())
            try:
                detail = host.run(lambda ctx, item=listing: self._read_detail(ctx, item, pause=self._pause), timeout=180)
            except (jobright.LoginRequired, jobright.HumanCheck):
                raise
            except Exception as exc:  # noqa: BLE001 - one page failing leaves that job for the next cycle
                problems.append(f"{listing.external_id}: {type(exc).__name__}")
                continue
            item = keep_posting_for_target(detail, supported)
            opened.append(item)
            if knew_the_link:
                taken += 1  # already counted when the list was sorted into pages to open
            elif decide(item) == QUEUED:
                taken += 1
        full = sum(1 for item in opened if jobright.is_full_posting(item.description))
        self._note([f"Read {len(result.listings)} job(s) ({result.from_json} from data, {result.from_dom} from page links); "
                    f"{len(recent)} posted within the last {s.discovery_max_age_hours}h"
                    + (f". First: {sample}" if sample else ""),
                    f"Batch of {budget} (limit {s.discovery_batch_size()}, {len(waiting) - len(stale)} still waiting): "
                    f"opened {len(opened)} Jobright page(s) for applyable jobs ({full} had the full posting); "
                    f"{len(unopened)} fresh job(s) did not fit; left {len(to_record)} other or blocked job(s) unopened."
                    + (f" Could not open: {', '.join(problems)}" if problems else ""),
                    *result.trace[:16]])
        # Only a batch with something fresh in it replaces the jobs still waiting from the last one.
        expired = self.service.expire(stale) if taken and stale else 0
        # Visited pages first, so a duplicate later in the list sees the job just created.
        # Rows the list already settled are recorded with no visit. A list blurb never becomes the job text.
        settled = [l for l in recent if l.external_id in known]
        report = self.service.ingest(
            SOURCE, [*settled, *opened, *unopened, *to_record], limit=budget,
            over_limit_ids=[l.external_id for l in unopened], ignore_ids=stale if expired else (), now=now)
        report.expired = expired
        report.listed, report.recent = len(result.listings), len(recent)
        return report

    def _note(self, lines: list[str]) -> None:
        """Keep what the last read saw for the page, and append it to the log file."""
        self.status.detail = lines
        stamp = self._now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self._log(f"[{stamp}] " + "\n    ".join(lines))

    def _problem(self, state: str, detail: str, alert_title: str) -> None:
        self.status.state, self.status.message = state, detail
        self._note([f"{state}: {detail}"] + self.status.detail[:12])
        self.status.last_run = self._now().isoformat()
        if alert_title and self._alerted != state:
            self._alerted = state
            self._alert(alert_title, detail)
        return None
