"""SPEC section 6 scoring semantics: the predicted set, escalate units scored by the review
flag, error patients as misses, the validator's unsafe closes, unresolved evidence, and the
per-measure publication guard."""

import pytest

from caregap.evals.gold import GoldCase
from caregap.evals.scoring import (
    ScoringError,
    gold_set,
    min_test_open,
    predicted_set,
    score,
)
from caregap.measures.ids import MeasureId
from tests.unit.evals.helpers import gold, outcome, synthetic_patient

T0, T1, T2 = (synthetic_patient("test", i) for i in range(3))
D0 = synthetic_patient("dev", 0)


def test_predicted_and_gold_sets() -> None:
    assert predicted_set("gap_open") == {"gap"}
    assert predicted_set("needs_review") == {"gap"}
    assert predicted_set("needs_review", strict=True) == set()
    assert predicted_set("closed") == set()
    assert predicted_set(None) == set()
    assert gold_set(gold(T0, "CBP", "open")) == {"gap"}
    for status in ("closed", "excluded", "not_eligible", "escalate"):
        assert gold_set(gold(T0, "CBP", status)) == set()


def test_headline_counts_over_the_test_split_only() -> None:
    cases = [
        gold(T0, "CBP", "open"),  # TP
        gold(T0, "EED", "closed"),  # FP (engine says needs_review)
        gold(T1, "COL", "open"),  # FN
        gold(T1, "BCS", "not_eligible"),  # TN
        gold(D0, "CBP", "open"),  # dev: never in the headline
    ]
    rows = [
        outcome(T0, final={"CBP": "gap_open", "EED": "needs_review"}),
        outcome(T1, final={"COL": "closed", "BCS": "not_eligible"}),
        outcome(D0, final={"CBP": "closed"}),
    ]
    card = score(cases, rows)
    assert card.headline["micro_precision"] == 0.5
    assert card.headline["micro_recall"] == 0.5
    assert card.headline["micro_f1"] == 0.5
    # Strict: needs_review is not a predicted gap -> the FP disappears.
    assert card.strict_headline["micro_precision"] == 1.0
    assert card.strict_headline["micro_recall"] == 0.5
    assert card.error_patients == []
    assert card.gold_counts["test"]["open"] == 2
    assert card.gold_counts["dev"]["open"] == 1


def test_escalate_units_are_scored_by_the_review_flag() -> None:
    cases = [gold(T0, "SPC", "escalate"), gold(T1, "SPC", "escalate")]
    rows = [outcome(T0, final={"SPC": "needs_review"}, flagged=["SPC"]), outcome(T1, final={})]
    card = score(cases, rows)
    assert card.headline == {"review_flag_rate": 0.5}
    assert [item.counts for item in card.report.per_item] == [None, None]
    assert all(row.tp == row.fp == row.fn == 0 for row in card.measures)


def test_error_patients_miss_every_gold_open_unit() -> None:
    cases = [gold(T0, "CBP", "open"), gold(T0, "EED", "escalate"), gold(T1, "CBP", "open")]
    rows = [outcome(T0, error=True, flagged=["EED"]), outcome(T1, final={"CBP": "gap_open"})]
    card = score(cases, rows)
    assert card.error_patients == [T0]
    assert card.headline["micro_recall"] == 0.5
    assert card.headline["micro_precision"] == 1.0
    assert card.headline["review_flag_rate"] == 0.0, "a flag on an error patient never counts"


def test_validator_unsafe_close_and_unresolved_evidence() -> None:
    cases = [
        gold(T0, "CBP", "open"),
        gold(T0, "EED", "open"),
        gold(T1, "CBP", "open"),
        gold(D0, "CBP", "open"),
    ]
    rows = [
        outcome(
            T0,
            final={"CBP": "excluded", "EED": "closed"},
            engine={"CBP": "needs_review", "EED": "needs_review"},
            decisions={"CBP": "exclude", "EED": "numerator_met"},
            unverified=["EED"],
        ),
        outcome(T1, final={"CBP": "gap_open"}, decisions={"CBP": "confirm_open"}),
        # dev split: excluded by the validator too, but never counted in the published number
        outcome(D0, final={"CBP": "excluded"}, decisions={"CBP": "exclude"}),
    ]
    card = score(cases, rows)
    assert card.validator_unsafe_close == 2
    assert card.unresolved_evidence == 1
    extras = card.extras()
    assert extras["validator_unsafe_close"] == 2
    assert extras["unresolved_evidence"] == 1


def test_publication_guard_per_measure() -> None:
    assert min_test_open("BCS") == 3
    assert min_test_open("CBP") == 5
    bcs_patients = [synthetic_patient("test", i) for i in range(3)]
    cbp_patients = [synthetic_patient("test", i) for i in range(4)]
    cases: list[GoldCase] = [gold(p, "BCS", "open") for p in bcs_patients]
    cases += [gold(p, "CBP", "open") for p in cbp_patients]
    rows = [
        outcome(p, final={"BCS": "gap_open", "CBP": "gap_open"})
        for p in sorted(set(bcs_patients) | set(cbp_patients))
    ]
    card = score(cases, rows)
    by_measure = {row.measure: row for row in card.measures}
    assert by_measure["BCS"].status == "ok"
    assert (by_measure["BCS"].n_test, by_measure["BCS"].n_test_open) == (3, 3)
    assert by_measure["CBP"].status == "insufficient"
    assert (by_measure["CBP"].tp, by_measure["CBP"].precision, by_measure["CBP"].recall) == (
        4,
        1.0,
        1.0,
    )
    assert by_measure["EED"].as_dict()["precision"] is None
    assert card.insufficient_measures == ["CBP", "EED", "COL", "SPC", "SPD"]
    assert "provisional" in card.publication_note
    assert "CBP" in card.publication_note and "BCS" not in card.insufficient_measures
    publication = card.extras()["publication"]
    assert isinstance(publication, dict)
    assert publication["min_test_open"] == {
        "BCS": 3,
        "CBP": 5,
        "COL": 5,
        "EED": 5,
        "SPC": 5,
        "SPD": 5,
    }


def test_publication_note_when_every_measure_is_covered() -> None:
    measures: tuple[MeasureId, ...] = ("BCS",)
    patients = [synthetic_patient("test", i) for i in range(3)]
    card = score(
        [gold(p, "BCS", "open") for p in patients],
        [outcome(p, final={"BCS": "gap_open"}) for p in patients],
        measures=measures,
    )
    assert card.insufficient_measures == []
    assert card.publication_note == "publication guard satisfied for every core measure"


def test_scoring_refuses_partial_or_duplicate_outcomes() -> None:
    cases = [gold(T0, "CBP", "open"), gold(T1, "CBP", "open")]
    with pytest.raises(ScoringError, match=T1):
        score(cases, [outcome(T0, final={"CBP": "gap_open"})])
    with pytest.raises(ScoringError, match="duplicate"):
        score(cases, [outcome(T0), outcome(T0), outcome(T1)])
