"""Viewer side: where the controller's documents live on this computer, and which ones to rebuild.

The database copy holds each resume as JSON, so documents are rebuilt locally with the existing "Sync to disk"
logic instead of being downloaded. Nothing here writes to the database copy.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath

from core.application_db import ApplicationDatabase

MAX_ATTEMPTS = 3  # a job whose rebuild keeps failing is left for the manual button, not retried forever


def make_path_mapper(controller_root: Callable[[], str], local_root: Callable[[], Path]) -> Callable[[str], str]:
    """Translate a path stored by the controller (under its output folder) into this computer's output folder.
    Paths that are not under the controller's folder are returned unchanged."""

    def map_path(path: str) -> str:
        root = controller_root()
        if not path or not root:
            return path
        windows = bool(re.match(r"^[A-Za-z]:[\\/]", root)) or "\\" in root
        flavor = PureWindowsPath if windows else PurePosixPath
        try:
            relative = flavor(path).relative_to(flavor(root))
        except ValueError:
            return path
        return str(Path(local_root()).joinpath(*relative.parts))

    return map_path


class FileMaterializer:
    """Remembers which finished jobs already had their documents handled on this computer.

    The first time a viewer connects, everything already finished is taken as "handled" (so connecting never
    floods the disk with old resumes; the Sync to disk button restores any of them). After that, each newly
    finished job is rebuilt once. A file you delete afterwards stays deleted until you press Sync to disk.
    """

    def __init__(self, state_path: Path) -> None:
        self.path = state_path
        self.baselined = False
        self.seen: set[str] = set()
        self.attempts: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self.baselined = bool(data.get("baselined"))
            self.seen = {str(x) for x in data.get("seen", []) if isinstance(x, str)}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"baselined": self.baselined, "seen": sorted(self.seen)}), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass  # not fatal: at worst a job is considered again next start

    def due(self, db: ApplicationDatabase) -> list[str]:
        """Finished jobs whose documents should be rebuilt now."""
        finished = [job_id for job_id, _files in db.finished_jobs_with_documents()]
        if not self.baselined:
            self.seen, self.baselined = set(finished), True
            self._save()
            return []
        return [j for j in finished if j not in self.seen and self.attempts.get(j, 0) < MAX_ATTEMPTS]

    def record(self, results: list) -> None:
        """Mark rebuilt (or already present) jobs as handled; count failures."""
        for r in results:
            if r.ok:
                self.seen.add(r.job_id)
            else:
                self.attempts[r.job_id] = self.attempts.get(r.job_id, 0) + 1
        self._save()

    def mark_handled(self, job_ids: list[str]) -> None:
        """Jobs the person synced by hand count as handled too."""
        self.seen.update(job_ids)
        self._save()
