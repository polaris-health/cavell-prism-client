# Cavell Prism Client

Python client for turning clinical data into FHIR: **clinical notes** extracted
by Prism's models, and **structured lab results** mapped deterministically with
no LLM involved.

**Cavell never connects to your FHIR server.** Your system sends clinical text
to Cavell, receives extracted resources back, and persists them locally with
your own credentials. Cavell has no access to your database.

## Installation

```bash
uv add cavell-prism-client
# or: pip install cavell-prism-client
```

The import name is `cavell_client`.

## FHIR Server

The SDK reads from and writes to your local FHIR server. If you do not already have one running, this repository includes a `docker-compose.yml` that starts [HAPI FHIR](https://hapifhir.io/) with Postgres:

```bash
scripts/start_fhir.sh            # docker compose up + wait until ready
scripts/start_fhir.sh --fresh    # wipe the database first, start empty
```

This starts HAPI on `http://localhost:8090` and exposes the FHIR API at `/fhir`. To see what's on the server, `uv run python scripts/fhir_summary.py` prints a count per resource type.

```python
client = CavellClient(
    ...,
    fhir_base_url="http://localhost:8090",
    fhir_api_path="/fhir",  # use the path exposed by your server
)
```

If your FHIR server uses OAuth2 client credentials, provide both `fhir_client_id` and `fhir_client_secret`. For unauthenticated servers, omit both.

You also need a **Prism API URL** (`https://prd.prism.cavell.app/api`) and an
**LLM Gateway key** — contact your Cavell representative for one.

---

## Ingest a CSV of clinical notes

Most real work starts from an export, so this is the shortest complete path
from a CSV to FHIR resources on your server.

```csv title="notes.csv"
note_id,patient_id,patient_name,note_date,encounter_id,practitioner_id,practitioner_name,note_text
note-0001,MRN-12345,John Doe,2024-01-15,V-001,DOC-001,Jane Smith,"Patient presents with..."
```

```python
import csv
from cavell_client import (
    CavellClient,
    IngestionPipeline,
    Organization,
    Patient,
    Practitioner,
    Document,
)

with open("notes.csv", newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

with CavellClient(
    api_url="https://prd.prism.cavell.app/api",
    api_key="your-llm-gateway-key",
    fhir_base_url="http://localhost:8090",
) as client:
    pipeline = IngestionPipeline(client, default_organization="CGH-001")

    # 1. Reference data — deduplicated from the same rows
    pipeline.seed(
        organizations=[Organization(identifier="CGH-001", name="City General")],
        patients=Patient.from_rows(
            rows,
            columns={"identifier": "patient_id", "name": "patient_name"},
            managing_organization="CGH-001",
        ),
        practitioners=Practitioner.from_rows(
            rows,
            columns={"identifier": "practitioner_id", "name": "practitioner_name"},
            organization_identifier="CGH-001",
        ),
    )

    # 2. One Document per row
    documents = Document.from_rows(
        rows,
        columns={
            "text": "note_text",
            "patient_identifier": "patient_id",
            "date": "note_date",
            "document_id": "note_id",
            "encounter_id": "encounter_id",
            "practitioner_identifier": "practitioner_id",
        },
    )

    # 3. Extract everything — globally date-sorted, batched, resume-safe
    for outcome in pipeline.extract_all(documents, batch_size=500):
        print(outcome)

    print(f"{pipeline.documents_processed} docs, ${pipeline.total_cost:.2f}")
```

`columns` maps **SDK field names to your headers**, so nothing in your export
has to be renamed. `extract_all()` sorts every document by date before
batching, so each patient's notes are extracted oldest-first; re-running skips
documents already processed.

## Ingest a CSV of lab results

Structured results do not need a model. The same row always produces the same
Observation, costs no tokens, and can be re-sent safely.

```csv title="lab_results.csv"
lab_result_id,patient_id,encounter_id,practitioner_id,test_name,loinc_code,value,unit,reference_low,reference_high,collected_datetime
LAB-0001,MRN-12345,V-001,DOC-001,C-reactive protein,1988-5,212,mg/L,,5.0,2024-01-15T21:40:00+01:00
```

```python
import csv
from cavell_client import CavellClient, LabResult, LabIngestionPipeline

with open("lab_results.csv", newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

with CavellClient(
    api_url="https://prd.prism.cavell.app/api",
    api_key="your-llm-gateway-key",
    fhir_base_url="http://localhost:8090",
) as client:
    results = LabResult.from_rows(
        rows,
        columns={
            "lab_result_id": "lab_result_id",
            "patient_identifier": "patient_id",
            "test_name": "test_name",
            "value": "value",
            "collected_datetime": "collected_datetime",
            "loinc_code": "loinc_code",
            "unit": "unit",
            "reference_low": "reference_low",
            "reference_high": "reference_high",
            "encounter_id": "encounter_id",
            "practitioner_id": "practitioner_id",
        },
    )

    outcome = LabIngestionPipeline(client).ingest(results)
    print(outcome)
    # 244/251 rows accepted (244 created, 0 already present), 7 rejected

    for r in outcome.rejected:
        print(f"row {r.index}  {r.lab_result_id}  [{r.stage}]  {r.reason}")
```

The patients, encounters and practitioners the rows reference must already
exist — lab ingestion never creates them. A row pointing at something unknown
is skipped and reported rather than silently dropped, and only the first five
`columns` entries above are required.

!!! tip "Column meanings, formats and rejection rules"
    The [CSV field reference](csv-reference.md) documents every column of both
    files: what it means, what it produces in FHIR, and exactly
    [when a row is rejected](csv-reference.md#when-a-lab-row-is-rejected).

---

## One record at a time

The same objects, built by hand — for a queue consumer, a webhook, or a quick
try without a file.

### A single clinical note

```python
from cavell_client import (
    CavellClient,
    IngestionPipeline,
    Organization,
    Patient,
    Document,
)

# Construction validates the key and both endpoints, raising on failure -
# CavellAuthError for a bad key, FHIRConnectionError for an unreachable
# FHIR server. No separate connection check is needed.
with CavellClient(
    api_url="https://prd.prism.cavell.app/api",
    api_key="your-llm-gateway-key",
    fhir_base_url="http://localhost:8090",
) as client:
    pipeline = IngestionPipeline(client, default_organization="CGH-001")

    pipeline.seed(
        organizations=[Organization(identifier="CGH-001", name="City General")],
        patients=[Patient(identifier="MRN-12345", managing_organization="CGH-001")],
    )

    for outcome in pipeline.extract(
        [
            Document(
                text="Patient presents with type 2 diabetes...",
                patient_identifier="MRN-12345",
                date="2024-01-15",
                document_id="note-001",
            ),
        ]
    ):
        print(outcome)

    print(f"Total: {pipeline.documents_processed} docs, ${pipeline.total_cost:.3f}")
```

### A single lab result

```python
from cavell_client import LabResult, LabIngestionPipeline

outcome = LabIngestionPipeline(client).ingest(
    [
        LabResult(
            lab_result_id="LAB-0001",  # your LIS accession — the idempotency key
            patient_identifier="MRN-12345",
            test_name="C-reactive protein",
            value="212",
            unit="mg/L",
            loinc_code="1988-5",
            reference_high=5.0,
            collected_datetime="2024-01-15T21:40:00+01:00",
        ),
    ]
)
```

---

## Clinical validation

Every resource extracted from a note carries an `unvalidated` meta tag. When a
clinician has reviewed one, remove the tag; any later update re-adds it:

```python
client.list_unvalidated_resources(patient_fhir_id, "Condition")  # review queue
client.mark_validated("Condition", condition_id)  # $meta-delete
```

Lab Observations carry no such tag — a structured feed is already a source of
truth.

## Where to go next

| Page | What is in it |
|------|---------------|
| [CSV field reference](csv-reference.md) | Every column of both file types, what it does, and the rejection rules |
| [Clinical notes pipeline](ingestion.md) | Seeding, chronological ordering, out-of-order notes, updates, error handling |
| [Lab results pipeline](labs.md) | Fail-closed reference checks, the rejection report, idempotent re-runs |
| [Client API](extract.md) | The lower-level client: direct extraction, resource queries, validation tags |
| [Demo notebooks](notebooks/extraction_demo.ipynb) | Runnable end-to-end walkthroughs on synthetic data |
