"""``SnapshotP6Client`` — committed P6 responses; what unit, graph, and eval tests run on.

Layout: ``<dir>/MANIFEST.json`` plus ``<dir>/<patient_id>/record_<as_of>.json.gz`` and
``features_<as_of>.json``. Zero DuckDB, zero network.
"""

import gzip
import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from caregap.p6.client import (
    ENGINE_SECTIONS,
    P6ContractError,
    PatientNotFound,
    record_from_p6_payload,
)
from caregap.p6.models import (
    FeatureRow,
    FeatureSchema,
    PatientPage,
    PatientRecord,
    PatientSummary,
    ServiceInfo,
)


class SnapshotManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    service_version: str
    schema_version: int = 3
    feature_version: str
    valuesets_version: str
    generated_from_sha: str = ""
    command: str = ""
    patient_ids: list[str]


class SnapshotP6Client:
    source: str = "snapshot"

    def __init__(self, snapshot_dir: Path) -> None:
        self._dir = snapshot_dir
        manifest_path = snapshot_dir / "MANIFEST.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"no snapshot manifest at {manifest_path}")
        self.manifest = SnapshotManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )

    def healthz(self) -> ServiceInfo:
        return ServiceInfo(
            service_version=self.manifest.service_version,
            schema_version=self.manifest.schema_version,
            feature_version=self.manifest.feature_version,
        )

    def features_schema(self) -> FeatureSchema:
        return FeatureSchema(
            feature_version=self.manifest.feature_version,
            valuesets_version=self.manifest.valuesets_version,
        )

    def list_patients(self, *, limit: int, offset: int) -> PatientPage:
        ids = sorted(self.manifest.patient_ids)
        page = ids[offset : offset + limit]
        items = []
        for pid in page:
            header = self._read_record_payload(pid, self._latest_as_of(pid)).get("patient", {})
            items.append(
                PatientSummary(
                    patient_id=pid,
                    source="snapshot",
                    birth_date=header.get("birth_date"),
                    sex=header.get("sex", "unknown"),
                    deceased=header.get("death_date") is not None,
                )
            )
        return PatientPage(items=items, total=len(ids))

    def get_record(
        self, patient_id: str, *, to: date, sections: Sequence[str] = ENGINE_SECTIONS
    ) -> PatientRecord:
        payload = self._read_record_payload(patient_id, to)
        try:
            return record_from_p6_payload(payload, to)
        except ValidationError as exc:
            raise P6ContractError(
                f"snapshot record failed validation ({exc.error_count()})"
            ) from exc

    def get_features(self, patient_id: str, *, as_of: date) -> FeatureRow:
        path = self._dir / patient_id / f"features_{as_of.isoformat()}.json"
        if not path.exists():
            raise PatientNotFound(f"{patient_id}@{as_of}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        features = payload.get("features", payload) if isinstance(payload, dict) else payload
        try:
            return FeatureRow.model_validate(features)
        except ValidationError as exc:
            raise P6ContractError(
                f"snapshot features failed validation ({exc.error_count()})"
            ) from exc

    # -- helpers -------------------------------------------------------------------------

    def _read_record_payload(self, patient_id: str, as_of: date) -> dict[str, Any]:
        path = self._dir / patient_id / f"record_{as_of.isoformat()}.json.gz"
        if not path.exists():
            raise PatientNotFound(f"{patient_id}@{as_of}")
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            raise P6ContractError("snapshot record is not an object")
        return payload

    def _latest_as_of(self, patient_id: str) -> date:
        files = sorted((self._dir / patient_id).glob("record_*.json.gz"))
        if not files:
            raise PatientNotFound(patient_id)
        return date.fromisoformat(files[-1].name[len("record_") : -len(".json.gz")])
