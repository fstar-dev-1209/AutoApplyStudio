"""What the browser extension needs to fill an employer's application form.

Pure lookups over the queue and the saved profile: nothing here writes, submits or marks anything applied.
"""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.company_match import normalize_company
from core.files import KINDS, classify
from core.models import JobItem, JobStatus, QueueStore
from core.settings_store import ProfileSettings

MAX_FILE_BYTES = 8_000_000  # a resume or cover letter is far smaller; this only stops a wrong path being served
CANDIDATE_LIMIT = 25
_KIND_NAMES = {kind for kind, _ in KINDS}
_PARTICLES = {"van", "von", "de", "del", "della", "der", "di", "da", "la", "le", "bin", "al"}


class KitError(Exception):
    """A request the app cannot serve; `status` is the HTTP code the extension receives."""

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.extra = extra


@dataclass(frozen=True)
class KitFile:
    kind: str
    name: str
    size: int
    mime: str


def split_name(full: str) -> tuple[str, str]:
    """"Mary Ann van der Berg" -> ("Mary Ann", "van der Berg"); one word -> (word, "")."""
    parts = full.split()
    if len(parts) <= 1:
        return (parts[0] if parts else ""), ""
    cut = len(parts) - 1
    while cut > 1 and parts[cut - 1].casefold() in _PARTICLES:
        cut -= 1
    return " ".join(parts[:cut]), " ".join(parts[cut:])


def profile_payload(profile: ProfileSettings) -> dict[str, str]:
    first, last = split_name(profile.name.strip())
    return {
        "full_name": profile.name.strip(),
        "first_name": first,
        "last_name": last,
        "email": profile.email.strip(),
        "phone": profile.phone.strip(),
        "location": profile.location.strip(),
        "linkedin_url": profile.linkedin_url.strip(),
        "github_url": profile.github_url.strip(),
        "website_url": profile.website_url.strip(),
        "work_authorization": profile.work_authorization,
        "needs_sponsorship": profile.needs_sponsorship,
    }


def _same_url(a: str, b: str) -> bool:
    return bool(a) and bool(b) and a.strip().rstrip("/").casefold() == b.strip().rstrip("/").casefold()


def has_files(job: JobItem) -> bool:
    return any(Path(p).is_file() for p in classify(job.generated_files).values())


def find_job(store: QueueStore, url: str = "", company: str = "", job_id: str = "") -> JobItem | None:
    """The job whose documents should fill this form: chosen id, else the same link, else the same company.

    Only jobs with generated files on disk that have not been applied to are offered; filling a form for
    a job you already applied to is almost certainly a mistake.
    """
    jobs = [j for j in store.jobs() if j.status == JobStatus.DONE and not j.applied_at and has_files(j)]
    if job_id:
        return next((j for j in jobs if j.id == job_id), None)
    for job in jobs:
        if _same_url(job.url, url) or _same_url(job.origin_url, url):
            return job
    key = normalize_company(company)
    if key:
        same = [j for j in jobs if normalize_company(j.company) == key]
        if len(same) == 1:
            return same[0]
    return None


def candidates(store: QueueStore) -> list[dict[str, str]]:
    """Jobs ready to apply to, newest first, for the extension's "which job is this?" picker."""
    ready = [j for j in store.jobs() if j.status == JobStatus.DONE and not j.applied_at and has_files(j)]
    ready.sort(key=lambda j: j.completed_at or j.updated_at, reverse=True)
    return [{"id": j.id, "company": j.company, "role": j.role_title} for j in ready[:CANDIDATE_LIMIT]]


def list_files(job: JobItem) -> list[KitFile]:
    out = []
    for kind, path in classify(job.generated_files).items():
        p = Path(path)
        if not p.is_file():
            continue
        out.append(KitFile(kind, p.name, p.stat().st_size, mimetypes.guess_type(p.name)[0] or "application/octet-stream"))
    order = {kind: i for i, (kind, _) in enumerate(KINDS)}
    return sorted(out, key=lambda f: order[f.kind])


def kit(job: JobItem, profile: ProfileSettings) -> dict[str, Any]:
    return {
        "job": {"id": job.id, "company": job.company, "role": job.role_title, "url": job.url},
        "profile": profile_payload(profile),
        "files": [f.__dict__ for f in list_files(job)],
    }


def read_file(job: JobItem, kind: str) -> dict[str, Any]:
    """One of the job's own generated files as base64. The path comes from the job record, never the client."""
    if kind not in _KIND_NAMES:
        raise KitError(400, "Unknown file kind.")
    path = classify(job.generated_files).get(kind)
    if not path or not Path(path).is_file():
        raise KitError(404, "That file was not generated for this job.")
    p = Path(path)
    if p.stat().st_size > MAX_FILE_BYTES:
        raise KitError(413, "That file is too large to attach.")
    return {
        "kind": kind,
        "name": p.name,
        "mime": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
        "base64": base64.b64encode(p.read_bytes()).decode("ascii"),
    }
