# ADR-0002: The statin on-therapy rule reads `MedicationRequest.status`

- Status: accepted
- Date: 2026-09-04
- Scope: `src/caregap/measures/rules/spc.py`, `src/caregap/measures/rules/spd.py`, `src/caregap/p6/models.py::MedicationEvent`, `src/caregap/measures/rules/json/{spc,spd}.json`
- Related: `docs/SPEC.md` section 2 (SPC and SPD rows, E3); ADR-0001; ADR-0005

## Context

The public SPC numerator (CMS 2026 Star Ratings Technical Notes, C19) counts members "dispensed
at least one high or moderate-intensity statin medication during the measurement year"; SPD
(D12) counts a statin "fill". Neither dispensing nor fills are observable here: P6 carries
`MedicationRequest` rows, not pharmacy claims, and the Synthea bundles carry no
`MedicationDispense`. The only medication signal is a request with a code, an `authored_date`
and a `status`.

P6's doctrine is date-only: every canonical section is filtered on its primary event date
(`?to=as_of`), and this repository's `caregap.p6.client.mask_as_of` extends that to future
death, abatement and end dates. No rule in this repository reads a condition's
`clinical_status` or `verification_status`, and no rule other than the one described here
reads a medication's `status`
(`tests/unit/measures/test_cbp.py::test_rule_never_reads_status_fields_or_features`;
`tests/unit/measures/test_screening.py::test_unrelated_sections_and_medication_status_never_matter`).

A purely date-based statin rule ("a statin request authored inside the measurement year") fails
on this data. Synthea authors most long-term medications once, at initiation, and never again
(`docs/SPEC.md` section 12: "statins authored once"; `src/caregap/measures/value_sets/SCAN.md`
shows the statin request rows concentrated on a handful of codes). Under a date-only rule every
member on a long-standing statin authored before the measurement year would be reported as a
gap, and the drafter would tell them to start a medication they already take.

## Decision

**On therapy in the measurement year** (`src/caregap/measures/rules/spc.py::_on_therapy`,
reused by `spd.py`) means: a `statin_rxnorm` `MedicationRequest`

- authored in `[my_start, as_of]`, whatever its status, unless the status is one of
  `NEVER_COUNT_STATUSES = {"stopped", "cancelled", "entered-in-error"}`; or
- authored before `my_start` with `status == "active"` (`ACTIVE_STATUS`).

Any other status on a pre-MY request (`completed`, `on-hold`, `unknown`, `draft`, missing) does
not count. SPC then requires an intensity of `moderate` or `high` from
`value_sets/statin_intensity.json` (public ACC/AHA table); only-low therapy yields numerator
`no` with subtype `low_intensity_only`; a statin whose code has no intensity entry yields
`unknown` (`needs_review`). SPD accepts any intensity.

**E3 `medication_status_conflict`** (`spc.py::_escalations`, shared by SPD): a statin authored
inside the measurement year whose status is in `CONFLICT_STATUSES = {"stopped", "cancelled"}`
raises a measure-scoped escalation, even when another statin closes the numerator. The verdict
algebra promotes the candidate to `needs_review`, and the validator (ADR-0001) can only confirm
it open, close it on a tagged in-window citation when the engine numerator is already `yes`, or
hand it to a human (`tests/unit/agents/test_validator.py::test_e3_candidate_with_qualifying_statin_closes_only_with_a_tagged_in_window_citation`,
`::test_low_intensity_statin_cannot_be_closed_by_the_validator`).

**These are the only readers of `MedicationEvent.status`.** The field's docstring in
`src/caregap/p6/models.py` says so, and the rule JSON elements
`spc/numerator/on_therapy_status`, `spd/numerator/on_therapy_status`,
`spc/coverage/e3_medication_status_conflict` and `spd/coverage/e3_medication_status_conflict`
are tagged `demo_choice` and cite this ADR (`docs/MEASURES.md`). The choice is an explicit,
documented deviation from P6's date-only doctrine, confined to two rules and one flag.

## Leakage caveat

`MedicationRequest.status` is not date-versioned in Synthea or in P6. A request row carries one
status: the status at the time the bundle was generated. P6's `?to=as_of` filter keeps or drops
the row by `authored_date`; it cannot rewind the status, and `mask_as_of` has no date to mask.

Consequences for a retrospective run (the eval anchor `as_of = 2025-12-31` over snapshots):

- A statin authored before the measurement year and stopped **after** `as_of` shows as
  `stopped`, so the member looks off therapy for a year in which they were on it. The error
  points toward a false open gap (or an E3 review), never toward a false closure; it inflates
  open counts and lowers engine precision on SPC and SPD.
- A statin authored inside the measurement year and stopped after `as_of` counts as on therapy
  (authored in window) but also raises E3, so the case reaches a human.
- Nothing in the record says when a status changed, so no rule can be written that is correct
  at every `as_of`; the demo picks the reading that is safe for outreach (never tell a member
  to start a statin they are recorded as taking) and surfaces the rest.

The gold labels (ADR-0005) are produced from worksheets that show the same status column, by
labelers applying the same written rule. The leak therefore affects the engine and the gold
identically, and the headline measures fidelity to the written rule, not the clinical truth of
whether the member was on therapy. `docs/SPEC.md` section 12 and `evals/README.md` say this
plainly.

## Consequences

### Positive

- Members on a stable, long-standing statin are not chased to start one: on this data the
  date-only rule would have made SPC and SPD nearly all false gaps.
- A stop or cancellation inside the year is never silently ignored: E3 puts it in front of a
  human with the request ids cited.
- The deviation is small, named, and testable: `NEVER_COUNT_STATUSES`, `CONFLICT_STATUSES`,
  `ACTIVE_STATUS` are module constants; the status paths are table-driven in
  `tests/unit/measures/test_spd.py::test_numerator_statin_status_paths` and
  `::test_escalation_e3_statin_status_conflict`.

### Negative

- The leakage above: retrospective SPC and SPD numbers carry post-`as_of` information about
  stops, in the direction of extra open gaps.
- Status vocabulary dependence: only `active` rescues a pre-MY request. A real FHIR server that
  marks a long-term prescription `completed` when the order period ends would make every such
  member a gap; the rule would need the server's status semantics before reuse outside Synthea.
- A `stopped` statin authored before the year, followed by nothing, is indistinguishable from a
  member who never took one: the engine says gap, which is correct for outreach and wrong for a
  "was on therapy at some point" reading of the public measure.
- The rule cannot be reconciled with P6's doctrine without P6 versioning statuses, which is out
  of scope for the pinned `v0.1.0`.

## Alternatives considered

1. **Date-only: count only requests authored inside the measurement year.** Rejected. On
   Synthea data this reports nearly every established statin user as a gap and the drafter would
   send "start a statin" messages to members already on one.
2. **Extend the look-back (for example, any statin authored in the last N years) without reading
   status.** Rejected. It counts therapy that was stopped or cancelled years ago and cannot say
   which; the leak becomes a systematic false closure, the direction the product must never take.
3. **Treat every pre-MY statin as `unknown` and route the member to review.** Rejected. It
   floods the review queue with the most common case (stable therapy) and defeats the point of
   a deterministic verdict; E3 already routes the genuinely conflicting cases.
4. **Require `MedicationDispense` or `MedicationAdministration` evidence.** Not available: the
   Synthea bundles and P6's canonical rows carry neither.
5. **Ask P6 to version `status` by date.** Deferred; it would need a schema change in a pinned
   dependency and Synthea does not emit status history to version.
