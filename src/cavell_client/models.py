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

    @property
    def resources(self) -> list[dict]:
        """Extract the FHIR resources from the bundle."""
        return [e["resource"] for e in self.bundle.get("entry", [])]


class CavellError(Exception):
    """Base exception for Cavell client errors."""


class PatientNotFoundError(CavellError):
    """Raised when a patient_id is provided but not found in FHIR server."""

    def __init__(self, patient_id: str):
        self.patient_id = patient_id
        super().__init__(f"Patient not found: {patient_id}")


class OutOfOrderDocument(NamedTuple):
    """One document that predates its patient's newest persisted document."""

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
    """Raised when a document is older than its patient's newest persisted one.

    Extraction is context-aware and only moves forward in time: each note is
    extracted against the resources its predecessors produced. A note older
    than what is already persisted for that patient would be read against a
    future clinical picture, so the pipeline refuses it up front — before any
    document in the call is extracted and before any tokens are spent.

    Inspect :attr:`violations` to see every offending document. To ingest them,
    either delete the patient's data and re-extract the whole timeline in order
    (see ``CavellClient.delete_patient_resources``), or drop the older
    documents from the batch.
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
            f"Extraction only moves forward in time, so nothing was extracted:\n"
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
