"""数据资产湖读缓存胶水层（ob:lake:* 键空间）。

复用 app.shared.redis_cache 的 fail-open 契约：Redis 任何连接/读写异常都
静默降级直查，绝不影响主流程。本模块只负责资产湖特有的键空间与失效语义：

- **版本数据不可变**：预览/契约/统计的缓存键携带 version id，上传、在线
  编辑、流水线发布产生新版本自动换新键，旧键靠 TTL 自然回收，无需主动
  失效，也绝无脏读；
- **总览是聚合视图**：键携带总览版本号，写路径（版本发布/删除/契约声明/
  成品评审与删除）自增版本键整体失效，旧键依赖 10s 短 TTL 自然过期；
  总览摘要（五张洞察卡片的聚合口径）与总览列表共用同一版本号；
- **体积上限**：超过上限的载荷只回源不回填，避免大分页预览撑爆内存。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from app.config import settings
from app.shared import redis_cache

OVERVIEW_TTL_SECONDS: int = 10
VERSION_TTL_SECONDS: int = 1800
#: 单条缓存值的字节上限；超限不缓存。
MAX_PAYLOAD_BYTES: int = 512 * 1024

_OVERVIEW_VERSION_KEY = "ob:lake:overview:ver"


def _enabled() -> bool:
    return bool(settings.dataset_cache_enabled)


def _version(key: str) -> str:
    if not _enabled():
        return "0"
    return redis_cache.cache_version(key)


def overview_key(params: dict) -> str:
    """总览键 = 查询参数指纹 + 总览版本号（写路径 bump 即整体失效）。"""
    canonical = json.dumps(params, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
    return f"ob:lake:overview:v{_version(_OVERVIEW_VERSION_KEY)}:{digest}"


def summary_key() -> str:
    """总览摘要键：无查询参数，只携带总览版本号，与总览列表同步失效。"""
    return f"ob:lake:summary:v{_version(_OVERVIEW_VERSION_KEY)}"


def cached_call(
    key: str,
    ttl_seconds: int,
    builder: Callable[[], Any],
    *,
    cap_payload: bool = True,
) -> Any:
    """命中即返回缓存；未命中回源并尽力回填（超限载荷只回源不回填）。"""
    if not _enabled():
        return builder()
    cached = redis_cache.cache_get(key)
    if cached is not None:
        return cached
    value = builder()
    if cap_payload:
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 — 序列化失败等同超限，跳过回填
            payload = ""
        if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            return value
    redis_cache.cache_set(key, value, ttl_seconds)
    return value


def invalidate_overview() -> None:
    """写路径调用：自增总览版本键，旧键由短 TTL 自然回收。"""
    redis_cache.cache_bump(_OVERVIEW_VERSION_KEY)
