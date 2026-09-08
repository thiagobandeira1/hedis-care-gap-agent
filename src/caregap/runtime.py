"""``Runtime`` — everything the API, CLI, UI and evals share, built once per process.

``build_runtime(settings)`` opens the P6 client for ``settings.p6_mode`` (embedded: the real
fhir-feature-service in-process, entered once and kept open until ``close()``; http: an
``httpx.Client`` against a running service; snapshot: committed responses), picks the model
bundle (``bundle_for(settings)`` — the ONLY path to a real key, and only in anthropic mode),
opens the ``RunStore`` ledger plus its outbox, the checkpointer (``SqliteSaver`` over
``settings.checkpoint_path`` unless one is injected), compiles the graph and wraps it in a
``PatientRunner``. ``close()`` releases all of it in reverse order and is idempotent.

Injection points (``models`` / ``checkpointer`` / ``p6``) exist for tests, the CLI and the
eval tiers; an injected object is used as-is and never closed here.
"""

import os
import sqlite3
import threading
from collections.abc import MutableMapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from caregap.config import Settings
from caregap.graph.build import GraphDeps, build_graph, checkpoint_serializer
from caregap.graph.outbox import RunStoreOutbox
from caregap.graph.runner import Graph, PatientRunner
from caregap.graph.runstore import RunStore
from caregap.llm import ModelBundle, bundle_for
from caregap.logging_setup import configure_logging
from caregap.measures.engine import default_engine
from caregap.measures.value_sets import load_value_sets
from caregap.p6.client import P6Client
from caregap.p6.embedded import build_embedded_client
from caregap.p6.http import HttpP6Client
from caregap.p6.snapshot import SnapshotP6Client
from caregap.structured import StructuredCaller

#: Timeout for the http P6 topology (P6 itself answers a record in well under a second).
HTTP_TIMEOUT_S = 30.0


class PanelRunStore(RunStore):
    """``RunStore`` plus the one read the panel needs and the base ledger does not expose:
    the latest patient run per patient. Reads the documented ``runs`` / ``patient_runs``
    tables under the store's own lock (same connection, so ``:memory:`` stores work too)."""

    def latest_patient_runs(
        self, patient_ids: Sequence[str], *, as_of: date | None = None
    ) -> dict[str, tuple[str, str]]:
        """``patient_id -> (run_id, status)`` of the most recent patient run (by run creation,
        then row update) for each listed patient; ``as_of`` restricts to runs at that anchor.
        Patients without a run are absent from the result."""
        ids = list(dict.fromkeys(patient_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))  # bind markers only; ids travel as params
        params: list[str] = list(ids)
        sql = (
            "SELECT pr.patient_id, pr.run_id, pr.status FROM patient_runs pr "  # noqa: S608
            "JOIN runs r ON r.run_id = pr.run_id "
            f"WHERE pr.patient_id IN ({placeholders})"
        )
        if as_of is not None:
            sql += " AND r.as_of = ?"
            params.append(as_of.isoformat())
        sql += " ORDER BY r.created_at, pr.updated_at, pr.rowid"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        latest: dict[str, tuple[str, str]] = {}
        for row in rows:  # ascending order: the last row per patient wins
            latest[row["patient_id"]] = (row["run_id"], row["status"])
        return latest


@dataclass
class Runtime:
    settings: Settings
    p6: P6Client
    deps: GraphDeps
    graph: Graph
    run_store: PanelRunStore
    runner: PatientRunner
    checkpointer: BaseCheckpointSaver[Any]
    cancel_flags: dict[str, threading.Event] = field(default_factory=dict)
    """``run_id -> cancel Event`` for every panel run this process launched."""
    _stack: ExitStack = field(default_factory=ExitStack, repr=False)

    def close(self) -> None:
        """Ask every launched panel loop to stop, then release what ``build_runtime`` opened
        (ledger, checkpointer connection, P6 client) in reverse order. Idempotent."""
        for flag in self.cancel_flags.values():
            flag.set()
        self._stack.close()


#: Opt-in switch for LangSmith / LangChain tracing. Without it, graph state (the full patient
#: record, drafts) is never shipped to a cloud tracing endpoint, whatever else the env says.
ALLOW_TRACING_ENV = "CAREGAP_ALLOW_TRACING"
TRACING_ENV_VARS: tuple[str, ...] = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")


def disable_tracing_unless_opted_in(environ: MutableMapping[str, str] = os.environ) -> bool:
    """Force LangSmith/LangChain tracing off unless ``CAREGAP_ALLOW_TRACING=1``.
    Returns True when tracing was forced off (SPEC section 1: cloud only in explicit runs)."""
    if environ.get(ALLOW_TRACING_ENV) == "1":
        return False
    for name in TRACING_ENV_VARS:
        environ[name] = "false"
    return True


def build_runtime(
    settings: Settings,
    *,
    models: ModelBundle | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    p6: P6Client | None = None,
) -> Runtime:
    stack = ExitStack()
    try:
        disable_tracing_unless_opted_in()
        client = p6 if p6 is not None else _open_p6(settings, stack)
        # P6's embedded app configures structlog globally; P1's allow-list wins by going last.
        configure_logging()
        bundle = models if models is not None else bundle_for(settings)
        saver = checkpointer if checkpointer is not None else _open_sqlite_saver(settings, stack)
        run_store = stack.enter_context(PanelRunStore(settings.runstore_path))
        deps = GraphDeps(
            p6=client,
            models=bundle,
            engine=default_engine(),
            run_store=run_store,
            outbox=RunStoreOutbox(run_store),
            structured=StructuredCaller(),
            value_sets=load_value_sets(),
            clinic_name=settings.clinic_name,
            clinic_phone=settings.clinic_phone,
        )
        graph = build_graph(deps, saver)
        runner = PatientRunner(graph, deps, timeout_s=settings.patient_timeout_s)
    except BaseException:
        stack.close()
        raise
    return Runtime(
        settings=settings,
        p6=client,
        deps=deps,
        graph=graph,
        run_store=run_store,
        runner=runner,
        checkpointer=saver,
        _stack=stack,
    )


def _open_p6(settings: Settings, stack: ExitStack) -> P6Client:
    if settings.p6_mode == "embedded":
        embedded = stack.enter_context(build_embedded_client(settings.p6_db_path))
        # Starlette's TestClient shares httpx's Client surface (``get(path, params=)``,
        # ``status_code``, ``json()``) but, since Starlette 1.x, subclasses ``httpx2.Client``;
        # ``HttpP6Client`` is annotated for ``httpx.Client`` (p6/http.py, fixed module).
        return HttpP6Client(embedded)  # type: ignore[arg-type]
    if settings.p6_mode == "http":
        http = stack.enter_context(httpx.Client(base_url=settings.p6_url, timeout=HTTP_TIMEOUT_S))
        return HttpP6Client(http)
    return SnapshotP6Client(settings.snapshot_dir)


def _open_sqlite_saver(settings: Settings, stack: ExitStack) -> SqliteSaver:
    """One connection shared by the API's request threads and the panel loop; ``SqliteSaver``
    serialises access with its own lock and switches the file to WAL on first use."""
    path = settings.checkpoint_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    stack.callback(conn.close)
    return SqliteSaver(conn, serde=checkpoint_serializer())
