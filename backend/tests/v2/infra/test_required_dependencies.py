"""Fail-closed production dependency configuration regression tests."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest
import yaml

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
    "WEKNORA_WEB_URL",
    "WEKNORA_ADMIN_USER",
    "WEKNORA_ADMIN_EMAIL",
    "WEKNORA_APP_IMAGE",
    "WEKNORA_UI_IMAGE",
    "WEKNORA_DOCREADER_IMAGE",
    "WEKNORA_POSTGRES_IMAGE",
    "WEKNORA_REDIS_IMAGE",
    "KANEO_WEB_URL",
    "KANEO_ADMIN_USER",
    "KANEO_ADMIN_EMAIL",
    "KANEO_APP_IMAGE",
    "KANEO_POSTGRES_IMAGE",
    "MULTICA_DEFAULT_BASE_URL",
    "MULTICA_DEFAULT_WEB_URL",
    "MULTICA_DEFAULT_WORKSPACE_ID",
    "MULTICA_DEFAULT_WORKSPACE_NAME",
    "MULTICA_DEFAULT_PAT",
    "MULTICA_BACKEND_IMAGE",
    "MULTICA_WEB_IMAGE",
    "MULTICA_POSTGRES_IMAGE",
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

MATERIALIZER_OPTIONAL_VALUES = {
    "PROD_WEKNORA_WEB_URL": "https://weknora.example.com",
    "PROD_WEKNORA_ADMIN_USER": "admin",
    "PROD_WEKNORA_ADMIN_EMAIL": "admin@weknora.local",
    "PROD_WEKNORA_APP_IMAGE": f"example.invalid/weknora-app@sha256:{'a' * 64}",
    "PROD_WEKNORA_UI_IMAGE": f"example.invalid/weknora-ui@sha256:{'b' * 64}",
    "PROD_WEKNORA_DOCREADER_IMAGE": (
        f"example.invalid/weknora-docreader@sha256:{'c' * 64}"
    ),
    "PROD_WEKNORA_POSTGRES_IMAGE": (
        f"example.invalid/weknora-postgres@sha256:{'d' * 64}"
    ),
    "PROD_WEKNORA_REDIS_IMAGE": f"example.invalid/weknora-redis@sha256:{'e' * 64}",
    # Kaneo's materialized surface carries only the optional public origin,
    # admin identity, and immutable image pins; credentials never pass
    # through the dependency manifest (they live in the server's .env).
    "PROD_KANEO_WEB_URL": "https://pm.example.com",
    "PROD_KANEO_ADMIN_USER": "admin",
    "PROD_KANEO_ADMIN_EMAIL": "admin@kaneo.local",
    "PROD_KANEO_APP_IMAGE": f"example.invalid/kaneo-app@sha256:{'1' * 64}",
    "PROD_KANEO_POSTGRES_IMAGE": (
        f"example.invalid/kaneo-postgres@sha256:{'2' * 64}"
    ),
    # The Multica deployment-level default is the full five-value set: the
    # manifest must be able to carry all five (plus digest image overrides)
    # so a controlled manifest can provision a complete default. The PAT is
    # the only secret of the five; the rest is deployment configuration.
    "PROD_MULTICA_DEFAULT_BASE_URL": "http://multica:8080",
    "PROD_MULTICA_DEFAULT_WEB_URL": "https://multica.example.com",
    "PROD_MULTICA_DEFAULT_WORKSPACE_ID": "00000000-0000-0000-0000-000000000001",
    "PROD_MULTICA_DEFAULT_WORKSPACE_NAME": "OpenOntology default workspace",
    "PROD_MULTICA_DEFAULT_PAT": "synthetic-multica-default-pat",
    "PROD_MULTICA_BACKEND_IMAGE": (
        f"example.invalid/multica-backend@sha256:{'3' * 64}"
    ),
    "PROD_MULTICA_WEB_IMAGE": f"example.invalid/multica-web@sha256:{'4' * 64}",
    "PROD_MULTICA_POSTGRES_IMAGE": (
        f"example.invalid/multica-postgres@sha256:{'5' * 64}"
    ),
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
        "steward_browser_allow_private_networks": False,
        "integrated_dbx_url": "http://dbx:4224",
        "integrated_dbx_password": "strong-dbx-password",
        "integrated_dbx_commit": "0137e0cbc8b663da5a17ec424c6d47e4e5570ead",
        "n8n_api_url": "https://n8n.example.com/api/v1",
        "n8n_api_key": "strong-n8n-api-key",
        "weknora_web_url": "https://kb.example.com",
        "kaneo_web_url": "https://pm.example.com",
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
    env.update(MATERIALIZER_OPTIONAL_VALUES)
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
        "MULTICA_DEFAULT_PAT",
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


def _compose_services(path: Path) -> dict[str, dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    def resolved(node):
        if isinstance(node, list):
            return [resolved(item) for item in node]
        if not isinstance(node, dict):
            return node
        merged: dict = {}
        for key, value in node.items():
            if key == "<<":
                inherited = resolved(value)
                if isinstance(inherited, list):
                    for item in inherited:
                        merged.update(item)
                else:
                    merged.update(inherited)
            else:
                merged[key] = resolved(value)
        return merged

    return {
        name: resolved(block)
        for name, block in raw.get("services", {}).items()
    }


def _assert_weknora_compose_graph():
    local = _compose_services(ROOT / "docker-compose.local.yml")
    prod = _compose_services(ROOT / "docker-compose.prod.yml")

    init = local["weknora-app-init"]
    bootstrap = local["weknora-bootstrap"]
    app = local["weknora-app"]
    verify = local["weknora-verify"]
    gateway = local["weknora-gateway"]

    for services in (local, prod):
        for name in ("weknora-app-init", "weknora-app"):
            assert services[name]["environment"]["RETRIEVE_DRIVER"] == "postgres"
    assert init["environment"]["DISABLE_REGISTRATION"] == "false"
    assert "ports" not in init
    assert bootstrap["depends_on"]["weknora-app-init"]["condition"] == (
        "service_healthy"
    )
    assert bootstrap["environment"]["WEKNORA_BOOTSTRAP_BASE_URL"] == (
        "http://weknora-app-init:8080"
    )
    assert app["environment"]["DISABLE_REGISTRATION"] == "true"
    assert app["depends_on"]["weknora-bootstrap"]["condition"] == (
        "service_completed_successfully"
    )
    assert verify["depends_on"]["weknora-app"]["condition"] == (
        "service_healthy"
    )
    assert verify["depends_on"]["weknora-bootstrap"]["condition"] == (
        "service_completed_successfully"
    )
    assert "--verify" in verify["command"]
    assert gateway["depends_on"]["weknora-verify"]["condition"] == (
        "service_completed_successfully"
    )
    assert "backend" not in gateway["depends_on"]
    assert gateway["depends_on"]["weknora-app"]["condition"] == (
        "service_healthy"
    )

    assert prod["weknora-app"]["environment"]["DISABLE_REGISTRATION"] == (
        "true"
    )
    assert prod["weknora-app-init"]["environment"][
        "DISABLE_REGISTRATION"
    ] == "false"
    assert "backend" not in prod["weknora-gateway"]["depends_on"]
    assert prod["weknora-gateway"]["depends_on"]["weknora-app"][
        "condition"
    ] == "service_healthy"

    # The dependency graph must be acyclic; a gateway->backend edge is safe
    # only because backend never depends on the gateway. depends_on may use
    # either the mapping or the plain list form (multica-web follows the
    # upstream list form), so normalize lists to dependency-name sets first.
    def _dependency_names(block: dict) -> set[str]:
        depends_on = block.get("depends_on") or {}
        if isinstance(depends_on, list):
            return {str(item) for item in depends_on}
        return set(depends_on.keys())

    graph = {
        name: _dependency_names(block)
        for name, block in {**local, **prod}.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise AssertionError(f"WeKnora compose dependency cycle at {node}")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph.get(node, set()):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for service in graph:
        visit(service)


def _assert_multica_compose_graph():
    local = _compose_services(ROOT / "docker-compose.local.yml")
    prod = _compose_services(ROOT / "docker-compose.prod.yml")

    for services in (local, prod):
        for name in ("multica-postgres", "multica", "multica-web"):
            assert services[name]["profiles"] == ["multica"]
        # The official healthchecks keep the upstream contract: the API
        # container probes /healthz (ready) and the web container its own
        # origin; uploads persist on the upstream /app/data/uploads volume.
        assert "multica_uploads:/app/data/uploads" in services[
            "multica"
        ]["volumes"]
        assert "multica_pgdata:/var/lib/postgresql/data" in services[
            "multica-postgres"
        ]["volumes"]

    # Production exposure contract: the API container never publishes a host
    # port; the web UI (native login/PAT minting) binds loopback by default
    # and must be reached through an SSH tunnel or a TLS reverse proxy.
    assert "ports" not in prod["multica"]
    assert "ports" not in prod["multica-postgres"]
    assert prod["multica-web"]["ports"] == [
        "${MULTICA_WEB_BIND_ADDR:-127.0.0.1}:${MULTICA_WEB_PORT:-18100}:3000"
    ]
    assert prod["multica-web"]["networks"] == ["multica_internal"]
    # The official API container stays solely on the private network: only
    # the calling backend / pipeline_executor join multica_internal, so the
    # API never becomes reachable from the default compose network.
    assert prod["multica"]["networks"] == ["multica_internal"]
    assert prod["multica-postgres"]["networks"] == ["multica_internal"]

    # Local exposure stays loopback-only for both the web and API ports.
    assert local["multica-web"]["ports"] == [
        "127.0.0.1:${MULTICA_WEB_LOOPBACK_PORT:-18100}:3000"
    ]
    assert local["multica"]["ports"] == [
        "127.0.0.1:${MULTICA_API_LOOPBACK_PORT:-18101}:8080"
    ]

    # Secret masking: every env_file consumer keeps the empty masks for the
    # Multica PAT and the stack's own JWT/DB secrets; only backend and
    # pipeline_executor re-declare the real PAT for the deployment default
    # and the AutoLoop worker.
    masked = {"MULTICA_DEFAULT_PAT", "MULTICA_JWT_SECRET",
              "MULTICA_POSTGRES_PASSWORD"}
    for compose_services_map in (local, prod):
        for name, block in compose_services_map.items():
            environment = block.get("environment") or {}
            if not isinstance(environment, dict):
                continue
            if name in {"backend", "pipeline_executor"}:
                assert environment["MULTICA_DEFAULT_PAT"] == (
                    "${MULTICA_DEFAULT_PAT:-}"
                )
                # The stack's own JWT/DB secrets stay masked even for the
                # two sanctioned PAT consumers. pipeline_executor maps all
                # five MULTICA_DEFAULT_* values: executor-side consumers
                # (autoloop dispatch, kernel activation) run the same
                # all-or-nothing completeness predicate, and a consumer
                # missing WEB_URL would treat the deployment default as
                # unconfigured and soft-delete the materialized
                # platform-default profile.
                assert environment["MULTICA_JWT_SECRET"] == ""
                assert environment["MULTICA_POSTGRES_PASSWORD"] == ""
                if name == "pipeline_executor":
                    assert environment["MULTICA_DEFAULT_WEB_URL"] == (
                        "${MULTICA_DEFAULT_WEB_URL:-}"
                    )
            else:
                # Any other service that carries these keys must keep the
                # empty mask (only the multica stack containers consume the
                # real stack secrets, and they use plain interpolated keys
                # like JWT_SECRET/POSTGRES_PASSWORD instead).
                for key in masked & set(environment):
                    assert environment[key] == "", (name, key)

    # The local stack starts multica by default through the repo .env
    # template; the production toggle defaults to enabled as well.
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "COMPOSE_PROFILES=multica" in env_example
    assert "MULTICA_ENABLED=true" in env_example


def test_deploy_merges_required_runtime_manifest(tmp_path):
    # Fold the WeKnora compose graph ordering assertions into this existing
    # test so the pytest-split duration table keeps the same test IDs.
    _assert_weknora_compose_graph()
    _assert_multica_compose_graph()
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    image_keys = (
        "POSTGRES_IMAGE", "REDIS_IMAGE", "NEO4J_IMAGE", "MINIO_IMAGE",
        "BROWSER_IMAGE", "PYTHON_BASE_IMAGE", "NODE_BASE_IMAGE", "NGINX_BASE_IMAGE",
        "WEKNORA_APP_IMAGE", "WEKNORA_UI_IMAGE", "WEKNORA_DOCREADER_IMAGE",
    )
    example = (tmp_path / ".env.example").read_text(encoding="utf-8").splitlines()
    replacements = {key: f"test/{key.lower()}@sha256:{'a' * 64}" for key in image_keys}
    public_tls_ip = "203.0.113.10"
    replacements["PUBLIC_TLS_IP"] = public_tls_ip
    (tmp_path / ".env.example").write_text(
        "\n".join(
            f"{line.split('=', 1)[0]}={replacements[line.split('=', 1)[0]]}"
            if "=" in line and line.split("=", 1)[0] in replacements else line
            for line in example
        ) + "\n",
        encoding="utf-8",
    )
    manifest_path = tmp_path / "deploy" / "production.dependencies.env"
    materialized = _run_materializer(
        manifest_path,
        {"PROD_KANEO_WEB_URL": f"https://{public_tls_ip}:8445"},
    )
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
        # Fresh WeKnora bootstrap adds several durable secret writes; keep
        # headroom for slow macOS fsync while retaining a bounded check.
        timeout=40,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(tmp_path / ".env")
    manifest = _read_env(manifest_path)
    for key, value in manifest.items():
        assert generated[key] == value
    assert "applied" in result.stdout
    assert manifest["POSTGRES_PASSWORD"] not in result.stdout
    assert manifest["MINIO_SECRET_KEY"] not in result.stdout
    assert manifest["MULTICA_DEFAULT_PAT"] not in result.stdout


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


def _multica_settings(monkeypatch, **overrides):
    """Point the five deployment-default Multica settings at synthetic values."""
    from app.shared.config import settings as shared_settings

    values = {
        "multica_default_base_url": "",
        "multica_default_web_url": "",
        "multica_default_workspace_id": "",
        "multica_default_workspace_name": "",
        "multica_default_pat": "",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(shared_settings, key, value)
    return values


_MULTICA_COMPLETE = {
    "multica_default_base_url": "http://multica:8080",
    "multica_default_web_url": "https://multica.example.com",
    "multica_default_workspace_id": "ws-default",
    "multica_default_workspace_name": "平台默认工作区",
    "multica_default_pat": "synthetic-multica-default-pat",
}


def test_multica_default_config_state_classifies_five_value_set(monkeypatch):
    from app.shared import dependency_probe

    _multica_settings(monkeypatch)
    assert dependency_probe.multica_default_config_state() == "disabled"

    _multica_settings(monkeypatch, **_MULTICA_COMPLETE)
    assert dependency_probe.multica_default_config_state() == "complete"

    # Any partial set is "partial": a deployment that intended a default but
    # cannot deliver one must never read as unconfigured.
    _multica_settings(
        monkeypatch,
        multica_default_base_url=_MULTICA_COMPLETE["multica_default_base_url"],
    )
    assert dependency_probe.multica_default_config_state() == "partial"

    # Whitespace-only values count as unset, matching Settings consumers.
    _multica_settings(
        monkeypatch,
        multica_default_base_url="   ",
        multica_default_pat="\t",
    )
    assert dependency_probe.multica_default_config_state() == "disabled"


def test_probe_multica_default_skips_incomplete_configuration(monkeypatch):
    from app.super_assistant import multica_client
    from app.shared import dependency_probe

    def explode(*_args, **_kwargs):
        raise AssertionError("an incomplete default must not be probed")

    monkeypatch.setattr(multica_client, "fetch_me", explode)
    monkeypatch.setattr(multica_client, "list_workspaces", explode)

    _multica_settings(monkeypatch)
    dependency_probe.probe_multica_default()

    _multica_settings(monkeypatch, multica_default_pat="synthetic-pat")
    dependency_probe.probe_multica_default()


def test_probe_multica_default_verifies_workspace_membership(monkeypatch):
    from app.super_assistant import multica_client
    from app.shared import dependency_probe

    calls = []

    def fake_fetch_me(base_url, token):
        calls.append(("me", base_url, token))

    def fake_list_workspaces(base_url, token):
        calls.append(("workspaces", base_url, token))
        return [{"id": "other-workspace"}, {"id": "ws-default"}]

    monkeypatch.setattr(multica_client, "fetch_me", fake_fetch_me)
    monkeypatch.setattr(multica_client, "list_workspaces", fake_list_workspaces)
    _multica_settings(monkeypatch, **_MULTICA_COMPLETE)

    dependency_probe.probe_multica_default()

    assert calls == [
        ("me", "http://multica:8080", "synthetic-multica-default-pat"),
        ("workspaces", "http://multica:8080", "synthetic-multica-default-pat"),
    ]

    _multica_settings(
        monkeypatch,
        **{**_MULTICA_COMPLETE, "multica_default_workspace_id": "missing"},
    )
    with pytest.raises(RuntimeError) as exc_info:
        dependency_probe.probe_multica_default()
    assert str(exc_info.value) == (
        "Multica default workspace is not visible to the default PAT"
    )


def test_probe_multica_default_failure_never_leaks_details(monkeypatch):
    from app.super_assistant import multica_client
    from app.shared import dependency_probe

    def leaky_fetch_me(*_args, **_kwargs):
        raise RuntimeError(
            "GET http://multica:8080/api/me failed: PAT "
            "synthetic-multica-default-pat rejected (upstream body)"
        )

    monkeypatch.setattr(multica_client, "fetch_me", leaky_fetch_me)
    _multica_settings(monkeypatch, **_MULTICA_COMPLETE)

    with pytest.raises(RuntimeError) as exc_info:
        dependency_probe.probe_multica_default()

    assert str(exc_info.value) == "Multica default service is unavailable"
    assert exc_info.value.__cause__ is None


def test_startup_dependency_probe_treats_multica_default_as_advisory(
    monkeypatch, capsys,
):
    from app.shared import dependency_probe

    def reject_multica():
        raise RuntimeError("multica detail must not be rendered")

    monkeypatch.setattr(
        dependency_probe,
        "PROBES",
        (("Multica default", reject_multica),),
    )
    monkeypatch.setattr(dependency_probe, "probe_dbx", lambda: None)
    monkeypatch.setattr(dependency_probe.settings, "environment", "production")

    # Startup stays advisory in every environment: the bundled stack starts
    # after the core deployment, so blocking process startup on it would
    # deadlock the platform; production readiness still gates a configured
    # default (covered by the readiness tests).
    dependency_probe.probe_startup_dependencies()
    assert dependency_probe.main() == 0
    captured = capsys.readouterr()
    assert "Multica default: unavailable (RuntimeError)" in captured.err
    assert "advisory; API may start" in captured.err
    assert "multica detail must not be rendered" not in captured.err


def test_environment_minio_is_always_authoritative(monkeypatch):
    from app.shared import storage

    sentinel = object()
    monkeypatch.setattr(
        storage,
        "get_environment_storage_service",
        lambda: sentinel,
    )

    assert storage.get_storage_service() is sentinel


def test_dependency_probe_checks_dbx_only_in_production(monkeypatch, capsys):
    from app.shared import dependency_probe

    calls = []
    monkeypatch.setattr(dependency_probe, "PROBES", ())
    monkeypatch.setattr(
        dependency_probe, "probe_dbx", lambda: calls.append("dbx"))
    monkeypatch.setattr(dependency_probe.settings, "environment", "production")

    assert dependency_probe.main() == 0
    assert calls == ["dbx"]
    assert "dbx: ok" in capsys.readouterr().out


def test_dependency_probe_dbx_failure_fails_closed_without_secret(
    monkeypatch, capsys,
):
    from app.shared import dependency_probe

    def reject_dbx():
        raise RuntimeError("DBX_AUTH_FAILED should never be rendered as a value")

    monkeypatch.setattr(dependency_probe, "PROBES", ())
    monkeypatch.setattr(dependency_probe, "probe_dbx", reject_dbx)
    monkeypatch.setattr(dependency_probe.settings, "environment", "production")

    assert dependency_probe.main() == 1
    captured = capsys.readouterr()
    assert "dbx: unavailable (RuntimeError)" in captured.err
    assert "DBX_AUTH_FAILED should never be rendered" not in captured.err


def test_dependency_probe_does_not_include_dbx_outside_production(
    monkeypatch, capsys,
):
    from app.shared import dependency_probe

    calls = []
    monkeypatch.setattr(dependency_probe, "PROBES", ())
    monkeypatch.setattr(
        dependency_probe, "probe_dbx", lambda: calls.append("dbx"))
    monkeypatch.setattr(dependency_probe.settings, "environment", "development")

    assert dependency_probe.main() == 0
    assert calls == []
