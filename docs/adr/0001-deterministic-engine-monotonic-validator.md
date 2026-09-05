# ADR-0001: Deterministic measure engine with a monotonic, code-verified validator

- Status: accepted
- Date: 2026-09-04
- Scope: `src/caregap/measures/`, `src/caregap/agents/validator.py`, `src/caregap/agents/packet.py`, `src/caregap/graph/edges.py`, `src/caregap/evals/`
- Related: `docs/SPEC.md` sections 1, 2, 4, 6; ADR-0002 (the one status-reading rule); ADR-0005 (what the engine is measured against)

## Context

The sibling `fhir-feature-service` (P6, pinned `v0.1.0`) serves canonical patient rows and a
feature table but deliberately delegates measure logic to this repository. Two ways to build a
care-gap agent were on the table: let a language model read the record and decide which gaps
are open, with code as a guardrail; or let code decide and confine the model to a narrow,
verifiable role.

Three facts settled it:

1. The headline metric is gap-detection precision and recall against a frozen gold set
   (ADR-0005). If a model can open or close gaps, precision and recall stop describing anything
   stable: they move with the prompt, the model version, and sampling.
2. Public HEDIS-aligned rule text is a set of dates, windows, code memberships and age bands.
   Those are exactly the things code evaluates deterministically and a model evaluates
   unreliably.
3. Some situations cannot be resolved from the record alone (prior hospice that may continue into
   the year, a stopped statin next to an active one, an incomplete blood-pressure panel, an
   ambiguous colon code). A human must see them. A model can help triage them, but only if every
   decision it makes can be checked by code against the same evidence it was shown.

## Decision

**The engine is code and owns every clinical decision.** `MeasureEngine.evaluate_one`
(`src/caregap/measures/engine.py`) runs one rule per measure
(`src/caregap/measures/rules/{cbp,eed,bcs,col,spc,spd,screening}.py`). A rule returns a
`RuleOutput`: denominator and numerator as three-valued `TriResult`s, coded `ExclusionHit`s, a
coverage table, and measure-scoped `EscalationFlag`s. The engine prepends the global rules
(`rules/global_rules.py`: death and hospice in the measurement year as exclusions, E1 and E4 as
global escalations), resolves the verdict with the single function
`caregap.measures.tri.resolve` (exhaustively tested over Tri^3 x escalation in
`tests/unit/measures/test_tri.py`), and scores priority with `measures/priority.py`
(`star_weight x clinical_weight x time_pressure`; rank by score, ties by measure id). Rules read
the record only through `measures/evidence.py`; `FeatureRow` never enters a rule
(`tests/unit/measures/test_cbp.py::test_rule_never_reads_status_fields_or_features`).

**The validator is monotonic and code-verified.** `ValidationVerdict.decision` is one of
`confirm_open | exclude | numerator_met | needs_human` (`src/caregap/agents/schemas.py`). The
model sees only an `EvidencePacket` built by code (`agents/packet.py::build_packet`): the tagged
rule text, the engine's findings, the exclusion categories with their value sets and windows,
and an evidence table that is an allowlist projection of the record (every event whose code
belongs to a value set the measure references, plus every event the engine cited). Record
strings are confined to one fenced `data` block that the prompt declares to be data, never
instructions (`tests/unit/agents/test_packet.py::test_injection_probe_is_rendered_verbatim_inside_the_data_block_only`).

`agents/validator.py::verify_verdict` is pure code and rewrites any unverifiable decision to
`needs_human` with `verified=False`:

- every cited `evidence_id` must resolve in the packet;
- `exclude` needs a category listed in the packet and a cited event tagged with that category's
  value set, dated inside that category's window;
- `numerator_met` needs a cited event tagged with a numerator value set inside the numerator
  window, and the engine's own numerator must already be `yes` (every in-set, in-window event was
  evaluated by the engine; a citation on a numerator `no` can only be a re-reading of rejected
  evidence such as an uncontrolled panel or a low-intensity statin);
- `confirm_open` needs the engine's denominator `yes` and numerator `no`;
- confidence `low` routes to a human; a `closed`, `excluded` or `not_eligible` candidate admits
  only `needs_human`; a measure-id mismatch is never trusted.

`resolve_candidate` then lets only a verified `exclude` or `numerator_met` change the engine's
status. The validator can never add a gap.

**The validator runs only on escalated candidates.** `graph/edges.py::after_evaluate` routes a
patient to `validate_gaps` only when some candidate carries an escalation or a `needs_review`
verdict; clean open gaps go straight to drafting. Inside `graph/nodes.py::validate_gaps`, a
non-escalated `needs_review` (an `unknown` denominator or numerator) becomes an
`engine_needs_review` item with no model call, and `validation_mode="off"` turns every
escalated candidate into an `escalated:<kinds>` review item, again with no model call. A model
failure (`AgentOutputError` from `caregap.structured.StructuredCaller`, or any exception) becomes
a `validator_failed` review item: the graph fails closed to a human.

**The drafter writes language only.** `agents/drafter.py` builds the plan from the open-gap list
and a minimal context; code owns the gap set, the rank, the action ids and the lint gate
(`lint_plan`: gap-id set equality, no codes or event ids in the patient message, numbers limited
to the evidence numbers, forbidden phrases, "cancer" only in screening phrases, length caps).
One regeneration, then the deterministic `agents/template_drafter.py::TemplateDrafter` takes
over with `draft_error` set.

**The claim is falsifiable by construction.** The eval tiers (`src/caregap/evals/outcomes.py`)
run the same graph twice: `validation_mode="off"` (engine tier, the engine-only column) and
`validation_mode="escalated"` (pipeline tier). `evals/scoring.py` publishes both columns plus two
lower-is-better counts: `validator_unsafe_close` (gold-open units the validator excluded or
closed) and `unresolved_evidence` (verdicts that came back unverified). All four headline
numbers and both counts are ratchet-gated in `evals/gates.py`. The README carries the pipeline
column labelled `Engine+validator` beside the `Engine-only` comparison
(`caregap.evalrun.COMPARISON`).

## Consequences

### Positive

- Precision and recall describe code that can be read, diffed and regression-tested; the
  committed engine goldens (`tests/unit/measures/goldens/`, byte-compared in
  `tests/unit/measures/test_goldens.py`) and `evals/gold/engine-outcomes.jsonl` (byte-compared
  by the engine tier) make any change to clinical logic visible in review.
- Every model decision that changes an outcome is checkable after the fact: the verdict cites
  event ids the reviewer can open, and `verification_note` records why code accepted or
  downgraded it.
- CI needs no model key: the engine tier runs live; the validator and drafter run under fakes
  and replays through the same `StructuredCaller` parser as the real models.
- The validator's contribution is measured, not assumed. If it hurts, the two columns and
  `validator_unsafe_close` show it and the gate fails.

### Negative

- The recall ceiling is the engine's. A false negative in a rule cannot be rescued by the
  validator, by design; it can only be fixed in code and shows up in the next eval run.
- The validator's useful surface is narrow. With `verify_verdict` stricter than SPEC section 4
  (`numerator_met` requires an engine numerator of `yes`; `confirm_open` requires denominator
  `yes` and numerator `no`; `docs/REVIEW_BACKLOG.md` item 13), it can confirm an escalated open
  gap, exclude on in-window coded evidence, or clear an escalation on a numerator the engine
  already met. Everything else is a human's.
- Every escalated candidate costs a model call and a human-readable review item; on records with
  many escalations the review queue, not the model, is the bottleneck.
- Demo-grade rule fidelity bounds what the headline can mean: the numbers measure agreement
  with the written demo rules, not with NCQA specifications or clinical truth (ADR-0005).
- Three-valued logic pushes ambiguity to `needs_review`, which the publication scoring counts as
  a predicted gap; the strict variant (`needs_review` counts as no gap) is published beside it
  so the trade-off is visible rather than hidden.

## Alternatives considered

1. **Model-first gap detection with code guardrails.** Rejected. Precision and recall would
   attach to a prompt and a model version rather than to reviewable logic, and every prompt
   edit would silently move clinical behaviour.
2. **A validator allowed to open gaps or return free-form verdicts.** Rejected. A hallucinated
   gap becomes patient outreach; monotonicity (the model can only narrow what code proposed,
   never widen it) is the property that makes the approval step meaningful.
3. **No validator at all.** Kept as the engine-only ablation column. It is not the product
   because escalations E1-E7 exist precisely where the record needs a reader of context that
   the engine cannot resolve deterministically; routing all of them to humans unassisted would
   make the review queue the whole product.
4. **Provider-native structured output (tool calling) per model.** Rejected in favour of one
   `StructuredCaller` path (`invoke -> clinevals.extract_json_object -> model_validate`, one
   correction turn, then fail closed) so that fakes, replays, recordings and real models all
   exercise the same parser and retry branch in CI.
