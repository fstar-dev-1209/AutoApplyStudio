"""Normalize and validate generated resume JSON."""

from __future__ import annotations

import json
from typing import Any

from core.json_extract import find_placeholder
from core.settings_store import AppSettings
from engine import ResumeError, load_resume


def has_experience(resume_obj: dict[str, Any] | None) -> bool:
    """True when the resume lists at least one job. A resume without one must never be sent to an employer."""
    experience = (resume_obj or {}).get("experience")
    return isinstance(experience, list) and any(isinstance(e, dict) and any(e.values()) for e in experience)


def _squash(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def employers_outside_history(resume_obj: dict[str, Any] | None, work_history: str) -> list[str]:
    """Employers the resume lists that the candidate's own work history does not name ([] when no history is set)."""
    known = _squash(work_history)
    if not known:
        return []
    out: list[str] = []
    for entry in (resume_obj or {}).get("experience") or []:
        name = str(entry.get("company") or "").strip() if isinstance(entry, dict) else ""
        if name and _squash(name) not in known:
            out.append(name)
    return out


def finalize_resume_object(
    resume_obj: dict[str, Any],
    settings: AppSettings,
    *,
    company: str = "",
    role_title: str = "",
    job_url: str = "",
) -> dict[str, Any]:
    profile = settings.profile
    placeholder = find_placeholder(resume_obj)
    if placeholder:
        raise ResumeError(
            f"The reply still contains a template placeholder, \"{placeholder}\", so it is not a finished resume. "
            "It was not used; retry, or tell the chat to fill in every field from your background.")
    if not has_experience(resume_obj):
        raise ResumeError(
            "The reply has no work experience, so it is not a finished resume (the chat's answer was cut short or "
            "left it out). It was not used; it will be generated again.")
    invented = employers_outside_history(resume_obj, profile.work_history)
    if invented:
        raise ResumeError(
            f"The reply lists an employer that is not in your work history ({', '.join(invented)}), so it is not a "
            "truthful resume. It was not used; it will be generated again.")
    if company and not str(resume_obj.get("company_name") or "").strip():
        resume_obj["company_name"] = company
    if role_title and not str(resume_obj.get("role_title") or "").strip():
        resume_obj["role_title"] = role_title
    if job_url and not str(resume_obj.get("job_link") or "").strip():
        resume_obj["job_link"] = job_url
    for key_name, value in (
        ("name", profile.name),
        ("email", profile.email),
        ("phone", profile.phone),
        ("location", profile.location),
    ):
        if not str(resume_obj.get(key_name) or "").strip():
            resume_obj[key_name] = value
    try:
        return load_resume(json.dumps(resume_obj))
    except ResumeError as exc:
        raise ResumeError(f"Generated JSON failed validation: {exc}") from exc
