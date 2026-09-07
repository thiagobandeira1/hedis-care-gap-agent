"""Table-driven tests for the EED rule (C11 Eye Exam for Patients With Diabetes).

Records are built inline with the P6 models through a small ``RecordFactory``; the value sets
are constructed in-test from exactly the codes the rule needs (no dependency on the package
JSON files). Covers SPEC section 9: window edges, the retinal MY-1 branches, traps, E6,
death before / during / after, order independence, and the engine-level verdicts.
"""

import random
from dataclasses import dataclass, field
from datetime import date
from typing import Self

import pytest

from caregap.measures.context import MeasurementContext
from caregap.measures.engine import MeasureEngine, RuleOutput
from caregap.measures.models import Coverage
from caregap.measures.rules.eed import (
    AGE_MAX,
    AGE_MIN,
    COVERAGE,
    NEGATIVE_RETINOPATHY_ANSWER,
    NON_EXCLUSIONS,
    PRIOR_YEAR_EXAM_WITH_RETINOPATHY,
    PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT,
    EedRule,
)
from caregap.measures.tri import Tri
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

# --- anchors -------------------------------------------------------------------------------

RETRO = date(2025, 12, 31)  # retrospective: as_of == my_end
MID = date(2025, 6, 30)  # prospective: as_of mid-year
MY_START = date(2025, 1, 1)
MY_END = date(2025, 12, 31)
PRIOR_START = date(2024, 1, 1)
PRIOR_END = date(2024, 12, 31)

# --- codes (the exact codes the rule needs; no package JSON involved) ------------------------

T2DM = "44054006"
T1DM = "46635009"
PREDIABETES = "15777000"
RETINAL_EXAM = "722161008"
OCT_RETINA = "700070005"
RETINOPATHY = "422034002"
HBA1C = "4548-4"
LEFT_EYE = "71490-7"
RIGHT_EYE = "71491-5"
NEGATIVE = NEGATIVE_RETINOPATHY_ANSWER
MILD_NPDR = "LA18644-7"
HOSPICE = "385763009"
DEMENTIA = "26929004"
DONEPEZIL = "310436"

SNOMED = "SNOMED"
LOINC = "LOINC"
RXNORM = "RXNORM"


def _set(set_id: str, system: str, codes: tuple[str, ...]) -> ValueSet:
    return ValueSet(
        id=set_id,
        code_system=system,
        source="synthea-scan",
        codes=[ValueSetCode(code=c) for c in codes],
    )


@pytest.fixture(scope="module")
def vs() -> ValueSets:
    return ValueSets(
        version="test",
        sets={
            "diabetes_snomed": _set("diabetes_snomed", SNOMED, (T2DM, T1DM)),
            "prediabetes_trap_snomed": _set("prediabetes_trap_snomed", SNOMED, (PREDIABETES,)),
            "retinal_exam_proc": _set("retinal_exam_proc", SNOMED, (RETINAL_EXAM, OCT_RETINA)),
            "diabetic_retinopathy_snomed": _set(
                "diabetic_retinopathy_snomed", SNOMED, (RETINOPATHY,)
            ),
            "hba1c_loinc": _set("hba1c_loinc", LOINC, (HBA1C,)),
            # Needed only by the global rules when the engine runs.
            "hospice_snomed": _set("hospice_snomed", SNOMED, (HOSPICE,)),
            "dementia_snomed": _set("dementia_snomed", SNOMED, (DEMENTIA,)),
            "dementia_meds_rxnorm": _set("dementia_meds_rxnorm", RXNORM, (DONEPEZIL,)),
        },
    )


@pytest.fixture(scope="module")
def rule() -> EedRule:
    return EedRule()


# --- record factory ------------------------------------------------------------------------


@dataclass
class RecordFactory:
    """Fluent builder over the P6 models; every event id is deterministic."""

    birth_date: date | None = date(1970, 6, 15)
    death_date: date | None = None
    sex: str = "female"
    conditions: list[ConditionEvent] = field(default_factory=list)
    observations: list[ObservationEvent] = field(default_factory=list)
    procedures: list[ProcedureEvent] = field(default_factory=list)
    medications: list[MedicationEvent] = field(default_factory=list)
    encounters: list[EncounterEvent] = field(default_factory=list)
    _seq: int = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def born(self, birth_date: date | None) -> Self:
        self.birth_date = birth_date
        return self

    def died(self, death_date: date) -> Self:
        self.death_date = death_date
        return self

    def condition(
        self,
        code: str,
        onset: date,
        *,
        abatement: date | None = None,
        system: str = SNOMED,
    ) -> Self:
        self.conditions.append(
            ConditionEvent(
                condition_id=self._next("c"),
                code=code,
                code_system=system,
                onset_date=onset,
                abatement_date=abatement,
            )
        )
        return self

    def diabetes(self, onset: date = date(2020, 3, 1), *, abatement: date | None = None) -> Self:
        return self.condition(T2DM, onset, abatement=abatement)

    def procedure(self, code: str, on: date, *, system: str = SNOMED) -> Self:
        self.procedures.append(
            ProcedureEvent(
                procedure_id=self._next("p"),
                code=code,
                code_system=system,
                performed_date=on,
            )
        )
        return self

    def exam(self, on: date, *, code: str = RETINAL_EXAM, system: str = SNOMED) -> Self:
        return self.procedure(code, on, system=system)

    def observation(
        self,
        code: str,
        on: date,
        *,
        value_code: str | None = None,
        value_num: float | None = None,
        system: str = LOINC,
    ) -> Self:
        self.observations.append(
            ObservationEvent(
                observation_id=self._next("o"),
                code=code,
                code_system=system,
                effective_date=on,
                value_code=value_code,
                value_num=value_num,
            )
        )
        return self

    def negative_eyes(self, on: date, *, codes: tuple[str, ...] = (LEFT_EYE, RIGHT_EYE)) -> Self:
        for code in codes:
            self.observation(code, on, value_code=NEGATIVE)
        return self

    def hba1c(self, on: date, value: float = 6.8) -> Self:
        return self.observation(HBA1C, on, value_num=value)

    def medication(self, code: str, on: date, *, status: str | None = None) -> Self:
        self.medications.append(
            MedicationEvent(
                medication_request_id=self._next("m"),
                code=code,
                code_system=RXNORM,
                authored_date=on,
                status=status,
            )
        )
        return self

    def encounter(self, on: date, *, klass: str = "AMB", type_code: str | None = None) -> Self:
        self.encounters.append(
            EncounterEvent(
                encounter_id=self._next("e"),
                encounter_class=klass,
                type_code=type_code,
                start_date=on,
            )
        )
        return self

    def build(self, as_of: date = RETRO) -> PatientRecord:
        return PatientRecord(
            patient=PatientHeader(
                patient_id="pt-eed",
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


def _run(rule: EedRule, vs: ValueSets, record: PatientRecord) -> RuleOutput:
    return rule.evaluate(record, _ctx(record), vs)


def _evidence_ids(out: RuleOutput, part: str) -> set[str]:
    tri = out.denominator if part == "denominator" else out.numerator
    return {e.event_id for e in tri.evidence}


# --- denominator ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DenomCase:
    label: str
    factory: RecordFactory
    as_of: date
    expected: Tri
    reason_fragment: str


DENOMINATOR_CASES: tuple[DenomCase, ...] = (
    DenomCase(
        "age 18 at Dec 31 (born Dec 31 2007) -> yes",
        RecordFactory().born(date(2007, 12, 31)).diabetes(),
        RETRO,
        "yes",
        f"age 18 at Dec 31 of the MY within {AGE_MIN}-{AGE_MAX}",
    ),
    DenomCase(
        "age 17 at Dec 31 (born Jan 1 2008) -> no",
        RecordFactory().born(date(2008, 1, 1)).diabetes(),
        RETRO,
        "no",
        "age 17 at Dec 31 of the MY outside",
    ),
    DenomCase(
        "age 75 at Dec 31 (born Jan 1 1950) -> yes",
        RecordFactory().born(date(1950, 1, 1)).diabetes(),
        RETRO,
        "yes",
        "age 75 at Dec 31 of the MY within",
    ),
    DenomCase(
        "age 76 at Dec 31 (born Dec 31 1949) -> no",
        RecordFactory().born(date(1949, 12, 31)).diabetes(),
        RETRO,
        "no",
        "age 76 at Dec 31 of the MY outside",
    ),
    DenomCase(
        "age uses Dec 31 even when as_of is mid-year (born Sep 1 2007 -> 18)",
        RecordFactory().born(date(2007, 9, 1)).diabetes(),
        MID,
        "yes",
        "age 18 at Dec 31 of the MY within",
    ),
    DenomCase(
        "birth_date unknown -> unknown",
        RecordFactory().born(None).diabetes(),
        RETRO,
        "unknown",
        "birth_date unknown",
    ),
    DenomCase(
        "birth_date unknown and no diabetes -> no (missing datum would not decide it)",
        RecordFactory().born(None),
        RETRO,
        "no",
        "no diabetes condition active",
    ),
    DenomCase(
        "died before the MY -> no",
        RecordFactory().diabetes().died(date(2024, 12, 31)),
        RETRO,
        "no",
        "died before the measurement year",
    ),
    DenomCase(
        "died on my_start -> still in denominator (exclusion is global)",
        RecordFactory().diabetes().died(MY_START),
        RETRO,
        "yes",
        "diabetes condition active",
    ),
    DenomCase(
        "died after as_of -> in denominator",
        RecordFactory().diabetes().died(date(2025, 9, 1)),
        MID,
        "yes",
        "diabetes condition active",
    ),
    DenomCase(
        "diabetes onset in prior year, no abatement -> yes",
        RecordFactory().diabetes(date(2024, 5, 5)),
        RETRO,
        "yes",
        "diabetes condition active",
    ),
    DenomCase(
        "diabetes onset in MY -> yes",
        RecordFactory().diabetes(date(2025, 11, 30)),
        RETRO,
        "yes",
        "diabetes condition active",
    ),
    DenomCase(
        "diabetes onset on my_end with mid-year as_of -> yes (MY bounds, not as_of)",
        RecordFactory().diabetes(MY_END),
        MID,
        "yes",
        "diabetes condition active",
    ),
    DenomCase(
        "diabetes onset after my_end -> no",
        RecordFactory().diabetes(date(2026, 1, 1)),
        MID,
        "no",
        "no diabetes condition active",
    ),
    DenomCase(
        "old diabetes abated the day before prior_my_start -> no",
        RecordFactory().diabetes(date(2015, 1, 1), abatement=date(2023, 12, 31)),
        RETRO,
        "no",
        "no diabetes condition active",
    ),
    DenomCase(
        "old diabetes abated ON prior_my_start -> no (abatement must be after the start)",
        RecordFactory().diabetes(date(2015, 1, 1), abatement=PRIOR_START),
        RETRO,
        "no",
        "no diabetes condition active",
    ),
    DenomCase(
        "old diabetes abated the day after prior_my_start -> yes",
        RecordFactory().diabetes(date(2015, 1, 1), abatement=date(2024, 1, 2)),
        RETRO,
        "yes",
        "diabetes condition active",
    ),
    DenomCase(
        "diabetes abated inside the MY -> still in denominator",
        RecordFactory().diabetes(date(2015, 1, 1), abatement=date(2025, 3, 1)),
        RETRO,
        "yes",
        "diabetes condition active",
    ),
    DenomCase(
        "type 1 diabetes counts",
        RecordFactory().condition(T1DM, date(2010, 1, 1)),
        RETRO,
        "yes",
        "diabetes condition active",
    ),
    DenomCase(
        "prediabetes only -> no (trap)",
        RecordFactory().condition(PREDIABETES, date(2024, 6, 1)),
        RETRO,
        "no",
        "1 prediabetes code(s) ignored",
    ),
    DenomCase(
        "prediabetes beside real diabetes -> yes, trap reported",
        RecordFactory().condition(PREDIABETES, date(2024, 6, 1)).diabetes(),
        RETRO,
        "yes",
        "1 prediabetes code(s) ignored",
    ),
    DenomCase(
        "diabetes code under another code system -> no",
        RecordFactory().condition(T2DM, date(2020, 1, 1), system="ICD10"),
        RETRO,
        "no",
        "no diabetes condition active",
    ),
    DenomCase(
        "no conditions at all -> no",
        RecordFactory(),
        RETRO,
        "no",
        "no diabetes condition active",
    ),
)


@pytest.mark.parametrize("case", DENOMINATOR_CASES, ids=[c.label for c in DENOMINATOR_CASES])
def test_denominator(rule: EedRule, vs: ValueSets, case: DenomCase) -> None:
    out = _run(rule, vs, case.factory.build(case.as_of))
    assert out.denominator.value == case.expected
    assert any(case.reason_fragment in r for r in out.denominator.reasons), out.denominator.reasons


def test_denominator_window_uses_my_bounds(rule: EedRule, vs: ValueSets) -> None:
    out = _run(rule, vs, RecordFactory().diabetes().build(MID))
    assert (out.denominator.window_start, out.denominator.window_end) == (PRIOR_START, MY_END)


def test_denominator_evidence_carries_patient_and_diabetes_refs(
    rule: EedRule, vs: ValueSets
) -> None:
    out = _run(rule, vs, RecordFactory().diabetes().condition(T1DM, date(2024, 2, 2)).build())
    assert out.denominator.value == "yes"
    assert {e.section for e in out.denominator.evidence} == {"patient", "conditions"}
    assert {e.role for e in out.denominator.evidence} == {"eligibility"}
    assert _evidence_ids(out, "denominator") == {"patient", "c1", "c2"}


def test_prediabetes_trap_never_enters_evidence(rule: EedRule, vs: ValueSets) -> None:
    out = _run(rule, vs, RecordFactory().condition(PREDIABETES, date(2024, 6, 1)).build())
    assert out.denominator.value == "no"
    assert _evidence_ids(out, "denominator") == {"patient"}


# --- numerator -------------------------------------------------------------------------------


@dataclass(frozen=True)
class NumCase:
    label: str
    factory: RecordFactory
    as_of: date
    expected: Tri
    subtype: str | None = None
    evidence_ids: frozenset[str] | None = None


NUMERATOR_CASES: tuple[NumCase, ...] = (
    NumCase(
        "exam on my_start -> yes",
        RecordFactory().diabetes().exam(MY_START),
        RETRO,
        "yes",
        evidence_ids=frozenset({"p2"}),
    ),
    NumCase(
        "exam on as_of (mid-year) -> yes",
        RecordFactory().diabetes().exam(MID),
        MID,
        "yes",
        evidence_ids=frozenset({"p2"}),
    ),
    NumCase(
        "exam the day after as_of (still inside the MY) -> no",
        RecordFactory().diabetes().exam(date(2025, 7, 1)),
        MID,
        "no",
    ),
    NumCase(
        "OCT of retina code in MY -> yes",
        RecordFactory().diabetes().exam(MID, code=OCT_RETINA),
        RETRO,
        "yes",
    ),
    NumCase(
        "exam code under another code system -> no",
        RecordFactory().diabetes().exam(MID, system="CPT"),
        RETRO,
        "no",
    ),
    NumCase(
        "exam in MY with retinopathy on record -> yes (MY exam always counts)",
        RecordFactory().diabetes().condition(RETINOPATHY, date(2020, 1, 1)).exam(MID),
        RETRO,
        "yes",
    ),
    NumCase(
        "exam on Dec 31 MY-1 with negative eyes that day -> yes (prior-year branch)",
        RecordFactory().diabetes().exam(PRIOR_END).negative_eyes(PRIOR_END),
        RETRO,
        "yes",
        evidence_ids=frozenset({"p2", "o3", "o4"}),
    ),
    NumCase(
        "exam on Jan 1 MY-1 with negative eyes -> yes",
        RecordFactory().diabetes().exam(PRIOR_START).negative_eyes(PRIOR_START),
        RETRO,
        "yes",
    ),
    NumCase(
        "prior-year exam, negative right eye only -> yes",
        RecordFactory()
        .diabetes()
        .exam(date(2024, 5, 5))
        .negative_eyes(date(2024, 5, 5), codes=(RIGHT_EYE,)),
        RETRO,
        "yes",
        evidence_ids=frozenset({"p2", "o3"}),
    ),
    NumCase(
        "prior-year exam, negative left eye only -> yes",
        RecordFactory()
        .diabetes()
        .exam(date(2024, 5, 5))
        .negative_eyes(date(2024, 5, 5), codes=(LEFT_EYE,)),
        RETRO,
        "yes",
    ),
    NumCase(
        "prior-year exam, negative answer dated later in the prior year -> yes",
        RecordFactory().diabetes().exam(date(2024, 3, 3)).negative_eyes(date(2024, 10, 10)),
        RETRO,
        "yes",
    ),
    NumCase(
        "prior-year exam, no eye observations -> no (without negative result)",
        RecordFactory().diabetes().exam(date(2024, 5, 5)),
        RETRO,
        "no",
        subtype=PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT,
        evidence_ids=frozenset({"p2"}),
    ),
    NumCase(
        "prior-year exam, eyes answered mild NPDR -> no",
        RecordFactory()
        .diabetes()
        .exam(date(2024, 5, 5))
        .observation(LEFT_EYE, date(2024, 5, 5), value_code=MILD_NPDR)
        .observation(RIGHT_EYE, date(2024, 5, 5), value_code=MILD_NPDR),
        RETRO,
        "no",
        subtype=PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT,
    ),
    NumCase(
        "prior-year exam, eye observation with null value_code -> no",
        RecordFactory().diabetes().exam(date(2024, 5, 5)).observation(LEFT_EYE, date(2024, 5, 5)),
        RETRO,
        "no",
        subtype=PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT,
    ),
    NumCase(
        "negative answer carried by an unrelated code -> no",
        RecordFactory()
        .diabetes()
        .exam(date(2024, 5, 5))
        .observation("99999-9", date(2024, 5, 5), value_code=NEGATIVE),
        RETRO,
        "no",
        subtype=PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT,
    ),
    NumCase(
        "prior-year exam but negative eyes dated in the MY -> no",
        RecordFactory().diabetes().exam(date(2024, 5, 5)).negative_eyes(MY_START),
        RETRO,
        "no",
        subtype=PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT,
    ),
    NumCase(
        "prior-year exam but negative eyes dated in MY-2 -> no",
        RecordFactory().diabetes().exam(date(2024, 5, 5)).negative_eyes(date(2023, 12, 31)),
        RETRO,
        "no",
        subtype=PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT,
    ),
    NumCase(
        "exam on Dec 31 MY-2 with negative eyes in prior year -> no (no exam in window)",
        RecordFactory().diabetes().exam(date(2023, 12, 31)).negative_eyes(date(2024, 2, 2)),
        RETRO,
        "no",
    ),
    NumCase(
        "prior-year exam + negatives, retinopathy onset before the exam -> no",
        RecordFactory()
        .diabetes()
        .condition(RETINOPATHY, date(2024, 5, 4))
        .exam(date(2024, 5, 5))
        .negative_eyes(date(2024, 5, 5)),
        RETRO,
        "no",
        subtype=PRIOR_YEAR_EXAM_WITH_RETINOPATHY,
        evidence_ids=frozenset({"p3", "o4", "o5", "c2"}),
    ),
    NumCase(
        "prior-year exam + negatives, retinopathy onset on the exam date -> no",
        RecordFactory()
        .diabetes()
        .condition(RETINOPATHY, date(2024, 5, 5))
        .exam(date(2024, 5, 5))
        .negative_eyes(date(2024, 5, 5)),
        RETRO,
        "no",
        subtype=PRIOR_YEAR_EXAM_WITH_RETINOPATHY,
    ),
    NumCase(
        "prior-year exam + negatives, retinopathy onset the day after the exam -> yes",
        RecordFactory()
        .diabetes()
        .condition(RETINOPATHY, date(2024, 5, 6))
        .exam(date(2024, 5, 5))
        .negative_eyes(date(2024, 5, 5)),
        RETRO,
        "yes",
    ),
    NumCase(
        "prior-year exam + negatives, retinopathy years earlier -> no",
        RecordFactory()
        .diabetes()
        .condition(RETINOPATHY, date(2019, 1, 1))
        .exam(date(2024, 5, 5))
        .negative_eyes(date(2024, 5, 5)),
        RETRO,
        "no",
        subtype=PRIOR_YEAR_EXAM_WITH_RETINOPATHY,
    ),
    NumCase(
        "two prior-year exams, retinopathy between them -> yes on the earlier exam only",
        RecordFactory()
        .diabetes()
        .exam(date(2024, 3, 1))
        .condition(RETINOPATHY, date(2024, 6, 1))
        .exam(date(2024, 9, 1))
        .negative_eyes(date(2024, 3, 1)),
        RETRO,
        "yes",
        evidence_ids=frozenset({"p2", "o5", "o6"}),
    ),
    NumCase(
        "prior-year exam with no negatives AND retinopathy -> no, without-negative subtype",
        RecordFactory().diabetes().condition(RETINOPATHY, date(2020, 1, 1)).exam(date(2024, 5, 5)),
        RETRO,
        "no",
        subtype=PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT,
        evidence_ids=frozenset({"p3", "c2"}),
    ),
    NumCase(
        "prior-year exam, negative answer dated BEFORE the exam -> no (not linked)",
        RecordFactory().diabetes().exam(date(2024, 5, 5)).negative_eyes(date(2024, 5, 4)),
        RETRO,
        "no",
        subtype=PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT,
    ),
    NumCase(
        "prior-year exam, negative answer dated after the exam in MY-1 -> yes",
        RecordFactory().diabetes().exam(date(2024, 5, 5)).negative_eyes(date(2024, 6, 1)),
        RETRO,
        "yes",
        evidence_ids=frozenset({"p2", "o3", "o4"}),
    ),
    NumCase(
        "negative answer after the exam but retinopathy onset on the exam date -> no",
        RecordFactory()
        .diabetes()
        .condition(RETINOPATHY, date(2024, 5, 5))
        .exam(date(2024, 5, 5))
        .negative_eyes(date(2024, 6, 1)),
        RETRO,
        "no",
        subtype=PRIOR_YEAR_EXAM_WITH_RETINOPATHY,
    ),
    NumCase(
        "no exam anywhere -> no",
        RecordFactory().diabetes().negative_eyes(date(2024, 5, 5)),
        RETRO,
        "no",
        evidence_ids=frozenset(),
    ),
)


@pytest.mark.parametrize("case", NUMERATOR_CASES, ids=[c.label for c in NUMERATOR_CASES])
def test_numerator(rule: EedRule, vs: ValueSets, case: NumCase) -> None:
    out = _run(rule, vs, case.factory.build(case.as_of))
    assert out.numerator.value == case.expected, out.numerator.reasons
    assert out.numerator.subtype == case.subtype
    if case.evidence_ids is not None:
        assert _evidence_ids(out, "numerator") == set(case.evidence_ids)
    assert {e.role for e in out.numerator.evidence} <= {"numerator"}


def test_numerator_window_ends_at_as_of_for_my_branch(rule: EedRule, vs: ValueSets) -> None:
    out = _run(rule, vs, RecordFactory().diabetes().exam(date(2025, 2, 2)).build(MID))
    assert (out.numerator.window_start, out.numerator.window_end) == (MY_START, MID)


def test_numerator_window_is_prior_year_for_negative_branch(rule: EedRule, vs: ValueSets) -> None:
    rec = RecordFactory().diabetes().exam(date(2024, 2, 2)).negative_eyes(date(2024, 2, 2)).build()
    out = _run(rule, vs, rec)
    assert out.numerator.value == "yes"
    assert (out.numerator.window_start, out.numerator.window_end) == (PRIOR_START, PRIOR_END)


def test_numerator_open_window_spans_prior_year_to_as_of(rule: EedRule, vs: ValueSets) -> None:
    out = _run(rule, vs, RecordFactory().diabetes().build(MID))
    assert out.numerator.value == "no"
    assert (out.numerator.window_start, out.numerator.window_end) == (PRIOR_START, MID)
    assert len(out.numerator.reasons) == 2


def test_numerator_my_exam_evidence_lists_every_my_exam(rule: EedRule, vs: ValueSets) -> None:
    rec = (
        RecordFactory()
        .diabetes()
        .exam(date(2025, 1, 5))
        .exam(date(2025, 8, 5), code=OCT_RETINA)
        .exam(date(2024, 5, 5))
        .build()
    )
    out = _run(rule, vs, rec)
    assert out.numerator.value == "yes"
    assert _evidence_ids(out, "numerator") == {"p2", "p3"}
    assert "2025-08-05" in out.numerator.reasons[0]


def test_numerator_is_independent_of_denominator(rule: EedRule, vs: ValueSets) -> None:
    """The engine gates on the denominator; the rule itself reports the numerator regardless."""
    out = _run(rule, vs, RecordFactory().exam(MID).build())
    assert out.denominator.value == "no"
    assert out.numerator.value == "yes"


# --- E6 --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class E6Case:
    label: str
    factory: RecordFactory
    as_of: date
    expected: bool


E6_CASES: tuple[E6Case, ...] = (
    E6Case(
        "abated in MY + HbA1c in MY -> E6",
        RecordFactory().diabetes(abatement=date(2025, 3, 1)).hba1c(date(2025, 4, 1)),
        RETRO,
        True,
    ),
    E6Case(
        "abated on my_start + HbA1c on as_of -> E6 (edges)",
        RecordFactory().diabetes(abatement=MY_START).hba1c(RETRO),
        RETRO,
        True,
    ),
    E6Case(
        "abated on as_of (mid-year) + HbA1c in MY -> E6",
        RecordFactory().diabetes(abatement=MID).hba1c(date(2025, 2, 1)),
        MID,
        True,
    ),
    E6Case(
        "abated in MY, no HbA1c -> E6 (abatement alone triggers, as CBP)",
        RecordFactory().diabetes(abatement=date(2025, 3, 1)),
        RETRO,
        True,
    ),
    E6Case(
        "abated in MY, HbA1c only in the prior year -> E6",
        RecordFactory().diabetes(abatement=date(2025, 3, 1)).hba1c(PRIOR_END),
        RETRO,
        True,
    ),
    E6Case(
        "abated in MY, HbA1c after as_of -> E6",
        RecordFactory().diabetes(abatement=date(2025, 3, 1)).hba1c(date(2025, 7, 1)),
        MID,
        True,
    ),
    E6Case(
        "abated the day before my_start + HbA1c in MY -> none",
        RecordFactory().diabetes(abatement=PRIOR_END).hba1c(date(2025, 4, 1)),
        RETRO,
        False,
    ),
    E6Case(
        "abated after as_of + HbA1c in MY -> none (masking boundary)",
        RecordFactory().diabetes(abatement=date(2025, 9, 1)).hba1c(date(2025, 4, 1)),
        MID,
        False,
    ),
    E6Case(
        "active diabetes + HbA1c in MY -> none",
        RecordFactory().diabetes().hba1c(date(2025, 4, 1)),
        RETRO,
        False,
    ),
    E6Case(
        "prediabetes trap abated in MY + HbA1c -> none",
        RecordFactory()
        .condition(PREDIABETES, date(2020, 1, 1), abatement=date(2025, 3, 1))
        .hba1c(date(2025, 4, 1)),
        RETRO,
        False,
    ),
    E6Case(
        "HbA1c code under another code system -> E6 (abatement alone)",
        RecordFactory()
        .diabetes(abatement=date(2025, 3, 1))
        .observation(HBA1C, date(2025, 4, 1), value_num=7.0, system="SNOMED"),
        RETRO,
        True,
    ),
)


@pytest.mark.parametrize("case", E6_CASES, ids=[c.label for c in E6_CASES])
def test_e6(rule: EedRule, vs: ValueSets, case: E6Case) -> None:
    out = _run(rule, vs, case.factory.build(case.as_of))
    kinds = [f.kind for f in out.escalations]
    assert kinds == (["E6"] if case.expected else [])


def test_e6_flag_shape(rule: EedRule, vs: ValueSets) -> None:
    rec = (
        RecordFactory()
        .diabetes(abatement=date(2025, 3, 1))
        .hba1c(date(2025, 4, 1))
        .hba1c(date(2025, 10, 1))
        .build()
    )
    out = _run(rule, vs, rec)
    (flag,) = out.escalations
    assert flag.kind == "E6"
    assert flag.scope == "measure"
    assert {e.role for e in flag.evidence} == {"escalation"}
    assert {e.event_id for e in flag.evidence} == {"c1", "o2", "o3"}
    assert "2 HbA1c result(s)" in flag.reason
    # Without HbA1c the flag still fires, on the condition alone.
    (flag,) = _run(
        rule, vs, RecordFactory().diabetes(abatement=date(2025, 3, 1)).build()
    ).escalations
    assert {e.event_id for e in flag.evidence} == {"c1"}
    assert "HbA1c" not in flag.reason


def test_rule_never_emits_global_or_other_escalations(rule: EedRule, vs: ValueSets) -> None:
    """E1 / E4 are the engine's job; the rule must not duplicate them."""
    rec = (
        RecordFactory()
        .born(date(1950, 1, 1))
        .diabetes()
        .procedure(HOSPICE, date(2024, 12, 1))
        .condition(DEMENTIA, date(2020, 1, 1))
        .encounter(date(2025, 3, 3), klass="IMP")
        .build()
    )
    out = _run(rule, vs, rec)
    assert out.escalations == []
    assert out.exclusions == []


# --- determinism / order independence --------------------------------------------------------


def _rich_record(as_of: date = RETRO) -> RecordFactory:
    return (
        RecordFactory()
        .diabetes(date(2015, 1, 1), abatement=date(2025, 3, 1))
        .condition(T1DM, date(2024, 2, 2))
        .condition(PREDIABETES, date(2012, 1, 1))
        .condition(RETINOPATHY, date(2024, 6, 1))
        .exam(date(2024, 3, 1))
        .exam(date(2024, 9, 1))
        .exam(date(2023, 12, 31))
        .negative_eyes(date(2024, 3, 1))
        .negative_eyes(date(2024, 9, 1))
        .observation(LEFT_EYE, date(2024, 9, 1), value_code=MILD_NPDR)
        .hba1c(date(2025, 2, 1))
        .hba1c(date(2025, 8, 1))
        .medication(DONEPEZIL, date(2025, 1, 1), status="active")
        .encounter(date(2025, 3, 3), klass="IMP")
    )


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7])
def test_order_independence(rule: EedRule, vs: ValueSets, seed: int) -> None:
    base = _rich_record().build()
    rng = random.Random(seed)

    def shuffled[T](items: list[T]) -> list[T]:
        copy = list(items)
        rng.shuffle(copy)
        return copy

    shuffled_record = base.model_copy(
        update={
            "conditions": shuffled(base.conditions),
            "observations": shuffled(base.observations),
            "procedures": shuffled(base.procedures),
            "medications": shuffled(base.medications),
            "encounters": shuffled(base.encounters),
        }
    )
    assert _run(rule, vs, shuffled_record) == _run(rule, vs, base)


def test_evaluate_is_pure(rule: EedRule, vs: ValueSets) -> None:
    rec = _rich_record().build()
    before = rec.model_copy(deep=True)
    first = _run(rule, vs, rec)
    second = _run(rule, vs, rec)
    assert first == second
    assert rec == before


# --- metadata / coverage ---------------------------------------------------------------------


def test_metadata(rule: EedRule) -> None:
    assert rule.measure_id == "EED"
    assert rule.rule_version == "eed-v1"
    assert set(rule.value_set_ids) == {
        "diabetes_snomed",
        "prediabetes_trap_snomed",
        "retinal_exam_proc",
        "diabetic_retinopathy_snomed",
        "hba1c_loinc",
    }


def test_coverage_table(rule: EedRule, vs: ValueSets) -> None:
    out = _run(rule, vs, RecordFactory().diabetes().build())
    assert out.coverage == COVERAGE
    assert out.coverage is not COVERAGE  # a copy: callers cannot mutate the module table
    allowed: set[Coverage] = {"observable", "partial", "not_representable"}
    assert set(out.coverage.values()) <= allowed
    assert out.coverage["died_during_measurement_period"] == "observable"
    assert out.coverage["hospice_during_measurement_period"] == "observable"
    assert out.coverage["frailty_and_advanced_illness_66_plus"] == "partial"
    assert out.coverage["palliative_care_during_measurement_period"] == "not_representable"
    assert "bilateral_blindness_or_enucleation" in NON_EXCLUSIONS
    assert not set(NON_EXCLUSIONS) & set(COVERAGE)


# --- engine-level verdicts (global rules + verdict algebra composed with this rule) ----------


@dataclass(frozen=True)
class VerdictCase:
    label: str
    factory: RecordFactory
    as_of: date
    verdict: str


VERDICT_CASES: tuple[VerdictCase, ...] = (
    VerdictCase(
        "eligible + MY exam -> closed", RecordFactory().diabetes().exam(MID), RETRO, "closed"
    ),
    VerdictCase(
        "eligible + negative prior-year exam -> closed",
        RecordFactory().diabetes().exam(date(2024, 4, 4)).negative_eyes(date(2024, 4, 4)),
        RETRO,
        "closed",
    ),
    VerdictCase("eligible + no exam -> gap_open", RecordFactory().diabetes(), RETRO, "gap_open"),
    VerdictCase(
        "eligible + prior-year exam with retinopathy -> gap_open",
        RecordFactory()
        .diabetes()
        .condition(RETINOPATHY, date(2020, 1, 1))
        .exam(date(2024, 4, 4))
        .negative_eyes(date(2024, 4, 4)),
        RETRO,
        "gap_open",
    ),
    VerdictCase(
        "E6 promotes a closed gap to needs_review",
        RecordFactory().diabetes(abatement=date(2025, 3, 1)).hba1c(date(2025, 4, 1)).exam(MID),
        RETRO,
        "needs_review",
    ),
    VerdictCase(
        "E6 promotes an open gap to needs_review",
        RecordFactory().diabetes(abatement=date(2025, 3, 1)).hba1c(date(2025, 4, 1)),
        RETRO,
        "needs_review",
    ),
    VerdictCase(
        "age 17 -> not_eligible",
        RecordFactory().born(date(2008, 1, 1)).diabetes(),
        RETRO,
        "not_eligible",
    ),
    VerdictCase(
        "prediabetes only -> not_eligible",
        RecordFactory().condition(PREDIABETES, MID),
        RETRO,
        "not_eligible",
    ),
    VerdictCase(
        "birth_date unknown -> needs_review",
        RecordFactory().born(None).diabetes(),
        RETRO,
        "needs_review",
    ),
    VerdictCase(
        "died before MY -> not_eligible",
        RecordFactory().diabetes().died(PRIOR_END),
        RETRO,
        "not_eligible",
    ),
    VerdictCase(
        "died in MY -> excluded (global)", RecordFactory().diabetes().died(MID), RETRO, "excluded"
    ),
    VerdictCase(
        "died after as_of -> gap_open",
        RecordFactory().diabetes().died(date(2025, 9, 1)),
        MID,
        "gap_open",
    ),
    VerdictCase(
        "hospice in MY -> excluded (global)",
        RecordFactory().diabetes().procedure(HOSPICE, date(2025, 2, 2)),
        RETRO,
        "excluded",
    ),
    VerdictCase(
        "hospice 30 days before MY -> E1 promotes to needs_review (global)",
        RecordFactory().diabetes().procedure(HOSPICE, date(2024, 12, 2)),
        RETRO,
        "needs_review",
    ),
    VerdictCase(
        "hospice a year before MY -> no exclusion, no E1 -> gap_open",
        RecordFactory().diabetes().procedure(HOSPICE, date(2024, 1, 2)),
        RETRO,
        "gap_open",
    ),
)


@pytest.mark.parametrize("case", VERDICT_CASES, ids=[c.label for c in VERDICT_CASES])
def test_engine_verdicts(vs: ValueSets, case: VerdictCase) -> None:
    engine = MeasureEngine([EedRule()], value_sets=vs)
    record = case.factory.build(case.as_of)
    evaluation = engine.evaluate_one(record, _ctx(record), "EED")
    assert evaluation.verdict == case.verdict
    assert evaluation.measure_id == "EED"
    assert evaluation.rule_version == "eed-v1"
    if evaluation.verdict in {"gap_open", "needs_review"}:
        assert evaluation.priority_score > 0
    else:
        assert evaluation.priority_score == 0.0


def test_engine_priority_is_flat_for_eed_subtypes(vs: ValueSets) -> None:
    """EED subtypes carry no priority bonus (SPEC: bonus only for CBP / SPC subtypes)."""
    engine = MeasureEngine([EedRule()], value_sets=vs)
    plain = RecordFactory().diabetes().build()
    blocked = RecordFactory().diabetes().exam(date(2024, 4, 4)).build()
    a = engine.evaluate_one(plain, _ctx(plain), "EED")
    b = engine.evaluate_one(blocked, _ctx(blocked), "EED")
    assert b.numerator.subtype == PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT
    assert a.priority_score == b.priority_score == 2.0
