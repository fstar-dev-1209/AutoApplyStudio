"""What the chat is told about you when it answers an application question.

A question with no saved answer goes to the chat that wrote the resume. The chat knows the job and the resume, but not
the plain facts about you (where you live, whether you need sponsorship, your citizenship, what you already answered
on other forms). Without them it guesses ("I can't determine my citizenship"). This turns your profile, your saved
answers and the questions you already answered into one short block of text that goes with every such question.

Nothing here sends anything. The block holds only what you typed into the app; secrets (passwords, tokens) are never in it.
"""

from __future__ import annotations

from core.apply_facts import DECLINE, FACTS
from core.settings_store import ProfileSettings

MAX_SAVED_ANSWERS = 40
MAX_ANSWER_CHARS = 140
# Saved facts that would only add noise to a question answer (the form filler uses them directly).
_LEFT_OUT = {"salary_expectation"}  # the salary has its own rules and prompt (core.salary_answer)


def _line(label: str, value: str) -> str:
    return f"- {label}: {value}"


def personal_facts_text(profile: ProfileSettings, facts: dict[str, str], form_answers: dict[str, str] | None = None,
                        *, today_text: str = "") -> str:
    """The facts block: profile, saved answers, and the form questions you already answered. '' when there is nothing."""
    lines: list[str] = []
    p = profile
    if p.name.strip():
        lines.append(_line("Name", p.name.strip()))
    if p.location.strip():
        lines.append(_line("Lives in", p.location.strip()))
    country = (facts.get("country") or "").strip()
    if country:
        lines.append(_line("Country", country))
    if p.work_authorization:
        lines.append(_line("Legally authorized to work in the job's country", p.work_authorization))
    if p.needs_sponsorship:
        lines.append(_line("Will now or in the future need visa sponsorship", p.needs_sponsorship))
    if p.education_school.strip():
        lines.append(_line("Education", ", ".join(x for x in (p.education_degree.strip(), p.education_school.strip()) if x)))
    if p.linkedin_url.strip():
        lines.append(_line("LinkedIn", p.linkedin_url.strip()))
    if today_text:
        lines.append(_line("Today's date", today_text))

    specs = {f.key: f for f in FACTS}
    for key, value in facts.items():
        spec = specs.get(key)
        text = str(value or "").strip()
        if spec is None or not text or key in _LEFT_OUT or key == "country" or key.startswith("eeo_"):
            continue  # (demographic questions never go to the chat, so those answers are not sent to it)
        if text == DECLINE:
            text = "prefers not to say"
        lines.append(_line(spec.label, text))

    saved = []
    for key, answer in (form_answers or {}).items():
        answer = " ".join(str(answer or "").split())
        if key and answer:
            saved.append(_line(f"Question \"{key}\"", answer[:MAX_ANSWER_CHARS]))
    if saved:
        lines.append("")
        lines.append("Questions I already answered on other forms (answer the same way when asked again):")
        lines += saved[:MAX_SAVED_ANSWERS]
    return "\n".join(lines).strip()


def facts_for(ctx) -> str:
    """The block for a form's Context (its profile, facts and saved answers)."""
    return personal_facts_text(ctx.profile, ctx.facts or {}, ctx.form_answers or {}, today_text=ctx.today.strftime("%B %d, %Y"))


__all__ = ["facts_for", "personal_facts_text"]
