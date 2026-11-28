"""Turn-local coverage failures and conservative evidence for partial replies."""
from __future__ import annotations

import re

from app.ontologies.agent_runtime.answer_evidence import complete_result
from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.query_coverage import MAX_GAPS
from app.ontologies.agent_runtime.tool_execution import tool_call_key


_TOTAL = re.compile(
    r"(?:(?:请)?(?:问|查(?:询)?|查看|统计|告诉我)|请|先查)?\s*(?P<name>.+?)\s*"
    r"(?:的)?(?:总数|总量|总条数|(?:一共|总共|共有|有)?多少(?:条|笔|个)?)(?:是多少|是什么)?")
_EN_TOTAL = re.compile(r"(?:total(?: number)? of|how many)\s+(?P<name>.+)", re.IGNORECASE)
# A conjunct or strong sentence boundary establishes a separate request. A
# bare comma could instead introduce a qualifier on the preceding total.
_SEPARATE = re.compile(
    r"[，,\s]*(?:并(?:且)?|另外|以及|再(?:请)?|\band\b)|[。；;！？!?\n]+", re.IGNORECASE)
_GROUP_ASK = re.compile(
    r"(?:请)?(?:按|按照|根据)\s*[^\s，,。；;！？!?\n]{1,40}\s*(?:拆分|分组|统计|汇总|分类)"
    r"|(?:break\s+(?:it\s+)?down|group)\s+by\s+[\w -]{1,40}", re.IGNORECASE)


def _requested_total(question: str, scope):
    first = _SEPARATE.split(question.strip().rstrip("。！？!?"), maxsplit=1)
    # A second clause that qualifies the total (e.g. 并且只算有供应商的)
    # does not establish an independent question.
    if len(first) != 2 or not _GROUP_ASK.fullmatch(first[1].strip()):
        return None
    match = _TOTAL.fullmatch(first[0].strip()) or _EN_TOTAL.fullmatch(first[0].strip())
    if not match:
        return None
    name = match["name"].strip(" \"'「」‘’“”").casefold()
    matches = [ot for ot in scope.object_types.values() if name in {
        str(ref).casefold() for ref in (ot.name, ot.display_name, ot.id) if ref}]
    return matches[0] if len(matches) == 1 else None


def _whole_count_type(call: dict, scope) -> str | None:
    """Recognize only unfiltered row counts, never a count of a field or join."""
    args = call.get("arguments") or {}
    tool = call["name"]
    if tool == "aggregate_objects":
        if (set(args) - {"object_type", "metric", "filters", "group_by", "metric_property"}
                or (args.get("metric") or "count") != "count"
                or any(args.get(key) for key in ("filters", "group_by", "metric_property"))):
            return None
    elif tool == "query_objects":
        if set(args) - {"object_type", "aggregates", "page_size", "filters", "joins", "group_by"}:
            return None
        aggregates = args.get("aggregates") or []
        if (len(aggregates) != 1 or not isinstance(aggregates[0], dict)
                or aggregates[0].get("fn") != "count"
                or set(aggregates[0]) - {"fn", "as", "property"}
                or aggregates[0].get("property")
                or any(args.get(key) for key in ("filters", "joins", "group_by"))):
            return None
    else:
        return None
    try:
        return scope.require_object_type(args.get("object_type", "")).id
    except ToolError:
        return None


def _complete(result: dict) -> bool:
    coverage = result.get("dataCoverage") or {}
    return (complete_result(result)
            and not (coverage.get("gaps") or coverage.get("gapsOmitted"))
            and not any(result.get(key) for key in (
                "hasNext", "truncated", "budgetHit", "frontierCapped")))


def _count_value(call: dict, result: dict):
    if call["name"] == "aggregate_objects":
        if result.get("metric") != "count" or result.get("groupBy"):
            return None
        value = result.get("value")
    else:
        rows = result.get("rows") or []
        label = call["arguments"]["aggregates"][0].get("as") or "count"
        value = rows[0].get(label) if len(rows) == 1 else None
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return value
    return None


class TurnCoverage:
    def __init__(self, scope, question: str):
        self.scope = scope
        self.requested_total = _requested_total(question, scope)
        self.blocked: dict = {}
        self.pending: dict[tuple[str, str], dict] = {}
        self.counts: dict[str, int | str] = {}
        self.seen_blocked: set[tuple[str, str]] = set()

    def observe(self, call: dict, result: dict) -> None:
        query_key = tool_call_key(call)
        count_type = _whole_count_type(call, self.scope)
        # Cross-tool equivalence is proved only for a whole-type row count.
        # A first-page size is transport metadata, but the successful retry
        # must still have the complete result. Never normalize cursor requests.
        args = call.get("arguments") or {}
        if count_type:
            key = ("whole_count", count_type)
        elif call["name"] == "query_objects" and not args.get("cursor"):
            key = tool_call_key({**call, "arguments": {
                arg: value for arg, value in args.items() if arg != "page_size"}})
        else:
            key = query_key
        coverage = result.get("dataCoverage") or {}
        if coverage.get("gaps") or coverage.get("gapsOmitted"):
            if self.blocked.get(query_key) is result:
                if query_key in self.seen_blocked:
                    return
                self.seen_blocked.add(query_key)
            self.pending[key] = coverage
            if count_type:
                self.counts.pop(count_type, None)
        elif _complete(result):
            if count_type:
                value = _count_value(call, result)
                if value is None:
                    return
                self.counts[count_type] = value
            self.pending.pop(key, None)

    def render(self) -> str:
        lines = []
        ot = self.requested_total
        if ot is not None and ot.id in self.counts:
            lines.append(f"当前授权查询范围内，{ot.display_name or ot.name}总数为 "
                         f"{self.counts[ot.id]} 条。")

        lines.append("以下查询范围未能核实，实际数量仍未知。")
        entries = {}
        omitted = 0
        for coverage in self.pending.values():
            omitted += coverage.get("gapsOmitted", 0)
            for gap in coverage.get("gaps") or []:
                kind = gap.get("kind")
                label = gap.get("label") or "当前查询"
                subject = (f"关系「{label}」" if kind == "linkType" else
                           f"对象「{label}」的属性「{gap.get('property', '')}」" if kind == "property" else
                           f"对象「{label}」" if kind == "objectType" else "数据源")
                code = gap.get("code")
                reason = ("尚无可用的数据映射或本地事实" if code in {
                    "object_data_unmapped", "property_data_unmapped", "relation_data_unmapped",
                    "remote_property_unmapped", "remote_relation_unmapped",
                } else "本次查询未完成" if kind == "source" or code in {
                    "SOURCE_UNAVAILABLE", "SOURCE_SCHEMA_DRIFT", "SOURCE_CONFIG_UNAVAILABLE",
                    "query_source_failed", "remote_query_unavailable", "query_coverage_unavailable",
                } else "当前查询路径不可用")
                entries[(subject, reason)] = f"- {subject}{reason}。"
        lines.extend(list(entries.values())[:MAX_GAPS])
        if omitted or len(entries) > MAX_GAPS:
            lines.append("还有其他未覆盖项，未逐项列出。")
        return "\n".join(lines)
