"""Tests for the ingestion pipeline."""

import datetime
import json
import logging
from urllib.parse import quote

import httpx
import pytest

from cavell_client.fhir import (
    _PATIENT_SEARCH_PARAM,
    CONTEXT_RESOURCE_TYPES,
    IDENTIFIER_SYSTEM,
    ORGANIZATION_IDENTIFIER_SYSTEM,
    PRACTITIONER_IDENTIFIER_SYSTEM,
)
from cavell_client.ingestion import (
    _DOC_MAX_ATTEMPTS,
    Document,
    IngestionOutcome,
    IngestionPipeline,
    Organization,
    Patient,
    Practitioner,
    _dedupe_documents_by_content,
    _Phase,
)
from tests.helpers import (
    mock_api_preflight,
    mock_fhir_auth,
    mock_related_documents,
    mock_watermark,
)

IDENTIFIER_SYSTEM_ENCODED = quote(IDENTIFIER_SYSTEM, safe="")
ORG_SYSTEM_ENCODED = quote(ORGANIZATION_IDENTIFIER_SYSTEM, safe="")


def mock_seed_response(httpx_mock, entries):
    """Mock a seed_bundle POST response.

    entries: list of (status, location) tuples, e.g.
        [("201 Created", "Organization/org-1/_history/1")]
    """
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/fhir/",
        json={
            "resourceType": "Bundle",
            "type": "transaction-response",
            "entry": [
                {"response": {"status": status, "location": location}}
                for status, location in entries
            ],
        },
    )


def mock_context_empty(httpx_mock, patient_fhir_id, repeat=False):
    """Mock ALL clinical context fetches (empty results).

    Driven off ``CONTEXT_RESOURCE_TYPES`` rather than a hand-kept list. The
    context fetch swallows per-type errors by design, so a type added to the
    SDK but missed here would not fail a test — it would just quietly stop
    being exercised, which is how two context types went unnoticed before.
    """
    special = {"Observation", "CarePlan"}
    for rt in CONTEXT_RESOURCE_TYPES:
        if rt in special:
            continue
        param = _PATIENT_SEARCH_PARAM.get(rt, "subject")
        httpx_mock.add_response(
            method="GET",
            url=f"http://localhost:8080/fhir/{rt}?{param}={patient_fhir_id}&_count=500",
            json={"entry": []},
            repeat=repeat,
        )
    # The window cutoff comes from each document's date, so match on a prefix.
    httpx_mock.add_response(
        method="GET",
        url_prefix=(
            f"http://localhost:8080/fhir/Observation?subject={patient_fhir_id}"
            f"&_count=50&date=ge"
        ),
        json={"entry": []},
        repeat=True,
    )
    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8080/fhir/ResearchStudy?_count=500&_sort=-_lastUpdated",
        json={"entry": []},
        repeat=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=(
            f"http://localhost:8080/fhir/CarePlan?subject={patient_fhir_id}"
            f"&_count=500&status=active"
        ),
        json={"entry": []},
        repeat=repeat,
    )


def _extract_body(httpx_mock):
    """Decode the body of the most recent POST /extract/text."""
    extract_request = [
        r
        for r in httpx_mock.get_requests()
        if r.method == "POST" and "extract/text" in str(r.url)
    ][-1]
    return json.loads(extract_request.content)


def mock_context_split_empty(httpx_mock, patient_fhir_id, repeat=False):
    """Mock the split-context fetches an out-of-order document makes.

    Everything ``mock_context_empty`` covers, plus the provenance query that
    classifies each resource and the second (future-side) Observation query.
    The Observation prefix in ``mock_context_empty`` does not match the future
    query — that one sorts ascending and bounds with ``gt`` — so it is
    registered here rather than inherited.
    """
    mock_context_empty(httpx_mock, patient_fhir_id, repeat=repeat)
    mock_related_documents(httpx_mock, patient_fhir_id)
    httpx_mock.add_response(
        method="GET",
        url_prefix=(
            f"http://localhost:8080/fhir/Observation?subject={patient_fhir_id}"
            f"&_count=50&date=gt"
        ),
        json={"entry": []},
        repeat=True,
    )


def mock_extract_response(
    httpx_mock,
    count=1,
    estimated_cost=None,
    extraction_status=None,
    failed_extractors=None,
):
    """Mock a Cavell API extraction response."""
    entries = []
    for _ in range(count):
        entries.append(
            {
                "resource": {
                    "resourceType": "Condition",
                    "subject": {"reference": "urn:uuid:patient"},
                },
                "request": {"method": "POST", "url": "Condition"},
            }
        )
    body = {
        "bundle": {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": entries,
        },
        "count": count,
    }
    if estimated_cost is not None:
        body["usage"] = {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "requests": 1,
            "estimated_cost": estimated_cost,
        }
    if extraction_status is not None:
        body["extraction_status"] = extraction_status
    if failed_extractors is not None:
        body["failed_extractors"] = failed_extractors
    httpx_mock.add_response(
        method="POST",
        url="https://qa.prism.cavell.app/api/extract/text",
        json=body,
    )


def mock_persist_response(httpx_mock, created=1):
    """Mock a FHIR bundle persist response."""
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/fhir/",
        json={
            "resourceType": "Bundle",
            "type": "transaction-response",
            "entry": [{"response": {"status": "201 Created"}} for _ in range(created)],
        },
    )


def mock_patient_exists(httpx_mock, patient_fhir_ids, repeat=False):
    """Mock GET /Patient/{id} returning 200 for each patient.

    Pass repeat=True when a test makes several extract() calls — each one
    re-verifies every patient in its batch.
    """
    if isinstance(patient_fhir_ids, str):
        patient_fhir_ids = [patient_fhir_ids]
    for pid in patient_fhir_ids:
        httpx_mock.add_response(
            method="GET",
            url=f"http://localhost:8080/fhir/Patient/{pid}",
            json={"resourceType": "Patient", "id": pid},
            repeat=repeat,
        )


def mock_empty_document_identifiers(httpx_mock, patient_fhir_ids=("pat-1",)):
    """Mock the extract() plumbing: API pre-flight, per-patient
    DocumentReference resume queries, and chronology watermarks (all empty)."""
    mock_api_preflight(httpx_mock)
    if isinstance(patient_fhir_ids, str):
        patient_fhir_ids = [patient_fhir_ids]
    for pid in patient_fhir_ids:
        httpx_mock.add_response(
            method="GET",
            url=(
                f"http://localhost:8080/fhir/DocumentReference?patient={pid}"
                f"&identifier=urn%3Acavell%3Adocument%7C&_elements=identifier"
                f"&_count=1000"
            ),
            json={"resourceType": "Bundle", "entry": []},
            repeat=True,
        )
        mock_watermark(httpx_mock, pid)


class TestDocumentValidation:
    """Test Document dataclass validation."""

    def test_date_accepts_iso_string(self):
        doc = Document(
            text="t",
            patient_identifier="MRN-1",
            date="2024-01-15",
            document_id="doc-1",
        )
        assert doc.date == "2024-01-15"

    def test_date_accepts_datetime_date(self):
        doc = Document(
            text="t",
            patient_identifier="MRN-1",
            date=datetime.date(2024, 1, 15),
            document_id="doc-1",
        )
        assert doc.date == "2024-01-15"

    def test_date_accepts_datetime_datetime(self):
        doc = Document(
            text="t",
            patient_identifier="MRN-1",
            date=datetime.datetime(2024, 1, 15, 10, 30),
            document_id="doc-1",
        )
        assert doc.date == "2024-01-15"

    def test_date_rejects_bad_format(self):
        with pytest.raises(ValueError, match="Invalid date format"):
            Document(
                text="t",
                patient_identifier="MRN-1",
                date="15-01-2024",
                document_id="doc-1",
            )

    def test_date_rejects_garbage(self):
        with pytest.raises(ValueError, match="Invalid date format"):
            Document(
                text="t",
                patient_identifier="MRN-1",
                date="not-a-date",
                document_id="doc-1",
            )

    def test_organization_identifier_optional(self):
        doc = Document(
            text="t",
            patient_identifier="MRN-1",
            date="2024-01-15",
            document_id="doc-1",
        )
        assert doc.organization_identifier is None


class TestDefaultOrganization:
    """Test default_organization on IngestionPipeline."""

    def test_default_organization_fills_in(self, client, httpx_mock):
        """Documents without org_identifier use the pipeline default."""
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])
        mock_patient_exists(httpx_mock, "pat-1")

        pipeline = IngestionPipeline(client, default_organization="CGH-001")
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[Patient(identifier="MRN-1")],
        )

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        # Document has no organization_identifier — should use default
        outcomes = []
        for outcome in pipeline.extract(
            [
                Document(
                    text="test",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    document_id="doc-1",
                ),
            ]
        ):
            outcomes.append(outcome)

        assert len(outcomes) == 1
        assert outcomes[0].success

    def test_no_org_no_default_raises(self, client, httpx_mock):
        """Document without org_identifier and no default raises ValueError."""
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client)  # no default_organization
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[Patient(identifier="MRN-1")],
        )

        with pytest.raises(ValueError, match="no organization_identifier"):
            for _ in pipeline.extract(
                [
                    Document(
                        text="test",
                        patient_identifier="MRN-1",
                        date="2024-01-01",
                        document_id="doc-1",
                    ),
                ]
            ):
                pass

    def test_explicit_org_overrides_default(self, client, httpx_mock):
        """Explicit org on document takes precedence over default."""
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock,
            [
                ("201 Created", "Organization/org-1/_history/1"),
                ("201 Created", "Organization/org-2/_history/1"),
            ],
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])
        mock_patient_exists(httpx_mock, "pat-1")

        pipeline = IngestionPipeline(client, default_organization="CGH-001")
        pipeline.seed(
            organizations=[
                Organization(identifier="CGH-001", name="City General"),
                Organization(identifier="SMH-002", name="St. Mary's"),
            ],
            patients=[Patient(identifier="MRN-1")],
        )

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        outcomes = []
        for outcome in pipeline.extract(
            [
                Document(
                    text="test",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="SMH-002",
                    document_id="doc-1",
                ),
            ]
        ):
            outcomes.append(outcome)

        assert len(outcomes) == 1
        assert outcomes[0].success

        # Verify the API got the explicit org, not the default
        requests = httpx_mock.get_requests()
        extract_req = [r for r in requests if "extract/text" in str(r.url)][-1]
        body = json.loads(extract_req.content)
        assert body["organization_id"] == "org-2"


class TestPhaseGuards:
    """Test that methods enforce correct phase ordering."""

    def test_extract_before_seed_raises(self, client):
        pipeline = IngestionPipeline(client)
        with pytest.raises(RuntimeError, match="patients_seeded"):
            for _ in pipeline.extract(
                [
                    Document(
                        text="test",
                        patient_identifier="MRN-1",
                        date="2024-01-01",
                        organization_identifier="CGH-001",
                        document_id="doc-1",
                    ),
                ]
            ):
                pass

    def test_seed_twice_raises(self, client, httpx_mock):
        mock_fhir_auth(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[Patient(identifier="MRN-1")],
        )

        with pytest.raises(RuntimeError, match="created"):
            pipeline.seed(
                organizations=[Organization(identifier="CGH-002", name="Another")],
                patients=[Patient(identifier="MRN-2")],
            )


class TestCrossValidation:
    """Test cross-validation between phases."""

    def test_patient_unknown_org_raises(self, client):
        pipeline = IngestionPipeline(client)
        with pytest.raises(ValueError, match="unknown organization"):
            pipeline.seed(
                organizations=[Organization(identifier="CGH-001", name="City General")],
                patients=[
                    Patient(identifier="MRN-1", managing_organization="UNKNOWN"),
                ],
            )

    def test_document_unknown_patient_raises(self, client, httpx_mock):
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[Patient(identifier="MRN-1")],
        )

        with pytest.raises(ValueError, match="unknown patient"):
            for _ in pipeline.extract(
                [
                    Document(
                        text="test",
                        patient_identifier="UNKNOWN-MRN",
                        date="2024-01-01",
                        organization_identifier="CGH-001",
                        document_id="doc-1",
                    ),
                ]
            ):
                pass

    def test_document_unknown_org_raises(self, client, httpx_mock):
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[Patient(identifier="MRN-1")],
        )

        with pytest.raises(ValueError, match="unknown organization"):
            for _ in pipeline.extract(
                [
                    Document(
                        text="test",
                        patient_identifier="MRN-1",
                        date="2024-01-01",
                        organization_identifier="UNKNOWN-ORG",
                        document_id="doc-1",
                    ),
                ]
            ):
                pass

    def test_duplicate_document_id_raises(self, client, httpx_mock):
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[Patient(identifier="MRN-1")],
        )

        with pytest.raises(ValueError, match="Duplicate document_id.*note-001"):
            for _ in pipeline.extract(
                [
                    Document(
                        text="First note with some clinical content",
                        patient_identifier="MRN-1",
                        date="2024-01-01",
                        organization_identifier="CGH-001",
                        document_id="note-001",
                    ),
                    Document(
                        text="Second note with different content",
                        patient_identifier="MRN-1",
                        date="2024-01-02",
                        organization_identifier="CGH-001",
                        document_id="note-001",
                    ),
                ]
            ):
                pass

    def test_short_text_warns(self, client, httpx_mock, caplog):
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])
        mock_patient_exists(httpx_mock, "pat-1")

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[Patient(identifier="MRN-1")],
        )

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=0)
        mock_persist_response(httpx_mock, created=0)

        with caplog.at_level(logging.WARNING):
            for _ in pipeline.extract(
                [
                    Document(
                        text="Short note here.",
                        patient_identifier="MRN-1",
                        date="2024-01-01",
                        organization_identifier="CGH-001",
                        document_id="note-001",
                    ),
                ]
            ):
                pass
        assert "short text" in caplog.text.lower()


class TestSeed:
    """Test seed() method."""

    def test_seed_orgs_and_patients(self, client, httpx_mock):
        """Test seeding organizations and patients."""
        mock_fhir_auth(httpx_mock)
        mock_seed_response(
            httpx_mock,
            [
                ("201 Created", "Organization/org-1/_history/1"),
                ("201 Created", "Organization/org-2/_history/1"),
            ],
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[
                Organization(identifier="CGH-001", name="City General"),
                Organization(identifier="SMH-002", name="St. Mary's"),
            ],
            patients=[Patient(identifier="MRN-1")],
        )

        assert pipeline._phase == _Phase.PATIENTS_SEEDED
        assert (ORGANIZATION_IDENTIFIER_SYSTEM, "CGH-001") in pipeline._id_map
        assert (ORGANIZATION_IDENTIFIER_SYSTEM, "SMH-002") in pipeline._id_map
        assert pipeline._id_map[(ORGANIZATION_IDENTIFIER_SYSTEM, "CGH-001")] == "org-1"
        assert pipeline._id_map[(ORGANIZATION_IDENTIFIER_SYSTEM, "SMH-002")] == "org-2"
        assert pipeline._id_map[(IDENTIFIER_SYSTEM, "MRN-1")] == "pat-1"

    def test_seed_no_orgs_raises(self, client):
        """Test that empty organizations list raises ValueError."""
        pipeline = IngestionPipeline(client)
        with pytest.raises(ValueError, match="At least one organization"):
            pipeline.seed(organizations=[], patients=[Patient(identifier="MRN-1")])

    def test_seed_failure_raises_runtime(self, client, httpx_mock):
        """Test that FHIR error during seeding raises RuntimeError."""
        mock_fhir_auth(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            status_code=400,
            json={
                "resourceType": "OperationOutcome",
                "issue": [{"diagnostics": "Bad request"}],
            },
        )

        pipeline = IngestionPipeline(client)
        with pytest.raises(httpx.HTTPStatusError):
            pipeline.seed(
                organizations=[Organization(identifier="BAD", name="Bad Org")],
                patients=[Patient(identifier="MRN-1")],
            )

    def test_seed_patients_with_all_fields(self, client, httpx_mock):
        """Test seeding patients with org reference and demographics."""
        mock_fhir_auth(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[
                Patient(
                    identifier="MRN-1",
                    name="John Doe",
                    birth_date="1990-01-15",
                    gender="male",
                    managing_organization="CGH-001",
                ),
            ],
        )

        assert pipeline._phase == _Phase.PATIENTS_SEEDED
        assert pipeline._id_map[(IDENTIFIER_SYSTEM, "MRN-1")] == "pat-1"

        # Verify the patient resource had resolved references
        requests = httpx_mock.get_requests()
        # Find the last POST to /fhir/ (patient seed)
        patient_bundle_request = [
            r
            for r in requests
            if r.method == "POST" and str(r.url) == "http://localhost:8080/fhir/"
        ][-1]
        body = json.loads(patient_bundle_request.content)
        patient_resource = body["entry"][0]["resource"]
        assert (
            patient_resource["managingOrganization"]["reference"]
            == "Organization/org-1"
        )
        assert patient_resource["name"] == [{"text": "John Doe"}]
        assert patient_resource["birthDate"] == "1990-01-15"
        assert patient_resource["gender"] == "male"

    def test_seed_patients_minimal(self, client, httpx_mock):
        """Test seeding patient with just identifier."""
        mock_fhir_auth(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[Patient(identifier="MRN-1")],
        )

        assert pipeline._phase == _Phase.PATIENTS_SEEDED
        assert pipeline._id_map[(IDENTIFIER_SYSTEM, "MRN-1")] == "pat-1"


class TestExtract:
    """Test Phase 3: extract."""

    def _setup_pipeline(self, client, httpx_mock, num_patients=1):
        """Helper to set up a pipeline through seeding."""
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(
            httpx_mock, [f"pat-{i + 1}" for i in range(num_patients)]
        )
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        patient_entries = [
            ("201 Created", f"Patient/pat-{i + 1}/_history/1")
            for i in range(num_patients)
        ]
        mock_seed_response(httpx_mock, patient_entries)
        mock_patient_exists(httpx_mock, [f"pat-{i + 1}" for i in range(num_patients)])

        patients = [
            Patient(identifier=f"MRN-{i + 1}", managing_organization="CGH-001")
            for i in range(num_patients)
        ]

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=patients,
        )

        return pipeline

    def test_single_document_extraction(self, client, httpx_mock):
        """Test extracting a single document."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        # Mock context fetch + extract + persist
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        outcomes = []
        for outcome in pipeline.extract(
            [
                Document(
                    text="Patient has type 2 diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-15",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            outcomes.append(outcome)

        assert len(outcomes) == 1
        assert outcomes[0].success
        assert outcomes[0].patient_identifier == "MRN-1"
        assert outcomes[0].document_index == 0
        assert outcomes[0].extract_result is not None
        assert outcomes[0].extract_result.count == 1

    def test_extract_returns_list(self, client, httpx_mock):
        """extract() returns a list, not a generator."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        # Call extract() without iterating — this must still process documents
        results = pipeline.extract(
            [
                Document(
                    text="Patient has type 2 diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-15",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        )
        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0].success
        assert pipeline.documents_processed == 1

    def _extract_body(self, httpx_mock):
        return _extract_body(httpx_mock)

    def test_document_date_sent_as_its_own_field(self, client, httpx_mock):
        """Document.date is a payload field, not prose inside meta."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for _ in pipeline.extract(
            [
                Document(
                    text="Patient has diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-15",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            pass

        body = self._extract_body(httpx_mock)
        assert body["document_date"] == "2024-01-15"
        # Nothing else to say about this document, so meta is omitted entirely.
        assert "meta" not in body

    def test_observation_context_window_anchors_on_the_document(
        self, client, httpx_mock
    ):
        """Backfilling old documents must not query a window behind *today*."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for _ in pipeline.extract(
            [
                Document(
                    text="Patient has diabetes",
                    patient_identifier="MRN-1",
                    date="2015-04-02",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            pass

        observation_requests = [
            str(r.url)
            for r in httpx_mock.get_requests()
            if "/Observation?" in str(r.url)
        ]
        assert len(observation_requests) == 1
        assert "date=ge2013-04-02" in observation_requests[0]

    def test_context_covers_every_type_the_api_reads(self, client, httpx_mock):
        """A type in CONTEXT_RESOURCE_TYPES must actually be fetched.

        The context fetch logs and swallows per-type failures, so a type that
        stops being requested degrades silently — no error, just a thinner
        context and duplicate resources downstream.
        """
        pipeline = self._setup_pipeline(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for _ in pipeline.extract(
            [
                Document(
                    text="Patient has diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-15",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            pass

        urls = [str(r.url) for r in httpx_mock.get_requests()]
        for resource_type in CONTEXT_RESOURCE_TYPES:
            assert any(f"/fhir/{resource_type}?" in url for url in urls), (
                f"{resource_type} is in CONTEXT_RESOURCE_TYPES but was never fetched"
            )
        assert any("/fhir/ResearchStudy?" in url for url in urls)

    def test_fetched_context_reaches_the_extract_payload(self, client, httpx_mock):
        """Fetching a type is only half the contract — it must also be sent.

        The three types added most recently are the ones worth pinning: the API
        reads them, but nothing filled their slots for months, and the failure
        was silent in both directions.
        """
        pipeline = self._setup_pipeline(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        stored = {
            "NutritionOrder?patient=pat-1": {
                "resourceType": "NutritionOrder",
                "id": "no-88",
                "status": "active",
            },
            "MedicationAdministration?subject=pat-1": {
                "resourceType": "MedicationAdministration",
                "id": "ma-12",
                "status": "in-progress",
            },
            "FamilyMemberHistory?patient=pat-1": {
                "resourceType": "FamilyMemberHistory",
                "id": "fmh-4",
                "status": "completed",
            },
        }
        for query, resource in stored.items():
            httpx_mock.add_response(
                method="GET",
                url=f"http://localhost:8080/fhir/{query}&_count=500",
                json={"entry": [{"resource": resource}]},
                replace=True,
            )
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for _ in pipeline.extract(
            [
                Document(
                    text="Patient has diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-15",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            pass

        sent = self._extract_body(httpx_mock)["context"]
        assert {r["resourceType"]: r["id"] for r in sent} == {
            "NutritionOrder": "no-88",
            "MedicationAdministration": "ma-12",
            "FamilyMemberHistory": "fmh-4",
        }

    def test_document_date_normalized_before_sending(self, client, httpx_mock):
        """Whatever Document accepted, the payload carries ISO YYYY-MM-DD."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for _ in pipeline.extract(
            [
                Document(
                    text="Patient has diabetes",
                    patient_identifier="MRN-1",
                    date=datetime.date(2024, 1, 15),
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            pass

        assert self._extract_body(httpx_mock)["document_date"] == "2024-01-15"

    def test_meta_carries_context_without_the_date(self, client, httpx_mock):
        """meta keeps the caller's context plus the attending, and only that."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for _ in pipeline.extract(
            [
                Document(
                    text="Patient has diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-15",
                    organization_identifier="CGH-001",
                    meta="Department: Cardiology",
                    document_id="doc-1",
                ),
            ]
        ):
            pass

        body = self._extract_body(httpx_mock)
        assert body["meta"] == "Department: Cardiology"
        assert body["document_date"] == "2024-01-15"

    def test_date_sorting(self, client, httpx_mock):
        """Test that documents are sorted by date within patient."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        # Mock context + extract + persist for 2 docs
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        docs = [
            Document(
                text="Second note",
                patient_identifier="MRN-1",
                date="2024-03-01",
                organization_identifier="CGH-001",
                document_id="doc-1",
            ),
            Document(
                text="First note",
                patient_identifier="MRN-1",
                date="2024-01-01",
                organization_identifier="CGH-001",
                document_id="doc-2",
            ),
        ]

        outcomes = []
        for outcome in pipeline.extract(docs):
            outcomes.append(outcome)

        assert len(outcomes) == 2
        # Both should succeed (sorted by date internally)
        assert all(o.success for o in outcomes)

    def test_extraction_failure_continues_with_remaining(self, client, httpx_mock):
        """A deterministic failure (4xx) fails that document alone.

        A failed document persists nothing, so the patient's remaining
        documents still extract against a consistent record; the failed one is
        re-ingested later on the split-context path. (Transient failures are
        retried instead — see the retry tests below.)
        """
        pipeline = self._setup_pipeline(client, httpx_mock)

        # First doc: context fetch succeeds but extract API returns a 400 (content
        # error) — not transient, so it fails fast without retrying.
        mock_context_empty(httpx_mock, "pat-1")
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=400,
            json={"detail": "Bad request"},
        )
        # Second doc processes normally.
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        docs = [
            Document(
                text="First note",
                patient_identifier="MRN-1",
                date="2024-01-01",
                organization_identifier="CGH-001",
                document_id="doc-1",
            ),
            Document(
                text="Second note",
                patient_identifier="MRN-1",
                date="2024-02-01",
                organization_identifier="CGH-001",
                document_id="doc-2",
            ),
        ]

        outcomes = []
        for outcome in pipeline.extract(docs):
            outcomes.append(outcome)

        assert len(outcomes) == 2
        assert not outcomes[0].success
        assert outcomes[1].success

    def test_persist_failure_yields_error(self, client, httpx_mock):
        """Test that persist failure returns error outcome."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        # Persist fails
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            status_code=400,
            json={
                "resourceType": "OperationOutcome",
                "issue": [{"diagnostics": "Validation error"}],
            },
        )

        outcomes = []
        for outcome in pipeline.extract(
            [
                Document(
                    text="test",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            outcomes.append(outcome)

        assert len(outcomes) == 1
        assert not outcomes[0].success
        assert "Persist failed" in outcomes[0].error

    def test_transient_failure_retried_in_place(self, client, httpx_mock, monkeypatch):
        """A transient extract failure (500) is retried in place and then succeeds."""
        monkeypatch.setattr("cavell_client.ingestion.time.sleep", lambda *_: None)
        pipeline = self._setup_pipeline(client, httpx_mock)

        # Context is re-fetched each attempt (custom mock pops responses), so register
        # it once per attempt: attempt 1 (fails) + attempt 2 (succeeds).
        mock_context_empty(httpx_mock, "pat-1")
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=500,
            json={"detail": "transient"},
        )
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        outcomes = list(
            pipeline.extract(
                [
                    Document(
                        text="note",
                        patient_identifier="MRN-1",
                        date="2024-01-01",
                        organization_identifier="CGH-001",
                        document_id="doc-1",
                    ),
                ]
            )
        )

        assert len(outcomes) == 1
        assert outcomes[0].success

    def test_deferred_retry_recovers_transient_failure(
        self, client, httpx_mock, monkeypatch
    ):
        """A transient failure is recovered in a deferred pass after the run.

        The follower is no longer skipped: it processes (and persists) in the
        main pass, so the deferred re-run of the older document lands on the
        split-context path — the run's published watermark has advanced past it.
        """
        monkeypatch.setattr("cavell_client.ingestion.time.sleep", lambda *_: None)
        pipeline = self._setup_pipeline(client, httpx_mock)

        # Main pass: doc1 exhausts all in-place attempts (all 500).
        # Each attempt re-fetches context, so register context + a 500 per attempt.
        for _ in range(_DOC_MAX_ATTEMPTS):
            mock_context_empty(httpx_mock, "pat-1")
            httpx_mock.add_response(
                method="POST",
                url="https://qa.prism.cavell.app/api/extract/text",
                status_code=500,
                json={"detail": "transient"},
            )
        # Main pass: doc2 processes normally.
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)
        # Deferred pass: doc1 alone, now older than the persisted doc2, so it
        # fetches split context (provenance query + context) and succeeds.
        mock_context_split_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        docs = [
            Document(
                text="first",
                patient_identifier="MRN-1",
                date="2024-01-01",
                organization_identifier="CGH-001",
                document_id="doc-1",
            ),
            Document(
                text="second",
                patient_identifier="MRN-1",
                date="2024-02-01",
                organization_identifier="CGH-001",
                document_id="doc-2",
            ),
        ]
        outcomes = sorted(pipeline.extract(docs), key=lambda o: o.document_index)

        assert len(outcomes) == 2
        assert all(o.success for o in outcomes), [o.error for o in outcomes]
        # The re-run was routed as out-of-order; the follower was not.
        assert outcomes[0].out_of_order is True
        assert outcomes[1].out_of_order is False

    def test_extract_callable_multiple_times(self, client, httpx_mock):
        """Test that extract() can be called multiple times."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        # First call
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for outcome in pipeline.extract(
            [
                Document(
                    text="First batch",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            assert outcome.success

        # Second call
        mock_empty_document_identifiers(httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for outcome in pipeline.extract(
            [
                Document(
                    text="Second batch",
                    patient_identifier="MRN-1",
                    date="2024-02-01",
                    organization_identifier="CGH-001",
                    document_id="doc-2",
                ),
            ]
        ):
            assert outcome.success

        assert pipeline._phase == _Phase.EXTRACTING

    def test_reference_data_as_params(self, client, httpx_mock):
        """Test that patient_id and organization_id are passed as params, not in
        context."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for _ in pipeline.extract(
            [
                Document(
                    text="Patient has diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            pass

        # Find the extract API call
        requests = httpx_mock.get_requests()
        extract_request = [
            r for r in requests if r.method == "POST" and "extract/text" in str(r.url)
        ][-1]
        body = json.loads(extract_request.content)
        # Reference data comes as explicit params
        assert body["patient_id"] == "pat-1"
        assert body["organization_id"] == "org-1"
        # Context should only contain clinical resources (empty here)
        assert "context" not in body

    def test_document_id_passed_as_identifier(self, client, httpx_mock):
        """Test that document_id is passed as document_identifier to the API."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for _ in pipeline.extract(
            [
                Document(
                    text="test",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    document_id="doc-abc-123",
                ),
            ]
        ):
            pass

        requests = httpx_mock.get_requests()
        extract_request = [
            r for r in requests if r.method == "POST" and "extract/text" in str(r.url)
        ][-1]
        body = json.loads(extract_request.content)
        assert body["document_identifier"] == "doc-abc-123"

    def test_visit_id_passed_as_visit_identifier(self, client, httpx_mock):
        """Test that visit_id is passed as visit_identifier to the API."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for _ in pipeline.extract(
            [
                Document(
                    text="test",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    visit_id="visit-abc-123",
                    document_id="doc-1",
                ),
            ]
        ):
            pass

        requests = httpx_mock.get_requests()
        extract_request = [
            r for r in requests if r.method == "POST" and "extract/text" in str(r.url)
        ][-1]
        body = json.loads(extract_request.content)
        assert body["visit_identifier"] == "visit-abc-123"

    def test_multiple_patients_concurrent(self, client, httpx_mock):
        """Test that multiple patients are processed concurrently."""
        pipeline = self._setup_pipeline(client, httpx_mock, num_patients=2)

        # Patient 1
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        # Patient 2
        mock_context_empty(httpx_mock, "pat-2")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        docs = [
            Document(
                text="Note for patient 1",
                patient_identifier="MRN-1",
                date="2024-01-01",
                organization_identifier="CGH-001",
                document_id="doc-1",
            ),
            Document(
                text="Note for patient 2",
                patient_identifier="MRN-2",
                date="2024-01-01",
                organization_identifier="CGH-001",
                document_id="doc-2",
            ),
        ]

        outcomes = []
        for outcome in pipeline.extract(docs):
            outcomes.append(outcome)

        assert len(outcomes) == 2
        assert all(o.success for o in outcomes)
        patient_ids = {o.patient_identifier for o in outcomes}
        assert patient_ids == {"MRN-1", "MRN-2"}

    def test_skip_processed_filters_documents(self, client, httpx_mock, monkeypatch):
        """skip_processed=True filters out already-processed documents."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        # doc-1 already processed, doc-2 is new
        monkeypatch.setattr(
            pipeline._fhir, "list_document_identifiers", lambda **kw: {"doc-1"}
        )

        # Only doc-2 should be extracted
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        docs = [
            Document(
                text="Already processed note",
                patient_identifier="MRN-1",
                date="2024-01-01",
                organization_identifier="CGH-001",
                document_id="doc-1",
            ),
            Document(
                text="New note to process",
                patient_identifier="MRN-1",
                date="2024-02-01",
                organization_identifier="CGH-001",
                document_id="doc-2",
            ),
        ]

        outcomes = list(pipeline.extract(docs, skip_processed=True))
        assert len(outcomes) == 1
        assert outcomes[0].success
        assert outcomes[0].document_id == "doc-2"

    def test_skip_processed_false_processes_all(self, client, httpx_mock, monkeypatch):
        """skip_processed=False skips the FHIR query and processes all docs."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        # Should NOT be called — monkeypatch to blow up if it is
        def _boom(**kw):
            raise AssertionError("list_document_identifiers should not be called")

        monkeypatch.setattr(pipeline._fhir, "list_document_identifiers", _boom)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        docs = [
            Document(
                text="Note one",
                patient_identifier="MRN-1",
                date="2024-01-01",
                organization_identifier="CGH-001",
                document_id="doc-1",
            ),
            Document(
                text="Note two",
                patient_identifier="MRN-1",
                date="2024-02-01",
                organization_identifier="CGH-001",
                document_id="doc-2",
            ),
        ]

        outcomes = list(pipeline.extract(docs, skip_processed=False))
        assert len(outcomes) == 2
        assert all(o.success for o in outcomes)

    def test_limit_caps_documents(self, client, httpx_mock, monkeypatch):
        """limit caps remaining docs after filtering."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        monkeypatch.setattr(
            pipeline._fhir, "list_document_identifiers", lambda **kw: set()
        )

        # Only 1 doc should be extracted
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        docs = [
            Document(
                text=f"Note {i}",
                patient_identifier="MRN-1",
                date=f"2024-0{i + 1}-01",
                organization_identifier="CGH-001",
                document_id=f"doc-{i}",
            )
            for i in range(3)
        ]

        outcomes = list(pipeline.extract(docs, limit=1))
        assert len(outcomes) == 1
        assert outcomes[0].success

    def test_batch_size_kwarg_raises_pointed_migration_error(self, client, httpx_mock):
        """The renamed kwarg fails loudly — ignoring it would over-spend."""
        pipeline = self._setup_pipeline(client, httpx_mock)

        with pytest.raises(TypeError, match="renamed 'batch_size' to 'limit'"):
            pipeline.extract([], batch_size=1)

    def test_unknown_kwarg_raises(self, client, httpx_mock):
        pipeline = self._setup_pipeline(client, httpx_mock)

        with pytest.raises(TypeError, match="Unexpected keyword argument"):
            pipeline.extract([], nonsense=1)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_limit_below_one_raises(self, client, httpx_mock, bad):
        pipeline = self._setup_pipeline(client, httpx_mock)

        with pytest.raises(ValueError, match="limit must be >= 1"):
            pipeline.extract([], limit=bad)

    def test_document_id_is_required(self):
        """Without an id there is no resume, no watermark and no failure label."""
        with pytest.raises(TypeError, match="document_id"):
            Document(  # ty: ignore[missing-argument]
                text="Note without document_id",
                patient_identifier="MRN-1",
                date="2024-01-01",
            )


class TestEndToEnd:
    """End-to-end pipeline test."""

    def test_full_pipeline(self, client, httpx_mock):
        """Full pipeline: 1 org, 1 patient, 2 docs."""
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)

        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])
        mock_patient_exists(httpx_mock, "pat-1")

        pipeline = IngestionPipeline(client, tier="test-model")
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[
                Patient(
                    identifier="MRN-1",
                    managing_organization="CGH-001",
                ),
            ],
        )
        assert pipeline._phase == _Phase.PATIENTS_SEEDED

        # Phase 3: extract 2 documents
        # Doc 1: context + extract + persist
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=2)
        mock_persist_response(httpx_mock, created=2)

        # Doc 2: context (now with resources from doc 1) + extract + persist
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        docs = [
            Document(
                text="Patient presents with type 2 diabetes",
                patient_identifier="MRN-1",
                date="2024-01-15",
                organization_identifier="CGH-001",
                document_id="doc-001",
            ),
            Document(
                text="Follow-up visit, diabetes well controlled",
                patient_identifier="MRN-1",
                date="2024-04-15",
                organization_identifier="CGH-001",
                document_id="doc-002",
            ),
        ]

        outcomes = []
        for outcome in pipeline.extract(docs):
            outcomes.append(outcome)

        assert len(outcomes) == 2
        assert all(o.success for o in outcomes)
        assert pipeline._phase == _Phase.EXTRACTING

        # Verify tier was passed through
        requests = httpx_mock.get_requests()
        extract_requests = [
            r for r in requests if r.method == "POST" and "extract/text" in str(r.url)
        ]
        assert len(extract_requests) == 2
        for req in extract_requests:
            body = json.loads(req.content)
            assert body["tier"] == "test-model"


class TestPractitionerSeeding:
    """Test practitioner seeding in Phase 1."""

    def test_practitioner_unknown_org_raises(self, client):
        """Test that practitioner referencing unknown org raises ValueError."""
        pipeline = IngestionPipeline(client)
        with pytest.raises(ValueError, match="unknown organization"):
            pipeline.seed(
                organizations=[Organization(identifier="CGH-001", name="City General")],
                patients=[Patient(identifier="MRN-1")],
                practitioners=[
                    Practitioner(
                        identifier="DOC-001",
                        family_name="Smith",
                        given_name="Jane",
                        organization_identifier="UNKNOWN-ORG",
                    ),
                ],
            )

    def test_seed_orgs_and_practitioners(self, client, httpx_mock):
        """Test seeding organizations and practitioners together."""
        mock_fhir_auth(httpx_mock)
        # Org seed
        mock_seed_response(
            httpx_mock,
            [
                ("201 Created", "Organization/org-1/_history/1"),
            ],
        )
        # Practitioner seed
        mock_seed_response(
            httpx_mock,
            [
                ("201 Created", "Practitioner/prac-1/_history/1"),
            ],
        )
        # PractitionerRole seed
        mock_seed_response(
            httpx_mock,
            [
                ("201 Created", "PractitionerRole/role-1/_history/1"),
            ],
        )
        # Patient seed
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[Patient(identifier="MRN-1")],
            practitioners=[
                Practitioner(
                    identifier="DOC-001",
                    family_name="Smith",
                    given_name="Jane",
                    organization_identifier="CGH-001",
                ),
            ],
        )

        assert pipeline._phase == _Phase.PATIENTS_SEEDED
        assert (PRACTITIONER_IDENTIFIER_SYSTEM, "DOC-001") in pipeline._id_map
        assert pipeline._practitioner_names["DOC-001"] == "Jane Smith"

    def test_specialty_written_to_practitioner_role(self, client, httpx_mock):
        """Practitioner with specialty -> PractitionerRole has specialty field."""
        mock_fhir_auth(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(
            httpx_mock, [("201 Created", "Practitioner/prac-1/_history/1")]
        )
        mock_seed_response(
            httpx_mock, [("201 Created", "PractitionerRole/role-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[Patient(identifier="MRN-1")],
            practitioners=[
                Practitioner(
                    identifier="DOC-001",
                    family_name="Smith",
                    given_name="Jane",
                    organization_identifier="CGH-001",
                    specialty="Cardiology",
                ),
            ],
        )

        # Find the PractitionerRole bundle POST (third POST to /fhir/)
        requests = httpx_mock.get_requests()
        bundle_posts = [
            r
            for r in requests
            if r.method == "POST" and str(r.url) == "http://localhost:8080/fhir/"
        ]
        # Order: org, prac, role, patient — role is second-to-last
        role_bundle = json.loads(bundle_posts[-2].content)
        role_resource = role_bundle["entry"][0]["resource"]
        assert role_resource["specialty"] == [{"text": "Cardiology"}]

    def test_specialty_omitted_when_none(self, client, httpx_mock):
        """Practitioner without specialty -> PractitionerRole has no specialty field."""
        mock_fhir_auth(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(
            httpx_mock, [("201 Created", "Practitioner/prac-1/_history/1")]
        )
        mock_seed_response(
            httpx_mock, [("201 Created", "PractitionerRole/role-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[Patient(identifier="MRN-1")],
            practitioners=[
                Practitioner(
                    identifier="DOC-001",
                    family_name="Smith",
                    given_name="Jane",
                    organization_identifier="CGH-001",
                ),
            ],
        )

        requests = httpx_mock.get_requests()
        bundle_posts = [
            r
            for r in requests
            if r.method == "POST" and str(r.url) == "http://localhost:8080/fhir/"
        ]
        # Order: org, prac, role, patient — role is second-to-last
        role_bundle = json.loads(bundle_posts[-2].content)
        role_resource = role_bundle["entry"][0]["resource"]
        assert "specialty" not in role_resource

    def test_patient_unknown_practitioner_raises(self, client):
        """Test that patient referencing unknown practitioner raises ValueError."""
        pipeline = IngestionPipeline(client)
        with pytest.raises(ValueError, match="unknown practitioner"):
            pipeline.seed(
                organizations=[Organization(identifier="CGH-001", name="City General")],
                patients=[
                    Patient(
                        identifier="MRN-1",
                        general_practitioners=["UNKNOWN-DOC"],
                    ),
                ],
            )

    def test_seed_patients_with_general_practitioners(self, client, httpx_mock):
        """Test seeding patients with general practitioner references."""
        mock_fhir_auth(httpx_mock)
        # org + practitioner + role + patient
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(
            httpx_mock, [("201 Created", "Practitioner/prac-1/_history/1")]
        )
        mock_seed_response(
            httpx_mock, [("201 Created", "PractitionerRole/role-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client)
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[
                Patient(
                    identifier="MRN-1",
                    managing_organization="CGH-001",
                    general_practitioners=["DOC-001"],
                ),
            ],
            practitioners=[
                Practitioner(
                    identifier="DOC-001",
                    family_name="Smith",
                    given_name="Jane",
                    organization_identifier="CGH-001",
                ),
            ],
        )

        # Verify patient resource has generalPractitioner
        requests = httpx_mock.get_requests()
        patient_bundle_request = [
            r
            for r in requests
            if r.method == "POST" and str(r.url) == "http://localhost:8080/fhir/"
        ][-1]
        body = json.loads(patient_bundle_request.content)
        patient_resource = body["entry"][0]["resource"]
        assert patient_resource["generalPractitioner"] == [
            {"reference": "Practitioner/prac-1"}
        ]


def mock_extract_with_practitioners(httpx_mock, practitioners, conditions=1):
    """Mock extraction response that includes Practitioner entries."""
    entries = []
    for prac in practitioners:
        entry = {
            "fullUrl": prac["fullUrl"],
            "resource": {
                "resourceType": "Practitioner",
                "name": [{"family": prac["family"], "given": [prac["given"]]}],
            },
            "request": {"method": "POST", "url": "Practitioner"},
        }
        if "identifier" in prac:
            entry["resource"]["identifier"] = [{"value": prac["identifier"]}]
        entries.append(entry)

    for _ in range(conditions):
        entry = {
            "resource": {
                "resourceType": "Condition",
                "subject": {"reference": "Patient/pat-1"},
            },
            "request": {"method": "POST", "url": "Condition"},
        }
        # Link first condition to first practitioner if any
        if practitioners:
            entry["resource"]["asserter"] = {"reference": practitioners[0]["fullUrl"]}
        entries.append(entry)

    httpx_mock.add_response(
        method="POST",
        url="https://qa.prism.cavell.app/api/extract/text",
        json={
            "bundle": {
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": entries,
            },
            "count": conditions,
        },
    )


PRACTITIONER_SYSTEM_ENCODED = quote(PRACTITIONER_IDENTIFIER_SYSTEM, safe="")


def mock_practitioner_search_by_identifier(httpx_mock, identifier, results):
    """Mock a FHIR Practitioner search by identifier."""
    entries = [{"resource": r} for r in results]
    url = f"http://localhost:8080/fhir/Practitioner?identifier={PRACTITIONER_SYSTEM_ENCODED}%7C{identifier}"
    httpx_mock.add_response(
        method="GET",
        url=url,
        json={"resourceType": "Bundle", "entry": entries},
    )


def mock_practitioner_search_by_name(httpx_mock, family, given, org_id, results):
    """Mock a FHIR Practitioner search by name and org."""
    entries = [{"resource": r} for r in results]
    url = (
        f"http://localhost:8080/fhir/Practitioner?"
        f"family={family}&given={given}"
        f"&_has%3APractitionerRole%3Apractitioner%3Aorganization=Organization%2F{org_id}"
    )
    httpx_mock.add_response(
        method="GET",
        url=url,
        json={"resourceType": "Bundle", "entry": entries},
    )


def _setup_pipeline_with_practitioners(client, httpx_mock):
    """Set up pipeline with org + practitioner + patient."""
    mock_fhir_auth(httpx_mock)
    mock_empty_document_identifiers(httpx_mock)
    mock_seed_response(httpx_mock, [("201 Created", "Organization/org-1/_history/1")])
    mock_seed_response(httpx_mock, [("201 Created", "Practitioner/prac-1/_history/1")])
    mock_seed_response(
        httpx_mock, [("201 Created", "PractitionerRole/role-1/_history/1")]
    )
    mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])
    mock_patient_exists(httpx_mock, "pat-1")

    pipeline = IngestionPipeline(client)
    pipeline.seed(
        organizations=[Organization(identifier="CGH-001", name="City General")],
        patients=[
            Patient(identifier="MRN-1", managing_organization="CGH-001"),
        ],
        practitioners=[
            Practitioner(
                identifier="DOC-001",
                family_name="Smith",
                given_name="Jane",
                organization_identifier="CGH-001",
            ),
        ],
    )

    return pipeline


class TestPractitionerMatching:
    """Test practitioner matching after extraction."""

    def test_practitioner_matched_by_identifier(self, client, httpx_mock):
        """Extracted practitioner with identifier → match found → references
        rewritten."""
        pipeline = _setup_pipeline_with_practitioners(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_with_practitioners(
            httpx_mock,
            [
                {
                    "fullUrl": "urn:uuid:prac-x",
                    "family": "Smith",
                    "given": "Jane",
                    "identifier": "DOC-001",
                },
            ],
        )
        # Search by identifier returns match
        mock_practitioner_search_by_identifier(
            httpx_mock,
            "DOC-001",
            [
                {"resourceType": "Practitioner", "id": "prac-1"},
            ],
        )
        mock_persist_response(httpx_mock, created=1)  # Only Condition persisted

        outcomes = []
        for outcome in pipeline.extract(
            [
                Document(
                    text="Dr. Smith notes diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            outcomes.append(outcome)

        assert len(outcomes) == 1
        assert outcomes[0].success
        # Bundle should have Condition with rewritten practitioner reference, no
        # Practitioner entries
        bundle = outcomes[0].extract_result.bundle
        assert all(
            e["resource"]["resourceType"] != "Practitioner"
            for e in bundle.get("entry", [])
        )
        condition = bundle["entry"][0]["resource"]
        assert condition["asserter"]["reference"] == "Practitioner/prac-1"

    def test_practitioner_matched_by_name(self, client, httpx_mock):
        """Extracted practitioner without identifier → name+org search → match found."""
        pipeline = _setup_pipeline_with_practitioners(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_with_practitioners(
            httpx_mock,
            [
                {"fullUrl": "urn:uuid:prac-x", "family": "Smith", "given": "Jane"},
            ],
        )
        # Search by name+org returns match
        mock_practitioner_search_by_name(
            httpx_mock,
            "Smith",
            "Jane",
            "org-1",
            [
                {"resourceType": "Practitioner", "id": "prac-1"},
            ],
        )
        mock_persist_response(httpx_mock, created=1)

        outcomes = []
        for outcome in pipeline.extract(
            [
                Document(
                    text="Dr. Smith notes diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            outcomes.append(outcome)

        assert outcomes[0].success
        condition = outcomes[0].extract_result.bundle["entry"][0]["resource"]
        assert condition["asserter"]["reference"] == "Practitioner/prac-1"

    def test_practitioner_no_match_drops_references(self, client, httpx_mock):
        """No match found → references dropped, warning logged."""
        pipeline = _setup_pipeline_with_practitioners(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_with_practitioners(
            httpx_mock,
            [
                {"fullUrl": "urn:uuid:prac-x", "family": "Unknown", "given": "Doctor"},
            ],
        )
        # Search returns empty
        mock_practitioner_search_by_name(httpx_mock, "Unknown", "Doctor", "org-1", [])
        mock_persist_response(httpx_mock, created=1)

        outcomes = []
        for outcome in pipeline.extract(
            [
                Document(
                    text="Dr. Unknown notes diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            outcomes.append(outcome)

        assert outcomes[0].success
        condition = outcomes[0].extract_result.bundle["entry"][0]["resource"]
        assert "asserter" not in condition

    def test_practitioner_multiple_matches_drops_references(self, client, httpx_mock):
        """Ambiguous match (>1 result) → references dropped."""
        pipeline = _setup_pipeline_with_practitioners(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_with_practitioners(
            httpx_mock,
            [
                {"fullUrl": "urn:uuid:prac-x", "family": "Smith", "given": "Jane"},
            ],
        )
        # Search returns 2 matches — ambiguous
        mock_practitioner_search_by_name(
            httpx_mock,
            "Smith",
            "Jane",
            "org-1",
            [
                {"resourceType": "Practitioner", "id": "prac-1"},
                {"resourceType": "Practitioner", "id": "prac-99"},
            ],
        )
        mock_persist_response(httpx_mock, created=1)

        outcomes = []
        for outcome in pipeline.extract(
            [
                Document(
                    text="Dr. Smith notes diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            outcomes.append(outcome)

        assert outcomes[0].success
        condition = outcomes[0].extract_result.bundle["entry"][0]["resource"]
        assert "asserter" not in condition

    def test_practitioner_deduplication(self, client, httpx_mock):
        """Two Practitioner entries with same identifier → only one FHIR search."""
        pipeline = _setup_pipeline_with_practitioners(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")

        # Two practitioners with the same identifier
        entries = [
            {
                "fullUrl": "urn:uuid:prac-a",
                "resource": {
                    "resourceType": "Practitioner",
                    "identifier": [{"value": "DOC-001"}],
                    "name": [{"family": "Smith", "given": ["Jane"]}],
                },
                "request": {"method": "POST", "url": "Practitioner"},
            },
            {
                "fullUrl": "urn:uuid:prac-b",
                "resource": {
                    "resourceType": "Practitioner",
                    "identifier": [{"value": "DOC-001"}],
                    "name": [{"family": "Smith", "given": ["Jane"]}],
                },
                "request": {"method": "POST", "url": "Practitioner"},
            },
            {
                "resource": {
                    "resourceType": "Condition",
                    "subject": {"reference": "Patient/pat-1"},
                    "asserter": {"reference": "urn:uuid:prac-a"},
                },
                "request": {"method": "POST", "url": "Condition"},
            },
            {
                "resource": {
                    "resourceType": "MedicationRequest",
                    "subject": {"reference": "Patient/pat-1"},
                    "requester": {"reference": "urn:uuid:prac-b"},
                },
                "request": {"method": "POST", "url": "MedicationRequest"},
            },
        ]
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={
                "bundle": {
                    "resourceType": "Bundle",
                    "type": "transaction",
                    "entry": entries,
                },
                "count": 2,
            },
        )

        # Only one search should happen (deduplicated by identifier)
        mock_practitioner_search_by_identifier(
            httpx_mock,
            "DOC-001",
            [
                {"resourceType": "Practitioner", "id": "prac-1"},
            ],
        )
        mock_persist_response(httpx_mock, created=2)

        outcomes = []
        for outcome in pipeline.extract(
            [
                Document(
                    text="test",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            outcomes.append(outcome)

        assert outcomes[0].success
        bundle = outcomes[0].extract_result.bundle
        # No Practitioner entries remain
        resource_types = [e["resource"]["resourceType"] for e in bundle["entry"]]
        assert "Practitioner" not in resource_types
        # Both references rewritten
        condition = next(
            e["resource"]
            for e in bundle["entry"]
            if e["resource"]["resourceType"] == "Condition"
        )
        med_req = next(
            e["resource"]
            for e in bundle["entry"]
            if e["resource"]["resourceType"] == "MedicationRequest"
        )
        assert condition["asserter"]["reference"] == "Practitioner/prac-1"
        assert med_req["requester"]["reference"] == "Practitioner/prac-1"

    def test_practitioner_identifier_injected_into_meta(self, client, httpx_mock):
        """When practitioner_identifier is set on Document, meta includes attending
        info."""
        pipeline = _setup_pipeline_with_practitioners(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for _ in pipeline.extract(
            [
                Document(
                    text="Patient has diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    practitioner_identifier="DOC-001",
                    document_id="doc-1",
                ),
            ]
        ):
            pass

        # Find the extract API call and verify meta
        requests = httpx_mock.get_requests()
        extract_request = [
            r for r in requests if r.method == "POST" and "extract/text" in str(r.url)
        ][-1]
        body = json.loads(extract_request.content)
        # The document carries no meta of its own, so the attending line is all
        # of it — the date is a payload field, not a line in here.
        assert body["meta"] == "Attending: Jane Smith (DOC-001)"
        assert body["document_date"] == "2024-01-01"

    def test_meta_joins_document_meta_and_attending(self, client, httpx_mock):
        """Both parts, in order, with nothing else spliced in."""
        pipeline = _setup_pipeline_with_practitioners(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for _ in pipeline.extract(
            [
                Document(
                    text="Patient has diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    meta="Department: Cardiology",
                    practitioner_identifier="DOC-001",
                    document_id="doc-1",
                ),
            ]
        ):
            pass

        extract_request = [
            r
            for r in httpx_mock.get_requests()
            if r.method == "POST" and "extract/text" in str(r.url)
        ][-1]
        body = json.loads(extract_request.content)
        assert body["meta"] == (
            "Department: Cardiology\nAttending: Jane Smith (DOC-001)"
        )

    def test_reference_data_as_params_with_practitioners(self, client, httpx_mock):
        """Verify patient_id, organization_id, practitioner_id are passed as params."""
        pipeline = _setup_pipeline_with_practitioners(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        for _ in pipeline.extract(
            [
                Document(
                    text="Patient has diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    practitioner_identifier="DOC-001",
                    document_id="doc-1",
                ),
            ]
        ):
            pass

        requests = httpx_mock.get_requests()
        extract_request = [
            r for r in requests if r.method == "POST" and "extract/text" in str(r.url)
        ][-1]
        body = json.loads(extract_request.content)
        # Reference data comes as explicit params
        assert body["patient_id"] == "pat-1"
        assert body["organization_id"] == "org-1"
        assert body["practitioner_id"] == "prac-1"
        # Context should only contain clinical resources (empty here)
        assert "context" not in body

    def test_patient_references_not_rewritten(self, client, httpx_mock):
        """Verify bundle patient references are NOT rewritten (backend handles via
        patient_id)."""
        pipeline = _setup_pipeline_with_practitioners(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")
        # API returns bundle with Patient/pat-1 references (backend resolved)
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={
                "bundle": {
                    "resourceType": "Bundle",
                    "type": "transaction",
                    "entry": [
                        {
                            "resource": {
                                "resourceType": "Condition",
                                "subject": {"reference": "Patient/pat-1"},
                            },
                            "request": {"method": "POST", "url": "Condition"},
                        }
                    ],
                },
                "count": 1,
            },
        )
        mock_persist_response(httpx_mock, created=1)

        outcomes = []
        for outcome in pipeline.extract(
            [
                Document(
                    text="test",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            outcomes.append(outcome)

        # Patient reference should remain as-is from API
        condition = outcomes[0].extract_result.bundle["entry"][0]["resource"]
        assert condition["subject"]["reference"] == "Patient/pat-1"


class TestPractitionerEdgeCases:
    """Test edge cases in practitioner matching."""

    def test_practitioner_no_identity_skipped(self, client, httpx_mock):
        """Practitioner with no identifier and no name → reference nulled, no crash."""
        pipeline = _setup_pipeline_with_practitioners(client, httpx_mock)

        mock_context_empty(httpx_mock, "pat-1")

        # Extraction returns a Practitioner with no identifier and empty name
        entries = [
            {
                "fullUrl": "urn:uuid:prac-anon",
                "resource": {
                    "resourceType": "Practitioner",
                    "name": [{"family": "", "given": []}],
                },
                "request": {"method": "POST", "url": "Practitioner"},
            },
            {
                "resource": {
                    "resourceType": "Condition",
                    "subject": {"reference": "Patient/pat-1"},
                    "asserter": {"reference": "urn:uuid:prac-anon"},
                },
                "request": {"method": "POST", "url": "Condition"},
            },
        ]
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={
                "bundle": {
                    "resourceType": "Bundle",
                    "type": "transaction",
                    "entry": entries,
                },
                "count": 1,
            },
        )
        mock_persist_response(httpx_mock, created=1)

        outcomes = []
        for outcome in pipeline.extract(
            [
                Document(
                    text="test",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    organization_identifier="CGH-001",
                    document_id="doc-1",
                ),
            ]
        ):
            outcomes.append(outcome)

        assert len(outcomes) == 1
        assert outcomes[0].success
        # The anonymous practitioner reference should be dropped
        condition = outcomes[0].extract_result.bundle["entry"][0]["resource"]
        assert "asserter" not in condition


class TestEndToEndWithPractitioners:
    """End-to-end pipeline test with practitioners."""

    def test_full_pipeline_with_practitioners(self, client, httpx_mock):
        """Full pipeline: org + practitioner, patient with GP, doc with matching."""
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)

        # Seed: org + practitioner + role + patient
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(
            httpx_mock, [("201 Created", "Practitioner/prac-1/_history/1")]
        )
        mock_seed_response(
            httpx_mock, [("201 Created", "PractitionerRole/role-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])
        mock_patient_exists(httpx_mock, "pat-1")

        pipeline = IngestionPipeline(client, tier="test-model")
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[
                Patient(
                    identifier="MRN-1",
                    managing_organization="CGH-001",
                    general_practitioners=["DOC-001"],
                ),
            ],
            practitioners=[
                Practitioner(
                    identifier="DOC-001",
                    family_name="Smith",
                    given_name="Jane",
                    organization_identifier="CGH-001",
                ),
            ],
        )

        # Phase 3: extract with practitioner in response
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_with_practitioners(
            httpx_mock,
            [
                {
                    "fullUrl": "urn:uuid:prac-x",
                    "family": "Smith",
                    "given": "Jane",
                    "identifier": "DOC-001",
                },
            ],
            conditions=1,
        )
        mock_practitioner_search_by_identifier(
            httpx_mock,
            "DOC-001",
            [
                {"resourceType": "Practitioner", "id": "prac-1"},
            ],
        )
        mock_persist_response(httpx_mock, created=1)

        outcomes = []
        for outcome in pipeline.extract(
            [
                Document(
                    text="Patient presents with diabetes",
                    patient_identifier="MRN-1",
                    date="2024-01-15",
                    organization_identifier="CGH-001",
                    practitioner_identifier="DOC-001",
                    document_id="doc-1",
                ),
            ]
        ):
            outcomes.append(outcome)

        assert len(outcomes) == 1
        assert outcomes[0].success
        assert outcomes[0].extract_result is not None
        bundle = outcomes[0].extract_result.bundle
        # Only clinical resources remain (no Practitioner entries)
        resource_types = [e["resource"]["resourceType"] for e in bundle["entry"]]
        assert "Practitioner" not in resource_types
        assert "Condition" in resource_types


class TestPatientNormalization:
    """Test that general_practitioners accepts str | list[str] | None."""

    def test_list_passthrough(self):
        p = Patient(identifier="P1", general_practitioners=["GP1", "GP2"])
        assert p.general_practitioners == ["GP1", "GP2"]

    def test_string_wrapping(self):
        p = Patient(identifier="P1", general_practitioners="GP1")
        assert p.general_practitioners == ["GP1"]

    def test_explicit_none(self):
        p = Patient(identifier="P1", general_practitioners=None)
        assert p.general_practitioners is None

    def test_default_none(self):
        p = Patient(identifier="P1")
        assert p.general_practitioners is None


class TestPatientFromRows:
    """Test Patient.from_rows() classmethod."""

    def test_basic_mapping(self):
        rows = [
            {"pid": "P1", "pname": "Alice"},
            {"pid": "P2", "pname": "Bob"},
        ]
        patients = Patient.from_rows(
            rows, columns={"identifier": "pid", "name": "pname"}
        )
        assert len(patients) == 2
        assert patients[0].identifier == "P1"
        assert patients[0].name == "Alice"
        assert patients[1].identifier == "P2"
        assert patients[1].name == "Bob"

    def test_dedup_first_occurrence_wins(self):
        rows = [
            {"pid": "P1", "pname": "Alice"},
            {"pid": "P1", "pname": "Alice-duplicate"},
        ]
        patients = Patient.from_rows(
            rows, columns={"identifier": "pid", "name": "pname"}
        )
        assert len(patients) == 1
        assert patients[0].name == "Alice"

    def test_defaults(self):
        rows = [{"pid": "P1"}]
        patients = Patient.from_rows(
            rows,
            columns={"identifier": "pid"},
            managing_organization="ORG-1",
        )
        assert patients[0].managing_organization == "ORG-1"

    def test_gp_normalization(self):
        """GP column value (a single string) gets normalized to a list."""
        rows = [{"pid": "P1", "gp": "GP1"}]
        patients = Patient.from_rows(
            rows,
            columns={"identifier": "pid", "general_practitioners": "gp"},
        )
        assert patients[0].general_practitioners == ["GP1"]

    def test_missing_identifier_key(self):
        rows = [{"pid": "P1"}]
        with pytest.raises(ValueError, match="identifier"):
            Patient.from_rows(rows, columns={"name": "pname"})

    def test_unknown_field_in_columns(self):
        rows = [{"pid": "P1"}]
        with pytest.raises(ValueError, match="bogus"):
            Patient.from_rows(rows, columns={"identifier": "pid", "bogus": "col"})

    def test_unknown_field_in_defaults(self):
        rows = [{"pid": "P1"}]
        with pytest.raises(ValueError, match="bogus"):
            Patient.from_rows(rows, columns={"identifier": "pid"}, bogus="val")

    def test_empty_rows(self):
        patients = Patient.from_rows([], columns={"identifier": "pid"})
        assert patients == []

    def test_empty_strings_become_none(self):
        rows = [{"pid": "P1", "pname": "", "bd": ""}]
        patients = Patient.from_rows(
            rows,
            columns={"identifier": "pid", "name": "pname", "birth_date": "bd"},
        )
        assert patients[0].name is None
        assert patients[0].birth_date is None

    def test_defaults_override_columns(self):
        """When a field appears in both columns and defaults, defaults win."""
        rows = [{"pid": "P1", "org": "CSV-ORG"}]
        patients = Patient.from_rows(
            rows,
            columns={"identifier": "pid", "managing_organization": "org"},
            managing_organization="DEFAULT-ORG",
        )
        assert patients[0].managing_organization == "DEFAULT-ORG"

    def test_empty_identifier_skipped(self):
        """Rows with empty identifier are skipped (not created with None)."""
        rows = [
            {"pid": "", "pname": "Ghost"},
            {"pid": "P1", "pname": "Alice"},
        ]
        patients = Patient.from_rows(
            rows, columns={"identifier": "pid", "name": "pname"}
        )
        assert len(patients) == 1
        assert patients[0].identifier == "P1"

    def test_missing_column_raises(self):
        """Raises ValueError if a mapped column doesn't exist in rows."""
        rows = [
            {"pname": "Ghost"},
        ]
        with pytest.raises(ValueError, match="Column 'pid'.*not found in rows"):
            Patient.from_rows(rows, columns={"identifier": "pid", "name": "pname"})


class TestDocumentFromRows:
    """Test Document.from_rows() classmethod."""

    REQUIRED = {
        "text": "note_text",
        "patient_identifier": "patient_id",
        "date": "note_date",
        "document_id": "doc_id",
    }

    def _row(self, **overrides):
        base = {
            "note_text": "Patient presents with persistent cough and fever.",
            "patient_id": "MRN-1",
            "note_date": "2024-01-15",
            "doc_id": "N1",
        }
        base.update(overrides)
        return base

    def test_basic_mapping(self):
        rows = [
            self._row(),
            self._row(doc_id="N2", patient_id="MRN-2", note_date="2024-02-01"),
        ]
        docs = Document.from_rows(rows, columns=self.REQUIRED)
        assert len(docs) == 2
        assert docs[0].patient_identifier == "MRN-1"
        assert docs[0].date == "2024-01-15"
        assert docs[1].patient_identifier == "MRN-2"

    def test_optional_columns(self):
        rows = [self._row(dept="Cardiology", prac="DOC-1", vid="V1")]
        docs = Document.from_rows(
            rows,
            columns={
                **self.REQUIRED,
                "meta": "dept",
                "practitioner_identifier": "prac",
                "visit_id": "vid",
            },
        )
        assert docs[0].document_id == "N1"
        assert docs[0].meta == "Cardiology"
        assert docs[0].practitioner_identifier == "DOC-1"
        assert docs[0].visit_id == "V1"

    def test_defaults(self):
        rows = [self._row()]
        docs = Document.from_rows(
            rows, columns=self.REQUIRED, organization_identifier="ORG-1"
        )
        assert docs[0].organization_identifier == "ORG-1"

    def test_defaults_override_columns(self):
        rows = [self._row(org="CSV-ORG")]
        docs = Document.from_rows(
            rows,
            columns={**self.REQUIRED, "organization_identifier": "org"},
            organization_identifier="DEFAULT-ORG",
        )
        assert docs[0].organization_identifier == "DEFAULT-ORG"

    def test_missing_required_column(self):
        with pytest.raises(ValueError, match="columns must include 'text'"):
            Document.from_rows(
                [self._row()],
                columns={"patient_identifier": "patient_id", "date": "note_date"},
            )

    def test_missing_document_id_column(self):
        """document_id is required, like text/patient_identifier/date."""
        with pytest.raises(ValueError, match="columns must include 'document_id'"):
            Document.from_rows(
                [self._row()],
                columns={
                    "text": "note_text",
                    "patient_identifier": "patient_id",
                    "date": "note_date",
                },
            )

    def test_document_id_column_cannot_be_disabled(self):
        """Mapping to None disables optional fields; required ones must raise."""
        with pytest.raises(ValueError, match="cannot be disabled"):
            Document.from_rows(
                [self._row()], columns={**self.REQUIRED, "document_id": None}
            )

    def test_unknown_field_in_columns(self):
        with pytest.raises(ValueError, match="bogus"):
            Document.from_rows([self._row()], columns={**self.REQUIRED, "bogus": "col"})

    def test_unknown_field_in_defaults(self):
        with pytest.raises(ValueError, match="bogus"):
            Document.from_rows([self._row()], columns=self.REQUIRED, bogus="val")

    def test_missing_csv_column_raises(self):
        rows = [{"patient_id": "MRN-1", "note_date": "2024-01-15"}]
        with pytest.raises(ValueError, match="Column 'note_text'.*not found"):
            Document.from_rows(rows, columns=self.REQUIRED)

    def test_empty_rows(self):
        docs = Document.from_rows([], columns=self.REQUIRED)
        assert docs == []

    def test_duplicate_document_id_raises(self):
        rows = [
            self._row(doc_id="N1"),
            self._row(doc_id="N1", patient_id="MRN-2"),
        ]
        with pytest.raises(ValueError, match="Duplicate document_id.*N1"):
            Document.from_rows(rows, columns=self.REQUIRED)

    def test_short_text_warns(self, caplog):
        rows = [self._row(note_text="Short.")]
        with caplog.at_level(logging.WARNING):
            docs = Document.from_rows(rows, columns=self.REQUIRED)
        assert len(docs) == 1
        assert "short text" in caplog.text.lower()

    def test_empty_optional_becomes_none(self):
        rows = [self._row(prac="", vid="")]
        docs = Document.from_rows(
            rows,
            columns={
                **self.REQUIRED,
                "practitioner_identifier": "prac",
                "visit_id": "vid",
            },
        )
        assert docs[0].practitioner_identifier is None
        assert docs[0].visit_id is None

    def test_empty_document_id_raises(self):
        """A blank id is not silently coerced to None — it is a required field."""
        rows = [self._row(doc_id="")]
        with pytest.raises(ValueError, match="document_id must not be empty"):
            Document.from_rows(rows, columns=self.REQUIRED)

    def test_date_conversion(self):
        """Date strings are validated through Document.__post_init__."""
        rows = [self._row(note_date="2024-06-15")]
        docs = Document.from_rows(rows, columns=self.REQUIRED)
        assert docs[0].date == "2024-06-15"

    # --- dict meta support ---

    def test_dict_meta_single_column(self):
        rows = [self._row(dept="Cardiology")]
        docs = Document.from_rows(
            rows,
            columns={**self.REQUIRED, "meta": {"Department": "dept"}},
        )
        assert docs[0].meta == "Department: Cardiology"

    def test_dict_meta_multiple_columns(self):
        rows = [self._row(dept="Cardiology", ntype="Progress Note")]
        docs = Document.from_rows(
            rows,
            columns={
                **self.REQUIRED,
                "meta": {"Department": "dept", "Note type": "ntype"},
            },
        )
        assert docs[0].meta == "Department: Cardiology\nNote type: Progress Note"

    def test_dict_meta_skips_empty_values(self):
        rows = [self._row(dept="Cardiology", ntype="")]
        docs = Document.from_rows(
            rows,
            columns={
                **self.REQUIRED,
                "meta": {"Department": "dept", "Note type": "ntype"},
            },
        )
        assert docs[0].meta == "Department: Cardiology"

    def test_dict_meta_all_empty_is_none(self):
        rows = [self._row(dept="", ntype="")]
        docs = Document.from_rows(
            rows,
            columns={
                **self.REQUIRED,
                "meta": {"Department": "dept", "Note type": "ntype"},
            },
        )
        assert docs[0].meta is None

    def test_dict_meta_validates_column_names(self):
        rows = [self._row()]
        with pytest.raises(ValueError, match="Column 'nonexistent'.*not found"):
            Document.from_rows(
                rows,
                columns={**self.REQUIRED, "meta": {"Dept": "nonexistent"}},
            )

    def test_string_meta_still_works(self):
        """Backward compat: plain str meta unchanged."""
        rows = [self._row(dept="Cardiology")]
        docs = Document.from_rows(
            rows,
            columns={**self.REQUIRED, "meta": "dept"},
        )
        assert docs[0].meta == "Cardiology"


class TestPractitionerFromRows:
    """Test Practitioner.from_rows() classmethod."""

    def test_basic_with_name_column(self):
        rows = [
            {"pid": "PR1", "pname": "Jane Smith"},
            {"pid": "PR2", "pname": "John Doe"},
        ]
        pracs = Practitioner.from_rows(
            rows,
            columns={"identifier": "pid", "name": "pname"},
            organization_identifier="ORG-1",
        )
        assert len(pracs) == 2
        assert pracs[0].identifier == "PR1"
        assert pracs[0].given_name == "Jane"
        assert pracs[0].family_name == "Smith"
        assert pracs[1].given_name == "John"
        assert pracs[1].family_name == "Doe"

    def test_name_column_multi_word_family(self):
        """'name' column with 3+ words: first=given, rest=family."""
        rows = [{"pid": "PR1", "pname": "Jan van der Berg"}]
        pracs = Practitioner.from_rows(
            rows,
            columns={"identifier": "pid", "name": "pname"},
            organization_identifier="ORG-1",
        )
        assert pracs[0].given_name == "Jan"
        assert pracs[0].family_name == "van der Berg"

    def test_separate_given_family_columns(self):
        rows = [{"pid": "PR1", "gn": "Jane", "fn": "Smith"}]
        pracs = Practitioner.from_rows(
            rows,
            columns={"identifier": "pid", "given_name": "gn", "family_name": "fn"},
            organization_identifier="ORG-1",
        )
        assert pracs[0].given_name == "Jane"
        assert pracs[0].family_name == "Smith"

    def test_dedup_first_occurrence_wins(self):
        rows = [
            {"pid": "PR1", "pname": "Jane Smith"},
            {"pid": "PR1", "pname": "Jane Duplicate"},
        ]
        pracs = Practitioner.from_rows(
            rows,
            columns={"identifier": "pid", "name": "pname"},
            organization_identifier="ORG-1",
        )
        assert len(pracs) == 1
        assert pracs[0].family_name == "Smith"

    def test_defaults(self):
        rows = [{"pid": "PR1", "pname": "Jane Smith"}]
        pracs = Practitioner.from_rows(
            rows,
            columns={"identifier": "pid", "name": "pname"},
            organization_identifier="ORG-1",
            specialty="Cardiology",
        )
        assert pracs[0].organization_identifier == "ORG-1"
        assert pracs[0].specialty == "Cardiology"

    def test_missing_identifier_key(self):
        rows = [{"pid": "PR1"}]
        with pytest.raises(ValueError, match="identifier"):
            Practitioner.from_rows(
                rows,
                columns={"name": "pname"},
                organization_identifier="ORG-1",
            )

    def test_unknown_field_in_columns(self):
        rows = [{"pid": "PR1"}]
        with pytest.raises(ValueError, match="bogus"):
            Practitioner.from_rows(
                rows,
                columns={"identifier": "pid", "bogus": "col"},
                organization_identifier="ORG-1",
            )

    def test_unknown_field_in_defaults(self):
        rows = [{"pid": "PR1"}]
        with pytest.raises(ValueError, match="bogus"):
            Practitioner.from_rows(
                rows,
                columns={"identifier": "pid", "name": "pname"},
                organization_identifier="ORG-1",
                bogus="val",
            )

    def test_empty_rows(self):
        pracs = Practitioner.from_rows(
            [],
            columns={"identifier": "pid", "name": "pname"},
            organization_identifier="ORG-1",
        )
        assert pracs == []

    def test_empty_name_skips_row(self):
        """Row with empty practitioner name is skipped (can't split)."""
        rows = [
            {"pid": "", "pname": "Jane Smith"},
            {"pid": "PR2", "pname": "John Doe"},
        ]
        pracs = Practitioner.from_rows(
            rows,
            columns={"identifier": "pid", "name": "pname"},
            organization_identifier="ORG-1",
        )
        assert len(pracs) == 1
        assert pracs[0].identifier == "PR2"

    def test_name_and_given_name_conflict(self):
        """Cannot use 'name' together with 'given_name' or 'family_name'."""
        rows = [{"pid": "PR1"}]
        with pytest.raises(ValueError, match="name.*given_name"):
            Practitioner.from_rows(
                rows,
                columns={"identifier": "pid", "name": "n", "given_name": "gn"},
                organization_identifier="ORG-1",
            )


class TestFindPatientId:
    """Test CavellClient.find_patient_id()."""

    def test_find_returns_id(self, client, httpx_mock):
        """find_patient_id returns the FHIR ID when patient exists."""
        mock_fhir_auth(httpx_mock)

        httpx_mock.add_response(
            method="GET",
            url=f"http://localhost:8080/fhir/Patient?identifier={IDENTIFIER_SYSTEM_ENCODED}%7CMRN-123",
            json={
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Patient", "id": "pat-42"}}],
            },
        )

        assert client.find_patient_id("MRN-123") == "pat-42"

    def test_find_returns_none(self, client, httpx_mock):
        """find_patient_id returns None when patient not found."""
        mock_fhir_auth(httpx_mock)

        httpx_mock.add_response(
            method="GET",
            url=f"http://localhost:8080/fhir/Patient?identifier={IDENTIFIER_SYSTEM_ENCODED}%7Cunknown",
            json={"resourceType": "Bundle", "entry": []},
        )

        assert client.find_patient_id("unknown") is None


class TestPipelineStats:
    """Test cumulative stats: processed, failed, total_cost."""

    def test_stats_zero_on_init(self, client):
        """Properties start at 0."""
        pipeline = IngestionPipeline(client)
        assert pipeline.documents_processed == 0
        assert pipeline.documents_failed == 0
        assert pipeline.total_cost == 0.0

    def test_stats_after_success(self, client, httpx_mock):
        """documents_processed increments and total_cost accumulates on success."""
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])
        mock_patient_exists(httpx_mock, "pat-1")

        pipeline = IngestionPipeline(client, default_organization="ORG-1")
        pipeline.seed(
            organizations=[Organization(identifier="ORG-1", name="Org")],
            patients=[Patient(identifier="MRN-1")],
        )

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1, estimated_cost=0.01)
        mock_persist_response(httpx_mock, created=1)

        outcomes = list(
            pipeline.extract(
                [
                    Document(
                        text="test note text here",
                        patient_identifier="MRN-1",
                        date="2024-01-01",
                        document_id="d1",
                    )
                ]
            )
        )

        assert len(outcomes) == 1
        assert outcomes[0].success
        assert pipeline.documents_processed == 1
        assert pipeline.documents_failed == 0
        assert pipeline.total_cost == pytest.approx(0.01)

    def test_stats_after_failure(self, client, httpx_mock):
        """documents_failed increments on failure."""
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])
        mock_patient_exists(httpx_mock, "pat-1")

        pipeline = IngestionPipeline(client, default_organization="ORG-1")
        pipeline.seed(
            organizations=[Organization(identifier="ORG-1", name="Org")],
            patients=[Patient(identifier="MRN-1")],
        )

        # Mock context fetch to raise an error
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Condition?subject=pat-1&_count=500",
            status_code=500,
        )

        outcomes = list(
            pipeline.extract(
                [
                    Document(
                        text="test note text here",
                        patient_identifier="MRN-1",
                        date="2024-01-01",
                        document_id="d1",
                    )
                ]
            )
        )

        assert len(outcomes) == 1
        assert not outcomes[0].success
        assert pipeline.documents_failed == 1
        assert pipeline.documents_processed == 0

    def test_stats_accumulate_across_calls(self, client, httpx_mock):
        """Stats add up across multiple extract() calls."""
        mock_fhir_auth(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])
        mock_patient_exists(httpx_mock, "pat-1")

        pipeline = IngestionPipeline(client, default_organization="ORG-1")
        pipeline.seed(
            organizations=[Organization(identifier="ORG-1", name="Org")],
            patients=[Patient(identifier="MRN-1")],
        )

        # First call
        mock_empty_document_identifiers(httpx_mock)
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1, estimated_cost=0.01)
        mock_persist_response(httpx_mock, created=1)

        list(
            pipeline.extract(
                [
                    Document(
                        text="first note text here",
                        patient_identifier="MRN-1",
                        date="2024-01-01",
                        document_id="d1",
                    )
                ]
            )
        )

        # Second call
        mock_empty_document_identifiers(httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1, estimated_cost=0.02)
        mock_persist_response(httpx_mock, created=1)

        list(
            pipeline.extract(
                [
                    Document(
                        text="second note text here",
                        patient_identifier="MRN-1",
                        date="2024-02-01",
                        document_id="d2",
                    )
                ]
            )
        )

        assert pipeline.documents_processed == 2
        assert pipeline.total_cost == pytest.approx(0.03)

    def test_stats_updated_before_yield(self, client, httpx_mock):
        """Inside the loop, counter equals outcomes seen so far."""
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])
        mock_patient_exists(httpx_mock, "pat-1")

        pipeline = IngestionPipeline(
            client, default_organization="ORG-1", max_concurrency=1
        )
        pipeline.seed(
            organizations=[Organization(identifier="ORG-1", name="Org")],
            patients=[Patient(identifier="MRN-1")],
        )

        # Two documents for the same patient (processed sequentially)
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1, estimated_cost=0.01)
        mock_persist_response(httpx_mock, created=1)
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1, estimated_cost=0.02)
        mock_persist_response(httpx_mock, created=1)

        results = pipeline.extract(
            [
                Document(
                    text="first note text here",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    document_id="d1",
                ),
                Document(
                    text="second note text here",
                    patient_identifier="MRN-1",
                    date="2024-02-01",
                    document_id="d2",
                ),
            ]
        )

        assert len(results) == 2
        assert pipeline.documents_processed == 2

    def test_progress_log(self, client, httpx_mock, caplog):
        """caplog captures the 'Extracting N documents' message."""
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])
        mock_patient_exists(httpx_mock, "pat-1")

        pipeline = IngestionPipeline(client, default_organization="ORG-1")
        pipeline.seed(
            organizations=[Organization(identifier="ORG-1", name="Org")],
            patients=[Patient(identifier="MRN-1")],
        )

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1, estimated_cost=0.01)
        mock_persist_response(httpx_mock, created=1)

        with caplog.at_level(logging.INFO, logger="cavell_client.ingestion"):
            list(
                pipeline.extract(
                    [
                        Document(
                            text="test note text here",
                            patient_identifier="MRN-1",
                            date="2024-01-01",
                            document_id="d1",
                        )
                    ]
                )
            )

        assert any(
            "Extracting 1 documents across 1 patient" in m for m in caplog.messages
        )

    def test_deleted_patient_raises_before_extraction(self, client, httpx_mock):
        """extract() fails fast when a patient was deleted from FHIR."""
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client, default_organization="ORG-1")
        pipeline.seed(
            organizations=[Organization(identifier="ORG-1", name="Org")],
            patients=[Patient(identifier="MRN-1")],
        )

        # Patient was deleted — HAPI returns 410 Gone
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient/pat-1",
            status_code=410,
        )

        with pytest.raises(RuntimeError, match="seed\\(\\)"):
            list(
                pipeline.extract(
                    [
                        Document(
                            text="test note text here",
                            patient_identifier="MRN-1",
                            date="2024-01-01",
                            document_id="d1",
                        )
                    ]
                )
            )


def _extract_posts(httpx_mock):
    """Count POSTs to the extraction endpoint."""
    return [
        r
        for r in httpx_mock.get_requests()
        if r.method == "POST" and str(r.url).endswith("/extract/text")
    ]


def _last_persisted_bundle(httpx_mock):
    """Return the body of the last transaction POST to the FHIR server."""
    posts = [
        r
        for r in httpx_mock.get_requests()
        if r.method == "POST" and str(r.url) == "http://localhost:8080/fhir/"
    ]
    return json.loads(posts[-1].content)


class _PipelineHarness:
    """Shared seeding setup for abort/chronology tests."""

    def _seed(self, client, httpx_mock, num_patients=1):
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(
            httpx_mock, [f"pat-{i + 1}" for i in range(num_patients)]
        )
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(
            httpx_mock,
            [
                ("201 Created", f"Patient/pat-{i + 1}/_history/1")
                for i in range(num_patients)
            ],
        )
        mock_patient_exists(httpx_mock, [f"pat-{i + 1}" for i in range(num_patients)])

        pipeline = IngestionPipeline(client, default_organization="CGH-001")
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[
                Patient(identifier=f"MRN-{i + 1}", managing_organization="CGH-001")
                for i in range(num_patients)
            ],
        )
        return pipeline


class TestExtractInputValidation(_PipelineHarness):
    """extract()/extract_all() reject anything that is not a Document.

    Field contents are Document's own business — this is only about the type,
    so the mistake surfaces up front instead of as an AttributeError inside a
    worker thread mid-run.
    """

    @staticmethod
    def _valid():
        return Document(
            text="Clinical note long enough to avoid the short-text warning.",
            patient_identifier="MRN-1",
            date="2024-01-15",
            document_id="doc-1",
        )

    @pytest.mark.parametrize("method", ["extract", "extract_all"])
    def test_rejects_non_document_items(self, client, httpx_mock, method):
        """A raw CSV row is the obvious mistake — it must not reach a worker."""
        pipeline = self._seed(client, httpx_mock)
        rows = [{"patient_id": "MRN-1", "note_text": "t", "note_date": "2024-01-15"}]

        with pytest.raises(TypeError, match="are not Document objects") as exc:
            getattr(pipeline, method)(rows)

        assert "index 0: got dict" in str(exc.value)

    def test_reports_every_offending_position(self, client, httpx_mock):
        pipeline = self._seed(client, httpx_mock)

        with pytest.raises(TypeError) as exc:
            pipeline.extract([self._valid(), "nope", 42])

        message = str(exc.value)
        assert "2 of 3 item(s) are not Document objects" in message
        assert "index 1: got str" in message
        assert "index 2: got int" in message

    def test_rejects_before_any_request(self, client, httpx_mock):
        """The check runs ahead of the API pre-flight, so it costs no request."""
        pipeline = self._seed(client, httpx_mock)
        before = len(httpx_mock.get_requests())

        with pytest.raises(TypeError):
            pipeline.extract(["not a document"])

        assert len(httpx_mock.get_requests()) == before

    def test_truncates_a_long_list(self, client, httpx_mock):
        pipeline = self._seed(client, httpx_mock)

        with pytest.raises(TypeError) as exc:
            pipeline.extract([{}] * 14)

        assert "... and 4 more" in str(exc.value)

    def test_documents_pass_through(self, client, httpx_mock):
        """The guard must not reject anything the pipeline used to accept."""
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        outcomes = pipeline.extract([self._valid()])

        assert [o.success for o in outcomes] == [True]


class TestRunAbort(_PipelineHarness):
    """Run-global failures (401, exhausted 503) abort the whole run."""

    def test_preflight_401_aborts_before_any_extraction(self, client, httpx_mock):
        """A 401 on the pre-flight raises before any extract call is made."""
        mock_fhir_auth(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])

        pipeline = IngestionPipeline(client, default_organization="CGH-001")
        pipeline.seed(
            organizations=[Organization(identifier="CGH-001", name="City General")],
            patients=[Patient(identifier="MRN-1", managing_organization="CGH-001")],
        )

        httpx_mock.add_response(
            method="GET",
            url="https://qa.prism.cavell.app/api/key/info",
            status_code=401,
            json={"detail": "LLM Gateway rejected the provided key."},
        )

        from cavell_client.models import CavellAuthError

        docs = [
            Document(
                text="Patient has diabetes mellitus",
                patient_identifier="MRN-1",
                date="2024-01-01",
                document_id="doc-1",
            )
        ]
        with pytest.raises(CavellAuthError):
            pipeline.extract(docs)

        assert _extract_posts(httpx_mock) == []

    def test_mid_run_401_aborts_the_run(self, client, httpx_mock):
        """A 401 from extraction raises instead of cascading per-patient noise."""
        pipeline = self._seed(client, httpx_mock, num_patients=2)
        mock_patient_exists(httpx_mock, ["pat-1", "pat-2"])
        mock_context_empty(httpx_mock, "pat-1", repeat=True)
        mock_context_empty(httpx_mock, "pat-2", repeat=True)
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=401,
            json={"detail": "LLM Gateway rejected the provided key."},
            repeat=True,
        )

        from cavell_client.models import CavellAuthError

        docs = [
            Document(
                text="Note 1 for patient 1 with enough text",
                patient_identifier="MRN-1",
                date="2024-01-01",
                document_id="doc-1",
            ),
            Document(
                text="Note 2 for patient 1 with enough text",
                patient_identifier="MRN-1",
                date="2024-01-02",
                document_id="doc-2",
            ),
            Document(
                text="Note 1 for patient 2 with enough text",
                patient_identifier="MRN-2",
                date="2024-01-01",
                document_id="doc-3",
            ),
            Document(
                text="Note 2 for patient 2 with enough text",
                patient_identifier="MRN-2",
                date="2024-01-02",
                document_id="doc-4",
            ),
        ]
        with pytest.raises(CavellAuthError):
            pipeline.extract(docs)

        # At most one extraction attempt per patient; never the later docs.
        assert 1 <= len(_extract_posts(httpx_mock)) <= 2

    def test_exhausted_503_aborts_without_deferred_pass(
        self, client, httpx_mock, monkeypatch
    ):
        """A persistent 503 exhausts in-place retries, then aborts the run."""
        monkeypatch.setattr("cavell_client.ingestion.time.sleep", lambda s: None)
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_context_empty(httpx_mock, "pat-1", repeat=True)
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=503,
            json={"detail": "LLM Gateway is unreachable; try again shortly."},
            repeat=True,
        )

        from cavell_client.models import CavellGatewayUnavailableError

        docs = [
            Document(
                text="Patient has diabetes mellitus",
                patient_identifier="MRN-1",
                date="2024-01-01",
                document_id="doc-1",
            )
        ]
        with pytest.raises(CavellGatewayUnavailableError):
            pipeline.extract(docs)

        # In-place retries ran (3 attempts), the deferred pass did not (else 6).
        assert len(_extract_posts(httpx_mock)) == _DOC_MAX_ATTEMPTS
        assert pipeline.documents_failed == 1

    def test_single_503_then_success(self, client, httpx_mock, monkeypatch):
        """One 503 blip is retried in place and the run continues normally."""
        monkeypatch.setattr("cavell_client.ingestion.time.sleep", lambda s: None)
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_context_empty(httpx_mock, "pat-1", repeat=True)
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=503,
            json={"detail": "LLM Gateway is unreachable; try again shortly."},
        )
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        docs = [
            Document(
                text="Patient has diabetes mellitus",
                patient_identifier="MRN-1",
                date="2024-01-01",
                document_id="doc-1",
            )
        ]
        outcomes = pipeline.extract(docs)

        assert len(outcomes) == 1
        assert outcomes[0].success
        assert len(_extract_posts(httpx_mock)) == 2


class TestChronologyGuard(_PipelineHarness):
    """Chronology handling: in-order documents pass, watermark failures fail open.

    The update-guard behaviour (drop updates sourced from newer documents) is
    disabled: an older document is extracted against split context instead, and
    the API decides what to reconcile — see TestOutOfOrderExtraction. The one
    test covering the guard is skipped rather than deleted, because letting the
    API decide is provisional.
    """

    RELATED_URL = (
        "http://localhost:8080/fhir/DocumentReference?patient=pat-1"
        "&identifier=urn%3Acavell%3Adocument%7C&_elements=date%2Ccontext"
        "&_count=1000"
    )

    @staticmethod
    def _guard_bundle():
        """Extraction bundle with one create and three updates."""

        def put(rid):
            return {
                "resource": {"resourceType": "Condition", "id": rid},
                "request": {"method": "PUT", "url": f"Condition/{rid}"},
            }

        return {
            "bundle": {
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": [
                    {
                        "fullUrl": "urn:uuid:new-condition",
                        "resource": {"resourceType": "Condition"},
                        "request": {"method": "POST", "url": "Condition"},
                    },
                    put("c-newer"),
                    put("c-equal"),
                    put("c-unknown"),
                ],
            },
            "count": 4,
        }

    @pytest.mark.skip(
        reason="Update guard is disabled: older documents are extracted "
        "against split context and the API reconciles (see "
        "TestOutOfOrderExtraction). Kept for the provisional revert to "
        "guarding client-side — re-enable together with the commented-out "
        "block in _process_single_document."
    )
    def test_out_of_order_drops_updates_from_newer_documents(
        self, client, httpx_mock, caplog
    ):
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_watermark(httpx_mock, "pat-1", date="2024-06-01T00:00:00Z")
        mock_context_empty(httpx_mock, "pat-1")
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json=self._guard_bundle(),
        )
        httpx_mock.add_response(
            method="GET",
            url=self.RELATED_URL,
            json={
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "DocumentReference",
                            "date": "2024-06-01T00:00:00Z",
                            "context": {
                                "related": [{"reference": "Condition/c-newer"}]
                            },
                        }
                    },
                    {
                        "resource": {
                            "resourceType": "DocumentReference",
                            "date": "2024-05-01T00:00:00Z",
                            "context": {
                                "related": [{"reference": "Condition/c-equal"}]
                            },
                        }
                    },
                ],
            },
        )
        mock_persist_response(httpx_mock, created=3)

        docs = [
            Document(
                text="An older note about the patient's diabetes",
                patient_identifier="MRN-1",
                date="2024-05-01",
                document_id="doc-1",
            )
        ]
        with caplog.at_level(logging.WARNING):
            outcomes = pipeline.extract(docs)

        assert outcomes[0].success
        assert outcomes[0].out_of_order is True
        assert any(
            "older than the newest persisted" in r.message for r in caplog.records
        )

        persisted = _last_persisted_bundle(httpx_mock)
        persisted_ids = {e["resource"].get("id") for e in persisted["entry"]}
        # The update sourced from a NEWER document is dropped; the create,
        # the equal-date update, and the unknown-provenance update persist.
        assert "c-newer" not in persisted_ids
        assert "c-equal" in persisted_ids
        assert "c-unknown" in persisted_ids
        assert len(persisted["entry"]) == 3

    def test_in_order_document_skips_guard_queries(self, client, httpx_mock):
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_watermark(httpx_mock, "pat-1", date="2024-01-01T00:00:00Z")
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        docs = [
            Document(
                text="A newer note about the patient's diabetes",
                patient_identifier="MRN-1",
                date="2024-03-01",
                document_id="doc-1",
            )
        ]
        outcomes = pipeline.extract(docs)

        assert outcomes[0].success
        assert outcomes[0].out_of_order is False
        assert all(
            "_elements=date%2Ccontext" not in str(r.url)
            for r in httpx_mock.get_requests()
        )

    def test_same_day_document_is_not_out_of_order(self, client, httpx_mock):
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_watermark(httpx_mock, "pat-1", date="2024-03-01T00:00:00Z")
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        docs = [
            Document(
                text="A same-day note about the patient's diabetes",
                patient_identifier="MRN-1",
                date="2024-03-01",
                document_id="doc-1",
            )
        ]
        outcomes = pipeline.extract(docs)

        assert outcomes[0].success
        assert outcomes[0].out_of_order is False

    def test_watermark_failure_is_fail_open(self, client, httpx_mock, caplog):
        """An unreachable watermark query disables the check, not the run."""
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        # Watermark query errors out -> the guard fails open.
        mock_watermark(httpx_mock, "pat-1", status_code=500)
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        docs = [
            Document(
                text="Patient has diabetes mellitus",
                patient_identifier="MRN-1",
                date="2024-01-01",
                document_id="doc-1",
            )
        ]
        with caplog.at_level(logging.WARNING):
            outcomes = pipeline.extract(docs)

        assert outcomes[0].success
        assert outcomes[0].out_of_order is False
        assert any("chronology check skipped" in r.message for r in caplog.records)


class TestApplyUpdateGuard:
    """Unit tests for the pure update-guard function."""

    @staticmethod
    def _put(rid, rtype="Condition"):
        return {
            "resource": {"resourceType": rtype, "id": rid},
            "request": {"method": "PUT", "url": f"{rtype}/{rid}"},
        }

    @staticmethod
    def _create():
        return {
            "fullUrl": "urn:uuid:x",
            "resource": {"resourceType": "Condition"},
            "request": {"method": "POST", "url": "Condition"},
        }

    def test_drops_put_from_newer_document(self):
        from cavell_client.ingestion import _apply_update_guard

        kept, dropped = _apply_update_guard(
            [self._put("c1")], "2024-05-01", {"Condition/c1": "2024-06-01"}
        )
        assert kept == []
        assert dropped == ["Condition/c1"]

    def test_keeps_put_from_older_document(self):
        from cavell_client.ingestion import _apply_update_guard

        entries = [self._put("c1")]
        kept, dropped = _apply_update_guard(
            entries, "2024-05-01", {"Condition/c1": "2024-04-01"}
        )
        assert kept == entries
        assert dropped == []

    def test_keeps_put_with_equal_date(self):
        from cavell_client.ingestion import _apply_update_guard

        entries = [self._put("c1")]
        kept, dropped = _apply_update_guard(
            entries, "2024-05-01", {"Condition/c1": "2024-05-01"}
        )
        assert kept == entries
        assert dropped == []

    def test_keeps_put_with_unknown_provenance(self):
        from cavell_client.ingestion import _apply_update_guard

        entries = [self._put("c1")]
        kept, dropped = _apply_update_guard(entries, "2024-05-01", {})
        assert kept == entries
        assert dropped == []

    def test_creates_always_pass(self):
        from cavell_client.ingestion import _apply_update_guard

        entries = [self._create()]
        kept, dropped = _apply_update_guard(
            entries, "2024-05-01", {"Condition/c1": "2024-06-01"}
        )
        assert kept == entries
        assert dropped == []


class TestOutcomeStr:
    """IngestionOutcome.__str__ surfaces transient and out-of-order flags."""

    def test_out_of_order_marker_on_success(self):
        from cavell_client.ingestion import IngestionOutcome

        outcome = IngestionOutcome(
            success=True,
            patient_identifier="MRN-1",
            document_index=0,
            document_id="doc-1",
            out_of_order=True,
        )
        assert "[out-of-order]" in str(outcome)

    def test_transient_marker_on_failure(self):
        from cavell_client.ingestion import IngestionOutcome

        outcome = IngestionOutcome(
            success=False,
            patient_identifier="MRN-1",
            document_index=0,
            error="boom",
            transient=True,
        )
        assert "[transient]" in str(outcome)


class TestDateNormalization:
    """Dates are canonicalized, not just validated (chronology is lexical)."""

    def test_compact_iso_date_normalized(self):
        doc = Document(
            text="t",
            patient_identifier="MRN-1",
            date="20240115",
            document_id="doc-1",
        )
        assert doc.date == "2024-01-15"

    def test_week_date_normalized(self):
        doc = Document(
            text="t",
            patient_identifier="MRN-1",
            date="2024-W03-1",
            document_id="doc-1",
        )
        assert doc.date == "2024-01-15"


class TestDeferredPassAbort(_PipelineHarness):
    """A run-global failure during the deferred pass must raise too."""

    def test_503_in_deferred_pass_raises(self, client, httpx_mock, monkeypatch):
        monkeypatch.setattr("cavell_client.ingestion.time.sleep", lambda s: None)
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_context_empty(httpx_mock, "pat-1", repeat=True)
        # Main pass: transient 500s exhaust in-place retries (no abort),
        # marking the doc transient and triggering the deferred pass.
        for _ in range(_DOC_MAX_ATTEMPTS):
            httpx_mock.add_response(
                method="POST",
                url="https://qa.prism.cavell.app/api/extract/text",
                status_code=500,
                json={"detail": "boom"},
            )
        # Deferred pass: gateway goes down hard -> run-global.
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=503,
            json={"detail": "LLM Gateway is unreachable; try again shortly."},
            repeat=True,
        )

        from cavell_client.models import CavellGatewayUnavailableError

        docs = [
            Document(
                text="Patient has diabetes mellitus",
                patient_identifier="MRN-1",
                date="2024-01-01",
                document_id="doc-1",
            )
        ]
        with pytest.raises(CavellGatewayUnavailableError):
            pipeline.extract(docs)
        assert pipeline.documents_failed == 1


class TestPractitionerRefListCleanup:
    """Unmatched practitioner refs inside lists must not leave [{}] behind."""

    def test_list_items_pruned(self):
        resource = {
            "resourceType": "Observation",
            "performer": [{"reference": "urn:uuid:prac-1"}],
            "note": [{"text": "keep me"}],
        }
        IngestionPipeline._rewrite_practitioner_refs(
            resource, {"urn:uuid:prac-1": None}
        )
        assert "performer" not in resource
        assert resource["note"] == [{"text": "keep me"}]

    def test_nested_actor_pruned(self):
        resource = {
            "resourceType": "Procedure",
            "performer": [
                {"actor": {"reference": "urn:uuid:prac-1"}},
                {"actor": {"reference": "Practitioner/42"}},
            ],
        }
        IngestionPipeline._rewrite_practitioner_refs(
            resource, {"urn:uuid:prac-1": None}
        )
        assert resource["performer"] == [{"actor": {"reference": "Practitioner/42"}}]

    def test_matched_refs_rewritten_in_lists(self):
        resource = {
            "resourceType": "Observation",
            "performer": [{"reference": "urn:uuid:prac-1"}],
        }
        IngestionPipeline._rewrite_practitioner_refs(
            resource, {"urn:uuid:prac-1": "Practitioner/7"}
        )
        assert resource["performer"] == [{"reference": "Practitioner/7"}]


def _note(patient, date, doc_id):
    """A valid Document with text long enough to avoid the short-text warning.

    The text includes ``doc_id`` so same-patient, same-day notes stay distinct:
    identical content is what ``extract_all``'s duplicate-content pass drops.
    """
    return Document(
        text=f"Clinical note {doc_id} for {patient} recorded on {date}.",
        patient_identifier=patient,
        date=date,
        document_id=doc_id,
    )


class TestExtractAll(_PipelineHarness):
    """extract_all() walks the whole dataset in global date order."""

    def _seed_for_batches(self, client, httpx_mock, num_patients=1):
        """Seed, then allow the repeated patient-existence checks each batch makes."""
        pipeline = self._seed(client, httpx_mock, num_patients=num_patients)
        mock_patient_exists(
            httpx_mock, [f"pat-{i + 1}" for i in range(num_patients)], repeat=True
        )
        return pipeline

    @staticmethod
    def _record(pipeline, monkeypatch):
        """Replace real extraction with a recorder. Returns the docs seen."""
        seen: list[Document] = []

        def fake(
            doc, doc_index, organization_identifier, watermark=None, on_fatal=None
        ):
            seen.append(doc)
            return IngestionOutcome(
                success=True,
                patient_identifier=doc.patient_identifier,
                document_index=doc_index,
                document_id=doc.document_id,
            )

        monkeypatch.setattr(pipeline, "_process_single_document", fake)
        return seen

    def test_processes_every_document_when_batch_smaller_than_dataset(
        self, client, httpx_mock, monkeypatch
    ):
        """The regression that motivated this method: extract() would stop at one
        batch, leaving the rest of the dataset unprocessed."""
        pipeline = self._seed_for_batches(client, httpx_mock)
        seen = self._record(pipeline, monkeypatch)

        docs = [_note("MRN-1", f"2024-01-{i + 1:02d}", f"doc-{i}") for i in range(7)]

        outcomes = pipeline.extract_all(docs, batch_size=2)

        assert len(outcomes) == 7
        assert all(o.success for o in outcomes)
        assert len(seen) == 7

    def test_batch_membership_follows_global_date_order(
        self, client, httpx_mock, monkeypatch
    ):
        """Batches are cut on global date order, not on input order."""
        pipeline = self._seed_for_batches(client, httpx_mock, num_patients=2)
        self._record(pipeline, monkeypatch)

        # Shuffled, interleaving two patients.
        docs = [
            _note("MRN-1", "2024-05-01", "a-may"),
            _note("MRN-2", "2024-01-01", "b-jan"),
            _note("MRN-1", "2024-03-01", "a-mar"),
            _note("MRN-2", "2024-07-01", "b-jul"),
        ]

        batches: list[list[IngestionOutcome]] = []
        pipeline.extract_all(docs, batch_size=2, on_batch=batches.append)

        assert [sorted(o.document_id or "" for o in b) for b in batches] == [
            ["a-mar", "b-jan"],
            ["a-may", "b-jul"],
        ]

    def test_documents_reach_extraction_oldest_first_across_batches(
        self, client, httpx_mock, monkeypatch
    ):
        """One patient's notes stay chronological even when split across batches."""
        pipeline = self._seed_for_batches(client, httpx_mock)
        seen = self._record(pipeline, monkeypatch)

        docs = [
            _note("MRN-1", "2024-09-01", "sep"),
            _note("MRN-1", "2024-02-01", "feb"),
            _note("MRN-1", "2024-11-01", "nov"),
            _note("MRN-1", "2024-05-01", "may"),
        ]

        pipeline.extract_all(docs, batch_size=2)

        assert [d.document_id for d in seen] == ["feb", "may", "sep", "nov"]

    def test_same_day_documents_keep_input_order(self, client, httpx_mock, monkeypatch):
        """The global sort is stable, so same-day notes are not reshuffled."""
        pipeline = self._seed_for_batches(client, httpx_mock)
        seen = self._record(pipeline, monkeypatch)

        docs = [
            _note("MRN-1", "2024-03-01", "first"),
            _note("MRN-1", "2024-03-01", "second"),
            _note("MRN-1", "2024-03-01", "third"),
        ]

        pipeline.extract_all(docs, batch_size=2)

        assert [d.document_id for d in seen] == ["first", "second", "third"]

    def test_duplicate_content_under_different_ids_extracted_once(self):
        """Re-exported copies of one note must not each be extracted.

        The resume-skip keys on document_id, so a duplicate carrying a new id
        looks like a new document and would duplicate that event's resources.
        """
        docs = [
            _note("MRN-1", "2024-03-01", "note-1"),
            Document(
                text=_note("MRN-1", "2024-03-01", "note-1").text,
                patient_identifier="MRN-1",
                date="2024-03-01",
                document_id="note-9999",
            ),
        ]

        kept, dropped = _dedupe_documents_by_content(docs)

        assert dropped == 1
        assert [d.document_id for d in kept] == ["note-1"]

    def test_same_text_on_different_dates_kept(self):
        """Copy-forward across two encounters is two real events, not a dup."""
        a = _note("MRN-1", "2024-03-01", "a")
        b = Document(
            text=a.text,
            patient_identifier="MRN-1",
            date="2024-06-01",
            document_id="b",
        )

        kept, dropped = _dedupe_documents_by_content([a, b])

        assert dropped == 0
        assert len(kept) == 2

    def test_same_text_different_patients_kept(self):
        a = _note("MRN-1", "2024-03-01", "a")
        b = Document(
            text=a.text,
            patient_identifier="MRN-2",
            date="2024-03-01",
            document_id="b",
        )

        kept, dropped = _dedupe_documents_by_content([a, b])

        assert dropped == 0
        assert len(kept) == 2

    def test_dedupe_content_false_processes_every_copy(
        self, client, httpx_mock, monkeypatch
    ):
        pipeline = self._seed_for_batches(client, httpx_mock)
        seen = self._record(pipeline, monkeypatch)

        base = _note("MRN-1", "2024-03-01", "note-1")
        docs = [
            base,
            Document(
                text=base.text,
                patient_identifier="MRN-1",
                date="2024-03-01",
                document_id="note-2",
            ),
        ]

        pipeline.extract_all(docs, dedupe_content=False)

        assert [d.document_id for d in seen] == ["note-1", "note-2"]

    def test_duplicates_dropped_before_extraction(
        self, client, httpx_mock, monkeypatch
    ):
        pipeline = self._seed_for_batches(client, httpx_mock)
        seen = self._record(pipeline, monkeypatch)

        base = _note("MRN-1", "2024-03-01", "note-1")
        docs = [
            base,
            Document(
                text=base.text,
                patient_identifier="MRN-1",
                date="2024-03-01",
                document_id="note-2",
            ),
        ]

        outcomes = pipeline.extract_all(docs)

        assert [d.document_id for d in seen] == ["note-1"]
        assert len(outcomes) == 1

    def test_batch_size_none_processes_everything_in_one_call(
        self, client, httpx_mock, monkeypatch
    ):
        pipeline = self._seed_for_batches(client, httpx_mock)
        self._record(pipeline, monkeypatch)

        docs = [_note("MRN-1", f"2024-01-{i + 1:02d}", f"doc-{i}") for i in range(5)]

        batches: list[list[IngestionOutcome]] = []
        outcomes = pipeline.extract_all(docs, on_batch=batches.append)

        assert len(outcomes) == 5
        assert len(batches) == 1

    def test_input_list_is_not_mutated(self, client, httpx_mock, monkeypatch):
        """Sorting must not reorder the caller's list in place."""
        pipeline = self._seed_for_batches(client, httpx_mock)
        self._record(pipeline, monkeypatch)

        docs = [
            _note("MRN-1", "2024-09-01", "sep"),
            _note("MRN-1", "2024-02-01", "feb"),
        ]

        pipeline.extract_all(docs, batch_size=1)

        assert [d.document_id for d in docs] == ["sep", "feb"]

    def test_validation_runs_before_any_batch_spends(
        self, client, httpx_mock, monkeypatch
    ):
        """A bad reference in the last batch raises before the first one runs."""
        pipeline = self._seed_for_batches(client, httpx_mock)
        seen = self._record(pipeline, monkeypatch)

        docs = [
            _note("MRN-1", "2024-01-01", "ok-1"),
            _note("MRN-1", "2024-02-01", "ok-2"),
            _note("MRN-UNSEEDED", "2024-03-01", "bad"),
        ]

        with pytest.raises(ValueError, match="unknown patient 'MRN-UNSEEDED'"):
            pipeline.extract_all(docs, batch_size=1)

        assert seen == []

    def test_duplicate_document_ids_split_across_batches_are_rejected(
        self, client, httpx_mock, monkeypatch
    ):
        """Per-batch checks alone would miss this — extract() sees one batch."""
        pipeline = self._seed_for_batches(client, httpx_mock)
        seen = self._record(pipeline, monkeypatch)

        docs = [
            _note("MRN-1", "2024-01-01", "dupe"),
            _note("MRN-1", "2024-02-01", "other"),
            _note("MRN-1", "2024-03-01", "dupe"),
        ]

        with pytest.raises(ValueError, match="Duplicate document_id values"):
            pipeline.extract_all(docs, batch_size=1)

        assert seen == []

    def test_same_document_id_across_patients_is_allowed(
        self, client, httpx_mock, monkeypatch
    ):
        """document_id uniqueness is scoped per patient, as in extract()."""
        pipeline = self._seed_for_batches(client, httpx_mock, num_patients=2)
        self._record(pipeline, monkeypatch)

        docs = [
            _note("MRN-1", "2024-01-01", "note-1"),
            _note("MRN-2", "2024-02-01", "note-1"),
        ]

        outcomes = pipeline.extract_all(docs, batch_size=1)

        assert len(outcomes) == 2

    def test_empty_list_returns_empty(self, client, httpx_mock):
        pipeline = self._seed_for_batches(client, httpx_mock)

        assert pipeline.extract_all([], batch_size=10) == []

    @pytest.mark.parametrize("bad", [0, -1])
    def test_batch_size_below_one_raises(self, client, httpx_mock, bad):
        pipeline = self._seed_for_batches(client, httpx_mock)

        with pytest.raises(ValueError, match="batch_size must be >= 1"):
            pipeline.extract_all([_note("MRN-1", "2024-01-01", "d")], batch_size=bad)

    def test_requires_seeded_patients(self, client, httpx_mock):
        mock_fhir_auth(httpx_mock)
        pipeline = IngestionPipeline(client, default_organization="CGH-001")

        with pytest.raises(RuntimeError, match="requires phase 'patients_seeded'"):
            pipeline.extract_all([_note("MRN-1", "2024-01-01", "d")])

    def test_cumulative_stats_span_all_batches(self, client, httpx_mock, monkeypatch):
        pipeline = self._seed_for_batches(client, httpx_mock)
        self._record(pipeline, monkeypatch)

        docs = [_note("MRN-1", f"2024-01-{i + 1:02d}", f"doc-{i}") for i in range(6)]

        pipeline.extract_all(docs, batch_size=2)

        assert pipeline.documents_processed == 6
        assert pipeline.documents_failed == 0

    def test_full_pipeline_across_batches(self, client, httpx_mock):
        """End-to-end through the real HTTP path, not the recorder stub."""
        pipeline = self._seed_for_batches(client, httpx_mock)
        mock_context_empty(httpx_mock, "pat-1", repeat=True)
        for _ in range(4):
            mock_extract_response(httpx_mock, count=1)
            mock_persist_response(httpx_mock, created=1)

        docs = [
            _note("MRN-1", "2024-07-01", "jul"),
            _note("MRN-1", "2024-01-01", "jan"),
            _note("MRN-1", "2024-10-01", "oct"),
            _note("MRN-1", "2024-04-01", "apr"),
        ]

        outcomes = pipeline.extract_all(docs, batch_size=2)

        assert len(outcomes) == 4
        assert all(o.success for o in outcomes)
        assert not any(o.out_of_order for o in outcomes)
        assert pipeline.documents_processed == 4

        # The API saw the notes oldest-first, spanning both batches.
        extract_bodies = [
            json.loads(r.content)
            for r in httpx_mock.get_requests()
            if str(r.url).endswith("/api/extract/text")
        ]
        assert [b["document_date"] for b in extract_bodies] == [
            "2024-01-01",
            "2024-04-01",
            "2024-07-01",
            "2024-10-01",
        ]


class TestOutOfOrderExtraction(_PipelineHarness):
    """Documents older than the patient's newest persisted one are extracted.

    They take the split-context path: the extraction API is shown the record as
    of the document's own date, and everything newer travels separately as
    ``future_context``. Nothing is refused — this replaces the refusal that
    0.5.0 shipped.
    """

    @staticmethod
    def _record(pipeline, monkeypatch, transient_ids=()):
        """Replace real extraction with a recorder. Returns the docs seen."""
        seen: list[Document] = []

        def fake(
            doc, doc_index, organization_identifier, watermark=None, on_fatal=None
        ):
            seen.append(doc)
            return IngestionOutcome(
                success=doc.document_id not in transient_ids,
                patient_identifier=doc.patient_identifier,
                document_index=doc_index,
                document_id=doc.document_id,
                error=None if doc.document_id not in transient_ids else "timed out",
                transient=doc.document_id in transient_ids,
            )

        monkeypatch.setattr(pipeline, "_process_single_document", fake)
        return seen

    def test_older_document_is_extracted(self, client, httpx_mock):
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_watermark(httpx_mock, "pat-1", date="2024-06-01T00:00:00Z")
        mock_context_split_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        (outcome,) = pipeline.extract([_note("MRN-1", "2024-05-01", "older")])

        assert outcome.success is True
        assert outcome.out_of_order is True
        assert outcome.document_id == "older"
        assert pipeline.documents_processed == 1
        assert pipeline.documents_failed == 0

    def test_payload_carries_the_out_of_order_flag(self, client, httpx_mock):
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_watermark(httpx_mock, "pat-1", date="2024-06-01T00:00:00Z")
        mock_context_split_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        pipeline.extract([_note("MRN-1", "2024-05-01", "older")])

        body = _extract_body(httpx_mock)
        assert body["out_of_order"] is True
        assert body["document_date"] == "2024-05-01"

    def test_payload_splits_context_around_the_document_date(self, client, httpx_mock):
        """The past side reaches `context`, the newer side `future_context`."""
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_watermark(httpx_mock, "pat-1", date="2024-06-01T00:00:00Z")
        mock_context_split_empty(httpx_mock, "pat-1")
        # Two Conditions on record: one known before the backdated note, one
        # that only a later note introduced.
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Condition?subject=pat-1&_count=500",
            json={
                "entry": [
                    {"resource": {"resourceType": "Condition", "id": "old"}},
                    {"resource": {"resourceType": "Condition", "id": "new"}},
                ]
            },
            replace=True,
            repeat=True,
        )
        mock_related_documents(
            httpx_mock,
            "pat-1",
            provenance=[
                ("2024-01-01", ["Condition/old"]),
                ("2024-06-01", ["Condition/new"]),
            ],
        )
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        pipeline.extract([_note("MRN-1", "2024-05-01", "older")])

        body = _extract_body(httpx_mock)
        assert [r["id"] for r in body["context"]] == ["old"]
        assert [r["id"] for r in body["future_context"]] == ["new"]

    def test_in_order_document_sends_neither_new_field(self, client, httpx_mock):
        """The forward path is untouched: no split, no provenance query."""
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_watermark(httpx_mock, "pat-1", date="2024-01-01T00:00:00Z")
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        pipeline.extract([_note("MRN-1", "2024-05-01", "newer")])

        body = _extract_body(httpx_mock)
        assert "out_of_order" not in body
        assert "future_context" not in body
        assert not any(
            "_elements=date%2Ccontext" in str(r.url) for r in httpx_mock.get_requests()
        )

    def test_other_patients_are_unaffected(self, client, httpx_mock, monkeypatch):
        pipeline = self._seed(client, httpx_mock, num_patients=3)
        mock_patient_exists(httpx_mock, ["pat-1", "pat-2", "pat-3"], repeat=True)
        mock_watermark(httpx_mock, "pat-1", date="2024-06-01T00:00:00Z")
        seen = self._record(pipeline, monkeypatch)

        docs = [
            _note("MRN-1", "2024-05-01", "a-old"),
            _note("MRN-2", "2024-05-01", "b-ok"),
            _note("MRN-3", "2024-05-01", "c-ok"),
        ]

        outcomes = pipeline.extract(docs)

        assert {d.document_id for d in seen} == {"a-old", "b-ok", "c-ok"}
        assert {o.document_id for o in outcomes if o.success} == {
            "a-old",
            "b-ok",
            "c-ok",
        }
        assert pipeline.documents_processed == 3
        assert pipeline.documents_failed == 0

    def test_document_index_points_at_the_callers_document(
        self, client, httpx_mock, monkeypatch
    ):
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_watermark(httpx_mock, "pat-1", date="2024-06-01T00:00:00Z")
        self._record(pipeline, monkeypatch)

        docs = [
            _note("MRN-1", "2024-07-01", "newer"),
            _note("MRN-1", "2024-05-01", "older"),
        ]

        outcomes = pipeline.extract(docs)

        by_id = {o.document_id: o for o in outcomes}
        assert docs[by_id["older"].document_index].document_id == "older"
        assert docs[by_id["newer"].document_index].document_id == "newer"

    def test_out_of_order_document_is_deferred_retried(
        self, client, httpx_mock, monkeypatch
    ):
        """A transient failure retries like any other — refusals used to not."""
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1", repeat=True)
        mock_watermark(httpx_mock, "pat-1", date="2024-06-01T00:00:00Z")
        seen = self._record(pipeline, monkeypatch, transient_ids={"older"})

        outcomes = pipeline.extract([_note("MRN-1", "2024-05-01", "older")])

        assert [d.document_id for d in seen] == ["older", "older"]
        assert len(outcomes) == 1

    def test_marks_every_out_of_order_document(self, client, httpx_mock, monkeypatch):
        pipeline = self._seed(client, httpx_mock, num_patients=2)
        mock_patient_exists(httpx_mock, ["pat-1", "pat-2"], repeat=True)
        mock_watermark(httpx_mock, "pat-1", date="2024-06-01T00:00:00Z")
        mock_watermark(httpx_mock, "pat-2", date="2025-01-01T00:00:00Z")
        watermarks = {}

        def fake(doc, doc_index, organization_identifier, watermark=None, **kw):
            watermarks[doc.document_id] = watermark
            return IngestionOutcome(
                success=True,
                patient_identifier=doc.patient_identifier,
                document_index=doc_index,
                document_id=doc.document_id,
                out_of_order=watermark is not None and str(doc.date) < watermark,
            )

        monkeypatch.setattr(pipeline, "_process_single_document", fake)

        outcomes = pipeline.extract(
            [
                _note("MRN-1", "2024-05-01", "a-old"),
                _note("MRN-2", "2024-12-01", "b-old"),
                _note("MRN-1", "2024-04-01", "a-older"),
            ]
        )

        assert {o.document_id for o in outcomes if o.out_of_order} == {
            "a-old",
            "b-old",
            "a-older",
        }

    def test_logs_the_out_of_order_documents(
        self, client, httpx_mock, caplog, monkeypatch
    ):
        pipeline = self._seed(client, httpx_mock, num_patients=2)
        mock_patient_exists(httpx_mock, ["pat-1", "pat-2"], repeat=True)
        mock_watermark(httpx_mock, "pat-1", date="2024-06-01T00:00:00Z")
        mock_watermark(httpx_mock, "pat-2", date="2025-01-01T00:00:00Z")
        self._record(pipeline, monkeypatch)

        with caplog.at_level(logging.INFO):
            pipeline.extract(
                [
                    _note("MRN-1", "2024-05-01", "a-old"),
                    _note("MRN-2", "2024-12-01", "b-old"),
                ]
            )

        assert any(
            "2 reverse-chronological document(s) across 2 patient(s)" in r.message
            for r in caplog.records
        )

    def test_same_day_is_allowed(self, client, httpx_mock):
        """Dates are day-resolution, so equal dates are not a violation."""
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_watermark(httpx_mock, "pat-1", date="2024-06-01T00:00:00Z")
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        outcomes = pipeline.extract([_note("MRN-1", "2024-06-01", "same-day")])

        assert outcomes[0].success
        assert outcomes[0].out_of_order is False

    def test_first_documents_for_a_patient_are_allowed(self, client, httpx_mock):
        """No watermark (nothing persisted yet) means nothing to violate."""
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_watermark(httpx_mock, "pat-1", date=None)
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        outcomes = pipeline.extract([_note("MRN-1", "2020-01-01", "first")])

        assert outcomes[0].success

    def test_watermark_failure_still_fails_open(
        self, client, httpx_mock, caplog, monkeypatch
    ):
        """An unreachable watermark must not refuse the document."""
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_watermark(httpx_mock, "pat-1", status_code=500)
        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1)
        mock_persist_response(httpx_mock, created=1)

        with caplog.at_level(logging.WARNING):
            outcomes = pipeline.extract([_note("MRN-1", "2020-01-01", "unknowable")])

        assert outcomes[0].success
        assert any("chronology check skipped" in r.message for r in caplog.records)

    def test_extract_all_extracts_every_batch(self, client, httpx_mock, monkeypatch):
        """The global ascending sort puts the backdated note in batch 1.

        It is extracted there, ahead of the newer ones, so by the time the
        later batches run nothing is out of order at all.
        """
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1", repeat=True)
        mock_watermark(httpx_mock, "pat-1", date="2024-06-01T00:00:00Z")
        seen = self._record(pipeline, monkeypatch)

        docs = [_note("MRN-1", f"2024-1{i}-01", f"ok-{i}") for i in range(1, 3)]
        docs.append(_note("MRN-1", "2024-05-01", "older"))

        outcomes = pipeline.extract_all(docs, batch_size=1)

        assert [d.document_id for d in seen] == ["older", "ok-1", "ok-2"]
        assert {o.document_id for o in outcomes if o.success} == {
            "older",
            "ok-1",
            "ok-2",
        }
        assert pipeline.documents_processed == 3
        assert pipeline.documents_failed == 0

    def test_skipped_documents_are_never_out_of_order(self, client, httpx_mock):
        """An already-processed older document is filtered out before the check."""
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1")
        mock_watermark(httpx_mock, "pat-1", date="2024-06-01T00:00:00Z")
        httpx_mock.add_response(
            method="GET",
            url=(
                "http://localhost:8080/fhir/DocumentReference?patient=pat-1"
                "&identifier=urn%3Acavell%3Adocument%7C&_elements=identifier"
                "&_count=1000"
            ),
            json={
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "DocumentReference",
                            "identifier": [
                                {
                                    "system": "urn:cavell:document",
                                    "value": "already-done",
                                }
                            ],
                        }
                    }
                ],
            },
            repeat=True,
            # _seed() registers a repeating empty resume response for this URL;
            # without replace=True that one always wins.
            replace=True,
        )

        outcomes = pipeline.extract([_note("MRN-1", "2024-05-01", "already-done")])

        assert outcomes == []


class TestContinueOnFailure(_PipelineHarness):
    """One document's failure no longer costs the patient's remaining documents.

    A failed document persists nothing, so the record its followers extract
    against stays consistent; re-ingesting it later takes the split-context
    path. Cascade-skipping the rest of the timeline predates that path.
    """

    @staticmethod
    def _record(pipeline, monkeypatch, failed_ids=(), transient_ids=()):
        """Replace real extraction with a recorder. Returns (doc_id, watermark)."""
        seen: list[tuple[str, str | None]] = []

        def fake(
            doc, doc_index, organization_identifier, watermark=None, on_fatal=None
        ):
            seen.append((doc.document_id, watermark))
            ok = doc.document_id not in failed_ids and doc.document_id not in (
                transient_ids
            )
            return IngestionOutcome(
                success=ok,
                patient_identifier=doc.patient_identifier,
                document_index=doc_index,
                document_id=doc.document_id,
                error=None if ok else "failed",
                transient=doc.document_id in transient_ids,
            )

        monkeypatch.setattr(pipeline, "_process_single_document", fake)
        return seen

    def test_deterministic_failure_continues_with_remaining(
        self, client, httpx_mock, monkeypatch
    ):
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1", repeat=True)
        mock_watermark(httpx_mock, "pat-1", date=None)
        seen = self._record(pipeline, monkeypatch, failed_ids={"bad"})

        outcomes = pipeline.extract(
            [
                _note("MRN-1", "2024-01-01", "ok-1"),
                _note("MRN-1", "2024-02-01", "bad"),
                _note("MRN-1", "2024-03-01", "ok-2"),
            ]
        )

        # Every document was attempted; nothing re-ran (deterministic failures
        # are not retried — re-running reproduces them).
        assert [d for d, _ in seen] == ["ok-1", "bad", "ok-2"]
        by_id = {o.document_id: o for o in outcomes}
        assert by_id["ok-1"].success and by_id["ok-2"].success
        assert not by_id["bad"].success
        assert pipeline.documents_processed == 2
        assert pipeline.documents_failed == 1

    def test_deferred_pass_reruns_only_transient_failures(
        self, client, httpx_mock, monkeypatch
    ):
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1", repeat=True)
        mock_watermark(httpx_mock, "pat-1", date=None)
        seen = self._record(
            pipeline, monkeypatch, failed_ids={"bad"}, transient_ids={"blip"}
        )

        pipeline.extract(
            [
                _note("MRN-1", "2024-01-01", "bad"),
                _note("MRN-1", "2024-02-01", "blip"),
                _note("MRN-1", "2024-03-01", "ok"),
            ]
        )

        # Main pass attempts everything; the deferred pass re-runs the
        # transient failure alone — not the deterministic one.
        assert [d for d, _ in seen] == ["bad", "blip", "ok", "blip"]

    def test_deferred_rerun_sees_the_advanced_watermark(
        self, client, httpx_mock, monkeypatch
    ):
        """A re-run older than what persisted after it must route as out-of-order.

        The main pass publishes each patient's advanced watermark, so the
        deferred pass hands _process_single_document a watermark newer than the
        re-run document — which is what routes it onto the split-context path.
        """
        pipeline = self._seed(client, httpx_mock)
        mock_patient_exists(httpx_mock, "pat-1", repeat=True)
        mock_watermark(httpx_mock, "pat-1", date=None)
        seen = self._record(pipeline, monkeypatch, transient_ids={"older"})

        pipeline.extract(
            [
                _note("MRN-1", "2024-01-01", "older"),
                _note("MRN-1", "2024-02-01", "newer"),
            ]
        )

        assert [d for d, _ in seen] == ["older", "newer", "older"]
        # Main pass: no watermark yet. Deferred re-run: the follower persisted.
        assert seen[0][1] is None
        assert seen[2][1] == "2024-02-01"


class TestPartialExtractionPassthrough:
    """extraction_status / failed_extractors reach ExtractResult.

    A partial extraction still returns a usable bundle — it is simply missing
    whatever the failed extractors would have found. Callers (and the quality
    benchmark) need to tell that apart from a note that genuinely had nothing
    to extract, so the signal must survive the trip through the pipeline.
    """

    def _run(self, client, httpx_mock, **extract_kwargs):
        mock_fhir_auth(httpx_mock)
        mock_empty_document_identifiers(httpx_mock)
        mock_seed_response(
            httpx_mock, [("201 Created", "Organization/org-1/_history/1")]
        )
        mock_seed_response(httpx_mock, [("201 Created", "Patient/pat-1/_history/1")])
        mock_patient_exists(httpx_mock, "pat-1")

        pipeline = IngestionPipeline(client, default_organization="ORG-1")
        pipeline.seed(
            organizations=[Organization(identifier="ORG-1", name="Org")],
            patients=[Patient(identifier="MRN-1")],
        )

        mock_context_empty(httpx_mock, "pat-1")
        mock_extract_response(httpx_mock, count=1, **extract_kwargs)
        mock_persist_response(httpx_mock, created=1)

        outcomes = pipeline.extract(
            [
                Document(
                    text="test note text here",
                    patient_identifier="MRN-1",
                    date="2024-01-01",
                    document_id="d1",
                )
            ]
        )
        assert len(outcomes) == 1
        assert outcomes[0].extract_result is not None
        return outcomes[0].extract_result

    def test_partial_status_and_failed_extractors(self, client, httpx_mock):
        result = self._run(
            client,
            httpx_mock,
            extraction_status="partial",
            failed_extractors=["medications", "procedures"],
        )

        assert result.extraction_status == "partial"
        assert result.failed_extractors == ["medications", "procedures"]
        assert result.is_partial

    def test_complete_status(self, client, httpx_mock):
        result = self._run(
            client, httpx_mock, extraction_status="complete", failed_extractors=[]
        )

        assert result.extraction_status == "complete"
        assert result.failed_extractors == []
        assert not result.is_partial

    def test_absent_fields_default_safely(self, client, httpx_mock):
        """An older API that omits both fields must not break ingestion."""
        result = self._run(client, httpx_mock)

        assert result.extraction_status is None
        assert result.failed_extractors == []
        assert not result.is_partial

    def test_usage_breakdown_survives(self, client, httpx_mock):
        """The nested per-agent cost attribution is preserved end to end."""
        result = self._run(client, httpx_mock, estimated_cost=0.01)

        assert result.usage is not None
        assert result.usage.estimated_cost == pytest.approx(0.01)
        # this mock carries no breakdown; the field exists and is None
        assert result.usage.breakdown is None
