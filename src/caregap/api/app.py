"""``create_app`` — the FastAPI factory (``uvicorn caregap.api.app:create_app --factory``).

The lifespan builds the ``Runtime`` from ``settings`` (or adopts an injected one, which its
owner closes) and publishes it as ``app.state.runtime``; on shutdown it cancels and joins
every panel thread it launched (``Runtime.stop_runs``) so the ledger never keeps a half-run
``running`` past the process. Every route is a sync ``def``: the
embedded P6 topology drives a Starlette ``TestClient`` in-process, which must never be
called from the event loop thread. All errors are RFC 9457 problem details.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from caregap import __version__
from caregap.api import (
    routes_decisions,
    routes_health,
    routes_measures,
    routes_outbox,
    routes_panel,
    routes_patients,
    routes_runs,
)
from caregap.api.errors import install_error_handlers
from caregap.config import Settings, get_settings
from caregap.measures.ids import CONFORMANCE_NOTICE
from caregap.runtime import Runtime, build_runtime

ROUTERS = (
    routes_health.router,
    routes_measures.router,
    routes_panel.router,
    routes_patients.router,
    routes_runs.router,
    routes_decisions.router,
    routes_outbox.router,
)


def create_app(settings: Settings | None = None, *, runtime: Runtime | None = None) -> FastAPI:
    if settings is None:
        settings = runtime.settings if runtime is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = runtime is None
        active = runtime if runtime is not None else build_runtime(settings)
        app.state.runtime = active
        try:
            yield
        finally:
            app.state.runtime = None
            active.stop_runs(dict(getattr(app.state, "run_threads", {})))
            if owned:
                active.close()

    app = FastAPI(
        title="hedis-care-gap-agent",
        version=__version__,
        description=f"HEDIS care-gap closure agent API ({CONFORMANCE_NOTICE}).",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.runtime = None
    app.state.sync_runs = False
    app.state.run_threads = {}
    install_error_handlers(app)
    for router in ROUTERS:
        app.include_router(router)
    return app
