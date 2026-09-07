from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from dotenv import dotenv_values

from .models import (
    AdvancedConfig,
    BrowserConfig,
    ConfigProfile,
    MinioConfig,
    N8nConfig,
    NatsConfig,
    Neo4jConfig,
    PlatformConfig,
    PostgresConfig,
    RedisConfig,
    default_profile,
)


SECRET_FIELDS: dict[str, tuple[str, str]] = {
    "platform.first_admin_password": ("platform", "first_admin_password"),
    "platform.secret_key": ("platform", "secret_key"),
    "platform.encryption_key": ("platform", "encryption_key"),
    "postgres.password": ("postgres", "password"),
    "redis.password": ("redis", "password"),
    "nats.token": ("nats", "token"),
    "neo4j.password": ("neo4j", "password"),
    "minio.access_key": ("minio", "access_key"),
    "minio.secret_key": ("minio", "secret_key"),
    "n8n.api_key": ("n8n", "api_key"),
    "advanced.api_hub_system_mcp_token": (
        "advanced",
        "api_hub_system_mcp_token",
    ),
    "advanced.api_hub_internal_proxy_token": (
        "advanced",
        "api_hub_internal_proxy_token",
    ),
}

REQUIRED_SECRET_FIELDS = frozenset(
    {
        "platform.first_admin_password",
        "platform.secret_key",
        "platform.encryption_key",
        "postgres.password",
        "redis.password",
        "neo4j.password",
        "minio.access_key",
        "minio.secret_key",
        "n8n.api_key",
        "advanced.api_hub_system_mcp_token",
        "advanced.api_hub_internal_proxy_token",
    }
)


@dataclass(frozen=True)
class WriteResult:
    path: Path
    backup_path: Path | None


class LocalEnvStore:
    def __init__(self, path: Path, defaults_path: Path | None = None):
        self.path = path
        self.defaults_path = defaults_path or path.with_name("defaults.env")
        self._fresh_defaults = default_profile()

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @property
    def defaults_exists(self) -> bool:
        return self.defaults_path.is_file()

    def read_values(self) -> dict[str, str]:
        source = self.path if self.exists else self.defaults_path
        if not source.is_file():
            return {}
        return {
            key: str(value)
            for key, value in dotenv_values(source, interpolate=False).items()
            if value is not None
        }

    def load_profile(self) -> ConfigProfile:
        values = self.read_values()
        if not values:
            return self._fresh_defaults.model_copy(deep=True)

        defaults = self._fresh_defaults
        loading_defaults = not self.exists and self.defaults_exists
        database = _parse_database_url(values.get("DATABASE_URL", ""))
        redis = _parse_redis_url(values.get("REDIS_URL", ""))
        nats = _parse_nats_url(values.get("NATS_URL", ""))
        default_secret = (
            (lambda value: value) if loading_defaults else (lambda _value: "")
        )

        return ConfigProfile(
            platform=PlatformConfig(
                backend_host=values.get(
                    "LOCAL_BACKEND_HOST", defaults.platform.backend_host
                ),
                backend_port=_int_value(
                    values, "LOCAL_BACKEND_PORT", defaults.platform.backend_port
                ),
                frontend_host=values.get(
                    "LOCAL_FRONTEND_HOST", defaults.platform.frontend_host
                ),
                frontend_port=_int_value(
                    values, "LOCAL_FRONTEND_PORT", defaults.platform.frontend_port
                ),
                first_admin_user=values.get(
                    "FIRST_ADMIN_USER", defaults.platform.first_admin_user
                ),
                first_admin_password=values.get(
                    "FIRST_ADMIN_PASSWORD",
                    default_secret(defaults.platform.first_admin_password),
                ),
                secret_key=values.get(
                    "SECRET_KEY",
                    default_secret(defaults.platform.secret_key),
                ),
                encryption_key=values.get(
                    "ENCRYPTION_KEY",
                    default_secret(defaults.platform.encryption_key),
                ),
            ),
            postgres=PostgresConfig(
                host=values.get(
                    "LOCAL_POSTGRES_HOST",
                    database.get("host", defaults.postgres.host),
                ),
                port=_int_value(
                    values,
                    "LOCAL_POSTGRES_PORT",
                    int(database.get("port", defaults.postgres.port)),
                ),
                database=values.get(
                    "LOCAL_POSTGRES_DATABASE",
                    database.get("database", defaults.postgres.database),
                ),
                username=values.get(
                    "LOCAL_POSTGRES_USER",
                    database.get("username", defaults.postgres.username),
                ),
                password=values.get(
                    "LOCAL_POSTGRES_PASSWORD",
                    database.get("password", ""),
                ),
                ssl_mode=database.get("ssl_mode", defaults.postgres.ssl_mode),
            ),
            redis=RedisConfig(
                host=values.get(
                    "LOCAL_REDIS_HOST",
                    redis.get("host", defaults.redis.host),
                ),
                port=_int_value(
                    values,
                    "LOCAL_REDIS_PORT",
                    int(redis.get("port", defaults.redis.port)),
                ),
                database=_int_value(
                    values,
                    "LOCAL_REDIS_DATABASE",
                    int(redis.get("database", defaults.redis.database)),
                ),
                username=values.get(
                    "LOCAL_REDIS_USER",
                    redis.get("username", defaults.redis.username),
                ),
                password=values.get(
                    "LOCAL_REDIS_PASSWORD",
                    redis.get("password", ""),
                ),
                use_tls=redis.get("use_tls", defaults.redis.use_tls),
            ),
            nats=NatsConfig(
                host=nats.get("host", defaults.nats.host),
                port=int(nats.get("port", defaults.nats.port)),
                token=nats.get("token", ""),
            ),
            neo4j=Neo4jConfig(
                uri=values.get("NEO4J_URI", defaults.neo4j.uri),
                username=values.get("NEO4J_USER", defaults.neo4j.username),
                password=values.get("NEO4J_PASSWORD", ""),
            ),
            minio=MinioConfig(
                endpoint=values.get("MINIO_ENDPOINT", defaults.minio.endpoint),
                access_key=values.get("MINIO_ACCESS_KEY", ""),
                secret_key=values.get("MINIO_SECRET_KEY", ""),
                secure=_bool_value(
                    values, "MINIO_USE_SSL", defaults.minio.secure
                ),
            ),
            browser=BrowserConfig(
                cdp_url=values.get(
                    "STEWARD_BROWSER_CDP_URL", defaults.browser.cdp_url
                )
            ),
            n8n=N8nConfig(
                api_url=values.get("N8N_API_URL", defaults.n8n.api_url),
                api_key=values.get("N8N_API_KEY", ""),
                timeout_seconds=_int_value(
                    values,
                    "N8N_TIMEOUT_SECONDS",
                    defaults.n8n.timeout_seconds,
                ),
            ),
            advanced=AdvancedConfig(
                uploads_dir=values.get(
                    "UPLOADS_DIR", defaults.advanced.uploads_dir
                ),
                storage_local_dir=values.get(
                    "STORAGE_LOCAL_DIR", defaults.advanced.storage_local_dir
                ),
                api_hub_data_dir=values.get(
                    "API_HUB_DATA_DIR", defaults.advanced.api_hub_data_dir
                ),
                super_assistant_skill_root=values.get(
                    "SUPER_ASSISTANT_SKILL_ROOT",
                    defaults.advanced.super_assistant_skill_root,
                ),
                steward_workspace_root=values.get(
                    "STEWARD_WORKSPACE_ROOT",
                    defaults.advanced.steward_workspace_root,
                ),
                api_hub_system_mcp_token=values.get(
                    "API_HUB_SYSTEM_MCP_TOKEN",
                    default_secret(defaults.advanced.api_hub_system_mcp_token),
                ),
                api_hub_internal_proxy_token=values.get(
                    "API_HUB_INTERNAL_PROXY_TOKEN",
                    default_secret(
                        defaults.advanced.api_hub_internal_proxy_token
                    ),
                ),
            ),
        )

    def public_profile(
        self,
    ) -> tuple[ConfigProfile, dict[str, bool], str | None]:
        warning: str | None = None
        try:
            profile = self.load_profile()
        except (UnicodeError, ValueError):
            profile = default_profile()
            warning = (
                "现有本地配置无法安全读取，已加载全新的默认值。"
                "旧密码和密钥不会被沿用，请重新填写全部凭据后生成配置；"
                "原文件会备份为 .env.bak。"
            )
        payload = profile.model_dump()
        present: dict[str, bool] = {}
        has_saved_values = self.exists or self.defaults_exists
        for field, (section, key) in SECRET_FIELDS.items():
            present[field] = (
                has_saved_values
                and warning is None
                and bool(payload[section][key])
            )
            if has_saved_values and warning is None:
                payload[section][key] = ""
        return ConfigProfile.model_validate(payload), present, warning

    def _existing_profile_payload(self) -> dict[str, object]:
        if not self.exists and not self.defaults_exists:
            return {}
        try:
            return self.load_profile().model_dump()
        except (UnicodeError, ValueError):
            # An invalid existing file must never leak partial secrets or make
            # the repair path impossible. The user must re-enter credentials.
            return {}

    def resolve_secrets(self, submitted: ConfigProfile) -> ConfigProfile:
        payload = submitted.model_dump()
        existing = self._existing_profile_payload()
        missing: list[str] = []

        for field, (section, key) in SECRET_FIELDS.items():
            if payload[section][key]:
                continue
            old_value = existing.get(section, {}).get(key, "")
            if old_value:
                payload[section][key] = old_value
                continue
            if field in REQUIRED_SECRET_FIELDS:
                missing.append(field)

        if missing:
            labels = ", ".join(missing)
            raise ValueError(f"以下完整功能凭据尚未填写: {labels}")
        return ConfigProfile.model_validate(payload)

    def resolve_service_secrets(
        self,
        submitted: ConfigProfile,
        service: str,
    ) -> ConfigProfile:
        """Resolve only credentials required by one connectivity probe."""
        payload = submitted.model_dump()
        existing = self._existing_profile_payload()
        missing: list[str] = []

        for field, (section, key) in SECRET_FIELDS.items():
            if section != service:
                continue
            if payload[section][key]:
                continue
            old_value = existing.get(section, {}).get(key, "")
            if old_value:
                payload[section][key] = old_value
            elif field != "nats.token":
                # NATS 默认无认证，token 留空是合法配置而不是缺失凭据
                missing.append(field)

        if missing:
            raise ValueError(
                "当前测试所需凭据尚未填写: " + ", ".join(missing)
            )
        return ConfigProfile.model_validate(payload)

    def write(self, profile: ConfigProfile) -> WriteResult:
        resolved = self.resolve_secrets(profile)
        content = render_env(resolved)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None

        if self.path.exists():
            backup = self.path.with_name(".env.bak")
            shutil.copy2(self.path, backup)
            _restrict_permissions(backup)

        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".env.",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            _restrict_permissions(temporary_path)
            os.replace(temporary_path, self.path)
            _restrict_permissions(self.path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

        return WriteResult(path=self.path, backup_path=backup)


def render_env(profile: ConfigProfile) -> str:
    pg = profile.postgres
    redis = profile.redis
    backend_origin = (
        f"http://{_origin_host(profile.platform.backend_host)}:"
        f"{profile.platform.backend_port}"
    )
    frontend_origin = (
        f"http://{_origin_host(profile.platform.frontend_host)}:"
        f"{profile.platform.frontend_port}"
    )
    database_url = (
        "postgresql://"
        f"{quote(pg.username, safe='')}:{quote(pg.password, safe='')}@"
        f"{_url_host(pg.host)}:{pg.port}/{quote(pg.database, safe='')}"
        f"?sslmode={quote(pg.ssl_mode, safe='')}"
    )
    redis_scheme = "rediss" if redis.use_tls else "redis"
    redis_userinfo = (
        f"{quote(redis.username, safe='')}:{quote(redis.password, safe='')}"
        if redis.username
        else f":{quote(redis.password, safe='')}"
    )
    redis_url = (
        f"{redis_scheme}://{redis_userinfo}@{_url_host(redis.host)}:"
        f"{redis.port}/{redis.database}"
    )
    nats = profile.nats
    nats_userinfo = f"{quote(nats.token, safe='')}@" if nats.token else ""
    nats_url = f"nats://{nats_userinfo}{_url_host(nats.host)}:{nats.port}"

    sections: list[tuple[str, list[tuple[str, object]]]] = [
        (
            "本地配置中心标记。生产环境不会生成或依赖此文件",
            [
                ("LOCAL_CONFIG_SCHEMA_VERSION", "1"),
                ("LOCAL_CONFIG_MANAGED", True),
                ("ENVIRONMENT", "development"),
            ],
        ),
        (
            "前后端本地监听与代理。只监听本机，避免开发服务暴露到局域网",
            [
                ("LOCAL_BACKEND_HOST", profile.platform.backend_host),
                ("LOCAL_BACKEND_PORT", profile.platform.backend_port),
                ("LOCAL_FRONTEND_HOST", profile.platform.frontend_host),
                ("LOCAL_FRONTEND_PORT", profile.platform.frontend_port),
                ("APP_HOST", profile.platform.backend_host),
                ("APP_PORT", profile.platform.backend_port),
                ("VITE_API_PROXY_TARGET", backend_origin),
            ],
        ),
        (
            "平台身份与加密。已有数据库使用后不要随意更换密钥",
            [
                ("SECRET_KEY", profile.platform.secret_key),
                ("ENCRYPTION_KEY", profile.platform.encryption_key),
                ("FIRST_ADMIN_USER", profile.platform.first_admin_user),
                ("FIRST_ADMIN_PASSWORD", profile.platform.first_admin_password),
                ("ACCESS_TOKEN_EXPIRE_MINUTES", 1440),
                ("CORS_ALLOWED_ORIGINS", ""),
            ],
        ),
        (
            "PostgreSQL。完整本地模式不允许回退到 SQLite",
            [("DATABASE_URL", database_url)],
        ),
        (
            "Redis 与 Celery。请另外启动 Celery worker",
            [
                ("REDIS_PASSWORD", redis.password),
                ("REDIS_URL", redis_url),
            ],
        ),
        (
            "NATS 消息队列。流水线任务派发通道，需以 -js 启用 JetStream",
            [("NATS_URL", nats_url)],
        ),
        (
            "Neo4j 图数据库",
            [
                ("NEO4J_URI", profile.neo4j.uri),
                ("NEO4J_USER", profile.neo4j.username),
                ("NEO4J_PASSWORD", profile.neo4j.password),
            ],
        ),
        (
            "MinIO 对象存储。9000 通常是 API 端口，9001 通常是管理页面",
            [
                ("MINIO_ENDPOINT", profile.minio.endpoint),
                ("MINIO_ACCESS_KEY", profile.minio.access_key),
                ("MINIO_SECRET_KEY", profile.minio.secret_key),
                ("MINIO_USE_SSL", profile.minio.secure),
                ("STORAGE_LOCAL_DIR", profile.advanced.storage_local_dir),
            ],
        ),
        (
            "数据管家浏览器与项目相对数据目录",
            [
                ("STEWARD_BROWSER_CDP_URL", profile.browser.cdp_url),
                ("STEWARD_BROWSER_ALLOW_PRIVATE_NETWORKS", True),
                ("STEWARD_WORKSPACE_ROOT", profile.advanced.steward_workspace_root),
                ("UPLOADS_DIR", profile.advanced.uploads_dir),
                (
                    "SUPER_ASSISTANT_SKILL_ROOT",
                    profile.advanced.super_assistant_skill_root,
                ),
            ],
        ),
        (
            "n8n 启动配置。后端启动后加密写入平台数据库",
            [
                ("N8N_API_URL", profile.n8n.api_url),
                ("N8N_API_KEY", profile.n8n.api_key),
                ("N8N_TIMEOUT_SECONDS", profile.n8n.timeout_seconds),
            ],
        ),
        (
            "API Hub 本地数据与独立权限令牌",
            [
                ("API_HUB_DATA_DIR", profile.advanced.api_hub_data_dir),
                (
                    "API_HUB_SYSTEM_MCP_TOKEN",
                    profile.advanced.api_hub_system_mcp_token,
                ),
                (
                    "API_HUB_INTERNAL_PROXY_TOKEN",
                    profile.advanced.api_hub_internal_proxy_token,
                ),
            ],
        ),
        (
            "n8n 文件网关和浏览器可见地址",
            [
                (
                    "PIPELINE_FILE_GATEWAY_BASE_URL",
                    f"{backend_origin}/api/v2/file-transfer",
                ),
                ("PIPELINE_FILE_PUBLIC_APP_BASE_URL", frontend_origin),
                ("PIPELINE_FILE_PUBLIC_API_BASE_URL", backend_origin),
                (
                    "STEWARD_INTERNAL_PROXY_BASE_URL",
                    f"{backend_origin}/api-hub/internal/interfaces",
                ),
            ],
        ),
    ]

    output = [
        "# 由 OpenOntology 本地配置中心生成，请通过 config/start.bat 或 start.sh 修改",
        "# 文件编码为 UTF-8，配置值不会同步到 GitHub",
        "",
    ]
    for comment, items in sections:
        output.append(f"# {comment}")
        for key, value in items:
            output.append(f"{key}={_dotenv_value(value)}")
        output.append("")
    return "\n".join(output).rstrip() + "\n"


def _dotenv_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    raw = str(value)
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ValueError("配置值不能包含换行或空字符")
    if "${" in raw:
        raise ValueError("配置值不能包含 ${，以免环境变量解析产生歧义")
    return json.dumps(raw, ensure_ascii=False)


def _parse_database_url(value: str) -> dict[str, object]:
    if not value:
        return {}
    parsed = urlsplit(value)
    if not parsed.scheme.startswith("postgresql") and parsed.scheme != "postgres":
        return {}
    query = {}
    for item in parsed.query.split("&"):
        key, separator, raw = item.partition("=")
        if separator:
            query[key] = unquote(raw)
    return {
        "host": parsed.hostname or "",
        "port": parsed.port or 5432,
        "database": unquote(parsed.path.lstrip("/")),
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "ssl_mode": query.get("sslmode", "prefer"),
    }


def _parse_redis_url(value: str) -> dict[str, object]:
    if not value:
        return {}
    parsed = urlsplit(value)
    if parsed.scheme not in {"redis", "rediss"}:
        return {}
    try:
        database = int(parsed.path.lstrip("/") or 0)
    except ValueError:
        database = 0
    return {
        "host": parsed.hostname or "",
        "port": parsed.port or 6379,
        "database": database,
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "use_tls": parsed.scheme == "rediss",
    }


def _parse_nats_url(value: str) -> dict[str, object]:
    if not value:
        return {}
    parsed = urlsplit(value)
    if parsed.scheme != "nats":
        return {}
    # NATS 连接串的 userinfo 是单段 token，即 nats://<token>@host:port
    return {
        "host": parsed.hostname or "",
        "port": parsed.port or 4222,
        "token": unquote(parsed.username or ""),
    }


def _int_value(values: dict[str, str], key: str, default: int) -> int:
    try:
        return int(values.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _bool_value(values: dict[str, str], key: str, default: bool) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _origin_host(host: str) -> str:
    return _url_host(host)


def _restrict_permissions(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
