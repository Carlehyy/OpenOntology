"""统一关联查询执行核 — 服务端完成关联/过滤/投影/聚合/排序与稳定分页。

设计要点（YIYA-39 契约 v2 §3）：
- 标识符（对象类型/属性/链接类型）只从发布快照 + AgentProfile 解析，请求中
  不接受原生 SQL/函数名/任意 JSON 路径；所有值经绑定参数。
- 每源独立校验：本体读权限、profile.enabled、对象/链接/属性 allowlist、
  current release（expected 不符 → 409 release_context_changed）。
- typed key 严格校验覆盖过滤后可见输入全集（非抽样）；基数在可见范围内用
  SQL GROUP BY/HAVING 实查；missing/JSON null 均按 NULL。
- 首请求物化有界结果快照（≤1000 行），后续页按 ordinal 读取；cursor 为
  HMAC 签名 opaque token，绑定快照/查询/主体/会话/授权/排序/页长。
- 查询在独立数据库会话中执行：PG 上整个执行位于 REPEATABLE READ +
  项目行 FOR SHARE + profile 行 FOR SHARE 之下；SQLite 保留契约语义
  （锁/隔离由真实 PG 验收覆盖）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.ontology import OntologyProject
from app.models.ontology_formal import LinkInstance, ObjectInstance
from app.ontologies.agent_query import schemas as QS
from app.ontologies.agent_query.models import AgentQueryRow, AgentQuerySnapshot
from app.ontologies.agent_runtime.boundary import (
    ToolError, build_scope, get_or_create_profile,
)
from app.ontologies.agent_runtime.models import AgentProfile
from app.ontologies.release_context import current_release_context

SCHEMA_VERSION = 1
SNAPSHOT_TTL_SECONDS = 300
SNAPSHOT_QUOTA_PER_USER = 10

SOURCE_ROW_BUDGET = 5000
RELATION_ROW_BUDGET = 10000
JOIN_ROW_BUDGET = 20000
RESULT_ROW_BUDGET = 1000
RESULT_BYTES_BUDGET = 1024 * 1024
VALUE_BYTES_BUDGET = 4096
MAX_FILTER_DEPTH = 4
MAX_FILTER_LEAVES = 32
MAX_IN_LIST = 100

_NUMBER_TYPES = {"number", "integer", "float", "double", "int", "decimal"}
_BOOLEAN_TYPES = {"boolean", "bool"}
_STRING_TYPES = {"string", "text", "date", "datetime", "uuid", "enum"}

_COUNTS_SCOPE = "complete_snapshot"


class QueryError(Exception):
    """查询面可识别错误 — 携带 HTTP 状态与机器可读 code。"""

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


def _declared_type(prop_def: dict) -> str:
    return str(prop_def.get("type") or "string").lower()


def _is_computed_prop(prop_def: dict) -> bool:
    """派生属性声明（与 evolution_service/function_engine 同一判据）。"""
    return prop_def.get("source") == "computed" or bool(prop_def.get("computed"))


_MEMORY_UNIT_KB = {
    "b": 1 / 1024, "k": 1, "kb": 1,
    "m": 1024, "mb": 1024, "g": 1024 * 1024, "gb": 1024 * 1024,
    "t": 1024 * 1024 * 1024, "tb": 1024 * 1024 * 1024,
}


def _parse_memory_limit_kb(raw: str) -> Optional[int]:
    """解析 PG 内存参数显示值（'-1'、'32768kB'、'32MB'、'1GB'…）为 kB。

    PG 的 current_setting 按最大人类单位归一显示，裸数字按该 GUC 的基础
    单位 kB 解释；无法解析或非正值（含 -1 unlimited）返回 None。
    """
    text = str(raw or "").strip().lower()
    match = re.fullmatch(r"(-?\d+)\s*(b|k|kb|m|mb|g|gb|t|tb)?", text)
    if match is None:
        return None
    number = int(match.group(1))
    if number < 0:
        return None
    unit = match.group(2) or "kb"
    value = number * _MEMORY_UNIT_KB[unit]
    kb = int(value)
    return kb if value == kb else kb + 1


def _is_number_type(decl: str) -> bool:
    return decl in _NUMBER_TYPES


def _is_boolean_type(decl: str) -> bool:
    return decl in _BOOLEAN_TYPES


def _is_string_type(decl: str) -> bool:
    return decl in _STRING_TYPES or decl not in _NUMBER_TYPES | _BOOLEAN_TYPES


def _validate_filter_value(prop: str, decl: str, value: Any) -> Any:
    """按声明类型严格校验过滤值；非法 → 422 invalid_typed_key。"""
    if value is None:
        raise QueryError(422, "invalid_typed_key",
                         f"属性「{prop}」的比较值不能为 null；判空请用 is_null")
    if _is_number_type(decl):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise QueryError(422, "invalid_typed_key",
                             f"属性「{prop}」声明为数值，值必须为 JSON number")
        if not math.isfinite(value):
            raise QueryError(422, "invalid_typed_key",
                             f"属性「{prop}」的数值必须有限")
        return float(value)
    if _is_boolean_type(decl):
        if not isinstance(value, bool):
            raise QueryError(422, "invalid_typed_key",
                             f"属性「{prop}」声明为布尔，值必须为 JSON boolean")
        return value
    if not isinstance(value, str):
        raise QueryError(422, "invalid_typed_key",
                         f"属性「{prop}」声明为 {decl}，值必须为字符串")
    return value


def _key_validator(key_type: str):
    if key_type == "number":
        return lambda v: (not isinstance(v, bool)
                          and isinstance(v, (int, float)) and math.isfinite(v))
    if key_type == "uuid":
        def _uuid_ok(value: Any) -> bool:
            if not isinstance(value, str):
                return False
            try:
                uuid_lib.UUID(value)
            except (ValueError, AttributeError):
                return False
            return True
        return _uuid_ok
    return lambda v: isinstance(v, str)


def _id_label(alias: str) -> str:
    return f"__id__{alias}"


def _prop_label(alias: str, prop: str) -> str:
    return f"__p__{alias}__{prop}"


def _raw_label(alias: str, prop: str) -> str:
    return f"__r__{alias}__{prop}"


class _SourceMeta:
    __slots__ = ("alias", "req", "scope", "ot", "props", "allowed", "subquery")

    def __init__(self, alias, req, scope, ot):
        self.alias = alias
        self.req = req
        self.scope = scope
        self.ot = ot
        self.props: dict[str, dict] = {}
        self.allowed: Optional[set] = None
        self.subquery = None


class UnifiedQueryService:
    """一次查询的解析、授权、执行与快照分页。"""

    def __init__(self, db: Session, user: Any, request: QS.QueryRequest,
                 *, session_kind: str, session_id: str):
        self.db = db
        self.user = user
        self.req = request
        self.session_kind = session_kind
        self.session_id = session_id
        self.sources: dict[str, _SourceMeta] = {}
        self.link_labels: list[str] = []
        self.output_columns: list[dict] = []
        self._pg = db.get_bind().dialect.name == "postgresql"

    # ------------------------------------------------------------- execute

    def execute(self) -> dict:
        self._validate_shape()
        self._ensure_profiles(
            {s.ontology_id for s in self.req.sources})
        self._pin_transaction()
        self._resolve_sources()
        page_size = self._resolve_page_size()
        tree, join_row_counts = self._build_join_tree()
        # P2-3：先预算后校验 — typed-key 核验只发生在受限且有界的输入上
        for meta in self.sources.values():
            self._check_source_budget(meta)
        self._validate_join_keys()
        counts = self._compute_entity_counts(tree)
        final_sql = self._build_final_select(tree)
        rows = self._materialize(final_sql)
        snapshot = self._persist_snapshot(rows, counts, join_row_counts,
                                          page_size)
        return self._page_payload(snapshot, rows[:page_size], start=0)

    # ------------------------------------------------------------- shape

    def _validate_shape(self):
        aliases = [s.alias for s in self.req.sources]
        if len(set(aliases)) != len(aliases):
            raise QueryError(422, "invalid_parameter", "source alias 重复")
        known = set(aliases)
        joined = {aliases[0]}
        for join in self.req.joins:
            if join.left not in known or join.right not in known:
                raise QueryError(422, "invalid_parameter",
                                 f"join 引用未知 alias：{join.left}/{join.right}")
            if join.left == join.right:
                raise QueryError(422, "invalid_parameter",
                                 "join 两侧不能是同一 alias")
            fresh = ({join.right} | {join.left}) - joined
            if not fresh:
                raise QueryError(422, "invalid_parameter",
                                 "join 必须引入新 alias（无环树）")
            joined |= fresh
        if len(joined) != len(known):
            missing = sorted(known - joined)
            raise QueryError(422, "invalid_parameter",
                             f"存在未连通的 source：{missing}")
        out_names = [i.as_ for i in self.req.select] + \
            [i.as_ for i in self.req.aggregates]
        if len(set(out_names)) != len(out_names):
            raise QueryError(422, "invalid_parameter", "输出别名重复")
        outputs = set(out_names)
        for item in self.req.order_by:
            if item.output not in outputs:
                raise QueryError(422, "invalid_parameter",
                                 f"orderBy 引用未知输出「{item.output}」")
        if self.req.distinct and (self.req.group_by or self.req.aggregates):
            raise QueryError(422, "invalid_parameter",
                             "distinct 不能与分组/聚合同时使用")
        select_keys = {(s.source, s.property) for s in self.req.select}
        group_keys = {(g.source, g.property) for g in self.req.group_by}
        if not self.req.select and not self.req.aggregates:
            raise QueryError(422, "invalid_parameter",
                             "select 与 aggregates 至少需要一项")
        if self.req.aggregates or self.req.group_by:
            if group_keys and select_keys != group_keys:
                raise QueryError(
                    422, "invalid_parameter",
                    "聚合查询的 select 只能是分组列（聚合输出走 aggregates）")
            if not group_keys and self.req.select:
                raise QueryError(
                    422, "invalid_parameter",
                    "纯聚合查询（无分组）不能输出业务列")
        for g in self.req.group_by:
            if (g.source, g.property) not in select_keys:
                raise QueryError(
                    422, "invalid_parameter",
                    "分组列必须出现在 select 中（只允许分组列和聚合输出）")
        for agg in self.req.aggregates:
            if agg.fn == "count":
                if agg.source is not None or agg.property is not None:
                    raise QueryError(422, "invalid_parameter",
                                     "count(*) 不能指定 source/property")
            elif not agg.source or not agg.property:
                raise QueryError(422, "invalid_parameter",
                                 f"聚合 {agg.fn} 必须指定 source 与 property")
        for sf in self.req.source_filters:
            if sf.source not in known:
                raise QueryError(422, "invalid_parameter",
                                 f"sourceFilter 引用未知 alias：{sf.source}")
        leaves = list(self._walk_filters(
            [sf.filter for sf in self.req.source_filters] + list(self.req.where)))
        if len(leaves) > MAX_FILTER_LEAVES:
            raise QueryError(422, "invalid_parameter",
                             f"过滤叶子总数最多 {MAX_FILTER_LEAVES}")
        for leaf in leaves:
            if leaf.source not in known:
                raise QueryError(422, "invalid_parameter",
                                 f"过滤引用未知 alias：{leaf.source}")
        for g in self.req.group_by:
            key = f"{g.source}__{g.property}"
            if key in outputs:
                raise QueryError(422, "invalid_parameter",
                                 f"分组输出名「{key}」与其他输出别名冲突")
        select_keys = {(s.source, s.property) for s in self.req.select}

    def _walk_filters(self, nodes, depth: int = 1):
        if depth > MAX_FILTER_DEPTH:
            raise QueryError(422, "invalid_parameter",
                             f"过滤表达式最大深度 {MAX_FILTER_DEPTH}")
        for node in nodes:
            if isinstance(node, QS.FilterLeaf):
                yield node
            elif isinstance(node, QS.FilterNot):
                yield from self._walk_filters([node.not_], depth + 1)
            else:
                children = node.and_ if isinstance(node, QS.FilterAnd) else node.or_
                yield from self._walk_filters(list(children), depth + 1)

    # ------------------------------------------------- release/auth pinning

    def _ensure_profiles(self, ontology_ids) -> None:
        """pin 前确保 profile 行已存在。

        ``get_or_create_profile`` 创建缺失 profile 时会 ``commit``；若发生在
        pinned 事务内部，REPEATABLE READ 快照与 SET LOCAL 预算会被提前终结。
        这里只在确实存在的本体上预建（占位/未知 id 跳过）。
        """
        oids = {str(oid) for oid in ontology_ids if oid}
        if not oids:
            return
        existing = {row[0] for row in self.db.query(OntologyProject.id)
                    .filter(OntologyProject.id.in_(sorted(oids))).all()}
        for oid in sorted(existing):
            get_or_create_profile(self.db, oid)

    def _pin_transaction(self):
        if not self._pg:
            return
        from sqlalchemy import text
        if self.db.in_transaction():
            # 认证依赖可能已开启 READ COMMITTED 事务；查询核必须以
            # REPEATABLE READ 作为事务首语句，丢弃只读残余是安全的。
            self.db.rollback()
        # P2-4：已冻结的查询资源/事务预算（契约 §3.4 表）。SET LOCAL 仅影响
        # 本事务；可设参数设置失败或预算校验不通过时查询 fail closed。
        try:
            # 隔离级别必须是事务首语句：解析/授权/计数/物化共享同一个
            # 一致性快照，执行中途的并发提交不会渗入本次结果或计数。
            self.db.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            is_superuser, temp_limit = self.db.execute(text(
                "SELECT current_setting('is_superuser'), "
                "current_setting('temp_file_limit')")).one()
            super_role = str(is_superuser).strip().lower() == "on"
            budgets = [
                "SET LOCAL statement_timeout = "
                f"'{settings.agent_query_statement_timeout_ms}ms'",
                "SET LOCAL lock_timeout = "
                f"'{settings.agent_query_lock_timeout_ms}ms'",
                "SET LOCAL idle_in_transaction_session_timeout = '5000ms'",
                # PG 内存单位是 B/kB/MB/GB（二进制倍数），不接受 MiB 拼写。
                "SET LOCAL work_mem = "
                f"'{settings.agent_query_work_mem_mib}MB'",
            ]
            if super_role:
                # temp_file_limit 是 SUSET 参数：只有超级角色能逐事务 SET LOCAL。
                budgets.append(
                    "SET LOCAL temp_file_limit = "
                    f"'{settings.agent_query_temp_file_limit_mib}MB'")
            for statement in budgets:
                self.db.execute(text(statement))
            if not super_role:
                # 普通角色无法逐事务收紧 SUSET 参数：生效值必须由运维以
                # ALTER ROLE 持久设置（改后需重建连接池）。未设置/宽于帽
                # 都 fail closed —— 预算不允许无声失效。
                self._verify_temp_file_budget(str(temp_limit))
        except QueryError:
            self.db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed
            self.db.rollback()
            raise QueryError(
                503, "query_budget_unavailable",
                "查询事务隔离级别/资源预算参数设置失败，查询 fail closed",
                retryable=False) from exc
        self._lock_pinned_rows()

    def _verify_temp_file_budget(self, raw_limit: str) -> None:
        """普通角色的 temp_file_limit 硬校验（SUSET 无法 SET LOCAL）。"""
        cap_kb = settings.agent_query_temp_file_limit_mib * 1024
        kb = _parse_memory_limit_kb(str(raw_limit or ""))
        if kb is None or kb <= 0 or kb > cap_kb:
            raise QueryError(
                503, "query_budget_unavailable",
                "temp_file_limit 是超级用户参数，当前应用角色无法逐事务"
                "设置，且生效值未设置(-1)或宽于配置帽 "
                f"{settings.agent_query_temp_file_limit_mib}MiB，"
                "查询 fail closed。请运维执行 "
                "ALTER ROLE <应用角色> SET temp_file_limit = "
                f"'{settings.agent_query_temp_file_limit_mib}MB'，"
                "并重建应用连接池后重试。",
                retryable=False,
                effectiveTempFileLimit=str(raw_limit or "") or None,
                budgetKb=cap_kb)

    def _lock_pinned_rows(self) -> None:
        oids = sorted({s.ontology_id for s in self.req.sources})
        (self.db.query(OntologyProject)
             .filter(OntologyProject.id.in_(oids))
             .order_by(OntologyProject.id)
             .with_for_update(read=True, of=OntologyProject)
             .all())
        for profile in (self.db.query(AgentProfile)
                        .filter(AgentProfile.ontology_id.in_(oids))
                        .order_by(AgentProfile.ontology_id)
                        .with_for_update(read=True, of=AgentProfile)
                        .all()):
            self.db.refresh(profile)

    def _release_error(self, exc: Exception) -> QueryError:
        status = getattr(exc, "status_code", None)
        detail = getattr(exc, "detail", None) or {}
        if isinstance(detail, dict):
            return QueryError(status or 409,
                              detail.get("code", "release_context_changed"),
                              detail.get("message", "当前发布版本已变化"),
                              reprepareRequired=True)
        return QueryError(status or 409, "release_context_changed",
                          str(detail or exc), reprepareRequired=True)

    def _resolve_sources(self):
        for src in self.req.sources:
            try:
                context = current_release_context(
                    self.db, src.ontology_id, expected_release_id=src.release_id)
            except QueryError:
                raise
            except Exception as exc:  # noqa: BLE001 - HTTP 状态透传
                raise self._release_error(exc) from exc
            project, profile, scope = build_scope(
                self.db, src.ontology_id, release_id=context.release.id)
            if not profile.enabled:
                raise QueryError(403, "profile_disabled",
                                 f"本体「{project.name}」的智能体已停用")
            ot = scope.object_types.get(src.object_type_id)
            if ot is None:
                raise QueryError(403, "object_type_forbidden",
                                 f"对象类型「{src.object_type_id}」不在授权范围内")
            meta = _SourceMeta(src.alias, src, scope, ot)
            meta.allowed = scope.allowed_property_names(ot.id)
            meta.props = {
                str(p.get("name")): p
                for p in (ot.properties or [])
                if isinstance(p, dict) and p.get("name")
            }
            self.sources[src.alias] = meta
        for sf in self.req.source_filters:
            for leaf in self._walk_filters([sf.filter]):
                if leaf.source != sf.source:
                    raise QueryError(
                        422, "invalid_parameter",
                        "sourceFilter 的过滤叶子只能引用所属 source")

    def _resolve_property(self, meta: _SourceMeta, ref: str) -> tuple[str, dict]:
        try:
            prop = meta.scope.resolve_property(meta.ot, ref)
        except ToolError as exc:
            if "授权范围内" in str(exc):
                raise QueryError(403, "property_forbidden",
                                 str(exc)) from exc
            raise QueryError(422, "unknown_parameter",
                             str(exc)) from exc
        definition = meta.props.get(prop)
        if definition is None:
            raise QueryError(422, "unknown_parameter",
                             f"属性「{ref}」未在发布定义中声明")
        if meta.allowed is not None and prop not in meta.allowed:
            raise QueryError(403, "property_forbidden",
                             f"属性「{prop}」不在授权范围内"
                             f"（对象类型「{meta.ot.display_name}」）")
        return prop, definition

    def _page_size_cap(self) -> int:
        caps = []
        for meta in self.sources.values():
            cap = int(meta.scope.profile.max_rows_per_query or 0)
            if cap <= 0:
                raise QueryError(403, "profile_query_disabled",
                                 "该本体的 profile 不允许查询（maxRowsPerQuery<=0）")
            caps.append(min(cap, 200))
        return min(caps)

    def _resolve_page_size(self) -> int:
        cap = self._page_size_cap()
        if self.req.page_size is None:
            # 契约 §3.4：默认页长 = min(50, 各源 profile 上限)
            return min(50, cap)
        if self.req.page_size > cap:
            raise QueryError(422, "invalid_parameter",
                             f"pageSize 超出上限 {cap}")
        return self.req.page_size

    # ------------------------------------------------------------ SQL 构造

    def _value_column(self, meta: _SourceMeta, prop: str):
        """按发布声明选择取值列：业务值在 properties，派生值在 computed。

        structure/skill card 把 computed 属性同样列为可查询字段；查询核若
        只读 properties，派生字段会静默变成全 NULL。声明与取值列的一致性
        以发布快照为准（写侧按同一声明拆分两列）。
        """
        if _is_computed_prop(meta.props[prop]):
            return ObjectInstance.computed[prop]
        return ObjectInstance.properties[prop]

    def _typed_expr(self, meta: _SourceMeta, prop: str):
        decl = _declared_type(meta.props[prop])
        column = self._value_column(meta, prop)
        if _is_number_type(decl):
            return column.as_float()
        if _is_boolean_type(decl):
            return column.as_boolean()
        return column.as_string()

    def _needed_props(self) -> dict[str, set[str]]:
        needed = {a: set() for a in self.sources}
        for item in self.req.select:
            prop, _ = self._resolve_property(self.sources[item.source],
                                             item.property)
            needed[item.source].add(prop)
        for g in self.req.group_by:
            prop, _ = self._resolve_property(self.sources[g.source], g.property)
            needed[g.source].add(prop)
        for agg in self.req.aggregates:
            if agg.source and agg.property:
                prop, _ = self._resolve_property(self.sources[agg.source],
                                                 agg.property)
                needed[agg.source].add(prop)
        for join in self.req.joins:
            if join.on.kind == "explicit_key":
                lp, _ = self._resolve_property(self.sources[join.left],
                                               join.on.left_property)
                rp, _ = self._resolve_property(self.sources[join.right],
                                               join.on.right_property)
                needed[join.left].add(lp)
                needed[join.right].add(rp)
        for leaf in self._walk_filters(
                [sf.filter for sf in self.req.source_filters]
                + list(self.req.where)):
            prop, _ = self._resolve_property(self.sources[leaf.source],
                                             leaf.property)
            needed[leaf.source].add(prop)
        return needed

    def _leaf_expr(self, expr, leaf: QS.FilterLeaf, decl: str, prop: str):
        op = leaf.op
        if op == "is_null":
            return expr.is_(None)
        if op == "is_not_null":
            return expr.is_not(None)
        if op == "in":
            if not isinstance(leaf.value, list) or not leaf.value:
                raise QueryError(422, "invalid_parameter",
                                 "in 操作需要非空数组值")
            if len(leaf.value) > MAX_IN_LIST:
                raise QueryError(422, "invalid_parameter",
                                 f"in 列表最多 {MAX_IN_LIST} 项")
            values = [_validate_filter_value(prop, decl, v)
                      for v in leaf.value]
            return expr.in_(values)
        value = _validate_filter_value(prop, decl, leaf.value)
        if op == "eq":
            return expr == value
        if op == "neq":
            return expr != value
        if op == "contains":
            if not isinstance(value, str):
                raise QueryError(422, "invalid_parameter", "contains 仅支持字符串")
            return expr.contains(value, autoescape=True)
        if op in ("gt", "gte", "lt", "lte"):
            symbol = {"gt": "__gt__", "gte": "__ge__",
                      "lt": "__lt__", "lte": "__le__"}[op]
            return getattr(expr, symbol)(value)
        raise QueryError(422, "invalid_parameter", f"未知操作符 {op}")

    def _filter_expr(self, node, column_of):
        """column_of(alias, prop) → 当前列表达式（子查询或树列）。"""
        if isinstance(node, QS.FilterLeaf):
            meta = self.sources[node.source]
            prop, definition = self._resolve_property(meta, node.property)
            return self._leaf_expr(column_of(node.source, prop), node,
                                   _declared_type(definition), prop)
        if isinstance(node, QS.FilterNot):
            return not_(self._filter_expr(node.not_, column_of))
        if isinstance(node, QS.FilterAnd):
            return and_(*[self._filter_expr(n, column_of) for n in node.and_])
        return or_(*[self._filter_expr(n, column_of) for n in node.or_])

    def _build_source_subqueries(self, needed: dict[str, set[str]]):
        for alias, meta in self.sources.items():
            columns = [ObjectInstance.id.label(_id_label(alias))]
            for prop in sorted(needed[alias]):
                columns.append(
                    self._typed_expr(meta, prop).label(_prop_label(alias, prop)))
                if prop in self._join_key_props().get(alias, set()):
                    # P2-3：join 键原始 JSON 值 — typed-key 校验在 SQL 内完成
                    columns.append(
                        self._value_column(meta, prop).as_json().label(
                            _raw_label(alias, prop)))
            stmt = select(*columns).where(
                ObjectInstance.ontology_id == meta.req.ontology_id,
                ObjectInstance.ontology_release_id == meta.req.release_id,
                ObjectInstance.object_type_id == meta.ot.id,
            )
            for sf in self.req.source_filters:
                if sf.source == alias:
                    stmt = stmt.where(self._filter_expr(
                        sf.filter,
                        lambda a, p: self._typed_expr(self.sources[a], p)))
            meta.subquery = stmt.subquery(f"sq_{alias}")

    def _join_key_props(self) -> dict[str, set[str]]:
        props: dict[str, set[str]] = {}
        for join in self.req.joins:
            if join.on.kind == "explicit_key":
                props.setdefault(join.left, set()).add(join.on.left_property)
                props.setdefault(join.right, set()).add(join.on.right_property)
        return {alias: {self._resolve_property(self.sources[alias], p)[0]
                        for p in ps}
                for alias, ps in props.items()}

    def _sub_col(self, alias: str, prop: str):
        return self.sources[alias].subquery.c[_prop_label(alias, prop)]

    def _build_join_tree(self):
        needed = self._needed_props()
        self._build_source_subqueries(needed)
        first = self.req.sources[0].alias
        id_cols = [self.sources[a].subquery.c[_id_label(a)]
                   for a in map(lambda s: s.alias, self.req.sources)]
        prop_cols = []
        for alias, meta in self.sources.items():
            for prop in sorted(needed[alias]):
                prop_cols.append(meta.subquery.c[_prop_label(alias, prop)])
        query = select(*(id_cols + prop_cols)).select_from(
            self.sources[first].subquery)
        joined = {first}
        join_row_counts: list[int] = []
        for join in self.req.joins:
            isouter = join.type == "left"
            new_alias = join.right if join.left in joined else join.left
            if join.on.kind == "explicit_key":
                onclause = (self._sub_col(join.left, join.on.left_property)
                            == self._sub_col(join.right, join.on.right_property))
                query = query.join(self.sources[new_alias].subquery, onclause,
                                   isouter=isouter)
            else:
                left = self.sources[join.left]
                right = self.sources[join.right]
                if (left.req.ontology_id != right.req.ontology_id
                        or left.req.release_id != right.req.release_id):
                    raise QueryError(422, "invalid_parameter",
                                     "declared_relation join 的两源必须同本体同发布")
                lt = left.scope.require_link_type(join.on.link_type_id)
                if join.on.direction == "source_to_target":
                    if not (left.ot.id == lt.source_object_type_id
                            and right.ot.id == lt.target_object_type_id):
                        raise QueryError(422, "invalid_parameter",
                                         "链接类型端点与 join 方向不符")
                elif not (left.ot.id == lt.target_object_type_id
                          and right.ot.id == lt.source_object_type_id):
                    raise QueryError(422, "invalid_parameter",
                                     "链接类型端点与 join 方向不符")
                link_label = f"__link__{join.left}_{join.right}"
                link_sq = select(
                    LinkInstance.id.label(link_label),
                    LinkInstance.source_object_id.label("src"),
                    LinkInstance.target_object_id.label("tgt"),
                ).where(
                    LinkInstance.ontology_id == left.req.ontology_id,
                    LinkInstance.ontology_release_id == left.req.release_id,
                    LinkInstance.link_type_id == lt.id,
                ).subquery(f"lq_{join.left}_{join.right}")
                self.link_labels.append(link_label)
                left_id = self.sources[join.left].subquery.c[
                    _id_label(join.left)]
                right_id = self.sources[join.right].subquery.c[
                    _id_label(join.right)]
                if join.on.direction == "source_to_target":
                    left_match = left_id == link_sq.c["src"]
                    right_match = link_sq.c["tgt"] == right_id
                else:
                    left_match = left_id == link_sq.c["tgt"]
                    right_match = link_sq.c["src"] == right_id
                # ON 条件必须先锚定「已在树中」的一侧，再接入新 alias：
                # 新侧在 left（连接源侧）时，link 先按已入树的 right 端
                # 关联，再 join 新 left 子查询，避免引用未入 FROM 的表。
                if new_alias == join.right:
                    query = query.join(link_sq, left_match, isouter=isouter)
                    query = query.join(self.sources[new_alias].subquery,
                                       right_match, isouter=isouter)
                else:
                    query = query.join(link_sq, right_match, isouter=isouter)
                    query = query.join(self.sources[new_alias].subquery,
                                       left_match, isouter=isouter)
                query = query.add_columns(link_sq.c[link_label])
            joined.add(new_alias)
            count = int(self.db.execute(
                select(func.count()).select_from(query.subquery())).scalar() or 0)
            join_row_counts.append(count)
            if count > JOIN_ROW_BUDGET:
                raise QueryError(422, "join_budget_exceeded",
                                 f"join 中间/最终明细超出预算 {JOIN_ROW_BUDGET}",
                                 limit=JOIN_ROW_BUDGET)
        for join in self.req.joins:
            if join.on.kind == "declared_relation":
                left = self.sources[join.left]
                lt = left.scope.require_link_type(join.on.link_type_id)
                link_count = int(self.db.execute(
                    select(func.count(LinkInstance.id)).where(
                        LinkInstance.ontology_id == left.req.ontology_id,
                        LinkInstance.ontology_release_id == left.req.release_id,
                        LinkInstance.link_type_id == lt.id)).scalar() or 0)
                if link_count > RELATION_ROW_BUDGET:
                    raise QueryError(422, "relation_budget_exceeded",
                                     f"声明关系输入超出预算 {RELATION_ROW_BUDGET}",
                                     limit=RELATION_ROW_BUDGET)
        return query.subquery("tree"), join_row_counts

    def _tree_col(self, alias: str, prop: str, tree):
        return tree.c[_prop_label(alias, prop)]

    def _where_clause(self, tree):
        if not self.req.where:
            return None
        return and_(*[self._filter_expr(
            node, lambda a, p: self._tree_col(a, p, tree))
            for node in self.req.where])

    # ------------------------------------------------------- typed keys

    def _validate_join_keys(self):
        self.join_evidence: list[dict] = []
        for join in self.req.joins:
            if join.on.kind != "explicit_key":
                continue
            left = self.sources[join.left]
            right = self.sources[join.right]
            self.join_evidence.append({
                "leftAlias": join.left, "rightAlias": join.right,
                **verify_join_evidence(self.db, self.user, join, left,
                                       right),
            })
            lp, lp_def = self._resolve_property(left, join.on.left_property)
            rp, rp_def = self._resolve_property(right, join.on.right_property)
            key_type = join.on.key_type
            if key_type == "number":
                if not (_is_number_type(_declared_type(lp_def))
                        and _is_number_type(_declared_type(rp_def))):
                    raise QueryError(422, "invalid_typed_key",
                                     "number join 键必须双方声明为数值")
            else:
                if not (_is_string_type(_declared_type(lp_def))
                        and _is_string_type(_declared_type(rp_def))):
                    raise QueryError(422, "invalid_typed_key",
                                     f"{key_type} join 键必须双方声明为字符串")
            check = _key_validator(key_type)
            self._check_side_key(left, lp, check, join.on.left_property,
                                 key_type)
            self._check_side_key(right, rp, check, join.on.right_property,
                                 key_type)
            self._check_cardinality(join, left, lp, right, rp)

    def _check_side_key(self, meta: _SourceMeta, prop: str, check, label: str,
                        key_type: str) -> None:
        """P2-3：类型族核验在 SQL 内完成（sourceFilters 后的受限子查询），
        不再预算前无界加载行到 Python；uuid 形态做预算内有限核对。"""
        raw = meta.subquery.c[_raw_label(meta.alias, prop)]
        typed = meta.subquery.c[_prop_label(meta.alias, prop)]
        dialect = self.db.get_bind().dialect.name
        if dialect == "postgresql":
            # properties/computed 列是 json（非 jsonb）：jsonb_typeof(json)
            # 函数不存在会直接 500。json_typeof 原生可用，类型名集合与
            # jsonb_typeof 一致（number/string/boolean/object/array/null）。
            typeof_expr = func.json_typeof(raw)
            allowed = {"number"} if key_type == "number" else {"string"}
        else:
            typeof_expr = func.json_type(raw)
            allowed = ({"integer", "real"} if key_type == "number"
                       else {"text"})
        bad = int(self.db.execute(
            select(func.count()).select_from(meta.subquery).where(
                typed.is_not(None),
                typeof_expr.notin_(allowed),
            )).scalar() or 0)
        if key_type == "uuid":
            # uuid 形态无法纯 SQL 解析：预算内有限取值核对
            values = self.db.execute(
                select(raw).where(
                    typed.is_not(None)).limit(
                        SOURCE_ROW_BUDGET + 1)).scalars().all()
            bad += sum(1 for v in values if v is not None and not check(v))
        if bad:
            raise QueryError(422, "invalid_typed_key",
                             f"join 键「{label}」存在 {bad} 个非空非法值"
                             "（类型检查覆盖 SQL 过滤后可见输入全集）",
                             errorCount=bad, field=label)

    def _check_cardinality(self, join, left, lp, right, rp):
        def unique_violation(meta, prop) -> bool:
            col = meta.subquery.c[_prop_label(meta.alias, prop)]
            stmt = (select(col)
                    .select_from(meta.subquery)
                    .where(col.is_not(None))
                    .group_by(col)
                    .having(func.count() > 1)
                    .limit(1))
            return self.db.execute(stmt).first() is not None

        cardinality = join.on.cardinality
        violated = False
        if cardinality in ("N:1", "1:1"):
            violated = violated or unique_violation(right, rp)
        if cardinality in ("1:N", "1:1"):
            violated = violated or unique_violation(left, lp)
        if violated:
            raise QueryError(
                422, "join_cardinality_mismatch",
                f"声明基数 {cardinality} 在本次可见输入范围内不成立",
                cardinality=cardinality, scope="visible_filtered_inputs")

    def _check_source_budget(self, meta: _SourceMeta):
        count = int(self.db.execute(
            select(func.count()).select_from(meta.subquery)).scalar() or 0)
        if count > SOURCE_ROW_BUDGET:
            raise QueryError(422, "source_row_budget_exceeded",
                             f"源「{meta.alias}」过滤后输入超出预算"
                             f" {SOURCE_ROW_BUDGET}",
                             limit=SOURCE_ROW_BUDGET, source=meta.alias)

    # ------------------------------------------------------------- output

    def _compute_entity_counts(self, tree) -> dict[str, int]:
        where = self._where_clause(tree)
        counts: dict[str, int] = {}
        for alias in self.sources:
            col = tree.c[_id_label(alias)]
            stmt = select(func.count(func.distinct(col))).select_from(tree)
            if where is not None:
                stmt = stmt.where(where)
            counts[alias] = int(self.db.execute(stmt).scalar() or 0)
        return counts

    def _input_counts(self) -> dict[str, int]:
        return {
            alias: int(self.db.execute(
                select(func.count()).select_from(meta.subquery)).scalar() or 0)
            for alias, meta in self.sources.items()
        }

    def _output_expr(self, item, tree):
        return self._tree_col(item.source, item.property, tree)

    def _build_final_select(self, tree):
        where = self._where_clause(tree)
        aggregated = bool(self.req.group_by) or bool(self.req.aggregates)
        order_clauses: list[Any] = []

        def ordered(expr, item: QS.OrderByItem):
            if item.direction == "asc":
                base = expr.asc()
            else:
                base = expr.desc()
            return base.nulls_first() if item.nulls == "first" \
                else base.nulls_last()

        if aggregated:
            # 分组列必须以 select 别名输出（校验已保证 select ⊇ groupBy），
            # 纯聚合（无分组）时 select 必须为空。
            stmt = select().select_from(tree)
            output_map: dict[str, Any] = {}
            group_exprs = []
            for item in self.req.select:
                expr = self._output_expr(item, tree)
                output_map[item.as_] = expr
                stmt = stmt.add_columns(expr.label(item.as_))
                group_exprs.append(expr)
            for agg in self.req.aggregates:
                if agg.fn == "count":
                    expr = func.count()
                else:
                    col = self._output_expr(
                        QS.SelectItem(source=agg.source, property=agg.property,
                                      as_=agg.as_), tree)
                    if agg.fn in ("sum", "avg"):
                        meta = self.sources[agg.source]
                        _, definition = self._resolve_property(meta, agg.property)
                        if not _is_number_type(_declared_type(definition)):
                            raise QueryError(
                                422, "invalid_parameter",
                                f"{agg.fn} 仅支持数值属性（{agg.property}）")
                    factory = {
                        "count_distinct": lambda c: func.count(func.distinct(c)),
                        "sum": func.sum, "avg": func.avg,
                        "min": func.min, "max": func.max,
                    }[agg.fn]
                    expr = factory(col)
                output_map[agg.as_] = expr
                stmt = stmt.add_columns(expr.label(agg.as_))
            if group_exprs:
                stmt = stmt.group_by(*group_exprs)
            if where is not None:
                stmt = stmt.where(where)
            chosen = set()
            for item in self.req.order_by:
                order_clauses.append(ordered(output_map[item.output], item))
                chosen.add(item.output)
            # 全序：用户输出后补完整 groupBy typed tuple
            for item in self.req.select:
                if item.as_ not in chosen:
                    order_clauses.append(
                        output_map[item.as_].asc().nulls_last())
            self.output_columns = self._describe_columns([])
            self._output_names = list(output_map.keys())
            if order_clauses:
                stmt = stmt.order_by(*order_clauses)
            return stmt


        stmt = select().select_from(tree)
        output_map = {}
        for item in self.req.select:
            expr = self._output_expr(item, tree)
            output_map[item.as_] = expr
            stmt = stmt.add_columns(expr.label(item.as_))
        if where is not None:
            stmt = stmt.where(where)

        if self.req.distinct:
            stmt = stmt.distinct()
            chosen = set()
            for item in self.req.order_by:
                order_clauses.append(ordered(output_map[item.output], item))
                chosen.add(item.output)
            for item in self.req.select:
                if item.as_ not in chosen:
                    order_clauses.append(
                        output_map[item.as_].asc().nulls_last())
            self.output_columns = self._describe_columns([])
            self._output_names = list(output_map.keys())
            if order_clauses:
                stmt = stmt.order_by(*order_clauses)
            return stmt

        tiebreak = [tree.c[_id_label(a)]
                    for a in map(lambda s: s.alias, self.req.sources)]
        tiebreak += [tree.c[label] for label in self.link_labels]
        for col in tiebreak:
            stmt = stmt.add_columns(col)
        chosen = set()
        for item in self.req.order_by:
            order_clauses.append(ordered(output_map[item.output], item))
            chosen.add(item.output)
        # 全序：用户输出后补每源 instance.id 与声明关系 link.id，
        # 升降序均固定 NULLS LAST（left 缺右端时右端 id 为 NULL）。
        order_clauses.extend(col.asc().nulls_last() for col in tiebreak)
        self.output_columns = self._describe_columns([])
        self._output_names = list(output_map.keys())
        stmt = stmt.order_by(*order_clauses)
        return stmt

    def _describe_columns(self, group_keys: list[str]) -> list[dict]:
        columns = []
        for item in self.req.select:
            meta = self.sources[item.source]
            prop, definition = self._resolve_property(meta, item.property)
            columns.append({
                "name": item.as_,
                "type": _declared_type(definition),
                "nullable": not definition.get("required"),
                "sourceRefs": {
                    "alias": item.source,
                    "ontologyId": meta.req.ontology_id,
                    "releaseId": meta.req.release_id,
                    "objectTypeId": meta.ot.id,
                    "property": prop,
                },
            })
        for agg in self.req.aggregates:
            columns.append({
                "name": agg.as_,
                "type": "number",
                "nullable": True,
                "sourceRefs": ({"alias": agg.source, "fn": agg.fn,
                                "property": agg.property}
                               if agg.source else {"fn": agg.fn}),
            })
        return columns

    def _materialize(self, final_sql) -> list[dict]:
        result = self.db.execute(final_sql)
        keys = list(result.keys())
        output_names = set(getattr(self, "_output_names", []))
        rows: list[dict] = []
        total_bytes = 0
        for row in result:
            if len(rows) >= RESULT_ROW_BUDGET:
                raise QueryError(422, "result_budget_exceeded",
                                 f"完整结果超出 {RESULT_ROW_BUDGET} 行上限",
                                 limit=RESULT_ROW_BUDGET)
            record = {}
            for idx, name in enumerate(keys):
                if name not in output_names:
                    continue
                value = row[idx]
                if value is None:
                    record[name] = None
                    continue
                encoded = json.dumps(value, ensure_ascii=False, default=str)
                size = len(encoded.encode("utf-8"))
                if size > VALUE_BYTES_BUDGET:
                    raise QueryError(
                        422, "result_budget_exceeded",
                        f"输出值超出 {VALUE_BYTES_BUDGET} 字节上限",
                        limit=VALUE_BYTES_BUDGET)
                record[name] = value
                total_bytes += size
            rows.append(record)
            if total_bytes > RESULT_BYTES_BUDGET:
                raise QueryError(422, "result_budget_exceeded",
                                 f"完整结果超出 {RESULT_BYTES_BUDGET} 字节上限",
                                 limit=RESULT_BYTES_BUDGET)
        return rows

    # ------------------------------------------------------------ snapshot

    def _auth_material(self) -> dict:
        material = {"user": str(getattr(self.user, "id", "")),
                    "role": str(getattr(self.user, "role", ""))}
        for alias, meta in sorted(self.sources.items()):
            profile = meta.scope.profile
            material[alias] = {
                "ontology": meta.req.ontology_id,
                "release": meta.req.release_id,
                "enabled": bool(profile.enabled),
                "allowedObject": profile.allowed_object_type_ids,
                "allowedLink": profile.allowed_link_type_ids,
                "allowedAction": profile.allowed_action_ids,
                "allowedProperties": getattr(profile, "allowed_properties", None),
                "maxRows": int(profile.max_rows_per_query or 0),
            }
        return material

    def _auth_hash(self) -> str:
        return _sha256(_canonical_json(self._auth_material()))

    def _query_hash(self) -> str:
        return _sha256(_canonical_json(self.req.model_dump(by_alias=True)))

    def _persist_snapshot(self, rows: list[dict], counts: dict,
                          join_row_counts: list[int],
                          page_size: int) -> AgentQuerySnapshot:
        now = datetime.now(timezone.utc)
        self.db.query(AgentQuerySnapshot).filter(
            AgentQuerySnapshot.owner_user_id == str(self.user.id),
            AgentQuerySnapshot.expires_at < now,
        ).delete(synchronize_session=False)
        active = int(self.db.query(func.count(AgentQuerySnapshot.id)).filter(
            AgentQuerySnapshot.owner_user_id == str(self.user.id),
            AgentQuerySnapshot.expires_at >= now).scalar() or 0)
        if active >= SNAPSHOT_QUOTA_PER_USER:
            raise QueryError(429, "snapshot_quota",
                             f"未过期查询快照已达 {SNAPSHOT_QUOTA_PER_USER} 个上限",
                             retryableAfter=SNAPSHOT_TTL_SECONDS)
        releases = {
            alias: {
                "ontologyId": meta.req.ontology_id,
                "releaseId": meta.req.release_id,
                "versionNumber": meta.scope.release.version
                if meta.scope.release else None,
            }
            for alias, meta in self.sources.items()}
        snapshot = AgentQuerySnapshot(
            owner_user_id=str(self.user.id),
            session_kind=self.session_kind,
            session_id=self.session_id,
            query_hash=self._query_hash(),
            auth_hash=self._auth_hash(),
            releases=releases,
            schema_version=SCHEMA_VERSION,
            order_spec=[o.model_dump(by_alias=True) for o in self.req.order_by],
            columns=self.output_columns,
            counts={
                "distinctEntityCounts": counts,
                "joinRowCounts": join_row_counts,
                "inputCounts": self._input_counts(),
                "joinEvidence": getattr(self, "join_evidence", []),
            },
            citations=[{
                "alias": alias,
                "ontologyId": meta.req.ontology_id,
                "releaseId": meta.req.release_id,
                "objectTypeId": meta.ot.id,
            } for alias, meta in sorted(self.sources.items())],
            page_size=page_size,
            total_row_count=len(rows),
            observed_at=now,
            expires_at=now + timedelta(seconds=SNAPSHOT_TTL_SECONDS),
        )
        self.db.add(snapshot)
        self.db.flush()
        if rows:
            # 批量落行（executemany）：SQL 条数不随行数线性增长
            self.db.execute(
                AgentQueryRow.__table__.insert(),
                [{"snapshot_id": snapshot.id, "ordinal": ordinal,
                  "row": row} for ordinal, row in enumerate(rows)],
            )
        self.db.commit()
        return snapshot

    # -------------------------------------------------------------- cursor

    def _sign(self, payload: dict) -> str:
        body = _canonical_json(payload)
        digest = hmac.new(settings.secret_key.encode("utf-8"),
                          body.encode("utf-8"), hashlib.sha256).hexdigest()
        encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
        return encoded.rstrip("=") + "." + digest

    def _verify_cursor(self, token: str) -> dict:
        try:
            body_part, digest = token.rsplit(".", 1)
            padded = body_part + "=" * (-len(body_part) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        except Exception as exc:  # noqa: BLE001
            raise QueryError(400, "invalid_cursor", "cursor 格式非法") from exc
        expected = hmac.new(settings.secret_key.encode("utf-8"), raw,
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, digest):
            raise QueryError(400, "invalid_cursor", "cursor 签名校验失败")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise QueryError(400, "invalid_cursor", "cursor 载荷非法") from exc
        if payload.get("owner") != str(self.user.id):
            raise QueryError(404, "snapshot_not_found", "快照不存在")
        if (payload.get("sessionKind") != self.session_kind
                or payload.get("sessionId") != self.session_id):
            raise QueryError(404, "snapshot_not_found", "快照不存在")
        return payload

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _page_payload(self, snapshot: AgentQuerySnapshot, rows: list[dict],
                      *, start: int) -> dict:
        total = snapshot.total_row_count
        has_next = start + len(rows) < total
        cursor = None
        if has_next:
            cursor = self._sign({
                "v": SCHEMA_VERSION,
                "sid": snapshot.id,
                "qh": snapshot.query_hash,
                "ah": snapshot.auth_hash,
                "owner": snapshot.owner_user_id,
                "sessionKind": snapshot.session_kind,
                "sessionId": snapshot.session_id,
                "releases": snapshot.releases,
                "pageSize": snapshot.page_size,
                "next": start + len(rows),
                "exp": self._as_utc(snapshot.expires_at).timestamp(),
            })
        counts = snapshot.counts or {}
        return {
            "queryId": snapshot.id,
            "schemaVersion": snapshot.schema_version,
            "observedAt": self._as_utc(snapshot.observed_at).isoformat(),
            "releases": snapshot.releases,
            "columns": snapshot.columns,
            "rows": rows,
            "pageRowCount": len(rows),
            "totalRowCount": total,
            "distinctEntityCounts": counts.get("distinctEntityCounts", {}),
            "joinEvidence": counts.get("joinEvidence", []),
            "inputCounts": counts.get("inputCounts", {}),
            "joinRowCounts": counts.get("joinRowCounts", []),
            "partial": False,
            "truncated": False,
            "countsScope": _COUNTS_SCOPE,
            "hasNext": has_next,
            "cursor": cursor,
            "expiresAt": self._as_utc(snapshot.expires_at).isoformat(),
            "citations": snapshot.citations,
        }

    # ---------------------------------------------------------- page fetch

    def fetch_page(self, token: str) -> dict:
        payload = self._verify_cursor(token)
        # 快照本体集在签名 cursor 载荷内：据此预建 profile，避免 pinned
        # 事务内部因 profile 缺失被 get_or_create_profile 的 commit 打断。
        self._ensure_profiles(
            {info.get("ontologyId")
             for info in (payload.get("releases") or {}).values()})
        self._pin_transaction()
        snapshot = (
            self.db.query(AgentQuerySnapshot)
            .filter(AgentQuerySnapshot.id == payload.get("sid"),
                    AgentQuerySnapshot.owner_user_id == str(self.user.id),
                    AgentQuerySnapshot.session_kind == self.session_kind,
                    AgentQuerySnapshot.session_id == self.session_id)
            .first())
        if snapshot is None:
            raise QueryError(404, "snapshot_not_found", "快照不存在")
        if self._as_utc(snapshot.expires_at) < datetime.now(timezone.utc):
            raise QueryError(410, "query_snapshot_expired",
                             "查询快照已过期，请重新查询", reprepareRequired=True)
        for release_info in (snapshot.releases or {}).values():
            try:
                current_release_context(
                    self.db, release_info["ontologyId"],
                    expected_release_id=release_info["releaseId"])
            except QueryError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise self._release_error(exc) from exc
        current_auth = self._recompute_auth_hash(snapshot)
        if current_auth != snapshot.auth_hash:
            raise QueryError(409, "query_context_changed",
                             "授权上下文已变化，不能继续读取该快照",
                             reprepareRequired=True)
        start = int(payload.get("next", 0))
        records = (self.db.query(AgentQueryRow)
                   .filter(AgentQueryRow.snapshot_id == snapshot.id,
                           AgentQueryRow.ordinal >= start,
                           AgentQueryRow.ordinal < start + snapshot.page_size)
                   .order_by(AgentQueryRow.ordinal).all())
        return self._page_payload(snapshot, [r.row for r in records],
                                  start=start)

    def _recompute_auth_hash(self, snapshot: AgentQuerySnapshot) -> str:
        """按快照记录的源集合重算授权指纹（会话/意图不入指纹）。"""
        material = {"user": str(getattr(self.user, "id", "")),
                    "role": str(getattr(self.user, "role", ""))}
        for alias, release_info in sorted(
                (snapshot.releases or {}).items()):
            _, profile, _ = build_scope(
                self.db, release_info["ontologyId"],
                release_id=release_info["releaseId"])
            material[alias] = {
                "ontology": release_info["ontologyId"],
                "release": release_info["releaseId"],
                "enabled": bool(profile.enabled),
                "allowedObject": profile.allowed_object_type_ids,
                "allowedLink": profile.allowed_link_type_ids,
                "allowedAction": profile.allowed_action_ids,
                "allowedProperties": getattr(profile, "allowed_properties", None),
                "maxRows": int(profile.max_rows_per_query or 0),
            }
        return _sha256(_canonical_json(material))


def execute_unified_query(db: Session, user: Any, request: QS.QueryRequest, *,
                          session_kind: str, session_id: str) -> dict:
    service = UnifiedQueryService(db, user, request,
                                  session_kind=session_kind,
                                  session_id=session_id)
    try:
        return service.execute()
    except ToolError as exc:
        raise QueryError(403, "capability_forbidden", str(exc)) from exc


def fetch_unified_query_page(db: Session, user: Any, token: str, *,
                             session_kind: str, session_id: str) -> dict:
    placeholder = QS.QueryRequest(
        sources=[QS.QuerySource(alias="a", ontology_id="0",
                                release_id="0", object_type_id="0")],
        select=[QS.SelectItem(source="a", property="id", as_="x")])
    service = UnifiedQueryService(db, user, placeholder,
                                  session_kind=session_kind,
                                  session_id=session_id)
    return service.fetch_page(token)


# ------------------------------------------------- join evidence（P2-1）

def verify_join_evidence(db: Session, user, join, left: _SourceMeta,
                         right: _SourceMeta) -> dict:
    """服务端核验 explicit_key join 的 evidenceRefs（契约 §3.2）。

    - ``structure:{ontologyId}:{releaseId}:{objectTypeId}:{propertyId}``
      必须命中本查询某一侧的发布结构（release/type/property 三者一致）；
      structure 引用只是字段存在的佐证，永不构成充分依据 —— 即使两端
      之间存在 declared link，也不证明任意两个属性可按键相等关联
      （链接语义应走 declared_relation join + link 实例）；
    - ``assertion:{id}`` 必须是本人登记的关联含义声明，且两端
      ontology/type/property 与 keyType 完全匹配 → 标 ``user_asserted``
      （用户经认证入口登记的显式依据；模型自填 businessMeaning 不算），
      是 explicit_key 唯一的充分证据类别；
    - 仅 structure 引用（单侧/双侧）、``fixture:`` 及未知引用 → 422
      ``join_semantics_unverified``。
    """
    categories: list[str] = []
    verified_refs: list[str] = []
    for ref in join.on.evidence_refs:
        parts = str(ref).split(":", 1)
        kind = parts[0]
        if kind == "structure":
            fields = parts[1].split(":") if len(parts) > 1 else []
            if len(fields) != 4:
                raise QueryError(422, "join_semantics_unverified",
                                 f"evidenceRef「{ref}」格式非法")
            ontology_id, release_id, object_type_id, prop = fields
            matched = (
                (ontology_id == left.req.ontology_id
                 and release_id == left.req.release_id
                 and object_type_id == left.ot.id
                 and prop == join.on.left_property)
                or (ontology_id == right.req.ontology_id
                    and release_id == right.req.release_id
                    and object_type_id == right.ot.id
                    and prop == join.on.right_property))
            if not matched:
                raise QueryError(
                    422, "join_semantics_unverified",
                    f"evidenceRef「{ref}」与本次查询的发布结构不一致")
            categories.append("structure")
            verified_refs.append(str(ref))
        elif kind == "assertion":
            from app.ontologies.agent_query.models import AgentJoinAssertion

            assertion = db.query(AgentJoinAssertion).filter(
                AgentJoinAssertion.id == (parts[1] if len(parts) > 1 else ""),
                AgentJoinAssertion.created_by == str(user.id),
            ).first()
            if assertion is None:
                raise QueryError(
                    422, "join_semantics_unverified",
                    f"evidenceRef「{ref}」不存在或不属于当前用户")
            same = (
                assertion.left_ontology_id == left.req.ontology_id
                and assertion.left_object_type_id == left.ot.id
                and assertion.left_property == join.on.left_property
                and assertion.right_ontology_id == right.req.ontology_id
                and assertion.right_object_type_id == right.ot.id
                and assertion.right_property == join.on.right_property
                and assertion.key_type == join.on.key_type)
            if not same:
                raise QueryError(
                    422, "join_semantics_unverified",
                    f"evidenceRef「{ref}」声明的两端/键型与本次 join 不匹配")
            categories.append("user_asserted")
            verified_refs.append(str(ref))
        else:
            raise QueryError(422, "join_semantics_unverified",
                             f"evidenceRef「{ref}」类别未知或无法核验")
    if not verified_refs:
        raise QueryError(422, "join_semantics_unverified",
                         "跨本体 join 缺少可核验的业务键证据")
    if "user_asserted" not in categories:
        raise QueryError(
            422, "join_semantics_unverified",
            "structure 引用只证明字段存在（两端存在 declared link 也不证明"
            "任意属性可按键相等），explicit_key join 需要用户登记的关联含义"
            "声明：请先经 join-assertions 登记两端与键型一致的声明"
            "（assertion 证据），或改用 declared_relation join")
    return {"refs": verified_refs, "categories": categories}
