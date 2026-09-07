"""SPD - Statin Use in Persons With Diabetes (CMS Star Ratings id D12; SUPD-style).

Public-source basis
-------------------
CMS 2026 Star Ratings Technical Notes, Part D measure **D12: Statin Use in Persons with
Diabetes** (the PQA-endorsed SUPD measure: "the percent of Medicare Part D beneficiaries 40-75
years old who were dispensed at least two diabetes medication fills and received a statin
medication fill during the measurement period"), text via P2's committed corpus (the tagged
rule text with citation ids lives in ``rules/json/spd.json``), plus the public NCQA HEDIS SPD
measure summary. Demo-grade, HEDIS-aligned, NOT NCQA-certified. ``docs/SPEC.md`` section 2 is
the row this module implements.

Element tags
------------
quoted
  * denominator age band: 40-75, age at Dec 31 of the measurement year (MY), any sex;
  * numerator concept: a statin "fill" during the measurement period - unlike SPC (C19), the
    public D12 text carries NO intensity requirement, so a statin of ANY intensity counts;
  * exclusions: ESRD or dialysis "at any time during the measurement period" (the window
    itself is a demo_choice, see below); hospice during the MY (applied by the GLOBAL rules in
    ``rules/global_rules.py``, never re-implemented here).
demo_choice
  * "diabetes" is identified exactly as EED (C11) does it: a ``diabetes_snomed`` condition
    active in [Jan 1 of MY-1, Dec 31 of MY] (onset on/before the window end, abatement null or
    on/after the window start). The public "two diabetes medication fills" denominator path is
    NOT represented (P6 carries no pharmacy claims). A ``prediabetes_trap_snomed`` code never
    counts, even if it were to leak into ``diabetes_snomed``.
  * NOT in the SPC denominator (product choice - no double outreach): an ``ascvd_snomed``
    condition with ``onset_date <= my_end`` AND SPC-eligible age/sex makes the SPD denominator
    ``no`` with the reason ``routed_to_spc``; the SPC rule owns that patient.
  * "dispensed" is approximated by ``MedicationRequest`` rows exactly as SPC does (the
    on-therapy helper is imported from ``rules/statin.py``): on therapy iff a ``statin_rxnorm``
    request is authored in [my_start, as_of], or authored before my_start with
    ``status == "active"``; ``stopped`` / ``cancelled`` / ``entered-in-error`` never count.
    ADR-0002: SPC, this rule, and E3 are the ONLY readers of ``MedicationEvent.status``.
  * ESRD / dialysis exclusion window = [Jan 1 of MY-1, Dec 31 of MY] (MY or the year prior,
    mirroring SPC) - WIDER than the quoted D12 text ("during the measurement period"), so the
    hits are tagged ``demo_choice``; the numerator window ends at ``as_of``.
  * pregnancy (MY or the year prior, mirroring SPC; the SPEC SPD row lists it as demo): a
    ``pregnancy_snomed`` condition active in the window OR a LOINC 82810-3 "Pregnancy status"
    observation answered SNOMED 77386006 in the window (``pregnancy_status_positive``) - the
    shared :func:`evidence.pregnancy_hits`. The public D12 criterion is "Pregnancy, Lactation,
    and Fertility"; only the pregnancy part is observable (coverage ``partial``).
  * E3 ``medication_status_conflict`` (measure scope, shared with SPC): a statin authored in
    the MY with status stopped or cancelled, raised even when another statin closes the gap.
not_representable (listed in the coverage table, never computed)
  * the pharmacy-claims denominator ("at least two diabetes medication fills") and its
    companion exclusion for members on diabetes medication without a diabetes diagnosis who
    have PCOS / gestational / steroid-induced diabetes (moot: this denominator is dx-based);
  * in-vitro fertilisation / clomiphene / lactation: no codes in the committed Synthea scan;
  * cirrhosis; myalgia / myositis / myopathy / rhabdomyolysis; palliative care; I-SNP /
    long-term institutional residence (66+).
  * frailty plus advanced illness (66+) is ``partial``: only the global E4 hint.
documented non-members (intentionally no code set)
  * prediabetes is a denominator trap, never an exclusion; fibromyalgia is NOT a public
    exclusion criterion (it is not the myalgia family) - both named so nobody adds them.

Doctrine: deterministic, pure, order-independent over shuffled events; every date compared
against ``ctx`` / ``windows``; ``clinical_status`` / ``verification_status`` never read; the
record is read only through :mod:`caregap.measures.evidence`; features never enter.
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

# Shared statin machinery lives in rules/statin.py (reused by SPC - not copied): the
# on-therapy rule and E3 are the ONLY readers of MedicationEvent.status (ADR-0002).
from caregap.measures.rules.statin import (
    e3_escalations,
    exclusion_window,
    on_therapy,
    spc_age_sex,
    tri_all,
)
from caregap.measures.tri import Tri
from caregap.measures.value_sets import ValueSets
from caregap.measures.windows import Window, any_time
from caregap.p6.models import ConditionEvent, PatientRecord

#: D12 text (quoted): 40-75 years old at Dec 31 of the MY, any sex.
AGE_BAND: tuple[int, int] = (40, 75)

#: Denominator reason emitted when SPC owns the patient (demo_choice: no double outreach).
ROUTED_TO_SPC = "routed_to_spc"
DIABETES_WINDOW_LABEL = "MY or prior year"

DIABETES_SET = "diabetes_snomed"
PREDIABETES_TRAP_SET = "prediabetes_trap_snomed"
ASCVD_SET = "ascvd_snomed"
STATIN_SET = "statin_rxnorm"
ESRD_SET = "esrd_snomed"
DIALYSIS_SET = "dialysis_snomed"

#: Every public SPD / SUPD exclusion criterion and how far this demo rule can observe it.
COVERAGE: dict[str, Coverage] = {
    "died_during_measurement_period": "observable",  # global rule (quoted)
    "hospice_during_measurement_period": "observable",  # global rule (quoted)
    "esrd": "observable",  # esrd_snomed condition active in MY or prior year (demo_choice)
    "dialysis": "observable",  # dialysis_snomed procedure in MY or prior year (demo_choice)
    "pregnancy": "observable",  # condition active / 82810-3 status in MY or prior year (demo)
    "in_vitro_fertilization": "not_representable",
    "clomiphene_dispensed": "not_representable",
    "cirrhosis": "not_representable",
    "myalgia_myositis_myopathy_rhabdomyolysis": "not_representable",
    "palliative_care": "not_representable",
    "frailty_and_advanced_illness_66_plus": "partial",  # global E4 hint only
    "institutional_snp_or_long_term_institution_66_plus": "not_representable",
    # Companion of the pharmacy-claims denominator path (not represented: dx-based here).
    "diabetes_medication_only_with_pcos_gestational_or_steroid_induced": "not_representable",
}
#: Criteria people expect to see that the public measure does NOT list (never coded).
NON_EXCLUSIONS: tuple[str, ...] = ("prediabetes", "fibromyalgia")


def _age_band(ctx: MeasurementContext) -> tuple[Tri, str]:
    age = ctx.age_at_my_end
    if age is None:
        return "unknown", "birth_date unknown"
    low, high = AGE_BAND
    if low <= age <= high:
        return "yes", f"aged {age} at MY end: within {low}-{high}"
    return "no", f"aged {age} at MY end: outside {low}-{high}"


def _diabetes_window(ctx: MeasurementContext) -> Window:
    return Window(start=ctx.prior_my_start, end=ctx.my_end, label=DIABETES_WINDOW_LABEL)


def _diabetes_conditions(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets
) -> list[ConditionEvent]:
    """Diabetes as EED identifies it; a prediabetes trap code never counts."""
    window = _diabetes_window(ctx)
    trap = vs.codes(PREDIABETES_TRAP_SET)
    return [
        c
        for c in conditions_in(record, vs, DIABETES_SET)
        if c.code not in trap and condition_active_in(c, window)
    ]


def _spc_routing(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets
) -> tuple[Tri, str, list[EvidenceRef]]:
    """``yes`` when SPD keeps the patient; ``no`` (``routed_to_spc``) when SPC owns them."""
    ascvd = [c for c in conditions_in(record, vs, ASCVD_SET) if c.onset_date <= ctx.my_end]
    if not ascvd:
        return "yes", "no ASCVD condition with onset on/before MY end (not in SPC denominator)", []
    refs = [cond_ref(c, "eligibility") for c in ascvd]
    spc_eligible, spc_reason = spc_age_sex(record, ctx)
    if spc_eligible == "yes":
        return "no", ROUTED_TO_SPC, refs
    if spc_eligible == "no":
        return "yes", f"ASCVD present but not SPC-eligible ({spc_reason}); SPD keeps patient", refs
    return "unknown", f"ASCVD present; SPC eligibility unknown ({spc_reason})", refs


def _denominator(record: PatientRecord, ctx: MeasurementContext, vs: ValueSets) -> TriResult:
    values: list[Tri] = []
    reasons: list[str] = []
    evidence: list[EvidenceRef] = []

    if died_before_my(record, ctx):
        values.append("no")
        reasons.append("died before the measurement year")
        evidence.append(patient_ref("eligibility", event_date=record.patient.death_date))

    age_value, age_reason = _age_band(ctx)
    values.append(age_value)
    reasons.append(age_reason)
    evidence.append(patient_ref("eligibility", event_date=record.patient.birth_date))

    diabetes = _diabetes_conditions(record, ctx, vs)
    if diabetes:
        values.append("yes")
        reasons.append(f"diabetes condition active in {DIABETES_WINDOW_LABEL}")
        evidence.extend(cond_ref(c, "eligibility") for c in diabetes)
    else:
        values.append("no")
        reasons.append(f"no diabetes condition active in {DIABETES_WINDOW_LABEL}")

    routing_value, routing_reason, routing_refs = _spc_routing(record, ctx, vs)
    values.append(routing_value)
    reasons.append(routing_reason)
    evidence.extend(routing_refs)

    return TriResult(
        value=tri_all(values),
        reasons=reasons,
        evidence=evidence,
        window_start=ctx.prior_my_start,
        window_end=ctx.my_end,
    )


def _numerator(record: PatientRecord, ctx: MeasurementContext, vs: ValueSets) -> TriResult:
    statins = [
        m for m in medications_in(record, vs, STATIN_SET, any_time(ctx.as_of)) if on_therapy(m, ctx)
    ]
    value: Tri = "yes" if statins else "no"
    reason = (
        "statin of any intensity on therapy in the measurement year"
        if statins
        else "no statin therapy in the measurement year"
    )
    return TriResult(
        value=value,
        reasons=[reason],
        evidence=[med_ref(m, "numerator") for m in statins],
        window_start=ctx.my_start,
        window_end=ctx.as_of,
    )


def _exclusions(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets
) -> list[ExclusionHit]:
    window = exclusion_window(ctx)
    hits: list[ExclusionHit] = []

    # demo_choice: the quoted D12 text says "during the measurement period"; the demo mirrors
    # SPC's MY-or-prior-year window so one statin patient is judged the same way twice.
    esrd = [
        cond_ref(c, "exclusion")
        for c in conditions_in(record, vs, ESRD_SET)
        if condition_active_in(c, window)
    ]
    if esrd:
        hits.append(
            ExclusionHit(
                category="esrd", source="demo_choice", window_label=window.label, evidence=esrd
            )
        )

    dialysis = [proc_ref(p, "exclusion") for p in procedures_in(record, vs, DIALYSIS_SET, window)]
    if dialysis:
        hits.append(
            ExclusionHit(
                category="dialysis",
                source="demo_choice",
                window_label=window.label,
                evidence=dialysis,
            )
        )
    hits.extend(pregnancy_hits(record, vs, window, condition_source="demo_choice"))
    return hits


def _escalations(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets
) -> list[EscalationFlag]:
    """E3 exactly as SPC raises it (same statin set, same MY window, same statuses)."""
    return e3_escalations(record, ctx, vs)


class SpdRule:
    """D12 Statin Use in Persons With Diabetes (demo-grade, SUPD-style)."""

    measure_id: MeasureId = "SPD"
    rule_version: str = "spd-v1"

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
