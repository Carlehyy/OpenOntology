"""Web 工具：``web_fetch`` 抓取网页正文，``web_search`` 走配置化搜索后端。

``web_fetch`` 复用 MCP 的 SSRF 校验（生产环境拒绝非公网地址），正文提取
只用标准库 html.parser。``web_search`` 支持 tavily / brave 两种后端，由
``SUPER_ASSISTANT_WEB_SEARCH_BACKEND`` 显式开启；所有 httpx 调用集中在
``_request``，便于测试 mock。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import httpx

from app.shared.config import settings
from app.super_assistant.mcp_client import validate_mcp_url

_TIMEOUT_SECONDS = 20.0
_MAX_REDIRECTS = 3
_USER_AGENT = "OpenOntology-SuperAssistant/1.0"
_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


class WebToolError(RuntimeError):
    """可直接展示给模型/用户的 web 工具失败。"""


def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """集中的 httpx 调用点（测试 monkeypatch 此函数）。"""
    return httpx.request(method, url, **kwargs)


class _TextExtractor(HTMLParser):
    """提取可见文本，跳过 script/style 内容。"""

    _SKIP_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _extract_text(html: str) -> str:
    """去 script/style 后折叠空白，返回纯文本。"""
    parser = _TextExtractor()
    parser.feed(html)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def web_fetch(url: str, max_chars: int | None = None) -> str:
    """抓取 URL 并返回折叠空白后的纯文本，截断到 max_chars。"""
    if not settings.super_assistant_web_fetch_enabled:
        raise WebToolError("web_fetch 未启用：请开启 SUPER_ASSISTANT_WEB_FETCH_ENABLED")
    safe_url = validate_mcp_url(url)
    # Do not let the HTTP client follow redirects implicitly.  Each hop is a
    # fresh network target and must pass the SSRF guard before it is requested;
    # otherwise a public URL could redirect into localhost/private metadata
    # services.  The fetch carries no credentials, but the same boundary is
    # required for consistent egress policy.
    for redirect_count in range(_MAX_REDIRECTS + 1):
        response = _request(
            "GET",
            safe_url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        if response.status_code < 300 or response.status_code >= 400:
            break
        location = getattr(response, "headers", {}).get("location")
        if not location:
            raise WebToolError("web_fetch 重定向缺少 Location")
        if redirect_count >= _MAX_REDIRECTS:
            raise WebToolError("web_fetch 重定向次数超过上限")
        safe_url = validate_mcp_url(urljoin(safe_url, str(location)))
    if response.status_code < 200 or response.status_code >= 300:
        raise WebToolError(f"web_fetch 请求失败：HTTP {response.status_code}")
    text = _extract_text(response.text)
    limit = int(max_chars or settings.super_assistant_web_fetch_max_chars)
    if len(text) > limit:
        text = text[:limit]
    return text


def web_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """按配置后端搜索，返回 [{title, url, snippet}]；未配置时抛错。"""
    backend = (settings.super_assistant_web_search_backend or "").strip().lower()
    if backend == "tavily":
        return _search_tavily(query, max_results)
    if backend == "brave":
        return _search_brave(query, max_results)
    raise WebToolError("未配置 web search：请设置 SUPER_ASSISTANT_WEB_SEARCH_BACKEND")


def _search_tavily(query: str, max_results: int) -> list[dict[str, str]]:
    response = _request(
        "POST",
        _TAVILY_SEARCH_URL,
        json={
            "api_key": settings.super_assistant_web_search_tavily_api_key,
            "query": query,
            "max_results": max_results,
        },
        timeout=_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise WebToolError(f"tavily 搜索失败：HTTP {response.status_code}")
    data = response.json()
    results = [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": str(item.get("content") or ""),
        }
        for item in data.get("results") or []
    ]
    return results[:max_results]


def _search_brave(query: str, max_results: int) -> list[dict[str, str]]:
    response = _request(
        "GET",
        _BRAVE_SEARCH_URL,
        headers={
            "X-Subscription-Token": settings.super_assistant_web_search_brave_api_key,
            "Accept": "application/json",
        },
        params={"q": query, "count": max_results},
        timeout=_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise WebToolError(f"brave 搜索失败：HTTP {response.status_code}")
    data = response.json()
    results = [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": str(item.get("description") or ""),
        }
        for item in (data.get("web") or {}).get("results") or []
    ]
    return results[:max_results]
