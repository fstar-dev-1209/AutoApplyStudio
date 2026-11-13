"""Generated-file helpers. Files stay on disk; only paths live in SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.models import JobItem

KINDS = (("resume", "Resume (DOCX)"), ("pdf", "Resume (PDF)"), ("cover", "Cover letter (DOCX)"))


def classify(paths: list[str]) -> dict[str, str]:
    """Map exporter output names (<Name>_Resume.docx / .pdf / _Cover_Letter.docx) to roles."""
    out: dict[str, str] = {}
    for raw in paths:
        p = Path(raw)
        suffix = p.suffix.lower()
        if suffix == ".pdf":
            out.setdefault("pdf", raw)
        elif suffix == ".docx":
            out.setdefault("cover" if "cover" in p.stem.lower() else "resume", raw)
    return out


@dataclass
class FileRef:
    kind: str
    label: str
    path: str
    exists: bool


def file_refs(job: JobItem) -> list[FileRef]:
    mapping = classify(job.generated_files)
    return [FileRef(k, label, mapping.get(k, ""), bool(mapping.get(k)) and Path(mapping[k]).is_file())
            for k, label in KINDS]


def output_folder(job: JobItem) -> Path | None:
    if job.output_path:
        return Path(job.output_path)
    if job.generated_files:
        return Path(job.generated_files[0]).parent
    return None
