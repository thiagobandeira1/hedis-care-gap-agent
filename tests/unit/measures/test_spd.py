"""SPD (D12, SUPD-style) rule tests.

Table-driven over an in-test ``RecordFactory`` and an in-test ``ValueSets`` built from exactly
the codes the rule needs (no dependency on the package JSON files). Covers SPEC section 9 edges
for SPD: window edges, statin status paths, any-intensity numerator (contrasted with SPC), the
SPC routing product choice, ESRD / dialysis exclusions, E3, death before / during the MY, the
prediabetes trap, shuffled-event order independence, and engine-level verdicts.
"""

import random
from dataclasses import dataclass, field
from datetime import date
from typing import get_args

import pytest

from caregap.measures.context import MeasurementContext
from caregap.measures.engine import MeasureEngine, RuleOutput
from caregap.measures.evidence import PREGNANCY_STATUS_LOINC
from caregap.measures.models import Coverage
from caregap.measures.rules.spc import SpcRule
from caregap.measures.rules.spd import (
    AGE_BAND,
    COVERAGE,
    DIABETES_WINDOW_LABEL,
    NON_EXCLUSIONS,
    ROUTED_TO_SPC,
    SpdRule,
)
from caregap.measures.tri import Tri
from caregap.measures.value_sets import (
    StatinIntensity,
    ValueSet,
    ValueSetCode,
    ValueSets,
)
from caregap.p6.models import (
    ConditionEvent,
    EncounterEvent,
    MedicationEvent,
    ObservationEvent,
    PatientHeader,
    PatientRecord,
    ProcedureEvent,
)

# --- anchors ---------------------------------------------------------------------------------

RETRO = date(2025, 12, 31)  # eval anchor: complete MY2025, retrospective
MY_START = date(2025, 1, 1)
MY_END = date(2025, 12, 31)
PRIOR_START = date(2024, 1, 1)
PRIOR_END = date(2024, 12, 31)
DEMO = date(2026, 6, 30)  # demo anchor: mid-MY2026

# --- codes -----------------------------------------------------------------------------------

SNOMED = "SNOMED"
RXNORM = "RXNORM"

T2DM = "44054006"
PREDIABETES = "15777000"
MI = "22298006"  # ASCVD
ESRD = "46177005"
DIALYSIS = "265764009"
PREGNANCY = "72892002"
PREGNANT = "77386006"  # SNOMED answer to LOINC 82810-3 "Pregnancy status"
HOSPICE = "385763009"
DEMENTIA = "26929004"
DONEPEZIL = "310436"

ATORVA_80 = "259255"  # high intensity
SIMVA_10 = "314231"  # low intensity
ATORVA_20_NO_INTENSITY = "617310"  # deliberately missing from the in-test intensity table
METFORMIN = "860975"  # not a statin

STATIN_CODES: tuple[str, ...] = (ATORVA_80, SIMVA_10, ATORVA_20_NO_INTENSITY)
INTENSITIES: dict[str, StatinIntensity] = {ATORVA_80: "high", SIMVA_10: "low"}


def _vs(set_id: str, system: str, codes: tuple[str, ...]) -> ValueSet:
    return ValueSet(
        id=set_id,
        code_system=system,
        source="synthea-scan",
        codes=[ValueSetCode(code=c) for c in codes],
    )


def build_value_sets(*, diabetes_codes: tuple[str, ...] = (T2DM,)) -> ValueSets:
    sets = [
        _vs("diabetes_snomed", SNOMED, diabetes_codes),
        _vs("prediabetes_trap_snomed", SNOMED, (PREDIABETES,)),
        _vs("ascvd_snomed", SNOMED, (MI,)),
        _vs("statin_rxnorm", RXNORM, STATIN_CODES),
        _vs("esrd_snomed", SNOMED, (ESRD,)),
        _vs("dialysis_snomed", SNOMED, (DIALYSIS,)),
        # Needed by SpcRule (contrast tests) and the global rules (engine-level tests).
        _vs("pregnancy_snomed", SNOMED, (PREGNANCY,)),
        _vs("hospice_snomed", SNOMED, (HOSPICE,)),
        _vs("dementia_snomed", SNOMED, (DEMENTIA,)),
        _vs("dementia_meds_rxnorm", RXNORM, (DONEPEZIL,)),
        ValueSet(
            id="statin_intensity",
            code_system=RXNORM,
            source="public-acc-aha",
            codes=[
                ValueSetCode(code=code, intensity=intensity)
                for code, intensity in INTENSITIES.items()
            ],
        ),
    ]
    return ValueSets(version="test", sets={s.id: s for s in sets})


VS = build_value_sets()

# --- record factory --------------------------------------------------------------------------


@dataclass
class RecordFactory:
    """Builds ``PatientRecord`` objects inline with deterministic, order-based event ids."""

    birth_date: date | None = date(1970, 6, 15)  # 55 at 2025-12-31
    sex: str = "male"
    death_date: date | None = None
    conditions: list[ConditionEvent] = field(default_factory=list)
    observations: list[ObservationEvent] = field(default_factory=list)
    procedures: list[ProcedureEvent] = field(default_factory=list)
    medications: list[MedicationEvent] = field(default_factory=list)
    encounters: list[EncounterEvent] = field(default_factory=list)

    def observation(
        self, code: str, effective: date, *, value_code: str | None = None, system: str = "LOINC"
    ) -> "RecordFactory":
        self.observations.append(
            ObservationEvent(
                observation_id=f"obs-{len(self.observations) + 1}",
                code=code,
                code_system=system,
                effective_date=effective,
                value_code=value_code,
                value_code_system="SNOMED" if value_code else None,
            )
        )
        return self

    def condition(
        self,
        code: str,
        onset: date,
        *,
        abatement: date | None = None,
        system: str = SNOMED,
    ) -> "RecordFactory":
        self.conditions.append(
            ConditionEvent(
                condition_id=f"cond-{len(self.conditions) + 1}",
                code=code,
                code_system=system,
                code_display=f"display {code}",
                onset_date=onset,
                abatement_date=abatement,
            )
        )
        return self

    def procedure(self, code: str, performed: date, *, system: str = SNOMED) -> "RecordFactory":
        self.procedures.append(
            ProcedureEvent(
                procedure_id=f"proc-{len(self.procedures) + 1}",
                code=code,
                code_system=system,
                performed_date=performed,
            )
        )
        return self

    def medication(
        self, code: str, authored: date, *, status: str | None = None, system: str = RXNORM
    ) -> "RecordFactory":
        self.medications.append(
            MedicationEvent(
                medication_request_id=f"med-{len(self.medications) + 1}",
                code=code,
                code_system=system,
                code_display=f"display {code}",
                authored_date=authored,
                status=status,
            )
        )
        return self

    def encounter(
        self, start: date, *, encounter_class: str = "AMB", type_code: str | None = None
    ) -> "RecordFactory":
        self.encounters.append(
            EncounterEvent(
                encounter_id=f"enc-{len(self.encounters) + 1}",
                encounter_class=encounter_class,
                type_code=type_code,
                start_date=start,
            )
        )
        return self

    def build(self, as_of: date = RETRO) -> PatientRecord:
        return PatientRecord(
            patient=PatientHeader(
                patient_id="p-1",
                birth_date=self.birth_date,
                death_date=self.death_date,
                sex=self.sex,
            ),
            as_of=as_of,
            conditions=list(self.conditions),
            observations=list(self.observations),
            procedures=list(self.procedures),
            medications=list(self.medications),
            encounters=list(self.encounters),
        )


def diabetic(
    *,
    birth_date: date | None = date(1970, 6, 15),
    sex: str = "male",
    death_date: date | None = None,
) -> RecordFactory:
    """A 55-year-old male with long-standing T2DM (the baseline SPD denominator member)."""
    rf = RecordFactory(birth_date=birth_date, sex=sex, death_date=death_date)
    return rf.condition(T2DM, date(2020, 3, 1))


def ctx_for(rf: RecordFactory, as_of: date = RETRO) -> MeasurementContext:
    return MeasurementContext.for_(as_of, rf.birth_date)


def evaluate(rf: RecordFactory, as_of: date = RETRO, vs: ValueSets = VS) -> RuleOutput:
    return SpdRule().evaluate(rf.build(as_of), ctx_for(rf, as_of), vs)


def engine_verdict(rf: RecordFactory, as_of: date = RETRO) -> str:
    engine = MeasureEngine([SpdRule()], value_sets=VS)
    return engine.evaluate_one(rf.build(as_of), ctx_for(rf, as_of), "SPD").verdict


# --- contract --------------------------------------------------------------------------------


def test_rule_contract() -> None:
    rule = SpdRule()
    assert rule.measure_id == "SPD"
    assert rule.rule_version == "spd-v1"
    assert AGE_BAND == (40, 75)
    engine = MeasureEngine([rule], value_sets=VS)
    assert engine.measure_ids == ("SPD",)


# --- denominator: age ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("birth_date", "expected", "fragment"),
    [
        (date(1985, 12, 31), "yes", "aged 40"),  # turns 40 on Dec 31 of the MY
        (date(1986, 1, 1), "no", "aged 39"),  # 39 at Dec 31 (40 the next day)
        (date(1950, 1, 1), "yes", "aged 75"),
        (date(1949, 12, 31), "no", "aged 76"),
        (None, "unknown", "birth_date unknown"),
    ],
)
def test_denominator_age_band_at_my_end(
    birth_date: date | None, expected: Tri, fragment: str
) -> None:
    out = evaluate(diabetic(birth_date=birth_date))
    assert out.denominator.value == expected
    assert any(fragment in r for r in out.denominator.reasons)


@pytest.mark.parametrize("sex", ["male", "female", "unknown", "other", ""])
def test_denominator_ignores_sex(sex: str) -> None:
    out = evaluate(diabetic(sex=sex))
    assert out.denominator.value == "yes"


def test_denominator_window_is_my_and_prior_year() -> None:
    out = evaluate(diabetic())
    assert (out.denominator.window_start, out.denominator.window_end) == (PRIOR_START, MY_END)


# --- denominator: diabetes as EED ------------------------------------------------------------


@pytest.mark.parametrize(
    ("onset", "abatement", "as_of", "expected"),
    [
        (MY_END, None, RETRO, "yes"),  # onset on the MY end
        (date(2026, 1, 1), None, RETRO, "no"),  # onset after the MY end (unmasked record)
        (PRIOR_START, None, RETRO, "yes"),  # onset on the prior-year start
        (date(2010, 1, 1), date(2023, 12, 31), RETRO, "no"),  # abated before the prior year
        (date(2010, 1, 1), PRIOR_START, RETRO, "no"),  # abated ON the prior-year start
        (date(2010, 1, 1), date(2024, 1, 2), RETRO, "yes"),  # abated the day after the start
        (date(2010, 1, 1), date(2024, 6, 30), RETRO, "yes"),  # abated during the prior year
        (date(2010, 1, 1), None, RETRO, "yes"),
        (date(2026, 12, 31), None, DEMO, "yes"),  # demo anchor: MY end is Dec 31 2026
        (date(2025, 1, 1), None, DEMO, "yes"),  # demo anchor: prior year is 2025
        (date(2010, 1, 1), date(2024, 12, 31), DEMO, "no"),  # abated before Jan 1 2025
    ],
)
def test_denominator_diabetes_window_edges(
    onset: date, abatement: date | None, as_of: date, expected: Tri
) -> None:
    rf = RecordFactory().condition(T2DM, onset, abatement=abatement)
    out = evaluate(rf, as_of)
    assert out.denominator.value == expected
    reason = "diabetes condition active in" if expected == "yes" else "no diabetes condition"
    assert any(r.startswith(reason) and DIABETES_WINDOW_LABEL in r for r in out.denominator.reasons)


def test_denominator_diabetes_evidence_refs() -> None:
    out = evaluate(diabetic())
    cond_refs = [e for e in out.denominator.evidence if e.section == "conditions"]
    assert [(e.event_id, e.code, e.role) for e in cond_refs] == [("cond-1", T2DM, "eligibility")]
    assert any(e.section == "patient" and e.role == "eligibility" for e in out.denominator.evidence)


def test_denominator_prediabetes_trap_never_counts() -> None:
    out = evaluate(RecordFactory().condition(PREDIABETES, date(2020, 1, 1)))
    assert out.denominator.value == "no"


def test_denominator_prediabetes_trap_even_if_leaked_into_diabetes_set() -> None:
    leaky = build_value_sets(diabetes_codes=(T2DM, PREDIABETES))
    out = evaluate(RecordFactory().condition(PREDIABETES, date(2020, 1, 1)), vs=leaky)
    assert out.denominator.value == "no"
    out = evaluate(RecordFactory().condition(T2DM, date(2020, 1, 1)), vs=leaky)
    assert out.denominator.value == "yes"


def test_denominator_wrong_code_system_never_counts() -> None:
    out = evaluate(RecordFactory().condition(T2DM, date(2020, 1, 1), system="ICD10"))
    assert out.denominator.value == "no"


# --- denominator: SPC routing (demo_choice) --------------------------------------------------


@pytest.mark.parametrize("sex", ["male", "female", "unknown"])
def test_denominator_routed_to_spc_when_ascvd_and_spc_eligible(sex: str) -> None:
    out = evaluate(diabetic(sex=sex).condition(MI, date(2019, 5, 5)))
    assert out.denominator.value == "no"
    assert ROUTED_TO_SPC in out.denominator.reasons
    ascvd_refs = [e for e in out.denominator.evidence if e.code == MI]
    assert [(e.event_id, e.role) for e in ascvd_refs] == [("cond-2", "eligibility")]


@pytest.mark.parametrize(
    ("onset", "as_of", "routed"),
    [
        (MY_END, RETRO, True),
        (date(2026, 1, 1), RETRO, False),  # onset after the MY end (unmasked record)
        (date(2026, 12, 31), DEMO, True),  # demo anchor: onset later in the MY still routes
        (date(2027, 1, 1), DEMO, False),
    ],
)
def test_denominator_routing_uses_my_end(onset: date, as_of: date, routed: bool) -> None:
    out = evaluate(diabetic().condition(MI, onset), as_of)
    assert (out.denominator.value == "no") is routed
    assert (ROUTED_TO_SPC in out.denominator.reasons) is routed


def test_denominator_routing_ignores_ascvd_abatement_as_spc_does() -> None:
    out = evaluate(diabetic().condition(MI, date(2010, 1, 1), abatement=date(2015, 1, 1)))
    assert out.denominator.value == "no"
    assert ROUTED_TO_SPC in out.denominator.reasons


def test_denominator_routing_unknown_when_spc_eligibility_unknown() -> None:
    out = evaluate(diabetic(birth_date=None).condition(MI, date(2019, 5, 5)))
    assert out.denominator.value == "unknown"
    assert ROUTED_TO_SPC not in out.denominator.reasons
    assert any("SPC eligibility unknown" in r for r in out.denominator.reasons)


@pytest.mark.parametrize("sex", ["male", "female", "unknown"])
@pytest.mark.parametrize("birth_date", [date(1985, 12, 31), date(1970, 6, 15), date(1950, 1, 1)])
def test_never_in_both_spc_and_spd_denominators(sex: str, birth_date: date) -> None:
    """Product choice: a patient is never simultaneously owed SPC and SPD outreach."""
    rf = diabetic(sex=sex, birth_date=birth_date).condition(MI, date(2019, 5, 5))
    record, ctx = rf.build(), ctx_for(rf)
    spc = SpcRule().evaluate(record, ctx, VS).denominator.value
    spd = SpdRule().evaluate(record, ctx, VS).denominator.value
    assert spc == "yes"
    assert spd == "no"


# --- denominator: death ----------------------------------------------------------------------


def test_denominator_no_when_died_before_my() -> None:
    out = evaluate(diabetic(death_date=date(2024, 12, 31)))
    assert out.denominator.value == "no"
    assert "died before the measurement year" in out.denominator.reasons
    assert any(
        e.section == "patient" and e.event_date == date(2024, 12, 31)
        for e in out.denominator.evidence
    )


def test_denominator_yes_when_died_during_my_global_rule_excludes() -> None:
    rf = diabetic(death_date=date(2025, 6, 1))
    assert evaluate(rf).denominator.value == "yes"
    assert engine_verdict(rf) == "excluded"


# --- numerator: on therapy as SPC, any intensity ---------------------------------------------


@pytest.mark.parametrize(
    ("authored", "status", "as_of", "expected"),
    [
        (date(2025, 6, 1), None, RETRO, "yes"),
        (date(2025, 6, 1), "active", RETRO, "yes"),
        (date(2025, 6, 1), "completed", RETRO, "yes"),
        (date(2025, 6, 1), "some-unmapped-status", RETRO, "yes"),
        (date(2025, 6, 1), " Active ", RETRO, "yes"),
        (date(2025, 6, 1), "stopped", RETRO, "no"),
        (date(2025, 6, 1), "cancelled", RETRO, "no"),
        (date(2025, 6, 1), "entered-in-error", RETRO, "no"),
        (date(2024, 6, 1), "active", RETRO, "yes"),  # earlier, still active
        (date(2024, 6, 1), " ACTIVE", RETRO, "yes"),
        (date(2024, 6, 1), None, RETRO, "no"),  # earlier, status unknown
        (date(2024, 6, 1), "completed", RETRO, "no"),
        (date(2024, 6, 1), "stopped", RETRO, "no"),
        (MY_START, None, RETRO, "yes"),  # MY start edge
        (PRIOR_END, None, RETRO, "no"),  # day before MY start
        (RETRO, None, RETRO, "yes"),  # as_of edge
        (DEMO, None, DEMO, "yes"),  # demo anchor: authored on as_of
        (date(2026, 7, 1), None, DEMO, "no"),  # after as_of (unmasked record)
        (date(2026, 1, 1), None, DEMO, "yes"),
        (date(2025, 12, 31), "active", DEMO, "yes"),
    ],
)
def test_numerator_statin_status_paths(
    authored: date, status: str | None, as_of: date, expected: Tri
) -> None:
    out = evaluate(diabetic().medication(ATORVA_80, authored, status=status), as_of)
    assert out.numerator.value == expected
    assert out.numerator.subtype is None
    if expected == "yes":
        assert [e.event_id for e in out.numerator.evidence] == ["med-1"]
        assert all(
            e.role == "numerator" and e.section == "medications" for e in out.numerator.evidence
        )
        assert out.numerator.reasons == [
            "statin of any intensity on therapy in the measurement year"
        ]
    else:
        assert out.numerator.evidence == []
        assert out.numerator.reasons == ["no statin therapy in the measurement year"]


@pytest.mark.parametrize("as_of", [RETRO, DEMO])
def test_numerator_window_ends_at_as_of(as_of: date) -> None:
    out = evaluate(diabetic(), as_of)
    assert (out.numerator.window_start, out.numerator.window_end) == (date(as_of.year, 1, 1), as_of)


def test_numerator_low_intensity_counts_unlike_spc() -> None:
    rf = diabetic().condition(MI, date(2019, 1, 1)).medication(SIMVA_10, date(2025, 3, 3))
    record, ctx = rf.build(), ctx_for(rf)
    spd = SpdRule().evaluate(record, ctx, VS).numerator
    spc = SpcRule().evaluate(record, ctx, VS).numerator
    assert spd.value == "yes"
    assert [e.event_id for e in spd.evidence] == ["med-1"]
    assert (spc.value, spc.subtype) == ("no", "low_intensity_only")


def test_numerator_unknown_intensity_counts_unlike_spc() -> None:
    rf = (
        diabetic()
        .condition(MI, date(2019, 1, 1))
        .medication(ATORVA_20_NO_INTENSITY, date(2025, 3, 3))
    )
    record, ctx = rf.build(), ctx_for(rf)
    assert SpdRule().evaluate(record, ctx, VS).numerator.value == "yes"
    assert SpcRule().evaluate(record, ctx, VS).numerator.value == "unknown"


def test_numerator_non_statin_or_wrong_system_never_counts() -> None:
    rf = (
        diabetic()
        .medication(METFORMIN, date(2025, 3, 3))
        .medication(ATORVA_80, date(2025, 3, 3), system="NDC")
    )
    assert evaluate(rf).numerator.value == "no"


def test_numerator_evidence_lists_only_counted_statins_in_stable_order() -> None:
    rf = (
        diabetic()
        .medication(ATORVA_80, date(2025, 9, 1))
        .medication(SIMVA_10, date(2025, 2, 1), status="stopped")
        .medication(SIMVA_10, date(2024, 2, 1), status="active")
        .medication(ATORVA_80, date(2023, 2, 1))
    )
    out = evaluate(rf)
    assert out.numerator.value == "yes"
    assert [e.event_id for e in out.numerator.evidence] == ["med-3", "med-1"]


# --- exclusions: ESRD / dialysis in MY or prior year -----------------------------------------


@pytest.mark.parametrize(
    ("onset", "abatement", "hit"),
    [
        (PRIOR_START, None, True),
        (date(2023, 12, 31), None, True),  # onset earlier, never abated -> active in window
        (date(2010, 1, 1), date(2023, 12, 31), False),  # abated before the window
        (date(2010, 1, 1), PRIOR_START, False),  # abated ON the window start: not active
        (date(2010, 1, 1), date(2024, 1, 2), True),  # abated the day after the window start
        (MY_END, None, True),
        (date(2026, 1, 1), None, False),  # onset after the MY end (unmasked record)
    ],
)
def test_exclusion_esrd_window(onset: date, abatement: date | None, hit: bool) -> None:
    out = evaluate(diabetic().condition(ESRD, onset, abatement=abatement))
    assert [h.category for h in out.exclusions] == (["esrd"] if hit else [])
    if hit:
        (esrd,) = out.exclusions
        assert esrd.source == "demo_choice"  # window wider than the quoted D12 text
        assert esrd.window_label == "MY or prior year"
        assert [(e.event_id, e.role) for e in esrd.evidence] == [("cond-2", "exclusion")]


@pytest.mark.parametrize(
    ("performed", "hit"),
    [
        (date(2023, 12, 31), False),
        (PRIOR_START, True),
        (date(2025, 6, 1), True),
        (MY_END, True),
        (date(2026, 1, 1), False),
    ],
)
def test_exclusion_dialysis_window(performed: date, hit: bool) -> None:
    out = evaluate(diabetic().procedure(DIALYSIS, performed))
    assert [h.category for h in out.exclusions] == (["dialysis"] if hit else [])
    if hit:
        (dialysis,) = out.exclusions
        assert dialysis.source == "demo_choice"  # window wider than the quoted D12 text
        assert dialysis.window_label == "MY or prior year"
        assert [(e.event_id, e.section, e.role) for e in dialysis.evidence] == [
            ("proc-1", "procedures", "exclusion")
        ]


def test_exclusion_both_esrd_and_dialysis_ordered() -> None:
    out = evaluate(
        diabetic().procedure(DIALYSIS, date(2025, 2, 2)).condition(ESRD, date(2024, 2, 2))
    )
    assert [h.category for h in out.exclusions] == ["esrd", "dialysis"]


@pytest.mark.parametrize(
    ("onset", "abatement", "hit"),
    [
        (date(2025, 3, 1), None, True),  # in the MY
        (date(2024, 2, 1), date(2024, 10, 1), True),  # entirely in the prior year
        (date(2023, 2, 1), date(2023, 10, 1), False),  # before the window
        (date(2023, 2, 1), PRIOR_START, False),  # abated ON the window start
        (date(2026, 1, 1), None, False),  # after the MY end (unmasked record)
    ],
)
def test_exclusion_pregnancy_condition_mirrors_spc(
    onset: date, abatement: date | None, hit: bool
) -> None:
    """Pregnancy in the MY or the prior year excludes (demo_choice, mirroring SPC)."""
    out = evaluate(diabetic(sex="female").condition(PREGNANCY, onset, abatement=abatement))
    assert [(h.category, h.source, h.window_label) for h in out.exclusions] == (
        [("pregnancy", "demo_choice", "MY or prior year")] if hit else []
    )
    assert COVERAGE["pregnancy"] == "observable"


@pytest.mark.parametrize(
    ("effective", "value_code", "hit"),
    [
        (date(2025, 4, 1), PREGNANT, True),
        (PRIOR_START, PREGNANT, True),
        (date(2023, 12, 31), PREGNANT, False),
        (date(2025, 4, 1), "60001007", False),  # "not pregnant"
        (date(2025, 4, 1), None, False),
    ],
)
def test_exclusion_pregnancy_status_observation_counts_for_spd_and_spc(
    effective: date, value_code: str | None, hit: bool
) -> None:
    rf = diabetic(sex="female").observation(
        PREGNANCY_STATUS_LOINC, effective, value_code=value_code
    )
    out = evaluate(rf)
    expected = [("pregnancy_status_positive", "demo_choice", "MY or prior year")] if hit else []
    assert [(h.category, h.source, h.window_label) for h in out.exclusions] == expected
    # SPC applies the very same helper over the same window.
    spc = SpcRule().evaluate(rf.condition(MI, date(2019, 1, 1)).build(), ctx_for(rf), VS)
    assert [(h.category, h.source, h.window_label) for h in spc.exclusions] == expected


# --- escalations: E3 as SPC ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("authored", "status", "raised"),
    [
        (date(2025, 6, 1), "stopped", True),
        (date(2025, 6, 1), "cancelled", True),
        (date(2025, 6, 1), "Stopped ", True),
        (date(2025, 6, 1), "entered-in-error", False),
        (date(2025, 6, 1), "active", False),
        (date(2024, 6, 1), "stopped", False),  # prior year: not a MY conflict
        (date(2026, 1, 1), "stopped", False),  # after as_of (unmasked record)
    ],
)
def test_escalation_e3_statin_status_conflict(authored: date, status: str, raised: bool) -> None:
    out = evaluate(diabetic().medication(ATORVA_80, authored, status=status))
    assert [f.kind for f in out.escalations] == (["E3"] if raised else [])
    if raised:
        (flag,) = out.escalations
        assert flag.scope == "measure"
        assert flag.reason.startswith("medication_status_conflict")
        assert [(e.event_id, e.role) for e in flag.evidence] == [("med-1", "escalation")]


def test_escalation_e3_raised_even_when_another_statin_closes() -> None:
    rf = (
        diabetic()
        .medication(ATORVA_80, date(2025, 2, 1), status="stopped")
        .medication(SIMVA_10, date(2025, 8, 1), status="active")
    )
    out = evaluate(rf)
    assert out.numerator.value == "yes"
    assert [f.kind for f in out.escalations] == ["E3"]
    assert engine_verdict(rf) == "needs_review"


def test_no_measure_scoped_escalations_other_than_e3() -> None:
    rf = (
        diabetic(birth_date=date(1950, 1, 1))
        .condition(DEMENTIA, date(2020, 1, 1))
        .encounter(date(2025, 3, 1), encounter_class="IMP")
        .procedure(HOSPICE, date(2024, 12, 1))
    )
    assert evaluate(rf).escalations == []  # E1 / E4 are global, never re-implemented here


# --- coverage --------------------------------------------------------------------------------


def test_coverage_table_lists_every_public_criterion() -> None:
    assert set(COVERAGE) == {
        "died_during_measurement_period",
        "hospice_during_measurement_period",
        "esrd",
        "dialysis",
        "pregnancy",
        "in_vitro_fertilization",
        "clomiphene_dispensed",
        "cirrhosis",
        "myalgia_myositis_myopathy_rhabdomyolysis",
        "palliative_care",
        "frailty_and_advanced_illness_66_plus",
        "institutional_snp_or_long_term_institution_66_plus",
        "diabetes_medication_only_with_pcos_gestational_or_steroid_induced",
    }
    allowed = set(get_args(Coverage))
    assert set(COVERAGE.values()) <= allowed
    assert COVERAGE["esrd"] == COVERAGE["dialysis"] == "observable"
    assert COVERAGE["frailty_and_advanced_illness_66_plus"] == "partial"
    assert not set(NON_EXCLUSIONS) & set(COVERAGE)


def test_coverage_output_is_a_copy() -> None:
    out = evaluate(diabetic())
    assert out.coverage == COVERAGE
    out.coverage["esrd"] = "partial"
    assert COVERAGE["esrd"] == "observable"


# --- determinism / purity --------------------------------------------------------------------


def _rich() -> RecordFactory:
    return (
        diabetic()
        .condition(PREDIABETES, date(2018, 1, 1))
        .condition(T2DM, date(2024, 6, 1), abatement=date(2024, 9, 1))
        .condition(ESRD, date(2024, 5, 1))
        .procedure(DIALYSIS, date(2025, 4, 1))
        .procedure(DIALYSIS, date(2023, 4, 1))
        .medication(ATORVA_80, date(2025, 9, 1))
        .medication(SIMVA_10, date(2025, 2, 1), status="stopped")
        .medication(SIMVA_10, date(2024, 2, 1), status="active")
        .medication(ATORVA_20_NO_INTENSITY, date(2025, 5, 1), status="cancelled")
        .medication(METFORMIN, date(2025, 5, 1))
        .encounter(date(2025, 3, 1), encounter_class="IMP")
    )


@pytest.mark.parametrize("seed", [1, 7, 42, 2025])
def test_order_independent_over_shuffled_events(seed: int) -> None:
    rf = _rich()
    baseline = evaluate(rf)
    rng = random.Random(seed)
    for events in (rf.conditions, rf.procedures, rf.medications, rf.encounters):
        rng.shuffle(events)
    assert evaluate(rf) == baseline
    assert baseline.denominator.value == "yes"
    assert baseline.numerator.value == "yes"
    assert [h.category for h in baseline.exclusions] == ["esrd", "dialysis"]
    assert [f.kind for f in baseline.escalations] == ["E3"]


def test_pure_repeated_evaluation_is_identical() -> None:
    rf = _rich()
    record, ctx = rf.build(), ctx_for(rf)
    rule = SpdRule()
    first = rule.evaluate(record, ctx, VS)
    second = rule.evaluate(record, ctx, VS)
    assert first == second
    assert record == rf.build()


# --- engine-level verdicts -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rf", "verdict"),
    [
        pytest.param(diabetic().medication(ATORVA_80, date(2025, 6, 1)), "closed", id="closed"),
        pytest.param(diabetic(), "gap_open", id="gap_open"),
        pytest.param(diabetic().condition(ESRD, date(2024, 6, 1)), "excluded", id="esrd"),
        pytest.param(diabetic().procedure(DIALYSIS, date(2025, 6, 1)), "excluded", id="dialysis"),
        pytest.param(
            diabetic().condition(MI, date(2019, 1, 1)), "not_eligible", id="routed_to_spc"
        ),
        pytest.param(RecordFactory(), "not_eligible", id="no_diabetes"),
        pytest.param(diabetic(birth_date=date(1986, 1, 1)), "not_eligible", id="age_39"),
        pytest.param(diabetic(birth_date=None), "needs_review", id="birth_unknown"),
        pytest.param(
            diabetic().medication(ATORVA_80, date(2025, 6, 1), status="stopped"),
            "needs_review",
            id="e3",
        ),
        pytest.param(diabetic(death_date=date(2024, 1, 1)), "not_eligible", id="died_before_my"),
        pytest.param(diabetic(death_date=date(2025, 1, 1)), "excluded", id="died_in_my"),
        pytest.param(
            diabetic().procedure(HOSPICE, date(2025, 1, 1)), "excluded", id="hospice_in_my"
        ),
        pytest.param(
            diabetic().procedure(HOSPICE, date(2024, 12, 1)), "needs_review", id="hospice_e1"
        ),
        pytest.param(
            diabetic().procedure(HOSPICE, date(2024, 9, 1)),
            "gap_open",
            id="hospice_before_lookback",
        ),
    ],
)
def test_engine_verdicts(rf: RecordFactory, verdict: str) -> None:
    assert engine_verdict(rf) == verdict


def test_engine_priority_scored_only_for_candidates() -> None:
    engine = MeasureEngine([SpdRule()], value_sets=VS)
    open_rf = diabetic()
    closed_rf = diabetic().medication(ATORVA_80, date(2025, 6, 1))
    gap = engine.evaluate_one(open_rf.build(), ctx_for(open_rf), "SPD")
    closed = engine.evaluate_one(closed_rf.build(), ctx_for(closed_rf), "SPD")
    assert gap.is_candidate and gap.priority_score > 0
    assert not closed.is_candidate and closed.priority_score == 0.0
