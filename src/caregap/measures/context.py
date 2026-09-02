"""Measurement-year context. Age is age at Dec 31 of the MY (CMS Technical Notes wording)."""

from datetime import date

from pydantic import BaseModel, ConfigDict


class MeasurementContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: date
    my_start: date
    my_end: date
    age_at_my_end: int | None
    """None when birth_date is unknown (denominator becomes ``unknown`` -> needs_review)."""
    retrospective: bool
    """True when as_of == Dec 31 of the MY: report-only, time_pressure = 1."""

    @classmethod
    def for_(cls, as_of: date, birth_date: date | None) -> "MeasurementContext":
        my_start = date(as_of.year, 1, 1)
        my_end = date(as_of.year, 12, 31)
        age = None
        if birth_date is not None:
            age = my_end.year - birth_date.year
            if (my_end.month, my_end.day) < (birth_date.month, birth_date.day):
                age -= 1
        return cls(
            as_of=as_of,
            my_start=my_start,
            my_end=my_end,
            age_at_my_end=age,
            retrospective=as_of == my_end,
        )

    @property
    def days_to_my_end(self) -> int:
        return max((self.my_end - self.as_of).days, 0)

    @property
    def prior_my_start(self) -> date:
        return date(self.as_of.year - 1, 1, 1)

    @property
    def prior_my_end(self) -> date:
        return date(self.as_of.year - 1, 12, 31)
