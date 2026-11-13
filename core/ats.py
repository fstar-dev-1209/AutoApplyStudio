"""Which applicant-tracking system a job link belongs to, and which of them the auto-apply bot handles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"
WORKABLE = "workable"
RIPPLING = "rippling"
WORKDAY = "workday"

# The adapters that exist. Everything else is still queued by hand if you want, but never applied to by the bot.
SUPPORTED = frozenset({GREENHOUSE})


@dataclass(frozen=True)
class AtsLink:
    ats: str  # one of the constants above, or "" when the link is not a known ATS
    board: str = ""  # the company's slug / tenant on that ATS
    job_id: str = ""
    embedded: bool = False  # the form lives in an iframe on the company's own site
    url: str = ""

    @property
    def supported(self) -> bool:
        return self.ats in SUPPORTED

    @property
    def key(self) -> str:
        """Stable identity of the posting across link spellings (tracking parameters, mirrors)."""
        return f"{self.ats}:{self.board.lower()}:{self.job_id.lower()}" if self.ats and self.job_id else ""


_GH_HOSTS = re.compile(r"^(boards|job-boards)(\.eu)?\.greenhouse\.io$")
_WD_HOST = re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$")


def classify(url: str) -> AtsLink:
    """Identify the ATS behind ``url`` (host and path only; no network)."""
    raw = (url or "").strip()
    parts = urlsplit(raw if "://" in raw else "https://" + raw)
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    query = parse_qs(parts.query)

    if _GH_HOSTS.match(host):
        if segs[:2] == ["embed", "job_app"]:
            board = (query.get("for") or [""])[0]
            job = (query.get("token") or [""])[0]
            return AtsLink(GREENHOUSE, board, job, True, raw)
        if len(segs) >= 3 and segs[1] == "jobs":
            return AtsLink(GREENHOUSE, segs[0], segs[2], False, raw)
        return AtsLink(GREENHOUSE, segs[0] if segs else "", "", False, raw)

    if host == "jobs.lever.co" and segs:
        return AtsLink(LEVER, segs[0], segs[1] if len(segs) > 1 else "", False, raw)
    if host in {"jobs.ashbyhq.com", "ashbyhq.com"} and segs:
        return AtsLink(ASHBY, segs[0], segs[1] if len(segs) > 1 else "", False, raw)
    if host == "apply.workable.com" and segs:
        job = segs[2] if len(segs) > 2 and segs[1] == "j" else ""
        return AtsLink(WORKABLE, segs[0], job, False, raw)
    if host == "ats.rippling.com" and segs:
        job = segs[2] if len(segs) > 2 and segs[1] == "jobs" else ""
        return AtsLink(RIPPLING, segs[0], job, False, raw)
    m = _WD_HOST.match(host)
    if m:
        job = segs[-1] if segs else ""
        return AtsLink(WORKDAY, m.group(1), job, False, raw)

    # A company's own careers page that embeds Greenhouse carries the posting id as ``gh_jid``.
    gh_jid = (query.get("gh_jid") or [""])[0]
    if gh_jid.isdigit():
        return AtsLink(GREENHOUSE, host, gh_jid, True, raw)
    return AtsLink("", url=raw)
