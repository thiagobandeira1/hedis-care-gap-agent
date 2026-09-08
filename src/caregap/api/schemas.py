"""Request / response bodies. Everything the graph already types (``MeasureEvaluation``,
``ApprovalRequest``, ``RunOutcome``, the ledger records ...) is served as-is; the models here
are the envelopes around them. No patient name, address or demographic beyond
birth date / sex / deceased ever appears (P6 strips the rest at the boundary)."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from caregap.agents.schemas import CareActionPlan, ValidationVerdict
from caregap.graph.runstore import PatientRunRecord, RunRecord
from caregap.graph.state import (
    AgentError,
    ApprovalDecision,
    ApprovalRequest,
    LoadError,
    MeasurementSummary,
    NodeTrace,
    RunOptions,
    RunOutcome,
)
from caregap.measures.ids import MeasureId
from caregap.measures.models import Coverage, MeasureEvaluation, OpenGap, ReviewItem
from caregap.measures.rule_text import ElementKind, ElementSource
from caregap.p6.models import ServiceInfo


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True)


# --- health / measures --------------------------------------------------------------------


class HealthResponse(_Body):
    status: Literal["ok"]
    service_version: str
    p6: ServiceInfo
    p6_mode: Literal["embedded", "http", "snapshot"]
    models_mode: Literal["fake", "replay", "anthropic"]
    prompt_version: str
    engine_measures: list[MeasureId]


class ElementCounts(_Body):
    quoted: int
    demo_choice: int
    not_representable: int


class MeasureElement(_Body):
    id: str
    kind: ElementKind
    text: str
    source: ElementSource
    citation: str
    coverage: Coverage | None = None


class MeasureInfo(_Body):
    measure_id: MeasureId
    name: str
    star_id: str | None
    rule_version: str
    conformance: str
    element_counts: ElementCounts
    coverage: dict[str, Coverage] = Field(default_factory=dict)
    """Every public exclusion criterion -> observable | partial | not_representable."""
    elements: list[MeasureElement] = Field(default_factory=list)


# --- panel / patient ------------------------------------------------------------------------


class LastRun(_Body):
    run_id: str
    status: str


class PanelItem(_Body):
    patient_id: str
    sex: str
    birth_date: date | None
    deceased: bool
    last_run: LastRun | None = None


class PanelResponse(_Body):
    items: list[PanelItem]
    total: int
    as_of: date | None = None
    limit: int
    offset: int


class PatientGapsResponse(_Body):
    patient_id: str
    as_of: date
    context: MeasurementSummary
    evaluations: list[MeasureEvaluation]


# --- runs -----------------------------------------------------------------------------------


#: One path segment, bounded: patient ids reach P6 URLs and snapshot paths verbatim (V17).
PatientId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]


class RunRequest(_Body):
    as_of: date
    patient_ids: list[PatientId] = Field(min_length=1)
    options: RunOptions = RunOptions()


class RunAccepted(_Body):
    run_id: str
    status: Literal["queued"]


class RunDetail(_Body):
    run: RunRecord
    patients: list[PatientRunRecord]


class CancelResponse(_Body):
    run_id: str
    status: str
    """``cancelling`` while the loop is still going; otherwise the run's terminal status."""


class PatientState(_Body):
    """The thread's checkpointed state, minus the record and features (served by
    ``/v1/patients/{id}/gaps`` and never wholesale)."""

    evaluations: list[MeasureEvaluation] = Field(default_factory=list)
    verdicts: list[ValidationVerdict] = Field(default_factory=list)
    open_gaps: list[OpenGap] = Field(default_factory=list)
    review_items: list[ReviewItem] = Field(default_factory=list)
    plan: CareActionPlan | None = None
    draft_error: str | None = None
    revision_count: int = 0
    trace: list[NodeTrace] = Field(default_factory=list)
    agent_errors: list[AgentError] = Field(default_factory=list)
    context: MeasurementSummary | None = None
    load_error: LoadError | None = None
    decisions: list[ApprovalDecision] = Field(default_factory=list)


STATE_FIELDS: tuple[str, ...] = tuple(PatientState.model_fields)


class PatientRunDetail(_Body):
    patient_run: PatientRunRecord
    pending: ApprovalRequest | None
    """Derived from the graph's interrupt; ``null`` once resolved or superseded."""
    state: PatientState | None


# --- decisions ------------------------------------------------------------------------------


class DecisionResponse(_Body):
    kind: Literal["run_outcome", "approval_request"]
    result: RunOutcome | ApprovalRequest
    replayed: bool
    """``true`` when ``decision_id`` was already recorded: the stored result, nothing re-run."""

    @classmethod
    def of(cls, result: RunOutcome | ApprovalRequest, *, replayed: bool) -> "DecisionResponse":
        kind: Literal["run_outcome", "approval_request"] = (
            "run_outcome" if isinstance(result, RunOutcome) else "approval_request"
        )
        return cls(kind=kind, result=result, replayed=replayed)
