"""Tests for Cavell API client."""

import pytest

from cavell_client.api import CavellAPI
from cavell_client.models import (
    CavellAPIError,
    CavellAuthError,
    CavellGatewayUnavailableError,
)


@pytest.fixture
def api():
    """Create API client for testing."""
    return CavellAPI(
        base_url="https://qa.prism.cavell.app/api",
        api_key="test-key",
    )


class TestCavellAPIInit:
    """Test CavellAPI initialization."""

    def test_strips_trailing_slash(self):
        api = CavellAPI("https://qa.prism.cavell.app/api/", "key")
        assert api.base_url == "https://qa.prism.cavell.app/api"

    def test_appends_api_prefix_to_bare_host(self):
        api = CavellAPI("https://qa.prism.cavell.app", "key")
        assert api.base_url == "https://qa.prism.cavell.app/api"

    def test_appends_api_prefix_to_bare_host_with_slash(self):
        api = CavellAPI("https://qa.prism.cavell.app/", "key")
        assert api.base_url == "https://qa.prism.cavell.app/api"

    def test_keeps_custom_path(self):
        api = CavellAPI("http://localhost:8000/custom", "key")
        assert api.base_url == "http://localhost:8000/custom"

    def test_sends_bearer_header(self, httpx_mock):
        api = CavellAPI("https://qa.prism.cavell.app/api", "secret-key")
        httpx_mock.add_response(
            method="GET",
            url="https://qa.prism.cavell.app/api/resources",
            json={"resources": []},
        )
        api.check_connection()
        request = httpx_mock.get_request()
        assert request.headers["authorization"] == "Bearer secret-key"

    def test_empty_api_key_rejected(self):
        with pytest.raises(ValueError, match="api_key"):
            CavellAPI("https://qa.prism.cavell.app/api", "")

    def test_username_password_rejected(self):
        with pytest.raises(TypeError, match="api_key"):
            CavellAPI(
                "https://qa.prism.cavell.app/api",
                username="user",
                password="pass",
            )

    def test_unexpected_kwarg_rejected(self):
        with pytest.raises(TypeError, match="token"):
            CavellAPI("https://qa.prism.cavell.app/api", "key", token="x")


class TestCavellAPIExtract:
    """Test extract method."""

    def test_extract_success(self, api, httpx_mock):
        """Test successful extraction."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={
                "bundle": {
                    "resourceType": "Bundle",
                    "type": "transaction",
                    "entry": [
                        {
                            "resource": {"resourceType": "Condition"},
                            "request": {"method": "POST", "url": "Condition"},
                        }
                    ],
                },
                "count": 1,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                    "requests": 2,
                    "estimated_cost": 0.005,
                },
            },
        )

        bundle, count, usage = api.extract(text="Patient has diabetes")

        assert count == 1
        assert bundle["resourceType"] == "Bundle"
        assert len(bundle["entry"]) == 1
        assert usage is not None
        assert usage.total_tokens == 150
        assert usage.estimated_cost == 0.005

    def test_extract_with_options(self, api, httpx_mock):
        """Test extraction with all options."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={"bundle": {"entry": []}, "count": 0},
        )

        api.extract(
            text="Patient has diabetes",
            context=[{"resourceType": "Condition", "id": "123"}],
            meta="Document date: 2024-01-15",
            tier="medium",
            allowed_resources=["Condition", "MedicationRequest"],
        )

        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["text"] == "Patient has diabetes"
        assert "persist" not in body
        assert body["context"] == [{"resourceType": "Condition", "id": "123"}]
        assert body["meta"] == "Document date: 2024-01-15"
        assert body["tier"] == "medium"
        assert body["allowed_resources"] == ["Condition", "MedicationRequest"]

    def test_extract_api_error(self, api, httpx_mock):
        """Test extraction API error handling."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=422,
            json={"detail": "Invalid text format"},
        )

        with pytest.raises(CavellAPIError) as exc_info:
            api.extract(text="bad text")

        assert exc_info.value.status_code == 422
        assert "Invalid text format" in exc_info.value.message

    def test_extract_no_usage(self, api, httpx_mock):
        """Test extraction without usage stats."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={"bundle": {"entry": []}, "count": 0},
        )

        bundle, count, usage = api.extract(text="Patient info")

        assert usage is None

    def test_close(self, api, httpx_mock):
        """Test client cleanup."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={"bundle": {"entry": []}, "count": 0},
        )

        api.extract(text="test")
        assert api._client is not None

        api.close()
        assert api._client is None


class TestExtractNewParams:
    """Test new extract parameters: organization_id, practitioner_id, identifiers."""

    def test_extract_with_organization_id(self, api, httpx_mock):
        """Test extraction with organization_id."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={"bundle": {"entry": []}, "count": 0},
        )

        api.extract(
            text="Patient has diabetes",
            organization_id="org-1",
        )

        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["organization_id"] == "org-1"

    def test_extract_with_practitioner_id(self, api, httpx_mock):
        """Test extraction with practitioner_id."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={"bundle": {"entry": []}, "count": 0},
        )

        api.extract(
            text="Patient has diabetes",
            practitioner_id="prac-1",
        )

        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["practitioner_id"] == "prac-1"

    def test_extract_with_document_identifier(self, api, httpx_mock):
        """Test extraction with document_identifier."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={"bundle": {"entry": []}, "count": 0},
        )

        api.extract(
            text="Patient has diabetes",
            document_identifier="doc-123",
        )

        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["document_identifier"] == "doc-123"

    def test_extract_with_patient_id(self, api, httpx_mock):
        """Test extraction with patient_id parameter."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={"bundle": {"entry": []}, "count": 0},
        )

        api.extract(
            text="Patient has diabetes",
            patient_id="pat-123",
        )

        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["patient_id"] == "pat-123"

    def test_extract_new_params_omitted_when_none(self, api, httpx_mock):
        """Test that new params are absent from payload when None."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={"bundle": {"entry": []}, "count": 0},
        )

        api.extract(text="Patient has diabetes")

        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert "patient_id" not in body
        assert "organization_id" not in body
        assert "practitioner_id" not in body
        assert "document_identifier" not in body
        assert "visit_identifier" not in body


class TestListTiers:
    """Test list_tiers method."""

    def test_list_tiers_success(self, api, httpx_mock):
        """Test successful tier listing."""
        httpx_mock.add_response(
            method="GET",
            url="https://qa.prism.cavell.app/api/tiers",
            json={
                "tiers": [
                    {"name": "low", "default": False},
                    {"name": "medium", "default": True},
                    {"name": "high", "default": False},
                ],
            },
        )

        tiers = api.list_tiers()

        assert len(tiers) == 3
        assert tiers[0]["name"] == "low"
        assert tiers[1]["name"] == "medium"
        assert tiers[1]["default"] is True

    def test_list_tiers_error(self, api, httpx_mock):
        """Test tier listing API error."""
        httpx_mock.add_response(
            method="GET",
            url="https://qa.prism.cavell.app/api/tiers",
            status_code=401,
            json={"detail": "Unauthorized"},
        )

        with pytest.raises(CavellAPIError) as exc_info:
            api.list_tiers()

        assert exc_info.value.status_code == 401


class TestCavellAPIErrors:
    """Test API error handling edge cases."""

    def test_extract_500_error(self, api, httpx_mock):
        """500 → CavellAPIError with correct status_code."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=500,
            json={"detail": "Internal server error"},
        )

        with pytest.raises(CavellAPIError) as exc_info:
            api.extract(text="test")

        assert exc_info.value.status_code == 500
        assert "Internal server error" in exc_info.value.message

    def test_extract_429_retries_then_succeeds(self, api, httpx_mock, monkeypatch):
        """429 followed by 200 → retry succeeds transparently."""
        monkeypatch.setattr("cavell_client.api.time.sleep", lambda s: None)
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=429,
            json={"detail": "Rate limit exceeded"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={"bundle": {"entry": []}, "count": 0},
        )

        bundle, count, usage = api.extract(text="test")
        assert count == 0

    def test_extract_429_exhausts_retries(self, api, httpx_mock, monkeypatch):
        """Persistent 429 → CavellAPIError after all retries."""
        monkeypatch.setattr("cavell_client.api.time.sleep", lambda s: None)
        for _ in range(4):  # 1 initial + 3 retries
            httpx_mock.add_response(
                method="POST",
                url="https://qa.prism.cavell.app/api/extract/text",
                status_code=429,
                json={"detail": "Rate limit exceeded"},
            )

        with pytest.raises(CavellAPIError) as exc_info:
            api.extract(text="test")

        assert exc_info.value.status_code == 429

    def test_extract_429_honors_retry_after(self, api, httpx_mock, monkeypatch):
        """A 429 with Retry-After waits that many seconds, not the backoff."""
        sleeps: list[float] = []
        monkeypatch.setattr("cavell_client.api.time.sleep", sleeps.append)
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=429,
            json={"detail": "Rate limit exceeded"},
            headers={"Retry-After": "7"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={"bundle": {"entry": []}, "count": 0},
        )

        api.extract(text="test")
        assert sleeps == [7.0]

    def test_extract_429_caps_retry_after(self, api, httpx_mock, monkeypatch):
        """An absurd Retry-After is capped at 300s."""
        sleeps: list[float] = []
        monkeypatch.setattr("cavell_client.api.time.sleep", sleeps.append)
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=429,
            json={"detail": "Rate limit exceeded"},
            headers={"Retry-After": "9999"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={"bundle": {"entry": []}, "count": 0},
        )

        api.extract(text="test")
        assert sleeps == [300.0]

    def test_extract_429_backoff_without_header(self, api, httpx_mock, monkeypatch):
        """429s without Retry-After (upstream LLM) use exponential backoff."""
        sleeps: list[float] = []
        monkeypatch.setattr("cavell_client.api.time.sleep", sleeps.append)
        for _ in range(3):
            httpx_mock.add_response(
                method="POST",
                url="https://qa.prism.cavell.app/api/extract/text",
                status_code=429,
                json={"detail": "Rate limit exceeded"},
            )
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            json={"bundle": {"entry": []}, "count": 0},
        )

        api.extract(text="test")
        assert sleeps == [2.0, 4.0, 8.0]

    def test_extract_401_raises_auth_error(self, api, httpx_mock):
        """401 → CavellAuthError (a CavellAPIError subclass)."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=401,
            json={"detail": "LLM Gateway rejected the provided key."},
        )

        with pytest.raises(CavellAuthError) as exc_info:
            api.extract(text="test")

        assert isinstance(exc_info.value, CavellAPIError)
        assert exc_info.value.status_code == 401
        assert "rejected" in exc_info.value.message

    def test_extract_503_raises_gateway_error(self, api, httpx_mock):
        """503 → CavellGatewayUnavailableError (a CavellAPIError subclass)."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=503,
            json={"detail": "LLM Gateway is unreachable; try again shortly."},
        )

        with pytest.raises(CavellGatewayUnavailableError) as exc_info:
            api.extract(text="test")

        assert isinstance(exc_info.value, CavellAPIError)
        assert exc_info.value.status_code == 503


class TestCheckConnection:
    """Test check_connection method."""

    def test_pings_resources_route(self, api, httpx_mock):
        """check_connection hits the authenticated /resources route."""
        httpx_mock.add_response(
            method="GET",
            url="https://qa.prism.cavell.app/api/resources",
            json={"resources": []},
        )

        api.check_connection()

        request = httpx_mock.get_request()
        assert str(request.url).endswith("/resources")

    def test_missing_key_raises_auth_error(self, api, httpx_mock):
        """A 401 from the API surfaces as CavellAuthError."""
        httpx_mock.add_response(
            method="GET",
            url="https://qa.prism.cavell.app/api/resources",
            status_code=401,
            json={"detail": "Missing LLM Gateway key."},
        )

        with pytest.raises(CavellAuthError):
            api.check_connection()

    def test_extract_non_json_error(self, api, httpx_mock):
        """502 with plain text body → CavellAPIError uses response.text."""
        httpx_mock.add_response(
            method="POST",
            url="https://qa.prism.cavell.app/api/extract/text",
            status_code=502,
            text="Bad Gateway",
        )

        with pytest.raises(CavellAPIError) as exc_info:
            api.extract(text="test")

        assert exc_info.value.status_code == 502
        assert "Bad Gateway" in exc_info.value.message
