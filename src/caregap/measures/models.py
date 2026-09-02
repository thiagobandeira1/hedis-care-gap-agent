"""Engine output models. All frozen; ids reference P6 event ids, never content beyond codes,
display strings, and dates."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from caregap.measures.ids import MeasureId

Section = Literal[
    "conditions", "observations", "procedures", "medications", "encounters", "patient"
]
EvidenceRole = Literal["eligibility", "numerator", "exclusion", "escalation"]
Coverage = Literal["observable", "partial", "not_representable"]
EscalationKind = Literal["E1", "E2", "E3", "E4", "E5", "E6", "E7"]
Scope = Literal["global", "measure"]
GapSource = Literal["engine", "validator_confirmed", "reviewer"]


class EvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    section: Section
    event_id: str
    code: str | None = None
    code_system: str | None = None
    display: str | None = None
    event_date: date | None = None
    role: EvidenceRole


class ExclusionHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str
    source: Literal["quoted", "demo_choice"]
    window_label: str
    evidence: list[EvidenceRef] = Field(default_factory=list)


class EscalationFlag(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: EscalationKind
    scope: Scope
    reason: str
    evidence: list[EvidenceRef] = Field(default_factory=list)


class TriResult(BaseModel):
    """A three-valued rule outcome with the evidence that produced it."""

    model_config = ConfigDict(frozen=True)

    value: Literal["yes", "no", "unknown"]
    reasons: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    subtype: str | None = None
    """Numerator detail such as ``no_bp_in_my`` or ``low_intensity_only``."""
    window_start: date | None = None
    window_end: date | None = None


class MeasureEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    measure_id: MeasureId
    rule_version: str
    denominator: TriResult
    numerator: TriResult
    exclusions: list[ExclusionHit] = Field(default_factory=list)
    coverage: dict[str, Coverage] = Field(default_factory=dict)
    escalations: list[EscalationFlag] = Field(default_factory=list)
    verdict: Literal["not_eligible", "closed", "excluded", "gap_open", "needs_review"]
    priority_score: float = 0.0

    @property
    def is_candidate(self) -> bool:
        return self.verdict in {"gap_open", "needs_review"}

    @property
    def global_escalations(self) -> list[EscalationFlag]:
        return [e for e in self.escalations if e.scope == "global"]


class OpenGap(BaseModel):
    model_config = ConfigDict(frozen=True)

    measure_id: MeasureId
    subtype: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    priority_score: float
    rank: int
    source: GapSource = "engine"


class ReviewItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    measure_id: MeasureId | None
    scope: Scope
    reason: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
