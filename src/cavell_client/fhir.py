"""Local FHIR server operations with OAuth2 authentication."""

import json
import logging
import threading
from collections.abc import Iterator, Sequence
from typing import Any
from urllib.parse import urlparse

import httpx

from cavell_client.models import FHIRAuthError, PatientNotFoundError, PersistResult

logger = logging.getLogger(__name__)

# Patient identifier system for linking resources
IDENTIFIER_SYSTEM = "urn:cavell:patient"
DOCUMENT_IDENTIFIER_SYSTEM = "urn:cavell:document"
ORGANIZATION_IDENTIFIER_SYSTEM = "urn:cavell:organization"
PRACTITIONER_IDENTIFIER_SYSTEM = "urn:cavell:practitioner"
PRACTITIONER_ROLE_IDENTIFIER_SYSTEM = "urn:cavell:practitioner-role"

# Default resource types to fetch for context
CONTEXT_RESOURCE_TYPES: tuple[str, ...] = (
    "Condition",
    "AllergyIntolerance",
    "Procedure",
    "MedicationRequest",
    "Observation",
    "ResearchSubject",
    "CarePlan",
)

# FHIR search parameter that scopes a resource to a patient. Most resources use
# "subject"; a few use a different reference field.
_PATIENT_SEARCH_PARAM: dict[str, str] = {
    "AllergyIntolerance": "patient",
    "ResearchSubject": "individual",
}

# Observations can be voluminous; only the most recent ones are sent as context.
MAX_CONTEXT_OBSERVATIONS = 50


def _observation_signature(obs: dict) -> tuple[str, str, str] | None:
    """Build (date, code, value) signature for deduplication.

    Returns None if observation lacks required fields.
    """
    date = obs.get("effectiveDateTime", "")[:10]
    if not date:
        return None

    code_obj = obs.get("code", {})
    codings = code_obj.get("coding", [])
    if codings:
        code = f"{codings[0].get('system', '')}|{codings[0].get('code', '')}"
    else:
        code = code_obj.get("text", "")
    if not code:
        return None

    if vq := obs.get("valueQuantity"):
        value = f"{vq.get('value')}|{vq.get('unit', '')}"
    elif vcc := obs.get("valueCodeableConcept"):
        value = vcc.get("text", "")
    elif vs := obs.get("valueString"):
        value = vs
    else:
        value = ""

    return (date, code, value)


def _filter_stale_refs(entries: list[dict]) -> list[dict]:
    """Remove references to resources no longer in the bundle.

    After deduplication, DocumentReference and Encounter entries may reference
    observations that were removed. This filters those dangling references.
    """
    valid_refs = {e["fullUrl"] for e in entries if "fullUrl" in e}

    def _is_valid_ref(ref_str: str) -> bool:
        return not ref_str.startswith("urn:uuid:") or ref_str in valid_refs

    for entry in entries:
        resource = entry.get("resource", {})

        if resource.get("resourceType") == "DocumentReference":
            context = resource.get("context", {})
            related = context.get("related", [])
            if not related:
                continue
            filtered = [
                ref for ref in related if _is_valid_ref(ref.get("reference", ""))
            ]
            if len(filtered) < len(related):
                removed = len(related) - len(filtered)
                logger.debug(f"Removed {removed} stale ref(s) from DocumentReference")
                if filtered:
                    context["related"] = filtered
                else:
                    del context["related"]
                    if not context:
                        del resource["context"]

        elif resource.get("resourceType") == "Encounter":
            reason_refs = resource.get("reasonReference", [])
            if not reason_refs:
                continue
            filtered = [
                ref for ref in reason_refs if _is_valid_ref(ref.get("reference", ""))
            ]
            if len(filtered) < len(reason_refs):
                removed = len(reason_refs) - len(filtered)
                logger.debug(
                    f"Removed {removed} stale ref(s) from Encounter.reasonReference"
                )
                if filtered:
                    resource["reasonReference"] = filtered
                else:
                    del resource["reasonReference"]

    return entries


def _dedupe_entries_by_id(entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Drop transaction entries that collide on FHIR logical id.

    HAPI rejects a whole transaction Bundle (HAPI-0535) if two entries carry
    the same (resourceType, id). The extraction service occasionally emits the
    same existing resource twice (parallel general+radiology extractors). Keep
    the FIRST entry for each (resourceType, id) and drop later collisions so the
    rest of the transaction can still persist.

    NOTE: defensive client-side guard only; the proper field-level merge belongs
    upstream in the extraction service. Diverging fields on dropped copies (e.g.
    a second bodySite) are lost for this run.

    Entries without a logical id (creates with urn:uuid fullUrls) are never
    deduped and always kept. Returns (deduped_entries, dropped_keys) where
    dropped_keys lists the string id-keys dropped (e.g. "Procedure/5380").
    """

    def _id_key(entry: dict) -> str | None:
        resource = entry.get("resource", {})
        rtype = resource.get("resourceType")
        rid = resource.get("id")
        if rtype and rid:
            return f"{rtype}/{rid}"
        # Fallback: PUT-with-id request.url ("Procedure/5380"); never a create
        # ("Procedure") or conditional url ("Procedure?identifier=x|y").
        url = entry.get("request", {}).get("url", "")
        if url and "/" in url and "?" not in url:
            return url
        return None

    seen: set[str] = set()
    deduped: list[dict] = []
    dropped: list[str] = []
    for entry in entries:
        key = _id_key(entry)
        if key is None:
            deduped.append(entry)
            continue
        if key in seen:
            dropped.append(key)
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped, dropped


class FHIRClient:
    """Client for local FHIR server operations."""

    def __init__(
        self,
        base_url: str,
        client_id: str | None = None,
        client_secret: str | None = None,
        api_path: str = "/fhir",
    ):
        """Initialize FHIR client.

        Args:
            base_url: FHIR server base URL (e.g., "http://localhost:8080")
            client_id: OAuth2 client ID (None to skip auth)
            client_secret: OAuth2 client secret (None to skip auth)
            api_path: API path prefix (e.g., "/fhir" for Aidbox, "" for HAPI)
        """
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_path = api_path.rstrip("/") if api_path else ""
        if (client_id is None) != (client_secret is None):
            raise ValueError("Provide both client_id and client_secret, or neither")
        self._no_auth = client_id is None

        self._access_token: str | None = None
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()

    def _get_access_token(self) -> str:
        """Get OAuth2 access token using client credentials flow."""
        if self._access_token:
            return self._access_token

        token_url = f"{self.base_url}/auth/token"

        try:
            with httpx.Client() as client:
                response = client.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
            response.raise_for_status()
            data = response.json()
            token = data.get("access_token")
            if not token:
                raise FHIRAuthError(f"Token response missing 'access_token': {data}")
            self._access_token = token
            return self._access_token
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            raise FHIRAuthError(f"Token request failed: {status}") from e
        except Exception as e:
            raise FHIRAuthError(str(e)) from e

    def _get_client(self) -> httpx.Client:
        """Get or create the authenticated FHIR client."""
        if self._client is not None:
            return self._client

        with self._client_lock:
            if self._client is not None:
                return self._client

            headers = {}
            if not self._no_auth:
                token = self._get_access_token()
                headers["Authorization"] = f"Bearer {token}"
            self._client = httpx.Client(
                base_url=f"{self.base_url}{self.api_path}",
                headers=headers,
                timeout=30.0,
            )
            return self._client

    def _clear_auth(self, close_client: bool = True) -> None:
        """Clear cached token and client (forces re-auth on next request).

        With ``close_client=False`` the old client object is only dereferenced,
        not closed — used on the 401-refresh path, where other worker threads
        may still have in-flight requests on it (closing would raise on them).
        """
        with self._client_lock:
            self._access_token = None
            if self._client:
                if close_client:
                    self._client.close()
                self._client = None

    def close(self) -> None:
        """Close the HTTP client."""
        self._clear_auth()

    def check_connection(self) -> None:
        """Ping the FHIR server's metadata endpoint.

        Raises on any failure (connection refused, auth error, bad URL).
        """
        self._make_request("GET", "/metadata")

    def _make_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make authenticated request to FHIR server.

        Automatically retries once on 401 (token expired).
        Bypasses server-side search caches on GET requests.
        """
        # Bypass HAPI's search result cache so we always get fresh results.
        if method.upper() == "GET":
            headers = kwargs.pop("headers", {})
            headers["Cache-Control"] = "no-cache"
            kwargs["headers"] = headers

        client = self._get_client()
        response = client.request(method, path, **kwargs)

        # Retry once on 401 (token expired)
        if response.status_code == 401:
            logger.info("FHIR token expired, refreshing")
            self._clear_auth(close_client=False)
            client = self._get_client()
            response = client.request(method, path, **kwargs)

        response.raise_for_status()
        return response

    def _iter_bundle_entries(
        self, method: str, path: str, **kwargs: Any
    ) -> Iterator[dict]:
        """Yield bundle entries, following FHIR pagination links."""
        response = self._make_request(method, path, **kwargs)
        bundle = response.json()

        while True:
            yield from bundle.get("entry", [])

            next_url = None
            for link in bundle.get("link", []):
                if link.get("relation") == "next":
                    next_url = link["url"]
                    break
            if not next_url:
                break

            response = self._make_request("GET", next_url)
            bundle = response.json()

    def get_patient_everything(self, patient_id: str) -> list[dict]:
        """Fetch all resources for a patient via $everything.

        Follows pagination links to collect all pages.

        Args:
            patient_id: FHIR Patient.id

        Returns:
            List of FHIR resources (excluding the Patient itself)
        """
        return [
            resource
            for entry in self._iter_bundle_entries(
                "GET", f"/Patient/{patient_id}/$everything"
            )
            if (resource := entry.get("resource", {})).get("resourceType") != "Patient"
        ]

    def delete_patient_resources(self, patient_id: str) -> None:
        """Delete a patient and all resources referencing them.

        Uses HAPI's cascade delete to walk the reference graph.
        Requires ``allow_cascading_deletes=true`` in HAPI config.

        Args:
            patient_id: FHIR Patient.id to delete
        """
        self._make_request(
            "DELETE", f"/Patient/{patient_id}", params={"_cascade": "delete"}
        )

    def count_resources(self, resource_type: str) -> int:
        """Count resources of a given type on the FHIR server.

        Args:
            resource_type: FHIR resource type (e.g., "Patient", "Condition")

        Returns:
            Total count
        """
        response = self._make_request(
            "GET", f"/{resource_type}", params={"_summary": "count"}
        )
        return response.json().get("total", 0)

    def list_document_identifiers(self, patient: str | None = None) -> set[str]:
        """Return the set of document identifiers from FHIR DocumentReferences.

        Queries DocumentReference resources filtered by identifier system
        and collects ``identifier[].value`` where
        ``system == DOCUMENT_IDENTIFIER_SYSTEM``.

        Args:
            patient: FHIR Patient.id to scope the query to. Without it the
                whole server is scanned — on a shared server prefer the
                patient-scoped form.
        """
        params = {} if patient is None else {"patient": patient}
        params.update(
            {
                "identifier": f"{DOCUMENT_IDENTIFIER_SYSTEM}|",
                "_elements": "identifier",
                "_count": "1000",
            }
        )
        document_ids: set[str] = set()
        for entry in self._iter_bundle_entries(
            "GET", "/DocumentReference", params=params
        ):
            for ident in entry.get("resource", {}).get("identifier", []):
                if ident.get("system") == DOCUMENT_IDENTIFIER_SYSTEM:
                    document_ids.add(ident["value"])
        return document_ids

    def get_latest_document_date(self, patient_id: str) -> str | None:
        """Return the date (YYYY-MM-DD) of the newest processed document.

        Queries the patient's pipeline-created DocumentReferences sorted by
        date descending. Returns None when the patient has no processed
        documents (or the newest one carries no date).
        """
        response = self._make_request(
            "GET",
            "/DocumentReference",
            params={
                "patient": patient_id,
                "identifier": f"{DOCUMENT_IDENTIFIER_SYSTEM}|",
                "_sort": "-date",
                "_count": "1",
                "_elements": "date",
            },
        )
        entries = response.json().get("entry", [])
        if not entries:
            return None
        date = entries[0].get("resource", {}).get("date", "")
        return date[:10] or None

    def get_related_document_dates(self, patient_id: str) -> dict[str, str]:
        """Map each resource a document touched to its newest source-document date.

        Walks the patient's pipeline-created DocumentReferences and their
        ``context.related`` references (the resources each document created or
        updated). Returns {"Type/id": "YYYY-MM-DD"} keeping the newest date
        per resource — the provenance the update guard compares against.
        """
        related_dates: dict[str, str] = {}
        for entry in self._iter_bundle_entries(
            "GET",
            "/DocumentReference",
            params={
                "patient": patient_id,
                "identifier": f"{DOCUMENT_IDENTIFIER_SYSTEM}|",
                "_elements": "date,context",
                "_count": "1000",
            },
        ):
            resource = entry.get("resource", {})
            date = resource.get("date", "")[:10]
            if not date:
                continue
            for related in resource.get("context", {}).get("related", []):
                ref = related.get("reference", "")
                if not ref or ref.startswith("urn:"):
                    continue
                # Absolute references reduce to their last two path segments.
                segments = ref.split("/")
                if len(segments) < 2:
                    continue
                key = f"{segments[-2]}/{segments[-1]}"
                if date > related_dates.get(key, ""):
                    related_dates[key] = date
        return related_dates

    def get_resource(self, resource_type: str, resource_id: str) -> dict:
        """Fetch a single resource by type and id."""
        response = self._make_request("GET", f"/{resource_type}/{resource_id}")
        return response.json()

    def delete_meta_tags(
        self, resource_type: str, resource_id: str, tags: list[tuple[str, str]]
    ) -> None:
        """Remove meta.tag entries from a resource via $meta-delete.

        Args:
            resource_type: FHIR resource type (e.g., "Condition")
            resource_id: FHIR logical id
            tags: (system, code) pairs to remove
        """
        self._make_request(
            "POST",
            f"/{resource_type}/{resource_id}/$meta-delete",
            json={
                "resourceType": "Parameters",
                "parameter": [
                    {
                        "name": "meta",
                        "valueMeta": {
                            "tag": [
                                {"system": system, "code": code}
                                for system, code in tags
                            ]
                        },
                    }
                ],
            },
        )

    def search_patient_resources(
        self,
        patient_id: str,
        resource_type: str,
        params: dict[str, str] | None = None,
        max_results: int | None = None,
    ) -> list[dict]:
        """Search for FHIR resources belonging to a patient.

        Args:
            patient_id: The patient ID to search for
            resource_type: FHIR resource type (e.g., "Condition")
            params: Additional query parameters (e.g., {"date": "2023-03-15"})
            max_results: Stop after collecting this many resources, without
                fetching further pages (None = fetch all pages)

        Returns:
            List of matching FHIR resources
        """
        param_name = _PATIENT_SEARCH_PARAM.get(resource_type, "subject")
        page_size = min(max_results, 500) if max_results else 500
        query = {param_name: patient_id, "_count": str(page_size)}
        if params:
            query.update(params)

        results: list[dict] = []
        for entry in self._iter_bundle_entries(
            "GET", f"/{resource_type}", params=query
        ):
            results.append(entry["resource"])
            if max_results is not None and len(results) >= max_results:
                break
        return results

    def search_research_studies(self, status: str = "active") -> list[dict]:
        """Search for ResearchStudy resources by status.

        ResearchStudy resources are not patient-scoped: every study with the
        given status applies to all patients, so they are always included in
        the extraction context for deduplication.

        Args:
            status: ResearchStudy.status to filter by (default "active")

        Returns:
            List of matching ResearchStudy resources
        """
        return [
            entry["resource"]
            for entry in self._iter_bundle_entries(
                "GET", "/ResearchStudy", params={"status": status, "_count": "500"}
            )
        ]

    def search_practitioners(
        self,
        identifier: str | None = None,
        family_name: str | None = None,
        given_name: str | None = None,
        organization_id: str | None = None,
    ) -> list[dict]:
        """Search for Practitioner resources.

        Args:
            identifier: Practitioner identifier value
            family_name: Family name to search
            given_name: Given name to search
            organization_id: FHIR Organization ID to scope via PractitionerRole

        Returns:
            List of matching Practitioner resources
        """
        params: dict[str, str] = {}

        if identifier:
            params["identifier"] = f"{PRACTITIONER_IDENTIFIER_SYSTEM}|{identifier}"
        else:
            if family_name:
                params["family"] = family_name
            if given_name:
                params["given"] = given_name
            if organization_id:
                params["_has:PractitionerRole:practitioner:organization"] = (
                    f"Organization/{organization_id}"
                )

        response = self._make_request("GET", "/Practitioner", params=params)
        bundle = response.json()
        return [entry["resource"] for entry in bundle.get("entry", [])]

    def fetch_patient_context(
        self,
        patient_id: str,
        resource_types: Sequence[str] = CONTEXT_RESOURCE_TYPES,
    ) -> list[dict]:
        """Fetch existing FHIR resources to use as extraction context.

        Includes the patient-scoped ``resource_types`` plus all active
        ResearchStudy resources, which are not patient-scoped but always
        provided as context for deduplication.

        Args:
            patient_id: The patient ID to fetch context for
            resource_types: Patient-scoped resource types to fetch

        Returns:
            List of FHIR resources to use as context
        """
        all_resources: list[dict] = []
        for resource_type in resource_types:
            try:
                if resource_type == "Observation":
                    # Cap to the most recent observations, sorted newest-first.
                    results = self.search_patient_resources(
                        patient_id,
                        resource_type,
                        params={"_sort": "-date"},
                        max_results=MAX_CONTEXT_OBSERVATIONS,
                    )
                elif resource_type == "CarePlan":
                    # Only active plans can be meaningfully updated; ended
                    # plans would grow the context forever for no benefit.
                    results = self.search_patient_resources(
                        patient_id,
                        resource_type,
                        params={"status": "active"},
                    )
                else:
                    results = self.search_patient_resources(patient_id, resource_type)
                logger.debug(
                    f"Fetched {len(results)} {resource_type} for patient {patient_id}"
                )
                all_resources.extend(results)
            except Exception as e:
                logger.warning(
                    f"Failed to fetch {resource_type} for patient {patient_id}: {e}"
                )

        # Active studies apply to all patients, so always include them.
        try:
            studies = self.search_research_studies(status="active")
            logger.debug(f"Fetched {len(studies)} active ResearchStudy for context")
            all_resources.extend(studies)
        except Exception as e:
            logger.warning(f"Failed to fetch active ResearchStudy: {e}")

        # Server bookkeeping (meta) and generated narrative (text) carry no
        # extraction signal; stripping them keeps the context payload small.
        for resource in all_resources:
            resource.pop("meta", None)
            resource.pop("text", None)

        return all_resources

    def find_patient_by_identifier(self, identifier: str) -> tuple[str, dict] | None:
        """Search for Patient by identifier, return (id, resource) if found.

        Args:
            identifier: Hospital's patient identifier (MRN)

        Returns:
            Tuple of (Patient.id, Patient resource) if found, None otherwise
        """
        response = self._make_request(
            "GET",
            "/Patient",
            params={"identifier": f"{IDENTIFIER_SYSTEM}|{identifier}"},
        )
        bundle = response.json()
        entries = bundle.get("entry", [])
        if not entries:
            logger.debug(f"Patient not found for identifier: {identifier}")
            return None
        resource = entries[0]["resource"]
        return resource["id"], resource

    def get_patient(self, patient_id: str) -> dict:
        """Get a Patient resource by ID.

        Args:
            patient_id: FHIR Patient.id

        Returns:
            Patient resource dict

        Raises:
            PatientNotFoundError: If patient not found
        """
        try:
            response = self._make_request("GET", f"/Patient/{patient_id}")
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 410):
                raise PatientNotFoundError(patient_id) from None
            raise

    def create_patient(self, identifier: str | None = None) -> tuple[str, dict]:
        """Create a new Patient resource.

        Args:
            identifier: Optional hospital patient identifier (MRN)

        Returns:
            Tuple of (patient_id, patient_resource)
        """
        patient: dict = {"resourceType": "Patient"}
        if identifier:
            patient["identifier"] = [{"system": IDENTIFIER_SYSTEM, "value": identifier}]

        response = self._make_request("POST", "/Patient", json=patient)
        created = response.json()
        return created["id"], created

    def ensure_patient(
        self,
        identifier: str | None,
        patient_id: str | None,
    ) -> tuple[str, dict]:
        """Resolve or create patient, return (patient_id, patient_resource).

        Resolution flow:
        1. If patient_id provided -> verify exists, return it
        2. If identifier provided -> search, create if not found
        3. If neither -> create new Patient

        Args:
            identifier: Hospital's patient identifier (MRN)
            patient_id: FHIR Patient.id from previous call

        Returns:
            Tuple of (patient_id, patient_resource)

        Raises:
            PatientNotFoundError: If patient_id not found
        """
        # Case 1: patient_id provided - verify exists
        if patient_id:
            patient = self.get_patient(patient_id)
            return patient_id, patient

        # Case 2: identifier provided - search first
        if identifier:
            result = self.find_patient_by_identifier(identifier)
            if result:
                return result

            # Not found - create new patient with identifier
            return self.create_patient(identifier)

        # Case 3: Neither - create new patient
        return self.create_patient()

    def post_bundle(self, entries: list[dict]) -> PersistResult:
        """Post a FHIR transaction Bundle and return result.

        Uses transaction (not batch) so the server resolves urn:uuid: references
        between resources in the same bundle.

        Args:
            entries: List of Bundle entries

        Returns:
            PersistResult with created/updated counts and errors
        """
        if not entries:
            return PersistResult(status="success", created=0, updated=0, errors=[])

        entries, dropped_ids = _dedupe_entries_by_id(entries)
        if dropped_ids:
            logger.warning(
                f"Dropped {len(dropped_ids)} duplicate bundle entry(ies) "
                f"colliding on logical id: {', '.join(dropped_ids)}"
            )

        bundle = {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": entries,
        }

        # Route through _make_request so an expired OAuth token refreshes
        # instead of surfacing as a deterministic persist failure.
        try:
            response = self._make_request("POST", "/", json=bundle)
        except httpx.HTTPStatusError as e:
            response = e.response

        # Transaction failure returns OperationOutcome
        if response.status_code >= 400:
            try:
                outcome = response.json()
            except Exception:
                outcome = {}
            error_msg = self._extract_error_message(outcome, str(response.status_code))
            if logger.isEnabledFor(logging.DEBUG):
                for i, entry in enumerate(entries):
                    res = entry.get("resource", {})
                    rtype = res.get("resourceType", "Unknown")
                    full_url = entry.get("fullUrl", "no-fullUrl")
                    logger.debug(
                        f"Bundle entry {i}: {rtype} ({full_url})\n{json.dumps(res)}"
                    )
            logger.error(f"Transaction failed: {error_msg}")
            return PersistResult(
                status="failed",
                created=0,
                updated=0,
                errors=[{"resource": "Bundle", "error": error_msg}],
            )

        response_bundle = response.json()
        created = 0
        updated = 0
        errors: list[dict] = []

        for original, response_entry in zip(
            entries, response_bundle.get("entry", []), strict=False
        ):
            response_info = response_entry.get("response", {})
            status = response_info.get("status", "")
            if status.startswith("2"):
                # 201 = created, 200 = updated
                if status.startswith("201"):
                    created += 1
                else:
                    updated += 1
            else:
                outcome = (
                    response_entry.get("resource", {})
                    if response_entry.get("resource", {}).get("resourceType")
                    == "OperationOutcome"
                    else response_info.get("outcome", {})
                )
                error_msg = self._extract_error_message(outcome, status)
                resource_type = original.get("resource", {}).get(
                    "resourceType", "Unknown"
                )
                logger.warning(f"FHIR {status} for {resource_type}: {error_msg}")
                errors.append({"resource": resource_type, "error": error_msg})

        if errors:
            status = "partial_failure" if created + updated > 0 else "failed"
        else:
            status = "success"

        return PersistResult(
            status=status, created=created, updated=updated, errors=errors
        )

    def seed_bundle(
        self, resources: list[dict]
    ) -> tuple[PersistResult, dict[tuple[str, str], str]]:
        """Seed resources via conditional PUT (upsert) and return id_map.

        Each resource must have an identifier[0] with system and value.
        Uses PUT with ?identifier= query to create-or-update.

        Args:
            resources: List of FHIR resources with identifiers

        Returns:
            Tuple of (PersistResult, id_map) where id_map maps
            (system, value) → server-assigned FHIR id
        """
        if not resources:
            return PersistResult(status="success", created=0, updated=0, errors=[]), {}

        entries = []
        id_keys: list[tuple[str, str]] = []
        for resource in resources:
            resource_type = resource["resourceType"]
            system = resource["identifier"][0]["system"]
            value = resource["identifier"][0]["value"]
            id_keys.append((system, value))
            entries.append(
                {
                    "resource": resource,
                    "request": {
                        "method": "PUT",
                        "url": f"{resource_type}?identifier={system}|{value}",
                    },
                }
            )

        bundle = {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": entries,
        }

        response = self._make_request("POST", "/", json=bundle)
        response_bundle = response.json()

        created = 0
        updated = 0
        errors: list[dict] = []
        id_map: dict[tuple[str, str], str] = {}

        for idx, (key, response_entry) in enumerate(
            zip(id_keys, response_bundle.get("entry", []), strict=False)
        ):
            response_info = response_entry.get("response", {})
            status = response_info.get("status", "")

            # Extract server ID from response.location
            server_id = self._extract_id_from_location(
                response_info.get("location", "")
            )
            if not server_id:
                server_id = response_entry.get("resource", {}).get("id")

            if status.startswith("2"):
                if status.startswith("201"):
                    created += 1
                else:
                    updated += 1
                if server_id:
                    id_map[key] = server_id
            else:
                resource_type = entries[idx]["resource"]["resourceType"]
                error_msg = self._extract_error_message(
                    response_info.get("outcome", {}), status
                )
                errors.append({"resource": resource_type, "error": error_msg})

        if errors:
            result_status = "partial_failure" if created + updated > 0 else "failed"
        else:
            result_status = "success"

        return (
            PersistResult(
                status=result_status, created=created, updated=updated, errors=errors
            ),
            id_map,
        )

    @staticmethod
    def _extract_id_from_location(location: str) -> str | None:
        """Extract resource id from transaction response location.

        Supports both relative and absolute formats, e.g.
        - Organization/org-1/_history/1
        - https://example.com/fhir/Organization/org-1/_history/1
        """
        if not location:
            return None

        parsed = urlparse(location)
        path = parsed.path if (parsed.scheme or parsed.netloc) else location
        segments = [segment for segment in path.split("/") if segment]

        if not segments:
            return None

        if "_history" in segments:
            history_index = segments.index("_history")
            if history_index >= 1:
                return segments[history_index - 1]
            return None

        if len(segments) >= 2:
            return segments[-1]
        return None

    def deduplicate_observations(
        self, entries: list[dict], patient_id: str
    ) -> list[dict]:
        """Remove bundle entries for observations that already exist in FHIR.

        Queries FHIR per unique date in the batch (typically 1-2 queries),
        builds (date, code, value) signatures, and filters out matches.
        Also cleans up stale references from DocumentReference and Encounter
        entries that pointed to removed observations.

        Args:
            entries: Bundle entries from extraction
            patient_id: FHIR Patient.id for querying existing observations

        Returns:
            Filtered entries with duplicates removed
        """
        obs_entries = [
            e
            for e in entries
            if e.get("resource", {}).get("resourceType") == "Observation"
        ]
        if not obs_entries:
            return entries

        # Collect unique dates from new observations
        dates: set[str] = set()
        for entry in obs_entries:
            if date := entry["resource"].get("effectiveDateTime"):
                dates.add(date[:10])
        if not dates:
            return entries

        # Fetch existing observation signatures per date
        existing_sigs: set[tuple[str, str, str]] = set()
        for date in dates:
            try:
                existing = self.search_patient_resources(
                    patient_id, "Observation", params={"date": date}
                )
                for obs in existing:
                    if sig := _observation_signature(obs):
                        existing_sigs.add(sig)
            except Exception as e:
                logger.warning(f"Failed to fetch observations for dedup on {date}: {e}")

        if not existing_sigs:
            return entries

        # Filter out duplicates
        other_entries = [
            e
            for e in entries
            if e.get("resource", {}).get("resourceType") != "Observation"
        ]
        kept: list[dict] = []
        skipped = 0
        for entry in obs_entries:
            sig = _observation_signature(entry["resource"])
            if sig and sig in existing_sigs:
                skipped += 1
                logger.debug(f"Skipping duplicate observation: {sig}")
            else:
                kept.append(entry)

        if skipped:
            logger.info(f"Deduplicated {skipped} observation(s)")

        result = other_entries + kept
        return _filter_stale_refs(result)

    @staticmethod
    def _extract_error_message(outcome: dict, status: str = "") -> str:
        """Extract error message from OperationOutcome."""
        issues = outcome.get("issue", [])
        if issues:
            issue = issues[0]
            msg = (
                issue.get("diagnostics")
                or issue.get("details", {}).get("text")
                or issue.get("code")
            )
            location = issue.get("expression") or issue.get("location")
            if location:
                if isinstance(location, list):
                    loc_str = ", ".join(location)
                else:
                    loc_str = location
                msg = f"{msg} at {loc_str}" if msg else loc_str
            if msg:
                return msg
        if outcome:
            logger.debug(f"Full OperationOutcome: {json.dumps(outcome)}")
        return f"HTTP {status}" if status else "Unknown error"
