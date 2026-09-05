"""``GET /v1/panel`` — P6's patient page (birth date / sex / deceased only) joined with the
latest patient run per patient from the ledger."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from caregap.api.deps import RuntimeDep
from caregap.api.errors import p6_error, problem_responses
from caregap.api.schemas import LastRun, PanelItem, PanelResponse
from caregap.p6.client import P6Error

router = APIRouter(tags=["panel"])

#: P6's own page bounds (``/v1/patients``: 1..1000).
MAX_LIMIT = 1000
DEFAULT_LIMIT = 50


@router.get("/v1/panel", response_model=PanelResponse, responses=problem_responses(502, 503))
def panel(
    runtime: RuntimeDep,
    as_of: Annotated[date | None, Query(description="Restrict last_run to runs at this anchor")] = (
        None
    ),
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PanelResponse:
    try:
        page = runtime.p6.list_patients(limit=limit, offset=offset)
    except P6Error as exc:
        raise p6_error(exc) from exc
    latest = runtime.run_store.latest_patient_runs(
        [item.patient_id for item in page.items], as_of=as_of
    )
    items = [
        PanelItem(
            patient_id=item.patient_id,
            sex=item.sex,
            birth_date=item.birth_date,
            deceased=item.deceased,
            last_run=(
                LastRun(run_id=latest[item.patient_id][0], status=latest[item.patient_id][1])
                if item.patient_id in latest
                else None
            ),
        )
        for item in page.items
    ]
    return PanelResponse(items=items, total=page.total, as_of=as_of, limit=limit, offset=offset)
