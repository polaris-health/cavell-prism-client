# Demo notebooks and datasets

## Data provenance — fully synthetic

**`notes.csv` and `hospitalizations.csv` contain no real patient data.**
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
| `hospitalization_extraction_demo.ipynb` | Hospital stays grouped per visit under FHIR Encounters, plus a walkthrough of the chronology check |
| `notes.csv` | 1,758 synthetic clinical notes across 104 patients |
| `hospitalizations.csv` | 104 synthetic documents across 17 hospital stays (16 patients), plus 5 held-back notes — 3 backdated for `MRN-20002` and 2 forward-dated for `MRN-20017` (`V-018`) — submitted together to demonstrate that only the out-of-order notes are refused |

## Running

1. Start the local FHIR server: `docker compose up -d` (HAPI on
   `http://localhost:8090`).
2. Set `CAVELL_API_URL` (your Prism deployment) and `CAVELL_API_KEY` (your
   LLM Gateway key) — the notebooks prompt for the key if unset.
3. Open a notebook and run top to bottom.
