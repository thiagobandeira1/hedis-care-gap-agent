"""Graph state (TypedDict with reducers) and the frozen payloads that flow through it.

``status`` is never stored by the interrupting node (LangGraph discards an interrupting
node's writes and re-runs it on resume); ``awaiting_approval`` is derived from
``graph.get_state``. ``outcome`` exists only after ``finalize``.
"""

import operator
from datetime import date
from typing import Annotated, Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from caregap.agents.schemas import CareActionPlan, ValidationVerdict
from caregap.measures.ids import ALL_MEASURES, MeasureId
from caregap.measures.models import MeasureEvaluation, OpenGap, ReviewItem
from caregap.p6.models import FeatureRow, PatientRecord

LoadErrorKind = Literal["not_found", "unavailable", "contract"]
ApprovalAction = Literal["approve", "edit", "revise", "reject", "auto_reviewed"]
ResolutionStatus = Literal["open", "excluded", "closed", "not_eligible"]
RunStatus = Literal["completed", "rejected", "no_action", "error"]


class RunOptions(BaseModel):
    model_config = ConfigDict(frozen=True)

    validation_mode: Literal["escalated", "off"] = "escalated"
    """``off`` = engine-only ablation: escalated candidates become review items directly."""
    approval_mode: Literal["interrupt", "auto"] = "interrupt"
    """``auto`` = evals/batch only: emits the request, records ``auto_reviewed``, never
    actionable, never writes the outbox."""
    max_revisions: int = 1
    measures: tuple[MeasureId, ...] = ALL_MEASURES


class LoadError(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: LoadErrorKind
    detail: str = ""
    """Error class / problem code only — never record content."""


class MeasurementSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: date
    my_start: date
    my_end: date
    age_at_my_end: int | None


class ReviewResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    measure_id: MeasureId | None
    """None resolves a GLOBAL review item."""
    status: ResolutionStatus
    reason: str = Field(min_length=1)


class ApprovalRequest(BaseModel):
    """Built purely from state by ``await_approval`` (no I/O, no model call)."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    patient_id: str
    as_of: date
    open_gaps: list[OpenGap] = Field(default_factory=list)
    review_items: list[ReviewItem] = Field(default_factory=list)
    plan: CareActionPlan | None = None
    draft_error: str | None = None
    revision_count: int = 0
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    model_ids: dict[str, str] = Field(default_factory=dict)
    demo_grade_notice: str = "demo-grade; HEDIS-aligned; not NCQA-certified"


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: str = Field(min_length=1)
    """Client-generated idempotency key; UNIQUE in the RunStore."""
    action: ApprovalAction
    edited_plan: CareActionPlan | None = None
    """Required iff action == "edit"; re-linted before resume."""
    feedback: str | None = None
    """Required iff action == "revise"."""
    review_resolutions: list[ReviewResolution] = Field(default_factory=list)
    reviewer: str = Field(min_length=1)
    note: str = ""


class AgentError(BaseModel):
    model_config = ConfigDict(frozen=True)

    node: str
    schema_name: str
    error_class: str
    attempt: int


class NodeTrace(BaseModel):
    model_config = ConfigDict(frozen=True)

    node: str
    started_at: str
    finished_at: str
    duration_ms: int
    model_id: str | None = None
    prompt_version: str | None = None
    case_key: str | None = None


class RunOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: RunStatus
    actionable: bool
    approved_actions: list[str] = Field(default_factory=list)
    """action_ids appended to the outbox (empty unless actionable)."""
    engine_verdicts: dict[str, str] = Field(default_factory=dict)
    """measure_id -> engine verdict (pre-validation) — the engine-only eval column."""
    final_statuses: dict[str, str] = Field(default_factory=dict)
    """measure_id -> final status after validation + reviewer resolutions."""
    decision_action: ApprovalAction | None = None
    load_error: LoadError | None = None


class GapState(TypedDict):
    run_id: str
    patient_id: str
    as_of: date
    options: RunOptions
    record: NotRequired[PatientRecord]
    features: NotRequired[FeatureRow | None]
    load_error: NotRequired[LoadError]
    context: NotRequired[MeasurementSummary]
    evaluations: NotRequired[list[MeasureEvaluation]]
    verdicts: Annotated[list[ValidationVerdict], operator.add]
    open_gaps: NotRequired[list[OpenGap]]
    review_items: NotRequired[list[ReviewItem]]
    plan: NotRequired[CareActionPlan | None]
    draft_error: NotRequired[str | None]
    revision_feedback: Annotated[list[str], operator.add]
    revision_count: int
    decision: NotRequired[ApprovalDecision]
    decisions: Annotated[list[ApprovalDecision], operator.add]
    agent_errors: Annotated[list[AgentError], operator.add]
    outcome: NotRequired[RunOutcome]
    trace: Annotated[list[NodeTrace], operator.add]


def initial_state(run_id: str, patient_id: str, as_of: date, options: RunOptions) -> GapState:
    return GapState(
        run_id=run_id,
        patient_id=patient_id,
        as_of=as_of,
        options=options,
        verdicts=[],
        revision_feedback=[],
        revision_count=0,
        decisions=[],
        agent_errors=[],
        trace=[],
    )


def thread_id(run_id: str, patient_id: str) -> str:
    return f"{run_id}:{patient_id}"
