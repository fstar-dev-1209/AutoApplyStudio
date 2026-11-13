"""Discovery -> queue -> (finished resume) -> apply in a real Chrome -> recorded, with the pieces joined as in the app."""

from __future__ import annotations

import socket
from datetime import datetime, timezone

import pytest

pytest.importorskip("playwright")
from core import jobright  # noqa: E402
from core.apply_runner import ApplyRunner  # noqa: E402
from core.applications import ApplicationService  # noqa: E402
from core.browser_host import BrowserConfig, BrowserError, BrowserHost, find_browser  # noqa: E402
from core.discovery import DiscoveryService, Listing  # noqa: E402
from core.discovery_runner import DiscoveryRunner  # noqa: E402
from core.greenhouse import GreenhouseApplier  # noqa: E402
from core.models import JobStatus, QueueStore  # noqa: E402
from core.settings_store import AppSettings, ProfileSettings  # noqa: E402
from tests.greenhouse_fakes import Board  # noqa: E402

try:
    find_browser("chrome")
except BrowserError:  # pragma: no cover
    pytest.skip("Chrome is not installed", allow_module_level=True)


class Reply:
    def __init__(self, answer):
        self.answer, self.error, self.chat_url, self.raw = answer, "", "https://claude.ai/chat/11111111-2222-3333-4444-555555555555", ""


def test_a_job_found_on_jobright_ends_up_applied_to_and_recorded(tmp_path):
    board = Board().start()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    host = BrowserHost(BrowserConfig(profile_dir=tmp_path / "profile", headless=True, debug_port=port, mode="attach"))
    host.start()
    store = QueueStore(tmp_path / "studio.db")
    apps = ApplicationService(store.database)
    settings = AppSettings(
        apply_mode="auto", discovery_enabled=True,
        profile=ProfileSettings(name="Mary Ann van der Berg", email="mary@example.com", phone="(555) 010-0000",
                                location="Austin, TX", linkedin_url="https://linkedin.com/in/mary",
                                work_authorization="Yes", needs_sponsorship="No"),
        apply_facts={"city": "Austin", "state": "TX", "referral_source": "LinkedIn", "agree_consents": "Yes"})
    questions = []

    def ask_chat(prompt, **kw):
        questions.append((prompt, kw))
        return Reply("3-5 years" if "Snowflake" in prompt else "Built retrieval pipelines in production.")

    try:
        # 1. Jobright shows one new Greenhouse-powered posting (the company page embeds the form)
        link = board.url("/careers?gh_jid=1")
        posting = Listing(
            "jr-1", title="Backend Engineer", company="Acme", apply_url=link, posted_at="1 hour ago",
            source_url="https://jobright.ai/jobs/info/jr-1",
            description="Backend Engineer — Acme\n\nSummary\nBuild services.\n\nResponsibilities\n• Own the API")
        found = DiscoveryRunner(
            DiscoveryService(store), lambda: settings, lambda: host, pause=lambda lo, hi: None,
            read=lambda ctx, url, is_known, pause, debug_dir=None, max_age_hours=None: jobright.ReadResult(
                listings=[posting]),
            read_detail=lambda ctx, listing, pause=None: listing)
        report = found.run_once()
        assert report.queued == 1
        (job,) = store.jobs()
        assert job.url == link and job.source == "jobright"

        # 2. the existing pipeline writes the documents (stood in for here): the job becomes DONE with files
        folder = tmp_path / "Remote" / "Mary" / "Acme" / "Backend"
        folder.mkdir(parents=True)
        files = [folder / "Mary_Resume.pdf", folder / "Mary_Resume.docx", folder / "Mary_Cover_Letter.docx"]
        for f in files:
            f.write_bytes(b"x")
        job.generated_files = [str(f) for f in files]
        job.job_description = "Build RAG systems."
        job.resume_json = {"cover_letter": "Dear team", "company_name": "Acme"}
        job.chat_url = "https://claude.ai/chat/aaaaaaaa-2222-3333-4444-555555555555"
        job.completed_at = datetime.now(timezone.utc).isoformat()
        job.set_status(JobStatus.DONE, "Completed")
        store.update(job)

        # 3. the apply loop takes it, with the chat available for the open question
        runner = ApplyRunner(
            store, apps, lambda: settings, lambda: host, ask_chat=ask_chat, shots_dir=tmp_path / "shots",
            applier_factory=lambda **kw: GreenhouseApplier(pause=lambda lo, hi: None, **{k: v for k, v in kw.items() if k != "mail"}))
        settings.generation_mode = "claude_chat"
        outcome = runner.run_next()

        assert outcome.state == "submitted", (outcome.reason, outcome.needs)
        (sent,) = board.submissions
        assert sent["email"] == "mary@example.com" and sent["resume"] == "Mary_Resume.pdf"
        assert sent["question_5"] == "Built retrieval pipelines in production."
        assert sent["gender"] == "Decline To Self Identify"
        # the question went to the job's own conversation, with the job context ready in case it had to start a new one
        prompt, kw = questions[0]
        assert "RAG" in prompt and kw["chat_url"].endswith("aaaaaaaa-2222-3333-4444-555555555555")
        assert "Build RAG systems." in kw["context_prompt"]
        # recorded as applied, once, with the bot's attempt and the conversation kept on the job
        done = store.get(job.id)
        assert done.applied_at and done.chat_url == "https://claude.ai/chat/11111111-2222-3333-4444-555555555555"
        assert [a["state"] for a in runner.attempts.for_job(job.id)] == ["submitted"]
        assert runner.run_next() is None  # and it is not applied to a second time
        assert len(board.submissions) == 1
    finally:
        host.stop()
        board.stop()
        store.database.close()
