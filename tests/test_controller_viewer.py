"""Controller <-> viewer link: the viewer reads a copy of the controller's database and sends changes back."""

from __future__ import annotations

import json
import ntpath
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest

from core.application_stage import Outcome, Stage
from core.applications import ApplicationError, ApplicationService
from core.browser_intake import IntakeServer, IntakeService
from core.models import JobItem, JobStatus, QueueStore
from core.settings_store import AppSettings, ProfileSettings
from core.sync_protocol import CommandRefused, decode, encode
from core.viewer_client import (
    ControllerClient,
    ControllerRejected,
    ControllerUnavailable,
    RemoteApplicationService,
    ViewerSync,
)
from core.viewer_files import FileMaterializer, make_path_mapper
from tests.resume_fixtures import make_resume

TOKEN = "link-token"


def _done_job(store, company, *, resume=True, files=()):
    job = JobItem.create(f"https://jobs.example/{company}")
    job.company, job.role_title, job.status = company, "Engineer", JobStatus.DONE
    job.completed_at = "2026-02-01T10:00:00+00:00"
    job.generated_files = list(files)
    job.output_path = ntpath.dirname(files[0]) if files else ""  # the queue records the folder next to the files
    if resume:
        job.resume_json = {**make_resume((2, 2)), "company_name": company, "role_title": "Engineer"}
    store.add(job)
    store.update(job)
    return job


@pytest.fixture
def link(tmp_path):
    controller = QueueStore(tmp_path / "controller" / "studio.db")
    settings = AppSettings(output_root=str(tmp_path / "controller" / "out"),
                           profile=ProfileSettings(name="Test Person", email="t@example.com"))
    service = IntakeService(controller, lambda: TOKEN, lambda d: {"status": "added"},
                            settings=lambda: settings, profile=lambda: settings.profile,
                            worker_state=lambda: "running")
    server = IntakeServer(service, port=0)
    assert server.start()
    base = f"http://127.0.0.1:{server.port}"
    replica = QueueStore(tmp_path / "viewer" / "studio.db", read_only=True)
    client = ControllerClient(lambda: base, lambda: TOKEN, timeout=10)
    profiles: list[dict] = []
    sync = ViewerSync(replica.database, client, on_profile=profiles.append)
    apps = RemoteApplicationService(replica.database)
    apps.attach(sync)

    class Link:
        pass

    L = Link()
    L.tmp, L.controller, L.service, L.server, L.base = tmp_path, controller, service, server, base
    L.replica, L.client, L.sync, L.apps, L.profiles, L.settings = replica, client, sync, apps, profiles, settings
    L.real_apps = ApplicationService(controller.database)
    yield L
    server.stop()
    replica.database.close()
    controller.database.close()


# ------------------------------------------------------------------------------ reading the copy


def test_viewer_gets_a_copy_and_only_downloads_again_when_something_changed(link):
    job = _done_job(link.controller, "Acme")
    first = link.sync.sync_once()
    assert first.changed and link.sync.connected and link.sync.worker == "running"
    assert [j.company for j in link.replica.jobs()] == ["Acme"]
    assert link.replica.get(job.id).resume_json["company_name"] == "Acme"

    assert not link.sync.sync_once().changed  # nothing new: no download

    link.real_apps.add_note(job.id, "controller side note")
    assert link.sync.sync_once().changed
    assert any(e["event_type"] == "note" for e in link.replica.list_events(job.id))


def test_the_copy_is_read_only(link):
    job = _done_job(link.controller, "Acme")
    link.sync.sync_once()
    copy = link.replica.get(job.id)
    copy.notes = "edited on the viewer"
    with pytest.raises(sqlite3.OperationalError):
        link.replica.update(copy)
    with pytest.raises(sqlite3.OperationalError):
        link.replica.add_event(job.id, "note", {})
    assert link.controller.get(job.id).notes == ""


def test_profile_and_worker_state_come_across(link):
    link.sync.sync_once()
    assert link.profiles and link.profiles[0]["profile"]["name"] == "Test Person"
    assert link.profiles[0]["output_root"].endswith("out")
    link.settings.profile.phone = "555"
    link.sync.sync_once()
    assert link.profiles[-1]["profile"]["phone"] == "555"


def test_a_failed_sync_keeps_the_current_copy(link):
    _done_job(link.controller, "Acme")
    link.sync.sync_once()
    link.server.stop()
    outcome = link.sync.sync_once()
    assert outcome.error and not link.sync.connected
    assert [j.company for j in link.replica.jobs()] == ["Acme"]


def test_wrong_token_and_wrong_address_are_reported_clearly(link):
    with pytest.raises(ControllerRejected, match="token"):
        ControllerClient(lambda: link.base, lambda: "nope").status()
    with pytest.raises(ControllerUnavailable):
        ControllerClient(lambda: "http://127.0.0.1:9", lambda: TOKEN, timeout=2).status()
    with pytest.raises(ControllerUnavailable, match="not set"):
        ControllerClient(lambda: "", lambda: TOKEN).status()


def test_a_controller_with_a_newer_database_is_refused(link):
    class Newer(ControllerClient):
        def status(self):
            return {**super().status(), "schema": 999}

    sync = ViewerSync(link.replica.database, Newer(lambda: link.base, lambda: TOKEN))
    outcome = sync.sync_once()
    assert "newer" in outcome.error and not sync.connected


def test_a_damaged_copy_is_never_installed(link):
    _done_job(link.controller, "Acme")
    link.sync.sync_once()
    _done_job(link.controller, "Globex")

    class Damaged(ControllerClient):
        def snapshot(self, have):
            token, schema, data = super().snapshot(have)
            return token, schema, b"not a database" + data[14:]

    sync = ViewerSync(link.replica.database, Damaged(lambda: link.base, lambda: TOKEN))
    outcome = sync.sync_once()
    assert not outcome.changed and ("integrity" in outcome.error or "unreadable" in outcome.error)
    assert [j.company for j in link.replica.jobs()] == ["Acme"]  # still the last good copy


# ------------------------------------------------------------------------------ sending changes


def test_marking_applied_on_the_viewer_changes_the_controllers_database(link):
    job = _done_job(link.controller, "Acme")
    link.sync.sync_once()
    assert link.apps.mark_applied(job.id) is True
    assert link.controller.get(job.id).applied_at  # the real database has it
    assert link.replica.get(job.id).applied_at  # and the viewer already shows it
    assert link.apps.mark_applied(job.id) is False  # the controller's own rule, relayed


def test_stages_outcomes_dates_and_tuples_survive_the_trip(link):
    a, b = _done_job(link.controller, "Acme"), _done_job(link.controller, "Globex")
    link.sync.sync_once()
    when = datetime(2026, 3, 1, 9, 30, tzinfo=timezone.utc)
    link.apps.mark_applied(a.id, when)
    assert link.apps.set_stage(a.id, Stage.SCREENING, when) is True
    assert link.replica.get(a.id).app_stage == "screening"
    link.apps.set_outcome(a.id, Outcome.REJECTED)
    assert link.controller.get(a.id).outcome == "rejected"
    done, skipped = link.apps.bulk_mark_applied([b.id, "missing"])
    assert (done, skipped) == (1, 1)
    iid = link.apps.schedule_interview(b.id, kind="screening", scheduled_at=when)
    assert link.apps.list_interviews(b.id)[0]["id"] == iid  # ids the controller made match the copy


def test_errors_from_the_controller_arrive_as_application_errors(link):
    job = _done_job(link.controller, "Acme")
    link.sync.sync_once()
    with pytest.raises(ApplicationError, match="Note is empty"):
        link.apps.add_note(job.id, "  ")
    link.server.stop()
    with pytest.raises(ApplicationError):  # also an ApplicationError, so existing screens show it
        link.apps.add_note(job.id, "hello")


def test_only_listed_commands_run_and_a_repeat_is_not_applied_twice(link):
    job = _done_job(link.controller, "Acme")
    run = link.service.run_command
    assert run({"kind": "app", "name": "db", "args": []})[0] == 400  # not a command
    assert run({"kind": "app", "name": "list_messages", "args": [job.id]})[0] == 400  # reads are not commands
    assert run({"kind": "queue", "name": "start", "args": []})[0] == 400  # run control stays on the controller
    assert run({"kind": "app", "name": "add_note", "args": "x"})[0] == 400
    body = {"kind": "app", "name": "add_note", "args": [job.id, "once"], "request_id": "r1"}
    assert run(body)[0] == 200 and run(body)[0] == 200
    assert [e["notes"] for e in link.controller.database.raw_events(job.id) if e["type"] == "note"] == ["once"]


def test_commands_need_the_token(link):
    reply = httpx.post(link.base + "/api/command", json={"kind": "app", "name": "add_note"}, timeout=10)
    assert reply.status_code == 401
    bad_host = httpx.post(link.base + "/api/command", json={}, timeout=10,
                          headers={"Authorization": f"Bearer {TOKEN}", "Host": "evil.example"})
    assert bad_host.status_code == 403


def test_remote_hosts_must_be_listed_by_the_owner(link):
    headers = {"Authorization": f"Bearer {TOKEN}", "Host": "vps.tailnet.example:47821"}
    assert httpx.get(link.base + "/api/sync/status", headers=headers, timeout=10).status_code == 403
    link.service.allowed_hosts = lambda: {"vps.tailnet.example:47821"}
    assert httpx.get(link.base + "/api/sync/status", headers=headers, timeout=10).status_code == 200


def test_tagged_values_round_trip_and_reject_forgery():
    value = [Stage.OFFER, Outcome.CLOSED, datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc), {"a": (1, 2)}]
    assert decode(json.loads(json.dumps(encode(value)))) == [Stage.OFFER, Outcome.CLOSED, value[2], {"a": [1, 2]}]
    with pytest.raises(CommandRefused):
        decode({"$stage": "nonsense"})
    with pytest.raises(CommandRefused):
        decode({"$stage": "applied", "extra": 1, "$dt": "x"})
    with pytest.raises(ValueError):
        encode({"$stage": 1})
    with pytest.raises(TypeError):
        encode(object())


# ------------------------------------------------------------------------------ documents


def test_controller_paths_are_translated_to_this_computers_output_folder(link, tmp_path):
    local = tmp_path / "my-resumes"
    mapper = make_path_mapper(lambda: r"C:\Users\Admin\Remote", lambda: local)
    assert mapper(r"C:\Users\Admin\Remote\Jane\Acme\Eng\Jane_Resume.pdf") == str(local / "Jane" / "Acme" / "Eng" / "Jane_Resume.pdf")
    assert mapper(r"c:\users\admin\remote\x.docx") == str(local / "x.docx")  # Windows paths ignore case
    assert mapper(r"D:\elsewhere\x.docx") == r"D:\elsewhere\x.docx"  # not under the controller's folder
    assert mapper("") == ""
    posix = make_path_mapper(lambda: "/srv/out", lambda: local)
    assert posix("/srv/out/a/b.pdf") == str(local / "a" / "b.pdf")


def test_documents_are_rebuilt_from_the_copy_without_writing_to_it(link, tmp_path):
    from core.job_export import sync_jobs_to_disk

    root = r"C:\Users\Admin\Remote"
    local = tmp_path / "my-resumes"
    job = _done_job(link.controller, "Acme", files=[rf"{root}\Test Person\Acme\Engineer\Test_Person_Resume.docx",
                                                    rf"{root}\Test Person\Acme\Engineer\Test_Person_Resume.pdf"])
    link.replica.database.path_mapper = make_path_mapper(lambda: root, lambda: local)
    link.sync.sync_once()

    mapped = link.replica.get(job.id)
    assert all(p.startswith(str(local)) for p in mapped.generated_files)
    assert mapped.output_path.startswith(str(local))

    events_before = len(link.replica.database.raw_events(job.id))
    settings = AppSettings(output_root=str(local), profile=link.settings.profile)
    [result] = sync_jobs_to_disk(link.replica, settings, [job.id], record=False)
    assert result.ok and len(result.restored) == 2
    assert all(p.startswith(str(local)) for p in result.restored)
    assert all((tmp_path / "my-resumes" / "Test Person" / "Acme" / "Engineer" / n).is_file()
               for n in ("Test_Person_Resume.docx", "Test_Person_Resume.pdf"))
    assert len(link.replica.database.raw_events(job.id)) == events_before  # the copy was not touched
    assert len(link.controller.database.raw_events(job.id)) == events_before


def test_only_newly_finished_jobs_are_rebuilt_automatically(link, tmp_path):
    state = tmp_path / "viewer_state.json"
    old = _done_job(link.controller, "Old Co")
    link.sync.sync_once()
    files = FileMaterializer(state)
    assert files.due(link.replica.database) == []  # first connection: existing jobs are not flooded onto the disk

    new = _done_job(link.controller, "New Co")
    link.sync.sync_once()
    assert files.due(link.replica.database) == [new.id]
    files.record([type("R", (), {"job_id": new.id, "ok": True})()])
    assert files.due(link.replica.database) == []

    again = FileMaterializer(state)  # remembered across restarts
    assert again.due(link.replica.database) == []
    assert old.id in again.seen and new.id in again.seen


def test_a_job_that_keeps_failing_is_left_for_the_manual_button(link, tmp_path):
    files = FileMaterializer(tmp_path / "state.json")
    link.sync.sync_once()
    files.due(link.replica.database)
    bad = _done_job(link.controller, "Broken Co")
    link.sync.sync_once()
    failure = type("R", (), {"job_id": bad.id, "ok": False})()
    for _ in range(3):
        assert files.due(link.replica.database) == [bad.id]
        files.record([failure])
    assert files.due(link.replica.database) == []
