"""Data models for Cavell client responses."""

from dataclasses import dataclass, field
from typing import Literal

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
