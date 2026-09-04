"""P6 boundary models: ``extra="ignore"`` drops demographics and P6-only columns; frozen."""

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from caregap.p6.models import (
    ConditionEvent,
    EncounterEvent,
    FeatureRow,
    MedicationEvent,
    ObservationEvent,
    PatientHeader,
    PatientPage,
    PatientRecord,
    ProcedureEvent,
)

P6_PATIENT = {
    "patient_id": "p1",
    "birth_date": "1960-06-15",
    "death_date": None,
    "sex": "female",
    "race": "2106-3",
    "ethnicity": "2186-5",
    "city": "Springfield",
    "state": "MA",
    "postal_code": "01101",
}


def test_patient_header_keeps_birth_death_sex_only() -> None:
    header = PatientHeader.model_validate(P6_PATIENT)
    assert header.model_dump() == {
        "patient_id": "p1",
        "birth_date": date(1960, 6, 15),
        "death_date": None,
        "sex": "female",
    }
    for dropped in ("race", "ethnicity", "city", "state", "postal_code"):
        assert not hasattr(header, dropped)
        assert dropped not in header.model_dump()
        assert dropped not in header.model_dump_json()
    assert set(PatientHeader.model_fields) == {"patient_id", "birth_date", "death_date", "sex"}


def test_patient_header_defaults() -> None:
    header = PatientHeader(patient_id="p2")
    assert (header.birth_date, header.death_date, header.sex) == (None, None, "unknown")


def test_condition_event_drops_p6_only_columns() -> None:
    row = ConditionEvent.model_validate(
        {
            "condition_id": "c1",
            "patient_id": "p1",
            "encounter_id": "e1",
            "code": "44054006",
            "code_system": "SNOMED",
            "code_display": "Diabetes",
            "clinical_status": "active",
            "verification_status": "confirmed",
            "onset_date": "2020-01-01",
            "abatement_date": None,
            "recorded_date": "2020-01-02",
            "date_precision": "day",
        }
    )
    assert row.onset_date == date(2020, 1, 1)
    assert row.recorded_date == date(2020, 1, 2)
    assert not hasattr(row, "clinical_status")
    assert not hasattr(row, "verification_status")
    assert not hasattr(row, "patient_id")
    assert set(row.model_dump()) == set(ConditionEvent.model_fields)


def test_observation_event_keeps_component_link_and_coerces_value() -> None:
    row = ObservationEvent.model_validate(
        {
            "observation_id": "o2",
            "parent_observation_id": "o1",
            "patient_id": "p1",
            "code": "8480-6",
            "code_system": "LOINC",
            "category": "vital-signs",
            "effective_ts": "2025-03-15T09:00:00",
            "effective_date": "2025-03-15",
            "date_precision": "day",
            "value_num": 128,
            "value_unit": "mm[Hg]",
            "status": "final",
        }
    )
    assert row.parent_observation_id == "o1"
    assert row.value_num == 128.0
    assert isinstance(row.value_num, float)
    assert row.value_unit == "mm[Hg]"
    assert row.effective_date == date(2025, 3, 15)
    assert not hasattr(row, "status")
    assert not hasattr(row, "effective_ts")


def test_procedure_and_medication_events_drop_status_where_not_read() -> None:
    proc = ProcedureEvent.model_validate(
        {
            "procedure_id": "pr1",
            "patient_id": "p1",
            "code": "71651007",
            "code_system": "SNOMED",
            "performed_date": "2025-04-01",
            "date_precision": "day",
            "status": "completed",
        }
    )
    assert not hasattr(proc, "status")
    assert proc.performed_end_date is None
    med = MedicationEvent.model_validate(
        {
            "medication_request_id": "m1",
            "patient_id": "p1",
            "code": "617310",
            "code_system": "RXNORM",
            "authored_date": "2025-02-01",
            "date_precision": "day",
            "status": "active",
            "intent": "order",
        }
    )
    # MedicationRequest.status IS kept: read only by the statin rule / E3 (ADR-0002).
    assert (med.status, med.intent) == ("active", "order")


def test_encounter_event_parses_timestamps_and_drops_type_system() -> None:
    row = EncounterEvent.model_validate(
        {
            "encounter_id": "e1",
            "patient_id": "p1",
            "encounter_class": "IMP",
            "type_code": "185349003",
            "type_system": "SNOMED",
            "type_display": "Encounter for check up",
            "start_ts": "2025-03-01T09:00:00",
            "end_ts": "2025-03-03T11:30:00",
            "start_date": "2025-03-01",
            "date_precision": "day",
        }
    )
    assert row.start_ts == datetime(2025, 3, 1, 9, 0)
    assert row.end_ts == datetime(2025, 3, 3, 11, 30)
    assert row.start_date == date(2025, 3, 1)
    assert not hasattr(row, "type_system")


def test_patient_record_ignores_sections_the_engine_never_reads() -> None:
    record = PatientRecord.model_validate(
        {
            "patient": P6_PATIENT,
            "as_of": "2025-12-31",
            "immunizations": [{"immunization_id": "i1", "code": "140"}],
            "claim_diagnoses": [{"claim_id": "cl1"}],
            "counts": {"conditions": 0},
        }
    )
    assert record.as_of == date(2025, 12, 31)
    assert record.conditions == []
    assert record.encounters == []
    for dropped in ("immunizations", "claim_diagnoses", "counts"):
        assert not hasattr(record, dropped)
    assert not hasattr(record.patient, "race")


def test_models_are_frozen() -> None:
    header = PatientHeader.model_validate(P6_PATIENT)
    with pytest.raises(ValidationError):
        header.sex = "male"
    record = PatientRecord(patient=header, as_of=date(2025, 12, 31))
    with pytest.raises(ValidationError):
        record.as_of = date(2024, 12, 31)


def test_feature_row_defaults_and_extra_columns() -> None:
    row = FeatureRow.model_validate(
        {"patient_id": "p1", "as_of": "2025-12-31", "brand_new_feature": 1, "age_years": 65}
    )
    assert row.age_years == 65
    assert row.has_diabetes is False
    assert row.latest_sbp is None
    assert not hasattr(row, "brand_new_feature")


def test_patient_page_drops_last_ingested_at() -> None:
    page = PatientPage.model_validate(
        {
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
            "total": 1,
        }
    )
    assert page.total == 1
    assert page.items[0].patient_id == "p1"
    assert page.items[0].deceased is False
    assert not hasattr(page.items[0], "last_ingested_at")


def test_required_fields_are_enforced() -> None:
    with pytest.raises(ValidationError):
        ConditionEvent.model_validate({"condition_id": "c1", "code": "x", "code_system": "SNOMED"})
    with pytest.raises(ValidationError):
        PatientHeader.model_validate({"birth_date": "1960-01-01"})
