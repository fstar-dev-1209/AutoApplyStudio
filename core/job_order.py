"""Which job goes first: the freshest posting, for resume generation and for applying alike.

A job found by discovery is ranked by when its posting went up (or, when the source gave no date, when it was first
seen). A job somebody added by hand has no posting date; it is the owner's explicit choice, so for generation it goes
first, in the order it was added.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from core.models import JobItem

_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


def moment(text: str | None) -> datetime:
    """An ISO timestamp as an aware datetime; anything unreadable is the earliest possible moment."""
    if not text:
        return _EARLIEST
    try:
        value = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return _EARLIEST
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def posting_times(database) -> dict[str, datetime]:
    """job id -> when its posting went up, for every job discovery created.

    "3 hours ago" style dates are read against the moment the posting was first seen, not against now, so the
    ranking does not drift as time passes."""
    from core.jobright import to_datetime  # late: jobright imports discovery, which imports this package's models

    times: dict[str, datetime] = {}
    rows = database.conn.execute(
        "SELECT job_id, posted_at, first_seen_at FROM discovered_jobs WHERE job_id != ''")
    for row in rows:
        seen = moment(row["first_seen_at"])
        posted = to_datetime(row["posted_at"], seen if seen != _EARLIEST else None) if row["posted_at"] else None
        times[row["job_id"]] = posted or seen
    return times


def generation_order(jobs: Iterable[JobItem], posted: dict[str, datetime]) -> list[JobItem]:
    """Jobs added by hand first, oldest first; then discovered jobs, freshest posting first."""
    jobs = list(jobs)
    manual = [j for j in jobs if j.id not in posted]
    found = [j for j in jobs if j.id in posted]
    manual.sort(key=lambda j: j.sort_order)
    found.sort(key=lambda j: (-posted[j.id].timestamp(), j.sort_order))
    return [*manual, *found]


def application_order(jobs: Iterable[JobItem], posted: dict[str, datetime]) -> list[JobItem]:
    """Jobs to apply to, freshest first. A job with no posting date counts from when its documents were finished."""
    def when(job: JobItem) -> float:
        return (posted.get(job.id) or moment(job.completed_at or job.updated_at)).timestamp()

    return sorted(jobs, key=lambda j: (-when(j), j.sort_order))
