"""BCS — Breast Cancer Screening (CMS 2026 Star Ratings Technical Notes, Part C measure C01).

Public-source basis
-------------------
CMS 2026 Star Ratings Technical Notes, measure **C01 Breast Cancer Screening** (HEDIS BCS-E
lineage), as summarised in ``docs/SPEC.md`` section 2 and carried with verbatim citations in
``rules/json/bcs.json`` (built from P2's committed public corpus). Demo-grade, HEDIS-aligned,
**not NCQA-certified**.

Element tags
------------
quoted
    * denominator sex: female;
    * denominator age: 52-74, evaluated at Dec 31 of the measurement year (MY).
      NOTE — ``normative_quote`` conflict: the C01 *Description* text states the age range
      as 50-74 while the C01 *Metric* text states 52-74. This rule implements **52-74**
      (the SPEC table; the Metric wording is the one the rate is computed from — the
      Description reflects the two-year screening interval that starts at 50). The rule
      JSON records both quotes so the UI can surface the conflict.
    * exclusions: died during the MY; hospice during the MY — both are GLOBAL rules
      (``rules/global_rules.py``) and are deliberately NOT re-implemented here;
      bilateral mastectomy is a quoted criterion that is ``not_representable``.
demo_choice
    * numerator window: Oct 1 of MY-2 through ``as_of`` (the public "27 months" summary of
      the "two years prior" lookback; :func:`caregap.measures.windows.bcs_window`);
    * "mammogram" = any procedure whose code is in P6's ``mammogram_proc`` SNOMED value set.
not_representable
    * bilateral mastectomy (no code in the committed Synthea scan; no value set);
    * advanced illness & frailty (66+) — ``partial``: the global E4 flag only *hints*
      (dementia + inpatient/ED care), it is never computed as an exclusion;
    * palliative care during the MY (HEDIS BCS-E criterion; no codes in the scan).

Doctrine: deterministic and pure; order-independent over shuffled events; every date is
compared against ``ctx`` / ``windows``; ``clinical_status`` / ``verification_status`` /
``MedicationEvent.status`` are never read; the record is read only through
:mod:`caregap.measures.evidence`. No measure-scoped escalations (E1/E4 are global).
"""

from caregap.measures.context import MeasurementContext
from caregap.measures.engine import RuleOutput
from caregap.measures.evidence import patient_ref, proc_ref, procedures_in
from caregap.measures.ids import MeasureId
from caregap.measures.models import Coverage, TriResult
from caregap.measures.rules.global_rules import died_before_my
from caregap.measures.tri import Tri
from caregap.measures.value_sets import ValueSets
from caregap.measures.windows import bcs_window
from caregap.p6.models import PatientRecord

#: C01 Metric text (quoted). The C01 Description says 50-74 — see the module docstring.
AGE_MIN = 52
AGE_MAX = 74

#: FHIR AdministrativeGender values as P6 canonicalises them. Anything outside
#: ``FEMALE`` / ``KNOWN_NOT_FEMALE`` (e.g. ``"unknown"``, ``""``) leaves sex undetermined, so
#: the denominator is ``unknown`` (needs_review) rather than a silent ``not_eligible``.
FEMALE = "female"
KNOWN_NOT_FEMALE: frozenset[str] = frozenset({"male", "other"})

MAMMOGRAM_SET = "mammogram_proc"

#: Every public exclusion criterion for C01 and how observable it is in this engine.
COVERAGE: dict[str, Coverage] = {
    # Global rules (quoted): computed in rules/global_rules.py for every measure.
    "died_during_measurement_period": "observable",
    "hospice_during_measurement_period": "observable",
    # Quoted for C01 but no code exists in the Synthea scan / value sets.
    "bilateral_mastectomy": "not_representable",
    # Global E4 raises a *hint* (dementia + inpatient/ED at 66+); never an exclusion.
    "advanced_illness_and_frailty": "partial",
    # HEDIS BCS-E criterion; no palliative-care codes in the scan.
    "palliative_care": "not_representable",
}


def _sex_tri(sex: str) -> tuple[Tri, str]:
    if sex == FEMALE:
        return "yes", "sex female"
    if sex in KNOWN_NOT_FEMALE:
        return "no", f"sex {sex} (BCS denominator is female only)"
    return "unknown", f"sex {sex!r} not recorded as female/male"


def _age_tri(age_at_my_end: int | None) -> tuple[Tri, str]:
    if age_at_my_end is None:
        return "unknown", "birth_date unknown"
    if AGE_MIN <= age_at_my_end <= AGE_MAX:
        return "yes", f"age {age_at_my_end} at Dec 31 of the MY within {AGE_MIN}-{AGE_MAX}"
    return "no", f"age {age_at_my_end} at Dec 31 of the MY outside {AGE_MIN}-{AGE_MAX}"


def _combine(*parts: Tri) -> Tri:
    """Three-valued AND: any ``no`` wins, else any ``unknown``, else ``yes``."""
    if "no" in parts:
        return "no"
    if "unknown" in parts:
        return "unknown"
    return "yes"


class BcsRule:
    """C01 Breast Cancer Screening: female 52-74; mammogram in [Oct 1 MY-2, as_of]."""

    measure_id: MeasureId = "BCS"
    rule_version: str = "bcs-v1"

    def evaluate(
        self, record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
    ) -> RuleOutput:
        return RuleOutput(
            denominator=self._denominator(record, ctx),
            numerator=self._numerator(record, ctx, value_sets),
            exclusions=[],  # none observable beyond the global death/hospice rules
            coverage=dict(COVERAGE),
            escalations=[],  # no measure-scoped escalations for C01
        )

    @staticmethod
    def _denominator(record: PatientRecord, ctx: MeasurementContext) -> TriResult:
        patient = record.patient
        if died_before_my(record, ctx):
            return TriResult(
                value="no",
                reasons=["died before the measurement year"],
                evidence=[patient_ref("eligibility", event_date=patient.death_date)],
                window_start=ctx.my_start,
                window_end=ctx.my_end,
            )
        sex_value, sex_reason = _sex_tri(patient.sex)
        age_value, age_reason = _age_tri(ctx.age_at_my_end)
        return TriResult(
            value=_combine(sex_value, age_value),
            reasons=[sex_reason, age_reason],
            evidence=[patient_ref("eligibility", event_date=patient.birth_date)],
            window_start=ctx.my_start,
            window_end=ctx.my_end,
        )

    @staticmethod
    def _numerator(
        record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
    ) -> TriResult:
        window = bcs_window(ctx.as_of)
        mammograms = procedures_in(record, value_sets, MAMMOGRAM_SET, window)
        if mammograms:
            most_recent = mammograms[-1]  # procedures_in sorts by (date, id)
            return TriResult(
                value="yes",
                reasons=[
                    f"mammogram on {most_recent.performed_date.isoformat()} in {window.label}"
                ],
                evidence=[proc_ref(p, "numerator") for p in mammograms],
                window_start=window.start,
                window_end=window.end,
            )
        return TriResult(
            value="no",
            reasons=[f"no mammogram in {window.label}"],
            window_start=window.start,
            window_end=window.end,
        )
