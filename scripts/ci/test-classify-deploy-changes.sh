#!/usr/bin/env bash
# Fixtures for family-level deploy classification. Fail-closed: unknown paths
# and classifier/workflow edits must force the full pipeline.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CLASSIFY="$SCRIPT_DIR/classify-deploy-changes.sh"
failures=0

expect_flags() {
  local name="$1"
  local files="$2"
  local want_backend="$3"
  local want_frontend_tests="$4"
  local want_frontend_build="$5"
  local want_docker="$6"
  local want_deploy="$7"
  local output got
  output="$(printf '%s\n' "${files}" | bash "$CLASSIFY" --stdin)"
  got="$(
    printf '%s\n' "${output}" | awk -F= '
      $1=="run_backend"{b=$2}
      $1=="run_frontend_tests"{ft=$2}
      $1=="run_frontend_build"{fb=$2}
      $1=="run_docker"{d=$2}
      $1=="run_deploy"{p=$2}
      END{print b, ft, fb, d, p}
    '
  )"
  local want="${want_backend} ${want_frontend_tests} ${want_frontend_build} ${want_docker} ${want_deploy}"
  if [ "${got}" != "${want}" ]; then
    printf 'FAIL %s\n  files:\n%s\n  got:  %s\n  want: %s\n' \
      "${name}" "${files}" "${got}" "${want}" >&2
    failures=$((failures + 1))
    return
  fi
  printf 'ok %s\n' "${name}"
}

expect_flags "empty-diff-is-full" "" true true true true true

expect_flags "docs-only" "docs/operations/deployment.md" false false false false false
expect_flags "root-markdown" "README.md" false false false false false
expect_flags "nested-readme" "backend/app/README.md" false false false false false
expect_flags "design-md" "DESIGN.md" false false false false false

expect_flags "durations-only" "backend/.test_durations" true false false false false

expect_flags "backend-tests-only" \
  $'backend/tests/data_channel/test_steward_browser_live.py' \
  true false false false false

expect_flags "backend-tests-and-durations" \
  $'backend/.test_durations\nbackend/tests/super_assistant/test_tools_router.py' \
  true false false false false

expect_flags "frontend-e2e-only" \
  "frontend/src/test/e2e/agent_composer_enter.spec.ts" \
  false true false false false

expect_flags "frontend-gate-script-only" \
  "frontend/scripts/check-component-convergence.mjs" \
  false true false false false

expect_flags "frontend-product" \
  $'frontend/src/components/ui/Modal.tsx\nfrontend/src/test/e2e/ontology_evolution.spec.ts' \
  false true true true true

expect_flags "backend-product" \
  "backend/app/super_assistant/models.py" \
  true false true true true

expect_flags "backend-product-with-docs" \
  $'backend/app/data_channel/steward/orchestrator.py\ndocs/operations/README.md' \
  true false true true true

expect_flags "mixed-full" \
  $'backend/app/super_assistant/runtime.py\nfrontend/src/pages/super-assistant/components/AssistantConfiguration.tsx' \
  true true true true true

expect_flags "pr-ci-workflow-only" ".github/workflows/ci.yml" false false false false false
expect_flags "deploy-workflow-forces-full" \
  ".github/workflows/deploy-nano-ontoprompt.yml" \
  true true true true true
expect_flags "classifier-forces-full" \
  "scripts/ci/classify-deploy-changes.sh" \
  true true true true true

expect_flags "infra-deploy-script" \
  "deploy/deploy-prod.sh" \
  true false true true true

expect_flags "infra-compose" \
  "docker-compose.prod.yml" \
  true false true true true

expect_flags "env-example-runs-backend-and-deploy" \
  ".env.example" \
  true false true true true

expect_flags "config-center-only" "config/app/main.py" false false false false false
expect_flags "hygiene-script-only" \
  "scripts/ci/check-repository-hygiene.sh" \
  false false false false false

expect_flags "archive-script-deploys" \
  "scripts/ci/create-deployment-archive.sh" \
  true false true true true

expect_flags "materializer-runs-backend-and-deploy" \
  "scripts/ci/materialize-production-dependencies.sh" \
  true false true true true

expect_flags "rename-product-into-tests-still-deploys" \
  $'backend/app/foo.py\nbackend/tests/foo.py' \
  true false true true true

expect_flags "unknown-path-forces-full" "not-a-real/family.bin" true true true true true
expect_flags "local-compose-forces-full" "docker-compose.local.yml" true true true true true
expect_flags "fixtures-run-backend-no-deploy" "test_data/医疗/clinical_data.xlsx" true false false false false

# Historical deploy pushes that should have skipped work.
expect_flags "history-e2e-dead-var" \
  "frontend/src/test/e2e/agent_composer_enter.spec.ts" \
  false true false false false
expect_flags "history-auto-durations-refresh" \
  "backend/.test_durations" \
  true false false false false
expect_flags "history-migration-0110" \
  $'backend/alembic/versions/2026_09_17_0110_sa_browser_source.py\nbackend/app/super_assistant/models.py' \
  true false true true true
expect_flags "history-style-ontology-dialog" \
  $'frontend/scripts/color-gate-manifest.mjs\nfrontend/src/pages/ontologies/detail/tabs/BusinessModelDialog.tsx\nfrontend/src/pages/ontologies/detail/tabs/StructureDocDialog.tsx\nfrontend/src/pages/ontologies/detail/tabs/ontology-dialogs.css' \
  false true true true true
expect_flags "history-image-pin" \
  $'.env.example\ndeploy/deploy-prod.sh' \
  true false true true true

full_output="$(bash "$CLASSIFY" --full)"
if ! grep -qx 'run_backend=true' <<<"${full_output}" \
  || ! grep -qx 'run_deploy=true' <<<"${full_output}"; then
  printf 'FAIL --full must enable the complete pipeline\n%s\n' "${full_output}" >&2
  failures=$((failures + 1))
else
  printf 'ok --full\n'
fi

if [ "${failures}" -ne 0 ]; then
  printf '%s classification fixture(s) failed\n' "${failures}" >&2
  exit 1
fi
