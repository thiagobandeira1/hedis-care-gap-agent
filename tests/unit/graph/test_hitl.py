"""HITL helpers: the ``validate_decision`` rule matrix, interrupt payload parsing, and
``pending_request`` derived from ``graph.get_state`` over a tiny interrupting graph."""

from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt, interrupt

from caregap.agents.drafter import DrafterContext
from caregap.agents.schemas import CareActionPlan, GapAction, PlannedGap
from caregap.agents.template_drafter import TemplateDrafter
from caregap.graph.build import checkpoint_serializer
from caregap.graph.hitl import pending_request, request_from_interrupt, validate_decision
from caregap.graph.state import (
    ApprovalDecision,
    ApprovalRequest,
    GapState,
    ReviewResolution,
    RunOptions,
    initial_state,
    thread_id,
)
from caregap.measures.ids import MeasureId
from caregap.measures.models import EvidenceRef, OpenGap, ReviewItem
from tests.factories import EVAL_AS_OF

CLINIC = "Springfield Clinic"
PHONE = "555-0100"


def gap(measure_id: MeasureId, rank: int) -> OpenGap:
    return OpenGap(
        measure_id=measure_id,
        evidence=[
            EvidenceRef(
                section="observations",
                event_id=f"o{rank}",
                code="85354-9",
                code_system="LOINC",
                event_date=EVAL_AS_OF.replace(month=3, day=1),
                role="numerator",
            )
        ],
        priority_score=float(10 - rank),
        rank=rank,
    )


def item(measure_id: MeasureId | None) -> ReviewItem:
    return ReviewItem(
        measure_id=measure_id,
        scope="measure" if measure_id is not None else "global",
        reason="E6: test" if measure_id is not None else "E1: hospice before the MY",
    )


OPEN_GAPS = [gap("CBP", 1), gap("BCS", 2)]
CONTEXT = DrafterContext(
    patient_id="p1",
    as_of=EVAL_AS_OF,
    age_band="65-74",
    sex="female",
    clinic_name=CLINIC,
    clinic_phone=PHONE,
)
TEMPLATE_PLAN = TemplateDrafter().draft(OPEN_GAPS, [], CONTEXT)


def request(**overrides: Any) -> ApprovalRequest:
    base: dict[str, Any] = {
        "run_id": "run1",
        "patient_id": "p1",
        "as_of": EVAL_AS_OF,
        "open_gaps": OPEN_GAPS,
        "review_items": [item("COL")],
        "plan": TEMPLATE_PLAN,
        "revision_count": 0,
    }
    base.update(overrides)
    return ApprovalRequest(**base)


def decision(action: str, **overrides: Any) -> ApprovalDecision:
    base: dict[str, Any] = {"decision_id": "d1", "action": action, "reviewer": "dr"}
    base.update(overrides)
    return ApprovalDecision(**base)


def errors(
    dec: ApprovalDecision, req: ApprovalRequest | None = None, *, max_revisions: int = 1
) -> list[str]:
    return validate_decision(
        dec,
        req or request(),
        max_revisions=max_revisions,
        clinic_name=CLINIC,
        clinic_phone=PHONE,
    )


# --- validate_decision matrix ------------------------------------------------------------------


def test_approve_and_reject_are_valid_as_is() -> None:
    assert errors(decision("approve")) == []
    assert errors(decision("reject")) == []


def test_auto_reviewed_cannot_be_submitted_by_a_reviewer() -> None:
    assert any("auto_reviewed" in e for e in errors(decision("auto_reviewed")))


def test_edit_requires_edited_plan() -> None:
    assert errors(decision("edit")) == ["edit requires edited_plan"]


def test_edit_with_a_lint_clean_plan_is_valid() -> None:
    assert errors(decision("edit", edited_plan=TEMPLATE_PLAN)) == []


def test_edit_relints_the_plan() -> None:
    extra_gap = TEMPLATE_PLAN.model_copy(
        update={
            "gaps": [*TEMPLATE_PLAN.gaps, PlannedGap(measure_id="COL", rationale="added")],
            "actions": [
                *TEMPLATE_PLAN.actions,
                GapAction(measure_id="COL", kind="screening", detail="colonoscopy"),
            ],
        }
    )
    found = errors(decision("edit", edited_plan=extra_gap))
    codes = {e.split(": ")[1] for e in found}
    assert all(e.startswith("edited_plan: ") for e in found)
    # COL is both outside the open-gap set and pending review.
    assert {"GAP_SET", "REVIEW_DRAFTED", "ACTIONS"} <= codes


def test_edit_with_a_stray_number_in_the_message_fails_lint() -> None:
    message = TEMPLATE_PLAN.patient_message.replace(
        "We are happy to help.", "We are happy to help, room 42."
    )
    found = errors(
        decision("edit", edited_plan=TEMPLATE_PLAN.model_copy(update={"patient_message": message}))
    )
    assert [e for e in found if e.startswith("edited_plan: NUMBERS")]


def test_edit_relint_without_clinic_arguments_still_checks_structure() -> None:
    """The runner passes no clinic name/phone: the structural rules still apply; the clinic
    presence rule is vacuous (empty tokens)."""
    dec = decision("edit", edited_plan=TEMPLATE_PLAN.model_copy(update={"gaps": []}))
    found = validate_decision(dec, request(), max_revisions=1)
    assert any(e.startswith("edited_plan: GAP_SET") for e in found)


def test_revise_requires_feedback() -> None:
    assert errors(decision("revise")) == ["revise requires feedback"]
    assert errors(decision("revise", feedback="   ")) == ["revise requires feedback"]


def test_revise_with_feedback_inside_the_budget_is_valid() -> None:
    assert errors(decision("revise", feedback="shorter please")) == []


def test_revise_needs_revision_budget() -> None:
    found = errors(decision("revise", feedback="again"), request(revision_count=1))
    assert found == ["revision budget exhausted (1/1 used)"]
    assert (
        errors(decision("revise", feedback="again"), request(revision_count=1), max_revisions=2)
        == []
    )


def test_revise_needs_something_to_draft() -> None:
    req = request(open_gaps=[], plan=None)
    found = errors(decision("revise", feedback="draft COL too"), req)
    assert found == ["revise requires an open gap or a review resolution to 'open'"]
    opened = decision(
        "revise",
        feedback="draft COL too",
        review_resolutions=[ReviewResolution(measure_id="COL", status="open", reason="fine")],
    )
    assert errors(opened, req) == []


def test_resolution_to_open_requires_revise() -> None:
    res = [ReviewResolution(measure_id="COL", status="open", reason="looks open")]
    assert errors(decision("approve", review_resolutions=res)) == [
        "resolution of COL to 'open' requires action 'revise'"
    ]
    assert errors(decision("revise", feedback="add COL", review_resolutions=res)) == []


@pytest.mark.parametrize("status", ["excluded", "closed", "not_eligible"])
def test_measure_resolutions_must_reference_a_review_item(status: str) -> None:
    ok = [ReviewResolution(measure_id="COL", status=status, reason="chart says so")]  # type: ignore[arg-type]
    assert errors(decision("approve", review_resolutions=ok)) == []
    stray = [ReviewResolution(measure_id="EED", status=status, reason="chart says so")]  # type: ignore[arg-type]
    assert errors(decision("approve", review_resolutions=stray)) == [
        "resolution for EED does not reference a review item"
    ]


def test_global_resolution_requires_a_global_review_item() -> None:
    res = [ReviewResolution(measure_id=None, status="not_eligible", reason="hospice confirmed")]
    assert errors(decision("approve", review_resolutions=res)) == [
        "global resolution does not reference a global review item"
    ]
    held = request(open_gaps=[], plan=None, review_items=[item(None), item("CBP")])
    assert errors(decision("approve", review_resolutions=res), held) == []


def test_global_resolution_cannot_be_open() -> None:
    held = request(open_gaps=[], plan=None, review_items=[item(None)])
    res = [ReviewResolution(measure_id=None, status="open", reason="?")]
    found = errors(decision("revise", feedback="x", review_resolutions=res), held)
    assert "a global review item cannot be resolved to 'open'" in found


def test_duplicate_resolutions_are_rejected() -> None:
    res = [
        ReviewResolution(measure_id="COL", status="closed", reason="a"),
        ReviewResolution(measure_id="COL", status="excluded", reason="b"),
    ]
    assert "duplicate resolution for COL" in errors(decision("approve", review_resolutions=res))


def test_errors_accumulate_rather_than_short_circuit() -> None:
    dec = decision(
        "revise",
        review_resolutions=[ReviewResolution(measure_id="EED", status="open", reason="x")],
    )
    found = errors(dec, request(revision_count=1))
    assert "revise requires feedback" in found
    assert "revision budget exhausted (1/1 used)" in found
    assert "resolution for EED does not reference a review item" in found


# --- request_from_interrupt ---------------------------------------------------------------------


def test_request_from_interrupt_accepts_dump_interrupt_and_model() -> None:
    req = request()
    dumped = req.model_dump(mode="json")
    assert request_from_interrupt(dumped) == req
    assert request_from_interrupt(Interrupt(value=dumped, id="abc")) == req
    assert request_from_interrupt(req) is req
    assert request_from_interrupt(dumped).model_dump(mode="json") == dumped


def test_request_from_interrupt_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        request_from_interrupt({"run_id": "only"})


# --- pending_request ---------------------------------------------------------------------------


def _pause(state: GapState) -> dict[str, Any]:
    req = ApprovalRequest(
        run_id=state["run_id"],
        patient_id=state["patient_id"],
        as_of=state["as_of"],
        open_gaps=OPEN_GAPS,
        plan=TEMPLATE_PLAN,
    )
    raw = interrupt(req.model_dump(mode="json"))
    return {"decision": ApprovalDecision.model_validate(raw)}


def _tiny_graph() -> Any:
    builder: StateGraph[GapState] = StateGraph(GapState)
    builder.add_node("pause", _pause)
    builder.add_edge(START, "pause")
    builder.add_edge("pause", END)
    return builder.compile(checkpointer=MemorySaver(serde=checkpoint_serializer()))


def test_pending_request_is_derived_from_get_state() -> None:
    graph = _tiny_graph()
    tid = thread_id("run1", "p1")
    config = {"configurable": {"thread_id": tid}}
    assert pending_request(graph, tid) is None, "unknown thread: nothing pending"

    graph.invoke(initial_state("run1", "p1", EVAL_AS_OF, RunOptions()), config)
    pending = pending_request(graph, tid)
    assert pending is not None
    assert (pending.run_id, pending.patient_id, pending.as_of) == ("run1", "p1", EVAL_AS_OF)
    assert pending.open_gaps == OPEN_GAPS
    assert pending.plan == TEMPLATE_PLAN
    assert pending_request(graph, "run1:someone-else") is None

    graph.invoke(Command(resume=decision("approve").model_dump(mode="json")), config)
    assert pending_request(graph, tid) is None
    assert graph.get_state(config).values["decision"].action == "approve"


def test_pending_request_survives_a_repeated_lookup() -> None:
    graph = _tiny_graph()
    tid = thread_id("run2", "p1")
    config = {"configurable": {"thread_id": tid}}
    graph.invoke(initial_state("run2", "p1", EVAL_AS_OF, RunOptions()), config)
    first = pending_request(graph, tid)
    second = pending_request(graph, tid)
    assert first == second
    assert first is not None
    assert isinstance(first.plan, CareActionPlan)
