# Evaluation protocol

This directory holds the eval **data**: gold labels, snapshots, recordings, artifacts and the
ratchet baseline. The eval **code** lives in `src/caregap/evals/` (gold loader, scoring, gates,
rubrics, outcome rows) and `src/caregap/evalrun.py` (the tiers, artifacts and README sync);
the shared harness is the sibling `clinical-agent-evals` (P5, pinned `v0.1.0`). The design
decision behind the protocol is `docs/adr/0005-public-source-rules-and-blind-gold-protocol.md`.

> The headline precision/recall measures the deterministic engine (plus code-verified validator decisions) against demo-grade rules applied by a blind labeler (an LLM working from worksheets, independent of the engine; human spot-check pending); it is independent of the drafting agent. Agents are measured separately.

## 1. Unit of evaluation

- One unit is `(patient_id, measure_id)` at one `as_of`. The eval anchor is `2025-12-31`, a
  complete MY2025, so every window is retrospective and `time_pressure` is 1.
- Core measures (scored): CBP, EED, BCS, COL, SPC, SPD. Screening measures (TSC, SNS) are
  reported as status counts only, never as a ratio.
- Gold statuses: `not_eligible | closed | excluded | open | escalate`.
- Prediction: the pipeline's final status per measure (`RunOutcome.final_statuses`, engine
  vocabulary). `predicted = {"gap"}` iff the final status is `gap_open` or `needs_review`;
  the **strict variant** counts only `gap_open`.
- Gold `open` maps to `{"gap"}`; every other gold status maps to the empty set, so `closed`,
  `excluded` and `not_eligible` units are load-bearing false-positive traps.
- Gold `escalate` carries no confusion counts. It is scored by `review_flag_rate`: did the
  pipeline hold that measure for a human (a measure-scoped review item, a final `needs_review`,
  or a global hold covering every candidate)?
- A patient whose graph run ends in `error` predicts nothing: every gold-open unit becomes a
  false negative and every escalate unit an unflagged one. Error patients are listed in the
  artifact.
- Two lower-is-better counts on the test split: `validator_unsafe_close` (gold-open units the
  validator excluded or closed) and `unresolved_evidence` (units whose verdict came back
  `verified=False`).
- Headline precision, recall and F1 are micro-averaged over the summed test-split counts of the
  six core measures (`EvalReport.test_overall`). Dev-split metrics are stored beside them and are
  never the headline.

Implementation: `src/caregap/evals/scoring.py`, `src/caregap/evals/outcomes.py`.

## 2. Gold protocol

The labels are produced blind, by a language model, from the written rules, and frozen before
the engine ever sees them. In order:

1. **Rules and guide first.** The tagged rule JSON (`src/caregap/measures/rules/json/`) and the
   labeling guide `evals/gold/LABELING_GUIDE.md` are written before any patient is chosen. The
   guide restates every demo-grade rule in labeler terms (windows, age at December 31, what a
   BP panel is, the statin on-therapy definition of ADR-0002, the E1-E7 conditions that make a
   unit `escalate`).
2. **Feasibility scan.** The stratified Synthea panel is scanned for code presence
   (`scripts/scan_codes.py` writes `src/caregap/measures/value_sets/SCAN.md`) so the selection
   targets are realistic for the data.
3. **Selection on descriptive facts.** Patients are selected by age band, sex, diagnosis
   presence and encounter counts, never by an engine verdict. Targets: at least eight eligible
   members per core measure, at least twelve no-gap probes, at least six escalation carriers.
4. **Snapshots.** `python scripts/gold.py snapshot --as-of 2025-12-31 --out evals/gold/snapshots`
   boots the real P6 in-process and writes each selected patient's raw record with every section
   and every observation code (`caregap.p6.client.ENGINE_SECTIONS` applies no
   `observation_codes` filter), plus a `MANIFEST.json` stamped with P6's versions and the git
   sha. The labelers and the engine read the same bytes.
5. **Value-set-agnostic worksheets.** One worksheet per patient lists events with code, display,
   date and status, built without importing any value set or rule module, so a labeler cannot
   inherit the engine's code-membership decisions. Events a labeler judges to match a concept
   but that fall outside the guide's listed codes are reported per unit and collected in
   `evals/gold/CODE_AUDIT.md`.
6. **Blind LLM labelers.** Labeler A is a blind LLM labeler (Claude, run as Claude Code agents)
   that receives only the guide and one worksheet at a time. It has no access to the engine,
   its verdicts, its value sets or this repository's source. Per measure it returns a status, a
   rationale (truncated to 600 characters on freeze), the decisive dates, an `uncertain` flag,
   and any events outside the listed codes. Labeler B, an independent blind LLM labeler with the
   same inputs, labels a 12-patient subset. No gold label was written by a human.
7. **Split.** `dev` iff `int(sha256(patient_id)[:8], 16) % 3 == 0`, else `test` (about one third
   dev, two thirds test). `caregap.evals.gold.load_gold` refuses a row whose stored split
   disagrees with the hash, refuses duplicate units, and refuses a patient with more than one
   `as_of`.
8. **Freeze before engine contact.** `python scripts/freeze_gold.py --labels <workflow json>`
   writes `gap_cases.jsonl` (A), `second_labeler.jsonl` (B), `AGREEMENT.md`, `CODE_AUDIT.md`
   and `FREEZE.json` (sha256 of `gap_cases.jsonl`, per-split and per-measure counts, uncertain
   row count, labeler id, git sha, `engine_contact_before_freeze: false`). It refuses to
   overwrite an existing freeze. The pipeline and outreach tiers refuse to run when
   `gap_cases.jsonl` no longer hashes to the frozen value.
9. **Adjudication on dev only.** After engine contact, dev-split disagreements may be
   adjudicated and recorded in `evals/gold/ADJUDICATION.md`, with the number of changed labels
   published there. Test-split labels are never edited after the freeze.
10. **Agreement sample.** `AGREEMENT.md` publishes A-versus-B agreement on the subset: exact
    status agreement, collapsed (gap / no_gap / escalate) agreement, per-measure agreement, and
    every disagreement.
11. **Human spot-check: pending.** A human review of the agreement subset has not been done. The
    artifact, the README and this file say `pending` rather than implying human verification.

## 3. Files

```
evals/
  README.md                     this file
  gold/
    LABELING_GUIDE.md           the written rules the labelers apply
    gap_cases.jsonl             labeler A: one row per (patient, measure); split derived from the hash
    second_labeler.jsonl        labeler B on the 12-patient subset
    FREEZE.json                 sha256 of gap_cases.jsonl, counts, git sha, engine_contact_before_freeze
    AGREEMENT.md                A vs B agreement (exact, collapsed, per measure, disagreements)
    CODE_AUDIT.md               events labelers matched to a concept outside the listed codes
    ADJUDICATION.md             dev-split adjudications after engine contact (count published)
    engine-outcomes.jsonl       committed engine-tier outcome rows (byte-compared in CI)
    judge_human_spotcheck.jsonl human labels for the outreach judge sample (absent = pending)
    snapshots/                  P6 records + features per gold patient, MANIFEST.json
  recorded/
    validator.jsonl             recorded validator responses keyed by case_key + prompt sha
    drafter.jsonl               recorded drafter responses
    judge.jsonl                 recorded judge responses (one per drafted plan)
  artifacts/
    engine-latest.json          stamped artifact per tier (git sha, date, config hash,
    pipeline-latest.json        dataset hash, model ids, metrics, per-item scores)
    outreach-latest.json
  baseline.json                 measured ratchet baseline (never aspirational)
```

Gold row shape: `patient_id, measure_id, as_of, gold, rationale, decisive_dates, uncertain,
labeler, split`. `item_id` and `category` are derived by the loader and rejected if they
disagree.

## 4. Tiers and what CI does keylessly

All tiers run the real compiled graph through `PatientRunner` over `evals/gold/snapshots` with
`approval_mode="auto"`: the approval request is emitted and auto-reviewed, nothing is ever
actionable, and the outbox is never written.

| Tier | Command | Models | What it measures | In CI |
|---|---|---|---|---|
| engine | `caregap eval --tier engine [--regen] [--gate]` | none (validation off, fake drafter) | the engine-only column: `metrics.overall` of this artifact **is** the engine-only headline; outcome rows must be byte-identical to `evals/gold/engine-outcomes.jsonl` | yes, live: `--gate` runs on every push (exit 2 until gold and baseline exist) |
| pipeline | `caregap eval --tier pipeline [--record] [--gate]` | replayed recordings (`--record` uses the real models and a key; never in CI) | Engine+validator: the headline P/R/F1, the engine-only comparison column, `validator_unsafe_close`, `unresolved_evidence` | keyless replay; refuses an unfrozen or changed gold set; stamps `fallback_count` |
| outreach | `caregap eval --tier outreach [--judge]` | judge = `claude-opus-5` locally with `--judge`; CI re-parses `evals/recorded/judge.jsonl` | faithfulness of provider note + patient message to the evidence lines, under the frozen rubric `caregap-faithfulness-v1`; judge-human agreement over the 12-item stratified sample | keyless re-parse; no gates |

Exit codes: 0 ok; 1 a measured failure (byte diff, freeze mismatch, ratchet regression, README
out of sync); 2 something required is missing (gold labels, snapshots, recordings, baseline,
key). There is never a silent pass.

Replay determinism: recorded responses are keyed by `case_key`
(`validator:<patient>:<measure>:<as_of>:0`, `drafter:<patient>:plan:<as_of>:<revision>`,
`judge:<patient>:plan:<as_of>:0`) and carry the prompt sha. A missing recording returns a
deterministic fail-closed sentinel (validator: `needs_human`; drafter: an empty plan that lint
rejects, so the template drafter takes over) and increments `fallback_count`. A prompt-sha
mismatch increments `sha_drift_count`. Both are stamped into the artifact.

## 5. Artifacts, baseline and ratchet

- Every tier writes `evals/artifacts/<tier>-latest.json` via `clinevals.build_artifact`: git
  sha, date, a config hash (approval and validation modes, measures, prompt version and shas,
  rule versions), the dataset hash (sha256 of `gap_cases.jsonl`), model ids, `metrics.overall`
  (test split), `metrics.overall_dev`, `metrics.strict_overall`, per-measure rows, per-item
  scores, the freeze block, the snapshot stamp, screening counts, and the publication block.
  The pipeline artifact adds `metrics.engine_only_overall`.
- `evals/baseline.json` is one flat `{"metrics": {...}}` block written only by
  `caregap eval --tier <tier> --update-baseline` after a reviewed run. It is a **measured**
  baseline, never a target.
- Six gated metrics (`src/caregap/evals/gates.py`): `micro_precision`, `micro_recall`,
  `engine.micro_precision`, `engine.micro_recall`, `validator_unsafe_close` (lower is better),
  `unresolved_evidence` (lower is better). Tolerance 0.02. The engine tier is held to the
  `engine.*` pair; the pipeline tier to all six; the outreach tier is not gated.
- `--gate` compares the fresh artifact to the baseline and fails on a regression beyond the
  tolerance. Gated metrics the baseline has not measured yet are skipped with a message; an
  absent baseline exits 2 rather than passing.

## 6. Publication guard and README regions

- A per-measure row is published only when the **test** split holds at least five gold-open
  units (three for BCS, where mammography is rare in Synthea). Otherwise the row reads
  `insufficient` and the headline carries the caveat "the headline is provisional".
- The pipeline column is published only when `fallback_count == 0`; otherwise the artifact is
  `unpublishable` and the README shows the engine-only numbers with the reason.
- The outreach line shows the judge-human agreement, or `pending`; agreement below 0.80 marks
  the line **UNTRUSTED**.
- `caregap report` rewrites the two README regions (`EVAL` and `EVAL-MEASURES`) from the latest
  artifacts, preferring a publishable pipeline artifact over the engine artifact, and writes the
  `not yet measured` placeholders when no artifact exists. `caregap report --check` fails CI when
  the regions are stale. No number in the README is typed by hand.

## 7. How to read the numbers

- **The headline scores fidelity to the written demo rules, not clinical truth.** Gold and
  engine both apply the same demo-grade rules (`docs/MEASURES.md`): the same windows, the same
  evidence definitions, the same tagged demo choices, and the same reading of
  `MedicationRequest.status` (ADR-0002). A perfect score means the engine implements the written
  rules the way a blind reader of those rules does. It does not mean the rules match NCQA
  specifications, and it does not mean a member truly has or lacks a gap.
- **"Hand-applied" means applied from the written rules by a labeler that never saw the engine.**
  That labeler is a language model, not a clinician. Its agreement with a second independent
  model is published; its agreement with a human is pending.
- **The two columns answer one question each.** Engine-only says how well the deterministic
  code applies the rules. Engine+validator says what the validator adds on escalated candidates,
  and `validator_unsafe_close` says what it costs. The validator can never add a gap, so recall
  cannot rise above the engine's; it can only fall, and that fall is gated.
- **`needs_review` counts as a predicted gap** in the publication numbers because a held measure
  reaches a human. The strict variant beside it shows what the numbers look like when only
  confirmed open gaps count.
- **Escalate units are scored on holding, not on the eventual answer.** A gold `escalate`
  passes when the pipeline puts the measure in front of a human, whichever way a human would
  decide.
- **Read raw counts beside every ratio.** Synthea prevalence (rare mammography, frequent prior
  hospice codes, statins authored once) shapes both the label distribution and the error profile;
  a measure with few gold-open units is published as `insufficient`, not as a ratio.
- **Known leak.** `MedicationRequest.status` is not date-versioned, so retrospective SPC and SPD
  numbers include post-`as_of` stops, in the direction of extra open gaps for both gold and
  engine.
- **The outreach score is a different instrument.** It grades faithfulness of drafted language to
  the evidence lines under a frozen rubric, with a model as judge; it says nothing about gap
  detection and is not gated.

## 8. Commands

```bash
# gold protocol (local; the labeling itself runs outside this CLI)
python scripts/gold.py snapshot --as-of 2025-12-31 --out evals/gold/snapshots
python scripts/freeze_gold.py --labels <workflow-result.json> --as-of 2025-12-31

# tiers
uv run caregap eval --tier engine --regen        # first time: write engine-outcomes.jsonl, then commit it
uv run caregap eval --tier engine --gate         # what CI runs
uv run caregap eval --tier pipeline --record     # local, needs CAREGAP_ANTHROPIC_API_KEY; commit evals/recorded/
uv run caregap eval --tier pipeline --gate       # keyless replay
uv run caregap eval --tier outreach --judge      # local, needs a key; commit evals/recorded/judge.jsonl
uv run caregap eval --tier outreach              # keyless re-parse

# baseline and README
uv run caregap eval --tier engine --update-baseline
uv run caregap eval --tier pipeline --update-baseline
uv run caregap report            # rewrite the README regions from the artifacts
uv run caregap report --check    # CI: fail when stale
```
