"""Fail-closed production dependency configuration regression tests."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

from app.shared.config import (
    Settings,
    production_config_errors,
    required_dependency_config_errors,
)


ROOT = Path(__file__).resolve().parents[4]
MATERIALIZER = ROOT / "scripts" / "ci" / "materialize-production-dependencies.sh"
MATERIALIZED_KEYS = {
    "ENVIRONMENT",
    "PUBLIC_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "DATABASE_URL",
    "REDIS_URL",
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "NEO4J_AUTH",
    "MINIO_ENDPOINT",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "MINIO_USE_SSL",
    "N8N_API_URL",
    "N8N_API_KEY",
    "N8N_TIMEOUT_SECONDS",
}
MATERIALIZER_SOURCE_VALUES = {
    "PROD_PUBLIC_PORT": "8080",
    "PROD_POSTGRES_DB": "ontology",
    "PROD_POSTGRES_USER": "ontology",
    "PROD_POSTGRES_PASSWORD": "synthetic-postgres-password",
    "PROD_DATABASE_URL": (
        "postgresql://ontology:synthetic-postgres-password"
        "@pg.example.com:5432/ontology"
    ),
    "PROD_REDIS_URL": (
        "redis://:synthetic-redis-password@redis.example.com:6379/0"
    ),
    "PROD_NEO4J_URI": "bolt+s://graph.example.com:7687",
    "PROD_NEO4J_USER": "neo4j",
    "PROD_NEO4J_PASSWORD": "synthetic-neo4j-password",
    "PROD_NEO4J_AUTH": "neo4j/synthetic-neo4j-password",
    "PROD_MINIO_ENDPOINT": "objects.example.com:9000",
    "PROD_MINIO_ACCESS_KEY": "synthetic-minio-access",
    "PROD_MINIO_SECRET_KEY": "synthetic-minio-secret",
    "PROD_MINIO_USE_SSL": "true",
    "PROD_N8N_API_URL": "https://n8n.example.com/api/v1",
    "PROD_N8N_API_KEY": "synthetic-n8n-api-key",
}


def _required_settings(**updates) -> Settings:
    values = {
        "environment": "production",
        "database_url": (
            "postgresql://ontology:strong-password@pg.example.com:5432/ontology"
        ),
        "redis_url": "redis://:strong-password@redis.example.com:6379/0",
        "secret_key": "0123456789abcdef0123456789abcdef",
        "cors_allowed_origins": "",
        "first_admin_password": "strong-admin-password",
        "neo4j_uri": "bolt://graph.example.com:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "strong-neo4j-password",
        "minio_endpoint": "objects.example.com:9000",
        "minio_access_key": "ontology-minio",
        "minio_secret_key": "strong-minio-password",
        "steward_browser_cdp_url": "http://browser:9222",
        "n8n_api_url": "https://n8n.example.com/api/v1",
        "n8n_api_key": "strong-n8n-api-key",
        "python_kernel_gateway_auth_token": "strong-kernel-gateway-token",
        "pipeline_file_public_app_base_url": "https://platform.example.com",
        "pipeline_file_public_api_base_url": "https://api.example.com",
    }
    values.update(updates)
    return Settings(**values)


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value
    return values


def _run_materializer(
    target: Path,
    overrides: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(MATERIALIZER_SOURCE_VALUES)
    env["PROD_N8N_TIMEOUT_SECONDS"] = "30"
    for key, value in (overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        ["bash", str(MATERIALIZER), str(target)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_required_runtime_stack_accepts_complete_configuration():
    assert production_config_errors(_required_settings()) == []


def test_runtime_dependency_gate_is_independent_from_application_secrets():
    current = _required_settings(
        secret_key="dev-secret-key",
        first_admin_password="admin123",
    )

    assert required_dependency_config_errors(current) == []
    assert "SECRET_KEY" in production_config_errors(current)
    assert "FIRST_ADMIN_PASSWORD" in production_config_errors(current)


def test_runtime_gate_rejects_invalid_dependency_configuration():
    errors = production_config_errors(_required_settings(
        database_url="sqlite:////tmp/fallback.db",
        redis_url="redis://redis/0",
        neo4j_uri="http://neo4j:7474",
        minio_endpoint="minio:9001",
        n8n_api_key="",
        steward_browser_cdp_url="ws://browser:9222",
    ))

    assert any("authenticated PostgreSQL" in item for item in errors)
    assert any("explicit port" in item for item in errors)
    assert any("reference Neo4j" in item for item in errors)
    assert any("S3 API" in item for item in errors)
    assert any("configure n8n" in item for item in errors)
    assert any("CDP_URL" in item for item in errors)


def test_runtime_gate_rejects_cdp_discovery_path_but_accepts_root_slash():
    errors = required_dependency_config_errors(_required_settings(
        steward_browser_cdp_url="https://browser.example.com/json/version",
    ))

    assert errors == [
        "STEWARD_BROWSER_CDP_URL must be an absolute HTTP(S) origin/root URL"
    ]
    assert required_dependency_config_errors(_required_settings(
        steward_browser_cdp_url="https://browser.example.com/",
    )) == []


def test_runtime_gate_rejects_empty_database_and_redis_passwords():
    errors = required_dependency_config_errors(_required_settings(
        database_url="postgresql://ontology:@pg.example.com:5432/ontology",
        redis_url="redis://:@redis.example.com:6379/0",
    ))

    assert any("authenticated PostgreSQL" in item for item in errors)
    assert any("authenticated Redis" in item for item in errors)


def test_committed_manifest_template_declares_contract_without_secrets():
    template = ROOT / "deploy" / "production.dependencies.example.env"
    manifest = _read_env(template)

    assert set(manifest) == MATERIALIZED_KEYS
    assert manifest["ENVIRONMENT"] == "production"
    assert manifest["N8N_TIMEOUT_SECONDS"] == "30"
    assert not manifest["MINIO_ENDPOINT"].endswith(":9001")
    secret_keys = {
        "POSTGRES_PASSWORD",
        "DATABASE_URL",
        "REDIS_URL",
        "NEO4J_PASSWORD",
        "NEO4J_AUTH",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "N8N_API_KEY",
    }
    assert all(manifest[key] == "" for key in secret_keys)
    assert ".internal" not in template.read_text(encoding="utf-8")


def test_materializer_writes_exact_fail_closed_contract(tmp_path):
    target = tmp_path / "production.dependencies.env"

    result = _run_materializer(target)

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = _read_env(target)
    assert set(manifest) == MATERIALIZED_KEYS
    assert manifest["ENVIRONMENT"] == "production"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    output = result.stdout + result.stderr
    assert "synthetic-" not in output


def test_materializer_accepts_bounded_n8n_timeout_override(tmp_path):
    target = tmp_path / "production.dependencies.env"

    result = _run_materializer(
        target,
        {"PROD_N8N_TIMEOUT_SECONDS": "45"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _read_env(target)["N8N_TIMEOUT_SECONDS"] == "45"


def test_materializer_rejects_each_missing_value_without_overwrite(tmp_path):
    for source_name in MATERIALIZER_SOURCE_VALUES:
        target = tmp_path / f"{source_name}.env"
        target.write_text("sentinel=unchanged\n", encoding="utf-8")

        result = _run_materializer(target, {source_name: None})

        assert result.returncode != 0
        assert target.read_text(encoding="utf-8") == "sentinel=unchanged\n"
        assert "synthetic-" not in result.stdout + result.stderr


def test_materializer_rejects_multiline_values_without_overwrite(tmp_path):
    target = tmp_path / "production.dependencies.env"
    target.write_text("sentinel=unchanged\n", encoding="utf-8")

    result = _run_materializer(
        target,
        {"PROD_POSTGRES_PASSWORD": "first-line\nsecond-line"},
    )

    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == "sentinel=unchanged\n"
    assert "first-line" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("source_name", "value"),
    [
        ("PROD_PUBLIC_PORT", "0"),
        ("PROD_PUBLIC_PORT", "65536"),
        ("PROD_PUBLIC_PORT", "not-a-port"),
        ("PROD_MINIO_USE_SSL", "TRUE"),
        ("PROD_MINIO_USE_SSL", "1"),
        ("PROD_N8N_TIMEOUT_SECONDS", "2"),
        ("PROD_N8N_TIMEOUT_SECONDS", "121"),
        ("PROD_N8N_TIMEOUT_SECONDS", "slow"),
    ],
)
def test_materializer_validates_typed_values(
    tmp_path,
    source_name,
    value,
):
    target = tmp_path / "production.dependencies.env"

    result = _run_materializer(target, {source_name: value})

    assert result.returncode != 0
    assert not target.exists()


def test_deploy_merges_required_runtime_manifest(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    manifest_path = tmp_path / "deploy" / "production.dependencies.env"
    materialized = _run_materializer(manifest_path)
    assert materialized.returncode == 0, materialized.stdout + materialized.stderr
    env = os.environ.copy()
    env.update({
        "APP_DIR": str(tmp_path),
        "SKIP_GIT": "1",
        "BOOTSTRAP_PRODUCTION_ENV": "1",
        "DEPLOY_VALIDATE_ONLY": "1",
        "HEALTH_URL": "https://platform.example.com/",
    })

    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / "deploy-prod.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(tmp_path / ".env")
    manifest = _read_env(manifest_path)
    for key, value in manifest.items():
        assert generated[key] == value
    assert "applied" in result.stdout
    assert manifest["POSTGRES_PASSWORD"] not in result.stdout
    assert manifest["MINIO_SECRET_KEY"] not in result.stdout


def test_dependency_probe_never_logs_driver_error_details(monkeypatch, capsys):
    from app.shared import dependency_probe

    def fail_with_secret():
        raise RuntimeError("driver exposed should-never-appear")

    monkeypatch.setattr(
        dependency_probe,
        "PROBES",
        (("PostgreSQL", fail_with_secret),),
    )

    assert dependency_probe.main() == 1
    captured = capsys.readouterr()
    assert "PostgreSQL: unavailable (RuntimeError)" in captured.err
    assert "should-never-appear" not in captured.err


def test_dependency_preflight_reports_cdp_without_blocking_api_start(
    monkeypatch,
    capsys,
):
    from app.shared import dependency_probe

    monkeypatch.setattr(
        dependency_probe,
        "PROBES",
        (("Chromium CDP", lambda: (_ for _ in ()).throw(
            RuntimeError("browser unavailable"))),),
    )

    assert dependency_probe.main() == 0
    captured = capsys.readouterr()
    assert "advisory; API may start" in captured.err


def test_startup_dependency_probe_fails_closed_except_for_cdp(monkeypatch):
    from app.shared import dependency_probe

    def reject_redis():
        raise RuntimeError("redis secret must not be rendered")

    def reject_browser():
        raise RuntimeError("browser detail must not be rendered")

    monkeypatch.setattr(
        dependency_probe,
        "PROBES",
        (
            ("Redis", reject_redis),
            ("Chromium CDP", reject_browser),
        ),
    )

    with pytest.raises(RuntimeError) as exc_info:
        dependency_probe.probe_startup_dependencies()

    assert str(exc_info.value) == (
        "Required startup dependencies unavailable: Redis"
    )
    assert "secret" not in str(exc_info.value)


def test_startup_dependency_probe_treats_cdp_as_advisory(monkeypatch):
    from app.shared import dependency_probe

    def reject_browser():
        raise RuntimeError("unavailable")

    monkeypatch.setattr(
        dependency_probe,
        "PROBES",
        (("Chromium CDP", reject_browser),),
    )

    dependency_probe.probe_startup_dependencies()


def test_startup_dependency_probe_treats_n8n_as_advisory_outside_production(monkeypatch):
    from app.shared import dependency_probe

    def reject_n8n():
        raise RuntimeError("unavailable")

    monkeypatch.setattr(dependency_probe, "PROBES", (("n8n", reject_n8n),))
    monkeypatch.setattr(dependency_probe.settings, "environment", "development")

    dependency_probe.probe_startup_dependencies()


def test_startup_dependency_probe_fails_closed_for_n8n_in_production(monkeypatch):
    from app.shared import dependency_probe

    def reject_n8n():
        raise RuntimeError("unavailable")

    monkeypatch.setattr(dependency_probe, "PROBES", (("n8n", reject_n8n),))
    monkeypatch.setattr(dependency_probe.settings, "environment", "production")

    with pytest.raises(RuntimeError) as exc_info:
        dependency_probe.probe_startup_dependencies()
    assert "n8n" in str(exc_info.value)


def test_environment_minio_is_always_authoritative(monkeypatch):
    from app.shared import storage

    sentinel = object()
    monkeypatch.setattr(
        storage,
        "get_environment_storage_service",
        lambda: sentinel,
    )

    assert storage.get_storage_service() is sentinel
