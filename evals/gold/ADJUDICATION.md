# Adjudication (dev split only; SPEC section 6, evals/README.md step 9)

- Adjudicated: 2026-09-04 (adjudicator: review-gate agent, after engine contact)
- Gold file: `evals/gold/gap_cases.jsonl`, sha256 `241ae473bf579ef465aee8b1642ec566d4a32565ee733f3690d22ae1890b53ef` (unchanged; equals `FREEZE.json.gap_cases_sha256`)
- Engine tier compared: `evals/gold/engine-outcomes.jsonl` / `evals/artifacts/engine-latest.json` (git `3ddf78ab`, 2026-09-05)
- Mapping used: engine `gap_open` -> `open`, `needs_review` -> `escalate`, `closed` / `excluded` / `not_eligible` as-is

## Counts

| item | count |
|---|---:|
| dev patients / dev units (all 8 measures) | 17 / 136 |
| dev units, six core measures | 102 |
| dev disagreements (gold vs engine) | **0** |
| dev label corrections made | **0** |
| engine findings recorded for the reviewers | 5 (plus 1 value-set/rule-text note) |
| test patients / test units | 43 / 344 |
| test disagreements listed (frozen, not changed) | **0** |
| code-audit events reviewed / value-set additions recommended | 124 / 0 |

`FREEZE.json` was left untouched because no row changed; `adjudicated_dev_corrections` is 0.

## Method

1. Joined every gold row to the engine's `final_statuses` for the same patient (60 patients, `error: false` for all). 480/480 units agree, so there was no per-unit adjudication to perform on either split.
2. Because a 100% match between a labeler and an engine that read the same rule text can hide a *shared* misreading, a third reading was made: an independent script applying `LABELING_GUIDE.md` literally over the raw snapshots (`evals/gold/snapshots/<pid>/record_2025-12-31.json.gz`, loaded through the P6 model layer only, no `caregap.measures` import). It agrees with the gold labels on 480/480 units as well.
3. The same script scanned all 60 records for every escalation trigger (E1 hospice 2024-10-03..2024-12-31, E3 stopped/cancelled statin authored in 2025, E4 dementia + IMP/EMER in 2025 at 66+, E5 incomplete/mis-united 85354-9 panel in 2025, E6 hypertension/diabetes abatement in 2025, E7 ambiguous colon code) and for the worksheet edge cases the guide flags as `uncertain` (single-eye negative prior-year exam, unknown sex).
4. Engine rule modules were read against the rule JSON and the guide, and each suspected divergence was executed against the real engine (`default_engine()`, packaged value sets) on constructed records. Those runs are the evidence for the findings below.
5. Freeze-before-engine ordering was checked from file stamps and git history.

## Dev-split adjudication

No dev unit disagrees, and the third reading found no dev label that the written rules contradict. **No label was changed.**

## Test-split disagreements (frozen)

None. (Listed for completeness: 344/344 test units agree between gold, engine and the third reading.)

## What the 480/480 agreement does and does not show

- It shows the engine applies the demo rules the way two blind readers of the same text do, on the statuses this panel exercises: `not_eligible` (253), `closed` (185), `open` (40), `excluded` (2).
- It does **not** exercise escalation. The frozen gold set has **0 `escalate` rows and 0 `uncertain` rows**, and the engine emitted **0 `needs_review`** verdicts and an empty `review_flagged` list for all 60 patients. `review_flag_rate` is therefore unmeasured (the artifact's `overall_dev` is `{}`), and every E1-E7 code path is untested on real records (REVIEW_BACKLOG item 17 is confirmed, not resolved). Why the 14 "escalation carriers" carry nothing:
  - all 13 hospice carriers have hospice events dated 2018-2024-06-01 only; the latest (`f4c8fc6f`, 2024-04-13..2024-06-01) ends four months before the E1 window opens on 2024-10-03. The selection stratum keyed on the `hospice` keyword at any date, not on the E1 window;
  - the 6 dementia carriers (`387a078e`, `473e789e`, `7e305567`, `8356c108`, `c1943780`, `e8646279`) have no IMP/EMER encounter starting in 2025, so E4 never fires;
  - no patient in the 237-patient panel carries a stopped/cancelled statin (FEASIBILITY.md), so E3 cannot occur;
  - no hypertension or diabetes condition abates inside 2025 anywhere in the 60 records (E6 never fires);
  - no 85354-9 panel in 2025 is incomplete or mis-united (E5 never fires);
  - the only E7 carrier (`d671b0c6`, test) also carries a colorectal-cancer code, so `excluded` wins by precedence and E7 is moot.
- The three labels of `c644e98b` that the guide's rules decide without a 2025 observation (`CBP open no_bp_in_my`, `TSC open`, `SNS open`) are the only gold-open units on CBP/TSC/SNS that come from *absence* of data; they agree across all three readings.

## Engine findings (recorded here, NOT fixed; for the reviewers)

Each was reproduced with `uv run python <probe>` against `default_engine()`; expected values are what `LABELING_GUIDE.md` / `rules/json/<id>.json` say.

| # | file | rule text | engine behaviour | probe result |
|---|---|---|---|---|
| F1 | `src/caregap/measures/rules/cbp.py` `_assess_bp` (lines 286-314) | `cbp/coverage/e5_incomplete_panel_or_unit`: "a BP panel in the MY missing its systolic or diastolic child ... raises a review flag"; guide 1: "any 85354-9 panel in the MY ... its mere presence in the MY escalates" | E5 is raised only for defective panels **on the most recent panel date**; a defective panel earlier in the MY is silently dropped, and defective panels at IMP/EMER encounters are dropped before the defect check. This is deliberate and locked by tests (`tests/unit/measures/test_cbp.py` cases `defective_earlier_complete_latest` -> numerator `yes`, no E5; `imp_defective_latest_ignored`) and by the cbp.py docstring, so the split is between code+tests and the rule JSON + guide, which must be aligned one way or the other | htn; panel 2025-03-02 SBP 130 / no DBP (AMB); panel 2025-09-14 138/86 (AMB) -> guide `escalate`, engine `closed`, `esc=[]`. Variant: defective panel 2025-09-14 at EMER -> guide `escalate`, engine `closed` |
| F2 | `src/caregap/measures/rules/eed.py` `_escalations` (lines 295-318) | `eed/coverage/e6_diabetes_abated_in_my`: "a diabetes condition abated inside the MY raises a review flag"; guide 2: "*any* qualifying diabetes condition has abatement dated 2025-01-01..2025-12-31 -> escalate" | E6 additionally requires an HbA1c (`hba1c_loinc`) observation in the MY; without one the abatement is ignored | 44054006 onset 2012, abatement 2025-05-05; 722161008 on 2025-04-10; no HbA1c -> guide `escalate`, engine `closed`, `esc=[]`. Adding a 4548-4 result in 2025 -> engine `needs_review` `esc=['E6']` |
| F3 | `src/caregap/measures/rules/cbp.py` `_exclusions` (lines 402-415), constants lines 104-105 | `cbp/exclusion/pregnancy` rationale: "pregnancy_snomed condition active in the MY"; guide 1 lists only conditions 72892002 / 77386006 | An additional exclusion category `pregnancy_status_positive` fires on LOINC 82810-3 with value 77386006 in the MY; this reader is documented only in the cbp.py docstring, not in the rule JSON, the guide, or `docs/MEASURES.md` | female 30, htn, 120/80 in 2025, observation 82810-3 = 77386006 on 2025-06-01, no pregnancy condition -> guide `closed`, engine `excluded` (`pregnancy_status_positive`) |
| F4 | `src/caregap/measures/rules/global_rules.py` `global_escalations` (lines 116-120) | `*/coverage/e4_advanced_illness_hint`: "dementia diagnosis or dementia medication plus an inpatient/ED encounter in the MY at age 66+"; guide 0.4: dementia dx "any date on/before as_of" | Dementia conditions are filtered with `condition_active_in(c, my)`, so a dementia diagnosis abated before the MY does not raise E4 (dementia medications are not filtered) | 26929004 onset 2015 abatement 2020; IMP encounter 2025-03-03; age 70 -> guide `escalate`, engine `closed`, `esc=[]` |
| F5 | `src/caregap/measures/rules/eed.py` `_numerator` (lines 243-263) | guide 2: the negative answer must be "on the same date" as the prior-year exam; rule JSON `eed/numerator/prior_year_negative_exam` says the exam "carries" the answer (no date) | Any LA18643-9 answer anywhere in MY-1 plus any un-blocked MY-1 exam closes the numerator; the answer and the exam are never matched by date | 722161008 on 2024-06-01; 71490-7 = LA18643-9 on 2024-09-01; no 2025 exam -> guide `open` (+`uncertain`), engine `closed` |

Rule-text ambiguities documented, label kept (no gold unit is affected):

- **A1** `cbp/denominator/hypertension_active` says abatement `> Jan 1 of the MY` while `cbp/coverage/e6_hypertension_abated_in_my` says an abatement inside the MY (which includes Jan 1) keeps the member in the denominator. On abatement == 2025-01-01 the two elements contradict; the engine returns `not_eligible` (denominator `no` wins). EED's denominator uses `>=` (`evidence.condition_active_in`), so the two measures treat the same edge differently (REVIEW_BACKLOG item 1 confirmed; the guide states each measure's edge correctly, so no label is affected).
- **A2** F5 above is partly a text gap: the rule JSON does not say "same date"; the guide added it. Decide which reading is the rule, then align the other.
- **A3** `value_sets/retinopathy_negative_loinc.json` note says "Both eyes must answer LA18643-9 for the EED prior-year exam to count", while the rule JSON, the guide and the engine treat one eye as enough. The note is stale.
- **A4** `spc/denominator/ascvd_onset` rationale claims the public PCI/CABG event look-back "needs claims"; the 60 snapshots carry 11 PCI (415070008) and 10 CABG (232717009 / 418824004) procedure rows, so the data does represent those events. Not label-affecting (see code audit), but the rationale is factually wrong.

## Code audit decisions (`evals/gold/CODE_AUDIT.md`, 124 flagged events)

Co-occurrence was computed over all 60 gold snapshots.

| code / concept | flagged | carriers in gold | decision |
|---|---|---|---|
| 274531002 "Abnormal findings diagnostic imaging heart+coronary circulat" (condition) as ASCVD | 47 rows (SPC/SPD) | 44 patients, **44/44 also carry a listed `ascvd_snomed` code** (Synthea emits it in the same CAD module as 414545008) | **Do not add.** It is an imaging finding, not a clinical ASCVD diagnosis; zero label impact. |
| 33367005 Angiography of coronary artery (procedure) | 20 rows | 20 patients, 20/20 with listed ASCVD | **Do not add.** A diagnostic procedure that can be negative; not ASCVD evidence by itself. |
| 415070008 PCI, 232717009 CABG, 418824004 off-pump CABG (procedures) | 21 rows | 11 / 6 / 4 patients, all with listed ASCVD (399261000 or 414545008) | **No addition required for fidelity** (every carrier already qualifies). Optional widening: the public SPC denominator does count PCI/CABG events; if the rule is ever widened, add a P1 `ascvd_procedures_snomed` set as a tagged `demo_choice`, and fix the A4 rationale either way. |
| 314971001 Camera fundoscopy (procedure) as a retinal exam | 15 rows (EED) | 13 patients, 157 occurrences, **0 not on the same day as a listed 722161008/700070005** | **Do not add now** (zero label impact; `retinal_exam_proc` is P6-vendored and parity-tested). Note for real-world bundles: a standalone fundoscopy would be missed. |
| 710824005 Assessment of health and social care needs (procedure) as SNS screening | 2 rows | 59 patients in the MY, **0 without a 93025-5 PRAPARE in the MY** | **Do not add.** The rule pins the PRAPARE panel; zero label impact. |
| 866148006 Screening for domestic abuse | 1 row | - | **Do not add**; domain screen, `not_representable` by design. |
| 305432006 Admission to surgical transplant department (encounter type) as kidney transplant | 1 row (56633e5f CBP) | patient already carries 161665007 | **Do not add**; not organ-specific, and the listed condition already excludes. |
| 424619006 Prenatal visit encounter type (1993) as pregnancy | 2 rows (c7d16647) | - | **Do not add**; an encounter type, 32 years outside every window. |

Net: no value set gains a code from this audit.

## Leakage / ordering check (REVIEW_BACKLOG item 22)

- `gap_cases.jsonl`, `FREEZE.json`, `second_labeler.jsonl`, `CODE_AUDIT.md`: written 2026-09-05 01:23:52 UTC; `engine-outcomes.jsonl`: 01:24:15 UTC; `engine-latest.json`: 01:24:19 UTC. `FREEZE.json.git_sha` = `3ddf78a` (the commit holding selection, snapshots, worksheets and guide); the frozen labels and the engine outcomes were committed together afterwards in `13dff7d`.
- The ordering is consistent with "freeze, then engine", but the margin is 23 seconds inside one automated workflow; the stamps prove sequence, not isolation. The labeler-A / labeler-B agreement of 96/96 says nothing about leakage (same rule text, same code lists, same model family). The strongest independent evidence that the labels are not engine copies is the third reading above, which never touched the engine and also agrees 480/480.
- The stated human spot-check is still `pending`; this adjudication is not a human review.

## REVIEW_BACKLOG items touched by this lens

- 1 confirmed (A1), no label impact. 6 confirmed and sharpened: the HbA1c arm is not merely a narrowing of the "meds" trigger, it contradicts the rule JSON text (F2). 7 confirmed: SPD does not compute pregnancy (`spd.py` coverage `"pregnancy": "not_representable"`), and the guide says the same, so labels agree. 17 confirmed: zero escalation units in gold. 21 confirmed: `metrics.overall_dev`, `per_split`, `per_category` are empty. 22: see the leakage section.
