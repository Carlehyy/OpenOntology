#!/usr/bin/env python3
"""Offline-safe, ownership-checked integrated E2E stack and deterministic oracle.

Uses the standard library and local Docker images only. No dotenv is loaded,
no image is pulled, and no unrelated process/container/volume is stopped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "oo-integrated-20261024"
COMPOSE = ROOT / "docker-compose.integrated-e2e.yml"
ART = ROOT / ".artifacts" / "integrated-e2e"
STATE = ART / "resources.json"
PORTS = {
    "db": [45432], "source-pg": [45433], "mysql": [43306],
    "redis": [46379], "nats": [44222, 48222],
    "neo4j": [47474, 47687], "minio": [49000, 49001],
}
IMAGES = {
    "db": "postgres:16-alpine", "source-pg": "postgres:16-alpine",
    "mysql": "mysql:8.4", "redis": "redis:7-alpine", "nats": "nats:alpine",
    "neo4j": "neo4j:5.20-community", "minio": "minio/minio:latest",
}
COLUMNS = ["id", "join_key", "amount_cents", "active", "observed_at", "source_tag", "note"] + [f"c{i:02d}" for i in range(1, 41)]
FINAL_SCALES = (2_300_000, 10_000_000)
SAFE_ENV = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG") if key in os.environ}
SAFE_ENV["COMPOSE_PROJECT_NAME"] = PROJECT


def run(args: list[str], *, text: str | None = None) -> str:
    process = subprocess.run(args, input=text, text=True, capture_output=True, env=SAFE_ENV, cwd=ROOT)
    if process.returncode:
        raise RuntimeError(f"{args[0]} failed (exit {process.returncode}): {process.stderr[-3000:]}")
    return process.stdout


def compose(*args: str) -> str:
    # No .env discovery, interpolation, implicit pull, or inherited profile.
    return run(["docker", "compose", "--env-file", os.devnull, "-p", PROJECT,
                "-f", str(COMPOSE), "--profile", "mysql", *args])


def write_report(name: str, report: Any) -> None:
    ART.mkdir(parents=True, exist_ok=True)
    path = ART / name
    if path.resolve().parent != ART.resolve():
        raise ValueError("report must be inside the owned artifact directory")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def owned_containers() -> list[dict[str, Any]]:
    ids = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"]).split()
    if not ids:
        return []
    # Inspect only our own label-selected resources; never inspect env values.
    values = run(["docker", "inspect", "--format", '{{json .Id}} {{json .Name}} {{json .Config.Labels}} {{json .State}} {{json .HostConfig.PortBindings}} {{json .HostConfig.Memory}}', *ids])
    decoder = json.JSONDecoder()
    records = []
    for line in values.splitlines():
        fields = []
        remaining = line
        for _ in range(6):
            value, offset = decoder.raw_decode(remaining.lstrip())
            fields.append(value)
            remaining = remaining.lstrip()[offset:]
        cid, name, labels, state, bindings, memory = fields
        if labels.get("com.docker.compose.project") != PROJECT:
            raise RuntimeError("container project label mismatch")
        for mappings in (bindings or {}).values():
            if any(item.get("HostIp") != "127.0.0.1" for item in mappings or []):
                raise RuntimeError("owned container has a non-loopback published port")
        records.append({"id": cid, "name": name.lstrip("/"), "service": labels.get("com.docker.compose.service"),
                        "running": state.get("Running"), "oom_killed": state.get("OOMKilled"),
                        "health": state.get("Health", {}).get("Status"), "memory_limit_bytes": memory})
    return records


def update_state() -> dict[str, Any]:
    state = {"project": PROJECT, "observed_at_unix": time.time(), "containers": owned_containers(),
             "jobs": [], "pids": [], "note": "No host process is started by this script."}
    write_report("resources.json", state)
    return state


def require_owned(service: str) -> str:
    matches = [item for item in owned_containers() if item["service"] == service and item["running"]]
    if len(matches) != 1:
        raise RuntimeError(f"exactly one running owned {service} container is required")
    return matches[0]["id"]


def preflight(services: list[str]) -> dict[str, Any]:
    bound = []
    for service in services:
        for port in PORTS[service]:
            sock = socket.socket()
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                bound.append(port)
            finally:
                sock.close()
    available = {}
    for service in services:
        result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGES[service]],
                                capture_output=True, text=True, env=SAFE_ENV)
        available[service] = {"image": IMAGES[service], "available": result.returncode == 0,
                              "id": result.stdout.strip() if result.returncode == 0 else None}
    report = {"project": PROJECT, "result": "pass" if not bound and all(v["available"] for v in available.values()) else "blocked",
              "occupied_ports": bound, "images": available, "host_free_bytes": shutil.disk_usage(ROOT).free,
              "docker": json.loads(run(["docker", "info", "--format", '{"memory_bytes":{{.MemTotal}},"cpus":{{.NCPU}}}'])),
              "services": services, "network_required": False,
              "dotenv_contents_read": False, "backend_venv_exists": (ROOT / "backend/.venv/bin/python").exists(),
              "frontend_dependencies_exist": (ROOT / "frontend/node_modules/.bin/playwright").exists()}
    write_report("preflight.json", report)
    return report


def stack_up(services: list[str]) -> dict[str, Any]:
    existing = owned_containers()
    if existing and not STATE.exists():
        raise RuntimeError("project resources already exist without this run's ledger; refuse adoption")
    if STATE.exists():
        ledger = json.loads(STATE.read_text(encoding="utf-8"))
        known = {item["id"] for item in ledger.get("containers", [])}
        if ledger.get("project") != PROJECT or any(item["id"] not in known for item in existing):
            raise RuntimeError("untracked project resources appeared; refuse adoption")
    check = preflight(services)
    # Already-running owned services may hold their own ports, but starting an
    # existing service is never used to recreate/adopt an untracked container.
    if check["result"] != "pass":
        raise RuntimeError("preflight blocked: occupied ports or missing local images; see artifact")
    if shutil.disk_usage(ROOT).free < 20 * 2**30:
        raise RuntimeError("less than 20GiB host disk headroom; refuse even smoke")
    try:
        output = compose("up", "-d", "--pull", "never", "--no-build", "--wait", "--wait-timeout", "180", *services)
    finally:
        # Preserve partial startup ownership for exact cleanup even if health fails.
        state = update_state()
    return {"output": output, **state}


def stack_down(destroy_data: bool) -> dict[str, Any]:
    if not STATE.exists():
        raise RuntimeError("no resource ledger; refusing cleanup")
    ledger = json.loads(STATE.read_text(encoding="utf-8"))
    if ledger.get("project") != PROJECT:
        raise RuntimeError("resource ledger project mismatch")
    current = owned_containers()
    known = {item["id"] for item in ledger["containers"]}
    if any(item["id"] not in known for item in current):
        raise RuntimeError("untracked project container appeared; refusing cleanup")
    args = ["down", "--timeout", "20"]
    if destroy_data:
        args.append("--volumes")
    output = compose(*args)
    return {"output": output, "destroyed_data": destroy_data, **update_state()}


def sql(source: str, statement: str) -> str:
    service = "source-pg" if source == "postgres" else "mysql"
    cid = require_owned(service)
    if source == "postgres":
        return run(["docker", "exec", "-i", cid, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                    "-U", "integrated_e2e", "-d", "integrated_source"], text=statement)
    return run(["docker", "exec", "-i", "-e", "MYSQL_PWD=integrated_e2e_fixture", cid,
                "mysql", "--default-character-set=utf8mb4", "--batch", "--raw", "--skip-column-names", "-u", "integrated_e2e", "integrated_source"], text=statement)


def expressions(source: str, n: str = "n") -> list[str]:
    is_pg = source == "postgres"
    concat = lambda *parts: " || ".join(parts) if is_pg else f"CONCAT({','.join(parts)})"
    cast = lambda value: f"CAST({value} AS {'text' if is_pg else 'CHAR'})"
    pad = lambda value, width: f"LPAD({cast(value)},{width},'0')"
    return [n, f"9007199254740992+{n}", f"MOD({n},100000)",
            f"MOD({n},2)=0", "'2026-01-01T00:00:00'", f"'{source}'",
            f"CASE WHEN MOD({n},11)=0 THEN NULL ELSE {concat(chr(39)+'合成-'+chr(39), pad(n,10))} END",
            *[concat(f"'v{i:02d}-'", pad(f"MOD({n},1000000)",6)) for i in range(1,41)]]


def seed(source: str, rows: int, batch_size: int, extend: bool = False) -> dict[str, Any]:
    if not 1 <= rows <= 10_000_000 or not 1 <= batch_size <= 100_000:
        raise ValueError("rows must be 1..10000000 and batch-size 1..100000")
    # The fixed table is inside a dedicated source DB in a label-verified owned
    # container. Refuse a second seed rather than silently dropping prior work.
    cols = ["id BIGINT PRIMARY KEY", "join_key BIGINT NOT NULL", "amount_cents BIGINT NOT NULL",
            "active BOOLEAN NOT NULL", "observed_at VARCHAR(24) NOT NULL", "source_tag VARCHAR(16) NOT NULL", "note VARCHAR(64)"]
    cols += [f"c{i:02d} VARCHAR(32) NOT NULL" for i in range(1,41)]
    initial = 0
    if extend:
        initial = int(sql(source, "SELECT COUNT(*) FROM scale_rows;").strip())
        if initial >= rows:
            raise ValueError("extend requires a larger row target")
        oracle(source, initial)
    else:
        sql(source, f"CREATE TABLE scale_rows ({','.join(cols)});")
    started = time.monotonic()
    for offset in range(initial, rows, batch_size):
        stop = min(offset + batch_size, rows)
        if source == "postgres":
            seq = f"generate_series({offset+1},{stop}) AS seq(n)"
        else:
            digits = "(" + " UNION ALL ".join(f"SELECT {i} AS d" for i in range(10)) + ")"
            value = "+".join(f"d{i}.d*{10**i}" for i in range(5))
            seq = f"(SELECT {offset+1}+{value} AS n FROM " + " CROSS JOIN ".join(f"{digits} d{i}" for i in range(5)) + f") seq WHERE n<={stop}"
        sql(source, f"INSERT INTO scale_rows ({','.join(COLUMNS)}) SELECT {','.join(expressions(source))} FROM {seq};")
    report = {"source": source, "rows": rows, "columns": len(COLUMNS), "batch_size": batch_size,
              "wall_seconds": round(time.monotonic()-started, 3), "result": "seeded_not_accepted",
              "is_final_scale": rows in FINAL_SCALES}
    write_report(f"seed-{source}-{rows}.json", report)
    return report


def oracle(source: str, rows: int) -> dict[str, Any]:
    started = time.monotonic()
    separator = " || '|' || " if source == "postgres" else None
    fields = ["COUNT(*)", "COUNT(DISTINCT id)", "MIN(id)", "MAX(id)", "SUM(id)", "SUM(amount_cents)", "COUNT(note)"]
    casts = [f"CAST({field} AS {'text' if source == 'postgres' else 'CHAR'})" for field in fields]
    combined = separator.join(casts) if separator else f"CONCAT_WS('|',{','.join(casts)})"
    observed = [int(value) for value in sql(source, f"SELECT {combined} FROM scale_rows;").strip().split("|")]
    cycles, remainder = divmod(rows, 100000)
    expected = [rows, rows, 1, rows, rows*(rows+1)//2,
                cycles*99999*100000//2 + remainder*(remainder+1)//2, rows-rows//11]
    schema = sql(source, "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='scale_rows' AND table_schema=" + ("'public'" if source == "postgres" else "DATABASE()") + ";").strip()
    if observed != expected or int(schema) != 47:
        raise RuntimeError(f"source oracle mismatch: {observed} expected {expected}, columns={schema}")
    expected_expressions = expressions(source, "id")
    comparisons = [f"{column} IS DISTINCT FROM ({expr})" if source == "postgres" else f"NOT ({column} <=> ({expr}))"
                   for column, expr in zip(COLUMNS, expected_expressions)]
    mismatch_count = int(sql(source, f"SELECT COUNT(*) FROM scale_rows WHERE {' OR '.join(comparisons)};").strip())
    if mismatch_count:
        raise RuntimeError(f"full 47-column fixture oracle found {mismatch_count} corrupt rows")
    # Full-table, ordered, bounded streaming digest. SQL renders canonical
    # text, boolean 0/1 and explicit NULL markers identically in both dialects.
    fields_sql = []
    for column in COLUMNS:
        expr = f"CASE WHEN {column} THEN '1' ELSE '0' END" if column == "active" else f"CAST({column} AS {'text' if source == 'postgres' else 'CHAR'})"
        fields_sql.append(f"COALESCE({expr},'<NULL>')")
    canonical = " || '|' || ".join(fields_sql) if source == "postgres" else f"CONCAT_WS('|',{','.join(fields_sql)})"
    statement = f"SELECT {canonical} FROM scale_rows ORDER BY id;"
    cid = require_owned("source-pg" if source == "postgres" else "mysql")
    if source == "postgres":
        command = ["docker", "exec", "-i", cid, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", "integrated_e2e", "-d", "integrated_source"]
    else:
        command = ["docker", "exec", "-i", "-e", "MYSQL_PWD=integrated_e2e_fixture", cid, "mysql", "--default-character-set=utf8mb4", "--batch", "--raw", "--skip-column-names", "--quick", "-u", "integrated_e2e", "integrated_source"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=SAFE_ENV)
    assert process.stdin and process.stdout and process.stderr
    process.stdin.write(statement.encode()); process.stdin.close()
    digest = hashlib.sha256()
    lines = 0
    try:
        for block in iter(lambda: process.stdout.read(65536), b""):
            digest.update(block); lines += block.count(b"\n")
        error = process.stderr.read(4096)
        code = process.wait(timeout=30)
        if code or lines != rows:
            raise RuntimeError(f"oracle streaming failed: exit={code}, lines={lines}, error={error.decode(errors='replace')}")
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=10)
    report = {"source": source, "rows": rows, "columns": 47, "oracle_fields": fields,
              "observed": observed, "expected": expected, "full_47_column_mismatches": mismatch_count,
              "sha256_canonical_ordered": digest.hexdigest(),
              "canonical_columns": COLUMNS, "wall_seconds": round(time.monotonic()-started,3),
              "result": "source_oracle_pass_not_full_e2e", "full_e2e_pass": False}
    write_report(f"oracle-{source}-{rows}.json", report)
    return report


def capacity() -> dict[str, Any]:
    cid = require_owned("db")
    storage = run(["docker", "exec", cid, "df", "-Pk", "/var/lib/postgresql/data"])
    containers = owned_containers()
    stats = run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}", *[c["id"] for c in containers]])
    report = {"vm_filesystem_df_kib": storage, "own_container_stats": stats,
              "host_free_bytes": shutil.disk_usage(ROOT).free, "containers": containers,
              "note": "smoke capacity only; 47-property mapping amplification must be measured separately"}
    write_report("capacity.json", report)
    return report


# Snapshot allowlist is the single source of truth for source-only copies. It is
# deliberately explicit: tailwind.config.ts and every frontend gate config must
# be present so a snapshot can build the real UI and run classification/lint
# gates. Never add a fallback that copies omitted files from live or a draft.
PUBLIC_ENV_TEMPLATES = frozenset({".env.example", "config/defaults.env.example",
                                  "deploy/production.dependencies.example.env"})
SNAPSHOT_EXCLUDED_PARTS = frozenset({"secrets", "credentials", "node_modules",
                                     "__pycache__", ".artifacts", "generated"})
SNAPSHOT_UNTRACKED_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".css", ".html"})
# Snapshots live under the source worktree, so any git invocation inside a
# snapshot discovers the live repository's .git by walking upward. Git-index
# based gates therefore describe the live index/file list, not an independent
# checkout; this must be reported, never claimed as full isolation.
GIT_INDEX_ISOLATION = {
    "independent_checkout": False,
    "git_discovery": "live repository .git is discovered upward from .artifacts/<snapshot>",
    "git_index_source": "live repository index and untracked list",
    "implication": "git-index/search hygiene gates executed inside a snapshot are not evidence of a snapshot-local checkout; compare the live index/file list with snapshot contents explicitly",
}


def snapshot_allowlist(tests: bool) -> tuple[tuple[str, ...], set[str]]:
    allowed = ("backend/app/", "backend/alembic/", "frontend/src/", "frontend/public/")
    explicit = {"AGENTS.md", "backend/alembic.ini", "backend/pyproject.toml", "backend/uv.lock",
                "backend/pytest.ini", "frontend/index.html", "frontend/vite.config.ts",
                "frontend/package.json", "frontend/package-lock.json", "frontend/tsconfig.json",
                "frontend/tsconfig.app.json", "frontend/tsconfig.node.json",
                "frontend/tailwind.config.ts", "frontend/postcss.config.js",
                "frontend/playwright.config.ts", "frontend/playwright.stack.config.ts"}
    if tests:
        # ``--tests`` snapshots carry the tracked, no-secret repository contracts
        # exercised by backend/config/deployment/architecture and frontend gates.
        allowed += ("backend/tests/", "backend/scripts/", "scripts/", "config/app/", "config/tests/",
                    "config/static/", "test_data/", ".github/workflows/", "docker/", "deploy/",
                    "docs/", "agentloop/", "frontend/scripts/")
        explicit |= {"README.md", "DESIGN.md", "backend/.test_durations",
                     "config/pyproject.toml", "config/uv.lock", "config/pytest.ini",
                     "config/start.bat", "config/start.sh", *PUBLIC_ENV_TEMPLATES,
                     "docker-compose.e2e.yml", "docker-compose.local.yml", "docker-compose.prod.yml",
                     "docker-compose.integrated-e2e.yml", "backend/Dockerfile", "frontend/Dockerfile",
                     "frontend/Dockerfile.prod", "agentloop/Dockerfile", ".dockerignore",
                     "backend/.dockerignore", "frontend/.dockerignore",
                     # classification reads all three suite configs; mocked/unit/
                     # check gates read scripts/. tailwind.config.ts is explicit
                     # above for every snapshot, including app-only.
                     "frontend/eslint.config.js", "frontend/playwright.mocked.config.ts",
                     "frontend/playwright.external.config.ts",
                     # Hygiene gates require these directory guides even when the
                     # copied directory itself is allowlisted (root-level READMEs
                     # are not under frontend/src, backend/app, ...).
                     "backend/README.md", "config/README.md", "frontend/README.md"}
    return allowed, explicit


def snapshot_included(relative: str, *, tracked: set[str], tests: bool) -> bool:
    """Return whether a repo-relative path belongs in a source snapshot.

    Pure name/suffix policy only; the caller still rejects symlinks and checks
    bytes. This is shared by runtime_copy and the frozen-runner freeze generator
    so the two cannot disagree about the frontend input set.
    """
    allowed, explicit = snapshot_allowlist(tests)
    if not relative or (relative not in explicit and not relative.startswith(allowed)):
        return False
    path = Path(relative)
    is_env_file = ".env" in path.name or any(part.startswith(".env") for part in path.parts)
    if (is_env_file and relative not in PUBLIC_ENV_TEMPLATES):
        return False
    if any(part in SNAPSHOT_EXCLUDED_PARTS for part in path.parts):
        return False
    # Include new Python/JS/TS sources, but never ingest arbitrary untracked
    # fixture data just because it lives in an allowlisted directory.
    if relative not in tracked and path.suffix not in SNAPSHOT_UNTRACKED_SUFFIXES:
        return False
    return True


def runtime_copy(name: str = "runtime", tests: bool = False) -> dict[str, Any]:
    """Copy current tracked/changed sources into a new, immutable test snapshot.

    Always choose a unique name. A snapshot is never refreshed in place while
    another teammate may be testing it. Source edits during copying invalidate
    the snapshot rather than silently yielding a mixed revision.
    """
    if not name or not all(char.isalnum() or char in "-_" for char in name):
        raise ValueError("snapshot name must contain only letters, digits, dash and underscore")
    destination = ART / name
    if destination.resolve().parent != ART.resolve() or destination.exists():
        raise RuntimeError("snapshot exists or is outside owned artifacts; choose a fresh unique name")
    tracked = set(run(["git", "ls-files", "--cached", "-z"]).split("\0"))
    untracked = set(run(["git", "ls-files", "--others", "--exclude-standard", "-z"]).split("\0"))
    destination.mkdir(parents=True)
    manifest: dict[str, str] = {}
    untracked_sources = []
    for relative in sorted(tracked | untracked):
        if not snapshot_included(relative, tracked=tracked, tests=tests):
            continue
        path = Path(relative)
        source = ROOT / path
        if source.is_symlink() or not source.is_file():
            continue
        resolved = source.resolve()
        if not resolved.is_relative_to(ROOT):
            raise RuntimeError("source symlink ancestor escaped the working tree")
        data = source.read_bytes()
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest[relative] = hashlib.sha256(data).hexdigest()
        if relative not in tracked:
            untracked_sources.append(relative)
    changed = [relative for relative, digest in manifest.items()
               if not (ROOT / relative).is_file() or hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != digest]
    report = {"result": "invalid_concurrent_source_change" if changed else "source_snapshot_only",
              "snapshot": name, "head": run(["git", "rev-parse", "HEAD"]).strip(), "files": manifest,
              "untracked_sources": untracked_sources, "changed_during_copy": changed,
              "dotenv_copied": False,
              "public_env_templates_copied": sorted(set(manifest) & PUBLIC_ENV_TEMPLATES),
              "tests_included": tests,
              "git_index_isolation": dict(GIT_INDEX_ISOLATION),
              "note": "Use this immutable snapshot with the shared venv; do not run uv sync here or refresh it in place."}
    write_report(f"{name}-source-manifest.json", report)
    if changed:
        raise RuntimeError(f"{len(changed)} source files changed during copy; discard this snapshot and retry with a new name")
    return {"file_count": len(manifest), "snapshot": str(destination.relative_to(ROOT)),
            "dotenv_copied": False, "tests_included": tests, "result": report["result"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "up"):
        entry = sub.add_parser(name)
        entry.add_argument("services", nargs="+", choices=sorted(PORTS))
    down = sub.add_parser("down"); down.add_argument("--destroy-data", action="store_true")
    for name in ("status", "capacity"):
        sub.add_parser(name)
    snapshot = sub.add_parser("runtime-copy")
    snapshot.add_argument("--name", default="runtime")
    snapshot.add_argument("--tests", action="store_true")
    for name in ("seed", "oracle"):
        entry = sub.add_parser(name)
        entry.add_argument("--source", choices=["postgres", "mysql"], required=True)
        entry.add_argument("--rows", type=int, required=True)
        if name == "seed":
            entry.add_argument("--batch-size", type=int, default=1000)
            entry.add_argument("--extend", action="store_true", help="verify then append to an existing smaller deterministic source")
    args = parser.parse_args()
    try:
        if args.command == "preflight": result = preflight(args.services)
        elif args.command == "up": result = stack_up(args.services)
        elif args.command == "down": result = stack_down(args.destroy_data)
        elif args.command == "status": result = update_state()
        elif args.command == "capacity": result = capacity()
        elif args.command == "runtime-copy": result = runtime_copy(args.name, args.tests)
        elif args.command == "seed": result = seed(args.source, args.rows, args.batch_size, args.extend)
        else: result = oracle(args.source, args.rows)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("result") == "blocked" else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"integrated-e2e: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
