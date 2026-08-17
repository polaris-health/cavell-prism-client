# Client

## Setup

```python
from cavell_client import CavellClient, IngestionPipeline

client = CavellClient(
    api_url="https://prd.prism.cavell.app/api",
    api_key="your-llm-gateway-key",
    fhir_base_url="http://localhost:8090",
    fhir_client_id="...",  # optional; provide together with fhir_client_secret
    fhir_client_secret="...",  # optional; omit both for unauthenticated FHIR servers
    fhir_api_path="/fhir",  # use the path exposed by your FHIR server
)
```

Use the client as a context manager so both underlying HTTP clients are closed cleanly:

```python
with CavellClient(...) as client:
    pipeline = IngestionPipeline(client, default_organization="ORG-1")
    ...
```

## Methods

### List tiers

```python
tiers = client.list_tiers()
for t in tiers:
    print(f"{t['name']} (default={t['default']})")
```

Returns a `list[dict]` with the keys `name` and `default`.

### Get patient resources

```python
# All resources for a patient
resources = client.get_patient_resources("pat-123")

# Filter by type
conditions = client.get_patient_resources("pat-123", "Condition")
```

### Delete patient resources

Cascade-deletes the patient and any resources that reference that patient. This depends on server-side cascading delete support. The repository's local HAPI configuration enables it with `allow_cascading_deletes=true`.

```python
client.delete_patient_resources("pat-123")
```

### Count resources

```python
n = client.count_resources("Patient")
```

### Find patient ID

```python
fhir_id = client.find_patient_id("MRN-12345")
```

### List processed document IDs

```python
processed = client.list_processed_document_ids()
# or scoped to one patient (preferred on shared servers):
processed = client.list_processed_document_ids(patient_id=fhir_id)
```

Returns a `set[str]` of `DocumentReference.identifier` values used by the pipeline for resume-safe extraction. Without `patient_id` the whole server is scanned.

### Mark a resource validated

The extraction API stamps every resource it emits with an `unvalidated` meta
tag (system `<deployment-url>/fhir/CodeSystem/validation-status`). The
consumer contract for "a clinician has reviewed this resource" is removing
that tag:

```python
client.mark_validated("Condition", condition_id)  # True if a tag was removed
```

Any later extraction that updates the resource re-adds the tag.

### List unvalidated resources

```python
pending = client.list_unvalidated_resources(patient_fhir_id, "Condition")
```

Returns the patient's resources still carrying the `unvalidated` tag —
the review queue for clinical validation.

### Direct extraction API

The pipeline is the supported path, but `CavellAPI` can be used directly for
one-off extraction without FHIR persistence:

```python
from cavell_client.api import CavellAPI

api = CavellAPI("https://prd.prism.cavell.app/api", api_key="...")
bundle, count, usage = api.extract(
    text=note_text,
    document_date="2024-01-15",  # ISO YYYY-MM-DD; its own field, not meta prose
    meta="Department: Cardiology",
    tier="high",
    allowed_resources=["Condition", "MedicationRequest"],  # restrict output types
)
```

`allowed_resources` restricts extraction to the listed FHIR resource types.
It is not available through the pipeline, which assumes full extraction for
its context and deduplication behavior.

#### Extracting a document out of chronological order

If you are extracting a note that predates data already on record, pass the
record as it stood on that note's date as `context`, everything newer as
`future_context`, and set `out_of_order`:

```python
from cavell_client.fhir import FHIRClient

fhir = FHIRClient(base_url="...", client_id="...", client_secret="...")
past, future = fhir.fetch_split_patient_context(
    patient_fhir_id, reference_date="2023-09-12"
)

bundle, count, usage = api.extract(
    text=note_text,
    document_date="2023-09-12",
    patient_id=patient_fhir_id,
    context=past,
    future_context=future,
    out_of_order=True,
)
```

`fetch_split_patient_context` sorts each resource by **provenance** — the newest
already-processed document that created or updated it — so `past` is what was
genuinely on record on that date, not merely what carries an older clinical date.

Without this, the note is read against a clinical picture from its own future
and anything it creates carries that contamination backwards. Both fields are
omitted from the payload when unset, so ordinary forward extraction is
unchanged. The pipeline does all of this for you — see
[Out-of-order documents](ingestion.md#out-of-order-documents-get-split-context).

!!! warning "Requires matching API support"

    An extraction API that predates these fields ignores them silently, which
    would leave the note extracting against past-only context with nothing to
    reconcile against.

## Response Types

### ExtractResult

Returned on successful pipeline outcomes.

| Field | Type | Description |
|-------|------|-------------|
| `bundle` | `dict` | FHIR transaction bundle |
| `resources` | `list[dict]` | Extracted FHIR resources (shorthand for bundle entries) |
| `count` | `int` | Number of extracted resources |
| `patient_id` | `str` | Resolved patient ID |
| `usage` | `UsageStats` or `None` | Token usage and cost |
| `persistence` | `PersistResult` or `None` | Persistence result |

### PersistResult

| Field | Type | Description |
|-------|------|-------------|
| `status` | `str` | `"success"`, `"partial_failure"`, or `"failed"` |
| `created` | `int` | Resources created |
| `updated` | `int` | Resources updated |
| `errors` | `list[dict]` | Error details for failed resources |

### UsageStats

| Field | Type | Description |
|-------|------|-------------|
| `input_tokens` | `int` | Input tokens consumed |
| `output_tokens` | `int` | Output tokens generated |
| `total_tokens` | `int` | Total tokens |
| `requests` | `int` | Number of API requests |
| `estimated_cost` | `float` | Estimated cost in USD |

## Timeouts

| Client | Default | Notes |
|--------|---------|-------|
| Cavell API | 800s | Extraction involves many LLM calls; the API caps each call at ~300s |
| FHIR server | 30s | Per-request timeout for all FHIR operations |

## Exceptions

| Exception | When |
|-----------|------|
| `CavellAPIError` | Cavell API returns an error |
| `CavellAuthError` | The API returns 401 — the LLM Gateway key is missing or rejected (subclass of `CavellAPIError`, non-retryable) |
| `CavellGatewayUnavailableError` | The API returns 503 — the LLM Gateway is unreachable from the server (subclass of `CavellAPIError`) |
| `PatientNotFoundError` | A `patient_id` was provided but does not exist on the FHIR server |
| `FHIRAuthError` | FHIR OAuth2 authentication fails |
| `FHIRConnectionError` | The FHIR server is unreachable or `fhir_base_url` is wrong |
| `CavellError` | Base class for library-specific exceptions |

```python
from cavell_client import CavellAPIError

try:
    ...
except CavellAPIError as e:
    print(f"API error ({e.status_code}): {e.message}")
```
