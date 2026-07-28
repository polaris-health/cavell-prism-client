#!/usr/bin/env python3
"""Summarize a FHIR server: count every resource type with data.

Reads the server's CapabilityStatement for the supported resource types and
issues one ``_summary=count`` search per type, so the counts are exact (not
HAPI's cached homepage numbers).

Usage:
    uv run python scripts/fhir_summary.py
    uv run python scripts/fhir_summary.py --fhir-url http://localhost:8090
"""

import argparse
import sys

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fhir-url",
        default="http://localhost:8090",
        help="FHIR server base URL (default: http://localhost:8090)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include resource types with zero resources",
    )
    args = parser.parse_args()
    base = f"{args.fhir_url.rstrip('/')}/fhir"

    with httpx.Client(timeout=30.0) as client:
        try:
            response = client.get(f"{base}/metadata")
            response.raise_for_status()
        except httpx.HTTPError as e:
            print(
                f"FHIR server not reachable at {base} ({e}) — "
                f"run: scripts/start_fhir.sh",
                file=sys.stderr,
            )
            sys.exit(1)

        types = [
            resource["type"]
            for rest in response.json().get("rest", [])
            for resource in rest.get("resource", [])
        ]

        counts: dict[str, int] = {}
        for resource_type in types:
            result = client.get(f"{base}/{resource_type}", params={"_summary": "count"})
            result.raise_for_status()
            counts[resource_type] = result.json().get("total", 0)

    shown = counts if args.all else {t: n for t, n in counts.items() if n}
    if not shown:
        print(f"No resources on {base} ({len(types)} types checked).")
        return

    width = max(len(t) for t in shown)
    for resource_type, count in sorted(shown.items(), key=lambda kv: -kv[1]):
        print(f"{resource_type:<{width}}  {count:>8}")
    print(f"{'-' * width}  {'-' * 8}")
    print(f"{'Total':<{width}}  {sum(shown.values()):>8}")


if __name__ == "__main__":
    main()
