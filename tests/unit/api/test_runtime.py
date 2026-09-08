"""``build_runtime`` / ``Runtime``: the pinned wiring per ``p6_mode``, injection points,
persistence across runtimes, resource release, and the panel's latest-run lookup."""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from caregap.api.app import create_app
from caregap.config import ConfigError
from caregap.graph.build import NODE_NAMES, checkpoint_serializer
from caregap.graph.hitl import pending_request
from caregap.graph.outbox import RunStoreOutbox
from caregap.graph.runner import PatientRunner
from caregap.graph.runstore import RunStore
from caregap.graph.state import ApprovalDecision, ApprovalRequest, RunOptions, RunOutcome, thread_id
from caregap.llm import fake_bundle
from caregap.measures.engine import MeasureEngine
from caregap.measures.ids import ALL_MEASURES
from caregap.measures.rule_text import load_rule_text
from caregap.measures.value_sets import load_value_sets
from caregap.p6.http import HttpP6Client
from caregap.p6.snapshot import SnapshotP6Client
from caregap.runtime import PanelRunStore, Runtime, build_runtime
from caregap.structured import StructuredCaller
from tests.unit.api.conftest import (
    AS_OF,
    KAYCE,
    SHERYL,
    SNAPSHOT_DIR,
    TONY,
    TONY_PLAN,
    GatedP6,
    make_settings,
)

P6_HEALTH = {"service_version": "0.1.0", "schema_version": 3, "feature_version": "v1"}


@pytest.fixture
def runtimes() -> Iterator[list[Runtime]]:
    opened: list[Runtime] = []
    yield opened
    for runtime in opened:
        runtime.close()


def test_snapshot_runtime_wires_the_pinned_contract(
    tmp_path: Path, runtimes: list[Runtime]
) -> None:
    settings = make_settings(tmp_path, "wired", checkpoint_path=tmp_path / "nested" / "ckpt.sqlite")
    runtime = build_runtime(settings)
    runtimes.append(runtime)

    assert runtime.settings is settings
    assert isinstance(runtime.p6, SnapshotP6Client)
    assert runtime.deps.p6 is runtime.p6
    # Defaults: bundle_for(settings) -> fake; SqliteSaver over checkpoint_path (parent created).
    assert runtime.deps.models.mode == "fake"
    assert runtime.deps.models.model_ids == {"validator_model": "fake", "drafter_model": "fake"}
    assert isinstance(runtime.checkpointer, SqliteSaver)
    assert (tmp_path / "nested" / "ckpt.sqlite").is_file()
    # Ledger + outbox share one store; engine, value sets, rule text and clinic strings as pinned.
    assert isinstance(runtime.run_store, RunStore)
    assert runtime.run_store.path == str(settings.runstore_path)
    assert runtime.deps.run_store is runtime.run_store
    assert isinstance(runtime.deps.outbox, RunStoreOutbox)
    assert isinstance(runtime.deps.engine, MeasureEngine)
    assert runtime.deps.engine.measure_ids == ALL_MEASURES
    assert runtime.deps.value_sets is load_value_sets()
    assert runtime.deps.rule_text_loader is load_rule_text
    assert isinstance(runtime.deps.structured, StructuredCaller)
    assert (runtime.deps.clinic_name, runtime.deps.clinic_phone) == (
        settings.clinic_name,
        settings.clinic_phone,
    )
    assert isinstance(runtime.runner, PatientRunner)
    assert runtime.cancel_flags == {}
    assert set(NODE_NAMES) <= set(runtime.graph.get_graph().nodes)

    # And it drives the real graph through the sqlite checkpointer.
    outcome = runtime.runner.start("r1", KAYCE, AS_OF, RunOptions())
    assert isinstance(outcome, RunOutcome)
    assert outcome.status == "no_action"
    row = runtime.run_store.get_patient_run("r1", KAYCE)
    assert row is not None and row.status == "no_action"


def test_checkpoints_and_ledger_persist_across_runtimes(
    tmp_path: Path, runtimes: list[Runtime]
) -> None:
    settings = make_settings(tmp_path, "persist")
    first = build_runtime(settings, models=fake_bundle([], [TONY_PLAN]))
    request = first.runner.start("r1", TONY, AS_OF, RunOptions())
    assert isinstance(request, ApprovalRequest)
    first.close()

    second = build_runtime(settings)
    runtimes.append(second)
    assert pending_request(second.graph, thread_id("r1", TONY)) == request
    row = second.run_store.get_patient_run("r1", TONY)
    assert row is not None and row.status == "awaiting_approval"
    outcome = second.runner.resume(
        "r1", TONY, ApprovalDecision(decision_id="d1", action="approve", reviewer="nurse")
    )
    assert isinstance(outcome, RunOutcome)
    assert (outcome.status, outcome.actionable) == ("completed", True)
    assert [e.action_id for e in second.run_store.list_outbox("r1")] == ["a1", "a2"]


def test_close_stops_loops_releases_resources_and_is_idempotent(tmp_path: Path) -> None:
    runtime = build_runtime(make_settings(tmp_path, "close"))
    flag = threading.Event()
    runtime.cancel_flags["run_x"] = flag
    assert isinstance(runtime.checkpointer, SqliteSaver)

    runtime.close()
    assert flag.is_set()
    with pytest.raises(sqlite3.ProgrammingError):
        runtime.run_store.get_run("r1")
    with pytest.raises(sqlite3.ProgrammingError):
        runtime.checkpointer.conn.execute("SELECT 1")
    runtime.close()  # second close is a no-op


def test_injected_models_checkpointer_and_p6_are_used_as_is(
    tmp_path: Path, runtimes: list[Runtime]
) -> None:
    bundle = fake_bundle([], [])
    saver = MemorySaver(serde=checkpoint_serializer())
    p6 = SnapshotP6Client(SNAPSHOT_DIR)
    settings = make_settings(tmp_path, "inject", p6_mode="http", p6_url="http://unused.invalid")
    runtime = build_runtime(settings, models=bundle, checkpointer=saver, p6=p6)
    runtimes.append(runtime)
    assert runtime.deps.models is bundle
    assert runtime.checkpointer is saver
    assert runtime.p6 is p6
    assert not settings.checkpoint_path.exists(), "no sqlite saver is opened when one is injected"
    runtime.close()
    assert p6.healthz().service_version, "an injected P6 client is never closed here"


def test_http_mode_wraps_an_httpx_client_at_p6_url(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "http", p6_mode="http", p6_url="http://p6.test")
    with respx.mock(base_url="http://p6.test", assert_all_called=False) as router:
        route = router.get("/healthz").mock(return_value=httpx.Response(200, json=P6_HEALTH))
        runtime = build_runtime(settings)
        assert isinstance(runtime.p6, HttpP6Client)
        assert runtime.p6.healthz().service_version == "0.1.0"
        assert route.call_count == 1
        assert str(route.calls.last.request.url) == "http://p6.test/healthz"
        runtime.close()
        with pytest.raises(RuntimeError):  # the httpx client was closed with the runtime
            runtime.p6.healthz()


def test_embedded_mode_enters_p6_once_and_exits_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, Path]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/healthz"
        return httpx.Response(200, json=P6_HEALTH)

    @contextmanager
    def fake_embedded(db_path: Path) -> Iterator[httpx.Client]:
        events.append(("enter", db_path))
        with httpx.Client(
            base_url="http://p6.embedded", transport=httpx.MockTransport(handler)
        ) as client:
            yield client
        events.append(("exit", db_path))

    monkeypatch.setattr("caregap.runtime.build_embedded_client", fake_embedded)
    settings = make_settings(
        tmp_path, "embedded", p6_mode="embedded", p6_db_path=tmp_path / "p6.duckdb"
    )
    runtime = build_runtime(settings)
    assert events == [("enter", settings.p6_db_path)]
    assert isinstance(runtime.p6, HttpP6Client)
    assert runtime.p6.healthz().feature_version == "v1"
    assert events == [("enter", settings.p6_db_path)], "entered once, kept open"
    runtime.close()
    assert events == [("enter", settings.p6_db_path), ("exit", settings.p6_db_path)]


def test_build_failure_releases_what_was_already_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []

    class SpyStore(PanelRunStore):
        def close(self) -> None:
            closed.append("store")
            super().close()

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("scripted graph build failure")

    monkeypatch.setattr("caregap.runtime.PanelRunStore", SpyStore)
    monkeypatch.setattr("caregap.runtime.build_graph", explode)
    with pytest.raises(RuntimeError, match="scripted"):
        build_runtime(make_settings(tmp_path, "fail"))
    assert closed == ["store"]


def test_anthropic_mode_without_a_key_fails_closed(tmp_path: Path) -> None:
    """Keyless by construction: the only path to a real model refuses without a key, and
    nothing here ever reads one."""
    with pytest.raises(ConfigError, match="CAREGAP_ANTHROPIC_API_KEY"):
        build_runtime(make_settings(tmp_path, "anthropic", models="anthropic"))


# --- PanelRunStore ------------------------------------------------------------------------------


class TickingClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def test_latest_patient_runs_picks_the_newest_run_per_patient(tmp_path: Path) -> None:
    with PanelRunStore(tmp_path / "rs.sqlite", clock=TickingClock()) as store:
        store.create_run("r1", AS_OF, RunOptions(), ["p1", "p2"])
        store.upsert_patient_run("r1", "p1", "no_action", None, None)
        store.upsert_patient_run("r1", "p2", "error", None, None)
        store.create_run("r2", date(2026, 6, 30), RunOptions(), ["p1", "p3"])
        store.upsert_patient_run("r2", "p1", "awaiting_approval", None, None)
        store.create_run("r3", AS_OF, RunOptions(), ["p2"], status="queued")  # not started

        assert store.latest_patient_runs(["p1", "p2", "p3", "p9"]) == {
            "p1": ("r2", "awaiting_approval"),
            "p2": ("r1", "error"),
        }
        assert store.latest_patient_runs(["p1", "p2"], as_of=AS_OF) == {
            "p1": ("r1", "no_action"),
            "p2": ("r1", "error"),
        }
        assert store.latest_patient_runs(["p1"], as_of=date(2024, 12, 31)) == {}
        assert store.latest_patient_runs([]) == {}
        assert store.latest_patient_runs(["p1", "p1"]) == {"p1": ("r2", "awaiting_approval")}


def test_latest_patient_runs_follows_status_updates(tmp_path: Path) -> None:
    with PanelRunStore(":memory:", clock=TickingClock()) as store:
        store.create_run("r1", AS_OF, RunOptions(), ["p1"])
        store.upsert_patient_run("r1", "p1", "running", None, None)
        assert store.latest_patient_runs(["p1"]) == {"p1": ("r1", "running")}
        store.upsert_patient_run("r1", "p1", "completed", None, None)
        assert store.latest_patient_runs(["p1"]) == {"p1": ("r1", "completed")}


# --- tracing (V19) ----------------------------------------------------------------------------


def test_build_runtime_forces_langsmith_tracing_off_unless_opted_in(
    tmp_path: Path, runtimes: list[Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env vars alone must never ship graph state (the patient record) to a cloud tracer."""
    from caregap.runtime import disable_tracing_unless_opted_in

    monkeypatch.delenv("CAREGAP_ALLOW_TRACING", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    runtimes.append(build_runtime(make_settings(tmp_path, "tracing")))
    import os

    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"

    env = {"CAREGAP_ALLOW_TRACING": "1", "LANGSMITH_TRACING": "true"}
    assert disable_tracing_unless_opted_in(env) is False
    assert env["LANGSMITH_TRACING"] == "true"


def test_app_shutdown_cancels_and_joins_the_run_thread(tmp_path: Path) -> None:
    """V11: leaving the lifespan sets the cancel flag and joins the panel loop while the
    ledger is still open, so the run ends ``cancelled`` instead of ``running`` forever."""
    gate = threading.Event()
    settings = make_settings(tmp_path, "join")
    runtime = build_runtime(
        settings,
        models=fake_bundle([], [TONY_PLAN]),
        checkpointer=MemorySaver(serde=checkpoint_serializer()),
        p6=GatedP6(gate),
    )
    try:
        app = create_app(settings, runtime=runtime)
        with TestClient(app) as client:
            response = client.post(
                "/v1/runs", json={"as_of": AS_OF.isoformat(), "patient_ids": [KAYCE, SHERYL]}
            )
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            thread = app.state.run_threads[run_id]
            assert thread.is_alive()
            threading.Timer(0.2, gate.set).start()  # the loop is inside patient 1 until then
        assert not thread.is_alive()
        assert runtime.cancel_flags[run_id].is_set()
        run = runtime.run_store.get_run(run_id)
        assert run is not None and run.status == "cancelled" and run.cursor == 1
        rows = runtime.run_store.list_patient_runs(run_id)
        assert [r.patient_id for r in rows] == [KAYCE]
        assert all(r.status != "running" for r in rows)
    finally:
        runtime.close()


def test_build_runtime_marks_runs_of_a_dead_process_interrupted(
    tmp_path: Path, runtimes: list[Runtime]
) -> None:
    """V18: a ledger left ``running`` by a process that never wrote its terminal status is
    repaired on open; the API then reports ``interrupted`` and cancel does not say cancelling."""
    settings = make_settings(tmp_path, "stale")
    earlier = datetime.now(UTC) - timedelta(seconds=5)
    with RunStore(settings.runstore_path, clock=lambda: earlier) as seed:
        seed.create_run("run_dead", AS_OF, RunOptions(), [TONY], status="running")
        seed.upsert_patient_run("run_dead", TONY, "running", None, None)
    runtime = build_runtime(settings, models=fake_bundle([], [TONY_PLAN]))
    runtimes.append(runtime)
    run = runtime.run_store.get_run("run_dead")
    assert run is not None and run.status == "interrupted"
    row = runtime.run_store.get_patient_run("run_dead", TONY)
    assert row is not None and row.status == "interrupted"
    with TestClient(create_app(settings, runtime=runtime)) as client:
        assert client.get("/v1/runs/run_dead").json()["run"]["status"] == "interrupted"
        assert client.post("/v1/runs/run_dead/cancel").json()["status"] == "interrupted"
