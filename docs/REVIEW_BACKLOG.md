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

## Review gate 2026-09-05 (7 lenses + adjudication; 3 refuters per finding, majority rule)

Findings: 28 confirmed (every one reproduced by a verifier), 3 refuted, 52 unverified (their verifiers were cut off by the session limit). Fixers did not run.

### Resolved at the measure-rules fix pass (2026-09-07)

Fixed (each with a regression test; goldens regenerated - only the coverage tables of the five
persona goldens changed, no persona / gold-panel verdict changed, `caregap eval --tier engine
--gate` still PASS at 1.000/1.000/1.000):

- ~~C4~~ / ~~C13~~ CBP E5: the engine's "most recent date decides" semantics is the SPEC's;
  `cbp.json` e5 element now says exactly that (earlier defective panels ignored; a defective
  panel on the latest date makes the numerator unknown even beside a complete one).
- ~~C5~~ / ~~C12~~ EED E6 = diabetes abated in [my_start, as_of] (as CBP); HbA1c results are
  attached as evidence only; no diabetes-medication set exists (stated in `eed.json`).
- ~~C6~~ / ~~C10~~ / ~~C11~~ pregnancy: one shared `evidence.pregnancy_hits` (condition active
  OR LOINC 82810-3 = 77386006) used by CBP (MY), SPC and SPD (MY or prior year); new
  `*/exclusion/pregnancy_status_observation` demo elements; SPD `pregnancy` now observable.
- ~~C8~~ hospice episodes: `performed_end_date` / encounter `end_ts` inside [my_start, as_of]
  is hospice-in-MY (exclusion); an end inside the 90-day lookback is E1.
- ~~C9~~ / ~~C18~~ / backlog #1: ONE abatement semantics (`abatement > window.start`, the SPEC
  CBP row) in `evidence.condition_active_in`; CBP uses the helper; EED/SPD/SPC/E4 boundary
  tests flipped accordingly and the rule JSON texts say "abated ON the first day is not active".
- ~~C14~~ / ~~C21~~ E4: dementia dx active in the MY OR dementia medication authored in MY or
  prior year (one window, documented as demo_choice in every `*/coverage/e4_advanced_illness_hint`).
- ~~C15~~ / ~~C27~~ EED prior-year negative answer must be dated on/after the exam inside MY-1
  (and no retinopathy dx <= exam); new open-gap reason when every answer predates the exam.
- ~~C16~~ `spc.json` ascvd_onset rationale no longer claims PCI/CABG "need claims".
- ~~C22~~ SPD ESRD/dialysis window retagged `demo_choice` (quote kept) with the widening rationale;
  `ExclusionHit.source` and the packet category table follow.
- ~~C23~~ `col.py` docstring tags now match `col.json`.
- ~~C24~~ / backlog #5: `rule_text.COVERAGE_ELEMENTS` joins every rule `COVERAGE` key to a rule
  JSON element id; parity test in `tests/unit/measures/test_rule_json.py`; TSC/SNS now carry
  their own coverage tables and palliative / I-SNP / frailty elements.
- ~~C25~~ / backlog #19: `E2` dropped from `EscalationKind`, SPEC and README.
- ~~C26~~ / backlog #2 / #3: `rules/statin.py` (public names) shared by SPC and SPD;
  `evidence.child_observations_of` shared by CBP and the screening rule.
- ~~C28~~ engine: a global death / hospice exclusion wins over an `unknown` denominator
  (unknown birth date) -> `excluded`; measure-level exclusions still need a definite denominator
  (SPEC section 2 global-rules paragraph updated).

Left for the orchestrator: `scripts/gold.py` (gold trigger facts, C1 territory) still derives
E4 "active" with `abatement >= my_start`; the engine now uses `>`; the boundary case did not
occur in the 60-patient panel (engine outcomes unchanged) but the labeler script should be
aligned before the next freeze.

### Confirmed (fix, with a regression test each)

| # | sev | lens | finding | where |
|---|---|---|---|---|
| C1 | high | adjudication | Gold set exercises zero escalation units; E1-E7 and review_flag_rate are unmeasured | `scripts/gold.py:499` |
| C2 | high | hitl | Two concurrent decisions on one pending thread are both accepted (approve writes outbox while reject is recorded) | `src/caregap/graph/runner.py:152` |
| C3 | high | hitl | Resume timeout: abandoned worker writes the outbox after the ledger and decision record say 'error' | `src/caregap/graph/runner.py:247` |
| ~~C4~~ (resolved 2026-09-07) | medium | adjudication | CBP E5 ignores defective BP panels that are not on the most recent date | `src/caregap/measures/rules/cbp.py:288` |
| ~~C5~~ (resolved 2026-09-07) | medium | adjudication | EED E6 silently requires an HbA1c in the MY that the rule text does not mention | `src/caregap/measures/rules/eed.py:302` |
| ~~C6~~ (resolved 2026-09-07) | medium | adjudication | CBP applies an undocumented pregnancy-status observation exclusion (LOINC 82810-3) | `src/caregap/measures/rules/cbp.py:402` |
| C7 | medium | hitl | Start timeout leaves a resumable interrupt that no ledger view lists; approving it flips an 'error' patient to 'completed' | `src/caregap/graph/runner.py:207` |
| ~~C8~~ (resolved 2026-09-07) | medium | leakage | Hospice episode that starts before the MY and ends inside it is neither excluded nor E1-flagged | `src/caregap/measures/rules/global_rules.py:42` |
| ~~C9~~ (resolved 2026-09-07) | medium | rules | Abatement on the window's first day means opposite things inside CBP (and vs the EED/SPD/SPC helper) | `src/caregap/measures/rules/cbp.py:181` |
| ~~C10~~ (resolved 2026-09-07) | medium | rules | Pregnancy recorded the Synthea way (LOINC 82810-3 = 77386006) excludes in CBP but not in SPC | `src/caregap/measures/rules/spc.py:257` |
| ~~C11~~ (resolved 2026-09-07) | medium | rules | SPD computes no pregnancy exclusion and mis-tags it not_representable although pregnancy_snomed exists and SPC uses it | `src/caregap/measures/rules/spd.py:231` |
| ~~C12~~ (resolved 2026-09-07) | medium | rules | eed.json E6 text describes a rule the engine does not implement (HbA1c conjunct missing from text; meds arm missing from code) | `src/caregap/measures/rules/eed.py:302` |
| ~~C13~~ (resolved 2026-09-07) | medium | rules | cbp.json E5 text contradicts the engine on when E5 fires and what a defective panel does to the numerator | `src/caregap/measures/rules/cbp.py:286` |
| ~~C14~~ (resolved 2026-09-07) | low | adjudication | Global E4 drops dementia diagnoses abated before the MY, contrary to the rule text | `src/caregap/measures/rules/global_rules.py:119` |
| ~~C15~~ (resolved 2026-09-07) | low | adjudication | EED prior-year negative answer is not matched to the exam date | `src/caregap/measures/rules/eed.py:243` |
| ~~C16~~ (resolved 2026-09-07) | low | adjudication | spc.json rationale claims PCI/CABG events 'need claims' although the snapshots carry them; CBP/EED abatement edge contradicts itself | `src/caregap/measures/rules/json/spc.json:72` |
| C17 | low | leakage | Panel 'deceased' flag is not as_of-aware and can contradict the as_of-masked verdicts shown next to it | `src/caregap/p6/snapshot.py:74` |
| ~~C18~~ (resolved 2026-09-07) | low | leakage | Backlog #1 confirmed: abatement exactly on the window start is 'active' for EED/SPD/SPC exclusions but 'inactive' for the CBP denominator; the frozen gold mirrors both | `src/caregap/measures/evidence.py:100` |
| C19 | low | leakage | ADR-0002 leak direction confirmed; a pre-MY statin marked 'stopped' after as_of becomes a plain gap_open with no E3, contrary to the ADR's '(or an E3 review)' wording | `src/caregap/measures/rules/spc.py:142` |
| C20 | low | leakage | Backlog #22 (leakage half) confirmed as far as this checkout allows: labelers saw as_of-masked worksheets and descriptive-fact selection; freeze-before-engine ordering is consistent with timestamps but not provable without history | `scripts/worksheet.py:268` |
| ~~C21~~ (resolved 2026-09-07) | low | rules | E4 dementia-medication arm has no date window while the dementia-condition arm is windowed to the MY | `src/caregap/measures/rules/global_rules.py:131` |
| ~~C22~~ (resolved 2026-09-07) | low | rules | SPD ESRD/dialysis window is tagged quoted but is wider than the quoted D12 text | `src/caregap/measures/rules/spd.py:19` |
| ~~C23~~ (resolved 2026-09-07) | low | rules | col.py docstring tags disagree with col.json tags (backlog item 4 confirmed) | `src/caregap/measures/rules/col.py:10` |
| ~~C24~~ (resolved 2026-09-07) | low | rules | Coverage tables use three vocabularies and cannot be joined to rule JSON element ids (backlog item 5 confirmed) | `src/caregap/measures/rules/col.py:76` |
| ~~C25~~ (resolved 2026-09-07) | low | rules | E2 is declared but never emitted (backlog item 19 confirmed) | `src/caregap/measures/models.py:16` |
| ~~C26~~ (resolved 2026-09-07) | low | rules | Shared statin and child-observation helpers live in the wrong modules (backlog items 2 and 3 confirmed) | `src/caregap/measures/rules/spd.py:82` |
| ~~C27~~ (resolved 2026-09-07) | low | rules | EED prior-year negative result is not linked to the exam that closes the numerator | `src/caregap/measures/rules/eed.py:243` |
| ~~C28~~ (resolved 2026-09-07) | low | rules | A member who died in the MY with unknown birth date becomes a needs_review candidate with priority instead of excluded | `src/caregap/measures/engine.py:78` |

### Unverified (re-verify before fixing; ordered as reported)

| # | finding | where |
|---|---|---|
| U1 | Global 'hold all' review item is bypassed by revise + resolution-to-open; outreach reaches the outbox with the global item unresolved | `src/caregap/graph/nodes.py` |
| U2 | finalize applies only the last decision's review_resolutions; resolutions sent with a revise are lost and reopened measures stay 'needs_revi | `src/caregap/graph/nodes.py` |
| U3 | Supersession has no run ordering: finalizing an OLDER run marks the NEWER run's pending request superseded | `src/caregap/graph/runstore.py` |
| U4 | A node failure after the interrupt is resumed (e.g. outbox sink error in finalize) strands the thread: decision stored as 'error', retry is  | `src/caregap/graph/runner.py` |
| U5 | start() on a thread that already has a checkpoint re-drives from START and duplicates reducer lists (trace, decisions, verdicts, agent_error | `src/caregap/graph/runner.py` |
| U6 | No reconciliation of 'running' run/patient rows after a process restart; runs stay 'running' forever and cancel reports 'cancelling' indefin | `src/caregap/runtime.py` |
| U7 | ReviewItem.reason echoes unbounded model-authored strings (evidence_ids, exclusion_category, rule_citation) into the approval request and le | `src/caregap/graph/nodes.py` |
| U8 | Backlog #14 confirmed: every needs_review candidate (not only escalated ones) is routed to validate_gaps | `src/caregap/graph/edges.py` |
| U9 | Backlog #15 confirmed: PatientRunRecord.pending persists after completion and is served as-is by the API | `src/caregap/graph/runstore.py` |
| U10 | Backlog #16 confirmed: final_statuses uses engine vocabulary ('gap_open'), never 'open' | `src/caregap/graph/nodes.py` |
| U11 | Backlog #12 dismissed: only one allowed_numbers_for remains | `src/caregap/graph/hitl.py` |
| U12 | Backlog #11 confirmed: DRAFTER_FALLBACK '{}' is a valid empty plan, so replay fallback costs two lint failures per draft node, not a parse f | `src/caregap/llm.py` |
| U13 | finalize re-keys validator verdicts by the model-supplied measure_id and rewrites other measures' final status | `src/caregap/graph/nodes.py` |
| U14 | VALIDATOR_FALLBACK hard-codes measure_id "CBP": every replay fallback on a non-CBP case corrupts CBP's final status | `src/caregap/llm.py` |
| U15 | verify_verdict's exclude check is section-agnostic, so the validator can exclude on evidence the rule deliberately ignores (CBP dialysis con | `src/caregap/agents/validator.py` |
| U16 | Model-authored validator strings (unbounded) reach the drafter prompt outside the data fence and the provider note | `src/caregap/agents/drafter.py` |
| U17 | patient.sex is a free P6 string rendered outside both packets' data fences | `src/caregap/agents/packet.py` |
| U18 | lint_plan's forbidden-phrase list does not block undisclosed diagnoses, most dose instructions, or spelled-out readings | `src/caregap/agents/drafter.py` |
| U19 | Lint and the edited-plan re-lint never inspect action detail, provider note, or rationale content; a patient-owned action with dose instruct | `src/caregap/agents/drafter.py` |
| U20 | parse_headers reads assistant turns, so the correction-turn recording/replay can be keyed by whatever the model echoed; both attempts collap | `src/caregap/fakes.py` |
| U21 | Prompt-sha drift and packet-content drift never block pipeline publication; recordings carry only the system-prompt sha | `src/caregap/evalrun.py` |
| U22 | Backlog #17 confirmed: the gold panel has zero escalations/needs_review, so the pipeline tier never calls the validator and the Engine+valid | `evals/gold/engine-outcomes.jsonl` |
| U23 | Backlog #12 dismissed; #13 and #14 confirmed with code locations (no behavioural defect, SPEC wording needs updating) | `src/caregap/agents/validator.py` |
| U24 | Streamlit approval console binds all interfaces with no authentication (caregap ui) | `src/caregap/cli.py` |
| U25 | UI sidebar 'API base URL' is unrestricted: server-side request forgery with 500-char error-body echo | `src/caregap/ui/app.py` |
| U26 | LangSmith tracing is never disabled: env vars alone ship full patient-record state to a cloud endpoint, including under fake/replay models a | `src/caregap/runtime.py` |
| U27 | patient_id is interpolated unquoted into P6 URLs and snapshot paths; POST /v1/runs accepts arbitrary strings | `src/caregap/p6/http.py` |
| U28 | caregap serve runs uvicorn with default stdlib logging: access lines and full tracebacks bypass the structlog allowlist; SPEC's log-safety t | `src/caregap/cli.py` |
| U29 | Unbounded request inputs: as_of before year 10 yields 500s and error rows with no load_error; decision/note/feedback/max_revisions have no c | `src/caregap/api/routes_patients.py` |
| U30 | No hygiene test exists; .env.example contains `sk-ant-`; .gitignore negations re-allow data files under fixture dirs; CI guard is a single r | `.env.example` |
| U31 | Committed fixtures keep quasi-identifiers the code deliberately strips (city/state/postal_code/race/ethnicity; raw Synthea names/addresses/S | `scripts/gold.py` |
| U32 | Supply-chain posture: CI installs without --locked, actions pinned by major tag, dependency floors only in pyproject | `.github/workflows/ci.yml` |
| U33 | Published headline has no freeze enforcement: engine tier runs on edited gold and `caregap report` syncs the numbers | `src/caregap/evalrun.py` |
| U34 | `--gate` and `--update-baseline` accept an unpublishable pipeline artifact; baseline absorbs fallback-sentinel numbers | `src/caregap/evalrun.py` |
| U35 | Escalation / validator paths are unmeasured on real records: 0 gold `escalate`, 0 `needs_review`, 0 validator calls — the Engine+validator c | `scripts/gold.py` |
| U36 | `metrics.overall_dev`, `per_split`, `per_category` are empty dicts because dev P/R is never computed from counts | `src/caregap/evalrun.py` |
| U37 | Prompt-sha drift does not block publication: replayed responses from an older prompt publish under the new config_hash | `src/caregap/fakes.py` |
| U38 | Labeler inputs are not pinned: worksheets are git-ignored and FREEZE.json hashes neither the worksheets nor LABELING_GUIDE.md; the split rul | `scripts/freeze_gold.py` |
| U39 | Pipeline tier (freeze refusal, fallback stamping) never executes in CI; SPEC/README describe it as a CI replay tier | `.github/workflows/ci.yml` |
| U40 | Headline P/R published and ratchet baseline set while 3 of 6 measures fail the publication guard | `README.md` |
| U41 | SPEC's '>=24 h self-agreement re-label' was replaced by a same-session second LLM labeler; docs disagree on what AGREEMENT.md measures | `evals/gold/AGREEMENT.md` |
| U42 | Backlog item 11 confirmed: `DRAFTER_FALLBACK == '{}'` consumes exactly two fallbacks per open-gap patient (52 = 26 x 2) | `src/caregap/llm.py` |
| U43 | Eval tiers write their runs into the live API/UI ledger (data/caregap.sqlite) and can supersede real pending approvals | `src/caregap/evalrun.py` |
| U44 | Single-patient runs (CLI `run`, eval tiers) stay at run status 'created' forever; the UI treats 'created' as active and polls every 2 s inde | `src/caregap/graph/runner.py` |
| U45 | Process shutdown or crash during a panel run leaves the run 'running' forever; lifespan does not join run threads and startup never reconcil | `src/caregap/api/app.py` |
| U46 | Per-patient timeout leaves a resumable interrupt behind an 'error' row: the decision endpoint accepts it and writes the outbox while /v1/app | `src/caregap/graph/runner.py` |
| U47 | CI is a single ubuntu job with no Windows job, no coverage floor, no contract job and no hygiene tests (backlog #18 confirmed); a tracked fi | `.github/workflows/ci.yml` |
| U48 | `caregap panel` with an unreachable P6 dumps a raw traceback (exit 1) instead of a usage/configuration error; `serve` exits 3 with a traceba | `src/caregap/cli.py` |
| U49 | Embedded mode silently creates an empty DuckDB when CAREGAP_P6_DB_PATH does not exist; the FileNotFoundError branch in _open_runtime is dead | `src/caregap/p6/embedded.py` |
| U50 | pyproject floors (langgraph>=0.2, langgraph-checkpoint-sqlite>=2.0) admit versions the code cannot import; SPEC section 12 promises exact pi | `pyproject.toml` |
| U51 | Backlog #20 confirmed for this lens: the CLI uses SqliteSaver at data/checkpoints.sqlite, not the MemorySaver SPEC section 3 states | `src/caregap/runtime.py` |
| U52 | Backlog #15 dismissed as by-design: patient_runs.pending persists after completion, pending-ness is derived; only the UI stepper caption rea | `src/caregap/graph/runstore.py` |

### Refuted by the verifiers

- MY-bounded denominator/exclusion windows are as_of-correct only through mask_as_of; two engine callers skip the defensive re-mask and the P6 (`src/caregap/cli.py`)
- Sex string normalisation differs between BCS and SPC (`src/caregap/measures/rules/bcs.py`)
- Every escalation promotes a closed verdict to needs_review, including E1/E4/E6/E7 whose resolution cannot make it actionable (`src/caregap/measures/tri.py`)

## Resolved 2026-09-05 (rule-fidelity pass + escalation slice)

- C1 (gold exercises zero escalation units): resolved by the escalation slice — 12 carriers
  (4 dementia+acute care, 3 ambiguous colon, 5 hospice-in-MY) blind-labeled, frozen as slice
  `escalation` (FREEZE.json history), `review_flag_rate` now measured. E1/E3/E5/E6 remain
  unrepresentable in this panel at 2025-12-31 (FEASIBILITY_escalation.md); covered by unit tests.
- Abatement-edge semantics unified in `evidence.condition_active_in` (> window.start) and used by
  CBP; boundary tests in every caller (C10/C13, backlog 1).
- Pregnancy: one shared helper (pregnancy_snomed condition OR LOINC 82810-3 = 77386006) in CBP
  (MY), SPC and SPD (MY or prior year); validator category table updated (C4/C14/C15).
- EED: E6 code/text aligned (abatement triggers; HbA1c attached), prior-year negative answer
  linked to the exam date (C3/C6/C16/C24).
- Hospice episodes spanning into the MY count as hospice-in-MY (C8); engine: exclusions win over
  the unknown-birth-date downgrade (C25); SPD ESRD/dialysis retagged demo_choice in code (C19).
- `rules/statin.py` (public helpers shared by SPC/SPD) and `evidence.child_observations_of`
  (CBP + screening) (C23, backlog 2/3); E2 removed from the escalation vocabulary (C22, 19).
- Persona goldens and engine outcomes regenerated; ratchet gate passes.

## Still open after that pass (small, text-level; one cheap agent)

- Rule-JSON wording must match the code for: EED denominator abatement edge, CBP/SPC/SPD
  pregnancy demo_choice text (82810-3 arm), CBP E5 text (only most-recent-date panels; defective
  panel -> numerator unknown + E5), SPD ESRD/dialysis element retag in JSON, E4 text (two windows:
  condition active in MY; medication in [prior MY start, as_of]), spc.json PCI/CABG rationale.
  Edit at the source in `scripts/sync_rule_text.py`, regenerate JSON + docs/MEASURES.md.
- Coverage-key parity test (C21): one vocabulary joinable to rule JSON element ids.
- Verify docs/SPEC.md escalation table has no E2 reference; SPD row lists pregnancy.

## Re-verification 2026-09-05 (52 previously unverified findings; one verifier per cluster)

48 confirmed (44 reproduced), 4 refuted or already fixed. Fixers have NOT run on these.

| # | sev | finding | where | suggested fix |
|---|---|---|---|---|
| V1 | high — RESOLVED (fixed 2026-09-05) | Streamlit approval console binds all interfaces with no authentication (caregap ui) | `src/caregap/cli.py:335` | In ui(): argv += ['--server.address', '127.0.0.1', '--server.headless', 'true'] (or ship src/caregap/ui/.streamlit/config.toml with [server] address='127.0.0.1'), extend test_ui_launches_streamlit_as_a_subprocess to asse |
| V2 | high — RESOLVED (fixed 2026-09-05) | Published headline has no freeze enforcement: engine tier runs on edited gold and `caregap report` syncs the numbers | `src/caregap/evalrun.py:355` | Make `_run_engine_tier` call `_require_frozen(paths, 'engine')` when FREEZE.json exists (keep 'not_frozen' allowed only while no freeze file exists so the bootstrap path survives), and make `sync_readme_cmd` (and `--chec |
| V3 | high — RESOLVED (fixed 2026-09-05) | Eval tiers write their runs into the live API/UI ledger (data/caregap.sqlite) and can supersede real pending approvals | `src/caregap/evalrun.py:199` | In `_eval_settings` add `'runstore_path': RunStore.MEMORY` (or `paths.root / 'ledger.sqlite'` under out_dir) and `'checkpoint_path': paths.root / 'checkpoints.sqlite'`; add a test asserting `run_eval` never opens `settin |
| V4 | high — RESOLVED (fixed 2026-09-05) | finalize re-keys validator verdicts by the model-supplied measure_id and rewrites other measures' final status | `src/caregap/graph/nodes.py:519` | Make the packet's measure id authoritative: in verify_verdict return model_copy(update={'measure_id': packet.measure_id}) after recording the mismatch in verification_note (the mismatch already forces needs_human), and i |
| V5 | high — RESOLVED (fixed 2026-09-05) | VALIDATOR_FALLBACK hard-codes measure_id "CBP": every replay fallback on a non-CBP case corrupts CBP's final status | `src/caregap/llm.py:18` | (1) In verify_verdict, stamp the packet's measure_id on the returned verdict (model_copy(update={'measure_id': packet.measure_id}) after recording the mismatch in verification_note) so no model/sentinel label can address |
| V6 | medium | Backlog #17 confirmed: the gold panel has zero escalations/needs_review, so the pipeline tier never calls the validator and the Engine+validator colum | `evals/gold/engine-outcomes.jsonl:1` | Finish and freeze the supplementary escalation slice under the same blind protocol (freeze_gold.py --append-slice) and publish the change count; in `_run_pipeline_tier` stamp `validator_calls` (sum of validator decisions |
| V7 | medium | Escalation / validator paths are unmeasured on real records: 0 gold `escalate`, 0 `needs_review`, 0 validator calls — the Engine+validator column is t | `scripts/gold.py:524` | Keep the new window-aware trigger facts (hospice event in [my_start-90d, my_start), dementia + IMP/EMER in MY at 66+, E7 ambiguous colon code) as the carrier definition, complete the blind labeling of the supplementary s |
| V8 | medium | Model-authored validator strings (unbounded) reach the drafter prompt outside the data fence and the provider note | `src/caregap/agents/drafter.py:232` | Report exclusion_category/rule_citation/evidence-id problems with fixed wording (e.g. 'exclusion_category not in packet') instead of interpolating the model strings; keep raw model fields only on the stored verdict; rend |
| V9 | medium | lint_plan's forbidden-phrase list does not block undisclosed diagnoses, most dose instructions, or spelled-out readings | `src/caregap/agents/drafter.py:46` | Add a disclosure check driven by chronic_flags (block diabet*/hypertens*/heart/ascvd words unless disclosed; always block hospice, dialysis, kidney, pregnan*, transplant, stroke), replace the dose substrings with regexes |
| V10 | medium | verify_verdict's exclude check is section-agnostic, so the validator can exclude on evidence the rule deliberately ignores (CBP dialysis condition) | `src/caregap/agents/validator.py:122` | Add a `sections: frozenset[Section]` field to CategorySpec mirroring each rule (dialysis: procedures; esrd/kidney_transplant per cbp.py; hospice: procedures/conditions/encounters; pregnancy_status_positive: observations) |
| V11 | medium | Process shutdown or crash during a panel run leaves the run 'running' forever; lifespan does not join run threads and startup never reconciles | `src/caregap/api/app.py:49` | In the lifespan finally block (before active.close()): set every cancel flag, then join each thread in app.state.run_threads with a bound of settings.patient_timeout_s so the loop can write 'cancelled'/'error' on a live  |
| V12 | medium | caregap serve runs uvicorn with default stdlib logging: access lines and full tracebacks bypass the structlog allowlist; SPEC's log-safety tests do no | `src/caregap/cli.py:323` | In serve(): pass access_log=False and log_config=a sanitized dict (mirror fhir_features.logging_setup.uvicorn_log_config: no access handlers, a message-only formatter on uvicorn.error that drops exc_info). Add tests/unit |
| V13 | medium | `--gate` and `--update-baseline` accept an unpublishable pipeline artifact; baseline absorbs fallback-sentinel numbers | `src/caregap/evalrun.py:640` | In `run_eval`, when `artifact.get('publishable') is False`: refuse `--update-baseline` with EXIT_FAIL ('unpublishable artifact cannot set the ratchet') and restrict `--gate` to the engine.* keys (or exit 2 'nothing publi |
| V14 | medium — RESOLVED (fixed 2026-09-05) | Global 'hold all' review item is bypassed by revise + resolution-to-open; outreach reaches the outbox with the global item unresolved | `src/caregap/graph/nodes.py:544` | (1) hitl.validate_decision: when request.review_items contains a global item, reject any resolution to 'open' (and any approve/edit) unless the same decision resolves every global item with a non-open status. (2) record_ |
| V15 | medium — RESOLVED (fixed 2026-09-05) | finalize applies only the last decision's review_resolutions; resolutions sent with a revise are lost and reopened measures stay 'needs_review' next t | `src/caregap/graph/nodes.py:523` | In finalize, iterate state['decisions'] in order (last resolution per measure wins) instead of state['decision'] only, and additionally set final_statuses[m]='gap_open' for every OpenGap with source=='reviewer' (or, more |
| V16 | medium — RESOLVED (fixed 2026-09-05) | Supersession has no run ordering: finalizing an OLDER run marks the NEWER run's pending request superseded | `src/caregap/graph/runstore.py:478` | Restrict the UPDATE to older runs: join runs and add 'AND runs.created_at < (SELECT created_at FROM runs WHERE run_id = ?)' (pass the finalizing run id), or better, supersede older pending requests when a newer run START |
| V17 | medium | patient_id is interpolated unquoted into P6 URLs and snapshot paths; POST /v1/runs accepts arbitrary strings | `src/caregap/p6/http.py:74` | In src/caregap/p6/http.py wrap the id at both call sites: `from urllib.parse import quote` and use f"/v1/patients/{quote(patient_id, safe='')}/record" (line 74) and f"/v1/patients/{quote(patient_id, safe='')}/features" ( |
| V18 | medium | No reconciliation of 'running' run/patient rows after a process restart; runs stay 'running' forever and cancel reports 'cancelling' indefinitely | `src/caregap/runtime.py:100` | Add RunStore.reconcile_stale() and call it from build_runtime (after the store opens): UPDATE runs SET status='interrupted' WHERE status IN ('queued','running'); for their patient_runs rows still 'running', set 'error' w |
| V19 | medium | LangSmith tracing is never disabled: env vars alone ship full patient-record state to a cloud endpoint, including under fake/replay models and in CI | `src/caregap/runtime.py:100` | In build_runtime (before build_graph) force tracing off unless explicitly opted in: if os.environ.get('CAREGAP_ALLOW_TRACING') != '1': os.environ['LANGSMITH_TRACING']='false'; os.environ['LANGCHAIN_TRACING_V2']='false'.  |
| V20 | medium | UI sidebar 'API base URL' is unrestricted: server-side request forgery with 500-char error-body echo | `src/caregap/ui/app.py:68` | Remove the free-text input (derive the base URL from CAREGAP_API_HOST/PORT only) or validate it: scheme in {http, https}, host in {127.0.0.1, localhost, ::1, settings.api_host}, no userinfo. In problem_from_response, nev |
| V21 | low | Supply-chain posture: CI installs without --locked, actions pinned by major tag, dependency floors only in pyproject | `.github/workflows/ci.yml:26` | Change ci.yml:26 to `uv sync --locked --group dev` so drift fails the job instead of being re-resolved; pin `actions/checkout` and `astral-sh/setup-uv` to full commit SHAs with a version comment; tighten the LangGraph fl |
| V22 | low | Pipeline tier (freeze refusal, fallback stamping) never executes in CI; SPEC/README describe it as a CI replay tier | `.github/workflows/ci.yml:35` | Add a CI step `uv run caregap eval --tier pipeline --gate` (it exits 0 today and starts enforcing the freeze immediately; it will additionally gate the headline once recordings are committed), and call `_require_frozen(p |
| V23 | low | No hygiene test exists; .env.example contains `sk-ant-`; .gitignore negations re-allow data files under fixture dirs; CI guard is a single regex | `.gitignore:61` | Narrow the negations to the fixture types actually committed: `!synthetic/samples/*.json.gz`, `!synthetic/p6_snapshots/**/*.json`, `!synthetic/p6_snapshots/**/*.json.gz`, `!evals/gold/snapshots/**/*.json`, `!evals/gold/s |
| V24 | low | SPEC's '>=24 h self-agreement re-label' was replaced by a same-session second LLM labeler; docs disagree on what AGREEMENT.md measures | `evals/gold/AGREEMENT.md:1` | Retitle AGREEMENT.md to 'Same-family blind LLM labelers A vs B (same session)' with the caveat sentence from README:211; in SPEC section 6 and evals/README, mark the '>=24 h self-agreement re-label' as 'pending' (or perf |
| V25 | low | pyproject floors (langgraph>=0.2, langgraph-checkpoint-sqlite>=2.0) admit versions the code cannot import; SPEC section 12 promises exact pins for Lan | `pyproject.toml:10` | Raise the floors to the tested majors with upper bounds: `langgraph>=1.2,<2`, `langgraph-checkpoint-sqlite>=3.1,<4`, `langchain-core>=1,<2` (and `langchain-anthropic>=1,<2`), re-run `uv lock`, and pair it with `uv sync - |
| V26 | low | Labeler inputs are not pinned: worksheets are git-ignored and FREEZE.json hashes neither the worksheets nor LABELING_GUIDE.md; the split rule is dupli | `scripts/freeze_gold.py:26` | Have freeze_gold.py record `labeling_guide_sha256`, `worksheet_script_sha256` (scripts/worksheet.py) and a sha256 over the sorted worksheet bytes in FREEZE.json (and in each --append-slice entry); make `load_gold`/`_free |
| V27 | low | Committed fixtures keep quasi-identifiers the code deliberately strips (city/state/postal_code/race/ethnicity; raw Synthea names/addresses/SSN identif | `scripts/gold.py:211` | In `cmd_snapshot` project `record['patient']` onto the PatientHeader field set (+ 'source') before writing, regenerate the 70 snapshots (MANIFEST hash will change; the engine never reads the dropped keys so engine-outcom |
| V28 | low | Lint and the edited-plan re-lint never inspect action detail, provider note, or rationale content; a patient-owned action with dose instructions and c | `src/caregap/agents/drafter.py:355` | Run the NO_CODES/NUMBERS/FORBIDDEN/CANCER checks over every GapAction.detail whose owner is 'patient' (or all details), and at least NO_CODES/event-id checks over provider_note and rationales; add an outbox test assertin |
| V29 | low | patient.sex is a free P6 string rendered outside both packets' data fences | `src/caregap/agents/packet.py:648` | Normalise sex at the P6 boundary: make PatientHeader.sex (and DrafterContext.sex / EvidencePacket.sex) a Literal['male','female','other','unknown'] with a validator mapping anything else to 'unknown', and add a packet-re |
| V30 | low | Backlog #12 dismissed; #13 and #14 confirmed with code locations (no behavioural defect, SPEC wording needs updating) | `src/caregap/agents/validator.py:99` | Delete #12 from docs/REVIEW_BACKLOG.md; amend the SPEC section 3 diagram caption to 'candidate with escalation or unknown denominator/numerator' and the section 4 verify_verdict bullets to state the engine-tri preconditi |
| V31 | low | Unbounded request inputs: as_of before year 10 yields 500s and error rows with no load_error; decision/note/feedback/max_revisions have no caps | `src/caregap/api/routes_patients.py:23` | Validate as_of at the boundary: in RunRequest and the gaps route use a pydantic validator / Query bounds rejecting as_of outside [1900-01-01, 2100-12-31] with a 422 'request-invalid'. In state.py: decision_id and reviewe |
| V32 | low | `caregap panel` with an unreachable P6 dumps a raw traceback (exit 1) instead of a usage/configuration error; `serve` exits 3 with a traceback on a co | `src/caregap/cli.py:253` | panel(): wrap runtime.p6.list_patients in `except P6Error as exc: raise _fail(f'P6 ({settings.p6_mode}, {settings.p6_url}) unavailable: {type(exc).__name__}: {exc}')` (exit 2). serve(): build and immediately close a Runt |
| V33 | low | Prompt-sha drift does not block publication: replayed responses from an older prompt publish under the new config_hash | `src/caregap/evalrun.py:440` | Either run the pipeline tier with replay_bundle(mode='strict') once recordings exist, or treat sha_drift_count > 0 as unpublishable (unpublishable_reason naming the count); at minimum print sha_drift_count in the README  |
| V34 | low | Prompt-sha drift and packet-content drift never block pipeline publication; recordings carry only the system-prompt sha | `src/caregap/evalrun.py:440` | In `_run_pipeline_tier` set `publishable = fallback_count == 0 and sha_drift == 0` (with an `unpublishable_reason` naming the drift count), or build the tier's bundle with `replay_bundle(paths.recorded, mode='strict')`;  |
| V35 | low | `metrics.overall_dev`, `per_split`, `per_category` are empty dicts because dev P/R is never computed from counts | `src/caregap/evalrun.py:340` | In `_compose`, when `scorecard.report.counts_per_split` has 'dev', set `metrics['overall_dev'] = {**report.per_split.get('dev', {}), **micro_prf([report.counts_per_split['dev']])}`; either populate `metrics.per_split`/`p |
| V36 | low | Headline P/R published and ratchet baseline set while 3 of 6 measures fail the publication guard | `src/caregap/evalrun.py:392` | In `run_eval`, refuse `--update-baseline` (exit 1 with the publication note) when `artifact['publication']['insufficient_measures']` is non-empty unless `--allow-provisional` is passed, and record `provisional: [CBP, SPC |
| V37 | low | parse_headers reads assistant turns, so the correction-turn recording/replay can be keyed by whatever the model echoed; both attempts collapse to one  | `src/caregap/fakes.py:32` | Parse headers only from the first HumanMessage (skip AIMessage instances) and either suffix the recorded case key with the attempt number (':a2') or record a list of responses per key and replay them in order so CallTrac |
| V38 | low | Backlog #14 confirmed: every needs_review candidate (not only escalated ones) is routed to validate_gaps | `src/caregap/graph/edges.py:122` | Update docs/SPEC.md section 3 caption to 'candidate escalated or needs_review' (and the README mermaid if it carries the same caption); keep the code and the structure test. Delete backlog item 14. |
| V39 | low | ReviewItem.reason echoes unbounded model-authored strings (evidence_ids, exclusion_category, rule_citation) into the approval request and ledger | `src/caregap/graph/nodes.py:333` | Constrain the schema: evidence_ids items max_length ~64 with a pattern like ^[A-Za-z0-9._:-]+$, exclusion_category max_length 64, rule_citation max_length 128; have verify_verdict emit counts/categories in verification_n |
| V40 | low | Backlog #16 confirmed: final_statuses uses engine vocabulary ('gap_open'), never 'open' | `src/caregap/graph/nodes.py:95` | Edit docs/SPEC.md section 6 to say 'final status in {gap_open, needs_review}' (engine vocabulary) and delete backlog item 16. |
| V41 | low | A node failure after the interrupt is resumed (e.g. outbox sink error in finalize) strands the thread: decision stored as 'error', retry is 409, no re | `src/caregap/graph/runner.py:207` | When _drive fails AFTER the resume was applied (box.error from a node past await_approval), do not record the decision as 'error'; instead persist a 'failed_after_decision' patient status and expose a retry path that re- |
| V42 | low | start() on a thread that already has a checkpoint re-drives from START and duplicates reducer lists (trace, decisions, verdicts, agent_errors) | `src/caregap/graph/runner.py:143` | In PatientRunner.start, under the thread lock, refuse when self._store.get_patient_run(run_id, patient_id) is not None (return the stored pending request / outcome, or raise a new AlreadyStarted error the CLI maps to a u |
| V43 | low | Single-patient runs (CLI `run`, eval tiers) stay at run status 'created' forever; the UI treats 'created' as active and polls every 2 s indefinitely | `src/caregap/graph/runner.py:143` | In PatientRunner.start, when the run row was created by this call (single-patient run), set the run status to 'running' before _drive and to 'completed' with cursor=1 afterwards (an interrupt still counts as the run havi |
| V44 | low | Backlog #15 confirmed: PatientRunRecord.pending persists after completion and is served as-is by the API | `src/caregap/graph/runstore.py:325` | Rename the ledger/wire field to last_request (or document it in the API schema and UI), and extend ApprovalStatus with 'failed' derived when the stored outcome has status 'error' so /v1/approvals never reports an unsent  |
| V45 | low | Backlog #11 confirmed: DRAFTER_FALLBACK '{}' is a valid empty plan, so replay fallback costs two lint failures per draft node, not a parse failure | `src/caregap/llm.py:23` | Replace the sentinel with non-JSON text (e.g. DRAFTER_FALLBACK = 'RECORDING_MISSING') so StructuredCaller raises AgentOutputError after its correction turn and draft_error becomes 'drafter_output_error:JudgeParseError' w |
| V46 | low | Backlog item 11 confirmed: `DRAFTER_FALLBACK == '{}'` consumes exactly two fallbacks per open-gap patient (52 = 26 x 2) | `src/caregap/llm.py:23` | Use a non-JSON sentinel (e.g. 'RECORDING_MISSING') so one fallback == one missing case, or keep '{}' and document in the artifact/README that fallback_count counts model calls (two per drafted patient); fix the llm.py co |
| V47 | low | Embedded mode silently creates an empty DuckDB when CAREGAP_P6_DB_PATH does not exist; the FileNotFoundError branch in _open_runtime is dead code | `src/caregap/p6/embedded.py:21` | In src/caregap/p6/embedded.py build_embedded_client, before mkdir/create_app: `if not db_path.is_file(): raise FileNotFoundError(f"{db_path} not found; run `caregap bootstrap` first (or set CAREGAP_P6_DB_PATH)")`, and dr |
| V48 | low | Backlog #20 confirmed for this lens: the CLI uses SqliteSaver at data/checkpoints.sqlite, not the MemorySaver SPEC section 3 states | `src/caregap/runtime.py:113` | Edit docs/SPEC.md line 119 to: 'Checkpointers: MemorySaver (tests/CI/evals); SqliteSaver at data/checkpoints.sqlite (API/UI/CLI, one shared file so `caregap run` hands a pending approval off to `caregap serve`; single wr |

### Refuted or already fixed

- Backlog #12 dismissed: only one allowed_numbers_for remains (`src/caregap/graph/hitl.py`): The dismissal is accurate: `def allowed_numbers_for` exists once in src (agents/drafter.py:259); hitl.py:10 imports it and uses the same number rule for the edi
- Per-patient timeout leaves a resumable interrupt behind an 'error' row: the decision endpoint accepts it and writes the outbox while /v1/app (`src/caregap/graph/runner.py`): fixed in 3af09fc. resume() now refuses a thread whose ledger row says 'error' (runner.py:179-189 -> NotAwaitingApproval / API 409) and finalize refuses the outb
- Backlog #15 dismissed as by-design: patient_runs.pending persists after completion, pending-ness is derived; only the UI stepper caption rea (`src/caregap/graph/runstore.py`): The dismissal holds: the COALESCE is documented intent (runstore.py:310-313), the API computes the live pending from graph.get_state (routes_runs.py:110-112), a
- CI is a single ubuntu job with no Windows job, no coverage floor, no contract job and no hygiene tests (backlog #18 confirmed); a tracked fi (`.github/workflows/ci.yml`): fixed in 3af09fc. ci.yml:10-14 now runs the `quality` job on an `[ubuntu-latest, windows-latest]` matrix; pyproject.toml:73 addopts carries `--cov-fail-under=85
