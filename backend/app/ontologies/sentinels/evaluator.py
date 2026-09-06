"""
哨兵评估器 (Sentinel Evaluator) — 三入口共用的核心

对单个哨兵：解析跨对象绑定(别名 + 链接遍历 + 必要时笛卡尔) → 跨别名条件求值
→ 命中则对 primary 别名对象依次执行绑定的动作列表 → 落 SentinelFiring 日志。

断环：执行哨兵动作期间置上下文标记 in_sentinel_run，CDC 据此抑制由动作回写引发的
即时再触发(同线程内可见)；遗漏的后续条件由定期扫描兜底。
"""
from __future__ import annotations

import ast
import logging
import hashlib
import re
import threading
import time
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _now():
    return datetime.now(timezone.utc)

from app.models.ontology_formal import (
    ActionExecutionLog,
    ActionType,
    LinkInstance,
    LinkType,
    ObjectInstance,
)
from app.models.sentinel import Sentinel, SentinelFiring, SentinelMatchState
from app.services.formal.action_engine import execute_action
from app.services.formal.safe_eval import safe_eval, SafeEvalError
from app.ontologies.release_context import (
    runtime_release_identity,
    runtime_release_version,
)
from app.ontologies.sentinels.cep import temporal_ops

logger = logging.getLogger(__name__)

_ACTIVE_MATCH_STATUSES = {
    "completed", "processing_leave", "pending_leave", "failed_leave",
}

# 执行哨兵动作期间为 True；CDC 用它抑制级联即时再触发(断环)。
in_sentinel_run: ContextVar[bool] = ContextVar("in_sentinel_run", default=False)

MAX_TUPLES = 1000  # 跨对象匹配元组上限，防组合爆炸
_MATCH_SNAPSHOTS_KEY = "__snapshots__"
_MATCH_EVENT_KEY = "__event__"
_MISSING_BINDING = object()
RESERVED_SENTINEL_ALIASES = frozenset({
    "event", "edge", "primary", "target",
    _MATCH_SNAPSHOTS_KEY, _MATCH_EVENT_KEY,
    # Binding filters always inject ``obj`` and safe_eval always injects the
    # utility/literal namespace.  Letting business aliases shadow these names
    # makes the same expression mean different things between filter, condition
    # and runtime replay.
    "obj", "object", "objects", "params", "utils",
    "sum", "avg", "count", "len", "min", "max", "round", "abs",
    "lower", "upper", "contains", "now",
    "True", "False", "None", "true", "false", "null",
    # CEP 时间算子按 per-evaluation scope 注入；业务别名不得遮蔽。
    "changed_within", "prev",
})
_RESERVED_ALIASES = RESERVED_SENTINEL_ALIASES

_LOCKS_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, threading.RLock] = {}


class ReleaseContextChanged(RuntimeError):
    """The current release pointer no longer matches the captured evaluation."""


class SentinelOperationalStateChanged(RuntimeError):
    """A selected Sentinel is no longer executable at the lock boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _guard_expected_release(
        db: Session, ontology_id: str,
        expected_release_id: str | None) -> None:
    if expected_release_id is None:
        return
    from app.models.ontology import OntologyProject
    project = (
        db.query(OntologyProject)
        .filter(OntologyProject.id == ontology_id)
        # Compatible with the CDC worker's dedicated FOR KEY SHARE lease;
        # still blocks a concurrent promotion/rollback FOR UPDATE.
        .with_for_update(key_share=True)
        .populate_existing()
        .first()
    )
    if (
        project is None
        or str(project.current_release_id or "")
        != str(expected_release_id)
    ):
        raise ReleaseContextChanged(
            "当前发布节点已变化，哨兵评估已中止")


def _local_lock(sentinel_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(sentinel_id, threading.RLock())


@contextmanager
def _sentinel_execution_lock(db: Session, sentinel_id: str):
    """Serialize one sentinel across threads and, on PostgreSQL, processes.

    PostgreSQL session advisory locks survive the commits performed by the
    action engine, unlike transaction locks.  They must therefore be owned by
    a dedicated connection for the complete evaluator run.  Acquiring through
    ``Session.execute`` is unsafe: an action may commit, return that physical
    connection to the pool, and make the later unlock run on another pooled
    connection.  The original connection would then retain the lock forever.

    The unique match-state claim remains the final backstop for other database
    dialects.
    """
    lock = _local_lock(sentinel_id)
    lock.acquire()
    advisory_connection = None
    advisory_acquired = False
    try:
        bind = db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            # ``get_bind`` normally returns an Engine.  A Session explicitly
            # bound to a Connection is also supported by opening a sibling
            # connection from its Engine.  Never borrow the business Session's
            # current connection for a session-scoped advisory lock.
            engine = bind if hasattr(bind, "connect") else bind.engine
            advisory_connection = engine.connect()
            advisory_connection.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"),
                {"key": f"sentinel:{sentinel_id}"},
            )
            advisory_acquired = True
        yield
    finally:
        if advisory_acquired and advisory_connection is not None:
            try:
                advisory_connection.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                    {"key": f"sentinel:{sentinel_id}"},
                )
            except Exception:  # closing this connection also releases its locks
                logger.warning("释放哨兵 advisory lock 失败: %s", sentinel_id, exc_info=True)
        if advisory_connection is not None:
            try:
                advisory_connection.close()
            except Exception:
                logger.warning("关闭哨兵 advisory lock 连接失败: %s", sentinel_id,
                               exc_info=True)
        lock.release()


def _blocked_operational_state(code: str, message: str):
    raise SentinelOperationalStateChanged(code, message)


def _released_builtin_for_execution(
        ontology_id: str, release_id: str, snapshot: dict, raw: dict,
        live: Sentinel | None, operational: Sentinel | None,
) -> SimpleNamespace:
    """Materialize a built-in exclusively from its immutable release snapshot."""
    return SimpleNamespace(
        id=str(raw.get("id") or ""),
        ontology_id=ontology_id,
        name=str(raw.get("name") or ""),
        display_name=str(
            raw.get("displayName") or raw.get("name") or ""),
        description=raw.get("description"),
        bindings=deepcopy(raw.get("bindings") or []),
        links=deepcopy(raw.get("links") or []),
        condition=raw.get("condition"),
        condition_rows=deepcopy(raw.get("conditionRows") or []),
        condition_logic=raw.get("conditionLogic") or "and",
        primary_alias=raw.get("primaryAlias"),
        action_ids=deepcopy(raw.get("actionIds") or []),
        action_parameters=deepcopy(raw.get("actionParameters") or {}),
        on_change=bool(raw.get("onChange", True)),
        on_schedule=bool(raw.get("onSchedule", False)),
        scan_interval_seconds=int(raw.get("scanIntervalSeconds") or 300),
        last_scanned_at=(
            live.last_scanned_at if live is not None else None),
        trigger_mode=raw.get("triggerMode") or "on_enter",
        muted=(
            bool(operational.muted)
            if operational is not None
            else bool(raw.get("muted", False))
        ),
        enabled=(
            bool(operational.enabled)
            if operational is not None
            else bool(raw.get("enabled", True))
        ),
        status="published",
        origin="release_builtin",
        bound_release_id=release_id,
        retired_at=None,
        _release_snapshot=snapshot,
        _live_row=live,
    )


def _reload_executable_sentinel(
        db: Session, ontology_id: str, selected,
        expected_release_id: str | None,
):
    """Reload and authorize a selected Sentinel while its operation lock is held.

    Engine selection is only an optimization.  A management write may commit
    after selection but before this evaluator obtains the advisory lock, so the
    live operational row and immutable release membership are re-read here.
    Legacy direct evaluator callers without a release pointer retain their
    historical unscoped behavior; production entry points always provide the
    captured current release id.
    """
    if expected_release_id is None:
        return selected

    from app.models.ontology_version import OntologyVersion
    from app.ontologies.versions.snapshot_contract import (
        complete_snapshot,
    )

    selected_id = str(getattr(selected, "id", "") or "")
    selected_origin = str(
        getattr(selected, "origin", None) or "release_builtin")
    selected_revision = getattr(selected, "definition_revision", None)

    release = (
        db.query(OntologyVersion)
        .filter(
            OntologyVersion.id == expected_release_id,
            OntologyVersion.ontology_id == ontology_id,
            OntologyVersion.node_kind == "release",
            OntologyVersion.lifecycle_status == "released",
        )
        .populate_existing()
        .first()
    )
    if release is None:
        _blocked_operational_state(
            "current_release_invalid",
            "当前发布指针未指向有效发布快照，哨兵执行已阻断")
    snapshot = complete_snapshot(release.snapshot_formal)

    if selected_origin == "release_builtin":
        # Built-in structure and fallback operational defaults belong to the
        # immutable release.  A missing/draft live row is the next-editing
        # projection and must not stop or rewrite the current release.
        raw = next(
            (
                item for item in snapshot.get("sentinels") or []
                if isinstance(item, dict)
                and str(item.get("id") or "") == selected_id
            ),
            None,
        )
        if raw is None:
            _blocked_operational_state(
                "released_sentinel_missing",
                "哨兵不在当前发布快照中，执行已阻断")
        live = (
            db.query(Sentinel)
            .filter(
                Sentinel.id == selected_id,
                Sentinel.ontology_id == ontology_id,
            )
            .populate_existing()
            .first()
        )
        operational = (
            live
            if live is not None
            and live.origin == "release_builtin"
            and live.status == "published"
            else None
        )
        if operational is not None and operational.retired_at is not None:
            _blocked_operational_state(
                "sentinel_retired",
                "当前发布哨兵已退役，旧选择不能继续执行")
        effective_enabled = (
            bool(operational.enabled)
            if operational is not None
            else bool(raw.get("enabled", True))
        )
        if not effective_enabled:
            _blocked_operational_state(
                "sentinel_disabled",
                "当前发布哨兵已停用，旧选择不能继续执行")
        return _released_builtin_for_execution(
            ontology_id, expected_release_id, snapshot, raw, live, operational)

    if selected_origin != "assistant_dynamic":
        _blocked_operational_state(
            "sentinel_origin_invalid",
            "哨兵来源非法，执行已阻断")
    live = (
        db.query(Sentinel)
        .filter(
            Sentinel.id == selected_id,
            Sentinel.ontology_id == ontology_id,
        )
        .populate_existing()
        .first()
    )
    if live is None:
        _blocked_operational_state(
            "sentinel_not_found",
            "动态哨兵实时投影已不存在，旧选择不能继续执行")
    if str(live.origin or "") != selected_origin:
        _blocked_operational_state(
            "sentinel_origin_changed",
            "动态哨兵管理来源已变化，旧选择不能继续执行")
    if live.status != "published":
        _blocked_operational_state(
            "sentinel_not_published",
            "动态哨兵实时状态不是发布态，执行已阻断")
    if not bool(live.enabled):
        _blocked_operational_state(
            "sentinel_disabled",
            "动态哨兵已停用，旧选择不能继续执行")
    if live.retired_at is not None:
        _blocked_operational_state(
            "sentinel_retired",
            "动态哨兵已退役，旧选择不能继续执行")
    if str(live.bound_release_id or "") != str(expected_release_id):
        _blocked_operational_state(
            "dynamic_sentinel_release_mismatch",
            "动态哨兵未精确绑定当前发布版本，执行已阻断")
    try:
        live_revision = int(live.definition_revision)
        candidate_revision = int(selected_revision)
    except (TypeError, ValueError):
        _blocked_operational_state(
            "dynamic_sentinel_revision_invalid",
            "动态哨兵定义修订号无效，执行已阻断")
    if candidate_revision != live_revision:
        _blocked_operational_state(
            "dynamic_sentinel_definition_changed",
            "动态哨兵定义在选择后已变化，旧选择不能继续执行")
    validation = (
        live.validation_report
        if isinstance(live.validation_report, dict) else {}
    )
    trial = (
        live.last_trial_report
        if isinstance(live.last_trial_report, dict) else {}
    )
    if (
        not validation.get("passed")
        or not trial.get("passed")
        or str(live.last_trial_release_id or "")
        != str(expected_release_id)
        or live.last_trial_revision != live_revision
    ):
        _blocked_operational_state(
            "dynamic_sentinel_trial_required",
            "动态哨兵当前定义尚未在当前发布版本通过试跑，执行已阻断")
    live._release_snapshot = snapshot
    live._live_row = live
    return live


def _instance_query(db: Session, ontology_id: str, object_type_id: str,
                    release_id: str | None = None):
    query = db.query(ObjectInstance).filter(
        ObjectInstance.ontology_id == ontology_id,
        ObjectInstance.object_type_id == object_type_id,
    )
    if release_id is not None:
        query = query.filter(ObjectInstance.ontology_release_id == release_id)
    return query.order_by(ObjectInstance.id)


def _instance_values(instance) -> dict:
    """One authoritative runtime view for stored and derived properties."""
    return {
        **(getattr(instance, "properties", None) or {}),
        **(getattr(instance, "computed", None) or {}),
    }


def _missing_expression_properties(expr: str, scope: dict[str, dict]) -> list[str]:
    """Find direct alias property reads that are absent from this real row.

    ``safe_eval`` intentionally maps a missing dictionary attribute to ``None``
    for general ontology functions.  Sentinel observations need stricter
    semantics: ``a.missing != 'closed'`` must not silently become true and fire
    an automation, nor may it make a prior match look like a leave.
    """
    try:
        tree = ast.parse((expr or "").strip().rstrip(";"), mode="eval")
    except SyntaxError:
        return []
    missing: set[str] = set()
    for node in ast.walk(tree):
        alias = None
        prop = None
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            alias, prop = node.value.id, node.attr
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            alias, prop = node.value.id, node.slice.value
        if alias in scope and prop not in scope[alias]:
            missing.add(f"{alias}.{prop}")
    return sorted(missing)


def _passes(expr: str | None, alias: str, props: dict,
            errors: list[str] | None = None) -> bool:
    if not expr:
        return True
    if not isinstance(expr, str):
        if errors is not None and len(errors) < 5:
            errors.append("绑定过滤必须是字符串表达式")
        return False
    scope = {alias: props, "obj": props}
    missing = _missing_expression_properties(expr, scope)
    if missing:
        if errors is not None and len(errors) < 5:
            errors.append(
                f"绑定过滤「{expr}」引用当前数据不存在的属性: {', '.join(missing)}")
        return False
    try:
        return bool(safe_eval(expr, scope))
    except SafeEvalError as e:
        if errors is not None and len(errors) < 5:
            errors.append(f"绑定过滤「{expr}」求值失败: {e}")
        return False


def _traverse(db: Session, ontology_id: str, link_type_id: str,
              instance_id: str, forward: bool, target_type: str,
              release_id: str | None = None):
    endpoint = (
        LinkInstance.target_object_id
        if forward else LinkInstance.source_object_id
    )
    anchor = (
        LinkInstance.source_object_id
        if forward else LinkInstance.target_object_id
    )
    query = db.query(ObjectInstance).join(
        LinkInstance, endpoint == ObjectInstance.id,
    ).filter(
        LinkInstance.ontology_id == ontology_id,
        LinkInstance.link_type_id == link_type_id,
        anchor == instance_id,
        ObjectInstance.object_type_id == target_type,
        ObjectInstance.ontology_id == ontology_id,
    )
    if release_id is not None:
        query = query.filter(
            LinkInstance.ontology_release_id == release_id,
            ObjectInstance.ontology_release_id == release_id,
        )
    # PG 的 json 类型没有等值操作符：实体级 DISTINCT 会 SELECT 全部列（含
    # properties/computed），PostgreSQL 直接 UndefinedFunction（2026-08-30
    # 云端哨兵评估死信事故）。join 去重必须落在主键上——GROUP BY 主键在
    # PG 走函数依赖规则、SQLite 天然允许裸列，两方言行为一致。
    return query.group_by(ObjectInstance.id).order_by(ObjectInstance.id).yield_per(500)


def _link_exists(db: Session, ontology_id: str, link: dict, tup: dict,
                 release_id: str | None = None) -> bool:
    source = tup.get(link.get("from"))
    target = tup.get(link.get("to"))
    if source is None or target is None:
        return True
    query = db.query(LinkInstance.id).filter(
        LinkInstance.ontology_id == ontology_id,
        LinkInstance.link_type_id == link.get("linkTypeId"),
        LinkInstance.source_object_id == source.id,
        LinkInstance.target_object_id == target.id,
    )
    if release_id is not None:
        query = query.filter(LinkInstance.ontology_release_id == release_id)
    return query.first() is not None


def _all_bound_links_hold(db: Session, ontology_id: str, links: list,
                          tup: dict, release_id: str | None = None) -> bool:
    return all(
        _link_exists(db, ontology_id, link, tup, release_id)
        for link in links
        if link.get("from") in tup and link.get("to") in tup
    )


def _sentinel_release_snapshot(
        db: Session, ontology_id: str, sentinel: Sentinel,
        release_id: str | None) -> dict | None:
    """Return the immutable definition snapshot for a release-scoped run.

    Runtime sentinels normally receive this snapshot from ``engine.py``.  The
    database lookup is a fail-closed fallback for direct evaluator callers
    (including assistant trial): a release-scoped evaluation must never fall
    back to the mutable formal-model tables.
    """
    if release_id is None:
        return None
    attached = getattr(sentinel, "_release_snapshot", None)
    if isinstance(attached, dict):
        return attached

    from app.models.ontology_version import OntologyVersion
    release = db.query(OntologyVersion).filter(
        OntologyVersion.id == release_id,
        OntologyVersion.ontology_id == ontology_id,
        OntologyVersion.node_kind == "release",
        OntologyVersion.lifecycle_status == "released",
    ).first()
    if release is None or not isinstance(release.snapshot_formal, dict):
        return None
    snapshot = release.snapshot_formal
    # SQLAlchemy model rows and the runtime SimpleNamespace both permit an
    # ephemeral attribute.  Do not persist it: it is only a per-run cache.
    sentinel._release_snapshot = snapshot
    return snapshot


def _resolve_tuples(db: Session, ontology_id: str, sentinel: Sentinel,
                    errors: list[str] | None = None,
                    release_id: str | None = None,
                    metadata: dict | None = None) -> list[dict]:
    """Resolve complete tuples and fail visibly instead of returning a partial set.

    Every link whose two endpoints are bound is enforced.  The previous
    first-link-only expansion could pass a candidate that violated another
    declared relationship and disagreed with release trials.
    """
    bindings = sentinel.bindings or []
    if not bindings:
        if errors is not None:
            errors.append("哨兵至少需要一个对象绑定")
        return []
    links = sentinel.links or []
    if metadata is not None:
        metadata.setdefault("candidateCapReached", False)
    binding_aliases = [
        item.get("alias") for item in bindings
        if isinstance(item, dict) and isinstance(item.get("alias"), str)
    ]
    invalid_bindings = [
        item for item in bindings
        if not isinstance(item, dict)
        or not isinstance(item.get("alias"), str)
        or not item.get("alias")
        or not item.get("objectTypeId")
        or item.get("alias") in _RESERVED_ALIASES
    ]
    if invalid_bindings or len(binding_aliases) != len(set(binding_aliases)):
        if errors is not None:
            errors.append(
                "哨兵 bindings 缺少 alias/objectTypeId、使用保留别名或 alias 重复")
        return []
    aliases = {
        item.get("alias") for item in bindings
        if isinstance(item, dict) and item.get("alias")
    }
    if sentinel.primary_alias and sentinel.primary_alias not in aliases:
        if errors is not None:
            errors.append(
                f"哨兵 primaryAlias 不在 bindings 中: {sentinel.primary_alias}")
        return []
    invalid_links = [
        link for link in links
        if not isinstance(link, dict)
        or link.get("from") not in aliases
        or link.get("to") not in aliases
        or not link.get("linkTypeId")
    ]
    if invalid_links:
        if errors is not None:
            errors.append("哨兵 links 包含无效别名或缺少 linkTypeId")
        return []
    if links:
        binding_types = {
            item.get("alias"): item.get("objectTypeId") for item in bindings
        }
        link_type_ids = {str(link["linkTypeId"]) for link in links}
        if release_id is not None:
            snapshot = _sentinel_release_snapshot(
                db, ontology_id, sentinel, release_id)
            if snapshot is None:
                if errors is not None:
                    errors.append("当前发布快照不存在，无法校验关系端点")
                return []
            try:
                from app.ontologies.versions.snapshot_contract import (
                    snapshot_models,
                )
                link_types = {
                    item.id: item
                    for item in snapshot_models(snapshot)["linkTypes"]
                    if item.id in link_type_ids
                }
            except Exception:  # noqa: BLE001
                logger.exception(
                    "哨兵无法解析发布快照关系定义: ontology=%s "
                    "sentinel=%s release=%s",
                    ontology_id, sentinel.id, release_id)
                if errors is not None:
                    errors.append("当前发布快照关系定义无法解析")
                return []
        else:
            # Legacy projects without a release pointer retain their historical
            # live-table behavior.  Published runtime always takes the branch
            # above and therefore cannot be polluted by draft edits.
            link_types = {
                row.id: row for row in db.query(LinkType).filter(
                    LinkType.ontology_id == ontology_id,
                    LinkType.id.in_(link_type_ids),
                ).all()
            }
        invalid_contracts: list[str] = []
        for link in links:
            link_type = link_types.get(str(link["linkTypeId"]))
            if link_type is None:
                invalid_contracts.append(
                    f"关系类型不存在: {link['linkTypeId']}")
                continue
            if (
                link_type.source_object_type_id
                != binding_types.get(link.get("from"))
                or link_type.target_object_type_id
                != binding_types.get(link.get("to"))
            ):
                invalid_contracts.append(
                    f"关系方向或端点类型不一致: {link['linkTypeId']}")
        if invalid_contracts:
            if errors is not None:
                errors.extend(invalid_contracts[:5])
            return []

    def append_candidate(bucket: list[dict], item: dict) -> bool:
        bucket.append(item)
        if len(bucket) <= MAX_TUPLES:
            return True
        if metadata is not None:
            metadata["candidateCapReached"] = True
        if errors is not None and not any("安全上限" in error for error in errors):
            errors.append(
                f"哨兵候选组合超过安全上限 {MAX_TUPLES}，"
                "本轮已阻断执行，请收窄绑定过滤条件")
        bucket.pop()
        return False

    b0 = bindings[0]
    tuples: list[dict] = []
    for inst in _instance_query(
            db, ontology_id, b0["objectTypeId"], release_id).yield_per(500):
        if _passes(b0.get("filter"), b0["alias"], _instance_values(inst), errors):
            if not append_candidate(tuples, {b0["alias"]: inst}):
                return tuples

    for b in bindings[1:]:
        alias, otype, filt = b["alias"], b["objectTypeId"], b.get("filter")
        new_tuples: list[dict] = []
        for t in tuples:
            applicable = [
                link for link in links
                if (
                    link.get("to") == alias and link.get("from") in t
                ) or (
                    link.get("from") == alias and link.get("to") in t
                )
            ]
            if applicable:
                seed = applicable[0]
                if seed.get("to") == alias:
                    related = _traverse(
                        db, ontology_id, seed["linkTypeId"],
                        t[seed["from"]].id, forward=True,
                        target_type=otype, release_id=release_id)
                else:
                    related = _traverse(
                        db, ontology_id, seed["linkTypeId"],
                        t[seed["to"]].id, forward=False,
                        target_type=otype, release_id=release_id)
            else:
                related = _instance_query(
                    db, ontology_id, otype, release_id).yield_per(500)
            for candidate in related:
                if not _passes(
                        filt, alias, _instance_values(candidate), errors):
                    continue
                joined = {**t, alias: candidate}
                if not _all_bound_links_hold(
                        db, ontology_id, links, joined, release_id):
                    continue
                if not append_candidate(new_tuples, joined):
                    return new_tuples
        tuples = new_tuples
        if not tuples:
            break
    return tuples


def _holds(expr: str | None, tup: dict, errors: list[str] | None = None,
           temporal: dict | None = None) -> bool:
    """条件求值。求值失败视为不命中（fail-closed），但错误必须被记录并
    展示到触发日志——写错的条件不能表现为"永远静默 no_match"。

    ``temporal`` 为该元组的时间算子闭包（changed_within/prev，见
    temporal_ops）；仅哨兵 condition 携带时间引用时注入。
    """
    if not expr:
        return True
    if not isinstance(expr, str):
        if errors is not None and len(errors) < 5:
            errors.append("哨兵 condition 必须是字符串表达式")
        return False
    scope = {alias: _instance_values(inst) for alias, inst in tup.items()}
    if temporal:
        scope.update(temporal)
    missing = _missing_expression_properties(expr, scope)
    if missing:
        if errors is not None and len(errors) < 5:
            errors.append(
                f"条件「{expr}」引用当前数据不存在的属性: {', '.join(missing)}")
        return False
    try:
        return bool(safe_eval(expr, scope))
    except SafeEvalError as e:
        if errors is not None and len(errors) < 5:
            errors.append(f"条件「{expr}」求值失败: {e}")
        return False


def _match_key(tup: dict, primary: str | None) -> str:
    """命中键:跨对象时为整个元组的稳定签名,单对象时即 primary 实例 id。"""
    if len(tup) <= 1 and primary and primary in tup:
        return tup[primary].id
    return "|".join(f"{a}={tup[a].id}" for a in sorted(tup))


_PARAM_TEMPLATE = re.compile(
    r"\{\{\s*(?P<alias>[^.\s{}]+)\.(?P<property>[^{}\s]+)\s*\}\}"
)


def _binding_instance(tup: dict, alias: str | None, primary: str | None):
    resolved = primary if alias in (None, "", "primary", "target") else alias
    return tup.get(resolved) if resolved else None


def _event_binding_value(event: dict, prop: str):
    if prop in event:
        return event[prop], None
    return _MISSING_BINDING, f"事件参数不存在: {prop}"


def _template_binding_value(alias: str, prop: str, tup: dict,
                            primary: str | None, event: dict):
    if alias in ("event", "edge"):
        return _event_binding_value(event, prop)
    inst = _binding_instance(tup, alias, primary)
    if inst is None:
        return _MISSING_BINDING, f"参数绑定找不到别名 {alias}"
    if prop == "id":
        return inst.id, None
    values = _instance_values(inst)
    if prop not in values:
        return _MISSING_BINDING, f"参数绑定属性不存在: {prop}"
    return values[prop], None


def _resolve_parameter_binding(spec, tup: dict, primary: str | None,
                               event: dict):
    """Resolve safe literals, match properties and immutable edge context."""
    if isinstance(spec, str):
        full_match = _PARAM_TEMPLATE.fullmatch(spec)
        if full_match:
            return _template_binding_value(
                full_match.group("alias"), full_match.group("property"),
                tup, primary, event)
        matches = list(_PARAM_TEMPLATE.finditer(spec))
        if not matches:
            if "{{" in spec or "}}" in spec:
                return _MISSING_BINDING, f"参数模板格式无效: {spec}"
            return spec, None
        rendered: list[str] = []
        cursor = 0
        for match in matches:
            rendered.append(spec[cursor:match.start()])
            value, error = _template_binding_value(
                match.group("alias"), match.group("property"),
                tup, primary, event)
            if error:
                return _MISSING_BINDING, error
            rendered.append("" if value is None else str(value))
            cursor = match.end()
        rendered.append(spec[cursor:])
        result = "".join(rendered)
        if "{{" in result or "}}" in result:
            return _MISSING_BINDING, f"参数模板格式无效: {spec}"
        return result, None

    if not isinstance(spec, dict):
        return spec, None

    source = spec.get("sourceType", spec.get("source"))
    if isinstance(source, str):
        source = source.strip().lower().replace("-", "_")
    if source not in (
        "constant", "literal", "property", "match", "match_property",
        "target_id", "primary_id", "event", "event_property", "edge",
    ):
        # Plain dicts remain valid literal values for object parameters.
        return spec, None
    if source in ("constant", "literal"):
        if "value" in spec:
            return spec.get("value"), None
        return spec.get("sourceValue"), None
    if source in ("event", "event_property", "edge"):
        prop = spec.get("property", spec.get("sourceValue"))
        if source == "edge" and not prop:
            prop = "edge"
        if not prop:
            return _MISSING_BINDING, "event 参数绑定缺少 property"
        return _event_binding_value(event, str(prop))
    inst = _binding_instance(tup, spec.get("alias"), primary)
    if inst is None:
        return _MISSING_BINDING, f"参数绑定找不到别名 {spec.get('alias') or primary}"
    if source in ("target_id", "primary_id"):
        return inst.id, None
    prop = spec.get("property", spec.get("sourceValue"))
    if not prop:
        return _MISSING_BINDING, "property/match 参数绑定缺少 property"
    if prop == "id":
        return inst.id, None
    values = _instance_values(inst)
    if prop not in values:
        return _MISSING_BINDING, f"参数绑定属性不存在: {prop}"
    return values[prop], None


def _configured_action_parameters(sentinel: Sentinel, action_id: str, tup: dict,
                                  primary: str | None, *,
                                  action: ActionType | None = None,
                                  event: dict | None = None) -> tuple[dict, list[str]]:
    all_config = sentinel.action_parameters or {}
    if not isinstance(all_config, dict):
        return {}, ["哨兵 actionParameters 顶层必须是对象"]
    configured = all_config.get(action_id, {})
    if configured is None:
        configured = {}
    if not isinstance(configured, dict):
        return {}, [f"动作 {action_id} 的 actionParameters 必须是对象"]
    definitions = {
        str(item.get("name")): item
        for item in (
            (action.parameters or []) if action is not None else []
        )
        if isinstance(item, dict) and item.get("name")
    }
    params: dict = {}
    errors: list[str] = []
    for name, spec in configured.items():
        parameter_name = str(name)
        value, error = _resolve_parameter_binding(
            spec, tup, primary, event or {})
        if error:
            definition = definitions.get(parameter_name)
            has_default = bool(definition) and any(
                key in definition
                for key in ("defaultValue", "default_value", "default")
            )
            if value is _MISSING_BINDING and definition and (
                    has_default or not definition.get("required")):
                continue
            errors.append(f"参数「{parameter_name}」: {error}")
        else:
            params[parameter_name] = value
    return params, errors


def preview_sentinel(db: Session, ontology_id: str, sentinel: Sentinel,
                     release_id: str) -> dict:
    """Evaluate the complete current-release dataset without runtime effects.

    It calls Action Engine only in explicit preview-only mode, which validates
    the complete action plan without ActionLog/Fact/Notification/network writes.
    Cross-object combinations retain the engine's hard safety cap; hitting it
    makes the trial fail instead of presenting a partial run as complete.
    """
    started = time.time()
    errors: list[str] = []
    metadata: dict = {"candidateCapReached": False}
    tuples = _resolve_tuples(
        db, ontology_id, sentinel, errors,
        release_id=release_id, metadata=metadata,
    )
    matched = _match_tuples_with_temporal(
        db, sentinel.condition, tuples, errors)
    primary = sentinel.primary_alias or (
        sentinel.bindings[0].get("alias") if sentinel.bindings else None)
    try:
        from app.models.ontology_version import OntologyVersion
        from app.ontologies.versions.snapshot_contract import (
            snapshot_models,
        )
        release = db.query(OntologyVersion).filter(
            OntologyVersion.id == release_id,
            OntologyVersion.ontology_id == ontology_id,
            OntologyVersion.node_kind == "release",
            OntologyVersion.lifecycle_status == "released",
        ).first()
        if release is None:
            raise ValueError("当前发布快照不存在")
        actions = {
            row.id: row
            for row in snapshot_models(
                release.snapshot_formal or {})["actions"]
        }
    except Exception:  # noqa: BLE001
        logger.exception(
            "动态哨兵试跑无法加载发布快照动作定义: ontology=%s release=%s",
            ontology_id, release_id)
        actions = {}
        errors.append("当前发布快照动作定义无法加载")
    planned_samples: list[dict] = []
    parameter_error_count = 0
    for tup in matched:
        target = _binding_instance(tup, primary, primary)
        target_id = target.id if target is not None else None
        match = {alias: instance.id for alias, instance in tup.items()}
        event = {
            "edge": "enter",
            "matchKey": _match_key(tup, primary),
            "occurredAt": _now().isoformat(),
            "sentinelId": sentinel.id,
            "sentinelName": sentinel.display_name,
        }
        for action_id in sentinel.action_ids or []:
            action = actions.get(action_id)
            edges = ["enter"]
            if sentinel.trigger_mode == "on_enter_leave":
                edges.append("leave")
            for edge in edges:
                parameters, binding_errors = _configured_action_parameters(
                    sentinel, action_id, tup, primary,
                    action=action, event={**event, "edge": edge})
                if action is None:
                    binding_errors.append(f"动作不存在: {action_id}")
                elif action.object_type_id and (
                        target is None
                        or target.object_type_id != action.object_type_id):
                    binding_errors.append(
                        f"动作 {action.display_name or action.name} "
                        "的目标类型与命中对象不一致")
                if action is not None:
                    from app.services.formal.action_engine import (
                        prepare_action_parameters,
                    )
                    parameters, parameter_errors = (
                        prepare_action_parameters(action, parameters)
                    )
                    binding_errors.extend(parameter_errors)
                parameter_error_count += len(binding_errors)
                if len(errors) < 20:
                    errors.extend(
                        (
                            f"{edge} 参数: {item}"
                            if edge == "leave" else item
                        )
                        for item in binding_errors[:20 - len(errors)]
                    )
                preview = {
                    "status": "failed",
                    "effects": [],
                    "validationErrors": binding_errors,
                    "errorMessage": (
                        "; ".join(binding_errors)
                        if binding_errors else "动作不存在"),
                }
                if action is not None:
                    body = SimpleNamespace(
                        action_id=action_id,
                        parameters=parameters,
                        target_instance_id=target_id,
                        dry_run=True,
                        target_snapshot=None,
                        idempotency_key=None,
                        sentinel_match_state_id=None,
                        sentinel_id=sentinel.id,
                        expected_release_id=release_id,
                        preview_only=True,
                    )
                    preview = execute_action(
                        db, ontology_id, body,
                        preview_only=True,
                        expected_release_id=release_id,
                    )
                    if preview.get("status") != "success":
                        action_errors = list(
                            preview.get("validationErrors") or [])
                        if (
                            preview.get("errorMessage")
                            and preview.get("errorMessage")
                            not in action_errors
                        ):
                            action_errors.append(
                                str(preview["errorMessage"]))
                        if len(errors) < 20:
                            errors.extend(
                                (
                                    f"{edge} 动作: {item}"
                                    if edge == "leave" else str(item)
                                )
                                for item in action_errors[
                                    :20 - len(errors)]
                            )
                if len(planned_samples) < 200:
                    planned_samples.append({
                        "actionId": action_id,
                        "actionName": (
                            action.display_name or action.name
                            if action is not None else action_id
                        ),
                        "edge": edge,
                        "targetInstanceId": target_id,
                        "match": match,
                        "parameters": parameters,
                        "status": preview.get("status"),
                        "effects": preview.get("effects") or [],
                        "validationErrors": [
                            *binding_errors,
                            *[
                                item for item in (
                                    preview.get(
                                        "validationErrors") or [])
                                if item not in binding_errors
                            ],
                        ],
                        "errorMessage": preview.get("errorMessage"),
                        "sideEffects": "none",
                    })
    if metadata["candidateCapReached"]:
        errors.append(
            f"跨对象候选组合超过安全上限 {MAX_TUPLES}，请收窄绑定过滤条件后重试")
    edge_count = 2 if sentinel.trigger_mode == "on_enter_leave" else 1
    total_actions = (
        len(matched) * len(sentinel.action_ids or []) * edge_count)
    return {
        "passed": not errors and not metadata["candidateCapReached"],
        "releaseId": release_id,
        "candidateCount": len(tuples),
        "matchCount": len(matched),
        "plannedActionCount": total_actions,
        "plannedActions": planned_samples,
        "plannedActionsTruncated": total_actions > len(planned_samples),
        "parameterErrorCount": parameter_error_count,
        "candidateCapReached": bool(metadata["candidateCapReached"]),
        "errors": errors[:20],
        "durationMs": int((time.time() - started) * 1000),
        "sideEffects": "none",
    }


def _action_idempotency_key(sentinel: Sentinel, state: SentinelMatchState,
                            match_key: str, edge: str, action_id: str) -> str:
    """Stable for one edge lifecycle, distinct after leave/re-enter or run_on_all.

    The digest includes the explicit business tuple requested by the runtime
    contract plus the durable claim/epoch.  The latter is what lets a real
    leave→re-enter transition execute again without losing crash-retry safety.
    """
    material = "\x00".join([
        sentinel.id, match_key, edge, action_id, state.id,
        str(state.execution_epoch or 0),
    ])
    return f"sentinel:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _run_actions(db: Session, ontology_id: str, sentinel: Sentinel,
                 tup: dict, primary: str | None, edge: str,
                 match_key: str, state: SentinelMatchState,
                 results: list, *,
                 expected_release_id: str | None = None) -> tuple[bool, str]:
    """Run one edge fail-fast; only all-success may consume match state."""
    action_ids = list(sentinel.action_ids or [])
    if not action_ids:
        return True, "no_actions"
    invalid_action_ids = [a for a in action_ids if not isinstance(a, str) or not a]
    if invalid_action_ids:
        invalid_action_id = invalid_action_ids[0]
        results.append({
            "actionId": str(invalid_action_id), "targetInstanceId": None,
            "edge": edge, "matchKey": match_key, "status": "failed",
            "errorMessage": "actionIds 中存在非法动作 ID", "effects": [],
            "validationErrors": ["invalid_action_id"],
        })
        return False, "failed"
    if len(action_ids) != len(set(action_ids)):
        results.append({
            "actionId": "", "targetInstanceId": None, "edge": edge,
            "matchKey": match_key, "status": "failed",
            "errorMessage": "actionIds 不允许重复；重复动作会破坏幂等语义",
            "effects": [], "validationErrors": ["duplicate_action_id"],
        })
        return False, "failed"
    target = _binding_instance(tup, primary, primary)
    target_id = target.id if target is not None else None
    stored_event = (
        state.match_detail.get(_MATCH_EVENT_KEY, {})
        if isinstance(state.match_detail, dict) else {}
    )
    event = {
        "edge": edge,
        "matchKey": match_key,
        "occurredAt": (
            stored_event.get("occurredAt")
            or (
                state.first_seen_at.isoformat()
                if state.first_seen_at is not None else _now().isoformat()
            )
        ),
        "sentinelId": sentinel.id,
        "sentinelName": sentinel.display_name,
    }
    entries: list[tuple[str, ActionType, SimpleNamespace]] = []
    frozen_actions: dict[str, ActionType] | None = None
    if expected_release_id is not None:
        try:
            from app.models.ontology_version import OntologyVersion
            from app.ontologies.versions.snapshot_contract import (
                snapshot_models,
            )
            release = db.query(OntologyVersion).filter(
                OntologyVersion.id == expected_release_id,
                OntologyVersion.ontology_id == ontology_id,
                OntologyVersion.node_kind == "release",
                OntologyVersion.lifecycle_status == "released",
            ).first()
            if release is None:
                raise ValueError("发布快照不存在")
            frozen_actions = {
                item.id: item
                for item in snapshot_models(
                    release.snapshot_formal or {})["actions"]
            }
        except Exception:  # noqa: BLE001
            logger.exception(
                "哨兵动作链无法加载发布快照: ontology=%s release=%s",
                ontology_id, expected_release_id)
            results.append({
                "actionId": "", "targetInstanceId": target_id,
                "edge": edge, "matchKey": match_key,
                "status": "failed",
                "errorMessage": "发布快照动作定义无法加载",
                "effects": [],
                "validationErrors": ["release_definition_invalid"],
            })
            return False, "failed"
    for aid in action_ids:
        action = (
            frozen_actions.get(aid)
            if frozen_actions is not None else
            db.query(ActionType).filter(
                ActionType.id == aid,
                ActionType.ontology_id == ontology_id,
            ).first()
        )
        if action is None:
            results.append({
                "actionId": aid, "targetInstanceId": target_id, "edge": edge,
                "matchKey": match_key, "status": "failed",
                "errorMessage": "动作不存在", "effects": [],
                "validationErrors": ["missing_action"],
            })
            return False, "failed"
        params, binding_errors = _configured_action_parameters(
            sentinel, aid, tup, primary, action=action, event=event)
        from app.services.formal.action_engine import prepare_action_parameters
        params, parameter_errors = prepare_action_parameters(action, params)
        binding_errors.extend(parameter_errors)
        if action.object_type_id and (
                target is None
                or target.object_type_id != action.object_type_id):
            binding_errors.append(
                f"动作 {action.display_name or action.name} "
                "的目标类型与命中对象不一致")
        if binding_errors:
            results.append({
                "actionId": aid, "targetInstanceId": target_id, "edge": edge,
                "matchKey": match_key,
                "status": "failed", "errorMessage": "; ".join(binding_errors),
                "effects": [], "validationErrors": binding_errors,
            })
            return False, "failed"
        body = SimpleNamespace(
            action_id=aid, parameters=params,
            target_instance_id=target_id, dry_run=False,
            target_snapshot=({
                "id": target.id,
                "objectTypeId": target.object_type_id,
                "properties": deepcopy(
                    getattr(target, "properties", None) or {}),
                "computed": deepcopy(
                    getattr(target, "computed", None) or {}),
            } if edge == "leave" and target is not None else None),
            idempotency_key=_action_idempotency_key(
                sentinel, state, match_key, edge, aid),
            sentinel_match_state_id=state.id,
            sentinel_id=sentinel.id,
            expected_release_id=expected_release_id,
        )
        entries.append((aid, action, body))

    def _execute(aid: str, body: SimpleNamespace):
        try:
            return execute_action(
                db, ontology_id, body,
                expected_release_id=expected_release_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("哨兵动作执行抛出未封装异常: %s", aid)
            return {
                "status": "failed",
                "errorMessage": "动作执行出现内部错误，请检查服务端日志",
                "effects": [],
                "validationErrors": ["sentinel_action_internal_error"],
            }

    def _append(aid: str, log: dict) -> str:
        status = str(log.get("status") or "failed")
        item = {
            "actionId": aid, "targetInstanceId": target_id, "edge": edge,
            "matchKey": match_key,
            "status": status, "logId": log.get("id"),
            "idempotentReplay": bool(log.get("idempotentReplay")),
            "effects": log.get("effects", []),
            "errorMessage": log.get("errorMessage"),
            "validationErrors": log.get("validationErrors", []),
        }
        if log.get("pendingApproval"):
            item["pendingApproval"] = True
        if log.get("approvalExecuting"):
            item["approvalExecuting"] = True
            item["approvalLogStatus"] = log.get("approvalLogStatus")
        results.append(item)
        return status

    # HITL is a gate for the *whole* chain.  Do not commit earlier automatic
    # effects and only then discover that a later step is pending/rejected.
    # Approved gates resolve through their durable idempotency owner and let the
    # normal pass continue; the first unresolved gate stops before side effects.
    for aid, action, body in entries:
        if not action.requires_approval:
            continue
        gate_log = _execute(aid, body)
        gate_status = str(gate_log.get("status") or "failed")
        if gate_status != "success":
            _append(aid, gate_log)
            return False, "pending" if gate_status == "pending" else "failed"

    for aid, _action, body in entries:
        status = _append(aid, _execute(aid, body))
        if status != "success":
            return False, "pending" if status == "pending" else "failed"
    return True, "success"


def _claim_match_state(db: Session, ontology_id: str, sentinel: Sentinel,
                       key: str, tup: dict, now: datetime,
                       *, new_cycle: bool = False,
                       expected_release_id: str | None = None
                       ) -> SentinelMatchState | None:
    """Claim/recover an enter edge; uniqueness arbitrates concurrent workers."""
    existing = db.query(SentinelMatchState).filter(
        SentinelMatchState.sentinel_id == sentinel.id,
        SentinelMatchState.match_key == key,
    ).first()
    if existing is not None:
        previous_event = (
            (existing.match_detail or {}).get(_MATCH_EVENT_KEY)
            if not new_cycle else None
        )
        if new_cycle and existing.runtime_status == "completed":
            existing.execution_epoch = int(existing.execution_epoch or 0) + 1
        existing.runtime_status = "processing_enter"
        existing.match_detail = _snapshot_match_detail(
            tup, edge="enter", match_key=key, occurred_at=now,
            previous_event=previous_event,
            expected_release_id=expected_release_id,
            sentinel=sentinel)
        existing.last_seen_at = now
        db.commit()
        db.refresh(existing)
        return existing

    state = SentinelMatchState(
        ontology_id=ontology_id, sentinel_id=sentinel.id, match_key=key,
        match_detail=_snapshot_match_detail(
            tup, edge="enter", match_key=key, occurred_at=now,
            expected_release_id=expected_release_id,
            sentinel=sentinel),
        runtime_status="processing_enter", execution_epoch=0,
        first_seen_at=now, last_seen_at=now)
    db.add(state)
    try:
        db.commit()
        db.refresh(state)
        return state
    except IntegrityError:
        db.rollback()
        winner = db.query(SentinelMatchState).filter(
            SentinelMatchState.sentinel_id == sentinel.id,
            SentinelMatchState.match_key == key,
        ).first()
        return winner


def _record_edge_outcome(db: Session, state: SentinelMatchState,
                         edge: str, outcome: str) -> None:
    if edge == "leave" and outcome in ("success", "no_actions"):
        db.delete(state)
        return
    if edge == "enter" and outcome in ("success", "no_actions"):
        state.runtime_status = "completed"
        return
    state.runtime_status = f"{'pending' if outcome == 'pending' else 'failed'}_{edge}"


def _match_ids_from_detail(detail: dict | None) -> dict[str, str]:
    return {
        str(alias): str(instance_id)
        for alias, instance_id in (detail or {}).items()
        if not str(alias).startswith("__") and isinstance(instance_id, str)
    }


def _snapshot_match_detail(tup: dict, *, edge: str,
                           match_key: str, occurred_at: datetime,
                           previous_event: dict | None = None,
                           expected_release_id: str | None = None,
                           sentinel: Sentinel | None = None) -> dict:
    detail: dict = {alias: instance.id for alias, instance in tup.items()}
    detail[_MATCH_SNAPSHOTS_KEY] = {
        alias: {
            "id": instance.id,
            "objectTypeId": getattr(instance, "object_type_id", None),
            "properties": deepcopy(getattr(instance, "properties", None) or {}),
            "computed": deepcopy(getattr(instance, "computed", None) or {}),
            "externalId": getattr(instance, "external_id", None),
            "ontologyReleaseId": getattr(
                instance, "ontology_release_id", None),
        }
        for alias, instance in tup.items()
    }
    event = dict(previous_event or {})
    stable_occurred_at = (
        event.get("occurredAt")
        if event.get("edge") == edge and event.get("occurredAt")
        else occurred_at.isoformat()
    )
    event.update({
        "edge": edge,
        "matchKey": match_key,
        "occurredAt": stable_occurred_at,
    })
    if expected_release_id is not None:
        event["ontologyReleaseId"] = expected_release_id
    if sentinel is not None:
        event.update({
            "sentinelOrigin": str(
                getattr(sentinel, "origin", None)
                or "release_builtin"),
            "sentinelBoundReleaseId": getattr(
                sentinel, "bound_release_id", None),
            "sentinelDefinitionRevision": int(
                getattr(sentinel, "definition_revision", 1) or 1),
        })
    detail[_MATCH_EVENT_KEY] = event
    return detail


def _tuple_from_detail(db: Session, ontology_id: str, detail: dict) -> dict:
    snapshots = (detail or {}).get(_MATCH_SNAPSHOTS_KEY, {})
    if isinstance(snapshots, dict) and snapshots:
        restored: dict = {}
        for alias, raw in snapshots.items():
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            restored[alias] = SimpleNamespace(
                id=raw["id"],
                ontology_id=ontology_id,
                object_type_id=raw.get("objectTypeId"),
                properties=deepcopy(raw.get("properties") or {}),
                computed=deepcopy(raw.get("computed") or {}),
                external_id=raw.get("externalId"),
                ontology_release_id=raw.get("ontologyReleaseId"),
            )
        if restored:
            return restored
    ids_by_alias = _match_ids_from_detail(detail)
    ids = list(ids_by_alias.values())
    if not ids:
        return {}
    objects = {obj.id: obj for obj in db.query(ObjectInstance).filter(
        ObjectInstance.ontology_id == ontology_id,
        ObjectInstance.id.in_(ids)).all()}
    return {
        alias: objects[instance_id]
        for alias, instance_id in ids_by_alias.items()
        if instance_id in objects
    }


def _claim_edge(state: SentinelMatchState) -> str:
    return "leave" if (state.runtime_status or "").endswith("_leave") else "enter"


def reject_sentinel_match_claim(db: Session, ontology_id: str,
                                state_id: str) -> dict:
    """Release the edge claim owned by a rejected HITL action."""
    observed = db.query(SentinelMatchState).filter(
        SentinelMatchState.id == state_id,
        SentinelMatchState.ontology_id == ontology_id,
    ).first()
    if observed is None:
        return {"status": "not_found"}
    with _sentinel_execution_lock(db, observed.sentinel_id):
        # The first read only discovers the lock key.  Re-read after acquiring
        # it so a scheduler/evaluator that won the lock cannot leave us acting
        # on a stale edge or deleting a newly advanced state.
        state = (
            db.query(SentinelMatchState)
            .filter(
                SentinelMatchState.id == state_id,
                SentinelMatchState.ontology_id == ontology_id,
            )
            .populate_existing()
            .first()
        )
        if state is None:
            return {"status": "not_found"}
        edge = _claim_edge(state)
        match_key = state.match_key
        if edge == "leave":
            # Leave was not completed; retain the previously consumed enter so
            # the absent match can be proposed again with a fresh action key.
            state.runtime_status = "completed"
        else:
            db.delete(state)
        db.commit()
    return {"status": "released", "edge": edge, "matchKey": match_key}


def fail_sentinel_match_claim(db: Session, ontology_id: str,
                              state_id: str) -> dict:
    """Keep a failed edge recoverable without counting it as consumed."""
    observed = db.query(SentinelMatchState).filter(
        SentinelMatchState.id == state_id,
        SentinelMatchState.ontology_id == ontology_id,
    ).first()
    if observed is None:
        return {"status": "not_found"}
    with _sentinel_execution_lock(db, observed.sentinel_id):
        state = (
            db.query(SentinelMatchState)
            .filter(
                SentinelMatchState.id == state_id,
                SentinelMatchState.ontology_id == ontology_id,
            )
            .populate_existing()
            .first()
        )
        if state is None:
            return {"status": "not_found"}
        edge = _claim_edge(state)
        match_key = state.match_key
        state.runtime_status = f"failed_{edge}"
        db.commit()
    return {"status": "failed", "edge": edge, "matchKey": match_key}


def resume_sentinel_match_claim(db: Session, ontology_id: str,
                                state_id: str) -> dict:
    """Resume the whole action chain after one pending step is approved.

    Earlier successful steps replay their durable idempotency logs; the approved
    step resolves through its linked success log; only the remaining steps run.
    This avoids both duplicate side effects and silently truncating the chain.
    """
    from app.ontologies.runtime_fence import _ontology_build_lock
    with _ontology_build_lock(db, ontology_id):
        return _resume_sentinel_match_claim_locked(
            db, ontology_id, state_id)


def _resume_sentinel_match_claim_locked(
        db: Session, ontology_id: str, state_id: str) -> dict:
    state = db.query(SentinelMatchState).filter(
        SentinelMatchState.id == state_id,
        SentinelMatchState.ontology_id == ontology_id,
    ).first()
    if state is None:
        return {"status": "not_found"}
    sentinel_id = state.sentinel_id

    with _sentinel_execution_lock(db, sentinel_id):
        state = (
            db.query(SentinelMatchState)
            .filter(
                SentinelMatchState.id == state_id,
                SentinelMatchState.ontology_id == ontology_id,
            )
            .populate_existing()
            .first()
        )
        sentinel = (
            db.query(Sentinel)
            .filter(
                Sentinel.id == sentinel_id,
                Sentinel.ontology_id == ontology_id,
            )
            .populate_existing()
            .first()
        )
        if state is None:
            return {"status": "not_found"}
        event = (
            (state.match_detail or {}).get(_MATCH_EVENT_KEY) or {}
            if isinstance(state.match_detail, dict) else {}
        )
        expected_release_id = event.get("ontologyReleaseId")
        current_release = runtime_release_identity(db, ontology_id)
        current_release_id = (
            current_release.id if current_release is not None else None)

        def blocked(code: str, message: str) -> dict:
            return {
                "status": "blocked",
                "code": code,
                "errorMessage": message,
                "matchKey": state.match_key,
            }

        stored_origin = event.get("sentinelOrigin")
        live_origin = (
            str(getattr(sentinel, "origin", None) or "release_builtin")
            if sentinel is not None else None
        )
        if (
            stored_origin is not None
            and live_origin is not None
            and str(stored_origin) != live_origin
        ):
            return blocked(
                "sentinel_origin_changed",
                "哨兵管理来源已变化，旧动作链不能恢复")
        origin = str(stored_origin or live_origin or "")
        if sentinel is None and origin != "release_builtin":
            # Assistant-created Sentinels are mutable release-bound overlays,
            # so their live row is the authoritative revision/retirement/trial
            # credential and may never be reconstructed from a formal release.
            return {"status": "sentinel_not_found"}

        execution_sentinel = sentinel
        if origin == "release_builtin" and expected_release_id is not None:
            snapshot_owner = sentinel or SimpleNamespace(id=sentinel_id)
            snapshot = _sentinel_release_snapshot(
                db, ontology_id, snapshot_owner, expected_release_id)
            raw = next(
                (
                    item for item in (snapshot or {}).get("sentinels", [])
                    if isinstance(item, dict)
                    and str(item.get("id") or "") == str(sentinel_id)
                ),
                None,
            )
            if raw is None:
                return blocked(
                    "released_sentinel_missing",
                    "待审批哨兵不在原发布快照中，不能恢复")
            # Mirror engine._released_builtin: structural fields are immutable
            # release data; a published live row may carry only operational
            # enable/mute state.  Draft edits must not rewrite an R1 approval.
            operational = (
                sentinel
                if sentinel is not None
                and sentinel.status == "published"
                and sentinel.origin == "release_builtin"
                else None
            )
            execution_sentinel = SimpleNamespace(
                id=str(raw.get("id") or ""),
                ontology_id=ontology_id,
                name=str(raw.get("name") or ""),
                display_name=str(
                    raw.get("displayName") or raw.get("name") or ""),
                bindings=deepcopy(raw.get("bindings") or []),
                links=deepcopy(raw.get("links") or []),
                condition=raw.get("condition"),
                primary_alias=raw.get("primaryAlias"),
                action_ids=deepcopy(raw.get("actionIds") or []),
                action_parameters=deepcopy(
                    raw.get("actionParameters") or {}),
                trigger_mode=raw.get("triggerMode") or "on_enter",
                enabled=(
                    bool(operational.enabled)
                    if operational is not None
                    else bool(raw.get("enabled", True))
                ),
                muted=(
                    bool(operational.muted)
                    if operational is not None
                    else bool(raw.get("muted", False))
                ),
                status="published",
                origin="release_builtin",
                bound_release_id=expected_release_id,
                retired_at=None,
                _release_snapshot=snapshot,
            )
        elif sentinel is None:
            # A legacy/unscoped match has no immutable definition from which it
            # can be resumed safely.
            return {"status": "sentinel_not_found"}

        if not bool(execution_sentinel.enabled):
            return blocked(
                "sentinel_disabled",
                "哨兵已禁用，待审批动作链不能恢复")
        if getattr(execution_sentinel, "retired_at", None) is not None:
            return blocked(
                "sentinel_retired",
                "哨兵已退役，待审批动作链不能恢复")
        if (
            expected_release_id is not None
            and str(current_release_id or "")
            != str(expected_release_id)
        ):
            return blocked(
                "release_context_changed",
                "当前发布节点已变化，待审批动作链不能跨发布恢复")
        if origin == "assistant_dynamic":
            stored_revision = event.get(
                "sentinelDefinitionRevision")
            if (
                str(sentinel.bound_release_id or "")
                != str(expected_release_id or current_release_id or "")
            ):
                return blocked(
                    "dynamic_sentinel_release_changed",
                    "动态哨兵绑定的发布节点已变化")
            if (
                stored_revision is None
                or int(stored_revision)
                != int(sentinel.definition_revision or 0)
            ):
                return blocked(
                    "dynamic_sentinel_revision_changed",
                    "动态哨兵定义已变化，旧动作链不能恢复")
            if (
                sentinel.last_trial_release_id
                != sentinel.bound_release_id
                or sentinel.last_trial_revision
                != sentinel.definition_revision
            ):
                return blocked(
                    "dynamic_sentinel_trial_stale",
                    "动态哨兵试跑凭据已失效")

        snapshot_releases = {
            str(raw.get("ontologyReleaseId"))
            for raw in (
                (state.match_detail or {}).get(
                    _MATCH_SNAPSHOTS_KEY, {}) or {}
            ).values()
            if isinstance(raw, dict)
            and raw.get("ontologyReleaseId") is not None
        }
        if expected_release_id is not None and (
            snapshot_releases
            and snapshot_releases != {str(expected_release_id)}
        ):
            return blocked(
                "match_state_release_mismatch",
                "命中状态对象快照不属于同一发布节点")
        action_logs = db.query(ActionExecutionLog).filter(
            ActionExecutionLog.ontology_id == ontology_id,
            ActionExecutionLog.sentinel_match_state_id == state.id,
        ).all()
        if expected_release_id is not None and any(
            str(item.ontology_release_id or "")
            != str(expected_release_id)
            for item in action_logs
        ):
            return blocked(
                "action_release_mismatch",
                "待审批动作日志与命中状态发布血缘不一致")
        current_action_ids = set(execution_sentinel.action_ids or [])
        if any(
            item.action_id not in current_action_ids
            for item in action_logs
        ):
            return blocked(
                "sentinel_action_chain_changed",
                "哨兵动作链已变化，旧审批不能继续执行")

        try:
            _guard_expected_release(
                db, ontology_id, expected_release_id)
        except ReleaseContextChanged:
            return blocked(
                "release_context_changed",
                "当前发布节点已变化，待审批动作链不能跨发布恢复")
        edge = _claim_edge(state)
        match_key = state.match_key
        match_detail = _match_ids_from_detail(state.match_detail or {})
        state.runtime_status = f"processing_{edge}"
        db.commit()
        db.refresh(state)
        tup = _tuple_from_detail(db, ontology_id, state.match_detail or {})
        primary = execution_sentinel.primary_alias or (
            execution_sentinel.bindings[0].get("alias")
            if execution_sentinel.bindings else None)
        results: list[dict] = []
        token = in_sentinel_run.set(True)
        try:
            _ok, outcome = _run_actions(
                db, ontology_id, execution_sentinel, tup, primary, edge,
                match_key, state, results,
                expected_release_id=expected_release_id)
        finally:
            in_sentinel_run.reset(token)
        try:
            _guard_expected_release(
                db, ontology_id, expected_release_id)
        except ReleaseContextChanged:
            db.rollback()
            return blocked(
                "release_context_changed",
                "动作执行期间发布节点已变化，状态推进已中止")
        _record_edge_outcome(db, state, edge, outcome)

        if outcome == "pending":
            firing_status = "pending"
        elif outcome == "failed":
            firing_status = "error"
        elif outcome == "no_actions":
            firing_status = "skipped"
        else:
            firing_status = "fired"
        release_identity = runtime_release_identity(db, ontology_id)
        firing = SentinelFiring(
            ontology_id=ontology_id, sentinel_id=execution_sentinel.id,
            sentinel_name=execution_sentinel.display_name,
            trigger_source="approval",
            matches=[match_detail], match_count=1,
            entered=[match_key] if edge == "enter" else [],
            left=[match_key] if edge == "leave" else [],
            action_results=results, status=firing_status,
            error="; ".join(
                str(item.get("errorMessage")) for item in results
                if item.get("status") == "failed" and item.get("errorMessage")
            ) or None,
            duration_ms=0,
            ontology_version=(
                release_identity.version if release_identity is not None
                else runtime_release_version(db, ontology_id)
            ),
            ontology_release_id=(
                expected_release_id
                or (
                    release_identity.id
                    if release_identity is not None else None
                )
            ),
        )
        db.add(firing)
        db.commit()
        db.refresh(firing)
        return {
            "status": firing_status, "edge": edge,
            "matchKey": match_key, "actionResults": results,
            "firingId": firing.id,
        }


def evaluate_sentinel(db: Session, ontology_id: str, sentinel: Sentinel,
                      source: str,
                      expected_release_id: str | None = None
                      ) -> SentinelFiring:
    """边沿触发评估(参考 Foundry Automate):算当前命中集 → 与上次做差 →
    仅对"进入"(可选"离开")执行动作 → 更新命中状态。source ∈ manual|change|schedule

    整体异常防护：单个哨兵怎么坏（配置损坏/表达式炸/动作抛错）都不能让
    手动触发 500、扫描 tick 崩溃或 CDC 线程静默吞错——统一落 status=error 日志。"""
    start = time.time()
    release_identity = runtime_release_identity(db, ontology_id)
    release_version = (
        release_identity.version if release_identity is not None
        else runtime_release_version(db, ontology_id)
    )
    release_id = release_identity.id if release_identity is not None else None
    captured_release_id = expected_release_id or release_id
    selected_id = str(getattr(sentinel, "id", "") or "")
    selected_name = str(
        getattr(sentinel, "display_name", None)
        or getattr(sentinel, "name", None)
        or selected_id)
    try:
        # Global order is ontology build/projection → Sentinel.  Promotion and
        # Mapping already own the first lock; independent scheduler/manual CDC
        # evaluations acquire it here before the per-Sentinel lock.  This keeps
        # action Fact writes inside promotion's runtime-state serialization
        # fence without introducing the reverse Sentinel→build ABBA order.
        from app.ontologies.runtime_fence import (
            _ontology_build_lock,
        )
        with _ontology_build_lock(db, ontology_id):
            with _sentinel_execution_lock(db, selected_id):
                _guard_expected_release(
                    db, ontology_id, captured_release_id)
                execution_sentinel = _reload_executable_sentinel(
                    db, ontology_id, sentinel, captured_release_id)
                return _evaluate_inner(
                    db, ontology_id, execution_sentinel, source, start,
                    release_version, captured_release_id)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        if isinstance(e, ReleaseContextChanged):
            public_error = str(e)
            public_code = "release_context_changed"
        elif isinstance(e, SentinelOperationalStateChanged):
            public_error = str(e)
            public_code = e.code
        else:
            logger.exception(
                "哨兵评估出现未封装异常: ontology=%s sentinel=%s source=%s",
                ontology_id, selected_id, source)
            public_error = "哨兵评估出现内部错误，请检查服务端日志"
            public_code = "sentinel_evaluation_internal_error"
        firing = SentinelFiring(
            ontology_id=ontology_id, sentinel_id=selected_id,
            sentinel_name=selected_name, trigger_source=source,
            matches=[], match_count=0, entered=[], left=[],
            action_results=[{
                "status": "failed",
                "validationErrors": [public_code],
                "errorMessage": public_error,
                "effects": [],
            }],
            status="error", error=public_error,
            duration_ms=int((time.time() - start) * 1000),
            ontology_version=release_version,
            # This row explains why the captured run was rejected.  Assigning
            # it to the newly-current release would contaminate that release's
            # operational history with work which never evaluated against it.
            ontology_release_id=captured_release_id)
        db.add(firing); db.commit(); db.refresh(firing)
        return firing


def _match_tuples_with_temporal(
        db: Session, condition, tuples: list[dict],
        errors: list[str] | None = None) -> list[dict]:
    """按 condition 过滤候选元组；携带时间算子时批量预取时间事实。

    时间算子形态非法属于配置错误：并入 eval_errors 后整轮观察作废
    （与现有 fail-closed 不变量一致），绝不静默降级为"永不命中"。
    """
    refs, temporal_errors = temporal_ops.extract_temporal_refs(condition)
    if temporal_errors:
        if errors is not None:
            errors.extend(temporal_errors)
        return []
    if not refs:
        return [t for t in tuples if _holds(condition, t, errors)]
    facts = temporal_ops.prefetch_temporal_facts(
        db, refs, tuples, now=_now())
    matched: list[dict] = []
    for tup in tuples:
        temporal = temporal_ops.build_temporal_scope(refs, facts, tup)
        if _holds(condition, tup, errors, temporal=temporal):
            matched.append(tup)
    return matched


def _evaluate_inner(db: Session, ontology_id: str, sentinel: Sentinel,
                    source: str, start: float,
                    release_version: str | None,
                    release_id: str | None) -> SentinelFiring:
    primary = sentinel.primary_alias or (sentinel.bindings[0]["alias"]
                                         if sentinel.bindings else None)
    mode = sentinel.trigger_mode or "on_enter"
    eval_errors: list[str] = []   # 表达式求值错误——fail-closed 但必须可见
    metadata: dict = {"candidateCapReached": False}

    # 1) 当前命中集
    tuples = _resolve_tuples(
        db, ontology_id, sentinel, eval_errors, release_id=release_id,
        metadata=metadata)
    matched = _match_tuples_with_temporal(
        db, sentinel.condition, tuples, eval_errors)
    # 命中键 → 元组(同键去重,保留首个)
    current: dict[str, dict] = {}
    for t in matched:
        k = _match_key(t, primary)
        current.setdefault(k, t)

    # Any evaluation/configuration error invalidates the *whole* observation.
    # A partial result must never be diffed against durable state: doing so can
    # manufacture leave edges for healthy rows and execute destructive cleanup.
    if eval_errors or metadata["candidateCapReached"]:
        _guard_expected_release(
            db, ontology_id, release_id)
        firing = SentinelFiring(
            ontology_id=ontology_id, sentinel_id=sentinel.id,
            sentinel_name=sentinel.display_name, trigger_source=source,
            matches=[
                {alias: instance.id for alias, instance in item.items()}
                for item in current.values()
            ],
            match_count=len(current), entered=[], left=[],
            action_results=[], status="error",
            error="; ".join(eval_errors) or (
                f"哨兵候选组合超过安全上限 {MAX_TUPLES}"),
            duration_ms=int((time.time() - start) * 1000),
            ontology_version=release_version,
            ontology_release_id=release_id,
        )
        db.add(firing)
        db.commit()
        db.refresh(firing)
        return firing

    # 2) 上次命中集
    prior_rows = db.query(SentinelMatchState).filter(
        SentinelMatchState.sentinel_id == sentinel.id).all()
    prior_by_key = {r.match_key: r for r in prior_rows}
    # Only a completed enter (or an unfinished leave of a previously completed
    # enter) belongs to the consumed match set.  Enter failures/pending claims
    # stay recoverable but do not suppress the edge.
    prior_keys = {
        r.match_key for r in prior_rows
        if (r.runtime_status or "completed") in _ACTIVE_MATCH_STATUSES
    }
    cur_keys = set(current.keys())

    entered = cur_keys - prior_keys          # 新进入
    left = prior_keys - cur_keys             # 离开

    # 3) 决定本次要执行动作的命中(按触发模式)
    if mode == "run_on_all":
        enter_targets = cur_keys             # 电平:每轮对全部当前命中执行
    else:
        enter_targets = entered              # 边沿:仅新进入
    leave_targets = left if mode == "on_enter_leave" else set()

    action_results: list[dict] = []
    edge_outcomes: list[str] = []
    now = _now()

    if not sentinel.muted:
        token = in_sentinel_run.set(True)    # 断环:动作回写不即时级联
        try:
            for k in enter_targets:
                tup = current.get(k)
                if not tup:
                    continue
                newly_entered = k in entered
                _guard_expected_release(
                    db, ontology_id, release_id)
                state = _claim_match_state(
                    db, ontology_id, sentinel, k, tup, now,
                    new_cycle=(mode == "run_on_all" and not newly_entered),
                    expected_release_id=release_id,
                )
                if state is None:
                    # Another evaluator has already claimed this edge.
                    continue
                _ok, outcome = _run_actions(
                    db, ontology_id, sentinel, tup, primary, "enter",
                    k, state, action_results,
                    expected_release_id=release_id)
                edge_outcomes.append(outcome)
                _record_edge_outcome(db, state, "enter", outcome)
            for k in leave_targets:
                state = prior_by_key.get(k)
                if state is None:
                    continue
                state.runtime_status = "processing_leave"
                detail = dict(state.match_detail or {})
                previous_event = detail.get(_MATCH_EVENT_KEY) or {}
                detail[_MATCH_EVENT_KEY] = {
                    **previous_event,
                    "edge": "leave",
                    "matchKey": k,
                    "occurredAt": (
                        previous_event.get("occurredAt")
                        if previous_event.get("edge") == "leave"
                        and previous_event.get("occurredAt")
                        else now.isoformat()
                    ),
                }
                state.match_detail = detail
                _guard_expected_release(
                    db, ontology_id, release_id)
                db.commit()
                db.refresh(state)
                detail = state.match_detail or {}
                tup = _tuple_from_detail(db, ontology_id, detail or {})
                _ok, outcome = _run_actions(
                    db, ontology_id, sentinel, tup, primary, "leave",
                    k, state, action_results,
                    expected_release_id=release_id)
                edge_outcomes.append(outcome)
                _record_edge_outcome(db, state, "leave", outcome)
        finally:
            in_sentinel_run.reset(token)

        # 4) 更新命中状态。failed/pending claim 虽保留用于恢复与幂等，
        #    但 runtime_status 不属于 consumed set，因此不会静默吸收 on_enter。
        #    muted(影子模式)不更新——match_state 语义是"已执行过动作的命中"；
        #    否则解除静默后存量命中已被吸收，on_enter 永不触发。
        for r in prior_rows:
            if r.match_key in left:
                if mode != "on_enter_leave":
                    db.delete(r)
            elif r.match_key in cur_keys:
                r.last_seen_at = now
                if (r.runtime_status or "completed") == "completed":
                    previous_event = (
                        (r.match_detail or {}).get(_MATCH_EVENT_KEY)
                        if isinstance(r.match_detail, dict) else None
                    )
                    r.match_detail = _snapshot_match_detail(
                        current[r.match_key], edge="enter",
                        match_key=r.match_key, occurred_at=now,
                        previous_event=previous_event,
                        expected_release_id=release_id,
                        sentinel=sentinel)
            elif (r.runtime_status or "").endswith("_enter") \
                    and r.runtime_status != "pending_enter":
                # The condition disappeared before an enter retry succeeded;
                # end that lifecycle so a future re-entry receives a fresh key.
                db.delete(r)

    # 4.5) 缺席事实：查询结果为空/非空的状态翻转冻结进事实流（Negation-as-Failure 溯源）。
    #      表达式全在报错时跳过——那是 error 不是"确认为空"，不能伪造缺席证据。
    _guard_expected_release(
        db, ontology_id, release_id)
    try:
        # ``Session.flush`` marks a failed SAVEPOINT inactive even though
        # ``SessionTransaction.rollback()`` must still be called to restore
        # the parent Session.  A manual ``if nested.is_active`` cleanup skips
        # exactly that path and leaves the evaluator in PendingRollbackError.
        # Let the transaction context always close/rollback the SAVEPOINT so
        # this best-effort forensic fact can never poison the durable firing.
        with db.begin_nested():
            from app.ontologies.formal_modeling.facts import (
                record_absence_fact,
            )
            record_absence_fact(
                db, ontology_id=ontology_id, subject_id=sentinel.id,
                empty=(len(current) == 0), scanned=len(tuples),
                source=f"sentinel://{sentinel.name or sentinel.id}@{source}",
                detail={"condition": sentinel.condition or "",
                        "sentinelName": sentinel.display_name},
                ontology_version=release_version,
                ontology_release_id=release_id)
    except Exception:  # noqa: BLE001 — 取证失败不能影响评估主流程
        # ``begin_nested()`` itself first flushes pending outer state.  If that
        # pre-SAVEPOINT flush fails there is no nested context to restore the
        # Session, so recover the invalid outer transaction before recording
        # the firing.  Durable action/edge checkpoints above have already been
        # committed; only this run's best-effort observation updates can roll
        # back here.
        if not db.is_active:
            db.rollback()

    # 5) 记录触发日志
    action_failed = "failed" in edge_outcomes
    action_pending = "pending" in edge_outcomes
    only_no_actions = bool(edge_outcomes) and all(o == "no_actions" for o in edge_outcomes)
    if sentinel.muted:
        status = "muted"
    elif action_failed:
        status = "error"
    elif action_pending:
        status = "pending"
    elif only_no_actions:
        status = "skipped"
    elif action_results and all(r.get("status") == "success" for r in action_results):
        status = "fired"
    elif matched:
        status = "no_change"   # 仍有命中但无新进入/离开 → 不重复触发
    else:
        status = "no_match"

    firing = SentinelFiring(
        ontology_id=ontology_id, sentinel_id=sentinel.id,
        sentinel_name=sentinel.display_name, trigger_source=source,
        matches=[{a: inst.id for a, inst in t.items()} for t in current.values()],
        match_count=len(current),
        entered=sorted(entered), left=sorted(left),
        action_results=action_results, status=status,
        error=("; ".join([
            *eval_errors,
            *[str(r.get("errorMessage")) for r in action_results
              if r.get("status") == "failed" and r.get("errorMessage")],
        ]) or None),
        duration_ms=int((time.time() - start) * 1000),
        ontology_version=release_version,
        ontology_release_id=release_id)
    _guard_expected_release(
        db, ontology_id, release_id)
    db.add(firing); db.commit(); db.refresh(firing)
    return firing
