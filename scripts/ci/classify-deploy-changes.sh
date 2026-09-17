#!/usr/bin/env bash
# Classify a push's changed paths into deploy-pipeline flags.
#
# This is family-level, fail-closed classification — not file-level incremental
# tests. Unknown paths, empty diffs, --full, and changes to this script or the
# deploy workflow force every flag on.
#
# Usage:
#   classify-deploy-changes.sh --stdin
#   classify-deploy-changes.sh --full
#   classify-deploy-changes.sh --self-test
#
# Flags written to stdout and to $GITHUB_OUTPUT when that file is set:
#   run_backend, run_frontend_tests, run_frontend_build, run_docker, run_deploy
set -Eeuo pipefail

emit_flags() {
  local run_backend="$1"
  local run_frontend_tests="$2"
  local run_frontend_build="$3"
  local run_docker="$4"
  local run_deploy="$5"
  if [ "$run_deploy" = "true" ]; then
    run_docker="true"
  fi
  if [ "$run_docker" = "true" ]; then
    run_frontend_build="true"
  fi
  local line
  for line in \
    "run_backend=${run_backend}" \
    "run_frontend_tests=${run_frontend_tests}" \
    "run_frontend_build=${run_frontend_build}" \
    "run_docker=${run_docker}" \
    "run_deploy=${run_deploy}"
  do
    printf '%s\n' "$line"
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
      printf '%s\n' "$line" >> "$GITHUB_OUTPUT"
    fi
  done
}

emit_full() {
  emit_flags true true true true true
}

# One family per path. "full" means the whole pipeline; "none" means gates only.
classify_path() {
  local f="$1"
  case "$f" in
    .github/workflows/deploy-nano-ontoprompt.yml|\
    scripts/ci/classify-deploy-changes.sh|\
    scripts/ci/test-classify-deploy-changes.sh)
      printf 'full\n'
      return
      ;;
    */README.md|README.md|AGENTS.md|DESIGN.md|\
    .github/workflows/README.md|docs/*)
      printf 'none\n'
      return
      ;;
    .gitignore)
      printf 'none\n'
      return
      ;;
    backend/.test_durations)
      # Not shipped, so do not deploy; still run backend so a bad split
      # table cannot poison the next product deploy's shard timeout.
      printf 'backend_tests\n'
      return
      ;;
    backend/tests/*|backend/pytest.ini)
      printf 'backend_tests\n'
      return
      ;;
    frontend/src/test/*|frontend/playwright*.ts|frontend/scripts/*|frontend/eslint.config.js)
      printf 'frontend_tests\n'
      return
      ;;
    frontend/*)
      printf 'frontend_product\n'
      return
      ;;
    backend/*)
      printf 'backend_product\n'
      return
      ;;
    config/*)
      printf 'none\n'
      return
      ;;
    test_data/*)
      printf 'backend_tests\n'
      return
      ;;
    .github/workflows/ci.yml)
      printf 'none\n'
      return
      ;;
    .env.example)
      printf 'env_example\n'
      return
      ;;
    .dockerignore|docker-compose.prod.yml|docker/*|deploy/*|agentloop/*|\
    scripts/ci/create-deployment-archive.sh|\
    scripts/ci/validate-deploy-app-dir.sh|\
    scripts/ci/materialize-production-dependencies.sh)
      printf 'infra\n'
      return
      ;;
    scripts/ci/*)
      printf 'none\n'
      return
      ;;
    docker-compose.e2e.yml|docker-compose.local.yml|scripts/data/*)
      printf 'full\n'
      return
      ;;
  esac
  printf 'unknown\n'
}

classify_files() {
  local run_backend=false
  local run_frontend_tests=false
  local run_frontend_build=false
  local run_docker=false
  local run_deploy=false
  local seen=0
  local path family
  while IFS= read -r path || [ -n "${path}" ]; do
    [ -n "${path}" ] || continue
    seen=1
    family="$(classify_path "${path}")"
    case "${family}" in
      full|unknown)
        emit_full
        return
        ;;
      none) ;;
      backend_tests)
        run_backend=true
        ;;
      frontend_tests)
        run_frontend_tests=true
        ;;
      frontend_product)
        run_frontend_tests=true
        run_deploy=true
        ;;
      backend_product)
        run_backend=true
        run_deploy=true
        ;;
      env_example)
        run_backend=true
        run_deploy=true
        ;;
      infra)
        # Compose and deploy-prod.sh are locked by backend production-config
        # pytest, not only test-deploy-guards.sh.
        run_backend=true
        run_deploy=true
        ;;
      *)
        emit_full
        return
        ;;
    esac
  done
  if [ "${seen}" -eq 0 ]; then
    emit_full
    return
  fi
  emit_flags \
    "${run_backend}" \
    "${run_frontend_tests}" \
    "${run_frontend_build}" \
    "${run_docker}" \
    "${run_deploy}"
}

usage() {
  printf 'usage: classify-deploy-changes.sh --stdin | --full | --self-test\n' >&2
  exit 2
}

mode="${1:-}"
case "${mode}" in
  --full)
    emit_full
    ;;
  --stdin)
    classify_files
    ;;
  --self-test)
    bash "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/test-classify-deploy-changes.sh"
    ;;
  *)
    usage
    ;;
esac
