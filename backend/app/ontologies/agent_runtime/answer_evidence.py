"""答案证据生命周期：可信历史统计结果、修正回环可见证据与修正采纳判定。

历史证据只接受白名单工具产生的、可结构复验的统计 query 结果：
- ``aggregate_objects`` / ``query_objects``；
- 必须携带 ``querySpec`` 且为 objects 形态的统计计划（groupBy/aggregates）；
- 计划中的每个 source 与每个 source.property 都按当前 scope 逐源复验，
  不使用跨对象类型的同名属性并集；
- 必须当前 ontology/release 一致；SPL、派生 source、graph/path、sequence、
  无 spec 结果一律不作为历史证据。

证据不来自 assistant 文本，也不新增任何落库字段；远程明细仍按既有审计口径
脱敏，本模块只读取已持久化的工具结果。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from app.ontologies.agent_runtime.answer_verifier import VerificationResult
from app.ontologies.agent_runtime.context_policy import tool_payload
from app.ontologies.agent_runtime.limits import limit

_HISTORICAL_TOOLS = frozenset({"aggregate_objects", "query_objects"})
_AGGREGATE_SEMANTICS = frozenset({
    "aggregate_scalar", "aggregate_grouped", "grouped_aggregate",
    "distinct_subject_count",
})


def complete_result(result: Any) -> bool:
    """完整成功结果才能作为数值证据；预览截断/部分/错误结果一律不算。"""
    return isinstance(result, dict) and not (
        result.get("error")
        or result.get("_truncated")
        or result.get("llmPreviewTruncated")
        or result.get("partial")
        or result.get("exact") is False
        or result.get("completeness", "complete") != "complete"
    )


def _field(item: Any, name: str, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _release_matches(value, scope) -> bool:
    return str(value) == str(getattr(scope, "release_id", None))


def _sources(query: dict, scope) -> Optional[dict[str, Any]]:
    sources: dict[str, Any] = {}
    for raw in query.get("sources") or []:
        if not isinstance(raw, dict) or raw.get("subquery") is not None:
            return None
        alias = str(raw.get("alias") or "")
        if not alias:
            return None
        if str(raw.get("ontologyId")) != str(scope.ontology.id):
            return None
        if not _release_matches(raw.get("releaseId"), scope):
            return None
        object_type = scope.object_types.get(str(raw.get("objectTypeId") or ""))
        if object_type is None:
            return None
        sources[alias] = object_type
    return sources or None


def _property_visible(scope, object_type, prop: str) -> bool:
    """只在 source 自己的对象类型上解析属性，禁止跨类型同名字段兜底。"""
    for item in getattr(object_type, "properties", None) or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        if prop not in (item.get("name"), item.get("displayName"),
                        item.get("display_name")):
            continue
        allowed = scope.allowed_property_names(object_type.id)
        return allowed is None or item["name"] in allowed
    return False


def _filter_visible(node: Any, sources: dict, scope) -> bool:
    if not isinstance(node, dict):
        return False
    for key in ("and", "or"):
        if key in node:
            return bool(node[key]) and all(
                _filter_visible(child, sources, scope) for child in node[key])
    if "not" in node:
        return _filter_visible(node["not"], sources, scope)
    source = sources.get(str(node.get("source") or ""))
    prop = str(node.get("property") or "")
    return source is not None and bool(prop) and _property_visible(
        scope, source, prop)


def _query_visible(query: Any, scope) -> bool:
    """结构树逐源复验；无法证明作用域的形态直接拒绝。"""
    if not isinstance(query, dict) or query.get("spl"):
        return False
    sources = _sources(query, scope)
    if sources is None:
        return False
    for item in query.get("select") or []:
        if not isinstance(item, dict):
            return False
        source = sources.get(str(item.get("source") or ""))
        if source is None or not _property_visible(
                scope, source, str(item.get("property") or "")):
            return False
    for item in query.get("groupBy") or []:
        if not isinstance(item, dict):
            return False
        source = sources.get(str(item.get("source") or ""))
        if source is None or not _property_visible(
                scope, source, str(item.get("property") or "")):
            return False
    for item in query.get("aggregates") or []:
        if not isinstance(item, dict):
            return False
        prop = item.get("property")
        if not prop:
            continue
        source = sources.get(str(item.get("source") or ""))
        if source is None or not _property_visible(scope, source, str(prop)):
            return False
    for item in query.get("sourceFilters") or []:
        if not isinstance(item, dict) or not _filter_visible(
                item.get("filter"), sources, scope):
            return False
    for node in query.get("where") or []:
        if not _filter_visible(node, sources, scope):
            return False
    for join in query.get("joins") or []:
        on = join.get("on") if isinstance(join, dict) else None
        if not isinstance(on, dict):
            return False
        if on.get("kind") == "declared_relation":
            link_types = getattr(scope, "link_types", None) or {}
            if str(on.get("linkTypeId") or "") not in link_types:
                return False
            continue
        if on.get("kind") != "explicit_key":
            return False
        for alias, prop in ((join.get("left"), on.get("leftProperty")),
                            (join.get("right"), on.get("rightProperty"))):
            source = sources.get(str(alias or ""))
            if source is None or not _property_visible(
                    scope, source, str(prop or "")):
                return False
    return True


def _trusted_statistical_result(result: dict, scope) -> Optional[dict]:
    if scope is None or result.get("querySpec") is None:
        return None
    if not _release_matches(result.get("releaseId", scope.release_id), scope):
        return None
    ontology_id = result.get("ontologyId")
    if ontology_id is not None and str(ontology_id) != str(scope.ontology.id):
        return None
    spec = result["querySpec"]
    if not isinstance(spec, dict) or spec.get("shape") != "objects":
        return None
    if str(spec.get("ontologyId")) != str(scope.ontology.id):
        return None
    if not _release_matches(spec.get("releaseId"), scope):
        return None
    object_type_id = spec.get("objectType")
    if object_type_id is not None and str(object_type_id) not in scope.object_types:
        return None
    query = spec.get("query")
    semantics = ((spec.get("description") or {}).get("semantics")
                 if isinstance(spec.get("description"), dict) else None)
    if not (isinstance(query, dict)
            and (query.get("groupBy") or query.get("aggregates")
                 or semantics in _AGGREGATE_SEMANTICS)):
        return None
    if not _query_visible(query, scope):
        return None
    drilldown = spec.get("drilldownBase")
    if drilldown is not None and not _query_visible(drilldown, scope):
        return None
    return result


def trusted_history_steps(messages: Iterable[Any], scope) -> list[dict]:
    """同一会话历史消息中可复用的统计工具证据（忽略 assistant 文本本身）。"""
    evidence: list[dict] = []
    for message in messages or []:
        if _field(message, "role") != "assistant":
            continue
        for step in _field(message, "steps", None) or []:
            if not isinstance(step, dict) or step.get("tool") not in _HISTORICAL_TOOLS:
                continue
            result = step.get("result")
            if not complete_result(result):
                continue
            trusted = _trusted_statistical_result(result, scope)
            if trusted is not None:
                evidence.append({"tool": step.get("tool"), "result": trusted,
                                 "source": "history"})
    return evidence


def evidence_digest(steps: Iterable[dict], *, title: str,
                    cap: Optional[int] = None) -> str:
    """把当前轮与可信历史证据整理成有界、可读的上下文片段。

    每个结果走 context_policy.tool_payload：剥离传输元数据，并在预算允许时
    保留 Top10 或月度 12 行。Top-N 截断的列表不代表全量，摘要也不得声称全量。
    """
    cap = cap or limit("tool_result_llm_cap")
    per_step = max(limit("tool_result_llm_cap") // 4, 1_000)
    lines = [f"# {title}"]
    seen: set[str] = set()
    used = len(lines[0])
    for step in steps:
        if not complete_result(step.get("result")):
            continue
        payload = tool_payload(step["result"], cap=per_step)
        if payload in seen:
            continue
        seen.add(payload)
        label = "历史轮" if step.get("source") == "history" else "本轮"
        line = f"[{label}] {step.get('tool') or 'tool'}: {payload}"
        if used + len(line) > cap:
            break
        lines.append(line)
        used += len(line) + 1
    if len(lines) == 1:
        return ""
    lines.append("以上数字均来自当前授权范围内的工具结果；Top-N/分组列表可能已截断，"
                 "未返回项不代表不存在或为零。")
    return "\n".join(lines)


@dataclass(frozen=True)
class CorrectionChoice:
    answer: str
    verification: VerificationResult
    reverted: bool


def choose_correction(original_answer: str, original: VerificationResult,
                      corrected_answer: str,
                      corrected: VerificationResult) -> CorrectionChoice:
    """修正答案若丢失原答案中已核验的数字，则保留原文（并交由调用方加 notice）。

    空泛的“无法核验”修正不能覆盖原本有证据的答案；只有保留全部已核验数字
    的修正才被采纳。
    """
    preserved = {number for number in corrected.matched}
    if corrected_answer and all(number in preserved for number in original.matched):
        return CorrectionChoice(corrected_answer, corrected, False)
    return CorrectionChoice(original_answer, original, True)
