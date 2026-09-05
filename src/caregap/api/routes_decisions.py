"""``POST /v1/runs/{id}/patients/{pid}/decision`` (validate -> resume; 404 / 409 / 422) and
``GET /v1/approvals``.

Idempotency: a ``decision_id`` already in the ledger replays the stored result with
``replayed=true`` and never touches the graph; the same id on a different patient run is a
409. Supersession is checked here (the interrupt still exists in the checkpoint, so the
runner alone would happily resume a stale request). Validation is the runner's: its
``DecisionValidationError`` list becomes the 422 problem's ``errors``.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from caregap.api.deps import RuntimeDep, require_patient_run
from caregap.api.errors import ConflictError, UnprocessableError, problem_responses
from caregap.api.schemas import DecisionResponse
from caregap.graph.runner import DecisionValidationError, NotAwaitingApproval
from caregap.graph.runstore import ApprovalRecord, ApprovalStatus
from caregap.graph.state import ApprovalDecision

router = APIRouter(tags=["decisions"])


@router.post(
    "/v1/runs/{run_id}/patients/{patient_id}/decision",
    response_model=DecisionResponse,
    responses=problem_responses(404, 409, 422),
)
def decide(
    run_id: str, patient_id: str, decision: ApprovalDecision, runtime: RuntimeDep
) -> DecisionResponse:
    row = require_patient_run(runtime, run_id, patient_id)
    stored = runtime.run_store.get_decision(decision.decision_id)
    if stored is not None:
        if (stored.run_id, stored.patient_id) != (run_id, patient_id):
            raise ConflictError(
                f"decision_id {decision.decision_id!r} was already used for another patient run",
                code="decision-id-reused",
            )
        return DecisionResponse.of(stored.result, replayed=True)
    if row.superseded_by is not None and row.outcome is None:
        raise ConflictError(
            f"the approval request of run {run_id!r} for patient {patient_id!r} was "
            f"superseded by run {row.superseded_by!r}",
            code="superseded",
        )
    try:
        result = runtime.runner.resume(run_id, patient_id, decision)
    except NotAwaitingApproval as exc:
        raise ConflictError(str(exc), code="not-awaiting-approval") from exc
    except DecisionValidationError as exc:
        raise UnprocessableError(
            "decision does not fit the pending request", code="decision-invalid", errors=exc.errors
        ) from exc
    return DecisionResponse.of(result, replayed=False)


@router.get("/v1/approvals", response_model=list[ApprovalRecord])
def list_approvals(
    runtime: RuntimeDep,
    status: Annotated[ApprovalStatus | None, Query()] = None,
) -> list[ApprovalRecord]:
    return runtime.run_store.list_approvals(status)
