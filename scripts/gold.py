"""Gold-protocol tooling: P6 snapshot fixtures, engine goldens, and the blind panel draw
(SPEC sections 5, 6, 9).

Subcommands::

    uv run python scripts/gold.py snapshot --as-of 2025-12-31 --out synthetic/p6_snapshots
    uv run python scripts/gold.py snapshot --as-of 2025-12-31 --db data/p6-panel.duckdb \\
        --patients-from evals/gold/SELECTION.json --out evals/gold/snapshots
    uv run python scripts/gold.py goldens [--regen] [--snapshots DIR] [--out DIR]
    uv run python scripts/gold.py select --db data/p6-panel.duckdb --as-of 2025-12-31 \\
        [--n 60] [--seed 20260903] [--cap 2500] [--out evals/gold]

``snapshot`` boots the real fhir-feature-service in-process — over a throwaway DuckDB that
P6's own CLI fills from ``synthetic/samples`` (``FF_DB_PATH`` is set BEFORE ``fhir_features``
is imported), or over an existing ``--db`` (no ingest) — then writes, per patient, the raw
``GET /v1/patients/{id}/record?to=<as_of>`` body (every section, every observation code —
labelers must see everything) as ``<pid>/record_<as_of>.json.gz``, the raw
``/features?as_of=<as_of>`` body as ``<pid>/features_<as_of>.json``, and a ``MANIFEST.json``
the :class:`SnapshotP6Client` reads. ``--patients-from SELECTION.json`` restricts the run to the
blind draw. Running it again for another ``--as-of`` merges into the same manifest.

``goldens`` evaluates every (patient, as_of) pair in the snapshot dir through
:func:`caregap.measures.engine.default_engine` and writes / checks
``tests/unit/measures/goldens/<pid>_<as_of>.json`` (sorted keys, indent 2, LF, trailing newline
— the byte layout ``tests/unit/measures/test_goldens.py`` compares against).

``select`` is the BLIND stratified draw of SPEC section 6: for every panel patient it computes
DESCRIPTIVE FACTS ONLY from the raw record at ``?to=as_of`` (age, sex, death, keyword flags over
display strings, a few observation-code presences) — never the engine, never a value set —
then draws ``--n`` patients with a seeded, reproducible stratified sample and writes
``FEASIBILITY.md`` (panel counts, shortfalls) and ``SELECTION.json`` (facts + stratum per
patient). Patients whose event count exceeds ``--cap`` are excluded from the draw because a
blind labeler cannot read them.

Synthetic (Synthea) data only. Outputs are deterministic: gzip members carry ``mtime=0``, JSON
keys are sorted, and the draw is a pure function of (facts, seed), so a regen over unchanged
inputs is byte-identical.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SAMPLES = REPO_ROOT / "synthetic" / "samples"
DEFAULT_SNAPSHOTS = REPO_ROOT / "synthetic" / "p6_snapshots"
DEFAULT_GOLDENS = REPO_ROOT / "tests" / "unit" / "measures" / "goldens"
DEFAULT_GOLD_DIR = REPO_ROOT / "evals" / "gold"
MANIFEST_NAME = "MANIFEST.json"
SELECTION_NAME = "SELECTION.json"
FEASIBILITY_NAME = "FEASIBILITY.md"

DEFAULT_N = 60
DEFAULT_SEED = 20260903
DEFAULT_CAP = 2500
"""Labeling-feasibility cap: total events dated <= as_of a blind labeler can still read."""

EVENT_SECTIONS: tuple[str, ...] = (
    "conditions",
    "procedures",
    "medications",
    "observations",
    "encounters",
)
SECTION_DATE_FIELD: dict[str, str] = {
    "conditions": "onset_date",
    "procedures": "performed_date",
    "medications": "authored_date",
    "observations": "effective_date",
    "encounters": "start_date",
}


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


@contextmanager
def _embedded_client(db: Path | None, samples: Path) -> Iterator[Any]:
    """The embedded P6 TestClient over ``db`` (no ingest) or over a throwaway ingest."""
    from caregap.p6.embedded import build_embedded_client

    if db is not None:
        with build_embedded_client(db) as client:
            yield client
        return
    with tempfile.TemporaryDirectory(prefix="caregap-p6-") as tmp:
        db_path = Path(tmp) / "p6.duckdb"
        _ingest_samples(db_path, samples)
        with build_embedded_client(db_path) as client:
            yield client


def _get_json(client: Any, path: str, params: dict[str, Any] | None = None) -> Any:
    response = client.get(path, params=params)
    if response.status_code != 200:
        raise RuntimeError(f"{path}: HTTP {response.status_code}")
    return response.json()


def _all_patient_ids(client: Any) -> list[str]:
    ids: list[str] = []
    offset = 0
    while True:
        page = _get_json(client, "/v1/patients", {"limit": 1000, "offset": offset})
        items = page["items"]
        ids.extend(str(item["patient_id"]) for item in items)
        offset += len(items)
        if not items or offset >= int(page.get("total", offset)):
            break
    return sorted(set(ids))


def selection_patient_ids(path: Path) -> list[str]:
    """The sorted patient ids of a ``SELECTION.json`` written by ``select``."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return sorted(str(item["patient_id"]) for item in raw["patients"])


# --- snapshot ------------------------------------------------------------------------------


def cmd_snapshot(args: argparse.Namespace) -> int:
    as_of: date = args.as_of
    out: Path = args.out
    samples: Path = args.samples
    db: Path | None = args.db
    if db is None and not samples.is_dir():
        print(f"error: {samples} is not a directory", file=sys.stderr)
        return 2
    if db is not None and not db.is_file():
        print(f"error: {db} is not a file", file=sys.stderr)
        return 2
    wanted: list[str] | None = None
    if args.patients_from is not None:
        wanted = selection_patient_ids(args.patients_from)
        if not wanted:
            print(f"error: {args.patients_from} lists no patients", file=sys.stderr)
            return 2
    command = "python scripts/gold.py " + " ".join(sys.argv[1:])

    with _embedded_client(db, samples) as client:
        health = _get_json(client, "/healthz")
        schema = _get_json(client, "/v1/features/schema")
        available = _all_patient_ids(client)
        if not available:
            print("error: P6 has no patients", file=sys.stderr)
            return 1
        patient_ids = available
        if wanted is not None:
            missing = sorted(set(wanted) - set(available))
            if missing:
                print(f"error: {len(missing)} selected patient(s) not in P6: {missing[:5]}")
                return 1
            patient_ids = wanted

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


# --- select: descriptive facts ---------------------------------------------------------------

#: Keyword flags over condition / procedure / medication display strings (case-insensitive).
#: Descriptive only: a stratification aid, deliberately NOT a value set and never an engine
#: input. ``diabetes`` refuses ``prediabetes``; ``mammogra`` catches mammogram AND mammography;
#: FOBT/FIT accepts the Synthea wording ``occult blood in feces`` and the upper-case token FIT.
KEYWORD_FLAGS: dict[str, str] = {
    "hypertension": r"hypertension",
    "diabetes": r"(?<!pre)diabetes",
    "ascvd": r"myocardial infarction|coronary|ischemic heart|stroke|peripheral vascular|angina",
    "statin": (
        r"simvastatin|atorvastatin|rosuvastatin|pravastatin|lovastatin|pitavastatin|fluvastatin"
    ),
    "mammogram": r"mammogra",
    "colonoscopy": r"colonoscopy",
    "fobt_fit": r"fecal occult|occult blood|fecal immunochemical|\bfit\b",
    "eye_exam": r"retinal|ophthalm|eye exam",
    "hospice": r"hospice",
    "dementia": r"dementia|alzheimer",
    "esrd_dialysis": r"dialysis|renal failure|end[- ]stage",
}
_KEYWORD_RE: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE) for name, pattern in KEYWORD_FLAGS.items()
}
_STOPPED_STATUSES = frozenset({"stopped", "cancelled"})

BP_PANEL_CODE = "85354-9"
TOBACCO_CODE = "72166-2"
PRAPARE_CODE = "93025-5"
ACUTE_CLASSES = frozenset({"IMP", "EMER"})


@dataclass(frozen=True)
class PatientFacts:
    """Descriptive facts of one panel patient at ``as_of`` — the ONLY input of the draw."""

    patient_id: str
    sex: str
    birth_date: str | None
    death_date: str | None
    """Death on or before as_of (a later death is invisible to a run at as_of)."""
    age: int | None
    """Age at Dec 31 of the measurement year (calendar year of as_of)."""
    deceased: bool
    event_count: int
    """Conditions + procedures + medications + observations + encounters dated <= as_of."""
    hypertension: bool = False
    diabetes: bool = False
    ascvd: bool = False
    statin: bool = False
    statin_stopped: bool = False
    """Any statin-keyword medication whose status is stopped or cancelled."""
    mammogram: bool = False
    colonoscopy: bool = False
    fobt_fit: bool = False
    eye_exam: bool = False
    hospice: bool = False
    dementia: bool = False
    esrd_dialysis: bool = False
    bp_panel_in_my: bool = False
    """An observation coded 85354-9 dated inside the measurement year."""
    tobacco_screen_in_my: bool = False
    prapare_in_my: bool = False
    encounter_in_my: bool = False
    inpatient_or_ed_in_my: bool = False

    @property
    def alive(self) -> bool:
        return not self.deceased


FLAG_NAMES: tuple[str, ...] = (
    *KEYWORD_FLAGS,
    "statin_stopped",
    "bp_panel_in_my",
    "tobacco_screen_in_my",
    "prapare_in_my",
    "encounter_in_my",
    "inpatient_or_ed_in_my",
)


def age_at_my_end(birth: date | None, as_of: date) -> int | None:
    """Age at Dec 31 of the measurement year (the calendar year of ``as_of``)."""
    if birth is None:
        return None
    my_end = date(as_of.year, 12, 31)
    return my_end.year - birth.year - ((my_end.month, my_end.day) < (birth.month, birth.day))


def _date_of(row: Mapping[str, Any], key: str) -> date | None:
    raw = row.get(key)
    if not isinstance(raw, str) or not raw:
        return None
    return date.fromisoformat(raw[:10])


def facts_from_record(payload: Mapping[str, Any], as_of: date) -> PatientFacts:
    """Descriptive facts from a raw ``/record?to=as_of`` body. No value set, no engine."""
    my_start = date(as_of.year, 1, 1)
    header = payload["patient"]
    birth = _date_of(header, "birth_date")
    death = _date_of(header, "death_date")
    if death is not None and death > as_of:
        death = None

    events: dict[str, list[Mapping[str, Any]]] = {}
    for section in EVENT_SECTIONS:
        key = SECTION_DATE_FIELD[section]
        kept: list[Mapping[str, Any]] = []
        for row in payload.get(section, []):
            when = _date_of(row, key)
            if when is not None and when <= as_of:
                kept.append(row)
        events[section] = kept

    displays = [
        str(row.get("code_display") or "")
        for section in ("conditions", "procedures", "medications")
        for row in events[section]
    ]
    flags = {
        name: any(pattern.search(text) for text in displays)
        for name, pattern in _KEYWORD_RE.items()
    }
    statin_re = _KEYWORD_RE["statin"]
    statin_stopped = any(
        statin_re.search(str(row.get("code_display") or ""))
        and str(row.get("status") or "").lower() in _STOPPED_STATUSES
        for row in events["medications"]
    )

    def in_my(row: Mapping[str, Any], key: str) -> bool:
        when = _date_of(row, key)
        return when is not None and my_start <= when <= as_of

    def obs_in_my(code: str) -> bool:
        return any(
            row.get("code") == code and in_my(row, "effective_date")
            for row in events["observations"]
        )

    encounters_in_my = [row for row in events["encounters"] if in_my(row, "start_date")]
    return PatientFacts(
        patient_id=str(header["patient_id"]),
        sex=str(header.get("sex") or "unknown"),
        birth_date=birth.isoformat() if birth else None,
        death_date=death.isoformat() if death else None,
        age=age_at_my_end(birth, as_of),
        deceased=death is not None,
        event_count=sum(len(rows) for rows in events.values()),
        statin_stopped=statin_stopped,
        bp_panel_in_my=obs_in_my(BP_PANEL_CODE),
        tobacco_screen_in_my=obs_in_my(TOBACCO_CODE),
        prapare_in_my=obs_in_my(PRAPARE_CODE),
        encounter_in_my=bool(encounters_in_my),
        inpatient_or_ed_in_my=any(
            str(row.get("encounter_class") or "").upper() in ACUTE_CLASSES
            for row in encounters_in_my
        ),
        **flags,
    )


# --- select: the stratified draw -------------------------------------------------------------

EDGE_AGES: frozenset[int] = frozenset(
    {17, 18, 20, 21, 39, 40, 49, 50, 51, 52, 65, 66, 74, 75, 76, 85, 86}
)
"""Ages one year either side of every demo-rule age boundary (18, 21, 40, 50, 52, 66, 75, 85)."""


def _in_band(facts: PatientFacts, low: int, high: int) -> bool:
    return facts.age is not None and low <= facts.age <= high


def is_escalation_carrier(facts: PatientFacts) -> bool:
    """Hospice OR dementia OR a stopped statin OR hypertension without a BP panel in the MY."""
    return (
        facts.hospice
        or facts.dementia
        or facts.statin_stopped
        or (facts.hypertension and not facts.bp_panel_in_my)
    )


def is_edge_case(facts: PatientFacts) -> bool:
    return facts.deceased or (facts.age is not None and facts.age in EDGE_AGES)


@dataclass(frozen=True)
class Stratum:
    name: str
    target: int
    """How many members the draw tries to reach (counting members drawn under other strata)."""
    minimum: int
    """The guarantee SPEC section 6 asks for; a shortfall is logged, never hidden."""
    description: str
    member: Callable[[PatientFacts], bool]


#: Draw order = priority: rarer strata first so the common ones cannot starve them.
STRATA: tuple[Stratum, ...] = (
    Stratum(
        "escalation_carrier",
        6,
        6,
        "alive; hospice OR dementia OR stopped/cancelled statin OR hypertension with no BP "
        "panel in the MY (E1/E3/E4/E5-style review carriers)",
        lambda f: f.alive and is_escalation_carrier(f),
    ),
    Stratum(
        "edge",
        4,
        2,
        "deceased on/before as_of OR age at Dec 31 in " + ", ".join(map(str, sorted(EDGE_AGES))),
        is_edge_case,
    ),
    Stratum(
        "ascvd_65_75",
        8,
        8,
        "alive; age 65-75; ASCVD keyword (SPC-eligible-ish)",
        lambda f: f.alive and _in_band(f, 65, 75) and f.ascvd,
    ),
    Stratum(
        "diabetes_65_75",
        8,
        8,
        "alive; age 65-75; diabetes keyword, prediabetes ignored (EED/SPD-eligible-ish)",
        lambda f: f.alive and _in_band(f, 65, 75) and f.diabetes,
    ),
    Stratum(
        "women_65_74",
        8,
        8,
        "alive; female; age 65-74 (BCS-eligible-ish)",
        lambda f: f.alive and f.sex == "female" and _in_band(f, 65, 74),
    ),
    Stratum(
        "hypertension",
        8,
        8,
        "alive; hypertension keyword, any age (CBP-eligible-ish)",
        lambda f: f.alive and f.hypertension,
    ),
    Stratum(
        "no_gap_probe_65_75",
        12,
        8,
        "alive; age 65-75; no diabetes / ASCVD / hypertension keyword (no-gap probes)",
        lambda f: f.alive and _in_band(f, 65, 75) and not (f.diabetes or f.ascvd or f.hypertension),
    ),
)
FILLER = "filler"


@dataclass(frozen=True)
class StratumReport:
    target: int
    minimum: int
    description: str
    available: int
    """Members inside the cap (the pool the draw could pick from)."""
    available_over_cap: int
    """Members lost to the labeling-feasibility cap."""
    drawn: int
    """Drawn under this stratum's own pass."""
    selected: int
    """Members among the final selection, whichever pass drew them."""
    shortfall: int
    """max(0, minimum - selected): the panel could not honour the guarantee."""


@dataclass(frozen=True)
class SelectedPatient:
    patient_id: str
    stratum: str
    facts: PatientFacts


@dataclass(frozen=True)
class Selection:
    as_of: str
    seed: int
    n: int
    cap: int
    panel_size: int
    excluded_over_cap: list[str]
    strata: dict[str, StratumReport]
    patients: list[SelectedPatient]
    filler_count: int = 0
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "seed": self.seed,
            "n": self.n,
            "cap": self.cap,
            "panel_size": self.panel_size,
            "selected": len(self.patients),
            "excluded_over_cap": list(self.excluded_over_cap),
            "strata": {name: asdict(report) for name, report in self.strata.items()},
            "filler_count": self.filler_count,
            "notes": list(self.notes),
            "patients": [
                {"patient_id": p.patient_id, "stratum": p.stratum, "facts": asdict(p.facts)}
                for p in self.patients
            ],
        }


def select_patients(
    facts: Mapping[str, PatientFacts],
    *,
    as_of: date,
    n: int = DEFAULT_N,
    seed: int = DEFAULT_SEED,
    cap: int = DEFAULT_CAP,
    strata: Sequence[Stratum] = STRATA,
) -> Selection:
    """A pure function of (facts, seed): sorted ids, one ``random.Random(seed)``, one pass per
    stratum in priority order (each pass counts members already drawn), then a filler draw to
    reach exactly ``n`` (or the whole pool when the panel is smaller)."""
    ordered = [facts[pid] for pid in sorted(facts)]
    over_cap = [f.patient_id for f in ordered if f.event_count > cap]
    pool = [f for f in ordered if f.event_count <= cap]
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling, not security
    chosen: dict[str, str] = {}
    drawn_per_stratum: dict[str, int] = {}
    for stratum in strata:
        members = [f for f in pool if stratum.member(f)]
        already = sum(1 for f in members if f.patient_id in chosen)
        candidates = [f for f in members if f.patient_id not in chosen]
        k = max(0, min(stratum.target - already, len(candidates), n - len(chosen)))
        for picked in rng.sample(candidates, k) if k else []:
            chosen[picked.patient_id] = stratum.name
        drawn_per_stratum[stratum.name] = k
    remaining = [f for f in pool if f.patient_id not in chosen]
    filler = max(0, min(n - len(chosen), len(remaining)))
    for picked in rng.sample(remaining, filler) if filler else []:
        chosen[picked.patient_id] = FILLER

    selected = [facts[pid] for pid in sorted(chosen)]
    reports: dict[str, StratumReport] = {}
    notes: list[str] = []
    for stratum in strata:
        count = sum(1 for f in selected if stratum.member(f))
        in_pool = sum(1 for f in pool if stratum.member(f))
        lost = sum(1 for f in ordered if f.event_count > cap and stratum.member(f))
        shortfall = max(0, stratum.minimum - count)
        reports[stratum.name] = StratumReport(
            target=stratum.target,
            minimum=stratum.minimum,
            description=stratum.description,
            available=in_pool,
            available_over_cap=lost,
            drawn=drawn_per_stratum[stratum.name],
            selected=count,
            shortfall=shortfall,
        )
        if shortfall:
            notes.append(
                f"{stratum.name}: {count} selected < minimum {stratum.minimum} "
                f"({in_pool} available inside the cap, {lost} more over the cap)"
            )
        elif count < stratum.target:
            notes.append(
                f"{stratum.name}: {count} selected < target {stratum.target} (minimum "
                f"{stratum.minimum} met; {in_pool} available inside the cap, {lost} over the cap)"
            )
    if len(chosen) < n:
        notes.append(f"panel allows only {len(chosen)} patients inside the cap (< n={n})")
    deceased = sum(1 for f in ordered if f.deceased)
    if deceased == 0:
        notes.append(
            "no panel patient is deceased on/before as_of: the edge stratum holds "
            "boundary ages only"
        )
    stopped = sum(1 for f in ordered if f.statin_stopped)
    if stopped == 0:
        notes.append(
            "no panel patient carries a stopped/cancelled statin: escalation carriers "
            "are hospice / dementia / hypertension-without-BP-panel only"
        )
    return Selection(
        as_of=as_of.isoformat(),
        seed=seed,
        n=n,
        cap=cap,
        panel_size=len(ordered),
        excluded_over_cap=over_cap,
        strata=reports,
        patients=[SelectedPatient(f.patient_id, chosen[f.patient_id], f) for f in selected],
        filler_count=filler,
        notes=notes,
    )


# --- select: feasibility report --------------------------------------------------------------

AGE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("<18", 0, 17),
    ("18-39", 18, 39),
    ("40-49", 40, 49),
    ("50-64", 50, 64),
    ("65-75", 65, 75),
    ("76-85", 76, 85),
    ("86+", 86, 999),
)
COUNT_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("<= 500", 0, 500),
    ("501-1000", 501, 1000),
    ("1001-2500", 1001, 2500),
    ("2501-5000", 2501, 5000),
    ("5001-10000", 5001, 10000),
    ("> 10000", 10001, 10**9),
)


def _band(age: int | None) -> str:
    if age is None:
        return "unknown"
    for label, low, high in AGE_BANDS:
        if low <= age <= high:
            return label
    return "unknown"


def _md_table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return lines


def render_feasibility(facts: Mapping[str, PatientFacts], selection: Selection) -> str:
    """``FEASIBILITY.md``: panel-wide descriptive counts, the cap, strata availability and any
    shortfall against the SPEC section 6 guarantees. Descriptive only — no engine, no value set."""
    all_facts = [facts[pid] for pid in sorted(facts)]
    in_cap = [f for f in all_facts if f.event_count <= selection.cap]
    selected_ids = {p.patient_id for p in selection.patients}
    selected = [f for f in all_facts if f.patient_id in selected_ids]
    counts = [f.event_count for f in all_facts]

    lines: list[str] = [
        "# Gold panel feasibility scan",
        "",
        f"Descriptive scan of the {len(all_facts)}-patient synthetic (Synthea) panel at "
        f"as_of {selection.as_of} (measurement year {selection.as_of[:4]}), computed from the "
        "raw P6 record only: age at Dec 31 of the MY, sex, death on/before as_of, "
        "case-insensitive keyword flags over condition / procedure / medication display "
        "strings, and the presence of a few observation codes inside the MY. No value set and "
        "no engine verdict was consulted (SPEC section 6: selection on descriptive facts only).",
        "",
        f"Draw: n={selection.n}, seed={selection.seed}, cap={selection.cap} events; "
        f"selected {len(selection.patients)} ({selection.filler_count} filler).",
        "",
        "## Labeling-feasibility cap",
        "",
        f"Total events dated <= as_of (conditions + procedures + medications + observations + "
        f"encounters): min {min(counts)}, median {statistics.median(counts):.0f}, "
        f"max {max(counts)}. Patients over the cap of {selection.cap} are excluded from the "
        f"draw because a blind labeler cannot read them: **{len(selection.excluded_over_cap)} "
        f"excluded**, {len(in_cap)} eligible for the draw.",
        "",
        *_md_table(
            ("events <= as_of", "patients", "of which deceased"),
            [
                (
                    label,
                    sum(1 for f in all_facts if low <= f.event_count <= high),
                    sum(1 for f in all_facts if low <= f.event_count <= high and f.deceased),
                )
                for label, low, high in COUNT_BUCKETS
            ],
        ),
        "",
        "## Panel by age band and sex (alive vs deceased on/before as_of)",
        "",
    ]
    sexes = sorted({f.sex for f in all_facts})
    header = ["age at Dec 31", *sexes, "alive", "deceased", "total", "inside cap"]
    rows: list[list[Any]] = []
    for label, _low, _high in (*AGE_BANDS, ("unknown", 0, -1)):
        group = [f for f in all_facts if _band(f.age) == label]
        if not group:
            continue
        rows.append(
            [
                label,
                *[sum(1 for f in group if f.sex == sex) for sex in sexes],
                sum(1 for f in group if f.alive),
                sum(1 for f in group if f.deceased),
                len(group),
                sum(1 for f in group if f.event_count <= selection.cap),
            ]
        )
    rows.append(
        [
            "all",
            *[sum(1 for f in all_facts if f.sex == sex) for sex in sexes],
            sum(1 for f in all_facts if f.alive),
            sum(1 for f in all_facts if f.deceased),
            len(all_facts),
            len(in_cap),
        ]
    )
    lines.extend(_md_table(header, rows))

    lines += ["", "## Descriptive flags", ""]
    lines += ["Keyword patterns (case-insensitive) over display strings:", ""]
    lines.extend(f"- `{name}`: `{pattern}`" for name, pattern in KEYWORD_FLAGS.items())
    lines += [
        "- `statin_stopped`: a statin-keyword medication with status stopped / cancelled",
        f"- `bp_panel_in_my`: observation code {BP_PANEL_CODE} dated inside the MY",
        f"- `tobacco_screen_in_my`: observation code {TOBACCO_CODE} dated inside the MY",
        f"- `prapare_in_my`: observation code {PRAPARE_CODE} dated inside the MY",
        "- `encounter_in_my`: any encounter starting inside the MY",
        "- `inpatient_or_ed_in_my`: an encounter of class IMP or EMER starting inside the MY",
        "",
    ]
    flag_rows = []
    for name in FLAG_NAMES:
        flag_rows.append(
            [
                name,
                sum(1 for f in all_facts if getattr(f, name)),
                sum(1 for f in all_facts if getattr(f, name) and f.alive),
                sum(1 for f in in_cap if getattr(f, name)),
                sum(1 for f in selected if getattr(f, name)),
            ]
        )
    lines.extend(_md_table(("flag", "panel", "alive", "inside cap", "selected"), flag_rows))

    lines += ["", "## Strata (draw order = priority)", ""]
    strata_rows = [
        [
            name,
            r.target,
            r.minimum,
            r.available,
            r.available_over_cap,
            r.drawn,
            r.selected,
            r.shortfall or "",
            r.description,
        ]
        for name, r in selection.strata.items()
    ]
    filler_note = "seeded draw from the remaining pool to reach n"
    strata_rows.append(
        [FILLER, "", "", "", "", selection.filler_count, selection.filler_count, "", filler_note]
    )
    strata_header = (
        "stratum",
        "target",
        "minimum",
        "inside cap",
        "over cap",
        "drawn",
        "selected",
        "shortfall",
        "membership",
    )
    lines.extend(_md_table(strata_header, strata_rows))
    lines += ["", "## Shortfalls vs SPEC section 6 targets", ""]
    if selection.notes:
        lines.extend(f"- {note}" for note in selection.notes)
    else:
        lines.append("- none: every minimum was met inside the cap.")

    lines += ["", "## Selected patients", ""]
    sel_rows = [
        [
            p.patient_id,
            p.stratum,
            p.facts.age if p.facts.age is not None else "",
            p.facts.sex,
            "yes" if p.facts.deceased else "",
            p.facts.event_count,
            ", ".join(name for name in FLAG_NAMES if getattr(p.facts, name)),
        ]
        for p in selection.patients
    ]
    sel_header = ("patient_id", "drawn under", "age", "sex", "deceased", "events", "flags")
    lines.extend(_md_table(sel_header, sel_rows))
    lines += ["", "## Excluded over the cap", ""]
    if selection.excluded_over_cap:
        over_rows = [
            [
                f.patient_id,
                f.age if f.age is not None else "",
                f.sex,
                "yes" if f.deceased else "",
                f.event_count,
            ]
            for f in (facts[pid] for pid in selection.excluded_over_cap)
        ]
        lines.extend(_md_table(("patient_id", "age", "sex", "deceased", "events"), over_rows))
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def cmd_select(args: argparse.Namespace) -> int:
    as_of: date = args.as_of
    out: Path = args.out
    db: Path = args.db
    if not db.is_file():
        print(f"error: {db} is not a file", file=sys.stderr)
        return 2
    facts: dict[str, PatientFacts] = {}
    with _embedded_client(db, DEFAULT_SAMPLES) as client:
        ids = _all_patient_ids(client)
        if not ids:
            print("error: P6 has no patients", file=sys.stderr)
            return 1
        for pid in ids:
            record = _get_json(client, f"/v1/patients/{pid}/record", {"to": as_of.isoformat()})
            facts[pid] = facts_from_record(record, as_of)
    selection = select_patients(facts, as_of=as_of, n=args.n, seed=args.seed, cap=args.cap)
    write_text_lf(out / SELECTION_NAME, render_goldens_json(selection.to_json()))
    write_text_lf(out / FEASIBILITY_NAME, render_feasibility(facts, selection))
    counter = Counter(p.stratum for p in selection.patients)
    print(
        f"selected {len(selection.patients)} of {len(facts)} "
        f"({len(selection.excluded_over_cap)} over cap {args.cap}); "
        + ", ".join(f"{k}={v}" for k, v in sorted(counter.items()))
    )
    for note in selection.notes:
        print(f"shortfall: {note}", file=sys.stderr)
    print(f"wrote {out / SELECTION_NAME} and {out / FEASIBILITY_NAME}")
    return 0


# --- entry ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gold.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="write P6 snapshot fixtures for one as_of")
    snap.add_argument("--as-of", type=date.fromisoformat, required=True)
    snap.add_argument("--out", type=Path, default=DEFAULT_SNAPSHOTS)
    snap.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    snap.add_argument("--db", type=Path, default=None, help="existing P6 DuckDB (skips ingest)")
    snap.add_argument(
        "--patients-from", type=Path, default=None, help="SELECTION.json restricting the patients"
    )
    snap.set_defaults(func=cmd_snapshot)

    gold = sub.add_parser("goldens", help="check (default) or --regen engine goldens")
    gold.add_argument("--regen", action="store_true")
    gold.add_argument("--snapshots", type=Path, default=DEFAULT_SNAPSHOTS)
    gold.add_argument("--out", type=Path, default=DEFAULT_GOLDENS)
    gold.set_defaults(func=cmd_goldens)

    sel = sub.add_parser("select", help="blind stratified draw of the gold panel")
    sel.add_argument("--db", type=Path, required=True, help="existing P6 DuckDB of the panel")
    sel.add_argument("--as-of", type=date.fromisoformat, required=True)
    sel.add_argument("--n", type=int, default=DEFAULT_N)
    sel.add_argument("--seed", type=int, default=DEFAULT_SEED)
    sel.add_argument("--cap", type=int, default=DEFAULT_CAP)
    sel.add_argument("--out", type=Path, default=DEFAULT_GOLD_DIR)
    sel.set_defaults(func=cmd_select)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
