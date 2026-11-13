"""Browser-extension intake: auth, applied-company check, duplicate links, and the UI hand-off."""

from __future__ import annotations

import threading

import httpx
import pytest

from core.browser_intake import IntakeServer, IntakeService
from core.company_match import applied_matches, normalize_company
from core.models import JobItem, JobStatus, QueueStore

TOKEN = "test-token"
DESCRIPTION = ("Build and run services. " * 10).strip()


def _store(tmp_path, applied_company="Acme, Inc."):
    store = QueueStore(tmp_path / "studio.db")
    job = JobItem.create("https://old.example/1")
    job.company = applied_company
    job.role_title = "Engineer"
    job.applied_at = "2026-01-05T12:00:00+00:00"
    store.add(job)
    store.update(job)
    return store


@pytest.fixture
def server(tmp_path):
    store = _store(tmp_path)
    added: list[dict] = []

    def add_job(data):
        added.append(data)
        return {"status": "added", "id": "x", "started": False}

    srv = IntakeServer(IntakeService(store, lambda: TOKEN, add_job), port=0)
    assert srv.start()
    srv.added = added
    srv.base = f"http://127.0.0.1:{srv.port}"
    yield srv
    srv.stop()
    store.database.close()


def _post(srv, path, body, token=TOKEN, **headers):
    h = {"Authorization": f"Bearer {token}", **headers}
    return httpx.post(srv.base + path, json=body, headers=h, timeout=10)


def test_normalize_company_ignores_case_punctuation_and_legal_suffixes():
    assert normalize_company("ACME, Inc.") == normalize_company("acme")
    assert normalize_company("The Acme Corporation") == "acme"
    assert normalize_company("Acme Labs") != normalize_company("Acme")


def test_requires_the_token_and_rejects_web_pages(server):
    assert httpx.get(server.base + "/api/status", timeout=10).status_code == 401
    assert _post(server, "/api/check", {}, token="wrong").status_code == 401
    ok = httpx.get(server.base + "/api/status", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
    assert ok.status_code == 200 and ok.json()["ok"] is True
    # A page in an ordinary website sends its own Origin; only extension origins are accepted.
    assert _post(server, "/api/check", {}, Origin="https://evil.example").status_code == 403
    assert _post(server, "/api/check", {}, Origin="chrome-extension://abc").status_code == 200
    # DNS rebinding: a foreign Host header is refused even with the right token.
    assert _post(server, "/api/check", {}, Host="evil.example").status_code == 403


def test_applied_matches_counts_only_applied_jobs(tmp_path):
    store = _store(tmp_path)
    queued = JobItem.create("https://q.example/1")
    queued.company = "Globex"  # queued but never applied
    store.add(queued)
    assert [m["company"] for m in applied_matches(store, "acme")] == ["Acme, Inc."]
    assert applied_matches(store, "Globex") == []
    assert applied_matches(store, "") == []
    applied_id = applied_matches(store, "acme")[0]["id"]
    assert applied_matches(store, "acme", exclude_id=applied_id) == []  # a job never matches itself
    store.database.close()


def test_check_flags_applied_company_and_queued_link(server):
    hit = _post(server, "/api/check", {"company": "acme", "url": "https://new.example/2"}).json()
    assert hit["applied"] is True and hit["in_queue"] is False and hit["matches"][0]["company"] == "Acme, Inc."
    assert _post(server, "/api/check", {"company": "Globex"}).json()["applied"] is False
    assert _post(server, "/api/check", {"url": "https://old.example/1/"}).json()["in_queue"] is True


def test_add_validates_and_applies_the_rules(server):
    good = {"url": "https://new.example/2", "company": "Globex", "job_description": DESCRIPTION, "role_title": "SRE"}
    assert _post(server, "/api/jobs", {**good, "url": "not a url"}).status_code == 400
    assert _post(server, "/api/jobs", {**good, "job_description": "short"}).status_code == 400
    applied = _post(server, "/api/jobs", {**good, "company": "ACME"})
    assert applied.status_code == 409 and applied.json()["status"] == "applied_company"
    dup = _post(server, "/api/jobs", {**good, "url": "https://old.example/1/"})
    assert dup.status_code == 409 and dup.json()["status"] == "duplicate_url"
    assert server.added == []

    ok = _post(server, "/api/jobs", good)
    assert ok.status_code == 200 and ok.json()["status"] == "added"
    assert server.added[0]["company"] == "Globex" and server.added[0]["description"] == DESCRIPTION


def test_only_the_job_link_is_kept_and_salary_is_passed_on(server):
    job = {"url": "https://boards.example/apply/9", "company": "Globex",
           "job_description": DESCRIPTION, "salary": "$120K - $150K/yr"}
    ok = _post(server, "/api/jobs", {**job, "origin_url": "https://jobright.ai/jobs/info/abc"})
    assert ok.status_code == 200
    assert server.added[0]["url"] == job["url"] and server.added[0]["salary"] == job["salary"]
    assert "origin_url" not in server.added[0]


def test_a_company_already_in_the_queue_is_refused(server, tmp_path):
    store = server.service.store
    queued = JobItem.create("https://queued.example/1")
    queued.company = "Globex Corp."
    store.add(queued)
    skipped = JobItem.create("https://skipped.example/1")
    skipped.company = "Initech"
    skipped.status = JobStatus.SKIPPED
    store.add(skipped)

    hit = _post(server, "/api/check", {"company": "globex", "url": "https://new.example/3"}).json()
    assert hit["company_queued"] is True and hit["queued_matches"][0]["status"] == "Queued"
    assert _post(server, "/api/check", {"company": "Initech"}).json()["company_queued"] is False
    assert _post(server, "/api/check", {"company": ""}).json()["company_queued"] is False

    good = {"url": "https://new.example/3", "company": "GLOBEX", "job_description": DESCRIPTION}
    refused = _post(server, "/api/jobs", good)
    assert refused.status_code == 409 and refused.json()["status"] == "duplicate_company"
    assert server.added == []
    assert _post(server, "/api/jobs", {**good, "company": "Initech"}).status_code == 200


def test_oversized_body_is_refused(server):
    big = {"url": "https://x.example", "job_description": "x" * 1_100_000}
    try:
        assert _post(server, "/api/jobs", big).status_code == 413
    except httpx.TransportError:
        pass  # the server may hang up before the client finishes uploading; either way nothing is added
    assert server.added == []
