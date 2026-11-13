"""Application lifecycle vocabulary.

Five independent concerns are tracked separately (never one overloaded status):

A. document generation  -> ``JobItem.status`` (queue pipeline)
B. submitted?           -> ``applied_at`` (set only by an explicit user action)
C. employer response    -> ``ResponseStatus``
D. recruitment stage    -> ``Stage`` (a summary of the event history)
E. final outcome        -> ``Outcome`` (terminal, reversible only via an explicit reopen)
"""

from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    NOT_APPLIED = "not_applied"
    APPLIED = "applied"
    RECRUITER = "recruiter_contact"
    SCREENING = "screening"
    TECHNICAL = "technical"
    FINAL = "final"
    OFFER = "offer"

    @property
    def label(self) -> str:
        return STAGE_LABELS[self]

    @property
    def rank(self) -> int:
        return STAGE_ORDER.index(self)


STAGE_ORDER = list(Stage)
STAGE_LABELS = {
    Stage.NOT_APPLIED: "Not Applied",
    Stage.APPLIED: "Applied",
    Stage.RECRUITER: "Recruiter Contact",
    Stage.SCREENING: "Screening",
    Stage.TECHNICAL: "Technical Interview",
    Stage.FINAL: "Final Interview",
    Stage.OFFER: "Offer",
}


class Outcome(str, Enum):
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    CLOSED = "position_closed"
    ACCEPTED = "accepted"

    @property
    def label(self) -> str:
        return OUTCOME_LABELS[self]


OUTCOME_LABELS = {
    Outcome.REJECTED: "Rejected",
    Outcome.WITHDRAWN: "Withdrawn",
    Outcome.CLOSED: "Position Closed",
    Outcome.ACCEPTED: "Accepted Offer",
}


class Response(str, Enum):
    NONE = "none"
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


RESPONSE_LABELS = {
    Response.NONE: "No reply",
    Response.POSITIVE: "Positive",
    Response.NEUTRAL: "Neutral",
    Response.NEGATIVE: "Negative",
}


def stage_from_value(value: str | None) -> Stage:
    try:
        return Stage(value or "")
    except ValueError:
        return Stage.NOT_APPLIED


def outcome_from_value(value: str | None) -> Outcome | None:
    try:
        return Outcome(value) if value else None
    except ValueError:
        return None


# --- Legacy (schema v1) pipeline labels: kept only so old rows can be migrated. ---


class ApplicationStage(str, Enum):
    NOT_STARTED = "Not started"
    DOCS_READY = "Documents ready"
    APPLIED = "Applied"
    REPLIED = "Heard back"
    SCREENING = "Screening"
    TECH = "Technical"
    FINAL = "Final"
    OFFER = "Offer"
    REJECTED = "Rejected"
    WITHDRAWN = "Withdrawn"
    GHOSTED = "Ghosted"


APPLICATION_STAGE_ORDER = list(ApplicationStage)

# legacy label -> (recruitment stage, outcome or None)
LEGACY_STAGE_MAP: dict[str, tuple[Stage, Outcome | None]] = {
    "Not started": (Stage.NOT_APPLIED, None),
    "Documents ready": (Stage.NOT_APPLIED, None),
    "Applied": (Stage.APPLIED, None),
    "Heard back": (Stage.RECRUITER, None),
    "Screening": (Stage.SCREENING, None),
    "Technical": (Stage.TECHNICAL, None),
    "Final": (Stage.FINAL, None),
    "Offer": (Stage.OFFER, None),
    "Rejected": (Stage.APPLIED, Outcome.REJECTED),
    "Withdrawn": (Stage.APPLIED, Outcome.WITHDRAWN),
    "Ghosted": (Stage.APPLIED, None),
}
