"""Published ontology semantics for the shared, non-persistent query port.

The composition root supplies execution; this module owns release/profile/join
checks and never imports a data-channel implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import HTTPException

from app.auth.permissions import user_has_menu_access
from app.ontologies.access import require_ontology_access
from app.ontologies.agent_query import schemas as QS
from app.ontologies.release_context import current_release_context


class RemoteQueryPort(Protocol):
    def execute(self, db, user, plan: dict, *, request: dict,
                session_kind: str, session_id: str) -> dict: ...

    def fetch(self, db, user, token: str, *, session_kind: str,
              session_id: str, prepare) -> dict: ...

    def cancel(self, db, user, query_id: str) -> dict: ...


_port: RemoteQueryPort | None = None


def set_remote_query_port(port: RemoteQueryPort) -> None:
    global _port
    _port = port


def _error(status, code, message, **extra):
    from .service import QueryError
    return QueryError(status, code, message, **extra)


def _installed() -> RemoteQueryPort:
    if _port is None:
        raise _error(503, "remote_query_unavailable", "远程查询执行端口尚未配置")
    return _port


def object_sources(request: QS.QueryRequest):
    for source in request.sources:
        if source.subquery is None:
            yield source
        else:
            yield from object_sources(source.subquery)


def uses_remote_sources(db, request: QS.QueryRequest) -> bool:
    if any(source.subquery is not None for source in request.sources):
        return True
    from app.ontologies.mappings.remote_binding import get_remote_binding

    for source in object_sources(request):
        context = current_release_context(db, source.ontology_id,
                                          expected_release_id=source.release_id)
        if get_remote_binding(context.snapshot, source.object_type_id) is not None:
            return True
    return False


@dataclass
class _PreparedSource:
    descriptor: dict
    lineage: dict[str, tuple[Any, str]]


def prepare_remote_plan(db, user, request: QS.QueryRequest | dict, *, serialized=False):
    parsed = QS.QueryRequest.model_validate(request) if serialized else request
    try:
        from sqlalchemy import select
        from app.auth.models import User
        user = db.execute(select(User).where(User.id == user.id).execution_options(
            populate_existing=True)).scalar_one_or_none()
        plan = _prepare_remote_plan(db, user, parsed)
        return (parsed, plan) if serialized else plan
    finally:
        # Release/profile locks only fence the metadata read. Remote execution
        # may outlive the metadata connection's idle timeout and rechecks this
        # complete plan against a fresh transaction before returning any page.
        if db.in_transaction():
            db.rollback()


def _prepare_remote_plan(db, user, request: QS.QueryRequest):
    from .service import UnifiedQueryService, verify_join_evidence
    from app.ontologies.mappings.remote_binding import get_remote_binding

    if not user or not getattr(user, "is_active", False):
        raise _error(403, "query_access_denied", "当前用户不可查询")
    if not user_has_menu_access(db, user, "agent"):
        raise _error(403, "MENU_ACCESS_DENIED", "当前角色无本体助手查询权限")

    leaves = list(object_sources(request))
    flat_sources = [source.model_copy(update={"alias": f"leaf_{index}"})
                    for index, source in enumerate(leaves)]
    metadata_request = QS.QueryRequest(
        sources=flat_sources[:4],
        select=[QS.SelectItem(source=flat_sources[0].alias,
                              property="_metadata", as_="_metadata")])
    resolver = UnifiedQueryService(db, user, metadata_request,
                                  session_kind="remote_metadata", session_id=str(user.id))
    resolver._ensure_profiles({source.ontology_id for source in leaves})
    # All leaf definitions are locked in one metadata transaction, including
    # leaves nested below a derived source.
    resolver.req = metadata_request.model_copy(update={"sources": flat_sources})
    resolver._pin_transaction()
    resolver._resolve_sources()
    metas = iter(resolver.sources.values())
    releases: dict[str, dict] = {}
    citations: list[dict] = []
    cap = 200

    def compile_level(query: QS.QueryRequest, path: str) -> tuple[dict, dict]:
        nonlocal cap
        aliases = [source.alias for source in query.sources]
        if len(set(aliases)) != len(aliases):
            raise _error(422, "invalid_parameter", "source alias 重复")
        sources: dict[str, _PreparedSource] = {}
        descriptors = []
        for source in query.sources:
            if source.subquery is not None:
                nested, lineage = compile_level(source.subquery, f"{path}{source.alias}.")
                descriptor = {"alias": source.alias, "subquery": nested}
                sources[source.alias] = _PreparedSource(descriptor, lineage)
                descriptors.append(descriptor)
                continue
            meta = next(metas)
            require_ontology_access(db, source.ontology_id, user, write=False)
            context = current_release_context(db, source.ontology_id,
                                              expected_release_id=source.release_id)
            binding = get_remote_binding(context.snapshot, source.object_type_id)
            allowed = {name: definition for name, definition in meta.props.items()
                       if meta.allowed is None or name in meta.allowed}
            page_cap = int(meta.scope.profile.max_rows_per_query or 0)
            if page_cap <= 0:
                raise _error(403, "profile_query_disabled", "本体配置不允许查询")
            cap = min(cap, page_cap)
            if binding is None:
                descriptor = {
                    "alias": source.alias,
                    "binding": {"kind": "ontology", "ontologyId": source.ontology_id,
                                "releaseId": source.release_id,
                                "objectTypeId": source.object_type_id},
                    "propertyMap": {name: name for name in allowed},
                }
            else:
                property_map = {}
                for column, prop in binding["fieldMap"].items():
                    if prop in allowed:
                        if prop in property_map:
                            raise _error(422, "remote_property_ambiguous", "远程属性映射不唯一")
                        property_map[prop] = column
                descriptor = {
                    "alias": source.alias,
                    "binding": {"kind": "integrated", "assetId": binding["assetId"]},
                    "propertyMap": property_map,
                    "schemaFingerprint": binding["schemaFingerprint"],
                    "declaredTypes": {name: definition.get("type", "string")
                                      for name, definition in allowed.items()},
                    "ontology": {"ontologyId": source.ontology_id,
                                 "releaseId": source.release_id,
                                 "objectTypeId": source.object_type_id},
                }
            from .service import _canonical_json, _sha256
            descriptor["profileFingerprint"] = _sha256(_canonical_json({
                "enabled": meta.scope.profile.enabled,
                "objects": meta.scope.profile.allowed_object_type_ids,
                "links": meta.scope.profile.allowed_link_type_ids,
                "properties": meta.scope.profile.allowed_properties,
                "maxRows": page_cap,
            }))
            sources[source.alias] = _PreparedSource(
                descriptor, {name: (meta, name) for name in descriptor["propertyMap"]})
            descriptors.append(descriptor)
            releases[f"{path}{source.alias}"] = {
                "ontologyId": source.ontology_id, "releaseId": source.release_id,
                "objectTypeId": source.object_type_id,
                "dataMode": "remote" if binding else "materialized",
            }
            citations.append({"ontologyId": source.ontology_id,
                              "releaseId": source.release_id,
                              "objectTypeId": source.object_type_id,
                              "sourceAlias": f"{path}{source.alias}"})

        def field(alias, prop):
            if alias not in sources:
                raise _error(422, "invalid_parameter", f"未知 source：{alias}")
            source = sources[alias]
            if "subquery" in source.descriptor:
                outputs = source.descriptor["subquery"]["outputs"]
                if prop not in outputs:
                    raise _error(422, "unknown_parameter", f"未知派生字段：{prop}")
                return prop
            meta = next(iter(source.lineage.values()))[0] if source.lineage else None
            if meta is None:
                raise _error(403, "property_forbidden", "该对象没有授权的映射属性")
            name, _ = resolver._resolve_property(meta, prop)
            if name not in source.descriptor["propertyMap"]:
                raise _error(422, "remote_property_unmapped", f"属性「{name}」没有远程列映射")
            return name

        filter_leaves = 0

        def predicate(node, depth=1):
            nonlocal filter_leaves
            if depth > 4:
                raise _error(422, "invalid_parameter", "过滤表达式超过深度限制")
            if isinstance(node, QS.FilterLeaf):
                filter_leaves += 1
                if filter_leaves > 32:
                    raise _error(422, "invalid_parameter", "过滤条件超过数量限制")
                value = node.model_dump(by_alias=True)
                value["property"] = field(node.source, node.property)
                return value
            if isinstance(node, QS.FilterNot):
                return {"not": predicate(node.not_, depth + 1)}
            if isinstance(node, QS.FilterAnd):
                return {"and": [predicate(child, depth + 1) for child in node.and_]}
            return {"or": [predicate(child, depth + 1) for child in node.or_]}

        source_filters = []
        for source_filter in query.source_filters:
            pred = predicate(source_filter.filter)
            if any(leaf.source != source_filter.source
                   for leaf in resolver._walk_filters([source_filter.filter])):
                raise _error(422, "invalid_parameter", "sourceFilter 只能引用所属 source")
            source_filters.append({"source": source_filter.source, "filter": pred})
        joins = []
        joined = {aliases[0]}
        for join in query.joins:
            if join.left not in joined or join.right in joined or join.right not in sources:
                raise _error(422, "invalid_parameter", "关联必须从已连接的左侧引入一个新右侧 source")
            left, right = sources[join.left], sources[join.right]
            if join.on.kind == "explicit_key":
                lp = field(join.left, join.on.left_property)
                rp = field(join.right, join.on.right_property)
                left_origin = left.lineage.get(lp)
                right_origin = right.lineage.get(rp)
                if left_origin is None or right_origin is None:
                    raise _error(422, "join_semantics_unverified", "关联键必须可追溯到发布属性，不能关联聚合值")
                left_meta, left_prop = left_origin
                right_meta, right_prop = right_origin
                evidence_join = join.model_copy(update={"on": join.on.model_copy(update={
                    "left_property": left_prop, "right_property": right_prop})})
                evidence = verify_join_evidence(db, user, evidence_join, left_meta, right_meta)
                cardinality = join.on.cardinality
                key_type = join.on.key_type
            else:
                if "subquery" in left.descriptor or "subquery" in right.descriptor:
                    raise _error(422, "invalid_parameter", "声明关系需要对象源；派生键关联使用可核验的业务键证据")
                left_meta = next(iter(left.lineage.values()))[0]
                right_meta = next(iter(right.lineage.values()))[0]
                lp, rp, cardinality = _declared_keys(db, join, left_meta, right_meta,
                                                     left.descriptor, right.descriptor)
                field(join.left, lp)
                field(join.right, rp)
                key_type = None
                evidence = {"categories": ["published_remote_relation"],
                            "verifiedRefs": [f"relation:{left_meta.req.release_id}:{join.on.link_type_id}"]}
            joins.append({"left": join.left, "right": join.right, "type": join.type,
                          "leftProperty": lp, "rightProperty": rp,
                          "cardinality": cardinality, "keyType": key_type,
                          "evidence": evidence})
            joined.add(join.right)
        if joined != set(aliases):
            raise _error(422, "invalid_parameter", "查询包含未关联的 source")
        select = []
        lineage = {}
        for item in query.select:
            prop = field(item.source, item.property)
            select.append({"source": item.source, "property": prop, "as": item.as_})
            if prop in sources[item.source].lineage:
                lineage[item.as_] = sources[item.source].lineage[prop]
        groups = [{"source": item.source, "property": field(item.source, item.property)}
                  for item in query.group_by]
        aggregates = []
        for item in query.aggregates:
            if item.fn == "count":
                if item.source is not None or item.property is not None:
                    raise _error(422, "invalid_parameter", "count(*) 不可指定 source/property")
            elif not item.source or not item.property:
                raise _error(422, "invalid_parameter", "聚合缺少字段")
            aggregates.append({"fn": item.fn, "source": item.source,
                               "property": field(item.source, item.property) if item.property else None,
                               "as": item.as_})
        outputs = [item["as"] for item in select + aggregates]
        if not outputs or len(set(outputs)) != len(outputs):
            raise _error(422, "invalid_parameter", "输出不能为空或重名")
        group_keys = {(item["source"], item["property"]) for item in groups}
        select_keys = {(item["source"], item["property"]) for item in select}
        if groups or aggregates:
            if group_keys != select_keys:
                raise _error(422, "invalid_parameter", "聚合只允许输出已声明分组列")
        if query.distinct and (groups or aggregates):
            raise _error(422, "invalid_parameter", "distinct 不可与分组聚合并用")
        if any(item.output not in outputs for item in query.order_by):
            raise _error(422, "invalid_parameter", "排序引用未知输出")
        plan = {"sources": descriptors, "sourceFilters": source_filters,
                "where": [predicate(node) for node in query.where], "joins": joins,
                "select": select, "groupBy": groups, "aggregates": aggregates,
                "distinct": query.distinct,
                "orderBy": [item.model_dump(by_alias=True) for item in query.order_by],
                "outputs": outputs}
        return plan, lineage

    plan, _ = compile_level(request, "")
    page_size = request.page_size or min(50, cap)
    if page_size > cap:
        raise _error(422, "invalid_parameter", f"pageSize 超出授权上限 {cap}")
    plan.update(pageSize=page_size, releases=releases, citations=citations)
    return plan


def _declared_keys(db, join, left, right, left_descriptor, right_descriptor):
    if (left.req.ontology_id != right.req.ontology_id
            or left.req.release_id != right.req.release_id):
        raise _error(422, "invalid_parameter", "声明关系必须属于同一本体发布版本")
    link = left.scope.require_link_type(join.on.link_type_id)
    forward = join.on.direction == "source_to_target"
    src, tgt = (left, right) if forward else (right, left)
    if src.ot.id != link.source_object_type_id or tgt.ot.id != link.target_object_type_id:
        raise _error(422, "invalid_parameter", "声明关系端点与对象源不符")
    context = current_release_context(db, left.req.ontology_id,
                                      expected_release_id=left.req.release_id)
    from app.ontologies.mappings.remote_binding import (
        get_remote_binding, snapshot_link_binding_mode,
    )
    sb = get_remote_binding(context.snapshot, src.ot.id)
    tb = get_remote_binding(context.snapshot, tgt.ot.id)
    if sb is None or tb is None:
        raise _error(422, "remote_relation_unsupported", "混合查询的声明关系需要远程键映射；本地关系仍走原本体查询")
    mappings = [item for item in context.snapshot.get("linkMappings", [])
                if snapshot_link_binding_mode(item) == "remote"
                and item.get("linkTypeId") == link.id
                and item.get("srcDatasetId") == sb["datasetId"]
                and item.get("tgtDatasetId") == tb["datasetId"]]
    if len(mappings) != 1 or mappings[0].get("edgeDatasetId"):
        raise _error(422, "remote_relation_unmapped", "声明关系缺少唯一的已发布直接键映射")
    mapping = mappings[0]
    try:
        sp = sb["fieldMap"][mapping["srcKey"]]
        tp = tb["fieldMap"][mapping["tgtKey"]]
    except KeyError as exc:
        raise _error(422, "remote_relation_unmapped", "声明关系键未映射到发布属性") from exc
    cardinality = {"one-to-one": "1:1", "one-to-many": "1:N",
                   "many-to-one": "N:1", "many-to-many": "N:M"}.get(link.cardinality)
    if cardinality is None:
        raise _error(422, "invalid_parameter", "声明关系基数无效")
    if forward:
        return sp, tp, cardinality
    return tp, sp, {"1:N": "N:1", "N:1": "1:N"}.get(cardinality, cardinality)


def _port_error(exc):
    from app.ontologies.agent_runtime.boundary import ToolError

    if isinstance(exc, ToolError):
        return _error(403, "capability_forbidden", str(exc))
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return _error(exc.status_code, detail.get("code", "remote_query_rejected"),
                      detail.get("message", "远程查询已拒绝"))
    if all(hasattr(exc, key) for key in ("status", "code", "message")):
        return _error(exc.status, exc.code, exc.message, **getattr(exc, "extra", {}))
    from app.ontologies.mappings.remote_binding import RemoteBindingError
    if isinstance(exc, RemoteBindingError):
        return _error(422, exc.code, exc.message, **exc.context)
    raise exc


def execute_remote_query(db, user, request, *, session_kind, session_id):
    result = None
    try:
        plan = prepare_remote_plan(db, user, request)
        result = _installed().execute(db, user, plan, request=request.model_dump(by_alias=True),
                                      session_kind=session_kind, session_id=session_id)
        from .service import _canonical_json, _sha256
        current = prepare_remote_plan(db, user, request)
        if _sha256(_canonical_json(current)) != _sha256(_canonical_json(plan)):
            raise _error(409, "query_context_changed", "查询期间发布范围或授权配置发生变化")
        return result
    except Exception as exc:
        if result is not None:
            _installed().cancel(db, user, result["queryId"])
        from .service import QueryError
        if isinstance(exc, QueryError):
            raise
        raise _port_error(exc) from exc
    finally:
        if db.in_transaction():
            db.rollback()


def fetch_remote_page(db, user, token, *, session_kind, session_id):
    try:
        return _installed().fetch(db, user, token, session_kind=session_kind,
                                  session_id=session_id, prepare=prepare_remote_plan)
    except Exception as exc:
        from .service import QueryError
        if isinstance(exc, QueryError):
            raise
        raise _port_error(exc) from exc
