"""Data models for Cavell client responses."""

from dataclasses import dataclass, field
from typing import Literal, NamedTuple

# Status of persistence operation
PersistStatus = Literal["success", "partial_failure", "failed"]


@dataclass
class UsageStats:
    """Token usage statistics for an extraction."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    requests: int
    estimated_cost: float
    #: Per-stage, then per-agent token/cost attribution, exactly as the API
    #: returned it (stage -> {..., "breakdown": {label -> {...}}}). Kept as a
    #: plain dict rather than nested UsageStats so an added stage or label
    #: reaches callers without a client release. ``None`` when the API omits it.
    breakdown: dict | None = None


def parse_usage(response: dict) -> "UsageStats | None":
    """Build :class:`UsageStats` from an extraction response body.

    Returns ``None`` when the response carries no ``usage`` object.
    """
    usage = response.get("usage")
    if not usage:
        return None
    return UsageStats(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        requests=usage.get("requests", 0),
        estimated_cost=usage.get("estimated_cost", 0.0),
        breakdown=usage.get("breakdown"),
    )


@dataclass
class PersistResult:
    """Result of persisting resources to local FHIR server."""

    status: PersistStatus
    created: int
    updated: int
    errors: list[dict] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """True if all resources were persisted successfully."""
        return self.status == "success"


@dataclass
class ExtractResult:
    """Result of FHIR extraction from clinical text."""

    bundle: dict  # FHIR transaction bundle
    count: int  # Number of resources in bundle
    patient_id: str  # Resolved patient ID
    usage: UsageStats | None = None
    persistence: PersistResult | None = None
    #: "complete" when every extractor ran, "partial" when one or more failed
    #: after their retries. A partial extraction still returns a valid bundle —
    #: it is simply missing whatever the failed extractors would have found.
    extraction_status: str | None = None
    #: Names of the extractors that failed after retries. Non-empty implies
    #: extraction_status == "partial".
    failed_extractors: list[str] = field(default_factory=list)

    @property
    def resources(self) -> list[dict]:
        """Extract the FHIR resources from the bundle."""
        return [e["resource"] for e in self.bundle.get("entry", [])]

    @property
    def is_partial(self) -> bool:
        """True when at least one extractor failed for this document."""
        return self.extraction_status == "partial" or bool(self.failed_extractors)


class CavellError(Exception):
    """Base exception for Cavell client errors."""


class PatientNotFoundError(CavellError):
    """Raised when a patient_id is provided but not found in FHIR server."""

    def __init__(self, patient_id: str):
        self.patient_id = patient_id
        super().__init__(f"Patient not found: {patient_id}")


class OutOfOrderDocument(NamedTuple):
    """One document that predates its patient's newest persisted document.

    A reporting record, not a rejection: these documents are extracted against
    context split at their own date. See ``IngestionOutcome.out_of_order``.
    """

    patient_identifier: str
    document_id: str | None
    document_index: int
    date: str
    watermark: str

    def __str__(self) -> str:
        label = self.document_id or f"doc[{self.document_index}]"
        return (
            f"{label} (patient {self.patient_identifier}) dated {self.date} "
            f"is older than {self.watermark}"
        )


class OutOfOrderDocumentError(CavellError):
    """Deprecated: no longer raised. Nothing is refused for being backdated.

    .. deprecated::
        The pipeline used to abort a whole ``extract()``/``extract_all()`` call
        with this exception as soon as any one document predated its patient's
        watermark, which threw away every other patient's valid work. Such a
        document is now extracted against context split at its own date and
        comes back as ``IngestionOutcome(success=True, out_of_order=True)``,
        where the flag records how it was processed rather than that it was
        rejected.

        Filter outcomes on ``out_of_order`` instead. Retained only so existing
        ``except`` clauses keep importing; it will be removed in a future
        release.
    """

    #: How many offenders to name in the exception message before truncating.
    _MAX_LISTED = 10

    def __init__(self, violations: "list[OutOfOrderDocument]"):
        self.violations = violations
        patients = sorted({v.patient_identifier for v in violations})
        listed = "\n".join(f"  - {v}" for v in violations[: self._MAX_LISTED])
        if len(violations) > self._MAX_LISTED:
            listed += f"\n  ... and {len(violations) - self._MAX_LISTED} more"
        super().__init__(
            f"{len(violations)} document(s) across {len(patients)} patient(s) are "
            f"older than the newest already-persisted document for their patient. "
            f"Extraction only moves forward in time, so they were not extracted:\n"
            f"{listed}"
        )


class CavellAPIError(CavellError):
    """Raised when Cavell API returns an error."""

    def __init__(self, status_code: int, message: str, details: dict | None = None):
        self.status_code = status_code
        self.message = message
        self.details = details or {}
        super().__init__(f"Cavell API error ({status_code}): {message}")


class CavellAuthError(CavellAPIError):
    """Raised when the Cavell API rejects the request with 401.

    Non-retryable: the LLM Gateway key is missing or was rejected. Check the
    key (and the base URL) rather than retrying.
    """


class CavellGatewayUnavailableError(CavellAPIError):
    """Raised when the Cavell API returns 503 (LLM Gateway unreachable).

    A run-global condition: no request will succeed until the gateway is
    reachable again.
    """


class FHIRAuthError(CavellError):
    """Raised when FHIR OAuth2 authentication fails."""

    def __init__(self, message: str):
        super().__init__(f"FHIR authentication failed: {message}")


class FHIRConnectionError(CavellError):
    """Raised when the FHIR server is unreachable or rejects the request.

    Distinct from the Cavell API errors so callers can tell which half of the
    configuration is wrong: this one always points at fhir_base_url.
    """

    def __init__(self, message: str):
        super().__init__(f"FHIR server unreachable: {message}")
