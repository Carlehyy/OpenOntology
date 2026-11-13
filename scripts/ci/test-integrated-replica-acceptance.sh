#!/usr/bin/env bash
# Real integrated-replica acceptance for CI: isolated PG/MySQL sources + target,
# the production driver inside the freshly built executor image, and the
# production acceptance script. Fixture-only credentials; no host ports; no
# production dependency manifest or .env is mounted into this stack.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE="$ROOT/docker-compose.replica-acceptance.yml"
ACCEPTANCE="$ROOT/scripts/e2e/integrated_replica_acceptance.py"
ARTIFACTS="${REPLICA_ACCEPTANCE_ARTIFACT_ROOT:-$ROOT/.artifacts/ci-replicaacceptance}"
RUN_TOKEN="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
PROJECT="${REPLICA_ACCEPTANCE_PROJECT:-oo-replica-ci-${RUN_TOKEN}}"
TARGET_DB="replica_acceptance_${RUN_TOKEN}"
TIMEOUT_SECONDS="${REPLICA_ACCEPTANCE_TIMEOUT_SECONDS:-1200}"

# Docker project/network names are user-overridable input: keep them lowercase
# and short enough for the derived "<project>_default" network name.
if ! [[ "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]{0,47}$ ]]; then
  printf 'invalid REPLICA_ACCEPTANCE_PROJECT: %s\n' "$PROJECT" >&2
  exit 2
fi
if ! [[ "$TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] \
    || [ "$TIMEOUT_SECONDS" -lt 60 ] || [ "$TIMEOUT_SECONDS" -gt 3600 ]; then
  printf 'invalid REPLICA_ACCEPTANCE_TIMEOUT_SECONDS: %s\n' "$TIMEOUT_SECONDS" >&2
  exit 2
fi
NETWORK="${PROJECT}_default"

resolve_image() {
  if [ -n "${REPLICA_ACCEPTANCE_IMAGE:-}" ]; then
    printf '%s\n' "$REPLICA_ACCEPTANCE_IMAGE"
    return
  fi
  local tag
  tag="$(
    docker compose -f "$ROOT/docker-compose.prod.yml" build --print integrated_replica_executor \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["target"]["integrated_replica_executor"]["tags"][0])'
  )"
  [ -n "$tag" ] || {
    printf 'could not resolve the built integrated_replica_executor image tag\n' >&2
    exit 1
  }
  printf '%s\n' "$tag"
}

IMAGE="$(resolve_image)"
docker image inspect "$IMAGE" >/dev/null
mkdir -p "$ARTIFACTS"

cleanup() {
  # Remove only this run's driver and project. The acceptance driver can be
  # orphaned if the host-side watchdog fires, so name it explicitly before the
  # project teardown; all fixture containers/volumes belong to the unique
  # project and are safe to remove here.
  docker rm -f "${PROJECT}-acceptance-driver" >/dev/null 2>&1 || true
  docker compose -p "$PROJECT" -f "$COMPOSE" down --volumes --remove-orphans \
    >/dev/null 2>&1 || true
}
trap 'rc=$?; cleanup; trap - EXIT; exit "$rc"' EXIT

docker compose -p "$PROJECT" -f "$COMPOSE" up -d --wait --wait-timeout 300

for source in pg mysql; do
  python3 "$ACCEPTANCE" \
    --source "$source" \
    --row-count 3000 \
    --latency-samples 20 \
    --source-count 2 \
    --project "$PROJECT" \
    --network "$NETWORK" \
    --image "$IMAGE" \
    --jar builtin \
    --target-db "$TARGET_DB" \
    --artifacts "$ARTIFACTS" \
    --driver-memory 3g \
    --driver-cpus 2 \
    --timeout "$TIMEOUT_SECONDS"
done
