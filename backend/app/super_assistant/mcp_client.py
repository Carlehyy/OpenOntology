from __future__ import annotations

from contextlib import asynccontextmanager
import ipaddress
import json
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.shared.config import settings
from app.shared.encryption import decrypt, encrypt


class McpClientError(ValueError):
    pass


_MAX_TOOL_RESULT_BYTES = 256 * 1024


def _sse_http_client_factory(*, headers: dict[str, str] | None = None,
                             timeout=None, auth=None):
    """Build the SSE client with redirects disabled.

    The MCP SDK's default factory enables redirects.  SSE endpoints can carry
    API-key headers and may emit a cross-host 30x before the first event, so
    using that default would bypass the URL/SSRF validation performed here.
    """
    import httpx

    return httpx.AsyncClient(
        headers=headers, timeout=timeout, auth=auth, follow_redirects=False,
    )


def _error_message(exc: BaseException) -> str:
    """Unwrap AnyIO task groups so connection failures stay actionable."""
    if isinstance(exc, BaseExceptionGroup):
        messages = [_error_message(child) for child in exc.exceptions]
        messages = [message for message in messages if message]
        return "; ".join(dict.fromkeys(messages)) or exc.__class__.__name__
    return str(exc).strip() or exc.__class__.__name__


def _resolved_addresses(hostname: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return {ipaddress.ip_address(hostname)}
    except ValueError:
        pass
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise McpClientError(f"MCP 目标 {hostname} 无法解析") from exc
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for record in records:
        try:
            addresses.add(ipaddress.ip_address(record[4][0].split("%", 1)[0]))
        except ValueError:
            continue
    if not addresses:
        raise McpClientError(f"MCP 目标 {hostname} 没有可用的 IP 地址")
    return addresses


def validate_mcp_url(url: str) -> str:
    value = (url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise McpClientError("MCP URL 必须是 HTTP/HTTPS 绝对地址")
    if parsed.username or parsed.password:
        raise McpClientError("MCP URL 不能内嵌账号或密码")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise McpClientError("MCP URL 端口无效") from exc
    hostname = parsed.hostname.lower().rstrip(".")
    addresses = _resolved_addresses(hostname, port)
    if settings.environment.strip().lower() not in {"development", "dev", "test", "local"}:
        blocked = sorted(str(address) for address in addresses if not address.is_global)
        if blocked:
            raise McpClientError(
                f"MCP 目标 {hostname} 解析到非公网地址 {', '.join(blocked)}，已拒绝连接"
            )
    return value


def _encrypt_mapping(values: dict[str, str]) -> tuple[str | None, list[str]]:
    if not values:
        return None, []
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True)
    return encrypt(payload), sorted(values.keys(), key=str.lower)


def _decrypt_mapping(ciphertext: str | None, label: str) -> dict[str, str]:
    if not ciphertext:
        return {}
    try:
        value = json.loads(decrypt(ciphertext))
    except Exception as exc:  # cryptography/json errors share a safe public message
        raise McpClientError(f"MCP {label}无法解密，请重新保存配置") from exc
    if not isinstance(value, dict):
        raise McpClientError(f"MCP {label}配置格式无效")
    return {str(key): str(item) for key, item in value.items()}


def encrypt_headers(headers: dict[str, str]) -> tuple[str | None, list[str]]:
    return _encrypt_mapping(headers)


def decrypt_headers(ciphertext: str | None) -> dict[str, str]:
    return _decrypt_mapping(ciphertext, "请求头")


def encrypt_env(env: dict[str, str]) -> tuple[str | None, list[str]]:
    return _encrypt_mapping(env)


def decrypt_env(ciphertext: str | None) -> dict[str, str]:
    return _decrypt_mapping(ciphertext, "环境变量")


def normalize_connection(*, transport: str, url: str = "", command: str | None = None,
                         args: list[str] | None = None) -> tuple[str, str, str | None, list[str]]:
    """Validate a connection and unwrap the common npx mcp-remote bridge."""
    normalized = (transport or "streamable_http").strip().lower().replace("-", "_").replace(" ", "_")
    arguments = [str(item) for item in (args or [])]
    executable = (command or "").strip() or None
    if normalized == "stdio":
        remote_index = next((
            index for index, item in enumerate(arguments)
            if item == "mcp-remote" or item.startswith("mcp-remote@")
        ), None)
        if executable and Path(executable).name in {"npx", "npx.cmd"} and remote_index is not None:
            remote_url = next((item for item in arguments[remote_index + 1:] if item.startswith(("http://", "https://"))), "")
            if not remote_url:
                raise McpClientError("mcp-remote 配置缺少远程 MCP URL")
            return "streamable_http", validate_mcp_url(remote_url), None, []
        if not executable or "\x00" in executable:
            raise McpClientError("stdio MCP 必须配置 command")
        return "stdio", "", executable, arguments
    if normalized not in {"sse", "streamable_http"}:
        raise McpClientError("传输方式必须是 stdio、sse 或 streamable_http")
    return normalized, validate_mcp_url(url), None, []


def _validate_stdio_runtime(command: str) -> None:
    if not settings.super_assistant_mcp_stdio_enabled:
        raise McpClientError(
            "stdio MCP 在当前部署中未启用；需由管理员开启 "
            "SUPER_ASSISTANT_MCP_STDIO_ENABLED"
        )
    allowed = {
        item.strip() for item in settings.super_assistant_mcp_stdio_allowed_commands.split(",") if item.strip()
    }
    if command not in allowed and Path(command).name not in allowed:
        raise McpClientError(
            f"stdio command {command!r} 未进入 SUPER_ASSISTANT_MCP_STDIO_ALLOWED_COMMANDS"
        )


def namespaced_tool_name(server_name: str, tool_name: str) -> str:
    import hashlib

    server = re.sub(r"[^a-zA-Z0-9_-]", "_", server_name)
    tool = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_name)
    raw = f"mcp__{server}__{tool}"
    if len(raw) <= 64:
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"{raw[:55]}_{digest}"


def _tool_manifest_item(tool: Any) -> dict[str, Any]:
    if hasattr(tool, "model_dump"):
        raw = tool.model_dump(by_alias=True, exclude_none=True)
    else:
        raw = {
            "name": getattr(tool, "name", ""),
            "description": getattr(tool, "description", "") or "",
            "inputSchema": getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {},
        }
    schema = raw.get("inputSchema") or raw.get("input_schema") or {"type": "object", "properties": {}}
    return {
        "name": str(raw.get("name") or ""),
        "description": str(raw.get("description") or ""),
        "input_schema": schema,
    }


@asynccontextmanager
async def _client_session(*, transport: str, url: str, headers: dict[str, str],
                          command: str | None, args: list[str], env: dict[str, str]):
    try:
        import httpx
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise McpClientError("Python MCP SDK 未安装，请通过 uv 同步后端依赖") from exc

    if transport == "stdio":
        if not command:
            raise McpClientError("stdio MCP 缺少 command")
        _validate_stdio_runtime(command)
        params = StdioServerParameters(command=command, args=args, env=env or None)
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
        return
    valid_url = validate_mcp_url(url)
    if transport == "sse":
        async with sse_client(
            valid_url, headers=headers, timeout=20, sse_read_timeout=120,
            httpx_client_factory=_sse_http_client_factory,
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
        return
    timeout = httpx.Timeout(connect=20, read=120, write=60, pool=20)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False) as http_client:
        async with streamable_http_client(
            valid_url,
            http_client=http_client,
            terminate_on_close=True,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


async def discover_tools(*, transport: str, url: str, headers: dict[str, str],
                         command: str | None = None, args: list[str] | None = None,
                         env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    try:
        async with _client_session(
            transport=transport, url=url, headers=headers, command=command,
            args=args or [], env=env or {},
        ) as session:
            result = await session.list_tools()
            return [_tool_manifest_item(tool) for tool in result.tools]
    except McpClientError:
        raise
    except Exception as exc:
        raise McpClientError(f"无法连接 MCP Server: {_error_message(exc)}") from exc


async def call_tool(*, transport: str, url: str, headers: dict[str, str], tool_name: str,
                    arguments: dict[str, Any], command: str | None = None,
                    args: list[str] | None = None, env: dict[str, str] | None = None) -> str:
    from app.shared import perf_spans

    span_target = (
        perf_spans.http_target(url) if transport != "stdio" else f"stdio:{command or ''}"
    )
    span = perf_spans.begin_span(
        "http", name=f"MCP {tool_name}", target=span_target)
    status = "success"
    try:
        async with _client_session(
            transport=transport, url=url, headers=headers, command=command,
            args=args or [], env=env or {},
        ) as session:
            result = await session.call_tool(tool_name, arguments=arguments)
            if hasattr(result, "model_dump_json"):
                serialized = result.model_dump_json(by_alias=True, exclude_none=True)
            else:
                serialized = json.dumps(result, ensure_ascii=False, default=str)
            if len(serialized.encode("utf-8")) > _MAX_TOOL_RESULT_BYTES:
                raise McpClientError("MCP 工具响应超过 256 KiB 上限")
            return serialized
    except McpClientError:
        status = "error"
        raise
    except Exception as exc:
        status = "error"
        raise McpClientError(f"MCP 工具 {tool_name} 执行失败: {_error_message(exc)}") from exc
    finally:
        perf_spans.end_span(span, status=status)
