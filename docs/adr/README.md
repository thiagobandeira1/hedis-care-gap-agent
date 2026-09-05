# Architecture decision records

MADR-style records of the decisions that shape `hedis-care-gap-agent`. Each record states the
context, the decision as the code implements it (with module paths), the consequences including
the negative ones, and the alternatives that were rejected. `docs/SPEC.md` section 11 lists the
same five decisions in one line each.

| ADR | Title | Status | Date |
|---|---|---|---|
| [0001](0001-deterministic-engine-monotonic-validator.md) | Deterministic measure engine with a monotonic, code-verified validator | accepted | 2026-09-04 |
| [0002](0002-statin-on-therapy-uses-medication-status.md) | The statin on-therapy rule reads `MedicationRequest.status` (explicit deviation from P6's date-only doctrine; leakage caveat) | accepted | 2026-09-04 |
| [0003](0003-interrupt-hitl-single-outbox-writer.md) | Interrupt-based human approval, per-patient threads, derived pending status, and a single outbox writer | accepted | 2026-09-04 |
| [0004](0004-p6-access-modes-and-snapshots.md) | One HTTP client over an injected transport for P6 (http and embedded), plus committed snapshots for tests | accepted | 2026-09-04 |
| [0005](0005-public-source-rules-and-blind-gold-protocol.md) | Public-source rule text with element tags, and a blind, frozen gold protocol | accepted | 2026-09-04 |

## Conventions

- Numbering is sequential and never reused. A superseded record keeps its file; its status line
  changes to `superseded by ADR-NNNN` and the new record links back.
- Status values: `proposed`, `accepted`, `superseded by ADR-NNNN`, `deprecated`.
- Cite code by module path (`src/caregap/...`) and tests by `path::test_name`. A decision that
  the code does not implement is not accepted; record it as `proposed`.
- Keep the negative consequences honest. They are what a reader checks first.
