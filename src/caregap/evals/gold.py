"""Gold cases: the blind, frozen labels every eval tier scores against (SPEC section 6).

One JSONL row per ``(patient_id, measure_id)``; the labeler applied the written demo rules to
value-set-agnostic worksheets and never saw an engine verdict. ``split`` is derived from
``sha256(patient_id)`` (one third dev, two thirds test) and the loader REFUSES a row whose
split disagrees with the hash — a hand-moved item can never leak into the published test
column. ``FREEZE.json`` pins the sha256 of ``gap_cases.jsonl`` before engine contact; the
pipeline tier refuses to run when the two disagree.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

from clinevals import EvalItemBase, Split, load_jsonl, sha256_file
from pydantic import ConfigDict, Field, model_validator

from caregap.measures.ids import MeasureId

GoldStatus = Literal["not_eligible", "closed", "excluded", "open", "escalate"]

GOLD_STATUSES: tuple[GoldStatus, ...] = ("not_eligible", "closed", "excluded", "open", "escalate")
SPLITS: tuple[Split, ...] = ("dev", "test")
GOLD_CASES_NAME = "gap_cases.jsonl"
FREEZE_NAME = "FREEZE.json"
FREEZE_HASH_KEY = "gap_cases_sha256"


def expected_split(patient_id: str) -> Split:
    """``dev`` iff ``int(sha256(patient_id)[:8], 16) % 3 == 0`` (SPEC section 6)."""
    digest = hashlib.sha256(patient_id.encode("utf-8")).hexdigest()
    return "dev" if int(digest[:8], 16) % 3 == 0 else "test"


def gold_item_id(patient_id: str, measure_id: str) -> str:
    return f"{patient_id}:{measure_id}"


class GoldCase(EvalItemBase):
    """One labeled unit. ``item_id`` / ``category`` are derived from the unit when absent so
    the committed JSONL stays minimal; when present they must agree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    patient_id: str = Field(min_length=1)
    measure_id: MeasureId
    as_of: date
    gold: GoldStatus
    rationale: str = Field(min_length=1)
    decisive_dates: list[str] = Field(default_factory=list)
    uncertain: bool = False
    labeler: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _derive_ids(cls, data: Any) -> Any:
        if isinstance(data, dict):
            patient_id, measure_id = data.get("patient_id"), data.get("measure_id")
            if isinstance(patient_id, str) and isinstance(measure_id, str):
                data = dict(data)
                data.setdefault("item_id", gold_item_id(patient_id, measure_id))
                data.setdefault("category", measure_id)
        return data

    @model_validator(mode="after")
    def _check_ids(self) -> "GoldCase":
        expected = gold_item_id(self.patient_id, self.measure_id)
        if self.item_id != expected:
            raise ValueError(f"item_id {self.item_id!r} must be {expected!r}")
        if self.category != self.measure_id:
            raise ValueError(
                f"category {self.category!r} must equal measure_id {self.measure_id!r}"
            )
        for raw in self.decisive_dates:
            try:
                date.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError(f"decisive_dates entry {raw!r} is not an ISO date") from exc
        return self


# --- dataset-level validators -----------------------------------------------------------------


def unique_units(items: Sequence[GoldCase]) -> list[str]:
    seen: set[tuple[str, str]] = set()
    problems: list[str] = []
    for item in items:
        key = (item.patient_id, item.measure_id)
        if key in seen:
            problems.append(f"{item.item_id}: duplicate (patient_id, measure_id)")
        seen.add(key)
    return problems


def split_matches_hash(items: Sequence[GoldCase]) -> list[str]:
    return [
        f"{item.item_id}: split {item.split!r} but sha256(patient_id) assigns "
        f"{expected_split(item.patient_id)!r}"
        for item in items
        if item.split != expected_split(item.patient_id)
    ]


def one_as_of_per_patient(items: Sequence[GoldCase]) -> list[str]:
    """Every case of a patient shares one ``as_of``: the tiers run each patient once."""
    by_patient: dict[str, set[date]] = {}
    for item in items:
        by_patient.setdefault(item.patient_id, set()).add(item.as_of)
    return [
        f"{patient_id}: cases carry several as_of dates {sorted(d.isoformat() for d in dates)}"
        for patient_id, dates in sorted(by_patient.items())
        if len(dates) > 1
    ]


GOLD_VALIDATORS = (unique_units, split_matches_hash, one_as_of_per_patient)


def load_gold(path: Path) -> list[GoldCase]:
    """Typed gold cases; every problem is reported at once (``clinevals.DatasetError``);
    ``FileNotFoundError`` when the file is absent (the caller decides the exit code)."""
    return load_jsonl(path, GoldCase, validators=list(GOLD_VALIDATORS))


def freeze_hash(path: Path) -> str:
    """sha256 of the gold file bytes — what ``FREEZE.json`` pins before engine contact."""
    return sha256_file(path)


def read_frozen_hash(freeze_path: Path) -> str | None:
    """The pinned hash, or ``None`` when the gold set was never frozen."""
    if not freeze_path.exists():
        return None
    payload = json.loads(freeze_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{freeze_path} is not a JSON object")
    value = payload.get(FREEZE_HASH_KEY)
    return value if isinstance(value, str) and value else None


def freeze_status(gold_path: Path, freeze_path: Path) -> str | None:
    """``None`` when ``FREEZE.json`` pins exactly the current gold bytes, else the refusal."""
    pinned = read_frozen_hash(freeze_path)
    if pinned is None:
        return f"gold set is not frozen: {freeze_path} is missing or lacks {FREEZE_HASH_KEY!r}"
    actual = freeze_hash(gold_path)
    if pinned != actual:
        return (
            f"gold set changed after freeze: {freeze_path} pins {pinned[:12]}… but "
            f"{gold_path.name} hashes to {actual[:12]}…"
        )
    return None


def gold_counts(items: Sequence[GoldCase]) -> dict[str, dict[str, int]]:
    """``{split: {gold_status: n, "total": n}}`` plus an ``all`` block; key-sorted."""
    counts: dict[str, dict[str, int]] = {split: dict.fromkeys(GOLD_STATUSES, 0) for split in SPLITS}
    counts["all"] = dict.fromkeys(GOLD_STATUSES, 0)
    for item in items:
        counts[item.split][item.gold] += 1
        counts["all"][item.gold] += 1
    for block in counts.values():
        block["total"] = sum(block.values())
    return {split: dict(sorted(block.items())) for split, block in sorted(counts.items())}
