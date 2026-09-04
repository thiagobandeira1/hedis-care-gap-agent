"""Evidence helpers: value-set membership, window filters, activity, refs (tested once)."""

from datetime import date, datetime

import pytest

from caregap.measures.evidence import (
    cond_ref,
    condition_active_in,
    conditions_in,
    enc_ref,
    encounter_class_of,
    encounters_in,
    latest,
    med_ref,
    medications_in,
    obs_ref,
    observations_in,
    observations_with_code,
    patient_ref,
    proc_ref,
    procedures_in,
)
from caregap.measures.value_sets import ValueSets
from caregap.measures.windows import Window, lookback_years, measurement_year
from tests.factories import (
    EVAL_AS_OF,
    bp_panel,
    build_record,
    condition,
    encounter,
    medication,
    observation,
    procedure,
)

MY_2025 = Window(start=date(2025, 1, 1), end=date(2025, 12, 31), label="MY")


def test_conditions_in_filters_by_set_and_system_and_sorts_by_onset_then_id(
    small_value_sets: ValueSets,
) -> None:
    record = build_record(
        conditions=[
            condition("44054006", onset=date(2021, 1, 1), condition_id="c9"),
            condition("44054006", onset=date(2019, 1, 1), condition_id="c1"),
            condition("44054006", onset=date(2021, 1, 1), condition_id="c2"),
            condition("59621000", onset=date(2018, 1, 1), condition_id="c3"),
            condition("44054006", onset=date(2017, 1, 1), condition_id="c4", code_system="ICD10"),
        ]
    )
    assert [c.condition_id for c in conditions_in(record, small_value_sets, "diabetes_snomed")] == [
        "c1",
        "c2",
        "c9",
    ]
    assert [
        c.condition_id for c in conditions_in(record, small_value_sets, "hypertension_snomed")
    ] == ["c3"]


def test_unknown_value_set_id_raises_key_error(small_value_sets: ValueSets) -> None:
    with pytest.raises(KeyError, match="unknown value set 'nope'"):
        conditions_in(build_record(), small_value_sets, "nope")


@pytest.mark.parametrize(
    ("onset", "abatement", "active"),
    [
        (date(2020, 1, 1), None, True),
        (date(2020, 1, 1), date(2025, 1, 1), True),  # abated ON window start: still active
        (date(2020, 1, 1), date(2024, 12, 31), False),  # abated the day before the window
        (date(2020, 1, 1), date(2026, 3, 1), True),  # abates after the window
        (date(2025, 12, 31), None, True),  # onset ON window end
        (date(2026, 1, 1), None, False),  # onset after window end
        (date(2025, 6, 1), date(2025, 6, 1), True),  # same-day onset and abatement inside
    ],
)
def test_condition_active_in(onset: date, abatement: date | None, active: bool) -> None:
    result = condition_active_in(condition("44054006", onset=onset, abatement=abatement), MY_2025)
    assert result is active


def test_procedures_in_window_edges_and_order(small_value_sets: ValueSets) -> None:
    window = lookback_years(EVAL_AS_OF, 10, label="colonoscopy")
    record = build_record(
        procedures=[
            procedure("73761001", performed=date(2016, 1, 1), procedure_id="in_start"),
            procedure("73761001", performed=date(2015, 12, 31), procedure_id="before"),
            procedure("73761001", performed=EVAL_AS_OF, procedure_id="on_end"),
            procedure("73761001", performed=date(2026, 1, 1), procedure_id="after"),
            procedure("71651007", performed=date(2020, 1, 1), procedure_id="mammogram"),
            procedure(
                "73761001", performed=date(2020, 1, 1), procedure_id="wrong", code_system="CPT"
            ),
        ]
    )
    found = procedures_in(record, small_value_sets, "colonoscopy_proc", window)
    assert [p.procedure_id for p in found] == ["in_start", "on_end"]


def test_observations_in_and_with_code(small_value_sets: ValueSets) -> None:
    panel = bp_panel(effective=date(2025, 3, 1))
    record = build_record(
        observations=[
            *panel,
            observation("85354-9", effective=date(2024, 12, 31), observation_id="old"),
            observation("72166-2", effective=date(2025, 5, 1), observation_id="tob"),
            observation(
                "85354-9", effective=date(2025, 5, 1), observation_id="snomed", code_system="SNOMED"
            ),
        ]
    )
    window = measurement_year(EVAL_AS_OF)
    in_set = observations_in(record, small_value_sets, "bp_loinc", window)
    assert [o.observation_id for o in in_set] == ["o1", "o2", "o3"]
    # observations_with_code ignores the code system but honours the window.
    assert [o.observation_id for o in observations_with_code(record, "85354-9", window)] == [
        "o1",
        "snomed",
    ]
    assert [
        o.observation_id
        for o in observations_in(record, small_value_sets, "tobacco_status_loinc", window)
    ] == ["tob"]


def test_medications_in_with_and_without_window(small_value_sets: ValueSets) -> None:
    record = build_record(
        medications=[
            medication("617310", authored=date(2025, 2, 1), medication_request_id="my"),
            medication("617310", authored=date(2019, 2, 1), medication_request_id="old"),
            medication(
                "617310", authored=date(2025, 2, 1), medication_request_id="ndc", code_system="NDC"
            ),
            medication("310436", authored=date(2025, 2, 1), medication_request_id="other"),
        ]
    )
    assert [
        m.medication_request_id for m in medications_in(record, small_value_sets, "statin_rxnorm")
    ] == [
        "old",
        "my",
    ]
    window = measurement_year(EVAL_AS_OF)
    assert [
        m.medication_request_id
        for m in medications_in(record, small_value_sets, "statin_rxnorm", window)
    ] == ["my"]


def test_encounters_in_sorted_by_start_then_id() -> None:
    record = build_record(
        encounters=[
            encounter(start=date(2025, 3, 1), encounter_id="b"),
            encounter(start=date(2025, 3, 1), encounter_id="a"),
            encounter(start=date(2025, 1, 1), encounter_id="z"),
            encounter(start=date(2024, 12, 31), encounter_id="out"),
        ]
    )
    assert [e.encounter_id for e in encounters_in(record, MY_2025)] == ["z", "a", "b"]


def test_encounter_class_of() -> None:
    record = build_record(encounters=[encounter(encounter_id="e7", encounter_class="IMP")])
    assert encounter_class_of(record, "e7") == "IMP"
    assert encounter_class_of(record, "missing") is None
    assert encounter_class_of(record, None) is None


def test_latest() -> None:
    assert latest([]) is None
    assert latest([date(2025, 1, 1), date(2025, 6, 1), date(2024, 1, 1)]) == date(2025, 6, 1)


def test_ref_builders_carry_ids_codes_and_dates_only() -> None:
    c = condition("44054006", onset=date(2020, 1, 1), display="Diabetes", condition_id="c1")
    ref = cond_ref(c, "eligibility")
    assert ref.model_dump() == {
        "section": "conditions",
        "event_id": "c1",
        "code": "44054006",
        "code_system": "SNOMED",
        "display": "Diabetes",
        "event_date": date(2020, 1, 1),
        "role": "eligibility",
    }
    o = observation("72166-2", effective=date(2025, 5, 1), observation_id="o1")
    assert (obs_ref(o, "numerator").section, obs_ref(o, "numerator").event_date) == (
        "observations",
        date(2025, 5, 1),
    )
    p = procedure("71651007", performed=date(2025, 4, 1), procedure_id="pr1")
    assert (proc_ref(p, "numerator").event_id, proc_ref(p, "numerator").event_date) == (
        "pr1",
        date(2025, 4, 1),
    )
    m = medication("617310", authored=date(2025, 2, 1), medication_request_id="m1")
    assert (med_ref(m, "numerator").section, med_ref(m, "numerator").code_system) == (
        "medications",
        "RXNORM",
    )
    e = encounter(
        start=date(2025, 3, 1),
        encounter_id="e1",
        type_code="185349003",
        type_display="Encounter for check up",
        start_ts=datetime(2025, 3, 1, 9, 0),
    )
    enc = enc_ref(e, "escalation")
    assert (enc.section, enc.code, enc.code_system, enc.display, enc.event_date) == (
        "encounters",
        "185349003",
        None,
        "Encounter for check up",
        date(2025, 3, 1),
    )
    pr = patient_ref("exclusion", event_date=date(2025, 7, 1))
    assert (pr.section, pr.event_id, pr.code, pr.event_date, pr.role) == (
        "patient",
        "patient",
        None,
        date(2025, 7, 1),
        "exclusion",
    )
