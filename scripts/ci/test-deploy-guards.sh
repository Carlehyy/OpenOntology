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
readiness_line="$(grep -nF 'log "waiting for backend, pipeline executor and frontend readiness' "$DEPLOY_SCRIPT" | head -n1 | cut -d: -f1)"
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
      'if ! compose stop --timeout 30 frontend pipeline_executor backend; then' \
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
    || ! grep -Fq 'run_weknora_bootstrap --verify' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'python -m app.super_assistant.weknora.bootstrap "$@"' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'compose --profile weknora-bootstrap build --pull weknora-bootstrap' "$DEPLOY_SCRIPT" \
    || ! grep -Fq 'check_weknora_public_entry' "$DEPLOY_SCRIPT"; then
  printf 'WeKnora app/MCP must be private, formal registration closed, init profile-gated, and only the UI gateway published\n' >&2
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
  || ! grep -Fq 'run: docker compose -f docker-compose.prod.yml build' <<<"$build_step" \
  || grep -Eq 'secrets\.|build-arg|docker compose.* up' <<<"$build_step"; then
  printf 'Docker build must use one step-local public dbx interpolation value and no secret/build-arg/start\n' >&2
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
  "deploy/production.dependencies.env"
  "backend/.dockerignore"
  "backend/Dockerfile"
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
  "backend/app/main.py" \
  "backend/scripts/maintenance/reset_admin_password.py" \
  "backend/scripts/download_voice_model.py" \
  "frontend/dist/index.html" \
  "frontend/src/main.tsx" \
  "docker/browser/Dockerfile" \
  "docker/weknora/gateway.conf" \
  "docker/weknora/weknora-proxy.inc" \
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
  WEKNORA_POSTGRES_IMAGE WEKNORA_REDIS_IMAGE ; do
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
  WEKNORA_POSTGRES_IMAGE WEKNORA_REDIS_IMAGE ; do
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
  WEKNORA_DEFAULT_LOCALE; do
  if [ "${!key+x}" = "x" ]; then
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
  */healthz) body='ok' ;;
esac
if [ -n "$headers" ]; then
  case "$url" in
    *.js) printf 'HTTP/1.1 200 OK\r\nContent-Type: application/javascript\r\n\r\n' >"$headers" ;;
    *.css) printf 'HTTP/1.1 200 OK\r\nContent-Type: text/css\r\n\r\n' >"$headers" ;;
    *) printf 'HTTP/1.1 %s OK\r\nContent-Type: text/html\r\n\r\n' "$status" >"$headers" ;;
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
  WEKNORA_DEFAULT_LOCALE; do
  if [ "${!key+x}" = "x" ]; then
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
  *' ps -q pipeline_executor') printf '%s\n' "$(printf 'p%.0s' {1..64})" ;;
  *' ps -q backend') printf '%s\n' "$(printf 'c%.0s' {1..64})" ;;
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
if [ "${FAKE_TIMEOUT_TRIGGER:-0}" = "1" ]; then
  sleep "$seconds"
  exit 124
fi
exec "$@"
FAKE_TIMEOUT
chmod +x "$attestation_fake_bin/curl" "$attestation_fake_bin/docker" "$attestation_fake_bin/timeout"

run_attestation_deploy() {
  local log="$1" source="$2" timeout="$3"
  shift 3
  (
    cd "$test_dir"
    env \
      PATH="$attestation_fake_bin:$PATH" \
      FAKE_DOCKER_LOG="$log.docker" \
      FAKE_ATTESTATION_SOURCE="$source" \
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

printf 'deployment guard self-tests passed\n'
