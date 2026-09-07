"""Global rules applied to every measure (SPEC section 2, "Global rules").

Exclusions (quoted from the public Technical Notes text):
- died during the measurement period: ``my_start <= death_date <= as_of``;
- hospice during the measurement period: any hospice procedure / encounter type / condition
  whose start OR recorded end (``performed_end_date`` / encounter ``end_ts``) falls in
  [my_start, as_of] - an episode that starts before the MY and ends inside it counts. Hospice
  entirely BEFORE the MY (or with no recorded end) is deterministically NOT an exclusion.

Escalations (global scope — hold all drafting until a human resolves them):
- E1: a hospice event within 90 days before my_start (possible continuation into the MY);
- E4: dementia diagnosis active in the MY (abatement null or after Jan 1 of the MY) OR a
  dementia medication authored in the MY or the prior year, plus an inpatient/ED encounter in
  the MY, for age >= 66 (advanced-illness hint; never computed as an exclusion).

Death before the MY makes the patient ``not_eligible`` for everything — handled by the
denominator helpers each rule uses (``alive_for_my``).
"""

from caregap.measures.context import MeasurementContext
from caregap.measures.evidence import (
    cond_ref,
    condition_active_in,
    conditions_in,
    enc_ref,
    encounters_in,
    encounters_overlapping,
    med_ref,
    medications_in,
    patient_ref,
    proc_ref,
    procedures_overlapping,
)
from caregap.measures.models import EscalationFlag, EvidenceRef, ExclusionHit
from caregap.measures.value_sets import ValueSets
from caregap.measures.windows import Window, days_before, measurement_year
from caregap.p6.models import PatientRecord

HOSPICE_LOOKBACK_DAYS = 90


def died_before_my(record: PatientRecord, ctx: MeasurementContext) -> bool:
    death = record.patient.death_date
    return death is not None and death < ctx.my_start


def hospice_events(
    record: PatientRecord, value_sets: ValueSets, window: Window
) -> list[EvidenceRef]:
    """Hospice evidence in a window across the three places Synthea records it.

    A procedure counts when its ``performed_date`` OR ``performed_end_date`` falls in the
    window; an encounter when its ``start_date`` OR ``end_ts`` date does (an episode that
    started before the window and ended inside it). Conditions count by ``onset_date``.
    """
    refs: list[EvidenceRef] = []
    refs += [
        proc_ref(p, "exclusion")
        for p in procedures_overlapping(record, value_sets, "hospice_snomed", window)
    ]
    refs += [
        cond_ref(c, "exclusion")
        for c in conditions_in(record, value_sets, "hospice_snomed")
        if window.contains(c.onset_date)
    ]
    hospice_codes = value_sets.codes("hospice_snomed")
    refs += [
        enc_ref(e, "exclusion")
        for e in encounters_overlapping(record, window)
        if e.type_code is not None and e.type_code in hospice_codes
    ]
    return refs


def global_exclusions(
    record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
) -> list[ExclusionHit]:
    hits: list[ExclusionHit] = []
    death = record.patient.death_date
    if death is not None and ctx.my_start <= death <= ctx.as_of:
        hits.append(
            ExclusionHit(
                category="died_during_measurement_period",
                source="quoted",
                window_label="measurement period",
                evidence=[patient_ref("exclusion", event_date=death)],
            )
        )
    my = measurement_year(ctx.as_of)
    hospice = hospice_events(record, value_sets, my)
    if hospice:
        hits.append(
            ExclusionHit(
                category="hospice_during_measurement_period",
                source="quoted",
                window_label=my.label,
                evidence=hospice,
            )
        )
    return hits


def global_escalations(
    record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
) -> list[EscalationFlag]:
    flags: list[EscalationFlag] = []
    # E1: hospice shortly before the MY may continue into it.
    lookback = Window(
        start=days_before(ctx.my_start, HOSPICE_LOOKBACK_DAYS),
        end=days_before(ctx.my_start, 1),
        label=f"{HOSPICE_LOOKBACK_DAYS} days before MY",
    )
    prior_hospice = hospice_events(record, value_sets, lookback)
    if prior_hospice and not hospice_events(record, value_sets, measurement_year(ctx.as_of)):
        flags.append(
            EscalationFlag(
                kind="E1",
                scope="global",
                reason="hospice event within 90 days before the measurement year",
                evidence=[e.model_copy(update={"role": "escalation"}) for e in prior_hospice],
            )
        )
    # E4: advanced-illness hint (dementia + acute care in the MY, age >= 66). demo_choice:
    # the dementia diagnosis must be active in the MY; a dementia medication counts when
    # authored in the MY or the prior year (Synthea authors most requests once).
    if ctx.age_at_my_end is not None and ctx.age_at_my_end >= 66:
        my = measurement_year(ctx.as_of)
        meds_window = Window(start=ctx.prior_my_start, end=ctx.as_of, label="MY or prior year")
        dementia = [
            cond_ref(c, "escalation")
            for c in conditions_in(record, value_sets, "dementia_snomed")
            if condition_active_in(c, my)
        ]
        dementia += [
            med_ref(m, "escalation")
            for m in medications_in(record, value_sets, "dementia_meds_rxnorm", meds_window)
        ]
        acute = [
            enc_ref(e, "escalation")
            for e in encounters_in(record, my)
            if e.encounter_class in {"IMP", "EMER"}
        ]
        if dementia and acute:
            flags.append(
                EscalationFlag(
                    kind="E4",
                    scope="global",
                    reason="dementia with inpatient/ED care in the MY (advanced-illness hint)",
                    evidence=[*dementia, *acute[:3]],
                )
            )
    return flags
