"""Per-job processing checklist derived from the job's real state (no invented progress)."""

from __future__ import annotations

from core.models import JobItem, JobStatus

STEPS = (
    "Job information extracted",
    "Company verified",
    "Duplicate check passed",
    "Resume & cover letter generated",
    "Documents exported",
)


def step_states(job: JobItem) -> list[str]:
    """done | active | pending | failed for each step."""
    st = job.status
    dup = st == JobStatus.SKIPPED and "company folder" in (job.skip_reason or "").lower()
    has_jd = bool(job.job_description.strip())
    return [
        "done" if has_jd else ("active" if st == JobStatus.EXTRACTING else (
            "failed" if st == JobStatus.NEEDS_JD else "pending")),
        "done" if job.company.strip() else "pending",
        "failed" if dup else ("done" if (job.resume_json or st in {
            JobStatus.GENERATING, JobStatus.EXPORTING, JobStatus.DONE}) else "pending"),
        "done" if job.resume_json else ("active" if st == JobStatus.GENERATING else (
            "failed" if job.failed_stage == "generation" and st == JobStatus.FAILED else "pending")),
        "done" if st == JobStatus.DONE else ("active" if st == JobStatus.EXPORTING else (
            "failed" if job.failed_stage == "export" and st == JobStatus.FAILED else "pending")),
    ]


def progress_pct(job: JobItem) -> int:
    states = step_states(job)
    return int(100 * sum(1 for s in states if s == "done") / len(states))


def current_activity(job: JobItem) -> str:
    """One line for 'what is happening now'."""
    return {
        JobStatus.QUEUED: "Waiting in the queue",
        JobStatus.EXTRACTING: "Fetching the job page…",
        JobStatus.GENERATING: "Generating documents with Claude…",
        JobStatus.EXPORTING: "Writing Word and PDF files…",
        JobStatus.NEEDS_JD: "Needs a job description",
        JobStatus.DONE: "Completed",
        JobStatus.SKIPPED: job.skip_reason or "Skipped",
        JobStatus.FAILED: job.error or "Failed",
    }[job.status]
