# CSV Field Reference

Both pipelines start from a table: a CSV of clinical notes, or a CSV of lab
results. This page is the reference for what each column means, what it
produces in FHIR, and which rules decide whether a row is accepted.

You do not have to rename anything in your export. You map your column headers
to SDK field names once, and the mapping is validated before a single object is
built.

## How a CSV becomes objects

Every helper dataclass has a `from_rows()` classmethod with the same shape:

```python
Document.from_rows(rows, columns={...}, **defaults)
```

| Argument | What it is |
|----------|------------|
| `rows` | A list of dicts — `csv.DictReader`, `df.to_dict("records")`, or parsed JSON |
| `columns` | Maps **SDK field name → your column header**. The left side is fixed by the SDK; the right side is whatever your export calls it |
| `**defaults` | Literal values applied to every row — for constants your file does not carry (`organization_identifier="CGH-001"`, `status="preliminary"`) |

```python
documents = Document.from_rows(
    rows,
    columns={
        "text": "note_text",  # SDK field  ←  your header
        "patient_identifier": "mrn",
        "date": "service_date",
        "document_id": "note_id",
    },
    organization_identifier="CGH-001",  # same value on every document
)
```

Set a column to `None` to disable an optional field your export does not have:
`columns={..., "encounter_id": None}`. Required fields cannot be disabled that
way.

Everything is validated upfront — unknown SDK field names, headers that are not
in the data, and missing required keys all raise `ValueError` before any object
is built, so a typo costs you nothing but the error message.

---

## Clinical notes CSV

One row per note. The same file usually feeds three object types: patients and
practitioners are deduplicated by identifier (first occurrence wins), documents
are one per row.

### The smallest file that works

```csv
note_id,patient_id,note_date,note_text
note-0001,MRN-12345,2024-01-15,"Patient presents with a persistent cough..."
note-0002,MRN-12345,2024-04-15,"Follow-up: symptoms resolved on amoxicillin..."
```

```python
patients = Patient.from_rows(
    rows, columns={"identifier": "patient_id"}, managing_organization="CGH-001"
)
documents = Document.from_rows(
    rows,
    columns={
        "text": "note_text",
        "patient_identifier": "patient_id",
        "date": "note_date",
        "document_id": "note_id",
    },
)
```

Everything else is optional and makes the extraction better.

### Document columns

The `Document` is the unit of extraction — one note in, FHIR resources out.

| SDK field | Required | Format | What it does |
|-----------|----------|--------|--------------|
| `text` | Yes | Free text | The clinical narrative the model reads. Warns below 20 characters |
| `patient_identifier` | Yes | Your MRN | Resolved against `urn:cavell:patient`; the patient must have been seeded |
| `date` | Yes | `YYYY-MM-DD` or `datetime.date` | **The document's own date.** Drives chronological ordering, is sent to the API as `document_date`, and is what every relative expression in the text ("two days ago", "since last winter") is resolved against |
| `document_id` | Yes | Your note id, unique across the feed | The idempotency key. The resume filter (`skip_processed`), the chronology watermark, and failure reporting all key on it. Stamped onto the DocumentReference |
| `encounter_id` | No | Your visit id | Ties the note to a hospital stay. The SDK looks the Encounter up (`urn:cavell:encounter`); the API creates it on the first note and **updates it in place** on later ones, and links every resource extracted from the note to it. Omit for standalone notes — then no Encounter is created |
| `practitioner_identifier` | No | Your staff id | Resolved to a FHIR id and the practitioner's name is injected into the model's context, which sharply improves author matching |
| `organization_identifier` | No | Your facility code | Falls back to the pipeline's `default_organization` |
| `meta` | No | Short free text | Extra context for the model — department, ward, note type. **Do not put the date or the author here**; both have their own fields |

!!! warning "Blank required values raise, they are not skipped"
    `Document.from_rows()` raises if `text`, `patient_identifier`, `date` or
    `document_id` is blank on any row, and raises again if two rows share a
    `document_id`. A note is expensive to extract and silently dropping one
    would be worse than stopping. (The lab pipeline makes the opposite choice —
    see [rejection rules](#when-a-lab-row-is-rejected).)

### Patient, Practitioner and Organization columns

These come from the same CSV, deduplicated by identifier. Rows with a blank
identifier are skipped.

| Object | SDK field | Required | What it does |
|--------|-----------|----------|--------------|
| `Patient` | `identifier` | Yes | Your MRN — the key every document and lab row references |
| | `name` | No | Patient name |
| | `birth_date` | No | ISO date |
| | `gender` | No | FHIR gender code |
| | `managing_organization` | No | Must match a seeded organization |
| | `general_practitioners` | No | Practitioner identifier(s); a single string is normalized to a list |
| `Practitioner` | `identifier` | Yes | Your staff id |
| | `name` | Yes* | Virtual column: splits `"Jane Smith"` into given + family |
| | `given_name` / `family_name` | Yes* | Use these *or* `name`, never both |
| | `organization_identifier` | Yes | Must match a seeded organization |
| | `specialty` | No | Written to `PractitionerRole.specialty` |
| `Organization` | `identifier` | Yes | Your facility code |
| | `name` | Yes | Display name |

Organizations are usually a short literal list rather than a CSV column.

### Order matters, and the SDK handles it

Notes are extracted per patient in date order, because each extraction is shown
what is already in the record. Hand the whole file to
[`extract_all()`](ingestion.md#extract_all-the-whole-dataset) and it sorts
globally by `date` before batching. A note that predates one already extracted
is not rejected — it is extracted against
[split context](ingestion.md#out-of-order-documents-get-split-context) instead.

---

## Lab results CSV

One row per result. No LLM is involved: the row is mapped to a FHIR Observation
deterministically, so there is no token cost and the same file always produces
the same resources.

### The smallest file that works

```csv
lab_result_id,patient_id,test_name,value,collected_datetime
LAB-0001,MRN-20001,C-reactive protein,212,2024-03-14
LAB-0002,MRN-20001,Haemoglobin,11.2,2024-03-14
```

```python
results = LabResult.from_rows(
    rows,
    columns={
        "lab_result_id": "lab_result_id",
        "patient_identifier": "patient_id",
        "test_name": "test_name",
        "value": "value",
        "collected_datetime": "collected_datetime",
    },
)
```

Add `loinc_code`, `unit` and reference bounds and the same row becomes a coded,
quantified, interpreted Observation.

### Lab columns

| SDK field | Required | Format | What it does |
|-----------|----------|--------|--------------|
| `lab_result_id` | Yes | Letters, digits and `. _ : / -`, max 200 chars | **The idempotency key** — typically your LIS accession number. Becomes the Observation's `urn:cavell:lab-result` identifier and drives the conditional create, so re-running a feed matches instead of duplicating. See [Identity and re-runs](#identity-and-re-runs) |
| `patient_identifier` | Yes | Your MRN | Must already exist in FHIR (`urn:cavell:patient`). The pipeline never creates patients |
| `test_name` | Yes | Free text | The analyte name. Becomes `code.text`, and the display when no LOINC code is given |
| `value` | Yes | Number, comparator, or text | See [Values](#values-numeric-comparator-qualitative) |
| `collected_datetime` | Yes | `YYYY-MM-DD` or ISO datetime **with an offset** | When the specimen was drawn → `effectiveDateTime`. See [Datetimes](#datetimes-and-timezones) |
| `loinc_code` | No | LOINC code | Adds a real LOINC coding with the official display. Omit and the code is text-only |
| `unit` | No | UCUM-style unit | `mmol/L`, `10*9/L`, `g/dL`. Recognized units get a UCUM code; unrecognized ones keep the text you gave without claiming a coding system |
| `reference_low` | No | Number | Lower bound of the normal range |
| `reference_high` | No | Number | Upper bound |
| `encounter_id` | No | Your visit id | Links the result to a stay. **If given it must resolve for that patient or the row is rejected**; if blank the Observation simply has no encounter — which is exactly right for an outpatient or GP draw |
| `practitioner_id` | No | Your staff id | The performer. Same rule: given means it must resolve |
| `status` | No | `preliminary`, `final`, `amended`, `cancelled` | Defaults to `final` |

### What the row becomes

| CSV | FHIR element |
|-----|--------------|
| `lab_result_id` | `identifier[0]` — system `urn:cavell:lab-result` |
| `test_name` + `loinc_code` | `code` — LOINC coding plus `text`, or text-only |
| `value` + `unit` | `valueQuantity` (numeric) or `valueString` (qualitative) |
| `reference_low` / `reference_high` | `referenceRange[0]` |
| derived | `interpretation` — `H`, `L` or `N` |
| `collected_datetime` | `effectiveDateTime` |
| `patient_identifier` | `subject` |
| `encounter_id` | `encounter` (omitted when blank) |
| `practitioner_id` | `performer[0]` (omitted when blank) |
| always | `category` = `laboratory`, `status` |

Lab Observations carry **no `unvalidated` tag**. A structured feed is already a
source of truth — there is nothing for a clinician to review, unlike an
LLM-extracted resource.

### Values: numeric, comparator, qualitative

A lab feed is not all numbers, and the SDK does not pretend otherwise.

| Value | Result |
|-------|--------|
| `212`, `11.2`, `-0.3` | `valueQuantity` with the unit |
| `<5`, `>=60`, `<= 0.01` | `valueQuantity` with a `comparator` — and **no interpretation**, since "below the assay floor" cannot be scored against a range |
| `6,4` | Read as `6.4`. A comma is accepted as a decimal separator only when it cannot be a grouping one |
| `No growth after 5 days`, `++`, `E. coli >10^5 CFU/mL` | `valueString`, verbatim. A qualitative result is a legitimate result, not a parse failure — reference range and interpretation are skipped |
| `150,000`, `1 500` | **Rejected as ambiguous.** Digit grouping is never guessed at: `150,000` could be 150.0 or 150000, and reading it wrong is worse than refusing it |
| blank, or a `NaN` from a dataframe | **Rejected** — a row with no value has nothing to record |

### Units and UCUM

Write units the UCUM way and they are coded properly: `10*9/L` rather than
`x10^9/L`, `umol/L`, `mmol/L`, `ng/L`, `kPa`, `U/L`, `mm/h`. An unrecognized
unit is not an error — the text you supplied is kept as the display, and no
UCUM coding is claimed for it, so no downstream consumer is misled into
converting a unit the server never actually identified.

### Reference ranges and interpretation

Supply either bound, both, or neither. When the value is numeric and at least
one bound is present, `interpretation` is derived: `H` above the high bound,
`L` below the low bound, `N` otherwise. Bounds on a qualitative value are
ignored rather than rejected.

Bounds must be unambiguously numeric. `3,5` is read as `3.5`; `1,000` or
`see report` rejects the row rather than being guessed at.

### Datetimes and timezones

`collected_datetime` accepts exactly two shapes:

- `2024-03-14` — a date, when the time is not meaningful or not exported
- `2024-03-14T21:40:00+01:00` — a full ISO datetime **with a timezone offset**
  (`Z` is fine too)

A time without an offset is rejected. This is FHIR's rule, not ours: a
`dateTime` carrying a time must carry a zone, and there is no safe default to
invent. It also matters clinically — a serial troponin drawn at 08:00, 09:00
and 11:00 is three distinct results, and the offset is what keeps them ordered
across a daylight-saving boundary.

### Identity and re-runs

`lab_result_id` must be unique per patient — unique across the whole feed is
safest. It makes ingestion idempotent: every bundle entry is a conditional
create matched on that identifier scoped to the patient, so re-sending a file
after a partial run creates nothing new and reports the rest as
`skipped_existing`.

!!! note "Ingestion only creates — it never updates"
    Re-sending an existing `lab_result_id` with a different value leaves the
    stored Observation untouched. An amended result therefore needs a **new
    id** (`LAB-0001.A`, or your LIS's amendment accession) so it lands as its
    own Observation with `status="amended"`.

---

## When a lab row is rejected

One bad row never sinks the batch. Bad rows are skipped, everything else is
ingested, and each skipped row is reported with its **0-based position in the
list you passed**, its id, and a reason. Rejections do not make a run
unsuccessful — they are the designed outcome.

Rows are checked in three stages, and a row is reported against the first one
that catches it.

### Stage `validation` — content checks, before any network

| Rejected when | Example |
|---------------|---------|
| A required field is blank after normalization | Empty `value`; a pandas `NaN`; a missing MRN |
| `lab_result_id` uses characters outside letters, digits and `. _ : / -`, or exceeds 200 characters | `LAB 1&2` — the id lands in a FHIR query string, where `&` and <code>&#124;</code> would change the query's meaning |
| `reference_low` / `reference_high` is present but not unambiguously numeric | `1,000`, `see report` |
| `status` is not one of `preliminary`, `final`, `amended`, `cancelled` | `final ` is fine (trimmed); `Final` and `verified` are not |
| Two rows in the same call share a `lab_result_id` | First occurrence is kept, the rest are rejected |

### Stage `reference` — fail-closed lookups against your FHIR server

Nothing is created for a reference that does not resolve.

| Rejected when | Scope |
|---------------|-------|
| The patient is not in FHIR | **All** of that patient's rows |
| `encounter_id` is given but does not resolve for that patient | Only the rows carrying it |
| `practitioner_id` is given but does not resolve | Only the rows carrying it |

A blank `encounter_id` or `practitioner_id` is not a rejection — the Observation
is simply written without that reference.

!!! danger "Not found rejects a row; a lookup *failure* aborts the run"
    If the FHIR server returns a 500 or the connection drops, that is
    infrastructure trouble rather than bad data, and guessing would be wrong.
    The run raises `FHIRConnectionError` and stops. Because persistence is
    idempotent, fix the infrastructure and re-ingest the same file — what
    already landed comes back as `skipped_existing`.

### Stage `server` — the API's own row checks

| Rejected when | Example |
|---------------|---------|
| `collected_datetime` is malformed, offset-less, or an impossible date | `2024-03-14 21:40`, `2024-02-30` |
| `value` is numerically ambiguous | `150,000` |
| A reference id is not a well-formed FHIR id | Only reachable when calling the API directly |

### Never rejected

- A missing `loinc_code` → text-only code
- An unrecognized `unit` → the text is kept, no UCUM coding claimed
- A missing reference range → no interpretation
- A qualitative `value` → `valueString`
- A re-submitted `lab_result_id` → counted as `skipped_existing`

### Reading the report

```python
outcome = LabIngestionPipeline(client).ingest(results)

print(outcome)
# 244/251 rows accepted (244 created, 0 already present), 7 rejected

for r in outcome.rejected:
    rid = r.lab_result_id or "(no id)"
    print(f"row {r.index:>4}  {rid:<12} [{r.stage}] {r.reason}")
# row  244  LAB-0245     [reference] unknown patient 'MRN-99999'
# row  246  LAB-0247     [reference] unknown encounter 'V-999' for patient 'MRN-20001'
# row  249  LAB-0250     [validation] value must be non-empty
# row  250  LAB-0251     [server] collected_datetime must be YYYY-MM-DD or a full
#                        ISO datetime with a timezone offset: '2024-12-03T08:00:00'
```

Rejections from all three stages arrive in one list, sorted by input position,
so a report can be written straight back against the source file — the `index`
is the row you gave, never an internal one.

One failure mode is neither accepted nor rejected: if a FHIR transaction itself
fails, its rows appear in that bundle's `PersistResult.errors` and
`outcome.success` is `False`. `accepted` only ever counts rows that actually
landed.

---

## Where to go next

- [Clinical notes pipeline](ingestion.md) — seeding, ordering, updates, error handling
- [Lab results pipeline](labs.md) — the full lab walkthrough and return types
- [Demo datasets](notebooks/README.md) — real CSVs you can run end to end
