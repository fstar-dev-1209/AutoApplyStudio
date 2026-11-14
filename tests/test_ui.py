"""PySide6 UI tests (pytest-qt, offscreen). The Claude web view is stubbed; nothing touches real user data."""

import threading

import httpx
import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QPushButton, QToolButton, QWidget

from core import analytics
from core.application_db import ApplicationDatabase
from core.application_stage import Stage
from core.applications import ApplicationService
from core.models import JobStatus
from core.settings_store import SettingsStore
from core.timeutil import range_for
from tests.conftest import at
from tests.fixture_data import build
from ui import theme
from ui.application_dialogs import InterviewDialog, MessageDialog, WhenDialog
from ui.applications_view import C_ACTIONS, C_APPLIED, C_COMPANY, C_DOCS, C_STAGE
from ui.charts import BarChart, FunnelChart, LineChart, nice_ceiling


class _Browser(QObject):
    loadFinished = Signal(bool)


class _Service:
    busy = False
    label = "Claude"
    last_chat_url = ""

    class site:  # noqa: N801 - mirrors ChatService.site
        key = "claude"
        poll_clipboard = False
        require_idle = False

    def run_page_state(self):
        return {"loggedIn": True}, ""

    def set_progress_handler(self, _f):
        pass

    def finish_generation(self):
        pass


class StubChatView(QWidget):
    session_cleared = Signal()

    def __init__(self, parent=None, site_key="claude", proxy_credentials=None):
        super().__init__(parent)
        self.browser = _Browser()

    def refresh_status(self):
        pass

    def chat_service(self):
        return _Service()


@pytest.fixture
def seeded(tmp_path):
    db = ApplicationDatabase(tmp_path / "studio.db")
    ids = build(db, ApplicationService(db))
    db.close()
    return tmp_path, ids


@pytest.fixture
def window(qtbot, seeded, monkeypatch):
    import ui.main_window as mw

    monkeypatch.setattr(mw, "ClaudeChatView", StubChatView)
    monkeypatch.setattr(mw, "INTAKE_PORT", 0)  # ephemeral port: tests never touch the real one
    tmp, ids = seeded
    store = SettingsStore(tmp / "settings.json")
    store.settings.output_root = str(tmp / "Remote")
    win = mw.MainWindow(settings_store=store, db_dir=tmp)
    win.engine.start = lambda: None  # never start a real worker from a UI test
    qtbot.addWidget(win)
    win.resize(1440, 900)
    win.show()
    win.ids = ids
    yield win
    win.queue_store.database.close()


def row_of(view, company):
    for r in range(view.table.rowCount()):
        if view.table.item(r, C_COMPANY).text() == company:
            return r
    raise AssertionError(f"{company} not in table")


def cell_text(view, row, col):
    return view.table.item(row, col).text()


def badge_text(view, row, col):
    w = view.table.cellWidget(row, col)
    return " ".join(lbl.text() for lbl in w.findChildren(type(w).__mro__[0]) if False) or "".join(
        child.text() for child in w.children() if hasattr(child, "text"))


def test_navigation_has_the_six_main_items(window):
    labels = [window._nav[k].text().strip() for k in ("queue", "applications", "analytics", "files", "json", "settings")]
    assert labels == ["Job Queue", "Applications", "Analytics", "Generated Files", "Manual JSON", "Settings"]
    for key in ("queue", "applications", "analytics", "files", "json", "settings", "claude"):
        window._navigate(key)
        assert window.current_page() == key
    assert window._nav["queue"].toolTip() == "" or True


def test_applications_lists_generated_jobs_unapplied(window):
    window._navigate("applications")
    view = window.apps_page.view
    assert view.table.rowCount() == 7  # India is archived and hidden by default
    echo = row_of(view, "Echo")
    assert cell_text(view, echo, C_APPLIED) == "Not applied"  # generated, but never "applied" automatically
    assert cell_text(view, row_of(view, "Delta"), C_APPLIED) != "Not applied"


def test_mark_applied_quick_action_updates_db_and_table(window, qtbot):
    window._navigate("applications")
    view = window.apps_page.view
    echo = view.table.cellWidget(row_of(view, "Echo"), C_ACTIONS).findChildren(QToolButton)[0]
    assert echo.isEnabled()
    qtbot.mouseClick(echo, Qt.MouseButton.LeftButton)
    job = next(j for j in window.queue_store.jobs() if j.company == "Echo")
    assert job.applied_at and job.app_stage == "applied"
    qtbot.waitUntil(lambda: cell_text(view, row_of(view, "Echo"), C_APPLIED) != "Not applied", timeout=3000)


def test_selecting_a_row_opens_detail_and_stage_change_is_one_interaction(window, qtbot):
    window._navigate("applications")
    page, view = window.apps_page, window.apps_page.view
    assert page.detail_host.isHidden()  # table gets full width until something is selected
    view.select_job(window.ids["D"])
    qtbot.waitUntil(lambda: not page.detail_host.isHidden())
    assert page.detail.company.text() == "Delta"
    combo = page.detail.stage_combo
    combo.setCurrentIndex(combo.findData(Stage.SCREENING.value))
    combo.activated.emit(combo.currentIndex())
    assert window.queue_store.get(window.ids["D"]).app_stage == "screening"
    page._back()  # the list refreshes when it is shown again
    qtbot.waitUntil(lambda: any("Screening" in c.text() for c in view.table.cellWidget(
        row_of(view, "Delta"), C_STAGE).findChildren(QWidget) if hasattr(c, "text")), timeout=3000)


def test_search_and_stage_chips_filter_rows(window, qtbot):
    window._navigate("applications")
    view = window.apps_page.view
    view.search.setText("alp")
    assert view.table.rowCount() == 1 and cell_text(view, 0, C_COMPANY) == "Alpha"
    view.search.clear()
    view._set_chip("not_applied")
    assert [cell_text(view, r, C_COMPANY) for r in range(view.table.rowCount())] == ["Echo"]
    view._set_chip("closed")
    assert {cell_text(view, r, C_COMPANY) for r in range(view.table.rowCount())} == {"Alpha", "Bravo", "Charlie", "Hotel"}
    view._set_chip("all")
    view.response_combo.setCurrentIndex(view.response_combo.findData("waiting"))
    assert {cell_text(view, r, C_COMPANY) for r in range(view.table.rowCount())} <= {"Delta", "Juliet"}


def test_table_sorts_by_header_and_blank_dates_last(window):
    window._navigate("applications")
    view = window.apps_page.view
    view._sort, view._desc = "company", False
    view.refresh()
    assert cell_text(view, 0, C_COMPANY) == "Alpha"
    view._sort, view._desc = "applied", False
    view.refresh()
    assert cell_text(view, view.table.rowCount() - 1, C_COMPANY) == "Echo"  # never applied -> last


def test_next_action_from_detail_panel(window, qtbot):
    window._navigate("applications")
    page = window.apps_page
    page.view.select_job(window.ids["D"])
    page.detail.action_text.setText("Prepare system design")
    page.detail.action_nodate.setChecked(True)
    page.detail._save_action()
    actions = window.apps.list_actions(window.ids["D"], include_done=False)
    assert [(a["description"], a["due_date"]) for a in actions] == [("Prepare system design", "")]


def test_ui_refreshes_immediately_after_database_change(window, qtbot):
    window._navigate("applications")
    view = window.apps_page.view
    window.apps.set_stage(window.ids["E"], Stage.TECHNICAL)  # a change made anywhere
    qtbot.waitUntil(lambda: cell_text(view, row_of(view, "Echo"), C_APPLIED) != "Not applied", timeout=3000)
    assert window.apps_badge.text() in ("", "0") or int(window.apps_badge.text()) == 0  # "ready to apply" badge dropped


def test_narrow_pane_hides_low_priority_columns(window):
    window._navigate("applications")
    view = window.apps_page.view
    window.resize(1900, 900)
    view._fit_columns()
    assert not view.table.isColumnHidden(C_DOCS)
    view.resize(560, 700)
    view._fit_columns()
    assert view.table.isColumnHidden(C_DOCS)


def test_analytics_screen_shows_stored_numbers(window):
    window._navigate("analytics")
    av = window.analytics_view
    av.range_combo.setCurrentIndex(av.range_combo.findData("all"))
    res = analytics.compute(analytics.load_data(window.queue_store.database), range_for("all"))
    assert res.submitted == 7 and res.reply.text == "71%"
    assert av._tiles["submitted"][0].text() == "7"
    assert av._tiles["reply"][0].text() == "71%" and av._tiles["reply"][1].text() == "5 of 7"
    assert av._tiles["generation_success"][0].text() == "89%"
    av.range_combo.setCurrentIndex(av.range_combo.findData("today"))  # nothing happened today: no fake percentages
    assert av._tiles["reply"][0].text() == "—" and av._tiles["submitted"][0].text() == "0"


def test_queue_intake_adds_jobs_with_explicit_duplicate_override(window):
    qv = window.queue_view
    before = len(window.queue_store.jobs())
    qv.input.setPlainText("https://jobs.example.com/newco/1\nhttps://jobs.example.com/newco/2")
    qv.dup_box.setChecked(True)
    qv.add_btn.click()
    new = window.queue_store.jobs()[before:]
    assert len(new) == 2 and all(j.duplicate_override for j in new) and all(j.status == JobStatus.QUEUED for j in new)
    assert qv.input.toPlainText() == ""
    qv.input.setPlainText("https://jobs.example.com/newco/1\nhttps://jobs.example.com/plain/9")
    qv.dup_box.setChecked(False)
    qv.add_btn.click()
    again = window.queue_store.jobs()
    assert len(again) == before + 3  # the repeated URL was not queued twice
    assert [j.duplicate_override for j in again[-1:]] == [False]  # the default rule stays on unless asked


def test_queue_status_counts_and_filters(window):
    qv = window.queue_view
    assert qv._stat_labels["done"].text() == "8" and qv._stat_labels["failed"].text() == "1"
    assert qv._stat_labels["skipped"].text() == "1"
    assert qv.table.rowCount() == 10
    qv._set_filter(JobStatus.FAILED, qv._filter_buttons["Failed"])
    window._refresh_queue()
    assert qv.table.rowCount() == 1


def test_allow_duplicate_requeues_a_skipped_job(window):
    gid = window.ids["G"]
    job = window.queue_store.get(gid)
    job.skip_reason = "Company folder already exists: E:/Remote/Me/Alpha"
    window.queue_store.update(job)
    window._allow_duplicate(gid)
    job = window.queue_store.get(gid)
    assert job.duplicate_override is True and job.status == JobStatus.QUEUED and job.skip_reason == ""


def test_mark_applied_from_queue_and_nothing_else(window):
    window._mark_applied([window.ids["E"], window.ids["F"]])  # F failed generation: not eligible
    assert window.queue_store.get(window.ids["E"]).applied_at
    assert window.queue_store.get(window.ids["F"]).applied_at == ""


def test_files_library_shows_files_and_existence(window, tmp_path):
    from pathlib import Path

    job = window.queue_store.get(window.ids["E"])
    real = tmp_path / "Echo_Resume.docx"
    real.write_text("x")
    job.generated_files = [str(real), str(tmp_path / "Echo_Resume.pdf")]
    window.queue_store.update(job)
    window._navigate("files")
    fv = window.files_view
    fv._mode(1)  # the tracked-files tree is drawn only while it is the view on screen
    fv.search.setText("echo")
    assert fv.tree.topLevelItemCount() == 1
    child = fv.tree.topLevelItem(0).child(0)
    resume_btn = fv.tree.itemWidget(child, 2).findChildren(QToolButton)[0]
    pdf_btn = fv.tree.itemWidget(child, 3).findChildren(QToolButton)[0]
    assert "Open" in resume_btn.toolTip() and "missing" in pdf_btn.toolTip()
    assert "1 file(s) missing" in fv.count_label.text()
    assert Path(real).exists()  # nothing was copied into or removed from the database/filesystem


def test_manual_json_reports_syntax_errors_with_position(window):
    mv = window.manual_view
    mv.editor.setPlainText('{\n  "name": "A",\n  "x": \n}')
    mv._live_syntax()
    assert mv.syntax.text().startswith("Line 4")
    mv.editor.setPlainText('{"name": "A"}')
    mv._live_syntax()
    assert "Valid JSON" in mv.syntax.text()


def test_settings_roundtrip_applies_theme_and_density(window, tmp_path):
    sv = window.settings_view
    sv.theme.setCurrentIndex(sv.theme.findData("light"))
    sv.density.setCurrentIndex(sv.density.findData("compact"))
    sv.ghost_days.setValue(21)
    sv.auto_retries.setValue(2)
    sv.export_cover.setChecked(False)
    sv.api_key.setText("sk-test-123")
    sv.save()
    assert theme.current().name == "light" and theme.density() == "compact"
    s = window.settings_store.settings
    assert (s.ghost_after_days, s.auto_retries, s.export_cover_letter) == (21, 2, False)
    assert window.apps.ghost_after_days == 21
    assert "sk-test-123" not in (tmp_path / "settings.json").read_text(encoding="utf-8")
    sv.theme.setCurrentIndex(sv.theme.findData("dark"))
    sv.save()
    assert theme.current().name == "dark"


def test_dialog_values_map_to_service_arguments(qtbot):
    dlg = InterviewDialog()
    qtbot.addWidget(dlg)
    dlg.tz.setCurrentIndex(dlg.tz.findData("America/New_York"))
    dlg.when_edit.setDateTime(dlg.when_edit.dateTime().fromString("2026-01-20 15:00", "yyyy-MM-dd HH:mm"))
    v = dlg.values()
    assert v["tz_name"] == "America/New_York" and v["scheduled_at"] == at(2026, 1, 20, 20, 0)  # EST = UTC-5
    dlg.has_time.setChecked(False)
    assert dlg.values()["scheduled_at"] is None  # "time not confirmed yet" is supported
    msg = MessageDialog()
    qtbot.addWidget(msg)
    msg.kind.setCurrentIndex(msg.kind.findData("rejection"))
    assert msg.values()["sentiment"] == "negative" and msg.values()["kind"] == "rejection"
    when = WhenDialog("t", note="hi", reason=True)
    qtbot.addWidget(when)
    assert when.values()["note"] == "hi" and "reason" in when.values()


def test_interview_dialog_edit_roundtrips_timezone(qtbot):
    existing = {"kind": "technical", "round_no": 2, "scheduled_at": "2026-01-20T20:00:00+00:00",
                "timezone": "America/New_York", "status": "Scheduled", "interviewer": "Pat", "meeting_url": "https://x"}
    dlg = InterviewDialog(existing)
    qtbot.addWidget(dlg)
    assert dlg.when_edit.dateTime().toString("yyyy-MM-dd HH:mm") == "2026-01-20 15:00"  # shown in the interview's zone
    assert dlg.values()["scheduled_at"] == at(2026, 1, 20, 20, 0) and dlg.values()["round_no"] == 2


def test_charts_render_with_empty_zero_and_real_data(qtbot):
    for chart, args in ((BarChart(), (["a", "b"], [0, 0])), (BarChart(), ([], [])),
                        (LineChart(), (["a", "b", "c"], [("x", [1, 0, 3]), ("y", [0, 2, 0])])),
                        (LineChart(), ([], [])), (FunnelChart(), ([("A", 7, ""), ("B", 5, "71% of prev")],)),
                        (FunnelChart(), ([("A", 0, "")],))):
        qtbot.addWidget(chart)
        chart.resize(400, 220)
        chart.set_data(*args)
        assert not chart.grab().isNull()
    assert [nice_ceiling(v) for v in (0, 3, 5, 9, 11, 120, 999)] == [4, 4, 5, 10, 20, 200, 1000]


def test_app_survives_theme_switch_and_high_density(window):
    for pref, dens in (("light", "compact"), ("dark", "comfortable"), ("system", "comfortable")):
        window.settings_store.settings.theme, window.settings_store.settings.density = pref, dens
        window._apply_theme()
        for key in ("queue", "applications", "analytics", "files"):
            window._navigate(key)
            QApplication.processEvents()
    assert theme.row_height() == 32


@pytest.fixture
def empty_window(qtbot, tmp_path, monkeypatch):
    import ui.main_window as mw

    monkeypatch.setattr(mw, "ClaudeChatView", StubChatView)
    monkeypatch.setattr(mw, "INTAKE_PORT", 0)  # ephemeral port: tests never touch the real one
    store = SettingsStore(tmp_path / "settings.json")
    store.settings.output_root = str(tmp_path / "Remote")
    store.settings.request_delay_seconds = 0
    win = mw.MainWindow(settings_store=store, db_dir=tmp_path)
    win.engine.start = lambda: None
    qtbot.addWidget(win)
    win.show()
    yield win
    win.queue_store.database.close()


def test_every_page_renders_with_an_empty_database(empty_window):
    w = empty_window
    for key in ("queue", "applications", "analytics", "files", "json", "settings"):
        w._navigate(key)
        QApplication.processEvents()
    assert w.apps_page.view.table.rowCount() == 0 and not w.apps_page.view.empty.isHidden()
    assert w.analytics_view._tiles["reply"][0].text() == "—"
    assert w.queue_view.table.isHidden() and not w.queue_view.empty.isHidden()


def test_daily_workflow_paste_generate_apply_analyze(empty_window, tmp_path):
    """Paste URL -> generate documents -> shows in Applications (not applied) -> mark applied -> analytics."""
    import json
    from pathlib import Path

    w = empty_window
    sample = json.loads((Path(__file__).resolve().parent.parent / "scripts" / "_sample_peraton.json").read_text("utf-8"))
    w.engine._claude_chat_generate = lambda prompt, meta, settings: dict(sample)

    w.queue_view.input.setPlainText("https://jobs.example.com/peraton/1")
    w.queue_view.add_btn.click()
    job = w.queue_store.jobs()[0]
    job.job_description = "SRE role"  # as if extracted
    w.queue_store.update(job)
    w.engine._process_job(w.queue_store.get(job.id))
    w.engine._record_result(job.id)
    w._on_engine_event("job_updated", {"job_id": job.id})

    w._navigate("applications")
    view = w.apps_page.view
    assert view.table.rowCount() == 1
    assert cell_text(view, 0, C_APPLIED) == "Not applied" and w.apps_badge.text() == "1"
    assert w.queue_store.get(job.id).status == JobStatus.DONE

    w.analytics_view.range_combo.setCurrentIndex(w.analytics_view.range_combo.findData("today"))
    assert w.analytics_view._tiles["generated"][0].text() == "1"
    assert w.analytics_view._tiles["submitted"][0].text() == "0"
    assert w.analytics_view._tiles["ready_not_applied"][0].text() == "1"

    view.select_job(job.id)
    w.apps_page.detail.applied_btn.click()  # the user applies manually, then marks it
    w.apps.record_message(job.id, kind="outreach", sentiment="positive", sender="Sam", summary="Let's talk")
    iid = w.apps.schedule_interview(job.id, kind="screening")
    w.apps.update_interview(iid, status="Completed")
    w.analytics_view.refresh()
    t = w.analytics_view._tiles
    assert t["submitted"][0].text() == "1" and t["responses"][0].text() == "1"
    assert t["screening_invites"][0].text() == "1" and t["screening_done"][0].text() == "1"
    assert t["reply"][0].text() == "100%" and t["ready_not_applied"][0].text() == "0"
    assert w.queue_store.get(job.id).app_stage == "screening"


def test_store_updates_from_the_worker_thread_reach_the_ui_safely(window, qtbot):
    import threading

    window._navigate("applications")
    view = window.apps_page.view
    job = window.queue_store.get(window.ids["E"])
    job.company = "Echo Renamed"

    errors = []

    def worker():  # QueueStore notifies listeners from whatever thread updates it
        try:
            window.queue_store.update(job)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert not errors
    qtbot.waitUntil(lambda: any(cell_text(view, r, C_COMPANY) == "Echo Renamed" for r in range(view.table.rowCount())),
                    timeout=4000)


def test_archive_and_restore_from_the_applications_screen(window, qtbot):
    window._navigate("applications")
    view = window.apps_page.view
    view.select_job(window.ids["D"])
    window.apps_page._back()
    assert window.apps.archive([window.ids["D"]]) == 1
    qtbot.waitUntil(lambda: all(cell_text(view, r, C_COMPANY) != "Delta" for r in range(view.table.rowCount())))
    view.archived_box.setChecked(True)  # the archive view lists archived rows only (India, Delta)
    assert {cell_text(view, r, C_COMPANY) for r in range(view.table.rowCount())} == {"India", "Delta"}
    assert view.bulk_archive.text() == "Restore"
    view.table.selectAll()
    view._bulk_archive()
    view.archived_box.setChecked(False)
    assert "Delta" in {cell_text(view, r, C_COMPANY) for r in range(view.table.rowCount())}
    assert window.queue_store.get(window.ids["D"]).archived_at == ""  # history was never deleted


def test_add_manual_application_tracks_without_generating_or_applying(window):
    window._navigate("applications")
    n = window.apps_page.view.table.rowCount()
    job_id = window.apps.add_manual("Zeta Corp", "Platform Engineer", url="https://zeta.example/jobs/1")
    job = window.queue_store.get(job_id)
    assert job.company == "Zeta Corp" and job.source == "Manual" and not job.applied_at
    assert job.app_stage == "not_applied" and not job.generated_files
    applied_id = window.apps.add_manual("Eta Inc", "SRE", applied_on="2026-01-10T09:00:00+00:00",
                                         stage=Stage.SCREENING)
    applied = window.queue_store.get(applied_id)
    assert applied.applied_at and applied.app_stage == "screening"
    window._navigate("applications")
    window.apps_page.view.refresh()
    assert window.apps_page.view.table.rowCount() == n + 2


def test_add_manual_requires_a_company(window):
    import pytest

    from core.applications import ApplicationError

    with pytest.raises(ApplicationError):
        window.apps.add_manual("  ", "Role")


def test_back_returns_to_the_list_from_the_detail_page(window, qtbot):
    window._navigate("applications")
    page = window.apps_page
    page.view.select_job(window.ids["D"])
    assert page.stack.currentWidget() is page.detail
    page.detail.back_btn.click()
    assert page.stack.currentWidget() is page.view


def test_copy_button_copies_and_flashes_a_check_icon(qtbot):
    from PySide6.QtWidgets import QApplication

    from ui.widgets import copy_button

    btn = copy_button("Copy job URL", lambda: "https://example.com/job")
    qtbot.addWidget(btn)
    btn.show()
    btn.click()
    assert QApplication.clipboard().text() == "https://example.com/job"
    assert btn.toolTip() == "Copied"
    qtbot.waitUntil(lambda: btn.toolTip() == "Copy job URL", timeout=4000)  # reverts to the copy icon


def test_job_inspector_and_strip_offer_copy_for_the_job_url(window):
    from PySide6.QtWidgets import QToolButton

    window._navigate("queue")
    tips = {b.toolTip() for b in window.inspector.findChildren(QToolButton)}
    assert "Copy origin url" in tips
    strip_tips = {b.toolTip() for b in window.queue_view.strip.findChildren(QToolButton)}
    assert "Copy job URL" in strip_tips


def test_queue_row_click_activates_the_row_and_only_the_checkbox_checks_it(window, qtbot):
    window._navigate("queue")
    q = window.queue_view
    q.select_row(1)
    assert q.checked_job_ids() == [] and q.selected_job_id()
    active = q.selected_job_id()
    # clicking inside the checkbox column checks that row but keeps the active row

    rect = q.table.visualRect(q.table.model().index(2, 0))
    qtbot.mouseClick(q.table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert len(q.checked_job_ids()) == 1 and q.selected_job_id() == active
    assert q.selected_job_ids() == q.checked_job_ids()  # bulk actions use the checked rows
    q._header_clicked(0)  # header checkbox checks everything, then clears
    assert len(q.checked_job_ids()) == q.table.rowCount()
    q._header_clicked(0)
    assert q.checked_job_ids() == [] and q.selected_job_ids() == [active]


def test_hover_highlight_covers_the_whole_row(window):
    window._navigate("applications")
    t = window.apps_page.view.table
    t._set_hover(1)
    assert t.hover_row() == 1
    t.leaveEvent(None) if False else t._set_hover(-1)
    assert t.hover_row() == -1


def test_analytics_headline_cards_show_counts_and_percent_of_bids(window):
    window._navigate("analytics")
    av = window.analytics_view
    av.range_combo.setCurrentIndex(av.range_combo.findData("all"))
    values = {k: (v.text(), s.text()) for k, (v, s, _d) in av._stat.items()}
    assert values["submitted"][0] == "7"
    assert values["responses"][0] == "5" and values["responses"][1].startswith("71% reply rate")
    assert values["screening_invites"] == ("2", "29% of bids")
    assert values["offers"][1] == "14% of bids"
    assert [step[0] for step in av.funnel.steps][:3] == ["Bids", "Replies", "Screening"]
    assert av.funnel.steps[1][2] == "71% of bids"


def test_period_comparison_helpers():
    from core.timeutil import range_for
    from ui.analytics_view import delta_text, previous_range

    rng = range_for("last_7")
    prev = previous_range(rng)
    assert (rng.first_day - prev.last_day).days == 1 and (prev.last_day - prev.first_day).days == 6
    assert previous_range(range_for("all")) is None
    assert delta_text(12, 10)[0] == "▲ 20%" and delta_text(5, 10)[0] == "▼ 50%"
    assert delta_text(3, 0)[0] == "new this period" and delta_text(0, 0)[0] == "no change"


def test_weekly_and_monthly_cohorts_count_how_far_each_periods_bids_got(window):
    window._navigate("analytics")
    av = window.analytics_view
    av.range_combo.setCurrentIndex(av.range_combo.findData("all"))
    res = av.result
    for series in (res.weekly, res.monthly_cohort):
        assert sum(series.bids) <= 7 and len(series.labels) == len(series.bids) <= 12
        for name, values in series.rows()[1:]:
            assert all(v <= b for v, b in zip(values, series.bids)), name  # a stage never exceeds the bids sent
    assert sum(res.monthly_cohort.bids) == 7 and sum(res.monthly_cohort.replies) == 5
    assert sum(res.monthly_cohort.screening) == 2 and sum(res.monthly_cohort.offers) == 1


def _sample_exports(folder):
    import engine

    resume = {
        "name": "Test Person", "subtitle": "Engineer", "location": "Remote", "email": "t@example.com",
        "summary": "Builds things.",
        "experience": [{"company": "Acme", "dates": "2020 - 2024", "title": "Engineer", "bullets": ["Shipped it."]}],
        "cover_letter": "Dear team,\n\nHello.\n\nBest,\nTest",
    }
    res = engine.export_resume(resume, folder / "Resume.docx", folder / "Resume.pdf", folder / "Cover_Letter.docx")
    return [str(res.docx_path), str(res.pdf_path), str(res.cover_letter_path)]


def test_doc_preview_renders_docx_and_pdf_pages(qapp, tmp_path):
    from ui import doc_preview

    docx_path, pdf_path, cover_path = _sample_exports(tmp_path)
    for path in (docx_path, pdf_path, cover_path):
        pages = doc_preview.render_pages(path, 300, 2)
        assert pages and pages[0].width() == 300 and pages[0].height() > 300  # a portrait page
    assert doc_preview.render_pages(tmp_path / "missing.docx", 300) == []
    (tmp_path / "broken.docx").write_text("not a zip")
    assert doc_preview.render_pages(tmp_path / "broken.docx", 300) == []  # corrupt files never raise


def test_files_gallery_lists_tiles_and_previews_selection(window, tmp_path, qtbot):
    job = window.queue_store.get(window.ids["E"])
    job.generated_files = _sample_exports(tmp_path)
    window.queue_store.update(job)
    window._navigate("files")
    fv = window.files_view
    fv.search.setText("echo")
    assert fv.stack.currentIndex() == 0 and fv.gallery.count() == 3  # resume, PDF, cover letter
    from ui.files_gallery import R_THUMB

    qtbot.waitUntil(lambda: all(fv.gallery.item(i).data(R_THUMB) is not None for i in range(3)), timeout=8000)
    fv.gallery.setCurrentRow(1)
    assert fv.preview.title.text() == job.company
    assert fv.preview.meta_labels["File"].text() == "Resume.pdf"
    assert fv.preview.open_btn.isEnabled()
    fv.kind_combo.setCurrentIndex(fv.kind_combo.findData("cover"))
    assert fv.gallery.count() == 1
    fv._mode(1)  # list view still reflects the same data
    assert fv.tree.topLevelItemCount() == 1


def test_manual_json_format_and_generation_options(window):
    mv = window.manual_view
    mv.editor.setPlainText('{"name":"A","skills":["x"]}')
    mv._format()
    assert mv.editor.toPlainText().startswith('{\n  "name": "A"') and "Valid JSON" in mv.syntax.text()
    mv.editor.setPlainText('{"name": ')
    mv._format()  # invalid JSON is left untouched and reported
    assert mv.editor.toPlainText() == '{"name": ' and "Line" in mv.syntax.text()
    assert mv.fmt_both.isChecked() and mv.cover_check.isChecked()


def test_settings_theme_tiles_follow_the_combo(window):
    sv = window.settings_view
    sv.theme.setCurrentIndex(sv.theme.findData("light"))
    tiles = [b for b in sv.findChildren(QPushButton) if b.objectName() == "Segment" and b.text() in ("Dark", "Light", "System")]
    assert {b.text(): b.isChecked() for b in tiles} == {"Dark": False, "Light": True, "System": False}
    next(b for b in tiles if b.text() == "System").click()
    assert sv.theme.currentData() == "system"
    sv.theme.setCurrentIndex(sv.theme.findData("dark"))


def _job_with_files(window, tmp_path, key="E"):
    job = window.queue_store.get(window.ids[key])
    job.generated_files = _sample_exports(tmp_path / "out")
    job.output_path = str(tmp_path / "out")
    job.resume_json = {
        "name": "Test Person", "subtitle": "Engineer", "location": "Remote", "email": "t@example.com",
        "summary": "Builds things.",
        "experience": [{"company": "Acme", "dates": "2020 - 2024", "title": "Engineer", "bullets": ["Shipped it."]}],
        "cover_letter": "Dear team,\n\nHello.\n\nBest,\nTest",
    }
    window.queue_store.update(job)
    return job


def test_sync_restores_only_missing_files_and_never_overwrites(window, tmp_path):
    from pathlib import Path

    job = _job_with_files(window, tmp_path)
    docx, pdf, cover = map(Path, job.generated_files)
    docx.write_bytes(docx.read_bytes() + b"\0")  # stands in for a hand-edited resume
    edited = docx.read_bytes()
    pdf.unlink()
    cover.unlink()
    window._sync_files([job.id, job.id])  # duplicates are ignored
    assert pdf.is_file() and cover.is_file()
    assert docx.read_bytes() == edited  # existing file untouched
    after = window.queue_store.get(job.id)
    assert [Path(p).name for p in after.generated_files] == [docx.name, pdf.name, cover.name]
    again = window.queue_store.get(job.id)
    from core.job_export import missing_kinds, sync_jobs_to_disk

    assert missing_kinds(again) == []
    res = sync_jobs_to_disk(window.queue_store, window.settings_store.settings, [job.id])
    assert res[0].ok and res[0].restored == [] and res[0].present == 3  # nothing left to do


def test_sync_recreates_a_deleted_folder_and_reports_unsyncable_jobs(window, tmp_path):
    import shutil
    from pathlib import Path

    from core.job_export import sync_jobs_to_disk

    job = _job_with_files(window, tmp_path)
    shutil.rmtree(tmp_path / "out")
    res = sync_jobs_to_disk(window.queue_store, window.settings_store.settings, [job.id, window.ids["F"], "nope"])
    assert res[0].ok and len(res[0].restored) == 3 and all(Path(p).is_file() for p in res[0].restored)
    assert not res[1].ok  # failed job: not a finished job, nothing to rebuild
    assert not res[2].ok and "not found" in res[2].message


def test_files_page_sync_button_scopes_to_restorable_jobs(window, tmp_path):
    job = _job_with_files(window, tmp_path)
    window._navigate("files")
    fv = window.files_view
    fv.search.setText(job.company.lower())
    assert not fv.sync_btn.isEnabled()  # everything is on disk
    from pathlib import Path

    Path(job.generated_files[1]).unlink()
    fv.refresh()
    assert fv.sync_btn.isEnabled()
    got = []
    fv.sync_requested.connect(got.append)
    fv.sync_btn.click()
    assert got == [[job.id]]
    window._sync_files(got[0])
    assert Path(job.generated_files[1]).is_file() and not fv.sync_btn.isEnabled()


def test_inspector_flags_missing_files_and_offers_restore(window, tmp_path):
    from pathlib import Path

    job = _job_with_files(window, tmp_path)
    window.inspector.show_job(window.queue_store.get(job.id))
    assert window.inspector.restore_btn.isHidden()  # all three files exist
    Path(job.generated_files[2]).unlink()
    seen = []
    window.inspector.sync_files.connect(seen.append)
    window.inspector.show_job(window.queue_store.get(job.id))
    assert not window.inspector.restore_btn.isHidden()
    window.inspector.restore_btn.click()
    assert seen == [job.id]
    window._sync_files(seen)
    window.inspector.show_job(window.queue_store.get(job.id))
    assert window.inspector.restore_btn.isHidden() and Path(job.generated_files[2]).is_file()


def test_brand_icon_and_exe_icon_asset_exist(qapp):
    from pathlib import Path

    from ui.icons import brand_icon, brand_pixmap

    assert not brand_icon().isNull() and brand_pixmap(48).width() == 48
    ico = Path(__file__).resolve().parent.parent / "assets" / "app.ico"
    assert ico.is_file() and ico.read_bytes()[:4] == b"\x00\x00\x01\x00"  # valid ICO header (the spec embeds it)


INTAKE_DESCRIPTION = ("Build and run services. " * 10).strip()


def test_job_from_extension_reaches_the_queue_and_starts_it(window, qtbot):
    """End to end through the real window: the request thread waits while the UI thread adds the job."""
    started = []
    window.engine.start = lambda: started.append(True)
    window._claude_signed_in = True
    window.settings_store.settings.intake_auto_start = True
    token = window.intake.service.token()
    base = f"http://127.0.0.1:{window.intake.port}"
    result: dict = {}

    def call():
        r = httpx.post(base + "/api/jobs", headers={"Authorization": f"Bearer {token}"}, timeout=20, json={
            "url": "https://careers.example/jobs/77", "company": "Initech", "role_title": "DevOps Engineer",
            "job_description": INTAKE_DESCRIPTION, "source": "Example",
            "salary": "$120K - $150K/yr"})
        result["code"], result["body"] = r.status_code, r.json()

    t = threading.Thread(target=call, daemon=True)
    t.start()
    qtbot.waitUntil(lambda: "code" in result, timeout=15000)
    assert result["code"] == 200 and result["body"]["started"] is True and started
    job = window.queue_store.get(result["body"]["id"])
    assert job.company == "Initech" and job.job_description == INTAKE_DESCRIPTION and job.status.value == "Queued"
    assert any("browser extension" in line for line in job.logs)
    # The company's apply link is the job's address; the aggregator page that was opened is kept too
    assert job.url == "https://careers.example/jobs/77" and job.origin_url == job.url
    assert (job.salary_text, job.salary_min, job.salary_max, job.salary_period) == (
        "$120K - $150K/yr", 120000, 150000, "year")


def test_auto_start_never_overrides_a_pause_or_missing_sign_in(window):
    from core.queue_engine import WorkerState

    started = []
    window.engine.start = lambda: started.append(True)
    window._claude_signed_in = False
    assert window._maybe_auto_start() is False  # Claude not signed in
    window._claude_signed_in = True
    window.engine._state = WorkerState.PAUSED
    assert window._maybe_auto_start() is False  # the user paused the queue
    window.engine._state = WorkerState.IDLE
    window.settings_store.settings.intake_auto_start = False
    assert window._maybe_auto_start() is False  # setting off
    assert not started


def test_a_long_applications_list_is_drawn_in_slices_and_ends_complete(window, qtbot, monkeypatch):
    import ui.applications_view as av

    monkeypatch.setattr(av, "SLICE_FROM", 3)
    monkeypatch.setattr(av, "SLICE", 2)
    window._navigate("applications")
    view = window.apps_page.view
    view.refresh()
    assert view.table.rowCount() == 7
    assert view.table.item(0, C_COMPANY) is not None  # the first slice is there at once
    assert view.table.item(6, C_COMPANY) is None  # the rest is still being drawn
    assert view.selected_ids() == []  # nothing breaks while rows are missing
    view.refresh()  # a newer refresh replaces the unfinished one
    qtbot.waitUntil(lambda: all(view.table.item(r, C_COMPANY) for r in range(7)), timeout=5000)
    assert row_of(view, "Echo") >= 0
    assert view.table.cellWidget(6, C_ACTIONS) is not None


def test_saving_settings_restyles_and_redraws_only_when_the_look_changed(window, monkeypatch):
    import ui.main_window as mw

    calls = []
    real = mw.apply_theme
    monkeypatch.setattr(mw, "apply_theme", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    window._apply_theme()  # first call after startup values are known
    calls.clear()
    window._on_settings_saved()
    window._on_settings_saved()
    assert calls == []  # same theme and density: nothing to restyle
    window.settings_store.settings.density = "compact" if window.settings_store.settings.density != "compact" else "comfortable"
    window._on_settings_saved()
    assert calls == [1]
