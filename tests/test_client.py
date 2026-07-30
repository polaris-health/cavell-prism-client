"""Tests for CavellClient orchestrator."""

import pytest

from cavell_client import CavellClient
from tests.helpers import mock_fhir_auth


class TestCavellClientInit:
    """Test constructor migration errors."""

    def test_username_password_rejected(self):
        with pytest.raises(TypeError, match="api_key"):
            CavellClient(
                api_url="https://qa.prism.cavell.app/api",
                username="u",
                password="p",
                fhir_base_url="http://localhost:8080",
            )

    def test_missing_fhir_base_url_rejected(self):
        with pytest.raises(TypeError, match="fhir_base_url"):
            CavellClient(
                api_url="https://qa.prism.cavell.app/api",
                api_key="k",
            )


class TestCavellClientContextManager:
    """Test context manager."""

    def test_context_manager(self, httpx_mock):
        """Test using client as context manager calls close()."""
        mock_fhir_auth(httpx_mock)

        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient?_summary=count",
            json={"resourceType": "Bundle", "total": 0},
        )

        with CavellClient(
            api_url="https://qa.prism.cavell.app/api",
            api_key="k",
            fhir_base_url="http://localhost:8080",
            fhir_client_id="c",
            fhir_client_secret="s",
        ) as client:
            count = client.count_resources("Patient")
            assert count == 0


class TestGetPatientResources:
    """Test get_patient_resources method."""

    def test_returns_resources(self, client, httpx_mock):
        mock_fhir_auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Condition?subject=pat-1&_count=500",
            json={
                "entry": [
                    {"resource": {"resourceType": "Condition", "id": "c1"}},
                    {"resource": {"resourceType": "Condition", "id": "c2"}},
                ]
            },
        )

        results = client.get_patient_resources("pat-1", "Condition")
        assert len(results) == 2
        assert results[0]["id"] == "c1"
        assert results[1]["id"] == "c2"

    def test_empty_results(self, client, httpx_mock):
        mock_fhir_auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Condition?subject=pat-1&_count=500",
            json={"entry": []},
        )

        results = client.get_patient_resources("pat-1", "Condition")
        assert results == []


class TestCountResources:
    """Test count_resources method."""

    def test_returns_count(self, client, httpx_mock):
        mock_fhir_auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Patient?_summary=count",
            json={"resourceType": "Bundle", "total": 42},
        )

        count = client.count_resources("Patient")
        assert count == 42

    def test_missing_total_returns_zero(self, client, httpx_mock):
        mock_fhir_auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Condition?_summary=count",
            json={"resourceType": "Bundle"},
        )

        count = client.count_resources("Condition")
        assert count == 0


class TestListProcessedDocumentIds:
    """Test list_processed_document_ids delegation."""

    def test_delegates_to_fhir(self, client, monkeypatch):
        expected = {"doc-1", "doc-2"}
        monkeypatch.setattr(
            client._fhir, "list_document_identifiers", lambda **kw: expected
        )
        assert client.list_processed_document_ids() == expected


class TestCheckConnection:
    """Test check_connection() pre-flight check."""

    def test_both_ok(self, client, httpx_mock):
        """Both services reachable → returns ok for both."""
        mock_fhir_auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/metadata",
            json={"resourceType": "CapabilityStatement"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://qa.prism.cavell.app/api/key/info",
            json={"valid": True},
        )

        result = client.check_connection()
        assert result["fhir"]["ok"] is True
        assert result["cavell_api"]["ok"] is True

    def test_fhir_unreachable(self, client, httpx_mock):
        """FHIR server down → fhir.ok is False with error message."""
        mock_fhir_auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/metadata",
            status_code=500,
            text="Internal Server Error",
        )
        httpx_mock.add_response(
            method="GET",
            url="https://qa.prism.cavell.app/api/key/info",
            json={"valid": True},
        )

        result = client.check_connection()
        assert result["fhir"]["ok"] is False
        assert "error" in result["fhir"]
        assert result["cavell_api"]["ok"] is True

    def test_api_bad_credentials(self, client, httpx_mock):
        """Bad Cavell API creds → cavell_api.ok is False."""
        mock_fhir_auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/metadata",
            json={"resourceType": "CapabilityStatement"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://qa.prism.cavell.app/api/key/info",
            status_code=401,
            json={"detail": "Unauthorized"},
        )

        result = client.check_connection()
        assert result["fhir"]["ok"] is True
        assert result["cavell_api"]["ok"] is False
        assert "error" in result["cavell_api"]

    def test_both_fail(self, client, httpx_mock):
        """Both services unreachable → both report errors."""
        mock_fhir_auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/metadata",
            status_code=500,
            text="Down",
        )
        httpx_mock.add_response(
            method="GET",
            url="https://qa.prism.cavell.app/api/key/info",
            status_code=500,
            json={"detail": "Internal server error"},
        )

        result = client.check_connection()
        assert result["fhir"]["ok"] is False
        assert result["cavell_api"]["ok"] is False


class TestClose:
    """Test close() cleanup."""

    def test_close_fhir_runs_even_if_api_close_fails(self, client, monkeypatch):
        """_fhir.close() must run even when _api.close() raises."""
        fhir_closed = False
        original_fhir_close = client._fhir.close

        def track_fhir_close():
            nonlocal fhir_closed
            fhir_closed = True
            original_fhir_close()

        def failing_api_close():
            raise RuntimeError("API close exploded")

        monkeypatch.setattr(client._api, "close", failing_api_close)
        monkeypatch.setattr(client._fhir, "close", track_fhir_close)

        with pytest.raises(RuntimeError, match="API close exploded"):
            client.close()

        assert fhir_closed, "_fhir.close() was never called"


class TestMarkValidated:
    """Test mark_validated ($meta-delete of the unvalidated tag)."""

    TAG_SYSTEM = "https://qa.prism.cavell.app/fhir/CodeSystem/validation-status"

    def test_removes_unvalidated_tag(self, client, httpx_mock):
        mock_fhir_auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Condition/c1",
            json={
                "resourceType": "Condition",
                "id": "c1",
                "meta": {
                    "tag": [
                        {"system": self.TAG_SYSTEM, "code": "unvalidated"},
                        {"system": "https://qa.prism.cavell.app", "code": "sha-abc"},
                    ]
                },
            },
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/fhir/Condition/c1/$meta-delete",
            json={"resourceType": "Meta", "tag": []},
        )

        assert client.mark_validated("Condition", "c1") is True

        import json

        request = httpx_mock.get_requests()[-1]
        body = json.loads(request.content)
        assert body["parameter"][0]["valueMeta"]["tag"] == [
            {"system": self.TAG_SYSTEM, "code": "unvalidated"}
        ]

    def test_no_tag_is_noop(self, client, httpx_mock):
        mock_fhir_auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8080/fhir/Condition/c1",
            json={"resourceType": "Condition", "id": "c1"},
        )

        assert client.mark_validated("Condition", "c1") is False
        # Only auth + GET happened — no $meta-delete POST
        assert all("$meta-delete" not in str(r.url) for r in httpx_mock.get_requests())


class TestListUnvalidatedResources:
    """Test list_unvalidated_resources (_tag search)."""

    def test_searches_by_tag(self, client, httpx_mock):
        mock_fhir_auth(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=(
                "http://localhost:8080/fhir/Condition?subject=pat-1"
                "&_count=500&_tag=unvalidated"
            ),
            json={"entry": [{"resource": {"resourceType": "Condition", "id": "c1"}}]},
        )

        results = client.list_unvalidated_resources("pat-1", "Condition")
        assert len(results) == 1
        assert results[0]["id"] == "c1"


class TestListProcessedDocumentIdsPatientScoped:
    """Test patient-scoped delegation."""

    def test_passes_patient_through(self, client, monkeypatch):
        seen = {}

        def fake(patient=None):
            seen["patient"] = patient
            return {"doc-1"}

        monkeypatch.setattr(client._fhir, "list_document_identifiers", fake)
        assert client.list_processed_document_ids(patient_id="pat-9") == {"doc-1"}
        assert seen["patient"] == "pat-9"
