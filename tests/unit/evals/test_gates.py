"""Ratchet gates: which artifact numbers are gated per tier, the no-vacuous-pass rule (an
absent baseline raises), regressions beyond the 0.02 tolerance, and the merged baseline."""

import json
from pathlib import Path
from typing import Any

import pytest

from caregap.evals.gates import (
    GATES,
    TOLERANCE,
    check_gates,
    flatten_metrics,
    gates_for,
    update_baseline,
)


def engine_artifact(precision: float = 1.0, recall: float = 0.75) -> dict[str, Any]:
    return {
        "tier": "engine",
        "git_sha": "abc",
        "metrics": {
            "overall": {"micro_precision": precision, "micro_recall": recall, "micro_f1": 0.857},
            "overall_dev": {"micro_precision": 0.1},
        },
    }


def pipeline_artifact() -> dict[str, Any]:
    return {
        "tier": "pipeline",
        "git_sha": "def",
        "validator_unsafe_close": 1,
        "unresolved_evidence": 0,
        "publishable": True,
        "metrics": {
            "overall": {"micro_precision": 0.9, "micro_recall": 0.8, "review_flag_rate": 1.0},
            "engine_only_overall": {"micro_precision": 1.0, "micro_recall": 0.75},
        },
    }


def test_gate_set_matches_the_spec() -> None:
    assert [g.metric for g in GATES] == [
        "micro_precision",
        "micro_recall",
        "engine.micro_precision",
        "engine.micro_recall",
        "validator_unsafe_close",
        "unresolved_evidence",
    ]
    assert {g.metric for g in GATES if not g.higher_is_better} == {
        "validator_unsafe_close",
        "unresolved_evidence",
    }
    assert TOLERANCE == 0.02
    assert [g.metric for g in gates_for("engine")] == [
        "engine.micro_precision",
        "engine.micro_recall",
    ]
    assert len(gates_for("pipeline")) == 6
    assert gates_for("outreach") == [] and gates_for("vibes") == []


def test_flatten_metrics_per_tier() -> None:
    assert flatten_metrics(engine_artifact()) == {
        "engine.micro_f1": 0.857,
        "engine.micro_precision": 1.0,
        "engine.micro_recall": 0.75,
    }
    assert flatten_metrics(pipeline_artifact()) == {
        "engine.micro_precision": 1.0,
        "engine.micro_recall": 0.75,
        "micro_precision": 0.9,
        "micro_recall": 0.8,
        "review_flag_rate": 1.0,
        "unresolved_evidence": 0.0,
        "validator_unsafe_close": 1.0,
    }
    assert (
        flatten_metrics({"tier": "outreach", "metrics": {"overall": {"faithfulness": 1.0}}}) == {}
    )
    assert flatten_metrics({"tier": "engine", "metrics": {"overall": {"flag": True}}}) == {}
    assert flatten_metrics({"tier": "engine"}) == {}


def test_check_gates_requires_a_baseline(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        check_gates(engine_artifact(), tmp_path / "baseline.json")


def test_check_gates_pass_fail_and_unmeasured(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"metrics": {}}), encoding="utf-8")
    passed, message = check_gates(engine_artifact(), baseline)
    assert passed and "no gated metric" in message

    passed, message = check_gates({"tier": "outreach", "metrics": {"overall": {}}}, baseline)
    assert passed and "no gated metrics for tier 'outreach'" in message

    baseline.write_text(
        json.dumps({"metrics": {"engine.micro_precision": 1.0, "engine.micro_recall": 0.75}}),
        encoding="utf-8",
    )
    passed, message = check_gates(engine_artifact(), baseline)
    assert passed and message == "ratchet gate: PASS (engine.micro_precision, engine.micro_recall)"
    # Within tolerance.
    assert check_gates(engine_artifact(recall=0.74), baseline)[0]
    # Beyond it.
    passed, message = check_gates(engine_artifact(recall=0.5), baseline)
    assert not passed
    assert message.startswith("ratchet gate: FAIL")
    assert "engine.micro_recall" in message

    # Lower-is-better counts regress upward.
    baseline.write_text(json.dumps({"metrics": {"validator_unsafe_close": 0.0}}), encoding="utf-8")
    passed, message = check_gates(pipeline_artifact(), baseline)
    assert not passed and "validator_unsafe_close" in message


def test_update_baseline_merges_tiers_deterministically(tmp_path: Path) -> None:
    baseline = tmp_path / "nested" / "baseline.json"
    merged = update_baseline(engine_artifact(), baseline, source="engine-latest.json@abc")
    assert merged == flatten_metrics(engine_artifact())
    first = baseline.read_bytes()
    assert first.endswith(b"\n") and b"\r" not in first
    assert update_baseline(engine_artifact(), baseline, source="engine-latest.json@abc") == merged
    assert baseline.read_bytes() == first, "byte-identical on an unchanged artifact"

    merged = update_baseline(pipeline_artifact(), baseline, source="pipeline-latest.json@def")
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    assert payload["metrics"] == merged
    assert merged["engine.micro_f1"] == 0.857, "the engine tier's keys survive"
    assert merged["micro_precision"] == 0.9
    assert payload["sources"] == {
        "engine": "engine-latest.json@abc",
        "pipeline": "pipeline-latest.json@def",
    }
    assert "never aspirational" in payload["note"]
    assert list(payload) == sorted(payload)
