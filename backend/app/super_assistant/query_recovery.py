"""助手查询的有界恢复策略（纯内存逻辑，无 IO）。

分类只依赖受控字段（``code``/``quota``/``retryableAfter`` 等），不解析
message、不猜 TTL、不做参数自动改写。四类语义：

* ``resource``（R1）：句柄饱和 / 并发字节预留 / 本地快照配额 / 查询 ID
  冲突。句柄饱和与快照配额只在**新查询**上做 release-gated 阻塞，已有
  cursor 续读仍可正常执行；并发字节预留与 ``query_id_in_use`` 不封锁其他
  工作（依赖 owner 单飞与同签名阈值）。解除只认真实释放事件或本回合结束，
  不推算 TTL。
* ``retryable``（R2）：仅非 cursor 续页的瞬时连接/可达性错误，允许同签名
  短退避（0.5s/1.5s，最多 2 次），可取消、不新造等待配置。
* ``correctable``（R3）：模型可纠正的参数错误，只回灌，不改参。
* ``terminal``（R4/过期/发布/注销 cursor）：不重试、不改写；重新发现
  新的 catalog/structure 后以新查询重新授权。

签名含实际参数（含 cursor）：不同合法续页不合并；换参不重置无进展。
连续 8 轮无有效成功 → 停止并以受控事实输出诚实失败（code + 次数 + 建议）。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

ONTOLOGY_QUERY_TOOL = "ontology_query"

# R1 资源/请求冲突（细分在 classify_payload 内做，不因同类错误整轮永久封锁）
R1_CODES = frozenset({
    "memory_quota_exceeded",
    "memory_reservation_exceeded",
    "snapshot_quota",
    "query_snapshot_quota_exceeded",
    "query_id_in_use",
})
HANDLE_SATURATION_QUOTA = "max_handles_per_user"
# 可由“终页已真实释放”解除的新查询阻塞（句柄饱和 / 本地快照配额）
RELEASE_GATED_CODES = frozenset({
    "memory_quota_exceeded",
    "snapshot_quota",
    "query_snapshot_quota_exceeded",
})

# R2 瞬时可达性错误（只有非 cursor 续页才允许自动短退避）
R2_CODES = frozenset({
    "SOURCE_UNAVAILABLE",
    "query_provider_unavailable",
    "query_backend_unavailable",
    "remote_query_unavailable",
    "query_retry_required",
})
R2_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.5)
R2_MAX_RETRIES = len(R2_RETRY_DELAYS)

# 过期/发布/cursor 注销类：不重试旧 cursor、不自动改写。
TERMINAL_CODES = frozenset({
    "invalid_cursor",
    "cursor_expired",
    "result_handle_expired",
    "query_snapshot_expired",
    "query_context_changed",
    "release_context_changed",
})

# R3 模型可纠正（只回灌，不改参）
R3_CODES = frozenset({
    "invalid_parameter",
    "invalid_page_size",
    "unknown_parameter",
    "unknown_source",
    "duplicate_alias",
    "spl_limit_unsupported",
    "remote_query_rejected",
})
_R3_PREFIXES = ("invalid_request",)

# 同签名阈值：R1/R4 总尝试 ≤2，其他 ≤3（工程默认，测试锁定）。
RESOURCE_SIGNATURE_LIMIT = 2
TERMINAL_SIGNATURE_LIMIT = 2
OTHER_SIGNATURE_LIMIT = 3
NO_PROGRESS_LIMIT = 8

CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_QUOTA_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
FALLBACK_CODE = "ontology_tool_failed"

CLASSIFICATION_RESOURCE = "resource"
CLASSIFICATION_RETRYABLE = "retryable"
CLASSIFICATION_CORRECTABLE = "correctable"
CLASSIFICATION_TERMINAL = "terminal"


@dataclass(frozen=True)
class FailureInfo:
    code: str
    classification: str
    quota: str | None
    retryable: bool
    signature: str
    base_signature: str
    attempts: int
    limit: int


@dataclass
class _FailureRecord:
    base: str
    code: str
    limit: int
    count: int = 0


def _safe_code(value: Any) -> str:
    if isinstance(value, str) and CODE_PATTERN.fullmatch(value):
        return value
    return FALLBACK_CODE


def _safe_quota(value: Any) -> str | None:
    if isinstance(value, str) and _QUOTA_PATTERN.fullmatch(value):
        return value
    return None


def _canonical(arguments: Any) -> str:
    try:
        return json.dumps(
            arguments if isinstance(arguments, dict) else {},
            sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, default=str,
        )
    except (TypeError, ValueError):
        return "{}"


def base_signature(tool_name: str, arguments: Any) -> str:
    """请求签名（工具 + 规范参数，含实际 cursor）：不同续页不合并。"""
    raw = json.dumps(
        {"tool": tool_name, "arguments": _canonical(arguments)},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def failure_signature(tool_name: str, arguments: Any, code: str) -> str:
    """失败签名（工具 + code + 规范参数）；成功按 base_signature 清除。"""
    raw = json.dumps(
        {"tool": tool_name, "code": code, "arguments": _canonical(arguments)},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _has_cursor(arguments: Any) -> bool:
    """cursor 键存在且值非 None 即视为续查请求（与门面 builder 语义一致）。

    显式 ``cursor=None`` / 缺键属于新查询；``""``/``0``/``False`` 等非法值
    仍落入 cursor 分支，交由工具入口返回受控 ``invalid_cursor``，而不是被
    release-gated 资源预检遮成 ``query_resource_blocked``。
    """
    return (
        isinstance(arguments, dict)
        and "cursor" in arguments
        and arguments.get("cursor") is not None
    )


def classify_payload(payload: Any,
                     *, arguments: Any) -> FailureInfo | None:
    """受控 error 信封 → 恢复分类；成功/不可解析 → None。"""
    if not isinstance(payload, dict) or "error" not in payload:
        return None
    code = _safe_code(payload.get("code"))
    quota = _safe_quota(payload.get("quota"))
    cursor = _has_cursor(arguments)
    if code in R1_CODES:
        classification = CLASSIFICATION_RESOURCE
        limit = RESOURCE_SIGNATURE_LIMIT
        retryable = False
    elif code in R2_CODES:
        # 续页遇到 SOURCE_UNAVAILABLE 时旧句柄可能已注销：不重试旧 cursor。
        classification = CLASSIFICATION_TERMINAL if cursor else CLASSIFICATION_RETRYABLE
        retryable = not cursor
        limit = TERMINAL_SIGNATURE_LIMIT if cursor else OTHER_SIGNATURE_LIMIT
    elif code in TERMINAL_CODES:
        classification = CLASSIFICATION_TERMINAL
        retryable = False
        limit = TERMINAL_SIGNATURE_LIMIT
    elif code in R3_CODES or code.startswith(_R3_PREFIXES):
        classification = CLASSIFICATION_CORRECTABLE
        retryable = False
        limit = OTHER_SIGNATURE_LIMIT
    else:
        # R4：权限/发布/属性/会话、ontology_tool_failed、裸异常等不可纠正错误。
        classification = CLASSIFICATION_TERMINAL
        retryable = False
        limit = TERMINAL_SIGNATURE_LIMIT
    return FailureInfo(
        code=code,
        classification=classification,
        quota=quota,
        retryable=retryable,
        signature=failure_signature(ONTOLOGY_QUERY_TOOL, arguments, code),
        base_signature=base_signature(ONTOLOGY_QUERY_TOOL, arguments),
        attempts=1,
        limit=limit,
    )


def _release_gated_failure(code: str, quota: str | None) -> dict[str, Any] | None:
    """只有句柄饱和/快照配额类进入“新查询暂停”，并保留受控依据。"""
    if code not in RELEASE_GATED_CODES:
        return None
    if code == "memory_quota_exceeded" and quota not in (
            None, HANDLE_SATURATION_QUOTA):
        return None
    return {"code": code, "quota": quota or HANDLE_SATURATION_QUOTA}


def sleep_cancellable(delay: float, *,
                      cancelled: Callable[[], bool] | None = None,
                      poll_seconds: float = 0.05) -> bool:
    """可取消的短等待（沿用既有 100ms 心跳窗口，不新增配置）。

    返回 True 表示等待完成可继续重试；False 表示期间观察到取消。
    """
    deadline = time.monotonic() + max(0.0, float(delay or 0.0))
    while True:
        if cancelled is not None and cancelled():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(poll_seconds, remaining))


class QueryRecoveryState:
    """单个助手回合内的查询恢复状态（内存态，不落库、不跨回合）。"""

    def __init__(self) -> None:
        self._failures: dict[str, _FailureRecord] = {}
        self.last_failure: FailureInfo | None = None
        self.total_attempts = 0
        self.no_progress_rounds = 0
        self.release_block: dict[str, Any] | None = None
        self.stopped = False
        # 并行只读池里同轮可能有多个 ontology_query 线程（锁只保证执行不重叠，
        # 结果登记发生在锁外）：失败计数/资源阻塞用锁保护，避免读改写竞争。
        self._guard = threading.Lock()

    # ---------------------------------------------------------- 观测
    def record_failure(self, payload: dict[str, Any],
                       arguments: Any) -> FailureInfo:
        info = classify_payload(payload, arguments=arguments)
        if info is None:  # 调用方只在 error 信封上调用；显式失败而非静默
            raise ValueError("record_failure requires a controlled error payload")
        with self._guard:
            record = self._failures.get(info.signature)
            if record is None:
                record = _FailureRecord(
                    base=info.base_signature, code=info.code,
                    limit=info.limit, count=0)
                self._failures[info.signature] = record
            record.count += 1
            self.total_attempts += 1
            self.last_failure = FailureInfo(
                code=info.code, classification=info.classification,
                quota=info.quota, retryable=info.retryable,
                signature=info.signature, base_signature=info.base_signature,
                attempts=record.count, limit=record.limit)
            if info.classification == CLASSIFICATION_RESOURCE:
                gated = _release_gated_failure(info.code, info.quota)
                if gated is not None:
                    self.release_block = gated
            return self.last_failure

    def record_success(self, arguments: Any,
                       payload: dict[str, Any] | None = None) -> None:
        """有效成功（payload 无 error）：清除对应请求的失败计数。"""
        base = base_signature(ONTOLOGY_QUERY_TOOL, arguments)
        with self._guard:
            self._failures = {
                key: record for key, record in self._failures.items()
                if record.base != base
            }
            if isinstance(payload, dict) and payload.get("released") is True:
                # 只有真实释放事件才解除资源阻塞（released=false 不解除）。
                self.release_block = None

    # ---------------------------------------------------------- 决策
    def block_reason(self, arguments: Any) -> dict[str, Any] | None:
        """派发前的拒绝依据：同签名阈值 / release-gated 新查询阻塞。"""
        base = base_signature(ONTOLOGY_QUERY_TOOL, arguments)
        records = [record for record in self._failures.values()
                   if record.base == base]
        if records:
            latest = max(records, key=lambda record: record.count)
            if latest.count >= latest.limit:
                return {
                    "error": (
                        "同一查询参数已连续失败并达到重试上限，已停止重复执行；"
                        "请先按错误码修正参数或重新发现后再试"),
                    "code": "query_retry_suppressed",
                    "lastCode": latest.code,
                    "attempts": latest.count,
                }
        if self.release_block is not None and not _has_cursor(arguments):
            return {
                "error": (
                    "查询句柄/快照资源尚未恢复：请先读完已有 cursor 的续页，"
                    "待终页真实释放后再新建查询；不要原样重复失败查询"),
                "code": "query_resource_blocked",
                "lastCode": self.release_block.get("code"),
                "quota": self.release_block.get("quota"),
            }
        return None

    def allows_short_retry(self, info: FailureInfo, arguments: Any, *,
                           retries_used: int) -> bool:
        return (
            info.retryable
            and info.classification == CLASSIFICATION_RETRYABLE
            and not _has_cursor(arguments)
            and retries_used < R2_MAX_RETRIES
            and info.attempts < info.limit
        )

    # ------------------------------------------------------ 无进展停止
    def note_round(self, *, had_attempts: bool,
                   had_valid_success: bool) -> bool:
        if had_valid_success:
            self.no_progress_rounds = 0
            return False
        if had_attempts:
            self.no_progress_rounds += 1
        if self.no_progress_rounds >= NO_PROGRESS_LIMIT:
            self.stopped = True
        return self.stopped

    def honest_failure_text(self) -> str:
        """受控事实的诚实失败文案（code + 次数 + 下一步；不含业务值）。"""
        lines = ["本轮查询未取得有效成功，已停止继续自动尝试以免无进展循环。"]
        if self.last_failure is not None:
            lines.append(
                f"最后受控错误码：{self.last_failure.code}；"
                f"已累计尝试 {self.total_attempts} 次 ontology_query。")
        if self.release_block is not None:
            lines.append(
                "查询句柄/快照资源仍不足：请先读完已有 cursor 的续页，"
                "待终页释放后再新建查询。")
        lines.append(
            "请先按错误码用 ontology_catalog / ontology_structure 重新确认当前"
            "发布与可用参数，再以新查询重试；本轮答复不包含未验证的查询结果。")
        return "\n\n".join(lines)
