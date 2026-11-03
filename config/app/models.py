from __future__ import annotations

import ipaddress
import re
import secrets
import string
from base64 import b64decode
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


def _random_password(length: int = 20) -> str:
    """Return a shell and dotenv friendly password with mixed character types."""
    alphabet = string.ascii_letters + string.digits + "@#%+=_"
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(char.islower() for char in value)
            and any(char.isupper() for char in value)
            and any(char.isdigit() for char in value)
            and any(char in "@#%+=_" for char in value)
        ):
            return value


def _random_token(length: int = 48) -> str:
    return secrets.token_urlsafe(length)


def _fernet_key() -> str:
    from base64 import urlsafe_b64encode

    return urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PlatformConfig(StrictModel):
    backend_host: str = "127.0.0.1"
    backend_port: int = Field(default=8000, ge=1, le=65535)
    frontend_host: str = "127.0.0.1"
    frontend_port: int = Field(default=5173, ge=1, le=65535)
    first_admin_user: str = Field(default="admin", min_length=1, max_length=100)
    first_admin_password: str = Field(default="", min_length=0, max_length=512)
    secret_key: str = Field(default="", min_length=0, max_length=1024)
    encryption_key: str = Field(default="", min_length=0, max_length=1024)

    @model_validator(mode="after")
    def validate_platform(self) -> "PlatformConfig":
        if self.backend_port == self.frontend_port:
            raise ValueError("前端端口和后端端口不能相同")
        for label, host in (
            ("后端监听地址", self.backend_host),
            ("前端监听地址", self.frontend_host),
        ):
            try:
                address = ipaddress.ip_address(host)
            except ValueError as exc:
                if host != "localhost":
                    raise ValueError(f"{label}必须使用 127.0.0.1、::1 或 localhost") from exc
            else:
                if not address.is_loopback:
                    raise ValueError(f"{label}必须使用本机回环地址")
        if self.first_admin_password and len(self.first_admin_password) < 12:
            raise ValueError("首次管理员密码至少需要 12 个字符")
        if self.secret_key and len(self.secret_key) < 32:
            raise ValueError("平台签名密钥至少需要 32 个字符")
        if self.encryption_key:
            try:
                decoded = b64decode(
                    self.encryption_key.encode("ascii"),
                    altchars=b"-_",
                    validate=True,
                )
            except (ValueError, UnicodeEncodeError) as exc:
                raise ValueError("平台加密密钥必须是有效的 Fernet 密钥") from exc
            if len(decoded) != 32:
                raise ValueError("平台加密密钥必须是有效的 Fernet 密钥")
        return self


class PostgresConfig(StrictModel):
    host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(default="openontology", min_length=1, max_length=128)
    username: str = Field(default="openontology", min_length=1, max_length=128)
    password: str = Field(default="", min_length=0, max_length=512)
    ssl_mode: str = "prefer"

    @model_validator(mode="after")
    def validate_postgres(self) -> "PostgresConfig":
        if self.ssl_mode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise ValueError("PostgreSQL SSL 模式无效")
        return self


class RedisConfig(StrictModel):
    host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(default=6379, ge=1, le=65535)
    database: int = Field(default=0, ge=0, le=15)
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", min_length=0, max_length=512)
    use_tls: bool = False


class NatsConfig(StrictModel):
    host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(default=4222, ge=1, le=65535)
    # 本机 Docker 启动的 NATS 默认无认证，token 可留空
    token: str = Field(default="", min_length=0, max_length=512)


class Neo4jConfig(StrictModel):
    uri: str = Field(default="bolt://127.0.0.1:7687", min_length=1, max_length=500)
    username: str = Field(default="neo4j", min_length=1, max_length=128)
    password: str = Field(default="", min_length=0, max_length=512)

    @model_validator(mode="after")
    def validate_uri(self) -> "Neo4jConfig":
        parsed = urlsplit(self.uri)
        if parsed.scheme not in {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}:
            raise ValueError("Neo4j 地址必须使用 bolt 或 neo4j 协议")
        if not parsed.hostname:
            raise ValueError("Neo4j 地址缺少主机名")
        return self


class MinioConfig(StrictModel):
    endpoint: str = Field(default="127.0.0.1:9000", min_length=1, max_length=500)
    access_key: str = Field(default="", min_length=0, max_length=512)
    secret_key: str = Field(default="", min_length=0, max_length=512)
    secure: bool = False

    @model_validator(mode="after")
    def validate_endpoint(self) -> "MinioConfig":
        if "://" in self.endpoint:
            raise ValueError("MinIO 地址只填写主机和 API 端口，不要包含 http:// 或 https://")
        parsed = urlsplit(f"//{self.endpoint}")
        if not parsed.hostname or parsed.port is None:
            raise ValueError("MinIO 地址必须包含主机和 API 端口")
        if parsed.port == 9001:
            raise ValueError("9001 是 MinIO 管理页面端口，请填写 S3 API 端口，默认是 9000")
        return self


class BrowserConfig(StrictModel):
    cdp_url: str = Field(default="http://127.0.0.1:9222", min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_url(self) -> "BrowserConfig":
        _validate_http_origin(self.cdp_url, "Chromium CDP 地址")
        return self


class N8nConfig(StrictModel):
    api_url: str = Field(default="http://127.0.0.1:5678", min_length=1, max_length=500)
    api_key: str = Field(default="", min_length=0, max_length=2000)
    timeout_seconds: int = Field(default=30, ge=3, le=120)

    @model_validator(mode="after")
    def validate_url(self) -> "N8nConfig":
        _validate_http_origin(self.api_url, "n8n 地址")
        return self


class WeKnoraConfig(StrictModel):
    """Shared WeKnora knowledge base consumed by the super assistant.

    ``base_url`` is the app endpoint for a source-run backend (loopback
    ``http://127.0.0.1:18080``, because the public UI gateway deliberately
    rejects MCP). Docker deployments use the internal
    ``http://weknora-app:8080`` contract. ``state_origin`` is the deployment
    identity stored in state and must not be rewritten to the loopback alias.
    ``state_path`` points at the bootstrap-owned integration metadata (MCP
    token/endpoint and KB identity) and is read-only for the backend.
    Chat/embedding credentials are deployment inputs; there is no fallback
    provider.
    """

    # Source-run backend and the local config center use the loopback-only app
    # port (MCP is intentionally not proxied by the public UI gateway). Docker
    # deployments override this with http://weknora-app:8080.
    base_url: str = Field(
        default="http://127.0.0.1:18080", min_length=1, max_length=500
    )
    state_origin: str = Field(
        default="http://weknora-app:8080", min_length=1, max_length=500
    )
    web_url: str = Field(
        default="http://127.0.0.1:8089", min_length=1, max_length=500
    )
    state_path: str = Field(
        default="./runtime/weknora/integration.json",
        min_length=1,
        max_length=500,
    )
    knowledge_base_name: str = Field(default="共享知识库", min_length=1, max_length=200)
    timeout_seconds: int = Field(default=15, ge=3, le=60)
    chat_base_url: str = Field(default="", max_length=500)
    chat_api_key: str = Field(default="", max_length=2000)
    chat_model: str = Field(default="", max_length=200)
    embedding_base_url: str = Field(default="", max_length=500)
    embedding_api_key: str = Field(default="", max_length=2000)
    embedding_model: str = Field(default="", max_length=200)
    embedding_dimensions: int = Field(default=1024, ge=1, le=8192)

    @model_validator(mode="after")
    def validate_weknora(self) -> "WeKnoraConfig":
        _validate_http_origin(self.base_url, "WeKnora 内部地址")
        _validate_http_origin(self.state_origin, "WeKnora state 身份地址")
        _validate_http_origin(self.web_url, "WeKnora 公网地址")
        _validate_weknora_state_path(self.state_path)
        if self.chat_base_url:
            _validate_http_origin(
                self.chat_base_url, "WeKnora 聊天模型地址", allow_path=True
            )
        if self.embedding_base_url:
            _validate_http_origin(
                self.embedding_base_url, "WeKnora 向量模型地址", allow_path=True
            )
        return self


class AdvancedConfig(StrictModel):
    uploads_dir: str = "./runtime/uploads"
    storage_local_dir: str = "./runtime/object-storage"
    api_hub_data_dir: str = "./runtime/api-hub"
    super_assistant_skill_root: str = "./runtime/super-assistant/skills"
    steward_workspace_root: str = "./runtime/steward-sessions"
    api_hub_system_mcp_token: str = Field(default="", max_length=2000)
    api_hub_internal_proxy_token: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_paths_and_url(self) -> "AdvancedConfig":
        for label, raw in (
            ("上传目录", self.uploads_dir),
            ("历史本地对象目录", self.storage_local_dir),
            ("API Hub 数据目录", self.api_hub_data_dir),
            ("超级助手技能目录", self.super_assistant_skill_root),
            ("数据管家工作区", self.steward_workspace_root),
        ):
            _validate_relative_path(raw, label)
        return self


class ConfigProfile(StrictModel):
    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    nats: NatsConfig = Field(default_factory=NatsConfig)
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    minio: MinioConfig = Field(default_factory=MinioConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    n8n: N8nConfig = Field(default_factory=N8nConfig)
    weknora: WeKnoraConfig = Field(default_factory=WeKnoraConfig)
    advanced: AdvancedConfig = Field(default_factory=AdvancedConfig)

    @model_validator(mode="after")
    def validate_required_secrets(self) -> "ConfigProfile":
        required = (
            ("首次管理员密码", self.platform.first_admin_password),
            ("平台签名密钥", self.platform.secret_key),
            ("平台加密密钥", self.platform.encryption_key),
            ("PostgreSQL 密码", self.postgres.password),
            ("Redis 密码", self.redis.password),
            ("Neo4j 密码", self.neo4j.password),
            ("MinIO Access Key", self.minio.access_key),
            ("MinIO Secret Key", self.minio.secret_key),
            ("n8n API Key", self.n8n.api_key),
            ("WeKnora 聊天 API Key", self.weknora.chat_api_key),
            ("WeKnora 向量 API Key", self.weknora.embedding_api_key),
            ("API Hub System MCP Token", self.advanced.api_hub_system_mcp_token),
            ("API Hub Internal Proxy Token", self.advanced.api_hub_internal_proxy_token),
        )
        # Blank means "preserve the existing secret" and is resolved server-side.
        for label, value in required:
            if value and ("\n" in value or "\r" in value or "\x00" in value):
                raise ValueError(f"{label}不能包含换行或空字符")
            if "${" in value:
                raise ValueError(f"{label}不能包含 ${{，以免环境变量解析产生歧义")
        return self


def default_profile() -> ConfigProfile:
    return ConfigProfile(
        platform=PlatformConfig(
            first_admin_password=_random_password(),
            secret_key=_random_token(),
            encryption_key=_fernet_key(),
        ),
        postgres=PostgresConfig(
            host="127.0.0.1",
            port=5432,
            database="openontology",
            username="postgres",
            password="",
        ),
        redis=RedisConfig(host="localhost", port=6379, password=""),
        nats=NatsConfig(host="localhost", port=4222),
        neo4j=Neo4jConfig(
            uri="neo4j://localhost:7687",
            username="neo4j",
            password="",
        ),
        minio=MinioConfig(
            endpoint="localhost:9000",
            access_key="admin",
            secret_key="",
        ),
        n8n=N8nConfig(api_key=""),
        weknora=WeKnoraConfig(),
        advanced=AdvancedConfig(
            api_hub_system_mcp_token=_random_token(),
            api_hub_internal_proxy_token=_random_token(),
        ),
    )


def _validate_http_origin(value: str, label: str, *, allow_path: bool = False) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label}必须是完整的 http 或 https 地址")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label}不能在网址中包含账号或密码")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{label}不能包含查询参数或片段")
    if not allow_path and parsed.path not in {"", "/"}:
        raise ValueError(f"{label}只填写服务根地址，不要包含接口路径")


def _validate_weknora_state_path(value: str) -> None:
    normalized = value.replace("\\", "/")
    if normalized.startswith("/"):
        path = PurePosixPath(normalized)
        if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("WeKnora 初始化状态文件不能包含空目录、当前目录或上级目录")
        return
    _validate_relative_path(value, "WeKnora 初始化状态文件")


def _validate_relative_path(value: str, label: str) -> None:
    normalized = value.replace("\\", "/")
    if not normalized.startswith("./"):
        raise ValueError(f"{label}必须以 ./ 开头")
    path = PurePosixPath(normalized[2:])
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label}不能包含空目录、当前目录或上级目录")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"{label}不能使用 Windows 绝对路径")
