"""画布 → 本体草稿转化管线（需求→本体稳定性的核心实现）

三步管线，确定性优先：
  1. 确定性映射：对象→ObjectType、关系→LinkType、行为→ActionType、
     规则(constraint|validation)→validation 规则（默认 disabled，待人工形式化）、
     规则(approval)→requiresApproval、规则(derivation)→激活函数草稿（enabled=false）、
     规则(alert)+事件→哨兵草稿（muted 影子 + enabled=false + status=draft，
     event.source=time→定期扫描 / source=行为→变化驱动，绑定行为作用对象）。
     骨架完全由代码生成，LLM 永不生成 id —— 引用一律按名称解析。
  2. LLM 补缺（可选）：仅允许补 描述/显示名/属性类型/基数/主键 这几个白名单
     字段，pydantic 校验失败带错误回炉 ≤2 轮，仍失败则整体丢弃补丁只用第 1 步结果。
  3. lint + 自动修复：主键必在属性中、链接端点必须可解析、名称唯一化、
     枚举白名单外回退默认值；全部修复记入 report.warnings。
     叠加与目标本体的同名冲突标记（保守合并：冲突项应用时跳过）、
     流程/场景可表达性检查（引用的对象/行为是否都在草稿+目标本体中）。

草稿永不直写本体 —— apply_draft 只落用户勾选且无冲突的元素，且转出的
函数/哨兵带三重闸门（enabled=false / muted / status=draft），落地即休眠待人工形式化。
合并进已有本体走版本正门：apply_draft_to_snapshot 以同一保守合并语义改写
目标草稿版本的结构快照，不触碰 fo_* live 表（发布时才物化）；合并校验与
草稿保存同一分层口径 —— 休眠动作类缺口降级为 warnings 透出，其他校验错误
422 阻断合并。
落库元素写 source 血缘列（sessionId/documentId/draftId/draftKey/sourceRefs），
元素级可回溯到探索会话与画布卡片。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.ontologies.agent_runtime import llm_bridge
from app.ontologies.formal_modeling.models import ActionType, LinkType, ObjectType, OntologyFunction
from app.ontologies.sentinels.models import Sentinel
from app.ontologies.versions.models import OntologyVersion
from app.ontologies.versions.evolution_service import validate_snapshot
from app.ontologies.versions.snapshot_contract import (
    complete_snapshot,
    snapshot_hash,
)
from app.exploration import canvas as C
from app.exploration.canvas import norm_name

logger = logging.getLogger(__name__)

PROPERTY_TYPES = {"string", "number", "boolean", "date", "datetime", "array", "object", "reference"}
CARDINALITIES = {"one-to-one", "one-to-many", "many-to-one", "many-to-many"}

_COLORS = ["#4C6EF5", "#12B886", "#F59F00", "#FA5252", "#7950F2", "#15AABF", "#E64980", "#82C91E"]

_NUM_HINTS = ("数字", "数量", "金额", "价格", "费用", "单价", "总额", "百分比", "重量", "评分",
              "number", "int", "float", "decimal", "amount", "price", "count", "qty")
_DATETIME_HINTS = ("时间", "datetime", "timestamp", "时刻")
_DATE_HINTS = ("日期", "date", "生日", "年月日")
_BOOL_HINTS = ("是否", "布尔", "bool", "标志", "flag")
_ARRAY_HINTS = ("列表", "数组", "多个", "array", "list", "多值")


def map_type_hint(hint: Optional[str]) -> str:
    h = (hint or "").strip().lower()
    if not h:
        return "string"
    if h in PROPERTY_TYPES:
        return h
    if any(k in h for k in _BOOL_HINTS):
        return "boolean"
    if any(k in h for k in _DATETIME_HINTS):
        return "datetime"
    if any(k in h for k in _DATE_HINTS):
        return "date"
    if any(k in h for k in _ARRAY_HINTS):
        return "array"
    if any(k in h for k in _NUM_HINTS):
        return "number"
    return "string"


def _slug(name: str, fallback: str) -> str:
    """标识符化：保留字母/数字/下划线/中文；空则用 fallback。"""
    s = re.sub(r"[^\w一-鿿]+", "_", (name or "").strip()).strip("_")
    return s or fallback


def _pid() -> str:
    return f"prop-{uuid.uuid4().hex[:8]}"


def _prop_from_attr(a: dict) -> dict:
    p: dict = {
        "id": _pid(),
        "name": _slug(a.get("name", ""), "field"),
        "displayName": a.get("display_name") or a.get("name", ""),
        "type": map_type_hint(a.get("type_hint")),
        "required": bool(a.get("required")),
    }
    desc = a.get("notes") or a.get("description")
    if desc:
        p["description"] = desc
    if a.get("enum"):
        p["validation"] = {"enum": [str(v) for v in a["enum"]]}
    return p


def _mk_validation_rule(name: str, statement: str, error_message: str, order: int,
                        source_ref: Optional[str] = None) -> dict:
    """规则以 disabled 形式挂载：错误提示与表述保真，条件表达式留待人工形式化。"""
    return {"id": f"rule-{uuid.uuid4().hex[:8]}", "type": "validation",
            "name": f"待形式化: {name}", "description": statement,
            "enabled": False, "order": order,
            "config": {"type": "validation", "condition": "",
                       "errorMessage": error_message or statement or name},
            "sourceRefs": [source_ref] if source_ref else []}


# ---------------------------------------------------------------- 第 1 步：确定性映射


def _deterministic_draft(canvas: dict, warnings: list[str],
                         semantic_issues: Optional[list[dict]] = None) -> dict:
    objects: list[dict] = canvas.get("objects") or []
    actors: list[dict] = canvas.get("actors") or []
    behaviors: list[dict] = canvas.get("behaviors") or []
    events: list[dict] = canvas.get("events") or []
    rules: list[dict] = canvas.get("rules") or []
    issues = semantic_issues if semantic_issues is not None else []

    def issue(code: str, severity: str, message: str, *,
              key: Optional[str] = None, source_refs: Optional[list[str]] = None) -> None:
        value = {"code": code, "severity": severity, "message": message,
                 "sourceRefs": [x for x in (source_refs or []) if x]}
        if key:
            value["key"] = key
        marker = (code, key, tuple(value["sourceRefs"]), message)
        if not any((x.get("code"), x.get("key"), tuple(x.get("sourceRefs") or []),
                    x.get("message")) == marker for x in issues):
            issues.append(value)

    def add_alias(index: dict[str, set[str]], value: Optional[str], key: str) -> None:
        normalized = norm_name(value or "")
        if normalized:
            index.setdefault(normalized, set()).add(key)

    def resolve_alias(index: dict[str, set[str]], value: Optional[str]) -> tuple[Optional[str], str]:
        keys = index.get(norm_name(value or ""), set())
        if len(keys) == 1:
            return next(iter(keys)), "resolved"
        return None, "ambiguous" if len(keys) > 1 else "missing"

    def unique(values: list[Optional[str]]) -> list[str]:
        out: list[str] = []
        for value in values:
            if value and value not in out:
                out.append(value)
        return out

    draft_objects: list[dict] = []
    obj_by_key: dict[str, dict] = {}
    entity_aliases: dict[str, set[str]] = {}
    canonical_object_keys: dict[str, str] = {}

    def add_object_type(name: str, display_name: str, description: str,
                        props: list[dict], key_attr: Optional[str],
                        origin: str, source_ref: Optional[str],
                        actor_metadata: Optional[dict] = None) -> str:
        key = f"obj:{norm_name(name)}"
        prop_names = {norm_name(p["name"]) for p in props}
        primary = None
        if key_attr and norm_name(key_attr) in prop_names:
            # 图谱编辑器以 Property.id 作为主键引用（兼容读取 name，但新产物必须统一为 id）。
            primary = next(p["id"] for p in props if norm_name(p["name"]) == norm_name(key_attr))
        if primary is None:
            id_prop = {"id": _pid(), "name": "id", "displayName": "ID",
                       "type": "string", "required": True}
            if "id" not in prop_names:
                props = [id_prop] + props
                primary = id_prop["id"]
            else:
                primary = next(p["id"] for p in props if norm_name(p["name"]) == "id")
            if key_attr:
                warnings.append(f"对象「{name}」的主键属性「{key_attr}」不在属性列表中，已回退为 id")
            else:
                warnings.append(f"对象「{name}」未指定业务主键，已自动补 id 属性作为主键")
        item = {
            "key": key, "name": _slug(name, f"Object{len(draft_objects) + 1}"),
            "displayName": display_name or name, "description": description or "",
            "color": _COLORS[len(draft_objects) % len(_COLORS)],
            "primaryKey": primary, "properties": props,
            "origin": origin, "sourceRefs": [r for r in [source_ref] if r],
        }
        if actor_metadata:
            item["actorMetadata"] = [actor_metadata]
        draft_objects.append(item)
        obj_by_key[key] = item
        canonical_object_keys[norm_name(name)] = key
        add_alias(entity_aliases, name, key)
        add_alias(entity_aliases, display_name, key)
        return key

    for o in objects:
        add_object_type(
            o.get("name", ""), o.get("display_name") or o.get("name", ""),
            o.get("description") or "",
            [_prop_from_attr(a) for a in (o.get("attributes") or [])],
            o.get("key_attribute"), "object", o.get("id"))

    actor_aliases: dict[str, set[str]] = {}
    actor_records: dict[str, dict] = {}

    # 主体 → 参与方对象类型。与对象 canonical name 相同时做可审计合并：
    # 属性/职责/血缘全部保留；无法无损合并的主键或属性契约形成 blocking issue。
    for a in actors:
        actor_key = f"actor:{norm_name(a.get('name', ''))}"
        actor_record = {
            "id": a.get("id"), "name": a.get("name", ""),
            "displayName": a.get("display_name") or a.get("name", ""),
            "kind": a.get("kind") or "role",
            "description": a.get("description") or "",
            "responsibilities": list(a.get("responsibilities") or []),
            "attributes": list(a.get("attributes") or []),
            "keyAttribute": a.get("key_attribute"),
        }
        actor_records[actor_key] = actor_record
        add_alias(actor_aliases, a.get("name"), actor_key)
        add_alias(actor_aliases, a.get("display_name"), actor_key)

        if (a.get("kind") or "role") == "system":
            continue
        same_key = canonical_object_keys.get(norm_name(a.get("name", "")))
        if same_key:
            target = obj_by_key[same_key]
            add_alias(entity_aliases, a.get("name"), same_key)
            add_alias(entity_aliases, a.get("display_name"), same_key)
            target["origin"] = "object+actor"
            target["sourceRefs"] = unique(
                list(target.get("sourceRefs") or []) + [a.get("id")])
            target.setdefault("actorMetadata", []).append(actor_record)
            actor_record["objectTypeKey"] = same_key

            additions = [f"业务主体（{a.get('kind', 'role')}）"]
            if a.get("description"):
                additions.append(a["description"])
            if a.get("responsibilities"):
                additions.append("职责: " + "；".join(a["responsibilities"]))
            for text in additions:
                if text and text not in (target.get("description") or ""):
                    target["description"] = (
                        (target.get("description") or "").rstrip("。")
                        + ("。" if target.get("description") else "") + text)

            props_by_name = {norm_name(p.get("name", "")): p
                             for p in target.get("properties") or []}
            for raw_attr in a.get("attributes") or []:
                incoming = _prop_from_attr(raw_attr)
                normalized = norm_name(incoming["name"])
                existing_prop = props_by_name.get(normalized)
                if not existing_prop:
                    target["properties"].append(incoming)
                    props_by_name[normalized] = incoming
                    continue
                if existing_prop.get("type") != incoming.get("type"):
                    issue(
                        "actor_object_attribute_conflict", "blocking",
                        f"同名主体/对象「{a.get('name')}」的属性「{raw_attr.get('name')}」"
                        f"类型冲突（对象={existing_prop.get('type')}，主体={incoming.get('type')}）",
                        key=same_key, source_refs=[a.get("id")],
                    )
                    continue
                existing_prop["required"] = bool(
                    existing_prop.get("required") or incoming.get("required"))
                old_enum = (existing_prop.get("validation") or {}).get("enum")
                new_enum = (incoming.get("validation") or {}).get("enum")
                if old_enum and new_enum and old_enum != new_enum:
                    issue(
                        "actor_object_enum_conflict", "blocking",
                        f"同名主体/对象「{a.get('name')}」的属性「{raw_attr.get('name')}」"
                        "枚举口径不一致",
                        key=same_key, source_refs=[a.get("id")],
                    )
                elif new_enum and not old_enum:
                    existing_prop["validation"] = {"enum": new_enum}

            actor_pk = norm_name(a.get("key_attribute") or "")
            object_pk_prop = next(
                (p for p in target["properties"] if p.get("id") == target.get("primaryKey")), None)
            object_pk = norm_name((object_pk_prop or {}).get("name", ""))
            if actor_pk and object_pk and actor_pk != object_pk:
                issue(
                    "actor_object_primary_key_conflict", "blocking",
                    f"同名主体/对象「{a.get('name')}」的主键口径冲突"
                    f"（对象={object_pk_prop.get('name') if object_pk_prop else '?'}，"
                    f"主体={a.get('key_attribute')}）",
                    key=same_key, source_refs=[a.get("id")],
                )
            warnings.append(f"主体「{a.get('name')}」与同名对象合并（不重复生成对象类型）")
            continue
        desc = f"业务主体（{a.get('kind', 'role')}）。" + (a.get("description") or "")
        if a.get("responsibilities"):
            desc += " 职责: " + "；".join(a["responsibilities"])
        # 参与方也是数据实体：映射其属性（与对象一致），name 作为身份兜底
        props = [_prop_from_attr(x) for x in (a.get("attributes") or [])]
        if not any(norm_name(p["name"]) == "name" for p in props):
            props.insert(0, {"id": _pid(), "name": "name", "displayName": "名称",
                             "type": "string", "required": True})
        actor_obj_key = add_object_type(
            a.get("name", ""), a.get("display_name") or a.get("name", ""), desc,
            props, a.get("key_attribute") or "name", "actor", a.get("id"),
            actor_metadata=actor_record)
        actor_record["objectTypeKey"] = actor_obj_key

    # 对象关系 → 链接类型
    draft_links: list[dict] = []
    seen_link_names: set[str] = set()
    for o in objects:
        src_key = canonical_object_keys.get(norm_name(o.get("name", "")))
        for r in o.get("relations") or []:
            tgt_key, target_status = resolve_alias(entity_aliases, r.get("target"))
            if not src_key or not tgt_key:
                reason = "存在多个同名/同显示名候选" if target_status == "ambiguous" else "目标对象未定义"
                message = f"关系「{o.get('name')} → {r.get('target')}」{reason}，已跳过"
                warnings.append(message)
                issue("relation_target_unresolved", "blocking", message,
                      source_refs=[o.get("id")])
                continue
            cardinality = (r.get("cardinality") or "").strip().lower()
            if cardinality not in CARDINALITIES:
                if cardinality:
                    warnings.append(f"关系「{o.get('name')} → {r.get('target')}」基数「{cardinality}」"
                                    f"不合法，已回退 one-to-many")
                cardinality = "one-to-many"
            base = _slug(r.get("name") or "", "") or \
                f"{_slug(o.get('name', ''), 'src')}_{_slug(obj_by_key[tgt_key]['name'], 'tgt')}"
            name = base
            n = 2
            while norm_name(name) in seen_link_names:
                name = f"{base}_{n}"
                n += 1
            seen_link_names.add(norm_name(name))
            draft_links.append({
                "key": f"link:{norm_name(name)}", "name": name,
                "displayName": r.get("display_name") or name,
                "description": r.get("description") or "",
                "sourceKey": src_key, "targetKey": tgt_key,
                "sourceName": obj_by_key[src_key]["name"],
                "targetName": obj_by_key[tgt_key]["name"],
                "cardinality": cardinality,
                "sourceRefs": [x for x in [o.get("id")] if x],
            })

    behavior_aliases: dict[str, set[str]] = {}
    behavior_by_key: dict[str, dict] = {}
    for b in behaviors:
        key = f"act:{norm_name(b.get('name', ''))}"
        behavior_by_key[key] = b
        add_alias(behavior_aliases, b.get("name"), key)
        add_alias(behavior_aliases, b.get("display_name"), key)

    # 规则目标先统一解析成 behavior/entity，不再由各映射分支各自猜名字。
    resolved_rules: dict[str, tuple[Optional[str], Optional[str], str]] = {}
    for index, r in enumerate(rules):
        rid = str(r.get("id") or f"rule-index-{index}")
        target = r.get("applies_to") or ""
        behavior_key, behavior_status = resolve_alias(behavior_aliases, target)
        entity_key, entity_status = resolve_alias(entity_aliases, target)
        candidates = [(kind, key) for kind, key in
                      (("behavior", behavior_key), ("entity", entity_key)) if key]
        if len(candidates) == 1:
            kind, key = candidates[0]
            resolved_rules[rid] = (kind, key, "resolved")
        elif len(candidates) > 1 or behavior_status == "ambiguous" or entity_status == "ambiguous":
            resolved_rules[rid] = (None, None, "ambiguous")
            issue("rule_target_ambiguous", "blocking",
                  f"规则「{r.get('display_name') or r.get('name')}」的作用目标「{target}」"
                  "同时命中多个对象/行为，无法无歧义转换",
                  source_refs=[r.get("id")])
        else:
            resolved_rules[rid] = (None, None, "missing")
            issue("rule_target_unresolved", "blocking",
                  f"规则「{r.get('display_name') or r.get('name')}」的作用目标"
                  f"「{target or '未指定'}」未解析",
                  source_refs=[r.get("id")])

    rules_by_behavior: dict[str, list[dict]] = {}
    for index, r in enumerate(rules):
        rid = str(r.get("id") or f"rule-index-{index}")
        target_kind, target_key, _ = resolved_rules[rid]
        if target_kind == "behavior" and target_key:
            rules_by_behavior.setdefault(target_key, []).append(r)

    resolved_events: dict[str, tuple[Optional[str], str]] = {}
    events_by_behavior: dict[str, list[dict]] = {}
    for index, e in enumerate(events):
        eid = str(e.get("id") or f"event-index-{index}")
        src = (e.get("source") or "").strip()
        normalized = norm_name(src)
        if normalized in {"time", "external"}:
            resolved_events[eid] = (None, normalized)
            if normalized == "external":
                issue(
                    "external_event_binding_unsupported", "unsupported",
                    f"外部事件「{e.get('display_name') or e.get('name')}」已保留为未绑定哨兵草稿，"
                    "但当前画布没有外部连接器/监听对象契约，落地后需显式补配触发入口",
                    source_refs=[e.get("id")],
                )
            continue
        behavior_key, status = resolve_alias(behavior_aliases, src)
        resolved_events[eid] = (behavior_key, status)
        if behavior_key:
            events_by_behavior.setdefault(behavior_key, []).append(e)
        else:
            issue("event_source_unresolved", "blocking",
                  f"事件「{e.get('display_name') or e.get('name')}」的来源「{src or '未指定'}」"
                  f"{'存在歧义' if status == 'ambiguous' else '未解析到行为'}",
                  source_refs=[e.get("id")])

    # 行为 → 动作类型
    draft_actions: list[dict] = []
    for b in behaviors:
        bname = b.get("name", "")
        action_key = f"act:{norm_name(bname)}"
        desc_parts = [b.get("description") or ""]
        actor_refs: list[dict] = []
        actor_source_refs: list[str] = []
        if b.get("actor"):
            resolved_actor, actor_status = resolve_alias(actor_aliases, b.get("actor"))
            if resolved_actor:
                record = dict(actor_records[resolved_actor])
                actor_refs.append(record)
                if record.get("id"):
                    actor_source_refs.append(record["id"])
                desc_parts.append(
                    f"执行主体: {record.get('displayName') or record.get('name')}")
                issue(
                    "actor_runtime_binding_unsupported", "unsupported",
                    f"行为「{b.get('display_name') or bname}」的执行主体已保留为 actorRefs 和血缘，"
                    "但当前正式 ActionType 不支持运行时主体/授权绑定，落地后需在权限模型中补配",
                    key=action_key, source_refs=[b.get("id"), record.get("id")],
                )
            else:
                # readiness 兼容把对象本身作为执行主体；这里同样保留为 object actorRef。
                actor_obj_key, object_actor_status = resolve_alias(entity_aliases, b.get("actor"))
                if actor_obj_key:
                    actor_item = obj_by_key[actor_obj_key]
                    actor_refs.append({
                        "name": actor_item["name"], "displayName": actor_item["displayName"],
                        "kind": "object", "objectTypeKey": actor_obj_key,
                    })
                    desc_parts.append(f"执行主体: {actor_item['displayName']}")
                    issue(
                        "actor_runtime_binding_unsupported", "unsupported",
                        f"行为「{b.get('display_name') or bname}」以对象"
                        f"「{actor_item['displayName']}」作为执行主体；actorRefs 已保留，"
                        "但当前正式 ActionType 不支持运行时主体/授权绑定",
                        key=action_key, source_refs=[b.get("id")],
                    )
                else:
                    status = ("ambiguous" if "ambiguous" in
                              {actor_status, object_actor_status} else "missing")
                    issue(
                        "behavior_actor_unresolved", "blocking",
                        f"行为「{b.get('display_name') or bname}」的执行主体「{b.get('actor')}」"
                        f"{'存在歧义' if status == 'ambiguous' else '未解析'}",
                        key=action_key, source_refs=[b.get("id")],
                    )
        if b.get("trigger"):
            desc_parts.append(f"触发: {b['trigger']}")
        if b.get("outcome"):
            desc_parts.append(f"结果: {b['outcome']}")
        event_source_refs: list[str] = []
        for e in events_by_behavior.get(action_key, []):
            cons = "；".join(e.get("consequences") or [])
            desc_parts.append(f"触发事件「{e.get('display_name') or e.get('name')}」"
                              + (f"（后果: {cons}）" if cons else ""))
            if e.get("id"):
                event_source_refs.append(e["id"])

        obj_key, object_status = resolve_alias(entity_aliases, b.get("object"))
        if b.get("object") and not obj_key:
            warnings.append(f"行为「{bname}」作用的对象「{b.get('object')}」未定义，动作未绑定对象类型")
            issue(
                "behavior_object_unresolved", "blocking",
                f"行为「{b.get('display_name') or bname}」作用对象「{b.get('object')}」"
                f"{'存在歧义' if object_status == 'ambiguous' else '未解析'}",
                key=action_key, source_refs=[b.get("id")],
            )

        requires_approval = bool(b.get("needs_approval"))
        action_rules: list[dict] = []
        for i, cst in enumerate(b.get("constraints") or []):
            action_rules.append(_mk_validation_rule(f"约束{i + 1}", cst, cst, len(action_rules)))
        rule_source_refs: list[str] = []
        for r in rules_by_behavior.get(action_key, []):
            if r.get("id"):
                rule_source_refs.append(r["id"])
            if (r.get("kind") or "constraint") == "approval":
                requires_approval = True
                continue
            if (r.get("kind") or "constraint") in ("constraint", "validation"):
                action_rules.append(_mk_validation_rule(
                    r.get("display_name") or r.get("name", "规则"),
                    r.get("statement") or r.get("description") or "",
                    r.get("error_message") or "", len(action_rules), r.get("id")))
        if action_rules:
            warnings.append(f"动作「{bname}」挂载了 {len(action_rules)} 条待形式化规则"
                            f"（disabled，请在编辑器中补条件表达式后启用）")

        draft_actions.append({
            "key": action_key,
            "name": _slug(bname, f"action{len(draft_actions) + 1}"),
            "displayName": b.get("display_name") or bname,
            "description": "。".join(p for p in desc_parts if p),
            "objectTypeKey": obj_key,
            "objectTypeName": obj_by_key[obj_key]["name"] if obj_key else b.get("object"),
            "actorRefs": actor_refs,
            "parameters": [{**_prop_from_attr(i2), "id": f"param-{uuid.uuid4().hex[:8]}"}
                           for i2 in (b.get("inputs") or [])],
            "rules": action_rules,
            "requiresApproval": requires_approval,
            "sourceRefs": unique(
                [b.get("id")] + actor_source_refs + event_source_refs + rule_source_refs),
        })

    action_by_key = {a["key"]: a for a in draft_actions}

    def resolved_rule_object(index: int, rule: dict) -> Optional[str]:
        rid = str(rule.get("id") or f"rule-index-{index}")
        target_kind, target_key, _ = resolved_rules[rid]
        if target_kind == "entity":
            return target_key
        if target_kind == "behavior" and target_key:
            return action_by_key.get(target_key, {}).get("objectTypeKey")
        return None

    # 派生规则 + 对象级 constraint/validation → 停用对象函数草稿。
    # 后者不会伪装成已执行校验，而是完整保留为待形式化的 boolean 函数。
    draft_functions: list[dict] = []
    seen_fn_names: set[str] = set()
    for index, r in enumerate(rules):
        kind = r.get("kind") or "constraint"
        rid = str(r.get("id") or f"rule-index-{index}")
        target_kind, _, _ = resolved_rules[rid]
        if kind not in ("derivation", "constraint", "validation"):
            continue
        if kind in ("constraint", "validation") and target_kind != "entity":
            continue
        rname = r.get("name", "")
        base = _slug(rname, f"function{len(draft_functions) + 1}")
        fname, n = base, 2
        while norm_name(fname) in seen_fn_names:
            fname = f"{base}_{n}"
            n += 1
        seen_fn_names.add(norm_name(fname))
        obj_key = resolved_rule_object(index, r)
        desc = r.get("statement") or r.get("description") or ""
        if r.get("error_message"):
            desc += f"；违规提示: {r['error_message']}"
        if not obj_key:
            warnings.append(f"规则「{rname}」作用的「{r.get('applies_to') or '未指定'}」"
                            f"未解析到对象，函数草稿未绑定对象类型")
        draft_functions.append({
            "key": f"fn:{norm_name(fname)}", "name": fname,
            "displayName": (
                f"待形式化校验: {r.get('display_name') or rname}"
                if kind in ("constraint", "validation")
                else f"待形式化: {r.get('display_name') or rname}"),
            "description": desc,
            "functionType": "object",
            "language": "expression",
            "returnType": "boolean" if kind in ("constraint", "validation") else "string",
            "body": "",
            "enabled": False,
            "targetObjectTypeKey": obj_key,
            "targetObjectTypeName": (
                obj_by_key[obj_key]["name"] if obj_key else r.get("applies_to") or ""),
            "semanticRole": (
                "object_validation" if kind in ("constraint", "validation") else "derivation"),
            "originKind": "rule",
            "sourceRefs": [x for x in [r.get("id")] if x],
        })
    if draft_functions:
        warnings.append(f"生成 {len(draft_functions)} 个待形式化函数草稿"
                        f"（enabled=false，请在编辑器中补函数体后启用）")

    # 告警规则 + 事件 → 哨兵草稿（muted 影子 + enabled=false，条件留待人工形式化）
    draft_sentinels: list[dict] = []
    seen_sen_names: set[str] = set()

    def add_sentinel(name: str, display_name: str, description: str,
                     bind_key: Optional[str], bind_name: str,
                     on_change: bool, on_schedule: bool, interval: int,
                     origin_kind: str, source_ref: Optional[str]) -> None:
        base = _slug(name, f"sentinel{len(draft_sentinels) + 1}")
        sname, n = base, 2
        while norm_name(sname) in seen_sen_names:
            sname = f"{base}_{n}"
            n += 1
        seen_sen_names.add(norm_name(sname))
        draft_sentinels.append({
            "key": f"sen:{norm_name(sname)}", "name": sname,
            "displayName": f"待形式化: {display_name or name}",
            "description": description,
            "bindingObjectKey": bind_key, "bindingObjectName": bind_name,
            "onChange": on_change, "onSchedule": on_schedule,
            "scanIntervalSeconds": interval,
            "muted": True, "enabled": False, "status": "draft",
            "originKind": origin_kind,
            "sourceRefs": [x for x in [source_ref] if x],
        })

    for index, r in enumerate(rules):
        if (r.get("kind") or "constraint") != "alert":
            continue
        tgt = r.get("applies_to") or ""
        bind_key = resolved_rule_object(index, r)
        if not bind_key:
            warnings.append(f"告警规则「{r.get('name')}」作用的「{tgt or '未指定'}」"
                            f"未解析到对象，哨兵草稿未绑定监听对象（落地后请在编辑器中补绑定）")
        desc = r.get("statement") or r.get("description") or ""
        if r.get("error_message"):
            desc += f"；告警提示: {r['error_message']}"
        add_sentinel(r.get("name", ""), r.get("display_name") or r.get("name", ""), desc,
                     bind_key, tgt, on_change=True, on_schedule=False, interval=300,
                     origin_kind="rule", source_ref=r.get("id"))

    for event_index, e in enumerate(events):
        src = (e.get("source") or "").strip()
        is_time = norm_name(src) == "time"
        bind_key = None
        bind_name = ""
        if src and not is_time and norm_name(src) != "external":
            eid = str(e.get("id") or f"event-index-{event_index}")
            source_behavior_key, _ = resolved_events.get(eid, (None, "missing"))
            bind_key = (action_by_key.get(source_behavior_key, {}).get("objectTypeKey")
                        if source_behavior_key else None)
            bind_name = obj_by_key[bind_key]["name"] if bind_key else ""
            if not bind_key:
                warnings.append(f"事件「{e.get('name')}」的来源「{src}」未解析到行为的作用对象，"
                                f"哨兵草稿未绑定监听对象（落地后请在编辑器中补绑定）")
        parts = [e.get("description") or ""]
        if e.get("payload"):
            parts.append("载荷: " + "、".join(e["payload"]))
        if e.get("consequences"):
            parts.append("后果: " + "、".join(e["consequences"]))
        parts.append(f"来源: {src or '未指定'}")
        add_sentinel(e.get("name", ""), e.get("display_name") or e.get("name", ""),
                     "。".join(p for p in parts if p),
                     bind_key, bind_name,
                     on_change=not is_time, on_schedule=is_time,
                     interval=3600 if is_time else 300,
                     origin_kind="event", source_ref=e.get("id"))
    if draft_sentinels:
        warnings.append(f"生成 {len(draft_sentinels)} 个哨兵草稿"
                        f"（muted 影子 + 停用，请在编辑器中补条件表达式后发布）")

    # approval 只能落在 ActionType.requiresApproval。作用于对象时显式 blocking，
    # 不再以 warning 代替丢失；未解析目标已在统一解析阶段 blocking。
    for index, r in enumerate(rules):
        kind = r.get("kind") or "constraint"
        if kind != "approval":
            continue
        rid = str(r.get("id") or f"rule-index-{index}")
        target_kind, _, status = resolved_rules[rid]
        if target_kind == "behavior":
            continue
        if target_kind == "entity":
            issue(
                "object_approval_unsupported", "blocking",
                f"审批规则「{r.get('display_name') or r.get('name')}」作用于对象"
                f"「{r.get('applies_to')}」，当前正式模型只支持动作级 requiresApproval；"
                "请将规则改为作用于具体行为",
                source_refs=[r.get("id")],
            )
        elif status != "resolved":
            warnings.append(
                f"审批规则「{r.get('name')}」作用于「{r.get('applies_to') or '未指定'}」"
                "未命中任何行为，requiresApproval 未能挂载")

    return {"objectTypes": draft_objects, "linkTypes": draft_links, "actions": draft_actions,
            "functions": draft_functions, "sentinels": draft_sentinels,
            "semanticIssues": issues}


# ---------------------------------------------------------------- 第 2 步：LLM 补缺


class _PatchProp(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    name: str
    type: Optional[str] = None
    displayName: Optional[str] = None
    description: Optional[str] = None


class _PatchItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    key: str
    displayName: Optional[str] = None
    description: Optional[str] = None
    cardinality: Optional[str] = None
    primaryKey: Optional[str] = None
    properties: list[_PatchProp] = Field(default_factory=list)


class _Patch(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    objectTypes: list[_PatchItem] = Field(default_factory=list)
    linkTypes: list[_PatchItem] = Field(default_factory=list)
    actions: list[_PatchItem] = Field(default_factory=list)
    functions: list[_PatchItem] = Field(default_factory=list)
    sentinels: list[_PatchItem] = Field(default_factory=list)


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    return m.group(1).strip() if m else t


def _merge_patch(draft: dict, patch: _Patch, warnings: list[str]) -> None:
    """白名单合并：只接受 描述/显示名/属性类型/基数/主键；非法值忽略并记警告。

    函数/哨兵草稿只接受 描述/显示名 润色——functionType/绑定/触发方式等
    结构字段全部确定性生成，LLM 不可改。
    """
    by_key = {coll: {x["key"]: x for x in draft.get(coll) or []}
              for coll in ("objectTypes", "linkTypes", "actions", "functions", "sentinels")}
    for coll in ("objectTypes", "linkTypes", "actions", "functions", "sentinels"):
        for item in getattr(patch, coll):
            target = by_key[coll].get(item.key)
            if not target:
                continue
            if item.description:
                target["description"] = item.description
            if item.displayName:
                target["displayName"] = item.displayName
                # 函数/哨兵草稿的「待形式化」标记不可被润色抹掉
                if coll in ("functions", "sentinels") \
                        and not target["displayName"].startswith("待形式化"):
                    target["displayName"] = f"待形式化: {target['displayName']}"
            if coll == "linkTypes" and item.cardinality:
                if item.cardinality in CARDINALITIES:
                    target["cardinality"] = item.cardinality
                else:
                    warnings.append(f"LLM 补缺给出的基数「{item.cardinality}」不合法，已忽略")
            if coll == "objectTypes":
                props = {norm_name(p["name"]): p for p in target.get("properties", [])}
                for pp in item.properties:
                    tp = props.get(norm_name(pp.name))
                    if not tp:
                        continue  # 不允许 LLM 增删属性，只允许润色既有属性
                    if pp.type:
                        if pp.type in PROPERTY_TYPES:
                            tp["type"] = pp.type
                        else:
                            warnings.append(f"LLM 补缺给出的属性类型「{pp.type}」不合法，已忽略")
                    if pp.displayName:
                        tp["displayName"] = pp.displayName
                    if pp.description:
                        tp["description"] = pp.description
                if item.primaryKey and norm_name(item.primaryKey) in props:
                    target["primaryKey"] = props[norm_name(item.primaryKey)]["id"]


def refine_draft(draft: dict, canvas: dict, call_kwargs: Optional[dict],
                 warnings: list[str]) -> bool:
    """LLM 补缺一轮（校验失败回炉 ≤2 次）。任何失败都只丢弃补丁，不影响草稿。"""
    if not call_kwargs:
        return False
    skeleton = json.dumps(draft, ensure_ascii=False)[:12000]
    base_prompt = f"""下面是从业务画布确定性生成的本体草稿骨架。请只做「补缺」：
- 属性 type 不准确的（全是 string 的地方按语义改成 number/date/datetime/boolean）
- 链接 cardinality 按业务语义修正
- 补充更好的中文 displayName 与一句话 description
- 对象 primaryKey 若有更合适的既有属性可指定

只输出 JSON（不要解释、不要代码块），结构：
{{"objectTypes": [{{"key", "displayName"?, "description"?, "primaryKey"?, "properties": [{{"name", "type"?, "displayName"?, "description"?}}]}}],
 "linkTypes": [{{"key", "displayName"?, "description"?, "cardinality"?}}],
 "actions": [{{"key", "displayName"?, "description"?}}],
 "functions": [{{"key", "displayName"?, "description"?}}],
 "sentinels": [{{"key", "displayName"?, "description"?}}]}}
只引用骨架中已有的 key 和属性 name，不要新增/删除任何元素或属性。

# 业务画布
{C.canvas_summary(canvas)}

# 草稿骨架
{skeleton}"""
    messages = [{"role": "system", "content": "你是本体建模专家，严格按要求输出 JSON。"},
                {"role": "user", "content": base_prompt}]
    for attempt in range(3):
        try:
            resp = llm_bridge.chat(call_kwargs, messages, tools=[])
        except llm_bridge.LLMError as e:
            warnings.append(f"LLM 补缺调用失败，仅使用确定性映射结果: {e}")
            return False
        raw = _strip_fence(resp.get("content") or "")
        try:
            patch = _Patch.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            if attempt < 2:
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user",
                                 "content": f"输出不合法: {str(e)[:500]}。请重新只输出合法 JSON。"})
                continue
            warnings.append("LLM 补缺输出两次校验失败，已丢弃补丁，仅使用确定性映射结果")
            return False
        _merge_patch(draft, patch, warnings)
        return True
    return False


# ---------------------------------------------------------------- 第 3 步：lint / 冲突 / 场景覆盖


def _lint(draft: dict, warnings: list[str]) -> None:
    for coll, label in (("objectTypes", "对象类型"), ("linkTypes", "链接类型"), ("actions", "动作"),
                        ("functions", "激活函数"), ("sentinels", "哨兵")):
        seen: set[str] = set()
        for item in draft.get(coll) or []:
            base = item["name"]
            n = 2
            while norm_name(item["name"]) in seen:
                item["name"] = f"{base}_{n}"
                n += 1
            if item["name"] != base:
                warnings.append(f"{label}名称「{base}」重复，已改为「{item['name']}」")
            seen.add(norm_name(item["name"]))
    obj_keys = {o["key"] for o in draft["objectTypes"]}
    kept_links = []
    for lk in draft["linkTypes"]:
        if lk["sourceKey"] in obj_keys and lk["targetKey"] in obj_keys:
            kept_links.append(lk)
        else:
            warnings.append(f"链接「{lk['displayName']}」端点缺失，已从草稿剔除")
    draft["linkTypes"] = kept_links
    for ot in draft["objectTypes"]:
        props = ot["properties"]
        by_id = {p["id"]: p for p in props}
        by_name = {norm_name(p["name"]): p for p in props}
        primary = ot.get("primaryKey")
        if primary in by_id:
            continue
        # 兼容补丁/历史草稿传 name，生成时统一规范化成属性 id。
        named = by_name.get(norm_name(str(primary or "")))
        if named:
            ot["primaryKey"] = named["id"]
            continue
        id_prop = by_name.get("id")
        if not id_prop:
            id_prop = {"id": _pid(), "name": "id", "displayName": "ID",
                       "type": "string", "required": True}
            props.insert(0, id_prop)
        ot["primaryKey"] = id_prop["id"]
        warnings.append(f"对象「{ot['displayName']}」主键失效，已回退 id")


def _mark_conflicts(draft: dict, existing: Optional[dict[str, set[str]]],
                    conflicts: list[str]) -> None:
    ex = existing or {}
    for coll, label in (("objectTypes", "对象类型"), ("linkTypes", "链接类型"), ("actions", "动作"),
                        ("functions", "激活函数"), ("sentinels", "哨兵")):
        for item in draft.get(coll) or []:
            item["conflict"] = norm_name(item["name"]) in ex.get(coll, set())
            if item["conflict"]:
                conflicts.append(f"{label}「{item['displayName']}（{item['name']}）」"
                                 f"与目标本体同名 —— 保守合并将跳过该项")


def _scenario_coverage(canvas: dict, draft: dict,
                       existing: Optional[dict[str, set[str]]]) -> list[dict]:
    ex = existing or {}
    known_objects = {norm_name(o["name"]) for o in draft["objectTypes"]} \
        | {norm_name(o["displayName"]) for o in draft["objectTypes"]} | ex.get("objectTypes", set())
    known_actions = {norm_name(a["name"]) for a in draft["actions"]} \
        | {norm_name(a["displayName"]) for a in draft["actions"]} | ex.get("actions", set())
    out = []
    # 场景条目保持 {scenario, ...} 形状、顺序与语义不变（既有测试锁定该契约）。
    for s in canvas.get("scenarios") or []:
        missing_o = [x for x in (s.get("objects") or []) if norm_name(x) not in known_objects]
        missing_b = [x for x in (s.get("behaviors") or []) if norm_name(x) not in known_actions]
        if missing_o or missing_b:
            out.append({"scenario": s.get("display_name") or s.get("name"),
                        "missingObjects": missing_o, "missingBehaviors": missing_b})
    # 流程条目 additive 追加在尾部，判别键为 process：objects 引用 +
    # steps[].behavior 引用做同一口径校验（已知集合 = 草稿 + 目标本体存量同名）。
    for p in canvas.get("processes") or []:
        missing_o = [x for x in (p.get("objects") or []) if norm_name(x) not in known_objects]
        step_behaviors = [str(step.get("behavior") or "").strip()
                          for step in (p.get("steps") or []) if isinstance(step, dict)]
        missing_b = [x for x in step_behaviors if x and norm_name(x) not in known_actions]
        if missing_o or missing_b:
            out.append({"process": p.get("display_name") or p.get("name"),
                        "missingObjects": missing_o, "missingBehaviors": missing_b})
    return out


def _selected_draft_keys(
        draft: dict, selected_keys: Optional[list[str]] = None) -> set[str]:
    """Resolve the one canonical selection set shared by validation and apply.

    Omitting ``selected_keys`` means all non-conflicting items, never every raw
    item in the draft. A target ontology can change after draft generation, but
    that must not silently turn a previously excluded conflict into an approved
    element.
    """
    if selected_keys is not None:
        return {str(key) for key in selected_keys}
    collections = ("objectTypes", "linkTypes", "actions", "functions", "sentinels")
    return {
        str(item["key"])
        for collection in collections
        for item in (draft.get(collection) or [])
        if item.get("key") and not item.get("conflict")
    }


def validate_draft_selection(draft: dict, selected_keys: Optional[list[str]] = None,
                             existing: Optional[dict[str, set[str]]] = None,
                             snapshot: Optional[dict] = None) -> dict:
    """按图谱编辑器正式契约校验一次草稿选择集，不依赖 LLM。

    默认选择全部非冲突项；显式选择用于落地前检查依赖闭包，避免“选了关系但
    没选端点对象”这类静默跳过。返回结构可直接展示在前端质量报告中。
    ``snapshot=`` 指定目标草稿版本的结构快照作为“已有名集合”来源（合并路径
    的权威真相）；缺省时沿用调用方传入的 ``existing``（live 表口径）。
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    existing = _snapshot_name_sets(snapshot) if snapshot is not None else (existing or {})
    collections = ("objectTypes", "linkTypes", "actions", "functions", "sentinels")
    all_items = [item for coll in collections for item in (draft.get(coll) or [])]
    by_key = {str(item.get("key") or ""): item for item in all_items if item.get("key")}
    selected = _selected_draft_keys(draft, selected_keys)
    if selected_keys is not None:
        for key in sorted(selected - set(by_key)):
            errors.append({"code": "unknown_draft_key", "key": key,
                           "message": f"选择项 {key} 不存在于草稿"})

    def err(code: str, item: dict, message: str, field: str = "") -> None:
        value = {"code": code, "key": item.get("key"), "name": item.get("name"),
                 "message": message}
        if field:
            value["field"] = field
        errors.append(value)

    chosen_objects = {item["key"] for item in (draft.get("objectTypes") or [])
                      if item.get("key") in selected and not item.get("conflict")}
    chosen_names = {norm_name(item.get("name", "")) for item in (draft.get("objectTypes") or [])
                    if item.get("key") in selected and not item.get("conflict")}
    known_names = chosen_names | set(existing.get("objectTypes", set()))

    seen_by_collection: dict[str, set[str]] = {name: set() for name in collections}
    counts = {name: 0 for name in collections}
    for coll in collections:
        for item in draft.get(coll) or []:
            if item.get("key") not in selected:
                continue
            counts[coll] += 1
            if item.get("conflict"):
                err("conflict_selected", item, "该项与目标本体同名，不能作为新增项落地")
                continue
            name = norm_name(item.get("name", ""))
            if not name:
                err("name_missing", item, "元素缺少稳定英文名称", "name")
            elif name in seen_by_collection[coll]:
                err("duplicate_name", item, f"{coll} 内名称「{item.get('name')}」重复", "name")
            seen_by_collection[coll].add(name)

    for item in draft.get("objectTypes") or []:
        if item.get("key") not in selected or item.get("conflict"):
            continue
        props = item.get("properties") or []
        prop_ids: set[str] = set()
        prop_names: set[str] = set()
        for prop in props:
            pid = str(prop.get("id") or "")
            pname = norm_name(prop.get("name", ""))
            if not pid or pid in prop_ids:
                err("invalid_property_id", item, "属性 id 缺失或重复", "properties")
            if not pname or pname in prop_names:
                err("duplicate_property", item, "属性 name 缺失或重复", "properties")
            if prop.get("type") not in PROPERTY_TYPES:
                err("invalid_property_type", item,
                    f"属性「{prop.get('name')}」类型「{prop.get('type')}」不受图谱编辑器支持",
                    "properties.type")
            prop_ids.add(pid)
            prop_names.add(pname)
        if not props:
            err("properties_missing", item, "对象类型没有属性", "properties")
        if item.get("primaryKey") not in prop_ids:
            err("invalid_primary_key", item, "主键必须指向 properties 中的属性 id", "primaryKey")

    def object_dependency_ok(key: str | None, name: str | None) -> bool:
        return bool((key and key in chosen_objects) or norm_name(name or "") in known_names)

    for item in draft.get("linkTypes") or []:
        if item.get("key") not in selected or item.get("conflict"):
            continue
        if item.get("cardinality") not in CARDINALITIES:
            err("invalid_cardinality", item, "关系基数不受图谱编辑器支持", "cardinality")
        if not object_dependency_ok(item.get("sourceKey"), item.get("sourceName")):
            err("missing_source_dependency", item, "关系源对象未选择且目标本体中不存在", "sourceKey")
        if not object_dependency_ok(item.get("targetKey"), item.get("targetName")):
            err("missing_target_dependency", item, "关系目标对象未选择且目标本体中不存在", "targetKey")

    for item in draft.get("actions") or []:
        if item.get("key") not in selected or item.get("conflict"):
            continue
        if not object_dependency_ok(item.get("objectTypeKey"),
                                    item.get("objectTypeName")
                                    or str(item.get("objectTypeKey") or "").split(":", 1)[-1]):
            err("missing_action_object", item, "动作绑定对象未选择且目标本体中不存在", "objectTypeKey")
        for prop in item.get("parameters") or []:
            if prop.get("type") not in PROPERTY_TYPES:
                err("invalid_parameter_type", item,
                    f"参数「{prop.get('name')}」类型「{prop.get('type')}」不受支持", "parameters.type")

    for item in draft.get("functions") or []:
        if item.get("key") not in selected or item.get("conflict"):
            continue
        if item.get("functionType") not in {"object", "object_set", "action_validation"}:
            err("invalid_function_type", item, "函数类型与图谱编辑器契约不一致", "functionType")
        if not object_dependency_ok(item.get("targetObjectTypeKey"), item.get("targetObjectTypeName")):
            err("missing_function_object", item, "对象函数缺少可落地的绑定对象", "targetObjectTypeKey")
        if item.get("enabled"):
            err("unsafe_function_enabled", item, "探索生成的待形式化函数必须以停用状态落地", "enabled")

    for item in draft.get("sentinels") or []:
        if item.get("key") not in selected or item.get("conflict"):
            continue
        if item.get("enabled") or not item.get("muted") or item.get("status") != "draft":
            err("unsafe_sentinel_state", item, "探索生成的哨兵必须保持 draft + muted + disabled", "status")
        if item.get("bindingObjectKey") and not object_dependency_ok(
                item.get("bindingObjectKey"), item.get("bindingObjectName")):
            err("missing_sentinel_object", item, "哨兵绑定对象未选择且目标本体中不存在",
                "bindingObjectKey")
        if not item.get("bindingObjectKey"):
            warnings.append({"code": "sentinel_unbound", "key": item.get("key"),
                             "message": "哨兵尚未绑定对象，将以不可执行影子草稿落地"})

    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "selectedCount": len(selected), "counts": counts}


# ---------------------------------------------------------------- 预览投影（只读）


def _text_of(value: Any) -> str:
    return str(value or "").strip()


def _prop_signature(props: Any) -> list[tuple[str, str, bool]]:
    """属性集合的等价签名（与属性 id 无关，避免草稿/快照各自生成的随机 id 误判冲突）。"""
    return sorted(
        (norm_name(str(p.get("name") or "")), str(p.get("type") or ""), bool(p.get("required")))
        for p in (props or []) if isinstance(p, dict)
    )


def _primary_key_name(item: dict) -> str:
    """primaryKey 统一解析成属性名（草稿与快照都是属性 id，兼容历史 name 形式）。"""
    props = [p for p in (item.get("properties") or []) if isinstance(p, dict)]
    pk = item.get("primaryKey")
    by_id = {str(p.get("id")): p for p in props if p.get("id")}
    prop = by_id.get(str(pk))
    if prop is None:
        prop = next((p for p in props
                     if norm_name(str(p.get("name") or "")) == norm_name(str(pk or ""))), None)
    return norm_name(str((prop or {}).get("name") or ""))


def _same_object_type(d: dict, b: dict, _obj_names: dict[str, str]) -> bool:
    return (
        _text_of(d.get("displayName")) == _text_of(b.get("displayName"))
        and _text_of(d.get("description")) == _text_of(b.get("description"))
        and _prop_signature(d.get("properties")) == _prop_signature(b.get("properties"))
        and _primary_key_name(d) == _primary_key_name(b)
    )


def _same_link_type(d: dict, b: dict, obj_names: dict[str, str]) -> bool:
    return (
        _text_of(d.get("displayName")) == _text_of(b.get("displayName"))
        and _text_of(d.get("description")) == _text_of(b.get("description"))
        and _text_of(d.get("cardinality")) == _text_of(b.get("cardinality"))
        and norm_name(str(d.get("sourceName") or ""))
            == obj_names.get(str(b.get("sourceObjectTypeId") or ""), "")
        and norm_name(str(d.get("targetName") or ""))
            == obj_names.get(str(b.get("targetObjectTypeId") or ""), "")
    )


def _same_action(d: dict, b: dict, obj_names: dict[str, str]) -> bool:
    return (
        _text_of(d.get("displayName")) == _text_of(b.get("displayName"))
        and _text_of(d.get("description")) == _text_of(b.get("description"))
        and bool(d.get("requiresApproval")) == bool(b.get("requiresApproval"))
        and norm_name(str(d.get("objectTypeName") or ""))
            == obj_names.get(str(b.get("objectTypeId") or ""), "")
        and _prop_signature(d.get("parameters")) == _prop_signature(b.get("parameters"))
    )


def _same_function(d: dict, b: dict, obj_names: dict[str, str]) -> bool:
    return (
        _text_of(d.get("displayName")) == _text_of(b.get("displayName"))
        and _text_of(d.get("description")) == _text_of(b.get("description"))
        and _text_of(d.get("functionType")) == _text_of(b.get("functionType"))
        and _text_of(d.get("returnType")) == _text_of(b.get("returnType"))
        and norm_name(str(d.get("targetObjectTypeName") or ""))
            == obj_names.get(str(b.get("targetObjectTypeId") or ""), "")
    )


def _same_sentinel(d: dict, b: dict, obj_names: dict[str, str]) -> bool:
    bindings = [x for x in (b.get("bindings") or []) if isinstance(x, dict)]
    bound = (obj_names.get(str(bindings[0].get("objectTypeId") or ""), "")
             if bindings else "")
    return (
        _text_of(d.get("displayName")) == _text_of(b.get("displayName"))
        and _text_of(d.get("description")) == _text_of(b.get("description"))
        and bool(d.get("onChange")) == bool(b.get("onChange"))
        and bool(d.get("onSchedule")) == bool(b.get("onSchedule"))
        and int(d.get("scanIntervalSeconds") or 300) == int(b.get("scanIntervalSeconds") or 300)
        and norm_name(str(d.get("bindingObjectName") or "")) == bound
    )


_SAME_DEFINITION = {
    "objectTypes": _same_object_type,
    "linkTypes": _same_link_type,
    "actions": _same_action,
    "functions": _same_function,
    "sentinels": _same_sentinel,
}


def project_dispositions(draft: dict, baseline: Optional[dict]) -> dict[str, list[dict]]:
    """预览投影：为草稿五类集合逐项标注与基线结构的关系（纯函数，不写库）。

    disposition 与 _mark_conflicts / validate_draft_selection 同一口径：
      - add：基线无同名，应用时将新增（进入默认选择集）；
      - exists：同名且关键定义一致，保守合并将跳过（幂等无变化）；
      - conflict：同名但定义不同，不进默认选择集，需人工处理。
    依赖 build_draft 已按同一基线名集合打好 conflict 标记；基线中没有可比对
    定义的同名项（live 表兜底口径）只能确认会被跳过，按 exists 处理。
    """
    collections = tuple(_SAME_DEFINITION)
    baseline_by_name: dict[str, dict[str, dict]] = {name: {} for name in collections}
    obj_names: dict[str, str] = {}
    if baseline is not None:
        snap = complete_snapshot(baseline)
        obj_names = {
            str(item.get("id")): norm_name(str(item.get("name") or ""))
            for item in snap["objectTypes"] if isinstance(item, dict) and item.get("id")
        }
        for coll in collections:
            baseline_by_name[coll] = {
                norm_name(str(item.get("name") or "")): item
                for item in snap[coll] if isinstance(item, dict) and item.get("name")
            }

    def disposition(coll: str, item: dict) -> str:
        if not item.get("conflict"):
            return "add"
        base_item = baseline_by_name[coll].get(norm_name(str(item.get("name") or "")))
        if base_item is None:
            return "exists"
        return "exists" if _SAME_DEFINITION[coll](item, base_item, obj_names) else "conflict"

    return {
        coll: [
            {"key": item.get("key"), "name": item.get("name"),
             "displayName": item.get("displayName") or item.get("name"),
             "disposition": disposition(coll, item)}
            for item in draft.get(coll) or []
        ]
        for coll in collections
    }


# ---------------------------------------------------------------- 对外入口


def _snapshot_name_sets(snapshot: dict) -> dict[str, set[str]]:
    """从版本结构快照读取五类集合的归一化名集合（与 live 表口径同构）。"""
    snap = complete_snapshot(snapshot)
    return {
        collection: {
            normalized
            for item in snap[collection]
            if isinstance(item, dict)
            for normalized in [norm_name(str(item.get("name") or ""))]
            if normalized
        }
        for collection in (
            "objectTypes", "linkTypes", "actions", "functions", "sentinels",
        )
    }


def existing_name_sets(db: Session, ontology_id: str,
                       *, snapshot: Optional[dict] = None) -> dict[str, set[str]]:
    """目标本体的五类同名集合。

    默认读 fo_* live 表；传入 ``snapshot=``（目标草稿版本的结构快照）时改从
    快照读取 —— 合并路径的权威真相是版本快照，live 表会漏掉草稿内未发布元素。
    """
    if snapshot is not None:
        return _snapshot_name_sets(snapshot)
    return {
        "objectTypes": {norm_name(x.name) for x in db.query(ObjectType.name)
                        .filter(ObjectType.ontology_id == ontology_id)},
        "linkTypes": {norm_name(x.name) for x in db.query(LinkType.name)
                      .filter(LinkType.ontology_id == ontology_id)},
        "actions": {norm_name(x.name) for x in db.query(ActionType.name)
                    .filter(ActionType.ontology_id == ontology_id)},
        "functions": {norm_name(x.name) for x in db.query(OntologyFunction.name)
                      .filter(OntologyFunction.ontology_id == ontology_id)},
        "sentinels": {norm_name(x.name) for x in db.query(Sentinel.name)
                      .filter(Sentinel.ontology_id == ontology_id)},
    }


def resolve_merge_baseline_snapshot(
    db: Session,
    ontology_id: str,
    *,
    bound_version_id: Optional[str] = None,
    current_release_id: Optional[str] = None,
    version_model=OntologyVersion,
) -> Optional[dict]:
    """只读解析“合并进已有本体”的校验基线快照（不修复、不分叉、不写库）。

    优先会话绑定的有效草稿版本（同本体 + draft + editing），其次当前发布
    指针指向的发布快照；两者都不可用时返回 None（调用方回退 live 表口径，
    与 legacy 基线修复将从 live 表冻结的内容同构）。
    """
    if bound_version_id:
        bound = db.query(version_model).filter(
            version_model.id == str(bound_version_id),
            version_model.ontology_id == ontology_id,
        ).first()
        if (bound is not None and bound.node_kind == "draft"
                and bound.lifecycle_status == "editing"):
            return complete_snapshot(bound.snapshot_formal)
    if current_release_id:
        release = db.query(version_model).filter(
            version_model.id == str(current_release_id),
            version_model.ontology_id == ontology_id,
            version_model.node_kind == "release",
            version_model.lifecycle_status == "released",
        ).first()
        if release is not None and release.snapshot_formal is not None:
            return complete_snapshot(release.snapshot_formal)
    return None


def build_draft(canvas: dict, existing: Optional[dict[str, set[str]]] = None,
                call_kwargs: Optional[dict] = None) -> tuple[dict, dict]:
    """完整管线：确定性映射 → lint → 冲突标记 → 场景覆盖。
    LLM 补缺已移除 —— 确定性映射依靠 map_type_hint() 推断属性类型，已足够。"""
    warnings: list[str] = []
    conflicts: list[str] = []
    semantic_issues: list[dict] = []
    draft = _deterministic_draft(canvas, warnings, semantic_issues)
    refined = False  # LLM 补缺已跳过，确定性映射已满足需求
    _lint(draft, warnings)
    _mark_conflicts(draft, existing, conflicts)
    coverage = _scenario_coverage(canvas, draft, existing)
    validation = validate_draft_selection(draft, existing=existing)
    blocking_count = sum(1 for item in semantic_issues if item.get("severity") == "blocking")
    unsupported_count = sum(1 for item in semantic_issues
                            if item.get("severity") == "unsupported")
    report = {"warnings": warnings, "conflicts": conflicts,
              "scenarioCoverage": coverage, "llmRefined": refined,
              "validation": validation,
              "semanticIssues": semantic_issues,
              "semanticFidelity": {
                  "blockingCount": blocking_count,
                  "unsupportedCount": unsupported_count,
                  "readyToApply": blocking_count == 0,
              }}
    return draft, report


def _source_of(item: dict, lineage: Optional[dict]) -> Optional[dict]:
    """元素级血缘：sessionId/documentId/draftId + draftKey/sourceRefs。

    正式模型暂时没有主体运行时绑定列；把结构化 actorRefs/主体元数据留在
    血缘中，既不伪装成授权能力，也不让需求语义在落库后消失。
    """
    if not lineage:
        return None
    source = {"kind": "business_exploration", **lineage,
              "draftKey": item.get("key"), "sourceRefs": item.get("sourceRefs") or []}
    for field in ("actorRefs", "actorMetadata", "semanticRole", "originKind"):
        if item.get(field):
            source[field] = item[field]
    return source


def apply_draft(db: Session, draft_data: dict, selected_keys: Optional[list[str]],
                ontology_id: str, lineage: Optional[dict] = None) -> dict:
    """把勾选且无冲突的草稿元素落入本体（保守合并：同名一律跳过）。

    链接端点/动作绑定解析顺序：本次落地的新对象 → 目标本体已有同名对象 → 跳过。
    同名跳过使重复 apply 天然幂等——已落地元素再次应用只会进 skipped。
    lineage（sessionId/documentId/draftId）与元素的 draftKey/sourceRefs 一起
    写入 source 血缘列，落地后可回溯到探索会话与画布卡片。
    """
    selected = _selected_draft_keys(draft_data, selected_keys)

    def picked(item: dict) -> bool:
        return str(item.get("key") or "") in selected

    def src_of(item: dict) -> Optional[dict]:
        return _source_of(item, lineage)

    existing = existing_name_sets(db, ontology_id)
    existing_obj_ids = {norm_name(x.name): x.id for x in
                        db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id)}
    created = {"objectTypes": 0, "linkTypes": 0, "actions": 0, "functions": 0, "sentinels": 0}
    skipped: list[dict] = []
    if selected_keys is None:
        # Preserve the historical/audit-visible skipped report while ensuring
        # these conflicts never enter the executable selection set.
        for collection in ("objectTypes", "linkTypes", "actions", "functions", "sentinels"):
            skipped.extend({
                "key": item.get("key"),
                "reason": (
                    f"草稿元素「{item.get('name') or item.get('displayName') or item.get('key')}」"
                    "在生成时已标记为目标本体冲突，未纳入默认应用选择"
                ),
            } for item in (draft_data.get(collection) or []) if item.get("conflict"))
    key2id: dict[str, str] = {}

    base_count = len(existing_obj_ids)
    for item in draft_data.get("objectTypes", []):
        if not picked(item):
            continue
        if norm_name(item["name"]) in existing["objectTypes"]:
            skipped.append({"key": item["key"], "reason": f"目标本体已存在同名对象类型「{item['name']}」"})
            continue
        oid = str(uuid.uuid4())
        idx = base_count + created["objectTypes"]
        db.add(ObjectType(
            id=oid, ontology_id=ontology_id, name=item["name"],
            display_name=item["displayName"], description=item.get("description"),
            color=item.get("color"), primary_key=item.get("primaryKey"),
            properties=item.get("properties") or [], interfaces=[],
            position_x=120 + (idx % 4) * 340, position_y=120 + (idx // 4) * 260,
            source=src_of(item),
        ))
        key2id[item["key"]] = oid
        existing["objectTypes"].add(norm_name(item["name"]))
        created["objectTypes"] += 1

    def resolve_obj(key: Optional[str], name_hint: str) -> Optional[str]:
        if key and key in key2id:
            return key2id[key]
        return existing_obj_ids.get(norm_name(name_hint))

    for item in draft_data.get("linkTypes", []):
        if not picked(item):
            continue
        if norm_name(item["name"]) in existing["linkTypes"]:
            skipped.append({"key": item["key"], "reason": f"目标本体已存在同名链接类型「{item['name']}」"})
            continue
        src = resolve_obj(item.get("sourceKey"), item.get("sourceName", ""))
        tgt = resolve_obj(item.get("targetKey"), item.get("targetName", ""))
        if not src or not tgt:
            skipped.append({"key": item["key"],
                            "reason": f"链接「{item['displayName']}」的端点对象未落地也不在目标本体中"})
            continue
        db.add(LinkType(
            id=str(uuid.uuid4()), ontology_id=ontology_id, name=item["name"],
            display_name=item["displayName"], description=item.get("description"),
            source_object_type_id=src, target_object_type_id=tgt,
            cardinality=item.get("cardinality") or "one-to-many",
            properties=[],
            source=src_of(item),
        ))
        existing["linkTypes"].add(norm_name(item["name"]))
        created["linkTypes"] += 1

    for item in draft_data.get("actions", []):
        if not picked(item):
            continue
        if norm_name(item["name"]) in existing["actions"]:
            skipped.append({"key": item["key"], "reason": f"目标本体已存在同名动作「{item['name']}」"})
            continue
        obj_key = item.get("objectTypeKey")
        obj_id = key2id.get(obj_key) if obj_key else None
        if obj_key and not obj_id:
            # 端点对象是冲突/未勾选项 → 尝试按名字绑到已有对象
            src_name = item.get("objectTypeName") or obj_key.split(":", 1)[-1]
            obj_id = existing_obj_ids.get(norm_name(src_name))
        db.add(ActionType(
            id=str(uuid.uuid4()), ontology_id=ontology_id, name=item["name"],
            display_name=item["displayName"], description=item.get("description"),
            object_type_id=obj_id, parameters=item.get("parameters") or [],
            rules=item.get("rules") or [],
            requires_approval=bool(item.get("requiresApproval")),
            source=src_of(item),
        ))
        existing["actions"].add(norm_name(item["name"]))
        created["actions"] += 1

    # 激活函数草稿 → OntologyFunction（enabled=false，函数体待人工形式化后启用）
    for item in draft_data.get("functions", []):
        if not picked(item):
            continue
        if norm_name(item["name"]) in existing.get("functions", set()):
            skipped.append({"key": item["key"], "reason": f"目标本体已存在同名函数「{item['name']}」"})
            continue
        target_id = resolve_obj(item.get("targetObjectTypeKey"),
                                item.get("targetObjectTypeName", ""))
        db.add(OntologyFunction(
            id=str(uuid.uuid4()), ontology_id=ontology_id, name=item["name"],
            display_name=item["displayName"], description=item.get("description"),
            function_type=(item.get("functionType") or "query") if target_id else "query",
            language="expression", target_object_type_id=target_id,
            parameters=[], return_type=item.get("returnType") or "string",
            body=item.get("body") or "", enabled=False,
            source=src_of(item),
        ))
        existing.setdefault("functions", set()).add(norm_name(item["name"]))
        created["functions"] += 1

    # 哨兵草稿 → Sentinel（muted 影子 + enabled=false + status=draft，三重闸门确保不进执行链路）
    for item in draft_data.get("sentinels", []):
        if not picked(item):
            continue
        if norm_name(item["name"]) in existing.get("sentinels", set()):
            skipped.append({"key": item["key"], "reason": f"目标本体已存在同名哨兵「{item['name']}」"})
            continue
        bind_id = resolve_obj(item.get("bindingObjectKey"), item.get("bindingObjectName", ""))
        bindings = [{"alias": "a", "objectTypeId": bind_id, "filter": None}] if bind_id else []
        db.add(Sentinel(
            id=str(uuid.uuid4()), ontology_id=ontology_id, name=item["name"],
            display_name=item["displayName"], description=item.get("description"),
            bindings=bindings, links=[], condition=None,
            condition_rows=[], condition_logic="and",
            primary_alias="a" if bindings else None, action_ids=[],
            on_change=bool(item.get("onChange", True)),
            on_schedule=bool(item.get("onSchedule")),
            scan_interval_seconds=int(item.get("scanIntervalSeconds") or 300),
            trigger_mode="on_enter", muted=True, enabled=False, status="draft",
            source=src_of(item),
        ))
        existing.setdefault("sentinels", set()).add(norm_name(item["name"]))
        created["sentinels"] += 1

    db.flush()
    return {"created": created, "skipped": skipped}


def apply_draft_to_snapshot(
    db: Session,
    draft_data: dict,
    selected_keys: Optional[list[str]],
    draft_version,
    *,
    lineage: Optional[dict] = None,
    stale_trials_fn=None,
    diff_formal_fn=None,
) -> dict:
    """把勾选元素合并进目标草稿版本的结构快照（走版本正门，不触碰 fo_* live 表）。

    与 apply_draft 同一保守合并语义：同名 norm_name 一律跳过（重复应用天然幂等）；
    端点/绑定解析顺序：本批新落元素 → 既有快照同名元素 → 两端不可解析则跳过。
    新元素补 createdAt/updatedAt（ISO）与 source 血缘；对象补 interfaces/icon 缺省；
    函数 enabled=False；哨兵带三重闸门（muted + enabled=false + status=draft）。
    写回草稿版本：snapshot_formal 整体替换、revision + 1、snapshot_hash 重算、
    change_summary 用既有 _diff_formal 口径、既有非 running 试跑置 stale；
    mappings/linkMappings 原样保留。合并校验与草稿保存同一分层口径：
    休眠动作类缺口进返回的 warnings，其他校验错误抛 422 阻断合并
    （候选快照不落版本）。本函数不 commit，事务由调用方持有。
    """
    selected = _selected_draft_keys(draft_data, selected_keys)

    def picked(item: dict) -> bool:
        return str(item.get("key") or "") in selected

    before = complete_snapshot(draft_version.snapshot_formal)
    candidate = complete_snapshot(draft_version.snapshot_formal)
    existing = _snapshot_name_sets(candidate)
    existing_obj_ids = {
        norm_name(str(ot.get("name") or "")): str(ot.get("id"))
        for ot in candidate["objectTypes"]
        if isinstance(ot, dict) and ot.get("id")
    }
    created = {"objectTypes": 0, "linkTypes": 0, "actions": 0,
               "functions": 0, "sentinels": 0}
    skipped: list[dict] = []
    if selected_keys is None:
        # 与 live 落地同一口径：生成期冲突项留在审计可见的 skipped 报告中，
        # 但永不进入默认选择集。
        for collection in ("objectTypes", "linkTypes", "actions", "functions", "sentinels"):
            skipped.extend({
                "key": item.get("key"),
                "reason": (
                    f"草稿元素「{item.get('name') or item.get('displayName') or item.get('key')}」"
                    "在生成时已标记为目标本体冲突，未纳入默认应用选择"
                ),
            } for item in (draft_data.get(collection) or []) if item.get("conflict"))
    key2id: dict[str, str] = {}
    now_iso = datetime.now(timezone.utc).isoformat()

    base_count = len(existing_obj_ids)
    for item in draft_data.get("objectTypes", []):
        if not picked(item):
            continue
        if norm_name(item["name"]) in existing["objectTypes"]:
            skipped.append({"key": item["key"], "reason": f"目标本体已存在同名对象类型「{item['name']}」"})
            continue
        oid = str(uuid.uuid4())
        idx = base_count + created["objectTypes"]
        candidate["objectTypes"].append({
            "id": oid, "name": item["name"], "displayName": item["displayName"],
            "description": item.get("description"), "icon": item.get("icon"),
            "color": item.get("color"), "primaryKey": item.get("primaryKey"),
            "properties": item.get("properties") or [], "interfaces": [],
            "positionX": 120 + (idx % 4) * 340, "positionY": 120 + (idx // 4) * 260,
            "source": _source_of(item, lineage),
            "createdAt": now_iso, "updatedAt": now_iso,
        })
        key2id[item["key"]] = oid
        existing["objectTypes"].add(norm_name(item["name"]))
        created["objectTypes"] += 1

    def resolve_obj(key: Optional[str], name_hint: str) -> Optional[str]:
        if key and key in key2id:
            return key2id[key]
        return existing_obj_ids.get(norm_name(name_hint))

    for item in draft_data.get("linkTypes", []):
        if not picked(item):
            continue
        if norm_name(item["name"]) in existing["linkTypes"]:
            skipped.append({"key": item["key"], "reason": f"目标本体已存在同名链接类型「{item['name']}」"})
            continue
        src = resolve_obj(item.get("sourceKey"), item.get("sourceName", ""))
        tgt = resolve_obj(item.get("targetKey"), item.get("targetName", ""))
        if not src or not tgt:
            skipped.append({"key": item["key"],
                            "reason": f"链接「{item['displayName']}」的端点对象未落地也不在目标本体中"})
            continue
        candidate["linkTypes"].append({
            "id": str(uuid.uuid4()), "name": item["name"],
            "displayName": item["displayName"],
            "description": item.get("description"),
            "sourceObjectTypeId": src, "targetObjectTypeId": tgt,
            "cardinality": item.get("cardinality") or "one-to-many",
            "sourceRole": None, "targetRole": None, "properties": [],
            "source": _source_of(item, lineage),
            "createdAt": now_iso, "updatedAt": now_iso,
        })
        existing["linkTypes"].add(norm_name(item["name"]))
        created["linkTypes"] += 1

    for item in draft_data.get("actions", []):
        if not picked(item):
            continue
        if norm_name(item["name"]) in existing["actions"]:
            skipped.append({"key": item["key"], "reason": f"目标本体已存在同名动作「{item['name']}」"})
            continue
        obj_key = item.get("objectTypeKey")
        obj_id = key2id.get(obj_key) if obj_key else None
        if obj_key and not obj_id:
            # 端点对象是冲突/未勾选项 → 尝试按名字绑到快照既有对象
            src_name = item.get("objectTypeName") or obj_key.split(":", 1)[-1]
            obj_id = existing_obj_ids.get(norm_name(src_name))
        candidate["actions"].append({
            "id": str(uuid.uuid4()), "name": item["name"],
            "displayName": item["displayName"],
            "description": item.get("description"),
            "objectTypeId": obj_id,
            "parameters": item.get("parameters") or [],
            "rules": item.get("rules") or [],
            "validationFunctionId": None,
            "requiresApproval": bool(item.get("requiresApproval")),
            "source": _source_of(item, lineage),
            "createdAt": now_iso, "updatedAt": now_iso,
        })
        existing["actions"].add(norm_name(item["name"]))
        created["actions"] += 1

    # 激活函数草稿（enabled=false，函数体待人工形式化后启用）
    for item in draft_data.get("functions", []):
        if not picked(item):
            continue
        if norm_name(item["name"]) in existing["functions"]:
            skipped.append({"key": item["key"], "reason": f"目标本体已存在同名函数「{item['name']}」"})
            continue
        target_id = resolve_obj(item.get("targetObjectTypeKey"),
                                item.get("targetObjectTypeName", ""))
        candidate["functions"].append({
            "id": str(uuid.uuid4()), "name": item["name"],
            "displayName": item["displayName"],
            "description": item.get("description"),
            "functionType": (item.get("functionType") or "query") if target_id else "query",
            "language": "expression", "targetObjectTypeId": target_id,
            "targetActionId": None, "parameters": [],
            "returnType": item.get("returnType") or "string",
            "body": item.get("body") or "",
            "cacheStrategy": None, "cacheTtl": None, "enabled": False,
            "source": _source_of(item, lineage),
            "createdAt": now_iso, "updatedAt": now_iso,
        })
        existing["functions"].add(norm_name(item["name"]))
        created["functions"] += 1

    # 哨兵草稿（muted 影子 + enabled=false + status=draft，三重闸门确保不进执行链路）
    for item in draft_data.get("sentinels", []):
        if not picked(item):
            continue
        if norm_name(item["name"]) in existing["sentinels"]:
            skipped.append({"key": item["key"], "reason": f"目标本体已存在同名哨兵「{item['name']}」"})
            continue
        bind_id = resolve_obj(item.get("bindingObjectKey"), item.get("bindingObjectName", ""))
        bindings = [{"alias": "a", "objectTypeId": bind_id, "filter": None}] if bind_id else []
        candidate["sentinels"].append({
            "id": str(uuid.uuid4()), "name": item["name"],
            "displayName": item["displayName"],
            "description": item.get("description"),
            "bindings": bindings, "links": [], "condition": None,
            "conditionRows": [], "conditionLogic": "and",
            "primaryAlias": "a" if bindings else None,
            "actionIds": [], "actionParameters": {},
            "onChange": bool(item.get("onChange", True)),
            "onSchedule": bool(item.get("onSchedule")),
            "scanIntervalSeconds": int(item.get("scanIntervalSeconds") or 300),
            "triggerMode": "on_enter",
            "muted": True, "enabled": False, "status": "draft",
            "source": _source_of(item, lineage),
            "createdAt": now_iso, "updatedAt": now_iso,
        })
        existing["sentinels"].add(norm_name(item["name"]))
        created["sentinels"] += 1

    # 合并校验与草稿保存（save_draft_workspace）同一分层口径：
    # 「动作无启用的可执行副作用规则」（落地即休眠、待人工形式化）降级为
    # warnings 透出，不阻断合并；其他校验错误 422 阻断 —— 候选快照不落版本，
    # 事务由调用方持有，抛错后整体回滚。
    merge_warnings: list[dict] = []
    errors = validate_snapshot(
        candidate,
        require_object_type=False,
        require_executable_action_rules=False,
        warnings_out=merge_warnings,
    )
    if errors:
        raise HTTPException(422, detail={
            "code": "invalid_merged_snapshot",
            "message": "合并后的草稿快照结构校验未通过，未执行合并",
            "errors": errors[:20],
        })
    draft_version.snapshot_formal = candidate
    draft_version.revision = (draft_version.revision or 0) + 1
    draft_version.snapshot_hash = snapshot_hash(candidate)
    if diff_formal_fn is not None:
        draft_version.change_summary = diff_formal_fn(before, candidate)
    draft_version.lifecycle_status = "editing"
    if stale_trials_fn is not None:
        stale_trials_fn(db, draft_version)
    return {"created": created, "skipped": skipped,
            "versionId": str(draft_version.id),
            "warnings": merge_warnings}
