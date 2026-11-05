"""可重放的查询语义计划（querySpec）与 Web 侧交互令牌。

设计目标：
- 聊天回合在 NATS worker 中执行；worker 的引擎 cursor 不能交给浏览器，
  也不能作为跨进程状态持久化。工具结果因此携带**语义计划**：固定作用域
  三元组 + 已解析的绝对时间窗口 + 结构化查询树/SPL，不含底层连接信息或
  任何引擎句柄。页面交互端点用同一 querySpec 重新执行，拥有自己的
  Web 侧句柄。
- 聚合下钻不是让前端拼 SQL，也不是让模型再推理一次：服务端把分组条件
  （多维数组、NULL、时间桶 [start,end) 边界）封进 HMAC 令牌，点击时由
  服务端按同一绝对窗口重查明细。
- ``public_query_result`` 是 worker 审计/展示的唯一脱敏口径：剔除
  rows/cursor/queryId，只留语义计划、列与计数元信息。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.query_window import bucket_bounds

QUERY_SPEC_VERSION = 1
MAX_QUERY_SPEC_BYTES = 16 * 1024
VIEW_CURSOR_TTL_SECONDS = 300
DRILLDOWN_TTL_SECONDS = 900
_MAX_LABEL = 120
_MAX_TOKEN_BYTES = 16000
_SPEC_KEYS = {
    "version", "shape", "ontologyId", "releaseId", "objectType",
    "query", "labels", "resolvedWindow", "pageSize", "description",
    "spl", "semanticBindings", "params", "drilldownBase", "drilldownLabels",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def _sign(purpose: str, payload: dict) -> str:
    body = canonical_json(payload)
    digest = hmac.new(settings.secret_key.encode("utf-8"),
                      f"{purpose}:{body}".encode("utf-8"),
                      hashlib.sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    token = f"{purpose}.{encoded.rstrip('=')}.{digest}"
    if len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise ToolError("query_token_too_large: 查询令牌超出体积上限；"
                        "请减少分组维度或分页重试")
    return token


def _verify(purpose: str, token: str) -> dict:
    try:
        prefix, body_part, digest = token.split(".", 2)
        if prefix != purpose:
            raise ValueError("purpose")
        padded = body_part + "=" * (-len(body_part) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise ToolError("query_view_cursor_invalid: 查询令牌格式非法") from exc
    expected = hmac.new(settings.secret_key.encode("utf-8"),
                        f"{purpose}:".encode("utf-8") + raw,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        raise ToolError("query_view_cursor_invalid: 查询令牌签名校验失败")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ToolError("query_view_cursor_invalid: 查询令牌载荷非法") from exc
    if not isinstance(payload, dict):
        raise ToolError("query_view_cursor_invalid: 查询令牌载荷非法")
    return payload


def _expired(payload: dict) -> bool:
    try:
        expires = float(payload.get("exp"))
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc).timestamp() >= expires


def _clean_labels(labels) -> dict[str, str]:
    if not isinstance(labels, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in labels.items():
        name = str(key or "").strip()
        text = str(value or "").strip()
        if name and text:
            out[name[:64]] = text[:_MAX_LABEL]
    return out


def build_query_spec(*, shape: str, ontology_id: str, release_id: str,
                     request=None, labels=None, resolved_window=None,
                     description=None, spl=None, semantic_bindings=None,
                     params=None, object_type=None) -> dict:
    spec: dict[str, Any] = {
        "version": QUERY_SPEC_VERSION,
        "shape": shape,
        "ontologyId": str(ontology_id),
        "releaseId": str(release_id),
        "labels": _clean_labels(labels),
        "resolvedWindow": resolved_window,
        "description": description or {},
    }
    if object_type is not None:
        spec["objectType"] = object_type
    if shape == "objects":
        if request is None:
            raise ToolError("query_spec_invalid: objects 形态缺少查询树")
        spec["query"] = request.model_dump(by_alias=True)
        spec["pageSize"] = request.page_size
    elif shape == "spl":
        if not spl:
            raise ToolError("query_spec_invalid: spl 形态缺少 spl 文本")
        spec["spl"] = str(spl)
        spec["semanticBindings"] = semantic_bindings or {}
        spec["params"] = params or {}
    else:
        raise ToolError(f"query_spec_invalid: 未知形态「{shape}」")
    size = len(canonical_json(spec).encode("utf-8"))
    if size > MAX_QUERY_SPEC_BYTES:
        raise ToolError(
            "query_spec_too_large: 语义计划超过 "
            f"{MAX_QUERY_SPEC_BYTES} 字节（{size}）；请减少选择字段/分组维度/"
            "过滤条件后重试，禁止把超预算计划交给页面重放")
    return spec


def sanitize_spec(spec) -> dict:
    """浏览器回传的计划只做结构清洗；作用域与授权在调用方强制覆盖。"""
    if not isinstance(spec, dict):
        raise ToolError("query_spec_invalid: querySpec 必须是对象")
    unknown = sorted(set(spec) - _SPEC_KEYS)
    if unknown:
        raise ToolError(f"query_spec_invalid: querySpec 含未知字段：{', '.join(unknown)}")
    shape = str(spec.get("shape") or "").strip()
    if shape not in {"objects", "spl"}:
        raise ToolError("query_spec_invalid: shape 需为 objects 或 spl")
    try:
        version = int(spec.get("version"))
    except (TypeError, ValueError) as exc:
        raise ToolError("query_spec_invalid: version 必须是整数") from exc
    if version != QUERY_SPEC_VERSION:
        raise ToolError("query_spec_invalid: 不支持的 querySpec 版本")
    cleaned = {
        "version": QUERY_SPEC_VERSION,
        "shape": shape,
        "ontologyId": str(spec.get("ontologyId") or ""),
        "releaseId": str(spec.get("releaseId") or ""),
        "labels": _clean_labels(spec.get("labels")),
        "resolvedWindow": spec.get("resolvedWindow"),
        "description": spec.get("description") if isinstance(
            spec.get("description"), dict) else {},
    }
    if "objectType" in spec:
        cleaned["objectType"] = str(spec.get("objectType") or "")[:200]
    if spec.get("pageSize") is not None:
        try:
            page_size = int(spec["pageSize"])
        except (TypeError, ValueError) as exc:
            raise ToolError("query_spec_invalid: pageSize 必须是整数") from exc
        if page_size < 1 or page_size > 200:
            raise ToolError("query_spec_invalid: pageSize 需在 1..200")
        cleaned["pageSize"] = page_size
    if spec.get("drilldownBase") is not None:
        if not isinstance(spec["drilldownBase"], dict):
            raise ToolError("query_spec_invalid: drilldownBase 必须是查询树")
        cleaned["drilldownBase"] = spec["drilldownBase"]
    if spec.get("drilldownLabels") is not None:
        cleaned["drilldownLabels"] = _clean_labels(spec["drilldownLabels"])
    if shape == "objects":
        query = spec.get("query")
        if not isinstance(query, dict):
            raise ToolError("query_spec_invalid: objects 形态缺少 query")
        if "cursor" in query or "queryId" in query:
            raise ToolError("query_spec_invalid: querySpec 不能携带引擎 cursor/queryId")
        cleaned["query"] = query
    else:
        spl = str(spec.get("spl") or "").strip()
        if not spl:
            raise ToolError("query_spec_invalid: spl 形态缺少 spl")
        bindings = spec.get("semanticBindings")
        if not isinstance(bindings, dict) or not bindings:
            raise ToolError("query_spec_invalid: spl 形态缺少 semanticBindings")
        cleaned["spl"] = spl
        cleaned["semanticBindings"] = bindings
        params = spec.get("params")
        cleaned["params"] = params if isinstance(params, dict) else {}
    if cleaned.get("resolvedWindow") is not None:
        window = cleaned["resolvedWindow"]
        if not isinstance(window, dict) or "start" not in window or "end" not in window:
            raise ToolError("query_spec_invalid: resolvedWindow 必须是已解析的绝对窗口")
        if "last" in window:
            raise ToolError("query_spec_invalid: querySpec 不能保留相对时间 last；"
                            "请使用已解析的绝对窗口")
    return cleaned


def scope_from_spec(spec: dict, *, ontology_id: str, release_id: str) -> dict:
    """强制把计划作用域钉到会话本体/发布；任何跨 scope 声明直接拒绝。"""
    if spec.get("ontologyId") not in ("", str(ontology_id)):
        raise ToolError("query_scope_mismatch: querySpec 的本体与当前会话不一致")
    if spec.get("releaseId") not in ("", str(release_id)):
        raise ToolError("query_scope_mismatch: querySpec 的发布版本与当前会话不一致")
    spec["ontologyId"] = str(ontology_id)
    spec["releaseId"] = str(release_id)
    return spec


def _object_sources(query: dict):
    for source in query.get("sources") or []:
        if not isinstance(source, dict):
            continue
        if source.get("subquery") is not None:
            yield from _object_sources(source["subquery"])
        else:
            yield source


def validate_object_scope(spec: dict, *, ontology_id: str, release_id: str,
                          visible_type_ids: set[str]) -> None:
    if spec.get("shape") != "objects":
        return
    sources = list(_object_sources(spec["query"]))
    if not sources:
        raise ToolError("query_spec_invalid: 查询树缺少对象源")
    for source in sources:
        if source.get("ontologyId") != str(ontology_id):
            raise ToolError("query_scope_mismatch: 查询源本体与当前会话不一致")
        if source.get("releaseId") != str(release_id):
            raise ToolError("query_scope_mismatch: 查询源发布版本与当前会话不一致")
        type_id = str(source.get("objectTypeId") or "")
        if type_id not in visible_type_ids:
            raise ToolError("query_scope_mismatch: 查询源对象类型不在当前授权范围内")
    joins = spec["query"].get("joins") or []
    if len(joins) > 6:
        raise ToolError("query_spec_invalid: joins 最多 6 跳")


def validate_spl_bindings(spec: dict, *, ontology_id: str, release_id: str,
                          scope) -> dict:
    bindings = spec.get("semanticBindings") or {}
    fixed: dict[str, dict] = {}
    for alias, raw in bindings.items():
        if not isinstance(raw, dict):
            raise ToolError(f"query_spec_invalid: semanticBindings.{alias} 必须是对象")
        ref = (raw.get("objectTypeId") or raw.get("object_type")
               or raw.get("objectType") or raw.get("object_type_id"))
        if not ref:
            raise ToolError(f"query_spec_invalid: semanticBindings.{alias} 缺少对象类型")
        ot = scope.require_object_type(str(ref))
        claimed_ontology = raw.get("ontologyId")
        claimed_release = raw.get("releaseId")
        if claimed_ontology not in (None, "", str(ontology_id)):
            raise ToolError(f"query_scope_mismatch: {alias} 的 ontologyId 与当前会话不一致")
        if claimed_release not in (None, "", str(release_id)):
            raise ToolError(f"query_scope_mismatch: {alias} 的 releaseId 与当前会话不一致")
        fixed[str(alias)] = {
            "ontologyId": str(ontology_id),
            "releaseId": str(release_id),
            "objectTypeId": ot.id,
        }
    if not fixed:
        raise ToolError("query_spec_invalid: SPL 查询至少需要一个语义绑定")
    spec["semanticBindings"] = fixed
    return fixed


def query_spec_hash(spec: dict) -> str:
    return spec_hash(spec)


def make_view_cursor(*, owner: str, ontology_id: str, conversation_id: str,
                     spec_hash_value: str, engine_cursor: str, query_id: str,
                     persistence: str, resolved_window=None) -> str:
    now = datetime.now(timezone.utc)
    return _sign("agent_query_view", {
        "v": 1,
        "owner": str(owner),
        "ontologyId": str(ontology_id),
        "conversationId": str(conversation_id),
        "specHash": str(spec_hash_value),
        "engineCursor": str(engine_cursor),
        "queryId": str(query_id),
        "persistence": str(persistence),
        "resolvedWindow": resolved_window,
        "exp": (now + timedelta(seconds=VIEW_CURSOR_TTL_SECONDS)).timestamp(),
    })


def verify_view_cursor(token: str, *, owner: str, ontology_id: str,
                       conversation_id: str) -> dict:
    payload = _verify("agent_query_view", token)
    if payload.get("owner") != str(owner):
        raise ToolError("query_view_cursor_invalid: 查询令牌不属于当前用户")
    if payload.get("ontologyId") != str(ontology_id):
        raise ToolError("query_view_cursor_invalid: 查询令牌不属于当前本体")
    if payload.get("conversationId") != str(conversation_id):
        raise ToolError("query_view_cursor_invalid: 查询令牌不属于当前会话")
    if _expired(payload):
        raise ToolError("query_snapshot_expired: 查询句柄已过期，请刷新查询")
    if not payload.get("engineCursor"):
        raise ToolError("query_view_cursor_invalid: 查询令牌缺少执行句柄")
    return payload


def make_drilldown(*, owner: str, ontology_id: str, conversation_id: str,
                   base_spec_hash: str, conditions: list[dict],
                   group_values: list, group_by: list[str],
                   resolved_window: dict | None) -> dict:
    now = datetime.now(timezone.utc)
    payload = {
        "v": 1,
        "owner": str(owner),
        "ontologyId": str(ontology_id),
        "conversationId": str(conversation_id),
        "specHash": str(base_spec_hash),
        "conditions": conditions,
        "groupValues": group_values,
        "groupBy": group_by,
        "resolvedWindow": resolved_window,
        "exp": (now + timedelta(seconds=DRILLDOWN_TTL_SECONDS)).timestamp(),
    }
    token = _sign("agent_query_drilldown", payload)
    return {
        "token": token,
        "description": {
            "kind": "detail_rows",
            "groupBy": group_by,
            "groupValues": group_values,
            "conditions": conditions,
            "window": resolved_window,
            "note": ("按同一绝对时间窗口与组条件重新查询明细；"
                     "这是新观测，observedAt 会变化（sameSnapshot=false）"),
        },
    }


def verify_drilldown(token: str, *, owner: str, ontology_id: str,
                     conversation_id: str) -> dict:
    payload = _verify("agent_query_drilldown", token)
    if payload.get("owner") != str(owner):
        raise ToolError("query_drilldown_invalid: 下钻令牌不属于当前用户")
    if payload.get("ontologyId") != str(ontology_id):
        raise ToolError("query_drilldown_invalid: 下钻令牌不属于当前本体")
    if payload.get("conversationId") != str(conversation_id):
        raise ToolError("query_drilldown_invalid: 下钻令牌不属于当前会话")
    if _expired(payload):
        raise ToolError("query_drilldown_expired: 下钻令牌已过期，请重新执行聚合查询")
    if not isinstance(payload.get("specHash"), str):
        raise ToolError("query_drilldown_invalid: 下钻令牌缺少计划指纹")
    if not isinstance(payload.get("conditions"), list):
        raise ToolError("query_drilldown_invalid: 下钻缺少组条件数组")
    return payload


def apply_drilldown(payload: dict, base_spec: dict) -> dict:
    base_spec = sanitize_spec(base_spec)
    if payload.get("specHash") != spec_hash(base_spec):
        raise ToolError("query_drilldown_invalid: 下钻令牌与 querySpec 不匹配")
    detail = base_spec.get("drilldownBase")
    if not isinstance(detail, dict):
        raise ToolError("query_drilldown_unsupported: querySpec 没有明细分页计划")
    conditions = payload.get("conditions")
    if not isinstance(conditions, list):
        raise ToolError("query_drilldown_invalid: 下钻缺少组条件数组")
    merged = list(detail.get("where") or [])
    for condition in conditions:
        if not isinstance(condition, dict):
            raise ToolError("query_drilldown_invalid: 下钻条件格式非法")
        merged.append(condition)
    detail = dict(detail)
    detail["where"] = merged
    if detail.get("groupBy") or detail.get("aggregates"):
        raise ToolError("query_drilldown_unsupported: 明细计划不能包含分组/聚合")
    spec = {
        **base_spec,
        "query": detail,
        "labels": {
            **(base_spec.get("labels") or {}),
            **(base_spec.get("drilldownLabels") or {}),
        },
        "description": {
            **(base_spec.get("description") or {}),
            "drilldown": True,
            "drilldownConditions": conditions,
            "drilldownGroupValues": payload.get("groupValues") or [],
        },
    }
    return sanitize_spec(spec)


def drilldown_conditions(group_values: list, group_items: list) -> list[dict]:
    """多维 / NULL / 时间桶边界 → 结构化过滤条件（不做字符串拼接/拆分）。"""
    if len(group_values) != len(group_items):
        raise ToolError("query_drilldown_invalid: 分组值与分组维度数量不一致")
    conditions: list[dict] = []
    for value, item in zip(group_values, group_items):
        source, prop = item["source"], item["property"]
        bucket = item.get("bucket")
        if value is None:
            conditions.append({"source": source, "property": prop,
                               "op": "is_null"})
            continue
        if bucket:
            bounds = bucket_bounds(value, bucket)
            if bounds is None:
                raise ToolError(
                    "query_drilldown_unsupported: 时间桶值无法解析为边界，"
                    "无法保证同一时间窗口下钻")
            start, end = bounds
            start_value = start.isoformat() if hasattr(start, "isoformat") else str(start)
            end_value = end.isoformat() if hasattr(end, "isoformat") else str(end)
            conditions.append({"source": source, "property": prop,
                               "op": "gte", "value": start_value})
            conditions.append({"source": source, "property": prop,
                               "op": "lt", "value": end_value})
            continue
        conditions.append({"source": source, "property": prop,
                           "op": "eq", "value": value})
    return conditions


def build_columns(raw_columns, labels: dict[str, str] | None = None) -> list[dict]:
    labels = labels or {}
    columns = []
    for item in raw_columns or []:
        if not isinstance(item, dict):
            continue
        output = str(item.get("name") or "")
        if not output:
            continue
        column = dict(item)
        column["output"] = output
        column["name"] = str(labels.get(output) or output)
        columns.append(column)
    return columns


def map_rows(rows, labels: dict[str, str] | None = None) -> list[dict]:
    labels = labels or {}
    mapped = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        mapped.append({labels.get(key, key): value for key, value in row.items()})
    return mapped


def public_query_result(result: dict) -> dict:
    """worker 审计/展示脱敏口径。

    - 明细查询：rows 不落账本（远程明细尤其禁止），只保留 querySpec 与列/计数；
    - 聚合查询：分组行本身就是统计结果元信息，保留 groups/rows/groupValues/
      drilldown/chart，供 UI 直接渲染（rows 是聚合行，不是明细）；
    - cursor/queryId/expiresAt 任何形态都不进入公开结果；querySpec 保持结构化，
      由 turn presenter 直接返回结构而不是字符串 preview。
    """
    if not isinstance(result, dict) or "querySpec" not in result:
        return result
    spec = result.get("querySpec") or {}
    semantics = ((spec.get("description") or {}).get("semantics")
                 if isinstance(spec, dict) else None)
    aggregate = bool(
        result.get("groupBy")
        or result.get("groups")
        or semantics in {"aggregate_scalar", "aggregate_grouped",
                         "grouped_aggregate", "distinct_subject_count",
                         "window_distinct",
                         "sequence_consecutive", "sequence_interval",
                         "sequence_ordered"}
    )
    blocked = {"cursor", "queryId", "expiresAt"}
    if not aggregate:
        blocked.add("rows")
    public = {key: value for key, value in result.items() if key not in blocked}
    public.setdefault("kind", "ontology_query_view")
    public["display"] = "query_view"
    public["rowsAvailableVia"] = "POST /agent/query-view"
    if aggregate:
        public["rowsAreAggregates"] = True
        public.setdefault("publicNote",
                          "rows/groups 是聚合统计结果元信息；明细请用 querySpec 打开查询视图")
    else:
        public["rowsOmitted"] = True
    return public
