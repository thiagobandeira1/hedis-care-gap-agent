"""Canned API payloads shared by the UI tests: the frozen models the API would return for one
patient with two open gaps (CBP, EED), one escalated measure (SPC) and a pending approval."""

from datetime import date

from caregap.agents.drafter import DrafterContext
from caregap.agents.schemas import ValidationVerdict
from caregap.agents.template_drafter import TemplateDrafter
from caregap.graph.runstore import ApprovalRecord, OutboxEntry, PatientRunRecord, RunRecord
from caregap.graph.state import (
    ApprovalRequest,
    MeasurementSummary,
    NodeTrace,
    RunOptions,
    RunOutcome,
)
from caregap.measures.ids import ALL_MEASURES, MEASURE_NAMES, MeasureId
from caregap.measures.models import (
    EscalationFlag,
    EvidenceRef,
    EvidenceRole,
    ExclusionHit,
    MeasureEvaluation,
    OpenGap,
    ReviewItem,
    Section,
    TriResult,
)
from caregap.ui.client import (
    DecisionResult,
    ElementCounts,
    HealthInfo,
    LastRun,
    MeasureInfo,
    P6Info,
    PanelItem,
    PanelPage,
    PatientGaps,
    PatientRunDetail,
    PatientState,
    RunCreated,
    RunDetail,
)

AS_OF = date(2026, 6, 30)
RUN_ID = "run_0123456789ab"
PATIENT = "p1"
OTHER_PATIENT = "p2"
STAMP = "2026-06-30T12:00:00.000Z"

VERDICTS: dict[str, str] = {
    "CBP": "gap_open",
    "EED": "gap_open",
    "BCS": "not_eligible",
    "COL": "closed",
    "SPC": "needs_review",
    "SPD": "excluded",
    "TSC": "closed",
    "SNS": "gap_open",
}


def evidence(
    event_id: str,
    role: EvidenceRole,
    section: Section = "observations",
    code: str = "85354-9",
) -> EvidenceRef:
    return EvidenceRef(
        section=section,
        event_id=event_id,
        code=code,
        code_system="LOINC" if section == "observations" else "SNOMED",
        display="canned",
        event_date=date(2026, 3, 1),
        role=role,
    )


def evaluation(measure_id: MeasureId) -> MeasureEvaluation:
    verdict = VERDICTS[measure_id]
    candidate = verdict in {"gap_open", "needs_review"}
    return MeasureEvaluation(
        measure_id=measure_id,
        rule_version="2026.1",
        denominator=TriResult(value="no" if verdict == "not_eligible" else "yes"),
        numerator=TriResult(
            value="no" if candidate else ("yes" if verdict == "closed" else "unknown"),
            subtype="no_bp_in_my" if measure_id == "CBP" else None,
            evidence=[evidence("o1", "numerator")] if measure_id == "CBP" else [],
        ),
        exclusions=(
            [
                ExclusionHit(
                    category="hospice",
                    source="quoted",
                    window_label="MY",
                    evidence=[evidence("c1", "exclusion", "conditions", "385763009")],
                )
            ]
            if measure_id == "SPD"
            else []
        ),
        coverage={
            "denominator": "observable",
            "numerator": "partial" if measure_id == "SPC" else "observable",
        },
        escalations=(
            [
                EscalationFlag(
                    kind="E3",
                    scope="measure",
                    reason="E3: statin authored once",
                    evidence=[evidence("m1", "escalation", "medications", "617310")],
                )
            ]
            if measure_id == "SPC"
            else []
        ),
        verdict=verdict,  # type: ignore[arg-type]
        priority_score=3.0 if verdict == "gap_open" else 0.0,
    )


EVALUATIONS = [evaluation(measure_id) for measure_id in ALL_MEASURES]
OPEN_GAPS = [
    OpenGap(
        measure_id="CBP",
        subtype="no_bp_in_my",
        evidence=[evidence("o1", "numerator")],
        priority_score=3.0,
        rank=1,
    ),
    OpenGap(measure_id="EED", priority_score=2.0, rank=2),
]
REVIEW_ITEMS = [
    ReviewItem(measure_id="SPC", scope="measure", reason="E3: statin authored once; needs_human")
]
CONTEXT = DrafterContext(
    patient_id=PATIENT,
    as_of=AS_OF,
    age_band="65-74",
    sex="female",
    clinic_name="Demo Primary Care",
    clinic_phone="555-0100",
)
PLAN = TemplateDrafter().draft(OPEN_GAPS, REVIEW_ITEMS, CONTEXT)
VALIDATOR_VERDICTS = [
    ValidationVerdict(
        measure_id="SPC",
        decision="needs_human",
        evidence_ids=["m1"],
        rule_citation="SPC numerator: statin dispensed in the MY",
        confidence="low",
        rationale="One authored statin order; dispensing not observable.",
        verified=True,
    )
]
REQUEST = ApprovalRequest(
    run_id=RUN_ID,
    patient_id=PATIENT,
    as_of=AS_OF,
    open_gaps=OPEN_GAPS,
    review_items=REVIEW_ITEMS,
    plan=PLAN,
    prompt_versions={"drafter": "v1"},
    model_ids={"drafter": "fake"},
)
SUMMARY = MeasurementSummary(
    as_of=AS_OF, my_start=date(2026, 1, 1), my_end=date(2026, 12, 31), age_at_my_end=66
)
GAPS = PatientGaps(patient_id=PATIENT, as_of=AS_OF, context=SUMMARY, evaluations=EVALUATIONS)
RUN = RunRecord(
    run_id=RUN_ID,
    as_of=AS_OF,
    status="running",
    options=RunOptions(),
    patient_ids=[PATIENT, OTHER_PATIENT],
    cursor=1,
    created_at=STAMP,
    updated_at=STAMP,
)
PATIENT_RUN = PatientRunRecord(
    run_id=RUN_ID,
    patient_id=PATIENT,
    status="awaiting_approval",
    pending=REQUEST,
    updated_at=STAMP,
)
OUTCOME = RunOutcome(
    status="completed",
    actionable=True,
    approved_actions=["a1", "a2"],
    engine_verdicts=dict(VERDICTS),
    final_statuses={**VERDICTS, "SPC": "needs_review"},
    decision_action="approve",
)
STATE = PatientState(
    evaluations=EVALUATIONS,
    verdicts=VALIDATOR_VERDICTS,
    open_gaps=OPEN_GAPS,
    review_items=REVIEW_ITEMS,
    plan=PLAN,
    trace=[NodeTrace(node="evaluate_measures", started_at=STAMP, finished_at=STAMP, duration_ms=3)],
)
PATIENT_RUN_DETAIL = PatientRunDetail(patient_run=PATIENT_RUN, pending=REQUEST, state=STATE)
RUN_DETAIL = RunDetail(run=RUN, patients=[PATIENT_RUN])
RUN_CREATED = RunCreated(run_id=RUN_ID, status="queued")
DECISION_RESULT = DecisionResult(result=OUTCOME, replayed=False)
PANEL = PanelPage(
    items=[
        PanelItem(
            patient_id=PATIENT,
            sex="female",
            birth_date=date(1960, 6, 15),
            last_run=LastRun(run_id=RUN_ID, status="awaiting_approval"),
        ),
        PanelItem(patient_id=OTHER_PATIENT, sex="male", deceased=True),
    ],
    total=2,
)
HEALTH = HealthInfo(
    status="ok",
    p6=P6Info(service_version="0.1.0", schema_version=1, feature_version="f1"),
    models_mode="fake",
    prompt_version="v1",
    engine_measures=list(ALL_MEASURES),
)
MEASURES = [
    MeasureInfo(
        measure_id="CBP",
        name=MEASURE_NAMES["CBP"],
        star_id="C14",
        rule_version="2026.1",
        conformance="demo-grade; HEDIS-aligned; not NCQA-certified",
        element_counts=ElementCounts(quoted=3, demo_choice=1, not_representable=1),
    )
]
OUTBOX = [
    OutboxEntry(
        thread_id=f"{RUN_ID}:{PATIENT}",
        action_id="a1",
        run_id=RUN_ID,
        patient_id=PATIENT,
        measure_id="CBP",
        kind="schedule_visit",
        detail="Schedule a blood-pressure visit.",
        owner="care_team",
        approval_ref="d1",
        created_at=STAMP,
    )
]
APPROVALS = [ApprovalRecord(run_id=RUN_ID, patient_id=PATIENT, status="pending", request=REQUEST)]
