"""记忆宫殿图谱视图缓存胶水层（fail-open）。

键名空间 ob:palace:*。GET /palace/graph 是弹窗打开与轮询期间最贵的一腿
（每作用域数趟 Cypher + 大 payload），本层对其做整体短 TTL cache-aside；
写侧（抽取完成、文件删除、聚类合并、本体文档建图）bump 版本换键——
executor 进程与 Web 进程经共享 Redis 同一版本键，事件失效跨进程生效。

设计契约与 ``app.shared.redis_cache`` / ``app.ontologies.cache`` 一致：
Redis 只是加速器，任何连接/读写异常静默降级直查；开关关闭时完全绕过缓存，
等价于现状直查路径。轮询期间图谱新鲜度由「builds 定格后前端才拉图谱」
配合写侧 bump 保证，短 TTL 兜底漏失效。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from app.config import settings
from app.shared import redis_cache

_GRAPH_VERSION_KEY = "ob:palace:graph:ver"


def _max_bytes() -> int:
    """单键回填上限：超限（超大图 + 共享本体层）放弃缓存只走直查。"""
    return int(settings.super_assistant_palace_graph_cache_max_bytes)


def _enabled() -> bool:
    return bool(settings.super_assistant_palace_graph_cache_enabled)


def _version() -> str:
    if not _enabled():
        return "0"
    return redis_cache.cache_version(_GRAPH_VERSION_KEY)


def graph_cache_key(owner_id: str) -> str:
    """图谱视图响应：按作用域（用户 owner_id 或本体文档系统作用域）分键。"""
    return f"ob:palace:graph:v{_version()}:{owner_id}"


def graph_cached_call(key: str, ttl_seconds: int, builder: Callable[[], Any]) -> Any:
    """图谱视图 cache-aside：开关关闭时完全绕过；回填前校验字节上限。"""
    if not _enabled():
        return builder()
    cached = redis_cache.cache_get(key)
    if cached is not None:
        return cached
    value = builder()
    if isinstance(value, dict) and value.get("available") is False:
        # Neo4j 暂不可用是瞬态降级结果，不缓存
        return value
    try:
        payload = json.dumps(value, ensure_ascii=False, default=str)
        if len(payload.encode("utf-8")) <= _max_bytes():
            redis_cache.cache_set(key, value, ttl_seconds)
    except Exception:  # noqa: BLE001 — 序列化/回填失败不影响返回值
        pass
    return value


def invalidate_graph() -> None:
    """图谱内容发生变化的写路径调用（bump 版本，旧键靠 TTL 自然过期）。"""
    redis_cache.cache_bump(_GRAPH_VERSION_KEY)
