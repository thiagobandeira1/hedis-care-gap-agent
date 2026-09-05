"""One graph run per gold patient -> one :class:`PatientOutcomeRow` (SPEC section 6 tiers).

``run_tier`` drives the REAL compiled graph through :class:`PatientRunner` with
``approval_mode="auto"`` (the request is emitted and auto-reviewed; nothing is actionable and
the outbox is never written) and ``validation_mode="off"`` for the engine tier (escalated
candidates become review items without a model call) or ``"escalated"`` for the pipeline and
outreach tiers. Rows carry ids, statuses, and decisions only — never record content — and
serialise deterministically, which is what makes ``evals/gold/engine-outcomes.jsonl`` a
byte-comparable fixture.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from caregap.agents.schemas import ValidationVerdict
from caregap.evals.gold import GoldCase
from caregap.graph.build import GraphDeps
from caregap.graph.runner import PatientRunner
from caregap.graph.state import ApprovalRequest, RunOptions, RunOutcome, RunStatus, thread_id
from caregap.measures.models import ReviewItem

Tier = Literal["engine", "pipeline", "outreach"]
TIERS: tuple[Tier, ...] = ("engine", "pipeline", "outreach")

#: Engine-vocabulary statuses that mean "this measure is still a candidate for outreach".
CANDIDATE_STATUSES: frozenset[str] = frozenset({"gap_open", "needs_review"})


class OutcomeError(RuntimeError):
    """A tier run violated the eval contract (e.g. an interrupt under ``approval_mode=auto``)."""


class RuntimeLike(Protocol):
    """The slice of :class:`caregap.runtime.Runtime` the tiers read (structural)."""

    @property
    def deps(self) -> GraphDeps: ...

    @property
    def graph(self) -> Any: ...

    @property
    def runner(self) -> PatientRunner: ...

    def close(self) -> None: ...


class PatientOutcomeRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    patient_id: str
    as_of: date
    tier: Tier
    status: RunStatus
    engine_verdicts: dict[str, str] = Field(default_factory=dict)
    final_statuses: dict[str, str] = Field(default_factory=dict)
    validator_decisions: dict[str, str] = Field(default_factory=dict)
    review_flagged: list[str] = Field(default_factory=list)
    error: bool
    fallback_count: int = 0
    unverified: list[str] = Field(default_factory=list)
    """Measures whose validator verdict came back ``verified=False`` (unresolved evidence)."""


def tier_options(tier: Tier) -> RunOptions:
    return RunOptions(
        approval_mode="auto", validation_mode="off" if tier == "engine" else "escalated"
    )


def gold_patients(gold: Sequence[GoldCase]) -> dict[str, date]:
    """``{patient_id: as_of}`` in patient order (the loader guarantees one as_of each)."""
    patients: dict[str, date] = {}
    for case in gold:
        patients.setdefault(case.patient_id, case.as_of)
    return dict(sorted(patients.items()))


def _as_model[T: BaseModel](model: type[T], value: object) -> T | None:
    if isinstance(value, model):
        return value
    if isinstance(value, Mapping):
        return model.model_validate(value)
    return None


def _verdicts(state: Mapping[str, Any]) -> list[ValidationVerdict]:
    raw = state.get("verdicts") or []
    return [v for v in (_as_model(ValidationVerdict, item) for item in raw) if v is not None]


def _review_items(state: Mapping[str, Any]) -> list[ReviewItem]:
    raw = state.get("review_items") or []
    return [r for r in (_as_model(ReviewItem, item) for item in raw) if r is not None]


def review_flagged(outcome: RunOutcome, review_items: Sequence[ReviewItem]) -> list[str]:
    """Measures held for a human: a measure-scoped review item, a final ``needs_review``
    status, or — under a global hold — every candidate measure (the hold covers them all)."""
    flagged: set[str] = {item.measure_id for item in review_items if item.measure_id is not None}
    flagged.update(m for m, s in outcome.final_statuses.items() if s == "needs_review")
    if any(item.scope == "global" for item in review_items):
        flagged.update(m for m, s in outcome.engine_verdicts.items() if s in CANDIDATE_STATUSES)
    return sorted(flagged)


def outcome_row(
    patient_id: str,
    as_of: date,
    tier: Tier,
    outcome: RunOutcome,
    state: Mapping[str, Any],
    *,
    fallback_count: int = 0,
) -> PatientOutcomeRow:
    verdicts = _verdicts(state)
    return PatientOutcomeRow(
        patient_id=patient_id,
        as_of=as_of,
        tier=tier,
        status=outcome.status,
        engine_verdicts=dict(sorted(outcome.engine_verdicts.items())),
        final_statuses=dict(sorted(outcome.final_statuses.items())),
        validator_decisions={v.measure_id: v.decision for v in verdicts},
        review_flagged=review_flagged(outcome, _review_items(state)),
        error=outcome.status == "error",
        fallback_count=fallback_count,
        unverified=sorted({v.measure_id for v in verdicts if not v.verified}),
    )


def state_values(graph: Any, tid: str) -> Mapping[str, Any]:
    """The checkpointed state of one thread (``{}`` when the thread never ran)."""
    snapshot = graph.get_state({"configurable": {"thread_id": tid}})
    values = getattr(snapshot, "values", None)
    return values if isinstance(values, Mapping) else {}


def run_tier(
    tier: Tier, gold: Sequence[GoldCase], *, runtime: RuntimeLike, run_id: str
) -> list[PatientOutcomeRow]:
    """One ``PatientRunner.start`` per distinct gold patient, in patient order."""
    options = tier_options(tier)
    rows: list[PatientOutcomeRow] = []
    for patient_id, as_of in gold_patients(gold).items():
        before = runtime.deps.models.fallback_count()
        result = runtime.runner.start(run_id, patient_id, as_of, options)
        used = runtime.deps.models.fallback_count() - before
        if isinstance(result, ApprovalRequest):
            raise OutcomeError(
                f"patient {patient_id!r} interrupted for approval under approval_mode=auto"
            )
        state = state_values(runtime.graph, thread_id(run_id, patient_id))
        rows.append(outcome_row(patient_id, as_of, tier, result, state, fallback_count=used))
    return rows


# --- serialisation ----------------------------------------------------------------------------


def render_outcomes(rows: Sequence[PatientOutcomeRow]) -> str:
    """JSONL sorted by patient_id, sorted keys, LF line endings, trailing newline."""
    ordered = sorted(rows, key=lambda r: r.patient_id)
    lines = [
        json.dumps(
            row.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        for row in ordered
    ]
    return "".join(line + "\n" for line in lines)


def write_outcomes(rows: Sequence[PatientOutcomeRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_outcomes(rows))


def read_outcomes(path: Path) -> list[PatientOutcomeRow]:
    if not path.exists():
        raise FileNotFoundError(f"outcomes file not found at {path}")
    rows: list[PatientOutcomeRow] = []
    for line in path.read_text(encoding="utf-8-sig").split("\n"):
        if line.strip():
            rows.append(PatientOutcomeRow.model_validate_json(line))
    return rows


def diff_summary(expected: Sequence[PatientOutcomeRow], actual: Sequence[PatientOutcomeRow]) -> str:
    """Which patients differ (ids and changed field names only — never content)."""
    by_expected = {r.patient_id: r for r in expected}
    by_actual = {r.patient_id: r for r in actual}
    lines: list[str] = []
    for patient_id in sorted(set(by_expected) | set(by_actual)):
        left, right = by_expected.get(patient_id), by_actual.get(patient_id)
        if left is None:
            lines.append(f"  + {patient_id}: only in the fresh run")
        elif right is None:
            lines.append(f"  - {patient_id}: only in the committed file")
        elif left != right:
            changed = sorted(
                name
                for name in PatientOutcomeRow.model_fields
                if getattr(left, name) != getattr(right, name)
            )
            lines.append(f"  ~ {patient_id}: {', '.join(changed)}")
    return "\n".join(lines) if lines else "  (rows are equal; whitespace or ordering differs)"
