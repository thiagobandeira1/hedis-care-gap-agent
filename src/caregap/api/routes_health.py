"""``GET /healthz`` — the service, its P6, and which model mode is wired in."""

from fastapi import APIRouter

from caregap import __version__
from caregap.agents.prompts import PROMPT_VERSION
from caregap.api.deps import RuntimeDep
from caregap.api.errors import p6_error, problem_responses
from caregap.api.schemas import HealthResponse
from caregap.p6.client import P6Error

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse, responses=problem_responses(502, 503))
def healthz(runtime: RuntimeDep) -> HealthResponse:
    try:
        p6 = runtime.p6.healthz()
    except P6Error as exc:
        raise p6_error(exc) from exc
    return HealthResponse(
        status="ok",
        service_version=__version__,
        p6=p6,
        p6_mode=runtime.settings.p6_mode,
        models_mode=runtime.deps.models.mode,
        prompt_version=PROMPT_VERSION,
        engine_measures=list(runtime.deps.engine.measure_ids),
    )
