"""Table-driven tests for the CBP rule (C14 Controlling High Blood Pressure).

Records are built inline with the P6 models through ``RecordFactory``; value sets are built
inline with exactly the codes the rule needs (no dependency on the package JSON files).
Covers SPEC section 9 edges: window edges, 139/89 vs 140/90, missing DBP, wrong unit,
same-day panels, IMP/EMER ignored, traps, E5/E6, order independence, engine verdicts.
"""

import ast
import inspect
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import TypeVar

import pytest

from caregap.measures.context import MeasurementContext
from caregap.measures.engine import MeasureEngine, MeasureRule, RuleOutput
from caregap.measures.models import Coverage
from caregap.measures.rules import cbp as cbp_module
from caregap.measures.rules.cbp import (
    BP_UNIT,
    COVERAGE,
    DBP_CODE,
    NO_BP_IN_MY,
    PANEL_CODE,
    PREGNANCY_STATUS_LOINC,
    SBP_CODE,
    CbpRule,
)
from caregap.measures.value_sets import ValueSet, ValueSetCode, ValueSets
from caregap.p6.models import (
    ConditionEvent,
    EncounterEvent,
    MedicationEvent,
    ObservationEvent,
    PatientHeader,
    PatientRecord,
    ProcedureEvent,
)

AS_OF = date(2025, 12, 31)  # retrospective: complete MY2025
MID_YEAR = date(2025, 6, 30)  # prospective demo-style anchor
BIRTH_50 = date(1975, 6, 15)

HTN = "59621000"
ESRD = "46177005"
DIALYSIS = "265764009"
TRANSPLANT = "70536003"
PREGNANCY = "72892002"
PREGNANCY_TRAP = "169560008"  # a pregnancy-adjacent code deliberately NOT in pregnancy_snomed
PREGNANT = "77386006"
NOT_PREGNANT = "60001007"
HOSPICE = "385763009"
DEMENTIA = "26929004"
DONEPEZIL = "310436"

T = TypeVar("T")
Builder = Callable[["RecordFactory"], "RecordFactory"]


def _set(set_id: str, system: str, codes: list[str]) -> ValueSet:
    return ValueSet(
        id=set_id,
        code_system=system,
        source="synthea-scan",
        codes=[ValueSetCode(code=c) for c in codes],
    )


VALUE_SETS = ValueSets(
    version="test",
    sets={
        "hypertension_snomed": _set("hypertension_snomed", "SNOMED", [HTN]),
        "bp_loinc": _set("bp_loinc", "LOINC", [PANEL_CODE, SBP_CODE, DBP_CODE]),
        "esrd_snomed": _set("esrd_snomed", "SNOMED", [ESRD]),
        "dialysis_snomed": _set("dialysis_snomed", "SNOMED", [DIALYSIS]),
        "kidney_transplant_snomed": _set("kidney_transplant_snomed", "SNOMED", [TRANSPLANT]),
        "pregnancy_snomed": _set("pregnancy_snomed", "SNOMED", [PREGNANCY]),
        "pregnancy_trap_snomed": _set("pregnancy_trap_snomed", "SNOMED", [PREGNANCY_TRAP]),
        # Read by the GLOBAL rules only (engine integration tests).
        "hospice_snomed": _set("hospice_snomed", "SNOMED", [HOSPICE]),
        "dementia_snomed": _set("dementia_snomed", "SNOMED", [DEMENTIA]),
        "dementia_meds_rxnorm": _set("dementia_meds_rxnorm", "RXNORM", [DONEPEZIL]),
    },
)


@dataclass
class RecordFactory:
    """Builds ``PatientRecord`` objects inline with the P6 models (chainable)."""

    birth_date: date | None = BIRTH_50
    sex: str = "female"
    death_date: date | None = None
    conditions: list[ConditionEvent] = field(default_factory=list)
    observations: list[ObservationEvent] = field(default_factory=list)
    procedures: list[ProcedureEvent] = field(default_factory=list)
    medications: list[MedicationEvent] = field(default_factory=list)
    encounters: list[EncounterEvent] = field(default_factory=list)
    seq: int = 0

    def next_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}-{self.seq}"

    def condition(
        self, code: str, onset: date, abatement: date | None = None, *, system: str = "SNOMED"
    ) -> "RecordFactory":
        self.conditions.append(
            ConditionEvent(
                condition_id=self.next_id("cond"),
                code=code,
                code_system=system,
                onset_date=onset,
                abatement_date=abatement,
            )
        )
        return self

    def htn(self, onset: date = date(2020, 1, 1), abatement: date | None = None) -> "RecordFactory":
        return self.condition(HTN, onset, abatement)

    def procedure(self, code: str, day: date, *, system: str = "SNOMED") -> "RecordFactory":
        self.procedures.append(
            ProcedureEvent(
                procedure_id=self.next_id("proc"), code=code, code_system=system, performed_date=day
            )
        )
        return self

    def medication(self, code: str, day: date, *, status: str | None) -> "RecordFactory":
        self.medications.append(
            MedicationEvent(
                medication_request_id=self.next_id("med"),
                code=code,
                code_system="RXNORM",
                authored_date=day,
                status=status,
            )
        )
        return self

    def encounter(self, encounter_id: str, encounter_class: str, day: date) -> "RecordFactory":
        self.encounters.append(
            EncounterEvent(
                encounter_id=encounter_id, encounter_class=encounter_class, start_date=day
            )
        )
        return self

    def observation(
        self,
        code: str,
        day: date,
        *,
        system: str = "LOINC",
        observation_id: str | None = None,
        parent: str | None = None,
        value_num: float | None = None,
        value_unit: str | None = None,
        value_code: str | None = None,
        encounter_id: str | None = None,
    ) -> "RecordFactory":
        self.observations.append(
            ObservationEvent(
                observation_id=observation_id or self.next_id("obs"),
                parent_observation_id=parent,
                code=code,
                code_system=system,
                effective_date=day,
                value_num=value_num,
                value_unit=value_unit,
                value_code=value_code,
                value_code_system="SNOMED" if value_code else None,
                encounter_id=encounter_id,
            )
        )
        return self

    def bp(
        self,
        day: date,
        sbp: float | None,
        dbp: float | None,
        *,
        enc_class: str | None = None,
        encounter_id: str | None = None,
        sbp_unit: str | None = BP_UNIT,
        dbp_unit: str | None = BP_UNIT,
        panel_id: str | None = None,
        system: str = "LOINC",
    ) -> "RecordFactory":
        """A P6-shaped panel: parent 85354-9 + ``<pid>#8480-6`` / ``<pid>#8462-4`` children.

        ``sbp`` / ``dbp`` None omits that component; ``enc_class`` None leaves the parent
        without an encounter unless ``encounter_id`` names one (possibly a ghost).
        """
        pid = panel_id or self.next_id("panel")
        enc = encounter_id
        if enc_class is not None:
            enc = enc or self.next_id("enc")
            self.encounter(enc, enc_class, day)
        self.observation(PANEL_CODE, day, system=system, observation_id=pid, encounter_id=enc)
        if sbp is not None:
            self.observation(
                SBP_CODE,
                day,
                observation_id=f"{pid}#{SBP_CODE}",
                parent=pid,
                value_num=sbp,
                value_unit=sbp_unit,
                encounter_id=enc,
            )
        if dbp is not None:
            self.observation(
                DBP_CODE,
                day,
                observation_id=f"{pid}#{DBP_CODE}",
                parent=pid,
                value_num=dbp,
                value_unit=dbp_unit,
                encounter_id=enc,
            )
        return self

    def record(self, as_of: date = AS_OF) -> PatientRecord:
        return PatientRecord(
            patient=PatientHeader(
                patient_id="p1",
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


def _ctx(record: PatientRecord) -> MeasurementContext:
    return MeasurementContext.for_(record.as_of, record.patient.birth_date)


def evaluate(factory: RecordFactory, as_of: date = AS_OF) -> RuleOutput:
    record = factory.record(as_of)
    return CbpRule().evaluate(record, _ctx(record), VALUE_SETS)


def _e5(out: RuleOutput) -> bool:
    return any(e.kind == "E5" for e in out.escalations)


def _kinds(out: RuleOutput) -> set[str]:
    return {e.kind for e in out.escalations}


# --- contract -------------------------------------------------------------------------------


def test_rule_identity_matches_registry_contract() -> None:
    rule: MeasureRule = CbpRule()  # structural conformance is checked by mypy
    assert rule.measure_id == "CBP"
    assert rule.rule_version == "cbp-v1"


def test_rule_never_reads_status_fields_or_features() -> None:
    tree = ast.parse(inspect.getsource(cbp_module))
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attributes & {"status", "clinical_status", "verification_status"}
    imported = {
        alias.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for alias in n.names
    }
    assert "FeatureRow" not in imported


# --- denominator ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("birth", "expected"),
    [
        pytest.param(date(2007, 12, 31), "yes", id="age_18_at_dec_31"),
        pytest.param(date(2008, 1, 1), "no", id="age_17_at_dec_31"),
        pytest.param(date(1940, 1, 1), "yes", id="age_85_at_dec_31"),
        pytest.param(date(1939, 12, 31), "no", id="age_86_at_dec_31"),
        pytest.param(None, "unknown", id="birth_date_unknown"),
    ],
)
def test_denominator_age_band(birth: date | None, expected: str) -> None:
    out = evaluate(RecordFactory(birth_date=birth).htn())
    assert out.denominator.value == expected
    if expected == "unknown":
        assert "birth_date unknown" in out.denominator.reasons


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        pytest.param(lambda f: f.htn(), "yes", id="active_no_abatement"),
        pytest.param(lambda f: f.htn(onset=date(2025, 12, 31)), "yes", id="onset_on_my_end"),
        pytest.param(lambda f: f.htn(onset=date(2026, 1, 1)), "no", id="onset_after_my_end"),
        pytest.param(
            lambda f: f.htn(abatement=date(2024, 12, 31)), "no", id="abated_before_my_start"
        ),
        pytest.param(lambda f: f.htn(abatement=date(2025, 1, 1)), "no", id="abated_on_my_start"),
        pytest.param(
            lambda f: f.htn(abatement=date(2025, 1, 2)), "yes", id="abated_day_after_my_start"
        ),
        pytest.param(lambda f: f, "no", id="no_hypertension"),
        pytest.param(
            lambda f: f.condition(HTN, date(2020, 1, 1), system="ICD10"),
            "no",
            id="hypertension_wrong_code_system",
        ),
        pytest.param(
            lambda f: f.htn(abatement=date(2024, 6, 1)).htn(onset=date(2025, 3, 1)),
            "yes",
            id="one_abated_one_active",
        ),
    ],
)
def test_denominator_hypertension(build: Builder, expected: str) -> None:
    out = evaluate(build(RecordFactory()))
    assert out.denominator.value == expected
    assert out.denominator.window_start == date(2025, 1, 1)
    assert out.denominator.window_end == date(2025, 12, 31)
    if expected == "yes":
        assert any(e.section == "conditions" and e.code == HTN for e in out.denominator.evidence)


def test_denominator_evidence_carries_patient_and_condition_refs() -> None:
    out = evaluate(RecordFactory().htn())
    sections = [e.section for e in out.denominator.evidence]
    assert sections == ["patient", "conditions"]
    assert all(e.role == "eligibility" for e in out.denominator.evidence)


def test_died_before_my_is_not_eligible() -> None:
    out = evaluate(RecordFactory(death_date=date(2024, 12, 31)).htn())
    assert out.denominator.value == "no"
    assert "died before the measurement year" in out.denominator.reasons


def test_death_and_hospice_in_my_are_global_not_rule_exclusions() -> None:
    out = evaluate(RecordFactory(death_date=date(2025, 6, 1)).htn().procedure(HOSPICE, MID_YEAR))
    assert out.denominator.value == "yes"
    assert out.exclusions == []
    assert not _kinds(out) & {"E1", "E4"}


# --- numerator ------------------------------------------------------------------------------

JUN = date(2025, 6, 1)
DEC = date(2025, 12, 1)

NUMERATOR_CASES: list[tuple[str, Builder, str, str | None, bool]] = [
    ("no_panels", lambda f: f, "no", NO_BP_IN_MY, False),
    ("139_89_controlled", lambda f: f.bp(JUN, 139, 89), "yes", None, False),
    ("140_89_not_controlled", lambda f: f.bp(JUN, 140, 89), "no", None, False),
    ("139_90_not_controlled", lambda f: f.bp(JUN, 139, 90), "no", None, False),
    ("140_90_not_controlled", lambda f: f.bp(JUN, 140, 90), "no", None, False),
    ("139_5_89_5_float_controlled", lambda f: f.bp(JUN, 139.5, 89.5), "yes", None, False),
    ("missing_dbp", lambda f: f.bp(JUN, 130, None), "unknown", None, True),
    ("missing_sbp", lambda f: f.bp(JUN, None, 80), "unknown", None, True),
    ("parent_without_children", lambda f: f.bp(JUN, None, None), "unknown", None, True),
    ("sbp_wrong_unit", lambda f: f.bp(JUN, 130, 80, sbp_unit="mmHg"), "unknown", None, True),
    ("dbp_wrong_unit", lambda f: f.bp(JUN, 130, 80, dbp_unit="mm Hg"), "unknown", None, True),
    ("dbp_unit_missing", lambda f: f.bp(JUN, 130, 80, dbp_unit=None), "unknown", None, True),
    (
        "same_day_lowest_panel_wins",
        lambda f: f.bp(JUN, 150, 95).bp(JUN, 130, 85),
        "yes",
        None,
        False,
    ),
    (
        "same_day_lowest_sbp_and_lowest_dbp_across_panels",
        lambda f: f.bp(JUN, 130, 95).bp(JUN, 150, 80),
        "yes",
        None,
        False,
    ),
    (
        "same_day_all_uncontrolled",
        lambda f: f.bp(JUN, 150, 95).bp(JUN, 145, 92),
        "no",
        None,
        False,
    ),
    (
        "most_recent_date_wins_open",
        lambda f: f.bp(JUN, 130, 80).bp(DEC, 150, 95),
        "no",
        None,
        False,
    ),
    (
        "most_recent_date_wins_closed",
        lambda f: f.bp(JUN, 150, 95).bp(DEC, 130, 80),
        "yes",
        None,
        False,
    ),
    (
        "imp_latest_ignored_earlier_amb_counts",
        lambda f: f.bp(JUN, 130, 80, enc_class="AMB").bp(DEC, 150, 95, enc_class="IMP"),
        "yes",
        None,
        False,
    ),
    (
        "emer_latest_ignored_earlier_null_encounter_counts",
        lambda f: f.bp(JUN, 130, 80).bp(DEC, 150, 95, enc_class="EMER"),
        "yes",
        None,
        False,
    ),
    (
        "emer_only_is_no_bp",
        lambda f: f.bp(JUN, 130, 80, enc_class="EMER"),
        "no",
        NO_BP_IN_MY,
        False,
    ),
    ("imp_only_is_no_bp", lambda f: f.bp(JUN, 130, 80, enc_class="IMP"), "no", NO_BP_IN_MY, False),
    ("amb_encounter_counts", lambda f: f.bp(JUN, 130, 80, enc_class="AMB"), "yes", None, False),
    ("hh_encounter_counts", lambda f: f.bp(JUN, 130, 80, enc_class="HH"), "yes", None, False),
    (
        "ghost_encounter_id_counts",
        lambda f: f.bp(JUN, 130, 80, encounter_id="ghost"),
        "yes",
        None,
        False,
    ),
    (
        "imp_defective_latest_ignored",
        lambda f: f.bp(JUN, 130, 80).bp(DEC, None, 95, enc_class="IMP"),
        "yes",
        None,
        False,
    ),
    (
        "panel_before_my_start",
        lambda f: f.bp(date(2024, 12, 31), 130, 80),
        "no",
        NO_BP_IN_MY,
        False,
    ),
    ("panel_on_my_start", lambda f: f.bp(date(2025, 1, 1), 130, 80), "yes", None, False),
    ("panel_on_as_of", lambda f: f.bp(AS_OF, 130, 80), "yes", None, False),
    (
        "defective_earlier_complete_latest",
        lambda f: f.bp(JUN, 130, None).bp(DEC, 130, 80),
        "yes",
        None,
        False,
    ),
    (
        "defective_latest_complete_earlier",
        lambda f: f.bp(JUN, 130, 80).bp(DEC, 130, None),
        "unknown",
        None,
        True,
    ),
    (
        "complete_and_defective_on_latest_date",
        lambda f: f.bp(DEC, 130, 80).bp(DEC, 130, 80, dbp_unit="mmHg"),
        "unknown",
        None,
        True,
    ),
    (
        "child_value_missing",
        lambda f: f.bp(JUN, 130, None).observation(
            DBP_CODE, JUN, observation_id="panel-1#dbp", parent="panel-1", value_unit=BP_UNIT
        ),
        "unknown",
        None,
        True,
    ),
    (
        "orphan_child_does_not_complete_panel",
        lambda f: f.bp(JUN, 130, None).observation(
            DBP_CODE, JUN, parent=None, value_num=80, value_unit=BP_UNIT
        ),
        "unknown",
        None,
        True,
    ),
    (
        "child_pointing_at_other_parent",
        lambda f: f.bp(JUN, 130, None).observation(
            DBP_CODE, JUN, parent="someone-else", value_num=80, value_unit=BP_UNIT
        ),
        "unknown",
        None,
        True,
    ),
    (
        "orphan_components_without_parent_are_no_bp",
        lambda f: f.observation(SBP_CODE, JUN, value_num=130, value_unit=BP_UNIT).observation(
            DBP_CODE, JUN, value_num=80, value_unit=BP_UNIT
        ),
        "no",
        NO_BP_IN_MY,
        False,
    ),
    (
        "panel_in_wrong_code_system_ignored",
        lambda f: f.bp(JUN, 130, 80, system="SNOMED"),
        "no",
        NO_BP_IN_MY,
        False,
    ),
]


@pytest.mark.parametrize(
    ("build", "expected", "subtype", "e5"),
    [pytest.param(b, v, s, e, id=name) for name, b, v, s, e in NUMERATOR_CASES],
)
def test_numerator(build: Builder, expected: str, subtype: str | None, e5: bool) -> None:
    out = evaluate(build(RecordFactory().htn()))
    assert out.numerator.value == expected
    assert out.numerator.subtype == subtype
    assert _e5(out) is e5
    assert out.numerator.window_start == date(2025, 1, 1)
    assert out.numerator.window_end == AS_OF
    if expected in {"yes", "unknown"}:
        assert out.numerator.evidence, "yes/unknown numerators carry evidence"
    if subtype == NO_BP_IN_MY:
        assert out.numerator.evidence == []


def test_numerator_window_ends_at_as_of_not_my_end() -> None:
    out = evaluate(RecordFactory().htn().bp(date(2025, 7, 1), 130, 80), as_of=MID_YEAR)
    assert out.numerator.value == "no"
    assert out.numerator.subtype == NO_BP_IN_MY
    assert out.numerator.window_end == MID_YEAR
    out = evaluate(RecordFactory().htn().bp(MID_YEAR, 130, 80), as_of=MID_YEAR)
    assert out.numerator.value == "yes"


def test_representative_bp_reason_and_evidence() -> None:
    out = evaluate(
        RecordFactory().htn().bp(JUN, 130, 95, panel_id="a").bp(JUN, 150, 80, panel_id="b")
    )
    assert out.numerator.value == "yes"
    assert "130/80" in out.numerator.reasons[0]
    ids = [e.event_id for e in out.numerator.evidence]
    assert ids == ["a", "b", f"a#{SBP_CODE}", f"b#{DBP_CODE}"]
    assert all(
        e.role == "numerator" and e.section == "observations" for e in out.numerator.evidence
    )


def test_ignored_acute_panels_are_reported_in_reasons() -> None:
    out = evaluate(RecordFactory().htn().bp(JUN, 130, 80).bp(DEC, 150, 95, enc_class="EMER"))
    assert any("1 panel(s) at IMP/EMER encounters ignored" in r for r in out.numerator.reasons)
    out = evaluate(RecordFactory().htn().bp(DEC, 150, 95, enc_class="IMP"))
    assert out.numerator.subtype == NO_BP_IN_MY
    assert any("ignored" in r for r in out.numerator.reasons)


def test_e5_flag_shape() -> None:
    out = evaluate(RecordFactory().htn().bp(DEC, 130, None, panel_id="p"))
    (flag,) = out.escalations
    assert flag.kind == "E5"
    assert flag.scope == "measure"
    assert "2025-12-01" in flag.reason
    assert "missing DBP component" in flag.reason
    assert [e.event_id for e in flag.evidence] == ["p", f"p#{SBP_CODE}"]
    assert all(e.role == "escalation" for e in flag.evidence)
    assert [e.event_id for e in out.numerator.evidence] == ["p", f"p#{SBP_CODE}"]
    assert all(e.role == "numerator" for e in out.numerator.evidence)


def test_e5_reason_names_the_unit() -> None:
    out = evaluate(RecordFactory().htn().bp(DEC, 130, 80, sbp_unit="mmHg"))
    (flag,) = out.escalations
    assert "SBP unit 'mmHg' is not 'mm[Hg]'" in flag.reason


# --- coded exclusions -----------------------------------------------------------------------

ANY_TIME = "any time through as_of"


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        pytest.param(lambda f: f, [], id="none"),
        pytest.param(
            lambda f: f.condition(ESRD, date(1990, 1, 1)),
            [("esrd", "quoted", ANY_TIME)],
            id="esrd_condition_any_time",
        ),
        pytest.param(
            lambda f: f.condition(ESRD, date(1990, 1, 1), abatement=date(1995, 1, 1)),
            [("esrd", "quoted", ANY_TIME)],
            id="esrd_abated_still_counts",
        ),
        pytest.param(
            lambda f: f.procedure(DIALYSIS, date(2010, 5, 5)),
            [("dialysis", "demo_choice", ANY_TIME)],
            id="dialysis_procedure_any_time",
        ),
        pytest.param(
            lambda f: f.condition(DIALYSIS, date(2010, 5, 5)),
            [],
            id="dialysis_condition_is_not_the_procedure_criterion",
        ),
        pytest.param(
            lambda f: f.procedure(TRANSPLANT, date(2015, 1, 1)),
            [("kidney_transplant", "demo_choice", ANY_TIME)],
            id="kidney_transplant_procedure",
        ),
        pytest.param(
            lambda f: f.condition(TRANSPLANT, date(2015, 1, 1)),
            [("kidney_transplant", "demo_choice", ANY_TIME)],
            id="kidney_transplant_condition",
        ),
        pytest.param(
            lambda f: f.condition(PREGNANCY, date(2025, 3, 1)),
            [("pregnancy", "quoted", "MY")],
            id="pregnancy_condition_in_my",
        ),
        pytest.param(
            lambda f: f.condition(PREGNANCY, date(2024, 10, 1)),
            [("pregnancy", "quoted", "MY")],
            id="pregnancy_condition_from_prior_year_still_active",
        ),
        pytest.param(
            lambda f: f.condition(PREGNANCY, date(2024, 1, 1), abatement=date(2024, 9, 1)),
            [],
            id="pregnancy_condition_abated_before_my",
        ),
        pytest.param(
            lambda f: f.condition(PREGNANCY, date(2024, 4, 1), abatement=date(2025, 1, 1)),
            [("pregnancy", "quoted", "MY")],
            id="pregnancy_condition_abated_on_my_start",
        ),
        pytest.param(
            lambda f: f.condition(PREGNANCY, date(2026, 1, 1)),
            [],
            id="pregnancy_condition_after_my_end",
        ),
        pytest.param(
            lambda f: f.observation(PREGNANCY_STATUS_LOINC, date(2025, 3, 1), value_code=PREGNANT),
            [("pregnancy_status_positive", "demo_choice", "MY")],
            id="pregnancy_status_positive_in_my",
        ),
        pytest.param(
            lambda f: f.observation(
                PREGNANCY_STATUS_LOINC, date(2025, 3, 1), value_code=NOT_PREGNANT
            ),
            [],
            id="pregnancy_status_negative",
        ),
        pytest.param(
            lambda f: f.observation(PREGNANCY_STATUS_LOINC, date(2025, 3, 1)),
            [],
            id="pregnancy_status_without_value",
        ),
        pytest.param(
            lambda f: f.observation(
                PREGNANCY_STATUS_LOINC, date(2024, 12, 31), value_code=PREGNANT
            ),
            [],
            id="pregnancy_status_positive_before_my",
        ),
        pytest.param(
            lambda f: f.condition(PREGNANCY_TRAP, date(2025, 3, 1)),
            [],
            id="pregnancy_trap_code_is_not_pregnancy",
        ),
        pytest.param(
            lambda f: (
                f.condition(ESRD, date(2000, 1, 1))
                .procedure(DIALYSIS, date(2001, 1, 1))
                .condition(PREGNANCY, date(2025, 2, 1))
            ),
            [
                ("esrd", "quoted", ANY_TIME),
                ("dialysis", "demo_choice", ANY_TIME),
                ("pregnancy", "quoted", "MY"),
            ],
            id="multiple_hits_in_stable_order",
        ),
    ],
)
def test_exclusions(build: Builder, expected: list[tuple[str, str, str]]) -> None:
    out = evaluate(build(RecordFactory().htn().bp(JUN, 130, 80)))
    assert [(h.category, h.source, h.window_label) for h in out.exclusions] == expected
    for hit in out.exclusions:
        assert hit.evidence, "every exclusion hit carries evidence"
        assert all(e.role == "exclusion" for e in hit.evidence)


def test_exclusion_windows_end_at_as_of_not_after() -> None:
    out = evaluate(RecordFactory().htn().condition(ESRD, date(2025, 7, 1)), as_of=MID_YEAR)
    assert out.exclusions == []
    out = evaluate(RecordFactory().htn().condition(ESRD, MID_YEAR), as_of=MID_YEAR)
    assert [h.category for h in out.exclusions] == ["esrd"]


# --- escalations ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("abatement", "as_of", "e6", "denominator"),
    [
        pytest.param(date(2025, 6, 1), AS_OF, True, "yes", id="abated_mid_my"),
        pytest.param(date(2025, 1, 1), AS_OF, True, "no", id="abated_on_my_start"),
        pytest.param(date(2025, 1, 2), AS_OF, True, "yes", id="abated_day_after_my_start"),
        pytest.param(AS_OF, AS_OF, True, "yes", id="abated_on_as_of"),
        pytest.param(date(2024, 12, 31), AS_OF, False, "no", id="abated_before_my"),
        pytest.param(date(2025, 9, 1), MID_YEAR, False, "yes", id="abated_after_as_of"),
        pytest.param(None, AS_OF, False, "yes", id="not_abated"),
    ],
)
def test_e6_hypertension_abated_in_my(
    abatement: date | None, as_of: date, e6: bool, denominator: str
) -> None:
    out = evaluate(RecordFactory().htn(abatement=abatement).bp(date(2025, 3, 1), 130, 80), as_of)
    assert ("E6" in _kinds(out)) is e6
    assert out.denominator.value == denominator
    for flag in out.escalations:
        assert flag.kind == "E6"
        assert flag.scope == "measure"
        assert [e.code for e in flag.evidence] == [HTN]
        assert all(e.role == "escalation" for e in flag.evidence)


def test_e5_and_e6_together_and_only_measure_scope() -> None:
    out = evaluate(RecordFactory().htn(abatement=JUN).bp(DEC, 130, None))
    assert [e.kind for e in out.escalations] == ["E5", "E6"]
    assert all(e.scope == "measure" for e in out.escalations)


# --- coverage -------------------------------------------------------------------------------


def test_coverage_lists_every_public_criterion() -> None:
    expected: dict[str, Coverage] = {
        "died_during_measurement_period": "observable",
        "hospice_during_measurement_period": "observable",
        "esrd": "observable",
        "dialysis": "observable",
        "kidney_transplant": "observable",
        "pregnancy_during_measurement_period": "observable",
        "palliative_care": "not_representable",
        "frailty_and_advanced_illness_66_to_80": "partial",
        "frailty_81_plus": "not_representable",
        "institutional_snp_or_long_term_institution_66_plus": "not_representable",
    }
    assert expected == COVERAGE
    out = evaluate(RecordFactory().htn())
    assert out.coverage == expected
    assert out.coverage is not COVERAGE, "rules hand out a copy"


# --- determinism ----------------------------------------------------------------------------


def _shuffled(rng: random.Random, items: list[T]) -> list[T]:
    copy = list(items)
    rng.shuffle(copy)
    return copy


def test_order_independent_over_shuffled_events() -> None:
    factory = (
        RecordFactory()
        .htn(abatement=date(2025, 8, 1))
        .htn(onset=date(2025, 2, 1))
        .condition(ESRD, date(2001, 1, 1))
        .procedure(DIALYSIS, date(2002, 1, 1))
        .procedure(TRANSPLANT, date(2003, 1, 1))
        .condition(PREGNANCY, date(2025, 2, 1))
        .observation(PREGNANCY_STATUS_LOINC, date(2025, 3, 1), value_code=PREGNANT)
        .bp(JUN, 150, 95)
        .bp(DEC, 130, 95, enc_class="AMB")
        .bp(DEC, 150, 80, enc_class="HH")
        .bp(DEC, 100, 60, enc_class="IMP")
        .bp(DEC, 100, 60, enc_class="EMER")
        .bp(date(2025, 11, 1), 130, None)
        .medication("999", JUN, status="stopped")
    )
    record = factory.record()
    ctx = _ctx(record)
    baseline = CbpRule().evaluate(record, ctx, VALUE_SETS)
    assert baseline.numerator.value == "yes"
    assert "130/80" in baseline.numerator.reasons[0]
    assert [h.category for h in baseline.exclusions] == [
        "esrd",
        "dialysis",
        "kidney_transplant",
        "pregnancy",
        "pregnancy_status_positive",
    ]
    assert _kinds(baseline) == {"E6"}
    for seed in range(8):
        rng = random.Random(seed)
        shuffled = record.model_copy(
            update={
                "conditions": _shuffled(rng, record.conditions),
                "observations": _shuffled(rng, record.observations),
                "procedures": _shuffled(rng, record.procedures),
                "medications": _shuffled(rng, record.medications),
                "encounters": _shuffled(rng, record.encounters),
            }
        )
        assert CbpRule().evaluate(shuffled, ctx, VALUE_SETS) == baseline


def test_evaluate_is_pure_and_repeatable() -> None:
    record = RecordFactory().htn().bp(JUN, 130, 80).record()
    ctx = _ctx(record)
    rule = CbpRule()
    first = rule.evaluate(record, ctx, VALUE_SETS)
    second = rule.evaluate(record, ctx, VALUE_SETS)
    assert first == second
    assert record == RecordFactory().htn().bp(JUN, 130, 80).record()


def test_tie_on_lowest_value_is_broken_by_observation_id() -> None:
    out = evaluate(
        RecordFactory().htn().bp(JUN, 130, 80, panel_id="zz").bp(JUN, 130, 80, panel_id="aa")
    )
    ids = [e.event_id for e in out.numerator.evidence]
    assert ids == ["aa", "zz", f"aa#{SBP_CODE}", f"aa#{DBP_CODE}"]


# --- engine integration (verdict algebra + globals + priority) -------------------------------


def _engine_eval(factory: RecordFactory, as_of: date = AS_OF) -> tuple[str, float, set[str]]:
    record = factory.record(as_of)
    ev = MeasureEngine([CbpRule()], VALUE_SETS).evaluate_one(record, _ctx(record), "CBP")
    return ev.verdict, ev.priority_score, {e.kind for e in ev.escalations}


def test_engine_verdicts() -> None:
    assert _engine_eval(RecordFactory().htn().bp(JUN, 139, 89)) == ("closed", 0.0, set())
    assert _engine_eval(RecordFactory().htn().bp(JUN, 140, 90))[0] == "gap_open"
    assert _engine_eval(RecordFactory().htn())[0] == "gap_open"
    assert _engine_eval(RecordFactory().htn().bp(JUN, 130, None)) == (
        "needs_review",
        9.0,
        {"E5"},
    )
    assert _engine_eval(RecordFactory().htn(abatement=JUN).bp(JUN, 130, 80)) == (
        "needs_review",
        9.0,
        {"E6"},
    )
    assert _engine_eval(RecordFactory().htn().condition(ESRD, date(2000, 1, 1))) == (
        "excluded",
        0.0,
        set(),
    )
    assert _engine_eval(RecordFactory(birth_date=date(2008, 1, 1)).htn())[0] == "not_eligible"
    assert _engine_eval(RecordFactory(birth_date=None).htn().bp(JUN, 130, 80))[0] == "needs_review"


def test_engine_applies_global_death_and_hospice_exclusions() -> None:
    assert _engine_eval(RecordFactory(death_date=JUN).htn())[0] == "excluded"
    assert _engine_eval(RecordFactory().htn().procedure(HOSPICE, JUN))[0] == "excluded"
    assert _engine_eval(RecordFactory(death_date=date(2024, 12, 31)).htn())[0] == "not_eligible"


def test_engine_drops_measure_escalations_when_not_eligible() -> None:
    verdict, _, kinds = _engine_eval(RecordFactory().htn(abatement=date(2025, 1, 1)))
    assert verdict == "not_eligible"
    assert kinds == set()


def test_priority_no_bp_in_my_outranks_uncontrolled() -> None:
    _, no_bp, _ = _engine_eval(RecordFactory().htn())
    _, uncontrolled, _ = _engine_eval(RecordFactory().htn().bp(JUN, 150, 95))
    assert no_bp == 12.0  # star 3 x (clinical 3 + 1) x retrospective 1
    assert uncontrolled == 9.0
    _, prospective, _ = _engine_eval(RecordFactory().htn(), as_of=MID_YEAR)
    assert prospective > no_bp
