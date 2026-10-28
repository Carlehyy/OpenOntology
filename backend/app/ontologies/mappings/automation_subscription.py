"""映射自动化订阅总览与一键订阅（断点2：数据更新默认流入本体）。

两个自动接线开关（__auto_apply_on_version__ / __auto_apply_on_review__）
此前只能在创建/编辑映射时逐条开启。本模块提供：
- automation_overview：本体下全部对象映射的订阅状态与资格总览（发布前的
  「未订阅映射一览」）；
- subscribe_automation：一键订阅全部绑定映射（资格校验逐条执行，不满足
  资格的人工数据集跳过并返回原因，由发布门继续 fail-closed 兜底）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.ontologies.access import ontology_access_guard

logger = logging.getLogger(__name__)


# ── HTTP 子路由（由 app/main.py 组合） ──────────────────────────────
# 与 suggestion_router 同一模式：本体级访问守卫挂在本子路由上。
automation_router = APIRouter(dependencies=[Depends(ontology_access_guard)])


def _get_db():
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class SubscribeAutomationRequest(BaseModel):
    on_version: bool = False
    on_review: bool = False


@automation_router.get("/{ontology_id}/mappings/automation-overview")
def automation_overview_route(
    ontology_id: str,
    db: Session = Depends(_get_db),
):
    """发布前的「未订阅映射一览」：哪些映射会在数据更新后自动流入本体。"""
    return automation_overview(db, ontology_id)


@automation_router.post("/{ontology_id}/mappings/subscribe-automation")
def subscribe_automation_route(
    ontology_id: str,
    body: SubscribeAutomationRequest,
    db: Session = Depends(_get_db),
):
    """一键订阅：为绑定数据集的映射开启版本/审批自动接线（逐条资格校验）。"""
    return subscribe_automation(
        db,
        ontology_id,
        on_version=body.on_version,
        on_review=body.on_review,
    )


# ── 领域逻辑 ────────────────────────────────────────────────────────


def _dataset_view(db: Session, dataset_id: str | None) -> dict | None:
    if not dataset_id:
        return None
    from app.data_channel.datasets.models import Dataset

    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if dataset is None:
        return None
    return {
        "dataset_id": dataset.id,
        "dataset_name": dataset.name,
        "dataset_kind": dataset.kind,
    }


def _version_eligibility(db: Session, dataset_id: str) -> tuple[bool, str]:
    """人工数据集「版本后自动灌入」资格（curated 数据集走审批触发，不适用）。"""
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
        return False, "dataset_not_found"
    if dataset.kind == "curated":
        return False, "curated_dataset_uses_review_trigger"
    return manual_dataset_automation_eligibility(dataset, version)


def automation_overview(db: Session, ontology_id: str) -> dict:
    """未订阅/已订阅映射一览：发布前据此补订阅，保证数据更新自动流入本体。"""
    from app.ontologies.mappings.models import OntologyMapping
    from app.ontologies.mappings.remote_binding import (
        BINDING_MODE_REMOTE,
        mapping_binding_mode,
    )

    mappings = (
        db.query(OntologyMapping)
        .filter(OntologyMapping.ontology_id == ontology_id)
        .order_by(OntologyMapping.created_at, OntologyMapping.id)
        .all()
    )
    items: list[dict] = []
    unsubscribed = 0
    for mapping in mappings:
        field_mapping = mapping.field_mapping or {}
        dataset = _dataset_view(db, mapping.curated_dataset_id)
        binding_mode = mapping_binding_mode(field_mapping)
        remote_binding = binding_mode == BINDING_MODE_REMOTE
        eligible_version = False
        eligibility_reason: str | None = None
        if (dataset is not None and dataset["dataset_kind"] != "curated"
                and not remote_binding):
            eligible_version, eligibility_reason = _version_eligibility(
                db, mapping.curated_dataset_id)
        subscribed_version = bool(field_mapping.get("__auto_apply_on_version__"))
        subscribed_review = bool(field_mapping.get("__auto_apply_on_review__"))
        # curated 数据集：审批后自动灌入由 __auto_apply_on_review__ 控制；
        # 人工数据集：版本发布后自动灌入由 __auto_apply_on_version__ 控制；
        # 远程绑定：显式排除——不参与本地自动灌入，也无需订阅。
        needs_subscription = bool(dataset) and not remote_binding and not (
            subscribed_version or subscribed_review)
        if needs_subscription:
            unsubscribed += 1
        items.append({
            "mapping_id": mapping.id,
            "entity_class": mapping.entity_class,
            "status": mapping.status,
            "dataset": dataset,
            "binding_mode": binding_mode,
            "subscribed_version": subscribed_version,
            "subscribed_review": subscribed_review,
            "version_eligible": eligible_version,
            "version_eligibility_reason": (
                "remote_binding_not_automatable"
                if remote_binding else eligibility_reason),
            "needs_subscription": needs_subscription,
        })
    return {
        "ontology_id": ontology_id,
        "total": len(items),
        "unsubscribed": unsubscribed,
        "items": items,
    }


def subscribe_automation(
    db: Session,
    ontology_id: str,
    *,
    on_version: bool,
    on_review: bool,
) -> dict:
    """一键订阅：为全部绑定数据集的对象映射开启自动接线。

    逐条执行资格校验：人工数据集开启版本自动灌入必须先满足治理契约
    （主键 + 可校验不可变版本）；curated 数据集由审批触发（on_review）。
    不满足资格的映射跳过并在 skipped 中返回原因——发布门仍会 fail-closed。
    远程绑定显式排除：不参与本地自动灌入。
    """
    from app.ontologies.mappings.models import OntologyMapping
    from app.ontologies.mappings.remote_binding import (
        BINDING_MODE_REMOTE,
        mapping_binding_mode,
    )

    mappings = (
        db.query(OntologyMapping)
        .filter(OntologyMapping.ontology_id == ontology_id)
        .all()
    )
    subscribed: list[str] = []
    skipped: list[dict] = []
    for mapping in mappings:
        if not mapping.curated_dataset_id:
            skipped.append({
                "mapping_id": mapping.id,
                "entity_class": mapping.entity_class,
                "reason": "no_dataset_bound",
            })
            continue
        if mapping_binding_mode(mapping.field_mapping) == BINDING_MODE_REMOTE:
            skipped.append({
                "mapping_id": mapping.id,
                "entity_class": mapping.entity_class,
                "reason": "remote_binding_not_automatable",
            })
            continue
        field_mapping = dict(mapping.field_mapping or {})
        changed = False
        if on_version:
            from app.data_channel.datasets.models import Dataset

            dataset = db.query(Dataset).filter(
                Dataset.id == mapping.curated_dataset_id).first()
            if dataset is not None and dataset.kind != "curated":
                # 人工数据集：版本后自动灌入必须先满足治理契约
                eligible, reason = _version_eligibility(
                    db, mapping.curated_dataset_id)
                if not eligible:
                    skipped.append({
                        "mapping_id": mapping.id,
                        "entity_class": mapping.entity_class,
                        "reason": f"version_automation_not_eligible: {reason}",
                    })
                    continue
                if not field_mapping.get("__auto_apply_on_version__"):
                    field_mapping["__auto_apply_on_version__"] = True
                    changed = True
            # curated 数据集走审批触发（on_review），版本自动灌入不适用
        if on_review and not field_mapping.get("__auto_apply_on_review__"):
            field_mapping["__auto_apply_on_review__"] = True
            changed = True
        if changed:
            mapping.field_mapping = field_mapping
            subscribed.append(mapping.id)
        else:
            skipped.append({
                "mapping_id": mapping.id,
                "entity_class": mapping.entity_class,
                "reason": "already_subscribed",
            })
    db.flush()
    return {
        "ontology_id": ontology_id,
        "subscribed": subscribed,
        "skipped": skipped,
        "subscribed_count": len(subscribed),
    }
