"""COL — Colorectal Cancer Screening (demo-grade rule).

Public-source basis: **CMS 2026 Star Ratings Technical Notes, Part C measure C02
"Colorectal Cancer Screening"** (HEDIS COL) via P2's committed corpus, plus NCQA's public
measure summary. Demo-grade, HEDIS-aligned, not NCQA-certified (SPEC section 2, ADR-0005).

Element tags
------------
quoted
    * numerator — "colonoscopy during the measurement period or the nine years prior";
    * numerator — "FOBT during the measurement period" (FIT / gFOBT result, LOINC 57905-2);
    * exclusion — colorectal cancer "any time during the member's history through the end
      of the measurement period" (``colorectal_cancer_snomed`` condition, onset <= as_of);
    * exclusions the engine applies GLOBALLY (never re-implemented here): death during the
      measurement period; hospice during the measurement period.
demo_choice
    * denominator age band 50-75 at Dec 31 of the MY, fixed by SPEC section 2 (the public
      NCQA text for MY2024+ lists 45-75; verify against the corpus before promoting the band
      to ``quoted``);
    * "nine years prior" anchored to calendar years: [Jan 1 of MY-9, as_of];
    * every numerator window ENDS at ``as_of`` (prospective gap detection), so an event
      dated after ``as_of`` never counts; denominator / exclusion windows use MY bounds;
    * FOBT/FIT recorded as a *procedure* (SNOMED 104435004, the literal code) counts in the
      MY — Synthea records FIT this way beside the LOINC 57905-2 observation;
    * E7 (measure scope): any ``colon_ambiguous_snomed`` condition or procedure any time
      through ``as_of`` — could be a total-colectomy or colorectal-cancer signal — so the
      verdict is promoted to needs_review;
    * death before the MY -> denominator ``no`` (``global_rules.died_before_my``).
not_representable (listed in the coverage table)
    * numerator modalities: flexible sigmoidoscopy (MY + 4 prior years), CT colonography
      (MY + 4 prior years), stool DNA with FIT (MY + 2 prior years);
    * exclusions: total colectomy any time; palliative care in the MY; Medicare 66+ in an
      I-SNP or a long-term institutional stay. The 66+ frailty AND advanced-illness
      exclusion is only *hinted* by the global E4 flag (``partial``).

Doctrine: deterministic, pure, order-independent; ``clinical_status`` /
``verification_status`` / ``MedicationRequest.status`` never read; features never enter.
"""

from caregap.measures.context import MeasurementContext
from caregap.measures.engine import RuleOutput
from caregap.measures.evidence import (
    cond_ref,
    conditions_in,
    obs_ref,
    observations_in,
    patient_ref,
    proc_ref,
    procedures_in,
)
from caregap.measures.ids import MeasureId
from caregap.measures.models import (
    Coverage,
    EscalationFlag,
    EvidenceRef,
    ExclusionHit,
    TriResult,
)
from caregap.measures.rules.global_rules import died_before_my
from caregap.measures.value_sets import ValueSets
from caregap.measures.windows import Window, any_time, lookback_years, measurement_year
from caregap.p6.models import PatientRecord, ProcedureEvent

MIN_AGE = 50
MAX_AGE = 75
COLONOSCOPY_LOOKBACK_YEARS = 10
"""MY plus the nine prior calendar years (quoted "nine years prior"; calendar anchoring is demo)."""
FOBT_PROCEDURE_CODE = "104435004"
"""SNOMED "Screening for occult blood in feces (procedure)" — the literal FOBT procedure code."""

COLONOSCOPY_SET = "colonoscopy_proc"
FOBT_SET = "fobt_fit_loinc"
COLORECTAL_CANCER_SET = "colorectal_cancer_snomed"
COLON_AMBIGUOUS_SET = "colon_ambiguous_snomed"

COVERAGE: dict[str, Coverage] = {
    # numerator modalities
    "numerator_colonoscopy_my_plus_9_prior_years": "observable",
    "numerator_fobt_fit_in_my": "observable",
    "numerator_flexible_sigmoidoscopy_my_plus_4_prior_years": "not_representable",
    "numerator_ct_colonography_my_plus_4_prior_years": "not_representable",
    "numerator_sdna_fit_my_plus_2_prior_years": "not_representable",
    # public exclusion criteria
    "exclusion_death_during_measurement_period": "observable",
    "exclusion_hospice_during_measurement_period": "observable",
    "exclusion_colorectal_cancer_any_time": "observable",
    "exclusion_total_colectomy_any_time": "not_representable",
    "exclusion_palliative_care_during_measurement_period": "not_representable",
    "exclusion_isnp_or_long_term_institution_66_plus": "not_representable",
    "exclusion_frailty_and_advanced_illness_66_plus": "partial",
}


def _procedures_with_code(record: PatientRecord, code: str, window: Window) -> list[ProcedureEvent]:
    """Literal-code procedure lookup mirroring ``evidence.observations_with_code``.

    ``evidence.py`` has no procedure counterpart of ``observations_with_code`` (contract gap,
    reported); this keeps the same semantics — code equality, inclusive window, stable sort.
    """
    return sorted(
        (p for p in record.procedures if p.code == code and window.contains(p.performed_date)),
        key=lambda p: (p.performed_date, p.procedure_id),
    )


def _fmt(window: Window) -> str:
    return f"{window.label} [{window.start:%Y-%m-%d} .. {window.end:%Y-%m-%d}]"


class ColRule:
    measure_id: MeasureId = "COL"
    rule_version: str = "col-v1"
    value_set_ids: tuple[str, ...] = (
        COLONOSCOPY_SET,
        FOBT_SET,
        COLORECTAL_CANCER_SET,
        COLON_AMBIGUOUS_SET,
    )

    def evaluate(
        self, record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
    ) -> RuleOutput:
        return RuleOutput(
            denominator=self._denominator(record, ctx),
            numerator=self._numerator(record, ctx, value_sets),
            exclusions=self._exclusions(record, ctx, value_sets),
            coverage=dict(COVERAGE),
            escalations=self._escalations(record, ctx, value_sets),
        )

    # --- denominator: age 50-75 at Dec 31 of the MY (demo_choice band) -----------------------

    @staticmethod
    def _denominator(record: PatientRecord, ctx: MeasurementContext) -> TriResult:
        if died_before_my(record, ctx):
            return TriResult(
                value="no",
                reasons=["died before the measurement year"],
                evidence=[patient_ref("eligibility", event_date=record.patient.death_date)],
                window_start=ctx.my_start,
                window_end=ctx.my_end,
            )
        age = ctx.age_at_my_end
        if age is None:
            return TriResult(
                value="unknown",
                reasons=["birth_date unknown: age at Dec 31 of the MY cannot be computed"],
                window_start=ctx.my_start,
                window_end=ctx.my_end,
            )
        in_band = MIN_AGE <= age <= MAX_AGE
        return TriResult(
            value="yes" if in_band else "no",
            reasons=[
                f"age {age} at Dec 31 of the MY is "
                f"{'within' if in_band else 'outside'} {MIN_AGE}-{MAX_AGE}"
            ],
            evidence=[patient_ref("eligibility", event_date=record.patient.birth_date)],
            window_start=ctx.my_start,
            window_end=ctx.my_end,
        )

    # --- numerator: windows end at as_of ------------------------------------------------------

    @staticmethod
    def _numerator(
        record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
    ) -> TriResult:
        colonoscopy_window = lookback_years(
            ctx.as_of, COLONOSCOPY_LOOKBACK_YEARS, label="MY and nine prior years"
        )
        my = measurement_year(ctx.as_of)

        colonoscopies = procedures_in(record, value_sets, COLONOSCOPY_SET, colonoscopy_window)
        fobt_observations = observations_in(record, value_sets, FOBT_SET, my)
        fobt_procedures = _procedures_with_code(record, FOBT_PROCEDURE_CODE, my)

        reasons: list[str] = []
        evidence: list[EvidenceRef] = []
        if colonoscopies:
            reasons.append(f"colonoscopy in {_fmt(colonoscopy_window)}")
            evidence += [proc_ref(p, "numerator") for p in colonoscopies]
        if fobt_observations or fobt_procedures:
            reasons.append(f"FOBT/FIT in {_fmt(my)}")
            evidence += [obs_ref(o, "numerator") for o in fobt_observations]
            evidence += [proc_ref(p, "numerator") for p in fobt_procedures]
        if evidence:
            satisfied = colonoscopy_window if colonoscopies else my
            return TriResult(
                value="yes",
                reasons=reasons,
                evidence=evidence,
                window_start=satisfied.start,
                window_end=satisfied.end,
            )
        return TriResult(
            value="no",
            reasons=[
                f"no colonoscopy in {_fmt(colonoscopy_window)}",
                f"no FOBT/FIT observation or procedure in {_fmt(my)}",
            ],
            window_start=colonoscopy_window.start,
            window_end=colonoscopy_window.end,
        )

    # --- coded exclusions (measure scope; death / hospice are global) ------------------------

    @staticmethod
    def _exclusions(
        record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
    ) -> list[ExclusionHit]:
        window = any_time(ctx.as_of)
        cancer = [
            c
            for c in conditions_in(record, value_sets, COLORECTAL_CANCER_SET)
            if window.contains(c.onset_date)
        ]
        if not cancer:
            return []
        return [
            ExclusionHit(
                category="colorectal_cancer",
                source="quoted",
                window_label=window.label,
                evidence=[cond_ref(c, "exclusion") for c in cancer],
            )
        ]

    # --- escalations (measure scope): E7 ambiguous colon code --------------------------------

    @staticmethod
    def _escalations(
        record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
    ) -> list[EscalationFlag]:
        window = any_time(ctx.as_of)
        refs: list[EvidenceRef] = [
            cond_ref(c, "escalation")
            for c in conditions_in(record, value_sets, COLON_AMBIGUOUS_SET)
            if window.contains(c.onset_date)
        ]
        refs += [
            proc_ref(p, "escalation")
            for p in procedures_in(record, value_sets, COLON_AMBIGUOUS_SET, window)
        ]
        if not refs:
            return []
        return [
            EscalationFlag(
                kind="E7",
                scope="measure",
                reason=(
                    "ambiguous colon code on record (possible total colectomy or "
                    "colorectal cancer signal)"
                ),
                evidence=refs,
            )
        ]
