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

compose_environment() {
  # Docker Compose gives exported shell variables precedence over the project
  # .env file. The manifest has already been validated and persisted into
  # .env, so clear every interpolation/config authority that could otherwise
  # make the containers differ from what this script validated. Values are
  # deliberately named here rather than printed or dynamically expanded.
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
    -u COMPOSE_PROFILES \
    -u COMPOSE_PROJECT_NAME \
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
  if ! compose stop --timeout 30 frontend pipeline_executor backend; then
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

start_required_dependency_services() {
  start_integrated_dbx_if_needed
  stop_disabled_hindsight_stack
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
  # 可选记忆栈默认关闭；启用时由 configure_hindsight_env 生成独立凭据。
  set_env_value_in_file HINDSIGHT_ENABLED false "$bootstrap_tmp_path"
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

configure_hindsight_env() {
  # Hindsight 长期记忆（可选）：HINDSIGHT_ENABLED=true 时生成/保留独立凭据并
  # 钉扎镜像 digest；未启用时不动任何值（缺 secret 不阻断平台部署）。
  local current
  local -a updates=()
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
  current="$(env_value HINDSIGHT_TIMEOUT_SECONDS)"
  [ -n "$current" ] || updates+=("HINDSIGHT_TIMEOUT_SECONDS=2")
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
configure_hindsight_env
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
    HINDSIGHT_CP_ACCESS_KEY; do
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
validate_hindsight_config
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
  KANEO_APP_IMAGE KANEO_POSTGRES_IMAGE; do
  check_image_digest "$image_key"
done
# Hindsight 只在启用时参与 digest 门禁；未启用时缺键不得阻断全平台。
if [ "$HINDSIGHT_INTERNAL" = "1" ]; then
  for image_key in HINDSIGHT_IMAGE HINDSIGHT_POSTGRES_IMAGE; do
    check_image_digest "$image_key"
  done
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
if ! run_with_retry compose up -d --remove-orphans; then
  log "service startup failed; printing redacted backend diagnostics"
  compose ps || true
  redact_backend_failure_logs || true
  stop_new_runtime_after_failed_deploy
  log "migration reached head; do not restart the previous application against the new schema"
  log "restore the pre-deploy database/files backup before rolling back the application"
  exit 1
fi
log "waiting for backend, pipeline executor and frontend readiness: ${READINESS_URL}"
for i in $(seq 1 60); do
  if curl -fsS --connect-timeout 5 --max-time 10 "$READINESS_URL" >/dev/null \
      && check_pipeline_executor \
      && check_frontend_assets \
      && check_weknora_public_entry \
      && check_kaneo_public_entry; then
    if ! verify_runtime_source_attestation; then
      log "runtime source attestation failed; refusing to report deployment success"
      stop_new_runtime_after_failed_deploy
      log "migration reached head; restore the pre-deploy backup before rolling back the application"
      exit 1
    fi
    log "deployment succeeded"
    # 可选记忆栈在核心成功后单独启动（backend 已就绪，Hindsight 的 LLM 回调
    # 目标存在）。调用侧兜底：函数内部任何未预期非零也只记录告警。
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
  weknora-app weknora-ui weknora-gateway weknora-docreader \
  kaneo-db kaneo-app kaneo-gateway || true
stop_new_runtime_after_failed_deploy
log "migration reached head; restore the pre-deploy backup before rolling back the application"
exit 1
