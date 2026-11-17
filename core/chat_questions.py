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
    "and concise, in the first person. Make it the best fit for this job description and for the resume JSON you "
    "generated for this job. Never invent an employer, title, date, degree, tool, metric or project that is not "
    "in that resume JSON."
)

_PLACEHOLDERS = {"", "...", "…", "your answer here", "answer", "<answer>", "the answer", "final answer"}


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


def build_question_prompt(question: Question, style_prompt: str = DEFAULT_ANSWER_PROMPT) -> str:
    if question.kind == "salary":
        from core.salary_answer import build_salary_prompt  # late: salary_answer imports this module

        return build_salary_prompt(question.extra, question.text, question.options or None, question.figure)
    lines = [
        style_prompt.strip() or DEFAULT_ANSWER_PROMPT,
        "",
        "This is a question from the application form for THIS job. Use the job description and the resume JSON "
        "from earlier in this conversation.",
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
    lines += ["", f"Question: {question.text.strip()}"]
    if question.options:
        lines.append("Choose exactly one of these options and reply with it word for word: "
                     + json.dumps(question.options, ensure_ascii=False))
    if question.hint:
        lines.append(question.hint.strip())
    if question.kind == "number":
        lines.append("Reply with a single number only.")
    if question.max_chars:
        lines.append(f"The answer must be at most {question.max_chars} characters including spaces.")
    lines += [
        "",
        f"Reply with ONLY the answer between the markers {START} and {END}, with nothing before or after them. "
        "Plain text only: no markdown, no quotation marks around it, no bullet points unless the question asks "
        "for a list, no mention of being an AI.",
    ]
    return "\n".join(lines)


def build_context_prompt(*, job_description: str, resume_json: dict | None, company: str, role: str,
                         facts: str = "") -> str:
    """Sent first when a job has no saved conversation (or it is gone), so a new chat has the same grounding."""
    resume = json.dumps(resume_json, ensure_ascii=False) if resume_json else "(not available)"
    text = (
        f"I am applying for the {role or 'role'} position at {company or 'this company'}. I will ask you the "
        "questions from the application form, one by one.\n\n"
        f"JOB DESCRIPTION:\n{job_description.strip()}\n\n"
        f"MY RESUME JSON FOR THIS JOB:\n{resume}\n"
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
