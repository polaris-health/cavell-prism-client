"""Tests for local FHIR client."""

import datetime
import json
import logging
from urllib.parse import quote

import httpx
import pytest

from cavell_client.fhir import (
    _PATIENT_SEARCH_PARAM,
    CONTEXT_RESOURCE_TYPES,
    DOCUMENT_IDENTIFIER_SYSTEM,
    IDENTIFIER_SYSTEM,
    MAX_CONTEXT_OBSERVATIONS,
    OBSERVATION_CONTEXT_YEARS,
    ORGANIZATION_IDENTIFIER_SYSTEM,
    PRACTITIONER_IDENTIFIER_SYSTEM,
    FHIRClient,
    _dedupe_entries_by_id,
    _filter_stale_refs,
    _observation_signature,
    _observation_window_start,
)
from cavell_client.models import FHIRAuthError, PatientNotFoundError

# URL-encoded version of IDENTIFIER_SYSTEM for mock URLs
IDENTIFIER_SYSTEM_ENCODED = quote(IDENTIFIER_SYSTEM, safe="")
DOCUMENT_IDENTIFIER_SYSTEM_ENCODED = quote(DOCUMENT_IDENTIFIER_SYSTEM, safe="")


@pytest.fixture
def fhir():
    """Create FHIR client for testing."""
    return FHIRClient(
        base_url="http://localhost:8080",
        client_id="test-client",
        client_secret="test-secret",
        api_path="/fhir",
    )


class TestFHIRClientInit:
    """Test FHIRClient initialization."""

    def test_strips_trailing_slashes(self):
        client = FHIRClient(
            base_url="http://localhost:8080/",
            client_id="c",
            client_secret="s",
            api_path="/fhir/",
        )
        assert client.base_url == "http://localhost:8080"
        assert client.api_path == "/fhir"

    def test_handles_empty_api_path(self):
        client = FHIRClient(
            base_url="http://localhost:8080",
            client_id="c",
            client_secret="s",
            api_path="",
        )
        assert client.api_path == ""

    def test_no_auth_when_both_none(self):
        client = FHIRClient(base_url="http://localhost:8080")
        assert client._no_auth is True

    def test_auth_when_both_provided(self):
        client = FHIRClient(
            base_url="http://localhost:8080",
            client_id="c",
            client_secret="s",
        )
        assert client._no_auth is False

    def test_raises_when_only_client_id(self):
        with pytest.raises(ValueError, match="both"):
            FHIRClient(base_url="http://localhost:8080", client_id="c")

    def test_raises_when_only_client_secret(self):
        with pytest.raises(ValueError, match="both"):
            FHIRClient(base_url="http://localhost:8080", client_secret="s")


class TestFHIRAuth:
    """Test OAuth2 authentication."""

    def test_get_access_token(self, fhir, httpx_mock):
        """Test token acquisition."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "test-token-123"},
        )

        token = fhir._get_access_token()
        assert token == "test-token-123"
        assert fhir._access_token == "test-token-123"

    def test_get_access_token_cached(self, fhir, httpx_mock):
        """Test token caching."""
        fhir._access_token = "cached-token"

        token = fhir._get_access_token()
        assert token == "cached-token"
        # No HTTP request should have been made
        assert len(httpx_mock.get_requests()) == 0

    def test_auth_failure(self, fhir, httpx_mock):
        """Test authentication failure."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            status_code=401,
        )

        with pytest.raises(FHIRAuthError):
            fhir._get_access_token()

    def test_auth_missing_access_token_key(self, fhir, httpx_mock):
        """Token endpoint returns JSON without 'access_token' → FHIRAuthError."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"token_type": "bearer"},  # no access_token key
        )

        with pytest.raises(
            FHIRAuthError, match="Token response missing 'access_token'"
        ):
            fhir._get_access_token()


class TestPatientOperations:
    """Test patient-related operations."""

    def test_get_patient(self, fhir, httpx_mock):
        """Test getting a patient by ID."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient/123",
            json={"resourceType": "Patient", "id": "123"},
        )

        patient = fhir.get_patient("123")
        assert patient["id"] == "123"

    def test_get_patient_not_found(self, fhir, httpx_mock):
        """Test patient not found error."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient/missing",
            status_code=404,
        )

        with pytest.raises(PatientNotFoundError) as exc_info:
            fhir.get_patient("missing")
        assert exc_info.value.patient_id == "missing"

    def test_find_patient_by_identifier(self, fhir, httpx_mock):
        """Test finding patient by identifier."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"http://localhost:8080/fhir/Patient?identifier={IDENTIFIER_SYSTEM_ENCODED}%7CMRN-456",
            json={
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Patient", "id": "found-123"}}],
            },
        )

        result = fhir.find_patient_by_identifier("MRN-456")
        assert result is not None
        patient_id, patient_resource = result
        assert patient_id == "found-123"
        assert patient_resource["resourceType"] == "Patient"

    def test_find_patient_not_found(self, fhir, httpx_mock):
        """Test patient not found by identifier."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"http://localhost:8080/fhir/Patient?identifier={IDENTIFIER_SYSTEM_ENCODED}%7Cunknown",
            json={"resourceType": "Bundle", "entry": []},
        )

        result = fhir.find_patient_by_identifier("unknown")
        assert result is None

    def test_create_patient(self, fhir, httpx_mock):
        """Test patient creation."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/Patient",
            json={"resourceType": "Patient", "id": "new-123"},
        )

        patient_id, patient = fhir.create_patient("MRN-789")
        assert patient_id == "new-123"
        assert patient["id"] == "new-123"

    def test_ensure_patient_with_id(self, fhir, httpx_mock):
        """Test ensure_patient with existing patient_id."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient/existing-123",
            json={"resourceType": "Patient", "id": "existing-123"},
        )

        patient_id, patient = fhir.ensure_patient(
            identifier=None, patient_id="existing-123"
        )
        assert patient_id == "existing-123"

    def test_ensure_patient_creates_new(self, fhir, httpx_mock):
        """Test ensure_patient creates patient when identifier not found."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        # Search returns empty
        httpx_mock.add_response(
            method="GET",
            url=f"http://localhost:8080/fhir/Patient?identifier={IDENTIFIER_SYSTEM_ENCODED}%7Cnew-mrn",
            json={"resourceType": "Bundle", "entry": []},
        )
        # Create patient
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/Patient",
            json={"resourceType": "Patient", "id": "created-456"},
        )

        patient_id, patient = fhir.ensure_patient(identifier="new-mrn", patient_id=None)
        assert patient_id == "created-456"


class TestBundleOperations:
    """Test bundle posting."""

    def test_post_bundle_success(self, fhir, httpx_mock):
        """Test successful bundle posting."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            json={
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [
                    {"response": {"status": "201 Created"}},
                    {"response": {"status": "200 OK"}},
                ],
            },
        )

        entries = [
            {
                "resource": {"resourceType": "Condition"},
                "request": {"method": "POST", "url": "Condition"},
            },
            {
                "resource": {"resourceType": "MedicationRequest"},
                "request": {"method": "PUT", "url": "MedicationRequest/123"},
            },
        ]

        result = fhir.post_bundle(entries)
        assert result.status == "success"
        assert result.created == 1
        assert result.updated == 1
        assert result.errors == []

    def test_post_bundle_empty(self, fhir):
        """Test posting empty bundle."""
        result = fhir.post_bundle([])
        assert result.status == "success"
        assert result.created == 0
        assert result.updated == 0

    def test_post_bundle_failure(self, fhir, httpx_mock):
        """Test bundle posting failure."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            status_code=400,
            json={
                "resourceType": "OperationOutcome",
                "issue": [{"diagnostics": "Invalid bundle"}],
            },
        )

        entries = [{"resource": {"resourceType": "Condition"}}]
        result = fhir.post_bundle(entries)
        assert result.status == "failed"
        assert result.created == 0
        assert len(result.errors) == 1
        assert "Invalid bundle" in result.errors[0]["error"]

    def test_post_bundle_dedupes_duplicate_logical_ids(self, fhir, httpx_mock, caplog):
        """Two entries colliding on Procedure/5380 collapse to one before POST."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            json={
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [
                    {"response": {"status": "200 OK"}},  # the kept Procedure
                    {"response": {"status": "201 Created"}},  # the Observation
                ],
            },
        )

        entries = [
            {
                "resource": {
                    "resourceType": "Procedure",
                    "id": "5380",
                    "bodySite": [{"text": "left knee"}],
                },
                "request": {"method": "PUT", "url": "Procedure/5380"},
            },
            {
                "resource": {
                    "resourceType": "Procedure",
                    "id": "5380",
                    "bodySite": [{"text": "right knee"}],
                },
                "request": {"method": "PUT", "url": "Procedure/5380"},
            },
            {
                "resource": {"resourceType": "Observation"},
                "request": {"method": "POST", "url": "Observation"},
            },
        ]

        with caplog.at_level(logging.WARNING, logger="cavell_client.fhir"):
            result = fhir.post_bundle(entries)

        # Inspect the bundle actually sent to the server
        posts = [
            r
            for r in httpx_mock.get_requests()
            if str(r.url) == "http://localhost:8080/fhir/"
        ]
        sent = json.loads(posts[0].content)
        procedures = [
            e for e in sent["entry"] if e["resource"]["resourceType"] == "Procedure"
        ]
        assert len(procedures) == 1  # exactly one Procedure/5380
        assert procedures[0]["resource"]["id"] == "5380"
        # keep-first: the first copy (left knee) survives
        assert procedures[0]["resource"]["bodySite"] == [{"text": "left knee"}]
        assert len(sent["entry"]) == 2  # Procedure + Observation

        # A warning naming the dropped id was logged
        assert any("Procedure/5380" in m for m in caplog.messages)

        # zip alignment held: counts reflect the deduped bundle
        assert result.status == "success"
        assert result.updated == 1
        assert result.created == 1

    def test_post_bundle_failure_logging_levels(self, fhir, httpx_mock, caplog):
        """ERROR carries only the transaction message; per-entry dump is DEBUG."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            status_code=400,
            json={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "diagnostics": "HAPI-0535: Transaction bundle contains "
                        "multiple resources with ID: Procedure/5380"
                    }
                ],
            },
        )

        entries = [
            {
                "resource": {"resourceType": "Procedure", "id": "5380"},
                "request": {"method": "PUT", "url": "Procedure/5380"},
            }
        ]

        with caplog.at_level(logging.DEBUG, logger="cavell_client.fhir"):
            result = fhir.post_bundle(entries)

        assert result.status == "failed"
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
        # The single ERROR is the transaction message, surfacing the offending id
        assert any(r.getMessage().startswith("Transaction failed:") for r in errors)
        assert any("Procedure/5380" in r.getMessage() for r in errors)
        # The per-entry dump is NOT at ERROR
        assert not any("Bundle entry" in r.getMessage() for r in errors)
        # The per-entry dump appears at DEBUG
        assert any("Bundle entry 0" in r.getMessage() for r in debugs)

    def test_post_bundle_failure_no_dump_when_debug_off(self, fhir, httpx_mock, caplog):
        """With DEBUG off, the per-entry bundle dump is skipped entirely."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            status_code=400,
            json={
                "resourceType": "OperationOutcome",
                "issue": [{"diagnostics": "Invalid bundle"}],
            },
        )

        entries = [{"resource": {"resourceType": "Condition"}}]
        with caplog.at_level(logging.INFO, logger="cavell_client.fhir"):
            fhir.post_bundle(entries)

        assert not any("Bundle entry" in m for m in caplog.messages)


class TestDedupeEntriesById:
    """Test the _dedupe_entries_by_id helper."""

    def test_dedupe_entries_by_id_keep_first(self):
        entries = [
            {
                "resource": {
                    "resourceType": "Procedure",
                    "id": "5380",
                    "bodySite": "L",
                }
            },
            {
                "resource": {
                    "resourceType": "Procedure",
                    "id": "5380",
                    "bodySite": "R",
                }
            },
            {
                "resource": {"resourceType": "Observation"},
                "request": {"method": "POST", "url": "Observation"},  # create kept
            },
            {
                "resource": {"resourceType": "Procedure"},
                "request": {
                    "method": "PUT",
                    "url": "Procedure?identifier=x|y",  # conditional kept
                },
            },
        ]
        deduped, dropped = _dedupe_entries_by_id(entries)
        assert dropped == ["Procedure/5380"]
        assert len(deduped) == 3
        assert deduped[0]["resource"]["bodySite"] == "L"  # keep-first

    def test_dedupe_entries_by_id_no_collisions(self):
        entries = [
            {"resource": {"resourceType": "Procedure", "id": "1"}},
            # same id, different type -> no collision
            {"resource": {"resourceType": "Observation", "id": "1"}},
            {"resource": {"resourceType": "Condition"}},  # no id
        ]
        deduped, dropped = _dedupe_entries_by_id(entries)
        assert dropped == []
        assert len(deduped) == 3

    def test_dedupe_entries_by_id_url_fallback(self):
        """No resource.id but a PUT-with-id url still de-dupes."""
        entries = [
            {
                "resource": {"resourceType": "Procedure"},
                "request": {"method": "PUT", "url": "Procedure/99"},
            },
            {
                "resource": {"resourceType": "Procedure"},
                "request": {"method": "PUT", "url": "Procedure/99"},
            },
        ]
        deduped, dropped = _dedupe_entries_by_id(entries)
        assert dropped == ["Procedure/99"]
        assert len(deduped) == 1


class TestContextFetching:
    """Test patient context fetching."""

    def test_fetch_patient_context(self, fhir, httpx_mock):
        """Test fetching patient context."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        # Condition search — carries server bookkeeping the SDK must strip
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Condition?subject=123&_count=500",
            json={
                "entry": [
                    {
                        "resource": {
                            "resourceType": "Condition",
                            "id": "c1",
                            "meta": {"versionId": "3", "tag": [{"code": "x"}]},
                            "text": {"status": "generated", "div": "<div>…</div>"},
                        }
                    }
                ]
            },
        )
        # AllergyIntolerance search (uses 'patient' param)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/AllergyIntolerance?patient=123&_count=500",
            json={"entry": []},
        )
        # Procedure search
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Procedure?subject=123&_count=500",
            json={"entry": []},
        )
        # MedicationRequest search
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/MedicationRequest?subject=123&_count=500",
            json={
                "entry": [
                    {"resource": {"resourceType": "MedicationRequest", "id": "m1"}}
                ]
            },
        )
        # MedicationAdministration search
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/MedicationAdministration?subject=123&_count=500",
            json={
                "entry": [
                    {
                        "resource": {
                            "resourceType": "MedicationAdministration",
                            "id": "ma1",
                        }
                    }
                ]
            },
        )
        # NutritionOrder search (R4 has no 'subject' param — 'patient')
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/NutritionOrder?patient=123&_count=500",
            json={
                "entry": [{"resource": {"resourceType": "NutritionOrder", "id": "no1"}}]
            },
        )
        # FamilyMemberHistory search (R4 has no 'subject' param — 'patient')
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/FamilyMemberHistory?patient=123&_count=500",
            json={
                "entry": [
                    {"resource": {"resourceType": "FamilyMemberHistory", "id": "f1"}}
                ]
            },
        )
        # Observation search (windowed on the document date, capped, newest-first)
        httpx_mock.add_response(
            method="GET",
            url=(
                "http://localhost:8080/fhir/Observation?subject=123&_count=50"
                "&date=ge2022-06-15&_sort=-date"
            ),
            json={"entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}]},
        )
        # CarePlan search (active plans only)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/CarePlan?subject=123&_count=500&status=active",
            json={
                "entry": [
                    {
                        "resource": {
                            "resourceType": "CarePlan",
                            "id": "cp1",
                            "status": "active",
                            "meta": {"versionId": "2"},
                        }
                    }
                ]
            },
        )
        # ResearchStudy search (not patient-scoped, not status-filtered)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/ResearchStudy?_count=500&_sort=-_lastUpdated",
            json={
                "entry": [
                    {
                        "resource": {
                            "resourceType": "ResearchStudy",
                            "id": "rs1",
                            "status": "completed",
                        }
                    }
                ]
            },
        )

        context = fhir.fetch_patient_context("123", reference_date="2024-06-15")
        assert len(context) == 8
        resource_types = {r["resourceType"] for r in context}
        assert resource_types == {
            "Condition",
            "MedicationRequest",
            "MedicationAdministration",
            "NutritionOrder",
            "FamilyMemberHistory",
            "Observation",
            "CarePlan",
            "ResearchStudy",
        }
        # meta and narrative are stripped from every context resource
        assert all("meta" not in r and "text" not in r for r in context)


ORG_SYSTEM_ENCODED = quote(ORGANIZATION_IDENTIFIER_SYSTEM, safe="")


class TestSeedBundle:
    """Test seed_bundle method."""

    def test_seed_bundle_creates_and_returns_ids(self, fhir, httpx_mock):
        """Test seeding 2 orgs with 201 responses returns PersistResult + id_map."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            json={
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [
                    {
                        "response": {
                            "status": "201 Created",
                            "location": "Organization/org-1/_history/1",
                        },
                    },
                    {
                        "response": {
                            "status": "201 Created",
                            "location": "Organization/org-2/_history/1",
                        },
                    },
                ],
            },
        )

        resources = [
            {
                "resourceType": "Organization",
                "identifier": [
                    {"system": ORGANIZATION_IDENTIFIER_SYSTEM, "value": "CGH-001"}
                ],
                "name": "City General",
            },
            {
                "resourceType": "Organization",
                "identifier": [
                    {"system": ORGANIZATION_IDENTIFIER_SYSTEM, "value": "SMH-002"}
                ],
                "name": "St. Mary's",
            },
        ]

        result, id_map = fhir.seed_bundle(resources)
        assert result.success
        assert result.created == 2
        assert result.updated == 0
        assert id_map[(ORGANIZATION_IDENTIFIER_SYSTEM, "CGH-001")] == "org-1"
        assert id_map[(ORGANIZATION_IDENTIFIER_SYSTEM, "SMH-002")] == "org-2"

    def test_seed_bundle_parses_absolute_location_urls(self, fhir, httpx_mock):
        """Absolute response.location URLs should still produce correct ids."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            json={
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [
                    {
                        "response": {
                            "status": "201 Created",
                            "location": (
                                "https://fhir.example.com/fhir/Organization/org-1/_history/1"
                            ),
                        },
                    },
                ],
            },
        )

        resources = [
            {
                "resourceType": "Organization",
                "identifier": [
                    {"system": ORGANIZATION_IDENTIFIER_SYSTEM, "value": "CGH-001"}
                ],
                "name": "City General",
            },
        ]

        result, id_map = fhir.seed_bundle(resources)
        assert result.success
        assert result.created == 1
        assert id_map[(ORGANIZATION_IDENTIFIER_SYSTEM, "CGH-001")] == "org-1"

    def test_seed_bundle_handles_updates(self, fhir, httpx_mock):
        """Test seeding existing resources returns 200 OK with updated count."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            json={
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [
                    {
                        "response": {
                            "status": "200 OK",
                            "location": "Organization/org-1/_history/2",
                        },
                    },
                ],
            },
        )

        resources = [
            {
                "resourceType": "Organization",
                "identifier": [
                    {"system": ORGANIZATION_IDENTIFIER_SYSTEM, "value": "CGH-001"}
                ],
                "name": "City General Updated",
            },
        ]

        result, id_map = fhir.seed_bundle(resources)
        assert result.success
        assert result.created == 0
        assert result.updated == 1
        assert id_map[(ORGANIZATION_IDENTIFIER_SYSTEM, "CGH-001")] == "org-1"

    def test_seed_bundle_empty_list(self, fhir):
        """Test seeding empty list returns success with empty id_map."""
        result, id_map = fhir.seed_bundle([])
        assert result.success
        assert result.created == 0
        assert result.updated == 0
        assert id_map == {}

    def test_seed_bundle_fhir_error(self, fhir, httpx_mock):
        """Test seeding with FHIR error raises HTTPStatusError."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            status_code=400,
            json={
                "resourceType": "OperationOutcome",
                "issue": [{"diagnostics": "Invalid resource"}],
            },
        )

        resources = [
            {
                "resourceType": "Organization",
                "identifier": [
                    {"system": ORGANIZATION_IDENTIFIER_SYSTEM, "value": "BAD"}
                ],
                "name": "Bad Org",
            },
        ]

        with pytest.raises(httpx.HTTPStatusError):
            fhir.seed_bundle(resources)

    def test_seed_bundle_builds_put_urls(self, fhir, httpx_mock):
        """Test that seed_bundle builds PUT method + identifier query URLs."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            json={
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [
                    {
                        "response": {
                            "status": "201 Created",
                            "location": "Organization/org-1/_history/1",
                        },
                    },
                ],
            },
        )

        resources = [
            {
                "resourceType": "Organization",
                "identifier": [
                    {"system": ORGANIZATION_IDENTIFIER_SYSTEM, "value": "CGH-001"}
                ],
                "name": "City General",
            },
        ]

        fhir.seed_bundle(resources)

        # Find the bundle POST request (skip auth token request)
        requests = httpx_mock.get_requests()
        bundle_request = [
            r for r in requests if r.method == "POST" and str(r.url).endswith("/")
        ][-1]
        body = json.loads(bundle_request.content)

        assert body["type"] == "transaction"
        entry = body["entry"][0]
        assert entry["request"]["method"] == "PUT"
        assert entry["request"]["url"] == (
            f"Organization?identifier={ORGANIZATION_IDENTIFIER_SYSTEM}|CGH-001"
        )

    def test_seed_bundle_fallback_to_resource_id(self, fhir, httpx_mock):
        """Test ID extraction falls back to resource.id when location is missing."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            json={
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [
                    {
                        "response": {"status": "201 Created"},
                        "resource": {"id": "fallback-id"},
                    },
                ],
            },
        )

        resources = [
            {
                "resourceType": "Organization",
                "identifier": [
                    {"system": ORGANIZATION_IDENTIFIER_SYSTEM, "value": "FB-001"}
                ],
                "name": "Fallback Org",
            },
        ]

        result, id_map = fhir.seed_bundle(resources)
        assert result.success
        assert id_map[(ORGANIZATION_IDENTIFIER_SYSTEM, "FB-001")] == "fallback-id"


PRACTITIONER_SYSTEM_ENCODED = quote(PRACTITIONER_IDENTIFIER_SYSTEM, safe="")


class TestSearchPractitioners:
    """Test search_practitioners method."""

    def test_search_by_identifier(self, fhir, httpx_mock):
        """Test searching practitioners by identifier."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"http://localhost:8080/fhir/Practitioner?identifier={PRACTITIONER_SYSTEM_ENCODED}%7CDOC-001",
            json={
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "Practitioner",
                            "id": "prac-1",
                            "name": [{"family": "Smith", "given": ["Jane"]}],
                        }
                    }
                ],
            },
        )

        results = fhir.search_practitioners(identifier="DOC-001")
        assert len(results) == 1
        assert results[0]["id"] == "prac-1"

    def test_search_by_name_and_org(self, fhir, httpx_mock):
        """Test searching practitioners by name and organization."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Practitioner?family=Smith&given=Jane&_has%3APractitionerRole%3Apractitioner%3Aorganization=Organization%2Forg-1",
            json={
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "Practitioner",
                            "id": "prac-1",
                            "name": [{"family": "Smith", "given": ["Jane"]}],
                        }
                    }
                ],
            },
        )

        results = fhir.search_practitioners(
            family_name="Smith",
            given_name="Jane",
            organization_id="org-1",
        )
        assert len(results) == 1
        assert results[0]["id"] == "prac-1"

    def test_search_no_results(self, fhir, httpx_mock):
        """Test searching practitioners with no results."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"http://localhost:8080/fhir/Practitioner?identifier={PRACTITIONER_SYSTEM_ENCODED}%7CUNKNOWN",
            json={"resourceType": "Bundle", "entry": []},
        )

        results = fhir.search_practitioners(identifier="UNKNOWN")
        assert results == []


class TestMakeRequest:
    """Test _make_request behavior."""

    def test_retry_on_401_refreshes_token(self, fhir, httpx_mock):
        """First request 401 → token refresh → retry succeeds."""
        # Initial auth
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token-1"},
        )
        # First GET returns 401
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient/123",
            status_code=401,
        )
        # Re-auth after 401
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token-2"},
        )
        # Retry succeeds
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient/123",
            json={"resourceType": "Patient", "id": "123"},
        )

        patient = fhir.get_patient("123")
        assert patient["id"] == "123"

    def test_second_401_after_refresh_raises(self, fhir, httpx_mock):
        """Both attempts 401 → raises HTTPStatusError."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token-1"},
        )
        # First GET returns 401
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient/123",
            status_code=401,
        )
        # Re-auth
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token-2"},
        )
        # Retry also 401
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient/123",
            status_code=401,
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            fhir.get_patient("123")
        assert exc_info.value.response.status_code == 401

    def test_get_request_includes_cache_control(self, fhir, httpx_mock):
        """GET requests include Cache-Control: no-cache header."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient/123",
            json={"resourceType": "Patient", "id": "123"},
        )

        fhir.get_patient("123")

        requests = httpx_mock.get_requests()
        get_req = [r for r in requests if r.method == "GET"][0]
        assert get_req.headers.get("cache-control") == "no-cache"


class TestPostBundleEdgeCases:
    """Test post_bundle edge cases."""

    def test_post_bundle_partial_failure(self, fhir, httpx_mock):
        """Mixed 201/422 entries → status="partial_failure", correct counts."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            json={
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [
                    {"response": {"status": "201 Created"}},
                    {
                        "response": {"status": "422 Unprocessable Entity"},
                        "resource": {
                            "resourceType": "OperationOutcome",
                            "issue": [{"diagnostics": "Validation failed"}],
                        },
                    },
                ],
            },
        )

        entries = [
            {
                "resource": {"resourceType": "Condition"},
                "request": {"method": "POST", "url": "Condition"},
            },
            {
                "resource": {"resourceType": "MedicationRequest"},
                "request": {"method": "POST", "url": "MedicationRequest"},
            },
        ]

        result = fhir.post_bundle(entries)
        assert result.status == "partial_failure"
        assert result.created == 1
        assert result.updated == 0
        assert len(result.errors) == 1
        assert "Validation failed" in result.errors[0]["error"]


class TestSeedBundleEdgeCases:
    """Test seed_bundle edge cases."""

    def test_seed_bundle_no_location_no_resource_id(self, fhir, httpx_mock):
        """Missing location + resource.id → key omitted from id_map."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            json={
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [
                    {"response": {"status": "201 Created"}},
                ],
            },
        )

        resources = [
            {
                "resourceType": "Organization",
                "identifier": [
                    {"system": ORGANIZATION_IDENTIFIER_SYSTEM, "value": "NO-ID"}
                ],
                "name": "No ID Org",
            },
        ]

        result, id_map = fhir.seed_bundle(resources)
        assert result.success
        assert result.created == 1
        assert (ORGANIZATION_IDENTIFIER_SYSTEM, "NO-ID") not in id_map

    def test_seed_bundle_partial_failure(self, fhir, httpx_mock):
        """One entry succeeds, one fails → partial_failure, correct id_map."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            json={
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [
                    {
                        "response": {
                            "status": "201 Created",
                            "location": "Organization/org-1/_history/1",
                        },
                    },
                    {
                        "response": {
                            "status": "422 Unprocessable Entity",
                            "outcome": {
                                "issue": [{"diagnostics": "Duplicate identifier"}],
                            },
                        },
                    },
                ],
            },
        )

        resources = [
            {
                "resourceType": "Organization",
                "identifier": [
                    {"system": ORGANIZATION_IDENTIFIER_SYSTEM, "value": "GOOD"}
                ],
                "name": "Good Org",
            },
            {
                "resourceType": "Organization",
                "identifier": [
                    {"system": ORGANIZATION_IDENTIFIER_SYSTEM, "value": "BAD"}
                ],
                "name": "Bad Org",
            },
        ]

        result, id_map = fhir.seed_bundle(resources)
        assert result.status == "partial_failure"
        assert result.created == 1
        assert len(result.errors) == 1
        assert (ORGANIZATION_IDENTIFIER_SYSTEM, "GOOD") in id_map
        assert (ORGANIZATION_IDENTIFIER_SYSTEM, "BAD") not in id_map


class TestFetchContextEdgeCases:
    """Test fetch_patient_context edge cases."""

    def test_fetch_context_partial_failure(self, fhir, httpx_mock):
        """One resource type search 500s → returns the others, logs warning."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        # Condition succeeds
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Condition?subject=pat-1&_count=500",
            json={"entry": [{"resource": {"resourceType": "Condition", "id": "c1"}}]},
        )
        # AllergyIntolerance fails with 500
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/AllergyIntolerance?patient=pat-1&_count=500",
            status_code=500,
        )
        # MedicationRequest succeeds
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/MedicationRequest?subject=pat-1&_count=500",
            json={
                "entry": [
                    {"resource": {"resourceType": "MedicationRequest", "id": "m1"}}
                ]
            },
        )
        # Every remaining patient-scoped type succeeds with nothing to report.
        for resource_type in CONTEXT_RESOURCE_TYPES:
            if resource_type in {
                "Condition",
                "AllergyIntolerance",
                "MedicationRequest",
                "Observation",
                "CarePlan",
            }:
                continue
            param = _PATIENT_SEARCH_PARAM.get(resource_type, "subject")
            httpx_mock.add_response(
                method="GET",
                url=(
                    f"http://localhost:8080/fhir/{resource_type}"
                    f"?{param}=pat-1&_count=500"
                ),
                json={"entry": []},
            )
        # Observation succeeds (windowed, capped, newest-first)
        httpx_mock.add_response(
            method="GET",
            url_prefix=(
                "http://localhost:8080/fhir/Observation?subject=pat-1&_count=50&date=ge"
            ),
            json={"entry": []},
        )
        # CarePlan succeeds (empty)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/CarePlan?subject=pat-1&_count=500&status=active",
            json={"entry": []},
        )
        # ResearchStudy succeeds
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/ResearchStudy?_count=500&_sort=-_lastUpdated",
            json={"entry": []},
        )

        context = fhir.fetch_patient_context("pat-1")
        # Should have 2 resources (Condition + MedicationRequest), not crash
        assert len(context) == 2
        resource_types = {r["resourceType"] for r in context}
        assert resource_types == {"Condition", "MedicationRequest"}


class TestObservationWindow:
    """The Observation context window: last N years, then capped by count."""

    def test_window_is_two_years_back_from_the_document(self):
        assert _observation_window_start("2024-06-15") == "2022-06-15"

    def test_accepts_a_date_object(self):
        assert _observation_window_start(datetime.date(2024, 6, 15)) == "2022-06-15"

    def test_accepts_a_datetime(self):
        assert (
            _observation_window_start(datetime.datetime(2024, 6, 15, 9, 30))
            == "2022-06-15"
        )

    def test_accepts_a_full_iso_timestamp(self):
        assert _observation_window_start("2024-06-15T09:30:00Z") == "2022-06-15"

    def test_leap_day_does_not_blow_up(self):
        """29 Feb has no counterpart two years back."""
        assert _observation_window_start("2024-02-29") == "2022-02-28"

    def test_defaults_to_today_without_a_reference(self):
        expected = datetime.date.today().replace(
            year=datetime.date.today().year - OBSERVATION_CONTEXT_YEARS
        )
        assert _observation_window_start(None) == expected.isoformat()

    def test_an_unparseable_date_falls_back_rather_than_raising(self, caplog):
        """An unusable filter must not be able to empty the context."""
        with caplog.at_level(logging.WARNING):
            result = _observation_window_start("not-a-date")

        assert result  # a usable window, not an exception
        assert "Unparseable reference date" in caplog.text

    def test_context_fetch_windows_on_the_document_date(self, fhir, httpx_mock):
        """A 2015 backfill must not query a window two years behind today."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        for resource_type in CONTEXT_RESOURCE_TYPES:
            if resource_type in {"Observation", "CarePlan"}:
                continue
            param = _PATIENT_SEARCH_PARAM.get(resource_type, "subject")
            httpx_mock.add_response(
                method="GET",
                url=(
                    f"http://localhost:8080/fhir/{resource_type}"
                    f"?{param}=pat-1&_count=500"
                ),
                json={"entry": []},
            )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/CarePlan?subject=pat-1&_count=500&status=active",
            json={"entry": []},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/ResearchStudy?_count=500&_sort=-_lastUpdated",
            json={"entry": []},
        )
        # Registered EXACTLY: a today-anchored window would not match this.
        httpx_mock.add_response(
            method="GET",
            url=(
                f"http://localhost:8080/fhir/Observation?subject=pat-1"
                f"&_count={MAX_CONTEXT_OBSERVATIONS}&date=ge2013-04-02&_sort=-date"
            ),
            json={"entry": []},
        )

        fhir.fetch_patient_context("pat-1", reference_date="2015-04-02")

        observation_requests = [
            str(r.url) for r in httpx_mock.get_requests() if "Observation" in str(r.url)
        ]
        assert observation_requests == [
            "http://localhost:8080/fhir/Observation?subject=pat-1"
            f"&_count={MAX_CONTEXT_OBSERVATIONS}&date=ge2013-04-02&_sort=-date"
        ]

    def test_count_cap_still_applies_inside_the_window(self, fhir, httpx_mock):
        """A dense two years is still trimmed to MAX_CONTEXT_OBSERVATIONS."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url_prefix="http://localhost:8080/fhir/Observation?subject=pat-1",
            json={
                "entry": [
                    {"resource": {"resourceType": "Observation", "id": f"o{i}"}}
                    for i in range(200)
                ]
            },
        )

        results = fhir.search_patient_resources(
            "pat-1",
            "Observation",
            params={"date": "ge2022-06-15", "_sort": "-date"},
            max_results=MAX_CONTEXT_OBSERVATIONS,
        )

        assert len(results) == MAX_CONTEXT_OBSERVATIONS


class TestContextResourceTypeCoverage:
    """The context list and its search params must stay in step."""

    def test_r4_types_without_a_subject_param_are_mapped(self):
        """NutritionOrder and FamilyMemberHistory 400 on ``subject`` in R4."""
        for resource_type in ("NutritionOrder", "FamilyMemberHistory"):
            assert resource_type in CONTEXT_RESOURCE_TYPES
            assert _PATIENT_SEARCH_PARAM[resource_type] == "patient"

    def test_types_the_api_reads_are_all_requested(self):
        """Mirrors prism's CONTEXT_SLOTS, minus the non-patient-scoped study."""
        assert set(CONTEXT_RESOURCE_TYPES) == {
            "AllergyIntolerance",
            "CarePlan",
            "Condition",
            "FamilyMemberHistory",
            "MedicationAdministration",
            "MedicationRequest",
            "NutritionOrder",
            "Observation",
            "Procedure",
            "ResearchSubject",
        }

    def test_identity_resources_are_never_context(self):
        """Patient/Organization/Practitioner travel as reference IDs instead."""
        for resource_type in ("Patient", "Organization", "Practitioner", "Encounter"):
            assert resource_type not in CONTEXT_RESOURCE_TYPES


class TestObservationSignature:
    """Test _observation_signature helper."""

    def test_quantity_observation(self):
        obs = {
            "effectiveDateTime": "2023-03-15T10:00:00Z",
            "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
            "valueQuantity": {"value": 120, "unit": "mmHg"},
        }
        sig = _observation_signature(obs)
        assert sig == ("2023-03-15", "http://loinc.org|8480-6", "120|mmHg")

    def test_codeable_concept_observation(self):
        obs = {
            "effectiveDateTime": "2023-03-15",
            "code": {"coding": [{"system": "http://loinc.org", "code": "72166-2"}]},
            "valueCodeableConcept": {"text": "Never smoker"},
        }
        sig = _observation_signature(obs)
        assert sig == ("2023-03-15", "http://loinc.org|72166-2", "Never smoker")

    def test_string_observation(self):
        obs = {
            "effectiveDateTime": "2023-03-15",
            "code": {"coding": [{"system": "http://loinc.org", "code": "8251-1"}]},
            "valueString": "Normal",
        }
        sig = _observation_signature(obs)
        assert sig == ("2023-03-15", "http://loinc.org|8251-1", "Normal")

    def test_code_only_observation(self):
        """Observation with no value (e.g., presence/absence coded in code)."""
        obs = {
            "effectiveDateTime": "2023-03-15",
            "code": {"coding": [{"system": "http://loinc.org", "code": "11557-6"}]},
        }
        sig = _observation_signature(obs)
        assert sig == ("2023-03-15", "http://loinc.org|11557-6", "")

    def test_missing_date_returns_none(self):
        obs = {
            "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
            "valueQuantity": {"value": 120, "unit": "mmHg"},
        }
        assert _observation_signature(obs) is None

    def test_missing_code_returns_none(self):
        obs = {
            "effectiveDateTime": "2023-03-15",
            "valueQuantity": {"value": 120, "unit": "mmHg"},
        }
        assert _observation_signature(obs) is None

    def test_text_code_fallback(self):
        """Falls back to code.text when no codings present."""
        obs = {
            "effectiveDateTime": "2023-03-15",
            "code": {"text": "Blood Pressure"},
            "valueQuantity": {"value": 120, "unit": "mmHg"},
        }
        sig = _observation_signature(obs)
        assert sig == ("2023-03-15", "Blood Pressure", "120|mmHg")


class TestFilterStaleRefs:
    """Test _filter_stale_refs helper."""

    def test_removes_stale_document_reference_related(self):
        entries = [
            {
                "fullUrl": "urn:uuid:obs-1",
                "resource": {"resourceType": "Observation"},
            },
            {
                "fullUrl": "urn:uuid:doc-1",
                "resource": {
                    "resourceType": "DocumentReference",
                    "context": {
                        "related": [
                            {"reference": "urn:uuid:obs-1"},
                            {"reference": "urn:uuid:obs-removed"},
                        ]
                    },
                },
            },
        ]
        result = _filter_stale_refs(entries)
        doc = result[1]["resource"]
        assert doc["context"]["related"] == [{"reference": "urn:uuid:obs-1"}]

    def test_removes_stale_encounter_reason_reference(self):
        entries = [
            {
                "fullUrl": "urn:uuid:obs-1",
                "resource": {"resourceType": "Observation"},
            },
            {
                "fullUrl": "urn:uuid:enc-1",
                "resource": {
                    "resourceType": "Encounter",
                    "reasonReference": [
                        {"reference": "urn:uuid:obs-1"},
                        {"reference": "urn:uuid:obs-removed"},
                    ],
                },
            },
        ]
        result = _filter_stale_refs(entries)
        enc = result[1]["resource"]
        assert enc["reasonReference"] == [{"reference": "urn:uuid:obs-1"}]

    def test_removes_context_when_all_related_stale(self):
        entries = [
            {
                "fullUrl": "urn:uuid:doc-1",
                "resource": {
                    "resourceType": "DocumentReference",
                    "context": {"related": [{"reference": "urn:uuid:obs-gone"}]},
                },
            },
        ]
        result = _filter_stale_refs(entries)
        assert "context" not in result[0]["resource"]

    def test_removes_reason_reference_when_all_stale(self):
        entries = [
            {
                "fullUrl": "urn:uuid:enc-1",
                "resource": {
                    "resourceType": "Encounter",
                    "reasonReference": [{"reference": "urn:uuid:obs-gone"}],
                },
            },
        ]
        result = _filter_stale_refs(entries)
        assert "reasonReference" not in result[0]["resource"]

    def test_preserves_non_urn_uuid_refs(self):
        """References like Patient/123 are not urn:uuid and should be kept."""
        entries = [
            {
                "fullUrl": "urn:uuid:doc-1",
                "resource": {
                    "resourceType": "DocumentReference",
                    "context": {
                        "related": [
                            {"reference": "Patient/123"},
                            {"reference": "urn:uuid:obs-gone"},
                        ]
                    },
                },
            },
        ]
        result = _filter_stale_refs(entries)
        doc = result[0]["resource"]
        assert doc["context"]["related"] == [{"reference": "Patient/123"}]


class TestSearchResearchStudies:
    """Test search_research_studies."""

    @staticmethod
    def _mock_studies(httpx_mock, studies):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/ResearchStudy?_count=500&_sort=-_lastUpdated",
            json={
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "ResearchStudy",
                            "id": study_id,
                            "status": status,
                        }
                    }
                    for study_id, status in studies
                ],
            },
        )

    def test_returns_studies_regardless_of_status(self, fhir, httpx_mock):
        """A study that has left ``active`` is still the study a note names.

        Filtering to ``status=active`` dropped it from the context exactly when
        it moved on, and the extractor — which matches studies by title and
        embedding similarity — re-created it under a fresh id.
        """
        self._mock_studies(
            httpx_mock,
            [
                ("rs-1", "active"),
                ("rs-2", "completed"),
                ("rs-3", "closed-to-accrual"),
            ],
        )

        results = fhir.search_research_studies()
        assert [r["id"] for r in results] == ["rs-1", "rs-2", "rs-3"]

    def test_drops_the_statuses_that_carry_no_signal(self, fhir, httpx_mock):
        self._mock_studies(
            httpx_mock,
            [
                ("rs-1", "active"),
                ("rs-2", "entered-in-error"),
                ("rs-3", "withdrawn"),
            ],
        )

        results = fhir.search_research_studies()
        assert [r["id"] for r in results] == ["rs-1"]

    def test_a_study_without_a_status_is_kept(self, fhir, httpx_mock):
        """Absent status is not an excluded status — keep it rather than guess."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/ResearchStudy?_count=500&_sort=-_lastUpdated",
            json={
                "resourceType": "Bundle",
                "entry": [
                    {"resource": {"resourceType": "ResearchStudy", "id": "rs-1"}}
                ],
            },
        )

        assert [r["id"] for r in fhir.search_research_studies()] == ["rs-1"]

    def test_exclusions_are_overridable(self, fhir, httpx_mock):
        self._mock_studies(
            httpx_mock, [("rs-1", "active"), ("rs-2", "entered-in-error")]
        )

        results = fhir.search_research_studies(exclude_statuses=())
        assert [r["id"] for r in results] == ["rs-1", "rs-2"]

    def test_never_filtered_by_date(self, fhir, httpx_mock):
        """A ResearchStudy carries no date, so any date filter matches nothing.

        R4's ``date`` search param maps to ``ResearchStudy.period``, which the
        extraction API never writes — a date-bounded query would return an
        empty registry and the extractor would re-create every study.
        """
        self._mock_studies(httpx_mock, [("rs-1", "active")])

        fhir.search_research_studies()

        url = str(httpx_mock.get_requests()[-1].url)
        assert "date=" not in url
        assert "_lastUpdated=" not in url

    def test_caps_the_registry(self, fhir, httpx_mock):
        self._mock_studies(httpx_mock, [(f"rs-{i}", "active") for i in range(10)])

        results = fhir.search_research_studies(max_results=4)
        assert [r["id"] for r in results] == ["rs-0", "rs-1", "rs-2", "rs-3"]

    def test_cap_counts_studies_that_survive_the_status_filter(self, fhir, httpx_mock):
        """The cap bounds the payload, not the fetch.

        Counting before the status filter would let a run of withdrawn studies
        eat the budget and deliver fewer than asked for.
        """
        self._mock_studies(
            httpx_mock,
            [("rs-0", "withdrawn"), ("rs-1", "withdrawn"), ("rs-2", "active")]
            + [(f"rs-{i}", "active") for i in range(3, 8)],
        )

        results = fhir.search_research_studies(max_results=3)
        assert [r["id"] for r in results] == ["rs-2", "rs-3", "rs-4"]

    def test_uncapped_when_max_results_is_none(self, fhir, httpx_mock):
        self._mock_studies(httpx_mock, [(f"rs-{i}", "active") for i in range(6)])

        results = fhir.search_research_studies(max_results=None)
        assert len(results) == 6


class TestFetchSplitPatientContext:
    """Context split into what a backdated document could know, and what it could not.

    Assignment is by provenance — the newest already-processed document that
    touched a resource — not by the resource's own clinical dates.
    """

    PATIENT = "pat-1"
    REFERENCE = "2020-06-01"

    def _mock_context(
        self,
        httpx_mock,
        *,
        by_type=None,
        provenance=(),
        past_observations=(),
        studies=(),
    ):
        """Register every request the split fetch makes.

        by_type: {resource_type: [resources]} for the plain patient-scoped
        searches; anything unlisted comes back empty.
        provenance: (document_date, [refs]) pairs for the DocumentReference
        query that dates each resource.
        """
        by_type = by_type or {}
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url=(
                f"http://localhost:8080/fhir/DocumentReference?patient={self.PATIENT}"
                f"&identifier={DOCUMENT_IDENTIFIER_SYSTEM_ENCODED}%7C"
                f"&_elements=date%2Ccontext&_count=1000"
            ),
            json={
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "DocumentReference",
                            "date": date,
                            "context": {
                                "related": [{"reference": ref} for ref in refs]
                            },
                        }
                    }
                    for date, refs in provenance
                ],
            },
            repeat=True,
        )
        for rt in CONTEXT_RESOURCE_TYPES:
            if rt == "Observation":
                continue
            param = _PATIENT_SEARCH_PARAM.get(rt, "subject")
            url = f"http://localhost:8080/fhir/{rt}?{param}={self.PATIENT}&_count=500"
            if rt == "CarePlan":
                url += "&status=active"
            httpx_mock.add_response(
                method="GET",
                url=url,
                json={"entry": [{"resource": r} for r in by_type.get(rt, [])]},
                repeat=True,
            )
        # Past side: window lower bound plus the closing le{reference}.
        httpx_mock.add_response(
            method="GET",
            url_prefix=(
                f"http://localhost:8080/fhir/Observation?subject={self.PATIENT}"
                f"&_count={MAX_CONTEXT_OBSERVATIONS}&date=ge"
            ),
            json={"entry": [{"resource": r} for r in past_observations]},
            repeat=True,
        )
        httpx_mock.add_response(
            method="GET",
            url=(
                "http://localhost:8080/fhir/ResearchStudy?_count=500"
                "&_sort=-_lastUpdated"
            ),
            json={"entry": [{"resource": r} for r in studies]},
            repeat=True,
        )

    def _split(self, fhir):
        return fhir.fetch_split_patient_context(
            self.PATIENT, reference_date=self.REFERENCE
        )

    def test_splits_on_the_newest_touching_document(self, fhir, httpx_mock):
        """Last-touch, not first-touch.

        A condition first recorded in 2019 but edited by a 2025 note carries
        that later edit in its body — clinicalStatus, stage, whatever the
        newer note changed — so handing it back as history the 2020 author
        could have seen would leak exactly what the split exists to withhold.
        """
        self._mock_context(
            httpx_mock,
            by_type={
                "Condition": [
                    {"resourceType": "Condition", "id": "old"},
                    {"resourceType": "Condition", "id": "edited"},
                ]
            },
            provenance=[
                ("2019-01-01", ["Condition/old"]),
                ("2019-01-01", ["Condition/edited"]),
                ("2025-03-02", ["Condition/edited"]),
            ],
        )

        past, future = self._split(fhir)

        assert [r["id"] for r in past] == ["old"]
        assert [r["id"] for r in future] == ["edited"]

    def test_same_day_provenance_counts_as_past(self, fhir, httpx_mock):
        """Dates are day-resolution, so equal dates have no defined order."""
        self._mock_context(
            httpx_mock,
            by_type={"Condition": [{"resourceType": "Condition", "id": "c-1"}]},
            provenance=[(self.REFERENCE, ["Condition/c-1"])],
        )

        past, future = self._split(fhir)

        assert [r["id"] for r in past] == ["c-1"]
        assert future == []

    def test_unknown_provenance_is_past(self, fhir, httpx_mock):
        """Resources loaded outside the pipeline still reach the extractor."""
        self._mock_context(
            httpx_mock,
            by_type={"Condition": [{"resourceType": "Condition", "id": "seeded"}]},
            provenance=[],
        )

        past, future = self._split(fhir)

        assert [r["id"] for r in past] == ["seeded"]
        assert future == []

    def test_observation_query_is_closed_at_the_reference_date(self, fhir, httpx_mock):
        """The regression this split exists for.

        A single ``ge{window_start}`` search sorted newest-first spends its
        whole 50-result cap on the newest observations. For a backdated
        document those are all *after* it, so without the closing ``le`` bound
        the past side would come back empty.
        """
        self._mock_context(
            httpx_mock,
            past_observations=[{"resourceType": "Observation", "id": "before"}],
            provenance=[("2019-05-01", ["Observation/before"])],
        )

        past, future = self._split(fhir)

        assert [r["id"] for r in past] == ["before"]
        assert future == []

        (url,) = [
            str(r.url) for r in httpx_mock.get_requests() if "Observation" in str(r.url)
        ]
        assert f"date=le{self.REFERENCE}" in url
        assert "date=ge" in url
        assert "_sort=-date" in url

    def test_no_forward_observation_query_is_issued(self, fhir, httpx_mock):
        """Observations dated after the document can never match its proposals.

        The extraction API matches on an exact ``(date, code, value)``, and a
        document only reports results at or before its own date — so a forward
        query would add up to 50 resources to every payload that nothing could
        ever use.
        """
        self._mock_context(
            httpx_mock,
            past_observations=[{"resourceType": "Observation", "id": "before"}],
            provenance=[("2019-05-01", ["Observation/before"])],
        )

        self._split(fhir)

        urls = [
            str(r.url) for r in httpx_mock.get_requests() if "Observation" in str(r.url)
        ]
        assert len(urls) == 1, f"expected one Observation query, got {urls}"
        assert "date=gt" not in urls[0]

    def test_observation_recorded_later_still_lands_in_future(self, fhir, httpx_mock):
        """The case the forward query was never needed for.

        A result measured before the document but only written into the record
        by a later note comes back from the closed query, and provenance — not
        its clinical date — puts it on the future side.
        """
        self._mock_context(
            httpx_mock,
            past_observations=[
                {"resourceType": "Observation", "id": "measured-then-recorded-later"}
            ],
            provenance=[
                ("2025-05-01", ["Observation/measured-then-recorded-later"]),
            ],
        )

        past, future = self._split(fhir)

        assert past == []
        assert [r["id"] for r in future] == ["measured-then-recorded-later"]

    def test_studies_are_always_past(self, fhir, httpx_mock):
        """Studies are exempt: undatable, and duplicating one is worse."""
        self._mock_context(
            httpx_mock,
            studies=[{"resourceType": "ResearchStudy", "id": "rs-1"}],
            provenance=[("2025-01-01", ["ResearchStudy/rs-1"])],
        )

        past, future = self._split(fhir)

        assert [r["id"] for r in past] == ["rs-1"]
        assert future == []

    def test_strips_meta_and_text_from_both_sides(self, fhir, httpx_mock):
        self._mock_context(
            httpx_mock,
            by_type={
                "Condition": [
                    {
                        "resourceType": "Condition",
                        "id": "old",
                        "meta": {"versionId": "2"},
                        "text": {"status": "generated", "div": "<div>…</div>"},
                    },
                    {
                        "resourceType": "Condition",
                        "id": "new",
                        "meta": {"versionId": "5"},
                        "text": {"status": "generated", "div": "<div>…</div>"},
                    },
                ]
            },
            provenance=[
                ("2019-01-01", ["Condition/old"]),
                ("2025-01-01", ["Condition/new"]),
            ],
        )

        past, future = self._split(fhir)

        for resource in (*past, *future):
            assert "meta" not in resource
            assert "text" not in resource

    def test_a_failing_type_does_not_lose_the_others(self, fhir, httpx_mock):
        self._mock_context(
            httpx_mock,
            by_type={"Condition": [{"resourceType": "Condition", "id": "c-1"}]},
            provenance=[("2019-01-01", ["Condition/c-1"])],
        )
        httpx_mock.add_response(
            method="GET",
            url=f"http://localhost:8080/fhir/Procedure?subject={self.PATIENT}&_count=500",
            status_code=500,
            json={},
            replace=True,
            repeat=True,
        )

        past, future = self._split(fhir)

        assert [r["id"] for r in past] == ["c-1"]
        assert future == []

    def test_provenance_failure_leaves_everything_past(self, fhir, httpx_mock, caplog):
        """Fail open: unsplit context is the behaviour this replaced."""
        self._mock_context(
            httpx_mock,
            by_type={"Condition": [{"resourceType": "Condition", "id": "c-1"}]},
        )
        httpx_mock.add_response(
            method="GET",
            url=(
                f"http://localhost:8080/fhir/DocumentReference?patient={self.PATIENT}"
                f"&identifier={DOCUMENT_IDENTIFIER_SYSTEM_ENCODED}%7C"
                f"&_elements=date%2Ccontext&_count=1000"
            ),
            status_code=500,
            json={},
            replace=True,
            repeat=True,
        )

        with caplog.at_level(logging.WARNING):
            past, future = self._split(fhir)

        assert [r["id"] for r in past] == ["c-1"]
        assert future == []
        assert any("context will not be split" in r.message for r in caplog.records)


class TestSearchPatientResourcesWithParams:
    """Test search_patient_resources with extra params."""

    def test_passes_extra_params(self, fhir, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Observation?subject=pat-1&_count=500&date=2023-03-15",
            json={
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Observation", "id": "obs-1"}}],
            },
        )

        results = fhir.search_patient_resources(
            "pat-1", "Observation", params={"date": "2023-03-15"}
        )
        assert len(results) == 1
        assert results[0]["id"] == "obs-1"

    def test_research_subject_uses_individual_param(self, fhir, httpx_mock):
        """ResearchSubject is patient-scoped via ``individual``, not ``subject``
        (HAPI rejects ``subject`` for ResearchSubject)."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/ResearchSubject?individual=pat-1&_count=500",
            json={
                "resourceType": "Bundle",
                "entry": [
                    {"resource": {"resourceType": "ResearchSubject", "id": "rsub-1"}}
                ],
            },
        )

        results = fhir.search_patient_resources("pat-1", "ResearchSubject")
        assert [r["id"] for r in results] == ["rsub-1"]


class TestSearchPatientResourcesPagination:
    """Test that search_patient_resources follows pagination links."""

    def test_paginates_across_pages(self, fhir, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Condition?subject=pat-1&_count=500",
            json={
                "resourceType": "Bundle",
                "entry": [
                    {"resource": {"resourceType": "Condition", "id": "c-1"}},
                ],
                "link": [
                    {
                        "relation": "next",
                        "url": "http://localhost:8080/fhir?_getpages=page2",
                    },
                ],
            },
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir?_getpages=page2",
            json={
                "resourceType": "Bundle",
                "entry": [
                    {"resource": {"resourceType": "Condition", "id": "c-2"}},
                ],
            },
        )

        results = fhir.search_patient_resources("pat-1", "Condition")
        assert len(results) == 2
        assert results[0]["id"] == "c-1"
        assert results[1]["id"] == "c-2"

    def test_max_results_stops_pagination(self, fhir, httpx_mock):
        """max_results caps the list and avoids fetching further pages."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        # First page returns the full cap (2) plus a next link. The next page
        # is intentionally NOT mocked — requesting it would 404 and raise, so
        # this test only passes if pagination stops at max_results.
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Observation?subject=pat-1&_count=2",
            json={
                "resourceType": "Bundle",
                "entry": [
                    {"resource": {"resourceType": "Observation", "id": "o-1"}},
                    {"resource": {"resourceType": "Observation", "id": "o-2"}},
                ],
                "link": [
                    {
                        "relation": "next",
                        "url": "http://localhost:8080/fhir?_getpages=page2",
                    },
                ],
            },
        )

        results = fhir.search_patient_resources("pat-1", "Observation", max_results=2)
        assert [r["id"] for r in results] == ["o-1", "o-2"]


class TestGetPatientEverything:
    """Test FHIRClient.get_patient_everything."""

    def test_single_page(self, fhir, httpx_mock):
        """Single-page $everything returns all non-Patient resources."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient/pat-1/$everything",
            json={
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [
                    {"resource": {"resourceType": "Patient", "id": "pat-1"}},
                    {"resource": {"resourceType": "Condition", "id": "c1"}},
                    {"resource": {"resourceType": "Observation", "id": "o1"}},
                ],
            },
        )

        result = fhir.get_patient_everything("pat-1")
        assert len(result) == 2
        assert all(r["resourceType"] != "Patient" for r in result)

    def test_paginates_across_pages(self, fhir, httpx_mock):
        """Follows next links to collect all resources across pages."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        # Page 1
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient/pat-1/$everything",
            json={
                "resourceType": "Bundle",
                "type": "searchset",
                "link": [
                    {
                        "relation": "self",
                        "url": "http://localhost:8080/fhir/Patient/pat-1/$everything",
                    },
                    {
                        "relation": "next",
                        "url": "http://localhost:8080/fhir?_getpages=abc&_getpagesoffset=2&_count=2",
                    },
                ],
                "entry": [
                    {"resource": {"resourceType": "Patient", "id": "pat-1"}},
                    {"resource": {"resourceType": "Condition", "id": "c1"}},
                ],
            },
        )
        # Page 2
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir?_getpages=abc&_getpagesoffset=2&_count=2",
            json={
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [
                    {"resource": {"resourceType": "Observation", "id": "o1"}},
                    {"resource": {"resourceType": "MedicationRequest", "id": "m1"}},
                ],
            },
        )

        result = fhir.get_patient_everything("pat-1")
        types = [r["resourceType"] for r in result]
        assert types == ["Condition", "Observation", "MedicationRequest"]

    def test_empty_bundle(self, fhir, httpx_mock):
        """Empty $everything returns empty list."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient/pat-1/$everything",
            json={"resourceType": "Bundle", "type": "searchset"},
        )

        result = fhir.get_patient_everything("pat-1")
        assert result == []


class TestDeletePatientResources:
    """Test FHIRClient.delete_patient_resources."""

    def test_sends_cascade_delete(self, fhir, httpx_mock):
        """Sends DELETE with _cascade=delete param."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="DELETE",
            url="http://localhost:8080/fhir/Patient/pat-1?_cascade=delete",
            status_code=200,
        )

        fhir.delete_patient_resources("pat-1")

        requests = httpx_mock.get_requests()
        delete_req = [r for r in requests if r.method == "DELETE"][0]
        assert "_cascade=delete" in str(delete_req.url)


class TestDeduplicateObservations:
    """Test FHIRClient.deduplicate_observations."""

    def test_removes_duplicate_observations(self, fhir, httpx_mock):
        """Observations matching existing FHIR data are filtered out."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        # FHIR query returns existing BP observation
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Observation?subject=pat-1&_count=500&date=2023-03-15",
            json={
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "Observation",
                            "id": "srv-obs-1",
                            "effectiveDateTime": "2023-03-15T10:00:00Z",
                            "code": {
                                "coding": [
                                    {"system": "http://loinc.org", "code": "8480-6"}
                                ]
                            },
                            "valueQuantity": {"value": 120, "unit": "mmHg"},
                        }
                    }
                ],
            },
        )

        entries = [
            {
                "fullUrl": "urn:uuid:cond-1",
                "resource": {"resourceType": "Condition", "id": "c1"},
                "request": {"method": "POST", "url": "Condition"},
            },
            {
                "fullUrl": "urn:uuid:obs-dup",
                "resource": {
                    "resourceType": "Observation",
                    "effectiveDateTime": "2023-03-15T10:00:00Z",
                    "code": {
                        "coding": [{"system": "http://loinc.org", "code": "8480-6"}]
                    },
                    "valueQuantity": {"value": 120, "unit": "mmHg"},
                },
                "request": {"method": "POST", "url": "Observation"},
            },
            {
                "fullUrl": "urn:uuid:obs-new",
                "resource": {
                    "resourceType": "Observation",
                    "effectiveDateTime": "2023-03-15T10:00:00Z",
                    "code": {
                        "coding": [{"system": "http://loinc.org", "code": "8462-4"}]
                    },
                    "valueQuantity": {"value": 80, "unit": "mmHg"},
                },
                "request": {"method": "POST", "url": "Observation"},
            },
        ]

        result = fhir.deduplicate_observations(entries, "pat-1")
        resource_types = [e["resource"]["resourceType"] for e in result]
        assert "Condition" in resource_types
        # Duplicate BP removed, new diastolic kept
        obs_codes = [
            e["resource"]["code"]["coding"][0]["code"]
            for e in result
            if e["resource"]["resourceType"] == "Observation"
        ]
        assert obs_codes == ["8462-4"]

    def test_keeps_all_when_no_existing(self, fhir, httpx_mock):
        """No existing observations → all entries kept."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Observation?subject=pat-1&_count=500&date=2023-03-15",
            json={"resourceType": "Bundle", "entry": []},
        )

        entries = [
            {
                "fullUrl": "urn:uuid:obs-1",
                "resource": {
                    "resourceType": "Observation",
                    "effectiveDateTime": "2023-03-15",
                    "code": {
                        "coding": [{"system": "http://loinc.org", "code": "8480-6"}]
                    },
                    "valueQuantity": {"value": 120, "unit": "mmHg"},
                },
                "request": {"method": "POST", "url": "Observation"},
            },
        ]

        result = fhir.deduplicate_observations(entries, "pat-1")
        assert len(result) == 1

    def test_no_observations_returns_unchanged(self, fhir):
        """Bundle with no observations is returned as-is."""
        entries = [
            {
                "fullUrl": "urn:uuid:cond-1",
                "resource": {"resourceType": "Condition"},
                "request": {"method": "POST", "url": "Condition"},
            },
        ]
        result = fhir.deduplicate_observations(entries, "pat-1")
        assert result == entries

    def _mock_existing(self, httpx_mock, resources, date="2023-03-15"):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url=(
                "http://localhost:8080/fhir/Observation"
                f"?subject=pat-1&_count=500&date={date}"
            ),
            json={
                "resourceType": "Bundle",
                "entry": [{"resource": r} for r in resources],
            },
        )

    def test_repoints_refs_to_the_server_survivor(self, fhir, httpx_mock):
        """Refs to removed observations land on the existing server resource.

        Removing them (the old behaviour) kept the bundle valid but lost the
        link; the survivor is known, so referrers keep their provenance.
        """
        self._mock_existing(
            httpx_mock,
            [
                {
                    "resourceType": "Observation",
                    "id": "srv-obs-1",
                    "effectiveDateTime": "2023-03-15",
                    "code": {
                        "coding": [{"system": "http://loinc.org", "code": "8480-6"}]
                    },
                    "valueQuantity": {"value": 120, "unit": "mmHg"},
                }
            ],
        )

        entries = [
            {
                "fullUrl": "urn:uuid:obs-dup",
                "resource": {
                    "resourceType": "Observation",
                    "effectiveDateTime": "2023-03-15",
                    "code": {
                        "coding": [{"system": "http://loinc.org", "code": "8480-6"}]
                    },
                    "valueQuantity": {"value": 120, "unit": "mmHg"},
                },
                "request": {"method": "POST", "url": "Observation"},
            },
            {
                "fullUrl": "urn:uuid:doc-1",
                "resource": {
                    "resourceType": "DocumentReference",
                    "context": {"related": [{"reference": "urn:uuid:obs-dup"}]},
                },
                "request": {"method": "POST", "url": "DocumentReference"},
            },
        ]

        result = fhir.deduplicate_observations(entries, "pat-1")
        doc = [
            e for e in result if e["resource"]["resourceType"] == "DocumentReference"
        ][0]
        assert doc["resource"]["context"]["related"] == [
            {"reference": "Observation/srv-obs-1"}
        ]

    def test_repoints_has_member_of_a_stage_group(self, fhir, httpx_mock):
        """A TNM stage group keeps a valid hasMember when its member is deduped.

        Regression: the member entry was dropped while the group still carried
        ``hasMember: urn:uuid:{...}-t`` — HAPI rejected the whole transaction
        (HAPI-0541 'Unable to satisfy placeholder ID ... hasMember').
        """
        self._mock_existing(
            httpx_mock,
            [
                {
                    "resourceType": "Observation",
                    "id": "srv-t-1",
                    "effectiveDateTime": "2023-03-15",
                    "code": {
                        "coding": [{"system": "http://loinc.org", "code": "21905-5"}]
                    },
                    "valueCodeableConcept": {"text": "cT3"},
                }
            ],
        )

        member = {
            "fullUrl": "urn:uuid:staging-1-t",
            "resource": {
                "resourceType": "Observation",
                "effectiveDateTime": "2023-03-15",
                "code": {"coding": [{"system": "http://loinc.org", "code": "21905-5"}]},
                "valueCodeableConcept": {"text": "cT3"},
            },
            "request": {"method": "POST", "url": "Observation"},
        }
        group = {
            "fullUrl": "urn:uuid:staging-1",
            "resource": {
                "resourceType": "Observation",
                "effectiveDateTime": "2023-03-15",
                "code": {"coding": [{"system": "http://loinc.org", "code": "21908-9"}]},
                "valueCodeableConcept": {"text": "IIIA"},
                "hasMember": [{"reference": "urn:uuid:staging-1-t"}],
            },
            "request": {"method": "POST", "url": "Observation"},
        }

        result = fhir.deduplicate_observations([member, group], "pat-1")

        full_urls = [e.get("fullUrl") for e in result]
        assert full_urls == ["urn:uuid:staging-1"]  # member deduped, group kept
        assert result[0]["resource"]["hasMember"] == [
            {"reference": "Observation/srv-t-1"}
        ]

    def test_keeps_duplicate_when_existing_match_has_no_id(self, fhir, httpx_mock):
        """An id-less existing match cannot be referenced, so nothing is dropped."""
        self._mock_existing(
            httpx_mock,
            [
                {
                    "resourceType": "Observation",
                    "effectiveDateTime": "2023-03-15",
                    "code": {
                        "coding": [{"system": "http://loinc.org", "code": "8480-6"}]
                    },
                    "valueQuantity": {"value": 120, "unit": "mmHg"},
                }
            ],
        )

        entries = [
            {
                "fullUrl": "urn:uuid:obs-dup",
                "resource": {
                    "resourceType": "Observation",
                    "effectiveDateTime": "2023-03-15",
                    "code": {
                        "coding": [{"system": "http://loinc.org", "code": "8480-6"}]
                    },
                    "valueQuantity": {"value": 120, "unit": "mmHg"},
                },
                "request": {"method": "POST", "url": "Observation"},
            },
        ]

        result = fhir.deduplicate_observations(entries, "pat-1")
        assert len(result) == 1


def _docref_with_identifier(system: str, value: str) -> dict:
    """Build a DocumentReference entry with a single identifier."""
    return {
        "resource": {
            "resourceType": "DocumentReference",
            "identifier": [{"system": system, "value": value}],
        }
    }


class TestListDocumentIdentifiers:
    """Test FHIRClient.list_document_identifiers."""

    DOCREF_URL = f"http://localhost:8080/fhir/DocumentReference?identifier={DOCUMENT_IDENTIFIER_SYSTEM_ENCODED}%7C&_elements=identifier&_count=1000"

    def test_basic(self, fhir, httpx_mock):
        """Single page with 2 DocumentReferences with different document_ids."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url=self.DOCREF_URL,
            json={
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [
                    _docref_with_identifier(DOCUMENT_IDENTIFIER_SYSTEM, "doc-1"),
                    _docref_with_identifier(DOCUMENT_IDENTIFIER_SYSTEM, "doc-2"),
                ],
            },
        )

        result = fhir.list_document_identifiers()
        assert result == {"doc-1", "doc-2"}

    def test_pagination(self, fhir, httpx_mock):
        """Follows next link to collect identifiers from multiple pages."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url=self.DOCREF_URL,
            json={
                "resourceType": "Bundle",
                "type": "searchset",
                "link": [
                    {
                        "relation": "next",
                        "url": "http://localhost:8080/fhir?_getpages=abc&_getpagesoffset=1000&_count=1000",
                    }
                ],
                "entry": [_docref_with_identifier(DOCUMENT_IDENTIFIER_SYSTEM, "doc-1")],
            },
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir?_getpages=abc&_getpagesoffset=1000&_count=1000",
            json={
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [_docref_with_identifier(DOCUMENT_IDENTIFIER_SYSTEM, "doc-2")],
            },
        )

        result = fhir.list_document_identifiers()
        assert result == {"doc-1", "doc-2"}

    def test_empty_server(self, fhir, httpx_mock):
        """Bundle with no entries returns empty set."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url=self.DOCREF_URL,
            json={"resourceType": "Bundle", "type": "searchset"},
        )

        result = fhir.list_document_identifiers()
        assert result == set()

    def test_ignores_non_cavell_identifiers(self, fhir, httpx_mock):
        """Identifiers from other systems are filtered out."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url=self.DOCREF_URL,
            json={
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [
                    _docref_with_identifier(DOCUMENT_IDENTIFIER_SYSTEM, "doc-1"),
                    _docref_with_identifier("http://other-system.org", "ignored-doc"),
                ],
            },
        )

        result = fhir.list_document_identifiers()
        assert result == {"doc-1"}

    def test_patient_scoped(self, fhir, httpx_mock):
        """patient= adds the patient search param to the query."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="GET",
            url=(
                f"http://localhost:8080/fhir/DocumentReference?patient=pat-1"
                f"&identifier={DOCUMENT_IDENTIFIER_SYSTEM_ENCODED}%7C"
                f"&_elements=identifier&_count=1000"
            ),
            json={
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [_docref_with_identifier(DOCUMENT_IDENTIFIER_SYSTEM, "doc-1")],
            },
        )

        result = fhir.list_document_identifiers(patient="pat-1")
        assert result == {"doc-1"}


class TestGetLatestDocumentDate:
    """Test FHIRClient.get_latest_document_date."""

    URL = (
        f"http://localhost:8080/fhir/DocumentReference?patient=pat-1"
        f"&identifier={DOCUMENT_IDENTIFIER_SYSTEM_ENCODED}%7C"
        f"&_sort=-date&_count=1&_elements=date"
    )

    def _auth(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )

    def test_returns_date_part(self, fhir, httpx_mock):
        self._auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=self.URL,
            json={
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "DocumentReference",
                            "date": "2024-06-01T00:00:00+00:00",
                        }
                    }
                ],
            },
        )
        assert fhir.get_latest_document_date("pat-1") == "2024-06-01"

    def test_no_documents_returns_none(self, fhir, httpx_mock):
        self._auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=self.URL,
            json={"resourceType": "Bundle", "entry": []},
        )
        assert fhir.get_latest_document_date("pat-1") is None

    def test_dateless_document_returns_none(self, fhir, httpx_mock):
        self._auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=self.URL,
            json={
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "DocumentReference"}}],
            },
        )
        assert fhir.get_latest_document_date("pat-1") is None


class TestGetRelatedDocumentDates:
    """Test FHIRClient.get_related_document_dates."""

    URL = (
        f"http://localhost:8080/fhir/DocumentReference?patient=pat-1"
        f"&identifier={DOCUMENT_IDENTIFIER_SYSTEM_ENCODED}%7C"
        f"&_elements=date%2Ccontext&_count=1000"
    )

    def _auth(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )

    @staticmethod
    def _docref(date, refs):
        return {
            "resource": {
                "resourceType": "DocumentReference",
                **({"date": date} if date else {}),
                "context": {"related": [{"reference": r} for r in refs]},
            }
        }

    def test_newest_date_wins(self, fhir, httpx_mock):
        self._auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=self.URL,
            json={
                "resourceType": "Bundle",
                "entry": [
                    self._docref("2024-04-01T00:00:00Z", ["Condition/c1"]),
                    self._docref("2024-06-01T00:00:00Z", ["Condition/c1"]),
                ],
            },
        )
        assert fhir.get_related_document_dates("pat-1") == {
            "Condition/c1": "2024-06-01"
        }

    def test_absolute_refs_normalized_urn_and_dateless_skipped(self, fhir, httpx_mock):
        self._auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=self.URL,
            json={
                "resourceType": "Bundle",
                "entry": [
                    self._docref(
                        "2024-05-01T00:00:00Z",
                        [
                            "http://localhost:8080/fhir/Procedure/p1",
                            "urn:uuid:not-yet-created",
                        ],
                    ),
                    self._docref(None, ["Condition/from-dateless-doc"]),
                ],
            },
        )
        assert fhir.get_related_document_dates("pat-1") == {
            "Procedure/p1": "2024-05-01"
        }


class TestDeleteMetaTags:
    """Test FHIRClient.delete_meta_tags."""

    def test_posts_meta_delete_parameters(self, fhir, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/Condition/c1/$meta-delete",
            json={"resourceType": "Meta", "tag": []},
        )

        fhir.delete_meta_tags(
            "Condition",
            "c1",
            [
                (
                    "https://qa.prism.test/fhir/CodeSystem/validation-status",
                    "unvalidated",
                )
            ],
        )

        request = httpx_mock.get_requests()[-1]
        body = json.loads(request.content)
        assert body["resourceType"] == "Parameters"
        assert body["parameter"][0]["name"] == "meta"
        assert body["parameter"][0]["valueMeta"]["tag"] == [
            {
                "system": "https://qa.prism.test/fhir/CodeSystem/validation-status",
                "code": "unvalidated",
            }
        ]


class TestPostBundleTokenRefresh:
    """post_bundle must refresh an expired OAuth token like every other call."""

    def test_401_refreshes_token_and_retries(self, fhir, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token-1"},
        )
        # First POST: token expired
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            status_code=401,
            json={"resourceType": "OperationOutcome"},
        )
        # Re-auth + retried POST succeed
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token-2"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            json={
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [{"response": {"status": "201 Created"}}],
            },
        )

        result = fhir.post_bundle(
            [
                {
                    "resource": {"resourceType": "Condition"},
                    "request": {"method": "POST", "url": "Condition"},
                }
            ]
        )
        assert result.success
        assert result.created == 1

    def test_non_json_error_body_handled(self, fhir, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/auth/token",
            json={"access_token": "token"},
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/",
            status_code=502,
            text="<html>Bad Gateway</html>",
        )

        result = fhir.post_bundle(
            [
                {
                    "resource": {"resourceType": "Condition"},
                    "request": {"method": "POST", "url": "Condition"},
                }
            ]
        )
        assert result.status == "failed"
