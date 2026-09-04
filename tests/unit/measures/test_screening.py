"""TSC / SNS screening rules — table-driven over an in-test RecordFactory.

Covers the SPEC section 9 edges that apply to the screening measures: window edges (my_start
/ as_of, +/-1 day), age 17 vs 18 at Dec 31 (birthday on the boundary), missing birth_date,
death before / during / after the MY, numerator windows ending at ``as_of`` (mid-year anchor),
wrong code system / wrong code / prior-year screening never counting, value_code presence,
PRAPARE child components (``positive_domains``), order independence over shuffled events,
purity, and the verdict through the real ``MeasureEngine`` with the global rules.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date
from itertools import count

import pytest

from caregap.measures.context import MeasurementContext
from caregap.measures.engine import MeasureEngine, RuleOutput
from caregap.measures.rules.screening import (
    COVERAGE,
    MAX_ENCOUNTER_EVIDENCE,
    PRAPARE_CODE,
    STATUS_WITHOUT_VALUE,
    TOBACCO_STATUS_CODE,
    SnsRule,
    TscRule,
)
from caregap.measures.tri import Tri
from caregap.measures.value_sets import ValueSet, ValueSetCode, ValueSets, ValueSetSource
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

AS_OF = date(2025, 12, 31)  # eval anchor: complete MY2025
MY_START = date(2025, 1, 1)
MID_YEAR = date(2026, 6, 30)  # demo anchor: prospective, MY2026
BORN_1980 = date(1980, 5, 5)
HOSPICE_CODE = "385763009"
DEMENTIA_CODE = "26929004"
DEMENTIA_MED = "997224"
TOBACCO_ANSWER = "266919005"  # SNOMED "Never smoked tobacco"
PRAPARE_ITEM_A = "93035-4"
PRAPARE_ITEM_B = "93031-3"
PRAPARE_ITEM_C = "93030-5"


def _vs(
    set_id: str, system: str, codes: list[str], source: ValueSetSource = "synthea-scan"
) -> ValueSet:
    return ValueSet(
        id=set_id,
        code_system=system,
        source=source,
        codes=[ValueSetCode(code=c) for c in codes],
    )


VALUE_SETS = ValueSets(
    version="test",
    sets={
        "tobacco_status_loinc": _vs(
            "tobacco_status_loinc", "LOINC", [TOBACCO_STATUS_CODE], "p6-valuesets-2026.08"
        ),
        "sdoh_screening_loinc": _vs("sdoh_screening_loinc", "LOINC", [PRAPARE_CODE]),
        # Needed only because the engine's GLOBAL rules read them.
        "hospice_snomed": _vs("hospice_snomed", "SNOMED", [HOSPICE_CODE]),
        "dementia_snomed": _vs("dementia_snomed", "SNOMED", [DEMENTIA_CODE]),
        "dementia_meds_rxnorm": _vs("dementia_meds_rxnorm", "RXNORM", [DEMENTIA_MED]),
    },
)


# --- RecordFactory ---------------------------------------------------------------------------


@dataclass
class RecordFactory:
    """Builds PatientRecord objects inline from the p6 models; ids are deterministic."""

    as_of: date = AS_OF
    birth_date: date | None = BORN_1980
    death_date: date | None = None
    sex: str = "female"
    conditions: list[ConditionEvent] = field(default_factory=list)
    observations: list[ObservationEvent] = field(default_factory=list)
    procedures: list[ProcedureEvent] = field(default_factory=list)
    medications: list[MedicationEvent] = field(default_factory=list)
    encounters: list[EncounterEvent] = field(default_factory=list)
    _seq: count[int] = field(default_factory=count)

    def _id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._seq)}"

    def encounter(self, day: date, encounter_class: str = "AMB") -> EncounterEvent:
        e = EncounterEvent(
            encounter_id=self._id("enc"),
            encounter_class=encounter_class,
            type_code="185349003",
            type_display="Encounter for check up",
            start_date=day,
        )
        self.encounters.append(e)
        return e

    def observation(
        self,
        code: str,
        day: date,
        *,
        system: str = "LOINC",
        value_code: str | None = None,
        parent: ObservationEvent | None = None,
    ) -> ObservationEvent:
        o = ObservationEvent(
            observation_id=self._id("obs"),
            parent_observation_id=parent.observation_id if parent is not None else None,
            code=code,
            code_system=system,
            code_display=None,
            category="survey" if system == "LOINC" else None,
            effective_date=day,
            value_code=value_code,
            value_code_system="SNOMED" if value_code is not None else None,
        )
        self.observations.append(o)
        return o

    def tobacco(self, day: date, value_code: str | None = TOBACCO_ANSWER) -> ObservationEvent:
        return self.observation(TOBACCO_STATUS_CODE, day, value_code=value_code)

    def prapare(self, day: date) -> ObservationEvent:
        return self.observation(PRAPARE_CODE, day)

    def condition(self, code: str, onset: date, system: str = "SNOMED") -> ConditionEvent:
        c = ConditionEvent(
            condition_id=self._id("cond"), code=code, code_system=system, onset_date=onset
        )
        self.conditions.append(c)
        return c

    def procedure(self, code: str, day: date, system: str = "SNOMED") -> ProcedureEvent:
        p = ProcedureEvent(
            procedure_id=self._id("proc"), code=code, code_system=system, performed_date=day
        )
        self.procedures.append(p)
        return p

    def medication(self, code: str, day: date, status: str | None = None) -> MedicationEvent:
        m = MedicationEvent(
            medication_request_id=self._id("med"),
            code=code,
            code_system="RXNORM",
            authored_date=day,
            status=status,
        )
        self.medications.append(m)
        return m

    def build(self) -> PatientRecord:
        return PatientRecord(
            patient=PatientHeader(
                patient_id="p-test",
                birth_date=self.birth_date,
                death_date=self.death_date,
                sex=self.sex,
            ),
            as_of=self.as_of,
            conditions=list(self.conditions),
            observations=list(self.observations),
            procedures=list(self.procedures),
            medications=list(self.medications),
            encounters=list(self.encounters),
        )

    def context(self) -> MeasurementContext:
        return MeasurementContext.for_(self.as_of, self.birth_date)


def eligible(as_of: date = AS_OF, birth_date: date | None = BORN_1980) -> RecordFactory:
    """An adult with one ambulatory encounter on Mar 1 of the MY."""
    f = RecordFactory(as_of=as_of, birth_date=birth_date)
    f.encounter(date(as_of.year, 3, 1))
    return f


def run(rule: TscRule | SnsRule, f: RecordFactory) -> RuleOutput:
    return rule.evaluate(f.build(), f.context(), VALUE_SETS)


RULES: list[TscRule | SnsRule] = [TscRule(), SnsRule()]
RULE_IDS = ["TSC", "SNS"]


# --- identity / contract ---------------------------------------------------------------------


def test_identity_and_versions() -> None:
    assert (TscRule.measure_id, TscRule.rule_version) == ("TSC", "tsc-v1")
    assert (SnsRule.measure_id, SnsRule.rule_version) == ("SNS", "sns-v1")


@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_no_measure_scoped_exclusions_or_escalations(rule: TscRule | SnsRule) -> None:
    f = eligible()
    f.death_date = date(2025, 6, 1)  # died during MY: global exclusion, not the rule's
    f.condition(HOSPICE_CODE, date(2025, 2, 1))
    f.condition(DEMENTIA_CODE, date(2010, 1, 1))
    f.encounter(date(2025, 4, 1), "IMP")
    out = run(rule, f)
    assert out.exclusions == []
    assert out.escalations == []


@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_coverage_table(rule: TscRule | SnsRule) -> None:
    out = run(rule, eligible())
    assert out.coverage == COVERAGE
    assert out.coverage is not COVERAGE  # a copy; the rule never hands out its own table
    assert out.coverage["numerator_tobacco_cessation_intervention"] == "not_representable"
    assert (
        out.coverage["numerator_sdoh_intervention_food_housing_transportation_utility"]
        == "not_representable"
    )
    assert out.coverage["exclusion_death_during_measurement_period"] == "observable"
    assert out.coverage["exclusion_hospice_during_measurement_period"] == "observable"
    assert set(out.coverage.values()) <= {"observable", "partial", "not_representable"}


# --- denominator ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("birth_date", "expected", "reason_fragment"),
    [
        (date(2007, 12, 31), "yes", "18+"),  # turns 18 on Dec 31 of the MY
        (date(2008, 1, 1), "no", "under 18"),  # 17 at Dec 31 of the MY
        (date(1940, 1, 1), "yes", "18+"),  # no upper bound
        (None, "unknown", "birth_date unknown"),
    ],
    ids=["age-18-on-dec-31", "age-17", "age-85", "birth-unknown"],
)
@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_denominator_age(
    rule: TscRule | SnsRule, birth_date: date | None, expected: Tri, reason_fragment: str
) -> None:
    f = eligible(birth_date=birth_date)
    out = run(rule, f)
    assert out.denominator.value == expected
    assert any(reason_fragment in r for r in out.denominator.reasons)
    assert out.denominator.window_start == MY_START
    assert out.denominator.window_end == date(2025, 12, 31)
    assert out.denominator.evidence[0].section == "patient"
    assert out.denominator.evidence[0].role == "eligibility"


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2024, 12, 31), "no"),
        (MY_START, "yes"),
        (AS_OF, "yes"),
        (date(2026, 1, 1), "no"),
    ],
    ids=["day-before-my_start", "on-my_start", "on-as_of", "day-after-as_of"],
)
@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_denominator_encounter_window(rule: TscRule | SnsRule, day: date, expected: Tri) -> None:
    f = RecordFactory()
    f.encounter(day)
    out = run(rule, f)
    assert out.denominator.value == expected


@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_denominator_encounter_window_ends_at_as_of_mid_year(rule: TscRule | SnsRule) -> None:
    f = RecordFactory(as_of=MID_YEAR)
    f.encounter(date(2026, 7, 1))  # inside the MY but after as_of
    assert run(rule, f).denominator.value == "no"
    f.encounter(MID_YEAR)
    assert run(rule, f).denominator.value == "yes"


@pytest.mark.parametrize("encounter_class", ["AMB", "IMP", "EMER", "VR", "HH"])
@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_denominator_any_encounter_class_counts(
    rule: TscRule | SnsRule, encounter_class: str
) -> None:
    f = RecordFactory()
    f.encounter(date(2025, 5, 5), encounter_class)
    assert run(rule, f).denominator.value == "yes"


@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_denominator_no_encounter(rule: TscRule | SnsRule) -> None:
    f = RecordFactory()
    f.condition("44054006", date(2020, 1, 1))  # noise: conditions are not encounters
    out = run(rule, f)
    assert out.denominator.value == "no"
    assert any("no encounter" in r for r in out.denominator.reasons)
    assert all(e.section == "patient" for e in out.denominator.evidence)


@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_denominator_birth_unknown_and_no_encounter_is_no(rule: TscRule | SnsRule) -> None:
    f = RecordFactory(birth_date=None)
    assert run(rule, f).denominator.value == "no"  # a definite `no` beats `unknown`


@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_denominator_encounter_evidence_is_capped_and_earliest_first(
    rule: TscRule | SnsRule,
) -> None:
    f = RecordFactory()
    days = [date(2025, m, 1) for m in (9, 3, 12, 6, 1)]
    for d in days:
        f.encounter(d)
    out = run(rule, f)
    enc_refs = [e for e in out.denominator.evidence if e.section == "encounters"]
    assert len(enc_refs) == MAX_ENCOUNTER_EVIDENCE
    assert [e.event_date for e in enc_refs] == sorted(days)[:MAX_ENCOUNTER_EVIDENCE]
    assert all(e.role == "eligibility" for e in enc_refs)
    assert any("5 encounter(s)" in r for r in out.denominator.reasons)


@pytest.mark.parametrize(
    ("death_date", "expected"),
    [
        (date(2024, 12, 31), "no"),  # before MY -> not eligible (rule-level)
        (MY_START, "yes"),  # on my_start: died DURING the MY -> global exclusion
        (date(2025, 6, 15), "yes"),
        (date(2026, 1, 15), "yes"),  # after as_of: masked record would not show it anyway
    ],
    ids=["death-before-my", "death-on-my_start", "death-mid-my", "death-after-as_of"],
)
@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_denominator_death(rule: TscRule | SnsRule, death_date: date, expected: Tri) -> None:
    f = eligible()
    f.death_date = death_date
    out = run(rule, f)
    assert out.denominator.value == expected
    if expected == "no":
        assert out.denominator.reasons == ["died before the measurement year"]
        assert out.denominator.evidence[0].event_date == death_date


# --- numerator: TSC -------------------------------------------------------------------------


def test_tsc_numerator_yes_with_coded_status() -> None:
    f = eligible()
    obs = f.tobacco(date(2025, 3, 1))
    out = run(TscRule(), f)
    assert out.numerator.value == "yes"
    assert out.numerator.subtype is None
    assert (out.numerator.window_start, out.numerator.window_end) == (MY_START, AS_OF)
    assert [e.event_id for e in out.numerator.evidence] == [obs.observation_id]
    ref = out.numerator.evidence[0]
    assert (ref.section, ref.role, ref.code, ref.code_system) == (
        "observations",
        "numerator",
        TOBACCO_STATUS_CODE,
        "LOINC",
    )
    assert any("2025-03-01" in r for r in out.numerator.reasons)


def test_tsc_numerator_status_without_value_is_no_with_subtype() -> None:
    f = eligible()
    obs = f.tobacco(date(2025, 3, 1), value_code=None)
    out = run(TscRule(), f)
    assert out.numerator.value == "no"
    assert out.numerator.subtype == STATUS_WITHOUT_VALUE
    assert [e.event_id for e in out.numerator.evidence] == [obs.observation_id]
    assert any("without a coded value" in r for r in out.numerator.reasons)


def test_tsc_numerator_mixed_rows_counts_only_coded() -> None:
    f = eligible()
    f.tobacco(date(2025, 2, 1), value_code=None)
    coded = f.tobacco(date(2025, 8, 1))
    f.tobacco(date(2025, 10, 1), value_code=None)
    out = run(TscRule(), f)
    assert out.numerator.value == "yes"
    assert out.numerator.subtype is None
    assert [e.event_id for e in out.numerator.evidence] == [coded.observation_id]


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2024, 12, 31), "no"),
        (MY_START, "yes"),
        (AS_OF, "yes"),
        (date(2026, 1, 1), "no"),
    ],
    ids=["day-before-my_start", "on-my_start", "on-as_of", "day-after-as_of"],
)
def test_tsc_numerator_window_edges(day: date, expected: Tri) -> None:
    f = eligible()
    f.tobacco(day)
    assert run(TscRule(), f).numerator.value == expected


def test_tsc_numerator_window_ends_at_as_of_mid_year() -> None:
    f = eligible(as_of=MID_YEAR)
    f.tobacco(date(2026, 7, 15))  # in the MY, after as_of: must not count
    out = run(TscRule(), f)
    assert out.numerator.value == "no"
    assert out.numerator.evidence == []
    assert (out.numerator.window_start, out.numerator.window_end) == (date(2026, 1, 1), MID_YEAR)
    f.tobacco(MID_YEAR)
    assert run(TscRule(), f).numerator.value == "yes"


def test_tsc_numerator_prior_year_screening_never_counts() -> None:
    f = eligible()
    f.tobacco(date(2024, 12, 15))
    out = run(TscRule(), f)
    assert out.numerator.value == "no"
    assert out.numerator.subtype is None
    assert out.numerator.evidence == []


def test_tsc_numerator_wrong_code_system_does_not_count() -> None:
    f = eligible()
    f.observation(TOBACCO_STATUS_CODE, date(2025, 3, 1), system="SNOMED", value_code="x")
    out = run(TscRule(), f)
    assert out.numerator.value == "no"
    assert out.numerator.subtype is None


def test_tsc_numerator_ignores_prapare() -> None:
    f = eligible()
    f.prapare(date(2025, 3, 1))
    assert run(TscRule(), f).numerator.value == "no"


def test_tsc_numerator_no_screening_reason() -> None:
    out = run(TscRule(), eligible())
    assert out.numerator.value == "no"
    assert out.numerator.evidence == []
    assert out.numerator.reasons == [
        "no tobacco use screening (72166-2 with a coded status) in "
        "measurement year to as_of [2025-01-01 .. 2025-12-31]"
    ]


# --- numerator: SNS -------------------------------------------------------------------------


def test_sns_numerator_yes_without_value_code() -> None:
    f = eligible()
    panel = f.prapare(date(2025, 4, 2))
    out = run(SnsRule(), f)
    assert out.numerator.value == "yes"
    assert out.numerator.subtype is None
    assert (out.numerator.window_start, out.numerator.window_end) == (MY_START, AS_OF)
    assert [e.event_id for e in out.numerator.evidence] == [panel.observation_id]
    assert out.numerator.evidence[0].code == PRAPARE_CODE
    assert not any(r.startswith("positive_domains:") for r in out.numerator.reasons)


def test_sns_positive_domains_from_answered_children_sorted_and_deduped() -> None:
    f = eligible()
    panel = f.prapare(date(2025, 4, 2))
    f.observation(PRAPARE_ITEM_A, date(2025, 4, 2), value_code="LA30-8", parent=panel)
    f.observation(PRAPARE_ITEM_C, date(2025, 4, 2), value_code="LA33-6", parent=panel)
    f.observation(PRAPARE_ITEM_A, date(2025, 4, 2), value_code="LA30-8", parent=panel)  # dup
    f.observation(PRAPARE_ITEM_B, date(2025, 4, 2), value_code=None, parent=panel)  # unanswered
    out = run(SnsRule(), f)
    assert out.numerator.value == "yes"
    domains = [r for r in out.numerator.reasons if r.startswith("positive_domains:")]
    assert domains == [f"positive_domains:{PRAPARE_ITEM_C},{PRAPARE_ITEM_A}"]
    # children are reported in reasons only; the panel is the evidence
    assert [e.event_id for e in out.numerator.evidence] == [panel.observation_id]


def test_sns_positive_domains_ignore_other_parents_and_orphans() -> None:
    f = eligible()
    panel = f.prapare(date(2025, 4, 2))
    other = f.observation("85354-9", date(2025, 4, 2))  # a BP panel, not PRAPARE
    f.observation("8480-6", date(2025, 4, 2), value_code="x", parent=other)
    f.observation(PRAPARE_ITEM_B, date(2025, 4, 2), value_code="x")  # orphan, no parent
    prior = f.prapare(date(2024, 6, 1))  # prior-year panel: out of window
    f.observation(PRAPARE_ITEM_C, date(2024, 6, 1), value_code="x", parent=prior)
    out = run(SnsRule(), f)
    assert out.numerator.value == "yes"
    assert [e.event_id for e in out.numerator.evidence] == [panel.observation_id]
    assert not any(r.startswith("positive_domains:") for r in out.numerator.reasons)


def test_sns_positive_domains_child_outside_window_is_ignored() -> None:
    f = eligible(as_of=MID_YEAR)
    panel = f.prapare(MID_YEAR)
    f.observation(PRAPARE_ITEM_A, date(2026, 7, 1), value_code="x", parent=panel)
    f.observation(PRAPARE_ITEM_B, MID_YEAR, value_code="x", parent=panel)
    out = run(SnsRule(), f)
    assert out.numerator.value == "yes"
    assert [r for r in out.numerator.reasons if r.startswith("positive_domains:")] == [
        f"positive_domains:{PRAPARE_ITEM_B}"
    ]


def test_sns_positive_domains_span_all_in_window_panels() -> None:
    f = eligible()
    first = f.prapare(date(2025, 2, 1))
    second = f.prapare(date(2025, 9, 1))
    f.observation(PRAPARE_ITEM_B, date(2025, 2, 1), value_code="x", parent=first)
    f.observation(PRAPARE_ITEM_A, date(2025, 9, 1), value_code="x", parent=second)
    out = run(SnsRule(), f)
    assert [e.event_id for e in out.numerator.evidence] == [
        first.observation_id,
        second.observation_id,
    ]
    assert f"positive_domains:{PRAPARE_ITEM_B},{PRAPARE_ITEM_A}" in out.numerator.reasons
    assert any("2025-09-01" in r for r in out.numerator.reasons)  # most recent named


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2024, 12, 31), "no"),
        (MY_START, "yes"),
        (AS_OF, "yes"),
        (date(2026, 1, 1), "no"),
    ],
    ids=["day-before-my_start", "on-my_start", "on-as_of", "day-after-as_of"],
)
def test_sns_numerator_window_edges(day: date, expected: Tri) -> None:
    f = eligible()
    f.prapare(day)
    assert run(SnsRule(), f).numerator.value == expected


def test_sns_numerator_window_ends_at_as_of_mid_year() -> None:
    f = eligible(as_of=MID_YEAR)
    f.prapare(date(2026, 7, 15))
    assert run(SnsRule(), f).numerator.value == "no"
    f.prapare(MID_YEAR)
    assert run(SnsRule(), f).numerator.value == "yes"


def test_sns_numerator_wrong_code_system_and_other_codes_do_not_count() -> None:
    f = eligible()
    f.observation(PRAPARE_CODE, date(2025, 3, 1), system="SNOMED")
    f.tobacco(date(2025, 3, 1))
    f.observation("88122-7", date(2025, 3, 1), value_code="LA28397-0")  # Hunger Vital Sign
    out = run(SnsRule(), f)
    assert out.numerator.value == "no"
    assert out.numerator.subtype is None
    assert out.numerator.evidence == []


def test_sns_numerator_never_sets_status_without_value() -> None:
    f = eligible()
    f.prapare(date(2025, 3, 1))
    assert run(SnsRule(), f).numerator.subtype is None


# --- doctrine: determinism, order independence, purity, no status reads ---------------------


def _busy_record(as_of: date = AS_OF) -> RecordFactory:
    f = eligible(as_of=as_of)
    for m in (1, 4, 7, 11):
        f.encounter(date(as_of.year, m, 3), "IMP" if m == 7 else "AMB")
    f.tobacco(date(as_of.year, 2, 1), value_code=None)
    f.tobacco(date(as_of.year, 5, 1))
    f.tobacco(date(as_of.year - 1, 5, 1))
    panel = f.prapare(date(as_of.year, 6, 1))
    for code in (PRAPARE_ITEM_C, PRAPARE_ITEM_A, PRAPARE_ITEM_B):
        f.observation(code, date(as_of.year, 6, 1), value_code="x", parent=panel)
    f.prapare(date(as_of.year - 1, 6, 1))
    f.condition("44054006", date(2015, 1, 1))
    f.procedure("73761001", date(2019, 1, 1))
    f.medication("617318", date(as_of.year, 1, 1), status="stopped")
    return f


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_order_independence_over_shuffled_events(rule: TscRule | SnsRule, seed: int) -> None:
    f = _busy_record()
    baseline = run(rule, f)
    rng = random.Random(seed)
    for events in (f.encounters, f.observations, f.conditions, f.procedures, f.medications):
        rng.shuffle(events)
    assert run(rule, f) == baseline


@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_purity_and_determinism(rule: TscRule | SnsRule) -> None:
    f = _busy_record()
    record, ctx = f.build(), f.context()
    first = rule.evaluate(record, ctx, VALUE_SETS)
    second = rule.evaluate(record, ctx, VALUE_SETS)
    assert first == second
    assert record == f.build()  # untouched


@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_unrelated_sections_and_medication_status_never_matter(rule: TscRule | SnsRule) -> None:
    f = eligible()
    if rule.measure_id == "TSC":
        f.tobacco(date(2025, 3, 1))
    else:
        f.prapare(date(2025, 3, 1))
    baseline = run(rule, f)
    f.condition("44054006", date(2015, 1, 1))
    f.procedure("73761001", date(2025, 3, 1))
    f.medication("617318", date(2025, 3, 1), status="active")
    f.medication("617318", date(2025, 4, 1), status="stopped")
    assert run(rule, f) == baseline


def test_two_instances_share_denominator_and_differ_only_in_numerator() -> None:
    f = _busy_record()
    tsc, sns = run(TscRule(), f), run(SnsRule(), f)
    assert tsc.denominator == sns.denominator
    assert tsc.coverage == sns.coverage
    assert tsc.numerator.value == sns.numerator.value == "yes"
    assert {e.code for e in tsc.numerator.evidence} == {TOBACCO_STATUS_CODE}
    assert {e.code for e in sns.numerator.evidence} == {PRAPARE_CODE}


# --- through the real engine (global rules + verdict algebra) --------------------------------


ENGINE = MeasureEngine([TscRule(), SnsRule()], VALUE_SETS)


def _verdict(rule: TscRule | SnsRule, f: RecordFactory) -> str:
    return ENGINE.evaluate_one(f.build(), f.context(), rule.measure_id).verdict


def _screen(rule: TscRule | SnsRule, f: RecordFactory, day: date) -> None:
    if rule.measure_id == "TSC":
        f.tobacco(day)
    else:
        f.prapare(day)


@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_engine_verdicts(rule: TscRule | SnsRule) -> None:
    gap = eligible()
    assert _verdict(rule, gap) == "gap_open"

    closed = eligible()
    _screen(rule, closed, date(2025, 3, 1))
    assert _verdict(rule, closed) == "closed"

    minor = eligible(birth_date=date(2010, 1, 1))
    assert _verdict(rule, minor) == "not_eligible"

    no_visit = RecordFactory()
    assert _verdict(rule, no_visit) == "not_eligible"

    unknown_birth = eligible(birth_date=None)
    assert _verdict(rule, unknown_birth) == "needs_review"

    died_before = eligible()
    died_before.death_date = date(2024, 6, 1)
    assert _verdict(rule, died_before) == "not_eligible"

    died_during = eligible()
    died_during.death_date = date(2025, 6, 1)
    assert _verdict(rule, died_during) == "excluded"

    hospice_in_my = eligible()
    hospice_in_my.condition(HOSPICE_CODE, date(2025, 2, 1))
    assert _verdict(rule, hospice_in_my) == "excluded"

    hospice_before_my = eligible()
    hospice_before_my.condition(HOSPICE_CODE, date(2024, 6, 1))  # >90 days before: nothing
    assert _verdict(rule, hospice_before_my) == "gap_open"

    hospice_just_before = eligible()
    hospice_just_before.condition(HOSPICE_CODE, date(2024, 12, 1))  # E1 (global) -> review
    assert _verdict(rule, hospice_just_before) == "needs_review"


@pytest.mark.parametrize("rule", RULES, ids=RULE_IDS)
def test_engine_uses_rule_version_and_priority(rule: TscRule | SnsRule) -> None:
    f = eligible()
    ev = ENGINE.evaluate_one(f.build(), f.context(), rule.measure_id)
    assert ev.rule_version == rule.rule_version
    assert ev.verdict == "gap_open"
    assert ev.priority_score == 1.0  # star 1 x clinical 1 x retrospective time_pressure 1
    assert ev.exclusions == [] and ev.escalations == []
