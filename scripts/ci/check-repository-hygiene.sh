#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repository_root"

error_count=0

report_error() {
  printf 'ERROR [repository-hygiene] %s\n' "$*" >&2
  error_count=$((error_count + 1))
}

required_readmes=(
  "README.md"
  "backend/README.md"
  "backend/app/README.md"
  "backend/app/data_channel/README.md"
  "backend/app/ontologies/README.md"
  "backend/scripts/README.md"
  "backend/tests/README.md"
  "config/README.md"
  "deploy/README.md"
  "docker/README.md"
  "docs/README.md"
  "docs/development/README.md"
  "docs/operations/README.md"
  "frontend/README.md"
  "frontend/src/README.md"
  "frontend/src/pages/README.md"
  "frontend/src/palantir-graph/README.md"
  "frontend/src/features/README.md"
  "frontend/src/features/overview/README.md"
  "frontend/src/test/README.md"
  "frontend/src/test/unit/README.md"
  "frontend/src/test/e2e/README.md"
  "frontend/scripts/README.md"
  "scripts/README.md"
  "scripts/ci/README.md"
  "scripts/data/README.md"
  "test_data/README.md"
  ".github/workflows/README.md"
)

for readme_file in "${required_readmes[@]}"; do
  if [[ ! -f "$readme_file" ]]; then
    report_error "required directory guide is missing: $readme_file"
  fi
done

legacy_process_paths=(
  "backend/scripts/dev"
  "test_data/HR"
  "test_data/api"
  "test_data/db"
  "test_data/documents"
  "test_data/frontend"
  "test_data/prompts"
)
for legacy_path in "${legacy_process_paths[@]}"; do
  if [[ -e "$legacy_path" || -L "$legacy_path" ]]; then
    report_error "legacy process-data path must not return: $legacy_path"
  fi
done

while IFS= read -r -d '' misplaced_test_program; do
  report_error "test_data contains fixtures only; move executable test code to a test or scripts directory: $misplaced_test_program"
done < <(
  find test_data -type f \
    \( -name '*.py' -o -name '*.mjs' -o -name '*.js' -o -name '*.sh' \) \
    -print0
)

while IFS= read -r -d '' root_v2_test; do
  report_error "backend/tests/v2 root is a contract-family index; move the test into a capability subdirectory: $root_v2_test"
done < <(
  find backend/tests/v2 -maxdepth 1 -type f -name 'test_*.py' -print0
)

while IFS= read -r -d '' ignored_tracked_file; do
  if [[ -e "$ignored_tracked_file" || -L "$ignored_tracked_file" ]]; then
    report_error "ignored file is still tracked and present: $ignored_tracked_file"
  fi
done < <(git ls-files -ci --exclude-standard -z)

if git ls-files --error-unmatch deploy/production.dependencies.env >/dev/null 2>&1; then
  report_error "production dependency manifest must stay untracked; it is materialized from Repository secrets at deploy time"
fi

required_lockfiles=(
  "backend/uv.lock"
  "config/uv.lock"
  "frontend/package-lock.json"
)
for lockfile in "${required_lockfiles[@]}"; do
  if [[ ! -f "$lockfile" ]]; then
    report_error "required lockfile is missing: $lockfile"
  fi
done

competing_frontend_lockfiles=(
  "frontend/bun.lock"
  "frontend/bun.lockb"
  "frontend/npm-shrinkwrap.json"
  "frontend/pnpm-lock.yaml"
  "frontend/yarn.lock"
  "frontend/yarn.lock.yml"
)
for lockfile in "${competing_frontend_lockfiles[@]}"; do
  if [[ -e "$lockfile" || -L "$lockfile" ]]; then
    report_error "frontend uses npm/package-lock.json; remove competing lockfile: $lockfile"
  fi
done

is_generic_documented_path() {
  local file_name="$1"
  local matched_line="$2"
  [[
    "$file_name" == "config/static/app.js"
    && "$matched_line" == *'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'*
  ]]
}

should_scan_drive_paths() {
  local file_name="$1"
  # .test_durations 以参数化测试 ID 为键，内嵌的对抗性 fixture（如 C:/ 盘符
  # 用例）与被排除的测试目录同理，不是个人路径。
  [[
    "$file_name" != backend/tests/*
    && "$file_name" != backend/.test_durations
    && "$file_name" != config/tests/*
    && "$file_name" != test_data/*
  ]]
}

while IFS= read -r -d '' source_file; do
  if [[ ! -f "$source_file" ]] || ! grep -Iq . "$source_file"; then
    continue
  fi
  # This file necessarily contains the forbidden-path expressions themselves.
  if [[ "$source_file" == "scripts/ci/check-repository-hygiene.sh" ]]; then
    continue
  fi

  while IFS= read -r matched_line; do
    if is_generic_documented_path "$source_file" "$matched_line"; then
      continue
    fi
    line_number="${matched_line%%:*}"
    report_error "personal absolute/worktree path in $source_file:$line_number"
  done < <(
    {
      grep -nE \
        '(/Users/[^[:space:]`"'"'"']+|/home/[[:alnum:]_.-]+/|\.codex/worktrees/|[[:alpha:]]:[\\/]Users[\\/])' \
        "$source_file" \
        || true
      if should_scan_drive_paths "$source_file"; then
        grep -nE \
          '(^|[^[:alnum:]+.-])[[:alpha:]]:[\\/]' \
          "$source_file" \
          || true
      fi
    } | sort -t: -k1,1n -u
  )
done < <(git ls-files --cached --others --exclude-standard -z)

node scripts/ci/check-lean-tree.mjs --self-test
node frontend/scripts/check-feature-boundaries.mjs
node scripts/ci/check-lean-tree.mjs

if ((error_count > 0)); then
  printf 'Repository hygiene failed with %d error(s).\n' "$error_count" >&2
  exit 1
fi

printf 'Repository hygiene passed: directory guides, fixture boundaries, ignored files, local paths, and lockfiles are clean.\n'
