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
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Literal

from cavell_client.api import CavellAPI
from cavell_client.fhir import FHIRClient
from cavell_client.ingestion import _validate_columns
from cavell_client.models import PersistResult

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
    value: str
    #: ``YYYY-MM-DD``, or a full ISO datetime WITH a timezone offset (FHIR
    #: requires one alongside a time; the API rejects offset-less times).
    collected_datetime: str
    #: Your unique id for this result (e.g. a LIS accession number),
    #: keyword-only like ``Document.document_id``. It is the idempotency key:
    #: the Observation carries it as its ``urn:cavell:lab-result`` identifier
    #: and re-submitting a feed matches instead of duplicating. Must be unique
    #: across the whole feed.
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
            value = getattr(self, name)
            setattr(self, name, value.strip() if isinstance(value, str) else value)
        status = self.status.strip() if isinstance(self.status, str) else ""
        self.status = status or "final"
        for name in ("loinc_code", "unit", "encounter_id", "practitioner_id"):
            value = getattr(self, name)
            if isinstance(value, str):
                setattr(self, name, value.strip() or None)
        for name in ("reference_low", "reference_high"):
            value = getattr(self, name)
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    setattr(self, name, None)
                    continue
                try:
                    setattr(self, name, float(text.replace(",", ".")))
                except ValueError:
                    # Left as the string; ingest() rejects it with a reason.
                    setattr(self, name, text)

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
    #: Rows that reached a bundle POSTed to the FHIR server.
    accepted: int
    rejected: list[LabRejection]
    #: One (patient_identifier, PersistResult) per patient whose bundle was
    #: posted, in processing order.
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
    for name in ("reference_low", "reference_high"):
        if isinstance(getattr(result, name), str):
            return f"{name} is not numeric: '{getattr(result, name)}'"
    if result.status not in _ALLOWED_STATUSES:
        return f"status must be one of {sorted(_ALLOWED_STATUSES)}: '{result.status}'"
    return None


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

        Args:
            results: LabResult objects (see :meth:`LabResult.from_rows`).

        Returns:
            LabIngestionOutcome with accepted/rejected counts, per-row
            rejection reasons, and per-patient persistence results.
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
            found = self._fhir.find_patient_by_identifier(patient_identifier)
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
                        encounter = self._fhir.find_encounter_strict(
                            patient_fhir_id, result.encounter_id
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
                        matches = self._fhir.search_practitioners(
                            identifier=result.practitioner_id
                        )
                        practitioner_ids[result.practitioner_id] = (
                            matches[0]["id"] if matches else None
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

            if not payload_rows:
                continue
            response = self._api.ingest_lab_results(
                patient_id=patient_fhir_id, rows=payload_rows
            )
            # The API's rejection indexes are request-relative; map them back
            # to positions in the caller's input.
            for server_rejection in response.get("rejected", []):
                request_index = server_rejection.get("index")
                if not isinstance(request_index, int) or not (
                    0 <= request_index < len(sent)
                ):
                    logger.warning(
                        f"API rejection with unmappable index {request_index!r}: "
                        f"{server_rejection.get('reason')}"
                    )
                    continue
                index, result = sent[request_index]
                reject(
                    index,
                    result,
                    server_rejection.get("reason", "rejected by the API"),
                    "server",
                )

            entries = response.get("bundle", {}).get("entry", [])
            if not entries:
                continue
            # Identifier-based conditional creates make re-runs idempotent;
            # deliberately NOT deduplicate_observations, whose day-resolution
            # signature would collapse distinct same-day draws.
            persist_result = self._fhir.post_bundle(entries)
            persistence.append((patient_identifier, persist_result))
            accepted += len(entries)
            logger.info(
                f"Patient {patient_identifier}: {len(entries)} lab result(s) "
                f"posted ({persist_result.created} created, "
                f"{persist_result.updated} already present)"
            )

        rejections.sort(key=lambda r: r.index)
        return LabIngestionOutcome(
            total=len(results),
            accepted=accepted,
            rejected=rejections,
            persistence=persistence,
        )
