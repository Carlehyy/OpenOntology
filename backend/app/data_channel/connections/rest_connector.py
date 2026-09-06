"""REST API Connector — 支持分页与增量(since 参数)"""
from __future__ import annotations
import json
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from app.services.connection.base import ConnectorBase

logger = logging.getLogger(__name__)


class RestConnector(ConnectorBase):
    """
    REST API 数据源连接器。

    config 示例：
    {
        "base_url": "https://api.example.com/v1",
        "endpoints": ["/orders", "/customers"],   # list_resources() 返回该列表
        "auth": {
            "type": "bearer",   # bearer | basic | api_key
            "token": "xxx"      # bearer 令牌
        },
        "params": {"page_size": 100},     # 附加到所有请求的公共参数
        "pagination": {
            "type": "page",     # page | cursor | offset (当前实现 page)
            "page_param": "page",
            "size_param": "page_size",
            "data_path": "data"   # JSON 响应中数据数组的字段名 (如 "data", "results")
        },
        "delta_param": "since"    # 增量参数名, GET 请求附加 ?since=<timestamp>
    }
    """

    # 分页全量拉取的累计行数护栏（单页大小不受本端控制，见 pull_full）
    _TOTAL_ROWS_CEILING = 100_000

    def __init__(self, config: dict):
        self._config = self._normalize_config(config)
        self._session = None

    @staticmethod
    def _normalize_config(config: dict) -> dict:
        """兼容页面单 URL 配置，并归一为连接器的 canonical 契约。"""
        normalized = dict(config or {})

        headers = normalized.get("headers", {})
        if isinstance(headers, str):
            try:
                headers = json.loads(headers) if headers.strip() else {}
            except json.JSONDecodeError as exc:
                raise ValueError(f"REST 请求头不是合法 JSON：{exc.msg}") from exc
        if not isinstance(headers, dict):
            raise ValueError("REST 请求头必须是 JSON 对象")
        normalized["headers"] = {
            str(key): str(value) for key, value in headers.items()
        }

        endpoints = normalized.get("endpoints")
        if isinstance(endpoints, str):
            endpoints = [endpoints]
        if not isinstance(endpoints, list):
            endpoints = []
        endpoints = [str(item) for item in endpoints if str(item)]

        # 页面只填写一个完整 URL 时，拆成 origin + path/query，避免 httpx
        # base_url 的路径合并规则把用户请求地址改写掉。
        url = str(normalized.get("url") or "").strip()
        if url and not normalized.get("base_url"):
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("REST API URL 必须是有效的 http/https 地址")
            normalized["base_url"] = urlunsplit(
                (parsed.scheme, parsed.netloc, "", "", "")
            )
            target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            if not endpoints:
                endpoints = [target]

        endpoint = str(normalized.get("endpoint") or "").strip()
        if endpoint and not endpoints:
            endpoints = [endpoint]
        normalized["endpoints"] = endpoints
        return normalized

    def _get_session(self):
        """返回 httpx 会话实例 (延迟初始化)"""
        if self._session is None:
            try:
                import httpx
            except ImportError:
                raise RuntimeError("httpx 未安装, 请执行 pip install httpx")
            auth_cfg = self._config.get("auth", {})
            headers = dict(self._config.get("headers", {}))
            client_auth = None
            if auth_cfg.get("type") == "bearer":
                headers["Authorization"] = f"Bearer {auth_cfg.get('token', '')}"
            elif auth_cfg.get("type") == "api_key":
                headers[auth_cfg.get("header", "X-API-Key")] = auth_cfg.get("token", "")
            elif auth_cfg.get("type") == "basic":
                client_auth = (
                    str(auth_cfg.get("username", "")),
                    str(auth_cfg.get("password", "")),
                )
            self._session = httpx.Client(
                base_url=self._config.get("base_url", ""),
                headers=headers,
                auth=client_auth,
                timeout=30.0,
            )
        return self._session

    def test_connection(self) -> bool:
        """连接测试 — 请求第一个端点检查状态"""
        endpoints = self._config.get("endpoints", [])
        if not endpoints:
            return False
        try:
            pagination = self._config.get("pagination", {})
            params = dict(self._config.get("params", {}))
            params.update({
                pagination.get("page_param", "page"): 1,
                pagination.get("size_param", "page_size"): 1,
            })
            resp = self._get_session().get(endpoints[0], params=params)
            return resp.status_code < 400
        except Exception as e:
            logger.warning(f"REST 连接测试失败: {e}")
            return False

    def list_resources(self) -> list[str]:
        """返回 config 中定义的端点列表"""
        return self._config.get("endpoints", [])

    def pull_sample(self, resource: str, limit: int = 100) -> list[dict]:
        """从端点查询样本数据"""
        pagination = self._config.get("pagination", {})
        params = dict(self._config.get("params", {}))
        params.update({
            pagination.get("page_param", "page"): 1,
            pagination.get("size_param", "page_size"): min(limit, 100),
        })
        from app.shared import perf_spans

        span = perf_spans.begin_span(
            "http", name="GET", target=perf_spans.http_target(resource))
        request_status = "error"
        try:
            resp = self._get_session().get(resource, params=params)
            request_status = str(resp.status_code)
            resp.raise_for_status()
        finally:
            perf_spans.end_span(span, status=request_status)
        return self._extract_records(resp.json())[:limit]

    def pull_full(self, resource: str) -> list[dict]:
        """通过分页查询全量数据"""
        pagination = self._config.get("pagination", {})
        page_param = pagination.get("page_param", "page")
        size_param = pagination.get("size_param", "page_size")

        all_records = []
        page = 1
        base_params = dict(self._config.get("params", {}))

        session = self._get_session()
        from app.shared import perf_spans

        while True:
            params = {**base_params, page_param: page, size_param: 100}
            span = perf_spans.begin_span(
                "http", name="GET", target=perf_spans.http_target(resource))
            request_status = "error"
            try:
                resp = session.get(resource, params=params)
                request_status = str(resp.status_code)
                resp.raise_for_status()
            finally:
                perf_spans.end_span(span, status=request_status)
            data = resp.json()
            records = self._extract_records(data)
            if not records:
                break
            all_records.extend(records)
            # 总量护栏：页数有 100 页上限，但第三方服务端单页可无视
            # page_size 返回任意多条（100×N 放大），累计行数封顶截断
            if len(all_records) >= self._TOTAL_ROWS_CEILING:
                logger.warning(
                    "REST 拉取累计行数达到 %d 上限已截断（端点 %s 单页返回"
                    "不受 page_size 约束）；如需完整同步请缩小上游分页",
                    self._TOTAL_ROWS_CEILING, resource)
                break
            # 检查是否存在下一页
            if isinstance(data, dict):
                if not data.get("next") and len(records) < 100:
                    break
            else:
                break
            page += 1
            if page > 100:  # 安全上限
                break

        return all_records

    def pull_delta(self, resource: str, since: str | None = None) -> list[dict]:
        """增量查询: 将 since 参数加入查询串后请求"""
        if not since:
            return self.pull_full(resource)
        delta_param = self._config.get("delta_param", "since")
        params = dict(self._config.get("params", {}))
        params[delta_param] = since
        resp = self._get_session().get(resource, params=params)
        resp.raise_for_status()
        return self._extract_records(resp.json())

    def _extract_records(self, data: Any) -> list[dict]:
        """从 API 响应中提取记录列表"""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            data_path = self._config.get("pagination", {}).get("data_path", "")
            for key in [data_path, "data", "results", "items", "records"]:
                if key and key in data and isinstance(data[key], list):
                    return data[key]
        return []
