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

# 从 stdin 读取待扫描文本，输出命中行（行号:行内容）。
# $1: yes/no —— should_scan_drive_paths 对该文件的盘符检查判定。
# 全仓扫描与下方 --self-test 回归复用同一判定逻辑。
scan_content_for_personal_paths() {
  local scan_drive_paths="$1"
  # 两个检测分支独立消费同一份输入（不能都读 stdin：先到者会耗尽流）。
  # 命令替换剥除的尾部空行不参与任何模式匹配，不影响判定。
  local content
  content="$(cat)"
  {
    printf '%s\n' "$content" | grep -nE \
      '(/Users/[^[:space:]`"'"'"']+|/home/[[:alnum:]_.-]+/|\.codex/worktrees/|[[:alpha:]]:[\\/]Users[\\/])' \
      || true
    if [[ "$scan_drive_paths" == "yes" ]]; then
      # 冻结的 Grafana 官方 URL 模板 %(protocol)s://（GF_SERVER_ROOT_URL：
      # docker-compose.local/prod 与 deploy 守卫断言共三处出现）会被一般
      # 盘符模式误识别为盘符 s:/。只中和该精确字面量的局部误匹配后继续
      # 扫描同行剩余内容——模板前/后的真实个人路径仍被拦截。
      printf '%s\n' "$content" | sed 's/%(protocol)s:\/\//__OO_URL_TEMPLATE__/g' | grep -nE \
        '(^|[^[:alnum:]+.-])[[:alpha:]]:[\\/]' \
        || true
    fi
  } | sort -t: -k1,1n -u
}

# 判定逻辑的自测（全部合成值；本脚本自身在其既有扫描排除范围内）。
# $1: 描述，$2: 期望 match/nomatch，$3: 盘符检查 yes/no，$4: 行内容。
self_test_check() {
  local description="$1" expected="$2" scan_drive_paths="$3" line="$4"
  local got
  got="$(printf '%s\n' "$line" | scan_content_for_personal_paths "$scan_drive_paths")"
  if [[ "$expected" == "match" ]] && [[ -z "$got" ]]; then
    printf 'self-test failed (expected match): %s\n' "$description" >&2
    return 1
  fi
  if [[ "$expected" == "nomatch" ]] && [[ -n "$got" ]]; then
    printf 'self-test failed (expected no match): %s\n' "$description" >&2
    return 1
  fi
  return 0
}

run_hygiene_self_test() {
  local failures=0
  # 三种实际出现形式（Compose 值 / deploy 守卫命令断言 / 模板本身）
  # 不再产生盘符误报（t6 Fedora 真实实测曾对这三行误报）。
  self_test_check 'compose value form' nomatch yes \
    'GF_SERVER_ROOT_URL: "%(protocol)s://%(domain)s/grafana/"' \
    || failures=$((failures + 1))
  self_test_check 'deploy guard assertion form' nomatch yes \
    '|| ! grep -Fq '\''GF_SERVER_ROOT_URL: "%(protocol)s://%(domain)s/grafana/"'\'' <<<"$grafana_block" \' \
    || failures=$((failures + 1))
  self_test_check 'template itself' nomatch yes \
    '%(protocol)s://%(domain)s/grafana/' \
    || failures=$((failures + 1))
  # 正常 URL 不是盘符路径。
  self_test_check 'normal http url' nomatch yes \
    'GRAFANA_BASE_URL=http://grafana:3000' \
    || failures=$((failures + 1))
  self_test_check 'normal https url' nomatch yes \
    'root_url https://example.com/grafana/' \
    || failures=$((failures + 1))
  # 普通盘符路径（正/反斜杠）与既有 /Users/、/home/、worktree 检测仍拦截。
  self_test_check 'plain drive path' match yes \
    'cache_dir = D:/tmp/cache' \
    || failures=$((failures + 1))
  self_test_check 'backslash drive path' match yes \
    'openssl_bin C:\OpenSSL\bin' \
    || failures=$((failures + 1))
  self_test_check 'users absolute path' match yes \
    'copied from /Users/someone/dev/repo.txt' \
    || failures=$((failures + 1))
  self_test_check 'home path' match yes \
    'source /home/devuser/project/run.sh' \
    || failures=$((failures + 1))
  self_test_check 'codex worktree path' match yes \
    'checkout .codex/worktrees/repo-x' \
    || failures=$((failures + 1))
  # 同一行内模板前/后的真实路径必须仍被拦截。
  self_test_check 'real drive path before template on same line' match yes \
    'COPY --from=cache D:/build/artifacts && GF_SERVER_ROOT_URL: "%(protocol)s://%(domain)s/grafana/"' \
    || failures=$((failures + 1))
  self_test_check 'real drive path after template on same line' match yes \
    'GF_SERVER_ROOT_URL: "%(protocol)s://%(domain)s/grafana/" && curl C:\tools\curl.exe' \
    || failures=$((failures + 1))
  # 文件级盘符检查开关（测试等排除范围）：盘符模式不适用，/Users/ 等仍拦截。
  self_test_check 'drive scan disabled skips drive pattern' nomatch no \
    'cache_dir = D:/tmp/cache' \
    || failures=$((failures + 1))
  self_test_check 'drive scan disabled still catches users path' match no \
    'copied from /Users/someone/dev/repo.txt' \
    || failures=$((failures + 1))

  if ((failures > 0)); then
    printf 'repository-hygiene self-test failed with %d error(s).\n' "$failures" >&2
    exit 1
  fi
  printf 'repository-hygiene self-test passed.\n'
}

if [[ "${1:-}" == "--self-test" ]]; then
  run_hygiene_self_test
  exit 0
fi

# 正常门禁入口同样先跑同一回归自测，既有 CI 门禁因此自动包含这些用例。
run_hygiene_self_test

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
    if should_scan_drive_paths "$source_file"; then
      scan_content_for_personal_paths yes < "$source_file"
    else
      scan_content_for_personal_paths no < "$source_file"
    fi
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
