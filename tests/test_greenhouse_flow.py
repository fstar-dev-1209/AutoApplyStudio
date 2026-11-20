"""The Greenhouse application flow end to end, in a real Chrome, against a stand-in board (see greenhouse_fakes)."""

from __future__ import annotations

import socket

import pytest

pytest.importorskip("playwright")
from core.browser_host import BrowserConfig, BrowserError, BrowserHost, find_browser  # noqa: E402

try:
    find_browser("chrome")
except BrowserError:  # pragma: no cover
    pytest.skip("Chrome is not installed", allow_module_level=True)

from core.form_answers import Context  # noqa: E402
from core.form_filler import Files  # noqa: E402
from core.greenhouse import (  # noqa: E402
    DRY_RUN,
    EXPIRED,
    FAILED,
    HUMAN_CHECK,
    NEEDS_YOU,
    SUBMITTED,
    GreenhouseApplier,
)
from core.settings_store import ProfileSettings  # noqa: E402
from tests.greenhouse_fakes import Board  # noqa: E402

PROFILE = ProfileSettings(name="Mary Ann van der Berg", email="mary@example.com", phone="(555) 010-0000",
                          location="Austin, TX", linkedin_url="https://linkedin.com/in/mary",
                          work_authorization="Yes", needs_sponsorship="No")
FACTS = {"city": "Austin", "state": "TX", "referral_source": "LinkedIn", "agree_consents": "Yes"}


@pytest.fixture
def board():
    b = Board().start()
    yield b
    b.stop()


@pytest.fixture
def host(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    h = BrowserHost(BrowserConfig(profile_dir=tmp_path / "profile", headless=True, debug_port=port, mode="attach"))
    h.start()
    yield h
    h.stop()


@pytest.fixture
def files(tmp_path):
    resume, cover = tmp_path / "Mary_Resume.pdf", tmp_path / "Mary_Cover_Letter.docx"
    resume.write_bytes(b"%PDF-1.4 resume")
    cover.write_bytes(b"PK cover")
    return Files(resume=str(resume), cover=str(cover))


def ctx(**kw) -> Context:
    return Context(profile=PROFILE, facts=dict(FACTS), has_resume=True, has_cover=True, **kw)


# The fake forms are plain HTML (no scripts to wait for), so the "has the form come alive" waits are kept short.
GreenhouseApplier.INTERACTIVE_WAIT_MS = 1200
GreenhouseApplier.RELOAD_WAIT_MS = 1200
_REAL_INTERACTIVE_JS = GreenhouseApplier._INTERACTIVE_JS
GreenhouseApplier._INTERACTIVE_JS = "() => true"  # a fake form is usable as soon as it is on screen


def applier(tmp_path, **kw) -> GreenhouseApplier:
    kw.setdefault("pause", lambda lo, hi: None)
    kw.setdefault("board_api", "")
    kw.setdefault("shots_dir", tmp_path / "shots")
    kw.setdefault("answer", lambda q: q.options[2] if q.options else
                  "**Built** RAG pipelines — retrieval, ranking and evals, in production.")
    return GreenhouseApplier(**kw)


def run(host, board, files, tmp_path, *, path="/acme/jobs/1", submit=True, form_ctx=None, **kw):
    a = applier(tmp_path, **kw)
    return host.run(lambda c: a.apply(c, board.url(path), form_ctx=form_ctx or ctx(chat_enabled=True), files=files,
                                      submit=submit, tag="job1", company="Acme"), timeout=180)


def test_a_complete_application_is_filled_checked_sent_and_confirmed(host, board, files, tmp_path):
    out = run(host, board, files, tmp_path)
    assert out.state == SUBMITTED, (out.reason, out.needs)
    assert out.confirmation and "applying" in out.confirmation.lower() or "submitted" in out.confirmation.lower()
    (sent,) = board.submissions
    assert (sent["first_name"], sent["last_name"]) == ("Mary Ann", "van der Berg")
    assert sent["email"] == "mary@example.com" and sent["phone"] == "(555) 010-0000"
    assert sent["candidate-location"] == "Austin, Texas, United States"  # Texas, not Minnesota
    assert sent["resume"] == "Mary_Resume.pdf" and sent["cover_letter"] == "Mary_Cover_Letter.docx"
    assert sent["question_1"] == "https://linkedin.com/in/mary"
    assert sent["question_2"] == "Yes" and sent["question_3"] == "No" and sent["question_4"] == "LinkedIn"
    assert sent["visa"] == "0"  # "Do you require a visa": No, from the sponsorship answer
    assert sent["gender"] == "Decline To Self Identify" and sent["veteran_status"] == "I don't wish to answer"
    assert sent["snow"] == "3-5", "the chat chose among the form's own options for the skill question"
    assert sent["privacy"] is True
    # the open question went to the chat and came back cleaned and within the field's limit
    assert sent["question_5"].startswith("Built RAG pipelines, retrieval") and "**" not in sent["question_5"]
    assert "—" not in sent["question_5"] and len(sent["question_5"]) <= 400


def test_a_dry_run_fills_and_checks_everything_but_sends_nothing(host, board, files, tmp_path):
    out = run(host, board, files, tmp_path, submit=False)
    assert out.state == DRY_RUN and board.submissions == []
    assert out.screenshot and out.plan
    by_label = {p["label"]: p for p in out.plan}
    assert by_label["Gender"]["value"] == "Decline To Self Identify"
    assert by_label["Describe your experience building RAG systems."]["source"] == "chat"


def test_a_race_question_that_appears_after_the_other_answers_is_filled(host, board, files, tmp_path):
    board.late_race = True  # hidden until Gender is chosen, which is how Quisitive drew "Please identify your race"
    form_ctx = ctx(chat_enabled=True)
    form_ctx.facts = {**form_ctx.facts, "eeo_race": "Asian"}
    out = run(host, board, files, tmp_path, form_ctx=form_ctx)
    assert out.state == SUBMITTED, (out.reason, out.needs)
    (sent,) = board.submissions
    assert sent["race"] == "Asian"


def test_an_unanswerable_required_question_stops_everything_before_a_keystroke(host, board, files, tmp_path):
    board.extra_question = True  # "Which office would you prefer?" is nothing the facts cover
    out = run(host, board, files, tmp_path)
    assert out.state == NEEDS_YOU and board.submissions == []
    assert any("office" in n.lower() for n in out.needs)


def test_without_a_chat_the_open_question_blocks_the_application(host, board, files, tmp_path):
    out = run(host, board, files, tmp_path, answer=None, form_ctx=ctx(chat_enabled=False))
    assert out.state == NEEDS_YOU and board.submissions == []
    assert any("RAG" in n for n in out.needs)


def test_a_failing_chat_blocks_rather_than_sending_a_blank(host, board, files, tmp_path):
    def broken(question):
        raise RuntimeError("chat down")

    out = run(host, board, files, tmp_path, answer=broken)
    assert out.state == NEEDS_YOU and board.submissions == []
    assert any("chat down" in n for n in out.needs)


def test_a_closed_posting_is_reported_as_expired(host, board, files, tmp_path):
    assert run(host, board, files, tmp_path, path="/closed/jobs/9").state == EXPIRED
    assert run(host, board, files, tmp_path, path="/gone/jobs/404").state == EXPIRED
    assert board.submissions == []


def test_a_company_page_that_embeds_the_form_works(host, board, files, tmp_path):
    out = run(host, board, files, tmp_path, path="/careers?gh_jid=1")
    assert out.state == SUBMITTED, (out.reason, out.needs)
    assert len(board.submissions) == 1


def test_the_emailed_security_code_is_fetched_and_entered(host, board, files, tmp_path):
    board.code_step = True
    asked = []

    def fetch(context, before, company):
        asked.append((company, sorted(before)))
        return board.valid_code

    out = run(host, board, files, tmp_path, snapshot_mail=lambda context: {"old mail"}, fetch_code=fetch)
    assert out.state == SUBMITTED, (out.reason, out.needs)
    assert asked == [("Acme", ["old mail"])] and board.codes_seen == [board.valid_code]
    assert len(board.submissions) == 1


def test_a_missing_or_wrong_code_ends_as_needs_you_without_a_second_try(host, board, files, tmp_path):
    board.code_step = True
    out = run(host, board, files, tmp_path, snapshot_mail=lambda ctx: set(), fetch_code=lambda ctx, b, c: "")
    assert out.state == NEEDS_YOU and "code" in out.reason.lower() and board.submissions == []

    board.codes_seen.clear()
    wrong = run(host, board, files, tmp_path, snapshot_mail=lambda ctx: set(), fetch_code=lambda ctx, b, c: "ZZZZZZZZ")
    assert wrong.state == NEEDS_YOU and board.codes_seen == ["ZZZZZZZZ"] and board.submissions == []


def test_a_captcha_the_person_solves_lets_the_application_continue(host, board, files, tmp_path):
    board.captcha, board.captcha_solve_ms = True, 2500
    alerts = []
    out = run(host, board, files, tmp_path, human_wait_s=30, notify=lambda t, b: alerts.append(t))
    assert out.state == SUBMITTED, (out.reason, out.needs)
    assert alerts and "Verification" in alerts[0]


def test_a_captcha_nobody_solves_is_reported_and_nothing_is_sent(host, board, files, tmp_path):
    board.captcha, board.captcha_solve_ms = True, 0
    out = run(host, board, files, tmp_path, human_wait_s=4)
    assert out.state == HUMAN_CHECK and board.submissions == []


def test_a_recaptcha_message_waits_and_then_resubmits(host, board, files, tmp_path):
    board.captcha_message, board.captcha_solve_ms = True, 2500
    alerts = []
    out = run(host, board, files, tmp_path, human_wait_s=30, notify=lambda t, b: alerts.append(t))
    assert out.state == SUBMITTED, (out.reason, out.needs)
    assert alerts and "Verification" in alerts[0]
    assert len(board.submissions) == 1


def test_an_unfinished_recaptcha_message_is_not_a_rejected_form(host, board, files, tmp_path):
    board.captcha_message, board.captcha_solve_ms = True, 0
    out = run(host, board, files, tmp_path, human_wait_s=4)
    assert out.state == HUMAN_CHECK and "rejected" not in out.reason.lower() and board.submissions == []


def test_the_kill_switch_stops_before_sending(host, board, files, tmp_path):
    out = run(host, board, files, tmp_path, should_stop=lambda: True)
    assert out.state == FAILED and "Stopped" in out.reason and board.submissions == []


def test_a_chat_answer_that_is_not_one_of_the_options_stops_rather_than_guesses(host, board, files, tmp_path):
    out = run(host, board, files, tmp_path, answer=lambda q: "about four years" if q.options else "Built RAG pipelines.")
    assert out.state == NEEDS_YOU and board.submissions == []
    assert any("not one of the options" in n and "Snowflake" in n for n in out.needs)


def test_a_posting_that_is_not_the_job_is_not_filled(host, board, files, tmp_path):
    # the link leads to "Backend Engineer at Acme" but this job is a GitLab cloud role: a wrong link or a wrong resume
    a = applier(tmp_path)
    out = host.run(lambda c: a.apply(c, board.url("/acme/jobs/1"), form_ctx=ctx(chat_enabled=True), files=files,
                                     submit=True, tag="job1", company="GitLab", role="Cloud Platform Engineer"), timeout=180)
    assert out.state == NEEDS_YOU and "does not look like" in out.reason and board.submissions == []


def test_a_matching_posting_is_filled(host, board, files, tmp_path):
    a = applier(tmp_path)
    out = host.run(lambda c: a.apply(c, board.url("/acme/jobs/1"), form_ctx=ctx(chat_enabled=True), files=files,
                                     submit=False, tag="job1", company="Acme", role="Senior Backend Engineer"), timeout=180)
    assert out.state == DRY_RUN


def test_posting_matching_rules():
    from core.greenhouse import posting_matches

    page = "Cardiologist - AI Research Participants - United States (Remote) Job Application"
    url = "https://job-boards.greenhouse.io/embed/job_app?for=prolificacademicltd&token=1"
    assert not posting_matches("GitLab", "Cloud Platform Engineer", page, url)
    assert posting_matches("Prolific", "Cardiologist - AI Research Participants", page, url)
    assert posting_matches("Prolific", "Something Else Entirely", page, url)  # the company is in the board's address
    assert posting_matches("GitLab", "Senior Cloud Platform Engineer", "Cloud Platform Engineer Remote", "https://x/for=gitlab")
    assert posting_matches("", "", page, url)  # nothing to compare is not a reason to refuse


def test_a_dropdown_that_opens_only_on_the_arrow_key_is_still_read_and_filled(host, board, files, tmp_path):
    board.stubborn = 1  # a click or focus opens nothing; only the arrow key does
    notes: list[str] = []
    out = run(host, board, files, tmp_path, debug_log=notes.append)
    assert out.state == SUBMITTED, (out.reason, out.needs)
    assert board.submissions[0]["question_2"] == "Yes" and board.submissions[0]["gender"] == "Decline To Self Identify"
    assert any("the arrow key" in n for n in notes), notes  # how it had to be opened is recorded


def test_a_greenhouse_dropdown_opens_from_its_arrow_and_ignores_the_phone_country_list(host, board, files, tmp_path):
    board.stubborn = 3  # stays shut until the arrow is pressed; the choices are drawn on the page, beside a hidden phone list
    notes: list[str] = []
    out = run(host, board, files, tmp_path, debug_log=notes.append)
    assert out.state == SUBMITTED, (out.reason, out.needs)
    sent = board.submissions[0]
    assert sent["question_2"] == "Yes" and sent["gender"] == "Decline To Self Identify"
    assert "Austin, Texas" in sent["candidate-location"]
    assert not any("no choices appeared" in n and "authorized" in n.lower() for n in notes), notes


def test_a_dropdown_that_will_not_open_stops_the_application_and_records_its_structure(host, board, files, tmp_path):
    board.stubborn = 2  # nothing opens it
    notes: list[str] = []
    out = run(host, board, files, tmp_path, debug_log=notes.append)
    assert out.state == NEEDS_YOU and board.submissions == []
    assert any("Could not read the choices" in n for n in out.needs)
    log = "\n".join(notes)
    assert "no choices appeared. Structure:" in log and "select__input" in log and "menus_on_page" in log
    assert "mary@example.com" not in log  # structure only: no field values are logged


def test_a_company_page_with_a_cookie_pop_up_over_its_apply_button_still_opens_the_form(host, board, files, tmp_path):
    out = run(host, board, files, tmp_path, path="/cookie-careers?gh_jid=1")
    assert out.state == SUBMITTED, (out.reason, out.needs)
    assert len(board.submissions) == 1


def test_the_greenhouse_board_named_on_a_company_page_gives_the_direct_form_address(host, board, tmp_path):
    from core.greenhouse import GreenhouseApplier

    def task(context):
        page = context.new_page()
        try:
            page.goto(board.url("/jobs?gh_jid=8224111"))
            page.set_content('<script src="https://boards.greenhouse.io/embed/job_board/js?for=toast"></script>')
            return GreenhouseApplier(answer=lambda q: "")._embed_from_company_page(page)
        finally:
            page.close()
    assert host.run(task) == "https://job-boards.greenhouse.io/embed/job_app?for=toast&token=8224111"


def test_board_names_are_guessed_from_the_company_and_its_site():
    from core.greenhouse import board_guesses
    guesses = board_guesses("Toast", "https://careers.toasttab.com/jobs/x?gh_jid=8224111")
    assert guesses[0] == "toast" and "toasttab" in guesses and "careers" not in guesses
    assert board_guesses("Health-E Commerce", "")[:2] == ["healthecommerce", "health-e-commerce"]


def test_a_company_running_its_own_form_is_sent_to_greenhouse_once_the_api_confirms_the_board(host, board, tmp_path):
    from core.greenhouse import GreenhouseApplier

    def task(context):
        page = context.new_page()
        try:
            page.goto(board.url("/own-form?gh_jid=8224111"))
            a = GreenhouseApplier(answer=lambda q: "", board_api=board.url("/api/boards"))
            a._company = "Toast"
            return a._embed_from_board_lookup(page)
        finally:
            page.close()
    assert host.run(task) == "https://job-boards.greenhouse.io/embed/job_app?for=toast&token=8224111"


def _no_inbox(context):
    raise RuntimeError("Page.wait_for_selector: Timeout 30000ms exceeded.")


def test_an_inbox_that_will_not_open_does_not_stop_a_form_that_asks_for_no_code(host, board, files, tmp_path):
    out = run(host, board, files, tmp_path, snapshot_mail=_no_inbox, fetch_code=lambda ctx, b, c: "")
    assert out.state == SUBMITTED, (out.reason, out.needs)
    assert len(board.submissions) == 1


def test_an_inbox_that_will_not_open_is_named_when_the_form_does_ask_for_a_code(host, board, files, tmp_path):
    board.code_step = True
    out = run(host, board, files, tmp_path, snapshot_mail=_no_inbox, fetch_code=lambda ctx, b, c: board.valid_code)
    assert out.state == NEEDS_YOU and "Outlook" in out.reason and board.submissions == []


def test_a_page_whose_scripts_never_attached_is_a_failed_load_to_retry_not_questions_for_you(host, board, files, tmp_path, monkeypatch):
    monkeypatch.setattr(GreenhouseApplier, "_INTERACTIVE_JS", "() => false")
    board.stubborn = 2  # no dropdown opens, as on a page that never came alive
    out = run(host, board, files, tmp_path)
    assert out.state == "failed" and "never became usable" in out.reason and board.submissions == []


def test_questions_the_form_does_not_require_are_left_blank_and_the_application_still_goes(host, board, files, tmp_path):
    asked: list[str] = []

    def answer(q):
        asked.append(q.text)
        return q.options[2] if q.options else "Built RAG pipelines, retrieval, ranking and evals, in production."

    out = run(host, board, files, tmp_path, answer=answer, form_ctx=ctx(chat_enabled=True, skip_optional=True))
    assert out.state == SUBMITTED, (out.reason, out.needs)
    (sent,) = board.submissions
    # what the form requires is answered, as before
    assert sent["first_name"] == "Mary Ann" and sent["email"] == "mary@example.com"
    assert sent["resume"] == "Mary_Resume.pdf" and sent["question_5"].startswith("Built RAG")
    assert sent["snow"] == "3-5" and sent["privacy"] is True and sent["visa"] == "0"
    # what it does not require is not answered: phone, LinkedIn, gender, veteran status ...
    for name in ("phone", "question_1", "gender", "veteran_status"):
        assert not sent.get(name), f"{name} should have been left blank, got {sent.get(name)!r}"
    # ... and no question was put to the chat for them, but the cover letter (a file, not a question) is attached
    assert not any("Gender" in q or "Veteran" in q or "LinkedIn" in q for q in asked)
    assert sent["cover_letter"] == "Mary_Cover_Letter.docx"
    skipped = [p["label"] for p in out.plan if p["reason"] == "Optional: left blank"]
    assert "LinkedIn Profile" in skipped and "Gender" in skipped and "Phone" in skipped


def test_an_optional_question_is_still_answered_when_leaving_blanks_is_switched_off(host, board, files, tmp_path):
    out = run(host, board, files, tmp_path, form_ctx=ctx(chat_enabled=True, skip_optional=False))
    assert out.state == SUBMITTED, (out.reason, out.needs)
    assert board.submissions[0]["question_1"] == "https://linkedin.com/in/mary"


def test_a_chat_answer_about_missing_information_is_asked_again_and_never_typed_in():
    from core.form_answers import Context, Field
    from core.greenhouse import GreenhouseApplier
    from core.settings_store import ProfileSettings

    replies = iter(["Certifications not provided.", "NO_ANSWER"])
    applier = GreenhouseApplier(answer=lambda q: next(replies), board_api="")
    field = Field(ref="c", kind="textarea", label="Tell us about your on-call experience", required=True)
    d = applier._ask(field, Context(profile=ProfileSettings(), chat_enabled=True))
    assert d.action == "needs" and "No usable answer" in d.reason

    replies = iter(["Not provided.", "I led the on-call rotation for payments."])
    applier = GreenhouseApplier(answer=lambda q: next(replies), board_api="")
    d = applier._ask(field, Context(profile=ProfileSettings(), chat_enabled=True))
    assert (d.action, d.value) == ("fill", "I led the on-call rotation for payments.")

    optional = Field(ref="o", kind="text", label="Anything else?", required=False)
    applier = GreenhouseApplier(answer=lambda q: "I'm not sure", board_api="")
    assert applier._ask(optional, Context(profile=ProfileSettings(), chat_enabled=True)).action == "skip"


def test_a_machine_sounding_answer_is_asked_for_again_in_plain_words():
    from core.form_answers import Context, Field
    from core.greenhouse import GreenhouseApplier
    from core.settings_store import ProfileSettings

    asked = []

    def answer(q):
        asked.append(q)
        return ("I'm passionate about leveraging robust systems." if len(asked) == 1
                else "I built the billing service at Acme and cut failed payments by half.")

    applier = GreenhouseApplier(answer=answer, board_api="")
    field = Field(ref="w", kind="textarea", label="Why do you want this role?", required=True)
    d = applier._ask(field, Context(profile=ProfileSettings(), chat_enabled=True))
    assert d.value == "I built the billing service at Acme and cut failed payments by half."
    assert "passionate" in asked[1].hint
