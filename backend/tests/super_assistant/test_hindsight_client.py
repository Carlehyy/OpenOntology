"""HindsightClient 固定契约测试（httpx.MockTransport，不发真实网络）。"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app.super_assistant.hindsight_client import (
    HindsightClient,
    HindsightConflictError,
    HindsightError,
    HindsightNotFoundError,
    HindsightUnavailableError,
    install_test_transport,
)


@pytest.fixture(autouse=True)
def restore_transport():
    yield
    install_test_transport(None)


def _client(handler) -> HindsightClient:
    install_test_transport(httpx.MockTransport(handler))
    return HindsightClient("http://hindsight", "secret-key", timeout=5.0)


def test_probe_uses_health_with_bearer():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "healthy"})

    assert _client(handler).probe(timeout=1.0) == {"status": "healthy"}
    request = seen[0]
    assert request.url.path == "/health"
    assert request.headers["Authorization"] == "Bearer secret-key"


def test_retain_async_contract():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "success": True, "bank_id": "b", "items_count": 1,
            "async": True, "operation_id": "op-1",
        })

    result = _client(handler).retain_async(
        "b", [{"content": "x", "document_id": "d"}], "0b55a3a2-9d1f-4a2e-8f0d-1c2d3e4f5a6b",
    )
    assert result["operation_id"] == "op-1"
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/default/banks/b/memories"
    body = json.loads(request.content)
    assert body["async"] is True
    assert body["operation_id"] == "0b55a3a2-9d1f-4a2e-8f0d-1c2d3e4f5a6b"
    assert body["items"][0]["document_id"] == "d"


def test_recall_and_documents_paths():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.url.path.endswith("/memories/recall"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"items": [], "total": 0})

    client = _client(handler)
    client.recall("b", {"query": "q", "max_tokens": 2000})
    client.list_documents("b", q="doc-1", limit=10)
    assert seen[0] == ("POST", "http://hindsight/v1/default/banks/b/memories/recall")
    assert seen[1][0] == "GET"
    assert "documents" in seen[1][1]
    assert "q=doc-1" in seen[1][1] and "limit=10" in seen[1][1]


def test_delete_document_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/default/banks/b/documents/doc-1"
        return httpx.Response(200, json={"success": True})

    assert _client(handler).delete_document("b", "doc-1")["success"] is True


def test_error_mapping_no_body_echo():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operations/op"):
            return httpx.Response(409, json={"detail": "SECRET operation belongs to other"})
        if request.url.path.endswith("/documents/missing"):
            return httpx.Response(404, text="SECRET not found")
        if request.url.path.endswith("/memories"):
            return httpx.Response(422, json={"detail": "SECRET validation"})
        return httpx.Response(500, text="SECRET internal")

    client = _client(handler)
    with pytest.raises(HindsightConflictError) as conflict:
        client.get_operation("b", "op")
    assert "SECRET" not in str(conflict.value)
    assert conflict.value.detail == "operation_conflict"

    with pytest.raises(HindsightNotFoundError) as missing:
        client.delete_document("b", "missing")
    assert missing.value.detail == "not_found"

    with pytest.raises(HindsightError) as client_error:
        client.retain_async("b", [], "0b55a3a2-9d1f-4a2e-8f0d-1c2d3e4f5a6b")
    assert "SECRET" not in str(client_error.value)

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="SECRET down")

    install_test_transport(httpx.MockTransport(unavailable))
    with pytest.raises(HindsightUnavailableError):
        client.probe()


def test_transport_error_maps_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(HindsightUnavailableError):
        _client(handler).probe()


def test_invalid_json_maps_to_unavailable():
    for content in (b"<html>not json</html>", b"null", b"[]"):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=content)

        with pytest.raises(HindsightUnavailableError):
            _client(handler).probe()


def test_per_call_timeout_overrides_default():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"status": "healthy"})

    _client(handler).probe(timeout=0.5)
    timeout = captured["timeout"]
    assert timeout["read"] == 0.5 and timeout["connect"] == 0.5

    closed = []

    class DrippingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.02)
                yield b" "

        async def aclose(self):
            closed.append(True)

    async def drip(request):
        return httpx.Response(200, stream=DrippingBody())

    client = _client(drip)
    started = time.monotonic()
    with pytest.raises(HindsightUnavailableError):
        client.probe(timeout=0.1)
    assert time.monotonic() - started < 0.5
    assert closed == [True]  # 总预算取消并关闭正文读取，没有后台请求遗留。


def test_operations_get_path_and_config_patch():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "PATCH":
            return httpx.Response(200, json={
                "bank_id": "b", "config": {}, "overrides": json.loads(request.content),
            })
        return httpx.Response(200, json={"operation_id": "op", "status": "completed"})

    client = _client(handler)
    assert client.get_operation("b", "op", timeout=3.0)["status"] == "completed"
    response = client.update_bank_config("b", {"llm_model": "oo-memory-u1"})
    assert response["overrides"]["updates"] == {"llm_model": "oo-memory-u1"}
    assert seen == [
        ("GET", "/v1/default/banks/b/operations/op"),
        ("PATCH", "/v1/default/banks/b/config"),
    ]
