"""In-memory stand-ins for the FHIR server and the Prism API.

They keep real state — seeded resources, persisted DocumentReferences and lab
Observations — so a test can run a pipeline, run it again, and see what a real
re-run would see. The pipelines' own code runs unchanged on top of them,
including the per-document retry and failure classification.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any

import httpx

from cavell_client.fhir import (
    DOCUMENT_IDENTIFIER_SYSTEM,
    IDENTIFIER_SYSTEM,
    LAB_RESULT_IDENTIFIER_SYSTEM,
)
from cavell_client.models import CavellAPIError, PersistResult


def http_error(status: int, body: dict | str) -> httpx.HTTPStatusError:
    """What the FHIR client raises for an error response with ``body``."""
    request = httpx.Request("POST", "http://fhir.test/")
    if isinstance(body, dict):
        response = httpx.Response(status, json=body, request=request)
    else:
        response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class FakeFHIR:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        #: (system, value) -> FHIR id, for every seeded resource
        self.ids: dict[tuple[str, str], str] = {}
        #: (patient FHIR id, document_id) -> date of every persisted
        #: DocumentReference
        self.documents: dict[tuple[str, str], str] = {}
        #: (patient FHIR id, lab_result_id) of every persisted lab Observation
        self.labs: set[tuple[str, str]] = set()
        #: Patient identifiers the server refuses to create (bad demographics)
        self.refuse_patients: set[str] = set()
        #: Document ids whose bundle the server rejects
        self.reject_bundles_for: set[str] = set()
        self.seed_sizes: list[int] = []
        self.calls: list[str] = []

    # -- seeding -------------------------------------------------------
    def seed_bundle(self, resources: list[dict]) -> tuple[PersistResult, dict]:
        self.calls.append("seed_bundle")
        self.seed_sizes.append(len(resources))
        refused = [
            r["identifier"][0]["value"]
            for r in resources
            if r["resourceType"] == "Patient"
            and r["identifier"][0]["value"] in self.refuse_patients
        ]
        if refused:
            raise http_error(
                400,
                {
                    "resourceType": "OperationOutcome",
                    "issue": [{"diagnostics": "Patient.birthDate: invalid date"}],
                },
            )
        id_map = {}
        for r in resources:
            key = (r["identifier"][0]["system"], r["identifier"][0]["value"])
            self.ids.setdefault(key, f"{r['resourceType'].lower()}-{next(self._ids)}")
            id_map[key] = self.ids[key]
        return PersistResult("success", len(resources), 0, []), id_map

    # -- bulk scans ----------------------------------------------------
    def list_patient_ids(self) -> dict[str, str]:
        self.calls.append("list_patient_ids")
        return {v: i for (s, v), i in self.ids.items() if s == IDENTIFIER_SYSTEM}

    def list_document_keys(self) -> dict[tuple[str, str], str]:
        self.calls.append("list_document_keys")
        return dict(self.documents)

    def list_lab_result_keys(self) -> set[tuple[str, str]]:
        self.calls.append("list_lab_result_keys")
        return set(self.labs)

    # -- per-patient reads used by extract() ---------------------------
    def list_document_identifiers(self, patient: str | None = None) -> set[str]:
        return {d for p, d in self.documents if patient is None or p == patient}

    def get_patient(self, patient_id: str) -> dict:
        self.calls.append("get_patient")
        return {"resourceType": "Patient", "id": patient_id}

    def get_latest_document_date(self, patient_id: str) -> str | None:
        self.calls.append("get_latest_document_date")
        dates = [d for (p, _), d in self.documents.items() if p == patient_id]
        return max(dates, default=None)

    def fetch_patient_context(self, patient_id: str, reference_date: Any = None):
        return []

    def fetch_split_patient_context(self, patient_id: str, reference_date: Any = None):
        return [], []

    def find_encounter(self, patient_id: str, encounter_id: str):
        return None

    def find_encounter_strict(self, patient_id: str, encounter_id: str):
        key = ("urn:cavell:encounter", encounter_id)
        return {"id": self.ids[key]} if key in self.ids else None

    def find_practitioner_by_identifier(self, identifier: str):
        key = ("urn:cavell:practitioner", identifier)
        return {"id": self.ids[key]} if key in self.ids else None

    def find_patient_by_identifier(self, identifier: str):
        key = (IDENTIFIER_SYSTEM, identifier)
        return (self.ids[key], {}) if key in self.ids else None

    def deduplicate_observations(self, entries: list[dict], patient_id: str):
        return entries

    # -- writes ----------------------------------------------------------
    def post_bundle(self, entries: list[dict]) -> PersistResult:
        new_docs, new_labs = [], []
        for entry in entries:
            resource = entry["resource"]
            subject = resource["subject"]["reference"].split("/", 1)[1]
            for ident in resource.get("identifier", []):
                if ident["system"] == DOCUMENT_IDENTIFIER_SYSTEM:
                    if ident["value"] in self.reject_bundles_for:
                        return PersistResult(
                            "failed",
                            0,
                            0,
                            [
                                {
                                    "resource": "Condition",
                                    "error": "code: unknown system",
                                }
                            ],
                        )
                    new_docs.append(((subject, ident["value"]), resource["date"]))
                if ident["system"] == LAB_RESULT_IDENTIFIER_SYSTEM:
                    new_labs.append((subject, ident["value"]))
        created = len({k for k, _ in new_docs} - set(self.documents)) + len(
            {*new_labs} - self.labs
        )
        self.documents.update(new_docs)
        self.labs.update(new_labs)
        return PersistResult("success", created, len(entries) - created, [])


class FakeAPI:
    def __init__(self) -> None:
        #: document_id -> exception (or list of exceptions, one per attempt)
        self.failures: dict[str, Any] = {}
        #: document ids whose extraction comes back partial
        self.partial: set[str] = set()
        self.extracted: list[str] = []
        self.lab_requests: list[list[dict]] = []

    def check_connection(self) -> None:
        pass

    def extract_raw(
        self,
        *,
        text: str,
        patient_id: str,
        document_identifier: str,
        document_date: str,
        **kw,
    ):
        failure = self.failures.get(document_identifier)
        if isinstance(failure, list):
            failure = failure.pop(0) if failure else None
        if failure is not None:
            raise failure
        self.extracted.append(document_identifier)
        partial = document_identifier in self.partial
        return {
            "bundle": {
                "entry": [
                    {
                        "resource": {
                            "resourceType": "DocumentReference",
                            "subject": {"reference": f"Patient/{patient_id}"},
                            "date": document_date,
                            "identifier": [
                                {
                                    "system": DOCUMENT_IDENTIFIER_SYSTEM,
                                    "value": document_identifier,
                                }
                            ],
                        },
                        "request": {"method": "POST", "url": "DocumentReference"},
                    }
                ]
            },
            "count": 1,
            "usage": {"estimated_cost": 0.01},
            "extraction_status": "partial" if partial else "complete",
            "failed_extractors": ["example-step"] if partial else [],
        }

    def ingest_lab_results(self, *, patient_id: str, rows: list[dict]) -> dict:
        self.lab_requests.append(rows)
        return {
            "rejected": [],
            "bundle": {
                "entry": [
                    {
                        "resource": {
                            "resourceType": "Observation",
                            "subject": {"reference": f"Patient/{patient_id}"},
                            "identifier": [
                                {
                                    "system": LAB_RESULT_IDENTIFIER_SYSTEM,
                                    "value": row["lab_result_id"],
                                }
                            ],
                        }
                    }
                    for row in rows
                ]
            },
        }


def fake_client() -> Any:
    """Something pipelines accept as a client: ``_api`` and ``_fhir``."""
    return SimpleNamespace(_api=FakeAPI(), _fhir=FakeFHIR())


def prism_error(status: int, detail: str) -> CavellAPIError:
    return CavellAPIError(status, detail, {"detail": detail})
