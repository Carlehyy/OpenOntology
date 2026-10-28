"""v2 Ontology Mapping API — 含 Link Mapping 手动配置"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deps import get_current_user
from app.ontologies.access import ontology_access_guard
from app.ontologies.mappings import (
    entity_mapping_workflow as _entity_workflow,
)
from app.ontologies.mappings import link_mapping_workflow as _link_workflow
from app.ontologies.mappings import materialization_workflow as _materialization_workflow
from app.ontologies.mappings import query_service as _query_service
from app.ontologies.mappings.request_validation import (
    _assert_client_primary_key_matches,
    _assert_ignored_fields_do_not_hide_identity,
    _assert_link_mapping_types_compatible,
    _assert_mapping_types_compatible,
    _canonical_primary_key,
    _dataset_column_types,
    _lock_ontology,
    _mapping_types_compatible,
    _normal_mapping_type,
    _reject_reserved_mapping_keys,
    _require_draft_ontology,
    _validate_link_version_automation_policy,
    _validate_target_type,
    _validate_user_field_mapping,
    _validate_version_automation_policy,
)


router = APIRouter(dependencies=[Depends(ontology_access_guard)])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class SuggestRequest(BaseModel):
    dataset_name: str
    columns: list[str]
    sample_rows: list[dict] = []
    ontology_domain: str = ""


class CreateMappingRequest(BaseModel):
    curated_dataset_id: str
    entity_class: str
    field_mapping: dict
    ignored_fields: list[str] = Field(default_factory=list)
    primary_key_column: Optional[str] = None
    property_mappings: Optional[list[dict]] = None
    confidence: float = 1.0
    # 人工绑定：灌入到图谱里已有的对象实体（空=按名匹配，再无则由数据生成新类型）
    target_object_type_id: Optional[str] = None
    # 审核通过后自动灌入本体（存入 field_mapping.__auto_apply_on_review__）
    auto_apply_on_review: bool = False
    # 人工数据集发布通过契约校验的新版本后自动做本体全量对账。
    auto_apply_on_version: bool = False
    # local（缺省，本地湖版本物化）| remote（远程联邦绑定，仅元数据冻结，
    # 查询期由主协调解析；不可开启自动灌入订阅）。
    binding_mode: str = "local"


class UpdateMappingRequest(BaseModel):
    entity_class: Optional[str] = None
    field_mapping: Optional[dict] = None
    ignored_fields: Optional[list[str]] = None
    primary_key_column: Optional[str] = None
    target_object_type_id: Optional[str] = None
    auto_apply_on_review: Optional[bool] = None
    auto_apply_on_version: Optional[bool] = None
    # null=不变；切换 local/remote 属结构变更（需草稿态）。
    binding_mode: Optional[str] = None


class LinkMappingCreate(BaseModel):
    src_dataset_id: str
    tgt_dataset_id: str
    relation_type: str
    src_key: str
    tgt_key: str
    # —— 胖关系（连接表 + 边属性）——
    # link_type_id: 绑定手绘 LinkType，令边属性名对齐其 properties schema
    # （空则按 relation_type 名匹配/自建）
    link_type_id: str | None = None
    # edge_dataset_id: 连接表/关系数据集。有值 → 胖关系
    # （src_key/tgt_key/field_mapping 列均在连接表内）
    edge_dataset_id: str | None = None
    # field_mapping: {边属性名: 连接表列名}
    field_mapping: dict = {}
    auto_apply_on_version: bool = False
    # local（缺省）| remote（远程关系定义：两端均远程绑定，仅验证不物化，
    # 查询期做参数化 eq JOIN；不支持连接表/边属性/自动灌入）。
    binding_mode: str = "local"


class LinkMappingPolicyUpdate(BaseModel):
    auto_apply_on_version: bool


def _entity_mapping_rules() -> _entity_workflow.EntityMappingRules:
    """Resolve callbacks per request to preserve legacy monkeypatch seams."""
    return _entity_workflow.EntityMappingRules(
        require_draft_ontology=_require_draft_ontology,
        lock_ontology=_lock_ontology,
        validate_target_type=_validate_target_type,
        reject_reserved_mapping_keys=_reject_reserved_mapping_keys,
        canonical_primary_key=_canonical_primary_key,
        assert_client_primary_key_matches=_assert_client_primary_key_matches,
        validate_user_field_mapping=_validate_user_field_mapping,
        assert_ignored_fields_do_not_hide_identity=(
            _assert_ignored_fields_do_not_hide_identity
        ),
        assert_mapping_types_compatible=_assert_mapping_types_compatible,
        validate_version_automation_policy=(
            _validate_version_automation_policy
        ),
    )


def _link_mapping_rules() -> _link_workflow.LinkMappingRules:
    """Resolve callbacks per request to preserve legacy monkeypatch seams."""
    return _link_workflow.LinkMappingRules(
        require_draft_ontology=_require_draft_ontology,
        lock_ontology=_lock_ontology,
        reject_reserved_mapping_keys=_reject_reserved_mapping_keys,
        validate_link_version_automation_policy=(
            _validate_link_version_automation_policy
        ),
        canonical_primary_key=_canonical_primary_key,
        assert_link_mapping_types_compatible=(
            _assert_link_mapping_types_compatible
        ),
    )


@router.post("/{ontology_id}/mappings/suggest")
def suggest_mapping(
    ontology_id: str,
    body: SuggestRequest,
    db: Session = Depends(get_db),
):
    return _query_service.suggest_mapping(db, body)


@router.post("/{ontology_id}/mappings")
def create_mapping(
    ontology_id: str,
    body: CreateMappingRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _entity_workflow.create_mapping(
        db,
        ontology_id,
        body,
        rules=_entity_mapping_rules(),
        user=current_user,
    )


@router.put("/{ontology_id}/mappings/{mapping_id}")
def update_mapping(
    ontology_id: str,
    mapping_id: str,
    body: UpdateMappingRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """映射维护：结构和版本化自动触发策略均通过 draft 发布。"""
    return _entity_workflow.update_mapping(
        db,
        ontology_id,
        mapping_id,
        body,
        rules=_entity_mapping_rules(),
        user=current_user,
    )


@router.delete("/{ontology_id}/mappings/{mapping_id}", status_code=204)
def delete_mapping(
    ontology_id: str,
    mapping_id: str,
    db: Session = Depends(get_db),
):
    """删除映射并撤销其当前态投影；不可变事实历史通过墓碑保留。"""
    return _entity_workflow.delete_mapping(
        db,
        ontology_id,
        mapping_id,
        rules=_entity_mapping_rules(),
    )


@router.get("/{ontology_id}/mappings")
def list_mappings(
    ontology_id: str,
    db: Session = Depends(get_db),
):
    return _query_service.list_mappings(db, ontology_id)


@router.post("/{ontology_id}/mappings/{mapping_id}/apply")
def apply_mapping(
    ontology_id: str,
    mapping_id: str,
    data: list[dict],
    db: Session = Depends(get_db),
):
    """已禁用的原始数据旁路。

    正式 Apply 必须从映射绑定的、可追溯的 DatasetVersion 读取；否则请求方可
    用任意 JSON 冒充资产湖数据，审批、checksum 与版本血缘全部失效。
    """
    return _materialization_workflow.reject_raw_apply(
        db,
        ontology_id,
        mapping_id,
        data,
    )


@router.post("/{ontology_id}/mappings/{mapping_id}/apply-from-dataset")
def apply_mapping_from_dataset(
    ontology_id: str,
    mapping_id: str,
    db: Session = Depends(get_db),
):
    return _materialization_workflow.apply_mapping_from_dataset(
        db,
        ontology_id,
        mapping_id,
    )


@router.post("/{ontology_id}/mappings/build-all")
def build_all_mappings(
    ontology_id: str,
    db: Session = Depends(get_db),
):
    return _materialization_workflow.build_all_mappings(db, ontology_id)


@router.post("/{ontology_id}/link-mappings")
def create_link_mapping(
    ontology_id: str,
    body: LinkMappingCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _link_workflow.create_link_mapping(
        db,
        ontology_id,
        body,
        rules=_link_mapping_rules(),
        user=current_user,
    )


@router.get("/{ontology_id}/link-mappings")
def list_link_mappings(
    ontology_id: str,
    db: Session = Depends(get_db),
):
    return _query_service.list_link_mappings(db, ontology_id)


@router.put("/{ontology_id}/link-mappings/{link_mapping_id}/automation")
def update_link_mapping_automation(
    ontology_id: str,
    link_mapping_id: str,
    body: LinkMappingPolicyUpdate,
    db: Session = Depends(get_db),
):
    """Update only the operational subscription; safe for published ontologies."""
    return _link_workflow.update_link_mapping_automation(
        db,
        ontology_id,
        link_mapping_id,
        body,
        rules=_link_mapping_rules(),
    )


@router.delete(
    "/{ontology_id}/link-mappings/{link_mapping_id}",
    status_code=204,
)
def delete_link_mapping(
    ontology_id: str,
    link_mapping_id: str,
    db: Session = Depends(get_db),
):
    """删除关系映射并撤销当前态边；Link Fact 历史以墓碑保留。"""
    return _link_workflow.delete_link_mapping(
        db,
        ontology_id,
        link_mapping_id,
        rules=_link_mapping_rules(),
    )
