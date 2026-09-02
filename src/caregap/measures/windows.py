"""Date-window helpers shared by the rules. All windows are inclusive [start, end]."""

from datetime import date

from pydantic import BaseModel, ConfigDict


class Window(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: date
    end: date
    label: str

    def contains(self, day: date | None) -> bool:
        return day is not None and self.start <= day <= self.end


def measurement_year(as_of: date) -> Window:
    return Window(start=date(as_of.year, 1, 1), end=as_of, label="measurement year to as_of")


def measurement_year_full(as_of: date) -> Window:
    return Window(start=date(as_of.year, 1, 1), end=date(as_of.year, 12, 31), label="MY")


def prior_year(as_of: date) -> Window:
    return Window(
        start=date(as_of.year - 1, 1, 1), end=date(as_of.year - 1, 12, 31), label="prior year"
    )


def my_and_prior_year(as_of: date) -> Window:
    return Window(start=date(as_of.year - 1, 1, 1), end=as_of, label="MY and prior year")


def lookback_years(as_of: date, years: int, *, label: str) -> Window:
    """[Jan 1 of (MY - years + 1) ... as_of] — e.g. years=10 for the colonoscopy window."""
    return Window(start=date(as_of.year - years + 1, 1, 1), end=as_of, label=label)


def bcs_window(as_of: date) -> Window:
    """Oct 1 of MY-2 through as_of (the public 27-month summary window; demo_choice)."""
    return Window(start=date(as_of.year - 2, 10, 1), end=as_of, label="Oct 1 MY-2 to as_of")


def days_before(day: date, days: int) -> date:
    from datetime import timedelta

    return day - timedelta(days=days)


def any_time(as_of: date) -> Window:
    return Window(start=date.min, end=as_of, label="any time through as_of")
