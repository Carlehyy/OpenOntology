"""Validation rules shared by the ontology mapping application workflows.

The HTTP router re-exports these private helpers for compatibility with older
tests and integrations.  Keeping the rules here prevents the transport adapter
from accumulating dataset, ontology, and release-policy knowledge.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session


def _validate_target_type(
    db: Session,
    ontology_id: str,
    type_id: Optional[str],
):
    if not type_id:
        return None
    from app.models.ontology_formal import ObjectType

    ot = db.query(ObjectType).filter(
        ObjectType.id == type_id,
        ObjectType.ontology_id == ontology_id,
    ).first()
    if not ot:
        raise HTTPException(422, f"绑定的对象实体不存在: {type_id}")
    return ot


def _normal_mapping_type(raw: object) -> str:
    """Normalize lake and ontology type vocabularies for mapping contracts."""
    value = str(raw or "string").strip().lower().split("(", 1)[0]
    aliases = {
        "str": "string",
        "text": "string",
        "varchar": "string",
        "char": "string",
        "uuid": "string",
        "int": "number",
        "integer": "number",
        "bigint": "number",
        "smallint": "number",
        "float": "number",
        "double": "number",
        "decimal": "number",
        "decimal128": "number",
        "date": "datetime",
        "timestamp": "datetime",
        "time": "datetime",
        "bool": "boolean",
        "list": "array",
        "set": "array",
        "object": "json",
        "map": "json",
    }
    return aliases.get(value, value)


def _mapping_types_compatible(source_type: str, target_type: str) -> bool:
    """Allow a lake JSON column to feed an ontology array property.

    The manual-dataset contract deliberately stores nested lists as JSON, while
    the formal ontology vocabulary exposes them as ``array``.  Treating that
    lossless representation as incompatible made array properties impossible
    to map through the asset lake.
    """
    return source_type == target_type or (
        source_type == "json" and target_type == "array"
    )


def _dataset_column_types(db: Session, dataset_id: str) -> dict[str, str]:
    from app.data_channel.datasets.models import Dataset

    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if dataset is None:
        raise HTTPException(
            404,
            detail={
                "code": "mapping_dataset_not_found",
                "message": f"映射数据集不存在或尚未迁入资产湖：{dataset_id}",
            },
        )
    schema = dataset.schema_json if isinstance(dataset.schema_json, dict) else {}
    return {
        str(column.get("name")): _normal_mapping_type(column.get("type"))
        for column in (schema.get("columns_typed") or [])
        if isinstance(column, dict) and column.get("name")
    }


def _assert_mapping_types_compatible(
    db: Session,
    dataset_id: str,
    target_type: object | None,
    field_mapping: dict,
) -> None:
    """Enforce source-to-property compatibility at the trusted API boundary.

    Older imported datasets may not yet carry ``columns_typed``.  Those remain
    readable for backwards compatibility; every lake-managed dataset with a
    typed contract is checked strictly.
    """
    if target_type is None:
        return
    source_types = _dataset_column_types(db, dataset_id)
    if not source_types:
        return
    properties = {
        str(item.get("name")): item
        for item in (getattr(target_type, "properties", None) or [])
        if isinstance(item, dict) and item.get("name")
    }
    failures: list[dict] = []
    for source, target in (field_mapping or {}).items():
        source_name, target_name = str(source), str(target)
        source_type = source_types.get(source_name)
        target_property = properties.get(target_name)
        if source_type is None:
            failures.append(
                {
                    "source": source_name,
                    "target": target_name,
                    "reason": "source_column_not_found",
                }
            )
            continue
        if target_property is None:
            failures.append(
                {
                    "source": source_name,
                    "target": target_name,
                    "reason": "target_property_not_found",
                }
            )
            continue
        target_type_name = _normal_mapping_type(target_property.get("type"))
        if not _mapping_types_compatible(source_type, target_type_name):
            failures.append(
                {
                    "source": source_name,
                    "source_type": source_type,
                    "target": target_name,
                    "target_type": target_type_name,
                    "reason": "type_mismatch",
                }
            )
    if failures:
        raise HTTPException(
            422,
            detail={
                "code": "mapping_type_mismatch",
                "message": "字段映射包含不存在的字段或不兼容的数据类型，请修正后再保存。",
                "errors": failures,
            },
        )


def _assert_link_mapping_types_compatible(
    db: Session,
    *,
    src_dataset_id: str,
    tgt_dataset_id: str,
    edge_dataset_id: str | None,
    src_key: str,
    tgt_key: str,
    link_type: object | None,
    field_mapping: dict,
) -> None:
    """Validate relation endpoint keys and edge properties before persistence."""
    from app.data_channel.datasets.lake_gate import split_pk

    if field_mapping and not edge_dataset_id:
        raise HTTPException(
            422,
            detail={
                "code": "edge_dataset_required_for_properties",
                "message": "关系属性必须来自同一张连接表；请先选择关系数据集再映射属性。",
            },
        )

    src_types = _dataset_column_types(db, src_dataset_id)
    tgt_types = _dataset_column_types(db, tgt_dataset_id)
    edge_types = (
        _dataset_column_types(db, edge_dataset_id) if edge_dataset_id else None
    )
    failures: list[dict] = []

    def compare(
        source_name: str,
        source_type: str | None,
        target_name: str,
        target_type: str | None,
        role: str,
        *,
        allow_structured_representation: bool = False,
    ) -> None:
        # Untyped legacy assets remain operable; typed contracts are strict.
        if source_type is None or target_type is None:
            return
        # Only business edge properties may use a lossless lake
        # representation such as JSON → array. Endpoint identity keys keep
        # exact type equality and never opt into this branch.
        compatible = (
            _mapping_types_compatible(source_type, target_type)
            if allow_structured_representation
            else source_type == target_type
        )
        if not compatible:
            failures.append(
                {
                    "role": role,
                    "source": source_name,
                    "source_type": source_type,
                    "target": target_name,
                    "target_type": target_type,
                    "reason": "type_mismatch",
                }
            )

    src_pk = split_pk(_canonical_primary_key(db, src_dataset_id))[0]
    tgt_pk = split_pk(_canonical_primary_key(db, tgt_dataset_id))[0]
    if edge_types is not None:
        compare(
            src_key,
            edge_types.get(src_key),
            src_pk,
            src_types.get(src_pk),
            "source_endpoint",
        )
        compare(
            tgt_key,
            edge_types.get(tgt_key),
            tgt_pk,
            tgt_types.get(tgt_pk),
            "target_endpoint",
        )
    else:
        compare(
            src_key,
            src_types.get(src_key),
            tgt_key,
            tgt_types.get(tgt_key),
            "endpoint_join",
        )

    if link_type is not None:
        properties = {
            str(item.get("name")): item
            for item in (getattr(link_type, "properties", None) or [])
            if isinstance(item, dict) and item.get("name")
        }
        property_source_types = edge_types or {}
        for target_property, source_column in (field_mapping or {}).items():
            target_name, source_name = str(target_property), str(source_column)
            source_type = property_source_types.get(source_name)
            target = properties.get(target_name)
            if edge_types is not None and source_type is None:
                failures.append(
                    {
                        "role": "edge_property",
                        "source": source_name,
                        "target": target_name,
                        "reason": "source_column_not_found",
                    }
                )
                continue
            if target is None:
                failures.append(
                    {
                        "role": "edge_property",
                        "source": source_name,
                        "target": target_name,
                        "reason": "target_property_not_found",
                    }
                )
                continue
            compare(
                source_name,
                source_type,
                target_name,
                _normal_mapping_type(target.get("type")),
                "edge_property",
                allow_structured_representation=True,
            )

    if failures:
        raise HTTPException(
            422,
            detail={
                "code": "link_mapping_type_mismatch",
                "message": "关系映射的端点外键或关系属性类型不兼容，请修正后再保存。",
                "errors": failures,
            },
        )


def _require_draft_ontology(db: Session, ontology_id: str) -> None:
    """发布后冻结 Mapping 结构；数据实例仍可通过 apply-from-dataset 更新。"""
    from app.models.ontology import OntologyProject

    project = (
        db.query(OntologyProject)
        .filter(OntologyProject.id == ontology_id)
        .with_for_update()
        .first()
    )
    if project is None:
        raise HTTPException(404, "Ontology not found")
    if project.status != "draft":
        raise HTTPException(
            409,
            "本体已发布，Mapping/LinkMapping 结构已冻结；"
            "请先创建或切换到 draft 版本再维护映射",
        )


def _lock_ontology(db: Session, ontology_id: str):
    """Lock an ontology for guarded non-structural mapping operations."""
    from app.models.ontology import OntologyProject

    project = (
        db.query(OntologyProject)
        .filter(OntologyProject.id == ontology_id)
        .with_for_update()
        .first()
    )
    if project is None:
        raise HTTPException(404, "Ontology not found")
    return project


def _validate_version_automation_policy(db: Session, dataset_id: str) -> None:
    from app.data_channel.datasets.automation_policy import (
        manual_dataset_automation_eligibility,
    )
    from app.data_channel.datasets.models import Dataset, DatasetVersion

    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    version = (
        db.query(DatasetVersion)
        .filter(DatasetVersion.dataset_id == dataset_id)
        .order_by(DatasetVersion.version_no.desc())
        .first()
    )
    if dataset is None:
        raise HTTPException(404, "Mapping dataset not found")
    eligible, reason = manual_dataset_automation_eligibility(dataset, version)
    if not eligible:
        raise HTTPException(
            409,
            detail={
                "code": "manual_version_automation_not_eligible",
                "message": (
                    "仅具备主键契约和可校验不可变版本的人工数据集可开启版本后自动灌入；"
                    f"当前不满足：{reason}"
                ),
            },
        )


def _validate_link_version_automation_policy(
    db: Session,
    dataset_ids: set[str | None],
) -> None:
    """A link subscription may mix reviewed curated and governed manual inputs."""
    from app.data_channel.datasets.models import Dataset

    manual_ids: list[str] = []
    for dataset_id in dataset_ids - {None}:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if dataset is None:
            raise HTTPException(
                404,
                f"Link mapping dataset not found: {dataset_id}",
            )
        if dataset.kind != "curated":
            manual_ids.append(dataset_id)
    if not manual_ids:
        raise HTTPException(
            409,
            detail={
                "code": "manual_version_automation_not_applicable",
                "message": "关系映射没有人工数据依赖；Curated 数据请使用审核通过触发链路。",
            },
        )
    for dataset_id in manual_ids:
        _validate_version_automation_policy(db, dataset_id)


def _reject_reserved_mapping_keys(
    value: Optional[dict],
    field_name: str,
) -> None:
    """System lineage keys are write-only for the mapping runtime."""
    reserved = sorted(
        str(key) for key in (value or {}) if str(key).startswith("__")
    )
    if reserved:
        raise HTTPException(
            422,
            detail={
                "code": "reserved_mapping_keys",
                "message": f"{field_name} 包含平台保留键，客户端不得写入",
                "keys": reserved,
            },
        )


def _validate_user_field_mapping(
    value: dict,
    ignored_fields: list[str],
) -> None:
    """字段必须一一对应；忽略列通过独立显式契约表达。"""
    ignored = [
        str(item).strip()
        for item in ignored_fields
        if str(item).strip()
    ]
    if len(ignored) != len(set(ignored)):
        raise HTTPException(
            422,
            detail={
                "code": "duplicate_ignored_fields",
                "message": "ignored_fields 包含重复字段",
            },
        )
    overlap = sorted(set(value) & set(ignored))
    if overlap:
        raise HTTPException(
            422,
            detail={
                "code": "mapped_and_ignored_fields",
                "message": "同一源字段不能同时映射和忽略",
                "fields": overlap,
            },
        )
    targets = [
        str(target).strip()
        for target in value.values()
        if str(target).strip()
    ]
    duplicates = sorted(
        {target for target in targets if targets.count(target) > 1}
    )
    if duplicates:
        raise HTTPException(
            422,
            detail={
                "code": "duplicate_mapping_targets",
                "message": "字段映射必须一一对应，多个源字段不能写入同一目标属性",
                "targets": duplicates,
            },
        )


def _assert_ignored_fields_do_not_hide_identity(
    ignored_fields: list[str],
    declared_primary_key: str,
) -> None:
    from app.data_channel.datasets.lake_gate import split_pk

    hidden_identity = sorted(
        set(ignored_fields) & set(split_pk(declared_primary_key))
    )
    if hidden_identity:
        raise HTTPException(
            422,
            detail={
                "code": "primary_key_cannot_be_ignored",
                "message": "资产主键是实例身份契约，不能在本体映射中忽略",
                "fields": hidden_identity,
            },
        )


def _canonical_primary_key(db: Session, dataset_id: str) -> str:
    """Return the asset-lake identity contract for a mapping source.

    A mapping is a consumer of a Dataset, not an independent schema authority.
    Keeping this lookup at the API boundary prevents callers from changing object
    identity while the lake, review diff and merge paths still use another key.
    """
    from app.data_channel.datasets.lake_gate import split_pk
    from app.data_channel.datasets.models import Dataset

    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if dataset is None:
        raise HTTPException(
            404,
            detail={
                "code": "mapping_dataset_not_found",
                "message": f"映射数据集不存在或尚未迁入资产湖：{dataset_id}",
            },
        )
    schema = dataset.schema_json if isinstance(dataset.schema_json, dict) else {}
    columns = split_pk(schema.get("primary_key"))
    if not columns:
        raise HTTPException(
            400,
            detail={
                "code": "primary_key_required",
                "message": (
                    f"数据集「{dataset.name}」尚未声明主键契约，无法创建本体映射。"
                    "请先在数据资产湖维护主键。"
                ),
            },
        )
    # 统一拦截所有主键写入方（连接内省已在同步侧拒绝，人工声明入口在此兜底）：
    # 冒号与单列实例身份 f"{col}:{value}" 的分隔符产生拼接歧义，可构造跨数据集
    # 实例碰撞（对抗审查实证），必须在消费边界 fail-closed
    colon_columns = [c for c in columns if ":" in c]
    if colon_columns:
        raise HTTPException(
            400,
            detail={
                "code": "invalid_primary_key_contract",
                "message": (
                    f"数据集「{dataset.name}」的主键列 {colon_columns} 含冒号："
                    "冒号与实例身份编码冲突，请改用无冒号列或经流水线派生稳定键。"
                ),
            },
        )
    if len(columns) != len(set(columns)):
        raise HTTPException(
            400,
            detail={
                "code": "invalid_primary_key_contract",
                "message": (
                    f"数据集「{dataset.name}」的复合主键包含重复列，请先修复资产契约。"
                ),
            },
        )
    return ",".join(columns)


def _assert_client_primary_key_matches(
    supplied: str | None,
    declared: str,
    dataset_id: str,
) -> None:
    """Accept the legacy request field only as an assertion, never an override."""
    if supplied is None:
        return
    from app.data_channel.datasets.lake_gate import split_pk

    normalized = ",".join(split_pk(supplied))
    if normalized != declared:
        raise HTTPException(
            400,
            detail={
                "code": "primary_key_contract_mismatch",
                "message": (
                    "映射主键必须与资产湖已声明主键完全一致，客户端不能覆盖数据身份契约。"
                ),
                "dataset_id": dataset_id,
                "declared_primary_key": declared,
                "supplied_primary_key": normalized,
            },
        )
