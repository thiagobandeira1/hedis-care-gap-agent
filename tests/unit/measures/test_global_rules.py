"""Global rules (SPEC section 2): death / hospice exclusions, E1 and E4 global escalations."""

from datetime import date

from caregap.measures.context import MeasurementContext
from caregap.measures.rules.global_rules import (
    HOSPICE_LOOKBACK_DAYS,
    died_before_my,
    global_escalations,
    global_exclusions,
    hospice_events,
)
from caregap.measures.value_sets import ValueSets
from caregap.measures.windows import measurement_year
from caregap.p6.client import mask_as_of
from caregap.p6.models import PatientRecord
from tests.factories import (
    BIRTH_1960,
    DEMO_AS_OF,
    EVAL_AS_OF,
    build_record,
    condition,
    encounter,
    medication,
    patient,
    procedure,
)

HOSPICE = "385763009"
DEMENTIA = "26929004"
DONEPEZIL = "310436"
BIRTH_1959 = date(1959, 6, 15)  # 66 at Dec 31 2025

MY_START_2025 = date(2025, 1, 1)
ONE_DAY_BEFORE = date(2024, 12, 31)
DAYS_89_BEFORE = date(2024, 10, 4)
DAYS_90_BEFORE = date(2024, 10, 3)
DAYS_91_BEFORE = date(2024, 10, 2)


def ctx_for(as_of: date = EVAL_AS_OF, birth: date | None = BIRTH_1960) -> MeasurementContext:
    return MeasurementContext.for_(as_of, birth)


def hospice_record(*days: date, as_of: date = EVAL_AS_OF) -> PatientRecord:
    return build_record(as_of=as_of, procedures=[procedure(HOSPICE, performed=d) for d in days])


def categories(record: PatientRecord, ctx: MeasurementContext, vs: ValueSets) -> list[str]:
    return [hit.category for hit in global_exclusions(record, ctx, vs)]


def kinds(record: PatientRecord, ctx: MeasurementContext, vs: ValueSets) -> list[str]:
    return [flag.kind for flag in global_escalations(record, ctx, vs)]


# --- death -------------------------------------------------------------------------------


def test_lookback_constant_is_90_days() -> None:
    assert HOSPICE_LOOKBACK_DAYS == 90
    assert MY_START_2025.toordinal() - DAYS_90_BEFORE.toordinal() == 90


def test_death_before_my_is_not_eligible_not_excluded(small_value_sets: ValueSets) -> None:
    record = build_record(patient_header=patient(death_date=ONE_DAY_BEFORE))
    ctx = ctx_for()
    assert died_before_my(record, ctx) is True
    assert categories(record, ctx, small_value_sets) == []


def test_death_on_my_start_is_an_exclusion_not_died_before(small_value_sets: ValueSets) -> None:
    record = build_record(patient_header=patient(death_date=MY_START_2025))
    ctx = ctx_for()
    assert died_before_my(record, ctx) is False
    hits = global_exclusions(record, ctx, small_value_sets)
    assert [h.category for h in hits] == ["died_during_measurement_period"]
    hit = hits[0]
    assert hit.source == "quoted"
    assert hit.window_label == "measurement period"
    assert len(hit.evidence) == 1
    ref = hit.evidence[0]
    assert (ref.section, ref.event_id, ref.role) == ("patient", "patient", "exclusion")
    assert ref.event_date == MY_START_2025


def test_death_on_as_of_is_still_in_the_measurement_period(small_value_sets: ValueSets) -> None:
    record = build_record(patient_header=patient(death_date=EVAL_AS_OF))
    assert categories(record, ctx_for(), small_value_sets) == ["died_during_measurement_period"]


def test_death_after_as_of_never_appears_because_mask_as_of_nulls_it(
    small_value_sets: ValueSets,
) -> None:
    # Died Sep 2026: inside MY2026 but after the demo as_of of Jun 30 2026.
    raw = build_record(patient_header=patient(death_date=date(2026, 9, 1)), as_of=DEMO_AS_OF)
    ctx = ctx_for(DEMO_AS_OF)
    masked = mask_as_of(raw, DEMO_AS_OF)
    assert masked.patient.death_date is None
    assert died_before_my(masked, ctx) is False
    assert categories(masked, ctx, small_value_sets) == []
    # Belt and braces: even an unmasked future death is outside [my_start, as_of].
    assert categories(raw, ctx, small_value_sets) == []


def test_no_death_no_hit(small_value_sets: ValueSets) -> None:
    record = build_record()
    assert died_before_my(record, ctx_for()) is False
    assert categories(record, ctx_for(), small_value_sets) == []


# --- hospice -----------------------------------------------------------------------------


def test_hospice_in_my_is_an_exclusion_with_procedure_evidence(
    small_value_sets: ValueSets,
) -> None:
    record = hospice_record(date(2025, 6, 1))
    ctx = ctx_for()
    hits = global_exclusions(record, ctx, small_value_sets)
    assert [h.category for h in hits] == ["hospice_during_measurement_period"]
    hit = hits[0]
    assert hit.source == "quoted"
    assert hit.window_label == measurement_year(EVAL_AS_OF).label
    assert [(r.section, r.event_id, r.code, r.role) for r in hit.evidence] == [
        ("procedures", "pr1", HOSPICE, "exclusion")
    ]
    assert kinds(record, ctx, small_value_sets) == []


def test_hospice_condition_and_encounter_type_also_count(small_value_sets: ValueSets) -> None:
    record = build_record(
        conditions=[condition(HOSPICE, onset=date(2025, 2, 1))],
        encounters=[encounter(start=date(2025, 3, 1), type_code=HOSPICE)],
    )
    hits = global_exclusions(record, ctx_for(), small_value_sets)
    assert len(hits) == 1
    assert [(r.section, r.event_id) for r in hits[0].evidence] == [
        ("conditions", "c1"),
        ("encounters", "e1"),
    ]


def test_hospice_events_ignores_other_code_systems_and_codes(
    small_value_sets: ValueSets,
) -> None:
    record = build_record(
        procedures=[procedure(HOSPICE, performed=date(2025, 6, 1), code_system="CPT")],
        conditions=[condition("44054006", onset=date(2025, 6, 1))],
        encounters=[encounter(start=date(2025, 6, 1), type_code="185349003")],
    )
    assert hospice_events(record, small_value_sets, measurement_year(EVAL_AS_OF)) == []


def test_hospice_one_day_before_my_is_e1_not_an_exclusion(small_value_sets: ValueSets) -> None:
    record = hospice_record(ONE_DAY_BEFORE)
    ctx = ctx_for()
    assert categories(record, ctx, small_value_sets) == []
    flags = global_escalations(record, ctx, small_value_sets)
    assert [(f.kind, f.scope) for f in flags] == [("E1", "global")]
    flag = flags[0]
    assert "90 days" in flag.reason
    assert [(r.section, r.event_id, r.role, r.event_date) for r in flag.evidence] == [
        ("procedures", "pr1", "escalation", ONE_DAY_BEFORE)
    ]


def test_hospice_90_days_before_is_still_e1(small_value_sets: ValueSets) -> None:
    assert kinds(hospice_record(DAYS_90_BEFORE), ctx_for(), small_value_sets) == ["E1"]


def test_hospice_89_days_before_is_e1(small_value_sets: ValueSets) -> None:
    record = hospice_record(DAYS_89_BEFORE)
    assert kinds(record, ctx_for(), small_value_sets) == ["E1"]
    assert categories(record, ctx_for(), small_value_sets) == []


def test_hospice_91_days_before_raises_nothing(small_value_sets: ValueSets) -> None:
    record = hospice_record(DAYS_91_BEFORE)
    assert categories(record, ctx_for(), small_value_sets) == []
    assert kinds(record, ctx_for(), small_value_sets) == []


def test_hospice_89_days_before_and_in_my_is_exclusion_without_e1(
    small_value_sets: ValueSets,
) -> None:
    record = hospice_record(DAYS_89_BEFORE, date(2025, 2, 1))
    ctx = ctx_for()
    assert categories(record, ctx, small_value_sets) == ["hospice_during_measurement_period"]
    assert kinds(record, ctx, small_value_sets) == []


def test_hospice_after_as_of_is_outside_the_window(small_value_sets: ValueSets) -> None:
    record = hospice_record(date(2026, 8, 1), as_of=DEMO_AS_OF)
    ctx = ctx_for(DEMO_AS_OF)
    assert categories(record, ctx, small_value_sets) == []
    assert kinds(record, ctx, small_value_sets) == []


def test_hospice_years_before_my_is_deterministically_not_an_exclusion(
    small_value_sets: ValueSets,
) -> None:
    record = hospice_record(date(2021, 5, 1))
    assert categories(record, ctx_for(), small_value_sets) == []
    assert kinds(record, ctx_for(), small_value_sets) == []


# --- E4 ----------------------------------------------------------------------------------


def dementia_record(
    *,
    birth: date = BIRTH_1959,
    acute_class: str | None = "IMP",
    abatement: date | None = None,
    via_medication: bool = False,
    acute_count: int = 1,
) -> tuple[PatientRecord, MeasurementContext]:
    encounters = [
        encounter(start=date(2025, 5, 1 + i), encounter_class=acute_class or "AMB")
        for i in range(acute_count)
    ]
    if via_medication:
        record = build_record(
            patient_header=patient(birth_date=birth),
            medications=[medication(DONEPEZIL, authored=date(2023, 1, 10))],
            encounters=encounters,
        )
    else:
        record = build_record(
            patient_header=patient(birth_date=birth),
            conditions=[condition(DEMENTIA, onset=date(2022, 1, 1), abatement=abatement)],
            encounters=encounters,
        )
    return record, ctx_for(EVAL_AS_OF, birth)


def test_e4_dementia_plus_inpatient_at_66(small_value_sets: ValueSets) -> None:
    record, ctx = dementia_record()
    flags = global_escalations(record, ctx, small_value_sets)
    assert [(f.kind, f.scope) for f in flags] == [("E4", "global")]
    assert [(r.section, r.event_id, r.role) for r in flags[0].evidence] == [
        ("conditions", "c1", "escalation"),
        ("encounters", "e1", "escalation"),
    ]
    # Never an exclusion.
    assert categories(record, ctx, small_value_sets) == []


def test_e4_not_raised_at_65(small_value_sets: ValueSets) -> None:
    record, ctx = dementia_record(birth=BIRTH_1960)
    assert ctx.age_at_my_end == 65
    assert kinds(record, ctx, small_value_sets) == []


def test_e4_not_raised_without_acute_care(small_value_sets: ValueSets) -> None:
    record, ctx = dementia_record(acute_class=None)
    assert kinds(record, ctx, small_value_sets) == []


def test_e4_ed_visit_counts_as_acute(small_value_sets: ValueSets) -> None:
    record, ctx = dementia_record(acute_class="EMER")
    assert kinds(record, ctx, small_value_sets) == ["E4"]


def test_e4_via_dementia_medication(small_value_sets: ValueSets) -> None:
    record, ctx = dementia_record(via_medication=True)
    flags = global_escalations(record, ctx, small_value_sets)
    assert [f.kind for f in flags] == ["E4"]
    assert flags[0].evidence[0].section == "medications"
    assert flags[0].evidence[0].event_id == "m1"


def test_e4_dementia_abated_before_my_does_not_count(small_value_sets: ValueSets) -> None:
    record, ctx = dementia_record(abatement=ONE_DAY_BEFORE)
    assert kinds(record, ctx, small_value_sets) == []
    active, ctx2 = dementia_record(abatement=MY_START_2025)
    assert kinds(active, ctx2, small_value_sets) == ["E4"]


def test_e4_acute_evidence_is_capped_at_three(small_value_sets: ValueSets) -> None:
    record, ctx = dementia_record(acute_count=5)
    flags = global_escalations(record, ctx, small_value_sets)
    assert len(flags) == 1
    assert [r.section for r in flags[0].evidence] == ["conditions"] + ["encounters"] * 3


def test_e4_skipped_when_age_unknown(small_value_sets: ValueSets) -> None:
    record, _ = dementia_record()
    assert kinds(record, ctx_for(EVAL_AS_OF, None), small_value_sets) == []


def test_e4_acute_encounter_outside_my_does_not_count(small_value_sets: ValueSets) -> None:
    record = build_record(
        patient_header=patient(birth_date=BIRTH_1959),
        conditions=[condition(DEMENTIA, onset=date(2022, 1, 1))],
        encounters=[encounter(start=ONE_DAY_BEFORE, encounter_class="IMP")],
    )
    assert kinds(record, ctx_for(EVAL_AS_OF, BIRTH_1959), small_value_sets) == []


def test_e1_and_e4_can_coexist_in_order(small_value_sets: ValueSets) -> None:
    record = build_record(
        patient_header=patient(birth_date=BIRTH_1959),
        conditions=[condition(DEMENTIA, onset=date(2022, 1, 1))],
        procedures=[procedure(HOSPICE, performed=ONE_DAY_BEFORE)],
        encounters=[encounter(start=date(2025, 5, 1), encounter_class="IMP")],
    )
    assert kinds(record, ctx_for(EVAL_AS_OF, BIRTH_1959), small_value_sets) == ["E1", "E4"]
