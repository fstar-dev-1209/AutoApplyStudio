"""Which jobs the apply bot takes, in what order, within what limits, and how each result is recorded."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from core.apply_runner import ApplyRunner, pick_files
from core.applications import ApplicationService
from core.chat_questions import Question
from core.greenhouse import DRY_RUN, EXPIRED, FAILED, HUMAN_CHECK, NEEDS_YOU, SUBMITTED, ApplyOutcome
from core.models import JobItem, JobStatus, QueueStore
from core.settings_store import AppSettings, ProfileSettings

T0 = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)  # 10:00 in New York
GH = "https://boards.greenhouse.io/acme/jobs/{n}"


class FakeHost:
    def run(self, task, timeout=0):
        return task(None)


class FakeApplier:
    """Stands in for GreenhouseApplier: returns the queued outcome and remembers how it was called."""

    def __init__(self, env, answer, should_stop):
        self.env, self.answer, self.should_stop = env, answer, should_stop

    def apply(self, context, url, *, form_ctx, files, submit, tag, company, role=""):
        self.env.calls.append({"url": url, "submit": submit, "company": company, "files": files,
                               "chat": form_ctx.chat_enabled, "stop": self.should_stop(), "answer": self.answer, "role": role,
                               "skip_optional": form_ctx.skip_optional, "pay": form_ctx.pay})
        result = self.env.outcomes.pop(0) if self.env.outcomes else ApplyOutcome(SUBMITTED, "Application received.")
        if isinstance(result, Exception):
            raise result
        return result


class Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.store = QueueStore(tmp_path / "studio.db")
        self.apps = ApplicationService(self.store.database)
        self.settings = AppSettings(apply_mode="auto", apply_daily_cap=40,
                                    profile=ProfileSettings(name="Mary Ann Berg", work_history="Engineer | PayPal | 2019 - 2024"))
        self.now = T0
        self.calls: list[dict] = []
        self.outcomes: list = []
        self.alerts: list[tuple[str, str]] = []
        self.chat_calls: list[dict] = []
        self.appliers: list[FakeApplier] = []
        self.runner = ApplyRunner(
            self.store, self.apps, lambda: self.settings, lambda: FakeHost(),
            alert=lambda t, b: self.alerts.append((t, b)),
            applier_factory=self._make_applier,
            now=lambda: self.now, pause=lambda lo, hi: None,
        )

    def _make_applier(self, **kw):
        applier = FakeApplier(self, kw["answer"], kw["should_stop"])
        self.appliers.append(applier)
        return applier

    def job(self, n: int, *, company=None, url=None, completed_minutes_ago=0, files=True, status=JobStatus.DONE,
            chat_url="") -> JobItem:
        job = JobItem.create(url or GH.format(n=n))
        job.company, job.role_title = company or f"Company{n}", "Backend Engineer"
        job.status = status
        job.job_description = "We need an engineer."
        job.resume_json = {"cover_letter": "Dear team", "experience": [{"company": "PayPal", "title": "Engineer"}]}
        job.chat_url = chat_url
        job.completed_at = (T0 - timedelta(minutes=completed_minutes_ago)).isoformat()
        if files:
            folder = self.tmp / f"job{n}"
            folder.mkdir(exist_ok=True)
            paths = [folder / "Mary_Resume.docx", folder / "Mary_Resume.pdf", folder / "Mary_Cover_Letter.docx"]
            for p in paths:
                p.write_bytes(b"x")
            job.generated_files = [str(p) for p in paths]
        self.store.add_many([job])
        return job

    def tick(self):
        return self.runner.run_next()

    def attempts(self, job):
        return self.runner.attempts.for_job(job.id)

    def events(self, job):
        return [e.type for e in self.store.database.effective_events(job.id)]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.store.database.close()


def test_the_newest_finished_job_goes_first_and_a_sent_application_is_marked_applied(env):
    old, new = env.job(1, completed_minutes_ago=120), env.job(2, completed_minutes_ago=5)
    out = env.tick()
    assert out.state == SUBMITTED
    assert env.calls[0]["url"] == GH.format(n=2) and env.calls[0]["submit"] is True
    assert env.store.get(new.id).applied_at and not env.store.get(old.id).applied_at
    assert env.attempts(new)[0]["state"] == "submitted"
    assert "applied" in env.events(new)
    env.tick()  # the next tick takes the older one
    assert env.calls[1]["url"] == GH.format(n=1) and env.runner.status.submitted_today == 1


def test_files_prefer_the_pdf_and_keep_the_cover_letter_separate(env):
    job = env.job(1)
    files = pick_files(job)
    assert files.resume.endswith("Mary_Resume.pdf") and files.cover.endswith("Mary_Cover_Letter.docx")
    only_docx = JobItem.create(GH.format(n=3))
    only_docx.generated_files = [str(env.tmp / "job1" / "Mary_Resume.docx")]
    assert pick_files(only_docx).resume.endswith("Mary_Resume.docx") and pick_files(only_docx).cover == ""
    gone = JobItem.create(GH.format(n=4))
    gone.generated_files = [str(env.tmp / "missing.pdf")]
    assert pick_files(gone).resume == ""


def test_a_dry_run_sends_nothing_and_is_not_repeated(env):
    env.settings.apply_mode = "dry_run"
    job = env.job(1)
    env.outcomes = [ApplyOutcome(DRY_RUN, "Filled and checked; not sent (dry run).")]
    assert env.tick().state == DRY_RUN
    assert env.calls[0]["submit"] is False and not env.store.get(job.id).applied_at
    assert "apply_dry_run" in env.events(job) and "applied" not in env.events(job)
    assert env.tick() is None  # already dry-run; nothing else to do
    env.settings.apply_mode = "auto"
    assert env.tick().state == SUBMITTED  # switching to auto then really applies


def test_when_it_needs_you_you_are_told_once_and_it_waits_until_you_resolve_it(env):
    job = env.job(1)
    env.outcomes = [ApplyOutcome(NEEDS_YOU, "Which office would you prefer?", needs=["Which office would you prefer?"])]
    env.tick()
    assert len(env.alerts) == 1 and "Company1" in env.alerts[0][0] and "office" in env.alerts[0][1]
    assert "apply_blocked" in env.events(job)
    (waiting,) = env.runner.attempts.needs_you()
    assert waiting["company"] == "Company1" and "office" in waiting["reason"]

    assert env.tick() is None, "the bot must not keep retrying a job that is waiting for you"
    env.runner.attempts.resolve(waiting["id"], env.now)
    assert env.runner.attempts.needs_you() == []
    assert env.tick().state == SUBMITTED  # resolved: it may try again


def test_try_again_opens_the_job_even_when_auto_apply_is_off(env):
    env.settings.apply_mode = "off"
    job = env.job(1)
    assert env.tick() is None
    env.outcomes = [ApplyOutcome(NEEDS_YOU, "Which office would you prefer?")]
    env.runner.request_now(job.id, submit=False)
    assert env.tick().state == NEEDS_YOU
    assert env.calls[0]["submit"] is False and env.calls[0]["company"] == "Company1"
    assert env.tick() is None  # off again once the requested job has been tried


def test_submitting_a_finished_dry_run_sends_that_one_application(env):
    env.settings.apply_mode = "dry_run"
    job = env.job(1)
    env.outcomes = [ApplyOutcome(DRY_RUN, "Filled and checked")]
    assert env.tick().state == DRY_RUN and env.tick() is None
    env.outcomes = [ApplyOutcome(SUBMITTED, "Application received.")]
    env.runner.request_now(job.id, submit=True)
    assert env.tick().state == SUBMITTED and env.calls[-1]["submit"] is True
    assert env.store.get(job.id).applied_at


def test_a_failure_is_retried_later_and_gives_up_after_three_tries(env):
    job = env.job(1)
    env.outcomes = [ApplyOutcome(FAILED, "page did not load")]
    env.tick()
    assert env.tick() is None  # too soon
    env.now += timedelta(minutes=31)
    env.outcomes = [ApplyOutcome(FAILED, "again")]
    env.tick()
    env.now += timedelta(minutes=31)
    env.outcomes = [ApplyOutcome(FAILED, "third")]
    env.tick()
    env.now += timedelta(hours=5)
    assert env.tick() is None and len(env.attempts(job)) == 3
    assert env.alerts == []  # transient failures do not page you


def test_a_closed_posting_is_not_tried_again(env):
    job = env.job(1)
    env.outcomes = [ApplyOutcome(EXPIRED, "The posting is closed.")]
    env.tick()
    env.now += timedelta(days=2)
    assert env.tick() is None and env.attempts(job)[0]["state"] == "expired"
    assert env.alerts == []


def test_a_captcha_nobody_solved_asks_for_you(env):
    env.job(1)
    env.outcomes = [ApplyOutcome(HUMAN_CHECK, "A human verification was not completed in time.")]
    env.tick()
    assert env.alerts and env.runner.attempts.needs_you()[0]["state"] == "human_check"


def test_the_daily_limit_counts_sent_applications_in_the_bot_timezone(env):
    env.settings.apply_daily_cap = 2
    for n in range(1, 5):
        env.job(n, completed_minutes_ago=n)
    assert env.tick() and env.tick()
    assert env.tick() is None and "limit" in env.runner.status.message.lower()
    assert len(env.calls) == 2
    env.now += timedelta(days=1)  # the next day, New York time
    assert env.tick() is not None


def test_dry_runs_do_not_use_up_the_daily_limit(env):
    env.settings.apply_mode, env.settings.apply_daily_cap = "dry_run", 1
    for n in (1, 2, 3):
        env.job(n, completed_minutes_ago=n)
    env.outcomes = [ApplyOutcome(DRY_RUN, "ok")] * 3
    assert [bool(env.tick()) for _ in range(3)] == [True, True, True]


def test_off_outside_hours_and_the_kill_switch_all_stop_it(env):
    env.job(1)
    env.settings.apply_mode = "off"
    assert env.tick() is None and env.calls == []
    env.settings.apply_mode = "auto"
    env.settings.active_start_hour, env.settings.active_end_hour = 18, 23  # 10:00 in New York is outside
    assert env.tick() is None and "hours" in env.runner.status.message.lower()
    env.settings.active_start_hour, env.settings.active_end_hour = 0, 24
    env.runner.kill()
    assert env.tick() is None and env.runner.killed and env.calls == []
    env.runner.resume()
    assert env.tick() is not None


def test_the_kill_switch_reaches_the_applier_so_it_can_stop_before_sending(env):
    env.job(1)
    env.tick()
    assert env.calls[0]["stop"] is False
    live_flag = env.appliers[0].should_stop  # the applier holds the runner's live flag, not a copy of its value
    assert live_flag() is False
    env.runner.kill()
    assert live_flag() is True and env.runner.status.state == "stopped"
    env.runner.resume()
    assert live_flag() is False


def test_jobs_the_bot_cannot_or_should_not_take_are_ignored(env):
    env.job(1, url="https://jobs.lever.co/acme/abc")  # not an application site it handles
    env.job(2, files=False)  # nothing to attach
    env.job(3, status=JobStatus.QUEUED)
    applied = env.job(4)
    env.apps.mark_applied(applied.id)
    assert env.tick() is None and env.runner.status.message == "Nothing waiting to be applied to."


def test_a_company_already_applied_to_is_skipped(env):
    first = env.job(1, company="Acme Corp")
    env.apps.mark_applied(first.id)
    env.job(2, company="Acme Corp", url=GH.format(n=2))
    assert env.tick() is None and env.calls == []


def test_the_open_questions_are_asked_in_the_jobs_conversation_and_a_new_one_is_remembered(env):
    @dataclass
    class Result:
        answer: str = "I built it."
        error: str = ""
        chat_url: str = "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"
        raw: str = ""

    def ask_chat(prompt, **kw):
        env.chat_calls.append({"prompt": prompt, **kw})
        return Result()

    env.runner._ask_chat = ask_chat
    env.settings.answer_prompt = "Write like a person."
    saved = "https://chatgpt.com/c/aaaaaaaa-2222-3333-4444-555555555555"
    job = env.job(1, chat_url=saved)
    env.tick()
    call = env.calls[0]
    assert call["chat"] is True
    assert call["answer"](Question("Why us?")) == "I built it."
    ask = env.chat_calls[0]
    assert ask["chat_url"] == saved and "Why us?" in ask["prompt"] and "Write like a person." in ask["prompt"]
    assert "We need an engineer." in ask["context_prompt"]  # the job context is sent if a new chat is needed
    # the conversation the answer came from replaces the saved one, so the next question continues in it
    assert env.store.get(job.id).chat_url == Result.chat_url


def test_a_browser_crash_is_recorded_against_the_job_not_raised(env):
    job = env.job(1)
    env.outcomes = [RuntimeError("browser died")]
    out = env.tick()
    assert out.state == FAILED and "browser died" in out.reason
    assert env.attempts(job)[0]["state"] == "failed"


def test_analytics_counts_what_the_bot_sent_by_site_and_nothing_applied_by_hand(env):
    from core import analytics
    from core.timeutil import DateRange

    env.job(1, completed_minutes_ago=5)
    env.job(2, completed_minutes_ago=6)
    by_hand = env.job(3, completed_minutes_ago=7)
    env.apps.mark_applied(by_hand.id)  # applied by the person: not the bot's
    env.outcomes = [ApplyOutcome(SUBMITTED, "ok"), ApplyOutcome(NEEDS_YOU, "stopped")]
    env.tick()
    env.tick()
    day = DateRange(start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=1), label="today")
    assert analytics.bot_sent(env.store.database, day) == {"greenhouse": 1, "total": 1}
    later = DateRange(start=T0 + timedelta(days=1), end=T0 + timedelta(days=2), label="tomorrow")
    assert analytics.bot_sent(env.store.database, later) == {"total": 0}
    assert analytics.bot_sent(env.store.database, DateRange(start=None, end=None, label="all"))["total"] == 1


def test_the_documented_defaults_apply_unless_you_set_your_own(env):
    seen = []
    original = env._make_applier

    def spy(**kw):
        applier = original(**kw)
        real_apply = applier.apply
        applier.apply = lambda *a, **k: (seen.append(dict(k["form_ctx"].facts)), real_apply(*a, **k))[1]
        return applier

    env.runner._factory = spy
    env.settings.apply_facts = {"relocation": "Yes", "agree_consents": "No"}
    env.job(1)
    env.tick()
    facts = seen[0]
    assert facts["previously_employed"] == "No" and facts["sms_consent"] == "No" and facts["eeo_gender"] == "Decline"
    assert facts["relocation"] == "Yes" and facts["agree_consents"] == "No"  # what you entered wins
    assert env.calls[0]["role"] == "Backend Engineer"  # the posting check is given the role it should find


def test_an_attempt_cut_off_by_a_restart_is_closed_when_the_bot_starts(env):
    job = env.job(1)
    env.runner.attempts.start(job.id, "greenhouse", "auto", T0)
    assert env.runner.attempts.close_interrupted(T0) == 1
    row = env.attempts(job)[0]
    assert row["state"] == "failed" and "stopped" in row["reason"]
    assert env.runner.attempts.close_interrupted(T0) == 0


def test_a_resume_without_work_experience_is_never_sent(env):
    job = env.job(1)
    job.resume_json = {"cover_letter": "Dear team", "experience": []}
    env.store.update(job)
    assert "no work experience" in env.runner.eligible(env.store.get(job.id), env.runner._settings())
    assert env.tick() is None or not env.calls


def test_has_experience_needs_at_least_one_real_job():
    from core.resume_finalize import has_experience
    assert has_experience({"experience": [{"company": "PayPal", "title": "Engineer"}]})
    assert not has_experience({"experience": []})
    assert not has_experience({"experience": [{}]})
    assert not has_experience({})


def test_an_empty_work_history_does_not_hold_auto_apply_back(env):
    job = env.job(1)
    env.settings.profile.work_history = ""
    assert "work history" not in env.runner.eligible(env.store.get(job.id), env.settings)


def test_a_resume_naming_an_employer_outside_the_work_history_is_never_sent(env):
    job = env.job(2)
    job.resume_json = {"cover_letter": "Dear team", "experience": [{"company": "KPMG", "title": "Analyst"}]}
    env.store.update(job)
    assert "not in your work history" in env.runner.eligible(env.store.get(job.id), env.settings)


class _Told:
    def __init__(self):
        self.calls = []

    def application(self, job, state, reason, plan, confirmation, submitted_ok):
        self.calls.append((job.company, state, submitted_ok, [p["label"] for p in plan], confirmation))


def test_every_result_is_reported_and_a_sent_application_carries_its_answers(env):
    told = _Told()
    env.runner._reporter = told
    plan = [{"label": "First Name", "action": "fill", "source": "profile", "value": "Mary"}]
    env.outcomes = [ApplyOutcome(SUBMITTED, "Application received.", plan, confirmation="Thank you for applying"),
                    ApplyOutcome(FAILED, "boom"), ApplyOutcome(NEEDS_YOU, "Which office?")]
    jobs = [env.job(n, completed_minutes_ago=n) for n in (1, 2, 3)]
    for _ in jobs:
        env.tick()
    assert [(c[1], c[2]) for c in told.calls] == [("submitted", True), ("failed", False), ("needs_you", False)]
    assert told.calls[0][3] == ["First Name"] and told.calls[0][4] == "Thank you for applying"


def test_a_report_that_breaks_does_not_undo_an_application_that_was_sent(env):
    class Broken:
        def application(self, *a, **k):
            raise RuntimeError("telegram is down")

    env.runner._reporter = Broken()
    job = env.job(1)
    assert env.tick().state == SUBMITTED and env.store.get(job.id).applied_at


def test_optional_questions_are_left_blank_unless_the_setting_says_otherwise(env):
    env.job(1, completed_minutes_ago=1)
    env.tick()
    env.settings.apply_skip_optional = False
    env.job(2, completed_minutes_ago=2)
    env.tick()
    assert [c["skip_optional"] for c in env.calls] == [True, False]  # on by default; the setting reaches the form


def test_the_form_filler_is_given_the_jobs_own_pay_and_your_salary_rules(env):
    env.settings.salary_floor, env.settings.salary_band_low, env.settings.salary_band_high = 95_000, 30, 50
    env.settings.apply_facts = {"salary_expectation": "$140,000"}
    job = env.job(1)
    job.salary_min, job.salary_max, job.salary_period, job.salary_currency = 100_000, 130_000, "year", "USD"
    job.salary_text = "$100K/yr - $130K/yr"
    env.store.update(job)
    env.tick()
    pay = env.calls[0]["pay"]
    assert (pay.low, pay.high, pay.period, pay.floor) == (100_000, 130_000, "year", 95_000)
    assert (pay.band_low, pay.band_high, pay.fallback) == (30, 50, "$140,000")
    assert pay.aim() == 110_000  # 40% of the way from 100K to 130K is 112K, tidied to the nearest 5K


def test_trying_a_closed_posting_by_hand_does_not_reopen_it(env):
    job = env.job(1)
    env.outcomes = [ApplyOutcome(EXPIRED, "The posting is closed.")]
    env.tick()
    calls = len(env.calls)
    env.runner.request_now(job.id, submit=True)
    assert env.tick() is None and len(env.calls) == calls
