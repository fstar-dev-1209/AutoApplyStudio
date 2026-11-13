"""Deciding what to put in each field of an application form.

Every field gets exactly one of: ``fill`` (a value we are entitled to state), ``chat`` (an open question for the
job's chat conversation), ``skip`` (leave it as it is) or ``needs`` (a required field we cannot honestly answer: the
application is not sent). Facts come only from your profile and the facts you entered; legal and personal questions are
never handed to an AI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from core.apply_facts import DECLINE, norm_question
from core.apply_kit import split_name
from core.chat_questions import match_option
from core.settings_store import ProfileSettings

FILL, CHAT, SKIP, NEEDS = "fill", "chat", "skip", "needs"
RESUME_FILE, COVER_FILE = "@resume", "@cover"  # values meaning "attach the generated file"


@dataclass
class Option:
    label: str
    ref: str = ""  # how the filler finds this option (radio / checkbox inputs)
    value: str = ""


@dataclass
class Field:
    ref: str
    kind: str  # text | textarea | number | select | combobox | radio | checkbox | file
    label: str
    required: bool = False
    value: str = ""  # what the field already holds
    options: list[Option] = field(default_factory=list)
    max_chars: int = 0
    slot: str = ""  # file fields: resume | cover | other
    input_type: str = ""
    accept: str = ""

    @property
    def is_choice(self) -> bool:
        return self.kind in {"select", "combobox", "radio"}


@dataclass
class Decision:
    action: str
    value: str = ""
    source: str = ""  # profile | facts | rule | chat | page
    reason: str = ""


@dataclass
class Context:
    profile: ProfileSettings
    facts: dict[str, str] = field(default_factory=dict)
    chat_enabled: bool = False
    cover_letter_text: str = ""
    has_resume: bool = True
    has_cover: bool = False
    resume: dict = field(default_factory=dict)  # this job's resume JSON: the source of employer, title and experience
    today: date = field(default_factory=date.today)
    form_answers: dict[str, str] = field(default_factory=dict)  # a question's own options, once you have picked one


# Questions that are never answered by an AI, whatever else we know.
SENSITIVE = re.compile(
    r"convict|felon|crimin|arrest|lawsuit|litigation|immigration|citizenship|national origin|religio|"
    r"salary history|current (salary|compensation|pay)|previous (salary|compensation)|"
    r"disabilit|veteran|\brace\b|ethnic|gender|sexual|transgender|marital|pregnan|\bage\b|date of birth|"
    r"social security|\bssn\b|passport|driver'?s? licen",
    re.I)

# (fact key, label regex). First match wins, so specific rules come before general ones.
RULES: list[tuple[str, re.Pattern[str]]] = [(k, re.compile(p, re.I)) for k, p in [
    ("first_name", r"^(legal |preferred )?first name|^given name|^first$"),
    ("last_name", r"^(legal )?last name|^family name|^surname|^last$"),
    ("full_name", r"^(full |legal |your |candidate )?name$|first and last name|\b(full|legal) (legal )?name\b"),
    ("email", r"e-?mail"),
    ("phone", r"phone|mobile|telephone|\bcell\b"),
    ("linkedin_url", r"linkedin"),
    ("github_url", r"github"),
    ("website_url", r"website|portfolio|personal (site|url|page)|\bblog\b"),
    ("address", r"street address|address line|^address\b|mailing address"),
    ("zip", r"zip|postal"),
    ("state", r"^(current |home |what )?state\b|what state|state are you|province|region"),
    ("country", r"^(current |home )?country\b|country of residence|country where you (currently )?(live|reside)|"
                 r"countries you anticipate working"),
    ("city", r"^city\b"),
    ("located_in_us", r"currently located in the (united states|u\.?s\.?a?\.?)|"
                      r"do you (currently )?(live|reside) in the (united states|u\.?s\.?)"),
    ("non_compete", r"non-?compete|non-?solicitation|employment agreements? or restrictions|"
                    r"limits the companies, industries, or locations|agreement with a current or past employer"),
    ("location", r"location|where are you (based|located)|current city|city.*state"),
    ("authorized_and_no_sponsorship", r"authori[sz]ed.*without.*sponsor|without.*sponsor.*authori[sz]ed"),
    # before work authorization: "will you require the company to file a petition / immigration paperwork" is sponsorship,
    # even when the sentence also contains the words "work authorization"
    ("needs_sponsorship", r"sponsor|petition|employment(?:\s*-\s*|\s+|-)based status|immigration or work authorization|"
                          r"work authorization process|require.*visa|visa.*(sponsor|require)"),
    ("work_authorization", r"authori[sz]ed to work|authori[sz]ation to work|evidence of (your )?(identity|eligibility)|proof of (your )?(identity|eligibility|work authori[sz]ation)|eligible to work|legally (permitted|allowed|entitled)|right to work|work authori[sz]ation"),
    ("can_perform", r"essential functions"),
    ("visa_status", r"visa status|work status|immigration status|employment eligibility"),
    ("security_clearance", r"clearance"),
    ("background_check", r"background (check|screen|investigation)"),
    ("drug_test", r"drug (test|screen)"),
    ("over_18", r"(at least|over|older than|above)( the age of)? (18|eighteen)|age of (18|eighteen)|(18|eighteen)( years)? (of age )?or (older|over)|legal (working )?age|18 years"),
    # what you earn now is never stated (no fact exists for it), so it must not fall into the expectation rule below
    ("current_salary", r"current (salary|compensation|pay|rate)|salary history|(previous|last|prior) (salary|compensation|pay)"),
    ("salary_expectation", r"salary|compensation|pay (expectation|range)|desired (pay|rate)|expected (pay|rate)|hourly rate"),
    ("notice_period", r"notice period|when (can|could|would) you (start|begin|join)|earliest.*start|available to start|start date|availability to start"),
    ("relocation", r"relocat"),
    ("work_setup", r"workplace type|work this way"),
    ("remote_ok", r"work(ing)? remote|remote work|comfortable.*remote"),
    ("onsite_ok", r"comfortable.{0,40}(on-?site|hybrid|in[- ]office)|will you (work )?(on-?site|in[- ]office|hybrid)|commut"),
    ("years_experience", r"years of (relevant |professional |total |work |industry )?experience|how many years"),
    ("current_company", r"\b(?:current|most recent|previous)\b(?: or \b(?:current|previous|former)\b)? \b(?:employer|company)\b"),
    ("current_title", r"\b(?:current|most recent|previous)\b(?: or \b(?:current|previous|former)\b)? \b(?:job )?(?:title|role|position)\b"),
    ("school", r"^(school|university|college)\b|^(name of )?(school|university)|institution"),
    ("degree", r"^degree\b|highest (level of )?(education|degree)|level of education"),
    ("previously_employed", r"(previously|ever|formerly) (been )?(employed|worked)|former employee|"
                            r"worked (for|at|with) .* (before|past|previously)|worked for .{0,80} previously|"
                            r"applied (to|with|at|for) .{0,80}(past|before|previously)"),
    ("has_relatives", r"\brelatives?\b(?!\s+to\b)|family member|\brelated to\b"),
    ("referral_source", r"how did you (hear|find|learn)|where did you (hear|find)|referral source|source of application"),
    ("pronouns", r"pronoun"),
    ("sms_consent", r"\bsms\b|text message|whatsapp|receive (texts|messages)|text (you|me|us)\b"),
    ("eeo_hispanic", r"hispanic|latino|latinx"),
    ("eeo_transgender", r"transgender"),
    ("time_zone", r"\b(est|edt|cst|cdt|mst|mdt|pst|pdt|akst|hst)\b|"
                  r"\b(eastern|central|mountain|pacific)\b.{0,20}\b(time|zone|based)\b|time ?zone"),
    ("eeo_gender", r"\bgender\b|\bsex\b"),
    ("eeo_race", r"\brace\b|ethnic"),
    ("eeo_veteran", r"veteran|military"),
    ("eeo_disability", r"disabilit"),
    ("eeo_orientation", r"sexual orientation|lgbt"),
    ("agree_consents", r"acknowledge|i agree|agree to|consent|privacy|terms|certify|confirm that|i understand|i have read"),
]]

_DECLINE_OPTION = re.compile(
    r"decline|prefer not|do not wish|don'?t wish|choose not|not to (say|answer|disclose|identify|specify)|rather not|"
    r"i do not want|no answer|not specified", re.I)
_YES, _NO = re.compile(r"^\s*yes\b", re.I), re.compile(r"^\s*no\b", re.I)


_ASKS_FOR_NAME = re.compile(r"\b(provide|enter|state|write|type|list|what is|what's)\b.*\b(first and last|full|legal) (legal )?name", re.I)


def classify(label: str) -> str:
    """The fact key a field label asks about, or ''."""
    text = re.sub(r"\s+", " ", label or "").strip()
    for key, pattern in RULES:
        if not pattern.search(text):
            continue
        plain = re.sub(r"\([^)]*\)", " ", text)  # "State (If you do not live in any of the states listed...)" is still State
        if key == "full_name" and _ASKS_FOR_NAME.search(text) and not _YES_NO_QUESTION.match(text):
            return key  # "Please provide your legal first and last name." is the name field in sentence form
        if key in _IDENTITY and (len(plain.split()) > 7 or _YES_NO_QUESTION.match(text)):
            if not _ASKS_FOR_PLACE.search(text) and not _ASKS_FOR_ROLE.search(text):
                continue  # a long sentence that merely mentions a place or an employer is not that field
        if key == "work_authorization" and _SPONSOR.search(text):
            return "ambiguous"  # asks about authorization AND sponsorship in one question: not ours to merge
        if key == "needs_sponsorship" and re.search(r"authori[sz]ed to work|eligible to work|right to work", text):
            return "ambiguous"
        if key == "years_experience" and _SKILL_QUALIFIER.search(text):
            return ""  # "years of experience with Python" is about one skill, not the total: an open question
        return key
    return ""


# Rules that name a plain field (a name, an address, an employer). They must not capture a long sentence or a
# question that merely contains one of those words.
_IDENTITY = {"first_name", "last_name", "full_name", "email", "phone", "address", "zip", "state", "country", "city",
             "location", "current_company", "current_title", "school", "degree"}

_YES_NO_QUESTION = re.compile(r"\s*(will|would|do|does|did|are|is|have|has|can|could|should)\b.*\?\s*$", re.I)
# These ask for the place itself, so length does not turn them into an open question.
_ASKS_FOR_PLACE = re.compile(
    r"country where you (currently )?(live|reside)|countries you anticipate working|"
    r"(what|which) city and state|city and state do you (live|reside)|where do you (live|reside)|do you reside|"
    r"what state|state are you", re.I)
# "Who is your current or previous employer?" is the employer field even though it is longer than seven words.
_ASKS_FOR_ROLE = re.compile(
    r"\b(?:who|what)(?:'s| is| was)? your\b.{0,60}\b(?:employer|company|job title|title|role|position)\b", re.I)
_SPONSOR = re.compile(r"sponsor|visa", re.I)
_SKILL_QUALIFIER = re.compile(
    r"experience\b.*?\b(with|in|using|on|as|building|developing|working with)\s+"
    r"(?!total\b|all\b|the industry|the field|your career|general\b|overall\b|total)\S", re.I)


# ---------------------------------------------------------------------------- the stated value

def _profile_value(key: str, ctx: Context) -> str:
    p, f = ctx.profile, ctx.facts
    first, last = split_name(p.name)
    city_state = ", ".join(x for x in (f.get("city", ""), f.get("state", "")) if x)
    table = {
        "first_name": first, "last_name": last, "full_name": p.name.strip(), "email": p.email, "phone": p.phone,
        "linkedin_url": p.linkedin_url, "github_url": p.github_url, "website_url": p.website_url,
        "location": city_state or p.location, "city": f.get("city") or p.location.split(",")[0].strip(),
        "work_authorization": p.work_authorization, "needs_sponsorship": p.needs_sponsorship,
    }
    latest = _latest_job(ctx.resume)
    table.update({
        # read from the resume written for this job, never typed in twice
        "current_company": latest.get("company", ""), "current_title": latest.get("title", ""),
        "years_experience": years_from_resume(ctx.resume, ctx.today),
        "school": p.education_school, "degree": p.education_degree,
    })
    if key == "authorized_and_no_sponsorship":
        if not (p.work_authorization and p.needs_sponsorship):
            return ""
        return "Yes" if (p.work_authorization == "Yes" and p.needs_sponsorship == "No") else "No"
    value = table.get(key, "") or f.get(key, "")
    return str(value).strip()


def _latest_job(resume: dict) -> dict[str, str]:
    """The most recent position on the resume (the first one listed): its company and title."""
    jobs = (resume or {}).get("experience")
    first = jobs[0] if isinstance(jobs, list) and jobs and isinstance(jobs[0], dict) else {}
    return {"company": str(first.get("company") or "").strip(),
            "title": str(first.get("title") or first.get("role_title") or "").strip()}


def years_from_resume(resume: dict, today: date) -> str:
    """Whole years from the earliest year on the resume's experience to today ('' when no year can be read)."""
    years = []
    for job in (resume or {}).get("experience") or []:
        if isinstance(job, dict):
            years += [int(y) for y in re.findall(r"\b(19[89]\d|20\d\d)\b", str(job.get("dates") or ""))]
    return str(max(0, today.year - min(years))) if years else ""


# ---------------------------------------------------------------------------- choosing among options

def _number_option(n: int, options: list[Option]) -> Option | None:
    for opt in options:
        nums = [int(x) for x in re.findall(r"\d+", opt.label)]
        if len(nums) >= 2 and nums[0] <= n <= nums[1]:
            return opt
        if len(nums) == 1 and re.search(r"\+|or more|and (up|above)", opt.label) and n >= nums[0]:
            return opt
        if len(nums) == 1 and re.search(r"less than|under|fewer", opt.label, re.I) and n < nums[0]:
            return opt
        if len(nums) == 1 and "+" not in opt.label and nums[0] == n and not re.search(r"-|to", opt.label):
            return opt
    return None


def _plain(text: str) -> str:
    return (text or "").replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')


def _word_option(needle: str, options: list[Option]) -> Option | None:
    needle = needle.strip()
    if len(needle) < 3:
        return None
    pat = re.compile(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", re.I)
    hits = [o for o in options if pat.search(_plain(o.label))]
    return hits[0] if len(hits) == 1 else None


def _veteran_kind(text: str) -> str:
    t = _plain(text).strip()
    if _DECLINE_OPTION.search(t):
        return "decline"
    if _SERVED.search(t):
        return "served"
    if _NOT_VETERAN.search(t) or re.match(r"no\b", t, re.I):
        return "no"
    if _IS_VETERAN.search(t) or re.match(r"yes\b", t, re.I):
        return "yes"
    return ""


def _veteran_option(answer: str, options: list[Option]) -> Option | None:
    kind = _veteran_kind(answer)
    if not kind:
        return None
    hits = [o for o in options if _veteran_kind(o.label) == kind]
    if hits:
        return hits[0]
    if kind == "no":
        return next((o for o in options if _NO.search(o.label)), None)
    if kind == "yes":
        return next((o for o in options if _YES.search(o.label)), None)
    return None


def _phrase_option(answer: str, options: list[Option], key: str) -> Option | None:
    """A saved Greenhouse wording matched onto this form's own sentence."""
    if key == "eeo_veteran":
        return _veteran_option(answer, options)
    if key == "eeo_race":
        hit = _word_option(answer, options) or _word_option(answer.split("(")[0], options)
        if hit:
            return hit
        return _word_option(" ".join(answer.split()[:2]), options)
    if key == "eeo_orientation":
        for part in re.split(r"\s*/\s*|\s*,\s*", answer):
            hit = _word_option(part, options)
            if hit:
                return hit
        return None
    if key == "degree":
        return _word_option(answer, options)
    if key == "eeo_gender":
        hit = _word_option(answer, options)
        if hit:
            return hit
        # Greenhouse often says Man / Woman where the saved answer says Male / Female.
        synonyms = {"male": ("man", "male"), "female": ("woman", "female"),
                    "non-binary": ("non-binary", "nonbinary"), "nonbinary": ("non-binary", "nonbinary")}
        for word in synonyms.get(answer.casefold(), ()):
            hit = _word_option(word, options)
            if hit:
                return hit
        return None
    if key == "eeo_disability":
        text = _plain(answer)
        if re.search(r"do not have|don't have|^\s*no\b", text, re.I):
            specific = next((o for o in options if re.search(r"do not have|don't have", _plain(o.label), re.I)), None)
            return specific or next((o for o in options if _NO.search(o.label)), None)
        if re.search(r"have a disability|have had one|^\s*yes\b", text, re.I):
            specific = next((o for o in options
                             if re.search(r"have a disability|have had one", _plain(o.label), re.I)
                             and not re.search(r"do not have|don't have|^\s*no\b", _plain(o.label), re.I)), None)
            return specific or next((o for o in options if _YES.search(o.label)), None)
    return None


_NO_SPONSORSHIP = re.compile(r"\b(not|never|without|no)\b|n't", re.I)


def _sponsorship_option(answer: str, options: list[Option]) -> Option | None:
    """Sponsorship choices written as statements instead of Yes/No ("Requires sponsorship for employment
    authorization" / "Does not require sponsorship"). Only when exactly one choice says what you said."""
    if answer not in {"Yes", "No"}:
        return None
    about = [o for o in options if re.search(r"sponsor", o.label, re.I)]
    if len(about) != len(options) or len(options) != 2:
        return None  # anything but a plain two-way choice about sponsorship is left for you
    negative = [o for o in about if _NO_SPONSORSHIP.search(o.label)]
    positive = [o for o in about if not _NO_SPONSORSHIP.search(o.label)]
    if len(negative) != 1 or len(positive) != 1:
        return None
    return negative[0] if answer == "No" else positive[0]


_ASKS_AUTH_TYPE = re.compile(
    r"authori[sz]ed to work.{0,160}(specify|state|indicate|list|provide|describe).{0,60}"
    r"(authori[sz]ation|status|type|visa)", re.I | re.S)


def _authorized_and_status(f: Field, ctx: Context) -> Decision | None:
    """A free-text "Are you authorized to work here? If yes, specify your authorization type": both halves are in
    your saved answers (authorized: Yes; work status: e.g. US citizen), so it is answered from them, in one line."""
    if f.is_choice or f.kind not in {"text", "textarea"} or not _ASKS_AUTH_TYPE.search(f.label or ""):
        return None
    status = _stated("visa_status", ctx)
    if _stated("work_authorization", ctx) != "Yes" or not status:
        return None
    return Decision(FILL, f"Yes, {status}", "facts")


def _remote_arrangement_option(options: list[Option], ctx: Context) -> Option | None:
    """For "which arrangement do you prefer?" lists (Fully remote / Hybrid (city) / In office (city)) rather than
    yes/no: the single remote choice, when you said you are comfortable working remotely. Anything less clear-cut
    (no remote choice, several of them, or remote not wanted) is left for you."""
    if _stated("remote_ok", ctx) != "Yes":
        return None
    remote = [o for o in options if re.search(r"\bremote\b", o.label, re.I)
              and not re.search(r"\b(not|non|no)\b[ -]*remote", o.label, re.I)]
    return remote[0] if len(remote) == 1 else None


def choose_option(answer: str, options: list[Option], key: str = "") -> Option | None:
    """The option that states ``answer``: yes/no by the first word, a decline wording, a number range, or the text."""
    if not options:
        return None
    answer = _plain(answer)
    if key == "state" and re.fullmatch(r"[A-Za-z]{2}", answer.strip()):
        answer = _STATE_NAMES.get(answer.strip().upper(), answer)
    if answer == DECLINE or (key.startswith("eeo_") and _DECLINE_OPTION.search(answer)):
        hit = next((o for o in options if _DECLINE_OPTION.search(_plain(o.label))), None)
        if hit:
            return hit
        if answer == DECLINE:
            return None
    if answer in {"Yes", "No"}:
        if key == "eeo_veteran":  # "No" is "I do not fall into the protected-veteran categories", which may not begin with No
            return _veteran_option(answer, options)
        pattern = _YES if answer == "Yes" else _NO
        hits = [o for o in options if pattern.search(o.label)]
        if hits:
            return hits[0]
        if key == "agree_consents":  # the only choice is often "I agree", not "Yes"
            return _consent_option(answer, options)
        return None
    if key == "country":
        picked = _country_option(answer, options)
        if picked:
            return picked
    if key == "time_zone" and answer not in {"Yes", "No"}:
        pattern = dict(_ZONE_IN_QUESTION).get(answer, "")
        if pattern:
            hits = [o for o in options if re.search(pattern, o.label, re.I)]
            if len(hits) == 1:
                return hits[0]
    phrase = _phrase_option(answer, options, key)
    if phrase:
        return phrase
    if re.fullmatch(r"\d{1,2}", answer) and key == "years_experience":
        picked = _number_option(int(answer), options)
        if picked:
            return picked
    labels = [o.label for o in options]
    hit = match_option(answer, labels)
    if hit:
        return next(o for o in options if o.label == hit)
    if key == "referral_source":  # an unlisted source: the closest honest bucket
        return next((o for o in options if re.match(r"\s*(other|job board|online|internet|social)", o.label, re.I)), None)
    return None


# ---------------------------------------------------------------------------- the decision

def decide(f: Field, ctx: Context) -> Decision:
    label = f.label.strip()

    if f.kind == "file":
        if f.slot == "resume":
            return Decision(FILL, RESUME_FILE, "profile") if ctx.has_resume else _unanswerable(f, "No resume file")
        if f.slot == "cover":
            return Decision(FILL, COVER_FILE, "profile") if ctx.has_cover else Decision(SKIP, reason="No cover letter file")
        return _unanswerable(f, "Unknown attachment")

    if f.value.strip() and f.kind not in {"radio", "checkbox"}:
        return Decision(SKIP, reason="Already filled")
    if f.kind in {"radio", "checkbox"} and f.value == "checked":
        return Decision(SKIP, reason="Already chosen")

    if f.kind == "textarea" and re.search(r"cover letter", label, re.I) and ctx.cover_letter_text:
        return Decision(FILL, ctx.cover_letter_text, "profile")

    if _is_bot_check(f):
        # The options are "I am an AI" and "I am a human being". Leave it blank either way.
        return _unanswerable(f, "This asks whether the submission is automated, so it is left blank")

    typed = _manual_answer(f, ctx)
    if typed:
        return typed

    key = classify(label)

    if _CONDITIONAL.search(label) and not f.required:
        return Decision(SKIP, reason="Follow-up that only applies to a previous answer")

    if f.kind == "checkbox":
        return _checkbox(f, key, ctx)
    none = _none_of_the_above(f, ctx)
    if none:
        return none
    follow = _na_followup(f, ctx)
    if follow:
        return follow

    if key == "ambiguous":
        both = _authorized_and_status(f, ctx)
        if both:
            return both
        return _unanswerable(f, f"Asks two things at once: {label}")
    if key == "time_zone":
        return _time_zone_decision(f, ctx)
    if key == "years_experience" and ctx.chat_enabled and (f.options or f.kind in {"text", "textarea", "number"}):
        return Decision(CHAT, source="chat")  # the chat reads the resume; the date arithmetic below is the fallback
    if key:
        answer = _stated(key, ctx)
        if key == "degree" and (f.is_choice or f.kind == "combobox"):
            answer = _degree_level(answer) or answer  # a list is filed under Bachelor, not the full degree name
        if not answer:
            return _unanswerable(f, f"Not filled in under Auto Apply > Your answers: {label}")
        if key == "agree_consents" and answer == "No":
            # "No" on "I certify this is true" would be a false statement. Stop and let you switch it to Yes.
            return _unanswerable(f, "Acknowledgement is set to No under Auto Apply > Your answers, and this form requires agreeing")
        if f.kind == "combobox" and not f.options:
            if key in _TYPE_AHEAD:  # a city, school, or country lookup: the text is typed and a suggestion picked
                return Decision(FILL, answer, "profile" if key in _PROFILE_KEYS else "facts")
            return _unanswerable(f, f"Could not read the choices for: {label}")  # never type text into a list of options
        if f.is_choice or (f.kind == "checkgroup" and f.options):
            option = choose_option(answer, f.options, key)
            if option is None and key == "work_authorization":
                option = _authorization_status_option(f.options, ctx)
            if option is None and key == "needs_sponsorship":
                option = _sponsorship_option(answer, f.options)
            if option is None and key in {"remote_ok", "onsite_ok", "work_setup"}:
                option = _remote_arrangement_option(f.options, ctx)
            if option is None and f.kind == "combobox" and key in _TYPE_AHEAD:
                return Decision(FILL, answer, "profile" if key in _PROFILE_KEYS else "facts")  # the list is virtualized: type to filter
            if option is None:
                saved = _saved_form_answer(f, ctx)
                if saved:
                    return saved
                if key.startswith("eeo_"):
                    # A voluntary demographic list that words things differently (East Asian / South Asian for
                    # "Asian"): picking one would be a guess, and "I don't wish to answer" is always a true answer.
                    decline = next((o for o in f.options if _DECLINE_OPTION.search(_plain(o.label))), None)
                    if decline is not None:
                        return Decision(FILL, decline.label, "facts")
                return _unanswerable(f, f"No option matches '{answer}' for: {label}")
            return Decision(FILL, option.label, "facts")
        return Decision(FILL, answer, "profile" if key in _PROFILE_KEYS else "facts")

    # no personal fact: the chat that wrote the resume answers it from the job and the resume.
    # Legal and identity questions never go there. A choice you already picked is kept.
    if SENSITIVE.search(label):
        saved = _saved_form_answer(f, ctx)
        if saved:
            return saved
        return _unanswerable(f, f"Not covered by your facts: {label}")
    choice_like = f.is_choice or f.kind == "checkgroup"
    if choice_like and f.options and ctx.chat_enabled:
        saved = _saved_form_answer(f, ctx)
        if saved:
            return saved
        return Decision(CHAT, source="chat")
    if choice_like:
        saved = _saved_form_answer(f, ctx)
        if saved:
            return saved
        return _unanswerable(f, f"Not covered by your facts: {label}")
    if f.kind in {"text", "textarea", "number"} and ctx.chat_enabled and _is_open_question(f):
        return Decision(CHAT, source="chat")
    return _unanswerable(f, f"No answer for: {label}")


_TYPE_AHEAD = {"location", "city", "school", "country", "degree"}
# "If you selected X above, ...": a follow-up to another answer, so an optional one is left alone.
_CONDITIONAL = re.compile(r"^\s*(if|in case|when)\b.*\b(selected|chose|answered|checked|said|other|yes|no|not listed)\b", re.I)

_PROFILE_KEYS = {"first_name", "last_name", "full_name", "email", "phone", "linkedin_url", "github_url",
                 "website_url", "location", "work_authorization", "needs_sponsorship", "current_company", "current_title",
                 "years_experience", "school", "degree"}


def _stated(key: str, ctx: Context) -> str:
    if key == "can_perform":
        return ctx.facts.get("can_perform") or "Yes"  # applying says you can do the job, with or without an accommodation
    if key == "work_setup":
        return ctx.facts.get("work_setup") or "Yes"  # "Yes, I can work this way now" when you can work from where you live
    if key == "located_in_us":
        stated = (ctx.facts.get("located_in_us") or "").strip()
        if stated:
            return stated
        country = re.sub(r"[^a-z]", "", (ctx.facts.get("country") or "United States").lower())
        return "Yes" if country in {"unitedstates", "unitedstatesofamerica", "usa", "us"} else "No"
    if key.startswith("eeo_"):
        return ctx.facts.get(key, DECLINE)  # the default is to decline
    return _profile_value(key, ctx)


def _degree_level(text: str) -> str:
    """'B.S. Computer Science' is typed as Bachelor, which is the word a degree list is filed under."""
    t = (text or "").lower()
    if not t.strip():
        return ""
    if re.search(r"\bph\.?d\b|doctorate|\bdoctor of\b", t):
        return "Doctorate"
    if re.search(r"\bmba\b|\bmaster|\bm\.s\b|\bm\.a\b", t):
        return "Master"
    if re.search(r"\bbachelor|\bb\.s\b|\bb\.a\b|\bbs\b|\bba\b", t):
        return "Bachelor"
    if re.search(r"\bassociate", t):
        return "Associate"
    if re.search(r"high school|\bged\b", t):
        return "High School"
    return text.strip()


def _authorization_status_option(options: list[Option], ctx: Context) -> Option | None:
    """'Yes, I am authorized' mapped onto 'authorized and will not require sponsorship' when that is your status."""
    needs = ctx.profile.needs_sponsorship == "Yes"
    authorized = ctx.profile.work_authorization == "Yes"
    if authorized and not needs:
        return next((o for o in options if re.search(r"authori[sz]ed to work", o.label, re.I)
                     and re.search(r"not require|will not require", o.label, re.I)), None)
    if needs:
        return next((o for o in options if re.search(r"require", o.label, re.I) and re.search(r"sponsor", o.label, re.I)
                     and not re.search(r"not require|will not require", o.label, re.I)), None)
    return None


def _us_label(label: str) -> bool:
    """A country option that means the United States, including a longer wording of the same country."""
    name = re.sub(r"[^a-z]", "", (label or "").lower())
    if name in {"unitedstates", "unitedstatesofamerica", "usa", "us"}:
        return True
    return "minor" not in name and bool(re.fullmatch(r"unitedstates(?:ofamerica)?[a-z]*", name))


def _country_option(answer: str, options: list[Option]) -> Option | None:
    folded = re.sub(r"[^a-z]", "", answer.lower())
    if folded not in {"unitedstates", "unitedstatesofamerica", "usa", "us"}:
        return None
    hits = [opt for opt in options if _us_label(opt.label)]
    exact = [opt for opt in hits if re.sub(r"[^a-z]", "", opt.label.lower()) in
             {"unitedstates", "unitedstatesofamerica", "usa", "us"}]
    return (exact or hits)[0] if hits else None


def _manual_answer(f: Field, ctx: Context) -> Decision | None:
    """An answer you typed for this exact question. A list is filled only when your text is one of its options."""
    answer = (ctx.form_answers or {}).get(norm_question(f.label), "").strip()
    if not answer:
        return None
    if f.options:
        return _saved_form_answer(f, ctx)
    if f.kind in {"text", "textarea", "number"} or f.kind == "combobox":
        return Decision(FILL, answer, "facts")
    return None


def _saved_form_answer(f: Field, ctx: Context) -> Decision | None:
    """An option you already picked for this exact question, on Your answers."""
    answer = (ctx.form_answers or {}).get(norm_question(f.label), "")
    if not answer or not f.options:
        return None
    exact = next((o for o in f.options if o.label.strip() == answer.strip()), None)
    if exact is None:
        return None
    return Decision(FILL, exact.label, "facts")


def _na_followup(f: Field, ctx: Context) -> Decision | None:
    """'If Yes, enter your name. If No, enter N/A.': N/A when you have not worked there, your name when you have."""
    if not re.search(r"if no\b.{0,80}\b(n/?a|not applicable)\b", f.label, re.I):
        return None
    if _stated("previously_employed", ctx) == "Yes" and re.search(r"\bname\b", f.label, re.I):
        name = _stated("full_name", ctx)
        return Decision(FILL, name, "profile") if name else None
    if _stated("previously_employed", ctx) == "Yes":
        return None
    return Decision(FILL, "N/A", "facts")


def _none_of_the_above(f: Field, ctx: Context) -> Decision | None:
    """A 'check all that apply' list with a 'none of the above' box: tick that, unless a relative works there."""
    if f.kind != "checkgroup" or not re.search(r"any of the following apply|check all that apply", f.label, re.I):
        return None
    none = next((o for o in f.options if _NONE_OPTION.search(o.label)), None)
    if none is None:
        return None
    if _stated("has_relatives", ctx) == "Yes":
        rel = next((o for o in f.options if re.search(r"family member|relative|spouse|partner", o.label, re.I)), None)
        if rel:
            return Decision(FILL, rel.label, "facts")
    return Decision(FILL, none.label, "facts")


def _consent_option(answer: str, options: list[Option]) -> Option | None:
    """The choice that gives (or withholds) an acknowledgement: "I agree", "Acknowledge/Confirm", "Accept"."""
    negative = [o for o in options if re.search(r"do not|don't|not agree|disagree|decline", o.label, re.I)]
    if answer != "Yes":
        return negative[0] if negative else None
    positive = [o for o in options if o not in negative and re.search(
        r"^\s*(i\s+)?(have read|agree|consent|accept|acknowledge|confirm|understand)\b", o.label, re.I)]
    if positive:
        return positive[0]
    rest = [o for o in options if o not in negative]
    return rest[0] if len(options) == 1 and rest else None  # a list whose only choice is the acknowledgement itself


_NOT_VETERAN = re.compile(
    r"not a (protected )?veteran|i am not a veteran|do not identify as a (protected )?veteran|"
    r"do not fall into|non-?veteran|not (a )?protected veteran", re.I)
_IS_VETERAN = re.compile(r"identify as one|i am a protected veteran", re.I)
_SERVED = re.compile(r"previously served|prior (military|service)", re.I)
_NONE_OPTION = re.compile(r"none of the (above|following|these)|none of these|no,? none of", re.I)
_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


_EASTERN = {
    "connecticut", "delaware", "district of columbia", "florida", "georgia", "indiana", "kentucky", "maine",
    "maryland", "massachusetts", "michigan", "new hampshire", "new jersey", "new york", "north carolina", "ohio",
    "pennsylvania", "rhode island", "south carolina", "vermont", "virginia", "west virginia",
}
_CENTRAL = {
    "alabama", "arkansas", "illinois", "iowa", "kansas", "louisiana", "minnesota", "mississippi", "missouri",
    "nebraska", "north dakota", "oklahoma", "south dakota", "tennessee", "texas", "wisconsin",
}
_MOUNTAIN = {"arizona", "colorado", "idaho", "montana", "new mexico", "utah", "wyoming"}
_PACIFIC = {"california", "nevada", "oregon", "washington"}
_ZONE_OF_STATE = {name: zone for zone, names in (
    ("Eastern", _EASTERN), ("Central", _CENTRAL), ("Mountain", _MOUNTAIN), ("Pacific", _PACIFIC),
    ("Alaska", {"alaska"}), ("Hawaii", {"hawaii"}),
) for name in names}
_ZONE_IN_QUESTION = (
    ("Eastern", r"\b(est|edt)\b|\beastern\b"),
    ("Central", r"\b(cst|cdt)\b|\bcentral\b"),
    ("Mountain", r"\b(mst|mdt)\b|\bmountain\b"),
    ("Pacific", r"\b(pst|pdt)\b|\bpacific\b"),
    ("Alaska", r"\bakst\b|\balaska\b"),
    ("Hawaii", r"\bhst\b|\bhawaii\b"),
)
_BOT_CHECK_OPTION = re.compile(r"automated program|\bam an ai\b|\bhuman being\b", re.I)


def _is_bot_check(f: Field) -> bool:
    """A honeypot whose options are an automated program versus a human being."""
    return any(_BOT_CHECK_OPTION.search(o.label) for o in f.options)


def _residence_state_name(ctx: Context) -> str:
    raw = (ctx.facts.get("state") or "").strip()
    if not raw:
        parts = [p.strip() for p in (ctx.profile.location or "").split(",") if p.strip()]
        if parts:
            tail = parts[-1]
            country = re.sub(r"[^a-z]", "", tail.lower())
            raw = parts[-2] if country in {"unitedstates", "usa", "us"} and len(parts) >= 2 else tail
    if re.fullmatch(r"[A-Za-z]{2}", raw):
        raw = _STATE_NAMES.get(raw.upper(), raw)
    return raw


def _zone_of_residence(ctx: Context) -> str:
    return _ZONE_OF_STATE.get(_residence_state_name(ctx).casefold(), "")


def _zone_named(label: str) -> str:
    for zone, pattern in _ZONE_IN_QUESTION:
        if re.search(pattern, label, re.I):
            return zone
    return ""


def _time_zone_decision(f: Field, ctx: Context) -> Decision:
    """'Are you EST based?' is where you live. Florida is Eastern, so the answer is Yes."""
    zone = _zone_of_residence(ctx)
    label = f.label.strip()
    if not zone:
        return _unanswerable(f, f"Not filled in under Auto Apply > Your answers: {label}")
    asked = _zone_named(label)
    answer = ("Yes" if asked == zone else "No") if asked else zone
    if not f.is_choice:
        return Decision(FILL, answer, "facts")
    option = choose_option(answer, f.options, "time_zone")
    if option is None and f.kind == "combobox" and not f.options:
        return Decision(FILL, answer, "facts")
    if option is None:
        return _unanswerable(f, f"No option matches '{answer}' for: {label}")
    return Decision(FILL, option.label, "facts")


def _unanswerable(f: Field, reason: str) -> Decision:
    return Decision(NEEDS if f.required else SKIP, reason=reason)


def _is_open_question(f: Field) -> bool:
    text = f.label.strip()
    return f.kind == "textarea" or len(text.split()) >= 4 or text.endswith("?")


def _checkbox(f: Field, key: str, ctx: Context) -> Decision:
    label = f.label
    if key == "sms_consent":
        return Decision(FILL if ctx.facts.get("sms_consent") == "Yes" else SKIP, "checked", "facts", "Text-message consent")
    if key == "agree_consents":
        if SENSITIVE.search(label):
            return _unanswerable(f, f"Not covered by your facts: {label}")
        if ctx.facts.get("agree_consents", "Yes") == "Yes":
            return Decision(FILL, "checked", "facts")
        return _unanswerable(f, f"Acknowledgement not accepted: {label}")
    if re.search(r"newsletter|marketing|promotional|updates about|future (opportunities|roles)", label, re.I):
        return Decision(SKIP, reason="Optional communications")
    return _unanswerable(f, f"Unknown checkbox: {label}")
