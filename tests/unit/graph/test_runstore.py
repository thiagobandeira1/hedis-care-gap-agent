"""``RunStore``: JSON round-trips for every record type, decision and outbox idempotency,
approval status derivation (incl. supersession), WAL on file-backed stores, thread safety,
``:memory:`` and clock injection. sqlite3 stdlib only — no fixtures beyond ``tmp_path``."""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from caregap.agents.schemas import CareActionPlan, GapAction, PlannedGap
from caregap.graph.runstore import (
    MEMORY,
    ApprovalRecord,
    DecisionRecord,
    DuplicateRunError,
    OutboxEntry,
    PatientRunRecord,
    RunRecord,
    RunStore,
    UnknownRunError,
    format_timestamp,
)
from caregap.graph.state import (
    ApprovalDecision,
    ApprovalRequest,
    LoadError,
    ReviewResolution,
    RunOptions,
    RunOutcome,
    thread_id,
)
from caregap.measures.models import EvidenceRef, OpenGap, ReviewItem
from tests.factories import EVAL_AS_OF

T0 = datetime(2026, 1, 2, 3, 4, 5, 678_000, tzinfo=UTC)
T0_TEXT = "2026-01-02T03:04:05.678Z"


def at(seconds: int) -> str:
    """The stamp of the ``seconds``-th tick of a ``TickingClock`` started at ``T0``."""
    return format_timestamp(T0 + timedelta(seconds=seconds))


class TickingClock:
    """Deterministic clock: every call advances one second from ``T0``."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start
        self.calls = 0

    def __call__(self) -> datetime:
        current = self.now
        self.now = current + timedelta(seconds=1)
        self.calls += 1
        return current


# -- payload builders -------------------------------------------------------------------------


def make_options() -> RunOptions:
    return RunOptions(
        validation_mode="off", approval_mode="auto", max_revisions=2, measures=("CBP", "COL")
    )


def make_plan() -> CareActionPlan:
    return CareActionPlan(
        gaps=[PlannedGap(measure_id="CBP", rank=1, urgency="soon", rationale="BP not checked")],
        actions=[
            GapAction(
                action_id="a1",
                measure_id="CBP",
                kind="schedule_visit",
                detail="Schedule a blood pressure visit.",
                owner="care_team",
            )
        ],
        patient_message="Please call the clinic to book a blood pressure check.",
        provider_note="No BP recorded in the measurement year.",
    )


def make_request(run_id: str = "r1", patient_id: str = "p1", revision: int = 0) -> ApprovalRequest:
    evidence = EvidenceRef(
        section="observations",
        event_id="o1",
        code="85354-9",
        code_system="LOINC",
        display="Blood pressure panel",
        event_date=date(2025, 3, 15),
        role="numerator",
    )
    return ApprovalRequest(
        run_id=run_id,
        patient_id=patient_id,
        as_of=EVAL_AS_OF,
        open_gaps=[
            OpenGap(
                measure_id="CBP",
                subtype="no_bp_in_my",
                evidence=[evidence],
                priority_score=0.9,
                rank=1,
                source="validator_confirmed",
            )
        ],
        review_items=[
            ReviewItem(
                measure_id="COL", scope="measure", reason="validator_failed", evidence=[evidence]
            )
        ],
        plan=make_plan(),
        draft_error=None,
        revision_count=revision,
        prompt_versions={"validator": "v1", "drafter": "v1"},
        model_ids={"validator": "fake", "drafter": "fake"},
    )


def make_outcome(status: str = "completed") -> RunOutcome:
    if status == "error":
        return RunOutcome(
            status="error",
            actionable=False,
            load_error=LoadError(kind="unavailable", detail="P6Unavailable"),
        )
    return RunOutcome(
        status="completed",
        actionable=True,
        approved_actions=["a1"],
        engine_verdicts={"CBP": "gap_open", "COL": "needs_review"},
        final_statuses={"CBP": "open", "COL": "excluded"},
        decision_action="approve",
    )


def make_decision(decision_id: str = "d1", action: str = "approve") -> ApprovalDecision:
    return ApprovalDecision(
        decision_id=decision_id,
        action=action,  # type: ignore[arg-type]
        edited_plan=make_plan() if action == "edit" else None,
        feedback="Shorter please." if action == "revise" else None,
        review_resolutions=[
            ReviewResolution(measure_id="COL", status="excluded", reason="documented colectomy")
        ],
        reviewer="nurse-1",
        note="ok",
    )


def make_entry(action_id: str, run_id: str = "r1", patient_id: str = "p1") -> OutboxEntry:
    return OutboxEntry(
        thread_id=thread_id(run_id, patient_id),
        action_id=action_id,
        run_id=run_id,
        patient_id=patient_id,
        measure_id="CBP",
        kind="schedule_visit",
        detail="Schedule a blood pressure visit.",
        owner="care_team",
        approval_ref="d1",
    )


@pytest.fixture
def clock() -> TickingClock:
    return TickingClock()


@pytest.fixture
def store(clock: TickingClock) -> Iterator[RunStore]:
    with RunStore(MEMORY, clock=clock) as s:
        yield s


# -- runs -------------------------------------------------------------------------------------


def test_memory_store_round_trips_a_run(store: RunStore) -> None:
    created = store.create_run("r1", EVAL_AS_OF, make_options(), ["p2", "p1"])
    fetched = store.get_run("r1")

    assert isinstance(fetched, RunRecord)
    assert fetched == created
    assert fetched.options == make_options()
    assert fetched.options.measures == ("CBP", "COL")
    assert fetched.patient_ids == ["p2", "p1"]
    assert fetched.as_of == EVAL_AS_OF
    assert fetched.status == "created"
    assert fetched.cursor == 0
    assert fetched.created_at == fetched.updated_at == T0_TEXT
    assert store.get_run("nope") is None


def test_create_run_twice_raises_and_keeps_the_original(store: RunStore) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
    with pytest.raises(DuplicateRunError):
        store.create_run("r1", EVAL_AS_OF, make_options(), ["p9"])
    run = store.get_run("r1")
    assert run is not None
    assert run.patient_ids == ["p1"]


def test_update_run_status_and_cursor(store: RunStore, clock: TickingClock) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1", "p2"])

    store.update_run_status("r1", "running", cursor=1)
    run = store.get_run("r1")
    assert run is not None
    assert (run.status, run.cursor) == ("running", 1)
    assert run.created_at == T0_TEXT
    assert run.updated_at == at(1)

    store.update_run_status("r1", "cancelled")
    run = store.get_run("r1")
    assert run is not None
    assert (run.status, run.cursor) == ("cancelled", 1), "cursor=None keeps the cursor"

    with pytest.raises(UnknownRunError):
        store.update_run_status("missing", "running")


# -- patient runs -----------------------------------------------------------------------------


def test_patient_run_round_trips_outcome_and_request(store: RunStore) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
    request = make_request()
    outcome = make_outcome()

    written = store.upsert_patient_run("r1", "p1", "completed", outcome, request)
    fetched = store.get_patient_run("r1", "p1")

    assert isinstance(fetched, PatientRunRecord)
    assert fetched == written
    assert fetched.outcome == outcome
    assert fetched.pending == request
    assert fetched.pending is not None
    assert fetched.pending.plan == make_plan()
    assert fetched.pending.open_gaps[0].evidence[0].event_date == date(2025, 3, 15)
    assert fetched.superseded_by is None
    assert fetched.updated_at == at(1)
    assert store.get_patient_run("r1", "p9") is None


def test_error_outcome_round_trips_load_error(store: RunStore) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
    store.upsert_patient_run("r1", "p1", "error", make_outcome("error"), None)
    fetched = store.get_patient_run("r1", "p1")
    assert fetched is not None
    assert fetched.outcome == make_outcome("error")
    assert fetched.pending is None


def test_upsert_merges_none_payloads_and_always_writes_status(store: RunStore) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
    request = make_request()

    store.upsert_patient_run("r1", "p1", "running", None, None)
    store.upsert_patient_run("r1", "p1", "awaiting_approval", None, request)
    resolved = store.upsert_patient_run("r1", "p1", "completed", make_outcome(), None)

    assert resolved.status == "completed"
    assert resolved.outcome == make_outcome()
    assert resolved.pending == request, "None leaves the stored request in place"
    assert resolved.updated_at == at(3)


def test_upsert_replaces_request_on_revision(store: RunStore) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
    store.upsert_patient_run("r1", "p1", "awaiting_approval", None, make_request(revision=0))
    store.upsert_patient_run("r1", "p1", "awaiting_approval", None, make_request(revision=1))
    fetched = store.get_patient_run("r1", "p1")
    assert fetched is not None
    assert fetched.pending is not None
    assert fetched.pending.revision_count == 1


def test_upsert_without_run_raises_unknown_run(store: RunStore) -> None:
    with pytest.raises(UnknownRunError):
        store.upsert_patient_run("ghost", "p1", "running", None, None)


def test_list_patient_runs_is_sorted_by_patient_id(store: RunStore) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p3", "p1", "p2"])
    store.create_run("r2", EVAL_AS_OF, RunOptions(), ["p1"])
    for pid in ("p3", "p1", "p2"):
        store.upsert_patient_run("r1", pid, "running", None, None)
    store.upsert_patient_run("r2", "p1", "running", None, None)

    assert [r.patient_id for r in store.list_patient_runs("r1")] == ["p1", "p2", "p3"]
    assert [r.run_id for r in store.list_patient_runs("r2")] == ["r2"]
    assert store.list_patient_runs("r9") == []


# -- decisions --------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["approve", "edit", "revise"])
def test_decision_round_trips_payload_and_result(store: RunStore, action: str) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
    decision = make_decision("d1", action)
    result: RunOutcome | ApprovalRequest = (
        make_request(revision=1) if action == "revise" else make_outcome()
    )

    assert store.record_decision(decision, "r1", "p1", result) is True
    fetched = store.get_decision("d1")

    assert isinstance(fetched, DecisionRecord)
    assert fetched.payload == decision
    assert fetched.result == result
    assert type(fetched.result) is type(result)
    assert (fetched.run_id, fetched.patient_id, fetched.action) == ("r1", "p1", action)
    assert fetched.created_at == at(1)
    assert store.get_decision("d9") is None


def test_record_decision_is_idempotent_on_decision_id(store: RunStore) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
    first = make_decision("d1", "approve")
    replay = make_decision("d1", "reject")

    assert store.record_decision(first, "r1", "p1", make_outcome()) is True
    assert store.record_decision(replay, "r1", "p1", make_outcome("error")) is False

    stored = store.get_decision("d1")
    assert stored is not None
    assert stored.payload == first, "the duplicate changed nothing"
    assert stored.result == make_outcome()
    assert stored.action == "approve"


def test_record_decision_without_run_raises_unknown_run(store: RunStore) -> None:
    with pytest.raises(UnknownRunError):
        store.record_decision(make_decision(), "ghost", "p1", make_outcome())
    assert store.get_decision("d1") is None


# -- outbox -----------------------------------------------------------------------------------


def test_outbox_append_is_idempotent_and_stamps_created_at(store: RunStore) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
    entries = [make_entry("a1"), make_entry("a2")]

    assert store.append_outbox(entries) == 2
    assert store.append_outbox(entries) == 0
    assert store.append_outbox([make_entry("a2"), make_entry("a3")]) == 1

    listed = store.list_outbox()
    assert [e.action_id for e in listed] == ["a1", "a2", "a3"]
    assert [e.created_at for e in listed] == [at(1), at(1), at(3)], "one stamp per batch"
    assert listed[0].model_copy(update={"created_at": ""}) == entries[0]
    assert listed[0].approval_ref == "d1"


def test_outbox_keeps_a_caller_supplied_created_at(store: RunStore) -> None:
    stamped = make_entry("a1").model_copy(update={"created_at": "2020-01-01T00:00:00.000Z"})
    store.append_outbox([stamped])
    assert store.list_outbox()[0] == stamped


def test_outbox_uniqueness_is_per_thread(store: RunStore) -> None:
    assert store.append_outbox([make_entry("a1", "r1", "p1"), make_entry("a1", "r2", "p1")]) == 2
    assert [e.thread_id for e in store.list_outbox("r2")] == ["r2:p1"]
    assert store.list_outbox("r9") == []


@pytest.mark.parametrize("field", ["approval_ref", "thread_id", "action_id"])
def test_outbox_entry_requires_its_idempotency_and_approval_keys(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        OutboxEntry.model_validate(make_entry("a1").model_dump() | {field: ""})


# -- approvals --------------------------------------------------------------------------------


def test_list_approvals_derives_status_and_supersession(store: RunStore) -> None:
    for run_id in ("r1", "r2", "r3"):
        store.create_run(run_id, EVAL_AS_OF, RunOptions(), ["p1", "p2"])
    # r1/p1: pending, then superseded by r2. r1/p2: resolved (outcome). r2/p1: pending.
    # r2/p2: never emitted a request -> not an approval. r3/p1: pending in a third run.
    store.upsert_patient_run("r1", "p1", "awaiting_approval", None, make_request("r1", "p1"))
    store.upsert_patient_run("r1", "p2", "completed", make_outcome(), make_request("r1", "p2"))
    store.upsert_patient_run("r2", "p1", "awaiting_approval", None, make_request("r2", "p1"))
    store.upsert_patient_run("r2", "p2", "running", None, None)
    store.upsert_patient_run("r3", "p1", "awaiting_approval", None, make_request("r3", "p1"))

    pending_before = {(a.run_id, a.patient_id) for a in store.list_approvals("pending")}
    assert pending_before == {("r1", "p1"), ("r2", "p1"), ("r3", "p1")}

    assert store.mark_superseded("p1", "r2") == 2
    assert store.mark_superseded("p1", "r2") == 0, "already-superseded rows are left alone"

    everything = store.list_approvals()
    assert all(isinstance(a, ApprovalRecord) for a in everything)
    assert [(a.run_id, a.patient_id, a.status, a.superseded_by) for a in everything] == [
        ("r1", "p1", "superseded", "r2"),
        ("r1", "p2", "resolved", None),
        ("r2", "p1", "pending", None),
        ("r3", "p1", "superseded", "r2"),
    ]
    assert everything[0].request == make_request("r1", "p1")
    assert [a.run_id for a in store.list_approvals("pending")] == ["r2"]
    assert [a.run_id for a in store.list_approvals("superseded")] == ["r1", "r3"]
    assert [a.run_id for a in store.list_approvals("resolved")] == ["r1"]

    stale = store.get_patient_run("r1", "p1")
    assert stale is not None
    assert stale.superseded_by == "r2"
    assert stale.status == "awaiting_approval", "supersession never rewrites status"


def test_mark_superseded_skips_resolved_rows_and_other_patients(store: RunStore) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1", "p2"])
    store.create_run("r2", EVAL_AS_OF, RunOptions(), ["p1"])
    store.upsert_patient_run("r1", "p1", "completed", make_outcome(), make_request("r1", "p1"))
    store.upsert_patient_run("r1", "p2", "awaiting_approval", None, make_request("r1", "p2"))

    assert store.mark_superseded("p1", "r2") == 0
    assert [(a.run_id, a.status) for a in store.list_approvals()] == [
        ("r1", "resolved"),
        ("r1", "pending"),
    ]


def test_outcome_after_supersession_reads_as_resolved(store: RunStore) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
    store.create_run("r2", EVAL_AS_OF, RunOptions(), ["p1"])
    store.upsert_patient_run("r1", "p1", "awaiting_approval", None, make_request("r1", "p1"))
    store.mark_superseded("p1", "r2")
    store.upsert_patient_run("r1", "p1", "completed", make_outcome(), None)
    assert [a.status for a in store.list_approvals()] == ["resolved"]


# -- storage engine ---------------------------------------------------------------------------


def test_file_backed_store_uses_wal_and_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "ledger" / "runs.sqlite"
    with RunStore(db, clock=TickingClock()) as store:
        store.create_run("r1", EVAL_AS_OF, make_options(), ["p1"])
        store.append_outbox([make_entry("a1")])
        with closing(sqlite3.connect(db)) as probe:
            assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert probe.execute("PRAGMA foreign_keys").fetchone()[0] in (0, 1)

    with RunStore(db, clock=TickingClock()) as reopened:  # CREATE IF NOT EXISTS is idempotent
        run = reopened.get_run("r1")
        assert run is not None
        assert run.options == make_options()
        assert reopened.append_outbox([make_entry("a1")]) == 0
        assert len(reopened.list_outbox()) == 1
        assert reopened.path == str(db)


def test_memory_store_is_private_and_not_wal() -> None:
    a = RunStore(MEMORY)
    b = RunStore(MEMORY)
    a.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
    assert b.get_run("r1") is None
    assert a.path == ":memory:"
    a.close()
    b.close()


def test_default_clock_is_utc_now() -> None:
    before = datetime.now(UTC)
    with RunStore(MEMORY) as store:
        run = store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
    stamped = datetime.fromisoformat(run.created_at.replace("Z", "+00:00"))
    assert stamped.tzinfo is not None
    assert before - timedelta(seconds=1) <= stamped <= datetime.now(UTC) + timedelta(seconds=1)


def test_clock_injection_and_naive_datetimes(clock: TickingClock) -> None:
    with RunStore(MEMORY, clock=clock) as store:
        run = store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
        assert run.created_at == T0_TEXT
        assert clock.calls == 1, "one stamp per write, shared by created_at and updated_at"
        store.append_outbox([make_entry("a1")])
        assert store.list_outbox()[0].created_at == at(1)

    assert format_timestamp(datetime(2026, 5, 6, 7, 8, 9)) == "2026-05-06T07:08:09.000Z"
    minus_four = datetime(2026, 5, 5, 23, 8, 9, tzinfo=timezone(timedelta(hours=-4)))
    assert format_timestamp(minus_four) == "2026-05-06T03:08:09.000Z"


def test_concurrent_appends_from_four_threads(store: RunStore) -> None:
    store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
    per_thread = 25
    shared = [make_entry("shared-1"), make_entry("shared-2")]
    inserted: list[int] = []
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def worker(index: int) -> None:
        try:
            start.wait()
            own = [make_entry(f"t{index}-a{i}") for i in range(per_thread)]
            inserted.append(store.append_outbox(own + shared))
            store.upsert_patient_run("r1", "p1", f"running-{index}", None, None)
        except BaseException as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert sum(inserted) == 4 * per_thread + len(shared)
    assert len(store.list_outbox()) == 4 * per_thread + len(shared)
    assert store.get_patient_run("r1", "p1") is not None
