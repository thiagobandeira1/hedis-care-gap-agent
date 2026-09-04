"""CBP - Controlling High Blood Pressure (CMS Star Ratings id C14).

Public-source basis
-------------------
CMS 2026 Star Ratings Technical Notes, Part C measure **C14: Controlling Blood Pressure**
(HEDIS CBP lineage; text via P2's committed corpus, tagged citations in ``rules/json/cbp.json``)
plus the public NCQA HEDIS CBP measure summary. Demo-grade, HEDIS-aligned, NOT NCQA-certified
(SPEC section 2, ADR-0005).

Element tags
------------
quoted
  * denominator age band 18-85, age at Dec 31 of the measurement year (MY);
  * denominator concept: members "who had a diagnosis of hypertension";
  * numerator concept: BP "adequately controlled (<140/90 mmHg)" during the MY, judged on
    the most recent reading; the representative BP is the lowest systolic and the lowest
    diastolic among readings on that most recent date (NCQA public CBP summary);
  * readings taken in an acute inpatient setting or an ED visit do not count (NCQA);
  * exclusions: ESRD (diagnosis, any time); pregnancy during the MY; died / hospice during
    the MY (applied by the GLOBAL rules in ``rules/global_rules.py``, never here).
demo_choice
  * hypertension = ``hypertension_snomed`` condition with ``onset_date <= my_end`` and
    ``abatement_date`` null or ``> my_start``. The public "diagnosis on or before June 30 of
    the MY" nuance is deliberately NOT implemented (onset through Dec 31 counts).
  * a "reading" is a P6 BP panel: a LOINC 85354-9 parent observation whose children (same
    ``parent_observation_id``) include BOTH 8480-6 (SBP) and 8462-4 (DBP), each with a
    numeric value and ``value_unit == "mm[Hg]"`` (UCUM, P6 canonical);
  * the panel's setting is the ``encounter_class`` of the parent's encounter: IMP / EMER
    panels are ignored; a NULL or unresolvable encounter counts as non-acute;
  * numerator window [my_start, as_of]; the most recent DATE among non-acute panels decides.
    Any panel on that date that is incomplete, non-numeric, or not in mm[Hg] makes the
    numerator ``unknown`` and raises E5 (the representative BP cannot be computed);
  * no non-acute panel in the window -> numerator ``no`` with subtype ``no_bp_in_my``;
  * dialysis = ``dialysis_snomed`` procedure any time through as_of;
  * kidney transplant = ``kidney_transplant_snomed`` condition or procedure any time;
  * pregnancy is also detected from LOINC 82810-3 "Pregnancy status" with value SNOMED
    77386006 ("Patient currently pregnant") dated in the MY - Synthea records it this way;
  * "any time" exclusion windows end at ``as_of`` (the record is as_of-masked);
  * E6 (measure scope): a hypertension condition whose ``abatement_date`` falls in
    [my_start, as_of] - the diagnosis may have resolved inside the MY.
not_representable / partial (coverage table only, never computed)
  * palliative care during the MY; I-SNP / long-term institutional residence (66+); frailty
    alone at 81+; frailty AND advanced illness (66-80) is ``partial`` via the global E4 hint.

Doctrine: deterministic, pure, order-independent over shuffled events; every date is compared
against ``ctx`` / ``windows``; ``clinical_status`` / ``verification_status`` /
``MedicationEvent.status`` are never read; features never enter; the record is read only
through :mod:`caregap.measures.evidence`. E1 / E4 are global and not re-implemented here.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from caregap.measures.context import MeasurementContext
from caregap.measures.engine import RuleOutput
from caregap.measures.evidence import (
    cond_ref,
    condition_active_in,
    conditions_in,
    encounter_class_of,
    obs_ref,
    observations_in,
    observations_with_code,
    patient_ref,
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
from caregap.measures.tri import Tri
from caregap.measures.value_sets import ValueSets
from caregap.measures.windows import Window, any_time, measurement_year, measurement_year_full
from caregap.p6.models import ObservationEvent, PatientRecord

AGE_MIN = 18
AGE_MAX = 85

#: Controlled iff SBP < SBP_LIMIT and DBP < DBP_LIMIT (quoted "<140/90 mmHg").
SBP_LIMIT = 140.0
DBP_LIMIT = 90.0

BP_SET = "bp_loinc"
PANEL_CODE = "85354-9"
SBP_CODE = "8480-6"
DBP_CODE = "8462-4"
#: UCUM unit P6 canonicalises BP components to; anything else -> E5 (demo_choice).
BP_UNIT = "mm[Hg]"
#: Panels whose parent's encounter carries one of these classes never count (quoted).
ACUTE_ENCOUNTER_CLASSES: frozenset[str] = frozenset({"IMP", "EMER"})

HYPERTENSION_SET = "hypertension_snomed"
ESRD_SET = "esrd_snomed"
DIALYSIS_SET = "dialysis_snomed"
KIDNEY_TRANSPLANT_SET = "kidney_transplant_snomed"
PREGNANCY_SET = "pregnancy_snomed"
#: LOINC "Pregnancy status" and the SNOMED answer meaning "currently pregnant" (demo_choice).
PREGNANCY_STATUS_LOINC = "82810-3"
PREGNANT_VALUE_CODES: frozenset[str] = frozenset({"77386006"})

NO_BP_IN_MY = "no_bp_in_my"

#: Every public CBP exclusion criterion and how far this demo rule can observe it.
COVERAGE: dict[str, Coverage] = {
    "died_during_measurement_period": "observable",  # global rule (quoted)
    "hospice_during_measurement_period": "observable",  # global rule (quoted)
    "esrd": "observable",  # esrd_snomed condition any time (quoted)
    "dialysis": "observable",  # dialysis_snomed procedure any time (demo_choice)
    "kidney_transplant": "observable",  # kidney_transplant_snomed any time (demo_choice)
    "pregnancy_during_measurement_period": "observable",  # condition active in MY / 82810-3
    "palliative_care": "not_representable",
    "frailty_and_advanced_illness_66_to_80": "partial",  # global E4 hint only
    "frailty_81_plus": "not_representable",
    "institutional_snp_or_long_term_institution_66_plus": "not_representable",
}


@dataclass(frozen=True)
class _Panel:
    """One BP panel: the 85354-9 parent plus its SBP / DBP children."""

    parent: ObservationEvent
    sbp: list[ObservationEvent] = field(default_factory=list)
    dbp: list[ObservationEvent] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.defects


@dataclass(frozen=True)
class _BpAssessment:
    numerator: TriResult
    escalations: list[EscalationFlag]


def _tri_all(values: Sequence[Tri]) -> Tri:
    """Kleene AND: any ``no`` wins, then any ``unknown``, else ``yes``."""
    if "no" in values:
        return "no"
    if "unknown" in values:
        return "unknown"
    return "yes"


def _num(value: float) -> str:
    return f"{value:g}"


def _fmt(window: Window) -> str:
    return f"{window.label} [{window.start:%Y-%m-%d} .. {window.end:%Y-%m-%d}]"


def _age_tri(age: int | None) -> tuple[Tri, str]:
    if age is None:
        return "unknown", "birth_date unknown"
    if AGE_MIN <= age <= AGE_MAX:
        return "yes", f"age {age} at Dec 31 of the MY within {AGE_MIN}-{AGE_MAX}"
    return "no", f"age {age} at Dec 31 of the MY outside {AGE_MIN}-{AGE_MAX}"


def _hypertension_in_denominator(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets
) -> list[EvidenceRef]:
    """Onset on/before MY end and abatement null or strictly after MY start (SPEC table).

    Deliberately not :func:`evidence.condition_active_in` (which admits an abatement ON the
    window start) - the SPEC wording for CBP is ``abatement > my_start``.
    """
    return [
        cond_ref(c, "eligibility")
        for c in conditions_in(record, vs, HYPERTENSION_SET)
        if c.onset_date <= ctx.my_end
        and (c.abatement_date is None or c.abatement_date > ctx.my_start)
    ]


def _denominator(record: PatientRecord, ctx: MeasurementContext, vs: ValueSets) -> TriResult:
    values: list[Tri] = []
    reasons: list[str] = []
    evidence: list[EvidenceRef] = []

    if died_before_my(record, ctx):
        values.append("no")
        reasons.append("died before the measurement year")
        evidence.append(patient_ref("eligibility", event_date=record.patient.death_date))

    age_value, age_reason = _age_tri(ctx.age_at_my_end)
    values.append(age_value)
    reasons.append(age_reason)
    evidence.append(patient_ref("eligibility", event_date=record.patient.birth_date))

    hypertension = _hypertension_in_denominator(record, ctx, vs)
    if hypertension:
        values.append("yes")
        reasons.append("hypertension condition with onset on/before MY end, not abated by MY start")
        evidence.extend(hypertension)
    else:
        values.append("no")
        reasons.append("no hypertension condition with onset on/before MY end active at MY start")

    return TriResult(
        value=_tri_all(values),
        reasons=reasons,
        evidence=evidence,
        window_start=ctx.my_start,
        window_end=ctx.my_end,
    )


def _component_defects(label: str, children: list[ObservationEvent]) -> list[str]:
    if not children:
        return [f"missing {label} component"]
    defects: list[str] = []
    for child in children:
        if child.value_num is None:
            defects.append(f"{label} value missing")
        if child.value_unit != BP_UNIT:
            defects.append(f"{label} unit {child.value_unit!r} is not {BP_UNIT!r}")
    return defects


def _panels(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets
) -> tuple[list[_Panel], list[ObservationEvent]]:
    """Non-acute BP panels dated in [my_start, as_of] and the acute (IMP/EMER) ones ignored.

    Children are matched by ``parent_observation_id`` regardless of their own date (P6 gives
    them the parent's date); the panel's date is the parent's ``effective_date``.
    """
    window = measurement_year(ctx.as_of)
    bp_observations = observations_in(record, vs, BP_SET, any_time(ctx.as_of))
    children: dict[str, list[ObservationEvent]] = {}
    for o in bp_observations:
        if o.parent_observation_id is not None and o.code in {SBP_CODE, DBP_CODE}:
            children.setdefault(o.parent_observation_id, []).append(o)

    panels: list[_Panel] = []
    ignored: list[ObservationEvent] = []
    for parent in bp_observations:
        if parent.code != PANEL_CODE or not window.contains(parent.effective_date):
            continue
        if encounter_class_of(record, parent.encounter_id) in ACUTE_ENCOUNTER_CLASSES:
            ignored.append(parent)
            continue
        mine = children.get(parent.observation_id, [])
        sbp = [c for c in mine if c.code == SBP_CODE]
        dbp = [c for c in mine if c.code == DBP_CODE]
        defects = _component_defects("SBP", sbp) + _component_defects("DBP", dbp)
        panels.append(_Panel(parent=parent, sbp=sbp, dbp=dbp, defects=defects))
    return panels, ignored


def _lowest(children: list[ObservationEvent]) -> tuple[float, ObservationEvent]:
    """Lowest numeric value with its observation; ties broken by id (order independence)."""
    ranked = [(c.value_num, c.observation_id, c) for c in children if c.value_num is not None]
    value, _, observation = min(ranked, key=lambda t: (t[0], t[1]))
    return value, observation


def _assess_bp(record: PatientRecord, ctx: MeasurementContext, vs: ValueSets) -> _BpAssessment:
    window = measurement_year(ctx.as_of)
    panels, ignored = _panels(record, ctx, vs)
    if not panels:
        reasons = [f"no BP panel ({PANEL_CODE} with SBP/DBP children) in {_fmt(window)}"]
        if ignored:
            reasons.append(f"{len(ignored)} panel(s) at IMP/EMER encounters ignored")
        return _BpAssessment(
            numerator=TriResult(
                value="no",
                reasons=reasons,
                subtype=NO_BP_IN_MY,
                window_start=window.start,
                window_end=window.end,
            ),
            escalations=[],
        )

    latest_date = max(p.parent.effective_date for p in panels)
    on_date = [p for p in panels if p.parent.effective_date == latest_date]
    defective = [p for p in on_date if not p.complete]
    if defective:
        detail = "; ".join(
            f"{p.parent.observation_id}: {', '.join(p.defects)}"
            for p in sorted(defective, key=lambda p: p.parent.observation_id)
        )
        evidence = [
            obs_ref(o, "escalation")
            for p in sorted(defective, key=lambda p: p.parent.observation_id)
            for o in (p.parent, *p.sbp, *p.dbp)
        ]
        reason = (
            f"most recent BP panel date {latest_date:%Y-%m-%d} has an incomplete panel or a "
            f"unit other than {BP_UNIT}: {detail}"
        )
        return _BpAssessment(
            numerator=TriResult(
                value="unknown",
                reasons=[reason],
                evidence=[e.model_copy(update={"role": "numerator"}) for e in evidence],
                window_start=window.start,
                window_end=window.end,
            ),
            escalations=[
                EscalationFlag(kind="E5", scope="measure", reason=reason, evidence=evidence)
            ],
        )

    sbp_value, sbp = _lowest([c for p in on_date for c in p.sbp])
    dbp_value, dbp = _lowest([c for p in on_date for c in p.dbp])
    controlled = sbp_value < SBP_LIMIT and dbp_value < DBP_LIMIT
    reasons = [
        f"representative BP {_num(sbp_value)}/{_num(dbp_value)} {BP_UNIT} on "
        f"{latest_date:%Y-%m-%d} (lowest SBP and lowest DBP among {len(on_date)} same-date "
        f"panel(s)) is {'below' if controlled else 'not below'} "
        f"{_num(SBP_LIMIT)}/{_num(DBP_LIMIT)}"
    ]
    if ignored:
        reasons.append(f"{len(ignored)} panel(s) at IMP/EMER encounters ignored")
    evidence = [obs_ref(p.parent, "numerator") for p in on_date]
    evidence += [obs_ref(sbp, "numerator"), obs_ref(dbp, "numerator")]
    return _BpAssessment(
        numerator=TriResult(
            value="yes" if controlled else "no",
            reasons=reasons,
            evidence=evidence,
            window_start=window.start,
            window_end=window.end,
        ),
        escalations=[],
    )


def _exclusions(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets
) -> list[ExclusionHit]:
    ever = any_time(ctx.as_of)
    my = measurement_year_full(ctx.as_of)
    hits: list[ExclusionHit] = []

    esrd = [
        cond_ref(c, "exclusion")
        for c in conditions_in(record, vs, ESRD_SET)
        if ever.contains(c.onset_date)
    ]
    if esrd:
        hits.append(
            ExclusionHit(category="esrd", source="quoted", window_label=ever.label, evidence=esrd)
        )

    dialysis = [proc_ref(p, "exclusion") for p in procedures_in(record, vs, DIALYSIS_SET, ever)]
    if dialysis:
        hits.append(
            ExclusionHit(
                category="dialysis",
                source="demo_choice",
                window_label=ever.label,
                evidence=dialysis,
            )
        )

    transplant: list[EvidenceRef] = [
        cond_ref(c, "exclusion")
        for c in conditions_in(record, vs, KIDNEY_TRANSPLANT_SET)
        if ever.contains(c.onset_date)
    ]
    transplant += [
        proc_ref(p, "exclusion") for p in procedures_in(record, vs, KIDNEY_TRANSPLANT_SET, ever)
    ]
    if transplant:
        hits.append(
            ExclusionHit(
                category="kidney_transplant",
                source="demo_choice",
                window_label=ever.label,
                evidence=transplant,
            )
        )

    pregnancy = [
        cond_ref(c, "exclusion")
        for c in conditions_in(record, vs, PREGNANCY_SET)
        if condition_active_in(c, my)
    ]
    if pregnancy:
        hits.append(
            ExclusionHit(
                category="pregnancy",
                source="quoted",
                window_label=my.label,
                evidence=pregnancy,
            )
        )

    pregnancy_status = [
        obs_ref(o, "exclusion")
        for o in observations_with_code(record, PREGNANCY_STATUS_LOINC, my)
        if o.value_code in PREGNANT_VALUE_CODES
    ]
    if pregnancy_status:
        hits.append(
            ExclusionHit(
                category="pregnancy_status_positive",
                source="demo_choice",
                window_label=my.label,
                evidence=pregnancy_status,
            )
        )
    return hits


def _escalations(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets
) -> list[EscalationFlag]:
    """E6: hypertension abated inside [my_start, as_of] (E5 comes from the BP assessment)."""
    window = measurement_year(ctx.as_of)
    abated = [
        cond_ref(c, "escalation")
        for c in conditions_in(record, vs, HYPERTENSION_SET)
        if window.contains(c.abatement_date)
    ]
    if not abated:
        return []
    return [
        EscalationFlag(
            kind="E6",
            scope="measure",
            reason=f"hypertension condition abated inside {_fmt(window)}",
            evidence=abated,
        )
    ]


class CbpRule:
    """C14 Controlling High Blood Pressure (demo-grade)."""

    measure_id: MeasureId = "CBP"
    rule_version: str = "cbp-v1"
    value_set_ids: tuple[str, ...] = (
        HYPERTENSION_SET,
        BP_SET,
        ESRD_SET,
        DIALYSIS_SET,
        KIDNEY_TRANSPLANT_SET,
        PREGNANCY_SET,
    )

    def evaluate(
        self, record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
    ) -> RuleOutput:
        bp = _assess_bp(record, ctx, value_sets)
        return RuleOutput(
            denominator=_denominator(record, ctx, value_sets),
            numerator=bp.numerator,
            exclusions=_exclusions(record, ctx, value_sets),
            coverage=dict(COVERAGE),
            escalations=[*bp.escalations, *_escalations(record, ctx, value_sets)],
        )
