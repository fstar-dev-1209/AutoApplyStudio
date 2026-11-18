"""What the chat is told about you when it answers an application question.

A question with no saved answer goes to the chat that wrote the resume. The chat knows the job and the resume, but not
the plain facts about you (where you live, whether you need sponsorship, your citizenship, what you already answered
on other forms). Without them it guesses ("I can't determine my citizenship"). This turns your profile, your saved
answers and the latest questions you answered into one short block of text that goes with every such question.

The block is kept short on purpose: a long message is turned into an attached file by ChatGPT, which then answers slowly
or not at all. Only facts that answer application questions are in it (not consent ticks, pronouns or demographics).
Nothing here sends anything, and secrets (passwords, tokens) are never in it.
"""

from __future__ import annotations

from core.apply_facts import DECLINE, FACTS
from core.settings_store import ProfileSettings

MAX_SAVED_ANSWERS = 12  # the most recent ones
MAX_ANSWER_CHARS = 100

# Saved answers worth telling the chat, in this order (label, key). The address, the name and the education are
# written from the profile above them.
_FACT_LINES = (
    ("Citizenship / work status", "visa_status"),
    ("Security clearance", "security_clearance"),
    ("Certifications", "certifications"),
    ("Other names or aliases", "other_names"),
    ("Earliest start", "notice_period"),
    ("Willing to relocate", "relocation"),
    ("Comfortable working remotely", "remote_ok"),
    ("Comfortable working on site / hybrid", "onsite_ok"),
    ("Willing to take a background check", "background_check"),
    ("Willing to take a drug test", "drug_test"),
    ("At least 18 years old", "over_18"),
    ("Can perform the essential functions of the job", "can_perform"),
    ("Worked for this company before", "previously_employed"),
    ("Relatives who work at this company", "has_relatives"),
    ("Bound by a non-compete or similar agreement", "non_compete"),
    ("GPA", "gpa"),
)
_SPEC = {f.key: f for f in FACTS}


def _line(label: str, value: str) -> str:
    return f"- {label}: {value}"


def full_address(facts: dict[str, str]) -> str:
    street = (facts.get("address") or "").strip()
    town = ", ".join(x for x in ((facts.get("city") or "").strip(), (facts.get("state") or "").strip()) if x)
    zip_code = (facts.get("zip") or "").strip()
    country = (facts.get("country") or "").strip()
    if not street:
        return ""  # a town alone is not an address
    return ", ".join(p for p in (street, f"{town} {zip_code}".strip(), country) if p)


def personal_facts_text(profile: ProfileSettings, facts: dict[str, str], form_answers: dict[str, str] | None = None,
                        *, today_text: str = "") -> str:
    """The facts block: who you are, your saved answers, and the latest form questions you answered ('' when empty)."""
    p = profile
    lines: list[str] = []
    if p.name.strip():
        lines.append(_line("Name", p.name.strip()))
    address = full_address(facts)
    if address:
        lines.append(_line("Home address (my only address)", address))
    elif p.location.strip():
        lines.append(_line("Lives in", p.location.strip()))
    if p.work_authorization:
        lines.append(_line("Legally authorized to work in the job's country", p.work_authorization))
    if p.needs_sponsorship:
        lines.append(_line("Will now or in the future need visa sponsorship", p.needs_sponsorship))
    degree = ", ".join(x for x in (p.education_degree.strip(), p.education_school.strip()) if x)
    if degree:
        lines.append(_line("Education (my only degree)", degree))
    for label, key in _FACT_LINES:
        value = str(facts.get(key) or "").strip()
        if value and value != DECLINE and key in _SPEC:
            lines.append(_line(label, value))
    about = str(facts.get("about_me") or "").strip()
    if about:
        lines.append(_line("Also true about me", " ".join(about.split())))
    if today_text:
        lines.append(_line("Today's date", today_text))

    saved = []
    for key, answer in (form_answers or {}).items():
        answer = " ".join(str(answer or "").split())
        if key and answer:
            saved.append(_line(f"\"{key[:90]}\"", answer[:MAX_ANSWER_CHARS]))
    if saved:
        lines.append("")
        lines.append("Answers I gave on other forms (answer the same way when asked again):")
        lines += saved[-MAX_SAVED_ANSWERS:]
    return "\n".join(lines).strip()


def facts_for(ctx) -> str:
    """The block for a form's Context (its profile, facts and saved answers)."""
    return personal_facts_text(ctx.profile, ctx.facts or {}, ctx.form_answers or {}, today_text=ctx.today.strftime("%B %d, %Y"))


__all__ = ["facts_for", "full_address", "personal_facts_text"]
