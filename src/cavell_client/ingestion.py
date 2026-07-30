"""Ingestion pipeline for loading data into FHIR."""

import concurrent.futures
import datetime
import enum
import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

import httpx

from cavell_client.api import CavellAPI
from cavell_client.fhir import (
    IDENTIFIER_SYSTEM,
    ORGANIZATION_IDENTIFIER_SYSTEM,
    PRACTITIONER_IDENTIFIER_SYSTEM,
    PRACTITIONER_ROLE_IDENTIFIER_SYSTEM,
    FHIRClient,
)
from cavell_client.models import (
    CavellAPIError,
    CavellAuthError,
    CavellGatewayUnavailableError,
    ExtractResult,
    PatientNotFoundError,
)

if TYPE_CHECKING:
    from cavell_client.client import CavellClient

logger = logging.getLogger(__name__)

# Per-document in-place retry for transient failures (timeouts, 5xx, connection
# errors). A fresh extract call after a slow one is usually fast, so retrying keeps
# one blip from cascade-skipping the rest of a patient's timeline.
_DOC_MAX_ATTEMPTS = 3
_DOC_RETRY_BACKOFF = 2.0  # seconds, exponential

# Server 5xx / rate-limit statuses that are worth retrying (content errors are 4xx).
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


def _is_transient_error(exc: Exception) -> bool:
    """True for failures worth retrying: timeouts, connection drops, server 5xx/429.

    Deterministic failures (a rejected bundle, a 4xx content error) are NOT transient —
    retrying reproduces them, so they fail fast as before.
    """
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    return isinstance(exc, CavellAPIError) and exc.status_code in _TRANSIENT_STATUS


def _apply_update_guard(
    entries: list[dict], doc_date: str, related_dates: dict[str, str]
) -> tuple[list[dict], list[str]]:
    """Drop updates to resources whose current version came from a newer document.

    When a document is processed out of chronological order, its bundle may
    carry PUT entries that would overwrite data extracted from newer
    documents. Creates always pass; a PUT is dropped only when provenance
    (``related_dates``, from :meth:`FHIRClient.get_related_document_dates`)
    shows the resource was last touched by a strictly newer document. Equal
    dates and resources with unknown provenance pass.

    Returns (kept_entries, dropped_keys).
    """
    kept: list[dict] = []
    dropped: list[str] = []
    for entry in entries:
        resource = entry.get("resource", {})
        rtype = resource.get("resourceType")
        rid = resource.get("id")
        if entry.get("request", {}).get("method") == "PUT" and rtype and rid:
            key = f"{rtype}/{rid}"
            newest = related_dates.get(key)
            if newest is not None and newest > doc_date:
                dropped.append(key)
                continue
        kept.append(entry)
    return kept, dropped


def _validate_columns(columns: dict[str, str], rows: list[dict[str, str]]) -> None:
    """Check that all mapped column names exist in the rows.

    Args:
        columns: Maps field names to CSV column names.
        rows: Non-empty list of dicts (e.g. from csv.DictReader).

    Raises:
        ValueError: If a mapped column name is not found in the rows.
    """
    available = set(rows[0].keys())
    for field_name, col_name in columns.items():
        if col_name is not None and col_name not in available:
            raise ValueError(
                f"Column '{col_name}' (mapped from '{field_name}') "
                f"not found in rows. Available columns: {sorted(available)}"
            )


@dataclass
class Organization:
    """Hospital or facility to seed into the FHIR server."""

    identifier: str
    name: str


@dataclass
class Practitioner:
    """Practitioner to seed into the FHIR server."""

    identifier: str
    family_name: str
    given_name: str
    organization_identifier: str
    specialty: str | None = None

    @property
    def display_name(self) -> str:
        return f"{self.given_name} {self.family_name}"

    @classmethod
    def from_rows(
        cls,
        rows: list[dict[str, str]],
        columns: dict[str, str],
        **defaults: str,
    ) -> "list[Practitioner]":
        """Build a deduplicated list of Practitioners from CSV/dict rows.

        Args:
            rows: List of dicts (e.g. from csv.DictReader).
            columns: Maps Practitioner field names to column names.
                Must include "identifier". Supports a virtual "name" column
                that splits "Given Family" into given_name/family_name.
            **defaults: Literal values applied to every practitioner
                (e.g. organization_identifier).

        Returns:
            Deduplicated list of Practitioner (first occurrence wins).
            Rows with an empty identifier are skipped.
        """
        valid_fields = {f.name for f in fields(cls)}
        # 'name' is a virtual column, not a real field
        allowed_columns = valid_fields | {"name"}

        if "identifier" not in columns:
            raise ValueError("columns must include 'identifier'")

        has_name = "name" in columns
        has_parts = "given_name" in columns or "family_name" in columns
        if has_name and has_parts:
            raise ValueError(
                "columns cannot use 'name' together with 'given_name' or 'family_name'"
            )

        for key in columns:
            if key not in allowed_columns:
                raise ValueError(f"Unknown Practitioner field in columns: '{key}'")
        for key in defaults:
            if key not in valid_fields:
                raise ValueError(f"Unknown Practitioner field in defaults: '{key}'")

        if not rows:
            return []

        _validate_columns(columns, rows)

        seen: set[str] = set()
        practitioners: list[Practitioner] = []

        for row in rows:
            identifier = row.get(columns["identifier"])
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)

            kwargs: dict[str, Any] = {"identifier": identifier}

            # Handle virtual 'name' column → given_name + family_name
            if has_name:
                name_val = row.get(columns["name"], "")
                if name_val:
                    parts = name_val.split(None, 1)
                    kwargs["given_name"] = parts[0]
                    kwargs["family_name"] = parts[1] if len(parts) > 1 else ""

            # Map remaining real columns
            for field_name, col_name in columns.items():
                if field_name in ("identifier", "name") or col_name is None:
                    continue
                value = row.get(col_name)
                kwargs[field_name] = value if value else None

            kwargs.update(defaults)
            practitioners.append(cls(**kwargs))

        return practitioners


@dataclass
class Patient:
    """Patient to seed into the FHIR server."""

    identifier: str
    name: str | None = None
    birth_date: str | None = None
    gender: str | None = None
    managing_organization: str | None = None
    general_practitioners: str | list[str] | None = None

    def __post_init__(self):
        if isinstance(self.general_practitioners, str):
            self.general_practitioners = [self.general_practitioners]

    @classmethod
    def from_rows(
        cls,
        rows: list[dict[str, str]],
        columns: dict[str, str],
        **defaults: str,
    ) -> "list[Patient]":
        """Build a deduplicated list of Patients from CSV/dict rows.

        Args:
            rows: List of dicts (e.g. from csv.DictReader).
            columns: Maps Patient field names to column names in the rows.
                Must include "identifier". ``None`` values are skipped.
            **defaults: Literal values applied to every patient.

        Returns:
            Deduplicated list of Patient (first occurrence wins).
        """
        valid_fields = {f.name for f in fields(cls)}

        if "identifier" not in columns:
            raise ValueError("columns must include 'identifier'")

        for key in columns:
            if key not in valid_fields:
                raise ValueError(f"Unknown Patient field in columns: '{key}'")
        for key in defaults:
            if key not in valid_fields:
                raise ValueError(f"Unknown Patient field in defaults: '{key}'")

        if not rows:
            return []

        _validate_columns(columns, rows)

        seen: set[str] = set()
        patients: list[Patient] = []

        for row in rows:
            identifier = row.get(columns["identifier"])
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)

            kwargs: dict[str, Any] = {"identifier": identifier}
            for field_name, col_name in columns.items():
                if field_name == "identifier" or col_name is None:
                    continue
                value = row.get(col_name)
                kwargs[field_name] = value if value else None

            # Defaults override column-derived values
            kwargs.update(defaults)

            patients.append(cls(**kwargs))

        return patients


@dataclass
class Document:
    """Clinical document for extraction."""

    text: str
    patient_identifier: str
    date: str | datetime.date
    organization_identifier: str | None = None
    meta: str | None = None
    """Extra context (e.g. department). Do NOT include date or
    practitioner — the pipeline injects those automatically."""
    practitioner_identifier: str | None = None
    document_id: str | None = None
    visit_id: str | None = None

    @classmethod
    def from_rows(
        cls,
        rows: list[dict[str, str]],
        columns: "Mapping[str, str | dict[str, str]]",
        **defaults: str,
    ) -> "list[Document]":
        """Build a list of Documents from CSV/dict rows.

        Args:
            rows: List of dicts (e.g. from csv.DictReader).
            columns: Maps Document field names to column names.
                Must include "text", "patient_identifier", and "date".
                ``None`` values are skipped.
            **defaults: Literal values applied to every document
                (e.g. organization_identifier).

        Returns:
            List of Document, one per row.
        """
        valid_fields = {f.name for f in fields(cls)}

        for required in ("text", "patient_identifier", "date"):
            if required not in columns:
                raise ValueError(f"columns must include '{required}'")

        for key in columns:
            if key not in valid_fields:
                raise ValueError(f"Unknown Document field in columns: '{key}'")
        for key in defaults:
            if key not in valid_fields:
                raise ValueError(f"Unknown Document field in defaults: '{key}'")

        if not rows:
            return []

        # Separate dict meta from flat string columns
        meta_dict: dict[str, str] | None = None
        flat_columns: dict[str, str] = {}
        for k, v in columns.items():
            if k == "meta" and isinstance(v, dict):
                meta_dict = v
            elif isinstance(v, str):
                flat_columns[k] = v

        _validate_columns(flat_columns, rows)

        # Validate dict meta column names
        if meta_dict is not None:
            available = set(rows[0].keys())
            for label, inner_col in meta_dict.items():
                if inner_col not in available:
                    raise ValueError(
                        f"Column '{inner_col}' (mapped from 'meta[{label}]') "
                        f"not found in rows. Available columns: {sorted(available)}"
                    )

        # Check for duplicate document_ids upfront
        if "document_id" in flat_columns:
            doc_id_col = flat_columns["document_id"]
            seen_ids: dict[str, int] = {}
            duplicates: list[str] = []
            for i, row in enumerate(rows):
                doc_id = row.get(doc_id_col)
                if doc_id:
                    if doc_id in seen_ids:
                        duplicates.append(doc_id)
                    else:
                        seen_ids[doc_id] = i
            if duplicates:
                raise ValueError(
                    f"Duplicate document_id values: {sorted(set(duplicates))}"
                )

        documents: list[Document] = []
        for row in rows:
            kwargs: dict[str, Any] = {}

            # Build meta from dict columns
            if meta_dict is not None:
                parts = []
                for label, inner_col in meta_dict.items():
                    val = row.get(inner_col)
                    if val:
                        parts.append(f"{label}: {val}")
                kwargs["meta"] = "\n".join(parts) if parts else None

            for field_name, col_name in flat_columns.items():
                value = row.get(col_name)
                if field_name in ("text", "patient_identifier", "date"):
                    kwargs[field_name] = value
                else:
                    kwargs[field_name] = value if value else None

            kwargs.update(defaults)
            documents.append(cls(**kwargs))

        return documents

    def __post_init__(self):
        if not self.text or not self.text.strip():
            raise ValueError("Document.text must not be empty")
        if not self.patient_identifier or not self.patient_identifier.strip():
            raise ValueError("Document.patient_identifier must not be empty")

        stripped = self.text.strip()
        if len(stripped) < 20:
            label = self.document_id or "unknown"
            logger.warning(f"Document '{label}' has short text ({len(stripped)} chars)")

        if isinstance(self.date, datetime.datetime):
            self.date = self.date.date().isoformat()
        elif isinstance(self.date, datetime.date):
            self.date = self.date.isoformat()
        else:
            try:
                # Normalize, don't just validate: fromisoformat also accepts
                # compact forms like "20240115", and every chronology decision
                # downstream is a lexicographic string comparison.
                self.date = datetime.date.fromisoformat(self.date).isoformat()
            except ValueError:
                raise ValueError(
                    f"Invalid date format '{self.date}', "
                    "expected ISO format (YYYY-MM-DD)"
                ) from None


@dataclass
class IngestionOutcome:
    """Result of processing a single document."""

    success: bool
    patient_identifier: str
    document_index: int
    extract_result: ExtractResult | None = None
    error: str | None = None
    document_id: str | None = None
    # transient failure (timeout/5xx) — eligible for the deferred retry pass
    transient: bool = False
    # document was older than the patient's newest already-persisted document
    out_of_order: bool = False

    def __str__(self) -> str:
        label = self.document_id or f"doc[{self.document_index}]"
        marker = " [out-of-order]" if self.out_of_order else ""
        if not self.success:
            transient = " [transient]" if self.transient else ""
            return f"  {label} FAILED{transient}{marker}: {self.error}"
        r = self.extract_result
        created = r.persistence.created if r and r.persistence else 0
        updated = r.persistence.updated if r and r.persistence else 0
        cost = r.usage.estimated_cost if r and r.usage else 0.0
        count = r.count if r else 0
        return (
            f"  {label} -> {count} resources"
            f" ({created} new, {updated} updated)"
            f"  ${cost:.3f}{marker}"
        )


class _Phase(enum.Enum):
    CREATED = "created"
    PATIENTS_SEEDED = "patients_seeded"
    EXTRACTING = "extracting"


class IngestionPipeline:
    """Orchestrates 3-phase data ingestion: references → patients → extraction.

    Enforces correct ordering so that FHIR references are always valid:
    organizations and practitioners must exist before patients can reference them,
    and patients must exist before documents can be extracted against them.

    Practitioners are matched by the SDK after extraction — the Cavell API
    extracts practitioner names from text, and the SDK links them to seeded
    practitioners in FHIR.
    """

    def __init__(
        self,
        client: "CavellClient",
        tier: str | None = None,
        max_concurrency: int = 5,
        default_organization: str | None = None,
    ):
        self._api: CavellAPI = client._api
        self._fhir: FHIRClient = client._fhir
        self._tier = tier
        self._max_concurrency = max_concurrency
        self._default_organization = default_organization
        self._phase = _Phase.CREATED
        self._id_map: dict[tuple[str, str], str] = {}
        self._practitioner_names: dict[str, str] = {}
        self._documents_processed: int = 0
        self._documents_failed: int = 0
        self._total_cost: float = 0.0

    @property
    def documents_processed(self) -> int:
        """Total documents successfully extracted across all extract() calls."""
        return self._documents_processed

    @property
    def documents_failed(self) -> int:
        """Total documents that failed across all extract() calls."""
        return self._documents_failed

    @property
    def total_cost(self) -> float:
        """Cumulative estimated cost (USD) across all extract() calls."""
        return self._total_cost

    def _resolve_id(self, system: str, identifier: str) -> str:
        """Look up a FHIR server ID from the id_map."""
        return self._id_map[(system, identifier)]

    def seed(
        self,
        organizations: list[Organization],
        patients: list[Patient],
        practitioners: list[Practitioner] | None = None,
    ) -> None:
        """Seed organizations, practitioners, and patients into the FHIR server.

        Must be called before extract(). Enforces ordering: organizations and
        practitioners are created first so patient references resolve correctly.

        Args:
            organizations: Organizations to create/update
            patients: Patients to create/update
            practitioners: Practitioners to create/update (linked to orgs)

        Raises:
            RuntimeError: If called out of order or seeding fails
            ValueError: If validation fails
        """
        if self._phase != _Phase.CREATED:
            raise RuntimeError(
                f"seed() requires phase 'created', currently '{self._phase.value}'"
            )
        if not organizations:
            raise ValueError("At least one organization is required")

        practitioners = practitioners or []

        # Cross-validate practitioner org references
        org_ids = {org.identifier for org in organizations}
        for prac in practitioners:
            if prac.organization_identifier not in org_ids:
                raise ValueError(
                    f"Practitioner '{prac.identifier}' references unknown "
                    f"organization '{prac.organization_identifier}'"
                )

        # Cross-validate patient references
        prac_ids = {prac.identifier for prac in practitioners}
        for patient in patients:
            if patient.managing_organization:
                if patient.managing_organization not in org_ids:
                    raise ValueError(
                        f"Patient '{patient.identifier}' references unknown "
                        f"organization '{patient.managing_organization}'"
                    )
            for gp_id in patient.general_practitioners or []:
                if gp_id not in prac_ids:
                    raise ValueError(
                        f"Patient '{patient.identifier}' references unknown "
                        f"practitioner '{gp_id}'"
                    )

        # Build org FHIR resources
        org_resources = [
            {
                "resourceType": "Organization",
                "identifier": [
                    {"system": ORGANIZATION_IDENTIFIER_SYSTEM, "value": org.identifier}
                ],
                "name": org.name,
            }
            for org in organizations
        ]

        # Build practitioner FHIR resources
        prac_resources = [
            {
                "resourceType": "Practitioner",
                "identifier": [
                    {"system": PRACTITIONER_IDENTIFIER_SYSTEM, "value": prac.identifier}
                ],
                "name": [{"family": prac.family_name, "given": [prac.given_name]}],
            }
            for prac in practitioners
        ]

        # Seed orgs and practitioners
        org_result, org_id_map = self._fhir.seed_bundle(org_resources)
        if not org_result.success:
            raise RuntimeError(f"Failed to seed organizations: {org_result.errors}")
        self._id_map.update(org_id_map)

        if prac_resources:
            prac_result, prac_id_map = self._fhir.seed_bundle(prac_resources)
            if not prac_result.success:
                raise RuntimeError(
                    f"Failed to seed practitioners: {prac_result.errors}"
                )
            self._id_map.update(prac_id_map)

        # Store practitioner identifier → name for meta injection
        for prac in practitioners:
            self._practitioner_names[prac.identifier] = prac.display_name

        # Auto-create PractitionerRoles linking each practitioner to their org
        if practitioners:
            role_resources = []
            for prac in practitioners:
                prac_fhir_id = self._resolve_id(
                    PRACTITIONER_IDENTIFIER_SYSTEM, prac.identifier
                )
                org_fhir_id = self._resolve_id(
                    ORGANIZATION_IDENTIFIER_SYSTEM, prac.organization_identifier
                )
                role_value = f"{prac.identifier}@{prac.organization_identifier}"
                role: dict = {
                    "resourceType": "PractitionerRole",
                    "identifier": [
                        {
                            "system": PRACTITIONER_ROLE_IDENTIFIER_SYSTEM,
                            "value": role_value,
                        }
                    ],
                    "practitioner": {"reference": f"Practitioner/{prac_fhir_id}"},
                    "organization": {"reference": f"Organization/{org_fhir_id}"},
                }
                if prac.specialty:
                    role["specialty"] = [{"text": prac.specialty}]
                role_resources.append(role)
            role_result, role_id_map = self._fhir.seed_bundle(role_resources)
            if not role_result.success:
                raise RuntimeError(
                    f"Failed to seed practitioner roles: {role_result.errors}"
                )
            self._id_map.update(role_id_map)

        # Seed patients
        patient_resources = []
        for patient in patients:
            resource: dict = {
                "resourceType": "Patient",
                "identifier": [
                    {"system": IDENTIFIER_SYSTEM, "value": patient.identifier}
                ],
            }
            if patient.name:
                resource["name"] = [{"text": patient.name}]
            if patient.birth_date:
                resource["birthDate"] = patient.birth_date
            if patient.gender:
                resource["gender"] = patient.gender
            if patient.managing_organization:
                org_fhir_id = self._resolve_id(
                    ORGANIZATION_IDENTIFIER_SYSTEM, patient.managing_organization
                )
                resource["managingOrganization"] = {
                    "reference": f"Organization/{org_fhir_id}"
                }
            if patient.general_practitioners:
                gp_refs = []
                for gp_id in patient.general_practitioners:
                    prac_fhir_id = self._resolve_id(
                        PRACTITIONER_IDENTIFIER_SYSTEM, gp_id
                    )
                    gp_refs.append({"reference": f"Practitioner/{prac_fhir_id}"})
                resource["generalPractitioner"] = gp_refs
            patient_resources.append(resource)

        result, id_map = self._fhir.seed_bundle(patient_resources)
        if not result.success:
            raise RuntimeError(f"Failed to seed patients: {result.errors}")
        self._id_map.update(id_map)

        self._phase = _Phase.PATIENTS_SEEDED

    def extract(
        self,
        documents: list[Document],
        *,
        skip_processed: bool = True,
        batch_size: int | None = None,
    ) -> "list[IngestionOutcome]":
        """Phase 3: Extract FHIR resources from documents.

        Processes patients concurrently (limited by max_concurrency),
        documents within each patient sequentially in date order.

        Can be called multiple times. When ``skip_processed=True`` (the
        default), already-processed documents are automatically filtered
        out so re-running is always safe.

        Documents older than the patient's newest already-persisted document
        are flagged ``out_of_order`` (with a warning) and still processed,
        but their bundle passes an update guard: updates to resources whose
        current version came from a newer document are dropped.

        Args:
            documents: Documents to extract from
            skip_processed: Query FHIR for already-processed document IDs
                and skip them.  Set to ``False`` to process all documents.
            batch_size: Cap the number of documents processed in this call.
                Applied after skip_processed filtering.

        Returns:
            List of IngestionOutcome, one per document

        Raises:
            RuntimeError: If called before patients are seeded
            ValueError: If validation fails
            CavellAuthError: If the API rejects the key (checked once up
                front, and the run aborts on the first 401 mid-run)
            CavellGatewayUnavailableError: If the LLM Gateway stays
                unreachable through the in-place retries (run aborts)
        """
        if self._phase not in (_Phase.PATIENTS_SEEDED, _Phase.EXTRACTING):
            raise RuntimeError(
                f"extract() requires phase 'patients_seeded' or 'extracting', "
                f"currently '{self._phase.value}'"
            )

        # One cheap validated GET before any spend: the server pre-flights
        # the key against the LLM Gateway (no tokens), so a wrong URL, a
        # missing key, or an invalid key all fail here, before any documents
        # are processed.
        self._api.check_connection()

        # Resume filtering: skip already-processed documents
        skipped = 0
        if skip_processed:
            no_id = [d for d in documents if not d.document_id]
            if no_id:
                logger.warning(
                    f"{len(no_id)} document(s) have no document_id and cannot "
                    f"be tracked for resume — they will be re-processed every run"
                )
            processed_ids: set[str] = set()
            for pid in {d.patient_identifier for d in documents}:
                fhir_id = self._id_map.get((IDENTIFIER_SYSTEM, pid))
                if fhir_id:
                    processed_ids |= self._fhir.list_document_identifiers(
                        patient=fhir_id
                    )
            original_count = len(documents)
            documents = [
                d
                for d in documents
                if not d.document_id or d.document_id not in processed_ids
            ]
            skipped = original_count - len(documents)

        if batch_size is not None:
            documents = documents[:batch_size]

        # Build index for original document positions
        doc_indices = {id(doc): i for i, doc in enumerate(documents)}

        # Check for duplicate document_ids
        seen_doc_ids: dict[str, int] = {}
        duplicates: list[str] = []
        for doc in documents:
            if doc.document_id:
                if doc.document_id in seen_doc_ids:
                    duplicates.append(doc.document_id)
                else:
                    seen_doc_ids[doc.document_id] = doc_indices[id(doc)]
        if duplicates:
            raise ValueError(f"Duplicate document_id values: {sorted(set(duplicates))}")

        # Resolve default organization and validate references
        resolved_orgs: dict[int, str] = {}
        for doc in documents:
            org = doc.organization_identifier
            if not org:
                if not self._default_organization:
                    raise ValueError(
                        f"Document at index {doc_indices[id(doc)]} has no "
                        f"organization_identifier and no default_organization set"
                    )
                org = self._default_organization
            resolved_orgs[id(doc)] = org

            key = (IDENTIFIER_SYSTEM, doc.patient_identifier)
            if key not in self._id_map:
                raise ValueError(
                    f"Document at index {doc_indices[id(doc)]} references unknown "
                    f"patient '{doc.patient_identifier}'"
                )
            key = (ORGANIZATION_IDENTIFIER_SYSTEM, org)
            if key not in self._id_map:
                raise ValueError(
                    f"Document at index {doc_indices[id(doc)]} references unknown "
                    f"organization '{org}'"
                )
            if doc.practitioner_identifier:
                if doc.practitioner_identifier not in self._practitioner_names:
                    raise ValueError(
                        f"Document at index {doc_indices[id(doc)]} references unknown "
                        f"practitioner '{doc.practitioner_identifier}'"
                    )

        # Group by patient and sort by date
        by_patient: dict[str, list[Document]] = defaultdict(list)
        for doc in documents:
            by_patient[doc.patient_identifier].append(doc)

        for patient_id, docs in by_patient.items():
            sorted_docs = sorted(docs, key=lambda d: d.date)
            if sorted_docs != docs:
                logger.info(f"Reordered documents for patient '{patient_id}' by date")
            by_patient[patient_id] = sorted_docs

        # Verify all patients still exist on FHIR before spending on extraction
        for patient_id in by_patient:
            fhir_id = self._resolve_id(IDENTIFIER_SYSTEM, patient_id)
            try:
                self._fhir.get_patient(fhir_id)
            except PatientNotFoundError:
                raise RuntimeError(
                    f"Patient '{patient_id}' (FHIR id {fhir_id}) no longer exists "
                    f"on the FHIR server — re-run seed() to restore it"
                ) from None

        n_patients = len(by_patient)
        patient_label = "patient" if n_patients == 1 else "patients"
        parts = [
            f"Extracting {len(documents)} documents across {n_patients} {patient_label}"
        ]
        if skipped:
            parts.append(f"{skipped} skipped")
        if batch_size is not None:
            parts.append(f"batch_size={batch_size}")
        logger.info(" | ".join(parts))

        self._phase = _Phase.EXTRACTING

        # A 401 (bad/expired key) or an exhausted 503 (LLM Gateway down) is a
        # run-global condition: every remaining document would fail the same
        # way. The first such failure trips this event; workers drain
        # cooperatively and extract() re-raises the recorded exception.
        # Aborting is safe — failed documents persist nothing, and
        # skip_processed=True resumes the run.
        abort_event = threading.Event()
        abort_lock = threading.Lock()
        abort_exc: list[Exception] = []

        def trip_abort(exc: Exception) -> None:
            with abort_lock:
                if not abort_exc:
                    abort_exc.append(exc)
                    abort_event.set()

        # Process patients concurrently, limited by max_concurrency
        def process_patient(
            patient_identifier: str, docs: list[Document]
        ) -> list[IngestionOutcome]:
            outcomes = []
            # Per-patient chronology watermark: the newest already-persisted
            # document date. Fail-open — a guard must not take down ingestion.
            patient_fhir_id = self._resolve_id(IDENTIFIER_SYSTEM, patient_identifier)
            try:
                watermark = self._fhir.get_latest_document_date(patient_fhir_id)
            except Exception as e:
                logger.warning(
                    f"Failed to fetch latest document date for patient "
                    f"'{patient_identifier}' (chronology check disabled): {e}"
                )
                watermark = None
            for i, doc in enumerate(docs):
                if abort_event.is_set():
                    break
                outcome = self._process_single_document(
                    doc,
                    doc_indices[id(doc)],
                    resolved_orgs[id(doc)],
                    watermark=watermark,
                    on_fatal=trip_abort,
                )
                outcomes.append(outcome)
                if outcome.success:
                    watermark = max(watermark or "", str(doc.date)) or None
                else:
                    if abort_event.is_set():
                        # The run is over; no cascade outcomes for this patient.
                        break
                    remaining = len(docs) - i - 1
                    if remaining:
                        logger.warning(
                            f"Skipping {remaining} remaining document(s) for "
                            f"patient '{patient_identifier}' due to failure"
                        )
                    # Skip remaining docs for this patient
                    for remaining_doc in docs[i + 1 :]:
                        skip_msg = f"Skipped due to earlier failure: {outcome.error}"
                        outcomes.append(
                            IngestionOutcome(
                                success=False,
                                patient_identifier=patient_identifier,
                                document_index=doc_indices[id(remaining_doc)],
                                error=skip_msg,
                                document_id=remaining_doc.document_id,
                            )
                        )
                    break
            return outcomes

        def run_batches(
            patient_docs: dict[str, list[Document]],
        ) -> list[IngestionOutcome]:
            results: list[IngestionOutcome] = []
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self._max_concurrency
            ) as executor:
                futures = {
                    executor.submit(process_patient, pid, docs): pid
                    for pid, docs in patient_docs.items()
                }
                for future in concurrent.futures.as_completed(futures):
                    results.extend(future.result())
            return results

        all_outcomes = run_batches(by_patient)

        if abort_event.is_set():
            # Count what did complete, then surface the run-global failure.
            for outcome in all_outcomes:
                if outcome.success:
                    self._documents_processed += 1
                    if outcome.extract_result and outcome.extract_result.usage:
                        self._total_cost += outcome.extract_result.usage.estimated_cost
                else:
                    self._documents_failed += 1
            logger.error(f"Run aborted: {abort_exc[0]}")
            raise abort_exc[0]

        # Deferred retry: a transient failure (timeout/5xx) shouldn't permanently lose a
        # patient's remaining docs. Re-run every failed doc of any patient that hit a
        # transient failure, once, in date order — context is re-fetched fresh, so this
        # is safe (a failed document persists nothing). Deterministic failures (rejected
        # bundles) are not retried.
        transient_patients = {o.patient_identifier for o in all_outcomes if o.transient}
        if transient_patients:
            deferred: dict[str, list[Document]] = defaultdict(list)
            for o in all_outcomes:
                if not o.success and o.patient_identifier in transient_patients:
                    deferred[o.patient_identifier].append(documents[o.document_index])
            for pid in deferred:
                deferred[pid] = sorted(deferred[pid], key=lambda d: d.date)
            n_docs = sum(len(v) for v in deferred.values())
            logger.info(
                f"Deferred retry: re-running {n_docs} document(s) across "
                f"{len(deferred)} patient(s) after transient failures"
            )
            retry_outcomes = run_batches(deferred)
            # Merge: a re-run doc's new outcome replaces its earlier failure by index.
            merged = {o.document_index: o for o in all_outcomes}
            for o in retry_outcomes:
                merged[o.document_index] = o
            all_outcomes = list(merged.values())

            # A run-global failure can also strike during the deferred pass.
            if abort_event.is_set():
                for outcome in all_outcomes:
                    if outcome.success:
                        self._documents_processed += 1
                        if outcome.extract_result and outcome.extract_result.usage:
                            self._total_cost += (
                                outcome.extract_result.usage.estimated_cost
                            )
                    else:
                        self._documents_failed += 1
                logger.error(f"Run aborted: {abort_exc[0]}")
                raise abort_exc[0]

        # Tally counters from the final, merged outcomes.
        for outcome in all_outcomes:
            if outcome.success:
                self._documents_processed += 1
                if outcome.extract_result and outcome.extract_result.usage:
                    self._total_cost += outcome.extract_result.usage.estimated_cost
            else:
                self._documents_failed += 1

        return all_outcomes

    def _process_single_document(
        self,
        doc: Document,
        doc_index: int,
        organization_identifier: str,
        watermark: str | None = None,
        on_fatal: "Callable[[Exception], None] | None" = None,
    ) -> IngestionOutcome:
        """Process a single document: fetch context, extract, match, persist.

        Transient failures (timeouts, connection drops, server 5xx) are retried in
        place with backoff — a fresh call after a slow one is usually fast, so one blip
        doesn't cascade-skip the rest of the patient. Deterministic failures (a rejected
        bundle, a 4xx) fail fast without retrying.

        ``watermark`` is the patient's newest already-persisted document date;
        a strictly older document is processed anyway but flagged out-of-order
        and its bundle passes the update guard before persisting. ``on_fatal``
        is invoked with the exception when the failure is run-global (401, or
        503 after exhausted retries).
        """
        patient_fhir_id = self._resolve_id(IDENTIFIER_SYSTEM, doc.patient_identifier)
        label = doc.document_id or f"doc[{doc_index}]"

        out_of_order = watermark is not None and str(doc.date) < watermark
        if out_of_order:
            logger.warning(
                f"{label} dated {doc.date} is older than the newest persisted "
                f"document ({watermark}) for patient '{doc.patient_identifier}' "
                f"— extracting anyway with the update guard applied"
            )

        for attempt in range(1, _DOC_MAX_ATTEMPTS + 1):
            try:
                # Resolve org and optionally practitioner
                org_fhir_id = self._resolve_id(
                    ORGANIZATION_IDENTIFIER_SYSTEM, organization_identifier
                )
                practitioner_fhir_id = None
                if doc.practitioner_identifier:
                    practitioner_fhir_id = self._resolve_id(
                        PRACTITIONER_IDENTIFIER_SYSTEM, doc.practitioner_identifier
                    )

                # Fetch clinical context only (no Patient/Organization)
                context = self._fhir.fetch_patient_context(patient_fhir_id)

                # Build meta — inject document date and attending practitioner
                date_line = f"Document date: {doc.date}"
                meta = f"{date_line}\n{doc.meta}" if doc.meta else date_line
                if doc.practitioner_identifier:
                    prac_name = self._practitioner_names[doc.practitioner_identifier]
                    attending_line = (
                        f"\nAttending: {prac_name} ({doc.practitioner_identifier})"
                    )
                    meta = (
                        f"{meta}{attending_line}"
                        if meta
                        else attending_line.lstrip("\n")
                    )

                # Call extraction API — reference IDs as explicit params
                bundle, count, usage = self._api.extract(
                    text=doc.text,
                    context=context if context else None,
                    meta=meta,
                    tier=self._tier,
                    patient_id=patient_fhir_id,
                    organization_id=org_fhir_id,
                    practitioner_id=practitioner_fhir_id,
                    document_identifier=doc.document_id,
                    visit_identifier=doc.visit_id,
                )

                # Match extracted practitioners to seeded ones
                self._resolve_practitioners(bundle, org_fhir_id)

                # Deduplicate and persist to FHIR
                entries = bundle.get("entry", [])
                entries = self._fhir.deduplicate_observations(entries, patient_fhir_id)
                if out_of_order:
                    related_dates = self._fhir.get_related_document_dates(
                        patient_fhir_id
                    )
                    entries, dropped = _apply_update_guard(
                        entries, str(doc.date), related_dates
                    )
                    if dropped:
                        logger.info(
                            f"Update guard dropped {len(dropped)} update(s) from "
                            f"older document {label}: {', '.join(dropped)}"
                        )
                persistence = self._fhir.post_bundle(entries)

                if not persistence.success:
                    # Bundle rejected — deterministic content error, do not retry.
                    logger.error(
                        f"Persist failed for {label} "
                        f"(patient={doc.patient_identifier}): {persistence.errors}"
                    )
                    return IngestionOutcome(
                        success=False,
                        patient_identifier=doc.patient_identifier,
                        document_index=doc_index,
                        error=f"Persist failed: {persistence.errors}",
                        document_id=doc.document_id,
                        out_of_order=out_of_order,
                    )

                result = ExtractResult(
                    bundle=bundle,
                    count=count,
                    patient_id=patient_fhir_id,
                    usage=usage,
                    persistence=persistence,
                )

                return IngestionOutcome(
                    success=True,
                    patient_identifier=doc.patient_identifier,
                    document_index=doc_index,
                    extract_result=result,
                    document_id=doc.document_id,
                    out_of_order=out_of_order,
                )

            except Exception as e:
                transient = _is_transient_error(e)
                if transient and attempt < _DOC_MAX_ATTEMPTS:
                    wait = _DOC_RETRY_BACKOFF * (2 ** (attempt - 1))
                    logger.warning(
                        f"Transient failure on {label} "
                        f"(attempt {attempt}/{_DOC_MAX_ATTEMPTS}), "
                        f"retrying in {wait:.0f}s: {e}"
                    )
                    time.sleep(wait)
                    continue
                # A 401 (any time) or a 503 that survived the in-place retries
                # is run-global: no other document can succeed either.
                if on_fatal is not None and isinstance(
                    e, CavellAuthError | CavellGatewayUnavailableError
                ):
                    on_fatal(e)
                logger.error(
                    f"Extraction failed for {label} "
                    f"(patient={doc.patient_identifier}): {e}"
                )
                return IngestionOutcome(
                    success=False,
                    patient_identifier=doc.patient_identifier,
                    document_index=doc_index,
                    error=str(e),
                    document_id=doc.document_id,
                    transient=transient,
                    out_of_order=out_of_order,
                )

        # Unreachable: the loop always returns or exhausts into the failure branch.
        raise AssertionError("unreachable")

    def _resolve_practitioners(self, bundle: dict, organization_id: str) -> None:
        """Match extracted practitioners to seeded ones and rewrite references.

        1. Find Practitioner entries in the bundle
        2. Deduplicate by identifier, then by family name (case-insensitive)
        3. Search FHIR for matches
        4. Rewrite references for matches, null out for non-matches
        5. Remove all Practitioner entries from the bundle
        """
        entries = bundle.get("entry", [])
        if not entries:
            return

        # Collect practitioner entries and their urn:uuid: fullUrls
        prac_entries: list[dict] = []
        prac_full_urls: list[str] = []
        for entry in entries:
            resource = entry.get("resource", {})
            if resource.get("resourceType") == "Practitioner":
                prac_entries.append(entry)
                prac_full_urls.append(entry.get("fullUrl", ""))

        if not prac_entries:
            return

        # Extract name/identifier info and deduplicate
        # key → (fullUrls, family, given, identifier)
        seen_identifiers: dict[str, list[str]] = {}  # identifier → [fullUrls]
        seen_names: dict[str, list[str]] = {}  # "family|given" → [fullUrls]
        prac_info: dict[str, dict] = {}  # dedup_key → {family, given, identifier}

        for entry in prac_entries:
            resource = entry.get("resource", {})
            full_url = entry.get("fullUrl", "")

            # Extract identifier
            identifiers = resource.get("identifier", [])
            prac_id = identifiers[0]["value"] if identifiers else None

            # Extract name
            names = resource.get("name", [])
            family = names[0].get("family", "") if names else ""
            given_list = names[0].get("given", []) if names else []
            given = given_list[0] if given_list else ""

            # Deduplicate: identifier takes priority
            if prac_id:
                if prac_id in seen_identifiers:
                    seen_identifiers[prac_id].append(full_url)
                    continue
                seen_identifiers[prac_id] = [full_url]
                prac_info[f"id:{prac_id}"] = {
                    "family": family,
                    "given": given,
                    "identifier": prac_id,
                }
            else:
                name_key = f"{family.lower()}|{given.lower()}"
                if name_key in seen_names:
                    seen_names[name_key].append(full_url)
                    continue
                seen_names[name_key] = [full_url]
                prac_info[f"name:{name_key}"] = {
                    "family": family,
                    "given": given,
                    "identifier": None,
                }

        # Search FHIR for each unique practitioner
        # mapping: urn:uuid: fullUrl → Practitioner/{fhir-id}
        url_to_ref: dict[str, str | None] = {}

        for dedup_key, info in prac_info.items():
            prac_id = info["identifier"]
            family = info["family"]
            given = info["given"]

            if prac_id:
                matches = self._fhir.search_practitioners(identifier=prac_id)
                full_urls = seen_identifiers[prac_id]
            elif not family and not given:
                # No identifier and no name — skip match
                name_key = f"{family.lower()}|{given.lower()}"
                full_urls = seen_names[name_key]
                logger.warning(
                    f"Practitioner {full_urls} has no identifier or name, "
                    "skipping match"
                )
                for fu in full_urls:
                    url_to_ref[fu] = None
                continue
            else:
                matches = self._fhir.search_practitioners(
                    family_name=family,
                    given_name=given,
                    organization_id=organization_id,
                )
                name_key = f"{family.lower()}|{given.lower()}"
                full_urls = seen_names[name_key]

            if len(matches) == 1:
                ref = f"Practitioner/{matches[0]['id']}"
                for fu in full_urls:
                    url_to_ref[fu] = ref
            else:
                if len(matches) == 0:
                    logger.warning(f"No practitioner match for {dedup_key}")
                else:
                    logger.warning(
                        f"Ambiguous practitioner match for {dedup_key}: "
                        f"{len(matches)} results"
                    )
                for fu in full_urls:
                    url_to_ref[fu] = None

        # Rewrite references in non-Practitioner entries
        for entry in entries:
            resource = entry.get("resource", {})
            if resource.get("resourceType") == "Practitioner":
                continue
            self._rewrite_practitioner_refs(resource, url_to_ref)

        # Remove Practitioner entries from bundle
        bundle["entry"] = [
            e
            for e in entries
            if e.get("resource", {}).get("resourceType") != "Practitioner"
        ]

    @staticmethod
    def _rewrite_practitioner_refs(
        obj: dict | list, url_to_ref: dict[str, str | None]
    ) -> None:
        """Walk a resource and rewrite or remove practitioner references."""
        if isinstance(obj, dict):
            if "reference" in obj and obj["reference"] in url_to_ref:
                new_ref = url_to_ref[obj["reference"]]
                if new_ref:
                    obj["reference"] = new_ref
                else:
                    obj.pop("reference", None)
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    IngestionPipeline._rewrite_practitioner_refs(value, url_to_ref)
            # Remove keys whose values are now empty dicts (cleared references)
            for k in [k for k, v in obj.items() if isinstance(v, dict) and not v]:
                del obj[k]
            # Same for list values: prune items emptied by the recursion, and
            # drop the key when the whole list emptied — HAPI rejects [{}].
            for k in list(obj.keys()):
                v = obj[k]
                if isinstance(v, list):
                    v[:] = [i for i in v if not (isinstance(i, dict) and not i)]
                    if not v:
                        del obj[k]
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    IngestionPipeline._rewrite_practitioner_refs(item, url_to_ref)
            obj[:] = [i for i in obj if not (isinstance(i, dict) and not i)]
