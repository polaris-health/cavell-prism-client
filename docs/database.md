# Extracting from a Database

If your notes or lab results already live in a database table, you do not need
to export them to CSV first. Point the SDK at the table (or at a `SELECT`), map
the columns exactly as you would for the CSV, and run.

The database path is built for **large tables that keep growing**:

- **Only new work is read.** Each run first reads a handful of identifying
  columns, asks your FHIR server what is already extracted, and then fetches
  the note text only for the rows that still need it. A re-run with nothing
  new reads no note text and makes no extraction call.
- **What failed is retried.** A row that was not extracted successfully, for
  whatever reason, is not marked as done, so the next run picks it up again.
- **Bad rows never stop a run.** A row with a blank note, a malformed date or
  an unknown reference is skipped and reported. Everything else is extracted.
- **Every failure says why and who has to act**: your data, Prism, your FHIR
  server, or the run itself. See [When something fails](#8-when-something-fails).

The table has the **same columns as the CSV**. Everything in the
[CSV field reference](csv-reference.md) applies unchanged: field meanings,
formats, and rejection rules.

---

## 1. Install

```bash
pip install 'cavell-prism-client[database]'
```

The `database` extra installs [SQLAlchemy](https://www.sqlalchemy.org/), which
can connect to almost any database from a single URL. You also need the
**driver** for your database:

| Database | Driver to install | SQLAlchemy URL prefix |
|----------|-------------------|-----------------------|
| PostgreSQL | `pip install psycopg[binary]` | `postgresql+psycopg://` |
| Microsoft SQL Server / Azure SQL | `pip install pyodbc` + Microsoft ODBC Driver 18 | `mssql+pyodbc://` |
| Oracle | `pip install oracledb` | `oracle+oracledb://` |
| MySQL / MariaDB | `pip install pymysql` | `mysql+pymysql://` |
| SQLite | built into Python | `sqlite:///` |
| Snowflake | `pip install snowflake-sqlalchemy` | `snowflake://` |
| Databricks | `pip install databricks-sqlalchemy` | `databricks://` |

!!! tip "No SQLAlchemy? That works too"
    Any standard Python database connection (DB-API 2.0) is accepted as-is,
    with no extra install. See [Connecting without SQLAlchemy](#4-connecting-without-sqlalchemy).

---

## 2. Quick start

This runs end to end on your machine. It loads the demo `notes.csv` into a
local SQLite database, which stands in for your real one, and extracts it.

```python
import csv
import os
import sqlite3

from cavell_client import (
    CavellClient,
    DatabaseSource,
    IngestionPipeline,
    Organization,
    Practitioner,
)

# --- Stand-in for your database: the demo CSVs loaded into SQLite once ----
if not os.path.exists("clinical.db"):
    db = sqlite3.connect("clinical.db")
    for table, path in [
        ("notes", "docs/notebooks/notes.csv"),
        ("lab_results", "docs/notebooks/notes_lab_results.csv"),
    ]:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        cols = list(rows[0])
        db.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
        db.executemany(
            f"INSERT INTO {table} VALUES ({', '.join('?' for _ in cols)})",
            [tuple(r[c] or None for c in cols) for r in rows],  # blank -> NULL
        )
    db.commit()
    db.close()

# --- The part you keep ----------------------------------------------------
client = CavellClient(
    api_url="https://prd.prism.cavell.app/api",
    api_key=os.environ["PRISM_API_KEY"],  # your LLM Gateway key
    fhir_base_url="http://localhost:8090",
    fhir_api_path="/fhir",
)

notes = DatabaseSource("sqlite:///clinical.db", table="notes")

pipeline = IngestionPipeline(client, default_organization="CGH-001")
pipeline.seed(
    organizations=[Organization(identifier="CGH-001", name="City General Hospital")],
    patients=[],  # seeded from the table as they are needed
    practitioners=Practitioner.from_rows(
        DatabaseSource(
            "sqlite:///clinical.db",
            query="SELECT DISTINCT practitioner_id, practitioner_name FROM notes",
        ).rows(),
        columns={"identifier": "practitioner_id", "name": "practitioner_name"},
        organization_identifier="CGH-001",
    ),
)

report = pipeline.extract_from_database(
    notes,
    columns={  # the same mapping Document.from_rows() takes
        "document_id": "note_id",
        "patient_identifier": "patient_id",
        "date": "note_date",
        "text": "note_text",
        "encounter_id": "encounter_id",
        "practitioner_identifier": "practitioner_id",
        "meta": {"Department": "department"},
    },
    patient_columns={
        "name": "patient_name",
        "birth_date": "birth_date",
        "gender": "gender",
    },
    patient_defaults={"managing_organization": "CGH-001"},
    batch_size=1000,
    failures_path="prism_failures.jsonl",
)
print(report)
# 2,452 rows scanned: 0 already extracted, 2,452 extracted now (0 partial),
# 0 failed, 0 rejected, 0 duplicate(s) skipped — estimated cost $...
```

Run the last call again and it reports `2,452 already extracted, 0 extracted
now`. Insert new rows into `notes` and only those are extracted.

Lab results work the same way:

```python
from cavell_client import LabIngestionPipeline

outcome = LabIngestionPipeline(client).ingest_from_database(
    DatabaseSource("sqlite:///clinical.db", table="lab_results"),
    columns={  # the same mapping LabResult.from_rows() takes
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
    failures_path="prism_lab_failures.jsonl",
)
print(outcome)
```

---

## 3. Connecting with SQLAlchemy (recommended)

`DatabaseSource` takes a **SQLAlchemy URL**, an **Engine**, or a
**Connection**.

### Build the URL safely

Do not paste passwords into code, and do not hand-build URL strings:
passwords containing `@`, `/` or `%` then need escaping. Use `URL.create`
with values read from environment variables:

```python
import os
from sqlalchemy import URL
from cavell_client import DatabaseSource

url = URL.create(
    "postgresql+psycopg",
    username=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
    host=os.environ["DB_HOST"],
    port=5432,
    database="clinical",
)
notes = DatabaseSource(url.render_as_string(hide_password=False), table="notes")
```

### One example per database

```python
# PostgreSQL
URL.create(
    "postgresql+psycopg", username=u, password=p, host=h, port=5432, database="clinical"
)

# SQL Server / Azure SQL (ODBC Driver 18)
URL.create(
    "mssql+pyodbc",
    username=u,
    password=p,
    host=h,
    port=1433,
    database="clinical",
    query={"driver": "ODBC Driver 18 for SQL Server", "Encrypt": "yes"},
)

# Oracle (service name)
URL.create(
    "oracle+oracledb",
    username=u,
    password=p,
    host=h,
    port=1521,
    query={"service_name": "CLINICAL"},
)

# MySQL / MariaDB
URL.create(
    "mysql+pymysql", username=u, password=p, host=h, port=3306, database="clinical"
)

# SQLite (a file)
"sqlite:////absolute/path/to/clinical.db"
```

For Snowflake and Databricks, follow the URL format documented by their
SQLAlchemy dialect packages.

### Reuse an Engine you already have

If your application already has an Engine (with TLS settings, a connection
pool, a proxy…), pass it directly. The SDK never disposes an Engine or closes
a Connection it did not create.

```python
from sqlalchemy import create_engine

engine = create_engine(url, connect_args={"sslmode": "require"})  # PostgreSQL TLS
notes = DatabaseSource(engine, table="clinical.notes")
```

A `DatabaseSource` built from a URL owns its Engine. Use it as a context
manager (or call `close()`) to release it:

```python
with DatabaseSource(url, table="notes") as notes:
    report = pipeline.extract_from_database(notes, columns=...)
```

---

## 4. Connecting without SQLAlchemy

Any **DB-API 2.0** connection works: the standard interface every Python
database driver implements. No extra install is needed:

```python
import psycopg  # PostgreSQL

notes = DatabaseSource(psycopg.connect(os.environ["DB_DSN"]), table="notes")

import pyodbc  # SQL Server

conn = pyodbc.connect(
    "Driver={ODBC Driver 18 for SQL Server};Server=...;Database=clinical;"
    "UID=...;PWD=...;Encrypt=yes"
)
notes = DatabaseSource(conn, table="dbo.notes")

import oracledb  # Oracle

notes = DatabaseSource(
    oracledb.connect(user=u, password=p, dsn="host/CLINICAL"), table="NOTES"
)

import sqlite3  # SQLite

notes = DatabaseSource(sqlite3.connect("clinical.db"), table="notes")
```

The SDK detects the driver's placeholder style for its own lookups. With a
driver that does not declare one, pass `paramstyle=` (`qmark`, `numeric`,
`named`, `format` or `pyformat`).

!!! note "Transactions on a connection you pass in"
    Most drivers open a transaction with the first statement. So that a
    long run does not leave your session "idle in transaction" between
    batches (where a server's idle timeout can cut it off), the SDK ends the
    transactions its own reads opened. It only reads, so it rolls them back.
    A transaction you had open yourself is left alone.

    With psycopg, psycopg2, oracledb and sqlite3 the SDK can tell whether a
    transaction was open. With other drivers it does not touch the
    connection's transactions. Open the connection in autocommit mode, for
    example `pyodbc.connect(..., autocommit=True)`, or connect through
    SQLAlchemy.

!!! note "SQLAlchemy streams large scans; raw drivers may not"
    With SQLAlchemy, the SDK reads with server-side cursors where the driver
    supports them, so a multi-million-row key scan streams. Some raw DB-API
    drivers (psycopg's default cursor, for example) download a whole result
    before returning the first row. The key scan is small (a few short
    columns), but for very large tables SQLAlchemy is the better choice.

---

## 5. Choosing the rows: `table=` or `query=`

**`table=`** reads a table or view: `"notes"`, `"clinical.notes"` or
`"warehouse.clinical.notes"`. Only plain names are accepted. For anything
else, use `query=`.

**`query=`** reads the result of your own `SELECT`. Use it to:

- **filter**: only one department, only notes from 2020 onwards
- **join**: notes and patients in separate tables
- **rename** columns so they match what you want to map
- **convert** values, e.g. `CAST(note_ts AS DATE)`

```python
notes = DatabaseSource(
    engine,
    query="""
        SELECT n.note_id, n.patient_id, p.full_name AS patient_name, p.birth_date,
               n.note_date, n.encounter_id, n.author_id AS practitioner_id,
               n.department, n.body AS note_text
        FROM clinical.notes n
        JOIN clinical.patients p ON p.patient_id = n.patient_id
        WHERE n.note_date >= :since
    """,
    params={"since": "2020-01-01"},
)
```

Rules for `query=`:

- It is used as a **sub-query**, so it must be a single `SELECT`, without a
  trailing `;`. Anything else fails to parse rather than running: the SDK
  only ever reads.
- **Parameters.** With SQLAlchemy, write `:name` placeholders and pass a
  dict. A literal colon inside a string then needs escaping as `\:`. With a
  raw DB-API connection, use your driver's style: `%(name)s` for psycopg and
  pymysql, `?` for pyodbc and sqlite3, `:name` for oracledb. With psycopg and
  pymysql, write a literal `%` as `%%`.
- **No `ORDER BY` inside it on SQL Server**, which forbids it in a
  sub-query. Ordering is applied by the pipeline anyway: by date, globally.
- **Column names are matched exactly as the database reports them.**
  `notes.columns()` prints them. Oracle and Snowflake report unquoted names
  in UPPER CASE through their own drivers (`NOTE_ID`), and in lower case
  through SQLAlchemy (`note_id`). Map whichever you see, or alias in the
  query (`SELECT note_id AS "note_id" ...`).

---

## 6. The table schema

The columns are those of the CSV, and the mapping is the same:

- **Notes:** `columns=` takes exactly what [`Document.from_rows()`](csv-reference.md#document-columns)
  takes. `patient_columns=` maps the [`Patient`](csv-reference.md#patient-practitioner-and-organization-columns)
  fields used to create patients that are not in FHIR yet: `name`,
  `birth_date`, `gender`, `managing_organization`, `general_practitioners`.
  The patient identifier is always the `patient_identifier` column.
- **Labs:** `columns=` takes exactly what [`LabResult.from_rows()`](csv-reference.md#lab-columns)
  takes.

Database values are converted to the text a CSV export of the table would
hold, so every validation rule applies identically:

| Database value | Becomes |
|----------------|---------|
| `NULL` | empty, so it counts as blank exactly like an empty CSV cell |
| text | unchanged |
| integer, decimal, float | its digits (`4.50`, `12`), never in exponent form |
| `DATE` | `2024-03-14` |
| `TIMESTAMP` at exactly midnight, without a time zone | `2024-03-14` (Oracle and SQL Server return `DATE` columns this way) |
| other `TIMESTAMP` | `2024-03-14T21:40:00`, plus the offset when the column has one (`+01:00`) |
| `CLOB` / long text | read in full |
| bytes | decoded as UTF-8; anything else is an error naming the column |

`Document.date` accepts an ISO date **or** an ISO datetime and keeps the date
part as written, without converting to UTC. So a `TIMESTAMP` note date works
as-is. Lab `collected_datetime` still needs a timezone offset whenever it
carries a time ([why](csv-reference.md#datetimes-and-timezones)). Use
`TIMESTAMP WITH TIME ZONE`, or cast to a date in `query=`.

Bring practitioners (and, for small tables, anything else) in with
`DatabaseSource.rows()`. It returns the whole result as `csv.DictReader`
would, ready for any `from_rows()`:

```python
practitioners = Practitioner.from_rows(
    DatabaseSource(engine, table="clinical.staff").rows(),
    columns={"identifier": "staff_id", "name": "full_name"},
    organization_identifier="CGH-001",
)
```

---

## 7. Large and growing tables

### What one run does

1. **Key scan.** Reads only the identifying columns (document id, patient,
   date, encounter, practitioner, organization, and the patient columns),
   never the note text. Rows with a blank id, a bad date, a repeated
   `document_id` for one patient, or an unknown organization or practitioner
   are rejected here. Of rows that repeat a `document_id` for one patient,
   the first is kept if the rows are identical. If they differ, none of
   them is extracted until you make the ids unique, because there is no
   telling which one is meant.
2. **What is already done.** Two bulk, paginated scans of your FHIR server
   (seeded patients, extracted documents) rather than one query per patient.
   A document is *done* only if it was extracted and persisted successfully.
3. **Pending documents.** Sorted by date across the whole table, then
   processed in batches of `batch_size`. For each batch:
    - new patients are created from `patient_columns`
    - the full rows, note text included, are fetched for exactly that batch's
      keys
    - the batch is extracted with the usual in-place and deferred retries

Labs follow the same idea. A single scan of your FHIR server finds every
stored `lab_result_id`, the table is streamed once, and rows already stored
are skipped before anything is sent.

### What it costs

| | First (historical) run | Re-run with nothing new |
|---|---|---|
| Database | key scan + text for every row, batch by batch | key scan only |
| FHIR server | bulk scans + the extraction's own reads and writes | bulk scans only |
| Prism | one extraction per document | nothing |
| Memory | keys (≈150–300 bytes per row) + one batch of text | keys |

For scale: a key scan of a 1-million-row table, including working out the
pending rows, takes about 10 seconds and under 300 MB of memory on a laptop.
On a real deployment, the bulk FHIR scan (one request per 1,000 stored
documents) is usually the largest fixed cost of a re-run.

**Index the key columns.** An index on `(patient_id, note_id)`, or whatever
your two key columns are, makes fetching a batch's notes a direct lookup.

### Running it on a schedule

Because each run only does what is not done yet, the simplest way to keep up
with a growing table is to run the same script on a schedule, such as a
nightly cron job. Run **one extraction per table at a time**: a run decides
what is pending when it starts.

A run that is interrupted (crash, reboot, `Ctrl-C`) loses nothing. Whatever
was persisted is skipped next time, and whatever was not is still pending.

---

## 8. When something fails

### Every failure has a category

Each row that does not land is classified by **who has to act**:

| Category | Meaning | Typical cause | What to do |
|----------|---------|---------------|------------|
| `data` | The row itself is unusable | Blank text or id, unparseable date, repeated id, a patient record your FHIR server refuses (e.g. an invalid birth date) | Fix the row in your table. It is picked up on the next run |
| `reference` | The row points at something that does not exist | Unknown organization or practitioner; for labs, unknown patient or encounter | Seed it, or fix the reference |
| `prism_rejected` | Prism refused this document or row | A 4xx response; the detail says why | Fix the row if the detail names a content problem; otherwise contact Cavell support with the log line |
| `prism_unavailable` | Prism or the network failed transiently, through every retry | Timeouts, connection errors, 429, 5xx | Nothing; it is retried on the next run |
| `prism_partial` | Extracted, but some extraction steps failed | Some resources may be missing | Not retried automatically; contact Cavell support if it recurs |
| `fhir_rejected` | Your FHIR server refused the extracted resources | Validation or profile rules on your server; the detail carries its `OperationOutcome` | Check your FHIR server's validation settings |
| `fhir_unavailable` | Your FHIR server could not be reached, or errored | Connection refused, 5xx, authentication | Check the FHIR server; picked up on the next run |
| `run_aborted` | The run stopped | API key rejected, LLM Gateway unreachable | Fix the cause and re-run |
| `client_error` | An unexpected error inside the SDK | | Report it to Cavell support with the log line |

### Reading the log

Each failure is logged at `ERROR` as one self-contained line: the identifier,
patient, date, the row's position in the query result, the category, the
HTTP status, the number of attempts, the reason, the underlying detail, and
what to do.

```text
FAILED document note-0042 patient=MRN-1 date=2024-03-14 row=18231 category=prism_rejected http=422 attempts=1: failed while calling the Prism extraction API: Cavell API error (422): ... -> Prism refused this input — see the detail; ...
FAILED document note-0107 patient=MRN-7 date=2024-05-02 row=40112 category=fhir_rejected attempts=1: Persist failed: the FHIR server rejected the extracted bundle (1 resource error(s)) | detail: Condition: ... -> your FHIR server refused the data — check its validation/profile settings ...
REJECTED document note-0200 patient=MRN-9 date=2024-06-30 row=51007 category=data: Document.text must not be empty -> fix the row in the source data; it is picked up again on the next run
```

`row` is the 0-based position in the query result, so you can find the row
again with the same query.

The run ends with a summary: counts per outcome, failures per category, then
every failure grouped by category, up to 200 lines.

```text
extract_from_database: 69,410 extracted (12 partial), 3,050,000 already processed, 37 failed, 553 rejected, estimated cost $... — 602 row(s) need attention: data=540, prism_unavailable=30, reference=13, prism_partial=12, fhir_rejected=7
```

Make sure your application's logging shows `cavell_client` messages, for
example with `logging.basicConfig(level=logging.INFO)`.

### The failures file

`failures_path="prism_failures.jsonl"` also appends every failure, rejection
and partial extraction to a [JSON Lines](https://jsonlines.org/) file, one
object per line. It is written batch by batch, so it survives a crash. It is
append-only, so one file can hold the history of every run, each record
stamped with its run's `run_started_at`. A path that cannot be written fails
the run at once, before any work.

| Field | Meaning |
|-------|---------|
| `kind` | `document` or `lab_result` |
| `identifier` | Your `document_id` / `lab_result_id` |
| `patient_identifier`, `date`, `row` | Where to find it |
| `status` | `failed` (attempted), `rejected` (never sent), `partial` |
| `category`, `reason`, `detail`, `action` | Why, and what to do |
| `http_status`, `attempts`, `transient` | The technical detail |
| `run_started_at` | Which run recorded it |

```python
import pandas as pd

failures = pd.read_json("prism_failures.jsonl", lines=True)
latest = failures[failures.run_started_at == failures.run_started_at.max()]
print(latest.groupby("category").size())
print(latest[latest.category == "data"][["identifier", "row", "reason"]])
```

### What is retried, and when

- **Within a run.** Timeouts, connection drops, 429 and 5xx responses are
  retried in place, up to 3 attempts with backoff. Documents that still fail
  transiently get one more deferred pass after the batch.
- **On the next run.** Everything not extracted successfully: failures,
  rejections, and documents a run never reached because it aborted.
- **Not automatically.** Partial extractions (`prism_partial`), which did
  land.

!!! warning "A document that always fails is retried on every run"
    A row that fails the same way every time, such as a note Prism keeps
    refusing, is attempted again on each run until the row is fixed or
    removed from the query. Check the failures file after each run, and
    exclude rows you have decided to skip with a `WHERE` clause in `query=`.

### What raises instead

The run itself stops, after logging its summary, only for problems no row
can get past:

- **Mapping mistakes**, before anything is read: an unknown field, a
  required field not mapped, or a mapped column that is not in the table.
  These raise `ValueError`.
- **Database errors**: bad credentials, a missing table, a broken query.
  Your driver's own exception is raised.
- **An invalid API key or an unreachable LLM Gateway** raises
  `CavellAuthError` or `CavellGatewayUnavailableError`.
- **An unreachable FHIR server** during the bulk scans or patient creation
  raises.

Re-run once it is fixed: nothing is lost or duplicated.

---

## 9. Return values

`extract_from_database()` returns a `DatabaseExtractionReport`:

| Field | Meaning |
|-------|---------|
| `rows_scanned` | Rows read by the key scan |
| `already_extracted` | Rows skipped because their document is in FHIR |
| `pending` | Rows that needed extracting this run |
| `extracted` / `partial` | Extracted and persisted in this run (and how many were partial) |
| `duplicates_skipped` | Pending rows whose text repeats another pending row's for the same patient and date |
| `failed` | `IngestionOutcome`s of documents that were attempted and failed: `category`, `error`, `detail`, `http_status`, `attempts` |
| `rejected` | `DocumentRejection`s: `index` (row), `document_id`, `patient_identifier`, `reason`, `category` |
| `total_cost` | Estimated cost of this run |
| `success` | `True` when nothing failed or was rejected |

`ingest_from_database()` returns the usual
[`LabIngestionOutcome`](labs.md#labingestionoutcome) for the whole table.
`skipped_already_ingested` counts the rows not sent because they were already
stored, and each rejection's `index` is its position in the query result.

---

## 10. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ImportError: ... cavell-prism-client[database]` | Install the extra, or pass a DB-API connection instead of a URL |
| `ModuleNotFoundError: No module named 'psycopg'` (or `pyodbc`, `oracledb`, …) | Install your database's driver (see [Install](#1-install)) |
| `Column 'note_id' ... not found. Available columns: ['NOTE_ID', ...]` | Map the names the database reports, or alias them in `query=` |
| `table='...' is not a plain [catalog.][schema.]name` | Use `query="SELECT * FROM \"My Notes\""` for quoted or unusual names |
| `duplicate column name(s)` | Two columns of a join share a name; alias one of them |
| Every lab row with a time is rejected | `collected_datetime` needs an offset; use a time-zone-aware column, or format one in `query=` |
| The database closes the connection mid-run ("idle in transaction" timeout) | Open the connection in autocommit mode or connect through SQLAlchemy (see [Connecting without SQLAlchemy](#4-connecting-without-sqlalchemy)), then re-run; nothing done is repeated |
| `N rows share patient_id=..., note_id=... with different content` | Two rows carry the same id for one patient with different text; make the ids unique |
| The key scan is slow on a huge view | Point `table=`/`query=` at the base table, or a materialized copy, and index the key columns |

---

## 11. Limits

- **Run one extraction per table at a time.** What is pending is decided
  once, at the start of a run.
- **Patients are created once.** Demographics of a patient already in FHIR
  are not updated from the table. Use `pipeline.seed()` to update them.
- **Text duplicates are caught among pending rows only.** A row repeating the
  text of a note extracted in an *earlier* run, under a new id, is extracted
  again.
- **Read-only.** The SDK only runs `SELECT` statements, so a read-only
  database user is enough, and recommended. For a large historical load,
  point it at a reporting replica rather than the production database.
