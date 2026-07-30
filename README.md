# Cavell Prism Client

[![CI](https://github.com/polaris-health/cavell-prism-client/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/polaris-health/cavell-prism-client/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/cavell-prism-client)](https://pypi.org/project/cavell-prism-client/)
[![Python](https://img.shields.io/pypi/pyversions/cavell-prism-client)](https://pypi.org/project/cavell-prism-client/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Python client for [Cavell Prism](https://cavell.ai) — extract structured
FHIR resources from clinical notes and persist them to your own FHIR server.

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

## Quickstart

You need a **Prism API URL** (`https://prd.prism.cavell.app/api`) and an
**LLM Gateway key** — contact your Cavell representative for a key. For a
local FHIR server, `docker compose up -d` in this repo starts HAPI on
`http://localhost:8090`.

```python
from cavell_client import CavellClient, IngestionPipeline
from cavell_client import Organization, Patient, Document

with CavellClient(
    api_url="https://prd.prism.cavell.app/api",
    api_key="<your LLM Gateway key>",
    fhir_base_url="http://localhost:8090",
) as client:
    pipeline = IngestionPipeline(client, default_organization="CGH-001")

    # 1. Seed reference data and patients
    pipeline.seed(
        organizations=[Organization(identifier="CGH-001", name="City General")],
        patients=[Patient(identifier="MRN-1", managing_organization="CGH-001")],
    )

    # 2. Extract clinical notes (per patient, in date order) and persist
    outcomes = pipeline.extract(
        [
            Document(
                text="Patient diagnosed with type 2 diabetes...",
                patient_identifier="MRN-1",
                date="2024-01-15",
                document_id="note-001",
            ),
        ]
    )
    for outcome in outcomes:
        print(outcome)
```

Extraction is resume-safe (`skip_processed=True` by default), retries
transient failures, warns on out-of-order documents, and aborts cleanly on
auth/gateway outages.

## Clinical validation

Every extracted resource carries an `unvalidated` meta tag. When a clinician
has reviewed a resource, remove the tag; any later update re-adds it:

```python
client.list_unvalidated_resources(patient_fhir_id, "Condition")  # review queue
client.mark_validated("Condition", condition_id)  # $meta-delete
```

## Documentation

Setup guides, the pipeline walkthrough, and demo notebooks (synthetic data):
[polaris-health.github.io/cavell-prism-client](https://polaris-health.github.io/cavell-prism-client)

## Contributing & security

See [CONTRIBUTING.md](https://github.com/polaris-health/cavell-prism-client/blob/main/CONTRIBUTING.md) for development setup and the release
process, and [SECURITY.md](https://github.com/polaris-health/cavell-prism-client/blob/main/SECURITY.md) for how to report vulnerabilities.

## License

MIT
