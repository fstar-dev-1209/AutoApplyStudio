"""Viewer side of the controller link: fetch the database copy, send changes back as commands."""

from __future__ import annotations

import gzip
import hashlib
import shutil
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from core.application_db import ApplicationDatabase
from core.applications import ApplicationError, ApplicationService
from core.migrations import LATEST_VERSION
from core.sync_protocol import (
    API_VERSION,
    APP_COMMANDS,
    QUEUE_COMMANDS,
    SNAPSHOT_HEADER_SCHEMA,
    SNAPSHOT_HEADER_SHA,
    SNAPSHOT_HEADER_TOKEN,
    decode,
    encode,
)


class ControllerUnavailable(ApplicationError):
    """The controller could not be reached (offline, wrong address, timed out)."""


class ControllerRejected(ApplicationError):
    """The controller answered but refused: wrong token, locked, incompatible, or the command failed."""

    def __init__(self, status: int, message: str, kind: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind


class ControllerClient:
    def __init__(self, base_url: Callable[[], str], token: Callable[[], str], timeout: float = 20.0) -> None:
        self._base_url = base_url
        self._token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        base = self._base_url().strip().rstrip("/")
        if not base:
            raise ControllerUnavailable("The controller address is not set. Enter it in Settings.")
        try:
            response = httpx.request(
                method, base + path, headers={"Authorization": f"Bearer {self._token()}"},
                timeout=self.timeout, **kwargs)
        except httpx.InvalidURL as exc:
            raise ControllerUnavailable("The controller address is not a valid web address.") from exc
        except httpx.HTTPError as exc:
            raise ControllerUnavailable(f"Cannot reach the controller at {base}: {exc}") from exc
        if response.status_code == 401:
            raise ControllerRejected(401, "The controller refused the token. Copy it again from the controller's Settings.")
        if response.status_code == 403:
            raise ControllerRejected(403, "The controller refused this address. Add it to the controller's allowed hosts.")
        return response

    @staticmethod
    def _error(response: httpx.Response) -> ControllerRejected:
        try:
            body = response.json()
        except ValueError:
            body = {}
        return ControllerRejected(response.status_code, str(body.get("error") or f"HTTP {response.status_code}"),
                                  str(body.get("type") or ""))

    def status(self) -> dict[str, Any]:
        response = self._request("GET", "/api/sync/status")
        if response.status_code == 404:
            raise ControllerRejected(404, "That address is not an Application Studio controller (or it is an older version).")
        if response.status_code != 200:
            raise self._error(response)
        return response.json()

    def snapshot(self, have: str) -> tuple[str, int, bytes] | None:
        """(token, schema, database bytes) or None if ``have`` is already current. The download is verified."""
        response = self._request("GET", "/api/sync/db", params={"have": have})
        if response.status_code == 304:
            return None
        if response.status_code != 200:
            raise self._error(response)
        packed = response.content
        if hashlib.sha256(packed).hexdigest() != response.headers.get(SNAPSHOT_HEADER_SHA, ""):
            raise ControllerUnavailable("The database copy arrived damaged. Trying again shortly.")
        try:
            data = gzip.decompress(packed)
        except (OSError, EOFError) as exc:
            raise ControllerUnavailable("The database copy could not be unpacked.") from exc
        return response.headers[SNAPSHOT_HEADER_TOKEN], int(response.headers.get(SNAPSHOT_HEADER_SCHEMA, "0")), data

    def profile(self) -> dict[str, Any] | None:
        response = self._request("GET", "/api/sync/profile")
        if response.status_code == 423:
            return None
        if response.status_code != 200:
            raise self._error(response)
        return response.json()

    def command(self, kind: str, name: str, args: list, kwargs: dict) -> Any:
        """Run a change on the controller. One automatic retry after a dropped connection reuses the request id,
        so the controller never applies it twice."""
        body = {"kind": kind, "name": name, "args": encode(list(args)), "kwargs": encode(dict(kwargs)),
                "request_id": uuid.uuid4().hex}
        try:
            response = self._request("POST", "/api/command", json=body)
        except ControllerUnavailable:
            response = self._request("POST", "/api/command", json=body)
        if response.status_code != 200:
            raise self._error(response)
        return decode(response.json().get("result"))


@dataclass
class SyncOutcome:
    changed: bool = False  # a new database copy was installed
    profile_changed: bool = False
    error: str = ""


class ViewerSync:
    """Keeps this computer's read-only database in step with the controller's."""

    def __init__(
        self,
        db: ApplicationDatabase,
        client: ControllerClient,
        on_profile: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.db = db
        self.client = client
        self.on_profile = on_profile
        self.token = ""
        self.profile_hash = ""
        self.connected = False
        self.error = ""
        self.worker = "idle"
        self.controller_locked = False
        self.last_ok: datetime | None = None
        self._lock = threading.Lock()

    def sync_once(self) -> SyncOutcome:
        """One poll: cheap status check, then download only if the controller's data changed."""
        with self._lock:
            outcome = SyncOutcome()
            try:
                status = self.client.status()
                if int(status.get("api", 0)) != API_VERSION:
                    raise ControllerRejected(0, "The controller and this app speak different versions. Update both to the same release.")
                if int(status.get("schema", 0)) > LATEST_VERSION:
                    raise ControllerRejected(0, "The controller has a newer database format. Update this app.")
                self.worker = str(status.get("worker") or "idle")
                self.controller_locked = bool(status.get("locked"))
                if status["token"] != self.token:
                    outcome.changed = self._install(self.client.snapshot(self.token))
                if not self.controller_locked and status.get("profile_hash") != self.profile_hash:
                    payload = self.client.profile()
                    if payload is not None:
                        if self.on_profile:
                            self.on_profile(payload)
                        self.profile_hash = str(status.get("profile_hash") or "")
                        outcome.profile_changed = True
                self.connected, self.error, self.last_ok = True, "", datetime.now(timezone.utc)
            except ApplicationError as exc:
                self.connected, self.error = False, str(exc)
                outcome.error = str(exc)
            except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
                # e.g. antivirus holding the file, a damaged reply: keep the current copy and try again
                self.connected, self.error = False, f"Could not update from the controller: {exc}"
                outcome.error = self.error
            return outcome

    def _install(self, snapshot: tuple[str, int, bytes] | None) -> bool:
        if snapshot is None:
            return False
        token, schema, data = snapshot
        if schema > LATEST_VERSION:
            raise ControllerRejected(0, "The controller has a newer database format. Update this app.")
        work = Path(tempfile.mkdtemp(prefix=".sync-", dir=self.db.path.parent))
        try:
            incoming = work / "incoming.db"
            incoming.write_bytes(data)
            self._verify(incoming)
            ApplicationDatabase(incoming).close()  # brings an older copy up to this app's format (additive)
            self.db.reload_from(incoming)
            self.token = token
            return True
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _verify(path: Path) -> None:
        try:
            conn = sqlite3.connect(str(path))
            try:
                ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise ControllerUnavailable(f"The database copy is unreadable: {exc}") from exc
        if not ok or not {"jobs", "job_events", "meta"} <= tables:
            raise ControllerUnavailable("The database copy failed its integrity check.")


class RemoteApplicationService(ApplicationService):
    """ApplicationService for a viewer: reads come from the local copy, every change is sent to the controller.

    The names in ``APP_COMMANDS`` are forwarded; after the controller applies one, a fresh copy is fetched so
    the screen already shows the result when the call returns."""

    def __getattribute__(self, name: str) -> Any:
        if name in APP_COMMANDS:
            return object.__getattribute__(self, "_forward")(name)
        return super().__getattribute__(name)

    def attach(self, sync: ViewerSync) -> None:
        self._sync = sync

    def _forward(self, name: str) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            result = self._sync.client.command("app", name, list(args), kwargs)
            self._sync.sync_once()
            self._changed()
            return result

        return call


class RemoteQueue:
    """Queue-management actions a viewer can ask the controller to perform."""

    def __init__(self, sync: ViewerSync) -> None:
        self._sync = sync

    def _run(self, name: str, *args: Any) -> Any:
        assert name in QUEUE_COMMANDS
        result = self._sync.client.command("queue", name, list(args), {})
        self._sync.sync_once()
        return result

    def skip_job(self, job_id: str) -> None:
        self._run("skip_job", job_id)

    def remove_jobs(self, job_ids: list[str]) -> int:
        return int(self._run("remove_jobs", list(job_ids)))

    def add_urls(self, urls: list[str], allow_duplicate_companies: bool) -> dict[str, Any]:
        return self._run("add_urls", list(urls), bool(allow_duplicate_companies))

    def allow_duplicate(self, job_id: str) -> None:
        self._run("allow_duplicate", job_id)

    def submit_jd(self, job_id: str, text: str) -> bool:
        return bool(self._run("submit_jd", job_id, text))
