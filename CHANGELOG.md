# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-08-10

### Added

- **`CavellAPI.extract_raw()`** returns the extraction response body verbatim,
  with the same arguments, 429-retry behaviour and error contract as
  `extract()`, which is now a typed view over it. The tuple `extract()` returns
  could only carry `(bundle, count, usage)`, so three documented response fields
  were discarded before any caller could see them.
- **`ExtractResult.extraction_status`** and **`.failed_extractors`**, plus an
  `is_partial` property. Prism returns `extraction_status: "partial"` when an
  extractor fails after its retries; the bundle is still valid, just missing
  whatever that extractor would have found. Callers previously could not tell
  that apart from a note that genuinely had nothing to extract — which matters
  for anything measuring recall, and for deciding whether a document is worth
  re-running.
- **`UsageStats.breakdown`** carries the API's per-stage, then per-agent token
  and cost attribution (`pre_extraction`/`extraction`/`coding` → each
  extractor). Kept as a plain dict rather than nested `UsageStats` so a new
  stage or extractor reaches callers without a client release.

## [0.3.0] - 2026-07-31

### Added

- **Duplicate-content check**: `extract_all()` drops documents repeating an
  earlier one's content verbatim — same `patient_identifier`, same `date`,
  byte-identical `text`. Source exports often carry a note twice under
  different `document_id` values (multi-feed merges, amended-note
  re-exports); the resume-skip keys on `document_id`, so each copy looked
  like a new document and produced its own Encounter, DocumentReference and
  clinical resources for one real event. The first occurrence in the
  caller's order wins, dropped documents get no `IngestionOutcome`, and the
  count is logged at WARNING. The same text on two different dates is
  copy-forward documentation of two real encounters and is kept. Pass
  `dedupe_content=False` to process the list exactly as given. The check
  lives in `extract_all()` rather than `extract()` because only the pass
  that sees the whole dataset can catch a duplicate pair split across two
  batches.

## [0.2.0] - 2026-07-31

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
- **`CavellClient(...)` now validates both endpoints at construction**, so a
  misconfiguration raises there instead of deep inside `seed()`/`extract()`,
  and the exception type says which half is wrong: `GET /key/info`
  pre-flights the LLM Gateway key without spending tokens
  (`CavellAuthError` / `CavellGatewayUnavailableError` / `CavellAPIError`),
  then `GET /metadata` checks the FHIR server (`FHIRAuthError` /
  `FHIRConnectionError`). Calling `check_connection()` afterwards is no
  longer necessary; constructing a client now requires network access to
  both. `extract()` still pre-flights the key once per run.
- **`FHIRConnectionError`** — FHIR failures surface as a library exception
  instead of a raw `httpx.HTTPStatusError`/`ConnectError` escaping from
  `check_connection()`.
- **Run-global failures raise.** A 401 (`CavellAuthError`) or a 503 that
  survives in-place retries (`CavellGatewayUnavailableError`) aborts
  `IngestionPipeline.extract()` with an exception instead of producing
  per-document failure outcomes. Re-running with `skip_processed=True`
  (the default) resumes safely.
- **`extract(batch_size=...)` is now `extract(limit=...)`.** The old name
  implied chunking it never did: it caps a single pass, and the documents
  past the cap were left unprocessed. Passing `batch_size=` to `extract()`
  raises `TypeError` with migration guidance rather than being silently
  ignored — silently ignoring it would extract every document passed.
  Use `extract_all()` to process a whole dataset in chunks. `limit` must
  be `>= 1` if set.

### Added

- `CavellAuthError` (401) and `CavellGatewayUnavailableError` (503), both
  subclassing `CavellAPIError`.
- `Retry-After` is honored in the 429 retry loop (capped at 300s).
- **Chronology check**: documents older than the patient's newest
  already-persisted document are **refused**. `extract()` and `extract_all()`
  raise `OutOfOrderDocumentError` before anything in the call is extracted,
  so no tokens are spent and nothing is persisted — including the in-order
  documents in the same call. `OutOfOrderDocumentError.violations` lists every
  offender as an `OutOfOrderDocument` (`patient_identifier`, `document_id`,
  `document_index`, `date`, `watermark`). Equal dates are not violations
  (dates are day-resolution), already-processed documents are filtered out
  before the check, and the check fails open per patient if the watermark
  query errors.

  Extraction is context-aware: each note is read against the patient's
  *current* resources with no date filtering, so an older note would be
  interpreted against a clinical picture from its own future. Refusing is the
  conservative position while that is true.

  This supersedes an earlier design in which the older document was extracted
  anyway behind an **update guard** that dropped updates to resources sourced
  from newer documents. That code is retained but disabled
  (`_apply_update_guard` and the commented-out block in
  `_process_single_document`, plus one skipped test) in case the policy is
  revisited.
- `mark_validated(resource_type, id)` removes the Prism `unvalidated` meta
  tag via FHIR `$meta-delete`; `list_unvalidated_resources(patient, type)`
  lists the clinician review queue.
- `list_processed_document_ids(patient_id=...)` for patient-scoped resume.
- **Active CarePlans are sent as extraction context**, activating the
  server's plan versioning: continuing plans are updated/ended instead of
  re-created on every note (previously ~10 duplicate plans per patient).
- Local dev tooling: `scripts/start_fhir.sh [--fresh]` and
  `scripts/fhir_summary.py`; ruff security (S) rules; hardened pre-commit
  (detect-secrets, uv-lock, lockfile-pinned ruff/ty).
- The `notebook` extra now declares version floors —
  `pip install "cavell-prism-client[notebook]"` pulls `notebook>=7.6.1`,
  `tqdm>=4.70.0`, and `ipywidgets>=8.0` (the demo notebooks need the
  ipywidgets 8 progress-bar API).
- Python 3.14 is tested in CI and declared in the classifiers.
- **`IngestionPipeline.extract_all(documents, batch_size=N)`** — processes a
  whole dataset, which `extract()` never did. Sorts every document by
  ascending date across the **entire** dataset before splitting it into
  batches, so each patient's documents are extracted oldest-first even when
  they span batches. Batching an unsorted list splits it by input order,
  which lets a later batch carry documents older than what an earlier batch
  persisted; those trip the chronology guard and lose their updates.
  Batches are cut by index, so the walk always terminates and works with
  `skip_processed=False` and with documents that have no `document_id`. All
  documents are validated before the first batch spends, which is also the
  only way `document_id` uniqueness is enforced across batch boundaries. An
  optional `on_batch` callback reports progress as each batch finishes.

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
    api_url="https://<old-deployment-url>/api",
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

Batched extraction moves to `extract_all()`:

```python
# before (0.1.x) — processed only the first 500 documents; the rest needed
# further extract() calls, and batch membership followed input order
for outcome in pipeline.extract(documents, batch_size=500):
    ...

# after (0.2.0) — processes every document, globally date-ordered
for outcome in pipeline.extract_all(documents, batch_size=500):
    ...

# to cap a single pass instead (e.g. a sanity check before a full run)
for outcome in pipeline.extract(documents, limit=10):
    ...
```

## [0.1.0]

Internal release: ingestion pipeline (seed → extract → persist), CSV
helpers, practitioner matching, observation deduplication, transient-failure
retries with a deferred pass.
