"""Timestamp handling.

Storage rule: every instant is stored as an ISO-8601 UTC string.
Display / filtering rule: calendar boundaries (today, this month, ...) are local-time
boundaries, converted to UTC instants before comparing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo

PRESETS = [
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("last_7", "Last 7 days"),
    ("last_30", "Last 30 days"),
    ("this_month", "This month"),
    ("prev_month", "Previous month"),
    ("custom", "Custom range"),
    ("all", "All time"),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """Aware -> UTC ISO. Naive datetimes are interpreted as local time."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def now_iso() -> str:
    return to_iso(utc_now())


def parse_iso(value: str | None, tz: tzinfo | None = None) -> datetime | None:
    """Parse stored/user text into an aware UTC datetime.

    Date-only input ("2026-01-12") becomes local noon so it stays on that calendar day
    in any nearby timezone. Naive timestamps are interpreted in ``tz`` (default: local).
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            naive = datetime.combine(date.fromisoformat(text), time(12, 0))
            return _localize(naive, tz).astimezone(timezone.utc)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = _localize(dt, tz)
    return dt.astimezone(timezone.utc)


def _localize(naive: datetime, tz: tzinfo | None) -> datetime:
    if tz is None:
        return naive.astimezone()
    return naive.replace(tzinfo=tz)


def to_local(dt: datetime, tz: tzinfo | None = None) -> datetime:
    return dt.astimezone(tz) if tz else dt.astimezone()


def local_date(dt: datetime, tz: tzinfo | None = None) -> date:
    return to_local(dt, tz).date()


def local_day_start(day: date, tz: tzinfo | None = None) -> datetime:
    """UTC instant of local midnight at the start of ``day``."""
    return _localize(datetime.combine(day, time.min), tz).astimezone(timezone.utc)


def format_local(value: str | datetime | None, fmt: str = "%Y-%m-%d %H:%M", tz: tzinfo | None = None) -> str:
    dt = parse_iso(value, tz) if isinstance(value, str) else value
    if dt is None:
        return ""
    return to_local(dt, tz).strftime(fmt)


@dataclass(frozen=True)
class DateRange:
    """Half-open [start, end) in UTC. ``None`` bound means unbounded."""

    start: datetime | None
    end: datetime | None
    label: str = ""
    first_day: date | None = None  # local calendar days covered (inclusive), when bounded
    last_day: date | None = None

    def contains(self, dt: datetime | None) -> bool:
        if dt is None:
            return False
        if self.start is not None and dt < self.start:
            return False
        if self.end is not None and dt >= self.end:
            return False
        return True


def range_for(
    preset: str,
    today: date | None = None,
    tz: tzinfo | None = None,
    custom: tuple[date | None, date | None] = (None, None),
) -> DateRange:
    today = today or to_local(utc_now(), tz).date()
    label = dict(PRESETS).get(preset, preset)
    if preset == "all":
        return DateRange(None, None, label)
    if preset == "today":
        first = last = today
    elif preset == "yesterday":
        first = last = today - timedelta(days=1)
    elif preset == "last_7":
        first, last = today - timedelta(days=6), today
    elif preset == "last_30":
        first, last = today - timedelta(days=29), today
    elif preset == "this_month":
        first, last = today.replace(day=1), today
    elif preset == "prev_month":
        last = today.replace(day=1) - timedelta(days=1)
        first = last.replace(day=1)
    elif preset == "custom":
        first, last = custom
        if first is None and last is None:
            return DateRange(None, None, label)
        first = first or last
        last = last or first
        if first > last:
            first, last = last, first
    else:
        raise ValueError(f"unknown range preset: {preset}")
    return DateRange(
        local_day_start(first, tz),
        local_day_start(last + timedelta(days=1), tz),
        label,
        first,
        last,
    )


def tz_from_name(name: str | None) -> tzinfo | None:
    """IANA zone name -> tzinfo; empty/unknown means the system local zone."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - missing tzdata or bad name
        return None


def wall_to_utc(naive: datetime, tz_name: str = "") -> datetime:
    """Wall-clock time as typed in zone ``tz_name`` -> aware UTC."""
    return _localize(naive, tz_from_name(tz_name)).astimezone(timezone.utc)


def utc_to_wall(dt: datetime, tz_name: str = "") -> datetime:
    """Aware instant -> naive wall-clock time in zone ``tz_name`` (system local if empty)."""
    return to_local(dt, tz_from_name(tz_name)).replace(tzinfo=None)


def format_in_zone(value: str | None, tz_name: str = "", fmt: str = "%a %b %d, %H:%M") -> str:
    dt = parse_iso(value)
    if dt is None:
        return ""
    local = to_local(dt, tz_from_name(tz_name))
    return f"{local.strftime(fmt)} {local.tzname() or ''}".strip()
