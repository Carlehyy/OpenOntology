"""受限调用链的唯一本体能力门面 — ``super_assistant/ontology_tools.py`` 的
唯一允许依赖边（架构守卫白名单精确到本模块）。

“用不改”边界在这里落实：
- super_assistant 来源的 principal 固定只允许 discover/structure/query/
  action.prepare/action.confirmed_execute 五类能力；
- 任何定义管理（新建/修改本体、类型/属性/关系/动作/规则、profile、发布/
  回滚/导入）没有入口 — 不是靠 prompt 提醒，而是根本没有方法可调；
- 写路径只接受 confirmationId：模型/委派文字不能代替用户确认。
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.ontologies.agent_confirmation import models as CM
from app.ontologies.agent_confirmation import service as confirmation_service
from app.ontologies.agent_confirmation.schemas import PrepareProposalRequest
from app.ontologies.agent_query import schemas as QS
from app.ontologies.agent_query.service import (
    QueryError, execute_unified_query, fetch_unified_query_page,
)
from app.ontologies.agent_runtime.structure_service import (
    StructureError, build_structure,
)

SUPER_ASSISTANT_ORIGIN = "super_assistant"

# 受限 principal 的固定能力集合（任何子委派只能做交集，不能扩大）
PERMITTED_OPERATIONS = (
    "ontology.discover",
    "ontology.structure",
    "ontology.query",
    "ontology.action.prepare",
    "ontology.action.confirmed_execute",
)


class FacadeError(Exception):
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


def _wrap(exc: Exception) -> FacadeError:
    if isinstance(exc, (QueryError, StructureError,
                        confirmation_service.ConfirmationError)):
        return FacadeError(exc.status, exc.code, exc.message, **exc.extra)
    return FacadeError(500, "facade_error", str(exc))


def structure(db: Session, user, ontology_id: str, *, release_id: Optional[str],
              object_type_ids: Optional[list[str]] = None,
              link_type_ids: Optional[list[str]] = None,
              action_ids: Optional[list[str]] = None) -> dict:
    try:
        return build_structure(db, user, ontology_id,
                               expected_release_id=release_id,
                               object_type_ids=object_type_ids,
                               link_type_ids=link_type_ids,
                               action_ids=action_ids)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def query(db: Session, user, request: QS.QueryRequest, *,
          session_kind: str, session_id: str) -> dict:
    try:
        return execute_unified_query(db, user, request,
                                     session_kind=session_kind,
                                     session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def query_page(db: Session, user, cursor: str, *,
               session_kind: str, session_id: str) -> dict:
    try:
        return fetch_unified_query_page(db, user, cursor,
                                        session_kind=session_kind,
                                        session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def register_join_assertion(db: Session, user, body: dict) -> dict:
    """登记当前用户显式声明的跨本体关联含义（evidenceRefs 的 user_asserted 源）。

    校验语义与 direct HTTP 入口（proposals_router）一致：两端结构必须存在
    于对应本体的当前发布且属性已声明。businessMeaning 是用户确认过的关联
    含义 —— 调用方（工具层）必须先经用户确认（tool-run approve）再调用；
    本门面不做确认，模型不能自授。声明只是使用依据，不是治理认证。
    """
    from app.ontologies.agent_runtime.boundary import ToolError, build_scope
    from app.ontologies.release_context import current_release_context

    payload = QS.JoinAssertionCreate.model_validate(body)
    for ontology_ref, object_type_ref, prop_ref in (
            (payload.left_ontology_id, payload.left_object_type_id,
             payload.left_property),
            (payload.right_ontology_id, payload.right_object_type_id,
             payload.right_property)):
        context = current_release_context(db, ontology_ref)
        _, _, scope = build_scope(db, ontology_ref,
                                  release_id=context.release.id)
        try:
            ot = scope.require_object_type(object_type_ref)
            scope.resolve_property(ot, prop_ref)
        except ToolError as exc:
            raise FacadeError(422, "invalid_parameter", str(exc)) from exc
    from app.ontologies.agent_query.models import AgentJoinAssertion

    assertion = AgentJoinAssertion(
        created_by=str(user.id),
        left_ontology_id=payload.left_ontology_id,
        left_object_type_id=payload.left_object_type_id,
        left_property=payload.left_property,
        right_ontology_id=payload.right_ontology_id,
        right_object_type_id=payload.right_object_type_id,
        right_property=payload.right_property,
        key_type=payload.key_type,
        business_meaning=payload.business_meaning)
    db.add(assertion)
    db.commit()
    return {
        "id": assertion.id,
        "evidenceRef": f"assertion:{assertion.id}",
        "category": "user_asserted",
        "keyType": assertion.key_type,
        "businessMeaning": assertion.business_meaning,
        "createdAt": assertion.created_at.isoformat()
        if assertion.created_at else None,
    }


def prepare_action(db: Session, user, ontology_id: str, *,
                   session_kind: str, session_id: str,
                   root_intent_id: str, source_call_id: str,
                   action_id: str, release_id: str,
                   target_instance_id: Optional[str] = None,
                   parameters: Optional[dict] = None,
                   parameter_refs: Optional[dict] = None) -> dict:
    body = PrepareProposalRequest(
        session_kind=session_kind,
        session_id=session_id,
        client_submission_id=source_call_id,
        action_id=action_id,
        release_id=release_id,
        target_instance_id=target_instance_id,
        parameters=parameters or {},
        parameter_refs=parameter_refs or {},
    )
    try:
        confirmation, replayed = confirmation_service.prepare(
            db, user, ontology_id, body,
            origin=SUPER_ASSISTANT_ORIGIN,
            source_call_id=source_call_id,
            root_intent_id=root_intent_id,
            action_request_slot=_assistant_action_slot(
                root_intent_id, action_id, target_instance_id))
        return confirmation_service.confirmation_out(db, confirmation,
                                                    replayed=replayed)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def _assistant_action_slot(root_intent_id: str, action_id: str,
                           target_instance_id: Optional[str]) -> str:
    import hashlib

    return hashlib.sha256(
        f"{root_intent_id}|{action_id}|{target_instance_id or ''}".encode(
            "utf-8")).hexdigest()


def confirmation_status(db: Session, user, ontology_id: str, cid: str, *,
                        session_kind: str, session_id: str) -> dict:
    try:
        confirmation = confirmation_service.get_status(
            db, user, ontology_id, cid, session_kind, session_id)
        return confirmation_service.confirmation_out(db, confirmation)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def decide_confirmation(db: Session, user, ontology_id: str, cid: str,
                        decision: str, *, session_kind: str, session_id: str,
                        decision_id: str, payload_hash: str) -> dict:
    """direct 入口的 approve/deny：映射到同一确认协议（唯一用户确认）。"""
    try:
        confirmation = confirmation_service.decide(
            db, user, ontology_id, cid, decision, decision_id, payload_hash,
            session_kind, session_id)
        return confirmation_service.confirmation_out(db, confirmation)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def execute_confirmation(db: Session, user, ontology_id: str, cid: str, *,
                         session_kind: str, session_id: str) -> dict:
    """唤醒已批准操作 — 只派发，绝不代 approve。"""
    try:
        confirmation = confirmation_service.execute(
            db, user, ontology_id, cid, session_kind, session_id)
        return confirmation_service.confirmation_out(db, confirmation)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc

def build_query_request(arguments: dict):
    """把工具参数（camelCase dict）解析为统一查询请求对象。"""
    if arguments.get("cursor"):
        return QS.CursorRequest(cursor=str(arguments["cursor"]))
    sources = [QS.QuerySource(
        alias=str(s["alias"]), ontology_id=str(s["ontologyId"]),
        release_id=str(s["releaseId"]),
        object_type_id=str(s["objectTypeId"]))
        for s in arguments.get("sources") or []]
    joins = []
    for j in arguments.get("joins") or []:
        on = dict(j.get("on") or {})
        if on.get("kind") == "explicit_key":
            on_obj = QS.ExplicitKeyOn(
                kind="explicit_key",
                left_property=str(on["leftProperty"]),
                right_property=str(on["rightProperty"]),
                key_type=str(on["keyType"]),
                cardinality=str(on["cardinality"]),
                business_meaning=str(on["businessMeaning"]),
                evidence_refs=[str(e) for e in on.get("evidenceRefs") or []])
        else:
            on_obj = QS.DeclaredRelationOn(
                kind="declared_relation",
                link_type_id=str(on["linkTypeId"]),
                direction=str(on.get("direction") or "source_to_target"))
        joins.append(QS.QueryJoin(left=str(j["left"]), right=str(j["right"]),
                                  type=str(j.get("type") or "inner"),
                                  on=on_obj))
    return QS.QueryRequest(
        sources=sources,
        joins=joins,
        source_filters=_parse_source_filters(arguments.get("sourceFilters")),
        where=[_parse_filter_node(node)
               for node in arguments.get("where") or []],
        select=[QS.SelectItem(source=str(s["source"]),
                              property=str(s["property"]),
                              **{"as": str(s["as"])})
                for s in arguments.get("select") or []],
        group_by=[QS.GroupByItem(source=str(g["source"]),
                                 property=str(g["property"]))
                  for g in arguments.get("groupBy") or []],
        aggregates=[QS.AggregateItem(
            fn=str(a["fn"]), source=a.get("source"),
            property=a.get("property"), **{"as": str(a["as"])})
            for a in arguments.get("aggregates") or []],
        distinct=bool(arguments.get("distinct")),
        order_by=[QS.OrderByItem(
            output=str(o["output"]),
            direction=str(o.get("direction") or "asc"),
            nulls=str(o.get("nulls") or "last"))
            for o in arguments.get("orderBy") or []],
        page_size=arguments.get("pageSize"),
    )


def _parse_filter_node(raw):
    if not isinstance(raw, dict):
        raise FacadeError(422, "invalid_parameter", "过滤节点必须是对象")
    if "and" in raw:
        return QS.FilterAnd(**{"and": [_parse_filter_node(n)
                                       for n in raw["and"] or []]})
    if "or" in raw:
        return QS.FilterOr(**{"or": [_parse_filter_node(n)
                                     for n in raw["or"] or []]})
    if "not" in raw:
        return QS.FilterNot(**{"not": _parse_filter_node(raw["not"])})
    return QS.FilterLeaf(
        source=str(raw.get("source") or ""),
        property=str(raw.get("property") or ""),
        op=str(raw.get("op") or ""),
        value=raw.get("value"))


def _parse_source_filters(raw_list):
    if not raw_list:
        return []
    return [QS.SourceFilter(source=str(sf.get("source") or ""),
                            filter=_parse_filter_node(sf.get("filter")))
            for sf in raw_list]


def confirmation_owner_lookup(db: Session, user, cid: str) -> str:
    """按确认 id 反查本体（owner 过滤）；不存在返回空串。"""
    from app.ontologies.agent_confirmation import models as CM

    row = db.query(CM.ActionConfirmation).filter(
        CM.ActionConfirmation.id == cid,
        CM.ActionConfirmation.actor_user_id == str(user.id)).first()
    return row.ontology_id if row is not None else ""


def current_release_map(db: Session, ontology_ids: list[str]) -> dict[str, Any]:
    """KG 来源状态核对用：ontology → 当前 release（只读元数据）。"""
    from app.models.ontology import OntologyProject

    rows = db.query(OntologyProject).filter(
        OntologyProject.id.in_(ontology_ids)).all()
    return {row.id: row.current_release_id for row in rows}


def reconcile_confirmation(db: Session, user, ontology_id: str, cid: str, *,
                           session_kind: str, session_id: str) -> dict:
    """推进确认对账（审批后技术执行/租约过期恢复）并返回真实状态。"""
    try:
        confirmation = confirmation_service.get_status(
            db, user, ontology_id, cid, session_kind, session_id)
        if confirmation.operation_id:
            confirmation_service.reconcile_operation(
                confirmation.operation_id, db)
        refreshed = confirmation_service.get_status(
            db, user, ontology_id, cid, session_kind, session_id)
        return confirmation_service.confirmation_out(db, refreshed)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
