"""Loose company-name matching, used to avoid applying twice to the same employer."""

from __future__ import annotations

import re

from core.models import JobStatus, QueueStore

_COMPANY_NOISE = re.compile(
    r"\b(incorporated|inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|gmbh|the|group|holdings)\b"
)


def normalize_company(name: str) -> str:
    """Compare company names loosely: case, punctuation and legal suffixes ("Acme, Inc." == "ACME")."""
    text = re.sub(r"[^\w\s]", " ", (name or "").casefold())
    text = _COMPANY_NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def queued_matches(store: QueueStore, company: str) -> list[dict[str, str]]:
    """Jobs already in the queue for this company (skipped ones do not count; they were never produced)."""
    key = normalize_company(company)
    if not key:
        return []
    return [
        {"company": j.company, "role": j.role_title, "status": j.status.value, "id": j.id}
        for j in store.jobs()
        if j.status != JobStatus.SKIPPED and normalize_company(j.company) == key
    ]


def applied_matches(store: QueueStore, company: str, exclude_id: str = "") -> list[dict[str, str]]:
    """Jobs you have already applied to at this company (only applied jobs count)."""
    key = normalize_company(company)
    if not key:
        return []
    return [
        {"company": j.company, "role": j.role_title, "applied_at": j.applied_at, "id": j.id}
        for j in store.jobs()
        if j.applied_at and j.id != exclude_id and normalize_company(j.company) == key
    ]


def same_company(a: str, b: str) -> bool:
    """True when two names plausibly name one company: equal after normalising, or they share a distinctive word
    ("Prolific" and "Prolific Academic Ltd"). "GitLab" and "Prolific" share nothing, so they differ."""
    ka, kb = normalize_company(a), normalize_company(b)
    if not ka or not kb:
        return True  # nothing to compare: not a reason to doubt
    if ka == kb or ka in kb or kb in ka:
        return True
    return bool({w for w in ka.split() if len(w) >= 3} & {w for w in kb.split() if len(w) >= 3})
