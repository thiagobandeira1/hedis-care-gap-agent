"""SPC - Statin Therapy for Patients With Cardiovascular Disease (CMS Star Ratings id C19).

Public-source basis
-------------------
CMS 2026 Star Ratings Technical Notes, Part C measure **C19: Statin Therapy for Patients with
Cardiovascular Disease** (text via P2's committed corpus; the tagged rule text with citation
ids lives in ``rules/json/spc.json``), the public NCQA HEDIS SPC measure summary, and the
public ACC/AHA statin-intensity table (``value_sets/statin_intensity.json``). Demo-grade,
HEDIS-aligned, NOT NCQA-certified.

Element tags
------------
quoted
  * denominator age/sex bands: males 21-75 and females 40-75, age at Dec 31 of the MY;
  * denominator concept: members "identified as having clinical ASCVD";
  * numerator concept: at least one high-intensity or moderate-intensity statin during the
    measurement year;
  * exclusions: ESRD or dialysis during the MY or the year prior; pregnancy during the MY
    or the year prior (``pregnancy_snomed`` condition active in the window - the shared
    :func:`evidence.pregnancy_hits`); died / hospice during the MY (GLOBAL rules, not here).
demo_choice
  * ASCVD = any ``ascvd_snomed`` condition with ``onset_date <= my_end`` (abatement ignored).
    The public two-year ASCVD event / diagnosis look-back is deliberately NOT implemented.
  * "dispensed" is approximated by ``MedicationRequest`` rows (P6 carries no pharmacy
    claims): on therapy iff a ``statin_rxnorm`` request is authored in [my_start, as_of], or
    authored before my_start with ``status == "active"``; ``stopped`` / ``cancelled`` /
    ``entered-in-error`` never count. ADR-0002: this rule (with SPD) and E3 are the ONLY
    readers of ``MedicationEvent.status``.
  * intensity comes from ``statin_intensity`` (public ACC/AHA table). A statin whose code has
    no intensity entry makes the numerator ``unknown`` ("statin intensity unknown").
  * only low-intensity statins on therapy -> numerator ``no``, subtype ``low_intensity_only``.
  * exclusion window = [Jan 1 of MY-1, Dec 31 of MY], built from MY bounds; the numerator
    window ends at ``as_of``.
  * pregnancy is also detected from LOINC 82810-3 "Pregnancy status" answered SNOMED 77386006
    dated in the exclusion window (``pregnancy_status_positive``, as CBP / SPD).
  * the statin machinery (age/sex band, on-therapy rule, exclusion window, E3) lives in
    ``rules/statin.py`` and is shared with SPD.
  * E3 ``medication_status_conflict``: a statin authored in the MY with status stopped or
    cancelled, raised even when another statin closes the numerator.
not_representable (listed in the coverage table, never computed)
  * cirrhosis; myalgia / myositis / myopathy / rhabdomyolysis; in-vitro fertilisation;
    clomiphene dispensing; palliative care; I-SNP / long-term institutional residence (66+).
  * frailty plus advanced illness (66+) is ``partial``: only the global E4 hint.
documented non-member (intentionally no code set)
  * fibromyalgia is NOT a public SPC exclusion criterion; it is named here so nobody adds it.
"""

from caregap.measures.context import MeasurementContext
from caregap.measures.engine import RuleOutput
from caregap.measures.evidence import (
    cond_ref,
    condition_active_in,
    conditions_in,
    med_ref,
    medications_in,
    patient_ref,
    pregnancy_hits,
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
from caregap.measures.rules.statin import (
    ACTIVE_STATUS,
    CONFLICT_STATUSES,
    EXCLUSION_WINDOW_LABEL,
    FEMALE_AGE_BAND,
    MALE_AGE_BAND,
    NEVER_COUNT_STATUSES,
    e3_escalations,
    exclusion_window,
    on_therapy,
    spc_age_sex,
    tri_all,
)
from caregap.measures.tri import Tri
from caregap.measures.value_sets import StatinIntensity, ValueSets
from caregap.measures.windows import any_time
from caregap.p6.models import PatientRecord

__all__ = [
    "ACTIVE_STATUS",
    "CONFLICT_STATUSES",
    "EXCLUSION_WINDOW_LABEL",
    "FEMALE_AGE_BAND",
    "MALE_AGE_BAND",
    "NEVER_COUNT_STATUSES",
]

QUALIFYING_INTENSITIES: frozenset[StatinIntensity] = frozenset({"moderate", "high"})

LOW_INTENSITY_ONLY = "low_intensity_only"

#: Every public SPC exclusion criterion and how far this demo rule can observe it.
COVERAGE: dict[str, Coverage] = {
    "died_during_measurement_period": "observable",  # global rule (quoted)
    "hospice_during_measurement_period": "observable",  # global rule (quoted)
    "esrd": "observable",  # esrd_snomed condition active in MY or prior year
    "dialysis": "observable",  # dialysis_snomed procedure in MY or prior year
    "pregnancy": "observable",  # pregnancy_snomed condition active in MY or prior year
    "cirrhosis": "not_representable",
    "myalgia_myositis_myopathy_rhabdomyolysis": "not_representable",
    "in_vitro_fertilization": "not_representable",
    "clomiphene_dispensed": "not_representable",
    "palliative_care": "not_representable",
    "frailty_and_advanced_illness_66_plus": "partial",  # global E4 hint only
    "institutional_snp_or_long_term_institution_66_plus": "not_representable",
}
#: Criteria people expect to see that the public measure does NOT list (never coded).
NON_EXCLUSIONS: tuple[str, ...] = ("fibromyalgia",)


def _denominator(record: PatientRecord, ctx: MeasurementContext, vs: ValueSets) -> TriResult:
    values: list[Tri] = []
    reasons: list[str] = []
    evidence: list[EvidenceRef] = []

    if died_before_my(record, ctx):
        values.append("no")
        reasons.append("died before the measurement year")
        evidence.append(patient_ref("eligibility", event_date=record.patient.death_date))

    age_sex_value, age_sex_reason = spc_age_sex(record, ctx)
    values.append(age_sex_value)
    reasons.append(age_sex_reason)
    evidence.append(patient_ref("eligibility", event_date=record.patient.birth_date))

    # demo_choice: any ASCVD condition with onset on/before MY end, abatement ignored; the
    # public two-year ASCVD event / diagnosis look-back is NOT implemented.
    ascvd = [c for c in conditions_in(record, vs, "ascvd_snomed") if c.onset_date <= ctx.my_end]
    if ascvd:
        values.append("yes")
        reasons.append("ASCVD condition with onset on/before MY end")
        evidence.extend(cond_ref(c, "eligibility") for c in ascvd)
    else:
        values.append("no")
        reasons.append("no ASCVD condition with onset on/before MY end")

    return TriResult(
        value=tri_all(values),
        reasons=reasons,
        evidence=evidence,
        window_start=ctx.my_start,
        window_end=ctx.my_end,
    )


def _numerator(record: PatientRecord, ctx: MeasurementContext, vs: ValueSets) -> TriResult:
    intensity_set = vs["statin_intensity"]
    statins = [
        (m, intensity_set.intensity_of(m.code))
        for m in medications_in(record, vs, "statin_rxnorm", any_time(ctx.as_of))
        if on_therapy(m, ctx)
    ]
    qualifying = [m for m, intensity in statins if intensity in QUALIFYING_INTENSITIES]
    unknown = [m for m, intensity in statins if intensity is None]
    low = [m for m, intensity in statins if intensity == "low"]

    value: Tri
    reason: str
    subtype: str | None = None
    if qualifying:
        value, reason = "yes", "moderate/high-intensity statin on therapy in the measurement year"
        chosen = qualifying
    elif unknown:
        value, reason = "unknown", "statin intensity unknown"
        chosen = [m for m, _ in statins]
    elif low:
        value, reason = "no", "only low-intensity statin therapy in the measurement year"
        subtype = LOW_INTENSITY_ONLY
        chosen = low
    else:
        value, reason = "no", "no statin therapy in the measurement year"
        chosen = []

    return TriResult(
        value=value,
        reasons=[reason],
        evidence=[med_ref(m, "numerator") for m in chosen],
        subtype=subtype,
        window_start=ctx.my_start,
        window_end=ctx.as_of,
    )


def _exclusions(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets
) -> list[ExclusionHit]:
    window = exclusion_window(ctx)
    hits: list[ExclusionHit] = []

    esrd = [
        cond_ref(c, "exclusion")
        for c in conditions_in(record, vs, "esrd_snomed")
        if condition_active_in(c, window)
    ]
    if esrd:
        hits.append(
            ExclusionHit(category="esrd", source="quoted", window_label=window.label, evidence=esrd)
        )

    dialysis = [
        proc_ref(p, "exclusion") for p in procedures_in(record, vs, "dialysis_snomed", window)
    ]
    if dialysis:
        hits.append(
            ExclusionHit(
                category="dialysis",
                source="quoted",
                window_label=window.label,
                evidence=dialysis,
            )
        )

    hits.extend(pregnancy_hits(record, vs, window, condition_source="quoted"))
    return hits


def _escalations(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets
) -> list[EscalationFlag]:
    """E3 ``medication_status_conflict`` (``rules/statin.py``, shared with SPD)."""
    return e3_escalations(record, ctx, vs)


class SpcRule:
    """C19 Statin Therapy for Patients With Cardiovascular Disease (demo-grade)."""

    measure_id: MeasureId = "SPC"
    rule_version: str = "spc-v1"

    def evaluate(
        self, record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
    ) -> RuleOutput:
        return RuleOutput(
            denominator=_denominator(record, ctx, value_sets),
            numerator=_numerator(record, ctx, value_sets),
            exclusions=_exclusions(record, ctx, value_sets),
            coverage=dict(COVERAGE),
            escalations=_escalations(record, ctx, value_sets),
        )
