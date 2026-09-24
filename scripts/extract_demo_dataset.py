#!/usr/bin/env python3
"""Load the whole unified demo dataset into the local FHIR server.

Starts the local HAPI server (scripts/start_fhir.sh, port 8090) — wiping its
database first only with --fresh — then:

  1. seeds the organization, practitioners and patients of docs/notebooks/notes.csv
  2. extracts every note through the Prism API (the slow, paid part)
  3. ingests docs/notebooks/notes_lab_results.csv (deterministic, no LLM)

and prints a summary with FHIR resource counts. Progress bars show documents
done, cost so far and an ETA. Every outcome is appended to a JSONL log under
.cache/demo_runs/ as it lands.

The LLM Gateway key is read from $CAVELL_API_KEY, or prompted for (hidden) like
in the notebooks; $CAVELL_API_URL picks the Prism deployment.

A full run is ~2,400 documents: expect hours, not minutes. Without --fresh a
run continues where the data stands, so an interrupted run (Ctrl-C) resumes by
simply running it again: documents already in FHIR are skipped and lab rows
already present are matched, not duplicated.

Usage:
    uv run python scripts/extract_demo_dataset.py --fresh      # wipe + full run
    uv run python scripts/extract_demo_dataset.py              # start or continue
    uv run python scripts/extract_demo_dataset.py --patient MRN-20101 MRN-20401
    uv run python scripts/extract_demo_dataset.py --tier medium --concurrency 5
"""

import argparse
import csv
import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from cavell_client import (
    CavellClient,
    Document,
    IngestionOutcome,
    IngestionPipeline,
    LabIngestionPipeline,
    LabResult,
    Organization,
    Patient,
    Practitioner,
)

ROOT = Path(__file__).resolve().parent.parent
NOTEBOOKS = ROOT / "docs" / "notebooks"
NOTES_CSV = NOTEBOOKS / "notes.csv"
LABS_CSV = NOTEBOOKS / "notes_lab_results.csv"
RUNS_DIR = ROOT / ".cache" / "demo_runs"
FHIR_URL = "http://localhost:8090"
ORG_ID, ORG_NAME = "DEMO-HOSPITAL", "Demo Hospital"
DEFAULT_API_URL = "https://prd.prism.cavell.app/api"
RESOURCE_TYPES = [
    "Patient",
    "Practitioner",
    "Encounter",
    "DocumentReference",
    "Condition",
    "MedicationRequest",
    "Procedure",
    "Observation",
    "AllergyIntolerance",
]


# ------------------------------------------------------------------ progress


BAR_WIDTH = 20
RATE_LIMITED = re.compile(r"Rate limited, retrying in (\d+)s")


class Progress:
    """A dependency-free, single-line progress bar with count, ETA and info.

    On a terminal the line is redrawn in place — every second, so the clock
    keeps moving while a batch is in flight — and is cut to the window width,
    so it never wraps (a wrapped line cannot be erased with a carriage return,
    which is what leaves stray fragments behind). Log records are printed on
    their own line above the bar (see :class:`BarLogHandler`). Without a
    terminal it prints one line per update instead, which reads well in a log.
    """

    active: "Progress | None" = None
    lock = threading.RLock()

    def __init__(self, total: int, label: str) -> None:
        self.total, self.label = max(total, 1), label
        self.done = 0
        self.info = ""
        self.paused_until: float | None = None  # rate-limit wait, monotonic
        self.paused_workers = 0
        self.t0 = time.monotonic()
        self.tty = sys.stdout.isatty()
        self._stop = threading.Event()
        Progress.active = self
        if self.tty:
            threading.Thread(target=self._tick, daemon=True).start()

    def _tick(self) -> None:
        while not self._stop.wait(1.0):
            self.draw()

    def update(self, n: int, info: str) -> None:
        with self.lock:
            self.done += n
            self.info = info
            self.paused_until = None
            self.draw(newline=not self.tty)

    def rate_limited(self, wait: float) -> None:
        with self.lock:
            now = time.monotonic()
            if self.paused_until is None or now > self.paused_until:
                self.paused_workers = 0
            self.paused_until = now + wait
            self.paused_workers += 1
            self.draw()

    def line(self) -> str:
        elapsed = time.monotonic() - self.t0
        rate = self.done / elapsed if elapsed > 0 and self.done else 0.0
        eta = (self.total - self.done) / rate if rate else None
        filled = round(min(self.done / self.total, 1.0) * BAR_WIDTH)
        bar = "█" * filled + "░" * (BAR_WIDTH - filled)
        status = self.info
        if self.paused_until is not None:
            left = self.paused_until - time.monotonic()
            if left > 0:
                status = (
                    f"rate-limited, {self.paused_workers} request(s) waiting "
                    f"{_dur(left)} · {self.info}"
                )
        return (
            f"{self.label} {bar} {self.done}/{self.total}  "
            f"{_dur(elapsed)} · ETA {_dur(eta)}  {status}"
        )

    def draw(self, newline: bool = False) -> None:
        with self.lock:
            if self._stop.is_set():
                return
            if self.tty:
                width = shutil.get_terminal_size((100, 20)).columns - 1
                sys.stdout.write("\r\033[K" + self.line()[:width])
            elif newline:
                sys.stdout.write(self.line() + "\n")
            sys.stdout.flush()

    def message(self, text: str) -> None:
        """Print ``text`` on its own line without breaking the bar."""
        with self.lock:
            if self.tty:
                sys.stdout.write("\r\033[K")
            sys.stdout.write(text + "\n")
            self.draw()

    def close(self) -> None:
        with self.lock:
            self.draw()
            self._stop.set()
            if self.tty:
                sys.stdout.write("\n")
            Progress.active = None


class BarLogHandler(logging.Handler):
    """Route log records around the progress bar instead of through it.

    The SDK logs a warning per rate-limited request; with several workers a
    single rate limit produces a burst of identical lines, so those are folded
    into the bar ("rate-limited, 3 request(s) waiting 04m12s") after one line
    per burst.
    """

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        stamp = datetime.now().strftime("%H:%M:%S")
        bar = Progress.active
        match = RATE_LIMITED.search(text)
        if bar is None:
            sys.stdout.write(f"{stamp} {record.levelname.lower()}: {text}\n")
            return
        if match:
            first_of_burst = bar.paused_until is None or (
                time.monotonic() > bar.paused_until
            )
            if first_of_burst:
                bar.message(
                    f"{stamp} rate-limited by the Prism API (HTTP 429), "
                    f"retrying in {match.group(1)}s"
                )
            bar.rate_limited(float(match.group(1)))
            return
        bar.message(f"{stamp} {record.levelname.lower()}: {text}")


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m:02d}m{s:02d}s"


def step(title: str) -> None:
    print(f"\n\033[1m== {title}\033[0m" if sys.stdout.isatty() else f"\n== {title}")


# ------------------------------------------------------------------ steps


def start_fhir(fresh: bool) -> None:
    step("FHIR server" + (" (fresh database)" if fresh else " (keeping existing data)"))
    cmd = [str(ROOT / "scripts" / "start_fhir.sh")] + (["--fresh"] if fresh else [])
    subprocess.run(cmd, check=True, env={**os.environ, "FHIR_URL": FHIR_URL})  # noqa: S603


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def build_objects(rows: list[dict[str, str]]):
    organizations = [Organization(identifier=ORG_ID, name=ORG_NAME)]
    practitioners = Practitioner.from_rows(
        rows,
        columns={"identifier": "practitioner_id", "name": "practitioner_name"},
        organization_identifier=ORG_ID,
    )
    patients = Patient.from_rows(
        rows,
        columns={
            "identifier": "patient_id",
            "name": "patient_name",
            "birth_date": "birth_date",
            "gender": "gender",
            "general_practitioners": "practitioner_id",
        },
        managing_organization=ORG_ID,
    )
    documents = Document.from_rows(
        rows,
        columns={
            "text": "note_text",
            "patient_identifier": "patient_id",
            "date": "note_date",
            "document_id": "note_id",
            "encounter_id": "encounter_id",
            "practitioner_identifier": "practitioner_id",
            "meta": {"Department": "department"},
        },
        organization_identifier=ORG_ID,
    )
    return organizations, practitioners, patients, documents


def extract_notes(
    pipeline: IngestionPipeline,
    client: CavellClient,
    documents: list[Document],
    batch_size: int,
    results_log: Path,
) -> dict:
    step(f"Extracting {len(documents)} documents")
    done_ids = client.list_processed_document_ids()
    already = sum(1 for d in documents if d.document_id in done_ids)
    if already:
        print(f"{already} documents already in FHIR — skipped.")
    todo = len(documents) - already
    if todo == 0:
        print("Nothing left to extract.")
        return {"ok": 0, "failed": 0, "cost": 0.0, "resources": 0, "out_of_order": 0}

    stats = {"ok": 0, "failed": 0, "cost": 0.0, "resources": 0, "out_of_order": 0}
    bar = Progress(todo, "notes")
    bar.update(0, "first batch in flight…")

    def on_batch(outcomes: list[IngestionOutcome]) -> None:
        with results_log.open("a") as f:
            for o in outcomes:
                r = o.extract_result
                cost = r.usage.estimated_cost if r and r.usage else 0.0
                if o.success:
                    stats["ok"] += 1
                    stats["cost"] += cost
                    stats["resources"] += r.count if r else 0
                else:
                    stats["failed"] += 1
                    bar.message(
                        f"FAILED {o.document_id} ({o.patient_identifier}): {o.error}"
                    )
                stats["out_of_order"] += o.out_of_order
                f.write(
                    json.dumps(
                        {
                            "kind": "document",
                            "document_id": o.document_id,
                            "patient": o.patient_identifier,
                            "success": o.success,
                            "error": o.error,
                            "transient": o.transient,
                            "out_of_order": o.out_of_order,
                            "resources": r.count if r else 0,
                            "created": r.persistence.created
                            if r and r.persistence
                            else 0,
                            "updated": r.persistence.updated
                            if r and r.persistence
                            else 0,
                            "cost": cost,
                            "tokens": r.usage.total_tokens if r and r.usage else 0,
                        }
                    )
                    + "\n"
                )
        per_doc = stats["cost"] / stats["ok"] if stats["ok"] else 0.0
        bar.update(
            len(outcomes),
            f"${stats['cost']:.2f} · ${per_doc:.3f}/doc · "
            f"{stats['resources']} res · {stats['failed']} failed",
        )

    pipeline.extract_all(documents, batch_size=batch_size, on_batch=on_batch)
    bar.close()
    return stats


def ingest_labs(
    client: CavellClient, patient_filter: set[str] | None, results_log: Path
) -> dict:
    rows = read_csv(LABS_CSV)
    if patient_filter:
        rows = [r for r in rows if r["patient_id"] in patient_filter]
    step(f"Ingesting {len(rows)} lab rows (deterministic, no LLM)")
    results = LabResult.from_rows(
        rows,
        columns={
            "lab_result_id": "lab_result_id",
            "patient_identifier": "patient_id",
            "test_name": "test_name",
            "value": "value",
            "collected_datetime": "collected_datetime",
            "loinc_code": "loinc_code",
            "unit": "unit",
            "reference_low": "reference_low",
            "reference_high": "reference_high",
            "encounter_id": "encounter_id",
            "practitioner_id": "practitioner_id",
        },
    )
    # One call per group of patients so the bar moves; ingestion is idempotent.
    by_patient: dict[str, list[LabResult]] = {}
    for r in results:
        by_patient.setdefault(r.patient_identifier, []).append(r)
    groups, chunk = [], []
    for pid in sorted(by_patient):
        chunk += by_patient[pid]
        if len(chunk) >= 200:
            groups.append(chunk)
            chunk = []
    if chunk:
        groups.append(chunk)

    labs = LabIngestionPipeline(client)
    stats = {"accepted": 0, "created": 0, "existing": 0, "rejected": []}
    bar = Progress(len(results), "labs ")
    for group in groups:
        outcome = labs.ingest(group)
        stats["accepted"] += outcome.accepted
        stats["created"] += outcome.created
        stats["existing"] += outcome.skipped_existing
        with results_log.open("a") as f:
            for r in outcome.rejected:
                stats["rejected"].append((r.lab_result_id, r.stage, r.reason))
                f.write(
                    json.dumps(
                        {
                            "kind": "lab_rejection",
                            "lab_result_id": r.lab_result_id,
                            "stage": r.stage,
                            "reason": r.reason,
                        }
                    )
                    + "\n"
                )
        bar.update(
            len(group),
            f"{stats['created']} new · {stats['existing']} present · "
            f"{len(stats['rejected'])} rejected",
        )
    bar.close()
    return stats


# ------------------------------------------------------------------ main


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="wipe the FHIR database and start over (asks for confirmation)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="with --fresh: don't ask before wiping"
    )
    parser.add_argument(
        "--tier", help="model tier (default: the deployment's default tier)"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="patients extracted in parallel (default: 3). The Prism API's rate "
        "limit caps throughput anyway; more workers mostly hit it sooner and "
        "then all wait out its 5-minute block together",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="documents per extraction batch; the bar moves per batch (default: 25)",
    )
    parser.add_argument(
        "--patient",
        nargs="+",
        metavar="MRN",
        help="only these patients (e.g. a demo subset from demo_scenarios.md)",
    )
    parser.add_argument(
        "--skip-labs", action="store_true", help="don't ingest notes_lab_results.csv"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, handlers=[BarLogHandler()])

    all_rows = rows = read_csv(NOTES_CSV)
    patient_filter = set(args.patient) if args.patient else None
    if patient_filter:
        unknown = patient_filter - {r["patient_id"] for r in rows}
        if unknown:
            parser.error(f"unknown patient(s): {', '.join(sorted(unknown))}")
        rows = [r for r in rows if r["patient_id"] in patient_filter]
    organizations, _, patients, documents = build_objects(rows)
    # Always seed every practitioner: a subset's lab rows can name a clinician
    # who only wrote notes for other patients, and an unknown one is rejected.
    practitioners = build_objects(all_rows)[1]

    print(
        f"Dataset: {len(documents)} documents, {len(patients)} patients, "
        f"{len(practitioners)} practitioners ({NOTES_CSV.relative_to(ROOT)})"
    )

    if args.fresh and not args.yes:
        answer = input(
            f"This DELETES all data in the local FHIR server at {FHIR_URL}. "
            "Continue? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            sys.exit("Aborted.")

    api_url = os.environ.get("CAVELL_API_URL", DEFAULT_API_URL)
    api_key = os.environ.get("CAVELL_API_KEY") or getpass.getpass("LLM Gateway key: ")
    if not api_key:
        sys.exit("An LLM Gateway key is required.")

    start_fhir(args.fresh)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    results_log = RUNS_DIR / f"run-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    t0 = time.monotonic()
    client = CavellClient(
        api_url=api_url, api_key=api_key, fhir_base_url=FHIR_URL, fhir_api_path="/fhir"
    )
    note_stats = lab_stats = None
    try:
        step("Prism API")
        tiers = client.list_tiers()
        default_tier = next((t["name"] for t in tiers if t.get("default")), None)
        tier = args.tier or default_tier
        print(f"{api_url} — tiers: {', '.join(t['name'] for t in tiers)}; using {tier}")

        step("Seeding organization, practitioners and patients")
        pipeline = IngestionPipeline(
            client,
            tier=tier,
            max_concurrency=args.concurrency,
            default_organization=ORG_ID,
        )
        ts = time.monotonic()
        pipeline.seed(
            organizations=organizations, patients=patients, practitioners=practitioners
        )
        print(
            f"Seeded {len(practitioners)} practitioners and {len(patients)} patients "
            f"in {_dur(time.monotonic() - ts)}."
        )

        note_stats = extract_notes(
            pipeline, client, documents, args.batch_size, results_log
        )
        if not args.skip_labs:
            lab_stats = ingest_labs(client, patient_filter, results_log)
    except KeyboardInterrupt:
        print("\n\nInterrupted. Run again (without --fresh) to continue.")
    finally:
        step("Summary")
        wall = _dur(time.monotonic() - t0)
        print(f"Wall time: {wall}   log: {results_log.relative_to(ROOT)}")
        if note_stats:
            n = note_stats
            print(
                f"Documents: {n['ok']} extracted, {n['failed']} failed, "
                f"{n['out_of_order']} via split context; "
                f"{n['resources']} resources; ${n['cost']:.2f}"
            )
            if note_stats["failed"]:
                print("  Failed documents are in the log; running again retries them.")
        if lab_stats:
            lb = lab_stats
            print(
                f"Lab rows: {lb['accepted']} accepted ({lb['created']} created, "
                f"{lb['existing']} already present), {len(lb['rejected'])} rejected"
            )
            for rid, stage, reason in lab_stats["rejected"][:10]:
                print(f"  {rid} [{stage}] {reason}")
        try:
            counts = {t: client.count_resources(t) for t in RESOURCE_TYPES}
            print("FHIR now holds: " + ", ".join(f"{n} {t}" for t, n in counts.items()))
        except Exception as exc:  # the summary must not mask the real outcome
            print(f"(could not count FHIR resources: {exc})")
        client.close()


if __name__ == "__main__":
    main()
