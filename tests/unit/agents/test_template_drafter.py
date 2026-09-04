"""``TemplateDrafter`` passes ``lint_plan`` by construction — every measure singly (with its
subtypes), all eight at once, with review items present — and is deterministic."""

from datetime import date

import pytest

from caregap.agents.drafter import (
    OPT_OUT_LINE,
    DrafterContext,
    finalize_plan,
    lint_plan,
)
from caregap.agents.schemas import CareActionPlan
from caregap.agents.template_drafter import (
    ACTION_KIND,
    ACTION_OWNER,
    PATIENT_STEP,
    PATIENT_STEP_BY_SUBTYPE,
    URGENCY,
    TemplateDrafter,
)
from caregap.measures.ids import ALL_MEASURES, MEASURE_NAMES, MeasureId
from caregap.measures.models import EvidenceRef, OpenGap, ReviewItem
from tests.factories import EVAL_AS_OF

CLINIC = "Springfield Clinic 7"
PHONE = "(555) 010-2000"

SUBTYPES: dict[MeasureId, list[str | None]] = {
    "CBP": [None, "no_bp_in_my"],
    "EED": [None, "prior_year_exam_without_negative_result", "prior_year_exam_with_retinopathy"],
    "BCS": [None],
    "COL": [None],
    "SPC": [None, "low_intensity_only"],
    "SPD": [None],
    "TSC": [None, "status_without_value"],
    "SNS": [None],
}


def context(**overrides: object) -> DrafterContext:
    base: dict[str, object] = {
        "patient_id": "p1",
        "as_of": EVAL_AS_OF,
        "age_band": "65-74",
        "sex": "female",
        "chronic_flags": ["diabetes", "hypertension"],
        "encounters_in_my": 3,
        "positive_sdoh_domains": ["63586-2"],
        "clinic_name": CLINIC,
        "clinic_phone": PHONE,
    }
    base.update(overrides)
    return DrafterContext.model_validate(base)


def evidence(measure_id: MeasureId, n: int) -> list[EvidenceRef]:
    """``n`` dated refs with record text that must never reach the patient message."""
    return [
        EvidenceRef(
            section="observations",
            event_id=f"{measure_id.lower()}-evt-{i}",
            code=f"1234{i}-5",
            code_system="LOINC",
            display=f"Record text {i} for {measure_id} 99999",
            event_date=date(2025, 1 + i, 10 + i),
            role="numerator",
        )
        for i in range(n)
    ]


def gap(measure_id: MeasureId, rank: int, subtype: str | None = None, dates: int = 2) -> OpenGap:
    return OpenGap(
        measure_id=measure_id,
        subtype=subtype,
        evidence=evidence(measure_id, dates),
        priority_score=float(20 - rank),
        rank=rank,
    )


def all_eight() -> list[OpenGap]:
    # Ranked in a deliberately shuffled input order; subtypes on the two bonus measures.
    order: list[MeasureId] = ["SNS", "CBP", "EED", "SPC", "BCS", "TSC", "COL", "SPD"]
    ranks = {"CBP": 1, "SPC": 2, "EED": 3, "COL": 4, "BCS": 5, "SPD": 6, "TSC": 7, "SNS": 8}
    subtype = {"CBP": "no_bp_in_my", "SPC": "low_intensity_only"}
    return [gap(m, ranks[m], subtype.get(m), dates=5) for m in order]


def violations(
    plan: CareActionPlan,
    open_gaps: list[OpenGap],
    *,
    review: set[str] | None = None,
    allowed: set[str] | None = None,
) -> list[str]:
    return [
        v.code
        for v in lint_plan(
            plan,
            open_gaps=open_gaps,
            review_measure_ids=review or set(),
            allowed_numbers=set() if allowed is None else allowed,
            clinic_name=CLINIC,
            clinic_phone=PHONE,
        )
    ]


_SINGLE = [pytest.param(m, s, id=f"{m}-{s or 'none'}") for m in ALL_MEASURES for s in SUBTYPES[m]]


@pytest.mark.parametrize(("measure_id", "subtype"), _SINGLE)
def test_single_gap_passes_lint_with_no_allowed_numbers(
    measure_id: MeasureId, subtype: str | None
) -> None:
    gaps = [gap(measure_id, 1, subtype)]
    plan = TemplateDrafter().draft(gaps, [], context())
    assert violations(plan, gaps) == []
    assert [g.measure_id for g in plan.gaps] == [measure_id]
    assert plan.gaps[0].rank == 1
    assert plan.gaps[0].urgency == URGENCY[measure_id]
    assert [(a.action_id, a.measure_id, a.kind, a.owner) for a in plan.actions] == [
        ("a1", measure_id, ACTION_KIND[measure_id], ACTION_OWNER[measure_id])
    ]
    expected_step = PATIENT_STEP_BY_SUBTYPE.get(
        (measure_id, subtype or ""), PATIENT_STEP[measure_id]
    )
    assert expected_step in plan.patient_message
    assert "one thing that is due" in plan.patient_message
    assert MEASURE_NAMES[measure_id] in plan.provider_note
    assert "2025-02-11" in plan.provider_note  # the decisive evidence date reaches the provider
    assert "2025" not in plan.patient_message  # ... but never the patient
    assert "Record text" not in plan.patient_message
    assert "Record text" not in plan.provider_note


def test_action_kind_per_measure_matches_the_assignment() -> None:
    assert ACTION_KIND == {
        "CBP": "schedule_visit",
        "EED": "referral",
        "BCS": "screening",
        "COL": "screening",
        "SPC": "medication_review",
        "SPD": "medication_review",
        "TSC": "other",
        "SNS": "other",
    }
    assert set(ACTION_OWNER) == set(URGENCY) == set(PATIENT_STEP) == set(ALL_MEASURES)


def test_all_eight_open_at_once_passes_lint_and_fits_every_cap() -> None:
    gaps = all_eight()
    plan = TemplateDrafter().draft(gaps, [], context())
    assert violations(plan, gaps) == []
    assert [g.measure_id for g in plan.gaps] == [
        "CBP",
        "SPC",
        "EED",
        "COL",
        "BCS",
        "SPD",
        "TSC",
        "SNS",
    ]
    assert [g.rank for g in plan.gaps] == list(range(1, 9))
    assert [a.action_id for a in plan.actions] == [f"a{i}" for i in range(1, 9)]
    assert [a.measure_id for a in plan.actions] == [g.measure_id for g in plan.gaps]
    assert len(plan.patient_message) <= 1200
    assert len(plan.provider_note) <= 1500
    assert plan.patient_message.startswith("Hello,\n\n")
    assert plan.patient_message.endswith(OPT_OUT_LINE)
    assert "a few things that are due" in plan.patient_message
    assert CLINIC in plan.patient_message and PHONE in plan.patient_message
    # Steps appear in rank order.
    positions = [
        plan.patient_message.index(PATIENT_STEP_BY_SUBTYPE[("CBP", "no_bp_in_my")]),
        plan.patient_message.index(PATIENT_STEP_BY_SUBTYPE[("SPC", "low_intensity_only")]),
        plan.patient_message.index(PATIENT_STEP["EED"]),
        plan.patient_message.index(PATIENT_STEP["SNS"]),
    ]
    assert positions == sorted(positions)
    # The provider note keeps at most the three most recent dates per gap.
    assert "2025-03-12, 2025-04-13, 2025-05-14" in plan.provider_note
    assert "2025-01-10" not in plan.provider_note


def test_cancer_appears_only_inside_the_two_screening_phrases() -> None:
    gaps = [gap("BCS", 1), gap("COL", 2)]
    message = TemplateDrafter().draft(gaps, [], context()).patient_message.lower()
    stripped = message.replace("breast cancer screening", "").replace(
        "colorectal cancer screening", ""
    )
    assert "cancer" in message
    assert "cancer" not in stripped


def test_review_items_get_no_gap_no_action_no_outreach_but_reach_the_provider_note() -> None:
    gaps = [gap("CBP", 1, "no_bp_in_my")]
    review = [
        ReviewItem(measure_id="SPC", scope="measure", reason="validator_failed"),
        ReviewItem(measure_id="COL", scope="measure", reason="E7 ambiguous colon code"),
        ReviewItem(measure_id=None, scope="global", reason="E1 hospice before MY"),
    ]
    plan = TemplateDrafter().draft(gaps, review, context())
    assert violations(plan, gaps, review={"SPC", "COL"}) == []
    assert {g.measure_id for g in plan.gaps} == {"CBP"}
    assert {a.measure_id for a in plan.actions} == {"CBP"}
    assert "cholesterol" not in plan.patient_message
    assert "colorectal" not in plan.patient_message
    assert (
        "Pending clinical review, no outreach drafted: GLOBAL (global): E1 hospice before MY; "
        "COL (measure): E7 ambiguous colon code; SPC (measure): validator_failed."
    ) in plan.provider_note


def test_no_open_gaps_yields_an_empty_plan_that_passes_lint() -> None:
    review = [ReviewItem(measure_id="EED", scope="measure", reason="validator_failed")]
    plan = TemplateDrafter().draft([], review, context())
    assert plan.gaps == [] and plan.actions == []
    assert plan.patient_message == ""
    assert "No open gaps to draft." in plan.provider_note
    assert "EED (measure): validator_failed" in plan.provider_note
    assert violations(plan, [], review={"EED"}) == []


def test_template_is_deterministic_and_input_order_independent() -> None:
    gaps = all_eight()
    once = TemplateDrafter().draft(gaps, [], context())
    again = TemplateDrafter().draft(list(reversed(gaps)), [], context())
    assert once == again
    assert once.model_dump_json() == again.model_dump_json()


def test_finalize_is_a_no_op_on_a_template_plan() -> None:
    gaps = all_eight()
    plan = TemplateDrafter().draft(gaps, [], context())
    assert finalize_plan(plan, gaps) == plan


def test_template_survives_a_hostile_clinic_name_and_odd_phone() -> None:
    hostile = context(clinic_name="Clinic 12345-6 take dose", clinic_phone="555-0100")
    gaps = [gap("EED", 1)]
    plan = TemplateDrafter().draft(gaps, [], hostile)
    # The name/phone are scrubbed for the code/number checks; the forbidden-phrase check is
    # not scrubbed, so a clinic name carrying forbidden words is (correctly) rejected.
    codes = [
        v.code
        for v in lint_plan(
            plan,
            open_gaps=gaps,
            review_measure_ids=set(),
            allowed_numbers=set(),
            clinic_name=hostile.clinic_name,
            clinic_phone=hostile.clinic_phone,
        )
    ]
    assert codes == ["FORBIDDEN"]
    sane = context(clinic_name="Clinic 12345-6", clinic_phone="555-0100")
    plan = TemplateDrafter().draft(gaps, [], sane)
    assert [
        v.code
        for v in lint_plan(
            plan,
            open_gaps=gaps,
            review_measure_ids=set(),
            allowed_numbers=set(),
            clinic_name=sane.clinic_name,
            clinic_phone=sane.clinic_phone,
        )
    ] == []
