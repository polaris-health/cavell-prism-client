"""Ingestion pipeline for loading data into FHIR."""

import concurrent.futures
import contextlib
import datetime
import enum
import hashlib
import logging
import os
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

import httpx

from cavell_client.api import CavellAPI
from cavell_client.database import UnreadableRow, cell_to_text
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
    FHIRAuthError,
    FHIRConnectionError,
    OutOfOrderDocument,
    PatientNotFoundError,
    parse_usage,
)
from cavell_client.reporting import (
    FailureCategory,
    FailureRecord,
    FailureReporter,
    persist_error_detail,
    truncate,
)

if TYPE_CHECKING:
    from cavell_client.client import CavellClient
    from cavell_client.database import DatabaseSource

logger = logging.getLogger(__name__)

# Per-document in-place retry for transient failures (timeouts, 5xx, connection
# errors). A fresh extract call after a slow one is usually fast, so retrying keeps
# one blip from failing a document that would otherwise have succeeded.
_DOC_MAX_ATTEMPTS = 3
_DOC_RETRY_BACKOFF = 2.0  # seconds, exponential

# Server 5xx / rate-limit statuses that are worth retrying (content errors are 4xx).
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


def _reject_removed_extract_kwargs(kwargs: dict) -> None:
    """Raise a pointed migration error for extract()'s renamed `batch_size`.

    Silently ignoring `batch_size` would be expensive: the caller expects a
    capped run and would instead extract every document they passed.
    """
    if "batch_size" in kwargs:
        raise TypeError(
            "IngestionPipeline.extract() renamed 'batch_size' to 'limit' in "
            "cavell-prism-client 0.2.0. It caps how many documents this single "
            "call processes — it never chunked the list. Use "
            "extract(documents, limit=N) to cap one call, or "
            "extract_all(documents, batch_size=N) to process every document in "
            "chronological chunks of N. See the CHANGELOG for migration notes."
        )
    if kwargs:
        unexpected = ", ".join(sorted(kwargs))
        raise TypeError(f"Unexpected keyword argument(s): {unexpected}")


#: How many offending items to name before truncating the error message.
_MAX_LISTED_NON_DOCUMENTS = 10

#: Resources per seeding transaction.
_SEED_CHUNK = 500


def _require_documents(documents: list["Document"]) -> None:
    """Reject anything in the list that is not a :class:`Document`.

    Field contents and conversions are ``Document``'s own job — building one is
    what validates and normalizes it. This only checks that the pipeline was
    handed Documents at all: passing raw CSV rows straight through is the
    obvious mistake, and without this it surfaces as an ``AttributeError``
    inside a worker thread, long after the run has started spending.

    Raises:
        TypeError: If any element is not a :class:`Document`.
    """
    wrong_type = [
        f"index {i}: got {type(doc).__name__}"
        for i, doc in enumerate(documents)
        if not isinstance(doc, Document)
    ]
    if not wrong_type:
        return

    listed = "\n".join(f"  - {w}" for w in wrong_type[:_MAX_LISTED_NON_DOCUMENTS])
    if len(wrong_type) > _MAX_LISTED_NON_DOCUMENTS:
        listed += f"\n  ... and {len(wrong_type) - _MAX_LISTED_NON_DOCUMENTS} more"
    raise TypeError(
        f"{len(wrong_type)} of {len(documents)} item(s) are not Document "
        f"objects. Build them with Document(...) or "
        f"Document.from_rows(...):\n{listed}"
    )


def _dedupe_documents_by_content(
    documents: list["Document"],
    seen: "set[tuple[str, str, bytes]] | None" = None,
) -> tuple[list["Document"], int]:
    """Drop documents that repeat an earlier one's content verbatim.

    Source exports can carry the same note twice under different
    ``document_id`` values (multi-feed merges, amended-note re-exports). The
    resume-skip keys on ``document_id``, so those copies look like new
    documents and get extracted again — producing a second DocumentReference
    and set of clinical resources (and a second pass over the Encounter, when
    ``encounter_id`` is set) for one real event.

    Identity is ``(patient_identifier, date, text)``: same patient, same day,
    byte-identical note. Text is hashed so the key stays small on large
    corpora. Dates are kept in the key because the same text on two different
    days is copy-forward documentation of two real encounters, not a
    duplicate. The first occurrence in the caller's order wins.

    Args:
        documents: Documents in the caller's order.
        seen: Keys of documents already kept by an earlier call, updated in
            place — lets a batched run dedupe across its batches.

    Returns:
        ``(kept, dropped_count)``.
    """
    if seen is None:
        seen = set()
    kept: list[Document] = []
    dropped = 0
    for doc in documents:
        digest = hashlib.sha256(doc.text.strip().encode("utf-8")).digest()
        key = (doc.patient_identifier, str(doc.date), digest)
        if key in seen:
            dropped += 1
            logger.debug(
                f"Duplicate content: skipping {doc.document_id} "
                f"(patient {doc.patient_identifier}, {doc.date})"
            )
            continue
        seen.add(key)
        kept.append(doc)
    return kept, dropped


def _first_positions(documents: "list[Document]") -> dict[tuple[str, str], int]:
    """``(patient, document_id)`` -> position of its first occurrence — the
    row named in failure reports (later repeats are rejected duplicates)."""
    positions: dict[tuple[str, str], int] = {}
    for i, d in enumerate(documents):
        positions.setdefault((d.patient_identifier, d.document_id), i)
    return positions


def _is_transient_error(exc: Exception) -> bool:
    """True for failures worth retrying: timeouts, connection drops, server 5xx/429.

    Deterministic failures (a rejected bundle, a 4xx content error) are NOT transient —
    retrying reproduces them, so they fail fast as before.
    """
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    return isinstance(exc, CavellAPIError) and exc.status_code in _TRANSIENT_STATUS


#: Where in a document's processing a failure happened, for the failure
#: report. Everything but "extract" talks to your FHIR server.
_STEP_DESCRIPTIONS = {
    "lookup": "looking up the document's references on the FHIR server",
    "context": "fetching the patient's existing record from the FHIR server",
    "extract": "calling the Prism extraction API",
    "practitioners": "matching extracted practitioners on the FHIR server",
    "persist": "writing the extracted resources to the FHIR server",
}


def _http_detail(response: httpx.Response) -> str:
    """The human-readable part of an error response: OperationOutcome
    diagnostics for a FHIR server, ``detail`` for the API, else the body."""
    try:
        body = response.json()
    except ValueError:
        return truncate(response.text.strip() or f"HTTP {response.status_code}")
    if isinstance(body, dict):
        if body.get("resourceType") == "OperationOutcome":
            issues = [
                i.get("diagnostics") or (i.get("details") or {}).get("text") or ""
                for i in body.get("issue", [])
            ]
            text = "; ".join(i for i in issues if i)
            if text:
                return truncate(text)
        if body.get("detail"):
            return truncate(str(body["detail"]))
    return truncate(str(body))


def _fhir_infra_status(status: int) -> bool:
    """A FHIR server status that says the server, not the request, is the
    problem: down, overloaded, timing out or refusing the credentials."""
    return status >= 500 or status in (401, 403, 408, 429)


def _classify_failure(
    exc: Exception, step: str
) -> tuple[FailureCategory, int | None, str]:
    """Who has to act on ``exc`` raised during ``step``, plus the HTTP status
    and the detail worth showing.

    The Prism API raises :class:`CavellAPIError`; the FHIR client lets
    ``httpx`` errors through, so for transport errors the *step* says which
    side was being talked to.
    """
    if isinstance(exc, CavellAuthError | CavellGatewayUnavailableError):
        return "run_aborted", exc.status_code, truncate(str(exc.message))
    if isinstance(exc, CavellAPIError):
        category: FailureCategory = (
            "prism_unavailable"
            if exc.status_code in _TRANSIENT_STATUS
            else "prism_rejected"
        )
        return category, exc.status_code, truncate(str(exc.message))
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        detail = _http_detail(exc.response)
        if step == "extract":
            category = (
                "prism_unavailable" if status in _TRANSIENT_STATUS else "prism_rejected"
            )
        elif _fhir_infra_status(status):
            category = "fhir_unavailable"
        else:
            category = "fhir_rejected"
        return category, status, detail
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        category = "prism_unavailable" if step == "extract" else "fhir_unavailable"
        return category, None, f"{type(exc).__name__}: {exc}"
    if isinstance(exc, FHIRConnectionError | FHIRAuthError):
        return "fhir_unavailable", None, str(exc)
    if isinstance(exc, PatientNotFoundError):
        return "reference", None, str(exc)
    return "client_error", None, f"{type(exc).__name__}: {exc}"


def _apply_update_guard(
    entries: list[dict], doc_date: str, related_dates: dict[str, str]
) -> tuple[list[dict], list[str]]:
    """Drop updates to resources whose current version came from a newer document.

    .. note::
       **Currently unwired.** Reverse-chronological documents now reach the
       persist step, but the extraction API is told which resources postdate
       them (``future_context``) and decides for itself what to update. Every
       resource on that side has provenance newer than the document, so this
       guard would drop *every* PUT against them — vetoing the reconciliation
       the API was asked to make rather than protecting anything. It is kept —
       defined and unit-tested — as the fallback if the API turns out to emit
       unsafe updates: re-enable the commented-out block in
       :meth:`IngestionPipeline._process_single_document`.


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
    _require_available_columns(columns, set(rows[0].keys()))


def _require_available_columns(
    columns: Mapping[str, str | None], available: set[str]
) -> None:
    """Raise if a mapped column name is not among ``available``."""
    for field_name, col_name in columns.items():
        if col_name is not None and col_name not in available:
            raise ValueError(
                f"Column '{col_name}' (mapped from '{field_name}') "
                f"not found in rows. Available columns: {sorted(available)}"
            )


#: The ``columns`` a notes table is read with when none are given: the
#: example notes CSV's column names (``docs/notebooks/notes.csv``).
DEFAULT_NOTE_COLUMNS: "Mapping[str, str | dict[str, str]]" = MappingProxyType(
    {
        "document_id": "note_id",
        "patient_identifier": "patient_id",
        "date": "note_date",
        "text": "note_text",
        "encounter_id": "encounter_id",
        "practitioner_identifier": "practitioner_id",
        "meta": {"Department": "department"},
    }
)

#: The ``patient_columns`` used alongside :data:`DEFAULT_NOTE_COLUMNS`.
DEFAULT_PATIENT_COLUMNS: Mapping[str, str] = MappingProxyType(
    {"name": "patient_name", "birth_date": "birth_date", "gender": "gender"}
)


def _require_default_columns(
    expected: Iterable[str], available: Iterable[str], argument: str
) -> None:
    """With the default mapping, every column it reads must be in the table
    under exactly that name.

    Raises:
        ValueError: Naming each column not found (and a same-name column in
            another case, if there is one) and the table's columns no
            default refers to — usually the renamed ones.
    """
    available = list(available)
    expected = list(dict.fromkeys(expected))
    missing = [c for c in expected if c not in available]
    if not missing:
        return
    by_lower = {c.lower(): c for c in available}

    def describe(column: str) -> str:
        other = by_lower.get(column.lower())
        return f"'{column}'" + (f" (the table has '{other}')" if other else "")

    not_found = ", ".join(describe(c) for c in missing)
    unmatched = [c for c in available if c not in expected]
    raise ValueError(
        f"No {argument}= given, so the table must have the default columns "
        f"{expected}. Not found: {not_found}. Table columns matching no "
        f"default: {unmatched or 'none'}. Pass {argument}= to map your own "
        f"column names, or rename them with aliases in query=."
    )


def _forbid_key_defaults(defaults: Mapping[str, Any], names: Iterable[str]) -> None:
    """A database source's key columns identify each row; a literal default
    for one would give every row the same key."""
    for name in names:
        if name in defaults:
            raise ValueError(
                f"'{name}' cannot be a default for a database source — it "
                f"identifies the row and must come from a column"
            )


def _normalize_document_date(value: Any) -> str:
    """A document date as ``YYYY-MM-DD``.

    Accepts a ``date``/``datetime`` or an ISO string — a plain date
    (``2024-01-15``, ``20240115``) or a datetime (``2024-01-15T09:30:00``,
    ``2024-01-15 09:30:00+02:00``). A datetime keeps only its date **as
    written**: it is not converted to UTC first, so a note timestamped late in
    the evening in its own timezone stays on that day.

    Normalizes rather than just validates: every chronology decision
    downstream is a lexicographic string comparison.

    Raises:
        ValueError: If the value is not a recognizable ISO date or datetime.
    """
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        try:
            return datetime.date.fromisoformat(text).isoformat()
        except ValueError:
            pass
        try:
            return datetime.datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            pass
    raise ValueError(
        f"Invalid date format '{value}', expected an ISO date (YYYY-MM-DD) "
        "or ISO datetime (YYYY-MM-DDTHH:MM:SS)"
    )


def _cell(value: Any) -> Any:
    """A raw row value with the "no value" spellings collapsed to ``None``.

    Rows come from csv.DictReader (strings), dataframes (NaN for a blank
    cell, floats for numeric ids) or a database; anything but a string or a
    date is spelled the way a CSV export would hold it.
    """
    if value is None or isinstance(value, str | datetime.date):
        return value
    return cell_to_text(value) or None


#: Document fields a ``columns`` mapping must give a real column for (their
#: *values* are validated by ``Document.__post_init__``).
_REQUIRED_DOCUMENT_FIELDS = ("text", "patient_identifier", "date", "document_id")


@dataclass(frozen=True)
class _DocumentColumns:
    """A validated ``columns`` mapping (plus defaults) for Document rows —
    shared by :meth:`Document.from_rows` and the database pipeline so both
    map and validate rows identically."""

    #: Document field -> column, for the plain (non-``meta``) mappings.
    flat: dict[str, str]
    #: Label -> column for a dict ``meta`` mapping, if one was given.
    meta: dict[str, str] | None
    defaults: Mapping[str, Any]

    @classmethod
    def parse(
        cls,
        columns: "Mapping[str, str | dict[str, str] | None]",
        defaults: Mapping[str, Any],
    ) -> "_DocumentColumns":
        """Validate the mapping. Mistakes here are configuration errors —
        nothing could be built — so they raise, unlike row problems.

        Raises:
            ValueError: On an unknown field, or a required field not mapped.
        """
        valid_fields = {f.name for f in fields(Document)}
        # `not ...get()` rather than `not in`: mapping a field to None is how
        # callers disable an *optional* one, so `document_id=None` needs to
        # say why it cannot be disabled.
        for required in _REQUIRED_DOCUMENT_FIELDS:
            if not columns.get(required):
                raise ValueError(
                    f"columns must include '{required}' — it is a required "
                    f"Document field and cannot be disabled"
                )
        for where, keys in (("columns", columns), ("defaults", defaults)):
            for key in keys:
                if key not in valid_fields:
                    hint = (
                        " ('visit_id' was renamed to 'encounter_id' in "
                        "cavell-prism-client 0.8.0)"
                        if key == "visit_id"
                        else ""
                    )
                    raise ValueError(
                        f"Unknown Document field in {where}: '{key}'{hint}"
                    )
        meta = columns.get("meta")
        return cls(
            flat={k: v for k, v in columns.items() if isinstance(v, str)},
            meta=meta if isinstance(meta, dict) else None,
            defaults=defaults,
        )

    def all_columns(self) -> dict[str, str]:
        """Every mapped column, ``meta`` ones labelled ``meta[label]``."""
        meta = {f"meta[{label}]": col for label, col in (self.meta or {}).items()}
        return {**self.flat, **meta}

    def build(self, row: Mapping[str, Any]) -> "Document":
        """One Document from a row.

        Raises:
            ValueError: If the row's content is unusable (blank required
                value, unparseable date) — callers turn it into a rejection.
        """
        kwargs: dict[str, Any] = {}
        if self.meta is not None:
            parts = [
                f"{label}: {val}"
                for label, col in self.meta.items()
                if (val := _cell(row.get(col)))
            ]
            kwargs["meta"] = "\n".join(parts) or None
        for name, col in self.flat.items():
            value = _cell(row.get(col))
            if name in _REQUIRED_DOCUMENT_FIELDS:
                kwargs[name] = "" if value is None else value
            else:
                kwargs[name] = value or None
        kwargs.update(self.defaults)
        return Document(**kwargs)

    def value(self, row: Mapping[str, Any], name: str) -> Any:
        """One field of a row as :meth:`build` passes it to the Document
        (``None`` when blank/unmapped)."""
        if name in self.defaults:
            return self.defaults[name]
        if name in self.flat:
            return _cell(row.get(self.flat[name])) or None
        return None

    def text(self, row: Mapping[str, Any], name: str) -> str:
        """One field of a row as stripped text (``""`` when blank/unmapped)."""
        value = self.value(row, name)
        return "" if value is None else str(value).strip()

    def rejection(
        self,
        row: Mapping[str, Any],
        index: int,
        reason: str,
        category: 'Literal["data", "reference"]' = "data",
    ) -> "DocumentRejection":
        """A rejection naming the row by its id, patient and date."""
        return DocumentRejection(
            index=index,
            document_id=self.text(row, "document_id") or None,
            patient_identifier=self.text(row, "patient_identifier") or None,
            reason=reason,
            category=category,
            date=self.text(row, "date") or None,
        )


@dataclass(frozen=True)
class DocumentRejection:
    """One document row that was skipped before extraction, and why.

    Produced for rows whose content is unusable (``category="data"``: blank
    text or id, unparseable date, a ``document_id`` repeated for the same
    patient) and for documents pointing at something that does not exist
    (``category="reference"``: unknown patient, organization or practitioner).
    Nothing is sent for a rejected row; fix it in the source and it is picked
    up on the next run.
    """

    #: 0-based position of the row in the input (the rows passed to
    #: ``from_rows``, the documents passed to ``extract``/``extract_all``, or
    #: the database query result).
    index: int | None
    document_id: str | None
    patient_identifier: str | None
    reason: str
    category: Literal["data", "reference"] = "data"
    date: str | None = None

    def __str__(self) -> str:
        return self.to_failure().log_line()

    def to_failure(self) -> FailureRecord:
        return FailureRecord(
            kind="document",
            identifier=self.document_id,
            patient_identifier=self.patient_identifier,
            status="rejected",
            category=self.category,
            reason=self.reason,
            date=self.date,
            row=self.index,
        )

    def to_outcome(self) -> "IngestionOutcome":
        """This rejection as the outcome ``extract``/``extract_all`` return."""
        return IngestionOutcome(
            success=False,
            patient_identifier=self.patient_identifier or "",
            document_index=self.index or 0,
            error=self.reason,
            document_id=self.document_id,
            rejected=True,
            category=self.category,
            date=self.date,
        )


class DocumentList(list["Document"]):
    """A list of :class:`Document` that also remembers the rows it skipped.

    Returned by :meth:`Document.from_rows`. It *is* a plain list of
    documents — index it, slice it, pass it to ``extract_all`` — with one
    extra attribute, :attr:`rejected`, listing every row that could not
    become a Document and why. ``extract_all`` reports those rejections in
    its end-of-run summary and failures file.
    """

    def __init__(
        self,
        documents: Iterable["Document"] = (),
        rejected: list[DocumentRejection] | None = None,
    ) -> None:
        super().__init__(documents)
        self.rejected: list[DocumentRejection] = list(rejected or [])


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
    """Extra context (e.g. department). Do NOT include the practitioner — the
    pipeline injects that automatically — nor the date, which travels as its
    own payload field built from :attr:`date`."""
    practitioner_identifier: str | None = None
    #: Your identifier for this document, stamped on the DocumentReference.
    #: Required, and keyword-only so it can stay in place without shifting the
    #: positional arguments around it. Everything that makes re-running safe
    #: keys on it: the resume filter skips documents whose id is already in
    #: FHIR, the chronology watermark is read off persisted document
    #: identifiers, and failures are reported by id rather than by a
    #: batch-relative index.
    document_id: str = field(kw_only=True)
    #: Your identifier for the visit/admission this document belongs to. When
    #: set, the pipeline looks the Encounter up in FHIR
    #: (``urn:cavell:encounter``), sends it to the extraction API so the API
    #: updates it in place rather than creating a second one, and every
    #: resource extracted from this document references it. When ``None``, no
    #: Encounter is created for the document.
    encounter_id: str | None = None

    @classmethod
    def from_rows(
        cls,
        rows: "list[dict[str, Any]]",
        columns: "Mapping[str, str | dict[str, str] | None]",
        **defaults: Any,
    ) -> "DocumentList":
        """Build Documents from CSV/dict rows, skipping and reporting bad rows.

        Args:
            rows: List of dicts — ``csv.DictReader``, ``df.to_dict("records")``
                or :meth:`DatabaseSource.rows() <cavell_client.DatabaseSource.rows>`.
            columns: Maps Document field names to column names. ``None`` is how
                you disable an optional field whose column your export does not
                have, so the mapping can be written once with every field in
                it. Must include a real column name for "text",
                "patient_identifier", "date" and "document_id" — those are
                required and cannot be disabled that way.
            **defaults: Literal values applied to every document
                (e.g. organization_identifier).

        Returns:
            A :class:`DocumentList` — a plain list of Documents, one per usable
            row, whose ``rejected`` attribute lists every skipped row with its
            position and the reason. A row is skipped (and logged at WARNING)
            when ``text``, ``patient_identifier`` or ``document_id`` is blank,
            when ``date`` is not an ISO date/datetime, or when its
            ``document_id`` repeats an earlier row's for the same patient (the
            first occurrence is kept). One bad row never stops the others.

        Raises:
            ValueError: On a *mapping* mistake — an unknown field, a required
                field not mapped, or a mapped column that is not in the rows.
                Nothing could be built with those, so they fail fast.
        """
        mapping = _DocumentColumns.parse(columns, defaults)
        documents = DocumentList()
        if not rows:
            return documents
        _require_available_columns(mapping.all_columns(), set(rows[0]))

        first_seen: dict[tuple[str, str], int] = {}
        for index, row in enumerate(rows):
            try:
                doc = mapping.build(row)
                key = (doc.patient_identifier, doc.document_id)
                if key in first_seen:
                    raise ValueError(
                        f"duplicate document_id '{doc.document_id}' for patient "
                        f"'{doc.patient_identifier}' (first used by row "
                        f"{first_seen[key]}, which is kept)"
                    )
            except ValueError as e:
                rejection = mapping.rejection(row, index, str(e))
                documents.rejected.append(rejection)
                logger.warning(f"Skipping row {index}: {rejection}")
                continue
            first_seen[key] = index
            documents.append(doc)

        if documents.rejected:
            logger.warning(
                f"Document.from_rows: {len(documents)} document(s) built, "
                f"{len(documents.rejected)} row(s) skipped — see "
                f".rejected for the reasons"
            )
        return documents

    def __post_init__(self):
        for name in ("text", "patient_identifier", "document_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Document.{name} must not be empty")

        stripped = self.text.strip()
        if len(stripped) < 20:
            logger.warning(
                f"Document '{self.document_id}' has short text ({len(stripped)} chars)"
            )

        self.date = _normalize_document_date(self.date)


@dataclass
class IngestionOutcome:
    """Result of processing a single document."""

    success: bool
    patient_identifier: str
    # Position in the list extract() processed — i.e. AFTER skip_processed
    # filtering and the `limit` truncation, not the caller's original list.
    # Under extract_all() this is relative to the document's own batch.
    document_index: int
    extract_result: ExtractResult | None = None
    error: str | None = None
    document_id: str | None = None
    # transient failure (timeout/5xx) — eligible for the deferred retry pass
    transient: bool = False
    # Document was older than the patient's newest already-persisted document,
    # so it was extracted against context split at its own date, with anything
    # newer sent separately as future_context. Independent of `success` — this
    # marks how the document was processed, not whether it worked.
    out_of_order: bool = False
    #: The document was never sent: its row was unusable or pointed at
    #: something that does not exist. Always paired with ``success=False``.
    rejected: bool = False
    #: Who has to act on a failure — ``data``, ``reference``,
    #: ``prism_rejected``, ``prism_unavailable``, ``fhir_rejected``,
    #: ``fhir_unavailable``, ``run_aborted`` or ``client_error``. ``None`` on
    #: success. See :data:`cavell_client.reporting.FailureCategory`.
    category: FailureCategory | None = None
    #: The underlying detail behind ``error``: the API's message, the FHIR
    #: server's OperationOutcome diagnostics, the exception text.
    detail: str | None = None
    #: HTTP status of the failing response, when there was one.
    http_status: int | None = None
    #: Extraction attempts made in this run, in-place retries included.
    attempts: int = 0
    #: The document's date (``YYYY-MM-DD``).
    date: str | None = None

    def __str__(self) -> str:
        label = self.document_id or f"doc[{self.document_index}]"
        marker = " [out-of-order]" if self.out_of_order else ""
        if not self.success:
            transient = " [transient]" if self.transient else ""
            status = "REJECTED" if self.rejected else "FAILED"
            category = f" [{self.category}]" if self.category else ""
            return f"  {label} {status}{category}{transient}{marker}: {self.error}"
        r = self.extract_result
        created = r.persistence.created if r and r.persistence else 0
        updated = r.persistence.updated if r and r.persistence else 0
        cost = r.usage.estimated_cost if r and r.usage else 0.0
        count = r.count if r else 0
        partial = " [partial]" if r is not None and r.is_partial else ""
        return (
            f"  {label} -> {count} resources"
            f" ({created} new, {updated} updated)"
            f"  ${cost:.3f}{marker}{partial}"
        )

    def to_failure(self, row: int | None = None) -> FailureRecord | None:
        """This outcome as a :class:`~cavell_client.reporting.FailureRecord`,
        or ``None`` when it needs no attention (a complete success)."""
        r = self.extract_result
        if self.success:
            if r is None or not r.is_partial:
                return None
            steps = ", ".join(r.failed_extractors) or "unspecified"
            status, category = "partial", "prism_partial"
            reason = f"extraction completed partially; failed step(s): {steps}"
        else:
            status = "rejected" if self.rejected else "failed"
            category = self.category or "client_error"
            reason = self.error or "unknown error"
        return FailureRecord(
            kind="document",
            identifier=self.document_id,
            patient_identifier=self.patient_identifier,
            status=status,
            category=category,
            reason=reason,
            date=self.date,
            row=row,
            detail=self.detail,
            http_status=self.http_status,
            attempts=self.attempts or None,
            transient=self.transient,
        )


class _RowKey(NamedTuple):
    """One usable row of a database key scan. Field order makes a plain sort
    group repeats of a key, first row first."""

    patient: str
    document: str
    row: int
    date: str

    def rejection(
        self, reason: str, category: 'Literal["data", "reference"]' = "data"
    ) -> "DocumentRejection":
        return DocumentRejection(
            index=self.row,
            document_id=self.document,
            patient_identifier=self.patient,
            reason=reason,
            category=category,
            date=self.date,
        )


@dataclass
class _KeyScan:
    """What the key scan of :meth:`IngestionPipeline.extract_from_database`
    found."""

    #: Rows read.
    rows: int = 0
    #: One key per distinct (patient, document_id), its first row.
    keys: list[_RowKey] = field(default_factory=list)
    #: Each patient's first-seen ``patient_columns`` values.
    demographics: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Rows that cannot be extracted (in row order).
    rejected: list["DocumentRejection"] = field(default_factory=list)
    #: Row -> its reference problem; reported only if the document is pending.
    held: dict[int, "DocumentRejection"] = field(default_factory=dict)


@dataclass
class DatabaseExtractionReport:
    """What :meth:`IngestionPipeline.extract_from_database` did."""

    #: Rows read by the key scan.
    rows_scanned: int = 0
    #: Rows whose document is already in FHIR — skipped without reading text.
    already_extracted: int = 0
    #: Rows that still needed extracting after the scan.
    pending: int = 0
    #: Documents extracted and persisted in this run (partial ones included).
    extracted: int = 0
    #: Of those, extractions where some extraction step failed.
    partial: int = 0
    #: Pending documents skipped because their text repeats another
    #: document's for the same patient and date — pending, or extracted in
    #: an earlier run.
    duplicates_skipped: int = 0
    total_cost: float = 0.0
    #: Documents that were attempted and failed (see each one's
    #: ``category``, ``error`` and ``detail``).
    failed: list[IngestionOutcome] = field(default_factory=list)
    #: Rows that were not sent: unusable content or unresolvable references.
    rejected: list[DocumentRejection] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """True when every pending row was extracted."""
        return not self.failed and not self.rejected

    def __str__(self) -> str:
        return (
            f"{self.rows_scanned:,} rows scanned: {self.already_extracted:,} "
            f"already extracted, {self.extracted:,} extracted now "
            f"({self.partial:,} partial), {len(self.failed):,} failed, "
            f"{len(self.rejected):,} rejected, {self.duplicates_skipped:,} "
            f"duplicate(s) skipped — estimated cost ${self.total_cost:.2f}"
        )


def _check_patient_columns(
    patient_columns: "Mapping[str, str | None] | None",
    patient_defaults: "Mapping[str, Any] | None",
    patient_identifier_column: str,
) -> dict[str, str]:
    """Validate the Patient mapping of a database source.

    Returns:
        Patient field -> column, without ``identifier`` (that is always the
        documents' ``patient_identifier`` column) and without disabled fields.
    """
    valid = {f.name for f in fields(Patient)}
    mapped: dict[str, str] = {}
    for key, col in (patient_columns or {}).items():
        if key not in valid:
            raise ValueError(f"Unknown Patient field in patient_columns: '{key}'")
        if key == "identifier":
            if col != patient_identifier_column:
                raise ValueError(
                    f"patient_columns['identifier'] must be the documents' "
                    f"patient_identifier column '{patient_identifier_column}' "
                    f"(got {col!r}); it can simply be left out"
                )
            continue
        if col is not None:
            mapped[key] = col
    for key in patient_defaults or {}:
        if key not in valid or key == "identifier":
            raise ValueError(f"Unknown Patient field in patient_defaults: '{key}'")
    return mapped


@dataclass
class _RunStats:
    """Document counters: of one run (for its summary line), or of a
    pipeline's whole life (its ``documents_*`` properties)."""

    extracted: int = 0
    partial: int = 0
    failed: int = 0
    rejected: int = 0
    skipped: int = 0
    cost: float = 0.0

    def count(self, outcome: "IngestionOutcome") -> None:
        if outcome.rejected:
            self.rejected += 1
        elif not outcome.success:
            self.failed += 1
        else:
            self.extracted += 1
            r = outcome.extract_result
            if r is not None and r.is_partial:
                self.partial += 1
            if r is not None and r.usage:
                self.cost += r.usage.estimated_cost

    def headline(self, what: str, aborted_by: Exception | None = None) -> str:
        if aborted_by is not None:
            what += " ABORTED"
        text = (
            f"{what}: {self.extracted} extracted ({self.partial} partial), "
            f"{self.skipped} already processed, {self.failed} failed, "
            f"{self.rejected} rejected, estimated cost ${self.cost:.2f}"
        )
        if aborted_by is not None:
            text += f" — aborted by {type(aborted_by).__name__}: {aborted_by}"
        return text


class _Run:
    """One extract()/extract_all()/extract_from_database() call: its
    counters (folded into the pipeline's lifetime ``totals`` as they change)
    and its failure report.

    Use :meth:`summarize` around the run so it always ends with a summary.
    """

    def __init__(
        self,
        name: str,
        failures_path: "str | os.PathLike[str] | None",
        totals: _RunStats,
    ) -> None:
        self.name = name
        self.stats = _RunStats()
        self._totals = totals
        self._reporter = FailureReporter(failures_path)

    def summarize(self) -> "contextlib.AbstractContextManager[None]":
        return self._reporter.summarize(lambda e: self.stats.headline(self.name, e))

    def reject(self, rejections: "list[DocumentRejection]") -> None:
        """Count and report rows rejected before extraction."""
        for stats in (self.stats, self._totals):
            stats.rejected += len(rejections)
        self._reporter.add([r.to_failure() for r in rejections])

    def record(
        self,
        outcomes: "list[IngestionOutcome]",
        row_of: "Mapping[tuple[str, str], int]",
    ) -> None:
        """Count and report final outcomes; ``row_of`` names each one's row."""
        records = []
        for o in outcomes:
            self.stats.count(o)
            self._totals.count(o)
            record = o.to_failure(
                row_of.get((o.patient_identifier, o.document_id or ""))
            )
            if record is not None:
                records.append(record)
        self._reporter.add(records)


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
        self._totals = _RunStats()
        #: The API key passed the pre-flight check; not repeated per batch.
        self._key_checked = False

    @property
    def documents_processed(self) -> int:
        """Total documents successfully extracted across all extract() calls."""
        return self._totals.extracted

    @property
    def documents_failed(self) -> int:
        """Total documents that were attempted and failed across all
        extract() calls (rejections are counted in :attr:`documents_rejected`)."""
        return self._totals.failed

    @property
    def documents_rejected(self) -> int:
        """Total documents rejected before extraction across all extract()
        calls — an unknown patient/organization/practitioner, a missing
        organization, a duplicate ``document_id`` for one patient, or a row
        :meth:`Document.from_rows` or a database scan could not use."""
        return self._totals.rejected

    @property
    def total_cost(self) -> float:
        """Cumulative estimated cost (USD) across all extract() calls."""
        return self._totals.cost

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
        self._seed_chunked(org_resources, "organizations")
        self._seed_chunked(prac_resources, "practitioners")

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
            self._seed_chunked(role_resources, "practitioner roles")

        # Seed patients
        self._seed_chunked(
            [self._patient_resource(patient) for patient in patients], "patients"
        )

        self._phase = _Phase.PATIENTS_SEEDED

    def _seed_chunked(self, resources: list[dict], what: str) -> None:
        """Upsert ``resources`` in transactions of at most :data:`_SEED_CHUNK`
        and record their FHIR ids. A single transaction for every patient of a
        large dataset would exceed what a FHIR server accepts in one request.

        Raises:
            RuntimeError: If a chunk reports errors.
        """
        for start in range(0, len(resources), _SEED_CHUNK):
            chunk = resources[start : start + _SEED_CHUNK]
            result, id_map = self._fhir.seed_bundle(chunk)
            if not result.success:
                raise RuntimeError(f"Failed to seed {what}: {result.errors}")
            self._id_map.update(id_map)

    def _patient_resource(self, patient: Patient) -> dict:
        """The FHIR Patient resource seeded for ``patient``. Its organization
        and practitioners must already be seeded."""
        resource: dict = {
            "resourceType": "Patient",
            "identifier": [{"system": IDENTIFIER_SYSTEM, "value": patient.identifier}],
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
                prac_fhir_id = self._resolve_id(PRACTITIONER_IDENTIFIER_SYSTEM, gp_id)
                gp_refs.append({"reference": f"Practitioner/{prac_fhir_id}"})
            resource["generalPractitioner"] = gp_refs
        return resource

    def _reference_problem(
        self,
        patient_identifier: str,
        organization_identifier: str | None,
        practitioner_identifier: str | None,
        *,
        check_patient: bool = True,
    ) -> tuple[str | None, str | None]:
        """Resolve a document's organization and check its references.

        Returns:
            ``(organization, problem)``: the organization identifier the
            document will be filed under, and ``None`` — or, when a reference
            cannot be used, the reason as text (and the organization may be
            ``None``).
        """
        org = organization_identifier or self._default_organization
        if not org:
            return None, (
                "no organization_identifier, and the pipeline has no "
                "default_organization"
            )
        if (ORGANIZATION_IDENTIFIER_SYSTEM, org) not in self._id_map:
            return org, f"unknown organization '{org}' (not seeded)"
        if (
            check_patient
            and (IDENTIFIER_SYSTEM, patient_identifier) not in self._id_map
        ):
            return org, f"unknown patient '{patient_identifier}' (not seeded)"
        if practitioner_identifier and (
            practitioner_identifier not in self._practitioner_names
        ):
            return org, f"unknown practitioner '{practitioner_identifier}' (not seeded)"
        return org, None

    def _validate_documents(
        self, documents: list[Document]
    ) -> tuple[list[Document], dict[int, str], list[DocumentRejection]]:
        """Check document references and per-patient document_id uniqueness;
        a problem rejects the document, not the call.

        Returns:
            ``(kept, resolved_orgs, rejected)``: the usable documents in input
            order; ``id(doc)`` -> the organization each is filed under (the
            caller keeps the documents alive); and one rejection per refused
            document, indexed by its position in ``documents`` — a repeated
            ``document_id`` for one patient (the first is kept; ``data``), or
            no organization / an unseeded patient, organization or
            practitioner (``reference``).
        """
        kept: list[Document] = []
        resolved_orgs: dict[int, str] = {}
        rejected: list[DocumentRejection] = []
        # Scoped per patient to match the FHIR identity (patient + document
        # identifier); hospital exports commonly restart numbering per patient.
        first_seen: dict[tuple[str, str], int] = {}
        for index, doc in enumerate(documents):
            key = (doc.patient_identifier, doc.document_id)
            category: Literal["data", "reference"] = "data"
            if key in first_seen:
                problem = (
                    f"duplicate document_id '{doc.document_id}' for patient "
                    f"'{doc.patient_identifier}' (first used at index "
                    f"{first_seen[key]}, which is kept)"
                )
            else:
                first_seen[key] = index
                org, problem = self._reference_problem(
                    doc.patient_identifier,
                    doc.organization_identifier,
                    doc.practitioner_identifier,
                )
                category = "reference"
                if problem is None and org is not None:
                    resolved_orgs[id(doc)] = org
                    kept.append(doc)
                    continue
            rejected.append(
                DocumentRejection(
                    index=index,
                    document_id=doc.document_id,
                    patient_identifier=doc.patient_identifier,
                    reason=problem or "no organization",
                    category=category,
                    date=str(doc.date),
                )
            )
        return kept, resolved_orgs, rejected

    def _check_chronology(
        self,
        documents: list[Document],
        known: "dict[str, str | None] | None" = None,
    ) -> "tuple[dict[str, str | None], list[OutOfOrderDocument]]":
        """Find documents older than their patient's newest persisted document.

        These are still extracted — they take the split-context path, where the
        extraction API is shown the patient as of the document's own date and
        receives everything newer as separate ``future_context``. This method
        only identifies them, so the run can be logged and the watermarks
        reused; the decision is made per document in
        :meth:`_process_single_document`.

        Fails **open** per patient: if the watermark cannot be fetched, that
        patient's documents are all treated as in-order with a warning rather
        than taking the run down. A transient FHIR error should not block
        ingestion, and unsplit context is the behaviour they would have had
        anyway.

        Args:
            documents: Documents about to be extracted.
            known: Watermarks already known (from a bulk scan); used — and
                returned — as is, with no FHIR lookups.

        Returns:
            ``(watermarks, out_of_order)``. ``watermarks`` maps patient
            identifier -> watermark (``None`` where unknown) so the caller can
            reuse it instead of re-querying per patient. ``out_of_order`` lists
            one :class:`~cavell_client.models.OutOfOrderDocument` per backdated
            document, whose ``document_index`` is that document's position in
            ``documents``. Equal dates count as in-order — dates are
            day-resolution, so same-day documents have no defined order.
        """
        watermarks: dict[str, str | None] = {} if known is None else known
        if known is None:
            for patient_id in {d.patient_identifier for d in documents}:
                fhir_id = self._id_map.get((IDENTIFIER_SYSTEM, patient_id))
                if fhir_id is None:
                    continue
                try:
                    latest = self._fhir.get_latest_document_date(fhir_id)
                    watermarks[patient_id] = latest
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch latest document date for patient "
                        f"'{patient_id}' (chronology check skipped): {e}"
                    )
                    watermarks[patient_id] = None

        out_of_order: list[OutOfOrderDocument] = []
        for index, doc in enumerate(documents):
            watermark = watermarks.get(doc.patient_identifier)
            if watermark is not None and str(doc.date) < watermark:
                out_of_order.append(
                    OutOfOrderDocument(
                        patient_identifier=doc.patient_identifier,
                        document_id=doc.document_id,
                        document_index=index,
                        date=str(doc.date),
                        watermark=watermark,
                    )
                )
        if out_of_order:
            patients = {v.patient_identifier for v in out_of_order}
            logger.info(
                f"{len(out_of_order)} reverse-chronological document(s) across "
                f"{len(patients)} patient(s) will be extracted against context "
                f"split at their own date"
            )

        return watermarks, out_of_order

    def extract_all(
        self,
        documents: list[Document],
        *,
        batch_size: int | None = None,
        skip_processed: bool = True,
        dedupe_content: bool = True,
        on_batch: "Callable[[list[IngestionOutcome]], None] | None" = None,
        failures_path: "str | os.PathLike[str] | None" = None,
    ) -> "list[IngestionOutcome]":
        """Extract every document, globally date-ordered, in batches.

        This is the entry point for a whole dataset. :meth:`extract` makes a
        single pass and its ``limit`` truncates that pass; this method walks
        the full list, calling :meth:`extract` once per batch.

        Documents are sorted by ascending date across the **entire** dataset
        before batching. Batching an unsorted list splits it by input order, so
        a later batch could carry documents older than what an earlier batch
        already persisted; those are still extracted, but on the slower
        split-context path — an extra provenance query each, plus the
        reconciliation the API runs on them. Sorting first puts every batch
        boundary on a clean
        chronological cut, so each patient's documents reach the API
        oldest-first even when they span several batches, and none of them pay
        for a split that a plain forward pass never needs.

        Batches are cut by index, so the walk always terminates. It does not
        depend on ``skip_processed`` to advance, which means it works with
        ``skip_processed=False`` too.

        A document that cannot be extracted never stops the others. A
        document whose references do not resolve (unknown patient,
        organization or practitioner; no organization) or whose
        ``document_id`` repeats an earlier one for the same patient is
        **rejected** — not sent, reported, and returned as
        ``IngestionOutcome(rejected=True)``. When ``documents`` is the
        :class:`DocumentList` returned by :meth:`Document.from_rows`, the rows
        it skipped are reported too. Every final failure is logged at ERROR
        with its category (who has to act) and the underlying detail, and the
        run ends with a summary.

        Args:
            documents: Every document to process.
            batch_size: Documents per :meth:`extract` call. ``None`` processes
                everything in a single call. Prefer larger batches: each call
                issues one FHIR query per distinct patient *in that batch*, so
                halving the batch size roughly doubles the query overhead.
            skip_processed: Passed to :meth:`extract`, which applies it per
                batch, so an interrupted run resumes on a re-call.
            dedupe_content: Drop documents repeating an earlier one's content
                verbatim — same patient, same date, byte-identical text. The
                resume-skip keys on ``document_id``, so re-exported copies
                under new ids would otherwise each be extracted, duplicating
                that event's resources. Dropped documents get no outcome. Set
                ``False`` to process the list exactly as given.
            on_batch: Called with each batch's outcomes as that batch
                finishes. Use it for progress output or to persist partial
                results — a large run otherwise holds every outcome, including
                its extracted bundles, in memory until this returns.
            failures_path: Append every final failure, rejection and partial
                extraction to this file as JSON Lines, batch by batch. See
                :class:`~cavell_client.reporting.FailureRecord` for the fields.

        Returns:
            Outcomes for every document processed, in batch order — rejected
            documents first. Note that ``IngestionOutcome.document_index`` is
            relative to the document's own batch, not to ``documents`` (for a
            rejection it is the position in ``documents``). A document older
            than its patient's newest already-persisted document is extracted
            against split context and comes back with ``out_of_order=True``,
            which records how it was processed rather than that it failed.

        Raises:
            TypeError: If any element is not a :class:`Document`.
            ValueError: If ``batch_size`` is less than 1.
            RuntimeError: If called before patients are seeded.
            CavellAuthError: If the API rejects the key.
            CavellGatewayUnavailableError: If the LLM Gateway stays
                unreachable through the in-place retries.
        """
        if batch_size is not None and batch_size < 1:
            raise ValueError(f"batch_size must be >= 1 if set, got {batch_size}")
        if self._phase not in (_Phase.PATIENTS_SEEDED, _Phase.EXTRACTING):
            raise RuntimeError(
                f"extract_all() requires phase 'patients_seeded' or 'extracting', "
                f"currently '{self._phase.value}'"
            )
        run = _Run("extract_all", failures_path, self._totals)
        with run.summarize():
            # Rows Document.from_rows skipped, when given its DocumentList.
            run.reject(list(getattr(documents, "rejected", [])))
            if not documents:
                return []

            # Not redundant with extract()'s own check: validation, deduping
            # and the global sort below all read attributes off these objects.
            _require_documents(documents)
            row_of = _first_positions(documents)

            # Validate everything before the first batch spends. This is also
            # the only place per-patient document_id uniqueness can be enforced
            # across the whole dataset — extract() sees one batch at a time.
            documents, _, rejected = self._validate_documents(list(documents))
            run.reject(rejected)
            all_outcomes = [r.to_outcome() for r in rejected]
            if rejected and on_batch is not None:
                on_batch(list(all_outcomes))

            # Collapse re-exported copies before anything spends on them, for
            # the same reason: a duplicate pair split across two batches is
            # only visible to the pass that sees the whole dataset.
            if dedupe_content:
                documents, duplicates = _dedupe_documents_by_content(documents)
                if duplicates:
                    logger.warning(
                        f"extract_all: skipping {duplicates} document(s) whose "
                        f"content repeats an earlier document for the same "
                        f"patient and date"
                    )
            if not documents:
                return all_outcomes

            # Stable, so same-day documents keep the caller's order. The global
            # sort is also what makes the per-batch chronology check
            # sufficient: a patient's reverse-chronological documents land in
            # an earlier batch than their forward-dated ones.
            ordered = sorted(documents, key=lambda d: d.date)
            size = batch_size or len(ordered)
            n_batches = (len(ordered) + size - 1) // size
            if batch_size is not None:
                logger.info(
                    f"extract_all: {len(ordered)} documents dated "
                    f"{ordered[0].date} to {ordered[-1].date} in {n_batches} "
                    f"batch(es) of up to {batch_size}"
                )
            for n, start in enumerate(range(0, len(ordered), size), 1):
                batch = ordered[start : start + size]
                if batch_size is not None:
                    logger.info(
                        f"extract_all: batch {n}/{n_batches} — {len(batch)} "
                        f"document(s) dated {batch[0].date} to {batch[-1].date}"
                    )
                outcomes = self._extract(
                    batch, skip_processed=skip_processed, run=run, row_of=row_of
                )
                all_outcomes.extend(outcomes)
                if on_batch is not None:
                    on_batch(outcomes)
            return all_outcomes

    def extract(
        self,
        documents: list[Document],
        *,
        skip_processed: bool = True,
        limit: int | None = None,
        failures_path: "str | os.PathLike[str] | None" = None,
        **removed_kwargs: Any,
    ) -> "list[IngestionOutcome]":
        """Phase 3: Extract FHIR resources from documents.

        Processes patients concurrently (limited by max_concurrency),
        documents within each patient sequentially in date order.

        This processes **one** pass over ``documents``; ``limit`` truncates
        that pass rather than chunking it. To walk a large dataset in
        chronological chunks, use :meth:`extract_all`.

        Can be called multiple times. When ``skip_processed=True`` (the
        default), already-processed documents are automatically filtered
        out so re-running is always safe.

        A document older than its patient's newest already-persisted document
        is extracted against **split context**: ``context`` holds the record as
        it stood on that document's own date, and everything newer travels
        separately as ``future_context`` rather than masquerading as history the
        author could have known. Such a document comes back with
        ``out_of_order=True``, which records *how* it was processed, not that it
        failed — it pairs with ``success=True`` on a normal run. The routing is
        per document; every other document in the call is unaffected.

        A document whose references do not resolve, or whose ``document_id``
        repeats an earlier one for the same patient, is rejected rather than
        failing the call — see :meth:`extract_all`. Every final failure is
        logged at ERROR with its category and detail, followed by a summary.

        Args:
            documents: Documents to extract from
            skip_processed: Query FHIR for already-processed document IDs
                and skip them.  Set to ``False`` to process all documents.
            limit: Cap the number of documents processed in this call, taken
                from the front of the list after skip_processed filtering.
                ``None`` processes every document passed.
            failures_path: Append every final failure, rejection and partial
                extraction to this file as JSON Lines.

        Returns:
            List of IngestionOutcome, one per document processed (which is
            fewer than ``documents`` when filtering or ``limit`` applies).
            Documents taken off the split-context path are marked
            ``out_of_order=True`` rather than singled out.

        Raises:
            TypeError: If any element is not a :class:`Document`. Checked
                before the pre-flight, so a malformed list costs not even one
                request
            RuntimeError: If called before patients are seeded
            CavellAuthError: If the API rejects the key (checked once up
                front, and the run aborts on the first 401 mid-run)
            CavellGatewayUnavailableError: If the LLM Gateway stays
                unreachable through the in-place retries (run aborts)
        """
        _reject_removed_extract_kwargs(removed_kwargs)
        if limit is not None and limit < 1:
            raise ValueError(f"limit must be >= 1 if set, got {limit}")
        run = _Run("extract", failures_path, self._totals)
        with run.summarize():
            # Rows Document.from_rows skipped, when given its DocumentList.
            run.reject(list(getattr(documents, "rejected", [])))
            return self._extract(
                documents, skip_processed=skip_processed, limit=limit, run=run
            )

    def extract_from_database(
        self,
        source: "DatabaseSource",
        *,
        columns: "Mapping[str, str | dict[str, str] | None] | None" = None,
        patient_columns: "Mapping[str, str | None] | None" = None,
        patient_defaults: "Mapping[str, Any] | None" = None,
        batch_size: int = 1000,
        dedupe_content: bool = True,
        failures_path: "str | os.PathLike[str] | None" = None,
        on_batch: "Callable[[list[IngestionOutcome]], None] | None" = None,
        **defaults: Any,
    ) -> "DatabaseExtractionReport":
        """Extract every not-yet-extracted document of a database table.

        The table has the same columns as the notes CSV, and ``columns`` is
        the same mapping :meth:`Document.from_rows` takes. Built for large,
        growing tables that are extracted again and again: a scan of the key
        columns (never the note text) and two bulk FHIR scans decide what is
        pending, and only pending rows are fetched — in date order, in
        batches of ``batch_size`` — and extracted through the same per-document
        retries as :meth:`extract`. Rows that fail or are rejected are not
        marked done, so the next run picks them up. Run one extraction per
        table at a time. See the "Extracting from a database" guide.

        Every row that does not land is logged at ERROR with its category
        (:data:`~cavell_client.reporting.FailureCategory`), row number and
        detail, and the run ends with a summary.

        Call :meth:`seed` first with your organizations and practitioners;
        patients are seeded from the table as they are needed.

        Args:
            source: A :class:`~cavell_client.DatabaseSource`.
            columns: Maps Document fields to column names — as for
                :meth:`Document.from_rows`. ``None`` reads the example notes
                CSV's columns (:data:`DEFAULT_NOTE_COLUMNS`, and
                :data:`DEFAULT_PATIENT_COLUMNS` unless ``patient_columns`` is
                given); every one of them must then be in the table under
                exactly that name.
            patient_columns: Maps Patient fields (``name``, ``birth_date``,
                ``gender``, ``managing_organization``,
                ``general_practitioners``) to column names, for seeding
                patients that are not in FHIR yet. The patient identifier is
                always the ``patient_identifier`` column. ``None`` seeds new
                patients with their identifier only — or, when ``columns``
                is ``None`` too, with :data:`DEFAULT_PATIENT_COLUMNS`.
            patient_defaults: Literal Patient values for every new patient
                (e.g. ``{"managing_organization": "CGH-001"}``).
            batch_size: Documents per extraction batch (and per text fetch).
            dedupe_content: Skip a pending document whose text repeats
                another one's for the same patient and date — an earlier
                pending document, or one already extracted (whose text is
                then fetched too, for the days that have pending documents).
            failures_path: Append every failure, rejection and partial
                extraction to this file as JSON Lines, batch by batch.
            on_batch: Called with each batch's outcomes as it finishes.
            **defaults: Literal Document values for every row
                (e.g. ``organization_identifier="CGH-001"``).

        Returns:
            A :class:`DatabaseExtractionReport` with the counts, every failed
            outcome and every rejection.

        Raises:
            ValueError: On a mapping mistake (unknown field, unmapped required
                field, column not in the table), or ``batch_size`` < 1.
            RuntimeError: If called before :meth:`seed`.
            CavellAuthError / CavellGatewayUnavailableError: The run aborts;
                everything not yet extracted is picked up by the next run.
            FHIRConnectionError, httpx errors: If the FHIR server cannot be
                scanned or seeded — infrastructure, not data, so the run stops.
        """
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        if self._phase not in (_Phase.PATIENTS_SEEDED, _Phase.EXTRACTING):
            raise RuntimeError(
                "extract_from_database() requires seed() first — call "
                "seed(organizations, patients=[], practitioners=...) to seed "
                "your organizations and practitioners; patients are seeded "
                "from the table"
            )
        use_default = columns is None
        if columns is None:
            columns = DEFAULT_NOTE_COLUMNS
            if patient_columns is None:
                patient_columns = DEFAULT_PATIENT_COLUMNS
        mapping = _DocumentColumns.parse(columns, defaults)
        _forbid_key_defaults(defaults, ("patient_identifier", "document_id"))
        key_columns = (mapping.flat["patient_identifier"], mapping.flat["document_id"])
        patient_cols = _check_patient_columns(
            patient_columns, patient_defaults, key_columns[0]
        )
        available = source.columns()
        if use_default:
            _require_default_columns(
                [*mapping.all_columns().values(), *patient_cols.values()],
                available,
                "columns",
            )
        _require_available_columns(mapping.all_columns(), set(available))
        _require_available_columns(patient_cols, set(available))

        run = _Run("extract_from_database", failures_path, self._totals)
        report = DatabaseExtractionReport()
        try:
            with run.summarize():
                self._extract_pending(
                    source, mapping, patient_cols, patient_defaults or {},
                    batch_size, dedupe_content, on_batch, run, report,
                )  # fmt: skip
        finally:
            report.extracted, report.partial = run.stats.extracted, run.stats.partial
            report.total_cost = run.stats.cost
        return report

    def _extract_pending(
        self,
        source: "DatabaseSource",
        mapping: _DocumentColumns,
        patient_cols: dict[str, str],
        patient_defaults: Mapping[str, Any],
        batch_size: int,
        dedupe_content: bool,
        on_batch: "Callable[[list[IngestionOutcome]], None] | None",
        run: _Run,
        report: DatabaseExtractionReport,
    ) -> None:
        """The body of :meth:`extract_from_database`, filling ``report``."""

        def reject(rejections: list[DocumentRejection]) -> None:
            report.rejected.extend(rejections)
            run.reject(rejections)

        scan = self._scan_document_keys(source, mapping, patient_cols)
        pending, watermarks, demographics = self._pending_documents(
            scan.keys, scan.demographics
        )
        report.rows_scanned = scan.rows
        report.already_extracted = run.stats.skipped = len(scan.keys) - len(pending)
        extracted_same_day = (
            self._extracted_same_day(scan.keys, pending) if dedupe_content else {}
        )
        # A held reference problem counts only for a document still pending.
        held = [scan.held[k.row] for k in pending if k.row in scan.held]
        if held:
            pending = [k for k in pending if k.row not in scan.held]
        reject(sorted(scan.rejected + held, key=lambda r: r.index or 0))
        report.pending = len(pending)
        del scan
        logger.info(
            f"extract_from_database: {report.rows_scanned:,} row(s) scanned — "
            f"{report.already_extracted:,} already extracted, "
            f"{report.pending:,} pending, {len(report.rejected):,} rejected"
        )

        n_batches = (len(pending) + batch_size - 1) // batch_size
        content_seen: set[tuple[str, str, bytes]] = set()
        for n, start in enumerate(range(0, len(pending), batch_size), 1):
            batch = pending[start : start + batch_size]
            logger.info(
                f"extract_from_database: batch {n}/{n_batches} — {len(batch)} "
                f"document(s) dated {batch[0].date} to {batch[-1].date}"
            )
            docs, row_of, rejections = self._load_batch(
                source, mapping, batch, demographics, patient_defaults
            )
            reject(rejections)
            if dedupe_content:
                # Pending is date-ordered and the key holds the date, so
                # entries older than this batch can never match again.
                content_seen = {k for k in content_seen if k[1] >= batch[0].date}
                # Documents extracted in earlier runs on this batch's days
                # count as seen, so a copy of one is not extracted either.
                earlier = [
                    k
                    for day in dict.fromkeys((k.patient, k.date) for k in batch)
                    for k in extracted_same_day.pop(day, [])
                ]
                if earlier:
                    _dedupe_documents_by_content(
                        self._fetch_documents(source, mapping, earlier), content_seen
                    )
                docs, dropped = _dedupe_documents_by_content(docs, content_seen)
                report.duplicates_skipped += dropped
            # skip_processed=False and pre-fetched watermarks: the bulk scans
            # already know what is done, so a batch makes no per-patient FHIR
            # lookups of its own.
            outcomes = (
                self._extract(
                    docs,
                    skip_processed=False,
                    run=run,
                    row_of=row_of,
                    watermarks=watermarks,
                )
                if docs
                else []
            )
            # Every reference was checked by the scan, so outcomes are
            # extractions — successful or failed.
            report.failed.extend(o for o in outcomes if not o.success)
            if on_batch is not None:
                on_batch(outcomes)

    def _scan_document_keys(
        self,
        source: "DatabaseSource",
        mapping: _DocumentColumns,
        patient_cols: dict[str, str],
    ) -> "_KeyScan":
        """Step 1 of :meth:`extract_from_database`: stream the identifying
        columns (never the note text) and reject the rows that cannot be
        extracted.

        A ``(patient, document_id)`` on several rows becomes one key, its
        first row: the text fetch uses it when the rows agree and rejects it
        when they differ. A row whose organization or practitioner is
        unknown is *held*, not rejected — whether to report it waits for the
        bulk scans, as a document already extracted is not reported again.
        """
        flat = mapping.flat
        pid_col, did_col = flat["patient_identifier"], flat["document_id"]
        scan_cols = [
            pid_col,
            did_col,
            *(
                flat[f]
                for f in ("date", "organization_identifier", "practitioner_identifier")
                if f in flat
            ),
            *patient_cols.values(),
        ]
        logger.info(f"extract_from_database: scanning keys of {source!r}")
        scan = _KeyScan()
        keys = scan.keys
        interned: dict[str, str] = {}  # one string per distinct patient/date
        for row_no, row in enumerate(source._scan(list(dict.fromkeys(scan_cols)))):
            scan.rows += 1
            # Raw values, not stripped: they are the keys the text fetch
            # matches on.
            pid, did = row.get(pid_col, ""), row.get(did_col, "")
            try:
                if isinstance(row, UnreadableRow):
                    raise row.error
                if not pid.strip():
                    raise ValueError("Document.patient_identifier must not be empty")
                if not did.strip():
                    raise ValueError("Document.document_id must not be empty")
                date = _normalize_document_date(mapping.text(row, "date"))
            except ValueError as e:
                scan.rejected.append(mapping.rejection(row, row_no, str(e)))
                continue
            pid = interned.setdefault(pid, pid)
            if pid not in scan.demographics:
                scan.demographics[pid] = {
                    f: row.get(c, "") for f, c in patient_cols.items()
                }
            keys.append(_RowKey(pid, did, row_no, interned.setdefault(date, date)))
            # The values the Document will carry (not stripped), so the check
            # here and the one before extraction agree.
            _, problem = self._reference_problem(
                pid,
                mapping.value(row, "organization_identifier"),
                mapping.value(row, "practitioner_identifier"),
                check_patient=False,
            )
            if problem is not None:
                scan.held[row_no] = mapping.rejection(row, row_no, problem, "reference")

        # Repeats of a key, found by sorting (natively, on the tuple) rather
        # than a dict over every key: keep each key's first row.
        keys.sort()
        scan.keys = [k for i, k in enumerate(keys) if not i or keys[i - 1][:2] != k[:2]]
        return scan

    @staticmethod
    def _extracted_same_day(
        keys: "list[_RowKey]", pending: "list[_RowKey]"
    ) -> "dict[tuple[str, str], list[_RowKey]]":
        """The already-extracted documents dated on a pending document's
        day, by ``(patient, date)`` — the only ones a pending document can
        duplicate."""
        days = {(k.patient, k.date) for k in pending}
        pending_rows = {k.row for k in pending}
        found: dict[tuple[str, str], list[_RowKey]] = {}
        for k in keys:
            if (k.patient, k.date) in days and k.row not in pending_rows:
                found.setdefault((k.patient, k.date), []).append(k)
        return found

    @staticmethod
    def _fetch_documents(
        source: "DatabaseSource", mapping: _DocumentColumns, keys: "list[_RowKey]"
    ) -> list[Document]:
        """The Documents of ``keys``'s rows; rows that are gone or unusable
        are left out."""
        fetched = source.fetch_unique(
            (mapping.flat["patient_identifier"], mapping.flat["document_id"]),
            [(k.patient, k.document) for k in keys],
            compare=mapping.all_columns().values(),
        )
        docs = []
        for row in fetched.values():
            if isinstance(row, dict):
                with contextlib.suppress(ValueError):
                    docs.append(mapping.build(row))
        return docs

    def _pending_documents(
        self, keys: "list[_RowKey]", demographics: dict[str, dict[str, str]]
    ) -> "tuple[list[_RowKey], dict[str, str | None], dict[str, dict[str, str]]]":
        """Step 2 of :meth:`extract_from_database`: two bulk FHIR scans decide
        what is already done.

        Registers the patients that already exist, and returns
        ``(pending keys in date order, chronology watermark per existing
        patient, demographics of the patients still to seed)``.
        """
        patient_ids = self._fhir.list_patient_ids()
        done = self._fhir.list_document_keys()
        latest: dict[str, str] = {}  # patient FHIR id -> newest document date
        for (fhir_id, _), date in done.items():
            if date > latest.get(fhir_id, ""):
                latest[fhir_id] = date
        pending = [
            k for k in keys if (patient_ids.get(k.patient), k.document) not in done
        ]
        pending.sort(key=lambda k: (k.date, k.row))

        watermarks: dict[str, str | None] = {}
        for pid in demographics:
            fhir_id = patient_ids.get(pid)
            if fhir_id is not None:
                self._id_map[(IDENTIFIER_SYSTEM, pid)] = fhir_id
                watermarks[pid] = latest.get(fhir_id) or None
        to_seed = {p: d for p, d in demographics.items() if p not in patient_ids}
        return pending, watermarks, to_seed

    def _load_batch(
        self,
        source: "DatabaseSource",
        mapping: _DocumentColumns,
        batch: "list[_RowKey]",
        demographics: dict[str, dict[str, str]],
        patient_defaults: Mapping[str, Any],
    ) -> "tuple[list[Document], dict[tuple[str, str], int], list[DocumentRejection]]":
        """Step 3 of :meth:`extract_from_database`, per batch: seed the
        batch's new patients, fetch its rows (note text included) and build
        the Documents.

        Returns:
            ``(documents, row_of, rejections)``: the documents to extract,
            each one's row number by ``(patient, document_id)``, and the rows
            that could not be used.
        """
        refused = self._seed_new_patients(
            {k.patient for k in batch}, demographics, patient_defaults
        )
        rejections: list[DocumentRejection] = []
        wanted: list[_RowKey] = []
        for k in batch:
            if k.patient in refused:
                category, reason = refused[k.patient]
                rejections.append(k.rejection(reason, category))
            else:
                wanted.append(k)
        fetched = source.fetch_unique(
            (mapping.flat["patient_identifier"], mapping.flat["document_id"]),
            [(k.patient, k.document) for k in wanted],
            compare=mapping.all_columns().values(),
        )
        docs: list[Document] = []
        row_of: dict[tuple[str, str], int] = {}
        for k in wanted:
            row = fetched.get((k.patient, k.document))
            if row is None:
                continue  # gone since the scan (logged by fetch_unique)
            try:
                if isinstance(row, ValueError):
                    raise row
                docs.append(mapping.build(row))
            except ValueError as e:
                rejections.append(k.rejection(str(e)))
                continue
            row_of[(k.patient, k.document)] = k.row
        return docs, row_of, rejections

    def _seed_new_patients(
        self,
        identifiers: Iterable[str],
        demographics: Mapping[str, Mapping[str, str]],
        patient_defaults: Mapping[str, Any],
    ) -> "dict[str, tuple[Literal['data', 'reference'], str]]":
        """Seed the patients in ``identifiers`` that are not in FHIR yet.

        Returns:
            Patient identifier -> ``(category, reason)`` for each patient that
            could not be seeded; their documents are rejected. A transaction
            the FHIR server refuses is retried patient by patient so one bad
            record (an invalid birth date, say) only costs its own documents.

        Raises:
            httpx.HTTPError / FHIRConnectionError: When the FHIR server is
                unreachable, overloaded or refuses the credentials —
                infrastructure trouble stops the run.
        """
        failures: dict[str, tuple[Literal["data", "reference"], str]] = {}
        resources: list[tuple[str, dict]] = []
        for pid in sorted(set(identifiers)):
            if (IDENTIFIER_SYSTEM, pid) in self._id_map:
                continue
            values: dict[str, Any] = {
                f: v for f, v in demographics.get(pid, {}).items() if v
            }
            values.update(patient_defaults)
            patient = Patient(identifier=pid, **values)
            org = patient.managing_organization
            unknown_gps = [
                gp
                for gp in patient.general_practitioners or []
                if (PRACTITIONER_IDENTIFIER_SYSTEM, gp) not in self._id_map
            ]
            if org and (ORGANIZATION_IDENTIFIER_SYSTEM, org) not in self._id_map:
                failures[pid] = (
                    "reference",
                    f"patient '{pid}' has managing_organization '{org}', which "
                    f"is not seeded",
                )
            elif unknown_gps:
                failures[pid] = (
                    "reference",
                    f"patient '{pid}' has general practitioner(s) "
                    f"{unknown_gps} that are not seeded",
                )
            else:
                resources.append((pid, self._patient_resource(patient)))

        def seed(chunk: list[tuple[str, dict]]) -> str | None:
            """Seed ``chunk``; the server's reason when it refuses it."""
            try:
                result, id_map = self._fhir.seed_bundle([r for _, r in chunk])
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if _fhir_infra_status(status):
                    raise  # unreachable, overloaded or not authorized
                return f"HTTP {status}: {_http_detail(e.response)}"
            if not result.success:
                return truncate(str(result.errors))
            self._id_map.update(id_map)
            return None

        seeded = 0
        for start in range(0, len(resources), _SEED_CHUNK):
            chunk = resources[start : start + _SEED_CHUNK]
            if seed(chunk) is None:
                seeded += len(chunk)
                continue
            for pid, resource in chunk:  # isolate the record(s) refused
                problem = seed([(pid, resource)])
                if problem is None:
                    seeded += 1
                else:
                    failures[pid] = (
                        "data",
                        f"the FHIR server refused to create patient '{pid}' "
                        f"({problem}) — check the patient columns",
                    )
        if seeded:
            logger.info(f"Seeded {seeded} new patient(s)")
        return failures

    def _extract(
        self,
        documents: list[Document],
        *,
        skip_processed: bool,
        run: _Run,
        row_of: "Mapping[tuple[str, str], int] | None" = None,
        limit: int | None = None,
        watermarks: "dict[str, str | None] | None" = None,
    ) -> "list[IngestionOutcome]":
        """One extraction pass — the body of :meth:`extract`.

        Records every final outcome on ``run`` (including when the run
        aborts, before re-raising). ``row_of`` maps ``(patient_identifier,
        document_id)`` to the row number reported for each failure; without
        it the position in ``documents`` is used.

        ``watermarks`` — each patient's newest persisted document date, from
        a bulk scan — replaces the per-patient FHIR lookups (patient exists,
        newest document) and is kept up to date in place as documents land.
        """
        if self._phase not in (_Phase.PATIENTS_SEEDED, _Phase.EXTRACTING):
            raise RuntimeError(
                f"extract() requires phase 'patients_seeded' or 'extracting', "
                f"currently '{self._phase.value}'"
            )

        # Before the pre-flight, so a malformed list costs not even one request.
        # The resume filter below already reads document_id off every document,
        # so this has to run first.
        _require_documents(documents)

        if row_of is None:
            row_of = _first_positions(documents)

        # One cheap validated GET before any spend: the server pre-flights
        # the key against the LLM Gateway (no tokens), so a wrong URL, a
        # missing key, or an invalid key all fail here, before any documents
        # are processed. Once per pipeline: a key revoked later still aborts
        # the run on the first 401.
        if not self._key_checked:
            self._api.check_connection()
            self._key_checked = True

        # Resume filtering: skip already-processed documents
        skipped = 0
        if skip_processed:
            # Scoped per patient: document IDs only need to be unique within
            # a patient (hospital exports commonly restart numbering), so
            # patient B's "note-1" must not be skipped because patient A
            # already processed a "note-1".
            processed_by_patient: dict[str, set[str]] = {}
            for pid in {d.patient_identifier for d in documents}:
                fhir_id = self._id_map.get((IDENTIFIER_SYSTEM, pid))
                if fhir_id:
                    processed_by_patient[pid] = self._fhir.list_document_identifiers(
                        patient=fhir_id
                    )
            original_count = len(documents)
            documents = [
                d
                for d in documents
                if d.document_id
                not in processed_by_patient.get(d.patient_identifier, set())
            ]
            skipped = original_count - len(documents)
            run.stats.skipped += skipped

        if limit is not None:
            documents = documents[:limit]

        # Rejected documents get an outcome (positioned in this pass's list)
        # but are never sent.
        all_docs = documents
        documents, resolved_orgs, rejected = self._validate_documents(documents)

        # Build index for original document positions
        doc_indices = {id(doc): i for i, doc in enumerate(all_docs)}

        # Group by patient and sort by date
        by_patient: dict[str, list[Document]] = defaultdict(list)
        for doc in documents:
            by_patient[doc.patient_identifier].append(doc)

        # Kept even though extract_all() pre-sorts globally: extract() is a
        # public entry point in its own right, and the per-patient watermark
        # below only advances monotonically if each patient's documents arrive
        # oldest-first. On already-ordered input this is a no-op linear pass.
        # Compare date sequences, not documents — a stable sort reorders iff the
        # key sequence was out of order, so this is equivalent without
        # deep-comparing note text.
        for patient_id, docs in by_patient.items():
            sorted_docs = sorted(docs, key=lambda d: d.date)
            if [d.date for d in sorted_docs] != [d.date for d in docs]:
                logger.info(f"Reordered documents for patient '{patient_id}' by date")
            by_patient[patient_id] = sorted_docs

        # Verify all patients still exist on FHIR before spending on extraction
        # — unless the caller's bulk scan (which gave the watermarks) just did.
        if watermarks is None:
            for patient_id in by_patient:
                fhir_id = self._resolve_id(IDENTIFIER_SYSTEM, patient_id)
                try:
                    self._fhir.get_patient(fhir_id)
                except PatientNotFoundError:
                    raise RuntimeError(
                        f"Patient '{patient_id}' (FHIR id {fhir_id}) no longer "
                        f"exists on the FHIR server — re-run seed() to restore it"
                    ) from None

        # Identify reverse-chronological documents so the run can be logged,
        # and reuse the watermarks fetched here for the per-patient chronology
        # bookkeeping below. They are extracted like any other document — the
        # watermark is what routes each one onto the split-context path.
        watermarks, out_of_order = self._check_chronology(documents, watermarks)

        n_patients = len(by_patient)
        patient_label = "patient" if n_patients == 1 else "patients"
        parts = [
            f"Extracting {len(documents)} documents across {n_patients} {patient_label}"
        ]
        if skipped:
            parts.append(f"{skipped} skipped")
        if out_of_order:
            parts.append(f"{len(out_of_order)} out-of-order")
        if limit is not None:
            parts.append(f"limit={limit}")
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
            # document date, fetched once by _check_chronology above. None means
            # it could not be determined (fail-open) or the patient has no
            # processed documents yet.
            watermark = watermarks.get(patient_identifier)
            for doc in docs:
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
                elif not abort_event.is_set():
                    # A failed document persists nothing, so the patient's
                    # remaining documents still extract against a consistent
                    # record. The failed one is re-ingested later — a re-run
                    # lands on the split-context path, which reconciles it
                    # against whatever was recorded after it. (Skipping the
                    # rest of the timeline predates that path.)
                    logger.warning(
                        f"Document {outcome.document_id or '<no id>'} failed for "
                        f"patient '{patient_identifier}'; continuing with the "
                        f"remaining document(s)"
                    )
            # Newer documents may have persisted after a failure. Publish the
            # advanced watermark so the deferred retry below routes any re-run
            # of an older failure onto the split-context path. Safe unlocked:
            # one worker per patient, and the deferred pass starts only after
            # every worker has returned.
            watermarks[patient_identifier] = watermark
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

        def finish(outcomes: list[IngestionOutcome]) -> list[IngestionOutcome]:
            final = [*(r.to_outcome() for r in rejected), *outcomes]
            run.record(final, row_of)
            return final

        all_outcomes = run_batches(by_patient)

        if abort_event.is_set():
            # Count and report what did complete, then surface the run-global
            # failure. Documents not attempted get no outcome; they are still
            # unprocessed, so the next run picks them up.
            finish(all_outcomes)
            not_attempted = len(documents) - len(all_outcomes)
            logger.error(
                f"Run aborted: {abort_exc[0]} — {not_attempted} document(s) in "
                f"this pass were not attempted"
            )
            raise abort_exc[0]

        # Deferred retry: a transient failure (timeout/5xx) shouldn't permanently lose a
        # document. Re-run each transiently failed doc once, in date order — context is
        # re-fetched fresh, so this is safe (a failed document persists nothing), and
        # the published post-run watermark routes docs now older than what persisted
        # after them onto the split-context path. Deterministic failures (rejected
        # bundles) are not retried: re-running reproduces them.
        transient_failures = [o for o in all_outcomes if o.transient]
        if transient_failures:
            deferred: dict[str, list[Document]] = defaultdict(list)
            for o in transient_failures:
                deferred[o.patient_identifier].append(all_docs[o.document_index])
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
                # Report the document's total effort, not just the re-run's.
                o.attempts += merged[o.document_index].attempts
                merged[o.document_index] = o
            all_outcomes = list(merged.values())

            # A run-global failure can also strike during the deferred pass.
            if abort_event.is_set():
                finish(all_outcomes)
                logger.error(f"Run aborted: {abort_exc[0]}")
                raise abort_exc[0]

        # Tally and report from the final, merged outcomes.
        return finish(all_outcomes)

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
        doesn't cost the document its slot in the run. Deterministic failures (a
        rejected bundle, a 4xx) fail fast without retrying.

        ``watermark`` is the patient's newest already-persisted document date.
        A document older than it is extracted against *split* context: the
        record as of its own date, plus everything newer passed separately as
        ``future_context``. ``on_fatal`` is invoked with the exception when the
        failure is run-global (401, or 503 after exhausted retries).
        """
        patient_fhir_id = self._resolve_id(IDENTIFIER_SYSTEM, doc.patient_identifier)
        label = doc.document_id

        out_of_order = watermark is not None and str(doc.date) < watermark
        if out_of_order:
            logger.info(
                f"{label} dated {doc.date} predates the newest persisted document "
                f"({watermark}) for patient '{doc.patient_identifier}' — extracting "
                f"against context split at {doc.date}"
            )

        date = str(doc.date)
        attempt = 0

        def outcome(success: bool, **fields: Any) -> IngestionOutcome:
            return IngestionOutcome(
                success=success,
                patient_identifier=doc.patient_identifier,
                document_index=doc_index,
                document_id=doc.document_id,
                out_of_order=out_of_order,
                attempts=attempt,
                date=date,
                **fields,
            )

        for attempt in range(1, _DOC_MAX_ATTEMPTS + 1):
            step = "lookup"
            try:
                # A transient failure on the persist POST can strike AFTER the
                # FHIR transaction committed. Before re-extracting (and
                # re-persisting duplicates), check whether the previous
                # attempt's DocumentReference already landed.
                if attempt > 1:
                    try:
                        landed = self._fhir.list_document_identifiers(
                            patient=patient_fhir_id
                        )
                    except Exception:
                        landed = set()
                    if doc.document_id in landed:
                        logger.warning(
                            f"{label}: previous attempt persisted despite the "
                            f"error — treating as processed, not re-extracting"
                        )
                        return outcome(True)

                # Resolve org and optionally practitioner
                org_fhir_id = self._resolve_id(
                    ORGANIZATION_IDENTIFIER_SYSTEM, organization_identifier
                )
                practitioner_fhir_id = None
                if doc.practitioner_identifier:
                    practitioner_fhir_id = self._resolve_id(
                        PRACTITIONER_IDENTIFIER_SYSTEM, doc.practitioner_identifier
                    )

                # Fetch clinical context only (no Patient/Organization). The
                # document date anchors the Observation window, so backfilling
                # old documents sees the observations around them rather than
                # an empty window two years behind today.
                future_context: list[dict] | None = None
                step = "context"
                if out_of_order:
                    # Split so the API can treat this document as the latest
                    # one: `context` is the record as of its date, and what
                    # came after travels separately rather than masquerading
                    # as history the author could have known.
                    context, future_context = self._fhir.fetch_split_patient_context(
                        patient_fhir_id, reference_date=doc.date
                    )
                else:
                    context = self._fhir.fetch_patient_context(
                        patient_fhir_id, reference_date=doc.date
                    )

                # The Encounter this document belongs to, if an earlier
                # document already created it. It always travels in `context`
                # — never `future_context`, even for a backdated document —
                # because the API updates it in place, and its out-of-order
                # gate drops any proposed update whose id appears in
                # `future_context`. Without it the API creates the Encounter.
                if doc.encounter_id:
                    existing_encounter = self._fhir.find_encounter(
                        patient_fhir_id, doc.encounter_id
                    )
                    if existing_encounter is not None:
                        context = [*context, existing_encounter]

                # Build meta — the date is no longer part of it, it travels as
                # its own payload field. Only the attending practitioner is
                # still injected; meta stays None when there is nothing to say.
                meta_parts = [doc.meta] if doc.meta else []
                if doc.practitioner_identifier:
                    prac_name = self._practitioner_names[doc.practitioner_identifier]
                    meta_parts.append(
                        f"Attending: {prac_name} ({doc.practitioner_identifier})"
                    )
                meta = "\n".join(meta_parts) or None

                # Call extraction API — reference IDs as explicit params.
                # extract_raw keeps extraction_status/failed_extractors, which
                # extract()'s tuple cannot carry.
                step = "extract"
                response = self._api.extract_raw(
                    text=doc.text,
                    context=context if context else None,
                    meta=meta,
                    # str() to satisfy the declared `str | date` input type;
                    # __post_init__ has already normalized this to YYYY-MM-DD.
                    document_date=str(doc.date),
                    tier=self._tier,
                    patient_id=patient_fhir_id,
                    organization_id=org_fhir_id,
                    practitioner_id=practitioner_fhir_id,
                    document_identifier=doc.document_id,
                    encounter_identifier=doc.encounter_id,
                    future_context=future_context,
                    out_of_order=out_of_order,
                )
                bundle = response.get("bundle", {})
                count = response.get("count", 0)
                usage = parse_usage(response)
                extraction_status = response.get("extraction_status")
                failed_extractors = list(response.get("failed_extractors") or [])
                if failed_extractors:
                    logger.warning(
                        f"Partial extraction for {label}: "
                        f"{', '.join(failed_extractors)} failed"
                    )

                # Match extracted practitioners to seeded ones
                step = "practitioners"
                self._resolve_practitioners(bundle, org_fhir_id)
                step = "persist"

                # Deduplicate and persist to FHIR
                entries = bundle.get("entry", [])
                entries = self._fhir.deduplicate_observations(entries, patient_fhir_id)
                # DISABLED — the extraction API is now told which resources
                # postdate this document (future_context) and decides for
                # itself what to update. Under the split rule every one of
                # those has provenance newer than the document, so enabling
                # this guard would drop every PUT against them and silently
                # veto exactly the reconciliation the API was asked to make.
                # Kept (not deleted) as the fallback if it emits unsafe PUTs.
                # if out_of_order:
                #     related_dates = self._fhir.get_related_document_dates(
                #         patient_fhir_id
                #     )
                #     entries, dropped = _apply_update_guard(
                #         entries, str(doc.date), related_dates
                #     )
                #     if dropped:
                #         logger.info(
                #             f"Update guard dropped {len(dropped)} update(s) from "
                #             f"older document {label}: {', '.join(dropped)}"
                #         )
                persistence = self._fhir.post_bundle(entries)

                if not persistence.success:
                    # Bundle rejected — deterministic content error, do not retry.
                    detail = persist_error_detail(persistence.errors)
                    logger.warning(
                        f"Persist failed for {label} "
                        f"(patient={doc.patient_identifier}): {detail}"
                    )
                    return outcome(
                        False,
                        error=(
                            "Persist failed: the FHIR server rejected the "
                            f"extracted bundle ({len(persistence.errors)} "
                            "resource error(s))"
                        ),
                        category="fhir_rejected",
                        detail=detail,
                    )

                result = ExtractResult(
                    bundle=bundle,
                    count=count,
                    patient_id=patient_fhir_id,
                    usage=usage,
                    persistence=persistence,
                    extraction_status=extraction_status,
                    failed_extractors=failed_extractors,
                )

                return outcome(True, extract_result=result)

            except Exception as e:
                transient = _is_transient_error(e)
                if transient and attempt < _DOC_MAX_ATTEMPTS:
                    wait = _DOC_RETRY_BACKOFF * (2 ** (attempt - 1))
                    logger.warning(
                        f"Transient failure on {label} while "
                        f"{_STEP_DESCRIPTIONS.get(step, step)} "
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
                category, http_status, detail = _classify_failure(e, step)
                where = _STEP_DESCRIPTIONS.get(step, step)
                logger.warning(
                    f"Extraction failed for {label} "
                    f"(patient={doc.patient_identifier}) while {where}: {e}"
                )
                return outcome(
                    False,
                    error=f"failed while {where}: {e}",
                    transient=transient,
                    category=category,
                    detail=detail,
                    http_status=http_status,
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
