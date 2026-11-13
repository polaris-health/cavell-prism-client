"""Failure classification and reporting shared by the notes and lab pipelines.

Every row that does not end up in FHIR is described by one :class:`FailureRecord`
carrying a :data:`FailureCategory` — *who* has to act (the source data, the
references, Prism, the FHIR server, or the run as a whole) — plus the detail
needed to act on it. The :class:`FailureReporter` turns those records into
explicit log lines, an end-of-run summary, and optionally a JSON Lines file.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
from collections import Counter
from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

#: Why a row did not land, by who has to act on it.
#:
#: - ``data``: the row itself is unusable (blank text or id, bad date,
#:   duplicate id). Fix the row in the source.
#: - ``reference``: the row points at something that does not exist (unknown
#:   patient, organization, practitioner or encounter).
#: - ``prism_rejected``: the Prism API refused the document or row (4xx).
#: - ``prism_unavailable``: Prism or the network failed transiently through
#:   every retry (timeout, connection error, 429, 5xx).
#: - ``prism_partial``: extraction succeeded but some extraction steps failed,
#:   so some resources may be missing. Not retried automatically.
#: - ``fhir_rejected``: your FHIR server refused what was sent to it.
#: - ``fhir_unavailable``: your FHIR server could not be reached or errored.
#: - ``run_aborted``: the run stopped (API key rejected, LLM Gateway down).
#: - ``client_error``: an unexpected error inside the SDK.
FailureCategory = Literal[
    "data",
    "reference",
    "prism_rejected",
    "prism_unavailable",
    "prism_partial",
    "fhir_rejected",
    "fhir_unavailable",
    "run_aborted",
    "client_error",
]

#: What to do about each category — printed on every failure log line.
CATEGORY_ACTIONS: dict[str, str] = {
    "data": "fix the row in the source data; it is picked up again on the next run",
    "reference": (
        "seed the referenced patient/organization/practitioner or fix the "
        "reference in the source data; picked up again on the next run"
    ),
    "prism_rejected": (
        "Prism refused this input — see the detail; fix the row if it names a "
        "content problem, otherwise contact Cavell support with this line"
    ),
    "prism_unavailable": (
        "transient Prism/network problem; it is retried automatically on the next run"
    ),
    "prism_partial": (
        "extracted, but some extraction steps failed so resources may be missing; "
        "not retried automatically — contact Cavell support if it recurs"
    ),
    "fhir_rejected": (
        "your FHIR server refused the data — check its validation/profile "
        "settings and the detail; picked up again on the next run"
    ),
    "fhir_unavailable": (
        "your FHIR server could not be reached or errored; check it — picked up "
        "again on the next run"
    ),
    "run_aborted": (
        "the run stopped before this could be processed — fix the cause in the "
        "run error (API key, connectivity) and re-run"
    ),
    "client_error": (
        "unexpected SDK error — please report it to Cavell support with this line"
    ),
}

#: How many individual failure lines the end-of-run summary prints before
#: pointing at the failures file for the rest.
SUMMARY_LOG_LIMIT = 200

FailureStatus = Literal["failed", "rejected", "partial"]


@dataclass(frozen=True)
class FailureRecord:
    """One row that did not land cleanly, with everything needed to act on it."""

    #: ``"document"`` or ``"lab_result"``.
    kind: str
    #: Your ``document_id`` / ``lab_result_id`` (``None`` when the row had none).
    identifier: str | None
    patient_identifier: str | None
    #: ``failed`` (attempted and failed), ``rejected`` (never sent), or
    #: ``partial`` (landed, but incomplete).
    status: FailureStatus
    category: FailureCategory
    #: One-line explanation.
    reason: str
    #: Document date / lab collection datetime, when known.
    date: str | None = None
    #: 0-based position of the row in the input — the CSV row list or the
    #: database query result.
    row: int | None = None
    #: Underlying detail: the API's message, FHIR OperationOutcome
    #: diagnostics, the exception text.
    detail: str | None = None
    http_status: int | None = None
    #: Attempts made in this run (including the in-run retries).
    attempts: int | None = None
    transient: bool = False

    @property
    def action(self) -> str:
        return CATEGORY_ACTIONS.get(self.category, "")

    def log_line(self) -> str:
        label = self.identifier or "<no id>"
        parts = [f"{self.status.upper()} {self.kind} {label}"]
        if self.patient_identifier:
            parts.append(f"patient={self.patient_identifier}")
        if self.date:
            parts.append(f"date={self.date}")
        if self.row is not None:
            parts.append(f"row={self.row}")
        parts.append(f"category={self.category}")
        if self.http_status is not None:
            parts.append(f"http={self.http_status}")
        if self.attempts:
            parts.append(f"attempts={self.attempts}")
        if self.transient:
            parts.append("transient=yes")
        text = " ".join(parts) + f": {self.reason}"
        if self.detail and self.detail not in self.reason:
            text += f" | detail: {self.detail}"
        return f"{text} -> {self.action}"

    def to_json(self, run_started_at: str) -> dict:
        record = asdict(self)
        record["run_started_at"] = run_started_at
        record["action"] = self.action
        return record


def truncate(text: str, limit: int = 2000) -> str:
    """Keep log lines bounded — a FHIR OperationOutcome can be very long."""
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text) - limit})"


def persist_error_detail(errors: list[dict]) -> str:
    """The per-resource errors of a refused FHIR transaction, as one line."""
    detail = "; ".join(
        f"{e.get('resource', '?')}: {e.get('error', '?')}" for e in errors
    )
    return truncate(detail or str(errors))


class FailureReporter:
    """Collects the failure records of one run, logs them, and writes them out.

    Each record is logged as it is added and appended to ``failures_path``
    (JSON Lines) at once, so an interrupted run keeps what it had recorded.
    The file is opened in append mode: one file can accumulate many runs, each
    record stamped with its run's start time. In memory it keeps only the
    counts and the records the summary prints, so a run with millions of
    failures stays small.
    """

    def __init__(self, failures_path: str | os.PathLike[str] | None = None) -> None:
        """
        Raises:
            OSError: If ``failures_path`` cannot be written — checked now, so
                a bad path fails the run before any work, not hours in.
        """
        self._path = Path(failures_path) if failures_path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.open("a", encoding="utf-8").close()
        self.run_started_at = datetime.datetime.now(datetime.UTC).isoformat(
            timespec="seconds"
        )
        self._counts: Counter[str] = Counter()
        self._only_partial = True
        #: Per category, the first records — all the summary can show.
        self._samples: dict[str, list[FailureRecord]] = {}

    def add(self, records: list[FailureRecord]) -> None:
        for record in records:
            self._counts[record.category] += 1
            self._only_partial &= record.status == "partial"
            samples = self._samples.setdefault(record.category, [])
            if len(samples) < SUMMARY_LOG_LIMIT:
                samples.append(record)
            if record.status == "partial":
                logger.warning(record.log_line())
            else:
                logger.error(record.log_line())
        if records and self._path is not None:
            with self._path.open("a", encoding="utf-8") as f:
                for record in records:
                    f.write(
                        json.dumps(record.to_json(self.run_started_at), default=str)
                    )
                    f.write("\n")

    def log_summary(self, headline: str, *, aborted: bool = False) -> None:
        """Log the end-of-run summary: the headline, counts per category, and
        the failures grouped by category (capped at :data:`SUMMARY_LOG_LIMIT`
        lines). An aborted run is always logged at ERROR."""
        total = sum(self._counts.values())
        if not total:
            (logger.error if aborted else logger.info)(f"{headline} — no failures")
            return
        ranked = self._counts.most_common()
        counts = ", ".join(f"{cat}={n}" for cat, n in ranked)
        log = logger.warning if self._only_partial and not aborted else logger.error
        log(f"{headline} — {total} row(s) need attention: {counts}")
        shown = [r for cat, _ in ranked for r in self._samples[cat]]
        for record in shown[:SUMMARY_LOG_LIMIT]:
            log(f"  {record.log_line()}")
        if total > SUMMARY_LOG_LIMIT:
            where = (
                f"see {self._path}"
                if self._path is not None
                else "pass failures_path= to write them all to a file"
            )
            log(f"  ... and {total - SUMMARY_LOG_LIMIT} more ({where})")
        elif self._path is not None:
            log(f"  Full records appended to {self._path}")

    @contextlib.contextmanager
    def summarize(self, headline: Callable[[Exception | None], str]) -> Generator[None]:
        """Log the summary when the block ends: ``headline(None)`` — or, if
        the block raises, ``headline(exc)`` before the exception propagates."""
        try:
            yield
        except Exception as e:
            self.log_summary(headline(e), aborted=True)
            raise
        self.log_summary(headline(None))
