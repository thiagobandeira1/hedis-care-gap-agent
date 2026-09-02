"""Typed projections of P6's canonical rows (``extra="ignore"`` — additive P6 columns are fine).

Minimum-necessary at the boundary: ``PatientHeader`` keeps birth/death/sex only; race,
ethnicity, and address fields never enter P1 (a packet-render test asserts their absence).
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class _Row(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class PatientHeader(_Row):
    patient_id: str
    birth_date: date | None = None
    death_date: date | None = None
    sex: str = "unknown"


class ConditionEvent(_Row):
    condition_id: str
    code: str
    code_system: str
    code_display: str | None = None
    onset_date: date
    abatement_date: date | None = None
    recorded_date: date | None = None
    encounter_id: str | None = None


class ObservationEvent(_Row):
    observation_id: str
    parent_observation_id: str | None = None
    code: str
    code_system: str
    code_display: str | None = None
    category: str | None = None
    effective_date: date
    value_num: float | None = None
    value_unit: str | None = None
    value_code: str | None = None
    value_code_system: str | None = None
    value_text: str | None = None
    encounter_id: str | None = None


class ProcedureEvent(_Row):
    procedure_id: str
    code: str
    code_system: str
    code_display: str | None = None
    performed_date: date
    performed_end_date: date | None = None
    encounter_id: str | None = None


class MedicationEvent(_Row):
    medication_request_id: str
    code: str
    code_system: str
    code_display: str | None = None
    authored_date: date
    status: str | None = None
    """Read ONLY by the statin on-therapy rule and the E3 conflict flag (ADR-0002)."""
    intent: str | None = None
    encounter_id: str | None = None


class EncounterEvent(_Row):
    encounter_id: str
    encounter_class: str
    type_code: str | None = None
    type_display: str | None = None
    start_date: date
    start_ts: datetime | None = None
    end_ts: datetime | None = None


class PatientRecord(_Row):
    """The as_of-masked, engine-facing projection of ``GET /v1/patients/{id}/record``."""

    patient: PatientHeader
    as_of: date
    conditions: list[ConditionEvent] = Field(default_factory=list)
    observations: list[ObservationEvent] = Field(default_factory=list)
    procedures: list[ProcedureEvent] = Field(default_factory=list)
    medications: list[MedicationEvent] = Field(default_factory=list)
    encounters: list[EncounterEvent] = Field(default_factory=list)


class FeatureRow(_Row):
    """P6 feature row: drafter context + display only. NEVER an engine input."""

    patient_id: str
    as_of: date
    age_years: int | None = None
    sex: str = "unknown"
    has_diabetes: bool = False
    has_hypertension: bool = False
    has_ascvd: bool = False
    has_ckd: bool = False
    has_chf: bool = False
    has_copd: bool = False
    chronic_condition_count: int = 0
    encounters_365d: int = 0
    ed_visits_365d: int = 0
    inpatient_admits_365d: int = 0
    days_since_last_encounter: int | None = None
    tobacco_status_code: str | None = None
    latest_sbp: float | None = None
    latest_dbp: float | None = None
    latest_hba1c: float | None = None


class ServiceInfo(_Row):
    service_version: str
    schema_version: int
    feature_version: str


class FeatureSchema(_Row):
    feature_version: str
    valuesets_version: str


class PatientSummary(_Row):
    patient_id: str
    source: str
    birth_date: date | None = None
    sex: str = "unknown"
    deceased: bool = False


class PatientPage(_Row):
    items: list[PatientSummary]
    total: int
