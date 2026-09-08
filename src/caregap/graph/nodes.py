"""Graph nodes: closures over ``GraphDeps`` returning partial-state updates.

Doctrine (SPEC section 3, enforced by ``tests/unit/graph/test_structure.py``):

- every node appends exactly one ``NodeTrace`` (timestamps from ``_now``, monkeypatched in
  tests);
- ``await_approval`` is the only node that pauses for a human, does no I/O and no model
  call, and never writes status;
- ``finalize`` is the only node that touches the outbox (``approval_ref`` = decision id);
- agent failures fail closed: the validator becomes a ``validator_failed`` review item, the
  drafter falls back to the ``TemplateDrafter``; both append an ``AgentError`` carrying the
  error class only — never exception text, never record content;
- ordering is deterministic everywhere (evaluations in registry order, evidence by
  ``(date, id)``, gaps by rank).
"""

import threading
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from caregap.agents.drafter import (
    allowed_numbers_for,
    build_drafter_context,
    draft_plan,
    finalize_plan,
)
from caregap.agents.drafter import drafter_case_key as _drafter_case_key
from caregap.agents.packet import build_packet
from caregap.agents.packet import validator_case_key as _validator_case_key
from caregap.agents.prompts import DRAFTER_PROMPT_SHA, PROMPT_VERSION, VALIDATOR_PROMPT_SHA
from caregap.agents.schemas import ValidationVerdict
from caregap.agents.template_drafter import TemplateDrafter
from caregap.agents.validator import resolve_candidate, validate_candidate, verify_verdict
from caregap.graph.runstore import OutboxEntry
from caregap.graph.state import (
    AgentError,
    ApprovalDecision,
    ApprovalRequest,
    GapState,
    LoadError,
    MeasurementSummary,
    NodeTrace,
    RunOutcome,
    RunStatus,
    thread_id,
)
from caregap.measures.context import MeasurementContext
from caregap.measures.models import (
    EvidenceRef,
    GapSource,
    MeasureEvaluation,
    OpenGap,
    ReviewItem,
)
from caregap.measures.priority import rank
from caregap.p6.client import (
    P6ContractError,
    P6Error,
    P6Unavailable,
    PatientNotFound,
    mask_as_of,
)
from caregap.p6.models import FeatureRow
from caregap.structured import AgentOutputError

if TYPE_CHECKING:
    from caregap.graph.build import GraphDeps


class NodeFn(Protocol):
    """A LangGraph node: partial-state update from the current state (``state`` by name —
    the runtime's node protocol is keyword-aware, a bare ``Callable`` is not)."""

    def __call__(self, state: GapState) -> dict[str, Any]: ...


ATTEMPT_FLAG = "__caregap_attempt"
"""``configurable`` key under which the runner hands every ``graph.invoke`` a private
``threading.Event``, set once the runner has abandoned that attempt (per-patient timeout).
Non-primitive and ``__``-prefixed, so LangGraph never copies it into checkpoint metadata."""


def attempt_abandoned(config: RunnableConfig | None) -> bool:
    """True when the runner gave up on the invocation this node belongs to. ``finalize`` then
    refuses side effects, so the ledger's ``error`` stays the truth (SPEC section 3)."""
    configurable = (config or {}).get("configurable") or {}
    token = configurable.get(ATTEMPT_FLAG)
    return isinstance(token, threading.Event) and token.is_set()


RESOLUTION_TO_STATUS: dict[str, str] = {
    "open": "gap_open",
    "excluded": "excluded",
    "closed": "closed",
    "needs_review": "needs_review",
    "not_eligible": "not_eligible",
}
"""Validator / reviewer resolution vocabulary -> engine verdict vocabulary, so
``RunOutcome.final_statuses`` and ``engine_verdicts`` are directly comparable."""


def _now() -> datetime:
    return datetime.now(UTC)


def _trace(
    node: str,
    started: datetime,
    *,
    model_id: str | None = None,
    prompt_version: str | None = None,
    case_key: str | None = None,
) -> NodeTrace:
    finished = _now()
    return NodeTrace(
        node=node,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_ms=max(int((finished - started).total_seconds() * 1000), 0),
        model_id=model_id,
        prompt_version=prompt_version,
        case_key=case_key,
    )


def _context_of(state: GapState) -> MeasurementContext:
    return MeasurementContext.for_(state["as_of"], state["record"].patient.birth_date)


def _sorted_refs(refs: Sequence[EvidenceRef]) -> list[EvidenceRef]:
    return sorted(refs, key=lambda r: (r.event_date or date.min, r.event_id))


def _escalation_evidence(evaluation: MeasureEvaluation) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    for flag in evaluation.escalations:
        refs.extend(flag.evidence)
    return _sorted_refs(refs)


def _open_gaps_from(opened: Sequence[tuple[MeasureEvaluation, GapSource]]) -> list[OpenGap]:
    positions = dict(rank([(e.measure_id, e.priority_score) for e, _ in opened]))
    gaps = [
        OpenGap(
            measure_id=e.measure_id,
            subtype=e.numerator.subtype,
            evidence=_sorted_refs(e.numerator.evidence),
            priority_score=e.priority_score,
            rank=positions[e.measure_id],
            source=source,
        )
        for e, source in opened
    ]
    return sorted(gaps, key=lambda g: g.rank)


def _measure_item(evaluation: MeasureEvaluation, reason: str) -> ReviewItem:
    return ReviewItem(
        measure_id=evaluation.measure_id,
        scope="measure",
        reason=reason,
        evidence=_escalation_evidence(evaluation),
    )


def _engine_review_reason(evaluation: MeasureEvaluation) -> str:
    reasons: list[str] = []
    if evaluation.denominator.value == "unknown":
        reasons.extend(evaluation.denominator.reasons)
    if evaluation.numerator.value == "unknown":
        reasons.extend(evaluation.numerator.reasons)
    return "engine_needs_review" + (f": {'; '.join(reasons)}" if reasons else "")


# --- nodes -------------------------------------------------------------------------------


def load_record(deps: "GraphDeps") -> NodeFn:
    def node(state: GapState) -> dict[str, Any]:
        started = _now()
        patient_id, as_of = state["patient_id"], state["as_of"]
        update: dict[str, Any]
        try:
            record = mask_as_of(deps.p6.get_record(patient_id, to=as_of), as_of)
        except PatientNotFound as exc:
            update = {"load_error": LoadError(kind="not_found", detail=type(exc).__name__)}
        except P6Unavailable as exc:
            update = {"load_error": LoadError(kind="unavailable", detail=type(exc).__name__)}
        except P6ContractError as exc:
            update = {"load_error": LoadError(kind="contract", detail=type(exc).__name__)}
        except P6Error as exc:  # an unclassified P6 failure is treated as unavailability
            update = {"load_error": LoadError(kind="unavailable", detail=type(exc).__name__)}
        else:
            features: FeatureRow | None
            try:
                features = deps.p6.get_features(patient_id, as_of=as_of)
            except P6Error:
                features = None
            ctx = MeasurementContext.for_(as_of, record.patient.birth_date)
            update = {
                "record": record,
                "features": features,
                "context": MeasurementSummary(
                    as_of=ctx.as_of,
                    my_start=ctx.my_start,
                    my_end=ctx.my_end,
                    age_at_my_end=ctx.age_at_my_end,
                ),
            }
        update["trace"] = [_trace("load_record", started)]
        return update

    return node


def evaluate_measures(deps: "GraphDeps") -> NodeFn:
    def node(state: GapState) -> dict[str, Any]:
        started = _now()
        evaluations = deps.engine.evaluate(
            state["record"], _context_of(state), state["options"].measures
        )
        # Engine-only view: clean open gaps ranked now so the direct draft path is complete;
        # ``validate_gaps`` recomputes both lists whenever anything needs holding.
        opened: list[tuple[MeasureEvaluation, GapSource]] = [
            (e, "engine") for e in evaluations if e.verdict == "gap_open" and not e.escalations
        ]
        return {
            "evaluations": evaluations,
            "open_gaps": _open_gaps_from(opened),
            "review_items": [],
            "trace": [_trace("evaluate_measures", started)],
        }

    return node


def validate_gaps(deps: "GraphDeps") -> NodeFn:
    def node(state: GapState) -> dict[str, Any]:
        started = _now()
        options = state["options"]
        evaluations = state.get("evaluations", [])
        record = state["record"]
        ctx = _context_of(state)
        patient_id, as_of = state["patient_id"], state["as_of"]
        model_id = deps.models.model_ids.get("validator_model", "unknown")

        # Global escalations hold everything; the engine stamps the same flag on every
        # eligible measure, so de-duplicate by (kind, reason) in first-seen order.
        global_items: list[ReviewItem] = []
        seen_global: set[tuple[str, str]] = set()
        for evaluation in evaluations:
            for flag in evaluation.global_escalations:
                key = (flag.kind, flag.reason)
                if key in seen_global:
                    continue
                seen_global.add(key)
                global_items.append(
                    ReviewItem(
                        measure_id=None,
                        scope="global",
                        reason=f"{flag.kind}: {flag.reason}",
                        evidence=_sorted_refs(flag.evidence),
                    )
                )
        hold_all = bool(global_items)

        review_items: list[ReviewItem] = list(global_items)
        verdicts: list[ValidationVerdict] = []
        agent_errors: list[AgentError] = []
        opened: list[tuple[MeasureEvaluation, GapSource]] = []
        case_keys: list[str] = []

        for evaluation in evaluations:
            if not evaluation.is_candidate:
                continue
            if evaluation.verdict == "gap_open" and not evaluation.escalations:
                if not hold_all:
                    opened.append((evaluation, "engine"))
                continue
            kinds = sorted({flag.kind for flag in evaluation.escalations})
            if not kinds:
                review_items.append(_measure_item(evaluation, _engine_review_reason(evaluation)))
                continue
            if hold_all or options.validation_mode == "off":
                review_items.append(_measure_item(evaluation, "escalated:" + ",".join(kinds)))
                continue
            case_keys.append(_validator_case_key(patient_id, evaluation.measure_id, as_of))
            try:
                packet = build_packet(
                    evaluation,
                    record,
                    ctx,
                    patient_id=patient_id,
                    rule_text=deps.rule_text_loader(evaluation.measure_id),
                    value_sets=deps.value_sets,
                )
                verdict = validate_candidate(
                    packet, model=deps.models.validator, caller=deps.structured, model_id=model_id
                )
                verdict = verify_verdict(verdict, packet)
                resolution = resolve_candidate(evaluation, verdict)
            except AgentOutputError as exc:
                agent_errors.append(
                    AgentError(
                        node="validate_gaps",
                        schema_name=exc.schema,
                        error_class=exc.error_class,
                        attempt=exc.attempts,
                    )
                )
                review_items.append(_measure_item(evaluation, "validator_failed"))
                continue
            except Exception as exc:
                agent_errors.append(
                    AgentError(
                        node="validate_gaps",
                        schema_name=ValidationVerdict.__name__,
                        error_class=type(exc).__name__,
                        attempt=1,
                    )
                )
                review_items.append(_measure_item(evaluation, "validator_failed"))
                continue
            verdicts.append(verdict)
            if resolution == "open":
                opened.append((evaluation, "validator_confirmed"))
            elif resolution == "needs_review":
                note = verdict.verification_note
                reason = "validator_needs_human" + (f": {note}" if note else "")
                review_items.append(_measure_item(evaluation, reason))
            # excluded / closed: nothing to draft; ``finalize`` reads the verdict.

        called = bool(case_keys)
        return {
            "open_gaps": _open_gaps_from(opened),
            "review_items": review_items,
            "verdicts": verdicts,
            "agent_errors": agent_errors,
            "trace": [
                _trace(
                    "validate_gaps",
                    started,
                    model_id=model_id if called else None,
                    prompt_version=VALIDATOR_PROMPT_SHA if called else None,
                    case_key=",".join(case_keys) if called else None,
                )
            ],
        }

    return node


def draft_actions(deps: "GraphDeps") -> NodeFn:
    def node(state: GapState) -> dict[str, Any]:
        started = _now()
        patient_id, as_of = state["patient_id"], state["as_of"]
        open_gaps = list(state.get("open_gaps", []))
        review_items = list(state.get("review_items", []))
        revision = state.get("revision_count", 0)
        model_id = deps.models.model_ids.get("drafter_model", "unknown")
        context = build_drafter_context(
            state["record"],
            _context_of(state),
            state.get("evaluations", []),
            patient_id=patient_id,
            clinic_name=deps.clinic_name,
            clinic_phone=deps.clinic_phone,
        )
        feedback = list(state.get("revision_feedback", []))
        agent_errors: list[AgentError] = []
        try:
            plan, draft_error = draft_plan(
                open_gaps,
                review_items,
                context,
                feedback,
                model=deps.models.drafter,
                caller=deps.structured,
                model_id=model_id,
                revision=revision,
                allowed_numbers=allowed_numbers_for(open_gaps, context),
            )
            if draft_error is not None:
                agent_errors.append(
                    AgentError(
                        node="draft_actions",
                        schema_name="CareActionPlan",
                        error_class="DrafterFallback",
                        attempt=2,
                    )
                )
        except Exception as exc:
            # ``draft_plan`` already falls back on AgentOutputError; anything else is an
            # unexpected failure and still fails closed onto the template.
            plan = TemplateDrafter().draft(open_gaps, review_items, context)
            draft_error = f"drafter_failed: {type(exc).__name__}"
            agent_errors.append(
                AgentError(
                    node="draft_actions",
                    schema_name="CareActionPlan",
                    error_class=type(exc).__name__,
                    attempt=1,
                )
            )
        return {
            "plan": plan,
            "draft_error": draft_error,
            "agent_errors": agent_errors,
            "trace": [
                _trace(
                    "draft_actions",
                    started,
                    model_id=model_id,
                    prompt_version=DRAFTER_PROMPT_SHA,
                    case_key=_drafter_case_key(patient_id, as_of, revision),
                )
            ],
        }

    return node


def await_approval(deps: "GraphDeps") -> NodeFn:
    def node(state: GapState) -> dict[str, Any]:
        started = _now()
        run_id, patient_id = state["run_id"], state["patient_id"]
        request = ApprovalRequest(
            run_id=run_id,
            patient_id=patient_id,
            as_of=state["as_of"],
            open_gaps=list(state.get("open_gaps", [])),
            review_items=list(state.get("review_items", [])),
            plan=state.get("plan"),
            draft_error=state.get("draft_error"),
            revision_count=state.get("revision_count", 0),
            prompt_versions={
                "validator": VALIDATOR_PROMPT_SHA,
                "drafter": DRAFTER_PROMPT_SHA,
                "version": PROMPT_VERSION,
            },
            model_ids=dict(deps.models.model_ids),
        )
        if state["options"].approval_mode == "auto":
            decision = ApprovalDecision(
                decision_id="auto:" + thread_id(run_id, patient_id),
                action="auto_reviewed",
                reviewer="auto",
            )
        else:
            decision = ApprovalDecision.model_validate(interrupt(request.model_dump(mode="json")))
        return {
            "decision": decision,
            "decisions": [decision],
            "trace": [_trace("await_approval", started)],
        }

    return node


def record_decision(deps: "GraphDeps") -> NodeFn:
    def node(state: GapState) -> dict[str, Any]:
        started = _now()
        decision = state["decision"]
        open_gaps = list(state.get("open_gaps", []))
        update: dict[str, Any] = {}
        # Items the reviewer settled with a non-open status leave the review list (a
        # ``None`` measure resolves every global item), so a redraft or ``finalize`` never
        # sees a hold that has already been answered.
        settled = {r.measure_id for r in decision.review_resolutions if r.status != "open"}
        if settled:
            update["review_items"] = [
                item
                for item in state.get("review_items", [])
                if not (item.measure_id in settled or (None in settled and item.scope == "global"))
            ]
        if decision.action == "edit" and decision.edited_plan is not None:
            update["plan"] = finalize_plan(decision.edited_plan, open_gaps)
        elif decision.action == "revise":
            update["revision_feedback"] = [decision.feedback] if decision.feedback else []
            update["revision_count"] = state.get("revision_count", 0) + 1
            # A resolution to ``open`` (only legal with ``revise``) re-opens a held measure for
            # the redraft: it leaves the review list and joins the open gaps as a reviewer gap.
            reopened = {r.measure_id for r in decision.review_resolutions if r.status == "open"}
            reopened.discard(None)
            if reopened:
                by_measure = {e.measure_id: e for e in state.get("evaluations", [])}
                already = {g.measure_id for g in open_gaps}
                opened: list[tuple[MeasureEvaluation, GapSource]] = [
                    (by_measure[g.measure_id], g.source)
                    for g in open_gaps
                    if g.measure_id in by_measure
                ]
                for measure_id in sorted(m for m in reopened if m is not None):
                    if measure_id in by_measure and measure_id not in already:
                        opened.append((by_measure[measure_id], "reviewer"))
                update["open_gaps"] = _open_gaps_from(opened)
                update["review_items"] = [
                    item
                    for item in update.get("review_items", state.get("review_items", []))
                    if item.measure_id is None or item.measure_id not in reopened
                ]
        update["trace"] = [_trace("record_decision", started)]
        return update

    return node


def finalize(deps: "GraphDeps") -> NodeFn:
    """The only outbox writer. Refuses to write for an attempt the runner has abandoned
    (``attempt_abandoned``): the ledger already says ``error`` for it, and its checkpoint
    ends with an ``error`` outcome so the two never disagree."""

    def node(state: GapState, config: RunnableConfig | None = None) -> dict[str, Any]:
        started = _now()
        options = state["options"]
        load_error = state.get("load_error")
        decision = state.get("decision")
        plan = state.get("plan")
        evaluations = state.get("evaluations", [])
        by_measure = {e.measure_id: e for e in evaluations}

        engine_verdicts: dict[str, str] = {e.measure_id: e.verdict for e in evaluations}
        final_statuses: dict[str, str] = dict(engine_verdicts)
        for verdict in state.get("verdicts", []):
            evaluation = by_measure.get(verdict.measure_id)
            if evaluation is not None:
                resolution = resolve_candidate(evaluation, verdict)
                final_statuses[verdict.measure_id] = RESOLUTION_TO_STATUS[resolution]
        # Fold EVERY decision on this thread in order (a resolution sent with a ``revise``
        # counts; the last word per measure wins), then align with the gap set the reviewer
        # re-opened so a redrafted measure never stays ``needs_review``.
        folded = list(state.get("decisions", []))
        if decision is not None and decision not in folded:
            folded.append(decision)
        for past in folded:
            for resolution_item in past.review_resolutions:
                # A global resolution answers the global review item only; it never
                # rewrites a measure's status.
                if resolution_item.measure_id is not None:
                    final_statuses[resolution_item.measure_id] = RESOLUTION_TO_STATUS[
                        resolution_item.status
                    ]
        for gap in state.get("open_gaps", []):
            if gap.source == "reviewer":
                final_statuses[gap.measure_id] = RESOLUTION_TO_STATUS["open"]
        global_hold = any(i.scope == "global" for i in state.get("review_items", []))

        status: RunStatus
        if load_error is not None:
            status = "error"
        elif decision is None:
            status = "no_action"
        elif decision.action in {"approve", "edit", "auto_reviewed"}:
            status = "completed"
        elif decision.action == "reject":
            status = "rejected"
        else:  # a ``revise`` that ran out of budget: nothing was approved
            status = "no_action"

        actionable = (
            decision is not None
            and decision.action in {"approve", "edit"}
            and options.approval_mode == "interrupt"
            and plan is not None
            and not global_hold
        )
        if actionable and attempt_abandoned(config):
            # The runner timed this attempt out and recorded ``error``: nothing may be sent
            # on its behalf.
            status = "error"
            actionable = False
        approved_actions: list[str] = []
        if actionable and decision is not None and plan is not None:
            run_id, patient_id = state["run_id"], state["patient_id"]
            entries = [
                OutboxEntry(
                    thread_id=thread_id(run_id, patient_id),
                    action_id=action.action_id,
                    run_id=run_id,
                    patient_id=patient_id,
                    measure_id=action.measure_id,
                    kind=action.kind,
                    detail=action.detail,
                    owner=action.owner,
                    approval_ref=decision.decision_id,
                )
                for action in plan.actions
            ]
            if entries:
                deps.outbox.append(entries)
            approved_actions = [action.action_id for action in plan.actions]

        outcome = RunOutcome(
            status=status,
            actionable=actionable,
            approved_actions=approved_actions,
            engine_verdicts=engine_verdicts,
            final_statuses=final_statuses,
            decision_action=decision.action if decision is not None else None,
            load_error=load_error,
        )
        return {"outcome": outcome, "trace": [_trace("finalize", started)]}

    return node
