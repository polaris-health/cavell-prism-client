# Cavell Prism Client

Python client for extracting structured FHIR resources from clinical notes.

**Cavell never connects to your FHIR server.** Your system sends clinical text to Cavell, receives extracted resources back, and persists them locally with your own credentials. Cavell has no access to your database.

## Installation

```bash
uv add cavell-prism-client
# or: pip install cavell-prism-client
```

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

## Quick Start

Use the [pipeline](ingestion.md) to seed reference data and process documents in the correct order:

```python
from cavell_client import (
    CavellClient,
    IngestionPipeline,
    Organization,
    Patient,
    Document,
)

with CavellClient(
    api_url="https://prd.prism.cavell.app/api",
    api_key="your-llm-gateway-key",
    fhir_base_url="http://localhost:8090",
) as client:
    status = client.check_connection()
    if not status["fhir"]["ok"] or not status["cavell_api"]["ok"]:
        raise SystemExit(status)

    pipeline = IngestionPipeline(client, default_organization="CGH-001")

    pipeline.seed(
        organizations=[Organization(identifier="CGH-001", name="City General")],
        patients=[Patient(identifier="MRN-12345", managing_organization="CGH-001")],
    )

    for outcome in pipeline.extract(
        [
            Document(
                text="Patient presents with...",
                patient_identifier="MRN-12345",
                date="2024-01-15",
            ),
        ]
    ):
        print(f"Extracted {outcome.extract_result.count} resources")

    print(f"Total: {pipeline.documents_processed} docs, ${pipeline.total_cost:.3f}")
```
