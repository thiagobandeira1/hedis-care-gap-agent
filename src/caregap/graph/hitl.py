"""Human-in-the-loop helpers: decision validation (pure), interrupt payload parsing, and the
pending-request lookup derived from ``graph.get_state`` (never from stored status).
"""

from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Interrupt

from caregap.agents.drafter import DrafterContext, allowed_numbers_for, lint_plan
from caregap.graph.state import ApprovalDecision, ApprovalRequest


def validate_decision(
    decision: ApprovalDecision,
    request: ApprovalRequest,
    *,
    max_revisions: int,
    clinic_name: str = "",
    clinic_phone: str = "",
) -> list[str]:
    """Every rule the API enforces before resuming (422 when non-empty). ``clinic_name`` /
    ``clinic_phone`` feed the edited-plan re-lint; the request itself carries neither."""
    errors: list[str] = []
    if decision.action == "auto_reviewed":
        errors.append("auto_reviewed is recorded by approval_mode=auto, never submitted")
    if decision.action == "edit":
        if decision.edited_plan is None:
            errors.append("edit requires edited_plan")
        else:
            review_ids: set[str] = {
                i.measure_id for i in request.review_items if i.measure_id is not None
            }
            # The drafter's own allowed-number rule (evidence dates, as_of, clinic phone);
            # the other context fields play no part in lint.
            lint_context = DrafterContext(
                patient_id=request.patient_id,
                as_of=request.as_of,
                age_band="unknown",
                sex="unknown",
                clinic_name=clinic_name,
                clinic_phone=clinic_phone,
            )
            violations = lint_plan(
                decision.edited_plan,
                open_gaps=request.open_gaps,
                review_measure_ids=review_ids,
                allowed_numbers=allowed_numbers_for(request.open_gaps, lint_context),
                clinic_name=clinic_name,
                clinic_phone=clinic_phone,
            )
            errors.extend(f"edited_plan: {v.code}: {v.detail}" for v in violations)
    if decision.action == "revise":
        if not (decision.feedback or "").strip():
            errors.append("revise requires feedback")
        if request.revision_count >= max_revisions:
            errors.append(
                f"revision budget exhausted ({request.revision_count}/{max_revisions} used)"
            )
        opens_something = any(r.status == "open" for r in decision.review_resolutions)
        if not request.open_gaps and not opens_something:
            errors.append("revise requires an open gap or a review resolution to 'open'")
    seen: set[str | None] = set()
    has_global = any(i.scope == "global" for i in request.review_items)
    for resolution in decision.review_resolutions:
        label = resolution.measure_id or "global"
        if resolution.measure_id in seen:
            errors.append(f"duplicate resolution for {label}")
        seen.add(resolution.measure_id)
        if resolution.measure_id is None:
            if not has_global:
                errors.append("global resolution does not reference a global review item")
            if resolution.status == "open":
                errors.append("a global review item cannot be resolved to 'open'")
            continue
        if not any(i.measure_id == resolution.measure_id for i in request.review_items):
            errors.append(f"resolution for {label} does not reference a review item")
        if resolution.status == "open" and decision.action != "revise":
            errors.append(f"resolution of {label} to 'open' requires action 'revise'")
    return errors


def request_from_interrupt(value: object) -> ApprovalRequest:
    """The interrupt payload is ``ApprovalRequest.model_dump(mode="json")``; accept the raw
    ``Interrupt`` too so callers can pass whatever ``get_state`` handed them."""
    if isinstance(value, Interrupt):
        value = value.value
    if isinstance(value, ApprovalRequest):
        return value
    return ApprovalRequest.model_validate(value)


def pending_request(
    graph: CompiledStateGraph[Any, Any, Any, Any], thread_id: str
) -> ApprovalRequest | None:
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    for task in snapshot.tasks:
        for pending in task.interrupts:
            return request_from_interrupt(pending.value)
    return None
