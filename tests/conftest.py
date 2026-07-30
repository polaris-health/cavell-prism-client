"""Pytest configuration and fixtures."""

import pytest

from cavell_client import CavellClient


@pytest.fixture
def httpx_mock(monkeypatch):
    """Mock httpx requests for testing."""
    import httpx

    class MockTransport:
        def __init__(self):
            self._responses = []
            self._requests = []

        def add_response(
            self,
            method: str = "GET",
            url: str = "",
            status_code: int = 200,
            json: dict | None = None,
            text: str = "",
            headers: dict | None = None,
            repeat: bool = False,
        ):
            self._responses.append(
                {
                    "method": method.upper(),
                    "url": url,
                    "status_code": status_code,
                    "json": json,
                    "text": text,
                    "headers": headers or {},
                    "repeat": repeat,
                }
            )

        def get_request(self):
            return self._requests[0] if self._requests else None

        def get_requests(self):
            return self._requests

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            self._requests.append(request)

            for i, resp in enumerate(self._responses):
                if resp["method"] == request.method and resp["url"] == str(request.url):
                    if not resp["repeat"]:
                        self._responses.pop(i)
                    content = b""
                    if resp["json"] is not None:
                        import json

                        content = json.dumps(resp["json"]).encode()
                    elif resp["text"]:
                        content = resp["text"].encode()

                    return httpx.Response(
                        status_code=resp["status_code"],
                        content=content,
                        headers={"content-type": "application/json", **resp["headers"]},
                        request=request,
                    )

            # No matching response - return 404
            return httpx.Response(
                status_code=404,
                content=b"Not found",
                request=request,
            )

    mock = MockTransport()

    # Patch Client to use our mock transport
    original_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(mock.handle_request)
        # Remove mounts if present (not compatible with MockTransport)
        kwargs.pop("mounts", None)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    return mock


@pytest.fixture
def client(httpx_mock):
    """Create CavellClient for testing.

    Construction pre-flights both halves (GET /key/info and GET /metadata),
    so those stubs are registered here and then withdrawn, leaving the mock
    exactly as the test found it: tests stay free to stub either route
    themselves (including failures), and fixture setup traffic never shows up
    in per-test request assertions.
    """
    from tests.helpers import mock_api_preflight, mock_fhir_auth, mock_fhir_preflight

    first_own = len(httpx_mock._responses)
    mock_api_preflight(httpx_mock)
    mock_fhir_auth(httpx_mock)
    mock_fhir_preflight(httpx_mock)
    own = {id(r) for r in httpx_mock._responses[first_own:]}

    client = CavellClient(
        api_url="https://qa.prism.cavell.app/api",
        api_key="test-key",
        fhir_base_url="http://localhost:8080",
        fhir_client_id="fhir-client",
        fhir_client_secret="fhir-secret",
        fhir_api_path="/fhir",
    )

    httpx_mock._responses[:] = [r for r in httpx_mock._responses if id(r) not in own]
    httpx_mock._requests.clear()
    return client
