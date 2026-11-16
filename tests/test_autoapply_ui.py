"""The Auto Apply page and settings: what they show, what they save, and that the app wires the bot up safely."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from PySide6.QtWidgets import QComboBox, QDialog, QPushButton, QToolButton

import ui.main_window as mw
from core.apply_runner import ApplyStatus, AttemptStore
from core.discovery import DiscoveryService, Listing
from core.discovery_runner import DiscoveryStatus
from core.greenhouse import DRY_RUN, NEEDS_YOU, ApplyOutcome
from core.models import JobItem, JobStatus, QueueStore
from core.settings_store import AppSettings, SettingsStore
from tests.test_ui import StubChatView
from ui.autoapply_view import AutoApplyView
from ui.settings_view import SettingsView

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def parts(tmp_path):
    store = QueueStore(tmp_path / "studio.db")
    settings = SettingsStore(tmp_path / "settings.json")
    yield store, settings, tmp_path
    store.database.close()


def job(store, company="Acme", n=1):
    j = JobItem.create(f"https://boards.greenhouse.io/acme/jobs/{n}")
    j.company, j.role_title, j.status = company, "Backend Engineer", JobStatus.DONE
    store.add_many([j])
    return j


def view_for(parts, qtbot, readonly=False):
    store, settings, _ = parts
    v = AutoApplyView(settings, AttemptStore(store), DiscoveryService(store), readonly=readonly)
    qtbot.addWidget(v)
    v.show()
    return v


def stopped_attempt(store, j, reason="Which office would you prefer?"):
    attempts = AttemptStore(store)
    attempt = attempts.start(j.id, "greenhouse", "auto", NOW)
    attempts.finish(attempt, ApplyOutcome(NEEDS_YOU, reason), NOW)
    return attempt


def test_retry_runs_without_an_answer_and_a_dropdown_answer_is_optional(qtbot, parts, monkeypatch):
    store, settings, _ = parts
    held = job(store, "Acme", 1)
    plain = job(store, "Plain", 2)
    checked = job(store, "Checked", 3)
    attempt = stopped_attempt(store, held)
    AttemptStore(store).finish(attempt, ApplyOutcome(
        NEEDS_YOU, "Not covered by your facts: Which office would you prefer?",
        plan=[{"label": "Which office would you prefer?", "action": "needs",
               "reason": "Not covered by your facts: Which office would you prefer?",
               "options": ["Austin", "Remote"]}]), NOW)
    attempts = AttemptStore(store)
    attempts.finish(attempts.start(checked.id, "greenhouse", "dry_run", NOW),
                    ApplyOutcome(DRY_RUN, "Filled and checked"), NOW)
    v = view_for(parts, qtbot)
    v.refresh()
    assert v.tabs.tabText(0) == "To apply (2)"
    assert {v.queue_table.item(r, 0).text() for r in range(v.queue_table.rowCount())} == {"Acme", "Plain"}
    assert "office" in v.queue_table.item(0, 2).text() or "office" in v.queue_table.item(1, 2).text()
    assert v.ready_table.rowCount() == 1 and v.ready_table.item(0, 0).text() == "Checked"
    assert v.tabs.tabText(1) == "Ready to submit (1)"

    class Stub(QDialog):
        def __init__(self, company, role, questions, parent=None):
            super().__init__(parent)
            self.questions = questions

        def exec(self):  # noqa: A003
            return QDialog.DialogCode.Accepted

        def answers(self):
            return [("Which office would you prefer?", "Remote", ["Austin", "Remote"])]

    monkeypatch.setattr("ui.autoapply_view.AnswerDialog", Stub)
    started = []
    v.apply_now_requested.connect(lambda job_id, submit: started.append((job_id, submit)))
    acme = next(r for r in range(v.queue_table.rowCount()) if v.queue_table.item(r, 0).text() == "Acme")
    cell = v.queue_table.cellWidget(acme, 3)
    labeled = cell.findChildren(QPushButton)
    assert [b.text() for b in labeled] == ["Retry", "Answer"]
    next(b for b in labeled if b.text() == "Retry").click()
    assert started == [(held.id, False)]
    next(b for b in labeled if b.text() == "Answer").click()
    assert settings.settings.form_questions[0]["answer"] == "Remote"
    assert started == [(held.id, False)]  # saving an answer does not start another try

    sent = []
    v.apply_now_requested.connect(lambda job_id, submit: sent.append((job_id, submit)))
    ready_buttons = v.ready_table.cellWidget(0, 3).findChildren(QPushButton)
    next(b for b in ready_buttons if b.text() == "Submit").click()
    assert sent == [(checked.id, True)]
    assert plain.id  # the job that was never tried is the other row on To apply


def test_a_viewer_sees_the_same_lists_but_gets_no_actions(qtbot, parts):
    store, _, _ = parts
    stopped_attempt(store, job(store))
    v = view_for(parts, qtbot, readonly=True)
    v.refresh()
    assert v.queue_table.rowCount() == 1
    assert len(v.queue_table.cellWidget(0, 3).findChildren(QToolButton)) == 1  # only "open the posting"
    assert not v.mode.isEnabled() and not v.kill_btn.isEnabled() and not v.save_answers.isEnabled()


def test_history_and_found_jobs_tables(qtbot, parts):
    store, _, _ = parts
    attempts = AttemptStore(store)
    a = job(store)
    attempts.finish(attempts.start(a.id, "greenhouse", "dry_run", NOW), ApplyOutcome(DRY_RUN, "Filled and checked"), NOW)
    DiscoveryService(store).ingest("jobright", [
        Listing("j1", title="SRE", company="Beta", apply_url="https://boards.greenhouse.io/beta/jobs/2"),
        Listing("j2", title="Dev", company="Gamma", apply_url="https://jobs.lever.co/gamma/x")])
    v = view_for(parts, qtbot)
    v.refresh()
    assert v.history_table.rowCount() == 1 and v.history_table.item(0, 1).text() == "Acme"
    assert v.history_table.item(0, 4).text() == "Filled and checked"
    assert {v.found_table.item(r, 2).text() for r in range(v.found_table.rowCount())} == {"SRE", "Dev"}


def test_facts_are_saved_cleaned_and_sealed_by_the_app_password(qtbot, parts):
    _, settings, tmp = parts
    v = view_for(parts, qtbot)
    v._fact_widgets["salary_expectation"].setText("  $150,000  ")
    v._fact_widgets["relocation"].setCurrentIndex(v._fact_widgets["relocation"].findData("No"))
    v.answer_prompt.setPlainText("Write like me.")
    v.save_facts()
    assert settings.settings.apply_facts == {"salary_expectation": "$150,000", "relocation": "No"}
    assert SettingsStore(tmp / "settings.json").settings.apply_facts["relocation"] == "No"

    settings.enable_protection("correct horse battery")
    plain = json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
    assert "apply_facts" not in plain and "$150,000" not in json.dumps(plain)
    reopened = SettingsStore(tmp / "settings.json")  # sealed on disk, and read back once unlocked
    reopened.lock.unlock("correct horse battery")
    reopened.load()
    assert reopened.settings.apply_facts["salary_expectation"] == "$150,000"


def test_greenhouse_questions_are_dropdowns_and_a_companys_question_can_be_answered(qtbot, parts):
    _, settings, tmp = parts
    settings.settings.form_questions = [{
        "label": "Which office would you prefer?", "options": ["Austin", "Remote office"], "answer": ""}]
    v = view_for(parts, qtbot)
    assert isinstance(v._fact_widgets["eeo_race"], QComboBox)
    assert v._fact_widgets["eeo_race"].findText("Asian") >= 0
    assert v._fact_widgets["eeo_veteran"].findText("I am not a protected veteran") >= 0
    assert v._fact_widgets["eeo_orientation"].findText("Heterosexual / Straight") >= 0
    assert v._fact_widgets["non_compete"].findText("No") >= 0
    combo = next(iter(v._asked_widgets.values()))
    combo.setCurrentIndex(combo.findData("Remote office"))
    v._fact_widgets["eeo_veteran"].setCurrentIndex(
        v._fact_widgets["eeo_veteran"].findData("I am not a protected veteran"))
    v.save_facts()
    assert settings.settings.apply_facts["eeo_veteran"] == "I am not a protected veteran"
    assert settings.settings.form_questions[0]["answer"] == "Remote office"
    settings.enable_protection("correct horse battery")
    plain = json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
    dumped = json.dumps(plain)
    assert "form_questions" not in plain and "apply_facts" not in plain
    assert "Remote office" not in dumped and "protected veteran" not in dumped
    reopened = SettingsStore(tmp / "settings.json")
    reopened.lock.unlock("correct horse battery")
    reopened.load()
    assert reopened.settings.form_questions[0]["answer"] == "Remote office"
    assert reopened.settings.apply_facts["eeo_veteran"] == "I am not a protected veteran"


def test_status_tiles_reflect_the_bot(qtbot, parts):
    v = view_for(parts, qtbot)
    v.set_status(browser_running=True, discovery=DiscoveryStatus(state="idle", message="2 queued"),
                 apply=ApplyStatus(state="running", current="Acme - Backend", submitted_today=7), killed=False,
                 mode="auto", cap=40, problems=[])
    assert v.browser_value.text() == "Open" and v.apply_value.text() == "Running"
    assert v.today_value.text() == "7 / 40" and v.apply_sub.text() == "Acme - Backend"
    assert v.mode.currentData() == "auto" and v.kill_btn.text() == "Stop now" and not v.problem.isVisible()

    v.set_status(browser_running=False, discovery=DiscoveryStatus(state="needs_login", message="sign in"),
                 apply=ApplyStatus(state="stopped"), killed=True, mode="off", cap=40, problems=["Discord refused"])
    assert v.kill_btn.text() == "Resume" and v.disc_value.text() == "Needs login" and "Discord" in v.problem.text()
    stops, resumes = [], []
    v.kill_requested.connect(lambda: stops.append(1))
    v.resume_requested.connect(lambda: resumes.append(1))
    v.kill_btn.click()
    assert resumes == [1] and stops == []


# ------------------------------------------------------------------ settings

def test_auto_apply_settings_round_trip_and_secrets_stay_out_of_the_file(qtbot, parts):
    _, settings, tmp = parts
    sv = SettingsView(settings)
    qtbot.addWidget(sv)
    sv.show()
    sv.disc_enabled.setChecked(True)
    sv.disc_interval.setValue(7)
    sv.disc_cap.setValue(12)
    sv.disc_blocked.setText("Staffing Co, Bad Corp")
    sv.bot_tz.setText("America/Chicago")
    sv.hour_from.setValue(8)
    sv.hour_to.setValue(20)
    sv.apply_cap.setValue(25)
    sv.discord_hook.setText("https://discord.com/api/webhooks/1/secrettoken")
    sv.tg_token.setText("123:TELEGRAMSECRET")
    sv.tg_chat.setText("777")
    sv.save()
    s = settings.settings
    assert (s.discovery_enabled, s.discovery_interval_minutes, s.bot_timezone) == (True, 7, "America/Chicago")
    assert s.discovery_hourly_cap == 12
    assert (s.active_start_hour, s.active_end_hour, s.apply_daily_cap) == (8, 20, 25)
    assert settings.notify_secret("discord_webhook").endswith("secrettoken") and s.notify_telegram_chat == "777"
    raw = (tmp / "settings.json").read_text(encoding="utf-8")
    assert "secrettoken" not in raw and "TELEGRAMSECRET" not in raw
    assert sv.discord_hook.text() == "" and "Saved" in sv.discord_hook.placeholderText()
    reloaded = SettingsStore(tmp / "settings.json").settings
    assert reloaded.discovery_blocked_companies == "Staffing Co, Bad Corp" and reloaded.apply_daily_cap == 25


def test_bad_values_in_an_old_or_edited_settings_file_fall_back_safely():
    s = AppSettings.from_dict({"discovery_interval_minutes": "x", "apply_daily_cap": 99999, "bot_timezone": "Mars/Base",
                               "apply_mode": "yolo", "active_end_hour": 0, "discovery_url": "javascript:alert(1)",
                               "apply_facts": {"relocation": "maybe", "city": "Austin"}})
    assert (s.discovery_interval_minutes, s.apply_daily_cap, s.bot_timezone) == (60, 500, "America/New_York")
    assert (s.apply_mode, s.active_end_hour, s.discovery_url) == ("off", 1, "")
    assert s.apply_facts == {"city": "Austin"}
    assert AppSettings().apply_mode == "off" and AppSettings().discovery_enabled is False  # everything starts off


# ------------------------------------------------------------------ the window

@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "ClaudeChatView", StubChatView)
    monkeypatch.setattr(mw, "INTAKE_PORT", 0)
    store = SettingsStore(tmp_path / "settings.json")
    store.settings.output_root = str(tmp_path / "Remote")
    win = mw.MainWindow(settings_store=store, db_dir=tmp_path)
    win.engine.start = lambda: None
    qtbot.addWidget(win)
    win.resize(1400, 900)
    win.show()
    yield win
    win.queue_store.database.close()


def test_the_window_has_an_auto_apply_page_and_starts_the_bot_switched_off(window):
    assert "autoapply" in window._nav and window.bot is not None
    window._navigate("autoapply")
    assert window.current_page() == "autoapply" and window.autoapply_view.isVisible()
    assert window.settings_store.settings.apply_mode == "off"
    assert not window.bot.browser_running, "the bot browser must not open until it is needed"


def test_choosing_auto_asks_first_and_declining_changes_nothing(window, monkeypatch):
    answers = iter([False, True])
    monkeypatch.setattr(mw, "ask_yes_no", lambda *a, **k: next(answers))
    window._set_apply_mode("auto")
    assert window.settings_store.settings.apply_mode == "off"
    window._set_apply_mode("auto")
    assert window.settings_store.settings.apply_mode == "auto"
    window._set_apply_mode("dry_run")  # the safer modes never ask
    assert window.settings_store.settings.apply_mode == "dry_run"


def test_new_jobs_found_by_the_bot_start_generation(window):
    started = []
    window._maybe_auto_start = lambda: started.append(1) or True
    j = JobItem.create("https://boards.greenhouse.io/acme/jobs/5")
    window.queue_store.add_many([j])
    window._on_engine_event("bot_queued", {"ids": [j.id]})
    assert started == [1]


def test_i_applied_myself_marks_the_job_and_clears_the_waiting_item(window):
    j = job(window.queue_store, "Zeta", 9)
    attempts = window.bot.apply.attempts
    attempt = stopped_attempt(window.queue_store, j, "Needs a human")
    assert len(attempts.needs_you()) == 1
    window._applied_by_hand(j.id, attempt)
    assert window.queue_store.get(j.id).applied_at and attempts.needs_you() == []


def test_the_profile_fields_forms_ask_for_are_editable_on_this_page_and_shared_with_settings(qtbot, parts):
    _, settings, tmp = parts
    v = view_for(parts, qtbot)
    v._profile_widgets["linkedin_url"].setText(" https://linkedin.com/in/mary ")
    v._profile_widgets["github_url"].setText("https://github.com/mary")
    v._profile_widgets["work_authorization"].setCurrentIndex(v._profile_widgets["work_authorization"].findData("Yes"))
    v._profile_widgets["needs_sponsorship"].setCurrentIndex(v._profile_widgets["needs_sponsorship"].findData("No"))
    v.save_facts()
    p = settings.settings.profile
    assert (p.linkedin_url, p.github_url, p.work_authorization, p.needs_sponsorship) == (
        "https://linkedin.com/in/mary", "https://github.com/mary", "Yes", "No")
    assert SettingsStore(tmp / "settings.json").settings.profile.github_url == "https://github.com/mary"
    # and Settings shows the same values
    sv = SettingsView(settings)
    qtbot.addWidget(sv)
    assert sv.linkedin.text() == "https://linkedin.com/in/mary" and sv.sponsorship.text() == "No"
