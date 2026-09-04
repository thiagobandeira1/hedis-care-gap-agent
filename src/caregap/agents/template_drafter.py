"""``TemplateDrafter`` — the deterministic fallback when the model's plan fails lint twice or
never parses. Pure over (open gaps, review items, context); passes ``lint_plan`` by
construction: fixed grade-8 sentences, the generic salutation, the clinic name and phone, the
opt-out line, one step per open gap, no digits outside the clinic phone, no codes, no
forbidden phrases, and "cancer" only inside the two screening phrases.
"""

from typing import TYPE_CHECKING, Final

from caregap.agents.schemas import (
    ActionKind,
    ActionOwner,
    CareActionPlan,
    GapAction,
    PlannedGap,
    Urgency,
)
from caregap.measures.ids import MEASURE_NAMES, MeasureId
from caregap.measures.models import OpenGap, ReviewItem

if TYPE_CHECKING:  # DrafterContext lives in drafter.py, which imports this module.
    from caregap.agents.drafter import DrafterContext

#: SPEC section 4 / assignment: action kind per measure.
ACTION_KIND: Final[dict[MeasureId, ActionKind]] = {
    "CBP": "schedule_visit",
    "EED": "referral",
    "BCS": "screening",
    "COL": "screening",
    "SPC": "medication_review",
    "SPD": "medication_review",
    "TSC": "other",
    "SNS": "other",
}
ACTION_OWNER: Final[dict[MeasureId, ActionOwner]] = {
    "CBP": "care_team",
    "EED": "care_team",
    "BCS": "care_team",
    "COL": "care_team",
    "SPC": "provider",
    "SPD": "provider",
    "TSC": "care_team",
    "SNS": "care_team",
}
#: Outcome / therapy measures are "soon"; screenings are "routine".
URGENCY: Final[dict[MeasureId, Urgency]] = {
    "CBP": "soon",
    "EED": "routine",
    "BCS": "routine",
    "COL": "routine",
    "SPC": "soon",
    "SPD": "routine",
    "TSC": "routine",
    "SNS": "routine",
}

#: One plain-language step per measure (no digits, no codes, no forbidden substrings).
PATIENT_STEP: Final[dict[MeasureId, str]] = {
    "CBP": "Please call us to book a visit so we can check your blood pressure.",
    "EED": "Please call us to book your yearly eye exam. We can help you find a place.",
    "BCS": "You are due for breast cancer screening. Please call us to book a mammogram.",
    "COL": (
        "You are due for colorectal cancer screening. Please call us to talk about your choices."
    ),
    "SPC": "Please call us so your care team can go over your heart and cholesterol medicines.",
    "SPD": "Please call us so your care team can go over your medicines with you.",
    "TSC": "At your next visit, we will ask a few short questions about tobacco use.",
    "SNS": (
        "At your next visit, we will ask a few short questions about needs like food and housing."
    ),
}
PATIENT_STEP_BY_SUBTYPE: Final[dict[tuple[MeasureId, str], str]] = {
    ("CBP", "no_bp_in_my"): (
        "We do not have a blood pressure reading for you this year. "
        "Please call us to book a visit so we can check it."
    ),
    ("SPC", "low_intensity_only"): (
        "Please call us so your care team can go over your cholesterol medicine with you."
    ),
}

#: Care-team rationale per measure (<= 300 chars with up to three evidence dates appended).
RATIONALE: Final[dict[MeasureId, str]] = {
    "CBP": "Blood pressure not documented as controlled in the measurement year.",
    "EED": "No qualifying diabetic eye exam on file for the measurement year.",
    "BCS": "No mammogram on file in the screening window.",
    "COL": "No qualifying colorectal cancer screening on file in the look-back window.",
    "SPC": "No moderate- or high-intensity statin on therapy in the measurement year.",
    "SPD": "No statin therapy on file in the measurement year.",
    "TSC": "No tobacco use screening with a coded result in the measurement year.",
    "SNS": "No social need screening (PRAPARE) in the measurement year.",
}
RATIONALE_BY_SUBTYPE: Final[dict[tuple[MeasureId, str], str]] = {
    ("CBP", "no_bp_in_my"): "No qualifying blood pressure panel in the measurement year.",
    ("SPC", "low_intensity_only"): (
        "Only a low-intensity statin is on therapy in the measurement year."
    ),
}

ACTION_DETAIL: Final[dict[MeasureId, str]] = {
    "CBP": (
        "Schedule an office visit with a blood pressure check; record a complete panel "
        "(systolic and diastolic) at a non-acute encounter."
    ),
    "EED": "Refer for a dilated retinal eye exam and obtain the result for the record.",
    "BCS": "Order or schedule a screening mammogram.",
    "COL": (
        "Offer colorectal cancer screening (colonoscopy or a stool-based test) and schedule "
        "the chosen option."
    ),
    "SPC": (
        "Provider to review statin therapy against the guideline intensity for ASCVD and "
        "document the plan."
    ),
    "SPD": "Provider to review statin therapy for diabetes and document the plan.",
    "TSC": "Complete tobacco use screening with a coded result at the next encounter.",
    "SNS": "Complete a social need screening (PRAPARE) at the next encounter.",
}

MAX_DATES_PER_GAP: Final = 3
MAX_PROVIDER_NOTE: Final = 1500
DEMO_NOTICE: Final = "demo-grade; HEDIS-aligned; not NCQA-certified"


def _dates_of(gap: OpenGap) -> list[str]:
    """The most recent (up to three) distinct evidence dates, ascending, ISO formatted."""
    dates = sorted({ref.event_date for ref in gap.evidence if ref.event_date is not None})
    return [d.isoformat() for d in dates[-MAX_DATES_PER_GAP:]]


def _step(gap: OpenGap) -> str:
    if gap.subtype is not None:
        by_subtype = PATIENT_STEP_BY_SUBTYPE.get((gap.measure_id, gap.subtype))
        if by_subtype is not None:
            return by_subtype
    return PATIENT_STEP[gap.measure_id]


def _rationale(gap: OpenGap) -> str:
    text = RATIONALE[gap.measure_id]
    if gap.subtype is not None:
        text = RATIONALE_BY_SUBTYPE.get((gap.measure_id, gap.subtype), text)
    dates = _dates_of(gap)
    if dates:
        text += f" Evidence dates: {', '.join(dates)}."
    return text


class TemplateDrafter:
    """Deterministic care-action plan; the same inputs always yield the same plan."""

    def draft(
        self,
        open_gaps: list[OpenGap],
        review_items: list[ReviewItem],
        context: "DrafterContext",
    ) -> CareActionPlan:
        gaps = sorted(open_gaps, key=lambda g: (g.rank, g.measure_id))
        if not gaps:
            return CareActionPlan(provider_note=self._provider_note(gaps, review_items, context))
        planned = [
            PlannedGap(
                measure_id=gap.measure_id,
                rank=gap.rank,
                urgency=URGENCY[gap.measure_id],
                rationale=_rationale(gap),
            )
            for gap in gaps
        ]
        actions = [
            GapAction(
                action_id=f"a{position}",
                measure_id=gap.measure_id,
                kind=ACTION_KIND[gap.measure_id],
                detail=ACTION_DETAIL[gap.measure_id],
                owner=ACTION_OWNER[gap.measure_id],
            )
            for position, gap in enumerate(gaps, start=1)
        ]
        return CareActionPlan(
            gaps=planned,
            actions=actions,
            patient_message=self._patient_message(gaps, context),
            provider_note=self._provider_note(gaps, review_items, context),
        )

    @staticmethod
    def _patient_message(gaps: list[OpenGap], context: "DrafterContext") -> str:
        count = "one thing that is due" if len(gaps) == 1 else "a few things that are due"
        lines = [
            "Hello,",
            "",
            f"This is a message from {context.clinic_name}. We looked at your care and found "
            f"{count}.",
            *(_step(gap) for gap in gaps),
            f"You can call us at {context.clinic_phone}. We are happy to help.",
            "",
            "Reply STOP to opt out of these messages.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _provider_note(
        gaps: list[OpenGap], review_items: list[ReviewItem], context: "DrafterContext"
    ) -> str:
        lines = [f"Care gap summary as of {context.as_of.isoformat()} ({DEMO_NOTICE})."]
        if not gaps:
            lines.append("No open gaps to draft.")
        for position, gap in enumerate(gaps, start=1):
            dates = ", ".join(_dates_of(gap)) or "none in window"
            lines.append(
                f"{position}. {gap.measure_id} - {MEASURE_NAMES[gap.measure_id]} - subtype: "
                f"{gap.subtype or 'none'} - evidence dates: {dates}."
            )
        pending = sorted(review_items, key=lambda r: (r.measure_id or "", r.scope, r.reason))
        if pending:
            items = "; ".join(
                f"{item.measure_id or 'GLOBAL'} ({item.scope}): {' '.join(item.reason.split())}"
                for item in pending
            )
            lines.append(f"Pending clinical review, no outreach drafted: {items}.")
        note = "\n".join(lines)
        if len(note) > MAX_PROVIDER_NOTE:  # unreachable for eight gaps; defensive, deterministic
            note = note[: MAX_PROVIDER_NOTE - 3] + "..."
        return note
