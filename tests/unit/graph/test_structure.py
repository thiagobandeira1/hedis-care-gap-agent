"""Structural guarantees (SPEC section 3): the compiled node set and edge set equal the SPEC
diagram, ``await_approval`` is the only interrupt site, ``finalize`` the only outbox writer,
and the conditional edges are pure functions of state."""

import ast
import inspect
import re
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver

from caregap.agents.schemas import CareActionPlan
from caregap.graph import edges, nodes
from caregap.graph.build import NODE_NAMES, GraphDeps, build_graph, checkpoint_serializer
from caregap.graph.outbox import InMemoryOutbox
from caregap.graph.runstore import RunStore
from caregap.graph.state import (
    ApprovalDecision,
    GapState,
    LoadError,
    RunOptions,
    initial_state,
)
from caregap.llm import fake_bundle
from caregap.measures.engine import default_engine
from caregap.measures.ids import MeasureId
from caregap.measures.models import (
    EscalationFlag,
    MeasureEvaluation,
    OpenGap,
    ReviewItem,
    TriResult,
)
from caregap.measures.value_sets import load_value_sets
from caregap.p6.snapshot import SnapshotP6Client
from caregap.structured import StructuredCaller
from tests.factories import EVAL_AS_OF

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = REPO_ROOT / "docs" / "SPEC.md"
SNAPSHOT_DIR = REPO_ROOT / "synthetic" / "p6_snapshots"
START, END = "__start__", "__end__"


def make_deps() -> GraphDeps:
    return GraphDeps(
        p6=SnapshotP6Client(SNAPSHOT_DIR),
        models=fake_bundle([], []),
        engine=default_engine(),
        run_store=RunStore(":memory:"),
        outbox=InMemoryOutbox(),
        structured=StructuredCaller(),
        value_sets=load_value_sets(),
        clinic_name="Springfield Clinic",
        clinic_phone="555-0100",
    )


# --- SPEC diagram ------------------------------------------------------------------------------


def spec_state_diagram() -> str:
    text = SPEC.read_text(encoding="utf-8")
    match = re.search(r"```mermaid\s*\n(stateDiagram-v2.*?)```", text, re.DOTALL)
    assert match is not None, "SPEC section 3 must carry a stateDiagram-v2 mermaid block"
    return match.group(1)


def diagram_transitions(block: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for line in block.splitlines():
        match = re.match(r"\s*(\S+)\s*-->\s*([^\s:]+)", line)
        if match is None:
            continue
        source, target = match.groups()
        found.add((START if source == "[*]" else source, END if target == "[*]" else target))
    return found


def diagram_nodes(block: str) -> set[str]:
    return {
        name for pair in diagram_transitions(block) for name in pair if name not in {START, END}
    }


def test_diagram_node_set_equals_node_names() -> None:
    assert diagram_nodes(spec_state_diagram()) == set(NODE_NAMES)


def test_compiled_graph_node_set_equals_node_names() -> None:
    graph = build_graph(make_deps(), MemorySaver(serde=checkpoint_serializer()))
    compiled = set(graph.get_graph().nodes) - {START, END}
    assert compiled == set(NODE_NAMES)
    assert len(NODE_NAMES) == len(set(NODE_NAMES))


def test_compiled_graph_edges_equal_diagram_transitions() -> None:
    graph = build_graph(make_deps(), MemorySaver(serde=checkpoint_serializer()))
    compiled = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert compiled == diagram_transitions(spec_state_diagram())


# --- single interrupt site / single outbox writer -----------------------------------------------


def node_sources() -> dict[str, str]:
    source = inspect.getsource(nodes)
    tree = ast.parse(source)
    segments: dict[str, str] = {}
    for item in tree.body:
        if isinstance(item, ast.FunctionDef):
            segment = ast.get_source_segment(source, item)
            assert segment is not None
            segments[item.name] = segment
    return segments


def test_every_node_name_has_a_factory_in_nodes_py() -> None:
    assert set(NODE_NAMES) <= set(node_sources())


def test_only_await_approval_calls_interrupt() -> None:
    callers = {name for name, src in node_sources().items() if "interrupt(" in src}
    assert callers == {"await_approval"}


def test_only_finalize_references_the_outbox() -> None:
    writers = {name for name, src in node_sources().items() if "outbox" in src}
    assert writers == {"finalize"}


def test_await_approval_does_no_io_and_no_model_call() -> None:
    src = node_sources()["await_approval"]
    for token in ("deps.p6", "deps.engine", "deps.structured", "deps.outbox", "deps.run_store"):
        assert token not in src
    assert ".invoke(" not in src


# --- edges: pure functions of state -------------------------------------------------------------


def tri(value: str) -> TriResult:
    return TriResult(value=value)  # type: ignore[arg-type]


def evaluation(
    measure_id: MeasureId,
    verdict: str,
    *,
    escalations: list[EscalationFlag] | None = None,
) -> MeasureEvaluation:
    return MeasureEvaluation(
        measure_id=measure_id,
        rule_version="test",
        denominator=tri("yes"),
        numerator=tri("no"),
        escalations=escalations or [],
        verdict=verdict,  # type: ignore[arg-type]
        priority_score=1.0,
    )


def flag(kind: str, scope: str) -> EscalationFlag:
    return EscalationFlag(kind=kind, scope=scope, reason="test")  # type: ignore[arg-type]


def state(**update: object) -> GapState:
    base = initial_state("run1", "p1", EVAL_AS_OF, RunOptions())
    base.update(update)  # type: ignore[typeddict-item]
    return base


def gap(measure_id: MeasureId, rank: int = 1) -> OpenGap:
    return OpenGap(measure_id=measure_id, priority_score=1.0, rank=rank)


def item(measure_id: MeasureId | None, scope: str) -> ReviewItem:
    return ReviewItem(measure_id=measure_id, scope=scope, reason="test")  # type: ignore[arg-type]


def test_after_load() -> None:
    assert edges.after_load(state()) == "evaluate_measures"
    assert edges.after_load(state(load_error=LoadError(kind="not_found"))) == "finalize"


@pytest.mark.parametrize(
    ("evaluations", "expected"),
    [
        pytest.param([], "finalize", id="no_evaluations"),
        pytest.param(
            [evaluation("CBP", "closed"), evaluation("BCS", "not_eligible")],
            "finalize",
            id="no_candidates",
        ),
        pytest.param(
            [evaluation("CBP", "gap_open"), evaluation("BCS", "gap_open")],
            "draft_actions",
            id="clean_open_gaps",
        ),
        pytest.param(
            [
                evaluation("CBP", "gap_open"),
                evaluation("BCS", "needs_review", escalations=[flag("E6", "measure")]),
            ],
            "validate_gaps",
            id="escalated_candidate",
        ),
        pytest.param(
            [evaluation("CBP", "needs_review", escalations=[flag("E1", "global")])],
            "validate_gaps",
            id="global_escalation",
        ),
        pytest.param(
            [evaluation("CBP", "needs_review")],
            "validate_gaps",
            id="needs_review_without_escalation_is_held",
        ),
    ],
)
def test_after_evaluate(evaluations: list[MeasureEvaluation], expected: str) -> None:
    assert edges.after_evaluate(state(evaluations=evaluations)) == expected


@pytest.mark.parametrize(
    ("open_gaps", "review_items", "expected"),
    [
        pytest.param([gap("CBP")], [item(None, "global")], "await_approval", id="global_hold"),
        pytest.param([gap("CBP")], [item("BCS", "measure")], "draft_actions", id="open_gaps"),
        pytest.param([], [item("BCS", "measure")], "await_approval", id="review_only"),
        pytest.param([], [], "finalize", id="nothing"),
    ],
)
def test_after_validate(
    open_gaps: list[OpenGap], review_items: list[ReviewItem], expected: str
) -> None:
    assert edges.after_validate(state(open_gaps=open_gaps, review_items=review_items)) == expected


def decision(action: str, **kwargs: object) -> ApprovalDecision:
    return ApprovalDecision(decision_id="d1", action=action, reviewer="dr", **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("action", "revision_count", "max_revisions", "expected"),
    [
        pytest.param("revise", 1, 1, "draft_actions", id="revise_within_budget"),
        pytest.param("revise", 2, 1, "finalize", id="revise_budget_exhausted"),
        pytest.param("revise", 2, 3, "draft_actions", id="revise_larger_budget"),
        pytest.param("approve", 0, 1, "finalize", id="approve"),
        pytest.param("edit", 0, 1, "finalize", id="edit"),
        pytest.param("reject", 0, 1, "finalize", id="reject"),
        pytest.param("auto_reviewed", 0, 1, "finalize", id="auto_reviewed"),
    ],
)
def test_after_decision(
    action: str, revision_count: int, max_revisions: int, expected: str
) -> None:
    kwargs = {"feedback": "shorter"} if action == "revise" else {}
    if action == "edit":
        kwargs["edited_plan"] = CareActionPlan()  # type: ignore[assignment]
    st = state(
        options=RunOptions(max_revisions=max_revisions),
        decision=decision(action, **kwargs),
        revision_count=revision_count,
    )
    assert edges.after_decision(st) == expected


def test_after_decision_without_a_decision_finalizes() -> None:
    assert edges.after_decision(state()) == "finalize"


def test_edge_functions_are_pure_over_state() -> None:
    """Edges take the state only: no deps, no I/O, no side effects on the state."""
    for fn in (edges.after_load, edges.after_evaluate, edges.after_validate, edges.after_decision):
        params = list(inspect.signature(fn).parameters)
        assert params == ["state"]
    st = state(
        evaluations=[evaluation("CBP", "gap_open")],
        open_gaps=[gap("CBP")],
        review_items=[],
        decision=decision("approve"),
    )
    before = dict(st)
    edges.after_load(st)
    edges.after_evaluate(st)
    edges.after_validate(st)
    edges.after_decision(st)
    assert dict(st) == before
