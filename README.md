# hedis-care-gap-agent

LangGraph care-gap closure over synthetic (Synthea) patients: a deterministic, HEDIS-aligned
measure engine, a code-verified validator, a care-action drafter, and a human-in-the-loop
approval step in front of the outbox. **Demo-grade; HEDIS-aligned; not NCQA-certified; synthetic
data only.** The contract is `docs/SPEC.md`.

(README under construction.)

## Evaluation

<!-- EVAL:BEGIN -->
_Gap-detection precision / recall: not yet measured (no eval artifact yet; run `caregap eval --tier engine` once `evals/gold/gap_cases.jsonl` exists)._
<!-- EVAL:END -->

<!-- EVAL-MEASURES:BEGIN -->
_Per-measure counts: not yet measured._
<!-- EVAL-MEASURES:END -->

The headline precision/recall measures the deterministic engine (plus code-verified validator
decisions) against hand-applied demo-grade rules; it is independent of the drafting agent. Agents
are measured separately.
