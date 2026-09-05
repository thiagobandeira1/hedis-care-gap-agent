"""Panel runs: ``POST /v1/runs`` (202 + a daemon thread around ``PatientRunner.run_panel``),
``GET /v1/runs/{id}``, ``POST /v1/runs/{id}/cancel`` and ``GET /v1/runs/{id}/patients/{pid}``.

The run row is created ``queued`` BEFORE the thread starts, so a client polling right after
the 202 never sees a 404. ``app.state.sync_runs`` (tests only) joins the thread before the
202 is returned; ``app.state.run_threads`` keeps every launched thread by run id.
"""

import secrets
import threading
from typing import Any, Final

from fastapi import APIRouter, Request

from caregap.api.deps import RuntimeDep, require_patient_run, require_run
from caregap.api.errors import UnprocessableError, problem_responses
from caregap.api.schemas import (
    STATE_FIELDS,
    CancelResponse,
    PatientRunDetail,
    PatientState,
    RunAccepted,
    RunDetail,
    RunRequest,
)
from caregap.graph.hitl import pending_request
from caregap.graph.state import thread_id
from caregap.runtime import Runtime

router = APIRouter(tags=["runs"])

QUEUED: Final = "queued"
CANCELLING: Final = "cancelling"
LIVE_RUN_STATUSES: Final = frozenset({QUEUED, "running"})


def new_run_id() -> str:
    return "run_" + secrets.token_hex(6)


def launch_run(runtime: Runtime, request: Request, run_id: str, body: RunRequest) -> None:
    cancel = threading.Event()
    runtime.cancel_flags[run_id] = cancel
    thread = threading.Thread(
        target=runtime.runner.run_panel,
        args=(run_id, list(body.patient_ids), body.as_of, body.options, cancel),
        name=f"caregap-run-{run_id}",
        daemon=True,
    )
    state = request.app.state
    threads: dict[str, threading.Thread] = getattr(state, "run_threads", {})
    threads[run_id] = thread
    state.run_threads = threads
    thread.start()
    if getattr(state, "sync_runs", False):
        thread.join()


@router.post(
    "/v1/runs",
    response_model=RunAccepted,
    status_code=202,
    responses=problem_responses(422),
)
def create_run(body: RunRequest, request: Request, runtime: RuntimeDep) -> RunAccepted:
    cap = runtime.settings.max_patients_per_run
    if len(body.patient_ids) > cap:
        raise UnprocessableError(
            f"patient_ids has {len(body.patient_ids)} entries; the cap is {cap}",
            code="run-too-large",
        )
    duplicates = sorted({p for p in body.patient_ids if body.patient_ids.count(p) > 1})
    if duplicates:
        raise UnprocessableError(f"patient_ids repeats {duplicates}", code="run-duplicate-patients")
    run_id = new_run_id()
    runtime.run_store.create_run(
        run_id, body.as_of, body.options, list(body.patient_ids), status=QUEUED
    )
    launch_run(runtime, request, run_id, body)
    return RunAccepted(run_id=run_id, status=QUEUED)


@router.get("/v1/runs/{run_id}", response_model=RunDetail, responses=problem_responses(404))
def get_run(run_id: str, runtime: RuntimeDep) -> RunDetail:
    run = require_run(runtime, run_id)
    return RunDetail(run=run, patients=runtime.run_store.list_patient_runs(run_id))


@router.post(
    "/v1/runs/{run_id}/cancel",
    response_model=CancelResponse,
    status_code=202,
    responses=problem_responses(404),
)
def cancel_run(run_id: str, runtime: RuntimeDep) -> CancelResponse:
    run = require_run(runtime, run_id)
    runtime.cancel_flags.setdefault(run_id, threading.Event()).set()
    status = CANCELLING if run.status in LIVE_RUN_STATUSES else run.status
    return CancelResponse(run_id=run_id, status=status)


@router.get(
    "/v1/runs/{run_id}/patients/{patient_id}",
    response_model=PatientRunDetail,
    responses=problem_responses(404),
)
def get_patient_run(run_id: str, patient_id: str, runtime: RuntimeDep) -> PatientRunDetail:
    row = require_patient_run(runtime, run_id, patient_id)
    tid = thread_id(run_id, patient_id)
    pending = pending_request(runtime.graph, tid)
    if row.superseded_by is not None and row.outcome is None:
        pending = None  # a newer run finalized this patient: nothing left to decide here
    values: Any = runtime.graph.get_state({"configurable": {"thread_id": tid}}).values
    state = None
    if isinstance(values, dict) and values:
        state = PatientState.model_validate({k: values[k] for k in STATE_FIELDS if k in values})
    return PatientRunDetail(patient_run=row, pending=pending, state=state)
