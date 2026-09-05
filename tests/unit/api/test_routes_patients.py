"""``/v1/patients/{id}/gaps``: the engine alone over the masked record — no model, no ledger
write — with the P6 error taxonomy mapped to 404 / 502 / 503."""

from caregap.measures.ids import ALL_MEASURES
from caregap.p6.client import P6ContractError, P6Unavailable
from tests.unit.api.conftest import (
    AS_OF,
    KAYCE,
    TONY,
    Api,
    ApiFactory,
    assert_problem,
    failing_p6,
)


def test_tony_gaps_at_eval_anchor_match_the_goldens(api: Api) -> None:
    response = api.client.get(f"/v1/patients/{TONY}/gaps", params={"as_of": AS_OF.isoformat()})
    assert response.status_code == 200
    body = response.json()
    assert (body["patient_id"], body["as_of"]) == (TONY, AS_OF.isoformat())
    assert body["context"] == {
        "as_of": "2025-12-31",
        "my_start": "2025-01-01",
        "my_end": "2025-12-31",
        "age_at_my_end": body["context"]["age_at_my_end"],
    }
    assert isinstance(body["context"]["age_at_my_end"], int)
    verdicts = {e["measure_id"]: e["verdict"] for e in body["evaluations"]}
    assert list(verdicts) == list(ALL_MEASURES)
    assert verdicts["EED"] == "gap_open"
    assert verdicts["COL"] == "gap_open"
    assert verdicts["BCS"] == "not_eligible"
    open_gaps = [e for e in body["evaluations"] if e["verdict"] == "gap_open"]
    assert all(e["priority_score"] > 0 for e in open_gaps)
    assert all(e["escalations"] == [] for e in body["evaluations"])
    # Nothing was run: the ledger stays empty.
    assert api.runtime.run_store.list_approvals() == []


def test_deceased_persona_is_not_eligible_everywhere(api: Api) -> None:
    body = api.client.get(f"/v1/patients/{KAYCE}/gaps", params={"as_of": AS_OF.isoformat()}).json()
    assert {e["verdict"] for e in body["evaluations"]} == {"not_eligible"}


def test_unknown_patient_is_404(api: Api) -> None:
    response = api.client.get("/v1/patients/ghost/gaps", params={"as_of": AS_OF.isoformat()})
    body = assert_problem(response, 404, "patient-not-found")
    assert body["instance"] == "/v1/patients/ghost/gaps"


def test_missing_as_of_is_422(api: Api) -> None:
    body = assert_problem(api.client.get(f"/v1/patients/{TONY}/gaps"), 422, "request-invalid")
    assert body["errors"] == ["query.as_of: Field required"]


def test_p6_failures_map_to_503_and_502(make_api: ApiFactory) -> None:
    params = {"as_of": AS_OF.isoformat()}
    unavailable = make_api(p6=failing_p6(P6Unavailable("/v1/patients/x/record: HTTP 500")))
    assert_problem(unavailable.client.get(f"/v1/patients/{TONY}/gaps", params=params), 503)
    broken = make_api(p6=failing_p6(P6ContractError("record payload lacks a patient object")))
    body = assert_problem(broken.client.get(f"/v1/patients/{TONY}/gaps", params=params), 502)
    assert body["title"] == "Bad Gateway"
