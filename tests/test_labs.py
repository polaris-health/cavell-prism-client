"""Lab-results ingestion: row building, fail-closed resolution, persistence."""

import json
from urllib.parse import quote

import httpx
import pytest

from cavell_client import CavellAPIError, LabIngestionPipeline, LabResult
from cavell_client.fhir import (
    ENCOUNTER_IDENTIFIER_SYSTEM,
    IDENTIFIER_SYSTEM,
    PRACTITIONER_IDENTIFIER_SYSTEM,
)
from tests.helpers import API_URL, mock_api_preflight

PATIENT_SYSTEM_ENCODED = quote(IDENTIFIER_SYSTEM, safe="")
ENCOUNTER_SYSTEM_ENCODED = quote(ENCOUNTER_IDENTIFIER_SYSTEM, safe="")
PRACTITIONER_SYSTEM_ENCODED = quote(PRACTITIONER_IDENTIFIER_SYSTEM, safe="")

FHIR = "http://localhost:8080/fhir"


def _result(**overrides) -> LabResult:
    base = {
        "patient_identifier": "MRN-1",
        "test_name": "Potassium",
        "value": "4.2",
        "unit": "mmol/L",
        "collected_datetime": "2025-03-18T08:15:00+01:00",
        "lab_result_id": "LAB-1",
    }
    base.update(overrides)
    return LabResult(**base)


def mock_patient_lookup(httpx_mock, mrn, fhir_id, repeat=False):
    """Mock the identifier search find_patient_by_identifier runs."""
    entry = (
        [{"resource": {"resourceType": "Patient", "id": fhir_id}}] if fhir_id else []
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{FHIR}/Patient?identifier={PATIENT_SYSTEM_ENCODED}%7C{mrn}",
        json={"resourceType": "Bundle", "entry": entry},
        repeat=repeat,
    )


def mock_encounter_lookup(httpx_mock, patient_fhir_id, encounter_id, fhir_id, **kw):
    """Mock the patient-scoped Encounter identifier search."""
    entry = (
        [{"resource": {"resourceType": "Encounter", "id": fhir_id}}] if fhir_id else []
    )
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{FHIR}/Encounter?subject={patient_fhir_id}&_count=500"
            f"&identifier={ENCOUNTER_SYSTEM_ENCODED}%7C{quote(encounter_id, safe='')}"
        ),
        json={"resourceType": "Bundle", "entry": entry},
        **kw,
    )


def mock_practitioner_lookup(httpx_mock, identifier, fhir_id, **kw):
    """Mock the Practitioner identifier search."""
    entry = (
        [{"resource": {"resourceType": "Practitioner", "id": fhir_id}}]
        if fhir_id
        else []
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{FHIR}/Practitioner?identifier={PRACTITIONER_SYSTEM_ENCODED}%7C{identifier}",
        json={"resourceType": "Bundle", "entry": entry},
        **kw,
    )


def _api_response(lab_result_ids, rejected=()):
    """A canned /ingest/lab-results body: one bundle entry per id."""
    return {
        "bundle": {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "fullUrl": f"urn:uuid:{i}",
                    "resource": {"resourceType": "Observation"},
                    "request": {
                        "method": "POST",
                        "url": "Observation",
                        "ifNoneExist": f"identifier=urn:cavell:lab-result|{rid}",
                    },
                }
                for i, rid in enumerate(lab_result_ids)
            ],
        },
        "count": len(lab_result_ids),
        "rejected": list(rejected),
    }


def mock_ingest_endpoint(httpx_mock, body, **kw):
    httpx_mock.add_response(
        method="POST", url=f"{API_URL}/ingest/lab-results", json=body, **kw
    )


def mock_transaction(httpx_mock, statuses, **kw):
    """Mock the FHIR transaction POST with per-entry response statuses."""
    httpx_mock.add_response(
        method="POST",
        url=f"{FHIR}/",
        json={
            "resourceType": "Bundle",
            "type": "transaction-response",
            "entry": [{"response": {"status": s}} for s in statuses],
        },
        **kw,
    )


# --- LabResult.from_rows ---


def test_from_rows_maps_columns_applies_defaults():
    """Columns map by name, defaults apply to every row, blanks become None."""
    rows = [
        {
            "id": "LAB-1",
            "mrn": "MRN-1",
            "test": "Potassium",
            "loinc": "2823-3",
            "val": "4.2",
            "u": "mmol/L",
            "lo": "3,5",
            "hi": "5.0",
            "enc": "V-1",
            "doc": "",
            "at": "2025-03-18",
        }
    ]
    [result] = LabResult.from_rows(
        rows,
        columns={
            "lab_result_id": "id",
            "patient_identifier": "mrn",
            "test_name": "test",
            "loinc_code": "loinc",
            "value": "val",
            "unit": "u",
            "reference_low": "lo",
            "reference_high": "hi",
            "encounter_id": "enc",
            "practitioner_id": "doc",
            "collected_datetime": "at",
        },
        status="preliminary",
    )
    assert result.lab_result_id == "LAB-1"
    assert result.reference_low == 3.5  # European comma coerced
    assert result.reference_high == 5.0
    assert result.practitioner_id is None  # blank cell normalized away
    assert result.encounter_id == "V-1"
    assert result.status == "preliminary"


def test_from_rows_optional_field_can_be_disabled_with_none():
    """Mapping an optional field to None skips it without a column."""
    rows = [
        {"id": "LAB-1", "mrn": "MRN-1", "test": "K", "val": "4", "at": "2025-03-18"}
    ]
    [result] = LabResult.from_rows(
        rows,
        columns={
            "lab_result_id": "id",
            "patient_identifier": "mrn",
            "test_name": "test",
            "value": "val",
            "collected_datetime": "at",
            "loinc_code": None,
        },
    )
    assert result.loinc_code is None


def test_from_rows_unknown_field_raises():
    """A typo'd field name fails loudly instead of being dropped."""
    with pytest.raises(ValueError, match="Unknown LabResult field"):
        LabResult.from_rows(
            [{"id": "1"}],
            columns={
                "lab_result_id": "id",
                "patient_identifier": "id",
                "test_name": "id",
                "value": "id",
                "collected_datetime": "id",
                "loinc": "id",
            },
        )


def test_from_rows_required_mapping_cannot_be_disabled():
    """None only disables optional fields; required ones refuse it."""
    with pytest.raises(ValueError, match="'value' — it is a required"):
        LabResult.from_rows(
            [{"id": "1"}],
            columns={
                "lab_result_id": "id",
                "patient_identifier": "id",
                "test_name": "id",
                "value": None,
                "collected_datetime": "id",
            },
        )


# --- pipeline ---


def test_ingest_happy_path(httpx_mock, client):
    """References resolve, payload carries resolved FHIR ids, bundle persists."""
    mock_api_preflight(httpx_mock)
    mock_patient_lookup(httpx_mock, "MRN-1", "pat-1")
    mock_encounter_lookup(httpx_mock, "pat-1", "V-1", "enc-9")
    mock_practitioner_lookup(httpx_mock, "DOC-1", "prac-9")
    mock_ingest_endpoint(httpx_mock, _api_response(["LAB-1", "LAB-2"]))
    mock_transaction(httpx_mock, ["201 Created", "201 Created"])

    pipeline = LabIngestionPipeline(client)
    outcome = pipeline.ingest(
        [
            _result(
                lab_result_id="LAB-1",
                encounter_id="V-1",
                practitioner_id="DOC-1",
                loinc_code="2823-3",
                reference_low=3.5,
                reference_high=5.0,
            ),
            _result(
                lab_result_id="LAB-2", test_name="Blood culture", value="No growth"
            ),
        ]
    )

    assert outcome.total == 2
    assert outcome.accepted == 2
    assert outcome.created == 2
    assert outcome.skipped_existing == 0
    assert outcome.rejected == []
    assert outcome.success
    [(mrn, persist)] = outcome.persistence
    assert mrn == "MRN-1"
    assert persist.created == 2

    ingest_request = next(
        r
        for r in httpx_mock.get_requests()
        if str(r.url).endswith("/ingest/lab-results")
    )
    payload = json.loads(ingest_request.content)
    assert payload["patient_id"] == "pat-1"
    assert payload["rows"][0] == {
        "lab_result_id": "LAB-1",
        "test_name": "Potassium",
        "value": "4.2",
        "collected_datetime": "2025-03-18T08:15:00+01:00",
        "status": "final",
        "loinc_code": "2823-3",
        "unit": "mmol/L",
        "reference_low": 3.5,
        "reference_high": 5.0,
        "encounter_id": "enc-9",  # resolved FHIR id, not "V-1"
        "practitioner_id": "prac-9",  # resolved FHIR id, not "DOC-1"
    }
    assert payload["rows"][1] == {
        "lab_result_id": "LAB-2",
        "test_name": "Blood culture",
        "value": "No growth",
        "collected_datetime": "2025-03-18T08:15:00+01:00",
        "status": "final",
        "unit": "mmol/L",
    }


def test_unknown_patient_rejects_all_rows_without_api_call(httpx_mock, client):
    """A patient missing from FHIR rejects every row referencing it."""
    mock_api_preflight(httpx_mock)
    mock_patient_lookup(httpx_mock, "MRN-404", None)

    outcome = LabIngestionPipeline(client).ingest(
        [
            _result(patient_identifier="MRN-404", lab_result_id="LAB-1"),
            _result(patient_identifier="MRN-404", lab_result_id="LAB-2"),
        ]
    )

    assert outcome.accepted == 0
    assert outcome.persistence == []
    assert [(r.index, r.stage, r.reason) for r in outcome.rejected] == [
        (0, "reference", "unknown patient 'MRN-404'"),
        (1, "reference", "unknown patient 'MRN-404'"),
    ]
    assert not any(
        str(r.url).endswith("/ingest/lab-results") for r in httpx_mock.get_requests()
    )


def test_unknown_encounter_rejects_only_rows_carrying_it(httpx_mock, client):
    """A missing encounter rejects its rows; the patient's others proceed."""
    mock_api_preflight(httpx_mock)
    mock_patient_lookup(httpx_mock, "MRN-1", "pat-1")
    mock_encounter_lookup(httpx_mock, "pat-1", "V-404", None)
    mock_ingest_endpoint(httpx_mock, _api_response(["LAB-2"]))
    mock_transaction(httpx_mock, ["201 Created"])

    outcome = LabIngestionPipeline(client).ingest(
        [
            _result(lab_result_id="LAB-1", encounter_id="V-404"),
            _result(lab_result_id="LAB-2"),
        ]
    )

    assert outcome.accepted == 1
    [rejection] = outcome.rejected
    assert rejection.index == 0
    assert rejection.stage == "reference"
    assert rejection.reason == "unknown encounter 'V-404' for patient 'MRN-1'"


def test_unknown_practitioner_rejects_only_carrying_rows(httpx_mock, client):
    """A missing practitioner rejects its rows; the lookup is cached globally."""
    mock_api_preflight(httpx_mock)
    mock_patient_lookup(httpx_mock, "MRN-1", "pat-1")
    mock_patient_lookup(httpx_mock, "MRN-2", "pat-2")
    # One registration, no repeat: a second GET would fail the test, proving
    # the cache spans patients.
    mock_practitioner_lookup(httpx_mock, "DOC-404", None)
    mock_ingest_endpoint(httpx_mock, _api_response(["LAB-2"]))
    mock_ingest_endpoint(httpx_mock, _api_response(["LAB-4"]))
    mock_transaction(httpx_mock, ["201 Created"], repeat=True)

    outcome = LabIngestionPipeline(client).ingest(
        [
            _result(lab_result_id="LAB-1", practitioner_id="DOC-404"),
            _result(lab_result_id="LAB-2"),
            _result(
                patient_identifier="MRN-2",
                lab_result_id="LAB-3",
                practitioner_id="DOC-404",
            ),
            _result(patient_identifier="MRN-2", lab_result_id="LAB-4"),
        ]
    )

    assert outcome.accepted == 2
    assert [(r.index, r.reason) for r in outcome.rejected] == [
        (0, "unknown practitioner 'DOC-404'"),
        (2, "unknown practitioner 'DOC-404'"),
    ]
    practitioner_lookups = [
        r for r in httpx_mock.get_requests() if "/Practitioner?" in str(r.url)
    ]
    assert len(practitioner_lookups) == 1


def test_fhir_error_on_encounter_lookup_aborts(httpx_mock, client):
    """Infrastructure trouble is not bad data: a lookup error propagates."""
    mock_api_preflight(httpx_mock)
    mock_patient_lookup(httpx_mock, "MRN-1", "pat-1")
    mock_encounter_lookup(httpx_mock, "pat-1", "V-1", None, status_code=500)

    with pytest.raises(httpx.HTTPStatusError):
        LabIngestionPipeline(client).ingest(
            [_result(lab_result_id="LAB-1", encounter_id="V-1")]
        )


def test_row_validation_rejections(httpx_mock, client):
    """Content problems reject rows before any FHIR or API traffic."""
    mock_api_preflight(httpx_mock)
    mock_patient_lookup(httpx_mock, "MRN-1", "pat-1")
    mock_ingest_endpoint(httpx_mock, _api_response(["LAB-1"]))
    mock_transaction(httpx_mock, ["201 Created"])

    outcome = LabIngestionPipeline(client).ingest(
        [
            _result(lab_result_id="LAB-1"),
            _result(lab_result_id="LAB-2", value="   "),
            _result(lab_result_id="LAB-3", reference_low="high-ish"),
            _result(lab_result_id="LAB-4", status="bogus"),
            _result(lab_result_id="LAB-1", value="9.9"),  # duplicate id
        ]
    )

    assert outcome.accepted == 1
    assert [(r.index, r.stage) for r in outcome.rejected] == [
        (1, "validation"),
        (2, "validation"),
        (3, "validation"),
        (4, "validation"),
    ]
    reasons = [r.reason for r in outcome.rejected]
    assert reasons[0] == "value must be non-empty"
    assert reasons[1] == "reference_low is not numeric: 'high-ish'"
    assert "status must be one of" in reasons[2]
    assert reasons[3] == "duplicate lab_result_id (first used by row 0)"


def test_server_rejections_mapped_to_original_indices(httpx_mock, client):
    """The API's request-relative indexes come back input-relative."""
    mock_api_preflight(httpx_mock)
    mock_patient_lookup(httpx_mock, "MRN-1", "pat-1")
    # Input row 0 fails validation, so request row 0 is input row 1 and
    # request row 1 is input row 2 — the API rejects its row 1.
    mock_ingest_endpoint(
        httpx_mock,
        _api_response(
            ["LAB-2"],
            rejected=[
                {
                    "index": 1,
                    "lab_result_id": "LAB-3",
                    "reason": "collected_datetime must be YYYY-MM-DD or a full "
                    "ISO datetime with a timezone offset: '2025-03-18T08:15:00'",
                }
            ],
        ),
    )
    mock_transaction(httpx_mock, ["201 Created"])

    outcome = LabIngestionPipeline(client).ingest(
        [
            _result(lab_result_id="LAB-1", value=""),
            _result(lab_result_id="LAB-2"),
            _result(lab_result_id="LAB-3", collected_datetime="2025-03-18T08:15:00"),
        ]
    )

    assert outcome.accepted == 1
    server_rejection = next(r for r in outcome.rejected if r.stage == "server")
    assert server_rejection.index == 2  # input-relative, not the API's 1
    assert server_rejection.lab_result_id == "LAB-3"
    assert "timezone offset" in server_rejection.reason


def test_rerun_counts_skipped_existing(httpx_mock, client):
    """200s from conditional creates surface as skipped_existing, not created."""
    mock_api_preflight(httpx_mock)
    mock_patient_lookup(httpx_mock, "MRN-1", "pat-1")
    mock_ingest_endpoint(httpx_mock, _api_response(["LAB-1", "LAB-2"]))
    mock_transaction(httpx_mock, ["200 OK", "200 OK"])

    outcome = LabIngestionPipeline(client).ingest(
        [_result(lab_result_id="LAB-1"), _result(lab_result_id="LAB-2")]
    )

    assert outcome.accepted == 2
    assert outcome.created == 0
    assert outcome.skipped_existing == 2
    assert outcome.success
    assert "2 already present" in str(outcome)


def test_ingest_requires_lab_result_objects(client):
    """Raw CSV rows fail up front with a pointed message."""
    with pytest.raises(TypeError, match="not LabResult"):
        rows = [{"lab_result_id": "LAB-1"}]
        LabIngestionPipeline(client).ingest(rows)  # ty: ignore[invalid-argument-type]


def test_all_rows_rejected_skips_api_and_fhir(httpx_mock, client):
    """Nothing valid to send means no ingest call and no transaction."""
    mock_api_preflight(httpx_mock)

    outcome = LabIngestionPipeline(client).ingest([_result(value="")])

    assert outcome.accepted == 0
    assert outcome.persistence == []
    assert len(outcome.rejected) == 1
    paths = [str(r.url) for r in httpx_mock.get_requests()]
    assert all(
        "/ingest/lab-results" not in p and not p.endswith("/fhir/") for p in paths
    )


# --- CavellAPI.ingest_lab_results ---


def test_api_ingest_lab_results_retries_on_429(httpx_mock, client):
    """The shared 429 contract applies: Retry-After honoured, then success."""
    httpx_mock.add_response(
        method="POST",
        url=f"{API_URL}/ingest/lab-results",
        status_code=429,
        headers={"Retry-After": "0"},
        json={"detail": "rate limited"},
    )
    mock_ingest_endpoint(httpx_mock, _api_response(["LAB-1"]))

    body = client._api.ingest_lab_results(patient_id="pat-1", rows=[{"x": 1}])
    assert body["count"] == 1


def test_api_ingest_404_names_the_missing_route(httpx_mock, client):
    """A 404 explains the deployment predates lab ingestion."""
    httpx_mock.add_response(
        method="POST",
        url=f"{API_URL}/ingest/lab-results",
        status_code=404,
        json={"detail": "Not Found"},
    )

    with pytest.raises(CavellAPIError, match="predates structured lab ingestion"):
        client._api.ingest_lab_results(patient_id="pat-1", rows=[{"x": 1}])
