"""确认协议服务 — prepare 去重、一次确认、guard 保护与操作执行/恢复。

幂等设计（YIYA-39 v2 §4）：
- ``prepare_request_id`` 由可信来源派生（origin+owner/session+rootIntent+规范
  业务载荷 hash），执行器生成；相同人类意图下同载荷的重复调用/刷新/回放返回
  同一 confirmation，包括已成功/拒绝/过期。
- ``action_request_slot`` 由执行器按人类意图中的动作/目标槽分配；同槽异参数
  返回 409 prepare_payload_conflict，须新的受认证人类修改事件才能建立新槽。
- ``guard_hash``（actor+ontology+action+target+finalParameters，不含会话/意图）
  在活跃状态唯一：新会话试图确认相同在途载荷 → 409 operation_unresolved。
- 执行幂等键固定 ``proposal:{cid}``；审批技术执行沿用引擎既有
  ``approval-execution:{log_id}`` 键。engine_entered 标记先于引擎调用持久化；
  失锁/无日志不能推定未执行 → unknown，guard 不释放。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ontologies.agent_confirmation import models as M
from app.ontologies.agent_confirmation.schemas import (
    PrepareProposalRequest, ParameterRef,
)
from app.ontologies.agent_query.models import (
    AgentQueryRow, AgentQuerySnapshot,
)
from app.ontologies.release_context import current_release_context

CONFIRMATION_TTL_SECONDS = 24 * 3600
OPERATION_LEASE_SECONDS = 60
MAX_PARAMETERS = 64
MAX_PAYLOAD_BYTES = 64 * 1024

TERMINAL_STATES = {
    "succeeded", "failed_not_executed", "approval_rejected", "denied",
    "cancelled", "expired",
}


class ConfirmationError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def detail(self) -> dict:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        payload.update(self.extra)
        return payload


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def initial_engine_key(cid: str) -> str:
    return f"proposal:{cid}"


# --------------------------------------------------------------- prepare

def _resolve_http_root_intent(db: Session, user, body: PrepareProposalRequest):
    """HTTP 准备的来源绑定：clientSubmissionId 必须映射本人会话内的提案。"""
    from app.ontologies.agent_runtime.models import (
        AgentConversation, AgentMessage,
    )

    conversation = db.query(AgentConversation).filter(
        AgentConversation.id == body.session_id,
        AgentConversation.ontology_id.isnot(None),
    ).first()
    if (conversation is None
            or conversation.user_id != str(user.id)):
        raise ConfirmationError(404, "confirmation_not_found", "会话不存在")
    message = (db.query(AgentMessage)
               .filter(AgentMessage.conversation_id == conversation.id,
                       AgentMessage.role == "assistant")
               .order_by(AgentMessage.created_at.desc())
               .limit(200).all())
    for msg in message:
        # 查询按 role=assistant 过滤；root intent = 该提案之前的最近用户
        # 消息。裁决 3：拒绝缺失/assistant-only — 无可信人类事件不建立
        # 确认意图（无 assistant-ID/随机/空值兜底）。
        for proposal in (msg.proposals or []):
            if isinstance(proposal, dict) and proposal.get("proposalId") == \
                    body.client_submission_id:
                root = _last_user_intent(db, conversation.id,
                                         before=msg.created_at)
                if root is None:
                    raise ConfirmationError(
                        422, "invalid_parameter",
                        "会话内该提案之前没有可信的用户意图事件，"
                        "无法建立确认请求")
                return root
    # 允许直接以消息 id 提交（前端回放场景）
    direct = db.query(AgentMessage).filter(
        AgentMessage.id == body.client_submission_id,
        AgentMessage.conversation_id == conversation.id).first()
    if direct is not None:
        root = _last_user_intent(db, conversation.id,
                                 before=direct.created_at)
        if root is None:
            raise ConfirmationError(
                422, "invalid_parameter",
                "会话内该消息之前没有可信的用户意图事件，"
                "无法建立确认请求")
        return root
    raise ConfirmationError(
        422, "invalid_parameter",
        "clientSubmissionId 未映射到本人会话内的既有提案消息")


def _last_user_intent(db: Session, conversation_id: str, *, before):
    from app.ontologies.agent_runtime.models import AgentMessage

    row = (db.query(AgentMessage)
           .filter(AgentMessage.conversation_id == conversation_id,
                   AgentMessage.role == "user",
                   AgentMessage.created_at <= before)
           .order_by(AgentMessage.created_at.desc())
           .first())
    return row.id if row else None


def _resolve_parameter_refs(db: Session, user, refs: dict[str, ParameterRef],
                             parameters: dict[str, Any]) -> dict[str, Any]:
    """可信查询来源：从本人拥有的快照行解析参数，不信模型自述。"""
    sources: dict[str, Any] = {}
    for name, ref in refs.items():
        snapshot = db.query(AgentQuerySnapshot).filter(
            AgentQuerySnapshot.id == ref.query_id,
            AgentQuerySnapshot.owner_user_id == str(user.id)).first()
        if snapshot is None:
            raise ConfirmationError(422, "invalid_parameter",
                                    f"参数「{name}」引用的查询不存在")
        if _as_utc(snapshot.expires_at) < _now():
            raise ConfirmationError(
                422, "query_snapshot_expired",
                f"参数「{name}」引用的查询结果已过期，请重新查询后准备",
                reprepareRequired=True)
        row = db.query(AgentQueryRow).filter(
            AgentQueryRow.snapshot_id == snapshot.id,
            AgentQueryRow.ordinal == ref.row_ordinal).first()
        if row is None or ref.column not in (row.row or {}):
            raise ConfirmationError(422, "invalid_parameter",
                                    f"参数「{name}」引用的查询行列不存在")
        parameters[name] = row.row[ref.column]
        sources[name] = {
            "kind": "trusted_query",
            "queryId": ref.query_id,
            "rowOrdinal": ref.row_ordinal,
            "column": ref.column,
            "releases": snapshot.releases,
        }
    return sources


def prepare(db: Session, user, ontology_id: str, body: PrepareProposalRequest,
            *, origin: str = "user_direct", source_call_id: Optional[str] = None,
            root_intent_id: Optional[str] = None,
            action_request_slot: Optional[str] = None) -> tuple[M.ActionConfirmation, bool]:
    """持久准备：必要校验→展开默认值→不可变载荷。无业务效果。"""
    from app.ontologies.agent_runtime.boundary import ToolError, build_scope
    from app.ontologies.formal_modeling.action_validation import (
        prepare_action_parameters,
    )

    if len(body.parameters) > MAX_PARAMETERS:
        raise ConfirmationError(422, "invalid_parameter",
                                f"参数数量超过 {MAX_PARAMETERS} 上限")
    context = current_release_context(db, ontology_id,
                                      expected_release_id=body.release_id)
    project, profile, scope = build_scope(db, ontology_id,
                                           release_id=context.release.id)
    if not profile.enabled:
        raise ConfirmationError(403, "profile_disabled",
                                "该本体的智能体已停用")
    if not profile.allow_action_proposals:
        raise ConfirmationError(403, "action_proposals_forbidden",
                                "该本体的 profile 不允许提出动作提案")
    try:
        action = scope.require_action(body.action_id)
    except ToolError as exc:
        raise ConfirmationError(403, "action_forbidden", str(exc)) from exc
    target_snapshot_label = None
    if body.target_instance_id:
        from app.models.ontology_formal import ObjectInstance

        target = db.query(ObjectInstance).filter(
            ObjectInstance.id == body.target_instance_id).first()
        try:
            scope.visible_instance(target)
        except ToolError as exc:
            raise ConfirmationError(422, "invalid_target", str(exc)) from exc
        target_snapshot_label = getattr(target, "external_id", None) or target.id

    parameters = dict(body.parameters)
    parameter_sources = _resolve_parameter_refs(db, user, body.parameter_refs,
                                                parameters)
    final_params, validation_errors = prepare_action_parameters(
        action, parameters)
    if validation_errors:
        raise ConfirmationError(422, "invalid_parameter",
                                "参数校验失败", validationErrors=validation_errors)
    for key in parameters:
        parameter_sources.setdefault(key, {"kind": "user_input"})

    if root_intent_id is None:
        root_intent_id = _resolve_http_root_intent(db, user, body)
    if not root_intent_id:
        raise ConfirmationError(
            422, "invalid_parameter",
            "会话内没有可信的用户意图事件，无法建立确认请求")
    if action_request_slot is None:
        action_request_slot = _sha256(
            f"{root_intent_id}|{action.id}|{body.target_instance_id or ''}")
    call_ref = source_call_id or body.client_submission_id

    payload = {
        "actor": str(user.id),
        "sessionKind": body.session_kind,
        "sessionId": body.session_id,
        "rootIntentId": root_intent_id,
        "origin": origin,
        "ontology": ontology_id,
        "release": context.release.id,
        "action": action.id,
        "target": body.target_instance_id,
        "parameters": final_params,
        "requiresApproval": bool(action.requires_approval),
    }
    payload_hash = _sha256(_canonical_json(payload))
    if len(_canonical_json(payload).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ConfirmationError(422, "invalid_parameter",
                                f"载荷超过 {MAX_PAYLOAD_BYTES} 字节上限")
    prepare_request_id = _sha256(_canonical_json({
        "origin": origin, "actor": str(user.id),
        "sessionKind": body.session_kind, "sessionId": body.session_id,
        "rootIntentId": root_intent_id, "payloadHash": payload_hash,
    }))

    existing = db.query(M.ActionConfirmation).filter(
        M.ActionConfirmation.actor_user_id == str(user.id),
        M.ActionConfirmation.session_kind == body.session_kind,
        M.ActionConfirmation.session_id == body.session_id,
        M.ActionConfirmation.prepare_request_id == prepare_request_id,
    ).first()
    if existing is not None:
        if not hmac.compare_digest(existing.payload_hash, payload_hash):
            raise ConfirmationError(409, "prepare_payload_conflict",
                                    "同键载荷不一致")
        _expire_if_past_ttl(db, existing)
        return existing, True
    slotted = db.query(M.ActionConfirmation).filter(
        M.ActionConfirmation.actor_user_id == str(user.id),
        M.ActionConfirmation.session_kind == body.session_kind,
        M.ActionConfirmation.session_id == body.session_id,
        M.ActionConfirmation.root_intent_id == root_intent_id,
        M.ActionConfirmation.action_request_slot == action_request_slot,
    ).first()
    if slotted is not None:
        if not hmac.compare_digest(slotted.payload_hash, payload_hash):
            raise ConfirmationError(
                409, "prepare_payload_conflict",
                "同一人类意图下的动作槽已绑定不同载荷；修改目标/参数需要新的"
                "用户确认事件")
        _expire_if_past_ttl(db, slotted)
        return slotted, True

    display = _display_payload(project, context, action, final_params,
                               parameter_sources, body, target_snapshot_label)
    confirmation = M.ActionConfirmation(
        actor_user_id=str(user.id),
        session_kind=body.session_kind,
        session_id=body.session_id,
        root_intent_id=root_intent_id,
        action_request_slot=action_request_slot,
        prepare_request_id=prepare_request_id,
        origin=origin,
        source_call_id=call_ref,
        ontology_id=ontology_id,
        release_id=context.release.id,
        release_version=context.release.version_number,
        action_id=action.id,
        target_instance_id=body.target_instance_id,
        final_parameters=final_params,
        parameter_sources=parameter_sources,
        requires_approval=bool(action.requires_approval),
        payload_hash=payload_hash,
        display_payload=display,
        state="prepared",
        expires_at=_now() + timedelta(seconds=CONFIRMATION_TTL_SECONDS),
    )
    db.add(confirmation)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.query(M.ActionConfirmation).filter(
            M.ActionConfirmation.actor_user_id == str(user.id),
            M.ActionConfirmation.session_kind == body.session_kind,
            M.ActionConfirmation.session_id == body.session_id,
            M.ActionConfirmation.prepare_request_id == prepare_request_id,
        ).first()
        if existing is None:
            raise
        if not hmac.compare_digest(existing.payload_hash, payload_hash):
            raise ConfirmationError(409, "prepare_payload_conflict",
                                    "同键载荷不一致") from None
        return existing, True
    db.commit()
    return confirmation, False


def _display_payload(project, context, action, final_params, parameter_sources,
                     body: PrepareProposalRequest, target_label) -> dict:
    return {
        "ontology": {"id": project.id, "name": project.name},
        "action": {"id": action.id, "name": action.display_name or action.name},
        "target": ({"id": body.target_instance_id, "label": target_label}
                   if body.target_instance_id else None),
        "parameters": [
            {"name": key, "value": final_params[key],
             "source": parameter_sources.get(key, {}).get("kind", "default")}
            for key in sorted(final_params)
        ],
        "release": {"id": context.release.id,
                    "version": context.release.version_number},
        "requiresApproval": bool(action.requires_approval),
    }


# --------------------------------------------------------------- lookup

def _require_confirmation(db: Session, user, cid: str, session_kind: str,
                          session_id: str) -> M.ActionConfirmation:
    confirmation = db.query(M.ActionConfirmation).filter(
        M.ActionConfirmation.id == cid).first()
    if (confirmation is None
            or confirmation.actor_user_id != str(user.id)
            or confirmation.session_kind != session_kind
            or confirmation.session_id != session_id):
        # 跨主体/跨会话统一 404，不泄露 cid 是否存在
        raise ConfirmationError(404, "confirmation_not_found", "确认请求不存在")
    return confirmation


def _expire_if_past_ttl(db: Session, confirmation: M.ActionConfirmation) -> None:
    if confirmation.state == "prepared":
        expiry = _as_utc(confirmation.expires_at)
        if expiry is not None and expiry < _now():
            confirmation.state = "expired"
            confirmation.failure_code = "ttl_expired"
            db.commit()
            db.refresh(confirmation)


def get_status(db: Session, user, ontology_id: str, cid: str,
               session_kind: str, session_id: str) -> M.ActionConfirmation:
    confirmation = _require_confirmation(db, user, cid, session_kind,
                                          session_id)
    if confirmation.ontology_id != ontology_id:
        raise ConfirmationError(404, "confirmation_not_found", "确认请求不存在")
    _expire_if_past_ttl(db, confirmation)
    return confirmation


# --------------------------------------------------------------- decision

def decide(db: Session, user, ontology_id: str, cid: str, decision: str,
           decision_id: str, payload_hash: str, session_kind: str,
           session_id: str) -> M.ActionConfirmation:
    confirmation = get_status(db, user, ontology_id, cid, session_kind,
                              session_id)
    # 并发决策串行化（PG 真并发缺陷）：approve 与 deny 不得同时基于同一
    # prepared 快照提交 — 否则 deny 可覆盖已批准的 dispatch_queued 而在途
    # operation 仍然存在。get_status 是普通读；这里对确认行取 FOR UPDATE
    # 并重读最新已提交状态（populate_existing 绕过会话缓存），同
    # decision_id 的重放因此看到一致结果。SQLite 单写者语义下为 no-op。
    confirmation = (db.query(M.ActionConfirmation)
                    .filter(M.ActionConfirmation.id == cid)
                    .with_for_update()
                    .populate_existing()
                    .first())
    if (confirmation is None
            or confirmation.actor_user_id != str(user.id)
            or confirmation.session_kind != session_kind
            or confirmation.session_id != session_id):
        raise ConfirmationError(404, "confirmation_not_found",
                                "确认请求不存在")
    if confirmation.state == "expired":
        raise ConfirmationError(410, "confirmation_expired",
                                "确认请求已过期，需重新准备并再次确认",
                                currentState="expired", reprepareRequired=True)
    if confirmation.state == "prepared":
        if not hmac.compare_digest(confirmation.payload_hash, payload_hash):
            raise ConfirmationError(409, "prepare_payload_conflict",
                                    "载荷指纹不一致，拒绝替代确认")
    elif confirmation.decision_event_id == decision_id:
        if decision in ("approve",) and confirmation.state in (
                "confirmed", "dispatch_queued", "executing",
                "pending_approval", "succeeded", "unknown", "recovering",
                "compensation_required", "failed_not_executed"):
            return confirmation
        if decision == "deny" and confirmation.state == "denied":
            return confirmation
        if decision == "cancel" and confirmation.state == "cancelled":
            return confirmation
        raise ConfirmationError(409, "decision_conflict",
                                "确认请求状态不允许该决策")
    else:
        raise ConfirmationError(409, "decision_conflict",
                                f"确认请求已处于 {confirmation.state} 状态")

    if decision in ("deny", "cancel"):
        confirmation.state = "denied" if decision == "deny" else "cancelled"
        confirmation.decided_at = _now()
        confirmation.decision_event_id = decision_id
        confirmation.failure_code = None
        db.commit()
        db.refresh(confirmation)
        return confirmation

    # P2-5：assistant 业务写总闸（生产默认关闭；关闭时拒绝新的批准）
    from app.config import settings as _settings
    if decision == "approve" and not getattr(
            _settings, "assistant_action_execution_enabled", True):
        raise ConfirmationError(
            403, "assistant_writes_disabled",
            "assistant 业务写总闸已关闭；已有确认保留，待开启后重新批准",
            currentState=confirmation.state, retryable=True)

    # approve：重验 current release / 权限 / profile / 目标
    from app.ontologies.agent_runtime.boundary import ToolError, build_scope

    try:
        context = current_release_context(
            db, confirmation.ontology_id,
            expected_release_id=confirmation.release_id)
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", 409)
        detail = getattr(exc, "detail", None) or {}
        code = detail.get("code", "release_context_changed") if isinstance(
            detail, dict) else "release_context_changed"
        confirmation.state = "expired"
        confirmation.failure_code = code
        db.commit()
        raise ConfirmationError(
            status, code,
            "当前发布版本已变化，确认请求已过期；需重新准备并再次确认",
            currentState="expired", reprepareRequired=True) from exc
    project, profile, scope = build_scope(db, confirmation.ontology_id,
                                           release_id=context.release.id)
    if not profile.enabled or not profile.allow_action_proposals:
        confirmation.state = "expired"
        confirmation.failure_code = "profile_disabled"
        db.commit()
        raise ConfirmationError(403, "profile_disabled",
                                "该本体的智能体已停用或禁止提案",
                                currentState="expired", reprepareRequired=True)
    try:
        action = scope.require_action(confirmation.action_id)
    except ToolError as exc:
        confirmation.state = "expired"
        confirmation.failure_code = "action_forbidden"
        db.commit()
        raise ConfirmationError(403, "action_forbidden", str(exc),
                                currentState="expired",
                                reprepareRequired=True) from exc
    if confirmation.target_instance_id:
        from app.models.ontology_formal import ObjectInstance

        target = db.query(ObjectInstance).filter(
            ObjectInstance.id == confirmation.target_instance_id).first()
        try:
            scope.visible_instance(target)
        except ToolError as exc:
            confirmation.state = "failed_not_executed"
            confirmation.failure_code = "target_invalid"
            db.commit()
            raise ConfirmationError(422, "invalid_target", str(exc),
                                    currentState="failed_not_executed",
                                    reprepareRequired=True) from exc
    if bool(action.requires_approval) != confirmation.requires_approval:
        confirmation.state = "expired"
        confirmation.failure_code = "action_contract_changed"
        db.commit()
        raise ConfirmationError(409, "prepare_payload_conflict",
                                "动作审批契约已变化，需重新准备确认",
                                currentState="expired",
                                reprepareRequired=True)

    guard_hash = _guard_hash(confirmation)
    conflicting = db.query(M.ActionOperation).filter(
        M.ActionOperation.guard_hash == guard_hash,
        M.ActionOperation.state.in_(M.ACTIVE_OPERATION_STATES)).first()
    if conflicting is not None and conflicting.confirmation_id != cid:
        raise ConfirmationError(
            409, "operation_unresolved",
            "相同载荷存在未结案的在途操作；在其结案前拒绝重复确认",
            currentState=conflicting.state)

    confirmation.state = "dispatch_queued"
    confirmation.decided_at = _now()
    confirmation.decision_event_id = decision_id
    confirmation.row_version += 1
    operation = M.ActionOperation(
        confirmation_id=cid,
        initial_engine_key=initial_engine_key(cid),
        guard_hash=guard_hash,
        state="dispatch_queued",
    )
    confirmation.operation_id = None
    db.add(operation)
    try:
        db.flush()
        confirmation.operation_id = operation.id
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConfirmationError(409, "operation_unresolved",
                                "并发确认冲突；相同载荷的操作已在途") from exc
    db.refresh(confirmation)
    return confirmation


def _guard_hash(confirmation: M.ActionConfirmation) -> str:
    return _sha256(_canonical_json({
        "actor": confirmation.actor_user_id,
        "ontology": confirmation.ontology_id,
        "action": confirmation.action_id,
        "target": confirmation.target_instance_id,
        "parameters": confirmation.final_parameters,
    }))


# --------------------------------------------------------------- execute

def _revalidate_confirmation_access(db: Session, user,
                                    confirmation: M.ActionConfirmation,
                                    *, for_engine: bool) -> None:
    """裁决 1：统一权限重验（与 HTTP 入口共享 access.py 口径）。

    角色→本体写权限（admin 或 editor-owner）、release、profile.enabled/
    动作 allowlist。入引擎前明确撤权 → 调用方判 failed_not_executed；
    已入引擎/unknown 不释放 guard（由外部证据优先裁决）。
    """
    from app.ontologies.access import require_ontology_access
    from app.ontologies.agent_runtime.boundary import ToolError, build_scope

    try:
        require_ontology_access(db, confirmation.ontology_id, user,
                                write=True)
    except Exception as exc:  # noqa: BLE001 - HTTPException 透传
        status = getattr(exc, "status_code", 403)
        raise ConfirmationError(
            status, "access_revoked",
            "权限已变化（角色/本体写权限被撤销）",
            currentState=confirmation.state if not for_engine else None,
            engineEntryBlocked=for_engine) from exc
    context = current_release_context(
        db, confirmation.ontology_id,
        expected_release_id=confirmation.release_id)
    _, profile, scope = build_scope(db, confirmation.ontology_id,
                                    release_id=context.release.id)
    if not profile.enabled or not profile.allow_action_proposals:
        raise ConfirmationError(403, "profile_disabled",
                                "该本体的智能体已停用或禁止提案")
    try:
        scope.require_action(confirmation.action_id)
    except ToolError as exc:
        raise ConfirmationError(403, "action_forbidden", str(exc)) from exc


def execute(db: Session, user, ontology_id: str, cid: str, session_kind: str,
            session_id: str) -> M.ActionConfirmation:
    """唤醒已批准操作：只派发，绝不代 approve；终态重放不重新执行。"""
    confirmation = get_status(db, user, ontology_id, cid, session_kind,
                              session_id)
    if confirmation.state in ("prepared",):
        raise ConfirmationError(
            409, "confirmation_not_approved",
            "确认请求尚未获得用户批准", currentState=confirmation.state)
    if confirmation.state in ("denied", "cancelled", "expired"):
        return confirmation
    if confirmation.state == "dispatch_queued":
        # 裁决 1：入引擎前统一重验；撤权/收紧 → 判未执行失败（有证据
        # 未入引擎），零业务效果。
        try:
            _revalidate_confirmation_access(db, user, confirmation,
                                            for_engine=True)
        except ConfirmationError as exc:
            if exc.code in ("access_revoked", "profile_disabled",
                            "action_forbidden"):
                confirmation.state = "failed_not_executed"
                confirmation.failure_code = "access_revoked_before_engine"
                db.commit()
                db.refresh(confirmation)
                return confirmation
            raise
    operation = _operation_of(db, confirmation)
    if operation is None:
        return confirmation
    if operation.state == "dispatch_queued":
        run_operation_inline(operation.id, db)
        db.expire_all()
        return get_status(db, user, ontology_id, cid, session_kind,
                          session_id)
    if operation.state == "executing":
        reconcile_operation(operation.id, db)
        db.expire_all()
        return get_status(db, user, ontology_id, cid, session_kind,
                          session_id)
    return confirmation


def _operation_of(db: Session, confirmation: M.ActionConfirmation):
    if confirmation.operation_id:
        return db.query(M.ActionOperation).filter(
            M.ActionOperation.id == confirmation.operation_id).first()
    return db.query(M.ActionOperation).filter(
        M.ActionOperation.confirmation_id == confirmation.id).first()


def _fresh_session() -> Session:
    from app.database import SessionLocal

    return SessionLocal()


def run_operation_inline(operation_id: str, db: Session | None = None) -> None:
    """claim → engine_entered 标记 → 引擎执行 → 状态映射（跨会话窗口持久化）。

    ``db`` 为空时（worker）各阶段用独立短会话，崩溃窗口各自持久化；
    HTTP 内联执行传入请求会话，同一事务边界内完成。
    """
    owned = db is None
    session = db if db is not None else _fresh_session()
    try:
        op = _claim(operation_id, session)
        if op is None:
            return
        _mark_engine_entered(op.id, session)
        result = _invoke_engine(op.id, session)
        if result is None:
            return  # 撤权路径已就地终态化；无引擎结果不作失败裁决
        _apply_engine_result(op.id, result, session)
    finally:
        if owned:
            session.close()


def _claim(operation_id: str, db: Session):
    from app.config import settings as _settings
    if not getattr(_settings, "assistant_action_execution_enabled", True):
        return None  # 总闸关闭：停止领取，操作保持 dispatch_queued
    operation = (db.query(M.ActionOperation)
                 .filter(M.ActionOperation.id == operation_id)
                 .with_for_update().first())
    if operation is None or operation.state != "dispatch_queued":
        db.rollback()
        return None
    operation.state = "executing"
    operation.claimed_at = _now()
    operation.lease_expires_at = _now() + timedelta(
        seconds=OPERATION_LEASE_SECONDS)
    operation.worker_ref = f"inline:{uuid_lib.uuid4().hex[:12]}"
    operation.version += 1
    db.commit()
    return operation


def _mark_engine_entered(operation_id: str, db: Session) -> None:
    operation = (db.query(M.ActionOperation)
                 .filter(M.ActionOperation.id == operation_id)
                 .with_for_update().first())
    if operation is None or operation.state != "executing":
        db.rollback()
        return
    operation.engine_entered_at = _now()
    operation.fence_token += 1
    db.commit()


def _invoke_engine(operation_id: str, db: Session):
    operation = db.query(M.ActionOperation).filter(
        M.ActionOperation.id == operation_id).first()
    if operation is None:
        return None
    confirmation = db.query(M.ActionConfirmation).filter(
        M.ActionConfirmation.id == operation.confirmation_id).first()
    if confirmation is None:
        return None
    # P2-C：worker/approve 后队列领取时共用 canonical 撤权重验 ——
    # approve 到入引擎之间撤权（含 HITL 批准后的技术执行领取）不执行；
    # 已入引擎/unknown 的外部证据裁决不因此翻案。
    from app.models.user import User as _User

    actor = db.query(_User).filter(
        _User.id == confirmation.actor_user_id).first()
    # P3-1：actor 用户行缺失/已删除时禁止 fail-open 跳过 canonical 重验 —
    # 与撤权同语义（未入引擎且无外部 attempt → failed_not_executed）。
    if actor is not None:
        try:
            _revalidate_confirmation_access(db, actor, confirmation,
                                            for_engine=True)
        except ConfirmationError as exc:
            if exc.code in ("access_revoked", "profile_disabled",
                            "action_forbidden"):
                operation.state = "failed_not_executed"
                operation.last_error = "access_revoked_before_engine"
                confirmation.state = "failed_not_executed"
                confirmation.failure_code = "access_revoked_before_engine"
                db.commit()
                return None
            db.rollback()
    else:
        operation.state = "failed_not_executed"
        operation.last_error = "actor_user_missing"
        confirmation.state = "failed_not_executed"
        confirmation.failure_code = "actor_user_missing"
        db.commit()
        return None
    _record_external_attempts_started(db, operation, confirmation)
    from app.ontologies.formal_modeling.schemas import RunActionRequest
    from app.ontologies.formal_modeling.action_engine import execute_action

    body = RunActionRequest(
        action_id=confirmation.action_id,
        parameters=confirmation.final_parameters,
        target_instance_id=confirmation.target_instance_id,
        dry_run=False,
        release_id=confirmation.release_id,
        idempotency_key=initial_engine_key(confirmation.id),
    )
    try:
        return execute_action(
            db, confirmation.ontology_id, body,
            actor_id=confirmation.actor_user_id,
            expected_release_id=confirmation.release_id)
    except Exception as exc:  # noqa: BLE001 - 失败也入账本
        db.rollback()
        return {"status": "failed", "errorMessage": str(exc)}



def _webhook_rule_ordinals(db: Session, confirmation) -> list[int]:
    from app.ontologies.agent_runtime.boundary import build_scope

    try:
        _, _, scope = build_scope(db, confirmation.ontology_id,
                                  release_id=confirmation.release_id)
        action = scope.require_action(confirmation.action_id)
    except Exception:  # noqa: BLE001
        return []
    ordinals = []
    for index, rule in enumerate(action.rules or []):
        if isinstance(rule, dict) and rule.get("type") == "webhook":
            ordinals.append(index)
    return ordinals


def _record_external_attempts_started(db: Session, operation, confirmation):
    ordinals = _webhook_rule_ordinals(db, confirmation)
    for ordinal in ordinals:
        existing = db.query(M.ActionExternalAttempt).filter(
            M.ActionExternalAttempt.operation_id == operation.id,
            M.ActionExternalAttempt.effect_ordinal == ordinal).first()
        if existing is not None:
            continue
        db.add(M.ActionExternalAttempt(
            operation_id=operation.id,
            effect_ordinal=ordinal,
            stable_delivery_key=(
                f"proposal-external:{confirmation.id}:{ordinal}"),
            state="started",
        ))
    db.commit()


def _apply_engine_result(operation_id: str, result,
                          db: Session | None = None) -> None:
    owned = db is None
    db = db if db is not None else _fresh_session()
    try:
        operation = (db.query(M.ActionOperation)
                    .filter(M.ActionOperation.id == operation_id)
                    .with_for_update().first())
        if operation is None:
            return
        confirmation = db.query(M.ActionConfirmation).filter(
            M.ActionConfirmation.id == operation.confirmation_id).first()
        if confirmation is None:
            return
        status = str((result or {}).get("status") or "").lower()
        pending = bool((result or {}).get("pendingApproval"))
        attempts = db.query(M.ActionExternalAttempt).filter(
            M.ActionExternalAttempt.operation_id == operation.id).all()
        unresolved = [a for a in attempts if a.state in ("started", "unknown")]
        if status == "success":
            # R2/§4.4（缺陷-4 整改）：本地 success 日志不能单独结案 —
            # 引擎 commit-then-send 窗口内外部结果可能仍不可知。存在未结案
            # external attempt 时按全部外部结果裁决：unknown 且保留 guard，
            # 本地已提交效果记入对账证据（不宣称其未发生，也不重发）。
            if unresolved:
                operation.state = "unknown"
                operation.execution_log_id = result.get("id")
                operation.last_error = "external_attempts_unresolved"
                operation.reconcile_evidence = {
                    "reason": "local_success_with_unresolved_external_attempt",
                    "localSuccessLogId": result.get("id"),
                    "attemptStates": {
                        a.effect_ordinal: a.state for a in attempts},
                }
                confirmation.state = "unknown"
                confirmation.failure_code = "execution_result_unknown"
            else:
                operation.state = "succeeded"
                operation.execution_log_id = result.get("id")
                confirmation.state = "succeeded"
                _stamp_log_lineage(db, operation, result.get("id"))
        elif pending or status == "pending":
            operation.state = "pending_approval"
            operation.approval_log_id = result.get("id")
            confirmation.state = "pending_approval"
            _stamp_log_lineage(db, operation, result.get("id"))
            # 引擎在进入效果阶段前返回审批闸门：预登记的 attempt 实际未发送
            for attempt in attempts:
                if attempt.state == "started":
                    attempt.state = "proven_not_applied"
                    attempt.finished_at = _now()
                    attempt.evidence_ref = "engine_returned_pending_before_effects"
        else:
            _apply_failure_with_external_evidence(
                db, operation, confirmation, result, status, attempts)
        _record_external_attempts_finished(db, operation, status)
        operation.lease_expires_at = None
        db.commit()
    finally:
        if owned:
            db.close()


def _stamp_log_lineage(db: Session, operation, *log_ids) -> None:
    """引擎日志回填 operation 血缘（幂等；恢复按 operation_id 双向定位）。"""
    from app.ontologies.formal_modeling.models import ActionExecutionLog

    ids = [i for i in log_ids if i]
    if not ids:
        return
    db.query(ActionExecutionLog).filter(
        ActionExecutionLog.id.in_(ids),
        ActionExecutionLog.operation_id.is_(None),
    ).update({ActionExecutionLog.operation_id: operation.id},
             synchronize_session=False)


def _engine_log_by_key(db: Session, confirmation):
    """同 key 已提交引擎日志（success/pending 保留键；failed 清键查不到）。"""
    from app.ontologies.formal_modeling.models import ActionExecutionLog

    return db.query(ActionExecutionLog).filter(
        ActionExecutionLog.ontology_id == confirmation.ontology_id,
        ActionExecutionLog.idempotency_key
        == initial_engine_key(confirmation.id),
    ).first()


def _adjudicate_attempts(db: Session, operation, attempts) -> dict:
    """外部 attempt 未结案集合（started/unknown）— 失败/恢复裁决的输入。"""
    return {a.effect_ordinal: a.state for a in attempts}


def _close_from_key_log(db: Session, operation, confirmation, key_log,
                        attempts=None, error_message: str = "engine_failed") -> bool:
    """同 key 引擎日志的保守结案（本地日志 ≠ 外部结果证据）。

    success：存在未结案（started/unknown）attempt → unknown（guard 保留，
    本地已提交效果记入对账证据，不宣称未发生也不重发）；否则 succeeded。
    pending/executing：进入 pending_approval，预登记 attempt 因引擎在效果
    阶段前返回而可证明未发送 → proven_not_applied。
    failed：不在此结案（返回 False），由调用方按外部证据走失败裁决。
    """
    if key_log is None:
        return False
    if attempts is None:
        attempts = db.query(M.ActionExternalAttempt).filter(
            M.ActionExternalAttempt.operation_id == operation.id).all()
    unresolved = [a for a in attempts if a.state in ("started", "unknown")]
    key_status = str(key_log.status or "").lower()
    if key_status == "success":
        if unresolved:
            operation.state = "unknown"
            operation.execution_log_id = key_log.id
            operation.last_error = "external_attempts_unresolved"
            operation.reconcile_evidence = {
                "reason": "local_success_with_unresolved_external_attempt",
                "localSuccessLogId": key_log.id,
                "attemptStates": _adjudicate_attempts(db, operation, attempts),
            }
            confirmation.state = "unknown"
            confirmation.failure_code = "execution_result_unknown"
        else:
            operation.state = "succeeded"
            operation.execution_log_id = key_log.id
            confirmation.state = "succeeded"
            _stamp_log_lineage(db, operation, key_log.id)
        return True
    if key_status in ("pending", "executing"):
        if (key_status == "executing"
                and operation.engine_entered_at is not None):
            # 裁决 2：技术 checkpoint 已激活 — 管理员批准后的技术执行进行
            # 中，不降回 pending_approval；由 terminal 日志收敛。lease 失效
            # 由 _reconcile_executing 的无结案证据路径裁决（不在此返回
            # True，让调用方落到 unknown+guard）。
            return operation.lease_expires_at is not None and _as_utc(
                operation.lease_expires_at) >= _now()
        operation.state = "pending_approval"
        operation.approval_log_id = key_log.id
        confirmation.state = "pending_approval"
        _stamp_log_lineage(db, operation, key_log.id)
        for attempt in attempts:
            if attempt.state == "started":
                attempt.state = "proven_not_applied"
                attempt.finished_at = _now()
                attempt.evidence_ref = (
                    "engine_returned_pending_before_effects")
        return True
    return False


def _apply_failure_with_external_evidence(db, operation, confirmation,
                                           result, status: str,
                                           attempts=None) -> None:
    """失败分支的保守裁决（契约 §4.4 可证明失败条件 + PM 2026-10-18 裁决）。

    engine_entered 已落库后，只有在「无外部效果点」或「外部全部
    proven_not_applied」时失败才可证明为 failed_not_executed 并释放 guard；
    存在未结案（started/unknown）external attempt、或 delivery_uncertain
    时必须进入 unknown（guard 保留）；外部已确认成功而本地失败
    → compensation_required（guard 保留）。同 key success 日志的恢复也
    服从外部证据优先：未结案 attempt 存在时结 unknown，不结 succeeded。
    """
    error_message = (result or {}).get("errorMessage") or "engine_failed"
    effects = (result or {}).get("effects") or []
    uncertain = any(
        isinstance(e, dict) and e.get("deliveryUncertain")
        for e in effects)

    if attempts is None:
        attempts = db.query(M.ActionExternalAttempt).filter(
            M.ActionExternalAttempt.operation_id == operation.id).all()
    unresolved = [
        a for a in attempts
        if a.state in ("started", "unknown")
    ]
    acknowledged = [a for a in attempts if a.state == "acknowledged"]
    entered = operation.engine_entered_at is not None

    # 崩溃窗口恢复：引擎可能在响应丢失前已提交 success/pending 日志；
    # 但本地日志不是外部结果的证据 — 未结案 attempt 仍然优先。
    if _close_from_key_log(db, operation, confirmation,
                           _engine_log_by_key(db, confirmation),
                           attempts, error_message):
        return

    if uncertain or (entered and unresolved):
        # 外部效果不可证未发送/未生效：unknown，guard 不释放
        operation.state = "unknown"
        operation.last_error = error_message
        operation.reconcile_evidence = {
            "reason": "unresolved_external_attempt_or_uncertain_delivery",
            "attemptStates": _adjudicate_attempts(db, operation, attempts),
        }
        confirmation.state = "unknown"
        confirmation.failure_code = "execution_result_unknown"
        return
    if acknowledged:
        # 外部已成功但本地失败：不能宣称整体成功，也不能当作未执行
        operation.state = "compensation_required"
        operation.last_error = error_message
        confirmation.state = "compensation_required"
        confirmation.failure_code = "external_applied_local_failed"
        return
    # 无外部效果点（或全部 proven_not_applied）：本地回滚即可证明未执行
    operation.state = "failed_not_executed"
    operation.last_error = error_message
    confirmation.state = "failed_not_executed"
    confirmation.failure_code = "engine_failed"


def _record_external_attempts_finished(db: Session, operation,
                                        status: str) -> None:
    """attempt 状态只在「整体成功」时升级 acknowledged；其余情况保持
    started/unknown 作为未结案证据，只补 finished_at。"""
    attempts = db.query(M.ActionExternalAttempt).filter(
        M.ActionExternalAttempt.operation_id == operation.id,
        M.ActionExternalAttempt.state == "started").all()
    for attempt in attempts:
        if status == "success" and operation.state == "succeeded":
            attempt.state = "acknowledged"
        elif operation.state in ("pending_approval", "failed_not_executed",
                                 "succeeded"):
            # pending/本地可证明未执行路径已单独标记 proven_not_applied；
            # 这里兜底其余结案态，不再产生新的 unknown 噪声
            pass
        else:
            attempt.state = "unknown"
        attempt.finished_at = _now()


# ------------------------------------------------------------- reconcile

def reconcile_operation(operation_id: str, db: Session | None = None) -> None:
    """恢复/对账：审批日志与引擎幂等键是事实源；缺证据 → unknown 不释放 guard。"""
    owned = db is None
    db = db if db is not None else _fresh_session()
    try:
        operation = (db.query(M.ActionOperation)
                    .filter(M.ActionOperation.id == operation_id)
                    .with_for_update().first())
        if operation is None:
            return
        confirmation = db.query(M.ActionConfirmation).filter(
            M.ActionConfirmation.id == operation.confirmation_id).first()
        if confirmation is None:
            return
        if operation.state == "pending_approval" and operation.approval_log_id:
            _reconcile_pending_approval(db, operation, confirmation)
        elif operation.state == "executing":
            _reconcile_executing(db, operation, confirmation)
        db.commit()
    finally:
        if owned:
            db.close()


def _reopen_attempts_for_technical_execution(db, operation) -> None:
    """裁决 2：管理员批准后的技术执行窗口重开 attempt 证据。

    引擎返回 pending 时预登记的 attempt 被标 ``proven_not_applied``（该次
    调用确实未进入效果阶段）。管理员批准触发 ``approval-execution:{log}``
    技术执行 — 该执行会真实发送 webhook：进入 executing/approved 即把
    ``proven_not_applied`` 的 attempt 重开为 ``started``（绑定技术阶段与
    technical key），崩溃窗口恢复按未结案证据裁决。
    """
    attempts = db.query(M.ActionExternalAttempt).filter(
        M.ActionExternalAttempt.operation_id == operation.id,
        M.ActionExternalAttempt.state == "proven_not_applied").all()
    for attempt in attempts:
        attempt.state = "started"
        attempt.finished_at = None
        attempt.evidence_ref = (
            f"technical-execution:{operation.approval_log_id}")
    if attempts and not operation.technical_engine_key:
        operation.technical_engine_key = (
            f"approval-execution:{operation.approval_log_id}")


def _reconcile_pending_approval(db, operation, confirmation) -> None:
    from app.ontologies.formal_modeling.models import ActionExecutionLog

    log = db.query(ActionExecutionLog).filter(
        ActionExecutionLog.id == operation.approval_log_id).first()
    if log is None:
        return
    status = str(log.status or "").lower()
    if status == "executing":
        # 裁决 2：技术执行 checkpoint/lease — 活跃执行显示 executing；
        # attempt 重开为证据；`approval-execution:{log}`↔operation 双向
        # 绑定回填（日志行 operation_id + operation technical key）。
        _reopen_attempts_for_technical_execution(db, operation)
        _stamp_log_lineage(db, operation, log.id)
        operation.state = "executing"
        confirmation.state = "executing"
        if not operation.engine_entered_at:
            operation.engine_entered_at = _now()
        operation.lease_expires_at = _now() + timedelta(
            seconds=OPERATION_LEASE_SECONDS)
        operation.technical_engine_key = (
            f"approval-execution:{log.id}")
        return
    if status == "pending":
        return
    if status == "approved":
        _reopen_attempts_for_technical_execution(db, operation)
        related = db.query(ActionExecutionLog).filter(
            ActionExecutionLog.id == log.related_log_id).first() \
            if log.related_log_id else None
        if related is not None:
            attempts = db.query(M.ActionExternalAttempt).filter(
                M.ActionExternalAttempt.operation_id == operation.id).all()
            unresolved = [a for a in attempts
                          if a.state in ("started", "unknown")]
            # 裁决 2：成功路径 attempt 已在重开后由引擎结果收敛
            related_effects = related.effects or []
            uncertain = any(
                isinstance(e, dict) and e.get("deliveryUncertain")
                for e in related_effects)
            if str(related.status or "").lower() == "success":
                if unresolved or uncertain:
                    # 审批技术执行的外部结果未结案：不因本地 success 结案
                    operation.state = "unknown"
                    operation.execution_log_id = related.id
                    operation.last_error = "external_attempts_unresolved"
                    operation.reconcile_evidence = {
                        "reason":
                            "approval_execution_with_unresolved_external",
                        "localSuccessLogId": related.id,
                        "attemptStates": {
                            a.effect_ordinal: a.state for a in attempts},
                    }
                    confirmation.state = "unknown"
                    confirmation.failure_code = "execution_result_unknown"
                    return
                operation.state = "succeeded"
                operation.execution_log_id = related.id
                confirmation.state = "succeeded"
                _stamp_log_lineage(db, operation, related.id, log.id)
                for attempt in attempts:
                    if attempt.state == "started":
                        attempt.state = "acknowledged"
                        attempt.finished_at = _now()
            else:
                # 审批技术执行失败：其外部效果若存在 delivery_uncertain，
                # 同样不能证明未执行 — unknown，guard 保留（与执行路径一致）。
                if uncertain:
                    operation.state = "unknown"
                    operation.execution_log_id = related.id
                    operation.last_error = related.error_message
                    operation.reconcile_evidence = {
                        "reason": "approval_execution_uncertain_delivery",
                        "localFailureLogId": related.id,
                    }
                    confirmation.state = "unknown"
                    confirmation.failure_code = "execution_result_unknown"
                    return
                operation.state = "failed_not_executed"
                operation.execution_log_id = related.id
                operation.last_error = related.error_message
                confirmation.state = "failed_not_executed"
                confirmation.failure_code = "approval_execution_failed"
        else:
            confirmation.state = "pending_approval"
        return
    if status == "rejected":
        operation.state = "approval_rejected"
        confirmation.state = "approval_rejected"
        return
    if status == "failed":
        operation.state = "failed_not_executed"
        operation.last_error = log.error_message
        confirmation.state = "failed_not_executed"
        confirmation.failure_code = "engine_failed"


def _reconcile_executing(db, operation, confirmation) -> None:
    """lease 过期恢复：本地引擎日志不是外部结果证据，结案必须经外部
    attempt 裁决 — 与执行路径（_apply_engine_result / 失败裁决器）同一
    语义，防止 worker 崩溃/失锁后的 success/failed 日志被直接采信而
    误报已执行/未执行并释放 guard。"""
    from app.ontologies.formal_modeling.models import ActionExecutionLog

    lease = _as_utc(operation.lease_expires_at)
    if lease is not None and lease >= _now():
        return
    keys = [initial_engine_key(confirmation.id)]
    if operation.technical_engine_key:
        # 裁决 2：技术执行键（approval-execution:{log}）双向核对
        keys.append(operation.technical_engine_key)
    key_log = None
    for key in keys:
        key_log = db.query(ActionExecutionLog).filter(
            ActionExecutionLog.ontology_id == confirmation.ontology_id,
            ActionExecutionLog.idempotency_key == key).first()
        if key_log is not None:
            break
    attempts = db.query(M.ActionExternalAttempt).filter(
        M.ActionExternalAttempt.operation_id == operation.id).all()
    # success/pending 日志：与执行路径相同的保守裁决（未结案 attempt
    # 存在 → unknown，guard 保留；pending 才允许 proven_not_applied）。
    if _close_from_key_log(db, operation, confirmation, key_log, attempts):
        return
    if key_log is not None and str(key_log.status or "").lower() == "failed":
        # 本地 failed 日志同样不能单独证明未执行：交由失败分支的外部
        # 证据裁决（uncertain/unresolved → unknown；acknowledged →
        # compensation_required；无可证外部效果才 failed_not_executed）。
        _apply_failure_with_external_evidence(
            db, operation, confirmation,
            {"errorMessage": key_log.error_message,
             "effects": key_log.effects or []},
            "failed", attempts)
        return
    if operation.engine_entered_at is None:
        # durable engine_entered 为空且 lease 已过：可安全重新派发同一 operation
        operation.state = "dispatch_queued"
        operation.claimed_at = None
        operation.lease_expires_at = None
        return
    # 引擎已进入但无结案证据：不能推定失败/未执行
    # 裁决 2：unknown + blockReason + 历史入口，保留 guard；不重批/盲发/
    # 自动超时释放。组织授权者凭权威外部结果解除（外部对账接收人
    # 1040787244）。
    operation.state = "unknown"
    operation.reconcile_evidence = {
        "reason": "engine_entered_without_terminal_evidence",
        "checkedLogKeys": keys,
        "attemptStates": _adjudicate_attempts(db, operation, attempts),
        "recoveryOwner": "external-reconcile:1040787244",
    }
    confirmation.state = "unknown"
    confirmation.failure_code = "execution_result_unknown"


def confirmation_out(db: Session, confirmation: M.ActionConfirmation, *,
                     replayed: bool = False) -> dict:
    block_reason = None
    execution_log_id = None
    approval_log_id = None
    if confirmation.operation_id:
        operation = db.query(M.ActionOperation).filter(
            M.ActionOperation.id == confirmation.operation_id).first()
        if operation is not None:
            execution_log_id = operation.execution_log_id
            approval_log_id = operation.approval_log_id
    if confirmation.state == "unknown":
        block_reason = (
            "执行结果未知且保留保护：无自动超时释放；由外部对账接收人"
            "（1040787244）确认权威外部结果后解除，或经有界对账收敛")
    return {
        "confirmationId": confirmation.id,
        "state": confirmation.state,
        "payloadHash": confirmation.payload_hash,
        "display": confirmation.display_payload or {},
        "expiresAt": _as_utc(confirmation.expires_at).isoformat(),
        "requiresApproval": bool(confirmation.requires_approval),
        "replayed": replayed,
        "operationId": confirmation.operation_id,
        "executionLogId": execution_log_id,
        "approvalLogId": approval_log_id,
        "failureCode": confirmation.failure_code,
        "blockReason": block_reason,
        "decidedAt": _as_utc(confirmation.decided_at).isoformat()
        if confirmation.decided_at else None,
        "createdAt": _as_utc(confirmation.created_at).isoformat()
        if confirmation.created_at else None,
    }
