"""Query-view application service — Web-owned execution handle lifecycle.

The chat worker never hands its engine cursor to the browser. A page query is
re-executed from the replayable querySpec here, producing a Web-owned handle;
subsequent pages continue that handle via fetch_unified_query_page. Cursors are
HMAC envelopes around the opaque engine cursor (no offsets, no re-query), and
terminal pages / explicit DELETE release the handle.
"""
from __future__ import annotations

import logging
from sqlalchemy.orm import Session

from app.ontologies.agent_query import schemas as QS
from app.ontologies.agent_query.remote_query import cancel_remote_query
from app.ontologies.agent_query.service import (
    QueryError, execute_unified_query, fetch_unified_query_page,
    release_query_snapshot,
)
from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.models import AgentConversation
from app.ontologies.agent_runtime.query_view_payloads import (
    _consistency, _persistence, _view_payload,
)
from app.ontologies.agent_runtime.query_spec import (
    MAX_QUERY_SPEC_BYTES, apply_drilldown, build_columns, canonical_json,
    make_view_cursor, map_rows, sanitize_spec, scope_from_spec, spec_hash,
    validate_object_scope, validate_spl_bindings, verify_drilldown,
    verify_view_cursor,
)
from app.ontologies.agent_runtime.read_queries import encode_scalar

logger = logging.getLogger(__name__)

VIEW_SESSION_KIND = "ontology_agent_query_view"
class QueryViewError(Exception):
    """Transport-neutral query-view failure."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def _tool_error(exc: ToolError) -> QueryViewError:
    message = str(exc)
    code = message.split(":", 1)[0].strip() or "query_view_invalid"
    if "expired" in code:
        status = 410
    elif "scope_mismatch" in code or "cursor_invalid" in code:
        status = 403
    else:
        status = 422
    return QueryViewError(status, code, message)


def _query_error(exc: QueryError) -> QueryViewError:
    if exc.status == 404 and exc.code == "snapshot_not_found":
        # Web view 句柄在 DELETE/过期后已不存在：对 UI 是刷新信号，不是
        # 普通资源 404。底层 agent_query 契约保持不变。
        return QueryViewError(
            410, "query_snapshot_expired",
            "查询句柄已释放或过期，请刷新查询视图",
            reprepareRequired=True)
    return QueryViewError(exc.status, exc.code, exc.message,
                          **{key: value for key, value in exc.extra.items()
                             if key not in exc.assistant_private_keys})


def _map_exception(exc: Exception) -> QueryViewError:
    if isinstance(exc, QueryViewError):
        return exc
    if isinstance(exc, ToolError):
        return _tool_error(exc)
    if isinstance(exc, QueryError):
        return _query_error(exc)
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "detail", None)
    if status is not None and isinstance(detail, dict):
        return QueryViewError(status, detail.get("code", "query_view_error"),
                              detail.get("message", "查询视图请求失败"))
    if status is not None:
        return QueryViewError(status, "query_view_error", str(detail or exc))
    return QueryViewError(500, "query_view_error", "查询视图请求失败")


def _require_conversation(db: Session, ontology_id: str, conversation_id: str,
                          user) -> AgentConversation:
    conversation = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.ontology_id == ontology_id,
    ).first()
    if conversation is None:
        raise QueryViewError(404, "not_found", "会话不存在")
    if conversation.is_sandbox:
        raise QueryViewError(403, "forbidden", "评估沙箱会话不支持页面查询")
    if (conversation.user_id
            and conversation.user_id != getattr(user, "id", None)
            and getattr(user, "role", "") != "admin"):
        raise QueryViewError(403, "forbidden", "无权访问他人会话")
    if not conversation.ontology_release_id:
        raise QueryViewError(409, "release_required",
                             "该会话没有固定发布版本；请从已发布本体重新发起对话")
    return conversation


def _scope(db: Session, ontology_id: str, conversation: AgentConversation):
    try:
        _, profile, scope = build_scope(
            db, ontology_id, release_id=conversation.ontology_release_id)
    except Exception as exc:  # noqa: BLE001 - release/context errors are domain errors
        error = _map_exception(exc)
        if error.status == 500:
            raise QueryViewError(409, "release_context_changed", str(exc)) from exc
        raise error from exc
    if not profile.enabled:
        raise QueryViewError(403, "profile_disabled", "该本体的智能体已停用")
    if int(profile.max_rows_per_query or 0) <= 0:
        raise QueryViewError(403, "profile_query_disabled",
                             "当前本体助手未启用查询额度")
    return scope


def _request_from_spec(spec: dict, *, scope, page_size: int | None = None):
    if spec.get("shape") == "objects":
        request = QS.QueryRequest.model_validate(spec["query"])
        cap = min(200, int(scope.profile.max_rows_per_query or 0))
        wanted = request.page_size
        if page_size is not None:
            wanted = min(wanted or page_size, page_size)
        if wanted is not None:
            request = request.model_copy(
                update={"page_size": max(1, min(wanted, cap))})
        return request
    page = page_size or spec.get("pageSize")
    if page is not None:
        page = max(1, min(int(page), 200))
    return QS.QueryRequest(
        spl=spec["spl"], semantic_bindings=spec.get("semanticBindings") or {},
        params=spec.get("params") or {}, page_size=page)


def _validate_spec(spec: dict, *, ontology_id: str, release_id: str, scope) -> dict:
    spec = sanitize_spec(spec)
    spec = scope_from_spec(spec, ontology_id=ontology_id, release_id=release_id)
    if len(canonical_json(spec).encode("utf-8")) > MAX_QUERY_SPEC_BYTES:
        raise QueryViewError(422, "query_spec_too_large",
                             "querySpec 超出页面重放预算")
    if spec.get("shape") == "objects":
        validate_object_scope(
            spec, ontology_id=ontology_id, release_id=release_id,
            visible_type_ids=set(scope.object_types))
        if spec.get("drilldownBase") is not None:
            validate_object_scope(
                {**spec, "query": spec["drilldownBase"]},
                ontology_id=ontology_id, release_id=release_id,
                visible_type_ids=set(scope.object_types))
    else:
        spec["semanticBindings"] = validate_spl_bindings(
            spec, ontology_id=ontology_id, release_id=release_id, scope=scope)
    return spec


def _release(db: Session, user, conversation: AgentConversation,
             query_id: str | None, persistence: str) -> bool:
    if not query_id:
        return False
    try:
        if persistence.startswith("remote"):
            cancel_remote_query(db, user, str(query_id))
            return True
        return release_query_snapshot(
            db, user, str(query_id), session_kind=VIEW_SESSION_KIND,
            session_id=conversation.id)
    except Exception:  # noqa: BLE001
        logger.warning("agent query-view release failed: %s", query_id,
                       exc_info=True)
        return False


def handle_query_view(db: Session, ontology_id: str, user, *,
                      conversation_id: str, query_spec: dict | None = None,
                      cursor: str | None = None,
                      drilldown: str | None = None,
                      page_size: int | None = None) -> dict:
    try:
        conversation = _require_conversation(
            db, ontology_id, conversation_id, user)
        scope = _scope(db, ontology_id, conversation)
        ontology_id = str(conversation.ontology_id)
        release_id = str(conversation.ontology_release_id)

        if cursor and drilldown:
            raise QueryViewError(422, "query_view_invalid",
                                 "cursor 与 drilldown 不能同时使用")
        if drilldown and query_spec is None:
            raise QueryViewError(422, "query_view_invalid",
                                 "下钻必须同时提供原始 querySpec")
        if not cursor and not drilldown and query_spec is None:
            raise QueryViewError(422, "query_view_invalid",
                                 "必须提供 querySpec、cursor 或 drilldown")

        if cursor:
            if page_size is not None:
                raise QueryViewError(
                    422, "query_view_invalid",
                    "cursor 续页不能重新指定 pageSize；页长冻结在首个查询")
            payload = verify_view_cursor(
                cursor, owner=str(getattr(user, "id", "")),
                ontology_id=ontology_id, conversation_id=conversation.id)
            if query_spec is None:
                raise QueryViewError(
                    410, "query_spec_required",
                    "续页必须同时携带原始 querySpec；请重新打开该查询视图",
                    reprepareRequired=True)
            spec = _validate_spec(
                query_spec, ontology_id=ontology_id,
                release_id=release_id, scope=scope)
            if spec_hash(spec) != payload.get("specHash"):
                raise QueryViewError(403, "query_scope_mismatch",
                                     "cursor 与 querySpec 不属于同一次查询")
            data = fetch_unified_query_page(
                db, user, payload["engineCursor"],
                session_kind=VIEW_SESSION_KIND, session_id=conversation.id)
            resolved_window = (spec or {}).get("resolvedWindow") \
                or payload.get("resolvedWindow")
            result = _view_payload(
                data=data, spec=spec,
                spec_digest=str(payload.get("specHash")),
                user=user, conversation=conversation, first=False,
                resolved_window=resolved_window)
            if not result["hasNext"]:
                result["released"] = _release(
                    db, user, conversation, data.get("queryId"),
                    payload.get("persistence") or _persistence(data))
            result["querySpec"] = spec
            return result

        if drilldown:
            base_spec = _validate_spec(
                query_spec, ontology_id=ontology_id,
                release_id=release_id, scope=scope)
            payload = verify_drilldown(
                drilldown, owner=str(getattr(user, "id", "")),
                ontology_id=ontology_id,
                conversation_id=conversation.id)
            spec = apply_drilldown(payload, base_spec)
            if spec.get("shape") != "objects":
                raise QueryViewError(422, "query_drilldown_unsupported",
                                     "下钻仅支持结构化计划")
            validate_object_scope(
                spec, ontology_id=ontology_id, release_id=release_id,
                visible_type_ids=set(scope.object_types))
            request = _request_from_spec(spec, scope=scope,
                                         page_size=page_size)
            data = execute_unified_query(
                db, user, request, session_kind=VIEW_SESSION_KIND,
                session_id=conversation.id)
            digest = spec_hash(spec)
            result = _view_payload(
                data=data, spec=spec, spec_digest=digest, user=user,
                conversation=conversation, first=True,
                resolved_window=spec.get("resolvedWindow"))
            result["querySpec"] = spec
            result["drilldown"] = payload.get("description") or {}
            if not result["hasNext"]:
                result["released"] = _release(
                    db, user, conversation, data.get("queryId"),
                    _persistence(data))
            return result

        spec = _validate_spec(
            query_spec, ontology_id=ontology_id,
            release_id=release_id, scope=scope)
        request = _request_from_spec(spec, scope=scope, page_size=page_size)
        data = execute_unified_query(
            db, user, request, session_kind=VIEW_SESSION_KIND,
            session_id=conversation.id)
        digest = spec_hash(spec)
        result = _view_payload(
            data=data, spec=spec, spec_digest=digest, user=user,
            conversation=conversation, first=True,
            resolved_window=spec.get("resolvedWindow"))
        result["querySpec"] = spec
        if not result["hasNext"]:
            result["released"] = _release(
                db, user, conversation, data.get("queryId"), _persistence(data))
        return result
    except QueryViewError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _map_exception(exc) from exc


def delete_view(db: Session, ontology_id: str, user, *,
                conversation_id: str, cursor: str) -> dict:
    try:
        conversation = _require_conversation(
            db, ontology_id, conversation_id, user)
        payload = verify_view_cursor(
            cursor, owner=str(getattr(user, "id", "")),
            ontology_id=str(conversation.ontology_id),
            conversation_id=conversation.id)
        released = _release(
            db, user, conversation, payload.get("queryId"),
            payload.get("persistence") or "snapshot")
        return {"conversationId": conversation.id, "released": bool(released),
                "note": ("已释放当前 Web 查询句柄" if released
                         else "句柄已释放或已过期（幂等）")}
    except QueryViewError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _map_exception(exc) from exc
