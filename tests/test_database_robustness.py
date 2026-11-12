"""Breaking points of the database pipelines.

Each test pushes one thing the real world does to a long-running extraction:
rows changing under the run, NULLs and awkward values in key columns, the
database / FHIR server / Prism failing part-way, bad configuration, and
re-runs over all of it. The invariant everywhere: every good row is extracted
exactly once across runs, and every other row is reported, every run, with a
reason.
"""

# Pytest fixtures imported from test_database_pipeline are "redefined" as
# test parameters — that is how pytest injects them.
# ruff: noqa: F811

import datetime
import random
import sqlite3
from collections import Counter

import httpx
import pytest

from cavell_client import DatabaseSource, IngestionPipeline, LabIngestionPipeline
from cavell_client.models import CavellAuthError
from tests.fakes import prism_error
from tests.test_database_pipeline import (  # noqa: F401 - fixtures
    DOCUMENT_MAPPING,
    LAB_MAPPING,
    NOTE_COLUMNS,
    _insert,
    _no_backoff,
    _note,
    _pipeline,
    _run,
    _seed_lab_patients,
    client,
    conn,
    lab_conn,
)


def _extracted_once(client) -> Counter:
    counts = Counter(client._api.extracted)
    assert all(n == 1 for n in counts.values()), counts
    return counts


class TestRowsChangingUnderTheRun:
    def _after_the_scan(self, monkeypatch, action):
        """Run ``action`` between the key scan and the first text fetch."""
        original = IngestionPipeline._pending_documents

        def pending(self, *args, **kwargs):
            out = original(self, *args, **kwargs)
            action()
            return out

        monkeypatch.setattr(IngestionPipeline, "_pending_documents", pending)

    def test_row_deleted_after_the_scan_is_skipped_not_failed(
        self, client, conn, monkeypatch, caplog
    ):
        def delete():
            conn.execute("DELETE FROM notes WHERE note_id = 'n2'")
            conn.commit()

        self._after_the_scan(monkeypatch, delete)
        report = _run(client, conn)
        assert report.extracted == 4
        assert report.failed == [] and report.rejected == []
        assert "1 row(s) disappeared between the key scan and the fetch" in caplog.text
        monkeypatch.undo()
        assert _run(client, conn).pending == 0

    def test_row_blanked_after_the_scan_is_rejected(self, client, conn, monkeypatch):
        def blank():
            conn.execute("UPDATE notes SET note_text = '' WHERE note_id = 'n3'")
            conn.commit()

        self._after_the_scan(monkeypatch, blank)
        report = _run(client, conn)
        assert [(r.document_id, r.reason) for r in report.rejected] == [
            ("n3", "Document.text must not be empty")
        ]
        assert report.extracted == 4


class TestAwkwardKeys:
    def test_null_keys_and_dates_are_rejected_with_a_reason(self, client, conn):
        conn.executemany(
            f"INSERT INTO notes ({', '.join(NOTE_COLUMNS)}) VALUES "
            f"({', '.join('?' for _ in NOTE_COLUMNS)})",
            [
                tuple(None if c == "patient_id" else v for c, v in _row("x1").items()),
                tuple(None if c == "note_id" else v for c, v in _row("x2").items()),
                tuple(None if c == "note_date" else v for c, v in _row("x3").items()),
            ],
        )
        conn.commit()
        report = _run(client, conn)
        reasons = sorted(r.reason for r in report.rejected)
        assert len(reasons) == 3
        assert "Document.document_id must not be empty" in reasons
        assert "Document.patient_identifier must not be empty" in reasons
        assert any("Invalid date format" in r for r in reasons)
        assert report.extracted == 5

    def test_quotes_commas_unicode_and_sql_in_ids(self, client, conn):
        ids = ["it's", 'say "hi"', "a,b", "naïve-é", "x; DROP TABLE notes", "50%", "?"]
        _insert(conn, [_note(i, f"P-{i}", "2024-06-01") for i in ids])
        first = _run(client, conn)
        assert first.extracted == 5 + len(ids)
        assert _run(client, conn).extracted == 0
        assert set(ids) <= set(_extracted_once(client))

    def test_integer_key_columns(self, client):
        db = sqlite3.connect(":memory:")
        db.execute(
            "CREATE TABLE notes (note_id INTEGER, patient_id INTEGER, note_date TEXT, "
            "note_text TEXT)"
        )
        db.executemany(
            "INSERT INTO notes VALUES (?, ?, ?, ?)",
            [
                (n, 100 + n % 3, "2024-01-01", f"Note number {n} text.")
                for n in range(9)
            ],
        )
        mapping = {
            "document_id": "note_id",
            "patient_identifier": "patient_id",
            "date": "note_date",
            "text": "note_text",
        }

        def run():
            return _pipeline(client).extract_from_database(
                DatabaseSource(db, table="notes"), columns=mapping
            )

        assert run().extracted == 9
        assert run().extracted == 0
        db.execute("INSERT INTO notes VALUES (42, 101, '2024-02-01', 'A new note.')")
        assert run().extracted == 1
        assert "42" in _extracted_once(client)

    def test_timestamp_columns_become_the_note_date(self, client):
        db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        db.execute(
            "CREATE TABLE notes (note_id TEXT, patient_id TEXT, note_date TIMESTAMP, "
            "note_text TEXT)"
        )
        db.execute(
            "INSERT INTO notes VALUES (?, ?, ?, ?)",
            ("t1", "MRN-1", datetime.datetime(2024, 3, 5, 23, 30), "Late night note."),
        )
        report = _pipeline(client).extract_from_database(
            DatabaseSource(db, table="notes"),
            columns={
                "document_id": "note_id",
                "patient_identifier": "patient_id",
                "date": "note_date",
                "text": "note_text",
            },
        )
        assert report.extracted == 1
        assert list(client._fhir.documents.values()) == ["2024-03-05"]


class TestDuplicates:
    def test_identical_copy_is_extracted_once_and_reported_every_run(
        self, client, conn
    ):
        _insert(conn, [_note("n1", "MRN-1", "2024-01-10")])
        for _ in range(2):
            report = _run(client, conn)
            assert [r.document_id for r in report.rejected] == ["n1"]
        assert _extracted_once(client)["n1"] == 1

    def test_conflicting_copy_blocks_both_until_fixed(self, client, conn):
        _insert(conn, [_note("n1", "MRN-1", "2024-01-10", text="Different text.")])
        report = _run(client, conn)
        assert "n1" not in client._api.extracted
        assert {r.index for r in report.rejected if r.document_id == "n1"} == {0, 5}
        conn.execute("DELETE FROM notes WHERE note_text = 'Different text.'")
        assert _run(client, conn).extracted == 1
        assert _extracted_once(client)["n1"] == 1


class TestFailuresPartWay:
    def test_database_failure_aborts_and_the_next_run_resumes(
        self, client, conn, monkeypatch, caplog
    ):
        calls = 0
        original = DatabaseSource.fetch_matching

        def flaky(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("server closed the connection")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(DatabaseSource, "fetch_matching", flaky)
        with pytest.raises(sqlite3.OperationalError):
            _run(client, conn, batch_size=2)
        assert len(client._api.extracted) == 2
        assert "extract_from_database ABORTED" in caplog.text

        monkeypatch.undo()
        report = _run(client, conn, batch_size=2)
        assert report.already_extracted == 2 and report.extracted == 3
        assert len(_extracted_once(client)) == 5

    def test_fhir_down_at_start_spends_nothing(self, client, conn, monkeypatch):
        def down():
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(client._fhir, "list_patient_ids", down)
        with pytest.raises(httpx.ConnectError):
            _run(client, conn)
        assert client._api.extracted == []

    def test_prism_key_revoked_mid_run_then_resumed(self, client, conn):
        client._api.failures["n3"] = CavellAuthError(401, "key revoked")
        with pytest.raises(CavellAuthError):
            _run(client, conn, batch_size=2)
        client._api.failures.clear()
        _run(client, conn, batch_size=2)
        assert len(_extracted_once(client)) == 5

    def test_document_failing_every_run_is_reported_every_run(self, client, conn):
        client._api.failures["n2"] = prism_error(422, "unreadable")
        for _ in range(2):
            report = _run(client, conn)
            assert [o.document_id for o in report.failed] == ["n2"]
            assert report.failed[0].category == "prism_rejected"
        assert "n2" not in client._api.extracted


class TestConfiguration:
    def test_unwritable_failures_path_fails_before_any_work(
        self, client, conn, tmp_path
    ):
        blocker = tmp_path / "a-file"
        blocker.write_text("")
        with pytest.raises(OSError):
            _run(client, conn, failures_path=blocker / "failures.jsonl")
        assert client._api.extracted == []
        assert "list_patient_ids" not in client._fhir.calls

    @pytest.mark.parametrize("size", [0, -1])
    def test_bad_batch_sizes(self, client, conn, lab_conn, size):
        with pytest.raises(ValueError, match="batch_size"):
            _run(client, conn, batch_size=size)
        with pytest.raises(ValueError, match="chunk_size"):
            LabIngestionPipeline(client).ingest_from_database(
                DatabaseSource(lab_conn, table="labs"),
                columns=LAB_MAPPING,
                chunk_size=size,
            )

    def test_key_column_as_a_default_is_refused(self, client, conn):
        mapping = {k: v for k, v in DOCUMENT_MAPPING.items() if k != "document_id"}
        with pytest.raises(ValueError, match="cannot be a default"):
            _pipeline(client).extract_from_database(
                DatabaseSource(conn, table="notes"),
                columns={**mapping, "document_id": "note_id"},
                document_id="fixed",
            )


class TestEdgeTables:
    def test_empty_table(self, client, conn):
        conn.execute("DELETE FROM notes")
        report = _run(client, conn)
        assert (report.rows_scanned, report.extracted, report.rejected) == (0, 0, [])
        assert client._api.extracted == []

    def test_every_row_bad(self, client, conn):
        conn.execute("UPDATE notes SET note_date = 'soon'")
        report = _run(client, conn)
        assert len(report.rejected) == 5 and report.extracted == 0
        assert client._api.extracted == []

    def test_many_patients_concurrently_across_fetch_statements(self, client, conn):
        _insert(
            conn,
            [
                _note(f"m{i}", f"P{i % 300}", f"2024-0{1 + i % 9}-15")
                for i in range(700)
            ],
        )
        assert _run(client, conn, batch_size=400).extracted == 705
        assert _run(client, conn).extracted == 0
        assert len(_extracted_once(client)) == 705


class TestLabs:
    def _ingest(self, client, conn, **kw):
        return LabIngestionPipeline(client).ingest_from_database(
            DatabaseSource(conn, table="labs"), columns=LAB_MAPPING, **kw
        )

    def test_rows_are_sent_grouped_by_patient(self, client):
        db = sqlite3.connect(":memory:")
        db.execute(
            "CREATE TABLE labs (lab_result_id, patient_id, test_name, value, unit, "
            "collected)"
        )
        # Interleaved, as a time-ordered feed would be.
        db.executemany(
            "INSERT INTO labs VALUES (?, ?, 'CRP', '1', 'mg/L', '2024-01-01')",
            [(f"L{i}", f"MRN-{i % 3}") for i in range(12)],
        )
        _seed_lab_patients(client, "MRN-0", "MRN-1", "MRN-2")
        outcome = self._ingest(client, db)
        assert outcome.accepted == 12
        assert len(client._api.lab_requests) == 3  # one per patient, not per row

    def test_unseeded_patient_and_null_id_reported_every_run(self, client, lab_conn):
        lab_conn.execute(
            "INSERT INTO labs VALUES (NULL, 'MRN-1', 'CRP', 1, 'mg/L', '2024-01-01')"
        )
        _seed_lab_patients(client, "MRN-1", "MRN-2")
        for _ in range(2):
            outcome = self._ingest(client, lab_conn)
            reasons = {r.index: r.reason for r in outcome.rejected}
            assert reasons[6] == "lab_result_id must be non-empty"
            assert "unknown patient 'MRN-404'" in reasons[3]

    def test_database_failure_mid_run_then_resume(self, client, lab_conn, monkeypatch):
        _seed_lab_patients(client, "MRN-1", "MRN-2")
        original = DatabaseSource.fetch_matching
        calls = 0

        def flaky(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("lost connection")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(DatabaseSource, "fetch_matching", flaky)
        with pytest.raises(sqlite3.OperationalError):
            self._ingest(client, lab_conn, chunk_size=1)
        monkeypatch.undo()
        outcome = self._ingest(client, lab_conn, chunk_size=1)
        assert outcome.accepted + outcome.skipped_already_ingested == 3
        sent = [r["lab_result_id"] for req in client._api.lab_requests for r in req]
        assert sorted(sent) == ["L1", "L2", "L3"]  # each sent once


class TestRandomizedRoundTrip:
    """Whatever text a column holds comes back exactly as a CSV would hold it."""

    ALPHABET = "abcXYZ019 ,;'\"\\%?:\n\t\r-_./é漢🙂"

    def test_values_survive_the_database(self):
        rng = random.Random(20261112)  # noqa: S311 - test data, not secrets
        rows = [
            {
                c: (
                    ""
                    if rng.random() < 0.1
                    else "".join(
                        rng.choice(self.ALPHABET) for _ in range(rng.randint(1, 40))
                    )
                )
                for c in ("a", "b", "c")
            }
            for _ in range(500)
        ]
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE t (a TEXT, b TEXT, c TEXT)")
        db.executemany(
            "INSERT INTO t VALUES (?, ?, ?)",
            [tuple(r[c] or None for c in "abc") for r in rows],
        )
        source = DatabaseSource(db, table="t")
        assert source.rows() == rows
        keys = {(r["a"], r["b"]) for r in rows if r["a"] and r["b"]}
        found = source.fetch_matching(("a", "b"), keys)
        assert set(found) == keys


def _row(note_id: str) -> dict:
    return _note(note_id, "MRN-1", "2024-05-05")
