import os
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from core.application_db import ApplicationDatabase  # noqa: E402
from core.applications import ApplicationService  # noqa: E402
from core.models import JobItem, JobStatus  # noqa: E402
from core.timeutil import to_iso  # noqa: E402

UTC = timezone.utc


def at(y, m, d, h=12, mi=0, tz=UTC) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=tz)


@pytest.fixture
def db(tmp_path):
    database = ApplicationDatabase(tmp_path / "studio.db")
    yield database
    database.close()


@pytest.fixture
def svc(db):
    return ApplicationService(db)


def seed_job(
    db,
    company="Acme",
    role="Backend Engineer",
    *,
    created=at(2026, 1, 5, 9),
    generated=at(2026, 1, 5, 10),
    status=JobStatus.DONE,
    failed=None,
):
    """Create a job the way the queue engine does (import event + result event)."""
    job = JobItem.create(f"https://jobs.example.com/{company}/{role}".replace(" ", "-"))
    job.company, job.role_title, job.status = company, role, status
    job.created_at = to_iso(created)
    job.updated_at = to_iso(generated or created)
    if status == JobStatus.DONE:
        job.generated_files = [f"C:/nope/{company}/{role}.docx"]
    with db.transaction():
        db.upsert(job)
        db.add_event(job.id, "imported", {}, occurred_at=to_iso(created))
        if status == JobStatus.DONE and generated:
            db.add_event(job.id, "docs_generated", {}, occurred_at=to_iso(generated))
        if failed:
            db.add_event(job.id, "generation_failed", {}, occurred_at=to_iso(failed))
        db.refresh_summary(job.id)
    return job.id
