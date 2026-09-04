"""Outbox sinks. ``finalize`` is the only writer (SPEC section 3) and every sink is idempotent
on ``(thread_id, action_id)``, so a resumed or replayed finalize never double-appends."""

import threading
from typing import Protocol, runtime_checkable

from caregap.graph.runstore import Clock, OutboxEntry, RunStore, format_timestamp, utc_now


@runtime_checkable
class Outbox(Protocol):
    def append(self, entries: list[OutboxEntry]) -> int:
        """Append the entries not already present; return how many were actually added."""
        ...


class InMemoryOutbox:
    """Tests, CLI and evals. Mirrors the sqlite semantics: UNIQUE on ``(thread_id,
    action_id)``, ``created_at`` stamped when empty, append order preserved in ``entries``."""

    def __init__(self, *, clock: Clock = utc_now) -> None:
        self.entries: list[OutboxEntry] = []
        self._clock = clock
        self._lock = threading.Lock()

    def append(self, entries: list[OutboxEntry]) -> int:
        inserted = 0
        now = format_timestamp(self._clock())
        with self._lock:
            seen = {(entry.thread_id, entry.action_id) for entry in self.entries}
            for entry in entries:
                key = (entry.thread_id, entry.action_id)
                if key in seen:
                    continue
                seen.add(key)
                stamped = (
                    entry if entry.created_at else entry.model_copy(update={"created_at": now})
                )
                self.entries.append(stamped)
                inserted += 1
        return inserted

    def list_outbox(self, run_id: str | None = None) -> list[OutboxEntry]:
        with self._lock:
            return [e for e in self.entries if run_id is None or e.run_id == run_id]


class RunStoreOutbox:
    """The API/UI sink: writes through to ``RunStore.append_outbox``."""

    def __init__(self, store: RunStore) -> None:
        self._store = store

    def append(self, entries: list[OutboxEntry]) -> int:
        return self._store.append_outbox(entries)
