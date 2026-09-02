"""Embedded P6: the real fhir-feature-service ASGI app in-process, driven by a TestClient.

Entered ONCE as a context manager in P1's lifespan so P6's own lifespan (migrations,
value-set sync) runs. Consequence: every P1 route touching P6 or the graph is a sync ``def``
(calling a TestClient from ``async def`` deadlocks the loop). P6's ``create_app`` configures
its structlog globally; P1 configures its own logging AFTER constructing the P6 app.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from starlette.testclient import TestClient


@contextmanager
def build_embedded_client(db_path: Path) -> Iterator[TestClient]:
    from fhir_features.api.app import create_app
    from fhir_features.config import Settings as P6Settings

    db_path.parent.mkdir(parents=True, exist_ok=True)
    app = create_app(P6Settings(db_path=db_path))
    with TestClient(app, base_url="http://p6.embedded") as client:
        yield client
