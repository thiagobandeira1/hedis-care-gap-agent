# Code audit: events labelers flagged as matching a concept but outside the listed codes

| patient | measure | event (code / display / date) |
|---|---|---|
| 09832b2e | SNS | procedures:710824005:2025-07-19:Assessment of health and social care needs (procedure) - social-needs screening-like procedure not in the SNS list (93025-5 only |
| 09832b2e | SNS | procedures:866148006:2025-07-19:Screening for domestic abuse (procedure) - domain screen the guide marks not representable; does not change the label |
| 0cba162f | SPC | conditions:274531002:2012-03-08:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - guide names this as ASCVD-adjacent but not in ascvd_sno |
| 15424f61 | SPC | procedures:33367005:2016-10-13:Angiography of coronary artery - ASCVD-related procedure the guide names as not in ascvd_snomed |
| 15424f61 | SPC | procedures:415070008:2016-10-15:Percutaneous coronary intervention - ASCVD-related procedure the guide names as not in ascvd_snomed |
| 15424f61 | SPC | conditions:274531002:2016-10-13:Abnormal findings diagnostic imaging heart+coronary circulat - finding the guide names as not in ascvd_snomed |
| 26fbf7e3 | EED | procedures:314971001:2025-11-05:Camera fundoscopy (procedure) is a retinal-imaging exam not in retinal_exam_proc; same day as listed 722161008 so it does not ch |
| 26fbf7e3 | EED | procedures:314971001:2024-12-10:Camera fundoscopy (procedure), retinal-imaging exam not in retinal_exam_proc; label unaffected |
| 26fbf7e3 | SPC | conditions:274531002:2022-03-09:Abnormal findings diagnostic imaging heart+coronary circulat, ASCVD-like finding named by the guide as not in ascvd_snomed; labe |
| 26fbf7e3 | SPC | procedures:33367005:2022-03-09:Angiography of coronary artery, ASCVD-workup procedure not in ascvd_snomed; label unaffected |
| 26fbf7e3 | SPC | procedures:232717009:2022-05-02:Coronary artery bypass grafting (procedure), ASCVD evidence not in ascvd_snomed (the situation code 399261000 is listed and alre |
| 2c35e085 | SPC | conditions:274531002:2016-10-01:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like evidence named by the guide as not in ascvd_ |
| 2c35e085 | SPC | procedures:33367005:2016-10-01:Angiography of coronary artery (procedure) - coronary angiography, ASCVD-like evidence not in ascvd_snomed |
| 2c35e085 | SPC | procedures:232717009:2016-10-05:Coronary artery bypass grafting (procedure) - CABG procedure, ASCVD-like evidence not in ascvd_snomed (the situation code 399261 |
| 327361f1 | SPC | conditions:274531002:2001-08-11:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-adjacent finding the guide names as not in ascvd_ |
| 327361f1 | SPD | conditions:274531002:2001-08-11:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-adjacent finding not in ascvd_snomed; irrelevant  |
| 3640879f | SPC | procedures:33367005:2018-07-27:Angiography of coronary artery - ASCVD-type evidence not in ascvd_snomed (patient already has listed ASCVD codes) |
| 3640879f | SPC | procedures:415070008:2018-07-31:Percutaneous coronary intervention - ASCVD-type evidence not in ascvd_snomed |
| 3640879f | SPC | procedures:33367005:2024-09-02:Angiography of coronary artery - ASCVD-type evidence not in ascvd_snomed |
| 3640879f | SPC | conditions:274531002:2018-07-27:Abnormal findings diagnostic imaging heart+coronary circulat - finding the guide names as not in the ASCVD list |
| 37fdbafd | SPC | conditions:274531002:1997-11-07:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - guide section 5 names this as possible ASCVD evidence n |
| 387a078e | SPC | conditions:274531002:2009-04-01:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - guide names this finding as ASCVD-adjacent but not in a |
| 3c23abbc | SPC | procedures:33367005:2022-05-16:Angiography of coronary artery - ASCVD-related procedure the guide names as not in ascvd_snomed (not counted) |
| 3c23abbc | SPC | procedures:418824004:2022-06-08:Off-pump coronary artery bypass - CABG procedure, ASCVD evidence not in ascvd_snomed (not counted; listed 399261000 already qual |
| 3c23abbc | SPC | conditions:274531002:2022-05-16:Abnormal findings diagnostic imaging heart+coronary circulat - finding the guide names as ASCVD-adjacent but not in ascvd_snomed |
| 3c23abbc | SPD | procedures:33367005:2022-05-16:Angiography of coronary artery - ASCVD-related procedure not in ascvd_snomed (does not change the SPC-denominator decision) |
| 3c23abbc | SPD | procedures:418824004:2022-06-08:Off-pump coronary artery bypass - CABG procedure not in ascvd_snomed (does not change the SPC-denominator decision) |
| 3c23abbc | SPD | conditions:274531002:2022-05-16:Abnormal findings diagnostic imaging heart+coronary circulat - ASCVD-adjacent finding not in ascvd_snomed (does not change the d |
| 442ba058 | SPC | conditions:274531002:2016-11-19:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like finding the guide explicitly names as not in |
| 442ba058 | SPC | procedures:33367005:2016-11-19:Angiography of coronary artery (procedure) - coronary angiography, ASCVD-workup procedure not in ascvd_snomed |
| 442ba058 | SPC | procedures:418824004:2016-11-22:Off-pump coronary artery bypass (procedure) - CABG procedure not in ascvd_snomed (the listed history-of-CABG condition 399261000 |
| 454fffbd | EED | procedures:314971001:2025-06-06:Camera fundoscopy is a retinal-exam-like procedure not in retinal_exam_proc (same day as listed 722161008; no label impact) |
| 454fffbd | EED | procedures:314971001:2024-07-11:Camera fundoscopy is a retinal-exam-like procedure not in retinal_exam_proc (same day as listed 722161008; no label impact) |
| 454fffbd | SPC | procedures:33367005:2018-03-22:Angiography of coronary artery - ASCVD-related evidence not in ascvd_snomed (patient already has listed ASCVD codes) |
| 454fffbd | SPC | procedures:415070008:2018-03-22:Percutaneous coronary intervention - ASCVD-related evidence not in ascvd_snomed |
| 454fffbd | SPC | procedures:33367005:2020-08-23:Angiography of coronary artery - ASCVD-related evidence not in ascvd_snomed |
| 454fffbd | SPC | procedures:415070008:2020-08-27:Percutaneous coronary intervention - ASCVD-related evidence not in ascvd_snomed |
| 454fffbd | SPC | conditions:274531002:2020-08-23:Abnormal findings diagnostic imaging heart+coronary circulat - ASCVD-related finding not in ascvd_snomed |
| 46175fb2 | SPC | procedures:232717009:2024-12-09:Coronary artery bypass grafting (procedure) - ASCVD evidence not in ascvd_snomed (patient already qualifies via listed codes) |
| 46175fb2 | SPC | procedures:33367005:2024-12-08:Angiography of coronary artery (procedure) - ASCVD-related procedure not in ascvd_snomed |
| 46175fb2 | SPC | conditions:274531002:2024-12-08:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - named by guide as not listed ASCVD evidence |
| 46175fb2 | SPD | procedures:232717009:2024-12-09:Coronary artery bypass grafting (procedure) - additional ASCVD evidence outside ascvd_snomed; does not change the label |
| 473e789e | SPC | conditions:274531002:2009-11-19:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like finding named by the guide as not in ascvd_s |
| 4a07de18 | SPC | conditions:274531002:2025-04-04:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like evidence, not in ascvd_snomed (guide says re |
| 4a07de18 | SPC | procedures:33367005:2025-04-04:Angiography of coronary artery (procedure) - ASCVD-like evidence, not in ascvd_snomed |
| 4a07de18 | SPC | procedures:418824004:2025-04-06:Off-pump coronary artery bypass (procedure) - CABG procedure, ASCVD-like evidence, not in ascvd_snomed (listed 399261000 history |
| 4ba0c216 | SPC | procedures:33367005:2018-02-05:Angiography of coronary artery (procedure) - ASCVD-evidence procedure the guide names as not in ascvd_snomed |
| 4ba0c216 | SPC | procedures:415070008:2018-02-20:Percutaneous coronary intervention - ASCVD-evidence procedure the guide names as not in ascvd_snomed |
| 4ba0c216 | SPC | conditions:274531002:2018-02-05:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - finding the guide names as not in ascvd_snomed |
| 5504668d | EED | procedures:314971001:2025-06-19:Camera fundoscopy (procedure) - retinal imaging modality not in retinal_exam_proc list (also 2024-06-24); non-decisive, 72216100 |
| 5504668d | SPC | conditions:274531002:1996-08-15:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like finding the guide names as not in ascvd_snom |
| 5504668d | SPD | conditions:274531002:1996-08-15:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like finding not in ascvd_snomed; non-decisive (l |
| 5504668d | SNS | procedures:710824005:2025-01-11:Assessment of health and social care needs (procedure) - social-need-screening-like procedure not in the SNS code list (also 202 |
| 56633e5f | CBP | encounters:305432006:2013-11-18:IMP encounter 'Admission to surgical transplant department' corroborates the kidney transplant concept (listed 161665007 already |
| 56633e5f | SPC | conditions:274531002:2023-07-26:'Abnormal findings diagnostic imaging heart+coronary circulat' - ASCVD-adjacent finding the guide names as unlisted |
| 56633e5f | SPC | procedures:33367005:2023-07-26:Angiography of coronary artery - ASCVD evidence not in ascvd_snomed |
| 56633e5f | SPC | procedures:415070008:2023-07-31:Percutaneous coronary intervention - ASCVD evidence not in ascvd_snomed |
| 5fd55c9b | SPC | conditions:274531002:2010-08-11:'Abnormal findings diagnostic imaging heart+coronary circulat (finding)' reads as coronary-disease evidence but is not in ascvd_ |
| 68dd7fce | SPC | procedures:33367005:2022-08-02:Angiography of coronary artery (procedure) - ASCVD evidence not in ascvd_snomed |
| 68dd7fce | SPC | procedures:232717009:2022-10-08:Coronary artery bypass grafting (procedure) - CABG procedure, ASCVD evidence not in ascvd_snomed |
| 68dd7fce | SPC | conditions:274531002:2022-08-02:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like finding named by the guide as not in the lis |
| 68dd7fce | SPD | procedures:33367005:2022-08-02:Angiography of coronary artery (procedure) - ASCVD evidence not in ascvd_snomed |
| 68dd7fce | SPD | procedures:232717009:2022-10-08:Coronary artery bypass grafting (procedure) - CABG procedure, ASCVD evidence not in ascvd_snomed |
| 68dd7fce | SPD | conditions:274531002:2022-08-02:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like finding not in ascvd_snomed |
| 6957867a | SPC | procedures:33367005:2018-04-03:Angiography of coronary artery - ASCVD-type evidence not in ascvd_snomed (redundant, listed 414545008 already present) |
| 6957867a | SPC | procedures:415070008:2018-04-09:Percutaneous coronary intervention - ASCVD-type evidence not in ascvd_snomed (redundant) |
| 6957867a | SPC | conditions:274531002:2018-04-03:Abnormal findings diagnostic imaging heart+coronary circulat - guide names this finding as not in the ASCVD list (redundant) |
| 7801c065 | EED | procedures:314971001:2025-02-27:Camera fundoscopy is a retinal-imaging exam not in retinal_exam_proc (a listed 722161008 exists the same day) |
| 7801c065 | EED | procedures:314971001:2024-04-03:Camera fundoscopy is a retinal-imaging exam not in retinal_exam_proc (a listed 722161008 exists the same day) |
| 7801c065 | SPC | conditions:274531002:2004-02-06:Abnormal findings diagnostic imaging heart+coronary circulat is ASCVD-like evidence explicitly outside ascvd_snomed (listed ASCV |
| 7801c065 | SPD | conditions:274531002:2004-02-06:Abnormal findings diagnostic imaging heart+coronary circulat is ASCVD-like evidence explicitly outside ascvd_snomed (listed ASCV |
| 78d1568b | SPC | conditions:274531002:2010-05-20:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - the guide names this as ASCVD-adjacent but unlisted; no |
| 7e305567 | SPC | conditions:274531002:1987-10-19:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like finding the guide names as not in ascvd_snom |
| 8356c108 | SPC | conditions:274531002:2016-06-04:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-suggestive finding the guide names as not in ascv |
| 8889a2bf | SPC | conditions:274531002:1977-12-10:Abnormal findings diagnostic imaging heart+coronary circulat (finding) is ASCVD-like but not in ascvd_snomed; patient already ca |
| 8b6e913f | SPC | conditions:274531002:2021-10-08:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like finding the guide names as not in ascvd_snom |
| 8b6e913f | SPC | procedures:33367005:2021-10-08:Angiography of coronary artery (procedure) - coronary angiography, ASCVD-related procedure not in ascvd_snomed; not counted |
| 8b6e913f | SPC | procedures:415070008:2021-10-14:Percutaneous coronary intervention - PCI, ASCVD-related procedure not in ascvd_snomed; not counted |
| 8fe596bb | EED | procedures:314971001:2025-01-22:Camera fundoscopy - retinal-imaging procedure not in retinal_exam_proc (listed 722161008/700070005 present same day, so no label |
| 8fe596bb | EED | procedures:314971001:2025-06-21:Camera fundoscopy - retinal-imaging procedure not in retinal_exam_proc (listed codes present same day) |
| 8fe596bb | EED | procedures:314971001:2025-10-19:Camera fundoscopy - retinal-imaging procedure not in retinal_exam_proc (listed codes present same day) |
| 8fe596bb | SPC | conditions:274531002:2011-02-04:Abnormal findings diagnostic imaging heart+coronary circulat - ASCVD-like finding named by the guide as not in ascvd_snomed; not |
| 91cc7f8a | EED | procedures:314971001:2025-09-10:Camera fundoscopy (procedure) is a retinal-exam-like procedure not in retinal_exam_proc (same-day 722161008 already closes, no l |
| 91cc7f8a | SPC | conditions:274531002:2007-09-15:Abnormal findings diagnostic imaging heart+coronary circulat (finding), ASCVD-adjacent finding the guide names as not in ascvd_s |
| a391ffa4 | SPC | conditions:274531002:2002-06-08:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like evidence explicitly not in ascvd_snomed; doe |
| a4fb28d8 | EED | procedures:314971001:2025-10-09:Camera fundoscopy (procedure) - retinal imaging matching the retinal-exam concept but not in retinal_exam_proc; label unaffected |
| a4fb28d8 | EED | procedures:314971001:2024-10-14:Camera fundoscopy (procedure) - same concept, prior year; label unaffected |
| a4fb28d8 | SPC | conditions:274531002:2017-06-25:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-type evidence the guide names as outside ascvd_sn |
| a4fb28d8 | SPC | procedures:33367005:2017-06-25:Angiography of coronary artery (procedure) - coronary procedure, ASCVD-type evidence outside listed codes; not counted |
| a4fb28d8 | SPC | procedures:415070008:2017-06-29:Percutaneous coronary intervention - PCI, ASCVD-type evidence outside listed codes; not counted |
| aa38914f | SPC | conditions:274531002:2010-09-02:'Abnormal findings diagnostic imaging heart+coronary circulat (finding)' - the ASCVD-adjacent finding the guide names as not in  |
| b0a54fa5 | SPC | conditions:274531002:1980-12-18:Abnormal findings diagnostic imaging heart+coronary circulat (finding) — guide names this as ASCVD-adjacent evidence not in ascv |
| b2b4250d | SPC | conditions:274531002:2002-05-09:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like evidence explicitly outside ascvd_snomed; pa |
| bf2939a8 | EED | procedures:314971001:2025-05-08:Camera fundoscopy (procedure) is a retinal-exam-like procedure not in retinal_exam_proc; same day as listed 722161008, no label  |
| c1943780 | SPC | conditions:274531002:2005-07-22:Abnormal findings diagnostic imaging heart+coronary circulat - ASCVD-like finding the guide names as not in ascvd_snomed; not co |
| c644e98b | SPC | procedures:33367005:2022-05-22:Angiography of coronary artery (procedure) - guide names coronary angiography as ASCVD evidence not in ascvd_snomed; not counted |
| c644e98b | SPC | conditions:274531002:2022-05-22:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - guide names this finding as ASCVD evidence not in ascvd |
| c644e98b | SPC | procedures:232717009:2022-06-07:Coronary artery bypass grafting (procedure) - CABG procedure is ASCVD evidence not in ascvd_snomed (the history code 399261000 i |
| c6dba733 | EED | procedures:314971001:2025-07-12:Camera fundoscopy — retinal-exam-like procedure not in retinal_exam_proc (accompanies listed 722161008 same day) |
| c6dba733 | EED | procedures:314971001:2024-07-17:Camera fundoscopy — retinal-exam-like procedure not in retinal_exam_proc (accompanies listed 722161008 same day) |
| c6dba733 | SPC | conditions:274531002:2006-12-30:Abnormal findings diagnostic imaging heart+coronary circulat — ASCVD-like finding the guide names as not in ascvd_snomed (not co |
| c6dba733 | SPD | conditions:274531002:2006-12-30:Abnormal findings diagnostic imaging heart+coronary circulat — ASCVD-like finding not in ascvd_snomed (not counted) |
| c7d16647 | CBP | encounters:424619006:1993-09-01:Prenatal visit encounter type suggests a pregnancy concept but is not a listed pregnancy condition code and is dated 1993, far o |
| c7d16647 | SPC | encounters:424619006:1993-09-01:Prenatal visit encounter type suggests a pregnancy concept but is not a listed pregnancy condition code and is dated 1993, outsi |
| cca167ac | SPC | conditions:274531002:2011-12-08:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like finding the guide names as not in ascvd_snom |
| cca167ac | SPC | procedures:415070008:2019-06-04:Percutaneous coronary intervention - ASCVD evidence procedure not in ascvd_snomed; does not change the label |
| cca167ac | SPC | procedures:33367005:2019-06-04:Angiography of coronary artery (procedure) - ASCVD evidence procedure not in ascvd_snomed; does not change the label |
| d332296c | SPC | procedures:415070008:2024-03-08:Percutaneous coronary intervention - ASCVD evidence not in ascvd_snomed (not counted; ASCVD already met by listed codes) |
| d332296c | SPC | procedures:33367005:2024-03-08:Angiography of coronary artery (procedure) - ASCVD evidence not in ascvd_snomed (not counted; ASCVD already met by listed codes) |
| d671b0c6 | SPC | conditions:274531002:2012-12-07:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - the guide names this as ASCVD-adjacent evidence outside |
| de683e42 | SPC | conditions:274531002:2002-04-23:Abnormal findings diagnostic imaging heart+coronary circulat - guide names this as possible ASCVD evidence not in ascvd_snomed;  |
| e059d610 | SPC | conditions:274531002:2018-11-30:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - the ASCVD-like finding the guide names as not in ascvd_ |
| e059d610 | SPC | procedures:33367005:2018-11-30:Angiography of coronary artery (procedure) - coronary angiography, unlisted ASCVD evidence |
| e059d610 | SPC | procedures:418824004:2018-12-08:Off-pump coronary artery bypass (procedure) - CABG procedure, unlisted ASCVD evidence (listed 399261000 history-of-CABG conditio |
| e46c69ba | SPC | conditions:274531002:2005-12-21:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-adjacent finding the guide names as not in ascvd_ |
| e46c69ba | SPD | conditions:274531002:2005-12-21:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-adjacent finding not in ascvd_snomed; not counted |
| e48827f5 | SPC | conditions:274531002:2012-04-05:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - coronary-imaging finding the guide explicitly names as  |
| e48827f5 | SPD | conditions:274531002:2012-04-05:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - coronary-imaging finding the guide names as possible AS |
| e8646279 | SPC | procedures:33367005:2021-12-15:Angiography of coronary artery - ASCVD-related procedure not in ascvd_snomed |
| e8646279 | SPC | procedures:415070008:2021-12-15:Percutaneous coronary intervention - ASCVD-related procedure not in ascvd_snomed |
| e8646279 | SPC | procedures:33367005:2022-05-13:Angiography of coronary artery - ASCVD-related procedure not in ascvd_snomed |
| e8646279 | SPC | conditions:274531002:2022-05-13:Abnormal findings diagnostic imaging heart+coronary circulat - ASCVD-like finding named by guide as not listed |
| e8646279 | SPC | procedures:232717009:2022-05-17:Coronary artery bypass grafting - ASCVD-related procedure not in ascvd_snomed |
| e9ad08e7 | SPC | conditions:274531002:1994-05-25:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like finding the guide names as not in ascvd_snom |

Total flagged: 124. Review at the adjudication step (dev split only).
