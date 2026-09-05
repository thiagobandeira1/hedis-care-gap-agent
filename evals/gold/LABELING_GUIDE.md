# Blind labeling guide (gold protocol, SPEC section 6)

**What this is.** The written, hand-applicable statement of this project's *demo-grade,
HEDIS-aligned* care-gap rules, one section per measure, derived element by element from the
rule JSON in `src/caregap/measures/rules/json/<id>.json` and the public code lists in
`src/caregap/measures/value_sets/*.json`. Element ids such as `cbp/numerator/bp_panel` are
quoted so every instruction can be traced back to its source line.

**What this is not.** These are **not NCQA HEDIS specifications** and not CMS Star Ratings
specifications. Every rule element in the JSON is tagged `quoted` (a verbatim public sentence),
`demo_choice` (a parameter this demo fixed, with a stated rationale) or `not_representable` (a
public criterion the synthetic data cannot evidence). Where the public wording and the demo
choice differ, **the demo choice is the rule you label against** — the point of the gold set is to
measure the engine against the rules it claims to implement, applied by a blind labeler (an LLM working only from this
guide and one worksheet) who never saw the engine.

**Blindness.** While labeling you must not run, read, or reason from the measure engine, its
goldens, its evidence packets, the UI, `/v1/measures`, or `docs/MEASURES.md` verdict tables. Your
only inputs are the worksheet for the patient (`evals/gold/worksheets/<patient_id>_<as_of>.md`)
and this guide. Labels are written to `evals/gold/gap_cases.jsonl` and frozen (hash pinned) before
any engine contact. Do not edit this guide after labeling starts; if a rule turns out to be
ambiguous, record the ambiguity in the case's `rationale`, set `uncertain: true`, and (if it
would change the label) prefer `escalate`.

Data is synthetic (Synthea) only. Nothing here is medical advice.

---

## 0. Conventions used by every measure

### 0.1 Anchors and windows

| symbol | value for this gold set | source |
|---|---|---|
| `as_of` | **2025-12-31** (retrospective, complete MY2025) | `*/timeline/measurement_year` |
| MY (measurement year) | calendar year of `as_of`: **2025-01-01 .. 2025-12-31** | `*/timeline/measurement_year` |
| `my_start` / `my_end` | 2025-01-01 / 2025-12-31 | same |
| MY-1 (prior year) | 2024-01-01 .. 2024-12-31 | same |
| age | **age at Dec 31 of the MY** (the worksheet header prints it) | same |

Rules of thumb (all from `*/timeline/measurement_year`):

- Every **numerator** window ends at `as_of` (here `as_of == my_end`, so "in the MY" and "in
  [my_start, as_of]" coincide).
- **Denominator and exclusion** windows use the MY bounds.
- Events dated after `as_of` never exist for this run. The worksheet already hides them, and
  hides a death, an abatement, or an encounter end that happened after `as_of`.
- Dates are compared as calendar dates (inclusive on both ends): "in the MY" means
  `2025-01-01 <= date <= 2025-12-31`.

### 0.2 Reading the worksheet

The worksheet is the raw record, sorted by (date, id), with **no** value-set tagging and no
concept filtering. Sections: header, conditions (code, system, display, onset, abatement),
procedures (code, display, performed, end), medications (code, display, authored, status),
observations (code, display, date, value, unit, value_code, parent_id, encounter), encounters
(id, class, type, start, end).

- Conditions, procedures, medications and encounters are always listed in full.
- Observations dated **on/after 2024-01-01** are listed in full. Older observations are
  summarised per (code, display) as one row with count / first / last date. No rule in this
  guide reads an observation older than the prior year, so nothing you need is hidden; the
  summary rows only exist so you can see which codes the patient has ever carried.
- Blood pressure readings come as a **parent** observation `85354-9` plus **child** rows
  `8480-6` (systolic) and `8462-4` (diastolic) whose `parent_id` equals the parent's id. Match
  children to their parent by `parent_id`, not by date alone (one date may hold several panels).
- The observation `encounter` column names the encounter in which the reading was taken; look
  that id up in the encounters table to learn its **class** (`AMB`, `IMP`, `EMER`, ...).
- `status` on a medication row is read by **exactly two** rules: the statin on-therapy rule and
  the E3 escalation (ADR-0002). Ignore it everywhere else. Condition
  `clinical_status` / `verification_status` are never shown and never consulted.

### 0.3 Matching concepts to codes — and `events_outside_listed_codes`

Each measure section lists the **code list this demo actually uses** (from the value-set JSON
files), with displays as examples. Apply the rules using those codes. In addition, whenever you
see an event that **clearly matches the concept but is not in the listed codes** (for example a
mammography-like procedure with an unlisted SNOMED code, a hospice-like encounter type, a
different statin product), do **not** silently count or ignore it:

1. label the case using the **listed codes only** (that is the rule the engine claims), and
2. record the event in the case's `events_outside_listed_codes` list as
   `"<section>:<code>:<date>:<why it matches>"`, and
3. if counting that event would flip the label, set `uncertain: true`.

These records are what the adjudication step (dev split only) uses to decide whether a value set
was too narrow; they never change a frozen test label.

### 0.4 Status vocabulary and precedence

Per (patient, measure) you assign exactly one status:

| status | meaning |
|---|---|
| `not_eligible` | the patient is **not** in the denominator (age, sex, no qualifying diagnosis, no qualifying encounter, or death before the MY) |
| `excluded` | in the denominator, but a listed exclusion applies (death in MY, hospice in MY, measure-specific exclusion) |
| `escalate` | in the denominator, not excluded, and a listed **escalation trigger** is present that a careful reviewer would flag rather than decide |
| `closed` | in the denominator, not excluded, no trigger, and the numerator is met |
| `open` | in the denominator, not excluded, no trigger, numerator not met |

**Precedence: `not_eligible` > `excluded` > `escalate` > `closed` / `open`.** Work top-down: decide
eligibility first; if eligible, check exclusions; if none, check escalation triggers; only then
decide closed vs open. A trigger that is present always wins over closed/open, *even when the
numerator is obviously met* (the eval scores `escalate` through `review_flagged`, so an
escalated case is never counted as a missed gap or a false gap).

Global rules that apply to **every** measure (`*/timeline/death_before_my`, `*/exclusion/death`,
`*/exclusion/hospice`, `*/coverage/e1_prior_hospice`, `*/coverage/e4_advanced_illness_hint`):

- **Death before the MY** (`death_date < 2025-01-01`) → `not_eligible` for every measure.
- **Death in the MY** (`2025-01-01 <= death_date <= as_of`) → `excluded` for every measure the
  patient is otherwise eligible for.
- **Hospice in the MY**: any hospice event (see 0.5) dated `2025-01-01 .. as_of` → `excluded`.
- **Hospice before the MY is deterministically NOT an exclusion** (the public wording is "during
  the measurement period"; a third of living Synthea patients carry old hospice codes). Only a
  hospice event dated within **90 days before `my_start`** — i.e. `2024-10-03 .. 2024-12-31` —
  with **no** hospice event inside the MY raises **E1** → `escalate`.
- **E4 advanced-illness hint**: a dementia diagnosis or a dementia medication (see 0.5, any
  date on/before `as_of`) **plus** an encounter of class `IMP` or `EMER` starting in the MY
  **at age >= 66** → `escalate` for every measure the patient is eligible for. Never an
  exclusion.

### 0.5 Shared code lists (from `value_sets/*.json`)

Hospice (`hospice_snomed`) — any of: procedure `385763009` "Hospice care (regime/therapy)";
encounter type `305336008` "Admission to hospice (procedure)"; condition `876882001` (no display
in the panel, carried untested). Look in procedures, encounter types and conditions.

Dementia diagnosis (`dementia_snomed`): condition `26929004` "Alzheimer's disease (disorder)";
`230265002` "Familial Alzheimer's disease of early onset (disorder)" (untested).
Dementia medication (`dementia_meds_rxnorm`): `310436` "Galantamine 4 MG Oral Tablet";
`1599803` "24 HR Donepezil hydrochloride 10 MG / Memantine hydrochloride 28 MG Extended Release
Oral Capsule"; `996740` "Memantine hydrochloride 2 MG/ML Oral Solution".

ESRD (`esrd_snomed`): condition `46177005` "End-stage renal disease (disorder)".
Dialysis (`dialysis_snomed`): procedure `265764009` "Renal dialysis (procedure)"; `302497006`
"Hemodialysis (procedure)" (untested). *Observation* `74006-8` (pre/post-dialysis weight) is
not a dialysis event.
Kidney transplant (`kidney_transplant_snomed`): condition `161665007` "History of renal
transplant (situation)"; condition `213150003` "Kidney transplant failure and rejection
(disorder)"; procedure `70536003` "Transplant of kidney (procedure)" (untested).

Pregnancy (`pregnancy_snomed`): condition `72892002` "Normal pregnancy"; `77386006` "Patient
currently pregnant (finding)" — both untested in the panel. **Trap**: `161744009` "Past pregnancy
history of miscarriage (situation)" is NOT pregnancy; neither is a pregnancy test procedure.

Not representable anywhere (no code carried, or carried untested): palliative care
(`103735009`), I-SNP / long-term institution enrollment, frailty, bilateral mastectomy, total
colectomy (`26390003`), cirrhosis (`19943007`), myalgia / myositis / rhabdomyolysis, IVF,
clomiphene, PCOS. If you see one of these clearly in the record, the rule you label against still
ignores it: label per the listed rules, record it in `events_outside_listed_codes`, and set
`uncertain: true` when it would have excluded the patient.

---

## 1. CBP — Controlling High Blood Pressure (Star C14; `cbp.json`, `cbp-v1`)

Public metric (quoted): members 18–85 with a diagnosis of hypertension whose blood pressure was
adequately controlled (< 140/90 mm Hg).

### Eligibility (denominator)

- **Age 18–85** at Dec 31 of the MY (`cbp/denominator/age`, quoted).
- **Hypertension active** (`cbp/denominator/hypertension_active`, demo choice): a condition
  from `hypertension_snomed` with `onset <= 2025-12-31` and `abatement` empty **or**
  `abatement > 2025-01-01`.
  - Listed code: `59621000` "Essential hypertension (disorder)".
  - Any sex.

### Numerator (closes the gap)

`cbp/numerator/bp_panel` + `cbp/numerator/representative_bp` (demo choices around the quoted
"< 140/90" threshold):

1. A **BP reading** = a parent observation `85354-9` **with BOTH** a systolic child `8480-6` and a
   diastolic child `8462-4` sharing its `parent_id`, both with unit **`mm[Hg]`**, taken in an
   encounter whose class is **not `IMP` and not `EMER`** (look up the observation's `encounter`
   in the encounters table).
2. Consider only readings dated **2025-01-01 .. as_of**.
3. Representative BP = the reading(s) on the **most recent date**; among same-date readings take
   the **lowest systolic** and the **lowest diastolic** (they may come from different panels).
4. **Met iff systolic < 140 AND diastolic < 90.** Otherwise `open`.
5. **No qualifying reading in the MY → `open`** (the engine calls this `no_bp_in_my`; write
   `no_bp_in_my` in the rationale so the subtype can be checked later).

### Exclusions (after death/hospice in MY)

- ESRD diagnosis any time on/before Dec 31 of the MY (`cbp/exclusion/esrd`, quoted):
  condition `46177005`.
- Dialysis procedure any time through Dec 31 of the MY (`cbp/exclusion/dialysis`, demo):
  `265764009` / `302497006`.
- Kidney transplant any time through Dec 31 of the MY (`cbp/exclusion/kidney_transplant`,
  demo): `161665007`, `213150003`, `70536003`.
- Pregnancy diagnosis active in the MY (`cbp/exclusion/pregnancy`, quoted): `72892002`,
  `77386006`; the miscarriage-history code never counts.
- Not representable (ignore, but record): palliative care, I-SNP/LTI, frailty 66–80 with
  advanced illness, frailty 81+.

### Escalation triggers → `escalate`

- **E1** prior hospice (0.4), **E4** dementia + IMP/EMER in MY at 66+ (0.4).
- **E5 incomplete panel / unit** (`cbp/coverage/e5_incomplete_panel_or_unit`): any `85354-9`
  panel in the MY that is **missing** its systolic or diastolic child, or whose child carries a
  unit other than `mm[Hg]` (including an empty unit). Such a panel never counts toward the
  numerator; its mere presence in the MY escalates.
- **E6 hypertension abated in MY** (`cbp/coverage/e6_hypertension_abated_in_my`): the
  hypertension condition has `abatement` dated `2025-01-01 .. 2025-12-31`. The member stays in
  the denominator; the case escalates.

### Worked examples

- *A*: female, 71; `59621000` onset 2015, no abatement; panels in the MY on 2025-03-02
  (138/86, mm[Hg], AMB) and 2025-09-14 (142/84, mm[Hg], AMB). Most recent date = 2025-09-14 →
  142/84 → systolic not < 140 → **`open`**.
- *B*: as A but the 2025-09-14 encounter is class `EMER`. That reading is not a qualifying
  reading; the representative reading is 2025-03-02, 138/86 → **`closed`**.
- *C*: as A but the 2025-09-14 panel has no diastolic child. E5 fires → **`escalate`** (even
  though the 2025-03-02 reading alone would have closed the gap).
- *D*: male, 60, hypertension onset 2019, abatement 2025-05-05, readings fine → E6 →
  **`escalate`**.
- *E*: female, 88, hypertension → age > 85 → **`not_eligible`**.
- *F*: male, 70, hypertension, condition `46177005` onset 2018 → **`excluded`** (ESRD), regardless
  of BP values.

---

## 2. EED — Eye Exam for Patients With Diabetes (Star C11; `eed.json`, `eed-v1`)

Public metric (quoted): diabetic members 18–75 who had a retinal eye exam during the MY.

### Eligibility

- **Age 18–75** at Dec 31 of the MY (`eed/denominator/age`, quoted). Any sex.
- **Diabetes active in the MY or the prior year** (`eed/denominator/diabetes_active`, demo):
  a `diabetes_snomed` condition with `onset <= 2025-12-31` and `abatement` empty **or**
  `abatement >= 2024-01-01`. Listed codes: `44054006` "Diabetes mellitus type 2";
  `427089005` "Diabetes mellitus due to cystic fibrosis"; `60951000119105` "Blindness due to
  type 2 diabetes mellitus"; `127013003` "Disorder of kidney due to diabetes mellitus";
  `97331000119101` "Macular edema and retinopathy due to type 2 diabetes mellitus";
  `90781000119102` "Microalbuminuria due to type 2 diabetes mellitus"; `368581000119106`
  "Neuropathy due to type 2 diabetes mellitus"; `1551000119108` "Nonproliferative diabetic
  retinopathy due to type II diabetes mellitus"; `1501000119109` "Proliferative diabetic
  retinopathy due to type II diabetes mellitus"; `157141000119108` "Proteinuria due to type 2
  diabetes mellitus".
- **Prediabetes never qualifies**: `714628002` "Prediabetes (finding)" is a trap code.

### Numerator

- **Retinal exam in the MY** (`eed/numerator/retinal_exam_in_my`, quoted): a `retinal_exam_proc`
  procedure dated `2025-01-01 .. as_of`. Listed codes: `722161008` "Diabetic retinal eye exam
  (procedure)"; `700070005` "Optical coherence tomography of retina (procedure)".
- **OR prior-year negative exam** (`eed/numerator/prior_year_negative_exam`, demo): a
  `retinal_exam_proc` procedure dated `2024-01-01 .. 2024-12-31` **and**, on the same date, an
  observation `71490-7` (left eye) **or** `71491-5` (right eye) whose `value_code` is
  **`LA18643-9`** ("No apparent retinopathy") — with **no** diabetic-retinopathy diagnosis whose
  onset is on or before that exam date. Retinopathy diagnoses (`diabetic_retinopathy_snomed`):
  `1551000119108`, `1501000119109`, `97331000119101`. Note these three are also diabetes codes,
  so a patient in the denominator *because of* a retinopathy code can never use the prior-year
  route. Positive answers `LA18644-7`, `LA18645-4`, `LA18646-2`, `LA18648-8` do not count.
  The rule text says `71490-7` **or** `71491-5`, so one negative eye answer is enough to close;
  when the other eye is positive or absent, or the answer is dated on a different day than the
  exam procedure, label per the rule as written and set `uncertain: true`.

### Exclusions

- Death / hospice in MY (0.4). Not representable (ignore, record): palliative care,
  PCOS/gestational/steroid-induced diabetes without a diabetes diagnosis, I-SNP/LTI, frailty.

### Escalation triggers → `escalate`

- **E1**, **E4** (0.4).
- **E6 diabetes abated in MY** (`eed/coverage/e6_diabetes_abated_in_my`): *any* qualifying
  diabetes condition has `abatement` dated `2025-01-01 .. 2025-12-31`. Member stays in the
  denominator; escalate.

### Worked examples

- *A*: female, 68; `44054006` onset 2012; `722161008` on 2025-04-10 → **`closed`**.
- *B*: as A but the only exam is `722161008` on 2024-06-01 with `71490-7 = LA18643-9` and
  `71491-5 = LA18643-9` the same day, no retinopathy dx → **`closed`**.
- *C*: as B but `1551000119108` onset 2023-02-02 → prior-year route void → **`open`**.
- *D*: male, 77, diabetes → age > 75 → **`not_eligible`**.
- *E*: female, 60, only `714628002` Prediabetes → **`not_eligible`**.

---

## 3. BCS — Breast Cancer Screening (Star C01; `bcs.json`, `bcs-v1`)

Public metric (quoted): women 52–74 who had a mammogram in the past two years. (The public
Description says 50–74; the demo implements the Metric's **52–74** — `bcs/denominator/age`.)

### Eligibility

- **Sex = female** (`bcs/denominator/sex`, quoted). `male` / `other` → `not_eligible`; an
  unknown sex → `escalate` (write "sex unknown" in the rationale).
- **Age 52–74** at Dec 31 of the MY.
- No diagnosis requirement.

### Numerator

- A `mammogram_proc` procedure dated **2023-10-01 .. as_of** (`bcs/numerator/mammogram`,
  quoted + `bcs/numerator/window_27_months`, demo: Oct 1 of MY-2 through as_of). Listed codes:
  `71651007` "Mammography (procedure)"; `24623002` "Screening mammography (procedure)";
  `241055006` "Mammogram - symptomatic (procedure)".

### Exclusions

- Death / hospice in MY (0.4). Not representable (ignore, record): bilateral mastectomy,
  palliative care, I-SNP/LTI, frailty.

### Escalation triggers → `escalate`

- **E1**, **E4** (0.4) only.

### Worked examples

- *A*: female, 66, `71651007` on 2023-11-20 → inside 2023-10-01..as_of → **`closed`**.
- *B*: female, 66, `71651007` on 2023-09-15 only → **`open`**.
- *C*: female, 75 → **`not_eligible`**. *D*: male, 60 → **`not_eligible`**.
- *E*: female, 70, `385763009` Hospice care on 2025-02-01, mammogram 2024 → **`excluded`**.
- *F*: female, 70, hospice care on 2024-11-15 only, mammogram 2024 → E1 → **`escalate`**.

---

## 4. COL — Colorectal Cancer Screening (Star C02; `col.json`, `col-v1`)

Public metric (quoted): members 50–75 with appropriate colorectal cancer screening.

### Eligibility

- **Age 50–75** at Dec 31 of the MY (`col/denominator/age`, quoted). Any sex.

### Numerator (either route)

- **Colonoscopy** procedure `73761001` "Colonoscopy (procedure)" dated **2016-01-01 .. as_of**
  (`col/numerator/colonoscopy_my_plus_9_prior_years`, demo: Jan 1 of MY-9 through as_of).
- **FOBT / FIT in the MY** (`col/numerator/fobt_fit_in_my`, demo): an observation `57905-2`
  "Hemoglobin.gastrointestinal.lower [Presence] in Stool by Immunoassay --1st specimen" **or**
  a procedure `104435004` "Screening for occult blood in feces (procedure)", dated
  `2025-01-01 .. as_of`. The result value is irrelevant.
- Not representable (ignore, record): flexible sigmoidoscopy (MY + 4 prior years), CT
  colonography (MY + 4), sDNA-FIT (MY + 2).

### Exclusions

- **Colorectal cancer any time** on/before `as_of` (`col/exclusion/colorectal_cancer`, quoted):
  condition `363406005` "Malignant neoplasm of colon"; `109838007` "Overlapping malignant
  neoplasm of colon"; `93761005` "Primary malignant neoplasm of colon"; `363351006` "Malignant
  tumor of rectum" (untested).
- Death / hospice in MY (0.4). Not representable (ignore, record): total colectomy,
  palliative care, I-SNP/LTI, frailty.

### Escalation triggers → `escalate`

- **E1**, **E4** (0.4).
- **E7 ambiguous colon code** (`col/coverage/e7_ambiguous_colon_code`): any
  `colon_ambiguous_snomed` condition **or** procedure dated on/before `as_of`: `43075005`
  "Partial resection of colon (procedure)"; `94260004` "Secondary malignant neoplasm of colon
  (disorder)" (untested). Deliberately **not** ambiguous (ordinary findings, do not escalate):
  `68496003` polyp of colon, `76164006` biopsy of colon, `410006001` digital examination of
  rectum.

### Worked examples

- *A*: male, 62, colonoscopy 2017-05-05 → **`closed`**. *B*: colonoscopy 2015-12-30 only →
  **`open`**.
- *C*: female, 70, `104435004` on 2025-08-08 → **`closed`**; the same procedure on 2024-08-08
  only → **`open`**.
- *D*: male, 68, `363406005` onset 2009 → **`excluded`**.
- *E*: female, 55, `43075005` on 2021-01-01, colonoscopy 2021 → E7 → **`escalate`**.
- *F*: age 76 → **`not_eligible`**.

---

## 5. SPC — Statin Therapy for Patients With Cardiovascular Disease (Star C19; `spc.json`, `spc-v1`)

Public metric (quoted): males 21–75 and females 40–75 with clinical ASCVD who were dispensed at
least one high- or moderate-intensity statin during the MY.

### Eligibility

- **Age/sex** (`spc/denominator/age_sex`, quoted): male **21–75** or female **40–75** at Dec 31
  of the MY. Unknown sex: eligible when the age is inside both bands (40–75), not eligible when
  outside both, otherwise `escalate`.
- **ASCVD** (`spc/denominator/ascvd` quoted + `spc/denominator/ascvd_onset` demo): any
  `ascvd_snomed` condition with `onset <= 2025-12-31`; **abatement is ignored**. Listed codes:
  `414545008` "Ischemic heart disease (disorder)"; `22298006` "Myocardial infarction";
  `401303003` "Acute ST segment elevation myocardial infarction"; `401314000` "Acute non-ST
  segment elevation myocardial infarction"; `399211009` "History of myocardial infarction
  (situation)"; `4557003` "Preinfarction syndrome"; `399261000` "History of coronary artery
  bypass grafting (situation)"; `230690007` "Cerebrovascular accident".
  - Not in the list (record in `events_outside_listed_codes` if you consider them ASCVD
    evidence, but do not count): procedures such as coronary angiography, PCI, CABG, and the
    finding "Abnormal findings diagnostic imaging heart+coronary circulat".

### Numerator — ON THERAPY with moderate/high intensity

`spc/numerator/on_therapy_status` (demo, ADR-0002) — a statin request from `statin_rxnorm`
counts as on therapy in the MY when **either**:

- it is **authored `2025-01-01 .. as_of`**, **whatever its status except** `stopped`,
  `cancelled`, `entered-in-error` (those never count); **or**
- it is authored **before 2025-01-01 and its `status` is exactly `active`**.

(`completed`, `on-hold`, `unknown`, empty status authored before the MY do **not** count.)

`spc/numerator/intensity` (demo, public ACC/AHA table, `statin_intensity.json`):

| product | low | moderate | high |
|---|---|---|---|
| simvastatin | 10 mg (`314231`) | 20 mg (`312961`), 40 mg (`198211`) | 80 mg (`200345`) |
| atorvastatin | — | 10 mg (`617312`, `617314`), 20 mg (`617310`) | 40 mg (`617311`, `617320`), 80 mg (`259255`) |
| rosuvastatin | — | 5 mg (`859424`, `859426`), 10 mg (`859747`, `859749`) | 20 mg (`859751`, `859753`), 40 mg (`859419`) |
| pravastatin | 10 mg (`904458`), 20 mg (`904467`) | 40 mg (`904475`), 80 mg (`904481`) | — |
| lovastatin | 10 mg (`197903`), 20 mg (`197904`) | 40 mg (`197905`) | — |
| pitavastatin | — | 2 mg (`861650`) | — |
| ezetimibe/simvastatin | — | 10/20 (`476349`), 10/40 (`476350`) | — |

- **`closed`** iff at least one on-therapy statin is moderate or high intensity.
- Only low-intensity statins on therapy → **`open`** (write `low_intensity_only` in the
  rationale).
- No statin on therapy → **`open`**.
- A statin on therapy whose code is not in the table → **`escalate`** (rationale: "statin
  without intensity entry"; also record it in `events_outside_listed_codes`).

### Exclusions

- **ESRD or dialysis in the MY or the prior year** (`spc/exclusion/esrd_or_dialysis`, quoted):
  condition `46177005` active in `2024-01-01 .. 2025-12-31` (onset <= 2025-12-31 and abatement
  empty or >= 2024-01-01), **or** a dialysis procedure (`265764009`, `302497006`) dated in that
  window.
- **Pregnancy in the MY or the prior year** (`spc/exclusion/pregnancy`, quoted): `72892002`,
  `77386006` active in `2024-01-01 .. 2025-12-31`.
- Death / hospice in MY (0.4). Not representable (ignore, record): cirrhosis, myalgia /
  myositis / myopathy / rhabdomyolysis, IVF, clomiphene, palliative care, I-SNP/LTI, frailty.

### Escalation triggers → `escalate`

- **E1**, **E4** (0.4).
- **E3 medication status conflict** (`spc/coverage/e3_medication_status_conflict`): a
  `statin_rxnorm` request **authored inside the MY** whose `status` is `stopped` or `cancelled`
  — escalate **even when another statin closes the numerator**.

### Worked examples

- *A*: male, 70; `414545008` onset 2010; `312961` simvastatin 20 authored 2025-03-01, status
  `active` → moderate, on therapy → **`closed`**.
- *B*: as A but the only statin is `314231` simvastatin 10 authored 2025-03-01 → low only →
  **`open`** (`low_intensity_only`).
- *C*: as A but `312961` authored 2019-05-05 with status `completed` → not on therapy →
  **`open`**. With status `active` instead → **`closed`**.
- *D*: as A plus a second request `617311` atorvastatin 40 authored 2025-06-01 status
  `stopped` → E3 → **`escalate`**.
- *E*: female, 38, ASCVD → outside 40–75 → **`not_eligible`**. Male, 38 → inside 21–75 →
  eligible.
- *F*: male, 65, ASCVD, `265764009` Renal dialysis on 2024-03-03 → **`excluded`**.

---

## 6. SPD — Statin Use in Persons With Diabetes (Star D12-style proxy; `spd.json`, `spd-v1`)

Public metric (quoted, Part D fill measure): beneficiaries 40–75 with two diabetes medication
fills who received a statin fill. The demo evaluates a **diagnosis-based proxy over
MedicationRequest rows** (no fills exist in the data).

### Eligibility

- **Age 40–75** at Dec 31 of the MY (`spd/denominator/age`). Any sex.
- **Diabetes by diagnosis exactly as EED** (`spd/denominator/diabetes_diagnosis_proxy`, demo):
  a `diabetes_snomed` condition (list in section 2) active in the MY or the prior year;
  prediabetes never.
- **NOT in the SPC denominator** (`spd/denominator/not_in_spc_denominator`, demo, product
  choice): if the patient meets SPC eligibility (section 5 age/sex band **and** an ASCVD
  condition with onset <= 2025-12-31) they are `not_eligible` for SPD. Apply the SPC age/sex
  test as written: e.g. a female 40–75 with ASCVD → SPC → SPD `not_eligible`; a male aged 40–75
  with ASCVD → SPC → SPD `not_eligible`. (Everyone 40–75 is inside the SPC band for their sex, so
  in practice: **diabetes + any listed ASCVD code → SPD `not_eligible`**.)
- Not representable (ignore): IPSD >= 90 days before MY end; continuous enrollment.

### Numerator — ON THERAPY, any intensity

- Same on-therapy definition as SPC (`spd/numerator/on_therapy_status`): authored in the MY
  with status not `stopped` / `cancelled` / `entered-in-error`, **or** authored earlier with
  status exactly `active`.
- **Any intensity counts** (`spd/numerator/any_intensity`), including simvastatin 10 mg.
- Listed statin codes: the `statin_rxnorm` list in the section 5 table. A statin-looking product
  outside the list: record in `events_outside_listed_codes`, do not count, `uncertain: true` if
  it would flip the label.

### Exclusions

- **ESRD or dialysis** (`spd/exclusion/esrd_or_dialysis`, quoted): same window and codes as SPC
  (`2024-01-01 .. 2025-12-31`).
- Death / hospice in MY (0.4). Prediabetes is handled at the denominator. Not representable
  (ignore, record): rhabdomyolysis / myopathy, pregnancy / lactation / fertility, cirrhosis,
  PCOS. **Pregnancy is NOT an SPD exclusion in this demo** even though it is one for SPC.

### Escalation triggers → `escalate`

- **E1**, **E4** (0.4); **E3** exactly as SPC (statin authored in the MY with status
  `stopped` / `cancelled`).

### Worked examples

- *A*: female, 60; `44054006` onset 2015; no ASCVD; `314231` simvastatin 10 authored
  2025-02-02 status `active` → **`closed`** (low intensity is fine for SPD).
- *B*: as A with the statin authored 2018 and status `completed` → **`open`**.
- *C*: as A plus `414545008` onset 2020 → in SPC denominator → SPD **`not_eligible`** (label
  SPC instead).
- *D*: male, 72, diabetes, no ASCVD, `46177005` onset 2023 → **`excluded`**.

---

## 7. TSC — Tobacco Use Screening and SNS — Social Need Screening (screening proxies; `tsc.json`, `sns.json`)

One shared rule; two labels. Counts-only in the eval (not part of the headline P/R/F1), but label
them the same way.

### Eligibility (both)

- **Age >= 18** at Dec 31 of the MY (`tsc/denominator/age_18_plus`, demo).
- **At least one encounter of any class** starting `2025-01-01 .. as_of`
  (`*/denominator/encounter_in_my`, demo). No encounter in the MY → `not_eligible`.

### Numerator

- **TSC**: an observation `72166-2` "Tobacco smoking status" dated `2025-01-01 .. as_of` with a
  **non-empty `value_code`** (`tsc/numerator/tobacco_status_answer`). The answer itself
  (`266919005` never smoked, `8517006` ex-smoker, `449868002` smokes daily, ...) is not judged.
  A `72166-2` row with an empty `value_code` does not count. Cessation intervention: not
  representable.
- **SNS**: an observation `93025-5` "Protocol for Responding to and Assessing Patients' Assets,
  Risks, and Experiences [PRAPARE]" dated `2025-01-01 .. as_of` (`sns/numerator/prapare_panel`).
  A standalone `71802-3` "Housing status" is **not** a screening. Domain screens and
  interventions: not representable.

### Exclusions and escalations

- Death / hospice in MY → `excluded` (0.4). Escalations: only the global **E1** and **E4**.

### Worked examples

- *A*: 70, encounter 2025-05-05, `72166-2` on 2025-05-05 with value_code `266919005` → TSC
  **`closed`**; `93025-5` the same day → SNS **`closed`**.
- *B*: 70, only encounters in 2024 → TSC and SNS **`not_eligible`**.
- *C*: 70, encounter 2025-05-05 with no `93025-5` in 2025 (one in 2024) → SNS **`open`**.

---

## 8. Recording a label

One JSONL row per (patient, measure) for **all eight measures** of every selected patient, in
`evals/gold/gap_cases.jsonl`:

```json
{"patient_id": "<uuid>", "measure_id": "CBP", "as_of": "2025-12-31", "gold": "open",
 "rationale": "htn 59621000 onset 2016-02-01; most recent panel 2025-09-14 142/84 mm[Hg] AMB; no_bp_in_my=false",
 "decisive_dates": ["2016-02-01", "2025-09-14"], "uncertain": false, "labeler": "<initials>",
 "events_outside_listed_codes": []}
```

- `gold` ∈ `not_eligible | closed | excluded | open | escalate` (precedence in 0.4).
- `rationale`: the decisive facts in one line — codes, dates, values, and which element decided
  (e.g. `E5`, `no_bp_in_my`, `low_intensity_only`, `prior-year negative exam`).
- `decisive_dates`: every ISO date the decision hinges on.
- `uncertain`: `true` when a judgement call was needed (unlisted-but-matching code, ambiguous
  window edge, single-eye negative exam, unknown sex).
- `events_outside_listed_codes`: see 0.3 (empty list when none).
- Split (`dev` / `test`) is derived from `sha256(patient_id)` by the loader; never write it by
  hand. After the file is complete: freeze (`FREEZE.json`), then — and only then — engine
  contact.

## 9. Checklist before you save a patient

1. Header: age at Dec 31 2025, sex, death date → apply death rules to all eight measures first.
2. Hospice: any hospice event in 2025 → `excluded` everywhere the patient is eligible; hospice in
   2024-10-03..2024-12-31 with none in 2025 → E1 `escalate` everywhere eligible.
3. Dementia dx/med + IMP/EMER encounter in 2025 + age >= 66 → E4 `escalate` everywhere eligible.
4. Then per measure: eligibility → measure exclusions → measure escalations (E3, E5, E6, E7) →
   numerator → `closed` / `open`.
5. Never consult the engine. Never edit this guide. Note every ambiguity in `rationale`.
