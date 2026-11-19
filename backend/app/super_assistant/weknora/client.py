"""Private administrator API for the pinned WeKnora service; secrets stay server side."""
from __future__ import annotations

import re
from typing import Any

import httpx

from app.shared.config import settings
from app.super_assistant.web_entry import origin
from app.super_assistant import mcp_client


class WeKnoraError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 503):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def service_origin() -> str:
    try:
        return origin(settings.weknora_base_url)
    except ValueError:
        raise WeKnoraError("WEKNORA_CONFIG_INVALID", "WeKnora 服务地址无效，请检查服务配置") from None


def endpoint_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise WeKnoraError("WEKNORA_ENDPOINT_INVALID", "WeKnora 端点标识无效", 422)
    return value


def connection_url(identifier: str) -> str:
    try:
        return mcp_client.validate_mcp_url(
            f"{service_origin()}/mcp/{endpoint_id(identifier)}", require_https=True,
        )
    except mcp_client.McpClientError:
        raise WeKnoraError("WEKNORA_NETWORK_SCOPE_INVALID", "WeKnora MCP 地址不在受信任服务范围内，请检查服务地址和内网配置") from None


class WeKnoraClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None):
        self.base_url = service_origin()
        self.http = httpx.Client(
            base_url=self.base_url, timeout=settings.weknora_timeout_seconds,
            follow_redirects=False, trust_env=False, transport=transport,
        )

    def __enter__(self):
        try:
            if not settings.weknora_admin_email.strip() or not settings.weknora_admin_password:
                raise WeKnoraError("WEKNORA_CREDENTIALS_MISSING", "WeKnora 管理员凭据未配置，请检查服务配置")
            payload = self._request("POST", "/api/v1/auth/login", json={
                "email": settings.weknora_admin_email,
                "password": settings.weknora_admin_password,
            })
            token = payload.get("token")
            user = payload.get("user")
            if not isinstance(token, str) or not token or not isinstance(user, dict) or user.get("is_system_admin") is not True:
                raise WeKnoraError("WEKNORA_LOGIN_REJECTED", "WeKnora 管理员登录失败，请检查管理员账号与密码")
            self.http.headers["Authorization"] = f"Bearer {token}"
            return self
        except Exception:
            self.http.close()
            raise

    def __exit__(self, *_):
        self.http.close()

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise WeKnoraError("WEKNORA_UNAVAILABLE", "WeKnora 暂时无法连接，请稍后重试") from None
        if response.status_code in {401, 403}:
            raise WeKnoraError("WEKNORA_LOGIN_REJECTED", "WeKnora 管理员授权失败，请检查管理员凭据")
        if response.status_code == 404:
            raise WeKnoraError("WEKNORA_ENDPOINT_MISSING", "WeKnora 端点已不存在，请选择其他端点或重新创建", 404)
        if not 200 <= response.status_code < 300:
            raise WeKnoraError("WEKNORA_REQUEST_REJECTED", "WeKnora 拒绝了本次操作，请在领域知识页面检查配置")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise WeKnoraError("WEKNORA_RESPONSE_INVALID", "WeKnora 返回了无效响应，请检查服务版本")
        return payload

    def list(self, resource: str) -> list[dict]:
        if resource not in {"models", "knowledge-bases", "mcp-endpoints"}:
            raise ValueError("unsupported WeKnora resource")
        data = self._request("GET", f"/api/v1/{resource}").get("data")
        if data is None:
            return []
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise WeKnoraError("WEKNORA_RESPONSE_INVALID", "WeKnora 目录格式无效，请检查服务版本")
        return data

    def endpoint(self, identifier: str) -> dict:
        return self._data(self._request("GET", f"/api/v1/mcp-endpoints/{endpoint_id(identifier)}"))

    def create(self, resource: str, body: dict) -> dict:
        if resource not in {"knowledge-bases", "mcp-endpoints"}:
            raise ValueError("unsupported WeKnora creation")
        return self._data(self._request("POST", f"/api/v1/{resource}", json=body))

    def rotate(self, identifier: str) -> dict:
        return self._data(self._request("POST", f"/api/v1/mcp-endpoints/{endpoint_id(identifier)}/rotate-token"))

    @staticmethod
    def _data(payload: dict) -> dict:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise WeKnoraError("WEKNORA_RESPONSE_INVALID", "WeKnora 返回了无效的资源信息")
        return data
