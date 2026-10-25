"""业务语义层一致性校验与只读总览

版本快照挂两层数据：snapshot_formal（结构层：objectTypes/linkTypes/actions/
functions/sentinels 五类本体集合）与 snapshot_semantic（业务语义层：七类画布 +
需求文档 + 指纹）。一致性 = 把语义层画布重放正向确定性映射
（converter._deterministic_draft，与探索草稿同一条管线，忽略其语义报告），
再与结构快照按归一化名（canvas.norm_name，与 converter 身份键同口径）逐集合 diff：

  - 画布期望有、结构无   → semantic_structure_missing
  - 结构有、画布期望无   → semantic_business_missing
  - 同名但签名不一致     → semantic_signature_mismatch
      签名口径：objectType=属性名集合；linkType=(源对象名, 目标对象名, 基数)；
      action=参数名集合；function/sentinel 只比存在性（落地后语义由工程侧填充）
  - 文档缺失/指纹不符    → semantic_document_missing / semantic_document_stale

mappings/linkMappings 是数据通道绑定，不参与业务语义校验。
本模块为纯函数：不读库、不写库，供版本/发布链路按需调用。
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from app.exploration import canvas as C
from app.exploration import converter as CV
from app.exploration.canvas import norm_name
from app.exploration.document import canvas_fingerprint
from app.ontologies.versions.gate_contract import gate_error
from pydantic import ValidationError
from app.ontologies.versions.snapshot_contract import complete_snapshot

# 参与校验的五类集合；kind 与发布闸门 gate_error 的 kind 口径对齐。
_COLLECTIONS = ("objectTypes", "linkTypes", "actions", "functions", "sentinels")
_KIND = {"objectTypes": "objectType", "linkTypes": "linkType", "actions": "action",
         "functions": "function", "sentinels": "sentinel"}
_LABEL = {"objectTypes": "对象", "linkTypes": "链接", "actions": "动作",
          "functions": "函数", "sentinels": "哨兵"}
_CANVAS_LABEL = {"objectTypes": "对象/主体", "linkTypes": "关系", "actions": "行为",
                 "functions": "规则", "sentinels": "事件"}
_CANVAS_SCHEMA_KEYS = set(C.KIND_KEYS.values())
_CANVAS_MAX_ELEMENTS = 5000
_CANVAS_NESTED_MAX_ITEMS = 500


def _model_allowed_keys(model) -> set[str]:
    keys = set(model.model_fields)
    for field in model.model_fields.values():
        if isinstance(field.alias, str) and field.alias:
            keys.add(field.alias)
    return keys


def _model_issue(kind: str, path: str, message: str,
                 item: dict | None = None) -> dict:
    return gate_error(
        "semantic_canvas_invalid", "canvas", message,
        item_id=str((item or {}).get("id") or ""),
        name=str((item or {}).get("name") or path))


def _validate_element_schema(kind: str, index: int, item: Any,
                             issues: list[dict]) -> None:
    path = f"{C.KIND_KEYS[kind]}[{index}]"
    if not isinstance(item, dict):
        issues.append(_model_issue(kind, path,
                                   f"{path} 必须是对象，收到 {type(item).__name__}"))
        return
    model = C.KIND_MODELS[kind]
    unknown = sorted(set(item) - _model_allowed_keys(model))
    if unknown:
        issues.append(_model_issue(
            kind, path,
            f"{path} 含有 canonical 模型不接受的字段: {', '.join(unknown)}",
            item))
    nested_models = C._NESTED_MODELS.get(kind, {})
    for nested, child_model in nested_models.items():
        if nested not in item:
            continue
        children = item.get(nested)
        if not isinstance(children, list):
            issues.append(_model_issue(
                kind, path, f"{path}.{nested} 必须是数组"))
            continue
        if len(children) > _CANVAS_NESTED_MAX_ITEMS:
            issues.append(_model_issue(
                kind, path,
                f"{path}.{nested} 超过 {_CANVAS_NESTED_MAX_ITEMS} 项上限"))
        allowed_child = _model_allowed_keys(child_model)
        for child_index, child in enumerate(children):
            child_path = f"{path}.{nested}[{child_index}]"
            if not isinstance(child, dict):
                issues.append(_model_issue(
                    kind, child_path,
                    f"{child_path} 必须是对象，收到 {type(child).__name__}"))
                continue
            child_unknown = sorted(set(child) - allowed_child)
            if child_unknown:
                issues.append(_model_issue(
                    kind, child_path,
                    f"{child_path} 含有不接受的字段: {', '.join(child_unknown)}"))
            try:
                child_model.model_validate(child)
            except ValidationError as exc:
                issues.append(_model_issue(
                    kind, child_path,
                    f"{child_path} 不符合 canonical schema: "
                    f"{'; '.join(str(e.get('msg')) for e in exc.errors()[:3])}"))
    try:
        model.model_validate(item)
    except ValidationError as exc:
        issues.append(_model_issue(
            kind, path,
            f"{path} 不符合 canonical schema: "
            f"{'; '.join(str(e.get('msg')) for e in exc.errors()[:3])}",
            item))


def canvas_schema_issues(canvas: Any) -> list[dict]:
    """Strict canonical canvas validation for the semantic write/release copy.

    ``_ensure_canvas`` intentionally tolerates unknown keys; the write path must
    not: a release copies snapshot_semantic verbatim.  Unknown top-level keys,
    unknown element/nested fields, canonical model validation, and element /
    nested-list bounds are rejected here before any replay/fingerprint comparison.
    """
    issues: list[dict] = []
    if not isinstance(canvas, dict):
        return [_model_issue("canvas", "canvas",
                             "canvas 必须是 JSON 对象")]
    unknown = sorted(set(canvas) - _CANVAS_SCHEMA_KEYS)
    if unknown:
        issues.append(_model_issue(
            "canvas", "canvas",
            f"canvas 含有 unknown top-level keys: {', '.join(unknown)}"))
    total = 0
    for kind, key in C.KIND_KEYS.items():
        values = canvas.get(key)
        if values is None:
            continue
        if not isinstance(values, list):
            issues.append(_model_issue(kind, key, f"canvas.{key} 必须是数组"))
            continue
        if len(values) > _CANVAS_MAX_ELEMENTS:
            issues.append(_model_issue(
                kind, key,
                f"canvas.{key} 超过 {_CANVAS_MAX_ELEMENTS} 项上限"))
        total += len(values)
        for index, item in enumerate(values):
            _validate_element_schema(kind, index, item, issues)
    if total > _CANVAS_MAX_ELEMENTS:
        issues.append(_model_issue(
            "canvas", "canvas",
            f"canvas 超过 {_CANVAS_MAX_ELEMENTS} 项总上限"))
    return issues


def _text(value: Any) -> str:
    return str(value or "").strip()


def _display(item: dict) -> str:
    return _text(item.get("displayName")) or _text(item.get("name"))


def _key_of(item: dict) -> str:
    return norm_name(_text(item.get("name")))


def _index_by_name(items: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = _key_of(item)
        if key:
            out.setdefault(key, item)
    return out


def _names_of(items: Any) -> set[str]:
    """属性/参数子项的归一化名集合（签名成员与身份键同口径）。"""
    return {norm_name(_text(p.get("name")))
            for p in (items or []) if isinstance(p, dict)} - {""}


def _raw_names_of(items: Any) -> list[str]:
    """属性/参数子项的原始名（仅用于人读文案，比对一律用归一化名）。"""
    return sorted({_text(p.get("name"))
                   for p in (items or []) if isinstance(p, dict)} - {""})


def _fmt_names(names: list[str]) -> str:
    return "、".join(names) if names else "（无）"


def _link_endpoints(item: dict, obj_name_by_id: Optional[dict[str, str]]) -> tuple[str, str]:
    """链接端点对象名：画布侧草稿直接记 sourceName/targetName；
    结构侧端点是 objectType uuid，先按快照对象表解析回名字。"""
    if obj_name_by_id is None:
        return _text(item.get("sourceName")), _text(item.get("targetName"))
    return (obj_name_by_id.get(_text(item.get("sourceObjectTypeId")), ""),
            obj_name_by_id.get(_text(item.get("targetObjectTypeId")), ""))


def _link_signature(item: dict, obj_name_by_id: Optional[dict[str, str]]) -> tuple[str, str, str]:
    source, target = _link_endpoints(item, obj_name_by_id)
    cardinality = _text(item.get("cardinality")).lower() or "one-to-many"
    return (norm_name(source), norm_name(target), cardinality)


def _signature_detail(collection: str, expected_item: dict, actual_item: dict,
                      obj_name_by_id: dict[str, str]) -> str:
    if collection == "objectTypes":
        return (f"画布期望属性 [{_fmt_names(_raw_names_of(expected_item.get('properties')))}]，"
                f"结构实际属性 [{_fmt_names(_raw_names_of(actual_item.get('properties')))}]")
    if collection == "actions":
        return (f"画布期望参数 [{_fmt_names(_raw_names_of(expected_item.get('parameters')))}]，"
                f"结构实际参数 [{_fmt_names(_raw_names_of(actual_item.get('parameters')))}]")
    if collection == "linkTypes":
        e_source, e_target = _link_endpoints(expected_item, None)
        a_source, a_target = _link_endpoints(actual_item, obj_name_by_id)
        e_card = _text(expected_item.get("cardinality")).lower() or "one-to-many"
        a_card = _text(actual_item.get("cardinality")).lower() or "one-to-many"
        return (f"画布期望 {e_source or '?'}→{e_target or '?'}（基数 {e_card}），"
                f"结构实际 {a_source or '?'}→{a_target or '?'}（基数 {a_card}）")
    return ""


def _collection_issues(collection: str, expected_items: Any, actual_items: Any,
                       obj_name_by_id: dict[str, str]) -> list[dict]:
    kind = _KIND[collection]
    label = _LABEL[collection]
    expected = _index_by_name(expected_items)
    actual = _index_by_name(actual_items)
    issues: list[dict] = []
    for key, item in expected.items():
        if key not in actual:
            issues.append(gate_error(
                "semantic_structure_missing", kind,
                f"结构中缺少画布模型对应的{label}「{_display(item)}」",
                item_id=_text(item.get("name")), name=_display(item)))
    for key, item in actual.items():
        if key not in expected:
            issues.append(gate_error(
                "semantic_business_missing", kind,
                f"结构中的{label}「{_display(item)}」在业务画布中没有对应"
                f"{_CANVAS_LABEL[collection]}，请到业务澄清补齐业务语义",
                item_id=_text(item.get("name")), name=_display(item)))
    # functions/sentinels 只比存在性，不参与签名比对
    if collection in ("functions", "sentinels"):
        return issues
    for key in expected.keys() & actual.keys():
        exp_item, act_item = expected[key], actual[key]
        if collection == "objectTypes":
            matched = _names_of(exp_item.get("properties")) == _names_of(act_item.get("properties"))
        elif collection == "actions":
            matched = _names_of(exp_item.get("parameters")) == _names_of(act_item.get("parameters"))
        else:
            matched = _link_signature(exp_item, None) == _link_signature(act_item, obj_name_by_id)
        if not matched:
            issues.append(gate_error(
                "semantic_signature_mismatch", kind,
                f"结构中的{label}「{_display(act_item)}」与画布期望的签名不一致"
                f"（{_signature_detail(collection, exp_item, act_item, obj_name_by_id)}），"
                "请确认以业务画布还是本体结构为准",
                item_id=_text(act_item.get("name")), name=_display(act_item)))
    return issues


def _document_issues(sem: dict, canvas: dict) -> list[dict]:
    canvas_non_empty = any(canvas[key] for key in C.KIND_KEYS.values())
    document_md = sem.get("documentMd")
    document_md = document_md if isinstance(document_md, str) else ""
    title = _text(sem.get("documentTitle"))
    doc_label = f"「{title}」" if title else ""
    name = title or "需求文档"
    issues: list[dict] = []
    if canvas_non_empty and not document_md.strip():
        issues.append(gate_error(
            "semantic_document_missing", "document",
            "业务画布已有模型内容，但语义层缺少需求文档，请回到探索会话生成需求文档",
            item_id="document", name=name))
    document_fp = _text(sem.get("documentFingerprint"))
    if document_md and document_fp:
        actual_fp = hashlib.sha256(document_md.encode("utf-8")).hexdigest()
        if actual_fp != document_fp:
            issues.append(gate_error(
                "semantic_document_stale", "document",
                f"需求文档{doc_label}的内容指纹与语义层记录不符，文档已被修改，请重新生成",
                item_id="document", name=name, field="documentFingerprint"))
    canvas_fp = _text(sem.get("canvasFingerprint"))
    if canvas_fp and canvas_fingerprint(canvas) != canvas_fp:
        issues.append(gate_error(
            "semantic_document_stale", "document",
            f"语义层记录的画布指纹与画布现算指纹不符，画布在语义层沉淀后已变更，"
            f"需求文档{doc_label}与结构快照可能已过期",
            item_id="document", name=name, field="canvasFingerprint"))
    return issues


def semantic_consistency_issues(snapshot_semantic: dict | None,
                                snapshot_formal: dict | None) -> list[dict]:
    """重放正向映射并与结构快照 diff，返回 gate_error 形状的一致性 issue 列表。

    snapshot_semantic 缺失/None 时画布按空画布处理（语义层尚未沉淀的版本，
    结构元素会全部计为 semantic_business_missing，由调用方结合
    semantic_overview 的 hasSemanticLayer 决定是否展示）。
    """
    sem = snapshot_semantic if isinstance(snapshot_semantic, dict) else {}
    raw_canvas = sem.get("canvas")
    if raw_canvas is None:
        canvas = C.empty_canvas()
    else:
        schema_issues = canvas_schema_issues(raw_canvas)
        if schema_issues:
            return schema_issues
        canvas = C._ensure_canvas(raw_canvas)
    formal = complete_snapshot(snapshot_formal)

    warnings: list[str] = []  # 重放只取五类集合，warning/semanticIssues 不进闸门
    draft = CV._deterministic_draft(canvas, warnings)

    obj_name_by_id = {_text(ot.get("id")): _text(ot.get("name"))
                      for ot in formal["objectTypes"] if isinstance(ot, dict)}

    issues: list[dict] = []
    for collection in _COLLECTIONS:
        issues.extend(_collection_issues(
            collection, draft.get(collection), formal.get(collection), obj_name_by_id))
    issues.extend(_document_issues(sem, canvas))
    return issues


def semantic_overview(snapshot_semantic: dict | None,
                      snapshot_formal: dict | None) -> dict:
    """业务语义层只读总览：计数、文档状态与一致性聚合，不阻断任何流程。"""
    sem = snapshot_semantic if isinstance(snapshot_semantic, dict) else {}
    canvas = C._ensure_canvas(sem.get("canvas"))
    formal = complete_snapshot(snapshot_formal)
    issues = semantic_consistency_issues(snapshot_semantic, snapshot_formal)
    by_code: dict[str, int] = {}
    for issue in issues:
        code = _text(issue.get("code"))
        by_code[code] = by_code.get(code, 0) + 1
    title = _text(sem.get("documentTitle"))
    return {
        "hasSemanticLayer": bool(sem),
        "documentTitle": title or None,
        "documentStale": any(i.get("code") == "semantic_document_stale" for i in issues),
        "canvasCounts": {key: len(canvas[key]) for key in C.KIND_KEYS.values()},
        "structureCounts": {collection: len(formal[collection]) for collection in _COLLECTIONS},
        "consistency": {"issueCount": len(issues), "byCode": by_code},
    }
