"""Failure classification and reporting — shared by the CSV and database paths."""

import json
import logging

import httpx
import pytest

import cavell_client.ingestion as ingestion_module
from cavell_client import (
    Document,
    IngestionPipeline,
    LabIngestionPipeline,
    LabResult,
    Organization,
    Patient,
)
from cavell_client.ingestion import _classify_failure
from cavell_client.models import (
    CavellAPIError,
    CavellAuthError,
    CavellGatewayUnavailableError,
    FHIRConnectionError,
)
from cavell_client.reporting import (
    SUMMARY_LOG_LIMIT,
    FailureRecord,
    FailureReporter,
)
from tests.fakes import fake_client, http_error, prism_error


class TestClassifyFailure:
    @pytest.mark.parametrize(
        ("exc", "step", "category", "status"),
        [
            (CavellAuthError(401, "bad key"), "extract", "run_aborted", 401),
            (CavellGatewayUnavailableError(503, "down"), "extract", "run_aborted", 503),
            (CavellAPIError(422, "invalid text"), "extract", "prism_rejected", 422),
            (CavellAPIError(502, "bad gateway"), "extract", "prism_unavailable", 502),
            (CavellAPIError(429, "slow down"), "extract", "prism_unavailable", 429),
            (httpx.ReadTimeout("t"), "extract", "prism_unavailable", None),
            (httpx.ConnectError("refused"), "context", "fhir_unavailable", None),
            (httpx.ReadTimeout("t"), "persist", "fhir_unavailable", None),
            (FHIRConnectionError("down"), "context", "fhir_unavailable", None),
            (KeyError("x"), "extract", "client_error", None),
        ],
    )
    def test_categories(self, exc, step, category, status):
        got_category, got_status, detail = _classify_failure(exc, step)
        assert (got_category, got_status) == (category, status)
        assert detail

    def test_fhir_operation_outcome_diagnostics_become_the_detail(self):
        exc = http_error(
            422,
            {
                "resourceType": "OperationOutcome",
                "issue": [
                    {"diagnostics": "Condition.code: unknown code system"},
                    {"details": {"text": "Observation.status is required"}},
                ],
            },
        )
        category, status, detail = _classify_failure(exc, "persist")
        assert (category, status) == ("fhir_rejected", 422)
        assert detail == (
            "Condition.code: unknown code system; Observation.status is required"
        )

    @pytest.mark.parametrize("status", [500, 503, 401, 403])
    def test_fhir_server_errors_and_auth_are_unavailable(self, status):
        category, _, _ = _classify_failure(
            http_error(status, "upstream error"), "context"
        )
        assert category == "fhir_unavailable"


class TestFailureReporter:
    def _record(self, i=0, **kw):
        return FailureRecord(
            kind="document",
            identifier=f"doc-{i}",
            patient_identifier="MRN-1",
            status="failed",
            category="prism_rejected",
            reason="refused",
            **kw,
        )

    def test_log_line_carries_context_and_action(self):
        line = self._record(
            date="2024-01-02",
            row=7,
            http_status=422,
            attempts=1,
            detail="text empty",
        ).log_line()
        assert line.startswith(
            "FAILED document doc-0 patient=MRN-1 date=2024-01-02 row=7 "
            "category=prism_rejected http=422 attempts=1: refused | detail: text empty"
        )
        assert "-> Prism refused this input" in line

    def test_summary_is_capped_and_points_at_the_file(self, tmp_path, caplog):
        path = tmp_path / "f.jsonl"
        reporter = FailureReporter(path)
        reporter.add([self._record(i) for i in range(SUMMARY_LOG_LIMIT + 5)])
        with caplog.at_level(logging.ERROR):
            reporter.log_summary("run")
        assert "... and 5 more (see" in caplog.text
        assert len(path.read_text().splitlines()) == SUMMARY_LOG_LIMIT + 5

    def test_aborted_run_logs_at_error_even_without_failures(self, caplog):
        reporter = FailureReporter()
        with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
            with reporter.summarize(lambda e: f"run ABORTED by {e}"):
                raise RuntimeError("db gone")
        assert "run ABORTED by db gone — no failures" in caplog.text

    def test_unwritable_path_fails_at_once(self, tmp_path):
        (tmp_path / "file").write_text("")
        with pytest.raises(OSError):
            FailureReporter(tmp_path / "file" / "f.jsonl")

    def test_no_failures_logs_info(self, caplog):
        with caplog.at_level(logging.INFO):
            FailureReporter().log_summary("run: 3 extracted")
        assert "run: 3 extracted — no failures" in caplog.text


class TestCsvPathReporting:
    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch):
        monkeypatch.setattr(ingestion_module.time, "sleep", lambda s: None)

    @pytest.mark.parametrize("method", ["extract_all", "extract"])
    def test_reports_from_rows_rejections_and_failures(self, tmp_path, caplog, method):
        client = fake_client()
        pipeline = IngestionPipeline(client, default_organization="ORG-1")
        pipeline.seed(
            organizations=[Organization(identifier="ORG-1", name="Hospital")],
            patients=[Patient(identifier="MRN-1")],
        )
        rows = [
            {
                "id": "a",
                "pid": "MRN-1",
                "d": "2024-01-01",
                "t": "A valid clinical note.",
            },
            {"id": "b", "pid": "MRN-1", "d": "2024-01-02", "t": ""},
            {
                "id": "c",
                "pid": "MRN-9",
                "d": "2024-01-03",
                "t": "Unknown patient note.",
            },
            {"id": "d", "pid": "MRN-1", "d": "2024-01-04", "t": "Prism refuses this."},
        ]
        docs = Document.from_rows(
            rows,
            columns={
                "document_id": "id",
                "patient_identifier": "pid",
                "date": "d",
                "text": "t",
            },
        )
        client._api.failures["d"] = prism_error(422, "unsupported language")
        path = tmp_path / "failures.jsonl"

        with caplog.at_level(logging.ERROR):
            outcomes = getattr(pipeline, method)(docs, failures_path=path)

        records = {
            r["identifier"]: r for r in map(json.loads, path.read_text().splitlines())
        }
        assert records["b"]["category"] == "data" and records["b"]["row"] == 1
        assert records["c"]["category"] == "reference"
        assert records["d"]["category"] == "prism_rejected"
        assert records["d"]["detail"] == "unsupported language"
        assert [o.document_id for o in outcomes if o.success] == ["a"]
        assert f"{method}: 1 extracted" in caplog.text
        assert "3 row(s) need attention" in caplog.text
        assert pipeline.documents_rejected == 2  # the from_rows row counts too
        assert pipeline.documents_failed == 1

    def test_lab_ingest_logs_and_writes_rejections(self, tmp_path, caplog):
        client = fake_client()
        client._fhir.seed_bundle(
            [
                {
                    "resourceType": "Patient",
                    "identifier": [{"system": "urn:cavell:patient", "value": "MRN-1"}],
                }
            ]
        )
        results = [
            LabResult(
                patient_identifier="MRN-1",
                test_name="CRP",
                value="5",
                collected_datetime="2024-01-01",
                lab_result_id="L1",
            ),
            LabResult(
                patient_identifier="MRN-1",
                test_name="CRP",
                value="",
                collected_datetime="2024-01-01",
                lab_result_id="L2",
            ),
        ]
        path = tmp_path / "labs.jsonl"
        with caplog.at_level(logging.ERROR):
            outcome = LabIngestionPipeline(client).ingest(results, failures_path=path)
        assert outcome.accepted == 1
        assert "REJECTED lab_result L2 patient=MRN-1 row=1 category=data" in caplog.text
        record = json.loads(path.read_text())
        assert record["identifier"] == "L2"
        assert record["reason"] == "value must be non-empty"


class TestDocumentDates:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2024-01-15", "2024-01-15"),
            ("20240115", "2024-01-15"),
            (" 2024-01-15 ", "2024-01-15"),
            ("2024-01-15T10:30:00", "2024-01-15"),
            ("2024-01-15 10:30:00", "2024-01-15"),
            # Kept as written — not converted to UTC (which would be the 16th).
            ("2024-01-15T23:30:00-05:00", "2024-01-15"),
            ("2024-01-15T00:15:00+02:00", "2024-01-15"),
        ],
    )
    def test_accepts_iso_dates_and_datetimes(self, value, expected):
        doc = Document(
            text="A clinical note.",
            patient_identifier="MRN-1",
            date=value,
            document_id="d",
        )
        assert doc.date == expected

    @pytest.mark.parametrize("value", ["15-01-2024", "15/01/2024", "", "not-a-date"])
    def test_still_rejects_non_iso(self, value):
        with pytest.raises(ValueError, match="Invalid date format"):
            Document(
                text="A clinical note.",
                patient_identifier="MRN-1",
                date=value,
                document_id="d",
            )
