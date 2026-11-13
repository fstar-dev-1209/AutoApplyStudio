"""Shared rules for the controller <-> viewer link.

The controller owns the one real database. A viewer reads a copy of it and sends every change back as a
command; the controller applies the command and the next copy contains the result.

Values that JSON cannot carry (stages, outcomes, dates) travel as tagged objects, so a command means exactly
the same thing on both sides.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from core.application_stage import Outcome, Stage

API_VERSION = 1
SNAPSHOT_HEADER_TOKEN = "X-Snapshot-Token"
SNAPSHOT_HEADER_SHA = "X-Snapshot-Sha256"
SNAPSHOT_HEADER_SCHEMA = "X-Snapshot-Schema"

# ApplicationService methods that change data. Only these can be sent as commands; everything else (reads,
# listeners, attributes) is refused by the controller whatever a client asks for.
APP_COMMANDS = frozenset(
    {
        "mark_applied", "add_manual", "unmark_applied", "set_stage", "set_outcome", "reopen", "set_hold",
        "add_note", "log_followup_sent", "set_notes", "set_duplicate_override", "archive", "bulk_mark_applied",
        "bulk_set_stage", "bulk_set_outcome", "edit_event", "void_event", "restore_event", "record_message",
        "update_message", "schedule_interview", "update_interview", "delete_interview", "add_action",
        "set_next_action", "complete_action",
    }
)

# Queue-management actions a viewer may ask for. Starting, pausing, stopping and retrying generation are not
# here on purpose: those are done on the controller itself.
QUEUE_COMMANDS = frozenset({"skip_job", "remove_jobs", "add_urls", "allow_duplicate", "submit_jd"})

_TAGS = {"$stage", "$outcome", "$dt", "$date"}


class CommandRefused(Exception):
    """The controller will not run this command; `status` is the HTTP code sent back."""

    def __init__(self, status: int, message: str, kind: str = "CommandRefused") -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind


def encode(value: Any) -> Any:
    if isinstance(value, Stage):
        return {"$stage": value.value}
    if isinstance(value, Outcome):
        return {"$outcome": value.value}
    if isinstance(value, datetime):  # before date: datetime is a date
        return {"$dt": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, dict):
        if any(str(k).startswith("$") for k in value):
            raise ValueError("Keys starting with '$' are reserved.")
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"Cannot send a {type(value).__name__} to the controller.")


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        keys = set(value)
        if len(keys) == 1 and keys <= _TAGS:
            (tag, raw), = value.items()
            try:
                if tag == "$stage":
                    return Stage(raw)
                if tag == "$outcome":
                    return Outcome(raw)
                if tag == "$dt":
                    return datetime.fromisoformat(raw)
                return date.fromisoformat(raw)
            except (ValueError, TypeError) as exc:
                raise CommandRefused(400, f"Invalid {tag[1:]} value.") from exc
        if keys & _TAGS:
            raise CommandRefused(400, "Malformed tagged value.")
        return {k: decode(v) for k, v in value.items()}
    return value
