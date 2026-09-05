"""Panel runs: ``POST /v1/runs`` (202 + background loop), ``GET /v1/runs/{id}``, cancel, and
the per-patient view with the pending request and the checkpointed state."""

import re
import threading

from caregap.graph.build import NODE_NAMES
from caregap.measures.ids import ALL_MEASURES
from tests.unit.api.conftest import (
    AS_OF,
    KAYCE,
    MEREDITH,
    SHERYL,
    TONY,
    Api,
    ApiFactory,
    GatedP6,
    assert_problem,
)

RUN_ID = re.compile(r"^run_[0-9a-f]{12}$")


def test_post_run_is_202_and_executes_the_panel(api: Api) -> None:
    response = api.client.post(
        "/v1/runs", json={"as_of": AS_OF.isoformat(), "patient_ids": [KAYCE, TONY, SHERYL]}
    )
    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "queued"
    run_id = accepted["run_id"]
    assert RUN_ID.match(run_id)
    assert run_id in api.runtime.cancel_flags
    assert not api.runtime.cancel_flags[run_id].is_set()
    assert not api.app.state.run_threads[run_id].is_alive()

    body = api.client.get(f"/v1/runs/{run_id}").json()
    run = body["run"]
    assert (run["run_id"], run["as_of"], run["status"]) == (run_id, AS_OF.isoformat(), "completed")
    assert run["patient_ids"] == [KAYCE, TONY, SHERYL]
    assert run["cursor"] == 3
    assert run["options"]["approval_mode"] == "interrupt"
    assert run["options"]["validation_mode"] == "escalated"
    statuses = {p["patient_id"]: p["status"] for p in body["patients"]}
    assert statuses == {KAYCE: "no_action", TONY: "awaiting_approval", SHERYL: "no_action"}
    assert [p["patient_id"] for p in body["patients"]] == sorted(statuses)


def test_run_options_are_honoured(api: Api) -> None:
    run_id = api.run([TONY], options={"approval_mode": "auto", "validation_mode": "off"})
    body = api.client.get(f"/v1/runs/{run_id}").json()
    assert body["run"]["options"]["approval_mode"] == "auto"
    (tony,) = body["patients"]
    assert tony["status"] == "completed"
    assert tony["outcome"]["decision_action"] == "auto_reviewed"
    assert tony["outcome"]["actionable"] is False
    assert api.client.get("/v1/approvals", params={"status": "pending"}).json() == []


def test_run_ids_are_unique(api: Api) -> None:
    ids = {api.run([KAYCE]) for _ in range(3)}
    assert len(ids) == 3


def test_post_run_validation(api: Api) -> None:
    base = {"as_of": AS_OF.isoformat()}
    assert_problem(
        api.client.post("/v1/runs", json={**base, "patient_ids": []}), 422, "request-invalid"
    )
    assert_problem(
        api.client.post("/v1/runs", json={"patient_ids": [KAYCE]}), 422, "request-invalid"
    )
    body = assert_problem(
        api.client.post("/v1/runs", json={**base, "patient_ids": [KAYCE, TONY, KAYCE]}),
        422,
        "run-duplicate-patients",
    )
    assert KAYCE in body["detail"]
    bad_options = {**base, "patient_ids": [KAYCE], "options": {"approval_mode": "yolo"}}
    assert_problem(api.client.post("/v1/runs", json=bad_options), 422, "request-invalid")


def test_post_run_enforces_the_patient_cap(make_api: ApiFactory) -> None:
    api = make_api(max_patients_per_run=2)
    body = assert_problem(
        api.client.post(
            "/v1/runs", json={"as_of": AS_OF.isoformat(), "patient_ids": [KAYCE, TONY, SHERYL]}
        ),
        422,
        "run-too-large",
    )
    assert "cap is 2" in body["detail"]
    assert api.runtime.cancel_flags == {}


def test_unknown_run_is_404(api: Api) -> None:
    assert_problem(api.client.get("/v1/runs/run_000000000000"), 404, "run-not-found")
    assert_problem(api.client.post("/v1/runs/run_000000000000/cancel"), 404, "run-not-found")
    assert_problem(
        api.client.get(f"/v1/runs/run_000000000000/patients/{TONY}"), 404, "run-not-found"
    )


def test_patient_view_carries_pending_request_and_state(api: Api) -> None:
    run_id = api.run([TONY])
    body = api.patient(run_id, TONY)
    assert body["patient_run"]["status"] == "awaiting_approval"
    assert body["patient_run"]["outcome"] is None
    pending = body["pending"]
    assert pending is not None
    assert (pending["run_id"], pending["patient_id"], pending["as_of"]) == (
        run_id,
        TONY,
        AS_OF.isoformat(),
    )
    assert [g["measure_id"] for g in pending["open_gaps"]] == ["COL", "EED"]
    assert pending["plan"] is not None
    assert pending["review_items"] == []
    assert pending["revision_count"] == 0
    assert body["patient_run"]["pending"] == pending

    state = body["state"]
    assert state is not None
    assert set(state) == {
        "evaluations",
        "verdicts",
        "open_gaps",
        "review_items",
        "plan",
        "draft_error",
        "revision_count",
        "trace",
        "agent_errors",
        "context",
        "load_error",
        "decisions",
    }
    assert [e["measure_id"] for e in state["evaluations"]] == list(ALL_MEASURES)
    assert [g["measure_id"] for g in state["open_gaps"]] == ["COL", "EED"]
    assert state["plan"] == pending["plan"]
    assert (state["verdicts"], state["review_items"], state["agent_errors"]) == ([], [], [])
    assert (state["draft_error"], state["revision_count"], state["load_error"]) == (None, 0, None)
    assert state["decisions"] == []
    assert state["context"]["my_start"] == "2025-01-01"
    assert [t["node"] for t in state["trace"]] == [
        "load_record",
        "evaluate_measures",
        "draft_actions",
    ]
    assert {t["node"] for t in state["trace"]} <= set(NODE_NAMES)


def test_patient_view_after_completion_has_no_pending_but_keeps_state(api: Api) -> None:
    run_id = api.run([KAYCE])
    body = api.patient(run_id, KAYCE)
    assert body["patient_run"]["status"] == "no_action"
    assert body["patient_run"]["outcome"]["status"] == "no_action"
    assert body["pending"] is None
    assert [t["node"] for t in body["state"]["trace"]] == [
        "load_record",
        "evaluate_measures",
        "finalize",
    ]


def test_patient_not_in_run_is_404(api: Api) -> None:
    run_id = api.run([KAYCE])
    assert_problem(
        api.client.get(f"/v1/runs/{run_id}/patients/{MEREDITH}"), 404, "patient-run-not-found"
    )
    assert_problem(api.decide(run_id, MEREDITH, "d1", "approve"), 404, "patient-run-not-found")


def test_queued_run_is_visible_before_it_starts_and_cancel_stops_the_loop(
    make_api: ApiFactory,
) -> None:
    gate = threading.Event()
    api = make_api(p6=GatedP6(gate), sync_runs=False)
    run_id = api.run([KAYCE, SHERYL, TONY])

    # No 404 window: the run row exists before the thread starts.
    early = api.client.get(f"/v1/runs/{run_id}").json()
    assert early["run"]["status"] in {"queued", "running"}
    # A patient the loop has not reached is "not started", not "unknown".
    assert_problem(
        api.client.get(f"/v1/runs/{run_id}/patients/{SHERYL}"), 404, "patient-run-not-started"
    )

    cancel = api.client.post(f"/v1/runs/{run_id}/cancel")
    assert cancel.status_code == 202
    assert cancel.json() == {"run_id": run_id, "status": "cancelling"}
    assert api.runtime.cancel_flags[run_id].is_set()

    gate.set()
    api.app.state.run_threads[run_id].join(timeout=30)
    body = api.client.get(f"/v1/runs/{run_id}").json()
    assert body["run"]["status"] == "cancelled"
    assert body["run"]["cursor"] == 1
    assert [p["patient_id"] for p in body["patients"]] == [KAYCE]
    # Cancelling a finished run reports its terminal status.
    assert api.client.post(f"/v1/runs/{run_id}/cancel").json()["status"] == "cancelled"


def test_cancel_of_a_completed_run_reports_completed(api: Api) -> None:
    run_id = api.run([KAYCE])
    response = api.client.post(f"/v1/runs/{run_id}/cancel")
    assert response.status_code == 202
    assert response.json() == {"run_id": run_id, "status": "completed"}
    assert api.runtime.cancel_flags[run_id].is_set()
