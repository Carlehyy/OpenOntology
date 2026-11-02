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

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ontologies.agent_confirmation import models as CM
from app.ontologies.agent_confirmation import service as confirmation_service
from app.ontologies.agent_confirmation.schemas import PrepareProposalRequest
from app.ontologies.agent_query import schemas as QS
from app.ontologies.agent_query.remote_query import cancel_remote_query
from app.ontologies.agent_query.service import (
    QueryError, execute_unified_query, fetch_unified_query_page,
    release_query_snapshot,
)
from app.ontologies.agent_runtime.structure_service import (
    StructureError, build_structure,
)

logger = logging.getLogger(__name__)

SUPER_ASSISTANT_ORIGIN = "super_assistant"

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200
_QUERY_TREE_ARGUMENT_KEYS = (
    "sources", "joins", "sourceFilters", "where", "select", "groupBy",
    "aggregates", "orderBy", "distinct",
)
_CURSOR_INCOMPATIBLE_ARGUMENT_KEYS = (
    "pageSize", "params", "semanticBindings", "queryId",
)

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
    from fastapi import HTTPException

    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return FacadeError(exc.status_code, detail.get("code", "ontology_request_rejected"),
                           detail.get("message", "本体请求已拒绝"),
                           **{key: value for key, value in detail.items()
                              if key not in {"code", "message"}})
    return FacadeError(500, "facade_error", str(exc))


def catalog_search(db: Session, user, *, query=None, cursor=None) -> dict:
    from app.ontologies.agent_runtime.catalog import catalog_search as search
    try:
        return search(db, user, query=query, cursor=cursor)
    except Exception as exc:
        raise _wrap(exc) from exc


def catalog_object_types(db: Session, user, *, ontology_id, release_id=None,
                         cursor=None) -> dict:
    from app.ontologies.agent_runtime.catalog import catalog_object_types as object_types
    try:
        return object_types(db, user, ontology_id=ontology_id,
                            release_id=release_id, cursor=cursor)
    except Exception as exc:
        raise _wrap(exc) from exc


def catalog_get_document(db: Session, user, *, ontology_id, release_id=None,
                         cursor=None) -> dict:
    from app.ontologies.agent_runtime.catalog import catalog_get_document as get_document
    try:
        return get_document(db, user, ontology_id=ontology_id,
                            release_id=release_id, cursor=cursor)
    except Exception as exc:
        raise _wrap(exc) from exc


def query_limits(db: Session, ontology_id: str, *,
                 release_id: Optional[str] = None) -> dict:
    """助手专属查询限制元数据（公共直查 structure 响应不包含）。

    声明上限仍是 200；有效 ``maxPageSize`` 取 profile 上限与 200 的最小值，
    ``defaultPageSize`` 为 min(50, maxPageSize)。``enabled=False`` 只表示
    profile 停用或上限失效（此时查询仍按既有 403 profile_query_disabled
    拒绝）；``maxRowsPerQuery`` 的负值与本体的执行语义一致归一为 0，不返回
    负 cap，也不改变 profile 持久值。本函数不改变任何授权/发布校验。
    """
    from app.ontologies.agent_runtime.boundary import build_scope
    from app.ontologies.release_context import current_release_context

    context = current_release_context(
        db, ontology_id, expected_release_id=release_id)
    _, profile, _ = build_scope(db, ontology_id,
                                release_id=context.release.id)
    raw_page_size = int(profile.max_rows_per_query or 0)
    max_page_size = max(0, min(raw_page_size, _MAX_PAGE_SIZE))
    return {
        "enabled": bool(profile.enabled and max_page_size > 0),
        "maxPageSize": max_page_size,
        "defaultPageSize": min(_DEFAULT_PAGE_SIZE, max_page_size),
    }


def _release_terminal_page(db: Session, user, data: dict, *,
                           session_kind: str, session_id: str) -> dict:
    """终页资源回收 — 只在助手门面、仅严格 ``hasNext is False`` 触发。

    - 本地快照按 owner + 会话显式删除；远程 memory handle 复用既有
      ``cancel_remote_query``（``cancelled=false`` 视为已不存在，不抛错）。
    - 中间页 ``hasNext=true``、缺失 ``hasNext`` 与直查 HTTP 路径一律不释放。
    - best-effort：释放失败只记 warning 并标 ``released=false``，原
      ``expiresAt`` 不变，成功查询结果绝不因回收失败而失败。
    """
    if not isinstance(data, dict) or data.get("hasNext") is not False:
        return data
    query_id = data.get("queryId")
    if not query_id:
        return data
    query_id = str(query_id)
    released = False
    try:
        if isinstance(data.get("persistence"), dict):
            # 远程联邦响应带 ephemeral 标记：handle 在内存注册表里。
            cancel_remote_query(db, user, query_id)
            released = True
        else:
            released = release_query_snapshot(
                db, user, query_id,
                session_kind=session_kind, session_id=session_id)
            if not released:
                # 非本地快照 → 远程 memory handle（替代端口可能不带标记）。
                cancel_remote_query(db, user, query_id)
                released = True
    except Exception as exc:  # noqa: BLE001 - 释放不得破坏成功查询
        logger.warning("assistant facade terminal release failed (%s)",
                       type(exc).__name__)
        released = False
    data["released"] = released
    if not released:
        return data
    released_at = datetime.now(timezone.utc).isoformat()
    data["releasedAt"] = released_at
    data["expiresAt"] = released_at
    persistence = data.get("persistence")
    if isinstance(persistence, dict):
        reference = persistence.get("reference")
        if isinstance(reference, dict) and "expiresAt" in reference:
            # 只同步已有值，不新增 reference 键。
            reference["expiresAt"] = released_at
    return data


def structure(db: Session, user, ontology_id: str, *, release_id: Optional[str],
              object_type_ids: Optional[list[str]] = None,
              link_type_ids: Optional[list[str]] = None,
              action_ids: Optional[list[str]] = None) -> dict:
    try:
        data = build_structure(db, user, ontology_id,
                               expected_release_id=release_id,
                               object_type_ids=object_type_ids,
                               link_type_ids=link_type_ids,
                               action_ids=action_ids)
        data["queryLimits"] = query_limits(db, ontology_id,
                                           release_id=release_id)
        return data
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def query(db: Session, user, request: QS.QueryRequest, *,
          session_kind: str, session_id: str) -> dict:
    try:
        data = execute_unified_query(db, user, request,
                                     session_kind=session_kind,
                                     session_id=session_id,
                                     include_page_size=True)
        return _release_terminal_page(db, user, data,
                                      session_kind=session_kind,
                                      session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def query_page(db: Session, user, cursor: str, *,
               session_kind: str, session_id: str) -> dict:
    try:
        data = fetch_unified_query_page(db, user, cursor,
                                        session_kind=session_kind,
                                        session_id=session_id,
                                        include_page_size=True)
        return _release_terminal_page(db, user, data,
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
                   target_ref: Optional[dict] = None,
                   parameters: Optional[dict] = None,
                   parameter_refs: Optional[dict] = None) -> dict:
    from app.ontologies.agent_confirmation.schemas import TargetRef

    target_ref_model = None
    if target_ref is not None:
        target_ref_model = TargetRef.model_validate(target_ref)
    body = PrepareProposalRequest(
        session_kind=session_kind,
        session_id=session_id,
        client_submission_id=source_call_id,
        action_id=action_id,
        release_id=release_id,
        target_instance_id=target_instance_id,
        target_ref=target_ref_model,
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
                root_intent_id, action_id, target_instance_id,
                target_ref=target_ref_model))
        return confirmation_service.confirmation_out(db, confirmation,
                                                    replayed=replayed)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def _assistant_action_slot(root_intent_id: str, action_id: str,
                           target_instance_id: Optional[str],
                           *, target_ref=None) -> str:
    import hashlib

    if target_instance_id is None and target_ref is not None:
        key_token = "|".join(
            f"{name}={target_ref.key.get(name)}"
            for name in sorted(target_ref.key or {}))
        target_token = f"ref:{target_ref.object_type_id}:{key_token}"
    else:
        target_token = target_instance_id or ""
    return hashlib.sha256(
        f"{root_intent_id}|{action_id}|{target_token}".encode(
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


def _argument_present(arguments: dict, key: str) -> bool:
    return key in arguments and arguments[key] is not None


def _invalid_parameter(message: str, *, field: Optional[str] = None,
                       **extra) -> FacadeError:
    if field:
        extra["field"] = field
    return FacadeError(422, "invalid_parameter", message, **extra)


def _validate_page_size_argument(arguments: dict) -> None:
    """schema 级 pageSize 校验：0/201/非整数显式拒绝，不静默夹值。

    声明上限仍是 200；profile 有效 cap 的更小越界由查询核/远程 plan 给出
    ``requestedPageSize/maxPageSize/defaultPageSize``。错误不回显输入原值。
    """
    if not _argument_present(arguments, "pageSize"):
        return
    value = arguments["pageSize"]
    if (isinstance(value, bool) or not isinstance(value, int)
            or not 1 <= value <= _MAX_PAGE_SIZE):
        raise _invalid_parameter(
            f"pageSize 必须是 1 到 {_MAX_PAGE_SIZE} 的整数",
            field="pageSize", maxPageSize=_MAX_PAGE_SIZE)


def _wrap_validation_error(exc: Exception) -> FacadeError:
    """把请求模型/手工解析异常归一为可纠正 422，不回显 Pydantic input。"""
    field = None
    if isinstance(exc, ValidationError):
        try:
            errors = exc.errors()
        except Exception:  # noqa: BLE001
            errors = []
        if errors:
            loc = [str(part) for part in (errors[0].get("loc") or ())
                   if part != "__root__"]
            field = ".".join(loc) or None
    extra = {}
    if field and field.split(".")[0] == "pageSize":
        extra["maxPageSize"] = _MAX_PAGE_SIZE
    if isinstance(exc, KeyError):
        missing = exc.args[0] if exc.args else None
        if isinstance(missing, str):
            field = field or missing
    return _invalid_parameter(
        "查询参数不合法：字段类型或取值不符合查询契约",
        field=field, **extra)


def build_query_request(arguments: dict):
    """把工具参数（camelCase dict）解析为统一查询请求对象。"""
    if not isinstance(arguments, dict):
        raise _invalid_parameter("查询参数必须是对象")
    # Some compatible tool endpoints mistake a JSON-schema $defs name for an
    # outer argument envelope. Unwrap only a sole, repository-owned definition
    # name; the inner payload still goes through the strict request models.
    if len(arguments) == 1:
        for envelope in ("queryRequestMid", "queryRequestInner"):
            inner = arguments.get(envelope)
            if isinstance(inner, dict):
                arguments = inner
                break
    cursor_value = arguments.get("cursor")
    if "cursor" in arguments and cursor_value is not None:
        if arguments.get("spl"):
            raise FacadeError(422, "invalid_request_shape",
                              "cursor 与 spl 不能同时出现：续查只传 cursor，"
                              "新 SPL 查询只传 spl+semanticBindings")
        tree_conflicts = sorted(
            key for key in _QUERY_TREE_ARGUMENT_KEYS
            if _argument_present(arguments, key)
            and arguments[key] not in (None, [], {}, False))
        if tree_conflicts:
            raise FacadeError(422, "invalid_request_shape",
                              "cursor 续查不能同时携带结构树字段："
                              f"{', '.join(tree_conflicts)}；续查只传 cursor")
        cursor_conflicts = [
            key for key in _CURSOR_INCOMPATIBLE_ARGUMENT_KEYS
            if _argument_present(arguments, key)]
        if cursor_conflicts:
            # 只回显参数名（不含值），并给出唯一可纠正动作。
            raise _invalid_parameter(
                "cursor 续查不能同时携带其他查询参数："
                f"{', '.join(cursor_conflicts)}；续查只传 cursor",
                field=cursor_conflicts[0])
        # 续查 cursor 必须是已签发的字符串 token；短/超长/非字符串等模型
        # 校验失败统一受控为 invalid_cursor（不回显 token、长度或原值），
        # 不让裸 ValidationError 落到工具入口的 ontology_tool_failed。
        if not isinstance(cursor_value, str):
            raise FacadeError(400, "invalid_cursor", "cursor 格式非法")
        try:
            return QS.CursorRequest(cursor=cursor_value)
        except ValidationError as exc:
            raise FacadeError(400, "invalid_cursor",
                              "cursor 格式非法") from exc
    _validate_page_size_argument(arguments)
    try:
        if arguments.get("spl"):
            # SPL 文本形态：spl + semanticBindings 三元组 + params/pageSize。
            # 与结构树字段互斥——静默丢弃任何一半都会绕过请求模型自身的
            # 互斥校验，这里显式拒绝混形。
            tree_keys = ("sources", "joins", "sourceFilters", "where",
                         "select", "groupBy", "aggregates", "orderBy",
                         "distinct", "cursor")
            conflicting = sorted(
                key for key in tree_keys
                if arguments.get(key) not in (None, [], {}, False))
            if conflicting:
                raise FacadeError(
                    422, "invalid_request_shape",
                    "spl 文本请求不能同时携带结构树字段："
                    f"{', '.join(conflicting)}；两种形态互斥")
            payload = {"spl": str(arguments["spl"])}
            if arguments.get("semanticBindings"):
                payload["semanticBindings"] = dict(
                    arguments["semanticBindings"])
            if arguments.get("params"):
                payload["params"] = dict(arguments["params"])
            if _argument_present(arguments, "pageSize"):
                payload["pageSize"] = arguments["pageSize"]
            return QS.QueryRequest.model_validate(payload)
        sources = [QS.QuerySource.model_validate(source)
                   for source in arguments.get("sources") or []]
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
                    evidence_refs=[str(e)
                                   for e in on.get("evidenceRefs") or []])
            else:
                on_obj = QS.DeclaredRelationOn(
                    kind="declared_relation",
                    link_type_id=str(on["linkTypeId"]),
                    direction=str(on.get("direction") or "source_to_target"))
            joins.append(QS.QueryJoin(
                left=str(j["left"]), right=str(j["right"]),
                type=str(j.get("type") or "inner"), on=on_obj))
        return QS.QueryRequest(
            sources=sources,
            joins=joins,
            source_filters=_parse_source_filters(
                arguments.get("sourceFilters")),
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
    except FacadeError:
        raise
    except (ValidationError, KeyError, TypeError, ValueError,
            AttributeError) as exc:
        raise _wrap_validation_error(exc) from exc


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
    """当前发布比较入口（只读元数据）：ontology → 当前 release id。

    仅供跨轮结构引用判断“是否已过期/漂移”；**不构成授权**：不校验也不
    授予本体访问权，不替代 structure/query 的发布与授权校验。调用方必须
    先经门面完成访问校验，历史引用不能作为授权或查询构造依据。
    """
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
