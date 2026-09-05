"""``ApiClient`` over respx: every route's path, params, body and typed return, plus the
problem+json -> ``ApiError`` mapping (422 error lists, 409, 404, transport failures, bad
bodies)."""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from caregap.graph.state import ApprovalDecision, ApprovalRequest, RunOptions, RunOutcome
from caregap.ui.client import (
    ApiClient,
    ApiError,
    DecisionResult,
    new_decision_id,
    problem_from_response,
)
from tests.unit.ui import canned

BASE = "http://api.test"
PROBLEM = "application/problem+json"
DECISION = ApprovalDecision(decision_id="d1", action="approve", reviewer="dr")


def dump(model: Any) -> Any:
    if isinstance(model, list):
        return [item.model_dump(mode="json") for item in model]
    return model.model_dump(mode="json")


def problem(status: int, title: str, detail: Any = "", **extra: Any) -> httpx.Response:
    body = {"type": "about:blank", "title": title, "status": status, "detail": detail, **extra}
    return httpx.Response(status, json=body, headers={"content-type": PROBLEM})


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.MockRouter(base_url=BASE, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def api(router: respx.MockRouter) -> Iterator[ApiClient]:
    with ApiClient(BASE, timeout=1.0) as client:
        yield client


# --- routes ---------------------------------------------------------------------------------


def test_healthz(router: respx.MockRouter, api: ApiClient) -> None:
    router.get("/healthz").respond(200, json=dump(canned.HEALTH))
    health = api.healthz()
    assert health == canned.HEALTH
    assert health.p6.service_version == "0.1.0"
    assert health.engine_measures[0] == "CBP"


def test_healthz_ignores_unknown_fields(router: respx.MockRouter, api: ApiClient) -> None:
    router.get("/healthz").respond(200, json={"status": "ok", "extra": {"x": 1}})
    health = api.healthz()
    assert health.status == "ok"
    assert health.p6.service_version == ""


def test_list_measures(router: respx.MockRouter, api: ApiClient) -> None:
    router.get("/v1/measures").respond(200, json=dump(canned.MEASURES))
    measures = api.list_measures()
    assert measures == canned.MEASURES
    assert measures[0].element_counts.quoted == 3


def test_panel_params(router: respx.MockRouter, api: ApiClient) -> None:
    route = router.get("/v1/panel").respond(200, json=dump(canned.PANEL))
    page = api.panel(canned.AS_OF, limit=10, offset=5)
    assert page == canned.PANEL
    params = route.calls.last.request.url.params
    assert params["as_of"] == "2026-06-30"
    assert params["limit"] == "10"
    assert params["offset"] == "5"


def test_panel_all_follows_total(router: respx.MockRouter, api: ApiClient) -> None:
    items = dump(canned.PANEL.items)

    def page(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"items": items[offset : offset + 1], "total": 2})

    route = router.get("/v1/panel").mock(side_effect=page)
    rows = api.panel_all(canned.AS_OF, page_size=1)
    assert [row.patient_id for row in rows] == ["p1", "p2"]
    assert route.call_count == 2


def test_panel_all_stops_on_empty_page(router: respx.MockRouter, api: ApiClient) -> None:
    route = router.get("/v1/panel").respond(200, json={"items": [], "total": 5})
    assert api.panel_all(canned.AS_OF) == []
    assert route.call_count == 1


def test_patient_gaps(router: respx.MockRouter, api: ApiClient) -> None:
    route = router.get("/v1/patients/p1/gaps").respond(200, json=dump(canned.GAPS))
    gaps = api.patient_gaps("p1", canned.AS_OF)
    assert gaps == canned.GAPS
    assert [e.measure_id for e in gaps.evaluations] == list(canned.VERDICTS)
    assert route.calls.last.request.url.params["as_of"] == "2026-06-30"


def test_patient_id_is_url_quoted(router: respx.MockRouter, api: ApiClient) -> None:
    route = router.get("/v1/patients/p%201/gaps").respond(200, json=dump(canned.GAPS))
    api.patient_gaps("p 1", canned.AS_OF)
    assert route.called


def test_start_run_body(router: respx.MockRouter, api: ApiClient) -> None:
    route = router.post("/v1/runs").respond(202, json=dump(canned.RUN_CREATED))
    created = api.start_run(canned.AS_OF, ["p1", "p2"], RunOptions(approval_mode="auto"))
    assert created == canned.RUN_CREATED
    body = json.loads(route.calls.last.request.content)
    assert body["as_of"] == "2026-06-30"
    assert body["patient_ids"] == ["p1", "p2"]
    assert body["options"]["approval_mode"] == "auto"
    assert body["options"]["validation_mode"] == "escalated"


def test_start_run_default_options(router: respx.MockRouter, api: ApiClient) -> None:
    route = router.post("/v1/runs").respond(202, json=dump(canned.RUN_CREATED))
    api.start_run(canned.AS_OF, ["p1"])
    body = json.loads(route.calls.last.request.content)
    assert body["options"] == RunOptions().model_dump(mode="json")


def test_get_run(router: respx.MockRouter, api: ApiClient) -> None:
    router.get(f"/v1/runs/{canned.RUN_ID}").respond(200, json=dump(canned.RUN_DETAIL))
    detail = api.get_run(canned.RUN_ID)
    assert detail == canned.RUN_DETAIL
    assert detail.patients[0].pending is not None


def test_cancel_run(router: respx.MockRouter, api: ApiClient) -> None:
    route = router.post(f"/v1/runs/{canned.RUN_ID}/cancel").respond(
        202, json={"run_id": canned.RUN_ID, "status": "cancelling"}
    )
    cancelled = api.cancel_run(canned.RUN_ID)
    assert cancelled.status == "cancelling"
    assert route.called


def test_patient_run(router: respx.MockRouter, api: ApiClient) -> None:
    router.get(f"/v1/runs/{canned.RUN_ID}/patients/p1").respond(
        200, json=dump(canned.PATIENT_RUN_DETAIL)
    )
    detail = api.patient_run(canned.RUN_ID, "p1")
    assert detail == canned.PATIENT_RUN_DETAIL
    assert detail.state is not None
    assert detail.state.verdicts[0].decision == "needs_human"


def test_patient_run_without_state(router: respx.MockRouter, api: ApiClient) -> None:
    payload = {"patient_run": dump(canned.PATIENT_RUN), "pending": None, "state": None}
    router.get(f"/v1/runs/{canned.RUN_ID}/patients/p1").respond(200, json=payload)
    detail = api.patient_run(canned.RUN_ID, "p1")
    assert detail.pending is None
    assert detail.state is None


def test_decide_outcome(router: respx.MockRouter, api: ApiClient) -> None:
    route = router.post(f"/v1/runs/{canned.RUN_ID}/patients/p1/decision").respond(
        200, json=dump(canned.DECISION_RESULT)
    )
    result = api.decide(canned.RUN_ID, "p1", DECISION)
    assert isinstance(result.result, RunOutcome)
    assert result.result.approved_actions == ["a1", "a2"]
    assert result.replayed is False
    body = json.loads(route.calls.last.request.content)
    assert body["decision_id"] == "d1"
    assert body["action"] == "approve"
    assert body["reviewer"] == "dr"


def test_decide_request_result_and_replay(router: respx.MockRouter, api: ApiClient) -> None:
    payload = {"result": dump(canned.REQUEST), "replayed": True}
    router.post(f"/v1/runs/{canned.RUN_ID}/patients/p1/decision").respond(200, json=payload)
    result = api.decide(canned.RUN_ID, "p1", DECISION)
    assert isinstance(result.result, ApprovalRequest)
    assert result.result == canned.REQUEST
    assert result.replayed is True


def test_decision_result_union_is_unambiguous() -> None:
    outcome = DecisionResult.model_validate({"result": dump(canned.OUTCOME)})
    request = DecisionResult.model_validate({"result": dump(canned.REQUEST)})
    assert isinstance(outcome.result, RunOutcome)
    assert isinstance(request.result, ApprovalRequest)


def test_approvals_status_param(router: respx.MockRouter, api: ApiClient) -> None:
    route = router.get("/v1/approvals").respond(200, json=dump(canned.APPROVALS))
    records = api.approvals("pending")
    assert records == canned.APPROVALS
    assert route.calls.last.request.url.params["status"] == "pending"
    api.approvals()
    assert "status" not in route.calls.last.request.url.params


def test_outbox_run_id_param(router: respx.MockRouter, api: ApiClient) -> None:
    route = router.get("/v1/outbox").respond(200, json=dump(canned.OUTBOX))
    entries = api.outbox(canned.RUN_ID)
    assert entries == canned.OUTBOX
    assert route.calls.last.request.url.params["run_id"] == canned.RUN_ID
    api.outbox()
    assert "run_id" not in route.calls.last.request.url.params


# --- errors ---------------------------------------------------------------------------------


def test_404_problem(router: respx.MockRouter, api: ApiClient) -> None:
    router.get("/v1/patients/nope/gaps").mock(
        return_value=problem(404, "Not Found", "unknown patient nope")
    )
    with pytest.raises(ApiError) as excinfo:
        api.patient_gaps("nope", canned.AS_OF)
    err = excinfo.value
    assert (err.status, err.title, err.detail) == (404, "Not Found", "unknown patient nope")
    assert err.errors == []
    assert not err.unreachable
    assert str(err) == "HTTP 404: Not Found - unknown patient nope"


def test_422_error_list(router: respx.MockRouter, api: ApiClient) -> None:
    router.post(f"/v1/runs/{canned.RUN_ID}/patients/p1/decision").mock(
        return_value=problem(
            422,
            "Unprocessable Entity",
            "decision rejected",
            errors=["revise requires feedback", "duplicate resolution for CBP"],
        )
    )
    with pytest.raises(ApiError) as excinfo:
        api.decide(canned.RUN_ID, "p1", DECISION)
    assert excinfo.value.status == 422
    assert excinfo.value.errors == ["revise requires feedback", "duplicate resolution for CBP"]


def test_422_detail_list_and_fastapi_shapes(router: respx.MockRouter, api: ApiClient) -> None:
    router.post("/v1/runs").mock(
        return_value=problem(
            422,
            "Unprocessable Entity",
            [{"loc": ["body", "patient_ids"], "msg": "too short"}, "plain"],
        )
    )
    with pytest.raises(ApiError) as excinfo:
        api.start_run(canned.AS_OF, [])
    assert excinfo.value.errors == ["body.patient_ids: too short", "plain"]
    assert excinfo.value.detail == "body.patient_ids: too short; plain"


def test_409_problem(router: respx.MockRouter, api: ApiClient) -> None:
    router.post(f"/v1/runs/{canned.RUN_ID}/patients/p1/decision").mock(
        return_value=problem(409, "Conflict", "not awaiting approval")
    )
    with pytest.raises(ApiError) as excinfo:
        api.decide(canned.RUN_ID, "p1", DECISION)
    assert excinfo.value.status == 409
    assert excinfo.value.detail == "not awaiting approval"


def test_non_problem_error_body(router: respx.MockRouter, api: ApiClient) -> None:
    router.get("/healthz").respond(500, text="<html>boom</html>")
    with pytest.raises(ApiError) as excinfo:
        api.healthz()
    assert excinfo.value.status == 500
    assert excinfo.value.title == "Internal Server Error"
    assert "boom" in excinfo.value.detail


def test_transport_error(router: respx.MockRouter, api: ApiClient) -> None:
    router.get("/healthz").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ApiError) as excinfo:
        api.healthz()
    assert excinfo.value.status == 0
    assert excinfo.value.unreachable
    assert excinfo.value.title == "API unreachable"
    assert "ConnectError" in excinfo.value.detail
    assert str(excinfo.value).startswith("API unreachable")


def test_non_json_success_body(router: respx.MockRouter, api: ApiClient) -> None:
    router.get("/healthz").respond(200, text="not json")
    with pytest.raises(ApiError) as excinfo:
        api.healthz()
    assert excinfo.value.title == "Invalid response"


def test_schema_mismatch(router: respx.MockRouter, api: ApiClient) -> None:
    router.get("/v1/panel").respond(200, json={"items": [{"sex": "female"}], "total": 1})
    with pytest.raises(ApiError) as excinfo:
        api.panel(canned.AS_OF)
    assert excinfo.value.title == "Invalid response"
    assert "PanelPage" in excinfo.value.detail


def test_list_route_expects_array(router: respx.MockRouter, api: ApiClient) -> None:
    router.get("/v1/outbox").respond(200, json={"items": []})
    with pytest.raises(ApiError) as excinfo:
        api.outbox()
    assert "expected a JSON array" in excinfo.value.detail


def test_problem_from_response_uses_body_status_and_type() -> None:
    response = httpx.Response(
        400,
        json={"type": "https://example/err", "title": "Bad", "status": 422, "detail": None},
        headers={"content-type": PROBLEM},
    )
    err = problem_from_response(response)
    assert (err.status, err.title, err.detail, err.problem_type) == (
        422,
        "Bad",
        "",
        "https://example/err",
    )


# --- helpers --------------------------------------------------------------------------------


def test_new_decision_id_is_uuid4() -> None:
    first, second = new_decision_id(), new_decision_id()
    assert uuid.UUID(first).version == 4
    assert first != second


def test_owned_client_closes_and_injected_does_not() -> None:
    injected = httpx.Client(base_url=BASE)
    try:
        with ApiClient(BASE, client=injected):
            pass
        assert not injected.is_closed
    finally:
        injected.close()
    owned = ApiClient(BASE + "/")
    assert owned.base_url == BASE
    owned.close()
    assert owned._client.is_closed
