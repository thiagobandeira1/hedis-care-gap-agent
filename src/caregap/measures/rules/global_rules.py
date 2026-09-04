"""Global rules applied to every measure (SPEC section 2, "Global rules").

Exclusions (quoted from the public Technical Notes text):
- died during the measurement period: ``my_start <= death_date <= as_of``;
- hospice during the measurement period: any hospice procedure / encounter type / condition
  dated in [my_start, as_of]. Hospice BEFORE the MY is deterministically NOT an exclusion.

Escalations (global scope — hold all drafting until a human resolves them):
- E1: a hospice event within 90 days before my_start (possible continuation into the MY);
- E4: dementia diagnosis or dementia medication plus an inpatient/ED encounter in the MY for
  age >= 66 (advanced-illness hint; never computed as an exclusion).

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
    medications_in,
    patient_ref,
    proc_ref,
    procedures_in,
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
    """Hospice evidence in a window across the three places Synthea records it."""
    refs: list[EvidenceRef] = []
    refs += [
        proc_ref(p, "exclusion")
        for p in procedures_in(record, value_sets, "hospice_snomed", window)
    ]
    refs += [
        cond_ref(c, "exclusion")
        for c in conditions_in(record, value_sets, "hospice_snomed")
        if window.contains(c.onset_date)
    ]
    hospice_codes = value_sets.codes("hospice_snomed")
    refs += [
        enc_ref(e, "exclusion")
        for e in encounters_in(record, window)
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
    # E4: advanced-illness hint (dementia + acute care in the MY, age >= 66).
    if ctx.age_at_my_end is not None and ctx.age_at_my_end >= 66:
        my = measurement_year(ctx.as_of)
        dementia = [
            cond_ref(c, "escalation")
            for c in conditions_in(record, value_sets, "dementia_snomed")
            if condition_active_in(c, my)
        ]
        dementia += [
            EvidenceRef(
                section="medications",
                event_id=m.medication_request_id,
                code=m.code,
                code_system=m.code_system,
                display=m.code_display,
                event_date=m.authored_date,
                role="escalation",
            )
            for m in medications_in(record, value_sets, "dementia_meds_rxnorm")
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
