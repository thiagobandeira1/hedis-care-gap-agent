"""Gold-protocol tooling: P6 snapshot fixtures and engine goldens (SPEC sections 5, 6, 9).

Subcommands::

    uv run python scripts/gold.py snapshot --as-of 2025-12-31 --out synthetic/p6_snapshots
    uv run python scripts/gold.py goldens [--regen] [--snapshots DIR] [--out DIR]

``snapshot`` boots the real fhir-feature-service in-process over a throwaway DuckDB (P6's own
CLI ingests ``synthetic/samples``; ``FF_DB_PATH`` is set BEFORE ``fhir_features`` is imported),
then writes, per patient, the raw ``GET /v1/patients/{id}/record?to=<as_of>`` body (every
section, every observation code — labelers must see everything) as
``<pid>/record_<as_of>.json.gz``, the raw ``/features?as_of=<as_of>`` body as
``<pid>/features_<as_of>.json``, and a ``MANIFEST.json`` the :class:`SnapshotP6Client` reads.
Running it again for another ``--as-of`` merges into the same manifest.

``goldens`` evaluates every (patient, as_of) pair in the snapshot dir through
:func:`caregap.measures.engine.default_engine` and writes / checks
``tests/unit/measures/goldens/<pid>_<as_of>.json`` (sorted keys, indent 2, LF, trailing newline
— the byte layout ``tests/unit/measures/test_goldens.py`` compares against).

Synthetic (Synthea) data only. Outputs are deterministic: gzip members carry ``mtime=0`` and
JSON keys are sorted, so a regen over unchanged inputs is byte-identical.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SAMPLES = REPO_ROOT / "synthetic" / "samples"
DEFAULT_SNAPSHOTS = REPO_ROOT / "synthetic" / "p6_snapshots"
DEFAULT_GOLDENS = REPO_ROOT / "tests" / "unit" / "measures" / "goldens"
MANIFEST_NAME = "MANIFEST.json"


# --- shared helpers ------------------------------------------------------------------------


def render_goldens_json(payload: Any) -> str:
    """The one serialisation the goldens test byte-compares: sorted keys, indent 2, LF, EOF LF."""
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_text_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_gzip_json(path: Path, payload: Any) -> None:
    """Deterministic gzip (mtime=0, no filename) around compact sorted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(body.encode("utf-8"))
    path.write_bytes(buf.getvalue())


def git_head_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git resolved from PATH on purpose
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


# --- snapshot ------------------------------------------------------------------------------


def _ingest_samples(db_path: Path, samples: Path) -> None:
    """Run P6's own CLI in-process: ``init-db`` then ``ingest <samples>``.

    ``FF_DB_PATH`` must be in the environment before ``fhir_features`` is imported anywhere;
    P6 reads its settings from the environment on every call, but the import-time logging and
    adapter setup are the point of doing it first (SPEC section 5: bootstrap bypasses the HTTP
    size cap by ingesting through the CLI).
    """
    os.environ["FF_DB_PATH"] = str(db_path)
    if "fhir_features" in sys.modules:
        raise RuntimeError("fhir_features imported before FF_DB_PATH was set")
    from fhir_features.cli import main as p6_main

    if p6_main(["init-db"]) != 0:
        raise RuntimeError("fhir-features init-db failed")
    if p6_main(["ingest", str(samples)]) != 0:
        raise RuntimeError(f"fhir-features ingest failed for {samples}")


def _get_json(client: Any, path: str, params: dict[str, Any] | None = None) -> Any:
    response = client.get(path, params=params)
    if response.status_code != 200:
        raise RuntimeError(f"{path}: HTTP {response.status_code}")
    return response.json()


def cmd_snapshot(args: argparse.Namespace) -> int:
    as_of: date = args.as_of
    out: Path = args.out
    samples: Path = args.samples
    if not samples.is_dir():
        print(f"error: {samples} is not a directory", file=sys.stderr)
        return 2
    command = "python scripts/gold.py " + " ".join(sys.argv[1:])

    with tempfile.TemporaryDirectory(prefix="caregap-p6-") as tmp:
        db_path = Path(tmp) / "p6.duckdb"
        _ingest_samples(db_path, samples)

        from caregap.p6.embedded import build_embedded_client

        with build_embedded_client(db_path) as client:
            health = _get_json(client, "/healthz")
            schema = _get_json(client, "/v1/features/schema")
            page = _get_json(client, "/v1/patients", {"limit": 1000, "offset": 0})
            patient_ids = sorted(str(item["patient_id"]) for item in page["items"])
            if not patient_ids:
                print("error: P6 has no patients after ingest", file=sys.stderr)
                return 1

            for pid in patient_ids:
                folder = out / pid
                record = _get_json(client, f"/v1/patients/{pid}/record", {"to": as_of.isoformat()})
                write_gzip_json(folder / f"record_{as_of.isoformat()}.json.gz", record)
                features = _get_json(
                    client, f"/v1/patients/{pid}/features", {"as_of": as_of.isoformat()}
                )
                write_text_lf(
                    folder / f"features_{as_of.isoformat()}.json", render_goldens_json(features)
                )
                counts = record.get("counts", {})
                summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                print(f"{pid} @ {as_of}: {summary}")

    manifest_path = out / MANIFEST_NAME
    previous = _load_manifest(manifest_path)
    as_of_dates = sorted({*previous.get("as_of_dates", []), as_of.isoformat()})
    all_patient_ids = sorted({*previous.get("patient_ids", []), *patient_ids})
    manifest = {
        "service_version": str(health["service_version"]),
        "schema_version": int(health["schema_version"]),
        "feature_version": str(health["feature_version"]),
        "valuesets_version": str(schema["valuesets_version"]),
        "generated_from_sha": git_head_sha(),
        "command": command,
        "patient_ids": all_patient_ids,
        "as_of_dates": as_of_dates,
        "source": "synthea",
    }
    write_text_lf(manifest_path, render_goldens_json(manifest))
    print(f"wrote {manifest_path} ({len(all_patient_ids)} patients, as_of {as_of_dates})")
    return 0


# --- goldens -------------------------------------------------------------------------------


def snapshot_pairs(snapshots: Path) -> list[tuple[str, date]]:
    """Every (patient_id, as_of) with a record file, in sorted order."""
    from caregap.p6.snapshot import SnapshotP6Client

    client = SnapshotP6Client(snapshots)
    pairs: list[tuple[str, date]] = []
    for pid in sorted(client.manifest.patient_ids):
        for path in sorted((snapshots / pid).glob("record_*.json.gz")):
            pairs.append((pid, date.fromisoformat(path.name[len("record_") : -len(".json.gz")])))
    return pairs


def golden_text(snapshots: Path, patient_id: str, as_of: date) -> str:
    """The engine's ``list[MeasureEvaluation]`` for one persona at one as_of, rendered."""
    from caregap.measures.context import MeasurementContext
    from caregap.measures.engine import default_engine
    from caregap.p6.snapshot import SnapshotP6Client

    client = SnapshotP6Client(snapshots)
    record = client.get_record(patient_id, to=as_of)
    ctx = MeasurementContext.for_(as_of, record.patient.birth_date)
    evaluations = default_engine().evaluate(record, ctx)
    return render_goldens_json([e.model_dump(mode="json") for e in evaluations])


def cmd_goldens(args: argparse.Namespace) -> int:
    snapshots: Path = args.snapshots
    out: Path = args.out
    pairs = snapshot_pairs(snapshots)
    if not pairs:
        print(f"error: no snapshot records under {snapshots}", file=sys.stderr)
        return 2
    mismatches = 0
    for pid, as_of in pairs:
        path = out / f"{pid}_{as_of.isoformat()}.json"
        fresh = golden_text(snapshots, pid, as_of)
        if args.regen:
            write_text_lf(path, fresh)
            print(f"wrote {path}")
            continue
        committed = path.read_bytes().decode("utf-8") if path.exists() else None
        if committed != fresh:
            mismatches += 1
            print(f"MISMATCH {path}" if committed is not None else f"MISSING {path}")
        else:
            print(f"ok {path}")
    if mismatches:
        print(f"{mismatches} golden(s) differ; rerun with --regen to accept", file=sys.stderr)
        return 1
    return 0


# --- entry ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gold.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="write P6 snapshot fixtures for one as_of")
    snap.add_argument("--as-of", type=date.fromisoformat, required=True)
    snap.add_argument("--out", type=Path, default=DEFAULT_SNAPSHOTS)
    snap.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    snap.set_defaults(func=cmd_snapshot)

    gold = sub.add_parser("goldens", help="check (default) or --regen engine goldens")
    gold.add_argument("--regen", action="store_true")
    gold.add_argument("--snapshots", type=Path, default=DEFAULT_SNAPSHOTS)
    gold.add_argument("--out", type=Path, default=DEFAULT_GOLDENS)
    gold.set_defaults(func=cmd_goldens)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
