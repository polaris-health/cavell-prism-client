# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.5.1] - 2026-08-18

### Fixed

- **Documentation that still described reverse-chronological documents as
  refused.** 0.5.0 changed them to be extracted against split context, but the
  `IngestionOutcome.out_of_order` reference, the `extract()`/`extract_all()`
  docstrings, the `OutOfOrderDocumentError` deprecation note, the README and
  the notebook dataset table all still said such a document was dropped before
  extraction and came back as a failed outcome with nothing persisted and no
  tokens spent. Three in-page links pointed at a heading that no longer exists.
  No code changed.

  The correction worth reading: "How Updates Work" claimed a backdated document
  "cannot overwrite (and thus cannot un-validate) anything, because it is never
  extracted in the first place". It **is** extracted, and the bundle it produces
  can update a resource that a *newer* document created — which re-adds
  `unvalidated` to that resource. The client deliberately does not veto those
  updates: it sends `future_context` and the API's reconciliation decides what to
  merge, drop or create. The client-side update guard that would have dropped
  them is retained but disabled in `_process_single_document`.
- `IngestionOutcome.document_index` was documented as the position in the
  original input list. It is the position *after* `skip_processed` filtering and
  the `limit` truncation, and is batch-relative under `extract_all()`.
  `document_id` was still marked optional, having become required in 0.5.0.
- The notebook dataset table undercounted the held-back notes in
  `hospitalizations.csv` as 5 (3 backdated); there are 8 — 6 backdated for
  `MRN-20002` and 2 forward-dated for `MRN-20017`.

## [0.5.0] - 2026-08-18

### Added

- **Three more resource types are now sent as extraction context:
  `MedicationAdministration`, `NutritionOrder` and `FamilyMemberHistory`.** The
  extraction API has read the first two for months, with prompts and
  update-builders behind them, but nothing ever filled those slots — so their
  UPDATE branches were unreachable and every note re-created the resource. Most
  visibly, a diet order could not be *discontinued*: stopping one means updating
  the order that started it, and the extractor was never shown it.
  `FamilyMemberHistory` is new on the server side too; conditions are merged
  onto the relative already on record instead of adding another entry for the
  same father at every visit. **Requires the matching Prism-side context slot
  for `FamilyMemberHistory`**; the other two work against any current server.
  `NutritionOrder` and `FamilyMemberHistory` are searched by `patient` — R4
  defines no `subject` search parameter for either.
- **`FHIRClient.fetch_split_patient_context()`** returns `(past, future)` for a
  document being processed out of chronological order — the same context
  `fetch_patient_context()` builds, sorted by which side of the document's date
  each resource's provenance falls on. The Observation query is closed at the
  document's date rather than partitioned afterwards: a single newest-first
  search spends its whole 50-result cap on the newest observations, so for a
  backdated document every one of them would postdate it and the past half
  would come back empty. Observations dated *after* the document are not sent
  at all — the API matches on an exact `(date, code, value)` and a document
  only reports results at or before its own date, so a later one could never
  match anything it proposes.
- **`CavellAPI.extract()` and `.extract_raw()` take `future_context` and
  `out_of_order`.** Both are omitted from the payload when unset, so ordinary
  forward extraction sends exactly the payload it did before.

### Changed

- **The Observation context is now a 2-year window ending at the document's own
  date, capped at the 50 most recent within it** (previously: the 50 most
  recent overall, with no window). The window is anchored on the document
  rather than on today, so backfilling an archive of older notes still sees the
  observations around each one instead of an empty window two years behind the
  present. `FHIRClient.fetch_patient_context()` takes a new optional
  `reference_date`; the ingestion pipeline passes each document's date
  automatically.
- **`ResearchStudy` context is no longer filtered to `status=active`.** Only
  `entered-in-error` and `withdrawn` are dropped now. A study the patient
  joined two years ago is still the study a new note names, but its status
  moves on to `completed` or `closed-to-accrual` — and the active-only filter
  removed it from the context exactly then, so the extractor, which matches
  studies by title and embedding similarity, re-created it under a fresh id.
  `search_research_studies()` takes `exclude_statuses` in place of `status`
  (breaking for direct callers of that method; the pipeline is unaffected).
- **`Document.document_id` is now required** (breaking). It is keyword-only, so
  the positional arguments around it are unchanged, and omitting it raises
  `TypeError` at construction. Everything that makes ingestion safe to re-run
  keys on it: the `skip_processed` resume filter, the chronology watermark
  (read off persisted document identifiers), and failure reporting. A document
  without one was silently re-extracted and re-persisted on every run,
  invisible to the chronology guard, and reported only by a batch-relative
  `doc[N]` index. `Document.from_rows()` now requires a `document_id` column,
  and a blank value in that column raises instead of becoming `None`.
- **The document date is now sent as its own `document_date` payload field**
  (breaking), as an ISO `YYYY-MM-DD` string, instead of being prepended to
  `meta` as the prose line `Document date: 2024-01-15`. `meta` now carries only
  what it is for — your supplementary context plus the injected attending
  practitioner — and is omitted from the payload entirely when both are absent.
  `CavellAPI.extract()` and `.extract_raw()` take a new optional
  `document_date` argument, keyword-compatible with existing calls.

  **Requires the matching Prism-side field.** Until the server reads
  `document_date`, the extraction model no longer receives the document date
  at all, since it is no longer in `meta`.
- **`extract()` and `extract_all()` check that they were handed `Document`
  objects.** Passing anything else — a raw CSV row dict is the usual slip —
  now raises `TypeError` naming the offending positions, instead of an
  `AttributeError` from inside a worker thread partway through a run. The
  check runs over the whole list before the API pre-flight, so a bad item
  costs not even one request. Field contents are unchanged and still validated
  in one place, by `Document` itself.

- **Reverse-chronological documents are now extracted against split context
  instead of aborting the run** (breaking). A single backdated document for one
  patient used to raise `OutOfOrderDocumentError` before anything was
  extracted, throwing away every other patient's valid work. Such a document is
  now extracted, but is shown the record **as it stood on its own date** —
  `context` holds only what was already known then, everything newer travels
  separately as `future_context`, and `out_of_order: true` tells the API which
  it is looking at. Resources are sorted between the two by **provenance**: the
  newest already-processed document that created or updated each one, read from
  that document's `DocumentReference.context.related`. What matters is when a
  fact entered the record, not when it happened — a Condition recorded last
  year with a 1998 onset is still knowledge the older note's author could not
  have had.

  **Requires the matching Prism-side fields.** A server that predates them
  ignores unknown fields silently, which would leave a backdated document
  extracting against past-only context with nothing to reconcile against.
- **`extract()` and `extract_all()` no longer raise `OutOfOrderDocumentError`**
  (breaking). Replace `except OutOfOrderDocumentError` with a filter on
  `outcome.out_of_order`, which now marks *how* a document was processed rather
  than that it was rejected — it pairs with `success=True` on a normal run. A
  repeated call is also idempotent now: previously the second run of a mixed
  batch raised once its valid documents had been processed and filtered.
- `extract_all()` no longer runs a whole-dataset chronology check before the
  first batch. It existed so a late violation could not surface after earlier
  batches had spent; nothing aborts anymore, and dropping it saves one FHIR
  watermark query per patient per run. Global date-sorting is still worth it —
  an in-order document skips the provenance and future-Observation queries the
  split needs.
- **`ResearchStudy` context is capped at 200 studies.** The registry is global,
  fetched for every patient, and grows without limit. It is deliberately *not*
  date-filtered and never split: a study carries no clinical date to filter on
  (the API writes only identifier, status, title and arms), `meta.lastUpdated`
  records when the row was written rather than when the trial became known — a
  backfill stamps every study with today — and a study the extractor cannot see
  is one it re-creates under a fresh id, which is wrong for every patient on the
  server. `search_research_studies()` takes a new `max_results`.

### Deprecated

- **`OutOfOrderDocumentError`** is no longer raised anywhere. It remains
  exported so existing `except` clauses keep importing, and will be removed in
  a future release. `OutOfOrderDocument` is unaffected.

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
