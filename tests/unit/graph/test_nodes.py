"""Node behaviour, one scenario per branch, plus end-to-end runs through the compiled graph
with fake models: P6 error taxonomy, engine over a persona snapshot, validator outcomes
(confirm / verified exclude / unverifiable exclude / garbage / global hold / off), drafter
template fallback, auto-mode approval without an interrupt, outbox written only for
approve/edit in interrupt mode, and one ``NodeTrace`` per node."""

import json
import operator
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from caregap.agents.drafter import OPT_OUT_LINE, drafter_case_key
from caregap.agents.packet import validator_case_key
from caregap.agents.prompts import DRAFTER_PROMPT_SHA, PROMPT_VERSION, VALIDATOR_PROMPT_SHA
from caregap.agents.schemas import CareActionPlan, GapAction, PlannedGap
from caregap.graph import edges, nodes
from caregap.graph.build import GraphDeps, build_graph, checkpoint_serializer
from caregap.graph.hitl import pending_request, request_from_interrupt
from caregap.graph.outbox import InMemoryOutbox
from caregap.graph.runstore import RunStore
from caregap.graph.state import (
    AgentError,
    ApprovalDecision,
    GapState,
    LoadError,
    ReviewResolution,
    RunOptions,
    initial_state,
    thread_id,
)
from caregap.llm import fake_bundle
from caregap.measures.engine import default_engine
from caregap.measures.ids import MeasureId
from caregap.measures.models import ReviewItem
from caregap.measures.value_sets import load_value_sets
from caregap.p6.client import (
    ENGINE_SECTIONS,
    P6ContractError,
    P6Error,
    P6Unavailable,
    PatientNotFound,
)
from caregap.p6.models import (
    FeatureRow,
    FeatureSchema,
    PatientPage,
    PatientRecord,
    ServiceInfo,
)
from caregap.p6.snapshot import SnapshotP6Client
from caregap.structured import StructuredCaller
from tests.factories import (
    EVAL_AS_OF,
    bp_panel,
    build_record,
    condition,
    feature_row,
    patient,
    procedure,
)
from tests.unit.measures.test_goldens import PERSONAS, snapshot_client

HTN = "59621000"
ESRD = "46177005"
HOSPICE = "385763009"
CLINIC = "Springfield Clinic"
PHONE = "555-0100"
RUN = "run1"
PID = "p1"
TID = thread_id(RUN, PID)
CONFIG: RunnableConfig = {"configurable": {"thread_id": TID}}
REDUCED = ("verdicts", "revision_feedback", "decisions", "agent_errors", "trace")
SECRET = "SECRET-PAYLOAD-NEVER-PERSISTED"


# --- doubles ---------------------------------------------------------------------------------


class FakeP6:
    """A ``P6Client`` returning one raw record (unmasked) or raising a scripted error."""

    def __init__(
        self,
        record: PatientRecord | None = None,
        *,
        record_error: Exception | None = None,
        features_error: Exception | None = None,
    ) -> None:
        self.record = record
        self.record_error = record_error
        self.features_error = features_error

    def healthz(self) -> ServiceInfo:
        return ServiceInfo(service_version="fake", schema_version=3, feature_version="v1")

    def features_schema(self) -> FeatureSchema:
        return FeatureSchema(feature_version="v1", valuesets_version="2026.08")

    def list_patients(self, *, limit: int, offset: int) -> PatientPage:
        return PatientPage(items=[], total=0)

    def get_record(
        self, patient_id: str, *, to: date, sections: Sequence[str] = ENGINE_SECTIONS
    ) -> PatientRecord:
        if self.record_error is not None:
            raise self.record_error
        assert self.record is not None
        return self.record

    def get_features(self, patient_id: str, *, as_of: date) -> FeatureRow:
        if self.features_error is not None:
            raise self.features_error
        return FeatureRow.model_validate(feature_row(patient_id, as_of))


def deps_for(
    p6: FakeP6 | SnapshotP6Client,
    validator: Sequence[str] = (),
    drafter: Sequence[str] = (),
) -> GraphDeps:
    return GraphDeps(
        p6=p6,
        models=fake_bundle(list(validator), list(drafter)),
        engine=default_engine(),
        run_store=RunStore(":memory:"),
        outbox=InMemoryOutbox(),
        structured=StructuredCaller(),
        value_sets=load_value_sets(),
        clinic_name=CLINIC,
        clinic_phone=PHONE,
    )


def verdict_json(
    decision: str,
    *,
    category: str | None = None,
    evidence_ids: Sequence[str] = (),
    confidence: str = "high",
    measure_id: str = "CBP",
) -> str:
    return json.dumps(
        {
            "measure_id": measure_id,
            "decision": decision,
            "exclusion_category": category,
            "evidence_ids": list(evidence_ids),
            "rule_citation": "cbp/exclusions/esrd" if category else "cbp/numerator/controlled",
            "confidence": confidence,
            "rationale": "scripted",
        }
    )


CBP_MESSAGE = (
    "Hello,\n\n"
    f"This is a message from {CLINIC}. We found one thing that is due.\n"
    "Please call us to book a visit so we can check your blood pressure.\n"
    f"You can call us at {PHONE}. We are happy to help.\n\n"
    f"{OPT_OUT_LINE}"
)
CBP_PLAN_JSON = json.dumps(
    {
        "gaps": [{"measure_id": "CBP", "urgency": "soon", "rationale": "BP not controlled."}],
        "actions": [
            {
                "measure_id": "CBP",
                "kind": "schedule_visit",
                "detail": "Book a BP recheck visit.",
                "owner": "care_team",
            }
        ],
        "patient_message": CBP_MESSAGE,
        "provider_note": "CBP open: representative BP above goal on 2025-03-01.",
    }
)


# --- records -----------------------------------------------------------------------------------


def clean_record() -> PatientRecord:
    """CBP / BCS / COL open, nothing escalated (female, 65, hypertension, 150/95 in MY)."""
    return build_record(
        conditions=[condition(HTN)],
        observations=bp_panel(effective=date(2025, 3, 1), sbp=150, dbp=95),
    )


def e6_record() -> PatientRecord:
    """CBP needs_review with the measure-scoped E6 (hypertension abated inside the MY)."""
    return build_record(
        conditions=[condition(HTN, abatement=date(2025, 6, 1))],
        observations=bp_panel(effective=date(2025, 3, 1), sbp=150, dbp=95),
    )


def esrd_unknown_birth_record() -> PatientRecord:
    """Denominator unknown (no birth date) so the engine computes no exclusion, but the ESRD
    condition ``c2`` sits in the packet: a verifiable ``exclude`` for the validator."""
    return build_record(
        patient_header=patient(birth_date=None),
        conditions=[
            condition(HTN, abatement=date(2025, 6, 1)),
            condition(ESRD, onset=date(2021, 1, 1)),
        ],
        observations=bp_panel(effective=date(2025, 3, 1), sbp=150, dbp=95),
    )


def e1_record() -> PatientRecord:
    """Hospice one day before the MY: global E1 on every eligible measure."""
    return build_record(
        conditions=[condition(HTN)],
        observations=bp_panel(effective=date(2025, 3, 1), sbp=150, dbp=95),
        procedures=[procedure(HOSPICE, performed=date(2024, 12, 31))],
    )


def unknown_birth_record() -> PatientRecord:
    return build_record(
        patient_header=patient(birth_date=None),
        conditions=[condition(HTN)],
        observations=bp_panel(effective=date(2025, 3, 1), sbp=150, dbp=95),
    )


# --- state plumbing -----------------------------------------------------------------------------


def apply(state: GapState, update: dict[str, Any]) -> GapState:
    """What LangGraph does with a node's partial update (reducers on the ``⊕`` keys)."""
    merged = cast(dict[str, Any], dict(state))
    for key, value in update.items():
        merged[key] = operator.add(merged.get(key, []), value) if key in REDUCED else value
    return cast(GapState, merged)


def run_nodes(deps: GraphDeps, names: Sequence[str], options: RunOptions) -> GapState:
    state = initial_state(RUN, PID, EVAL_AS_OF, options)
    for name in names:
        factory: Callable[[GraphDeps], nodes.NodeFn] = getattr(nodes, name)
        state = apply(state, factory(deps)(state))
    return state


def loaded(deps: GraphDeps, options: RunOptions) -> GapState:
    return run_nodes(deps, ("load_record", "evaluate_measures"), options)


def opts(*measures: MeasureId, **overrides: Any) -> RunOptions:
    return RunOptions(measures=measures or RunOptions().measures, **overrides)


def decision(action: str, **overrides: Any) -> ApprovalDecision:
    base: dict[str, Any] = {"decision_id": "d1", "action": action, "reviewer": "dr"}
    base.update(overrides)
    return ApprovalDecision(**base)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[datetime]]:
    """``nodes._now`` ticks one second per call from a fixed origin; the list records calls."""
    calls: list[datetime] = []
    origin = datetime(2026, 1, 1, tzinfo=UTC)

    def tick() -> datetime:
        moment = origin + timedelta(seconds=len(calls))
        calls.append(moment)
        return moment

    monkeypatch.setattr(nodes, "_now", tick)
    yield calls


def trace_nodes(state: GapState) -> list[str]:
    return [t.node for t in state["trace"]]


# --- load_record --------------------------------------------------------------------------------


def test_load_record_masks_the_record_and_summarises_context(clock: list[datetime]) -> None:
    future = condition("44054006", onset=date(2026, 1, 15))  # after as_of: masked away
    record = build_record(conditions=[condition(HTN), future])
    deps = deps_for(FakeP6(record))
    state = run_nodes(deps, ("load_record",), opts())
    assert [c.code for c in state["record"].conditions] == [HTN]
    assert state["record"].as_of == EVAL_AS_OF
    assert state["features"] is not None
    assert state["features"].patient_id == PID
    summary = state["context"]
    assert (summary.as_of, summary.my_start, summary.my_end, summary.age_at_my_end) == (
        EVAL_AS_OF,
        date(2025, 1, 1),
        date(2025, 12, 31),
        65,
    )
    assert "load_error" not in state
    (trace,) = state["trace"]
    assert (trace.node, trace.started_at, trace.finished_at, trace.duration_ms) == (
        "load_record",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:01+00:00",
        1000,
    )
    assert edges.after_load(state) == "evaluate_measures"


def test_load_record_treats_missing_features_as_optional() -> None:
    deps = deps_for(FakeP6(clean_record(), features_error=P6Unavailable(SECRET)))
    state = run_nodes(deps, ("load_record",), opts())
    assert state["features"] is None
    assert "record" in state and "load_error" not in state


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (PatientNotFound(SECRET), "not_found"),
        (P6Unavailable(SECRET), "unavailable"),
        (P6ContractError(SECRET), "contract"),
        (P6Error(SECRET), "unavailable"),
    ],
    ids=["not_found", "unavailable", "contract", "base_p6_error"],
)
def test_load_record_maps_p6_errors_to_load_error(error: Exception, kind: str) -> None:
    deps = deps_for(FakeP6(record_error=error))
    update = nodes.load_record(deps)(initial_state(RUN, PID, EVAL_AS_OF, opts()))
    assert update["load_error"] == LoadError(kind=kind, detail=type(error).__name__)  # type: ignore[arg-type]
    assert set(update) == {"load_error", "trace"}
    assert SECRET not in repr(update), "exception text never enters the state"
    assert (
        edges.after_load(apply(initial_state(RUN, PID, EVAL_AS_OF, opts()), update)) == "finalize"
    )


# --- evaluate_measures --------------------------------------------------------------------------


def test_evaluate_measures_over_the_tony_snapshot() -> None:
    client = snapshot_client()
    deps = deps_for(client)
    state = initial_state(RUN, PERSONAS["Tony"], EVAL_AS_OF, opts())
    state = apply(state, nodes.load_record(deps)(state))
    state = apply(state, nodes.evaluate_measures(deps)(state))
    verdicts = {e.measure_id: e.verdict for e in state["evaluations"]}
    assert verdicts == {
        "CBP": "closed",
        "EED": "gap_open",
        "BCS": "not_eligible",
        "COL": "gap_open",
        "SPC": "not_eligible",
        "SPD": "closed",
        "TSC": "closed",
        "SNS": "closed",
    }
    # Equal scores: rank ties break on measure id, sources are engine-only.
    assert [(g.measure_id, g.rank, g.source, g.priority_score) for g in state["open_gaps"]] == [
        ("COL", 1, "engine", 2.0),
        ("EED", 2, "engine", 2.0),
    ]
    assert state["review_items"] == []
    assert trace_nodes(state) == ["load_record", "evaluate_measures"]
    assert edges.after_evaluate(state) == "draft_actions"


def test_evaluate_measures_honours_the_measure_selection() -> None:
    state = loaded(deps_for(FakeP6(clean_record())), opts("BCS", "CBP"))
    assert [e.measure_id for e in state["evaluations"]] == ["BCS", "CBP"]
    assert [(g.measure_id, g.rank) for g in state["open_gaps"]] == [("CBP", 1), ("BCS", 2)]


# --- validate_gaps ------------------------------------------------------------------------------


def test_validate_gaps_confirm_open_keeps_the_gap_as_validator_confirmed(
    clock: list[datetime],
) -> None:
    deps = deps_for(FakeP6(e6_record()), validator=[verdict_json("confirm_open")])
    state = loaded(deps, opts("CBP", "BCS"))
    assert edges.after_evaluate(state) == "validate_gaps"
    assert [g.measure_id for g in state["open_gaps"]] == ["BCS"], "engine-only view first"

    state = apply(state, nodes.validate_gaps(deps)(state))
    assert [(g.measure_id, g.source, g.rank) for g in state["open_gaps"]] == [
        ("CBP", "validator_confirmed", 1),
        ("BCS", "engine", 2),
    ]
    assert state["open_gaps"][0].evidence[0].event_date == date(2025, 3, 1)
    assert state["review_items"] == []
    (verdict,) = state["verdicts"]
    assert (verdict.decision, verdict.verified) == ("confirm_open", True)
    assert state["agent_errors"] == []
    trace = state["trace"][-1]
    assert (trace.node, trace.model_id, trace.prompt_version, trace.case_key) == (
        "validate_gaps",
        "fake",
        VALIDATOR_PROMPT_SHA,
        validator_case_key(PID, "CBP", EVAL_AS_OF),
    )
    assert edges.after_validate(state) == "draft_actions"


def test_validate_gaps_verified_exclude_removes_the_candidate() -> None:
    script = verdict_json("exclude", category="esrd", evidence_ids=["c2"])
    deps = deps_for(FakeP6(esrd_unknown_birth_record()), validator=[script])
    state = loaded(deps, opts("CBP"))
    assert state["evaluations"][0].verdict == "needs_review"
    state = apply(state, nodes.validate_gaps(deps)(state))
    (verdict,) = state["verdicts"]
    assert (verdict.decision, verdict.verified, verdict.exclusion_category) == (
        "exclude",
        True,
        "esrd",
    )
    assert state["open_gaps"] == []
    assert state["review_items"] == []
    assert edges.after_validate(state) == "finalize"
    state = apply(state, nodes.finalize(deps)(state))
    outcome = state["outcome"]
    assert outcome.engine_verdicts == {"CBP": "needs_review"}
    assert outcome.final_statuses == {"CBP": "excluded"}
    assert (outcome.status, outcome.actionable) == ("no_action", False)


def test_validate_gaps_wrong_measure_id_cannot_touch_another_measure() -> None:
    """V4: the packet's measure is authoritative — a validator answering for EED while the
    packet is CBP yields a CBP needs_review verdict; EED keeps its engine verdict."""
    script = verdict_json("exclude", category="esrd", evidence_ids=["c2"], measure_id="EED")
    deps = deps_for(FakeP6(esrd_unknown_birth_record()), validator=[script])
    state = loaded(deps, opts())
    engine = {e.measure_id: e.verdict for e in state["evaluations"]}
    assert engine["CBP"] == "needs_review" and "EED" in engine
    state = apply(state, nodes.validate_gaps(deps)(state))
    (verdict,) = state["verdicts"]
    assert (verdict.measure_id, verdict.decision, verdict.verified) == (
        "CBP",
        "needs_human",
        False,
    )
    assert verdict.verification_note is not None
    assert "measure_id EED does not match packet CBP" in verdict.verification_note
    state = apply(state, nodes.finalize(deps)(state))
    outcome = state["outcome"]
    assert outcome.final_statuses == {**engine, "CBP": "needs_review"}
    assert outcome.final_statuses["EED"] == engine["EED"]


def test_validate_gaps_unverifiable_exclude_becomes_a_review_item() -> None:
    script = verdict_json("exclude", category="frailty", evidence_ids=["c2"])
    deps = deps_for(FakeP6(esrd_unknown_birth_record()), validator=[script])
    state = loaded(deps, opts("CBP"))
    state = apply(state, nodes.validate_gaps(deps)(state))
    (verdict,) = state["verdicts"]
    assert (verdict.decision, verdict.verified) == ("needs_human", False)
    assert verdict.verification_note is not None
    (item,) = state["review_items"]
    assert (item.measure_id, item.scope) == ("CBP", "measure")
    assert item.reason.startswith("validator_needs_human: ")
    assert "frailty" in item.reason
    assert state["open_gaps"] == []
    assert edges.after_validate(state) == "await_approval"


def test_validate_gaps_garbage_output_fails_closed() -> None:
    deps = deps_for(FakeP6(e6_record()), validator=["I cannot help", "still not JSON"])
    state = loaded(deps, opts("CBP"))
    state = apply(state, nodes.validate_gaps(deps)(state))
    assert state["verdicts"] == []
    assert state["open_gaps"] == []
    (item,) = state["review_items"]
    assert (item.measure_id, item.scope, item.reason) == ("CBP", "measure", "validator_failed")
    assert [ref.event_id for ref in item.evidence] == ["c1"], "escalation evidence carried"
    assert state["agent_errors"] == [
        AgentError(
            node="validate_gaps",
            schema_name="ValidationVerdict",
            error_class="JudgeParseError",
            attempt=2,
        )
    ]
    assert "cannot help" not in repr(state["agent_errors"] + state["review_items"])


def test_validate_gaps_unexpected_exception_fails_closed() -> None:
    deps = deps_for(FakeP6(e6_record()), validator=[])  # exhausted fake raises StopIteration
    state = loaded(deps, opts("CBP"))
    state = apply(state, nodes.validate_gaps(deps)(state))
    assert [i.reason for i in state["review_items"]] == ["validator_failed"]
    (error,) = state["agent_errors"]
    assert (error.error_class, error.attempt, error.schema_name) == (
        "StopIteration",
        1,
        "ValidationVerdict",
    )


def test_validate_gaps_global_escalation_holds_everything_without_a_model_call() -> None:
    deps = deps_for(FakeP6(e1_record()), validator=[])
    state = loaded(deps, opts("CBP", "BCS"))
    state = apply(state, nodes.validate_gaps(deps)(state))
    items = state["review_items"]
    assert [(i.measure_id, i.scope, i.reason) for i in items] == [
        (None, "global", "E1: hospice event within 90 days before the measurement year"),
        ("CBP", "measure", "escalated:E1"),
        ("BCS", "measure", "escalated:E1"),
    ]
    assert [ref.event_id for ref in items[0].evidence] == ["pr1"]
    assert state["open_gaps"] == []
    assert state["verdicts"] == []
    assert state["agent_errors"] == [], "the validator was never invoked"
    trace = state["trace"][-1]
    assert (trace.model_id, trace.case_key) == (None, None)
    assert edges.after_validate(state) == "await_approval"


def test_validate_gaps_validation_mode_off_is_engine_only() -> None:
    deps = deps_for(FakeP6(e6_record()), validator=[])
    state = loaded(deps, opts("CBP", "BCS", validation_mode="off"))
    state = apply(state, nodes.validate_gaps(deps)(state))
    (item,) = state["review_items"]
    assert (item.measure_id, item.reason) == ("CBP", "escalated:E6")
    assert [(g.measure_id, g.rank, g.source) for g in state["open_gaps"]] == [("BCS", 1, "engine")]
    assert state["verdicts"] == [] and state["agent_errors"] == []
    assert edges.after_validate(state) == "draft_actions"


def test_validate_gaps_engine_needs_review_without_escalation_is_held() -> None:
    deps = deps_for(FakeP6(unknown_birth_record()), validator=[])
    state = loaded(deps, opts("CBP"))
    assert edges.after_evaluate(state) == "validate_gaps"
    state = apply(state, nodes.validate_gaps(deps)(state))
    (item,) = state["review_items"]
    assert item.reason.startswith("engine_needs_review: ")
    assert "birth_date unknown" in item.reason
    assert state["open_gaps"] == [] and state["verdicts"] == []
    assert edges.after_validate(state) == "await_approval"


# --- draft_actions ------------------------------------------------------------------------------


def test_draft_actions_falls_back_to_the_template(clock: list[datetime]) -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["not json", "still not json"])
    state = loaded(deps, opts("CBP", "BCS"))
    state = apply(state, nodes.draft_actions(deps)(state))
    plan = state["plan"]
    assert plan is not None
    assert [(g.measure_id, g.rank) for g in plan.gaps] == [("CBP", 1), ("BCS", 2)]
    assert [(a.action_id, a.measure_id) for a in plan.actions] == [("a1", "CBP"), ("a2", "BCS")]
    assert CLINIC in plan.patient_message and PHONE in plan.patient_message
    assert plan.patient_message.endswith(OPT_OUT_LINE)
    assert state["draft_error"] is not None
    assert state["draft_error"].startswith("drafter_output_error:")
    assert state["agent_errors"] == [
        AgentError(
            node="draft_actions",
            schema_name="CareActionPlan",
            error_class="DrafterFallback",
            attempt=2,
        )
    ]
    trace = state["trace"][-1]
    assert (trace.node, trace.model_id, trace.prompt_version, trace.case_key) == (
        "draft_actions",
        "fake",
        DRAFTER_PROMPT_SHA,
        drafter_case_key(PID, EVAL_AS_OF, 0),
    )


def test_draft_actions_keeps_a_lint_clean_model_plan() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=[CBP_PLAN_JSON])
    state = loaded(deps, opts("CBP"))
    state = apply(state, nodes.draft_actions(deps)(state))
    plan = state["plan"]
    assert plan is not None
    assert state["draft_error"] is None
    assert state["agent_errors"] == []
    assert plan.patient_message == CBP_MESSAGE
    assert [(a.action_id, a.kind) for a in plan.actions] == [("a1", "schedule_visit")]
    assert [(g.measure_id, g.rank) for g in plan.gaps] == [("CBP", 1)]


def test_draft_actions_unexpected_exception_still_yields_a_template_plan() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=[])  # exhausted fake raises StopIteration
    state = loaded(deps, opts("CBP"))
    state = apply(state, nodes.draft_actions(deps)(state))
    assert state["plan"] is not None
    assert [g.measure_id for g in state["plan"].gaps] == ["CBP"]
    assert state["draft_error"] == "drafter_failed: StopIteration"
    (error,) = state["agent_errors"]
    assert (error.node, error.error_class, error.attempt) == ("draft_actions", "StopIteration", 1)


def test_draft_actions_uses_the_revision_in_the_case_key_and_passes_feedback() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["x", "y"])
    state = loaded(deps, opts("CBP"))
    state = apply(state, {"revision_count": 1, "revision_feedback": ["shorter, please"]})
    state = apply(state, nodes.draft_actions(deps)(state))
    assert state["trace"][-1].case_key == drafter_case_key(PID, EVAL_AS_OF, 1)


# --- await_approval -----------------------------------------------------------------------------


def test_await_approval_auto_mode_records_auto_reviewed_without_interrupting(
    clock: list[datetime],
) -> None:
    deps = deps_for(FakeP6(clean_record()))
    state = loaded(deps, opts("CBP", approval_mode="auto"))
    # Outside a LangGraph runtime ``interrupt`` cannot be called: a plain call proves no pause.
    update = nodes.await_approval(deps)(state)
    expected = ApprovalDecision(decision_id=f"auto:{TID}", action="auto_reviewed", reviewer="auto")
    assert update["decision"] == expected
    assert update["decisions"] == [expected]
    assert [t.node for t in update["trace"]] == ["await_approval"]
    assert set(update) == {"decision", "decisions", "trace"}, "never writes status"


def test_await_approval_interrupt_mode_pauses_with_a_state_built_request() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["x", "y"])
    graph = build_graph(deps, MemorySaver(serde=checkpoint_serializer()))
    result = graph.invoke(initial_state(RUN, PID, EVAL_AS_OF, opts("CBP")), CONFIG)
    (raised,) = result["__interrupt__"]
    request = request_from_interrupt(raised)
    assert pending_request(graph, TID) == request
    assert (request.run_id, request.patient_id, request.as_of) == (RUN, PID, EVAL_AS_OF)
    assert [g.measure_id for g in request.open_gaps] == ["CBP"]
    assert request.plan is not None and request.draft_error is not None
    assert request.revision_count == 0
    assert request.prompt_versions == {
        "validator": VALIDATOR_PROMPT_SHA,
        "drafter": DRAFTER_PROMPT_SHA,
        "version": PROMPT_VERSION,
    }
    assert request.model_ids == {"validator_model": "fake", "drafter_model": "fake"}
    assert "decision" not in result, "the interrupting node's writes are discarded"
    assert "outcome" not in result


# --- record_decision ----------------------------------------------------------------------------


def drafted(deps: GraphDeps, options: RunOptions) -> GapState:
    state = loaded(deps, options)
    return apply(state, nodes.draft_actions(deps)(state))


def test_record_decision_edit_finalizes_the_edited_plan() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["x", "y"])
    state = drafted(deps, opts("CBP", "BCS"))
    edited = CareActionPlan(
        gaps=[
            PlannedGap(measure_id="BCS", rank=7, rationale="mammogram due"),
            PlannedGap(measure_id="CBP", rank=9, rationale="bp due"),
        ],
        actions=[
            GapAction(action_id="zz", measure_id="BCS", kind="screening", detail="mammogram"),
            GapAction(action_id="", measure_id="CBP", kind="schedule_visit", detail="bp visit"),
        ],
        patient_message=CBP_MESSAGE,
        provider_note="edited",
    )
    state = apply(state, {"decision": decision("edit", edited_plan=edited)})
    update = nodes.record_decision(deps)(state)
    plan = update["plan"]
    assert [(g.measure_id, g.rank) for g in plan.gaps] == [("CBP", 1), ("BCS", 2)]
    assert [(a.action_id, a.measure_id) for a in plan.actions] == [("a1", "CBP"), ("a2", "BCS")]
    assert plan.provider_note == "edited"
    assert [t.node for t in update["trace"]] == ["record_decision"]


def test_record_decision_revise_bumps_the_revision_and_keeps_feedback() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["x", "y"])
    state = drafted(deps, opts("CBP"))
    state = apply(state, {"decision": decision("revise", feedback="shorter")})
    update = nodes.record_decision(deps)(state)
    assert update["revision_feedback"] == ["shorter"]
    assert update["revision_count"] == 1
    assert "plan" not in update and "open_gaps" not in update
    state = apply(state, update)
    assert edges.after_decision(state) == "draft_actions"
    assert state["revision_feedback"] == ["shorter"]


def test_record_decision_revise_reopens_a_held_measure_as_a_reviewer_gap() -> None:
    deps = deps_for(FakeP6(e6_record()), validator=[])
    state = loaded(deps, opts("CBP", "BCS", validation_mode="off"))
    state = apply(state, nodes.validate_gaps(deps)(state))
    assert [i.measure_id for i in state["review_items"]] == ["CBP"]
    resolution = ReviewResolution(measure_id="CBP", status="open", reason="chart confirms")
    state = apply(
        state,
        {"decision": decision("revise", feedback="add CBP", review_resolutions=[resolution])},
    )
    update = nodes.record_decision(deps)(state)
    assert [(g.measure_id, g.source, g.rank) for g in update["open_gaps"]] == [
        ("CBP", "reviewer", 1),
        ("BCS", "engine", 2),
    ]
    assert update["review_items"] == []


def test_record_decision_approve_changes_nothing_but_the_trace() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["x", "y"])
    state = drafted(deps, opts("CBP"))
    state = apply(state, {"decision": decision("approve")})
    update = nodes.record_decision(deps)(state)
    assert set(update) == {"trace"}


# --- finalize -----------------------------------------------------------------------------------


def approved_state(deps: GraphDeps, action: str, options: RunOptions, **overrides: Any) -> GapState:
    state = drafted(deps, options)
    return apply(state, {"decision": decision(action, **overrides)})


def outbox_of(deps: GraphDeps) -> InMemoryOutbox:
    assert isinstance(deps.outbox, InMemoryOutbox)
    return deps.outbox


@pytest.mark.parametrize("action", ["approve", "edit"])
def test_finalize_writes_the_outbox_for_approve_and_edit_in_interrupt_mode(action: str) -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["x", "y"])
    extra = {"edited_plan": CareActionPlan()} if action == "edit" else {}
    state = approved_state(deps, action, opts("CBP", "BCS"), **extra)
    if action == "edit":
        state = apply(
            state,
            {
                "plan": drafted(
                    deps_for(FakeP6(clean_record()), drafter=["x", "y"]), opts("CBP", "BCS")
                )["plan"]
            },
        )
    state = apply(state, nodes.finalize(deps)(state))
    outcome = state["outcome"]
    assert (outcome.status, outcome.actionable, outcome.decision_action) == (
        "completed",
        True,
        action,
    )
    assert outcome.approved_actions == ["a1", "a2"]
    entries = outbox_of(deps).entries
    assert [
        (e.thread_id, e.action_id, e.measure_id, e.approval_ref, e.run_id, e.patient_id)
        for e in entries
    ] == [
        (TID, "a1", "CBP", "d1", RUN, PID),
        (TID, "a2", "BCS", "d1", RUN, PID),
    ]
    assert all(e.created_at for e in entries)
    plan = state["plan"]
    assert plan is not None
    assert [(e.kind, e.detail, e.owner) for e in entries] == [
        (a.kind, a.detail, a.owner) for a in plan.actions
    ]
    assert outcome.engine_verdicts == {"CBP": "gap_open", "BCS": "gap_open"}
    assert outcome.final_statuses == {"CBP": "gap_open", "BCS": "gap_open"}


def test_finalize_is_idempotent_on_the_outbox() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["x", "y"])
    state = approved_state(deps, "approve", opts("CBP"))
    nodes.finalize(deps)(state)
    nodes.finalize(deps)(state)
    assert [e.action_id for e in outbox_of(deps).entries] == ["a1"]


def test_finalize_never_writes_the_outbox_in_auto_mode() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["x", "y"])
    state = drafted(deps, opts("CBP", approval_mode="auto"))
    state = apply(state, nodes.await_approval(deps)(state))
    state = apply(state, nodes.finalize(deps)(state))
    outcome = state["outcome"]
    assert (outcome.status, outcome.actionable, outcome.decision_action) == (
        "completed",
        False,
        "auto_reviewed",
    )
    assert outcome.approved_actions == []
    assert outbox_of(deps).entries == []


def test_finalize_never_writes_the_outbox_on_reject() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["x", "y"])
    state = approved_state(deps, "reject", opts("CBP"))
    state = apply(state, nodes.finalize(deps)(state))
    outcome = state["outcome"]
    assert (outcome.status, outcome.actionable, outcome.approved_actions) == ("rejected", False, [])
    assert outbox_of(deps).entries == []


def test_finalize_approve_without_a_plan_is_completed_but_not_actionable() -> None:
    deps = deps_for(FakeP6(e6_record()), validator=["garbage", "garbage"])
    state = loaded(deps, opts("CBP"))
    state = apply(state, nodes.validate_gaps(deps)(state))
    state = apply(state, {"decision": decision("approve")})
    state = apply(state, nodes.finalize(deps)(state))
    outcome = state["outcome"]
    assert (outcome.status, outcome.actionable) == ("completed", False)
    assert outcome.final_statuses == {"CBP": "needs_review"}
    assert outbox_of(deps).entries == []


def test_finalize_without_a_decision_is_no_action() -> None:
    deps = deps_for(FakeP6(clean_record()))
    state = loaded(deps, opts("TSC"))  # no encounter in the MY: not eligible, no candidate
    assert edges.after_evaluate(state) == "finalize"
    state = apply(state, nodes.finalize(deps)(state))
    outcome = state["outcome"]
    assert (outcome.status, outcome.actionable, outcome.decision_action) == (
        "no_action",
        False,
        None,
    )
    assert outcome.engine_verdicts == {"TSC": "not_eligible"}


def test_finalize_on_a_load_error_is_error() -> None:
    deps = deps_for(FakeP6(record_error=PatientNotFound("nope")))
    state = run_nodes(deps, ("load_record", "finalize"), opts())
    outcome = state["outcome"]
    assert (outcome.status, outcome.actionable) == ("error", False)
    assert outcome.load_error == LoadError(kind="not_found", detail="PatientNotFound")
    assert outcome.engine_verdicts == {} and outcome.final_statuses == {}
    assert trace_nodes(state) == ["load_record", "finalize"]


def test_finalize_after_an_exhausted_revision_budget_is_no_action() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["x", "y"])
    state = approved_state(deps, "revise", opts("CBP"), feedback="again")
    state = apply(state, {"revision_count": 2})
    assert edges.after_decision(state) == "finalize"
    state = apply(state, nodes.finalize(deps)(state))
    outcome = state["outcome"]
    assert (outcome.status, outcome.actionable, outcome.decision_action) == (
        "no_action",
        False,
        "revise",
    )
    assert outbox_of(deps).entries == []


def test_finalize_applies_reviewer_resolutions_to_measures_only() -> None:
    deps = deps_for(FakeP6(e1_record()), validator=[])
    state = loaded(deps, opts("CBP", "BCS"))
    state = apply(state, nodes.validate_gaps(deps)(state))
    resolutions = [
        ReviewResolution(measure_id=None, status="not_eligible", reason="hospice continued"),
        ReviewResolution(measure_id="CBP", status="closed", reason="controlled at home"),
    ]
    state = apply(state, {"decision": decision("approve", review_resolutions=resolutions)})
    state = apply(state, nodes.finalize(deps)(state))
    outcome = state["outcome"]
    assert outcome.engine_verdicts == {"CBP": "needs_review", "BCS": "needs_review"}
    assert outcome.final_statuses == {"CBP": "closed", "BCS": "needs_review"}
    assert (outcome.status, outcome.actionable) == ("completed", False)


# --- end to end through the compiled graph -------------------------------------------------------


def graph_for(deps: GraphDeps) -> Any:
    return build_graph(deps, MemorySaver(serde=checkpoint_serializer()))


def test_every_node_appends_exactly_one_trace_end_to_end(clock: list[datetime]) -> None:
    deps = deps_for(
        FakeP6(e6_record()),
        validator=[verdict_json("confirm_open")],
        drafter=["x", "y"],
    )
    graph = graph_for(deps)
    graph.invoke(initial_state(RUN, PID, EVAL_AS_OF, opts("CBP", "BCS")), CONFIG)
    final = graph.invoke(Command(resume=decision("approve").model_dump(mode="json")), CONFIG)
    assert [t.node for t in final["trace"]] == [
        "load_record",
        "evaluate_measures",
        "validate_gaps",
        "draft_actions",
        "await_approval",
        "record_decision",
        "finalize",
    ]
    for trace in final["trace"]:
        assert trace.duration_ms == 1000
        assert trace.started_at < trace.finished_at
    assert final["outcome"].status == "completed"
    assert final["outcome"].actionable is True
    assert [e.action_id for e in outbox_of(deps).entries] == ["a1", "a2"]
    assert [d.action for d in final["decisions"]] == ["approve"]
    assert pending_request(graph, TID) is None


def test_revise_then_approve_round_trip() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["x", "y", "x", "y"])
    graph = graph_for(deps)
    first = graph.invoke(initial_state(RUN, PID, EVAL_AS_OF, opts("CBP")), CONFIG)
    assert request_from_interrupt(first["__interrupt__"][0]).revision_count == 0

    revise = decision("revise", feedback="shorter, please", decision_id="d-revise")
    second = graph.invoke(Command(resume=revise.model_dump(mode="json")), CONFIG)
    request = request_from_interrupt(second["__interrupt__"][0])
    assert request.revision_count == 1
    assert pending_request(graph, TID) == request
    assert second["revision_feedback"] == ["shorter, please"]
    assert outbox_of(deps).entries == [], "nothing approved yet"

    final = graph.invoke(Command(resume=decision("approve").model_dump(mode="json")), CONFIG)
    assert [t.node for t in final["trace"]] == [
        "load_record",
        "evaluate_measures",
        "draft_actions",
        "await_approval",
        "record_decision",
        "draft_actions",
        "await_approval",
        "record_decision",
        "finalize",
    ]
    assert [d.decision_id for d in final["decisions"]] == ["d-revise", "d1"]
    assert final["outcome"].status == "completed"
    assert [e.approval_ref for e in outbox_of(deps).entries] == ["d1"]
    assert final["trace"][5].case_key == drafter_case_key(PID, EVAL_AS_OF, 1)


def test_auto_mode_runs_to_completion_without_pausing() -> None:
    deps = deps_for(FakeP6(clean_record()), drafter=["x", "y"])
    graph = graph_for(deps)
    final = graph.invoke(
        initial_state(RUN, PID, EVAL_AS_OF, opts("CBP", approval_mode="auto")), CONFIG
    )
    assert "__interrupt__" not in final
    assert pending_request(graph, TID) is None
    outcome = final["outcome"]
    assert (outcome.status, outcome.actionable, outcome.decision_action) == (
        "completed",
        False,
        "auto_reviewed",
    )
    assert final["decision"].decision_id == f"auto:{TID}"
    assert outbox_of(deps).entries == []


def test_no_candidates_finalize_directly() -> None:
    client = snapshot_client()
    deps = deps_for(client)
    graph = graph_for(deps)
    kayce = PERSONAS["Kayce"]
    final = graph.invoke(
        initial_state(RUN, kayce, EVAL_AS_OF, opts()),
        {"configurable": {"thread_id": thread_id(RUN, kayce)}},
    )
    assert [t.node for t in final["trace"]] == ["load_record", "evaluate_measures", "finalize"]
    assert final["outcome"].status == "no_action"
    assert set(final["outcome"].engine_verdicts.values()) == {"not_eligible"}


def test_load_error_finalizes_as_error_end_to_end() -> None:
    deps = deps_for(FakeP6(record_error=P6Unavailable("down")))
    final = graph_for(deps).invoke(initial_state(RUN, PID, EVAL_AS_OF, opts()), CONFIG)
    assert [t.node for t in final["trace"]] == ["load_record", "finalize"]
    assert final["outcome"].status == "error"
    assert final["outcome"].load_error == LoadError(kind="unavailable", detail="P6Unavailable")


def test_global_hold_pauses_for_review_with_no_plan() -> None:
    deps = deps_for(FakeP6(e1_record()))
    graph = graph_for(deps)
    result = graph.invoke(initial_state(RUN, PID, EVAL_AS_OF, opts("CBP", "BCS")), CONFIG)
    request = request_from_interrupt(result["__interrupt__"][0])
    assert request.plan is None and request.open_gaps == []
    assert [i.scope for i in request.review_items] == ["global", "measure", "measure"]
    assert [t.node for t in result["trace"]] == [
        "load_record",
        "evaluate_measures",
        "validate_gaps",
    ]
    final = graph.invoke(Command(resume=decision("reject").model_dump(mode="json")), CONFIG)
    assert final["outcome"].status == "rejected"
    assert outbox_of(deps).entries == []


def test_two_runs_are_deterministic() -> None:
    record = e6_record()

    def run() -> tuple[dict[str, Any], list[ReviewItem]]:
        deps = deps_for(
            FakeP6(record), validator=[verdict_json("confirm_open")], drafter=["x", "y"]
        )
        graph = graph_for(deps)
        graph.invoke(initial_state(RUN, PID, EVAL_AS_OF, opts("CBP", "BCS")), CONFIG)
        request = pending_request(graph, TID)
        assert request is not None
        return request.model_dump(mode="json"), request.review_items

    assert run() == run()
