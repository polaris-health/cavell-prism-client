# Cavell Prism Client

[![CI](https://github.com/polaris-health/cavell-prism-client/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/polaris-health/cavell-prism-client/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/cavell-prism-client)](https://pypi.org/project/cavell-prism-client/)
[![Python](https://img.shields.io/pypi/pyversions/cavell-prism-client)](https://pypi.org/project/cavell-prism-client/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Python client for [Cavell Prism](https://cavell.ai) — turn clinical data into
FHIR and persist it to your own FHIR server. Two paths:

- **Clinical notes** → FHIR resources, extracted by Prism's models
- **Structured lab results** → FHIR Observations, mapped deterministically with
  no LLM, no token cost, and idempotent re-runs

**Cavell never connects to your FHIR server.** Your system sends clinical
text to the Prism API, receives extracted resources back, and persists them
locally with your own credentials. Cavell has no access to your database and
stores no credentials: every request carries your own LLM Gateway key.

## Installation

```bash
pip install cavell-prism-client
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add cavell-prism-client
```

The import name is `cavell_client`.

You need a **Prism API URL** (`https://prd.prism.cavell.app/api`) and an
**LLM Gateway key** — contact your Cavell representative for a key. For a
local FHIR server, `docker compose up -d` in this repo starts HAPI on
`http://localhost:8090`.

## Ingest a CSV of clinical notes

Most work starts from an export, so this is the shortest complete path from a
CSV to FHIR resources on your server. `columns` maps **SDK field names to your
headers** — nothing in your file has to be renamed.

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
    api_key="<your LLM Gateway key>",
    fhir_base_url="http://localhost:8090",
) as client:
    pipeline = IngestionPipeline(client, default_organization="CGH-001")

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

    for outcome in pipeline.extract_all(documents, batch_size=500):
        print(outcome)
```

`extract_all()` sorts every document by date before batching, so each patient's
notes are extracted oldest-first even when they span batches. Extraction is
resume-safe (`skip_processed=True` by default), retries transient failures, and
aborts cleanly on auth/gateway outages. A note older than the patient's newest
already-extracted one is extracted against **split context**: it is shown the
record as it stood on its own date, with everything newer sent separately so
the model cannot read its own future as history. Such a note comes back with
`out_of_order=True` — a record of how it was processed, not a failure.

## Ingest a CSV of lab results

Structured results do not need a model. The same row always produces the same
Observation, spends no gateway tokens, and can be re-sent safely.

```python
import csv
from cavell_client import CavellClient, LabResult, LabIngestionPipeline

with open("lab_results.csv", newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

with CavellClient(
    api_url="https://prd.prism.cavell.app/api",
    api_key="<your LLM Gateway key>",
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

Only the first five `columns` entries are required. The patients, encounters
and practitioners the rows reference must already exist — lab ingestion never
creates them, and a row pointing at something unknown is skipped and reported
rather than silently dropped. Re-running the same file creates nothing twice:
every row is a conditional create keyed on your own `lab_result_id`.

**What every column means, and exactly when a row is rejected:**
[CSV field reference](https://polaris-health.github.io/cavell-prism-client/csv-reference/).

## One record at a time

The same objects built by hand — for a queue consumer, a webhook, or a quick
try without a file.

```python
from cavell_client import CavellClient, IngestionPipeline
from cavell_client import (
    Organization,
    Patient,
    Document,
    LabResult,
    LabIngestionPipeline,
)

with CavellClient(
    api_url="https://prd.prism.cavell.app/api",
    api_key="<your LLM Gateway key>",
    fhir_base_url="http://localhost:8090",
) as client:
    pipeline = IngestionPipeline(client, default_organization="CGH-001")

    pipeline.seed(
        organizations=[Organization(identifier="CGH-001", name="City General")],
        patients=[Patient(identifier="MRN-1", managing_organization="CGH-001")],
    )

    for outcome in pipeline.extract(
        [
            Document(
                text="Patient diagnosed with type 2 diabetes...",
                patient_identifier="MRN-1",
                date="2024-01-15",
                document_id="note-001",
            ),
        ]
    ):
        print(outcome)

    print(
        LabIngestionPipeline(client).ingest(
            [
                LabResult(
                    lab_result_id="LAB-0001",  # your LIS accession — the idempotency key
                    patient_identifier="MRN-1",
                    test_name="C-reactive protein",
                    value="212",
                    unit="mg/L",
                    loinc_code="1988-5",
                    reference_high=5.0,
                    collected_datetime="2024-01-15T21:40:00+01:00",
                ),
            ]
        )
    )
```

## Clinical validation

Every resource extracted from a note carries an `unvalidated` meta tag. When a
clinician has reviewed a resource, remove the tag; any later update re-adds it:

```python
client.list_unvalidated_resources(patient_fhir_id, "Condition")  # review queue
client.mark_validated("Condition", condition_id)  # $meta-delete
```

Lab Observations carry no such tag — a structured feed is already a source of
truth.

## Documentation

[polaris-health.github.io/cavell-prism-client](https://polaris-health.github.io/cavell-prism-client)

| Page | What is in it |
|------|---------------|
| [CSV field reference](https://polaris-health.github.io/cavell-prism-client/csv-reference/) | Every column of both file types, what it does, and the rejection rules |
| [Clinical notes pipeline](https://polaris-health.github.io/cavell-prism-client/ingestion/) | Seeding, chronological ordering, out-of-order notes, updates, error handling |
| [Lab results pipeline](https://polaris-health.github.io/cavell-prism-client/labs/) | Fail-closed reference checks, the rejection report, idempotent re-runs |
| [Client API](https://polaris-health.github.io/cavell-prism-client/extract/) | The lower-level client: direct extraction, resource queries, validation tags |

Runnable demo notebooks on synthetic data live in
[`docs/notebooks/`](docs/notebooks/README.md).

## Contributing & security

See [CONTRIBUTING.md](https://github.com/polaris-health/cavell-prism-client/blob/main/CONTRIBUTING.md) for development setup and the release
process, and [SECURITY.md](https://github.com/polaris-health/cavell-prism-client/blob/main/SECURITY.md) for how to report vulnerabilities.

## License

MIT
