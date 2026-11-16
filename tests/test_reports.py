"""Status messages: their look (one icon and heading per kind), what goes to Telegram, and what stays on this computer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from core import report_format as fmt
from core.application_record import PRIVATE_NOTE, format_record, rows_from_plan
from core.discovery import IngestReport
from core.models import JobItem, JobStatus
from core.notify import Notifier
from core.reports import Reporter

WHEN = datetime(2026, 11, 16, 7, 44, tzinfo=timezone.utc)


def make_notifier(handler=lambda r: httpx.Response(200, json={"ok": True}), **kw):
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request):
        sent.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    return Notifier(client=client, background=False, telegram=lambda: ("123:ABC", "555"), **kw), sent


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# ------------------------------------------------------------------ the notifier's report channel

def test_a_report_goes_to_telegram_as_formatted_text_without_a_link_preview_or_an_in_app_alert():
    shown = []
    n, sent = make_notifier(in_app=lambda t, b: shown.append(t))
    assert n.report("🔎 <b>JOB SEARCH</b>") and n.report("🔎 <b>JOB SEARCH</b>")
    assert shown == []  # information, not an alert: no toast; and never held back as a repeat
    assert [str(r.url) for r in sent] == ["https://api.telegram.org/bot123:ABC/sendMessage"] * 2
    data = form(sent[0])
    assert data["chat_id"] == "555" and data["parse_mode"] == "HTML" and data["text"] == "🔎 <b>JOB SEARCH</b>"
    assert '"is_disabled": true' in data["link_preview_options"]  # a link must not unfold into a big card


def test_a_message_can_carry_a_file_as_one_message_with_its_text_as_the_caption():
    n, sent = make_notifier(discord_webhook=lambda: "https://discord.com/api/webhooks/1/abc")
    n.report("✅ <b>APPLIED</b>\nAcme", ("answers.txt", "1. Question\n   Answer: yes\n".encode()))
    urls = [str(r.url) for r in sent]
    assert urls.count("https://api.telegram.org/bot123:ABC/sendDocument") == 1
    assert not any(u.endswith("sendMessage") for u in urls)  # not a message plus a file: one message
    doc = next(r for r in sent if str(r.url).endswith("sendDocument"))
    assert b'filename="answers.txt"' in doc.content and b"Answer: yes" in doc.content
    assert b"parse_mode" in doc.content and b"<b>APPLIED</b>" in doc.content
    discord = next(r for r in sent if "discord" in str(r.url))
    assert b"**APPLIED**" in discord.content and b"answers.txt" not in discord.content  # markdown for Discord


def test_when_telegram_cannot_read_the_formatting_the_plain_text_is_sent_instead():
    def handler(request):
        if "parse_mode" in request.content.decode():
            return httpx.Response(400, json={"ok": False, "description": "can't parse entities"})
        return httpx.Response(200, json={"ok": True})

    n, sent = make_notifier(handler)
    n.report('🙋 <b>NEEDS YOU</b>\n<a href="https://x.example/1">Open</a> &amp; go')
    assert len(sent) == 2 and not n.failures  # the first refusal is not a failure: the message got through
    plain = form(sent[1])
    assert "parse_mode" not in plain and "<b>" not in plain["text"]
    assert "NEEDS YOU" in plain["text"] and "Open (https://x.example/1) & go" in plain["text"]


def test_nothing_is_reported_when_nowhere_is_set_up_and_failures_hide_the_token():
    bare = Notifier(telegram=lambda: ("", ""), background=False)
    assert bare.report("x") is False and not bare.can_report()

    def boom(request):
        raise httpx.ConnectError("no route")

    n, _ = make_notifier(boom)
    assert n.report("x") is True  # never raises
    assert n.failures and all("123:ABC" not in f for f in n.failures)


def test_telegram_asking_us_to_slow_down_is_answered_with_one_retry():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})
        return httpx.Response(200, json={"ok": True})

    n, sent = make_notifier(handler)
    n.report("hello")
    assert len(sent) == 2 and not n.failures


def test_a_message_too_long_for_telegram_is_cut_as_plain_text_never_in_the_middle_of_a_tag():
    n, sent = make_notifier()
    n.report("<b>" + "x" * 9000 + "</b>")
    data = form(sent[0])
    assert len(data["text"]) <= 3900 and "<b>" not in data["text"] and "parse_mode" not in data


def test_the_apps_own_alerts_use_the_same_look():
    n, sent = make_notifier()
    n.send("Needs you: Acme - Platform Engineer", "Which office?")
    text = form(sent[0])["text"]
    assert text.startswith("🙋 <b>NEEDS YOU</b>") and "<b>Acme - Platform Engineer</b>" in text and "Which office?" in text
    n.send("Verification needs you", "A captcha <appeared>")
    assert "⚠️ <b>Verification needs you</b>" in form(sent[1])["text"] and "&lt;appeared&gt;" in form(sent[1])["text"]


# ------------------------------------------------------------------ the messages

def reporter(tmp_path, enabled=True):
    notifier, sent = make_notifier()
    return Reporter(notifier, lambda: enabled, tmp_path, now=lambda: WHEN), sent


def texts(sent):
    return [form(r)["text"] for r in sent if str(r.url).endswith("sendMessage")]


def job(tmp_path, **kw):
    j = JobItem.create("https://job-boards.greenhouse.io/acme/jobs/1")
    j.company, j.role_title, j.output_path = "Acme", "Platform Engineer", str(tmp_path / "Acme" / "Platform Engineer")
    for key, value in kw.items():
        setattr(j, key, value)
    return j


def test_the_job_search_message_says_what_was_found_and_why_jobs_were_left_out(tmp_path):
    r, sent = reporter(tmp_path)
    r.discovery(IngestReport(queued=20, unsupported=14, duplicate=1, filtered=9, capped=5, expired=4, listed=87, recent=40,
                             reasons={"company already applied to": 3, "over this hour's limit": 5,
                                      "another job at the company is queued": 1}))
    (text,) = texts(sent)
    assert text.startswith("🔎 <b>JOB SEARCH</b>")
    assert "📥 <b>87</b> listed · <b>40</b> recent" in text
    assert "🧩 <b>30</b> Greenhouse · <b>14</b> other sites" in text and "✅ <b>20</b> queued for resumes" in text
    assert "🚫 <b>10</b> left out" in text and "├ 5 · over this hour's limit" in text
    assert "└ 1 · another job at the company is queued" in text
    assert "♻️ <b>4</b> old jobs dropped" in text


def test_a_quiet_search_is_one_short_line(tmp_path):
    r, sent = reporter(tmp_path)
    r.discovery(IngestReport(seen=12, listed=30, recent=12))
    (text,) = texts(sent)
    assert "😴 Nothing new · 12 already seen" in text and "queued" not in text


def test_each_kind_of_message_has_its_own_icon_and_heading(tmp_path):
    r, sent = reporter(tmp_path)
    r.discovery(IngestReport(queued=1, listed=5, recent=5))
    r.resume(job(tmp_path, status=JobStatus.DONE))
    r.resume(job(tmp_path, status=JobStatus.FAILED, error="ChatGPT cleared the composer but no reply started"))
    r.resume(job(tmp_path, status=JobStatus.SKIPPED, skip_reason="Company folder already exists: C:/x"))
    r.application(job(tmp_path), "failed", "TimeoutError: page", [], "", False)
    r.application(job(tmp_path), "expired", "gone", [], "", False)
    r.application(job(tmp_path), "dry_run", "", [], "", False)
    heads = [t.splitlines()[0] for t in texts(sent)]
    assert heads == ["🔎 <b>JOB SEARCH</b>", "📄 <b>RESUME READY</b>", "❌ <b>RESUME FAILED</b>", "⏭ <b>RESUME SKIPPED</b>",
                     "❌ <b>APPLICATION FAILED</b>", "🚫 <b>POSTING CLOSED</b>", "🧪 <b>DRY RUN</b> · nothing was sent"]
    ready, failed, skipped = texts(sent)[1:4]
    assert "<b>Acme</b> · Platform Engineer" in ready
    assert "no reply started" in failed and "Company folder already exists" in skipped


def test_text_from_a_job_is_escaped_so_it_cannot_break_or_inject_formatting(tmp_path):
    r, sent = reporter(tmp_path)
    r.resume(job(tmp_path, status=JobStatus.FAILED, error="<b>bad</b> & <script>", company="R&D <Labs>"))
    (text,) = texts(sent)
    assert "<script>" not in text and "&lt;script&gt;" in text and "R&amp;D &lt;Labs&gt;" in text


PLAN = [
    {"label": "First Name", "kind": "text", "action": "fill", "source": "profile", "required": True, "value": "Christopher"},
    {"label": "Email", "kind": "text", "action": "fill", "source": "profile", "required": True, "value": "chris@example.com"},
    {"label": "Phone", "kind": "text", "action": "fill", "source": "profile", "required": True, "value": "(209) 555-0100"},
    {"label": "Resume/CV", "kind": "file", "action": "fill", "source": "profile", "required": True, "value": "@resume"},
    {"label": "Cover Letter", "kind": "file", "action": "fill", "source": "profile", "required": False, "value": "@cover"},
    {"label": "What is your minimum required base salary?", "kind": "text", "action": "fill", "source": "facts",
     "required": True, "value": "$140,000"},
    {"label": "How did you hear about this job?", "kind": "select", "action": "fill", "source": "facts", "required": True,
     "value": "Indeed"},
    {"label": "Why do you want this role?", "kind": "textarea", "action": "fill", "source": "chat", "required": True,
     "value": "I like building reliable platforms.\nSecond <line> & more."},
    {"label": "Gender", "kind": "select", "action": "fill", "source": "facts", "required": True, "value": "Male"},
    {"label": "Do you identify as a member of the LGBTQIA+ community?", "kind": "select", "action": "fill",
     "source": "facts", "required": True, "value": "I prefer not to say"},
    {"label": "LinkedIn Profile", "kind": "text", "action": "skip", "source": "", "required": False,
     "value": "", "reason": "Optional: left blank"},
    {"label": "Pronouns", "kind": "text", "action": "skip", "source": "", "required": False, "value": "",
     "reason": "Optional: left blank"},
    {"label": "Country", "kind": "select", "action": "skip", "source": "", "required": True, "value": "",
     "reason": "Already filled"},
]


def test_rows_keep_the_order_the_source_and_what_was_left_blank():
    rows, blank = rows_from_plan(PLAN)
    assert [r.question for r in rows][:4] == ["First Name", "Email", "Phone", "Resume/CV"]
    assert next(r for r in rows if r.question == "Resume/CV").answer == "Resume attached"
    assert next(r for r in rows if r.question.startswith("Why do you")).source == "chat"
    assert blank == ["LinkedIn Profile", "Pronouns"]


def test_the_full_record_has_every_answer_and_the_file_copy_leaves_out_your_own_details(tmp_path):
    j = job(tmp_path)
    full = format_record(j, PLAN, WHEN, "Thank you for applying", private=True)
    assert "chris@example.com" in full and "(209) 555-0100" in full and "$140,000" in full and "Male" in full
    assert "From: written by the chat (review this one)" in full and "Optional questions left blank (2):" in full
    short = format_record(j, PLAN, WHEN, "Thank you for applying", private=False)
    for secret in ("chris@example.com", "(209) 555-0100", "$140,000", "Christopher", "Male", "prefer not to say"):
        assert secret not in short, secret
    assert short.count(PRIVATE_NOTE) >= 6 and "I like building reliable platforms." in short


def test_an_applied_message_holds_the_result_the_summary_and_the_answers_in_one_message(tmp_path):
    r, sent = reporter(tmp_path)
    j = job(tmp_path)
    r.application(j, "submitted", "Application received.", PLAN, "Thank you for applying", True)
    assert len(sent) == 1  # one message, no separate file
    (text,) = texts(sent)
    lines = text.splitlines()
    assert lines[0] == "✅ <b>APPLIED</b>" and lines[1] == "<b>Acme</b> · Platform Engineer"
    assert '<a href="https://job-boards.greenhouse.io/acme/jobs/1">Open the posting</a>' in text
    assert "📊 <b>8</b> answered · 👤 3 profile · 💾 4 saved · 🤖 1 chat · ⬜ 2 left blank" in text
    assert "<blockquote expandable>" in text and text.rstrip().endswith("</blockquote>")
    # the answers, with the source of each; your own details are not in the message
    assert "💾 Indeed" in text and "🤖 I like building reliable platforms." in text
    assert "Second &lt;line&gt; &amp; more." in text and "📎 Attached: Resume + Cover letter" in text
    assert "⬜ <i>Left blank (optional): LinkedIn Profile; Pronouns</i>" in text
    for secret in ("chris@example.com", "(209) 555-0100", "$140,000", "Christopher", "Male", "prefer not to say"):
        assert secret not in text, secret
    assert text.count("🔒") >= 6
    # the complete record, with everything, is written next to the job's documents
    record = Path(j.output_path) / "Application answers.txt"
    assert record.is_file() and "chris@example.com" in record.read_text(encoding="utf-8")


def test_answers_that_do_not_fit_in_a_message_go_out_as_a_file_with_the_result_as_its_caption(tmp_path):
    r, sent = reporter(tmp_path)
    many = PLAN + [{"label": f"Essay question number {i}?", "kind": "textarea", "action": "fill", "source": "chat",
                    "required": True, "value": "A long considered answer. " * 30} for i in range(12)]
    r.application(job(tmp_path), "submitted", "", many, "", True)
    assert len(sent) == 1 and str(sent[0].url).endswith("sendDocument")  # still one message
    body = sent[0].content.decode("utf-8", "replace")
    assert "✅ <b>APPLIED</b>" in body and "Questions and answers are in the file below." in body
    assert "Acme - Platform Engineer - answers.txt" in body and "Essay question number 11?" in body
    assert "chris@example.com" not in body  # the file is the copy without your own details


def test_the_record_is_kept_elsewhere_when_the_job_folder_cannot_be_written(tmp_path):
    r, sent = reporter(tmp_path)
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where a folder should be")
    j = job(tmp_path, output_path=str(blocker / "sub"))
    r.application(j, "submitted", "", PLAN, "", True)
    assert (tmp_path / "answers" / "Acme - Platform Engineer - answers.txt").is_file()


def test_failed_closed_and_dry_run_results_come_without_answers_and_needs_you_is_left_to_the_alerts(tmp_path):
    r, sent = reporter(tmp_path)
    j = job(tmp_path)
    r.application(j, "failed", "TimeoutError: page", PLAN, "", False)
    r.application(j, "needs_you", "Which office?", PLAN, "", False)
    r.application(j, "human_check", "captcha", PLAN, "", False)
    (failed,) = texts(sent)
    assert "TimeoutError" in failed and "↻ It will be tried again later." in failed
    assert "<blockquote" not in failed and not (Path(j.output_path) / "Application answers.txt").exists()


def test_reports_can_be_switched_off_and_never_break_the_work(tmp_path):
    r, sent = reporter(tmp_path, enabled=False)
    r.discovery(IngestReport(queued=1))
    r.resume(job(tmp_path, status=JobStatus.DONE))
    assert sent == []

    def explode(*a, **k):
        raise RuntimeError("boom")

    broken, _ = reporter(tmp_path)
    broken._notifier.report = explode  # type: ignore[method-assign]
    broken.discovery(IngestReport(queued=1))  # nothing raised
    broken.resume(job(tmp_path, status=JobStatus.DONE))


def test_discord_gets_the_same_message_as_markdown():
    text = fmt.applied(JobItem(id="1", url="https://x.example/j", company="Acme", role_title="SRE"),
                       [{"label": "Q?", "kind": "text", "action": "fill", "source": "chat", "value": "A <b> & c"}], "FULL").text
    md = fmt.to_discord(text)
    assert md.startswith("✅ **APPLIED**") and "[Open the posting](https://x.example/j)" in md
    assert "> **1.** Q?" in md and "A <b> & c" in md and "<blockquote" not in md
