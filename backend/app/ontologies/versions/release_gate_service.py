"""Fail-closed validation for ontology release activation.

The helpers in this module inspect SQL state and return deterministic errors.
They never flush, commit, or roll back; promotion and rollback retain complete
ownership of their surrounding transaction.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.curated.models import CuratedReview
from app.data_channel.datasets.automation_policy import (
    manual_dataset_automation_eligibility,
)
from app.data_channel.datasets.models import Dataset, DatasetVersion
from app.data_channel.datasets.service import version_has_content
from app.ontologies.formal_modeling.models import (
    ActionType as FoActionType,
    LinkInstance as FoLinkInstance,
    LinkType as FoLinkType,
    ObjectInstance as FoObjectInstance,
    ObjectType as FoObjectType,
    OntologyFunction as FoFunction,
)
from app.ontologies.formal_modeling.validation import validate_model
from app.ontologies.mappings.models import (
    OntologyLinkMapping,
    OntologyMapping,
)
from app.ontologies.sentinels import validation as sentinel_validation
from app.ontologies.sentinels.models import Sentinel
from app.ontologies.versions.evolution_service import (
    validate_builtin_sentinel_contract,
    validate_expression_function_contract,
)
from app.ontologies.versions.gate_contract import gate_error as _gate_error
from app.ontologies.versions.release_service import snapshot_release_sentinel


def raise_publish_errors(
    errors: list[dict],
    message: str = "本体发布门禁未通过",
) -> None:
    if errors:
        raise HTTPException(422, detail={
            "code": "publish_validation_failed",
            "message": f"{message}（{getattr(errors, 'total_count', len(errors))} 个错误）",
            "errors": errors,
        })


def validate_sentinels(
    sentinels: list[Sentinel],
    object_types: list[FoObjectType],
    link_types: list[FoLinkType],
    actions: list[FoActionType],
    *,
    validator: Callable[..., list[dict]] = (
        sentinel_validation.validate_sentinels
    ),
) -> list[dict]:
    return validator(
        sentinels,
        object_types,
        link_types,
        actions,
    )


def validate_production_mappings(
    db: Session,
    ontology_id: str,
    mappings: list[OntologyMapping],
    link_mappings: list[OntologyLinkMapping],
    instances: list[FoObjectInstance],
    object_types: list[FoObjectType],
    *,
    gate_error: Callable[..., dict] = _gate_error,
) -> list[dict]:
    from sqlalchemy.orm import Query
    from app.ontologies.versions.batching import query_batches

    errors: list[dict] = []
    instances_exist = (
        instances.with_entities(FoObjectInstance.id).first() is not None
        if isinstance(instances, Query) else bool(instances)
    )
    if instances_exist and not mappings:
        errors.append(gate_error(
            "production_mapping_required", "mapping",
            "生产本体已有实例，但没有任何 OntologyMapping；无法证明数据来源与审批版本"))

    object_by_id = {item.id: item for item in object_types}
    mapped_type_ids: set[str] = set()
    for mapping in mappings:
        if mapping.target_object_type_id:
            mapped_type_ids.add(mapping.target_object_type_id)
        for object_type in object_types:
            if mapping.entity_class in {
                object_type.name,
                object_type.display_name,
            }:
                mapped_type_ids.add(object_type.id)
    def iter_instances():
        if isinstance(instances, Query):
            for batch in query_batches(instances, FoObjectInstance.id):
                yield from batch
        else:
            yield from instances

    for instance in iter_instances():
        if instance.source != "pipeline" or not instance.external_id:
            errors.append(gate_error(
                "instance_lake_lineage_missing", "objectInstance",
                f"实例 {instance.id} 缺少 pipeline source/external_id，无法证明来自资产湖",
                item_id=instance.id or "", field="source"))
        if instance.object_type_id not in mapped_type_ids:
            object_type = object_by_id.get(instance.object_type_id)
            errors.append(gate_error(
                "instance_object_type_mapping_missing", "objectInstance",
                f"实例类型「{(object_type.display_name if object_type else instance.object_type_id)}」"
                "没有对应 OntologyMapping",
                item_id=instance.id or "", field="objectTypeId"))

    for mapping in mappings:
        mid = mapping.id or ""
        label = mapping.entity_class or mid
        if mapping.status != "applied":
            errors.append(gate_error(
                "mapping_not_applied", "mapping",
                f"Mapping「{label}」状态必须为 applied，当前为 {mapping.status}",
                item_id=mid, name=label, field="status"))
        dataset_id = mapping.curated_dataset_id
        dataset = (
            db.query(Dataset).filter(Dataset.id == dataset_id).first()
            if dataset_id
            else None
        )
        if dataset is None:
            errors.append(gate_error(
                "mapping_dataset_not_found", "mapping",
                f"Mapping「{label}」绑定的数据集不存在: {dataset_id or ''}",
                item_id=mid, name=label, field="curatedDatasetId"))
            continue
        latest = db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == dataset.id,
        ).order_by(DatasetVersion.version_no.desc()).first()
        if latest is None:
            errors.append(gate_error(
                "mapping_dataset_version_missing", "mapping",
                f"Mapping「{label}」绑定的数据集没有可发布版本",
                item_id=mid, name=label, field="curatedDatasetId"))
            continue
        if not version_has_content(latest) or not latest.checksum:
            errors.append(gate_error(
                "mapping_dataset_version_unverifiable", "mapping",
                f"Mapping「{label}」的数据版本缺少数据载荷/checksum",
                item_id=mid, name=label, field="curatedDatasetId"))
        if dataset.latest_version_id != latest.id:
            errors.append(gate_error(
                "dataset_latest_pointer_stale", "mapping",
                f"数据集「{dataset.name}」的 latest_version_id 未指向最新 v{latest.version_no}",
                item_id=mid, name=label, field="curatedDatasetId"))
        if dataset.kind == "curated":
            review = db.query(CuratedReview).filter(
                CuratedReview.curated_dataset_id == dataset.id,
                CuratedReview.dataset_version_id == latest.id,
            ).order_by(CuratedReview.created_at.desc()).first()
            if review is None or review.status != "approved":
                errors.append(gate_error(
                    "latest_dataset_version_not_approved", "mapping",
                    f"Mapping「{label}」的最新数据版本 v{latest.version_no} 未获得当前 approved 审批",
                    item_id=mid, name=label, field="curatedDatasetId"))
            if not (mapping.field_mapping or {}).get(
                "__auto_apply_on_review__"
            ):
                # 断点2：数据更新默认流入本体。审批通过后自动对账是发布契约的
                # 一部分；未订阅时发布门 fail-closed，提示一键订阅端点。
                errors.append(gate_error(
                    "mapping_curated_automation_not_subscribed", "mapping",
                    f"Mapping「{label}」消费 curated 数据，发布前必须开启“审批通过后自动灌入”（"
                    f"可用 /mappings/subscribe-automation 一键订阅）",
                    item_id=mid, name=label,
                    field="fieldMapping.__auto_apply_on_review__"))
        else:
            eligible, reason = manual_dataset_automation_eligibility(
                dataset,
                latest,
            )
            if not eligible:
                errors.append(gate_error(
                    "mapping_manual_dataset_not_governed", "mapping",
                    f"Mapping「{label}」的人工数据版本不满足治理契约：{reason}",
                    item_id=mid, name=label, field="curatedDatasetId"))
            if not (mapping.field_mapping or {}).get(
                "__auto_apply_on_version__"
            ):
                errors.append(gate_error(
                    "mapping_manual_automation_not_subscribed", "mapping",
                    f"Mapping「{label}」消费人工数据，发布前必须显式开启“版本后自动灌入”",
                    item_id=mid, name=label,
                    field="fieldMapping.__auto_apply_on_version__"))
        applied_version_id = (mapping.field_mapping or {}).get(
            "__applied_dataset_version_id__")
        if applied_version_id != latest.id:
            errors.append(gate_error(
                "mapping_applied_version_stale", "mapping",
                f"Mapping「{label}」尚未应用最新数据版本 v{latest.version_no}",
                item_id=mid, name=label,
                field="fieldMapping.__applied_dataset_version_id__"))

    for link in link_mappings:
        lid = link.id or ""
        label = link.relation_type or lid
        if link.status not in {"active", "inferred"}:
            errors.append(gate_error(
                "link_mapping_not_active", "linkMapping",
                f"LinkMapping「{label}」状态必须为 active/inferred，当前为 {link.status}",
                item_id=lid, name=label, field="status"))
        for role, dataset_id in (
            ("source", link.src_dataset_id),
            ("target", link.tgt_dataset_id),
            ("edge", link.edge_dataset_id),
        ):
            if not dataset_id:
                if role == "edge":
                    continue
                errors.append(gate_error(
                    "link_mapping_dataset_missing", "linkMapping",
                    f"LinkMapping「{label}」缺少 {role} 数据集",
                    item_id=lid, name=label, field=f"{role}DatasetId"))
                continue
            dataset = db.query(Dataset).filter(
                Dataset.id == dataset_id,
            ).first()
            if dataset is None:
                errors.append(gate_error(
                    "link_mapping_dataset_not_found", "linkMapping",
                    f"LinkMapping「{label}」的 {role} 数据集不存在: {dataset_id}",
                    item_id=lid, name=label, field=f"{role}DatasetId"))
                continue
            latest = db.query(DatasetVersion).filter(
                DatasetVersion.dataset_id == dataset_id,
            ).order_by(DatasetVersion.version_no.desc()).first()
            if latest is None:
                errors.append(gate_error(
                    "link_mapping_dataset_version_missing", "linkMapping",
                    f"LinkMapping「{label}」的 {role} 数据集没有版本",
                    item_id=lid, name=label, field=f"{role}DatasetId"))
                continue
            if not version_has_content(latest) or not latest.checksum:
                errors.append(gate_error(
                    "link_mapping_version_unverifiable", "linkMapping",
                    f"LinkMapping「{label}」的 {role} 版本缺少数据载荷/checksum",
                    item_id=lid, name=label, field=f"{role}DatasetId"))
            if dataset.kind == "curated":
                review = db.query(CuratedReview).filter(
                    CuratedReview.curated_dataset_id == dataset_id,
                    CuratedReview.dataset_version_id == latest.id,
                ).order_by(CuratedReview.created_at.desc()).first()
                if review is None or review.status != "approved":
                    errors.append(gate_error(
                        "link_mapping_version_not_approved", "linkMapping",
                        f"LinkMapping「{label}」的 {role} 最新版本 v{latest.version_no} 未审批",
                        item_id=lid, name=label,
                        field=f"{role}DatasetId"))
            else:
                eligible, reason = manual_dataset_automation_eligibility(
                    dataset,
                    latest,
                )
                if not eligible:
                    errors.append(gate_error(
                        "link_mapping_manual_dataset_not_governed",
                        "linkMapping",
                        f"LinkMapping「{label}」的 {role} 人工数据不满足治理契约：{reason}",
                        item_id=lid, name=label,
                        field=f"{role}DatasetId"))
                if not (link.field_mapping or {}).get(
                    "__auto_apply_on_version__"
                ):
                    errors.append(gate_error(
                        "link_mapping_manual_automation_not_subscribed",
                        "linkMapping",
                        f"LinkMapping「{label}」消费人工数据，发布前必须显式开启版本自动对账",
                        item_id=lid, name=label,
                        field="fieldMapping.__auto_apply_on_version__"))
            if dataset.latest_version_id != latest.id:
                errors.append(gate_error(
                    "link_mapping_latest_pointer_stale", "linkMapping",
                    f"LinkMapping「{label}」的 {role} 数据集 latest 指针过期",
                    item_id=lid, name=label, field=f"{role}DatasetId"))
            applied = (link.field_mapping or {}).get(
                f"__applied_{role}_version_id__")
            if applied != latest.id:
                errors.append(gate_error(
                    "link_mapping_applied_version_stale", "linkMapping",
                    f"LinkMapping「{label}」尚未应用 {role} 最新版本 v{latest.version_no}",
                    item_id=lid, name=label,
                    field=f"fieldMapping.__applied_{role}_version_id__"))
    return errors


def release_errors(
    db: Session,
    ontology_id: str,
    *,
    environment: str,
    action_definition_validator: Callable[..., list[str]],
    model_validator: Callable[..., list[dict]] = validate_model,
    expression_function_validator: Callable[..., list[dict]] = (
        validate_expression_function_contract
    ),
    builtin_sentinel_validator: Callable[..., list[dict]] = (
        validate_builtin_sentinel_contract
    ),
    sentinel_snapshotter: Callable[[Sentinel], dict] = (
        snapshot_release_sentinel
    ),
    sentinel_validator: Callable[..., list[dict]] = validate_sentinels,
    production_mapping_validator: Callable[..., list[dict]] = (
        validate_production_mappings
    ),
    gate_error: Callable[..., dict] = _gate_error,
) -> list[dict]:
    """Return the ordered, fail-closed release and rollback gate errors."""

    def query(model):
        return db.query(model).filter(
            model.ontology_id == ontology_id,
        ).all()

    object_types = query(FoObjectType)
    link_types = query(FoLinkType)
    actions = query(FoActionType)
    functions = query(FoFunction)
    from app.ontologies.versions.model_validator import validate_persisted_model

    instances = db.query(FoObjectInstance).filter(
        FoObjectInstance.ontology_id == ontology_id,
    )
    errors = validate_persisted_model(
        db, ontology_id, object_types, link_types, actions, functions,
        model_validator=model_validator,
    )
    errors.extend(expression_function_validator(
        functions,
        object_types,
    ))
    for action in actions:
        for message in action_definition_validator(
            action,
            object_types,
            link_types,
            functions,
        ):
            errors.append(gate_error(
                "invalid_action_definition", "action", message,
                item_id=action.id or "",
                name=action.display_name or action.name or action.id or "",
                field="rules"))
    if not object_types:
        errors.append(gate_error(
            "object_type_required", "ontology",
            "发布本体至少需要一个 ObjectType"))
    for function in functions:
        if (
            bool(function.enabled)
            and str(function.language or "").strip().lower() == "typescript"
        ):
            errors.append(gate_error(
                "enabled_typescript_function_forbidden", "function",
                f"启用的 TypeScript 函数「{function.display_name or function.name}」不能进入发布版本",
                item_id=function.id or "",
                name=function.display_name or function.name,
                field="language"))

    sentinels = db.query(Sentinel).filter(
        Sentinel.ontology_id == ontology_id,
        Sentinel.origin == "release_builtin",
    ).all()
    errors.extend(builtin_sentinel_validator(
        [sentinel_snapshotter(item) for item in sentinels],
    ))
    errors.extend(sentinel_validator(
        sentinels,
        object_types,
        link_types,
        actions,
    ))

    mappings = query(OntologyMapping)
    object_type_ids = {item.id for item in object_types}
    for mapping in mappings:
        if (
            mapping.target_object_type_id
            and mapping.target_object_type_id not in object_type_ids
        ):
            errors.append(gate_error(
                "mapping_object_type_not_found", "mapping",
                f"Mapping「{mapping.entity_class}」绑定的 ObjectType 不存在",
                item_id=mapping.id or "",
                name=mapping.entity_class,
                field="targetObjectTypeId"))
    if environment == "production":
        errors.extend(production_mapping_validator(
            db,
            ontology_id,
            mappings,
            query(OntologyLinkMapping),
            instances,
            object_types,
        ))
    return errors
