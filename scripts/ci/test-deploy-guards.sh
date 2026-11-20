#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
APP_DIR_VALIDATOR="$SCRIPT_DIR/validate-deploy-app-dir.sh"
DEPLOY_SCRIPT="$REPO_ROOT/deploy/deploy-prod.sh"
DEPLOY_WORKFLOW="$REPO_ROOT/.github/workflows/deploy-nano-ontoprompt.yml"
ARCHIVE_SCRIPT="$SCRIPT_DIR/create-deployment-archive.sh"
# 80/443 两个 server 共享 locations.inc；location 级契约的事实源在 inc
NGINX_CONFIG="$REPO_ROOT/frontend/nginx/locations.inc"
NGINX_SERVER_CONFS="$REPO_ROOT/frontend/nginx/default.conf $REPO_ROOT/frontend/nginx/ssl-443.conf"
PROD_COMPOSE="$REPO_ROOT/docker-compose.prod.yml"
LOCAL_COMPOSE="$REPO_ROOT/docker-compose.local.yml"

assert_accepted() {
  local value="$1"
  bash "$APP_DIR_VALIDATOR" "$value" >/dev/null
}

assert_rejected() {
  local value="${1-}"
  if bash "$APP_DIR_VALIDATOR" "$value" >/dev/null 2>&1; then
    printf 'expected DEPLOY_APP_DIR to be rejected\n' >&2
    exit 1
  fi
}

bash "$SCRIPT_DIR/test-classify-deploy-changes.sh"

assert_accepted /opt/openontology
assert_accepted /srv/apps/openontology_1

assert_rejected ""
assert_rejected /
assert_rejected /opt
assert_rejected opt/openontology
assert_rejected /opt/../openontology
assert_rejected /opt//openontology
assert_rejected /opt/openontology/
assert_rejected "/opt/openontology build"
assert_rejected "/opt/ontology'build"
assert_rejected $'/opt/openontology\nunexpected'
assert_rejected "/opt/openontology;unexpected"

if grep -q 'StrictHostKeyChecking=no' "$DEPLOY_WORKFLOW"; then
  printf 'deployment workflow must not bypass SSH host-key verification\n' >&2
  exit 1
fi
if ! grep -Fq 'cancel-in-progress: false' "$DEPLOY_WORKFLOW"; then
  printf 'deployment workflow must queue rather than cancel an in-flight migration\n' >&2
  exit 1
fi
if ! grep -Fq 'bash scripts/ci/classify-deploy-changes.sh --stdin' "$DEPLOY_WORKFLOW" \
  || ! grep -Fq 'bash scripts/ci/classify-deploy-changes.sh --full' "$DEPLOY_WORKFLOW"; then
  printf 'deployment workflow must classify push paths with the tested script\n' >&2
  exit 1
fi
if ! grep -Fq 'git diff --name-only --no-renames' "$DEPLOY_WORKFLOW"; then
  printf 'change classification must list rename source and destination paths\n' >&2
  exit 1
fi
if grep -Eq 'needs\.changes\.outputs\.code' "$DEPLOY_WORKFLOW"; then
  printf 'deployment workflow must not use the old code=true skip switch\n' >&2
  exit 1
fi
if ! grep -Fq "needs.changes.outputs.run_deploy == 'true'" "$DEPLOY_WORKFLOW" \
  || ! grep -Fq "needs.changes.outputs.run_backend != 'true'" "$DEPLOY_WORKFLOW" \
  || ! grep -Fq "needs.verify-backend.result == 'success'" "$DEPLOY_WORKFLOW"; then
  printf 'deploy job must require run_deploy and demanded backend verification\n' >&2
  exit 1
fi
if grep -Eq "run_backend != 'true'[[:space:]]*\|\|[[:space:]]*needs\.verify-backend\.result == 'skipped'" \
  "$DEPLOY_WORKFLOW"; then
  printf 'deploy job must not treat a skipped backend job as verification\n' >&2
  exit 1
fi
if ! grep -Fq "needs.changes.result" "$DEPLOY_WORKFLOW"; then
  printf 'report job must fail when change classification does not succeed\n' >&2
  exit 1
fi
fresh_database_migration_block="$(
  sed -n \
    '/^[[:space:]]*- name: Fresh database migration$/,/^[[:space:]]*- name: Install configuration-center dependencies$/p' \
    "$DEPLOY_WORKFLOW"
)"
if ! grep -Fq 'ENVIRONMENT: test' <<<"$fresh_database_migration_block" \
    || ! grep -Fq 'DATABASE_URL: postgresql://' \
      <<<"$fresh_database_migration_block"; then
  printf 'fresh migration verification must use the test config with real PostgreSQL\n' >&2
  exit 1
fi
for _conf in $NGINX_SERVER_CONFS; do
  if ! grep -Fq 'include /etc/nginx/conf.d/locations.inc;' "$_conf"; then
    printf 'every nginx server conf must include the shared locations snippet: %s\n' "$_conf" >&2
    exit 1
  fi
done
if ! grep -Fq 'location = /mcp {' "$NGINX_CONFIG" \
    || ! grep -Fq 'location /mcp/ {' "$NGINX_CONFIG"; then
  printf 'retired generic MCP paths must return an explicit production 404\n' >&2
  exit 1
fi
sshpass_command_count="$(grep -c 'sshpass -p' "$DEPLOY_WORKFLOW")"
strict_sshpass_command_count="$(
  grep -c 'sshpass -p.*StrictHostKeyChecking=yes' "$DEPLOY_WORKFLOW"
)"
if [ "$sshpass_command_count" -eq 0 ] ||
   [ "$sshpass_command_count" -ne "$strict_sshpass_command_count" ]; then
  printf 'every sshpass command must enforce the scanned host key\n' >&2
  exit 1
fi
if ! grep -Fq \
  'bash scripts/ci/create-deployment-archive.sh /tmp/openontology.tar.gz' \
  "$DEPLOY_WORKFLOW"; then
  printf 'deployment workflow must use the tested runtime archive builder\n' >&2
  exit 1
fi
if git -C "$REPO_ROOT" ls-files --error-unmatch \
    deploy/production.dependencies.env >/dev/null 2>&1; then
  printf 'the production dependency manifest must not be tracked; it is materialized from Repository secrets on the runner\n' >&2
  exit 1
fi
manifest_materialize_step_count="$(
  grep -cF 'bash scripts/ci/materialize-production-dependencies.sh' \
    "$DEPLOY_WORKFLOW"
)"
if [ "$manifest_materialize_step_count" -lt 2 ]; then
  printf 'verification and deploy must both materialize the production dependency manifest from Repository secrets\n' >&2
  exit 1
fi
kaneo_manifest_var_count="$(
  grep -cF 'PROD_KANEO_WEB_URL: ${{ vars.PROD_KANEO_WEB_URL }}' \
    "$DEPLOY_WORKFLOW"
)"
if [ "$kaneo_manifest_var_count" -lt 2 ]; then
  printf 'verification and deploy must both materialize the Kaneo manifest variables\n' >&2
  exit 1
fi
if ! grep -Fq 'Kaneo isolated service and bootstrap canary' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq '"kaneo-db", "kaneo-app", "kaneo-bootstrap"' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq 'python -m app.super_assistant.kaneo.bootstrap --verify' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq 'backend/scripts/kaneo_service_e2e.py' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq 'scripts/kaneo_service_e2e.py --create' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq 'scripts/kaneo_service_e2e.py --verify-persisted' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq 'OPENONTOLOGY_KANEO_E2E' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq 'ghcr.io/usekaneo/kaneo:2.49.0@sha256:cd1826b1aba3ce736015cadf6f6c975d923aac00f4b6aaa7aedb29bc722dae49' \
      "$DEPLOY_WORKFLOW"; then
  printf 'deployment workflow must run the isolated Kaneo bootstrap/service canary with pinned images\n' >&2
  exit 1
fi
if grep -Eq '^[[:space:]]+environment:[[:space:]]+production[[:space:]]*$' \
    "$DEPLOY_WORKFLOW"; then
  printf 'deployment workflow must not hardcode a production environment block\n' >&2
  exit 1
fi
if ! grep -Fq 'python3 deploy/runtime_source_attestation.py generate' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq -- '--commit "${GITHUB_SHA}"' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq 'deploy/runtime-source-manifest.json' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq "EXPECTED_RUNTIME_COMMIT='\${GITHUB_SHA}'" "$DEPLOY_WORKFLOW"; then
  printf 'deployment workflow must generate the exact-SHA runtime source manifest and pass the expected commit independently\n' >&2
  exit 1
fi
runtime_manifest_generate_line="$(grep -nF 'python3 deploy/runtime_source_attestation.py generate' "$DEPLOY_WORKFLOW" | head -n1 | cut -d: -f1)"
runtime_archive_line="$(grep -nF 'bash scripts/ci/create-deployment-archive.sh /tmp/openontology.tar.gz' "$DEPLOY_WORKFLOW" | head -n1 | cut -d: -f1)"
if [ -z "$runtime_manifest_generate_line" ] || [ -z "$runtime_archive_line" ] \
    || [ "$runtime_manifest_generate_line" -ge "$runtime_archive_line" ]; then
  printf 'runtime source manifest must be generated before the deployment archive is built\n' >&2
  exit 1
fi
if ! grep -Fq 'git rev-parse HEAD' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq 'HEAD_SHA' "$DEPLOY_WORKFLOW"; then
  printf 'runtime source manifest generation must bind to the exact checkout HEAD\n' >&2
  exit 1
fi
if ! grep -Fq 'deploy/runtime_source_attestation.py' "$ARCHIVE_SCRIPT" \
    || ! grep -Fq 'deploy/runtime-source-manifest.json' "$ARCHIVE_SCRIPT"; then
  printf 'deployment archive builder must include the runtime source verifier and manifest\n' >&2
  exit 1
fi
attestation_call_line="$(grep -nF 'if ! verify_runtime_source_attestation; then' "$DEPLOY_SCRIPT" | head -n1 | cut -d: -f1)"
readiness_line="$(grep -nF 'log "waiting for backend, pipeline executor, replica executor and frontend readiness' "$DEPLOY_SCRIPT" | head -n1 | cut -d: -f1)"
deployment_succeeded_line="$(grep -nF 'log "deployment succeeded"' "$DEPLOY_SCRIPT" | head -n1 | cut -d: -f1)"
if [ -z "$attestation_call_line" ] || [ -z "$readiness_line" ] \
    || [ -z "$deployment_succeeded_line" ] \
    || [ "$attestation_call_line" -le "$readiness_line" ] \
    || [ "$attestation_call_line" -ge "$deployment_succeeded_line" ]; then
  printf 'runtime source attestation must run after readiness and before deployment success\n' >&2
  exit 1
fi
if grep -nE 'docker inspect' "$DEPLOY_SCRIPT" | grep -v -- '--format' >/dev/null; then
  printf 'deployment must use docker inspect --format only; full inspect JSON is forbidden\n' >&2
  exit 1
fi
if grep -Fq '{{json' "$DEPLOY_SCRIPT" \
    || grep -Fq '{{.Config.Env' "$DEPLOY_SCRIPT" \
    || grep -Fq '{{json .Mounts}}' "$DEPLOY_SCRIPT"; then
  printf 'deployment must not dump full inspect, Env, or Mounts JSON\n' >&2
  exit 1
fi
if grep -Fq 'force-recreate' "$DEPLOY_SCRIPT" \
    || grep -Fq 'wait_for_backend_readiness' "$DEPLOY_SCRIPT"; then
  printf 'runtime source attestation must be read-only and must not add a new restart/readiness lifecycle\n' >&2
  exit 1
fi
if grep -Fq 'docker image ls' "$DEPLOY_SCRIPT" \
    || grep -Fq 'config --format json' "$DEPLOY_SCRIPT" \
    || grep -Fq 'config --images' "$DEPLOY_SCRIPT" \
    || grep -Fq -- '-backend$' "$DEPLOY_SCRIPT"; then
  printf 'backend image resolution must not use label/created-time candidates or guessed image names\n' >&2
  exit 1
fi
if ! grep -Fq 'compose build --print backend' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'backend_build_tag' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'docker image inspect --platform "$backend_image_platform"' "$DEPLOY_SCRIPT"; then
  printf 'backend image resolution must inspect the exact tag from Compose build --print backend\n' >&2
  exit 1
fi
if ! grep -Fq 'python3 "$SCRIPT_DIR/runtime_source_attestation.py" generate' "$DEPLOY_SCRIPT"; then
  printf 'direct Git deployment must explicitly generate the runtime source manifest\n' >&2
  exit 1
fi

stop_line="$(grep -nF \
  'compose stop --timeout 30 frontend pipeline_executor backend' \
  "$DEPLOY_SCRIPT" | tail -n1 | cut -d: -f1)"
migration_line="$(grep -nF \
  'run_with_retry compose run --rm --no-deps backend alembic upgrade head' \
  "$DEPLOY_SCRIPT" | cut -d: -f1)"
startup_line="$(grep -nF \
  'run_with_retry compose up -d --remove-orphans' \
  "$DEPLOY_SCRIPT" | cut -d: -f1)"
if [ -z "$stop_line" ] || [ -z "$migration_line" ] || [ -z "$startup_line" ] \
    || [ "$stop_line" -ge "$migration_line" ] \
    || [ "$migration_line" -ge "$startup_line" ]; then
  printf 'runtime must stop before migration and restart only after migration\n' >&2
  exit 1
fi
if grep -Fq 'profile_args+=(--profile hindsight)' "$DEPLOY_SCRIPT" \
    || [ "$(grep -cF -- '--profile hindsight up -d hindsight' "$DEPLOY_SCRIPT")" -ne 1 ] \
    || [ "$(grep -cF 'prepull_hindsight_images' "$DEPLOY_SCRIPT")" -ne 0 ] \
    || ! grep -Fq 'start_hindsight_memory_stack() {' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'stop_disabled_hindsight_stack' "$DEPLOY_SCRIPT"; then
  printf 'the optional Hindsight stack must start exactly once in a dedicated compose invocation and never via the shared profile injection or a pre-core pull\n' >&2
  exit 1
fi
hindsight_optional_call_line="$(
  grep -nF 'start_hindsight_memory_stack || log' "$DEPLOY_SCRIPT" \
    | head -n1 | cut -d: -f1
)"
if [ -z "$hindsight_optional_call_line" ] \
    || [ -z "$deployment_succeeded_line" ] \
    || [ "$hindsight_optional_call_line" -le "$deployment_succeeded_line" ]; then
  printf 'the optional Hindsight start must run only after the core deployment is declared successful\n' >&2
  exit 1
fi
if ! grep -Fq 'compose start "${previous_runtime_services[@]}"' \
    "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'migration_completed=1' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'pre_migration_revision="$(database_current_revision)"' \
      "$DEPLOY_SCRIPT" \
    || ! grep -Fq '"$current_revision" != "$pre_migration_revision"' \
      "$DEPLOY_SCRIPT"; then
  printf 'migration failure may restore the previous runtime only when the database revision is unchanged\n' >&2
  exit 1
fi
post_migration_stop_count="$(
  grep -cF 'stop_new_runtime_after_failed_deploy' "$DEPLOY_SCRIPT"
)"
if [ "$post_migration_stop_count" -ne 5 ] \
    || ! grep -Fq \
      'if ! compose stop --timeout 30 frontend pipeline_executor backend integrated_replica_executor; then' \
      "$DEPLOY_SCRIPT"; then
  printf 'post-migration startup, readiness, or attestation failures must stop the new runtime\n' >&2
  exit 1
fi

# Keep the production container hardening from being removed during a
# Compose edit.  These settings are defense in depth and do not replace the
# dedicated rootless runner required for user process plugins.
for hardened_service in browser python_kernel_gateway backend pipeline_executor; do
  hardened_block="$(awk -v service="$hardened_service" '
    $0 == "  " service ":" { in_service = 1 }
    in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
    in_service { print }
  ' "$PROD_COMPOSE")"
  if ! grep -Fq 'no-new-privileges:true' <<<"$hardened_block" \
      || ! grep -Fq '      - ALL' <<<"$hardened_block" \
      || ! grep -Eq '/tmp:size=[0-9]+m,nosuid,nodev' <<<"$hardened_block"; then
    printf '%s must retain no-new-privileges, cap_drop ALL, and a hardened /tmp tmpfs\n' \
      "$hardened_service" >&2
    exit 1
  fi
done

for private_network_service in backend pipeline_executor; do
  private_network_block="$(awk -v service="$private_network_service" '
    $0 == "  " service ":" { in_service = 1 }
    in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
    in_service { print }
  ' "$PROD_COMPOSE")"
  if ! grep -Fq 'STEWARD_BROWSER_ALLOW_PRIVATE_NETWORKS: "false"' \
      <<<"$private_network_block"; then
    printf '%s must explicitly deny browser private-network targets\n' \
      "$private_network_service" >&2
    exit 1
  fi
done

browser_block="$(awk -v service=browser '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
if ! grep -Fq 'user: "10001:10001"' <<<"$browser_block" \
    || ! grep -Fq 'read_only: true' <<<"$browser_block" \
    || ! grep -Fq 'USER 10001:10001' docker/browser/Dockerfile; then
  printf 'browser must run as the fixed non-root UID 10001\n' >&2
  exit 1
fi
if ! grep -Fq 'XDG_CACHE_HOME=/tmp/browser-cache' docker/browser/Dockerfile; then
  printf 'browser must keep its cache in a dedicated tmpfs subdirectory\n' >&2
  exit 1
fi
if ! grep -Fq 'fontconfig=2.15.0-2.3' docker/browser/Dockerfile \
    || ! grep -Fq 'fonts-noto-cjk=1:20240730+repack1-1' docker/browser/Dockerfile; then
  printf 'browser APT packages must be pinned to immutable versions\n' >&2
  exit 1
fi
if ! grep -Fq 'BROWSER_IMAGE:-chromedp/headless-shell@sha256:' "$PROD_COMPOSE" \
    || grep -Fq 'BROWSER_IMAGE:-chromedp/headless-shell:latest' "$PROD_COMPOSE"; then
  printf 'browser base image default must be pinned by digest\n' >&2
  exit 1
fi

# Integrated dbx is a fixed official image with no host port, reachable only
# by backend on a dedicated outbound-capable bridge (it must reach source DBs).
dbx_block="$(awk -v service=dbx '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
if ! grep -Fq     'docker.cnb.cool/dbxio.com/dbx@sha256:b661f7e35deed06d9611dcda79c44ffcc8e4fd0f44308028ea77f7f18b36638c'     <<<"$dbx_block"     || ! grep -Fq 'profiles: [integrated-dbx]' <<<"$dbx_block"     || ! grep -Fq 'DBX_PASSWORD: ${INTEGRATED_DBX_PASSWORD:?integrated dbx password is required}'       <<<"$dbx_block"     || grep -Eq '^[[:space:]]+ports:' <<<"$dbx_block"     || ! grep -Fq '      - dbx_bridge' <<<"$dbx_block"     || ! grep -Fq '  dbx_bridge:' "$PROD_COMPOSE"     || ! grep -Fq '    driver: bridge' "$PROD_COMPOSE"     || grep -Fq 'internal: true' "$PROD_COMPOSE"; then
  printf 'production dbx must be pinned, profile-gated, host-port-free, and on a normal outbound bridge\n' >&2
  exit 1
fi
backend_block="$(awk -v service=backend '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
if ! grep -Fq '      - dbx_bridge' <<<"$backend_block"; then
  printf 'backend must join the dedicated dbx bridge\n' >&2
  exit 1
fi
if ! grep -Fq 'configure_integrated_dbx' "$DEPLOY_SCRIPT"     || ! grep -Fq 'INTEGRATED_DBX_COMMIT_PIN="0137e0cbc8b663da5a17ec424c6d47e4e5570ead"'       "$DEPLOY_SCRIPT"     || ! grep -Fq 'start_integrated_dbx_if_needed' "$DEPLOY_SCRIPT"     || ! grep -Fq 'INTEGRATED_DBX_PASSWORD' "$DEPLOY_SCRIPT"; then
  printf 'deploy must configure/pin/preflight dbx and redact its password\n' >&2
  exit 1
fi

# Integrated replica executor: defined once in the standard local/prod files and
# started by the standard stack (no profiles gate — the approved plan mandates
# auto-start), host-port free, docker.sock free, env_file free (bootstrap
# secrets in .env stay out), backed by the dedicated single-writer state volume,
# with capacity knobs pinned to the honest default (2 concurrent captures).
# Compose silently merges same-name services, so the CDC overlay must never
# redefine the service.
replica_block="$(awk -v service=integrated_replica_executor '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && /^  (#|[A-Za-z0-9_-]+:)/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
local_replica_block="$(awk -v service=integrated_replica_executor '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && /^  (#|[A-Za-z0-9_-]+:)/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$LOCAL_COMPOSE")"
if [ -z "$replica_block" ] || [ -z "$local_replica_block" ] \
    || grep -Eq '^[[:space:]]+profiles:' <<<"$replica_block" \
    || grep -Eq '^[[:space:]]+profiles:' <<<"$local_replica_block" \
    || grep -Eq '^[[:space:]]+ports:' <<<"$replica_block" \
    || grep -Eq '^[[:space:]]+ports:' <<<"$local_replica_block" \
    || grep -Fq 'docker.sock' <<<"$replica_block" \
    || grep -Fq 'docker.sock' <<<"$local_replica_block" \
    || grep -Eq '^[[:space:]]+env_file:' <<<"$replica_block" \
    || grep -Eq '^[[:space:]]+env_file:' <<<"$local_replica_block" \
    || grep -Eq '^[[:space:]]+container_name:' <<<"$replica_block" \
    || grep -Eq '^[[:space:]]+container_name:' <<<"$local_replica_block" \
    || ! grep -Fq 'REPLICA_MAX_CAPTURES=2' "$REPO_ROOT/.env.example" \
    || ! grep -Fq 'command: python -m app.data_channel.integrated.replica_executor' <<<"$replica_block" \
    || ! grep -Fq 'command: python -m app.data_channel.integrated.replica_executor' <<<"$local_replica_block" \
    || ! grep -Fq 'REPLICA_STATE_ROOT: /var/lib/openontology-replicas' <<<"$replica_block" \
    || ! grep -Fq 'integrated_replica_state:/var/lib/openontology-replicas' <<<"$replica_block" \
    || ! grep -Fq 'REPLICA_MAX_CAPTURES: ${REPLICA_MAX_CAPTURES:-2}' <<<"$replica_block" \
    || ! grep -Fq 'mem_limit: ${REPLICA_EXECUTOR_MEM_LIMIT:-3g}' <<<"$replica_block" \
    || ! grep -Fq '  integrated_replica_state:' "$PROD_COMPOSE" \
    || ! grep -Fq '  integrated_replica_state:' "$LOCAL_COMPOSE" \
    || ! grep -Fq 'check_replica_executor' "$DEPLOY_SCRIPT" \
    || ! grep -Fq '&& check_replica_executor' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'REPLICA_MAX_CAPTURES must be within 1..64' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'compose stop --timeout 30 frontend pipeline_executor backend integrated_replica_executor' "$DEPLOY_SCRIPT" \
    || grep -Fq 'integrated_replica_executor:' "$REPO_ROOT/docker-compose.cdc.yml"; then
  printf 'integrated_replica_executor must auto-start without profiles, stay port/sock/env_file-free, remain scale-safe (no container_name), document REPLICA_MAX_CAPTURES in .env.example, be volume-backed with honest capacity defaults, participate in deploy readiness/rollback stop, and must not be redefined by the CDC overlay\n' >&2
  exit 1
fi
for key in \
  ENVIRONMENT DATABASE_URL SECRET_KEY ENCRYPTION_KEY CORS_ALLOWED_ORIGINS \
  REDIS_URL N8N_API_URL N8N_API_KEY NEO4J_URI NEO4J_USER NEO4J_PASSWORD \
  MINIO_ENDPOINT MINIO_ACCESS_KEY MINIO_SECRET_KEY STEWARD_BROWSER_CDP_URL \
  STEWARD_BROWSER_ALLOW_PRIVATE_NETWORKS WEKNORA_BASE_URL WEKNORA_WEB_URL \
  KANEO_BASE_URL KANEO_WEB_URL PIPELINE_FILE_PUBLIC_APP_BASE_URL \
  PIPELINE_FILE_PUBLIC_API_BASE_URL PYTHON_KERNEL_GATEWAY_AUTH_TOKEN \
  FIRST_ADMIN_PASSWORD INTEGRATED_DBX_URL INTEGRATED_DBX_PASSWORD \
  INTEGRATED_DBX_COMMIT; do
  if ! grep -Eq "^[[:space:]]+$key:" <<<"$replica_block" \
      || ! grep -Eq "^[[:space:]]+$key:" <<<"$local_replica_block"; then
    printf 'integrated_replica_executor must explicitly receive shared Settings dependency %s in prod and local compose\n' "$key" >&2
    exit 1
  fi
done
if ! grep -Fq \
    'COPY --from=debezium /tmp/replica-smt-artifacts/replica-smt.jar /opt/debezium/lib/replica-smt.jar' \
    "$REPO_ROOT/backend/Dockerfile.cdc" \
    || ! grep -Fq \
    'mkdir -p /var/lib/openontology-cdc /var/lib/openontology-replicas' \
    "$REPO_ROOT/backend/Dockerfile.cdc" \
    || ! grep -Fq 'backend/integrated_runtime' "$ARCHIVE_SCRIPT" \
    || ! grep -Fq 'COPY --chmod=0755 integrated_runtime/replica/ /src/replica-smt/' \
      "$REPO_ROOT/backend/Dockerfile.cdc" \
    || ! grep -Fq 'COPY --chown=10001:10001 . .' "$REPO_ROOT/backend/Dockerfile.cdc" \
    || ! grep -Fq 'USER 10001:10001' "$REPO_ROOT/backend/Dockerfile.cdc"; then
  printf 'Dockerfile.cdc must ship replica-smt.jar, preset both state roots, and keep archived sources readable by the donor and non-root runtime\n' >&2
  exit 1
fi

# The real replica acceptance gate must stay wired to the docker-build job,
# use an isolated port-free fixture stack, and upload evidence even on failure.
replica_acceptance_wrapper="$REPO_ROOT/scripts/ci/test-integrated-replica-acceptance.sh"
replica_acceptance_compose="$REPO_ROOT/docker-compose.replica-acceptance.yml"
if [ ! -x "$replica_acceptance_wrapper" ] \
    || [ ! -f "$replica_acceptance_compose" ] \
    || ! grep -Fq -- '--project' "$replica_acceptance_wrapper" \
    || ! grep -Fq -- '--network' "$replica_acceptance_wrapper" \
    || ! grep -Fq -- '--target-db' "$replica_acceptance_wrapper" \
    || ! grep -Fq -- '--jar builtin' "$replica_acceptance_wrapper" \
    || ! grep -Fq -- '--artifacts' "$replica_acceptance_wrapper" \
    || ! grep -Fq -- '--timeout' "$replica_acceptance_wrapper" \
    || ! grep -Fq '${PROJECT}-acceptance-driver' "$replica_acceptance_wrapper" \
    || ! grep -Fq 'down --volumes --remove-orphans' "$replica_acceptance_wrapper" \
    || grep -Eq '^[[:space:]]+ports:' "$replica_acceptance_compose" \
    || ! grep -Fq 'replica_fixture' "$replica_acceptance_compose" \
    || ! grep -Fq 'Integrated replica acceptance (real PG/MySQL)' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq 'bash scripts/ci/test-integrated-replica-acceptance.sh' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq 'name: integrated-replica-acceptance-${{ github.run_id }}' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq 'path: .artifacts/ci-replicaacceptance' "$DEPLOY_WORKFLOW" \
    || ! grep -Fq 'trap '\''rc=$?; cleanup; trap - EXIT; exit "$rc"'\'' EXIT' "$replica_acceptance_wrapper" \
    || ! grep -Fq 'ENVIRONMENT=production' "$replica_acceptance_wrapper" \
    || ! grep -Fq 'from app.shared.config import settings' "$replica_acceptance_wrapper" \
    || ! grep -Fq 'production executor imports passed' "$replica_acceptance_wrapper" \
    || ! grep -Fq 'CORS_ALLOWED_ORIGINS=' "$replica_acceptance_wrapper" \
    || grep -Eq 'test-integrated-replica-acceptance\.sh.*\|\|[[:space:]]*true' "$DEPLOY_WORKFLOW"; then
  printf 'real replica acceptance must be wired to the docker-build job, cover synthetic production imports, use an isolated port-free fixture stack, upload evidence, and never swallow failures\n' >&2
  exit 1
fi
for key in \
  ENVIRONMENT DATABASE_URL SECRET_KEY ENCRYPTION_KEY CORS_ALLOWED_ORIGINS \
  REDIS_URL N8N_API_URL N8N_API_KEY NEO4J_URI NEO4J_USER NEO4J_PASSWORD \
  MINIO_ENDPOINT MINIO_ACCESS_KEY MINIO_SECRET_KEY STEWARD_BROWSER_CDP_URL \
  STEWARD_BROWSER_ALLOW_PRIVATE_NETWORKS WEKNORA_BASE_URL WEKNORA_WEB_URL \
  KANEO_BASE_URL KANEO_WEB_URL PIPELINE_FILE_PUBLIC_APP_BASE_URL \
  PIPELINE_FILE_PUBLIC_API_BASE_URL PYTHON_KERNEL_GATEWAY_AUTH_TOKEN \
  FIRST_ADMIN_PASSWORD INTEGRATED_DBX_URL INTEGRATED_DBX_PASSWORD \
  INTEGRATED_DBX_COMMIT; do
  if ! grep -Fq -- "-e $key=" "$replica_acceptance_wrapper"; then
    printf 'real replica acceptance production import must pass synthetic dependency %s\n' "$key" >&2
    exit 1
  fi
done

# WeKnora app/MCP must stay private; only the native UI gateway publishes
# ports. The formal app is registration-closed, and the registration-open
# initializer is profile-gated and removed before the gateway starts.
weknora_app_block="$(awk -v service=weknora-app '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
weknora_init_block="$(awk -v service=weknora-app-init '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
weknora_bootstrap_block="$(awk -v service=weknora-bootstrap '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
weknora_gateway_block="$(awk -v service=weknora-gateway '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
if [ -z "$weknora_app_block" ] || [ -z "$weknora_init_block" ] \
    || [ -z "$weknora_bootstrap_block" ] || [ -z "$weknora_gateway_block" ] \
    || grep -Eq '^[[:space:]]+ports:' <<<"$weknora_app_block" \
    || grep -Eq '^[[:space:]]+ports:' <<<"$weknora_init_block" \
    || grep -Eq '^[[:space:]]+ports:' <<<"$weknora_bootstrap_block" \
    || ! grep -Fq 'DISABLE_REGISTRATION: "true"' <<<"$weknora_app_block" \
    || ! grep -Fq 'profiles: [weknora-bootstrap]' <<<"$weknora_init_block" \
    || ! grep -Fq 'DISABLE_REGISTRATION: "false"' <<<"$weknora_init_block" \
    || ! grep -Fq 'profiles: [weknora-bootstrap]' <<<"$weknora_bootstrap_block" \
    || ! grep -Fq 'WEKNORA_BOOTSTRAP_BASE_URL: http://weknora-app-init:8080' <<<"$weknora_bootstrap_block" \
    || ! grep -Fq 'WEKNORA_BASE_URL: http://weknora-app:8080' <<<"$weknora_bootstrap_block" \
    || ! grep -Fq 'weknora_internal' <<<"$backend_block" \
    || ! grep -Fq '${WEKNORA_HTTP_PORT:-8089}:80' <<<"$weknora_gateway_block" \
    || ! grep -Fq '${WEKNORA_HTTPS_PORT:-8444}:443' <<<"$weknora_gateway_block" \
    || grep -Fq 'backend:' <<<"$weknora_gateway_block" \
    || ! grep -Fq 'frontend_ssl_certs:/etc/nginx/certs:ro' <<<"$weknora_gateway_block" \
    || ! grep -Fq 'location /mcp/' "$REPO_ROOT/docker/weknora/gateway.conf" \
    || ! grep -Fq 'return 404;' "$REPO_ROOT/docker/weknora/gateway.conf" \
    || ! grep -Fq 'location = /sso/entry {' \
      "$REPO_ROOT/docker/weknora/gateway.conf" \
    || ! grep -Fq 'resolver 127.0.0.11 valid=30s ipv6=off;' \
      "$REPO_ROOT/docker/weknora/gateway.conf" \
    || ! grep -Fq 'proxy_pass http://$weknora_sso_backend/api/v2/super-assistant/weknora/sso/consume$is_args$args;' \
      "$REPO_ROOT/docker/weknora/gateway.conf" \
    || ! grep -Fq 'run_weknora_bootstrap --verify' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'python -m app.super_assistant.weknora.bootstrap "$@"' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'compose --profile weknora-bootstrap build --pull weknora-bootstrap' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'check_weknora_public_entry' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'WeKnora SSO entry relay must answer 302 on the HTTPS entry (got ${sso_status})' "$DEPLOY_SCRIPT"; then
  printf 'WeKnora app/MCP must be private, formal registration closed, init profile-gated, only the UI gateway published, and the SSO entry relay wired through the gateway without a compose dependency on backend\n' >&2
  exit 1
fi

# Kaneo app/bootstrap stay private with registration disabled everywhere (an
# empty instance still allows the first administrator); only the native nginx
# gateway publishes ports and it never reaches the platform backend network.
kaneo_db_block="$(awk -v service=kaneo-db '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
kaneo_app_block="$(awk -v service=kaneo-app '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
kaneo_bootstrap_block="$(awk -v service=kaneo-bootstrap '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
kaneo_gateway_block="$(awk -v service=kaneo-gateway '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
if [ -z "$kaneo_db_block" ] || [ -z "$kaneo_app_block" ] \
    || [ -z "$kaneo_bootstrap_block" ] || [ -z "$kaneo_gateway_block" ] \
    || grep -Eq '^[[:space:]]+ports:' <<<"$kaneo_db_block" \
    || grep -Eq '^[[:space:]]+ports:' <<<"$kaneo_app_block" \
    || grep -Eq '^[[:space:]]+ports:' <<<"$kaneo_bootstrap_block" \
    || ! grep -Fq 'DISABLE_REGISTRATION: "true"' <<<"$kaneo_app_block" \
    || ! grep -Fq 'kaneo_postgres_data:/var/lib/postgresql/data' <<<"$kaneo_db_block" \
    || ! grep -Fq 'kaneo_data:/var/lib/kaneo' <<<"$kaneo_app_block" \
    || ! grep -Fq 'restart: "no"' <<<"$kaneo_bootstrap_block" \
    || ! grep -Fq \
      'python -m app.super_assistant.kaneo.bootstrap && python -m app.super_assistant.kaneo.bootstrap --verify' \
      <<<"$kaneo_bootstrap_block" \
    || ! grep -Fq 'KANEO_BASE_URL: http://kaneo-app:5173' <<<"$kaneo_bootstrap_block" \
    || ! grep -Fq 'kaneo_internal' <<<"$backend_block" \
    || ! grep -Fq 'kaneo-bootstrap:' <<<"$backend_block" \
    || ! grep -Fq 'kaneo-bootstrap:' <<<"$kaneo_gateway_block" \
    || ! grep -Fq 'service_completed_successfully' <<<"$kaneo_gateway_block" \
    || ! grep -Fq '${KANEO_HTTP_PORT:-8090}:80' <<<"$kaneo_gateway_block" \
    || ! grep -Fq '${KANEO_HTTPS_PORT:-8445}:443' <<<"$kaneo_gateway_block" \
    || grep -Fq 'backend:' <<<"$kaneo_gateway_block" \
    || ! grep -Fq 'frontend_ssl_certs:/etc/nginx/certs:ro' <<<"$kaneo_gateway_block" \
    || ! grep -Fq 'return 308 ${KANEO_WEB_URL}$request_uri;' \
      "$REPO_ROOT/docker/kaneo/gateway.conf.template" \
    || ! grep -Fq 'proxy_pass http://kaneo-app:5173;' \
      "$REPO_ROOT/docker/kaneo/gateway.conf.template" \
    || ! grep -Fq 'location = /sso/entry {' \
      "$REPO_ROOT/docker/kaneo/gateway.conf.template" \
    || ! grep -Fq 'resolver 127.0.0.11 valid=30s ipv6=off;' \
      "$REPO_ROOT/docker/kaneo/gateway.conf.template" \
    || ! grep -Fq 'proxy_pass http://$kaneo_sso_backend/api/v2/super-assistant/kaneo/sso/consume$is_args$args;' \
      "$REPO_ROOT/docker/kaneo/gateway.conf.template" \
    || ! grep -Fq '"https://127.0.0.1:${https_port}/sso/entry"' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'proxy_set_header Upgrade $http_upgrade;' \
      "$REPO_ROOT/docker/kaneo/kaneo-proxy.inc" \
    || ! grep -Fq 'run_kaneo_bootstrap --verify' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'python -m app.super_assistant.kaneo.bootstrap "$@"' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'compose --profile kaneo-bootstrap build --pull kaneo-bootstrap' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'run --rm --no-deps kaneo-bootstrap' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'start_and_verify_kaneo_stack' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'check_kaneo_public_entry' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'KANEO_APP_IMAGE KANEO_POSTGRES_IMAGE' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'KANEO_ADMIN_PASSWORD KANEO_DB_PASSWORD KANEO_AUTH_SECRET' "$DEPLOY_SCRIPT" \
    || grep -Fq 'stop --timeout 30 kaneo-gateway >/dev/null 2>&1 || true' \
      "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'compose stop --timeout 30 kaneo-gateway' "$DEPLOY_SCRIPT"; then
  printf 'Kaneo app/bootstrap must be private with registration closed, only the native gateway published, and the SSO entry relay wired through the gateway without a compose dependency on backend\n' >&2
  exit 1
fi

# Grafana stays private with the auth-proxy contract pinned (empty dashboards,
# no provisioning, embedded kiosk previews); the shared-admin password reaches
# only grafana, backend and grafana-bootstrap via the mask anchor exception.
grafana_block="$(awk -v service=grafana '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
grafana_bootstrap_block="$(awk -v service=grafana-bootstrap '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
frontend_prod_block="$(awk -v service=frontend '
  $0 == "  " service ":" { in_service = 1 }
  in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
  in_service { print }
' "$PROD_COMPOSE")"
if [ -z "$grafana_block" ] || [ -z "$grafana_bootstrap_block" ] \
    || grep -Eq '^[[:space:]]+ports:' <<<"$grafana_block" \
    || grep -Fq 'provisioning' <<<"$grafana_block" \
    || ! grep -Fq 'GF_SECURITY_ALLOW_EMBEDDING: "true"' <<<"$grafana_block" \
    || ! grep -Fq 'GF_SERVER_SERVE_FROM_SUB_PATH: "true"' <<<"$grafana_block" \
    || ! grep -Fq 'GF_SERVER_ROOT_URL: "%(protocol)s://%(domain)s/grafana/"' <<<"$grafana_block" \
    || ! grep -Fq 'GF_USERS_ALLOW_SIGN_UP: "false"' <<<"$grafana_block" \
    || ! grep -Fq 'GF_AUTH_ANONYMOUS_ENABLED: "false"' <<<"$grafana_block" \
    || ! grep -Fq 'GF_AUTH_PROXY_ENABLED: "true"' <<<"$grafana_block" \
    || ! grep -Fq 'GF_AUTH_PROXY_HEADER_NAME: X-WEBAUTH-USER' <<<"$grafana_block" \
    || ! grep -Fq 'GF_AUTH_PROXY_AUTO_SIGN_UP: "false"' <<<"$grafana_block" \
    || ! grep -Fq 'GF_AUTH_DISABLE_LOGIN_FORM: "true"' <<<"$grafana_block" \
    || ! grep -Fq 'GF_AUTH_DISABLE_SIGNOUT_MENU: "true"' <<<"$grafana_block" \
    || ! grep -Fq 'grafana_data:/var/lib/grafana' <<<"$grafana_block" \
    || ! grep -Fq 'http://127.0.0.1:3000/grafana/api/health' <<<"$grafana_block" \
    || grep -Fq 'GF_SECURITY_ADMIN_PASSWORD: ""' <<<"$grafana_block" \
    || ! grep -Fq 'restart: "no"' <<<"$grafana_bootstrap_block" \
    || ! grep -Fq \
      'python -m app.operations.grafana.bootstrap && python -m app.operations.grafana.bootstrap --verify' \
      <<<"$grafana_bootstrap_block" \
    || ! grep -Fq 'GRAFANA_BASE_URL: http://grafana:3000' <<<"$grafana_bootstrap_block" \
    || grep -Fq 'GRAFANA_ADMIN_PASSWORD: ""' <<<"$grafana_bootstrap_block" \
    || ! grep -Fq 'grafana_internal' <<<"$backend_block" \
    || ! grep -Fq 'grafana:' <<<"$backend_block" \
    || ! grep -Fq 'grafana_internal' <<<"$frontend_prod_block" \
    || ! grep -Fq 'grafana-bootstrap:' <<<"$frontend_prod_block" \
    || ! grep -Fq 'service_completed_successfully' <<<"$frontend_prod_block" \
    || ! grep -Fq '  grafana_data:' "$PROD_COMPOSE" \
    || ! grep -Fq '  grafana_internal:' "$PROD_COMPOSE" \
    || ! grep -Fq 'GRAFANA_ADMIN_PASSWORD: ""' "$PROD_COMPOSE"; then
  printf 'Grafana must be private, unpinned-provisioning-free, auth-proxy-configured, masked everywhere except grafana/backend/bootstrap, and wired into backend and the frontend gateway\n' >&2
  exit 1
fi

# The shared /grafana/ gateway contract: nginx validates the board session
# cookie on every HTTP and WS request through auth_request with the original
# Cookie header explicitly forwarded, overwrites client identity headers,
# redirects the bare /grafana path, and never exposes Grafana without a valid
# session. The Vite dev proxy implements the same per-request validation
# without any static admin bypass, and the local compose grafana stays on the
# loopback-only exception.
if ! grep -Fq 'location = /_grafana_auth {' "$REPO_ROOT/frontend/nginx/locations.inc" \
    || ! grep -Fq 'auth_request /_grafana_auth;' "$REPO_ROOT/frontend/nginx/locations.inc" \
    || ! grep -Fq 'proxy_set_header Cookie $http_cookie;' "$REPO_ROOT/frontend/nginx/locations.inc" \
    || ! grep -Fq 'auth_request_set $grafana_user $upstream_http_x_grafana_user;' "$REPO_ROOT/frontend/nginx/locations.inc" \
    || ! grep -Fq 'proxy_set_header X-WEBAUTH-USER $grafana_user;' "$REPO_ROOT/frontend/nginx/locations.inc" \
    || ! grep -Fq 'location = /grafana {' "$REPO_ROOT/frontend/nginx/locations.inc" \
    || ! grep -Fq 'return 301 /grafana/;' "$REPO_ROOT/frontend/nginx/locations.inc" \
    || ! grep -Fq 'charset utf-8;' "$REPO_ROOT/frontend/nginx/locations.inc" \
    || ! grep -Fq 'resolver 127.0.0.11 valid=30s ipv6=off;' "$REPO_ROOT/frontend/nginx/locations.inc" \
    || ! grep -Fq 'proxy_set_header Host $http_host;' "$REPO_ROOT/frontend/nginx/locations.inc" \
    || ! grep -Fq '/api/v2/operations/dashboards/grafana-auth' "$REPO_ROOT/frontend/nginx/locations.inc" \
    || ! grep -Fq 'configure_grafana_env' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'validate_grafana_config' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'run_grafana_bootstrap' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'start_and_verify_grafana_stack' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'check_grafana_public_entry' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'grafana/grafana:13.3.1@sha256:0615ec4fbd4072004b7d86b46ded63013a74b7d6eb1119273dd89e8fa25c13cf' \
      "$DEPLOY_SCRIPT" \
    || ! grep -Fq '  GRAFANA_IMAGE; do' "$DEPLOY_SCRIPT"; then
  printf 'the /grafana/ gateway must validate the session cookie per request (nginx auth_request and the Vite dev middleware), pin the official image, and enforce the deploy-time Grafana chain\n' >&2
  exit 1
fi

# The Vite dev middleware validates each request with the real cookie and
# fails closed; a static admin injection (the pre-contract shortcut) must
# never come back, and non-Grafana websockets (vite HMR) must be left alone.
if ! grep -Fq 'function grafanaSessionProxyPlugin' "$REPO_ROOT/frontend/vite.config.ts" \
    || ! grep -Fq "grafana-auth" "$REPO_ROOT/frontend/vite.config.ts" \
    || ! grep -Fq "res.status !== 204" "$REPO_ROOT/frontend/vite.config.ts" \
    || ! grep -Fq "'x-webauth-user': user" "$REPO_ROOT/frontend/vite.config.ts" \
    || grep -Eq "X-WEBAUTH-USER'?: 'admin'|x-webauth-user'?: 'admin'" "$REPO_ROOT/frontend/vite.config.ts" \
    || ! grep -Fq "GRAFANA_PROXY_TARGET" "$REPO_ROOT/frontend/vite.config.ts" \
    || ! grep -Fq "server.httpServer?.on('upgrade'" "$REPO_ROOT/frontend/vite.config.ts"; then
  printf 'the Vite dev /grafana proxy must validate the real cookie per request (HTTP and WS) with no static admin bypass\n' >&2
  exit 1
fi

local_compose_block() {
  local service="$1"
  awk -v service="$service" '
    $0 == "  " service ":" { in_service = 1 }
    in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
    in_service { print }
  ' "$LOCAL_COMPOSE"
}
local_init_block="$(local_compose_block weknora-app-init)"
local_bootstrap_block="$(local_compose_block weknora-bootstrap)"
local_app_block="$(local_compose_block weknora-app)"
local_verify_block="$(local_compose_block weknora-verify)"
local_gateway_block="$(local_compose_block weknora-gateway)"
if [ -z "$local_init_block" ] || [ -z "$local_bootstrap_block" ] \
    || [ -z "$local_app_block" ] || [ -z "$local_verify_block" ] \
    || [ -z "$local_gateway_block" ] \
    || grep -Eq '^[[:space:]]+ports:' <<<"$local_init_block" \
    || ! grep -Fq 'DISABLE_REGISTRATION: "false"' <<<"$local_init_block" \
    || grep -Eq '^[[:space:]]+profiles:' <<<"$local_bootstrap_block" \
    || ! grep -Fq 'WEKNORA_BOOTSTRAP_BASE_URL: http://weknora-app-init:8080' <<<"$local_bootstrap_block" \
    || ! grep -Fq 'DISABLE_REGISTRATION: "true"' <<<"$local_app_block" \
    || ! grep -Fq 'weknora-bootstrap:' <<<"$local_app_block" \
    || ! grep -Fq 'service_completed_successfully' <<<"$local_app_block" \
    || grep -Eq '^[[:space:]]+profiles:' <<<"$local_verify_block" \
    || ! grep -Fq -- '--verify' <<<"$local_verify_block" \
    || ! grep -Fq 'weknora-bootstrap:' <<<"$local_verify_block" \
    || ! grep -Fq 'weknora-verify:' <<<"$local_gateway_block" \
    || grep -Fq 'backend:' <<<"$local_gateway_block" \
    || ! grep -Fq 'service_completed_successfully' <<<"$local_gateway_block"; then
  printf 'local WeKnora startup must auto-run init/bootstrap/verify and hide the gateway until success\n' >&2
  exit 1
fi

local_kaneo_bootstrap_block="$(local_compose_block kaneo-bootstrap)"
local_kaneo_app_block="$(local_compose_block kaneo-app)"
local_kaneo_gateway_block="$(local_compose_block kaneo-gateway)"
if [ -z "$local_kaneo_bootstrap_block" ] || [ -z "$local_kaneo_app_block" ] \
    || [ -z "$local_kaneo_gateway_block" ] \
    || grep -Eq '^[[:space:]]+profiles:' <<<"$local_kaneo_bootstrap_block" \
    || ! grep -Fq 'DISABLE_REGISTRATION: "true"' <<<"$local_kaneo_app_block" \
    || ! grep -Fq '127.0.0.1:${KANEO_APP_LOOPBACK_PORT:-18090}:5173' \
      <<<"$local_kaneo_app_block" \
    || ! grep -Fq 'kaneo-bootstrap:' <<<"$local_kaneo_gateway_block" \
    || ! grep -Fq 'service_completed_successfully' <<<"$local_kaneo_gateway_block" \
    || grep -Fq 'backend:' <<<"$local_kaneo_gateway_block" \
    || ! grep -Fq 'weknora_ssl_certs:/etc/nginx/certs:ro' \
      <<<"$local_kaneo_gateway_block"; then
  printf 'local Kaneo startup must auto-run the bootstrap and hide the gateway until success\n' >&2
  exit 1
fi

# Local Grafana keeps the production auth-proxy contract on the loopback-only
# exception port; the vite container proxies /grafana/ with per-request
# session validation (no static admin bypass, no 0.0.0.0 publication).
local_grafana_block="$(local_compose_block grafana)"
local_grafana_bootstrap_block="$(local_compose_block grafana-bootstrap)"
local_frontend_block="$(local_compose_block frontend)"
if [ -z "$local_grafana_block" ] || [ -z "$local_grafana_bootstrap_block" ] \
    || grep -Fq 'GF_SECURITY_ADMIN_PASSWORD: ""' <<<"$local_grafana_block" \
    || ! grep -Fq 'GF_AUTH_PROXY_ENABLED: "true"' <<<"$local_grafana_block" \
    || ! grep -Fq 'GF_AUTH_DISABLE_LOGIN_FORM: "true"' <<<"$local_grafana_block" \
    || ! grep -Fq '127.0.0.1:${GRAFANA_APP_LOOPBACK_PORT:-13001}:3000' \
      <<<"$local_grafana_block" \
    || ! grep -Fq 'grafana_data:/var/lib/grafana' <<<"$local_grafana_block" \
    || ! grep -Fq 'restart: "no"' <<<"$local_grafana_bootstrap_block" \
    || ! grep -Fq \
      'python -m app.operations.grafana.bootstrap && python -m app.operations.grafana.bootstrap --verify' \
      <<<"$local_grafana_bootstrap_block" \
    || ! grep -Fq 'GRAFANA_PROXY_TARGET: ${GRAFANA_PROXY_TARGET:-http://grafana:3000}' \
      <<<"$local_frontend_block" \
    || ! grep -Fq 'grafana_internal' <<<"$local_frontend_block" \
    || ! grep -Fq 'grafana-bootstrap:' <<<"$local_frontend_block" \
    || ! grep -Fq '  grafana_data:' "$LOCAL_COMPOSE" \
    || ! grep -Fq '  grafana_internal:' "$LOCAL_COMPOSE"; then
  printf 'local Grafana must start with the same pinned contract on a loopback-only port, with the vite container proxying /grafana/ without an admin bypass\n' >&2
  exit 1
fi

# Multica 默认服务（默认启用：本地经 .env.example 的 COMPOSE_PROFILES=multica
# 随标准栈启动；生产 MULTICA_ENABLED 留空/true 均启用，仅显式 false 关闭）。
# 官方 v0.11.0 栈契约已核验（digest ↔ 上游 release commit 的 OCI revision
# 标签一致），compose 必须以已验证 digest 为默认值引用（禁 latest/浮动
# tag），且不得用 :? 强制变量——inactive profile 的插值也不能阻断核心
# compose。生产 API 容器不发布宿主端口；web 宿主端口默认只绑 loopback
# （MULTICA_WEB_BIND_ADDR，登录/PAT 界面不得明文公网暴露）；三个 multica
# 容器都只在 multica_internal 私网，API 绝不挂 default——确要调用的
# backend 与 pipeline_executor 按既有钉扎网络清单另行加入该专网（与
# WeKnora/Kaneo/Hindsight 同构）；multica_internal 保持普通 bridge（非
# internal）：官方 backend 需要出站（RESEND 验证邮件、Multica 智能体调用
# 管理员配置的模型服务），标 internal 只会破坏官方 web/agent 依赖而不增加
# 真实隔离面。本地仅 loopback；PAT/JWT/DB 密钥由共享掩码锚点置空，仅
# backend 与 pipeline_executor 显式取回 PAT 真值；内网允许清单独立于
# MCP 清单；backend 用官方 /healthz 做容器健康检查；上传卷按官方契约挂
# /app/data/uploads。
prod_service_block() {
  local service="$1"
  awk -v service="$service" '
    $0 == "  " service ":" { in_service = 1 }
    in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
    in_service { print }
  ' "$PROD_COMPOSE"
}
prod_service_networks() {
  # A prod service's declared networks list from the source compose (YAML
  # list form), one network per line, so membership can be asserted exactly.
  # Comment lines inside the list are skipped.
  awk '
    /^    networks: *$/ { in_networks = 1; next }
    in_networks && /^[[:space:]]*#/ { next }
    in_networks && /^      - / { sub(/^      - /, ""); print; next }
    in_networks { exit }
  ' <<<"$1"
}
multica_block="$(prod_service_block multica)"
multica_pg_block="$(prod_service_block multica-postgres)"
multica_web_block="$(prod_service_block multica-web)"
local_multica_block="$(local_compose_block multica)"
local_multica_pg_block="$(local_compose_block multica-postgres)"
local_multica_web_block="$(local_compose_block multica-web)"
multica_executor_block="$(prod_service_block pipeline_executor)"
multica_task_worker_block="$(prod_service_block task_worker)"
local_backend_block="$(local_compose_block backend)"
local_executor_block="$(local_compose_block pipeline_executor)"
local_task_worker_block="$(local_compose_block task_worker)"
# The multica_internal definition must stay a plain (non-internal) bridge:
# the official backend needs outbound calls, and internal:true would break the
# official web/agent dependencies without adding real isolation.
multica_net_def="$(awk '
  /^  multica_internal: *$/ { in_net = 1; next }
  in_net && (/^  [A-Za-z0-9_-]+:/ || /^  #/) { exit }
  in_net { print }
' "$PROD_COMPOSE")"
if [ -z "$multica_block" ] || [ -z "$multica_pg_block" ] \
    || [ -z "$multica_web_block" ] || [ -z "$local_multica_block" ] \
    || [ -z "$local_multica_pg_block" ] || [ -z "$local_multica_web_block" ] \
    || [ -z "$local_backend_block" ] || [ -z "$local_executor_block" ] \
    || [ -z "$local_task_worker_block" ] \
    || ! grep -Fq \
      'image: ${MULTICA_BACKEND_IMAGE:-ghcr.io/multica-ai/multica-backend@sha256:099fd0d1eea8fc4cf03254c6dff86f7de206662ff71db8dc9a78e66726dd65a4}' \
      <<<"$multica_block" \
    || ! grep -Fq \
      'image: ${MULTICA_BACKEND_IMAGE:-ghcr.io/multica-ai/multica-backend@sha256:099fd0d1eea8fc4cf03254c6dff86f7de206662ff71db8dc9a78e66726dd65a4}' \
      <<<"$local_multica_block" \
    || ! grep -Fq \
      'image: ${MULTICA_WEB_IMAGE:-ghcr.io/multica-ai/multica-web@sha256:ba8fd0fda94a9ec141e15d77e591db8ee4b246720a722d6f9dffd185ee48863b}' \
      <<<"$multica_web_block" \
    || ! grep -Fq \
      'image: ${MULTICA_WEB_IMAGE:-ghcr.io/multica-ai/multica-web@sha256:ba8fd0fda94a9ec141e15d77e591db8ee4b246720a722d6f9dffd185ee48863b}' \
      <<<"$local_multica_web_block" \
    || ! grep -Fq \
      'image: ${MULTICA_POSTGRES_IMAGE:-pgvector/pgvector:pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f}' \
      <<<"$multica_pg_block" \
    || ! grep -Fq \
      'image: ${MULTICA_POSTGRES_IMAGE:-pgvector/pgvector:pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f}' \
      <<<"$local_multica_pg_block" \
    || ! grep -Fq 'profiles: [multica]' <<<"$multica_block" \
    || ! grep -Fq 'profiles: [multica]' <<<"$multica_pg_block" \
    || ! grep -Fq 'profiles: [multica]' <<<"$multica_web_block" \
    || ! grep -Fq 'profiles: ["multica"]' <<<"$local_multica_block" \
    || ! grep -Fq 'profiles: ["multica"]' <<<"$local_multica_pg_block" \
    || ! grep -Fq 'profiles: ["multica"]' <<<"$local_multica_web_block" \
    || grep -Eq 'MULTICA_[A-Z_]+:\?' "$PROD_COMPOSE" \
    || grep -Eq 'MULTICA_[A-Z_]+:\?' "$LOCAL_COMPOSE" \
    || grep -Eq 'multica-(backend|web):latest' "$PROD_COMPOSE" \
    || grep -Eq 'multica-(backend|web):latest' "$LOCAL_COMPOSE" \
    || grep -Fq 'MULTICA_APP_IMAGE' "$PROD_COMPOSE" \
    || grep -Fq 'MULTICA_APP_IMAGE' "$LOCAL_COMPOSE" \
    || grep -Fq 'MULTICA_APP_IMAGE' "$DEPLOY_SCRIPT" \
    || grep -Fq 'MULTICA_APP_IMAGE' "$DEPLOY_WORKFLOW" \
    || grep -Eq '^[[:space:]]+ports:' <<<"$multica_block" \
    || grep -Eq '^[[:space:]]+ports:' <<<"$multica_pg_block" \
    || ! grep -Fq 'expose:' <<<"$multica_block" \
    || ! grep -Fq '"8080"' <<<"$multica_block" \
    || ! grep -Fq \
      '${MULTICA_WEB_BIND_ADDR:-127.0.0.1}:${MULTICA_WEB_PORT:-18100}:3000' \
      <<<"$multica_web_block" \
    || ! grep -Fq 'REMOTE_API_URL: http://multica:8080' <<<"$multica_web_block" \
    || ! grep -Fq 'REMOTE_API_URL: http://multica:8080' \
      <<<"$local_multica_web_block" \
    || ! grep -Fq \
      '127.0.0.1:${MULTICA_API_LOOPBACK_PORT:-18101}:8080' \
      <<<"$local_multica_block" \
    || ! grep -Fq \
      '127.0.0.1:${MULTICA_WEB_LOOPBACK_PORT:-18100}:3000' \
      <<<"$local_multica_web_block" \
    || ! grep -Fq 'multica_uploads:/app/data/uploads' <<<"$multica_block" \
    || ! grep -Fq 'multica_uploads:/app/data/uploads' \
      <<<"$local_multica_block" \
    || ! grep -Fq 'multica_pgdata:/var/lib/postgresql/data' \
      <<<"$multica_pg_block" \
    || ! grep -Fq 'multica_pgdata:/var/lib/postgresql/data' \
      <<<"$local_multica_pg_block" \
    || ! grep -Fq 'http://127.0.0.1:8080/healthz' <<<"$multica_block" \
    || ! grep -Fq 'http://127.0.0.1:8080/healthz' <<<"$local_multica_block" \
    || ! grep -Fq 'networks:' <<<"$multica_pg_block" \
    || ! grep -Fq 'multica_internal' <<<"$multica_pg_block" \
    || grep -Fq 'default' <<<"$multica_pg_block" \
    || [ "$(prod_service_networks "$multica_block")" != 'multica_internal' ] \
    || [ "$(prod_service_networks "$multica_web_block")" != 'multica_internal' ] \
    || [ "$(prod_service_networks "$multica_pg_block")" != 'multica_internal' ] \
    || ! prod_service_networks "$backend_block" | grep -Fxq 'multica_internal' \
    || ! prod_service_networks "$multica_executor_block" | grep -Fxq 'multica_internal' \
    || ! grep -Fq '  multica_internal:' "$PROD_COMPOSE" \
    || [ "$multica_net_def" != '    driver: bridge' ] \
    || grep -Fq '  multica_data:' "$PROD_COMPOSE" \
    || grep -Fq '  multica_data:' "$LOCAL_COMPOSE" \
    || ! grep -Fq '  multica_pgdata:' "$PROD_COMPOSE" \
    || ! grep -Fq '  multica_uploads:' "$PROD_COMPOSE" \
    || ! grep -Fq '  multica_pgdata:' "$LOCAL_COMPOSE" \
    || ! grep -Fq '  multica_uploads:' "$LOCAL_COMPOSE" \
    || ! grep -Fq 'MULTICA_DEFAULT_PAT: ""' "$PROD_COMPOSE" \
    || ! grep -Fq 'MULTICA_DEFAULT_PAT: ""' "$LOCAL_COMPOSE" \
    || ! grep -Fq 'MULTICA_JWT_SECRET: ""' "$PROD_COMPOSE" \
    || ! grep -Fq 'MULTICA_JWT_SECRET: ""' "$LOCAL_COMPOSE" \
    || ! grep -Fq 'MULTICA_POSTGRES_PASSWORD: ""' "$PROD_COMPOSE" \
    || ! grep -Fq 'MULTICA_POSTGRES_PASSWORD: ""' "$LOCAL_COMPOSE" \
    || ! grep -Fq 'MULTICA_DEFAULT_PAT: ${MULTICA_DEFAULT_PAT:-}' \
      <<<"$backend_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_PAT: ${MULTICA_DEFAULT_PAT:-}' \
      <<<"$multica_executor_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_BASE_URL: ${MULTICA_DEFAULT_BASE_URL:-}' \
      <<<"$backend_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_WEB_URL: ${MULTICA_DEFAULT_WEB_URL:-}' \
      <<<"$backend_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_WORKSPACE_ID: ${MULTICA_DEFAULT_WORKSPACE_ID:-}' \
      <<<"$backend_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_WORKSPACE_NAME: ${MULTICA_DEFAULT_WORKSPACE_NAME:-}' \
      <<<"$backend_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_WEB_URL: ${MULTICA_DEFAULT_WEB_URL:-}' \
      <<<"$multica_executor_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_WORKSPACE_ID: ${MULTICA_DEFAULT_WORKSPACE_ID:-}' \
      <<<"$multica_executor_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_WORKSPACE_NAME: ${MULTICA_DEFAULT_WORKSPACE_NAME:-}' \
      <<<"$multica_executor_block" \
    || ! grep -Fq \
      'SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS: ${SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS:-}' \
      <<<"$backend_block" \
    || ! grep -Fq \
      'SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS: ${SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS:-}' \
      <<<"$multica_executor_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_BASE_URL: ${MULTICA_DEFAULT_BASE_URL:-}' \
      <<<"$local_backend_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_WEB_URL: ${MULTICA_DEFAULT_WEB_URL:-}' \
      <<<"$local_backend_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_WORKSPACE_ID: ${MULTICA_DEFAULT_WORKSPACE_ID:-}' \
      <<<"$local_backend_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_WORKSPACE_NAME: ${MULTICA_DEFAULT_WORKSPACE_NAME:-}' \
      <<<"$local_backend_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_PAT: ${MULTICA_DEFAULT_PAT:-}' \
      <<<"$local_backend_block" \
    || ! grep -Fq \
      'SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS: ${SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS:-}' \
      <<<"$local_backend_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_BASE_URL: ${MULTICA_DEFAULT_BASE_URL:-}' \
      <<<"$local_executor_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_WEB_URL: ${MULTICA_DEFAULT_WEB_URL:-}' \
      <<<"$local_executor_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_WORKSPACE_ID: ${MULTICA_DEFAULT_WORKSPACE_ID:-}' \
      <<<"$local_executor_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_WORKSPACE_NAME: ${MULTICA_DEFAULT_WORKSPACE_NAME:-}' \
      <<<"$local_executor_block" \
    || ! grep -Fq 'MULTICA_DEFAULT_PAT: ${MULTICA_DEFAULT_PAT:-}' \
      <<<"$local_executor_block" \
    || ! grep -Fq \
      'SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS: ${SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS:-}' \
      <<<"$local_executor_block" \
    || grep -Fq 'MULTICA_DEFAULT_PAT:' <<<"$multica_task_worker_block" \
    || grep -Fq 'MULTICA_DEFAULT_PAT:' <<<"$local_task_worker_block" \
    || grep -Fq 'MULTICA_JWT_SECRET: ${' <<<"$multica_task_worker_block" \
    || ! grep -Fq 'MULTICA_ENABLED=true' "$REPO_ROOT/.env.example" \
    || ! grep -Fq 'COMPOSE_PROFILES=multica' "$REPO_ROOT/.env.example" \
    || ! grep -Fq 'MULTICA_WEB_BIND_ADDR=127.0.0.1' "$REPO_ROOT/.env.example" \
    || ! grep -Fq 'MULTICA_DEFAULT_PAT=' "$REPO_ROOT/.env.example" \
    || ! grep -Fq \
      'MULTICA_BACKEND_IMAGE=ghcr.io/multica-ai/multica-backend@sha256:099fd0d1eea8fc4cf03254c6dff86f7de206662ff71db8dc9a78e66726dd65a4' \
      "$REPO_ROOT/.env.example" \
    || ! grep -Fq \
      'MULTICA_WEB_IMAGE=ghcr.io/multica-ai/multica-web@sha256:ba8fd0fda94a9ec141e15d77e591db8ee4b246720a722d6f9dffd185ee48863b' \
      "$REPO_ROOT/.env.example" \
    || ! grep -Fq 'start_multica_default_stack() {' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'stop_disabled_multica_stack' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'configure_multica_env' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'validate_multica_config' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'multica_bind_addr_ok' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'check_image_digest MULTICA_BACKEND_IMAGE' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'check_image_digest MULTICA_WEB_IMAGE' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'check_image_digest MULTICA_POSTGRES_IMAGE' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'MULTICA_BACKEND_IMAGE_PIN=' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'MULTICA_WEB_IMAGE_PIN=' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'MULTICA_JWT_SECRET' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'MULTICA_DEFAULT_PAT' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'COMPOSE_PROFILES= \' "$DEPLOY_SCRIPT" \
    || grep -Fq ' -u COMPOSE_PROFILES' "$DEPLOY_SCRIPT" \
    || grep -Fq 'profile_args+=(--profile multica)' "$DEPLOY_SCRIPT"; then
  printf 'Multica must stay a default-enabled official-stack profile with isolation guarantees: verified digest-pinned image defaults (no floating tags, no blocking required vars), all three multica containers solely on the multica_internal private network (the API never joins default; only the calling backend and pipeline_executor join that network), a plain non-internal bridge (the official stack needs outbound), no host ports on the API container, a loopback-default web bind, official /healthz healthchecks, /app/data/uploads volumes, loopback-only local exposure, backend and pipeline_executor both mapping all five MULTICA_DEFAULT_* values explicitly (the all-or-nothing completeness predicate is process-local: a consumer missing any value treats the deployment default as unconfigured and soft-deletes the materialized platform profile; env_file inheritance must not be the only delivery), PAT/JWT/DB secrets masked everywhere except the sanctioned PAT consumers, an MCP-independent allowlist, and a script-owned COMPOSE_PROFILES authority\n' >&2
  exit 1
fi
multica_optional_call_line="$(
  grep -nF 'start_multica_default_stack || log' "$DEPLOY_SCRIPT" \
    | head -n1 | cut -d: -f1
)"
multica_start_call_count="$(
  grep -cF 'start_multica_default_stack || log' "$DEPLOY_SCRIPT"
)"
multica_core_up_def_line="$(
  grep -nF 'if ! run_with_retry compose up -d --remove-orphans; then' \
    "$DEPLOY_SCRIPT" | head -n1 | cut -d: -f1
)"
multica_readiness_wait_line="$(
  grep -nF 'log "waiting for backend, pipeline executor, replica executor and frontend readiness' \
    "$DEPLOY_SCRIPT" | head -n1 | cut -d: -f1
)"
if [ "$multica_start_call_count" -ne 1 ] \
    || [ -z "$multica_optional_call_line" ] \
    || [ -z "$multica_core_up_def_line" ] \
    || [ -z "$multica_readiness_wait_line" ] \
    || [ "$multica_optional_call_line" -le "$multica_core_up_def_line" ] \
    || [ "$multica_optional_call_line" -ge "$multica_readiness_wait_line" ]; then
  printf 'the Multica default stack must start exactly once, after the final core compose up and before the readiness gate wait (a configured multica_default readiness check would otherwise deadlock on a stack that is never started)\n' >&2
  exit 1
fi
multica_manifest_secret_count="$(
  grep -cF \
    'PROD_MULTICA_DEFAULT_PAT: ${{ secrets.PROD_MULTICA_DEFAULT_PAT }}' \
    "$DEPLOY_WORKFLOW"
)"
if [ "$multica_manifest_secret_count" -lt 2 ]; then
  printf 'verification and deploy must both materialize the Multica manifest secret\n' >&2
  exit 1
fi
multica_manifest_config_count="$(
  grep -cF \
    'PROD_MULTICA_DEFAULT_BASE_URL: ${{ vars.PROD_MULTICA_DEFAULT_BASE_URL }}' \
    "$DEPLOY_WORKFLOW"
)"
multica_manifest_workspace_count="$(
  grep -cF \
    'PROD_MULTICA_DEFAULT_WORKSPACE_ID: ${{ vars.PROD_MULTICA_DEFAULT_WORKSPACE_ID }}' \
    "$DEPLOY_WORKFLOW"
)"
multica_manifest_workspace_name_count="$(
  grep -cF \
    'PROD_MULTICA_DEFAULT_WORKSPACE_NAME: ${{ vars.PROD_MULTICA_DEFAULT_WORKSPACE_NAME }}' \
    "$DEPLOY_WORKFLOW"
)"
if [ "$multica_manifest_config_count" -lt 2 ] \
    || [ "$multica_manifest_workspace_count" -lt 2 ] \
    || [ "$multica_manifest_workspace_name_count" -lt 2 ]; then
  printf 'verification and deploy must both forward the full five-value Multica manifest surface (base URL, workspace id and name are controlled manifest config, not just the PAT)\n' >&2
  exit 1
fi
multica_image_var_count="$(
  grep -cF \
    'PROD_MULTICA_BACKEND_IMAGE: ${{ vars.PROD_MULTICA_BACKEND_IMAGE }}' \
    "$DEPLOY_WORKFLOW"
)"
if [ "$multica_image_var_count" -lt 2 ] \
    || grep -Fq 'MULTICA_APP_IMAGE' "$DEPLOY_WORKFLOW"; then
  printf 'verification and deploy must both forward the optional Multica image override, and no synthetic Multica image variable may remain (the compose digest defaults satisfy interpolation)\n' >&2
  exit 1
fi

# Synthetic Compose render: prove both compose files interpolate and expose
# the Multica stack exactly as the guards describe (default-on locally via
# COMPOSE_PROFILES, isolated profile in prod, loopback web bind). Rendering
# needs a real `docker compose`; environments without Docker skip this with a
# loud note instead of failing, because every structural fact is already
# asserted statically above.
if command -v docker >/dev/null 2>&1 \
    && docker compose version >/dev/null 2>&1; then
  compose_render_tmp="$(mktemp -d)"
  compose_required_env() {
    # Synthetic values for every ${VAR:?} required interpolation so
    # `docker compose config` renders deterministically without borrowing a
    # developer's real .env (values are never pulled or started).
    grep -oE '\$\{[A-Z0-9_]+:\?' "$1" | sed -E 's/^\$\{//; s/:.*$//' \
      | sort -u | while IFS= read -r key; do
        printf '%s=synthetic-%s\n' "$key" "$key"
      done
  }
  compose_required_env "$PROD_COMPOSE" >"$compose_render_tmp/render.env"
  compose_required_env "$LOCAL_COMPOSE" >>"$compose_render_tmp/render.env"
  printf '%s\n' \
    'MULTICA_WEB_BIND_ADDR=127.0.0.1' \
    'MULTICA_WEB_PORT=18100' \
    'MULTICA_POSTGRES_PASSWORD=synthetic-render-password' \
    'MULTICA_JWT_SECRET=synthetic-render-jwt' \
    >>"$compose_render_tmp/render.env"
  printf '%s\n' 'COMPOSE_PROFILES=multica' >>"$compose_render_tmp/render.env"
  cp "$compose_render_tmp/render.env" "$compose_render_tmp/disabled.env"
  printf '%s\n' 'COMPOSE_PROFILES=' >>"$compose_render_tmp/disabled.env"
  prod_profiled_services="$(
    cd "$REPO_ROOT" && docker compose \
      --env-file "$compose_render_tmp/render.env" \
      -f docker-compose.prod.yml --profile multica config --services 2>/dev/null \
      | sort
  )"
  prod_core_services="$(
    cd "$REPO_ROOT" && docker compose \
      --env-file "$compose_render_tmp/disabled.env" \
      -f docker-compose.prod.yml config --services 2>/dev/null \
      | sort
  )"
  local_default_services="$(
    cd "$REPO_ROOT" && docker compose \
      --env-file "$compose_render_tmp/render.env" \
      -f docker-compose.local.yml config --services 2>/dev/null \
      | sort
  )"
  local_disabled_services="$(
    cd "$REPO_ROOT" && docker compose \
      --env-file "$compose_render_tmp/disabled.env" \
      -f docker-compose.local.yml config --services 2>/dev/null \
      | sort
  )"
  for service in multica multica-web multica-postgres; do
    if ! grep -Fxq "$service" <<<"$prod_profiled_services" \
        || ! grep -Fxq "$service" <<<"$local_default_services"; then
      printf 'synthetic Compose render: %s must render in prod (--profile multica) and in the default local stack (COMPOSE_PROFILES=multica)\n' "$service" >&2
      rm -rf "$compose_render_tmp"
      exit 1
    fi
    if grep -Fxq "$service" <<<"$prod_core_services" \
        || grep -Fxq "$service" <<<"$local_disabled_services"; then
      printf 'synthetic Compose render: %s must stay out of the prod core model and out of the local stack when COMPOSE_PROFILES is emptied\n' "$service" >&2
      rm -rf "$compose_render_tmp"
      exit 1
    fi
  done
  cd "$REPO_ROOT" && docker compose \
    --env-file "$compose_render_tmp/render.env" \
    -f docker-compose.prod.yml --profile multica config \
    >"$compose_render_tmp/prod-rendered.yml" 2>/dev/null
  multica_web_rendered_block="$(
    awk '
      $0 == "  multica-web:" { in_service = 1 }
      in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  multica-web:" { exit }
      in_service { print }
    ' "$compose_render_tmp/prod-rendered.yml"
  )"
  multica_api_rendered_block="$(
    awk '
      $0 == "  multica:" { in_service = 1 }
      in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  multica:" { exit }
      in_service { print }
    ' "$compose_render_tmp/prod-rendered.yml"
  )"
  if ! grep -Eq 'host_ip: "127\.0\.0\.1"|host_ip: 127\.0\.0\.1' \
      <<<"$multica_web_rendered_block" \
      || ! grep -Fq 'published: "18100"' <<<"$multica_web_rendered_block" \
      || grep -Fq 'host_ip: "0.0.0.0"' <<<"$multica_web_rendered_block" \
      || grep -Fq 'published:' <<<"$multica_api_rendered_block"; then
    printf 'synthetic Compose render: the Multica web UI must publish only on the loopback bind (default 127.0.0.1:18100) and the API container must not publish any host port\n' >&2
    rm -rf "$compose_render_tmp"
    exit 1
  fi
  # Rendered network topology: docker compose config keys each service's
  # networks by the DECLARED network name (no project prefix), so the actual
  # rendered model can be asserted exactly: the three multica containers stay
  # solely on multica_internal (never the default network), and exactly the
  # calling backend / pipeline_executor join the private network.
  rendered_service_networks() {
    local service="$1"
    awk -v service="$service" '
      $0 == "  " service ":" { in_service = 1 }
      in_service && NR > 1 && $0 ~ /^  [A-Za-z0-9_-]+:/ && $0 != "  " service ":" { exit }
      in_service && /^    networks:/ { in_networks = 1; next }
      in_networks && /^      [^[:space:]#]/ {
        line = $0
        sub(/^ +/, "", line)
        sub(/:.*$/, "", line)
        print line
        next
      }
      in_networks { exit }
    ' "$compose_render_tmp/prod-rendered.yml"
  }
  for service in multica multica-web multica-postgres; do
    if [ "$(rendered_service_networks "$service")" != 'multica_internal' ]; then
      printf 'synthetic Compose render: %s must be attached solely to multica_internal, never to the default network\n' "$service" >&2
      rm -rf "$compose_render_tmp"
      exit 1
    fi
  done
  for service in backend pipeline_executor; do
    if ! rendered_service_networks "$service" | grep -Fxq 'multica_internal'; then
      printf 'synthetic Compose render: %s must join multica_internal to reach the default Multica stack\n' "$service" >&2
      rm -rf "$compose_render_tmp"
      exit 1
    fi
  done
  rm -rf "$compose_render_tmp"
  printf 'ok compose-render-multica-defaults\n'
else
  printf 'skipped synthetic Compose render: docker compose is unavailable in this environment (structural facts remain statically guarded)\n'
fi

# The Docker build job only needs a public value to satisfy Compose interpolation.
# Keep it step-local: a job/top-level secret would leak into unrelated steps or
# imply that the build actually needs a production credential.
build_step="$(awk '
  /^      - name: Build Docker images$/ { in_step = 1 }
  in_step && /^  [A-Za-z0-9_-]+:/ && !/^      - name: Build Docker images$/ { exit }
  in_step { print }
' "$DEPLOY_WORKFLOW")"
if [ -z "$build_step" ] \
  || ! grep -Fq 'INTEGRATED_DBX_PASSWORD: ci-only-dbx-compose-interpolation' <<<"$build_step" \
  || [ "$(grep -c 'INTEGRATED_DBX_PASSWORD' "$DEPLOY_WORKFLOW")" -ne 1 ] \
  || ! grep -Fq 'docker compose -f docker-compose.prod.yml build' <<<"$build_step" \
  || ! grep -Fq 'chmod -R go-rwx backend/app backend/integrated_runtime' <<<"$build_step" \
  || ! grep -Fq 'docker compose -f docker-compose.prod.yml build integrated_replica_executor' <<<"$build_step" \
  || ! grep -Fq 'trap '\''chmod -R go+rX backend/app backend/integrated_runtime'\'' EXIT' <<<"$build_step" \
  || ! grep -Fq 'assert os.geteuid() == 10001' <<<"$build_step" \
  || ! grep -Fq 'from app.data_channel.integrated import cdc_executor, replica_executor' <<<"$build_step" \
  || grep -Eq 'secrets\.|build-arg|docker compose.* up' <<<"$build_step"; then
  printf 'Docker build must test restrictive archived source permissions and non-root executor imports, use one step-local public dbx value, and no secret/build-arg/start\n' >&2
  exit 1
fi

test_dir="$(mktemp -d /tmp/openontology-deploy-guards.XXXXXX)"
archive_source="$(mktemp -d /tmp/openontology-archive-source.XXXXXX)"
# Keep the XXXXXX suffix at the end: BSD mktemp otherwise treats a trailing
# extension as literal text and a second guard run can collide with the first.
archive_output="$(mktemp /tmp/openontology-archive.XXXXXX)"
trap 'rm -rf -- "$test_dir" "$archive_source"; rm -f -- "$archive_output"' EXIT

archive_fixture_paths=(
  ".env.example"
  "docker-compose.prod.yml"
  "docker-compose.cdc.yml"
  "deploy/production.dependencies.env"
  "backend/.dockerignore"
  "backend/Dockerfile"
  "backend/Dockerfile.cdc"
  "backend/integrated_runtime"
  "backend/alembic.ini"
  "backend/alembic/env.py"
  "backend/app/main.py"
  "backend/pyproject.toml"
  "backend/uv.lock"
  "backend/scripts/maintenance/reset_admin_password.py"
  "backend/scripts/download_voice_model.py"
  "frontend/.dockerignore"
  "frontend/Dockerfile.prod"
  "frontend/dist/index.html"
  "frontend/index.html"
  "frontend/nginx/default.conf"
  "frontend/package.json"
  "frontend/package-lock.json"
  "frontend/postcss.config.js"
  "frontend/public/favicon.svg"
  "frontend/src/main.tsx"
  "frontend/src/test/e2e/should-not-deploy.spec.ts"
  "frontend/tailwind.config.ts"
  "frontend/tsconfig.app.json"
  "frontend/tsconfig.json"
  "frontend/tsconfig.node.json"
  "frontend/vite.config.ts"
  "docker/browser/Dockerfile"
  "docker/weknora/gateway.conf"
  "docker/weknora/weknora-proxy.inc"
  "docker/kaneo/gateway.conf.template"
  "docker/kaneo/kaneo-proxy.inc"
  "deploy/deploy-prod.sh"
  "deploy/runtime_source_attestation.py"
  "deploy/runtime-source-manifest.json"
  "scripts/ci/validate-deploy-app-dir.sh"
  "docs/should-not-deploy.md"
  ".artifacts/should-not-deploy.json"
)
for archive_path in "${archive_fixture_paths[@]}"; do
  case "$archive_path" in
    deploy/runtime_source_attestation.py|deploy/runtime-source-manifest.json)
      continue
      ;;
  esac
  mkdir -p "$archive_source/$(dirname "$archive_path")"
  : > "$archive_source/$archive_path"
done
mkdir -p "$archive_source/deploy"
cp "$REPO_ROOT/deploy/runtime_source_attestation.py" \
  "$archive_source/deploy/runtime_source_attestation.py"
git -C "$archive_source" init -q
git -C "$archive_source" config user.email "deploy-guards@example.invalid"
git -C "$archive_source" config user.name "Deploy Guards"
git -C "$archive_source" config commit.gpgsign false
git -C "$archive_source" add backend/app
git -C "$archive_source" commit -qm "archive fixture"
archive_fixture_commit="$(git -C "$archive_source" rev-parse HEAD)"
python3 "$REPO_ROOT/deploy/runtime_source_attestation.py" generate \
  --repo "$archive_source" \
  --commit "$archive_fixture_commit" \
  --output "$archive_source/deploy/runtime-source-manifest.json" >/dev/null
DEPLOY_SOURCE_ROOT="$archive_source" \
  bash "$ARCHIVE_SCRIPT" "$archive_output" >/dev/null
archive_listing="$(tar -tzf "$archive_output")"
if archive_mode="$(stat -c '%a' "$archive_output" 2>/dev/null)"; then
  : # GNU coreutils
elif archive_mode="$(stat -f '%Lp' "$archive_output" 2>/dev/null)"; then
  : # BSD/macOS
else
  printf 'could not inspect deployment archive mode\n' >&2
  exit 1
fi
if [ "$archive_mode" != "600" ]; then
  printf 'deployment archive must be mode 600, got %s\n' "$archive_mode" >&2
  exit 1
fi
for required_member in \
  ".env.example" \
  "docker-compose.cdc.yml" \
  "backend/Dockerfile.cdc" \
  "backend/integrated_runtime" \
  "backend/app/main.py" \
  "backend/scripts/maintenance/reset_admin_password.py" \
  "backend/scripts/download_voice_model.py" \
  "frontend/dist/index.html" \
  "frontend/src/main.tsx" \
  "docker/browser/Dockerfile" \
  "docker/weknora/gateway.conf" \
  "docker/weknora/weknora-proxy.inc" \
  "docker/kaneo/gateway.conf.template" \
  "docker/kaneo/kaneo-proxy.inc" \
  "deploy/runtime_source_attestation.py" \
  "deploy/runtime-source-manifest.json" \
  "deploy/production.dependencies.env"; do
  if ! grep -Fxq "$required_member" <<<"$archive_listing"; then
    printf 'deployment archive is missing required member: %s\n' \
      "$required_member" >&2
    exit 1
  fi
done
if ! tar -xOf "$archive_output" deploy/runtime-source-manifest.json \
    | grep -Fq '"backend/app/main.py"'; then
  printf 'deployment archive runtime source manifest is not the generated exact-HEAD manifest\n' >&2
  exit 1
fi
for forbidden_member in \
  ".env" \
  "frontend/src/test/e2e/should-not-deploy.spec.ts" \
  "docs/should-not-deploy.md" \
  ".artifacts/should-not-deploy.json"; do
  if grep -Fxq "$forbidden_member" <<<"$archive_listing"; then
    printf 'deployment archive contains non-runtime member: %s\n' \
      "$forbidden_member" >&2
    exit 1
  fi
done
archive_sha_before="$(python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$archive_output")"
printf 'changed runtime manifest\n' >"$archive_source/deploy/runtime-source-manifest.json"
archive_output_changed="$test_dir/archive-changed.tar.gz"
DEPLOY_SOURCE_ROOT="$archive_source" \
  bash "$ARCHIVE_SCRIPT" "$archive_output_changed" >/dev/null
archive_sha_after="$(python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$archive_output_changed")"
if [ "$archive_sha_before" = "$archive_sha_after" ]; then
  printf 'deployment archive SHA must change when the runtime source manifest changes\n' >&2
  exit 1
fi
if ! tar -xOf "$archive_output_changed" deploy/runtime-source-manifest.json \
    | grep -Fxq 'changed runtime manifest'; then
  printf 'deployment archive extraction must preserve the runtime source manifest\n' >&2
  exit 1
fi
cp "$archive_source/deploy/runtime-source-manifest.json" \
  "$test_dir/runtime-source-manifest.saved"
: >"$archive_source/deploy/runtime-source-manifest.json"
archive_empty_manifest_output="$test_dir/archive-empty-manifest.tar.gz"
if DEPLOY_SOURCE_ROOT="$archive_source" \
    bash "$ARCHIVE_SCRIPT" "$archive_empty_manifest_output" \
    >"$test_dir/archive-empty-manifest.log" 2>&1; then
  printf 'deployment archive builder must reject an empty runtime source manifest\n' >&2
  exit 1
fi
grep -Fq 'must be a non-empty regular file' \
  "$test_dir/archive-empty-manifest.log"
mv "$test_dir/runtime-source-manifest.saved" \
  "$archive_source/deploy/runtime-source-manifest.json"
printf 'ok deployment-archive-runtime-manifest-bind\n'

cp "$REPO_ROOT/.env.example" "$test_dir/.env"

set_test_env_value() {
  local key="$1"
  local value="$2"
  local output="$test_dir/.env.next"
  awk -v key="$key" -v value="$value" '
    BEGIN { found=0 }
    $0 ~ "^[[:space:]]*" key "=" {
      print key "=" value
      found=1
      next
    }
    { print }
    END { if (!found) print key "=" value }
  ' "$test_dir/.env" > "$output"
  mv "$output" "$test_dir/.env"
}

digest="example.invalid/openontology@sha256:$(printf 'a%.0s' {1..64})"
for image_key in \
  POSTGRES_IMAGE REDIS_IMAGE NEO4J_IMAGE MINIO_IMAGE BROWSER_IMAGE \
  PYTHON_BASE_IMAGE NODE_BASE_IMAGE NGINX_BASE_IMAGE \
  WEKNORA_APP_IMAGE WEKNORA_UI_IMAGE WEKNORA_DOCREADER_IMAGE \
  WEKNORA_POSTGRES_IMAGE WEKNORA_REDIS_IMAGE \
  KANEO_APP_IMAGE KANEO_POSTGRES_IMAGE ; do
  set_test_env_value "$image_key" "$digest"
done
set_test_env_value SECRET_KEY 0123456789abcdef0123456789abcdef
set_test_env_value FIRST_ADMIN_PASSWORD synthetic-admin-password
set_test_env_value STRICT_IMAGE_DIGESTS true

if (
  cd "$test_dir"
  APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=missing-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/missing-manifest.log" 2>&1; then
  printf 'expected a missing production dependency manifest to be rejected\n' >&2
  exit 1
fi
grep -q 'is required for production deployment' \
  "$test_dir/missing-manifest.log"

env \
  PROD_PUBLIC_PORT=8080 \
  PROD_POSTGRES_DB=ontology \
  PROD_POSTGRES_USER=ontology \
  PROD_POSTGRES_PASSWORD=synthetic-postgres-password \
  PROD_DATABASE_URL=postgresql://ontology:synthetic-postgres-password@pg.example.com:5432/ontology \
  PROD_REDIS_URL=redis://:synthetic-redis-password@redis.example.com:6379/0 \
  PROD_NEO4J_URI=bolt+s://graph.example.com:7687 \
  PROD_NEO4J_USER=neo4j \
  PROD_NEO4J_PASSWORD=synthetic-neo4j-password \
  PROD_NEO4J_AUTH=neo4j/synthetic-neo4j-password \
  PROD_MINIO_ENDPOINT=objects.example.com:9000 \
  PROD_MINIO_ACCESS_KEY=synthetic-minio-access \
  PROD_MINIO_SECRET_KEY=synthetic-minio-secret \
  PROD_MINIO_USE_SSL=true \
  PROD_N8N_API_URL=https://n8n.example.com \
  PROD_N8N_API_KEY=synthetic-n8n-api-key \
  PROD_N8N_TIMEOUT_SECONDS=30 \
  bash "$REPO_ROOT/scripts/ci/materialize-production-dependencies.sh" \
    "$test_dir/test-production-dependencies.env" >/dev/null

grep -Fxq 'N8N_TIMEOUT_SECONDS=30' \
  "$test_dir/test-production-dependencies.env"

(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
  DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >/dev/null
)
grep -Fxq 'KANEO_BASE_URL=http://kaneo-app:5173' "$test_dir/.env"
grep -Fxq 'KANEO_WEB_URL=https://64.83.14.139:8445' "$test_dir/.env"

external_redis_url='redis://:synthetic-redis-password@redis.example.com:6379/0'
grep -Fxq "REDIS_URL=${external_redis_url}" "$test_dir/.env"
if grep -Fxq 'REDIS_PASSWORD=synthetic-redis-password' "$test_dir/.env"; then
  printf 'external Redis credentials must not configure bundled Redis\n' >&2
  exit 1
fi

set_manifest_value() {
  local key="$1"
  local value="$2"
  local manifest="$test_dir/test-production-dependencies.env"
  local output="$test_dir/test-production-dependencies.next"
  awk -v key="$key" -v value="$value" '
    BEGIN { found=0 }
    $0 ~ "^[[:space:]]*" key "=" {
      print key "=" value
      found=1
      next
    }
    { print }
    END { if (!found) print key "=" value }
  ' "$manifest" > "$output"
  mv "$output" "$manifest"
}

canonical_database_url='postgresql://ontology:synthetic-postgres-password@pg.example.com:5432/ontology'
for legacy_database_url in \
  'postgres://ontology:synthetic-postgres-password@pg.example.com:5432/ontology' \
  'postgresql+psycopg2://ontology:synthetic-postgres-password@pg.example.com:5432/ontology'; do
  set_manifest_value DATABASE_URL "$legacy_database_url"
  (
    cd "$test_dir"
    env -u STRICT_IMAGE_DIGESTS \
      APP_DIR="$test_dir" \
      SKIP_GIT=1 \
      DEPLOY_VALIDATE_ONLY=1 \
      DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
      bash "$DEPLOY_SCRIPT" >database-url-normalization.log
  )
  grep -Fxq "DATABASE_URL=${canonical_database_url}" "$test_dir/.env"
  if grep -Fq 'synthetic-postgres-password' \
      "$test_dir/database-url-normalization.log"; then
    printf 'normalized PostgreSQL credential leaked into deploy validation logs\n' >&2
    exit 1
  fi
done
set_manifest_value DATABASE_URL "$canonical_database_url"

set_manifest_value REDIS_URL redis://redis:6379/0
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >legacy-redis.log
)
legacy_redis_password="$(
  awk -F= '$1 == "REDIS_PASSWORD" {
    print substr($0, index($0, "=") + 1)
  }' "$test_dir/.env" | tail -n1
)"
if [ -z "$legacy_redis_password" ] \
    || [ "$legacy_redis_password" = 'ontopromptredis123' ]; then
  printf 'legacy bundled Redis URL was not upgraded securely\n' >&2
  exit 1
fi
grep -Fxq \
  "REDIS_URL=redis://:${legacy_redis_password}@redis:6379/0" \
  "$test_dir/.env"
if grep -Fq "$legacy_redis_password" "$test_dir/legacy-redis.log"; then
  printf 'generated Redis password leaked into deploy validation logs\n' >&2
  exit 1
fi

bundled_redis_password='synthetic-bundled-redis-password'
set_manifest_value REDIS_URL \
  "redis://:${bundled_redis_password}@redis:6379/0"
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >bundled-redis.log
)
grep -Fxq "REDIS_PASSWORD=${bundled_redis_password}" "$test_dir/.env"
if grep -Fq "$bundled_redis_password" "$test_dir/bundled-redis.log"; then
  printf 'bundled Redis password leaked into deploy validation logs\n' >&2
  exit 1
fi

set_manifest_value REDIS_URL redis://:encoded%40password@redis:6379/0
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/encoded-redis.log" 2>&1; then
  printf 'encoded bundled Redis password must be rejected\n' >&2
  exit 1
fi
grep -q 'bundled Redis URL must use an unescaped' \
  "$test_dir/encoded-redis.log"
if grep -Fq 'encoded%40password' "$test_dir/encoded-redis.log"; then
  printf 'rejected Redis credential leaked into validation logs\n' >&2
  exit 1
fi
set_manifest_value REDIS_URL "$external_redis_url"

set_test_env_value POSTGRES_IMAGE postgres:16-alpine
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/strict-failure.log" 2>&1; then
  printf 'expected .env STRICT_IMAGE_DIGESTS=true to reject a floating image\n' >&2
  exit 1
fi
grep -q 'POSTGRES_IMAGE must be pinned' "$test_dir/strict-failure.log"

if (
  cd "$test_dir"
  STRICT_IMAGE_DIGESTS=0 \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >/dev/null
); then
  printf 'exported STRICT_IMAGE_DIGESTS must not override the validated .env\n' >&2
  exit 1
fi

set_test_env_value STRICT_IMAGE_DIGESTS false
if (
  cd "$test_dir"
  STRICT_IMAGE_DIGESTS=1 \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >strict-disabled.log 2>&1
); then
  printf 'production deployment must reject STRICT_IMAGE_DIGESTS=false even when the shell exports true\n' >&2
  exit 1
fi
grep -q 'production deployment requires STRICT_IMAGE_DIGESTS=true' "$test_dir/strict-disabled.log"
set_test_env_value STRICT_IMAGE_DIGESTS true
for image_key in \
  POSTGRES_IMAGE REDIS_IMAGE NEO4J_IMAGE MINIO_IMAGE BROWSER_IMAGE \
  PYTHON_BASE_IMAGE NODE_BASE_IMAGE NGINX_BASE_IMAGE \
  WEKNORA_APP_IMAGE WEKNORA_UI_IMAGE WEKNORA_DOCREADER_IMAGE \
  WEKNORA_POSTGRES_IMAGE WEKNORA_REDIS_IMAGE \
  KANEO_APP_IMAGE KANEO_POSTGRES_IMAGE ; do
  set_test_env_value "$image_key" "$digest"
done

set_test_env_value SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE direct_dev
if (
  cd "$test_dir"
  APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >runner-mode-failure.log 2>&1
); then
  printf 'production deployment must reject direct_dev plugin runner mode\n' >&2
  exit 1
fi
grep -q 'rejects SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE=direct_dev' \
  "$test_dir/runner-mode-failure.log"
cat >>"$test_dir/.env" <<'EOF'
super_assistant_process_plugin_runner_mode=disabled
EOF
if (
  cd "$test_dir"
  APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >runner-mode-ambiguous.log 2>&1
); then
  printf 'production deployment must reject case-variant plugin runner keys\n' >&2
  exit 1
fi
grep -q 'is duplicated or has a case-variant key' "$test_dir/runner-mode-ambiguous.log"
sed -i.bak '/^super_assistant_process_plugin_runner_mode=/d' "$test_dir/.env"
find "$test_dir" -maxdepth 1 -name '.env.bak' -delete
set_test_env_value SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE disabled

set_test_env_value BROWSER_IMAGE chromedp/headless-shell:stable
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/browser-image-failure.log" 2>&1; then
  printf 'production deployment must reject a floating browser image even when the global image gate is disabled\n' >&2
  exit 1
fi
grep -q 'BROWSER_IMAGE must be pinned' "$test_dir/browser-image-failure.log"
set_test_env_value BROWSER_IMAGE "$digest"

# Kaneo manifest variables must round-trip through the materializer; a
# plain-HTTP public origin must then be rejected by deployment validation.
env \
  PROD_PUBLIC_PORT=8080 \
  PROD_POSTGRES_DB=ontology \
  PROD_POSTGRES_USER=ontology \
  PROD_POSTGRES_PASSWORD=synthetic-postgres-password \
  PROD_DATABASE_URL=postgresql://ontology:synthetic-postgres-password@pg.example.com:5432/ontology \
  PROD_REDIS_URL=redis://:synthetic-redis-password@redis.example.com:6379/0 \
  PROD_NEO4J_URI=bolt+s://graph.example.com:7687 \
  PROD_NEO4J_USER=neo4j \
  PROD_NEO4J_PASSWORD=synthetic-neo4j-password \
  PROD_NEO4J_AUTH=neo4j/synthetic-neo4j-password \
  PROD_MINIO_ENDPOINT=objects.example.com:9000 \
  PROD_MINIO_ACCESS_KEY=synthetic-minio-access \
  PROD_MINIO_SECRET_KEY=synthetic-minio-secret \
  PROD_MINIO_USE_SSL=true \
  PROD_N8N_API_URL=https://n8n.example.com \
  PROD_N8N_API_KEY=synthetic-n8n-api-key \
  PROD_N8N_TIMEOUT_SECONDS=30 \
  PROD_KANEO_WEB_URL=http://kaneo.example.invalid:8445 \
  PROD_KANEO_ADMIN_EMAIL=admin@kaneo.example.invalid \
  bash "$REPO_ROOT/scripts/ci/materialize-production-dependencies.sh" \
    "$test_dir/test-kaneo-dependencies.env" >/dev/null
grep -Fxq 'KANEO_WEB_URL=http://kaneo.example.invalid:8445' \
  "$test_dir/test-kaneo-dependencies.env"
grep -Fxq 'KANEO_ADMIN_EMAIL=admin@kaneo.example.invalid' \
  "$test_dir/test-kaneo-dependencies.env"
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-kaneo-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/kaneo-http-origin.log" 2>&1; then
  printf 'a plain-HTTP KANEO_WEB_URL must be rejected\n' >&2
  exit 1
fi
grep -q 'must be an absolute HTTPS origin' "$test_dir/kaneo-http-origin.log"
set_test_env_value KANEO_WEB_URL ""
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >/dev/null
)
grep -Fxq 'KANEO_WEB_URL=https://64.83.14.139:8445' "$test_dir/.env"

set_test_env_value KANEO_HTTP_PORT 8444
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/kaneo-port-conflict.log" 2>&1; then
  printf 'Kaneo ports colliding with WeKnora must be rejected\n' >&2
  exit 1
fi
grep -q 'Kaneo public ports must be distinct' "$test_dir/kaneo-port-conflict.log"
set_test_env_value KANEO_HTTP_PORT 8090

# A trailing slash on the public origin is normalized away before validation so
# KANEO_API_URL and the gateway redirect never produce "//" targets.
set_test_env_value KANEO_WEB_URL "https://64.83.14.139:8445/"
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >/dev/null
)
grep -Fxq 'KANEO_WEB_URL=https://64.83.14.139:8445' "$test_dir/.env"

set_test_env_value KANEO_WEB_URL "https://64.83.14.139:9999"
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/kaneo-port-mismatch.log" 2>&1; then
  printf 'a KANEO_WEB_URL port different from KANEO_HTTPS_PORT must be rejected\n' >&2
  exit 1
fi
grep -q 'must equal KANEO_HTTPS_PORT' "$test_dir/kaneo-port-mismatch.log"

set_test_env_value KANEO_WEB_URL "https://kaneo.example.invalid:8445"
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/kaneo-host-mismatch.log" 2>&1; then
  printf 'a KANEO_WEB_URL host different from PUBLIC_TLS_IP must be rejected\n' >&2
  exit 1
fi
grep -q 'must equal PUBLIC_TLS_IP' "$test_dir/kaneo-host-mismatch.log"
set_test_env_value KANEO_WEB_URL ""
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >/dev/null
)
grep -Fxq 'KANEO_WEB_URL=https://64.83.14.139:8445' "$test_dir/.env"

# PUBLIC_TLS_IP initialization: an existing operator-configured value always
# wins; only a missing or empty value is derived exactly once from the literal
# public IP host of the deployment health URL and persisted. Loopback, private
# and hostname health URLs are rejected, and the generated Kaneo secrets must
# stay byte-identical across these repeated validation runs.
kaneo_secrets_snapshot() {
  for key in KANEO_ADMIN_PASSWORD KANEO_AUTH_SECRET KANEO_DB_PASSWORD; do
    awk -F= -v key="$key" '$1 == key {print substr($0, index($0, "=") + 1)}' \
      "$test_dir/.env" | tail -n1
  done
}
kaneo_secrets_before_public_tls="$(kaneo_secrets_snapshot)"

set_test_env_value KANEO_WEB_URL ""
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    HEALTH_URL="http://93.184.216.34:8080/" \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >public-tls-explicit-preserve.log 2>&1
)
# The template-derived 64.83.14.139 stays authoritative; the health URL host
# must never silently replace an existing configured address.
grep -Fxq 'PUBLIC_TLS_IP=64.83.14.139' "$test_dir/.env"
grep -Fxq 'KANEO_WEB_URL=https://64.83.14.139:8445' "$test_dir/.env"
if grep -q 'initialized PUBLIC_TLS_IP' "$test_dir/public-tls-explicit-preserve.log"; then
  printf 'an existing PUBLIC_TLS_IP must not be re-derived from HEALTH_URL\n' >&2
  exit 1
fi

set_test_env_value PUBLIC_TLS_IP ""
set_test_env_value KANEO_WEB_URL ""
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    HEALTH_URL="http://93.184.216.34:8080/" \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >public-tls-derived-empty.log 2>&1
)
grep -Fxq 'PUBLIC_TLS_IP=93.184.216.34' "$test_dir/.env"
grep -Fxq 'KANEO_WEB_URL=https://93.184.216.34:8445' "$test_dir/.env"
grep -q 'initialized PUBLIC_TLS_IP from the deployment health URL host' \
  "$test_dir/public-tls-derived-empty.log"

sed -i.bak '/^PUBLIC_TLS_IP=/d' "$test_dir/.env"
find "$test_dir" -maxdepth 1 -name '.env.bak' -delete
set_test_env_value KANEO_WEB_URL ""
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    HEALTH_URL="http://[2606:4700:4700::1111]:8080/" \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >public-tls-derived-v6.log 2>&1
)
grep -Fxq 'PUBLIC_TLS_IP=2606:4700:4700::1111' "$test_dir/.env"
grep -Fxq 'KANEO_WEB_URL=https://[2606:4700:4700::1111]:8445' "$test_dir/.env"

sed -i.bak '/^PUBLIC_TLS_IP=/d' "$test_dir/.env"
find "$test_dir" -maxdepth 1 -name '.env.bak' -delete
set_test_env_value KANEO_WEB_URL ""
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    HEALTH_URL="http://127.0.0.1:8080/" \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/public-tls-loopback.log" 2>&1; then
  printf 'a loopback health URL must not initialize PUBLIC_TLS_IP\n' >&2
  exit 1
fi
grep -q 'must use a literal public IP host' "$test_dir/public-tls-loopback.log"

set_test_env_value KANEO_WEB_URL ""
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    HEALTH_URL="http://kaneo.example.invalid/" \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/public-tls-hostname.log" 2>&1; then
  printf 'a hostname health URL must not initialize PUBLIC_TLS_IP\n' >&2
  exit 1
fi
grep -q 'must use a literal public IP host' "$test_dir/public-tls-hostname.log"

set_test_env_value KANEO_WEB_URL ""
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/public-tls-no-health-url.log" 2>&1; then
  printf 'a missing PUBLIC_TLS_IP with no health URL must be rejected\n' >&2
  exit 1
fi
grep -q 'no deployment health URL is available' \
  "$test_dir/public-tls-no-health-url.log"

# An exported shell PUBLIC_TLS_IP must not bypass or poison the persisted
# authority: the derivation still uses only the health URL host and .env.
set_test_env_value KANEO_WEB_URL ""
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    PUBLIC_TLS_IP=203.0.113.9 \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    HEALTH_URL="http://93.184.216.34:8080/" \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >/dev/null
)
grep -Fxq 'PUBLIC_TLS_IP=93.184.216.34' "$test_dir/.env"
if grep -Fxq 'PUBLIC_TLS_IP=203.0.113.9' "$test_dir/.env"; then
  printf 'an exported PUBLIC_TLS_IP must not override the persisted .env authority\n' >&2
  exit 1
fi

if [ "$(kaneo_secrets_snapshot)" != "$kaneo_secrets_before_public_tls" ]; then
  printf 'repeated validation runs must not rotate the generated Kaneo secrets\n' >&2
  exit 1
fi

# Restore the state the fake-deployment sections expect: the template IP and a
# freshly re-derived Kaneo origin.
set_test_env_value PUBLIC_TLS_IP 64.83.14.139
set_test_env_value KANEO_WEB_URL ""
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >/dev/null
)
grep -Fxq 'KANEO_WEB_URL=https://64.83.14.139:8445' "$test_dir/.env"

set_test_env_value KANEO_BASE_URL http://elsewhere.example.invalid:5173
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >/dev/null
)
# configure_kaneo_env re-pins the internal base URL the same way WeKnora does;
# the validator then enforces the contract on every subsequent deployment.
grep -Fxq 'KANEO_BASE_URL=http://kaneo-app:5173' "$test_dir/.env"

grafana_env_value() {
  awk -F= -v key="$1" '
    $0 ~ "^[[:space:]]*" key "=" {
      print substr($0, index($0, "=") + 1)
    }
  ' "$test_dir/.env" | tail -n1
}

# Grafana env materialization: a fresh/legacy .env without any Grafana key
# gets the full contract materialized (the shared administrator is the
# dedicated contract account, not the platform FIRST_ADMIN_USER), already
# persisted values and the generated password survive repeated runs, and
# invalid values are rejected by validation.
for grafana_key in \
  GRAFANA_IMAGE GRAFANA_BASE_URL GRAFANA_TIMEOUT_SECONDS GRAFANA_ADMIN_USER \
  GRAFANA_ADMIN_PASSWORD GRAFANA_FOLDER_UID GRAFANA_FOLDER_TITLE \
  GRAFANA_SESSION_TTL_SECONDS; do
  # .bak suffix keeps this portable across GNU and BSD/macOS sed (the other
  # call sites in this script already use -i.bak).
  sed -i.bak "/^[[:space:]]*${grafana_key}=/d" "$test_dir/.env"
  rm -f "$test_dir/.env.bak"
done
set_test_env_value FIRST_ADMIN_USER platform-admin-name
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >/dev/null
)
grep -Fxq 'GRAFANA_BASE_URL=http://grafana:3000' "$test_dir/.env"
grep -Fxq 'GRAFANA_TIMEOUT_SECONDS=15' "$test_dir/.env"
grep -Fxq 'GRAFANA_ADMIN_USER=admin' "$test_dir/.env"
grep -Fxq 'GRAFANA_SESSION_TTL_SECONDS=28800' "$test_dir/.env"
grep -Fxq 'GRAFANA_FOLDER_UID=openontology' "$test_dir/.env"
grep -Fxq 'GRAFANA_FOLDER_TITLE=OpenOntology 监控看板' "$test_dir/.env"
grep -Fxq \
  'GRAFANA_IMAGE=grafana/grafana:13.3.1@sha256:0615ec4fbd4072004b7d86b46ded63013a74b7d6eb1119273dd89e8fa25c13cf' \
  "$test_dir/.env"
case "$(grafana_env_value GRAFANA_ADMIN_PASSWORD)" in
  ""|change-me|admin123|localGrafanaAdmin2026!)
    printf 'configure_grafana_env must generate a real shared administrator password for a fresh .env\n' >&2
    exit 1
    ;;
esac
if [ "$(grafana_env_value GRAFANA_ADMIN_USER)" != "admin" ]; then
  printf 'GRAFANA_ADMIN_USER must stay the dedicated contract account, not inherit FIRST_ADMIN_USER\n' >&2
  exit 1
fi
grafana_password_snapshot="$(grafana_env_value GRAFANA_ADMIN_PASSWORD)"

# Already persisted legal values (including a custom timeout, TTL and folder)
# survive a repeated deployment; the generated password never rotates.
set_test_env_value GRAFANA_TIMEOUT_SECONDS 30
set_test_env_value GRAFANA_SESSION_TTL_SECONDS 7200
set_test_env_value GRAFANA_FOLDER_UID ops-board-folder
(
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >/dev/null
)
grep -Fxq 'GRAFANA_TIMEOUT_SECONDS=30' "$test_dir/.env"
grep -Fxq 'GRAFANA_SESSION_TTL_SECONDS=7200' "$test_dir/.env"
grep -Fxq 'GRAFANA_FOLDER_UID=ops-board-folder' "$test_dir/.env"
if [ "$(grafana_env_value GRAFANA_ADMIN_PASSWORD)" != "$grafana_password_snapshot" ]; then
  printf 'repeated validation runs must not rotate the generated Grafana administrator password\n' >&2
  exit 1
fi

# Invalid values are rejected instead of being silently normalized.
set_test_env_value GRAFANA_TIMEOUT_SECONDS not-a-number
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >"$test_dir/grafana-invalid-timeout.log" 2>&1
); then
  printf 'a non-integer GRAFANA_TIMEOUT_SECONDS must be rejected\n' >&2
  exit 1
fi
if [ ! -s "$test_dir/grafana-invalid-timeout.log" ]; then
  printf 'the rejected GRAFANA_TIMEOUT_SECONDS run must write its diagnostic log\n' >&2
  exit 1
fi
if ! grep -Fq 'GRAFANA_TIMEOUT_SECONDS must be an integer between 3 and 60' \
    "$test_dir/grafana-invalid-timeout.log"; then
  printf 'the rejected GRAFANA_TIMEOUT_SECONDS log must contain the validation error\n' >&2
  exit 1
fi
set_test_env_value GRAFANA_TIMEOUT_SECONDS 15
set_test_env_value GRAFANA_SESSION_TTL_SECONDS 5
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >/dev/null 2>&1
); then
  printf 'an out-of-range GRAFANA_SESSION_TTL_SECONDS must be rejected\n' >&2
  exit 1
fi
set_test_env_value GRAFANA_SESSION_TTL_SECONDS 28800
set_test_env_value GRAFANA_ADMIN_PASSWORD short
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT" >/dev/null 2>&1
); then
  printf 'a too-short GRAFANA_ADMIN_PASSWORD must be rejected\n' >&2
  exit 1
fi
set_test_env_value GRAFANA_ADMIN_PASSWORD "$grafana_password_snapshot"
# Restore the values later fake-deployment sections expect (FIRST_ADMIN_USER is
# back to the .env.example default; the folder identity is back on contract).
set_test_env_value FIRST_ADMIN_USER admin
set_test_env_value GRAFANA_FOLDER_UID openontology
printf 'ok deploy-grafana-env-materialization\n'

# Isolated Grafana startup/health-wait semantics. The harness sources the real
# run_grafana_bootstrap/start_and_verify_grafana_stack definitions extracted
# from deploy-prod.sh (the product function is the subject under test) and
# replaces only the external boundary (compose/run_with_retry/health/log).
# Tests call the function from an if-condition, so they also prove the new
# per-stage returns work when errexit is suppressed by the caller.
grafana_isolation_dir="$test_dir/grafana-startup-isolation"
mkdir -p "$grafana_isolation_dir"

extract_deploy_function() {
  local function_name="$1"
  awk -v function_name="$function_name" '
    $0 == function_name "() {" { in_function = 1 }
    in_function { print }
    in_function && $0 == "}" { exit }
  ' "$DEPLOY_SCRIPT"
}

grafana_bootstrap_source="$(extract_deploy_function run_grafana_bootstrap)"
grafana_start_source="$(extract_deploy_function start_and_verify_grafana_stack)"
if [ -z "$grafana_bootstrap_source" ] || [ -z "$grafana_start_source" ]; then
  printf 'the Grafana deploy functions must be extractable for isolation tests\n' >&2
  exit 1
fi

grafana_harness="$grafana_isolation_dir/harness.sh"
{
  printf '%s\n' '#!/usr/bin/env bash'
  printf '%s\n' 'set -Eeuo pipefail'
  cat <<'GRAFANA_HARNESS_HEADER'
log() { printf 'log: %s\n' "$*"; }
run_with_retry() { "$@"; }
compose() {
  printf '%s\n' "$*" >>"$CASE_LOG"
  case "$*" in
    'ps --status running -q grafana')
      if [ "${STATUS_QUERY_RC:-0}" -ne 0 ]; then
        return "$STATUS_QUERY_RC"
      fi
      if [ "${GRAFANA_WAS_RUNNING:-0}" -eq 1 ]; then
        printf 'grafana-container-id\n'
      fi
      return 0
      ;;
    'up --help')
      if [ "${COMPOSE_HAS_WAIT:-1}" -eq 1 ]; then
        printf '      --wait\n'
      fi
      return 0
      ;;
    'up -d --wait --wait-timeout 360 grafana')
      return "${WAIT_RC:-0}"
      ;;
    'up -d grafana')
      return "${UP_RC:-0}"
      ;;
    'stop --timeout 30 grafana')
      return "${STOP_RC:-0}"
      ;;
    'run --rm --no-deps grafana-bootstrap python -m app.operations.grafana.bootstrap')
      return "${BOOTSTRAP_RC:-0}"
      ;;
    'run --rm --no-deps grafana-bootstrap python -m app.operations.grafana.bootstrap --verify')
      return "${VERIFY_RC:-0}"
      ;;
  esac
  printf 'unexpected compose invocation: %s\n' "$*" >&2
  return 97
}
wait_for_service_healthy() {
  printf 'wait_for_service_healthy %s\n' "$*" >>"$CASE_LOG"
  return "${HEALTH_RC:-0}"
}
GRAFANA_HARNESS_HEADER
  printf '%s\n' "$grafana_bootstrap_source"
  printf '%s\n' "$grafana_start_source"
  cat <<'GRAFANA_HARNESS_MAIN'
rc=0
if start_and_verify_grafana_stack; then
  rc=0
else
  rc=$?
fi
printf 'rc=%s\n' "$rc" >"$CASE_RESULT"
exit 0
GRAFANA_HARNESS_MAIN
} >"$grafana_harness"

run_grafana_isolation_case() {
  local case_id="$1" compose_has_wait="$2" was_running="$3" status_rc="$4"
  local wait_rc="$5" up_rc="$6" health_rc="$7" bootstrap_rc="$8" verify_rc="$9"
  local case_dir="$grafana_isolation_dir/$case_id"
  mkdir -p "$case_dir"
  (
    cd "$test_dir"
    CASE_LOG="$case_dir/compose.log" \
      CASE_RESULT="$case_dir/result" \
      COMPOSE_HAS_WAIT="$compose_has_wait" \
      GRAFANA_WAS_RUNNING="$was_running" \
      STATUS_QUERY_RC="$status_rc" \
      WAIT_RC="$wait_rc" \
      UP_RC="$up_rc" \
      HEALTH_RC="$health_rc" \
      BOOTSTRAP_RC="$bootstrap_rc" \
      VERIFY_RC="$verify_rc" \
      bash "$grafana_harness" >"$case_dir/stdout.log" 2>&1
  )
}

assert_grafana_case_calls() {
  local case_id="$1" pattern="$2" expected="$3"
  local log_file="$grafana_isolation_dir/$case_id/compose.log" actual
  if [ ! -f "$log_file" ]; then
    printf 'Grafana isolation case %s did not record compose calls\n' "$case_id" >&2
    exit 1
  fi
  actual="$(awk -v pattern="$pattern" '
    $0 == pattern { count += 1 }
    END { print count + 0 }
  ' "$log_file")"
  if [ "$actual" -ne "$expected" ]; then
    printf 'Grafana isolation case %s: expected %s call(s) to "%s", got %s\n' \
      "$case_id" "$expected" "$pattern" "$actual" >&2
    exit 1
  fi
}

assert_grafana_bootstrap_order() {
  local case_id="$1"
  local log_file="$grafana_isolation_dir/$case_id/compose.log"
  local bootstrap_line verify_line
  bootstrap_line="$(awk '
    /grafana-bootstrap python -m app.operations.grafana.bootstrap/ && !/--verify/ { print NR; exit }
  ' "$log_file")"
  verify_line="$(awk '
    /grafana-bootstrap python -m app.operations.grafana.bootstrap --verify/ { print NR; exit }
  ' "$log_file")"
  if [ -z "$bootstrap_line" ] || [ -z "$verify_line" ] \
      || [ "$bootstrap_line" -ge "$verify_line" ]; then
    printf 'Grafana isolation case %s must run bootstrap before --verify\n' "$case_id" >&2
    exit 1
  fi
}

assert_grafana_isolation_case() {
  local case_id="$1" expected_rc="$2" status_calls="$3" wait_calls="$4"
  local up_calls="$5" health_calls="$6" stop_calls="$7"
  local bootstrap_calls="$8" verify_calls="$9"
  local case_dir="$grafana_isolation_dir/$case_id" rc first_line
  if [ ! -s "$case_dir/result" ]; then
    printf 'Grafana isolation case %s did not produce a result\n' "$case_id" >&2
    exit 1
  fi
  rc="$(sed -n 's/^rc=//p' "$case_dir/result")"
  case "$rc" in
    ''|*[!0-9]*)
      printf 'Grafana isolation case %s did not report a numeric return code\n' "$case_id" >&2
      exit 1
      ;;
  esac
  case "$expected_rc" in
    zero)
      if [ "$rc" -ne 0 ]; then
        printf 'Grafana isolation case %s must return zero, got %s\n' "$case_id" "$rc" >&2
        exit 1
      fi
      ;;
    nonzero)
      if [ "$rc" -eq 0 ]; then
        printf 'Grafana isolation case %s must return nonzero\n' "$case_id" >&2
        exit 1
      fi
      ;;
    *)
      printf 'Grafana isolation case %s has an invalid expected return code\n' "$case_id" >&2
      exit 1
      ;;
  esac
  if grep -Fq 'unexpected compose invocation:' "$case_dir/stdout.log"; then
    printf 'Grafana isolation case %s invoked an unexpected compose command\n' "$case_id" >&2
    exit 1
  fi
  first_line="$(sed -n '1p' "$grafana_isolation_dir/$case_id/compose.log")"
  if [ "$first_line" != 'ps --status running -q grafana' ]; then
    printf 'Grafana isolation case %s must query the running state before startup\n' "$case_id" >&2
    exit 1
  fi
  assert_grafana_case_calls "$case_id" 'ps --status running -q grafana' "$status_calls"
  assert_grafana_case_calls "$case_id" 'up -d --wait --wait-timeout 360 grafana' "$wait_calls"
  assert_grafana_case_calls "$case_id" 'up -d grafana' "$up_calls"
  assert_grafana_case_calls "$case_id" 'wait_for_service_healthy grafana 48 10' "$health_calls"
  assert_grafana_case_calls "$case_id" 'stop --timeout 30 grafana' "$stop_calls"
  assert_grafana_case_calls "$case_id" \
    'run --rm --no-deps grafana-bootstrap python -m app.operations.grafana.bootstrap' \
    "$bootstrap_calls"
  assert_grafana_case_calls "$case_id" \
    'run --rm --no-deps grafana-bootstrap python -m app.operations.grafana.bootstrap --verify' \
    "$verify_calls"
}

run_grafana_isolation_case status-query-failure 1 0 1 0 0 0 0 0
assert_grafana_isolation_case status-query-failure nonzero 1 0 0 0 0 0 0

run_grafana_isolation_case no-old-wait-failure 1 0 0 1 0 0 0 0
assert_grafana_isolation_case no-old-wait-failure nonzero 1 1 0 0 1 0 0

run_grafana_isolation_case old-wait-failure 1 1 0 1 0 0 0 0
assert_grafana_isolation_case old-wait-failure nonzero 1 1 0 0 0 0 0

run_grafana_isolation_case no-old-fallback-up-failure 0 0 0 0 1 0 0 0
assert_grafana_isolation_case no-old-fallback-up-failure nonzero 1 0 1 0 1 0 0

run_grafana_isolation_case old-fallback-up-failure 0 1 0 0 1 0 0 0
assert_grafana_isolation_case old-fallback-up-failure nonzero 1 0 1 0 0 0 0

run_grafana_isolation_case no-old-fallback-health-failure 0 0 0 0 0 1 0 0
assert_grafana_isolation_case no-old-fallback-health-failure nonzero 1 0 1 1 1 0 0

run_grafana_isolation_case old-fallback-health-failure 0 1 0 0 0 1 0 0
assert_grafana_isolation_case old-fallback-health-failure nonzero 1 0 1 1 0 0 0

run_grafana_isolation_case normal-wait 1 0 0 0 0 0 0 0
assert_grafana_isolation_case normal-wait zero 1 1 0 0 0 1 1
assert_grafana_bootstrap_order normal-wait

run_grafana_isolation_case normal-fallback 0 0 0 0 0 0 0 0
assert_grafana_isolation_case normal-fallback zero 1 0 1 1 0 1 1
assert_grafana_bootstrap_order normal-fallback

printf 'ok deploy-grafana-startup-isolation\n'

fake_bin="$test_dir/fake-bin"
fake_docker_log="$test_dir/fake-docker.log"
mkdir -p "$fake_bin"
cat >"$fake_bin/docker" <<'FAKE_DOCKER'
#!/usr/bin/env bash
set -Eeuo pipefail

for key in \
  ENVIRONMENT PUBLIC_PORT POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD \
  DATABASE_URL REDIS_PASSWORD REDIS_URL NEO4J_URI NEO4J_USER \
  NEO4J_PASSWORD NEO4J_AUTH MINIO_ENDPOINT MINIO_ACCESS_KEY \
  MINIO_SECRET_KEY MINIO_USE_SSL N8N_API_URL N8N_API_KEY \
  N8N_TIMEOUT_SECONDS STEWARD_BROWSER_CDP_URL CORS_ALLOWED_ORIGINS \
  UPLOADS_DIR PIPELINE_FILE_GATEWAY_BASE_URL \
  PIPELINE_FILE_PUBLIC_APP_BASE_URL PIPELINE_FILE_PUBLIC_API_BASE_URL \
  STEWARD_INTERNAL_PROXY_BASE_URL \
  STEWARD_BROWSER_HTTP_LEASE_SECONDS \
  STEWARD_BROWSER_HTTP_FRAME_INTERVAL_MS STEWARD_BROWSER_MAX_SESSIONS \
  STEWARD_BROWSER_MAX_SESSIONS_PER_USER \
  STEWARD_BROWSER_IDLE_TIMEOUT_SECONDS \
  STEWARD_BROWSER_REAPER_INTERVAL_SECONDS POSTGRES_IMAGE REDIS_IMAGE \
  NEO4J_IMAGE MINIO_IMAGE BROWSER_IMAGE PYTHON_BASE_IMAGE NODE_BASE_IMAGE \
  NGINX_BASE_IMAGE STRICT_IMAGE_DIGESTS SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE \
  ASSISTANT_ACTION_EXECUTION_ENABLED ASSISTANT_BROWSER_PROTECTED_ORIGINS \
  AGENT_QUERY_STATEMENT_TIMEOUT_MS AGENT_QUERY_LOCK_TIMEOUT_MS \
  AGENT_QUERY_WORK_MEM_MIB AGENT_QUERY_TEMP_FILE_LIMIT_MIB COMPOSE_DISABLE_ENV_FILE \
  COMPOSE_ENV_FILES COMPOSE_FILE COMPOSE_PATH_SEPARATOR COMPOSE_PROFILES \
  COMPOSE_PROJECT_NAME SUPER_ASSISTANT_MCP_INTERNAL_BASE_URLS WEKNORA_BASE_URL  WEKNORA_WEB_URL  \
  WEKNORA_HTTP_PORT WEKNORA_HTTPS_PORT WEKNORA_ADMIN_USER WEKNORA_ADMIN_EMAIL \
  WEKNORA_ADMIN_PASSWORD   \
   WEKNORA_TIMEOUT_SECONDS WEKNORA_DB_NAME \
  WEKNORA_DB_USER WEKNORA_DB_PASSWORD WEKNORA_REDIS_PASSWORD \
   WEKNORA_JWT_SECRET WEKNORA_SYSTEM_AES_KEY \
  WEKNORA_SYSTEM_SIGNING_KEY  WEKNORA_APP_IMAGE \
  WEKNORA_UI_IMAGE WEKNORA_DOCREADER_IMAGE WEKNORA_POSTGRES_IMAGE \
  WEKNORA_REDIS_IMAGE  WEKNORA_MAX_FILE_SIZE_MB \
  WEKNORA_DEFAULT_LOCALE KANEO_BASE_URL KANEO_WEB_URL KANEO_TIMEOUT_SECONDS \
  KANEO_HTTP_PORT KANEO_HTTPS_PORT KANEO_APP_IMAGE KANEO_POSTGRES_IMAGE \
  KANEO_DB_USER KANEO_DB_NAME KANEO_DB_PASSWORD KANEO_AUTH_SECRET \
  KANEO_ADMIN_USER KANEO_ADMIN_EMAIL KANEO_ADMIN_PASSWORD \
  HINDSIGHT_ENABLED HINDSIGHT_BASE_URL HINDSIGHT_WEB_URL HINDSIGHT_WEB_PORT \
  HINDSIGHT_CP_ACCESS_KEY HINDSIGHT_API_KEY \
  HINDSIGHT_SERVICE_TOKEN HINDSIGHT_TIMEOUT_SECONDS HINDSIGHT_NAMESPACE \
  HINDSIGHT_DB_NAME HINDSIGHT_DB_USER HINDSIGHT_DB_PASSWORD \
  HINDSIGHT_IMAGE HINDSIGHT_POSTGRES_IMAGE HINDSIGHT_HF_OFFLINE PUBLIC_TLS_IP \
  GRAFANA_IMAGE GRAFANA_BASE_URL GRAFANA_TIMEOUT_SECONDS GRAFANA_ADMIN_USER \
  GRAFANA_ADMIN_PASSWORD GRAFANA_FOLDER_UID GRAFANA_FOLDER_TITLE \
  GRAFANA_SESSION_TTL_SECONDS \
  MULTICA_ENABLED MULTICA_BACKEND_IMAGE MULTICA_WEB_IMAGE \
  MULTICA_POSTGRES_IMAGE MULTICA_POSTGRES_DB MULTICA_POSTGRES_USER \
  MULTICA_POSTGRES_PASSWORD MULTICA_JWT_SECRET MULTICA_WEB_PORT \
  MULTICA_WEB_BIND_ADDR \
  MULTICA_APP_ENV MULTICA_FRONTEND_ORIGIN MULTICA_APP_URL \
  MULTICA_ALLOW_SIGNUP MULTICA_DATABASE_STARTUP_TIMEOUT \
  MULTICA_DEFAULT_BASE_URL MULTICA_DEFAULT_WEB_URL \
  MULTICA_DEFAULT_WORKSPACE_ID MULTICA_DEFAULT_WORKSPACE_NAME \
  MULTICA_DEFAULT_PAT SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS; do
  # A present-but-empty value is not a leak: compose_environment pins
  # COMPOSE_PROFILES to an explicit empty assignment so a server .env (or
  # shell) can never smuggle profiles into the script-owned compose calls.
  if [ "${!key+x}" = "x" ] && [ -n "${!key}" ]; then
    printf 'compose environment leaked authority key: %s\n' "$key" \
      >>"$FAKE_DOCKER_LOG"
    exit 88
  fi
done

printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"
if [ "${1-}" = compose ] && [ "${2-}" = version ]; then
  exit 0
fi
if [ "${1-}" = image ] && [ "${2-}" = ls ]; then
  printf '%s\n' "$(printf 'a%.0s' {1..64})"
  exit 0
fi
if [ "${1-}" = image ] && [ "${2-}" = inspect ]; then
  case "$*" in
    *'{{.Os}}/{{.Architecture}}'*) printf 'linux/amd64\n' ;;
    *'{{.Created}}'*) printf '2026-11-02T03:00:00Z sha256:%s\n' "$(printf 'a%.0s' {1..64})" ;;
    *) printf 'sha256:%s\n' "$(printf 'a%.0s' {1..64})" ;;
  esac
  exit 0
fi
case "$*" in
  *' build --print backend')
    printf '%s\n' '{'
    printf '%s\n' '  "target": {'
    printf '%s\n' '    "backend": {'
    printf '%s\n' '      "tags": ['
    printf '%s\n' '        "fixture-tag"'
    printf '%s\n' '      ]'
    printf '%s\n' '    }'
    printf '%s\n' '  }'
    printf '%s\n' '}'
    ;;
  *' up --help')
    printf '%s\n' '      --wait'
    ;;
  *' alembic heads')
    printf '%s\n' '0055_projection_readiness (head)'
    ;;
  *' up -d --remove-orphans')
    exit 42
    ;;
esac
FAKE_DOCKER
chmod +x "$fake_bin/docker"

if (
  cd "$test_dir"
  PATH="$fake_bin:$PATH" \
    FAKE_DOCKER_LOG="$fake_docker_log" \
    ENVIRONMENT=development \
    PUBLIC_PORT=65535 \
    PUBLIC_TLS_IP=203.0.113.9 \
    POSTGRES_DB=shell_override \
    POSTGRES_USER=shell_override \
    POSTGRES_PASSWORD=shell-override-password \
    DATABASE_URL=postgres://shell-override.invalid/wrong \
    REDIS_PASSWORD=shell-override-password \
    REDIS_URL=redis://:wrong@shell-override.invalid:6379/0 \
    NEO4J_URI=bolt://shell-override.invalid:7687 \
    NEO4J_USER=shell_override \
    NEO4J_PASSWORD=shell-override-password \
    NEO4J_AUTH=shell_override/shell-override-password \
    MINIO_ENDPOINT=shell-override.invalid:9000 \
    MINIO_ACCESS_KEY=shell-override-access \
    MINIO_SECRET_KEY=shell-override-secret \
    MINIO_USE_SSL=false \
    N8N_API_URL=https://shell-override.invalid \
    N8N_API_KEY=shell-override-key \
    N8N_TIMEOUT_SECONDS=3 \
    STEWARD_BROWSER_CDP_URL=http://shell-override.invalid:9222 \
    CORS_ALLOWED_ORIGINS=https://shell-override.invalid \
    UPLOADS_DIR=/tmp/shell-override-uploads \
    PIPELINE_FILE_GATEWAY_BASE_URL=https://shell-override.invalid/gateway \
    PIPELINE_FILE_PUBLIC_APP_BASE_URL=https://shell-override.invalid \
    PIPELINE_FILE_PUBLIC_API_BASE_URL=https://shell-override.invalid \
    STEWARD_INTERNAL_PROXY_BASE_URL=https://shell-override.invalid/internal-proxy \
    STEWARD_BROWSER_HTTP_LEASE_SECONDS=1 \
    STEWARD_BROWSER_HTTP_FRAME_INTERVAL_MS=1 \
    STEWARD_BROWSER_MAX_SESSIONS=1 \
    STEWARD_BROWSER_MAX_SESSIONS_PER_USER=1 \
    STEWARD_BROWSER_IDLE_TIMEOUT_SECONDS=1 \
    STEWARD_BROWSER_REAPER_INTERVAL_SECONDS=1 \
    SUPER_ASSISTANT_MCP_INTERNAL_BASE_URLS=http://shell-override.invalid/mcp \
    WEKNORA_BASE_URL=https://shell-override.invalid \
    WEKNORA_WEB_URL=https://shell-override.invalid \
    HINDSIGHT_WEB_URL=https://shell-override.invalid \
    WEKNORA_HTTP_PORT=1 \
    WEKNORA_HTTPS_PORT=2 \
    WEKNORA_ADMIN_USER=shell_override \
    WEKNORA_ADMIN_EMAIL=shell-override@example.invalid \
    WEKNORA_ADMIN_PASSWORD=shell-override-password \
    WEKNORA_TIMEOUT_SECONDS=3 \
    WEKNORA_DB_NAME=shell_override \
    WEKNORA_DB_USER=shell_override \
    WEKNORA_DB_PASSWORD=shell-override-password \
    WEKNORA_REDIS_PASSWORD=shell-override-password \
    WEKNORA_JWT_SECRET=shell-override-secret-0123456789abcdef \
    WEKNORA_SYSTEM_AES_KEY=shell-override-secret-0123456789abcdef \
    WEKNORA_SYSTEM_SIGNING_KEY=shell-override-secret-0123456789abcdef \
    WEKNORA_APP_IMAGE=example.invalid/unvalidated:latest \
    WEKNORA_UI_IMAGE=example.invalid/unvalidated:latest \
    WEKNORA_DOCREADER_IMAGE=example.invalid/unvalidated:latest \
    WEKNORA_POSTGRES_IMAGE=example.invalid/unvalidated:latest \
    WEKNORA_REDIS_IMAGE=example.invalid/unvalidated:latest \
    KANEO_BASE_URL=https://shell-override.invalid \
    KANEO_WEB_URL=http://shell-override.invalid:1234 \
    KANEO_TIMEOUT_SECONDS=3 \
    KANEO_HTTP_PORT=3 \
    KANEO_HTTPS_PORT=4 \
    KANEO_APP_IMAGE=example.invalid/unvalidated:latest \
    KANEO_POSTGRES_IMAGE=example.invalid/unvalidated:latest \
    KANEO_DB_USER=shell_override \
    KANEO_DB_NAME=shell_override \
    KANEO_DB_PASSWORD=shell-override-password \
    KANEO_AUTH_SECRET=shell-override-secret-0123456789abcdef \
    KANEO_ADMIN_USER=shell_override \
    KANEO_ADMIN_EMAIL=shell-override@example.invalid \
    KANEO_ADMIN_PASSWORD=shell-override-password \
    MULTICA_ENABLED=true \
    MULTICA_BACKEND_IMAGE=example.invalid/unvalidated:latest \
    MULTICA_WEB_IMAGE=example.invalid/unvalidated:latest \
    MULTICA_POSTGRES_IMAGE=example.invalid/unvalidated:latest \
    MULTICA_WEB_PORT=1 \
    MULTICA_WEB_BIND_ADDR=10.9.8.7 \
    MULTICA_DEFAULT_BASE_URL=https://shell-override.invalid \
    MULTICA_DEFAULT_WEB_URL=https://shell-override.invalid \
    MULTICA_DEFAULT_WORKSPACE_ID=shell_override \
    MULTICA_DEFAULT_WORKSPACE_NAME=shell_override \
    MULTICA_DEFAULT_PAT=shell-override-pat \
    SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS=https://shell-override.invalid \
    POSTGRES_IMAGE=example.invalid/unvalidated:latest \
    REDIS_IMAGE=example.invalid/unvalidated:latest \
    NEO4J_IMAGE=example.invalid/unvalidated:latest \
    MINIO_IMAGE=example.invalid/unvalidated:latest \
    BROWSER_IMAGE=example.invalid/unvalidated:latest \
    PYTHON_BASE_IMAGE=example.invalid/unvalidated:latest \
    NODE_BASE_IMAGE=example.invalid/unvalidated:latest \
    NGINX_BASE_IMAGE=example.invalid/unvalidated:latest \
    STRICT_IMAGE_DIGESTS=true \
    SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE=direct_dev \
    ASSISTANT_ACTION_EXECUTION_ENABLED=false \
    ASSISTANT_BROWSER_PROTECTED_ORIGINS=https://shell-override.invalid \
    AGENT_QUERY_STATEMENT_TIMEOUT_MS=1 \
    AGENT_QUERY_LOCK_TIMEOUT_MS=1 \
    AGENT_QUERY_WORK_MEM_MIB=1 \
    AGENT_QUERY_TEMP_FILE_LIMIT_MIB=1 \
    COMPOSE_DISABLE_ENV_FILE=1 \
    COMPOSE_ENV_FILES=/tmp/unvalidated-compose.env \
    COMPOSE_FILE=/tmp/unvalidated-compose.yml \
    COMPOSE_PATH_SEPARATOR=: \
    COMPOSE_PROFILES=unvalidated-profile \
    COMPOSE_PROJECT_NAME=unvalidated-project \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_RETRIES=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/fake-deploy.log" 2>&1; then
  printf 'fake deployment must stop at the intentional final Compose failure\n' >&2
  exit 1
fi

if grep -Fq 'compose environment leaked authority key:' "$fake_docker_log"; then
  printf 'Compose inherited an exported shell authority\n' >&2
  exit 1
fi
weknora_aes_key="$(awk -F= '$1 == "WEKNORA_SYSTEM_AES_KEY" {print substr($0, index($0,"=")+1)}' "$test_dir/.env" | tail -n1)"
if [ "${#weknora_aes_key}" -ne 32 ]; then
  printf 'generated WeKnora AES key must be exactly 32 ASCII bytes\n' >&2
  exit 1
fi
weknora_admin_password="$(awk -F= '$1 == "WEKNORA_ADMIN_PASSWORD" {print substr($0, index($0,"=")+1)}' "$test_dir/.env" | tail -n1)"
case "$weknora_admin_password" in
  Aa1!*) ;;
  *) printf 'generated WeKnora admin password must guarantee upper/lower/digit/special\n' >&2; exit 1 ;;
esac
if [ "${#weknora_admin_password}" -lt 8 ] || [ "${#weknora_admin_password}" -gt 32 ]; then
  printf 'generated WeKnora admin password length out of range\n' >&2
  exit 1
fi
kaneo_admin_password="$(awk -F= '$1 == "KANEO_ADMIN_PASSWORD" {print substr($0, index($0,"=")+1)}' "$test_dir/.env" | tail -n1)"
case "$kaneo_admin_password" in
  Aa1!*) ;;
  *) printf 'generated Kaneo admin password must guarantee upper/lower/digit/special\n' >&2; exit 1 ;;
esac
if [ "${#kaneo_admin_password}" -lt 8 ] || [ "${#kaneo_admin_password}" -gt 32 ]; then
  printf 'generated Kaneo admin password length out of range\n' >&2
  exit 1
fi
kaneo_auth_secret="$(awk -F= '$1 == "KANEO_AUTH_SECRET" {print substr($0, index($0,"=")+1)}' "$test_dir/.env" | tail -n1)"
if [ "${#kaneo_auth_secret}" -lt 32 ]; then
  printf 'generated Kaneo auth secret must contain at least 32 characters\n' >&2
  exit 1
fi
if grep -Fq "$kaneo_admin_password" "$test_dir/fake-deploy.log"; then
  printf 'generated Kaneo admin password leaked into deploy logs\n' >&2
  exit 1
fi
probe_line="$(grep -nF \
  'run --rm --no-deps backend python -m app.shared.dependency_probe' \
  "$fake_docker_log" | head -n1 | cut -d: -f1)"
stop_line="$(grep -nF \
  'stop -t 30 backend pipeline_executor' \
  "$fake_docker_log" | head -n1 | cut -d: -f1)"
heads_line="$(grep -nF \
  'run --rm --no-deps backend alembic heads' \
  "$fake_docker_log" | head -n1 | cut -d: -f1)"
upgrade_line="$(grep -nF \
  'run --rm --no-deps backend alembic upgrade head' \
  "$fake_docker_log" | head -n1 | cut -d: -f1)"
if [ -z "$probe_line" ] || [ -z "$stop_line" ] || [ -z "$heads_line" ] \
    || [ -z "$upgrade_line" ] \
    || [ "$probe_line" -ge "$stop_line" ] \
    || [ "$stop_line" -ge "$heads_line" ] \
    || [ "$heads_line" -ge "$upgrade_line" ]; then
  printf 'backend and Celery worker must stop before Alembic validation/upgrade\n' >&2
  exit 1
fi
kaneo_gateway_close_line="$(grep -nF \
  'stop --timeout 30 kaneo-gateway' \
  "$fake_docker_log" | head -n1 | cut -d: -f1)"
kaneo_verify_line="$(grep -nF \
  'run --rm --no-deps kaneo-bootstrap python -m app.super_assistant.kaneo.bootstrap --verify' \
  "$fake_docker_log" | head -n1 | cut -d: -f1)"
if [ -z "$kaneo_gateway_close_line" ] || [ -z "$kaneo_verify_line" ] \
    || [ "$kaneo_gateway_close_line" -ge "$kaneo_verify_line" ] \
    || [ "$kaneo_verify_line" -ge "$probe_line" ]; then
  printf 'Kaneo gateway must close before private bootstrap and finish verification before the platform probe\n' >&2
  exit 1
fi

# Runtime source attestation: generate from one exact clean Git HEAD, then
# verify the same allowlisted bytes on a source-root fixture. These tests use
# no Docker daemon, no production endpoint, and no real dependency values.
attestation_helper="$REPO_ROOT/deploy/runtime_source_attestation.py"
attestation_root="$test_dir/runtime-source-attestation"
attestation_repo="$attestation_root/repo"
attestation_manifest="$attestation_root/manifest.json"
attestation_source="$attestation_root/source"
mkdir -p "$attestation_repo/backend/app/subdir" "$attestation_root"
git -C "$attestation_repo" init -q
git -C "$attestation_repo" config user.email "deploy-guards@example.invalid"
git -C "$attestation_repo" config user.name "Deploy Guards"
git -C "$attestation_repo" config commit.gpgsign false
printf 'print("fixture")\n' >"$attestation_repo/backend/app/main.py"
printf '{"fixture": true}\n' >"$attestation_repo/backend/app/subdir/data.json"
printf '# fixture readme\n' >"$attestation_repo/backend/app/README.md"
printf 'SECRET_SENTINEL=must-not-enter-manifest\n' >"$attestation_repo/.env"
mkdir -p "$attestation_repo/.artifacts"
printf 'must-not-enter-manifest\n' >"$attestation_repo/.artifacts/secret.txt"
git -C "$attestation_repo" add backend/app
git -C "$attestation_repo" commit -qm "runtime source fixture"
attestation_commit="$(git -C "$attestation_repo" rev-parse HEAD)"

python3 "$attestation_helper" generate \
  --repo "$attestation_repo" \
  --commit "$attestation_commit" \
  --output "$attestation_manifest" >"$attestation_root/generate.log" 2>&1
generated_manifest_sha="$(
  sed -n 's/.*manifest_sha256=\([0-9a-f]\{64\}\).*/\1/p' \
    "$attestation_root/generate.log" | tail -n1
)"
if [ "${#generated_manifest_sha}" -ne 64 ]; then
  printf 'runtime source manifest generator did not emit its digest\n' >&2
  exit 1
fi

python3 - "$attestation_manifest" "$attestation_commit" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
assert manifest["schema"] == "openontology-runtime-source-manifest/v1"
assert manifest["algorithm"] == "sha256"
assert manifest["source_prefix"] == "backend/app"
assert manifest["commit"] == sys.argv[2]
assert len(manifest["tree"]) == 40
assert manifest["file_count"] == len(manifest["files"]) == 3
assert set(manifest["files"]) == {
    "backend/app/main.py",
    "backend/app/subdir/data.json",
    "backend/app/README.md",
}
assert all("SECRET_SENTINEL" not in value for value in manifest["files"])
assert all(not value.startswith("/") for value in manifest["files"])
assert all("/../" not in value for value in manifest["files"])
assert all(".artifacts" not in value for value in manifest["files"])
assert "deploy/runtime-source-manifest.json" not in manifest["files"]
PY

attestation_manifest_two="$attestation_root/manifest-two.json"
python3 "$attestation_helper" generate \
  --repo "$attestation_repo" \
  --commit "$attestation_commit" \
  --output "$attestation_manifest_two" >/dev/null
manifest_sha_one="$(python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$attestation_manifest")"
manifest_sha_two="$(python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$attestation_manifest_two")"
if [ "$manifest_sha_one" != "$manifest_sha_two" ] \
    || [ "$manifest_sha_one" != "$generated_manifest_sha" ]; then
  printf 'runtime source manifest generation must be deterministic\n' >&2
  exit 1
fi

printf 'dirty\n' >>"$attestation_repo/backend/app/main.py"
if python3 "$attestation_helper" generate \
    --repo "$attestation_repo" \
    --commit "$attestation_commit" \
    --output "$attestation_root/dirty.json" >"$attestation_root/dirty.log" 2>&1; then
  printf 'runtime source manifest generation must reject a dirty allowlisted worktree\n' >&2
  exit 1
fi
grep -Fq 'worktree changes exist under backend/app' "$attestation_root/dirty.log"
git -C "$attestation_repo" checkout -- backend/app/main.py

if python3 "$attestation_helper" generate \
    --repo "$attestation_repo" \
    --commit 0000000000000000000000000000000000000000 \
    --output "$attestation_root/wrong-commit.json" >"$attestation_root/wrong-commit.log" 2>&1; then
  printf 'runtime source manifest generation must reject a non-HEAD commit\n' >&2
  exit 1
fi
grep -Fq 'checked-out HEAD' "$attestation_root/wrong-commit.log"
printf 'ok runtime-source-manifest-exact-head\n'

cp -R "$attestation_repo/backend/app" "$attestation_source"
attestation_container_id="$(printf 'c%.0s' {1..64})"
attestation_image_id="sha256:$(printf 'd%.0s' {1..64})"
attestation_started_at="2026-11-02T03:00:00.123456789Z"
file_set_sha="$(
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["file_set_sha256"])' \
    "$attestation_manifest"
)"
run_attestation_verify() {
  local manifest="$1" source="$2" expected="$3" output="$4"
  python3 "$attestation_helper" verify \
    --manifest "$manifest" \
    --source-root "$source" \
    --expected-commit "$expected" \
    --container-id "$attestation_container_id" \
    --image-id "$attestation_image_id" \
    --started-at "$attestation_started_at" >"$output" 2>&1
}

run_attestation_verify \
  "$attestation_manifest" "$attestation_source" "$attestation_commit" \
  "$attestation_root/match.log"
grep -Fq 'status=ok scope=running_container_files_not_loaded_memory' \
  "$attestation_root/match.log"
grep -Fq "commit=${attestation_commit}" "$attestation_root/match.log"
grep -Fq "manifest_sha256=${generated_manifest_sha}" "$attestation_root/match.log"
grep -Fq "file_set_sha256=${file_set_sha}" "$attestation_root/match.log"
grep -Fq "container_id=${attestation_container_id}" "$attestation_root/match.log"
grep -Fq "image_id=${attestation_image_id}" "$attestation_root/match.log"
grep -Fq "started_at=${attestation_started_at}" "$attestation_root/match.log"
if grep -Fq 'SECRET_SENTINEL' "$attestation_root/match.log"; then
  printf 'runtime source attestation leaked the synthetic secret sentinel\n' >&2
  exit 1
fi

manifest_b64="$(base64 <"$attestation_manifest" | tr -d '\r\n')"
python3 "$attestation_helper" verify \
  --manifest-b64 "$manifest_b64" \
  --source-root "$attestation_source" \
  --expected-commit "$attestation_commit" \
  --container-id "$attestation_container_id" \
  --image-id "$attestation_image_id" \
  --started-at "$attestation_started_at" >"$attestation_root/match-b64.log" 2>&1
grep -Fq 'status=ok scope=running_container_files_not_loaded_memory' \
  "$attestation_root/match-b64.log"

mkdir -p "$attestation_source/__pycache__" "$attestation_source/subdir/__pycache__"
printf 'cache\n' >"$attestation_source/__pycache__/main.cpython-312.pyc"
printf 'cache\n' >"$attestation_source/subdir/__pycache__/data.cpython-312.pyc"
printf 'cache\n' >"$attestation_source/subdir/legacy.pyc"
run_attestation_verify \
  "$attestation_manifest" "$attestation_source" "$attestation_commit" \
  "$attestation_root/cache-ignore.log"
grep -Fq 'status=ok scope=running_container_files_not_loaded_memory' \
  "$attestation_root/cache-ignore.log"
rm -rf "$attestation_source/__pycache__" "$attestation_source/subdir/__pycache__"
rm -f "$attestation_source/subdir/legacy.pyc"
printf 'ok runtime-source-verifier-match-and-cache\n'

cp -R "$attestation_repo/backend/app" "$attestation_root/tampered"
printf 'tampered\n' >>"$attestation_root/tampered/main.py"
if run_attestation_verify \
    "$attestation_manifest" "$attestation_root/tampered" "$attestation_commit" \
    "$attestation_root/tampered.log"; then
  printf 'runtime source attestation must reject changed file bytes\n' >&2
  exit 1
fi
grep -Fq 'status=mismatch' "$attestation_root/tampered.log"
grep -Fq 'changed_path=backend/app/main.py' "$attestation_root/tampered.log"
if grep -Fq 'status=ok' "$attestation_root/tampered.log"; then
  printf 'runtime source mismatch must not emit a success record\n' >&2
  exit 1
fi

cp -R "$attestation_repo/backend/app" "$attestation_root/missing"
rm -f "$attestation_root/missing/main.py"
if run_attestation_verify \
    "$attestation_manifest" "$attestation_root/missing" "$attestation_commit" \
    "$attestation_root/missing.log"; then
  printf 'runtime source attestation must reject missing files\n' >&2
  exit 1
fi
grep -Fq 'missing_path=backend/app/main.py' "$attestation_root/missing.log"

cp -R "$attestation_repo/backend/app" "$attestation_root/extra"
printf 'extra\n' >"$attestation_root/extra/extra.py"
if run_attestation_verify \
    "$attestation_manifest" "$attestation_root/extra" "$attestation_commit" \
    "$attestation_root/extra.log"; then
  printf 'runtime source attestation must reject extra files\n' >&2
  exit 1
fi
grep -Fq 'extra_path=backend/app/extra.py' "$attestation_root/extra.log"

cp -R "$attestation_repo/backend/app" "$attestation_root/symlink"
ln -s main.py "$attestation_root/symlink/link.py"
if run_attestation_verify \
    "$attestation_manifest" "$attestation_root/symlink" "$attestation_commit" \
    "$attestation_root/symlink.log"; then
  printf 'runtime source attestation must reject symlinks\n' >&2
  exit 1
fi
grep -Fq 'symlink' "$attestation_root/symlink.log"

cp -R "$attestation_repo/backend/app" "$attestation_root/fifo"
mkfifo "$attestation_root/fifo/pipe.py"
if run_attestation_verify \
    "$attestation_manifest" "$attestation_root/fifo" "$attestation_commit" \
    "$attestation_root/fifo.log"; then
  printf 'runtime source attestation must reject non-regular files\n' >&2
  exit 1
fi
grep -Fq 'non-regular' "$attestation_root/fifo.log"

if run_attestation_verify \
    "$attestation_manifest" "$attestation_source" \
    0000000000000000000000000000000000000000 \
    "$attestation_root/wrong-expected.log"; then
  printf 'runtime source attestation must reject a mismatched expected commit\n' >&2
  exit 1
fi
grep -Fq 'does not match expected commit' "$attestation_root/wrong-expected.log"

if python3 "$attestation_helper" verify \
    --manifest "$attestation_root/does-not-exist.json" \
    --source-root "$attestation_source" \
    --expected-commit "$attestation_commit" \
    --container-id "$attestation_container_id" \
    --image-id "$attestation_image_id" \
    --started-at "$attestation_started_at" >"$attestation_root/missing-manifest.log" 2>&1; then
  printf 'runtime source attestation must reject a missing manifest\n' >&2
  exit 1
fi
grep -Fq 'manifest is missing' "$attestation_root/missing-manifest.log"

python3 - "$attestation_root/oversized.json" <<'PY'
import sys
with open(sys.argv[1], "wb") as handle:
    handle.write(b"x" * (16 * 1024 * 1024 + 1))
PY
if run_attestation_verify \
    "$attestation_root/oversized.json" "$attestation_source" "$attestation_commit" \
    "$attestation_root/oversized.log"; then
  printf 'runtime source attestation must reject an oversized manifest\n' >&2
  exit 1
fi
grep -Fq 'exceeds the size limit' "$attestation_root/oversized.log"

cp "$attestation_manifest" "$attestation_root/unreadable-manifest.json"
chmod 000 "$attestation_root/unreadable-manifest.json"
if run_attestation_verify \
    "$attestation_root/unreadable-manifest.json" "$attestation_source" "$attestation_commit" \
    "$attestation_root/unreadable-manifest.log"; then
  printf 'runtime source attestation must reject an unreadable manifest\n' >&2
  exit 1
fi
chmod 644 "$attestation_root/unreadable-manifest.json"
grep -Fq 'unreadable' "$attestation_root/unreadable-manifest.log"

cp -R "$attestation_repo/backend/app" "$attestation_root/unreadable-source"
chmod 000 "$attestation_root/unreadable-source/main.py"
if run_attestation_verify \
    "$attestation_manifest" "$attestation_root/unreadable-source" "$attestation_commit" \
    "$attestation_root/unreadable-source.log"; then
  printf 'runtime source attestation must reject unreadable source files\n' >&2
  exit 1
fi
chmod 644 "$attestation_root/unreadable-source/main.py"
grep -Fq 'source file is unreadable' "$attestation_root/unreadable-source.log"

python3 - "$attestation_manifest" "$attestation_root/duplicate.json" <<'PY'
import sys
text = open(sys.argv[1], encoding="utf-8").read()
marker = '"files": {'
duplicate = '    "backend/app/main.py": "' + ("0" * 64) + '",\n'
text = text.replace(marker, marker + "\n" + duplicate, 1)
open(sys.argv[2], "w", encoding="utf-8").write(text)
PY
if run_attestation_verify \
    "$attestation_root/duplicate.json" "$attestation_source" "$attestation_commit" \
    "$attestation_root/duplicate.log"; then
  printf 'runtime source attestation must reject duplicate manifest paths\n' >&2
  exit 1
fi
grep -Fq 'duplicate key' "$attestation_root/duplicate.log"

python3 - "$attestation_manifest" "$attestation_root/traversal.json" <<'PY'
import json
import sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
manifest["files"]["backend/app/../escape.py"] = "0" * 64
manifest["file_count"] = len(manifest["files"])
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, ensure_ascii=True, indent=2, sort_keys=True)
PY
if run_attestation_verify \
    "$attestation_root/traversal.json" "$attestation_source" "$attestation_commit" \
    "$attestation_root/traversal.log"; then
  printf 'runtime source attestation must reject traversal paths\n' >&2
  exit 1
fi
grep -Fq 'traversal' "$attestation_root/traversal.log"

python3 - "$attestation_manifest" "$attestation_root/absolute.json" <<'PY'
import json
import sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
manifest["files"]["/etc/passwd"] = "0" * 64
manifest["file_count"] = len(manifest["files"])
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, ensure_ascii=True, indent=2, sort_keys=True)
PY
if run_attestation_verify \
    "$attestation_root/absolute.json" "$attestation_source" "$attestation_commit" \
    "$attestation_root/absolute.log"; then
  printf 'runtime source attestation must reject absolute manifest paths\n' >&2
  exit 1
fi
grep -Fq 'not relative POSIX' "$attestation_root/absolute.log"
printf 'ok runtime-source-verifier-fail-closed\n'

# Control-plane regression through the real deploy script: a strict fake
# Docker/Compose/curl exercises compose ps/inspect, the bounded stdin verifier
# exec, and the failure stop path without a daemon, production endpoint, or
# real dependency values.
attestation_deploy_dir="$test_dir/deploy"
attestation_fake_bin="$test_dir/attestation-fake-bin"
mkdir -p "$attestation_deploy_dir" "$test_dir/scripts/ci" "$attestation_fake_bin"
cp "$DEPLOY_SCRIPT" "$attestation_deploy_dir/deploy-prod.sh"
cp "$attestation_helper" "$attestation_deploy_dir/runtime_source_attestation.py"
cp "$attestation_manifest" "$attestation_deploy_dir/runtime-source-manifest.json"
cp "$REPO_ROOT/scripts/ci/validate-deploy-app-dir.sh" "$test_dir/scripts/ci/validate-deploy-app-dir.sh"

cat >"$attestation_fake_bin/curl" <<'FAKE_CURL'
#!/usr/bin/env bash
set -Eeuo pipefail
url=""
output=""
headers=""
write_out=""
status="200"
body="ok"
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    -o) output="${args[$((i + 1))]}" ;;
    -D) headers="${args[$((i + 1))]}" ;;
    -w) write_out="${args[$((i + 1))]}" ;;
    http://*|https://*) url="${args[$i]}" ;;
  esac
done
[ -n "$url" ] || { echo "fake curl received no URL" >&2; exit 9; }
case "$url" in
  *'/api/v1/auth/config') body='{"success":true,"registration_mode":"invite_only","complex_password_enabled":false}' ;;
  *'/mcp'*) status="404"; body='not found' ;;
  http://127.0.0.1:8090/*)
    status="308"
    redirect_target="${FAKE_KANEO_WEB_URL:-https://64.83.14.139:8445}/"
    ;;
  # 一键免登录中继：无票据访问由 backend 302 回各自服务的根路径
  https://127.0.0.1:8444/sso/entry) status="302" ;;
  https://127.0.0.1:8445/sso/entry) status="302" ;;
  # Grafana 看板会话门（check_grafana_public_entry）：经平台入口的未认证
  # /grafana/ 与 /grafana/api/health 必须由网关拒绝 401，裸 /grafana 301。
  http://127.0.0.1:*/grafana/) status="401" ;;
  http://127.0.0.1:*/grafana/api/health) status="401" ;;
  http://127.0.0.1:*/grafana) status="301" ;;
  https://127.0.0.1:8445/*) body='ok' ;;
  */healthz) body='ok' ;;
esac
if [ -n "$headers" ]; then
  case "$url" in
    *.js) printf 'HTTP/1.1 200 OK\r\nContent-Type: application/javascript\r\n\r\n' >"$headers" ;;
    *.css) printf 'HTTP/1.1 200 OK\r\nContent-Type: text/css\r\n\r\n' >"$headers" ;;
    *)
      if [ -n "${redirect_target:-}" ]; then
        printf 'HTTP/1.1 %s Permanent Redirect\r\nLocation: %s\r\nContent-Type: text/plain\r\n\r\n' \
          "$status" "$redirect_target" >"$headers"
      else
        printf 'HTTP/1.1 %s OK\r\nContent-Type: text/html\r\n\r\n' "$status" >"$headers"
      fi
      ;;
  esac
fi
if [ -n "$output" ] && [ "$output" != "/dev/null" ]; then
  case "$url" in
    */) printf '<script src="/assets/main.js"></script><link href="/assets/main.css">\n' >"$output" ;;
    *.js) printf 'const asset = "assets/chunk.js";\n' >"$output" ;;
    *.css) printf 'body{}\n' >"$output" ;;
    *) printf '%s\n' "$body" >"$output" ;;
  esac
fi
if [ -n "$write_out" ]; then
  printf '%s' "$write_out" | sed "s/%{http_code}/${status}/g"
elif [ -z "$output" ]; then
  printf '%s\n' "$body"
fi
exit 0
FAKE_CURL

cat >"$attestation_fake_bin/docker" <<'FAKE_DOCKER_SUCCESS'
#!/usr/bin/env bash
set -Eeuo pipefail
for key in \
  ENVIRONMENT PUBLIC_PORT POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD \
  DATABASE_URL REDIS_PASSWORD REDIS_URL NEO4J_URI NEO4J_USER \
  NEO4J_PASSWORD NEO4J_AUTH MINIO_ENDPOINT MINIO_ACCESS_KEY \
  MINIO_SECRET_KEY MINIO_USE_SSL N8N_API_URL N8N_API_KEY \
  N8N_TIMEOUT_SECONDS STEWARD_BROWSER_CDP_URL CORS_ALLOWED_ORIGINS \
  UPLOADS_DIR PIPELINE_FILE_GATEWAY_BASE_URL \
  PIPELINE_FILE_PUBLIC_APP_BASE_URL PIPELINE_FILE_PUBLIC_API_BASE_URL \
  STEWARD_INTERNAL_PROXY_BASE_URL STEWARD_BROWSER_HTTP_LEASE_SECONDS \
  STEWARD_BROWSER_HTTP_FRAME_INTERVAL_MS STEWARD_BROWSER_MAX_SESSIONS \
  STEWARD_BROWSER_MAX_SESSIONS_PER_USER STEWARD_BROWSER_IDLE_TIMEOUT_SECONDS \
  STEWARD_BROWSER_REAPER_INTERVAL_SECONDS POSTGRES_IMAGE REDIS_IMAGE \
  NEO4J_IMAGE MINIO_IMAGE BROWSER_IMAGE PYTHON_BASE_IMAGE NODE_BASE_IMAGE \
  NGINX_BASE_IMAGE STRICT_IMAGE_DIGESTS SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE \
  ASSISTANT_ACTION_EXECUTION_ENABLED ASSISTANT_BROWSER_PROTECTED_ORIGINS \
  AGENT_QUERY_STATEMENT_TIMEOUT_MS AGENT_QUERY_LOCK_TIMEOUT_MS \
  AGENT_QUERY_WORK_MEM_MIB AGENT_QUERY_TEMP_FILE_LIMIT_MIB COMPOSE_DISABLE_ENV_FILE \
  COMPOSE_ENV_FILES COMPOSE_FILE COMPOSE_PATH_SEPARATOR COMPOSE_PROFILES \
  COMPOSE_PROJECT_NAME SUPER_ASSISTANT_MCP_INTERNAL_BASE_URLS WEKNORA_BASE_URL  WEKNORA_WEB_URL  \
  WEKNORA_HTTP_PORT WEKNORA_HTTPS_PORT WEKNORA_ADMIN_USER WEKNORA_ADMIN_EMAIL \
  WEKNORA_ADMIN_PASSWORD   \
   WEKNORA_TIMEOUT_SECONDS WEKNORA_DB_NAME \
  WEKNORA_DB_USER WEKNORA_DB_PASSWORD WEKNORA_REDIS_PASSWORD \
   WEKNORA_JWT_SECRET WEKNORA_SYSTEM_AES_KEY \
  WEKNORA_SYSTEM_SIGNING_KEY  WEKNORA_APP_IMAGE \
  WEKNORA_UI_IMAGE WEKNORA_DOCREADER_IMAGE WEKNORA_POSTGRES_IMAGE \
  WEKNORA_REDIS_IMAGE  WEKNORA_MAX_FILE_SIZE_MB \
  WEKNORA_DEFAULT_LOCALE KANEO_BASE_URL KANEO_WEB_URL KANEO_TIMEOUT_SECONDS \
  KANEO_HTTP_PORT KANEO_HTTPS_PORT KANEO_APP_IMAGE KANEO_POSTGRES_IMAGE \
  KANEO_DB_USER KANEO_DB_NAME KANEO_DB_PASSWORD KANEO_AUTH_SECRET \
  KANEO_ADMIN_USER KANEO_ADMIN_EMAIL KANEO_ADMIN_PASSWORD \
  HINDSIGHT_ENABLED HINDSIGHT_BASE_URL HINDSIGHT_WEB_URL HINDSIGHT_WEB_PORT \
  HINDSIGHT_CP_ACCESS_KEY HINDSIGHT_API_KEY \
  HINDSIGHT_SERVICE_TOKEN HINDSIGHT_TIMEOUT_SECONDS HINDSIGHT_NAMESPACE \
  HINDSIGHT_DB_NAME HINDSIGHT_DB_USER HINDSIGHT_DB_PASSWORD \
  HINDSIGHT_IMAGE HINDSIGHT_POSTGRES_IMAGE HINDSIGHT_HF_OFFLINE PUBLIC_TLS_IP \
  GRAFANA_IMAGE GRAFANA_BASE_URL GRAFANA_TIMEOUT_SECONDS GRAFANA_ADMIN_USER \
  GRAFANA_ADMIN_PASSWORD GRAFANA_FOLDER_UID GRAFANA_FOLDER_TITLE \
  GRAFANA_SESSION_TTL_SECONDS \
  MULTICA_ENABLED MULTICA_BACKEND_IMAGE MULTICA_WEB_IMAGE \
  MULTICA_POSTGRES_IMAGE MULTICA_POSTGRES_DB MULTICA_POSTGRES_USER \
  MULTICA_POSTGRES_PASSWORD MULTICA_JWT_SECRET MULTICA_WEB_PORT \
  MULTICA_WEB_BIND_ADDR \
  MULTICA_APP_ENV MULTICA_FRONTEND_ORIGIN MULTICA_APP_URL \
  MULTICA_ALLOW_SIGNUP MULTICA_DATABASE_STARTUP_TIMEOUT \
  MULTICA_DEFAULT_BASE_URL MULTICA_DEFAULT_WEB_URL \
  MULTICA_DEFAULT_WORKSPACE_ID MULTICA_DEFAULT_WORKSPACE_NAME \
  MULTICA_DEFAULT_PAT SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS; do
  # A present-but-empty value is not a leak: compose_environment pins
  # COMPOSE_PROFILES to an explicit empty assignment so a server .env (or
  # shell) can never smuggle profiles into the script-owned compose calls.
  if [ "${!key+x}" = "x" ] && [ -n "${!key}" ]; then
    printf 'compose environment leaked authority key: %s\n' "$key" \
      >>"$FAKE_DOCKER_LOG"
    exit 88
  fi
done

if printf '%s\n' "$*" | grep -q -- ' exec -T backend '; then
  args=("$@")
  script=""
  rest=()
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [ "${args[$i]}" = "-c" ]; then
      script="${args[$((i + 1))]}"
      rest=("${args[@]:$((i + 2))}")
      break
    fi
  done
  [ -n "$script" ] || { echo "fake exec missing verifier script" >&2; exit 9; }
  printf 'compose exec -T backend python -c <verifier> %s\n' "${rest[*]}" >>"$FAKE_DOCKER_LOG"
  if [ "${FAKE_ATTESTATION_EXEC_FAIL:-0}" = "1" ]; then
    exit 7
  fi
  if [ "${FAKE_ATTESTATION_EXEC_SLEEP:-0}" != "0" ]; then
    sleep "$FAKE_ATTESTATION_EXEC_SLEEP"
  fi
  stdin_file="$FAKE_DOCKER_LOG.stdin"
  cat >"$stdin_file"
  printf 'exec-stdin-bytes=%s\n' "$(wc -c <"$stdin_file" | tr -d ' ')" \
    >>"$FAKE_DOCKER_LOG"
  rewritten=()
  index=0
  while [ "$index" -lt "${#rest[@]}" ]; do
    if [ "${rest[$index]}" = "--source-root" ]; then
      rewritten+=("--source-root" "$FAKE_ATTESTATION_SOURCE")
      index=$((index + 2))
    else
      rewritten+=("${rest[$index]}")
      index=$((index + 1))
    fi
  done
  python3 -c "$script" "${rewritten[@]}" <"$stdin_file"
  exit $?
fi

printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"
if [ "${1-}" = compose ] && [ "${2-}" = version ]; then
  exit 0
fi
if [ "${1-}" = image ] && [ "${2-}" = ls ]; then
  printf '%s\n' "$(printf 'a%.0s' {1..64})"
  exit 0
fi
if [ "${1-}" = image ] && [ "${2-}" = inspect ]; then
  if [ "${FAKE_ATTESTATION_PLATFORM_INSPECT_FAIL:-0}" = "1" ] && [[ "$*" == *'--platform '* ]]; then
    exit 9
  fi
  case "$*" in
    *'{{.Os}}/{{.Architecture}}'*) printf 'linux/amd64\n' ;;
    *'{{.Created}}'*) printf '2026-11-02T03:00:00Z sha256:%s\n' "$(printf 'a%.0s' {1..64})" ;;
    *fixture-tag) printf 'sha256:%s\n' "$(printf 'a%.0s' {1..64})" ;;
    *) printf 'old OCI index is no longer addressable\n' >&2; exit 9 ;;
  esac
  exit 0
fi
if [ "${1-}" = inspect ]; then
  case "$*" in
    *'{{if .State.Health}}'*) printf 'healthy\n' ;;
    *'{{.Image}}'*) printf '%s\n' "${FAKE_ATTESTATION_RUNNING_IMAGE_ID:-sha256:$(printf 'a%.0s' {1..64})}" ;;
    *'{{.ImageManifestDescriptor.Digest}}'*) printf '%s\n' "${FAKE_ATTESTATION_RUNNING_PLATFORM_IMAGE_ID:-${FAKE_ATTESTATION_RUNNING_IMAGE_ID:-sha256:$(printf 'a%.0s' {1..64})}}" ;;
    *'{{.State.StartedAt}}'*) date -u '+%Y-%m-%dT%H:%M:%SZ' ;;
    *'{{range .Mounts}}'*)
      if [ "${FAKE_ATTESTATION_MOUNT_INSPECT_FAIL:-0}" = "1" ]; then
        exit 9
      fi
      if [ -n "${FAKE_ATTESTATION_MOUNT_DESTINATION:-}" ]; then
        printf '%s\n' "$FAKE_ATTESTATION_MOUNT_DESTINATION"
      fi
      ;;
    *) echo "fake docker inspect received an unexpected format" >&2; exit 9 ;;
  esac
  exit 0
fi
case "$*" in
  *' build --print backend')
    printf '%s\n' '{'
    printf '%s\n' '  "target": {'
    printf '%s\n' '    "backend": {'
    printf '%s\n' '      "tags": ['
    printf '%s\n' '        "fixture-tag"'
    printf '%s\n' '      ]'
    printf '%s\n' '    }'
    printf '%s\n' '  }'
    printf '%s\n' '}'
    ;;
  *' up --help') printf '%s\n' '      --wait' ;;
  *' alembic heads') printf '%s\n' '0055_projection_readiness (head)' ;;
  *' up -d --remove-orphans')
    if [ "${FAKE_FINAL_UP_FAIL:-0}" = "1" ]; then
      exit 42
    fi
    ;;
  *'--profile hindsight'*'up -d hindsight'*)
    if [ "${FAKE_HINDSIGHT_UP_FAIL:-0}" = "1" ]; then
      printf 'fake hindsight start failure (pull/init/db/start)\n' >&2
      exit 18
    fi
    ;;
  *'--profile multica'*'up -d multica'*)
    if [ "${FAKE_MULTICA_UP_FAIL:-0}" = "1" ]; then
      printf 'fake multica start failure (pull/start)\n' >&2
      exit 18
    fi
    ;;
  *' ps -q pipeline_executor') printf '%s\n' "$(printf 'p%.0s' {1..64})" ;;
  *' ps -q integrated_replica_executor') printf '%s\n' "$(printf 'r%.0s' {1..64})" ;;
  *' ps -q backend') printf '%s\n' "$(printf 'c%.0s' {1..64})" ;;
  *' ps -q hindsight')
    if [ "${FAKE_HINDSIGHT_NEVER_HEALTHY:-0}" != "1" ]; then
      printf '%s\n' "$(printf 'h%.0s' {1..64})"
    fi
    ;;
  *' ps -q multica')
    printf '%s\n' "$(printf 'm%.0s' {1..64})"
    ;;
  *' ps --services --filter status=running') : ;;
  *) : ;;
esac
exit 0
FAKE_DOCKER_SUCCESS
cat >"$attestation_fake_bin/timeout" <<'FAKE_TIMEOUT'
#!/usr/bin/env bash
set -Eeuo pipefail
seconds="$1"
shift
if [ "${FAKE_TIMEOUT_TRIGGER:-0}" = "1" ] \
    || { [ -n "${FAKE_TIMEOUT_TRIGGER_MATCH:-}" ] \
      && printf '%s' "$*" | grep -q -- "$FAKE_TIMEOUT_TRIGGER_MATCH"; }; then
  sleep "$seconds"
  exit 124
fi
exec "$@"
FAKE_TIMEOUT
chmod +x "$attestation_fake_bin/curl" "$attestation_fake_bin/docker" "$attestation_fake_bin/timeout"

kaneo_web_url_fixture="$(awk -F= '$1 == "KANEO_WEB_URL" {print substr($0, index($0,"=")+1)}' "$test_dir/.env" | tail -n1)"

run_attestation_deploy() {
  local log="$1" source="$2" timeout="$3"
  shift 3
  (
    cd "$test_dir"
    env \
      PATH="$attestation_fake_bin:$PATH" \
      FAKE_DOCKER_LOG="$log.docker" \
      FAKE_ATTESTATION_SOURCE="$source" \
      FAKE_KANEO_WEB_URL="$kaneo_web_url_fixture" \
      APP_DIR="$test_dir" \
      SKIP_GIT=1 \
      EXPECTED_RUNTIME_COMMIT="$attestation_commit" \
      HEALTH_URL="http://127.0.0.1:65535/" \
      RUNTIME_SOURCE_ATTESTATION_TIMEOUT_SECONDS="$timeout" \
      DEPLOY_RETRIES=1 \
      DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
      "$@" \
      bash "$attestation_deploy_dir/deploy-prod.sh"
  ) >"$log" 2>&1
}

success_log="$attestation_root/deploy-success.log"
if ! run_attestation_deploy "$success_log" "$attestation_source" 5; then
  printf 'mock deployment with matching runtime source must succeed; log follows\n' >&2
  cat "$success_log" >&2
  if [ -f "$success_log.docker" ]; then
    printf 'mock docker log follows\n' >&2
    cat "$success_log.docker" >&2
  fi
  exit 1
fi
grep -Fq 'deployment succeeded' "$success_log"
grep -Fq 'runtime source attestation: status=ok scope=running_container_files_not_loaded_memory' "$success_log"
grep -Fq "container_id=$(printf 'c%.0s' {1..64})" "$success_log"
grep -Fq "image_id=sha256:$(printf 'a%.0s' {1..64})" "$success_log"
grep -Fq 'runtime source attestation mount_check=app_source_not_mounted image_matches_build=true' "$success_log"
grep -Fq 'compose exec -T backend python -c <verifier>' "$success_log.docker"
grep -Eq 'exec-stdin-bytes=[1-9][0-9]*' "$success_log.docker"
if grep -Fq 'synthetic-admin-password' "$success_log"; then
  printf 'deployment source attestation leaked the synthetic secret sentinel\n' >&2
  exit 1
fi
grep -Fq 'running Kaneo bootstrap: --verify' "$success_log"
if grep -Fq "$kaneo_admin_password" "$success_log"; then
  printf 'deployment success log leaked the generated Kaneo admin password\n' >&2
  exit 1
fi
printf 'ok deploy-runtime-source-match\n'

index_only_log="$attestation_root/deploy-index-only-change.log"
if ! run_attestation_deploy "$index_only_log" "$attestation_source" 5 \
    FAKE_ATTESTATION_RUNNING_IMAGE_ID="sha256:$(printf 'b%.0s' {1..64})" \
    FAKE_ATTESTATION_RUNNING_PLATFORM_IMAGE_ID="sha256:$(printf 'a%.0s' {1..64})"; then
  printf 'deployment must accept a changed OCI index when the runnable platform image matches\n' >&2
  exit 1
fi
grep -Fq 'image_matches_build=true' "$index_only_log"

platform_inspect_log="$attestation_root/deploy-platform-inspect-failure.log"
if run_attestation_deploy "$platform_inspect_log" "$attestation_source" 5 \
    FAKE_ATTESTATION_PLATFORM_INSPECT_FAIL=1; then
  printf 'deployment must fail when platform image inspection fails\n' >&2
  exit 1
fi
if grep -Fq 'deployment succeeded' "$platform_inspect_log"; then
  printf 'deployment must not succeed after platform image inspection fails\n' >&2
  exit 1
fi
descriptor_missing_log="$attestation_root/deploy-platform-descriptor-missing.log"
if run_attestation_deploy "$descriptor_missing_log" "$attestation_source" 5 \
    FAKE_ATTESTATION_RUNNING_PLATFORM_IMAGE_ID=unavailable; then
  printf 'deployment must fail when the running platform descriptor is unavailable\n' >&2
  exit 1
fi
if grep -Fq 'deployment succeeded' "$descriptor_missing_log"; then
  printf 'deployment must not succeed without a running platform descriptor\n' >&2
  exit 1
fi
printf 'ok deploy-platform-image-identity\n'

tampered_source="$attestation_root/deploy-tampered-source"
cp -R "$attestation_repo/backend/app" "$tampered_source"
printf 'tampered\n' >>"$tampered_source/main.py"
mismatch_log="$attestation_root/deploy-mismatch.log"
if run_attestation_deploy "$mismatch_log" "$tampered_source" 5; then
  printf 'deployment must fail when runtime source attestation detects a mismatch\n' >&2
  exit 1
fi
grep -Fq 'runtime source attestation: status=mismatch' "$mismatch_log"
grep -Fq 'changed_path=backend/app/main.py' "$mismatch_log"
grep -Fq 'runtime source attestation failed; refusing to report deployment success' "$mismatch_log"
grep -Fq 'stopping the partially started new runtime after a post-migration failure' "$mismatch_log"
if grep -Fq 'deployment succeeded' "$mismatch_log"; then
  printf 'deployment must not log success after a runtime source mismatch\n' >&2
  exit 1
fi
grep -Fq 'stop --timeout 30 frontend pipeline_executor backend' "$mismatch_log.docker"

image_mismatch_log="$attestation_root/deploy-image-mismatch.log"
if run_attestation_deploy "$image_mismatch_log" "$attestation_source" 5 \
    FAKE_ATTESTATION_RUNNING_IMAGE_ID="sha256:$(printf 'b%.0s' {1..64})"; then
  printf 'deployment must fail when the active container image differs from the built image\n' >&2
  exit 1
fi
grep -Fq 'does not match the image built by this deploy' "$image_mismatch_log"
if grep -Fq 'deployment succeeded' "$image_mismatch_log"; then
  printf 'deployment must not log success after an active-container image mismatch\n' >&2
  exit 1
fi

wrong_commit_log="$attestation_root/deploy-wrong-commit.log"
if run_attestation_deploy "$wrong_commit_log" "$attestation_source" 5 \
    EXPECTED_RUNTIME_COMMIT=0000000000000000000000000000000000000000; then
  printf 'deployment must fail when the independent expected commit does not match the manifest\n' >&2
  exit 1
fi
grep -Fq 'does not match expected commit' "$wrong_commit_log"
if grep -Fq 'deployment succeeded' "$wrong_commit_log"; then
  printf 'deployment must not log success after an expected-commit mismatch\n' >&2
  exit 1
fi
printf 'ok deploy-runtime-source-mismatch-fail\n'

mount_log="$attestation_root/deploy-mount.log"
if run_attestation_deploy "$mount_log" "$attestation_source" 5 \
    FAKE_ATTESTATION_MOUNT_DESTINATION=/; then
  printf 'deployment must fail when a mount covers the backend source path\n' >&2
  exit 1
fi
grep -Fq 'backend source path is covered by a container mount: /' "$mount_log"
if grep -Fq 'deployment succeeded' "$mount_log"; then
  printf 'deployment must not log success when the backend source path is mounted\n' >&2
  exit 1
fi

mount_inspect_log="$attestation_root/deploy-mount-inspect-failure.log"
if run_attestation_deploy "$mount_inspect_log" "$attestation_source" 5 \
    FAKE_ATTESTATION_MOUNT_INSPECT_FAIL=1; then
  printf 'deployment must fail when backend mount inspection fails\n' >&2
  exit 1
fi
grep -Fq 'could not inspect backend container mounts' "$mount_inspect_log"
if grep -Fq 'deployment succeeded' "$mount_inspect_log"; then
  printf 'deployment must not log success after mount inspection failure\n' >&2
  exit 1
fi
printf 'ok deploy-runtime-source-mount-fail\n'

exec_failure_log="$attestation_root/deploy-exec-failure.log"
if run_attestation_deploy "$exec_failure_log" "$attestation_source" 5 \
    FAKE_ATTESTATION_EXEC_FAIL=1; then
  printf 'deployment must fail when the in-container verifier exec fails\n' >&2
  exit 1
fi
grep -Fq 'runtime source attestation failed; refusing to report deployment success' "$exec_failure_log"
if grep -Fq 'deployment succeeded' "$exec_failure_log"; then
  printf 'deployment must not log success after a verifier exec failure\n' >&2
  exit 1
fi
printf 'ok deploy-runtime-source-exec-fail\n'

timeout_log="$attestation_root/deploy-timeout.log"
if run_attestation_deploy "$timeout_log" "$attestation_source" 1 \
    FAKE_TIMEOUT_TRIGGER=1; then
  printf 'deployment must fail when runtime source attestation times out\n' >&2
  exit 1
fi
grep -Fq 'runtime source attestation timed out after 1s' "$timeout_log"
if grep -Fq 'deployment succeeded' "$timeout_log"; then
  printf 'deployment must not log success after an attestation timeout\n' >&2
  exit 1
fi
printf 'ok deploy-runtime-source-timeout-fail\n'

# Hindsight memory stack isolation through the real deploy script: the memory
# stack now deploys on by default, including a one-time migration for legacy
# server .env files that still carry the historical false default. The fixture
# below simulates exactly such a server (false, no HINDSIGHT_WEB_PORT marker).
# Enabled startup happens only after core success in a separate --profile
# hindsight invocation; optional pull, init, database, app-start, timeout, or
# health failures never gate or stop the core deployment, and the final core
# compose up never carries the profile.
set_test_env_value HINDSIGHT_ENABLED false
set_test_env_value HINDSIGHT_WEB_URL ""
sed -i.bak '/^[[:space:]]*HINDSIGHT_WEB_PORT=/d' "$test_dir/.env"
rm -f "$test_dir/.env.bak"

hindsight_success_log="$attestation_root/deploy-hindsight-success.log"
if ! run_attestation_deploy "$hindsight_success_log" "$attestation_source" 5 \
    HINDSIGHT_HEALTH_WAIT_ATTEMPTS=2 HINDSIGHT_HEALTH_WAIT_INTERVAL=0; then
  printf 'mock deployment with the optional memory stack enabled must succeed; log follows\n' >&2
  cat "$hindsight_success_log" >&2
  if [ -f "$hindsight_success_log.docker" ]; then
    printf 'mock docker log follows\n' >&2
    cat "$hindsight_success_log.docker" >&2
  fi
  exit 1
fi
grep -Fq 'deployment succeeded' "$hindsight_success_log"
grep -Fq 'hindsight memory service is healthy (feature status only)' "$hindsight_success_log"
hindsight_core_up_line="$(
  grep -nF 'up -d --remove-orphans' "$hindsight_success_log.docker" \
    | tail -n1 | cut -d: -f1
)"
hindsight_optional_up_line="$(
  grep -nF 'up -d hindsight' "$hindsight_success_log.docker" \
    | tail -n1 | cut -d: -f1
)"
if [ -z "$hindsight_core_up_line" ] || [ -z "$hindsight_optional_up_line" ] \
    || [ "$hindsight_optional_up_line" -le "$hindsight_core_up_line" ]; then
  printf 'the optional Hindsight stack must start after the final core compose up\n' >&2
  exit 1
fi
if sed -n "${hindsight_core_up_line}p" "$hindsight_success_log.docker" \
    | grep -q -- '--profile hindsight'; then
  printf 'the final core compose up must not carry the hindsight profile\n' >&2
  exit 1
fi
if ! sed -n "${hindsight_optional_up_line}p" "$hindsight_success_log.docker" \
    | grep -q -- '--profile hindsight'; then
  printf 'the optional Hindsight start must be a separate --profile hindsight invocation\n' >&2
  exit 1
fi
grep -q -- '--profile hindsight ps -q hindsight' "$hindsight_success_log.docker"
if grep -q -- '--profile hindsight stop' "$hindsight_success_log.docker"; then
  printf 'a successful optional Hindsight start must not stop the memory stack\n' >&2
  exit 1
fi
hindsight_api_key_value="$(
  awk -F= '$1 == "HINDSIGHT_API_KEY" {print substr($0, index($0,"=")+1)}' \
    "$test_dir/.env" | tail -n1
)"
if [ -n "$hindsight_api_key_value" ] \
    && grep -Fq "$hindsight_api_key_value" "$hindsight_success_log"; then
  printf 'generated Hindsight API key leaked into deployment logs\n' >&2
  exit 1
fi
# 默认控制面入口：HINDSIGHT_ENABLED=true 且未显式配置时，入口 URL 从
# PUBLIC_TLS_IP + HINDSIGHT_WEB_PORT 推导并落 .env，控制面密钥自动生成且
# 不进部署日志（fixture 的 PUBLIC_TLS_IP 为 64.83.14.139）。同时验证存量
# false 服务器的一次性迁移确实发生并落盘。
grep -Fq 'migrated HINDSIGHT_ENABLED=true' "$hindsight_success_log"
grep -Fxq 'HINDSIGHT_ENABLED=true' "$test_dir/.env"
grep -Fxq 'HINDSIGHT_WEB_PORT=19999' "$test_dir/.env"
grep -Fq 'configured HINDSIGHT_WEB_URL from PUBLIC_TLS_IP and HINDSIGHT_WEB_PORT' \
    "$hindsight_success_log"
grep -Fxq 'HINDSIGHT_WEB_URL=http://64.83.14.139:19999' "$test_dir/.env"
grep -Eq '^HINDSIGHT_CP_ACCESS_KEY=[0-9a-f]{16}$' "$test_dir/.env"
hindsight_cp_key_value="$(
    awk -F= '$1 == "HINDSIGHT_CP_ACCESS_KEY" {print substr($0, index($0,"=")+1)}' \
    "$test_dir/.env" | tail -n1
)"
if [ -n "$hindsight_cp_key_value" ] \
    && grep -Fq "$hindsight_cp_key_value" "$hindsight_success_log"; then
  printf 'generated Hindsight control-plane access key leaked into deployment logs\n' >&2
  exit 1
fi
printf 'ok deploy-hindsight-optional-start-success\n'

hindsight_start_fail_log="$attestation_root/deploy-hindsight-start-failure.log"
if ! run_attestation_deploy "$hindsight_start_fail_log" "$attestation_source" 5 \
    HINDSIGHT_HEALTH_WAIT_ATTEMPTS=2 HINDSIGHT_HEALTH_WAIT_INTERVAL=0 \
    FAKE_HINDSIGHT_UP_FAIL=1; then
  printf 'deployment must still succeed when the optional Hindsight start fails\n' >&2
  exit 1
fi
grep -Fq 'deployment succeeded' "$hindsight_start_fail_log"
grep -Fq 'the optional Hindsight memory stack failed to start (exit 18' \
  "$hindsight_start_fail_log"
grep -q -- '--profile hindsight stop hindsight hindsight-db hindsight-model-init' \
  "$hindsight_start_fail_log.docker"
if grep -q -- 'stop --timeout 30 frontend pipeline_executor backend' \
    "$hindsight_start_fail_log.docker"; then
  printf 'an optional Hindsight start failure must never stop the core runtime\n' >&2
  exit 1
fi
printf 'ok deploy-hindsight-optional-start-failure\n'

hindsight_start_timeout_log="$attestation_root/deploy-hindsight-start-timeout.log"
# 仅对启动 up 触发假超时：清理 stop 同样被 timeout 包裹，宽匹配会在它到达
# docker 前就 124 掉，导致 docker 日志缺少 stop 记录。
if ! run_attestation_deploy "$hindsight_start_timeout_log" "$attestation_source" 5 \
    HINDSIGHT_START_TIMEOUT=1 \
    HINDSIGHT_HEALTH_WAIT_ATTEMPTS=2 HINDSIGHT_HEALTH_WAIT_INTERVAL=0 \
    FAKE_TIMEOUT_TRIGGER_MATCH='--profile hindsight up -d hindsight'; then
  printf 'deployment must still succeed when the optional Hindsight start times out\n' >&2
  exit 1
fi
grep -Fq 'deployment succeeded' "$hindsight_start_timeout_log"
grep -Fq 'did not start within 1s' "$hindsight_start_timeout_log"
grep -q -- '--profile hindsight stop hindsight hindsight-db hindsight-model-init' \
  "$hindsight_start_timeout_log.docker"
printf 'ok deploy-hindsight-optional-start-timeout\n'

hindsight_unhealthy_log="$attestation_root/deploy-hindsight-unhealthy.log"
if ! run_attestation_deploy "$hindsight_unhealthy_log" "$attestation_source" 5 \
    HINDSIGHT_HEALTH_WAIT_ATTEMPTS=2 HINDSIGHT_HEALTH_WAIT_INTERVAL=0 \
    FAKE_HINDSIGHT_NEVER_HEALTHY=1; then
  printf 'deployment must still succeed when the optional Hindsight stack stays unhealthy\n' >&2
  exit 1
fi
grep -Fq 'deployment succeeded' "$hindsight_unhealthy_log"
grep -Fq 'hindsight is not healthy yet' "$hindsight_unhealthy_log"
if grep -q -- '--profile hindsight stop' "$hindsight_unhealthy_log.docker"; then
  printf 'a started-but-unhealthy optional Hindsight stack must be left running\n' >&2
  exit 1
fi
printf 'ok deploy-hindsight-optional-unhealthy-degrades\n'

hindsight_core_fail_log="$attestation_root/deploy-hindsight-core-failure.log"
# 同时验证运维显式关闭：.env 已带 HINDSIGHT_WEB_PORT 指纹，写 false 即为
# 停用表态，部署必须尊重它，绝不能被一次性迁移翻回启用。
set_test_env_value HINDSIGHT_ENABLED false
if run_attestation_deploy "$hindsight_core_fail_log" "$attestation_source" 5 \
    HINDSIGHT_HEALTH_WAIT_ATTEMPTS=2 HINDSIGHT_HEALTH_WAIT_INTERVAL=0 \
    FAKE_FINAL_UP_FAIL=1; then
  printf 'a failing final core compose up must fail the deployment\n' >&2
  exit 1
fi
if grep -Fq 'deployment succeeded' "$hindsight_core_fail_log"; then
  printf 'deployment must not claim success after the final core compose up fails\n' >&2
  exit 1
fi
if grep -q -- '--profile hindsight up' "$hindsight_core_fail_log.docker"; then
  printf 'no compose invocation may start the hindsight profile before core deployment success\n' >&2
  exit 1
fi
# 显式 false（且已带 HINDSIGHT_WEB_PORT 指纹）是运维的停用表态：部署必须
# 尊重它，绝不能被一次性迁移翻回启用。
grep -Fxq 'HINDSIGHT_ENABLED=false' "$test_dir/.env"
if grep -Fq 'migrated HINDSIGHT_ENABLED=true' "$hindsight_core_fail_log"; then
  printf 'an explicit HINDSIGHT_ENABLED=false must not be migrated back on\n' >&2
  exit 1
fi
printf 'ok deploy-hindsight-explicit-disable-respected\n'
printf 'ok deploy-hindsight-core-final-isolation\n'

# Multica default service: the bundled stack is default-enabled (empty/true
# both enable, only an explicit false disables), the five-value default is
# optional but all-or-nothing (partial sets refused in both states), floating
# image references and non-IP bind addresses are refused, the isolated start
# runs between the final core up and the readiness gate (no self-deadlock),
# and its failure never rolls back the succeeded core.
set_test_env_value MULTICA_ENABLED maybe
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/multica-bad-toggle.log" 2>&1; then
  printf 'a non-boolean MULTICA_ENABLED must be rejected\n' >&2
  exit 1
fi
grep -q 'MULTICA_ENABLED must be true or false' "$test_dir/multica-bad-toggle.log"

# Default-on: an empty toggle enables the stack and validation succeeds with
# an empty five-value set (the stack runs; account/PAT/workspace are honest
# manual steps, not deployment prerequisites). configure writes the pins, the
# loopback bind default, and generated stack secrets; nothing is derived.
set_test_env_value MULTICA_ENABLED ""
set_test_env_value MULTICA_WEB_BIND_ADDR ""
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/multica-default-on.log" 2>&1; then
  printf 'ok deploy-multica-default-on-validate (stack enabled with an empty five-value set)\n'
else
  printf 'the default-enabled Multica stack must validate with an empty five-value set; log follows\n' >&2
  cat "$test_dir/multica-default-on.log" >&2
  exit 1
fi
grep -Fq 'Multica web UI is bound to loopback only' "$test_dir/multica-default-on.log"
grep -Fxq 'MULTICA_ENABLED=true' "$test_dir/.env"
grep -Fxq 'MULTICA_WEB_BIND_ADDR=127.0.0.1' "$test_dir/.env"
grep -Fxq 'MULTICA_WEB_PORT=18100' "$test_dir/.env"
grep -Fxq \
  'MULTICA_BACKEND_IMAGE=ghcr.io/multica-ai/multica-backend@sha256:099fd0d1eea8fc4cf03254c6dff86f7de206662ff71db8dc9a78e66726dd65a4' \
  "$test_dir/.env"
grep -Fxq \
  'MULTICA_WEB_IMAGE=ghcr.io/multica-ai/multica-web@sha256:ba8fd0fda94a9ec141e15d77e591db8ee4b246720a722d6f9dffd185ee48863b' \
  "$test_dir/.env"
grep -Fxq \
  'MULTICA_POSTGRES_IMAGE=pgvector/pgvector:pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f' \
  "$test_dir/.env"
multica_jwt_secret="$(awk -F= '$1 == "MULTICA_JWT_SECRET" {print substr($0, index($0,"=")+1)}' "$test_dir/.env" | tail -n1)"
if ! printf '%s' "$multica_jwt_secret" | grep -Eq '^[0-9a-f]{64}$'; then
  printf 'generated Multica JWT secret must be exactly 64 hex characters\n' >&2
  exit 1
fi
multica_db_password="$(awk -F= '$1 == "MULTICA_POSTGRES_PASSWORD" {print substr($0, index($0,"=")+1)}' "$test_dir/.env" | tail -n1)"
if ! printf '%s' "$multica_db_password" | grep -Eq '^[0-9a-f]{48}$'; then
  printf 'generated Multica DB password must be exactly 48 hex characters\n' >&2
  exit 1
fi
if grep -q '^MULTICA_DEFAULT_' "$test_dir/.env" \
    && grep -Eq '^MULTICA_DEFAULT_[A-Z_]+=.+' "$test_dir/.env"; then
  printf 'an empty five-value set must stay empty (nothing is derived without the native-UI values)\n' >&2
  exit 1
fi

# configure_multica_env writes the verified official digest pins itself; an
# operator override that is not digest-pinned must still be rejected.
set_test_env_value MULTICA_BACKEND_IMAGE example.invalid/unvalidated:latest
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/multica-floating-image.log" 2>&1; then
  printf 'a floating Multica image override must be rejected even though the default pins exist\n' >&2
  exit 1
fi
grep -q 'MULTICA_BACKEND_IMAGE must be pinned to an immutable @sha256 digest' \
  "$test_dir/multica-floating-image.log"
set_test_env_value MULTICA_BACKEND_IMAGE ""

# The web bind address must be a plain IP literal: hostnames, host:port and
# schemes would silently change the published-interface contract.
set_test_env_value MULTICA_WEB_BIND_ADDR multica.example.invalid
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/multica-bad-bind-addr.log" 2>&1; then
  printf 'a non-IP MULTICA_WEB_BIND_ADDR must be rejected\n' >&2
  exit 1
fi
grep -q 'MULTICA_WEB_BIND_ADDR must be a plain IPv4 or IPv6 address' \
  "$test_dir/multica-bad-bind-addr.log"
set_test_env_value MULTICA_WEB_BIND_ADDR ""

# The five-value default is all-or-nothing in BOTH states (enabled or
# explicitly disabled): a partial set is a deployment error either way.
set_test_env_value MULTICA_DEFAULT_PAT synthetic-multica-default-pat
set_test_env_value MULTICA_DEFAULT_WORKSPACE_ID 00000000-0000-0000-0000-000000000001
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/multica-partial-enabled.log" 2>&1; then
  printf 'a partial (enabled) Multica default set must be rejected\n' >&2
  exit 1
fi
grep -q 'MULTICA_DEFAULT_\* must be all set or all empty' \
  "$test_dir/multica-partial-enabled.log"
# Four-of-five with WEB_URL missing: configure derives the internal base URL
# and prints the explicit-WEB_URL hint, then validation still refuses the
# incomplete set (the deployment never guesses a browser origin).
set_test_env_value MULTICA_DEFAULT_WORKSPACE_NAME "OpenOntology 默认工作台"
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/multica-missing-web-url.log" 2>&1; then
  printf 'a four-of-five Multica default set (WEB_URL missing) must be rejected\n' >&2
  exit 1
fi
grep -Fq 'MULTICA_DEFAULT_WEB_URL is not set while the rest of the five-value default is present' \
  "$test_dir/multica-missing-web-url.log"
grep -Fq 'configured MULTICA_DEFAULT_BASE_URL from the bundled multica service' \
  "$test_dir/multica-missing-web-url.log"
grep -q 'MULTICA_DEFAULT_\* must be all set or all empty (found 4/5)' \
  "$test_dir/multica-missing-web-url.log"
set_test_env_value MULTICA_DEFAULT_WORKSPACE_NAME ""
set_test_env_value MULTICA_DEFAULT_PAT ""
set_test_env_value MULTICA_DEFAULT_WORKSPACE_ID ""
grep -Fxq 'MULTICA_DEFAULT_BASE_URL=http://multica:8080' "$test_dir/.env" \
  || {
    printf 'configure must persist the derived internal base URL before validation rejects the four-of-five set\n' >&2
    exit 1
  }
set_test_env_value MULTICA_DEFAULT_BASE_URL ""
set_test_env_value MULTICA_ENABLED false
# Re-establish the partial set for the disabled-state check: all-or-nothing
# must hold with the stack disabled too (an external-Multica half
# configuration is still a deployment error).
set_test_env_value MULTICA_DEFAULT_PAT synthetic-multica-default-pat
set_test_env_value MULTICA_DEFAULT_WORKSPACE_ID 00000000-0000-0000-0000-000000000001
if (
  cd "$test_dir"
  env -u STRICT_IMAGE_DIGESTS \
    APP_DIR="$test_dir" \
    SKIP_GIT=1 \
    DEPLOY_VALIDATE_ONLY=1 \
    DEPENDENCY_CONFIG_FILE=test-production-dependencies.env \
    bash "$DEPLOY_SCRIPT"
) >"$test_dir/multica-partial-disabled.log" 2>&1; then
  printf 'a partial (disabled) Multica default set must be rejected\n' >&2
  exit 1
fi
grep -q 'MULTICA_DEFAULT_\* must be all set or all empty' \
  "$test_dir/multica-partial-disabled.log"
set_test_env_value MULTICA_DEFAULT_PAT ""
set_test_env_value MULTICA_DEFAULT_WORKSPACE_ID ""

# An explicit false is the operator's opt-out: the deployment succeeds, the
# canonical false is persisted, leftover multica containers are stopped, and
# the profile start is never invoked.
multica_disabled_log="$attestation_root/deploy-multica-disabled.log"
if ! run_attestation_deploy "$multica_disabled_log" "$attestation_source" 5; then
  printf 'mock deployment with the Multica stack explicitly disabled must succeed; log follows\n' >&2
  cat "$multica_disabled_log" >&2
  if [ -f "$multica_disabled_log.docker" ]; then
    printf 'mock docker log follows\n' >&2
    cat "$multica_disabled_log.docker" >&2
  fi
  exit 1
fi
grep -Fq 'deployment succeeded' "$multica_disabled_log"
grep -Fq 'Multica default service profile is disabled' "$multica_disabled_log"
grep -Fxq 'MULTICA_ENABLED=false' "$test_dir/.env"
grep -q -- '--profile multica stop multica' "$multica_disabled_log.docker"
if grep -q -- '--profile multica up' "$multica_disabled_log.docker"; then
  printf 'an explicitly disabled Multica stack must never be started\n' >&2
  exit 1
fi
printf 'ok deploy-multica-explicit-disable-respected\n'

# Enabled with the complete five-value default: the stack starts between the
# final core up and the readiness gate, the internal base URL and the
# independent allowlist are derived, and the PAT never reaches the logs.
set_test_env_value MULTICA_ENABLED true
set_test_env_value MULTICA_DEFAULT_BASE_URL ""
set_test_env_value MULTICA_DEFAULT_WEB_URL https://multica.example.invalid
set_test_env_value MULTICA_DEFAULT_WORKSPACE_ID 00000000-0000-0000-0000-000000000001
set_test_env_value MULTICA_DEFAULT_WORKSPACE_NAME "OpenOntology 默认工作台"
set_test_env_value MULTICA_DEFAULT_PAT synthetic-multica-default-pat
set_test_env_value SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS ""

multica_success_log="$attestation_root/deploy-multica-success.log"
if ! run_attestation_deploy "$multica_success_log" "$attestation_source" 5; then
  printf 'mock deployment with the default-enabled Multica stack and a complete five-value default must succeed; log follows\n' >&2
  cat "$multica_success_log" >&2
  if [ -f "$multica_success_log.docker" ]; then
    printf 'mock docker log follows\n' >&2
    cat "$multica_success_log.docker" >&2
  fi
  exit 1
fi
grep -Fq 'deployment succeeded' "$multica_success_log"
grep -Fq 'multica default stack started' "$multica_success_log"
grep -Fq 'the official backend /healthz check is healthy' "$multica_success_log"
multica_start_msg_line="$(
  grep -nF 'starting the Multica default stack' "$multica_success_log" \
    | head -n1 | cut -d: -f1
)"
multica_readiness_msg_line="$(
  grep -nF 'waiting for backend, pipeline executor, replica executor and frontend readiness' \
    "$multica_success_log" | head -n1 | cut -d: -f1
)"
if [ -z "$multica_start_msg_line" ] || [ -z "$multica_readiness_msg_line" ] \
    || [ "$multica_start_msg_line" -ge "$multica_readiness_msg_line" ]; then
  printf 'the Multica start message must precede the readiness gate wait (no self-deadlock)\n' >&2
  exit 1
fi
# bundled 默认地址与独立允许清单按官方 8080 契约推导落 .env；digest pin、
# loopback 绑定与自动生成的栈密钥一并持久化。
grep -Fxq 'MULTICA_DEFAULT_BASE_URL=http://multica:8080' "$test_dir/.env"
grep -Fxq \
  'SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS=http://multica:8080' \
  "$test_dir/.env"
grep -Fxq 'MULTICA_DEFAULT_WEB_URL=https://multica.example.invalid' "$test_dir/.env"
multica_core_up_line="$(
  grep -nF 'up -d --remove-orphans' "$multica_success_log.docker" \
    | tail -n1 | cut -d: -f1
)"
multica_optional_up_line="$(
  grep -nF -- '--profile multica up -d multica' \
    "$multica_success_log.docker" | tail -n1 | cut -d: -f1
)"
if [ -z "$multica_core_up_line" ] || [ -z "$multica_optional_up_line" ] \
    || [ "$multica_optional_up_line" -le "$multica_core_up_line" ]; then
  printf 'the Multica stack must start after the final core compose up\n' >&2
  exit 1
fi
if sed -n "${multica_core_up_line}p" "$multica_success_log.docker" \
    | grep -q -- '--profile multica'; then
  printf 'the final core compose up must not carry the multica profile\n' >&2
  exit 1
fi
if grep -Fq 'synthetic-multica-default-pat' "$multica_success_log"; then
  printf 'the Multica default PAT leaked into deployment logs\n' >&2
  exit 1
fi
printf 'ok deploy-multica-default-start-success\n'

# With no five-value default configured (the honest fresh-install state), a
# failed stack start only degrades the feature: the succeeded core deployment
# is never rolled back. (With the five-value default configured, the real
# readiness gate would report the deployment as failed — that is backend
# behavior covered by the readiness tests, not reproducible through the fake
# readiness curl here.)
set_test_env_value MULTICA_DEFAULT_BASE_URL ""
set_test_env_value MULTICA_DEFAULT_WEB_URL ""
set_test_env_value MULTICA_DEFAULT_WORKSPACE_ID ""
set_test_env_value MULTICA_DEFAULT_WORKSPACE_NAME ""
set_test_env_value MULTICA_DEFAULT_PAT ""
set_test_env_value SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS ""
multica_start_fail_log="$attestation_root/deploy-multica-start-failure.log"
if ! run_attestation_deploy "$multica_start_fail_log" "$attestation_source" 5 \
    FAKE_MULTICA_UP_FAIL=1; then
  printf 'deployment must still succeed when the unconfigured Multica start fails\n' >&2
  exit 1
fi
grep -Fq 'deployment succeeded' "$multica_start_fail_log"
grep -Fq 'the Multica default stack failed to start (exit 18' \
  "$multica_start_fail_log"
grep -q -- '--profile multica stop multica' "$multica_start_fail_log.docker"
if grep -q -- 'stop --timeout 30 frontend pipeline_executor backend' \
    "$multica_start_fail_log.docker"; then
  printf 'a Multica start failure must never stop the core runtime\n' >&2
  exit 1
fi
printf 'ok deploy-multica-start-failure-not-fatal\n'

# Restore the shipped default so later readers of the fixture .env see the
# honest state (enabled stack, empty five-value default).
set_test_env_value MULTICA_ENABLED true

printf 'deployment guard self-tests passed\n'
