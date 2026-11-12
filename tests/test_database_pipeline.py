"""extract_from_database / ingest_from_database: incremental runs over a table.

The FHIR server and the Prism API are in-memory fakes with real state
(tests/fakes.py), so a second run sees what the first one persisted. The
pipelines' own code — key scan, bulk "already done" scans, batching, seeding,
the per-document retries and the failure classification — runs unchanged.
"""

import json
import logging
import sqlite3

import httpx
import pytest

import cavell_client.ingestion as ingestion_module
from cavell_client import (
    DatabaseSource,
    IngestionPipeline,
    LabIngestionPipeline,
    Organization,
    Practitioner,
)
from cavell_client.models import CavellAuthError
from tests.fakes import fake_client, prism_error

NOTE_COLUMNS = (
    "note_id",
    "patient_id",
    "patient_name",
    "birth_date",
    "note_date",
    "practitioner_id",
    "department",
    "note_text",
)

DOCUMENT_MAPPING = {
    "document_id": "note_id",
    "patient_identifier": "patient_id",
    "date": "note_date",
    "text": "note_text",
    "practitioner_identifier": "practitioner_id",
    "meta": {"Department": "department"},
}
PATIENT_MAPPING = {"name": "patient_name", "birth_date": "birth_date"}


def _note(note_id, patient_id, note_date, text=None, **extra):
    row = {
        "note_id": note_id,
        "patient_id": patient_id,
        "patient_name": f"Name of {patient_id}",
        "birth_date": "1960-05-01",
        "note_date": note_date,
        "practitioner_id": "DOC-1",
        "department": "Cardiology",
        "note_text": text if text is not None else f"Clinical note {note_id} text.",
    }
    row.update(extra)
    return row


def _insert(conn, rows):
    conn.executemany(
        f"INSERT INTO notes VALUES ({', '.join('?' for _ in NOTE_COLUMNS)})",
        [tuple(r[c] for c in NOTE_COLUMNS) for r in rows],
    )
    conn.commit()


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute(f"CREATE TABLE notes ({', '.join(NOTE_COLUMNS)})")
    _insert(
        connection,
        [
            _note("n1", "MRN-1", "2024-01-10"),
            _note("n2", "MRN-1", "2024-02-10"),
            _note("n3", "MRN-2", "2024-01-20"),
            _note("n4", "MRN-2", "2024-03-01"),
            _note("n5", "MRN-3", "2023-12-31"),
        ],
    )
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(ingestion_module.time, "sleep", lambda s: None)


@pytest.fixture
def client():
    return fake_client()


def _pipeline(client):
    pipeline = IngestionPipeline(client, default_organization="ORG-1")
    pipeline.seed(
        organizations=[Organization(identifier="ORG-1", name="General Hospital")],
        patients=[],
        practitioners=[
            Practitioner(
                identifier="DOC-1",
                family_name="Smith",
                given_name="Jane",
                organization_identifier="ORG-1",
            )
        ],
    )
    return pipeline


def _run(client, conn, **kwargs):
    pipeline = _pipeline(client)
    source = DatabaseSource(conn, table="notes")
    return pipeline.extract_from_database(
        source,
        columns=DOCUMENT_MAPPING,
        patient_columns=PATIENT_MAPPING,
        **kwargs,
    )


def _spy_fetches(monkeypatch):
    """Record every key the pipeline asks the database for in full."""
    fetched: list[tuple[str, str]] = []
    original = DatabaseSource.fetch_matching

    def spy(self, key_columns, keys, **kw):
        keys = list(keys)
        fetched.extend(keys)
        return original(self, key_columns, keys, **kw)

    monkeypatch.setattr(DatabaseSource, "fetch_matching", spy)
    return fetched


class TestIncrementalRuns:
    def test_first_run_extracts_every_row_in_date_order(self, client, conn):
        report = _run(client, conn, batch_size=2)

        assert report.rows_scanned == 5
        assert report.already_extracted == 0
        assert report.extracted == 5
        assert report.success
        assert client._api.extracted == ["n5", "n1", "n3", "n2", "n4"]
        assert report.total_cost == pytest.approx(0.05)

    def test_patients_are_seeded_from_the_table(self, client, conn):
        _run(client, conn)
        seeded = client._fhir.list_patient_ids()
        assert set(seeded) == {"MRN-1", "MRN-2", "MRN-3"}

    def test_rerun_with_nothing_new_reads_no_text_and_extracts_nothing(
        self, client, conn, monkeypatch
    ):
        _run(client, conn)
        fetched = _spy_fetches(monkeypatch)
        extracted_before = list(client._api.extracted)

        report = _run(client, conn)

        assert report.already_extracted == 5
        assert report.pending == 0
        assert fetched == []
        assert client._api.extracted == extracted_before

    def test_growing_table_only_new_rows_are_fetched_and_extracted(
        self, client, conn, monkeypatch
    ):
        _run(client, conn)
        _insert(
            conn,
            [_note("n6", "MRN-1", "2024-04-01"), _note("n7", "MRN-9", "2024-04-02")],
        )
        fetched = _spy_fetches(monkeypatch)
        seeds_before = len(client._fhir.seed_sizes)

        report = _run(client, conn)

        assert sorted(fetched) == [("MRN-1", "n6"), ("MRN-9", "n7")]
        assert client._api.extracted[-2:] == ["n6", "n7"]
        assert report.already_extracted == 5
        assert report.extracted == 2
        # Only the new patient is seeded (after the org/practitioner/role seeds).
        assert client._fhir.seed_sizes[seeds_before + 3 :] == [1]

    def test_failed_documents_are_picked_up_by_the_next_run(self, client, conn):
        client._api.failures["n3"] = prism_error(422, "document text is not clinical")
        first = _run(client, conn)
        assert [o.document_id for o in first.failed] == ["n3"]

        client._api.failures.clear()
        second = _run(client, conn)
        assert second.already_extracted == 4
        assert second.extracted == 1
        assert client._api.extracted[-1] == "n3"

    def test_content_duplicates_are_skipped_across_batches(self, client, conn):
        _insert(conn, [_note("n1-copy", "MRN-1", "2024-01-10")])
        # Same patient, date and text as n1 but a different id.
        conn.execute(
            "UPDATE notes SET note_text = (SELECT note_text FROM notes WHERE "
            "note_id = 'n1') WHERE note_id = 'n1-copy'"
        )
        conn.commit()
        report = _run(client, conn, batch_size=1)
        assert report.duplicates_skipped == 1
        assert "n1-copy" not in client._api.extracted


class TestRejections:
    def test_bad_rows_are_rejected_and_the_rest_extracted(self, client, conn, caplog):
        _insert(
            conn,
            [
                _note("b1", "MRN-1", "2024-05-01", text="   "),
                _note("b2", "MRN-1", "not a date"),
                _note("n1", "MRN-1", "2024-05-02"),  # duplicate of row 0
                _note("b4", "MRN-1", "2024-05-03", practitioner_id="DOC-404"),
                _note("", "MRN-1", "2024-05-04"),
            ],
        )
        with caplog.at_level(logging.WARNING):
            report = _run(client, conn)

        # Row 7 repeats row 0's id with other content: neither is extracted.
        assert report.extracted == 4
        reasons = {r.index: (r.category, r.reason) for r in report.rejected}
        assert reasons[0] == (
            "data",
            "2 rows share patient_id='MRN-1', note_id='n1' with different "
            "content — none is used until the key is made unique",
        )
        assert reasons[5] == ("data", "Document.text must not be empty")
        assert (
            reasons[6][0] == "data"
            and "Invalid date format 'not a date'" in reasons[6][1]
        )
        assert reasons[7][0] == "data" and "first used by row 0" in reasons[7][1]
        assert reasons[8] == (
            "reference",
            "unknown practitioner 'DOC-404' (not seeded)",
        )
        assert (
            reasons[9][0] == "data" and "document_id must not be empty" in reasons[9][1]
        )
        assert (
            "REJECTED document b1 patient=MRN-1 date=2024-05-01 row=5 category=data"
            in caplog.text
        )
        assert "fix the row in the source data" in caplog.text

    def test_patient_the_fhir_server_refuses_rejects_only_its_documents(
        self, client, conn
    ):
        client._fhir.refuse_patients.add("MRN-2")
        report = _run(client, conn)
        assert report.extracted == 3
        rejected = {r.document_id: r for r in report.rejected}
        assert set(rejected) == {"n3", "n4"}
        assert "Patient.birthDate: invalid date" in rejected["n3"].reason
        assert rejected["n3"].category == "data"

    def test_mapping_mistakes_still_raise(self, client, conn):
        pipeline = _pipeline(client)
        source = DatabaseSource(conn, table="notes")
        with pytest.raises(ValueError, match="Column 'nope'"):
            pipeline.extract_from_database(
                source, columns={**DOCUMENT_MAPPING, "text": "nope"}
            )
        with pytest.raises(ValueError, match="Unknown Patient field"):
            pipeline.extract_from_database(
                source, columns=DOCUMENT_MAPPING, patient_columns={"mrn": "patient_id"}
            )

    def test_requires_seed_first(self, client, conn):
        pipeline = IngestionPipeline(client)
        with pytest.raises(RuntimeError, match="seed"):
            pipeline.extract_from_database(
                DatabaseSource(conn, table="notes"), columns=DOCUMENT_MAPPING
            )


class TestFailureReporting:
    def _failures(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_each_failure_names_who_has_to_act(self, client, conn, tmp_path, caplog):
        client._api.failures["n1"] = prism_error(422, "text is empty after cleaning")
        client._api.failures["n2"] = httpx.ReadTimeout("timed out")
        client._api.partial.add("n3")
        client._fhir.reject_bundles_for.add("n4")
        path = tmp_path / "failures.jsonl"

        with caplog.at_level(logging.WARNING):
            report = _run(client, conn, failures_path=path)

        by_id = {o.document_id: o for o in report.failed}
        assert by_id["n1"].category == "prism_rejected"
        assert by_id["n1"].http_status == 422
        assert by_id["n2"].category == "prism_unavailable"
        assert by_id["n2"].transient
        # 3 in-place attempts, then 3 more in the deferred retry pass.
        assert by_id["n2"].attempts == 6
        assert by_id["n4"].category == "fhir_rejected"
        assert "Condition: code: unknown system" in by_id["n4"].detail
        assert report.partial == 1

        records = {r["identifier"]: r for r in self._failures(path)}
        assert records["n1"]["category"] == "prism_rejected"
        assert records["n1"]["detail"] == "text is empty after cleaning"
        assert records["n1"]["row"] == 0
        assert records["n2"]["transient"] is True
        assert records["n3"]["status"] == "partial"
        assert records["n3"]["category"] == "prism_partial"
        assert records["n4"]["category"] == "fhir_rejected"
        assert all(r["run_started_at"] and r["action"] for r in records.values())

        assert "while calling the Prism extraction API" in caplog.text
        assert "the FHIR server rejected the extracted bundle" in caplog.text
        assert "extract_from_database: 2 extracted (1 partial)" in caplog.text
        assert "fhir_rejected=1" in caplog.text

    def test_abort_reports_what_happened_and_reraises(
        self, client, conn, tmp_path, caplog
    ):
        client._api.failures["n5"] = CavellAuthError(401, "invalid key")
        path = tmp_path / "failures.jsonl"
        with caplog.at_level(logging.ERROR), pytest.raises(CavellAuthError):
            _run(client, conn, failures_path=path, batch_size=2)

        records = self._failures(path)
        assert records[0]["identifier"] == "n5"
        assert records[0]["category"] == "run_aborted"
        assert "extract_from_database ABORTED" in caplog.text

    def test_failures_file_is_appended_across_runs(self, client, conn, tmp_path):
        path = tmp_path / "failures.jsonl"
        client._api.failures["n1"] = prism_error(422, "bad")
        _run(client, conn, failures_path=path)
        client._api.failures["n1"] = prism_error(422, "bad")
        _run(client, conn, failures_path=path)
        assert [r["identifier"] for r in self._failures(path)] == ["n1", "n1"]


LAB_COLUMNS = ("lab_result_id", "patient_id", "test_name", "value", "unit", "collected")
LAB_MAPPING = {
    "lab_result_id": "lab_result_id",
    "patient_identifier": "patient_id",
    "test_name": "test_name",
    "value": "value",
    "unit": "unit",
    "collected_datetime": "collected",
}


@pytest.fixture
def lab_conn():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE labs (lab_result_id TEXT, patient_id TEXT, test_name TEXT, "
        "value NUMERIC, unit TEXT, collected TEXT)"
    )
    connection.executemany(
        "INSERT INTO labs VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("L1", "MRN-1", "CRP", 12.5, "mg/L", "2024-01-10"),
            ("L2", "MRN-1", "Hb", 11, "g/dL", "2024-01-10"),
            ("L3", "MRN-2", "CRP", 3, "mg/L", "2024-01-11"),
            ("L4", "MRN-404", "CRP", 3, "mg/L", "2024-01-11"),
            ("L1", "MRN-2", "CRP", 4, "mg/L", "2024-01-12"),
            ("L6", "MRN-2", "Na", None, "mmol/L", "2024-01-12"),
        ],
    )
    connection.commit()
    yield connection
    connection.close()


def _seed_lab_patients(client, *identifiers):
    client._fhir.seed_bundle(
        [
            {
                "resourceType": "Patient",
                "identifier": [{"system": "urn:cavell:patient", "value": i}],
            }
            for i in identifiers
        ]
    )


class TestLabsFromDatabase:
    def _ingest(self, client, conn, **kw):
        return LabIngestionPipeline(client).ingest_from_database(
            DatabaseSource(conn, table="labs"), columns=LAB_MAPPING, **kw
        )

    def test_first_run_ingests_and_reports_rejections_by_query_row(
        self, client, lab_conn, tmp_path
    ):
        _seed_lab_patients(client, "MRN-1", "MRN-2")
        path = tmp_path / "lab_failures.jsonl"
        outcome = self._ingest(client, lab_conn, chunk_size=2, failures_path=path)

        assert outcome.total == 6
        assert outcome.accepted == 3
        rejected = {r.index: r for r in outcome.rejected}
        assert rejected[3].category == "reference"
        assert "unknown patient 'MRN-404'" in rejected[3].reason
        assert rejected[4].category == "data"
        assert "duplicate lab_result_id (first used by row 0)" in rejected[4].reason
        assert rejected[5].reason == "value must be non-empty"
        lines = [json.loads(x) for x in path.read_text().splitlines()]
        assert sorted(r["row"] for r in lines) == [3, 4, 5]
        # Numbers from a NUMERIC column reach the API as their text.
        sent = [row for request in client._api.lab_requests for row in request]
        assert {r["lab_result_id"]: r["value"] for r in sent}["L1"] == "12.5"

    def test_rerun_sends_nothing_already_ingested(self, client, lab_conn):
        _seed_lab_patients(client, "MRN-1", "MRN-2")
        self._ingest(client, lab_conn)
        requests_before = len(client._api.lab_requests)

        outcome = self._ingest(client, lab_conn)

        assert outcome.skipped_already_ingested == 3
        assert outcome.accepted == 0
        # The still-bad rows are reported again — they are not stored. The
        # repeated L1 stays a duplicate even though its first use is stored.
        assert len(outcome.rejected) == 3
        assert (
            "already stored for another patient"
            in {r.index: r for r in outcome.rejected}[4].reason
        )
        sent = [r for req in client._api.lab_requests[requests_before:] for r in req]
        assert all(r["lab_result_id"] != "L1" for r in sent)

    def test_appended_rows_only(self, client, lab_conn):
        _seed_lab_patients(client, "MRN-1", "MRN-2")
        self._ingest(client, lab_conn)
        lab_conn.execute(
            "INSERT INTO labs VALUES ('L7', 'MRN-1', 'K', 4.1, 'mmol/L', '2024-02-01')"
        )
        lab_conn.commit()
        requests_before = len(client._api.lab_requests)

        outcome = self._ingest(client, lab_conn)

        assert outcome.accepted == 1
        sent = [
            r["lab_result_id"]
            for req in client._api.lab_requests[requests_before:]
            for r in req
        ]
        assert sent == ["L7"]
