"""Regenerate documents from DB and export job data."""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.files import classify, file_refs, output_folder
from core.models import JobItem, JobStatus, QueueStore
from core.paths import default_output_root
from core.profile_apply import apply_settings_profile
from core.settings_store import AppSettings
from engine import (
    PdfExportError,
    ResumeError,
    cover_letter_stem,
    export_resume,
    load_resume,
    resume_stem,
    safe_stem,
)


@dataclass
class RegenerateResult:
    job_id: str
    ok: bool
    message: str
    files: list[str]


def regenerate_jobs(store: QueueStore, settings: AppSettings, job_ids: list[str]) -> list[RegenerateResult]:
    """Rewrite every tracked document of the given jobs (overwrites existing files)."""
    return [RegenerateResult(r.job_id, r.ok, r.message, r.restored)
            for r in sync_jobs_to_disk(store, settings, job_ids, overwrite=True)]


def _validated(job: JobItem, resume: dict[str, Any]) -> dict[str, Any]:
    resume = dict(resume)
    resume.setdefault("company_name", job.company)
    resume.setdefault("role_title", job.role_title)
    if job.origin_url:
        resume.setdefault("job_link", job.origin_url)
    return load_resume(json.dumps(resume))


def _render(folder: Path, validated: dict[str, Any]) -> list[str]:
    """Write the resume DOCX, PDF and cover letter into ``folder``; returns their paths."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = resume_stem(validated)
    cover_stem = cover_letter_stem(validated)
    result = export_resume(validated, folder / f"{stem}.docx", folder / f"{stem}.pdf", folder / f"{cover_stem}.docx")
    files = [str(result.docx_path)]
    if result.pdf_path:
        files.append(str(result.pdf_path))
    if result.cover_letter_path:
        files.append(str(result.cover_letter_path))
    return files


def _default_folder(job: JobItem, validated: dict[str, Any], output_root: Path, settings: AppSettings) -> Path:
    person = safe_stem(str(validated.get("name") or settings.profile.name or "Candidate"))
    company = safe_stem(str(validated.get("company_name") or job.company or "Company"))
    role = safe_stem(str(validated.get("role_title") or job.role_title or "Role"))
    return output_root / person / company / role


def _write_exports(job: JobItem, resume: dict[str, Any], output_root: Path, settings: AppSettings) -> list[str]:
    validated = _validated(job, resume)
    return _render(_default_folder(job, validated, output_root, settings), validated)


@dataclass
class SyncResult:
    job_id: str
    ok: bool
    message: str
    restored: list[str] = field(default_factory=list)  # paths written back to disk
    present: int = 0  # files that were already on disk and left untouched


def missing_kinds(job: JobItem) -> list[str]:
    """Tracked document kinds (resume / pdf / cover) whose file is gone from disk."""
    return [r.kind for r in file_refs(job) if r.path and not r.exists]


def sync_jobs_to_disk(
    store: QueueStore,
    settings: AppSettings,
    job_ids: list[str],
    overwrite: bool = False,
    record: bool = True,
) -> list[SyncResult]:
    """Rebuild documents from the resume JSON kept in the database.

    By default only files that are missing are written, at their original paths, so anything the user edited or
    replaced by hand is never touched. ``overwrite=True`` rewrites every tracked file.

    ``record=False`` only writes files and leaves the database alone (a viewer's copy is read-only).
    """
    apply_settings_profile(settings)
    output_root = Path(settings.resolved_output_root() or default_output_root())
    results = []
    for job_id in dict.fromkeys(job_ids):  # de-duplicate, keep order
        job = store.get(job_id)
        if job is None:
            results.append(SyncResult(job_id, False, "Job not found."))
            continue
        results.append(_sync_one(store, settings, output_root, job, overwrite, record))
    return results


def _sync_one(
    store: QueueStore, settings: AppSettings, output_root: Path, job: JobItem, overwrite: bool, record: bool = True
) -> SyncResult:
    refs = {r.kind: r for r in file_refs(job)}
    tracked = [k for k, r in refs.items() if r.path]
    if job.status != JobStatus.DONE:
        return SyncResult(job.id, False, "Only finished jobs can be synced. Generate this job first.")
    if not job.resume_json:
        return SyncResult(job.id, False, "No resume JSON is stored for this job, so the files cannot be rebuilt.")
    wanted = tracked or ["resume"] + (["pdf"] if settings.export_pdf else []) + (
        ["cover"] if settings.export_cover_letter else [])
    todo = [k for k in wanted if overwrite or not (k in refs and refs[k].exists)]
    present = len(wanted) - len(todo)
    if not todo:
        return SyncResult(job.id, True, "Already on disk.", present=present)
    try:
        validated = _validated(job, job.resume_json)
        with tempfile.TemporaryDirectory(prefix="appstudio-sync-") as tmp:
            built = classify(_render(Path(tmp), validated))
            folder = output_folder(job) or _default_folder(job, validated, output_root, settings)
            written: dict[str, str] = {}
            for kind in todo:
                src = built.get(kind)
                if not src:  # e.g. the resume has no cover letter text
                    continue
                dest = Path(refs[kind].path) if kind in refs and refs[kind].path else folder / Path(src).name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                written[kind] = str(dest)
    except (ResumeError, PdfExportError, OSError, ValueError) as exc:
        return SyncResult(job.id, False, str(exc), present=present)
    if not written:
        return SyncResult(job.id, False, "The stored resume JSON produced none of the missing documents.", present=present)

    if record:
        paths = {k: r.path for k, r in refs.items() if r.path}
        paths.update(written)
        job.generated_files = [paths[k] for k in ("resume", "pdf", "cover") if k in paths]
        job.output_path = str(Path(job.generated_files[0]).parent)
        job.log(f"Restored {len(written)} file(s) from the database.")
        store.update(job)
        store.add_event(job.id, "files_regenerated", {"files": list(written.values()), "mode": "sync"})
    return SyncResult(job.id, True, f"Restored {len(written)} file(s).", list(written.values()), present)


def export_jobs_csv(path: Path, jobs: list[JobItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id",
        "company",
        "role_title",
        "url",
        "status",
        "app_stage",
        "outcome",
        "applied_at",
        "stage",
        "source",
        "output_path",
        "created_at",
        "updated_at",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for job in jobs:
            writer.writerow(
                {
                    "id": job.id,
                    "company": job.company,
                    "role_title": job.role_title,
                    "url": job.origin_url or job.url,
                    "status": job.status.value,
                    "app_stage": job.app_stage,
                    "outcome": job.outcome,
                    "applied_at": job.applied_at,
                    "stage": job.stage,
                    "source": job.source,
                    "output_path": job.output_path,
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                    "error": job.error,
                }
            )


def verify_files_on_disk(job: JobItem) -> tuple[list[str], list[str]]:
    present: list[str] = []
    missing: list[str] = []
    for path in job.generated_files:
        p = Path(path)
        if p.is_file():
            present.append(path)
        else:
            missing.append(path)
    return present, missing
