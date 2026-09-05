"""Gold cases (SPEC section 6): derived ids, the hash-derived split the loader enforces, the
dataset validators reported together, and the freeze hash the pipeline tier refuses without."""

import json
from pathlib import Path

import pytest
from clinevals import DatasetError
from pydantic import ValidationError

from caregap.evals.gold import (
    FREEZE_HASH_KEY,
    GoldCase,
    expected_split,
    freeze_hash,
    freeze_status,
    gold_counts,
    gold_item_id,
    load_gold,
    read_frozen_hash,
)
from tests.unit.evals.helpers import (
    KAYCE,
    MEREDITH,
    TONY,
    committed_gold,
    gold,
    gold_row,
    synthetic_patient,
    write_gold,
)


def test_split_is_derived_from_the_patient_hash() -> None:
    assert expected_split(TONY) == "test"
    assert expected_split(MEREDITH) == "test"
    assert expected_split(KAYCE) == "dev"
    assert all(expected_split(f"p{n}") in {"dev", "test"} for n in range(50))
    assert expected_split("x") == expected_split("x"), "deterministic"
    assert synthetic_patient("dev", 0) != synthetic_patient("test", 0)


def test_gold_case_derives_item_id_and_category() -> None:
    case = gold(TONY, "EED", "open")
    assert case.item_id == gold_item_id(TONY, "EED") == f"{TONY}:EED"
    assert case.category == "EED"
    assert case.split == "test"


def test_gold_case_rejects_inconsistent_ids_and_dates() -> None:
    base = gold_row(gold(TONY, "EED", "open"))
    with pytest.raises(ValidationError, match="item_id"):
        GoldCase.model_validate({**base, "item_id": "wrong"})
    with pytest.raises(ValidationError, match="category"):
        GoldCase.model_validate({**base, "category": "CBP"})
    with pytest.raises(ValidationError, match="ISO date"):
        GoldCase.model_validate({**base, "decisive_dates": ["2025-13-01"]})
    with pytest.raises(ValidationError):
        GoldCase.model_validate({**base, "gold": "maybe"})
    with pytest.raises(ValidationError):  # extra="forbid"
        GoldCase.model_validate({**base, "engine_verdict": "closed"})


def test_load_gold_round_trips_the_committed_shape(tmp_path: Path) -> None:
    cases = committed_gold()
    path = write_gold(tmp_path / "gap_cases.jsonl", (gold_row(c) for c in cases))
    loaded = load_gold(path)
    assert loaded == cases
    assert {c.split for c in loaded} == {"dev", "test"}


def test_load_gold_reports_every_dataset_problem_at_once(tmp_path: Path) -> None:
    rows = [
        gold_row(gold(TONY, "EED", "open")),
        gold_row(gold(TONY, "EED", "closed")),  # duplicate unit
        {**gold_row(gold(KAYCE, "CBP", "not_eligible")), "split": "test"},  # hand-moved
        {**gold_row(gold(MEREDITH, "BCS", "open")), "as_of": "2024-12-31"},
        gold_row(gold(MEREDITH, "COL", "open")),  # second as_of for Meredith
    ]
    path = write_gold(tmp_path / "gap_cases.jsonl", rows)
    with pytest.raises(DatasetError) as info:
        load_gold(path)
    message = str(info.value)
    assert "duplicate" in message
    assert "sha256(patient_id) assigns 'dev'" in message
    assert "several as_of dates" in message


def test_load_gold_missing_file_is_a_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_gold(tmp_path / "missing.jsonl")


def test_freeze_hash_and_status(tmp_path: Path) -> None:
    path = write_gold(tmp_path / "gap_cases.jsonl", [gold_row(gold(TONY, "EED", "open"))])
    freeze = tmp_path / "FREEZE.json"
    digest = freeze_hash(path)
    assert len(digest) == 64
    assert read_frozen_hash(freeze) is None
    assert freeze_status(path, freeze) is not None
    assert "not frozen" in str(freeze_status(path, freeze))

    freeze.write_text(json.dumps({FREEZE_HASH_KEY: digest}), encoding="utf-8")
    assert read_frozen_hash(freeze) == digest
    assert freeze_status(path, freeze) is None

    write_gold(path, [gold_row(gold(TONY, "EED", "closed"))])
    refusal = freeze_status(path, freeze)
    assert refusal is not None and "changed after freeze" in refusal

    freeze.write_text(json.dumps({FREEZE_HASH_KEY: ""}), encoding="utf-8")
    assert read_frozen_hash(freeze) is None
    freeze.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        read_frozen_hash(freeze)


def test_gold_counts_per_split() -> None:
    counts = gold_counts(committed_gold())
    assert set(counts) == {"all", "dev", "test"}
    assert counts["test"]["open"] == 4
    assert counts["test"]["total"] == 14
    assert counts["dev"]["escalate"] == 1
    assert counts["all"]["total"] == counts["dev"]["total"] + counts["test"]["total"]
