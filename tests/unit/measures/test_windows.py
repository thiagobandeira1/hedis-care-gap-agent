"""Date windows are inclusive [start, end]; every rule window derives from ``as_of``."""

from datetime import date

import pytest
from pydantic import ValidationError

from caregap.measures.windows import (
    Window,
    any_time,
    bcs_window,
    days_before,
    lookback_years,
    measurement_year,
    measurement_year_full,
    my_and_prior_year,
    prior_year,
)
from tests.factories import DEMO_AS_OF, EVAL_AS_OF


def test_bcs_window_is_oct_1_of_my_minus_2_through_as_of() -> None:
    window = bcs_window(EVAL_AS_OF)
    assert (window.start, window.end) == (date(2023, 10, 1), EVAL_AS_OF)
    demo = bcs_window(DEMO_AS_OF)
    assert (demo.start, demo.end) == (date(2024, 10, 1), DEMO_AS_OF)
    assert window.label == "Oct 1 MY-2 to as_of"


def test_lookback_years_10_starts_jan_1_of_my_minus_9() -> None:
    window = lookback_years(EVAL_AS_OF, 10, label="colonoscopy")
    assert (window.start, window.end) == (date(2016, 1, 1), EVAL_AS_OF)
    assert window.label == "colonoscopy"
    assert lookback_years(DEMO_AS_OF, 10, label="x").start == date(2017, 1, 1)
    # years=1 collapses to the measurement year to as_of.
    assert lookback_years(DEMO_AS_OF, 1, label="x").start == date(2026, 1, 1)


def test_contains_is_inclusive_on_both_boundaries_and_false_for_none() -> None:
    window = Window(start=date(2025, 1, 1), end=date(2025, 12, 31), label="MY")
    assert window.contains(date(2025, 1, 1))
    assert window.contains(date(2025, 12, 31))
    assert window.contains(date(2025, 6, 15))
    assert not window.contains(date(2024, 12, 31))
    assert not window.contains(date(2026, 1, 1))
    assert not window.contains(None)


def test_measurement_year_family_derives_from_as_of() -> None:
    my = measurement_year(DEMO_AS_OF)
    assert (my.start, my.end) == (date(2026, 1, 1), DEMO_AS_OF)
    full = measurement_year_full(DEMO_AS_OF)
    assert (full.start, full.end) == (date(2026, 1, 1), date(2026, 12, 31))
    prior = prior_year(DEMO_AS_OF)
    assert (prior.start, prior.end) == (date(2025, 1, 1), date(2025, 12, 31))
    both = my_and_prior_year(DEMO_AS_OF)
    assert (both.start, both.end) == (date(2025, 1, 1), DEMO_AS_OF)
    ever = any_time(DEMO_AS_OF)
    assert (ever.start, ever.end) == (date.min, DEMO_AS_OF)


def test_measurement_year_ends_at_as_of_not_dec_31() -> None:
    # Numerator windows end at as_of (SPEC section 2); the full MY is a separate helper.
    assert measurement_year(EVAL_AS_OF).end == date(2025, 12, 31)
    assert measurement_year(DEMO_AS_OF).end == date(2026, 6, 30)
    assert measurement_year_full(DEMO_AS_OF).end == date(2026, 12, 31)


@pytest.mark.parametrize(
    ("day", "days", "expected"),
    [
        (date(2025, 1, 1), 0, date(2025, 1, 1)),
        (date(2025, 1, 1), 1, date(2024, 12, 31)),
        (date(2025, 1, 1), 89, date(2024, 10, 4)),
        (date(2025, 1, 1), 90, date(2024, 10, 3)),
        (date(2025, 1, 1), 91, date(2024, 10, 2)),
        (date(2024, 3, 1), 1, date(2024, 2, 29)),
    ],
)
def test_days_before(day: date, days: int, expected: date) -> None:
    assert days_before(day, days) == expected


def test_window_is_frozen() -> None:
    window = measurement_year(EVAL_AS_OF)
    with pytest.raises(ValidationError):
        window.end = DEMO_AS_OF
