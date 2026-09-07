# Code audit: events labelers flagged as matching a concept but outside the listed codes

| patient | measure | event (code / display / date) |
|---|---|---|
| 041763e0 | CBP | encounters:424619006:2001-05-02:Prenatal visit (regime/therapy) is a pregnancy-like encounter type not in pregnancy_snomed; dated 24 years before the MY so outs |
| 31e0596e | SPC | conditions:274531002:2000-12-10:Abnormal findings diagnostic imaging heart+coronary circulat — the guide names this finding as ASCVD-adjacent but outside ascvd_ |
| 4250b90f | SPC | conditions:274531002:1988-05-21:Abnormal findings diagnostic imaging heart+coronary circulat - guide names this as possible ASCVD evidence not in ascvd_snomed;  |
| 6e3d889d | SPC | conditions:274531002:1996-09-01:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - guide names this as ASCVD-like evidence not in ascvd_sn |
| 7b0025d7 | SPC | procedures:33367005:2019-03-18:Angiography of coronary artery - ASCVD-type evidence not in ascvd_snomed (guide 5 lists it as not counted) |
| 7b0025d7 | SPC | procedures:415070008:2019-04-03:Percutaneous coronary intervention - ASCVD-type evidence not in ascvd_snomed (guide 5 lists it as not counted) |
| 7b0025d7 | SPC | conditions:274531002:2019-03-18:Abnormal findings diagnostic imaging heart+coronary circulat - finding named by guide 5 as not in ascvd_snomed |
| 7b0025d7 | SPD | procedures:33367005:2019-03-18:Angiography of coronary artery - ASCVD-type evidence not in ascvd_snomed |
| 7b0025d7 | SPD | procedures:415070008:2019-04-03:Percutaneous coronary intervention - ASCVD-type evidence not in ascvd_snomed |
| 7b0025d7 | SPD | conditions:274531002:2019-03-18:Abnormal findings diagnostic imaging heart+coronary circulat - finding named by guide 5 as not in ascvd_snomed |
| 7d33cc51 | SPC | conditions:274531002:2001-06-18:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like finding the guide names as not in ascvd_snom |
| 9f8d5b91 | SPC | conditions:274531002:1988-07-12:Abnormal findings diagnostic imaging heart+coronary circulat (finding) — coronary-imaging finding the guide names as ASCVD-like  |
| a0548efc | SPC | procedures:33367005:2017-06-02:Angiography of coronary artery - ASCVD-related procedure the guide names as not in ascvd_snomed |
| a0548efc | SPC | procedures:415070008:2017-06-17:Percutaneous coronary intervention - ASCVD-related procedure the guide names as not in ascvd_snomed |
| a0548efc | SPC | conditions:274531002:2017-06-02:Abnormal findings diagnostic imaging heart+coronary circulat - finding the guide names as not in ascvd_snomed |
| b7e8cab2 | SPC | conditions:274531002:1993-06-10:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-adjacent finding the guide names as not in ascvd_ |
| be7a5cad | SPC | conditions:274531002:2015-12-26:Abnormal findings diagnostic imaging heart+coronary circulat (finding) - ASCVD-like finding the guide names as not in ascvd_snom |
| d245882f | CBP | procedures:763228001:2023-12-29:Assessment using Canadian Study of Health and Aging Clinical Frailty Scale - a frailty-scale assessment (not a frailty finding); |
| d245882f | SPC | conditions:274531002:1987-03-16:Abnormal findings diagnostic imaging heart+coronary circulat - ASCVD-adjacent finding the guide names as not in ascvd_snomed (pa |
| d245882f | SPC | procedures:33367005:2023-12-29:Angiography of coronary artery - coronary angiography procedure the guide names as not in ascvd_snomed (not counted; label unaffe |

Total flagged: 20. Review at the adjudication step (dev split only).
