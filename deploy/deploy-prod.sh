#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-/opt/openontology}"
bash "$SCRIPT_DIR/../scripts/ci/validate-deploy-app-dir.sh" "$APP_DIR"
BRANCH="${BRANCH:-nano-ontoprompt}"
REPO_URL="${REPO_URL:-https://github.com/Carlehyy/OpenOntology.git}"
COMPOSE_FILE="docker-compose.prod.yml"
# AgentLoop 观测接入（阿里云 AgentLoop）：服务器 .env 中配置了 ARMS_LICENSE_KEY 时
# 自动叠加探针覆盖文件（agentloop/compose.agentloop.yml）。未配置时部署行为与
# 之前完全一致；接入细节见 agentloop/README.md。
if [ -f "$APP_DIR/.env" ] && grep -qE '^ARMS_LICENSE_KEY=[^[:space:]]+' "$APP_DIR/.env"; then
  COMPOSE_FILE="${COMPOSE_FILE}:agentloop/compose.agentloop.yml"
fi
DEPENDENCY_CONFIG_FILE="${DEPENDENCY_CONFIG_FILE:-deploy/production.dependencies.env}"
# Preserve explicit operator overrides, but defer defaults until the persistent
# .env and the production dependency manifest have been merged. The dependency
# manifest is mandatory and must configure every external runtime service.
# PUBLIC_PORT from that manifest drives the endpoint Compose will expose.
HEALTH_URL="${HEALTH_URL:-}"
READINESS_URL="${READINESS_URL:-}"
EXPECTED_RUNTIME_COMMIT="${EXPECTED_RUNTIME_COMMIT:-}"
RETRIES="${DEPLOY_RETRIES:-3}"
SLEEP_SECONDS="${DEPLOY_RETRY_SLEEP:-10}"
RUNTIME_SOURCE_ATTESTATION_TIMEOUT_SECONDS="${RUNTIME_SOURCE_ATTESTATION_TIMEOUT_SECONDS:-120}"
built_backend_image_id=""
backend_image_platform=""
previous_runtime_services=()
runtime_stopped_for_migration=0
migration_completed=0
migration_head_revision=""
pre_migration_revision=""
bootstrap_tmp_path=""
weknora_bootstrap_active=0
kaneo_bootstrap_active=0
# Default for check_image_digest, which validate_multica_config may reach
# before the strict-mode switch below re-derives it from STRICT_IMAGE_DIGESTS.
strict_image_digests_enabled=0
log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
if [ -L "$APP_DIR" ]; then
  log "$APP_DIR must be a real deployment directory; refusing to follow a symlink"
  exit 1
fi
if [ -L "$APP_DIR/.env" ]; then
  log "production .env must be a regular server-side file; refusing to replace a secret-authority symlink"
  exit 1
fi
run_with_retry() {
  local attempt=1
  until "$@"; do
    if [ "$attempt" -ge "$RETRIES" ]; then
      log "command failed after ${attempt} attempts: $*"
      return 1
    fi
    log "command failed, retrying in ${SLEEP_SECONDS}s (${attempt}/${RETRIES}): $*"
    attempt=$((attempt + 1))
    sleep "$SLEEP_SECONDS"
  done
}
INTEGRATED_DBX_INTERNAL=0
INTEGRATED_DBX_COMMIT_PIN="0137e0cbc8b663da5a17ec424c6d47e4e5570ead"
# Hindsight 长期记忆是可选 profile：核心 compose 调用一律不携带该 profile。
# HINDSIGHT_ENABLED=true 只生成/校验配置；拉取与启动完全后置到核心部署成功
# 之后的 start_hindsight_memory_stack（唯一带 --profile hindsight 的启动调用，
# 有时间上界）。未启用时仅停旧容器。可选栈任何失败都不进核心 up/成败判定。
HINDSIGHT_INTERNAL=0
HINDSIGHT_IMAGE_PIN="ghcr.io/vectorize-io/hindsight:1.2.0@sha256:19d0c08f3df79acac29384826419a1ca158129e81ec527cdcbd8c97ff4ef76da"
# 官方 pgvector pg17 的多架构 index digest：amd64+arm64 清单已经公共镜像源
# OCI 元数据核验（arm64 本机 Docker Hub 实拉成功；docker.io 直连不可达，
# amd64 实拉/官方重取当前 tag 头 sha256:7d0a4146… 待网络恢复）。拉取失败时
# 运维重取多架构 digest 并覆盖 .env，禁止浮动 tag。
HINDSIGHT_POSTGRES_IMAGE_PIN="pgvector/pgvector:pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f"
# Multica 默认服务与本地栈一样默认启用（MULTICA_ENABLED 留空/true 即启用，
# 仅显式 false 关闭）。官方自托管栈契约已按上游 multica-ai/multica v0.11.0
# （commit cb190810a659c1a61c4a86d9f6ecfc9df4b086d8 源码树 + GHCR OCI
# revision/version 标签）核验：pgvector/pg17 + backend（容器内 8080，
# /healthz=ready）+ web（容器内 3000，服务端代理 API）。GHCR 只发布
# latest tag，因此 pin 用与 v0.11.0 revision 一致的多架构 digest。启动
# 用单独的 --profile multica 有界调用，时机在最终核心 up 之后、readiness
# 门禁等待之前——五项 MULTICA_DEFAULT_* 完整时生产 readiness 会门禁
# multica_default，先启动才能避免“等就绪才启动”的自我死锁；五项全空时
# 该启动仍只是尽力而为（失败仅告警，readiness 不受影响）。MULTICA_DEFAULT_PAT
# 与 MULTICA_JWT_SECRET/MULTICA_POSTGRES_PASSWORD 是部署级密钥：只落服务器
# .env（或受控 manifest 物化 PAT），绝不进部署日志。
MULTICA_INTERNAL=0
MULTICA_BACKEND_IMAGE_PIN="ghcr.io/multica-ai/multica-backend@sha256:099fd0d1eea8fc4cf03254c6dff86f7de206662ff71db8dc9a78e66726dd65a4"
MULTICA_WEB_IMAGE_PIN="ghcr.io/multica-ai/multica-web@sha256:ba8fd0fda94a9ec141e15d77e591db8ee4b246720a722d6f9dffd185ee48863b"
# 与 HINDSIGHT_POSTGRES_IMAGE_PIN 同一官方 pgvector pg17 多架构 digest。
MULTICA_POSTGRES_IMAGE_PIN="pgvector/pgvector:pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f"

compose_environment() {
  # Docker Compose gives exported shell variables precedence over the project
  # .env file. The manifest has already been validated and persisted into
  # .env, so clear every interpolation/config authority that could otherwise
  # make the containers differ from what this script validated. Values are
  # deliberately named here rather than printed or dynamically expanded.
  # COMPOSE_PROFILES is set (empty) instead of unset: profile activation is
  # script-owned (--profile hindsight/multica/integrated-dbx flags only).
  # Unsetting would let a server .env that carries the local-dev default
  # COMPOSE_PROFILES=multica smuggle the multica stack into the core
  # `up -d --remove-orphans`, bypassing the bounded isolated start below.
  env \
    -u ENVIRONMENT \
    -u PUBLIC_PORT \
    -u PUBLIC_TLS_IP \
    -u POSTGRES_DB \
    -u POSTGRES_USER \
    -u POSTGRES_PASSWORD \
    -u DATABASE_URL \
    -u REDIS_PASSWORD \
    -u REDIS_URL \
    -u NEO4J_URI \
    -u NEO4J_USER \
    -u NEO4J_PASSWORD \
    -u NEO4J_AUTH \
    -u MINIO_ENDPOINT \
    -u MINIO_ACCESS_KEY \
    -u MINIO_SECRET_KEY \
    -u MINIO_USE_SSL \
    -u N8N_API_URL \
    -u N8N_API_KEY \
    -u N8N_TIMEOUT_SECONDS \
    -u INTEGRATED_DBX_URL \
    -u INTEGRATED_DBX_PASSWORD \
    -u INTEGRATED_DBX_COMMIT \
    -u INTEGRATED_DBX_INTERNAL \
    -u REPLICA_MAX_CAPTURES \
    -u REPLICA_EXECUTOR_MEM_LIMIT \
    -u REPLICA_EXECUTOR_CPUS \
    -u PYTHON_KERNEL_GATEWAY_AUTH_TOKEN \
    -u SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE \
    -u ASSISTANT_ACTION_EXECUTION_ENABLED \
    -u ASSISTANT_BROWSER_PROTECTED_ORIGINS \
    -u AGENT_QUERY_STATEMENT_TIMEOUT_MS \
    -u AGENT_QUERY_LOCK_TIMEOUT_MS \
    -u AGENT_QUERY_WORK_MEM_MIB \
    -u AGENT_QUERY_TEMP_FILE_LIMIT_MIB \
    -u STEWARD_BROWSER_CDP_URL \
    -u CORS_ALLOWED_ORIGINS \
    -u UPLOADS_DIR \
    -u PIPELINE_FILE_GATEWAY_BASE_URL \
    -u PIPELINE_FILE_PUBLIC_APP_BASE_URL \
    -u PIPELINE_FILE_PUBLIC_API_BASE_URL \
    -u SUPER_ASSISTANT_PUBLIC_API_BASE_URL \
    -u SUPER_ASSISTANT_MCP_INTERNAL_BASE_URLS \
    -u WEKNORA_BASE_URL \
    -u WEKNORA_WEB_URL \
    -u WEKNORA_HTTP_PORT \
    -u WEKNORA_HTTPS_PORT \
    -u WEKNORA_ADMIN_USER \
    -u WEKNORA_ADMIN_EMAIL \
    -u WEKNORA_ADMIN_PASSWORD \
    -u WEKNORA_TIMEOUT_SECONDS \
    -u WEKNORA_DB_NAME \
    -u WEKNORA_DB_USER \
    -u WEKNORA_DB_PASSWORD \
    -u WEKNORA_REDIS_PASSWORD \
    -u WEKNORA_JWT_SECRET \
    -u WEKNORA_SYSTEM_AES_KEY \
    -u WEKNORA_SYSTEM_SIGNING_KEY \
    -u WEKNORA_APP_IMAGE \
    -u WEKNORA_UI_IMAGE \
    -u WEKNORA_DOCREADER_IMAGE \
    -u WEKNORA_POSTGRES_IMAGE \
    -u WEKNORA_REDIS_IMAGE \
    -u WEKNORA_MAX_FILE_SIZE_MB \
    -u WEKNORA_DEFAULT_LOCALE \
    -u KANEO_BASE_URL \
    -u KANEO_WEB_URL \
    -u KANEO_TIMEOUT_SECONDS \
    -u KANEO_HTTP_PORT \
    -u KANEO_HTTPS_PORT \
    -u KANEO_APP_IMAGE \
    -u KANEO_POSTGRES_IMAGE \
    -u KANEO_DB_USER \
    -u KANEO_DB_NAME \
    -u KANEO_DB_PASSWORD \
    -u KANEO_AUTH_SECRET \
    -u KANEO_ADMIN_USER \
    -u KANEO_ADMIN_EMAIL \
    -u KANEO_ADMIN_PASSWORD \
    -u GRAFANA_IMAGE \
    -u GRAFANA_BASE_URL \
    -u GRAFANA_TIMEOUT_SECONDS \
    -u GRAFANA_ADMIN_USER \
    -u GRAFANA_ADMIN_PASSWORD \
    -u GRAFANA_FOLDER_UID \
    -u GRAFANA_FOLDER_TITLE \
    -u GRAFANA_SESSION_TTL_SECONDS \
    -u HINDSIGHT_ENABLED \
    -u HINDSIGHT_BASE_URL \
    -u HINDSIGHT_WEB_URL \
    -u HINDSIGHT_WEB_PORT \
    -u HINDSIGHT_CP_ACCESS_KEY \
    -u HINDSIGHT_API_KEY \
    -u HINDSIGHT_SERVICE_TOKEN \
    -u HINDSIGHT_TIMEOUT_SECONDS \
    -u HINDSIGHT_NAMESPACE \
    -u HINDSIGHT_DB_NAME \
    -u HINDSIGHT_DB_USER \
    -u HINDSIGHT_DB_PASSWORD \
    -u HINDSIGHT_IMAGE \
    -u HINDSIGHT_POSTGRES_IMAGE \
    -u HINDSIGHT_HF_OFFLINE \
    -u MULTICA_ENABLED \
    -u MULTICA_BACKEND_IMAGE \
    -u MULTICA_WEB_IMAGE \
    -u MULTICA_POSTGRES_IMAGE \
    -u MULTICA_POSTGRES_DB \
    -u MULTICA_POSTGRES_USER \
    -u MULTICA_POSTGRES_PASSWORD \
    -u MULTICA_JWT_SECRET \
    -u MULTICA_WEB_PORT \
    -u MULTICA_WEB_BIND_ADDR \
    -u MULTICA_HTTPS_PORT \
    -u MULTICA_APP_ENV \
    -u MULTICA_FRONTEND_ORIGIN \
    -u MULTICA_APP_URL \
    -u MULTICA_ALLOW_SIGNUP \
    -u MULTICA_DATABASE_STARTUP_TIMEOUT \
    -u MULTICA_DEFAULT_BASE_URL \
    -u MULTICA_DEFAULT_WEB_URL \
    -u MULTICA_DEFAULT_WORKSPACE_ID \
    -u MULTICA_DEFAULT_WORKSPACE_NAME \
    -u MULTICA_DEFAULT_PAT \
    -u SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS \
    -u STEWARD_INTERNAL_PROXY_BASE_URL \
    -u STEWARD_BROWSER_HTTP_LEASE_SECONDS \
    -u STEWARD_BROWSER_HTTP_FRAME_INTERVAL_MS \
    -u STEWARD_BROWSER_MAX_SESSIONS \
    -u STEWARD_BROWSER_MAX_SESSIONS_PER_USER \
    -u STEWARD_BROWSER_IDLE_TIMEOUT_SECONDS \
    -u STEWARD_BROWSER_REAPER_INTERVAL_SECONDS \
    -u POSTGRES_IMAGE \
    -u REDIS_IMAGE \
    -u NEO4J_IMAGE \
    -u MINIO_IMAGE \
    -u BROWSER_IMAGE \
    -u PYTHON_BASE_IMAGE \
    -u NODE_BASE_IMAGE \
    -u NGINX_BASE_IMAGE \
    -u STRICT_IMAGE_DIGESTS \
    -u COMPOSE_DISABLE_ENV_FILE \
    -u COMPOSE_ENV_FILES \
    -u COMPOSE_FILE \
    -u COMPOSE_PATH_SEPARATOR \
    -u COMPOSE_PROJECT_NAME \
    COMPOSE_PROFILES= \
    "$@"
}
compose() {
  # COMPOSE_FILE 支持冒号分隔的多个 compose 文件（与 docker compose 的 COMPOSE_FILE
  # 环境变量语义一致），逐个展开为 -f 参数。
  local compose_args=() file
  local call_timeout="${COMPOSE_CALL_TIMEOUT:-0}"
  local -a profile_args=()
  case "$call_timeout" in
    ""|*[!0-9]*) call_timeout=0 ;;
  esac
  local IFS=':'
  for file in $COMPOSE_FILE; do
    compose_args+=(-f "$file")
  done
  if [ "$INTEGRATED_DBX_INTERNAL" = "1" ] && [ "${1:-}" != "version" ]; then
    profile_args=(--profile integrated-dbx)
  fi
  if [ "$call_timeout" -gt 0 ] && command -v timeout >/dev/null 2>&1; then
    if compose_environment docker compose version >/dev/null 2>&1; then
      compose_environment timeout "$call_timeout" docker compose "${profile_args[@]}" "${compose_args[@]}" "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
      compose_environment timeout "$call_timeout" docker-compose "${profile_args[@]}" "${compose_args[@]}" "$@"
    else
      log "Docker Compose is not installed"
      return 1
    fi
  elif compose_environment docker compose version >/dev/null 2>&1; then
    compose_environment docker compose "${profile_args[@]}" "${compose_args[@]}" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    compose_environment docker-compose "${profile_args[@]}" "${compose_args[@]}" "$@"
  else
    log "Docker Compose is not installed"
    return 1
  fi
}
database_current_revision() {
  local current_output current_revision
  if ! current_output="$(
    compose run --rm --no-deps backend alembic current 2>/dev/null
  )"; then
    return 1
  fi
  current_revision="$(
    awk '$1 ~ /^[0-9][0-9][0-9][0-9]_[[:alnum:]_]+$/ { print $1; exit }' \
      <<<"$current_output"
  )"
  [ -n "$current_revision" ] || return 1
  printf '%s\n' "$current_revision"
}
restore_previous_runtime_on_exit() {
  local exit_status=$?
  local current_revision
  if [ -n "$bootstrap_tmp_path" ] && [ -f "$bootstrap_tmp_path" ]; then
    rm -f -- "$bootstrap_tmp_path"
  fi
  if [ "$weknora_bootstrap_active" = "1" ]; then
    # A failed initialization must never leave the registration-open
    # initializer or the new public gateway running. Volumes are preserved.
    log "closing the WeKnora initialization surface after a failed deployment"
    compose --profile weknora-bootstrap rm -sf weknora-app-init >/dev/null 2>&1 || true
    compose stop --timeout 30 weknora-gateway weknora-ui weknora-app >/dev/null 2>&1 || true
  fi
  if [ "$kaneo_bootstrap_active" = "1" ]; then
    # A failed initialization must never leave the Kaneo public gateway or the
    # private app running. Volumes are preserved for a retry.
    log "closing the Kaneo public surface after a failed deployment"
    compose stop --timeout 30 kaneo-gateway kaneo-app >/dev/null 2>&1 || true
  fi
  if [ "$runtime_stopped_for_migration" = "1" ] \
      && [ "$migration_completed" = "0" ] \
      && [ "${#previous_runtime_services[@]}" -gt 0 ]; then
    if ! current_revision="$(database_current_revision)"; then
      log "could not prove the database revision after migration failure; refusing an unsafe runtime restart"
      log "inspect the migration revision before taking operator action"
      return "$exit_status"
    fi
    if [ "$current_revision" = "$migration_head_revision" ]; then
      log "migration reached head; refusing to restart the previous runtime against the new schema"
      log "restore the pre-deploy backup or apply a forward fix"
      return "$exit_status"
    fi
    if [ -z "$pre_migration_revision" ] \
        || [ "$current_revision" != "$pre_migration_revision" ]; then
      log "database revision changed from ${pre_migration_revision:-unknown} to ${current_revision}; refusing to restart the previous runtime"
      log "restore the pre-deploy backup or inspect compatibility before operator action"
      return "$exit_status"
    fi
    log "migration failed without changing the database revision; restarting the previous runtime"
    if ! compose start "${previous_runtime_services[@]}"; then
      log "failed to restart the previous runtime; operator intervention is required"
    fi
  fi
  return "$exit_status"
}
trap restore_previous_runtime_on_exit EXIT
stop_new_runtime_after_failed_deploy() {
  log "stopping the partially started new runtime after a post-migration failure"
  if ! compose stop --timeout 30 frontend pipeline_executor backend integrated_replica_executor; then
    log "failed to stop the new runtime completely; operator intervention is required"
  fi
  if ! compose stop --timeout 30 weknora-gateway; then
    log "failed to stop the WeKnora public gateway; operator intervention is required"
  fi
  if ! compose stop --timeout 30 kaneo-gateway; then
    log "failed to stop the Kaneo public gateway; operator intervention is required"
  fi
}
assert_asset() {
  local url="$1"
  local expected="$2"
  local headers
  headers="$(mktemp)"
  if ! curl -fsS --connect-timeout 5 --max-time 20 -D "$headers" -o /dev/null "$url"; then
    rm -f "$headers"
    return 1
  fi
  if ! grep -Eiq "^content-type:.*${expected}" "$headers"; then
    log "asset content type mismatch: $url"
    tr -d '\r' < "$headers" | awk 'BEGIN{IGNORECASE=1}/^HTTP|^Content-Type|^Content-Length/{print "  " $0}' >&2
    rm -f "$headers"
    return 1
  fi
  rm -f "$headers"
}
check_frontend_assets() {
  local base="${HEALTH_URL%/}"
  local index_file main_file
  local entry css asset expected
  index_file="$(mktemp)"
  main_file="$(mktemp)"
  curl -fsS --connect-timeout 5 --max-time 20 "$base/" -o "$index_file"

  entry="$(grep -oE 'src="/assets/[^"]+\.js"' "$index_file" | head -n1 | sed -E 's/src="([^"]+)"/\1/')"
  css="$(grep -oE 'href="/assets/[^"]+\.css"' "$index_file" | head -n1 | sed -E 's/href="([^"]+)"/\1/')"
  [ -n "$entry" ] || { log "frontend entry script not found in index.html"; rm -f "$index_file" "$main_file"; return 1; }

  assert_asset "$base$entry" "javascript"
  [ -z "$css" ] || assert_asset "$base$css" "text/css"
  curl -fsS --connect-timeout 5 --max-time 30 "$base$entry" -o "$main_file"

  grep -aoE 'assets/[-A-Za-z0-9_./]+\.(js|css)' "$main_file" | sort -u | while read -r asset; do
    case "$asset" in
      *.js) expected="javascript" ;;
      *.css) expected="text/css" ;;
      *) continue ;;
    esac
    assert_asset "$base/$asset" "$expected"
  done

  rm -f "$index_file" "$main_file"
}
check_pipeline_executor() {
  # executor 没有进程内 ping 接口，以容器健康检查（心跳文件 mtime）为准
  local container_id health
  container_id="$(compose ps -q pipeline_executor 2>/dev/null || true)"
  [ -n "$container_id" ] || return 1
  health="$(compose_environment docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
    "$container_id" 2>/dev/null || true)"
  [ "$health" = "healthy" ]
}
check_replica_executor() {
  # 副本执行器同样以容器健康检查（对账心跳文件 mtime）为准；没有运行时
  # 捕获时也会刷新心跳，因此健康不代表已接入源，只代表执行器真实存活。
  local container_id health
  container_id="$(compose ps -q integrated_replica_executor 2>/dev/null || true)"
  [ -n "$container_id" ] || return 1
  health="$(compose_environment docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
    "$container_id" 2>/dev/null || true)"
  [ "$health" = "healthy" ]
}
backend_app_source_is_unmounted() {
  local container_id="$1" destinations destination
  if ! destinations="$(compose_environment docker inspect --format '{{range .Mounts}}{{.Destination}}{{"\n"}}{{end}}' \
      "$container_id" 2>/dev/null)"; then
    log "could not inspect backend container mounts"
    return 1
  fi
  while IFS= read -r destination; do
    [ -n "$destination" ] || continue
    destination="${destination%/}"
    [ -n "$destination" ] || destination="/"
    case "$destination" in
      /|/app|/app/app|/app/app/*)
        log "backend source path is covered by a container mount: $destination"
        return 1
        ;;
    esac
  done <<<"$destinations"
  return 0
}
verify_runtime_source_attestation() {
  local manifest="$SCRIPT_DIR/runtime-source-manifest.json"
  local verifier="$SCRIPT_DIR/runtime_source_attestation.py"
  local expected_commit="$EXPECTED_RUNTIME_COMMIT"
  local backend_cids backend_cid_count backend_cid
  local running_image_id running_platform_image_id started_at
  local verifier_source attestation_output attestation_status line
  case "$RUNTIME_SOURCE_ATTESTATION_TIMEOUT_SECONDS" in
    ""|*[!0-9]*) log "RUNTIME_SOURCE_ATTESTATION_TIMEOUT_SECONDS must be a positive integer"; return 1 ;;
  esac
  if [ "$RUNTIME_SOURCE_ATTESTATION_TIMEOUT_SECONDS" -lt 1 ] \
      || [ "$RUNTIME_SOURCE_ATTESTATION_TIMEOUT_SECONDS" -gt 900 ]; then
    log "RUNTIME_SOURCE_ATTESTATION_TIMEOUT_SECONDS must be between 1 and 900"
    return 1
  fi
  if [ ! -f "$manifest" ] || [ -L "$manifest" ]; then
    log "runtime source manifest is missing or is not a regular file"
    return 1
  fi
  if [ ! -f "$verifier" ] || [ -L "$verifier" ]; then
    log "runtime source verifier is missing or is not a regular file"
    return 1
  fi
  if [[ ! "$expected_commit" =~ ^[0-9a-f]{40}$ ]]; then
    log "EXPECTED_RUNTIME_COMMIT must be a full lowercase commit"
    return 1
  fi
  if [[ ! "$built_backend_image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    log "the backend image built by this deploy has no recorded image ID"
    return 1
  fi

  backend_cids="$(compose ps -q backend 2>/dev/null || true)"
  backend_cid_count="$(printf '%s\n' "$backend_cids" | sed '/^$/d' | wc -l | tr -d ' ')"
  if [ "$backend_cid_count" -ne 1 ]; then
    log "expected exactly one running backend container, found ${backend_cid_count}"
    return 1
  fi
  backend_cid="$(printf '%s\n' "$backend_cids" | sed '/^$/d' | head -n1)"
  case "$backend_cid" in
    *[!0-9a-f]*) log "active backend container id is malformed"; return 1 ;;
  esac
  if [ "${#backend_cid}" -lt 12 ] || [ "${#backend_cid}" -gt 64 ]; then
    log "active backend container id has an unexpected length"
    return 1
  fi

  running_image_id="$(compose_environment docker inspect --format '{{.Image}}' "$backend_cid" 2>/dev/null || true)"
  started_at="$(compose_environment docker inspect --format '{{.State.StartedAt}}' "$backend_cid" 2>/dev/null || true)"
  if [[ ! "$running_image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    log "active backend image id is malformed"
    return 1
  fi
  if [[ ! "$started_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z$ ]]; then
    log "active backend StartedAt is malformed"
    return 1
  fi
  running_platform_image_id="$(
    compose_environment docker inspect --format '{{.ImageManifestDescriptor.Digest}}' \
      "$backend_cid" 2>/dev/null || true
  )"
  if [[ ! "$running_platform_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
      || [ "$running_platform_image_id" != "$built_backend_image_id" ]; then
    log "active backend image ${running_image_id} does not match the image built by this deploy ${built_backend_image_id}"
    return 1
  fi
  if ! backend_app_source_is_unmounted "$backend_cid"; then
    return 1
  fi

  verifier_source="$(cat "$verifier")"
  if [ -z "$verifier_source" ]; then
    log "runtime source verifier could not be read"
    return 1
  fi
  if attestation_output="$(
    COMPOSE_CALL_TIMEOUT="$RUNTIME_SOURCE_ATTESTATION_TIMEOUT_SECONDS" \
      compose exec -T backend python -c "$verifier_source" verify \
        --manifest-stdin \
        --source-root /app/app \
        --expected-commit "$expected_commit" \
        --container-id "$backend_cid" \
        --image-id "$running_image_id" \
        --started-at "$started_at" \
        --timeout-seconds "$RUNTIME_SOURCE_ATTESTATION_TIMEOUT_SECONDS" \
        < "$manifest" 2>&1
  )"; then
    attestation_status=0
  else
    attestation_status=$?
  fi
  if [ "$attestation_status" -ne 0 ]; then
    if [ "$attestation_status" -eq 124 ]; then
      log "runtime source attestation timed out after ${RUNTIME_SOURCE_ATTESTATION_TIMEOUT_SECONDS}s"
    fi
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      log "runtime source attestation: ${line}"
    done <<<"$attestation_output"
    return 1
  fi
  if ! grep -Fq 'status=ok scope=running_container_files_not_loaded_memory' \
      <<<"$attestation_output"; then
    log "runtime source verifier returned no success record"
    return 1
  fi
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    log "runtime source attestation: ${line}"
  done <<<"$attestation_output"
  log "runtime source attestation mount_check=app_source_not_mounted image_matches_build=true"
  return 0
}
stop_disabled_hindsight_stack() {
  # 未启用时仅停掉历史遗留的可选容器（不删卷）。启用时的拉取/启动完全后置到
  # 核心部署成功之后的 start_hindsight_memory_stack：核心 compose 调用一律不
  # 携带 hindsight profile，可选栈的失败不可能进入核心 up 或成败判定。
  if [ "$HINDSIGHT_INTERNAL" = "0" ]; then
    compose --profile hindsight stop hindsight hindsight-db hindsight-model-init \
      >/dev/null 2>&1 || true
  fi
}

stop_disabled_multica_stack() {
  # 与 Hindsight 同构：显式关闭（MULTICA_ENABLED=false）时仅停掉历史遗留的
  # 可选容器（不删卷，数据留给再次启用或外部 Multica 就绪时复用）；启用时
  # 的拉取/启动由核心 up 之后、readiness 门禁之前的
  # start_multica_default_stack 负责。
  if [ "$MULTICA_INTERNAL" = "0" ]; then
    compose --profile multica stop multica multica-web multica-postgres >/dev/null 2>&1 || true
  fi
}

start_required_dependency_services() {
  start_integrated_dbx_if_needed
  stop_disabled_hindsight_stack
  stop_disabled_multica_stack
  # CDP must be configured and is checked by deep readiness, but an unhealthy
  # browser must not prevent the API process from starting for diagnostics.
  # python_kernel_gateway executes user scripts for Python pipelines; it is
  # optional at the platform level (script execution reports a clear error
  # when unreachable), so it starts with the dependency tier without joining
  # the mandatory connectivity probe.
  compose up -d browser python_kernel_gateway
  if compose up --help 2>&1 | grep -q -- '--wait'; then
    compose up -d --wait \
      --wait-timeout "${DEPENDENCY_WAIT_TIMEOUT:-180}" \
      db redis neo4j minio nats
  else
    # Legacy Compose has no --wait. The mandatory connectivity probe below is
    # retried and remains the authoritative availability gate.
    compose up -d db redis neo4j minio nats
  fi
}
wait_for_service_healthy() {
  local service="$1" attempts="${2:-60}" interval="${3:-5}" profile="${4:-}"
  local attempt container_id health
  for attempt in $(seq 1 "$attempts"); do
    if [ -n "$profile" ]; then
      container_id="$(compose --profile "$profile" ps -q "$service" 2>/dev/null || true)"
    else
      container_id="$(compose ps -q "$service" 2>/dev/null || true)"
    fi
    if [ -n "$container_id" ]; then
      health="$(compose_environment docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
        "$container_id" 2>/dev/null || true)"
      if [ "$health" = "healthy" ]; then
        return 0
      fi
    fi
    sleep "$interval"
  done
  log "timed out waiting for $service to become healthy"
  return 1
}

start_hindsight_memory_stack() {
  # 可选记忆栈的独立启动入口：只在核心部署成功（backend 已就绪，Hindsight 的
  # LLM 回调目标存在）之后执行，是全脚本唯一携带 --profile hindsight 的启动
  # 调用，且有 COMPOSE_CALL_TIMEOUT 时间上界。镜像已按 @sha256 钉扎且本地存在
  # 时 compose 不再访问 registry；缺镜像时由该 up 自行拉取。拉取、缓存卷属主
  # init、专用 DB 健康、应用启动或超时的任何失败都只降级记忆特性：停掉本次
  # 起了一半的可选容器（卷保留，下次部署自动重试），绝不触碰核心服务，也
  # 不改写已记录的核心部署成功结论。启动成功但未在观察窗内健康（首启可能在
  # 下载 embedding 模型）则保持运行，仅记录告警。
  [ "$HINDSIGHT_INTERNAL" = "1" ] || return 0
  if ! command -v timeout >/dev/null 2>&1; then
    log "warning: the timeout command is unavailable, so the optional Hindsight start cannot be time-bounded; leaving the memory stack as-is (chat and platform readiness are unaffected)"
    return 0
  fi
  local start_timeout="${HINDSIGHT_START_TIMEOUT:-300}"
  case "$start_timeout" in
    ''|*[!0-9]*|0)
      log "warning: HINDSIGHT_START_TIMEOUT must be a positive integer; using 300s for this deployment"
      start_timeout=300
      ;;
  esac
  log "starting the optional Hindsight memory stack in a separate bounded compose invocation (it never gates the core deployment)"
  local up_status=0
  COMPOSE_CALL_TIMEOUT="$start_timeout" \
    compose --profile hindsight up -d hindsight || up_status=$?
  if [ "$up_status" -ne 0 ]; then
    if [ "$up_status" -eq 124 ]; then
      log "warning: the optional Hindsight memory stack did not start within ${start_timeout}s; stopping it for this deployment (volumes are preserved and the next deployment retries)"
    else
      log "warning: the optional Hindsight memory stack failed to start (exit ${up_status}: image pull, cache init, database health or container start); stopping it for this deployment (volumes are preserved and the next deployment retries)"
    fi
    # 上界与启动一致；含一次性 init 容器——超时打断时它可能仍在运行。
    COMPOSE_CALL_TIMEOUT="$start_timeout" \
      compose --profile hindsight stop hindsight hindsight-db hindsight-model-init \
      >/dev/null 2>&1 || true
    log "hint: inspect on the server with 'docker compose --profile hindsight logs hindsight hindsight-db'"
    return 0
  fi
  if wait_for_service_healthy hindsight "${HINDSIGHT_HEALTH_WAIT_ATTEMPTS:-24}" \
      "${HINDSIGHT_HEALTH_WAIT_INTERVAL:-5}" hindsight; then
    log "hindsight memory service is healthy (feature status only)"
  else
    log "warning: hindsight is not healthy yet (first start may download the embedding model); it stays running and chat/platform readiness are unaffected"
  fi
  check_hindsight_control_plane_entry
}

check_hindsight_control_plane_entry() {
  # 管理 UI 已发布宿主端口：就绪后从本机探测公开入口（/api/health 白名单
  # 200；设了 HINDSIGHT_CP_ACCESS_KEY 时 / 必须跳登录页）。仅记状态与告警，
  # 不影响部署结论——控制面起得比 API health 略晚属正常，浏览器侧重试即可。
  local web_port cp_status root_status attempt
  web_port="$(env_value HINDSIGHT_WEB_PORT)"
  web_port="${web_port:-19999}"
  command -v curl >/dev/null 2>&1 || return 0
  cp_status=""
  for attempt in 1 2 3 4; do
    cp_status="$(curl -s -o /dev/null -w '%{http_code}' \
      --connect-timeout 2 --max-time 5 "http://127.0.0.1:${web_port}/api/health" \
      2>/dev/null || true)"
    [ "$cp_status" = "200" ] && break
    cp_status=""
    sleep 2
  done
  if [ -z "$cp_status" ]; then
    log "warning: the Hindsight control plane did not answer on 127.0.0.1:${web_port}/api/health (it may still be starting; the memory feature and this deployment are unaffected)"
    return 0
  fi
  log "hindsight control-plane entry is reachable (feature status only)"
  if [ -n "$(env_value HINDSIGHT_CP_ACCESS_KEY)" ]; then
    root_status="$(curl -s -o /dev/null -w '%{http_code}' \
      --connect-timeout 2 --max-time 5 "http://127.0.0.1:${web_port}/" \
      2>/dev/null || true)"
    if [ "$root_status" = "307" ]; then
      log "hindsight control-plane login gate is active (feature status only)"
    else
      log "warning: the Hindsight control-plane root returned HTTP ${root_status:-none} instead of the expected login redirect; verify HINDSIGHT_CP_ACCESS_KEY reached the container"
    fi
  fi
}

start_multica_default_stack() {
  # Multica 默认栈的独立启动入口（与 Hindsight 同构的隔离调用）：是全脚本
  # 唯一携带 --profile multica 的启动调用，且有 COMPOSE_CALL_TIMEOUT 时间
  # 上界。调用时机在最终核心 up 成功之后、readiness 门禁等待之前——生产
  # readiness 在五项 MULTICA_DEFAULT_* 配置完整时会门禁 multica_default，
  # 先启动才能避免“等就绪才启动”的自我死锁；五项全空时 readiness 不看
  # multica_default，这里的失败只降级为告警。官方镜像契约已核验（backend
  # /healthz 容器健康检查 + web 健康检查），启动后做有界健康等待；空卷
  # 首启会在容器内先跑 ./migrate up，等待窗口须覆盖该预算。默认 300 秒与
  # Hindsight 的隔离启动先例一致：首次部署要在这个有界调用内拉满三个
  # 钉扎镜像（backend/web/pgvector，GHCR + Docker Hub），慢链路下更短的
  # 上界会在首拉时超时。启动失败只停掉本次起了一半的容器（卷保留，
  # 下次部署自动重试，已拉取的镜像层会被复用），绝不触碰核心服务；
  # 若五值已配置，随后的 readiness 门禁会如实把部署判为失败。绝不宣称空
  # 卷已初始化账号/PAT/workspace——那是管理员在 Multica 原生界面/官方
  # CLI 的手动步骤（见 configuration.md）。
  [ "$MULTICA_INTERNAL" = "1" ] || return 0
  if ! command -v timeout >/dev/null 2>&1; then
    log "warning: the timeout command is unavailable, so the bounded Multica start cannot be enforced; starting it unbounded before the readiness gate"
  fi
  local start_timeout="${MULTICA_START_TIMEOUT:-300}"
  case "$start_timeout" in
    ''|*[!0-9]*|0)
      log "warning: MULTICA_START_TIMEOUT must be a positive integer; using 300s for this deployment"
      start_timeout=300
      ;;
  esac
  log "starting the Multica default stack in a separate bounded compose invocation (before the readiness gate, so a configured multica_default check cannot deadlock on a stack that was never started)"
  # 网关容器按部署强制重建：历史上一次挂载源缺失的失败创建会让 Docker 在
  # 容器内把 /etc/nginx 挂载点建成目录，之后源文件就位也复用旧容器而无法
  # file→file 挂载（not a directory）。nginx 容器秒级重建，代价可忽略。
  compose --profile multica rm -f --stop multica-gateway >/dev/null 2>&1 || true
  local up_status=0
  COMPOSE_CALL_TIMEOUT="$start_timeout" \
    compose --profile multica up -d multica multica-web multica-gateway || up_status=$?
  if [ "$up_status" -ne 0 ]; then
    if [ "$up_status" -eq 124 ]; then
      log "warning: the Multica default stack did not start within ${start_timeout}s; stopping it for this deployment (volumes are preserved and the next deployment retries)"
    else
      log "warning: the Multica default stack failed to start (exit ${up_status}: image pull or container start); stopping it for this deployment (volumes are preserved and the next deployment retries)"
    fi
    COMPOSE_CALL_TIMEOUT="$start_timeout" \
      compose --profile multica stop multica multica-web multica-gateway multica-postgres >/dev/null 2>&1 || true
    log "hint: inspect on the server with 'docker compose --profile multica logs multica multica-postgres'"
    return 0
  fi
  # 有界健康等待（官方 /healthz 容器健康检查；空库首启含迁移预算）。
  if wait_for_service_healthy multica 36 5 multica; then
    log "multica default stack started and the official backend /healthz check is healthy"
  else
    log "warning: the Multica backend did not report a healthy /healthz within the bounded wait; leaving the containers up for operator inspection (volumes are preserved; when the five-value default is configured the readiness gate below will report this deployment as failed)"
    log "hint: inspect on the server with 'docker compose --profile multica logs multica multica-postgres'"
  fi
}

start_weknora_initializer() {
  # The initializer is the ONLY registration-open WeKnora process. It runs on
  # the private compose network with no published port, is removed before the
  # formal app starts, and is also removed by the failure trap.
  weknora_bootstrap_active=1
  log "starting WeKnora dependencies and the temporary registration-open initializer"
  if compose up --help 2>&1 | grep -q -- '--wait'; then
    run_with_retry compose --profile weknora-bootstrap up -d --wait \
      --wait-timeout "${WEKNORA_BOOTSTRAP_WAIT_TIMEOUT:-300}" \
      weknora-db weknora-redis weknora-docreader weknora-app-init
  else
    run_with_retry compose --profile weknora-bootstrap up -d \
      weknora-db weknora-redis weknora-docreader weknora-app-init
    run_with_retry wait_for_service_healthy weknora-app-init 60 5
  fi
}

stop_weknora_initializer() {
  log "removing the registration-open WeKnora initializer before the formal app starts"
  compose --profile weknora-bootstrap rm -sf weknora-app-init >/dev/null
}

run_weknora_bootstrap() {
  log "running WeKnora bootstrap: $*"
  run_with_retry compose --profile weknora-bootstrap run --rm weknora-bootstrap \
    python -m app.super_assistant.weknora.bootstrap "$@"
}

start_and_verify_weknora_app() {
  log "starting the formal WeKnora app with registration disabled"
  if compose up --help 2>&1 | grep -q -- '--wait'; then
    run_with_retry compose up -d --wait \
      --wait-timeout "${WEKNORA_APP_WAIT_TIMEOUT:-300}" weknora-app
  else
    run_with_retry compose up -d weknora-app
    run_with_retry wait_for_service_healthy weknora-app 60 5
  fi
  # Account verification must finish before the public gateway starts. Models,
  # knowledge bases and MCP endpoints are configured later in the native Web.
  run_weknora_bootstrap --verify
  weknora_bootstrap_active=0
}

check_weknora_public_entry() {
  local http_port https_port auth_json mcp_status sso_status
  http_port="$(env_value WEKNORA_HTTP_PORT)"
  https_port="$(env_value WEKNORA_HTTPS_PORT)"
  http_port="${http_port:-8089}"
  https_port="${https_port:-8444}"
  # Check public service liveness and registration protection.
  curl -fsS --connect-timeout 5 --max-time 10 \
    "http://127.0.0.1:${http_port}/healthz" >/dev/null || return 1
  auth_json="$(curl -kfsS --connect-timeout 5 --max-time 20 \
    "https://127.0.0.1:${https_port}/api/v1/auth/config")" || return 1
  if ! grep -Eq '"registration_mode"[[:space:]]*:[[:space:]]*"invite_only"' \
      <<<"$auth_json"; then
    log "WeKnora public auth config does not report invite_only registration"
    return 1
  fi
  # Public MCP must remain closed at the gateway even though the native UI
  # keeps /mcp/ internally. Test both the exact collection path and a child.
  for mcp_path in /mcp /mcp/not-a-real-endpoint; do
    mcp_status="$(curl -ks -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 \
      "https://127.0.0.1:${https_port}${mcp_path}")" || return 1
    if [ "$mcp_status" != "404" ]; then
      log "WeKnora public ${mcp_path} must return 404, got ${mcp_status}"
      return 1
    fi
  done
  # 一键免登录中继链路（weknora-gateway /sso/entry → backend consume）：无票据
  # 访问必须 302 回根路径且不带载荷，证明网关→backend 反代与路由已接通。
  sso_status="$(curl -ksS -o /dev/null -w '%{http_code}' \
    --connect-timeout 5 --max-time 10 \
    "https://127.0.0.1:${https_port}/sso/entry" || true)"
  if [ "$sso_status" != "302" ]; then
    log "WeKnora SSO entry relay must answer 302 on the HTTPS entry (got ${sso_status})"
    return 1
  fi
  return 0
}

run_kaneo_bootstrap() {
  log "running Kaneo bootstrap: $*"
  run_with_retry compose --profile kaneo-bootstrap run --rm --no-deps kaneo-bootstrap \
    python -m app.super_assistant.kaneo.bootstrap "$@"
}

start_and_verify_kaneo_stack() {
  # Kaneo keeps registration disabled in every process: an empty instance
  # still allows creating the first administrator, so no registration-open
  # initializer exists. The public gateway is closed while the private app is
  # (re)initialized and is only started by the final compose up after the
  # platform health conditions are satisfied.
  kaneo_bootstrap_active=1
  log "closing the Kaneo public gateway before private initialization"
  # A running public gateway must never stay open while an empty or restored
  # instance is reinitialized: abort instead of ignoring a failed stop. Compose
  # stop succeeds when the service has no container, so no fallback is needed.
  compose stop --timeout 30 kaneo-gateway
  log "starting the Kaneo database and app on the private network"
  if compose up --help 2>&1 | grep -q -- '--wait'; then
    run_with_retry compose up -d --wait \
      --wait-timeout "${KANEO_APP_WAIT_TIMEOUT:-360}" kaneo-db kaneo-app
  else
    run_with_retry compose up -d kaneo-db kaneo-app
    run_with_retry wait_for_service_healthy kaneo-app 48 10
  fi
  # Account creation and login verification must finish before the public
  # gateway starts; project data lives in Kaneo's native Web UI.
  run_kaneo_bootstrap
  run_kaneo_bootstrap --verify
  kaneo_bootstrap_active=0
}

run_grafana_bootstrap() {
  log "running Grafana bootstrap: $*"
  run_with_retry compose run --rm --no-deps grafana-bootstrap \
    python -m app.operations.grafana.bootstrap "$@" || {
    # A failed Grafana bootstrap makes the deployment fail loudly. Stop only
    # the Grafana service; already running platform business services stay up.
    log "stopping the grafana container after a failed bootstrap"
    compose stop --timeout 30 grafana >/dev/null 2>&1 || true
    return 1
  }
}

start_and_verify_grafana_stack() {
  # Grafana is a hard platform dependency: it is started privately and its
  # one-shot bootstrap (health + shared-admin + folder ensure, then --verify)
  # must pass before the platform probe, migrations and the final compose up.
  # The browser entry only opens with the frontend, which the final compose up
  # starts after all platform health conditions are satisfied.
  #
  # Record whether this Compose project already had a running Grafana before
  # startup. A failed status query is a hard failure: an empty result must not
  # be mistaken for "no container", because that could wrongly stop a service
  # on a later failure. When startup/health-wait fails, only a Grafana instance
  # created by this deployment is stopped; a previously running instance is
  # left in place, and no other business service is touched.
  local grafana_was_running=0 grafana_container_id startup_status=0
  if ! grafana_container_id="$(compose ps --status running -q grafana)"; then
    log "could not determine whether the grafana container was already running; refusing to start it"
    return 1
  fi
  if [ -n "$grafana_container_id" ]; then
    grafana_was_running=1
  fi
  log "starting the Grafana service on the private network"
  if compose up --help 2>&1 | grep -q -- '--wait'; then
    if ! run_with_retry compose up -d --wait \
        --wait-timeout "${GRAFANA_APP_WAIT_TIMEOUT:-360}" grafana; then
      startup_status=1
    fi
  else
    if ! run_with_retry compose up -d grafana; then
      startup_status=1
    elif ! run_with_retry wait_for_service_healthy grafana 48 10; then
      startup_status=1
    fi
  fi
  if [ "$startup_status" -ne 0 ]; then
    if [ "$grafana_was_running" -eq 0 ]; then
      log "stopping the grafana container after a failed startup because it was not running before this deployment"
      compose stop --timeout 30 grafana >/dev/null 2>&1 || true
    else
      log "grafana was already running before this deployment; leaving it in place after the failed startup"
    fi
    return 1
  fi
  if ! run_grafana_bootstrap; then
    return 1
  fi
  if ! run_grafana_bootstrap --verify; then
    return 1
  fi
  return 0
}

check_grafana_public_entry() {
  # Security regression hard gate: the platform frontend entry must reject
  # unauthenticated /grafana/ access (nginx auth_request + auth-proxy wired,
  # Grafana never exposed bare). The 301 for the bare /grafana path is checked
  # too so it can never fall through to the SPA entry page.
  local http_port status
  http_port="$(env_value PUBLIC_PORT)"
  http_port="${http_port:-80}"
  status="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 \
    "http://127.0.0.1:${http_port}/grafana/" || true)"
  if [ "$status" != "401" ]; then
    log "unauthenticated /grafana/ must answer 401 through the platform entry (got ${status})"
    return 1
  fi
  status="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 \
    "http://127.0.0.1:${http_port}/grafana/api/health" || true)"
  if [ "$status" != "401" ]; then
    log "unauthenticated /grafana/api/health must answer 401 through the platform entry (got ${status})"
    return 1
  fi
  status="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 \
    "http://127.0.0.1:${http_port}/grafana" || true)"
  if [ "$status" != "301" ]; then
    log "bare /grafana must 301 to /grafana/ through the platform entry (got ${status})"
    return 1
  fi
  return 0
}

check_kaneo_public_entry() {
  # Basic public entry contract: the plain-HTTP listener only redirects to the
  # canonical HTTPS origin, the gateway answers on HTTPS and the native app
  # health endpoint is reachable through the gateway. Field-level response
  # contracts are asserted by backend/scripts/kaneo_service_e2e.py canary runs.
  local http_port https_port web_url headers redirect_location
  http_port="$(env_value KANEO_HTTP_PORT)"
  https_port="$(env_value KANEO_HTTPS_PORT)"
  http_port="${http_port:-8090}"
  https_port="${https_port:-8445}"
  web_url="$(env_value KANEO_WEB_URL)"
  headers="$(mktemp)"
  if ! curl -fsS -o /dev/null -D "$headers" --connect-timeout 5 --max-time 10 \
      "http://127.0.0.1:${http_port}/"; then
    rm -f "$headers"
    return 1
  fi
  redirect_location="$(
    tr -d '\r' < "$headers" \
      | awk 'tolower($1) == "location:" { $1 = ""; sub(/^ /, ""); print; exit }'
  )"
  rm -f "$headers"
  if [ "$redirect_location" != "${web_url}/" ]; then
    log "Kaneo plain-HTTP entry must redirect to the canonical HTTPS origin ${web_url}"
    return 1
  fi
  curl -kfsS --connect-timeout 5 --max-time 10 \
    "https://127.0.0.1:${https_port}/healthz" >/dev/null || return 1
  curl -kfsS --connect-timeout 5 --max-time 20 \
    "https://127.0.0.1:${https_port}/api/health" >/dev/null || return 1
  # 一键免登录中继链路（kaneo-gateway /sso/entry → backend consume）：无票据
  # 访问必须 302 回根路径且不种 cookie，证明网关→backend 反代与路由已接通。
  local sso_status
  sso_status="$(curl -ksS -o /dev/null -w '%{http_code}' \
    --connect-timeout 5 --max-time 10 \
    "https://127.0.0.1:${https_port}/sso/entry" || true)"
  if [ "$sso_status" != "302" ]; then
    log "Kaneo SSO entry relay must answer 302 on the HTTPS entry (got ${sso_status})"
    return 1
  fi
  return 0
}

if [ "${SKIP_GIT:-0}" != "1" ]; then
  command -v git >/dev/null 2>&1 || { log "git is not installed"; exit 1; }
  if [ ! -d "$APP_DIR/.git" ]; then
    # A source-only clone must never erase a persistent .env or any other
    # operator-owned content. The archive deployment path sets SKIP_GIT=1;
    # direct Git deployment is allowed to claim only a missing or empty path.
    if [ -L "$APP_DIR" ] || { [ -e "$APP_DIR" ] && [ ! -d "$APP_DIR" ]; }; then
      log "$APP_DIR exists but is not a deployable Git worktree; refusing to replace it"
      exit 1
    fi
    if [ -d "$APP_DIR" ]; then
      existing_entry="$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit)"
      if [ -n "$existing_entry" ]; then
        log "$APP_DIR is non-empty and has no .git directory; refusing to delete persistent deployment state"
        exit 1
      fi
      rmdir -- "$APP_DIR"
    fi
    run_with_retry git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$APP_DIR"
  fi
  cd "$APP_DIR"
  run_with_retry git fetch origin "$BRANCH"
  git checkout "$BRANCH"
  git reset --hard "origin/$BRANCH"
else
  cd "$APP_DIR"
fi
# Git restores tracked files with repository modes, and an existing .env may
# predate the hardened deployment path. Restrict both regular files before any
# later validation can fail; never follow or rewrite a .env authority symlink.
if [ -f "$DEPENDENCY_CONFIG_FILE" ]; then
  chmod 600 "$DEPENDENCY_CONFIG_FILE"
fi
if [ -f .env ] && [ ! -L .env ]; then
  chmod 600 .env
fi
random_hex() {
  local bytes="${1:-32}"
  od -An -N "$bytes" -tx1 /dev/urandom | tr -d ' \n'
}
random_service_admin_password() {
  # Native service accounts (WeKnora, Kaneo) accept 8-32 character passwords.
  # Guarantee uppercase, lowercase, digit and special characters with a fixed
  # policy prefix plus 20 hex characters (80 random bits).
  local raw
  raw="$(random_hex 16)"
  printf 'Aa1!%s' "${raw:0:20}"
}
random_fernet_key() {
  # Fernet requires 32 random bytes encoded with URL-safe base64. Thirty-two
  # bytes encode to one 44-character line, so GNU/BSD base64 wrapping differs
  # neither the value nor this portable pipeline.
  head -c 32 /dev/urandom | base64 | tr -d '\r\n' | tr '+/' '-_'
}
set_env_value_in_file() {
  local key="$1"
  local value="$2"
  local target="$3"
  local target_dir target_name tmp
  target_dir="${target%/*}"
  target_name="${target##*/}"
  [ "$target_dir" != "$target" ] || target_dir="."
  # Keep the temporary file beside the target so the final rename is atomic
  # even when /tmp and APP_DIR are different filesystems.
  tmp="$(mktemp "${target_dir}/.${target_name}.update.XXXXXX")"
  chmod 600 "$tmp"
  if ! OPENONTOLOGY_ENV_VALUE="$value" awk -v key="$key" '
    BEGIN { found=0; value=ENVIRON["OPENONTOLOGY_ENV_VALUE"] }
    $0 ~ "^[[:space:]]*" key "=" {
      print key "=" value; found=1; next
    }
    { print }
    END { if (!found) print key "=" value }
  ' "$target" > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  sync "$tmp" 2>/dev/null || sync
  if ! mv "$tmp" "$target"; then
    rm -f "$tmp"
    return 1
  fi
  chmod 600 "$target"
  sync "$target_dir" 2>/dev/null || sync
}
set_env_value() {
  set_env_value_in_file "$1" "$2" "${APP_DIR}/.env"
}
set_env_values_in_file() {
  # Update several dotenv keys in one atomic rewrite so a fresh bootstrap does
  # not pay one fsync per generated WeKnora secret. Values are passed as
  # KEY=value arguments, never printed.
  local target="$1"
  shift
  local target_dir target_name tmp updates
  [ "$#" -gt 0 ] || return 0
  target_dir="${target%/*}"
  target_name="${target##*/}"
  [ "$target_dir" != "$target" ] || target_dir="."
  updates="$(mktemp "${target_dir}/.${target_name}.updates.XXXXXX")"
  tmp="$(mktemp "${target_dir}/.${target_name}.update.XXXXXX")"
  chmod 600 "$updates" "$tmp"
  printf '%s\n' "$@" > "$updates"
  if ! awk '
    NR == FNR {
      separator = index($0, "=")
      if (separator > 1) {
        key = substr($0, 1, separator - 1)
        values[key] = substr($0, separator + 1)
        order[++count] = key
      }
      next
    }
    {
      separator = index($0, "=")
      key = separator > 1 ? substr($0, 1, separator - 1) : ""
      if (key != "" && (key in values)) {
        print key "=" values[key]
        delete values[key]
        next
      }
      print
    }
    END {
      for (index_number = 1; index_number <= count; index_number++) {
        key = order[index_number]
        if (key in values) print key "=" values[key]
      }
    }
  ' "$updates" "$target" > "$tmp"; then
    rm -f "$updates" "$tmp"
    return 1
  fi
  rm -f "$updates"
  sync "$tmp" 2>/dev/null || sync
  if ! mv "$tmp" "$target"; then
    rm -f "$tmp"
    return 1
  fi
  chmod 600 "$target"
  sync "$target_dir" 2>/dev/null || sync
}
bootstrap_production_env() {
  log "production .env is missing; creating a persistent server-side runtime configuration"
  local db_password redis_password neo4j_password minio_access minio_secret
  bootstrap_tmp_path="$(mktemp "${APP_DIR}/.env.bootstrap.XXXXXX")"
  cp .env.example "$bootstrap_tmp_path"
  chmod 600 "$bootstrap_tmp_path"
  db_password="$(random_hex 24)"
  redis_password="$(random_hex 24)"
  neo4j_password="$(random_hex 24)"
  minio_access="onto$(random_hex 10)"
  minio_secret="$(random_hex 24)"
  set_env_value_in_file ENVIRONMENT production "$bootstrap_tmp_path"
  set_env_value_in_file SECRET_KEY "$(random_hex 32)" "$bootstrap_tmp_path"
  set_env_value_in_file ENCRYPTION_KEY "$(random_fernet_key)" "$bootstrap_tmp_path"
  set_env_value_in_file CORS_ALLOWED_ORIGINS "" "$bootstrap_tmp_path"
  set_env_value_in_file FIRST_ADMIN_PASSWORD "$(random_hex 24)" "$bootstrap_tmp_path"
  set_env_value_in_file POSTGRES_PASSWORD "$db_password" "$bootstrap_tmp_path"
  set_env_value_in_file DATABASE_URL \
    "postgresql://ontoprompt:${db_password}@db:5432/ontoprompt" \
    "$bootstrap_tmp_path"
  set_env_value_in_file REDIS_PASSWORD "$redis_password" "$bootstrap_tmp_path"
  set_env_value_in_file REDIS_URL \
    "redis://:${redis_password}@redis:6379/0" "$bootstrap_tmp_path"
  set_env_value_in_file NEO4J_PASSWORD "$neo4j_password" "$bootstrap_tmp_path"
  set_env_value_in_file NEO4J_AUTH \
    "neo4j/${neo4j_password}" "$bootstrap_tmp_path"
  set_env_value_in_file MINIO_ACCESS_KEY "$minio_access" "$bootstrap_tmp_path"
  set_env_value_in_file MINIO_SECRET_KEY "$minio_secret" "$bootstrap_tmp_path"
  # Keep the historical local:// root readable during migration. New writes
  # are always sent to MinIO and never fall back to this directory.
  set_env_value_in_file STORAGE_LOCAL_DIR \
    /uploads/object-storage "$bootstrap_tmp_path"
  set_env_value_in_file API_HUB_SYSTEM_MCP_TOKEN \
    "$(random_hex 32)" "$bootstrap_tmp_path"
  set_env_value_in_file PYTHON_KERNEL_GATEWAY_AUTH_TOKEN \
    "$(random_hex 32)" "$bootstrap_tmp_path"
  # 记忆栈生产默认启用（开箱即用）：凭据/端口由 configure_hindsight_env
  # 生成；栈本体的拉取与启动仍后置到核心部署成功之后，失败只降级记忆特性。
  # 运维显式写 false 即可关闭（存量 false 服务器见 configure_hindsight_env
  # 的一次性迁移）。
  set_env_value_in_file HINDSIGHT_ENABLED true "$bootstrap_tmp_path"
  # Multica 默认栈同样生产默认启用（与本地 COMPOSE_PROFILES=multica 默认
  # 一致）：镜像 pin/端口/栈密钥由 configure_multica_env 生成，五项
  # MULTICA_DEFAULT_* 保持全空（空卷的账号/PAT/workspace 必须由管理员在
  # 原生界面创建后回填，部署绝不代写）；栈本体由
  # start_multica_default_stack 在 readiness 门禁等待前有界启动。运维显式
  # 写 false 即可关闭。
  set_env_value_in_file MULTICA_ENABLED true "$bootstrap_tmp_path"
  # COMPOSE_PROFILES 是本地开发旋钮（.env.example 默认 multica）：服务器
  # 上 profile 归 deploy-prod.sh 独占（compose_environment 固定空值），
  # 引导时显式置空，运维手工 compose 调用不会意外激活 multica profile。
  set_env_value_in_file COMPOSE_PROFILES "" "$bootstrap_tmp_path"
  sync "$bootstrap_tmp_path" 2>/dev/null || sync
  # Publishing with a same-directory hard link is atomic and never replaces
  # an existing file, directory or dangling secret-mount symlink. A check then
  # mv sequence would have a race that could overwrite recovered authority.
  if ! ln "$bootstrap_tmp_path" "${APP_DIR}/.env"; then
    log "production .env already exists or appeared during bootstrap; refusing to overwrite it"
    return 1
  fi
  rm -f -- "$bootstrap_tmp_path"
  bootstrap_tmp_path=""
  chmod 600 "${APP_DIR}/.env"
  sync "$APP_DIR" 2>/dev/null || sync
  log "generated runtime secrets were stored in ${APP_DIR}/.env (values are not printed to CI logs)"
}
if [ -L .env ]; then
  log "production .env must be a regular server-side file; refusing to replace a secret-authority symlink"
  exit 1
fi
if [ ! -f .env ]; then
  if [ "${BOOTSTRAP_PRODUCTION_ENV:-0}" != "1" ]; then
    log "production .env is missing; refusing to generate new encryption authority without an explicit fresh-install confirmation"
    log "restore the original .env for existing data, or set BOOTSTRAP_PRODUCTION_ENV=1 only after proving every persistent store is empty"
    exit 1
  fi
  bootstrap_production_env
fi

env_value() {
  local key="$1"
  awk -v key="$key" '
    $0 ~ "^[[:space:]]*" key "=" {
      line=$0; sub("^[[:space:]]*" key "=", "", line); value=line
    }
    END { gsub(/\r$/, "", value); print value }
  ' .env
}
runtime_env_assignment_state() {
  local key="$1"
  LC_ALL=C awk -v key="$key" '
    BEGIN {
      canonical=0
      ambiguous=0
      lowercase_key=tolower(key)
    }
    /^[[:space:]]*#/ { next }
    {
      if ($0 ~ "^[[:space:]]*" key "=") {
        canonical++
        next
      }
      lowercase_line=tolower($0)
      if (lowercase_line ~ "^[[:space:]]*(export[[:space:]]+)?" \
          lowercase_key "[[:space:]]*=") {
        ambiguous=1
      }
    }
    END {
      if (ambiguous || canonical > 1) print "ambiguous"
      else if (canonical) print "canonical"
      else print "missing"
    }
  ' .env
}
preflight_runtime_secret_authority() {
  local runtime_key assignment_state current_secret current_encryption
  for runtime_key in SECRET_KEY ENCRYPTION_KEY FIRST_ADMIN_PASSWORD; do
    if ! assignment_state="$(runtime_env_assignment_state "$runtime_key")"; then
      log "could not read $runtime_key from production .env"
      exit 1
    fi
    case "$assignment_state" in
      ambiguous)
        log "$runtime_key must use one canonical uppercase KEY=value assignment before automatic secret migration"
        exit 1
        ;;
      canonical|missing) ;;
      *)
        log "could not classify $runtime_key in production .env"
        exit 1
        ;;
    esac
  done
  current_secret="$(env_value SECRET_KEY)"
  current_encryption="$(env_value ENCRYPTION_KEY)"
  if [ -n "$current_encryption" ] \
      && [[ ! "$current_encryption" =~ ^[A-Za-z0-9_-]{43}=$ ]]; then
    log "ENCRYPTION_KEY must be a URL-safe base64 Fernet key"
    exit 1
  fi
  case "$current_secret" in
    ""|dev-secret-key|change-me-to-a-random-32-char-string) ;;
    *)
      if [ "${#current_secret}" -lt 32 ]; then
        log "SECRET_KEY must contain at least 32 characters"
        exit 1
      fi
      ;;
  esac
}
# Validate every protected authority before applying even non-secret manifest
# updates. Invalid or ambiguous historical dotenv syntax therefore leaves the
# complete persistent file byte-for-byte untouched for operator recovery.
preflight_runtime_secret_authority

integrated_dbx_origin_is_valid() {
  local raw="$1" port
  [[ "$raw" =~ ^https?://(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+)(:([0-9]{1,5}))?/?$ ]] || return 1
  port="${BASH_REMATCH[3]:-}"
  [ -z "$port" ] || [ "$port" -le 65535 ]
}

configure_integrated_dbx() {
  # Server-side .env authority only; no GitHub secret or manifest key is added.
  local url password commit generated=0
  url="$(env_value INTEGRATED_DBX_URL)"
  password="$(env_value INTEGRATED_DBX_PASSWORD)"
  commit="$(env_value INTEGRATED_DBX_COMMIT)"
  if [ -z "$url" ] && [ -z "$password" ]; then
    url="http://dbx:4224"
    password="$(random_hex 32)"
    generated=1
    INTEGRATED_DBX_INTERNAL=1
  elif [ -n "$url" ] && [ -n "$password" ]; then
    if [ "$url" = "http://dbx:4224" ]; then
      INTEGRATED_DBX_INTERNAL=1
    else
      INTEGRATED_DBX_INTERNAL=0
    fi
  else
    log "INTEGRATED_DBX_URL and INTEGRATED_DBX_PASSWORD must be both set or both empty; refusing a partial external configuration"
    exit 1
  fi
  if [ -z "$commit" ]; then
    commit="$INTEGRATED_DBX_COMMIT_PIN"
  fi
  if [ "$commit" != "$INTEGRATED_DBX_COMMIT_PIN" ]; then
    log "INTEGRATED_DBX_COMMIT must equal the pinned dbx release commit"
    exit 1
  fi
  if ! integrated_dbx_origin_is_valid "$url"; then
    log "INTEGRATED_DBX_URL must be an absolute HTTP(S) origin without credentials, path, query, or fragment"
    exit 1
  fi
  if [ "$generated" = "1" ]; then
    set_env_value INTEGRATED_DBX_URL "$url"
    set_env_value INTEGRATED_DBX_PASSWORD "$password"
    set_env_value INTEGRATED_DBX_COMMIT "$commit"
    log "configured internal dbx metadata service with a generated server-side password"
  else
    set_env_value INTEGRATED_DBX_COMMIT "$commit"
    log "preserved explicit dbx metadata configuration (internal=${INTEGRATED_DBX_INTERNAL})"
  fi
}

ensure_public_tls_ip() {
  # The WeKnora/Kaneo public HTTPS origins derive from the server's literal
  # public IP. An existing operator-configured PUBLIC_TLS_IP always wins; only
  # a missing or empty value is initialized exactly once from the deployment
  # health URL's host and persisted into .env. Hostnames, loopback, private
  # and non-literal hosts are rejected: this deployment model is a bare public
  # IP with dedicated ports, and a wrong silent guess would strand the public
  # gateways behind origins nothing can reach.
  local current candidate
  current="$(env_value PUBLIC_TLS_IP)"
  if [ -n "$current" ]; then
    return 0
  fi
  if [ -z "$HEALTH_URL" ]; then
    log "PUBLIC_TLS_IP is not configured and no deployment health URL is available to derive it; set PUBLIC_TLS_IP in ${APP_DIR}/.env or deploy with an explicit HEALTH_URL using the public IP"
    exit 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    log "PUBLIC_TLS_IP cannot be derived without python3; set it explicitly in ${APP_DIR}/.env"
    exit 1
  fi
  # Strict validation without echoing any part of HEALTH_URL: scheme limited
  # to http(s), no credentials/query/fragment, a valid optional port, and a
  # literal routable public IP (IPv4 or IPv6, stored in canonical form).
  if ! candidate="$(
    python3 - "$HEALTH_URL" <<'PY'
import ipaddress
import sys
from urllib.parse import urlsplit

try:
    parsed = urlsplit(sys.argv[1])
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("scheme")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("query or fragment")
    _ = parsed.port  # raises ValueError for an out-of-range port
    address = ipaddress.ip_address((parsed.hostname or "").strip("[]"))
except ValueError:
    sys.exit(1)
if address.is_loopback or not address.is_global:
    sys.exit(1)
print(address.compressed)
PY
  )"; then
    log "HEALTH_URL must use a literal public IP host to initialize PUBLIC_TLS_IP; set PUBLIC_TLS_IP explicitly for hostname or private-address deployments"
    exit 1
  fi
  set_env_value PUBLIC_TLS_IP "$candidate"
  log "initialized PUBLIC_TLS_IP from the deployment health URL host (persisted in ${APP_DIR}/.env)"
}

configure_weknora_env() {
  # Keep independent database/cache and encryption credentials stable across
  # deployments. Models and MCP tokens belong to the native UI, not this file.
  local current web_url tls_ip https_port
  local -a updates=()
  updates+=("WEKNORA_BASE_URL=http://weknora-app:8080")
  updates+=("SUPER_ASSISTANT_MCP_INTERNAL_BASE_URLS=http://weknora-app:8080/mcp")
  current="$(env_value WEKNORA_TIMEOUT_SECONDS)"
  [ -n "$current" ] || updates+=("WEKNORA_TIMEOUT_SECONDS=15")
  current="$(env_value WEKNORA_HTTP_PORT)"
  [ -n "$current" ] || updates+=("WEKNORA_HTTP_PORT=8089")
  current="$(env_value WEKNORA_HTTPS_PORT)"
  [ -n "$current" ] || updates+=("WEKNORA_HTTPS_PORT=8444")
  current="$(env_value WEKNORA_MAX_FILE_SIZE_MB)"
  [ -n "$current" ] || updates+=("WEKNORA_MAX_FILE_SIZE_MB=50")
  current="$(env_value WEKNORA_DEFAULT_LOCALE)"
  [ -n "$current" ] || updates+=("WEKNORA_DEFAULT_LOCALE=zh-CN")

  current="$(env_value WEKNORA_DB_NAME)"
  [ -n "$current" ] || updates+=("WEKNORA_DB_NAME=weknora")
  current="$(env_value WEKNORA_DB_USER)"
  [ -n "$current" ] || updates+=("WEKNORA_DB_USER=weknora")
  for secret_assignment in \
    "WEKNORA_DB_PASSWORD:24" \
    "WEKNORA_REDIS_PASSWORD:24" \
    "WEKNORA_JWT_SECRET:32" \
    "WEKNORA_SYSTEM_AES_KEY:16" \
    "WEKNORA_SYSTEM_SIGNING_KEY:32"; do
    current="$(env_value "${secret_assignment%%:*}")"
    case "$current" in
      ""|change-me|admin123|weknora-local-password|\
      weknora-local-redis-password|weknora-local-neo4j-password|\
      weknora-local-jwt-secret-change-me|\
      weknora-local-aes-key-change-me-32|\
      weknora-local-signing-key-change-me)
        updates+=("${secret_assignment%%:*}=$(random_hex "${secret_assignment#*:}")")
        log "generated independent WeKnora runtime secret: ${secret_assignment%%:*}"
        ;;
    esac
  done
  current="$(env_value WEKNORA_ADMIN_PASSWORD)"
  case "$current" in
    ""|change-me|admin123)
      updates+=("WEKNORA_ADMIN_PASSWORD=$(random_service_admin_password)")
      log "generated independent WeKnora administrator password"
      ;;
  esac

  current="$(env_value WEKNORA_ADMIN_USER)"
  if [ -z "$current" ]; then
    current="$(env_value FIRST_ADMIN_USER)"
    updates+=("WEKNORA_ADMIN_USER=${current:-admin}")
  fi
  current="$(env_value WEKNORA_ADMIN_EMAIL)"
  [ -n "$current" ] || updates+=("WEKNORA_ADMIN_EMAIL=admin@weknora.local")

  current="$(env_value WEKNORA_WEB_URL)"
  if [ -z "$current" ]; then
    tls_ip="$(env_value PUBLIC_TLS_IP)"
    https_port="$(env_value WEKNORA_HTTPS_PORT)"
    https_port="${https_port:-8444}"
    if [ -z "$tls_ip" ]; then
      log "WEKNORA_WEB_URL is required when PUBLIC_TLS_IP is empty; set the real public WeKnora origin explicitly"
      exit 1
    fi
    case "$tls_ip" in
      *:*) web_url="https://[${tls_ip}]:${https_port}" ;;
      *) web_url="https://${tls_ip}:${https_port}" ;;
    esac
    updates+=("WEKNORA_WEB_URL=${web_url}")
    log "configured WEKNORA_WEB_URL from PUBLIC_TLS_IP and WEKNORA_HTTPS_PORT"
  fi

  # Official v0.9.0 image names with verified digests. Existing values are
  # preserved; the production digest gate still rejects tag drift.
  for image_assignment in \
    "WEKNORA_APP_IMAGE=wechatopenai/weknora-app:v0.9.0@sha256:25b20959b7456f7842ef55ed4feff3694dbd0c6f8af4117c1fb3c5dec6bdf14d" \
    "WEKNORA_UI_IMAGE=wechatopenai/weknora-ui:v0.9.0@sha256:df26705d866997db07f3da78dabedc4f594b383566a3996810c166a5c6bee393" \
    "WEKNORA_DOCREADER_IMAGE=wechatopenai/weknora-docreader:v0.9.0@sha256:8c71ec3cc2e291829c3938d2c7dc5f921c1d10551c8774bf844c83bc4aa3cb84" \
    "WEKNORA_POSTGRES_IMAGE=paradedb/paradedb:v0.25.11-pg17@sha256:78d65750f0f88a56548526201bcab9910f83b90fe929cdbf979890f3f80b0f95" \
    "WEKNORA_REDIS_IMAGE=redis:7-alpine@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99"; do
    current="$(env_value "${image_assignment%%=*}")"
    [ -n "$current" ] || updates+=("$image_assignment")
  done
  set_env_values_in_file "${APP_DIR}/.env" "${updates[@]}"
}

configure_kaneo_env() {
  # Keep Kaneo's own database and native-account credentials stable across
  # deployments. Project management data belongs to Kaneo's Web UI, not here.
  local current web_url tls_ip https_port
  local -a updates=()
  updates+=("KANEO_BASE_URL=http://kaneo-app:5173")
  current="$(env_value KANEO_TIMEOUT_SECONDS)"
  [ -n "$current" ] || updates+=("KANEO_TIMEOUT_SECONDS=15")
  current="$(env_value KANEO_HTTP_PORT)"
  [ -n "$current" ] || updates+=("KANEO_HTTP_PORT=8090")
  current="$(env_value KANEO_HTTPS_PORT)"
  [ -n "$current" ] || updates+=("KANEO_HTTPS_PORT=8445")

  current="$(env_value KANEO_DB_NAME)"
  [ -n "$current" ] || updates+=("KANEO_DB_NAME=kaneo")
  current="$(env_value KANEO_DB_USER)"
  [ -n "$current" ] || updates+=("KANEO_DB_USER=kaneo")
  for secret_assignment in "KANEO_DB_PASSWORD:24" "KANEO_AUTH_SECRET:32"; do
    current="$(env_value "${secret_assignment%%:*}")"
    case "$current" in
      ""|change-me|admin123|kaneo-local-db-password|\
      kaneo-local-auth-secret-change-me-2026)
        updates+=("${secret_assignment%%:*}=$(random_hex "${secret_assignment#*:}")")
        log "generated independent Kaneo runtime secret: ${secret_assignment%%:*}"
        ;;
    esac
  done
  current="$(env_value KANEO_ADMIN_PASSWORD)"
  case "$current" in
    ""|change-me|admin123|localKaneoAdmin2026!)
      updates+=("KANEO_ADMIN_PASSWORD=$(random_service_admin_password)")
      log "generated independent Kaneo administrator password"
      ;;
  esac

  current="$(env_value KANEO_ADMIN_USER)"
  if [ -z "$current" ]; then
    current="$(env_value FIRST_ADMIN_USER)"
    updates+=("KANEO_ADMIN_USER=${current:-admin}")
  fi
  current="$(env_value KANEO_ADMIN_EMAIL)"
  [ -n "$current" ] || updates+=("KANEO_ADMIN_EMAIL=admin@kaneo.local")

  current="$(env_value KANEO_WEB_URL)"
  # KANEO_API_URL and the gateway redirect append paths to this origin, so a
  # trailing slash would produce "//api" and "//" targets; normalize in place.
  web_url="$current"
  while [ "${web_url%/}" != "$web_url" ]; do
    web_url="${web_url%/}"
  done
  if [ -z "$web_url" ]; then
    tls_ip="$(env_value PUBLIC_TLS_IP)"
    https_port="$(env_value KANEO_HTTPS_PORT)"
    https_port="${https_port:-8445}"
    if [ -z "$tls_ip" ]; then
      log "KANEO_WEB_URL is required when PUBLIC_TLS_IP is empty; set the real public Kaneo HTTPS origin explicitly"
      exit 1
    fi
    case "$tls_ip" in
      *:*) web_url="https://[${tls_ip}]:${https_port}" ;;
      *) web_url="https://${tls_ip}:${https_port}" ;;
    esac
    updates+=("KANEO_WEB_URL=${web_url}")
    log "configured KANEO_WEB_URL from PUBLIC_TLS_IP and KANEO_HTTPS_PORT"
  elif [ "$web_url" != "$current" ]; then
    updates+=("KANEO_WEB_URL=${web_url}")
    log "normalized KANEO_WEB_URL by removing its trailing slash"
  fi

  # Official Kaneo 2.49.0 image with a directly verified multiarch digest; the
  # dedicated Kaneo database reuses the platform's pinned postgres. Existing
  # values are preserved; the production digest gate still rejects tag drift.
  for image_assignment in \
    "KANEO_APP_IMAGE=ghcr.io/usekaneo/kaneo:2.49.0@sha256:cd1826b1aba3ce736015cadf6f6c975d923aac00f4b6aaa7aedb29bc722dae49" \
    "KANEO_POSTGRES_IMAGE=postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"; do
    current="$(env_value "${image_assignment%%=*}")"
    [ -n "$current" ] || updates+=("$image_assignment")
  done
  set_env_values_in_file "${APP_DIR}/.env" "${updates[@]}"
}

configure_grafana_env() {
  # The shared Grafana administrator password is generated once and stays
  # stable across deployments (GF_SECURITY_ADMIN_PASSWORD only takes effect
  # on the first volume initialization; re-running bootstrap never resets an
  # existing Grafana database). The value is never echoed into logs. Fresh and
  # pre-Grafana .env files get every required key materialized here so
  # validation cannot fail on a missing value; already persisted values are
  # never rewritten.
  local current
  local -a updates=()
  updates+=("GRAFANA_BASE_URL=http://grafana:3000")
  current="$(env_value GRAFANA_TIMEOUT_SECONDS)"
  [ -n "$current" ] || updates+=("GRAFANA_TIMEOUT_SECONDS=15")
  # The Grafana shared administrator is a dedicated service account fixed by
  # contract; it deliberately does not inherit the platform FIRST_ADMIN_USER.
  current="$(env_value GRAFANA_ADMIN_USER)"
  [ -n "$current" ] || updates+=("GRAFANA_ADMIN_USER=admin")
  current="$(env_value GRAFANA_ADMIN_PASSWORD)"
  case "$current" in
    ""|change-me|admin123|localGrafanaAdmin2026!)
      updates+=("GRAFANA_ADMIN_PASSWORD=$(random_service_admin_password)")
      log "generated independent Grafana administrator password"
      ;;
  esac
  # The board session cookie TTL defaults to the delivered 8h contract value
  # (28800) when a fresh/legacy .env has no key yet.
  current="$(env_value GRAFANA_SESSION_TTL_SECONDS)"
  [ -n "$current" ] || updates+=("GRAFANA_SESSION_TTL_SECONDS=28800")
  # Platform folder identity is part of the UID reconciliation contract.
  current="$(env_value GRAFANA_FOLDER_UID)"
  [ -n "$current" ] || updates+=("GRAFANA_FOLDER_UID=openontology")
  current="$(env_value GRAFANA_FOLDER_TITLE)"
  [ -n "$current" ] || updates+=("GRAFANA_FOLDER_TITLE=OpenOntology 监控看板")
  # Official Grafana 13.3.1 image with a directly verified multiarch digest
  # (both independent mirrors returned the identical manifest). Existing
  # values are preserved; the production digest gate still rejects tag drift.
  current="$(env_value GRAFANA_IMAGE)"
  [ -n "$current" ] || updates+=(
    "GRAFANA_IMAGE=grafana/grafana:13.3.1@sha256:0615ec4fbd4072004b7d86b46ded63013a74b7d6eb1119273dd89e8fa25c13cf"
  )
  set_env_values_in_file "${APP_DIR}/.env" "${updates[@]}"
}

hindsight_web_url_origin_ok() {
  # HINDSIGHT_WEB_URL 的部署期形态校验：http(s) origin（可带端口/IPv6 括号），
  # 不带凭据/路径/查询/fragment；允许单个尾部斜杠（backend 会归一化）。运行期
  # backend memory settings 仍会做同样的净化兜底，这里只负责把配置 typo 在
  # 部署期暴露出来。
  local value="${1:-}" rest
  case "$value" in
    http://*|https://*) ;;
    *) return 1 ;;
  esac
  case "$value" in
    *\#*|*\?*|*@*) return 1 ;;
  esac
  rest="${value#*://}"
  case "$rest" in
    /) return 1 ;;
    */) rest="${rest%/}" ;;
  esac
  case "$rest" in
    */*) return 1 ;;
  esac
  [ -n "$rest" ] || return 1
}

check_image_digest() {
  local key="$1"
  local value
  value="$(env_value "$key")"
  if [[ ! "$value" =~ @sha256:[0-9a-fA-F]{64}$ ]]; then
    if [ "$strict_image_digests_enabled" = "1" ]; then
      log "$key must be pinned to an immutable @sha256 digest"
      exit 1
    fi
    log "$key must be pinned to an immutable @sha256 digest"
    exit 1
  fi
}

configure_hindsight_env() {
  # Hindsight 长期记忆：生产默认启用（开箱即用），运维可随时显式关闭。
  # 历史模板/引导曾默认 false；对"从未开通"的存量服务器做一次性迁移——
  # 指纹取 HINDSIGHT_WEB_PORT（它只在启用后被本函数写入，不随依赖清单
  # 物化）。首次启用即生成全部凭据与端口并落 .env，指纹自然分化；此后
  # 运维显式 false 一律尊重，不再翻动。
  local current
  local -a updates=()
  if [ "$(env_value HINDSIGHT_ENABLED)" != "true" ] \
    && [ -z "$(env_value HINDSIGHT_WEB_PORT)" ]; then
    set_env_value HINDSIGHT_ENABLED true
    log "migrated HINDSIGHT_ENABLED=true (the memory stack now deploys on by default; write false explicitly to opt out)"
  fi
  case "$(env_value HINDSIGHT_ENABLED)" in
    1|true|TRUE|yes|YES) HINDSIGHT_INTERNAL=1 ;;
    ""|0|false|FALSE|no|NO) HINDSIGHT_INTERNAL=0 ;;
    *)
      log "HINDSIGHT_ENABLED must be true or false"
      exit 1
      ;;
  esac
  if [ "$HINDSIGHT_INTERNAL" = "0" ]; then
    log "Hindsight memory profile is disabled (HINDSIGHT_ENABLED!=true)"
    return 0
  fi
  updates+=("HINDSIGHT_BASE_URL=http://hindsight:8888")
  updates+=("HINDSIGHT_ENABLED=true")
  # 召回全链（query embedding + 检索 + source_facts 生成）在真实负载下
  # 超过旧默认 2 秒，聊天/面板召回会以「记忆服务不可用」整体失败；默认
  # 提到 5 秒，并对仍持有旧默认值（2/2.0）的存量 .env 做一次性迁移——
  # 运维显式写入的其它值一律保留。
  current="$(env_value HINDSIGHT_TIMEOUT_SECONDS)"
  case "$current" in
    ""|2|2.0)
      updates+=("HINDSIGHT_TIMEOUT_SECONDS=5")
      [ -n "$current" ] && log "migrated HINDSIGHT_TIMEOUT_SECONDS=5 (the recall chain needs more than the old 2s default; write your own value to override)"
      ;;
  esac
  current="$(env_value HINDSIGHT_NAMESPACE)"
  [ -n "$current" ] || updates+=("HINDSIGHT_NAMESPACE=openontology")
  current="$(env_value HINDSIGHT_DB_NAME)"
  [ -n "$current" ] || updates+=("HINDSIGHT_DB_NAME=hindsight")
  current="$(env_value HINDSIGHT_DB_USER)"
  [ -n "$current" ] || updates+=("HINDSIGHT_DB_USER=hindsight")
  # 管理 UI（Control Plane）的宿主发布端口；入口 URL 由此推导。
  current="$(env_value HINDSIGHT_WEB_PORT)"
  [ -n "$current" ] || updates+=("HINDSIGHT_WEB_PORT=19999")
  for secret_assignment in \
    "HINDSIGHT_DB_PASSWORD:24" \
    "HINDSIGHT_API_KEY:32" \
    "HINDSIGHT_SERVICE_TOKEN:32" \
    "HINDSIGHT_CP_ACCESS_KEY:8"; do
    current="$(env_value "${secret_assignment%%:*}")"
    case "$current" in
      ""|change-me|\
      hindsight-local-password|hindsight-local-api-key|hindsight-local-service-token)
        updates+=("${secret_assignment%%:*}=$(random_hex "${secret_assignment#*:}")")
        log "generated independent Hindsight runtime secret: ${secret_assignment%%:*}"
        ;;
    esac
  done
  # 默认管理员入口：未显式配置 HINDSIGHT_WEB_URL 时按 PUBLIC_TLS_IP +
  # HINDSIGHT_WEB_PORT 推导（与 WEKNORA_WEB_URL 同构）。控制面由上方
  # HINDSIGHT_CP_ACCESS_KEY 登录保护，运维可随时改写为自有受护地址。
  current="$(env_value HINDSIGHT_WEB_URL)"
  if [ -z "$current" ]; then
    tls_ip="$(env_value PUBLIC_TLS_IP)"
    web_port="$(env_value HINDSIGHT_WEB_PORT)"
    web_port="${web_port:-19999}"
    if [ -z "$tls_ip" ]; then
      log "HINDSIGHT_WEB_URL is required when PUBLIC_TLS_IP is empty; set the Hindsight control-plane origin explicitly to keep the admin entry"
      exit 1
    fi
    case "$tls_ip" in
      *:*) web_url="http://[${tls_ip}]:${web_port}" ;;
      *) web_url="http://${tls_ip}:${web_port}" ;;
    esac
    updates+=("HINDSIGHT_WEB_URL=${web_url}")
    log "configured HINDSIGHT_WEB_URL from PUBLIC_TLS_IP and HINDSIGHT_WEB_PORT"
  fi
  # 官方 1.2.0 镜像与 DB 镜像的已验证 digest。已存在的值（含运维自选的其它
  # @sha256 钉扎）一律保留；validate 阶段统一拒绝非 digest 引用。
  current="$(env_value HINDSIGHT_IMAGE)"
  [ -n "$current" ] || updates+=("HINDSIGHT_IMAGE=${HINDSIGHT_IMAGE_PIN}")
  current="$(env_value HINDSIGHT_POSTGRES_IMAGE)"
  [ -n "$current" ] || updates+=("HINDSIGHT_POSTGRES_IMAGE=${HINDSIGHT_POSTGRES_IMAGE_PIN}")
  set_env_values_in_file "${APP_DIR}/.env" "${updates[@]}"
}

configure_multica_env() {
  # Multica 默认服务（profile `multica`，生产默认启用，与本地默认一致：
  # MULTICA_ENABLED 留空/true 均为启用，仅显式 false 关闭——multica 是新
  # 能力，没有历史 false 模板需要迁移，运维显式写下的 false 永远被尊重）。
  # 官方 v0.11.0 栈契约已核验：与 Hindsight 同构地落 pin 与密钥。五项默认
  # 集成值 all-or-nothing 且保持可选：全空合法（栈先跑起来，管理员在原生
  # 界面完成账号/PAT/workspace 后再回填；此时 readiness 不门禁
  # multica_default），部分填写是部署错误（fail-closed），完整填写才启用
  # 部署级默认集成（此后生产 readiness 门禁 multica_default）。PAT 由管理
  # 员在 Multica 原生界面/官方 CLI 创建后填入服务器 .env（或经受控 manifest
  # 物化）——部署绝不宣称空卷已初始化账号/PAT/workspace。
  local current base_url web_url bind_addr ws_id ws_name default_pat tls_ip https_port
  local gateway_web_origin
  local -a updates=()
  case "$(env_value MULTICA_ENABLED)" in
    1|true|TRUE|yes|YES|"") MULTICA_INTERNAL=1 ;;
    0|false|FALSE|no|NO) MULTICA_INTERNAL=0 ;;
    *)
      log "MULTICA_ENABLED must be true or false"
      exit 1
      ;;
  esac
  if [ "$MULTICA_INTERNAL" = "0" ]; then
    log "Multica default service profile is disabled (MULTICA_ENABLED=false); the bundled stack stays stopped and the five-value default is validated as-is"
    set_env_value MULTICA_ENABLED false
    return 0
  fi
  updates+=("MULTICA_ENABLED=true")
  # 官方镜像 digest pin：已存在的值（含运维自选的其它 @sha256 钉扎）一律
  # 保留；validate 阶段统一拒绝非 digest 引用。
  current="$(env_value MULTICA_BACKEND_IMAGE)"
  [ -n "$current" ] || updates+=("MULTICA_BACKEND_IMAGE=${MULTICA_BACKEND_IMAGE_PIN}")
  current="$(env_value MULTICA_WEB_IMAGE)"
  [ -n "$current" ] || updates+=("MULTICA_WEB_IMAGE=${MULTICA_WEB_IMAGE_PIN}")
  current="$(env_value MULTICA_POSTGRES_IMAGE)"
  [ -n "$current" ] || updates+=("MULTICA_POSTGRES_IMAGE=${MULTICA_POSTGRES_IMAGE_PIN}")
  # web 宿主发布端口与绑定地址。Multica web 自带账号登录/PAT 铸造界面，
  # 不得明文公网暴露：默认只绑 loopback（SSH 隧道或同宿主 TLS 反代使用），
  # 需要反代跨机到达时才显式放开 MULTICA_WEB_BIND_ADDR（须为 IP 字面量，
  # 且必须置于 TLS 反代之后）。API 容器不发布宿主端口。
  # bundled multica-gateway 的公开 TLS 端口（与 weknora/kaneo 网关同构，
  # 复用 frontend_ssl_certs 证书）：公开 HTTPS origin 由该网关提供，loopback
  # 宿主端口仍只服务 SSH 隧道/自备反代。
  current="$(env_value MULTICA_HTTPS_PORT)"
  [ -n "$current" ] || updates+=("MULTICA_HTTPS_PORT=8446")
  current="$(env_value MULTICA_WEB_PORT)"
  [ -n "$current" ] || updates+=("MULTICA_WEB_PORT=18100")
  bind_addr="$(env_value MULTICA_WEB_BIND_ADDR)"
  if [ -z "$bind_addr" ]; then
    updates+=("MULTICA_WEB_BIND_ADDR=127.0.0.1")
    log "Multica web UI is bound to loopback only (default MULTICA_WEB_BIND_ADDR=127.0.0.1); access it via an SSH tunnel or a same-host TLS reverse proxy, never expose the plaintext login/PAT UI"
  fi
  # Multica 栈自身密钥：留空/本地开发兜底值时生成强随机值（与 Hindsight
  # 密钥同构；JWT_SECRET 用于 Multica 自身会话签名，与平台凭据无关）。
  for secret_assignment in "MULTICA_JWT_SECRET:32" "MULTICA_POSTGRES_PASSWORD:24"; do
    current="$(env_value "${secret_assignment%%:*}")"
    case "$current" in
      ""|multica-local-jwt-secret-change-me|multica-local-password)
        updates+=("${secret_assignment%%:*}=$(random_hex "${secret_assignment#*:}")")
        log "generated independent Multica runtime secret: ${secret_assignment%%:*}"
        ;;
    esac
  done
  # 五值默认集的推导只在“其余四值已齐、仅差派生项”时进行：全空集合保持
  # 全空（栈正常启动、无部署级默认，readiness 不门禁 multica_default——
  # 管理员在原生界面完成账号/PAT/workspace 后再回填），部分填写则原样交给
  # validate 的 all-or-nothing fail-closed，绝不替管理员把半成品配置补成
  # “完整”。
  base_url="$(env_value MULTICA_DEFAULT_BASE_URL)"
  web_url="$(env_value MULTICA_DEFAULT_WEB_URL)"
  ws_id="$(env_value MULTICA_DEFAULT_WORKSPACE_ID)"
  ws_name="$(env_value MULTICA_DEFAULT_WORKSPACE_NAME)"
  default_pat="$(env_value MULTICA_DEFAULT_PAT)"
  if [ -n "$ws_id" ] && [ -n "$ws_name" ] && [ -n "$default_pat" ]; then
    # bundled 部署的默认内网地址：未显式配置时按官方容器端口契约（API 固定
    # 8080）推导并落 .env（显式配置一律尊重，可指向外部 Multica）。
    if [ -z "$base_url" ]; then
      base_url="http://multica:8080"
      updates+=("MULTICA_DEFAULT_BASE_URL=${base_url}")
      log "configured MULTICA_DEFAULT_BASE_URL from the bundled multica service (official container contract: API on 8080)"
    fi
    # 浏览器入口 WEB_URL 按默认集指向的 Multica 分两种契约：
    #  - bundled 栈（BASE_URL 为推导值或显式的官方内网 origin）：WEB_URL 缺省
    #    时取 multica-gateway 的公开 origin（PUBLIC_TLS_IP +
    #    MULTICA_HTTPS_PORT，与 WEKNORA/KANEO_WEB_URL 的推导同构）——本部署
    #    对 bundled 栈发布的公开 Web 入口只有这个网关。已有显式值一律尊重
    #    （清单里的值必须原样落到 .env，与其它键一致；自备反代的部署靠它保留
    #    自己的 origin），与网关 origin 不一致时只告警不改写，提醒核对。
    #  - 外部 Multica（BASE_URL 指向别处）：平台无从得知其 Web 入口，显式值
    #    一律尊重；缺失时不代写（更不能套用 bundled 网关地址），交由 validate
    #    的 all-or-nothing 拒绝。
    if [ -z "$web_url" ]; then
      if [ "${base_url%/}" = "http://multica:8080" ]; then
        tls_ip="$(env_value PUBLIC_TLS_IP)"
        https_port="$(env_value MULTICA_HTTPS_PORT)"
        https_port="${https_port:-8446}"
        if [ -n "$tls_ip" ]; then
          case "$tls_ip" in
            *:*) web_url="https://[${tls_ip}]:${https_port}" ;;
            *) web_url="https://${tls_ip}:${https_port}" ;;
          esac
          updates+=("MULTICA_DEFAULT_WEB_URL=${web_url}")
          log "configured MULTICA_DEFAULT_WEB_URL from PUBLIC_TLS_IP and MULTICA_HTTPS_PORT (bundled multica-gateway)"
        else
          log "MULTICA_DEFAULT_WEB_URL is not set and PUBLIC_TLS_IP is empty; set the browser-reachable Multica web origin explicitly so the five-value default can take effect"
        fi
      else
        log "MULTICA_DEFAULT_WEB_URL is required when the default points at an external Multica; set its browser-reachable web origin explicitly so the five-value default can take effect"
      fi
    elif [ "${base_url%/}" = "http://multica:8080" ]; then
      tls_ip="$(env_value PUBLIC_TLS_IP)"
      https_port="$(env_value MULTICA_HTTPS_PORT)"
      https_port="${https_port:-8446}"
      case "$tls_ip" in
        "") gateway_web_origin="" ;;
        *:*) gateway_web_origin="https://[${tls_ip}]:${https_port}" ;;
        *) gateway_web_origin="https://${tls_ip}:${https_port}" ;;
      esac
      if [ -n "$gateway_web_origin" ] && [ "${web_url%/}" != "$gateway_web_origin" ]; then
        log "warning: MULTICA_DEFAULT_WEB_URL differs from the bundled multica-gateway origin (${gateway_web_origin}); the bundled stack is published there, so verify the configured entry is reachable (the default card links to it)"
      fi
    fi
  fi
  # Multica 内网地址允许清单独立于 MCP 清单：未显式配置时按同源补齐，绝不
  # 写入 SUPER_ASSISTANT_MCP_INTERNAL_BASE_URLS。
  current="$(env_value SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS)"
  if [ -z "$current" ] && [ -n "$base_url" ]; then
    updates+=("SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS=${base_url}")
    log "configured SUPER_ASSISTANT_MULTICA_INTERNAL_BASE_URLS for the bundled multica origin (independent of the MCP allowlist)"
  fi
  set_env_values_in_file "${APP_DIR}/.env" "${updates[@]}"
}

multica_default_origin_ok() {
  # MULTICA_DEFAULT_BASE_URL/WEB_URL 的部署期形态校验：http(s) origin（可带
  # 端口/IPv6 括号），不带凭据/路径/查询/fragment；允许单个尾部斜杠。
  local value="${1:-}" rest
  case "$value" in
    http://*|https://*) ;;
    *) return 1 ;;
  esac
  case "$value" in
    *\#*|*\?*|*@*) return 1 ;;
  esac
  rest="${value#*://}"
  case "$rest" in
    /) return 1 ;;
    */) rest="${rest%/}" ;;
  esac
  case "$rest" in
    */*) return 1 ;;
  esac
  [ -n "$rest" ] || return 1
}

multica_bind_addr_ok() {
  # MULTICA_WEB_BIND_ADDR 只允许 IP 字面量（IPv4 点分十进制或 IPv6 冒号
  # 十六进制），拒绝 host:port、CIDR、scheme 等会改变发布语义的值；docker
  # 端口发布会对地址做最终校验，这里提前给出可操作的部署错误。
  local value="${1:-}" octet
  [ -n "$value" ] || return 1
  case "$value" in
    *[!0-9A-Fa-f:.]*) return 1 ;;
  esac
  octet='([0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])'
  if [[ "$value" =~ ^$octet\.$octet\.$octet\.$octet$ ]]; then
    return 0
  fi
  [[ "$value" == *:* ]] || return 1
  [[ "$value" =~ ^[0-9A-Fa-f:]+$ ]]
}

validate_multica_config() {
  # 启用与未启用都先做五项默认值的 all-or-nothing 校验（部分填写是部署
  # 错误）；五项全空在两种状态下都合法——启用时栈正常跑、无部署级默认
  # （readiness 不门禁 multica_default），管理员在原生界面完成初始化后
  # 回填。仅在启用 bundled 栈时才强校验镜像/密钥/端口。
  local multica_pat
  local configured_count=0 key value
  for key in \
    MULTICA_DEFAULT_BASE_URL MULTICA_DEFAULT_WEB_URL \
    MULTICA_DEFAULT_WORKSPACE_ID MULTICA_DEFAULT_WORKSPACE_NAME \
    MULTICA_DEFAULT_PAT; do
    value="$(env_value "$key")"
    [ -n "$value" ] && configured_count=$((configured_count + 1))
  done
  if [ "$configured_count" -ne 0 ] && [ "$configured_count" -ne 5 ]; then
    log "MULTICA_DEFAULT_* must be all set or all empty (found ${configured_count}/5); a partial default configuration is refused"
    exit 1
  fi
  [ "$MULTICA_INTERNAL" = "1" ] || return 0
  # 官方镜像 digest 门禁：三个镜像引用（默认 pin 或运维覆盖）都必须是
  # 不可变 @sha256；GHCR 只发布 latest tag，浮动引用一律拒绝。
  for key in MULTICA_BACKEND_IMAGE MULTICA_WEB_IMAGE MULTICA_POSTGRES_IMAGE; do
    [ -n "$(env_value "$key")" ] || {
      log "$key is required when MULTICA_ENABLED=true (configure_multica_env writes the verified official pin)"
      exit 1
    }
    check_image_digest "$key"
  done
  # 栈自身密钥：由 configure_multica_env 生成，绝不接受空值/开发兜底值。
  for key in MULTICA_JWT_SECRET MULTICA_POSTGRES_PASSWORD; do
    value="$(env_value "$key")"
    case "$value" in
      ""|multica-local-jwt-secret-change-me|multica-local-password)
        log "$key must be a deployment-generated strong value when MULTICA_ENABLED=true (configure_multica_env generates it)"
        exit 1
        ;;
    esac
  done
  # 五值默认集完整时才校验形态：BASE_URL 允许内网 origin；WEB_URL 是所有
  # 平台用户浏览器可达的入口，不做部署期推导，必须显式给出（loopback 绑定
  # 下推导值既不可达也不安全）。
  if [ "$configured_count" -eq 5 ]; then
    if ! multica_default_origin_ok "$(env_value MULTICA_DEFAULT_BASE_URL)"; then
      log "MULTICA_DEFAULT_BASE_URL must be an absolute http(s) origin without credentials, path, query, or fragment"
      exit 1
    fi
    if ! multica_default_origin_ok "$(env_value MULTICA_DEFAULT_WEB_URL)"; then
      log "MULTICA_DEFAULT_WEB_URL must be an absolute http(s) origin without credentials, path, query, or fragment; set the browser-reachable web origin explicitly (the deployment never derives it)"
      exit 1
    fi
    multica_pat="$(env_value MULTICA_DEFAULT_PAT)"
    if [ "${#multica_pat}" -lt 8 ]; then
      log "MULTICA_DEFAULT_PAT must contain at least 8 characters"
      exit 1
    fi
    case "$multica_pat" in
      *[[:space:]]*) log "MULTICA_DEFAULT_PAT must not contain whitespace"; exit 1 ;;
    esac
    unset multica_pat
  fi
  case "$(env_value MULTICA_WEB_PORT)" in
    ""|*[!0-9]*)
      log "MULTICA_WEB_PORT must be an integer between 1 and 65535"
      exit 1
      ;;
    *)
      if [ "$(env_value MULTICA_WEB_PORT)" -lt 1 ] \
          || [ "$(env_value MULTICA_WEB_PORT)" -gt 65535 ]; then
        log "MULTICA_WEB_PORT must be an integer between 1 and 65535"
        exit 1
      fi
      ;;
  esac
  # web 绑定地址：只接受 IP 字面量（IPv4 点分十进制或 IPv6），杜绝
  # host:port/scheme 之类会改变发布语义的值；默认 127.0.0.1（loopback）。
  value="$(env_value MULTICA_WEB_BIND_ADDR)"
  value="${value:-127.0.0.1}"
  if ! multica_bind_addr_ok "$value"; then
    log "MULTICA_WEB_BIND_ADDR must be a plain IPv4 or IPv6 address (default 127.0.0.1); exposing the Multica login/PAT UI beyond loopback requires a TLS reverse proxy"
    exit 1
  fi
  log "validated Multica default service URLs and deployment credentials"
}

validate_hindsight_config() {
  # 仅在启用时校验；未启用时缺密钥/镜像键不得阻断全平台部署。
  local hindsight_secret
  [ "$HINDSIGHT_INTERNAL" = "1" ] || return 0
  [ "$(env_value HINDSIGHT_BASE_URL)" = "http://hindsight:8888" ] || {
    log "HINDSIGHT_BASE_URL must remain the internal http://hindsight:8888 contract"
    exit 1
  }
  for key in HINDSIGHT_DB_PASSWORD HINDSIGHT_API_KEY HINDSIGHT_SERVICE_TOKEN HINDSIGHT_CP_ACCESS_KEY; do
    require_secret "$key"
  done
  hindsight_secret="$(env_value HINDSIGHT_DB_PASSWORD)"
  if [ "${#hindsight_secret}" -lt 12 ]; then
    log "HINDSIGHT_DB_PASSWORD must contain at least 12 characters"
    exit 1
  fi
  for key in HINDSIGHT_API_KEY HINDSIGHT_SERVICE_TOKEN; do
    hindsight_secret="$(env_value "$key")"
    if [ "${#hindsight_secret}" -lt 24 ]; then
      log "$key must contain at least 24 characters"
      exit 1
    fi
  done
  # 控制面访问密钥由管理员在登录页手工输入（会话 24h）：默认 16 个十六进制
  # 字符（64 位），下限放宽到 12，与机器间 Bearer 密钥的 24 位要求区分。
  hindsight_secret="$(env_value HINDSIGHT_CP_ACCESS_KEY)"
  if [ "${#hindsight_secret}" -lt 12 ]; then
    log "HINDSIGHT_CP_ACCESS_KEY must contain at least 12 characters"
    exit 1
  fi
  unset hindsight_secret
  # HINDSIGHT_WEB_URL 必须是 http(s) origin（无路径/查询/凭据），与 backend
  # memory settings 的净化契约一致；形态错误在部署期即失败，避免线上静默回退
  # 成「未配置」提示。
  if ! hindsight_web_url_origin_ok "$(env_value HINDSIGHT_WEB_URL)"; then
    log "HINDSIGHT_WEB_URL must be an absolute http(s) origin without credentials, path, query, or fragment"
    exit 1
  fi
  case "$(env_value HINDSIGHT_TIMEOUT_SECONDS)" in
    ""|*[!0-9.]*|.*|*.|*.*.*)
      log "HINDSIGHT_TIMEOUT_SECONDS must be a number between 0.1 and 60"
      exit 1
      ;;
    *)
      if ! awk -v value="$(env_value HINDSIGHT_TIMEOUT_SECONDS)" \
          'BEGIN { exit !(value > 0 && value <= 60) }'; then
        log "HINDSIGHT_TIMEOUT_SECONDS must be a number between 0.1 and 60"
        exit 1
      fi
      ;;
  esac
  log "validated Hindsight memory service URLs and runtime credentials"
}

validate_weknora_config() {
  local http_port https_port public_port public_https_port admin_password weknora_secret
  [ "$(env_value WEKNORA_BASE_URL)" = "http://weknora-app:8080" ] || {
    log "WEKNORA_BASE_URL must remain the internal http://weknora-app:8080 contract"
    exit 1
  }
  validate_pipeline_file_public_base WEKNORA_WEB_URL "$(env_value WEKNORA_WEB_URL)"
  for key in \
    WEKNORA_ADMIN_USER WEKNORA_ADMIN_EMAIL WEKNORA_ADMIN_PASSWORD \
    WEKNORA_DB_NAME WEKNORA_DB_USER WEKNORA_DB_PASSWORD \
    WEKNORA_REDIS_PASSWORD \
    WEKNORA_JWT_SECRET WEKNORA_SYSTEM_AES_KEY WEKNORA_SYSTEM_SIGNING_KEY; do
    require_secret "$key"
  done
  case "$(env_value WEKNORA_ADMIN_EMAIL)" in
    *@*.*) ;;
    *) log "WEKNORA_ADMIN_EMAIL must be a valid email address"; exit 1 ;;
  esac
  for key in WEKNORA_DB_PASSWORD WEKNORA_REDIS_PASSWORD; do
    weknora_secret="$(env_value "$key")"
    if [ "${#weknora_secret}" -lt 12 ]; then
      log "$key must contain at least 12 characters"
      exit 1
    fi
  done
  for key in WEKNORA_JWT_SECRET WEKNORA_SYSTEM_SIGNING_KEY; do
    weknora_secret="$(env_value "$key")"
    if [ "${#weknora_secret}" -lt 32 ]; then
      log "$key must contain at least 32 characters"
      exit 1
    fi
  done
  weknora_secret="$(env_value WEKNORA_SYSTEM_AES_KEY)"
  if [ "${#weknora_secret}" -ne 32 ] \
      || [ "$(printf '%s' "$weknora_secret" | LC_ALL=C wc -c | tr -d ' ')" != "32" ]; then
    # Upstream crypto uses len([]byte(env)) == 32 to enable encryption; a
    # longer key silently disables encryption, so reject instead of regen.
    log "WEKNORA_SYSTEM_AES_KEY must be exactly 32 ASCII bytes (not a 64-char hex key)"
    exit 1
  fi
  unset weknora_secret
  admin_password="$(env_value WEKNORA_ADMIN_PASSWORD)"
  if [ "${#admin_password}" -lt 8 ] || [ "${#admin_password}" -gt 32 ]; then
    log "WEKNORA_ADMIN_PASSWORD must contain between 8 and 32 characters"
    exit 1
  fi
  case "$admin_password" in
    *[[:space:]]*) log "WEKNORA_ADMIN_PASSWORD must not contain whitespace"; exit 1 ;;
  esac
  unset admin_password
  case "$(env_value WEKNORA_TIMEOUT_SECONDS)" in
    ""|*[!0-9]*)
      log "WEKNORA_TIMEOUT_SECONDS must be an integer between 3 and 60"
      exit 1
      ;;
    *)
      if [ "$(env_value WEKNORA_TIMEOUT_SECONDS)" -lt 3 ] \
          || [ "$(env_value WEKNORA_TIMEOUT_SECONDS)" -gt 60 ]; then
        log "WEKNORA_TIMEOUT_SECONDS must be an integer between 3 and 60"
        exit 1
      fi
      ;;
  esac
  http_port="$(env_value WEKNORA_HTTP_PORT)"
  https_port="$(env_value WEKNORA_HTTPS_PORT)"
  public_port="$(env_value PUBLIC_PORT)"
  public_port="${public_port:-80}"
  public_https_port="$(env_value PUBLIC_HTTPS_PORT)"
  public_https_port="${public_https_port:-8443}"
  for port in "$http_port" "$https_port"; do
    case "$port" in
      ""|*[!0-9]*) log "WEKNORA_HTTP_PORT and WEKNORA_HTTPS_PORT must be integers between 1 and 65535"; exit 1 ;;
    esac
    if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
      log "WEKNORA_HTTP_PORT and WEKNORA_HTTPS_PORT must be integers between 1 and 65535"
      exit 1
    fi
  done
  if [ "$http_port" = "$https_port" ] \
      || [ "$http_port" = "$public_port" ] \
      || [ "$https_port" = "$public_https_port" ]; then
    log "WeKnora public ports must be distinct from each other and from PUBLIC_PORT/PUBLIC_HTTPS_PORT"
    exit 1
  fi
  log "validated WeKnora service URLs and independent account/runtime credentials"
}

validate_kaneo_config() {
  local http_port https_port public_port public_https_port
  local weknora_http_port weknora_https_port
  local kaneo_secret admin_password
  local web_url url_host url_port tls_ip
  [ "$(env_value KANEO_BASE_URL)" = "http://kaneo-app:5173" ] || {
    log "KANEO_BASE_URL must remain the internal http://kaneo-app:5173 contract"
    exit 1
  }
  http_port="$(env_value KANEO_HTTP_PORT)"
  https_port="$(env_value KANEO_HTTPS_PORT)"
  for port in "$http_port" "$https_port"; do
    case "$port" in
      ""|*[!0-9]*) log "KANEO_HTTP_PORT and KANEO_HTTPS_PORT must be integers between 1 and 65535"; exit 1 ;;
    esac
    if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
      log "KANEO_HTTP_PORT and KANEO_HTTPS_PORT must be integers between 1 and 65535"
      exit 1
    fi
  done
  # Kaneo's canonical origin feeds KANEO_CLIENT_URL/KANEO_API_URL and the
  # gateway redirect target, so plain HTTP and trailing slashes are rejected
  # instead of warned. The public address is the bare server IP with a
  # dedicated port; external TLS proxies are outside this deployment model,
  # so the origin port must equal the published HTTPS listener and the host
  # must equal PUBLIC_TLS_IP (the 127.0.0.1 probes only see that listener).
  web_url="$(env_value KANEO_WEB_URL)"
  if [[ ! "$web_url" =~ ^https://(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+)(:([0-9]{1,5}))?$ ]]; then
    log "KANEO_WEB_URL must be an absolute HTTPS origin without credentials, path, query, fragment, or trailing slash"
    exit 1
  fi
  url_host="${BASH_REMATCH[1]#[}"
  url_host="${url_host%]}"
  url_port="${BASH_REMATCH[3]:-443}"
  if [ "$((10#$url_port))" -ne "$https_port" ]; then
    log "KANEO_WEB_URL port ${url_port} must equal KANEO_HTTPS_PORT ${https_port}; the gateway publishes exactly that listener"
    exit 1
  fi
  tls_ip="$(env_value PUBLIC_TLS_IP)"
  if [ -z "$tls_ip" ]; then
    log "PUBLIC_TLS_IP is required so the public Kaneo origin is bound to the real server address"
    exit 1
  fi
  if [ "$url_host" != "$tls_ip" ]; then
    log "KANEO_WEB_URL host ${url_host} must equal PUBLIC_TLS_IP; external TLS proxy origins are not supported by this deployment"
    exit 1
  fi
  for key in \
    KANEO_ADMIN_USER KANEO_ADMIN_EMAIL KANEO_ADMIN_PASSWORD \
    KANEO_DB_NAME KANEO_DB_USER KANEO_DB_PASSWORD \
    KANEO_AUTH_SECRET; do
    require_secret "$key"
  done
  case "$(env_value KANEO_ADMIN_EMAIL)" in
    *@*.*) ;;
    *) log "KANEO_ADMIN_EMAIL must be a valid email address"; exit 1 ;;
  esac
  kaneo_secret="$(env_value KANEO_DB_PASSWORD)"
  if [ "${#kaneo_secret}" -lt 12 ]; then
    log "KANEO_DB_PASSWORD must contain at least 12 characters"
    exit 1
  fi
  kaneo_secret="$(env_value KANEO_AUTH_SECRET)"
  if [ "${#kaneo_secret}" -lt 32 ]; then
    log "KANEO_AUTH_SECRET must contain at least 32 characters"
    exit 1
  fi
  unset kaneo_secret
  admin_password="$(env_value KANEO_ADMIN_PASSWORD)"
  if [ "${#admin_password}" -lt 8 ] || [ "${#admin_password}" -gt 32 ]; then
    log "KANEO_ADMIN_PASSWORD must contain between 8 and 32 characters"
    exit 1
  fi
  case "$admin_password" in
    *[[:space:]]*) log "KANEO_ADMIN_PASSWORD must not contain whitespace"; exit 1 ;;
  esac
  unset admin_password
  case "$(env_value KANEO_TIMEOUT_SECONDS)" in
    ""|*[!0-9]*)
      log "KANEO_TIMEOUT_SECONDS must be an integer between 3 and 60"
      exit 1
      ;;
    *)
      if [ "$(env_value KANEO_TIMEOUT_SECONDS)" -lt 3 ] \
          || [ "$(env_value KANEO_TIMEOUT_SECONDS)" -gt 60 ]; then
        log "KANEO_TIMEOUT_SECONDS must be an integer between 3 and 60"
        exit 1
      fi
      ;;
  esac
  public_port="$(env_value PUBLIC_PORT)"
  public_port="${public_port:-80}"
  public_https_port="$(env_value PUBLIC_HTTPS_PORT)"
  public_https_port="${public_https_port:-8443}"
  weknora_http_port="$(env_value WEKNORA_HTTP_PORT)"
  weknora_http_port="${weknora_http_port:-8089}"
  weknora_https_port="$(env_value WEKNORA_HTTPS_PORT)"
  weknora_https_port="${weknora_https_port:-8444}"
  if [ "$http_port" = "$https_port" ] \
      || [ "$http_port" = "$public_port" ] \
      || [ "$http_port" = "$public_https_port" ] \
      || [ "$http_port" = "$weknora_http_port" ] \
      || [ "$http_port" = "$weknora_https_port" ] \
      || [ "$https_port" = "$public_port" ] \
      || [ "$https_port" = "$public_https_port" ] \
      || [ "$https_port" = "$weknora_http_port" ] \
      || [ "$https_port" = "$weknora_https_port" ]; then
    log "Kaneo public ports must be distinct from each other, from WeKnora and from PUBLIC_PORT/PUBLIC_HTTPS_PORT"
    exit 1
  fi
  log "validated Kaneo service URLs and independent account/runtime credentials"
}

validate_grafana_config() {
  local admin_password
  [ "$(env_value GRAFANA_BASE_URL)" = "http://grafana:3000" ] || {
    log "GRAFANA_BASE_URL must remain the internal http://grafana:3000 contract"
    exit 1
  }
  admin_password="$(env_value GRAFANA_ADMIN_PASSWORD)"
  if [ "${#admin_password}" -lt 8 ] || [ "${#admin_password}" -gt 32 ]; then
    log "GRAFANA_ADMIN_PASSWORD must contain between 8 and 32 characters"
    exit 1
  fi
  case "$admin_password" in
    *[[:space:]]*) log "GRAFANA_ADMIN_PASSWORD must not contain whitespace"; exit 1 ;;
  esac
  unset admin_password
  case "$(env_value GRAFANA_TIMEOUT_SECONDS)" in
    ""|*[!0-9]*)
      log "GRAFANA_TIMEOUT_SECONDS must be an integer between 3 and 60"
      exit 1
      ;;
    *)
      if [ "$(env_value GRAFANA_TIMEOUT_SECONDS)" -lt 3 ] \
          || [ "$(env_value GRAFANA_TIMEOUT_SECONDS)" -gt 60 ]; then
        log "GRAFANA_TIMEOUT_SECONDS must be an integer between 3 and 60"
        exit 1
      fi
      ;;
  esac
  case "$(env_value GRAFANA_SESSION_TTL_SECONDS)" in
    ""|*[!0-9]*)
      log "GRAFANA_SESSION_TTL_SECONDS must be an integer between 60 and 86400"
      exit 1
      ;;
    *)
      if [ "$(env_value GRAFANA_SESSION_TTL_SECONDS)" -lt 60 ] \
          || [ "$(env_value GRAFANA_SESSION_TTL_SECONDS)" -gt 86400 ]; then
        log "GRAFANA_SESSION_TTL_SECONDS must be an integer between 60 and 86400"
        exit 1
      fi
      ;;
  esac
  case "$(env_value GRAFANA_IMAGE)" in
    grafana/grafana:13.3.1@sha256:0615ec4fbd4072004b7d86b46ded63013a74b7d6eb1119273dd89e8fa25c13cf) ;;
    *)
      log "GRAFANA_IMAGE must remain the pinned grafana/grafana:13.3.1 digest contract"
      exit 1
      ;;
  esac
  log "validated Grafana service contract and shared administrator credentials"
}

start_integrated_dbx_if_needed() {
  if [ "$INTEGRATED_DBX_INTERNAL" = "1" ]; then
    compose up -d dbx
  else
    # Stop a prior internal service without deleting dbx_data. An explicit
    # external endpoint remains authoritative and is never replaced.
    compose --profile integrated-dbx stop dbx >/dev/null 2>&1 || true
  fi
}

dependency_key_allowed() {
  case "$1" in
    ENVIRONMENT|PUBLIC_PORT|\
    POSTGRES_DB|POSTGRES_USER|POSTGRES_PASSWORD|DATABASE_URL|REDIS_URL|\
    NEO4J_URI|NEO4J_USER|NEO4J_PASSWORD|NEO4J_AUTH|\
    MINIO_ENDPOINT|MINIO_ACCESS_KEY|MINIO_SECRET_KEY|MINIO_USE_SSL|\
    N8N_API_URL|N8N_API_KEY|N8N_TIMEOUT_SECONDS|\
    WEKNORA_WEB_URL|\
    WEKNORA_TIMEOUT_SECONDS|WEKNORA_ADMIN_USER|WEKNORA_ADMIN_EMAIL|\
    WEKNORA_APP_IMAGE|WEKNORA_UI_IMAGE|WEKNORA_DOCREADER_IMAGE|\
    WEKNORA_POSTGRES_IMAGE|WEKNORA_REDIS_IMAGE|\
    KANEO_WEB_URL|\
    KANEO_TIMEOUT_SECONDS|KANEO_ADMIN_USER|KANEO_ADMIN_EMAIL|\
    KANEO_APP_IMAGE|KANEO_POSTGRES_IMAGE|\
    HINDSIGHT_DB_PASSWORD|HINDSIGHT_API_KEY|HINDSIGHT_SERVICE_TOKEN|\
    HINDSIGHT_IMAGE|HINDSIGHT_POSTGRES_IMAGE|\
    MULTICA_DEFAULT_PAT|MULTICA_DEFAULT_WEB_URL|\
    MULTICA_DEFAULT_BASE_URL|MULTICA_DEFAULT_WORKSPACE_ID|\
    MULTICA_DEFAULT_WORKSPACE_NAME|\
    MULTICA_BACKEND_IMAGE|MULTICA_WEB_IMAGE|MULTICA_POSTGRES_IMAGE|\
    SUPER_ASSISTANT_MCP_STDIO_ENABLED|SUPER_ASSISTANT_MCP_STDIO_ALLOWED_COMMANDS)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}
dependency_key_ignored_legacy() {
  # The protected production manifest may still contain these historical
  # fields. Accept them read-only during the migration, but never write them
  # into .env and never let them control runtime behavior.
  case "$1" in
    POSTGRES_HOST|POSTGRES_PORT|MINIO_CONSOLE_URL|\
    STRICT_PRODUCTION_CONFIG|REQUIRE_EXTERNAL_DEPENDENCIES|\
    DATASET_IMPORT_USE_CELERY|STORAGE_LOCAL_FALLBACK|\
    N8N_EMAIL|N8N_PASSWORD)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}
apply_production_dependency_config() {
  local raw key value applied=0
  if [ ! -f "$DEPENDENCY_CONFIG_FILE" ]; then
    log "$DEPENDENCY_CONFIG_FILE is required for production deployment"
    exit 1
  fi
  # The tracked manifest is a temporary compatibility exception containing
  # real connection credentials. Restrict the unpacked copy before reading it.
  chmod 600 "$DEPENDENCY_CONFIG_FILE"

  while IFS= read -r raw || [ -n "$raw" ]; do
    raw="${raw%$'\r'}"
    case "$raw" in
      ""|\#*) continue ;;
    esac
    if [[ "$raw" != *=* ]]; then
      log "$DEPENDENCY_CONFIG_FILE contains an invalid line"
      exit 1
    fi
    key="${raw%%=*}"
    value="${raw#*=}"
    if [[ ! "$key" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
      log "$DEPENDENCY_CONFIG_FILE contains unsupported key: $key"
      exit 1
    fi
    if dependency_key_ignored_legacy "$key"; then
      log "ignored deprecated production dependency setting: $key"
      continue
    fi
    if ! dependency_key_allowed "$key"; then
      log "$DEPENDENCY_CONFIG_FILE contains unsupported key: $key"
      exit 1
    fi
    if [ -z "$value" ]; then
      log "$DEPENDENCY_CONFIG_FILE contains an empty required value: $key"
      exit 1
    fi
    if [ "$key" = "DATABASE_URL" ]; then
      case "$value" in
        postgres://*)
          value="postgresql://${value#postgres://}"
          log "normalized legacy postgres:// DATABASE_URL to postgresql://"
          ;;
        postgresql+psycopg2://*)
          value="postgresql://${value#postgresql+psycopg2://}"
          log "normalized driver-qualified DATABASE_URL to postgresql://"
          ;;
      esac
    fi
    set_env_value "$key" "$value"
    applied=$((applied + 1))
  done < "$DEPENDENCY_CONFIG_FILE"
  log "applied ${applied} production dependency settings (values are not printed)"
}
apply_production_dependency_config
chmod 600 .env
configure_integrated_dbx
# Seed the public TLS address before the service origins are derived from it.
ensure_public_tls_ip
configure_weknora_env
configure_kaneo_env
configure_grafana_env
configure_hindsight_env
configure_multica_env
# Chromium is bundled with the production Compose stack. Pin the internal CDP
# URL so existing server-side .env files receive the newly required setting.
set_env_value STEWARD_BROWSER_CDP_URL http://browser:9222
if [ -z "$(awk -F= '$1 == "N8N_TIMEOUT_SECONDS" {print substr($0, index($0,"=")+1)}' .env | tail -n1)" ]; then
  set_env_value N8N_TIMEOUT_SECONDS 30
fi
if [ -z "$(awk -F= '$1 == "API_HUB_SYSTEM_MCP_TOKEN" {print substr($0, index($0,"=")+1)}' .env | tail -n1)" ]; then
  set_env_value API_HUB_SYSTEM_MCP_TOKEN "$(random_hex 32)"
fi
# 镜像 digest 钉扎迁移：存量服务器 .env 来自 tag 形态且 STRICT=0 的旧
# .env.example，本次校验既会因 STRICT=0 也会因浮动镜像被拒。仅当 .env
# 呈现完整的 legacy 指纹——全部镜像键缺失或恰好等于旧浮动默认值，且
# STRICT 处于 0/缺失——才判定为 legacy 文件整体迁移：回填与 .env.example
# 一致的不可变 @sha256 引用并打开 STRICT。任何偏离（运维换用自己的钉扎
# 镜像、单独修改过某个镜像、或已显式开启 STRICT）都不迁移，严格模式的
# "浮动镜像必须拒绝"语义保持不变。
legacy_env_fingerprint=1
while read -r legacy_key legacy_value; do
  [ -n "$legacy_key" ] || continue
  legacy_current="$(env_value "$legacy_key")"
  if [ -n "$legacy_current" ] && [ "$legacy_current" != "$legacy_value" ]; then
    legacy_env_fingerprint=0
  fi
done <<'LEGACY_IMAGES'
POSTGRES_IMAGE postgres:16-alpine
REDIS_IMAGE redis:7-alpine
NEO4J_IMAGE neo4j:5-community
MINIO_IMAGE minio/minio:latest
BROWSER_IMAGE chromedp/headless-shell:latest
PYTHON_BASE_IMAGE python:3.12-slim
NODE_BASE_IMAGE node:22-alpine
NGINX_BASE_IMAGE nginx:1.27-alpine
LEGACY_IMAGES
case "$(env_value STRICT_IMAGE_DIGESTS)" in
  1|true|TRUE|yes|YES)
    legacy_env_fingerprint=0
    ;;
esac
if [ "$legacy_env_fingerprint" = "1" ]; then
  pin_image_default() {
    local key="$1" pinned="$2"
    if [[ ! "$(env_value "$key")" =~ @sha256:[0-9a-fA-F]{64}$ ]]; then
      set_env_value "$key" "$pinned"
    fi
  }
  pin_image_default POSTGRES_IMAGE "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
  pin_image_default REDIS_IMAGE "redis:7-alpine@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99"
  pin_image_default NEO4J_IMAGE "neo4j:5-community@sha256:362542416de6c09a971484d1893878016cc3b5cdec166e54b1c824a220ecd6b9"
  pin_image_default MINIO_IMAGE "minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e"
  pin_image_default BROWSER_IMAGE "chromedp/headless-shell@sha256:2d349b544a1ea6b5b5fd7c0fe99215ff662339c57407ee2e8c0a11af93516b04"
  pin_image_default PYTHON_BASE_IMAGE "python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
  pin_image_default NODE_BASE_IMAGE "node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32"
  pin_image_default NGINX_BASE_IMAGE "nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
  set_env_value STRICT_IMAGE_DIGESTS 1
  log "migrated legacy .env to immutable image digests and enabled STRICT_IMAGE_DIGESTS"
fi
write_runtime_secret_split() {
  local secret_key="$1"
  local encryption_key="$2"
  local admin_password="$3"
  local tmp
  tmp="$(mktemp "${APP_DIR}/.env.runtime-secrets.XXXXXX")"
  chmod 600 "$tmp"
  if ! OPENONTOLOGY_NEXT_SECRET_KEY="$secret_key" \
      OPENONTOLOGY_NEXT_ENCRYPTION_KEY="$encryption_key" \
      OPENONTOLOGY_NEXT_ADMIN_PASSWORD="$admin_password" \
      awk '
    BEGIN {
      secret_seen=0
      encryption_seen=0
      admin_seen=0
      secret_key=ENVIRON["OPENONTOLOGY_NEXT_SECRET_KEY"]
      encryption_key=ENVIRON["OPENONTOLOGY_NEXT_ENCRYPTION_KEY"]
      admin_password=ENVIRON["OPENONTOLOGY_NEXT_ADMIN_PASSWORD"]
    }
    /^[[:space:]]*SECRET_KEY=/ {
      if (!secret_seen) print "SECRET_KEY=" secret_key
      secret_seen=1
      next
    }
    /^[[:space:]]*ENCRYPTION_KEY=/ {
      if (!encryption_seen) print "ENCRYPTION_KEY=" encryption_key
      encryption_seen=1
      next
    }
    /^[[:space:]]*FIRST_ADMIN_PASSWORD=/ {
      if (!admin_seen) print "FIRST_ADMIN_PASSWORD=" admin_password
      admin_seen=1
      next
    }
    { print }
    END {
      if (!secret_seen) print "SECRET_KEY=" secret_key
      if (!encryption_seen) print "ENCRYPTION_KEY=" encryption_key
      if (!admin_seen) print "FIRST_ADMIN_PASSWORD=" admin_password
    }
  ' .env > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  # Flush the complete target before and the containing directory after the
  # atomic rename. BSD/GNU sync both accept a path on supported deployment
  # hosts; a global sync is the conservative fallback.
  sync "$tmp" 2>/dev/null || sync
  if ! mv "$tmp" .env; then
    rm -f "$tmp"
    return 1
  fi
  chmod 600 .env
  sync "$APP_DIR" 2>/dev/null || sync
}
upgrade_legacy_runtime_secrets() {
  local current_secret current_encryption current_admin
  local next_secret next_encryption next_admin
  local runtime_key assignment_state
  local secret_key_present=0
  local secret_rotated=0 encryption_pinned=0 admin_seed_rotated=0
  for runtime_key in SECRET_KEY ENCRYPTION_KEY FIRST_ADMIN_PASSWORD; do
    if ! assignment_state="$(runtime_env_assignment_state "$runtime_key")"; then
      log "could not read $runtime_key from production .env"
      exit 1
    fi
    case "$assignment_state" in
      ambiguous)
        log "$runtime_key must use one canonical uppercase KEY=value assignment before automatic secret migration"
        exit 1
        ;;
      canonical)
        if [ "$runtime_key" = "SECRET_KEY" ]; then
          secret_key_present=1
        fi
        ;;
      missing) ;;
      *)
        log "could not classify $runtime_key in production .env"
        exit 1
        ;;
    esac
  done
  current_secret="$(env_value SECRET_KEY)"
  current_encryption="$(env_value ENCRYPTION_KEY)"
  current_admin="$(env_value FIRST_ADMIN_PASSWORD)"
  next_secret="$current_secret"
  next_encryption="$current_encryption"
  next_admin="$current_admin"

  if [ -n "$current_encryption" ] \
      && [[ ! "$current_encryption" =~ ^[A-Za-z0-9_-]{43}=$ ]]; then
    log "ENCRYPTION_KEY must be a URL-safe base64 Fernet key"
    exit 1
  fi
  case "$current_secret" in
    ""|dev-secret-key|change-me-to-a-random-32-char-string) ;;
    *)
      if [ "${#current_secret}" -lt 32 ]; then
        log "SECRET_KEY must contain at least 32 characters"
        exit 1
      fi
      ;;
  esac

  case "$current_secret" in
    ""|dev-secret-key|change-me-to-a-random-32-char-string)
      if [ -z "$current_encryption" ]; then
        # These are the exact Fernet keys used by historical releases when
        # ENCRYPTION_KEY was empty. Pinning the effective key before rotating
        # SECRET_KEY keeps every PostgreSQL/API-Hub ciphertext byte unchanged.
        case "$current_secret" in
          "")
            if [ "$secret_key_present" = "1" ]; then
              # An explicitly empty dotenv value overrides the Settings
              # default, so historical encryption derived from the empty
              # string rather than from dev-secret-key.
              next_encryption="47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU="
            else
              next_encryption="BTff0inM1kTinILwwnobOwdaFYn6daGG7UCrwlv80kg="
            fi
            ;;
          dev-secret-key)
            next_encryption="BTff0inM1kTinILwwnobOwdaFYn6daGG7UCrwlv80kg="
            ;;
          change-me-to-a-random-32-char-string)
            next_encryption="gOw9duQ85uzIs30nOvvFMjrWEyJK4GhbpPTn07dujZs="
            ;;
        esac
        encryption_pinned=1
      fi
      next_secret="$(random_hex 32)"
      secret_rotated=1
      ;;
  esac
  case "$current_admin" in
    ""|admin123|change-me)
      # This is only the future seed. Existing administrator password hashes
      # remain authoritative and are deliberately not modified here.
      next_admin="$(random_hex 24)"
      admin_seed_rotated=1
      ;;
  esac

  if [ "$secret_rotated" = "0" ] \
      && [ "$encryption_pinned" = "0" ] \
      && [ "$admin_seed_rotated" = "0" ]; then
    return
  fi
  if [ "$secret_rotated" = "1" ] || [ "$encryption_pinned" = "1" ]; then
    write_runtime_secret_split \
      "$next_secret" "$next_encryption" "$next_admin"
  elif [ "$admin_seed_rotated" = "1" ]; then
    # Do not round-trip an arbitrary existing secret through a text parser
    # when only the future admin seed needs changing.
    set_env_value FIRST_ADMIN_PASSWORD "$next_admin"
  fi
  if [ "$encryption_pinned" = "1" ]; then
    log "pinned the existing SECRET_KEY-derived encryption key; persisted ciphertext was not rewritten"
  fi
  if [ "$secret_rotated" = "1" ]; then
    log "replaced the example JWT signing secret; existing sessions must sign in again"
  fi
  if [ "$admin_seed_rotated" = "1" ]; then
    log "replaced the example future-admin seed; existing administrator password hashes were not changed"
  fi
  log "runtime secret values remain stored only in ${APP_DIR}/.env"
}
upgrade_legacy_runtime_secrets
redis_password_is_compose_safe() {
  local value="$1"
  [ "$value" != "ontopromptredis123" ] \
    && [[ "$value" =~ ^[A-Za-z0-9._~-]{16,}$ ]]
}
configure_redis_auth() {
  local redis_url service_password url_password
  redis_url="$(env_value REDIS_URL)"
  service_password="$(env_value REDIS_PASSWORD)"

  case "$redis_url" in
    redis://redis:6379/0)
      # Compatibility for the exact historical bundled URL. Reuse a strong
      # persistent service secret when present; otherwise create one once.
      if ! redis_password_is_compose_safe "$service_password"; then
        service_password="$(random_hex 24)"
      fi
      set_env_value REDIS_PASSWORD "$service_password"
      set_env_value REDIS_URL \
        "redis://:${service_password}@redis:6379/0"
      log "upgraded the legacy bundled Redis URL to authenticated mode"
      ;;
    redis://:*@redis:6379/[0-9]|redis://:*@redis:6379/[0-9][0-9])
      # REDIS_URL is the manifest authority. For the bundled service, derive
      # requirepass from that same URL so client and server cannot diverge.
      url_password="${redis_url#redis://:}"
      url_password="${url_password%@redis:6379/*}"
      if ! redis_password_is_compose_safe "$url_password"; then
        log "bundled Redis URL must use an unescaped 16+ character password containing only letters, digits, '.', '_', '~' or '-'"
        exit 1
      fi
      set_env_value REDIS_PASSWORD "$url_password"
      log "synchronized the bundled Redis service password from REDIS_URL"
      ;;
    redis://*@redis:*|redis://*@redis/*|redis://redis:*|redis://redis/*|\
    rediss://*@redis:*|rediss://*@redis/*|rediss://redis:*|rediss://redis/*)
      log "bundled Redis URL must use redis://:<password>@redis:6379/<numeric-db>; use an external host for TLS or other connection modes"
      exit 1
      ;;
    *)
      # An external REDIS_URL remains untouched. The bundled container still
      # receives its own strong password, but backend/worker do not use it.
      if ! redis_password_is_compose_safe "$service_password"; then
        set_env_value REDIS_PASSWORD "$(random_hex 24)"
      fi
      log "preserved the external Redis URL; bundled Redis credentials are not used by clients"
      ;;
  esac
}
configure_redis_auth
percent_decode_url_component() {
  local encoded="$1" decoded="" prefix hex byte
  while [[ "$encoded" == *%* ]]; do
    prefix="${encoded%%\%*}"
    decoded+="$prefix"
    encoded="${encoded#*%}"
    if [ "${#encoded}" -lt 2 ]; then
      return 1
    fi
    hex="${encoded:0:2}"
    if [[ ! "$hex" =~ ^[0-9A-Fa-f]{2}$ ]]; then
      return 1
    fi
    printf -v byte '%b' "\\x${hex}"
    decoded+="$byte"
    encoded="${encoded:2}"
  done
  printf '%s' "${decoded}${encoded}"
}
bundled_postgres_url_matches_service() {
  local database_url="$1" remainder authority encoded_database
  local userinfo hostport encoded_user encoded_password
  local decoded_user decoded_password decoded_database

  remainder="${database_url#*://}"
  if [ "$remainder" = "$database_url" ] || [[ "$remainder" != */* ]]; then
    return 1
  fi
  authority="${remainder%%/*}"
  encoded_database="${remainder#*/}"
  if [[ "$encoded_database" == *\?* ]] \
      || [[ "$encoded_database" == *\#* ]] \
      || [[ "$encoded_database" == */* ]]; then
    return 1
  fi
  hostport="${authority##*@}"
  userinfo="${authority%@*}"
  if [ "$userinfo" = "$authority" ] || [ "$hostport" != "db:5432" ] \
      || [[ "$userinfo" != *:* ]]; then
    return 1
  fi
  encoded_user="${userinfo%%:*}"
  encoded_password="${userinfo#*:}"
  decoded_user="$(percent_decode_url_component "$encoded_user")" \
    || return 1
  decoded_password="$(percent_decode_url_component "$encoded_password")" \
    || return 1
  decoded_database="$(percent_decode_url_component "$encoded_database")" \
    || return 1
  [ "$decoded_user" = "$(env_value POSTGRES_USER)" ] \
    && [ "$decoded_password" = "$(env_value POSTGRES_PASSWORD)" ] \
    && [ "$decoded_database" = "$(env_value POSTGRES_DB)" ]
}
validate_bundled_service_authority() {
  local database_url neo4j_uri expected_neo4j_auth
  database_url="$(env_value DATABASE_URL)"
  case "$database_url" in
    postgresql://*'@db:5432/'*)
      if ! bundled_postgres_url_matches_service "$database_url"; then
        log "bundled DATABASE_URL must exactly match decoded POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB and db:5432"
        exit 1
      fi
      log "validated bundled PostgreSQL service credentials against DATABASE_URL"
      ;;
    postgresql://*'@db:'*|postgresql://*'@db/'*)
      log "bundled DATABASE_URL must use the canonical db:5432 endpoint"
      exit 1
      ;;
  esac

  case "$(env_value NEO4J_AUTH)" in
    ""|none|NONE|None)
      log "NEO4J_AUTH must enable authentication for the bundled Neo4j service"
      exit 1
      ;;
    */*) ;;
    *) log "NEO4J_AUTH must use username/password format"; exit 1 ;;
  esac
  neo4j_uri="$(env_value NEO4J_URI)"
  case "$neo4j_uri" in
    bolt://neo4j:7687|neo4j://neo4j:7687)
      expected_neo4j_auth="$(env_value NEO4J_USER)/$(env_value NEO4J_PASSWORD)"
      if [ "$(env_value NEO4J_AUTH)" != "$expected_neo4j_auth" ]; then
        log "bundled NEO4J_AUTH must match NEO4J_USER/NEO4J_PASSWORD"
        exit 1
      fi
      log "validated bundled Neo4j service credentials against client settings"
      ;;
    bolt://neo4j:*|neo4j://neo4j:*|bolt+s://neo4j:*|neo4j+s://neo4j:*|\
    bolt+ssc://neo4j:*|neo4j+ssc://neo4j:*)
      log "bundled NEO4J_URI must use bolt://neo4j:7687 or neo4j://neo4j:7687"
      exit 1
      ;;
  esac
}
validate_bundled_service_authority
effective_public_port="$(env_value PUBLIC_PORT)"
effective_public_port="${effective_public_port:-80}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${effective_public_port}/}"
READINESS_URL="${READINESS_URL:-${HEALTH_URL%/}/api/health}"
redact_compose_logs() {
  local key value line tail_lines
  local -a sensitive_values=()
  local -a services=("$@")
  tail_lines="${COMPOSE_LOG_TAIL:-200}"
  for key in \
    SECRET_KEY ENCRYPTION_KEY FIRST_ADMIN_PASSWORD POSTGRES_PASSWORD \
    DATABASE_URL REDIS_PASSWORD REDIS_URL NEO4J_PASSWORD NEO4J_AUTH \
    MINIO_ACCESS_KEY MINIO_SECRET_KEY API_HUB_SYSTEM_MCP_TOKEN \
    N8N_API_KEY INTEGRATED_DBX_PASSWORD \
    WEKNORA_ADMIN_PASSWORD \
    WEKNORA_DB_PASSWORD WEKNORA_REDIS_PASSWORD \
    WEKNORA_JWT_SECRET WEKNORA_SYSTEM_AES_KEY WEKNORA_SYSTEM_SIGNING_KEY \
    KANEO_ADMIN_PASSWORD KANEO_DB_PASSWORD KANEO_AUTH_SECRET \
    HINDSIGHT_DB_PASSWORD HINDSIGHT_API_KEY HINDSIGHT_SERVICE_TOKEN \
    HINDSIGHT_CP_ACCESS_KEY \
    MULTICA_DEFAULT_PAT MULTICA_JWT_SECRET MULTICA_POSTGRES_PASSWORD; do
    value="$(env_value "$key")"
    [ -z "$value" ] || sensitive_values+=("$value")
  done
  compose logs --tail="$tail_lines" "${services[@]}" 2>&1 | while IFS= read -r line; do
    for value in "${sensitive_values[@]}"; do
      line="${line//"$value"/<redacted>}"
    done
    printf '%s\n' "$line"
  done
}
redact_backend_failure_logs() {
  COMPOSE_LOG_TAIL=2000 redact_compose_logs backend | awk '
    /Traceback \(most recent call last\)/ ||
    /The above exception was the direct cause/ ||
    /During handling of the above exception/ ||
    /File "\/app\/app\// ||
    /ERROR:/ ||
    /WARNING:/ ||
    /Error:/ ||
    /Exception:/ ||
    /索引创建失败/ ||
    /Neo4j unavailable/ {
      print
    }
  '
}
set_env_value STORAGE_LOCAL_DIR /uploads/object-storage
configure_pipeline_file_gateway() {
  local current default_url public_url
  current="$(env_value PIPELINE_FILE_GATEWAY_BASE_URL)"
  default_url="http://backend:8000/api/v2/file-transfer"
  public_url="${HEALTH_URL%/}/api/v2/file-transfer"
  if [ -z "$current" ] || [ "$current" = "$default_url" ]; then
    set_env_value PIPELINE_FILE_GATEWAY_BASE_URL "$public_url"
    current="$public_url"
    log "configured PIPELINE_FILE_GATEWAY_BASE_URL from the deployment health URL"
  fi
  case "$current" in
    http://127.0.0.1/*|http://127.0.0.1:*|http://localhost/*|http://localhost:*|http://backend:*|https://*) ;;
    http://*)
      log "warning: PIPELINE_FILE_GATEWAY_BASE_URL uses public plain HTTP; configure HTTPS before production file transfers"
      ;;
    *)
      log "PIPELINE_FILE_GATEWAY_BASE_URL must be an absolute HTTP(S) URL"
      exit 1
      ;;
  esac
}
configure_pipeline_file_gateway
validate_pipeline_file_public_base() {
  local key="$1"
  local value="$2"
  local port
  if [[ ! "$value" =~ ^https?://(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+)(:([0-9]{1,5}))?/?$ ]]; then
    log "$key must be an absolute HTTP(S) origin without credentials, path, query, or fragment"
    exit 1
  fi
  port="${BASH_REMATCH[3]:-}"
  if [ -n "$port" ] && (( 10#$port > 65535 )); then
    log "$key contains an invalid TCP port"
    exit 1
  fi
  case "$value" in
    http://*)
      log "warning: $key uses plain HTTP; configure HTTPS before sharing attachment links"
      ;;
  esac
}
configure_pipeline_file_public_base() {
  local key="$1"
  local old_default="$2"
  local current public_base
  current="$(env_value "$key")"
  public_base="${HEALTH_URL%/}"
  if [ -z "$current" ] || [ "$current" = "$old_default" ]; then
    set_env_value "$key" "$public_base"
    current="$public_base"
    log "configured $key from the deployment health URL"
  fi
  validate_pipeline_file_public_base "$key" "$current"
}
configure_pipeline_file_public_base \
  PIPELINE_FILE_PUBLIC_APP_BASE_URL http://localhost:5173
configure_pipeline_file_public_base \
  PIPELINE_FILE_PUBLIC_API_BASE_URL http://localhost:8000
# 远程助手邀请自助接入：邀请函内的兑换/回连地址必须从外部可达，
# 与附件公网地址同一口径——未显式配置时从部署健康地址推导。
configure_pipeline_file_public_base \
  SUPER_ASSISTANT_PUBLIC_API_BASE_URL http://localhost:8000
require_secret() {
  local key="$1"
  local value
  value="$(env_value "$key")"
  [ -n "$value" ] || { log "$key is missing or empty in production .env"; exit 1; }
}
check_secret() {
  local key="$1"
  local insecure="$2"
  local value
  value="$(env_value "$key")"
  if [ -z "$value" ] || [ "$value" = "$insecure" ]; then
    log "$key is missing or still uses an example credential"
    exit 1
  fi
}
ensure_generated_secret() {
  # 服务间共享密钥未显式配置或仍是公开默认值（compose 兜底 / .env.example
  # 示例）时，自动生成强随机值并幂等持久化到服务器 .env——合法部署流程
  # 零手工配置；后端 production fail-closed 只兜绕过本脚本裸跑 compose 的场景。
  local key="$1" value
  value="$(env_value "$key")"
  case "$value" in
    ""|dev-python-gateway-token|change-me-python-gateway-token)
      if ! set_env_value "$key" "$(random_hex 32)"; then
        log "failed to persist generated $key into ${APP_DIR}/.env"
        exit 1
      fi
      log "$key was not configured or used a public example value; generated a random value into ${APP_DIR}/.env"
      ;;
  esac
}
ensure_generated_secret PYTHON_KERNEL_GATEWAY_AUTH_TOKEN
check_secret SECRET_KEY dev-secret-key
check_secret SECRET_KEY change-me-to-a-random-32-char-string
check_secret FIRST_ADMIN_PASSWORD admin123
check_secret POSTGRES_PASSWORD ontoprompt
check_secret DATABASE_URL postgresql://ontoprompt:ontoprompt@db:5432/ontoprompt
check_secret NEO4J_PASSWORD ontoprompt123
check_secret NEO4J_AUTH neo4j/ontoprompt123
check_secret MINIO_ACCESS_KEY minioadmin
check_secret MINIO_SECRET_KEY minioadmin
validate_replica_capacity() {
  # Capacity is per executor instance. 0 would silently starve every newly
  # registered asset; a large single-instance value would exceed the memory
  # budget. Scale with additional same-host instances instead of inflating it.
  local value
  value="$(env_value REPLICA_MAX_CAPTURES)"
  value="${value:-2}"
  case "$value" in
    ""|*[!0-9]*)
      log "REPLICA_MAX_CAPTURES must be a positive integer"
      exit 1
      ;;
  esac
  if [ "$value" -lt 1 ] || [ "$value" -gt 64 ]; then
    log "REPLICA_MAX_CAPTURES must be within 1..64 per executor instance; scale with more instances instead"
    exit 1
  fi
}
validate_replica_capacity
validated_secret_key="$(env_value SECRET_KEY)"
validated_admin_seed="$(env_value FIRST_ADMIN_PASSWORD)"
if [ "${#validated_secret_key}" -lt 32 ]; then
  log "SECRET_KEY must contain at least 32 characters"
  exit 1
fi
if [ "${#validated_admin_seed}" -lt 12 ]; then
  log "FIRST_ADMIN_PASSWORD must contain at least 12 characters"
  exit 1
fi
configured_encryption_key="$(env_value ENCRYPTION_KEY)"
if [ -n "$configured_encryption_key" ] \
    && [[ ! "$configured_encryption_key" =~ ^[A-Za-z0-9_-]{43}=$ ]]; then
  log "ENCRYPTION_KEY must be a URL-safe base64 Fernet key"
  exit 1
fi
unset validated_secret_key validated_admin_seed configured_encryption_key
validate_required_runtime_dependencies() {
  local key
  for key in \
    DATABASE_URL REDIS_URL NEO4J_URI NEO4J_USER NEO4J_PASSWORD \
    MINIO_ENDPOINT MINIO_ACCESS_KEY MINIO_SECRET_KEY \
    N8N_API_URL N8N_API_KEY STEWARD_BROWSER_CDP_URL; do
    require_secret "$key"
  done
  case "$(env_value ENVIRONMENT)" in
    production) ;;
    *) log "ENVIRONMENT=production is required"; exit 1 ;;
  esac
  case "$(env_value DATABASE_URL)" in
    postgresql://*) ;;
    *) log "DATABASE_URL must use the canonical postgresql:// scheme"; exit 1 ;;
  esac
  case "$(env_value REDIS_URL)" in
    redis://:*@*|rediss://:*@*) ;;
    *) log "REDIS_URL must use single-password authentication"; exit 1 ;;
  esac
  case "$(env_value NEO4J_URI)" in
    bolt://*|bolt+s://*|bolt+ssc://*|neo4j://*|neo4j+s://*|neo4j+ssc://*) ;;
    *) log "NEO4J_URI must use a supported Neo4j scheme"; exit 1 ;;
  esac
  case "$(env_value MINIO_ENDPOINT)" in
    *://*) log "MINIO_ENDPOINT must not include a URL scheme"; exit 1 ;;
    *:9001) log "MINIO_ENDPOINT must use the S3 API port, not console port 9001"; exit 1 ;;
    *:*) ;;
    *) log "MINIO_ENDPOINT must include the S3 API port"; exit 1 ;;
  esac
  case "$(env_value N8N_API_URL)" in
    http://*|https://*) ;;
    *) log "N8N_API_URL must be an absolute HTTP(S) URL"; exit 1 ;;
  esac
  case "$(env_value N8N_TIMEOUT_SECONDS)" in
    ""|*[!0-9]*)
      log "N8N_TIMEOUT_SECONDS must be an integer between 3 and 120"
      exit 1
      ;;
    *)
      if [ "$(env_value N8N_TIMEOUT_SECONDS)" -lt 3 ] \
          || [ "$(env_value N8N_TIMEOUT_SECONDS)" -gt 120 ]; then
        log "N8N_TIMEOUT_SECONDS must be an integer between 3 and 120"
        exit 1
      fi
      ;;
  esac
  case "$(env_value STEWARD_BROWSER_CDP_URL)" in
    http://*|https://*) ;;
    *) log "STEWARD_BROWSER_CDP_URL must be an absolute HTTP(S) URL"; exit 1 ;;
  esac
}
validate_required_runtime_dependencies
validate_weknora_config
validate_kaneo_config
validate_grafana_config
validate_hindsight_config
validate_multica_config
if [ -z "$(env_value ENCRYPTION_KEY)" ]; then
  log "ENCRYPTION_KEY is empty; preserving the existing SECRET_KEY-derived encryption key"
fi
strict_image_digests_value="$(env_value STRICT_IMAGE_DIGESTS)"
case "$strict_image_digests_value" in
  1|true|TRUE|yes|YES)
    strict_image_digests_enabled=1
    ;;
  ""|0|false|FALSE|no|NO)
    log "production deployment requires STRICT_IMAGE_DIGESTS=true"
    exit 1
    ;;
  *)
    log "STRICT_IMAGE_DIGESTS must be true in production"
    exit 1
    ;;
esac
process_plugin_runner_assignment_state="$(runtime_env_assignment_state SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE)"
if [ "$process_plugin_runner_assignment_state" = "ambiguous" ]; then
  log "SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE is duplicated or has a case-variant key in .env"
  exit 1
fi
process_plugin_runner_mode="$(env_value SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE)"
case "$process_plugin_runner_mode" in
  direct_dev)
    log "production deployment rejects SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE=direct_dev"
    exit 1
    ;;
  ""|disabled|nats)
    ;;
  *)
    log "SUPER_ASSISTANT_PROCESS_PLUGIN_RUNNER_MODE must be disabled or nats in production"
    exit 1
    ;;
esac
unset process_plugin_runner_assignment_state process_plugin_runner_mode
# The browser consumes attacker-controlled page content and is therefore a
# security boundary even when the operator intentionally keeps the broader
# image policy in warning mode during migration. Never allow its tag to drift.
browser_image_value="$(env_value BROWSER_IMAGE)"
if [[ ! "$browser_image_value" =~ @sha256:[0-9a-fA-F]{64}$ ]]; then
  log "BROWSER_IMAGE must be pinned to an immutable @sha256 digest"
  exit 1
fi
unset browser_image_value
for image_key in \
  POSTGRES_IMAGE REDIS_IMAGE NEO4J_IMAGE MINIO_IMAGE BROWSER_IMAGE \
  PYTHON_BASE_IMAGE NODE_BASE_IMAGE NGINX_BASE_IMAGE \
  WEKNORA_APP_IMAGE WEKNORA_UI_IMAGE WEKNORA_DOCREADER_IMAGE \
  WEKNORA_POSTGRES_IMAGE WEKNORA_REDIS_IMAGE \
  KANEO_APP_IMAGE KANEO_POSTGRES_IMAGE \
  GRAFANA_IMAGE; do
  check_image_digest "$image_key"
done
# Hindsight 只在启用时参与 digest 门禁；未启用时缺键不得阻断全平台。
if [ "$HINDSIGHT_INTERNAL" = "1" ]; then
  for image_key in HINDSIGHT_IMAGE HINDSIGHT_POSTGRES_IMAGE; do
    check_image_digest "$image_key"
  done
fi
# Multica 默认栈同理：只在 MULTICA_ENABLED=true 时参与 digest 门禁（三个
# 官方镜像引用；默认 pin 由 configure_multica_env 落 .env，validate 阶段
# 拒绝非 digest 引用）。
if [ "$MULTICA_INTERNAL" = "1" ]; then
  check_image_digest MULTICA_BACKEND_IMAGE
  check_image_digest MULTICA_WEB_IMAGE
  check_image_digest MULTICA_POSTGRES_IMAGE
fi
if [ "${DEPLOY_VALIDATE_ONLY:-0}" = "1" ]; then
  log "production environment validation succeeded"
  exit 0
fi
if [ "${SKIP_GIT:-0}" != "1" ]; then
  # Direct Git deployment has no archive-supplied manifest. Generate it from
  # the clean checked-out HEAD with the same helper and algorithm; if the host
  # lacks python3, fail explicitly instead of silently skipping attestation.
  command -v python3 >/dev/null 2>&1 || {
    log "direct Git deployment requires python3 to generate the runtime source manifest; use the deployment archive flow or install python3"
    exit 1
  }
  direct_runtime_commit="$(git -C "$APP_DIR" rev-parse HEAD)"
  if [ -n "$EXPECTED_RUNTIME_COMMIT" ] \
      && [ "$EXPECTED_RUNTIME_COMMIT" != "$direct_runtime_commit" ]; then
    log "EXPECTED_RUNTIME_COMMIT ${EXPECTED_RUNTIME_COMMIT} does not match checked-out HEAD ${direct_runtime_commit}"
    exit 1
  fi
  EXPECTED_RUNTIME_COMMIT="$direct_runtime_commit"
  if ! python3 "$SCRIPT_DIR/runtime_source_attestation.py" generate \
      --repo "$APP_DIR" \
      --commit "$EXPECTED_RUNTIME_COMMIT" \
      --output "$SCRIPT_DIR/runtime-source-manifest.json"; then
    log "failed to generate the runtime source manifest for direct Git deployment"
    exit 1
  fi
fi
command -v docker >/dev/null 2>&1 || { log "docker is not installed"; exit 1; }
log "building images"
run_with_retry compose build --pull
run_with_retry compose --profile weknora-bootstrap build --pull weknora-bootstrap
run_with_retry compose --profile kaneo-bootstrap build --pull kaneo-bootstrap
# Resolve the exact image tag Compose will use for the backend service from the
# structured build-print output (target.backend.tags). Do not guess an image
# name and do not pick among label/created-time candidates: the tag emitted for
# this build is the only reference inspected below. The full build-print output
# is parsed only in the pipeline and is never written to disk or logs.
backend_build_tag="$(
  compose build --print backend 2>/dev/null | awk '
    /"backend": \{/ { in_backend=1; next }
    in_backend && /"tags": \[/ { in_tags=1; next }
    in_tags && match($0, /"[^"]+"/) {
      print substr($0, RSTART + 1, RLENGTH - 2)
      exit
    }
  '
)"
if [ -z "$backend_build_tag" ]; then
  log "could not resolve the backend image tag from Compose build --print backend"
  exit 1
fi
case "$backend_build_tag" in
  *[!A-Za-z0-9._:/@-]*)
    log "backend image tag from Compose build --print is malformed"
    exit 1
    ;;
esac
backend_image_platform="$(
  compose_environment docker image inspect --format '{{.Os}}/{{.Architecture}}' \
    "$backend_build_tag" 2>/dev/null || true
)"
if [[ ! "$backend_image_platform" =~ ^[a-z0-9]+/[a-z0-9]+$ ]]; then
  log "could not resolve the backend image platform for the exact Compose build tag"
  exit 1
fi
# OCI indexes include provenance that can change without changing the runnable image.
built_backend_image_id="$(
  compose_environment docker image inspect --platform "$backend_image_platform" \
    --format '{{.Id}}' "$backend_build_tag" 2>/dev/null || true
)"
if [[ ! "$built_backend_image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  log "could not inspect the backend image ID for the exact Compose build tag"
  exit 1
fi
log "backend build tag: ${backend_build_tag}"
log "backend image built: ${built_backend_image_id}"
# WeKnora is initialized before the platform probe/migration. The registration-
# open init container is private and removed before the formal app; the public
# gateway is only started by the final compose up after the platform/app
# health conditions are satisfied.
start_weknora_initializer
run_weknora_bootstrap
stop_weknora_initializer
start_and_verify_weknora_app
# Kaneo is a hard platform dependency and is initialized the same way: the
# private app is (re)initialized and verified before the platform probe,
# migrations and the final compose up that opens the public gateway.
start_and_verify_kaneo_stack
# Grafana is a hard platform dependency as well: the private service is started
# and the one-shot bootstrap (health + shared-admin + folder, then --verify)
# must pass before the platform probe and migrations; a failure makes the
# deployment fail loudly without stopping other already-running services.
start_and_verify_grafana_stack
log "starting required dependency services"
run_with_retry start_required_dependency_services
log "verifying required production dependency connectivity"
run_with_retry compose run --rm --no-deps \
  backend python -m app.shared.dependency_probe
log "stopping backend and Celery worker before database migrations"
# Revision 0055 changes the projection readiness contract. Old application
# processes do not understand that fence, so no API/worker writer may remain
# active while Alembic upgrades the database.
run_with_retry compose stop -t 30 backend pipeline_executor
log "running database migrations"
# Never rewrite Alembic history during a normal deploy.  If a legacy database
# needs a one-off baseline stamp it must be an explicit, audited operation.
log "  validating migration graph"
MIGRATION_HEADS="$(compose run --rm --no-deps backend alembic heads)"
printf '%s\n' "$MIGRATION_HEADS"
HEAD_COUNT="$(printf '%s\n' "$MIGRATION_HEADS" | grep -c '(head)' || true)"
if [ "$HEAD_COUNT" -ne 1 ]; then
  log "migration graph must have exactly one head, found ${HEAD_COUNT}"
  exit 1
fi
migration_head_revision="$(
  printf '%s\n' "$MIGRATION_HEADS" | awk '/\(head\)/ { print $1; exit }'
)"
[ -n "$migration_head_revision" ] || {
  log "could not resolve the migration head revision"
  exit 1
}
running_services="$(compose ps --services --filter status=running 2>/dev/null || true)"
for runtime_service in backend pipeline_executor frontend weknora-gateway kaneo-gateway; do
  if grep -Fxq "$runtime_service" <<<"$running_services"; then
    previous_runtime_services+=("$runtime_service")
  fi
done
if [ "${#previous_runtime_services[@]}" -gt 0 ]; then
  if ! pre_migration_revision="$(database_current_revision)"; then
    log "could not record the pre-migration database revision; refusing to stop the current runtime"
    exit 1
  fi
  log "stopping public frontends, worker and API before the schema migration"
  runtime_stopped_for_migration=1
  compose stop --timeout 30 frontend pipeline_executor backend
  compose stop --timeout 30 weknora-gateway
  compose stop --timeout 30 kaneo-gateway
fi
log "  upgrading to head"
if ! run_with_retry compose run --rm --no-deps backend alembic upgrade head; then
  log "database migration failed; checking the current revision before any runtime restart"
  exit 1
fi
migration_completed=1
# The 0141 migration has retired the palace tables. Clean up only the old
# feature's dedicated external state after all old API/worker writers are
# stopped and before the new stack starts. The script is dry-run by default;
# deployment uses --execute with fixed uploads/Neo4j/Redis/NATS boundaries.
# A custom historical palace root must be handled by an explicit operator
# invocation with --palace-root/--allow-custom-root, never guessed here.
log "retiring legacy super-assistant palace storage"
if ! run_with_retry compose run --rm --no-deps backend \
    python scripts/maintenance/retire_palace_storage.py --execute; then
  log "legacy palace storage retirement failed; refusing to start the new runtime"
  stop_new_runtime_after_failed_deploy
  exit 1
fi
log "starting services"
# integrated_replica_executor is part of this single final up (it is not in
# start_required_dependency_services), so it starts exactly once after Alembic
# reached head and cannot race or double-start beside the migration.
if ! run_with_retry compose up -d --remove-orphans; then
  log "service startup failed; printing redacted backend diagnostics"
  compose ps || true
  redact_backend_failure_logs || true
  stop_new_runtime_after_failed_deploy
  log "migration reached head; do not restart the previous application against the new schema"
  log "restore the pre-deploy database/files backup before rolling back the application"
  exit 1
fi
# Multica 默认栈必须在 readiness 门禁等待之前启动：五项 MULTICA_DEFAULT_*
# 配置完整时，生产 readiness 会门禁 multica_default（backend 真实探测
# Multica），等核心就绪之后再启动会形成“readiness 等 Multica、Multica
# 等部署成功”的自我死锁。这里是独立的有界 compose 调用，失败只停掉本次
# 起了一半的 multica 容器（卷保留），绝不回滚已成功的核心 up；五值未配置
# 时 readiness 不看 multica_default，失败仅是特性级告警。
start_multica_default_stack || log "warning: the Multica default stack start reported an internal failure; the core deployment above is unaffected"
log "waiting for backend, pipeline executor, replica executor and frontend readiness: ${READINESS_URL}"
for i in $(seq 1 60); do
  if curl -fsS --connect-timeout 5 --max-time 10 "$READINESS_URL" >/dev/null \
      && check_pipeline_executor \
      && check_replica_executor \
      && check_frontend_assets \
      && check_weknora_public_entry \
      && check_kaneo_public_entry \
      && check_grafana_public_entry; then
    if ! verify_runtime_source_attestation; then
      log "runtime source attestation failed; refusing to report deployment success"
      stop_new_runtime_after_failed_deploy
      log "migration reached head; restore the pre-deploy backup before rolling back the application"
      exit 1
    fi
    log "deployment succeeded"
    # 可选记忆栈在核心成功后单独启动（backend 已就绪，Hindsight 的 LLM 回调
    # 目标存在）。调用侧兜底：函数内部任何未预期非零也只记录告警。
    # Multica 默认栈已在上方 readiness 门禁等待之前启动（见调用处注释）。
    start_hindsight_memory_stack || log "warning: the optional Hindsight memory stack start reported an internal failure; the succeeded core deployment above is unaffected"
    compose ps
    exit 0
  fi
  log "health check not ready (${i}/60)"
  sleep 5
done
log "deployment health check failed"
compose ps || true
redact_compose_logs backend dbx browser frontend pipeline_executor \
  integrated_replica_executor \
  weknora-app weknora-ui weknora-gateway weknora-docreader \
  kaneo-db kaneo-app kaneo-gateway grafana grafana-bootstrap \
  multica multica-web multica-postgres || true
stop_new_runtime_after_failed_deploy
log "migration reached head; restore the pre-deploy backup before rolling back the application"
exit 1
