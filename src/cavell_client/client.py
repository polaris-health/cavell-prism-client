"""CavellClient - config and FHIR utilities for the Cavell SDK."""

from cavell_client.api import CavellAPI, _reject_removed_auth_kwargs
from cavell_client.fhir import FHIRClient


class CavellClient:
    """Client holding API and FHIR credentials, used by IngestionPipeline.

    Example:
        from cavell_client import CavellClient, IngestionPipeline

        with CavellClient(
            api_url="https://prd.prism.cavell.app/api",
            api_key="<your LLM Gateway key>",
            fhir_base_url="http://localhost:8080",
        ) as client:
            pipeline = IngestionPipeline(client, default_organization="ORG-1")
            pipeline.seed(...)
            for outcome in pipeline.extract(documents):
                print(outcome)
    """

    def __init__(
        self,
        api_url: str,
        api_key: str = "",
        fhir_base_url: str = "",
        fhir_client_id: str | None = None,
        fhir_client_secret: str | None = None,
        fhir_api_path: str = "/fhir",
        **removed_kwargs,
    ):
        """Initialize CavellClient.

        Args:
            api_url: Cavell API URL, including the /api prefix
                (e.g., "https://prd.prism.cavell.app/api")
            api_key: LLM Gateway key, sent as a bearer token on every
                Cavell API request
            fhir_base_url: Local FHIR server URL (e.g., "http://localhost:8080")
            fhir_client_id: FHIR OAuth2 client ID (None to skip auth)
            fhir_client_secret: FHIR OAuth2 client secret (None to skip auth)
            fhir_api_path: FHIR API path prefix (default: "/fhir")
        """
        _reject_removed_auth_kwargs(removed_kwargs)
        if not fhir_base_url:
            raise TypeError("fhir_base_url is required")
        self._api = CavellAPI(api_url, api_key)
        self._fhir = FHIRClient(
            base_url=fhir_base_url,
            client_id=fhir_client_id,
            client_secret=fhir_client_secret,
            api_path=fhir_api_path,
        )

    def check_connection(self) -> dict:
        """Verify connectivity to both the FHIR server and Cavell API.

        The FHIR half checks /metadata (reachability and, if configured,
        OAuth2 credentials). The Cavell half calls GET /key/info, which
        validates the presented LLM Gateway key against the gateway without
        spending tokens — a wrong URL, missing key, or invalid key all
        surface here.

        Returns a dict with 'fhir' and 'cavell_api' keys, each containing
        'ok' (bool) and 'error' (str) if the check failed.
        """
        result: dict = {}

        for key, check_fn in [
            ("fhir", self._fhir.check_connection),
            ("cavell_api", self._api.check_connection),
        ]:
            try:
                check_fn()
                result[key] = {"ok": True}
            except Exception as e:
                result[key] = {"ok": False, "error": str(e)}

        return result

    def close(self) -> None:
        """Close all HTTP clients."""
        try:
            self._api.close()
        finally:
            self._fhir.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

    def find_patient_id(self, identifier: str) -> str | None:
        """Look up a patient's FHIR server ID from their business identifier.

        Queries the FHIR server directly, so it works across kernel restarts
        (unlike the pipeline's in-memory id map).

        Args:
            identifier: Patient identifier (e.g., MRN) as passed to seed()

        Returns:
            FHIR Patient.id if found, None otherwise
        """
        result = self._fhir.find_patient_by_identifier(identifier)
        return result[0] if result else None

    def get_patient_resources(
        self,
        patient_id: str,
        resource_type: str | None = None,
    ) -> list[dict]:
        """Fetch FHIR resources for a patient.

        Args:
            patient_id: FHIR Patient.id (use client.find_patient_id() to look this up)
            resource_type: FHIR resource type (e.g., "Condition"). If None,
                returns all resources via $everything.

        Returns:
            List of matching FHIR resources
        """
        if resource_type is None:
            return self._fhir.get_patient_everything(patient_id)
        return self._fhir.search_patient_resources(patient_id, resource_type)

    def list_tiers(self) -> list[dict]:
        """List available model tiers from the Cavell API.

        Returns:
            List of tier dicts with keys: name, default.
        """
        return self._api.list_tiers()

    def delete_patient_resources(self, patient_id: str) -> None:
        """Delete a patient and all resources referencing them.

        Uses cascade delete — the FHIR server walks the reference graph
        and removes everything. The patient must be re-seeded before
        re-extracting.

        Args:
            patient_id: FHIR Patient.id (use client.find_patient_id() to look this up)
        """
        self._fhir.delete_patient_resources(patient_id)

    def count_resources(self, resource_type: str) -> int:
        """Count resources of a given type on the FHIR server.

        Args:
            resource_type: FHIR resource type (e.g., "Patient", "Condition")

        Returns:
            Total count
        """
        return self._fhir.count_resources(resource_type)

    def list_processed_document_ids(self, patient_id: str | None = None) -> set[str]:
        """Return document IDs already processed on the FHIR server.

        Useful for resuming an interrupted pipeline run — skip documents
        whose IDs appear in the returned set.

        Args:
            patient_id: FHIR Patient.id to scope the query to (use
                client.find_patient_id() to look this up). Without it the
                whole server is scanned.
        """
        return self._fhir.list_document_identifiers(patient=patient_id)

    def mark_validated(self, resource_type: str, resource_id: str) -> bool:
        """Mark a resource as clinician-validated.

        Prism stamps every extracted resource with an ``unvalidated``
        meta tag (system ``<deployment-url>/fhir/CodeSystem/validation-status``).
        This removes that tag via FHIR ``$meta-delete`` — the consumer
        contract for "a clinician has validated this resource". Note that
        any later Prism update to the resource re-adds the tag.

        Args:
            resource_type: FHIR resource type (e.g., "Condition")
            resource_id: FHIR logical id

        Returns:
            True if an unvalidated tag was removed, False if none was present.
        """
        resource = self._fhir.get_resource(resource_type, resource_id)
        tags = [
            (tag.get("system", ""), tag["code"])
            for tag in resource.get("meta", {}).get("tag", [])
            if tag.get("code") == "unvalidated"
            and tag.get("system", "").endswith("/fhir/CodeSystem/validation-status")
        ]
        if not tags:
            return False
        self._fhir.delete_meta_tags(resource_type, resource_id, tags)
        return True

    def list_unvalidated_resources(
        self, patient_id: str, resource_type: str
    ) -> list[dict]:
        """List a patient's resources still carrying the ``unvalidated`` tag.

        Args:
            patient_id: FHIR Patient.id (use client.find_patient_id())
            resource_type: FHIR resource type (e.g., "Condition")

        Returns:
            List of matching FHIR resources
        """
        return self._fhir.search_patient_resources(
            patient_id, resource_type, params={"_tag": "unvalidated"}
        )
