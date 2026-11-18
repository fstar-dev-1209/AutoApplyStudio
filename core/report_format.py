"""How the status messages look. Each kind of message has its own icon and heading so they can be told apart at a glance:

    🔎 JOB SEARCH      📄 RESUME      📨 APPLICATION      🙋 NEEDS YOU / ⚠️ alerts

Messages are written in the small HTML subset Telegram understands (<b>, <i>, <a>, <code>, <blockquote expandable>);
every piece of text that comes from a job or from you is escaped first. ``to_discord`` and ``to_plain`` turn the same
message into Discord markdown or plain text. Nothing here sends anything.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from core.application_record import Row, rows_from_plan
from core.discovery import IngestReport
from core.models import JobItem, JobStatus

RULE = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
MESSAGE_LIMIT = 3800  # a Telegram message holds 4096 characters; leave room for the markup
CAPTION_LIMIT = 1000  # a file's caption holds 1024
SOURCE_ICON = {"profile": "👤", "facts": "💾", "rule": "⚙️", "chat": "🤖", "page": "📄", "salary": "💰"}
LEGEND = "👤 profile · 💾 saved answer · ⚙️ rule · 💰 salary rule · 🤖 written by the chat (worth a look)"


@dataclass
class Message:
    text: str  # Telegram HTML
    document: tuple[str, bytes] | None = None  # (file name, bytes): then ``text`` is its caption


def esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def cut(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _job(job: JobItem) -> str:
    company, role = esc(job.company or "Company"), esc(job.role_title or "Role")
    return f"<b>{company}</b> · {role}"


def _link(job: JobItem) -> str:
    url = (job.origin_url or job.url or "").strip()
    return f'🔗 <a href="{html.escape(url, quote=True)}">Open the posting</a>' if url.startswith("http") else ""


def _lines(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


# ------------------------------------------------------------------ job search

def job_search(report: IngestReport) -> Message:
    head = "🔎 <b>JOB SEARCH</b>"
    listed = f"📥 <b>{report.listed}</b> listed · <b>{report.recent}</b> recent"
    if report.new == 0 and not report.expired:
        return Message(_lines(head, RULE, listed,
                              f"😴 Nothing new · {report.seen} already seen" if report.seen else "😴 Nothing new"))
    other = f"<b>{report.unsupported}</b> other site{'s' if report.unsupported != 1 else ''}"
    parts = [head, RULE, listed,
             f"🧩 <b>{report.greenhouse}</b> Greenhouse · {other}"
             + (f" · <b>{report.unresolved}</b> without a link yet" if report.unresolved else ""),
             f"✅ <b>{report.queued}</b> queued for resumes"]
    left_out = report.filtered + report.duplicate
    if left_out:
        parts.append(f"🚫 <b>{left_out}</b> left out")
        items = sorted(report.reasons.items(), key=lambda kv: -kv[1])
        for i, (kind, n) in enumerate(items):
            parts.append(f"   {'└' if i == len(items) - 1 else '├'} {n} · {esc(kind)}")
    if report.expired:
        parts.append(f"♻️ <b>{report.expired}</b> old job{'s' if report.expired != 1 else ''} dropped (not generated in time)")
    return Message(_lines(*parts))


# ------------------------------------------------------------------ resumes

def resume(job: JobItem) -> Message | None:
    if job.status == JobStatus.DONE:
        return Message(_lines("📄 <b>RESUME READY</b>", _job(job)))
    if job.status == JobStatus.FAILED:
        return Message(_lines("❌ <b>RESUME FAILED</b>", _job(job),
                              f"<i>{esc(cut(job.error or job.stage or '', 300))}</i>" if (job.error or job.stage) else ""))
    if job.status == JobStatus.SKIPPED:
        return Message(_lines("⏭ <b>RESUME SKIPPED</b>", _job(job),
                              f"<i>{esc(cut(job.skip_reason or '', 200))}</i>" if job.skip_reason else ""))
    return None


# ------------------------------------------------------------------ applications

def _answer_summary(rows: list[Row], blank: list[str]) -> str:
    answered = [r for r in rows if r.source != "attached"]
    parts = [f"📊 <b>{len(answered)}</b> answered"]
    for key, name in (("profile", "profile"), ("facts", "saved"), ("rule", "rule"), ("salary", "salary"), ("chat", "chat")):
        n = sum(1 for r in answered if r.source == key)
        if n:
            parts.append(f"{SOURCE_ICON[key]} {n} {name}")
    if blank:
        parts.append(f"⬜ {len(blank)} left blank")
    return " · ".join(parts)


def _answers_block(rows: list[Row], blank: list[str]) -> str:
    """The questions and answers, in an expandable quote. Your own details are kept out (see ``Row.private``)."""
    lines: list[str] = []
    attached = [r.answer.replace(" attached", "") for r in rows if r.source == "attached"]
    n = 0
    for row in rows:
        if row.source == "attached":
            continue
        n += 1
        lines.append(f"<b>{n}.</b> {esc(cut(row.question, 170))}")
        if row.private:
            lines.append("    🔒 <i>kept private</i>")
        else:
            answer = cut(row.answer, 700).replace("\n", "\n      ")
            lines.append(f"    {SOURCE_ICON.get(row.source, '•')} {esc(answer)}")
            if row.note:
                lines.append(f"       <i>{esc(cut(row.note, 160))}</i>")
    if attached:
        lines.append(f"📎 Attached: {esc(' + '.join(attached))}")
    if blank:
        shown = "; ".join(cut(b, 50) for b in blank[:8]) + (f"; +{len(blank) - 8} more" if len(blank) > 8 else "")
        lines.append(f"⬜ <i>Left blank (optional): {esc(shown)}</i>")
    lines.append("")
    lines.append(f"<i>{LEGEND}</i>")
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def applied(job: JobItem, plan: list[dict], full_text_for_file: str) -> Message:
    """One message: the result, and the questions and answers inside it. If they do not fit in a message, the same
    message goes out as a file with the result as its caption."""
    rows, blank = rows_from_plan(plan)
    head = _lines("✅ <b>APPLIED</b>", _job(job), _link(job), RULE, _answer_summary(rows, blank))
    body = _lines(head, "", "📝 <b>Questions and answers</b> <i>(tap to open)</i>", _answers_block(rows, blank))
    if len(body) <= MESSAGE_LIMIT:
        return Message(body)
    name = re.sub(r'[\\/:*?"<>|]+', " ", f"{job.company or 'Company'} - {job.role_title or 'Role'}").strip()[:80]
    caption = _lines(head, "", "📝 Questions and answers are in the file below.")
    return Message(cut_html_caption(caption), (f"{name} - answers.txt", full_text_for_file.encode("utf-8")))


def cut_html_caption(caption: str) -> str:
    """A caption is short by construction; this only guards the limit (dropping whole lines keeps the tags balanced)."""
    while len(caption) > CAPTION_LIMIT and "\n" in caption:
        caption = caption.rsplit("\n", 1)[0]
    return caption


def application_result(job: JobItem, state: str, reason: str, plan: list[dict]) -> Message | None:
    if state == "failed":
        return Message(_lines("❌ <b>APPLICATION FAILED</b>", _job(job), _link(job),
                              f"<i>{esc(cut(reason, 300))}</i>" if reason else "", "↻ It will be tried again later."))
    if state == "dismissed":
        return Message(_lines("⏭ <b>APPLICATION SKIPPED</b>", _job(job), _link(job),
                              f"<i>{esc(cut(reason, 300))}</i>" if reason else ""))
    if state == "expired":
        return Message(_lines("🚫 <b>POSTING CLOSED</b>", _job(job), _link(job)))
    if state == "dry_run":
        rows, blank = rows_from_plan(plan)
        return Message(_lines("🧪 <b>DRY RUN</b> · nothing was sent", _job(job), RULE, _answer_summary(rows, blank)))
    return None  # "needs you" and "human check" reach you as alerts


# ------------------------------------------------------------------ alerts (the app's own notices)

def alert(title: str, body: str = "") -> Message:
    title = (title or "").strip()
    if title.lower().startswith("needs you"):
        subject = title.split(":", 1)[1].strip() if ":" in title else ""
        return Message(_lines("🙋 <b>NEEDS YOU</b>", f"<b>{esc(subject)}</b>" if subject else "",
                              esc(cut(body, 600)) if body else ""))
    return Message(_lines(f"⚠️ <b>{esc(title)}</b>", esc(cut(body, 600)) if body else ""))


# ------------------------------------------------------------------ other renderings

_TAG = re.compile(r"</?(?:b|i|code|a|blockquote)(?: [^>]*)?>")


def to_plain(text: str) -> str:
    """The message with the markup removed (used when a service refuses the formatted one)."""
    text = re.sub(r'<a href="([^"]*)">([^<]*)</a>', r"\2 (\1)", text)
    return html.unescape(_TAG.sub("", text))


def to_discord(text: str) -> str:
    """Discord markdown: **bold**, *italic*, `code`, > quotes, [text](link)."""
    out = re.sub(r'<a href="([^"]*)">([^<]*)</a>', r"[\2](\1)", text)
    out = re.sub(r"<b>(.*?)</b>", r"**\1**", out, flags=re.S)
    out = re.sub(r"<i>(.*?)</i>", r"*\1*", out, flags=re.S)
    out = re.sub(r"<code>(.*?)</code>", r"`\1`", out, flags=re.S)
    out = re.sub(r"<blockquote[^>]*>(.*?)</blockquote>", lambda m: "\n".join("> " + ln for ln in m.group(1).splitlines()),
                 out, flags=re.S)
    return html.unescape(re.sub(r"</?[a-z]+[^>]*>", "", out))
