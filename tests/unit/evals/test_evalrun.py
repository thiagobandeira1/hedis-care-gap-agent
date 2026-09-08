"""``caregap eval`` / ``caregap report`` end to end over a temporary ``evals/`` tree: the
committed personas as gold snapshots, a hand-written gold set, and the SPEC section 6 exit
codes — 2 for anything missing (gold, snapshots, baseline, recordings, key), 1 for a measured
failure (byte drift, freeze mismatch, ratchet regression, stale README), 0 otherwise."""

import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from caregap.config import Settings
from caregap.evalrun import (
    EVAL_PLACEHOLDER,
    EXIT_FAIL,
    EXIT_MISSING,
    EXIT_OK,
    MEASURES_PLACEHOLDER,
    EvalPaths,
    render_measures_region,
    run_eval,
    sync_readme_cmd,
)
from caregap.evals.gold import FREEZE_HASH_KEY, freeze_hash
from caregap.evals.outcomes import read_outcomes
from caregap.evals.rubrics import RUBRIC, JudgeRecord, judge_case_key
from tests.unit.evals.helpers import (
    AS_OF,
    MEREDITH,
    SNAPSHOT_DIR,
    TONY,
    committed_gold,
    gold_row,
    snapshot_settings,
    write_gold,
)

README_STUB = (
    "# demo\n\n<!-- EVAL:BEGIN -->\nstale\n<!-- EVAL:END -->\n\n"
    "<!-- EVAL-MEASURES:BEGIN -->\nstale\n<!-- EVAL-MEASURES:END -->\n"
)
GROUNDED = json.dumps(
    {"claims": [{"claim": "an eye exam is due", "verdict": "supported"}], "notes": ""}
)


@pytest.fixture
def evals_dir(tmp_path: Path) -> Path:
    root = tmp_path / "evals"
    root.mkdir()
    return root


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return snapshot_settings(tmp_path, "eval")


def with_gold(root: Path, *, frozen: bool = True) -> EvalPaths:
    """The committed-persona gold set, frozen by default (every tier refuses unfrozen gold)."""
    paths = EvalPaths(root)
    write_gold(paths.gold_cases, (gold_row(c) for c in committed_gold()))
    if frozen:
        freeze(paths)
    return paths


def with_snapshots(paths: EvalPaths) -> EvalPaths:
    shutil.copytree(SNAPSHOT_DIR, paths.snapshots)
    return paths


def freeze(paths: EvalPaths) -> None:
    paths.freeze.write_text(
        json.dumps({FREEZE_HASH_KEY: freeze_hash(paths.gold_cases)}), encoding="utf-8"
    )


def artifact(paths: EvalPaths, tier: str) -> dict[str, Any]:
    payload = json.loads(paths.artifact(tier).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def run(tier: str, root: Path, settings: Settings, **flags: bool) -> int:
    return run_eval(tier, settings=settings, out_dir=root, **flags)  # type: ignore[arg-type]


# --- engine tier ------------------------------------------------------------------------------


def test_missing_gold_labels_exit_2(
    evals_dir: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("engine", evals_dir, settings, gate=True) == EXIT_MISSING
    err = capsys.readouterr().err
    assert "no gold labels" in err and "gap_cases.jsonl" in err
    assert not (evals_dir / "artifacts").exists()


def test_empty_or_invalid_gold_exit_2(
    evals_dir: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = EvalPaths(evals_dir)
    write_gold(paths.gold_cases, [])
    assert run("engine", evals_dir, settings) == EXIT_MISSING
    assert "is empty" in capsys.readouterr().err
    rows = [gold_row(c) for c in committed_gold()]
    write_gold(paths.gold_cases, [rows[0], rows[0]])
    assert run("engine", evals_dir, settings) == EXIT_MISSING
    assert "gold set invalid" in capsys.readouterr().err


def test_missing_snapshots_exit_2(
    evals_dir: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with_gold(evals_dir)
    assert run("engine", evals_dir, settings) == EXIT_MISSING
    assert "no gold snapshots" in capsys.readouterr().err


def test_unknown_tier_exit_2(evals_dir: Path, settings: Settings) -> None:
    with_snapshots(with_gold(evals_dir))
    assert run(cast(Any, "vibes"), evals_dir, settings) == EXIT_MISSING


def test_engine_tier_regen_bytediff_gate_and_baseline(
    evals_dir: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = with_snapshots(with_gold(evals_dir))

    # No committed outcomes: a measured failure, and the artifact is still not written.
    assert run("engine", evals_dir, settings) == EXIT_FAIL
    assert "no committed engine outcomes" in capsys.readouterr().err
    assert not paths.artifact("engine").exists()

    assert run("engine", evals_dir, settings, regen=True) == EXIT_OK
    out = capsys.readouterr().out
    assert f"wrote {paths.engine_outcomes}" in out and "engine tier (test split):" in out
    rows = read_outcomes(paths.engine_outcomes)
    assert [r.patient_id for r in rows] == sorted(r.patient_id for r in rows)
    assert len(rows) == 5 and not any(r.error for r in rows)
    committed = paths.engine_outcomes.read_bytes()

    payload = artifact(paths, "engine")
    assert payload["tier"] == "engine"
    assert payload["validation_mode"] == "off" and payload["approval_mode"] == "auto"
    assert payload["publishable"] is True and payload["fallback_count"] == 0
    overall = payload["metrics"]["overall"]
    assert overall["micro_precision"] == 1.0
    assert overall["micro_recall"] == 0.75
    assert payload["counts"]["per_split"]["test"] == {"fn": 1, "fp": 0, "tp": 3}
    assert payload["metrics"]["overall_dev"]["review_flag_rate"] == 0.0
    assert payload["validator_unsafe_close"] == 0 and payload["unresolved_evidence"] == 0
    by_measure = {row["measure"]: row for row in payload["measures"]}
    assert (by_measure["COL"]["tp"], by_measure["COL"]["fn"], by_measure["COL"]["n_test"]) == (
        1,
        1,
        2,
    )
    assert by_measure["BCS"]["status"] == "insufficient"
    assert "provisional" in payload["publication"]["note"]
    assert payload["screening_counts"] == {
        "SNS": {"closed": 4, "not_eligible": 1},
        "TSC": {"closed": 4, "not_eligible": 1},
    }
    assert payload["freeze"]["status"] == "frozen"
    assert payload["snapshot"]["patient_count"] == "5"
    assert payload["dataset_hash"] == freeze_hash(paths.gold_cases)
    assert payload["error_patients"] == []
    assert payload["validator_model"] == "none" and payload["drafter_model"] == "fake"

    # A second run byte-matches the committed fixture.
    assert run("engine", evals_dir, settings) == EXIT_OK
    assert "engine outcomes match" in capsys.readouterr().out
    assert paths.engine_outcomes.read_bytes() == committed

    # Gate without a baseline: missing, never a vacuous pass.
    assert run("engine", evals_dir, settings, gate=True) == EXIT_MISSING
    assert "no baseline" in capsys.readouterr().err

    assert run("engine", evals_dir, settings, update_baseline=True) == EXIT_OK
    assert "baseline updated" in capsys.readouterr().out
    baseline = json.loads(paths.baseline.read_text(encoding="utf-8"))
    assert baseline["metrics"]["engine.micro_recall"] == 0.75
    assert baseline["sources"]["engine"].startswith("engine-latest.json@")

    assert run("engine", evals_dir, settings, gate=True) == EXIT_OK
    assert "ratchet gate: PASS (engine.micro_precision, engine.micro_recall)" in (
        capsys.readouterr().out
    )

    # A ratchet regression: the baseline demands more recall than the engine delivers.
    baseline["metrics"]["engine.micro_recall"] = 0.9
    paths.baseline.write_text(json.dumps(baseline), encoding="utf-8")
    assert run("engine", evals_dir, settings, gate=True) == EXIT_FAIL
    captured = capsys.readouterr()
    assert "REGRESSION" in captured.out and "regressed" in captured.err

    # Byte drift of the committed fixture is a measured failure naming patients, not content.
    drifted = committed.decode("utf-8").replace('"gap_open"', '"closed"', 1)
    paths.engine_outcomes.write_bytes(drifted.encode("utf-8"))
    assert run("engine", evals_dir, settings) == EXIT_FAIL
    err = capsys.readouterr().err
    assert "drifted" in err and "~ " in err and "--regen" in err


def test_engine_tier_enforces_the_freeze_missing_mismatch_and_ok(
    evals_dir: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """V2: the headline tier scores frozen gold only — missing FREEZE.json exits 2, an edited
    gold set exits 1 (before any engine contact), and the artifact stamps the freeze status."""
    paths = with_snapshots(with_gold(evals_dir, frozen=False))
    assert run("engine", evals_dir, settings, regen=True) == EXIT_MISSING
    assert "not frozen" in capsys.readouterr().err
    assert not paths.artifact("engine").exists() and not paths.engine_outcomes.exists()

    freeze(paths)
    assert run("engine", evals_dir, settings, regen=True) == EXIT_OK
    capsys.readouterr()
    assert artifact(paths, "engine")["freeze"]["status"] == "frozen"

    rows = [gold_row(c) for c in committed_gold()]
    rows[0]["rationale"] = "edited after the freeze"
    write_gold(paths.gold_cases, rows)
    before = paths.artifact("engine").read_bytes()
    assert run("engine", evals_dir, settings) == EXIT_FAIL
    assert "changed after freeze" in capsys.readouterr().err
    assert paths.artifact("engine").read_bytes() == before, "no artifact from edited gold"


def test_report_refuses_an_artifact_not_scored_on_frozen_gold(
    tmp_path: Path, evals_dir: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = with_snapshots(with_gold(evals_dir))
    assert run("engine", evals_dir, settings, regen=True) == EXIT_OK
    capsys.readouterr()
    readme = tmp_path / "README.md"
    readme.write_text(README_STUB, encoding="utf-8")
    payload = artifact(paths, "engine")
    payload["freeze"]["status"] = "mismatch"
    paths.artifact("engine").write_text(json.dumps(payload), encoding="utf-8")
    assert sync_readme_cmd(readme=readme, artifacts_dir=paths.artifacts) == EXIT_FAIL
    assert "freeze status 'mismatch'" in capsys.readouterr().err
    assert readme.read_text(encoding="utf-8") == README_STUB, "nothing published"
    assert sync_readme_cmd(check=True, readme=readme, artifacts_dir=paths.artifacts) == EXIT_FAIL


def test_eval_tiers_never_open_the_live_ledger(
    tmp_path: Path, evals_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V3: the configured ledger / checkpoint files stay absent, the tier runs on an in-memory
    store under a namespaced ``eval-<tier>-<gold sha>`` run id, and ``mark_superseded`` never
    fires against the live path."""
    from caregap.graph import runstore as runstore_module
    from caregap.runtime import PanelRunStore

    live = tmp_path / "live" / "caregap.sqlite"
    live_ckpt = tmp_path / "live" / "checkpoints.sqlite"
    settings = snapshot_settings(tmp_path, "live", runstore_path=live, checkpoint_path=live_ckpt)
    paths = with_snapshots(with_gold(evals_dir))
    opened: list[str] = []
    superseded: list[tuple[str, str]] = []
    original_init = PanelRunStore.__init__
    original_supersede = runstore_module.RunStore.mark_superseded

    def spy_init(self: PanelRunStore, path: Any = runstore_module.MEMORY, **kw: Any) -> None:
        opened.append(str(path))
        original_init(self, path, **kw)

    def spy_supersede(self: runstore_module.RunStore, patient_id: str, newer_run_id: str) -> int:
        superseded.append((self.path, newer_run_id))
        return original_supersede(self, patient_id, newer_run_id)

    monkeypatch.setattr(PanelRunStore, "__init__", spy_init)
    monkeypatch.setattr(runstore_module.RunStore, "mark_superseded", spy_supersede)
    assert run("engine", evals_dir, settings, regen=True) == EXIT_OK
    assert not live.exists() and not live_ckpt.exists()
    assert opened == [runstore_module.MEMORY]
    assert all(path == runstore_module.MEMORY for path, _ in superseded)
    expected_run_id = f"eval-engine-{freeze_hash(paths.gold_cases)[:8]}"
    assert all(run_id == expected_run_id for _, run_id in superseded)
    assert superseded, "finalize supersedes on the eval's own in-memory ledger only"


# --- pipeline tier ----------------------------------------------------------------------------


def test_pipeline_tier_refuses_unfrozen_gold_then_replays_with_fallbacks(
    evals_dir: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = with_snapshots(with_gold(evals_dir, frozen=False))
    assert run("pipeline", evals_dir, settings) == EXIT_MISSING
    assert "not frozen" in capsys.readouterr().err

    freeze(paths)
    assert run("pipeline", evals_dir, settings) == EXIT_MISSING
    assert "no committed engine outcomes" in capsys.readouterr().err
    assert run("engine", evals_dir, settings, regen=True) == EXIT_OK
    capsys.readouterr()

    assert run("pipeline", evals_dir, settings) == EXIT_OK
    out = capsys.readouterr().out
    assert "publication_status       unpublishable" in out
    payload = artifact(paths, "pipeline")
    assert payload["tier"] == "pipeline" and payload["validation_mode"] == "escalated"
    assert payload["recorded_this_run"] is False
    assert payload["fallback_count"] > 0, "no recordings: every drafter call fell back"
    assert payload["publishable"] is False
    assert "no recording" in payload["unpublishable_reason"]
    assert payload["freeze"]["status"] == "frozen"
    assert (
        payload["metrics"]["engine_only_overall"] == artifact(paths, "engine")["metrics"]["overall"]
    )
    assert payload["metrics"]["overall"]["micro_recall"] == 0.75, "template fallback keeps gaps"
    assert payload["validator_model"] == "replay" and payload["drafter_model"] == "replay"
    assert [row["measure"] for row in payload["engine_measures"]] == [
        "CBP",
        "EED",
        "BCS",
        "COL",
        "SPC",
        "SPD",
    ]

    # V13: an unpublishable artifact can neither set the ratchet nor pass the gate.
    assert run("engine", evals_dir, settings, update_baseline=True) == EXIT_OK
    baseline_before = json.loads(paths.baseline.read_text(encoding="utf-8"))
    assert run("pipeline", evals_dir, settings, update_baseline=True) == EXIT_FAIL
    assert "unpublishable artifact" in capsys.readouterr().err
    assert json.loads(paths.baseline.read_text(encoding="utf-8")) == baseline_before
    assert set(baseline_before["metrics"]) == {
        "engine.micro_f1",
        "engine.micro_precision",
        "engine.micro_recall",
    }
    assert run("pipeline", evals_dir, settings, gate=True) == EXIT_FAIL
    assert "nothing publishable to gate" in capsys.readouterr().err

    # Gold edited after the freeze: refused before any engine contact.
    rows = [gold_row(c) for c in committed_gold()]
    rows[0]["rationale"] = "edited after the freeze"
    write_gold(paths.gold_cases, rows)
    assert run("pipeline", evals_dir, settings) == EXIT_FAIL
    assert "changed after freeze" in capsys.readouterr().err


def test_pipeline_record_needs_the_real_models(
    evals_dir: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = with_snapshots(with_gold(evals_dir))
    freeze(paths)
    assert run("engine", evals_dir, settings, regen=True) == EXIT_OK
    capsys.readouterr()
    assert run("pipeline", evals_dir, settings, record=True) == EXIT_MISSING
    assert "CAREGAP_ANTHROPIC_API_KEY" in capsys.readouterr().err


# --- outreach tier ----------------------------------------------------------------------------


def judge_record(patient_id: str, response: str) -> JudgeRecord:
    return JudgeRecord(
        case_key=judge_case_key(patient_id, AS_OF),
        patient_id=patient_id,
        as_of=AS_OF,
        split="dev",  # the tier re-derives the split from gold
        rubric_name=RUBRIC.name,
        rubric_sha256=RUBRIC.sha256,
        judge_model="recorded-judge",
        response=response,
    )


def test_outreach_tier_reparses_recordings_and_flags_untrusted_judges(
    evals_dir: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = with_snapshots(with_gold(evals_dir, frozen=False))
    assert run("outreach", evals_dir, settings) == EXIT_MISSING
    assert "not frozen" in capsys.readouterr().err
    freeze(paths)
    assert run("outreach", evals_dir, settings) == EXIT_MISSING
    assert "no recorded judge outputs" in capsys.readouterr().err
    assert run("outreach", evals_dir, settings, judge=True) == EXIT_MISSING
    assert "CAREGAP_ANTHROPIC_API_KEY" in capsys.readouterr().err

    records = [judge_record(TONY, GROUNDED), judge_record(MEREDITH, "not json at all")]
    paths.judge_recordings.parent.mkdir(parents=True)
    paths.judge_recordings.write_text(
        "".join(json.dumps(r.model_dump(mode="json")) + "\n" for r in records), encoding="utf-8"
    )
    assert run("outreach", evals_dir, settings) == EXIT_OK
    out = capsys.readouterr().out
    assert "judge_human_agreement_status pending" in out
    payload = artifact(paths, "outreach")
    assert payload["tier"] == "outreach" and payload["judged_count"] == 2
    assert payload["judge_parse_failures"] == 1
    assert payload["rubric_sha256"] == RUBRIC.sha256 and payload["rubric_drift"] is False
    assert payload["judge_model"] == "recorded-judge"
    assert payload["metrics"]["overall"]["faithfulness"] == 1.0
    assert payload["metrics"]["overall"]["judge_parse_failure"] == 0.5
    assert payload["untrusted"] is False and payload["spotcheck_labels"] == 0
    assert {item["split"] for item in payload["per_item"]} == {"test"}, "split from gold"

    # A human spot-check that disagrees with the judge stamps the run untrusted.
    paths.spotcheck.write_text(
        json.dumps({"case_key": records[0].case_key, "label": "ungrounded", "reviewer": "rn"})
        + "\n",
        encoding="utf-8",
    )
    assert run("outreach", evals_dir, settings) == EXIT_OK
    capsys.readouterr()
    payload = artifact(paths, "outreach")
    assert payload["judge_human_agreement"] == 0.0
    assert payload["judge_human_agreement_status"] == "0.000"
    assert payload["untrusted"] is True and payload["spotcheck_labels"] == 1


# --- README regions ---------------------------------------------------------------------------


def test_report_placeholders_when_nothing_is_measured(
    tmp_path: Path, evals_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = tmp_path / "README.md"
    artifacts = evals_dir / "artifacts"
    readme.write_text(README_STUB, encoding="utf-8")
    assert sync_readme_cmd(check=True, readme=readme, artifacts_dir=artifacts) == EXIT_FAIL
    assert "placeholders" in capsys.readouterr().err
    assert sync_readme_cmd(readme=readme, artifacts_dir=artifacts) == EXIT_OK
    text = readme.read_text(encoding="utf-8")
    assert EVAL_PLACEHOLDER in text and MEASURES_PLACEHOLDER in text and "stale" not in text
    assert sync_readme_cmd(check=True, readme=readme, artifacts_dir=artifacts) == EXIT_OK
    assert "in sync" in capsys.readouterr().out
    assert readme.read_text(encoding="utf-8") == text, "--check never writes"
    # Missing markers are a clear failure, never a traceback.
    readme.write_text("# no markers\n", encoding="utf-8")
    assert sync_readme_cmd(readme=readme, artifacts_dir=artifacts) == EXIT_FAIL
    assert "README markers" in capsys.readouterr().err


def test_committed_readme_regions_are_in_sync_with_committed_artifacts() -> None:
    """The README carries both regions and matches evals/artifacts (or the placeholders)."""
    root = Path(__file__).resolve().parents[3]
    text = (root / "README.md").read_text(encoding="utf-8")
    assert "<!-- EVAL:BEGIN -->" in text and "<!-- EVAL-MEASURES:BEGIN -->" in text
    assert (
        sync_readme_cmd(
            check=True, readme=root / "README.md", artifacts_dir=root / "evals" / "artifacts"
        )
        == EXIT_OK
    )


def test_report_syncs_and_checks_the_engine_artifact(
    tmp_path: Path, evals_dir: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = with_snapshots(with_gold(evals_dir))
    assert run("engine", evals_dir, settings, regen=True) == EXIT_OK
    capsys.readouterr()
    readme = tmp_path / "README.md"
    readme.write_text(README_STUB, encoding="utf-8")
    assert sync_readme_cmd(check=True, readme=readme, artifacts_dir=paths.artifacts) == EXIT_FAIL
    assert "out of sync" in capsys.readouterr().err

    assert sync_readme_cmd(readme=readme, artifacts_dir=paths.artifacts) == EXIT_OK
    assert "synced from engine-latest.json" in capsys.readouterr().out
    text = readme.read_text(encoding="utf-8")
    assert "| Metric | Engine-only |" in text
    assert "| micro_recall | 0.750 |" in text
    assert "| measure | n_test | tp | fp | fn | precision | recall | f1 |" in text
    assert "| COL | 2 | 1 | 0 | 1 | insufficient | insufficient | insufficient |" in text
    assert "_Source: engine-latest.json (Engine-only, test split)" in text
    assert "_Pipeline tier (Engine+validator, replayed recordings): not yet measured._" in text
    assert "_Outreach faithfulness (LLM judge): not yet measured._" in text
    assert "stale" not in text and "\r" not in text

    assert sync_readme_cmd(check=True, readme=readme, artifacts_dir=paths.artifacts) == EXIT_OK
    assert sync_readme_cmd(readme=readme, artifacts_dir=paths.artifacts) == EXIT_OK
    assert readme.read_text(encoding="utf-8") == text, "idempotent"
    readme.write_text(text.replace("0.750", "0.999"), encoding="utf-8")
    assert sync_readme_cmd(check=True, readme=readme, artifacts_dir=paths.artifacts) == EXIT_FAIL


def test_measures_region_reports_unpublishable_pipeline_and_untrusted_judge() -> None:
    primary: dict[str, Any] = {
        "tier": "engine",
        "git_sha": "abc",
        "dataset_hash": "0123456789abcdef",
        "metrics": {"strict_overall": {"micro_precision": 1.0}},
        "measures": [
            {
                "measure": "EED",
                "n_test": 2,
                "tp": 1,
                "fp": 0,
                "fn": 0,
                "precision": 1.0,
                "recall": 1.0,
                "f1": 1.0,
                "status": "ok",
            },
            "not-a-row",
        ],
        "publication": {"note": "publication guard satisfied for every core measure"},
    }
    pipeline = {"publishable": False, "unpublishable_reason": "3 model call(s) had no recording"}
    outreach: dict[str, Any] = {
        "untrusted": True,
        "judge_human_agreement_status": "0.500",
        "judged_count": 4,
        "metrics": {"overall": {"faithfulness": 0.9}},
    }
    block = render_measures_region(primary, pipeline=pipeline, outreach=outreach)
    assert "| EED | 2 | 1 | 0 | 0 | 1.000 | 1.000 | 1.000 |" in block
    assert "gold_sha256=0123456789ab_" in block
    assert "_Strict variant (needs_review counts as no gap): micro_precision=1.000_" in block
    assert "unpublishable — 3 model call(s) had no recording" in block
    assert "**UNTRUSTED** (judge-human agreement 0.500 < 0.80)" in block
    assert "faithfulness=0.900" in block and "judged=4" in block


def test_report_check_is_stable_across_git_shas(
    tmp_path: Path, evals_dir: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """CI runs `eval` (which restamps the artifact with the current sha) before `report
    --check`; the README regions must therefore carry content hashes only."""
    paths = with_snapshots(with_gold(evals_dir))
    assert run("engine", evals_dir, settings, regen=True) == EXIT_OK
    capsys.readouterr()
    readme = tmp_path / "README.md"
    readme.write_text(README_STUB, encoding="utf-8")
    assert sync_readme_cmd(readme=readme, artifacts_dir=paths.artifacts) == EXIT_OK
    assert "git_sha" not in readme.read_text(encoding="utf-8")
    artifact_path = paths.artifact("engine")
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    payload["git_sha"] = "0" * 40
    artifact_path.write_text(json.dumps(payload), encoding="utf-8")
    assert sync_readme_cmd(check=True, readme=readme, artifacts_dir=paths.artifacts) == EXIT_OK
