"""Sequential job queue worker with Start / Pause / Stop."""

from __future__ import annotations

import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from core.claude_client import ChatPageBlocked, ClaudeClient, ClaudeError
from core import ats, jobright
from core.company_match import applied_matches, same_company
from core.salary import parse_salary
from core.prompt_builder import build_claude_chat_prompt
from core.settings_store import GENERATION_CLAUDE_API
from core.models import JobItem, JobStatus, QueueStore
from core.paths import company_folder_exists
from core.profile_apply import apply_settings_profile
from core.scraper import description_looks_broken, scrape_job_url
from core.settings_store import AppSettings, SettingsStore
from engine import (
    PdfExportError,
    ResumeError,
    cover_letter_stem,
    export_resume,
    load_resume,
    resume_stem,
    safe_stem,
)


class WorkerState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"


EventCallback = Callable[[str, dict], None]


def _reply_rejected(exc: BaseException) -> bool:
    """True when the chat did reply but the reply was read and thrown away (a template, no work experience)."""
    return "not a finished resume" in str(exc) or "not a truthful resume" in str(exc)


class QueueEngine:
    def __init__(
        self,
        store: QueueStore,
        settings: SettingsStore,
        on_event: EventCallback | None = None,
        claude_chat_generate: Callable[[str, dict[str, str], AppSettings], dict[str, Any]]
        | None = None,
        render_page: Callable[[str], str | None] | None = None,
    ) -> None:
        self._render_page = render_page
        self.store = store
        self.settings = settings
        self.on_event = on_event or (lambda _name, _payload: None)
        self._claude_chat_generate = claude_chat_generate
        self._state = WorkerState.IDLE
        self._thread: threading.Thread | None = None
        self._pause = threading.Event()
        self._pause.set()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._current_id: str | None = None
        self._jd_waiters: dict[str, threading.Event] = {}
        self._attempts: dict[str, int] = {}
        self._no_auto_retry: set[str] = set()
        self.retry_pause_seconds = 0.0  # the app sets 20: before the 1st automatic retry, twice that before the 2nd
        self.hold_generation: Callable[[], bool] = lambda: False  # True while a form question wants the chat
        self._fresh_chat_next: set[str] = set()  # jobs whose last reply was rejected: the retry must not re-read it

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def current_job_id(self) -> str | None:
        return self._current_id

    def _emit(self, name: str, **payload) -> None:
        try:
            self.on_event(name, payload)
        except Exception:  # noqa: BLE001
            pass

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self._state == WorkerState.PAUSED:
                    self._state = WorkerState.RUNNING
                    self._pause.set()
                    self._emit("state", state=self._state.value)
                return
            self._stop.clear()
            self._pause.set()
            self._state = WorkerState.RUNNING
            self._thread = threading.Thread(target=self._run_loop, name="queue-worker", daemon=True)
            self._thread.start()
            self._emit("state", state=self._state.value)

    def pause(self) -> None:
        with self._lock:
            if self._state != WorkerState.RUNNING:
                return
            self._state = WorkerState.PAUSED
            self._pause.clear()
            self._emit("state", state=self._state.value)

    def resume(self) -> None:
        self.start()

    def stop(self) -> None:
        with self._lock:
            self._state = WorkerState.STOPPING
            self._stop.set()
            self._pause.set()
            self._emit("state", state=self._state.value)
        # Unblock any Needs JD waiters so loop can exit cleanly
        for event in list(self._jd_waiters.values()):
            event.set()

    def apply_resume_json(self, job_id: str, json_text: str) -> tuple[bool, str]:
        from core.json_extract import JsonExtractError, extract_usable_resume
        from core.resume_finalize import finalize_resume_object

        job = self.store.get(job_id)
        if job is None:
            return False, "Job not found."
        text = json_text.strip()
        if not text:
            return False, "Paste resume JSON first."
        settings = self.settings.snapshot()
        apply_settings_profile(settings)
        try:
            obj = extract_usable_resume(text)
            validated = finalize_resume_object(
                obj,
                settings,
                company=job.company,
                role_title=job.role_title,
                job_url=job.origin_url or job.url,
            )
        except (JsonExtractError, Exception) as exc:  # noqa: BLE001
            return False, str(exc)
        job.resume_json = validated
        job.error = ""
        job.failed_stage = ""
        job.retry_from = "export"
        job.set_status(JobStatus.QUEUED, "Ready to export")
        job.log("Resume JSON imported from clipboard.")
        self.store.update(job)
        self._emit("job_updated", job_id=job_id)
        return True, ""

    def submit_jd(self, job_id: str, job_description: str) -> bool:
        job = self.store.get(job_id)
        if job is None:
            return False
        text = job_description.strip()
        if not text:
            return False
        job.job_description = text
        job.error = ""
        job.set_status(JobStatus.QUEUED, "Ready to generate")
        job.log("Job description saved.")
        self.store.update(job)
        waiter = self._jd_waiters.get(job_id)
        if waiter is not None:
            waiter.set()
        self._emit("job_updated", job_id=job_id)
        if self._state in {WorkerState.IDLE, WorkerState.PAUSED}:
            # Auto-continue if user resumes; if idle and they want processing, they hit Start
            pass
        return True

    def retry_job(self, job_id: str, mode: str = "auto") -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        if mode == "auto":
            if job.failed_stage == "export" and job.resume_json:
                mode = "export"
            else:
                mode = "generation"
        job.retry_from = mode
        job.error = ""
        job.failed_stage = ""
        if mode == "export" and job.resume_json:
            job.set_status(JobStatus.QUEUED, "Queued for export retry")
        elif job.job_description:
            job.set_status(JobStatus.QUEUED, "Queued for regeneration")
        else:
            job.set_status(JobStatus.QUEUED, "Queued for re-extraction")
        # Reusing the tab reads the reply that is already on screen, which is right after a send or wait problem and
        # wrong after a reply that was read and rejected: that one has to be written again in a new conversation.
        fresh = job_id in self._fresh_chat_next
        self._fresh_chat_next.discard(job_id)
        job.claude_reuse_tab = mode == "generation" and not fresh
        if fresh:
            job.chat_url = ""
        job.log(f"Retry scheduled ({mode}{', new conversation' if fresh else ''}).")
        self.store.update(job)
        self._emit("job_updated", job_id=job_id)

    def skip_job(self, job_id: str, reason: str = "Skipped by user") -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        job.skip_reason = reason
        job.set_status(JobStatus.SKIPPED, reason)
        job.log(reason)
        self.store.update(job)
        self.store.add_event(job_id, "skipped", {"reason": reason, "by_user": True})
        waiter = self._jd_waiters.get(job_id)
        if waiter is not None:
            waiter.set()
        self._emit("job_updated", job_id=job_id)

    def _wait_if_paused(self) -> bool:
        """Return False if stop requested."""
        while not self._pause.is_set():
            if self._stop.is_set():
                return False
            time.sleep(0.1)
        return not self._stop.is_set()

    def _yield_to_questions(self, limit_sec: float = 1200) -> None:
        """Let an open application form have the chat first. A form waiting for an answer is a job one step from
        being sent; the next resume can wait a minute. Never waits forever."""
        deadline = time.monotonic() + limit_sec
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                if not self.hold_generation():
                    return
            except Exception:  # noqa: BLE001 - a broken hook must not stop the queue
                return
            time.sleep(1)

    def _next_runnable(self) -> JobItem | None:
        self._yield_to_questions()
        for job in self.store.jobs():
            if job.status == JobStatus.QUEUED:
                return job
        return None

    def _run_loop(self) -> None:
        try:
            while not self._stop.is_set():
                if not self._wait_if_paused():
                    break
                job = self._next_runnable()
                if job is None:
                    break
                self._current_id = job.id
                self._emit("job_started", job_id=job.id)
                try:
                    self._process_job(job)
                except Exception as exc:  # noqa: BLE001
                    job = self.store.get(job.id) or job
                    job.failed_stage = job.failed_stage or "unknown"
                    job.error = str(exc)
                    job.set_status(JobStatus.FAILED, "Unexpected failure")
                    job.log(f"Error: {exc}")
                    self.store.update(job)
                    self._emit("job_updated", job_id=job.id)
                finally:
                    self._record_result(job.id)
                    self._maybe_auto_retry(job.id)
                    self._current_id = None
                    delay = max(0.0, self.settings.settings.request_delay_seconds)
                    if delay and not self._stop.is_set():
                        time.sleep(delay)
        finally:
            self._current_id = None
            self._state = WorkerState.IDLE
            self._emit("state", state=self._state.value)
            self._emit("batch_finished")

    def _restore_identity(self, job: JobItem) -> None:
        """A job found on Jobright keeps the company and title Jobright gave it. If something overwrote them (an older
        scrape named many jobs "Embed", a resume for another job renamed one), put them back before generating."""
        row = self.store.database.conn.execute(
            "SELECT company, title FROM discovered_jobs WHERE job_id = ?", (job.id,)).fetchone()
        if row and row["company"] and (not job.company or not same_company(job.company, row["company"])):
            job.log(f"Company and role restored from Jobright: {row['company']} - {row['title']}")
            job.company = row["company"]
            job.role_title = row["title"] or job.role_title
            self.store.update(job)
        elif not row and job.company.strip().casefold() == "embed":
            job.company = ""  # the word from an embed link's path, not a company; the page or the resume will say

    def _process_job(self, job: JobItem) -> None:
        self._restore_identity(job)
        if job.job_description.strip() and description_looks_broken(job.job_description):
            job.log("The saved job description was page debris (raw tags or the application form): reading the page again.")
            job.job_description = ""
            self.store.update(job)
        settings = self.settings.snapshot()
        apply_settings_profile(settings)
        output_root = settings.resolved_output_root()
        person = settings.profile.name

        # Fast path: retry export only
        if job.retry_from == "export" and job.resume_json:
            job.retry_from = ""
            self._export_job(job, job.resume_json, output_root)
            return

        # Extraction if no JD yet, or only a short list blurb for a posting we can read in full.
        # A sectioned Jobright posting is the description even when the role is short: the employer page is not read over it.
        saved_text = job.job_description.strip()
        thin = (bool(saved_text) and len(saved_text) < 1200 and ats.classify(job.url).supported
                and not jobright.is_full_posting(saved_text))
        if not saved_text or thin:
            if not self._wait_if_paused():
                return
            job.set_status(JobStatus.EXTRACTING, "Fetching job page")
            job.log("Extracting job description.")
            self.store.update(job)
            self._emit("job_updated", job_id=job.id)

            result = scrape_job_url(job.url, render=self._render_page)
            job.source = result.source or job.source
            if result.origin_url:
                job.origin_url = result.origin_url
            # What the job already says (from Jobright or the person who added it) wins over what a page scrape guesses:
            # a scrape can only fill in what is missing.
            if result.company and not job.company.strip():
                job.company = result.company
            if result.role_title and not job.role_title.strip():
                job.role_title = result.role_title
            if result.salary_text and not job.salary_text:
                pay = parse_salary(result.salary_text)
                job.salary_text, job.salary_min, job.salary_max = pay.text, pay.low, pay.high
                job.salary_period, job.salary_currency = pay.period, pay.currency
            if result.ok and len(result.job_description.strip()) > len(saved_text):
                job.job_description = result.job_description.strip()
                job.log("Job description extracted.")
                self.store.update(job)
                self._emit("job_updated", job_id=job.id)
            elif thin:
                job.log("The full posting could not be read; using the short description that came with the job.")
            else:
                job.error = result.error or "Could not extract job description."
                job.set_status(JobStatus.NEEDS_JD, "Job description required")
                job.log(job.error)
                self.store.update(job)
                self._emit("job_updated", job_id=job.id)
                self._wait_for_jd(job.id)
                job = self.store.get(job.id) or job
                if job.status == JobStatus.SKIPPED or self._stop.is_set():
                    return
                if job.status != JobStatus.QUEUED or not job.job_description.strip():
                    return

        # Early company skip, only when the page itself named the company. The real company is read from the
        # generated resume below; guessing from the web address gave names like "Www1" or "Careers".
        company = job.company.strip()
        if company:
            existing = None if job.duplicate_override else company_folder_exists(output_root, person, company)
            if existing is not None:
                job.skip_reason = f"Company folder already exists: {existing}"
                job.output_path = str(existing)
                job.set_status(JobStatus.SKIPPED, "Duplicate company folder")
                job.log(job.skip_reason)
                self.store.update(job)
                self._emit("job_updated", job_id=job.id)
                return

        if not self._wait_if_paused():
            return

        # Generation
        if not job.resume_json or job.retry_from == "generation" or job.retry_from == "":
            if job.retry_from == "generation":
                job.resume_json = None
            job.retry_from = ""
            use_chat = settings.generation_mode != GENERATION_CLAUDE_API
            if not use_chat and not settings.api_key.strip():
                job.failed_stage = "generation"
                job.error = "Claude API key is not configured. Add it in Settings or use Claude web chat."
                job.set_status(JobStatus.FAILED, "API key required")
                job.log(job.error)
                self.store.update(job)
                self._emit("job_updated", job_id=job.id)
                return
            if use_chat and self._claude_chat_generate is None:
                job.failed_stage = "generation"
                job.error = "Claude web chat is not available."
                job.set_status(JobStatus.FAILED, "Claude chat unavailable")
                job.log(job.error)
                self.store.update(job)
                self._emit("job_updated", job_id=job.id)
                return

            job.set_status(JobStatus.GENERATING, "Generating documents")
            if use_chat:
                job.log("Generating tailored resume content via Claude web chat.")
                self._emit("claude_chat_started", job_id=job.id)
            else:
                job.log("Generating tailored resume content via Claude API.")
            self.store.update(job)
            self._emit("job_updated", job_id=job.id)

            try:
                if use_chat:
                    prompt = build_claude_chat_prompt(
                        settings,
                        job_description=job.job_description,
                        job_url=job.origin_url or job.url,
                        company=job.company,
                        role_title=job.role_title,
                    )
                    meta = {
                        "company": job.company,
                        "role_title": job.role_title,
                        "job_url": job.origin_url or job.url,
                        "claude_reuse_tab": job.claude_reuse_tab,
                        "saved_chat_url": job.chat_url,  # lets a retry go back to THIS job's conversation
                    }
                    try:
                        resume = self._claude_chat_generate(prompt, meta, settings)
                    finally:
                        # The conversation address is kept even when this attempt failed: a retry can reuse the tab.
                        if meta.get("chat_url"):
                            job.chat_url = str(meta["chat_url"])
                            job.chat_provider = str(meta.get("chat_provider") or "")
                    job.claude_reuse_tab = False
                else:
                    client = ClaudeClient(settings)
                    resume = client.generate_resume(
                        job_description=job.job_description,
                        job_url=job.origin_url or job.url,
                        company=job.company,
                        role_title=job.role_title,
                    )
            except ChatPageBlocked as exc:
                # Signed out, a human check, or a usage limit. Retrying immediately would hit the same page.
                self._no_auto_retry.add(job.id)
                job.failed_stage = "generation"
                job.error = str(exc)
                job.set_status(JobStatus.FAILED, "Chat needs you")
                job.log(str(exc))
                self.store.update(job)
                self._emit("job_updated", job_id=job.id)
                return
            except ClaudeError as exc:
                if _reply_rejected(exc):
                    self._fresh_chat_next.add(job.id)
                job.failed_stage = "generation"
                job.error = str(exc)
                job.set_status(JobStatus.FAILED, "Generation failed")
                job.log(str(exc))
                self.store.update(job)
                self._emit("job_updated", job_id=job.id)
                return
            except Exception as exc:  # noqa: BLE001
                if _reply_rejected(exc):
                    self._fresh_chat_next.add(job.id)
                job.failed_stage = "generation"
                job.error = str(exc)
                job.set_status(JobStatus.FAILED, "Generation failed")
                job.log(str(exc))
                self.store.update(job)
                self._emit("job_updated", job_id=job.id)
                return
            finally:
                if use_chat:
                    self._emit("claude_chat_finished", job_id=job.id)

            # A resume written for a different company than this job's is the wrong resume (for example a reply left
            # over from another job's conversation). Never keep it, and never let it rename the job.
            written_for = str(resume.get("company_name") or "").strip()
            if job.company and written_for and not same_company(job.company, written_for):
                job.claude_reuse_tab = False  # the next try starts a fresh chat instead of reading what is on screen
                self._fresh_chat_next.add(job.id)
                job.failed_stage = "generation"
                job.error = (f"The resume the chat returned is for '{written_for}', but this job is at '{job.company}'. "
                             "It was discarded; retry to generate it again.")
                job.set_status(JobStatus.FAILED, "Resume was for a different company")
                job.log(job.error)
                self.store.update(job)
                self._emit("job_updated", job_id=job.id)
                return

            # Fill what the page and the user left empty from the resume (it was written from the posting).
            if not job.company:
                job.company = str(resume.get("company_name") or "").strip()
            if not job.role_title:
                job.role_title = str(resume.get("role_title") or resume.get("subtitle") or "").strip()
            if job.company and not job.duplicate_override:
                applied = applied_matches(self.store, job.company, exclude_id=job.id)
                if applied:
                    first = applied[0]
                    when = f" on {first['applied_at'][:10]}" if first["applied_at"] else ""
                    job.skip_reason = (f"Already applied to {first['company']}"
                                       f"{' (' + first['role'] + ')' if first['role'] else ''}{when}")
                    job.resume_json = resume
                    job.set_status(JobStatus.SKIPPED, "Already applied to this company")
                    job.log(job.skip_reason)
                    self.store.update(job)
                    self._emit("job_updated", job_id=job.id)
                    return
                existing = company_folder_exists(output_root, person, job.company)
                if existing is not None:
                    job.skip_reason = f"Company folder already exists: {existing}"
                    job.output_path = str(existing)
                    job.resume_json = resume
                    job.set_status(JobStatus.SKIPPED, "Duplicate company folder")
                    job.log(job.skip_reason)
                    self.store.update(job)
                    self._emit("job_updated", job_id=job.id)
                    return

            job.resume_json = resume
            job.log("Resume JSON generated.")
            self.store.update(job)
            self._emit("job_updated", job_id=job.id)

        if not self._wait_if_paused():
            return

        assert job.resume_json is not None
        self._export_job(job, job.resume_json, output_root)

    def _export_job(self, job: JobItem, resume: dict, output_root: Path) -> None:
        job.set_status(JobStatus.EXPORTING, "Writing documents")
        job.log("Exporting Word, PDF, and cover letter.")
        self.store.update(job)
        self._emit("job_updated", job_id=job.id)

        # Force output under configured root while preserving Name/Company/Role
        company = safe_stem(str(resume.get("company_name") or job.company or "Company"))
        role = safe_stem(
            str(resume.get("role_title") or resume.get("subtitle") or job.role_title or "Role")
        )
        person = safe_stem(str(resume.get("name") or self.settings.settings.profile.name or "Candidate"))
        folder = output_root / person / company / role
        if job.duplicate_override and str(folder) != job.output_path:
            folder = _unique_folder(folder)  # never overwrite another role's documents
        # Also sync fields used by engine helpers
        resume = dict(resume)
        resume.setdefault("company_name", job.company)
        resume.setdefault("role_title", job.role_title)
        if job.origin_url:
            resume.setdefault("job_link", job.origin_url)

        try:
            from core.resume_finalize import has_experience

            if not has_experience(resume):
                raise ResumeError("it has no work experience (the chat's answer was cut short or left it out)")
            validated = load_resume(_json_dumps(resume))
        except ResumeError as exc:
            self._fresh_chat_next.add(job.id)
            job.failed_stage = "generation"
            job.error = f"Generated JSON was invalid: {exc}"
            job.set_status(JobStatus.FAILED, "Invalid generated JSON")
            job.log(job.error)
            self.store.update(job)
            self._emit("job_updated", job_id=job.id)
            return

        stem = resume_stem(validated)
        cover_stem = cover_letter_stem(validated)
        docx_path = folder / f"{stem}.docx"
        opts = self.settings.settings
        pdf_path = folder / f"{stem}.pdf"
        cover_path = folder / f"{cover_stem}.docx"

        try:
            result = export_resume(
                validated,
                docx_path,
                pdf_path if opts.export_pdf else None,
                cover_path if opts.export_cover_letter else None,
            )
        except PdfExportError as exc:
            # Word may still exist
            files = [str(docx_path)] if docx_path.exists() else []
            if cover_path.exists():
                files.append(str(cover_path))
            job.generated_files = files
            job.output_path = str(folder)
            job.resume_json = validated
            job.failed_stage = "export"
            job.error = str(exc)
            job.set_status(JobStatus.FAILED, "PDF conversion failed")
            job.log(str(exc))
            self.store.update(job)
            self._emit("job_updated", job_id=job.id)
            return
        except (OSError, ValueError) as exc:
            job.failed_stage = "export"
            job.error = str(exc)
            job.resume_json = validated
            job.set_status(JobStatus.FAILED, "Export failed")
            job.log(str(exc))
            self.store.update(job)
            self._emit("job_updated", job_id=job.id)
            return

        files = [str(result.docx_path)]
        if result.pdf_path:
            files.append(str(result.pdf_path))
        if result.cover_letter_path:
            files.append(str(result.cover_letter_path))
        job.generated_files = files
        job.output_path = str(folder)
        job.company = str(validated.get("company_name") or job.company)
        job.role_title = str(validated.get("role_title") or validated.get("subtitle") or job.role_title)
        job.resume_json = validated
        job.error = ""
        job.failed_stage = ""
        from datetime import datetime, timezone

        job.completed_at = datetime.now(timezone.utc).isoformat()
        job.set_status(JobStatus.DONE, "Completed")
        job.log("Documents exported.")
        if result.warnings:
            for warning in result.warnings:
                job.log(f"Warning: {warning}")
        self.store.update(job)
        self._emit("job_updated", job_id=job.id)

    def _maybe_auto_retry(self, job_id: str) -> None:
        """Re-queue a failed generation up to ``auto_retries`` times (setting; default 0)."""
        if job_id in self._no_auto_retry:
            self._no_auto_retry.discard(job_id)
            return
        job = self.store.get(job_id)
        if job is None or job.status != JobStatus.FAILED or job.failed_stage != "generation":
            self._attempts.pop(job_id, None)
            return
        if self._attempts.get(job_id, 0) >= max(0, int(self.settings.settings.auto_retries)):
            return
        self._attempts[job_id] = self._attempts.get(job_id, 0) + 1
        # Whatever stopped the chat a second ago is usually still there a second later: give it time to pass.
        self._stop.wait(self.retry_pause_seconds * self._attempts[job_id])
        self.retry_job(job_id, "generation")

    def _record_result(self, job_id: str) -> None:
        """Append the pipeline outcome of this run to the event history (analytics source)."""
        job = self.store.get(job_id)
        if job is None:
            return
        if job.status == JobStatus.DONE:
            self.store.add_event(job_id, "docs_generated", {"files": job.generated_files})
        elif job.status == JobStatus.FAILED:
            self.store.add_event(
                job_id, "generation_failed", {"stage": job.failed_stage, "error": job.error[:300]}
            )
        elif job.status == JobStatus.SKIPPED:
            dup = "company folder" in (job.skip_reason or "").lower()
            self.store.add_event(
                job_id, "skipped_duplicate" if dup else "skipped", {"reason": job.skip_reason}
            )

    def _wait_for_jd(self, job_id: str) -> None:
        event = threading.Event()
        self._jd_waiters[job_id] = event
        try:
            while not self._stop.is_set():
                if not self._wait_if_paused():
                    return
                # Continue processing other jobs: mark waiter and return to loop
                # Strategy: don't block the whole queue on Needs JD.
                # Leave this job in Needs JD and move on.
                return
        finally:
            self._jd_waiters.pop(job_id, None)


def _unique_folder(folder: Path) -> Path:
    if not folder.exists() or not any(folder.iterdir()):
        return folder
    n = 2
    while True:
        candidate = folder.with_name(f"{folder.name} ({n})")
        if not candidate.exists():
            return candidate
        n += 1


def _json_dumps(data: dict) -> str:
    import json

    return json.dumps(data)
