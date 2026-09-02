"""``HttpP6Client`` — ONE code path for both topologies.

Production/dev: an ``httpx.Client(base_url=...)`` against ``fhir-features serve``.
Embedded (quickstart, demo, contract tests): a Starlette ``TestClient`` over P6's ASGI app —
an ``httpx.Client`` subclass, so retry/error mapping and their tests are shared.
"""

import time
from collections.abc import Sequence
from datetime import date
from typing import Any

import httpx
from pydantic import ValidationError

from caregap.p6.client import (
    ENGINE_SECTIONS,
    P6ContractError,
    P6Unavailable,
    PatientNotFound,
    record_from_p6_payload,
)
from caregap.p6.models import FeatureRow, FeatureSchema, PatientPage, PatientRecord, ServiceInfo

_RETRY_DELAYS_S: tuple[float, ...] = (0.2, 0.6, 1.8)


class HttpP6Client:
    def __init__(self, client: httpx.Client, *, sleep: Any = time.sleep) -> None:
        self._client = client
        self._sleep = sleep

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET with retries on connection errors / 5xx only; 4xx are mapped, never retried."""
        last_error: Exception | None = None
        for delay in (*_RETRY_DELAYS_S, None):
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code == 404:
                    raise PatientNotFound(path)
                if 400 <= response.status_code < 500:
                    code = _problem_code(response)
                    raise P6ContractError(f"{path}: HTTP {response.status_code} ({code})")
                if response.status_code >= 500:
                    last_error = P6Unavailable(f"{path}: HTTP {response.status_code}")
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise P6ContractError(f"{path}: non-JSON body") from exc
            if delay is None:
                break
            self._sleep(delay)
        raise P6Unavailable(f"{path}: unavailable after {len(_RETRY_DELAYS_S) + 1} attempts") from (
            last_error
        )

    def healthz(self) -> ServiceInfo:
        return _validate(ServiceInfo, self._get("/healthz"))

    def features_schema(self) -> FeatureSchema:
        return _validate(FeatureSchema, self._get("/v1/features/schema"))

    def list_patients(self, *, limit: int, offset: int) -> PatientPage:
        return _validate(PatientPage, self._get("/v1/patients", {"limit": limit, "offset": offset}))

    def get_record(
        self, patient_id: str, *, to: date, sections: Sequence[str] = ENGINE_SECTIONS
    ) -> PatientRecord:
        payload = self._get(
            f"/v1/patients/{patient_id}/record",
            {"to": to.isoformat(), "sections": ",".join(sections)},
        )
        if not isinstance(payload, dict):
            raise P6ContractError("record payload is not an object")
        try:
            return record_from_p6_payload(payload, to)
        except ValidationError as exc:
            raise P6ContractError(
                f"record payload failed validation ({exc.error_count()})"
            ) from exc

    def get_features(self, patient_id: str, *, as_of: date) -> FeatureRow:
        payload = self._get(f"/v1/patients/{patient_id}/features", {"as_of": as_of.isoformat()})
        if not isinstance(payload, dict) or not isinstance(payload.get("features"), dict):
            raise P6ContractError("features payload lacks a features object")
        return _validate(FeatureRow, payload["features"])


def _problem_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "unknown"
    return str(body.get("code", "unknown")) if isinstance(body, dict) else "unknown"


def _validate[T](model: type[T], payload: Any) -> T:
    try:
        return model.model_validate(payload)  # type: ignore[attr-defined, no-any-return]
    except ValidationError as exc:
        raise P6ContractError(
            f"{getattr(model, '__name__', 'model')} failed validation ({exc.error_count()})"
        ) from exc
