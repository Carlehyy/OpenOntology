"""Bounded ontology discovery, conversation context and structured tool previews."""
from __future__ import annotations

import json
from typing import Any

from app.ontologies.agent_runtime.boundary import remote_object_type_ids
from app.ontologies.agent_runtime.limits import limit


def ontology_catalog(scope) -> str:
    remote_ids = remote_object_type_ids(scope)
    budget = min(16_000, limit("context_chars_cap") // 3)
    lines = [f"# 本体「{scope.ontology.name}」授权目录", f"releaseId={scope.release_id}"]
    used = sum(map(len, lines))
    types = list(scope.object_types.values())
    shown = 0
    for item in types:
        allowed = scope.allowed_property_names(item.id)
        props = [p for p in (item.properties or []) if isinstance(p, dict)
                 and p.get("name") and (allowed is None or p["name"] in allowed)]
        fields = ", ".join(f"{p['name']}:{p.get('type', 'string')}" for p in props[:6])
        mode = "远程绑定" if item.id in remote_ids else "本地物化"
        line = f"- {item.display_name} ({item.name}, id={item.id}, {mode}): {fields}"
        if len(props) > 6:
            line += f"；另有 {len(props) - 6} 个授权属性，先 describe_objects"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
        shown += 1
    if shown < len(types):
        lines.append(f"目录仅展示 {shown}/{len(types)} 类；用 describe_objects 分页发现其余类型。")
    lines.append("关系、完整属性、可用动作通过 describe_objects 按需发现；目录不声明实例数量。")
    return "\n".join(lines)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def tool_payload(result: dict, *, cap: int | None = None) -> str:
    """Preview truncation never changes the underlying query completeness."""
    cap = cap or limit("tool_result_llm_cap")
    if len(payload := _json(result)) <= cap:
        return payload
    preview = dict(result)
    omitted: dict[str, int] = {}
    for key, value in list(preview.items()):
        if isinstance(value, list) and len(value) > 5:
            omitted[key] = len(value) - 5
            preview[key] = value[:5]
    preview["llmPreviewTruncated"] = True
    preview["omittedItems"] = omitted
    if len(payload := _json(preview)) <= cap:
        return payload
    for key in ("querySpec", "cursor", "html", "htmlContent", "templateSnapshot"):
        preview.pop(key, None)
    for key, value in list(preview.items()):
        if isinstance(value, list):
            preview[key] = value[:1]
        elif isinstance(value, str) and len(value) > 512:
            preview[key] = value[:512]
    if len(payload := _json(preview)) <= cap:
        return payload
    metadata = {key: result[key] for key in (
        "kind", "objectType", "returned", "total", "totalRowCount", "rowCounting",
        "hasNext", "exact", "partial", "completeness", "completenessScope",
        "dataCoverage", "resolvedWindow", "observedAt", "error", "value",
    ) if key in result}
    metadata["llmPreviewTruncated"] = True
    metadata["notice"] = "结果超出上下文预算；请缩小投影或查询范围，完整明细在查询视图查看。"
    if len(payload := _json(metadata)) <= cap:
        return payload
    return _json({"llmPreviewTruncated": True,
                  "notice": "结果超出上下文预算；不能据此推断业务事实，请缩小查询范围。"})


def budget_messages(messages: list[dict], *, cap: int | None = None) -> list[dict]:
    """Keep user turns and assistant/tool call pairs intact under a character budget."""
    cap = cap or limit("context_chars_cap")
    bounded = [dict(message) for message in messages]
    while len(_json(bounded)) > cap:
        user_positions = [i for i, message in enumerate(bounded) if message.get("role") == "user"]
        if len(user_positions) > 1:
            del bounded[user_positions[0]:user_positions[1]]
            continue
        tool_positions = [i for i, message in enumerate(bounded) if message.get("role") == "tool"
                          and len(str(message.get("content") or "")) > 1_000]
        if not tool_positions:
            break
        index = tool_positions[0]
        try:
            data = json.loads(bounded[index]["content"])
        except (TypeError, ValueError):
            data = {"notice": "较早的工具输出过长，请按需要重新读取。"}
        bounded[index]["content"] = tool_payload(data, cap=1_000)
    return bounded
