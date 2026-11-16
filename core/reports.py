"""Status reports for the person running the bot: what the job search found, how each resume went, how each application
went (with its questions and answers in the same message). They go to Telegram (and Discord) through the notifier; the
wording and layout are in ``report_format``. A report is information, not an alert, and it must never get in the way
of the work it describes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from core import report_format as fmt
from core.application_record import FILE_NAME, format_record, write_record
from core.discovery import IngestReport
from core.models import JobItem
from core.notify import Notifier

log = logging.getLogger(__name__)


class Reporter:
    def __init__(self, notifier: Notifier, enabled: Callable[[], bool], data_dir: Path,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._notifier, self._enabled, self._now = notifier, enabled, now
        self._fallback_dir = data_dir / "answers"

    def _send(self, message: fmt.Message | None) -> None:
        if message is None:
            return
        try:
            if self._enabled():
                self._notifier.report(message.text, message.document)
        except Exception:  # noqa: BLE001 - reporting must never break the pipeline
            log.exception("report failed")

    def discovery(self, report: IngestReport) -> None:
        self._send(fmt.job_search(report))

    def resume(self, job: JobItem) -> None:
        self._send(fmt.resume(job))

    def application(self, job: JobItem, state: str, reason: str, plan: list[dict], confirmation: str,
                    submitted_ok: bool) -> None:
        """``state`` is the attempt's state. A sent application is recorded on disk (everything) and reported with its
        questions and answers (without your own details)."""
        if submitted_ok:
            try:
                write_record(job, plan, self._now(), confirmation, self._fallback_dir)
            except Exception:  # noqa: BLE001
                log.exception("could not write the answers record")
            self._send(fmt.applied(job, plan, format_record(job, plan, self._now(), confirmation, private=False)))
        else:
            self._send(fmt.application_result(job, state, reason, plan))


__all__ = ["Reporter", "FILE_NAME"]
