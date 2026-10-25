"""Fail closed before SQL JSON serialization erases a raw-JSON string marker."""
from __future__ import annotations

from sqlalchemy import or_

from app.shared.value_types import JsonText
from app.ontologies.formal_modeling.models import ObjectType, LinkType


def structured_json_columns(db, mapping, *, edge=False):
    """Resolve the target schema once per mapping execution, never per row."""
    model = LinkType if edge else ObjectType
    bound = getattr(mapping, "link_type_id" if edge else "target_object_type_id", None)
    name = mapping.relation_type if edge else mapping.entity_class
    target = None
    if bound:
        target = db.query(model).filter(model.ontology_id == mapping.ontology_id, model.id == bound).first()
    if target is None:
        target = db.query(model).filter(model.ontology_id == mapping.ontology_id,
            or_(model.name == name, model.display_name == name)).first()
    declared = {prop.get("name"): prop.get("type") for prop in (target.properties if target else []) or []
                if isinstance(prop, dict)}
    field_map = mapping.field_mapping or {}
    columns = {str(column) for column, prop in field_map.items() if not str(column).startswith("__")
               and declared.get(prop) in ("object", "array", "number", "integer", "float")} if not edge else {
        str(column) for prop, column in field_map.items() if declared.get(prop) in ("object", "array", "number", "integer", "float")}
    # New data-first types may not exist yet. Explicit property metadata is also
    # a typed contract; never let its raw values reach a later legacy JSON parse.
    columns.update(str(prop["column"]) for prop in field_map.get("__properties__", [])
                   if isinstance(prop, dict) and prop.get("column") and prop.get("type") in ("object", "array", "number", "integer", "float"))
    return columns


def reject_raw_json_columns(rows, columns):
    for row in rows:
        for column in columns:
            if isinstance(row.get(column), JsonText):
                raise ValueError(
                    f"源列 {column} 的原生 raw JSON 请映射 string；结构化/数值语义未支持，已拒绝有损转换")
