"""Rendering helpers over the frozen graph payloads. Pure presentation: every function takes
models the API returned and calls ``st.*``; none computes a verdict, status or priority.

The approval card is the one component that produces a value: it returns the
``ApprovalDecision`` the reviewer just asked for (with a client-generated ``decision_id``) or
``None`` when no button was pressed or the form is incomplete; the page posts it.
"""

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any, Literal

import streamlit as st

from caregap.agents.schemas import CareActionPlan, ValidationVerdict
from caregap.graph.runstore import ApprovalRecord, OutboxEntry, PatientRunRecord, RunRecord
from caregap.graph.state import (
    AgentError,
    ApprovalDecision,
    ApprovalRequest,
    MeasurementSummary,
    NodeTrace,
    ResolutionStatus,
    ReviewResolution,
    RunOutcome,
)
from caregap.measures.ids import MEASURE_NAMES
from caregap.measures.models import MeasureEvaluation, OpenGap, ReviewItem
from caregap.ui.client import PanelItem

DEMO_BANNER = "demo-grade; HEDIS-aligned; not NCQA-certified; synthetic data only"

ChipColor = Literal["red", "orange", "green", "blue", "gray", "violet"]

VERDICT_LABELS: dict[str, str] = {
    "not_eligible": "not eligible",
    "closed": "closed",
    "excluded": "excluded",
    "gap_open": "gap open",
    "needs_review": "needs review",
    "open": "open",
}
VERDICT_COLORS: dict[str, ChipColor] = {
    "not_eligible": "gray",
    "closed": "green",
    "excluded": "blue",
    "gap_open": "red",
    "needs_review": "orange",
    "open": "red",
}
DECISION_COLORS: dict[str, ChipColor] = {
    "confirm_open": "red",
    "exclude": "blue",
    "numerator_met": "green",
    "needs_human": "orange",
}
STATUS_COLORS: dict[str, ChipColor] = {
    "queued": "gray",
    "created": "gray",
    "running": "orange",
    "awaiting_approval": "violet",
    "completed": "green",
    "no_action": "gray",
    "rejected": "blue",
    "cancelled": "gray",
    "error": "red",
    "pending": "violet",
    "superseded": "gray",
    "resolved": "green",
}

APPROVE_LABEL = "Approve"
EDIT_LABEL = "Edit & approve"
REVISE_LABEL = "Request revision"
REJECT_LABEL = "Reject"

RESOLUTION_NONE = "leave unresolved"
RESOLUTION_STATUSES: dict[str, ResolutionStatus] = {
    "open": "open",
    "excluded": "excluded",
    "closed": "closed",
    "not_eligible": "not_eligible",
}
RESOLUTION_OPTIONS: tuple[str, ...] = (RESOLUTION_NONE, *RESOLUTION_STATUSES)


def chip(text: str, color: ChipColor) -> str:
    """Streamlit markdown badge (``:color-background[...]``), no HTML."""
    return f":{color}-background[{text}]"


def verdict_chip(evaluation: MeasureEvaluation) -> str:
    label = VERDICT_LABELS.get(evaluation.verdict, evaluation.verdict)
    return chip(
        f"**{evaluation.measure_id}** {label}", VERDICT_COLORS.get(evaluation.verdict, "gray")
    )


def status_chip(status: str) -> str:
    return chip(status.replace("_", " "), STATUS_COLORS.get(status, "gray"))


def _date(value: date | None) -> str:
    return "" if value is None else value.isoformat()


# --- banner and context ---------------------------------------------------------------------


def render_banner() -> None:
    """Persistent demo-grade banner, first element of every page."""
    st.warning(DEMO_BANNER)


def render_context(summary: MeasurementSummary) -> None:
    cols = st.columns(4)
    cols[0].metric("as_of", summary.as_of.isoformat())
    cols[1].metric("MY start", summary.my_start.isoformat())
    cols[2].metric("MY end", summary.my_end.isoformat())
    age = "unknown" if summary.age_at_my_end is None else str(summary.age_at_my_end)
    cols[3].metric("Age at MY end", age)


# --- engine output --------------------------------------------------------------------------


def render_verdict_chips(evaluations: Sequence[MeasureEvaluation], *, per_row: int = 4) -> None:
    """One chip per measure, in the order the API returned them."""
    if not evaluations:
        st.caption("No measure evaluations.")
        return
    for start in range(0, len(evaluations), per_row):
        cols = st.columns(per_row)
        for col, evaluation in zip(cols, evaluations[start : start + per_row], strict=False):
            with col:
                st.markdown(verdict_chip(evaluation))
                details = [MEASURE_NAMES.get(evaluation.measure_id, evaluation.measure_id)]
                if evaluation.numerator.subtype:
                    details.append(f"subtype: {evaluation.numerator.subtype}")
                if evaluation.priority_score:
                    details.append(f"priority {evaluation.priority_score:.1f}")
                st.caption(" · ".join(details))


def evidence_rows(evaluations: Sequence[MeasureEvaluation]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for evaluation in evaluations:
        parts: list[tuple[str, str, Sequence[Any]]] = [
            ("denominator", "", evaluation.denominator.evidence),
            ("numerator", evaluation.numerator.subtype or "", evaluation.numerator.evidence),
        ]
        parts.extend(
            (f"exclusion: {hit.category}", hit.window_label, hit.evidence)
            for hit in evaluation.exclusions
        )
        parts.extend(
            (f"escalation {flag.kind}", flag.reason, flag.evidence)
            for flag in evaluation.escalations
        )
        for part, note, refs in parts:
            for ref in refs:
                rows.append(
                    {
                        "measure": evaluation.measure_id,
                        "part": part,
                        "role": ref.role,
                        "section": ref.section,
                        "code": ref.code or "",
                        "system": ref.code_system or "",
                        "display": ref.display or "",
                        "date": _date(ref.event_date),
                        "event_id": ref.event_id,
                        "note": note,
                    }
                )
    return rows


def render_evidence_table(evaluations: Sequence[MeasureEvaluation]) -> None:
    rows = evidence_rows(evaluations)
    if not rows:
        st.caption("No evidence references.")
        return
    st.dataframe(rows, hide_index=True)


def coverage_rows(evaluations: Sequence[MeasureEvaluation]) -> list[dict[str, str]]:
    return [
        {"measure": evaluation.measure_id, "element": element, "coverage": coverage}
        for evaluation in evaluations
        for element, coverage in sorted(evaluation.coverage.items())
    ]


def render_coverage_table(evaluations: Sequence[MeasureEvaluation]) -> None:
    rows = coverage_rows(evaluations)
    if not rows:
        st.caption("No coverage entries.")
        return
    st.dataframe(rows, hide_index=True)


def render_panel_table(items: Sequence[PanelItem]) -> None:
    if not items:
        st.caption("The panel is empty.")
        return
    rows = [
        {
            "patient_id": item.patient_id,
            "sex": item.sex,
            "birth_date": _date(item.birth_date),
            "deceased": item.deceased,
            "last_run": item.last_run.run_id if item.last_run else "",
            "last_status": item.last_run.status if item.last_run else "",
        }
        for item in items
    ]
    st.dataframe(rows, hide_index=True)


# --- graph state ----------------------------------------------------------------------------


def render_validator_cards(verdicts: Sequence[ValidationVerdict]) -> None:
    if not verdicts:
        st.caption("No validator verdicts (nothing was escalated).")
        return
    for verdict in verdicts:
        with st.container(border=True):
            verified = chip("verified", "green") if verdict.verified else chip("unverified", "red")
            st.markdown(
                f"**{verdict.measure_id}** "
                f"{chip(verdict.decision, DECISION_COLORS.get(verdict.decision, 'gray'))} "
                f"{verified} confidence: {verdict.confidence}"
            )
            if verdict.exclusion_category:
                st.caption(f"exclusion category: {verdict.exclusion_category}")
            if verdict.rule_citation:
                st.caption(f"rule: {verdict.rule_citation}")
            if verdict.rationale:
                st.write(verdict.rationale)
            if verdict.evidence_ids:
                st.caption("evidence: " + ", ".join(verdict.evidence_ids))
            if verdict.verification_note:
                st.caption(f"verification: {verdict.verification_note}")


def render_review_items(items: Sequence[ReviewItem]) -> None:
    if not items:
        return
    st.markdown("**Review items**")
    for item in items:
        scope = item.measure_id or "GLOBAL"
        st.markdown(f"- {chip(scope, 'orange')} ({item.scope}) {item.reason}")


def render_open_gaps(gaps: Sequence[OpenGap]) -> None:
    if not gaps:
        return
    st.markdown("**Open gaps**")
    rows = [
        {
            "rank": gap.rank,
            "measure": gap.measure_id,
            "subtype": gap.subtype or "",
            "priority": gap.priority_score,
            "source": gap.source,
            "evidence": len(gap.evidence),
        }
        for gap in sorted(gaps, key=lambda g: g.rank)
    ]
    st.dataframe(rows, hide_index=True)


def render_plan(plan: CareActionPlan | None, draft_error: str | None, revision_count: int) -> None:
    st.markdown(f"**Care-action plan** (revisions used: {revision_count})")
    if draft_error:
        st.warning(f"Drafter fell back to the template: {draft_error}")
    if plan is None:
        st.caption("No plan drafted.")
        return
    if plan.gaps:
        st.dataframe(
            [
                {
                    "rank": gap.rank,
                    "measure": gap.measure_id,
                    "urgency": gap.urgency,
                    "rationale": gap.rationale,
                }
                for gap in plan.gaps
            ],
            hide_index=True,
        )
    if plan.actions:
        st.dataframe(
            [
                {
                    "action_id": action.action_id,
                    "measure": action.measure_id,
                    "kind": action.kind,
                    "owner": action.owner,
                    "detail": action.detail,
                }
                for action in plan.actions
            ],
            hide_index=True,
        )
    if plan.patient_message:
        st.markdown("*Patient message*")
        st.text(plan.patient_message)
    if plan.provider_note:
        st.markdown("*Provider note*")
        st.text(plan.provider_note)


def render_trace(trace: Sequence[NodeTrace], errors: Sequence[AgentError]) -> None:
    if trace:
        st.dataframe(
            [
                {
                    "node": step.node,
                    "ms": step.duration_ms,
                    "model": step.model_id or "",
                    "prompt": step.prompt_version or "",
                    "case_key": step.case_key or "",
                }
                for step in trace
            ],
            hide_index=True,
        )
    else:
        st.caption("No trace.")
    for error in errors:
        st.caption(
            f"agent error: {error.node} {error.schema_name} {error.error_class} "
            f"(attempt {error.attempt})"
        )


def render_outcome(outcome: RunOutcome) -> None:
    actionable = chip("actionable", "green") if outcome.actionable else chip("no action", "gray")
    st.markdown(f"**Outcome** {status_chip(outcome.status)} {actionable}")
    if outcome.decision_action:
        st.caption(f"decision: {outcome.decision_action}")
    if outcome.load_error:
        st.error(f"load error: {outcome.load_error.kind} {outcome.load_error.detail}".strip())
    measures = sorted(set(outcome.engine_verdicts) | set(outcome.final_statuses))
    if measures:
        st.dataframe(
            [
                {
                    "measure": measure,
                    "engine": outcome.engine_verdicts.get(measure, ""),
                    "final": outcome.final_statuses.get(measure, ""),
                }
                for measure in measures
            ],
            hide_index=True,
        )
    if outcome.approved_actions:
        st.caption("approved actions: " + ", ".join(outcome.approved_actions))


# --- runs -----------------------------------------------------------------------------------


def render_run_stepper(run: RunRecord, patients: Sequence[PatientRunRecord]) -> None:
    """Progress over ``run.patient_ids`` (the run's own order); patients not yet started show
    as queued. Statuses come from the ledger, never derived here."""
    by_id = {record.patient_id: record for record in patients}
    total = len(run.patient_ids)
    done = sum(1 for pid in run.patient_ids if pid in by_id and by_id[pid].status != "running")
    st.markdown(f"**Run {run.run_id}** {status_chip(run.status)} · as_of {run.as_of.isoformat()}")
    st.progress(0.0 if total == 0 else done / total, text=f"{done}/{total} patients")
    for index, patient_id in enumerate(run.patient_ids, start=1):
        record = by_id.get(patient_id)
        status = "queued" if record is None else record.status
        line = f"{index}. `{patient_id}` {status_chip(status)}"
        if record is not None and record.outcome is not None and record.outcome.decision_action:
            line += f" · {record.outcome.decision_action}"
        st.markdown(line)


# --- audit ----------------------------------------------------------------------------------


def render_outbox_table(entries: Sequence[OutboxEntry]) -> None:
    if not entries:
        st.caption("The outbox is empty.")
        return
    st.dataframe(
        [
            {
                "created_at": entry.created_at,
                "run_id": entry.run_id,
                "patient_id": entry.patient_id,
                "measure": entry.measure_id,
                "action_id": entry.action_id,
                "kind": entry.kind,
                "owner": entry.owner,
                "detail": entry.detail,
                "approval_ref": entry.approval_ref,
            }
            for entry in entries
        ],
        hide_index=True,
    )


def render_approvals_table(records: Sequence[ApprovalRecord]) -> None:
    if not records:
        st.caption("No approval requests.")
        return
    st.dataframe(
        [
            {
                "run_id": record.run_id,
                "patient_id": record.patient_id,
                "status": record.status,
                "as_of": record.request.as_of.isoformat(),
                "open_gaps": ", ".join(gap.measure_id for gap in record.request.open_gaps),
                "review_items": len(record.request.review_items),
                "revisions": record.request.revision_count,
                "superseded_by": record.superseded_by or "",
            }
            for record in records
        ],
        hide_index=True,
    )


# --- approval card --------------------------------------------------------------------------


def _resolution_inputs(request: ApprovalRequest, key: str) -> list[ReviewResolution] | None:
    """One status + reason pair per review item; ``None`` when a chosen status lacks a reason."""
    resolutions: list[ReviewResolution] = []
    if not request.review_items:
        return resolutions
    st.markdown("**Resolve review items**")
    incomplete = False
    for index, item in enumerate(request.review_items):
        label = item.measure_id or "GLOBAL"
        item_key = f"{key}:resolution:{index}"
        cols = st.columns([2, 3])
        choice = cols[0].selectbox(f"{label} status", RESOLUTION_OPTIONS, key=f"{item_key}:status")
        reason = cols[1].text_input(f"{label} reason", key=f"{item_key}:reason") or ""
        status = RESOLUTION_STATUSES.get(choice or RESOLUTION_NONE)
        if status is None:
            continue
        if not reason.strip():
            st.error(f"A reason is required to resolve {label}.")
            incomplete = True
            continue
        resolutions.append(
            ReviewResolution(measure_id=item.measure_id, status=status, reason=reason.strip())
        )
    return None if incomplete else resolutions


def _edited_plan(plan: CareActionPlan, key: str) -> CareActionPlan:
    """Rebuild the plan from the edit widgets; only text fields are editable, structure is not."""
    patient_message = st.session_state.get(f"{key}:edit:patient_message", plan.patient_message)
    provider_note = st.session_state.get(f"{key}:edit:provider_note", plan.provider_note)
    actions = [
        action.model_copy(
            update={"detail": st.session_state.get(f"{key}:edit:action:{index}", action.detail)}
        )
        for index, action in enumerate(plan.actions)
    ]
    return plan.model_copy(
        update={
            "patient_message": patient_message,
            "provider_note": provider_note,
            "actions": actions,
        }
    )


def _render_edit_widgets(plan: CareActionPlan, key: str) -> None:
    st.text_area("Patient message", value=plan.patient_message, key=f"{key}:edit:patient_message")
    st.text_area("Provider note", value=plan.provider_note, key=f"{key}:edit:provider_note")
    for index, action in enumerate(plan.actions):
        st.text_input(
            f"{action.action_id} {action.measure_id} {action.kind} ({action.owner})",
            value=action.detail,
            key=f"{key}:edit:action:{index}",
        )


def render_approval_card(request: ApprovalRequest, *, key: str) -> ApprovalDecision | None:
    """Approve / edit & approve / request revision / reject. Returns the decision to POST, or
    ``None``. Validation here is only what pydantic would reject before the request leaves
    (reviewer, feedback, resolution reasons); the API's 422 list is the authority."""
    with st.container(border=True):
        st.subheader("Approval")
        st.caption(
            f"{request.demo_grade_notice} · run {request.run_id} · patient {request.patient_id} "
            f"· as_of {request.as_of.isoformat()} · revisions used {request.revision_count}"
        )
        if request.open_gaps:
            st.caption("open gaps: " + ", ".join(gap.measure_id for gap in request.open_gaps))
        cols = st.columns(2)
        reviewer = cols[0].text_input("Reviewer", key=f"{key}:reviewer") or ""
        note = cols[1].text_input("Note (optional)", key=f"{key}:note") or ""
        resolutions = _resolution_inputs(request, key)
        if request.plan is not None:
            with st.expander("Edit the plan before approving"):
                _render_edit_widgets(request.plan, key)
        feedback = (
            st.text_area(
                "Revision feedback (required to request a revision)", key=f"{key}:feedback"
            )
            or ""
        )
        buttons = st.columns(4)
        action: Literal["approve", "edit", "revise", "reject"] | None = None
        if buttons[0].button(APPROVE_LABEL, key=f"{key}:approve", type="primary"):
            action = "approve"
        if buttons[1].button(EDIT_LABEL, key=f"{key}:edit"):
            action = "edit"
        if buttons[2].button(REVISE_LABEL, key=f"{key}:revise"):
            action = "revise"
        if buttons[3].button(REJECT_LABEL, key=f"{key}:reject"):
            action = "reject"
        if action is None:
            return None
        if not reviewer.strip():
            st.error("Reviewer name is required.")
            return None
        if resolutions is None:
            return None
        if action == "revise" and not feedback.strip():
            st.error("Feedback is required to request a revision.")
            return None
        edited_plan: CareActionPlan | None = None
        if action == "edit":
            if request.plan is None:
                st.error("Nothing to edit: the request carries no plan.")
                return None
            edited_plan = _edited_plan(request.plan, key)
        return ApprovalDecision(
            decision_id=str(uuid.uuid4()),
            action=action,
            edited_plan=edited_plan,
            feedback=feedback.strip() or None,
            review_resolutions=resolutions,
            reviewer=reviewer.strip(),
            note=note.strip(),
        )
