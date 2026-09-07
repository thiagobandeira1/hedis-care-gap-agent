"""The MeasureEngine: composes per-measure rules with the shared global rules, the verdict
algebra, and deterministic priority. Pure over (masked record, context, measures).

Rule contract (``MeasureRule``): each rule returns denominator / numerator TriResults, coded
exclusion hits, a coverage table, and measure-scoped escalation flags. The engine adds the
global rules (death, hospice-in-MY exclusions; E1/E4 global escalations), resolves the verdict
via :func:`caregap.measures.tri.resolve`, and scores priority. Rules never see features.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from caregap.measures.context import MeasurementContext
from caregap.measures.ids import MeasureId
from caregap.measures.models import (
    Coverage,
    EscalationFlag,
    ExclusionHit,
    MeasureEvaluation,
    TriResult,
)
from caregap.measures.priority import priority_score
from caregap.measures.rules.global_rules import global_escalations, global_exclusions
from caregap.measures.tri import Tri, resolve
from caregap.measures.value_sets import ValueSets, load_value_sets
from caregap.p6.models import PatientRecord


@dataclass(frozen=True)
class RuleOutput:
    denominator: TriResult
    numerator: TriResult
    exclusions: list[ExclusionHit]
    coverage: dict[str, Coverage]
    escalations: list[EscalationFlag]


class MeasureRule(Protocol):
    measure_id: MeasureId
    rule_version: str

    def evaluate(
        self, record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
    ) -> RuleOutput: ...


def _exclusion_tri(hits: Sequence[ExclusionHit]) -> Tri:
    return "yes" if hits else "no"


class MeasureEngine:
    def __init__(self, rules: Sequence[MeasureRule], value_sets: ValueSets | None = None) -> None:
        self._rules: dict[MeasureId, MeasureRule] = {r.measure_id: r for r in rules}
        self._value_sets = value_sets or load_value_sets()

    @property
    def measure_ids(self) -> tuple[MeasureId, ...]:
        return tuple(self._rules)

    def evaluate_one(
        self, record: PatientRecord, ctx: MeasurementContext, measure_id: MeasureId
    ) -> MeasureEvaluation:
        rule = self._rules[measure_id]
        out = rule.evaluate(record, ctx, self._value_sets)
        # Global rules apply to every measure (death / hospice in MY; E1 / E4 hints).
        global_hits = global_exclusions(record, ctx, self._value_sets)
        exclusions = [*global_hits, *out.exclusions]
        escalations = [*global_escalations(record, ctx, self._value_sets), *out.escalations]
        denominator = out.denominator
        if ctx.age_at_my_end is None and denominator.value != "no":
            denominator = TriResult(
                value="unknown",
                reasons=[*denominator.reasons, "birth_date unknown"],
                evidence=denominator.evidence,
            )
        verdict = resolve(
            denominator.value,
            _exclusion_tri(exclusions) if denominator.value == "yes" else "no",
            out.numerator.value,
            escalated=bool(escalations),
        )
        kept_exclusions = exclusions if denominator.value == "yes" else []
        if denominator.value == "unknown" and global_hits:
            # A global death / hospice exclusion is denominator-independent: a member who
            # died or entered hospice in the MY can never receive outreach, so ``excluded``
            # wins over the unknown-denominator review (SPEC section 2, global rules).
            verdict = "excluded"
            kept_exclusions = global_hits
        return MeasureEvaluation(
            measure_id=measure_id,
            rule_version=rule.rule_version,
            denominator=denominator,
            numerator=out.numerator,
            exclusions=kept_exclusions,
            coverage=out.coverage,
            escalations=escalations if denominator.value != "no" else [],
            verdict=verdict,
            priority_score=priority_score(measure_id, out.numerator.subtype, ctx)
            if verdict in {"gap_open", "needs_review"}
            else 0.0,
        )

    def evaluate(
        self,
        record: PatientRecord,
        ctx: MeasurementContext,
        measures: Sequence[MeasureId] | None = None,
    ) -> list[MeasureEvaluation]:
        ids = list(measures) if measures is not None else list(self._rules)
        return [self.evaluate_one(record, ctx, m) for m in ids]


def default_engine() -> MeasureEngine:
    """All shipped rules, in registry order."""
    from caregap.measures.rules import all_rules

    return MeasureEngine(all_rules())
