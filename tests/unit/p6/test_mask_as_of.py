"""``mask_as_of`` hides what P6's ``?to=`` cannot; ``record_from_p6_payload`` applies it."""

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from caregap.p6.client import P6ContractError, mask_as_of, record_from_p6_payload
from tests.factories import (
    DEMO_AS_OF,
    EVAL_AS_OF,
    bp_panel,
    build_record,
    condition,
    encounter,
    medication,
    observation,
    p6_payload,
    patient,
    procedure,
)

AFTER = date(2026, 3, 1)
"""A date after the eval anchor (Dec 31 2025) but inside the demo MY."""


def test_future_death_is_nulled_and_past_death_kept() -> None:
    future = build_record(patient_header=patient(death_date=AFTER))
    assert mask_as_of(future, EVAL_AS_OF).patient.death_date is None
    past = build_record(patient_header=patient(death_date=date(2025, 6, 1)))
    assert mask_as_of(past, EVAL_AS_OF).patient.death_date == date(2025, 6, 1)
    on_day = build_record(patient_header=patient(death_date=EVAL_AS_OF))
    assert mask_as_of(on_day, EVAL_AS_OF).patient.death_date == EVAL_AS_OF


def test_future_abatement_is_nulled_and_abatement_on_as_of_kept() -> None:
    record = build_record(
        conditions=[
            condition("44054006", onset=date(2020, 1, 1), abatement=AFTER),
            condition("59621000", onset=date(2020, 1, 1), abatement=EVAL_AS_OF),
            condition("26929004", onset=date(2020, 1, 1), abatement=date(2024, 1, 1)),
        ]
    )
    masked = mask_as_of(record, EVAL_AS_OF)
    assert [c.abatement_date for c in masked.conditions] == [None, EVAL_AS_OF, date(2024, 1, 1)]


def test_events_after_as_of_are_dropped_in_every_section_and_on_as_of_kept() -> None:
    record = build_record(
        conditions=[
            condition("44054006", onset=EVAL_AS_OF, condition_id="keep"),
            condition("44054006", onset=AFTER, condition_id="drop"),
        ],
        observations=[
            observation("72166-2", effective=EVAL_AS_OF, observation_id="keep"),
            observation("72166-2", effective=AFTER, observation_id="drop"),
        ],
        procedures=[
            procedure("71651007", performed=EVAL_AS_OF, procedure_id="keep"),
            procedure("71651007", performed=AFTER, procedure_id="drop"),
        ],
        medications=[
            medication("617310", authored=EVAL_AS_OF, medication_request_id="keep"),
            medication("617310", authored=AFTER, medication_request_id="drop"),
        ],
        encounters=[
            encounter(start=EVAL_AS_OF, encounter_id="keep"),
            encounter(start=AFTER, encounter_id="drop"),
        ],
    )
    masked = mask_as_of(record, EVAL_AS_OF)
    assert [c.condition_id for c in masked.conditions] == ["keep"]
    assert [o.observation_id for o in masked.observations] == ["keep"]
    assert [p.procedure_id for p in masked.procedures] == ["keep"]
    assert [m.medication_request_id for m in masked.medications] == ["keep"]
    assert [e.encounter_id for e in masked.encounters] == ["keep"]


def test_future_end_dates_are_nulled() -> None:
    record = build_record(
        procedures=[
            procedure("385763009", performed=date(2025, 12, 1), performed_end=AFTER),
            procedure("385763009", performed=date(2025, 12, 1), performed_end=EVAL_AS_OF),
        ],
        encounters=[
            encounter(
                start=date(2025, 12, 30),
                start_ts=datetime(2025, 12, 30, 8, 0),
                end_ts=datetime(2026, 1, 2, 10, 0),
            ),
            encounter(
                start=date(2025, 12, 30),
                start_ts=datetime(2025, 12, 30, 8, 0),
                end_ts=datetime(2025, 12, 31, 23, 59),
            ),
        ],
    )
    masked = mask_as_of(record, EVAL_AS_OF)
    assert [p.performed_end_date for p in masked.procedures] == [None, EVAL_AS_OF]
    assert [e.end_ts for e in masked.encounters] == [None, datetime(2025, 12, 31, 23, 59)]
    # start_ts is a primary-event timestamp and is never touched.
    assert all(e.start_ts == datetime(2025, 12, 30, 8, 0) for e in masked.encounters)


def test_as_of_is_stamped_and_the_input_is_not_mutated() -> None:
    record = build_record(
        patient_header=patient(death_date=AFTER),
        as_of=DEMO_AS_OF,
        conditions=[condition("44054006", onset=date(2020, 1, 1), abatement=AFTER)],
    )
    masked = mask_as_of(record, EVAL_AS_OF)
    assert masked.as_of == EVAL_AS_OF
    assert record.as_of == DEMO_AS_OF
    assert record.patient.death_date == AFTER
    assert record.conditions[0].abatement_date == AFTER
    # Masking to the later date keeps everything (AFTER <= DEMO_AS_OF).
    later = mask_as_of(record, DEMO_AS_OF)
    assert later.patient.death_date == AFTER
    assert later.conditions[0].abatement_date == AFTER


def test_mask_is_idempotent() -> None:
    record = build_record(
        patient_header=patient(death_date=AFTER),
        conditions=[condition("44054006", onset=date(2020, 1, 1), abatement=AFTER)],
        encounters=[encounter(start=AFTER)],
    )
    once = mask_as_of(record, EVAL_AS_OF)
    assert mask_as_of(once, EVAL_AS_OF) == once


def test_record_from_p6_payload_on_a_p6_shaped_body_with_component_observations() -> None:
    raw = build_record(
        patient_header=patient(death_date=AFTER),
        observations=[
            *bp_panel(effective=date(2025, 3, 15), sbp=138.0, dbp=88.0, encounter_id="e1"),
            observation("72166-2", effective=AFTER),
        ],
        conditions=[condition("59621000", onset=date(2018, 1, 1), abatement=AFTER)],
        encounters=[encounter(start=date(2025, 3, 15), encounter_id="e1")],
    )
    payload = p6_payload(raw)
    assert payload["patient"]["race"] == "2106-3"  # P6 sends demographics ...
    assert payload["counts"]["observations"] == 4
    record = record_from_p6_payload(payload, EVAL_AS_OF)
    # ... and P1 drops them at the boundary, then masks.
    assert not hasattr(record.patient, "race")
    assert record.patient.model_dump() == {
        "patient_id": "p1",
        "birth_date": date(1960, 6, 15),
        "death_date": None,
        "sex": "female",
    }
    assert record.as_of == EVAL_AS_OF
    assert record.conditions[0].abatement_date is None
    assert [o.observation_id for o in record.observations] == ["o1", "o2", "o3"]
    parent, sbp, dbp = record.observations
    assert parent.code == "85354-9"
    assert parent.parent_observation_id is None
    assert (sbp.code, sbp.parent_observation_id, sbp.value_num, sbp.value_unit) == (
        "8480-6",
        "o1",
        138.0,
        "mm[Hg]",
    )
    assert (dbp.code, dbp.parent_observation_id, dbp.value_num) == ("8462-4", "o1", 88.0)
    assert all(o.encounter_id == "e1" for o in record.observations)
    assert not hasattr(record, "counts")


def test_record_from_p6_payload_tolerates_missing_sections() -> None:
    record = record_from_p6_payload({"patient": {"patient_id": "p1"}}, EVAL_AS_OF)
    assert record.patient.patient_id == "p1"
    assert record.patient.sex == "unknown"
    assert (record.conditions, record.observations, record.procedures) == ([], [], [])
    assert (record.medications, record.encounters) == ([], [])


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"patient": None},
        {"patient": "p1"},
        {"patient": {"birth_date": "1960-01-01"}},
        {"patient": []},
    ],
)
def test_record_from_p6_payload_without_a_patient_object_is_a_contract_error(
    payload: dict[str, object],
) -> None:
    with pytest.raises(P6ContractError, match="lacks a patient object"):
        record_from_p6_payload(payload, EVAL_AS_OF)


def test_record_from_p6_payload_surfaces_row_validation_errors() -> None:
    payload = {"patient": {"patient_id": "p1"}, "conditions": [{"condition_id": "c1"}]}
    with pytest.raises(ValidationError):
        record_from_p6_payload(payload, EVAL_AS_OF)
