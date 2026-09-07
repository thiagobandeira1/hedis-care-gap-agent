# Escalation / exclusion slice feasibility scan

Descriptive scan of the 237-patient synthetic (Synthea) panel at as_of 2025-12-31 (measurement year 2025) for the carriers of the escalation triggers E1, E3, E4, E5, E6, E7 and of the hospice-in-MY exclusion. Facts come from the raw P6 record at `?to=as_of` and the value-set JSON files read as plain code lists; no engine verdict, rule module or engine output was consulted (SPEC section 6: selection on descriptive facts only). There is no draw: every carrier within the labeling-feasibility cap of 2500 events who is not among the 60 patients of the base selection is selected, in sorted id order.

Selected: **12** patients; 1 carrier(s) already in the base slice; 14 carrier(s) over the cap.

## Trigger definitions (descriptive facts, not rules)

- `E1`: hospice_snomed code (procedure / condition / encounter type) dated in the 90 days before Jan 1 of the MY
- `E3`: statin_rxnorm request authored inside [Jan 1 MY, as_of] with status cancelled / entered-in-error / stopped
- `E4`: dementia_snomed condition active at as_of (onset <= as_of, abatement null or >= Jan 1 MY) + an IMP/EMER encounter starting inside [Jan 1 MY, as_of] + age >= 66 at Dec 31 of the MY
- `E5`: an 85354-9 observation inside [Jan 1 MY, as_of] whose child observations lack 8480-6 or 8462-4, or carry a unit other than mm[Hg]
- `E6`: hypertension_snomed or diabetes_snomed condition with abatement_date inside [Jan 1 MY, as_of]
- `E7`: colon_ambiguous_snomed code in conditions or procedures dated <= as_of
- `hospice_in_my`: hospice_snomed code (procedure / condition / encounter type) dated inside [Jan 1 MY, as_of] (an exclusion carrier, not an escalation)

Note: E1 is recorded whenever a hospice event falls in the 90-day window; the guide raises E1 as a label only when no hospice event lies inside the MY (a patient carrying both is an exclusion carrier first).

## Carriers per trigger

| trigger | whole panel | inside cap | already in base | selected |
|---|---|---|---|---|
| E1 | 1 | 0 | 0 | 0 |
| E3 | 0 | 0 | 0 | 0 |
| E4 | 10 | 4 | 0 | 4 |
| E5 | 0 | 0 | 0 | 0 |
| E6 | 0 | 0 | 0 | 0 |
| E7 | 5 | 4 | 1 | 3 |
| hospice_in_my | 12 | 5 | 0 | 5 |

## Triggers with zero carriers

The following triggers have **no carrier anywhere in the panel** at this anchor, so they cannot be represented in gold at as_of 2025-12-31: `E3`, `E5`, `E6`.
- `E3`: 0 carriers — statin_rxnorm request authored inside [Jan 1 MY, as_of] with status cancelled / entered-in-error / stopped
- `E5`: 0 carriers — an 85354-9 observation inside [Jan 1 MY, as_of] whose child observations lack 8480-6 or 8462-4, or carry a unit other than mm[Hg]
- `E6`: 0 carriers — hypertension_snomed or diabetes_snomed condition with abatement_date inside [Jan 1 MY, as_of]

## Selected patients

| patient_id | age | sex | deceased | events | triggers |
|---|---|---|---|---|---|
| 041763e0-8c8e-8303-eb95-33215f628131 | 74 | female |  | 848 | E4 |
| 31e0596e-ba9a-30a7-a7a0-81b6ca117f56 | 85 | male |  | 681 | hospice_in_my |
| 4250b90f-a3d3-57f4-4f71-236c449994d8 | 79 | male |  | 666 | E7 |
| 6e3d889d-386a-0f33-2156-05cb8938e3e1 | 79 | male |  | 725 | E4 |
| 7b0025d7-1f5b-22e6-4202-b9e23a715428 | 85 | female |  | 520 | hospice_in_my |
| 7d33cc51-a3cd-92a9-d8df-ca3c92f31709 | 75 | female |  | 607 | hospice_in_my |
| 9f8d5b91-1c76-fd0d-da90-b0489098fca0 | 90 | female |  | 786 | E4 |
| a0548efc-9569-549b-a01d-ab8f983095b1 | 78 | female |  | 1047 | E7 |
| b7e8cab2-ad4d-446b-51d1-db3ca854c0c7 | 85 | male |  | 959 | hospice_in_my |
| be7a5cad-5316-ee4b-5470-4093f20ff5ad | 85 | female |  | 529 | hospice_in_my |
| d245882f-26cd-11c3-fa90-44da7048dfb0 | 91 | male |  | 776 | E4 |
| ec3e67c3-185f-54fb-2dd6-408c73157431 | 77 | male |  | 582 | E7 |

## Evidence per selected patient

### 041763e0-8c8e-8303-eb95-33215f628131

- `E4`:
  - condition 26929004 onset 2025-09-17 abatement none
  - encounter EMER start 2025-03-09

### 31e0596e-ba9a-30a7-a7a0-81b6ca117f56

- `hospice_in_my`:
  - encounter type 305336008 start 2025-10-08
  - procedure 385763009 2025-10-08
  - procedure 385763009 2025-10-09
  - procedure 385763009 2025-10-10
  - procedure 385763009 2025-10-11
  - procedure 385763009 2025-10-12
  - procedure 385763009 2025-10-13
  - procedure 385763009 2025-10-14
  - procedure 385763009 2025-10-15
  - procedure 385763009 2025-10-16
  - procedure 385763009 2025-10-17
  - procedure 385763009 2025-10-18
  - procedure 385763009 2025-10-19
  - procedure 385763009 2025-10-20
  - procedure 385763009 2025-10-21
  - procedure 385763009 2025-10-22
  - procedure 385763009 2025-10-23
  - procedure 385763009 2025-10-24
  - procedure 385763009 2025-10-25
  - procedure 385763009 2025-10-26
  - procedure 385763009 2025-10-27
  - procedure 385763009 2025-10-28
  - procedure 385763009 2025-10-29
  - procedure 385763009 2025-10-30
  - procedure 385763009 2025-10-31
  - procedure 385763009 2025-11-01
  - procedure 385763009 2025-11-02
  - procedure 385763009 2025-11-03
  - procedure 385763009 2025-11-04
  - procedure 385763009 2025-11-05
  - procedure 385763009 2025-11-06
  - procedure 385763009 2025-11-07
  - procedure 385763009 2025-11-08
  - procedure 385763009 2025-11-09
  - procedure 385763009 2025-11-10
  - procedure 385763009 2025-11-11
  - procedure 385763009 2025-11-12
  - procedure 385763009 2025-11-13
  - procedure 385763009 2025-11-14
  - procedure 385763009 2025-11-15
  - procedure 385763009 2025-11-16
  - procedure 385763009 2025-11-17
  - procedure 385763009 2025-11-18
  - procedure 385763009 2025-11-19
  - procedure 385763009 2025-11-20
  - procedure 385763009 2025-11-21
  - procedure 385763009 2025-11-22
  - procedure 385763009 2025-11-23
  - procedure 385763009 2025-11-24
  - procedure 385763009 2025-11-25
  - procedure 385763009 2025-11-26
  - procedure 385763009 2025-11-27

### 4250b90f-a3d3-57f4-4f71-236c449994d8

- `E7`:
  - procedure 43075005 2023-09-18

### 6e3d889d-386a-0f33-2156-05cb8938e3e1

- `E4`:
  - condition 26929004 onset 2024-01-15 abatement none
  - encounter EMER start 2025-08-04

### 7b0025d7-1f5b-22e6-4202-b9e23a715428

- `hospice_in_my`:
  - encounter type 305336008 start 2025-10-01
  - procedure 385763009 2025-10-01
  - procedure 385763009 2025-10-02
  - procedure 385763009 2025-10-03
  - procedure 385763009 2025-10-04
  - procedure 385763009 2025-10-05
  - procedure 385763009 2025-10-06
  - procedure 385763009 2025-10-07
  - procedure 385763009 2025-10-08
  - procedure 385763009 2025-10-09
  - procedure 385763009 2025-10-10
  - procedure 385763009 2025-10-11
  - procedure 385763009 2025-10-12
  - procedure 385763009 2025-10-13
  - procedure 385763009 2025-10-14
  - procedure 385763009 2025-10-15

### 7d33cc51-a3cd-92a9-d8df-ca3c92f31709

- `hospice_in_my`:
  - encounter type 305336008 start 2025-06-09
  - procedure 385763009 2025-06-09
  - procedure 385763009 2025-06-10
  - procedure 385763009 2025-06-11
  - procedure 385763009 2025-06-12
  - procedure 385763009 2025-06-13
  - procedure 385763009 2025-06-14
  - procedure 385763009 2025-06-15
  - procedure 385763009 2025-06-16
  - procedure 385763009 2025-06-17
  - procedure 385763009 2025-06-18
  - procedure 385763009 2025-06-19
  - procedure 385763009 2025-06-20
  - procedure 385763009 2025-06-21
  - procedure 385763009 2025-06-22

### 9f8d5b91-1c76-fd0d-da90-b0489098fca0

- `E4`:
  - condition 26929004 onset 2023-01-13 abatement none
  - encounter EMER start 2025-06-27

### a0548efc-9569-549b-a01d-ab8f983095b1

- `E7`:
  - procedure 43075005 2023-03-10

### b7e8cab2-ad4d-446b-51d1-db3ca854c0c7

- `hospice_in_my`:
  - encounter type 305336008 start 2025-03-13
  - procedure 385763009 2025-03-13
  - procedure 385763009 2025-03-14
  - procedure 385763009 2025-03-15
  - procedure 385763009 2025-03-16
  - procedure 385763009 2025-03-17
  - procedure 385763009 2025-03-18
  - procedure 385763009 2025-03-19
  - procedure 385763009 2025-03-20
  - procedure 385763009 2025-03-21
  - procedure 385763009 2025-03-22
  - procedure 385763009 2025-03-23
  - procedure 385763009 2025-03-24
  - procedure 385763009 2025-03-25
  - procedure 385763009 2025-03-26
  - procedure 385763009 2025-03-27
  - procedure 385763009 2025-03-28
  - procedure 385763009 2025-03-29
  - procedure 385763009 2025-03-30
  - procedure 385763009 2025-03-31
  - procedure 385763009 2025-04-01
  - procedure 385763009 2025-04-02
  - procedure 385763009 2025-04-03
  - procedure 385763009 2025-04-04
  - procedure 385763009 2025-04-05
  - procedure 385763009 2025-04-06
  - procedure 385763009 2025-04-07
  - procedure 385763009 2025-04-08
  - procedure 385763009 2025-04-09
  - procedure 385763009 2025-04-10
  - procedure 385763009 2025-04-11
  - procedure 385763009 2025-04-12
  - procedure 385763009 2025-04-13
  - procedure 385763009 2025-04-14
  - procedure 385763009 2025-04-15
  - procedure 385763009 2025-04-16

### be7a5cad-5316-ee4b-5470-4093f20ff5ad

- `hospice_in_my`:
  - encounter type 305336008 start 2025-11-20
  - procedure 385763009 2025-11-20
  - procedure 385763009 2025-11-21
  - procedure 385763009 2025-11-22
  - procedure 385763009 2025-11-23
  - procedure 385763009 2025-11-24
  - procedure 385763009 2025-11-25
  - procedure 385763009 2025-11-26
  - procedure 385763009 2025-11-27
  - procedure 385763009 2025-11-28
  - procedure 385763009 2025-11-29
  - procedure 385763009 2025-11-30
  - procedure 385763009 2025-12-01
  - procedure 385763009 2025-12-02
  - procedure 385763009 2025-12-03
  - procedure 385763009 2025-12-04
  - procedure 385763009 2025-12-05
  - procedure 385763009 2025-12-06
  - procedure 385763009 2025-12-07
  - procedure 385763009 2025-12-08
  - procedure 385763009 2025-12-09
  - procedure 385763009 2025-12-10
  - procedure 385763009 2025-12-11
  - procedure 385763009 2025-12-12
  - procedure 385763009 2025-12-13
  - procedure 385763009 2025-12-14
  - procedure 385763009 2025-12-15
  - procedure 385763009 2025-12-16
  - procedure 385763009 2025-12-17
  - procedure 385763009 2025-12-18
  - procedure 385763009 2025-12-19
  - procedure 385763009 2025-12-20
  - procedure 385763009 2025-12-21
  - procedure 385763009 2025-12-22
  - procedure 385763009 2025-12-23
  - procedure 385763009 2025-12-24
  - procedure 385763009 2025-12-25
  - procedure 385763009 2025-12-26
  - procedure 385763009 2025-12-27
  - procedure 385763009 2025-12-28
  - procedure 385763009 2025-12-29
  - procedure 385763009 2025-12-30
  - procedure 385763009 2025-12-31

### d245882f-26cd-11c3-fa90-44da7048dfb0

- `E4`:
  - condition 26929004 onset 2025-10-23 abatement none
  - encounter EMER start 2025-07-28
  - encounter IMP start 2025-04-14
  - encounter IMP start 2025-07-28

### ec3e67c3-185f-54fb-2dd6-408c73157431

- `E7`:
  - procedure 43075005 2016-11-06

## Carriers already in the base selection (not re-selected)

| patient_id | events | triggers |
|---|---|---|
| d671b0c6-112a-da67-eb69-116323b2b1cc | 639 | E7 |

## Carriers over the cap

| patient_id | events | triggers |
|---|---|---|
| 011e8550-b07a-2551-ef2f-5f20c444087e | 7154 | hospice_in_my |
| 03eff367-7785-855a-e825-4833859d53b3 | 5789 | hospice_in_my |
| 03f39a0b-652e-85b8-8307-39cb8557d9cc | 2590 | hospice_in_my |
| 041bfe17-302a-d7dd-3f31-adade5e9a4b1 | 6350 | E1, hospice_in_my |
| 19926368-ebd4-68e0-65f1-af523e6eb460 | 5944 | hospice_in_my |
| 3532aa5f-c4f4-5b21-3833-04308a64ab0c | 10572 | E4 |
| 3813defe-9d73-2a06-3750-1b036db970f7 | 2620 | E7 |
| 53380749-54e9-c78d-309c-d9d93fa28964 | 4874 | E4 |
| 96c046f7-d890-20a2-5929-ebec2254ee95 | 16300 | hospice_in_my |
| 9a5add03-d6a9-3df1-1f07-f81e30120bd0 | 20098 | E4 |
| b0b5f662-a5ae-6c21-ebd0-f3a049e3129d | 10989 | E4 |
| e49988d2-a8c2-4e8f-a889-397f20f17378 | 15165 | E4 |
| ef19d120-4c6b-7f5b-6edd-f546be241623 | 11300 | hospice_in_my |
| fc68fc9c-aab7-a833-d702-fb70391eb399 | 18864 | E4 |
