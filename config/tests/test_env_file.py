from __future__ import annotations

import os
import stat
from pathlib import Path

from dotenv import dotenv_values

from app.env_file import LocalEnvStore
from app.models import ConfigProfile, default_profile


def complete_profile() -> ConfigProfile:
    profile = default_profile()
    payload = profile.model_dump()
    payload["postgres"]["password"] = "Pg@pass:word/#%"
    payload["redis"]["username"] = "ontology"
    payload["redis"]["password"] = "Redis@pass:word/#%"
    payload["nats"]["token"] = "Nats@token:word/#%"
    payload["neo4j"]["password"] = "Neo4j@pass:word/#%"
    payload["minio"]["access_key"] = "ontology-access"
    payload["minio"]["secret_key"] = "Minio@pass:word/#%"
    payload["n8n"]["api_key"] = "n8n-local-key"
    return ConfigProfile.model_validate(payload)


def test_env_round_trip_keeps_complete_mode_and_special_credentials(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "config" / "generated" / "local" / ".env"
    store = LocalEnvStore(env_path)
    original = complete_profile()

    result = store.write(original)
    loaded = store.load_profile()
    parsed = dotenv_values(env_path, interpolate=False)

    assert result.path == env_path
    assert result.backup_path is None
    assert loaded == original
    assert parsed["ENVIRONMENT"] == "development"
    assert parsed["N8N_API_KEY"] == "n8n-local-key"
    assert parsed["REDIS_PASSWORD"] == "Redis@pass:word/#%"
    assert parsed["REDIS_PASSWORD"] not in parsed["REDIS_URL"]
    assert parsed["NATS_URL"].startswith("nats://")
    assert "Nats@token:word" not in parsed["NATS_URL"]
    assert "Nats%40token%3Aword%2F%23%25" in parsed["NATS_URL"]
    assert parsed["LOCAL_CONFIG_MANAGED"] == "true"
    assert parsed["UPLOADS_DIR"] == "./runtime/uploads"
    assert "sqlite" not in parsed["DATABASE_URL"].lower()
    assert parsed["DATABASE_URL"].startswith("postgresql://")
    assert "Pg@pass:word" not in parsed["DATABASE_URL"]
    assert env_path.read_text(encoding="utf-8").startswith(
        "# 由 OpenOntology 本地配置中心生成"
    )


def test_config_center_renders_weknora_connection_without_model_credentials(tmp_path: Path) -> None:
    env_path = tmp_path / "config" / "generated" / "local" / ".env"
    store = LocalEnvStore(env_path, project_root=tmp_path)
    store.write(complete_profile())
    parsed = dotenv_values(env_path, interpolate=False)
    assert parsed["SUPER_ASSISTANT_MCP_INTERNAL_BASE_URLS"] == "http://127.0.0.1:18080/mcp"
    assert parsed["WEKNORA_BASE_URL"] == "http://127.0.0.1:18080"
    assert not any(key.startswith(("WEKNORA_STATE_", "WEKNORA_CHAT_", "WEKNORA_EMBEDDING_")) for key in parsed)


def test_public_profile_masks_saved_secrets_and_blank_preserves_them(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "generated" / ".env"
    store = LocalEnvStore(env_path)
    original = complete_profile()
    store.write(original)

    public, present, warning = store.public_profile()
    assert warning is None
    assert public.postgres.password == ""
    assert present["postgres.password"] is True

    payload = public.model_dump()
    payload["platform"]["frontend_port"] = 5199
    submitted = ConfigProfile.model_validate(payload)
    resolved = store.resolve_secrets(submitted)

    assert resolved.platform.frontend_port == 5199
    assert resolved.postgres.password == original.postgres.password


def test_fresh_defaults_are_visible_but_not_marked_as_retained(
    tmp_path: Path,
) -> None:
    store = LocalEnvStore(tmp_path / "generated" / ".env")

    public, present, warning = store.public_profile()

    assert warning is None
    assert public.minio.access_key == "admin"
    assert present["minio.access_key"] is False
    assert present["platform.secret_key"] is False


def test_rewrite_creates_single_backup_and_uses_restricted_permissions(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "generated" / ".env"
    store = LocalEnvStore(env_path)
    first = complete_profile()
    store.write(first)
    first_bytes = env_path.read_bytes()

    payload = first.model_dump()
    payload["platform"]["frontend_port"] = 5199
    second = ConfigProfile.model_validate(payload)
    result = store.write(second)

    assert result.backup_path == env_path.with_name(".env.bak")
    assert result.backup_path.read_bytes() == first_bytes
    assert not list(env_path.parent.glob("*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.backup_path.stat().st_mode) == 0o600


def test_missing_required_external_secret_fails_closed(tmp_path: Path) -> None:
    store = LocalEnvStore(tmp_path / ".env")
    profile = default_profile()

    try:
        store.resolve_secrets(profile)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("完整模式缺少第三方凭据时必须拒绝生成")

    assert "postgres.password" in message
    assert "redis.password" in message
    assert "n8n.api_key" in message
    # NATS token 非必需，空 token 不应被视为缺失凭据
    assert "nats.token" not in message


def test_nats_url_rendered_without_token_and_with_ipv6_host(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "generated" / ".env"
    store = LocalEnvStore(env_path)
    payload = complete_profile().model_dump()
    payload["nats"]["token"] = ""
    payload["nats"]["host"] = "::1"
    store.write(ConfigProfile.model_validate(payload))

    parsed = dotenv_values(env_path, interpolate=False)
    assert parsed["NATS_URL"] == "nats://[::1]:4222"
    assert "@" not in parsed["NATS_URL"]
    loaded = store.load_profile()
    assert loaded.nats.host == "::1"
    assert loaded.nats.port == 4222
    assert loaded.nats.token == ""


def test_nats_probe_allows_blank_token_and_preserves_saved_one(
    tmp_path: Path,
) -> None:
    store = LocalEnvStore(tmp_path / ".env")
    profile = ConfigProfile.model_validate(default_profile().model_dump())

    resolved = store.resolve_service_secrets(profile, "nats")
    assert resolved.nats.token == ""

    original = complete_profile()
    store.write(original)
    public, present, warning = store.public_profile()
    assert warning is None
    assert public.nats.token == ""
    assert present["nats.token"] is True

    resolved = store.resolve_service_secrets(public, "nats")
    assert resolved.nats.token == original.nats.token


def test_individual_probe_only_requires_its_own_credentials(
    tmp_path: Path,
) -> None:
    store = LocalEnvStore(tmp_path / ".env")
    payload = default_profile().model_dump()
    payload["postgres"]["password"] = "postgres-only-password"
    profile = ConfigProfile.model_validate(payload)

    postgres = store.resolve_service_secrets(profile, "postgres")
    browser = store.resolve_service_secrets(profile, "browser")

    assert postgres.postgres.password == "postgres-only-password"
    assert postgres.redis.password == ""
    assert browser.browser.cdp_url == "http://127.0.0.1:9222"

    try:
        store.resolve_service_secrets(profile, "redis")
    except ValueError as exc:
        assert "redis.password" in str(exc)
    else:
        raise AssertionError("Redis 单项测试缺少自身密码时必须拒绝")


def test_ignored_local_defaults_are_loaded_and_secrets_stay_masked(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "config" / "generated" / "local" / ".env"
    defaults_path = env_path.with_name("defaults.env")
    defaults_path.parent.mkdir(parents=True)
    defaults_path.write_text(
        "LOCAL_POSTGRES_HOST=127.0.0.1\n"
        "LOCAL_POSTGRES_PORT=5432\n"
        "LOCAL_POSTGRES_DATABASE=openontology\n"
        "LOCAL_POSTGRES_USER=postgres\n"
        "LOCAL_POSTGRES_PASSWORD=postgres-local-secret\n"
        "LOCAL_REDIS_HOST=localhost\n"
        "LOCAL_REDIS_PASSWORD=redis-local-secret\n"
        "NATS_URL=nats://localhost:4222\n"
        "NEO4J_URI=neo4j://localhost:7687\n"
        "NEO4J_USER=neo4j\n"
        "NEO4J_PASSWORD=neo4j-local-secret\n"
        "MINIO_ENDPOINT=localhost:9000\n"
        "MINIO_ACCESS_KEY=admin\n"
        "MINIO_SECRET_KEY=minio-local-secret\n"
        "N8N_API_URL=http://127.0.0.1:5678\n"
        "N8N_API_KEY=n8n-local-secret\n",
        encoding="utf-8",
    )
    store = LocalEnvStore(env_path)

    public, present, warning = store.public_profile()
    resolved = store.resolve_secrets(public)

    assert store.exists is False
    assert store.defaults_exists is True
    assert warning is None
    assert public.postgres.host == "127.0.0.1"
    assert public.postgres.database == "openontology"
    assert public.postgres.username == "postgres"
    assert public.postgres.password == ""
    assert public.redis.host == "localhost"
    assert public.nats.host == "localhost"
    assert public.nats.port == 4222
    assert public.nats.token == ""
    assert present["nats.token"] is False
    assert public.neo4j.uri == "neo4j://localhost:7687"
    assert public.minio.endpoint == "localhost:9000"
    assert public.minio.access_key == ""
    assert present["postgres.password"] is True
    assert present["minio.access_key"] is True
    assert resolved.postgres.password == "postgres-local-secret"
    assert resolved.n8n.api_key == "n8n-local-secret"
