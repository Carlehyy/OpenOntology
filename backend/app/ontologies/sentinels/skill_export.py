"""哨兵 Skill 包导出：确定性模板化渲染，不使用 LLM。

导出物遵循平台标准 Skill 包契约（权威实现见 ``super_assistant/skill_store.py``，
生产代码不反向依赖该域，契约兼容由测试侧 round-trip 验收）：
根级 ``SKILL.md``（frontmatter 仅 name/description）+ ``references/`` 保真资源。

取数口径：
- 公共哨兵（release_builtin）：定义冻结在当前发布快照 ``snapshot["sentinels"]``，
  live 行仅叠加 enabled/muted 运营态（同 ``query_service._released_dict``）；
- 动态哨兵（assistant_dynamic）：live 行定义（过滤已退役行）；
- 业务文档：当前发布版 ``snapshot_semantic.documentMd``（语义层契约见
  ``published_documents.py``，此处只读该 JSON 列，不引入事件派发依赖）。
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Any
from urllib.parse import quote

import yaml
from fastapi import HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.models.sentinel import Sentinel
from app.ontologies.release_context import (
    CurrentReleaseContext,
    current_release_context,
)
from app.ontologies.sentinels.dynamic_service import (
    ORIGIN_BUILTIN,
    ORIGIN_DYNAMIC,
    definition_from_row,
)
from app.ontologies.sentinels.query_service import _released_dict
from app.ontologies.versions.snapshot_contract import snapshot_models

# 标准 Skill 包契约的 name/description 约束（与 skill_store 保持一致）。
_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SKILL_NAME_LIMIT = 64
_SKILL_DESCRIPTION_LIMIT = 4000

_DEFINITION_KEYS = (
    "id", "ontologyId", "name", "displayName", "description", "origin",
    "bindings", "links", "condition", "conditionRows", "conditionLogic",
    "primaryAlias", "actionIds", "actionParameters", "onChange", "onSchedule",
    "scanIntervalSeconds", "triggerMode", "pattern", "muted", "enabled",
)

_TRIGGER_MODE_LABELS = {
    "on_enter": "on_enter · 仅当实例新进入匹配集时触发",
    "on_enter_leave": "on_enter_leave · 实例进入或离开匹配集时触发",
    "run_on_all": "run_on_all · 每轮对全部命中实例执行",
    "on_pattern": "on_pattern · 按事件模式（CEP）触发",
}

_ORIGIN_LABELS = {
    ORIGIN_BUILTIN: "公共哨兵",
    ORIGIN_DYNAMIC: "动态哨兵",
}


def _skill_slug(name: str, sentinel_id: str) -> str:
    """技术名归一化为 Skill name；中文/空名时回退到稳定短 id。"""
    candidates = (str(name or ""), f"sentinel {str(sentinel_id)[:8]}")
    for candidate in candidates:
        slug = re.sub(r"[^a-z0-9-]+", "-", candidate.strip().lower())
        slug = re.sub(r"-{2,}", "-", slug).strip("-")
        slug = slug[:_SKILL_NAME_LIMIT].strip("-")
        if slug and _SKILL_NAME_PATTERN.fullmatch(slug):
            return slug
    return "sentinel-skill"


def _builtin_definition(
    db: Session,
    ontology_id: str,
    sentinel_id: str,
    context: CurrentReleaseContext,
) -> dict[str, Any] | None:
    raw = next(
        (
            item
            for item in context.snapshot["sentinels"]
            if isinstance(item, dict) and str(item.get("id") or "") == sentinel_id
        ),
        None,
    )
    if raw is None:
        return None
    live = (
        db.query(Sentinel)
        .filter(
            Sentinel.id == sentinel_id,
            Sentinel.ontology_id == ontology_id,
            Sentinel.origin == ORIGIN_BUILTIN,
        )
        .first()
    )
    released = _released_dict(ontology_id, context.id, raw, live)
    return {key: released[key] for key in _DEFINITION_KEYS}


def _dynamic_definition(
    db: Session,
    ontology_id: str,
    sentinel_id: str,
) -> dict[str, Any]:
    row = (
        db.query(Sentinel)
        .filter(
            Sentinel.id == sentinel_id,
            Sentinel.ontology_id == ontology_id,
            Sentinel.origin == ORIGIN_DYNAMIC,
            Sentinel.retired_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(404, "哨兵不存在或已退役")
    return {
        "id": row.id,
        "ontologyId": row.ontology_id,
        "origin": row.origin,
        **definition_from_row(row),
        "enabled": bool(row.enabled),
    }


def _resolve_actions(
    context: CurrentReleaseContext,
    action_ids: list[str],
) -> list[dict[str, Any]]:
    by_id = {
        item.id: item
        for item in snapshot_models(context.snapshot)["actions"]
    }
    resolved: list[dict[str, Any]] = []
    for action_id in action_ids:
        action = by_id.get(str(action_id))
        if action is None:
            # 快照里不存在的动作引用如实保留（fail-closed），不静默省略。
            resolved.append({"id": str(action_id), "available": False})
            continue
        resolved.append({
            "id": action.id,
            "name": action.name,
            "displayName": action.display_name,
            "description": action.description,
            "objectTypeId": action.object_type_id,
            "parameters": action.parameters,
            "rules": action.rules,
            "requiresApproval": bool(action.requires_approval),
            "available": True,
        })
    return resolved


def _business_document(
    context: CurrentReleaseContext,
) -> tuple[str, str]:
    semantic = context.release.snapshot_semantic
    if not isinstance(semantic, dict):
        return "", ""
    document_md = str(semantic.get("documentMd") or "").strip()
    if not document_md:
        return "", ""
    title = (
        str(semantic.get("documentTitle") or "").strip()
        or f"{context.project.name} 业务文档"
    )
    return document_md, title


def _json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _json_inline(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _describe_parameter(value: Any) -> str:
    """actionParameters 参数绑定四类形态的如实描述（结构见 evaluator）。"""
    if isinstance(value, dict) and value.get("sourceType"):
        source = str(value.get("sourceType")).lower().replace("-", "_")
        if source in {"constant", "literal"}:
            inner = value.get("value", value.get("sourceValue"))
            return f"常量绑定（constant）：{_json_inline(inner)}"
        if source in {"match_property", "match", "property"}:
            alias = value.get("alias") or "primary"
            return (
                "命中实例属性绑定（match-property）："
                f"别名 `{alias}` 的属性 `{value.get('property')}`"
            )
        if source in {"target_id", "primary_id"}:
            alias = value.get("alias") or "primary"
            return f"命中实例 ID 绑定（target-id）：别名 `{alias}`"
        if source == "event":
            return f"触发事件字段绑定（event）：`{value.get('property')}`"
    if isinstance(value, str) and "{{" in value and "}}" in value:
        return (
            "属性插值模板：`" + value + "`"
            "（{{alias.property}} 引用命中实例属性）"
        )
    return f"字面量：{_json_inline(value)}"


def _trigger_summary(definition: dict[str, Any]) -> str:
    labels = []
    if isinstance(definition.get("pattern"), dict) or (
        definition.get("triggerMode") == "on_pattern"
    ):
        labels.append("事件模式")
    if definition.get("onChange"):
        labels.append("数据变更触发")
    if definition.get("onSchedule"):
        labels.append("定时扫描")
    return " / ".join(labels) if labels else "仅手动运行"


def _skill_description(
    ontology_name: str, definition: dict[str, Any],
) -> str:
    display = definition.get("displayName") or definition.get("name") or "哨兵"
    origin_label = _ORIGIN_LABELS.get(
        str(definition.get("origin")), str(definition.get("origin")),
    )
    summary = str(definition.get("description") or "").strip()
    summary = summary.splitlines()[0][:200] if summary else ""
    description = (
        f"OpenOntology 哨兵 Skill：{ontology_name} · {display}"
        f"（{origin_label}；{_trigger_summary(definition)}）。{summary}"
    ).replace("\n", " ")
    return description[:_SKILL_DESCRIPTION_LIMIT].strip() or (
        f"OpenOntology 哨兵 Skill：{ontology_name} · {display}"
    )


def _render_binding_lines(definition: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    bindings = definition.get("bindings") or []
    if not bindings:
        lines.append("- 无绑定（导出时如实为空）。")
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        lines.append(
            f"- 别名 `{binding.get('alias')}` → 对象类型 "
            f"`{binding.get('objectTypeId')}`"
        )
        if binding.get("filter"):
            lines.append(f"  - 过滤条件：`{_json_inline(binding['filter'])}`")
    for link in definition.get("links") or []:
        if not isinstance(link, dict):
            continue
        lines.append(
            f"- 关系约束：`{link.get('from')}` —[`{link.get('linkTypeId')}`]→ "
            f"`{link.get('to')}`"
        )
    return lines


def _render_action_lines(
    definition: dict[str, Any], actions: list[dict[str, Any]],
) -> list[str]:
    lines: list[str] = []
    if not actions:
        lines.append("- 无处置动作（命中仅记录触发日志）。")
        return lines
    parameter_bindings_all = definition.get("actionParameters") or {}
    for action in actions:
        if not action.get("available"):
            lines.append(
                f"- ⚠ 动作 `{action['id']}` 在当前发布快照中不可用，"
                "导出时如实保留引用。"
            )
            continue
        approval = "需人工审批" if action.get("requiresApproval") else "无需审批"
        scope = (
            f"，作用对象 `{action['objectTypeId']}`"
            if action.get("objectTypeId") else ""
        )
        lines.append(
            f"- **{action.get('displayName')}**"
            f"（`{action.get('name')}` · `{action['id']}`）：{approval}{scope}"
        )
        if action.get("description"):
            lines.append(f"  - 描述：{action['description']}")
        for parameter in action.get("parameters") or []:
            required = "必填" if parameter.get("required") else "可选"
            lines.append(
                f"  - 参数定义 `{parameter.get('name')}`"
                f"（{parameter.get('type')} · {required}）"
            )
        bindings = parameter_bindings_all.get(str(action["id"])) or {}
        if bindings:
            lines.append("  - 参数取值绑定：")
            for parameter_name in sorted(bindings):
                lines.append(
                    f"    - `{parameter_name}` = "
                    f"{_describe_parameter(bindings[parameter_name])}"
                )
        if action.get("rules"):
            lines.append("  - 执行规则（原样保真）：")
            lines.append("")
            lines.append("  ```json")
            for line in _json_block(action["rules"]).splitlines():
                lines.append(f"  {line}")
            lines.append("  ```")
    return lines


def _render_skill_markdown(
    *,
    ontology_name: str,
    definition: dict[str, Any],
    actions: list[dict[str, Any]],
    document_md: str,
) -> str:
    display = definition.get("displayName") or definition.get("name") or "哨兵"
    origin_label = _ORIGIN_LABELS.get(
        str(definition.get("origin")), str(definition.get("origin")),
    )
    lines: list[str] = []
    append = lines.append
    append(f"# {display}（{ontology_name} · 哨兵 Skill）")
    append("")
    append("> 本 Skill 由 OpenOntology 平台按哨兵规则确定性模板生成，不使用 LLM。")
    append("")
    append("## 何时使用本技能")
    append("")
    append(
        f"当需要按照「{ontology_name}」本体的治理逻辑执行「{display}」"
        f"（{origin_label}）监测时使用：按“绑定范围 → 触发时机 → 判定条件 → "
        "处置动作”的顺序执行。"
    )
    append("")
    append("## 执行步骤")
    append("")
    append("### 1. 绑定范围（监测哪些数据）")
    append("")
    lines.extend(_render_binding_lines(definition))
    append("")
    append("### 2. 触发时机（何时检查）")
    append("")
    append(f"- {_trigger_summary(definition)}")
    trigger_mode = str(definition.get("triggerMode") or "on_enter")
    append(
        f"- 触发语义：{_TRIGGER_MODE_LABELS.get(trigger_mode, trigger_mode)}"
    )
    if definition.get("onSchedule"):
        append(
            f"- 扫描间隔：每 {definition.get('scanIntervalSeconds') or 300} 秒"
        )
    append("")
    append("### 3. 判定条件（怎样判定命中）")
    append("")
    condition = str(definition.get("condition") or "").strip()
    if condition:
        append("- 权威执行条件（condition 表达式，跨别名求值）：")
        append("")
        append("```")
        append(condition)
        append("```")
    else:
        append("- 无条件表达式（对所有绑定实例生效）。")
    rows = definition.get("conditionRows") or []
    if rows:
        append(
            f"- 结构化条件行（UI 回显形态；运行期权威是 condition 表达式，"
            f"逻辑组合 {definition.get('conditionLogic') or 'and'}）："
        )
        append("")
        append("```json")
        append(_json_block(rows))
        append("```")
    pattern = definition.get("pattern")
    if isinstance(pattern, dict):
        append("- 事件模式定义（CEP pattern，原样保真）：")
        append("")
        append("```json")
        append(_json_block(pattern))
        append("```")
    append(
        f"- 主别名（处置动作作用的目标实例）：`{definition.get('primaryAlias') or '—'}`"
    )
    append("")
    append("### 4. 处置动作（命中后做什么）")
    append("")
    lines.extend(_render_action_lines(definition, actions))
    append("")
    append("## 参考资源")
    append("")
    append(
        "- `references/sentinel-definition.json`：哨兵结构化定义"
        "（保真序列化，判定歧义时的权威依据）"
    )
    if document_md:
        append("- `references/business-doc.md`：本体业务文档（当前发布版全文）")
    else:
        append("- 本体当前发布版无业务文档，未包含 `references/business-doc.md`。")
    append("")
    append("## 能力边界")
    append("")
    append(
        "本 Skill 是确定性治理规则的指令化描述。在 OpenOntology 平台内，哨兵由"
        "平台运行时（发布快照、变更捕获、动作引擎）自动执行；脱离平台后，请由 "
        "Agent 按上述步骤对用户提供的数据模拟执行，动作中涉及的平台能力"
        "（动作引擎、审批流等）需由人工或等效系统完成。"
    )
    return "\n".join(lines)


def _build_archive(files: list[tuple[str, str]]) -> bytes:
    """固定条目顺序与时间戳，保证同输入产出同字节。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content.encode("utf-8"))
    return buffer.getvalue()


def export_sentinel_skill(
    ontology_id: str,
    sentinel_id: str,
    db: Session,
) -> Response:
    context = current_release_context(db, ontology_id)
    definition = _builtin_definition(db, ontology_id, sentinel_id, context)
    if definition is None:
        definition = _dynamic_definition(db, ontology_id, sentinel_id)
    actions = _resolve_actions(context, definition["actionIds"])
    document_md, document_title = _business_document(context)
    ontology_name = str(context.project.name or "")
    display = (
        definition.get("displayName") or definition.get("name") or "哨兵"
    )
    slug = _skill_slug(str(definition.get("name") or ""), sentinel_id)
    body = _render_skill_markdown(
        ontology_name=ontology_name,
        definition=definition,
        actions=actions,
        document_md=document_md,
    )
    frontmatter = yaml.safe_dump(
        {
            "name": slug,
            "description": _skill_description(ontology_name, definition),
        },
        allow_unicode=True,
        sort_keys=False,
        width=100000,
    ).strip()
    markdown = f"---\n{frontmatter}\n---\n\n{body}\n"
    package = {
        "schema": "openontology.sentinel-skill/v1",
        "ontology": {"id": ontology_id, "name": ontology_name},
        "sentinel": definition,
        "actions": actions,
    }
    files = [
        ("SKILL.md", markdown),
        ("references/sentinel-definition.json", _json_block(package) + "\n"),
    ]
    if document_md:
        files.append((
            "references/business-doc.md",
            f"# {document_title}\n\n{document_md}\n",
        ))
    payload = _build_archive(files)
    base_name = f"{ontology_name}-{display}"
    safe_name = (
        re.sub(r"[^A-Za-z0-9._-]+", "_", base_name).strip("._")
        or "sentinel-skill"
    )
    utf8_name = quote(f"{base_name}.zip")
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{safe_name}.zip"; '
                f"filename*=UTF-8''{utf8_name}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
