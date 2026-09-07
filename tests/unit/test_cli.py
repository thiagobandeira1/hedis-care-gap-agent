"""``caregap`` CLI over ``typer.testing.CliRunner`` (SPEC section 7), keyless.

``panel`` / ``run`` go through the real ``caregap.runtime`` over ``SnapshotP6Client`` (the
committed personas, or a synthetic escalated persona from ``tests.factories``); the slow
``bootstrap`` test ingests ``synthetic/samples`` into a temporary DuckDB in-process and lists
the panel over the embedded P6. ``serve`` / ``ui`` / ``eval`` / ``report`` are checked at
their seams (``uvicorn.run``, ``subprocess.run``, the ``caregap.evalrun`` entry points, which
the CLI resolves at call time so a monkeypatched attribute is what it calls).
"""

import json
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner, Result

import caregap.runtime as runtime_module
from caregap import cli, evalrun
from caregap.config import Settings
from caregap.graph.runstore import RunStore
from caregap.llm import ModelBundle, fake_bundle
from caregap.runtime import Runtime
from tests.factories import build_record, condition, feature_row, patient, write_snapshot

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = REPO_ROOT / "synthetic" / "p6_snapshots"
SAMPLES_DIR = REPO_ROOT / "synthetic" / "samples"

AS_OF = "2025-12-31"
TONY = "939eea26-a679-2564-5cf9-c0fd557beefc"
"""EED + COL gap_open at 2025-12-31, no escalation (the draft path)."""
KAYCE = "009969ab-f1b8-a2c0-9fb7-f0621d7beea8"
"""Deceased: not_eligible everywhere."""
ESCALATED = "esc-cbp"
"""Synthetic: hypertension abated inside the MY -> CBP needs_review (E6), validator path."""

COMMANDS = ("bootstrap", "panel", "run", "serve", "ui", "eval", "report")
CLINIC_NAME = "Demo Primary Care"
CLINIC_PHONE = "555-0100"

runner = CliRunner()


# --- helpers ------------------------------------------------------------------------------


def invoke(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


def assert_ok(result: Result) -> None:
    assert result.exit_code == 0, (result.exit_code, result.output, repr(result.exception))


def manifest_patient_ids() -> list[str]:
    manifest = json.loads((SNAPSHOT_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    return sorted(str(pid) for pid in manifest["patient_ids"])


def table_rows(stdout: str) -> dict[str, list[str]]:
    """``patient_id -> cells`` for every data row of a ``panel`` table."""
    lines = stdout.splitlines()
    rows: dict[str, list[str]] = {}
    for line in lines[3:]:  # summary line, header, rule
        cells = line.split()
        if cells:
            rows[cells[0]] = cells
    return rows


def plan_json(measure_id: str, gap_name: str) -> str:
    """A drafter answer that satisfies the lint gate by construction."""
    message = (
        f"Hello, our records show some routine care is due. Please call us to book {gap_name}. "
        f"Call {CLINIC_NAME} at {CLINIC_PHONE}. Reply STOP to opt out of these messages."
    )
    return json.dumps(
        {
            "gaps": [
                {"measure_id": measure_id, "urgency": "routine", "rationale": "Open this year."}
            ],
            "actions": [
                {
                    "measure_id": measure_id,
                    "kind": "screening",
                    "detail": f"Schedule {gap_name}",
                    "owner": "care_team",
                }
            ],
            "patient_message": message,
            "provider_note": "Open gap per the packet; please review at the next visit.",
        }
    )


def verdict_json(measure_id: str, decision: str, *, rule_citation: str) -> str:
    return json.dumps(
        {
            "measure_id": measure_id,
            "decision": decision,
            "exclusion_category": None,
            "evidence_ids": [],
            "rule_citation": rule_citation,
            "confidence": "high",
            "rationale": "scripted verdict",
        }
    )


def escalated_snapshot(root: Path) -> Path:
    """One persona whose only eligible measure (CBP, age 40) carries an E6 escalation."""
    as_of = date.fromisoformat(AS_OF)
    record = build_record(
        patient_header=patient(patient_id=ESCALATED, birth_date=date(1985, 6, 15), sex="male"),
        conditions=[
            condition(
                "59621000",
                onset=date(2020, 1, 1),
                abatement=date(2025, 6, 1),
                display="Hypertension",
            )
        ],
    )
    return write_snapshot(
        root,
        as_of=as_of,
        records={ESCALATED: record},
        features={ESCALATED: feature_row(ESCALATED, as_of, has_hypertension=True)},
    )


def inject_models(monkeypatch: pytest.MonkeyPatch, bundle: ModelBundle) -> None:
    """Make ``caregap.runtime.build_runtime`` use a scripted bundle whatever the settings say."""
    real: Callable[..., Runtime] = runtime_module.build_runtime

    def build(settings: Settings, **kwargs: Any) -> Runtime:
        kwargs["models"] = bundle
        return real(settings, **kwargs)

    monkeypatch.setattr(runtime_module, "build_runtime", build)


# --- fixtures -----------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Snapshot P6 mode over the committed personas; every store in ``tmp_path``; no key,
    no ``CAREGAP_MODELS`` (the ``.env``-free default is replay with empty recordings)."""
    paths = {
        "checkpoint": tmp_path / "checkpoints.sqlite",
        "runstore": tmp_path / "caregap.sqlite",
        "recordings": tmp_path / "recorded",
    }
    monkeypatch.setenv("CAREGAP_P6_MODE", "snapshot")
    monkeypatch.setenv("CAREGAP_SNAPSHOT_DIR", str(SNAPSHOT_DIR))
    monkeypatch.setenv("CAREGAP_CHECKPOINT_PATH", str(paths["checkpoint"]))
    monkeypatch.setenv("CAREGAP_RUNSTORE_PATH", str(paths["runstore"]))
    monkeypatch.setenv("CAREGAP_RECORDINGS_DIR", str(paths["recordings"]))
    for name in ("CAREGAP_MODELS", "CAREGAP_API_HOST", "CAREGAP_API_PORT"):
        monkeypatch.delenv(name, raising=False)
    return paths


# --- help ---------------------------------------------------------------------------------


def test_root_help_lists_every_command() -> None:
    result = invoke("--help")
    assert_ok(result)
    assert "Usage" in result.output
    for command in COMMANDS:
        assert command in result.output
    assert "readme" not in result.output  # hidden alias of ``report``


@pytest.mark.parametrize("command", COMMANDS)
def test_help_for_every_command(command: str) -> None:
    result = invoke(command, "--help")
    assert_ok(result)
    assert "Usage" in result.output
    assert command in result.output


def test_no_arguments_shows_help() -> None:
    result = invoke()
    assert "Usage" in result.output
    assert result.exit_code in (0, 2)  # click >= 8.2 reports no-args-is-help as a usage exit


def test_main_entry_point_runs_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["caregap", "--help"])
    with pytest.raises(SystemExit) as info:
        cli.main()
    assert info.value.code == 0


# --- bootstrap ----------------------------------------------------------------------------


def test_bootstrap_rejects_a_missing_samples_directory(tmp_path: Path) -> None:
    result = invoke("bootstrap", "--db", str(tmp_path / "p6.duckdb"), "--samples", "nope")
    assert result.exit_code == 2
    assert "not a directory" in result.stderr


@pytest.mark.slow
def test_bootstrap_then_panel_lists_five_patients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FF_DB_PATH", raising=False)  # restored (removed) on teardown
    db = tmp_path / "p6" / "p6.duckdb"
    result = invoke("bootstrap", "--db", str(db), "--samples", str(SAMPLES_DIR))
    assert_ok(result)
    assert db.is_file()
    assert f"bootstrapped {db}" in result.stdout

    monkeypatch.setenv("CAREGAP_P6_MODE", "embedded")
    monkeypatch.setenv("CAREGAP_P6_DB_PATH", str(db))
    monkeypatch.setenv("CAREGAP_CHECKPOINT_PATH", str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setenv("CAREGAP_RUNSTORE_PATH", str(tmp_path / "caregap.sqlite"))
    result = invoke("panel", "--as-of", AS_OF)
    assert_ok(result)
    assert "panel as_of=2025-12-31 patients=5/5 p6=embedded" in result.stdout
    rows = table_rows(result.stdout)
    assert sorted(rows) == manifest_patient_ids()
    assert rows[TONY][3] == "2"  # EED + COL open, live P6 agrees with the snapshots


# --- panel --------------------------------------------------------------------------------


def test_panel_snapshot_mode_counts_engine_verdicts(cli_env: dict[str, Path]) -> None:
    result = invoke("panel", "--as-of", AS_OF)
    assert_ok(result)
    assert "panel as_of=2025-12-31 patients=5/5 p6=snapshot" in result.stdout
    rows = table_rows(result.stdout)
    assert sorted(rows) == manifest_patient_ids()
    # columns: patient_id sex deceased open review closed excluded n/a
    assert rows[TONY][1:] == ["male", "no", "2", "0", "4", "0", "2"]
    assert rows[KAYCE][1:] == ["female", "yes", "0", "0", "0", "0", "8"]
    assert "{" not in result.stdout  # logs never land on stdout


def test_panel_limit(cli_env: dict[str, Path]) -> None:
    result = invoke("panel", "--as-of", AS_OF, "--limit", "2")
    assert_ok(result)
    assert "patients=2/5" in result.stdout
    assert sorted(table_rows(result.stdout)) == manifest_patient_ids()[:2]


def test_panel_marks_patients_p6_cannot_serve(cli_env: dict[str, Path]) -> None:
    result = invoke("panel", "--as-of", "2026-06-30")  # no snapshot at that anchor
    assert_ok(result)
    rows = table_rows(result.stdout)
    assert len(rows) == 5
    assert all(cells[-1] == "PatientNotFound" for cells in rows.values())


def test_panel_rejects_a_bad_date(cli_env: dict[str, Path]) -> None:
    result = invoke("panel", "--as-of", "2025-13-01")
    assert result.exit_code == 2
    assert "--as-of must be an ISO date" in result.stderr


# --- run ----------------------------------------------------------------------------------


def test_run_fake_auto_prints_the_outcome_json(cli_env: dict[str, Path]) -> None:
    result = invoke(
        "run", "--patient", TONY, "--as-of", AS_OF, "--models", "fake", "--approve", "auto"
    )
    assert_ok(result)
    outcome = json.loads(result.stdout)
    assert outcome["status"] == "completed"
    assert outcome["decision_action"] == "auto_reviewed"
    assert outcome["actionable"] is False
    assert outcome["engine_verdicts"]["EED"] == "gap_open"
    assert outcome["engine_verdicts"]["COL"] == "gap_open"
    assert outcome["engine_verdicts"]["BCS"] == "not_eligible"
    assert outcome["final_statuses"] == outcome["engine_verdicts"]
    assert "status=completed" in result.stderr


def test_run_interrupt_prints_the_pending_request_when_the_validator_confirms(
    cli_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = escalated_snapshot(tmp_path / "p6_snapshots")
    monkeypatch.setenv("CAREGAP_SNAPSHOT_DIR", str(snapshot))
    confirm = verdict_json("CBP", "confirm_open", rule_citation="cbp/numerator/window")
    inject_models(monkeypatch, fake_bundle([confirm], [plan_json("CBP", "a blood pressure check")]))

    result = invoke(
        "run",
        "--patient",
        ESCALATED,
        "--as-of",
        AS_OF,
        "--models",
        "fake",
        "--approve",
        "interrupt",
        "--run-id",
        "run_cli000001",
    )
    assert_ok(result)
    request = json.loads(result.stdout)
    assert (request["run_id"], request["patient_id"], request["as_of"]) == (
        "run_cli000001",
        ESCALATED,
        AS_OF,
    )
    assert [g["measure_id"] for g in request["open_gaps"]] == ["CBP"]
    assert request["open_gaps"][0]["source"] == "validator_confirmed"
    assert request["review_items"] == []
    assert request["draft_error"] is None
    assert [a["measure_id"] for a in request["plan"]["actions"]] == ["CBP"]
    assert request["model_ids"] == {"validator_model": "fake", "drafter_model": "fake"}

    hint = result.stderr
    assert "awaiting approval: run_id=run_cli000001" in hint
    assert f"/v1/runs/run_cli000001/patients/{ESCALATED}/decision" in hint
    assert "caregap ui" in hint
    assert str(cli_env["checkpoint"]) in hint
    # The hand-off is real: the ledger the API shares lists the pending approval.
    with RunStore(cli_env["runstore"]) as store:
        pending = store.list_approvals(status="pending")
    assert [(a.run_id, a.patient_id) for a in pending] == [("run_cli000001", ESCALATED)]


def test_run_interrupt_without_a_validator_script_falls_back_to_the_template(
    cli_env: dict[str, Path],
) -> None:
    """``--models fake`` from the CLI has empty scripts: the drafter fails closed onto the
    TemplateDrafter and the request still reaches the reviewer (draft_error set)."""
    result = invoke("run", "--patient", TONY, "--as-of", AS_OF, "--models", "fake")
    assert_ok(result)
    request = json.loads(result.stdout)
    assert [g["measure_id"] for g in request["open_gaps"]] == ["COL", "EED"]
    assert request["plan"] is not None
    assert request["draft_error"] is not None
    assert request["revision_count"] == 0
    assert "run_" in result.stderr


def test_run_unknown_patient_is_an_error_outcome(cli_env: dict[str, Path]) -> None:
    result = invoke("run", "--patient", "ghost", "--as-of", AS_OF, "--models", "fake")
    assert result.exit_code == 1
    outcome = json.loads(result.stdout)
    assert outcome["status"] == "error"
    assert outcome["load_error"]["kind"] == "not_found"
    assert "patient run failed (not_found)" in result.stderr


def test_run_anthropic_without_a_key_is_a_configuration_error(
    cli_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no ``.env`` in reach either
    result = invoke("run", "--patient", TONY, "--as-of", AS_OF, "--models", "anthropic")
    assert result.exit_code == 2
    assert "CAREGAP_ANTHROPIC_API_KEY" in result.stderr
    assert result.stdout == ""


def test_run_rejects_unknown_models_and_approve_values(cli_env: dict[str, Path]) -> None:
    result = invoke("run", "--patient", TONY, "--as-of", AS_OF, "--models", "gpt")
    assert result.exit_code == 2
    result = invoke("run", "--patient", TONY, "--as-of", AS_OF, "--approve", "never")
    assert result.exit_code == 2


# --- serve / ui ---------------------------------------------------------------------------


def test_serve_runs_uvicorn_with_the_app_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_run(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setenv("CAREGAP_API_HOST", "127.0.0.9")
    monkeypatch.setenv("CAREGAP_API_PORT", "8123")

    assert_ok(invoke("serve"))
    assert_ok(invoke("serve", "--host", "127.0.0.2", "--port", "9010"))
    assert calls == [
        (
            ("caregap.api.app:create_app",),
            {"factory": True, "host": "127.0.0.9", "port": 8123},
        ),
        (
            ("caregap.api.app:create_app",),
            {"factory": True, "host": "127.0.0.2", "port": 9010},
        ),
    ]


def test_ui_launches_streamlit_as_a_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        assert kwargs == {"check": False}
        return subprocess.CompletedProcess(argv, 3)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = invoke("ui")
    assert result.exit_code == 3  # streamlit's exit code is passed through
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:4] == [sys.executable, "-m", "streamlit", "run"]
    assert Path(argv[4]) == REPO_ROOT / "src" / "caregap" / "ui" / "app.py"
    # V1: the console has no authentication, so it binds loopback only by default.
    options = dict(zip(argv[5::2], argv[6::2], strict=True))
    assert options == {
        "--server.address": "127.0.0.1",
        "--server.headless": "true",
        "--browser.gatherUsageStats": "false",
    }
    assert "warning" not in result.output


def test_ui_host_is_an_explicit_opt_in_with_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = invoke("ui", "--host", "0.0.0.0")
    assert result.exit_code == 0
    assert calls[0][calls[0].index("--server.address") + 1] == "0.0.0.0"
    assert "warning: binding the approval console to 0.0.0.0" in result.output
    assert "no authentication" in result.output


# --- eval / report ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ["--tier", "engine"],
            {
                "record": False,
                "judge": False,
                "regen": False,
                "update_baseline": False,
                "gate": False,
            },
        ),
        (
            ["--tier", "pipeline", "--record", "--gate"],
            {
                "record": True,
                "judge": False,
                "regen": False,
                "update_baseline": False,
                "gate": True,
            },
        ),
        (
            ["--tier", "outreach", "--judge", "--regen", "--update-baseline"],
            {"record": False, "judge": True, "regen": True, "update_baseline": True, "gate": False},
        ),
    ],
)
def test_eval_delegates_to_evalrun_with_the_pinned_kwargs(
    monkeypatch: pytest.MonkeyPatch, args: list[str], expected: dict[str, bool]
) -> None:
    calls: list[tuple[str, dict[str, bool]]] = []

    def run_eval(tier: str, **kwargs: bool) -> int:
        calls.append((tier, kwargs))
        return 3

    monkeypatch.setattr(evalrun, "run_eval", run_eval)
    result = invoke("eval", *args)
    assert result.exit_code == 3  # the tier's exit code is the command's
    assert calls == [(args[1], expected)]


def test_eval_requires_a_tier() -> None:
    result = invoke("eval")
    assert result.exit_code == 2
    assert "--tier" in result.output


def test_eval_rejects_an_unknown_tier() -> None:
    assert invoke("eval", "--tier", "vibes").exit_code == 2


@pytest.mark.parametrize(
    ("command", "check"), [("report", False), ("report", True), ("readme", True)]
)
def test_report_delegates_to_sync_readme_cmd(
    monkeypatch: pytest.MonkeyPatch, command: str, check: bool
) -> None:
    calls: list[dict[str, bool]] = []

    def sync_readme_cmd(**kwargs: bool) -> int:
        calls.append(kwargs)
        return 1 if kwargs["check"] else 0

    monkeypatch.setattr(evalrun, "sync_readme_cmd", sync_readme_cmd)
    result = invoke(command, *(["--check"] if check else []))
    assert result.exit_code == (1 if check else 0)
    assert calls == [{"check": check}]
