# Review backlog (consumed by the adversarial review gate; delete when empty)

Cross-module inconsistencies reported by the engine build agents, to be resolved at the review
gate (stage G). None blocks tests; all are documented in the modules that carry them.

1. `evidence.condition_active_in` uses `abatement >= window.start`; the SPEC CBP row says
   `abatement > my_start`. `cbp.py` implements the SPEC wording inline for its denominator.
   Reconcile the helper (and check EED/SPD/SPC callers) so one semantics is used everywhere.
2. `evidence.py` has no child-observation helper; `screening.py` (`_children_of`) and `cbp.py`
   (panel grouping) each group by `parent_observation_id` locally. Promote a shared
   `child_observations_of(record, parent_ids, window)` helper.
3. `spd.py` imports underscore-private helpers from `spc.py` (`_age_sex`, `_on_therapy`,
   `_escalations`, `_exclusion_window`, `_tri_all`). Move them to a shared `rules/statin.py`
   with public names.
4. `col.py` docstring tags "nine years prior" and "FOBT during the MY" as *quoted*; the TN corpus
   only says "appropriate screenings", so `rules/json/col.json` tags them `demo_choice`.
   Conversely COL age 50-75 is in the TN Metric (`quoted`) but `col.py` calls it `demo_choice`.
   Align the docstrings with the JSON tags (the JSON is verbatim-checked against the corpus).
5. Rule-JSON exclusion element ids (`<m>/exclusion/death`) and the rule modules' `COVERAGE`
   keys (`died_during_measurement_period`) use different vocabularies; add a parity test or a
   mapping so the UI can join coverage rows to citations.
6. EED escalation E6 implements only the HbA1c arm (no `diabetes_meds_rxnorm` value set exists).
   Either add the set from a scan or state the narrowing in `docs/MEASURES.md`.
7. SPD does not compute the public pregnancy exclusion (SPEC row lists ESRD/dialysis only) though
   `pregnancy_snomed` exists and SPC uses it. Decide and align SPEC + coverage tables.
8. Cite-only URLs for TSC (`ecqi.healthit.gov/ecqm/ep/2025/cms138v13`) and SNS (NCQA SNS-E
   page) returned 404 on 2026-09-03; NCQA per-measure links resolve to the generic library page.
   Replace with URLs that resolve before shipping (`docs/MEASURES.md` + rule JSON sources).
9. Value sets fully `untested_by_data` in the 237-patient panel: `pregnancy_snomed`, 11 of 27
   `statin_intensity` codes, `449868002` as a tobacco answer. Keep the flags; mention in README.
10. Demo storyboard: the "SPC low-intensity gap" case is not Tony at 2025-12-31 (no ASCVD dx;
    SPD closes on any intensity). Pick a panel patient for that scene or drop it.

## From the agents + graph wave (2026-09-03)

11. `llm.DRAFTER_FALLBACK == "{}"` is a VALID empty plan, so replay-fallback drafting goes
    call -> lint fail -> regen -> lint fail -> template, consuming two replay fallbacks per patient
    (the llm.py comment says "parse failure"). Decide: keep (publication still blocked when
    fallback_count > 0) and fix the comment, or use a non-JSON sentinel.
12. Two helpers named `allowed_numbers_for` exist (`agents/drafter.py` and `graph/hitl.py`) with
    different signatures. Keep one.
13. `verify_verdict` is stricter than SPEC section 4: `numerator_met` also requires the engine
    numerator to be `yes`, and `confirm_open` requires denominator `yes` + numerator `no`
    (prevents validator_unsafe_close on uncontrolled BP / low-intensity statin). Document in SPEC.
14. `after_evaluate` routes every `needs_review` candidate (not only escalated ones) to
    `validate_gaps`; non-escalated `unknown` verdicts become `engine_needs_review` items with no
    model call. Update the SPEC diagram caption.
15. `PatientRunRecord.pending` persists after completion (COALESCE); "is pending" must come from
    `list_approvals` / `hitl.pending_request`. Consider clearing it on completion instead.
16. `RunOutcome.final_statuses` uses engine vocabulary (`gap_open`, never `open`); SPEC section 6
    says OPEN. Align the wording (evals map `gap_open`/`needs_review` -> predicted gap).
17. The committed personas carry no escalations (goldens `esc=[]` for all five); validator paths
    are exercised only through synthetic factory snapshots. The gold panel must include real
    escalation carriers (E1/E3/E5/E6) or the pipeline column is untested on real records.

## From the docs pass (2026-09-04)

18. CI is a single ubuntu `quality` job: no Windows job, no coverage floor (SPEC says >= 85%), no
    `contract` job (live embedded P6 personas == snapshots), and no hygiene tests (tracked files,
    CPT URIs, MIMIC tokens, `sk-ant-`). Add before shipping.
19. `E2` is declared in `EscalationKind` but no shipped rule emits it; either implement or drop it
    from the SPEC table and README.
20. SPEC section 8/10 mention 13 personas, `scripts/slim_bundle.py`, `curate_personas.py`, and a
    MemorySaver CLI path; the code has 5 personas, no slimming scripts, and SqliteSaver in the CLI.
    Align the SPEC with the code (the docs describe the code).

## From the first eval run (2026-09-04)

21. `evals/artifacts/engine-latest.json`: `metrics.overall_dev` only carries `review_flag_rate`
    (empty when gold has no `escalate` rows, as now) and `metrics.per_split` / `per_category`
    are empty dicts. Fill them (dev P/R/F1, per-measure P/R) or drop the empty keys.
22. Engine tier scored 1.000/1.000/1.000 on 258 test items (28 gold-open) and the 12-patient
    second labeler agreed on 96/96 items. Both labelers are the same model family and applied a
    guide derived from the same rule JSON + code lists the engine uses, so agreement is expected;
    the review must confirm no leakage (labelers never saw engine output; engine ran only after
    FREEZE.json) and that CODE_AUDIT.md's 124 flagged events are adjudicated.
