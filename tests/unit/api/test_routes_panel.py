"""``/v1/panel``: P6's patient page (birth date / sex / deceased only) joined with the latest
patient run per patient, optionally at one ``as_of``."""

from datetime import date

from caregap.p6.client import P6ContractError, P6Unavailable
from tests.unit.api.conftest import (
    AS_OF,
    COMMITTED_PERSONAS,
    KAYCE,
    SHERYL,
    TONY,
    Api,
    ApiFactory,
    assert_problem,
    failing_p6,
)


def test_panel_lists_the_committed_personas_without_runs(api: Api) -> None:
    response = api.client.get("/v1/panel")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == len(COMMITTED_PERSONAS)
    assert (body["limit"], body["offset"], body["as_of"]) == (50, 0, None)
    assert [item["patient_id"] for item in body["items"]] == COMMITTED_PERSONAS
    for item in body["items"]:
        assert set(item) == {"patient_id", "sex", "birth_date", "deceased", "last_run"}
        assert item["last_run"] is None
    kayce = next(item for item in body["items"] if item["patient_id"] == KAYCE)
    assert (kayce["sex"], kayce["deceased"]) == ("female", True)
    assert kayce["birth_date"] == "1953-06-29"


def test_panel_pages_with_limit_and_offset(api: Api) -> None:
    body = api.client.get("/v1/panel", params={"limit": 2, "offset": 1}).json()
    assert body["total"] == len(COMMITTED_PERSONAS)
    assert [item["patient_id"] for item in body["items"]] == COMMITTED_PERSONAS[1:3]
    assert (body["limit"], body["offset"]) == (2, 1)


def test_panel_last_run_reflects_the_latest_patient_run(api: Api) -> None:
    run_id = api.run([KAYCE, TONY])
    body = api.client.get("/v1/panel", params={"as_of": AS_OF.isoformat()}).json()
    by_id = {item["patient_id"]: item["last_run"] for item in body["items"]}
    assert by_id[KAYCE] == {"run_id": run_id, "status": "no_action"}
    assert by_id[TONY] == {"run_id": run_id, "status": "awaiting_approval"}
    assert by_id[SHERYL] is None
    assert body["as_of"] == AS_OF.isoformat()

    second = api.run([KAYCE], as_of=date(2024, 12, 31))
    latest = api.client.get("/v1/panel").json()["items"]
    assert next(i for i in latest if i["patient_id"] == KAYCE)["last_run"]["run_id"] == second
    at_anchor = api.client.get("/v1/panel", params={"as_of": AS_OF.isoformat()}).json()["items"]
    assert next(i for i in at_anchor if i["patient_id"] == KAYCE)["last_run"]["run_id"] == run_id
    elsewhere = api.client.get("/v1/panel", params={"as_of": "2023-12-31"}).json()["items"]
    assert all(item["last_run"] is None for item in elsewhere)


def test_panel_rejects_bad_paging(api: Api) -> None:
    assert_problem(api.client.get("/v1/panel", params={"limit": 0}), 422, "request-invalid")
    assert_problem(api.client.get("/v1/panel", params={"limit": 1001}), 422, "request-invalid")
    assert_problem(api.client.get("/v1/panel", params={"offset": -1}), 422, "request-invalid")


def test_panel_maps_p6_failures(make_api: ApiFactory) -> None:
    unavailable = make_api(p6=failing_p6(P6Unavailable("/v1/patients: HTTP 503")))
    assert_problem(unavailable.client.get("/v1/panel"), 503, "p6-unavailable")
    broken = make_api(p6=failing_p6(P6ContractError("PatientPage failed validation (1)")))
    assert_problem(broken.client.get("/v1/panel"), 502, "p6-contract")
