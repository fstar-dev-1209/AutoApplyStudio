"""Local intake endpoint for the Chrome/Edge extension.

The extension reads a job posting from the page the user is looking at and posts it here. The server listens
on loopback only and every request needs the shared token (shown in Settings), so web pages cannot use it.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
import secrets as _secrets
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from core import apply_kit
from core.company_match import applied_matches, queued_matches
from core.lifecycle import EV
from core.models import QueueStore
from core.secrets import load_secret, store_secret
from core.settings_store import AppSettings, ProfileSettings
from core.sync_protocol import (
    API_VERSION,
    APP_COMMANDS,
    QUEUE_COMMANDS,
    SNAPSHOT_HEADER_SCHEMA,
    SNAPSHOT_HEADER_SHA,
    SNAPSHOT_HEADER_TOKEN,
    CommandRefused,
    decode,
    encode,
)

INTAKE_HOST = "127.0.0.1"
INTAKE_PORT = 47821
TOKEN_SECRET = "intake_token"
MAX_BODY = 1_000_000
MIN_DESCRIPTION = 80


def get_token(secret_path: Path | None = None) -> str:
    """The shared secret; created on first use and kept in OS-backed storage."""
    token = load_secret(TOKEN_SECRET, secret_path)
    if not token:
        token = regenerate_token(secret_path)
    return token


def regenerate_token(secret_path: Path | None = None) -> str:
    token = _secrets.token_urlsafe(24)
    store_secret(TOKEN_SECRET, token, secret_path)
    return token


class IntakeService:
    """The rules behind the endpoint, independent of HTTP. `add_job` runs on the UI thread."""

    def __init__(
        self,
        store: QueueStore,
        token: Callable[[], str],
        add_job: Callable[[dict[str, str]], dict[str, Any]],
        auto_start: Callable[[], bool] = lambda: True,
        profile: Callable[[], ProfileSettings] = ProfileSettings,
        locked: Callable[[], bool] = lambda: False,
        apply_event: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
        settings: Callable[[], AppSettings] | None = None,
        worker_state: Callable[[], str] = lambda: "idle",
        command: Callable[[str, str, list, dict], Any] | None = None,
        allowed_hosts: Callable[[], set[str]] = set,
        sync_enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        self.store = store
        self.token = token
        self.add_job = add_job
        self.auto_start = auto_start
        self.profile = profile
        self.locked = locked
        self.apply_event_hook = apply_event or self._record_apply_event
        self.settings = settings
        self.worker_state = worker_state
        self.command_hook = command or self._default_command
        self.allowed_hosts = allowed_hosts
        self.sync_enabled = sync_enabled  # False unless this app runs as a controller
        self._snapshot: tuple[str, bytes, str, int] | None = None  # token, gzip bytes, sha256, schema
        self._snapshot_lock = threading.Lock()
        self._done_commands: dict[str, dict[str, Any]] = {}
        self._command_lock = threading.Lock()
        self._apps = None

    def status(self) -> dict[str, Any]:
        return {"ok": True, "app": "Application Studio", "auto_start": bool(self.auto_start())}

    def check(self, url: str, company: str) -> dict[str, Any]:
        matches = applied_matches(self.store, company)
        in_queue = bool(url) and url.rstrip("/") in self.store.urls()
        queued = queued_matches(self.store, company)
        return {"in_queue": in_queue, "applied": bool(matches), "matches": matches[:5],
                "company_queued": bool(queued), "queued_matches": queued[:5]}

    def add(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        url = str(payload.get("url") or "").strip()
        description = str(payload.get("job_description") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return 400, {"status": "invalid", "error": "The job link is not a valid web address."}
        if len(description) < MIN_DESCRIPTION:
            return 400, {"status": "invalid", "error": "The job description is too short to use."}
        company = str(payload.get("company") or "").strip()
        verdict = self.check(url, company)
        if verdict["in_queue"]:
            return 409, {"status": "duplicate_url", "error": "This job link is already in your queue."}
        if verdict["applied"]:
            return 409, {"status": "applied_company", "matches": verdict["matches"],
                         "error": f"You already applied to {company or 'this company'}."}
        if verdict["company_queued"]:
            return 409, {"status": "duplicate_company", "matches": verdict["queued_matches"],
                         "error": f"{company} is already in your queue."}
        job = {
            "url": url, "company": company, "description": description,
            "role_title": str(payload.get("role_title") or "").strip(),
            "source": str(payload.get("source") or "").strip(),
            "salary": str(payload.get("salary") or "").strip(),
        }
        return 200, self.add_job(job)

    # ---- job apply assist: fill the employer's form from the job's documents and the saved profile.
    # Nothing here submits anything; a job becomes applied only on the user's explicit confirmation.

    def apply_kit(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.locked():
            return 423, {"error": "Application Studio is locked. Unlock it with your password, then try again."}
        job = apply_kit.find_job(
            self.store, str(payload.get("url") or ""), str(payload.get("company") or ""),
            str(payload.get("job_id") or ""),
        )
        if job is None:
            return 404, {"status": "no_job", "candidates": apply_kit.candidates(self.store),
                         "error": "No job with finished documents matches this page."}
        return 200, apply_kit.kit(job, self.profile())

    def apply_file(self, job_id: str, kind: str) -> tuple[int, dict[str, Any]]:
        if self.locked():
            return 423, {"error": "Application Studio is locked."}
        job = apply_kit.find_job(self.store, job_id=job_id)
        if job is None:
            return 404, {"error": "That job is not ready to apply to."}
        try:
            return 200, apply_kit.read_file(job, kind)
        except apply_kit.KitError as exc:
            return exc.status, {"error": str(exc)}

    def apply_event(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        job_id = str(payload.get("job_id") or "")
        event = str(payload.get("event") or "")
        if event not in {"autofilled", "submitted_confirmed"}:
            return 400, {"error": "Unknown event."}
        job = self.store.get(job_id) if job_id else None
        if job is None:
            return 404, {"error": "That job no longer exists."}
        if job.applied_at:
            return 409, {"status": "already_applied", "error": "This job is already marked as applied."}
        return 200, self.apply_event_hook(job_id, event, _clean_report(payload.get("report")))

    def _record_apply_event(self, job_id: str, event: str, report: dict[str, Any]) -> dict[str, Any]:
        if event == "autofilled":
            self.store.add_event(job_id, EV.AUTOFILLED, report)
            return {"status": "logged"}
        from core.applications import ApplicationService

        done = ApplicationService(self.store.database).mark_applied(
            job_id, note="Confirmed in the browser extension after filling the form.")
        return {"status": "applied" if done else "already_applied"}


    # ---- controller <-> viewer link: a read-only copy of the database going out, commands coming in.

    def _sync_profile_payload(self) -> dict[str, Any]:
        settings = self.settings() if self.settings else AppSettings()
        return {
            "profile": asdict(self.profile()),
            "export_pdf": settings.export_pdf,
            "export_cover_letter": settings.export_cover_letter,
            "output_root": str(settings.resolved_output_root()),
        }

    def sync_status(self) -> dict[str, Any]:
        db = self.store.database
        locked = bool(self.locked())
        profile_hash = ""
        if not locked:
            profile_hash = hashlib.sha256(json.dumps(self._sync_profile_payload(), sort_keys=True).encode()).hexdigest()
        return {
            "api": API_VERSION, "app": "Application Studio", "role": "controller",
            "token": db.change_token(), "schema": db.schema_version(),
            "worker": self.worker_state(), "locked": locked, "profile_hash": profile_hash,
        }

    def sync_snapshot(self, have: str) -> tuple[int, dict[str, str], bytes]:
        """The database as one gzip file. 304 when the viewer already has the current version."""
        db = self.store.database
        if have and have == db.change_token():
            return 304, {}, b""
        with self._snapshot_lock:
            if self._snapshot is None or self._snapshot[0] != db.change_token():
                handle, name = tempfile.mkstemp(prefix="appstudio-snapshot-", suffix=".db")
                os.close(handle)
                try:
                    token = db.snapshot_to(Path(name))
                    packed = gzip.compress(Path(name).read_bytes(), compresslevel=6)
                finally:
                    Path(name).unlink(missing_ok=True)
                self._snapshot = (token, packed, hashlib.sha256(packed).hexdigest(), db.schema_version())
            token, packed, digest, schema = self._snapshot
        return 200, {SNAPSHOT_HEADER_TOKEN: token, SNAPSHOT_HEADER_SHA: digest,
                     SNAPSHOT_HEADER_SCHEMA: str(schema)}, packed

    def sync_profile(self) -> tuple[int, dict[str, Any]]:
        if self.locked():
            return 423, {"error": "The controller is locked, so its profile cannot be shared."}
        return 200, self._sync_profile_payload()

    def run_command(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Run one change a viewer asked for. A repeated request id returns the first answer instead of
        running the command twice (a retry after a dropped connection must not add a second note)."""
        request_id = str(payload.get("request_id") or "")[:64]
        with self._command_lock:
            if request_id and request_id in self._done_commands:
                return 200, self._done_commands[request_id]
            try:
                kind, name = str(payload.get("kind") or ""), str(payload.get("name") or "")
                args, kwargs = decode(payload.get("args") or []), decode(payload.get("kwargs") or {})
                if not isinstance(args, list) or not isinstance(kwargs, dict):
                    raise CommandRefused(400, "Malformed command arguments.")
                allowed = {"app": APP_COMMANDS, "queue": QUEUE_COMMANDS}.get(kind, frozenset())
                if name not in allowed:
                    raise CommandRefused(400, f"Unknown command: {kind}.{name}")
                result = self.command_hook(kind, name, args, kwargs)
                body = {"result": encode(result), "token": self.store.database.change_token()}
            except CommandRefused as exc:
                return exc.status, {"error": str(exc), "type": exc.kind}
            except Exception as exc:  # noqa: BLE001 - reported to the viewer, never crashes the server
                from core.applications import ApplicationError

                if isinstance(exc, ApplicationError):
                    return 422, {"error": str(exc), "type": "ApplicationError"}
                return 500, {"error": f"The controller could not run that: {exc}", "type": type(exc).__name__}
            if request_id:
                if len(self._done_commands) >= 200:
                    self._done_commands.pop(next(iter(self._done_commands)))
                self._done_commands[request_id] = body
            return 200, body

    def _default_command(self, kind: str, name: str, args: list, kwargs: dict) -> Any:
        """Direct execution, used when no UI thread is involved (tests, headless use)."""
        if kind != "app":
            raise CommandRefused(501, "This controller cannot run queue commands.")
        if self._apps is None:
            from core.applications import ApplicationService

            self._apps = ApplicationService(self.store.database)
        return getattr(self._apps, name)(*args, **kwargs)


def _clean_report(raw: Any) -> dict[str, Any]:
    """Keep the extension's fill report small and plain: lists of short strings."""
    if not isinstance(raw, dict):
        return {}

    def texts(key: str) -> list[str]:
        value = raw.get(key)
        return [str(item)[:120] for item in value[:60]] if isinstance(value, list) else []

    return {"site": str(raw.get("site") or "")[:60], "page": str(raw.get("page") or "")[:300],
            "filled": texts("filled"), "needs_you": texts("needs_you"), "files": texts("files")}


class IntakeServer:
    def __init__(self, service: IntakeService, port: int = INTAKE_PORT, host: str = INTAKE_HOST) -> None:
        self.service = service
        self.port = port
        self.host = host
        self.error = ""
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> bool:
        if self._httpd is not None:
            return True
        service = self.service

        class Handler(BaseHTTPRequestHandler):
            server_version = "ApplicationStudio"

            def log_message(self, *_args: object) -> None:  # keep the console quiet
                return

            # ---- helpers
            def _origin_ok(self) -> bool:
                origin = self.headers.get("Origin")
                return origin is None or origin.startswith("chrome-extension://")

            def _host_ok(self) -> bool:
                """Blocks DNS rebinding: only our own loopback names, or the names the owner listed for remote use."""
                bound = self.server.server_address[1]
                names = {f"127.0.0.1:{bound}", f"localhost:{bound}"}
                names |= {h.strip().lower() for h in service.allowed_hosts() if h.strip()}
                return (self.headers.get("Host") or "").lower() in names

            def _drain(self) -> None:
                """Read the unread request body so closing the socket does not reset the client's connection."""
                if getattr(self, "_drained", False):
                    return
                self._drained = True
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return
                if 0 < length <= MAX_BODY:
                    self.rfile.read(length)

            def _send(self, code: int, body: dict[str, Any]) -> None:
                self._drain()
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                origin = self.headers.get("Origin")
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.end_headers()
                self.wfile.write(data)

            def _send_bytes(self, code: int, headers: dict[str, str], data: bytes) -> None:
                self._drain()
                self.send_response(code)
                if code != 304:
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                if code != 304:
                    self.wfile.write(data)

            def _authorized(self) -> bool:
                if not (self._host_ok() and self._origin_ok()):
                    self._send(403, {"error": "Forbidden."})
                    return False
                header = self.headers.get("Authorization") or ""
                given = header.removeprefix("Bearer ").strip()
                if not given or not hmac.compare_digest(given.encode(), service.token().encode()):
                    self._send(401, {"error": "Wrong or missing token. Copy it from Settings → Browser extension."})
                    return False
                return True

            def _json_body(self) -> dict[str, Any] | None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_BODY:
                    self._send(413, {"error": "Request too large."})
                    return None
                self._drained = True
                try:
                    data = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._send(400, {"error": "Invalid JSON."})
                    return None
                if not isinstance(data, dict):
                    self._send(400, {"error": "Invalid JSON."})
                    return None
                return data

            # ---- routes
            def do_OPTIONS(self) -> None:  # noqa: N802 - CORS preflight, extension origins only
                if not (self._host_ok() and self._origin_ok() and self.headers.get("Origin")):
                    self._send(403, {"error": "Forbidden."})
                    return
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Max-Age", "600")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                parsed = urlparse(self.path)
                try:
                    self._route_get(parsed)
                except Exception as exc:  # noqa: BLE001
                    self._send(500, {"error": f"The app could not answer: {exc}"})

            def _route_get(self, parsed) -> None:
                if parsed.path.startswith("/api/sync/") and not service.sync_enabled():
                    self._send(404, {"error": "This app is not running as a controller."})
                elif parsed.path == "/api/status":
                    self._send(200, service.status())
                elif parsed.path == "/api/sync/status":
                    self._send(200, service.sync_status())
                elif parsed.path == "/api/sync/db":
                    have = (parse_qs(parsed.query).get("have") or [""])[0]
                    self._send_bytes(*service.sync_snapshot(have))
                elif parsed.path == "/api/sync/profile":
                    code, result = service.sync_profile()
                    self._send(code, result)
                elif parsed.path == "/api/apply/file":
                    query = parse_qs(parsed.query)
                    code, result = service.apply_file(
                        (query.get("job") or [""])[0], (query.get("kind") or [""])[0])
                    self._send(code, result)
                else:
                    self._send(404, {"error": "Unknown endpoint."})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                path = urlparse(self.path).path
                body = self._json_body()
                if body is None:
                    return
                try:
                    if path == "/api/check":
                        self._send(200, service.check(str(body.get("url") or ""), str(body.get("company") or "")))
                    elif path == "/api/jobs":
                        code, result = service.add(body)
                        self._send(code, result)
                    elif path == "/api/apply/kit":
                        code, result = service.apply_kit(body)
                        self._send(code, result)
                    elif path == "/api/apply/event":
                        code, result = service.apply_event(body)
                        self._send(code, result)
                    elif path == "/api/command":
                        if service.sync_enabled():
                            code, result = service.run_command(body)
                        else:
                            code, result = 404, {"error": "This app is not running as a controller."}
                        self._send(code, result)
                    else:
                        self._send(404, {"error": "Unknown endpoint."})
                except Exception as exc:  # noqa: BLE001
                    self._send(500, {"error": f"The app could not add the job: {exc}"})

        try:
            httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            self.error = f"Could not listen on port {self.port}: {exc.strerror or exc}"
            return False
        self.port = httpd.server_address[1]
        httpd.daemon_threads = True
        self._httpd = httpd
        self.error = ""
        self._thread = threading.Thread(target=httpd.serve_forever, name="intake-server", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
