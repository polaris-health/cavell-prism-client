# Demo notebooks and datasets

## Data provenance — fully synthetic

**`notes.csv`, `hospitalizations.csv` and `lab_results.csv` contain no real
patient data.**
Every name, birth date, identifier (MRN), practitioner, and clinical
narrative was generated for demonstration purposes. Any resemblance to real
persons is coincidental. The hospitalization dataset is deliberately curated
so its 17 stays cover a spread of clinical-coding scenarios (procedures,
complications, comorbidities, a readmission); see the notebook's dataset
table.

## Contents

| File | What it is |
|------|------------|
| `extraction_demo.ipynb` | End-to-end CSV → FHIR extraction with the ingestion pipeline |
| `hospitalization_extraction_demo.ipynb` | Hospital stays, each updating one FHIR Encounter document by document via `encounter_id`, plus a walkthrough of reverse-chronological ingestion against split context |
| `notes.csv` | 1,758 synthetic clinical notes across 104 patients |
| `hospitalizations.csv` | 108 synthetic documents across 17 hospital stays and 4 pre-admission outpatient reviews carrying prior medical history (16 patients), plus 8 held-back notes — 6 backdated for `MRN-20002` (`V-903` Nov 2022, `V-902` Sept 2023) and 2 forward-dated for `MRN-20017` (`V-018` July 2025) — submitted in one call to show only the backdated notes taking the split-context path |
| `lab_results_ingestion_demo.ipynb` | Structured lab results → FHIR Observations, deterministically (no LLM): fail-closed reference checks, a merged rejection report, and an idempotent re-run. **Run the hospitalization demo first** — the labs attach to its patients, practitioners and admissions |
| `lab_results.csv` | 251 synthetic lab rows for the hospitalization cohort: the structured twin of its prose lab notes plus pre-admission/post-discharge draws, rows without LOINC codes, comparator and qualitative values, and 7 deliberately broken rows for the rejection report |

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
