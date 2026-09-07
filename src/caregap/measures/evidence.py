"""Shared evidence helpers: typed lookups over a masked record + EvidenceRef builders.

Every rule reads the record ONLY through these helpers so that value-set membership, date
windows, and evidence construction are uniform (and testable once) across measures.
"""

from collections.abc import Iterable
from datetime import date
from typing import Literal

from caregap.measures.models import EvidenceRef, EvidenceRole, ExclusionHit
from caregap.measures.value_sets import ValueSets
from caregap.measures.windows import Window
from caregap.p6.models import (
    ConditionEvent,
    EncounterEvent,
    MedicationEvent,
    ObservationEvent,
    PatientRecord,
    ProcedureEvent,
)


def cond_ref(c: ConditionEvent, role: EvidenceRole) -> EvidenceRef:
    return EvidenceRef(
        section="conditions",
        event_id=c.condition_id,
        code=c.code,
        code_system=c.code_system,
        display=c.code_display,
        event_date=c.onset_date,
        role=role,
    )


def obs_ref(o: ObservationEvent, role: EvidenceRole) -> EvidenceRef:
    return EvidenceRef(
        section="observations",
        event_id=o.observation_id,
        code=o.code,
        code_system=o.code_system,
        display=o.code_display,
        event_date=o.effective_date,
        role=role,
    )


def proc_ref(p: ProcedureEvent, role: EvidenceRole) -> EvidenceRef:
    return EvidenceRef(
        section="procedures",
        event_id=p.procedure_id,
        code=p.code,
        code_system=p.code_system,
        display=p.code_display,
        event_date=p.performed_date,
        role=role,
    )


def med_ref(m: MedicationEvent, role: EvidenceRole) -> EvidenceRef:
    return EvidenceRef(
        section="medications",
        event_id=m.medication_request_id,
        code=m.code,
        code_system=m.code_system,
        display=m.code_display,
        event_date=m.authored_date,
        role=role,
    )


def enc_ref(e: EncounterEvent, role: EvidenceRole) -> EvidenceRef:
    return EvidenceRef(
        section="encounters",
        event_id=e.encounter_id,
        code=e.type_code,
        code_system=None,
        display=e.type_display,
        event_date=e.start_date,
        role=role,
    )


def patient_ref(role: EvidenceRole, *, event_date: date | None) -> EvidenceRef:
    return EvidenceRef(section="patient", event_id="patient", event_date=event_date, role=role)


# --- membership + window filters (all deterministic, order-stable) ------------------------


def conditions_in(
    record: PatientRecord, value_sets: ValueSets, set_id: str
) -> list[ConditionEvent]:
    codes, system = value_sets.codes(set_id), value_sets.system(set_id)
    return sorted(
        (c for c in record.conditions if c.code_system == system and c.code in codes),
        key=lambda c: (c.onset_date, c.condition_id),
    )


def condition_active_in(c: ConditionEvent, window: Window) -> bool:
    """Onset on/before the window end and abatement null or strictly AFTER the window start.

    One semantics for every caller (SPEC section 2, CBP row: ``abatement null or >
    my_start``): a condition abated ON the window's first day is NOT active in the window.
    """
    if c.onset_date > window.end:
        return False
    return c.abatement_date is None or c.abatement_date > window.start


def procedures_in(
    record: PatientRecord, value_sets: ValueSets, set_id: str, window: Window
) -> list[ProcedureEvent]:
    codes, system = value_sets.codes(set_id), value_sets.system(set_id)
    return sorted(
        (
            p
            for p in record.procedures
            if p.code_system == system and p.code in codes and window.contains(p.performed_date)
        ),
        key=lambda p: (p.performed_date, p.procedure_id),
    )


def procedures_overlapping(
    record: PatientRecord, value_sets: ValueSets, set_id: str, window: Window
) -> list[ProcedureEvent]:
    """Procedures whose ``performed_date`` OR ``performed_end_date`` falls inside the window.

    An episode that starts before the window and ends inside it (hospice) counts; a future end
    date is nulled by ``mask_as_of`` so an open-ended prior episode never leaks in.
    """
    codes, system = value_sets.codes(set_id), value_sets.system(set_id)
    return sorted(
        (
            p
            for p in record.procedures
            if p.code_system == system
            and p.code in codes
            and (window.contains(p.performed_date) or window.contains(p.performed_end_date))
        ),
        key=lambda p: (p.performed_date, p.procedure_id),
    )


def observations_in(
    record: PatientRecord, value_sets: ValueSets, set_id: str, window: Window
) -> list[ObservationEvent]:
    codes, system = value_sets.codes(set_id), value_sets.system(set_id)
    return sorted(
        (
            o
            for o in record.observations
            if o.code_system == system and o.code in codes and window.contains(o.effective_date)
        ),
        key=lambda o: (o.effective_date, o.observation_id),
    )


def observations_with_code(
    record: PatientRecord, code: str, window: Window
) -> list[ObservationEvent]:
    return sorted(
        (o for o in record.observations if o.code == code and window.contains(o.effective_date)),
        key=lambda o: (o.effective_date, o.observation_id),
    )


def medications_in(
    record: PatientRecord, value_sets: ValueSets, set_id: str, window: Window | None = None
) -> list[MedicationEvent]:
    codes, system = value_sets.codes(set_id), value_sets.system(set_id)
    return sorted(
        (
            m
            for m in record.medications
            if m.code_system == system
            and m.code in codes
            and (window is None or window.contains(m.authored_date))
        ),
        key=lambda m: (m.authored_date, m.medication_request_id),
    )


def encounters_in(record: PatientRecord, window: Window) -> list[EncounterEvent]:
    return sorted(
        (e for e in record.encounters if window.contains(e.start_date)),
        key=lambda e: (e.start_date, e.encounter_id),
    )


def encounters_overlapping(record: PatientRecord, window: Window) -> list[EncounterEvent]:
    """Encounters whose ``start_date`` OR ``end_ts`` date falls inside the window."""
    return sorted(
        (
            e
            for e in record.encounters
            if window.contains(e.start_date)
            or (e.end_ts is not None and window.contains(e.end_ts.date()))
        ),
        key=lambda e: (e.start_date, e.encounter_id),
    )


def child_observations_of(
    record: PatientRecord, parent_ids: frozenset[str], window: Window | None = None
) -> list[ObservationEvent]:
    """Component observations whose ``parent_observation_id`` is one of ``parent_ids``.

    Exact id match; ``window`` (when given) filters on the child's own ``effective_date``;
    deterministic ``(date, id)`` order. Shared by the CBP panel grouping and the screening
    domain components.
    """
    return sorted(
        (
            o
            for o in record.observations
            if o.parent_observation_id is not None
            and o.parent_observation_id in parent_ids
            and (window is None or window.contains(o.effective_date))
        ),
        key=lambda o: (o.effective_date, o.observation_id),
    )


# --- pregnancy (shared by CBP / SPC / SPD) ---------------------------------------------------

PREGNANCY_SET = "pregnancy_snomed"
#: LOINC "Pregnancy status" and the SNOMED answer meaning "currently pregnant" (demo_choice):
#: Synthea records pregnancy this way beside (or instead of) a pregnancy condition.
PREGNANCY_STATUS_LOINC = "82810-3"
PREGNANT_VALUE_CODES: frozenset[str] = frozenset({"77386006"})
PREGNANCY_CATEGORY = "pregnancy"
PREGNANCY_STATUS_CATEGORY = "pregnancy_status_positive"


def pregnancy_evidence(
    record: PatientRecord, value_sets: ValueSets, window: Window
) -> tuple[list[EvidenceRef], list[EvidenceRef]]:
    """(pregnancy conditions active in the window, positive pregnancy-status observations
    dated in the window) as exclusion evidence."""
    conditions = [
        cond_ref(c, "exclusion")
        for c in conditions_in(record, value_sets, PREGNANCY_SET)
        if condition_active_in(c, window)
    ]
    status = [
        obs_ref(o, "exclusion")
        for o in observations_with_code(record, PREGNANCY_STATUS_LOINC, window)
        if o.value_code in PREGNANT_VALUE_CODES
    ]
    return conditions, status


def pregnancy_hits(
    record: PatientRecord,
    value_sets: ValueSets,
    window: Window,
    *,
    condition_source: Literal["quoted", "demo_choice"],
) -> list[ExclusionHit]:
    """The pregnancy exclusion as every measure applies it: ``pregnancy`` (condition; tagged
    per the measure's public text) then ``pregnancy_status_positive`` (observation;
    demo_choice)."""
    conditions, status = pregnancy_evidence(record, value_sets, window)
    hits: list[ExclusionHit] = []
    if conditions:
        hits.append(
            ExclusionHit(
                category=PREGNANCY_CATEGORY,
                source=condition_source,
                window_label=window.label,
                evidence=conditions,
            )
        )
    if status:
        hits.append(
            ExclusionHit(
                category=PREGNANCY_STATUS_CATEGORY,
                source="demo_choice",
                window_label=window.label,
                evidence=status,
            )
        )
    return hits


def encounter_class_of(record: PatientRecord, encounter_id: str | None) -> str | None:
    if encounter_id is None:
        return None
    for e in record.encounters:
        if e.encounter_id == encounter_id:
            return e.encounter_class
    return None


def latest(items: Iterable[date]) -> date | None:
    return max(items, default=None)
