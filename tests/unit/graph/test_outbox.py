"""Outbox sinks: both satisfy the ``Outbox`` protocol, both are idempotent on
``(thread_id, action_id)``, and the sqlite sink writes through to the ``RunStore`` ledger."""

from datetime import UTC, datetime

from caregap.graph.outbox import InMemoryOutbox, Outbox, RunStoreOutbox
from caregap.graph.runstore import MEMORY, OutboxEntry, RunStore
from caregap.graph.state import RunOptions, thread_id
from tests.factories import EVAL_AS_OF

T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def fixed_clock() -> datetime:
    return T0


def make_entry(action_id: str, run_id: str = "r1", patient_id: str = "p1") -> OutboxEntry:
    return OutboxEntry(
        thread_id=thread_id(run_id, patient_id),
        action_id=action_id,
        run_id=run_id,
        patient_id=patient_id,
        measure_id="COL",
        kind="screening",
        detail="Order a colorectal cancer screening.",
        owner="provider",
        approval_ref="d1",
    )


def test_both_sinks_satisfy_the_protocol() -> None:
    with RunStore(MEMORY) as store:
        assert isinstance(InMemoryOutbox(), Outbox)
        assert isinstance(RunStoreOutbox(store), Outbox)


def test_in_memory_outbox_is_idempotent_and_stamps_created_at() -> None:
    outbox = InMemoryOutbox(clock=fixed_clock)
    entries = [make_entry("a1"), make_entry("a2")]

    assert outbox.append(entries) == 2
    assert outbox.append(entries) == 0
    assert outbox.append([make_entry("a2"), make_entry("a3"), make_entry("a3", "r2")]) == 2

    assert [(e.thread_id, e.action_id) for e in outbox.entries] == [
        ("r1:p1", "a1"),
        ("r1:p1", "a2"),
        ("r1:p1", "a3"),
        ("r2:p1", "a3"),
    ]
    assert all(e.created_at == "2026-01-02T03:04:05.000Z" for e in outbox.entries)
    assert outbox.entries[0].model_copy(update={"created_at": ""}) == entries[0]
    assert entries[0].created_at == "", "the caller's entry is never mutated"
    assert [e.run_id for e in outbox.list_outbox("r2")] == ["r2"]
    assert len(outbox.list_outbox()) == 4


def test_in_memory_outbox_dedupes_within_one_batch_and_keeps_given_stamps() -> None:
    outbox = InMemoryOutbox(clock=fixed_clock)
    stamped = make_entry("a1").model_copy(update={"created_at": "2020-01-01T00:00:00.000Z"})
    assert outbox.append([stamped, make_entry("a1")]) == 1
    assert outbox.entries == [stamped]


def test_run_store_outbox_writes_through_to_the_ledger() -> None:
    with RunStore(MEMORY, clock=fixed_clock) as store:
        store.create_run("r1", EVAL_AS_OF, RunOptions(), ["p1"])
        outbox = RunStoreOutbox(store)

        assert outbox.append([make_entry("a1"), make_entry("a2")]) == 2
        assert outbox.append([make_entry("a1")]) == 0
        assert store.append_outbox([make_entry("a2")]) == 0, "same UNIQUE index as the store"

        listed = store.list_outbox("r1")
        assert [e.action_id for e in listed] == ["a1", "a2"]
        assert listed[0].created_at == "2026-01-02T03:04:05.000Z"
        assert listed[0].approval_ref == "d1"
