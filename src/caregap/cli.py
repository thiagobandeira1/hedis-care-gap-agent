"""``caregap`` — the command line (SPEC sections 7 and 10).

Commands: ``bootstrap`` (ingest the Synthea samples into a DuckDB through P6's own CLI),
``panel`` (engine verdict counts per patient), ``run`` (one patient through the real graph;
prints the outcome or the pending approval request), ``serve`` / ``ui`` (the API and the
Streamlit front end), ``eval`` and ``report`` (delegated to :mod:`caregap.evalrun`).

Conventions: stdout carries only the command's own output (tables, JSON), stderr carries
hints, errors and logs, so ``caregap run ... | jq`` works. Exit codes: 0 success, 1 a run or
eval that failed, 2 a usage / configuration problem. The graph stack (``caregap.runtime``)
and P6 (``fhir_features``) are imported inside the commands that need them, so ``--help``
stays instant and keyless by construction: no command reads a key — ``anthropic`` mode goes
through ``caregap.llm.bundle_for`` and fails with a clear message when none is configured.
"""

import contextlib
import importlib
import json
import os
import secrets
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Protocol, cast

import structlog
import typer

import caregap
from caregap.config import ConfigError, Settings, get_settings
from caregap.graph.build import GraphDeps
from caregap.graph.runner import PatientRunner
from caregap.graph.state import ApprovalRequest, RunOptions, RunOutcome
from caregap.logging_setup import configure_logging, uvicorn_log_config
from caregap.measures.context import MeasurementContext
from caregap.p6.client import P6Client, P6Error

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

#: ``caregap ui`` binds here unless ``--host`` opts in: the console has no authentication.
LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_HOSTS = frozenset({LOOPBACK_HOST, "localhost", "::1"})

#: Engine vocabulary in table order, with the short column labels the panel prints.
VERDICT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("gap_open", "open"),
    ("needs_review", "review"),
    ("closed", "closed"),
    ("excluded", "excluded"),
    ("not_eligible", "n/a"),
)

app = typer.Typer(
    name="caregap",
    help="HEDIS care-gap closure over synthetic (Synthea) patients: engine, agents, HITL.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


class ModelsMode(StrEnum):
    fake = "fake"
    replay = "replay"
    anthropic = "anthropic"


class ApproveMode(StrEnum):
    interrupt = "interrupt"
    auto = "auto"


class EvalTier(StrEnum):
    engine = "engine"
    pipeline = "pipeline"
    outreach = "outreach"


class RuntimeLike(Protocol):
    """What the CLI needs from :class:`caregap.runtime.Runtime`."""

    settings: Settings
    p6: P6Client
    deps: GraphDeps
    runner: PatientRunner

    def close(self) -> None: ...


# --- helpers -----------------------------------------------------------------------------


def _fail(message: str, code: int = EXIT_USAGE) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=code)


def _parse_date(value: str, option: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _fail(f"{option} must be an ISO date (YYYY-MM-DD), got {value!r}") from None


def _settings(**overrides: Any) -> Settings:
    """``CAREGAP_*`` env / ``.env`` settings with the command-line overrides applied."""
    try:
        settings = get_settings()
    except ValueError as exc:  # pydantic ValidationError
        raise _fail(f"invalid CAREGAP_* configuration: {type(exc).__name__}") from exc
    return settings.model_copy(update=overrides) if overrides else settings


@contextlib.contextmanager
def _quiet_stdout() -> Iterator[None]:
    """Send stdout to the PROCESS stderr while P6 code runs.

    P6 (``fhir_features``) configures structlog to print JSON to ``sys.stdout`` and caches its
    loggers on first use, so anything P6 logs while it starts would land in the middle of the
    CLI's machine-readable output — and would keep landing there for the life of the process.
    ``sys.__stderr__`` (not ``sys.stderr``) so a cached P6 logger never holds a per-invocation
    stream such as a test runner's capture buffer.
    """
    target = sys.__stderr__ or sys.stderr
    with contextlib.redirect_stdout(target):
        yield


def _route_logs_to_stderr() -> None:
    """P1's allow-listed JSON logging, written to whatever ``sys.stderr`` is at log time.

    P6 configures structlog with ``cache_logger_on_first_use=True`` and its loggers are first
    used while the embedded service starts, so they hold a ``PrintLogger`` on the ORIGINAL
    process stdout (structlog imports ``sys.stdout`` once; ``redirect_stdout`` cannot move
    it). Dropping those cached bindings makes every later P6 log — one per request in
    embedded mode — rebind through the stderr factory configured here.
    """
    configure_logging()
    structlog.configure(logger_factory=lambda *_: structlog.PrintLogger(file=sys.stderr))
    proxy_type = type(structlog.get_logger())  # BoundLoggerLazyProxy, via the public API
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "fhir_features" or name.startswith("fhir_features.")):
            continue
        for value in list(vars(module).values()):
            if isinstance(value, proxy_type):
                vars(value).pop("bind", None)  # the cache is an instance attribute


def _open_runtime(settings: Settings) -> RuntimeLike:
    """``caregap.runtime.build_runtime`` resolved at call time (``--help`` never pays for the
    graph stack); configuration problems become exit code 2."""
    module = importlib.import_module("caregap.runtime")
    build = cast(Callable[[Settings], RuntimeLike], module.build_runtime)
    try:
        with _quiet_stdout():
            runtime = build(settings)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    except FileNotFoundError as exc:
        raise _fail(f"{settings.p6_mode} P6 mode: {exc}") from exc
    _route_logs_to_stderr()
    return runtime


def _new_run_id() -> str:
    return "run_" + secrets.token_hex(6)


def _dump(model: RunOutcome | ApprovalRequest) -> str:
    return json.dumps(model.model_dump(mode="json"), indent=2, sort_keys=True)


def _render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip(),
        "  ".join("-" * w for w in widths),
    ]
    lines.extend(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows
    )
    return "\n".join(lines)


def _verdict_counts(runtime: RuntimeLike, patient_id: str, as_of: date) -> Counter[str]:
    """Engine-only verdict counts for one patient (no model call, nothing persisted)."""
    record = runtime.p6.get_record(patient_id, to=as_of)
    ctx = MeasurementContext.for_(as_of, record.patient.birth_date)
    return Counter(e.verdict for e in runtime.deps.engine.evaluate(record, ctx))


def _decision_hint(settings: Settings, run_id: str, patient_id: str) -> str:
    api = f"http://{settings.api_host}:{settings.api_port}"
    return "\n".join(
        [
            f"awaiting approval: run_id={run_id} patient_id={patient_id}",
            f"  decide via the API: POST {api}/v1/runs/{run_id}/patients/{patient_id}/decision"
            " (JSON body: ApprovalDecision with a unique decision_id)",
            "  or in the UI:       caregap serve  then  caregap ui  (Patient page, approval card)",
            "  the API must share this run's stores: "
            f"CAREGAP_CHECKPOINT_PATH={settings.checkpoint_path} "
            f"CAREGAP_RUNSTORE_PATH={settings.runstore_path}",
        ]
    )


# --- commands ----------------------------------------------------------------------------


@app.command()
def bootstrap(
    db: Annotated[
        Path | None,
        typer.Option("--db", help="DuckDB file to create (default: CAREGAP_P6_DB_PATH)."),
    ] = None,
    samples: Annotated[
        Path, typer.Option("--samples", help="Directory of Synthea bundles (.json/.json.gz).")
    ] = Path("synthetic/samples"),
) -> None:
    """Ingest the Synthea samples into a DuckDB through P6's own CLI (in-process)."""
    db_path = db if db is not None else _settings().p6_db_path
    if not samples.is_dir():
        raise _fail(f"{samples} is not a directory")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # P6 reads FF_DB_PATH from the environment; set it before fhir_features is imported
    # (scripts/gold.py does the same for the snapshot fixtures).
    os.environ["FF_DB_PATH"] = str(db_path)
    p6_main = cast(Callable[[list[str]], int], importlib.import_module("fhir_features.cli").main)
    with _quiet_stdout():
        if p6_main(["init-db"]) != 0:
            raise _fail("fhir-features init-db failed", EXIT_FAILED)
        if p6_main(["ingest", str(samples)]) != 0:
            raise _fail(f"fhir-features ingest failed for {samples}", EXIT_FAILED)
    typer.echo(f"bootstrapped {db_path} from {samples}")
    typer.echo("next: caregap panel --as-of 2026-06-30", err=True)


@app.command()
def panel(
    as_of: Annotated[str, typer.Option("--as-of", help="Measurement anchor, YYYY-MM-DD.")],
    limit: Annotated[int, typer.Option("--limit", min=1, help="Patients to list.")] = 50,
) -> None:
    """List patients with the engine's verdict counts per patient (engine only, no models)."""
    day = _parse_date(as_of, "--as-of")
    settings = _settings()
    runtime = _open_runtime(settings)
    try:
        page = runtime.p6.list_patients(limit=limit, offset=0)
        rows: list[list[str]] = []
        for item in page.items:
            row = [item.patient_id, item.sex, "yes" if item.deceased else "no"]
            try:
                counts = _verdict_counts(runtime, item.patient_id, day)
            except P6Error as exc:
                row.extend([""] * len(VERDICT_COLUMNS))
                row.append(f"error: {type(exc).__name__}")
            else:
                row.extend(str(counts.get(verdict, 0)) for verdict, _ in VERDICT_COLUMNS)
                row.append("")
            rows.append(row)
    finally:
        runtime.close()
    typer.echo(
        f"panel as_of={day.isoformat()} patients={len(rows)}/{page.total} p6={settings.p6_mode}"
    )
    headers = ["patient_id", "sex", "deceased", *(label for _, label in VERDICT_COLUMNS), "note"]
    typer.echo(_render_table(headers, rows))


@app.command()
def run(
    patient: Annotated[str, typer.Option("--patient", help="P6 patient id.")],
    as_of: Annotated[str, typer.Option("--as-of", help="Measurement anchor, YYYY-MM-DD.")],
    models: Annotated[
        ModelsMode | None,
        typer.Option("--models", help="Model bundle (default: CAREGAP_MODELS)."),
    ] = None,
    approve: Annotated[
        ApproveMode,
        typer.Option("--approve", help="interrupt: pause for a reviewer; auto: batch mode."),
    ] = ApproveMode.interrupt,
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Run id (default: run_<12 hex>).")
    ] = None,
) -> None:
    """Run ONE patient through the graph; print the outcome or the pending approval request."""
    day = _parse_date(as_of, "--as-of")
    settings = _settings(**({"models": models.value} if models is not None else {}))
    rid = run_id or _new_run_id()
    runtime = _open_runtime(settings)
    try:
        result = runtime.runner.start(rid, patient, day, RunOptions(approval_mode=approve.value))
    finally:
        runtime.close()
    typer.echo(_dump(result))
    if isinstance(result, ApprovalRequest):
        typer.echo(_decision_hint(settings, rid, patient), err=True)
        return
    typer.echo(f"finished: run_id={rid} patient_id={patient} status={result.status}", err=True)
    if result.status == "error":
        kind = result.load_error.kind if result.load_error is not None else "graph"
        raise _fail(f"patient run failed ({kind})", EXIT_FAILED)


@app.command()
def serve(
    host: Annotated[
        str | None, typer.Option("--host", help="Bind host (default: CAREGAP_API_HOST).")
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="Bind port (default: CAREGAP_API_PORT).")
    ] = None,
) -> None:
    """Serve the FastAPI app with uvicorn (app factory: caregap.api.app:create_app).

    No access log (request URLs carry patient ids) and a message-only uvicorn formatter that
    drops tracebacks, so nothing bypasses the structlog allow-list (V12)."""
    import uvicorn

    settings = _settings()
    uvicorn.run(
        "caregap.api.app:create_app",
        factory=True,
        host=host if host is not None else settings.api_host,
        port=port if port is not None else settings.api_port,
        access_log=False,
        log_config=uvicorn_log_config(),
    )


@app.command()
def ui(
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            help="Bind host (default: 127.0.0.1; the console has no authentication, so a "
            "non-loopback host is an explicit opt-in).",
        ),
    ] = None,
) -> None:
    """Launch the Streamlit front end (a subprocess; it only talks to the API)."""
    app_path = Path(caregap.__file__).resolve().parent / "ui" / "app.py"
    address = host if host is not None else LOOPBACK_HOST
    if address not in LOOPBACK_HOSTS:
        typer.echo(
            f"warning: binding the approval console to {address}: it has no authentication; "
            "anyone who can reach that address can approve outreach",
            err=True,
        )
    argv = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.address",
        address,
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    completed = subprocess.run(argv, check=False)  # noqa: S603 - fixed argv, no shell
    raise typer.Exit(code=completed.returncode)


@app.command("eval")
def eval_(
    tier: Annotated[EvalTier, typer.Option("--tier", help="engine | pipeline | outreach.")],
    record: Annotated[bool, typer.Option("--record", help="Record real model calls.")] = False,
    judge: Annotated[bool, typer.Option("--judge", help="Run the LLM judge (local).")] = False,
    regen: Annotated[
        bool, typer.Option("--regen", help="Rewrite the committed engine outcomes.")
    ] = False,
    update_baseline: Annotated[
        bool, typer.Option("--update-baseline", help="Accept the artifact as the baseline.")
    ] = False,
    gate: Annotated[bool, typer.Option("--gate", help="Compare against the baseline.")] = False,
) -> None:
    """Run one eval tier (caregap.evalrun.run_eval); the exit code is its result."""
    evalrun = importlib.import_module("caregap.evalrun")
    code = evalrun.run_eval(
        tier.value,
        record=record,
        judge=judge,
        regen=regen,
        update_baseline=update_baseline,
        gate=gate,
    )
    raise typer.Exit(code=int(code))


@app.command()
def report(
    check: Annotated[
        bool, typer.Option("--check", help="Fail when the README regions are stale.")
    ] = False,
) -> None:
    """Sync the README eval regions from the latest eval artifacts (--check: verify only)."""
    evalrun = importlib.import_module("caregap.evalrun")
    raise typer.Exit(code=int(evalrun.sync_readme_cmd(check=check)))


# SPEC section 7 names this command ``readme``; keep it as a hidden alias.
app.command("readme", hidden=True)(report)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
