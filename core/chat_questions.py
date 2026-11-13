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


def build_question_prompt(question: Question, style_prompt: str = DEFAULT_ANSWER_PROMPT) -> str:
    lines = [
        style_prompt.strip() or DEFAULT_ANSWER_PROMPT,
        "",
        "This is a question from the application form for THIS job. Use the job description and the resume JSON "
        "from earlier in this conversation.",
        "",
        f"Question: {question.text.strip()}",
    ]
    if question.options:
        lines.append("Choose exactly one of these options and reply with it word for word: "
                     + json.dumps(question.options, ensure_ascii=False))
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


def build_context_prompt(*, job_description: str, resume_json: dict | None, company: str, role: str) -> str:
    """Sent first when a job has no saved conversation (or it is gone), so a new chat has the same grounding."""
    resume = json.dumps(resume_json, ensure_ascii=False) if resume_json else "(not available)"
    return (
        f"I am applying for the {role or 'role'} position at {company or 'this company'}. I will ask you the "
        "questions from the application form, one by one.\n\n"
        f"JOB DESCRIPTION:\n{job_description.strip()}\n\n"
        f"MY RESUME JSON FOR THIS JOB:\n{resume}\n"
    )


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


def match_option(answer: str, options: list[str]) -> str:
    """The option the answer names (exact, then case/space-insensitive, then contained), else ''."""
    if not options:
        return answer
    norm = lambda s: re.sub(r"\s+", " ", s.strip().casefold())  # noqa: E731
    target = norm(answer)
    for opt in options:
        if norm(opt) == target:
            return opt
    hits = [opt for opt in options if norm(opt) and (norm(opt) in target or target in norm(opt))]
    return hits[0] if len(hits) == 1 else ""
