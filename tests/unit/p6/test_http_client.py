"""``HttpP6Client`` over respx: error mapping, retry policy, query params, schema guard."""

from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import pytest
import respx

from caregap.p6.client import (
    ENGINE_SECTIONS,
    P6ContractError,
    P6Unavailable,
    PatientNotFound,
)
from caregap.p6.http import HttpP6Client
from tests.factories import (
    EVAL_AS_OF,
    build_record,
    condition,
    feature_row,
    p6_payload,
    patient,
)

BASE = "http://p6.test"
RECORD_PATH = "/v1/patients/p1/record"
FEATURES_PATH = "/v1/patients/p1/features"
PROBLEM = "application/problem+json"


def problem(status: int, code: str) -> httpx.Response:
    body = {"type": "about:blank", "title": "x", "status": status, "detail": "y", "code": code}
    return httpx.Response(status, json=body, headers={"content-type": PROBLEM})


def record_body(**patient_kwargs: Any) -> dict[str, Any]:
    return p6_payload(
        build_record(
            patient_header=patient(**patient_kwargs),
            conditions=[condition("59621000", onset=date(2018, 1, 1))],
        )
    )


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def sleeper() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.MockRouter(base_url=BASE, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def p6(router: respx.MockRouter, sleeper: SleepRecorder) -> Iterator[HttpP6Client]:
    with httpx.Client(base_url=BASE) as http:
        yield HttpP6Client(http, sleep=sleeper)


# --- 4xx: mapped, never retried ---------------------------------------------------------


def test_404_maps_to_patient_not_found_without_retry(
    router: respx.MockRouter, p6: HttpP6Client, sleeper: SleepRecorder
) -> None:
    route = router.get(RECORD_PATH).mock(return_value=problem(404, "not_found"))
    with pytest.raises(PatientNotFound):
        p6.get_record("p1", to=EVAL_AS_OF)
    assert route.call_count == 1
    assert sleeper.delays == []


def test_422_problem_json_maps_to_contract_error_with_code(
    router: respx.MockRouter, p6: HttpP6Client, sleeper: SleepRecorder
) -> None:
    route = router.get(RECORD_PATH).mock(return_value=problem(422, "request_invalid"))
    with pytest.raises(P6ContractError, match=r"HTTP 422 \(request_invalid\)"):
        p6.get_record("p1", to=EVAL_AS_OF)
    assert route.call_count == 1
    assert sleeper.delays == []


def test_409_is_also_a_contract_error_and_a_bare_4xx_reports_unknown_code(
    router: respx.MockRouter, p6: HttpP6Client
) -> None:
    router.get(RECORD_PATH).mock(return_value=problem(409, "ambiguous_patient"))
    with pytest.raises(P6ContractError, match="ambiguous_patient"):
        p6.get_record("p1", to=EVAL_AS_OF)
    router.get("/healthz").mock(return_value=httpx.Response(400, text="nope"))
    with pytest.raises(P6ContractError, match=r"HTTP 400 \(unknown\)"):
        p6.healthz()


# --- 5xx / transport: retried with fixed backoff ----------------------------------------


def test_500_four_times_gives_unavailable_after_exactly_four_attempts(
    router: respx.MockRouter, p6: HttpP6Client, sleeper: SleepRecorder
) -> None:
    route = router.get(RECORD_PATH).mock(return_value=httpx.Response(500))
    with pytest.raises(P6Unavailable, match="unavailable after 4 attempts") as info:
        p6.get_record("p1", to=EVAL_AS_OF)
    assert route.call_count == 4
    assert sleeper.delays == [0.2, 0.6, 1.8]
    assert isinstance(info.value.__cause__, P6Unavailable)
    assert "HTTP 500" in str(info.value.__cause__)


def test_500_then_200_succeeds_after_one_sleep(
    router: respx.MockRouter, p6: HttpP6Client, sleeper: SleepRecorder
) -> None:
    route = router.get(RECORD_PATH).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=record_body())]
    )
    record = p6.get_record("p1", to=EVAL_AS_OF)
    assert record.patient.patient_id == "p1"
    assert route.call_count == 2
    assert sleeper.delays == [0.2]


def test_transport_error_is_retried_then_succeeds(
    router: respx.MockRouter, p6: HttpP6Client, sleeper: SleepRecorder
) -> None:
    route = router.get(RECORD_PATH).mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("slow"),
            httpx.Response(200, json=record_body()),
        ]
    )
    record = p6.get_record("p1", to=EVAL_AS_OF)
    assert [c.code for c in record.conditions] == ["59621000"]
    assert route.call_count == 3
    assert sleeper.delays == [0.2, 0.6]


def test_persistent_transport_error_gives_unavailable_with_cause(
    router: respx.MockRouter, p6: HttpP6Client, sleeper: SleepRecorder
) -> None:
    route = router.get(RECORD_PATH).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(P6Unavailable) as info:
        p6.get_record("p1", to=EVAL_AS_OF)
    assert route.call_count == 4
    assert sleeper.delays == [0.2, 0.6, 1.8]
    assert isinstance(info.value.__cause__, httpx.ConnectError)


# --- request shape ------------------------------------------------------------------------


def test_get_record_sends_to_and_sections(router: respx.MockRouter, p6: HttpP6Client) -> None:
    route = router.get(RECORD_PATH).mock(return_value=httpx.Response(200, json=record_body()))
    p6.get_record("p1", to=EVAL_AS_OF)
    params = route.calls.last.request.url.params
    assert params["to"] == "2025-12-31"
    assert params["sections"] == "conditions,observations,procedures,medications,encounters"
    assert params["sections"] == ",".join(ENGINE_SECTIONS)
    assert "observation_codes" not in params
    assert "from" not in params


def test_get_record_sections_override(router: respx.MockRouter, p6: HttpP6Client) -> None:
    route = router.get(RECORD_PATH).mock(return_value=httpx.Response(200, json=record_body()))
    p6.get_record("p1", to=EVAL_AS_OF, sections=("conditions", "encounters"))
    assert route.calls.last.request.url.params["sections"] == "conditions,encounters"


def test_get_features_sends_as_of(router: respx.MockRouter, p6: HttpP6Client) -> None:
    body = {
        "feature_version": "2026.08",
        "valuesets_version": "2026.08",
        "features": feature_row("p1", EVAL_AS_OF, latest_sbp=131.0),
    }
    route = router.get(FEATURES_PATH).mock(return_value=httpx.Response(200, json=body))
    row = p6.get_features("p1", as_of=EVAL_AS_OF)
    assert route.calls.last.request.url.params["as_of"] == "2025-12-31"
    assert (row.patient_id, row.as_of, row.latest_sbp) == ("p1", EVAL_AS_OF, 131.0)


def test_list_patients_sends_limit_and_offset(router: respx.MockRouter, p6: HttpP6Client) -> None:
    body = {
        "items": [
            {
                "source": "synthea",
                "patient_id": "p1",
                "birth_date": "1960-06-15",
                "sex": "female",
                "deceased": False,
                "last_ingested_at": "2026-08-01T00:00:00",
            }
        ],
        "total": 7,
    }
    route = router.get("/v1/patients").mock(return_value=httpx.Response(200, json=body))
    page = p6.list_patients(limit=5, offset=10)
    params = route.calls.last.request.url.params
    assert (params["limit"], params["offset"]) == ("5", "10")
    assert (page.total, page.items[0].patient_id) == (7, "p1")


def test_healthz_and_features_schema(router: respx.MockRouter, p6: HttpP6Client) -> None:
    router.get("/healthz").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "service_version": "0.1.0",
                "schema_version": 3,
                "feature_version": "2026.08",
            },
        )
    )
    info = p6.healthz()
    assert (info.service_version, info.schema_version, info.feature_version) == (
        "0.1.0",
        3,
        "2026.08",
    )
    router.get("/v1/features/schema").mock(
        return_value=httpx.Response(
            200,
            json={
                "feature_version": "2026.08",
                "valuesets_version": "2026.08",
                "as_of_semantics": "inclusive",
                "features": [],
            },
        )
    )
    schema = p6.features_schema()
    assert (schema.feature_version, schema.valuesets_version) == ("2026.08", "2026.08")


# --- response shape guard -----------------------------------------------------------------


def test_schema_invalid_record_body_is_a_contract_error(
    router: respx.MockRouter, p6: HttpP6Client, sleeper: SleepRecorder
) -> None:
    body = {"patient": {"patient_id": "p1"}, "conditions": [{"condition_id": "c1"}]}
    router.get(RECORD_PATH).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(P6ContractError, match="failed validation"):
        p6.get_record("p1", to=EVAL_AS_OF)
    assert sleeper.delays == []


def test_record_without_patient_object_is_a_contract_error(
    router: respx.MockRouter, p6: HttpP6Client
) -> None:
    router.get(RECORD_PATH).mock(return_value=httpx.Response(200, json={"conditions": []}))
    with pytest.raises(P6ContractError, match="lacks a patient object"):
        p6.get_record("p1", to=EVAL_AS_OF)


def test_non_object_record_body_is_a_contract_error(
    router: respx.MockRouter, p6: HttpP6Client
) -> None:
    router.get(RECORD_PATH).mock(return_value=httpx.Response(200, json=[1, 2]))
    with pytest.raises(P6ContractError, match="not an object"):
        p6.get_record("p1", to=EVAL_AS_OF)


def test_non_json_body_is_a_contract_error(router: respx.MockRouter, p6: HttpP6Client) -> None:
    router.get("/healthz").mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(P6ContractError, match="non-JSON body"):
        p6.healthz()


def test_healthz_missing_fields_is_a_contract_error(
    router: respx.MockRouter, p6: HttpP6Client
) -> None:
    router.get("/healthz").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    with pytest.raises(P6ContractError, match="ServiceInfo failed validation"):
        p6.healthz()


def test_features_without_features_object_is_a_contract_error(
    router: respx.MockRouter, p6: HttpP6Client
) -> None:
    router.get(FEATURES_PATH).mock(return_value=httpx.Response(200, json={"features": []}))
    with pytest.raises(P6ContractError, match="lacks a features object"):
        p6.get_features("p1", as_of=EVAL_AS_OF)


def test_get_record_masks_death_after_to(router: respx.MockRouter, p6: HttpP6Client) -> None:
    body = record_body(death_date=date(2026, 2, 1))
    assert body["patient"]["death_date"] == "2026-02-01"
    router.get(RECORD_PATH).mock(return_value=httpx.Response(200, json=body))
    record = p6.get_record("p1", to=EVAL_AS_OF)
    assert record.patient.death_date is None
    assert record.as_of == EVAL_AS_OF
    assert not hasattr(record.patient, "race")
