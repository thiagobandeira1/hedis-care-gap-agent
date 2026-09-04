"""Priority = star_weight x clinical_weight x time_pressure (SPEC section 2), rank ties by id."""

from datetime import date

import pytest

from caregap.measures.context import MeasurementContext
from caregap.measures.ids import ALL_MEASURES, MeasureId
from caregap.measures.priority import (
    CLINICAL_WEIGHT,
    STAR_WEIGHT,
    SUBTYPE_BONUS,
    priority_score,
    rank,
    time_pressure,
)
from tests.factories import BIRTH_1960, DEMO_AS_OF, EVAL_AS_OF

JAN_1_2026 = date(2026, 1, 1)


def ctx_at(as_of: date) -> MeasurementContext:
    return MeasurementContext.for_(as_of, BIRTH_1960)


@pytest.mark.parametrize(
    ("measure_id", "subtype", "as_of", "expected"),
    [
        # CBP no_bp_in_my: 3 x (3 + 1) x time_pressure.
        ("CBP", "no_bp_in_my", DEMO_AS_OF, 17.9507),  # 12 x (1 + (1 - 184/365))
        ("CBP", "no_bp_in_my", EVAL_AS_OF, 12.0),  # retrospective -> time_pressure 1
        ("CBP", "no_bp_in_my", JAN_1_2026, 12.0329),  # 12 x (1 + (1 - 364/365))
        # CBP without the subtype bonus: 3 x 3.
        ("CBP", None, EVAL_AS_OF, 9.0),
        ("CBP", "uncontrolled", EVAL_AS_OF, 9.0),
        ("CBP", None, DEMO_AS_OF, 13.463),
        # SPC: star 1, clinical 3 (+1 for low_intensity_only).
        ("SPC", "low_intensity_only", EVAL_AS_OF, 4.0),
        ("SPC", "low_intensity_only", DEMO_AS_OF, 5.9836),
        ("SPC", None, EVAL_AS_OF, 3.0),
        ("SPC", None, DEMO_AS_OF, 4.4877),
        # SPD / EED / COL / BCS: 1 x 2.
        ("SPD", None, EVAL_AS_OF, 2.0),
        ("EED", None, EVAL_AS_OF, 2.0),
        ("COL", None, EVAL_AS_OF, 2.0),
        ("BCS", None, EVAL_AS_OF, 2.0),
        ("EED", None, DEMO_AS_OF, 2.9918),
        # TSC / SNS: 1 x 1.
        ("TSC", None, EVAL_AS_OF, 1.0),
        ("SNS", None, EVAL_AS_OF, 1.0),
        ("TSC", None, DEMO_AS_OF, 1.4959),
    ],
)
def test_priority_score_table(
    measure_id: MeasureId, subtype: str | None, as_of: date, expected: float
) -> None:
    assert priority_score(measure_id, subtype, ctx_at(as_of)) == expected


def test_subtype_bonus_applies_only_to_the_two_named_subtypes() -> None:
    assert SUBTYPE_BONUS == {("CBP", "no_bp_in_my"): 1.0, ("SPC", "low_intensity_only"): 1.0}
    # The bonus is keyed by (measure, subtype): the same subtype on another measure is inert.
    assert priority_score("SPD", "low_intensity_only", ctx_at(EVAL_AS_OF)) == 2.0
    assert priority_score("EED", "no_bp_in_my", ctx_at(EVAL_AS_OF)) == 2.0


def test_time_pressure_is_one_when_retrospective_and_grows_toward_my_end() -> None:
    assert time_pressure(ctx_at(EVAL_AS_OF)) == 1.0
    mid = time_pressure(ctx_at(DEMO_AS_OF))
    assert mid == pytest.approx(1.0 + (1.0 - 184 / 365))
    start = time_pressure(ctx_at(JAN_1_2026))
    assert start == pytest.approx(1.0 + 1 / 365)
    late = time_pressure(ctx_at(date(2026, 12, 30)))
    assert start < mid < late < 2.0
    # A full leap year (366 days ahead) is clipped so the factor never drops below 1.
    assert time_pressure(ctx_at(date(2024, 1, 1))) == pytest.approx(1.0)


def test_star_weight_is_three_for_cbp_only_and_weights_cover_every_measure() -> None:
    assert set(STAR_WEIGHT) == set(ALL_MEASURES) == set(CLINICAL_WEIGHT)
    assert STAR_WEIGHT["CBP"] == 3.0
    assert all(STAR_WEIGHT[m] == 1.0 for m in ALL_MEASURES if m != "CBP")
    assert CLINICAL_WEIGHT == {
        "CBP": 3.0,
        "SPC": 3.0,
        "SPD": 2.0,
        "EED": 2.0,
        "COL": 2.0,
        "BCS": 2.0,
        "TSC": 1.0,
        "SNS": 1.0,
    }


def test_rank_orders_by_score_desc_then_measure_id_asc() -> None:
    items: list[tuple[MeasureId, float]] = [
        ("EED", 2.0),
        ("COL", 2.0),
        ("BCS", 2.0),
        ("CBP", 9.0),
        ("TSC", 1.0),
    ]
    assert rank(items) == [("CBP", 1), ("BCS", 2), ("COL", 3), ("EED", 4), ("TSC", 5)]


def test_rank_is_independent_of_input_order() -> None:
    items: list[tuple[MeasureId, float]] = [("SPC", 3.0), ("BCS", 2.0), ("SPD", 3.0)]
    assert rank(items) == rank(list(reversed(items))) == [("SPC", 1), ("SPD", 2), ("BCS", 3)]


def test_rank_of_nothing_is_empty() -> None:
    assert rank([]) == []
