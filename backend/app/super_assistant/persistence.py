"""可信远程联邦响应的 persistence 标记 — 落库边界与 JSON 感知截断。

联邦查询（dbx 等远程数据源）经可信服务端工具返回的 JSON 顶层可携带
``persistence`` 标记（发射端契约 v1）::

    "persistence": {
        "mode": "ephemeral",
        "classification": "detail" | "aggregate",
        "reference": {...},            # 可选受控引用补充
    }

消费语义（最保守 fail-closed）：

- 完全无 ``persistence`` 键 → 本地响应，完全保持旧落库行为；
- ``mode``/``classification`` 任何非法（未知 mode、classification 缺失或
  未知值）→ 按 detail 壳处理，不得视同旧行为；
- detail → 行级源明细只进入本轮模型上下文与 SSE（临时），所有持久面
  （ToolRun.result / steps.preview / 落库 messages / conversation.summary /
  记忆候选 / 导出）只允许元信息 + 受控引用壳；
- aggregate → 服务端已分类的聚合载荷允许按旧行为落库，其计数类聚合
  元数据可嵌入答复壳；运行时不采信模型口头声称的聚合（答复壳替换是
  代码层执行，不依赖 prompt）；
- 本轮建立远程上下文后（出现过任一合法标记），后续可信工具输出的
  marker 丢失 / 执行异常回灌同样壳化，不持久化任何无法证明不含源
  明细的内容；可信联邦工具输出不可解析（字符截断坏）时同样直接
  壳化——其执行器只会返回 JSON 对象，不可解析即无从证明不含明细，
  与是否已建立远程上下文无关。

标记只从可信内置工具（经白名单门面进程内执行，见 HONORED_MARKER_TOOLS）
解析；MCP/web/子代理等外部内容中的同形标记一律忽略——不可信来源没有
分类权，也不能借标记改变本地行为。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MARKER_KEY = "persistence"
MODE_EPHEMERAL = "ephemeral"
CLASSIFICATION_DETAIL = "detail"
CLASSIFICATION_AGGREGATE = "aggregate"

# 只有进程内可信执行的联邦查询工具其响应标记才被解析（架构守卫：
# ontology_tools 是 ontologies 门面的唯一桥接；MCP 等外部通道不在内）。
HONORED_MARKER_TOOLS = frozenset({"ontology_query"})
DETAIL_SAFE_TOOLS = frozenset({"ontology_query", "ontology_catalog", "ontology_structure"})
REDACTED_ARGUMENTS = {"ephemeralDetail": True, "persisted": "arguments_redacted"}

# 响应顶层的记录数组键：壳化与 JSON 感知截断都按“明细行”处理。
_RECORD_ARRAY_KEYS = ("rows", "records", "items", "data", "results")

# 壳与聚合元信息 allow-list：仅这些顶层键可进入持久面。columns 只含
# 列名；citations/sourceStates/params/joinEvidence 等可能携带原值的字段
# 一律不在名单内（即使发射端误带也不落库）。
_SHELL_METADATA_KEYS = (
    "queryId", "schemaVersion", "observedAt", "releases", "columns",
    "pageRowCount", "totalRowCount", "distinctEntityCounts", "inputCounts",
    "joinRowCounts", "countsScope", "hasNext", "cursor", "expiresAt",
    "partial", "truncated", "exact", "completeness",
)

# 异常/不可验证输出在远程上下文里可保留的错误信息上界（字符）。
_ERROR_MESSAGE_KEEP_CHARS = 120


@dataclass(frozen=True)
class PersistenceMarker:
    """合法 persistence 标记（mode=ephemeral + 可信 classification）。"""

    classification: str
    reference: dict[str, Any] = field(default_factory=dict)

    @property
    def is_detail(self) -> bool:
        return self.classification == CLASSIFICATION_DETAIL

    @property
    def is_aggregate(self) -> bool:
        return self.classification == CLASSIFICATION_AGGREGATE


def _fail_closed_marker() -> PersistenceMarker:
    return PersistenceMarker(CLASSIFICATION_DETAIL, {})


def parse_persistence_marker(payload: dict[str, Any]) -> PersistenceMarker | None:
    """解析顶层 persistence 标记。

    返回 None 表示响应没有 persistence 键（本地响应，旧行为）；键存在但
    形状非法时返回 fail-closed 的 detail 标记——标记既然出现就说明这是
    联邦路径，分类不可证时按最保守处理。
    """
    if MARKER_KEY not in payload:
        return None
    marker = payload.get(MARKER_KEY)
    if not isinstance(marker, dict):
        return _fail_closed_marker()
    if marker.get("mode") != MODE_EPHEMERAL:
        return _fail_closed_marker()
    classification = marker.get("classification")
    if classification not in (CLASSIFICATION_DETAIL, CLASSIFICATION_AGGREGATE):
        return _fail_closed_marker()
    reference = marker.get("reference")
    reference = reference if isinstance(reference, dict) else {}
    return PersistenceMarker(classification, reference)


def _clean_reference(reference: dict[str, Any]) -> dict[str, Any]:
    """受控引用只保留 allow-list 键内的标量/标量列表值。"""
    cleaned: dict[str, Any] = {}
    for key, value in reference.items():
        if key not in _SHELL_METADATA_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            cleaned[key] = value
        elif isinstance(value, list) and all(
            isinstance(item, (str, int, float, bool)) or item is None
            for item in value
        ):
            cleaned[key] = value
    return cleaned


def _metadata_view(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in _SHELL_METADATA_KEYS if key in payload}


def detail_shell(payload: dict[str, Any],
                 marker: PersistenceMarker) -> dict[str, Any]:
    """detail 载荷的持久壳：元信息 + 受控引用，不含任何记录数组。"""
    shell = _metadata_view(payload)
    rows = payload.get("rows")
    if isinstance(rows, list):
        shell["redactedRowCount"] = len(rows)
    if marker.reference:
        shell["reference"] = _clean_reference(marker.reference)
    shell[MARKER_KEY] = {
        "mode": MODE_EPHEMERAL,
        "classification": marker.classification,
        "persisted": "metadata_only",
    }
    shell["ephemeralDetail"] = True
    shell["note"] = "源明细按 ephemeral 处理未落库；可用 cursor 经 ontology_query 续读。"
    return shell


def _minimal_detail_shell(payload: dict[str, Any] | None) -> dict[str, Any]:
    """元数据超限时的最小壳：只保留可控引用必需字段。"""
    shell: dict[str, Any] = {
        MARKER_KEY: {
            "mode": MODE_EPHEMERAL,
            "classification": CLASSIFICATION_DETAIL,
            "persisted": "metadata_only",
        },
        "ephemeralDetail": True,
        "note": "远程联邦明细未落库（元数据超限，仅保留最小引用）。",
    }
    if isinstance(payload, dict):
        for key in ("queryId", "hasNext", "cursor", "totalRowCount"):
            if key in payload:
                shell[key] = payload[key]
    return shell


def aggregate_metadata(payload: dict[str, Any],
                       marker: PersistenceMarker | None = None) -> dict[str, Any]:
    """答复壳可嵌入的服务端聚合元数据（计数类 allow-list）。"""
    meta = _metadata_view(payload)
    if marker is not None and marker.reference:
        meta["reference"] = _clean_reference(marker.reference)
    return meta


def bounded_error_text(error: Any) -> str:
    """异常原文可能包含任意源值，即使很短也不能进入持久面。"""
    return "remote_execution_failed"


@dataclass
class EphemeralTurnState:
    """一条 assistant 消息生命周期内的远程联邦上下文与明细红线。

    runtime 在工具结果落库前逐个调用 ``register``，取得“应持久化”的
    结果文本；模型上下文与 SSE 继续使用原始 output（临时可见）。
    """

    detail_seen: bool = False
    remote_seen: bool = False
    refs: list[dict[str, Any]] = field(default_factory=list)
    aggregates: list[dict[str, Any]] = field(default_factory=list)
    # 摘要红线：tool_call_id → 持久文本（压缩摘要只允许看到该文本）
    redactions: dict[str, str] = field(default_factory=dict)

    def register(self, tool_name: str, call_id: str, output: str,
                 *, cap: int | None = None) -> str:
        """登记一次可信工具输出，返回持久视图文本。"""
        if tool_name not in HONORED_MARKER_TOOLS:
            if self.detail_seen:
                return self._register_fail_closed(call_id, None, cap)
            return output
        payload = _parse_output_json(output)
        if payload is None:
            # 到这里的一定是可信联邦工具（ontology_query），其执行器只会
            # 返回 JSON 对象；不可解析只能是字符截断损坏或异常回灌——
            # 即使是本包第一个输出也无从证明不含明细，一律 fail-closed。
            return self._register_fail_closed(call_id, None, cap)
        marker = parse_persistence_marker(payload)
        if marker is None:
            if self.remote_seen:
                # 远程上下文中 marker 丢失：不可证明是本地响应，壳化。
                return self._register_fail_closed(call_id, payload, cap)
            return output
        self.remote_seen = True
        if marker.is_aggregate:
            self.aggregates.append(aggregate_metadata(payload, marker))
            return output
        return self._register_detail(call_id, payload, marker, cap)

    # ---------------------------------------------------------- internals

    def _register_detail(self, call_id: str, payload: dict[str, Any],
                         marker: PersistenceMarker, cap: int | None) -> str:
        self.detail_seen = True
        shell = detail_shell(payload, marker)
        text = json.dumps(shell, ensure_ascii=False, default=str)
        if cap is not None and len(text) > cap:
            shell = _minimal_detail_shell(payload)
            text = json.dumps(shell, ensure_ascii=False, default=str)
        self.refs.append(shell)
        self.redactions[call_id] = text
        return text

    def _register_fail_closed(self, call_id: str, payload: dict[str, Any] | None,
                              cap: int | None) -> str:
        if isinstance(payload, dict) and "error" in payload:
            # 执行异常回灌：保留有界错误码/消息供排查，载荷不落。
            error = payload.get("error")
            code = payload.get("code")
            shell = {
                "error": bounded_error_text(error),
                "ephemeralDetail": True,
                "note": "远程上下文中执行异常，错误载荷按未落库处理。",
            }
            if code is not None:
                shell["code"] = code
        else:
            shell = _minimal_detail_shell(payload)
            self.detail_seen = True
        text = json.dumps(shell, ensure_ascii=False, default=str)
        self.refs.append(shell)
        self.redactions[call_id] = text
        return text


def _parse_output_json(output: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(str(output or ""))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


# ------------------------------------------------------ JSON 感知截断


def truncate_tool_output(output: str, cap: int) -> str:
    """工具结果字符上限的 JSON 感知截断。

    合法 JSON 对象 → 收缩记录数组到上限内，保留 counts/cursor/
    completeness 等元数据，并把 ``truncated`` 置 true、完整度声明降级
    （绝不把截断后的明细标成 complete）；非 JSON 或无法以 JSON 收缩 →
    保持旧的字符截断行为（同后缀）。
    """
    text = str(output or "")
    if len(text) <= cap:
        return text
    legacy = text[:cap] + "\n…[结果已截断]"
    payload = _parse_output_json(text)
    if payload is None:
        return legacy
    trimmed = _shrink_record_arrays(payload, cap)
    return trimmed if trimmed is not None else legacy


def _shrink_record_arrays(payload: dict[str, Any], cap: int) -> str | None:
    arrays = {
        key: value for key, value in payload.items()
        if key in _RECORD_ARRAY_KEYS and isinstance(value, list)
    }
    if not arrays:
        return None
    base = {key: value for key, value in payload.items() if key not in arrays}
    # 我们截掉了记录行：不得继续宣称结果完整。
    base["truncated"] = True
    if base.get("completeness") == "complete":
        base["completeness"] = "partial"
    base["truncatedBy"] = "super_assistant_context_cap"
    keys = sorted(arrays)
    longest = max(len(value) for value in arrays.values())

    def render(count: int) -> str:
        doc = dict(base)
        for key in keys:
            values = arrays[key]
            kept = values[:count]
            doc[key] = kept
            omitted = len(values) - len(kept)
            if omitted > 0:
                doc[f"{key}Returned"] = len(kept)
                doc[f"{key}Omitted"] = omitted
        return json.dumps(doc, ensure_ascii=False, default=str)

    if len(render(0)) > cap:
        return None
    lo, hi, best = 0, longest, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(render(mid)) <= cap:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return render(best)


# ------------------------------------------------------ 答复壳


def assistant_answer_shell(state: EphemeralTurnState) -> str:
    """本轮出现过 ephemeral detail 时，assistant 消息的落库内容壳。

    SSE 实时流仍给模型全文；历史、摘要、反思、导出只看到本壳。明细的
    继续查看经既有 ontology_query cursor 通道（不新造查询执行器）。
    """
    lines = [
        "【本轮答复包含远程联邦明细，按 ephemeral 处理未落库】",
        "完整答复已在当轮实时展示；历史视图仅保留以下受控元信息。",
    ]
    for index, ref in enumerate(state.refs, 1):
        lines.append(
            f"- 查询引用 {index}："
            + json.dumps(ref, ensure_ascii=False, default=str)
        )
    for index, agg in enumerate(state.aggregates, 1):
        lines.append(
            f"- 服务端聚合 {index}："
            + json.dumps(agg, ensure_ascii=False, default=str)
        )
    lines.append("- 需要继续查看明细时，请在后续对话中带上 cursor 重新发起查询。")
    return "\n".join(lines)
