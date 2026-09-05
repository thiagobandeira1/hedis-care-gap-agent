"""``GET /v1/patients/{id}/gaps`` — the deterministic engine over the as_of-masked record.
No model call, no ledger write: the same evaluations ``evaluate_measures`` would produce."""

from datetime import date

from fastapi import APIRouter

from caregap.api.deps import RuntimeDep
from caregap.api.errors import p6_error, problem_responses
from caregap.api.schemas import PatientGapsResponse
from caregap.graph.state import MeasurementSummary
from caregap.measures.context import MeasurementContext
from caregap.p6.client import P6Error, mask_as_of

router = APIRouter(tags=["patients"])


@router.get(
    "/v1/patients/{patient_id}/gaps",
    response_model=PatientGapsResponse,
    responses=problem_responses(404, 502, 503),
)
def patient_gaps(patient_id: str, as_of: date, runtime: RuntimeDep) -> PatientGapsResponse:
    try:
        record = mask_as_of(runtime.p6.get_record(patient_id, to=as_of), as_of)
    except P6Error as exc:
        raise p6_error(exc) from exc
    ctx = MeasurementContext.for_(as_of, record.patient.birth_date)
    return PatientGapsResponse(
        patient_id=patient_id,
        as_of=as_of,
        context=MeasurementSummary(
            as_of=ctx.as_of,
            my_start=ctx.my_start,
            my_end=ctx.my_end,
            age_at_my_end=ctx.age_at_my_end,
        ),
        evaluations=runtime.deps.engine.evaluate(record, ctx),
    )
