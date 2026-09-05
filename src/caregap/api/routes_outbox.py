"""``GET /v1/outbox`` — every approved action, in append order; ``approval_ref`` names the
decision that authorised it (``finalize`` is the only writer)."""

from typing import Annotated

from fastapi import APIRouter, Query

from caregap.api.deps import RuntimeDep
from caregap.graph.runstore import OutboxEntry

router = APIRouter(tags=["outbox"])


@router.get("/v1/outbox", response_model=list[OutboxEntry])
def list_outbox(
    runtime: RuntimeDep, run_id: Annotated[str | None, Query()] = None
) -> list[OutboxEntry]:
    return runtime.run_store.list_outbox(run_id)
