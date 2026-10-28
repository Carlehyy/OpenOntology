"""Translate an authorized semantic plan into the existing typed query core.

This adapter is assembled by main. It does not import the ontology domain.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from app.data_channel.query.contracts import (
    Binding, DerivedSourceSpec, JoinConstraint, QueryError, TypedQueryRequest,
)
from app.data_channel.query.parser import Command, Expr, Pipeline, Ref, Value
from app.data_channel.query.service import QueryService
from app.shared.config import settings


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def build_typed_request(plan: dict, *, request: dict, session_kind: str,
                        session_id: str) -> TypedQueryRequest:
    bindings, derived, expectations, join_constraints = {}, {}, {}, {}
    counter = 0

    def name():
        nonlocal counter
        counter += 1
        return f"s{counter}"

    def predicate(node, aliases):
        if "and" in node:
            return Expr("and", tuple(predicate(item, aliases) for item in node["and"]))
        if "or" in node:
            return Expr("or", tuple(predicate(item, aliases) for item in node["or"]))
        if "not" in node:
            return Expr("not", (predicate(node["not"], aliases),))
        ref = Ref(aliases[node["source"]], node["property"])
        op = {"eq": "=", "neq": "!=", "gt": ">", "gte": ">=",
              "lt": "<", "lte": "<="}.get(node["op"], node["op"])
        if op in {"is_null", "is_not_null"}:
            return Expr(op, (ref,))
        if op == "in":
            value = node.get("value")
            if not isinstance(value, list) or not 1 <= len(value) <= 100:
                raise QueryError(422, "invalid_parameter", "IN requires 1..100 values")
            return Expr("in", (ref, *(Value(item) for item in value)))
        return Expr(op, (ref, Value(node.get("value"))))

    def level(current):
        aliases = {}
        for source in current["sources"]:
            public_alias = source["alias"]
            resolved = name()
            aliases[public_alias] = resolved
            if "subquery" in source:
                nested = level(source["subquery"])
                derived[resolved] = DerivedSourceSpec(
                    alias=resolved, source=nested.source, pipeline=nested, params={})
            else:
                leaf = name()
                bindings[leaf] = Binding.model_validate(source["binding"])
                if source.get("schemaFingerprint"):
                    expectations[leaf] = {
                        "schemaFingerprint": source["schemaFingerprint"],
                        "declaredTypes": source.get("declaredTypes", {}),
                        "propertyMap": source["propertyMap"],
                    }
                fields = tuple((Ref(leaf, physical), semantic)
                               for semantic, physical in source["propertyMap"].items())
                if not fields:
                    raise QueryError(403, "property_forbidden", "No authorized mapped fields")
                derived[resolved] = DerivedSourceSpec(
                    alias=resolved, source=leaf,
                    pipeline=Pipeline(leaf, leaf, (Command("fields", fields),)), params={})
            prefilters = [item["filter"] for item in current["sourceFilters"]
                          if item["source"] == public_alias]
            if prefilters:
                filtered = name()
                inputs = tuple((Ref(resolved, field), field) for field in (
                    source["subquery"]["outputs"] if "subquery" in source
                    else source["propertyMap"].keys()))
                commands = tuple(Command("where", (predicate(item, {public_alias: resolved}),))
                                 for item in prefilters) + (Command("fields", inputs),)
                derived[filtered] = DerivedSourceSpec(
                    alias=filtered, source=resolved,
                    pipeline=Pipeline(resolved, resolved, commands), params={})
                aliases[public_alias] = filtered
        first = aliases[current["sources"][0]["alias"]]
        commands = []
        for join in current["joins"]:
            left, right = aliases[join["left"]], aliases[join["right"]]
            commands.append(Command("join", (
                right, right, Ref(left, join["leftProperty"]),
                Ref(right, join["rightProperty"]), join["type"])))
            join_constraints[right] = {
                "cardinality": join["cardinality"], "keyType": join["keyType"],
            }
        for node in current["where"]:
            commands.append(Command("where", (predicate(node, aliases),)))
        if current["groupBy"] or current["aggregates"]:
            aggregates = [(item["fn"], Ref(aliases[item["source"]], item["property"])
                           if item.get("source") else None, item["as"])
                          for item in current["aggregates"]]
            output_names = {(item["source"], item["property"]): item["as"]
                            for item in current["select"]}
            groups = tuple((Ref(aliases[item["source"]], item["property"]),
                            output_names[(item["source"], item["property"])])
                           for item in current["groupBy"])
            if groups:
                aggregates.append(("group", groups))
            commands.append(Command("stats", tuple(aggregates)))
        else:
            commands.append(Command("fields", tuple(
                (Ref(aliases[item["source"]], item["property"]), item["as"])
                for item in current["select"])))
        if current["distinct"]:
            commands.append(Command("distinct", ()))
        if current["orderBy"]:
            commands.append(Command("sort", tuple(
                (item["output"], item["direction"], item["nulls"])
                for item in current["orderBy"])))
        return Pipeline(first, first, tuple(commands))

    pipeline = level(plan)
    origin = {"request": request, "planFingerprint": _fingerprint(plan),
              "sessionKind": session_kind, "sessionId": session_id,
              "expectations": expectations, "joinConstraints": join_constraints}
    return TypedQueryRequest(pipeline=pipeline, bindings=bindings, derived=derived,
                             join_constraints={alias: JoinConstraint(
                                 cardinality=value["cardinality"], key_type=value["keyType"])
                                 for alias, value in join_constraints.items()},
                             params={}, page_size=plan["pageSize"],
                             result_storage="memory", origin=origin,
                             request_fingerprint=_fingerprint(origin),
                             session_kind=session_kind, session_id=session_id)


def _response(result, plan):
    page = result["page"]
    rows = result["rows"]
    return {"queryId": result["queryId"], "schemaVersion": 2,
            "observedAt": min((state.get("asOf", "") for state in result["sourceStates"]), default=""),
            "releases": plan["releases"], "columns": result["columns"], "rows": rows,
            "pageRowCount": len(rows), "totalRowCount": result["totalRows"],
            "distinctEntityCounts": {}, "inputCounts": {}, "joinRowCounts": [],
            "partial": False, "truncated": False,
            "countsScope": result["countsScope"], "hasNext": page["hasMore"],
            "cursor": page.get("cursor") if page["hasMore"] else None,
            "expiresAt": result["expiresAt"], "citations": plan["citations"],
            "consistency": result["consistency"], "sourceStates": result["sourceStates"],
            "plan": result["plan"],
            "countAvailability": {"distinctEntityCounts": "not_computed",
                                  "inputCounts": "see_plan_transfer_metrics",
                                  "joinRowCounts": "see_plan_transfer_metrics"},
            "persistence": {"mode": "ephemeral",
                            "classification": ("aggregate" if result.get("persistence", {}).get("classification") == "aggregate"
                                               else "detail"),
                            "reference": {"queryId": result["queryId"],
                                          "expiresAt": result["expiresAt"]}}}


class OntologyQueryAdapter:
    def _service(self, db, user):
        from .limits import configured_limits
        limits = configured_limits()
        limits = replace(limits, result_bytes_limit=min(limits.result_bytes_limit, 1024 * 1024))
        return QueryService(db, user, secret=settings.secret_key, limits=limits)

    def execute(self, db, user, plan, *, request, session_kind, session_id):
        typed = build_typed_request(plan, request=request, session_kind=session_kind,
                                    session_id=session_id)
        try:
            result = self._service(db, user).execute_typed(typed)
        except QueryError:
            raise
        return _response(result, plan)

    def cancel(self, db, user, query_id):
        return self._service(db, user).cancel(query_id)

    def fetch(self, db, user, token, *, session_kind, session_id, prepare):
        from .contracts import TextQueryCursorRequest
        service = self._service(db, user)
        handle = service.memory_origin(token)
        if handle.get("session") != {"kind": session_kind, "id": session_id}:
            raise QueryError(404, "snapshot_not_found", "Query handle belongs to another session")
        origin = handle.get("origin")
        if (not isinstance(origin, dict) or not isinstance(origin.get("request"), dict)
                or not isinstance(origin.get("planFingerprint"), str)):
            raise QueryError(409, "query_context_changed", "Query handle has no semantic context")
        # The ontology caller supplies parsing/authorization without a reverse
        # domain dependency or a retained SQLAlchemy session in the registry.
        try:
            _, plan = prepare(db, user, origin["request"], serialized=True)
            if _fingerprint(plan) != origin["planFingerprint"]:
                raise QueryError(409, "query_context_changed", "Published query scope changed")
            return _response(service.fetch(TextQueryCursorRequest(cursor=token),
                                           session_kind=session_kind, session_id=session_id), plan)
        except Exception as exc:
            status = getattr(exc, "status", getattr(exc, "status_code", None))
            code = getattr(exc, "code", None)
            if code is None and isinstance(getattr(exc, "detail", None), dict):
                code = exc.detail.get("code")
            if (status in {403, 404, 409, 410}
                    or code in {"join_semantics_unverified", "SOURCE_UNAVAILABLE"}):
                # SOURCE_UNAVAILABLE during delivery (physical source outage
                # found by the page liveness probe) invalidates the handle: a
                # later restore must not resurrect an invalid delivery
                # context. Other transient 503s keep the handle retryable.
                service.cancel(handle["queryId"])
            raise
        finally:
            if db.in_transaction():
                db.rollback()
