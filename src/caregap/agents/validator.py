"""Validator agent: one structured call per escalated candidate, then code verification.

Monotonic by construction (SPEC section 1, ADR-0001): the model may only ``confirm_open``,
``exclude``, ``numerator_met`` or ``needs_human``, and every decision that changes what the
engine would do is verified by :func:`verify_verdict` against the packet the model saw:

* every cited ``evidence_id`` must resolve in ``packet.evidence``;
* ``exclude`` needs a category listed in the packet AND a cited event tagged with that
  category's value set dated inside its window;
* ``numerator_met`` needs a cited event tagged with a numerator value set inside the numerator
  window — and the engine's own numerator must be ``yes``: the engine already evaluated every
  in-set, in-window event, so on a numerator ``no`` / ``unknown`` such a citation can only be a
  re-reading of evidence the engine rejected (uncontrolled panel, low-intensity statin, a
  status without a coded answer);
* ``confirm_open`` needs the engine's denominator ``yes`` and numerator ``no`` (a gap the
  engine itself computed; the validator confirms the escalation does not overturn it);
* confidence ``low`` routes to a human; a ``closed`` / ``excluded`` / ``not_eligible``
  candidate admits only ``needs_human``; a measure-id mismatch is never trusted.

Anything unverifiable becomes ``needs_human`` with ``verified=False`` and a note — the
validator can never add a gap nor close one without code-checked evidence. Model failures
(``AgentOutputError``) propagate; the graph turns them into ``validator_failed`` review items.
"""

from datetime import date
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel

from caregap.agents.packet import EvidencePacket, PacketEvidence, render_packet, validator_case_key
from caregap.agents.prompts import VALIDATOR_PROMPT_SHA, VALIDATOR_SYSTEM, request_header
from caregap.agents.schemas import ValidationVerdict
from caregap.measures.models import MeasureEvaluation
from caregap.structured import StructuredCaller

Resolution = Literal["open", "excluded", "closed", "needs_review"]

#: Engine verdicts on which only ``needs_human`` is acceptable (nothing to confirm or close).
FINAL_ENGINE_VERDICTS: frozenset[str] = frozenset({"closed", "excluded", "not_eligible"})


def validate_candidate(
    packet: EvidencePacket,
    *,
    model: BaseChatModel,
    caller: StructuredCaller,
    model_id: str,
) -> ValidationVerdict:
    """One structured call. The request header is the first two lines of the human message
    so replay / recording models can key on the case."""
    case_key = validator_case_key(packet.patient_id, packet.measure_id, packet.as_of)
    user = request_header(case_key, VALIDATOR_PROMPT_SHA) + render_packet(packet)
    return caller.call(
        model,
        ValidationVerdict,
        system=VALIDATOR_SYSTEM,
        user=user,
        case_key=case_key,
        model_id=model_id,
    )


def _in_window(row: PacketEvidence, start: date, end: date) -> bool:
    return row.event_date is not None and start <= row.event_date <= end


def _window(start: date, end: date) -> str:
    left = "any time" if start == date.min else start.isoformat()
    return f"{left}..{end.isoformat()}"


def verify_verdict(verdict: ValidationVerdict, packet: EvidencePacket) -> ValidationVerdict:
    """Pure code. Returns a copy with ``verified`` and ``verification_note`` set; any
    unverifiable decision becomes ``needs_human`` with ``verified=False``.

    The packet's ``measure_id`` is authoritative: the copy always carries the measure the
    packet was built for, so a model (or replay sentinel) naming another measure can only
    ever affect this candidate — the mismatch itself is recorded in ``verification_note``."""
    checked = _verify(verdict, packet)
    return checked.model_copy(update={"measure_id": packet.measure_id})


def _verify(verdict: ValidationVerdict, packet: EvidencePacket) -> ValidationVerdict:
    by_id = {row.event_id: row for row in packet.evidence}
    problems: list[str] = []
    notes: list[str] = []

    # V8: model-authored strings (ids, categories, citations) never enter the note verbatim —
    # the note reaches the drafter packet and the provider note. The raw fields stay on the
    # stored verdict; only the count / the packet's own vocabulary is echoed.
    unresolved = [event_id for event_id in verdict.evidence_ids if event_id not in by_id]
    if unresolved:
        problems.append(f"{len(unresolved)} unresolved evidence id(s)")
    if verdict.measure_id != packet.measure_id:
        problems.append(
            f"measure_id {verdict.measure_id} does not match packet {packet.measure_id}"
        )

    if verdict.decision == "needs_human":
        # Nothing to verify; keep what the model said (unresolved ids are noted, not fatal).
        note = "; ".join(["needs_human as issued", *problems]) or "needs_human as issued"
        return verdict.model_copy(update={"verified": True, "verification_note": note})

    if verdict.confidence == "low":
        problems.append("confidence low")
    if packet.engine_verdict in FINAL_ENGINE_VERDICTS:
        problems.append(f"engine verdict {packet.engine_verdict} admits only needs_human")

    cited = [by_id[event_id] for event_id in verdict.evidence_ids if event_id in by_id]

    if verdict.decision == "confirm_open":
        if packet.denominator.value != "yes":
            problems.append(
                f"confirm_open requires the engine denominator to be yes "
                f"(it is {packet.denominator.value})"
            )
        if packet.numerator.value != "no":
            problems.append(
                f"confirm_open requires the engine numerator to be no "
                f"(it is {packet.numerator.value})"
            )
    elif verdict.decision == "exclude":
        spec = next(
            (c for c in packet.categories if c.category == verdict.exclusion_category), None
        )
        if spec is None:
            problems.append("exclusion_category is not a packet category")
        else:
            hits = [
                row
                for row in cited
                if spec.value_set_id in row.tags
                and (not spec.sections or row.section in spec.sections)
                and _in_window(row, spec.window_start, spec.window_end)
            ]
            if not hits:
                sections = ", ".join(sorted(spec.sections)) or "any section"
                problems.append(
                    f"no cited event tagged {spec.value_set_id} in {sections} dated inside "
                    f"{_window(spec.window_start, spec.window_end)} for {spec.category}"
                )
    elif verdict.decision == "numerator_met":
        if packet.numerator.value != "yes":
            problems.append(
                f"numerator_met requires the engine numerator to be yes "
                f"(it is {packet.numerator.value}): every in-set, in-window event was "
                "already evaluated"
            )
        numerator_sets = set(packet.numerator_value_set_ids)
        hits = [
            row
            for row in cited
            if numerator_sets.intersection(row.tags)
            and _in_window(row, packet.numerator_window_start, packet.numerator_window_end)
        ]
        if not hits:
            problems.append(
                "no cited event tagged with a numerator value set dated inside "
                f"{_window(packet.numerator_window_start, packet.numerator_window_end)}"
            )

    citation = verdict.rule_citation.strip()
    prefix = packet.measure_id.lower()
    if not citation.lower().startswith(prefix):
        notes.append(f"rule_citation does not start with {prefix!r}")
    elif citation not in {element.id for element in packet.rule_elements}:
        notes.append("rule_citation is not a listed rule element id")

    if problems:
        return verdict.model_copy(
            update={
                "decision": "needs_human",
                "verified": False,
                "verification_note": "; ".join([*problems, *notes]),
            }
        )
    return verdict.model_copy(
        update={"verified": True, "verification_note": "; ".join(["verified", *notes])}
    )


def resolve_candidate(evaluation: MeasureEvaluation, verdict: ValidationVerdict) -> Resolution:
    """Final status of a candidate after validation. ``confirm_open`` keeps the gap open;
    only VERIFIED ``exclude`` / ``numerator_met`` change it; everything else is reviewed."""
    if verdict.measure_id != evaluation.measure_id or not evaluation.is_candidate:
        return "needs_review"
    if verdict.decision == "confirm_open":
        return "open"
    if verdict.decision == "exclude" and verdict.verified:
        return "excluded"
    if verdict.decision == "numerator_met" and verdict.verified:
        return "closed"
    return "needs_review"
