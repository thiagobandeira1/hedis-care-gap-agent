"""Outcome rows: the review-flag derivation, the byte-stable JSONL fixture, the diff summary,
and ``run_tier`` over the REAL graph (snapshot P6, fake models, ``approval_mode=auto``)."""

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from caregap.evals.outcomes import (
    OutcomeError,
    PatientOutcomeRow,
    diff_summary,
    gold_patients,
    outcome_row,
    read_outcomes,
    render_outcomes,
    review_flagged,
    run_tier,
    state_values,
    tier_options,
    write_outcomes,
)
from caregap.graph.build import checkpoint_serializer
from caregap.graph.state import ApprovalRequest, RunOptions, RunOutcome, thread_id
from caregap.measures.models import ReviewItem
from caregap.runtime import build_runtime
from tests.unit.evals.helpers import (
    AS_OF,
    KAYCE,
    TONY,
    committed_gold,
    gold,
    outcome,
    snapshot_settings,
)


def test_tier_options_are_auto_and_engine_only_for_the_engine_tier() -> None:
    assert tier_options("engine") == RunOptions(approval_mode="auto", validation_mode="off")
    for tier in ("pipeline", "outreach"):
        options = tier_options(tier)
        assert (options.approval_mode, options.validation_mode) == ("auto", "escalated")


def test_gold_patients_one_as_of_each_in_id_order() -> None:
    patients = gold_patients(committed_gold())
    assert list(patients) == sorted(patients)
    assert set(patients.values()) == {AS_OF}
    assert len(patients) == 5


def test_review_flagged_measure_items_final_status_and_global_hold() -> None:
    measure_item = ReviewItem(measure_id="SPC", scope="measure", reason="E3")
    global_item = ReviewItem(measure_id=None, scope="global", reason="E1")
    base = RunOutcome(
        status="completed",
        actionable=False,
        engine_verdicts={"CBP": "gap_open", "EED": "needs_review", "COL": "closed"},
        final_statuses={"CBP": "gap_open", "EED": "needs_review", "COL": "closed"},
    )
    assert review_flagged(base, []) == ["EED"]
    assert review_flagged(base, [measure_item]) == ["EED", "SPC"]
    # A global hold covers every candidate the engine produced.
    assert review_flagged(base, [global_item]) == ["CBP", "EED"]


def test_outcome_row_reads_state_payloads_or_dicts() -> None:
    verdict: dict[str, Any] = {
        "measure_id": "SPC",
        "decision": "needs_human",
        "exclusion_category": None,
        "evidence_ids": [],
        "rule_citation": "",
        "confidence": "low",
        "rationale": "scripted",
        "verified": False,
    }
    state: dict[str, Any] = {
        "verdicts": [verdict],
        "review_items": [{"measure_id": "SPC", "scope": "measure", "reason": "needs_human"}],
    }
    result = RunOutcome(
        status="completed",
        actionable=False,
        engine_verdicts={"SPC": "needs_review"},
        final_statuses={"SPC": "needs_review"},
    )
    row = outcome_row("p1", AS_OF, "pipeline", result, state, fallback_count=2)
    assert row.validator_decisions == {"SPC": "needs_human"}
    assert row.review_flagged == ["SPC"]
    assert row.unverified == ["SPC"]
    assert (row.error, row.fallback_count, row.tier) == (False, 2, "pipeline")


def test_outcomes_jsonl_is_byte_stable_and_round_trips(tmp_path: Path) -> None:
    rows = [outcome("p2", final={"CBP": "gap_open"}), outcome("p1", final={"EED": "closed"})]
    text = render_outcomes(rows)
    assert text == render_outcomes(list(reversed(rows))), "sorted by patient id"
    assert text.endswith("\n") and "\r" not in text
    assert text.index('"patient_id":"p1"') < text.index('"patient_id":"p2"')
    path = tmp_path / "engine-outcomes.jsonl"
    write_outcomes(rows, path)
    assert path.read_bytes() == text.encode("utf-8")
    assert read_outcomes(path) == sorted(rows, key=lambda r: r.patient_id)
    with pytest.raises(FileNotFoundError):
        read_outcomes(tmp_path / "nope.jsonl")


def test_diff_summary_names_patients_and_fields_only() -> None:
    engine = {"CBP": "gap_open"}
    expected = [outcome("p1", final={"CBP": "gap_open"}, engine=engine), outcome("p2")]
    actual = [outcome("p1", final={"CBP": "closed"}, engine=engine), outcome("p3")]
    summary = diff_summary(expected, actual)
    assert "~ p1: final_statuses\n" in summary, "only the changed field is named"
    assert "- p2: only in the committed file" in summary
    assert "+ p3: only in the fresh run" in summary
    assert "gap_open" not in summary
    assert "equal" in diff_summary(expected, expected)


def test_run_tier_drives_the_real_graph_in_auto_mode(tmp_path: Path) -> None:
    runtime = build_runtime(
        snapshot_settings(tmp_path, "tier"), checkpointer=MemorySaver(serde=checkpoint_serializer())
    )
    try:
        cases = [gold(TONY, "EED", "open"), gold(TONY, "COL", "open"), gold(KAYCE, "CBP", "closed")]
        rows = run_tier("engine", cases, runtime=runtime, run_id="t1")
        assert [r.patient_id for r in rows] == [KAYCE, TONY]
        kayce, tony = rows
        assert (kayce.status, kayce.error) == ("no_action", False)
        assert set(kayce.final_statuses.values()) == {"not_eligible"}
        assert (tony.status, tony.error, tony.fallback_count) == ("completed", False, 0)
        assert tony.final_statuses["EED"] == "gap_open"
        assert tony.final_statuses["COL"] == "gap_open"
        assert tony.engine_verdicts == tony.final_statuses
        assert tony.validator_decisions == {} and tony.review_flagged == []
        assert state_values(runtime.graph, thread_id("t1", TONY))["revision_count"] == 0
        assert state_values(runtime.graph, thread_id("t1", "never-ran")) == {}
        assert runtime.run_store.list_outbox() == [], "auto mode never writes the outbox"
        assert isinstance(rows[0], PatientOutcomeRow)
    finally:
        runtime.close()


def test_run_tier_refuses_an_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = build_runtime(
        snapshot_settings(tmp_path, "interrupt"),
        checkpointer=MemorySaver(serde=checkpoint_serializer()),
    )
    try:

        def interrupts(*args: Any, **kwargs: Any) -> ApprovalRequest:
            return ApprovalRequest(run_id="t1", patient_id=TONY, as_of=AS_OF)

        monkeypatch.setattr(runtime.runner, "start", interrupts)
        with pytest.raises(OutcomeError, match="approval_mode=auto"):
            run_tier("engine", [gold(TONY, "EED", "open")], runtime=runtime, run_id="t1")
    finally:
        runtime.close()
