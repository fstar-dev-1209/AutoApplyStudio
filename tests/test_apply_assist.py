"""Job apply assist: the endpoints behind extension autofill. Nothing here may submit or mark applied by itself."""

from __future__ import annotations

import base64

import httpx
import pytest

from core import apply_kit
from core.browser_intake import IntakeServer, IntakeService
from core.lifecycle import EV, replay
from core.models import JobItem, JobStatus, QueueStore
from core.settings_store import AppSettings, ProfileSettings

TOKEN = "test-token"
PROFILE = ProfileSettings(name="Mary Ann van der Berg", email="mary@example.com", phone="(555) 010-0000",
                          location="Austin, TX", linkedin_url="https://linkedin.com/in/mary",
                          work_authorization="Yes", needs_sponsorship="No")


def _job(store, tmp_path, company, url, *, files=True, applied=False, status=JobStatus.DONE):
    job = JobItem.create(url)
    job.company, job.role_title, job.status = company, "Engineer", status
    if files:
        folder = tmp_path / company.replace(" ", "_")
        folder.mkdir(exist_ok=True)
        paths = []
        for name, data in (("M_Resume.docx", b"docx-bytes"), ("M_Resume.pdf", b"%PDF-1.4 pdf"),
                           ("M_Cover_Letter.docx", b"cover-bytes")):
            (folder / name).write_bytes(data)
            paths.append(str(folder / name))
        job.generated_files = paths
    if applied:
        job.applied_at = "2026-01-05T12:00:00+00:00"
    store.add(job)
    store.update(job)
    return job


@pytest.fixture
def env(tmp_path):
    store = QueueStore(tmp_path / "studio.db")
    state = {"locked": False}
    service = IntakeService(store, lambda: TOKEN, lambda d: {"status": "added"},
                            profile=lambda: PROFILE, locked=lambda: state["locked"])
    yield store, service, state, tmp_path
    store.database.close()


def test_names_split_into_first_and_last():
    assert apply_kit.split_name("Mary Ann van der Berg") == ("Mary Ann", "van der Berg")
    assert apply_kit.split_name("Jane Q Public") == ("Jane Q", "Public")
    assert apply_kit.split_name("Cher") == ("Cher", "")
    assert apply_kit.split_name("") == ("", "")


def test_kit_is_found_by_link_then_by_company(env):
    store, service, _, tmp = env
    job = _job(store, tmp, "Acme Inc", "https://boards.greenhouse.io/acme/jobs/1")
    _job(store, tmp, "Globex", "https://jobright.ai/jobs/info/abc")

    code, body = service.apply_kit({"url": "https://boards.greenhouse.io/acme/jobs/1/"})
    assert code == 200 and body["job"]["id"] == job.id
    assert body["profile"]["first_name"] == "Mary Ann" and body["profile"]["last_name"] == "van der Berg"
    assert [f["kind"] for f in body["files"]] == ["resume", "pdf", "cover"]

    code, body = service.apply_kit({"url": "https://other.example/x", "company": "ACME, inc."})
    assert code == 200 and body["job"]["company"] == "Acme Inc"


def test_unknown_page_offers_the_ready_jobs_instead_of_guessing(env):
    store, service, _, tmp = env
    _job(store, tmp, "Acme", "https://a.example/1")
    _job(store, tmp, "Acme", "https://a.example/2")  # same company twice: ambiguous, so no guess
    _job(store, tmp, "Hidden", "https://h.example/1", files=False)
    code, body = service.apply_kit({"url": "https://x.example", "company": "Acme"})
    assert code == 404 and body["status"] == "no_job"
    assert sorted(c["company"] for c in body["candidates"]) == ["Acme", "Acme"]  # no-files job is not offered


def test_applied_and_unfinished_jobs_are_never_filled(env):
    store, service, _, tmp = env
    _job(store, tmp, "Done Co", "https://d.example/1", applied=True)
    _job(store, tmp, "Busy Co", "https://b.example/1", status=JobStatus.GENERATING)
    assert service.apply_kit({"url": "https://d.example/1"})[0] == 404
    assert service.apply_kit({"url": "https://b.example/1"})[0] == 404


def test_a_chosen_job_id_is_used(env):
    store, service, _, tmp = env
    first = _job(store, tmp, "Acme", "https://a.example/1")
    _job(store, tmp, "Acme", "https://a.example/2")
    code, body = service.apply_kit({"url": "https://x.example", "job_id": first.id})
    assert code == 200 and body["job"]["id"] == first.id


def test_locked_app_serves_nothing(env):
    store, service, state, tmp = env
    job = _job(store, tmp, "Acme", "https://a.example/1")
    state["locked"] = True
    assert service.apply_kit({"url": job.url})[0] == 423
    assert service.apply_file(job.id, "pdf")[0] == 423


def test_files_come_from_the_job_record_only(env):
    store, service, _, tmp = env
    job = _job(store, tmp, "Acme", "https://a.example/1")
    code, body = service.apply_file(job.id, "pdf")
    assert code == 200 and body["name"] == "M_Resume.pdf" and body["mime"] == "application/pdf"
    assert base64.b64decode(body["base64"]) == b"%PDF-1.4 pdf"
    assert service.apply_file(job.id, "../../secret")[0] == 400
    assert service.apply_file("not-a-job", "pdf")[0] == 404
    assert service.apply_file(job.id, "resume")[1]["base64"]


def test_autofill_is_logged_but_does_not_mark_applied(env):
    store, service, _, tmp = env
    job = _job(store, tmp, "Acme", "https://a.example/1")
    report = {"site": "greenhouse", "filled": ["First name", "Email"], "needs_you": ["Salary"], "files": ["Resume"],
              "junk": "ignored"}
    code, body = service.apply_event({"job_id": job.id, "event": "autofilled", "report": report})
    assert code == 200 and body["status"] == "logged"
    assert store.get(job.id).applied_at == ""
    events = store.list_events(job.id)
    assert EV.AUTOFILLED in [e["event_type"] for e in events]
    logged = next(e for e in events if e["event_type"] == EV.AUTOFILLED)["payload"]
    assert logged["filled"] == ["First name", "Email"] and "junk" not in logged
    assert not replay(store.database.effective_events(job.id)).applied
    assert replay(store.database.effective_events(job.id)).last_activity_at is not None  # from the import event only


def test_only_the_confirmation_marks_applied_and_only_once(env):
    store, service, _, tmp = env
    job = _job(store, tmp, "Acme", "https://a.example/1")
    assert service.apply_event({"job_id": job.id, "event": "submitted"})[0] == 400
    code, body = service.apply_event({"job_id": job.id, "event": "submitted_confirmed"})
    assert code == 200 and body["status"] == "applied"
    assert store.get(job.id).applied_at
    assert service.apply_event({"job_id": job.id, "event": "submitted_confirmed"})[0] == 409
    assert service.apply_event({"job_id": "nope", "event": "autofilled"})[0] == 404


def test_endpoints_need_the_token_and_reject_web_pages(env):
    store, service, _, tmp = env
    job = _job(store, tmp, "Acme", "https://a.example/1")
    server = IntakeServer(service, port=0)
    assert server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        good = {"Authorization": f"Bearer {TOKEN}"}
        assert httpx.post(base + "/api/apply/kit", json={}, timeout=10).status_code == 401
        assert httpx.get(base + f"/api/apply/file?job={job.id}&kind=pdf", timeout=10).status_code == 401
        assert httpx.post(base + "/api/apply/kit", json={"url": job.url},
                          headers={**good, "Origin": "https://evil.example"}, timeout=10).status_code == 403
        ok = httpx.post(base + "/api/apply/kit", json={"url": job.url}, headers=good, timeout=10)
        assert ok.status_code == 200 and ok.json()["job"]["id"] == job.id
        file = httpx.get(base + f"/api/apply/file?job={job.id}&kind=cover", headers=good, timeout=10)
        assert file.status_code == 200 and file.json()["name"] == "M_Cover_Letter.docx"
    finally:
        server.stop()


def test_profile_settings_default_blank_and_normalise_yes_no():
    old = AppSettings.from_dict({"profile": {"name": "A B", "email": "a@b.co"}})
    assert old.profile.linkedin_url == "" and old.profile.work_authorization == ""
    new = AppSettings.from_dict({"profile": {"work_authorization": " yes ", "needs_sponsorship": "maybe",
                                             "github_url": "https://github.com/x"}})
    assert (new.profile.work_authorization, new.profile.needs_sponsorship) == ("Yes", "")
    assert new.profile.github_url == "https://github.com/x"
