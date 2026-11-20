"""Asking the job's own chat conversation to answer an application-form question.

Replies are fenced with markers so the answer can be lifted out of whatever else the chat page shows (buttons,
disclaimers, a polite opening line). Nothing here touches the browser, so it is all unit-testable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

START = "[[ANSWER_START]]"
END = "[[ANSWER_END]]"

DEFAULT_ANSWER_PROMPT = (
    "Write the answer exactly as a real person would type it by hand in a professional setting: natural, specific "
    "and concise, in the first person. Make it the best fit for this job description and for my resume JSON for "
    "this job. Never invent an employer, title, date, degree, tool, metric or project that is not in that resume "
    "JSON."
)

_PLACEHOLDERS = {"", "...", "…", "your answer here", "answer", "<answer>", "the answer", "final answer"}

# How a typed answer (not a choice from a list) is written: the way a confident engineer types their own answer, in
# the plain, direct tone Steve Jobs and Bill Gates use when they talk about their work.
WRITING_STYLE = (
    "HOW TO WRITE IT\n"
    "- Write it the way I would type it myself: plain everyday words, short sentences, one idea per sentence.\n"
    "- Be concrete: what I built or did, for whom, and what changed because of it. Facts over adjectives.\n"
    "- The tone of Steve Jobs and Bill Gates when they talk about their work: simple, direct, specific and quietly "
    "confident. No hype, no buzzwords, no flattery of the company.\n"
    "- Active voice, first person. Contractions are fine. Vary sentence length a little, as people do.\n"
    "- Do not use these words or phrases: delve, leverage, utilize, spearhead, passionate, thrilled, excited to, eager "
    "to, robust, seamless, cutting-edge, innovative, synergy, dynamic, fast-paced, tapestry, landscape, realm, journey, "
    "testament, pivotal, crucial, meticulous, foster, empower, elevate, harness, navigate, moreover, furthermore, "
    "additionally, in conclusion, overall, it's worth noting, I'm confident that.\n"
    "- No dashes as punctuation, no lists of three adjectives, no rhetorical questions, no summary sentence at the end."
)

# Words that make a typed answer read as machine-written (see WRITING_STYLE). Found in an answer, it is asked for again.
_AI_TELLS = re.compile(
    r"\b(delv(e|es|ed|ing)|leverag(e|es|ed|ing)|utiliz(e|es|ed|ing)|spearhead(ed|ing)?|passionate|thrilled|"
    r"excited to|eager to|robust|seamless(ly)?|cutting-edge|innovative|synerg(y|ies)|fast-paced|tapestry|landscape|"
    r"realm|testament|pivotal|meticulous(ly)?|foster(ed|ing)?|empower(ed|ing|s)?|elevat(e|ed|ing)|harness(ed|ing)?|"
    r"moreover|furthermore|additionally|in conclusion|it'?s worth noting|i'?m confident that)\b", re.I)


def ai_tells(text: str) -> list[str]:
    """The machine-sounding words in a typed answer, each once, in the order they appear."""
    seen: list[str] = []
    for m in _AI_TELLS.finditer(text or ""):
        word = m.group(0).lower()
        if word not in seen:
            seen.append(word)
    return seen

NO_ANSWER = "NO_ANSWER"  # what the chat writes when it truly cannot answer, instead of explaining why

# Instructions that hold for every question, whatever answer style is set in Settings.
# How to answer experience and skill questions from the resume, when that resume is built on your real work history
# (Settings > work history). The chat answers every such question from the resume: years added up across roles,
# reasonable conclusions from the work described, the closest option of a list, an honest No when the resume plainly
# lacks something. It never adds anything to the resume.
RESUME_RULES = (
    "HOW TO ANSWER FROM MY RESUME\n"
    "- My resume above is built on my real work history. Answer experience and skill questions from it, the way I "
    "would answer them in an interview.\n"
    "- Years of experience with a skill: add up the time of every role in which the resume shows that skill (count "
    "overlapping roles once). Total experience: from my first role to today.\n"
    "- Draw reasonable conclusions from the work the resume describes: work that clearly involves something counts as "
    "experience with it (incident response and production support mean on-call; teams in several countries mean "
    "distributed teams; building APIs on a cloud platform means that cloud).\n"
    "- Never add an employer, title, date, degree, tool, metric or project that is not in the resume.\n"
    "- A list of ranges or levels: choose the option closest to what the resume supports.\n"
    "- A yes/no question: Yes when the resume shows it or it follows directly from it; No when the resume plainly lacks "
    "it. An honest No is an answer: do not reply NO_ANSWER to a question you can answer No to.\n"
    "- A written answer: use real work from the resume, framed for this job, specific and in the first person.\n"
    f"- Reply NO_ANSWER only when nothing in my resume or my facts relates to the question at all."
)

ANSWER_RULES = (
    "Answer as me, in the first person, exactly as I would type it into the form. Never mention a resume, JSON, the "
    "job description, verification, these instructions, or being an AI, and never comment on what information you "
    "have. Never write things like 'not provided', 'I can't determine', 'I can't confirm' or 'I'm not sure'. "
    f"If you truly cannot answer from my facts and my experience, reply with exactly {NO_ANSWER} between the markers "
    "instead of explaining. When the options include an unsure or prefer-not choice, pick it only if no other option "
    "is true."
)

# Signs that a written answer talks ABOUT the information instead of answering (it must never reach a form).
_META = [
    (re.compile(r"\bnot (provided|specified|available|listed|stated|mentioned|included)\b", re.I), "says something is not provided"),
    (re.compile(r"\b(can(?:'|\u2019|no)?t|cannot|unable to|not able to) (determine|confirm|verify|know|tell|say|provide)\b", re.I),
     "says it cannot answer"),
    (re.compile(r"\b(i'?m|i am) not sure\b|\bi don'?t know\b", re.I), "says it is not sure"),
    (re.compile(r"\b(my|the) resume\b|\bresume json\b|\bjson\b|\bjob description\b|\bverified\b", re.I),
     "talks about the resume or the job description"),
    (re.compile(r"\bas an ai\b|\b(i'?m|i am) an ai\b|\blanguage model\b|\bthe candidate\b", re.I), "speaks as an AI"),
]


def answer_problem(text: str, is_choice: bool = False) -> str:
    """Why a chat answer must not be typed into a form ('' when it is fine). A choice copied from the form's own list is
    the form's wording, so only the NO_ANSWER reply is checked there."""
    plain = (text or "").strip()
    if plain.strip(" .").upper() == NO_ANSWER:
        return "the chat had no answer"
    if is_choice:
        return ""
    for pattern, why in _META:
        if pattern.search(plain):
            return why
    return ""


@dataclass
class Question:
    text: str
    # Option labels when the field is a choice (select / radio); the answer must be one of them, verbatim.
    options: list[str] = field(default_factory=list)
    max_chars: int = 0  # 0 = no stated limit
    kind: str = "text"  # text | choice | number
    hint: str = ""  # added to the prompt, e.g. when the first answer was not one of the options
    extra: dict = field(default_factory=dict)  # kind "salary": the facts the prompt states (see core.salary_answer)
    figure: int = 0  # kind "salary" with options: the yearly figure the option must contain
    facts: str = ""  # the personal facts block (core.personal_facts): true, and never to be contradicted
    from_resume: bool = False  # answer experience questions by the RESUME_RULES (the resume is your real history)


def build_question_prompt(question: Question, style_prompt: str = DEFAULT_ANSWER_PROMPT) -> str:
    if question.kind == "salary":
        from core.salary_answer import build_salary_prompt  # late: salary_answer imports this module

        return build_salary_prompt(question.extra, question.text, question.options or None, question.figure)
    lines = [
        style_prompt.strip() or DEFAULT_ANSWER_PROMPT,
        "",
        "I am applying for THIS job right now, and you are filling in its application form as me. Give the answer "
        "that gives me the best real chance of being hired for this job: specific, confident and positive, in line "
        "with the job description and the resume JSON from earlier in this conversation, and true to my facts below.",
    ]
    if question.facts.strip():
        lines += [
            "",
            "MY PERSONAL FACTS (they are true, they come from my own settings, and they outrank everything else; never "
            "contradict them):",
            question.facts.strip(),
            "",
            "For questions about work eligibility, citizenship, sponsorship, location, availability or consent, answer "
            "ONLY from these facts. If a question asks several things at once, choose the option that is true for all of "
            "them. If the facts do not cover it, choose the most truthful option that does not overstate me. Never "
            "invent personal details.",
        ]
    lines += ["", ANSWER_RULES]
    if question.from_resume:
        lines += ["", RESUME_RULES]
    lines += ["", f"Question: {question.text.strip()}"]
    if question.options:
        lines.append("Choose exactly one of these options and reply with it word for word: "
                     + json.dumps(question.options, ensure_ascii=False))
    if question.hint:
        lines.append(question.hint.strip())
    if question.kind == "number":
        lines.append("Reply with a single number only.")
    if not question.options and question.kind == "text":
        lines += ["", WRITING_STYLE]
    if question.max_chars:
        lines.append(f"The answer must be at most {question.max_chars} characters including spaces.")
    lines += [
        "",
        f"Reply with ONLY the answer between the markers {START} and {END}, with nothing before or after them. "
        "Plain text only: no markdown, no quotation marks around it, no bullet points unless the question asks "
        "for a list, no mention of being an AI.",
    ]
    return "\n".join(lines)


# The first message of a question chat is kept well under the length at which ChatGPT turns a paste into a "Pasted
# text" card (about 20,000 characters for this kind of text): the description is cut at this length, and the resume
# is sent compact and without its cover letter (the letter is typed into its own field, never used for answers).
MAX_DESCRIPTION_CHARS = 6000
_NOT_FOR_ANSWERS = ("cover_letter",)


def compact_resume(resume_json: dict) -> str:
    """The resume as compact JSON, without the parts that do not help answer form questions."""
    kept = {k: v for k, v in (resume_json or {}).items() if k not in _NOT_FOR_ANSWERS and v not in ("", None, [], {})}
    return json.dumps(kept, ensure_ascii=False, separators=(",", ":"))


def build_context_prompt(*, job_description: str, resume_json: dict | None, company: str, role: str,
                         facts: str = "") -> str:
    """The first message of the chat that answers a job's application questions: the job, my resume for it, and my
    personal facts. The questions follow one by one in the same chat."""
    resume = compact_resume(resume_json) if resume_json else "(not available)"
    description = job_description.strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0] + " …"
    text = (
        f"I am applying for the {role or 'role'} position at {company or 'this company'} right now. You are helping me "
        "fill in its application form: I will send you its questions one by one, and you answer each one as me.\n\n"
        f"JOB DESCRIPTION:\n{description}\n\n"
        f"MY RESUME FOR THIS JOB (JSON):\n{resume}\n"
    )
    if facts.strip():
        text += f"\nMY PERSONAL FACTS (true, from my own settings):\n{facts.strip()}\n"
    return text


def extract_marked_answer(text: str) -> str:
    """The text between the last START and the END after it; '' when the reply carried no usable answer.

    The question itself names the markers ("between the markers [[ANSWER_START]] and [[ANSWER_END]]"). When the page
    text still holds that question (the reply has not arrived, or the page reader fell back to the whole page), that
    phrase must not be read as an answer of "and"."""
    text = re.sub(re.escape(START) + r"\s*and\s*" + re.escape(END), " ", text or "")
    start = text.rfind(START)
    if start < 0:
        return ""
    body_from = start + len(START)
    end = text.find(END, body_from)
    if end < 0:
        return ""
    answer = text[body_from:end].strip()
    return "" if answer.lower() in _PLACEHOLDERS else answer


_REPLY_AFTER = ("no mention of being an ai.", "reply with only the answer", "reply with a single number only.")


def salvage_unmarked(raw: str, options: list[str], kind: str = "text") -> str:
    """A reply that left out the markers can still be an answer: a short reply naming exactly one of the form's options
    (or one number). ``raw`` is the end of the page, which holds our own question first, so only what follows it is read.
    '' when the reply does not settle on one option."""
    text = raw or ""
    low = text.lower()
    cut = max((low.rfind(m) + len(m) for m in _REPLY_AFTER if low.rfind(m) >= 0), default=-1)
    if cut < 0:
        return ""  # our question is not in view, so what is there could be an earlier answer
    reply = text[cut:].strip()
    fenced = extract_marked_answer(reply)
    if fenced:  # the chat did answer in the markers; the page reader only missed it
        return (match_option(fenced, options) or "") if options else fenced
    reply = re.sub(re.escape(START) + r".*?" + re.escape(END), " ", reply, flags=re.S)
    if not reply or len(reply) > 400:
        return ""
    if options:
        found = [o for o in options if _canon(o) and re.search(r"(?<!\w)" + re.escape(_canon(o)) + r"(?!\w)", _canon(reply))]
        found = [o for o in found if not any(o != other and _canon(o) in _canon(other) for other in found)]
        exact = [o for o in options if _canon(o) == _canon(reply)]
        return exact[0] if exact else found[0] if len(found) == 1 else ""
    if kind == "number":
        numbers = re.findall(r"\d+(?:\.\d+)?", reply)
        return numbers[0] if len(set(numbers)) == 1 else ""
    return ""


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def answer_after_prompt(page_text: str, prompt: str) -> str:
    """The fenced answer that follows OUR question on the page, '' when there is none yet.

    The page reader sometimes cannot pick the reply element out (a conversation whose last answer was the same word,
    a page that re-draws), yet the reply is plainly on the page after our message. The anchor is our own "Question:"
    line: every question ends with the same instructions, so only the question itself tells our message from an
    earlier one. Until our question is on the page, nothing is taken."""
    asked = re.search(r"Question: ([^\r\n]{8,60})", prompt or "")  # the first words of the question line
    if not asked:
        return ""
    page = _flat(page_text)
    where = page.rfind(_flat(asked.group(1)))
    return extract_marked_answer(page[where:]) if where >= 0 else ""


def count_marked_answers(text: str) -> int:
    """How many fenced answers the text holds (the marker names inside a question do not count)."""
    text = re.sub(re.escape(START) + r"\s*and\s*" + re.escape(END), " ", text or "")
    return len(re.findall(re.escape(START) + r".*?" + re.escape(END), text, re.S))


_MD_EMPHASIS = re.compile(r"(\*\*|__)(.+?)\1", re.S)
_MD_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", re.S)


def clean_answer(text: str, max_chars: int = 0) -> str:
    """Plain, form-ready text: no markdown, no dash tics, within the limit (cut at a sentence/word boundary)."""
    out = text.strip().replace("\r\n", "\n")
    out = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", out).strip()
    out = _MD_EMPHASIS.sub(r"\2", out)
    out = _MD_ITALIC.sub(r"\1", out)
    out = out.replace("`", "")
    out = re.sub(r"^#{1,6}\s+", "", out, flags=re.M)
    out = re.sub(r"\s+[—–]\s+", ", ", out)  # " — " reads as machine-written
    out = out.replace("—", ", ").replace("–", "-")
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    if len(out) > 1 and out[0] == out[-1] and out[0] in "\"'“”":
        out = out[1:-1].strip()
    out = out.strip("“”").strip()
    if max_chars and len(out) > max_chars:
        cut = out[:max_chars]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        if stop >= max_chars * 0.5:
            out = cut[: stop + 1]
        else:
            out = cut[: cut.rfind(" ")] if " " in cut else cut
        out = out.rstrip(" ,;:-")
    return out.strip()


_DASHES = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")  # hyphen, en dash, em dash, minus ...
_LESS = re.compile(r"\b(?:less than|fewer than|under|below)\s*")
_MORE = re.compile(r"\b(?:more than|greater than|over|above)\s*")


def _canon(text: str) -> str:
    """A choice's wording with the differences that do not change its meaning removed: case, spacing, the kind of
    dash ("3-5" against "3 en-dash 5"), "3 - 5", and "less than 3" against "< 3"."""
    out = _DASHES.sub("-", (text or "").strip().casefold())
    out = re.sub(r"\s*-\s*", "-", out)
    out = _LESS.sub("<", out)
    out = _MORE.sub(">", out)
    out = re.sub(r"([<>])\s+", r"\1", out)
    return re.sub(r"\s+", " ", out).strip()


def match_option(answer: str, options: list[str]) -> str:
    """The option the answer names (exact, then case/space/dash-insensitive, then contained), else ''."""
    if not options:
        return answer
    norm = _canon
    target = norm(answer)
    for opt in options:
        if norm(opt) == target:
            return opt
    hits = [opt for opt in options if norm(opt) and (norm(opt) in target or target in norm(opt))]
    return hits[0] if len(hits) == 1 else ""
