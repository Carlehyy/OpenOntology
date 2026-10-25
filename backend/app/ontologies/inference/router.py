"""
推理运行 + 影子试跑 + 动作发射路由
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, load_only
from sqlalchemy import desc
import uuid
import time
from app.deps import get_db, get_current_user
from app.ontologies.access import ontology_access_guard
from app.models.inference import (
    ShadowRun, InferenceRun, InferenceResult, ActionFiring, AuditLog,
)
from app.models.ontology import OntologyProject
from app.models.entity import Entity
from app.models.relation import Relation
from app.models.logic import LogicRule
from app.models.action import Action
from app.models.user import User

router = APIRouter(dependencies=[Depends(ontology_access_guard)])

# 与 workspace_service.PUBLISHED_DOCUMENT_SAVED_SUBTYPE 同值契约：该子类型
# 审计行的 before_state/after_state 保存业务文档全文（最大各约 700KB），
# 列表默认投影不加载、不返回这两个大 JSON 列。
_DOCUMENT_STATE_SUBTYPE = "published_document_saved"

# 审计列表默认投影：除 before_state/after_state 外的全部标量列 + meta。
_AUDIT_LISTING_LOADED = (
    AuditLog.id,
    AuditLog.ontology_id,
    AuditLog.event_type,
    AuditLog.event_subtype,
    AuditLog.user_id,
    AuditLog.user_name,
    AuditLog.description,
    AuditLog.object_type,
    AuditLog.object_id,
    AuditLog.meta,
    AuditLog.ip_address,
    AuditLog.user_agent,
    AuditLog.created_at,
)


class ShadowRunCreate(BaseModel):
    rule_id: str
    rule_name: str


class InferenceRunCreate(BaseModel):
    name: str
    description: str | None = None
    rule_ids: list[str] = Field(default_factory=list)


class ActionPayload(BaseModel):
    action_type: str  # notify, risk, event, webhook
    payload: dict = Field(default_factory=dict)


# ── 影子试跑 ─────────────────────────────────────────────────────────

@router.post("/{ontology_id}/shadow-runs", status_code=201)
def create_shadow_run(ontology_id: str, body: ShadowRunCreate, db: Session = Depends(get_db),
                      current_user=Depends(get_current_user)):
    """创建影子试跑任务"""
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise HTTPException(404, "Ontology not found")

    sr = ShadowRun(
        id=str(uuid.uuid4()),
        ontology_id=ontology_id,
        rule_id=body.rule_id,
        rule_name=body.rule_name,
        status="running",
        created_by=current_user.id,
    )
    db.add(sr)
    db.commit()

    # 执行试跑（同步）
    _execute_shadow_run(db, sr, ontology_id)

    return {"data": {
        "id": sr.id,
        "status": sr.status,
        "total_entities_checked": sr.total_entities_checked,
        "entities_matched": sr.entities_matched,
        "quality_score": sr.quality_score,
        "verdict": sr.verdict,
        "match_samples": sr.match_samples or [],
    }}


def _execute_shadow_run(db: Session, sr: ShadowRun, ontology_id: str):
    """执行影子试跑逻辑"""
    entities = db.query(Entity).filter(Entity.ontology_id == ontology_id).all()
    rule = db.query(LogicRule).filter(LogicRule.id == sr.rule_id).first()

    total = len(entities)
    matched = 0
    samples = []
    quality_score = 0.0

    if rule and entities:
        # 简单的规则模拟：formula 中包含的关键词匹配
        formula = (rule.formula or "").lower()

        for e in entities:
            e_text = f"{e.name_cn or ''} {e.name_en or ''} {e.description or ''} {str(e.properties or {})}".lower()
            # 检查 formula 中的关键词是否在实体中
            keywords = [k.strip() for k in formula.replace("and", ",").replace("or", ",").replace("(", "").replace(")", "").split(",") if k.strip()]
            match_count = sum(1 for kw in keywords if kw in e_text)

            if keywords and match_count >= max(1, len(keywords) // 2):
                matched += 1
                if len(samples) < 20:
                    samples.append({
                        "entity_id": e.id,
                        "entity_name": e.name_cn or e.name_en or e.id,
                        "match_keywords": [kw for kw in keywords if kw in e_text][:5],
                        "confidence": round(match_count / len(keywords), 2),
                    })

        quality_score = round(matched / total, 4) if total else 0.0

    sr.total_entities_checked = total
    sr.entities_matched = matched
    sr.match_samples = samples
    sr.quality_score = quality_score
    sr.status = "completed"
    from datetime import datetime, timezone
    sr.completed_at = datetime.now(timezone.utc)

    # 判定
    if quality_score > 0.5:
        sr.verdict = "pass"
        sr.verdict_reason = f"命中率高 ({quality_score:.1%})，建议上线"
    elif quality_score > 0.1:
        sr.verdict = "pass"
        sr.verdict_reason = f"命中率 {quality_score:.1%}，可上线但需关注"
    else:
        sr.verdict = "fail"
        sr.verdict_reason = f"命中率过低 ({quality_score:.1%})，建议调整规则"

    from datetime import datetime, timezone
    sr.completed_at = datetime.now(timezone.utc)
    db.commit()


@router.get("/{ontology_id}/shadow-runs")
def list_shadow_runs(ontology_id: str, db: Session = Depends(get_db)):
    items = db.query(ShadowRun).filter(ShadowRun.ontology_id == ontology_id).order_by(desc(ShadowRun.created_at)).all()
    return {"data": [{
        "id": r.id, "rule_id": r.rule_id, "rule_name": r.rule_name,
        "status": r.status, "total_entities_checked": r.total_entities_checked,
        "entities_matched": r.entities_matched, "quality_score": r.quality_score,
        "verdict": r.verdict, "verdict_reason": r.verdict_reason,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in items]}


# ── 推理运行 ─────────────────────────────────────────────────────────

@router.post("/{ontology_id}/inference-runs", status_code=201)
def create_inference_run(ontology_id: str, body: InferenceRunCreate, db: Session = Depends(get_db),
                         current_user=Depends(get_current_user)):
    """创建推理运行"""
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise HTTPException(404, "Ontology not found")

    run = InferenceRun(
        id=str(uuid.uuid4()),
        ontology_id=ontology_id,
        name=body.name,
        description=body.description,
        rule_ids=body.rule_ids,
        status="running",
        created_by=current_user.id,
    )
    db.add(run)
    db.commit()

    # 执行推理（同步）
    _execute_inference(db, run, ontology_id)

    return {"data": {
        "id": run.id,
        "name": run.name,
        "status": run.status,
        "total_checked": run.total_checked,
        "total_matched": run.total_matched,
        "total_actions_fired": run.total_actions_fired,
    }}


def _execute_inference(db: Session, run: InferenceRun, ontology_id: str):
    """执行推理逻辑 — 在图上做模式匹配"""
    entities = db.query(Entity).filter(Entity.ontology_id == ontology_id).all()
    relations = db.query(Relation).filter(Relation.ontology_id == ontology_id).all()

    # 获取规则
    rules = db.query(LogicRule).filter(
        LogicRule.ontology_id == ontology_id,
        LogicRule.enabled == True,
    ).all()
    if run.rule_ids:
        rules = [r for r in rules if r.id in run.rule_ids]

    # 获取动作
    actions = db.query(Action).filter(
        Action.ontology_id == ontology_id,
        Action.enabled == True,
    ).all()

    total = len(entities)
    matched = 0
    fired = 0

    for rule in rules:
        formula = (rule.formula or "").lower()
        keywords = [k.strip() for k in formula.replace("and", ",").replace("or", ",").replace("(", "").replace(")", "").split(",") if k.strip()]

        for e in entities:
            e_text = f"{e.name_cn or ''} {e.name_en or ''} {e.type or ''} {e.description or ''}".lower()
            if not keywords:
                continue
            match_count = sum(1 for kw in keywords if kw in e_text)
            if match_count >= max(1, len(keywords) // 2):
                matched += 1
                confidence = round(match_count / len(keywords), 2)

                # 收集证据链
                evidence = []
                # 找与实体相关的关系
                related_rels = [r for r in relations if r.source_entity == e.id or r.target_entity == e.id]
                for rel in related_rels[:5]:
                    evidence.append({
                        "type": "relation",
                        "relation_type": rel.type,
                        "source": rel.source_entity,
                        "target": rel.target_entity,
                    })

                result = InferenceResult(
                    id=str(uuid.uuid4()),
                    run_id=run.id,
                    ontology_id=ontology_id,
                    rule_id=rule.id,
                    rule_name=rule.name_cn or rule.name_en or rule.id,
                    entity_id=e.id,
                    entity_name=e.name_cn or e.name_en or e.id,
                    evidence_chain=evidence,
                    match_detail={"keywords_matched": [kw for kw in keywords if kw in e_text], "match_ratio": confidence},
                    confidence=confidence,
                )
                db.add(result)

                # 触发动作
                for action in actions:
                    if action.linked_logic_ids and rule.id in action.linked_logic_ids:
                        payload = _build_action_payload(action, e, rule, evidence)
                        firing = ActionFiring(
                            id=str(uuid.uuid4()),
                            ontology_id=ontology_id,
                            inference_result_id=result.id,
                            action_id=action.id,
                            action_name=action.name_cn or action.name_en or action.id,
                            action_type=_detect_action_type(action),
                            payload=payload,
                            status="sent" if _detect_action_type(action) != "webhook" else "pending",
                        )
                        db.add(firing)
                        fired += 1

    run.total_checked = total
    run.total_matched = matched
    run.total_actions_fired = fired
    run.status = "completed"
    from datetime import datetime, timezone
    run.completed_at = datetime.now(timezone.utc)
    db.commit()


def _build_action_payload(action: Action, entity: Entity, rule: LogicRule, evidence: list) -> dict:
    """构建动作载荷"""
    return {
        "action_name": action.name_cn or action.name_en,
        "entity_id": entity.id,
        "entity_name": entity.name_cn or entity.name_en or entity.id,
        "entity_type": entity.type,
        "rule_id": rule.id,
        "rule_name": rule.name_cn or rule.name_en or rule.id,
        "evidence_count": len(evidence),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "message": f"规则「{rule.name_cn}」命中实体「{entity.name_cn or entity.id}」",
    }


def _detect_action_type(action: Action) -> str:
    """检测动作类型"""
    rule = (action.execution_rule or "").lower()
    if "webhook" in rule or "url" in rule:
        return "webhook"
    elif "risk" in rule or "风险" in rule:
        return "risk"
    elif "event" in rule or "事件" in rule:
        return "event"
    else:
        return "notify"


@router.get("/{ontology_id}/inference-runs")
def list_inference_runs(ontology_id: str, db: Session = Depends(get_db)):
    items = db.query(InferenceRun).filter(
        InferenceRun.ontology_id == ontology_id,
    ).order_by(desc(InferenceRun.created_at)).all()
    return {"data": [{
        "id": r.id, "name": r.name, "description": r.description,
        "status": r.status, "trigger_type": r.trigger_type,
        "total_checked": r.total_checked, "total_matched": r.total_matched,
        "total_actions_fired": r.total_actions_fired,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in items]}


@router.get("/{ontology_id}/inference-runs/{run_id}/results")
def get_inference_results(ontology_id: str, run_id: str, db: Session = Depends(get_db)):
    items = db.query(InferenceResult).filter(
        InferenceResult.run_id == run_id,
        InferenceResult.ontology_id == ontology_id,
    ).order_by(desc(InferenceResult.confidence)).all()
    return {"data": [{
        "id": r.id, "rule_id": r.rule_id, "rule_name": r.rule_name,
        "entity_id": r.entity_id, "entity_name": r.entity_name,
        "evidence_chain": r.evidence_chain or [],
        "match_detail": r.match_detail or {},
        "confidence": r.confidence,
        "processed": r.processed,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in items]}


# ── 动作发射 ─────────────────────────────────────────────────────────

@router.get("/{ontology_id}/action-firings")
def list_action_firings(ontology_id: str, status: str = None, db: Session = Depends(get_db)):
    q = db.query(ActionFiring).filter(ActionFiring.ontology_id == ontology_id)
    if status:
        q = q.filter(ActionFiring.status == status)
    items = q.order_by(desc(ActionFiring.created_at)).limit(200).all()
    return {"data": [{
        "id": f.id, "action_id": f.action_id, "action_name": f.action_name,
        "action_type": f.action_type, "payload": f.payload,
        "status": f.status, "response_info": f.response_info,
        "created_at": f.created_at.isoformat() if f.created_at else None,
    } for f in items]}


# ── 审计日志 ─────────────────────────────────────────────────────────

@router.get("/{ontology_id}/audit-logs")
def list_audit_logs(
    ontology_id: str,
    event_type: str = None,
    limit: int = 200,
    include_document_state: bool = False,
    db: Session = Depends(get_db),
):
    """审计日志列表。

    ``published_document_saved`` 行的 before_state/after_state 是业务文档
    全文（最大各约 700KB）：默认列表查询经 load_only 不取这两个 JSON 列、
    响应也不返回全文，文档行只透出 meta 摘要（字符数/指纹/修订号）；非
    文档行按 id 批量补齐两个 state 列，保持既有返回形状（避免 N+1）。
    显式 ``include_document_state=true`` 才返回文档行全文状态；权限沿用
    本端点的本体访问控制。
    """
    q = db.query(AuditLog).filter(AuditLog.ontology_id == ontology_id)
    if event_type:
        q = q.filter(AuditLog.event_type == event_type)
    items = (
        q.options(load_only(*_AUDIT_LISTING_LOADED))
        .order_by(desc(AuditLog.created_at))
        .limit(limit)
        .all()
    )
    document_row_ids = {
        log.id for log in items
        if log.event_subtype == _DOCUMENT_STATE_SUBTYPE
    }
    # 默认只为非文档行批量取 state 列；显式参数时才取文档行全文。
    state_ids = [
        log.id for log in items
        if include_document_state or log.id not in document_row_ids
    ]
    states: dict[str, tuple] = {}
    if state_ids:
        for log_id, before_state, after_state in db.query(
            AuditLog.id, AuditLog.before_state, AuditLog.after_state,
        ).filter(AuditLog.id.in_(state_ids)).all():
            states[log_id] = (before_state, after_state)

    def _state(log, index: int):
        if log.id in document_row_ids and not include_document_state:
            return None
        entry = states.get(log.id)
        return entry[index] if entry else None

    return {"data": [{
        "id": log.id, "event_type": log.event_type, "event_subtype": log.event_subtype,
        "user_name": log.user_name, "description": log.description,
        "object_type": log.object_type, "object_id": log.object_id,
        "before_state": _state(log, 0), "after_state": _state(log, 1),
        "document_state_omitted": (
            log.id in document_row_ids and not include_document_state),
        "meta": log.meta or {},
        "created_at": log.created_at.isoformat() if log.created_at else None,
    } for log in items]}


@router.post("/{ontology_id}/audit-logs", status_code=201)
def create_audit_log(ontology_id: str, body: dict, db: Session = Depends(get_db),
                     current_user=Depends(get_current_user)):
    """通用审计日志写入"""
    log = AuditLog(
        id=str(uuid.uuid4()),
        ontology_id=ontology_id,
        event_type=body.get("event_type", "custom"),
        event_subtype=body.get("event_subtype"),
        user_id=current_user.id,
        user_name=current_user.username,
        description=body.get("description", ""),
        object_type=body.get("object_type"),
        object_id=body.get("object_id"),
        before_state=body.get("before"),
        after_state=body.get("after"),
        meta=body.get("meta", {}),
    )
    db.add(log)
    db.commit()
    return {"data": {"id": log.id}}
