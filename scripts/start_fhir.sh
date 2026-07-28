#!/usr/bin/env bash
# Start the local HAPI FHIR server from docker-compose.yml and wait until it
# answers. With --fresh, wipe the database volume first for a clean slate.
#
# Usage:
#     scripts/start_fhir.sh            # start (or reuse) the server
#     scripts/start_fhir.sh --fresh    # DELETE all data, then start empty
set -euo pipefail

FHIR_URL="${FHIR_URL:-http://localhost:8090}"

cd "$(dirname "$0")/.."

case "${1:-}" in
  --fresh)
    echo "Wiping the FHIR database (docker compose down -v)..."
    docker compose down -v
    ;;
  "")
    ;;
  *)
    echo "Unknown argument: $1 (only --fresh is supported)" >&2
    exit 2
    ;;
esac

docker compose up -d

# HAPI cold-starts slowly (schema migration on a fresh volume).
printf 'Waiting for HAPI at %s/fhir/metadata ' "$FHIR_URL"
for _ in $(seq 1 120); do
  if curl -sf -o /dev/null "$FHIR_URL/fhir/metadata"; then
    printf '\nFHIR server ready at %s/fhir\n' "$FHIR_URL"
    exit 0
  fi
  printf '.'
  sleep 2
done

printf '\nTimed out waiting for the FHIR server. Recent logs:\n' >&2
docker compose logs --tail 20 fhir >&2
exit 1
