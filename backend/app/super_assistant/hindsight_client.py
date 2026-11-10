"""Hindsight 记忆服务的固定契约 HTTP 客户端（无 fallback）。

只实现已在上游源码核实的端点（hindsight-api http.py）：

- ``GET    /health``：readiness 探测（DB 可达才 200，否则 503）；
- ``PUT    /v1/default/banks/{bank_id}``：幂等创建/更新 bank；
- ``PATCH  /v1/default/banks/{bank_id}/config``：bank 级层级配置覆盖
  （llm_model 别名、retain_strategies 等）；
- ``POST   /v1/default/banks/{bank_id}/memories``：retain，``async=true`` +
  客户端提供 ``operation_id``（UUID，重放返回原 operation 不重复入队，
  复用他人 id 返回 409）；
- ``POST   /v1/default/banks/{bank_id}/memories/recall``：召回；
- ``GET    /v1/default/banks/{bank_id}/operations/{operation_id}``：operation
  状态（pending/processing/completed/failed/cancelled/not_found）；
- ``GET    /v1/default/banks/{bank_id}/documents``：文档列表（q 子串过滤，
  用于删除后的核实）；
- ``DELETE /v1/default/banks/{bank_id}/documents/{document_id}``：删除文档
  及其全部记忆单元与关联 observation（引擎双扫已证）。

不依赖未证实的端点（如 PATCH memories/invalidated）。错误 detail 只含
状态码级受控代码，不回显上游正文。超时按调用方传入：召回路径用平台
hindsight_timeout_seconds（默认 2s 总预算），worker 路径独立设置，互不
复用。同步入口内运行可取消的异步 HTTP 请求，避免后台线程超时后遗留
请求；测试可注入 transport。
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx


class HindsightError(Exception):
    """上游返回非 2xx；detail 为受控代码，不包含上游正文。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"hindsight HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class HindsightUnavailableError(Exception):
    """网络层不可达/超时/5xx/429：调用方按瞬态失败处理（可重试）。"""


class HindsightNotFoundError(HindsightError):
    """404。"""


class HindsightConflictError(HindsightError):
    """409：operation_id 已属于其他 operation，需 GET 核实。"""


def _raise_for_status(response: httpx.Response) -> None:
    code = response.status_code
    if code >= 500 or code == 429:
        raise HindsightUnavailableError(f"hindsight HTTP {code}")
    if code == 409:
        raise HindsightConflictError(code, "operation_conflict")
    if code == 404:
        raise HindsightNotFoundError(code, "not_found")
    if code >= 400:
        raise HindsightError(code, f"client_error_{code}")


_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None


def install_test_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试注入 MockTransport（生产路径不调用）。"""
    global _TEST_TRANSPORT
    _TEST_TRANSPORT = transport


class HindsightClient:
    """同步业务入口：路径、鉴权和包含响应正文读取的总超时。"""

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        if not base_url:
            raise ValueError("hindsight base_url 不能为空")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        budget = timeout if timeout is not None else self._timeout

        async def send() -> httpx.Response:
            async with asyncio.timeout(budget):
                async with httpx.AsyncClient(transport=_TEST_TRANSPORT) as client:
                    return await client.request(
                        method,
                        f"{self._base_url}{path}",
                        json=json,
                        params=params,
                        headers=headers,
                        timeout=budget,
                    )

        try:
            return asyncio.run(send())
        except (httpx.HTTPError, TimeoutError) as exc:
            raise HindsightUnavailableError(type(exc).__name__) from exc

    def _json(self, response: httpx.Response) -> Any:
        _raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise HindsightUnavailableError("invalid_json") from exc
        if not isinstance(payload, dict):
            raise HindsightUnavailableError("invalid_response")
        return payload

    # --- 探活（readiness：DB 可达才 200）---

    def probe(self, *, timeout: float = 2.0) -> dict:
        return self._json(self._request("GET", "/health", timeout=timeout))

    # --- bank ---

    def ensure_bank(self, bank_id: str, body: dict) -> dict:
        return self._json(self._request("PUT", f"/v1/default/banks/{bank_id}", json=body))

    def update_bank_config(self, bank_id: str, updates: dict) -> dict:
        return self._json(
            self._request(
                "PATCH", f"/v1/default/banks/{bank_id}/config",
                json={"updates": updates},
            )
        )

    # --- retain / operations ---

    def retain_async(self, bank_id: str, items: list[dict], operation_id: str) -> dict:
        """async retain；operation_id 必须是 UUID（上游强校验）。"""
        return self._json(
            self._request(
                "POST", f"/v1/default/banks/{bank_id}/memories",
                json={"items": items, "async": True, "operation_id": operation_id},
            )
        )

    def get_operation(
        self, bank_id: str, operation_id: str, *, timeout: float | None = None,
    ) -> dict:
        return self._json(
            self._request(
                "GET", f"/v1/default/banks/{bank_id}/operations/{operation_id}",
                timeout=timeout,
            )
        )

    # --- recall ---

    def recall(self, bank_id: str, payload: dict) -> dict:
        result = self._json(
            self._request(
                "POST", f"/v1/default/banks/{bank_id}/memories/recall", json=payload,
            )
        )
        items = result.get("results")
        facts = result.get("source_facts") or {}
        if (not isinstance(items, list) or not all(isinstance(item, dict) for item in items)
                or not isinstance(facts, dict)
                or not all(isinstance(fact, dict) for fact in facts.values())):
            raise HindsightUnavailableError("invalid_recall_response")
        return result

    # --- documents ---

    def list_documents(
        self,
        bank_id: str,
        *,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if q is not None:
            params["q"] = q
        result = self._json(
            self._request("GET", f"/v1/default/banks/{bank_id}/documents", params=params),
        )
        items = result.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise HindsightUnavailableError("invalid_documents_response")
        return result

    def delete_document(self, bank_id: str, document_id: str) -> dict:
        return self._json(
            self._request(
                "DELETE", f"/v1/default/banks/{bank_id}/documents/{document_id}",
            )
        )
