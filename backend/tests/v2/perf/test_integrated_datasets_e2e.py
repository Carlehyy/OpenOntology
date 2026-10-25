"""Infrastructure safety contracts, not real-service/full-chain acceptance.

Explicitly included in the scoped verification command; these pure tests do not
start Docker, touch source databases, read dotenv, or simulate E2E success.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def stack(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[4]
    spec = importlib.util.spec_from_file_location("integrated_stack_safety", root / "scripts/e2e/integrated_stack.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "ART", tmp_path / ".artifacts/integrated-e2e")
    monkeypatch.setattr(module, "STATE", module.ART / "resources.json")
    return module


def write_fixture(root, relative, text="fixture\n"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def fake_git(monkeypatch, stack, tracked, untracked=()):
    def run(args, **kwargs):
        assert args[0] == "git", args
        if args[1] == "rev-parse":
            return "fixture-head\n"
        return "\0".join(untracked if "--others" in args else tracked) + "\0"
    monkeypatch.setattr(stack, "run", run)


def test_fixture_preserves_all_47_columns_and_bigint_key(stack):
    assert len(stack.COLUMNS) == len(set(stack.COLUMNS)) == 47
    assert stack.COLUMNS[-1] == "c40"
    for kind in ("postgres", "mysql"):
        expressions = stack.expressions(kind)
        assert len(expressions) == 47
        assert expressions[1] == "9007199254740992+n"
        assert "NULL" in expressions[6]
    assert stack.FINAL_SCALES == (2_300_000, 10_000_000)


def test_snapshot_includes_test_contracts_and_public_templates_without_secrets(stack, monkeypatch):
    allowed = [
        "backend/app/fixture.py", "backend/tests/test_fixture.py", "scripts/e2e/probe.py",
        "config/static/index.html", "deploy/deploy-prod.sh", ".github/workflows/ci.yml",
        "docker/browser/Dockerfile", "docker-compose.prod.yml", ".env.example",
        "config/defaults.env.example", "deploy/production.dependencies.example.env",
        "README.md", "DESIGN.md", "docs/development/testing.md",
        "docs/operations/deployment.md", "agentloop/pyproject.toml", "agentloop/app/main.py",
    ]
    excluded = [
        "backend/.env", "backend/tests/.env.test", "backend/tests/secrets/key.py",
        "backend/tests/.artifacts/nested/leak.py", "backend/tests/generated/runtime.py",
        "backend/tests/__pycache__/cache.py", "backend/tests/credentials/private.py",
        "config/generated/local/.env", "config/defaults.env",
        "deploy/production.dependencies.env", "agentloop/.env", "docs/.env",
        "docs/secrets/private.py", "agentloop/generated/runtime.py",
    ]
    for relative in allowed + excluded:
        write_fixture(stack.ROOT, relative)
    write_fixture(stack.ROOT, "backend/tests/untracked.json", '{"do_not_copy":"untrusted data"}')
    fake_git(monkeypatch, stack, allowed + excluded, ["backend/tests/untracked.json"])
    report = stack.runtime_copy("safe", tests=True)
    destination = stack.ART / "safe"
    assert report["file_count"] == len(allowed)
    for relative in allowed:
        assert (destination / relative).read_text() == "fixture\n"
    for relative in excluded + ["backend/tests/untracked.json"]:
        assert not (destination / relative).exists()
    manifest = json.loads((stack.ART / "safe-source-manifest.json").read_text())
    assert manifest["dotenv_copied"] is False
    assert manifest["public_env_templates_copied"] == [
        ".env.example", "config/defaults.env.example",
        "deploy/production.dependencies.example.env",
    ]
    assert manifest["tests_included"] is True
    assert manifest["changed_during_copy"] == []


def test_snapshot_is_immutable_and_name_cannot_escape(stack, monkeypatch):
    fake_git(monkeypatch, stack, [])
    stack.runtime_copy("unique", tests=True)
    with pytest.raises(RuntimeError, match="snapshot exists"):
        stack.runtime_copy("unique", tests=True)
    for value in ("../escape", "bad/name", "", ".", "a b"):
        with pytest.raises(ValueError):
            stack.runtime_copy(value)


def test_snapshot_includes_frontend_build_and_gate_configs(stack, monkeypatch):
    """Regression: the source snapshot must carry Tailwind and every frontend gate config.

    A snapshot without frontend/tailwind.config.ts silently runs Vite with default
    Tailwind content (no utilities), and without the mocked/external configs the
    classification gate cannot run. Runtime-copy must include them from the real
    tree; no draft/live fallback is allowed later.
    """
    app_only = [
        "frontend/tailwind.config.ts", "frontend/postcss.config.js",
        "frontend/playwright.config.ts", "frontend/playwright.stack.config.ts",
        "frontend/index.html", "frontend/package.json", "frontend/tsconfig.json",
        "frontend/tsconfig.app.json", "frontend/tsconfig.node.json", "frontend/vite.config.ts",
    ]
    gate_only = [
        "frontend/eslint.config.js", "frontend/playwright.mocked.config.ts",
        "frontend/playwright.external.config.ts",
        "frontend/scripts/check-e2e-classification.mjs", "frontend/scripts/run-unit-tests.mjs",
        # Repository hygiene requires these directory guides; the frontend one
        # is not under frontend/src and must be admitted explicitly.
        "frontend/README.md", "backend/README.md", "config/README.md",
        "config/uv.lock",
    ]
    shared = [
        "frontend/src/main.tsx", "frontend/src/test/e2e/integrated_datasets.spec.ts",
        "frontend/src/README.md", "frontend/src/test/e2e/README.md",
        "frontend/public/favicon.svg", "backend/uv.lock", "frontend/package-lock.json",
    ]
    excluded = [
        "frontend/components.json", "frontend/dist/assets/app.js",
        "frontend/node_modules/playwright/package.json", "frontend/.env",
    ]
    tracked = app_only + gate_only + shared + excluded
    for relative in tracked:
        write_fixture(stack.ROOT, relative)
    fake_git(monkeypatch, stack, tracked)

    report = stack.runtime_copy("frontend-gates", tests=True)
    destination = stack.ART / "frontend-gates"
    assert report["file_count"] == len(app_only + gate_only + shared)
    for relative in app_only + gate_only + shared:
        assert (destination / relative).is_file(), relative
    for relative in excluded:
        assert not (destination / relative).exists(), relative
    manifest = json.loads((stack.ART / "frontend-gates-source-manifest.json").read_text())
    fixture_sha = hashlib.sha256(b"fixture\n").hexdigest()
    assert manifest["files"]["frontend/tailwind.config.ts"] == fixture_sha
    assert manifest["files"]["frontend/playwright.mocked.config.ts"] == fixture_sha
    assert "frontend/scripts/run-unit-tests.mjs" in manifest["files"]
    assert manifest["git_index_isolation"]["independent_checkout"] is False
    assert "live repository .git" in manifest["git_index_isolation"]["git_discovery"]
    assert "not evidence of a snapshot-local checkout" in manifest["git_index_isolation"]["implication"]

    fake_git(monkeypatch, stack, tracked)
    app_report = stack.runtime_copy("frontend-app", tests=False)
    app_destination = stack.ART / "frontend-app"
    assert app_report["file_count"] == len(app_only + shared)
    for relative in app_only + shared:
        assert (app_destination / relative).is_file(), relative
    for relative in gate_only + excluded:
        assert not (app_destination / relative).exists(), relative


def test_snapshot_ignores_symlink_sources(stack, monkeypatch, tmp_path):
    write_fixture(stack.ROOT, "backend/app/original.py")
    linked = stack.ROOT / "backend/app/linked.py"
    linked.symlink_to(stack.ROOT / "backend/app/original.py")
    fake_git(monkeypatch, stack, ["backend/app/linked.py"])
    assert stack.runtime_copy("no-links", tests=True)["file_count"] == 0


def test_snapshot_without_tests_omits_tests_and_scripts(stack, monkeypatch):
    tracked = ["backend/app/fixture.py", "backend/tests/test_fixture.py", "scripts/e2e/probe.py"]
    for relative in tracked:
        write_fixture(stack.ROOT, relative)
    fake_git(monkeypatch, stack, tracked)
    assert stack.runtime_copy("app-only", tests=False)["file_count"] == 1


def test_cleanup_refuses_untracked_project_container(stack, monkeypatch):
    stack.write_report("resources.json", {"project": stack.PROJECT, "containers": [{"id": "known"}]})
    monkeypatch.setattr(stack, "owned_containers", lambda: [{"id": "unknown"}])
    monkeypatch.setattr(stack, "compose", lambda *_: pytest.fail("cleanup must fail before compose"))
    with pytest.raises(RuntimeError, match="untracked project container"):
        stack.stack_down(False)


def test_cleanup_refuses_wrong_project_or_missing_ledger(stack, monkeypatch):
    monkeypatch.setattr(stack, "compose", lambda *_: pytest.fail("cleanup must fail before compose"))
    with pytest.raises(RuntimeError, match="no resource ledger"):
        stack.stack_down(True)
    stack.write_report("resources.json", {"project": "unrelated", "containers": []})
    with pytest.raises(RuntimeError, match="project mismatch"):
        stack.stack_down(True)


def test_reports_cannot_escape_owned_artifact_directory(stack):
    with pytest.raises(ValueError, match="inside the owned"):
        stack.write_report("../escape.json", {})


def test_seed_validates_bounds_before_touching_source(stack, monkeypatch):
    monkeypatch.setattr(stack, "sql", lambda *_: pytest.fail("invalid fixture must not touch source"))
    for rows, batch in ((0, 1000), (10_000_001, 1000), (500, 0), (500, 100001)):
        with pytest.raises(ValueError):
            stack.seed("postgres", rows, batch)


def test_extend_verifies_complete_existing_prefix_before_append(stack, monkeypatch):
    calls = []
    monkeypatch.setattr(stack, "sql", lambda source, statement: calls.append(statement) or "500")
    def corrupt(*_):
        raise RuntimeError("full 47-column fixture oracle found corrupt prefix")
    monkeypatch.setattr(stack, "oracle", corrupt)
    with pytest.raises(RuntimeError, match="corrupt prefix"):
        stack.seed("postgres", 10000, 1000, extend=True)
    assert len(calls) == 1
    assert calls[0] == "SELECT COUNT(*) FROM scale_rows;"


@pytest.fixture
def http_contract():
    root = Path(__file__).resolve().parents[4]
    spec = importlib.util.spec_from_file_location("integrated_http_contract", root / "backend/scripts/integrated_datasets_e2e.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_http_runner_polls_202_until_sealed_before_querying_version(http_contract, monkeypatch):
    """Pure runner state-machine contract, NOT evidence of a live queue/sync."""
    runner = object.__new__(http_contract.Acceptance)
    runner.rows = 10000
    runner.evidence = []
    common = {"run_id": "run", "dataset_id": "dataset", "version_id": None,
              "row_count": 0, "expected_rows": 10000, "complete": False}
    replies = iter([{**common, "status": "queued"}, {**common, "status": "loading"},
                    {**common, "status": "sealed", "complete": True, "version_id": "version",
                     "row_count": 10000, "checksum": "checksum"},
                    {"schema_fingerprint": "schema", "columns": [{}] * 47}])
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return next(replies)

    monkeypatch.setattr(runner, "request", request)
    monkeypatch.setattr(http_contract.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "query", lambda query, bindings: {
        "consistency": "immutable_versions", "rows": [{"n": "10000", "pk": "10000"}]})
    runner.sync("postgres", "asset")
    assert calls[0] == ("POST", "/api/v2/integrated-datasets/asset/sync",
                        {"status": 202, "params": {"batch_size": 1000}})
    assert [path for method, path, _ in calls if "sync-runs" in path] == [
        "/api/v2/integrated-datasets/asset/sync-runs/run"] * 2
    assert runner.evidence[-1]["stage"] == "complete_version"


@pytest.mark.parametrize("state", ["queued", "loading", "failed"])
def test_http_runner_never_marks_nonsealed_sync_complete(http_contract, monkeypatch, state):
    runner = object.__new__(http_contract.Acceptance)
    runner.rows = 10000
    runner.evidence = []
    monkeypatch.setattr(runner, "request", lambda *_a, **_k: {
        "status": state, "complete": False, "run_id": "run", "version_id": None})
    clock = iter([0, 901])
    monkeypatch.setattr(http_contract.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(runner, "query", lambda *_: pytest.fail("nonsealed run must not be queried as a version"))
    with pytest.raises(RuntimeError, match="did not finish|did not publish"):
        runner.sync("postgres", "asset")
    assert [item["stage"] for item in runner.evidence] == ["sync_requested"]


def test_http_runner_rejects_native_metadata_masquerading_as_dbx(http_contract, monkeypatch):
    runner = object.__new__(http_contract.Acceptance)
    monkeypatch.setattr(runner, "request", lambda *_: {
        "columns": [{}] * 47, "primary_key": ["id"],
        "backends": {"snapshot": "native", "mutation": "native", "metadata": "dbx"},
        "dbx": {"configured": False, "available": False, "commit": http_contract.DBX_COMMIT}})
    with pytest.raises(RuntimeError, match="genuine dbx"):
        runner.asset("asset")
