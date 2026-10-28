"""Read-side queries and response projection for ontology mappings."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


def suggest_mapping(db: Session, body: Any) -> dict:
    from app.ontologies.mappings.auto_mapper import AutoMapper

    mapper = AutoMapper(db)
    suggestion = mapper.suggest_field_mapping(
        body.dataset_name,
        body.columns,
        body.sample_rows,
        body.ontology_domain,
    )
    return {
        "entity_class": suggestion.entity_class,
        "entity_class_cn": suggestion.entity_class_cn,
        "description": suggestion.description,
        "primary_key_column": suggestion.primary_key_column,
        "field_mappings": [
            {
                "column_name": field_mapping.column_name,
                "property_name": field_mapping.property_name,
                "property_type": field_mapping.property_type,
                "confidence": field_mapping.confidence,
                "reason": field_mapping.reason,
            }
            for field_mapping in suggestion.field_mappings
        ],
    }


def list_mappings(db: Session, ontology_id: str) -> list[dict]:
    from app.models.ontology_formal import ObjectType
    from app.data_channel.curated.models import CuratedDataset
    from app.data_channel.datasets.models import Dataset, DatasetVersion
    from app.ontologies.mappings.mapping_service import MappingService

    service = MappingService(db)
    mappings = service.get_mappings(ontology_id)

    # 绑定/同名解析结果预载：让前端直接显示"灌到哪个对象实体"
    object_types_by_id = {
        object_type.id: object_type
        for object_type in db.query(ObjectType)
        .filter(ObjectType.ontology_id == ontology_id)
        .all()
    }
    object_types_by_name = {}
    for object_type in object_types_by_id.values():
        object_types_by_name.setdefault(object_type.name, object_type)
        if object_type.display_name:
            object_types_by_name.setdefault(
                object_type.display_name,
                object_type,
            )

    result = []
    for mapping in mappings:
        dataset_name = None
        dataset_kind = None
        dataset_source = None
        row_count = None
        if mapping.curated_dataset_id:
            dataset = (
                db.query(Dataset)
                .filter(Dataset.id == mapping.curated_dataset_id)
                .first()
            )
            if dataset:
                dataset_name = dataset.name
                dataset_kind = dataset.kind
                schema = (
                    dataset.schema_json
                    if isinstance(dataset.schema_json, dict)
                    else {}
                )
                dataset_source = schema.get("origin") or (
                    "sync" if dataset.source_connection_id else "manual"
                )
            else:
                curated_dataset = (
                    db.query(CuratedDataset)
                    .filter(CuratedDataset.id == mapping.curated_dataset_id)
                    .first()
                )
                if curated_dataset:
                    dataset_name = curated_dataset.name
            version = (
                db.query(DatasetVersion)
                .filter(DatasetVersion.dataset_id == mapping.curated_dataset_id)
                .order_by(DatasetVersion.version_no.desc())
                .first()
            )
            if version:
                row_count = version.rowcount

        # 远程联邦绑定（数据面绑定模式）：冻结资产描述符仅元信息。
        from app.ontologies.mappings.remote_binding import (
            BINDING_MODE_REMOTE,
            RemoteBindingError,
            mapping_binding_mode,
        )
        data_binding_mode = mapping_binding_mode(mapping.field_mapping)
        remote_binding = None
        if data_binding_mode == BINDING_MODE_REMOTE:
            try:
                from app.ontologies.mappings.remote_binding import (
                    remote_descriptor,
                )
                remote_binding = remote_descriptor(mapping.field_mapping)
            except RemoteBindingError as exc:
                remote_binding = {"error": exc.detail()}

        # 解析该映射会灌到哪个对象实体（显式绑定 → 按名匹配 → 数据新建）
        bound_object_type = (
            object_types_by_id.get(mapping.target_object_type_id)
            if mapping.target_object_type_id
            else None
        )
        matched_object_type = (
            bound_object_type
            or object_types_by_name.get(mapping.entity_class)
        )
        if bound_object_type is not None:
            binding_mode = "bound"  # 人工绑定
        elif matched_object_type is not None:
            binding_mode = "name_match"  # 自动按名复用
        else:
            binding_mode = "auto_create"  # 由数据生成新类型
        result.append(
            {
                "id": mapping.id,
                "curated_dataset_id": mapping.curated_dataset_id,
                "dataset_name": dataset_name,
                "row_count": row_count,
                "entity_class": mapping.entity_class,
                "field_mapping": mapping.field_mapping,
                "property_mappings": (mapping.field_mapping or {}).get(
                    "__properties__",
                    [],
                ),
                "status": mapping.status,
                "confidence": mapping.confidence,
                "created_at": (
                    mapping.created_at.isoformat()
                    if mapping.created_at
                    else None
                ),
                "target_object_type_id": mapping.target_object_type_id,
                "binding_mode": binding_mode,
                "data_binding_mode": data_binding_mode,
                "remote": remote_binding,
                "resolved_object_type": (
                    {
                        "id": matched_object_type.id,
                        "name": matched_object_type.name,
                        "display_name": matched_object_type.display_name,
                    }
                    if matched_object_type
                    else None
                ),
                "auto_apply_on_review": bool(
                    (mapping.field_mapping or {}).get(
                        "__auto_apply_on_review__"
                    )
                ),
                "auto_apply_on_version": bool(
                    (mapping.field_mapping or {}).get(
                        "__auto_apply_on_version__"
                    )
                ),
                "dataset_kind": dataset_kind,
                "dataset_source": dataset_source,
            }
        )
    return result


def list_link_mappings(db: Session, ontology_id: str) -> list[dict]:
    from app.ontologies.mappings.models import OntologyLinkMapping
    from app.ontologies.mappings.remote_binding import (
        link_mapping_binding_mode,
    )

    links = (
        db.query(OntologyLinkMapping)
        .filter(OntologyLinkMapping.ontology_id == ontology_id)
        .all()
    )
    return [
        {
            "id": link.id,
            "src_dataset_id": link.src_dataset_id,
            "tgt_dataset_id": link.tgt_dataset_id,
            "relation_type": link.relation_type,
            "src_key": link.src_key,
            "tgt_key": link.tgt_key,
            "status": link.status,
            "link_type_id": link.link_type_id,
            "edge_dataset_id": link.edge_dataset_id,
            "data_binding_mode": link_mapping_binding_mode(link.field_mapping),
            "field_mapping": {
                key: value
                for key, value in (link.field_mapping or {}).items()
                if not str(key).startswith("__")
            },
            "auto_apply_on_version": bool(
                (link.field_mapping or {}).get("__auto_apply_on_version__")
            ),
            "is_fat": bool(link.edge_dataset_id),
        }
        for link in links
    ]
