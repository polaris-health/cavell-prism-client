"""Python client for Cavell FHIR extraction API."""

from cavell_client.client import CavellClient
from cavell_client.database import DatabaseSource
from cavell_client.ingestion import (
    DEFAULT_NOTE_COLUMNS,
    DEFAULT_PATIENT_COLUMNS,
    DatabaseExtractionReport,
    Document,
    DocumentList,
    DocumentRejection,
    IngestionOutcome,
    IngestionPipeline,
    Organization,
    Patient,
    Practitioner,
)
from cavell_client.labs import (
    DEFAULT_LAB_COLUMNS,
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
from cavell_client.reporting import FailureCategory, FailureRecord

__all__ = [
    "CavellClient",
    "CavellError",
    "CavellAPIError",
    "CavellAuthError",
    "CavellGatewayUnavailableError",
    "DatabaseExtractionReport",
    "DEFAULT_LAB_COLUMNS",
    "DEFAULT_NOTE_COLUMNS",
    "DEFAULT_PATIENT_COLUMNS",
    "DatabaseSource",
    "Document",
    "DocumentList",
    "DocumentRejection",
    "FailureCategory",
    "FailureRecord",
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
