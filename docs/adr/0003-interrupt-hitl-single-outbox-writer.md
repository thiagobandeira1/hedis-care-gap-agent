# ADR-0003: Interrupt-based human approval, per-patient threads, and a single outbox writer

- Status: accepted
- Date: 2026-09-04
- Scope: `src/caregap/graph/{build,nodes,edges,state,hitl,runner,runstore,outbox}.py`, `src/caregap/api/routes_decisions.py`, `src/caregap/api/routes_runs.py`, `src/caregap/runtime.py`
- Related: `docs/SPEC.md` sections 3 and 7; ADR-0001; ADR-0004

## Context

The product promise is that no outreach reaches the outbox without a stored human decision. That
promise has to hold across process restarts, duplicate HTTP requests, a reviewer who clicks
twice, a second run started for the same patient while the first is still waiting, and a graph
library whose API changes between releases. It also has to be testable structurally, not just
by example.

LangGraph offers `interrupt()` with a checkpointer: a node can pause the thread, the caller can
inspect the pending interrupt from the checkpoint, and `Command(resume=...)` re-runs the
interrupting node with the reviewer's value. Two properties of that mechanism shaped the design:
an interrupting node's state writes are discarded and the node re-executes on resume, and
"pending" is a fact about the checkpoint, not a value a node can store.

## Decision

**One thread per (run, patient).** `graph/state.py::thread_id` is `f"{run_id}:{patient_id}"`.
`PatientRunner` (`graph/runner.py`) is the one place that invokes the compiled graph, once per
thread for a fresh start or a `Command(resume=...)`, in a worker thread bounded by
`settings.patient_timeout_s` (120 s by default; a thread that overruns is abandoned, never
killed, and the patient is recorded as `error`). Panel runs are a sequential loop over patients
with a persisted cursor and a cancel flag (`run_panel`).

**`await_approval` is the only interrupt site and does nothing else.**
`graph/nodes.py::await_approval` builds an `ApprovalRequest` purely from state (open gaps,
review items, the plan, revision count, prompt shas, model ids), calls `interrupt(...)` with its
JSON dump, and validates the resumed value into an `ApprovalDecision`. It performs no I/O and
no model call, and never writes a status. Under `approval_mode="auto"` (evals and batch mode)
it records an `auto_reviewed` decision instead of interrupting.
`tests/unit/graph/test_structure.py` asserts by AST inspection that `interrupt(` appears in
`await_approval` only, that `await_approval` references none of `deps.p6`, `deps.engine`,
`deps.structured`, `deps.outbox`, `deps.run_store`, and that the compiled node and edge sets
equal the `docs/SPEC.md` section 3 diagram.

**Pending is derived, never stored.** `graph/hitl.py::pending_request` reads
`graph.get_state(thread).tasks[*].interrupts` and parses the payload back into an
`ApprovalRequest`. The runner, the API (`routes_runs.get_patient_run`) and the decision route all
ask the checkpoint; `patient_runs.pending_json` in the ledger is a copy for listing and audit,
and `list_approvals` derives `resolved | superseded | pending` from the presence of an outcome
and a `superseded_by` stamp, not from a stored status word.

**`finalize` is the only outbox writer, and only after a human decision.**
`graph/nodes.py::finalize` computes `actionable = decision.action in {approve, edit} and
approval_mode == "interrupt" and plan is not None` and appends one `OutboxEntry` per plan
action with `approval_ref = decision.decision_id`. `OutboxEntry.approval_ref` is
`Field(min_length=1)` (`graph/runstore.py`), so an entry cannot be constructed without the
decision that authorised it. `test_structure.py` asserts `outbox` appears in `finalize` only.
Rejections, revisions that exhaust their budget, load errors and `auto_reviewed` decisions all
finalize with an empty `approved_actions`.

**Idempotency at every seam.** `RunStore` (sqlite, `graph/runstore.py`) declares `decisions`
UNIQUE on `decision_id` and `outbox` UNIQUE on `(thread_id, action_id)`; `append_outbox` is
`INSERT OR IGNORE` and returns the number actually inserted, so a replayed `finalize` reports 0.
`PatientRunner.resume` returns the stored result for a known `decision_id` without touching the
graph; `routes_decisions.decide` returns it with `replayed=true` and answers 409 when the same
id was used on a different patient run. `InMemoryOutbox` mirrors the UNIQUE semantics for tests,
the CLI and the eval tiers.

**Decisions are validated before resume.** `graph/hitl.py::validate_decision` enforces: `edit`
carries an `edited_plan` that is re-linted with the drafter's own `lint_plan`; `revise` carries
feedback, stays inside `max_revisions` (1 by default) and has something to redraft; a review
resolution must reference an existing review item, a global item cannot be resolved to `open`,
and a measure resolution to `open` is legal only with `revise` (it re-opens the measure for the
redraft in `record_decision`); `auto_reviewed` can never be submitted by a reviewer. The runner
raises `DecisionValidationError` (the API's 422 with the full error list) or
`NotAwaitingApproval` (409) before any side effect.

**Supersession.** When a newer run finalizes a patient with a non-error outcome,
`RunStore.mark_superseded` stamps `superseded_by` on every other run's still-pending request
for that patient; the decision route refuses a superseded request with 409 and the patient-run
route stops reporting it as pending.

**Checkpointers.** `runtime.build_runtime` opens a `SqliteSaver` over
`settings.checkpoint_path` (`data/checkpoints.sqlite`, one connection shared by the API's
request threads and the panel loop) unless a checkpointer is injected; tests and the eval tiers
inject a `MemorySaver`. Both use `graph/build.py::checkpoint_serializer`, a `JsonPlusSerializer`
whose msgpack allowlist is exactly the state's own pydantic payloads, so a checkpoint can never
revive an arbitrary class. Because the CLI's `caregap run` uses the same sqlite paths as
`caregap serve`, a run paused from the command line can be decided through the API or the UI.

## Consequences

### Positive

- The guarantee is structural and tested as such: single interrupt site, single outbox writer,
  `approval_ref` required by type, UNIQUE constraints in the ledger, pending derived from the
  checkpoint. Scenario tests cover every decision action, auto mode never writing the outbox,
  reject leaving it untouched, resume idempotency, and a sqlite round-trip across graph
  instances (`tests/unit/graph/test_scenarios.py`, `test_runner.py`, `test_hitl.py`).
- A reviewer's duplicate click, a retried HTTP request and a re-run `finalize` are all safe.
- Restarting the API does not lose pending approvals; they live in the checkpoint database.
- Per-patient threads keep one patient's failure or timeout from blocking the panel.

### Negative

- Single-writer sqlite (WAL, one connection per store) is a demo topology: a second API process
  against the same files is not supported.
- A timed-out worker thread is abandoned, not killed; it may still write its checkpoint later
  while the ledger already says `error` for that patient.
- `PatientRunRecord.pending` persists after completion (COALESCE on upsert), so "is pending" must
  come from `pending_request` or `list_approvals`, never from that column
  (`docs/REVIEW_BACKLOG.md` item 15).
- The design leans on LangGraph semantics (discarded writes of an interrupting node, interrupts
  visible in `get_state`); the library is pinned and the structural tests exist to catch API
  drift, but an upgrade is a deliberate event.
- The revision budget of one round-trip is small on purpose; a reviewer who wants a third draft
  edits the plan instead.

## Alternatives considered

1. **A status column written by the pausing node, polled by the API.** Rejected. LangGraph
   discards an interrupting node's writes, and a stored status can disagree with the checkpoint
   after a crash; deriving pending from `get_state` has one source of truth.
2. **A separate approval queue or service outside the graph.** Rejected for this scope. It adds a
   second store to keep consistent with the checkpoint and moves the outbox guarantee out of the
   graph where it can be asserted structurally.
3. **Auto-send with an audit log and an undo window.** Rejected. It is the opposite of the product
   promise; `approval_mode="auto"` exists only so evals can drive the graph end to end, and it is
   never actionable.
4. **One thread per run with all patients in one state.** Rejected. One reviewer decision would
   block every other patient, and a per-patient timeout would be impossible to express.
