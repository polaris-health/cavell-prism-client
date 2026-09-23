# Demo notebooks and datasets

## Data provenance — fully synthetic

**`notes.csv`, `notes_lab_results.csv`, `hospitalizations.csv` and
`lab_results.csv` contain no real patient data.**
Every name, birth date, identifier (MRN), practitioner, and clinical
narrative was generated for demonstration purposes. Any resemblance to real
persons is coincidental. The hospitalization dataset is deliberately curated
so its 18 stays cover a spread of clinical-coding scenarios (procedures,
complications, comorbidities, a readmission, a paediatric stay decided by a
deployment's own standing orders); see the notebook's dataset table.

## Contents

| File | What it is |
|------|------------|
| `extraction_demo.ipynb` | End-to-end CSV → FHIR extraction with the ingestion pipeline |
| `hospitalization_extraction_demo.ipynb` | Hospital stays, each updating one FHIR Encounter document by document via `encounter_id`, plus a walkthrough of reverse-chronological ingestion against split context |
| `notes.csv` | The unified demo dataset for Prism and Atlas: 2,414 synthetic documents across 262 patients, 2017–2026 (cut-off 2026-06-30). It merges the outpatient histories of the original notes file (duplicates removed) with the 21 curated stays of `hospitalizations.csv` (same ids, lab-report documents replaced by lab rows) and adds 718 documents: 94 hospital stays, 20 day cases, 25 ED attendances and outpatient work-ups built for the Quality, MZG, Research, Medical-department and BI use cases |
| `notes_lab_results.csv` | 4,984 structured lab rows for `notes.csv`: the MZG rows of `lab_results.csv` (same `LAB-` ids, without the 7 deliberately broken ones), the results written inside the outpatient notes, and the labs of every new stay and visit |
| `demo_scenarios.md` | The answer key for demos on the unified dataset — readmissions, deaths, complications, process times, trial-eligibility verdicts for six fictional protocols, audit findings and BI tables |
| `hospitalizations.csv` | 116 synthetic documents across 18 hospital stays and 4 pre-admission outpatient reviews carrying prior medical history (17 patients), plus 8 held-back notes — 6 backdated for `MRN-20002` (`V-903` Nov 2022, `V-902` Sept 2023) and 2 forward-dated for `MRN-20017` (`V-018` July 2025) — submitted in one call to show only the backdated notes taking the split-context path |
| `lab_results_ingestion_demo.ipynb` | Structured lab results → FHIR Observations, deterministically (no LLM): fail-closed reference checks, a merged rejection report, and an idempotent re-run. **Run the hospitalization demo first** — the labs attach to its patients, practitioners and admissions |
| `lab_results.csv` | 284 synthetic lab rows for the hospitalization cohort: the structured twin of its prose lab notes plus pre-admission/post-discharge draws, rows without LOINC codes, comparator and qualitative values, and 7 deliberately broken rows for the rejection report |

Every column in these files is documented in the
[CSV field reference](../csv-reference.md) — what it means, what it produces
in FHIR, and which rules decide whether a row is accepted.

## Running

1. Start the local FHIR server: `docker compose up -d` (HAPI on
   `http://localhost:8090`).
2. Set `CAVELL_API_URL` (your Prism deployment) and `CAVELL_API_KEY` (your
   LLM Gateway key) — the notebooks prompt for the key if unset.
3. Open a notebook and run top to bottom.

`lab_results_ingestion_demo.ipynb` must run **after**
`hospitalization_extraction_demo.ipynb` on the same FHIR server: it only
attaches labs to patients, practitioners and admissions that already exist.

`notes_lab_results.csv` must likewise be ingested **after** `notes.csv` has been
extracted: its rows reference the patients, practitioners and encounters that
the notes create. `notes.csv` is the input of `extraction_demo.ipynb`; at
2,414 documents a full run is a substantial number of API calls, so filter to
the patients your demo needs (`demo_scenarios.md` lists them per use case).
`scripts/extract_demo_dataset.py` does the whole load unattended — fresh FHIR
database, seeding, every note, then the labs — with progress bars and an ETA.
It keeps existing FHIR data and resumes an interrupted run; `--fresh` wipes
the database and starts over.
