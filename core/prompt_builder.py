"""Build generation prompts shared by Claude API and Claude web chat."""

from __future__ import annotations

from typing import Any

from core.settings_store import AppSettings


def build_user_payload(
    settings: AppSettings,
    *,
    job_description: str,
    job_url: str = "",
    company: str = "",
    role_title: str = "",
) -> dict[str, Any]:
    """Legacy structured payload (not used for JD-only master prompts)."""
    profile = settings.profile
    return {
        "candidate_profile": {
            "name": profile.name,
            "email": profile.email,
            "phone": profile.phone,
            "location": profile.location,
            "education": [
                {
                    "school": profile.education_school,
                    "location": profile.education_location,
                    "degree": profile.education_degree,
                    "dates": profile.education_dates,
                }
            ],
        },
        "job": {
            "company_name": company,
            "role_title": role_title,
            "job_link": job_url,
            "job_description": job_description,
        },
    }


def build_claude_chat_prompt(
    settings: AppSettings,
    *,
    job_description: str,
    job_url: str = "",
    company: str = "",
    role_title: str = "",
) -> str:
    """Assemble the full message sent to Claude.

    Your master prompt defines the contract: everything before the JOB DESCRIPTION
    divider is instructions; only the raw job posting text belongs after it.
    Candidate profile from Settings is applied at export time, not injected here.
    """
    template = (settings.prompt_template or "").strip() or "Return valid resume JSON only."
    jd = job_description.strip()
    history = (settings.profile.work_history or "").strip()
    if history:  # the real jobs: the chat may reword the bullets but never add, drop or change an employer or date
        template = (f"{template}\n\nCANDIDATE WORK HISTORY (fixed facts, taken from the candidate). The \"experience\" "
                    f"list must contain exactly these employers, with these titles and dates. Do not add, remove or "
                    f"rename any employer, and do not change any date. You may reword the bullets to fit the job.\n"
                    f"{history}")
    if not jd:
        return template
    return f"{template}\n\n{jd}"


def build_claude_chat_prompt_for_job(settings: AppSettings, job) -> str:
    """Build chat prompt from a JobItem-like object."""
    return build_claude_chat_prompt(
        settings,
        job_description=job.job_description,
        job_url=job.origin_url or job.url,
        company=job.company,
        role_title=job.role_title,
    )
