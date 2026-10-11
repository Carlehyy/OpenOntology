#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(
  cd -- "${DEPLOY_SOURCE_ROOT:-$SCRIPT_DIR/../..}"
  pwd
)"
ARCHIVE_PATH="${1:?usage: create-deployment-archive.sh ARCHIVE_PATH}"

required_paths=(
  ".env.example"
  "docker-compose.prod.yml"
  "deploy/production.dependencies.env"
  "backend/.dockerignore"
  "backend/Dockerfile"
  "backend/alembic.ini"
  "backend/alembic"
  "backend/app"
  "backend/pyproject.toml"
  "backend/uv.lock"
  "backend/scripts/maintenance"
  "backend/scripts/download_voice_model.py"
  "frontend/.dockerignore"
  "frontend/Dockerfile.prod"
  "frontend/dist"
  "frontend/index.html"
  "frontend/nginx"
  "frontend/package.json"
  "frontend/package-lock.json"
  "frontend/postcss.config.js"
  "frontend/public"
  "frontend/src"
  "frontend/tailwind.config.ts"
  "frontend/tsconfig.app.json"
  "frontend/tsconfig.json"
  "frontend/tsconfig.node.json"
  "frontend/vite.config.ts"
  "docker/browser"
  "deploy/deploy-prod.sh"
  "scripts/ci/validate-deploy-app-dir.sh"
)

for required_path in "${required_paths[@]}"; do
  if [[ ! -e "$REPOSITORY_ROOT/$required_path" ]]; then
    printf 'deployment archive input is missing: %s\n' "$required_path" >&2
    exit 1
  fi
done

# The server receives only build/runtime inputs plus the documented password
# recovery script family. Tests, docs, fixtures and local build reports remain
# CI artifacts and cannot accidentally enter the deployed source tree.
tar \
  --create \
  --gzip \
  --file "$ARCHIVE_PATH" \
  --directory "$REPOSITORY_ROOT" \
  --exclude='*/__pycache__' \
  --exclude='*.py[co]' \
  --exclude='frontend/src/test' \
  "${required_paths[@]}"
chmod 600 "$ARCHIVE_PATH"

printf 'deployment archive created: %s\n' "$ARCHIVE_PATH"
