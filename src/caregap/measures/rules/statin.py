"""Statin machinery shared by SPC (C19) and SPD (D12): the sex-specific SPC age band, the
on-therapy rule, the MY-or-prior-year exclusion window, and the E3 status-conflict flag.

ADR-0002: :func:`on_therapy` and :func:`e3_escalations` are the ONLY readers of
``MedicationEvent.status`` in the engine.
"""

from collections.abc import Sequence

from caregap.measures.context import MeasurementContext
from caregap.measures.evidence import med_ref, medications_in
from caregap.measures.models import EscalationFlag
from caregap.measures.tri import Tri
from caregap.measures.value_sets import ValueSets
from caregap.measures.windows import Window, measurement_year
from caregap.p6.models import MedicationEvent, PatientRecord

STATIN_SET = "statin_rxnorm"
MALE_AGE_BAND: tuple[int, int] = (21, 75)
FEMALE_AGE_BAND: tuple[int, int] = (40, 75)

#: ``MedicationRequest.status`` values that never count as therapy (ADR-0002).
NEVER_COUNT_STATUSES: frozenset[str] = frozenset({"stopped", "cancelled", "entered-in-error"})
#: A statin authored in the MY carrying one of these raises E3.
CONFLICT_STATUSES: frozenset[str] = frozenset({"stopped", "cancelled"})
ACTIVE_STATUS = "active"
EXCLUSION_WINDOW_LABEL = "MY or prior year"


def tri_all(values: Sequence[Tri]) -> Tri:
    """Kleene AND: any ``no`` wins, then any ``unknown``, else ``yes``."""
    if "no" in values:
        return "no"
    if "unknown" in values:
        return "unknown"
    return "yes"


def status_of(m: MedicationEvent) -> str:
    return (m.status or "").strip().lower()


def _in_band(age: int, band: tuple[int, int], label: str) -> tuple[Tri, str]:
    low, high = band
    if low <= age <= high:
        return "yes", f"{label} aged {age} at MY end: within {low}-{high}"
    return "no", f"{label} aged {age} at MY end: outside {low}-{high}"


def spc_age_sex(record: PatientRecord, ctx: MeasurementContext) -> tuple[Tri, str]:
    """SPC's sex-specific age band; ``unknown`` only when the missing datum would decide it."""
    age = ctx.age_at_my_end
    if age is None:
        return "unknown", "birth_date unknown"
    sex = record.patient.sex.strip().lower()
    if sex == "male":
        return _in_band(age, MALE_AGE_BAND, "male")
    if sex == "female":
        return _in_band(age, FEMALE_AGE_BAND, "female")
    male_low, high = MALE_AGE_BAND
    female_low, _ = FEMALE_AGE_BAND
    if female_low <= age <= high:
        return "yes", f"sex unknown, aged {age} at MY end: within both bands"
    if age < male_low or age > high:
        return "no", f"sex unknown, aged {age} at MY end: outside both bands"
    return "unknown", f"sex unknown, aged {age} at MY end: eligible only if male"


def on_therapy(m: MedicationEvent, ctx: MeasurementContext) -> bool:
    """Authored in [my_start, as_of], or authored before the MY with status active."""
    status = status_of(m)
    if status in NEVER_COUNT_STATUSES:
        return False
    if ctx.my_start <= m.authored_date <= ctx.as_of:
        return True
    return m.authored_date < ctx.my_start and status == ACTIVE_STATUS


def exclusion_window(ctx: MeasurementContext) -> Window:
    """[Jan 1 of MY-1, Dec 31 of the MY], built from MY bounds."""
    return Window(start=ctx.prior_my_start, end=ctx.my_end, label=EXCLUSION_WINDOW_LABEL)


def e3_escalations(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets
) -> list[EscalationFlag]:
    """E3 ``medication_status_conflict``: a statin authored in the MY with status stopped or
    cancelled, raised even when another statin closes the numerator."""
    my = measurement_year(ctx.as_of)
    conflicts = [
        m for m in medications_in(record, vs, STATIN_SET, my) if status_of(m) in CONFLICT_STATUSES
    ]
    if not conflicts:
        return []
    return [
        EscalationFlag(
            kind="E3",
            scope="measure",
            reason=(
                "medication_status_conflict: statin authored in the measurement year with "
                "status stopped/cancelled"
            ),
            evidence=[med_ref(m, "escalation") for m in conflicts],
        )
    ]
