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
import logging
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ontologies.agent_confirmation import models as M
from app.ontologies.agent_confirmation.schemas import (
    PrepareProposalRequest, ParameterRef,
)
from app.ontologies.agent_confirmation import write_port as WP
from app.ontologies.agent_query.models import (
    AgentQueryRow, AgentQuerySnapshot,
)
from app.ontologies.release_context import current_release_context

logger = logging.getLogger(__name__)

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
        for proposal in (msg.proposals or []):
            if isinstance(proposal, dict) and proposal.get("proposalId") == \
                    body.client_submission_id:

                return msg.id if msg.role == "user" else _last_user_intent(
                    db, conversation.id, before=msg.created_at)
    # 允许直接以消息 id 提交（前端回放场景）
    direct = db.query(AgentMessage).filter(
        AgentMessage.id == body.client_submission_id,
        AgentMessage.conversation_id == conversation.id).first()
    if direct is not None:
        return _last_user_intent(db, conversation.id,
                                 before=direct.created_at) or direct.id
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
    target_instance = None
    if body.target_instance_id:
        from app.models.ontology_formal import ObjectInstance

        target_instance = db.query(ObjectInstance).filter(
            ObjectInstance.id == body.target_instance_id).first()
        try:
            scope.visible_instance(target_instance)
        except ToolError as exc:
            raise ConfirmationError(422, "invalid_target", str(exc)) from exc
        target_snapshot_label = (
            getattr(target_instance, "external_id", None) or target_instance.id)
    elif body.target_ref is not None:
        try:
            scope.require_object_type(body.target_ref.object_type_id)
        except ToolError as exc:
            raise ConfirmationError(422, "invalid_target", str(exc)) from exc

    parameters = dict(body.parameters)
    if WP.WRITE_PLAN_RESERVED_KEY in parameters:
        raise ConfirmationError(
            422, "invalid_parameter", "该参数名为服务端保留键，不可作为业务参数")
    parameter_sources = _resolve_parameter_refs(db, user, body.parameter_refs,
                                                parameters)
    final_params, validation_errors = prepare_action_parameters(
        action, parameters)
    if validation_errors:
        raise ConfirmationError(422, "invalid_parameter",
                                "参数校验失败", validationErrors=validation_errors)
    for key in parameters:
        parameter_sources.setdefault(key, {"kind": "user_input"})

    # —— 资产写路由（goal §6）：按 release 快照映射决定本地/三源路由 ——
    write_plan = _resolve_asset_write_plan(
        db, user, ontology_id, context, action, scope, final_params,
        body, target_instance)

    if root_intent_id is None:
        root_intent_id = _resolve_http_root_intent(db, user, body)
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
    if write_plan is not None:
        # 冻结计划进入不可变载荷哈希：approve 回显的 payloadHash 覆盖资产
        # 写语义，任何事后改动都会被 decide 的指纹比对拒绝。
        payload["writePlan"] = write_plan
        parameter_sources[WP.WRITE_PLAN_RESERVED_KEY] = write_plan
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
    if write_plan is not None:
        # 公开摘要（不含 CAS 私有材料）：expected token / before 行只存
        # parameter_sources 保留键，绝不进入 display 或任何公开响应。
        display["target"] = display.get("target") or {}
        display["target"]["asset"] = WP.public_plan_summary(write_plan)
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


# ------------------------------------------------- asset write routing

def _resolve_asset_write_plan(db: Session, user, ontology_id: str, context,
                              action, scope, final_params: dict,
                              body: PrepareProposalRequest, target_instance):
    """prepare 阶段解析资产写计划；None = 保持本地动作语义。

    顺序：纯规则派生（描述性，不拒绝组合）→ 类型绑定判定（release 快照
    原文，fail-closed）→ 绑定后才执行组合守卫与端口解析（映射/schema/
    权限/源读 token 在适配器内完成并冻结）。任何拒绝都发生在零业务写阶段。
    """
    from app.ontologies.formal_modeling.action_runtime_values import (
        _resolve_value,
    )
    from app.ontologies.formal_modeling.safe_eval import SafeEvalError

    target_props = None
    if target_instance is not None:
        target_props = {
            **dict(target_instance.properties or {}),
            **dict(target_instance.computed or {}),
        }
    target_ref = None
    if body.target_ref is not None:
        target_ref = WP.TargetRef(
            object_type_id=str(body.target_ref.object_type_id),
            key=dict(body.target_ref.key or {}))

    def _resolve(mapping, params, props):
        return _resolve_value(mapping, params, props, db, ontology_id,
                              ontology_release_id=context.release.id)

    try:
        request = WP.build_write_request(
            action, final_params,
            target_props=target_props,
            target_instance_id=body.target_instance_id,
            target_ref=target_ref,
            resolve_value=_resolve)
    except WP.WritePortError as exc:
        raise ConfirmationError(exc.status, exc.code, exc.message,
                                **exc.extra) from exc
    except SafeEvalError as exc:
        raise ConfirmationError(422, "invalid_parameter",
                                f"写值解析失败：{exc}") from exc

    # targetRef 类型必须与动作绑定类型一致（防跨类型定位）。
    if (target_ref is not None and action.object_type_id
            and str(target_ref.object_type_id) != str(action.object_type_id)):
        raise ConfirmationError(
            422, "invalid_target",
            "targetRef.objectTypeId 与动作绑定的对象类型不一致",
            expected=str(action.object_type_id),
            provided=str(target_ref.object_type_id))

    touched = request.touched_type_ids()
    if request.link_type_ids:
        touched |= _link_endpoint_types(context, request.link_type_ids)
    if not touched:
        return None
    bound_types = _mapping_bound_object_types(context)
    if not (touched & bound_types):
        if request.operation == WP.OPERATION_DELETE:
            raise ConfirmationError(
                422, "delete_row_requires_asset",
                "delete_row 仅支持经资产映射路由的动作；目标类型未绑定数据"
                "资产，本地引擎没有对象删除语义")
        return None

    # 绑定命中：组合守卫先于端口解析，拒绝时源侧零变更。
    try:
        WP.enforce_asset_write_guards(request)
    except WP.WritePortError as exc:
        raise ConfirmationError(exc.status, exc.code, exc.message,
                                **exc.extra) from exc

    # 属性级授权（profile allowed_properties）：写入属性与定位键属性都必须
    # 在边界解析范围内（targetRef.key 同样不可越权）。
    from app.ontologies.agent_runtime.boundary import ToolError

    try:
        write_type = scope.require_object_type(request.object_type_id)
        for prop in [*request.property_values, *request.key_values,
                     *request.read_properties]:
            scope.resolve_property(write_type, prop)
    except ToolError as exc:
        raise ConfirmationError(403, "property_write_forbidden",
                                str(exc)) from exc

    try:
        plan = WP.action_write_port().resolve_write(
            db, user, ontology_id=ontology_id,
            release_id=context.release.id, request=request)
    except WP.WritePortError as exc:
        raise ConfirmationError(exc.status, exc.code, exc.message,
                                **exc.extra) from exc
    if plan is None:
        return None
    try:
        plan = WP.validate_frozen_plan(plan)
    except WP.WritePortError as exc:
        raise ConfirmationError(
            exc.status, exc.code,
            f"适配器返回的冻结计划不合法：{exc.message}") from exc

    # targetRef 场景的延迟取值：读取有界目标快照（plan.targetSnapshot）后
    # 按规则顺序求值，经 valueMap 合并进计划列值 — 求值失败即 prepare
    # 拒绝（零源变更），不用空 target_props 算错值。
    if request.pending_value_mappings:
        snapshot_props = plan.get("targetSnapshot")
        if not isinstance(snapshot_props, dict):
            snapshot_props = {}
        merged_values = dict(plan.get("values") or {})
        for prop in sorted(request.pending_value_mappings):
            mapping = request.pending_value_mappings[prop]
            try:
                value = _resolve(mapping, final_params, dict(snapshot_props))
            except SafeEvalError as exc:
                raise ConfirmationError(
                    422, "invalid_parameter",
                    f"写值解析失败（属性 {prop}）：{exc}") from exc
            column = (plan.get("valueMap") or {}).get(prop)
            if column is None:
                raise ConfirmationError(
                    422, "unknown_write_column",
                    f"属性「{prop}」未映射到资产列，拒绝写")
            merged_values[column] = value
        plan["values"] = merged_values

    # 资产路由绕过本地引擎，动作自身的校验规则必须在此真实执行（任何
    # 源写之前）：validation 表达式/函数按冻结目标快照求值，失败即 prepare
    # 拒绝、零源变更。目标快照优先取计划内源读单行（targetRef/curated），
    # 本地实例路径回落实例属性。
    validation_props = plan.get("targetSnapshot")
    if not isinstance(validation_props, dict):
        validation_props = target_props or {}
    from app.ontologies.formal_modeling.action_runtime_values import _validate
    from app.ontologies.formal_modeling.action_validation import (
        _prepare_action_rules,
    )

    rules, rule_errors = _prepare_action_rules(action)
    if rule_errors:
        raise ConfirmationError(422, "action_rules_invalid",
                                "动作规则定义无效", errors=rule_errors)
    errors = _validate(
        action, final_params, validation_props or None, db, ontology_id,
        rules, ontology_release_id=context.release.id)
    if errors:
        raise ConfirmationError(422, "invalid_parameter",
                                "动作校验未通过", validationErrors=errors)
    return plan


def _release_snapshot_dict(context) -> dict:
    snapshot = context.release.snapshot_formal
    if not isinstance(snapshot, dict):
        raise ConfirmationError(
            409, "mapping_contract_invalid", "发布快照损坏，无法判定资产绑定")
    return snapshot


def _mapping_bound_object_types(context) -> set[str]:
    """release 快照映射覆盖的对象类型 id 集合（远程 + 本地数据集绑定）。

    直接解析权威快照原文；任何解析异常 fail-closed（错误路由到 local 会
    产生孤儿本地写）。仅当快照证明 mappings 为空/无绑定类型时才返回空集。
    """
    snapshot = _release_snapshot_dict(context)
    try:
        by_name: dict[str, str] = {}
        for ot in snapshot.get("objectTypes") or []:
            if isinstance(ot, dict) and ot.get("id"):
                for key in ("name", "displayName"):
                    if ot.get(key):
                        by_name[str(ot[key])] = str(ot["id"])
        bound: set[str] = set()
        for entry in snapshot.get("mappings") or []:
            if not isinstance(entry, dict):
                continue
            target = entry.get("targetObjectTypeId")
            if target:
                bound.add(str(target))
                continue
            entity = str(entry.get("entityClass") or "")
            if entity and entity in by_name:
                bound.add(by_name[entity])
        return bound
    except ConfirmationError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail-closed，绝不回退 local
        raise ConfirmationError(
            409, "mapping_contract_invalid",
            f"发布快照映射解析失败：{exc}") from exc


def _link_endpoint_types(context, link_type_ids) -> set[str]:
    """快照 linkTypes 中这些链接类型的两端对象类型 id（fail-closed）。

    快照键为 camelCase（snapshot 序列化别名）；缺失链接定义或缺失端点键
    都是错误，不得静默当成未绑定（否则绑定守卫整体失效）。
    """
    snapshot = _release_snapshot_dict(context)
    wanted = {str(item) for item in link_type_ids}
    endpoints: set[str] = set()
    matched: set[str] = set()
    try:
        for link_type in snapshot.get("linkTypes") or []:
            if not isinstance(link_type, dict):
                continue
            if str(link_type.get("id") or "") not in wanted:
                continue
            matched.add(str(link_type.get("id")))
            source = link_type.get("sourceObjectTypeId")
            target = link_type.get("targetObjectTypeId")
            if not source or not target:
                raise ConfirmationError(
                    422, "invalid_parameter",
                    f"链接类型 {link_type.get('id')} 快照缺少两端对象类型定义")
            endpoints.add(str(source))
            endpoints.add(str(target))
    except ConfirmationError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail-closed
        raise ConfirmationError(
            409, "mapping_contract_invalid",
            f"发布快照链接类型解析失败：{exc}") from exc
    unresolved = wanted - matched
    if unresolved:
        raise ConfirmationError(
            422, "invalid_parameter",
            f"链接规则引用的链接类型不在发布快照中：{sorted(unresolved)}")
    return endpoints


def _frozen_plan(confirmation: M.ActionConfirmation) -> Optional[dict]:
    raw = (confirmation.parameter_sources or {}).get(WP.WRITE_PLAN_RESERVED_KEY)
    if not raw:
        return None
    return WP.validate_frozen_plan(raw)


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
        _verify_frozen_payload_integrity(confirmation)
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

    # 属性级授权（allowed_properties）在 decide 时按执行时态重验：冻结计划
    # 的 keyProperties/properties 必须仍在当前 profile 边界内（A21/A23：
    # prepare 后撤字段权限 → confirm 也拒绝，不只靠 execute 兜底）。
    _frozen_plan_field_guard(db, confirmation, scope)

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
    plan = _frozen_plan(confirmation)
    if plan is not None and confirmation.requires_approval:
        # 资产写 + 引擎级审批：用户确认后先落 HITL 待审日志与治理收件箱，
        # 源写派发由管理员批准（decide_pending_action →
        # dispatch_approved_asset_write）触发，拒绝则结 approval_rejected。
        # 与本地引擎 HITL 同一审批队列/血缘，不另起审批通道。
        _enter_asset_approval_gate(db, confirmation, plan)
        db.refresh(confirmation)
        return confirmation
    db.refresh(confirmation)
    return confirmation


def _enter_asset_approval_gate(db: Session, confirmation,
                               plan: dict) -> None:
    from app.ontologies.formal_modeling.models import ActionExecutionLog

    log = ActionExecutionLog(
        ontology_id=confirmation.ontology_id,
        action_id=confirmation.action_id,
        action_name=None,
        object_type_id=None,
        object_instance_id=confirmation.target_instance_id,
        parameters=confirmation.final_parameters,
        status="pending",
        idempotency_key=f"proposal-approval:{confirmation.id}",
        ontology_version=confirmation.release_version,
        ontology_release_id=confirmation.release_id,
        operation_id=confirmation.operation_id,
        dry_run=False, actor_id=confirmation.actor_user_id,
    )
    db.add(log)
    try:
        db.flush()
        from app.inbox.service import enqueue_ontology_approval_request

        enqueue_ontology_approval_request(
            db,
            log_id=log.id,
            ontology_id=confirmation.ontology_id,
            action_name=confirmation.action_id,
            ontology_version=confirmation.release_version,
            ontology_release_id=confirmation.release_id,
            occurred_at=_now(),
        )
        db.query(M.ActionOperation).filter(
            M.ActionOperation.id == confirmation.operation_id
        ).update({
            M.ActionOperation.state: "pending_approval",
            M.ActionOperation.approval_log_id: log.id,
        }, synchronize_session=False)
        confirmation.state = "pending_approval"
        db.commit()
    except Exception:
        db.rollback()
        raise


def _frozen_plan_field_guard(db, confirmation, scope) -> None:
    """资产路由确认的 decide 时字段级重验（allowed_properties 收紧）。

    冻结计划携带 keyProperties（定位键属性）与 properties（写入属性）；
    两者都必须在当前 profile 的 allowed_properties 边界内。非资产确认
    （无冻结计划）不在此门——本地动作的属性由引擎在效果阶段检查。
    """
    plan = _frozen_plan(confirmation)
    if plan is None:
        return
    from app.ontologies.agent_runtime.boundary import ToolError

    props = [str(p) for p in [*(plan.get("keyProperties") or []),
                              *(plan.get("properties") or [])] if p]
    if not props:
        return
    try:
        write_type = scope.require_object_type(
            str(plan.get("objectTypeId") or ""))
        for prop in props:
            scope.resolve_property(write_type, prop)
    except ToolError as exc:
        confirmation.state = "expired"
        confirmation.failure_code = "property_write_forbidden"
        db.commit()
        raise ConfirmationError(
            403, "property_write_forbidden",
            f"属性级权限已收紧：{exc}",
            currentState="expired", reprepareRequired=True) from exc


def _verify_frozen_payload_integrity(confirmation: M.ActionConfirmation) -> None:
    """计划承载确认的载荷完整性自检（decide 时重算，防服务端材料被改）。

    仅对冻结计划存在的确认生效；无计划的本地确认保持既有语义（用户回显
    hash 与存储 hash 比对已足够，不改变旧确认行为）。重算用的 payload 与
    prepare 完全同构 — 含冻结计划本体，任何事后改动都会被拒绝。
    """
    plan = _frozen_plan(confirmation)
    if plan is None:
        return
    payload = {
        "actor": confirmation.actor_user_id,
        "sessionKind": confirmation.session_kind,
        "sessionId": confirmation.session_id,
        "rootIntentId": confirmation.root_intent_id,
        "origin": confirmation.origin,
        "ontology": confirmation.ontology_id,
        "release": confirmation.release_id,
        "action": confirmation.action_id,
        "target": confirmation.target_instance_id,
        "parameters": confirmation.final_parameters,
        "requiresApproval": bool(confirmation.requires_approval),
        "writePlan": plan,
    }
    recomputed = _sha256(_canonical_json(payload))
    if not hmac.compare_digest(recomputed, confirmation.payload_hash):
        raise ConfirmationError(
            409, "prepare_payload_conflict",
            "确认载荷完整性校验失败：冻结材料与载荷指纹不一致，拒绝确认")


def _guard_hash(confirmation: M.ActionConfirmation) -> str:
    guard_input = {
        "actor": confirmation.actor_user_id,
        "ontology": confirmation.ontology_id,
        "action": confirmation.action_id,
        "target": confirmation.target_instance_id,
        "parameters": confirmation.final_parameters,
    }
    plan = _frozen_plan(confirmation)
    if plan is not None:
        # 旧确认（无计划）保持原 guard 语义；有计划时加入业务恒等投影 —
        # CAS 材料（expectedToken/before/fingerprint）剔除，避免同业务意图
        # 的不同准备失去互斥（并发双写）。
        guard_input["writePlan"] = WP.logical_guard_plan(plan)
    return _sha256(_canonical_json(guard_input))


# --------------------------------------------------------------- execute

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
    HTTP 内联执行传入请求会话，同一事务边界内完成。资产路由确认走
    :func:`_invoke_asset_write`（外部 attempt 先行落账，再派发单行源写）。
    """
    owned = db is None
    session = db if db is not None else _fresh_session()
    try:
        op = _claim(operation_id, session)
        if op is None:
            return
        _mark_engine_entered(op.id, session)
        confirmation = session.query(M.ActionConfirmation).filter(
            M.ActionConfirmation.id == op.confirmation_id).first()
        plan = _frozen_plan(confirmation) if confirmation is not None else None
        if plan is not None:
            _invoke_asset_write(session, op, confirmation, plan)
        else:
            result = _invoke_engine(op.id, session)
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


# ------------------------------------------------------------- asset write

ASSET_WRITE_EFFECT_ORDINAL = 0


def _record_asset_write_attempt(
        db: Session, operation) -> tuple[M.ActionExternalAttempt, bool]:
    """源写派发前持久化外部 attempt（started）并提交 — 崩溃窗口的对账锚点。

    返回 (attempt, created)。``created=False`` 表示存在历史 attempt：
    acknowledged=此前源写已回执（只读恢复，禁止再写）；started/unknown=
    崩溃窗口未结案（必须 unknown，禁止自动重放）。
    """
    attempt = db.query(M.ActionExternalAttempt).filter(
        M.ActionExternalAttempt.operation_id == operation.id,
        M.ActionExternalAttempt.effect_ordinal
        == ASSET_WRITE_EFFECT_ORDINAL).first()
    if attempt is not None:
        return attempt, False
    attempt = M.ActionExternalAttempt(
        operation_id=operation.id,
        effect_ordinal=ASSET_WRITE_EFFECT_ORDINAL,
        stable_delivery_key=(
            f"proposal-write:{operation.confirmation_id}"),
        state="started",
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt, True


def _recover_attempt_outcome(
        plan: dict, attempt: M.ActionExternalAttempt
) -> Optional[WP.AssetWriteOutcome]:
    """既有 attempt 的只读恢复 — 绝不重新派发源写；无证据按 unknown。"""
    try:
        evidence = json.loads(attempt.evidence_ref or "")
    except (TypeError, ValueError):
        evidence = None
    evidence = evidence if isinstance(evidence, dict) else None
    if not evidence:
        # 无证据不拼装回执：仅携带计划身份的 unknown（不虚构任何结果字段）。
        return WP.AssetWriteOutcome(
            asset_kind=str(plan.get("assetKind") or "unknown"),
            status=WP.OUTCOME_UNKNOWN,
            operation=str(plan.get("operation") or "update"),
            affected_rows=0,
            dataset_id=str(plan.get("datasetId") or ""),
            error_code="execution_result_unknown",
            message="attempt 无可读回执证据，保持 unknown 人工对账",
        )
    return WP.AssetWriteOutcome(
        asset_kind=str(evidence.get("assetKind") or "unknown"),
        status=str(evidence.get("status") or WP.OUTCOME_UNKNOWN),
        operation=str(evidence.get("operation") or "update"),
        affected_rows=int(evidence.get("affectedRows") or 0),
        dataset_id=str(evidence.get("datasetId") or ""),
        version_id=evidence.get("versionId"),
        review_id=evidence.get("reviewId"),
        receipt_id=evidence.get("receiptId"),
        error_code=str(evidence.get("errorCode")
                       or "execution_result_unknown"),
        message=str(evidence.get("message") or "历史回执只读恢复"),
    )


def _asset_failure_outcome(plan: dict, operation: str, code: str,
                           message: str) -> WP.AssetWriteOutcome:
    return WP.AssetWriteOutcome(
        asset_kind=str(plan.get("assetKind") or "unknown"),
        status=WP.OUTCOME_FAILED,
        operation=str(plan.get("operation") or operation or "update"),
        affected_rows=0,
        dataset_id=str(plan.get("datasetId") or ""),
        error_code=code,
        message=message,
    )


def _invoke_asset_write(db: Session, operation, confirmation, plan: dict) -> None:
    """资产路由确认的执行：release 栅栏 → attempt 落账 → 端口单行写 → 结案。

    顺序保证：attempt(started) 提交必须先于适配器内部业务 commit；确定性
    拒绝（WritePortError，源侧零变更可证明）结 failed_not_executed；适配器
    意外异常按 unknown 处理 — 源写可能已发出，不自动重放，guard 保留人工对账。
    结案携带 fence（engine_entered 递增值）：过期的并发 worker 不能覆盖
    更新一次派发的裁决。
    """
    # 派发前完整性重验：私有冻结材料与载荷指纹必须仍一致（批准后任何
    # 服务端篡改都不能凭旧 hash 执行）。
    try:
        _verify_frozen_payload_integrity(confirmation)
    except ConfirmationError as exc:
        operation = db.query(M.ActionOperation).filter(
            M.ActionOperation.id == operation.id).first()
        if operation is not None:
            _apply_asset_write_result(
                operation.id,
                _asset_failure_outcome(
                    plan, plan.get("operation"), exc.code,
                    "冻结载荷完整性校验失败，源写未执行"),
                db, expected_fence=operation.fence_token)
        return
    # 重载 operation 取当前 fence（_mark_engine_entered 已提交递增）。
    operation = db.query(M.ActionOperation).filter(
        M.ActionOperation.id == operation.id).first()
    if operation is None:
        return
    fence = operation.fence_token
    # release 栅栏：冻结计划跨发布即拒绝（源写零变更可证明）。
    try:
        current_release_context(db, confirmation.ontology_id,
                                expected_release_id=confirmation.release_id)
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "detail", None)
        code = detail.get("code", "release_context_changed") if isinstance(
            detail, dict) else "release_context_changed"
        _apply_asset_write_result(
            operation.id,
            _asset_failure_outcome(plan, plan.get("operation"),
                                   code, "当前发布版本已变化，源写未执行"),
            db, expected_fence=fence)
        return

    attempt, attempt_created = _record_asset_write_attempt(db, operation)
    if not attempt_created:
        # A11：历史 attempt 存在 → 一次派发已发生过。acknowledged=已有
        # 完整回执，只读恢复终态；started/unknown=崩溃窗口，只读结 unknown。
        # 两种情况都绝不再次 apply_write（不自动重放）。
        if attempt.state == "acknowledged":
            outcome = _recover_attempt_outcome(plan, attempt)
        else:
            outcome = WP.AssetWriteOutcome(
                asset_kind=str(plan.get("assetKind") or "unknown"),
                status=WP.OUTCOME_UNKNOWN,
                operation=str(plan.get("operation") or "update"),
                affected_rows=0,
                dataset_id=str(plan.get("datasetId") or ""),
                error_code="execution_result_unknown",
                message="检测到未结案的历史源写 attempt：保持 unknown，"
                        "禁止自动重放，等待人工对账",
            )
        _apply_asset_write_result(operation.id, outcome, db,
                                  expected_fence=fence)
        return
    try:
        port = WP.action_write_port()
    except WP.WritePortError as exc:
        _apply_asset_write_result(
            operation.id,
            _asset_failure_outcome(plan, plan.get("operation"),
                                   exc.code, exc.message),
            db, expected_fence=fence)
        return

    from app.auth.models import User

    user = db.query(User).filter(
        User.id == confirmation.actor_user_id).first()
    if user is None or not getattr(user, "is_active", True):
        _apply_asset_write_result(
            operation.id,
            _asset_failure_outcome(plan, plan.get("operation"),
                                   "actor_invalid", "确认发起人不存在或已停用"),
            db, expected_fence=fence)
        return
    # 执行前完整重授权（A23）：本体写权限 + profile + 动作/类型/属性范围
    # 全部按执行时态重验；任何拒绝都发生在源写之前（零源变更可证明）。
    gate_error = _asset_execution_authorization_error(
        db, user, confirmation, plan)
    if gate_error is not None:
        _apply_asset_write_result(
            operation.id,
            _asset_failure_outcome(plan, plan.get("operation"),
                                   gate_error[0], gate_error[1]),
            db, expected_fence=fence)
        return
    try:
        outcome = port.apply_write(
            db, user, plan,
            confirmation_id=confirmation.id,
            actor_id=confirmation.actor_user_id)
    except WP.WritePortError as exc:
        if exc.extra.get("businessArtifactCreated"):
            # 失败路径已留下业务痕迹（如空 pending review 壳）：不能宣称
            # 零写/未执行 — unknown，guard 保留人工对账。
            outcome = WP.AssetWriteOutcome(
                asset_kind=str(plan.get("assetKind") or "unknown"),
                status=WP.OUTCOME_UNKNOWN,
                operation=str(plan.get("operation") or "update"),
                affected_rows=0,
                dataset_id=str(plan.get("datasetId") or ""),
                review_id=exc.extra.get("reviewId"),
                receipt_id=exc.extra.get("receiptId"),
                error_code=exc.code,
                message=f"资产写失败且已留下待对账业务痕迹：{exc.message}",
            )
        else:
            outcome = WP.AssetWriteOutcome(
                asset_kind=str(plan.get("assetKind") or "unknown"),
                status=WP.OUTCOME_FAILED,
                operation=str(plan.get("operation") or "update"),
                affected_rows=0,
                dataset_id=str(plan.get("datasetId") or ""),
                review_id=exc.extra.get("reviewId"),
                receipt_id=exc.extra.get("receiptId"),
                error_code=exc.code,
                message=exc.message,
            )
    except Exception as exc:  # noqa: BLE001 - 源写可能已发出：unknown 不重放
        db.rollback()
        logger.exception("资产写适配器异常 op=%s", operation.id)
        outcome = WP.AssetWriteOutcome(
            asset_kind=str(plan.get("assetKind") or "unknown"),
            status=WP.OUTCOME_UNKNOWN,
            operation=str(plan.get("operation") or "update"),
            affected_rows=0,
            dataset_id=str(plan.get("datasetId") or ""),
            error_code="execution_result_unknown",
            message="资产写执行结果未知（适配器异常），保持 unknown 不自动重放",
        )
    _apply_asset_write_result(operation.id, outcome, db, expected_fence=fence)


def _asset_execution_authorization_error(db, user, confirmation,
                                         plan: dict) -> Optional[tuple]:
    """执行时态的完整重授权：返回 (code, message) 或 None 放行。

    复用既有守卫（require_ontology_access / build_scope / resolve_property），
    覆盖：本体写权限（含 owner/admin 口径）、profile 启用与提案开关、动作/
    类型/属性（含 allowed_properties 收紧）授权。批准后撤权在此拦截。
    """
    from fastapi import HTTPException

    from app.ontologies.access import require_ontology_access
    from app.ontologies.agent_runtime.boundary import ToolError, build_scope

    from app.auth.permissions import user_has_menu_access

    if not user_has_menu_access(db, user, "data"):
        return ("source_write_denied",
                "确认发起人的角色已无资产湖（data）菜单权限，拒绝源写")
    try:
        require_ontology_access(db, confirmation.ontology_id, user,
                                write=True)
    except HTTPException as exc:
        return ("source_write_denied",
                f"确认发起人已无该本体的写权限：{exc.detail}")
    try:
        _, profile, scope = build_scope(
            db, confirmation.ontology_id, release_id=confirmation.release_id)
    except ToolError as exc:
        return ("release_context_changed", str(exc))
    if not profile.enabled or not profile.allow_action_proposals:
        return ("profile_disabled", "该本体的智能体已停用或禁止提案")
    try:
        scope.require_action(confirmation.action_id)
        write_type = scope.require_object_type(str(plan.get("objectTypeId")))
        for prop in [*(plan.get("keyProperties") or []),
                     *(plan.get("properties") or [])]:
            scope.resolve_property(write_type, str(prop))
    except ToolError as exc:
        return ("action_forbidden", str(exc))
    return None


def _apply_asset_write_result(operation_id: str, outcome: WP.AssetWriteOutcome,
                              db: Session | None = None,
                              *, expected_fence: int | None = None) -> None:
    """资产写结果的持久结案（与引擎结果映射同一状态词汇表）。

    applied/pending_review → succeeded（curated 的 pending_review 表示变更
    已记录待审核：affectedRows=0、数据未生效；后续按 review 真实状态在
    读取时刷新 outcome，不重新执行）。unknown → guard 保留。failed →
    failed_not_executed（源侧零变更可证明的确定性拒绝）。
    ``expected_fence`` 不匹配即跳过：过期 worker 不能覆盖新一次派发的裁决。
    """
    owned = db is None
    db = db if db is not None else _fresh_session()
    try:
        operation = (db.query(M.ActionOperation)
                     .filter(M.ActionOperation.id == operation_id)
                     .with_for_update().first())
        if operation is None:
            return
        if (expected_fence is not None
                and operation.fence_token != expected_fence):
            db.rollback()
            return
        if operation.state in ("succeeded", "unknown", "failed_not_executed",
                               "approval_rejected"):
            # 已由更新一次派发结案：不重复覆盖。
            db.rollback()
            return
        confirmation = db.query(M.ActionConfirmation).filter(
            M.ActionConfirmation.id == operation.confirmation_id).first()
        if confirmation is None:
            return
        attempt = db.query(M.ActionExternalAttempt).filter(
            M.ActionExternalAttempt.operation_id == operation.id,
            M.ActionExternalAttempt.effect_ordinal
            == ASSET_WRITE_EFFECT_ORDINAL).first()
        evidence = outcome.to_evidence_dict()
        if outcome.status in (WP.OUTCOME_APPLIED, WP.OUTCOME_PENDING_REVIEW):
            operation.state = "succeeded"
            confirmation.state = "succeeded"
            confirmation.failure_code = None
            if attempt is not None:
                attempt.state = "acknowledged"
        elif outcome.status == WP.OUTCOME_UNKNOWN:
            operation.state = "unknown"
            operation.last_error = "asset_write_commit_unknown"
            operation.reconcile_evidence = {
                "reason": "asset_write_commit_unknown", **evidence}
            confirmation.state = "unknown"
            confirmation.failure_code = "execution_result_unknown"
            if attempt is not None:
                attempt.state = "unknown"
        else:
            operation.state = "failed_not_executed"
            operation.last_error = outcome.error_code or "asset_write_failed"
            confirmation.state = "failed_not_executed"
            confirmation.failure_code = (
                outcome.error_code or "asset_write_failed")
            if attempt is not None:
                attempt.state = "proven_not_applied"
        if attempt is not None:
            attempt.receipt_hash = _sha256(_canonical_json(evidence))
            attempt.evidence_ref = _canonical_json(evidence)
            attempt.finished_at = _now()
        operation.lease_expires_at = None
        db.commit()
    finally:
        if owned:
            db.close()


def operation_has_asset_plan(db: Session, operation_id: str) -> bool:
    """该 operation 是否承载冻结资产写计划（workflow 审批派发路由依据）。

    本地 HITL 确认的 operation 没有 frozen plan，必须保留原引擎执行路径。
    """
    operation = db.query(M.ActionOperation).filter(
        M.ActionOperation.id == operation_id).first()
    if operation is None:
        return False
    confirmation = db.query(M.ActionConfirmation).filter(
        M.ActionConfirmation.id == operation.confirmation_id).first()
    if confirmation is None:
        return False
    return _frozen_plan(confirmation) is not None


def dispatch_approved_asset_write(db: Session, operation_id: str) -> dict:
    """HITL 批准后的资产写派发（action_workflow 决策执行分支调用）。

    与 run_operation_inline 同一顺序语义：claim（pending_approval →
    executing）→ engine_entered → attempt → 单行源写 → 结案。已在途/
    已结案的 operation 幂等返回当前状态，不重复派发；unknown 保守保留。
    返回引擎结果同形 dict（status success/failed、id=None）供审批日志
    finalize 关联。
    """
    operation = (db.query(M.ActionOperation)
                 .filter(M.ActionOperation.id == operation_id)
                 .with_for_update().first())
    if operation is None:
        return {"status": "failed", "id": None,
                "errorMessage": "资产写 operation 不存在"}
    confirmation = db.query(M.ActionConfirmation).filter(
        M.ActionConfirmation.id == operation.confirmation_id).first()
    plan = _frozen_plan(confirmation) if confirmation is not None else None
    if plan is None:
        return {"status": "failed", "id": None,
                "errorMessage": "资产写 operation 缺少冻结计划"}
    try:
        _verify_frozen_payload_integrity(confirmation)
    except ConfirmationError as exc:
        return {"status": "failed", "id": None,
                "errorMessage": f"冻结载荷完整性校验失败：{exc.message}"}
    if operation.state == "pending_approval":
        operation.state = "executing"
        operation.claimed_at = _now()
        operation.lease_expires_at = _now() + timedelta(
            seconds=OPERATION_LEASE_SECONDS)
        operation.worker_ref = f"approval:{uuid_lib.uuid4().hex[:12]}"
        operation.version += 1
        db.commit()
    elif operation.state == "executing":
        lease = _as_utc(operation.lease_expires_at)
        if lease is not None and lease >= _now():
            db.rollback()
            return {"status": "failed", "id": None,
                    "errorMessage": "资产写派发已在途（lease 有效），稍后对账"}
        operation.claimed_at = _now()
        operation.lease_expires_at = _now() + timedelta(
            seconds=OPERATION_LEASE_SECONDS)
        operation.version += 1
        db.commit()
    else:
        # 终态（崩溃后重复批准恢复）：返回已结案状态，不重复派发。
        db.rollback()
        status = "success" if operation.state == "succeeded" else "failed"
        return {"status": status, "id": None,
                "errorMessage": operation.last_error}
    _mark_engine_entered(operation.id, db)
    db.refresh(operation)
    db.refresh(confirmation)
    _invoke_asset_write(db, operation, confirmation, plan)
    db.expire_all()
    operation = db.query(M.ActionOperation).filter(
        M.ActionOperation.id == operation_id).first()
    if operation is not None and operation.state == "succeeded":
        return {"status": "success", "id": None}
    last_error = (operation.last_error if operation is not None else None) \
        or (confirmation.failure_code if confirmation is not None else None)
    if operation is not None and operation.state == "unknown":
        # unknown 不宣称明确失败：审批日志保持可恢复态，真实状态以确认
        # 协议（unknown + 人工裁决正门）为准。
        return {"status": "unknown", "id": None,
                "errorMessage": last_error or "execution_result_unknown",
                "assetUnknown": True}
    return {"status": "failed", "id": None,
            "errorMessage": last_error or "asset_write_failed"}


def mark_asset_operation_rejected(db: Session, operation_id: str) -> None:
    """HITL 拒绝后的资产 operation 结案：源写从未派发，可证明零源变更。"""
    operation = (db.query(M.ActionOperation)
                 .filter(M.ActionOperation.id == operation_id)
                 .with_for_update().first())
    if operation is None or operation.state != "pending_approval":
        if operation is not None:
            db.rollback()
        return
    confirmation = db.query(M.ActionConfirmation).filter(
        M.ActionConfirmation.id == operation.confirmation_id).first()
    plan = _frozen_plan(confirmation) if confirmation is not None else None
    operation.state = "approval_rejected"
    if confirmation is not None:
        confirmation.state = "approval_rejected"
        confirmation.failure_code = "approval_rejected"
    # 审批拒绝时 attempt 可能尚不存在（源写从未派发）：创建一条
    # proven_not_applied 证据行，outcome 真实可见（不宣称源端有过派发）。
    attempt = db.query(M.ActionExternalAttempt).filter(
        M.ActionExternalAttempt.operation_id == operation.id,
        M.ActionExternalAttempt.effect_ordinal
        == ASSET_WRITE_EFFECT_ORDINAL).first()
    evidence = _canonical_json(
        WP.AssetWriteOutcome(
            asset_kind=str((plan or {}).get("assetKind") or "unknown"),
            status=WP.OUTCOME_FAILED,
            operation=str((plan or {}).get("operation") or "update"),
            affected_rows=0,
            dataset_id=str((plan or {}).get("datasetId") or ""),
            error_code="approval_rejected",
            message="动作审批被拒绝，源写从未派发",
        ).to_evidence_dict())
    if attempt is None:
        attempt = M.ActionExternalAttempt(
            operation_id=operation.id,
            effect_ordinal=ASSET_WRITE_EFFECT_ORDINAL,
            stable_delivery_key=(
                f"proposal-write:{operation.confirmation_id}"),
            state="proven_not_applied",
            started_at=_now(),
        )
        db.add(attempt)
    attempt.state = "proven_not_applied"
    attempt.finished_at = _now()
    attempt.evidence_ref = evidence
    operation.lease_expires_at = None
    db.commit()

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


def _reconcile_pending_approval(db, operation, confirmation) -> None:
    from app.ontologies.formal_modeling.models import ActionExecutionLog

    log = db.query(ActionExecutionLog).filter(
        ActionExecutionLog.id == operation.approval_log_id).first()
    if log is None:
        return
    status = str(log.status or "").lower()
    if _frozen_plan(confirmation) is not None:
        # 资产写审批闸门：rejected → approval_rejected（零源变更）；
        # approved 但 operation 仍 pending（决策与派发间崩溃）→ 重新入队由
        # worker 派发；approved 且 operation 已结案 → 状态已由派发路径落定。
        if status == "rejected":
            operation.state = "approval_rejected"
            confirmation.state = "approval_rejected"
            confirmation.failure_code = "approval_rejected"
            return
        if status in ("pending", "executing"):
            return
        if status == "approved" and operation.state == "pending_approval":
            operation.state = "dispatch_queued"
            operation.claimed_at = None
            operation.lease_expires_at = None
            return
        return
    if status in ("pending", "executing"):
        return
    if status == "approved":
        related = db.query(ActionExecutionLog).filter(
            ActionExecutionLog.id == log.related_log_id).first() \
            if log.related_log_id else None
        if related is not None:
            attempts = db.query(M.ActionExternalAttempt).filter(
                M.ActionExternalAttempt.operation_id == operation.id).all()
            unresolved = [a for a in attempts
                          if a.state in ("started", "unknown")]
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
    key = initial_engine_key(confirmation.id)
    key_log = db.query(ActionExecutionLog).filter(
        ActionExecutionLog.ontology_id == confirmation.ontology_id,
        ActionExecutionLog.idempotency_key == key).first()
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
    operation.state = "unknown"
    operation.reconcile_evidence = {
        "reason": "engine_entered_without_terminal_evidence",
        "checkedLogKey": key,
        "attemptStates": _adjudicate_attempts(db, operation, attempts),
    }
    confirmation.state = "unknown"
    confirmation.failure_code = "execution_result_unknown"


def adjudicate_unknown_not_executed(
        db: Session, admin_user, ontology_id: str, cid: str, *,
        operation_id: str, rationale: str,
        evidence_refs: list[str], expected_fence_token: int,
        expected_guard_hash: str, expected_payload_hash: str
) -> M.ActionConfirmation:
    """管理员对 unknown 资产操作的人工 not_executed 裁决（正门）。

    仅接受 unknown 态、operation/confirmation 归属与载荷完整性全部匹配的
    操作；裁决追加写入 reconcile_evidence（不可覆写既有证据），原 attempt
    的 unknown 证据原样保留；不执行任何源写/派发/重放，guard 随
    failed_not_executed 终态按既有逻辑自然释放。
    """
    # 固定双锁顺序：confirmation FOR UPDATE（populate_existing 绕过会话
    # 缓存）→ operation FOR UPDATE。管理员裁决绑定被审快照三项（fence/
    # guard/payload hash），任何失配即 409 且原证据/状态零变化。
    confirmation = (db.query(M.ActionConfirmation)
                    .filter(M.ActionConfirmation.id == cid,
                            M.ActionConfirmation.ontology_id == ontology_id)
                    .with_for_update().populate_existing().first())
    if confirmation is None:
        db.rollback()
        raise ConfirmationError(404, "confirmation_not_found",
                                "确认请求不存在")
    operation = (db.query(M.ActionOperation)
                 .filter(M.ActionOperation.id == operation_id)
                 .with_for_update().populate_existing().first())
    if operation is None or operation.confirmation_id != cid:
        db.rollback()
        raise ConfirmationError(409, "operation_mismatch",
                                "operation 与确认请求不匹配")
    if operation.state != "unknown":
        db.rollback()
        raise ConfirmationError(
            409, "operation_not_unknown",
            f"仅 unknown 态可人工裁决（当前 {operation.state}）")
    if operation.fence_token != expected_fence_token:
        db.rollback()
        raise ConfirmationError(
            409, "adjudication_fence_mismatch",
            "expectedFenceToken 与被审 operation 不一致，拒绝裁决")
    if not hmac.compare_digest(operation.guard_hash, expected_guard_hash):
        db.rollback()
        raise ConfirmationError(
            409, "adjudication_guard_mismatch",
            "expectedGuardHash 与被审 operation 不一致，拒绝裁决")
    if not hmac.compare_digest(confirmation.payload_hash,
                               expected_payload_hash):
        db.rollback()
        raise ConfirmationError(
            409, "adjudication_payload_mismatch",
            "expectedPayloadHash 与被审确认不一致，拒绝裁决")
    try:
        _verify_frozen_payload_integrity(confirmation)
    except ConfirmationError:
        db.rollback()
        raise
    lease = _as_utc(operation.lease_expires_at)
    if lease is not None and lease >= _now():
        db.rollback()
        raise ConfirmationError(
            409, "operation_lease_active",
            "operation 仍有有效执行租约（可能仍在途），先等租约失效或对账")
    adjudication = {
        "adminUserId": str(admin_user.id),
        "decidedAt": _now().isoformat(),
        "rationale": rationale,
        "evidenceRefs": [str(ref) for ref in evidence_refs],
        "previousState": operation.state,
        "adjudicatedFenceToken": operation.fence_token,
        "adjudicatedGuardHash": operation.guard_hash,
        "adjudicatedPayloadHash": confirmation.payload_hash,
    }
    evidence = dict(operation.reconcile_evidence or {})
    history = list(evidence.get("manualAdjudications") or [])
    history.append(adjudication)
    evidence["manualAdjudications"] = history
    operation.reconcile_evidence = evidence
    operation.state = "failed_not_executed"
    operation.last_error = "manual_adjudicated_not_executed"
    operation.lease_expires_at = None
    confirmation.state = "failed_not_executed"
    confirmation.failure_code = "manual_adjudicated_not_executed"
    db.commit()
    db.refresh(confirmation)
    return confirmation


def _asset_outcome_payload(db: Session,
                           confirmation: M.ActionConfirmation) -> Optional[dict]:
    """资产路由确认的公开 outcome（未派发/在途不出现）。

    结案证据来自 attempt 账本（evidence_ref，不含行值/秘密）；curated 的
    pending_review 按审核真实状态只读刷新（approved→applied、rejected→
    failed），刷新绝不重新执行动作。
    """
    if not confirmation.operation_id:
        return None
    if confirmation.state not in (
            "succeeded", "unknown", "failed_not_executed",
            "compensation_required", "approval_rejected"):
        return None
    plan = _frozen_plan(confirmation)
    if plan is None:
        return None
    attempt = db.query(M.ActionExternalAttempt).filter(
        M.ActionExternalAttempt.operation_id == confirmation.operation_id,
        M.ActionExternalAttempt.effect_ordinal
        == ASSET_WRITE_EFFECT_ORDINAL).first()
    if attempt is None or not attempt.evidence_ref:
        return None
    try:
        evidence = json.loads(attempt.evidence_ref)
    except (TypeError, ValueError):
        return None
    if not isinstance(evidence, dict):
        evidence = {}
    if confirmation.failure_code == "manual_adjudicated_not_executed":
        base = {
            "assetKind": str(plan.get("assetKind") or evidence.get(
                "assetKind") or "unknown"),
            "status": WP.OUTCOME_FAILED,
            "operation": str(plan.get("operation")
                             or evidence.get("operation") or "update"),
            "affectedRows": 0,
            "datasetId": str(plan.get("datasetId") or ""),
            "errorCode": "manual_adjudicated_not_executed",
        }
        if evidence.get("reviewId"):
            base["reviewId"] = evidence.get("reviewId")
        if evidence.get("receiptId"):
            base["receiptId"] = evidence.get("receiptId")
        return base
    if (str(plan.get("assetKind") or "") == WP.ASSET_KIND_CURATED
            and confirmation.state == "succeeded"
            and evidence.get("status") == WP.OUTCOME_PENDING_REVIEW
            and WP.write_port_ready()):
        try:
            refreshed = WP.action_write_port().inspect_write(
                db, None, plan, evidence=evidence)
        except WP.WritePortError:
            refreshed = None
        if refreshed is not None:
            return refreshed.to_public_dict()
    return {
        key: evidence[key] for key in (
            "assetKind", "status", "operation", "affectedRows", "datasetId",
            "versionId", "reviewId", "receiptId", "errorCode",
        ) if key in evidence
    }


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
        block_reason = "execution_result_unknown"
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
        "outcome": _asset_outcome_payload(db, confirmation),
        "decidedAt": _as_utc(confirmation.decided_at).isoformat()
        if confirmation.decided_at else None,
        "createdAt": _as_utc(confirmation.created_at).isoformat()
        if confirmation.created_at else None,
    }
