"""
正规本体模型的服务端强制校验

前端 schemaLint 是"提示"，本模块是"闸门"：不满足硬性约束的模型拒绝入库（422），
防止绕过前端直接调 API 写入损坏模型。

只拦"会损坏数据完整性或让模型不可加载"的硬错误：
  - 同类集合内名称重复（对象/关系/动作/函数）
  - 同一类型内属性名重复（否则保存时静默去重会丢数据）
  - 关系端点 / 动作绑定 / 函数绑定 / computed 属性函数 悬挂
  - 非法基数
  - 实例的类型引用、存储/派生属性类型、必填/未知字段、主键存在与唯一性
  - 链接实例的关系类型、端点类型、重复边与基数
草稿态对象类型可不设主键；属性列表为空时按开放 schema 处理，允许保存半成品。
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any, Iterable, Optional

from .function_engine import derived_function_contract_issues

VALID_CARDINALITIES = {"one-to-one", "one-to-many", "many-to-one", "many-to-many"}

# Formal PropertyType 的兼容词表。integer/float/timestamp 是历史数据和资产湖
# 里出现过的别名；运行时统一按 number/datetime 语义校验，避免旧模型突然失效。
PROPERTY_TYPE_ALIASES = {
    "integer": "number",
    "float": "number",
    "timestamp": "datetime",
}
VALID_PROPERTY_TYPES = {
    "string", "number", "boolean", "date", "datetime", "array", "object", "reference",
}

# 早期 pipeline projection 会把这些展示/溯源字段写进实例 properties，却不会把
# 它们声明成 ObjectType.properties。已声明业务属性的类型采用 closed-schema，
# 但保留这组平台字段，兼容存量数据和现有前端卡片渲染。
LEGACY_SYSTEM_PROPERTIES = {
    "id", "ontology_id", "name", "name_cn", "name_en", "display_name",
    "name_abbr", "source_id", "source_row_count", "object_type",
}


def _err(code: str, kind: str, message: str, name: str = "", item_id: str = "",
         field: str = "") -> dict:
    out = {"code": code, "kind": kind, "name": name, "id": item_id, "message": message}
    if field:
        out["field"] = field
    return out


def _id(item: Any) -> str:
    return str(getattr(item, "id", None) or "")


def _label(item: Any) -> str:
    return str(getattr(item, "display_name", None) or getattr(item, "name", None) or _id(item))


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _canonical(value: Any) -> str:
    """稳定比较 JSON 值；类型名防止 True 与 1 被视作同一主键。"""
    return f"{type(value).__name__}:{json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)}"


def _normal_property_type(raw: Any) -> str:
    value = str(raw or "string").lower()
    return PROPERTY_TYPE_ALIASES.get(value, value)


def _is_value_of_type(value: Any, property_type: str) -> bool:
    if property_type == "string":
        return isinstance(value, str)
    if property_type == "number":
        if not (isinstance(value, (int, float)) and not isinstance(value, bool)):
            return False
        try:
            # 超过 float64 量级的巨型 int 会 OverflowError——按"数值超域"拒绝，
            # 不能让单个畸形值以未分类异常击穿整批投影校验
            return math.isfinite(float(value))
        except OverflowError:
            return False
    if property_type == "boolean":
        return isinstance(value, bool)
    if property_type == "array":
        return isinstance(value, list)
    if property_type == "object":
        return isinstance(value, dict)
    if property_type == "reference":
        # 业务系统常用字符串 ID；兼容历史数值型主键。
        return isinstance(value, (str, int)) and not isinstance(value, bool)
    if property_type in ("date", "datetime"):
        if isinstance(value, datetime):
            return True
        if property_type == "date" and isinstance(value, date):
            return True
        if not isinstance(value, str):
            return False
        try:
            if property_type == "date":
                date.fromisoformat(value)
            else:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    return False


def property_value_type_issue(
    definition: dict,
    value: Any,
) -> Optional[dict[str, str]]:
    """Return one canonical type-contract issue for a property value.

    Runtime projection, derived recomputation and isolated trials all consume
    this helper.  Keeping the normalization and value predicate here prevents
    the trial path from accepting a value that the production projection later
    rejects (or vice versa).
    """
    raw_type = definition.get("type")
    expected = _normal_property_type(raw_type)
    if expected not in VALID_PROPERTY_TYPES:
        return {
            "code": "invalid_property_type_definition",
            "rawType": str(raw_type),
            "expected": expected,
            "actual": type(value).__name__,
        }
    if value is not None and not _is_value_of_type(value, expected):
        return {
            "code": "property_type_mismatch",
            "rawType": str(raw_type),
            "expected": expected,
            "actual": type(value).__name__,
        }
    return None


def _property_defs(owner: Any) -> dict[str, dict]:
    return {
        str(p.get("name")): p
        for p in (getattr(owner, "properties", None) or [])
        if isinstance(p, dict) and p.get("name")
    }


def _validate_property_values(owner: Any, values: Any, *, kind: str, item_id: str,
                              errors: list[dict], allowed_unknown: set[str] | None = None) -> None:
    label = _label(owner)
    if not isinstance(values, dict):
        errors.append(_err("invalid_properties", kind,
                           f"「{label}」实例的 properties 必须是对象", label, item_id))
        return

    definitions = _property_defs(owner)
    # 无属性定义 = 草稿/开放类型；允许先保存实例，保持既有草稿工作流兼容。
    if not definitions:
        return

    allowed = set(allowed_unknown or ())
    for key in values:
        if key not in definitions and key not in allowed:
            errors.append(_err(
                "unknown_property", kind,
                f"「{label}」未声明属性 \"{key}\"", label, item_id, field=str(key)))
        elif key in definitions and (
            definitions[key].get("source") == "computed"
            or bool(definitions[key].get("computed"))
        ):
            errors.append(_err(
                "computed_property_in_stored_projection",
                kind,
                f"「{label}」派生属性 \"{key}\" 不能写入 properties；"
                "必须由服务端函数写入 computed 投影",
                label,
                item_id,
                field=str(key),
            ))

    for name, definition in definitions.items():
        computed = definition.get("source") == "computed" or bool(definition.get("computed"))
        if computed:
            # Computed definitions are validated exclusively against the
            # separate materialized projection below.  Never interpret a
            # shadow value in ``properties`` as a valid stored field.
            continue
        value = values.get(name)
        if definition.get("required") and (name not in values or _is_empty(value)):
            errors.append(_err(
                "required_property_missing", kind,
                f"「{label}」必填属性 \"{name}\" 缺失", label, item_id, field=name))
            continue
        if name not in values or value is None:
            continue
        expected = _normal_property_type(definition.get("type"))
        if expected not in VALID_PROPERTY_TYPES:
            errors.append(_err(
                "invalid_property_type_definition", kind,
                f"「{label}」属性 \"{name}\" 的类型定义 \"{definition.get('type')}\" 非法",
                label, item_id, field=name))
        elif not _is_value_of_type(value, expected):
            errors.append(_err(
                "property_type_mismatch", kind,
                f"「{label}」属性 \"{name}\" 应为 {expected}，实际为 {type(value).__name__}",
                label, item_id, field=name))


def _validate_computed_values(owner: Any, values: Any, *, kind: str, item_id: str,
                              errors: list[dict]) -> None:
    """Validate the materialized derived projection independently of properties.

    ``computed`` is consumed by Sentinel as an authoritative overlay on top of
    stored properties.  It therefore cannot inherit the draft/open-schema
    exception used by ``properties``: every key must name an explicitly
    declared computed property, and every non-null value must satisfy that
    property's declared type.

    Requiredness deliberately does not apply here.  A missing computed value
    means the projection is unavailable and downstream evaluation must fail
    closed; it must not make an otherwise valid stored instance impossible to
    persist.
    """
    label = _label(owner)
    if not isinstance(values, dict):
        errors.append(_err(
            "invalid_computed", kind,
            f"「{label}」实例的 computed 必须是对象",
            label, item_id, field="computed"))
        return

    definitions = _property_defs(owner)
    computed_definitions = {
        name: definition
        for name, definition in definitions.items()
        if (definition.get("source") == "computed"
            or bool(definition.get("computed")))
    }

    for key in values:
        if key not in computed_definitions:
            errors.append(_err(
                "unknown_computed_property", kind,
                f"「{label}」未声明派生属性 \"{key}\"",
                label, item_id, field=str(key)))

    for name, definition in computed_definitions.items():
        if name not in values or values[name] is None:
            continue
        value = values[name]
        issue = property_value_type_issue(definition, value)
        if issue is None:
            continue
        if issue["code"] == "invalid_property_type_definition":
            errors.append(_err(
                "invalid_property_type_definition", kind,
                f"「{label}」派生属性 \"{name}\" 的类型定义 "
                f"\"{definition.get('type')}\" 非法",
                label, item_id, field=name))
        else:
            errors.append(_err(
                "property_type_mismatch", kind,
                f"「{label}」派生属性 \"{name}\" 应为 {issue['expected']}，"
                f"实际为 {issue['actual']}",
                label, item_id, field=name))


def validate_instance_contract(object_types: list, instances: list,
                               validate_ids: Optional[set[str]] = None) -> list[dict]:
    """校验 ObjectInstance；validate_ids 用于 CRUD 只拦本次候选，PK 比较仍看全体。"""
    errors: list[dict] = []
    ot_by_id = {_id(o): o for o in object_types if _id(o)}
    selected = lambda iid: validate_ids is None or iid in validate_ids

    seen_instance_ids: dict[str, int] = {}
    pk_groups: dict[tuple[str, str], list[Any]] = {}
    for inst in instances:
        iid = _id(inst)
        if iid:
            seen_instance_ids[iid] = seen_instance_ids.get(iid, 0) + 1
        otid = str(getattr(inst, "object_type_id", None) or "")
        ot = ot_by_id.get(otid)
        if ot is None:
            if selected(iid):
                errors.append(_err(
                    "object_type_not_found", "objectInstance",
                    f"实例引用的对象类型不存在: {otid}", item_id=iid))
            continue

        if selected(iid):
            _validate_property_values(
                ot, getattr(inst, "properties", None), kind="objectInstance", item_id=iid,
                errors=errors, allowed_unknown=LEGACY_SYSTEM_PROPERTIES)
            _validate_computed_values(
                ot, getattr(inst, "computed", None), kind="objectInstance",
                item_id=iid, errors=errors)

        primary_key = getattr(ot, "primary_key", None)
        if not primary_key:
            continue
        definitions = _property_defs(ot)
        pk_def = next((p for p in definitions.values()
                       if p.get("id") == primary_key or p.get("name") == primary_key), None)
        if pk_def is None:
            if selected(iid):
                errors.append(_err(
                    "invalid_primary_key", "objectInstance",
                    f"对象类型「{_label(ot)}」的主键 \"{primary_key}\" 不在属性定义中",
                    _label(ot), iid, field=str(primary_key)))
            continue
        pk_name = str(pk_def["name"])
        props = getattr(inst, "properties", None) or {}
        pk_value = props.get(pk_name) if isinstance(props, dict) else None
        if _is_empty(pk_value):
            if selected(iid):
                errors.append(_err(
                    "primary_key_missing", "objectInstance",
                    f"对象类型「{_label(ot)}」的实例缺少主键 \"{pk_name}\"",
                    _label(ot), iid, field=pk_name))
            continue
        pk_groups.setdefault((otid, _canonical(pk_value)), []).append(inst)

    for iid, count in seen_instance_ids.items():
        if count > 1 and selected(iid):
            errors.append(_err("duplicate_instance_id", "objectInstance",
                               f"实例 ID \"{iid}\" 重复（{count} 个）", item_id=iid))
    for group in pk_groups.values():
        if len(group) < 2:
            continue
        group_ids = {_id(i) for i in group}
        if validate_ids is not None and not (group_ids & validate_ids):
            continue
        for inst in group:
            iid = _id(inst)
            if selected(iid):
                errors.append(_err(
                    "duplicate_primary_key", "objectInstance",
                    f"同一对象类型下主键值重复（{len(group)} 个实例）", item_id=iid))
    return errors


def validate_link_instance_contract(link_types: list, instances: list, link_instances: list,
                                    validate_ids: Optional[set[str]] = None) -> list[dict]:
    """校验 LinkInstance 的引用、端点类型、属性、重复边与基数。"""
    errors: list[dict] = []
    lt_by_id = {_id(lt): lt for lt in link_types if _id(lt)}
    inst_by_id = {_id(i): i for i in instances if _id(i)}
    selected = lambda lid: validate_ids is None or lid in validate_ids
    valid_by_type: dict[str, list[Any]] = {}
    duplicate_groups: dict[tuple[str, str, str, str], list[Any]] = {}
    seen_ids: dict[str, int] = {}

    for link in link_instances:
        lid = _id(link)
        if lid:
            seen_ids[lid] = seen_ids.get(lid, 0) + 1
        ltid = str(getattr(link, "link_type_id", None) or "")
        source_id = str(getattr(link, "source_object_id", None) or "")
        target_id = str(getattr(link, "target_object_id", None) or "")
        lt = lt_by_id.get(ltid)
        if lt is None:
            if selected(lid):
                errors.append(_err("link_type_not_found", "linkInstance",
                                   f"链接实例引用的关系类型不存在: {ltid}", item_id=lid))
            continue
        source = inst_by_id.get(source_id)
        target = inst_by_id.get(target_id)
        if source is None and selected(lid):
            errors.append(_err("source_instance_not_found", "linkInstance",
                               f"链接源实例不存在: {source_id}", _label(lt), lid))
        if target is None and selected(lid):
            errors.append(_err("target_instance_not_found", "linkInstance",
                               f"链接目标实例不存在: {target_id}", _label(lt), lid))
        if source is None or target is None:
            continue
        source_type = str(getattr(source, "object_type_id", None) or "")
        target_type = str(getattr(target, "object_type_id", None) or "")
        endpoint_ok = True
        if source_type != getattr(lt, "source_object_type_id", None):
            endpoint_ok = False
            if selected(lid):
                errors.append(_err(
                    "source_type_mismatch", "linkInstance",
                    f"链接源实例类型应为 {getattr(lt, 'source_object_type_id', '')}，实际为 {source_type}",
                    _label(lt), lid))
        if target_type != getattr(lt, "target_object_type_id", None):
            endpoint_ok = False
            if selected(lid):
                errors.append(_err(
                    "target_type_mismatch", "linkInstance",
                    f"链接目标实例类型应为 {getattr(lt, 'target_object_type_id', '')}，实际为 {target_type}",
                    _label(lt), lid))
        if selected(lid):
            _validate_property_values(
                lt, getattr(link, "properties", None), kind="linkInstance", item_id=lid,
                errors=errors)
        if not endpoint_ok:
            continue
        valid_by_type.setdefault(ltid, []).append(link)
        duplicate_groups.setdefault((
            ltid, source_id, target_id,
            _canonical(getattr(link, "properties", None) or {}),
        ), []).append(link)

    for lid, count in seen_ids.items():
        if count > 1 and selected(lid):
            errors.append(_err("duplicate_link_instance_id", "linkInstance",
                               f"链接实例 ID \"{lid}\" 重复（{count} 个）", item_id=lid))
    for group in duplicate_groups.values():
        if len(group) < 2:
            continue
        group_ids = {_id(link) for link in group}
        if validate_ids is not None and not (group_ids & validate_ids):
            continue
        for link in group:
            lid = _id(link)
            if selected(lid):
                errors.append(_err("duplicate_link", "linkInstance",
                                   f"相同类型、端点和属性的链接重复（{len(group)} 条）",
                                   item_id=lid))

    for ltid, links in valid_by_type.items():
        cardinality = getattr(lt_by_id[ltid], "cardinality", None)
        if cardinality not in VALID_CARDINALITIES:
            continue  # schema 层另报 invalid_cardinality
        src_to_targets: dict[str, set[str]] = {}
        tgt_to_sources: dict[str, set[str]] = {}
        for link in links:
            src = str(getattr(link, "source_object_id", ""))
            tgt = str(getattr(link, "target_object_id", ""))
            src_to_targets.setdefault(src, set()).add(tgt)
            tgt_to_sources.setdefault(tgt, set()).add(src)
        for link in links:
            lid = _id(link)
            if not selected(lid):
                continue
            src = str(getattr(link, "source_object_id", ""))
            tgt = str(getattr(link, "target_object_id", ""))
            if cardinality in ("one-to-one", "many-to-one") and len(src_to_targets[src]) > 1:
                errors.append(_err(
                    "cardinality_violation", "linkInstance",
                    f"关系「{_label(lt_by_id[ltid])}」要求每个源实例最多关联一个目标实例",
                    _label(lt_by_id[ltid]), lid))
            if cardinality in ("one-to-one", "one-to-many") and len(tgt_to_sources[tgt]) > 1:
                errors.append(_err(
                    "cardinality_violation", "linkInstance",
                    f"关系「{_label(lt_by_id[ltid])}」要求每个目标实例最多关联一个源实例",
                    _label(lt_by_id[ltid]), lid))
    return errors


def _dup_names(items: Iterable[Any], kind: str, errors: list[dict]) -> None:
    seen: dict[str, int] = {}
    for it in items:
        name = getattr(it, "name", None)
        if not name:
            continue
        seen[name] = seen.get(name, 0) + 1
    for name, n in seen.items():
        if n > 1:
            errors.append(_err("duplicate_name", kind, f"名称 \"{name}\" 重复（{n} 个）", name=name))


def validate_model(object_types: list, link_types: list, actions: list,
                   functions: list, instances: list, link_instances: list) -> list[dict]:
    """校验一份完整的模型视图（全量保存直接传 body；增量保存传合并后的视图）。

    返回错误列表；空列表 = 通过。入参为 pydantic 模型或具备同名属性的对象。
    """
    errors: list[dict] = []

    _dup_names(object_types, "objectType", errors)
    _dup_names(link_types, "linkType", errors)
    _dup_names(actions, "action", errors)
    _dup_names(functions, "function", errors)

    ot_ids = {getattr(o, "id", None) for o in object_types} - {None}
    functions_by_id = {
        str(getattr(f, "id", None)): f
        for f in functions
        if getattr(f, "id", None) is not None
    }
    fn_ids = set(functions_by_id)

    # —— 对象类型：属性重名 / computed 属性函数悬挂 ——
    for ot in object_types:
        label = getattr(ot, "display_name", "") or getattr(ot, "name", "")
        object_type_id = str(getattr(ot, "id", None) or "")
        prop_names: dict[str, int] = {}
        prop_ids: set[str] = set()
        object_properties = list(
            getattr(ot, "properties", None) or [])
        computed_property_names = {
            str(prop.get("name"))
            for prop in object_properties
            if isinstance(prop, dict)
            and prop.get("name")
            and (
                prop.get("source") == "computed"
                or bool(prop.get("computed"))
            )
        }
        stored_property_names = {
            str(prop.get("name"))
            for prop in object_properties
            if isinstance(prop, dict)
            and prop.get("name")
            and not (
                prop.get("source") == "computed"
                or bool(prop.get("computed"))
            )
        } | set(LEGACY_SYSTEM_PROPERTIES)
        for p in object_properties:
            pname = p.get("name") if isinstance(p, dict) else None
            if pname:
                prop_names[pname] = prop_names.get(pname, 0) + 1
            if isinstance(p, dict) and p.get("id"):
                prop_ids.add(str(p["id"]))
            if isinstance(p, dict) and _normal_property_type(p.get("type")) not in VALID_PROPERTY_TYPES:
                errors.append(_err(
                    "invalid_property_type_definition", "objectType",
                    f"「{label}」的属性 \"{pname}\" 类型 \"{p.get('type')}\" 非法",
                    name=label, item_id=getattr(ot, "id", "") or "", field=str(pname or "")))
            if isinstance(p, dict) and (p.get("source") == "computed" or p.get("computed")):
                fid = str(p.get("functionId") or "")
                if fid and fid not in fn_ids:
                    errors.append(_err(
                        "dangling_function", "objectType",
                        f"「{label}」的计算属性 \"{pname}\" 绑定的函数不存在",
                        name=label, item_id=getattr(ot, "id", "") or ""))
                elif fid:
                    fn = functions_by_id[str(fid)]
                    for issue_code, issue_message in (
                        derived_function_contract_issues(
                            fn,
                            object_type_id=object_type_id,
                            computed_property_names=(
                                computed_property_names),
                            stored_property_names=stored_property_names,
                            expected_return_type=str(
                                p.get("type") or ""),
                        )
                    ):
                        errors.append(_err(
                            issue_code, "objectType",
                            f"「{label}」的计算属性 \"{pname}\" "
                            f"绑定函数契约无效：{issue_message}",
                            name=label,
                            item_id=object_type_id,
                            field=str(pname or ""),
                        ))
        for pname, n in prop_names.items():
            if n > 1:
                errors.append(_err(
                    "duplicate_property", "objectType",
                    f"「{label}」的属性名 \"{pname}\" 重复（{n} 个）——保存会静默丢弃前值",
                    name=label, item_id=getattr(ot, "id", "") or ""))
        primary_key = getattr(ot, "primary_key", None)
        if primary_key and primary_key not in prop_names and primary_key not in prop_ids:
            errors.append(_err(
                "invalid_primary_key", "objectType",
                f"「{label}」的主键 \"{primary_key}\" 不在属性定义中",
                name=label, item_id=getattr(ot, "id", "") or "", field=str(primary_key)))

    # —— 关系类型：端点悬挂 / 非法基数 ——
    for lt in link_types:
        label = getattr(lt, "display_name", "") or getattr(lt, "name", "")
        lid = getattr(lt, "id", "") or ""
        link_prop_names: dict[str, int] = {}
        for p in (getattr(lt, "properties", None) or []):
            if not isinstance(p, dict):
                continue
            pname = p.get("name")
            if pname:
                link_prop_names[str(pname)] = link_prop_names.get(str(pname), 0) + 1
            if _normal_property_type(p.get("type")) not in VALID_PROPERTY_TYPES:
                errors.append(_err(
                    "invalid_property_type_definition", "linkType",
                    f"关系「{label}」的属性 \"{pname}\" 类型 \"{p.get('type')}\" 非法",
                    name=label, item_id=lid, field=str(pname or "")))
        for pname, n in link_prop_names.items():
            if n > 1:
                errors.append(_err(
                    "duplicate_property", "linkType",
                    f"关系「{label}」的属性名 \"{pname}\" 重复（{n} 个）",
                    name=label, item_id=lid, field=pname))
        if getattr(lt, "source_object_type_id", None) not in ot_ids:
            errors.append(_err("dangling_endpoint", "linkType",
                               f"关系「{label}」的源对象类型不存在", name=label, item_id=lid))
        if getattr(lt, "target_object_type_id", None) not in ot_ids:
            errors.append(_err("dangling_endpoint", "linkType",
                               f"关系「{label}」的目标对象类型不存在", name=label, item_id=lid))
        if getattr(lt, "cardinality", None) not in VALID_CARDINALITIES:
            errors.append(_err("invalid_cardinality", "linkType",
                               f"关系「{label}」的基数 \"{getattr(lt, 'cardinality', '')}\" 非法",
                               name=label, item_id=lid))

    # —— 动作 / 函数：绑定的对象类型悬挂（rules 内引用由保存后的 scrub 兜底清理）——
    for a in actions:
        label = getattr(a, "display_name", "") or getattr(a, "name", "")
        otid = getattr(a, "object_type_id", None)
        if otid and otid not in ot_ids:
            errors.append(_err("dangling_binding", "action",
                               f"动作「{label}」绑定的对象类型不存在",
                               name=label, item_id=getattr(a, "id", "") or ""))
    for f in functions:
        label = getattr(f, "display_name", "") or getattr(f, "name", "")
        otid = getattr(f, "target_object_type_id", None)
        if otid and otid not in ot_ids:
            errors.append(_err("dangling_binding", "function",
                               f"函数「{label}」绑定的对象类型不存在",
                               name=label, item_id=getattr(f, "id", "") or ""))

    # —— 运行数据：schema 必须在所有写路径上真正生效 ——
    errors.extend(validate_instance_contract(object_types, instances))
    errors.extend(validate_link_instance_contract(link_types, instances, link_instances))

    return errors


def prune_dangling_data(instances: list, link_instances: list,
                        object_types: list, link_types: list) -> tuple[list, list, dict]:
    """清理实例层的悬挂引用（自动修复而非拒绝）。

    - 实例引用了不存在的对象类型 → 剔除
    - 链接实例引用了不存在的关系类型 / 端点实例 → 剔除
    返回 (保留的实例, 保留的链接实例, 清理计数)。
    """
    ot_ids = {getattr(o, "id", None) for o in object_types} - {None}
    lt_ids = {getattr(l, "id", None) for l in link_types} - {None}

    kept_instances = [i for i in instances if getattr(i, "object_type_id", None) in ot_ids]
    inst_ids = {getattr(i, "id", None) for i in kept_instances} - {None}

    kept_links = [
        li for li in link_instances
        if getattr(li, "link_type_id", None) in lt_ids
        and getattr(li, "source_object_id", None) in inst_ids
        and getattr(li, "target_object_id", None) in inst_ids
    ]
    pruned = {
        "instances": len(instances) - len(kept_instances),
        "linkInstances": len(link_instances) - len(kept_links),
    }
    return kept_instances, kept_links, pruned
