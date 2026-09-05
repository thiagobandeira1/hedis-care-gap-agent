"""Builders shared by the eval tests: gold cases whose split follows ``sha256(patient_id)``,
outcome rows, a hand-written gold set over the committed personas (labels chosen to yield
known test-split counts), and snapshot-mode ``Settings`` that never read ``.env``."""

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from caregap.config import Settings
from caregap.evals.gold import GoldCase, GoldStatus, expected_split, gold_item_id
from caregap.evals.outcomes import PatientOutcomeRow, Tier
from caregap.graph.state import RunStatus
from caregap.measures.ids import MeasureId

REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_DIR = REPO_ROOT / "synthetic" / "p6_snapshots"
AS_OF = date(2025, 12, 31)

TONY = "939eea26-a679-2564-5cf9-c0fd557beefc"
"""test split; engine: CBP closed, EED + COL gap_open, SPD closed, BCS/SPC not_eligible."""
MEREDITH = "1c1e0add-1be9-8194-109d-981ffd0adade"
"""test split; engine: BCS gap_open, COL closed, the rest not_eligible."""
KAYCE = "009969ab-f1b8-a2c0-9fb7-f0621d7beea8"
"""dev split; deceased 1954: not_eligible everywhere."""
CLARA = "3f8ef968-320b-65da-5011-1a387d4ed53f"
"""dev split; no core candidate (TSC / SNS closed)."""
SHERYL = "ec26a105-cdda-e9a0-683d-1c7662a370ea"
"""dev split; no core candidate."""
PERSONAS = (KAYCE, MEREDITH, CLARA, TONY, SHERYL)

CORE: tuple[MeasureId, ...] = ("CBP", "EED", "BCS", "COL", "SPC", "SPD")


def gold(
    patient_id: str,
    measure_id: MeasureId,
    status: GoldStatus,
    *,
    as_of: date = AS_OF,
    rationale: str = "hand-applied demo rule",
    labeler: str = "tester",
    uncertain: bool = False,
) -> GoldCase:
    return GoldCase(
        item_id=gold_item_id(patient_id, measure_id),
        category=measure_id,
        patient_id=patient_id,
        measure_id=measure_id,
        as_of=as_of,
        gold=status,
        rationale=rationale,
        labeler=labeler,
        uncertain=uncertain,
        split=expected_split(patient_id),
    )


def gold_row(case: GoldCase) -> dict[str, Any]:
    """The committed JSONL shape: no ``item_id`` / ``category`` (the loader derives them)."""
    return {
        "patient_id": case.patient_id,
        "measure_id": case.measure_id,
        "as_of": case.as_of.isoformat(),
        "gold": case.gold,
        "rationale": case.rationale,
        "decisive_dates": list(case.decisive_dates),
        "uncertain": case.uncertain,
        "labeler": case.labeler,
        "split": case.split,
    }


def write_gold(path: Path, rows: Iterable[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return path


def synthetic_patient(split: str, index: int) -> str:
    """The ``index``-th synthetic patient id whose hash-derived split is ``split``."""
    seen = 0
    for n in range(10_000):
        candidate = f"synthetic-{n:04d}"
        if expected_split(candidate) == split:
            if seen == index:
                return candidate
            seen += 1
    raise AssertionError("unreachable")


def outcome(
    patient_id: str,
    *,
    final: Mapping[str, str] | None = None,
    engine: Mapping[str, str] | None = None,
    decisions: Mapping[str, str] | None = None,
    flagged: Sequence[str] = (),
    unverified: Sequence[str] = (),
    error: bool = False,
    status: RunStatus = "completed",
    tier: Tier = "engine",
    as_of: date = AS_OF,
    fallback_count: int = 0,
) -> PatientOutcomeRow:
    final_statuses = dict(final or {})
    return PatientOutcomeRow(
        patient_id=patient_id,
        as_of=as_of,
        tier=tier,
        status="error" if error else status,
        engine_verdicts=dict(engine if engine is not None else final_statuses),
        final_statuses=final_statuses,
        validator_decisions=dict(decisions or {}),
        review_flagged=sorted(flagged),
        error=error,
        fallback_count=fallback_count,
        unverified=sorted(unverified),
    )


def committed_gold() -> list[GoldCase]:
    """Labels over the committed personas at 2025-12-31 with deliberate disagreements:
    test split -> TP 3 (Tony EED + COL, Meredith BCS), FP 0, FN 1 (Meredith COL labeled open
    while the engine says closed); one dev-split ``escalate`` the engine never flags."""
    tony: dict[MeasureId, GoldStatus] = {
        "CBP": "closed",
        "EED": "open",
        "BCS": "not_eligible",
        "COL": "open",
        "SPC": "not_eligible",
        "SPD": "closed",
        "TSC": "closed",
        "SNS": "closed",
    }
    meredith: dict[MeasureId, GoldStatus] = {
        "CBP": "not_eligible",
        "EED": "not_eligible",
        "BCS": "open",
        "COL": "open",
        "SPC": "not_eligible",
        "SPD": "not_eligible",
    }
    cases = [gold(TONY, m, s) for m, s in tony.items()]
    cases += [gold(MEREDITH, m, s) for m, s in meredith.items()]
    for patient_id in (KAYCE, CLARA, SHERYL):
        for measure in CORE:
            status: GoldStatus = (
                "escalate" if (patient_id, measure) == (CLARA, "CBP") else "not_eligible"
            )
            cases.append(gold(patient_id, measure, status))
    return cases


def snapshot_settings(tmp_path: Path, tag: str, **overrides: Any) -> Settings:
    """Snapshot P6 over the committed personas, fake models, every store under ``tmp_path``;
    ``_env_file=None`` so a developer's ``.env`` can never leak a key into the suite."""
    values: dict[str, Any] = {
        "p6_mode": "snapshot",
        "snapshot_dir": SNAPSHOT_DIR,
        "models": "fake",
        "checkpoint_path": tmp_path / f"ckpt-{tag}.sqlite",
        "runstore_path": tmp_path / f"rs-{tag}.sqlite",
        "recordings_dir": tmp_path / "recorded",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)
