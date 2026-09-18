"""Deterministic lab-results ingestion.

Structured lab rows (CSV/LIS exports) become FHIR Observations without an
LLM: the pipeline validates every reference against the FHIR server
(fail-closed — patients, encounters and practitioners must already exist),
sends the surviving rows to Prism's ``POST /ingest/lab-results`` per patient,
and persists the returned bundle. Rows that fail validation, reference
resolution, or the API's own content checks are skipped and reported with a
reason — one bad row never sinks the batch.

Unlike document extraction there are no phases, no seeding, no chronology
watermarks and no per-document retries: the endpoint is deterministic and its
bundle entries are conditional creates on each row's ``urn:cavell:lab-result``
identifier, so re-running a feed is idempotent by construction.
"""

import logging
import math
import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Literal

import httpx

from cavell_client.api import CavellAPI
from cavell_client.fhir import FHIRClient
from cavell_client.ingestion import _validate_columns
from cavell_client.models import FHIRConnectionError, PersistResult

if TYPE_CHECKING:
    from cavell_client.client import CavellClient

logger = logging.getLogger(__name__)

#: LabResult fields ``from_rows`` must be given a real column for. The values
#: are still only *content*-checked by ``ingest()`` (skip-and-report); this
#: decides which keys the caller has to map.
_REQUIRED_LAB_COLUMNS = (
    "patient_identifier",
    "lab_result_id",
    "test_name",
    "value",
    "collected_datetime",
)

_ALLOWED_STATUSES = frozenset({"preliminary", "final", "amended", "cancelled"})

#: The API caps rows per request (max_length=5000); bigger patients are sent
#: in chunks so one prolific patient cannot 422 the run.
_MAX_ROWS_PER_REQUEST = 5000

#: Mirrors the API's charset rule: the id lands in a FHIR conditional-create
#: query string, where metacharacters would change the query's meaning.
#: Checking client-side turns 5000 round-tripped server rejections into
#: local "validation"-stage ones.
_LAB_RESULT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")

_BOUND_PLAIN_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")
# A comma is a decimal separator only when it cannot be a grouping one:
# float('1,000'.replace(',', '.')) would silently read a thousands-separated
# bound as 1.0. Mirrors the API's rule for `value`.
_BOUND_EURO_RE = re.compile(r"^[+-]?\d+,\d{1,2}$")


def _as_text(value: Any) -> str:
    """``value`` as stripped text.

    Rows often arrive from dataframes rather than csv.DictReader, so numbers
    are legitimate input: they become their text form (the API's `value` is a
    string). ``None`` and non-finite floats (a pandas NaN) become "" so the
    row is rejected with a reason instead of crashing the whole request.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        return str(value) if math.isfinite(value) else ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _coerce_bound(value: Any) -> float | str | None:
    """A reference bound as float, ``None`` when absent, or the original text
    when it cannot be read unambiguously (``ingest()`` rejects it with a
    reason rather than guessing)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return float(value)
    if not isinstance(value, str):
        return str(value)
    text = value.strip()
    if not text:
        return None
    if _BOUND_PLAIN_RE.match(text):
        return float(text)
    if _BOUND_EURO_RE.match(text):
        return float(text.replace(",", "."))
    return text


#: How many offending items to name before truncating the error message.
_MAX_LISTED_NON_RESULTS = 10

RejectionStage = Literal["validation", "reference", "server"]


@dataclass
class LabResult:
    """One structured lab result to ingest.

    Content problems (blank value, malformed datetime) never raise here —
    :meth:`LabIngestionPipeline.ingest` skips and reports them, so one bad CSV
    row cannot sink the batch. Construction only normalizes: strings are
    stripped, empty optionals become ``None``, and numeric-looking reference
    bounds are coerced to float (European decimal commas included).
    """

    #: The hospital's patient identifier (MRN, ``urn:cavell:patient``) — NOT a
    #: FHIR id. The pipeline resolves it and rejects rows whose patient is not
    #: already in the FHIR server.
    patient_identifier: str
    test_name: str
    #: Result as text: numeric (comparators like ``<5`` allowed) becomes a
    #: valueQuantity server-side, anything else a verbatim valueString.
    #: Dataframe-shaped rows may pass a number; it is normalized to text
    #: (NaN becomes absent, so the row is rejected with a reason).
    value: str | float | int
    #: ``YYYY-MM-DD``, or a full ISO datetime WITH a timezone offset (FHIR
    #: requires one alongside a time; the API rejects offset-less times).
    collected_datetime: str
    #: Your unique id for this result (e.g. a LIS accession number),
    #: keyword-only like ``Document.document_id``. It is the idempotency key:
    #: the Observation carries it as its ``urn:cavell:lab-result`` identifier
    #: and the API's conditional create matches on it scoped to the patient,
    #: so re-submitting a feed matches instead of duplicating. Must be unique
    #: per patient (unique across the whole feed is safest). Letters, digits
    #: and ``. _ : / -`` only, max 200 characters. Note that ingestion only
    #: CREATES: re-sending an existing id never updates the stored
    #: Observation, so a corrected (amended) result needs a new id.
    lab_result_id: str = field(kw_only=True)
    loinc_code: str | None = None
    unit: str | None = None
    reference_low: float | str | None = None
    reference_high: float | str | None = None
    #: Your identifier for the visit (``urn:cavell:encounter``) — NOT a FHIR
    #: id. When set it must resolve for this patient or the row is rejected;
    #: when ``None`` the Observation simply has no encounter reference.
    encounter_id: str | None = None
    #: Your identifier for the performing practitioner
    #: (``urn:cavell:practitioner``) — NOT a FHIR id. Same semantics as
    #: ``encounter_id``: present means it must resolve.
    practitioner_id: str | None = None
    status: str = "final"

    def __post_init__(self) -> None:
        for name in (
            "patient_identifier",
            "test_name",
            "value",
            "collected_datetime",
            "lab_result_id",
        ):
            setattr(self, name, _as_text(getattr(self, name)))
        self.status = _as_text(self.status) or "final"
        for name in ("loinc_code", "unit", "encounter_id", "practitioner_id"):
            setattr(self, name, _as_text(getattr(self, name)) or None)
        for name in ("reference_low", "reference_high"):
            setattr(self, name, _coerce_bound(getattr(self, name)))

    @classmethod
    def from_rows(
        cls,
        rows: list[dict[str, str]],
        columns: Mapping[str, str | None],
        **defaults: str,
    ) -> "list[LabResult]":
        """Build a list of LabResults from CSV/dict rows.

        Args:
            rows: List of dicts (e.g. from csv.DictReader).
            columns: Maps LabResult field names to column names. ``None`` is
                how you disable an optional field whose column your export
                does not have. Must include a real column name for
                "patient_identifier", "lab_result_id", "test_name", "value"
                and "collected_datetime" — those are required and cannot be
                disabled that way.
            **defaults: Literal values applied to every result
                (e.g. ``status="preliminary"``).

        Returns:
            List of LabResult, one per row — no rows are dropped here; content
            problems surface as rejections from :meth:`LabIngestionPipeline.ingest`.
        """
        valid_fields = {f.name for f in fields(cls)}

        for required in _REQUIRED_LAB_COLUMNS:
            if not columns.get(required):
                raise ValueError(
                    f"columns must include '{required}' — it is a required "
                    f"LabResult field and cannot be disabled"
                )
        for key in columns:
            if key not in valid_fields:
                raise ValueError(f"Unknown LabResult field in columns: '{key}'")
        for key in defaults:
            if key not in valid_fields:
                raise ValueError(f"Unknown LabResult field in defaults: '{key}'")

        if not rows:
            return []

        flat_columns = {k: v for k, v in columns.items() if isinstance(v, str)}
        _validate_columns(flat_columns, rows)

        results: list[LabResult] = []
        for row in rows:
            kwargs: dict[str, Any] = dict(defaults)
            for field_name, col_name in flat_columns.items():
                kwargs[field_name] = row.get(col_name, "")
            results.append(cls(**kwargs))
        return results


@dataclass(frozen=True)
class LabRejection:
    """One skipped row: where it was, which result, and why."""

    #: 0-based position in the list passed to ``ingest()`` — input-relative,
    #: even for rejections the API reported against its own request.
    index: int
    lab_result_id: str | None
    patient_identifier: str | None
    reason: str
    #: Where the row fell out: "validation" (content checks before any
    #: network), "reference" (patient/encounter/practitioner not found in
    #: FHIR), or "server" (the API's own row checks).
    stage: RejectionStage


@dataclass
class LabIngestionOutcome:
    """Result of one :meth:`LabIngestionPipeline.ingest` call."""

    total: int
    #: Rows actually persisted to the FHIR server — created or matched as
    #: already present. A row lost to a failed transaction is neither
    #: accepted nor rejected: it appears in its PersistResult's errors and
    #: flips :attr:`success`.
    accepted: int
    rejected: list[LabRejection]
    #: One (patient_identifier, PersistResult) per POSTED bundle, in
    #: processing order. A patient with more than the API's per-request row
    #: cap contributes several entries.
    persistence: list[tuple[str, PersistResult]]

    @property
    def created(self) -> int:
        """Observations newly created (201s)."""
        return sum(p.created for _, p in self.persistence)

    @property
    def skipped_existing(self) -> int:
        """Observations already present (200s — the conditional create matched)."""
        return sum(p.updated for _, p in self.persistence)

    @property
    def success(self) -> bool:
        """True when every posted bundle persisted cleanly.

        Rejected rows do not make a run unsuccessful — skipping and reporting
        them is the designed behaviour; check :attr:`rejected` for those.
        """
        return all(p.success for _, p in self.persistence)

    def __str__(self) -> str:
        return (
            f"{self.accepted}/{self.total} rows accepted "
            f"({self.created} created, {self.skipped_existing} already present), "
            f"{len(self.rejected)} rejected"
        )


def _require_lab_results(results: list["LabResult"]) -> None:
    """Reject anything in the list that is not a :class:`LabResult`.

    Same rationale as the document pipeline's ``_require_documents``: handing
    raw CSV rows straight through is the obvious mistake, and it should fail
    up front with a pointed message, not as an ``AttributeError`` mid-run.

    Raises:
        TypeError: If any element is not a :class:`LabResult`.
    """
    wrong_type = [
        f"index {i}: got {type(item).__name__}"
        for i, item in enumerate(results)
        if not isinstance(item, LabResult)
    ]
    if not wrong_type:
        return
    listed = "\n".join(f"  - {w}" for w in wrong_type[:_MAX_LISTED_NON_RESULTS])
    if len(wrong_type) > _MAX_LISTED_NON_RESULTS:
        listed += f"\n  ... and {len(wrong_type) - _MAX_LISTED_NON_RESULTS} more"
    raise TypeError(
        f"{len(wrong_type)} of {len(results)} item(s) are not LabResult "
        f"objects. Build them with LabResult(...) or "
        f"LabResult.from_rows(...):\n{listed}"
    )


def _content_problem(result: LabResult) -> str | None:
    """The row's content problem, or None when it can be sent."""
    for name in (
        "patient_identifier",
        "lab_result_id",
        "test_name",
        "value",
        "collected_datetime",
    ):
        if not getattr(result, name):
            return f"{name} must be non-empty"
    if not _LAB_RESULT_ID_RE.match(result.lab_result_id):
        return (
            "lab_result_id may only contain letters, digits and . _ : / - "
            "(max 200 characters, starting with a letter or digit)"
        )
    for name in ("reference_low", "reference_high"):
        if isinstance(getattr(result, name), str):
            return f"{name} is not numeric: '{getattr(result, name)}'"
    if result.status not in _ALLOWED_STATUSES:
        return f"status must be one of {sorted(_ALLOWED_STATUSES)}: '{result.status}'"
    return None


def _fail_closed(description: str, lookup: Callable[..., Any], *args: Any) -> Any:
    """Run a FHIR call, wrapping transport failures in the library's type.

    Callers of ``ingest()`` handle ``CavellError`` subclasses; a raw
    httpx exception escaping the public API would bypass that contract.
    """
    try:
        return lookup(*args)
    except httpx.HTTPError as e:
        raise FHIRConnectionError(f"{description}: {e}") from e


class LabIngestionPipeline:
    """Validates, sends and persists structured lab results.

    Unlike :class:`IngestionPipeline` there is no seeding and no phase
    machinery: everything the rows reference must already exist in the FHIR
    server (patients seeded, encounters and practitioners created by earlier
    document extraction or by you), and rows referencing anything unknown are
    rejected rather than created for.
    """

    def __init__(self, client: "CavellClient") -> None:
        self._api: CavellAPI = client._api
        self._fhir: FHIRClient = client._fhir

    def ingest(self, results: list[LabResult]) -> LabIngestionOutcome:
        """Ingest lab results: validate → resolve references → API → persist.

        Rows are processed patient by patient (sorted by identifier, so runs
        are deterministic); within a patient the request preserves input
        order. All lookups are fail-closed: a missing patient, encounter or
        practitioner rejects the rows that reference it, while an *error*
        talking to the FHIR server aborts the run — infrastructure trouble is
        not bad data and retrying later is the right move.

        Aborts are safe to retry: persistence is idempotent (conditional
        creates), so after fixing the infrastructure the same feed can simply
        be re-ingested — already-persisted rows come back as
        ``skipped_existing``.

        Args:
            results: LabResult objects (see :meth:`LabResult.from_rows`).

        Returns:
            LabIngestionOutcome with accepted/rejected counts, per-row
            rejection reasons, and per-bundle persistence results.

        Raises:
            TypeError: If any element is not a :class:`LabResult`.
            CavellAPIError: If the Prism API rejects a request (a 404 names a
                deployment predating lab ingestion); auth and gateway
                problems raise the usual CavellAuthError /
                CavellGatewayUnavailableError.
            FHIRConnectionError: If a patient/encounter/practitioner lookup
                or bundle POST fails at the transport level — infrastructure
                trouble, not bad data.
        """
        _require_lab_results(results)
        self._api.check_connection()

        rejections: list[LabRejection] = []

        def reject(
            index: int, result: LabResult, reason: str, stage: RejectionStage
        ) -> None:
            rejections.append(
                LabRejection(
                    index=index,
                    lab_result_id=result.lab_result_id or None,
                    patient_identifier=result.patient_identifier or None,
                    reason=reason,
                    stage=stage,
                )
            )

        # Validation stage: content checks, no network.
        by_patient: dict[str, list[tuple[int, LabResult]]] = defaultdict(list)
        first_seen: dict[str, int] = {}
        for index, result in enumerate(results):
            if problem := _content_problem(result):
                reject(index, result, problem, "validation")
                continue
            if result.lab_result_id in first_seen:
                reject(
                    index,
                    result,
                    "duplicate lab_result_id "
                    f"(first used by row {first_seen[result.lab_result_id]})",
                    "validation",
                )
                continue
            first_seen[result.lab_result_id] = index
            by_patient[result.patient_identifier].append((index, result))

        # Reference + send + persist, one patient at a time. The practitioner
        # cache is global: practitioners are shared across patients.
        persistence: list[tuple[str, PersistResult]] = []
        accepted = 0
        practitioner_ids: dict[str, str | None] = {}
        for patient_identifier in sorted(by_patient):
            patient_rows = by_patient[patient_identifier]
            found = _fail_closed(
                f"Patient lookup '{patient_identifier}'",
                self._fhir.find_patient_by_identifier,
                patient_identifier,
            )
            if found is None:
                for index, result in patient_rows:
                    reject(
                        index,
                        result,
                        f"unknown patient '{patient_identifier}'",
                        "reference",
                    )
                continue
            patient_fhir_id, _ = found

            encounter_ids: dict[str, str | None] = {}
            payload_rows: list[dict[str, Any]] = []
            sent: list[tuple[int, LabResult]] = []
            for index, result in patient_rows:
                encounter_fhir_id = None
                if result.encounter_id:
                    if result.encounter_id not in encounter_ids:
                        encounter = _fail_closed(
                            f"Encounter lookup '{result.encounter_id}'",
                            self._fhir.find_encounter_strict,
                            patient_fhir_id,
                            result.encounter_id,
                        )
                        encounter_ids[result.encounter_id] = (
                            encounter["id"] if encounter else None
                        )
                    encounter_fhir_id = encounter_ids[result.encounter_id]
                    if encounter_fhir_id is None:
                        reject(
                            index,
                            result,
                            f"unknown encounter '{result.encounter_id}' for "
                            f"patient '{patient_identifier}'",
                            "reference",
                        )
                        continue
                practitioner_fhir_id = None
                if result.practitioner_id:
                    if result.practitioner_id not in practitioner_ids:
                        practitioner = _fail_closed(
                            f"Practitioner lookup '{result.practitioner_id}'",
                            self._fhir.find_practitioner_by_identifier,
                            result.practitioner_id,
                        )
                        practitioner_ids[result.practitioner_id] = (
                            practitioner["id"] if practitioner else None
                        )
                    practitioner_fhir_id = practitioner_ids[result.practitioner_id]
                    if practitioner_fhir_id is None:
                        reject(
                            index,
                            result,
                            f"unknown practitioner '{result.practitioner_id}'",
                            "reference",
                        )
                        continue

                row: dict[str, Any] = {
                    "lab_result_id": result.lab_result_id,
                    "test_name": result.test_name,
                    "value": result.value,
                    "collected_datetime": result.collected_datetime,
                    "status": result.status,
                }
                if result.loinc_code:
                    row["loinc_code"] = result.loinc_code
                if result.unit:
                    row["unit"] = result.unit
                if result.reference_low is not None:
                    row["reference_low"] = result.reference_low
                if result.reference_high is not None:
                    row["reference_high"] = result.reference_high
                if encounter_fhir_id:
                    row["encounter_id"] = encounter_fhir_id
                if practitioner_fhir_id:
                    row["practitioner_id"] = practitioner_fhir_id
                payload_rows.append(row)
                sent.append((index, result))

            # Chunked to the API's per-request row cap so one prolific
            # patient cannot 422 the whole run.
            for chunk_start in range(0, len(payload_rows), _MAX_ROWS_PER_REQUEST):
                chunk_rows = payload_rows[
                    chunk_start : chunk_start + _MAX_ROWS_PER_REQUEST
                ]
                chunk_sent = sent[chunk_start : chunk_start + _MAX_ROWS_PER_REQUEST]
                response = self._api.ingest_lab_results(
                    patient_id=patient_fhir_id, rows=chunk_rows
                )
                # The API's rejection indexes are request-relative; map them
                # back to positions in the caller's input.
                for server_rejection in response.get("rejected", []):
                    request_index = server_rejection.get("index")
                    if not isinstance(request_index, int) or not (
                        0 <= request_index < len(chunk_sent)
                    ):
                        logger.warning(
                            f"API rejection with unmappable index "
                            f"{request_index!r}: {server_rejection.get('reason')}"
                        )
                        continue
                    index, result = chunk_sent[request_index]
                    reject(
                        index,
                        result,
                        server_rejection.get("reason", "rejected by the API"),
                        "server",
                    )

                entries = response.get("bundle", {}).get("entry", [])
                if not entries:
                    continue
                # Identifier-based conditional creates make re-runs
                # idempotent; deliberately NOT deduplicate_observations,
                # whose day-resolution signature would collapse distinct
                # same-day draws.
                persist_result = _fail_closed(
                    f"Bundle POST for patient '{patient_identifier}'",
                    self._fhir.post_bundle,
                    entries,
                )
                persistence.append((patient_identifier, persist_result))
                # Count what actually landed: a failed transaction persists
                # nothing, and reporting its rows as accepted would print a
                # success headline over a run that wrote nothing.
                accepted += persist_result.created + persist_result.updated
                logger.info(
                    f"Patient {patient_identifier}: {len(entries)} lab "
                    f"result(s) posted ({persist_result.created} created, "
                    f"{persist_result.updated} already present, "
                    f"{len(persist_result.errors)} errors)"
                )

        rejections.sort(key=lambda r: r.index)
        return LabIngestionOutcome(
            total=len(results),
            accepted=accepted,
            rejected=rejections,
            persistence=persistence,
        )
