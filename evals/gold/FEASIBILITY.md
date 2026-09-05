# Gold panel feasibility scan

Descriptive scan of the 237-patient synthetic (Synthea) panel at as_of 2025-12-31 (measurement year 2025), computed from the raw P6 record only: age at Dec 31 of the MY, sex, death on/before as_of, case-insensitive keyword flags over condition / procedure / medication display strings, and the presence of a few observation codes inside the MY. No value set and no engine verdict was consulted (SPEC section 6: selection on descriptive facts only).

Draw: n=60, seed=20260903, cap=2500 events; selected 60 (34 filler).

## Labeling-feasibility cap

Total events dated <= as_of (conditions + procedures + medications + observations + encounters): min 319, median 790, max 20098. Patients over the cap of 2500 are excluded from the draw because a blind labeler cannot read them: **56 excluded**, 181 eligible for the draw.

| events <= as_of | patients | of which deceased |
|---|---|---|
| <= 500 | 27 | 0 |
| 501-1000 | 117 | 0 |
| 1001-2500 | 37 | 0 |
| 2501-5000 | 19 | 0 |
| 5001-10000 | 20 | 0 |
| > 10000 | 17 | 0 |

## Panel by age band and sex (alive vs deceased on/before as_of)

| age at Dec 31 | female | male | alive | deceased | total | inside cap |
|---|---|---|---|---|---|---|
| 50-64 | 4 | 1 | 5 | 0 | 5 | 4 |
| 65-75 | 44 | 35 | 79 | 0 | 79 | 63 |
| 76-85 | 41 | 38 | 79 | 0 | 79 | 62 |
| 86+ | 49 | 25 | 74 | 0 | 74 | 52 |
| all | 138 | 99 | 237 | 0 | 237 | 181 |

## Descriptive flags

Keyword patterns (case-insensitive) over display strings:

- `hypertension`: `hypertension`
- `diabetes`: `(?<!pre)diabetes`
- `ascvd`: `myocardial infarction|coronary|ischemic heart|stroke|peripheral vascular|angina`
- `statin`: `simvastatin|atorvastatin|rosuvastatin|pravastatin|lovastatin|pitavastatin|fluvastatin`
- `mammogram`: `mammogra`
- `colonoscopy`: `colonoscopy`
- `fobt_fit`: `fecal occult|occult blood|fecal immunochemical|\bfit\b`
- `eye_exam`: `retinal|ophthalm|eye exam`
- `hospice`: `hospice`
- `dementia`: `dementia|alzheimer`
- `esrd_dialysis`: `dialysis|renal failure|end[- ]stage`
- `statin_stopped`: a statin-keyword medication with status stopped / cancelled
- `bp_panel_in_my`: observation code 85354-9 dated inside the MY
- `tobacco_screen_in_my`: observation code 72166-2 dated inside the MY
- `prapare_in_my`: observation code 93025-5 dated inside the MY
- `encounter_in_my`: any encounter starting inside the MY
- `inpatient_or_ed_in_my`: an encounter of class IMP or EMER starting inside the MY

| flag | panel | alive | inside cap | selected |
|---|---|---|---|---|
| hypertension | 145 | 145 | 101 | 32 |
| diabetes | 138 | 138 | 82 | 23 |
| ascvd | 194 | 194 | 142 | 45 |
| statin | 190 | 190 | 139 | 43 |
| mammogram | 12 | 12 | 10 | 3 |
| colonoscopy | 103 | 103 | 86 | 31 |
| fobt_fit | 23 | 23 | 16 | 6 |
| eye_exam | 74 | 74 | 46 | 13 |
| hospice | 87 | 87 | 53 | 13 |
| dementia | 34 | 34 | 21 | 6 |
| esrd_dialysis | 20 | 20 | 1 | 0 |
| statin_stopped | 0 | 0 | 0 | 0 |
| bp_panel_in_my | 234 | 234 | 178 | 59 |
| tobacco_screen_in_my | 234 | 234 | 178 | 59 |
| prapare_in_my | 234 | 234 | 178 | 59 |
| encounter_in_my | 237 | 237 | 181 | 60 |
| inpatient_or_ed_in_my | 59 | 59 | 33 | 9 |

## Strata (draw order = priority)

| stratum | target | minimum | inside cap | over cap | drawn | selected | shortfall | membership |
|---|---|---|---|---|---|---|---|---|
| escalation_carrier | 6 | 6 | 59 | 35 | 6 | 14 |  | alive; hospice OR dementia OR stopped/cancelled statin OR hypertension with no BP panel in the MY (E1/E3/E4/E5-style review carriers) |
| edge | 4 | 2 | 35 | 15 | 3 | 11 |  | deceased on/before as_of OR age at Dec 31 in 17, 18, 20, 21, 39, 40, 49, 50, 51, 52, 65, 66, 74, 75, 76, 85, 86 |
| ascvd_65_75 | 8 | 8 | 40 | 13 | 5 | 16 |  | alive; age 65-75; ASCVD keyword (SPC-eligible-ish) |
| diabetes_65_75 | 8 | 8 | 31 | 16 | 4 | 14 |  | alive; age 65-75; diabetes keyword, prediabetes ignored (EED/SPD-eligible-ish) |
| women_65_74 | 8 | 8 | 35 | 5 | 0 | 18 |  | alive; female; age 65-74 (BCS-eligible-ish) |
| hypertension | 8 | 8 | 101 | 44 | 0 | 32 |  | alive; hypertension keyword, any age (CBP-eligible-ish) |
| no_gap_probe_65_75 | 12 | 8 | 8 | 0 | 8 | 8 |  | alive; age 65-75; no diabetes / ASCVD / hypertension keyword (no-gap probes) |
| filler |  |  |  |  | 34 | 34 |  | seeded draw from the remaining pool to reach n |

## Shortfalls vs SPEC section 6 targets

- no_gap_probe_65_75: 8 selected < target 12 (minimum 8 met; 8 available inside the cap, 0 over the cap)
- no panel patient is deceased on/before as_of: the edge stratum holds boundary ages only
- no panel patient carries a stopped/cancelled statin: escalation carriers are hospice / dementia / hypertension-without-BP-panel only

## Selected patients

| patient_id | drawn under | age | sex | deceased | events | flags |
|---|---|---|---|---|---|---|
| 09832b2e-96ea-78c7-6b8b-0e0bc05d9941 | filler | 77 | female |  | 509 | statin, colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 0cba162f-b2ab-4a67-7c9f-a4fdb8505192 | filler | 85 | female |  | 453 | hypertension, ascvd, statin, colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 15424f61-6bd2-6f7e-01e6-cb6190972e62 | filler | 82 | male |  | 601 | hypertension, diabetes, ascvd, statin, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 26fbf7e3-1281-e585-70b0-ecea6828d8ad | diabetes_65_75 | 68 | female |  | 1177 | hypertension, diabetes, ascvd, statin, colonoscopy, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 2b373d8f-57f3-505b-4e9d-5437697138d0 | filler | 71 | female |  | 485 | hypertension, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 2c35e085-7ebd-b1ec-f88e-055a183bacc5 | filler | 70 | male |  | 1311 | diabetes, ascvd, statin, colonoscopy, fobt_fit, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my, inpatient_or_ed_in_my |
| 327361f1-1a29-5535-21b5-228ba0a450b1 | escalation_carrier | 70 | female |  | 814 | hypertension, ascvd, statin, mammogram, hospice, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 3640879f-85b2-24a2-ff86-03dcd813ea96 | filler | 79 | female |  | 618 | ascvd, statin, colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 37fdbafd-b2eb-143a-7202-488d00e6b756 | filler | 90 | female |  | 777 | hypertension, ascvd, statin, mammogram, hospice, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 387a078e-5782-a47d-a1ed-6925a680612a | filler | 79 | female |  | 779 | hypertension, ascvd, statin, colonoscopy, hospice, dementia, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 3c23abbc-0d9c-1ac2-34a9-b3acb8ce5e19 | ascvd_65_75 | 74 | female |  | 1461 | diabetes, ascvd, statin, colonoscopy, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 41641e8b-fd13-2490-324d-ca84850a3b9c | no_gap_probe_65_75 | 67 | female |  | 321 | colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 442ba058-36df-70c9-5ae9-d675b79fba07 | filler | 89 | male |  | 725 | ascvd, statin, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 454fffbd-b67f-7918-2cb7-aa625f8bf94b | filler | 78 | female |  | 1007 | hypertension, diabetes, ascvd, statin, colonoscopy, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 46175fb2-8cf2-44c5-633e-0bac4a477aa8 | filler | 75 | female |  | 1210 | hypertension, diabetes, ascvd, statin, colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 473e789e-e003-fd0d-91f9-b6822e73d8e5 | filler | 89 | female |  | 606 | ascvd, statin, hospice, dementia, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 4a07de18-2583-5923-8157-611358aa5ec5 | ascvd_65_75 | 69 | female |  | 939 | diabetes, ascvd, statin, colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my, inpatient_or_ed_in_my |
| 4ba0c216-f7a8-4342-a3e1-8346d6cca084 | filler | 82 | male |  | 758 | ascvd, statin, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 5504668d-e9a8-d5ec-8ae5-1a251f950401 | filler | 78 | female |  | 1109 | hypertension, diabetes, ascvd, statin, colonoscopy, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 56633e5f-cac7-bb64-78ed-cec56cc2f3be | escalation_carrier | 72 | female |  | 551 | hypertension, diabetes, ascvd, statin, hospice, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 58ff6708-e968-ee61-82fd-bea26566d352 | filler | 71 | female |  | 440 | hypertension, colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 5fd55c9b-79bf-11f2-2058-889fdb8ebcee | filler | 72 | male |  | 942 | hypertension, diabetes, ascvd, statin, colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 68dd7fce-90de-47d7-df98-c887600eb5aa | filler | 83 | female |  | 1194 | hypertension, diabetes, ascvd, statin, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my, inpatient_or_ed_in_my |
| 6957867a-d1a8-5a15-7948-8ec8c0ecc96c | edge | 86 | male |  | 546 | ascvd, statin, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my, inpatient_or_ed_in_my |
| 6a39c39a-17fb-f102-5689-3684429932a0 | no_gap_probe_65_75 | 67 | male |  | 452 | colonoscopy, fobt_fit, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 72550061-64d4-edf7-2303-0f36da08f1ba | no_gap_probe_65_75 | 65 | female |  | 483 | colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 7801c065-d68b-a92b-8e45-6adf429dbd61 | filler | 92 | male |  | 750 | diabetes, ascvd, statin, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 78d1568b-4890-d6ef-3fed-c521975e4b81 | filler | 79 | male |  | 457 | ascvd, statin, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 7e305567-5f7c-a9a3-82a0-75a5de7e1c69 | filler | 89 | male |  | 428 | hypertension, ascvd, hospice, dementia, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 806b81ef-8ff6-7dda-963f-23bf66f4c687 | filler | 69 | female |  | 2457 | diabetes, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 8356c108-01d1-c739-62a8-bc64c5b29a0a | escalation_carrier | 88 | female |  | 592 | hypertension, ascvd, statin, hospice, dementia, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 8889a2bf-7192-6548-40da-58fa410ab3bf | filler | 90 | female |  | 918 | hypertension, diabetes, ascvd, statin, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 8b6e913f-8e31-9d5a-d95c-ec4f9a97f46c | ascvd_65_75 | 69 | female |  | 645 | ascvd, statin, colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 8fe596bb-b56c-9a06-d442-a0be4a2121f9 | filler | 80 | male |  | 1875 | hypertension, diabetes, ascvd, statin, colonoscopy, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| 91cc7f8a-64f8-6d17-048d-975421dc0bd4 | diabetes_65_75 | 73 | male |  | 988 | hypertension, diabetes, ascvd, statin, colonoscopy, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| a1fa053a-0ec1-711c-f34c-c76d84214660 | filler | 69 | male |  | 814 | diabetes, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| a391ffa4-1191-0632-8d99-d28f28e0640f | filler | 71 | male |  | 591 | hypertension, diabetes, ascvd, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| a4826e1d-ee79-80c2-5fda-fabf87b2f831 | no_gap_probe_65_75 | 69 | female |  | 511 | colonoscopy, fobt_fit, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| a4fb28d8-aa00-4356-212b-6eb861103e56 | diabetes_65_75 | 72 | female |  | 966 | hypertension, diabetes, ascvd, statin, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| aa38914f-54e6-c5e2-f798-d998405f41ff | ascvd_65_75 | 68 | male |  | 790 | hypertension, diabetes, ascvd, statin, colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| b0a54fa5-382d-0f0d-760d-8ad54a114f9e | filler | 91 | female |  | 518 | ascvd, statin, hospice, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| b2b4250d-b051-a2bb-5d73-50d73e4462da | edge | 85 | male |  | 538 | hypertension, ascvd, statin, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my, inpatient_or_ed_in_my |
| bf2939a8-f7de-a22a-70ee-47791c9fbb83 | diabetes_65_75 | 74 | male |  | 1121 | diabetes, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| c1943780-bc56-b4b0-6d54-72be441d4b38 | escalation_carrier | 81 | male |  | 562 | hypertension, ascvd, statin, colonoscopy, hospice, dementia, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| c644e98b-52a7-72da-9d70-a39812aa5d36 | filler | 73 | male |  | 812 | hypertension, ascvd, statin, colonoscopy, fobt_fit, encounter_in_my |
| c6dba733-f0dc-42fd-499c-c1dff2196667 | filler | 87 | female |  | 734 | hypertension, diabetes, ascvd, statin, eye_exam, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| c7d16647-4cbc-62a7-8100-2791c2303360 | no_gap_probe_65_75 | 67 | female |  | 413 | colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| cca167ac-254f-2751-1ce2-d0e54375265b | filler | 86 | female |  | 426 | ascvd, statin, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| d332296c-cc64-4163-1928-21963df2b8d6 | ascvd_65_75 | 75 | female |  | 732 | hypertension, ascvd, statin, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| d4b5ea79-1c04-879f-c0fd-cbcadec804e2 | no_gap_probe_65_75 | 67 | female |  | 419 | colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| d671b0c6-112a-da67-eb69-116323b2b1cc | escalation_carrier | 70 | female |  | 639 | hypertension, ascvd, statin, colonoscopy, hospice, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| de683e42-8865-bcfb-61ee-026c8a0b5497 | filler | 79 | male |  | 651 | ascvd, statin, colonoscopy, fobt_fit, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my, inpatient_or_ed_in_my |
| e059d610-1b64-ed6c-4b25-3358453f8215 | edge | 76 | male |  | 1154 | hypertension, ascvd, statin, colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| e46c69ba-b7d4-b875-c2ac-66af9f7a4f60 | filler | 92 | male |  | 708 | hypertension, diabetes, ascvd, statin, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my, inpatient_or_ed_in_my |
| e48827f5-80d5-c13d-81ae-83d3a83a1c44 | filler | 89 | male |  | 607 | ascvd, statin, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my, inpatient_or_ed_in_my |
| e8646279-833c-4ffe-077d-a509962848c2 | filler | 77 | female |  | 954 | hypertension, ascvd, statin, colonoscopy, fobt_fit, hospice, dementia, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| e9ad08e7-ff69-b9d6-7446-69ad5e662b1a | filler | 87 | female |  | 398 | ascvd, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| ead6778d-812d-4450-de66-ca8fa3bc071c | no_gap_probe_65_75 | 68 | female |  | 632 | mammogram, hospice, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my, inpatient_or_ed_in_my |
| f3c2ee41-c0f8-c199-494d-0be9f21fc994 | no_gap_probe_65_75 | 71 | male |  | 565 | colonoscopy, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |
| f4c8fc6f-853c-203e-3cfc-fb5046664824 | escalation_carrier | 66 | female |  | 523 | hypertension, colonoscopy, hospice, bp_panel_in_my, tobacco_screen_in_my, prapare_in_my, encounter_in_my |

## Excluded over the cap

| patient_id | age | sex | deceased | events |
|---|---|---|---|---|
| 011e8550-b07a-2551-ef2f-5f20c444087e | 65 | male |  | 7154 |
| 03eff367-7785-855a-e825-4833859d53b3 | 85 | male |  | 5789 |
| 03f39a0b-652e-85b8-8307-39cb8557d9cc | 65 | male |  | 2590 |
| 041bfe17-302a-d7dd-3f31-adade5e9a4b1 | 66 | female |  | 6350 |
| 04cbcba8-fb23-e595-12ec-e1ffb0f317fc | 80 | male |  | 4550 |
| 1126bda3-281f-14d6-d8d3-751659d504b4 | 65 | male |  | 11905 |
| 19926368-ebd4-68e0-65f1-af523e6eb460 | 85 | female |  | 5944 |
| 28976489-9965-63b4-4694-b6188a61b76d | 66 | male |  | 19531 |
| 2dc6e41f-98cf-178c-b7fe-8cad69b64c82 | 92 | male |  | 4405 |
| 2e4bce0b-47ef-ab2b-5764-a51f4035b99d | 93 | female |  | 3299 |
| 2f3462c3-d2ec-27d0-11e4-f1e1f5302cc9 | 74 | male |  | 3409 |
| 30e57fd8-2bad-b23b-674a-c2ad60b5d2b9 | 91 | female |  | 6033 |
| 3532aa5f-c4f4-5b21-3833-04308a64ab0c | 91 | male |  | 10572 |
| 37a0559a-9e9c-10a6-235f-e83ca20d0c6e | 77 | male |  | 2704 |
| 3813defe-9d73-2a06-3750-1b036db970f7 | 82 | female |  | 2620 |
| 3daca1cf-62e0-ddc6-36a9-ab106455ee1d | 84 | male |  | 3647 |
| 3e8b28c6-d10a-211e-100e-7a7cda41e044 | 83 | female |  | 8368 |
| 4bdebf9f-4240-eb30-4239-324042ee9604 | 90 | male |  | 5384 |
| 4f0a61e4-0ecb-185f-f880-72c41afb6b03 | 78 | male |  | 10663 |
| 53380749-54e9-c78d-309c-d9d93fa28964 | 84 | male |  | 4874 |
| 5b16ec41-5256-ea5e-387a-fdcea5b77690 | 69 | male |  | 6369 |
| 663e4e97-7110-61c1-88c1-0c5a439bb514 | 66 | female |  | 4997 |
| 66febef3-1c0f-9d1f-73f8-d3e3a09f9caa | 78 | male |  | 10819 |
| 6b787c76-001d-a70b-34eb-3ef1275dd983 | 93 | male |  | 13467 |
| 6c5217ad-5338-3181-897c-713d84ea7aaf | 65 | female |  | 3832 |
| 6e8730c0-6173-f7e2-e4bf-e41f372194e1 | 92 | female |  | 7454 |
| 6fee7ddc-768d-bdb4-0b68-9d470ee76e9d | 83 | female |  | 5777 |
| 700125b2-1fc1-c055-b02d-a4bf3e4cced4 | 91 | male |  | 8302 |
| 725b7690-e7da-0e79-9b0e-e60bacd33e55 | 92 | female |  | 4094 |
| 7627234b-35ea-4a1f-8acb-9778cc869b36 | 90 | female |  | 2554 |
| 78069cc0-956e-013f-bca4-27424321e3d3 | 80 | female |  | 12339 |
| 781a1f37-6c17-7289-b992-57832d7aa6e3 | 79 | male |  | 8862 |
| 7aeb6015-4dc1-11fd-b434-e02ddb86b654 | 73 | male |  | 5159 |
| 7bf4629c-b65f-772b-4a2a-cf3ce4f5919a | 89 | female |  | 9032 |
| 84de476b-8e7b-9839-e738-1b54165e0fe6 | 91 | male |  | 8444 |
| 8e017c4c-84cb-9b18-bb49-ce2f747aeaf8 | 85 | female |  | 9679 |
| 96c046f7-d890-20a2-5929-ebec2254ee95 | 75 | male |  | 16300 |
| 9a5add03-d6a9-3df1-1f07-f81e30120bd0 | 89 | female |  | 20098 |
| 9cf36f95-dd59-ddbf-e0d6-79529d16861d | 90 | male |  | 10397 |
| a1d1489a-3698-3851-372c-06c25b07b9f2 | 75 | male |  | 4642 |
| a424d661-5f7e-fedc-a18d-0fc5298b29a1 | 88 | female |  | 9386 |
| b0b5f662-a5ae-6c21-ebd0-f3a049e3129d | 93 | female |  | 10989 |
| b5e25d46-e0c9-8c50-6ea3-8e60cac56ab5 | 89 | female |  | 9663 |
| c5d3cd65-74c0-a5c9-49b1-ebd4d2febf31 | 92 | female |  | 3013 |
| d0816a9e-5925-0f86-b123-03cff87328ff | 71 | female |  | 5517 |
| d7596f69-1b0c-4e16-e8af-2040a55bb5ae | 81 | female |  | 3225 |
| da05d668-f7eb-750a-d3c3-f37f14b552a1 | 72 | male |  | 10466 |
| dcf38e92-5e1e-6e4c-f41e-997aa8854621 | 71 | female |  | 3016 |
| e49988d2-a8c2-4e8f-a889-397f20f17378 | 94 | female |  | 15165 |
| ecc1f3ad-a0ff-73d2-a39d-918c22ee1f15 | 66 | male |  | 5477 |
| edfcaa4c-da90-0c28-9a41-54348e0d3b3b | 81 | female |  | 4945 |
| ef19d120-4c6b-7f5b-6edd-f546be241623 | 85 | male |  | 11300 |
| f0b1956c-ddac-21a9-1ad5-34d581dc5e78 | 92 | male |  | 3357 |
| f4abe8ba-4c68-72c4-972b-1e6a2e07cb04 | 64 | female |  | 10212 |
| f51eeb62-6f5a-2e10-05a7-da8b6110441c | 92 | female |  | 16355 |
| fc68fc9c-aab7-a833-d702-fb70391eb399 | 87 | male |  | 18864 |
