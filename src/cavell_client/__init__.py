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
from cavell_client.labs import (
    LabIngestionOutcome,
    LabIngestionPipeline,
    LabRejection,
    LabResult,
)
from cavell_client.models import (
    CavellAPIError,
    CavellAuthError,
    CavellError,
    CavellGatewayUnavailableError,
    ExtractResult,
    FHIRAuthError,
    FHIRConnectionError,
    OutOfOrderDocument,
    OutOfOrderDocumentError,
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
    "FHIRConnectionError",
    "IngestionOutcome",
    "IngestionPipeline",
    "LabIngestionOutcome",
    "LabIngestionPipeline",
    "LabRejection",
    "LabResult",
    "Organization",
    "OutOfOrderDocument",
    "OutOfOrderDocumentError",
    "Patient",
    "Practitioner",
    "PatientNotFoundError",
    "ExtractResult",
    "PersistResult",
    "UsageStats",
]

__version__ = "0.10.0"
