"""Queue engine: unchanged generation pipeline + duplicate-company policy."""

import json
from pathlib import Path

import pytest

from core.applications import AppFilter, ApplicationService
from core.models import JobItem, JobStatus, QueueStore
from core.queue_engine import QueueEngine
from core.settings_store import SettingsStore

SAMPLE = Path(__file__).resolve().parent.parent / "scripts" / "_sample_peraton.json"


@pytest.fixture
def env(tmp_path):
    settings = SettingsStore(tmp_path / "settings.json")
    settings.settings.output_root = str(tmp_path / "Remote")
    settings.settings.request_delay_seconds = 0
    store = QueueStore(tmp_path / "studio.db")
    calls = []

    def fake_claude(prompt, meta, _settings):
        calls.append(meta)
        return json.loads(SAMPLE.read_text(encoding="utf-8"))

    engine = QueueEngine(store, settings, claude_chat_generate=fake_claude)
    yield engine, store, settings, tmp_path, calls
    store.database.close()


def queued_job(store, url="https://jobs.example.com/peraton/1", company="Peraton", role="Senior Site Reliability Engineer",
               override=False):
    job = JobItem.create(url)
    job.company, job.role_title = company, role
    job.job_description = "We need an SRE."
    job.duplicate_override = override
    store.add_many([job])
    return job.id


def run(engine, store, job_id):
    engine._process_job(store.get(job_id))
    engine._record_result(job_id)
    return store.get(job_id)


def test_generation_exports_files_and_is_not_marked_applied(env):
    engine, store, settings, tmp, _ = env
    job = run(engine, store, queued_job(store))
    assert job.status == JobStatus.DONE, job.error
    exts = sorted(Path(p).suffix for p in job.generated_files)
    assert exts == [".docx", ".docx", ".pdf"]  # resume DOCX + PDF + cover-letter DOCX
    assert all(Path(p).is_file() for p in job.generated_files)
    folder = Path(job.output_path)
    # existing convention: <root>/<Name>/<Company>/<Role Title>/
    assert folder.parent.name == "Peraton" and folder.parent.parent.parent == tmp / "Remote"
    assert (job.app_stage, job.applied_at) == ("not_applied", "")
    types = [e.type for e in store.database.effective_events(job.id)]
    assert types == ["imported", "docs_generated"]
    svc = ApplicationService(store.database)
    assert [r.job.id for r in svc.list_applications()] == [job.id]  # shows up in Applications
    assert svc.list_applications(AppFilter(applied=True)) == []


def test_existing_company_folder_is_skipped_by_default(env):
    engine, store, settings, tmp, calls = env
    first = run(engine, store, queued_job(store))
    assert first.status == JobStatus.DONE
    second = run(engine, store, queued_job(store, "https://jobs.example.com/peraton/2", role="Platform Engineer"))
    assert second.status == JobStatus.SKIPPED and "Company folder already exists" in second.skip_reason
    assert len(calls) == 1  # the duplicate never reached Claude
    assert [e.type for e in store.database.effective_events(second.id)][-1] == "skipped_duplicate"
    assert ApplicationService(store.database).list_applications() and len(
        ApplicationService(store.database).list_applications()) == 1


def test_explicit_override_tracks_another_role_without_overwriting(env):
    engine, store, settings, tmp, calls = env
    first = run(engine, store, queued_job(store))
    other = run(engine, store, queued_job(store, "https://jobs.example.com/peraton/2", override=True))
    assert other.status == JobStatus.DONE, other.error
    assert len(calls) == 2
    assert Path(other.output_path) != Path(first.output_path)  # same role title -> new folder, old files intact
    assert all(Path(p).is_file() for p in first.generated_files + other.generated_files)
    assert len(ApplicationService(store.database).list_applications()) == 2


def test_override_is_per_job_and_does_not_change_the_default(env):
    engine, store, *_ = env
    run(engine, store, queued_job(store))
    plain = run(engine, store, queued_job(store, "https://jobs.example.com/peraton/3"))
    assert plain.status == JobStatus.SKIPPED
    svc = ApplicationService(store.database)
    svc.set_duplicate_override(plain.id, True)
    assert store.get(plain.id).duplicate_override is True
    retry = store.get(plain.id)
    retry.status = JobStatus.QUEUED
    store.update(retry)
    assert run(engine, store, plain.id).status == JobStatus.DONE


def test_a_usage_limit_is_left_for_you_instead_of_being_retried(env):
    from core.claude_client import ChatPageBlocked

    engine, store, settings, tmp, _ = env
    settings.settings.auto_retries = 3
    engine._claude_chat_generate = lambda *a: (_ for _ in ()).throw(
        ChatPageBlocked("ChatGPT is showing a usage limit. This job was not sent on."))
    jid = queued_job(store)
    engine._process_job(store.get(jid))
    engine._maybe_auto_retry(jid)
    job = store.get(jid)
    assert job.status == JobStatus.FAILED and "usage limit" in job.error


def test_a_normal_generation_failure_still_retries_when_retries_are_on(env):
    engine, store, settings, tmp, _ = env
    settings.settings.auto_retries = 1
    engine._claude_chat_generate = lambda *a: (_ for _ in ()).throw(RuntimeError("timed out"))
    jid = queued_job(store, url="https://jobs.example.com/retry/1")
    engine._process_job(store.get(jid))
    engine._maybe_auto_retry(jid)
    assert store.get(jid).status == JobStatus.QUEUED


def test_failed_generation_is_recorded_but_not_an_application(env):
    engine, store, settings, tmp, _ = env
    engine._claude_chat_generate = lambda *a: (_ for _ in ()).throw(RuntimeError("claude down"))
    job = run(engine, store, queued_job(store))
    assert job.status == JobStatus.FAILED
    assert [e.type for e in store.database.effective_events(job.id)][-1] == "generation_failed"
    assert ApplicationService(store.database).list_applications() == []


def _applied_job(store, company, role="Platform Engineer"):
    job = JobItem.create("https://old.example/applied")
    job.company, job.role_title = company, role
    store.add(job)
    ApplicationService(store.database).mark_applied(job.id)
    return job.id


def test_company_comes_from_the_resume_and_applied_company_is_skipped(env):
    engine, store, settings, tmp, calls = env
    _applied_job(store, "PERATON, Inc.")  # the resume says "Peraton"
    jid = queued_job(store, company="", role="")  # unknown until the resume is generated
    job = run(engine, store, jid)
    assert job.status == JobStatus.SKIPPED and "Already applied to PERATON, Inc." in job.skip_reason
    assert job.company == "Peraton" and job.role_title  # filled from the generated resume
    assert job.resume_json and not job.generated_files  # nothing exported


def test_applied_company_check_is_overridable_and_ignores_unapplied_jobs(env):
    engine, store, settings, tmp, _ = env
    queued_other = JobItem.create("https://q.example/1")
    queued_other.company = "Peraton"  # queued, never applied: must not block
    store.add(queued_other)
    assert run(engine, store, queued_job(store, url="https://q.example/2")).status == JobStatus.DONE

    _applied_job(store, "Peraton")
    over = run(engine, store, queued_job(store, url="https://q.example/3", override=True))
    assert over.status == JobStatus.DONE, over.error


def test_nothing_is_written_to_disk_until_generation_has_finished(tmp_path):
    settings = SettingsStore(tmp_path / "settings.json")
    root = tmp_path / "Remote"
    settings.settings.output_root = str(root)
    settings.settings.request_delay_seconds = 0
    store = QueueStore(tmp_path / "studio.db")
    seen = []

    def fake_claude(prompt, meta, _settings):
        seen.append(list(root.rglob("*")) if root.exists() else [])  # snapshot while Claude is "working"
        return json.loads(SAMPLE.read_text(encoding="utf-8"))

    engine = QueueEngine(store, settings, claude_chat_generate=fake_claude)
    job = JobItem.create("https://careers.example.com/jobs/9")  # no company known yet
    job.job_description = "We need an SRE."
    store.add_many([job])
    done = run(engine, store, job.id)
    assert seen == [[]], seen  # no folder or file existed during generation
    assert done.status == JobStatus.DONE and Path(done.output_path).parent.name == "Peraton"
    store.database.close()


def test_the_chat_conversation_is_saved_on_the_job_even_when_generation_fails(env):
    engine, store, settings, tmp, _ = env
    url = "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"

    def chat_that_fills_the_address(prompt, meta, _settings):
        meta["chat_url"], meta["chat_provider"] = url, "chatgpt"
        return json.loads(SAMPLE.read_text(encoding="utf-8"))

    engine._claude_chat_generate = chat_that_fills_the_address
    job = run(engine, store, queued_job(store))
    assert (job.status, job.chat_url, job.chat_provider) == (JobStatus.DONE, url, "chatgpt")
    assert store.get(job.id).chat_url == url  # persisted, not just on the in-memory object

    def failing_chat(prompt, meta, _settings):
        meta["chat_url"], meta["chat_provider"] = url, "chatgpt"
        raise RuntimeError("the reply never came")

    engine._claude_chat_generate = failing_chat
    failed = run(engine, store, queued_job(store, url="https://jobs.example.com/other/2", company="Other"))
    assert failed.status == JobStatus.FAILED and failed.chat_url == url  # a retry can reuse the conversation


def test_a_resume_written_for_another_company_is_discarded_and_never_renames_the_job(env):
    engine, store, settings, tmp, calls = env
    # the chat page was left on another job's reply: the resume it returns is for Peraton, this job is at GitLab
    job = run(engine, store, queued_job(store, url="https://boards.greenhouse.io/prolific/jobs/1", company="Prolific",
                                        role="Cardiologist"))
    assert job.status == JobStatus.FAILED and job.failed_stage == "generation"
    assert "Peraton" in job.error and "Prolific" in job.error
    assert job.resume_json is None and job.company == "Prolific" and job.role_title == "Cardiologist"
    assert job.claude_reuse_tab is False, "the retry must start a new chat, not read what is on screen"
    assert not job.generated_files and not (tmp / "Remote").exists()


def test_the_same_company_under_another_spelling_is_accepted(env):
    engine, store, settings, tmp, calls = env
    job = run(engine, store, queued_job(store, company="Peraton Corp."))
    assert job.status == JobStatus.DONE, job.error


def test_the_jobs_own_conversation_address_is_offered_to_a_retry(env):
    engine, store, settings, tmp, calls = env
    job = queued_job(store)
    stored = store.get(job)
    stored.chat_url = "https://claude.ai/chat/aaaaaaaa-2222-3333-4444-555555555555"
    store.update(stored)
    run(engine, store, job)
    assert calls[-1]["saved_chat_url"].endswith("aaaaaaaa-2222-3333-4444-555555555555")


def _job_without_text(store, company, role, url="https://job-boards.greenhouse.io/embed/job_app?for=planet&token=1"):
    job = JobItem.create(url)
    job.company, job.role_title = company, role  # as Jobright gave them; no description yet, so the page is scraped
    store.add_many([job])
    return job.id


def test_a_page_scrape_never_renames_a_job_whose_company_is_already_known(env, monkeypatch):
    from core import queue_engine
    from core.scraper import ScrapeResult

    engine, store, settings, tmp, calls = env
    monkeypatch.setattr(queue_engine, "scrape_job_url", lambda *a, **k: ScrapeResult(
        ok=True, url="u", origin_url="u", company="Embed", role_title="Wrong Title", job_description="We need an SRE. " * 20,
        source="Greenhouse"))
    jid = _job_without_text(store, "Peraton", "Senior Site Reliability Engineer")
    job = run(engine, store, jid)
    assert job.status == JobStatus.DONE, job.error
    assert job.source == "Greenhouse" and job.job_description.startswith("We need an SRE")  # what was missing was filled
    assert job.company == "Peraton" and job.role_title == "Senior Site Reliability Engineer"


def test_a_scrape_fills_in_a_company_that_was_missing(env, monkeypatch):
    from core import queue_engine
    from core.scraper import ScrapeResult

    engine, store, settings, tmp, calls = env
    monkeypatch.setattr(queue_engine, "scrape_job_url", lambda *a, **k: ScrapeResult(
        ok=True, url="u", origin_url="u", company="Peraton", role_title="SRE", job_description="We need an SRE. " * 20))
    job = run(engine, store, _job_without_text(store, "", ""))
    assert job.status == JobStatus.DONE, job.error
    assert job.company == "Peraton"


def test_a_jobright_job_whose_company_was_overwritten_gets_its_own_back_before_generating(env):
    from core.discovery import DiscoveryService, Listing

    engine, store, settings, tmp, calls = env
    report = DiscoveryService(store).ingest("jobright", [Listing(
        "jr-1", title="Senior Site Reliability Engineer", company="Peraton",
        apply_url="https://boards.greenhouse.io/peraton/jobs/55")])
    jid = report.queued_job_ids[0]
    broken = store.get(jid)
    broken.company, broken.role_title = "Embed", "Something Else"  # what the old scrape / a wrong resume did
    broken.job_description = "We need an SRE."
    store.update(broken)

    job = run(engine, store, jid)
    assert job.status == JobStatus.DONE, job.error
    assert (job.company, job.role_title) == ("Peraton", "Senior Site Reliability Engineer")
    assert any("restored from Jobright" in line for line in job.logs)


def test_a_junk_embed_company_without_a_jobright_record_is_cleared(env):
    engine, store, settings, tmp, calls = env
    jid = queued_job(store, company="Embed")
    job = run(engine, store, jid)
    assert job.company == "Peraton"  # the resume names the real company once the junk is out of the way


JUNK_DESCRIPTIONS = [
    "<div class=\"content-intro\"><p>Empower Pharmacy is great</p></div><ul><li>Build models</li></ul>",
    "Data Scientist\nUS Remote\n" + "We build things. " * 30 + "\nApply for this job\nFirst Name\n*\nLast Name\nSubmit application",
    "A real posting " * 30 + "\nVoluntary Self-Identification\nGender\nSelect...",
]


@pytest.mark.parametrize("junk", JUNK_DESCRIPTIONS)
def test_a_saved_description_that_is_page_debris_is_thrown_away_and_the_page_read_again(env, monkeypatch, junk):
    from core import queue_engine
    from core.scraper import ScrapeResult

    engine, store, settings, tmp, calls = env
    monkeypatch.setattr(queue_engine, "scrape_job_url", lambda *a, **k: ScrapeResult(
        ok=True, url="u", origin_url="u", company="Peraton", role_title="SRE", job_description="Clean posting text. " * 20))
    jid = queued_job(store)
    stored = store.get(jid)
    stored.job_description = junk
    store.update(stored)
    job = run(engine, store, jid)
    assert job.status == JobStatus.DONE, job.error
    assert job.job_description.startswith("Clean posting text.")
    assert any("page debris" in line for line in job.logs)


def test_a_good_saved_description_is_never_replaced(env, monkeypatch):
    from core import queue_engine

    engine, store, settings, tmp, calls = env
    monkeypatch.setattr(queue_engine, "scrape_job_url", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not scrape")))
    job = run(engine, store, queued_job(store))  # its description is the plain "We need an SRE."
    assert job.status == JobStatus.DONE and job.job_description == "We need an SRE."


def test_a_jobright_posting_is_kept_even_when_it_is_shorter_than_the_employer_page(env, monkeypatch):
    from core import queue_engine

    engine, store, settings, tmp, calls = env
    monkeypatch.setattr(queue_engine, "scrape_job_url", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not scrape")))
    jid = _job_without_text(store, "Peraton", "Senior Site Reliability Engineer",
                            url="https://boards.greenhouse.io/peraton/jobs/1")
    text = ("Senior Site Reliability Engineer — Peraton\n\nSummary\nBuild the APIs.\n\n"
            "Responsibilities\n• Design services")
    stub = store.get(jid)
    stub.job_description = text
    store.update(stub)
    job = run(engine, store, jid)
    assert job.status == JobStatus.DONE, job.error
    assert job.job_description == text


def test_a_short_summary_for_a_posting_that_can_be_read_in_full_is_replaced_by_the_full_text(env, monkeypatch):
    from core import queue_engine
    from core.scraper import ScrapeResult

    engine, store, settings, tmp, calls = env
    monkeypatch.setattr(queue_engine, "scrape_job_url", lambda *a, **k: ScrapeResult(
        ok=True, url="u", origin_url="u", company="Peraton", role_title="SRE", job_description="The full posting. " * 120))
    jid = _job_without_text(store, "Peraton", "SRE", url="https://job-boards.greenhouse.io/embed/job_app?for=peraton&token=1")
    stub = store.get(jid)
    stub.job_description = "A 400 character summary from an aggregator. " * 9  # 405 characters
    store.update(stub)
    job = run(engine, store, jid)
    assert job.status == JobStatus.DONE, job.error
    assert job.job_description.startswith("The full posting.") and len(job.job_description) > 1500


def test_the_short_summary_is_kept_when_the_full_posting_cannot_be_read(env, monkeypatch):
    from core import queue_engine
    from core.scraper import ScrapeResult

    engine, store, settings, tmp, calls = env
    monkeypatch.setattr(queue_engine, "scrape_job_url", lambda *a, **k: ScrapeResult(ok=False, url="u", error="blocked"))
    jid = _job_without_text(store, "Peraton", "SRE", url="https://job-boards.greenhouse.io/embed/job_app?for=peraton&token=1")
    stub = store.get(jid)
    stub.job_description = "A short summary that still describes the role. " * 8
    store.update(stub)
    job = run(engine, store, jid)
    assert job.status == JobStatus.DONE and job.job_description.startswith("A short summary")
    assert any("short description" in line for line in job.logs)


def test_a_posting_that_is_not_on_a_readable_site_keeps_whatever_description_it_has(env, monkeypatch):
    from core import queue_engine

    engine, store, settings, tmp, calls = env
    monkeypatch.setattr(queue_engine, "scrape_job_url", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not scrape")))
    job = run(engine, store, queued_job(store, url="https://careers.example.com/job/5"))  # "We need an SRE." is its text
    assert job.status == JobStatus.DONE and job.job_description == "We need an SRE."
