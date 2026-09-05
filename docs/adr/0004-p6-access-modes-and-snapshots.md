# ADR-0004: One HTTP client over an injected transport for P6, plus committed snapshots for tests

- Status: accepted
- Date: 2026-09-04
- Scope: `src/caregap/p6/{client,http,embedded,snapshot,models}.py`, `src/caregap/runtime.py::_open_p6`, `src/caregap/cli.py::bootstrap`, `scripts/gold.py`, `synthetic/p6_snapshots/`
- Related: `docs/SPEC.md` section 5; ADR-0003 (sync routes); ADR-0005 (snapshots as the labelers' record)

## Context

Patient records come from the sibling `fhir-feature-service` (P6), pinned at `v0.1.0` through
`[tool.uv.sources]` in `pyproject.toml`. P6 is an HTTP service over DuckDB with its own CLI for
ingestion. This repository needs it in three very different situations:

- a production-shaped topology where P6 runs as its own process;
- a keyless, one-command quickstart and demo where nobody wants to start a second service;
- unit, graph and eval tests that must be deterministic, fast, and independent of DuckDB and
  of P6's ingest path.

The constraints: one code path for retries, error mapping and typed validation regardless of
topology; no P6 internals reached into; a strict boundary on what patient data enters this
process; and the engine must never consume P6's feature table.

## Decision

**A narrow client protocol and an error taxonomy.** `caregap.p6.client.P6Client` has five
methods: `healthz`, `features_schema`, `list_patients`, `get_record(to=as_of, sections=...)`,
`get_features(as_of)`. Errors are `PatientNotFound` (404), `P6ContractError` (422, any other
4xx, or a body that fails the typed models), and `P6Unavailable` (connection errors or 5xx after
retries). `graph/nodes.py::load_record` maps them to `LoadError(kind=not_found | contract |
unavailable)` and the graph finalizes with `status="error"`; the API maps them to 404 / 502 /
503 problem details.

**One `HttpP6Client` over an injected `httpx.Client`.** `p6/http.py` implements the protocol
with a single `_get`: retries on transport errors and 5xx only (delays 0.2 s, 0.6 s, 1.8 s; four
attempts), 4xx mapped and never retried, JSON validated into the typed models. The transport is
injected, which gives two topologies with one code path:

- **http mode**: `httpx.Client(base_url=settings.p6_url, timeout=30 s)` against a running
  `fhir-features serve`.
- **embedded mode** (the default, `CAREGAP_P6_MODE=embedded`): `p6/embedded.py` builds P6's own
  ASGI app with `fhir_features.api.app.create_app(P6Settings(db_path=...))` and wraps it in a
  Starlette `TestClient`, entered once in `runtime.build_runtime` and kept open until
  `Runtime.close()`, so P6's lifespan (migrations, value-set sync) runs. A `TestClient` cannot
  be driven from the event-loop thread, so every FastAPI route that touches P6 or the graph is a
  sync `def` (`src/caregap/api/app.py`).

`tests/unit/p6/test_http_client.py` exercises the retry and mapping matrix once, with `respx`,
for both topologies.

**`SnapshotP6Client` over committed responses.** `p6/snapshot.py` reads
`synthetic/p6_snapshots/MANIFEST.json` plus `<patient_id>/record_<as_of>.json.gz` and
`features_<as_of>.json`: the raw bodies P6 returned, including every observation code, with
deterministic gzip (`mtime=0`, sorted keys). `scripts/gold.py snapshot --as-of <date>` writes
them by booting the real P6 in-process over a throwaway DuckDB. The committed manifest records
`service_version 0.1.0`, `schema_version 3`, `valuesets_version 2026.08`, the git sha it was
generated from, the five persona ids, and both anchors (`2024-12-31`, `2025-12-31`). This is
what unit, graph, API, UI and eval tests run on (`CAREGAP_P6_MODE=snapshot`), with zero DuckDB
and zero network.

**Engine goldens over the snapshots.** `tests/unit/measures/test_goldens.py` byte-compares the
engine's full `list[MeasureEvaluation]` for every persona at both anchors against
`tests/unit/measures/goldens/<pid>_<as_of>.json`; `scripts/gold.py goldens --regen` rewrites
them deliberately. `tests/unit/measures/test_value_sets.py::test_vendored_p6_sets_match_installed_package`
asserts that `value_sets/p6_vendored.json` equals the installed P6 package's members set by set,
so the snapshots, the vendored value sets and the pinned package cannot drift apart silently.

**Bootstrap through P6's own CLI.** `caregap bootstrap` sets `FF_DB_PATH` before importing
`fhir_features` and runs P6's `init-db` and `ingest` in-process over `synthetic/samples`,
bypassing P6's HTTP upload cap. `scripts/gold.py snapshot` uses the same path.

**Boundary rules.** `p6/models.py` projects P6 rows with `extra="ignore"`; `PatientHeader`
keeps `patient_id`, `birth_date`, `death_date`, `sex` only, so race, ethnicity and address never
enter this process (`tests/unit/agents/test_packet.py::test_no_demographics_or_birth_date_from_a_p6_payload_reach_the_render`).
`client.mask_as_of` nulls future death, abatement and end dates and drops events dated after
`as_of`, on top of P6's own `?to=` filter. `FeatureRow` is loaded for drafter context and
display only and never reaches a rule
(`tests/unit/measures/test_cbp.py::test_rule_never_reads_status_fields_or_features`).

## Consequences

### Positive

- Retry, error mapping and validation are written and tested once; switching topology is a
  setting, not a code path.
- The quickstart and the demo need no second process and no key.
- Tests are deterministic and fast, and the persona goldens make any engine change visible as a
  byte diff in review.
- The labelers of ADR-0005 read the same bytes the engine reads: snapshots carry every section
  and every observation code (`ENGINE_SECTIONS` applies no `observation_codes` filter).

### Negative

- Embedded mode runs P6's ASGI app inside a long-lived API process through a test client. It is
  a demo topology; the http mode is the production-shaped one.
- Sync routes throughout: the API cannot use async I/O even where it would help.
- Snapshots go stale when P6 changes. The manifest carries the generating sha and versions, and
  the parity test covers value sets, but a live-vs-snapshot contract check against the pinned
  P6 (`docs/SPEC.md` section 9, `contract` job) is not yet in `.github/workflows/ci.yml`.
- `.gitignore` blocks `*.json.gz` and re-allows it only under `synthetic/**`; snapshots written
  anywhere else (for example an eval gold snapshot directory) need an explicit allow rule before
  they can be committed.
- The five committed personas carry no escalations, so validator paths are exercised on
  factory-built records rather than real Synthea ones until the gold panel (ADR-0005) adds
  escalation carriers (`docs/REVIEW_BACKLOG.md` item 17).

## Alternatives considered

1. **Read P6's DuckDB directly.** Rejected. It couples this repository to P6's storage schema,
   bypasses P6's API contract and masking, and would not exercise the same path as http mode.
2. **Re-implement Synthea ingestion here.** Rejected. Duplicated logic that P6 already owns and
   tests; the point of the pin is one ingestion path.
3. **Per-test mocks of the client.** Kept only for the retry matrix (`respx`). For everything
   else, committed real responses are cheaper to reason about than hand-written fixtures and
   cannot drift from P6's actual JSON shape.
4. **Vendor P6's source code.** Rejected. A pinned git dependency plus a parity test gives the
   same reproducibility without a fork to maintain.
