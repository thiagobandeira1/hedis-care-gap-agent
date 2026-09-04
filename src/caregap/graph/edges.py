"""Conditional edges: pure functions of ``GapState`` returning the next node name.

Names match the SPEC section 3 diagram. ``after_evaluate`` sends every candidate that is not a
clean ``gap_open`` (escalated, or ``needs_review`` for an unknown denominator / numerator) to
``validate_gaps`` — the hold-and-review node — so ``draft_actions`` only ever runs with the
open-gap list already settled.
"""

from typing import Literal

from caregap.graph.state import GapState


def after_load(state: GapState) -> Literal["finalize", "evaluate_measures"]:
    return "finalize" if state.get("load_error") is not None else "evaluate_measures"


def after_evaluate(state: GapState) -> Literal["finalize", "validate_gaps", "draft_actions"]:
    candidates = [e for e in state.get("evaluations", []) if e.is_candidate]
    if not candidates:
        return "finalize"
    if any(e.escalations or e.verdict == "needs_review" for e in candidates):
        return "validate_gaps"
    return "draft_actions"


def after_validate(state: GapState) -> Literal["await_approval", "draft_actions", "finalize"]:
    review_items = state.get("review_items", [])
    if any(item.scope == "global" for item in review_items):
        return "await_approval"
    if state.get("open_gaps"):
        return "draft_actions"
    if review_items:
        return "await_approval"
    return "finalize"


def after_decision(state: GapState) -> Literal["draft_actions", "finalize"]:
    decision = state.get("decision")
    if (
        decision is not None
        and decision.action == "revise"
        and state.get("revision_count", 0) <= state["options"].max_revisions
    ):
        return "draft_actions"
    return "finalize"
