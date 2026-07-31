"""Shared test helpers."""

API_URL = "https://qa.prism.cavell.app/api"


def mock_fhir_auth(httpx_mock):
    """Add FHIR auth token mock."""
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/auth/token",
        json={"access_token": "fhir-token"},
    )


def mock_api_preflight(httpx_mock):
    """Mock the key-validating GET /key/info pre-flight used by extract()."""
    httpx_mock.add_response(
        method="GET",
        url=f"{API_URL}/key/info",
        json={"valid": True},
        repeat=True,
    )


def mock_fhir_preflight(httpx_mock, base_url="http://localhost:8080/fhir"):
    """Mock the GET /metadata pre-flight CavellClient runs at construction."""
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/metadata",
        json={"resourceType": "CapabilityStatement", "status": "active"},
        repeat=True,
    )


def mock_watermark(
    httpx_mock, patient_fhir_id, date=None, repeat=True, status_code=200
):
    """Mock the newest-document-date query for a patient.

    date=None → no processed documents (empty bundle). Replaces any
    previously registered watermark for the same patient (the shared
    extract-plumbing helper registers an empty one).
    """
    entry = (
        []
        if date is None
        else [{"resource": {"resourceType": "DocumentReference", "date": date}}]
    )
    httpx_mock.add_response(
        method="GET",
        url=(
            f"http://localhost:8080/fhir/DocumentReference?patient={patient_fhir_id}"
            f"&identifier=urn%3Acavell%3Adocument%7C&_sort=-date&_count=1"
            f"&_elements=date"
        ),
        json={"resourceType": "Bundle", "entry": entry},
        status_code=status_code,
        replace=True,
        repeat=repeat,
    )
