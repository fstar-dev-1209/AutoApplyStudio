"""The questions an application asked and the answers the bot gave, for you to read afterwards.

Built from the plan the form filler keeps for every application (one entry per field: what was decided, from where).
Two versions: the full record, which stays on this computer, and a shorter copy for Telegram that leaves out your
contact details and your demographic and legal answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.form_answers import COVER_FILE, OPTIONAL_REASON, RESUME_FILE, SENSITIVE
from core.models import JobItem

SOURCES = {
    "profile": "your profile",
    "facts": "your saved answers",
    "rule": "a built-in rule",
    "chat": "written by the chat (review this one)",
    "page": "already on the form",
    "salary": "the salary rule (the job's posted range, your minimum, ChatGPT's judgement)",
}
# Contact details and who you are: kept off Telegram. (Demographic and legal questions are SENSITIVE or listed here.)
_PRIVATE = re.compile(
    r"e-?mail|phone|mobile|telephone|linkedin|github|website|portfolio|address|street|zip|postal|\bcity\b|location|"
    r"first name|last name|full name|preferred name|legal name|"
    r"lgbt|queer|pronoun|non-?binary|ethnic|hispanic|latino|demographic|equal.opportunity|self.identif|"
    r"salary|compensation|expected pay",
    re.I)
PRIVATE_NOTE = "(not sent to Telegram: contact, demographic or pay answer; it is in the record on your computer)"
FILE_NAME = "Application answers.txt"


@dataclass
class Row:
    question: str
    answer: str
    source: str  # profile | facts | rule | chat | salary | page | optional | attached
    private: bool = False
    note: str = ""  # how the answer was reached (salary: the posted range and where in it)


def rows_from_plan(plan: list[dict]) -> tuple[list[Row], list[str]]:
    """(answered rows in form order, labels of optional questions left blank)."""
    answered: list[Row] = []
    blank: list[str] = []
    for entry in plan or []:
        label = str(entry.get("label") or "").strip()
        action, reason = str(entry.get("action") or ""), str(entry.get("reason") or "")
        if not label:
            continue
        if action == "skip":
            if reason == OPTIONAL_REASON:
                blank.append(label)
            continue  # "Already filled" and the like: nothing of ours to report
        if action != "fill":
            continue
        value = str(entry.get("value") or "")
        source = str(entry.get("source") or "")
        if value == RESUME_FILE:
            answered.append(Row(label, "Resume attached", "attached"))
        elif value == COVER_FILE:
            answered.append(Row(label, "Cover letter attached", "attached"))
        else:
            private = bool(SENSITIVE.search(label) or (source != "salary" and _PRIVATE.search(label))
                           or (source == "profile" and entry.get("kind") != "file"))
            answered.append(Row(label, value, source, private, reason if source == "salary" else ""))
    return answered, blank


def _counts(rows: list[Row]) -> str:
    parts = []
    for key, name in (("profile", "from your profile"), ("facts", "from your saved answers"),
                      ("rule", "by a built-in rule"), ("salary", "by the salary rule"), ("chat", "written by the chat")):
        n = sum(1 for r in rows if r.source == key)
        if n:
            parts.append(f"{n} {name}")
    return ", ".join(parts)


def summary_line(plan: list[dict]) -> str:
    rows, blank = rows_from_plan(plan)
    questions = [r for r in rows if r.source != "attached"]
    text = f"{len(questions)} answered"
    detail = _counts(questions)
    if detail:
        text += f" ({detail})"
    if blank:
        text += f", {len(blank)} optional left blank"
    return text


def _header(job: JobItem, when: datetime, confirmation: str) -> list[str]:
    stamp = when.astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    lines = [f"Application: {job.company or 'Company'} - {job.role_title or 'Role'}", f"Sent: {stamp}",
             f"Posting: {job.origin_url or job.url}"]
    if confirmation:
        lines.append(f"Confirmation: {confirmation}")
    return lines


def format_record(job: JobItem, plan: list[dict], when: datetime, confirmation: str = "", *, private: bool = True) -> str:
    """The text of the record. ``private=False`` is the Telegram copy: your own details are left out."""
    rows, blank = rows_from_plan(plan)
    lines = _header(job, when, confirmation)
    if not private:
        lines.append("(Contact details and demographic, legal and pay answers are left out of this copy.)")
    lines += ["", f"Questions answered: {summary_line(plan)}", ""]
    n = 0
    for row in rows:
        n += 1
        shown = PRIVATE_NOTE if (row.private and not private) else row.answer
        lines.append(f"{n}. {row.question}")
        for i, part in enumerate(shown.splitlines() or [""]):
            lines.append(("   Answer: " if i == 0 else "           ") + part)
        if row.source not in {"attached"}:
            lines.append(f"   From: {SOURCES.get(row.source, row.source or 'unknown')}")
        if row.note:
            lines.append(f"   Basis: {row.note}")
        lines.append("")
    if blank:
        lines.append(f"Optional questions left blank ({len(blank)}):")
        lines += [f"  - {label}" for label in blank]
    return "\n".join(lines).rstrip() + "\n"


def write_record(job: JobItem, plan: list[dict], when: datetime, confirmation: str, fallback_dir: Path) -> Path | None:
    """Save the full record next to the job's documents (or in ``fallback_dir``). None when it cannot be written."""
    folder = Path(job.output_path) if job.output_path else fallback_dir
    for target in (folder, fallback_dir):
        try:
            target.mkdir(parents=True, exist_ok=True)
            name = FILE_NAME if target == folder and job.output_path else f"{_safe(job.company)} - {_safe(job.role_title)} - answers.txt"
            path = target / name
            path.write_text(format_record(job, plan, when, confirmation, private=True), encoding="utf-8")
            return path
        except OSError:
            continue
    return None


def _safe(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', " ", text or "").strip()[:60] or "job"
