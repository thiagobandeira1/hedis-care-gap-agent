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
