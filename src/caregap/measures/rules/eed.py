"""EED — Eye Exam for Patients With Diabetes (CMS 2026 Star Ratings Technical Notes, Part C
measure C11).

Public-source basis
-------------------
CMS 2026 Star Ratings Technical Notes, Part C measure **C11 Eye Exam for Patients With
Diabetes** (HEDIS EED lineage; text via P2's committed corpus, tagged rule text with citation
ids in ``rules/json/eed.json``), plus NCQA's public EED measure summary. Demo-grade,
HEDIS-aligned, **not NCQA-certified** (SPEC section 2).

Element tags
------------
quoted
    * denominator age: 18-75, evaluated at Dec 31 of the measurement year (MY);
    * denominator concept: members with diabetes (type 1 or type 2) identified during the
      MY or the year prior to the MY;
    * numerator concept: a retinal or dilated eye exam by an eye care professional in the
      MY, OR a negative retinal or dilated eye exam (negative for retinopathy) in the year
      prior to the MY;
    * exclusions: died during the MY; hospice during the MY — both are GLOBAL rules
      (``rules/global_rules.py``) and are deliberately NOT re-implemented here.
demo_choice
    * "diabetes" = any ``diabetes_snomed`` condition active in [Jan 1 of MY-1, Dec 31 of MY]
      (onset on/before the window end, abatement null or strictly after the window start —
      :func:`caregap.measures.evidence.condition_active_in`). The public claims / pharmacy
      identification paths are not implemented (diagnosis-only denominator);
    * ``prediabetes_trap_snomed`` codes NEVER count toward the denominator, even when they
      appear beside a real diabetes code (filtered out before the active-window test);
    * "eye exam in the MY" = any ``retinal_exam_proc`` procedure in [my_start, as_of];
    * "negative exam in the year prior" = a ``retinal_exam_proc`` procedure dated in
      [Jan 1 of MY-1, Dec 31 of MY-1] AND at least one retinopathy-severity observation
      (LOINC 71490-7 left eye / 71491-5 right eye) whose answer is LA18643-9 ("no apparent
      retinopathy") dated on the exam date or later inside MY-1 (an answer dated BEFORE the
      exam cannot be that exam's result) AND no ``diabetic_retinopathy_snomed`` condition
      with onset on/before that exam date (a retinopathy diagnosis on/before the exam means the
      exam could not have been negative). Each prior-year exam is tested on its own; any one
      qualifying exam closes the numerator;
    * "eye care professional" (optometrist / ophthalmologist) is not verified — P6 carries no
      practitioner qualification, so any recorded exam procedure counts;
    * numerator windows END at ``as_of`` (prospective gap detection); denominator windows use
      MY bounds;
    * open-gap subtypes ``prior_year_exam_without_negative_result`` and
      ``prior_year_exam_with_retinopathy`` explain why a prior-year exam did not qualify;
    * E6 (measure scope): a diabetes condition abated in [my_start, as_of] — exactly as CBP's
      E6 for hypertension; the abatement may be a coding artefact, so the verdict is promoted
      to needs_review. HbA1c results (``hba1c_loinc``) dated in [my_start, as_of] are attached
      as supporting evidence when present but are NOT a precondition. No diabetes-medication
      value set exists in the P1/P6 catalogue, so no medication signal is read;
    * death before the MY -> denominator ``no`` (``global_rules.died_before_my``).
not_representable (listed in the coverage table, never computed)
    * palliative care during the MY;
    * Medicare 66+ enrolled in an I-SNP or living long-term in an institution;
    * the gestational / steroid-induced diabetes refinement of the pharmacy-identified
      population (moot for a diagnosis-only denominator, listed for completeness);
    * frailty AND advanced illness (66+) is ``partial``: only the global E4 hint.
documented non-member (intentionally no code set)
    * bilateral blindness / enucleation is NOT a public C11 exclusion criterion; it is named
      here so nobody adds it.

Doctrine: deterministic and pure; order-independent over shuffled events; every date is
compared against ``ctx`` / ``windows``; ``clinical_status`` / ``verification_status`` /
``MedicationEvent.status`` are never read; features never enter; the record is read only
through :mod:`caregap.measures.evidence`. E1 / E4 are global and not re-implemented here.
"""

from caregap.measures.context import MeasurementContext
from caregap.measures.engine import RuleOutput
from caregap.measures.evidence import (
    cond_ref,
    condition_active_in,
    conditions_in,
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
    TriResult,
)
from caregap.measures.rules.global_rules import died_before_my
from caregap.measures.tri import Tri
from caregap.measures.value_sets import ValueSets
from caregap.measures.windows import Window, measurement_year, prior_year
from caregap.p6.models import ConditionEvent, ObservationEvent, PatientRecord, ProcedureEvent

#: C11 denominator age band (quoted), age at Dec 31 of the MY.
AGE_MIN = 18
AGE_MAX = 75

DIABETES_SET = "diabetes_snomed"
PREDIABETES_TRAP_SET = "prediabetes_trap_snomed"
RETINAL_EXAM_SET = "retinal_exam_proc"
RETINOPATHY_SET = "diabetic_retinopathy_snomed"
HBA1C_SET = "hba1c_loinc"

#: LOINC retinopathy-severity questions (left eye / right eye) Synthea records with a
#: diabetic retinal exam; the literal codes the public negative-prior-year rule keys on.
RETINOPATHY_SEVERITY_CODES: tuple[str, ...] = ("71490-7", "71491-5")
#: LOINC answer "no apparent retinopathy" — the only value that counts as negative.
NEGATIVE_RETINOPATHY_ANSWER = "LA18643-9"

DENOMINATOR_WINDOW_LABEL = "MY or prior year"

#: Open-gap subtypes (numerator ``no`` detail).
PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT = "prior_year_exam_without_negative_result"
PRIOR_YEAR_EXAM_WITH_RETINOPATHY = "prior_year_exam_with_retinopathy"

#: Every public C11 exclusion criterion and how far this demo rule can observe it.
COVERAGE: dict[str, Coverage] = {
    "died_during_measurement_period": "observable",  # global rule (quoted)
    "hospice_during_measurement_period": "observable",  # global rule (quoted)
    "frailty_and_advanced_illness_66_plus": "partial",  # global E4 hint only
    "palliative_care_during_measurement_period": "not_representable",
    "institutional_snp_or_long_term_institution_66_plus": "not_representable",
    # Refines the pharmacy-identified population only; this denominator is diagnosis-only.
    "gestational_or_steroid_induced_diabetes_without_diabetes_dx": "not_representable",
}
#: Criteria people expect to see that the public measure does NOT list (never coded).
NON_EXCLUSIONS: tuple[str, ...] = ("bilateral_blindness_or_enucleation",)


def _combine(*parts: Tri) -> Tri:
    """Three-valued AND: any ``no`` wins, else any ``unknown``, else ``yes``."""
    if "no" in parts:
        return "no"
    if "unknown" in parts:
        return "unknown"
    return "yes"


def _fmt(window: Window) -> str:
    return f"{window.label} [{window.start:%Y-%m-%d} .. {window.end:%Y-%m-%d}]"


def _denominator_window(ctx: MeasurementContext) -> Window:
    """[Jan 1 of MY-1, Dec 31 of MY] — built from MY bounds, never from ``as_of``."""
    return Window(start=ctx.prior_my_start, end=ctx.my_end, label=DENOMINATOR_WINDOW_LABEL)


def _age_tri(age_at_my_end: int | None) -> tuple[Tri, str]:
    if age_at_my_end is None:
        return "unknown", "birth_date unknown: age at Dec 31 of the MY cannot be computed"
    if AGE_MIN <= age_at_my_end <= AGE_MAX:
        return "yes", f"age {age_at_my_end} at Dec 31 of the MY within {AGE_MIN}-{AGE_MAX}"
    return "no", f"age {age_at_my_end} at Dec 31 of the MY outside {AGE_MIN}-{AGE_MAX}"


def _diabetes_conditions(record: PatientRecord, vs: ValueSets) -> list[ConditionEvent]:
    """``diabetes_snomed`` conditions with prediabetes-trap codes removed (sorted, stable)."""
    trap_codes = vs.codes(PREDIABETES_TRAP_SET)
    return [c for c in conditions_in(record, vs, DIABETES_SET) if c.code not in trap_codes]


def _denominator(record: PatientRecord, ctx: MeasurementContext, vs: ValueSets) -> TriResult:
    if died_before_my(record, ctx):
        return TriResult(
            value="no",
            reasons=["died before the measurement year"],
            evidence=[patient_ref("eligibility", event_date=record.patient.death_date)],
            window_start=ctx.my_start,
            window_end=ctx.my_end,
        )

    reasons: list[str] = []
    evidence: list[EvidenceRef] = [patient_ref("eligibility", event_date=record.patient.birth_date)]

    age_value, age_reason = _age_tri(ctx.age_at_my_end)
    reasons.append(age_reason)

    window = _denominator_window(ctx)
    diabetes = [c for c in _diabetes_conditions(record, vs) if condition_active_in(c, window)]
    trapped = conditions_in(record, vs, PREDIABETES_TRAP_SET)
    diabetes_value: Tri
    if diabetes:
        diabetes_value = "yes"
        reasons.append(f"diabetes condition active in {_fmt(window)}")
        evidence.extend(cond_ref(c, "eligibility") for c in diabetes)
    else:
        diabetes_value = "no"
        reasons.append(f"no diabetes condition active in {_fmt(window)}")
    if trapped:
        reasons.append(
            f"{len(trapped)} prediabetes code(s) ignored (never count toward the denominator)"
        )

    return TriResult(
        value=_combine(age_value, diabetes_value),
        reasons=reasons,
        evidence=evidence,
        window_start=window.start,
        window_end=window.end,
    )


def _negative_retinopathy_results(record: PatientRecord, window: Window) -> list[ObservationEvent]:
    """71490-7 / 71491-5 observations answered LA18643-9 inside ``window`` (sorted, stable)."""
    results: list[ObservationEvent] = []
    for code in RETINOPATHY_SEVERITY_CODES:
        results.extend(
            o
            for o in observations_with_code(record, code, window)
            if o.value_code == NEGATIVE_RETINOPATHY_ANSWER
        )
    return sorted(results, key=lambda o: (o.effective_date, o.observation_id))


def _blocked_by_retinopathy(exam: ProcedureEvent, retinopathy: list[ConditionEvent]) -> bool:
    """A retinopathy diagnosis with onset on/before the exam means it could not be negative."""
    return any(c.onset_date <= exam.performed_date for c in retinopathy)


def _numerator(record: PatientRecord, ctx: MeasurementContext, vs: ValueSets) -> TriResult:
    my = measurement_year(ctx.as_of)
    my_exams = procedures_in(record, vs, RETINAL_EXAM_SET, my)
    if my_exams:
        latest = my_exams[-1]  # procedures_in sorts by (date, id)
        return TriResult(
            value="yes",
            reasons=[f"retinal exam on {latest.performed_date.isoformat()} in {_fmt(my)}"],
            evidence=[proc_ref(p, "numerator") for p in my_exams],
            window_start=my.start,
            window_end=my.end,
        )

    prior = prior_year(ctx.as_of)
    prior_exams = procedures_in(record, vs, RETINAL_EXAM_SET, prior)
    if not prior_exams:
        return TriResult(
            value="no",
            reasons=[
                f"no retinal exam in {_fmt(my)}",
                f"no retinal exam in {_fmt(prior)}",
            ],
            window_start=prior.start,
            window_end=my.end,
        )

    negatives = _negative_retinopathy_results(record, prior)
    retinopathy = conditions_in(record, vs, RETINOPATHY_SET)
    # An exam qualifies only with a negative answer dated on/after it (inside MY-1) and no
    # retinopathy diagnosis with onset on/before it; each exam is tested on its own.
    qualifying = [
        p
        for p in prior_exams
        if not _blocked_by_retinopathy(p, retinopathy)
        and any(o.effective_date >= p.performed_date for o in negatives)
    ]
    if qualifying:
        latest = qualifying[-1]
        earliest_exam = min(p.performed_date for p in qualifying)
        linked = [o for o in negatives if o.effective_date >= earliest_exam]
        return TriResult(
            value="yes",
            reasons=[
                f"no retinal exam in {_fmt(my)}",
                f"retinal exam on {latest.performed_date.isoformat()} in {_fmt(prior)} with a "
                f"negative retinopathy result ({'/'.join(RETINOPATHY_SEVERITY_CODES)} = "
                f"{NEGATIVE_RETINOPATHY_ANSWER}) dated on/after the exam and no diabetic "
                "retinopathy diagnosis on/before the exam (demo_choice)",
            ],
            evidence=[
                *(proc_ref(p, "numerator") for p in qualifying),
                *(obs_ref(o, "numerator") for o in linked),
            ],
            window_start=prior.start,
            window_end=prior.end,
        )

    blocking = [c for c in retinopathy if any(_blocked_by_retinopathy(p, [c]) for p in prior_exams)]
    reasons = [f"no retinal exam in {_fmt(my)}"]
    subtype: str
    unblocked = [p for p in prior_exams if not _blocked_by_retinopathy(p, retinopathy)]
    if not negatives:
        subtype = PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT
        reasons.append(
            f"retinal exam in {_fmt(prior)} but no negative retinopathy result "
            f"({'/'.join(RETINOPATHY_SEVERITY_CODES)} = {NEGATIVE_RETINOPATHY_ANSWER}) in that year"
        )
    elif unblocked:
        subtype = PRIOR_YEAR_EXAM_WITHOUT_NEGATIVE_RESULT
        reasons.append(
            f"retinal exam in {_fmt(prior)} but every negative retinopathy result is dated "
            "before the exam (not linked to it)"
        )
    else:
        subtype = PRIOR_YEAR_EXAM_WITH_RETINOPATHY
    if blocking:
        reasons.append(
            f"retinal exam in {_fmt(prior)} but a diabetic retinopathy diagnosis has onset "
            "on/before the exam"
        )
    return TriResult(
        value="no",
        reasons=reasons,
        evidence=[
            *(proc_ref(p, "numerator") for p in prior_exams),
            *(obs_ref(o, "numerator") for o in negatives),
            *(cond_ref(c, "numerator") for c in blocking),
        ],
        subtype=subtype,
        window_start=prior.start,
        window_end=my.end,
    )


def _escalations(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets
) -> list[EscalationFlag]:
    """E6: diabetes abated inside [my_start, as_of]; HbA1c results in the window are
    attached as supporting evidence (never a precondition)."""
    my = measurement_year(ctx.as_of)
    abated = [c for c in _diabetes_conditions(record, vs) if my.contains(c.abatement_date)]
    if not abated:
        return []
    hba1c = observations_in(record, vs, HBA1C_SET, my)
    reason = f"diabetes condition abated inside {_fmt(my)} (possible coding artefact)"
    if hba1c:
        reason += f"; {len(hba1c)} HbA1c result(s) in the window attached"
    return [
        EscalationFlag(
            kind="E6",
            scope="measure",
            reason=reason,
            evidence=[
                *(cond_ref(c, "escalation") for c in abated),
                *(obs_ref(o, "escalation") for o in hba1c),
            ],
        )
    ]


class EedRule:
    """C11 Eye Exam for Patients With Diabetes: 18-75 with diabetes; exam in MY or negative
    exam in MY-1 (demo-grade)."""

    measure_id: MeasureId = "EED"
    rule_version: str = "eed-v1"
    value_set_ids: tuple[str, ...] = (
        DIABETES_SET,
        PREDIABETES_TRAP_SET,
        RETINAL_EXAM_SET,
        RETINOPATHY_SET,
        HBA1C_SET,
    )

    def evaluate(
        self, record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
    ) -> RuleOutput:
        return RuleOutput(
            denominator=_denominator(record, ctx, value_sets),
            numerator=_numerator(record, ctx, value_sets),
            exclusions=[],  # none observable beyond the global death / hospice rules
            coverage=dict(COVERAGE),
            escalations=_escalations(record, ctx, value_sets),
        )
