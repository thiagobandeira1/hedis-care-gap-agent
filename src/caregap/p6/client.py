"""P6 client contract, error taxonomy, and the as_of mask applied at the boundary."""

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Protocol

from caregap.p6.models import (
    ConditionEvent,
    EncounterEvent,
    FeatureRow,
    FeatureSchema,
    MedicationEvent,
    ObservationEvent,
    PatientHeader,
    PatientPage,
    PatientRecord,
    ProcedureEvent,
    ServiceInfo,
)

#: Record sections the engine consumes (no observation_codes filter: labelers see everything).
ENGINE_SECTIONS: tuple[str, ...] = (
    "conditions",
    "observations",
    "procedures",
    "medications",
    "encounters",
)


class P6Error(RuntimeError):
    """Base class; messages carry ids and codes only, never record content."""


class PatientNotFound(P6Error):
    """404 from P6."""


class P6ContractError(P6Error):
    """422 or a response that does not fit the typed models (problem+json code only)."""


class P6Unavailable(P6Error):
    """Connection errors / 5xx after retries."""


class P6Client(Protocol):
    def healthz(self) -> ServiceInfo: ...

    def features_schema(self) -> FeatureSchema: ...

    def list_patients(self, *, limit: int, offset: int) -> PatientPage: ...

    def get_record(
        self, patient_id: str, *, to: date, sections: Sequence[str] = ENGINE_SECTIONS
    ) -> PatientRecord: ...

    def get_features(self, patient_id: str, *, as_of: date) -> FeatureRow: ...


def mask_as_of(record: PatientRecord, as_of: date) -> PatientRecord:
    """Hide everything P6's ``?to=`` filter cannot: future death, abatement, and end dates.

    P6 filters each section on its primary event date only; a condition that abated after
    ``as_of``, or a patient who died after ``as_of``, must look alive/ongoing for this run.
    Events dated after ``as_of`` are dropped outright as a belt-and-braces guarantee.
    """
    patient = record.patient
    if patient.death_date is not None and patient.death_date > as_of:
        patient = patient.model_copy(update={"death_date": None})
    conditions = [
        c.model_copy(update={"abatement_date": None})
        if c.abatement_date is not None and c.abatement_date > as_of
        else c
        for c in record.conditions
        if c.onset_date <= as_of
    ]
    procedures = [
        p.model_copy(update={"performed_end_date": None})
        if p.performed_end_date is not None and p.performed_end_date > as_of
        else p
        for p in record.procedures
        if p.performed_date <= as_of
    ]
    encounters = [
        e.model_copy(update={"end_ts": None})
        if e.end_ts is not None and e.end_ts.date() > as_of
        else e
        for e in record.encounters
        if e.start_date <= as_of
    ]
    return record.model_copy(
        update={
            "patient": patient,
            "as_of": as_of,
            "conditions": conditions,
            "observations": [o for o in record.observations if o.effective_date <= as_of],
            "procedures": procedures,
            "medications": [m for m in record.medications if m.authored_date <= as_of],
            "encounters": encounters,
        }
    )


def record_from_p6_payload(payload: Mapping[str, Any], as_of: date) -> PatientRecord:
    """Build a masked :class:`PatientRecord` from P6's ``/record`` JSON body."""
    patient_raw = payload.get("patient")
    if not isinstance(patient_raw, Mapping) or "patient_id" not in patient_raw:
        raise P6ContractError("record payload lacks a patient object")
    record = PatientRecord(
        patient=PatientHeader.model_validate(patient_raw),
        as_of=as_of,
        conditions=[ConditionEvent.model_validate(x) for x in payload.get("conditions", [])],
        observations=[ObservationEvent.model_validate(x) for x in payload.get("observations", [])],
        procedures=[ProcedureEvent.model_validate(x) for x in payload.get("procedures", [])],
        medications=[MedicationEvent.model_validate(x) for x in payload.get("medications", [])],
        encounters=[EncounterEvent.model_validate(x) for x in payload.get("encounters", [])],
    )
    return mask_as_of(record, as_of)
