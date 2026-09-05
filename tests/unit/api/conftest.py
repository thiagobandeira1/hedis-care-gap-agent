"""API fixtures: a ``TestClient`` over ``create_app`` with an injected, keyless ``Runtime``
(snapshot P6, scripted fakes, ``MemorySaver``), plus the synthetic escalation personas the
committed snapshots lack (see ``tests/unit/graph/test_scenarios.py``).

Runs are executed synchronously by default (``app.state.sync_runs``): the POST still spawns
the real daemon thread, the route just joins it before answering 202.
"""

import json
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from caregap.api.app import create_app
from caregap.api.errors import PROBLEM_MEDIA_TYPE
from caregap.config import Settings
from caregap.graph.build import checkpoint_serializer
from caregap.llm import fake_bundle
from caregap.p6.client import ENGINE_SECTIONS, P6Client, P6Error
from caregap.p6.models import FeatureRow, FeatureSchema, PatientPage, PatientRecord, ServiceInfo
from caregap.p6.snapshot import SnapshotP6Client
from caregap.runtime import Runtime, build_runtime
from tests.factories import build_record, condition, feature_row, patient, procedure, write_snapshot

REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_DIR = REPO_ROOT / "synthetic" / "p6_snapshots"
AS_OF = date(2025, 12, 31)

TONY = "939eea26-a679-2564-5cf9-c0fd557beefc"
"""EED + COL open at 2025-12-31, no escalation: the plain draft path."""
MEREDITH = "1c1e0add-1be9-8194-109d-981ffd0adade"
"""BCS open."""
SHERYL = "ec26a105-cdda-e9a0-683d-1c7662a370ea"
"""No candidate."""
KAYCE = "009969ab-f1b8-a2c0-9fb7-f0621d7beea8"
"""Deceased 1954: not_eligible everywhere."""
COMMITTED_PERSONAS = sorted(
    json.loads((SNAPSHOT_DIR / "MANIFEST.json").read_text(encoding="utf-8"))["patient_ids"]
)

ESCALATED = "esc-cbp"
"""CBP gap_open promoted to needs_review by E6 (hypertension abated inside the MY)."""
OPEN_CBP = "open-cbp"
"""CBP gap_open, no escalation."""
GLOBAL_HOLD = "hold-e1"
"""CBP held by the global E1 flag (hospice 90 days before the MY)."""

CLINIC_NAME = "Demo Primary Care"
CLINIC_PHONE = "555-0100"
OPT_OUT = "Reply STOP to opt out of these messages."
HYPERTENSION = "59621000"
HOSPICE = "385763009"
BIRTH_1985 = date(1985, 6, 15)

GAP_NAMES: dict[str, str] = {
    "CBP": "a blood pressure check",
    "EED": "a diabetes eye exam",
    "BCS": "a breast cancer screening",
    "COL": "a colorectal cancer screening",
}

PROBLEM = PROBLEM_MEDIA_TYPE


class HttpResponse(Protocol):
    """What the assertions read from a ``TestClient`` response. Starlette's client answers
    with ``httpx2.Response`` (a transitive dependency, not one of ours), so the tests are
    typed against this slice instead of a package name."""

    @property
    def status_code(self) -> int: ...

    @property
    def text(self) -> str: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def json(self) -> Any: ...


# --- scripted model outputs -----------------------------------------------------------------


def plan_json(measure_ids: Sequence[str], *, note: str = "") -> str:
    """A drafter answer that passes lint by construction."""
    steps = " ".join(f"Please call us to book {GAP_NAMES[m]}." for m in measure_ids)
    message = (
        f"Hello, our records show some routine care is due. {steps} "
        f"Call {CLINIC_NAME} at {CLINIC_PHONE}. {OPT_OUT}"
    )
    return json.dumps(
        {
            "gaps": [
                {"measure_id": m, "urgency": "routine", "rationale": f"{m} is open this year."}
                for m in measure_ids
            ],
            "actions": [
                {
                    "measure_id": m,
                    "kind": "screening",
                    "detail": f"Schedule {GAP_NAMES[m]}",
                    "owner": "care_team",
                }
                for m in measure_ids
            ],
            "patient_message": message,
            "provider_note": note or "Open gaps per the packet; review at the next visit.",
        }
    )


def verdict_json(measure_id: str, decision: str, *, confidence: str = "high") -> str:
    return json.dumps(
        {
            "measure_id": measure_id,
            "decision": decision,
            "exclusion_category": None,
            "evidence_ids": [],
            "rule_citation": "cbp/numerator/window",
            "confidence": confidence,
            "rationale": "scripted verdict",
        }
    )


CONFIRM_CBP = verdict_json("CBP", "confirm_open")
NEEDS_HUMAN_CBP = verdict_json("CBP", "needs_human", confidence="low")
TONY_PLAN = plan_json(["COL", "EED"])
CBP_PLAN = plan_json(["CBP"])


# --- synthetic personas -----------------------------------------------------------------------


def synthetic_records() -> dict[str, PatientRecord]:
    def header(patient_id: str, sex: str) -> Any:
        return patient(patient_id=patient_id, birth_date=BIRTH_1985, sex=sex)

    return {
        ESCALATED: build_record(
            patient_header=header(ESCALATED, "male"),
            conditions=[
                condition(
                    HYPERTENSION,
                    onset=date(2020, 1, 1),
                    abatement=date(2025, 6, 1),
                    display="Hypertension",
                )
            ],
        ),
        OPEN_CBP: build_record(
            patient_header=header(OPEN_CBP, "female"),
            conditions=[condition(HYPERTENSION, onset=date(2020, 1, 1), display="Hypertension")],
        ),
        GLOBAL_HOLD: build_record(
            patient_header=header(GLOBAL_HOLD, "female"),
            conditions=[condition(HYPERTENSION, onset=date(2020, 1, 1), display="Hypertension")],
            procedures=[procedure(HOSPICE, performed=date(2024, 11, 15), display="Hospice")],
        ),
    }


@pytest.fixture(scope="session")
def synthetic_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    records = synthetic_records()
    features = {pid: feature_row(pid, AS_OF, has_hypertension=True) for pid in records}
    return write_snapshot(
        tmp_path_factory.mktemp("p6_snapshots"), as_of=AS_OF, records=records, features=features
    )


# --- P6 doubles -------------------------------------------------------------------------------


class FailingP6:
    """Every call raises ``error`` (P6 error mapping and the 500 handler)."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def healthz(self) -> ServiceInfo:
        raise self.error

    def features_schema(self) -> FeatureSchema:
        raise self.error

    def list_patients(self, *, limit: int, offset: int) -> PatientPage:
        raise self.error

    def get_record(
        self, patient_id: str, *, to: date, sections: Sequence[str] = ENGINE_SECTIONS
    ) -> PatientRecord:
        raise self.error

    def get_features(self, patient_id: str, *, as_of: date) -> FeatureRow:
        raise self.error


class GatedP6:
    """The committed snapshots, but every record load waits on ``gate`` first — so a panel
    run stays inside its first patient until the test says otherwise."""

    def __init__(self, gate: threading.Event, inner: P6Client | None = None) -> None:
        self.gate = gate
        self.inner: P6Client = inner or SnapshotP6Client(SNAPSHOT_DIR)

    def healthz(self) -> ServiceInfo:
        return self.inner.healthz()

    def features_schema(self) -> FeatureSchema:
        return self.inner.features_schema()

    def list_patients(self, *, limit: int, offset: int) -> PatientPage:
        return self.inner.list_patients(limit=limit, offset=offset)

    def get_record(
        self, patient_id: str, *, to: date, sections: Sequence[str] = ENGINE_SECTIONS
    ) -> PatientRecord:
        assert self.gate.wait(timeout=10), "gate never opened"
        return self.inner.get_record(patient_id, to=to, sections=sections)

    def get_features(self, patient_id: str, *, as_of: date) -> FeatureRow:
        return self.inner.get_features(patient_id, as_of=as_of)


# --- the app under test -----------------------------------------------------------------------


@dataclass
class Api:
    app: FastAPI
    client: TestClient
    runtime: Runtime
    settings: Settings

    def run(
        self,
        patient_ids: Sequence[str],
        *,
        as_of: date = AS_OF,
        options: dict[str, Any] | None = None,
    ) -> str:
        body: dict[str, Any] = {"as_of": as_of.isoformat(), "patient_ids": list(patient_ids)}
        if options is not None:
            body["options"] = options
        response = self.client.post("/v1/runs", json=body)
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        assert isinstance(run_id, str)
        return run_id

    def decide(
        self, run_id: str, patient_id: str, decision_id: str, action: str, **extra: Any
    ) -> HttpResponse:
        body = {"decision_id": decision_id, "action": action, "reviewer": "nurse", **extra}
        return self.client.post(f"/v1/runs/{run_id}/patients/{patient_id}/decision", json=body)

    def patient(self, run_id: str, patient_id: str) -> dict[str, Any]:
        response = self.client.get(f"/v1/runs/{run_id}/patients/{patient_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert isinstance(payload, dict)
        return payload


ApiFactory = Callable[..., Api]


def make_settings(tmp_path: Path, tag: str, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "p6_mode": "snapshot",
        "snapshot_dir": SNAPSHOT_DIR,
        "models": "fake",
        "checkpoint_path": tmp_path / f"ckpt-{tag}.sqlite",
        "runstore_path": tmp_path / f"rs-{tag}.sqlite",
        "clinic_name": CLINIC_NAME,
        "clinic_phone": CLINIC_PHONE,
    }
    base.update(overrides)
    # ``_env_file=None`` is pydantic-settings' documented switch for "ignore any .env"; it is
    # not a field, so it travels inside the kwargs mapping (mypy sees the synthesized
    # field-only ``__init__``).
    base["_env_file"] = None
    return Settings(**base)


@pytest.fixture
def make_api(tmp_path: Path) -> Iterator[ApiFactory]:
    opened: list[Api] = []

    def factory(
        *,
        validator: Sequence[str] = (),
        drafter: Sequence[str] = (),
        snapshot_dir: Path = SNAPSHOT_DIR,
        p6: P6Client | None = None,
        sync_runs: bool = True,
        raise_server_exceptions: bool = True,
        **overrides: Any,
    ) -> Api:
        settings = make_settings(
            tmp_path, f"api{len(opened)}", snapshot_dir=snapshot_dir, **overrides
        )
        runtime = build_runtime(
            settings,
            models=fake_bundle(validator, drafter),
            checkpointer=MemorySaver(serde=checkpoint_serializer()),
            p6=p6,
        )
        app = create_app(settings, runtime=runtime)
        app.state.sync_runs = sync_runs
        client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
        client.__enter__()
        api = Api(app=app, client=client, runtime=runtime, settings=settings)
        opened.append(api)
        return api

    yield factory
    for api in reversed(opened):
        api.client.__exit__(None, None, None)
        api.runtime.close()


@pytest.fixture
def api(make_api: ApiFactory) -> Api:
    """Committed snapshots; enough drafter script for Tony then Meredith."""
    return make_api(drafter=[TONY_PLAN, plan_json(["BCS"])])


def assert_problem(response: HttpResponse, status: int, code: str | None = None) -> dict[str, Any]:
    """RFC 9457: problem+json content type and the four core members; returns the body."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith(PROBLEM)
    body = response.json()
    assert isinstance(body, dict)
    assert {"type", "title", "status", "detail"} <= set(body)
    assert body["status"] == status
    assert isinstance(body["type"], str) and body["type"]
    assert isinstance(body["title"], str) and body["title"]
    assert isinstance(body["detail"], str)
    if code is not None:
        assert body["code"] == code
        assert body["type"].endswith(code)
    return body


def failing_p6(error: P6Error | Exception) -> P6Client:
    return FailingP6(error)
