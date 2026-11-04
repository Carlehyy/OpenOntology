#!/usr/bin/env bash
set -Eeuo pipefail

target="${1:-deploy/production.dependencies.env}"
target_dir="$(dirname "$target")"
mkdir -p "$target_dir"
umask 077
tmp_file="$(mktemp "${target}.XXXXXX")"
trap 'rm -f "$tmp_file"' EXIT

append_required() {
  local key="$1"
  local source_name="$2"
  local value="${!source_name:-}"
  if [ -z "$value" ]; then
    echo "$source_name is required" >&2
    exit 1
  fi
  case "$value" in
    *$'\n'*|*$'\r'*)
      echo "$source_name must be a single-line value" >&2
      exit 1
      ;;
  esac
  printf '%s=%s\n' "$key" "$value" >> "$tmp_file"
}

append_port() {
  local key="$1"
  local source_name="$2"
  local value="${!source_name:-}"
  append_required "$key" "$source_name"
  case "$value" in
    *[!0-9]*)
      echo "$source_name must be an integer between 1 and 65535" >&2
      exit 1
      ;;
  esac
  if [ "$value" -lt 1 ] || [ "$value" -gt 65535 ]; then
    echo "$source_name must be an integer between 1 and 65535" >&2
    exit 1
  fi
}

append_boolean() {
  local key="$1"
  local source_name="$2"
  local value="${!source_name:-}"
  case "$value" in
    true|false)
      append_required "$key" "$source_name"
      ;;
    *)
      echo "$source_name must be true or false" >&2
      exit 1
      ;;
  esac
}

append_optional() {
  local key="$1"
  local source_name="$2"
  local value="${!source_name:-}"
  [ -n "$value" ] || return 0
  append_required "$key" "$source_name"
}

append_optional_digest() {
  local key="$1"
  local source_name="$2"
  local value="${!source_name:-}"
  [ -n "$value" ] || return 0
  case "$value" in
    *@sha256:*) ;;
    *)
      echo "$source_name must be pinned with @sha256 when set" >&2
      exit 1
      ;;
  esac
  append_required "$key" "$source_name"
}

append_bounded_integer_default() {
  local key="$1"
  local source_name="$2"
  local default_value="$3"
  local minimum="$4"
  local maximum="$5"
  local value="${!source_name:-$default_value}"
  case "$value" in
    *[!0-9]*)
      echo "$source_name must be an integer between $minimum and $maximum" >&2
      exit 1
      ;;
  esac
  if [ "$value" -lt "$minimum" ] || [ "$value" -gt "$maximum" ]; then
    echo "$source_name must be an integer between $minimum and $maximum" >&2
    exit 1
  fi
  printf '%s=%s\n' "$key" "$value" >> "$tmp_file"
}

cat >> "$tmp_file" <<'EOF'
ENVIRONMENT=production
EOF
append_port PUBLIC_PORT PROD_PUBLIC_PORT
append_required POSTGRES_DB PROD_POSTGRES_DB
append_required POSTGRES_USER PROD_POSTGRES_USER
append_required POSTGRES_PASSWORD PROD_POSTGRES_PASSWORD
append_required DATABASE_URL PROD_DATABASE_URL
append_required REDIS_URL PROD_REDIS_URL
append_required NEO4J_URI PROD_NEO4J_URI
append_required NEO4J_USER PROD_NEO4J_USER
append_required NEO4J_PASSWORD PROD_NEO4J_PASSWORD
append_required NEO4J_AUTH PROD_NEO4J_AUTH
append_required MINIO_ENDPOINT PROD_MINIO_ENDPOINT
append_required MINIO_ACCESS_KEY PROD_MINIO_ACCESS_KEY
append_required MINIO_SECRET_KEY PROD_MINIO_SECRET_KEY
append_boolean MINIO_USE_SSL PROD_MINIO_USE_SSL
append_required N8N_API_URL PROD_N8N_API_URL
append_required N8N_API_KEY PROD_N8N_API_KEY
append_bounded_integer_default \
  N8N_TIMEOUT_SECONDS PROD_N8N_TIMEOUT_SECONDS 30 3 120

# Models, knowledge bases and MCP endpoints are configured in WeKnora's Web UI.
# Only optional deployment identity and immutable image pins belong here.
append_optional WEKNORA_WEB_URL PROD_WEKNORA_WEB_URL
append_optional WEKNORA_ADMIN_USER PROD_WEKNORA_ADMIN_USER
append_optional WEKNORA_ADMIN_EMAIL PROD_WEKNORA_ADMIN_EMAIL
append_optional_digest WEKNORA_APP_IMAGE PROD_WEKNORA_APP_IMAGE
append_optional_digest WEKNORA_UI_IMAGE PROD_WEKNORA_UI_IMAGE
append_optional_digest WEKNORA_DOCREADER_IMAGE PROD_WEKNORA_DOCREADER_IMAGE
append_optional_digest WEKNORA_POSTGRES_IMAGE PROD_WEKNORA_POSTGRES_IMAGE
append_optional_digest WEKNORA_REDIS_IMAGE PROD_WEKNORA_REDIS_IMAGE

chmod 600 "$tmp_file"
mv "$tmp_file" "$target"
trap - EXIT
