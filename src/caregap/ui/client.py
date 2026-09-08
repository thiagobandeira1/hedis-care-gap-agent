"""``ApiClient``: the UI's only door to data — one typed method per API route (SPEC section 7).

Every non-2xx response becomes an :class:`ApiError` built from the RFC 9457 problem+json body
(``status``, ``title``, ``detail`` and the validation ``errors`` list a 422 carries); transport
failures become ``ApiError(status=0)``; a 2xx body that does not fit the wire model is an
``ApiError`` too, so the pages handle exactly one exception type. The wire models mirror
``api/schemas.py`` field-for-field but live here so the UI depends on the HTTP contract alone,
never on the API process.
"""

import uuid
from collections.abc import Sequence
from datetime import date
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from caregap.agents.schemas import CareActionPlan, ValidationVerdict
from caregap.graph.runstore import (
    ApprovalRecord,
    ApprovalStatus,
    OutboxEntry,
    PatientRunRecord,
    RunRecord,
)
from caregap.graph.state import (
    AgentError,
    ApprovalDecision,
    ApprovalRequest,
    MeasurementSummary,
    NodeTrace,
    RunOptions,
    RunOutcome,
)
from caregap.measures.models import MeasureEvaluation, OpenGap, ReviewItem

DEFAULT_BASE_URL = "http://127.0.0.1:8010"
DEFAULT_TIMEOUT_S = 30.0
PROBLEM_JSON = "application/problem+json"


class ApiError(Exception):
    """One error type for the pages. ``status == 0`` means the API could not be reached."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str = "",
        *,
        errors: Sequence[str] = (),
        problem_type: str = "about:blank",
    ) -> None:
        message = f"HTTP {status}: {title}" if status else title
        if detail:
            message = f"{message} - {detail}"
        super().__init__(message)
        self.status = status
        self.title = title
        self.detail = detail
        self.errors = list(errors)
        self.problem_type = problem_type

    @property
    def unreachable(self) -> bool:
        return self.status == 0


def new_decision_id() -> str:
    """Client-generated idempotency key for an ``ApprovalDecision`` (uuid4)."""
    return str(uuid.uuid4())


# --- wire models (mirror api/schemas.py; unknown fields are ignored) ----------------------


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class P6Info(_Wire):
    service_version: str = ""
    schema_version: int | None = None
    feature_version: str = ""


class HealthInfo(_Wire):
    status: str
    p6: P6Info = Field(default_factory=P6Info)
    models_mode: str = ""
    prompt_version: str = ""
    engine_measures: list[str] = Field(default_factory=list)


class ElementCounts(_Wire):
    quoted: int = 0
    demo_choice: int = 0
    not_representable: int = 0


class MeasureInfo(_Wire):
    measure_id: str
    name: str
    star_id: str | None = None
    rule_version: str = ""
    conformance: str = ""
    element_counts: ElementCounts = Field(default_factory=ElementCounts)


class LastRun(_Wire):
    run_id: str
    status: str


class PanelItem(_Wire):
    patient_id: str
    sex: str = "unknown"
    birth_date: date | None = None
    deceased: bool = False
    last_run: LastRun | None = None


class PanelPage(_Wire):
    items: list[PanelItem] = Field(default_factory=list)
    total: int = 0


class PatientGaps(_Wire):
    patient_id: str
    as_of: date
    context: MeasurementSummary
    evaluations: list[MeasureEvaluation] = Field(default_factory=list)


class RunCreated(_Wire):
    run_id: str
    status: str


class RunDetail(_Wire):
    run: RunRecord
    patients: list[PatientRunRecord] = Field(default_factory=list)


class PatientState(_Wire):
    """The ``graph.get_state`` projection the API exposes; every field optional because a
    thread that failed early carries only some of them."""

    evaluations: list[MeasureEvaluation] = Field(default_factory=list)
    verdicts: list[ValidationVerdict] = Field(default_factory=list)
    open_gaps: list[OpenGap] = Field(default_factory=list)
    review_items: list[ReviewItem] = Field(default_factory=list)
    plan: CareActionPlan | None = None
    draft_error: str | None = None
    revision_count: int = 0
    trace: list[NodeTrace] = Field(default_factory=list)
    agent_errors: list[AgentError] = Field(default_factory=list)


class PatientRunDetail(_Wire):
    patient_run: PatientRunRecord
    pending: ApprovalRequest | None = None
    state: PatientState | None = None


class DecisionResult(_Wire):
    result: RunOutcome | ApprovalRequest
    replayed: bool = False


# --- problem+json ---------------------------------------------------------------------------


def _error_text(item: object) -> str:
    """Flatten one entry of an ``errors`` list: plain strings pass through, FastAPI-style
    ``{loc, msg}`` objects become ``loc: msg``."""
    if isinstance(item, dict):
        loc = item.get("loc")
        msg = item.get("msg") or item.get("detail") or item.get("message")
        if msg is not None:
            prefix = ".".join(str(part) for part in loc) if isinstance(loc, list) else ""
            return f"{prefix}: {msg}" if prefix else str(msg)
    return str(item)


def problem_from_response(response: httpx.Response) -> ApiError:
    """Build an ``ApiError`` from any non-2xx response; tolerates non-problem bodies."""
    status = response.status_code
    title = response.reason_phrase or f"HTTP {status}"
    detail = ""
    errors: list[str] = []
    problem_type = "about:blank"
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        title = str(body.get("title") or title)
        problem_type = str(body.get("type") or problem_type)
        raw_detail = body.get("detail")
        if isinstance(raw_detail, list):
            errors = [_error_text(item) for item in raw_detail]
            detail = "; ".join(errors)
        elif raw_detail is not None:
            detail = str(raw_detail)
        for key in ("errors", "invalid_params"):
            raw_errors = body.get(key)
            if isinstance(raw_errors, list):
                errors = [_error_text(item) for item in raw_errors]
                break
        raw_status = body.get("status")
        if isinstance(raw_status, int):
            status = raw_status
    elif response.text.strip():
        # Never echo a non-problem body (an arbitrary origin's HTML/text would render in the
        # console): report only that one was received and how large it was.
        detail = f"HTTP {status}: non-problem response body ({len(response.text)} chars)"
    return ApiError(status, title, detail, errors=errors, problem_type=problem_type)


# --- client ---------------------------------------------------------------------------------


class ApiClient:
    """Sync ``httpx`` client over the caregap API. Owns its ``httpx.Client`` unless one is
    injected (tests); ``close()`` / context manager release it."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- transport ---------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        try:
            response = self._client.request(method, path, params=params, json=json)
        except httpx.TransportError as exc:
            raise ApiError(0, "API unreachable", f"{method} {path}: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise problem_from_response(response)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(
                response.status_code, "Invalid response", f"{method} {path}: non-JSON body"
            ) from exc

    @staticmethod
    def _parse[T: BaseModel](model: type[T], payload: Any, path: str) -> T:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise ApiError(
                200,
                "Invalid response",
                f"{path}: {model.__name__} failed validation ({exc.error_count()} errors)",
            ) from exc

    @classmethod
    def _parse_list[T: BaseModel](cls, model: type[T], payload: Any, path: str) -> list[T]:
        if not isinstance(payload, list):
            raise ApiError(200, "Invalid response", f"{path}: expected a JSON array")
        return [cls._parse(model, item, path) for item in payload]

    # -- routes ------------------------------------------------------------------------------

    def healthz(self) -> HealthInfo:
        path = "/healthz"
        return self._parse(HealthInfo, self._request("GET", path), path)

    def list_measures(self) -> list[MeasureInfo]:
        path = "/v1/measures"
        return self._parse_list(MeasureInfo, self._request("GET", path), path)

    def panel(self, as_of: date, *, limit: int = 50, offset: int = 0) -> PanelPage:
        path = "/v1/panel"
        params = {"as_of": as_of.isoformat(), "limit": limit, "offset": offset}
        return self._parse(PanelPage, self._request("GET", path, params=params), path)

    def panel_all(
        self, as_of: date, *, page_size: int = 50, max_pages: int = 20
    ) -> list[PanelItem]:
        """Every panel row, following ``total`` across pages (bounded by ``max_pages``)."""
        items: list[PanelItem] = []
        offset = 0
        for _ in range(max_pages):
            page = self.panel(as_of, limit=page_size, offset=offset)
            items.extend(page.items)
            offset += len(page.items)
            if not page.items or offset >= page.total:
                break
        return items

    def patient_gaps(self, patient_id: str, as_of: date) -> PatientGaps:
        path = f"/v1/patients/{_segment(patient_id)}/gaps"
        params = {"as_of": as_of.isoformat()}
        return self._parse(PatientGaps, self._request("GET", path, params=params), path)

    def start_run(
        self,
        as_of: date,
        patient_ids: Sequence[str],
        options: RunOptions | None = None,
    ) -> RunCreated:
        path = "/v1/runs"
        body = {
            "as_of": as_of.isoformat(),
            "patient_ids": list(patient_ids),
            "options": (options or RunOptions()).model_dump(mode="json"),
        }
        return self._parse(RunCreated, self._request("POST", path, json=body), path)

    def get_run(self, run_id: str) -> RunDetail:
        path = f"/v1/runs/{_segment(run_id)}"
        return self._parse(RunDetail, self._request("GET", path), path)

    def cancel_run(self, run_id: str) -> RunCreated:
        path = f"/v1/runs/{_segment(run_id)}/cancel"
        return self._parse(RunCreated, self._request("POST", path), path)

    def patient_run(self, run_id: str, patient_id: str) -> PatientRunDetail:
        path = f"/v1/runs/{_segment(run_id)}/patients/{_segment(patient_id)}"
        return self._parse(PatientRunDetail, self._request("GET", path), path)

    def decide(self, run_id: str, patient_id: str, decision: ApprovalDecision) -> DecisionResult:
        path = f"/v1/runs/{_segment(run_id)}/patients/{_segment(patient_id)}/decision"
        body = decision.model_dump(mode="json")
        return self._parse(DecisionResult, self._request("POST", path, json=body), path)

    def approvals(self, status: ApprovalStatus | None = None) -> list[ApprovalRecord]:
        path = "/v1/approvals"
        params = None if status is None else {"status": status}
        return self._parse_list(ApprovalRecord, self._request("GET", path, params=params), path)

    def outbox(self, run_id: str | None = None) -> list[OutboxEntry]:
        path = "/v1/outbox"
        params = None if run_id is None else {"run_id": run_id}
        return self._parse_list(OutboxEntry, self._request("GET", path, params=params), path)


def _segment(value: str) -> str:
    return quote(value, safe="")
