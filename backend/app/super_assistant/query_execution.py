"""助手查询执行辅助：状态诚实化判别与按 owner 单飞。

职责单一，不承载查询实现本身：

1. **状态诚实化**（``query_failure_code``）：只对 ``server is None`` 的内置
   ``ontology_query`` 的受控 JSON error 信封生效。MCP/动作/其他内置工具
   以及成功结果（含被字符截断的大 JSON）一律不套用，避免把返回型失败
   记成 success，也避免把合法成功误判为失败。
2. **同 owner 单飞**（``owner_query_single_flight``）：只在 runtime 并行层
   包住 ``ontology_query`` 的执行；不同 owner 互不串行，其他只读工具仍走
   原有并行/串行路径。锁按 owner 引用计数注册，取消/异常/结束后清理，
   不泄漏、不跨 owner 阻塞。
"""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from typing import Any, Callable

ONTOLOGY_QUERY_TOOL = "ontology_query"

# 受控错误码：只允许短标识符形态进入状态/持久面；异常原文、消息、输入值
# 一律不当作 code。
SAFE_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
FALLBACK_ERROR_CODE = "ontology_tool_failed"

# 等待锁时的轮询间隔（只用于响应取消，不引入新的等待配置）。
_LOCK_POLL_SECONDS = 0.05


def parse_payload(output: Any) -> dict[str, Any] | None:
    """把工具输出解析为 JSON 对象；不可解析/非对象 → None。"""
    if isinstance(output, dict):
        return output
    try:
        payload = json.loads(str(output or ""))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def payload_has_error(output: Any) -> bool:
    """工具输出是否是受控/可解析的 error 信封（不可解析一律不算失败）。"""
    payload = parse_payload(output)
    return payload is not None and "error" in payload


def safe_error_code(value: Any) -> str:
    """把任意 code 收敛为受控错误码（非法/缺失 → 固定兜底码）。"""
    if isinstance(value, str) and SAFE_CODE_PATTERN.fullmatch(value):
        return value
    return FALLBACK_ERROR_CODE


def query_failure_code(*, tool_name: str, is_builtin: bool,
                       output: Any) -> str | None:
    """内置 ontology_query 的受控 JSON error → 安全 code；否则 None。

    * ``is_builtin`` 必须由调用方以 ``server is None`` 判定，MCP 不适用；
    * 只解析模型可见的**原始** output（不解析被壳化的持久文本）；
    * 不可解析（含成功大 JSON 被字符截断）→ None，不改变状态。
    """
    if not is_builtin or tool_name != ONTOLOGY_QUERY_TOOL:
        return None
    payload = parse_payload(output)
    if payload is None or "error" not in payload:
        return None
    return safe_error_code(payload.get("code"))


# ------------------------------------------------------- 按 owner 单飞


class QueryWaitCancelled(RuntimeError):
    """等待同 owner 查询锁时收到取消信号。"""


class _OwnerLockEntry:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


_ENTRIES: dict[str, _OwnerLockEntry] = {}
_ENTRIES_GUARD = threading.Lock()


def _register_entry(owner_key: str) -> _OwnerLockEntry:
    with _ENTRIES_GUARD:
        entry = _ENTRIES.get(owner_key)
        if entry is None:
            entry = _OwnerLockEntry()
            _ENTRIES[owner_key] = entry
        entry.users += 1
        return entry


def _unregister_entry(owner_key: str) -> None:
    with _ENTRIES_GUARD:
        entry = _ENTRIES.get(owner_key)
        if entry is None:
            return
        entry.users -= 1
        if entry.users <= 0 and not entry.lock.locked():
            _ENTRIES.pop(owner_key, None)


@contextmanager
def owner_query_single_flight(owner_id: str, *,
                              cancelled: Callable[[], bool] | None = None):
    """同 owner 的 ontology_query 串行执行；等待可被取消。

    取消/异常/正常结束都经 finally 释放锁并回收注册项；不同 owner 使用
    不同锁，互不串行。
    """
    owner_key = str(owner_id or "")
    entry = _register_entry(owner_key)
    acquired = False
    try:
        while not entry.lock.acquire(timeout=_LOCK_POLL_SECONDS):
            if cancelled is not None and cancelled():
                raise QueryWaitCancelled("查询排队等待已取消")
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        _unregister_entry(owner_key)


def active_owner_count() -> int:
    """当前注册的 owner 锁数量（诊断/测试用：正常结束后应为 0）。"""
    with _ENTRIES_GUARD:
        return len(_ENTRIES)
