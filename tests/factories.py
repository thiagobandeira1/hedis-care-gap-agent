"""Record factories shared by every test module (rule tests included).

Plain functions with sensible defaults and deterministic ids (``c1``, ``o1``, ``pr1``, ``m1``,
``e1``) drawn from per-section counters; ``conftest`` resets the counters before every test so
ids are stable within a test regardless of ordering. ``build_record`` returns a raw
:class:`~caregap.p6.models.PatientRecord` (NOT masked) so tests can exercise ``mask_as_of``
deliberately; ``p6_payload`` / ``write_snapshot`` turn records into P6-shaped JSON for the
client tests.
"""

import gzip
import itertools
import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from caregap.p6.models import (
    ConditionEvent,
    EncounterEvent,
    MedicationEvent,
    ObservationEvent,
    PatientHeader,
    PatientRecord,
    ProcedureEvent,
)

EVAL_AS_OF = date(2025, 12, 31)
"""SPEC section 2: eval anchor (complete MY2025)."""
DEMO_AS_OF = date(2026, 6, 30)
"""SPEC section 2: demo anchor (mid-MY2026)."""
BIRTH_1960 = date(1960, 6, 15)
"""Age 65 at Dec 31 2025, 66 at Dec 31 2026."""
DEFAULT_ONSET = date(2020, 1, 1)
DEFAULT_DAY = date(2025, 3, 15)

SNOMED = "SNOMED"
LOINC = "LOINC"
RXNORM = "RXNORM"

BP_PANEL = "85354-9"
BP_SYSTOLIC = "8480-6"
BP_DIASTOLIC = "8462-4"
MMHG = "mm[Hg]"

_PREFIXES = ("c", "o", "pr", "m", "e")
_counters: dict[str, Iterator[int]] = {}


def reset_ids() -> None:
    """Restart every id counter at 1 (called by the autouse conftest fixture)."""
    for prefix in _PREFIXES:
        _counters[prefix] = itertools.count(1)


def next_id(prefix: str) -> str:
    if prefix not in _counters:
        _counters[prefix] = itertools.count(1)
    return f"{prefix}{next(_counters[prefix])}"


reset_ids()


# --- row factories -----------------------------------------------------------------------


def patient(
    *,
    patient_id: str = "p1",
    birth_date: date | None = BIRTH_1960,
    death_date: date | None = None,
    sex: str = "female",
) -> PatientHeader:
    return PatientHeader(
        patient_id=patient_id, birth_date=birth_date, death_date=death_date, sex=sex
    )


def condition(
    code: str,
    *,
    onset: date = DEFAULT_ONSET,
    abatement: date | None = None,
    condition_id: str | None = None,
    code_system: str = SNOMED,
    display: str | None = None,
    recorded_date: date | None = None,
    encounter_id: str | None = None,
) -> ConditionEvent:
    return ConditionEvent(
        condition_id=condition_id or next_id("c"),
        code=code,
        code_system=code_system,
        code_display=display,
        onset_date=onset,
        abatement_date=abatement,
        recorded_date=recorded_date,
        encounter_id=encounter_id,
    )


def observation(
    code: str,
    *,
    effective: date = DEFAULT_DAY,
    observation_id: str | None = None,
    code_system: str = LOINC,
    display: str | None = None,
    category: str | None = None,
    value_num: float | None = None,
    unit: str | None = None,
    value_code: str | None = None,
    value_code_system: str | None = None,
    value_text: str | None = None,
    parent_observation_id: str | None = None,
    encounter_id: str | None = None,
) -> ObservationEvent:
    return ObservationEvent(
        observation_id=observation_id or next_id("o"),
        parent_observation_id=parent_observation_id,
        code=code,
        code_system=code_system,
        code_display=display,
        category=category,
        effective_date=effective,
        value_num=value_num,
        value_unit=unit,
        value_code=value_code,
        value_code_system=value_code_system,
        value_text=value_text,
        encounter_id=encounter_id,
    )


def bp_panel(
    *,
    effective: date = DEFAULT_DAY,
    sbp: float | None = 128.0,
    dbp: float | None = 82.0,
    unit: str = MMHG,
    encounter_id: str | None = None,
    panel_id: str | None = None,
) -> list[ObservationEvent]:
    """A P6-shaped BP panel: parent 85354-9 plus the SBP/DBP components that share
    ``parent_observation_id``. Pass ``sbp=None`` / ``dbp=None`` to omit a component."""
    parent = observation(
        BP_PANEL,
        effective=effective,
        observation_id=panel_id,
        category="vital-signs",
        encounter_id=encounter_id,
    )
    rows = [parent]
    for code, value in ((BP_SYSTOLIC, sbp), (BP_DIASTOLIC, dbp)):
        if value is None:
            continue
        rows.append(
            observation(
                code,
                effective=effective,
                category="vital-signs",
                value_num=value,
                unit=unit,
                parent_observation_id=parent.observation_id,
                encounter_id=encounter_id,
            )
        )
    return rows


def procedure(
    code: str,
    *,
    performed: date = DEFAULT_DAY,
    procedure_id: str | None = None,
    code_system: str = SNOMED,
    display: str | None = None,
    performed_end: date | None = None,
    encounter_id: str | None = None,
) -> ProcedureEvent:
    return ProcedureEvent(
        procedure_id=procedure_id or next_id("pr"),
        code=code,
        code_system=code_system,
        code_display=display,
        performed_date=performed,
        performed_end_date=performed_end,
        encounter_id=encounter_id,
    )


def medication(
    code: str,
    *,
    authored: date = DEFAULT_DAY,
    medication_request_id: str | None = None,
    code_system: str = RXNORM,
    display: str | None = None,
    status: str | None = "active",
    intent: str | None = "order",
    encounter_id: str | None = None,
) -> MedicationEvent:
    return MedicationEvent(
        medication_request_id=medication_request_id or next_id("m"),
        code=code,
        code_system=code_system,
        code_display=display,
        authored_date=authored,
        status=status,
        intent=intent,
        encounter_id=encounter_id,
    )


def encounter(
    *,
    start: date = DEFAULT_DAY,
    encounter_id: str | None = None,
    encounter_class: str = "AMB",
    type_code: str | None = None,
    type_display: str | None = None,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
) -> EncounterEvent:
    return EncounterEvent(
        encounter_id=encounter_id or next_id("e"),
        encounter_class=encounter_class,
        type_code=type_code,
        type_display=type_display,
        start_date=start,
        start_ts=start_ts,
        end_ts=end_ts,
    )


def build_record(
    *,
    patient_header: PatientHeader | None = None,
    as_of: date = EVAL_AS_OF,
    conditions: Sequence[ConditionEvent] = (),
    observations: Sequence[ObservationEvent] = (),
    procedures: Sequence[ProcedureEvent] = (),
    medications: Sequence[MedicationEvent] = (),
    encounters: Sequence[EncounterEvent] = (),
) -> PatientRecord:
    """A raw (unmasked) record; apply ``caregap.p6.client.mask_as_of`` when a test needs the
    boundary behaviour."""
    return PatientRecord(
        patient=patient_header or patient(),
        as_of=as_of,
        conditions=list(conditions),
        observations=list(observations),
        procedures=list(procedures),
        medications=list(medications),
        encounters=list(encounters),
    )


# --- P6-shaped JSON --------------------------------------------------------------------

#: Demographic columns P6 returns and P1 must drop at the boundary (synthetic values).
P6_PATIENT_EXTRAS: dict[str, Any] = {
    "race": "2106-3",
    "ethnicity": "2186-5",
    "city": "Springfield",
    "state": "MA",
    "postal_code": "01101",
}


def _rows(items: Sequence[Any], patient_id: str, **extra: Any) -> list[dict[str, Any]]:
    return [
        {**item.model_dump(mode="json"), "patient_id": patient_id, "date_precision": "day", **extra}
        for item in items
    ]


def p6_payload(record: PatientRecord) -> dict[str, Any]:
    """The ``GET /v1/patients/{id}/record`` body P6 would return for ``record``: every row
    carries P6's extra columns (patient_id, date_precision, statuses, demographics)."""
    pid = record.patient.patient_id
    conditions = _rows(
        record.conditions, pid, clinical_status="active", verification_status="confirmed"
    )
    observations = _rows(record.observations, pid, effective_ts=None, status="final")
    procedures = _rows(record.procedures, pid, status="completed")
    medications = _rows(record.medications, pid)
    encounters = _rows(record.encounters, pid, type_system=None)
    return {
        "patient": {**record.patient.model_dump(mode="json"), **P6_PATIENT_EXTRAS},
        "conditions": conditions,
        "observations": observations,
        "procedures": procedures,
        "medications": medications,
        "encounters": encounters,
        "immunizations": [],
        "claim_diagnoses": [],
        "counts": {
            "conditions": len(conditions),
            "observations": len(observations),
            "procedures": len(procedures),
            "medications": len(medications),
            "encounters": len(encounters),
            "immunizations": 0,
            "claim_diagnoses": 0,
        },
    }


def feature_row(patient_id: str, as_of: date, **overrides: Any) -> dict[str, Any]:
    """A P6 feature row (the ``features`` object of ``/features``); never an engine input."""
    row: dict[str, Any] = {
        "patient_id": patient_id,
        "as_of": as_of.isoformat(),
        "age_years": 65,
        "sex": "female",
        "has_diabetes": False,
        "has_hypertension": True,
        "has_ascvd": False,
        "has_ckd": False,
        "has_chf": False,
        "has_copd": False,
        "chronic_condition_count": 1,
        "encounters_365d": 2,
        "ed_visits_365d": 0,
        "inpatient_admits_365d": 0,
        "days_since_last_encounter": 40,
        "tobacco_status_code": None,
        "latest_sbp": 128.0,
        "latest_dbp": 82.0,
        "latest_hba1c": None,
    }
    row.update(overrides)
    return row


def write_snapshot(
    root: Path,
    *,
    as_of: date,
    records: Mapping[str, PatientRecord],
    features: Mapping[str, Mapping[str, Any]] | None = None,
    patient_ids: Sequence[str] | None = None,
    service_version: str = "0.1.0",
    feature_version: str = "2026.08",
    valuesets_version: str = "2026.08",
) -> Path:
    """Materialise the ``SnapshotP6Client`` layout: ``MANIFEST.json`` +
    ``<pid>/record_<as_of>.json.gz`` + ``<pid>/features_<as_of>.json``."""
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "service_version": service_version,
        "schema_version": 3,
        "feature_version": feature_version,
        "valuesets_version": valuesets_version,
        "generated_from_sha": "test",
        "command": "tests.factories.write_snapshot",
        "patient_ids": list(patient_ids if patient_ids is not None else records),
    }
    (root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for pid, record in records.items():
        folder = root / pid
        folder.mkdir(exist_ok=True)
        record_path = folder / f"record_{as_of.isoformat()}.json.gz"
        with gzip.open(record_path, "wt", encoding="utf-8") as fh:
            json.dump(p6_payload(record), fh)
        if features is not None and pid in features:
            (folder / f"features_{as_of.isoformat()}.json").write_text(
                json.dumps(
                    {
                        "feature_version": feature_version,
                        "valuesets_version": valuesets_version,
                        "features": dict(features[pid]),
                    }
                ),
                encoding="utf-8",
            )
    return root
