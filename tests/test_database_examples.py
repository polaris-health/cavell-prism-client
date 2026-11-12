"""The shipped demo CSVs, loaded into a local SQLite database.

Proves the database path is a drop-in for the CSV path on real-shaped data:
the same rows, the same objects, and a full incremental run — first run,
re-run, appended rows — through both pipelines (Prism and FHIR are the
stateful in-memory fakes from tests/fakes.py).
"""

import csv
import logging
import sqlite3
from pathlib import Path

import pytest

import cavell_client.ingestion as ingestion_module
from cavell_client import (
    DatabaseSource,
    Document,
    IngestionPipeline,
    LabIngestionPipeline,
    LabResult,
    Organization,
    Patient,
    Practitioner,
)
from tests.fakes import fake_client
from tests.helpers import load_table

NOTEBOOKS = Path(__file__).resolve().parent.parent / "docs" / "notebooks"
NOTE_FILES = ("notes.csv", "hospitalizations.csv")
LAB_FILES = ("lab_results.csv", "notes_lab_results.csv")

DOCUMENT_COLUMNS = {
    "text": "note_text",
    "patient_identifier": "patient_id",
    "date": "note_date",
    "document_id": "note_id",
    "encounter_id": "encounter_id",
    "meta": {"Department": "department"},
    "practitioner_identifier": "practitioner_id",
}
PATIENT_COLUMNS = {
    "name": "patient_name",
    "birth_date": "birth_date",
    "gender": "gender",
}
LAB_COLUMNS = {
    "lab_result_id": "lab_result_id",
    "patient_identifier": "patient_id",
    "test_name": "test_name",
    "value": "value",
    "collected_datetime": "collected_datetime",
    "loinc_code": "loinc_code",
    "unit": "unit",
    "reference_low": "reference_low",
    "reference_high": "reference_high",
}
ORG_ID = "CGH-001"


def read_csv(name: str) -> list[dict[str, str]]:
    with (NOTEBOOKS / name).open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def db_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("db") / "clinical.db"
    conn = sqlite3.connect(path)
    for name in (*NOTE_FILES, *LAB_FILES):
        load_table(conn, read_csv(name), name.removesuffix(".csv"))
    conn.close()
    return path


@pytest.fixture(params=["dbapi", "sqlalchemy"])
def source_for(request, db_path):
    """Build a DatabaseSource for a table, via sqlite3 or a SQLAlchemy URL."""
    opened = []

    def make(table: str) -> DatabaseSource:
        if request.param == "dbapi":
            conn = sqlite3.connect(db_path)
            opened.append(conn)
            return DatabaseSource(conn, table=table)
        source = DatabaseSource(f"sqlite:///{db_path}", table=table)
        opened.append(source)
        return source

    yield make
    for item in opened:
        item.close()


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, caplog):
    monkeypatch.setattr(ingestion_module.time, "sleep", lambda s: None)
    caplog.set_level(logging.ERROR)


class TestSameAsCsv:
    @pytest.mark.parametrize("name", [*NOTE_FILES, *LAB_FILES])
    def test_rows_equal_the_csv(self, source_for, name):
        assert source_for(name.removesuffix(".csv")).rows() == read_csv(name)

    @pytest.mark.parametrize("name", NOTE_FILES)
    def test_same_objects_from_both_paths(self, source_for, name):
        csv_rows = read_csv(name)
        db_rows = source_for(name.removesuffix(".csv")).rows()

        def build(rows):
            return (
                Document.from_rows(
                    rows, columns=DOCUMENT_COLUMNS, organization_identifier=ORG_ID
                ),
                Patient.from_rows(
                    rows,
                    columns={"identifier": "patient_id", **PATIENT_COLUMNS},
                    managing_organization=ORG_ID,
                ),
                Practitioner.from_rows(
                    rows,
                    columns={
                        "identifier": "practitioner_id",
                        "name": "practitioner_name",
                    },
                    organization_identifier=ORG_ID,
                ),
            )

        csv_docs, csv_patients, csv_pracs = build(csv_rows)
        db_docs, db_patients, db_pracs = build(db_rows)
        assert db_docs == csv_docs
        assert db_docs.rejected == csv_docs.rejected
        assert db_patients == csv_patients
        assert db_pracs == csv_pracs

    @pytest.mark.parametrize("name", LAB_FILES)
    def test_same_lab_results_from_both_paths(self, source_for, name):
        db_rows = source_for(name.removesuffix(".csv")).rows()
        assert LabResult.from_rows(db_rows, columns=LAB_COLUMNS) == LabResult.from_rows(
            read_csv(name), columns=LAB_COLUMNS
        )


def _seeded_pipeline(client, practitioner_rows):
    pipeline = IngestionPipeline(client, default_organization=ORG_ID, max_concurrency=8)
    pipeline.seed(
        organizations=[Organization(identifier=ORG_ID, name="City General Hospital")],
        patients=[],
        practitioners=Practitioner.from_rows(
            practitioner_rows,
            columns={"identifier": "practitioner_id", "name": "practitioner_name"},
            organization_identifier=ORG_ID,
        ),
    )
    return pipeline


class TestNotesEndToEnd:
    def test_first_run_rerun_and_appended_rows(self, tmp_path):
        rows = read_csv("notes.csv")
        path = tmp_path / "clinical.db"
        conn = sqlite3.connect(path)
        load_table(conn, rows, "notes")
        client = fake_client()

        def run():
            pipeline = _seeded_pipeline(
                client, DatabaseSource(conn, query="SELECT * FROM notes").rows()
            )
            return pipeline.extract_from_database(
                DatabaseSource(conn, table="notes"),
                columns=DOCUMENT_COLUMNS,
                patient_columns=PATIENT_COLUMNS,
                patient_defaults={"managing_organization": ORG_ID},
                batch_size=500,
                failures_path=tmp_path / "failures.jsonl",
            )

        # The CSV path's view of the same data, for the expected numbers.
        expected = Document.from_rows(
            rows, columns=DOCUMENT_COLUMNS, organization_identifier=ORG_ID
        )

        first = run()
        assert first.rows_scanned == len(rows)
        assert first.extracted + first.duplicates_skipped == len(expected)
        assert len(first.rejected) == len(expected.rejected)
        assert first.failed == []
        assert len(client._api.extracted) == first.extracted
        patients = {r["patient_id"] for r in rows}
        assert set(client._fhir.list_patient_ids()) == patients

        second = run()
        assert second.extracted == 0
        assert second.already_extracted == first.extracted
        assert len(client._api.extracted) == first.extracted

        template = rows[0]
        new_rows = [
            {
                **template,
                "note_id": "db-new-1",
                "note_date": "2026-07-01",
                "note_text": "New outpatient note: blood pressure well controlled.",
            },
            {
                **template,
                "note_id": "db-new-2",
                "patient_id": "MRN-DB-NEW",
                "patient_name": "New Patient",
                "note_date": "2026-07-02",
                "note_text": "First visit for a brand-new patient, no complaints.",
            },
            {
                **template,
                "note_id": "db-new-3",
                "note_date": "2026-07-03",
                "note_text": "",
            },
        ]
        cols = list(rows[0])
        conn.executemany(
            f"INSERT INTO notes VALUES ({', '.join('?' for _ in cols)})",
            [tuple(r[c] or None for c in cols) for r in new_rows],
        )
        conn.commit()

        third = run()
        assert third.extracted == 2
        assert client._api.extracted[-2:] == ["db-new-1", "db-new-2"]
        assert "MRN-DB-NEW" in client._fhir.list_patient_ids()
        new_rejections = [r for r in third.rejected if r.document_id == "db-new-3"]
        assert len(new_rejections) == 1
        assert new_rejections[0].category == "data"
        assert new_rejections[0].index == len(rows) + 2
        assert "db-new-3" in (tmp_path / "failures.jsonl").read_text()
        conn.close()


class TestLabsEndToEnd:
    def test_first_run_rerun_and_appended_rows(self, tmp_path):
        rows = read_csv("notes_lab_results.csv")
        conn = sqlite3.connect(tmp_path / "labs.db")
        load_table(conn, rows, "labs")
        client = fake_client()
        # The lab pipeline never creates patients: seed them as the notes
        # pipeline would have.
        client._fhir.seed_bundle(
            [
                {
                    "resourceType": "Patient",
                    "identifier": [{"system": "urn:cavell:patient", "value": p}],
                }
                for p in sorted({r["patient_id"] for r in rows})
            ]
        )
        pipeline = LabIngestionPipeline(client)

        def run():
            return pipeline.ingest_from_database(
                DatabaseSource(conn, table="labs"), columns=LAB_COLUMNS, chunk_size=1000
            )

        csv_outcome_rows = LabResult.from_rows(rows, columns=LAB_COLUMNS)
        first = run()
        assert first.total == len(rows)
        assert first.accepted + len(first.rejected) == len(csv_outcome_rows)

        requests_before = len(client._api.lab_requests)
        second = run()
        assert second.skipped_already_ingested == first.accepted
        assert second.accepted == 0
        assert len(client._api.lab_requests) == requests_before

        template = rows[0]
        conn.execute(
            f"INSERT INTO labs ({', '.join(template)}) VALUES "
            f"({', '.join('?' for _ in template)})",
            tuple({**template, "lab_result_id": "LAB-DB-NEW"}.values()),
        )
        conn.commit()
        third = run()
        assert third.accepted == 1
        assert [r["lab_result_id"] for r in client._api.lab_requests[-1]] == [
            "LAB-DB-NEW"
        ]
        conn.close()
