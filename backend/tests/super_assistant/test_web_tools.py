from __future__ import annotations

import pytest

from app.shared.config import settings
from app.super_assistant import web_tools
from app.super_assistant.mcp_client import McpClientError
from app.super_assistant.web_tools import WebToolError


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", payload: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


def _patch_request(monkeypatch, response: _FakeResponse, calls: list | None = None):
    def fake_request(method, url, **kwargs):
        if calls is not None:
            calls.append({"method": method, "url": url, **kwargs})
        return response

    monkeypatch.setattr(web_tools, "_request", fake_request)


def test_web_fetch_extracts_text_and_skips_script_and_style(monkeypatch):
    html = (
        "<html><head><style>body{color:red}</style>"
        "<script>var tracker = 1;</script></head>"
        "<body><h1>页面 标题</h1><p>第一\n\n 段</p>"
        "<script>alert('x')</script></body></html>"
    )
    _patch_request(monkeypatch, _FakeResponse(200, text=html))
    text = web_tools.web_fetch("http://203.0.113.10/page")
    assert "页面 标题" in text
    assert "第一 段" in text
    assert "tracker" not in text
    assert "alert" not in text
    assert "color" not in text


def test_web_fetch_sends_user_agent_without_implicit_redirects(monkeypatch):
    calls: list = []
    _patch_request(monkeypatch, _FakeResponse(200, text="<p>ok</p>"), calls)
    web_tools.web_fetch("http://203.0.113.10/page")
    assert calls[0]["method"] == "GET"
    assert calls[0]["headers"]["User-Agent"] == "OpenOntology-SuperAssistant/1.0"
    assert calls[0]["follow_redirects"] is False
    assert calls[0]["timeout"] == 20.0


def test_web_fetch_validates_every_redirect_hop(monkeypatch):
    calls: list = []
    responses = iter([
        _FakeResponse(302),
        _FakeResponse(200, text="<p>ok</p>"),
    ])

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        response = next(responses)
        if response.status_code == 302:
            response.headers = {"location": "http://127.0.0.1/secret"}
        return response

    monkeypatch.setattr(web_tools, "_request", fake_request)
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(McpClientError, match="非公网地址"):
        web_tools.web_fetch("http://93.184.216.34/start")
    assert len(calls) == 1


def test_web_fetch_raises_with_status_code_on_http_error(monkeypatch):
    _patch_request(monkeypatch, _FakeResponse(404, text="not found"))
    with pytest.raises(WebToolError, match="404"):
        web_tools.web_fetch("http://203.0.113.10/missing")


def test_web_fetch_truncates_to_max_chars(monkeypatch):
    _patch_request(monkeypatch, _FakeResponse(200, text="<p>" + "长" * 100 + "</p>"))
    assert len(web_tools.web_fetch("http://203.0.113.10/page", max_chars=10)) == 10
    monkeypatch.setattr(settings, "super_assistant_web_fetch_max_chars", 5)
    assert len(web_tools.web_fetch("http://203.0.113.10/page")) == 5


def test_web_fetch_requires_enablement(monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_web_fetch_enabled", False)
    with pytest.raises(WebToolError, match="未启用"):
        web_tools.web_fetch("http://203.0.113.10/page")


def test_web_fetch_rejects_private_targets_in_production(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    calls: list = []
    _patch_request(monkeypatch, _FakeResponse(200, text="<p>ok</p>"), calls)
    with pytest.raises(McpClientError, match="非公网地址"):
        web_tools.web_fetch("http://127.0.0.1/secret")
    assert calls == []  # SSRF 拒绝发生在发请求之前


def test_web_search_requires_configured_backend():
    with pytest.raises(WebToolError, match="未配置"):
        web_tools.web_search("本体")


def test_web_search_tavily_maps_results(monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_web_search_backend", "tavily")
    monkeypatch.setattr(settings, "super_assistant_web_search_tavily_api_key", "tv-key")
    calls: list = []
    _patch_request(monkeypatch, _FakeResponse(200, payload={
        "results": [
            {"title": "标题一", "url": "http://a.example", "content": "摘要一"},
            {"title": "标题二", "url": "http://b.example", "content": "摘要二"},
        ],
    }), calls)
    results = web_tools.web_search("本体 构建", max_results=5)
    assert results == [
        {"title": "标题一", "url": "http://a.example", "snippet": "摘要一"},
        {"title": "标题二", "url": "http://b.example", "snippet": "摘要二"},
    ]
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://api.tavily.com/search"
    assert calls[0]["json"] == {"api_key": "tv-key", "query": "本体 构建", "max_results": 5}


def test_web_search_tavily_raises_with_status_code(monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_web_search_backend", "tavily")
    _patch_request(monkeypatch, _FakeResponse(401))
    with pytest.raises(WebToolError, match="401"):
        web_tools.web_search("本体")


def test_web_search_brave_maps_results(monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_web_search_backend", "brave")
    monkeypatch.setattr(settings, "super_assistant_web_search_brave_api_key", "br-key")
    calls: list = []
    _patch_request(monkeypatch, _FakeResponse(200, payload={
        "web": {"results": [{"title": "标题", "url": "http://a.example", "description": "描述"}]},
    }), calls)
    results = web_tools.web_search("本体", max_results=3)
    assert results == [{"title": "标题", "url": "http://a.example", "snippet": "描述"}]
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "https://api.search.brave.com/res/v1/web/search"
    assert calls[0]["headers"]["X-Subscription-Token"] == "br-key"
    assert calls[0]["params"] == {"q": "本体", "count": 3}


def test_web_search_brave_raises_with_status_code(monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_web_search_backend", "brave")
    _patch_request(monkeypatch, _FakeResponse(429))
    with pytest.raises(WebToolError, match="429"):
        web_tools.web_search("本体")
