"""``MeasurementContext``: age at Dec 31 of the MY, retrospective flag, time-to-MY-end."""

from datetime import date

import pytest
from pydantic import ValidationError

from caregap.measures.context import MeasurementContext
from tests.factories import DEMO_AS_OF, EVAL_AS_OF


def test_age_is_taken_at_dec_31_even_when_the_birthday_is_after_as_of() -> None:
    # Born Sep 15 1960: 65 on Jun 30 2026, but 66 at Dec 31 2026 -> the MY age is 66.
    ctx = MeasurementContext.for_(DEMO_AS_OF, date(1960, 9, 15))
    assert ctx.age_at_my_end == 66
    assert ctx.as_of < date(2026, 9, 15) < ctx.my_end


@pytest.mark.parametrize(
    ("birth_date", "expected"),
    [
        (date(1960, 1, 1), 65),
        (date(1960, 6, 15), 65),
        (date(1960, 12, 31), 65),  # birthday ON Dec 31 counts
        (date(1960, 2, 29), 65),  # leap-day birthday
        (date(1959, 12, 31), 66),
        (date(2007, 12, 31), 18),
        (date(2008, 1, 1), 17),
    ],
)
def test_age_at_my_end_for_the_eval_anchor(birth_date: date, expected: int) -> None:
    assert MeasurementContext.for_(EVAL_AS_OF, birth_date).age_at_my_end == expected


def test_unknown_birth_date_gives_none_age() -> None:
    ctx = MeasurementContext.for_(EVAL_AS_OF, None)
    assert ctx.age_at_my_end is None


def test_my_bounds_come_from_the_as_of_year() -> None:
    ctx = MeasurementContext.for_(DEMO_AS_OF, date(1960, 6, 15))
    assert ctx.my_start == date(2026, 1, 1)
    assert ctx.my_end == date(2026, 12, 31)
    assert ctx.as_of == DEMO_AS_OF


@pytest.mark.parametrize(
    ("as_of", "retrospective"),
    [
        (EVAL_AS_OF, True),
        (DEMO_AS_OF, False),
        (date(2025, 12, 30), False),
        (date(2025, 1, 1), False),
    ],
)
def test_retrospective_only_when_as_of_is_dec_31(as_of: date, retrospective: bool) -> None:
    assert MeasurementContext.for_(as_of, date(1960, 6, 15)).retrospective is retrospective


@pytest.mark.parametrize(
    ("as_of", "days"),
    [
        (EVAL_AS_OF, 0),
        (DEMO_AS_OF, 184),
        (date(2026, 1, 1), 364),
        (date(2024, 1, 1), 365),  # leap year: Jan 1 -> Dec 31 is 365 days
    ],
)
def test_days_to_my_end(as_of: date, days: int) -> None:
    assert MeasurementContext.for_(as_of, date(1960, 6, 15)).days_to_my_end == days


def test_prior_my_bounds() -> None:
    demo = MeasurementContext.for_(DEMO_AS_OF, date(1960, 6, 15))
    assert (demo.prior_my_start, demo.prior_my_end) == (date(2025, 1, 1), date(2025, 12, 31))
    ev = MeasurementContext.for_(EVAL_AS_OF, date(1960, 6, 15))
    assert (ev.prior_my_start, ev.prior_my_end) == (date(2024, 1, 1), date(2024, 12, 31))


def test_context_is_frozen() -> None:
    ctx = MeasurementContext.for_(EVAL_AS_OF, date(1960, 6, 15))
    with pytest.raises(ValidationError):
        ctx.as_of = DEMO_AS_OF
