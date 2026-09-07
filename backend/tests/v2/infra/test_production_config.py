"""Fail-closed production configuration and deployment gates."""

import base64
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tomllib

import pytest
import yaml
from cryptography.fernet import Fernet
import jwt

from app.shared.config import Settings, production_config_errors
from app.settings.workflows.n8n_client import enforce_n8n_url_policy


ROOT = Path(__file__).resolve().parents[4]


def _derived_fernet_key(secret_key: str) -> str:
    digest = hashlib.sha256(secret_key.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


def _production_settings(**updates):
    values = {
        "environment": "production",
        "database_url": "postgresql://app:strong-password@db:5432/app",
        "secret_key": "0123456789abcdef0123456789abcdef",
        "encryption_key": "",
        "cors_allowed_origins": "",
        "first_admin_password": "strong-admin-password",
        "redis_url": "redis://:strong-password@redis:6379/0",
        "neo4j_uri": "bolt://neo4j:7687",
        "neo4j_password": "strong-neo4j-password",
        "minio_endpoint": "minio:9000",
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


def test_existing_production_can_keep_secret_key_derived_encryption():
    assert production_config_errors(_production_settings()) == []


@pytest.mark.parametrize(
    "scheme",
    ["postgres", "postgresql+psycopg2"],
)
def test_production_rejects_noncanonical_postgres_schemes(scheme):
    settings = _production_settings(
        database_url=f"{scheme}://app:strong-password@db:5432/app"
    )

    assert any(
        "canonical postgresql://" in error
        for error in production_config_errors(settings)
    )


def test_explicit_encryption_key_must_still_be_valid_fernet():
    errors = production_config_errors(
        _production_settings(encryption_key="not-a-fernet-key"))
    assert "ENCRYPTION_KEY must be a valid Fernet key" in errors


def test_wildcard_cors_remains_blocked_but_empty_is_same_origin():
    errors = production_config_errors(
        _production_settings(cors_allowed_origins="*"))
    assert "CORS_ALLOWED_ORIGINS" in errors


@pytest.mark.parametrize(
    "token",
    ["", "dev-python-gateway-token", "change-me-python-gateway-token"],
)
def test_production_rejects_missing_or_default_kernel_gateway_token(token):
    errors = production_config_errors(
        _production_settings(python_kernel_gateway_auth_token=token))
    assert "PYTHON_KERNEL_GATEWAY_AUTH_TOKEN" in errors


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://:ontopromptredis123@redis:6379/0",  # compose 弱兜底默认
        "redis://redis:6379/0",                      # 无密码
    ],
)
def test_production_rejects_default_or_passwordless_redis(redis_url):
    errors = production_config_errors(
        _production_settings(redis_url=redis_url))
    assert "REDIS_URL credentials" in errors


def test_production_rejects_non_public_or_malformed_file_link_origins():
    defaults = production_config_errors(_production_settings(
        pipeline_file_public_app_base_url="http://localhost:5173",
        pipeline_file_public_api_base_url="http://127.0.0.1:8000",
    ))
    malformed = production_config_errors(_production_settings(
        pipeline_file_public_app_base_url="https://user@example.com",
        pipeline_file_public_api_base_url="https://api.example.com/files?x=1",
    ))
    with_path = production_config_errors(_production_settings(
        pipeline_file_public_app_base_url="https://platform.example.com/app",
    ))

    assert sum("browser-reachable public host" in error
               for error in defaults) == 2
    assert any("APP_BASE_URL must be an absolute" in error
               for error in malformed)
    assert any("API_BASE_URL must be an absolute" in error
               for error in malformed)
    assert any("APP_BASE_URL must be an absolute" in error
               for error in with_path)


def test_production_compose_requires_real_stack_without_chroma_or_fallbacks():
    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
    backend = compose["services"]["backend"]

    assert "chromadb" not in compose["services"]
    assert "celery_worker" not in compose["services"]  # Celery 已退役
    assert "chroma_data" not in compose["volumes"]
    environment = backend["environment"]
    assert "STORAGE_LOCAL_FALLBACK" not in environment
    assert "DATASET_IMPORT_USE_CELERY" not in environment
    assert "REQUIRE_EXTERNAL_DEPENDENCIES" not in environment
    assert "STRICT_PRODUCTION_CONFIG" not in environment
    assert backend["depends_on"]["minio"]["condition"] == "service_healthy"
    assert backend["depends_on"]["browser"]["condition"] == "service_started"
    assert "webSocketDebuggerUrl" in compose["services"]["browser"][
        "healthcheck"
    ]["test"][-1]
    assert backend["environment"]["STORAGE_LOCAL_DIR"] == "/uploads/object-storage"
    assert backend["environment"]["STEWARD_BROWSER_CDP_URL"] == (
        "http://browser:9222"
    )
    assert backend["environment"]["N8N_TIMEOUT_SECONDS"] == (
        "${N8N_TIMEOUT_SECONDS:-30}"
    )
    assert "--requirepass" in compose["services"]["redis"]["command"][-1]
    assert "PIPELINE_FILE_PUBLIC_APP_BASE_URL" in backend["environment"]
    assert "PIPELINE_FILE_PUBLIC_API_BASE_URL" in backend["environment"]
    assert "uploads:/uploads" in backend["volumes"]


def test_removed_graph_fallback_dependencies_are_not_packaged():
    project = tomllib.loads(
        (ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = project["project"]["dependencies"]

    assert not any(item.startswith("chromadb") for item in dependencies)
    assert not any(item.startswith("networkx") for item in dependencies)
    assert not (
        ROOT / "backend" / "app" / "ontologies" / "graph"
        / "networkx_service.py"
    ).exists()
    assert not (
        ROOT / "backend" / "app" / "shared" / "chroma_service.py"
    ).exists()


def test_local_compose_requires_real_stack_without_chroma():
    compose = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
    backend = compose["services"]["backend"]
    migrate = compose["services"]["migrate"]

    assert "chromadb" not in compose["services"]
    assert "celery_worker" not in compose["services"]  # Celery 已退役
    assert "chroma_data" not in compose["volumes"]
    for dependency in ("db", "redis", "neo4j", "minio"):
        assert backend["depends_on"][dependency]["condition"] == (
            "service_healthy"
        )
    assert backend["depends_on"]["browser"]["condition"] == "service_started"
    assert migrate["command"] == "alembic upgrade head"
    assert migrate["depends_on"]["db"]["condition"] == "service_healthy"
    assert backend["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert "webSocketDebuggerUrl" in compose["services"]["browser"][
        "healthcheck"
    ]["test"][-1]
    assert "--requirepass" in compose["services"]["redis"]["command"][-1]


def test_deploy_probes_dependencies_after_start_and_before_migrations():
    script = (ROOT / "deploy" / "deploy-prod.sh").read_text(
        encoding="utf-8"
    )

    assert "compose up -d --wait" in script
    start = script.index("run_with_retry start_required_dependency_services")
    probe = script.index("backend python -m app.shared.dependency_probe")
    migrate = script.index('log "running database migrations"')
    assert start < probe < migrate
    assert "compose up -d browser" in script
    wait_block = script[script.index("compose up -d --wait"):script.index(
        "else", script.index("compose up -d --wait"))]
    assert "db redis neo4j minio" in wait_block
    assert "db redis neo4j minio browser" not in wait_block


def test_workflow_keeps_persistent_env_in_place_during_source_replacement(
    tmp_path,
):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-nano-ontoprompt.yml").read_text()
    )
    upload_step = next(
        step for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("name") == "Upload source to server"
    )
    script = upload_step["run"]
    syntax = subprocess.run(
        ["bash", "-n"], input=script, capture_output=True, text=True,
    )

    assert syntax.returncode == 0, syntax.stderr
    assert "/tmp/openontology.env" not in script
    assert "! -name .env" in script
    assert "rm -rf '${APP_DIR}'" not in script
    assert "REMOTE_ARCHIVE=" in script
    assert "bash -s --" in script
    assert "umask 077" in script
    assert "--no-same-permissions" in script
    assert "--no-same-owner" in script
    assert '[ -L "$APP_DIR" ]' in script
    assert '[ -L "$APP_DIR/.env" ]' in script
    assert 'chmod 600 "$APP_DIR/deploy/production.dependencies.env"' in script
    assert 'chmod -R a+rX "$APP_DIR/frontend/dist"' in script
    assert '[ ! -L "$APP_DIR/.env" ]' in script
    assert script.index(" scp ") < script.index('find "$APP_DIR"')
    assert script.index("tar -tzf") < script.index('find "$APP_DIR"')
    assert script.index('[ -L "$APP_DIR/.env" ]') < script.index(
        'find "$APP_DIR"'
    )

    lines = script.splitlines()
    remote_start = next(
        index for index, line in enumerate(lines)
        if line.endswith("<<'REMOTE_SOURCE_SWAP'")
    ) + 1
    remote_end = lines.index("REMOTE_SOURCE_SWAP", remote_start)
    remote_script = "\n".join(lines[remote_start:remote_end]) + "\n"
    app_dir = tmp_path / "app"
    authority_dir = app_dir / "runtime-secrets"
    authority_dir.mkdir(parents=True)
    authority = authority_dir / "prod.env"
    authority.write_text("SECRET_KEY=must-survive\n", encoding="utf-8")
    original = authority.read_bytes()
    (app_dir / ".env").symlink_to(Path("runtime-secrets/prod.env"))
    archive_source = tmp_path / "archive-source"
    (archive_source / "deploy").mkdir(parents=True)
    (archive_source / "deploy" / "production.dependencies.env").write_text(
        "ENVIRONMENT=production\n",
        encoding="utf-8",
    )
    archive = tmp_path / "source.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(archive), "-C", str(archive_source), "."],
        check=True,
    )

    result = subprocess.run(
        ["bash", "-s", "--", str(app_dir), str(archive)],
        input=remote_script,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "production .env must be a regular file" in (
        result.stdout + result.stderr
    )
    assert (app_dir / ".env").is_symlink()
    assert authority.read_bytes() == original
    assert not archive.exists()


def test_source_swap_keeps_prebuilt_frontend_assets_readable(tmp_path):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-nano-ontoprompt.yml").read_text()
    )
    upload_step = next(
        step for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("name") == "Upload source to server"
    )
    lines = upload_step["run"].splitlines()
    remote_start = next(
        index for index, line in enumerate(lines)
        if line.endswith("<<'REMOTE_SOURCE_SWAP'")
    ) + 1
    remote_end = lines.index("REMOTE_SOURCE_SWAP", remote_start)
    remote_script = "\n".join(lines[remote_start:remote_end]) + "\n"

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    archive_source = tmp_path / "archive-source"
    (archive_source / "deploy").mkdir(parents=True)
    (archive_source / "deploy" / "production.dependencies.env").write_text(
        "ENVIRONMENT=production\n",
        encoding="utf-8",
    )
    dist = archive_source / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>\n", encoding="utf-8")
    archive = tmp_path / "source.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(archive), "-C", str(archive_source), "."],
        check=True,
    )

    result = subprocess.run(
        ["bash", "-s", "--", str(app_dir), str(archive)],
        input=remote_script,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    extracted = app_dir / "frontend" / "dist" / "index.html"
    assert stat.S_IMODE(extracted.stat().st_mode) & 0o044
    manifest = app_dir / "deploy" / "production.dependencies.env"
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


def test_workflow_requires_manual_fresh_install_confirmation():
    workflow_path = ROOT / ".github/workflows/deploy-nano-ontoprompt.yml"
    contents = workflow_path.read_text()
    workflow = yaml.safe_load(contents)
    deploy_step = next(
        step for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("name") == "Deploy over SSH"
    )

    assert "bootstrap_production_env:" in contents
    assert "default: false" in contents
    assert "BOOTSTRAP_PRODUCTION_ENV" in deploy_step["env"]
    bootstrap_expression = deploy_step["env"]["BOOTSTRAP_PRODUCTION_ENV"]
    assert "workflow_dispatch" in bootstrap_expression
    assert "inputs.bootstrap_production_env" in bootstrap_expression
    assert "BOOTSTRAP_PRODUCTION_ENV=" in deploy_step["run"]


def test_workflow_publishes_real_ciphertext_migration_evidence():
    workflow_path = ROOT / ".github/workflows/deploy-nano-ontoprompt.yml"
    workflow = yaml.safe_load(workflow_path.read_text())
    steps = workflow["jobs"]["verify-backend"]["steps"]
    names = [step.get("name") for step in steps]
    migration_index = names.index("Fresh database migration")
    e2e_index = names.index("Persisted ciphertext secret-split E2E")
    upload_index = names.index("Upload runtime-secret migration evidence")
    e2e_step = steps[e2e_index]
    upload_step = steps[upload_index]
    script = (
        ROOT / "backend/scripts/runtime_secret_split_postgres_e2e.py"
    ).read_text()

    assert migration_index < e2e_index < upload_index
    assert e2e_step["env"]["OPENONTOLOGY_RUNTIME_SECRET_E2E"] == "1"
    assert e2e_step["env"]["ENVIRONMENT"] == "test"
    assert "127.0.0.1" in e2e_step["env"]["DATABASE_URL"]
    assert e2e_step["env"]["DATABASE_URL"].endswith("_ci")
    assert "--self-test" in e2e_step["run"]
    assert "--report" in e2e_step["run"]
    assert upload_step["if"] == "${{ always() && matrix.shard == 1 }}"
    assert upload_step["with"]["if-no-files-found"] == "error"
    assert "production.dependencies.env" not in script
    assert "session_replication_role = replica" in script
    assert "transaction.rollback()" in script


def test_operator_configured_public_http_n8n_is_allowed_in_production():
    assert enforce_n8n_url_policy(
        "http://n8n.example.com:5678/api/v1", environment="production"
    ) == "http://n8n.example.com:5678/api/v1"


def test_private_http_and_public_https_n8n_are_allowed_in_production():
    assert enforce_n8n_url_policy(
        "http://10.0.0.8:5678", environment="production"
    ) == "http://10.0.0.8:5678/api/v1"
    assert enforce_n8n_url_policy(
        "https://n8n.example.com/api/v1", environment="production"
    ) == "https://n8n.example.com/api/v1"


def test_secret_key_derived_encryption_remains_decryptable(monkeypatch):
    from app.shared import encryption

    legacy_secret = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(encryption.settings, "encryption_key", "")
    monkeypatch.setattr(encryption.settings, "secret_key", legacy_secret)
    ciphertext = encryption.encrypt("existing-connection-password")
    monkeypatch.setattr(
        encryption.settings, "encryption_key",
        _derived_fernet_key(legacy_secret),
    )
    monkeypatch.setattr(
        encryption.settings, "secret_key",
        "fedcba9876543210fedcba9876543210",
    )

    assert encryption.decrypt(ciphertext) == "existing-connection-password"


def test_secret_key_rotation_invalidates_only_old_jwt(monkeypatch):
    from app.auth import service
    from app.data_channel.file_assets import service as file_service

    monkeypatch.setattr(
        service.settings, "secret_key",
        "0123456789abcdef0123456789abcdef",
    )
    old_token = service.create_access_token({"sub": "existing-user"})
    old_upload_token = file_service.create_upload_token(
        pipeline_id="pipeline-1",
        workflow_id="workflow-1",
        invocation_id="invocation-1",
        purpose="run",
        owner_id="existing-user",
    )
    monkeypatch.setattr(
        service.settings, "secret_key",
        "fedcba9876543210fedcba9876543210",
    )

    with pytest.raises(jwt.InvalidTokenError):
        service.decode_token(old_token)
    with pytest.raises(file_service.FileAssetError):
        file_service.decode_upload_token(old_upload_token)
    new_token = service.create_access_token({"sub": "existing-user"})
    assert service.decode_token(new_token)["sub"] == "existing-user"
    new_upload_token = file_service.create_upload_token(
        pipeline_id="pipeline-1",
        workflow_id="workflow-1",
        invocation_id="invocation-2",
        purpose="run",
        owner_id="existing-user",
    )
    assert file_service.decode_upload_token(new_upload_token)[
        "invocation_id"
    ] == "invocation-2"


def test_future_admin_seed_does_not_change_existing_admin_hash(
    db,
    admin_user,
    monkeypatch,
):
    from app.auth import service

    original_hash = admin_user.password_hash
    monkeypatch.setattr(
        service.settings, "first_admin_password",
        "new-future-admin-seed",
    )

    service.seed_admin(db)

    db.refresh(admin_user)
    assert admin_user.password_hash == original_hash


def _run_deploy_validation(
    app_dir: Path,
    *,
    health_url: str | None = None,
    bootstrap: bool = True,
    extra_env: dict[str, str] | None = None,
):
    manifest = app_dir / "deploy" / "production.dependencies.env"
    if not manifest.exists():
        manifest.parent.mkdir(parents=True, exist_ok=True)
        _write_valid_dependency_manifest(manifest)
    env = os.environ.copy()
    env.update({
        "APP_DIR": str(app_dir),
        "SKIP_GIT": "1",
        "DEPLOY_VALIDATE_ONLY": "1",
        "BOOTSTRAP_PRODUCTION_ENV": "1" if bootstrap else "0",
    })
    if health_url is not None:
        env["HEALTH_URL"] = health_url
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(ROOT / "deploy" / "deploy-prod.sh")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
    )


def _write_valid_dependency_manifest(
    path: Path,
    *,
    public_port: str = "80",
    redis_url: str = (
        "redis://:synthetic-redis-password@redis.example.com:6379/0"
    ),
    database_url: str = (
        "postgresql://ontology:synthetic-postgres-password"
        "@pg.example.com:5432/ontology"
    ),
    neo4j_uri: str = "bolt+s://graph.example.com:7687",
    neo4j_auth: str = "neo4j/synthetic-neo4j-password",
    postgres_user: str = "ontology",
    postgres_password: str = "synthetic-postgres-password",
    postgres_db: str = "ontology",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "ENVIRONMENT=production\n"
        f"PUBLIC_PORT={public_port}\n"
        f"POSTGRES_DB={postgres_db}\n"
        f"POSTGRES_USER={postgres_user}\n"
        f"POSTGRES_PASSWORD={postgres_password}\n"
        f"DATABASE_URL={database_url}\n"
        f"REDIS_URL={redis_url}\n"
        f"NEO4J_URI={neo4j_uri}\n"
        "NEO4J_USER=neo4j\n"
        "NEO4J_PASSWORD=synthetic-neo4j-password\n"
        f"NEO4J_AUTH={neo4j_auth}\n"
        "MINIO_ENDPOINT=objects.example.com:9000\n"
        "MINIO_ACCESS_KEY=synthetic-minio-access\n"
        "MINIO_SECRET_KEY=synthetic-minio-secret\n"
        "MINIO_USE_SSL=true\n"
        "N8N_API_URL=https://n8n.example.com/api/v1\n"
        "N8N_API_KEY=synthetic-n8n-api-key\n"
        "N8N_TIMEOUT_SECONDS=30\n",
        encoding="utf-8",
    )


def _read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value
    return values


def test_deploy_bootstraps_server_env_without_more_github_secrets(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated_path = tmp_path / ".env"
    generated = _read_env(generated_path)
    assert generated["ENVIRONMENT"] == "production"
    assert len(generated["SECRET_KEY"]) == 64
    Fernet(generated["ENCRYPTION_KEY"].encode())
    assert generated["ENCRYPTION_KEY"] != _derived_fernet_key(
        generated["SECRET_KEY"]
    )
    assert len(generated["FIRST_ADMIN_PASSWORD"]) == 48
    assert generated["POSTGRES_PASSWORD"] in generated["DATABASE_URL"]
    assert generated["NEO4J_AUTH"] == f"neo4j/{generated['NEO4J_PASSWORD']}"
    assert generated["MINIO_ACCESS_KEY"] != "minioadmin"
    assert generated["MINIO_SECRET_KEY"] != "minioadmin"
    assert generated["REDIS_PASSWORD"] != "ontopromptredis123"
    assert generated["STORAGE_LOCAL_DIR"] == "/uploads/object-storage"
    assert generated["STEWARD_BROWSER_CDP_URL"] == "http://browser:9222"
    assert generated["PIPELINE_FILE_GATEWAY_BASE_URL"] == (
        "http://127.0.0.1:80/api/v2/file-transfer")
    assert generated["PIPELINE_FILE_PUBLIC_APP_BASE_URL"] == (
        "http://127.0.0.1:80")
    assert generated["PIPELINE_FILE_PUBLIC_API_BASE_URL"] == (
        "http://127.0.0.1:80")
    output = result.stdout + result.stderr
    for sensitive_value in (
        generated["SECRET_KEY"],
        generated["ENCRYPTION_KEY"],
        generated["FIRST_ADMIN_PASSWORD"],
        generated["POSTGRES_PASSWORD"],
        generated["REDIS_PASSWORD"],
        generated["NEO4J_PASSWORD"],
        generated["MINIO_ACCESS_KEY"],
        generated["MINIO_SECRET_KEY"],
        generated["API_HUB_SYSTEM_MCP_TOKEN"],
    ):
        assert sensitive_value
        assert sensitive_value not in output
    assert stat.S_IMODE(generated_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(
        (tmp_path / "deploy" / "production.dependencies.env").stat().st_mode
    ) == 0o600


def test_deploy_refuses_missing_env_without_fresh_install_confirmation(
    tmp_path,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")

    result = _run_deploy_validation(tmp_path, bootstrap=False)

    assert result.returncode != 0
    assert not (tmp_path / ".env").exists()
    assert "refusing to generate new encryption authority" in (
        result.stdout + result.stderr
    )


def test_deploy_cleans_incomplete_bootstrap_without_publishing_env(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_ln = fake_bin / "ln"
    fake_ln.write_text("#!/usr/bin/env bash\nexit 73\n", encoding="utf-8")
    fake_ln.chmod(0o755)

    result = _run_deploy_validation(
        tmp_path,
        extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode != 0
    assert not (tmp_path / ".env").exists()
    assert list(tmp_path.glob(".env.bootstrap.*")) == []
    assert "refusing to overwrite it" in result.stdout + result.stderr


@pytest.mark.parametrize("dangling", [False, True])
def test_deploy_refuses_env_symlink_without_replacing_authority(
    tmp_path,
    dangling,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    target = tmp_path / "mounted-production.env"
    if not dangling:
        shutil.copy(ROOT / ".env.example", target)
        original = target.read_bytes()
    (tmp_path / ".env").symlink_to(target)

    result = _run_deploy_validation(tmp_path)

    assert result.returncode != 0
    assert (tmp_path / ".env").is_symlink()
    if not dangling:
        assert target.read_bytes() == original
    else:
        assert not target.exists()
    assert "refusing to replace a secret-authority symlink" in (
        result.stdout + result.stderr
    )


def test_git_mode_refuses_to_delete_non_worktree_deployment_state(tmp_path):
    sentinel = tmp_path / ".env"
    sentinel.write_text("SECRET_KEY=must-survive\n", encoding="utf-8")
    original = sentinel.read_bytes()
    env = os.environ.copy()
    env.update({"APP_DIR": str(tmp_path), "SKIP_GIT": "0"})

    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / "deploy-prod.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert sentinel.read_bytes() == original
    assert "refusing to delete persistent deployment state" in (
        result.stdout + result.stderr
    )

    git_worktree = tmp_path / "git-worktree"
    git_worktree.mkdir()
    (git_worktree / ".git").mkdir()
    authority = git_worktree / "tracked-authority.env"
    authority.write_text("SECRET_KEY=must-also-survive\n", encoding="utf-8")
    authority_original = authority.read_bytes()
    (git_worktree / ".env").symlink_to(authority.name)
    fake_bin = tmp_path / "fake-git-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/usr/bin/env bash\n"
        ": > \"$OPENONTOLOGY_FAKE_GIT_MARKER\"\n"
        "rm -f -- \"$OPENONTOLOGY_FAKE_GIT_TARGET\"\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    marker = tmp_path / "git-was-invoked"
    symlink_env = os.environ.copy()
    symlink_env.update(
        {
            "APP_DIR": str(git_worktree),
            "SKIP_GIT": "0",
            "PATH": f"{fake_bin}:{symlink_env['PATH']}",
            "OPENONTOLOGY_FAKE_GIT_MARKER": str(marker),
            "OPENONTOLOGY_FAKE_GIT_TARGET": str(authority),
        }
    )

    symlink_result = subprocess.run(
        ["bash", str(ROOT / "deploy" / "deploy-prod.sh")],
        cwd=ROOT,
        env=symlink_env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert symlink_result.returncode != 0
    assert not marker.exists()
    assert (git_worktree / ".env").is_symlink()
    assert authority.read_bytes() == authority_original
    assert "refusing to replace a secret-authority symlink" in (
        symlink_result.stdout + symlink_result.stderr
    )


def test_deploy_refuses_app_dir_symlink_without_touching_target(tmp_path):
    target = tmp_path / "real-target"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("must survive\n", encoding="utf-8")
    app_link = tmp_path / "linked-app"
    app_link.symlink_to(target, target_is_directory=True)
    env = os.environ.copy()
    env.update({"APP_DIR": str(app_link), "SKIP_GIT": "1"})

    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / "deploy-prod.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert app_link.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "must survive\n"
    assert "refusing to follow a symlink" in result.stdout + result.stderr


def test_git_mode_restricts_manifest_before_missing_env_failure(tmp_path):
    (tmp_path / ".git").mkdir()
    manifest = tmp_path / "deploy" / "production.dependencies.env"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "N8N_API_KEY=must-not-remain-world-readable\n",
        encoding="utf-8",
    )
    manifest.chmod(0o644)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_git.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "APP_DIR": str(tmp_path),
        "SKIP_GIT": "0",
        "BOOTSTRAP_PRODUCTION_ENV": "0",
        "PATH": f"{fake_bin}:{env['PATH']}",
    })

    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / "deploy-prod.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert "refusing to generate new encryption authority" in (
        result.stdout + result.stderr
    )


def test_deploy_upgrades_exact_legacy_bundled_redis_url(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    _write_valid_dependency_manifest(
        tmp_path / "deploy" / "production.dependencies.env",
        redis_url="redis://redis:6379/0",
    )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(tmp_path / ".env")
    password = generated["REDIS_PASSWORD"]
    assert password != "ontopromptredis123"
    assert generated["REDIS_URL"] == f"redis://:{password}@redis:6379/0"
    assert "upgraded the legacy bundled Redis URL" in result.stdout
    assert password not in result.stdout + result.stderr


def test_deploy_derives_bundled_redis_requirepass_from_client_url(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    password = "synthetic-bundled-redis-password"
    redis_url = f"redis://:{password}@redis:6379/0"
    _write_valid_dependency_manifest(
        tmp_path / "deploy" / "production.dependencies.env",
        redis_url=redis_url,
    )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(tmp_path / ".env")
    assert generated["REDIS_PASSWORD"] == password
    assert generated["REDIS_URL"] == redis_url
    assert "synchronized the bundled Redis service password" in result.stdout
    assert password not in result.stdout + result.stderr


def test_deploy_preserves_external_redis_url_without_binding_local_password(
    tmp_path,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    redis_url = (
        "rediss://:synthetic-provider-password@cache.example.com:6380/0"
    )
    _write_valid_dependency_manifest(
        tmp_path / "deploy" / "production.dependencies.env",
        redis_url=redis_url,
    )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(tmp_path / ".env")
    assert generated["REDIS_URL"] == redis_url
    assert generated["REDIS_PASSWORD"] not in redis_url
    assert "preserved the external Redis URL" in result.stdout
    assert "synthetic-provider-password" not in result.stdout + result.stderr


def test_deploy_validates_bundled_postgres_and_neo4j_authorities(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    manifest = tmp_path / "deploy" / "production.dependencies.env"
    _write_valid_dependency_manifest(
        manifest,
        database_url=(
            "postgresql://ontology:synthetic-postgres-password"
            "@db:5432/ontology"
        ),
        neo4j_uri="bolt://neo4j:7687",
    )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "validated bundled PostgreSQL" in result.stdout
    assert "validated bundled Neo4j" in result.stdout


def test_deploy_compares_decoded_bundled_postgres_authority(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    _write_valid_dependency_manifest(
        tmp_path / "deploy" / "production.dependencies.env",
        database_url=(
            "postgres://onto%40logy:synthetic%3Apostgres-password"
            "@db:5432/onto%2Flogy"
        ),
        postgres_user="onto@logy",
        postgres_password="synthetic:postgres-password",
        postgres_db="onto/logy",
    )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "validated bundled PostgreSQL" in result.stdout


@pytest.mark.parametrize(
    ("database_url", "neo4j_uri", "neo4j_auth", "message"),
    [
        (
            "postgresql://ontology:wrong-password@db:5432/ontology",
            "bolt+s://graph.example.com:7687",
            "neo4j/synthetic-neo4j-password",
            "bundled DATABASE_URL must exactly match",
        ),
        (
            "postgresql://ontology:synthetic-postgres-password@pg.example.com:5432/ontology",
            "bolt://neo4j:7687",
            "neo4j/wrong-password",
            "bundled NEO4J_AUTH must match",
        ),
        (
            "postgresql://ontology:synthetic-postgres-password@pg.example.com:5432/ontology",
            "bolt+s://graph.example.com:7687",
            "none",
            "NEO4J_AUTH must enable authentication",
        ),
    ],
)
def test_deploy_rejects_mismatched_or_disabled_bundled_auth(
    tmp_path,
    database_url,
    neo4j_uri,
    neo4j_auth,
    message,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    _write_valid_dependency_manifest(
        tmp_path / "deploy" / "production.dependencies.env",
        database_url=database_url,
        neo4j_uri=neo4j_uri,
        neo4j_auth=neo4j_auth,
    )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode != 0
    assert message in result.stdout + result.stderr


def test_deploy_rejects_encoded_password_for_bundled_redis(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    _write_valid_dependency_manifest(
        tmp_path / "deploy" / "production.dependencies.env",
        redis_url="redis://:encoded%40password@redis:6379/0",
    )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "bundled Redis URL must use an unescaped" in output
    assert "encoded%40password" not in output


def test_deploy_splits_legacy_example_secret_without_rewriting_ciphertext(
    tmp_path,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env")
    legacy_secret = "change-me-to-a-random-32-char-string"
    legacy_key = _derived_fernet_key(legacy_secret)
    ciphertext = Fernet(legacy_key.encode()).encrypt(
        b"persisted-production-credential"
    )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(tmp_path / ".env")
    assert generated["SECRET_KEY"] != legacy_secret
    assert len(generated["SECRET_KEY"]) == 64
    assert generated["ENCRYPTION_KEY"] == legacy_key
    assert generated["FIRST_ADMIN_PASSWORD"] != "admin123"
    assert Fernet(generated["ENCRYPTION_KEY"].encode()).decrypt(
        ciphertext
    ) == b"persisted-production-credential"
    output = result.stdout + result.stderr
    assert "persisted ciphertext was not rewritten" in output
    for secret in (
        generated["SECRET_KEY"],
        generated["ENCRYPTION_KEY"],
        generated["FIRST_ADMIN_PASSWORD"],
    ):
        assert secret not in output

    # The split is a one-time, idempotent state transition. Ordinary future
    # deployments must preserve all three persisted values.
    second = _run_deploy_validation(tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _read_env(tmp_path / ".env") == generated


def test_deploy_splits_explicit_dev_secret_without_rewriting_ciphertext(
    tmp_path,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    env_path = tmp_path / ".env"
    contents = (ROOT / ".env.example").read_text().replace(
        "SECRET_KEY=change-me-to-a-random-32-char-string",
        "SECRET_KEY=dev-secret-key",
    )
    env_path.write_text(contents, encoding="utf-8")
    legacy_key = _derived_fernet_key("dev-secret-key")
    ciphertext = Fernet(legacy_key.encode()).encrypt(b"legacy-dev-secret")

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(env_path)
    assert generated["ENCRYPTION_KEY"] == legacy_key
    assert len(generated["SECRET_KEY"]) == 64
    assert Fernet(generated["ENCRYPTION_KEY"].encode()).decrypt(
        ciphertext
    ) == b"legacy-dev-secret"


def test_deploy_uses_historical_runtime_default_when_secret_key_is_absent(
    tmp_path,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            line for line in (ROOT / ".env.example").read_text().splitlines()
            if not line.startswith("SECRET_KEY=")
        ) + "\n",
        encoding="utf-8",
    )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(env_path)
    assert generated["ENCRYPTION_KEY"] == _derived_fernet_key(
        "dev-secret-key"
    )
    assert len(generated["SECRET_KEY"]) == 64


def test_deploy_preserves_ciphertext_from_explicitly_empty_legacy_secret(
    tmp_path,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    env_path = tmp_path / ".env"
    contents = (ROOT / ".env.example").read_text().replace(
        "SECRET_KEY=change-me-to-a-random-32-char-string",
        "SECRET_KEY=",
    )
    env_path.write_text(contents, encoding="utf-8")
    legacy_key = _derived_fernet_key("")
    ciphertext = Fernet(legacy_key.encode()).encrypt(
        b"credential-encrypted-with-empty-secret"
    )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(env_path)
    assert generated["ENCRYPTION_KEY"] == legacy_key
    assert len(generated["SECRET_KEY"]) == 64
    assert Fernet(generated["ENCRYPTION_KEY"].encode()).decrypt(
        ciphertext
    ) == b"credential-encrypted-with-empty-secret"


def test_admin_seed_upgrade_does_not_rewrite_custom_secret_escapes(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    env_path = tmp_path / ".env"
    custom_secret = r"custom-secret-with-literal-\n-and-\t-escapes-123456789"
    contents = (ROOT / ".env.example").read_text().replace(
        "SECRET_KEY=change-me-to-a-random-32-char-string",
        f"SECRET_KEY={custom_secret}",
    )
    env_path.write_text(contents, encoding="utf-8")

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(env_path)
    assert generated["SECRET_KEY"] == custom_secret
    assert generated["ENCRYPTION_KEY"] == ""
    assert generated["FIRST_ADMIN_PASSWORD"] != "admin123"


def test_deploy_preserves_existing_explicit_encryption_key(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    env_path = tmp_path / ".env"
    explicit_key = Fernet.generate_key().decode()
    contents = (ROOT / ".env.example").read_text().replace(
        "ENCRYPTION_KEY=", f"ENCRYPTION_KEY={explicit_key}",
    )
    env_path.write_text(contents, encoding="utf-8")
    ciphertext = Fernet(explicit_key.encode()).encrypt(b"existing-secret")

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(env_path)
    assert generated["ENCRYPTION_KEY"] == explicit_key
    assert generated["SECRET_KEY"] != (
        "change-me-to-a-random-32-char-string"
    )
    assert Fernet(generated["ENCRYPTION_KEY"].encode()).decrypt(
        ciphertext
    ) == b"existing-secret"
    assert explicit_key not in result.stdout + result.stderr


def test_bootstrap_confirmation_never_changes_existing_strong_authority(
    tmp_path,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    env_path = tmp_path / ".env"
    secret = "strong-existing-secret-key-0123456789abcdef"
    encryption_key = Fernet.generate_key().decode()
    admin_seed = "strong-existing-admin-seed"
    contents = (ROOT / ".env.example").read_text()
    contents = contents.replace(
        "SECRET_KEY=change-me-to-a-random-32-char-string",
        f"SECRET_KEY={secret}",
    ).replace(
        "ENCRYPTION_KEY=", f"ENCRYPTION_KEY={encryption_key}",
    ).replace(
        "FIRST_ADMIN_PASSWORD=admin123",
        f"FIRST_ADMIN_PASSWORD={admin_seed}",
    )
    env_path.write_text(contents, encoding="utf-8")

    result = _run_deploy_validation(tmp_path, bootstrap=True)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(env_path)
    assert generated["SECRET_KEY"] == secret
    assert generated["ENCRYPTION_KEY"] == encryption_key
    assert generated["FIRST_ADMIN_PASSWORD"] == admin_seed


def test_deploy_rejects_malformed_explicit_encryption_key_before_rotation(
    tmp_path,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    env_path = tmp_path / ".env"
    contents = (ROOT / ".env.example").read_text().replace(
        "ENCRYPTION_KEY=", "ENCRYPTION_KEY=malformed-key",
    )
    env_path.write_text(contents, encoding="utf-8")

    result = _run_deploy_validation(tmp_path)

    assert result.returncode != 0
    generated = _read_env(env_path)
    assert generated["SECRET_KEY"] == (
        "change-me-to-a-random-32-char-string"
    )
    assert generated["ENCRYPTION_KEY"] == "malformed-key"
    assert generated["FIRST_ADMIN_PASSWORD"] == "admin123"
    assert "ENCRYPTION_KEY must be a URL-safe base64 Fernet key" in (
        result.stdout + result.stderr
    )


def test_deploy_rejects_unknown_short_secret_without_guessing_old_key(
    tmp_path,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    env_path = tmp_path / ".env"
    contents = (ROOT / ".env.example").read_text()
    contents = contents.replace(
        "SECRET_KEY=change-me-to-a-random-32-char-string",
        "SECRET_KEY=unknown-short-secret",
    )
    env_path.write_text(contents, encoding="utf-8")

    result = _run_deploy_validation(tmp_path)

    assert result.returncode != 0
    generated = _read_env(env_path)
    assert generated["SECRET_KEY"] == "unknown-short-secret"
    assert generated["ENCRYPTION_KEY"] == ""
    assert generated["FIRST_ADMIN_PASSWORD"] == "admin123"
    assert "SECRET_KEY must contain at least 32 characters" in (
        result.stdout + result.stderr
    )


@pytest.mark.parametrize(
    "assignment",
    [
        "export SECRET_KEY=change-me-to-a-random-32-char-string",
        "SECRET_KEY =change-me-to-a-random-32-char-string",
    ],
)
def test_deploy_rejects_ambiguous_dotenv_secret_syntax_before_rotation(
    tmp_path,
    assignment,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    env_path = tmp_path / ".env"
    contents = (ROOT / ".env.example").read_text().replace(
        "SECRET_KEY=change-me-to-a-random-32-char-string",
        assignment,
    )
    env_path.write_text(contents, encoding="utf-8")

    result = _run_deploy_validation(tmp_path)

    assert result.returncode != 0
    assert assignment in env_path.read_text()
    assert "SECRET_KEY must use one canonical uppercase KEY=value" in (
        result.stdout + result.stderr
    )


@pytest.mark.parametrize(
    ("canonical_key", "variant_key"),
    [
        ("SECRET_KEY", "secret_key"),
        ("SECRET_KEY", "Secret_Key"),
        ("ENCRYPTION_KEY", "encryption_key"),
        ("ENCRYPTION_KEY", "Encryption_Key"),
        ("FIRST_ADMIN_PASSWORD", "first_admin_password"),
        ("FIRST_ADMIN_PASSWORD", "First_Admin_Password"),
    ],
)
def test_deploy_rejects_case_variant_authority_without_touching_env(
    tmp_path,
    canonical_key,
    variant_key,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    env_path = tmp_path / ".env"
    values = {
        "SECRET_KEY": "change-me-to-a-random-32-char-string",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "FIRST_ADMIN_PASSWORD": "admin123",
    }
    original_assignments = {
        "SECRET_KEY": "SECRET_KEY=change-me-to-a-random-32-char-string",
        "ENCRYPTION_KEY": "ENCRYPTION_KEY=",
        "FIRST_ADMIN_PASSWORD": "FIRST_ADMIN_PASSWORD=admin123",
    }
    contents = (ROOT / ".env.example").read_text().replace(
        original_assignments[canonical_key],
        f"{variant_key}={values[canonical_key]}",
    )
    env_path.write_text(contents, encoding="utf-8")
    original = env_path.read_bytes()

    result = _run_deploy_validation(tmp_path)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert env_path.read_bytes() == original
    assert f"{canonical_key} must use one canonical uppercase KEY=value" in output
    assert values[canonical_key] not in output


def test_deploy_rejects_duplicate_case_variant_authority(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    env_path = tmp_path / ".env"
    contents = (ROOT / ".env.example").read_text()
    contents += "secret_key=another-strong-secret-0123456789abcdef\n"
    env_path.write_text(contents, encoding="utf-8")
    original = env_path.read_bytes()

    result = _run_deploy_validation(tmp_path)

    assert result.returncode != 0
    assert env_path.read_bytes() == original
    assert "SECRET_KEY must use one canonical uppercase KEY=value" in (
        result.stdout + result.stderr
    )


def test_deploy_requires_dependency_manifest(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    env = os.environ.copy()
    env.update({
        "APP_DIR": str(tmp_path),
        "SKIP_GIT": "1",
        "BOOTSTRAP_PRODUCTION_ENV": "1",
        "DEPLOY_VALIDATE_ONLY": "1",
        "DEPENDENCY_CONFIG_FILE": "missing-production-dependencies.env",
    })

    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / "deploy-prod.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert "is required for production deployment" in result.stdout


def test_deploy_ignores_legacy_manifest_switches_without_reenabling_them(
    tmp_path,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    manifest = tmp_path / "deploy" / "production.dependencies.env"
    _write_valid_dependency_manifest(manifest)
    with manifest.open("a", encoding="utf-8") as stream:
        stream.write(
            "POSTGRES_HOST=ignored.example.com\n"
            "POSTGRES_PORT=5432\n"
            "MINIO_CONSOLE_URL=https://ignored.example.com\n"
            "STRICT_PRODUCTION_CONFIG=false\n"
            "REQUIRE_EXTERNAL_DEPENDENCIES=false\n"
            "DATASET_IMPORT_USE_CELERY=false\n"
            "STORAGE_LOCAL_FALLBACK=true\n"
            "N8N_EMAIL=\n"
            "N8N_PASSWORD=\n"
        )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(tmp_path / ".env")
    for key in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "MINIO_CONSOLE_URL",
        "STRICT_PRODUCTION_CONFIG",
        "REQUIRE_EXTERNAL_DEPENDENCIES",
        "DATASET_IMPORT_USE_CELERY",
        "STORAGE_LOCAL_FALLBACK",
        "N8N_EMAIL",
        "N8N_PASSWORD",
    ):
        assert key not in generated
        assert f"ignored deprecated production dependency setting: {key}" in (
            result.stdout
        )


def test_deploy_backfills_n8n_timeout_for_existing_env_and_old_manifest(
    tmp_path,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    first = _run_deploy_validation(tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr

    for filename in (".env", "deploy/production.dependencies.env"):
        path = tmp_path / filename
        path.write_text(
            "\n".join(
                line for line in path.read_text(encoding="utf-8").splitlines()
                if not line.startswith("N8N_TIMEOUT_SECONDS=")
            ) + "\n",
            encoding="utf-8",
        )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _read_env(tmp_path / ".env")["N8N_TIMEOUT_SECONDS"] == "30"


def test_deploy_derives_pipeline_file_gateway_from_external_health_url(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")

    result = _run_deploy_validation(
        tmp_path, health_url="https://platform.example.com/")

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(tmp_path / ".env")
    assert generated["PIPELINE_FILE_GATEWAY_BASE_URL"] == (
        "https://platform.example.com/api/v2/file-transfer")
    assert generated["PIPELINE_FILE_PUBLIC_APP_BASE_URL"] == (
        "https://platform.example.com")
    assert generated["PIPELINE_FILE_PUBLIC_API_BASE_URL"] == (
        "https://platform.example.com")


def test_deploy_uses_manifest_public_port_for_default_health_origin(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    _write_valid_dependency_manifest(
        tmp_path / "deploy" / "production.dependencies.env", public_port="8123"
    )

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(tmp_path / ".env")
    assert generated["PUBLIC_PORT"] == "8123"
    assert generated["PIPELINE_FILE_GATEWAY_BASE_URL"] == (
        "http://127.0.0.1:8123/api/v2/file-transfer")
    assert generated["PIPELINE_FILE_PUBLIC_APP_BASE_URL"] == (
        "http://127.0.0.1:8123")
    assert generated["PIPELINE_FILE_PUBLIC_API_BASE_URL"] == (
        "http://127.0.0.1:8123")


def _deploy_workflow_script() -> str:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" /
         "deploy-nano-ontoprompt.yml").read_text())
    deploy_step = next(
        step
        for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("name") == "Deploy over SSH"
    )
    return deploy_step["run"]


def _run_deploy_workflow_step(
    tmp_path: Path,
    *,
    dependency_config: str,
    health_url: str = "",
):
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "production.dependencies.env").write_text(
        dependency_config,
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sshpass = fake_bin / "sshpass"
    fake_sshpass.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    fake_sshpass.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "GITHUB_WORKSPACE": str(ROOT),
        "DEPLOY_HOST": "203.0.113.8",
        "DEPLOY_USER": "deploy-user",
        "DEPLOY_PASSWORD": "test-password",
        "DEPLOY_APP_DIR": "/srv/openontology",
        "DEPLOY_HEALTH_URL": health_url,
        "BOOTSTRAP_PRODUCTION_ENV": "0",
    })
    return subprocess.run(
        ["bash", "-e", "-c", _deploy_workflow_script()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize(
    ("public_port", "expected_url"),
    [
        ("8088", "http://203.0.113.8:8088/"),
        ("80", "http://203.0.113.8/"),
    ],
)
def test_deploy_workflow_defaults_health_url_to_manifest_public_port(
    tmp_path, public_port, expected_url,
):
    result = _run_deploy_workflow_step(
        tmp_path,
        dependency_config=f"PUBLIC_PORT={public_port}\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"HEALTH_URL='{expected_url}'" in result.stdout


def test_deploy_workflow_preserves_explicit_health_url(tmp_path):
    result = _run_deploy_workflow_step(
        tmp_path,
        dependency_config="PUBLIC_PORT=not-used\n",
        health_url="https://platform.example.com/health-root/",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "HEALTH_URL='https://platform.example.com/health-root/'"
        in result.stdout
    )


@pytest.mark.parametrize(
    "health_url",
    [
        "ftp://platform.example.com/",
        "https://platform.example.com/' ; touch /tmp/injected ; echo '",
        "https://platform.example.com/unsafe path/",
    ],
)
def test_deploy_workflow_rejects_unsafe_health_url_before_ssh(
    tmp_path,
    health_url,
):
    result = _run_deploy_workflow_step(
        tmp_path,
        dependency_config="PUBLIC_PORT=not-used\n",
        health_url=health_url,
    )

    assert result.returncode != 0
    assert "DEPLOY_HEALTH_URL must" in result.stdout
    assert "ssh -o StrictHostKeyChecking" not in result.stdout


@pytest.mark.parametrize("public_port", ["not-a-port", "0", "65536"])
def test_deploy_workflow_rejects_invalid_manifest_public_port(
    tmp_path, public_port,
):
    result = _run_deploy_workflow_step(
        tmp_path,
        dependency_config=f"PUBLIC_PORT={public_port}\n",
    )

    assert result.returncode != 0
    assert "PUBLIC_PORT must be an integer between 1 and 65535" in result.stdout
    assert "ssh -o StrictHostKeyChecking" not in result.stdout


def test_deploy_ignores_empty_exported_public_port_and_uses_validated_env(
    tmp_path, monkeypatch,
):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    _write_valid_dependency_manifest(
        tmp_path / "deploy" / "production.dependencies.env", public_port="8123"
    )
    monkeypatch.setenv("PUBLIC_PORT", "")

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(tmp_path / ".env")
    assert generated["PUBLIC_PORT"] == "8123"
    assert generated["PIPELINE_FILE_GATEWAY_BASE_URL"] == (
        "http://127.0.0.1:8123/api/v2/file-transfer")
    assert generated["PIPELINE_FILE_PUBLIC_APP_BASE_URL"] == (
        "http://127.0.0.1:8123")
    assert generated["PIPELINE_FILE_PUBLIC_API_BASE_URL"] == (
        "http://127.0.0.1:8123")


def test_deploy_preserves_explicit_pipeline_file_gateway(tmp_path):
    content = (ROOT / ".env.example").read_text().replace(
        "PIPELINE_FILE_GATEWAY_BASE_URL=http://backend:8000/api/v2/file-transfer",
        "PIPELINE_FILE_GATEWAY_BASE_URL=https://platform.example.com/api/v2/file-transfer",
    )
    (tmp_path / ".env.example").write_text(content)

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(tmp_path / ".env")
    assert generated["PIPELINE_FILE_GATEWAY_BASE_URL"] == (
        "https://platform.example.com/api/v2/file-transfer")


def test_deploy_preserves_explicit_public_file_origins(tmp_path):
    content = (ROOT / ".env.example").read_text()
    content = content.replace(
        "PIPELINE_FILE_PUBLIC_APP_BASE_URL=http://localhost:5173",
        "PIPELINE_FILE_PUBLIC_APP_BASE_URL=https://app.example.com",
    ).replace(
        "PIPELINE_FILE_PUBLIC_API_BASE_URL=http://localhost:8000",
        "PIPELINE_FILE_PUBLIC_API_BASE_URL=https://files.example.com",
    )
    (tmp_path / ".env.example").write_text(content)

    result = _run_deploy_validation(
        tmp_path, health_url="https://platform.example.com/")

    assert result.returncode == 0, result.stdout + result.stderr
    generated = _read_env(tmp_path / ".env")
    assert generated["PIPELINE_FILE_PUBLIC_APP_BASE_URL"] == (
        "https://app.example.com")
    assert generated["PIPELINE_FILE_PUBLIC_API_BASE_URL"] == (
        "https://files.example.com")


def test_deploy_rejects_malformed_public_file_origin(tmp_path):
    content = (ROOT / ".env.example").read_text().replace(
        "PIPELINE_FILE_PUBLIC_API_BASE_URL=http://localhost:8000",
        "PIPELINE_FILE_PUBLIC_API_BASE_URL=https://user@example.com/files?x=1",
    )
    (tmp_path / ".env.example").write_text(content)

    result = _run_deploy_validation(tmp_path)

    assert result.returncode != 0
    assert "PIPELINE_FILE_PUBLIC_API_BASE_URL must be an absolute" in result.stdout


def test_deploy_pins_legacy_storage_read_root_to_shared_volume(tmp_path):
    content = (ROOT / ".env.example").read_text().replace(
        "STORAGE_LOCAL_DIR=storage",
        "STORAGE_LOCAL_DIR=relative/object-storage",
    )
    (tmp_path / ".env.example").write_text(content)

    result = _run_deploy_validation(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _read_env(tmp_path / ".env")["STORAGE_LOCAL_DIR"] == (
        "/uploads/object-storage")
