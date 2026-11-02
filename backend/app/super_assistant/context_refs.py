"""跨轮结构引用投影（纯函数、零迁移、只读）。

只从当前会话最近的历史 ``ontology_*`` ToolRun.result 中提取 allow-list
结构 ID（``releases`` / ``sourceRefs`` / 目录条目），去重保留最新，并渲染
为系统提示中的可选段。禁止行值、列值、筛选取值、params、citations、
cursor、queryId、计数、plan 等任何明细进入投影；不写持久面、不改导出。

授权不在此模块：调用方必须先经本体门面按当前 user 校验访问（见
``ontology_tools.authorize_structure_refs``），本模块只做纯投影。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

MAX_TOOL_RUNS = 20
MAX_REFS = 32
MAX_SECTION_CHARS = 4000
MAX_STRING_CHARS = 128
MAX_VERSION_NUMBER = 2_000_000_000
_DATA_MODES = frozenset({"remote", "materialized"})

_REF_KEYS = (
    "ontologyId", "releaseId", "objectTypeId", "versionNumber",
    "dataMode", "resolvedAt", "sourceTool", "sourceToolRunId",
)
_SOURCE_PATHS = ("releases", "sourceRefs")


def _bounded_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_STRING_CHARS:
        return None
    if any(ord(char) < 32 for char in text):
        return None
    return text


def _bounded_version(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_VERSION_NUMBER:
        return None
    return value


def _payload(output: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(str(output or ""))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _ref_from_source(source: Any, *, tool_name: str, tool_run_id: str,
                     resolved_at: str) -> dict[str, Any] | None:
    """单个结构来源 → allow-list 引用；任一必需 ID 非法即丢弃。"""
    if not isinstance(source, dict):
        return None
    ontology_id = _bounded_text(source.get("ontologyId"))
    release_id = _bounded_text(source.get("releaseId"))
    if not ontology_id or not release_id:
        return None
    ref: dict[str, Any] = {
        "ontologyId": ontology_id,
        "releaseId": release_id,
    }
    object_type_id = _bounded_text(source.get("objectTypeId"))
    if object_type_id:
        ref["objectTypeId"] = object_type_id
    version_number = _bounded_version(source.get("versionNumber"))
    if version_number is not None:
        ref["versionNumber"] = version_number
    if "dataMode" in source:
        # 枚举字段存在即必须合法：未知 dataMode 视为不可信条目整条丢弃。
        if source.get("dataMode") not in _DATA_MODES:
            return None
        ref["dataMode"] = source["dataMode"]
    if "versionNumber" in source and source.get("versionNumber") is not None:
        if version_number is None:
            return None
    ref["resolvedAt"] = resolved_at
    ref["sourceTool"] = tool_name
    ref["sourceToolRunId"] = tool_run_id
    return ref


def _iter_sources(payload: dict[str, Any]) -> Iterable[Any]:
    for key in _SOURCE_PATHS:
        value = payload.get(key)
        if isinstance(value, dict):
            yield from value.values()
        elif isinstance(value, list):
            yield from value
    for key in ("objectTypes", "linkTypes", "items"):
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            source_refs = item.get("sourceRefs")
            if isinstance(source_refs, dict):
                yield source_refs
            elif key == "items":
                # 目录条目：本体/发布成对即结构引用（无对象类型 ID）。
                yield {"ontologyId": item.get("ontologyId"),
                       "releaseId": item.get("releaseId")}
    # 目录文档/对象类型索引的顶层本体+发布引用
    if payload.get("ontologyId") and payload.get("releaseId"):
        yield {"ontologyId": payload.get("ontologyId"),
               "releaseId": payload.get("releaseId")}


def _resolved_at(record: dict[str, Any]) -> str:
    value = record.get("createdAt")
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    if isinstance(value, str):
        return value[:64]
    return ""


def project_tool_runs(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """最近 ToolRun 记录 → 去重、有界的 allow-list 结构引用。

    输入应按“新 → 旧”排序，只读取最近 ``MAX_TOOL_RUNS`` 条；同一结构键保留
    最新来源；最多 ``MAX_REFS`` 条。
    """
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, record in enumerate(records):
        if index >= MAX_TOOL_RUNS:
            break
        if not isinstance(record, dict):
            continue
        tool_name = str(record.get("toolName") or "")
        tool_run_id = _bounded_text(record.get("toolRunId")) or ""
        payload = _payload(record.get("result"))
        if payload is None:
            continue
        resolved_at = _resolved_at(record)
        for source in _iter_sources(payload):
            ref = _ref_from_source(
                source, tool_name=tool_name,
                tool_run_id=tool_run_id, resolved_at=resolved_at)
            if ref is None:
                continue
            key = (ref["ontologyId"], ref["releaseId"],
                   ref.get("objectTypeId") or "")
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
            if len(refs) >= MAX_REFS:
                return refs
    return refs


def render_structure_refs(refs: Iterable[dict[str, Any]]) -> str:
    """渲染系统提示可选段；段总长 ≤ MAX_SECTION_CHARS，不含明细。"""
    entries = [ref for ref in refs if isinstance(ref, dict)]
    if not entries:
        return ""
    header = (
        "【本会话历史结构引用（只读投影，非权威，非授权）】\n"
        "以下结构 ID 只来自当前会话历史查询结果，可能已经过期；使用前必须"
        "重新经 ontology_catalog / ontology_structure 校验当前发布与访问权，"
        "不得跨会话使用，也不得把它们当作查询结果、权限或可用句柄。"
        "标记 stale=true 的条目发布已变化，须重新发现后再查询；历史引用"
        "不参与授权或查询构造。"
    )
    lines: list[str] = []
    for ref in entries[:MAX_REFS]:
        rendered = {key: ref[key] for key in _REF_KEYS if key in ref}
        stale = bool(ref.get("stale"))
        if stale:
            rendered["stale"] = True
        line = "- " + json.dumps(rendered, ensure_ascii=False, sort_keys=True)
        if stale:
            line += "（发布已变化：必须重新发现后再使用）"
        candidate = header + "\n" + "\n".join([*lines, line])
        if len(candidate) > MAX_SECTION_CHARS:
            if not lines:
                # 单条即超限：只保留头部，宁缺勿泄/勿截断非法 JSON 片段。
                return header[:MAX_SECTION_CHARS]
            break
        lines.append(line)
    if not lines:
        return header[:MAX_SECTION_CHARS]
    return header + "\n" + "\n".join(lines)
