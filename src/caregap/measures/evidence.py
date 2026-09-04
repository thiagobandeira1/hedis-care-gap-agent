"""Shared evidence helpers: typed lookups over a masked record + EvidenceRef builders.

Every rule reads the record ONLY through these helpers so that value-set membership, date
windows, and evidence construction are uniform (and testable once) across measures.
"""

from collections.abc import Iterable
from datetime import date

from caregap.measures.models import EvidenceRef, EvidenceRole
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
    """Onset on/before the window end and not abated before the window start."""
    if c.onset_date > window.end:
        return False
    return c.abatement_date is None or c.abatement_date >= window.start


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


def encounter_class_of(record: PatientRecord, encounter_id: str | None) -> str | None:
    if encounter_id is None:
        return None
    for e in record.encounters:
        if e.encounter_id == encounter_id:
            return e.encounter_class
    return None


def latest(items: Iterable[date]) -> date | None:
    return max(items, default=None)
