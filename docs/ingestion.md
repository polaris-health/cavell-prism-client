# Pipeline

## Overview

The ingestion pipeline loads data in two steps between a hospital's FHIR server and the Cavell extraction API:

1. **Seed** — organizations, practitioners, and patients into FHIR (enforces correct reference ordering internally)
2. **Extract** — clinical documents, processed per patient in chronological order. Use `extract_all()` for a whole dataset (globally date-sorted, batched) or `extract()` for a single pass; see [Extract Options](#extract-options).

The pipeline resolves identifiers to IDs, so you work with business identifiers (patient IDs, facility codes, staff IDs) rather than FHIR server-assigned IDs.

Before each extraction, the SDK passes the patient, organization, and practitioner FHIR IDs as explicit API params, alongside any existing clinical resources as context. The API uses these real FHIR references on all extracted resources — no reference rewriting needed.

Practitioners are matched by the SDK after extraction — the Cavell API extracts practitioner names from text, and the SDK links them to seeded practitioners in FHIR.

## Data Flow

```
Seed: organizations + practitioners + patients → FHIR server
Extract: For each document:
         1. Fetch existing clinical resources from FHIR
         2. Send text + clinical context + reference IDs as params → Cavell API
         3. Match extracted practitioners to seeded ones
         4. Persist clinical resources with correct references → FHIR server
```

## Full Walkthrough

```python
from cavell_client import (
    CavellClient,
    IngestionPipeline,
    Organization,
    Practitioner,
    Patient,
    Document,
)

with CavellClient(
    api_url="https://prd.prism.cavell.app/api",
    api_key="your-llm-gateway-key",
    fhir_base_url="http://localhost:8090",
    fhir_client_id="...",  # optional; provide together with fhir_client_secret
    fhir_client_secret="...",  # optional; omit both for unauthenticated FHIR servers
) as client:
    # Connectivity is already verified: construction raises if the key is
    # rejected or either endpoint is unreachable.

    # Create pipeline with optional tier selection and concurrency
    pipeline = IngestionPipeline(
        client,
        tier="low",
        max_concurrency=10,
        default_organization="CGH-001",
    )

    # Seed organizations, practitioners, and patients
    pipeline.seed(
        organizations=[
            Organization(identifier="CGH-001", name="City General Hospital"),
            Organization(identifier="SMH-002", name="St. Mary's Hospital"),
        ],
        patients=[
            Patient(
                identifier="MRN-12345",
                name="John Doe",
                birth_date="1985-03-15",
                gender="male",
                managing_organization="CGH-001",
                general_practitioners=["DOC-001"],
            ),
            Patient(
                identifier="MRN-67890",
                managing_organization="SMH-002",
            ),
        ],
        practitioners=[
            Practitioner(
                identifier="DOC-001",
                family_name="Smith",
                given_name="Jane",
                organization_identifier="CGH-001",
            ),
            Practitioner(
                identifier="DOC-002",
                family_name="Jones",
                given_name="Bob",
                organization_identifier="SMH-002",
            ),
        ],
    )

    # Extract documents (skip_processed=True by default, so reruns are safe
    # when documents have stable document_id values)
    outcomes = pipeline.extract(
        [
            Document(
                text="Patient presents with type 2 diabetes...",
                patient_identifier="MRN-12345",
                date="2024-01-15",
                practitioner_identifier="DOC-001",
                document_id="note-001",
            ),
            Document(
                text="Follow-up: diabetes well controlled on metformin...",
                patient_identifier="MRN-12345",
                date="2024-04-15",
                document_id="note-002",
            ),
            Document(
                text="Patient reports chest pain...",
                patient_identifier="MRN-67890",
                date="2024-02-01",
                document_id="note-003",
                organization_identifier="SMH-002",  # overrides default
            ),
        ]
    )

    for outcome in outcomes:
        if outcome.success:
            print(
                f"[{outcome.patient_identifier}] "
                f"Extracted {outcome.extract_result.count} resources"
            )
        else:
            print(f"[{outcome.patient_identifier}] Error: {outcome.error}")
```

## Loading from CSV

All three data types provide a `from_rows()` classmethod that builds objects from CSV data (or any list of dicts). The `columns` dict maps SDK field names (left) to your CSV column headers (right). Keyword arguments are applied as literal defaults to every row.

```python
import csv
from cavell_client import Patient, Practitioner, Document

with open("notes.csv", newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

practitioners = Practitioner.from_rows(
    rows,
    columns={"identifier": "practitioner_id", "name": "practitioner_name"},
    organization_identifier="DEMO-HOSPITAL",
)

patients = Patient.from_rows(
    rows,
    columns={
        "identifier": "patient_id",
        "name": "patient_name",
        "birth_date": "birth_date",
        "gender": "gender",
        "general_practitioners": "practitioner_id",
    },
    managing_organization="DEMO-HOSPITAL",
)

documents = Document.from_rows(
    rows,
    columns={
        "text": "note_text",
        "patient_identifier": "patient_id",
        "date": "note_date",
        "document_id": "note_id",
        "visit_id": "visit_id",
        "practitioner_identifier": "practitioner_id",
        "meta": "department",
    },
    organization_identifier="DEMO-HOSPITAL",
)
```

`Patient.from_rows()` and `Practitioner.from_rows()` deduplicate by identifier (first occurrence wins) and skip rows with empty identifiers. `Document.from_rows()` creates one document per row, validates that `document_id` values are unique, and warns on short text (`< 20` characters). Its `columns` mapping must include `text`, `patient_identifier`, `date` and `document_id`; a row with a blank value for any of them raises rather than being silently coerced to `None`.

`Document.from_rows()` preserves CSV row order and does **not** sort by date. To
ingest the whole file, hand the list to
[`extract_all()`](#extract_all-the-whole-dataset), which sorts globally by date
before batching:

```python
pipeline.seed(organizations=[...], patients=patients, practitioners=practitioners)
outcomes = pipeline.extract_all(documents, batch_size=500)
```

`Practitioner.from_rows()` supports a virtual `"name"` column that auto-splits `"Given Family"` into `given_name` and `family_name`. Use either `"name"` or the individual `"given_name"`/`"family_name"` keys, not both.

All three validate upfront — unknown field names, missing CSV columns, and missing required keys raise `ValueError` before any objects are built.

## Helper Dataclasses

### Organization

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `identifier` | `str` | Yes | Facility code (e.g., `"CGH-001"`) |
| `name` | `str` | Yes | Display name for the extraction API |

### Practitioner

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `identifier` | `str` | Yes | Staff ID (e.g., `"DOC-001"`) |
| `family_name` | `str` | Yes | Family name |
| `given_name` | `str` | Yes | Given name |
| `organization_identifier` | `str` | Yes | Organization identifier (must match a seeded org) |
| `specialty` | `str` | No | Clinical specialty (written to `PractitionerRole.specialty`) |

### Patient

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `identifier` | `str` | Yes | Patient identifier (e.g., MRN) |
| `name` | `str` | No | Patient name |
| `birth_date` | `str` | No | ISO date (e.g., `"1990-01-15"`) |
| `gender` | `str` | No | FHIR gender code |
| `managing_organization` | `str` | No | Organization identifier |
| `general_practitioners` | `str` or `list[str]` | No | Practitioner identifier(s); a single string is normalized to a list |

### Document

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | `str` | Yes | Clinical text |
| `patient_identifier` | `str` | Yes | Patient identifier (e.g., MRN) |
| `date` | `str` or `date` | Yes | ISO date (`"2024-01-15"`) or `datetime.date`, normalized to `YYYY-MM-DD` on construction. Drives chronological ordering, and is sent to the API as the `document_date` payload field |
| `document_id` | `str` | Yes | Your identifier for this document, stamped on the DocumentReference. Keyword-only. Everything that makes re-running safe keys on it — the resume filter, the chronology watermark, and failure reporting |
| `organization_identifier` | `str` | No | Org identifier — falls back to `default_organization` if omitted |
| `meta` | `str` | No | Extra context for the extraction API (e.g. department, ward). **Do not include the document date or practitioner** — the date is sent as its own `document_date` payload field and the practitioner is injected automatically (see [Meta Assembly](#meta-assembly)). |
| `practitioner_identifier` | `str` | No | If provided, the SDK resolves this to a FHIR ID (passed as a param) and injects the practitioner's name into `meta`, improving matching precision |
| `visit_id` | `str` | No | Visit/admission identifier stamped on the Encounter resource — groups notes by hospital visit |

Field validation and normalization happen in one place: building a `Document`
is what checks it. `extract()` and `extract_all()` add only a type check —
passing raw CSV rows straight through raises `TypeError` naming the offending
positions, rather than failing inside a worker thread mid-run. It runs before
the API pre-flight, so a malformed list costs not even one request.

### IngestionOutcome

Returned by `pipeline.extract()` for each document.

| Field | Type | Description |
|-------|------|-------------|
| `success` | `bool` | Whether extraction and persistence succeeded |
| `patient_identifier` | `str` | Patient identifier from the document |
| `document_index` | `int` | Position in the original input list |
| `document_id` | `str` or `None` | The `document_id` from the source `Document`, if set — useful for correlating failures back to source records |
| `extract_result` | `ExtractResult` or `None` | Result if successful |
| `error` | `str` or `None` | Error message if failed |
| `transient` | `bool` | The failure was transient (timeout, connection drop, server 5xx/429) — the deferred retry pass re-ran or will re-run it (see [Document extraction failures](#document-extraction-failures)) |
| `out_of_order` | `bool` | The document was [refused](#out-of-order-documents-are-refused) for predating its patient's newest already-persisted document. Always paired with `success=False`; nothing was extracted or persisted for it and no tokens were spent |

Note on naming: `Document.meta` is the SDK's free-text supplementary context
for the extraction model. It is unrelated to FHIR `Resource.meta`, the
server-side element that carries version, provenance, and validation tags.

## Practitioner Matching

After extraction, the SDK matches practitioners found in the text to those seeded via `seed()`:

1. **1 match** — the reference is linked to the seeded practitioner.
2. **0 matches** — the reference is removed and a warning is logged.
3. **>1 matches** — the match is treated as ambiguous, the reference is removed, and a warning is logged.

Matching uses identifier first (exact match), then falls back to family name + given name scoped to the document's organization.

Practitioner resources are always removed from the persisted bundle — only clinical resources (Conditions, MedicationRequests, etc.) are written to FHIR with correct practitioner references.

## Ordering and Dates

Every document requires a date, and documents reach the API in ascending date
order per patient. This ordering determines correct behavior -- see [How
Updates Work](#how-updates-work).

Sorting happens at two levels:

- `extract_all()` sorts the **entire dataset** by ascending date before
  batching, so batch boundaries are chronologically clean.
- `extract()` sorts **within each patient** in the pass it was given. This is
  the safety net for calling `extract()` directly, and a no-op on input
  `extract_all()` has already ordered.

Both sorts are stable, so same-day documents keep the order you passed them in.

Neither sorts your input list in place, and neither sorts the *dataset* when
you call `extract()` yourself — if you batch by hand with `extract(..., limit=N)`,
ordering across those calls is yours to get right. Use `extract_all()` and it
is handled.

### Out-of-order documents are refused

Sorting only orders the documents *within one run*. If a document is older
than data already persisted for that patient (from an earlier run), you are
going backwards in time — and the pipeline **refuses to extract it**.

The check runs before anything in the call is extracted. It compares each
document against **its own patient's** watermark (the date of the newest
already-processed document for that patient) and drops the ones that are
older. Nothing is extracted or persisted for a refused document and no tokens
are spent on it, but it still gets an outcome — no exception is raised, and
refusal is scoped to the offending document.

```python
outcomes = pipeline.extract_all(documents, batch_size=500)

for o in outcomes:
    if o.out_of_order:
        print(o.error)
        # note-9001 (patient MRN-20002) dated 2023-09-12 is older than 2024-04-09
```

Everything else in the call is extracted as normal — other patients' documents,
and the *same* patient's forward-dated documents. A single backdated note
therefore costs exactly that note.

**Why refuse rather than merge?** Extraction is context-aware: each note is
read against the resources its predecessors produced (see [Meta
Assembly](#meta-assembly)). Context is always the patient's *current* state
with no date filtering, so an older note would be interpreted against a
clinical picture from its own future — and anything it created would carry
that contamination. Refusing is the conservative position while that is true.

**To ingest documents you have out of order**, either:

- Leave them refused, if the newer data already supersedes them — you no
  longer have to pull them out of the batch by hand; or
- Delete the patient's data with `client.delete_patient_resources(...)`,
  re-seed, and re-extract the whole timeline in date order. `extract_all()`
  handles the ordering.

Caveats:

- Dates are truncated to the day, so two same-day documents have no defined
  order and are **not** a violation. Process same-day documents in one run
  (the input order is preserved for equal dates).
- The check **fails open per patient**: if the watermark query errors, that
  patient is left unchecked with a warning rather than failing the run. A
  transient FHIR error should not block ingestion.
- Documents already processed are filtered out by `skip_processed` *before*
  the check, so re-running a completed batch never trips it.
- Refusals count toward `pipeline.documents_failed`, not `documents_processed`,
  and contribute nothing to `pipeline.total_cost`.

Step 10 of `docs/notebooks/hospitalization_extraction_demo.ipynb` demonstrates
this end-to-end: it holds back five notes — three from an earlier admission for
one patient, two from a later readmission for another — extracts the rest, then
submits all five in one call and shows the three older ones refused while the
two newer ones are extracted.

!!! note "Provisional"

    Refusing is a deliberate interim position. An earlier design extracted the
    older document anyway behind an **update guard** that dropped updates to
    resources sourced from newer documents. That code is retained but disabled
    (`_apply_update_guard`, and the commented-out block in
    `_process_single_document`) in case the policy is revisited.

## Meta Assembly

Before each extraction call, the pipeline builds the `meta` string sent to the API by combining up to two parts in order:

1. **Your `meta` value** (if set) — e.g. `Department: Cardiology`
2. **Attending practitioner** (if `practitioner_identifier` is set) — e.g. `Attending: Jane Smith (DOC-001)`

For example, a document with `meta="Department: Cardiology"` and `practitioner_identifier="DOC-001"` produces:

```
Department: Cardiology
Attending: Jane Smith (DOC-001)
```

When there is nothing to say — no `meta`, no practitioner — the field is omitted from the payload entirely rather than sent empty.

The **document date is not part of `meta`**. It travels as its own `document_date` payload field, normalized to `YYYY-MM-DD`:

```json
{
  "text": "...",
  "document_date": "2024-01-15",
  "meta": "Department: Cardiology\nAttending: Jane Smith (DOC-001)",
  "document_identifier": "note-001"
}
```

Because the pipeline sends the date separately and injects the practitioner, **do not duplicate either** in your `meta` field — that would confuse the extraction model.

## Error Handling

### Seed failures (fail-fast)

If seeding fails, the pipeline raises `RuntimeError` immediately. Resources already written to the FHIR server stay there, but re-running is safe — seeding uses upsert, so it picks up where it left off.

```python
try:
    pipeline.seed(organizations=[...], patients=[...])
except RuntimeError as e:
    print(f"Seeding failed: {e}")
```

### Cross-validation failures

If you reference an unknown identifier, `seed()` raises `ValueError` before making any FHIR calls:

```python
# This raises ValueError because "UNKNOWN-ORG" was never provided
pipeline.seed(
    organizations=[Organization(identifier="CGH-001", name="City General")],
    patients=[Patient(identifier="MRN-1", managing_organization="UNKNOWN-ORG")],
)
```

### Document extraction failures

When extraction fails for a document, the pipeline:

1. Returns a failed `IngestionOutcome` for that document.
2. Skips remaining documents for that patient, because subsequent context would be incomplete.
3. Continues processing other patients.

```python
for outcome in pipeline.extract(documents):
    if not outcome.success:
        print(f"Failed: {outcome.error}")
        # Other patients' documents still process
```

**Transient failures are retried.** Timeouts, connection drops, and server
5xx/429 responses are retried in place (3 attempts with backoff). If a
document still fails transiently, its outcome is marked `transient=True` and,
after all patients finish, a **deferred pass** re-runs every failed document
of the affected patients once, in date order. Context is re-fetched fresh, so
this is safe — a failed document persists nothing. Deterministic failures
(rejected bundles, 4xx content errors) are not retried.

### Run aborts

Two failures are run-global — no document can succeed until they are fixed —
so instead of failing every document one by one, `extract()` raises:

- **`CavellAuthError` (401)**: the key is missing, expired, or rejected.
  Checked once cheaply before any processing; a mid-run 401 stops all
  patients after the first failure.
- **`CavellGatewayUnavailableError` (503)**: the LLM Gateway is unreachable
  from the server. In-place retries still run (a blip should not abort), but
  if a document exhausts them the run stops.

Aborting is safe: failed documents persist nothing, and re-running
`extract()` with `skip_processed=True` (the default) resumes where the run
stopped.

### Persistence failures

The pipeline treats these the same as extraction failures: it skips remaining documents for that patient.

## How Updates Work

Before each document, the SDK fetches existing clinical resources from the FHIR server and passes them as context to the extraction API. The API uses this context to POST a new resource or PUT an update to an existing one.

The context covers Conditions, AllergyIntolerances, Procedures, MedicationRequests, the 50 most recent Observations, ResearchSubjects, and **active CarePlans** — so care plans that continue across notes are updated and versioned instead of duplicated (a plan that ends is marked completed/revoked and drops out of the context) — plus all **active ResearchStudy** resources, which are not patient-scoped and always included for deduplication.

Chronological ordering matters:

1. Doc 1 mentions "type 2 diabetes" → API creates a new Condition resource
2. SDK persists the Condition to FHIR
3. Before Doc 2, SDK fetches context again -- the Condition now exists
4. Doc 2 mentions "diabetes well controlled" → API sees the existing Condition and updates it rather than creating a duplicate

### Validation tags and updates

Every resource the extraction API emits carries an `unvalidated` meta tag
(see [Mark a resource validated](extract.md#mark-a-resource-validated)). By
the server contract, any update to a resource re-adds the tag — validation
applies to a specific version.

Two client-side behaviors soften this in practice:

- **Duplicate suppression**: observations that already exist (same date,
  code, and value) are dropped client-side before persisting, so
  re-extracting the same data does not touch validated resources.
- **Chronological refusal**: an out-of-order document cannot overwrite (and
  thus cannot un-validate) anything, because it is [never extracted in the
  first place](#out-of-order-documents-are-refused).

An in-order update with genuinely new information *does* replace the resource
and re-marks it `unvalidated` — by design, since a clinician has not seen the
new version.

## Connection Check

**You do not need to check the connection — `CavellClient` already did.** Constructing it validates both halves of your configuration and raises on the first failure, so nothing is left to verify before `seed()` or `extract()`:

| Check | Request | Raises on failure |
|-------|---------|-------------------|
| Cavell API key | `GET /key/info` — pre-flights your LLM Gateway key against the gateway, no tokens spent | `CavellAuthError` (rejected key), `CavellGatewayUnavailableError` (gateway down), `CavellAPIError` (URL doesn't serve the route) |
| FHIR server | `GET /metadata` | `FHIRAuthError` (OAuth2 handshake failed), `FHIRConnectionError` (unreachable or wrong URL) |

The exception type tells you which half is misconfigured:

```python
CavellClient(api_url=..., api_key="sk-typo", fhir_base_url=...)  # pragma: allowlist secret
# CavellAuthError: Cavell API error (401): LLM Gateway rejected the provided key.

CavellClient(api_url=..., api_key=<valid>, fhir_base_url="http://localhost:9999")
# FHIRConnectionError: FHIR server unreachable: http://localhost:9999 (...)
```

Neither check can be skipped: the Prism API is always remote, so a configuration that can't be validated is an error rather than a supported mode. `extract()` re-runs the key pre-flight once per run, so a key that expires between runs never reaches the pipeline.

`client.check_connection()` remains available for re-checking a long-lived client (say, before a new batch hours later). Unlike the constructor it *reports* rather than raises, and always checks both services even if one fails:

```python
status = client.check_connection()
# {'fhir': {'ok': True}, 'cavell_api': {'ok': True}}
```

Each key contains `ok` (bool) and, on failure, `error` (str).

## Pipeline Options

```python
pipeline = IngestionPipeline(
    client,
    tier="low",
    max_concurrency=10,
    default_organization="CGH-001",
)
```

| Option | Default | Description |
|--------|---------|-------------|
| `tier` | `None` | Model tier to use for extraction (low/medium/high) |
| `max_concurrency` | `5` | How many patients are processed in parallel |
| `default_organization` | `None` | Org identifier used when `Document.organization_identifier` is omitted |

## Extract Options

Two entry points, and the difference matters for large datasets:

| Method | Scope | Ordering |
|--------|-------|----------|
| `extract_all(documents, batch_size=N)` | Every document, in batches of `N` | Sorts the **whole dataset** by ascending date first |
| `extract(documents, limit=N)` | **One** pass, capped at `N` documents | Sorts by date **within each patient** in that pass |

### `extract_all()` — the whole dataset

Use this for anything you would describe as "ingest this CSV". It sorts every
document by ascending date, splits the sorted list into batches, and calls
`extract()` once per batch.

```python
outcomes = pipeline.extract_all(
    documents,
    batch_size=500,  # documents per extract() call; None = one call
    skip_processed=True,  # applied per batch
    on_batch=lambda batch: print(f"{len(batch)} done"),  # optional progress
)
```

| Option | Default | Description |
|--------|---------|-------------|
| `batch_size` | `None` | Documents per `extract()` call. `None` processes everything in one call. |
| `skip_processed` | `True` | Passed to `extract()`, which applies it per batch. |
| `on_batch` | `None` | Called with each batch's outcomes as that batch finishes. Use it for progress output or to persist partial results — a large run otherwise holds every outcome, including extracted bundles, in memory until it returns. |

The global sort is what makes batching safe. Batching an *unsorted* list splits
it by input order, so a later batch could carry documents older than what an
earlier batch already persisted — and those would be
[refused](#out-of-order-documents-are-refused) rather than extracted, silently
losing them. Sorting first puts every batch boundary on a clean chronological
cut, which also means each patient's backdated documents are checked in an
earlier batch than their forward-dated ones.

Batches are cut by index, so the walk always terminates — it does not rely on
`skip_processed` to advance, and works with `skip_processed=False` too.

All documents are validated *before* the first batch runs, so a bad reference
late in the list surfaces before earlier batches spend anything. This is also
the only place `document_id` uniqueness is checked across the whole dataset:
`extract()` sees one batch at a time, so a duplicate pair split across two
batches would otherwise slip through.

Prefer larger batches. Each `extract()` call issues one FHIR query per distinct
patient in that batch, so halving `batch_size` roughly doubles the query
overhead. Batching bounds how much work an interruption loses; 500 bounds that
about as usefully as 50 while doing a tenth of the chatter.

### `extract()` — a single pass

```python
for outcome in pipeline.extract(
    documents,
    skip_processed=True,  # default: query FHIR and skip already-processed docs
    limit=10,  # optional: cap this call, e.g. a sanity check before a full run
):
    ...
```

| Option | Default | Description |
|--------|---------|-------------|
| `skip_processed` | `True` | Query FHIR for already-processed document IDs and skip them. Set to `False` to process all documents. |
| `limit` | `None` | Cap the number of documents processed in **this call**, taken from the front of the list after `skip_processed` filtering. `None` processes everything passed. |

`limit` truncates a single pass; it does not chunk. `extract(documents, limit=500)`
on a 2000-document list processes 500 and returns — the other 1500 are
untouched. Reach for `extract_all()` when you want all of them.

!!! note "Renamed in 0.2.0"

    `limit` was called `batch_size`, a name that implied chunking it never
    did. Passing `batch_size=` to `extract()` raises `TypeError` with
    migration guidance rather than being silently ignored, since ignoring it
    would extract every document you passed.

When `skip_processed=True`, re-running `extract()` with the same document list is safe as long as documents have stable `document_id` values — reuse the identifier from your source system rather than generating a fresh one per run, or every run re-extracts everything and duplicates its resources.

### Cumulative statistics

The pipeline tracks totals across all `extract()` calls:

| Property | Type | Description |
|----------|------|-------------|
| `documents_processed` | `int` | Documents successfully extracted |
| `documents_failed` | `int` | Documents that failed extraction |
| `total_cost` | `float` | Estimated cost in USD |

```python
for outcome in pipeline.extract(documents):
    print(outcome)

print(f"{pipeline.documents_processed} succeeded, {pipeline.documents_failed} failed")
print(f"Total cost: ${pipeline.total_cost:.3f}")
```

Statistics are fully updated when `extract()` returns. They accumulate if you call `extract()` multiple times on the same pipeline.

## Timeouts

| Client | Default | Notes |
|--------|---------|-------|
| Cavell API | 800s | Extraction involves many LLM calls; the API caps each call at ~300s |
| FHIR server | 30s | Per-request timeout for all FHIR operations |

### Deleted patient detection

Before making any extraction API calls, `extract()` verifies that all referenced patients still exist on the FHIR server. If a patient was deleted (HAPI returns 404 or 410 Gone), the pipeline raises `RuntimeError` immediately — before spending on API calls:

```python
RuntimeError: Patient 'MRN-12345' (FHIR id 1007) no longer exists
on the FHIR server — re-run seed() to restore it
```

This catches the common mistake of deleting a patient and forgetting to re-seed before extracting.

## Deleting and Redoing a Patient

If a patient's extracted data looks wrong, delete all their resources and re-extract. Cascade delete removes the patient and everything referencing them — organizations and practitioners are unaffected.

```python
# Delete
fhir_id = client.find_patient_id("MRN-12345")
client.delete_patient_resources(fhir_id)

# Re-extract: create a fresh pipeline and re-seed (recreates the deleted patient),
# then extract. skip_processed=True (default) means only the deleted patient's
# docs are re-processed.
pipeline = IngestionPipeline(client, tier="low", default_organization="CGH-001")
pipeline.seed(organizations=[...], patients=[...], practitioners=[...])

for outcome in pipeline.extract_all(all_documents, batch_size=500):
    ...
```

**Important:** You must create a new pipeline and re-run `seed()` after deleting a patient. If you try to extract with the old pipeline, `extract()` will detect that the patient no longer exists and raise `RuntimeError`.

Requires `allow_cascading_deletes=true` in HAPI config (set in the repo's docker-compose).

## Multi-Organization

Documents from different facilities for the same patient work without extra configuration:

```python
for outcome in pipeline.extract([
    Document(
        text="Initial visit at City General...",
        patient_identifier="MRN-12345",
        date="2024-01-15",
        document_id="note-001",
        organization_identifier="CGH-001",
    ),
    Document(
        text="Transfer to St. Mary's...",
        patient_identifier="MRN-12345",
        date="2024-02-01",
        document_id="note-002",
        organization_identifier="SMH-002",
    ),
])
```
