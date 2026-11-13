"""The real windows in each mode: a viewer talking to a controller, and a controller applying a viewer's requests.

The viewer window runs against a controller service in this process; the controller window's own UI-thread
hand-off is exercised separately, because a viewer and a controller sharing one thread would wait on each other.
"""

from __future__ import annotations

import ntpath
import threading

import httpx
import pytest

from core.applications import ApplicationService
from core.browser_intake import IntakeServer, IntakeService
from core.models import JobItem, JobStatus, QueueStore
from core.settings_store import AppSettings, ProfileSettings, SettingsStore
from tests.fixture_data import build
from tests.resume_fixtures import make_resume
from tests.test_ui import StubChatView

TOKEN = "window-token"
CONTROLLER_ROOT = r"C:\Users\Admin\Remote"


@pytest.fixture
def controller(tmp_path):
    """A headless controller holding a realistic job search, reachable over HTTP."""
    store = QueueStore(tmp_path / "controller" / "studio.db")
    apps = ApplicationService(store.database)
    ids = build(store.database, apps)
    settings = AppSettings(output_root=CONTROLLER_ROOT, profile=ProfileSettings(name="Test Person", email="t@example.com"))

    def hook(kind, name, args, kwargs):
        if kind == "app":
            return getattr(apps, name)(*args, **kwargs)
        if name == "remove_jobs":
            store.remove(list(args[0]))
            return len(args[0])
        if name == "skip_job":
            job = store.get(args[0])
            job.set_status(JobStatus.SKIPPED, "Skipped")
            store.update(job)
            return None
        raise AssertionError(name)

    service = IntakeService(store, lambda: TOKEN, lambda d: {"status": "added"}, settings=lambda: settings,
                            profile=lambda: settings.profile, worker_state=lambda: "running", command=hook)
    server = IntakeServer(service, port=0)
    assert server.start()

    class Controller:
        pass

    c = Controller()
    c.store, c.apps, c.ids, c.server, c.settings = store, apps, ids, server, settings
    c.base = f"http://127.0.0.1:{server.port}"
    yield c
    server.stop()
    store.database.close()


@pytest.fixture
def viewer(qtbot, controller, tmp_path, monkeypatch):
    import ui.main_window as mw

    monkeypatch.setattr(mw, "INTAKE_PORT", 0)
    home = tmp_path / "viewer"
    home.mkdir()
    store = SettingsStore(home / "settings.json")
    store.settings.app_mode = "viewer"
    store.settings.controller_url = controller.base
    store.settings.output_root = str(home / "out")
    store.set_controller_token(TOKEN)
    win = mw.MainWindow(settings_store=store, db_dir=home)
    qtbot.addWidget(win)
    win.resize(1440, 900)
    win.show()
    win.home = home
    qtbot.waitUntil(lambda: win.viewer.sync.connected and win.queue_view.table.rowCount() > 0, timeout=20000)
    yield win
    win.close()


def test_viewer_shows_the_controllers_data_and_has_no_generation(viewer, controller):
    assert viewer.is_viewer and viewer.claude_view is None and viewer.page_renderer is None
    assert viewer.queue_store.read_only and not (viewer.home / "studio.db").exists()  # its own copy, nothing else
    assert viewer.queue_view.table.rowCount() == len(controller.store.jobs())
    for button in (viewer.queue_view.start_btn, viewer.queue_view.pause_btn, viewer.queue_view.stop_btn):
        assert button.isHidden()
    assert viewer.queue_view.worker_label.text() == "Controller: running"
    assert viewer.api_title.text() == "Controller" and viewer.api_state.text() == "Connected"
    viewer._navigate("claude")
    assert viewer.current_page() == "settings"  # there is no Claude page to open


def test_run_control_and_claude_actions_are_refused_in_the_viewer(viewer):
    from core.applications import ApplicationError

    for action in (viewer.engine.start, viewer.engine.pause, viewer.engine.stop):
        with pytest.raises(ApplicationError, match="controller"):
            action()
    inspector = viewer.inspector
    viewer.queue_view.select_row(0)
    inspector.show_job(viewer.queue_store.get(viewer.queue_view.selected_job_id()))
    for button in (inspector.copy_prompt_btn, inspector.import_json_btn, inspector.pull_claude_btn,
                   inspector.copy_claude_json_btn, inspector.retry_export_btn, inspector.retry_gen_btn):
        assert button.isHidden()


def test_marking_applied_in_the_viewer_updates_the_controllers_database(viewer, controller, qtbot):
    echo = controller.ids["E"]  # documents ready, not applied
    assert not controller.store.get(echo).applied_at
    viewer._mark_applied([echo])
    assert controller.store.get(echo).applied_at  # the one real database changed
    qtbot.waitUntil(lambda: bool(viewer.queue_store.get(echo).applied_at), timeout=10000)  # and the copy shows it


def test_the_application_screens_work_through_the_viewer(viewer, controller):
    viewer.apps.add_note(controller.ids["D"], "called the recruiter")
    notes = [e for e in controller.store.database.raw_events(controller.ids["D"]) if e["type"] == "note"]
    assert [n["notes"] for n in notes] == ["called the recruiter"]
    assert viewer.apps.get_row(controller.ids["D"]) is not None  # reads come from the local copy


def test_skip_and_remove_are_relayed(viewer, controller, qtbot, monkeypatch):
    import ui.main_window as mw

    monkeypatch.setattr(mw, "ask_yes_no", lambda *a, **k: True)
    failed, skipped = controller.ids["F"], controller.ids["G"]
    viewer._remove_jobs([failed, controller.ids["A"]])  # the applied job is never removable
    assert controller.store.get(failed) is None and controller.store.get(controller.ids["A"]) is not None
    viewer._skip_job(controller.ids["E"])
    assert controller.store.get(controller.ids["E"]).status == JobStatus.SKIPPED
    assert controller.store.get(skipped) is not None


def test_an_unreachable_controller_is_reported_not_raised(viewer, controller, qtbot):
    controller.server.stop()
    viewer.viewer.sync_soon()
    qtbot.waitUntil(lambda: not viewer.viewer.sync.connected, timeout=20000)
    qtbot.waitUntil(lambda: viewer.api_state.text() == "Offline", timeout=10000)
    viewer._mark_applied([controller.ids["E"]])  # shows a message; must not raise
    from core.applications import ApplicationError

    with pytest.raises(ApplicationError, match="Cannot reach"):  # the screens catch this and show it
        viewer.apps.add_note(controller.ids["D"], "x")
    assert not controller.store.get(controller.ids["E"]).applied_at
    assert viewer.queue_view.table.rowCount() > 0  # the last copy is still on screen


def test_newly_finished_resumes_are_saved_to_this_computer(viewer, controller, qtbot):
    assert viewer.viewer.latest_profile and viewer.materializer.baselined  # existing jobs were taken as handled
    files = [rf"{CONTROLLER_ROOT}\Test Person\Newco\Engineer\Test_Person_Resume.docx",
             rf"{CONTROLLER_ROOT}\Test Person\Newco\Engineer\Test_Person_Resume.pdf"]
    job = JobItem.create("https://jobs.example/newco")
    job.company, job.role_title, job.status = "Newco", "Engineer", JobStatus.DONE
    job.generated_files, job.output_path = files, ntpath.dirname(files[0])
    job.resume_json = {**make_resume((2, 2)), "company_name": "Newco", "role_title": "Engineer"}
    controller.store.add(job)
    controller.store.update(job)
    target = viewer.home / "out" / "Test Person" / "Newco" / "Engineer"
    qtbot.waitUntil(lambda: (target / "Test_Person_Resume.docx").is_file()
                    and (target / "Test_Person_Resume.pdf").is_file(), timeout=30000)
    assert viewer.queue_store.get(job.id).generated_files[0].startswith(str(viewer.home / "out"))
    assert len(viewer.queue_store.database.raw_events(job.id)) == len(controller.store.database.raw_events(job.id))


def test_files_you_deleted_stay_deleted_until_you_press_sync(viewer, controller, qtbot, monkeypatch):
    done = controller.ids["E"]
    viewer.materializer.seen.add(done)  # handled earlier
    folder = viewer.home / "out"
    assert not folder.exists() or not any(folder.rglob("*.docx"))
    viewer._materialize_new_files()
    assert not folder.exists() or not any(folder.rglob("*.docx"))  # not resurrected automatically


# ------------------------------------------------------------------------------ controller window


def _app_window(qtbot, home, monkeypatch, mode):
    import ui.main_window as mw

    monkeypatch.setattr(mw, "ClaudeChatView", StubChatView)
    monkeypatch.setattr(mw, "INTAKE_PORT", 0)
    home.mkdir()
    db = QueueStore(home / "studio.db")
    ids = build(db.database, ApplicationService(db.database))
    db.database.close()
    store = SettingsStore(home / "settings.json")
    store.settings.app_mode = mode
    store.settings.output_root = str(home / "Remote")
    win = mw.MainWindow(settings_store=store, db_dir=home)
    win.engine.start = lambda: None  # never start a real worker from a UI test
    qtbot.addWidget(win)
    win.show()
    win.ids = ids
    return win


@pytest.fixture
def controller_window(qtbot, tmp_path, monkeypatch):
    win = _app_window(qtbot, tmp_path / "ctrl", monkeypatch, "controller")
    yield win
    win.queue_store.database.close()


@pytest.fixture
def standalone_window(qtbot, tmp_path, monkeypatch):
    win = _app_window(qtbot, tmp_path / "solo", monkeypatch, "standalone")
    yield win
    win.queue_store.database.close()


def _ask_controller(win, qtbot, path, body, method="POST"):
    out: dict = {}
    token = win.intake.service.token()
    base = f"http://127.0.0.1:{win.intake.port}"

    def call():
        r = httpx.request(method, base + path, headers={"Authorization": f"Bearer {token}"}, json=body if method == "POST" else None, timeout=30)
        out["code"], out["body"] = r.status_code, r.json() if "json" in r.headers.get("content-type", "") else r.content

    threading.Thread(target=call, daemon=True).start()
    qtbot.waitUntil(lambda: "code" in out, timeout=30000)
    return out["code"], out["body"]


def test_a_controller_applies_a_viewers_requests_on_its_ui_thread(controller_window, qtbot):
    win = controller_window
    code, body = _ask_controller(win, qtbot, "/api/command", {
        "kind": "queue", "name": "add_urls", "args": [["https://a.example/1", "https://a.example/1", "not a link"], False]})
    assert code == 200 and body["result"]["added"] == 1 and body["result"]["skipped"] == 2
    assert any(j.url == "https://a.example/1" for j in win.queue_store.jobs())

    code, body = _ask_controller(win, qtbot, "/api/command", {
        "kind": "app", "name": "add_note", "args": [win.ids["D"], "from the viewer"]})
    assert code == 200
    code, body = _ask_controller(win, qtbot, "/api/command", {"kind": "app", "name": "add_note", "args": [win.ids["D"], "  "]})
    assert code == 422 and "Note is empty" in body["error"]  # the error crosses back with its real meaning

    removable = win.ids["F"]
    code, body = _ask_controller(win, qtbot, "/api/command", {"kind": "queue", "name": "remove_jobs",
                                                              "args": [[removable, win.ids["A"]]]})
    assert code == 200 and body["result"] == 1 and win.queue_store.get(removable) is None
    assert win.queue_store.get(win.ids["A"]) is not None  # applied jobs are history, never removed


def test_a_controller_serves_its_database_only_in_controller_mode(controller_window, qtbot, standalone_window):
    code, body = _ask_controller(controller_window, qtbot, "/api/sync/status", None, method="GET")
    assert code == 200 and body["role"] == "controller" and body["token"]
    code, body = _ask_controller(controller_window, qtbot, "/api/sync/db", None, method="GET")
    assert code == 200 and body[:2] == b"\x1f\x8b"  # gzip
    code, body = _ask_controller(standalone_window, qtbot, "/api/sync/status", None, method="GET")
    assert code == 404
    code, body = _ask_controller(standalone_window, qtbot, "/api/command", {"kind": "app", "name": "add_note"})
    assert code == 404


def test_only_a_controller_listens_beyond_this_computer(controller_window, standalone_window):
    controller_window.settings_store.settings.intake_bind = "0.0.0.0"
    assert controller_window._bind_host() == "0.0.0.0"
    standalone_window.settings_store.settings.intake_bind = "0.0.0.0"  # ignored outside controller mode
    assert standalone_window._bind_host() == "127.0.0.1"
    controller_window.settings_store.settings.intake_allowed_hosts = "100.64.0.5:47821; VPS.example:47821"
    assert controller_window._allowed_hosts() == {"100.64.0.5:47821", "vps.example:47821"}
