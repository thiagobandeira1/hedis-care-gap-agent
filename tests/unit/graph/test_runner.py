"""``PatientRunner`` mechanics over a self-contained mini graph (same ``GapState``, same
interrupt / ``Command`` protocol as the real graph, no P6, no engine, no models).

The mini graph scripts behaviour by patient id: ``boom-*`` raises, ``slow-*`` sleeps past the
timeout, ``quiet-*`` finishes without an interrupt, ``holdstart-*`` / ``hold-*`` park on the
test's ``Gate`` (before the interrupt / after the decision is applied); everything else
interrupts with an ``ApprovalRequest`` exactly like ``await_approval``. Scenario tests over
the REAL graph live in ``test_scenarios.py``.
"""

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from caregap.agents.schemas import CareActionPlan, GapAction, PlannedGap
from caregap.graph import nodes
from caregap.graph.build import GraphDeps, checkpoint_serializer
from caregap.graph.hitl import pending_request
from caregap.graph.outbox import InMemoryOutbox
from caregap.graph.runner import (
    DecisionValidationError,
    NotAwaitingApproval,
    PatientRunner,
)
from caregap.graph.runstore import RunStore
from caregap.graph.state import (
    ApprovalDecision,
    ApprovalRequest,
    GapState,
    NodeTrace,
    RunOptions,
    RunOutcome,
    thread_id,
)
from caregap.llm import fake_bundle
from caregap.measures.engine import default_engine
from caregap.measures.models import OpenGap
from caregap.measures.value_sets import load_value_sets
from caregap.p6.snapshot import SnapshotP6Client
from caregap.structured import StructuredCaller

SNAPSHOT_DIR = Path(__file__).resolve().parents[3] / "synthetic" / "p6_snapshots"
AS_OF = date(2025, 12, 31)
SLOW_SECONDS = 3.0
TIMEOUT_S = 0.3

BOOM = "boom-"
SLOW = "slow-"
QUIET = "quiet-"
HOLD_START = "holdstart-"
HOLD = "hold-"
GATE_MAX_WAIT_S = 10.0


class Gate:
    """Parks a scripted node until the test releases it; ``entered`` says it is parked."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def arrive(self) -> None:
        self.entered.set()
        assert self.release.wait(GATE_MAX_WAIT_S), "gate never released"


GATE = Gate()

PLAN = CareActionPlan(
    gaps=[PlannedGap(measure_id="BCS", rank=1, urgency="routine", rationale="due")],
    actions=[
        GapAction(action_id="a1", measure_id="BCS", kind="screening", detail="Book a mammogram")
    ],
    patient_message="Hello, a breast cancer screening is due. Reply STOP to opt out.",
    provider_note="BCS open; no mammogram in the window.",
)
OPEN_GAPS = [OpenGap(measure_id="BCS", priority_score=2.0, rank=1)]


# --- the mini graph ------------------------------------------------------------------------


def _trace(node: str) -> NodeTrace:
    now = datetime.now(UTC).isoformat()
    return NodeTrace(node=node, started_at=now, finished_at=now, duration_ms=0)


def _work(state: GapState) -> dict[str, Any]:
    patient_id = state["patient_id"]
    if patient_id.startswith(BOOM):
        raise RuntimeError("scripted node failure")
    if patient_id.startswith(SLOW):
        time.sleep(SLOW_SECONDS)
    if patient_id.startswith(HOLD_START):
        GATE.arrive()
    return {"plan": PLAN, "trace": [_trace("work")]}


def _after_work(state: GapState) -> str:
    return "finalize" if state["patient_id"].startswith(QUIET) else "await_approval"


def _await_approval(state: GapState) -> dict[str, Any]:
    if state["options"].approval_mode == "auto":
        decision = ApprovalDecision(
            decision_id=f"auto:{state['run_id']}:{state['patient_id']}",
            action="auto_reviewed",
            reviewer="system",
        )
    else:
        request = ApprovalRequest(
            run_id=state["run_id"],
            patient_id=state["patient_id"],
            as_of=state["as_of"],
            open_gaps=OPEN_GAPS,
            plan=PLAN,
            revision_count=state["revision_count"],
        )
        decision = ApprovalDecision.model_validate(interrupt(request.model_dump(mode="json")))
    return {"decision": decision, "decisions": [decision], "trace": [_trace("await_approval")]}


def _record_decision(state: GapState) -> dict[str, Any]:
    if state["patient_id"].startswith(HOLD):
        GATE.arrive()
    decision = state["decision"]
    update: dict[str, Any] = {"trace": [_trace("record_decision")]}
    if decision.action == "revise":
        update["revision_count"] = state["revision_count"] + 1
        update["revision_feedback"] = [decision.feedback or ""]
    return update


def _after_record(state: GapState) -> str:
    return "await_approval" if state["decision"].action == "revise" else "finalize"


def _finalize(state: GapState) -> dict[str, Any]:
    decision = state.get("decision")
    if decision is None:
        outcome = RunOutcome(status="no_action", actionable=False)
    elif decision.action == "reject":
        outcome = RunOutcome(status="rejected", actionable=False, decision_action="reject")
    else:
        actionable = decision.action in {"approve", "edit"}
        outcome = RunOutcome(
            status="completed",
            actionable=actionable,
            approved_actions=["a1"] if actionable else [],
            decision_action=decision.action,
        )
    return {"outcome": outcome, "trace": [_trace("finalize")]}


def build_mini_graph(
    checkpointer: BaseCheckpointSaver[Any], *, finalize: nodes.NodeFn | None = None
) -> Any:
    builder: StateGraph[GapState] = StateGraph(GapState)
    builder.add_node("work", _work)
    builder.add_node("await_approval", _await_approval)
    builder.add_node("record_decision", _record_decision)
    builder.add_node("finalize", finalize or _finalize)
    builder.add_edge(START, "work")
    builder.add_conditional_edges("work", _after_work, ["await_approval", "finalize"])
    builder.add_edge("await_approval", "record_decision")
    builder.add_conditional_edges("record_decision", _after_record, ["await_approval", "finalize"])
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)


_VALUE_SETS = load_value_sets()


def real_deps(run_store: RunStore, *, outbox: InMemoryOutbox | None = None) -> GraphDeps:
    """A real ``GraphDeps`` (the pinned contract) over the committed snapshots and keyless
    fakes. The mini graph reads none of it (unless it borrows the real ``finalize``, which
    writes ``outbox``); the runner reads ``run_store`` and the clinic strings (edited-plan
    re-lint)."""
    return GraphDeps(
        p6=SnapshotP6Client(SNAPSHOT_DIR),
        models=fake_bundle([], []),
        engine=default_engine(),
        run_store=run_store,
        outbox=outbox or InMemoryOutbox(),
        structured=StructuredCaller(),
        value_sets=_VALUE_SETS,
        clinic_name="Demo Primary Care",
        clinic_phone="555-0100",
    )


def make_runner(
    store: RunStore | None = None,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    timeout_s: float = 30.0,
    real_finalize: bool = False,
    outbox: InMemoryOutbox | None = None,
) -> tuple[PatientRunner, Any, RunStore]:
    """``real_finalize`` swaps the scripted finalize for ``nodes.finalize(deps)``, the only
    outbox writer, so its guards can be exercised over the mini graph."""
    store = store or RunStore(":memory:")
    deps = real_deps(store, outbox=outbox)
    graph = build_mini_graph(
        checkpointer or MemorySaver(),
        finalize=nodes.finalize(deps) if real_finalize else None,
    )
    runner = PatientRunner(graph, deps, timeout_s=timeout_s)
    return runner, graph, store


def decision(action: str, decision_id: str = "d1", **extra: Any) -> ApprovalDecision:
    return ApprovalDecision(decision_id=decision_id, action=action, reviewer="nurse", **extra)  # type: ignore[arg-type]


def as_json(result: object) -> str:
    assert isinstance(result, RunOutcome | ApprovalRequest)
    return json.dumps(result.model_dump(mode="json"), sort_keys=True)


@pytest.fixture
def store() -> RunStore:
    return RunStore(":memory:")


@pytest.fixture
def gate() -> Iterator[Gate]:
    GATE.entered.clear()
    GATE.release.clear()
    try:
        yield GATE
    finally:
        GATE.release.set()  # never leave a parked worker behind


# --- start -----------------------------------------------------------------------------------


def test_start_interrupts_and_persists_pending(store: RunStore) -> None:
    runner, graph, _ = make_runner(store)
    result = runner.start("r1", "p1", AS_OF, RunOptions())
    assert isinstance(result, ApprovalRequest)
    assert (result.run_id, result.patient_id, result.revision_count) == ("r1", "p1", 0)
    assert store.get_run("r1") is not None
    row = store.get_patient_run("r1", "p1")
    assert row is not None
    assert row.status == "awaiting_approval"
    assert row.pending == result
    assert row.outcome is None
    assert pending_request(graph, thread_id("r1", "p1")) == result


def test_start_without_interrupt_persists_outcome(store: RunStore) -> None:
    runner, graph, _ = make_runner(store)
    result = runner.start("r1", "quiet-p1", AS_OF, RunOptions())
    assert isinstance(result, RunOutcome)
    assert result.status == "no_action"
    row = store.get_patient_run("r1", "quiet-p1")
    assert row is not None
    assert (row.status, row.outcome, row.pending) == ("no_action", result, None)
    assert pending_request(graph, thread_id("r1", "quiet-p1")) is None


def test_start_marks_status_running_before_invoking(store: RunStore) -> None:
    seen: list[str] = []
    original = store.upsert_patient_run

    def spy(run_id: str, patient_id: str, status: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(status)
        return original(run_id, patient_id, status, *args, **kwargs)

    store.upsert_patient_run = spy  # type: ignore[method-assign]
    runner, _, _ = make_runner(store)
    runner.start("r1", "p1", AS_OF, RunOptions())
    assert seen == ["running", "awaiting_approval"]


def test_start_node_failure_is_an_error_outcome_never_raises(store: RunStore) -> None:
    runner, _, _ = make_runner(store)
    result = runner.start("r1", "boom-p1", AS_OF, RunOptions())
    assert result == RunOutcome(status="error", actionable=False)
    row = store.get_patient_run("r1", "boom-p1")
    assert row is not None
    assert (row.status, row.outcome) == ("error", result)


def test_start_timeout_abandons_worker_and_reports_error(store: RunStore) -> None:
    runner, _, _ = make_runner(store, timeout_s=TIMEOUT_S)
    started = time.monotonic()
    result = runner.start("r1", "slow-p1", AS_OF, RunOptions())
    elapsed = time.monotonic() - started
    assert result == RunOutcome(status="error", actionable=False)
    assert elapsed < SLOW_SECONDS, "the runner must give up at the timeout, not at the end"
    row = store.get_patient_run("r1", "slow-p1")
    assert row is not None
    assert row.status == "error"


def test_start_store_failure_never_raises() -> None:
    class BrokenStore(RunStore):
        def __init__(self) -> None:
            super().__init__(":memory:")

        def get_run(self, run_id: str) -> NoReturn:
            raise sqlite3.OperationalError("disk gone")

        def upsert_patient_run(self, *args: Any, **kwargs: Any) -> NoReturn:
            raise sqlite3.OperationalError("disk gone")

    graph = build_mini_graph(MemorySaver())
    runner = PatientRunner(graph, real_deps(BrokenStore()), timeout_s=5)
    assert runner.start("r1", "p1", AS_OF, RunOptions()) == RunOutcome(
        status="error", actionable=False
    )


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        PatientRunner(build_mini_graph(MemorySaver()), real_deps(RunStore(":memory:")), timeout_s=0)


def test_auto_mode_completes_without_interrupt(store: RunStore) -> None:
    runner, graph, _ = make_runner(store)
    result = runner.start("r1", "p1", AS_OF, RunOptions(approval_mode="auto"))
    assert isinstance(result, RunOutcome)
    assert (result.status, result.actionable, result.decision_action) == (
        "completed",
        False,
        "auto_reviewed",
    )
    assert pending_request(graph, thread_id("r1", "p1")) is None


# --- resume ----------------------------------------------------------------------------------


def test_resume_approve_completes_and_records_decision(store: RunStore) -> None:
    runner, graph, _ = make_runner(store)
    request = runner.start("r1", "p1", AS_OF, RunOptions())
    result = runner.resume("r1", "p1", decision("approve"))
    assert isinstance(result, RunOutcome)
    assert (result.status, result.actionable, result.decision_action) == (
        "completed",
        True,
        "approve",
    )
    row = store.get_patient_run("r1", "p1")
    assert row is not None
    # The store keeps the request its outcome resolved (approvals derive ``resolved``).
    assert (row.status, row.outcome, row.pending) == ("completed", result, request)
    assert [a.status for a in store.list_approvals()] == ["resolved"]
    stored = store.get_decision("d1")
    assert stored is not None
    assert stored.result == result
    assert pending_request(graph, thread_id("r1", "p1")) is None


def test_resume_is_idempotent_on_decision_id(store: RunStore) -> None:
    runner, graph, _ = make_runner(store)
    runner.start("r1", "p1", AS_OF, RunOptions())
    first = runner.resume("r1", "p1", decision("approve"))
    invocations: list[object] = []
    original = graph.invoke

    def spy(payload: object, *args: Any, **kwargs: Any) -> Any:
        invocations.append(payload)
        return original(payload, *args, **kwargs)

    graph.invoke = spy
    second = runner.resume("r1", "p1", decision("approve"))
    assert as_json(second) == as_json(first)
    assert invocations == [], "a replayed decision never touches the graph"
    # Even a different action under the same id replays the stored result.
    third = runner.resume("r1", "p1", decision("reject"))
    assert as_json(third) == as_json(first)


def test_resume_reject(store: RunStore) -> None:
    runner, _, _ = make_runner(store)
    runner.start("r1", "p1", AS_OF, RunOptions())
    result = runner.resume("r1", "p1", decision("reject"))
    assert isinstance(result, RunOutcome)
    assert (result.status, result.actionable) == ("rejected", False)
    row = store.get_patient_run("r1", "p1")
    assert row is not None
    assert row.status == "rejected"


def test_resume_when_not_pending_raises_409_style(store: RunStore) -> None:
    runner, _, _ = make_runner(store)
    with pytest.raises(NotAwaitingApproval):
        runner.resume("r1", "never-started", decision("approve"))
    runner.start("r1", "quiet-p1", AS_OF, RunOptions())
    with pytest.raises(NotAwaitingApproval):
        runner.resume("r1", "quiet-p1", decision("approve", "d2"))
    assert store.get_decision("d1") is None
    assert store.get_decision("d2") is None


def test_resume_invalid_decision_raises_value_error_with_error_list(store: RunStore) -> None:
    runner, graph, _ = make_runner(store)
    runner.start("r1", "p1", AS_OF, RunOptions())
    with pytest.raises(DecisionValidationError) as info:
        runner.resume("r1", "p1", decision("revise"))  # revise without feedback
    assert isinstance(info.value, ValueError)
    assert info.value.errors and info.value.args == (info.value.errors,)
    assert all(isinstance(e, str) and e for e in info.value.errors)
    # Nothing recorded, the request is still pending, the patient row untouched.
    assert store.get_decision("d1") is None
    assert pending_request(graph, thread_id("r1", "p1")) is not None
    row = store.get_patient_run("r1", "p1")
    assert row is not None
    assert row.status == "awaiting_approval"


def test_resume_edit_requires_edited_plan(store: RunStore) -> None:
    runner, _, _ = make_runner(store)
    runner.start("r1", "p1", AS_OF, RunOptions())
    with pytest.raises(DecisionValidationError):
        runner.resume("r1", "p1", decision("edit"))


def test_revise_loops_back_then_budget_is_enforced(store: RunStore) -> None:
    runner, _, _ = make_runner(store)
    first = runner.start("r1", "p1", AS_OF, RunOptions(max_revisions=1))
    assert isinstance(first, ApprovalRequest)
    second = runner.resume("r1", "p1", decision("revise", "d1", feedback="shorter please"))
    assert isinstance(second, ApprovalRequest)
    assert second.revision_count == 1
    row = store.get_patient_run("r1", "p1")
    assert row is not None
    assert (row.status, row.pending) == ("awaiting_approval", second)
    stored = store.get_decision("d1")
    assert stored is not None
    assert stored.result == second
    with pytest.raises(DecisionValidationError):
        runner.resume("r1", "p1", decision("revise", "d2", feedback="again"))
    assert store.get_decision("d2") is None
    final = runner.resume("r1", "p1", decision("approve", "d3"))
    assert isinstance(final, RunOutcome)
    assert final.status == "completed"


def test_resume_node_failure_records_error_outcome(store: RunStore) -> None:
    runner, graph, _ = make_runner(store)
    runner.start("r1", "p1", AS_OF, RunOptions())

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("checkpointer gone")

    graph.invoke = explode
    result = runner.resume("r1", "p1", decision("approve"))
    assert result == RunOutcome(status="error", actionable=False)
    row = store.get_patient_run("r1", "p1")
    assert row is not None
    assert row.status == "error"
    stored = store.get_decision("d1")
    assert stored is not None
    assert stored.result == result


# --- concurrency and timeouts ------------------------------------------------------------


def abandoned_worker(tid: str) -> threading.Thread:
    return next(t for t in threading.enumerate() if t.name == f"caregap-{tid}")


def test_concurrent_decisions_on_one_thread_apply_exactly_one(store: RunStore) -> None:
    """Two reviewers race on one pending approval with different decision ids: exactly one
    decision reaches the graph and the ledger; the other is refused (409), not recorded."""
    runner, graph, _ = make_runner(store)
    runner.start("r1", "p1", AS_OF, RunOptions())
    tid = thread_id("r1", "p1")
    # Without per-thread serialization both racers pass the pending check and meet here.
    barrier = threading.Barrier(2)
    invocations: list[object] = []
    original = graph.invoke

    def gated(payload: object, *args: Any, **kwargs: Any) -> Any:
        invocations.append(payload)
        # The loser never arrives (it waits for the thread lock), so the barrier breaks.
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=0.5)
        return original(payload, *args, **kwargs)

    graph.invoke = gated
    results: dict[str, object] = {}

    def submit(label: str, submitted: ApprovalDecision) -> None:
        try:
            results[label] = runner.resume("r1", "p1", submitted)
        except NotAwaitingApproval as exc:
            results[label] = exc

    racers = [
        threading.Thread(target=submit, args=("approve", decision("approve", "dA"))),
        threading.Thread(target=submit, args=("reject", decision("reject", "dB"))),
    ]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join(10)
        assert not racer.is_alive()

    assert len(invocations) == 1, "exactly one decision may resume the interrupt"
    outcomes = [r for r in results.values() if isinstance(r, RunOutcome)]
    refused = [r for r in results.values() if isinstance(r, NotAwaitingApproval)]
    assert (len(outcomes), len(refused)) == (1, 1)
    stored = [store.get_decision(d) for d in ("dA", "dB")]
    assert sum(s is not None for s in stored) == 1, "the loser is never recorded"
    winner = next(s for s in stored if s is not None)
    assert winner.result == outcomes[0]
    assert winner.action == next(k for k, v in results.items() if isinstance(v, RunOutcome))
    row = store.get_patient_run("r1", "p1")
    assert row is not None
    assert row.status == outcomes[0].status
    assert pending_request(graph, tid) is None


def test_resume_timeout_abandoned_worker_never_writes_outbox(store: RunStore, gate: Gate) -> None:
    """A resume that times out after the decision was applied but before ``finalize`` ran:
    the runner records ``error``; when the abandoned worker later reaches the real
    ``finalize`` it must refuse the outbox write and agree with the ledger."""
    outbox = InMemoryOutbox()
    runner, graph, _ = make_runner(store, timeout_s=TIMEOUT_S, real_finalize=True, outbox=outbox)
    request = runner.start("r1", "hold-p1", AS_OF, RunOptions())
    assert isinstance(request, ApprovalRequest)
    tid = thread_id("r1", "hold-p1")

    result = runner.resume("r1", "hold-p1", decision("approve"))
    assert result == RunOutcome(status="error", actionable=False)
    assert gate.entered.wait(5), "the worker should be parked past the timeout"
    assert outbox.entries == []
    worker = abandoned_worker(tid)
    assert worker.is_alive()

    gate.release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert outbox.entries == [], "an abandoned attempt never writes the outbox"
    row = store.get_patient_run("r1", "hold-p1")
    assert row is not None
    assert row.status == "error"
    assert row.outcome is not None and row.outcome.status == "error"
    stored = store.get_decision("d1")
    assert stored is not None
    assert stored.result == result
    assert pending_request(graph, tid) is None
    checkpoint = graph.get_state({"configurable": {"thread_id": tid}}).values["outcome"]
    assert checkpoint.status == "error" and checkpoint.approved_actions == []


def test_start_timeout_leftover_interrupt_is_not_resumable(store: RunStore, gate: Gate) -> None:
    """A start that times out before the interrupt: the abandoned worker still checkpoints
    one, but the ledger says ``error`` and never listed a request, so the interrupt is
    inert: never in the approvals queue, refused by ``resume``, nothing reaches the outbox."""
    outbox = InMemoryOutbox()
    runner, graph, _ = make_runner(store, timeout_s=TIMEOUT_S, real_finalize=True, outbox=outbox)
    tid = thread_id("r1", "holdstart-p1")
    result = runner.start("r1", "holdstart-p1", AS_OF, RunOptions())
    assert result == RunOutcome(status="error", actionable=False)
    assert gate.entered.wait(5)
    worker = abandoned_worker(tid)
    gate.release.set()
    worker.join(10)
    assert not worker.is_alive()

    assert pending_request(graph, tid) is not None, "the checkpoint does hold an interrupt"
    assert store.list_approvals(status="pending") == []
    with pytest.raises(NotAwaitingApproval):
        runner.resume("r1", "holdstart-p1", decision("approve"))
    assert store.get_decision("d1") is None
    assert outbox.entries == []
    row = store.get_patient_run("r1", "holdstart-p1")
    assert row is not None
    assert row.status == "error"


# --- supersession ----------------------------------------------------------------------------


def test_newer_completed_run_supersedes_older_pending_request(store: RunStore) -> None:
    runner, _, _ = make_runner(store)
    older = runner.start("r1", "p1", AS_OF, RunOptions())
    assert isinstance(older, ApprovalRequest)
    assert [(a.run_id, a.patient_id) for a in store.list_approvals(status="pending")] == [
        ("r1", "p1")
    ]
    newer = runner.start("r2", "p1", AS_OF, RunOptions(approval_mode="auto"))
    assert isinstance(newer, RunOutcome)
    superseded = store.list_approvals(status="superseded")
    assert [(a.run_id, a.patient_id) for a in superseded] == [("r1", "p1")]
    assert store.list_approvals(status="pending") == []


def test_error_outcome_does_not_supersede(store: RunStore) -> None:
    runner, _, _ = make_runner(store)
    runner.start("r1", "p1", AS_OF, RunOptions())
    runner.start("r2", "boom-p1", AS_OF, RunOptions())
    assert [(a.run_id, a.patient_id) for a in store.list_approvals(status="pending")] == [
        ("r1", "p1")
    ]


# --- run_panel -------------------------------------------------------------------------------


def test_run_panel_is_sequential_and_tracks_cursor(store: RunStore) -> None:
    cursors: list[tuple[str, int | None]] = []
    original = store.update_run_status

    def spy(run_id: str, status: str, cursor: int | None = None) -> Any:
        cursors.append((status, cursor))
        return original(run_id, status, cursor=cursor)

    store.update_run_status = spy  # type: ignore[method-assign]
    runner, _, _ = make_runner(store)
    results = runner.run_panel("r1", ["quiet-a", "p-b", "boom-c"], AS_OF, RunOptions(), cancel=None)
    assert [type(r).__name__ for r in results] == ["RunOutcome", "ApprovalRequest", "RunOutcome"]
    assert cursors == [("running", 0), ("running", 1), ("running", 2), ("completed", 3)]
    run = store.get_run("r1")
    assert run is not None
    assert run.status == "completed"
    statuses = {row.patient_id: row.status for row in store.list_patient_runs("r1")}
    assert statuses == {"quiet-a": "no_action", "p-b": "awaiting_approval", "boom-c": "error"}


def test_run_panel_stops_when_cancelled(store: RunStore) -> None:
    cancel = threading.Event()
    original = store.upsert_patient_run

    def cancel_after_first(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if args[2] != "running":
            cancel.set()
        return result

    store.upsert_patient_run = cancel_after_first  # type: ignore[method-assign]
    runner, _, _ = make_runner(store)
    results = runner.run_panel("r1", ["quiet-a", "quiet-b", "quiet-c"], AS_OF, RunOptions(), cancel)
    assert len(results) == 1
    run = store.get_run("r1")
    assert run is not None
    assert run.status == "cancelled"
    assert store.get_patient_run("r1", "quiet-b") is None


def test_run_panel_pre_cancelled_runs_nothing(store: RunStore) -> None:
    cancel = threading.Event()
    cancel.set()
    runner, _, _ = make_runner(store)
    assert runner.run_panel("r1", ["quiet-a"], AS_OF, RunOptions(), cancel) == []
    run = store.get_run("r1")
    assert run is not None
    assert run.status == "cancelled"


# --- SqliteSaver round trip -----------------------------------------------------------------


@pytest.fixture
def sqlite_saver(tmp_path: Path) -> Iterator[Callable[[], SqliteSaver]]:
    path = tmp_path / "checkpoints.sqlite"
    connections: list[sqlite3.Connection] = []

    def open_saver() -> SqliteSaver:
        conn = sqlite3.connect(path, check_same_thread=False)
        connections.append(conn)
        return SqliteSaver(conn, serde=checkpoint_serializer())

    yield open_saver
    for conn in connections:
        conn.close()


def test_sqlite_checkpointer_round_trip(
    store: RunStore, sqlite_saver: Callable[[], SqliteSaver]
) -> None:
    runner1, _, _ = make_runner(store, checkpointer=sqlite_saver())
    request = runner1.start("r1", "p1", AS_OF, RunOptions())
    assert isinstance(request, ApprovalRequest)

    # A brand-new graph instance over the same file (a fresh API process) sees the pending
    # request purely from the checkpoint, then resumes it to completion.
    runner2, graph2, _ = make_runner(store, checkpointer=sqlite_saver())
    revived = pending_request(graph2, thread_id("r1", "p1"))
    assert revived is not None
    assert as_json(revived) == as_json(request)
    result = runner2.resume("r1", "p1", decision("approve"))
    assert isinstance(result, RunOutcome)
    assert result.status == "completed"
    assert pending_request(graph2, thread_id("r1", "p1")) is None
    row = store.get_patient_run("r1", "p1")
    assert row is not None
    assert row.status == "completed"
