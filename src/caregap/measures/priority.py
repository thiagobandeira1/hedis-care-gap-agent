"""Deterministic gap priority: ``star_weight * clinical_weight * time_pressure``.

star_weight: 3 for CBP (an outcome measure in the CMS weighting section), 1 otherwise.
clinical_weight: CBP 3, SPC 3, SPD/EED/COL/BCS 2, TSC/SNS 1; +1 for CBP ``no_bp_in_my`` or
SPC ``low_intensity_only``. time_pressure: 1 + (1 - days_to_my_end/365); 1 when retrospective.
Rank is assigned by code from the score, ties broken by measure id.
"""

from collections.abc import Iterable

from caregap.measures.context import MeasurementContext
from caregap.measures.ids import MeasureId

STAR_WEIGHT: dict[MeasureId, float] = {
    "CBP": 3.0,
    "EED": 1.0,
    "BCS": 1.0,
    "COL": 1.0,
    "SPC": 1.0,
    "SPD": 1.0,
    "TSC": 1.0,
    "SNS": 1.0,
}
CLINICAL_WEIGHT: dict[MeasureId, float] = {
    "CBP": 3.0,
    "SPC": 3.0,
    "SPD": 2.0,
    "EED": 2.0,
    "COL": 2.0,
    "BCS": 2.0,
    "TSC": 1.0,
    "SNS": 1.0,
}
SUBTYPE_BONUS: dict[tuple[MeasureId, str], float] = {
    ("CBP", "no_bp_in_my"): 1.0,
    ("SPC", "low_intensity_only"): 1.0,
}


def time_pressure(ctx: MeasurementContext) -> float:
    if ctx.retrospective:
        return 1.0
    return 1.0 + (1.0 - min(ctx.days_to_my_end, 365) / 365.0)


def priority_score(measure_id: MeasureId, subtype: str | None, ctx: MeasurementContext) -> float:
    clinical = CLINICAL_WEIGHT[measure_id] + SUBTYPE_BONUS.get((measure_id, subtype or ""), 0.0)
    return round(STAR_WEIGHT[measure_id] * clinical * time_pressure(ctx), 4)


def rank(items: Iterable[tuple[MeasureId, float]]) -> list[tuple[MeasureId, int]]:
    """Deterministic ranking: score descending, then measure id ascending."""
    ordered = sorted(items, key=lambda pair: (-pair[1], pair[0]))
    return [(measure_id, position) for position, (measure_id, _) in enumerate(ordered, start=1)]
