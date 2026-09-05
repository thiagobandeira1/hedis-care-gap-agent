"""SPEC section 6 scoring over ``clinevals``: unit = (patient, core measure).

* ``predicted = {"gap"}`` iff the final status is ``gap_open`` or ``needs_review`` (the
  strict variant counts ``needs_review`` as no gap); gold ``open`` -> ``{"gap"}``; every other
  gold status -> ``set()`` (a no-gap probe is a load-bearing false-positive trap).
* gold ``escalate`` carries no counts: it is scored by ``review_flag_rate`` — did the
  pipeline hold the measure for a human?
* an error patient predicts nothing, so every gold-open unit becomes a false negative and
  every escalate unit an unflagged one.
* ``validator_unsafe_close`` (lower is better) counts gold-open units the validator excluded
  or closed; ``unresolved_evidence`` counts units whose verdict came back unverified.
* publication guard: per-measure test gold-open n >= 5 (BCS >= 3), else that measure's row
  is ``insufficient`` and the headline carries the caveat.

Headline P/R/F1 = ``EvalReport.test_overall`` (micro over summed test-split counts). Counts
and item order are deterministic (gold sorted by unit id) so artifacts are byte-stable.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from clinevals import ConfusionCounts, EvalReport, ItemScore, score_items, set_confusion

from caregap.evals.gold import GoldCase, gold_counts
from caregap.evals.outcomes import PatientOutcomeRow
from caregap.measures.ids import CORE_MEASURES, MeasureId

GAP = "gap"
PREDICTED_GAP_STATUSES: frozenset[str] = frozenset({"gap_open", "needs_review"})
STRICT_GAP_STATUSES: frozenset[str] = frozenset({"gap_open"})
UNSAFE_CLOSE_DECISIONS: frozenset[str] = frozenset({"exclude", "numerator_met"})
CLOSED_STATUSES: frozenset[str] = frozenset({"excluded", "closed"})
REVIEW_FLAG_METRIC = "review_flag_rate"

MIN_TEST_OPEN_DEFAULT = 5
MIN_TEST_OPEN: dict[str, int] = {"BCS": 3}
"""Per-measure floor on TEST-split gold-open units before a row may be published."""


class ScoringError(ValueError):
    """Outcomes do not cover the gold set (a harness bug, never a silent partial number)."""


def min_test_open(measure_id: str) -> int:
    return MIN_TEST_OPEN.get(measure_id, MIN_TEST_OPEN_DEFAULT)


def predicted_set(final_status: str | None, *, strict: bool = False) -> set[str]:
    statuses = STRICT_GAP_STATUSES if strict else PREDICTED_GAP_STATUSES
    return {GAP} if final_status in statuses else set()


def gold_set(case: GoldCase) -> set[str]:
    return {GAP} if case.gold == "open" else set()


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


@dataclass(frozen=True)
class MeasureRow:
    """One line of the README ``EVAL-MEASURES`` table (test split)."""

    measure: str
    n_test: int
    n_test_open: int
    tp: int
    fp: int
    fn: int
    precision: float | None
    recall: float | None
    f1: float | None
    status: str
    """``ok`` or ``insufficient`` (publication guard)."""

    def as_dict(self) -> dict[str, object]:
        return {
            "measure": self.measure,
            "n_test": self.n_test,
            "n_test_open": self.n_test_open,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "status": self.status,
            "min_test_open": min_test_open(self.measure),
        }


@dataclass(frozen=True)
class Scorecard:
    report: EvalReport
    """The publication report (``needs_review`` counts as a predicted gap)."""
    strict: EvalReport
    """The strict variant (``needs_review`` counts as no gap)."""
    measures: list[MeasureRow]
    validator_unsafe_close: int
    unresolved_evidence: int
    gold_counts: dict[str, dict[str, int]]
    error_patients: list[str]

    @property
    def headline(self) -> dict[str, float]:
        return self.report.test_overall

    @property
    def strict_headline(self) -> dict[str, float]:
        return self.strict.test_overall

    @property
    def insufficient_measures(self) -> list[str]:
        return [row.measure for row in self.measures if row.status == "insufficient"]

    @property
    def publication_note(self) -> str:
        if not self.insufficient_measures:
            return "publication guard satisfied for every core measure"
        return (
            "insufficient test gold-open units for "
            + ", ".join(self.insufficient_measures)
            + " (guard: n >= 5 per measure, BCS >= 3); the headline is provisional"
        )

    def extras(self) -> dict[str, object]:
        """Everything beyond ``metrics.overall`` the artifact carries (JSON-ready)."""
        return {
            "validator_unsafe_close": self.validator_unsafe_close,
            "unresolved_evidence": self.unresolved_evidence,
            "measures": [row.as_dict() for row in self.measures],
            "publication": {
                "insufficient_measures": self.insufficient_measures,
                "note": self.publication_note,
                "min_test_open": {
                    row.measure: min_test_open(row.measure)
                    for row in sorted(self.measures, key=lambda r: r.measure)
                },
            },
            "gold_counts": self.gold_counts,
            "error_patients": self.error_patients,
        }


def _scored_cases(gold: Sequence[GoldCase], measures: Sequence[MeasureId]) -> list[GoldCase]:
    wanted = set(measures)
    return sorted(
        (case for case in gold if case.measure_id in wanted),
        key=lambda c: (c.patient_id, c.measure_id),
    )


def _rows_by_patient(outcomes: Sequence[PatientOutcomeRow]) -> dict[str, PatientOutcomeRow]:
    rows: dict[str, PatientOutcomeRow] = {}
    for row in outcomes:
        if row.patient_id in rows:
            raise ScoringError(f"duplicate outcome row for patient {row.patient_id!r}")
        rows[row.patient_id] = row
    return rows


def _score_fn(
    rows: dict[str, PatientOutcomeRow], *, strict: bool
) -> Callable[[GoldCase], ItemScore | None]:
    def score_case(case: GoldCase) -> ItemScore | None:
        row = rows[case.patient_id]
        if case.gold == "escalate":
            flagged = (not row.error) and case.measure_id in row.review_flagged
            return ItemScore(metrics={REVIEW_FLAG_METRIC: 1.0 if flagged else 0.0})
        predicted: set[str] = set()
        if not row.error:
            predicted = predicted_set(row.final_statuses.get(case.measure_id), strict=strict)
        return ItemScore(metrics={}, counts=set_confusion(predicted, gold_set(case)))

    return score_case


def _measure_rows(
    cases: Sequence[GoldCase], report: EvalReport, measures: Sequence[MeasureId]
) -> list[MeasureRow]:
    counts_by_item = {item.item_id: item.counts for item in report.per_item}
    rows: list[MeasureRow] = []
    for measure in measures:
        test_cases = [c for c in cases if c.measure_id == measure and c.split == "test"]
        total = ConfusionCounts(tp=0, fp=0, fn=0)
        for case in test_cases:
            counts = counts_by_item.get(case.item_id)
            if counts is not None:
                total = total + counts
        n_open = sum(1 for c in test_cases if c.gold == "open")
        precision, recall = total.precision, total.recall
        rows.append(
            MeasureRow(
                measure=measure,
                n_test=len(test_cases),
                n_test_open=n_open,
                tp=total.tp,
                fp=total.fp,
                fn=total.fn,
                precision=precision,
                recall=recall,
                f1=_f1(precision, recall),
                status="ok" if n_open >= min_test_open(measure) else "insufficient",
            )
        )
    return rows


def score(
    gold: Sequence[GoldCase],
    outcomes: Sequence[PatientOutcomeRow],
    *,
    measures: Sequence[MeasureId] = CORE_MEASURES,
) -> Scorecard:
    """Score every gold unit of ``measures`` against the outcome rows (SPEC section 6)."""
    cases = _scored_cases(gold, measures)
    rows = _rows_by_patient(outcomes)
    missing = sorted({c.patient_id for c in cases} - set(rows))
    if missing:
        raise ScoringError(
            "no outcome row for gold patient(s): " + ", ".join(missing) + " (run the tier first)"
        )

    report = score_items(cases, _score_fn(rows, strict=False))
    strict = score_items(cases, _score_fn(rows, strict=True))

    unsafe = 0
    unresolved = 0
    for case in cases:
        if case.split != "test":
            continue
        row = rows[case.patient_id]
        decision = row.validator_decisions.get(case.measure_id)
        final = row.final_statuses.get(case.measure_id)
        if case.gold == "open" and decision in UNSAFE_CLOSE_DECISIONS and final in CLOSED_STATUSES:
            unsafe += 1
        if case.measure_id in row.unverified:
            unresolved += 1

    return Scorecard(
        report=report,
        strict=strict,
        measures=_measure_rows(cases, report, measures),
        validator_unsafe_close=unsafe,
        unresolved_evidence=unresolved,
        gold_counts=gold_counts(cases),
        error_patients=sorted(pid for pid, row in rows.items() if row.error),
    )
