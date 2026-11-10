"""MCP 本体只读工具服务层 — 结构发现 / 结构树查询 / 续页。

授权与查询沿 agent 菜单 + current_release_context + build_scope +
execute_unified_query 正门；schema 只暴露对象/属性/关系与受限业务
说明；终页回收快照/远程句柄。平台侧不运行 LLM。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth.permissions import user_has_menu_access
from app.ontologies.access import require_ontology_access
from app.ontologies.agent_query import schemas as QS
from app.ontologies.agent_query.service import (
    QueryError,
    execute_unified_query,
    fetch_unified_query_page,
    release_query_snapshot,
)
from app.ontologies.agent_query.remote_query import cancel_remote_query
from app.ontologies.agent_runtime.boundary import (
    ToolError,
    build_scope,
    remote_object_type_ids,
)
from app.ontologies.agent_runtime.models import AgentProfile
from app.ontologies.agent_runtime.structure_service import StructureError
from app.ontologies.agent_runtime.query_planning import _visible_properties
from app.ontologies.mcp.schemas import (
    McpPageInput,
    McpQueryInput,
    McpQueryTree,
    McpSchemaInput,
)
from app.ontologies.release_context import current_release_context
from app.ontologies.versions.models import OntologyVersion
from app.shared.config import settings

MCP_SESSION_KIND = "mcp"
# pi 客户端对工具文本输出有 20KB 截断；单页内容预算 16KB 并预留信封/
# queryGuide/游标开销，使整页序列化后仍稳定低于 20KB。
MCP_SCHEMA_PAGE_BUDGET_BYTES = 16 * 1024
_ENVELOPE_RESERVE_BYTES = 1024
MCP_SCHEMA_CURSOR_TTL_SECONDS = 300
_MCP_DEFAULT_PAGE_SIZE = 50
_REREAD_GUIDANCE = (
    "当前发布版本已变化：请重新调用 ontology_schema（可不带参数）读取"
    "当前版本结构，再以新的 releaseId 重试")
_SCHEMA_TOOL = "ontology_schema"
_QUERY_TOOL = "ontology_query"
_PAGE_TOOL = "ontology_query_page"


class McpError(Exception):
    """MCP 工具面可识别错误 — 协议层用 ``status``/``detail()`` 映射输出。"""

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


# ------------------------------------------------------------- 会话与守卫

def mcp_session_id(user, ontology_id: str) -> str:
    """查询快照会话身份：绑定 user+ontology，跨入口/跨本体 cursor 即拒绝。"""
    return f"{MCP_SESSION_KIND}:{getattr(user, 'id', '')}:{ontology_id}"


def _require_active_user_with_menu(db: Session, user) -> None:
    if (not user or not getattr(user, "is_active", False)
            or not user_has_menu_access(db, user, "agent")):
        raise McpError(403, "MENU_ACCESS_DENIED", "当前用户无本体助手权限")


def authorize_request(db: Session, user, ontology_id: str) -> dict:
    """MCP 会话初始化只读守卫：不改库、不建 profile、不提交。"""
    try:
        _require_active_user_with_menu(db, user)
        require_ontology_access(db, ontology_id, user, write=False)
        context = current_release_context(db, ontology_id)
        profile = db.query(AgentProfile).filter(
            AgentProfile.ontology_id == ontology_id).first()
        if profile is not None and not profile.enabled:
            raise McpError(403, "profile_disabled", "该本体助手未启用")
        return {
            "ontologyId": ontology_id,
            "releaseId": context.release.id,
            "versionNumber": context.release.version_number,
            "sessionKind": MCP_SESSION_KIND,
            "sessionId": mcp_session_id(user, ontology_id),
        }
    except Exception as exc:  # noqa: BLE001 - 统一映射为 McpError
        raise _wrap_error(exc, db=db, ontology_id=ontology_id) from exc

def _current_release_pointer(db: Session, ontology_id: str) -> dict:
    from app.ontologies.projects.models import OntologyProject

    row = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).first()
    if row is None or not row.current_release_id:
        return {}
    version = db.query(OntologyVersion.version_number).filter(
        OntologyVersion.id == row.current_release_id).first()
    return {"currentReleaseId": row.current_release_id,
            "currentVersionNumber": str(version[0]) if version else None}


def _enrich_release_drift(db: Session, extra: dict,
                          ontology_id: str | None) -> dict:
    """409 release_context_changed 附当前指针与重读指引（仅限已授权本体）。"""
    enriched = dict(extra)
    if ontology_id:
        pointer = _current_release_pointer(db, ontology_id)
        enriched.setdefault("currentReleaseId", pointer.get("currentReleaseId"))
        enriched.setdefault("currentVersionNumber",
                            pointer.get("currentVersionNumber"))
    enriched.setdefault("guidance", _REREAD_GUIDANCE)
    return enriched


def _wrap_error(exc: Exception, *, db: Session | None = None,
                ontology_id: str | None = None) -> McpError:
    if isinstance(exc, McpError):
        return exc
    if isinstance(exc, QueryError):
        extras = {key: value for key, value in exc.detail().items()
                  if key not in ("code", "message")}
        if exc.code == "release_context_changed" and db is not None:
            extras = _enrich_release_drift(db, extras, ontology_id)
        return McpError(exc.status, exc.code, exc.message, **extras)
    if isinstance(exc, ToolError):
        return McpError(403, "capability_forbidden", str(exc))
    if isinstance(exc, StructureError):
        return McpError(exc.status, exc.code, exc.message, **exc.extra)
    if isinstance(exc, ValidationError):
        return McpError(422, "invalid_input", str(exc))
    if isinstance(exc, HTTPException):
        detail = exc.detail
        payload = detail if isinstance(detail, dict) else {"message": str(detail)}
        code = str(payload.get("code") or "request_rejected")
        message = str(payload.get("message") or payload)
        extra = {k: v for k, v in payload.items()
                 if k not in ("code", "message")}
        if code == "release_context_changed":
            if db is not None:
                extra = _enrich_release_drift(db, extra, ontology_id)
            extra.setdefault("guidance", _REREAD_GUIDANCE)
            message = "当前发布版本已变化，请求已拒绝；请重读 schema 后重试"
        return McpError(exc.status_code, code, message, **extra)
    return McpError(500, "mcp_internal_error",
                    f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------- schema 分页

def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _sign(payload: dict) -> str:
    raw = _canonical(payload).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(settings.secret_key.encode("utf-8"), raw,
                         hashlib.sha256).hexdigest()
    return body + "." + signature


def _verify_schema_cursor(token: str, user, ontology_id: str) -> dict:
    try:
        body, signature = token.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        expected = hmac.new(settings.secret_key.encode("utf-8"), raw,
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature mismatch")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload not a dict")
        if (payload.get("owner") != str(getattr(user, "id", ""))
                or payload.get("v") != 1
                or payload.get("kind") != "mcp_schema"
                or payload.get("ontology") != ontology_id):
            raise ValueError("cursor scope mismatch")
        if type(payload.get("exp")) is not int or payload["exp"] <= time.time():
            raise McpError(410, "schema_cursor_expired",
                           "schema 游标已过期，请从第一页重新读取")
        return payload
    except McpError:
        raise
    except (ValueError, TypeError, KeyError, binascii.Error,
            UnicodeError) as exc:
        raise McpError(400, "invalid_schema_cursor",
                       "schema 游标无效或不属于当前用户/本体") from exc


def _scope_fingerprint(profile) -> str:
    """授权指纹（对象/链接/属性 allowlist）——权限变化即游标失效。"""
    material = {
        "objects": profile.allowed_object_type_ids,
        "links": profile.allowed_link_type_ids,
        "properties": getattr(profile, "allowed_properties", None),
    }
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def _entry_bytes(entry) -> int:
    return len(_canonical(entry).encode("utf-8"))


def _usable_budget() -> int:
    return MCP_SCHEMA_PAGE_BUDGET_BYTES - _ENVELOPE_RESERVE_BYTES


def _declared_property_count(scope, ot) -> int:
    allowed = scope.allowed_property_names(ot.id)
    return sum(1 for p in (ot.properties or [])
               if isinstance(p, dict) and p.get("name")
               and (allowed is None or p["name"] in allowed))


def _type_data_access(scope, context, ot) -> dict:
    from app.ontologies.mappings.remote_binding import get_remote_binding

    binding = get_remote_binding(context.snapshot, ot.id)
    if binding is None:
        return {"mode": "materialized"}
    return {"mode": "remote",
            "mappedProperties": sorted(_visible_properties(scope, ot))}


def _query_guide(context) -> str | None:
    """确定性 Markdown 查询说明（可保存）。

    只拼装固定模板 + 有界字段（releaseId/versionNumber），不嵌入任意
    长度的 documentTitle/本体名 —— 正常情况下必然落入预算；防御性
    超限时返回 None，由调用方显式标记 queryGuideOmitted。
    """
    guide = (
        f"# 本体查询说明（{context.release.version_number}）\n\n"
        "- 工具链：ontology_schema（结构与续页）→ ontology_query（结构树"
        "查询，releaseId 必填当前值）→ ontology_query_page（cursor 续页）。\n"
        f"- 当前 releaseId：{context.release.id}"
        f"（versionNumber {context.release.version_number}）。\n"
        "- sources 只写 alias+objectTypeId（+subquery），本体与发布身份由"
        "服务端注入；对象/属性/关系以 ontology_schema 输出为准，越界即拒。\n"
        "- 业务说明以各对象类型/属性/关系的 description 字段为准。")
    if _entry_bytes(guide) > _ENVELOPE_RESERVE_BYTES - 384:
        return None
    return guide


def _schema_overview_page(scope, context, *, after_type: str) -> dict:
    remote_ids = remote_object_type_ids(scope)
    types = sorted(scope.object_types.values(), key=lambda t: t.id)
    remaining = [t for t in types if t.id > after_type]
    budget = _usable_budget()
    page: list[dict] = []
    total = 0
    for ot in remaining:
        entry = {
            "objectTypeId": ot.id,
            "name": ot.name,
            "displayName": ot.display_name,
            "description": ot.description,
            "primaryKey": ot.primary_key,
            "propertyCount": _declared_property_count(scope, ot),
            "instanceMode": "remote" if ot.id in remote_ids else "materialized",
        }
        size = _entry_bytes(entry)
        if total + size > budget:
            if not page:
                raise McpError(
                    422, "schema_item_exceeds_page_budget",
                    f"对象类型「{ot.display_name}」摘要单项 "
                    f"{size} 字节超过单页 {MCP_SCHEMA_PAGE_BUDGET_BYTES} "
                    "字节预算；请治理该类型的 description 长度",
                    itemKind="objectType", itemId=ot.id, itemBytes=size)
            break
        page.append(entry)
        total += size
    has_next = len(page) < len(remaining)
    return {"objectTypes": page, "objectTypeCount": len(types),
            "hasNext": has_next,
            "afterType": page[-1]["objectTypeId"] if page else after_type}


def _schema_type_page(scope, context, *, type_ref: str, after_property: str,
                      after_relation: str,
                      relations_started: bool) -> dict:
    ot = scope.require_object_type(type_ref)
    allowed = scope.allowed_property_names(ot.id)
    queryable = scope.queryable_property_names(ot)
    definitions = sorted(
        (p for p in (ot.properties or [])
         if isinstance(p, dict) and p.get("name")
         and (allowed is None or p["name"] in allowed)),
        key=lambda p: str(p["name"]))
    relations = sorted(
        (lt for lt in scope.link_types.values()
         if lt.source_object_type_id == ot.id
         or lt.target_object_type_id == ot.id),
        key=lambda lt: lt.id)
    budget = _usable_budget()

    header = {
        "objectTypeId": ot.id,
        "name": ot.name,
        "displayName": ot.display_name,
        "description": ot.description,
        "primaryKey": ot.primary_key,
        "dataAccess": _type_data_access(scope, context, ot),
    }
    total = _entry_bytes(header)
    if total > budget:
        raise McpError(
            422, "schema_item_exceeds_page_budget",
            f"对象类型「{ot.display_name}」基本信息单项 {total} 字节"
            f"超过单页 {MCP_SCHEMA_PAGE_BUDGET_BYTES} 字节预算",
            itemKind="objectType", itemId=ot.id, itemBytes=total)

    def relation_entry(lt) -> dict:
        if lt.source_object_type_id == ot.id:
            other_id, direction = lt.target_object_type_id, "outgoing"
        else:
            other_id, direction = lt.source_object_type_id, "incoming"
        other = scope.object_types.get(other_id)
        return {
            "linkTypeId": lt.id,
            "name": lt.name,
            "displayName": lt.display_name,
            "description": lt.description,
            "cardinality": lt.cardinality,
            "direction": direction,
            "otherObjectTypeId": other_id,
            "otherObjectType": (other.display_name if other is not None
                                else None),
        }

    props: list[dict] = []
    emitted_relations: list[dict] = []
    prop_stream = [p for p in definitions
                   if not relations_started and str(p["name"]) > after_property]
    for prop in prop_stream:
        name = str(prop["name"])
        entry = {
            "name": name,
            "displayName": prop.get("displayName") or name,
            "type": str(prop.get("type") or "string"),
            "required": bool(prop.get("required")),
            "description": prop.get("description"),
            "source": "computed" if prop.get("source") == "computed"
            else "stored",
        }
        if queryable is None:
            entry["queryable"] = True
        else:
            entry["queryable"] = name in queryable
            if not entry["queryable"]:
                entry["unqueryableReason"] = "remote_property_unmapped"
        size = _entry_bytes(entry)
        if total + size > budget:
            if not props and not emitted_relations:
                raise McpError(
                    422, "schema_item_exceeds_page_budget",
                    f"属性「{name}」单项 {size} 字节超过单页 "
                    f"{MCP_SCHEMA_PAGE_BUDGET_BYTES} 字节预算；请治理该属性"
                    "的 description 长度",
                    itemKind="property", itemId=name, itemBytes=size)
            break
        props.append(entry)
        total += size

    properties_pending = len(props) < len(prop_stream)
    relation_candidates = (
        [r for r in relations if r.id > after_relation]
        if relations_started else list(relations))
    if not properties_pending:
        # 属性全部入页后才开始关系段；关系按 id keyset 推进（afterRelation）。
        for lt in relation_candidates:
            entry = relation_entry(lt)
            size = _entry_bytes(entry)
            if total + size > budget:
                if not props and not emitted_relations:
                    raise McpError(
                        422, "schema_item_exceeds_page_budget",
                        f"关系「{lt.display_name}」单项 {size} 字节超过单页 "
                        f"{MCP_SCHEMA_PAGE_BUDGET_BYTES} 字节预算",
                        itemKind="linkType", itemId=lt.id, itemBytes=size)
                break
            emitted_relations.append(entry)
            total += size

    relations_pending = properties_pending or (
        len(emitted_relations) < len(relation_candidates))
    return {
        "objectType": header,
        "properties": props,
        "propertyCount": len(definitions),
        "relations": emitted_relations,
        "relationCount": len(relations),
        "hasNext": relations_pending,
        "afterProperty": (props[-1]["name"] if props else after_property),
        "afterRelation": (emitted_relations[-1]["linkTypeId"]
                          if emitted_relations else after_relation),
        "relationsStarted": not properties_pending,
    }


def describe_schema(db: Session, user, ontology_id: str,
                    payload: dict | None) -> dict:
    """ontology_schema：无参 bootstrap 当前版本对象目录，按类型/预算分页。"""
    try:
        args = McpSchemaInput.model_validate(payload or {})
        _require_active_user_with_menu(db, user)
        require_ontology_access(db, ontology_id, user, write=False)
        cursor_payload = None
        if args.cursor:
            cursor_payload = _verify_schema_cursor(args.cursor, user,
                                                   ontology_id)
            if args.release_id is not None and \
                    args.release_id != cursor_payload.get("release"):
                # 显式 releaseId 与游标钉定的 release 不一致：拒绝跨版本续页。
                raise McpError(
                    400, "invalid_schema_cursor",
                    "releaseId 与 schema 游标钉定的发布版本不一致；"
                    "请从第一页重新读取")
        expected_release = (cursor_payload or {}).get("release") \
            if cursor_payload else args.release_id
        context = current_release_context(
            db, ontology_id, expected_release_id=expected_release)
        _, profile, scope = build_scope(
            db, ontology_id, release_id=context.release.id)
        if not profile.enabled:
            raise McpError(403, "profile_disabled", "该本体助手未启用")
        if cursor_payload is not None and cursor_payload.get("scope") != \
                _scope_fingerprint(profile):
            raise McpError(
                409, "schema_context_changed",
                "对象/属性授权范围已变化，请从第一页重新读取 schema")

        if cursor_payload is not None:
            mode = cursor_payload.get("mode")
        elif args.object_type_id:
            mode = "type"
        else:
            mode = "overview"
        if mode == "type":
            type_ref = (cursor_payload or {}).get("typeId") \
                or args.object_type_id
            if not type_ref:
                raise McpError(422, "invalid_input",
                               "type 模式需要 objectTypeId")
            body = _schema_type_page(
                scope, context, type_ref=type_ref,
                after_property=(cursor_payload or {}).get("afterProperty")
                or "",
                after_relation=(cursor_payload or {}).get("afterRelation")
                or "",
                relations_started=bool(
                    (cursor_payload or {}).get("relationsStarted")))
            next_cursor_payload = {
                "mode": "type", "typeId": type_ref,
                "afterProperty": body.pop("afterProperty"),
                "afterRelation": body.pop("afterRelation"),
                "relationsStarted": body.pop("relationsStarted")}
        else:
            body = _schema_overview_page(
                scope, context,
                after_type=(cursor_payload or {}).get("afterType") or "")
            next_cursor_payload = {
                "mode": "overview", "afterType": body.pop("afterType")}
            if not cursor_payload:
                # 首页附确定性 Markdown 查询说明（可保存形式）；防御性
                # 超限时显式标记省略，绝不静默缺失。
                guide = _query_guide(context)
                if guide is not None:
                    body["queryGuide"] = guide
                else:
                    body["queryGuideOmitted"] = True

        cursor = None
        if body.get("hasNext"):
            next_cursor_payload.update({
                "v": 1, "kind": "mcp_schema",
                "owner": str(getattr(user, "id", "")),
                "ontology": ontology_id,
                "release": context.release.id,
                "scope": _scope_fingerprint(profile),
                "exp": int(time.time()) + MCP_SCHEMA_CURSOR_TTL_SECONDS,
            })
            cursor = _sign(next_cursor_payload)
        return {
            "ontologyId": ontology_id,
            "releaseId": context.release.id,
            "versionNumber": context.release.version_number,
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "authority": "current_release_pointer",
            "pageBytes": _entry_bytes(body),
            "pageBudgetBytes": MCP_SCHEMA_PAGE_BUDGET_BYTES,
            "cursor": cursor,
            **body,
        }
    except Exception as exc:  # noqa: BLE001 - 统一映射为 McpError
        raise _wrap_error(exc, db=db, ontology_id=ontology_id) from exc


# -------------------------------------------------------------- query 装配

def _assemble_request(tree: McpQueryTree, *, ontology_id: str,
                      release_id: str, cap: int) -> QS.QueryRequest:
    """McpQueryTree → QS.QueryRequest：源身份全部由服务端注入。"""
    page_size = tree.page_size
    if page_size is None:
        page_size = min(_MCP_DEFAULT_PAGE_SIZE, cap)
    else:
        page_size = min(page_size, cap)
    sources = []
    for source in tree.sources:
        if source.subquery is not None:
            sources.append(QS.QuerySource(
                alias=source.alias,
                subquery=_assemble_request(
                    source.subquery, ontology_id=ontology_id,
                    release_id=release_id, cap=cap)))
        else:
            sources.append(QS.QuerySource(
                alias=source.alias, ontology_id=ontology_id,
                release_id=release_id,
                object_type_id=source.object_type_id))
    return QS.QueryRequest(
        sources=sources, joins=tree.joins,
        source_filters=tree.source_filters, where=tree.where,
        select=tree.select, group_by=tree.group_by,
        aggregates=tree.aggregates, distinct=tree.distinct,
        order_by=tree.order_by, page_size=page_size,
        bounded_stats=tree.bounded_stats)


def _assert_sources_pinned(request: QS.QueryRequest, ontology_id: str,
                           release_id: str) -> None:
    """纵深防御：注入后的请求所有叶子源必须钉在绑定本体+固定 release。"""
    for source in request.sources:
        if source.subquery is not None:
            _assert_sources_pinned(source.subquery, ontology_id, release_id)
        elif (source.ontology_id != ontology_id
                or source.release_id != release_id):
            raise McpError(
                403, "mcp_source_scope_violated",
                f"查询源「{source.alias}」不属于绑定的本体或当前发布；"
                "MCP 会话只允许查询绑定的本体")


def _enrich_release_metadata(db: Session, data: dict,
                             ontology_id: str) -> None:
    """按 release_id 精确行查回填 versionNumber（不可变行，非当前指针）。"""
    releases = data.get("releases")
    if not isinstance(releases, dict):
        return
    release_ids = {entry.get("releaseId") for entry in releases.values()
                   if isinstance(entry, dict) and entry.get("releaseId")}
    version_by_id: dict[str, str] = {}
    if release_ids:
        rows = db.query(OntologyVersion.id, OntologyVersion.version_number)\
            .filter(OntologyVersion.id.in_(release_ids)).all()
        version_by_id = {str(row[0]): str(row[1]) for row in rows}
    for alias, entry in releases.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("ontologyId") not in (None, ontology_id):
            raise McpError(
                409, "mcp_release_scope_violated",
                f"结果源「{alias}」不属于绑定的本体，已拒绝返回",
                sourceAlias=alias)
        if not entry.get("versionNumber"):
            version = version_by_id.get(str(entry.get("releaseId")))
            if version:
                entry["versionNumber"] = version


def _release_terminal_page(db: Session, user, session_id: str,
                           data: dict) -> None:
    """终页回收：hasNext=false 时释放本地快照/远程句柄，best-effort。

    判定只认既有契约标记：``persistence`` 为 dict → 远程句柄取消；
    否则为本地快照，按 owner+会话显式删除（不属于本会话即不动）。
    """
    if data.get("hasNext") is not False or not data.get("queryId"):
        return
    query_id = str(data["queryId"])
    released = False
    try:
        if isinstance(data.get("persistence"), dict):
            cancel_remote_query(db, user, query_id)
            released = True
        else:
            released = release_query_snapshot(
                db, user, query_id, session_kind=MCP_SESSION_KIND,
                session_id=session_id)
    except Exception:  # noqa: BLE001 - 回收失败不破坏成功查询
        released = False
    data["released"] = released
    if released:
        released_at = datetime.now(timezone.utc).isoformat()
        data["releasedAt"] = released_at
        data["expiresAt"] = released_at


def _query_envelope(data: dict, ontology_id: str) -> dict:
    releases = data.get("releases") if isinstance(
        data.get("releases"), dict) else {}
    primary = releases.get("s0")
    if not isinstance(primary, dict):
        primary = next((entry for entry in releases.values()
                        if isinstance(entry, dict)
                        and entry.get("ontologyId") == ontology_id), None)
    has_next = bool(data.get("hasNext"))
    return {
        "ontologyId": ontology_id,
        "releaseId": primary.get("releaseId") if primary else None,
        "versionNumber": primary.get("versionNumber") if primary else None,
        "observedAt": data.get("observedAt"),
        "queryId": data.get("queryId"),
        "schemaVersion": data.get("schemaVersion"),
        "releases": releases,
        "citations": data.get("citations"),
        "columns": data.get("columns"),
        "rows": data.get("rows") or [],
        "returned": len(data.get("rows") or []),
        "totalRowCount": data.get("totalRowCount"),
        "rowCounting": data.get("rowCounting"),
        "distinctEntityCounts": data.get("distinctEntityCounts"),
        "inputCounts": data.get("inputCounts"),
        "joinRowCounts": data.get("joinRowCounts"),
        "joinEvidence": data.get("joinEvidence"),
        "partial": bool(data.get("partial")),
        "truncated": bool(data.get("truncated")),
        "exact": data.get("exact") is not False,
        "completeness": data.get("completeness", "complete"),
        "pageSize": data.get("pageSize"),
        "hasNext": has_next,
        "cursor": data.get("cursor") if has_next else None,
        "expiresAt": data.get("expiresAt"),
        "released": data.get("released"),
        "releasedAt": data.get("releasedAt"),
    }


def run_query(db: Session, user, ontology_id: str,
              payload: dict | None) -> dict:
    """ontology_query：结构树查询（releaseId 必填，先读 schema）。"""
    try:
        args = McpQueryInput.model_validate(payload or {})
        _require_active_user_with_menu(db, user)
        require_ontology_access(db, ontology_id, user, write=False)
        context = current_release_context(
            db, ontology_id, expected_release_id=args.release_id)
        _, profile, _ = build_scope(
            db, ontology_id, release_id=context.release.id)
        if not profile.enabled:
            raise McpError(403, "profile_disabled", "该本体助手未启用")
        cap = min(QS.MAX_PAGE_SIZE, int(profile.max_rows_per_query or 0))
        if cap <= 0:
            raise McpError(403, "profile_query_disabled",
                           "当前本体助手未启用查询额度")
        request = _assemble_request(
            args.query, ontology_id=ontology_id,
            release_id=context.release.id, cap=cap)
        _assert_sources_pinned(request, ontology_id, context.release.id)
        session_id = mcp_session_id(user, ontology_id)
        data = execute_unified_query(
            db, user, request, session_kind=MCP_SESSION_KIND,
            session_id=session_id, include_page_size=True)
        _enrich_release_metadata(db, data, ontology_id)
        _release_terminal_page(db, user, session_id, data)
        return _query_envelope(data, ontology_id)
    except Exception as exc:  # noqa: BLE001 - 统一映射为 McpError
        raise _wrap_error(exc, db=db, ontology_id=ontology_id) from exc


def fetch_query_page(db: Session, user, ontology_id: str,
                     payload: dict | None) -> dict:
    """ontology_query_page：续页；会话绑定校验在既有 cursor 链内完成。"""
    try:
        args = McpPageInput.model_validate(payload or {})
        _require_active_user_with_menu(db, user)
        require_ontology_access(db, ontology_id, user, write=False)
        session_id = mcp_session_id(user, ontology_id)
        data = fetch_unified_query_page(
            db, user, args.cursor, session_kind=MCP_SESSION_KIND,
            session_id=session_id, include_page_size=True)
        _enrich_release_metadata(db, data, ontology_id)
        _release_terminal_page(db, user, session_id, data)
        return _query_envelope(data, ontology_id)
    except Exception as exc:  # noqa: BLE001 - 统一映射为 McpError
        raise _wrap_error(exc, db=db, ontology_id=ontology_id) from exc


# --------------------------------------------------------------- 工具清单

_SCHEMA_DESCRIPTION = (
    "读取绑定本体的当前发布版本结构：无参数返回当前 release（releaseId/"
    "versionNumber/observedAt）、确定性查询说明与对象类型目录页（每页有"
    "字节预算，hasNext/cursor 续页）；传 objectTypeId 返回该类型完整属性与"
    "关系（按预算分页）。只包含对象/属性/关系与受限业务说明，不含动作/函数/"
    "哨兵/关联声明。可选 releaseId 作为期望版本：与当前发布不一致时返回 409 "
    "release_context_changed（附 currentReleaseId/currentVersionNumber），"
    "应重新无参调用本工具后再查询。")

_QUERY_DESCRIPTION = (
    "对绑定本体执行结构树查询（明细/过滤/排序/分组/聚合/去重/同本体声明"
    "关系 join/派生子查询）。releaseId 必填：先无参调用 ontology_schema "
    "获取当前 releaseId；release 变化会得到 409 并需重读 schema。树字段与"
    "统一查询契约一致（sources 至多 8 个、select 至多 32 项、pageSize 至多"
    "200 且受本体配置上限约束）；源只写 alias+objectTypeId（或 subquery），"
    "本体与发布身份由服务端注入，跨本体源不可表达。返回 rows/columns/"
    "releases（versionNumber 按实际使用的 release 精确标注）/hasNext/cursor。")

_PAGE_DESCRIPTION = (
    "用 ontology_query 返回的 cursor 续读下一页。cursor 绑定用户、本体与"
    "MCP 会话：跨入口/跨本体/过期/授权变化的 cursor 会被拒绝（404/410/409），"
    "需重新执行 ontology_query。读到终页（hasNext=false）时服务端会释放"
    "查询快照/远程句柄。")

_TOOL_HANDLERS = {
    _SCHEMA_TOOL: describe_schema,
    _QUERY_TOOL: run_query,
    _PAGE_TOOL: fetch_query_page,
}


def tool_manifests() -> list[dict]:
    """MCP tools/list 清单：inputSchema 由 pydantic 精确生成（extra=forbid）。"""
    return [
        {
            "name": _SCHEMA_TOOL,
            "description": _SCHEMA_DESCRIPTION,
            "inputSchema": McpSchemaInput.model_json_schema(),
        },
        {
            "name": _QUERY_TOOL,
            "description": _QUERY_DESCRIPTION,
            "inputSchema": McpQueryInput.model_json_schema(),
        },
        {
            "name": _PAGE_TOOL,
            "description": _PAGE_DESCRIPTION,
            "inputSchema": McpPageInput.model_json_schema(),
        },
    ]


def run_tool(db: Session, user, ontology_id: str, tool_name: str,
             arguments: dict | None) -> dict:
    """工具分发入口：协议层认证并钉定本体后调用。"""
    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        raise McpError(404, "unknown_tool",
                       f"未知 MCP 工具「{tool_name}」；可用："
                       + "、".join(sorted(_TOOL_HANDLERS)))
    return handler(db, user, ontology_id, arguments)
