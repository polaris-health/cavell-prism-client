# Demo scenarios — ground truth for the unified dataset

This page is the answer key for demonstrating **Prism** (notes and labs → FHIR) and **Atlas** (questions over the record) on `notes.csv` and `notes_lab_results.csv`. Every name, date and value is synthetic. It is generated from the authoring manifests; if you edit the data, regenerate it rather than hand-editing numbers.

- **2,414 documents**, **262 patients**, **1,927 encounters**, **320 practitioners**; **4,984 lab rows** for 242 patients.
- Documents span 2017-08-14 → 2026-06-26. The **data cut-off is 2026-06-30**: judge anything relative ("in the past 12 months", "eligible now") as of that date.
- Hospital stays: **115 inpatient admissions** (21 curated MZG stays from 2022–2025 plus 94 in 2025–2026), **20 day cases** and **25 ED attendances** without admission. The rest are outpatient visits (clinic, GP, imaging, pathology).
- Lab reports are **not** documents: every blood, urine, culture and CSF result is a row in `notes_lab_results.csv` (LOINC-coded where a code applies, units and reference ranges as reported, hospital draws time-stamped with their Belgian UTC offset). Clinicians quote key values in their notes, and those quotes equal the rows.

Clock times in documents are local Belgian time. One stay crosses a daylight-saving change: V-1108 (arrival 28/03/2026 16:10, incision 29/03 11:30) is 19.3 h by the clock and 18.3 h elapsed — both are under the 36 h standard.

## 1. Quality — outcomes across the population

Example questions: *What was our 30-day unplanned readmission rate in 2025?* · *Which hip fracture patients waited more than 36 hours for surgery?* · *Median door-to-needle time for thrombolysed strokes?* · *List every hospital-acquired infection with its day of onset.* · *How many STEMIs missed the 90-minute door-to-balloon target?* · *What share of our diabetic outpatients had an HbA1c above 8 %?*

**Inpatient totals:** 115 stays, 630 bed-days, mean LOS 5.5 days.

### Readmissions

| Stay | Patient | After | Days since discharge | Planned | 30-day unplanned |
|------|---------|-------|---------------------:|:-------:|:----------------:|
| V-1007 | Luc Moens | V-1006 | 12 | yes | no |
| V-1105 | Paul Dierickx | V-1104 | 20 | no | **yes** |
| V-1303 | Gilberte Somers | V-1302 | 18 | no | **yes** |
| V-1408 | Omar Haddad | V-1407 | 14 | no | **yes** |
| V-1504 | Nicole Pauwels | V-1503 | 20 | no | **yes** |
| V-1606 | Fatima Zahra El Idrissi | V-1605 | 5 | no | **yes** |
| V-1608 | Gaston Leclercq | V-1607 | 37 | yes | no |
| V-1703 | Jean-Pierre Hubert | V-1702 | 41 | no | no |
| V-1706 | Rudi Vercammen | V-1705 | 24 | no | **yes** |
| V-014 | Geoffrey Almeida | V-003 | 310 | no | no |
| V-018 | Hugo Vermeulen | V-017 | 42 | no | no |

### In-hospital deaths

| Stay | Patient | Department | Admitted | Died | Principal diagnosis |
|------|---------|------------|----------|------|---------------------|
| V-1103 | Simone Wuyts | Orthopaedics | 2025-06-02 | 2025-06-11 | Displaced intracapsular fracture of the right femoral neck (S72.0) |
| V-1203 | Marcel Verlinden | Intensive Care | 2025-07-09 | 2025-07-11 | Acute anterior ST-elevation myocardial infarction (left main / proximal LAD) with cardiogenic shock (Killip IV) |
| V-1403 | Madeleine Vos | Internal Medicine | 2025-07-15 | 2025-07-20 | Sepsis of uncertain source (urinary tract vs lower respiratory tract), organism unidentified |
| V-1005 | Roger Timmermans | Neurology | 2025-12-03 | 2025-12-06 | Intracerebral haemorrhage, left basal ganglia, warfarin-associated (I61.0) |

### Complications and adverse events

| Stay | Patient | Type | Day | Hospital-acquired | Detail |
|------|---------|------|----:|:-----------------:|--------|
| V-1005 | Roger Timmermans | other | 2 | no | Haematoma expansion 38 -> 52 mL with intraventricular extension, GCS 13 -> 8 (progression of presenting ICH) |
| V-1102 | Albert Desmedt | delirium | 3 | yes | Post-operative delirium 14/03/2025 (post-operative day 2), 4AT 6 |
| V-1102 | Albert Desmedt | CAUTI | 5 | yes | Fever 38.6 °C 16/03/2025, catheter since 11/03 09:30, E. coli >10^5 CFU/mL; IV ceftriaxone, catheter removed day 6 (17/03), oral co-amoxiclav at 48 h review |
| V-1103 | Simone Wuyts | HAP | 6 | yes | Right lower lobe pneumonia 08/06/2025, IV co-amoxiclav from 11:40; stopped at 48 h review on transition to comfort care; cause of death |
| V-1106 | Josee Renard | pressure_injury | 4 | yes | Grade 2 sacral pressure injury found 08/11/2025 07:30, skin intact on admission (Braden 13) |
| V-1108 | Yvonne Cools | inpatient_fall | 3 | yes | Unwitnessed fall from bed 31/03/2026 03:10, no injury; post-fall medical review 03:35 |
| V-1202 | Rita Van Laere | other | 1 | yes | Transient reperfusion bradycardia 42/min treated with atropine during PCI |
| V-1203 | Marcel Verlinden | AKI | 1 | yes | Anuric AKI stage 3 from cardiogenic shock (creatinine 132 -> 286 umol/L); no RRT per treatment limitation |
| V-1203 | Marcel Verlinden | other | 1 | yes | Ischaemic hepatitis (ALT 1840 U/L, INR 2.6) from shock |
| V-1402 | Dirk Lauwers | CLABSI | 8 | yes | 11/04/2025: S. epidermidis (MR) in 2/2 blood cultures (line + peripheral) and catheter tip >15 CFU; right IJ CVC inserted 03/04, removed 11/04; vancomycin 7 days |
| V-1408 | Omar Haddad | CDI | 0 | yes | Healthcare-associated CDI (community onset 02/04/2026, 11 days after discharge from V-1407) after meropenem 17/03-21/03 and PPI; present on admission to V-1408, attributable to V-1407 |
| V-1502 | Guido Martens | SSI | 15 | yes | Superficial incisional SSI found at wound clinic 29/04/2025 (post-discharge); wound swab MSSA; flucloxacillin 1 g qds 7 days; no readmission |
| V-1503 | Nicole Pauwels | other | 23 | no | Posterior dislocation of the THR on 16/07/2025 after discharge -> unplanned readmission V-1504 (20 days after discharge), closed reduction |
| V-1505 | Marc Vandevelde | transfusion | 2 | yes | Hb 7.9 g/dL on POD2 (17/09/2025), symptomatic; 2 units red cells |
| V-1604 | Etienne Dumortier | anastomotic_leak | 5 | yes | Anterior colorectal anastomotic dehiscence (15 mm) with faecal peritonitis and septic shock, CT 11/10/2025 |
| V-1604 | Etienne Dumortier | return_to_theatre | 5 | yes | Unplanned laparotomy and Hartmann's procedure 11/10/2025 |
| V-1606 | Fatima Zahra El Idrissi | other | 0 | no | Post-cholecystectomy cystic duct stump bile leak (complication of V-1605 surgery 02/12/2025), present on readmission |
| V-1609 | Marthe Van den Bossche | delirium | 2 | yes | Hyperactive post-operative delirium 15/04/2026 (4AT 7), resolved by 17/04 |
| V-1704 | Clementine Hoste | delirium | 0 | no | delirium on arrival (4AT 6), resolved day 4 |
| V-1704 | Clementine Hoste | pressure_injury | 0 | no | category 1 sacral, present on admission, did not progress |
| V-1710 | Marie-Louise Dehaene | other | 1 | yes | steroid-induced hyperglycaemia (capillary glucose up to 13 mmol/L) |
| V-1802 | Laura Deconinck | OASI | 1 | yes | Third-degree tear grade 3b at ventouse delivery 17/06, repaired in theatre |
| V-1804 | Elke Roose | PPH | 1 | yes | Uterine atony at CS, quantified blood loss 1600 mL; oxytocin, TXA, carboprost x2 |
| V-1804 | Elke Roose | transfusion | 1 | yes | 2 units red cells (01:50, 02:30) for Hb 8.1 g/dL |
| V-1901 | Freddy Bosmans | transfusion | 0 | no | 2 units red cells (03/02 23:40, 04/02 03:10) for Hb 7.2 g/dL on arrival |
| V-1903 | Germaine Lievens | inpatient_fall | 3 | yes | Unwitnessed fall 03/07/2025 02:40 going to the toilet alone; 3 cm left parietal scalp laceration sutured; CT head no bleed; incident report filed |
| V-1907 | Octavie Stassen | pressure_injury | 6 | yes | Grade 2 left heel, found 14/01/2026 07:30; heels intact on admission |
| V-1910 | Raf Coenen | transfusion | 0 | no | 2 units red cells 22/06 (14:10, 17:40) for Hb 6.9 g/dL |
| V-2002 | Alfons Geerinck | other | 0 | yes | Posterior capsule rupture with vitreous loss at 10:02, anterior vitrectomy and sulcus IOL; post-op IOP 26 mmHg treated with acetazolamide |
| V-2013 | Mohamed Tahiri | other | 0 | yes | Post-operative acute urinary retention (bladder scan 820 mL), catheterised 18:45, unplanned overnight admission; TWOC successful 18/11 |

### Process measures per stay

Time stamps and measures exactly as documented (door-to-CT/needle/groin, door-to-balloon, arrival-to-incision, time to first antibiotic, ED length of stay…).

#### WP01 — 9 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-1001 | Jozef Van Acker | last_known_well 08:15; door_time 09:12; ct_time 09:28; door_to_ct 16 min; needle_time 09:50; door_to_needle 38 min; onset_to_needle 95 min; nihss_admission 8; nihss_discharge 3; swallow_screen 10:40 passed; first_oral_intake 10:55; anticoagulation_start 2025-02-21 (day 5); ldl 3.6 mmol/L; hba1c 5.8 % |
| V-1002 | Marleen Verbruggen | last_known_well 13:20; door_time 14:05; ct_time 14:21; door_to_ct 16 min; needle_time 14:57; door_to_needle 52 min; groin_puncture 15:33; door_to_groin 88 min; reperfusion_time 16:10; tici 2b; nihss_admission 18; nihss_discharge 9; swallow_screen 19:40 failed; first_oral_intake 2025-05-09 12:15 (after SLT); holter 72 h, no AF |
| V-1003 | Ahmed Benali | last_known_well 22:30 on 2025-08-25; symptom_discovery 06:50; door_time 07:45; ct_time 08:02; door_to_ct 17 min; mri_time 08:40 (FLAIR-positive); thrombolysis not given (no DWI-FLAIR mismatch); nihss_admission 6; nihss_discharge 2; first_oral_medication aspirin 300 mg 09:10 in ED; swallow_screen 11:30 passed; ldl 4.1 mmol/L |
| V-1004 | Monique Delvaux | last_known_well 10:30; door_time 11:20; ct_time 11:41; door_to_ct 21 min; needle_time 12:34; door_to_needle 74 min; dtn_delay_reason BP 205/110, two doses IV labetalol (178/98 at 12:25); onset_to_needle 124 min; nihss_admission 7; nihss_discharge 2; swallow_screen 14:20 passed; first_oral_intake 14:40; discharge_letter_finalised 2025-10-30 |
| V-1005 | Roger Timmermans | last_known_well 16:05; door_time 16:50; ct_time 17:08; door_to_ct 18 min; reversal_time 17:40; door_to_reversal 50 min; inr_series 3.4 -> 1.3 -> 1.2; haematoma_volume 38 mL -> 52 mL; gcs 13 -> 8 (day 2); ich_score 2; dnacpr 2025-12-04 15:40; date_of_death 2025-12-06 03:20; swallow_screen 19:15 failed, no oral intake |
| V-1006 | Luc Moens | symptom_onset 12:15 (40 min); door_time 13:40; ct_time 14:02; door_to_ct 22 min; abcd2 5; nihss_admission 0; swallow_screen 15:30 passed; first_oral_intake 15:50; ldl 3.8 mmol/L |
| V-1007 | Luc Moens | index_event_to_surgery 15 days; sign_in 08:12; time_out 08:31; antibiotic cefazolin 2 g 08:25; knife_to_skin 08:40; sign_out 10:05; clamp_time 38 min; ebl 150 mL |
| V-1008 | Nadia Bouzid | last_known_well 07:40; door_time 10:02; ct_time 10:31; door_to_ct 29 min; thrombolysis not given (non-disabling deficit); nihss_admission 3; nihss_discharge 1; swallow_screen 11:15 passed; first_oral_intake 11:40; hba1c 8.4 %; ldl 3.4 mmol/L; dapt 21 days |
| V-1009 | Georgette Lambrechts | last_known_well 07:10; door_time 08:30; ct_time 08:47; door_to_ct 17 min; thrombolysis contraindicated (apixaban 06:00); thrombectomy not offered (pre-stroke mRS 3); nihss_admission 14; nihss_discharge 11; braden 12; swallow_screen 10:15 failed; ng_feeding 2026-05-19 to 2026-05-29; apixaban_restart 2026-05-24 (day 7); dnacpr 2026-05-18 10:00 |

#### WP02 — 9 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-1101 | Maria Vandenberghe | ed_arrival 2025-01-22T19:40; knife_to_skin 2025-01-23T13:10; arrival_to_incision 17.5 h; orthogeriatric_review 2025-01-23T09:15 (13.6 h); 4AT 0; vitamin_D 12 ng/mL; antibiotic_to_incision 20 min; morse 55; braden 15; nrs2002 3; admission_pain_nrs 8 |
| V-1102 | Albert Desmedt | ed_arrival 2025-03-11T08:15; knife_to_skin 2025-03-12T16:40; arrival_to_incision 32.4 h; last_apixaban_to_incision 44.7 h; orthogeriatric_review 2025-03-11T15:30 (7.2 h); 4AT 2 on admission, 6 on 14/03; vitamin_D 18 ng/mL; catheter_days 6; antimicrobial_review 2025-03-18T16:00 (48.8 h) |
| V-1103 | Simone Wuyts | ed_arrival 2025-06-02T23:05; knife_to_skin 2025-06-05T07:30; arrival_to_incision 56.4 h (beyond 48 h; INR reversal then theatre capacity); INR 2.8 (02/06) -> 1.9 (03/06) -> 1.5 (04/06) -> 1.4 (05/06); orthogeriatric_review 2025-06-03T10:30 (11.4 h); 4AT 0; DNACPR 2025-06-09T16:00; time_of_death 2025-06-11T04:50; CRP_series 9 / 58 / 186 / 241 mg/L |
| V-1104 | Paul Dierickx | ed_arrival 2025-08-19T14:30; knife_to_skin 2025-08-20T09:00; arrival_to_incision 18.5 h; orthogeriatric_review none; 4AT none; vitamin_D 22 ng/mL |
| V-1105 | Paul Dierickx | ed_arrival 2025-09-15T11:05; 4AT 7 on admission, 0 on 19/09; first_antibiotic 2025-09-15T12:40; antimicrobial_review 2025-09-17T13:30 (48.8 h) |
| V-1106 | Josee Renard | ed_arrival 2025-11-04T10:20; knife_to_skin 2025-11-05T18:45; arrival_to_incision 32.4 h; orthogeriatric_review 2025-11-05T08:30 (22.2 h); 4AT 0; braden_admission 13; vitamin_D 24 ng/mL |
| V-1107 | Frans Baetens | ed_arrival 2026-01-20T06:50; knife_to_skin 2026-01-21T20:10; arrival_to_incision 37.3 h (beyond 36 h, within 48 h; trauma list overran); orthogeriatric_review 2026-01-20T14:20 (7.5 h); 4AT 0; discharge_letter_finalised 2026-02-06 |
| V-1108 | Yvonne Cools | ed_arrival 2026-03-28T16:10; knife_to_skin 2026-03-29T11:30; arrival_to_incision 19.3 h by wall clock (18.3 h elapsed: DST began 29/03/2026 02:00); orthogeriatric_review 2026-03-29T09:00 (16.8 h wall clock, 15.8 h elapsed); 4AT 3 (dementia, no delirium); morse_admission 65; vitamin_D 15 ng/mL |
| V-1109 | Liliane Stevens | ed_arrival 2026-06-01T12:00; knife_to_skin 2026-06-02T08:15; arrival_to_incision 20.3 h; orthogeriatric_review 2026-06-01T17:30 (5.5 h); 4AT 0; vitamin_D 28 ng/mL |

#### WP03 — 8 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-1201 | Kristof Smets | pathway STEMI; symptom_onset 07:05; first_ecg 07:31 (pre-hospital); cath_lab_activation 07:36; door 07:52; arrival_in_lab 08:32; balloon 08:54; door_to_balloon 62 min; door_to_balloon_le_90 true; LVEF 35 % (2026-04-22); peak_hs_TnT 6850 ng/L; discharge_secondary_prevention aspirin, ticagrelor, atorvastatin 80, bisoprolol, ramipril (+ eplerenone, dapagliflozin); cardiac_rehab_referral yes |
| V-1202 | Rita Van Laere | pathway STEMI; door 22:14; triage 22:20; first_ecg 22:31; door_to_ecg 17 min; cath_lab_activation 22:52; arrival_in_lab 23:40; balloon 00:12 (2025-02-27); door_to_balloon 118 min; door_to_balloon_le_90 false; LVEF 50 % (2025-02-28); peak_hs_TnT 3480 ng/L; cardiac_rehab_referral yes |
| V-1203 | Marcel Verlinden | pathway STEMI; door 03:40; first_ecg 03:44; cath_lab_activation 03:48; intubation 04:00; arrival_in_lab 04:12; balloon 04:35; door_to_balloon 55 min; door_to_balloon_le_90 true; killip IV; admission_lactate 6.4 mmol/L; ICU_admission yes, 05:30 on 2025-07-09; DNACPR 2025-07-09 16:00 (family meeting); comfort_care 2025-07-10; time_of_death 2025-07-11 02:15; in_hospital_death yes |
| V-1204 | Tomasz Kowalczyk | pathway NSTEMI; admission_time 2025-09-30 11:05; angiography_time 2025-10-01 09:30; admission_to_angiography 22.4 h; angiography_within_24h true; GRACE 142; hs_TnT 88 -> 164 ng/L (3 h); antithrombotic triple therapy 1 week (aspirin stop 2025-10-08), then apixaban + clopidogrel to 12 months; cardiac_rehab_referral yes |
| V-1205 | Irene Lejeune | pathway NSTEMI; admission_time 2025-11-17 14:30; angiography not performed — shared decision for conservative management (2025-11-18); angiography_within_24h not applicable (documented conservative strategy); GRACE 158; hs_TnT_peak 112 ng/L; beta_blocker withheld — asthma contraindication documented; cardiac_rehab_referral no — frailty/housebound, declined; home physiotherapy; code_status DNACPR, not for ICU (2025-11-18) |
| V-1206 | Hendrik De Wit | pathway NSTEMI; admission_time 2026-02-03 18:20; angiography_time 2026-02-04 11:15; admission_to_angiography 16.9 h; angiography_within_24h true; GRACE 131; heart_team 2026-02-05 — CABG; clopidogrel held from 2026-02-04 (last dose 08:00); transfer regional cardiac surgery centre 2026-02-07; cardiac_rehab_referral deferred to cardiac surgery centre after CABG |
| V-1207 | Sabine Mortier | pathway chest pain rule-out; door 19:40; hs_TnT_0h 5 ng/L (19:55); hs_TnT_1h 6 ng/L (20:55); HEART 3; CTCA 2026-05-13 09:10, CAD-RADS 0, CACS 0; ACS excluded |
| V-20301-001 | Kristof Smets | NYHA II; BP 118/72 mmHg; LVEF 37 % (2026-06-09); NT-proBNP 1650 ng/L (2026-06-05) |

#### WP04 — 8 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-1301 | Eddy Coppieters | ed_arrival 2025-03-04 10:42; door_to_iv_diuretic 58 min; gdmt_at_discharge BB + ARNI + MRA + SGLT2i (4/4); follow_up_days 7 (HF nurse clinic 19/03/2025); nt_probnp_admission 8400 ng/L; weight_change 88.4 -> 84.6 kg; readmitted_30d no |
| V-1302 | Gilberte Somers | ed_arrival 2025-06-10 14:25; door_to_iv_diuretic 55 min; gdmt_at_discharge BB + ACEi + MRA; no SGLT2i and no reason documented (3/4); follow_up_days 35 (HF clinic in 5 weeks); readmitted_30d yes (V-1303, 18 days) |
| V-1303 | Gilberte Somers | ed_arrival 2025-07-04 08:55; door_to_iv_diuretic 55 min; gdmt_at_discharge BB + ACEi + MRA + SGLT2i (4/4); follow_up_days 11 (HF clinic 22/07/2025) |
| V-1304 | Arlette Bogaert | ed_arrival 2025-09-08 19:20; gdmt_at_discharge HFpEF: SGLT2i + loop diuretic; follow_up_days 10 (HF nurse clinic 24/09/2025); code_status DNACPR, ward-based ceiling incl. NIV |
| V-1305 | Willy Van Hoof | ed_arrival 2025-11-25 16:05; gdmt_at_discharge BB + ACEi (low dose) + SGLT2i; MRA withheld with documented reason (eGFR < 30, K); follow_up_days 12 (HF nurse clinic 16/12/2025); nephrology_consult 27/11/2025 |
| V-1306 | Paula Geens | ed_arrival 2026-01-14 11:15; door_to_iv_diuretic 50 min; gdmt_at_discharge BB (1.25 mg) + ARB + MRA + SGLT2i; ARNI stopped for documented hypotension; follow_up_days 10 (HF nurse clinic 03/02/2026); sbp_range 86-94 mmHg; code_status DNACPR, not for ICD, ward-based ceiling (17/01/2026) |
| V-1307 | Bart Cuypers | ed_arrival 2025-10-02 20:40; gdmt_at_discharge BB + ARNI + MRA + SGLT2i (4/4); follow_up_days 11 (HF clinic 20/10/2025) |
| V-1308 | Jeannine Raes | ed_arrival 2026-02-24 09:30; door_to_iv_diuretic 50 min; gdmt_at_discharge BB + ARNI + MRA + SGLT2i (4/4); follow_up_days 10 (HF nurse clinic 12/03/2026) |

#### WP05 — 9 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-1401 | Harold Cunningham | triage_time 10:05; sepsis_recognition_time 10:15; lactate_time 10:20; blood_cultures_time 10:35; first_antibiotic_time 10:50; first_antibiotic ceftriaxone 2 g IV; triage_to_antibiotic_min 45; blood_cultures_before_antibiotic yes; antibiotic_within_60_min yes; news2 9; qsofa 2; lactate 2.8 mmol/L; antimicrobial_review 2025-01-30 (48 h), IV to oral ciprofloxacin 31/01 |
| V-1402 | Dirk Lauwers | triage_time 15:20; sepsis_recognition_time 15:20; lactate_time 15:30; blood_cultures_time 15:40; first_antibiotic_time 15:58; first_antibiotic ceftriaxone 2 g + clarithromycin 500 mg IV; triage_to_antibiotic_min 38; blood_cultures_before_antibiotic yes; antibiotic_within_60_min yes; news2 16; qsofa 3; lactate 5.2 mmol/L; icu_admission 2025-04-03 17:30; icu_discharge 2025-04-13; icu_days 10; ventilation 2025-04-04 02:10 to 2025-04-08 11:00 (4 days); vasopressor noradrenaline 2025-04-03 18:20 to 2025-04-07; central_line_days 8 (03/04-11/04); code_status full escalation (2025-04-03 18:40, ICU); antimicrobial_review 2025-04-05 (48 h), clarithromycin stopped |
| V-1403 | Madeleine Vos | triage_time 14:20; sepsis_recognition_time 14:20; lactate_time 14:45; blood_cultures_time 15:10; first_antibiotic_time 17:30; first_antibiotic ceftriaxone 2 g IV; triage_to_antibiotic_min 190; blood_cultures_before_antibiotic yes; antibiotic_within_60_min no; news2 13; qsofa 3; lactate 4.1 mmol/L; date_of_death 2025-07-20 04:15; code_status DNACPR and ward-based ceiling of care, 2025-07-15 18:45 (day 1); antimicrobial_review 2025-07-17 (48 h), continue ceftriaxone; comfort_care_from 2025-07-18 (palliative care); admitted_from nursing home |
| V-1404 | Kevin De Backer | triage_time 20:40; sepsis_recognition_time 20:50; lactate_time 21:00; blood_cultures_time 21:05; first_antibiotic_time 21:50; first_antibiotic flucloxacillin 2 g IV; triage_to_antibiotic_min 70; blood_cultures_before_antibiotic yes; antibiotic_within_60_min no; news2 7; qsofa 1; lactate 3.1 mmol/L; antimicrobial_review 2025-09-25 (59 h), switched to benzylpenicillin; iv_to_oral 2025-09-26 (day 4), oral amoxicillin |
| V-1405 | Chantal Boon | triage_time 09:30; sepsis_recognition_time 09:35; lactate_time 09:40; blood_cultures_time 09:50; first_antibiotic_time 10:15; first_antibiotic piperacillin-tazobactam 4.5 g IV; triage_to_antibiotic_min 45; blood_cultures_before_antibiotic yes; antibiotic_within_60_min yes; news2 7; qsofa 1; lactate 2.4 mmol/L; admission_to_ercp 2025-12-10 (about 25 h after triage); antimicrobial_review 2025-12-11 (48 h), de-escalated to ceftriaxone then oral co-amoxiclav; referral interval laparoscopic cholecystectomy (DOC-232) |
| V-1406 | Leen Verhaegen | triage_time 18:02; sepsis_recognition_time 18:02; lactate_time 18:15; blood_cultures_time 18:20; first_antibiotic_time 18:47; first_antibiotic piperacillin-tazobactam 4.5 g IV; triage_to_antibiotic_min 45; blood_cultures_before_antibiotic yes; antibiotic_within_60_min yes; news2 7; qsofa 2; lactate 2.3 mmol/L; neutrophils_nadir 0.2 x10^9/L (17/02 and 18/02); chemotherapy EC cycle 2 on 2026-02-06 (day 11); antimicrobial_review 2026-02-19 (48 h), continue then stop on recovery (stopped 21/02) |
| V-1407 | Omar Haddad | triage_time 07:40; sepsis_recognition_time 07:45; lactate_time 07:55; blood_cultures_time 08:05; first_antibiotic_time 08:35; first_antibiotic ceftriaxone 2 g IV; triage_to_antibiotic_min 55; blood_cultures_before_antibiotic yes; antibiotic_within_60_min yes; news2 8; qsofa 2; lactate 3.0 mmol/L; escalation meropenem from 2026-03-17 20:00 (day 2) on ESBL sensitivities; to 21/03; antimicrobial_review 2026-03-18 (48 h); iv_to_oral 2026-03-21, co-trimoxazole to 26/03 |
| V-1408 | Omar Haddad | triage_time 13:12; isolation_time 13:20; stool_sample_time 13:40; cdiff_result_time 17:30; first_oral_vancomycin 18:10; cdi_severity severe (WBC 16.8); news2 2 |
| V-1409 | Hilda Wellens | triage_time 11:15; sepsis_recognition_time 11:20; lactate_time 11:30; blood_cultures_time 12:20; first_antibiotic_time 11:50; first_antibiotic cefazolin 2 g IV; triage_to_antibiotic_min 35; blood_cultures_before_antibiotic no; antibiotic_within_60_min yes; news2 7; qsofa 1; lactate 2.6 mmol/L; joint_aspiration_time 11:40 (before antibiotic); surgery 2026-06-08, knife to skin 19:58 (8 h 43 min after triage); surgical_antibiotic cefazolin 2 g IV 19:25, 33 min before incision; who_checklist sign-in 19:30, time-out 19:52, sign-out 20:48; antimicrobial_review 2026-06-10 (48 h); discharge_therapy home IV cefazolin via PICC (OPAT) |

#### WP06 — 9 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-1501 | Margaret O'Brien | cefazolin_time 08:05; incision_time 08:30; antibiotic_to_incision 25 min; ebl 250 mL; hb_preop 13.4 g/dL; hb_pod1 11.6 g/dL; transfusion none |
| V-1502 | Guido Martens | cefazolin_time 08:25; incision_time 08:45; antibiotic_to_incision 20 min; ebl 300 mL; hb_preop 14.6 g/dL; hb_pod1 12.4 g/dL; ssi_detected 2025-04-29 (post-operative day 15, outpatient V-20601-001) |
| V-1503 | Nicole Pauwels | cefazolin_time 08:12; incision_time 08:38; antibiotic_to_incision 26 min; ebl 350 mL; hb_preop 13.1 g/dL; hb_pod1 10.9 g/dL |
| V-1504 | Nicole Pauwels | ed_arrival 13:52; xray_time 14:25; time_out 18:14; arrival_to_reduction 4 h 22 min; brace hip abduction brace 6 weeks |
| V-1505 | Marc Vandevelde | cefazolin_time 07:40; incision_time 08:55; antibiotic_to_incision 75 min; ebl 600 mL; hb_preop 12.9 g/dL; hb_pod1 9.1 g/dL; hb_pod2 7.9 g/dL; hb_post_transfusion 9.6 g/dL; units_transfused 2 |
| V-1506 | Ingrid Hellemans | cefazolin_time 08:10; incision_time 08:26; antibiotic_to_incision 16 min; ebl 300 mL; hb_preop 13.6 g/dL; hb_pod1 11.4 g/dL |
| V-1507 | Patrick Donckers | cefazolin_time 08:30; incision_time 08:20; antibiotic_to_incision -10 min (after incision); ebl 250 mL; hb_preop 14.9 g/dL; hb_pod1 12.7 g/dL |
| V-1508 | Anne-Sophie Collard | cefazolin_time 08:15; incision_time 08:30; antibiotic_to_incision 15 min; ebl 100 mL; first_mobilisation 13:30 day 0 (5 h after incision); hb_preop 13.8 g/dL; hb_pod1 12.6 g/dL |
| V-1509 | Johan Verbeke | cefazolin_time 08:18; incision_time 08:42; antibiotic_to_incision 24 min; ebl 450 mL; hb_preop 14.2 g/dL; hb_pod1 12.1 g/dL; bmi 38; hba1c 7.6 % (2026-04-20) |

#### WP07 — 10 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-1601 | Samantha Okafor | prophylactic_antibiotic_to_incision 20 min |
| V-1602 | Thibault Marechal | ed_arrival 2025-05-26 21:40; arrival_to_first_antibiotic 55 min; ed_arrival_to_incision 3 h 22 min; antimicrobial_review_hours 59 h; iv_antibiotic_days 5; crp_peak 241 mg/L (2025-05-28) |
| V-1603 | Rosa Ferrante | contrast_in_colon_at_24h yes (radiograph 2025-08-06 09:00); operation_required no |
| V-1604 | Etienne Dumortier | icu_admission 2025-10-11 12:40; icu_discharge 2025-10-14; icu_los_days 3; invasive_ventilation 11/10–12/10 (extubated 12/10 08:15); vasopressor noradrenaline 11/10–12/10; crp_peak 312 mg/L (2025-10-12); antimicrobial_review_hours 50 h; mdt_date 2025-09-04; mdt_to_surgery_days 32; transfusion none |
| V-1605 | Fatima Zahra El Idrissi | arrival_to_first_antibiotic 70 min; antimicrobial_review_hours 48.5 h; admission_to_surgery_hours 23 h 20 min |
| V-1606 | Fatima Zahra El Idrissi | unplanned_readmission_within_30_days yes (5 days) |
| V-1607 | Gaston Leclercq | lipase_peak 1850 U/L (2026-01-26); severity mild (revised Atlanta); modified_glasgow_imrie 1 |
| V-1608 | Gaston Leclercq | prophylactic_antibiotic_to_incision 18 min |
| V-1609 | Marthe Van den Bossche | ed_arrival_to_incision 3 h 28 min; prophylactic_antibiotic_to_incision 16 min |
| V-1610 | Jens Hofmann | planned_day_case yes; unplanned_overnight_stay yes (organisational: list over-ran, op end 18:40); prophylactic_antibiotic_to_incision 18 min |

#### WP08 — 13 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-1701 | Emma Willaert | ed_arrival 2025-01-09 13:05; first_antibiotic 2025-01-09 15:15; arrival_to_antibiotic 2 h 10 min; antibiotic_within_4h yes; curb65 0; spo2_on_air 91 %; blood_cultures_before_antibiotic yes |
| V-1702 | Jean-Pierre Hubert | ed_arrival 2025-02-20 10:30; first_antibiotic 2025-02-20 13:40; arrival_to_antibiotic 3 h 10 min; antibiotic_within_4h yes; curb65 3; code_status DNACPR, not for ICU/intubation (2025-02-20 12:50) |
| V-1703 | Jean-Pierre Hubert | ed_arrival 2025-04-09 18:20; first_antibiotic 2025-04-09 20:05; readmission_within_30_days no; antibiotic_days 2 |
| V-1704 | Clementine Hoste | ed_arrival 2025-06-18 08:15; first_antibiotic 2025-06-18 13:20; arrival_to_antibiotic 5 h 05 min; antibiotic_within_4h no; curb65 3; slt_diet IDDSI level 5 diet, level 2 fluids; code_status DNACPR, not for ICU/NIV (2025-06-18 12:30) |
| V-1705 | Rudi Vercammen | ed_arrival 2025-10-20 08:40; admission_abg pH 7.24, pCO2 9.6 kPa; niv_start 2025-10-20 10:05; niv_stop 2025-10-22 10:00; niv_days 2; icu_admission no; eosinophils_admission 0.36 x10^9/L (2025-10-20 08:55, pre-steroid); fev1_pct_pred 36 % (2025-03-12); fev1_fvc 0.42 |
| V-1706 | Rudi Vercammen | ed_arrival 2025-11-19 14:10; readmission_within_30_days yes; admission_abg pH 7.37, pCO2 6.3 kPa; niv no; eosinophils_admission 0.41 x10^9/L (2025-11-19 14:30, pre-steroid) |
| V-1707 | Sandra Vlaeminck | eosinophils_admission 0.45 x10^9/L (2026-02-02 12:10, pre-steroid); fev1_pct_pred 58 % (2024-11-17); fev1_fvc 0.61 |
| V-1708 | Tom Degroote | ed_arrival 2025-07-28 16:20; ctpa_time 2025-07-28 18:40; anticoagulation_start 2025-07-28 20:15; spesi 0; ddimer 3.8 mg/L FEU; hs_tnt 9 then 10 ng/L |
| V-1709 | Louis Vanneste | ed_arrival 2025-11-10 09:35; drain_inserted 2025-11-10 12:15; drain_removed 2025-11-13 10:00; drain_days 3 |
| V-1710 | Marie-Louise Dehaene | eosinophils_admission 0.12 x10^9/L (2026-01-06 20:05, pre-steroid); fev1_pct_pred 55 % (2025-06-03); fev1_fvc 0.52; antibiotics none |
| V-1711 | Norbert Callewaert | eosinophils_admission 0.62 x10^9/L (2026-03-02 08:20, pre-steroid); feno 48 ppb (2025-11-18); fev1_pct_pred 62 % (2025-11-18); code_status not for CPR or intubation; for NIV (2026-03-02 09:40) |
| V-20804-001 | Rudi Vercammen | fev1_pct_pred_post_bd 37 %; fev1_fvc 0.43; eosinophils 0.38 x10^9/L |
| V-20805-001 | Sandra Vlaeminck | eosinophils 0.42 x10^9/L; triple_therapy_start 2026-04-08 |

#### WP09 — 9 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-1801 | Julie Verschueren | gravida_para G1P0; gestation 40+2; mode_of_birth SVD 04/03/2025 02:14; EBL 350 mL; baby male 3520 g, Apgar 9/10; birth_to_skin_to_skin 6 min (02:20); birth_to_first_breastfeed 41 min (02:55); caesarean no; OASI no; PPH_1000 no |
| V-1802 | Laura Deconinck | gravida_para G1P0; gestation 41+1; mode_of_birth Ventouse 17/06/2025 05:48; EBL 700 mL; baby female 3880 g, Apgar 7/9; birth_to_skin_to_skin 42 min (06:30); birth_to_first_breastfeed 53 min (06:41); antibiotic_to_procedure co-amoxiclav 07:05, start 07:15 (10 min); caesarean no; OASI yes (3b); PPH_1000 no |
| V-1803 | Samira Ouali | gravida_para G2P1; gestation 39+1; mode_of_birth Elective CS 09/09/2025 09:12; EBL 450 mL; baby male 3340 g, Apgar 9/10; antibiotic_to_incision cefazolin 08:50, knife-to-skin 09:02 (12 min); birth_to_skin_to_skin 4 min (09:16, theatre); birth_to_first_breastfeed 46 min (09:58); caesarean yes (elective); OASI no; PPH_1000 no |
| V-1804 | Elke Roose | gravida_para G1P0; gestation 40+5; mode_of_birth Emergency CS cat 2, 16/12/2025 01:05; decision_to_delivery 60 min (00:05 to 01:05); EBL 1600 mL; baby female 3610 g, Apgar 8/9; antibiotic_to_incision cefazolin 00:38, knife-to-skin 00:48 (10 min); birth_to_skin_to_skin 125 min with mother (03:10); partner from 01:20; birth_to_first_breastfeed 140 min (03:25); red_cell_units 2; Hb_nadir 8.1 g/dL; caesarean yes (emergency); OASI no; PPH_1000 yes |
| V-1805 | Ines Demeulemeester | gravida_para G1P0; gestation 37+0 at admission; mode_of_birth SVD 10/02/2026 14:30; admission_BP 162/108; UPCR 68 mg/mmol; magnesium_sulfate 10/02 08:00 to 11/02 14:30; EBL 400 mL; baby female 2780 g, Apgar 8/9; birth_to_skin_to_skin 3 min (14:33); birth_to_first_breastfeed 40 min (15:10); caesarean no; OASI no; PPH_1000 no |
| V-1806 | Hanna Wauters | gravida_para G2P1; gestation 39+4; mode_of_birth SVD 25/05/2026 07:45; EBL 250 mL; baby male 3450 g, Apgar 9/10; birth_to_first_breastfeed 35 min (08:20); caesarean no; OASI no; PPH_1000 no |
| V-1807 | Noah Vandermeulen | ED_arrival 02/02/2026 10:15; HFNO 02/02 11:30 to 04/02 11:00 (47.5 h, 2 days); NG_feeding 02/02 12:00 to 05/02 09:00; STRONGkids 2 (03/02/2026); weight 6.1 kg admission, 6.15 kg discharge; age 3 months |
| V-1808 | Lina El Khatib | ED_arrival 18/08/2025 14:20; first_antibiotic ceftriaxone 15:35 (75 min after arrival); IV_antibiotic_duration 48 h (18/08 15:35 to 20/08 review); IV_to_oral_switch 20/08/2025 cefixime; antimicrobial_review 20/08/2025 15:10 (48 h); age 8 months |
| V-1809 | Arthur Vincke | ED_arrival 24/11/2025 18:40; admission_pH 7.12; admission_glucose 32.0 mmol/L; admission_ketones 5.8 mmol/L; HbA1c 11.8 %; fluids_start 19:10; insulin_start 20:10; DKA_resolution 25/11/2025 08:00 (~12 h after insulin); SC_insulin_start 25/11/2025 11:30; STRONGkids 2 (24/11/2025); age 11 years |

#### WP10 — 10 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-1901 | Freddy Bosmans | ed_arrival 2025-02-03 21:30; endoscopy_time 2025-02-04 09:15; arrival_to_endoscopy 11 h 45 min; glasgow_blatchford 12; hb_admission 7.2 g/dL; hb_discharge 9.1 g/dL; red_cell_units 2; forrest_class IIa |
| V-1902 | Sarah Moreau | ph_venous_admission 7.18; bicarbonate_admission 9 mmol/L; ketones_admission 5.8 mmol/L; insulin_infusion_start 2025-04-22 18:50; dka_resolved 2025-04-23 04:30; time_to_resolution 9 h 40 min |
| V-1903 | Germaine Lievens | sodium_admission 118 mmol/L; sodium_24h 123 mmol/L; sodium_discharge 133 mmol/L; morse_admission 55; morse_after_fall 80; fall_time 2025-07-03 02:40 |
| V-1904 | James Okafor | creatinine_peak 398 umol/L; creatinine_baseline ~95 umol/L; creatinine_discharge 132 umol/L; potassium_peak 5.9 mmol/L; aki_stage 3 |
| V-1905 | Dominique Hanssens | child_pugh C10; meld 19; paracentesis_volume 7.0 L; albumin_given 60 g; ascitic_neutrophils 90 /uL; saag 18 g/L; weight_admission 88.0 kg; weight_discharge 79.6 kg |
| V-1906 | Joke Verhulst | iv_antibiotic_start 2025-12-15 15:10; iv_antibiotic_stop 2025-12-19 06:00; crp_admission 96 mg/L; crp_discharge 31 mg/L |
| V-1907 | Octavie Stassen | 4AT_admission 8; braden_day6 13; morse_admission 75; iv_antibiotic_doses 3 (ceftriaxone 08/01-10/01) |
| V-1908 | Ivan Petrov | hr_admission 148; cha2ds2_vasc 1; has_bled 1; cardioversion_time 2026-03-04 11:22; shocks 1 x 150 J; lvef_toe 55 % |
| V-1909 | Maryse Delhaye | orthostatic_drop_admission 36 mmHg systolic at 3 min; orthostatic_drop_day1 10 mmHg systolic at 3 min; morse_admission 60 |
| V-1910 | Raf Coenen | hb_admission 6.9 g/dL; hb_discharge 9.2 g/dL; ferritin 4 ug/L; red_cell_units 2 |

#### WP11 — 45 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-004-014 | Gerald M. Thornton | arrival 2025-08-27T09:15; seen_by_clinician 2025-08-27T09:55; departure 2025-08-27T12:50; manchester_triage MTS 3 (yellow); ed_los 3 h 35 min; ed_los_minutes 215 min; four_hour_breach no; arrival_to_clinician 40 min |
| V-024-017 | James Whitfield | arrival 2026-02-28T14:10; seen_by_clinician 2026-02-28T14:50; departure 2026-02-28T17:25; manchester_triage MTS 3 (yellow); ed_los 3 h 15 min; ed_los_minutes 195 min; four_hour_breach no; arrival_to_clinician 40 min |
| V-033-015 | Lucia Fernandez-Garcia | arrival 2026-03-30T19:30; seen_by_clinician 2026-03-30T19:55; departure 2026-03-30T22:40; manchester_triage MTS 3 (yellow); ed_los 3 h 10 min; ed_los_minutes 190 min; four_hour_breach no; arrival_to_clinician 25 min |
| V-044-017 | Ahmed Al-Rashidi | arrival 2026-04-19T06:50; seen_by_clinician 2026-04-19T07:15; departure 2026-04-19T10:35; manchester_triage MTS 2 (orange); ed_los 3 h 45 min; ed_los_minutes 225 min; four_hour_breach no; arrival_to_clinician 25 min |
| V-064-017 | Reginald Thornton | arrival 2026-05-16T07:30; seen_by_clinician 2026-05-16T07:50; departure 2026-05-16T11:05; manchester_triage MTS 2 (orange); ed_los 3 h 35 min; ed_los_minutes 215 min; four_hour_breach no; arrival_to_clinician 20 min |
| V-070-017 | Elena Vasquez | arrival 2026-06-12T13:15; seen_by_clinician 2026-06-12T13:50; departure 2026-06-12T16:45; manchester_triage MTS 3 (yellow); ed_los 3 h 30 min; ed_los_minutes 210 min; four_hour_breach no; arrival_to_clinician 35 min |
| V-076-018 | Darius Washington | arrival 2025-10-18T21:05; seen_by_clinician 2025-10-18T21:30; departure 2025-10-18T23:50; manchester_triage MTS 2 (orange); ed_los 2 h 45 min; ed_los_minutes 165 min; four_hour_breach no; arrival_to_clinician 25 min |
| V-084-016 | William O'Donnell | arrival 2025-12-06T10:20; seen_by_clinician 2025-12-06T11:15; departure 2025-12-06T16:25; manchester_triage MTS 3 (yellow); ed_los 6 h 05 min; ed_los_minutes 365 min; four_hour_breach yes; arrival_to_clinician 55 min |
| V-091-014 | Marcus Jefferson | arrival 2026-01-24T08:40; seen_by_clinician 2026-01-24T09:20; departure 2026-01-24T10:55; manchester_triage MTS 3 (yellow); ed_los 2 h 15 min; ed_los_minutes 135 min; four_hour_breach no; arrival_to_clinician 40 min |
| V-2001 | Josephine Van Roy | admission_time 2025-01-27T07:30; discharge_time 2025-01-27T11:05; anaesthesia topical; who_sign_in 08:40; who_time_out 08:52; who_sign_out 09:18; first_incision 08:55; antibiotic intracameral cefuroxime 09:14; converted_to_overnight no |
| V-2002 | Alfons Geerinck | admission_time 2025-03-10T07:15; discharge_time 2025-03-10T14:30; anaesthesia sub-Tenon block; who_sign_in 09:30; who_time_out 09:41; who_sign_out 10:44; first_incision 09:45; antibiotic intracameral cefuroxime 10:38; converted_to_overnight no |
| V-2003 | Maurice Dujardin | admission_time 2025-05-05T12:30; discharge_time 2025-05-05T15:50; anaesthesia topical; who_sign_in 13:35; who_time_out 13:44; who_sign_out 14:09; first_incision 13:47; antibiotic intracameral cefuroxime 14:05; converted_to_overnight no |
| V-2004 | Rosette Van Wijk | admission_time 2025-09-29T07:45; discharge_time 2025-09-29T11:10; anaesthesia topical; who_sign_in 08:50; who_time_out 09:01; who_sign_out 09:26; first_incision 09:04; antibiotic intracameral cefuroxime 09:22; converted_to_overnight no |
| V-2005 | Emile Carpentier | admission_time 2026-01-19T08:00; discharge_time 2026-01-19T11:35; anaesthesia topical; who_sign_in 09:15; who_time_out 09:25; who_sign_out 09:50; first_incision 09:28; antibiotic intracameral cefuroxime 09:47; converted_to_overnight no |
| V-2006 | Denise Wyns | admission_time 2026-04-20T07:20; discharge_time 2026-04-20T10:50; anaesthesia topical; who_sign_in 08:25; who_time_out None; who_sign_out 09:00; first_incision 08:34; antibiotic intracameral cefuroxime 08:55; converted_to_overnight no |
| V-2007 | Hugo Delanghe | admission_time 2026-06-15T12:15; discharge_time 2026-06-15T15:40; anaesthesia topical; who_sign_in 13:20; who_time_out 13:31; who_sign_out 13:57; first_incision 13:34; antibiotic intracameral cefuroxime 13:53; converted_to_overnight no |
| V-2008 | Filip Vanderlinden | admission_time 2025-02-24T08:15; discharge_time 2025-02-24T11:30; anaesthesia conscious sedation; who_sign_in 09:02; who_time_out 09:08; who_sign_out 09:41; caecal_intubation yes; withdrawal_time 14 min; boston_score 8; adenomas_detected 2; converted_to_overnight no |
| V-2009 | Katrien Nuyts | admission_time 2025-06-16T12:45; discharge_time 2025-06-16T15:15; anaesthesia light sedation; who_sign_in 13:30; who_time_out 13:35; who_sign_out 13:58; caecal_intubation yes; withdrawal_time 9 min; boston_score 9; adenomas_detected 0; converted_to_overnight no |
| V-2010 | Stefaan Meert | admission_time 2025-10-13T08:00; discharge_time 2025-10-13T11:40; anaesthesia conscious sedation; who_sign_in 08:50; who_time_out 08:55; who_sign_out 09:36; caecal_intubation yes; withdrawal_time 15 min; boston_score 7; converted_to_overnight no |
| V-2011 | Veerle Bracke | admission_time 2026-02-02T08:30; discharge_time 2026-02-02T10:45; anaesthesia throat spray + light sedation; who_sign_in 09:15; who_time_out 09:19; who_sign_out 09:34; converted_to_overnight no |
| V-2012 | Guy Lemoine | admission_time 2025-04-07T07:10; discharge_time 2025-04-07T15:40; anaesthesia spinal; who_sign_in 08:15; who_time_out 08:44; who_sign_out 09:52; antibiotic_time 08:32; knife_to_skin 08:50; antibiotic_to_incision 18 min; converted_to_overnight no |
| V-2013 | Mohamed Tahiri | admission_time 2025-11-17T10:30; discharge_time 2025-11-18T11:30; anaesthesia general; who_sign_in 11:50; who_time_out 12:14; who_sign_out 13:34; antibiotic_time 12:05; knife_to_skin 12:21; antibiotic_to_incision 16 min; unplanned_overnight_stay yes; converted_to_overnight yes |
| V-2014 | Els Vandamme-Claus | admission_time 2026-03-16T09:00; discharge_time 2026-03-16T17:10; anaesthesia general; who_sign_in 10:25; who_time_out 10:55; who_sign_out 11:48; antibiotic_time 10:40; knife_to_skin 11:02; antibiotic_to_incision 22 min; converted_to_overnight no |
| V-2015 | Robert Descamps | admission_time 2025-07-14T07:00; discharge_time 2025-07-14T14:20; anaesthesia spinal; who_sign_in 08:10; who_time_out 08:52; who_sign_out 09:40; antibiotic_time 08:40; knife_to_skin 08:58; antibiotic_to_incision 18 min; converted_to_overnight no |
| V-2016 | Ilse Van Bael | admission_time 2025-12-01T13:00; discharge_time 2025-12-01T15:45; anaesthesia local; who_sign_in 14:05; who_time_out 14:30; who_sign_out 14:52; antibiotic_time not indicated; knife_to_skin 14:34; converted_to_overnight no |
| V-2017 | Werner Stijnen | admission_time 2025-08-25T11:30; discharge_time 2025-08-25T19:40; anaesthesia spinal; who_sign_in 12:55; who_time_out 13:25; who_sign_out 14:18; antibiotic_time 13:10; knife_to_skin 13:32; antibiotic_to_incision 22 min; converted_to_overnight no |
| V-2018 | Lucienne Pirard | admission_time 2026-05-11T09:00; discharge_time 2026-05-11T10:40; anaesthesia local (lidocaine gel); who_sign_in 09:40; who_time_out 09:44; who_sign_out 09:55; antibiotic_time not indicated; converted_to_overnight no |
| V-2019 | Tim Verhelst | admission_time 2026-06-08T13:30; discharge_time 2026-06-08T15:50; anaesthesia local; who_sign_in 14:20; who_time_out 14:30; who_sign_out 15:05; antibiotic_time not indicated; knife_to_skin 14:33; converted_to_overnight no |
| V-2020 | Brigitte Mahieu | admission_time 2026-02-23T07:00; discharge_time 2026-02-23T17:30; anaesthesia general; who_sign_in 07:50; who_time_out 08:15; who_sign_out 09:25; antibiotic_time 08:05; knife_to_skin 08:21; antibiotic_to_incision 16 min; same_day_discharge yes; converted_to_overnight no |
| V-21101-001 | Sven Wouman | arrival 2025-01-18T23:40; seen_by_clinician 2025-01-19T00:30; departure 2025-01-19T01:30; manchester_triage MTS 4 (green); ed_los 1 h 50 min; ed_los_minutes 110 min; four_hour_breach no; arrival_to_clinician 50 min |
| V-21102-001 | Elisa Brugmans | arrival 2025-02-08T02:10; seen_by_clinician 2025-02-08T02:40; departure 2025-02-08T08:30; manchester_triage MTS 3 (yellow); ed_los 6 h 20 min; ed_los_minutes 380 min; four_hour_breach yes; arrival_to_clinician 30 min |
| V-21103-001 | Karim Mansouri | arrival 2025-03-14T10:05; seen_by_clinician 2025-03-14T10:25; departure 2025-03-14T14:40; manchester_triage MTS 2 (orange); ed_los 4 h 35 min; ed_los_minutes 275 min; four_hour_breach yes; arrival_to_clinician 20 min |
| V-21104-001 | Anouk Dewever | arrival 2025-04-02T15:30; seen_by_clinician 2025-04-02T16:05; departure 2025-04-02T18:35; manchester_triage MTS 3 (yellow); ed_los 3 h 05 min; ed_los_minutes 185 min; four_hour_breach no; arrival_to_clinician 35 min |
| V-21105-001 | Theo Van Loock | arrival 2025-05-20T19:15; seen_by_clinician 2025-05-20T19:50; departure 2025-05-20T21:30; manchester_triage MTS 4 (green); ed_los 2 h 15 min; ed_los_minutes 135 min; four_hour_breach no; arrival_to_clinician 35 min |
| V-21106-001 | Martine Delforge | arrival 2025-06-11T09:40; seen_by_clinician 2025-06-11T10:45; departure 2025-06-11T14:50; manchester_triage MTS 3 (yellow); ed_los 5 h 10 min; ed_los_minutes 310 min; four_hour_breach yes; arrival_to_clinician 65 min |
| V-21107-001 | Bjorn Segaert | arrival 2025-07-04T13:20; seen_by_clinician 2025-07-04T13:55; departure 2025-07-04T15:00; manchester_triage MTS 4 (green); ed_los 1 h 40 min; ed_los_minutes 100 min; four_hour_breach no; arrival_to_clinician 35 min |
| V-21108-001 | Loes Vandeputte | arrival 2025-08-09T21:00; seen_by_clinician 2025-08-09T21:08; departure 2025-08-10T03:30; manchester_triage MTS 2 (orange); ed_los 6 h 30 min; ed_los_minutes 390 min; four_hour_breach yes; arrival_to_clinician 8 min |
| V-21109-001 | Hamza Kaya | arrival 2025-09-13T01:30; departure 2025-09-13T04:15; manchester_triage MTS 4 (green); ed_los 2 h 45 min; ed_los_minutes 165 min; four_hour_breach no |
| V-21110-001 | Gerda Van Steen | arrival 2025-10-05T17:45; seen_by_clinician 2025-10-05T18:20; departure 2025-10-05T21:15; manchester_triage MTS 3 (yellow); ed_los 3 h 30 min; ed_los_minutes 210 min; four_hour_breach no; arrival_to_clinician 35 min |
| V-21111-001 | Nico Heylen | arrival 2025-11-21T08:10; seen_by_clinician 2025-11-21T08:30; departure 2025-11-21T11:05; manchester_triage MTS 2 (orange); ed_los 2 h 55 min; ed_los_minutes 175 min; four_hour_breach no; arrival_to_clinician 20 min |
| V-21112-001 | Aline Rousseau | arrival 2025-12-27T12:30; seen_by_clinician 2025-12-27T13:10; departure 2025-12-27T16:10; manchester_triage MTS 3 (yellow); ed_los 3 h 40 min; ed_los_minutes 220 min; four_hour_breach no; arrival_to_clinician 40 min |
| V-21113-001 | Jonas Van Dyck | arrival 2026-01-09T07:50; seen_by_clinician 2026-01-09T08:30; departure 2026-01-09T09:20; manchester_triage MTS 4 (green); ed_los 1 h 30 min; ed_los_minutes 90 min; four_hour_breach no; arrival_to_clinician 40 min |
| V-21114-001 | Mathilde Engels | arrival 2026-02-14T20:20; seen_by_clinician 2026-02-14T21:05; departure 2026-02-14T23:10; manchester_triage MTS 3 (yellow); ed_los 2 h 50 min; ed_los_minutes 170 min; four_hour_breach no; arrival_to_clinician 45 min |
| V-21115-001 | Rachid Bensaid | arrival 2026-03-22T16:00; seen_by_clinician 2026-03-22T16:15; departure 2026-03-22T20:20; manchester_triage MTS 2 (orange); ed_los 4 h 20 min; ed_los_minutes 260 min; four_hour_breach yes; arrival_to_clinician 15 min |
| V-345-003 | John Doe | arrival 2025-05-03T11:20; seen_by_clinician 2025-05-03T12:40; departure 2025-05-03T14:15; manchester_triage MTS 4 (green); ed_los 2 h 55 min; ed_los_minutes 175 min; four_hour_breach no; arrival_to_clinician 80 min |

#### WP12 — 11 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-2101 | Werner Goris | ed_arrival 2026-03-10 14:05; triage 14:12; ed_arrival_to_ward 4 h 30 min; pleural_fluid_drained 1500 mL; diagnosis_by_cytology 2026-03-13; ecog_at_discharge 2 |
| V-2102 | Pascal Delmotte | antibiotic_to_incision 18 min (cefazolin 08:18, incision 08:36); operating_time 2 h 15 min; chest_drain_days 3; ebl 150 mL; transfusion none |
| V-21201-005 | Annick Deprez | gp_referral 2026-05-05; first_ct 2026-05-12; histology 2026-06-02; molecular 2026-06-10; referral_to_histology 28 days; referral_to_mdt 44 days; molecular_to_mdt 8 days; first_treatment not started by 2026-06-30 (consent visit 2026-07-02) |
| V-21202-004 | Li Wei Zhang | gp_referral 2026-03-30; first_ct 2026-04-07; histology 2026-04-24; molecular 2026-05-06; referral_to_histology 25 days; referral_to_mdt 45 days; first_treatment stereotactic radiosurgery 2026-05-28 (external centre); mdt_to_first_treatment 14 days |
| V-21203-004 | Georges Lambot | gp_referral 2025-10-20; first_ct 2025-10-28; histology 2025-11-14; molecular 2025-11-21; referral_to_histology 25 days; referral_to_mdt 38 days; first_treatment pembrolizumab 2025-12-03; mdt_to_first_treatment 6 days; referral_to_first_treatment 44 days |
| V-21204-004 | Rik Van Opstal | gp_referral 2025-12-09; first_ct 2025-12-18; histology 2026-01-16; referral_to_histology 38 days; referral_to_mdt 44 days; first_treatment concurrent chemoradiotherapy 2026-02-09; mdt_to_first_treatment 18 days; referral_to_first_treatment 62 days |
| V-21205-004 | Helene Masson | gp_ct 2026-04-28; referral 2026-04-29; histology 2026-05-15; molecular 2026-05-29; referral_to_histology 16 days; referral_to_mdt 36 days; first_treatment not started by 2026-06-30 (cycle 1 booked 2026-07-07) |
| V-21206-002 | Werner Goris | first_ct 2026-02-24; emergency_admission 2026-03-10; cytology 2026-03-13; molecular 2026-03-18; ct_to_cytology 17 days; ct_to_mdt 23 days; first_treatment osimertinib 2026-03-20; mdt_to_first_treatment 1 day |
| V-21207-005 | Kathleen Dewitte | gp_referral 2026-02-10; first_ct 2026-02-19; histology 2026-03-16; molecular 2026-03-27; referral_to_histology 34 days; referral_to_mdt 65 days; first_treatment osimertinib 2026-04-02; treatment_before_mdt 14 days; referral_to_first_treatment 51 days |
| V-21208-001 | Pascal Delmotte | gp_referral 2025-08-19; first_ct 2025-08-28; histology 2025-09-22; referral_to_histology 34 days; referral_to_mdt 37 days; first_treatment VATS right upper lobectomy 2025-10-14; mdt_to_first_treatment 19 days; referral_to_surgery 56 days |
| V-21209-005 | Yasmina Cherif | first_ct 2026-04-17; gp_referral 2026-04-21; histology 2026-05-15; molecular 2026-05-27; referral_to_histology 24 days; referral_to_mdt 44 days; first_treatment not started by 2026-06-30 (osimertinib planned 2026-07-06) |

#### WP13 — 11 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-21301-005 | Lieve Van Gorp | diagnosis MCI due to AD; biomarker amyloid PET positive; informant_history yes; mmse_moca yes; cdr yes; gds yes; reversible_cause_screen B12, folate, TSH, Na, Ca, Hb; mri_brain yes; workup_complete yes |
| V-21302-005 | Jacques Deleu | diagnosis Mild AD dementia; biomarker CSF positive; informant_history yes; mmse_moca yes; cdr yes; gds yes; reversible_cause_screen B12, folate, TSH, Na, Ca, Hb; mri_brain yes; workup_complete yes |
| V-21303-005 | Mireille Gevaert | diagnosis MCI, vascular + depression (not AD); biomarker amyloid PET negative; informant_history yes; mmse_moca yes; cdr yes; gds yes; reversible_cause_screen B12, folate, TSH, Na, Ca, Hb; mri_brain yes; workup_complete yes |
| V-21304-005 | Raymond Ooms | diagnosis Mild AD dementia; biomarker amyloid PET positive; informant_history yes; mmse_moca yes; cdr yes; gds yes; reversible_cause_screen B12, folate, TSH, Na, Ca, Hb; mri_brain yes; workup_complete yes |
| V-21305-005 | Christine Vervoort | diagnosis MCI due to AD; biomarker CSF positive; informant_history yes; mmse_moca yes; cdr yes; gds yes; reversible_cause_screen B12, folate, TSH, Na, Ca, Hb; mri_brain yes; workup_complete yes |
| V-21306-005 | Etienne Paquet | diagnosis MCI due to AD; biomarker amyloid PET positive; informant_history yes; mmse_moca yes; cdr yes; gds yes; reversible_cause_screen B12, folate, TSH, Na, Ca, Hb; mri_brain yes; workup_complete yes |
| V-21307-005 | Josephine Thys | diagnosis Mild AD dementia; biomarker amyloid PET positive; informant_history yes; mmse_moca yes; cdr yes; gds yes; reversible_cause_screen B12, folate, TSH, Na, Ca, Hb; mri_brain yes; workup_complete yes |
| V-21308-005 | Frederik Baert | diagnosis Probable dementia with Lewy bodies; biomarker DaTscan abnormal; informant_history yes; mmse_moca yes; cdr yes; gds yes; reversible_cause_screen B12, folate, TSH, Na, Ca, Hb; mri_brain yes; workup_complete yes |
| V-21309-004 | Nadine Wijns | diagnosis Amnestic MCI, aetiology pending; biomarker amyloid PET booked 2026-07-14; informant_history yes; mmse_moca yes; cdr yes; gds yes; reversible_cause_screen B12, folate, TSH, Na, Ca, Hb; mri_brain yes; workup_complete no (biomarker pending) |
| V-21310-004 | Albert Huysmans | diagnosis MCI due to AD; biomarker CSF positive; informant_history yes; mmse_moca yes; cdr yes; gds yes; reversible_cause_screen B12, folate, TSH, Na, Ca, Hb; mri_brain yes; workup_complete yes |
| V-2201 | Albert Huysmans | ed_arrival 2026-01-19 09:12; last_known_well 2026-01-18 22:30; door_to_ct 19 min; thrombolysis not given (unknown onset > 4.5 h, non-disabling NIHSS 3); thrombectomy not indicated (no LVO); nihss_admission 3; nihss_discharge 1; swallow_screen 09:58, before first oral intake 10:20; ward8_admission 11:05; physiotherapy_assessment 2026-01-20 (< 48 h); slt_assessment 2026-01-20 (< 48 h); statin_at_discharge atorvastatin 80 mg; antiplatelet_at_discharge clopidogrel + aspirin 21 days; af_detected no (72 h monitoring) |

#### WP14 — 18 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-028-015 | Camille Dubois | DAS28-CRP 4.9; SJC28 5; TJC28 6; CRP 1.4 mg/dL |
| V-028-016 | Camille Dubois | DAS28-CRP 5.2; SJC28 6; TJC28 7; CRP 2.0 mg/dL |
| V-043-016 | Grace Ndiaye | DAS28-CRP 4.7; SJC28 5; TJC28 7; CRP 8 mg/L |
| V-043-017 | Grace Ndiaye | DAS28-CRP 4.5; SJC28 5; TJC28 6; CRP 9.5 mg/L |
| V-049-015 | Chen Wei-Lin | DAS28-CRP 4.6; SJC28 5; TJC28 7; CRP 9 mg/L |
| V-049-016 | Chen Wei-Lin | DAS28-CRP 5.0; SJC28 6; TJC28 8; CRP 12 mg/L |
| V-061-012 | Priya Chakraborty | DAS28-CRP 2.3; SJC28 0; TJC28 1; CRP 0.1 mg/dL |
| V-061-013 | Priya Chakraborty | DAS28-CRP 4.1; SJC28 4; TJC28 5; CRP 0.5 mg/dL |
| V-21401-001 | Sofie Van Linden | DAS28-CRP 5.1; SJC28 7; TJC28 9; CRP 10.5 mg/L |
| V-21401-002 | Sofie Van Linden | DAS28-CRP 5.1; SJC28 7; TJC28 9; CRP 11.0 mg/L |
| V-21402-001 | Kaat Mestdagh | DAS28-CRP 4.4; SJC28 5; TJC28 6; CRP 7.0 mg/L |
| V-21402-002 | Kaat Mestdagh | DAS28-CRP 4.4; SJC28 5; TJC28 6; CRP 7.5 mg/L |
| V-21403-001 | Dimitri Popescu | DAS28-CRP 5.3; SJC28 8; TJC28 10; CRP 12.5 mg/L |
| V-21403-002 | Dimitri Popescu | DAS28-CRP 5.3; SJC28 8; TJC28 10; CRP 13.0 mg/L |
| V-21404-001 | Veronique Laurent | DAS28-CRP 4.9; SJC28 6; TJC28 8; CRP 9.0 mg/L |
| V-21404-002 | Veronique Laurent | DAS28-CRP 4.9; SJC28 6; TJC28 8; CRP 9.5 mg/L |
| V-21405-001 | Pieter-Jan Govaerts | DAS28-CRP 3.1; SJC28 3; TJC28 4; CRP 1.5 mg/L |
| V-21405-002 | Pieter-Jan Govaerts | DAS28-CRP 3.0; SJC28 3; TJC28 4; CRP 1.4 mg/L |

#### WP15 — 17 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-012-016 | Thomas Brennan | eGFR 38 (2026-04-01); UACR 486 mg/g (2026-04-01); potassium 5.2 mmol/L (2026-04-01); HbA1c 7.6 %; ACEi/ARB losartan 100 mg since before 2018; SGLT2i empagliflozin 10 mg since 2024-03; MRA none (finerenone stopped 2025-08 for K 5.8) |
| V-017-016 | Florence Nakamura | eGFR 51 (2026-03-10); UACR 118 mg/g (2026-03-10); potassium 4.5; HbA1c 6.8 %; ACEi/ARB lisinopril 40 mg since 2020-02; SGLT2i empagliflozin 10 mg started 2026-03-17; MRA finerenone 10 mg (since 2022-06) |
| V-040-017 | Robert Lindqvist | eGFR 23 (2026-02-16); UACR 212 mg/g (2026-02-16); potassium 4.7 (on patiromer); HbA1c 7.2 %; ACEi/ARB lisinopril 40 mg long-term; SGLT2i dapagliflozin 10 mg since 2020-03; MRA none |
| V-053-015 | George Papadopoulos | eGFR 48 (2026-04-14); UACR 14.8 mg/mmol = 131 mg/g (2026-04-14); potassium 4.6; HbA1c 6.8 %; ACEi/ARB ramipril 5 mg since 2025-07; SGLT2i dapagliflozin 10 mg since 2024-10; MRA none |
| V-067-018 | Raymond Kavanagh | eGFR 52 (2026-05-04); UACR 168 mg/g (2026-05-04); potassium 4.6; HbA1c 6.9 %; ACEi/ARB lisinopril 20 mg since 2025-08; SGLT2i dapagliflozin 10 mg since 2022-07; MRA none |
| V-081-014 | Amara Okafor | eGFR 87 (2026-01-13); UACR 14 mg/g (2026-01-13); potassium 4.2; HbA1c 6.3 %; ACEi/ARB lisinopril 20 mg since 2019-05; SGLT2i empagliflozin 10 mg since 2019-10; MRA none |
| V-085-021 | Gerald Thornton | eGFR 56 (2026-04-06); UACR 146 mg/g (2026-04-06); potassium 4.5; HbA1c 6.6 %; ACEi/ARB lisinopril 20 mg since before 2019; SGLT2i empagliflozin 10 mg started 2026-04-13; MRA none |
| V-089-016 | Robert Castellano | eGFR 57 (2026-02-09); UACR 152 mg/g (2026-02-09); 138 mg/g (2025-11-17); potassium 4.4; HbA1c 7.4 %; ACEi/ARB lisinopril 10 mg since 2017-11; SGLT2i empagliflozin 10 mg since 2020-11; MRA none |
| V-095-021 | Gerald Fitzpatrick | eGFR 53 mL/min/1.73m2 (2026-05-18); UACR 134 mg/g (2025-10-13; not repeated); potassium 4.6 mmol/L; HbA1c 6.2 %; ACEi/ARB lisinopril 10 mg since 2024-01; SGLT2i no; MRA none |
| V-20012-001 | Grace Mbeki | eGFR 70 (2026-02-03); UACR 15.6 mg/mmol = 138 mg/g (2026-02-03); potassium 4.3; HbA1c 7.6 %; ACEi/ARB ramipril 5 mg since 2025-06; SGLT2i no (withheld: recurrent UTI); MRA none |
| V-20013-001 | Thomas Sandberg | eGFR 57 (2026-03-17); UACR 21.6 mg/mmol = 191 mg/g (2026-03-17); potassium 4.6; HbA1c 8.3 %; ACEi/ARB ramipril 10 mg since 2021; SGLT2i dapagliflozin 10 mg since 2025-06; MRA none |
| V-21501-001 | Hassan Ouedraogo | eGFR 39 (2026-01-20); UACR 52 mg/mmol = 460 mg/g (2026-01-20); potassium 4.6; HbA1c 7.9 %; ACEi/ARB losartan 100 mg since 2024-03; SGLT2i empagliflozin 10 mg started 2026-01-27; MRA none |
| V-21501-002 | Hassan Ouedraogo | eGFR 38 (2026-04-07); UACR 45 mg/mmol = 398 mg/g (2026-04-07); potassium 4.5; HbA1c 7.4 %; ACEi/ARB losartan 100 mg since 2024-03; SGLT2i empagliflozin 10 mg since 2026-01-27; MRA none |
| V-21502-001 | Monika Wisniewska | eGFR 43 (2026-01-27); UACR 8.6 mg/mmol = 76 mg/g (2026-01-27); potassium 4.4; HbA1c 7.1 %; ACEi/ARB ramipril 10 mg since 2019; SGLT2i dapagliflozin 10 mg since 2024-05; MRA none |
| V-21502-002 | Monika Wisniewska | eGFR 44 (2026-05-12); UACR 8.0 mg/mmol = 71 mg/g (2026-05-12); potassium 4.4; HbA1c 7.0 %; ACEi/ARB ramipril 10 mg since 2019; SGLT2i dapagliflozin 10 mg since 2024-05; MRA none |
| V-21503-001 | Frank Van der Heyden | eGFR 26 (2026-03-03); UACR 1340 mg/g (2026-03-03); potassium 4.7; HbA1c 8.2 %; ACEi/ARB perindopril 10 mg since 2023; SGLT2i dapagliflozin 10 mg since 2023; MRA none |
| V-21503-002 | Frank Van der Heyden | eGFR 25 (2026-05-26); UACR 1200 mg/g (2026-05-26); potassium 4.8 mmol/L (2026-05-26); HbA1c 7.9 %; ACEi/ARB perindopril 10 mg since 2023; SGLT2i dapagliflozin 10 mg since 2023; MRA none |

#### WP16 — 14 encounters

| Encounter | Patient | Measures |
|-----------|---------|----------|
| V-008-016 | James Kowalski | post_bd_FEV1_pct_pred 62 %; post_bd_FEV1_FVC 0.61; spirometry_date 2026-04-15; exacerbations_12m 1 moderate (2025-12-09); inhaled_therapy LAMA+LABA+ICS since 2020-03; home_oxygen none; blood_eosinophils 0.31 x10^9/L (2026-04-15) |
| V-022-018 | Harold Svensson | post_bd_FEV1_pct_pred 55 %; post_bd_FEV1_FVC 0.60; spirometry_date 2026-05-06; exacerbations_12m 2 moderate (2025-09-22, 2026-02-03); inhaled_therapy LAMA+LABA+ICS since 2019-07; roflumilast since 2023-11; home_oxygen ambulatory O2 with exertion (~2 h/day); blood_eosinophils 0.14 x10^9/L (2026-05-06) |
| V-027-019 | Walter Brinkman | post_bd_FEV1_pct_pred 47 %; post_bd_FEV1_FVC 0.55; spirometry_date 2026-05-13; exacerbations_12m 2 moderate (2025-09-15, 2026-01-28); inhaled_therapy LAMA+LABA since 2020-09; triple since 2024-10; roflumilast since 2021-01; home_oxygen ambulatory O2 with exertion; blood_eosinophils 0.34 x10^9/L (2026-05-13) |
| V-037-020 | Patricia Kowalski | post_bd_FEV1_pct_pred 50 %; post_bd_FEV1_FVC 0.57; spirometry_date 2026-04-08; exacerbations_12m 2 moderate (2025-11-24, 2026-06-10); inhaled_therapy LAMA+LABA+ICS since 2019-08; roflumilast since 2020-07; home_oxygen portable O2 PRN activity (<2 h/day); blood_eosinophils 0.41 x10^9/L (2026-04-08) |
| V-048-018 | Robert J. Harmon | post_bd_FEV1_pct_pred 77 %; post_bd_FEV1_FVC 0.66; spirometry_date 2025-09-16; exacerbations_12m 0; inhaled_therapy SABA PRN only; home_oxygen none; blood_eosinophils 0.21 x10^9/L (2025-09-16) |
| V-087-018 | Douglas Fletcher | post_bd_FEV1_pct_pred 52 %; post_bd_FEV1_FVC 0.59; spirometry_date 2026-04-27; exacerbations_12m 2 moderate (2025-10-14, 2026-01-22); inhaled_therapy LAMA+LABA+ICS since 2020-06; home_oxygen none; blood_eosinophils 0.36 x10^9/L (2026-04-27) |
| V-103-019 | William O'Brien | post_bd_FEV1_pct_pred 46 %; post_bd_FEV1_FVC 0.54; spirometry_date 2026-03-02; exacerbations_12m 2 moderate (2025-10-20, 2026-01-12); inhaled_therapy LAMA+LABA+ICS since 2018-07; roflumilast since 2022-10; home_oxygen ambulatory O2 with exertion (~3 h/day); blood_eosinophils 0.31 x10^9/L (2025-11-17, PCP) |
| V-20010-001 | Walter Brennan | post_bd_FEV1_pct_pred 36 %; post_bd_FEV1_FVC 0.41; spirometry_date 2026-04-22; exacerbations_12m 1 moderate (2025-11-20); inhaled_therapy LAMA+LABA+ICS since 2019-03; home_oxygen LTOT 16 h/day; blood_eosinophils 0.26 x10^9/L (2026-04-22) |
| V-20016-001 | Frank Geerts | post_bd_FEV1_pct_pred 54 %; post_bd_FEV1_FVC 0.58; spirometry_date 2026-05-12; exacerbations_12m 2 moderate (2025-11-04, 2026-02-16); inhaled_therapy LAMA+LABA since 2021; ICS added 2025-06-18 (triple); home_oxygen none; blood_eosinophils 0.38 x10^9/L (2026-05-12) |
| V-20017-001 | Hugo Vermeulen | post_bd_FEV1_pct_pred 44 %; post_bd_FEV1_FVC 0.49; spirometry_date 2026-06-16; exacerbations_12m 1 severe (admission 2025-07-21..2025-07-25); inhaled_therapy LABA+ICS since 2020; LAMA added 2025-09-03 (triple); home_oxygen none; blood_eosinophils 0.34 x10^9/L (2026-06-16) |
| V-21601-001 | Roland Vanhaecke | post_bd_FEV1_pct_pred 48 %; post_bd_FEV1_FVC 0.51; spirometry_date 2026-04-14; exacerbations_12m 2 moderate (2025-09-18, 2026-01-27); inhaled_therapy LAMA+LABA+ICS since 2024-11-12; home_oxygen none; blood_eosinophils 0.42 x10^9/L (2026-04-14) |
| V-21602-001 | Christiane Dumont-Vercruysse | imaging HRCT chest |
| V-21602-002 | Christiane Dumont-Vercruysse | post_bd_FEV1_pct_pred 55 %; post_bd_FEV1_FVC 0.60; spirometry_date 2026-03-10; exacerbations_12m 2 moderate (2025-10-07, 2026-01-13); inhaled_therapy LAMA+LABA+ICS since 2025-06-05; home_oxygen none; blood_eosinophils 0.36 x10^9/L (2026-03-10) |
| V-890-003 | Sarah Johnson | post_bd_FEV1_pct_pred 55 %; post_bd_FEV1_FVC 0.61; spirometry_date 2026-06-08; exacerbations_12m 2 moderate (2025-12-03, 2026-03-17); inhaled_therapy LAMA monotherapy since 2024-02; triple started 2026-06-08; home_oxygen none; blood_eosinophils 0.36 x10^9/L (2026-06-08) |

**Population lab measure:** 101 patients have an HbA1c in the lab file; on the most recent value 4 are above 8 % and 1 above 9 %.

## 2. MZG — automated coding

The 21 curated stays of `hospitalizations.csv` (V-001 … V-019, V-902, V-903 and the outpatient reviews V-101 … V-104) are carried over unchanged — same note, encounter and practitioner ids — so the coding scenarios documented in `hospitalization_extraction_demo.ipynb` hold here too. Their structured lab rows keep their `LAB-` ids from `lab_results.csv`; the prose `LABORATORY RESULTS` documents are replaced by those rows. Every new stay is written to be codable as well (principal and secondary diagnoses, procedures with dates, present-on-admission status of complications) — see the per-stay diagnoses in the manifests' tables below.

## 3. Research — trial recruitment

Six fictional protocols. Screen **as of 2026-06-30**. Each pool mixes clear eligibles, single-criterion failures (often subtle: a unit conversion, a date window, an exon-20 insertion instead of exon 19) and pending cases.

### Protocol criteria

Use exactly these fictional protocols. Put every value a screener needs into
the notes / lab rows (with dates) — never write "eligible for the trial";
clinicians don't know about screening. The verdict goes only in the manifest.

- **T1 HF-OPTIMA (HFrEF).** Incl: age 18–85; chronic HF with LVEF ≤ 40 % on an
  echo within 12 months; NYHA II–IV; NT-proBNP ≥ 600 ng/L (≥ 900 ng/L if in AF)
  within 90 days; beta-blocker + ACEi/ARB/ARNI at stable doses ≥ 4 weeks.
  Excl: eGFR < 30; systolic BP < 95 mmHg; MI, PCI or CABG within 90 days;
  IV therapy for decompensated HF within 30 days; active malignancy.
- **T2 KIDNEY-SHIELD (diabetic kidney disease).** Incl: age 40–80; type 2
  diabetes; eGFR 25–60; UACR 100–3000 mg/g (= 11.3–339 mg/mmol) on the most
  recent sample within 6 months; ACEi or ARB at a stable dose ≥ 4 weeks;
  potassium ≤ 4.8 mmol/L; HbA1c ≤ 10.5 %. Excl: type 1 diabetes; known
  non-diabetic kidney disease; dialysis or kidney transplant; current
  spironolactone, eplerenone or finerenone.
- **T3 EOS-COPD.** Incl: age 40–80; COPD ≥ 1 year; post-bronchodilator
  FEV1/FVC < 0.70 and FEV1 30–70 % predicted; ≥ 2 moderate (systemic steroid
  and/or antibiotic) or ≥ 1 severe (hospitalised) exacerbations in the 12
  months before screening; triple inhaled therapy (LAMA + LABA + ICS) ≥ 3
  months; blood eosinophils ≥ 0.30 ×10^9/L (300/µL) within 3 months. Excl:
  current asthma diagnosis; bronchiectasis, interstitial lung disease or lung
  cancer; long-term oxygen > 12 h/day or home NIV; exacerbation within the 4
  weeks before screening.
- **T4 EGFR-FIRST (NSCLC).** Incl: age ≥ 18; histologically confirmed
  non-squamous NSCLC; stage IIIB–IV not amenable to curative treatment, or
  recurrent after curative treatment; EGFR exon 19 deletion or L858R; ECOG 0–1;
  no prior systemic therapy for advanced disease; eGFR ≥ 45. Excl: untreated or
  symptomatic brain metastases (treated and stable ≥ 2 weeks is allowed);
  history of ILD or pneumonitis; QTc > 470 ms; other active malignancy.
- **T5 CLEAR-AD (early Alzheimer's).** Incl: age 55–85; MCI due to AD or mild
  AD dementia; MMSE 22–30; amyloid positive by PET, or CSF Aβ42/40 < 0.068
  with p-tau181 > 56 ng/L; a study partner seeing the patient ≥ 10 h/week.
  Excl: > 4 microbleeds, superficial siderosis or prior macro-haemorrhage on
  MRI; stroke or TIA within 12 months; therapeutic anticoagulation; other
  primary neurodegenerative diagnosis (DLB, FTD, Parkinson's disease
  dementia); untreated B12 deficiency or hypothyroidism.
- **T6 RA-ADVANCE.** Incl: age 18–75; RA (2010 ACR/EULAR) ≥ 6 months;
  DAS28-CRP ≥ 3.2 with ≥ 4 swollen and ≥ 4 tender joints (28-joint count);
  inadequate response to methotrexate ≥ 12 weeks at a stable ≥ 15 mg/week for
  ≥ 4 weeks, or documented intolerance at the maximum tolerated dose. Excl:
  failure of ≥ 2 biologic or targeted synthetic DMARDs; positive IGRA without
  completed TB prophylaxis; HBsAg or anti-HBc positive, or HCV antibody
  positive; malignancy within 5 years (except treated non-melanoma skin
  cancer); pregnancy or breastfeeding; eGFR < 40.

### T1 HF-OPTIMA (HFrEF) — 5 eligible, 8 ineligible, 0 pending

| Patient | MRN | Verdict | Why |
|---------|-----|---------|-----|
| Beatrice Kamau | MRN-10057 | **eligible** | Age 83, HFrEF, EF 36 % (echo 2026-03-18), NYHA II, AF: NT-proBNP 2400 pg/mL (= ng/L) on 2026-05-13 >= 900, Carvedilol 12.5 mg bd + sacubitril/valsartan 24/26 mg bd unchanged since 2023, eGFR 44 (2026-05-13), SBP 128; no recent MI/PCI; no IV therapy |
| Eddy Coppieters | MRN-20401 | **eligible** | Age 69, Chronic ischaemic HFrEF, LVEF 30 % (echo 2026-04-08), NYHA II (2026-05-20), Sinus rhythm; NT-proBNP 1450 ng/L (2026-05-20) >= 600, Bisoprolol 10 mg + sacubitril/valsartan 97/103 mg bd unchanged since 11/2025, eGFR 58, SBP 112; last MI/PCI 2018; last IV diuretic 03/2025; no malignancy |
| Gilberte Somers | MRN-20402 | **eligible** | Age 76, HFrEF, LVEF 30 % (echo 2026-03-25), NYHA III (2026-04-15), Sinus rhythm; NT-proBNP 3100 ng/L (2026-04-15, 76 days before cut-off), Bisoprolol 5 mg since 07/2025 + sacubitril/valsartan 24/26 mg bd since 14/10/2025, eGFR 41, SBP 104; no MI/PCI/CABG; last IV diuretic 07/2025 |
| Bart Cuypers | MRN-20406 | **eligible** | Age 52, Non-ischaemic DCM, LVEF 33 % (echo 2026-05-27), NYHA II (2026-06-03), Sinus rhythm; NT-proBNP 780 ng/L (2026-06-03) >= 600, Quadruple therapy stable since 11/02/2026 (bisoprolol 10 mg, sacubitril/valsartan 97/103 mg bd), eGFR 88, SBP 118; normal coronaries (no MI/PCI); last IV diuretic 10/2025 |
| Jeannine Raes | MRN-20407 | **eligible** | Age 75, LVEF 40 % (echo 2026-02-26) — meets <= 40 %, NYHA II (2026-06-10), Atrial fibrillation: NT-proBNP 1100 ng/L (2026-06-10) >= 900, Bisoprolol 5 mg + sacubitril/valsartan 49/51 mg bd unchanged since 02/03/2026, eGFR 52, SBP 126; last IV diuretic 27/02/2026 (> 30 days) |
| Winifred Ashby | MRN-10072 | ineligible | Age 87 at 2026-06-30 (> 85), HFpEF (LVEF 55 %), eGFR 18 (2023-01-09) < 30 |
| Dorothy Mae Henderson | MRN-10098 | ineligible | HFpEF: LVEF 52 % (echo 2024-08-12), never <= 40 %, No echo within 12 months and no NT-proBNP within 90 days of 2026-06-30 |
| Geoffrey Almeida | MRN-20003 | ineligible | eGFR 29 (2026-04-20) < 30, (LVEF 30 % echo 2026-02-12, AF, NT-proBNP 2600 ng/L 2026-04-20 would otherwise qualify) |
| Patricia Nowak | MRN-20009 | ineligible | LVEF 48 % after inferior STEMI (echo 2024-11-10) — no heart failure diagnosis and LVEF > 40 % |
| Kristof Smets | MRN-20301 | ineligible | Age 57, LVEF 37 % on echo 2026-06-09 (35 % on 2026-04-22) — meets LVEF <= 40 %, NYHA II (2026-06-09), NT-proBNP 1650 ng/L (2026-06-05), sinus rhythm — meets >= 600, Bisoprolol 2.5 mg + ramipril 2.5 mg unchanged since 2026-04-26 (> 4 weeks), eGFR 79 (2026-06-05); SBP 118 mmHg — no exclusion, EXCLUDED: acute anterior STEMI with primary PCI on 2026-04-21 — 70 days before 2026-06-30 (< 90 days) |
| Arlette Bogaert | MRN-20403 | ineligible | HFpEF: LVEF 58 % (echo 2025-09-09) > 40 %, No NT-proBNP within 90 days of 2026-06-30 (last 2025-09-08) |
| Willy Van Hoof | MRN-20404 | ineligible | eGFR 27 (2026-03-10) < 30 (CKD G4), Also: last NT-proBNP 4200 ng/L on 2026-03-10 is > 90 days before cut-off |
| Paula Geens | MRN-20405 | ineligible | Systolic BP 90 mmHg (2026-05-06) < 95, (Otherwise LVEF 20 %, NYHA III, NT-proBNP 3600 ng/L 2026-05-06, eGFR 44) |

### T2 KIDNEY-SHIELD (diabetic kidney disease) — 6 eligible, 7 ineligible, 1 pending

| Patient | MRN | Verdict | Why |
|---------|-----|---------|-----|
| Raymond Kavanagh | MRN-10067 | **eligible** | Age 70; type 2 diabetes since 2010; diabetic kidney disease, eGFR 52, UACR 168 mg/g (2026-05-04), Lisinopril 20 mg since 2025-08 (> 4 weeks stable); K 4.6; HbA1c 6.9 %, No MRA; no dialysis |
| Gerald Thornton | MRN-10085 | **eligible** | Age 78 (<= 80); type 2 diabetes since 2019-10; diabetic kidney disease, eGFR 56 (2026-04-06), UACR 146 mg/g (2026-04-06), Lisinopril 20 mg stable for years; K 4.5; HbA1c 6.6 %, No MRA, no dialysis/transplant (empagliflozin start is not an exclusion) |
| Robert Castellano | MRN-10089 | **eligible** | Age 64 on 2026-06-30; type 2 diabetes since 2007; diabetic nephropathy, eGFR 57, UACR 152 mg/g (2026-02-09; 138 mg/g 2025-11-17), Lisinopril 10 mg since 2017-11; K 4.4; HbA1c 7.4 %, No MRA; no dialysis |
| Thomas Sandberg | MRN-20013 | **eligible** | Age 71; type 2 diabetes since 2009; diabetic kidney disease, eGFR 57, ACR 21.6 mg/mmol = 191 mg/g (2026-03-17), Ramipril 10 mg since 2021; K 4.6; HbA1c 8.3 % (<= 10.5), No MRA; no dialysis |
| Hassan Ouedraogo | MRN-21501 | **eligible** | Age 66; type 2 diabetes since 2011; diabetic kidney disease (nephrology 2026-04-14), eGFR 38, ACR 45 mg/mmol = 398 mg/g (2026-04-07), Losartan 100 mg since 2024-03; K 4.5; HbA1c 7.4 %, No MRA; no dialysis |
| Frank Van der Heyden | MRN-21503 | **eligible** | Boundary case: eGFR 25 (>= 25), K 4.8 (<= 4.8), UACR 1200 mg/g — all 2026-05-26, Age 63; type 2 diabetes since 2004; diabetic kidney disease, Perindopril 10 mg since 2023; HbA1c 7.9 %; no MRA; not on dialysis |
| Gerald Fitzpatrick | MRN-10095 | pending | Age 78; type 2 diabetes since 2020; CKD 3a attributed to diabetic kidney disease, eGFR 53, K 4.6 mmol/L, HbA1c 6.2 % (2026-05-18), Lisinopril 10 mg stable since 2024-01; no MRA; no dialysis, Most recent UACR 134 mg/g on 2025-10-13 — older than 6 months on 2026-06-30; repeat requested but not collected by the cut-off |
| Thomas Brennan | MRN-10012 | ineligible | Potassium 5.2 mmol/L (2026-04-01) > 4.8 — single failing criterion, Otherwise meets: age 73 (74 on 2026-06-30), T2DM since 2003, DKD, eGFR 38, UACR 486 mg/g, losartan 100 mg long-term, HbA1c 7.6 %, finerenone stopped 2025-08 (no current MRA) |
| Florence Nakamura | MRN-10017 | ineligible | Exclusion: current finerenone 10 mg daily (MRA) — single failing criterion, Otherwise meets: age 67, T2DM since 2012, DKD, eGFR 51, UACR 118 mg/g (2026-03-10), lisinopril 40 mg since 2020-02, K 4.5, HbA1c 6.8 % |
| Robert Lindqvist | MRN-10040 | ineligible | eGFR 23 (2026-02-16) < 25 — single failing criterion, Otherwise meets: age 73, T2DM since 2004, DKD, UACR 212 mg/g, lisinopril 40 mg, K 4.7 on patiromer, HbA1c 7.2 %, no MRA, not on dialysis |
| George Papadopoulos | MRN-10053 | ineligible | Exclusion: known non-diabetic kidney disease (hypertensive nephrosclerosis, predating the 2025-11 diabetes diagnosis) — single failing criterion, Otherwise meets: age 66, T2DM, eGFR 48, ACR 14.8 mg/mmol = 131 mg/g (2026-04-14), ramipril 5 mg since 2025-07, K 4.6, HbA1c 6.8 %, no MRA |
| Amara Okafor | MRN-10081 | ineligible | eGFR 87 (2026-01-13) > 60, UACR 14 mg/g (2026-01-13) < 100 mg/g, No chronic kidney disease (prior microalbuminuria resolved) |
| Grace Mbeki | MRN-20012 | ineligible | eGFR 70 (2026-02-03) > 60 — single failing criterion, Otherwise meets: age 57, T2DM since 2016, DKD, ACR 15.6 mg/mmol = 138 mg/g, ramipril 5 mg since 2025-06, K 4.3, HbA1c 7.6 %, no MRA |
| Monika Wisniewska | MRN-21502 | ineligible | ACR 8.0 mg/mmol = 71 mg/g (2026-05-12) < 100 mg/g (11.3 mg/mmol) — single failing criterion, Otherwise meets: age 72, T2DM since 2006, DKD, eGFR 44, ramipril 10 mg since 2019, K 4.4, HbA1c 7.0 %, no MRA |

### T3 EOS-COPD — 5 eligible, 10 ineligible, 2 pending

| Patient | MRN | Verdict | Why |
|---------|-----|---------|-----|
| Douglas Fletcher | MRN-10087 | **eligible** | Age 70, Post-BD FEV1 52 % pred, FEV1/FVC 0.59 (2026-04-27), 2 moderate exacerbations in 12 months: 2025-10-14 and 2026-01-22, Triple therapy since 2020-06, Eosinophils 0.36 x10^9/L (2026-04-27, within 3 months), No asthma, bronchiectasis, ILD, lung cancer (LDCT 2026-02), home O2 or NIV; last exacerbation > 4 weeks before screening |
| Frank Geerts | MRN-20016 | **eligible** | Age 74; COPD since at least 2021, Post-BD FEV1 54 % pred, FEV1/FVC 0.58 (2026-05-12), 2 moderate exacerbations in 12 months: 2025-11-04 and 2026-02-16 (in-hospital AECOPD 2025-05-09 outside window), Triple therapy since 2025-06-18 (> 3 months), Eosinophils 0.38 x10^9/L (2026-05-12, within 3 months), No asthma, bronchiectasis, ILD, lung cancer, home O2 or NIV; last exacerbation 2026-02-16 (> 4 weeks) |
| Hugo Vermeulen | MRN-20017 | **eligible** | Age 76, Post-BD FEV1 44 % pred, FEV1/FVC 0.49 (2026-06-16), 1 severe (hospitalised) exacerbation within 12 months: 2025-07-21..25 (V-018); the 2025-06-03 admission is outside the window, Triple therapy since 2025-09-03 (LAMA added to LABA/ICS; ~10 months), Eosinophils 0.34 x10^9/L (2026-06-16), No asthma, bronchiectasis, ILD, lung cancer, home O2 or NIV; no exacerbation since 2025-07 |
| Rudi Vercammen | MRN-20804 | **eligible** | Age 71 on 2026-06-30, COPD since 2014, Post-BD FEV1 37 % predicted, FEV1/FVC 0.43 (spirometry 2026-05-12), 2 severe (hospitalised) exacerbations in 12 months: 2025-10-20 and 2025-11-19, Single-inhaler triple therapy (ICS/LAMA/LABA) since March 2024, Blood eosinophils 0.38 x10^9/L on 2026-05-12 (within 3 months), No asthma, no bronchiectasis/ILD/lung cancer (CT 2023), no LTOT, no home NIV, Last exacerbation November 2025 (> 4 weeks) |
| Roland Vanhaecke | MRN-21601 | **eligible** | Age 75 at screening, Post-BD FEV1 48 % pred, FEV1/FVC 0.51 (2026-04-14), 2 moderate exacerbations: 2025-09-18 and 2026-01-27, Triple therapy since 2024-11-12, Eosinophils 0.42 x10^9/L (2026-04-14), No asthma, bronchiectasis, ILD, lung cancer, home O2 or NIV; last exacerbation 2026-01-27 |
| William O'Brien | MRN-10103 | pending | No blood eosinophil count within 3 months of 2026-06-30: last value 0.31 x10^9/L on 2025-11-17; repeat FBC requested but not yet drawn, Other criteria met: age 73, FEV1 46 % pred, FEV1/FVC 0.54 (2026-03-02), 2 moderate exacerbations (2025-10-20, 2026-01-12), triple since 2018-07, ambulatory O2 only ~3 h/day, no NIV |
| Sandra Vlaeminck | MRN-20805 | pending | Age 64 on 2026-06-30, COPD since 2021, Post-BD FEV1 58 % predicted, FEV1/FVC 0.61 (spirometry 2024-11-17), Exacerbations: moderate 2025-09-15 (GP, prednisolone + doxycycline), severe 2026-02-02 (hospitalised), Eosinophils 0.42 x10^9/L on 2026-04-08 (0.45 on 2026-02-02), Triple therapy only since 2026-04-08: 83 days at 2026-06-30, < 3 months (met from 2026-07-08), Current smoker (not an exclusion); no asthma; no LTOT |
| James Kowalski | MRN-10008 | ineligible | Too few exacerbations: only 1 moderate exacerbation in 12 months (2025-12-09), no severe, Otherwise meets: age 56 at screening, FEV1 62 % pred, FEV1/FVC 0.61 (2026-04-15), triple since 2020-03, eosinophils 0.31 (2026-04-15); no asthma diagnosis |
| Harold Svensson | MRN-10022 | ineligible | Blood eosinophils 0.14 x10^9/L (2026-05-06) < 0.30, Otherwise meets: age 80, FEV1 55 % pred, FEV1/FVC 0.60 (2026-05-06), 2 moderate exacerbations (2025-09-22, 2026-02-03), triple since 2019; exertional O2 only ~2 h/day; prostate cancer is not a T3 exclusion |
| Walter Brinkman | MRN-10027 | ineligible | Exclusion: lung cancer — RUL adenocarcinoma in situ, VATS wedge resection 2023-04-12, Otherwise meets: age 80, FEV1 47 % pred, FEV1/FVC 0.55 (2026-05-13), 2 moderate exacerbations (2025-09-15, 2026-01-28), triple since 2024-10, eosinophils 0.34 (2026-05-13) |
| Patricia Kowalski | MRN-10037 | ineligible | Exclusion: exacerbation within 4 weeks before screening — moderate exacerbation 2026-06-10 (20 days before 2026-06-30), Otherwise meets: age 80, FEV1 50 % pred, FEV1/FVC 0.57 (2026-04-08), exacerbations 2025-11-24 and 2026-06-10, triple since 2019-08, eosinophils 0.41 (2026-04-08); PRN O2 < 2 h/day |
| Robert J. Harmon | MRN-10048 | ineligible | Post-BD FEV1 77 % predicted (2025-09-16) > 70 % (GOLD 1), Also: no exacerbations in 12 months; not on triple therapy (SABA only); eosinophils 0.21 (2025-09-16) |
| Walter Brennan | MRN-20010 | ineligible | Exclusion: long-term oxygen therapy 16 h/day (> 12 h/day) (clinic 2026-04-22), Also: only 1 moderate exacerbation in the 12 months (2025-11-20); admission 2024-12-02..07 is outside the window, Also: eosinophils 0.26 < 0.30 (2026-04-22), and > 3 months before 2026-06-30 |
| Marie-Louise Dehaene | MRN-20808 | ineligible | Blood eosinophils 0.12 x10^9/L (2026-01-06), below 0.30; no qualifying eosinophil count within 3 months, Otherwise: FEV1 55 % pred, FEV1/FVC 0.52 (2025-06-03), triple therapy since 2022, 1 severe exacerbation 2026-01-06 |
| Norbert Callewaert | MRN-20809 | ineligible | Current asthma diagnosis (asthma-COPD overlap, childhood asthma, asthma on active problem list, FeNO 48 ppb), Otherwise meets eosinophils 0.62 x10^9/L (2026-03-02) and FEV1 62 % pred |
| Christiane Dumont-Vercruysse | MRN-21602 | ineligible | Exclusion: bronchiectasis on HRCT chest 2026-02-24 (bilateral middle/lower-lobe cylindrical bronchiectasis), Otherwise meets: age 69, FEV1 55 % pred, FEV1/FVC 0.60 (2026-03-10), 2 moderate exacerbations (2025-10-07, 2026-01-13), triple since 2025-06-05, eosinophils 0.36 (2026-03-10) |
| Sarah Johnson | MRN-67890 | ineligible | Not on triple therapy >= 3 months: LAMA monotherapy until triple started 2026-06-08 (22 days before screening), Otherwise meets: age 53, FEV1 55 % pred, FEV1/FVC 0.61, 2 moderate exacerbations (2025-12-03, 2026-03-17), eosinophils 0.36 (2026-06-08) |

### T4 EGFR-FIRST (NSCLC) — 2 eligible, 8 ineligible, 0 pending

| Patient | MRN | Verdict | Why |
|---------|-----|---------|-----|
| Annick Deprez | MRN-21201 | **eligible** | Age 62, Adenocarcinoma (non-squamous), histology 2026-06-02, Stage IVA cT2b N1 M1a (PET-CT 2026-05-21), not amenable to curative treatment (MDT 2026-06-18), EGFR exon 19 deletion p.(E746_A750del) (NGS 2026-06-10), ECOG 1 (2026-06-25), No systemic therapy given as of 2026-06-30 (consent visit booked 2026-07-02), eGFR 84 (2026-06-23), MRI brain 2026-06-09: no metastases, No ILD/pneumonitis; QTc 428 ms; no other malignancy |
| Li Wei Zhang | MRN-21202 | **eligible** | Age 67, Adenocarcinoma, histology 2026-04-24, Stage IVB cT2a N2 M1c (MDT 2026-05-14), EGFR L858R (NGS 2026-05-06), ECOG 0 (2026-06-22), No systemic therapy as of 2026-06-30 (osimertinib planned 2026-07-06), Two asymptomatic brain metastases treated with SRS 2026-05-28, stable on MRI 2026-06-15, no steroids — treated and stable >= 2 weeks, eGFR 76 (2026-06-22), QTc 441 ms, No ILD, no other malignancy |
| Walter Henriksson | MRN-10060 | ineligible | Recurrent adenocarcinoma (liver biopsy 2026-02-04) but EGFR wild-type (KRAS G12D, STK11 loss), Prior systemic therapy for advanced disease: carboplatin/pemetrexed since 2026-03-04 |
| Georges Lambot | MRN-21203 | ineligible | EGFR wild-type (KRAS G12C) on NGS 2025-11-21 — no exon 19 deletion or L858R, Prior systemic therapy for advanced disease: pembrolizumab since 2025-12-03 |
| Rik Van Opstal | MRN-21204 | ineligible | Squamous cell carcinoma (histology 2026-01-16) — not non-squamous, Stage IIIA cT3 N1 M0 treated with curative-intent concurrent chemoradiotherapy from 2026-02-09, No EGFR exon 19 deletion or L858R documented |
| Helene Masson | MRN-21205 | ineligible | EGFR exon 20 insertion p.(A767_V769dup) (NGS 2026-05-29) — not exon 19 deletion or L858R, Otherwise: adenocarcinoma stage IVB, ECOG 1, untreated, eGFR 97, QTc 432 ms, MRI brain clear |
| Werner Goris | MRN-21206 | ineligible | ECOG 2 (discharge 2026-03-15, clinic 2026-03-20), Prior systemic therapy for advanced disease: osimertinib since 2026-03-20 |
| Kathleen Dewitte | MRN-21207 | ineligible | Prior systemic therapy for advanced disease: osimertinib since 2026-04-02 |
| Pascal Delmotte | MRN-21208 | ineligible | Early-stage disease: pT2a pN0 stage IB, curatively resected 2025-10-14, No recurrence (CT 2026-03-02) — not stage IIIB-IV or recurrent, On adjuvant osimertinib since 2025-12-08 |
| Yasmina Cherif | MRN-21209 | ineligible | History of interstitial lung disease: rheumatoid-arthritis-associated fibrotic NSIP (HRCT 2024) on problem list, Otherwise: adenocarcinoma stage IVB, EGFR L858R, ECOG 1, untreated, eGFR 104, QTc 424 ms, MRI brain clear |

### T5 CLEAR-AD (early Alzheimer's) — 2 eligible, 13 ineligible, 1 pending

| Patient | MRN | Verdict | Why |
|---------|-----|---------|-----|
| Lieve Van Gorp | MRN-21301 | **eligible** | Age 71 on 2026-06-30, MCI due to AD (diagnosis letter 2026-04-07), MMSE 26 (2026-04-07), Amyloid PET positive, Centiloid 68 (2026-03-17), Study partner: husband Marc lives with her (daily), MRI 2026-02-10: 1 microbleed, no siderosis, no macro-haemorrhage, No stroke/TIA; no anticoagulant, B12 412 ng/L, TSH 1.8 mIU/L (2025-11-28); no other neurodegenerative diagnosis |
| Josephine Thys | MRN-21307 | **eligible** | Age 82 on 2026-06-30 (within 55–85), Mild AD dementia (letter 2026-04-14), MMSE 24 (2026-04-14), Amyloid PET positive, Centiloid 81 (2026-02-24), Study partner: daughter Ann visits daily 2–3 h (~18 h/week), MRI 2026-01-13: 2 microbleeds, no siderosis, No stroke/TIA, no anticoagulant, B12 244 ng/L, TSH 2.6 mIU/L (2025-11-07) |
| Nadine Wijns | MRN-21309 | pending | Amnestic MCI, MMSE 27 (2026-06-16), MRI unremarkable (2026-04-14), husband lives with her, Amyloid status unknown — amyloid PET booked for 2026-07-14, after the cut-off |
| Eileen Donnelly | MRN-10030 | ineligible | Therapeutic anticoagulation: apixaban for AF (CHA2DS2-VASc 5, continued 2022-09-20), MMSE 21 (2024-03-20) — below 22, No amyloid PET or CSF biomarker |
| Patricia Lawson | MRN-10050 | ineligible | Age 86 on 2026-06-30 (born 1940-06-17) — above 85, Advanced Alzheimer's disease: MMSE 17 (2022-04-25), fully dependent in nursing home (2024-01-10), No amyloid PET or CSF biomarker documented |
| Harold Mitchell | MRN-10062 | ineligible | Other primary neurodegenerative diagnosis: Parkinson's disease dementia (MoCA 19, 2021-06-23), Age 88 on 2026-06-30 (born 1938-06-22) — above 85 |
| William Tan | MRN-10069 | ineligible | Therapeutic anticoagulation: apixaban 5 mg twice daily for AF (continued, 2021-03-15), No amyloid PET or CSF biomarker; last cognitive record 2023-02-06 (MMSE 24, amnestic MCI) |
| Constance Beaumont | MRN-10090 | ineligible | Therapeutic anticoagulation: apixaban 5 mg twice daily for AF (2018–2024 notes), MCI judged multifactorial (vascular + neurodegenerative), no amyloid PET or CSF biomarker |
| Helen Kowalski | MRN-10092 | ineligible | Age 87 on 2026-06-30 (born 1938-12-03) — above 85, No documented cognitive diagnosis: MoCA 20 (2023-11-20) with neurology referral only; no MCI/AD diagnosis, no biomarker |
| Jacques Deleu | MRN-21302 | ineligible | Therapeutic anticoagulation: apixaban 5 mg twice daily for permanent AF (continued long term, letter 2025-12-02) |
| Mireille Gevaert | MRN-21303 | ineligible | Amyloid PET negative, Centiloid 8 (2026-01-20); no CSF biomarkers — not MCI due to AD, Diagnosis: MCI of vascular origin with depression |
| Raymond Ooms | MRN-21304 | ineligible | MMSE 20 (2025-11-18 and 2026-02-03) — below 22 |
| Christine Vervoort | MRN-21305 | ineligible | 6 lobar cerebral microbleeds on MRI 2025-11-04 (> 4) |
| Etienne Paquet | MRN-21306 | ineligible | No study partner: lives alone, only relative a niece in Montréal who phones every 2–3 weeks; no one sees him regularly |
| Frederik Baert | MRN-21308 | ineligible | Other primary neurodegenerative diagnosis: probable dementia with Lewy bodies (abnormal DaTscan 2025-11-18, letter 2025-12-09), No amyloid biomarker |
| Albert Huysmans | MRN-21310 | ineligible | Ischaemic stroke 2026-01-19 (stay V-2201) — within 12 months of screening |

### T6 RA-ADVANCE — 3 eligible, 22 ineligible, 0 pending

| Patient | MRN | Verdict | Why |
|---------|-----|---------|-----|
| Chen Wei-Lin | MRN-10049 | **eligible** | Age 58 on 2026-06-30; seropositive RA since 08/2020, DAS28-CRP 5.0, SJC28 6, TJC28 8 (2026-03-24); 4.6 with SJC 5/TJC 7 on 2025-12-09, Inadequate response to MTX 20 mg/week (dose since 12/2020, stable since restart 02/2024) + HCQ, No prior b/tsDMARD (biologic recommended but not started), IGRA, HBsAg, anti-HBc, HCV Ab negative (2026-03-24); eGFR 85; no malignancy |
| Sofie Van Linden | MRN-21401 | **eligible** | Age 47 on 2026-06-30; seropositive RA diagnosed 2025-03-11 (> 6 months), DAS28-CRP 5.1, SJC28 7, TJC28 9 (2026-03-03; also 2026-02-10), MTX 20 mg/week since 2025-09-09 (> 12 weeks, stable >= 4 weeks) with inadequate response, No prior b/tsDMARD; IGRA, HBsAg, anti-HBc, HCV Ab negative (2026-02-10), No malignancy; not pregnant (urine hCG negative 2026-02-10), IUD; eGFR >90 |
| Kaat Mestdagh | MRN-21402 | **eligible** | Age 41; seropositive RA since 2024-06-18, DAS28-CRP 4.4, SJC28 5, TJC28 6 (2026-02-17; also 2026-01-27), Documented MTX intolerance: nausea on 15 mg/week (oral then SC) and ALT 118 U/L (2025-10-07); reduced to maximum tolerated 10 mg/week SC since 2025-10-14, No prior b/tsDMARD; IGRA, HBsAg, anti-HBc, HCV Ab negative (2026-01-27), No malignancy; urine hCG negative, copper IUD; eGFR >90 |
| Evelyn Hartman | MRN-10005 | ineligible | Age 84 on 2026-06-30 (> 75), Never treated with methotrexate (HCQ monotherapy) |
| Margaret Chen | MRN-10015 | ineligible | Age 84 on 2026-06-30 (> 75); seropositive RA on MTX/HCQ |
| Beatrice Osei-Mensah | MRN-10019 | ineligible | Not RA — erosive (nodal) osteoarthritis, RF/anti-CCP negative |
| Amara Johnson | MRN-10021 | ineligible | Not RA — systemic lupus erythematosus with class III lupus nephritis |
| Camille Dubois | MRN-10028 | ineligible | Failure of 2 biologic DMARDs: adalimumab (secondary loss of response, anti-drug antibodies, stopped 2025-09) and etanercept (primary non-response, stopped 2026-03); now on abatacept, Active disease (DAS28-CRP 5.2, SJC28 6, TJC28 7 on 2026-03-16) and serology negative, but excluded |
| Ahmad Hassan Al-Rashidi | MRN-10034 | ineligible | Not RA — tophaceous gout |
| Patricia Kowalski | MRN-10037 | ineligible | Age 80 on 2026-06-30 (> 75); RA in remission (DAS28 2.6, 2023) |
| Grace Ndiaye | MRN-10043 | ineligible | Positive QuantiFERON-TB (2026-04-20) — latent TB; rifampicin 4 months started 2026-05-25, not completed by 2026-06-30, Otherwise meets inclusion: DAS28-CRP 4.5-4.7 with SJC28 5, TJC28 6-7 on MTX 15 mg/week (stable since 08/2023) + HCQ |
| George Papadopoulos | MRN-10053 | ineligible | Not RA — gout |
| Priya Chakraborty | MRN-10061 | ineligible | Pregnant 2025 (delivered 2025-10-30) and exclusively breastfeeding as of 2026-03-18 with plan to continue to >= 12 months — breastfeeding exclusion, Also not on methotrexate since 05/2024 (certolizumab + HCQ), so no MTX inadequate-response criterion |
| Sonia Alvarez-Mendez | MRN-10068 | ineligible | Not RA — systemic lupus erythematosus with class III lupus nephritis |
| Derek Johansson | MRN-10073 | ineligible | Not RA — axial spondyloarthritis (ankylosing spondylitis), HLA-B27 positive |
| Dorothy Chen-Williams | MRN-10074 | ineligible | Not RA — nodal osteoarthritis, RF/anti-CCP negative |
| Margaret Sullivan | MRN-10075 | ineligible | Age 78 on 2026-06-30 (> 75); seropositive RA on MTX 15 mg |
| Patricia Nguyen | MRN-10083 | ineligible | Not RA — systemic lupus erythematosus with class III lupus nephritis |
| Constance Beaumont | MRN-10090 | ineligible | Age 81 on 2026-06-30 (> 75), Low disease activity (DAS28 2.4, 2021); also on apixaban (not an exclusion) |
| Dorothy Mae Henderson | MRN-10098 | ineligible | Age 85 on 2026-06-30 (> 75); RA on HCQ only, low activity (DAS28 2.8, 2021) |
| Sylvia Johansson | MRN-10102 | ineligible | Seronegative RA in remission on HCQ monotherapy (DAS28 2.4, 2023-05-15), Never received methotrexate — MTX criterion not met; no active disease |
| Amara Diallo | MRN-10104 | ineligible | Not RA — systemic lupus erythematosus with class III lupus nephritis |
| Dimitri Popescu | MRN-21403 | ineligible | Anti-HBc positive with HBsAg negative (2026-03-10) — resolved hepatitis B, exclusion, Otherwise meets inclusion: DAS28-CRP 5.3, SJC28 8, TJC28 10 on MTX 20 mg/week since 2025-03-03 |
| Veronique Laurent | MRN-21404 | ineligible | Breast cancer (left IDC pT1c N0) treated 2023 — wide local excision 2023-03-14, radiotherapy, letrozole ongoing: malignancy within 5 years, Otherwise meets inclusion: DAS28-CRP 4.9, SJC28 6, TJC28 8 on MTX 20 mg/week since 2025-02-11; serology negative |
| Pieter-Jan Govaerts | MRN-21405 | ineligible | Most recent DAS28-CRP 3.0 (< 3.2) with SJC28 3 (< 4) on 2026-06-09; also 3.1 with SJC28 3 on 2026-04-14 — insufficient disease activity |

## 4. Medical department — quality-label audits

Every adult inpatient stay written for this dataset follows the hospital's *Inpatient documentation standard 2025* (codes D1–D11 below) and, where relevant, a specialty label (stroke unit, hip fracture, oncology MDT, Baby-Friendly maternity). Roughly one element in six to ten is deliberately missing — really absent from the text, not written as "not documented" — so an audit has real findings to surface.

| Code | Element |
|------|---------|
| D1 | Allergy status (named allergies or "no known drug allergies") at admission |
| D2 | VTE risk assessment within 24 h with the prophylaxis decision |
| D3 | Falls risk — Morse Fall Scale score — within 24 h (patients ≥ 65) |
| D4 | Pressure-injury risk — Braden score — within 24 h |
| D5 | Nutritional screening — NRS-2002 (adults), STRONGkids (children) — within 48 h |
| D6 | Medication reconciliation within 24 h of admission (who, which sources) |
| D7 | Pain score (NRS 0–10) on admission |
| D8 | Surgery: written informed consent before the procedure; WHO Surgical Safety Checklist (sign-in, time-out, sign-out with times); prophylactic antibiotic given 0–60 min before incision (drug, time, knife-to-skin time) |
| D9 | Code status / treatment-limitation decision for patients ≥ 80 or admitted to ICU |
| D10 | Antimicrobial review at 48–72 h for patients on IV antibiotics |
| D11 | Discharge letter finalised ≤ 7 days after discharge, with diagnoses, medications and follow-up |

Example questions: *Audit all surgical stays of 2025 for the WHO checklist and antibiotic timing.* · *Which stroke patients received oral medication before a swallow screen?* · *Which discharge letters were finalised more than 7 days after discharge?* · *Were all new lung cancers discussed at MDT before first treatment?*

### Findings (non-compliant elements)

| Stay | Patient | Department | Finding |
|------|---------|------------|---------|
| V-1002 | Marleen Verbruggen | Neurology | D5 nutritional screening missing |
| V-1003 | Ahmed Benali | Neurology | label: swallow screen before first oral intake |
| V-1004 | Monique Delvaux | Neurology | D11 letter 11 days after discharge |
| V-1004 | Monique Delvaux | Neurology | label: door to needle ≤ 60min |
| V-1008 | Nadia Bouzid | Neurology | label: door to ct ≤ 25min |
| V-1008 | Nadia Bouzid | Neurology | label: statin at discharge |
| V-1102 | Albert Desmedt | Orthopaedics | D6 medication reconciliation missing |
| V-1103 | Simone Wuyts | Orthopaedics | label: surgery within 36h of arrival |
| V-1104 | Paul Dierickx | Orthopaedics | label: orthogeriatric review within 72h |
| V-1104 | Paul Dierickx | Orthopaedics | label: 4AT delirium screen |
| V-1106 | Josee Renard | Orthopaedics | D5 nutritional screening missing |
| V-1107 | Frans Baetens | Orthopaedics | D11 letter 9 days after discharge |
| V-1107 | Frans Baetens | Orthopaedics | label: surgery within 36h of arrival |
| V-1107 | Frans Baetens | Orthopaedics | label: bone health treatment at discharge |
| V-1202 | Rita Van Laere | Cardiology | label: door to balloon ≤ 90 |
| V-1202 | Rita Van Laere | Cardiology | label: door to ecg ≤ 10 |
| V-1205 | Irene Lejeune | Cardiology | label: beta blocker |
| V-1205 | Irene Lejeune | Cardiology | label: cardiac rehab referral |
| V-1206 | Hendrik De Wit | Cardiology | label: P2Y12 |
| V-1206 | Hendrik De Wit | Cardiology | label: cardiac rehab referral |
| V-1302 | Gilberte Somers | Cardiology | label: gdmt 4 pillars or documented reason |
| V-1302 | Gilberte Somers | Cardiology | label: follow up ≤ 14 days |
| V-1403 | Madeleine Vos | Internal Medicine | label: sepsis bundle complete |
| V-1403 | Madeleine Vos | Internal Medicine | label: antibiotic within 60 min |
| V-1404 | Kevin De Backer | Internal Medicine | label: sepsis bundle complete |
| V-1404 | Kevin De Backer | Internal Medicine | label: antibiotic within 60 min |
| V-1409 | Hilda Wellens | Orthopaedics | label: sepsis bundle complete |
| V-1409 | Hilda Wellens | Orthopaedics | label: blood cultures before antibiotic |
| V-1505 | Marc Vandevelde | Orthopaedics | D8 surgical safety (consent / WHO checklist / prophylaxis timing) missing |
| V-1505 | Marc Vandevelde | Orthopaedics | label: antibiotic 0 60 min before incision |
| V-1506 | Ingrid Hellemans | Orthopaedics | D8 surgical safety (consent / WHO checklist / prophylaxis timing) missing |
| V-1506 | Ingrid Hellemans | Orthopaedics | label: who sign out |
| V-1507 | Patrick Donckers | Orthopaedics | D8 surgical safety (consent / WHO checklist / prophylaxis timing) missing |
| V-1507 | Patrick Donckers | Orthopaedics | label: antibiotic 0 60 min before incision |
| V-1509 | Johan Verbeke | Orthopaedics | D5 nutritional screening missing |
| V-1609 | Marthe Van den Bossche | General Surgery | D9 code status missing |
| V-1804 | Elke Roose | Obstetrics/Gynaecology | label: skin to skin within 1h |
| V-1804 | Elke Roose | Obstetrics/Gynaecology | label: breastfeeding within 1h |
| V-1806 | Hanna Wauters | Obstetrics/Gynaecology | label: skin to skin within 1h |
| V-1808 | Lina El Khatib | Pediatrics | D5 nutritional screening missing |
| V-1808 | Lina El Khatib | Pediatrics | label: STRONGkids within 48h |
| V-1906 | Joke Verhulst | Internal Medicine | D10 48-72 h antimicrobial review missing |
| V-1907 | Octavie Stassen | Geriatrics | D4 pressure-injury risk (Braden) missing |
| V-2006 | Denise Wyns | Ophthalmology | D8 surgical safety (consent / WHO checklist / prophylaxis timing) missing |
| V-2006 | Denise Wyns | Ophthalmology | label: who time out |
| V-21207-005 | Kathleen Dewitte | Pulmonology | label: mdt before first treatment |

46 findings across 124 audited encounters.

## 5. Business intelligence — questions answered in minutes

Example questions: *Admissions per month by department in 2025?* · *Average length of stay per department?* · *How many ED attendances breached 4 hours?* · *Day-case volumes by specialty and the unplanned-overnight rate?* · *Share of patients over 80 discharged to a nursing home?* · *How many outpatient documents did Rheumatology produce per year?*

### Inpatient admissions per month (2025-01 → 2026-06)

| Month | Admissions | By department |
|-------|-----------:|---------------|
| 2025-01 | 4 | Neurology 1, Orthopaedics 1, Internal Medicine 1, Pulmonology 1 |
| 2025-02 | 7 | Internal Medicine 2, Vascular Surgery 1, Neurology 1, Cardiology 1, Orthopaedics 1, Pulmonology 1 |
| 2025-03 | 5 | Nephrology 1, Orthopaedics 1, Cardiology 1, General Surgery 1, Obstetrics/Gynaecology 1 |
| 2025-04 | 5 | Internal Medicine 2, General Surgery 1, Orthopaedics 1, Pulmonology 1 |
| 2025-05 | 3 | Orthopaedics 1, Neurology 1, General Surgery 1 |
| 2025-06 | 7 | Pulmonology 2, Orthopaedics 2, Cardiology 1, Obstetrics/Gynaecology 1, Internal Medicine 1 |
| 2025-07 | 6 | Pulmonology 2, Intensive Care 1, Cardiology 1, Internal Medicine 1, Orthopaedics 1 |
| 2025-08 | 5 | Pediatrics 2, Neurology 1, Orthopaedics 1, General Surgery 1 |
| 2025-09 | 7 | Internal Medicine 3, Geriatrics 1, Cardiology 1, Orthopaedics 1, Obstetrics/Gynaecology 1 |
| 2025-10 | 6 | Neurology 1, Cardiology 1, General Surgery 1, Pulmonology 1, Gastroenterology 1, Thoracic Surgery 1 |
| 2025-11 | 7 | Orthopaedics 2, Cardiology 2, Pulmonology 2, Pediatrics 1 |
| 2025-12 | 6 | Internal Medicine 2, General Surgery 2, Neurology 1, Obstetrics/Gynaecology 1 |
| 2026-01 | 9 | Neurology 2, Orthopaedics 2, Vascular Surgery 1, Cardiology 1, General Surgery 1, Pulmonology 1, Geriatrics 1 |
| 2026-02 | 6 | Cardiology 2, Oncology 1, Pulmonology 1, Obstetrics/Gynaecology 1, Pediatrics 1 |
| 2026-03 | 8 | Orthopaedics 2, Pulmonology 2, Neurology 1, Internal Medicine 1, General Surgery 1, Cardiology 1 |
| 2026-04 | 4 | Internal Medicine 2, Cardiology 1, General Surgery 1 |
| 2026-05 | 4 | Neurology 1, Cardiology 1, Orthopaedics 1, Obstetrics/Gynaecology 1 |
| 2026-06 | 4 | Orthopaedics 2, General Surgery 1, Gastroenterology 1 |

### Length of stay by department (all inpatient stays)

| Department | Stays | Bed-days | Mean LOS | Emergency / elective |
|------------|------:|---------:|---------:|---------------------:|
| Orthopaedics | 21 | 129 | 6.1 | 12 / 9 |
| Pulmonology | 16 | 76 | 4.8 | 16 / 0 |
| Cardiology | 16 | 88 | 5.5 | 16 / 0 |
| Internal Medicine | 16 | 94 | 5.9 | 16 / 0 |
| General Surgery | 14 | 79 | 5.6 | 8 / 6 |
| Neurology | 10 | 65 | 6.5 | 10 / 0 |
| Obstetrics/Gynaecology | 7 | 22 | 3.1 | 6 / 1 |
| Pediatrics | 4 | 15 | 3.8 | 4 / 0 |
| Vascular Surgery | 2 | 12 | 6.0 | 1 / 1 |
| Geriatrics | 2 | 17 | 8.5 | 2 / 0 |
| Gastroenterology | 2 | 12 | 6.0 | 2 / 0 |
| General Medicine | 1 | 4 | 4.0 | 1 / 0 |
| Nephrology | 1 | 5 | 5.0 | 1 / 0 |
| Intensive Care | 1 | 2 | 2.0 | 1 / 0 |
| Oncology | 1 | 5 | 5.0 | 1 / 0 |
| Thoracic Surgery | 1 | 5 | 5.0 | 0 / 1 |

**Discharge destinations:** home 93, home_with_care 8, nursing_home 5, rehabilitation 4, deceased 4, another_hospital 1.

**Day cases:** 20 — Ophthalmology 7, General Surgery 5, Gastroenterology 4, Orthopaedics 2, Urology 2.

**ED attendances without admission:** 25.

### Documents per department and year

| Department | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 | Total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Internal Medicine | 2 | 38 | 93 | 101 | 101 | 79 | 80 | 50 | 23 | 13 | 580 |
| Orthopaedics |  | 1 | 13 | 13 | 33 | 19 | 9 | 6 | 36 | 19 | 149 |
| Cardiology |  | 10 | 13 | 10 | 19 | 7 | 10 | 20 | 30 | 28 | 147 |
| Obstetrics/Gynaecology |  |  | 4 | 40 | 28 | 8 | 25 | 14 | 13 | 4 | 136 |
| Emergency Medicine |  |  | 5 | 11 | 16 | 7 | 6 | 12 | 49 | 27 | 133 |
| Neurology |  | 4 | 13 | 18 | 15 | 12 | 13 | 9 | 25 | 18 | 127 |
| Pulmonology |  | 8 | 14 | 14 | 12 | 9 | 8 | 11 | 21 | 29 | 126 |
| Gastroenterology |  |  | 13 | 17 | 18 | 32 | 15 | 5 | 13 | 5 | 118 |
| Rheumatology |  | 3 | 8 | 17 | 24 | 20 | 19 | 7 | 5 | 13 | 116 |
| Radiology |  |  |  |  |  | 1 | 1 | 9 | 56 | 23 | 90 |
| Nursing |  |  |  |  |  |  |  |  | 57 | 33 | 90 |
| Dermatology |  |  |  | 4 | 24 | 24 | 17 | 3 |  |  | 72 |
| General Surgery |  |  | 1 | 3 | 7 | 4 | 1 | 12 | 24 | 17 | 69 |
| Oncology |  | 4 | 8 | 8 | 14 | 8 | 9 |  |  | 14 | 65 |
| Ophthalmology |  | 2 | 10 | 7 | 12 | 10 | 2 | 1 | 8 | 6 | 58 |
| Endocrinology | 1 | 4 | 8 | 9 | 9 | 6 | 6 |  | 2 | 6 | 51 |
| Psychiatry |  |  |  | 7 | 11 | 20 | 8 | 3 |  |  | 49 |
| Nephrology |  | 6 | 2 | 4 | 4 | 7 | 5 | 4 | 5 | 5 | 42 |
| Urology |  |  | 8 | 6 | 5 | 7 | 2 | 4 | 2 | 2 | 36 |
| Pathology |  |  |  |  |  |  |  | 3 | 12 | 14 | 29 |
| ENT |  |  | 1 | 2 | 2 | 5 | 9 |  |  |  | 19 |
| Geriatrics |  |  |  |  |  |  |  |  | 11 | 6 | 17 |
| Pediatrics |  |  |  |  |  |  |  |  | 9 | 3 | 12 |
| Nuclear Medicine |  |  |  |  |  |  |  |  | 2 | 10 | 12 |
| General Practice |  |  |  |  |  |  |  | 2 | 7 | 2 | 11 |
| Intensive Care |  |  |  |  |  |  |  |  | 9 |  | 9 |
| Midwifery |  |  |  |  |  |  |  |  | 5 | 4 | 9 |
| Microbiology |  |  |  |  |  |  |  | 3 | 5 |  | 8 |
| Vascular Surgery |  |  |  |  |  |  |  |  | 3 | 4 | 7 |
| Speech and Language Therapy |  |  |  |  |  |  |  |  | 3 | 1 | 4 |
| Physiotherapy |  |  |  |  |  |  |  |  | 2 | 2 | 4 |
| Dietetics |  |  |  |  |  |  |  |  | 2 | 1 | 3 |
| Pharmacy |  |  |  |  |  |  |  |  | 2 | 1 | 3 |
| Palliative Care |  |  |  |  |  |  |  |  | 3 |  | 3 |
| Thoracic Surgery |  |  |  |  |  |  |  |  | 3 |  | 3 |
| Anaesthesiology |  |  |  |  |  |  |  |  | 1 | 1 | 2 |
| Neuropsychology |  |  |  |  |  |  |  |  | 2 |  | 2 |
| General Medicine |  |  |  |  |  | 1 |  |  |  |  | 1 |
| Cardiology Outpatients |  |  |  |  |  |  |  | 1 |  |  | 1 |
| Respiratory Outpatients |  |  |  |  |  |  |  | 1 |  |  | 1 |

## Appendix — every hospital stay and day case

| Stay | Patient | Department | Admitted | Discharged | LOS | Route | Destination | Principal diagnosis |
|------|---------|------------|----------|------------|----:|-------|-------------|---------------------|
| V-903 | Margaret Doyle | General Medicine | 2022-11-08 | 2022-11-12 | 4 | emergency | home | (see discharge letter) |
| V-902 | Margaret Doyle | Orthopaedics | 2023-09-12 | 2023-09-14 | 2 | emergency | home | (see discharge letter) |
| V-001 | Robert Hayes | Pulmonology | 2024-03-14 | 2024-03-18 | 4 | emergency | home | (see discharge letter) |
| V-002 | Margaret Doyle | Orthopaedics | 2024-04-02 | 2024-04-09 | 7 | emergency | home | (see discharge letter) |
| V-003 | Geoffrey Almeida | Cardiology | 2024-05-06 | 2024-05-12 | 6 | emergency | home | (see discharge letter) |
| V-004 | Daniel Okonkwo | General Surgery | 2024-06-10 | 2024-06-20 | 10 | elective | home | (see discharge letter) |
| V-005 | Aisha Rahman | Internal Medicine | 2024-07-01 | 2024-07-06 | 5 | emergency | home | (see discharge letter) |
| V-006 | Henry Lindqvist | General Surgery | 2024-08-05 | 2024-08-13 | 8 | elective | home | (see discharge letter) |
| V-007 | Sofia Marchetti | Obstetrics/Gynaecology | 2024-09-12 | 2024-09-16 | 4 | emergency | home | (see discharge letter) |
| V-008 | Lucas Fernandez | General Surgery | 2024-10-03 | 2024-10-06 | 3 | emergency | home | (see discharge letter) |
| V-009 | Patricia Nowak | Cardiology | 2024-11-08 | 2024-11-13 | 5 | emergency | home | (see discharge letter) |
| V-010 | Walter Brennan | Pulmonology | 2024-12-02 | 2024-12-07 | 5 | emergency | home | (see discharge letter) |
| V-1701 | Emma Willaert | Pulmonology | 2025-01-09 | 2025-01-12 | 3 | emergency | home | Community-acquired pneumonia, right lower lobe, Streptococcus pneumoniae |
| V-011 | Eleanor Whitfield | Neurology | 2025-01-15 | 2025-01-23 | 8 | emergency | home | (see discharge letter) |
| V-1101 | Maria Vandenberghe | Orthopaedics | 2025-01-22 | 2025-01-31 | 9 | emergency | rehabilitation | Displaced intracapsular fracture of the right femoral neck (S72.0) |
| V-2001 | Josephine Van Roy | Ophthalmology | 2025-01-27 | 2025-01-27 | 0 | elective | home | Right age-related cataract |
| V-1401 | Harold Cunningham | Internal Medicine | 2025-01-28 | 2025-02-02 | 5 | emergency | home | Sepsis due to Escherichia coli (bacteraemia) from complicated urinary tract infection / acute prostatitis |
| V-1901 | Freddy Bosmans | Internal Medicine | 2025-02-03 | 2025-02-07 | 4 | emergency | home | Duodenal ulcer with haemorrhage (Forrest IIa) due to NSAID and aspirin |
| V-012 | Grace Mbeki | Internal Medicine | 2025-02-04 | 2025-02-10 | 6 | emergency | home | (see discharge letter) |
| V-1501 | Margaret O'Brien | Orthopaedics | 2025-02-10 | 2025-02-13 | 3 | elective | home | Primary osteoarthritis of the left knee |
| V-1001 | Jozef Van Acker | Neurology | 2025-02-17 | 2025-02-23 | 6 | emergency | home | Acute ischaemic stroke, left MCA territory, cardioembolic (I63.4) |
| V-013 | Thomas Sandberg | Vascular Surgery | 2025-02-20 | 2025-03-02 | 10 | emergency | home | (see discharge letter) |
| V-1702 | Jean-Pierre Hubert | Pulmonology | 2025-02-20 | 2025-02-27 | 7 | emergency | home_with_care | Community-acquired pneumonia, left lower lobe, organism not identified |
| V-2008 | Filip Vanderlinden | Gastroenterology | 2025-02-24 | 2025-02-24 | 0 | elective | home | Colonic adenomas (tubular, low-grade dysplasia) |
| V-1202 | Rita Van Laere | Cardiology | 2025-02-26 | 2025-03-02 | 4 | emergency | home | Acute inferior ST-elevation myocardial infarction (mid RCA) |
| V-1801 | Julie Verschueren | Obstetrics/Gynaecology | 2025-03-03 | 2025-03-05 | 2 | emergency | home | Spontaneous vertex delivery at term (40+2), single live birth |
| V-1301 | Eddy Coppieters | Cardiology | 2025-03-04 | 2025-03-12 | 8 | emergency | home | Acute decompensated chronic HFrEF (LVEF 25 %), ischaemic cardiomyopathy |
| V-2002 | Alfons Geerinck | Ophthalmology | 2025-03-10 | 2025-03-10 | 0 | elective | home | Left age-related cataract with pseudoexfoliation |
| V-1102 | Albert Desmedt | Orthopaedics | 2025-03-11 | 2025-03-24 | 13 | emergency | nursing_home | Unstable left intertrochanteric fracture (S72.1) |
| V-014 | Geoffrey Almeida | Nephrology | 2025-03-18 | 2025-03-23 | 5 | emergency | home | (see discharge letter) |
| V-1601 | Samantha Okafor | General Surgery | 2025-03-18 | 2025-03-19 | 1 | elective | home | Symptomatic cholelithiasis |
| V-1402 | Dirk Lauwers | Internal Medicine | 2025-04-03 | 2025-04-20 | 17 | emergency | rehabilitation | Pneumococcal pneumonia (right lower lobe) with Streptococcus pneumoniae bacteraemia and septic shock |
| V-015 | Beatrice Lefevre | General Surgery | 2025-04-07 | 2025-04-11 | 4 | emergency | home | (see discharge letter) |
| V-2012 | Guy Lemoine | General Surgery | 2025-04-07 | 2025-04-07 | 0 | elective | home | Left indirect inguinal hernia |
| V-1703 | Jean-Pierre Hubert | Pulmonology | 2025-04-09 | 2025-04-14 | 5 | emergency | home_with_care | RSV lower respiratory tract infection |
| V-1502 | Guido Martens | Orthopaedics | 2025-04-14 | 2025-04-16 | 2 | elective | home | Primary osteoarthritis of the right knee |
| V-1902 | Sarah Moreau | Internal Medicine | 2025-04-22 | 2025-04-25 | 3 | emergency | home | Diabetic ketoacidosis in type 1 diabetes due to insulin pump failure |
| V-016 | Frank Geerts | Orthopaedics | 2025-05-05 | 2025-05-13 | 8 | elective | home | (see discharge letter) |
| V-2003 | Maurice Dujardin | Ophthalmology | 2025-05-05 | 2025-05-05 | 0 | elective | home | Right age-related cataract |
| V-1002 | Marleen Verbruggen | Neurology | 2025-05-08 | 2025-05-19 | 11 | emergency | rehabilitation | Acute ischaemic stroke, left MCA territory, left M1 occlusion, ESUS (I63.4) |
| V-1602 | Thibault Marechal | General Surgery | 2025-05-26 | 2025-06-01 | 6 | emergency | home | Acute gangrenous perforated appendicitis with pelvic abscess |
| V-1103 | Simone Wuyts | Orthopaedics | 2025-06-02 | 2025-06-11 | 9 | emergency | deceased | Displaced intracapsular fracture of the right femoral neck (S72.0) |
| V-017 | Hugo Vermeulen | Pulmonology | 2025-06-03 | 2025-06-09 | 6 | emergency | home | (see discharge letter) |
| V-1302 | Gilberte Somers | Cardiology | 2025-06-10 | 2025-06-16 | 6 | emergency | home | New-onset heart failure with reduced ejection fraction (LVEF 32 %), hypertensive heart disease |
| V-1802 | Laura Deconinck | Obstetrics/Gynaecology | 2025-06-16 | 2025-06-19 | 3 | emergency | home | Obstetric anal sphincter injury, third-degree tear grade 3b, after ventouse delivery at 41+1 |
| V-2009 | Katrien Nuyts | Gastroenterology | 2025-06-16 | 2025-06-16 | 0 | elective | home | Positive FIT, normal colonoscopy |
| V-1704 | Clementine Hoste | Pulmonology | 2025-06-18 | 2025-06-26 | 8 | emergency | nursing_home | Aspiration pneumonia, right lung |
| V-1503 | Nicole Pauwels | Orthopaedics | 2025-06-23 | 2025-06-26 | 3 | elective | home | Primary osteoarthritis of the right hip |
| V-1903 | Germaine Lievens | Internal Medicine | 2025-06-30 | 2025-07-07 | 7 | emergency | home | Hyponatraemia due to indapamide and low solute intake |
| V-1303 | Gilberte Somers | Cardiology | 2025-07-04 | 2025-07-11 | 7 | emergency | home | Acute decompensated HFrEF (LVEF 32 %), early readmission |
| V-1203 | Marcel Verlinden | Intensive Care | 2025-07-09 | 2025-07-11 | 2 | emergency | deceased | Acute anterior ST-elevation myocardial infarction (left main / proximal LAD) with cardiogenic shock (Killip IV) |
| V-2015 | Robert Descamps | Orthopaedics | 2025-07-14 | 2025-07-14 | 0 | elective | home | Displaced flap tear of the medial meniscus, left knee |
| V-1403 | Madeleine Vos | Internal Medicine | 2025-07-15 | 2025-07-20 | 5 | emergency | deceased | Sepsis of uncertain source (urinary tract vs lower respiratory tract), organism unidentified |
| V-1504 | Nicole Pauwels | Orthopaedics | 2025-07-16 | 2025-07-18 | 2 | emergency | home | Posterior dislocation of right total hip replacement |
| V-018 | Hugo Vermeulen | Pulmonology | 2025-07-21 | 2025-07-25 | 4 | emergency | home | (see discharge letter) |
| V-1708 | Tom Degroote | Pulmonology | 2025-07-28 | 2025-07-30 | 2 | emergency | home | Acute bilateral segmental pulmonary embolism after long-haul flight |
| V-1603 | Rosa Ferrante | General Surgery | 2025-08-04 | 2025-08-09 | 5 | emergency | home | Adhesional small-bowel obstruction (previous hysterectomy 1992) |
| V-019 | Mila Declercq | Pediatrics | 2025-08-11 | 2025-08-15 | 4 | emergency | home | (see discharge letter) |
| V-1808 | Lina El Khatib | Pediatrics | 2025-08-18 | 2025-08-21 | 3 | emergency | home | Acute pyelonephritis (febrile UTI) due to Escherichia coli, aged 8 months |
| V-1104 | Paul Dierickx | Orthopaedics | 2025-08-19 | 2025-08-26 | 7 | emergency | home | Displaced intracapsular fracture of the left femoral neck (S72.0) |
| V-2017 | Werner Stijnen | Urology | 2025-08-25 | 2025-08-25 | 0 | elective | home | Non-invasive papillary urothelial carcinoma of the bladder, low grade, pTa |
| V-1003 | Ahmed Benali | Neurology | 2025-08-26 | 2025-09-02 | 7 | emergency | home | Acute ischaemic stroke, right MCA territory (corona radiata), wake-up stroke (I63.3) |
| V-1904 | James Okafor | Internal Medicine | 2025-09-01 | 2025-09-07 | 6 | emergency | home | Acute kidney injury stage 3, prerenal, from norovirus gastroenteritis with ARB and NSAID |
| V-1304 | Arlette Bogaert | Internal Medicine | 2025-09-08 | 2025-09-14 | 6 | emergency | home | Acute decompensated heart failure with preserved ejection fraction (LVEF 58 %) |
| V-1803 | Samira Ouali | Obstetrics/Gynaecology | 2025-09-09 | 2025-09-12 | 3 | elective | home | Breech presentation at term (39+1), delivered by elective caesarean section |
| V-1105 | Paul Dierickx | Geriatrics | 2025-09-15 | 2025-09-20 | 5 | emergency | home_with_care | Febrile urinary tract infection due to Klebsiella pneumoniae (probable prostatitis) |
| V-1505 | Marc Vandevelde | Orthopaedics | 2025-09-15 | 2025-09-21 | 6 | elective | home | Primary osteoarthritis of the left knee (valgus) |
| V-1404 | Kevin De Backer | Internal Medicine | 2025-09-22 | 2025-09-28 | 6 | emergency | home | Cellulitis of the right lower leg with Streptococcus pyogenes (group A) bacteraemia and sepsis |
| V-2004 | Rosette Van Wijk | Ophthalmology | 2025-09-29 | 2025-09-29 | 0 | elective | home | Left age-related cataract |
| V-1204 | Tomasz Kowalczyk | Cardiology | 2025-09-30 | 2025-10-03 | 3 | emergency | home | NSTEMI (type 1) due to in-stent restenosis of mid-LAD stent |
| V-1307 | Bart Cuypers | Cardiology | 2025-10-02 | 2025-10-09 | 7 | emergency | home | Heart failure with reduced ejection fraction due to new non-ischaemic dilated cardiomyopathy (LVEF 30 %) |
| V-1604 | Etienne Dumortier | General Surgery | 2025-10-06 | 2025-10-27 | 21 | elective | home | Adenocarcinoma of the upper rectum, pT2 pN0 (0/16) R0, stage I |
| V-2102 | Pascal Delmotte | Thoracic Surgery | 2025-10-13 | 2025-10-18 | 5 | elective | home | Adenocarcinoma of the right upper lobe, stage IB (cT2a N0 M0; pathology pT2a pN0 R0) |
| V-2010 | Stefaan Meert | Gastroenterology | 2025-10-13 | 2025-10-13 | 0 | elective | home | Adenocarcinoma of the sigmoid colon |
| V-1004 | Monique Delvaux | Neurology | 2025-10-14 | 2025-10-19 | 5 | emergency | home | Acute ischaemic stroke, left MCA branch (M3) territory, ESUS (I63.4) |
| V-1705 | Rudi Vercammen | Pulmonology | 2025-10-20 | 2025-10-26 | 6 | emergency | home | Severe acute exacerbation of COPD due to Haemophilus influenzae |
| V-1905 | Dominique Hanssens | Gastroenterology | 2025-10-27 | 2025-11-05 | 9 | emergency | home | Decompensated alcohol-related cirrhosis with tense ascites |
| V-1506 | Ingrid Hellemans | Orthopaedics | 2025-11-03 | 2025-11-06 | 3 | elective | home | Primary osteoarthritis of the left hip |
| V-1106 | Josee Renard | Orthopaedics | 2025-11-04 | 2025-11-14 | 10 | emergency | rehabilitation | Displaced intracapsular fracture of the right femoral neck (S72.0) |
| V-1709 | Louis Vanneste | Pulmonology | 2025-11-10 | 2025-11-14 | 4 | emergency | home | Primary spontaneous pneumothorax, left |
| V-1205 | Irene Lejeune | Cardiology | 2025-11-17 | 2025-11-22 | 5 | emergency | home_with_care | NSTEMI (type 1), conservative management |
| V-2013 | Mohamed Tahiri | General Surgery | 2025-11-17 | 2025-11-18 | 1 | elective | home | Bilateral direct inguinal hernias |
| V-1706 | Rudi Vercammen | Pulmonology | 2025-11-19 | 2025-11-24 | 5 | emergency | home | Acute exacerbation of COPD |
| V-1809 | Arthur Vincke | Pediatrics | 2025-11-24 | 2025-11-28 | 4 | emergency | home | Type 1 diabetes mellitus, new diagnosis, presenting with moderate diabetic ketoacidosis |
| V-1305 | Willy Van Hoof | Cardiology | 2025-11-25 | 2025-12-04 | 9 | emergency | home | Acute decompensated heart failure, ischaemic cardiomyopathy LVEF 38 % |
| V-1605 | Fatima Zahra El Idrissi | General Surgery | 2025-12-01 | 2025-12-04 | 3 | emergency | home | Acute calculous cholecystitis (Tokyo grade I) |
| V-2016 | Ilse Van Bael | Orthopaedics | 2025-12-01 | 2025-12-01 | 0 | elective | home | Right carpal tunnel syndrome |
| V-1005 | Roger Timmermans | Neurology | 2025-12-03 | 2025-12-06 | 3 | emergency | deceased | Intracerebral haemorrhage, left basal ganglia, warfarin-associated (I61.0) |
| V-1405 | Chantal Boon | Internal Medicine | 2025-12-09 | 2025-12-16 | 7 | emergency | home | Acute ascending cholangitis (Tokyo grade II) due to choledocholithiasis, with Escherichia coli bacteraemia and sepsis |
| V-1606 | Fatima Zahra El Idrissi | General Surgery | 2025-12-09 | 2025-12-13 | 4 | emergency | home | Bile leak from cystic duct stump with subhepatic biloma after laparoscopic cholecystectomy |
| V-1804 | Elke Roose | Obstetrics/Gynaecology | 2025-12-15 | 2025-12-20 | 5 | emergency | home | Failure to progress in the first stage of labour at 40+5, delivered by category 2 emergency caesarean section |
| V-1906 | Joke Verhulst | Internal Medicine | 2025-12-15 | 2025-12-19 | 4 | emergency | home | Cellulitis of the left lower leg |
| V-1710 | Marie-Louise Dehaene | Pulmonology | 2026-01-06 | 2026-01-11 | 5 | emergency | home | Influenza A infection with acute exacerbation of COPD |
| V-1907 | Octavie Stassen | Geriatrics | 2026-01-08 | 2026-01-20 | 12 | emergency | nursing_home | Delirium due to urinary tract infection with Proteus mirabilis |
| V-1006 | Luc Moens | Neurology | 2026-01-12 | 2026-01-14 | 2 | emergency | home | Transient ischaemic attack, left carotid territory (G45.1) |
| V-1507 | Patrick Donckers | Orthopaedics | 2026-01-12 | 2026-01-15 | 3 | elective | home | Primary osteoarthritis of the right knee |
| V-1306 | Paula Geens | Cardiology | 2026-01-14 | 2026-01-24 | 10 | emergency | home_with_care | Acute decompensated heart failure, dilated cardiomyopathy LVEF 20 % |
| V-2201 | Albert Huysmans | Neurology | 2026-01-19 | 2026-01-24 | 5 | emergency | home | Acute ischaemic stroke, left MCA cortical branch territory (minor, NIHSS 3), probable artery-to-artery embolism from left ICA atheroma (40 % stenosis) |
| V-2005 | Emile Carpentier | Ophthalmology | 2026-01-19 | 2026-01-19 | 0 | elective | home | Right age-related cataract |
| V-1107 | Frans Baetens | Orthopaedics | 2026-01-20 | 2026-01-28 | 8 | emergency | home | Stable left intertrochanteric fracture (S72.1) |
| V-1007 | Luc Moens | Vascular Surgery | 2026-01-26 | 2026-01-28 | 2 | elective | home | Symptomatic left internal carotid artery stenosis (I65.2) |
| V-1607 | Gaston Leclercq | General Surgery | 2026-01-26 | 2026-01-31 | 5 | emergency | home | Acute biliary (gallstone) pancreatitis, mild |
| V-1707 | Sandra Vlaeminck | Pulmonology | 2026-02-02 | 2026-02-05 | 3 | emergency | home | Acute exacerbation of COPD |
| V-1807 | Noah Vandermeulen | Pediatrics | 2026-02-02 | 2026-02-06 | 4 | emergency | home | Acute bronchiolitis due to respiratory syncytial virus (RSV positive), aged 3 months |
| V-2011 | Veerle Bracke | Gastroenterology | 2026-02-02 | 2026-02-02 | 0 | elective | home | Coeliac disease (Marsh 3a) |
| V-1206 | Hendrik De Wit | Cardiology | 2026-02-03 | 2026-02-07 | 4 | emergency | another_hospital | NSTEMI (type 1) |
| V-1805 | Ines Demeulemeester | Obstetrics/Gynaecology | 2026-02-09 | 2026-02-13 | 4 | emergency | home | Pre-eclampsia with severe features at 37+0 weeks |
| V-1406 | Leen Verhaegen | Oncology | 2026-02-17 | 2026-02-22 | 5 | emergency | home | Febrile neutropenia / neutropenic sepsis after chemotherapy, no organism identified |
| V-2020 | Brigitte Mahieu | General Surgery | 2026-02-23 | 2026-02-23 | 0 | elective | home | Symptomatic cholelithiasis |
| V-1308 | Jeannine Raes | Cardiology | 2026-02-24 | 2026-03-02 | 6 | emergency | home | Acute decompensated heart failure with LVEF 40 % |
| V-1711 | Norbert Callewaert | Pulmonology | 2026-03-02 | 2026-03-06 | 4 | emergency | home | Acute exacerbation of asthma-COPD overlap |
| V-1908 | Ivan Petrov | Cardiology | 2026-03-03 | 2026-03-05 | 2 | emergency | home | Atrial fibrillation, new, with rapid ventricular response, cardioverted |
| V-1008 | Nadia Bouzid | Neurology | 2026-03-09 | 2026-03-13 | 4 | emergency | home | Lacunar infarct, right internal capsule (I63.8) |
| V-1608 | Gaston Leclercq | General Surgery | 2026-03-09 | 2026-03-10 | 1 | elective | home | Cholelithiasis after mild acute gallstone pancreatitis |
| V-2101 | Werner Goris | Pulmonology | 2026-03-10 | 2026-03-15 | 5 | emergency | home_with_care | Malignant left pleural effusion (secondary malignant neoplasm of pleura) from lung adenocarcinoma |
| V-1407 | Omar Haddad | Internal Medicine | 2026-03-16 | 2026-03-22 | 6 | emergency | home | Sepsis due to ESBL-producing Klebsiella pneumoniae (bacteraemia) from acute left pyelonephritis |
| V-2014 | Els Vandamme-Claus | General Surgery | 2026-03-16 | 2026-03-16 | 0 | elective | home | Umbilical hernia |
| V-1508 | Anne-Sophie Collard | Orthopaedics | 2026-03-23 | 2026-03-24 | 1 | elective | home | Primary anteromedial osteoarthritis of the right knee |
| V-1108 | Yvonne Cools | Orthopaedics | 2026-03-28 | 2026-04-08 | 11 | emergency | nursing_home | Displaced intracapsular fracture of the right femoral neck (S72.0) |
| V-1408 | Omar Haddad | Internal Medicine | 2026-04-05 | 2026-04-10 | 5 | emergency | home | Clostridioides difficile infection, severe, healthcare-associated (after meropenem in V-1407) |
| V-1609 | Marthe Van den Bossche | General Surgery | 2026-04-13 | 2026-04-20 | 7 | emergency | home_with_care | Incarcerated umbilical hernia without strangulation |
| V-2006 | Denise Wyns | Ophthalmology | 2026-04-20 | 2026-04-20 | 0 | elective | home | Left age-related cataract |
| V-1201 | Kristof Smets | Cardiology | 2026-04-21 | 2026-04-26 | 5 | emergency | home | Acute anterior ST-elevation myocardial infarction (proximal LAD) |
| V-1909 | Maryse Delhaye | Internal Medicine | 2026-04-27 | 2026-04-29 | 2 | emergency | home | Syncope due to drug-induced orthostatic hypotension |
| V-1509 | Johan Verbeke | Orthopaedics | 2026-05-04 | 2026-05-08 | 4 | elective | home | Primary osteoarthritis of the left hip |
| V-2018 | Lucienne Pirard | Urology | 2026-05-11 | 2026-05-11 | 0 | elective | home | Painless visible haematuria, normal cystoscopy |
| V-1207 | Sabine Mortier | Cardiology | 2026-05-12 | 2026-05-13 | 1 | emergency | home | Musculoskeletal (costochondral) chest wall pain |
| V-1009 | Georgette Lambrechts | Neurology | 2026-05-18 | 2026-06-01 | 14 | emergency | nursing_home | Acute ischaemic stroke, right MCA territory, right M1 occlusion, cardioembolic (I63.4) |
| V-1806 | Hanna Wauters | Obstetrics/Gynaecology | 2026-05-25 | 2026-05-26 | 1 | emergency | home | Spontaneous vertex delivery at term (39+4), single live birth |
| V-1109 | Liliane Stevens | Orthopaedics | 2026-06-01 | 2026-06-07 | 6 | emergency | home | Undisplaced intracapsular fracture of the left femoral neck, Garden I (S72.0) |
| V-1409 | Hilda Wellens | Orthopaedics | 2026-06-08 | 2026-06-20 | 12 | emergency | home_with_care | Septic arthritis of the right knee due to methicillin-sensitive Staphylococcus aureus, with sepsis |
| V-2019 | Tim Verhelst | General Surgery | 2026-06-08 | 2026-06-08 | 0 | elective | home | Subcutaneous lipoma of the back |
| V-1610 | Jens Hofmann | General Surgery | 2026-06-15 | 2026-06-16 | 1 | elective | home | Right indirect inguinal hernia |
| V-2007 | Hugo Delanghe | Ophthalmology | 2026-06-15 | 2026-06-15 | 0 | elective | home | Right age-related cataract |
| V-1910 | Raf Coenen | Gastroenterology | 2026-06-22 | 2026-06-25 | 3 | emergency | home | Iron-deficiency anaemia due to chronic blood loss from caecal angiodysplasia |
