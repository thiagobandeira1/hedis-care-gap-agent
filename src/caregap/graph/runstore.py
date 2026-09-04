"""``RunStore``: the sqlite ledger behind the API and UI (SPEC section 7).

Four tables — ``runs``, ``patient_runs``, ``decisions`` (UNIQUE on ``decision_id``) and
``outbox`` (UNIQUE on ``(thread_id, action_id)``) — so an approval decision can be replayed
and a resumed or repeated ``finalize`` never double-appends an action.

One connection per store, every read and write under one lock, WAL when file-backed,
foreign keys on. Timestamps are ISO-8601 UTC strings from an injectable clock so tests are
deterministic. Only ids, statuses and the frozen graph payloads are stored: nothing from a
patient record beyond what those payloads already carry (codes, dates, event ids).
"""

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from caregap.graph.state import ApprovalDecision, ApprovalRequest, RunOptions, RunOutcome

ApprovalStatus = Literal["pending", "superseded", "resolved"]
Clock = Callable[[], datetime]

MEMORY = ":memory:"
"""Pass as ``path`` for a private in-memory store (tests, CLI, evals)."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    as_of             TEXT NOT NULL,
    status            TEXT NOT NULL,
    options_json      TEXT NOT NULL,
    patient_ids_json  TEXT NOT NULL,
    cursor            INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patient_runs (
    run_id         TEXT NOT NULL REFERENCES runs(run_id),
    patient_id     TEXT NOT NULL,
    status         TEXT NOT NULL,
    outcome_json   TEXT,
    pending_json   TEXT,
    superseded_by  TEXT,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, patient_id)
);
CREATE INDEX IF NOT EXISTS ix_patient_runs_patient ON patient_runs(patient_id);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id   TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    patient_id    TEXT NOT NULL,
    action        TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    result_json   TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id     TEXT NOT NULL,
    action_id     TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    patient_id    TEXT NOT NULL,
    measure_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    detail        TEXT NOT NULL,
    owner         TEXT NOT NULL,
    approval_ref  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (thread_id, action_id)
);
CREATE INDEX IF NOT EXISTS ix_outbox_run ON outbox(run_id);
"""

_RESULT_KINDS: dict[type[BaseModel], str] = {
    RunOutcome: "run_outcome",
    ApprovalRequest: "approval_request",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_timestamp(moment: datetime) -> str:
    """ISO-8601 UTC with millisecond precision and a ``Z`` suffix; naive input is taken as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunStoreError(Exception):
    """Base class for ledger errors."""


class DuplicateRunError(RunStoreError):
    """``create_run`` for a ``run_id`` that already exists."""


class UnknownRunError(RunStoreError):
    """A write that references a ``run_id`` never created (foreign keys are enforced)."""


class RunRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    as_of: date
    status: str
    options: RunOptions
    patient_ids: list[str]
    cursor: int
    created_at: str
    updated_at: str


class PatientRunRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    patient_id: str
    status: str
    outcome: RunOutcome | None = None
    pending: ApprovalRequest | None = None
    """The latest approval request emitted for this thread (kept once an outcome resolves it)."""
    superseded_by: str | None = None
    updated_at: str


class DecisionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: str
    run_id: str
    patient_id: str
    action: str
    payload: ApprovalDecision
    result: RunOutcome | ApprovalRequest
    """What the resume produced — replayed verbatim for a duplicate ``decision_id``."""
    created_at: str


class OutboxEntry(BaseModel):
    """One approved action. ``approval_ref`` is the decision id that authorised it — required,
    so an entry can never be built without one (SPEC section 3: finalize is the only writer)."""

    model_config = ConfigDict(frozen=True)

    thread_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    run_id: str
    patient_id: str
    measure_id: str
    kind: str
    detail: str
    owner: str
    approval_ref: str = Field(min_length=1)
    created_at: str = ""
    """Stamped by the sink when empty."""


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    patient_id: str
    status: ApprovalStatus
    request: ApprovalRequest
    superseded_by: str | None = None


def _dump_result(result: RunOutcome | ApprovalRequest) -> str:
    kind = _RESULT_KINDS[type(result)]
    return json.dumps(
        {"kind": kind, "value": result.model_dump(mode="json")},
        separators=(",", ":"),
        sort_keys=True,
    )


def _load_result(text: str) -> RunOutcome | ApprovalRequest:
    payload = json.loads(text)
    kind = payload["kind"]
    if kind == "run_outcome":
        return RunOutcome.model_validate(payload["value"])
    if kind == "approval_request":
        return ApprovalRequest.model_validate(payload["value"])
    raise RunStoreError(f"unknown decision result kind {kind!r}")


def _approval_status(outcome_json: str | None, superseded_by: str | None) -> ApprovalStatus:
    if outcome_json is not None:
        return "resolved"
    if superseded_by is not None:
        return "superseded"
    return "pending"


class RunStore:
    """sqlite3 stdlib, one connection (``check_same_thread=False``), one lock, WAL when
    file-backed. Safe to share across the API's background loop and its request threads."""

    def __init__(self, path: Path | str = MEMORY, *, clock: Clock = utc_now) -> None:
        self._path = str(path)
        self._clock = clock
        self._lock = threading.Lock()
        if self._path != MEMORY:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self._path != MEMORY:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _stamp(self) -> str:
        return format_timestamp(self._clock())

    # -- runs --------------------------------------------------------------------------------

    def create_run(
        self,
        run_id: str,
        as_of: date,
        options: RunOptions,
        patient_ids: list[str],
        *,
        status: str = "created",
    ) -> RunRecord:
        now = self._stamp()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, as_of, status, options_json, "
                "patient_ids_json, cursor, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    run_id,
                    as_of.isoformat(),
                    status,
                    options.model_dump_json(),
                    json.dumps(list(patient_ids)),
                    now,
                    now,
                ),
            )
            if cur.rowcount != 1:
                raise DuplicateRunError(f"run {run_id!r} already exists")
        return RunRecord(
            run_id=run_id,
            as_of=as_of,
            status=status,
            options=options,
            patient_ids=list(patient_ids),
            cursor=0,
            created_at=now,
            updated_at=now,
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return None if row is None else _run_from_row(row)

    def update_run_status(self, run_id: str, status: str, cursor: int | None = None) -> None:
        """Set the status and, when given, the panel cursor (index of the next patient)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE runs SET status = ?, cursor = COALESCE(?, cursor), updated_at = ? "
                "WHERE run_id = ?",
                (status, cursor, self._stamp(), run_id),
            )
            if cur.rowcount != 1:
                raise UnknownRunError(f"run {run_id!r} does not exist; call create_run first")

    # -- patient runs ------------------------------------------------------------------------

    def upsert_patient_run(
        self,
        run_id: str,
        patient_id: str,
        status: str,
        outcome: RunOutcome | None,
        pending: ApprovalRequest | None,
    ) -> PatientRunRecord:
        """Insert or merge one thread's progress. ``status`` and ``updated_at`` are always
        written; ``outcome`` / ``pending`` given as ``None`` leave the stored value untouched,
        so a run that finishes keeps the request its outcome resolved (``list_approvals``
        derives ``resolved`` from the outcome, never from a cleared request)."""
        now = self._stamp()
        outcome_json = None if outcome is None else outcome.model_dump_json()
        pending_json = None if pending is None else pending.model_dump_json()
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO patient_runs (run_id, patient_id, status, outcome_json, "
                    "pending_json, superseded_by, updated_at) VALUES (?, ?, ?, ?, ?, NULL, ?) "
                    "ON CONFLICT(run_id, patient_id) DO UPDATE SET "
                    "status = excluded.status, "
                    "outcome_json = COALESCE(excluded.outcome_json, patient_runs.outcome_json), "
                    "pending_json = COALESCE(excluded.pending_json, patient_runs.pending_json), "
                    "updated_at = excluded.updated_at",
                    (run_id, patient_id, status, outcome_json, pending_json, now),
                )
            except sqlite3.IntegrityError as exc:
                raise UnknownRunError(
                    f"run {run_id!r} does not exist; call create_run first"
                ) from exc
            row = self._conn.execute(
                "SELECT * FROM patient_runs WHERE run_id = ? AND patient_id = ?",
                (run_id, patient_id),
            ).fetchone()
        return _patient_run_from_row(row)

    def get_patient_run(self, run_id: str, patient_id: str) -> PatientRunRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM patient_runs WHERE run_id = ? AND patient_id = ?",
                (run_id, patient_id),
            ).fetchone()
        return None if row is None else _patient_run_from_row(row)

    def list_patient_runs(self, run_id: str) -> list[PatientRunRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM patient_runs WHERE run_id = ? ORDER BY patient_id", (run_id,)
            ).fetchall()
        return [_patient_run_from_row(row) for row in rows]

    # -- decisions ---------------------------------------------------------------------------

    def record_decision(
        self,
        decision: ApprovalDecision,
        run_id: str,
        patient_id: str,
        result: RunOutcome | ApprovalRequest,
    ) -> bool:
        """Store a decision and what the resume produced. Returns ``False`` — and changes
        nothing — when ``decision_id`` already exists; ``get_decision`` then replays it."""
        with self._lock, self._conn:
            try:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO decisions (decision_id, run_id, patient_id, action, "
                    "payload_json, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        decision.decision_id,
                        run_id,
                        patient_id,
                        decision.action,
                        decision.model_dump_json(),
                        _dump_result(result),
                        self._stamp(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise UnknownRunError(
                    f"run {run_id!r} does not exist; call create_run first"
                ) from exc
            return cur.rowcount == 1

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        if row is None:
            return None
        return DecisionRecord(
            decision_id=row["decision_id"],
            run_id=row["run_id"],
            patient_id=row["patient_id"],
            action=row["action"],
            payload=ApprovalDecision.model_validate_json(row["payload_json"]),
            result=_load_result(row["result_json"]),
            created_at=row["created_at"],
        )

    # -- outbox ------------------------------------------------------------------------------

    def append_outbox(self, entries: list[OutboxEntry]) -> int:
        """``INSERT OR IGNORE`` on ``UNIQUE(thread_id, action_id)``; returns the number actually
        inserted, so a replayed finalize reports 0 instead of duplicating. One stamp per call:
        every entry of a batch shares the moment it was appended."""
        inserted = 0
        now = self._stamp()
        with self._lock, self._conn:
            for entry in entries:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO outbox (thread_id, action_id, run_id, patient_id, "
                    "measure_id, kind, detail, owner, approval_ref, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.thread_id,
                        entry.action_id,
                        entry.run_id,
                        entry.patient_id,
                        entry.measure_id,
                        entry.kind,
                        entry.detail,
                        entry.owner,
                        entry.approval_ref,
                        entry.created_at or now,
                    ),
                )
                inserted += cur.rowcount
        return inserted

    def list_outbox(self, run_id: str | None = None) -> list[OutboxEntry]:
        """Entries in append order (by ``id``), optionally for one run."""
        with self._lock:
            if run_id is None:
                rows = self._conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM outbox WHERE run_id = ? ORDER BY id", (run_id,)
                ).fetchall()
        return [_outbox_from_row(row) for row in rows]

    # -- approvals ---------------------------------------------------------------------------

    def list_approvals(self, status: ApprovalStatus | None = None) -> list[ApprovalRecord]:
        """Every patient run that ever emitted a request, with a derived status: ``resolved``
        when an outcome exists, else ``superseded`` when a newer run took the patient, else
        ``pending``. Ordered by ``(run_id, patient_id)``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, patient_id, outcome_json, pending_json, superseded_by "
                "FROM patient_runs WHERE pending_json IS NOT NULL ORDER BY run_id, patient_id"
            ).fetchall()
        records: list[ApprovalRecord] = []
        for row in rows:
            derived = _approval_status(row["outcome_json"], row["superseded_by"])
            if status is not None and derived != status:
                continue
            records.append(
                ApprovalRecord(
                    run_id=row["run_id"],
                    patient_id=row["patient_id"],
                    status=derived,
                    request=ApprovalRequest.model_validate_json(row["pending_json"]),
                    superseded_by=row["superseded_by"],
                )
            )
        return records

    def mark_superseded(self, patient_id: str, newer_run_id: str) -> int:
        """Stamp ``superseded_by`` on every OTHER run's still-pending request for this patient
        (SPEC section 3: supersession when a newer run finalizes the patient). Returns the
        number of requests marked."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE patient_runs SET superseded_by = ?, updated_at = ? "
                "WHERE patient_id = ? AND run_id != ? AND pending_json IS NOT NULL "
                "AND outcome_json IS NULL AND superseded_by IS NULL",
                (newer_run_id, self._stamp(), patient_id, newer_run_id),
            )
            return cur.rowcount


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        as_of=date.fromisoformat(row["as_of"]),
        status=row["status"],
        options=RunOptions.model_validate_json(row["options_json"]),
        patient_ids=json.loads(row["patient_ids_json"]),
        cursor=row["cursor"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _patient_run_from_row(row: sqlite3.Row) -> PatientRunRecord:
    outcome_json = row["outcome_json"]
    pending_json = row["pending_json"]
    return PatientRunRecord(
        run_id=row["run_id"],
        patient_id=row["patient_id"],
        status=row["status"],
        outcome=None if outcome_json is None else RunOutcome.model_validate_json(outcome_json),
        pending=(
            None if pending_json is None else ApprovalRequest.model_validate_json(pending_json)
        ),
        superseded_by=row["superseded_by"],
        updated_at=row["updated_at"],
    )


def _outbox_from_row(row: sqlite3.Row) -> OutboxEntry:
    return OutboxEntry(
        thread_id=row["thread_id"],
        action_id=row["action_id"],
        run_id=row["run_id"],
        patient_id=row["patient_id"],
        measure_id=row["measure_id"],
        kind=row["kind"],
        detail=row["detail"],
        owner=row["owner"],
        approval_ref=row["approval_ref"],
        created_at=row["created_at"],
    )
