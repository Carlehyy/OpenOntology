"""确认协议 HTTP 端点（YIYA-36）— prepare / decision / execute / structure。

从 ``router.py`` 拆出以守住模块行数门禁；挂载在同一前缀
``/api/v2/formal/ontologies``（见 main.py）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.ontologies.access import require_ontology_access
from app.ontologies.agent_confirmation import schemas as CS
from app.ontologies.agent_confirmation import service as _confirmation_service
from app.ontologies.agent_runtime import schemas as S
from app.ontologies.agent_runtime import structure_service as _structure_service

router = APIRouter()


def _confirmation_error(exc) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail())


@router.post("/{ontology_id}/agent/proposals")
def prepare_proposal(ontology_id: str, body: CS.PrepareProposalRequest,
                     db: Session = Depends(get_db),
                     current_user=Depends(get_current_user)):
    """持久准备确认请求：必要校验→展开默认值→不可变载荷，无业务效果。"""
    require_ontology_access(db, ontology_id, current_user, write=True)
    try:
        confirmation, replayed = _confirmation_service.prepare(
            db, current_user, ontology_id, body)
    except _confirmation_service.ConfirmationError as exc:
        raise _confirmation_error(exc) from exc
    data = _confirmation_service.confirmation_out(db, confirmation,
                                                  replayed=replayed)
    # 201 首次 / 200 replay
    if replayed:
        return JSONResponse(status_code=200, content={"data": data})
    return JSONResponse(status_code=201, content={"data": data})


@router.get("/{ontology_id}/agent/proposals/{confirmation_id}")
def get_proposal(ontology_id: str, confirmation_id: str,
                 session_kind: str = Query(min_length=1, max_length=32),
                 session_id: str = Query(min_length=1, max_length=64),
                 db: Session = Depends(get_db),
                 current_user=Depends(get_current_user)):
    """读取本人确认请求的真实恢复态（unknown 不随 GET 自动执行）。"""
    try:
        confirmation = _confirmation_service.get_status(
            db, current_user, ontology_id, confirmation_id,
            session_kind, session_id)
    except _confirmation_service.ConfirmationError as exc:
        raise _confirmation_error(exc) from exc
    return _ok(_confirmation_service.confirmation_out(db, confirmation))


def _ok(data):
    return {"data": data}


@router.post("/{ontology_id}/agent/proposals/{confirmation_id}/decision")
def decide_proposal(ontology_id: str, confirmation_id: str,
                    body: CS.ConfirmationDecisionRequest,
                    db: Session = Depends(get_db),
                    current_user=Depends(get_current_user)):
    """一次用户二次确认：approve=同意提交（含入持久派发队列），非执行成功。"""
    require_ontology_access(db, ontology_id, current_user, write=True)
    try:
        confirmation = _confirmation_service.decide(
            db, current_user, ontology_id, confirmation_id, body.decision,
            body.decision_id, body.payload_hash, body.session_kind,
            body.session_id)
    except _confirmation_service.ConfirmationError as exc:
        raise _confirmation_error(exc) from exc
    return _ok(_confirmation_service.confirmation_out(db, confirmation))


@router.post("/{ontology_id}/agent/execute-proposal")
def execute_proposal(ontology_id: str,
                     body: CS.ExecuteConfirmationRequest | S.ExecuteProposalRequest,
                     db: Session = Depends(get_db),
                     current_user=Depends(get_current_user)):
    """用户在界面确认提案后的真实执行 — 只接受 confirmationId。

    旧形态（actionId/parameters 直发）返回 400 ``confirmation_required``：
    无持久确认请求的执行路径已关闭，不能绕过二次确认与全链幂等。
    """
    require_ontology_access(db, ontology_id, current_user, write=True)
    if not isinstance(body, CS.ExecuteConfirmationRequest):
        raise HTTPException(400, detail={
            "code": "confirmation_required",
            "message": "执行必须携带持久确认请求的 confirmationId；"
                       "请先 POST /agent/proposals 并完成用户确认",
            "reprepareRequired": True,
        })
    try:
        confirmation = _confirmation_service.execute(
            db, current_user, ontology_id, body.confirmation_id,
            body.session_kind, body.session_id)
    except _confirmation_service.ConfirmationError as exc:
        raise _confirmation_error(exc) from exc
    return _ok(_confirmation_service.confirmation_out(db, confirmation))


@router.get("/{ontology_id}/agent/structure")
def get_structure(ontology_id: str,
                  release_id: str = Query(min_length=1),
                  object_type_ids: list[str] | None = Query(default=None),
                  link_type_ids: list[str] | None = Query(default=None),
                  action_ids: list[str] | None = Query(default=None),
                  db: Session = Depends(get_db),
                  current_user=Depends(get_current_user)):
    """按需结构发现：只读当前问题所需的类型/关系/动作片段与来源版本证据。"""
    try:
        data = _structure_service.build_structure(
            db, current_user, ontology_id,
            expected_release_id=release_id,
            object_type_ids=object_type_ids,
            link_type_ids=link_type_ids,
            action_ids=action_ids)
    except _structure_service.StructureError as exc:
        raise HTTPException(status_code=exc.status,
                            detail=exc.detail()) from exc
    return _ok(data)
