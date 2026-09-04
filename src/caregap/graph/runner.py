"""``PatientRunner`` — the ONE place that drives the compiled graph for a (run, patient) thread.

It owns the lifecycle around ``graph.invoke``: the run / patient rows in the ``RunStore``,
the per-patient timeout (a worker thread that is abandoned, never killed, on expiry), the
interrupt / completion split (pending is DERIVED from ``hitl.pending_request``, never
stored by a node), decision idempotency and validation on resume, and supersession of older
pending approvals once a newer run finalizes the patient. ``start`` / ``run_panel`` never
raise: every failure becomes ``RunOutcome(status="error")`` plus a patient status of
``error``. ``resume`` raises only for CALLER mistakes — :class:`NotAwaitingApproval` (the
API's 409) and :class:`DecisionValidationError` (422, a ``ValueError`` whose single argument
is the error list) — and turns everything else into an error outcome too.

Logs carry ids, statuses, durations, and error classes only (``logging_setup`` allowlist).
"""

import threading
import time
from collections.abc import Sequence
from datetime import date
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from caregap.graph.build import GraphDeps
from caregap.graph.hitl import pending_request, validate_decision
from caregap.graph.state import (
    ApprovalDecision,
    ApprovalRequest,
    GapState,
    RunOptions,
    RunOutcome,
    initial_state,
    thread_id,
)
from caregap.logging_setup import get_logger

log = get_logger(__name__)

RunResult = RunOutcome | ApprovalRequest
Graph = CompiledStateGraph[Any, Any, Any, Any]

#: Patient statuses the runner writes (completion statuses are ``RunOutcome.status``).
STATUS_RUNNING = "running"
STATUS_AWAITING = "awaiting_approval"
STATUS_ERROR = "error"
#: Run statuses written by ``run_panel``.
RUN_RUNNING = "running"
RUN_CANCELLED = "cancelled"
RUN_COMPLETED = "completed"


class DecisionValidationError(ValueError):
    """The decision does not fit the pending request (the API maps this to 422)."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: list[str] = list(errors)
        super().__init__(self.errors)

    def __str__(self) -> str:
        return "; ".join(self.errors)


class NotAwaitingApproval(RuntimeError):
    """No pending approval exists for this (run, patient) thread (the API maps this to 409)."""

    def __init__(self, run_id: str, patient_id: str) -> None:
        super().__init__(f"run {run_id!r} patient {patient_id!r} is not awaiting approval")
        self.run_id = run_id
        self.patient_id = patient_id


class PatientTimeout(RuntimeError):
    """The graph did not return within ``timeout_s`` (internal; never escapes the runner)."""


class GraphContractError(RuntimeError):
    """The graph ended without an interrupt and without an ``outcome`` (fail closed)."""


class _Invocation:
    """Result box shared with the worker thread."""

    __slots__ = ("error", "result")

    def __init__(self) -> None:
        self.result: dict[str, Any] | None = None
        self.error: Exception | None = None


def _error_outcome() -> RunOutcome:
    return RunOutcome(status="error", actionable=False)


def _outcome_of(result: object) -> RunOutcome:
    outcome = result.get("outcome") if isinstance(result, dict) else None
    if isinstance(outcome, RunOutcome):
        return outcome
    if isinstance(outcome, dict):
        return RunOutcome.model_validate(outcome)
    raise GraphContractError("graph finished without an outcome")


def _options_of(values: object) -> RunOptions:
    options = values.get("options") if isinstance(values, dict) else None
    if isinstance(options, RunOptions):
        return options
    if isinstance(options, dict):
        return RunOptions.model_validate(options)
    return RunOptions()


class PatientRunner:
    def __init__(self, graph: Graph, deps: GraphDeps, *, timeout_s: float) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._graph = graph
        self._deps = deps
        self._store = deps.run_store
        self._timeout_s = timeout_s

    # -- public API --------------------------------------------------------------------

    def start(self, run_id: str, patient_id: str, as_of: date, options: RunOptions) -> RunResult:
        """Run one patient from the start of the graph; never raises."""
        tid = thread_id(run_id, patient_id)
        try:
            self._ensure_run(run_id, as_of, options, [patient_id])
            self._store.upsert_patient_run(run_id, patient_id, STATUS_RUNNING, None, None)
        except Exception as exc:
            return self._fail(run_id, patient_id, tid, exc)
        return self._drive(run_id, patient_id, initial_state(run_id, patient_id, as_of, options))

    def resume(self, run_id: str, patient_id: str, decision: ApprovalDecision) -> RunResult:
        """Apply a reviewer decision to the pending approval of one patient.

        Idempotent on ``decision_id`` (the stored result is returned, the graph untouched).
        Raises :class:`NotAwaitingApproval` / :class:`DecisionValidationError` before any
        side effect; every later failure becomes an error outcome.
        """
        stored = self._store.get_decision(decision.decision_id)
        if stored is not None:
            log.info(
                "decision_replayed",
                run_id=run_id,
                patient_id=patient_id,
                action=decision.action,
            )
            return stored.result
        tid = thread_id(run_id, patient_id)
        pending = pending_request(self._graph, tid)
        if pending is None:
            raise NotAwaitingApproval(run_id, patient_id)
        options = _options_of(self._graph.get_state(self._config(tid)).values)
        errors = validate_decision(
            decision,
            pending,
            max_revisions=options.max_revisions,
            clinic_name=self._deps.clinic_name,
            clinic_phone=self._deps.clinic_phone,
        )
        if errors:
            raise DecisionValidationError(errors)
        try:
            self._store.upsert_patient_run(run_id, patient_id, STATUS_RUNNING, None, None)
        except Exception as exc:
            return self._fail(run_id, patient_id, tid, exc)
        result = self._drive(run_id, patient_id, Command(resume=decision.model_dump(mode="json")))
        self._record_decision(decision, run_id, patient_id, result)
        return result

    def run_panel(
        self,
        run_id: str,
        patient_ids: list[str],
        as_of: date,
        options: RunOptions,
        cancel: threading.Event | None = None,
    ) -> list[RunResult]:
        """Sequential panel loop; persists the cursor as it goes and stops on ``cancel``."""
        results: list[RunResult] = []
        try:
            self._ensure_run(run_id, as_of, options, patient_ids)
        except Exception as exc:
            log.error("run_create_failed", run_id=run_id, error_class=type(exc).__name__)
            return results
        for index, patient_id in enumerate(patient_ids):
            if cancel is not None and cancel.is_set():
                self._set_run_status(run_id, RUN_CANCELLED, index)
                log.info("run_cancelled", run_id=run_id, count=len(results))
                return results
            self._set_run_status(run_id, RUN_RUNNING, index)
            results.append(self.start(run_id, patient_id, as_of, options))
        self._set_run_status(run_id, RUN_COMPLETED, len(patient_ids))
        log.info("run_completed", run_id=run_id, count=len(results))
        return results

    # -- lifecycle ---------------------------------------------------------------------

    def _drive(self, run_id: str, patient_id: str, payload: GapState | Command[Any]) -> RunResult:
        """Invoke the thread once (fresh input or ``Command``); classify the result."""
        tid = thread_id(run_id, patient_id)
        started = time.monotonic()
        try:
            result = self._invoke_with_timeout(payload, tid)
            pending = pending_request(self._graph, tid)
            if pending is not None:
                self._store.upsert_patient_run(run_id, patient_id, STATUS_AWAITING, None, pending)
                log.info(
                    "patient_awaiting_approval",
                    run_id=run_id,
                    patient_id=patient_id,
                    thread_id=tid,
                    duration_ms=_elapsed_ms(started),
                )
                return pending
            outcome = _outcome_of(result)
            self._store.upsert_patient_run(run_id, patient_id, outcome.status, outcome, None)
            if outcome.status != "error":
                self._store.mark_superseded(patient_id, run_id)
        except Exception as exc:
            return self._fail(run_id, patient_id, tid, exc, started=started)
        log.info(
            "patient_finished",
            run_id=run_id,
            patient_id=patient_id,
            thread_id=tid,
            status=outcome.status,
            duration_ms=_elapsed_ms(started),
        )
        return outcome

    def _invoke_with_timeout(self, payload: GapState | Command[Any], tid: str) -> dict[str, Any]:
        box = _Invocation()
        config = self._config(tid)

        def work() -> None:
            try:
                box.result = self._graph.invoke(payload, config=config)
            except Exception as exc:
                box.error = exc

        worker = threading.Thread(target=work, name=f"caregap-{tid}", daemon=True)
        worker.start()
        worker.join(self._timeout_s)
        if worker.is_alive():
            # Abandoned on purpose: Python threads cannot be killed. It may still write its
            # checkpoint later, but the RunStore already says ``error`` for this patient.
            raise PatientTimeout(f"patient thread {tid!r} exceeded {self._timeout_s}s")
        if box.error is not None:
            raise box.error
        if box.result is None:
            raise GraphContractError("graph returned no state")
        return box.result

    def _fail(
        self,
        run_id: str,
        patient_id: str,
        tid: str,
        exc: Exception,
        *,
        started: float | None = None,
    ) -> RunOutcome:
        """Fail closed: log the error class only, persist ``error``, never raise."""
        outcome = _error_outcome()
        log.error(
            "patient_run_failed",
            run_id=run_id,
            patient_id=patient_id,
            thread_id=tid,
            error_class=type(exc).__name__,
            duration_ms=_elapsed_ms(started) if started is not None else None,
        )
        try:
            self._store.upsert_patient_run(run_id, patient_id, STATUS_ERROR, outcome, None)
        except Exception as store_exc:
            log.error(
                "patient_status_write_failed",
                run_id=run_id,
                patient_id=patient_id,
                error_class=type(store_exc).__name__,
            )
        return outcome

    def _record_decision(
        self, decision: ApprovalDecision, run_id: str, patient_id: str, result: RunResult
    ) -> None:
        try:
            fresh = self._store.record_decision(decision, run_id, patient_id, result)
        except Exception as exc:
            log.error(
                "decision_write_failed",
                run_id=run_id,
                patient_id=patient_id,
                error_class=type(exc).__name__,
            )
            return
        if not fresh:
            log.warning("decision_already_recorded", run_id=run_id, patient_id=patient_id)

    def _ensure_run(
        self, run_id: str, as_of: date, options: RunOptions, patient_ids: list[str]
    ) -> None:
        if self._store.get_run(run_id) is None:
            self._store.create_run(run_id, as_of, options, patient_ids)

    def _set_run_status(self, run_id: str, status: str, cursor: int) -> None:
        try:
            self._store.update_run_status(run_id, status, cursor=cursor)
        except Exception as exc:
            log.error("run_status_write_failed", run_id=run_id, error_class=type(exc).__name__)

    @staticmethod
    def _config(tid: str) -> RunnableConfig:
        return {"configurable": {"thread_id": tid}}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
