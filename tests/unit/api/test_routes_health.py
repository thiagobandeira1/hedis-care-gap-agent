"""``/healthz`` plus the app-wide error rendering (problem+json for routing errors, request
validation, P6 failures and unhandled exceptions)."""

from caregap import __version__
from caregap.agents.prompts import PROMPT_VERSION
from caregap.measures.ids import ALL_MEASURES
from caregap.p6.client import P6ContractError, P6Unavailable
from tests.unit.api.conftest import Api, ApiFactory, assert_problem, failing_p6


def test_healthz_reports_snapshot_p6_and_fake_models(api: Api) -> None:
    response = api.client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service_version"] == __version__
    assert body["p6"] == {"service_version": "0.1.0", "schema_version": 3, "feature_version": "v1"}
    assert body["p6_mode"] == "snapshot"
    assert body["models_mode"] == "fake"
    assert body["prompt_version"] == PROMPT_VERSION
    assert body["engine_measures"] == list(ALL_MEASURES)


def test_healthz_is_503_when_p6_is_unavailable(make_api: ApiFactory) -> None:
    api = make_api(p6=failing_p6(P6Unavailable("/healthz: unavailable after 4 attempts")))
    body = assert_problem(api.client.get("/healthz"), 503, "p6-unavailable")
    assert "unavailable" in body["detail"]
    assert body["instance"] == "/healthz"


def test_healthz_is_502_on_a_p6_contract_breach(make_api: ApiFactory) -> None:
    api = make_api(p6=failing_p6(P6ContractError("ServiceInfo failed validation (2)")))
    assert_problem(api.client.get("/healthz"), 502, "p6-contract")


def test_unknown_route_and_wrong_method_are_problems(api: Api) -> None:
    body = assert_problem(api.client.get("/nope"), 404)
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    body = assert_problem(api.client.delete("/healthz"), 405)
    assert body["title"] == "Method Not Allowed"


def test_request_validation_is_a_422_problem_without_echoing_input(api: Api) -> None:
    response = api.client.get("/v1/panel", params={"limit": 0, "as_of": "not-a-date"})
    body = assert_problem(response, 422, "request-invalid")
    assert body["detail"] == "request validation failed"
    assert any(e.startswith("query.limit:") for e in body["errors"])
    assert any(e.startswith("query.as_of:") for e in body["errors"])
    assert "not-a-date" not in response.text


def test_unhandled_exception_is_a_500_problem_naming_the_class_only(
    make_api: ApiFactory,
) -> None:
    api = make_api(
        p6=failing_p6(RuntimeError("secret: patient text")), raise_server_exceptions=False
    )
    body = assert_problem(api.client.get("/healthz"), 500, "internal")
    assert body["detail"] == "RuntimeError"
    assert "secret" not in api.client.get("/healthz").text


def test_openapi_document_renders(api: Api) -> None:
    response = api.client.get("/openapi.json")
    assert response.status_code == 200
    paths = set(response.json()["paths"])
    assert {
        "/healthz",
        "/v1/measures",
        "/v1/panel",
        "/v1/patients/{patient_id}/gaps",
        "/v1/runs",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/cancel",
        "/v1/runs/{run_id}/patients/{patient_id}",
        "/v1/runs/{run_id}/patients/{patient_id}/decision",
        "/v1/approvals",
        "/v1/outbox",
    } == paths
