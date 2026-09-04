"""Structured-output schemas for the agents. Frozen; validated by code after every call."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from caregap.measures.ids import MeasureId

ValidationDecision = Literal["confirm_open", "exclude", "numerator_met", "needs_human"]
Confidence = Literal["high", "medium", "low"]
Urgency = Literal["routine", "soon"]
ActionKind = Literal[
    "schedule_visit", "order", "referral", "medication_review", "screening", "other"
]
ActionOwner = Literal["care_team", "provider", "patient"]


class ValidationVerdict(BaseModel):
    """The validator's answer for ONE escalated candidate. ``verified`` is set by code only."""

    model_config = ConfigDict(frozen=True)

    measure_id: MeasureId
    decision: ValidationDecision
    exclusion_category: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    rule_citation: str = ""
    confidence: Confidence = "low"
    rationale: str = Field(default="", max_length=600)
    verified: bool = False
    verification_note: str | None = None


class PlannedGap(BaseModel):
    model_config = ConfigDict(frozen=True)

    measure_id: MeasureId
    rank: int = 0
    """Overwritten by code from the deterministic priority order."""
    urgency: Urgency = "routine"
    rationale: str = Field(default="", max_length=300)


class GapAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action_id: str = ""
    """Assigned by code (``a1``, ``a2`` ...) — the outbox idempotency key with the thread id."""
    measure_id: MeasureId
    kind: ActionKind
    detail: str = Field(max_length=300)
    owner: ActionOwner = "care_team"


class CareActionPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    gaps: list[PlannedGap] = Field(default_factory=list)
    actions: list[GapAction] = Field(default_factory=list)
    patient_message: str = Field(default="", max_length=1200)
    provider_note: str = Field(default="", max_length=1500)
