from __future__ import annotations

import json
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg2
import redis as redis_client
import urllib3
from minio import Minio
from neo4j import GraphDatabase, Query

from .http_transport import loopback_httpx_mounts
from .models import ConfigProfile


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    message: str
    detail: str
    duration_ms: int


def run_probe(service: str, profile: ConfigProfile) -> ProbeResult:
    probe = PROBES.get(service)
    if probe is None:
        return ProbeResult(False, "未知的测试项目", "请刷新页面后重试", 0)
    started = time.monotonic()
    try:
        message, detail = probe(profile)
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        return ProbeResult(
            ok=False,
            message="连接失败",
            detail=_safe_error(exc, profile),
            duration_ms=duration_ms,
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    return ProbeResult(True, message, detail, duration_ms)


def probe_postgres(profile: ConfigProfile) -> tuple[str, str]:
    config = profile.postgres
    connection = psycopg2.connect(
        host=config.host,
        port=config.port,
        dbname=config.database,
        user=config.username,
        password=config.password,
        sslmode=config.ssl_mode,
        connect_timeout=6,
        options="-c statement_timeout=6000",
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
            if row != (1,):
                raise RuntimeError("数据库没有返回预期结果")
    finally:
        connection.close()
    return "PostgreSQL 连接正常", "已登录数据库并成功执行只读查询"


def probe_redis(profile: ConfigProfile) -> tuple[str, str]:
    config = profile.redis
    client = redis_client.Redis(
        host=config.host,
        port=config.port,
        db=config.database,
        username=config.username or None,
        password=config.password,
        ssl=config.use_tls,
        socket_connect_timeout=6,
        socket_timeout=6,
        decode_responses=True,
    )
    try:
        if client.ping() is not True:
            raise RuntimeError("Redis 没有返回 PONG")
    finally:
        client.close()
    return "Redis 连接正常", "Redis 缓存连接已通过 PING 测试"


def probe_nats(profile: ConfigProfile) -> tuple[str, str]:
    """用裸 socket 完成 NATS 握手，避免为配置中心引入 nats.py 依赖。"""
    config = profile.nats
    with socket.create_connection((config.host, config.port), timeout=3) as connection:
        connection.settimeout(3)
        info_line = _read_nats_line(connection)
        if not info_line.startswith("INFO "):
            raise RuntimeError("对端不是 NATS 服务，未收到 INFO 握手")
        try:
            info = json.loads(info_line[len("INFO "):])
        except ValueError as exc:
            raise RuntimeError("NATS INFO 握手无法解析") from exc
        if info.get("jetstream") is not True:
            raise RuntimeError("NATS 需要以 -js/--jetstream 启动才能承载流水线任务")
        connect_options: dict[str, object] = {"verbose": False}
        if config.token or info.get("auth_required"):
            connect_options["auth_token"] = config.token
        payload = json.dumps(connect_options, separators=(",", ":"))
        connection.sendall(f"CONNECT {payload}\r\nPING\r\n".encode("utf-8"))
        reply = _read_nats_line(connection)
    if reply != "PONG":
        raise RuntimeError("NATS 没有返回 PONG，请检查连接令牌是否正确")
    return "NATS 连接正常", "流水线任务派发通道已通过 PING 测试，JetStream 已启用"


def _read_nats_line(connection: socket.socket) -> str:
    buffer = bytearray()
    while not buffer.endswith(b"\n"):
        chunk = connection.recv(4096)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > 65536:
            raise RuntimeError("NATS 握手响应超出预期长度")
    return buffer.decode("utf-8", "replace").strip()


def probe_neo4j(profile: ConfigProfile) -> tuple[str, str]:
    config = profile.neo4j
    driver = GraphDatabase.driver(
        config.uri,
        auth=(config.username, config.password),
        connection_timeout=6,
        connection_acquisition_timeout=6,
        max_transaction_retry_time=0,
        liveness_check_timeout=6,
    )
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            value = session.run(
                Query("RETURN 1 AS ok", timeout=6)
            ).single()
            if value is None or value["ok"] != 1:
                raise RuntimeError("Neo4j 没有返回预期结果")
    finally:
        driver.close()
    return "Neo4j 连接正常", "已完成身份验证和只读图查询"


def probe_minio(profile: ConfigProfile) -> tuple[str, str]:
    config = profile.minio
    http = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=6, read=6),
        retries=urllib3.Retry(
            total=0,
            connect=0,
            read=0,
            redirect=0,
            status=0,
        ),
    )
    try:
        client = Minio(
            config.endpoint,
            access_key=config.access_key,
            secret_key=config.secret_key,
            secure=config.secure,
            http_client=http,
        )
        buckets = client.list_buckets()
    finally:
        http.clear()
    return "MinIO 连接正常", f"凭据可读取存储桶列表，当前可见 {len(buckets)} 个桶"


def probe_browser(profile: ConfigProfile) -> tuple[str, str]:
    url = f"{profile.browser.cdp_url.rstrip('/')}/json/version"
    with httpx.Client(
        timeout=6,
        follow_redirects=False,
        mounts=loopback_httpx_mounts(url),
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    websocket_url = payload.get("webSocketDebuggerUrl")
    if not isinstance(websocket_url, str) or not websocket_url.startswith(("ws://", "wss://")):
        raise RuntimeError("CDP 返回中缺少 WebSocket 调试地址")
    browser_name = str(payload.get("Browser") or "Chromium")
    return "浏览器控制接口正常", f"已识别 {browser_name}"


def probe_n8n(profile: ConfigProfile) -> tuple[str, str]:
    config = profile.n8n
    url = f"{config.api_url.rstrip('/')}/api/v1/workflows"
    with httpx.Client(
        timeout=config.timeout_seconds,
        follow_redirects=False,
        mounts=loopback_httpx_mounts(url),
    ) as client:
        response = client.get(
            url,
            params={"limit": 1},
            headers={"X-N8N-API-KEY": config.api_key},
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, (dict, list)):
        raise RuntimeError("n8n 返回格式无法识别")
    return "n8n 连接正常", "API Key 可读取工作流列表"


_WEKNORA_ALLOWED_TOOLS = frozenset({
    "list_knowledge_bases",
    "search_knowledge",
    "grep_chunks",
    "list_documents",
    "read_document",
    "wiki_search",
    "wiki_read_page",
    "wiki_index",
})
_WEKNORA_REQUIRED_TOOLS = frozenset({
    "list_knowledge_bases",
    "search_knowledge",
    "read_document",
})


def _normalize_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise RuntimeError("WeKnora origin 格式非法") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query or parsed.fragment
        or any(char.isspace() for char in value)
    ):
        raise RuntimeError("WeKnora origin 格式非法")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{host}{suffix}"


def _validate_weknora_state(state: object, expected_origin: str) -> dict:
    """Strict schema/origin/tool validation for the bootstrap-owned snapshot."""
    if not isinstance(state, dict):
        raise RuntimeError("WeKnora 集成状态文件格式无法识别")
    if state.get("version") != 1:
        raise RuntimeError("WeKnora 集成状态文件版本不是 1")
    if state.get("verified") is not True:
        raise RuntimeError(
            "WeKnora 集成状态未通过 bootstrap --verify（verified 不是 true）"
        )
    stored_origin = state.get("base_url")
    if (
        not isinstance(stored_origin, str)
        or _normalize_origin(stored_origin) != _normalize_origin(expected_origin)
    ):
        raise RuntimeError(
            "WeKnora 集成状态 origin 与 WEKNORA_STATE_ORIGIN 不一致；"
            "不要混用不同网络口径的 state"
        )
    endpoint = state.get("mcp_endpoint_id")
    if not isinstance(endpoint, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", endpoint
    ):
        raise RuntimeError("WeKnora 集成状态 mcp_endpoint_id 格式非法")
    token = state.get("mcp_token")
    if not isinstance(token, str) or not re.fullmatch(
        r"mcp_[A-Za-z0-9_-]{16,256}", token
    ):
        raise RuntimeError("WeKnora 集成状态 mcp_token 格式非法")
    knowledge_base_id = state.get("knowledge_base_id")
    if not isinstance(knowledge_base_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", knowledge_base_id
    ):
        raise RuntimeError("WeKnora 集成状态 knowledge_base_id 格式非法")
    knowledge_base_name = state.get("knowledge_base_name")
    if not isinstance(knowledge_base_name, str) or not knowledge_base_name.strip():
        raise RuntimeError("WeKnora 集成状态 knowledge_base_name 为空")
    tools = state.get("tools")
    if not isinstance(tools, list) or not tools:
        raise RuntimeError(
            "WeKnora 集成状态没有 MCP 工具清单；请运行 bootstrap --verify"
        )
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            raise RuntimeError("WeKnora MCP 工具条目格式非法")
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("WeKnora MCP 工具缺少 name")
        if name not in _WEKNORA_ALLOWED_TOOLS:
            raise RuntimeError(f"WeKnora MCP 工具不在只读白名单: {name}")
        if name in names:
            raise RuntimeError(f"WeKnora MCP 工具重复: {name}")
        names.add(name)
        if not isinstance(tool.get("description"), str):
            raise RuntimeError("WeKnora MCP 工具 description 格式非法")
        input_schema = tool.get("input_schema")
        if (
            not isinstance(input_schema, dict)
            or input_schema.get("type") != "object"
        ):
            raise RuntimeError(
                "WeKnora MCP 工具 input_schema 必须是 type=object"
            )
    missing = sorted(_WEKNORA_REQUIRED_TOOLS - names)
    if missing:
        raise RuntimeError(
            "WeKnora 集成状态缺少必需只读工具: " + ", ".join(missing)
        )
    return state


def probe_weknora(profile: ConfigProfile) -> tuple[str, str]:
    config = profile.weknora
    url = f"{config.base_url.rstrip('/')}/api/v1/auth/config"
    with httpx.Client(
        timeout=config.timeout_seconds,
        follow_redirects=False,
        mounts=loopback_httpx_mounts(url),
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("WeKnora 认证配置返回格式无法识别")
    registration_mode = str(payload.get("registration_mode") or "")
    if not registration_mode:
        raise RuntimeError("WeKnora 认证配置缺少 registration_mode")
    if registration_mode == "self_serve":
        raise RuntimeError(
            "WeKnora 公网入口仍在允许自助注册；请先完成 bootstrap --verify，"
            "由正式系统管理员写入 invite_only 后再重试"
        )

    state_path = Path(config.state_path)
    if not state_path.is_absolute():
        state_path = Path(__file__).resolve().parents[2] / state_path
    if not state_path.is_file():
        raise RuntimeError(
            "WeKnora 尚未初始化：缺少集成状态文件 "
            f"{config.state_path}。Docker 部署会在公网网关启动前自动运行 "
            "bootstrap；本地源码运行需先启动 docker-compose.local.yml 中的 "
            "WeKnora，并执行 weknora-bootstrap 初始化与 --verify。"
        )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("WeKnora 集成状态文件无法读取或不是有效 JSON") from exc
    state = _validate_weknora_state(state, config.state_origin)
    return (
        "WeKnora 集成元数据通过，后端启动再验证 MCP",
        "仅校验 app HTTP 认证配置与 verified state 元数据；"
        "state origin {origin}，知识库 {name}，记录 {count} 个只读工具，"
        "注册模式 {mode}。真实 MCP 调用/RAG 由 backend 启动 probe 门禁".format(
            origin=state.get("base_url"),
            name=state.get("knowledge_base_name"),
            count=len(state["tools"]),
            mode=registration_mode,
        ),
    )


PROBES: dict[str, Callable[[ConfigProfile], tuple[str, str]]] = {
    "postgres": probe_postgres,
    "redis": probe_redis,
    "nats": probe_nats,
    "neo4j": probe_neo4j,
    "minio": probe_minio,
    "browser": probe_browser,
    "n8n": probe_n8n,
    "weknora": probe_weknora,
}


def _safe_error(exc: Exception, profile: ConfigProfile) -> str:
    raw = str(exc).strip() or exc.__class__.__name__
    for secret in _secret_values(profile):
        if secret:
            raw = raw.replace(secret, "***")
    raw = re.sub(r"://([^/@\s]+)@", "://***@", raw)
    raw = re.sub(r"password\s*=\s*[^,\s]+", "password=***", raw, flags=re.IGNORECASE)
    if len(raw) > 400:
        raw = raw[:397] + "..."
    return raw


def _secret_values(profile: ConfigProfile) -> list[str]:
    return [
        profile.platform.first_admin_password,
        profile.platform.secret_key,
        profile.platform.encryption_key,
        profile.postgres.password,
        profile.redis.password,
        profile.nats.token,
        profile.neo4j.password,
        profile.minio.access_key,
        profile.minio.secret_key,
        profile.n8n.api_key,
        profile.weknora.chat_api_key,
        profile.weknora.embedding_api_key,
        profile.advanced.api_hub_system_mcp_token,
        profile.advanced.api_hub_internal_proxy_token,
    ]
