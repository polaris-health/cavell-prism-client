"""Cavell API client for FHIR extraction."""

import logging
import threading
import time
from urllib.parse import urlparse

import httpx

from cavell_client.models import (
    CavellAPIError,
    CavellAuthError,
    CavellGatewayUnavailableError,
    UsageStats,
    parse_usage,
)

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3

# A WAF-issued 429 tells us how long to wait (Retry-After: 300); never wait
# longer than that even if the header says so.
_RETRY_AFTER_CAP = 300.0

_REMOVED_AUTH_KWARGS = ("username", "password")


def _normalize_base_url(url: str) -> str:
    """Append the /api prefix when the URL has no path.

    All Prism API routes live under /api; a bare host would 404 on every
    request. URLs that already carry a path (e.g. ".../api") are untouched.
    """
    url = url.rstrip("/")
    if urlparse(url).path in ("", "/"):
        logger.info(f"No path in base_url; using {url}/api")
        return f"{url}/api"
    return url


def _reject_removed_auth_kwargs(kwargs: dict) -> None:
    """Raise a pointed migration error for the removed Basic-auth kwargs."""
    if any(k in kwargs for k in _REMOVED_AUTH_KWARGS):
        raise TypeError(
            "cavell-prism-client 0.2.0 replaced HTTP Basic auth with LLM Gateway "
            "keys: pass api_key=<your gateway key> instead of "
            "username/password. See the CHANGELOG for migration notes."
        )
    if kwargs:
        unexpected = ", ".join(sorted(kwargs))
        raise TypeError(f"Unexpected keyword argument(s): {unexpected}")


class CavellAPI:
    """Client for Cavell extraction API."""

    def __init__(self, base_url: str, api_key: str = "", **removed_kwargs):
        """Initialize Cavell API client.

        Args:
            base_url: Cavell API base URL, including the /api prefix
                (e.g., "https://prd.prism.cavell.app/api"). The prefix is
                appended automatically if the URL has no path.
            api_key: LLM Gateway key, sent as "Authorization: Bearer <key>"
                on every request.
        """
        _reject_removed_auth_kwargs(removed_kwargs)
        if not api_key or not api_key.strip():
            raise ValueError("api_key is required (LLM Gateway key)")
        self.base_url = _normalize_base_url(base_url)
        self._api_key = api_key
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()

    def _get_client(self) -> httpx.Client:
        """Get or create the HTTP client (thread-safe)."""
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                self._client = httpx.Client(
                    base_url=self.base_url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    # Extraction involves many LLM calls; the API caps each call at
                    # ~300s, so a whole document (incl. an occasional slow call + its
                    # server-side retry) must be allowed comfortably more than that.
                    # Connect stays short: a blackholed host should fail in
                    # seconds, not hold a worker for the full read budget.
                    timeout=httpx.Timeout(800.0, connect=10.0),
                )
            return self._client

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        """Raise a CavellAPIError subclass if the response indicates failure."""
        if response.status_code >= 400:
            error_detail = "Unknown error"
            details = {}
            try:
                error_json = response.json()
                error_detail = error_json.get("detail", str(error_json))
                details = error_json
            except Exception:
                error_detail = response.text or f"HTTP {response.status_code}"
            if response.status_code == 401:
                raise CavellAuthError(response.status_code, error_detail, details)
            if response.status_code == 503:
                raise CavellGatewayUnavailableError(
                    response.status_code, error_detail, details
                )
            raise CavellAPIError(response.status_code, error_detail, details)

    def check_connection(self) -> None:
        """Validate the connection AND the key (GET /key/info).

        The server pre-flights the presented key against the LLM Gateway
        without spending any tokens, so this catches a wrong URL, a missing
        key, and an invalid/expired key up front.

        Raises CavellAuthError (401) when the key is missing or rejected,
        CavellGatewayUnavailableError (503) when the gateway is unreachable,
        CavellAPIError on other failures.
        """
        client = self._get_client()
        response = client.get("/key/info")
        self._raise_for_error(response)
        # A 2xx alone does not prove the key was checked: a base_url pointing
        # at a host that answers unknown paths with a page (a SPA catch-all,
        # a proxy error page) would turn "invalid key" into "valid". Demand
        # the documented body, so a wrong URL - or an API too old to expose
        # this route - fails loudly instead of passing silently.
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or payload.get("valid") is not True:
            raise CavellAPIError(
                response.status_code,
                f"GET {self.base_url}/key/info did not return the expected "
                '{"valid": true} response. Check that api_url points at a '
                "Cavell Prism API (including the /api path) and that the "
                "server exposes /key/info.",
            )

    def extract_raw(
        self,
        text: str,
        context: list[dict] | None = None,
        meta: str | None = None,
        document_date: str | None = None,
        tier: str | None = None,
        allowed_resources: list[str] | None = None,
        patient_id: str | None = None,
        organization_id: str | None = None,
        practitioner_id: str | None = None,
        document_identifier: str | None = None,
        visit_identifier: str | None = None,
    ) -> dict:
        """Extract FHIR resources and return the response body verbatim.

        Same arguments, retry behaviour and error contract as :meth:`extract`,
        but nothing is dropped or reshaped: callers get every documented
        response field, including ``extraction_status``, ``failed_extractors``
        and the nested ``usage.breakdown``. Use this when you need fields the
        typed :class:`~cavell_client.models.ExtractResult` does not surface, or
        when you want to persist the raw response.

        Args:
            document_date: When the document was written, as an ISO
                ``YYYY-MM-DD`` string. Sent as its own payload field rather
                than buried in ``meta`` prose.

        Returns:
            The decoded JSON response body.

        Raises:
            CavellAPIError: If the API returns an error
        """
        client = self._get_client()

        payload: dict = {"text": text}
        if context:
            payload["context"] = context
        if meta:
            payload["meta"] = meta
        if document_date:
            payload["document_date"] = document_date
        if tier:
            payload["tier"] = tier
        if allowed_resources:
            payload["allowed_resources"] = allowed_resources
        if patient_id:
            payload["patient_id"] = patient_id
        if organization_id:
            payload["organization_id"] = organization_id
        if practitioner_id:
            payload["practitioner_id"] = practitioner_id
        if document_identifier:
            payload["document_identifier"] = document_identifier
        if visit_identifier:
            payload["visit_identifier"] = visit_identifier

        response = client.post("/extract/text", json=payload)
        for attempt in range(1, _MAX_RETRIES + 1):
            if response.status_code != 429:
                break
            # WAF rate limits send Retry-After; upstream LLM 429s do not.
            retry_after = response.headers.get("Retry-After", "")
            try:
                # int(), not isdigit(): isdigit() accepts Unicode digits that
                # float() rejects; HTTP-date values fall back to backoff.
                wait = min(float(int(retry_after)), _RETRY_AFTER_CAP)
                source = "Retry-After"
            except ValueError:
                wait = float(2**attempt)
                source = "backoff"
            logger.warning(
                f"Rate limited, retrying in {wait:.0f}s "
                f"({source}, {attempt}/{_MAX_RETRIES})"
            )
            time.sleep(wait)
            response = client.post("/extract/text", json=payload)
        self._raise_for_error(response)

        return response.json()

    def extract(
        self,
        text: str,
        context: list[dict] | None = None,
        meta: str | None = None,
        document_date: str | None = None,
        tier: str | None = None,
        allowed_resources: list[str] | None = None,
        patient_id: str | None = None,
        organization_id: str | None = None,
        practitioner_id: str | None = None,
        document_identifier: str | None = None,
        visit_identifier: str | None = None,
    ) -> tuple[dict, int, UsageStats | None]:
        """Extract FHIR resources from clinical text.

        A typed view over :meth:`extract_raw`. ``extraction_status`` and
        ``failed_extractors`` are not part of this tuple; they are carried on
        :class:`~cavell_client.models.ExtractResult` by the pipeline, or read
        directly from :meth:`extract_raw`.

        Args:
            text: Clinical text to extract from
            context: Existing FHIR resources for context-aware extraction
            meta: Supplementary free-text context (department, ward, attending
                practitioner). The document date is not part of this — pass it
                as ``document_date``
            document_date: When the document was written, as an ISO
                ``YYYY-MM-DD`` string
            tier: Model tier to use for extraction (low/medium/high)
            allowed_resources: Restrict extraction to these FHIR resource types
                (allowlist); omit to extract all types
            patient_id: FHIR Patient ID for real references in the bundle
            organization_id: FHIR Organization ID for real references
            practitioner_id: FHIR Practitioner ID of attending practitioner
            document_identifier: Identifier stamped on the DocumentReference
            visit_identifier: Visit/admission identifier stamped on the Encounter

        Returns:
            Tuple of (bundle, count, usage_stats)

        Raises:
            CavellAPIError: If the API returns an error
        """
        data = self.extract_raw(
            text,
            context=context,
            meta=meta,
            document_date=document_date,
            tier=tier,
            allowed_resources=allowed_resources,
            patient_id=patient_id,
            organization_id=organization_id,
            practitioner_id=practitioner_id,
            document_identifier=document_identifier,
            visit_identifier=visit_identifier,
        )
        return data.get("bundle", {}), data.get("count", 0), parse_usage(data)

    def list_tiers(self) -> list[dict]:
        """List available model tiers (public endpoint, no key required).

        Returns:
            List of tier dicts with keys: name, default.

        Raises:
            CavellAPIError: If the API returns an error
        """
        client = self._get_client()
        response = client.get("/tiers")
        self._raise_for_error(response)

        return response.json().get("tiers", [])
