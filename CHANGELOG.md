# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0] - Unreleased

First public release, targeting the Prism API. The package is published as
**`cavell-prism-client`**; the import name stays `cavell_client`.

### Breaking

- **Bearer auth replaces HTTP Basic.** `CavellAPI(base_url, api_key)` and
  `CavellClient(api_url, api_key, fhir_base_url, ...)` take an LLM Gateway
  key sent as `Authorization: Bearer <key>` on every request. Passing the
  removed `username=`/`password=` keyword arguments raises a `TypeError`
  with migration guidance. Positional callers of the old signature fail at
  FHIR-client construction — switch to keyword arguments.
- **Base URLs moved** to `https://{qa,stg,prd}.prism.cavell.app/api`. A bare
  host without a path gets `/api` appended automatically.
- **`check_connection()`** now pings the authenticated `GET /resources`
  route (`GET /tiers` is public on Prism and no longer proves anything).
  It verifies the URL, key presence, and reachability — not key validity;
  a bad key is caught by the first extraction call (~0.1s, no pipeline
  spend).
- **Run-global failures raise.** A 401 (`CavellAuthError`) or a 503 that
  survives in-place retries (`CavellGatewayUnavailableError`) aborts
  `IngestionPipeline.extract()` with an exception instead of producing
  per-document failure outcomes. Re-running with `skip_processed=True`
  (the default) resumes safely.

### Added

- `CavellAuthError` (401) and `CavellGatewayUnavailableError` (503), both
  subclassing `CavellAPIError`.
- `Retry-After` is honored in the 429 retry loop (capped at 300s).
- **Chronology guard**: documents older than the patient's newest
  already-persisted document warn, are flagged
  `IngestionOutcome.out_of_order`, and pass an update guard — updates to
  resources whose current version came from a newer document are dropped
  (creates always persist).
- `mark_validated(resource_type, id)` removes the Prism `unvalidated` meta
  tag via FHIR `$meta-delete`; `list_unvalidated_resources(patient, type)`
  lists the clinician review queue.
- `list_processed_document_ids(patient_id=...)` for patient-scoped resume.

### Changed

- `skip_processed` queries `DocumentReference`s per patient instead of
  scanning the whole server.
- Outbound extraction context is deflated: server bookkeeping (`meta`) and
  generated narrative (`text`) are stripped from context resources.
- Version is single-sourced from `cavell_client.__version__` (hatch dynamic
  versioning).

### Migration

```python
# before (0.1.x)
client = CavellClient(
    api_url="https://fhir-agent-demo.cavell.app/api",
    username="user",
    password="pass",
    fhir_base_url="http://localhost:8090",
)

# after (0.2.0)
client = CavellClient(
    api_url="https://prd.prism.cavell.app/api",
    api_key="<your LLM Gateway key>",
    fhir_base_url="http://localhost:8090",
)
```

Gateway keys are issued by the Cavell LLM Gateway, not Cavell accounts —
contact your Cavell representative if you don't have one.

## [0.1.0]

Internal release: ingestion pipeline (seed → extract → persist), CSV
helpers, practitioner matching, observation deduplication, transient-failure
retries with a deferred pass.
