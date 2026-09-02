"""Measure identifiers and display names."""

from typing import Literal

MeasureId = Literal["CBP", "EED", "BCS", "COL", "SPC", "SPD", "TSC", "SNS"]

CORE_MEASURES: tuple[MeasureId, ...] = ("CBP", "EED", "BCS", "COL", "SPC", "SPD")
SCREENING_MEASURES: tuple[MeasureId, ...] = ("TSC", "SNS")
ALL_MEASURES: tuple[MeasureId, ...] = CORE_MEASURES + SCREENING_MEASURES

MEASURE_NAMES: dict[MeasureId, str] = {
    "CBP": "Controlling High Blood Pressure",
    "EED": "Eye Exam for Patients With Diabetes",
    "BCS": "Breast Cancer Screening",
    "COL": "Colorectal Cancer Screening",
    "SPC": "Statin Therapy for Patients With Cardiovascular Disease",
    "SPD": "Statin Use in Persons With Diabetes",
    "TSC": "Tobacco Use Screening",
    "SNS": "Social Need Screening",
}

#: CMS Star Ratings ids for the 2026 Technical Notes (citations in the rule JSON).
STAR_IDS: dict[MeasureId, str | None] = {
    "CBP": "C14",
    "EED": "C11",
    "BCS": "C01",
    "COL": "C02",
    "SPC": "C19",
    "SPD": "D12",
    "TSC": None,
    "SNS": None,
}

CONFORMANCE_NOTICE = "demo-grade; HEDIS-aligned; not NCQA-certified"
