"""Engine goldens over the committed P6 snapshots (SPEC section 9).

For every vendored persona at both anchors (``2025-12-31`` eval anchor, ``2024-12-31`` prior
MY) the default engine's ``list[MeasureEvaluation]`` must be BYTE-identical to
``tests/unit/measures/goldens/<pid>_<as_of>.json``. Regenerate deliberately with::

    uv run python scripts/gold.py goldens --regen

The serialisation contract (sorted keys, indent 2, LF, trailing newline) is duplicated here on
purpose: the test must not import the script it is guarding.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from caregap.measures.context import MeasurementContext
from caregap.measures.engine import default_engine
from caregap.measures.ids import ALL_MEASURES
from caregap.p6.snapshot import SnapshotP6Client

REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_DIR = REPO_ROOT / "synthetic" / "p6_snapshots"
GOLDEN_DIR = Path(__file__).resolve().parent / "goldens"
ANCHORS: tuple[date, ...] = (date(2025, 12, 31), date(2024, 12, 31))

PERSONAS: dict[str, str] = {
    "Tony": "939eea26-a679-2564-5cf9-c0fd557beefc",
    "Meredith": "1c1e0add-1be9-8194-109d-981ffd0adade",
    "Sheryl": "ec26a105-cdda-e9a0-683d-1c7662a370ea",
    "Chet": "3f8ef968-320b-65da-5011-1a387d4ed53f",
    "Kayce": "009969ab-f1b8-a2c0-9fb7-f0621d7beea8",
}


def render(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def snapshot_client() -> SnapshotP6Client:
    if not (SNAPSHOT_DIR / "MANIFEST.json").is_file():
        pytest.fail(
            f"missing P6 snapshot fixture at {SNAPSHOT_DIR}; build it with "
            "`uv run python scripts/gold.py snapshot --as-of 2025-12-31` and "
            "`... --as-of 2024-12-31`"
        )
    return SnapshotP6Client(SNAPSHOT_DIR)


def fresh_golden(client: SnapshotP6Client, patient_id: str, as_of: date) -> str:
    record = client.get_record(patient_id, to=as_of)
    ctx = MeasurementContext.for_(as_of, record.patient.birth_date)
    evaluations = default_engine().evaluate(record, ctx)
    assert [e.measure_id for e in evaluations] == list(ALL_MEASURES)
    return render([e.model_dump(mode="json") for e in evaluations])


_CASES = [
    pytest.param(pid, as_of, id=f"{name}@{as_of.isoformat()}")
    for name, pid in PERSONAS.items()
    for as_of in ANCHORS
]


@pytest.mark.parametrize(("patient_id", "as_of"), _CASES)
def test_engine_matches_committed_golden_bytes(patient_id: str, as_of: date) -> None:
    client = snapshot_client()
    path = GOLDEN_DIR / f"{patient_id}_{as_of.isoformat()}.json"
    assert path.is_file(), f"missing golden {path.name}; run scripts/gold.py goldens --regen"
    committed = path.read_bytes()
    assert b"\r\n" not in committed, "goldens are LF-only"
    fresh = fresh_golden(client, patient_id, as_of).encode("utf-8")
    assert fresh == committed, (
        f"engine output drifted from {path.name}; if intended, regenerate with "
        "`uv run python scripts/gold.py goldens --regen`"
    )


def test_manifest_lists_exactly_the_five_personas() -> None:
    client = snapshot_client()
    assert sorted(client.manifest.patient_ids) == sorted(PERSONAS.values())
    assert client.manifest.valuesets_version == "2026.08"


def test_every_golden_file_belongs_to_a_persona_and_anchor() -> None:
    expected = {f"{pid}_{as_of.isoformat()}.json" for pid in PERSONAS.values() for as_of in ANCHORS}
    present = {p.name for p in GOLDEN_DIR.glob("*.json")}
    assert present == expected
