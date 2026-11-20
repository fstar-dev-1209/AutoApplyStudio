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

import re

from core.apply_facts import DECLINE, FACTS
from core.settings_store import ProfileSettings

MAX_SAVED_ANSWERS = 15  # the most recent ones

# A saved answer is reused only when the question is about you as a person: where you live and can work, when you can
# start, travel, hours, checks, your status. A question about experience or skills ("5+ years of SQL?", "your level
# with React") is answered for each job from that job's resume, never copied from another form.
_PERSONAL = re.compile(
    r"relocat|\bstart\b|availab|notice period|travel|address|\bcity\b|\bstate\b|country|\breside|\blived?\b|\blocated\b|"
    r"located in|authori[sz]|eligib|sponsor|visa|citizen|green card|permanent resident|clearance|background check|"
    r"drug (test|screen)|\b18\b|years of age|relative|previously (been )?employed|worked (for|at) .{0,40} before|"
    r"former employee|non-?compete|restrictive covenant|hours|shift|weekend|on-?site|hybrid|remote|in[- ]office|"
    r"commut|contract(or)?\b|w-?2|1099|full[- ]time|part[- ]time|time ?zone|\best\b|\bpst\b|"
    r"office|work location|where would you|hear about|referr|linkedin|pronoun|name|phone|e-?mail|accommodation|references|consider(ed)? for other",
    re.I)
_EXPERIENCE = re.compile(
    r"experience|years? of|skill|proficien|familiar|expert|level (with|in)|rate yourself|scale (of|from)|hands-?on|"
    r"built|used|worked with|knowledge|certif|degree|portfolio|github|tell us|describe|explain|why\b",
    re.I)


def is_personal_question(question: str) -> bool:
    """True for a question about you as a person (see _PERSONAL); False for one about experience or skills."""
    text = question or ""
    return bool(_PERSONAL.search(text)) and not _EXPERIENCE.search(text)
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
    ("Need accommodations for interviews", "interview_accommodations"),
    ("Worked for this company before", "previously_employed"),
    ("Applied to or interviewed with this company before", "previously_applied"),
    ("Preferred work locations", "preferred_locations"),
    ("Employment types I accept", "employment_type"),
    ("Willing to complete assessments", "assessments_ok"),
    ("Languages", "languages"),
    ("How I found this job", "referral_source"),
    ("Current employment status", "employment_status"),
    ("Consider me for other positions", "other_positions"),
    ("Can work shifts, weekends or on-call", "shifts_ok"),
    ("Valid driver's license", "drivers_license"),
    ("Reliable home office and internet", "home_office"),
    ("Ever convicted of a crime", "criminal_record"),
    ("Ever terminated or asked to resign", "terminated"),
    ("Current or former government employee", "government_employee"),
    ("Willing to sign an NDA / IP agreement", "nda_ok"),
    ("Can pass a credit check", "credit_check"),
    ("Hold a current passport", "passport"),
    ("Hours per week I can work", "hours_per_week"),
    ("Would consider a lower level or title", "lower_level_ok"),
    ("Other offers or interviews", "other_offers"),
    ("Reliable transportation", "transportation"),
    ("Can work any US time zone's business hours", "zone_hours_ok"),
    ("Member of a professional association", "professional_association"),
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
        if key and answer and is_personal_question(key):
            saved.append(_line(f"\"{key[:90]}\"", answer[:MAX_ANSWER_CHARS]))
    if saved:
        lines.append("")
        lines.append("Answers I gave on other forms (answer the same way when asked again):")
        lines += saved[-MAX_SAVED_ANSWERS:]
    return "\n".join(lines).strip()


def facts_for(ctx) -> str:
    """The block for a form's Context (its profile, facts and saved answers)."""
    return personal_facts_text(ctx.profile, ctx.facts or {}, ctx.form_answers or {}, today_text=ctx.today.strftime("%B %d, %Y"))


__all__ = ["facts_for", "full_address", "is_personal_question", "personal_facts_text"]
