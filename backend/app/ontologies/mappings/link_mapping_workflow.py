"""Application workflows for relationship/link mappings."""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session


Rule = Callable[..., Any]


def _projection_locked_writer(func):
    @wraps(func)
    def wrapped(db: Session, ontology_id: str, *args, **kwargs):
        from app.ontologies.runtime_fence import _ontology_build_lock

        with _ontology_build_lock(db, ontology_id):
            return func(db, ontology_id, *args, **kwargs)

    return wrapped


@dataclass(frozen=True)
class LinkMappingRules:
    require_draft_ontology: Rule
    lock_ontology: Rule
    reject_reserved_mapping_keys: Rule
    validate_link_version_automation_policy: Rule
    canonical_primary_key: Rule
    assert_link_mapping_types_compatible: Rule


def create_link_mapping(
    db: Session,
    ontology_id: str,
    body: Any,
    *,
    rules: LinkMappingRules,
    user: Any = None,
) -> dict:
    from app.data_channel.datasets.lake_gate import split_pk
    from app.models.ontology_formal import LinkType
    from app.ontologies.mappings.models import OntologyLinkMapping, OntologyMapping
    from app.data_channel.datasets.service import DatasetService
    from app.ontologies.mappings.remote_binding import (
        create_remote_link_definition,
        normalize_binding_mode,
        reject_local_link_on_remote_dataset,
    )

    rules.require_draft_ontology(db, ontology_id)
    rules.reject_reserved_mapping_keys(body.field_mapping, "field_mapping")
    if normalize_binding_mode(getattr(body, "binding_mode", None)) == "remote":
        # 远程关系定义：仅验证冻结契约（两端远程绑定 + 端点键），不物化。
        return create_remote_link_definition(db, ontology_id, body)
    dataset_ids = {
        body.src_dataset_id, body.tgt_dataset_id, body.edge_dataset_id,
    }
    reject_local_link_on_remote_dataset(db, ontology_id, dataset_ids)
    if body.auto_apply_on_version:
        rules.validate_link_version_automation_policy(db, dataset_ids)
    link_type = None

    src_pk = split_pk(
        rules.canonical_primary_key(db, body.src_dataset_id)
    )
    tgt_pk = split_pk(
        rules.canonical_primary_key(db, body.tgt_dataset_id)
    )
    if len(src_pk) != 1 or len(tgt_pk) != 1:
        raise HTTPException(
            400,
            detail={
                "code": "composite_endpoint_fk_not_supported",
                "message": (
                    "关系连接表当前只支持单列端点主键；复合主键必须先提供多列外键映射，"
                    "平台不会把逗号拼接列名当成真实字段后静默生成 0 条关系。"
                ),
                "source_primary_key": src_pk,
                "target_primary_key": tgt_pk,
            },
        )
    if body.link_type_id:
        link_type = (
            db.query(LinkType)
            .filter(
                LinkType.id == body.link_type_id,
                LinkType.ontology_id == ontology_id,
            )
            .first()
        )
        if link_type is None:
            raise HTTPException(422, "绑定的 LinkType 不存在")
        # 一个 ObjectType 可以有多条候选数据映射。端点校验必须查询“对象 +
        # 本次所选数据集”的精确组合；先按对象 ``first()`` 再比较数据集会因
        # 查询顺序误拒绝同一对象的其他合法映射。
        src_mapping = (
            db.query(OntologyMapping)
            .filter(
                OntologyMapping.ontology_id == ontology_id,
                OntologyMapping.target_object_type_id
                == link_type.source_object_type_id,
                OntologyMapping.curated_dataset_id == body.src_dataset_id,
            )
            .first()
        )
        tgt_mapping = (
            db.query(OntologyMapping)
            .filter(
                OntologyMapping.ontology_id == ontology_id,
                OntologyMapping.target_object_type_id
                == link_type.target_object_type_id,
                OntologyMapping.curated_dataset_id == body.tgt_dataset_id,
            )
            .first()
        )
        if src_mapping is None or tgt_mapping is None:
            src_candidates = [
                row[0]
                for row in db.query(
                    OntologyMapping.curated_dataset_id,
                )
                .filter(
                    OntologyMapping.ontology_id == ontology_id,
                    OntologyMapping.target_object_type_id
                    == link_type.source_object_type_id,
                )
                .all()
            ]
            tgt_candidates = [
                row[0]
                for row in db.query(
                    OntologyMapping.curated_dataset_id,
                )
                .filter(
                    OntologyMapping.ontology_id == ontology_id,
                    OntologyMapping.target_object_type_id
                    == link_type.target_object_type_id,
                )
                .all()
            ]
            if not src_candidates or not tgt_candidates:
                raise HTTPException(
                    409,
                    detail={
                        "code": "link_endpoint_mapping_required",
                        "message": (
                            "请先分别为该关系的源对象和目标对象建立显式数据映射。"
                        ),
                    },
                )
            raise HTTPException(
                409,
                detail={
                    "code": "link_endpoint_dataset_mismatch",
                    "message": (
                        "关系端点数据集必须与所选 LinkType 两端对象的映射完全一致。"
                    ),
                    "expected_src_dataset_ids": src_candidates,
                    "expected_tgt_dataset_ids": tgt_candidates,
                },
            )

    rules.assert_link_mapping_types_compatible(
        db,
        src_dataset_id=body.src_dataset_id,
        tgt_dataset_id=body.tgt_dataset_id,
        edge_dataset_id=body.edge_dataset_id,
        src_key=body.src_key,
        tgt_key=body.tgt_key,
        link_type=link_type,
        field_mapping=body.field_mapping or {},
    )

    existing_query = db.query(OntologyLinkMapping).filter(
        OntologyLinkMapping.ontology_id == ontology_id
    )
    if body.link_type_id:
        existing_query = existing_query.filter(
            OntologyLinkMapping.link_type_id == body.link_type_id
        )
    else:
        existing_query = existing_query.filter(
            OntologyLinkMapping.link_type_id.is_(None),
            OntologyLinkMapping.src_dataset_id == body.src_dataset_id,
            OntologyLinkMapping.tgt_dataset_id == body.tgt_dataset_id,
            OntologyLinkMapping.relation_type == body.relation_type,
        )
    existing_link = existing_query.first()
    if existing_link is not None:
        same_definition = (
            existing_link.src_dataset_id == body.src_dataset_id
            and existing_link.tgt_dataset_id == body.tgt_dataset_id
            and existing_link.edge_dataset_id == body.edge_dataset_id
            and existing_link.relation_type == body.relation_type
            and existing_link.src_key == body.src_key
            and existing_link.tgt_key == body.tgt_key
            and {
                key: value
                for key, value in dict(
                    existing_link.field_mapping or {}
                ).items()
                if not str(key).startswith("__")
            }
            == dict(body.field_mapping or {})
            and bool(
                (existing_link.field_mapping or {}).get(
                    "__auto_apply_on_version__"
                )
            )
            == bool(body.auto_apply_on_version)
        )
        if same_definition:
            return {
                "link_mapping_id": existing_link.id,
                "relation_type": existing_link.relation_type,
                "edge_dataset_id": existing_link.edge_dataset_id,
                "edge_properties": [
                    key
                    for key in (existing_link.field_mapping or {})
                    if not str(key).startswith("__")
                ],
                "idempotent_replay": True,
            }
        raise HTTPException(
            409,
            detail={
                "code": "link_mapping_already_exists",
                "message": (
                    "该 LinkType 已存在关系映射；请维护现有映射，不要重复创建。"
                ),
                "link_mapping_id": existing_link.id,
            },
        )

    service = DatasetService(db)
    try:
        src_rows = service.preview(
            body.src_dataset_id,
            None,
            limit=10000,
        )
        tgt_rows = service.preview(
            body.tgt_dataset_id,
            None,
            limit=10000,
        )
    except Exception as exc:
        raise HTTPException(
            400,
            f"Failed to preview datasets for link mapping: {exc}",
        )
    if not src_rows:
        raise HTTPException(400, "Source dataset has no rows")
    if not tgt_rows:
        raise HTTPException(400, "Target dataset has no rows")

    if body.edge_dataset_id:
        # —— 连接表胖关系：src_key/tgt_key 及 field_mapping 的列都在连接表内 ——
        try:
            edge_rows = service.preview(
                body.edge_dataset_id,
                None,
                limit=10000,
            )
        except Exception as exc:
            raise HTTPException(
                400,
                f"Failed to preview edge dataset for link mapping: {exc}",
            )
        if not edge_rows:
            raise HTTPException(
                400,
                "Edge (junction) dataset has no rows",
            )
        edge_columns = set(edge_rows[0].keys())
        if body.src_key not in edge_columns:
            raise HTTPException(
                400,
                f"Source FK '{body.src_key}' not found in edge dataset",
            )
        if body.tgt_key not in edge_columns:
            raise HTTPException(
                400,
                f"Target FK '{body.tgt_key}' not found in edge dataset",
            )
        missing = [
            column
            for column in (body.field_mapping or {}).values()
            if column not in edge_columns
        ]
        if missing:
            raise HTTPException(
                400,
                (
                    "Edge property columns not found in edge dataset: "
                    f"{missing}"
                ),
            )
        # 两端外键只允许命中端点的 canonical 主键，不能跨所有列做模糊交集。
        src_all = {
            str(row.get(src_pk[0], "")).strip()
            for row in src_rows
        }
        tgt_all = {
            str(row.get(tgt_pk[0], "")).strip()
            for row in tgt_rows
        }
        match_count = sum(
            1
            for edge_row in edge_rows
            if str(edge_row.get(body.src_key, "")).strip() in src_all
            and str(edge_row.get(body.tgt_key, "")).strip() in tgt_all
        )
        if match_count == 0:
            raise HTTPException(
                400,
                "连接表两端外键未命中任何端点实体；请检查 FK 列选择",
            )
    else:
        # —— 直连外键瘦关系（原行为不变）——
        if body.src_key not in src_rows[0]:
            raise HTTPException(
                400,
                (
                    f"Source key '{body.src_key}' not found in source "
                    "dataset"
                ),
            )
        if body.tgt_key not in tgt_rows[0]:
            raise HTTPException(
                400,
                (
                    f"Target key '{body.tgt_key}' not found in target "
                    "dataset"
                ),
            )
        tgt_values = {
            str(row.get(body.tgt_key, "")).strip()
            for row in tgt_rows
            if row.get(body.tgt_key)
        }
        match_count = sum(
            1
            for row in src_rows
            if str(row.get(body.src_key, "")).strip() in tgt_values
        )
        if match_count == 0:
            raise HTTPException(
                400,
                (
                    "Link mapping produced 0 matches; choose different "
                    "source/target keys"
                ),
            )

    link_mapping = OntologyLinkMapping(
        ontology_id=ontology_id,
        src_dataset_id=body.src_dataset_id,
        tgt_dataset_id=body.tgt_dataset_id,
        relation_type=body.relation_type,
        src_key=body.src_key,
        tgt_key=body.tgt_key,
        link_type_id=body.link_type_id,
        edge_dataset_id=body.edge_dataset_id,
        field_mapping={
            **(body.field_mapping or {}),
            **(
                {"__auto_apply_on_version__": True}
                if body.auto_apply_on_version
                else {}
            ),
        },
        status="active",
    )
    db.add(link_mapping)
    db.commit()
    db.refresh(link_mapping)
    return {
        "link_mapping_id": link_mapping.id,
        "relation_type": link_mapping.relation_type,
        "match_count": match_count,
        "edge_dataset_id": link_mapping.edge_dataset_id,
        "edge_properties": list((body.field_mapping or {}).keys()),
    }


def update_link_mapping_automation(
    db: Session,
    ontology_id: str,
    link_mapping_id: str,
    body: Any,
    *,
    rules: LinkMappingRules,
) -> dict:
    """Update only the operational subscription; safe for published ontologies."""
    from app.ontologies.mappings.models import OntologyLinkMapping

    rules.lock_ontology(db, ontology_id)
    mapping = (
        db.query(OntologyLinkMapping)
        .filter(
            OntologyLinkMapping.id == link_mapping_id,
            OntologyLinkMapping.ontology_id == ontology_id,
        )
        .first()
    )
    if mapping is None:
        raise HTTPException(404, "Link mapping not found")
    from app.ontologies.mappings.remote_binding import (
        BINDING_MODE_REMOTE,
        link_mapping_binding_mode,
    )
    if link_mapping_binding_mode(mapping.field_mapping) == BINDING_MODE_REMOTE:
        raise HTTPException(
            422,
            detail={
                "code": "remote_auto_apply_forbidden",
                "message": "远程绑定不参与本地自动灌入，不能开启自动触发订阅",
            },
        )
    if body.auto_apply_on_version:
        rules.validate_link_version_automation_policy(
            db,
            {
                mapping.src_dataset_id,
                mapping.tgt_dataset_id,
                mapping.edge_dataset_id,
            },
        )
    field_mapping = dict(mapping.field_mapping or {})
    if body.auto_apply_on_version:
        field_mapping["__auto_apply_on_version__"] = True
    else:
        field_mapping.pop("__auto_apply_on_version__", None)
    mapping.field_mapping = field_mapping
    db.commit()
    return {
        "link_mapping_id": mapping.id,
        "auto_apply_on_version": body.auto_apply_on_version,
    }


@_projection_locked_writer
def delete_link_mapping(
    db: Session,
    ontology_id: str,
    link_mapping_id: str,
    *,
    rules: LinkMappingRules,
) -> None:
    """删除关系映射并撤销当前态边；Link Fact 历史以墓碑保留。"""
    from app.ontologies.mappings.models import OntologyLinkMapping
    from app.ontologies.mappings.mapping_service import (
        MappingApplyError,
        MappingService,
    )

    rules.require_draft_ontology(db, ontology_id)
    link_mapping = (
        db.query(OntologyLinkMapping)
        .filter(
            OntologyLinkMapping.id == link_mapping_id,
            OntologyLinkMapping.ontology_id == ontology_id,
        )
        .first()
    )
    if not link_mapping:
        raise HTTPException(404, "Link mapping not found")
    service = MappingService(db)
    try:
        service.remove_link_mapping_projection(link_mapping)
        db.delete(link_mapping)
        from app.ontologies.projection_state import mark_projecting
        mark_projecting(db, ontology_id)
        db.commit()
    except MappingApplyError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    from app.ontologies.projection_state import (
        ProjectionRebuildError,
        rebuild_after_commit,
    )
    try:
        rebuild_after_commit(db, ontology_id)
    except ProjectionRebuildError as exc:
        raise HTTPException(503, detail={
            "code": "ontology_projection_failed",
            "message": (
                "关系 Mapping 已从关系型真相删除，但 Neo4j 对账失败；"
                "图读取已阻断，请执行图修复"
            ),
            "ontology_id": ontology_id,
        }) from exc
    return None
