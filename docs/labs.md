# Lab Results Pipeline

## Overview

Structured lab results — a CSV from your LIS, a query result, a dataframe —
become FHIR Observations **without an LLM**. Value, unit, reference range,
collection time, patient and encounter are already machine-readable; sending
them through a language model would only add cost and a chance of being wrong.

What that buys you:

- **No token cost.** The endpoint spends nothing on the gateway.
- **Deterministic.** The same file always produces the same resources.
- **Idempotent.** Every row is a conditional create keyed on your own result
  id, so re-running a feed after a partial run creates nothing twice.
- **Skip-and-report.** A bad row is skipped with a reason; the rest still land.

The pipeline never invents references. Patients, encounters and practitioners
must already exist on your FHIR server — seeded by you, or created by
[document extraction](ingestion.md). Rows pointing at anything unknown are
rejected rather than created for.

## Data Flow

```
1. Validate every row's content, once over the whole input      (no network)

2. Then, for each patient (sorted, deterministic):
     a. Resolve patient / encounter / practitioner against FHIR  (fail-closed)
     b. Send surviving rows → Prism /ingest/lab-results → Observation bundle
     c. Persist the bundle → your FHIR server
```

## Ingesting a CSV

```python
import csv
from cavell_client import CavellClient, LabResult, LabIngestionPipeline

with open("lab_results.csv", newline="", encoding="utf-8-sig") as f:
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

Only the first five mappings are required. Drop any of the rest your export
does not have — see the [CSV field reference](csv-reference.md#lab-columns) for
what each column contributes.

There is no `seed()` step and no batching decision to make: rows are grouped by
patient, and a patient with more rows than the API's per-request cap (5,000) is
chunked automatically.

## Building results by hand

The same object, constructed directly — for a queue consumer, a webhook, or a
handful of results:

```python
from cavell_client import LabResult, LabIngestionPipeline

results = [
    LabResult(
        lab_result_id="LAB-0001",  # your LIS accession — the idempotency key
        patient_identifier="MRN-20001",
        test_name="C-reactive protein",
        value="212",
        unit="mg/L",
        loinc_code="1988-5",
        reference_high=5.0,
        collected_datetime="2024-03-14T21:40:00+01:00",
        encounter_id="V-001",  # optional — must resolve if given
        practitioner_id="DOC-201",  # optional — must resolve if given
    ),
    LabResult(  # a qualitative result, no encounter
        lab_result_id="LAB-0002",
        patient_identifier="MRN-20001",
        test_name="Blood culture",
        value="No growth after 5 days",
        collected_datetime="2024-03-19",
    ),
]

outcome = LabIngestionPipeline(client).ingest(results)
```

Construction never raises on content. A blank value or a malformed datetime
becomes a rejection from `ingest()`, not an exception — one bad record cannot
sink the batch.

## What must already exist

| Referenced by | Identifier system | Created by |
|---------------|-------------------|------------|
| `patient_identifier` | `urn:cavell:patient` | `IngestionPipeline.seed()` |
| `encounter_id` | `urn:cavell:encounter` | Document extraction with `Document.encounter_id`, or your own writes |
| `practitioner_id` | `urn:cavell:practitioner` | `IngestionPipeline.seed()` |

Only the patient is mandatory. A row without `encounter_id` produces an
Observation with no encounter reference — which is what an outpatient or GP
draw should look like. A row without `practitioner_id` has no performer.

## Reference resolution is fail-closed

Every lookup that returns *nothing* rejects rows:

- Unknown patient → **all** of that patient's rows are rejected
- `encounter_id` given but unresolvable for that patient → only the rows
  carrying it
- `practitioner_id` given but unresolvable → only the rows carrying it

Every lookup that *fails* — a 500, a dropped connection — aborts the run with
`FHIRConnectionError`. Infrastructure trouble is not bad data, and quietly
turning an outage into 3,000 rejected rows would be the wrong report. Aborts
are safe to retry: persistence is idempotent, so re-ingest the same file once
the server is back and what already landed returns as `skipped_existing`.

Lookups are cached within a run — once per distinct encounter per patient, once
per distinct practitioner globally.

## Rejections

Rejected rows are reported, never guessed at. Each rejection carries the
**0-based position in the list you passed**, the result id, the patient, a
reason, and the stage that caught it:

```python
for r in outcome.rejected:
    rid = r.lab_result_id or "(no id)"
    print(f"row {r.index:>4}  {rid:<12} [{r.stage}] {r.reason}")
# row  244  LAB-0245     [reference] unknown patient 'MRN-99999'
# row  246  LAB-0247     [reference] unknown encounter 'V-999' for patient 'MRN-20001'
# row  249  LAB-0250     [validation] value must be non-empty
```

`stage` is `"validation"` (content, before any network), `"reference"` (a FHIR
lookup found nothing) or `"server"` (the API's own row checks). Rejections from
all three arrive in one list sorted by input position, so the report maps
straight back onto your source file.

The complete rule set — every condition that rejects a row, and the ones that
deliberately never do — is in
[When a lab row is rejected](csv-reference.md#when-a-lab-row-is-rejected).

## Re-running a feed

Ingestion is idempotent by construction. The Observation carries your
`lab_result_id` as a `urn:cavell:lab-result` identifier, and each bundle entry
is a conditional create matched on that identifier scoped to the patient:

```python
outcome = pipeline.ingest(results)  # 244 created
outcome = pipeline.ingest(results)  # again
print(outcome.created, outcome.skipped_existing)
# 0 244
```

So a run interrupted halfway can simply be repeated, and a daily feed that
overlaps yesterday's costs nothing extra.

!!! note "Ingestion only creates — it never updates"
    Re-sending an existing `lab_result_id` with a corrected value leaves the
    stored Observation untouched. An amended result needs a **new id**
    (`LAB-0001.A`, or your LIS's amendment accession) so it lands as its own
    Observation, ideally with `status="amended"`.

Unlike LLM-extracted resources, lab Observations carry **no `unvalidated`
meta tag**: a structured feed is already a source of truth, so there is nothing
for a clinician to review.

## Return types

### LabResult

See the [CSV field reference](csv-reference.md#lab-columns) for the full field
table. Construction normalizes only — strings are stripped, blank optionals
become `None`, numeric-looking bounds are coerced to float (European decimal
commas included).

### LabRejection

| Field | Type | Description |
|-------|------|-------------|
| `index` | `int` | 0-based position in the list passed to `ingest()` — input-relative, even for rejections the API reported against its own request |
| `lab_result_id` | `str` or `None` | The row's id, when it had one |
| `patient_identifier` | `str` or `None` | The row's patient |
| `reason` | `str` | Why it was skipped |
| `stage` | `"validation"`, `"reference"` or `"server"` | Which check caught it |

### LabIngestionOutcome

| Field / property | Type | Description |
|------------------|------|-------------|
| `total` | `int` | Rows passed in |
| `accepted` | `int` | Rows that actually landed in FHIR (created + already present) |
| `rejected` | `list[LabRejection]` | Skipped rows, sorted by input position |
| `persistence` | `list[tuple[str, PersistResult]]` | One entry per posted bundle, in processing order |
| `created` | `int` | Newly created Observations (201s) |
| `skipped_existing` | `int` | Already present (200s — the conditional create matched) |
| `success` | `bool` | Every posted bundle persisted cleanly. Rejected rows do **not** make a run unsuccessful — check `rejected` for those |

A row lost to a failed FHIR transaction is neither accepted nor rejected: it
appears in that bundle's `PersistResult.errors` and flips `success`.

## Error handling

| Raises | When |
|--------|------|
| `TypeError` | The list contains something that is not a `LabResult` — the classic mistake of passing raw CSV rows. Raised upfront, naming the offending positions, before any request |
| `FHIRConnectionError` | A reference lookup or a bundle POST failed at the transport level |
| `CavellAuthError` | The gateway rejected your key |
| `CavellGatewayUnavailableError` | The gateway is down |
| `CavellAPIError` | The API rejected a request. A **404** means the Prism deployment predates structured lab ingestion and needs upgrading |

Everything else is a rejection, not an exception.

## How it differs from the notes pipeline

| | [Clinical notes](ingestion.md) | Lab results |
|---|---|---|
| Cost | LLM tokens per document | None |
| Seeding | `pipeline.seed()` creates orgs, practitioners, patients | Nothing is created — all references must exist |
| Ordering | Chronological per patient; out-of-order notes get split context | Order is irrelevant; rows are independent facts |
| Bad input | `Document.from_rows()` raises on blank required fields | Rows are skipped and reported |
| Re-runs | `skip_processed` filters on `document_id` | Conditional creates on `lab_result_id` |
| Updates | Resources are updated in place as the record evolves | Create-only; an amended result needs a new id |
| Validation tag | Resources carry `unvalidated` until reviewed | Never tagged `unvalidated` — the feed is a source of truth |
| Concurrency | `max_concurrency` workers | Sequential; there is no latency to hide |

## Timeouts

| Client | Default | Notes |
|--------|---------|-------|
| Cavell API | 120s | Per ingest request (up to 5,000 rows), plus 10s to connect; no LLM latency to absorb |
| FHIR server | 30s | Per-request timeout for lookups and bundle POSTs |

A 429 is retried up to three times: the server's `Retry-After` is honoured when
it sends one (capped at 300s), and exponential backoff is used when it does not.

## Try it

`docs/notebooks/lab_results_ingestion_demo.ipynb` runs this end to end against
251 synthetic rows — fail-closed reference checks, a merged rejection report,
and an idempotent re-run. Run the
[hospitalization demo](notebooks/hospitalization_extraction_demo.ipynb) first:
the labs attach to its patients, practitioners and admissions.
