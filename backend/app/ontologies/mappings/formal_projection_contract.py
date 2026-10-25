"""Pure identity, binding, and value contracts for Formal projection."""
from __future__ import annotations

import re
import uuid as _uuid
from typing import Any

from app.ontologies.mappings.formal_projection_values import _coerce_props_to_type, _display_value


# Entity.properties 中属于"系统/溯源"的键，不作为业务属性投影
_RESERVED_PROP_KEYS = {
    "id", "ontology_id", "source_id", "object_type",
    "source_row_count", "name", "name_cn", "name_en",
    "name_abbr", "display_name",
    "__mapping_ids__", "__business_properties__",
}


def projection_property_mappings(field_mapping: dict | None) -> list[dict]:
    """Return the executable source-column → Formal-property contract.

    ``__properties__`` is enriched draft-time metadata, but it is not the
    authority for which source column writes which property.  Released
    mappings deliberately skip runtime normalization so an older release (or
    a mapping created before its first data build) may have no
    ``__properties__`` at all.  The explicit non-technical field mapping must
    therefore be sufficient to recover identity and data bindings.
    """
    field_mapping = dict(field_mapping or {})
    metadata_by_column = {
        str(item.get("column")): dict(item)
        for item in (field_mapping.get("__properties__") or [])
        if isinstance(item, dict) and item.get("column")
    }
    ignored = {
        str(item) for item in (field_mapping.get("__ignored_fields__") or [])
    }
    primary_key_columns = {
        part.strip()
        for part in str(field_mapping.get("__primary_key__") or "").split(",")
        if part.strip()
    }

    result: list[dict] = []
    for source_column, target_property in field_mapping.items():
        source_column = str(source_column)
        if source_column.startswith("__"):
            continue
        if source_column in ignored and source_column not in primary_key_columns:
            continue
        if target_property in (None, ""):
            continue
        item = dict(metadata_by_column.pop(source_column, {}))
        item["column"] = source_column
        # The explicit mapping is the immutable authority.  Preserve inferred
        # type/display metadata, but never let stale metadata redirect writes.
        item["property"] = str(target_property)
        result.append(item)

    # Compatibility for legacy metadata whose field map was incompletely
    # persisted.  These rows still provide useful binding/type hints, while a
    # declared ignored field remains excluded.
    for source_column, item in metadata_by_column.items():
        if source_column in ignored and source_column not in primary_key_columns:
            continue
        item.setdefault("column", source_column)
        item.setdefault("property", source_column)
        result.append(item)
    return result


def _stable_id(*parts: Any) -> str:
    """根据语义键生成确定性 UUID，保证多次投影幂等。"""
    raw = ":".join(str(p) for p in parts)
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, raw))


def stable_pipeline_entity_id(
    ontology_id: str, entity_class: str, row_identity: str,
) -> str:
    """Return the canonical legacy ``Entity.id`` used by MappingService.

    Version trials materialize directly into the Formal projection and used to
    invent a different identity scheme.  Keeping the canonical formulas here
    gives trials, promotion and the normal MappingService projection one
    contract without requiring trial code to write the legacy Entity table.
    """
    return _stable_id(ontology_id, entity_class, row_identity)


def stable_object_instance_id(ontology_id: str, entity_id: str) -> str:
    """Return the canonical pipeline-backed ``ObjectInstance.id``."""
    return _stable_id("oi", ontology_id, entity_id)


def stable_pipeline_relation_id(
    ontology_id: str,
    source_entity_id: str,
    target_entity_id: str,
    relation_type: str,
    source: str,
) -> str:
    """Return the canonical MappingService ``Relation.id``."""
    return _stable_id(
        ontology_id, source_entity_id, relation_type, target_entity_id, source)


def stable_link_instance_id(
    ontology_id: str,
    link_type_id: str,
    source_object_id: str,
    target_object_id: str,
    edge_key: str = "",
) -> str:
    """Return the canonical pipeline-backed ``LinkInstance.id``."""
    return _stable_id(
        "li", ontology_id, link_type_id, source_object_id, target_object_id,
        edge_key)


def _infer_property_type(values: list[Any]) -> str:
    """根据样本值粗略推断 PropertyType（与前端 PropertyType 对齐）。"""
    non_null = [v for v in values if v not in (None, "")]
    if not non_null:
        return "string"
    sample = non_null[: min(len(non_null), 20)]

    def _is_bool(v: Any) -> bool:
        return isinstance(v, bool) or str(v).strip().lower() in ("true", "false", "是", "否")

    def _is_num(v: Any) -> bool:
        if isinstance(v, bool):
            return False
        if isinstance(v, (int, float)):
            return True
        try:
            float(str(v).replace(",", ""))
            return True
        except (ValueError, TypeError):
            return False

    def _is_datetime(v: Any) -> bool:
        s = str(v)
        return bool(re.match(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2})?", s))

    if all(isinstance(v, (list, tuple)) for v in sample):
        return "array"
    if all(isinstance(v, dict) for v in sample):
        return "object"
    if all(_is_bool(v) for v in sample):
        return "boolean"
    if all(_is_num(v) for v in sample):
        return "number"
    if all(_is_datetime(v) for v in sample):
        return "datetime"
    return "string"


def _pick_first(data: dict, *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _property_data_binding(
    prop_name: str, meta: dict, binding_context: dict | None,
) -> dict:
    source_column = _pick_first(
        meta,
        "sourceColumn", "source_column",
        "column", "column_name",
        "originalColumn", "original_column",
    )
    original_column = _pick_first(meta, "originalColumn", "original_column", "column", "column_name")
    inferred = not bool(source_column)

    binding = {
        "sourceColumn": str(source_column or prop_name),
        "inferred": inferred,
    }
    if original_column:
        binding["originalColumn"] = str(original_column)
    if binding_context:
        if binding_context.get("mapping_id"):
            binding["mappingId"] = binding_context["mapping_id"]
        if binding_context.get("curated_dataset_id"):
            binding["curatedDatasetId"] = binding_context["curated_dataset_id"]
        if binding_context.get("source_dataset_version_id"):
            binding["datasetVersionId"] = binding_context["source_dataset_version_id"]
        if binding_context.get("dataset_name"):
            binding["datasetName"] = binding_context["dataset_name"]
    return binding


def _merge_properties(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """属性合并：已有定义（按 name 匹配）原样保留，只追加数据里新出现的属性。

    这是"人工绑定和维护"的护栏——用户在类型上手工加的 computed 属性、
    改过的类型/显示名/校验配置，不能被重跑投影的数据推断结果覆盖。
    """
    existing = [p for p in (existing or []) if isinstance(p, dict)]
    seen = {p.get("name") for p in existing if p.get("name")}
    out = list(existing)
    for p in (incoming or []):
        if isinstance(p, dict) and p.get("name") and p["name"] not in seen:
            out.append(p)
            seen.add(p["name"])
    return out


def _build_object_type_properties(
    entities: list[dict], pk_field: str | list[str], property_mappings: list[dict] | None,
    binding_context: dict | None = None,
) -> tuple[list[dict], str]:
    """
    从一批 Entity props 推导 ObjectType.properties (Property[])。

    返回 (properties, primary_key_property_id)。
    """
    pk_fields = ([str(item) for item in pk_field if str(item)]
                 if isinstance(pk_field, list)
                 else [part.strip() for part in str(pk_field or "").split(",")
                       if part.strip()])
    # Entity's compatibility envelope also owns keys such as id/name. Explicit
    # field mappings are the authority that distinguishes same-named business
    # properties restored from __business_properties__ from that metadata.
    explicit_business_keys = set(pk_fields)
    for item in (property_mappings or []):
        if not isinstance(item, dict):
            continue
        property_name = (
            item.get("property") or item.get("name") or item.get("column")
        )
        if property_name:
            explicit_business_keys.add(str(property_name))

    # Visit the complete relation, retaining only the same first 20 non-null
    # values which _infer_property_type has always used. Storage is O(columns),
    # not O(rows × columns); late-appearing and all-null columns remain visible.
    samples: dict[str, list[Any]] = {}
    for ent in entities:
        for k, v in ent.items():
            if k in _RESERVED_PROP_KEYS and k not in explicit_business_keys:
                continue
            values = samples.setdefault(k, [])
            if len(values) < 20 and v not in (None, ""):
                values.append(v)

    # property_mappings 提供更准确的 displayName / type 提示
    meta_by_name: dict[str, dict] = {}
    for pm in (property_mappings or []):
        # property_mappings 元素形如 {"column": ..., "property": ..., "type": ..., "display_name": ...}
        prop_name = pm.get("property") or pm.get("name") or pm.get("column")
        if prop_name:
            meta_by_name[prop_name] = pm

    pk_rank = {name: index + 1 for index, name in enumerate(pk_fields)}
    synthetic_composite = (
        "__composite_identity__" if len(pk_fields) > 1
        and "__composite_identity__" in samples else None)
    storage_pk_fields = [synthetic_composite] if synthetic_composite else pk_fields
    properties: list[dict] = []
    pk_prop_id = ""
    # 确保复合主键各分量按契约顺序优先出现
    ordered_keys = sorted(
        samples.keys(),
        key=lambda k: (
            (0, 0) if k == synthetic_composite
            else (1, pk_rank[k]) if k in pk_rank
            else (2, str(k))))
    for key in ordered_keys:
        pid = f"prop_{re.sub(r'[^A-Za-z0-9_]', '_', str(key))}"
        meta = meta_by_name.get(key, {})
        ptype = meta.get("type") or _infer_property_type(samples[key])
        if ptype not in ("string", "number", "boolean", "date", "datetime", "array", "object", "reference"):
            ptype = "string"
        prop = {
            "id": pid,
            "name": str(key),
            "displayName": meta.get("display_name") or meta.get("displayName") or str(key),
            "type": ptype,
            "required": bool(key in pk_rank or key in storage_pk_fields),
            "source": "stored",
            "dataBinding": _property_data_binding(str(key), meta, binding_context),
        }
        if key in pk_rank:
            prop["primaryKeyPart"] = pk_rank[key]
        if key == synthetic_composite:
            prop["technical"] = True
            prop["identityComponents"] = pk_fields
        properties.append(prop)
        if key in storage_pk_fields and not pk_prop_id:
            pk_prop_id = pid

    found_components = {p["name"] for p in properties if p.get("primaryKeyPart")}
    if pk_fields and found_components != set(pk_fields):
        missing = [field for field in pk_fields if field not in found_components]
        raise ValueError(f"正规本体投影缺少主键属性：{missing}")

    # 仅在没有上游身份契约时提供旧数据兼容推断；有契约却找不到列必须失败，
    # 不能悄悄把第一列变成另一套实例身份。
    if not pk_fields and not pk_prop_id and properties:
        _id_keywords = ("id", "code", "key", "编号", "编码", "no", "number")
        _name_keywords = ("name", "title", "名称", "标题")

        def _rank(p: dict) -> int:
            n = str(p.get("name", "")).lower()
            if any(k in n for k in _id_keywords):
                return 0
            if any(k in n for k in _name_keywords):
                return 1
            return 2

        best = min(properties, key=_rank)
        pk_prop_id = best["id"]
        best["required"] = True
    return properties, pk_prop_id
