#!/usr/bin/env python3
"""Run extraction on study notes via the Cavell SDK.

Reads a study directory (with manifest.json and notes/), seeds organizations,
practitioners, and patients into the local FHIR server, then extracts all notes
through the IngestionPipeline.

Saves per-note results to a JSONL file for later analysis.

Prerequisites:
    docker compose up -d          # HAPI FHIR on localhost:8090
    A Prism API URL + LLM Gateway key

Options:
    --dir         Path to study directory containing manifest.json and notes/
    --init        Seed references + patients and exit (no extraction)
    --patient     Process only these patient IDs
    --tier        Model tier: low, medium, high (default: medium)
    --api-url     Cavell API base URL (default: https://qa.prism.cavell.app/api)
    --api-key     LLM Gateway key (default: $CAVELL_API_KEY)
    --fhir-url    FHIR server base URL (default: http://localhost:8090)
    --concurrency Max parallel patients (default: 4)

Usage:
    uv run python scripts/run_study.py --dir path/to/study --init
    uv run python scripts/run_study.py --dir path/to/study
    uv run python scripts/run_study.py \\
        --dir path/to/study \\
        --patient patient-001 patient-002
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from cavell_client import (
    CavellClient,
    Document,
    IngestionPipeline,
    Organization,
    Patient,
    Practitioner,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_processed(study_dir: Path) -> set[str]:
    """Load set of already-processed note filenames."""
    processed_file = study_dir / ".processed"
    if processed_file.exists():
        return {line for line in processed_file.read_text().strip().split("\n") if line}
    return set()


def mark_processed(study_dir: Path, filename: str | None) -> None:
    """Append filename to .processed tracking file."""
    if not filename:
        return
    processed_file = study_dir / ".processed"
    with processed_file.open("a") as f:
        f.write(f"{filename}\n")


def build_organizations(manifest: dict) -> list[Organization]:
    """Build Organization objects from manifest."""
    return [
        Organization(identifier=org_id, name=org_name)
        for org_id, org_name in manifest.get("organizations", {}).items()
    ]


def build_practitioners(manifest: dict) -> list[Practitioner]:
    """Build Practitioner objects from manifest."""
    practitioners = []
    for prac_id, prac_data in manifest.get("practitioners", {}).items():
        # Name is a single string like "Jan Willems" — split into given/family
        name_parts = prac_data["name"].split()
        given_name = name_parts[0] if name_parts else ""
        family_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

        practitioners.append(
            Practitioner(
                identifier=prac_id,
                family_name=family_name,
                given_name=given_name,
                organization_identifier=prac_data.get("organization", ""),
                specialty=prac_data.get("specialty"),
            )
        )
    return practitioners


def build_patients(manifest: dict) -> list[Patient]:
    """Build Patient objects from manifest."""
    patients = []
    for patient_id, patient_data in manifest.get("patients", {}).items():
        names = patient_data.get("name", [{}])
        name = names[0] if names else {}
        given = " ".join(name.get("given", []))
        family = name.get("family", "")
        full_name = f"{given} {family}".strip() or None

        meta = patient_data.get("_meta", {})
        org_id = meta.get("organization_id")
        prac_id = meta.get("primary_physician_id")

        patients.append(
            Patient(
                identifier=patient_id,
                name=full_name,
                birth_date=patient_data.get("birthDate"),
                gender=patient_data.get("gender"),
                managing_organization=org_id,
                general_practitioners=[prac_id] if prac_id else None,
            )
        )
    return patients


def build_documents(
    manifest: dict,
    notes_dir: Path,
    processed: set[str],
    patients_filter: list[str] | None = None,
) -> list[Document]:
    """Build Document objects from manifest notes, filtering processed/patient."""
    documents = []
    skipped_processed = 0
    skipped_missing = 0

    for note in manifest.get("notes", []):
        filename = note["filename"]
        patient_id = note["patient_id"]

        # Apply filters
        if patients_filter and patient_id not in patients_filter:
            continue
        if filename in processed:
            skipped_processed += 1
            continue

        note_path = notes_dir / filename
        if not note_path.exists():
            skipped_missing += 1
            continue

        text = note_path.read_text()

        documents.append(
            Document(
                text=text,
                patient_identifier=patient_id,
                date=note["date"],
                organization_identifier=note.get("organization_id"),
                practitioner_identifier=note.get("practitioner_id"),
                document_id=filename,
            )
        )

    if skipped_processed:
        print(f"Skipped {skipped_processed} already-processed notes")
    if skipped_missing:
        print(f"Skipped {skipped_missing} missing note files")

    return documents


def make_client(api_url: str, api_key: str, fhir_url: str) -> CavellClient:
    """Create a CavellClient configured for local HAPI FHIR (no auth)."""
    return CavellClient(
        api_url=api_url,
        api_key=api_key,
        fhir_base_url=fhir_url,
        fhir_api_path="/fhir",
    )


def seed(
    client: CavellClient,
    manifest: dict,
    tier: str | None,
    default_org: str | None,
    concurrency: int,
) -> IngestionPipeline:
    """Seed references and patients, return the ready pipeline."""
    pipeline = IngestionPipeline(
        client,
        tier=tier,
        max_concurrency=concurrency,
        default_organization=default_org,
    )

    organizations = build_organizations(manifest)
    practitioners = build_practitioners(manifest)
    patients = build_patients(manifest)

    print(
        f"Seeding {len(organizations)} organizations, "
        f"{len(practitioners)} practitioners, {len(patients)} patients..."
    )
    pipeline.seed(organizations, patients, practitioners)
    print("  Done.")

    return pipeline


def run(
    api_url: str,
    api_key: str,
    fhir_url: str,
    study_dir: Path,
    tier: str | None,
    init_only: bool = False,
    patients_filter: list[str] | None = None,
    concurrency: int = 4,
) -> None:
    """Run the full study extraction."""
    manifest_path = study_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest.json found in {study_dir}")
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text())
    notes_dir = study_dir / "notes"

    # Determine default organization (first one in manifest)
    orgs = manifest.get("organizations", {})
    default_org = next(iter(orgs)) if orgs else None

    client = make_client(api_url, api_key, fhir_url)

    try:
        pipeline = seed(client, manifest, tier, default_org, concurrency)

        if init_only:
            print("\nSeeding complete (--init). No extraction.")
            return

        # Build documents
        processed = load_processed(study_dir)
        documents = build_documents(manifest, notes_dir, processed, patients_filter)

        if not documents:
            print("No notes to process.")
            return

        # Count patients
        patient_ids = {d.patient_identifier for d in documents}
        print(
            f"\nPhase 3: Extracting {len(documents)} notes across "
            f"{len(patient_ids)} patients"
        )
        print(f"  API: {api_url}, tier: {tier}, concurrency: {concurrency}\n")

        # Results file
        results_path = study_dir / "results.jsonl"
        total_cost = 0.0
        completed = 0
        failed = 0
        t0 = time.monotonic()

        for outcome in pipeline.extract(documents):
            completed += 1

            if outcome.success:
                r = outcome.extract_result
                assert r is not None
                cost = r.usage.estimated_cost if r.usage else 0.0
                total_cost += cost

                mark_processed(study_dir, documents[outcome.document_index].document_id)

                result_entry = {
                    "patient": outcome.patient_identifier,
                    "document": documents[outcome.document_index].document_id,
                    "success": True,
                    "resources": r.count,
                    "created": r.persistence.created if r.persistence else 0,
                    "updated": r.persistence.updated if r.persistence else 0,
                    "cost": cost,
                    "tokens": r.usage.total_tokens if r.usage else 0,
                }

                print(
                    f"[{completed}/{len(documents)}] (${total_cost:.3f}) "
                    f"{documents[outcome.document_index].document_id} → "
                    f"{r.count} resources, "
                    f"{r.persistence.created if r.persistence else 0} created / "
                    f"{r.persistence.updated if r.persistence else 0} updated"
                    f"  (${cost:.3f})"
                )
            else:
                failed += 1
                result_entry = {
                    "patient": outcome.patient_identifier,
                    "document": documents[outcome.document_index].document_id,
                    "success": False,
                    "error": outcome.error,
                }
                print(
                    f"[{completed}/{len(documents)}] (${total_cost:.3f}) "
                    f"{documents[outcome.document_index].document_id} FAILED: "
                    f"{outcome.error}"
                )

            # Append to results JSONL
            with results_path.open("a") as f:
                f.write(json.dumps(result_entry) + "\n")

            sys.stdout.flush()

        elapsed = time.monotonic() - t0
        print(
            f"\nDone in {elapsed:.1f}s. "
            f"{completed - failed}/{completed} succeeded, "
            f"{failed} failed. "
            f"Total cost: ${total_cost:.3f}"
        )
        print(f"Results saved to {results_path}")

    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract FHIR resources from study notes via Cavell SDK"
    )
    parser.add_argument(
        "--dir",
        required=True,
        help="Study directory containing manifest.json and notes/",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Seed references + patients only (no extraction)",
    )
    parser.add_argument(
        "--patient",
        nargs="*",
        help="Process only notes for these patient IDs",
    )
    parser.add_argument(
        "--tier",
        default="medium",
        help="Model tier: low, medium, high (default: medium)",
    )
    parser.add_argument(
        "--api-url",
        default="https://qa.prism.cavell.app/api",
        help="Cavell API base URL (default: https://qa.prism.cavell.app/api)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("CAVELL_API_KEY", ""),
        help="LLM Gateway key (default: $CAVELL_API_KEY)",
    )
    parser.add_argument(
        "--fhir-url",
        default="http://localhost:8090",
        help="FHIR server base URL (default: http://localhost:8090)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Max parallel patients (default: 4)",
    )
    args = parser.parse_args()

    if not args.api_key:
        parser.error("--api-key or $CAVELL_API_KEY is required")

    run(
        api_url=args.api_url,
        api_key=args.api_key,
        fhir_url=args.fhir_url,
        study_dir=Path(args.dir),
        tier=args.tier,
        init_only=args.init,
        patients_filter=args.patient or None,
        concurrency=args.concurrency,
    )


if __name__ == "__main__":
    main()
