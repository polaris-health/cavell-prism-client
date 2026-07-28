"""Python client for Cavell FHIR extraction API."""

from cavell_client.client import CavellClient
from cavell_client.ingestion import (
    Document,
    IngestionOutcome,
    IngestionPipeline,
    Organization,
    Patient,
    Practitioner,
)
from cavell_client.models import (
    CavellAPIError,
    CavellAuthError,
    CavellError,
    CavellGatewayUnavailableError,
    ExtractResult,
    FHIRAuthError,
    PatientNotFoundError,
    PersistResult,
    UsageStats,
)

__all__ = [
    "CavellClient",
    "CavellError",
    "CavellAPIError",
    "CavellAuthError",
    "CavellGatewayUnavailableError",
    "Document",
    "FHIRAuthError",
    "IngestionOutcome",
    "IngestionPipeline",
    "Organization",
    "Patient",
    "Practitioner",
    "PatientNotFoundError",
    "ExtractResult",
    "PersistResult",
    "UsageStats",
]

__version__ = "0.2.0"
