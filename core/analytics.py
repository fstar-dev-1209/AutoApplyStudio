"""Job-search analytics computed from stored events (nothing hardcoded).

Two kinds of numbers, deliberately kept apart:

* **Period counts** - things that *happened* inside the selected range, dated by their
  event timestamp ("applications submitted", "replies", "interview invitations", ...).
* **Cohort rates** - of the applications *submitted in the range*, what share ever got a
  reply / reached a stage / got an offer, measured as of now. A rate is a ``Rate`` carrying
  numerator and denominator; with an empty denominator it renders "-" rather than 0%.

Edge-case definitions
---------------------
* Archived applications keep counting everywhere (their events happened). They are only
  excluded from the "right now" snapshot tiles (awaiting reply / ready not applied).
* Skipped duplicate-company jobs count only as "skipped duplicates"/"processed"; they never
  become applications, so they never enter application metrics. A job the user explicitly
  allowed past the duplicate rule is treated like any other job.
* Reopened applications are counted once: "submitted" is the first applied event; stage
  invitations are the first time a stage was reached; a reopen clears only the outcome. A
  rejection counts in the range of the rejection that currently stands.
* Document counts are attempt results: a retry that succeeds adds one "generated".
* "Reached stage X" in rates means X *or any later stage* (stages can be skipped).
* A reply is the first non-automated message, stage advance past Applied, scheduled
  interview or rejection. Automated acknowledgements and withdrawals are not replies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from statistics import mean, median
from typing import Any

from core.application_stage import Outcome, Stage
from core.lifecycle import EV, Event, Facts, replay
from core.timeutil import DateRange, local_date, parse_iso, utc_now

MAX_DAILY_BUCKETS = 92


@dataclass(frozen=True)
class Rate:
    num: int
    den: int

    @property
    def value(self) -> float | None:
        return None if self.den <= 0 else self.num / self.den

    @property
    def text(self) -> str:
        v = self.value
        return "—" if v is None else f"{v * 100:.0f}%"

    @property
    def detail(self) -> str:
        return f"{self.num} of {self.den}" if self.den else "no data"


@dataclass
class Series:
    labels: list[str]
    values: list[int]
    bucket: str = "day"  # day | week | month


@dataclass
class CohortSeries:
    """Per week / month: bids sent in that bucket, and how many of those went on to each stage (as of now)."""

    labels: list[str] = field(default_factory=list)
    bids: list[int] = field(default_factory=list)
    replies: list[int] = field(default_factory=list)
    screening: list[int] = field(default_factory=list)
    technical: list[int] = field(default_factory=list)
    final: list[int] = field(default_factory=list)
    offers: list[int] = field(default_factory=list)
    bucket: str = "week"

    def rows(self) -> list[tuple[str, list[int]]]:
        return [("Bids", self.bids), ("Replies", self.replies), ("Screening", self.screening),
                ("Technical", self.technical), ("Final", self.final), ("Offers", self.offers)]


@dataclass
class FunnelStep:
    name: str
    count: int
    of_applied: Rate
    of_previous: Rate


@dataclass
class Analytics:
    range: DateRange
    # document productivity
    imported: int = 0
    processed: int = 0
    generated: int = 0
    failed: int = 0
    skipped_duplicates: int = 0
    ready_not_applied: int = 0  # snapshot
    # application activity
    submitted: int = 0
    awaiting_reply: int = 0  # snapshot
    responses: int = 0
    positive_responses: int = 0
    rejections: int = 0
    # interviews
    screening_invites: int = 0
    screening_done: int = 0
    technical_invites: int = 0
    technical_done: int = 0
    final_invites: int = 0
    final_done: int = 0
    offers: int = 0
    # rates
    generation_success: Rate = Rate(0, 0)
    submission: Rate = Rate(0, 0)
    reply: Rate = Rate(0, 0)
    screening: Rate = Rate(0, 0)
    technical: Rate = Rate(0, 0)
    final: Rate = Rate(0, 0)
    offer: Rate = Rate(0, 0)
    cohort_size: int = 0
    # trends
    applications_series: Series = field(default_factory=lambda: Series([], []))
    replies_series: Series = field(default_factory=lambda: Series([], []))
    interviews_series: Series = field(default_factory=lambda: Series([], [], "week"))
    invites_series: Series = field(default_factory=lambda: Series([], []))  # first screening/technical/final invites
    monthly_series: Series = field(default_factory=lambda: Series([], [], "month"))
    funnel: list[FunnelStep] = field(default_factory=list)
    weekly: CohortSeries = field(default_factory=CohortSeries)
    monthly_cohort: CohortSeries = field(default_factory=lambda: CohortSeries(bucket="month"))
    days_to_response: list[float] = field(default_factory=list)
    status_mix: list[tuple[str, int]] = field(default_factory=list)  # where this period's applications stand now
    top_companies: list[tuple[str, int]] = field(default_factory=list)  # applications submitted per company

    @property
    def median_days_to_response(self) -> float | None:
        return median(self.days_to_response) if self.days_to_response else None

    @property
    def mean_days_to_response(self) -> float | None:
        return mean(self.days_to_response) if self.days_to_response else None


@dataclass
class JobLite:
    id: str
    created_at: datetime | None
    status: str
    archived: bool
    events: list[Event]
    facts: Facts
    company: str = ""


@dataclass
class AnalyticsData:
    jobs: list[JobLite]
    interviews: list[dict[str, Any]]


def load_data(db) -> AnalyticsData:
    events = db.all_effective_events()
    jobs = []
    for r in db.conn.execute("SELECT id, created_at, status, archived_at, company FROM jobs"):
        evs = events.get(r["id"], [])
        jobs.append(JobLite(r["id"], parse_iso(r["created_at"]), r["status"], bool(r["archived_at"]), evs, replay(evs),
                            r["company"] or ""))
    interviews = [dict(r) for r in db.conn.execute("SELECT * FROM interviews")]
    return AnalyticsData(jobs, interviews)


def compute(
    data: AnalyticsData,
    rng: DateRange,
    tz: tzinfo | None = None,
    now: datetime | None = None,
) -> Analytics:
    now = now or utc_now()
    a = Analytics(range=rng)
    cohort: list[JobLite] = []
    generated_cohort: list[JobLite] = []
    first_reach_in_range = {s: 0 for s in (Stage.SCREENING, Stage.TECHNICAL, Stage.FINAL, Stage.OFFER)}
    done_by_kind = {"screening": 0, "technical": 0, "final": 0}

    for j in data.jobs:
        f = j.facts
        if rng.contains(j.created_at):
            a.imported += 1
        for e in j.events:
            if not rng.contains(e.when):
                continue
            if e.type == EV.DOCS_GENERATED:
                a.generated += 1
            elif e.type == EV.GENERATION_FAILED:
                a.failed += 1
            elif e.type == EV.SKIPPED_DUPLICATE:
                a.skipped_duplicates += 1
            elif e.type == EV.INTERVIEW_COMPLETED:
                kind = e.payload.get("kind")
                if kind in done_by_kind:
                    done_by_kind[kind] += 1
        # Snapshots ("right now"): archived rows are hidden from them.
        if not j.archived:
            if j.status == "Done" and not f.applied and not f.outcome:
                a.ready_not_applied += 1
            if f.applied and not f.outcome and not f.on_hold and f.first_response_at is None:
                a.awaiting_reply += 1
        if rng.contains(f.applied_at):
            a.submitted += 1
            cohort.append(j)
            if f.first_response_at is not None and f.applied_at is not None:
                a.days_to_response.append(max(0.0, (f.first_response_at - f.applied_at).total_seconds() / 86400))
        if rng.contains(f.first_response_at):
            a.responses += 1
        if rng.contains(f.first_positive_at):
            a.positive_responses += 1
        if f.outcome == Outcome.REJECTED and rng.contains(f.outcome_at):
            a.rejections += 1
        for stage in first_reach_in_range:
            if rng.contains(f.first_reach.get(stage)):
                first_reach_in_range[stage] += 1
        if rng.contains(f.docs_generated_at):
            generated_cohort.append(j)

    a.processed = a.generated + a.failed + sum(
        1 for j in data.jobs for e in j.events if rng.contains(e.when) and e.type in (EV.SKIPPED_DUPLICATE, EV.SKIPPED)
    )
    a.screening_invites = first_reach_in_range[Stage.SCREENING]
    a.technical_invites = first_reach_in_range[Stage.TECHNICAL]
    a.final_invites = first_reach_in_range[Stage.FINAL]
    a.offers = first_reach_in_range[Stage.OFFER]
    a.screening_done, a.technical_done, a.final_done = (
        done_by_kind["screening"], done_by_kind["technical"], done_by_kind["final"],
    )

    # ---- cohort rates (applications submitted in the range) ----
    a.cohort_size = len(cohort)
    n = len(cohort)
    replied = sum(1 for j in cohort if j.facts.first_response_at is not None)
    reached = {s: sum(1 for j in cohort if j.facts.reached(s) is not None)
               for s in (Stage.SCREENING, Stage.TECHNICAL, Stage.FINAL, Stage.OFFER)}
    a.reply = Rate(replied, n)
    a.screening = Rate(reached[Stage.SCREENING], n)
    a.technical = Rate(reached[Stage.TECHNICAL], n)
    a.final = Rate(reached[Stage.FINAL], n)
    a.offer = Rate(reached[Stage.OFFER], n)
    a.generation_success = Rate(a.generated, a.generated + a.failed)
    a.submission = Rate(sum(1 for j in generated_cohort if j.facts.applied), len(generated_cohort))

    steps = [
        ("Applied", n),
        ("Replied", replied),
        ("Screening", reached[Stage.SCREENING]),
        ("Technical", reached[Stage.TECHNICAL]),
        ("Final", reached[Stage.FINAL]),
        ("Offer", reached[Stage.OFFER]),
    ]
    prev = n
    for name, count in steps:
        a.funnel.append(FunnelStep(name, count, Rate(count, n), Rate(count, prev)))
        prev = count

    # ---- where the period's applications stand now, and who they were sent to ----
    mix = {"Offer": 0, "Interviewing": 0, "Replied": 0, "Waiting": 0, "Rejected": 0, "Withdrawn / closed": 0}
    per_company: dict[str, int] = {}
    for j in cohort:
        f = j.facts
        if f.outcome == Outcome.REJECTED:
            mix["Rejected"] += 1
        elif f.outcome in (Outcome.WITHDRAWN, Outcome.CLOSED):
            mix["Withdrawn / closed"] += 1
        elif f.outcome == Outcome.ACCEPTED or f.stage == Stage.OFFER:
            mix["Offer"] += 1
        elif f.stage.rank >= Stage.SCREENING.rank:
            mix["Interviewing"] += 1
        elif f.first_response_at is not None:
            mix["Replied"] += 1
        else:
            mix["Waiting"] += 1
        key = (j.company or "Unknown").strip()
        per_company[key] = per_company.get(key, 0) + 1
    a.status_mix = [(k, v) for k, v in mix.items() if v]
    a.top_companies = sorted(per_company.items(), key=lambda kv: (-kv[1], kv[0].casefold()))[:6]

    # ---- trends ----
    first, last = _span(data, rng, tz, now)
    days = (last - first).days + 1
    step = 1 if days <= MAX_DAILY_BUCKETS else 7
    apps = [j.facts.applied_at for j in data.jobs if rng.contains(j.facts.applied_at)]
    reps = [j.facts.first_response_at for j in data.jobs if rng.contains(j.facts.first_response_at)]
    a.applications_series = _bucket(apps, first, last, step, tz)
    a.replies_series = _bucket(reps, first, last, step, tz)
    invites = [t for j in data.jobs for st in (Stage.SCREENING, Stage.TECHNICAL, Stage.FINAL)
               if rng.contains(t := j.facts.first_reach.get(st))]
    a.invites_series = _bucket(invites, first, last, step, tz)

    sched = []
    for iv in data.interviews:
        if iv.get("status") == "Cancelled":
            continue
        dt = parse_iso(iv.get("scheduled_at"))
        if rng.contains(dt):
            sched.append(dt)
    a.interviews_series = _bucket(sched, first, last, 7, tz, week_aligned=True)

    a.weekly = _cohort_series(cohort, rng, tz, "week", 12)
    a.monthly_cohort = _cohort_series(cohort, rng, tz, "month", 12)
    end_month = last.replace(day=1)
    months = _month_starts(end_month, 12 if not (rng.start is None and rng.end is None) else _months_all(data, end_month, tz))
    counts = {m: 0 for m in months}
    for j in data.jobs:
        ap = j.facts.applied_at
        if ap is not None:
            m = local_date(ap, tz).replace(day=1)
            if m in counts:
                counts[m] += 1
    a.monthly_series = Series([m.strftime("%b %Y") for m in months], [counts[m] for m in months], "month")
    return a


def _cohort_series(cohort: list[JobLite], rng: DateRange, tz, kind: str, cap: int) -> CohortSeries:
    """Bucket the period's bids by the week/month they were sent; each bucket counts how far those bids got."""
    out = CohortSeries(bucket=kind)
    sent = [(j, local_date(j.facts.applied_at, tz)) for j in cohort if j.facts.applied_at]
    if rng.first_day and rng.last_day:
        first, last = rng.first_day, rng.last_day
    elif sent:
        first, last = min(d for _j, d in sent), max(d for _j, d in sent)
    else:
        return out

    def start_of(d: date) -> date:
        return d - timedelta(days=d.weekday()) if kind == "week" else d.replace(day=1)

    def advance(d: date) -> date:
        if kind == "week":
            return d + timedelta(days=7)
        return date(d.year + (d.month == 12), d.month % 12 + 1, 1)

    starts, d = [], start_of(first)
    while d <= last:
        starts.append(d)
        d = advance(d)
    starts = starts[-cap:]
    if not starts:
        return out
    index = {s: i for i, s in enumerate(starts)}
    n = len(starts)
    cols = {k: [0] * n for k in ("bids", "replies", "screening", "technical", "final", "offers")}
    for j, day in sent:
        i = index.get(start_of(day))
        if i is None:
            continue
        f = j.facts
        cols["bids"][i] += 1
        cols["replies"][i] += f.first_response_at is not None
        cols["screening"][i] += f.reached(Stage.SCREENING) is not None
        cols["technical"][i] += f.reached(Stage.TECHNICAL) is not None
        cols["final"][i] += f.reached(Stage.FINAL) is not None
        cols["offers"][i] += f.reached(Stage.OFFER) is not None
    fmt = "%b %d" if kind == "week" else "%b %Y"
    out.labels = [st.strftime(fmt).replace(" 0", " ") for st in starts]
    for k, v in cols.items():
        setattr(out, k, v)
    return out


def _span(data: AnalyticsData, rng: DateRange, tz: tzinfo | None, now: datetime) -> tuple[date, date]:
    today = local_date(now, tz)
    if rng.first_day and rng.last_day:
        return rng.first_day, rng.last_day
    stamps = [j.facts.applied_at for j in data.jobs if j.facts.applied_at]
    stamps += [j.facts.first_response_at for j in data.jobs if j.facts.first_response_at]
    stamps += [parse_iso(i.get("scheduled_at")) for i in data.interviews if i.get("scheduled_at")]
    stamps = [s for s in stamps if s]
    if not stamps:
        return today, today
    return min(local_date(s, tz) for s in stamps), max(today, max(local_date(s, tz) for s in stamps))


def _bucket(stamps, first: date, last: date, step: int, tz, week_aligned: bool = False) -> Series:
    if week_aligned:
        first = first - timedelta(days=first.weekday())
        step = 7
    starts = []
    d = first
    while d <= last:
        starts.append(d)
        d += timedelta(days=step)
    counts = [0] * len(starts)
    for s in stamps:
        if s is None:
            continue
        idx = (local_date(s, tz) - first).days // step
        if 0 <= idx < len(counts):
            counts[idx] += 1
    if step == 1:
        labels = [d.strftime("%b %d").replace(" 0", " ") for d in starts]
        kind = "day"
    else:
        labels = [d.strftime("%b %d").replace(" 0", " ") for d in starts]
        kind = "week"
    return Series(labels, counts, kind)


def _month_starts(end_month: date, count: int) -> list[date]:
    out: list[date] = []
    y, m = end_month.year, end_month.month
    for _ in range(max(1, count)):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def _months_all(data: AnalyticsData, end_month: date, tz) -> int:
    stamps = [j.facts.applied_at for j in data.jobs if j.facts.applied_at]
    if not stamps:
        return 1
    first = min(local_date(s, tz) for s in stamps)
    return (end_month.year - first.year) * 12 + end_month.month - first.month + 1


def bot_sent(db, rng: DateRange) -> dict[str, int]:
    """Applications the auto-apply bot sent inside ``rng``, by application site (``{"total": n, "greenhouse": n}``).

    Counted from the bot's own attempt records, so an application you sent by hand is never counted as the bot's."""
    sql, args = ("SELECT adapter, COUNT(*) AS n FROM apply_attempts WHERE state = 'submitted'", [])
    if rng.start is not None:
        sql, args = sql + " AND finished_at >= ?", [*args, rng.start.isoformat()]
    if rng.end is not None:
        sql, args = sql + " AND finished_at < ?", [*args, rng.end.isoformat()]
    rows = db.conn.execute(sql + " GROUP BY adapter", tuple(args))
    out = {r["adapter"] or "other": int(r["n"]) for r in rows}
    out["total"] = sum(out.values())
    return out
