"""Viewer mode: keep this computer's copy of the controller's data fresh, and stand in for the queue engine.

The viewer never generates anything. It shows what the controller is doing, and asks the controller to apply
the changes you make (skip, remove, mark applied...). Starting, pausing and retrying generation are done on the
controller itself.
"""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import QObject, Signal

from core.applications import ApplicationError
from core.queue_engine import WorkerState
from core.viewer_client import ViewerSync

POLL_SECONDS = 3.0
OFFLINE_POLL_SECONDS = 8.0

ON_CONTROLLER = "This is done on the controller (the VPS), not in the viewer."


class ViewerEngine:
    """Looks like the queue engine to the screens, but only reports the controller's state and relays the few
    queue actions a viewer is allowed to ask for."""

    current_job_id = ""

    def __init__(self, link: "ViewerLink") -> None:
        self._link = link

    @property
    def state(self) -> WorkerState:
        try:
            return WorkerState(self._link.sync.worker)
        except ValueError:
            return WorkerState.IDLE

    def skip_job(self, job_id: str) -> None:
        self._link.queue.skip_job(job_id)

    def submit_jd(self, job_id: str, text: str) -> bool:
        return self._link.queue.submit_jd(job_id, text)

    def _refuse(self, *_args: Any, **_kwargs: Any) -> None:
        raise ApplicationError(ON_CONTROLLER)

    start = pause = stop = retry_job = apply_resume_json = _refuse


class ViewerLink(QObject):
    """Polls the controller on a background thread and tells the UI thread when something changed."""

    data_changed = Signal()  # a newer copy of the database was installed
    profile_changed = Signal()  # the controller's profile / output folder changed
    status_changed = Signal()  # connected / offline / controller worker state

    def __init__(self, sync: ViewerSync, queue, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.sync = sync
        self.queue = queue
        self.latest_profile: dict[str, Any] | None = None
        self.controller_root = ""
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_status: tuple = ()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="viewer-sync", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def sync_soon(self) -> None:
        self._wake.set()

    def accept_profile(self, payload: dict[str, Any]) -> None:
        """Called from the sync thread; applied to the settings on the UI thread."""
        self.latest_profile = payload
        self.controller_root = str(payload.get("output_root") or "")

    def _run(self) -> None:
        while not self._stop.is_set():
            outcome = self.sync.sync_once()
            if outcome.profile_changed:
                self.profile_changed.emit()
            if outcome.changed:
                self.data_changed.emit()
            status = (self.sync.connected, self.sync.error, self.sync.worker, self.sync.controller_locked)
            if status != self._last_status:
                self._last_status = status
                self.status_changed.emit()
            self._wake.wait(POLL_SECONDS if self.sync.connected else OFFLINE_POLL_SECONDS)
            self._wake.clear()
