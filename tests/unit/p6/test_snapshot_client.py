"""``SnapshotP6Client`` over a generated snapshot dir: MANIFEST + gz record + features."""

import gzip
import json
from datetime import date
from pathlib import Path

import pytest

from caregap.p6.client import P6ContractError, PatientNotFound
from caregap.p6.snapshot import SnapshotManifest, SnapshotP6Client
from tests.factories import (
    EVAL_AS_OF,
    bp_panel,
    build_record,
    condition,
    feature_row,
    patient,
    write_snapshot,
)

DEATH_AFTER_AS_OF = date(2026, 2, 1)


@pytest.fixture
def snapshot_dir(tmp_path: Path) -> Path:
    records = {
        # Listed out of order on purpose: list_patients must sort.
        "p2": build_record(
            patient_header=patient(patient_id="p2", sex="male", death_date=DEATH_AFTER_AS_OF),
            conditions=[condition("44054006", onset=date(2019, 4, 1))],
        ),
        "p1": build_record(
            patient_header=patient(patient_id="p1"),
            observations=bp_panel(effective=date(2025, 3, 15)),
        ),
    }
    return write_snapshot(
        tmp_path / "p6_snapshots",
        as_of=EVAL_AS_OF,
        records=records,
        features={"p1": feature_row("p1", EVAL_AS_OF, latest_sbp=128.0)},
    )


@pytest.fixture
def client(snapshot_dir: Path) -> SnapshotP6Client:
    return SnapshotP6Client(snapshot_dir)


def test_layout_is_manifest_plus_gz_record_plus_features(snapshot_dir: Path) -> None:
    assert (snapshot_dir / "MANIFEST.json").is_file()
    assert (snapshot_dir / "p1" / "record_2025-12-31.json.gz").is_file()
    assert (snapshot_dir / "p1" / "features_2025-12-31.json").is_file()
    assert not (snapshot_dir / "p2" / "features_2025-12-31.json").exists()


def test_manifest_and_service_info(client: SnapshotP6Client) -> None:
    assert client.source == "snapshot"
    assert isinstance(client.manifest, SnapshotManifest)
    assert client.manifest.patient_ids == ["p2", "p1"]
    info = client.healthz()
    assert (info.service_version, info.schema_version, info.feature_version) == (
        "0.1.0",
        3,
        "2026.08",
    )
    schema = client.features_schema()
    assert (schema.feature_version, schema.valuesets_version) == ("2026.08", "2026.08")


def test_missing_manifest_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"MANIFEST\.json"):
        SnapshotP6Client(tmp_path / "nowhere")


def test_manifest_ignores_extra_keys_and_defaults_schema_version(tmp_path: Path) -> None:
    root = tmp_path / "snap"
    root.mkdir()
    (root / "MANIFEST.json").write_text(
        json.dumps(
            {
                "service_version": "9.9.9",
                "feature_version": "f",
                "valuesets_version": "v",
                "patient_ids": [],
                "extra": "ignored",
            }
        ),
        encoding="utf-8",
    )
    client = SnapshotP6Client(root)
    assert client.healthz().schema_version == 3
    assert client.list_patients(limit=10, offset=0).total == 0


def test_list_patients_sorted_paged_with_deceased_flag(client: SnapshotP6Client) -> None:
    page = client.list_patients(limit=10, offset=0)
    assert page.total == 2
    assert [(s.patient_id, s.sex, s.deceased, s.source) for s in page.items] == [
        ("p1", "female", False, "snapshot"),
        ("p2", "male", True, "snapshot"),
    ]
    assert page.items[0].birth_date == date(1960, 6, 15)
    second = client.list_patients(limit=1, offset=1)
    assert [s.patient_id for s in second.items] == ["p2"]
    assert second.total == 2
    assert client.list_patients(limit=1, offset=5).items == []


def test_list_patients_with_a_manifest_id_lacking_files_raises_not_found(
    tmp_path: Path,
) -> None:
    root = write_snapshot(
        tmp_path / "snap",
        as_of=EVAL_AS_OF,
        records={"p1": build_record()},
        patient_ids=["p1", "ghost"],
    )
    with pytest.raises(PatientNotFound, match="ghost"):
        SnapshotP6Client(root).list_patients(limit=10, offset=0)


def test_get_record_is_masked_and_boundary_projected(client: SnapshotP6Client) -> None:
    record = client.get_record("p2", to=EVAL_AS_OF)
    assert record.patient.patient_id == "p2"
    assert record.patient.death_date is None  # died after as_of -> hidden
    assert record.as_of == EVAL_AS_OF
    assert not hasattr(record.patient, "race")
    assert [c.code for c in record.conditions] == ["44054006"]
    p1 = client.get_record("p1", to=EVAL_AS_OF, sections=("observations",))
    assert [o.code for o in p1.observations] == ["85354-9", "8480-6", "8462-4"]
    assert p1.observations[1].parent_observation_id == "o1"


def test_get_record_unknown_patient_or_as_of_raises_not_found(client: SnapshotP6Client) -> None:
    with pytest.raises(PatientNotFound, match="p9@2025-12-31"):
        client.get_record("p9", to=EVAL_AS_OF)
    with pytest.raises(PatientNotFound, match="p1@2024-12-31"):
        client.get_record("p1", to=date(2024, 12, 31))


def test_get_features_wrapped_and_bare_payloads(
    client: SnapshotP6Client, snapshot_dir: Path
) -> None:
    row = client.get_features("p1", as_of=EVAL_AS_OF)
    assert (row.patient_id, row.as_of, row.latest_sbp, row.has_hypertension) == (
        "p1",
        EVAL_AS_OF,
        128.0,
        True,
    )
    # A bare feature row (no {"features": ...} wrapper) is accepted too.
    bare = snapshot_dir / "p2" / "features_2025-12-31.json"
    bare.write_text(json.dumps(feature_row("p2", EVAL_AS_OF, sex="male")), encoding="utf-8")
    assert client.get_features("p2", as_of=EVAL_AS_OF).sex == "male"


def test_get_features_missing_raises_not_found(client: SnapshotP6Client) -> None:
    with pytest.raises(PatientNotFound, match="p2@2025-12-31"):
        client.get_features("p2", as_of=EVAL_AS_OF)
    with pytest.raises(PatientNotFound):
        client.get_features("p1", as_of=date(2024, 12, 31))


def test_corrupt_snapshot_files_are_contract_errors(
    client: SnapshotP6Client, snapshot_dir: Path
) -> None:
    record_path = snapshot_dir / "p1" / "record_2025-12-31.json.gz"
    with gzip.open(record_path, "wt", encoding="utf-8") as fh:
        json.dump([], fh)
    with pytest.raises(P6ContractError, match="not an object"):
        client.get_record("p1", to=EVAL_AS_OF)
    with gzip.open(record_path, "wt", encoding="utf-8") as fh:
        json.dump({"patient": {"patient_id": "p1"}, "conditions": [{"condition_id": "c1"}]}, fh)
    with pytest.raises(P6ContractError, match="failed validation"):
        client.get_record("p1", to=EVAL_AS_OF)
    features_path = snapshot_dir / "p1" / "features_2025-12-31.json"
    features_path.write_text(json.dumps({"features": {"as_of": "2025-12-31"}}), encoding="utf-8")
    with pytest.raises(P6ContractError, match="features failed validation"):
        client.get_features("p1", as_of=EVAL_AS_OF)
