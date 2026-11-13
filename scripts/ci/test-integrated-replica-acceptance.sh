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

# Production startup imports the same Settings validator used by backend.
# Use synthetic values for every required dependency; this runs with no
# network and no database, and never reads production .env/secrets.
production_import_gate() {
  docker run --rm --network none --read-only --tmpfs /tmp:size=256m,nosuid,nodev \
    --user 10001:10001 \
    -e ENVIRONMENT=production \
    -e DATABASE_URL=postgresql://ci_user:ci_password@db:5432/ci_db \
    -e SECRET_KEY=ci-only-secret-key-0123456789abcdef \
    -e ENCRYPTION_KEY= \
    -e CORS_ALLOWED_ORIGINS= \
    -e FIRST_ADMIN_PASSWORD=ci-only-admin-password \
    -e REDIS_URL=redis://:ci-only-redis-password@redis:6379/0 \
    -e N8N_API_URL=https://n8n.ci.invalid/api/v1 \
    -e N8N_API_KEY=ci-only-n8n-api-key \
    -e NEO4J_URI=bolt://neo4j:7687 \
    -e NEO4J_USER=neo4j \
    -e NEO4J_PASSWORD=ci-only-neo4j-password \
    -e MINIO_ENDPOINT=minio:9000 \
    -e MINIO_ACCESS_KEY=ci-only-minio-access \
    -e MINIO_SECRET_KEY=ci-only-minio-secret \
    -e STEWARD_BROWSER_CDP_URL=http://browser:9222 \
    -e STEWARD_BROWSER_ALLOW_PRIVATE_NETWORKS=false \
    -e WEKNORA_BASE_URL=http://weknora-app:8080 \
    -e WEKNORA_WEB_URL=https://weknora.ci.invalid \
    -e KANEO_BASE_URL=http://kaneo-app:5173 \
    -e KANEO_WEB_URL=https://kaneo.ci.invalid \
    -e PIPELINE_FILE_PUBLIC_APP_BASE_URL=https://app.ci.invalid \
    -e PIPELINE_FILE_PUBLIC_API_BASE_URL=https://api.ci.invalid \
    -e PYTHON_KERNEL_GATEWAY_AUTH_TOKEN=ci-only-kernel-gateway-token \
    -e INTEGRATED_DBX_URL=http://dbx:4224 \
    -e INTEGRATED_DBX_PASSWORD=ci-only-dbx-password \
    -e INTEGRATED_DBX_COMMIT=0137e0cbc8b663da5a17ec424c6d47e4e5570ead \
    --entrypoint python "$IMAGE" \
    -c 'import os; assert os.geteuid() == 10001; from app.shared.config import settings; from app.data_channel.integrated import cdc_executor, replica_executor; print("production executor imports passed")'
}
production_import_gate

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
