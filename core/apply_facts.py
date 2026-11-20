"""The facts the bot may state on an application, as you gave them. Nothing here is ever written by an AI."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FactSpec:
    key: str
    label: str
    hint: str = ""
    choices: tuple[str, ...] = ()  # when set, the value must be one of these
    group: str = "About you"


YES_NO = ("Yes", "No")
DECLINE = "Decline"  # the value that means "choose the 'prefer not to say' option"

# Wordings Greenhouse application forms actually offer. One saved choice is matched onto a board's own sentence.
GENDER = (
    "Male", "Female", "Non-binary", "Agender", "Genderqueer", "Transgender", "Two-Spirit",
    "Decline to self-identify", "I don't wish to answer",
)
RACE = (
    "American Indian or Alaska Native", "American Indian or Alaskan Native", "Asian",
    "Black or African American", "Hispanic or Latino", "Native Hawaiian or Other Pacific Islander",
    "Two or More Races", "White", "Decline to self-identify", "I don't wish to answer",
)
HISPANIC = ("Yes", "No", "Decline to self-identify", "I don't wish to answer")
VETERAN = (
    "I am not a protected veteran",
    "I do not fall into one of the above categories of \"protected veteran\"",
    "I identify as one or more of the classifications of a protected veteran",
    "I identify as one or more of the above categories of \"protected veteran\"",
    "I previously served in the military, but I am not a protected veteran",
    "I don't wish to answer", "Decline to self-identify",
)
DISABILITY = (
    "No, I do not have a disability, or have a history/record of having a disability",
    "No, I don't have a disability, or a history/record of having a disability",
    "Yes, I have a disability, or have had one in the past",
    "Yes, I have a disability, or have a history/record of having a disability",
    "I do not want to answer", "I don't wish to answer",
)
ORIENTATION = (
    "Heterosexual / Straight", "Asexual", "Bisexual", "Gay/Lesbian", "Pansexual", "Queer",
    "Questioning", "Sexual Orientation Not Listed", "I don't wish to answer", "Decline to self-identify",
)
PRONOUNS = (
    "He/Him", "He/They", "She/Her", "She/They", "They/Them", "Xe/Xem", "Ze/Zir",
    "Pronouns Not Listed", "I don't wish to answer",
)

# Free-text people already saved, mapped onto the list above so a typo is not thrown away.
_ALIASES = {
    "eeo_gender": {"male": "Male", "female": "Female", "non-binary": "Non-binary", "nonbinary": "Non-binary",
                   "decline": "Decline to self-identify"},
    "eeo_race": {"assian": "Asian", "asian": "Asian", "white": "White", "caucasian": "White",
                 "black": "Black or African American", "african american": "Black or African American",
                 "decline": "Decline to self-identify"},
    "eeo_hispanic": {"no": "No", "yes": "Yes", "decline": "Decline to self-identify"},
    "eeo_veteran": {
        "no": "I am not a protected veteran",
        "yes": "I identify as one or more of the classifications of a protected veteran",
        "decline": "I don't wish to answer"},
    "eeo_disability": {
        "no": "No, I do not have a disability, or have a history/record of having a disability",
        "yes": "Yes, I have a disability, or have had one in the past",
        "decline": "I do not want to answer"},
    "eeo_orientation": {"straight": "Heterosexual / Straight", "heterosexual": "Heterosexual / Straight",
                        "decline": "I don't wish to answer"},
    "pronouns": {"he/him": "He/Him", "he/him/his": "He/Him", "she/her": "She/Her", "they/them": "They/Them",
                 "he/they": "He/They", "she/they": "She/They"},
}

FACTS: tuple[FactSpec, ...] = (
    FactSpec("located_in_us", "Live in the United States",
             "Leave unset to follow Country. Pick Yes or No to override.", choices=YES_NO,
             group="Questions Greenhouse asks"),
    FactSpec("can_perform", "Can perform the essential functions of the job",
             "With or without a reasonable accommodation.", choices=YES_NO, group="Questions Greenhouse asks"),
    FactSpec("work_setup", "Can work the workplace type in the posting",
             "Yes selects \"Yes, I can work this way now\" on workplace-type questions.",
             choices=YES_NO, group="Questions Greenhouse asks"),
    FactSpec("non_compete", "A past agreement limits where you can work",
             "Non-compete or a limit on companies, industries, or locations. Leave unset until you choose.",
             choices=YES_NO, group="Questions Greenhouse asks"),

    FactSpec("address", "Street address", group="Contact"),
    FactSpec("city", "City", group="Contact"),
    FactSpec("state", "State / region", group="Contact"),
    FactSpec("zip", "ZIP / postal code", group="Contact"),
    FactSpec("country", "Country", "e.g. United States", group="Contact"),
    FactSpec("pronouns", "Pronouns", "Only if you want them stated", choices=PRONOUNS, group="Contact"),

    FactSpec("visa_status", "Work status (text)", "e.g. US citizen, Green card, H-1B, OPT", group="Work authorization"),
    FactSpec("over_18", "At least 18 years old", choices=YES_NO, group="Work authorization"),
    FactSpec("background_check", "Willing to take a background check", choices=YES_NO, group="Work authorization"),
    FactSpec("drug_test", "Willing to take a drug test", choices=YES_NO, group="Work authorization"),
    FactSpec("security_clearance", "Security clearance", "e.g. None, Secret, Top Secret", group="Work authorization"),
    FactSpec("gpa", "GPA", "e.g. 3.6. Blank: an optional GPA question is left blank, a required one stops for you",
             group="Background"),
    FactSpec("certifications", "Certifications", "e.g. AWS Solutions Architect - Associate (2023), or None",
             group="Background"),
    FactSpec("interview_accommodations", "Need accommodations for interviews", choices=YES_NO, group="Background"),
    FactSpec("previously_applied", "Applied to or interviewed with the company before", choices=YES_NO, group="Background"),
    FactSpec("preferred_locations", "Preferred work locations", "e.g. Remote; anywhere in the US if required",
             group="Pay and availability"),
    FactSpec("employment_type", "Employment types you accept", "e.g. Full-time or contract", group="Pay and availability"),
    FactSpec("assessments_ok", "Willing to complete assessments", choices=YES_NO, group="Background"),
    FactSpec("languages", "Languages", "e.g. English (native)", group="Background"),
    FactSpec("employment_status", "Current employment status", "e.g. Not employed, available now", group="Background"),
    FactSpec("other_positions", "Consider me for other positions", choices=YES_NO, group="Background"),
    FactSpec("shifts_ok", "Can work shifts, weekends or on-call", choices=YES_NO, group="Pay and availability"),
    FactSpec("drivers_license", "Valid driver's license", choices=YES_NO, group="Background"),
    FactSpec("home_office", "Reliable home office and internet", choices=YES_NO, group="Pay and availability"),
    FactSpec("criminal_record", "Ever convicted of a crime", choices=YES_NO, group="Background"),
    FactSpec("terminated", "Ever terminated or asked to resign", choices=YES_NO, group="Background"),
    FactSpec("government_employee", "Current or former government employee", choices=YES_NO, group="Background"),
    FactSpec("nda_ok", "Willing to sign an NDA / IP agreement", choices=YES_NO, group="Background"),
    FactSpec("credit_check", "Can pass a credit check", choices=YES_NO, group="Work authorization"),
    FactSpec("middle_name", "Middle name", group="Contact"),
    FactSpec("passport", "Hold a current passport", choices=YES_NO, group="Work authorization"),
    FactSpec("hours_per_week", "Hours per week you can work", "e.g. 40+", group="Pay and availability"),
    FactSpec("lower_level_ok", "Would consider a lower level or title", choices=YES_NO, group="Pay and availability"),
    FactSpec("other_offers", "Other offers or interviews", "e.g. No, free now", group="Pay and availability"),
    FactSpec("transportation", "Reliable transportation", choices=YES_NO, group="Pay and availability"),
    FactSpec("zone_hours_ok", "Can work any US time zone's business hours", choices=YES_NO, group="Pay and availability"),
    FactSpec("professional_association", "Member of a professional association", "e.g. No", group="Background"),
    FactSpec("social_profiles", "Other public profiles (Twitter/X ...)", "e.g. N/A", group="Contact"),
    FactSpec("date_of_birth", "Date of birth (MM/DD/YYYY)", "Used only to answer age questions; never sent to the chat",
             group="Self-identification (voluntary)"),
    FactSpec("other_names", "Other names or aliases", "None if you have never used another name", group="Background"),
    FactSpec("about_me", "Anything else the chat should know",
             "Plain facts it can use when answering, e.g. open to travel, any shift, any start date", group="Background"),

    FactSpec("salary_expectation", "Expected salary", "e.g. $150,000 per year", group="Pay and availability"),
    FactSpec("notice_period", "Notice period / earliest start", "e.g. 2 weeks, Immediately", group="Pay and availability"),
    FactSpec("relocation", "Willing to relocate", choices=YES_NO, group="Pay and availability"),
    FactSpec("remote_ok", "Comfortable working remotely", choices=YES_NO, group="Pay and availability"),
    FactSpec("onsite_ok", "Comfortable working on site / hybrid", choices=YES_NO, group="Pay and availability"),

    # Not here on purpose: current employer, job title, years of experience, school and degree. They are read from
    # the resume written for each job (and the chat answers questions about it), so they are never typed in twice.
    FactSpec("referral_source", "How you heard about jobs", "Used for \"How did you hear about us\"", group="Background"),
    FactSpec("previously_employed", "Worked for the company before", "Yes only if it applies to many; usually No",
             choices=YES_NO, group="Background"),
    FactSpec("has_relatives", "Relatives who work at the company", choices=YES_NO, group="Background"),

    FactSpec("agree_consents", "Tick required acknowledgement boxes",
             "Yes ticks privacy, demographic, and \"I certify this is true\" boxes. No leaves a required box unticked and stops that application.",
             choices=YES_NO, group="Consents"),
    FactSpec("sms_consent", "Agree to text messages", choices=YES_NO, group="Consents"),

    FactSpec("eeo_gender", "Gender", "Matched to each form's own list, including Gender Identity.",
             choices=GENDER, group="Self-identification (voluntary)"),
    FactSpec("eeo_race", "Race / ethnicity", "Matched to each form's own list.",
             choices=RACE, group="Self-identification (voluntary)"),
    FactSpec("eeo_hispanic", "Hispanic / Latino", choices=HISPANIC, group="Self-identification (voluntary)"),
    FactSpec("eeo_veteran", "Veteran status", "\"No\" is saved as \"I am not a protected veteran\" and matched to each form's wording.",
             choices=VETERAN, group="Self-identification (voluntary)"),
    FactSpec("eeo_disability", "Disability status", choices=DISABILITY, group="Self-identification (voluntary)"),
    FactSpec("eeo_orientation", "Sexual orientation", "\"Straight\" is saved as Heterosexual / Straight.",
             choices=ORIENTATION, group="Self-identification (voluntary)"),
)

FACT_KEYS = {f.key for f in FACTS}

DEFAULTS: dict[str, str] = {
    "country": "United States",
    "over_18": "Yes",
    "background_check": "Yes",
    "drug_test": "Yes",
    "referral_source": "LinkedIn",
    "previously_employed": "No",
    "has_relatives": "No",
    "agree_consents": "Yes",
    "sms_consent": "No",
    "can_perform": "Yes",
    "work_setup": "Yes",
    "eeo_gender": DECLINE, "eeo_race": DECLINE, "eeo_hispanic": DECLINE,
    "eeo_veteran": DECLINE, "eeo_disability": DECLINE, "eeo_orientation": DECLINE,
}


def clean_facts(raw: object) -> dict[str, str]:
    """Only known keys, text values; a choice fact keeps only a valid choice (anything else is dropped)."""
    if not isinstance(raw, dict):
        return {}
    specs = {f.key: f for f in FACTS}
    out: dict[str, str] = {}
    for key, value in raw.items():
        spec = specs.get(key)
        text = str(value or "").strip()
        if spec is None or not text:
            continue
        text = _ALIASES.get(key, {}).get(text.casefold(), text)
        if spec.choices:
            match = next((c for c in spec.choices if c.casefold() == text.casefold()), "")
            if not match:
                continue
            text = match
        out[key] = text
    return out


def norm_question(label: str) -> str:
    """Stable key for a question label, so the same question is recognized next time."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (label or "").lower())).strip()[:180]


def clean_form_questions(raw: object) -> list[dict]:
    """Questions a form asked, each with the options it offered and the answer you picked."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get("label") or "").split())
        key = norm_question(label)
        if not key or key in seen:
            continue
        options: list[str] = []
        for opt in item.get("options") or []:
            text = " ".join(str(opt or "").split())
            if text and text not in options and len(options) < 24:
                options.append(text[:240])
        answer = " ".join(str(item.get("answer") or "").split())[:500]
        if not options:
            if not answer:
                continue
            seen.add(key)
            out.append({"label": label[:400], "options": [], "answer": answer})
        else:
            seen.add(key)
            if answer not in options:
                answer = ""
            out.append({"label": label[:400], "options": options, "answer": answer})
        if len(out) >= 80:
            break
    return out


def merge_form_questions(existing: object, found: list[tuple[str, list[str]]]) -> list[dict]:
    """Add questions a form just asked. An answer you already picked is kept."""
    current = clean_form_questions(existing)
    by_key = {norm_question(q["label"]): q for q in current}
    for label, options in found:
        key = norm_question(str(label or ""))
        cleaned: list[str] = []
        for opt in options:
            text = " ".join(str(opt or "").split())
            if text and text not in cleaned and len(cleaned) < 24:
                cleaned.append(text[:240])
        if not key or not cleaned:
            continue
        prior = by_key.get(key)
        if prior:
            if cleaned != prior["options"]:
                prior["options"] = cleaned
                if prior["answer"] not in cleaned:
                    prior["answer"] = ""
            continue
        if len(current) >= 80:
            continue
        item = {"label": " ".join(str(label).split())[:400], "options": cleaned, "answer": ""}
        current.append(item)
        by_key[key] = item
    return current


def answered_form_questions(raw: object) -> dict[str, str]:
    """Normalized label to the option you selected, or the text you typed for that question."""
    return {norm_question(q["label"]): q["answer"] for q in clean_form_questions(raw) if q["answer"]}


def store_manual_answers(existing: object, answers: list[tuple[str, str, list[str]]]) -> list[dict]:
    """Keep an answer you typed for one question. A listed answer is kept only when it is one of the options."""
    current = clean_form_questions(existing)
    by_key = {norm_question(q["label"]): q for q in current}
    for label, answer, options in answers:
        label = " ".join(str(label or "").split())
        answer = " ".join(str(answer or "").split())[:500]
        key = norm_question(label)
        if not key or not answer:
            continue
        cleaned: list[str] = []
        for opt in options or []:
            text = " ".join(str(opt or "").split())
            if text and text not in cleaned and len(cleaned) < 24:
                cleaned.append(text[:240])
        if cleaned and answer not in cleaned:
            continue
        item = by_key.get(key)
        if item is None:
            if len(current) >= 80:
                continue
            item = {"label": label[:400], "options": cleaned, "answer": answer}
            current.append(item)
            by_key[key] = item
            continue
        if cleaned and not item["options"]:
            item["options"] = cleaned
        if item["options"] and answer not in item["options"]:
            continue
        item["answer"] = answer
    return clean_form_questions(current)


_REASON_LABELS = (
    re.compile(r"Not covered by your facts:\s*(.+)$", re.I),
    re.compile(r"Not filled in under Auto Apply > Your answers:\s*(.+)$", re.I),
    re.compile(r"No option matches '.+' for:\s*(.+)$", re.I),
    re.compile(r"The chat could not answer '([^']+)'", re.I),
    re.compile(r"Could not read the choices for:\s*(.+)$", re.I),
    re.compile(r"Asks two things at once:\s*(.+)$", re.I),
    re.compile(r"Needs an answer:\s*(.+)$", re.I),
    re.compile(r"Still empty after filling:\s*(.+)$", re.I),
)

# A degree list on a form. The saved choice is used only when it matches one of that form's own options.
_DEGREE_CHOICES = (
    "High School", "Associate's Degree", "Bachelor's Degree", "Master's Degree", "MBA", "Doctorate", "Other",
)
_CHOICE_LISTS = (
    ("transgender", ("Yes", "No", "I don't wish to answer"), "eeo_transgender"),
    ("race", RACE, "eeo_race"),
    ("ethnic", RACE, "eeo_race"),
    ("hispanic", HISPANIC, "eeo_hispanic"),
    ("latino", HISPANIC, "eeo_hispanic"),
    ("veteran", VETERAN, "eeo_veteran"),
    ("disability", DISABILITY, "eeo_disability"),
    ("orientation", ORIENTATION, "eeo_orientation"),
    ("pronoun", PRONOUNS, "pronouns"),
    ("gender", GENDER, "eeo_gender"),
    ("degree", _DEGREE_CHOICES, ""),
)
_NOT_A_QUESTION = re.compile(r"resume|cover letter|\bcv\b", re.I)


def _plan_entries(raw: object) -> list:
    if isinstance(raw, list):
        return raw
    if not raw or not isinstance(raw, str):
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _label_from_reason(reason: str) -> str:
    for pattern in _REASON_LABELS:
        found = pattern.search(reason.strip())
        if found:
            return " ".join(found.group(1).split())[:400]
    return ""


def _option_list(raw: object) -> list[str]:
    out: list[str] = []
    for opt in raw or []:
        text = " ".join(str(opt or "").split())
        if text and text not in out and len(out) < 24:
            out.append(text[:240])
    return out


def _known_choices(label: str) -> tuple[list[str], str]:
    """The standard choices for a question we already know, and the saved-fact key they belong to."""
    text = (label or "").casefold()
    for word, choices, fact_key in _CHOICE_LISTS:
        if re.search(rf"\b{re.escape(word)}\b", text):
            return list(choices), fact_key
    return [], ""


def _question_row(label: str, reason: str, options: object, saved: dict, facts: dict, *, locked: bool = False) -> dict:
    known = saved.get(norm_question(label), {})
    standard, fact_key = _known_choices(label)
    opts = _option_list(options) or _option_list(known.get("options")) or standard
    answer = known.get("answer") or ""
    if not answer and fact_key:
        stated = " ".join(str((facts or {}).get(fact_key) or "").split())
        if stated in opts:
            answer = stated
    return {"label": label, "reason": reason, "options": opts, "answer": answer, "locked": locked}


def questions_from_attempt(row: dict, form_questions: object, facts: dict | None = None) -> list[dict]:
    """Questions a person can answer for this attempt. A file or browser failure is not one of them.

    A known select question (race, gender, degree, and the other saved lists) includes its choices even when the
    attempt did not record the options the form showed."""
    saved = {norm_question(str(q.get("label") or "")): q for q in (form_questions or []) if isinstance(q, dict)}
    facts = facts or {}
    plan = [e for e in _plan_entries(row.get("plan_json")) if isinstance(e, dict)]
    by_label = {norm_question(str(e.get("label") or "")): e for e in plan if e.get("label")}
    found: list[dict] = []
    seen: set[str] = set()

    def add(label: str, reason: str, options: object, *, locked: bool = False) -> None:
        label = " ".join(str(label or "").split())[:400]
        key = norm_question(label)
        if not key or key in seen or _NOT_A_QUESTION.search(label):
            return
        seen.add(key)
        recorded = by_label.get(key, {}).get("options") or []
        found.append(_question_row(label, reason, options or recorded, saved, facts, locked=locked))

    for entry in plan:
        if entry.get("action") != "needs":
            continue
        reason = " ".join(str(entry.get("reason") or "").split())
        add(str(entry.get("label") or ""), reason, entry.get("options") or [],
            locked="automated" in reason.lower())
    reason = " ".join(str(row.get("reason") or "").split())
    still = re.match(r"Still empty after filling:\s*(.*)$", reason, re.I)
    pieces = [p.strip() for p in (still.group(1) if still else reason).split(";") if p.strip()]
    for piece in pieces:
        if "automated" in piece.lower():
            add(_label_from_reason(piece) or "Automated submission check", piece, [], locked=True)
            continue
        label = piece if still else _label_from_reason(piece)
        if label:
            add(label, "" if still else piece, [])
    return found
