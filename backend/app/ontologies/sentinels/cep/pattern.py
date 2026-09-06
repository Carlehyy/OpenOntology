"""模式哨兵匹配器 — 水位驱动的受限 CEP 状态机。

架构（2026-09-06 评审收敛）：
- 事件日志（sentinel_event_log）是唯一时间事实源；本模块按"每哨兵水位
  游标"增量拉取 organic 事件（id 升序），outbox/定时扫描/保存路径只是
  唤醒信号——一次性解决编辑器保存无 outbox、消费乱序、retry 重放三个
  正确性问题。
- pattern_state 是"过程"（在途状态行），模式命中/缺失超时合成普通
  SentinelMatchState（结果），完全复用既有动作 claim/幂等/HITL 链；
  动作执行钩子由 evaluator 注入，本模块不 import evaluator/engine/cdc。
- 转移核心（process_change_set / expire_states）是纯函数：评估路径与
  试跑回放（preview_pattern）共用，保证"预演=线上同构"。

语义要点：
- stage filter 在事件时刻求值：当前值叠加该事件的 new_value（键级事实
  重建事件时视图）；未变更键读取评估时当前值——stage filter 应引用被
  监听的变更属性。
- 完成即触发一次（on-complete）；缺失分支（absence）在 stage 超时后以
  edge='absence' 触发；窗口默认 3600s，上限 7 天与事件保留期对齐。
- 聚合为单 stage 变体：对 (instance, property) 的窗口事件做
  count/avg/sum/min/max，带滞回（below→above 才触发，回落清理命中行），
  杜绝窗口内每个事件重复放炮。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.models.ontology_formal import LinkInstance
from app.ontologies.formal_modeling.models import ObjectInstance
from app.ontologies.formal_modeling.safe_eval import (
    SafeEvalError, safe_eval, validate_safe_expression,
)
from app.ontologies.sentinels.cep import contract as cep_contract
from app.ontologies.sentinels.cep import event_store
from app.ontologies.sentinels.cep import temporal_ops
from app.ontologies.sentinels.models import (
    SentinelEventLog,
    SentinelFiring,
    SentinelMatchState,
    SentinelPatternCursor,
    SentinelPatternState,
)

logger = logging.getLogger(__name__)

# 与 outbox 级联深度同界的防护：过深级联事件不参与模式推进。
PATTERN_MAX_EVENT_CASCADE_DEPTH = 8

_ABOVE_MARKER = "__above__"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite DateTime 列读回 naive；统一按 UTC 补 tzinfo 再参与比较。"""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def is_pattern_sentinel(sentinel) -> bool:
    return str(getattr(sentinel, "trigger_mode", "") or "") == (
        cep_contract.PATTERN_TRIGGER_MODE)


# ---------------------------------------------------------------------------
# 定义归一化（运行期防御；结构校验权威在 validation.py）
# ---------------------------------------------------------------------------

def normalize_pattern(raw) -> dict | None:
    """把存储的 pattern JSON 归一为运行结构；形态非法返回 None。"""
    if not isinstance(raw, dict):
        return None
    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, list) or not (
            cep_contract.PATTERN_STAGES_MIN
            <= len(stages_raw) <= cep_contract.PATTERN_STAGES_MAX):
        return None
    stages = []
    for item in stages_raw:
        if not isinstance(item, dict):
            return None
        alias = str(item.get("alias") or "").strip()
        object_type_id = str(item.get("objectTypeId") or "").strip()
        if not alias or not object_type_id:
            return None
        within = item.get("within")
        if within is not None:
            try:
                within = int(within)
            except (TypeError, ValueError):
                return None
            if not (cep_contract.PATTERN_WITHIN_MIN_SECONDS
                    <= within <= cep_contract.PATTERN_WITHIN_MAX_SECONDS):
                return None
        filter_expr = item.get("filter")
        if filter_expr is not None and not isinstance(filter_expr, str):
            return None
        stages.append({
            "alias": alias, "objectTypeId": object_type_id,
            "filter": filter_expr or None, "within": within,
        })
    aliases = [stage["alias"] for stage in stages]
    if len(set(aliases)) != len(aliases):
        return None

    absence_raw = raw.get("absence")
    absence_enabled = bool(
        isinstance(absence_raw, dict) and absence_raw.get("enabled"))
    default_within = raw.get("within")
    if default_within is None:
        default_within = cep_contract.PATTERN_WITHIN_DEFAULT_SECONDS
    else:
        try:
            default_within = int(default_within)
        except (TypeError, ValueError):
            return None
        if not (cep_contract.PATTERN_WITHIN_MIN_SECONDS
                <= default_within <= cep_contract.PATTERN_WITHIN_MAX_SECONDS):
            return None

    aggregate = None
    aggregate_raw = raw.get("aggregate")
    if isinstance(aggregate_raw, dict) and aggregate_raw:
        function = str(aggregate_raw.get("function") or "")
        comparison = str(aggregate_raw.get("comparison") or "gte")
        try:
            window = int(aggregate_raw.get("window") or 0)
            threshold = float(aggregate_raw.get("threshold"))
        except (TypeError, ValueError):
            return None
        property_name = str(aggregate_raw.get("property") or "").strip()
        if (
                function not in cep_contract.PATTERN_AGGREGATE_FUNCTIONS
                or comparison not in cep_contract.PATTERN_AGGREGATE_COMPARISONS
                or not property_name
                or not (cep_contract.PATTERN_WITHIN_MIN_SECONDS
                        <= window <= cep_contract.PATTERN_WITHIN_MAX_SECONDS)
        ):
            return None
        aggregate = {
            "property": property_name, "function": function,
            "window": window, "threshold": threshold,
            "comparison": comparison,
        }
        if len(stages) != 1:
            return None

    condition = raw.get("condition")
    if condition is not None and not isinstance(condition, str):
        return None
    return {
        "stages": stages,
        "absence": {"enabled": absence_enabled},
        "within": default_within,
        "aggregate": aggregate,
        "condition": condition or None,
        "same_instance": len({s["objectTypeId"] for s in stages}) == 1,
    }


def replay_window_seconds(definition: dict) -> int:
    """试跑回放需要覆盖的最小时间跨度。"""
    windows = [int(definition.get("within")
                   or cep_contract.PATTERN_WITHIN_DEFAULT_SECONDS)]
    windows.extend(
        int(stage["within"])
        for stage in definition["stages"] if stage.get("within"))
    if definition.get("aggregate"):
        windows.append(int(definition["aggregate"]["window"]))
    return max(windows)


# ---------------------------------------------------------------------------
# 事件分组：连续同实例键级行合并为"变更集"（近似一次事务的同实例变更）
# ---------------------------------------------------------------------------

@dataclass
class ChangeSet:
    instance_id: str
    object_type_id: str
    overlay: dict = field(default_factory=dict)   # key -> new_value
    max_event_id: int = 0
    occurred_at: datetime | None = None


def group_change_sets(events) -> list[ChangeSet]:
    """每条键级行一个变更集。

    刻意不做"连续同实例合并"：跨事务的相邻 id 无法区分同批与异批，
    合并会吞掉中间态（如 submitted→approved 的两次独立提交被并为一个
    终态变更集，stage0 永远失配）。单键 overlay 语义无损——filter 中
    未变更的键本来就回读当前值（提交后的最新值），同事务多键变更的
    后续键事件也能看到前面键的新值。
    """
    sets: list[ChangeSet] = []
    for event in events:
        if int(event.cascade_depth or 0) > PATTERN_MAX_EVENT_CASCADE_DEPTH:
            continue
        sets.append(ChangeSet(
            instance_id=str(event.instance_id),
            object_type_id=str(event.object_type_id),
            overlay={str(event.key): event.new_value},
            max_event_id=int(event.id),
            occurred_at=_aware(event.occurred_at),
        ))
    return sets


# ---------------------------------------------------------------------------
# 纯转移核心：状态视图为 dict，评估与回放共用
# ---------------------------------------------------------------------------
# 状态视图字段：stage_index / snapshots / started_at / stage_entered_at /
# deadline / above(聚合滞回) / drop(聚合回落标记) / correlation_key / _row


def _stage_values(instance, overlay: dict | None = None) -> dict:
    values = {
        **(getattr(instance, "properties", None) or {}),
        **(getattr(instance, "computed", None) or {}),
    }
    if overlay:
        values.update(overlay)
    return values


def _filter_holds(filter_expr: str | None, alias: str, values: dict,
                  errors: list[str]) -> bool:
    if not filter_expr:
        return True
    try:
        return bool(safe_eval(filter_expr, {alias: values, "obj": values}))
    except SafeEvalError as exc:
        errors.append(f"stage filter「{filter_expr}」求值失败: {exc}")
        return False


def _snapshot_of(instance) -> dict:
    return {
        "id": str(instance.id),
        "objectTypeId": getattr(instance, "object_type_id", None),
        "properties": dict(getattr(instance, "properties", None) or {}),
        "computed": dict(getattr(instance, "computed", None) or {}),
    }


def _snapshot_instance(snapshot: dict, ontology_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=snapshot["id"],
        ontology_id=ontology_id,
        object_type_id=snapshot.get("objectTypeId"),
        properties=dict(snapshot.get("properties") or {}),
        computed=dict(snapshot.get("computed") or {}),
        external_id=None,
        ontology_release_id=None,
    )


def _aggregate_metric(function: str, values: list, errors: list[str]):
    if function == "count":
        return float(len(values))
    numeric: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(
                f"聚合属性包含非数值样本: {value!r}（{function} 要求数值）")
            return None
        numeric.append(float(value))
    if not numeric:
        return None
    if function == "avg":
        return sum(numeric) / len(numeric)
    if function == "sum":
        return sum(numeric)
    if function == "min":
        return min(numeric)
    return max(numeric)


def _threshold_holds(comparison: str, metric: float, threshold) -> bool:
    if comparison == "gte":
        return metric >= threshold
    if comparison == "gt":
        return metric > threshold
    if comparison == "lte":
        return metric <= threshold
    return metric < threshold


def process_change_set(
        definition: dict, change: ChangeSet,
        instance, states: dict[str, dict], *,
        correlator=None,
        aggregate_property_values=None,
        now: datetime, errors: list[str]) -> list[dict]:
    """对一个变更集推进状态机；返回完成事件列表。

    ``instance`` 为该变更集对应的当前实例（调用方保证非 None）。
    ``states`` 为 correlation_key → 状态视图。``correlator`` 为跨对象
    stage 的关联判定（anchor_id, stage, instance）→ bool，由带 db 会话
    的调用方注入；同实例模式无需注入。
    """
    completions: list[dict] = []
    stages = definition["stages"]

    if definition.get("aggregate"):
        return _process_aggregate(
            definition, change, instance, states, now, errors,
            aggregate_property_values)

    same_instance = definition.get("same_instance", True)
    for index, stage in enumerate(stages):
        if stage["objectTypeId"] != change.object_type_id:
            continue
        alias = stage["alias"]
        values = _stage_values(instance, change.overlay)
        if not _filter_holds(stage["filter"], alias, values, errors):
            continue
        if index == 0:
            # stage0 事件：已在该键上开窗（在途）则不重复开窗。
            if str(instance.id) in states:
                continue
            if len(stages) == 1:
                # 单 stage 无聚合：每个满足条件的事件即一次完成。
                completions.append({
                    "edge": "enter", "correlation_key": str(instance.id),
                    "snapshots": {alias: _snapshot_of(instance)},
                    "started_at": change.occurred_at or now,
                })
                continue
            occurred = change.occurred_at or now
            states[str(instance.id)] = {
                "stage_index": 1,
                "snapshots": {alias: _snapshot_of(instance)},
                "started_at": occurred,
                "stage_entered_at": occurred,
                "deadline": occurred + timedelta(
                    seconds=stage_window(definition, 1)),
                "correlation_key": str(instance.id),
            }
            continue
        # index > 0：推进在途状态（stage_index 语义 = 下一个期望 stage）。
        for key, state in list(states.items()):
            if state.get("stage_index") != index:
                continue
            if same_instance:
                if key != str(instance.id):
                    continue
            elif correlator is None or not correlator(
                    key, stages[index - 1], stage, instance):
                continue
            state["snapshots"][alias] = _snapshot_of(instance)
            if index == len(stages) - 1:
                completions.append({
                    "edge": "enter", "correlation_key": key,
                    "snapshots": dict(state["snapshots"]),
                    "started_at": state["started_at"],
                })
                states.pop(key, None)
            else:
                state["stage_index"] = index + 1
                occurred = change.occurred_at or now
                state["stage_entered_at"] = occurred
                state["deadline"] = occurred + timedelta(
                    seconds=stage_window(definition, index + 1))
    return completions


def _process_aggregate(definition, change, instance, states, now, errors,
                       property_values):
    stage = definition["stages"][0]
    alias = stage["alias"]
    aggregate = definition["aggregate"]
    key = str(instance.id)
    if property_values is None:
        return []
    matching = []
    for value in property_values:
        overlay = {aggregate["property"]: value}
        values = _stage_values(instance, overlay)
        if _filter_holds(stage["filter"], alias, values, errors):
            matching.append(value)
    metric = _aggregate_metric(aggregate["function"], matching, errors)
    if metric is None:
        return []
    above = _threshold_holds(
        aggregate["comparison"], metric, aggregate["threshold"])
    state = states.setdefault(key, {
        "stage_index": 0, "snapshots": {}, "started_at": now,
        "stage_entered_at": now, "deadline": None,
        "above": False, "correlation_key": key,
    })
    if above and not state.get("above"):
        state["above"] = True
        state["started_at"] = now
        return [{
            "edge": "enter", "correlation_key": key,
            "snapshots": {alias: _snapshot_of(instance)},
            "started_at": now,
        }]
    if not above and state.get("above"):
        # 回落：清理命中行（下一次上穿允许重新触发），本轮不出完成事件。
        state["above"] = False
        state["drop"] = True
    return []


def stage_window(definition: dict, stage_index: int) -> int:
    """推进到 stage_index 完成前允许的窗口秒数（沿用 pattern 级缺省）。"""
    stages = definition["stages"]
    if 0 < stage_index < len(stages):
        within = stages[stage_index].get("within")
        if within is not None:
            return int(within)
    return int(definition.get("within")
               or cep_contract.PATTERN_WITHIN_DEFAULT_SECONDS)


def expire_states(definition: dict, states: dict[str, dict],
                  now: datetime) -> list[dict]:
    """超时判定：缺失分支启用则产出 absence 完成，否则静默丢弃。"""
    completions: list[dict] = []
    for key, state in list(states.items()):
        deadline = state.get("deadline")
        if deadline is None or deadline > now:
            continue
        if definition.get("absence", {}).get("enabled"):
            completions.append({
                "edge": "absence", "correlation_key": key,
                "snapshots": dict(state.get("snapshots") or {}),
                "started_at": state.get("started_at"),
            })
        states.pop(key, None)
    return completions


# ---------------------------------------------------------------------------
# 评估路径（DB 状态 + hooks 动作链）
# ---------------------------------------------------------------------------

@dataclass
class PatternHooks:
    """evaluator 注入的动作链钩子（本模块不 import evaluator）。"""
    run_actions: object
    claim_match_state: object
    record_edge_outcome: object


def _link_correlator(db: Session, ontology_id: str, links: list[dict]):
    """跨对象关联判定工厂：锚实例与事件实例间必须存在 links 定义的关系。"""
    def correlator(anchor_id: str, previous_stage, stage, instance) -> bool:
        event_id = str(instance.id)
        for link in links:
            pair = {link.get("from"), link.get("to")}
            if pair != {previous_stage["alias"], stage["alias"]}:
                continue
            if link.get("from") == previous_stage["alias"]:
                source_id, target_id = anchor_id, event_id
            else:
                source_id, target_id = event_id, anchor_id
            exists = db.query(LinkInstance.id).filter(
                LinkInstance.ontology_id == ontology_id,
                LinkInstance.link_type_id == link.get("linkTypeId"),
                LinkInstance.source_object_id == source_id,
                LinkInstance.target_object_id == target_id,
            ).first()
            if exists is not None:
                return True
        return False
    return correlator


def _ensure_cursor(db: Session, sentinel) -> SentinelPatternCursor:
    cursor = db.query(SentinelPatternCursor).filter(
        SentinelPatternCursor.sentinel_id == sentinel.id,
    ).first()
    if cursor is None:
        cursor = SentinelPatternCursor(
            sentinel_id=sentinel.id,
            ontology_id=str(sentinel.ontology_id),
            event_id=0,
        )
        db.add(cursor)
        db.flush()
    return cursor


def _load_states(db: Session, sentinel, release_id,
                 definition_revision, enable_generation) -> dict[str, dict]:
    """载入在途状态行为视图 dict；身份不符的旧行直接清理（发布切换防护）。"""
    rows = db.query(SentinelPatternState).filter(
        SentinelPatternState.sentinel_id == sentinel.id,
        SentinelPatternState.status == "active",
    ).all()
    states: dict[str, dict] = {}
    for row in rows:
        if (
            (release_id is not None
             and str(row.ontology_release_id or "") != str(release_id))
            or int(row.definition_revision or 1) != int(definition_revision)
            or int(row.enable_generation or 1) != int(enable_generation)
        ):
            db.delete(row)
            continue
        states[str(row.correlation_key)] = {
            "stage_index": int(row.stage_index or 0),
            "snapshots": {
                alias: value for alias, value in
                (row.snapshots or {}).items()
                if alias != _ABOVE_MARKER
            },
            "started_at": _aware(row.started_at),
            "stage_entered_at": _aware(row.stage_entered_at),
            "deadline": _aware(row.deadline),
            "above": bool((row.snapshots or {}).get(_ABOVE_MARKER)),
            "correlation_key": str(row.correlation_key),
            "_row": row,
        }
    return states


def _sync_states(db: Session, states: dict[str, dict], sentinel,
                 release_id, definition_revision, enable_generation,
                 now: datetime) -> None:
    """把视图 dict 写回状态行（新增/更新/完成删除）。"""
    seen_keys: set[str] = set()
    for key, state in states.items():
        seen_keys.add(key)
        row = state.get("_row")
        snapshots = {
            alias: value for alias, value in
            (state.get("snapshots") or {}).items()
            if not str(alias).startswith("__")
        }
        if state.get("above"):
            snapshots[_ABOVE_MARKER] = True
        if row is None:
            db.add(SentinelPatternState(
                ontology_id=str(sentinel.ontology_id),
                sentinel_id=sentinel.id,
                ontology_release_id=release_id,
                definition_revision=int(definition_revision),
                enable_generation=int(enable_generation),
                correlation_key=key,
                stage_index=int(state.get("stage_index", 0)),
                started_at=state.get("started_at") or now,
                stage_entered_at=state.get("stage_entered_at") or now,
                deadline=state.get("deadline"),
                snapshots=snapshots,
                status="active",
            ))
        else:
            row.stage_index = int(state.get("stage_index", 0))
            row.deadline = state.get("deadline")
            row.snapshots = snapshots
            row.updated_at = now
    # 完成被弹出的状态（states 中已删除）→ 删除对应行。
    rows = db.query(SentinelPatternState).filter(
        SentinelPatternState.sentinel_id == sentinel.id,
        SentinelPatternState.status == "active",
    ).all()
    for row in rows:
        if str(row.correlation_key) not in seen_keys:
            db.delete(row)


def evaluate_pattern(
        db: Session, ontology_id: str, sentinel, source: str, *,
        release_id: str | None, release_version: str | None,
        start_time: float, hooks: PatternHooks) -> SentinelFiring:
    """模式哨兵评估入口（由 evaluator 在执行锁内分派）。"""
    definition = normalize_pattern(getattr(sentinel, "pattern", None))
    errors: list[str] = []
    if definition is None:
        errors.append("哨兵 pattern 定义非法（结构/窗口/聚合不合规）")
        return _error_firing(
            db, ontology_id, sentinel, source, errors, release_id,
            release_version, start_time)
    now = _now()

    cursor = _ensure_cursor(db, sentinel)
    type_ids = {stage["objectTypeId"] for stage in definition["stages"]}
    events = event_store.events_after_cursor(
        db, ontology_id, type_ids, int(cursor.event_id or 0),
        limit=cep_contract.PATTERN_EVENT_BATCH_LIMIT)
    changes = group_change_sets(events)

    definition_revision = int(getattr(sentinel, "definition_revision", 1) or 1)
    enable_generation = int(getattr(sentinel, "enable_generation", 1) or 1)
    states = _load_states(
        db, sentinel, release_id, definition_revision, enable_generation)
    correlator = None
    if not definition.get("same_instance", True):
        correlator = _link_correlator(
            db, ontology_id,
            [link for link in (getattr(sentinel, "links", None) or [])
             if isinstance(link, dict)])

    completions: list[dict] = []
    for change in changes:
        instance = db.query(ObjectInstance).filter(
            ObjectInstance.id == change.instance_id,
            ObjectInstance.ontology_id == ontology_id,
        ).first()
        if instance is None:
            # 实例已删除：同实例模式由 deadline 超时收尾，跳过推进。
            continue
        property_values = None
        if definition.get("aggregate"):
            aggregate = definition["aggregate"]
            window_start = (change.occurred_at or now) - timedelta(
                seconds=aggregate["window"])
            sample_rows = db.query(SentinelEventLog).filter(
                SentinelEventLog.instance_id == change.instance_id,
                SentinelEventLog.key == aggregate["property"],
                SentinelEventLog.source == cep_contract.EVENT_SOURCE_ORGANIC,
                SentinelEventLog.occurred_at >= window_start,
                SentinelEventLog.id <= change.max_event_id,
            ).order_by(SentinelEventLog.id.asc()).all()
            property_values = [
                row.new_value for row in sample_rows
                if int(row.cascade_depth or 0) <= (
                    PATTERN_MAX_EVENT_CASCADE_DEPTH)
            ]
        completions.extend(process_change_set(
            definition, change, instance, states, correlator=correlator,
            aggregate_property_values=property_values,
            now=now, errors=errors))
    completions.extend(expire_states(definition, states, now))
    if errors:
        return _error_firing(
            db, ontology_id, sentinel, source, errors, release_id,
            release_version, start_time)

    completions = _filter_completions_by_condition(
        db, ontology_id, definition, completions, now, errors)
    if errors:
        return _error_firing(
            db, ontology_id, sentinel, source, errors, release_id,
            release_version, start_time)

    _sync_states(db, states, sentinel, release_id, definition_revision,
                 enable_generation, now)

    action_results: list[dict] = []
    edge_outcomes: list[str] = []
    fired_keys: list[str] = []
    matches: list[dict[str, str]] = []
    if not sentinel.muted and completions:
        from app.ontologies.sentinels.cep.contract import in_sentinel_run
        token = in_sentinel_run.set(True)
        try:
            for completion in completions:
                key = _match_key(completion)
                fired_keys.append(key)
                tup = {
                    alias: _snapshot_instance(snapshot, ontology_id)
                    for alias, snapshot in (
                        completion["snapshots"] or {}).items()
                }
                if not tup:
                    continue
                matches.append({
                    alias: instance.id for alias, instance in tup.items()})
                overrides = (
                    {"edge": "absence"}
                    if completion["edge"] == "absence" else None)
                state = hooks.claim_match_state(
                    db, ontology_id, sentinel, key, tup, now,
                    expected_release_id=release_id,
                    event_overrides=overrides)
                if state is None:
                    continue
                _ok, outcome = hooks.run_actions(
                    db, ontology_id, sentinel, tup,
                    sentinel.primary_alias, "enter",
                    key, state, action_results,
                    expected_release_id=release_id,
                    event_overrides=overrides,
                )
                edge_outcomes.append(outcome)
                hooks.record_edge_outcome(db, state, "enter", outcome)
        finally:
            in_sentinel_run.reset(token)
    _drop_aggregate_match_states(db, sentinel, states)

    # 水位推进覆盖本轮拉取的全部事件（被级联防护跳过的也推进，
    # 否则会永远重拉）；未拉完的（达到批量上限）由下次唤醒继续。
    if events:
        cursor.event_id = int(events[-1].id)
        cursor.updated_at = now

    return _final_firing(
        db, ontology_id, sentinel, source, matches, fired_keys,
        action_results, edge_outcomes, errors, release_id, release_version,
        start_time, muted=bool(sentinel.muted))


def _match_key(completion: dict) -> str:
    started = completion.get("started_at")
    anchor = (
        started.isoformat() if hasattr(started, "isoformat")
        else str(started or ""))
    return f"pattern:{completion['correlation_key']}:{anchor}"


def _drop_aggregate_match_states(db: Session, sentinel, states: dict) -> None:
    """聚合回落：删除该哨兵对应实例已完成的聚合命中行，允许再次上穿触发。"""
    prefixes = [
        f"pattern:{state['correlation_key']}:%"
        for state in states.values() if state.get("drop")
    ]
    if not prefixes:
        return
    from sqlalchemy import or_
    conditions = [
        SentinelMatchState.match_key.like(prefix) for prefix in prefixes]
    db.query(SentinelMatchState).filter(
        SentinelMatchState.sentinel_id == sentinel.id,
        SentinelMatchState.runtime_status == "completed",
        or_(*conditions),
    ).delete(synchronize_session=False)


def _filter_completions_by_condition(
        db: Session, ontology_id: str, definition: dict,
        completions: list[dict], now: datetime,
        errors: list[str]) -> list[dict]:
    condition = definition.get("condition")
    if not condition or not completions:
        return completions
    try:
        validate_safe_expression(
            condition,
            set(completions[0]["snapshots"] or {})
            | set(cep_contract.TEMPORAL_FUNCTIONS))
    except SafeEvalError as exc:
        errors.append(f"模式条件「{condition}」无法编译: {exc}")
        return []
    refs, temporal_errors = temporal_ops.extract_temporal_refs(condition)
    if temporal_errors:
        errors.extend(temporal_errors)
        return []
    kept: list[dict] = []
    for completion in completions:
        tup = {
            alias: _snapshot_instance(snapshot, ontology_id)
            for alias, snapshot in (completion["snapshots"] or {}).items()
        }
        temporal = None
        if refs:
            facts = temporal_ops.prefetch_temporal_facts(
                db, refs, [tup], now=now)
            temporal = temporal_ops.build_temporal_scope(refs, facts, tup)
        scope = {
            alias: {
                **(instance.properties or {}),
                **(instance.computed or {}),
            }
            for alias, instance in tup.items()
        }
        if temporal:
            scope.update(temporal)
        try:
            if bool(safe_eval(condition, scope)):
                kept.append(completion)
        except SafeEvalError as exc:
            errors.append(f"模式条件「{condition}」求值失败: {exc}")
    return kept


def _error_firing(db, ontology_id, sentinel, source, errors, release_id,
                  release_version, start_time) -> SentinelFiring:
    db.rollback()
    import time as _time
    firing = SentinelFiring(
        ontology_id=ontology_id, sentinel_id=sentinel.id,
        sentinel_name=sentinel.display_name, trigger_source=source,
        matches=[], match_count=0, entered=[], left=[],
        action_results=[], status="error",
        error="; ".join(errors[:5]),
        duration_ms=int((_time.time() - start_time) * 1000),
        ontology_version=release_version,
        ontology_release_id=release_id,
    )
    db.add(firing)
    db.commit()
    db.refresh(firing)
    return firing


def _final_firing(db, ontology_id, sentinel, source, matches, fired_keys,
                  action_results, edge_outcomes, errors, release_id,
                  release_version, start_time, *, muted: bool
                  ) -> SentinelFiring:
    import time as _time
    action_failed = "failed" in edge_outcomes
    action_pending = "pending" in edge_outcomes
    if muted:
        status = "muted"
    elif errors or action_failed:
        status = "error"
    elif action_pending:
        status = "pending"
    elif fired_keys:
        status = "fired"
    else:
        status = "no_match"
    firing = SentinelFiring(
        ontology_id=ontology_id, sentinel_id=sentinel.id,
        sentinel_name=sentinel.display_name, trigger_source=source,
        matches=matches, match_count=len(matches),
        entered=sorted(set(fired_keys)), left=[],
        action_results=action_results, status=status,
        error="; ".join(errors[:5]) or None,
        duration_ms=int((_time.time() - start_time) * 1000),
        ontology_version=release_version,
        ontology_release_id=release_id,
    )
    db.add(firing)
    db.commit()
    db.refresh(firing)
    return firing


# ---------------------------------------------------------------------------
# 试跑回放（零副作用预演）
# ---------------------------------------------------------------------------

def preview_pattern(db: Session, ontology_id: str, sentinel,
                    release_id: str) -> dict:
    """按事件日志最近窗口回放模式（无状态写入/动作执行）。"""
    definition = normalize_pattern(getattr(sentinel, "pattern", None))
    if definition is None:
        return {
            "passed": False,
            "errors": ["哨兵 pattern 定义非法（结构/窗口/聚合不合规）"],
            "matches": [], "matchCount": 0, "replayCoverage": "invalid",
        }
    now = _now()
    window = replay_window_seconds(definition)
    replay_start = now - timedelta(seconds=window)
    type_ids = {stage["objectTypeId"] for stage in definition["stages"]}
    events = db.query(SentinelEventLog).filter(
        SentinelEventLog.ontology_id == str(ontology_id),
        SentinelEventLog.source == cep_contract.EVENT_SOURCE_ORGANIC,
        SentinelEventLog.object_type_id.in_(type_ids),
        SentinelEventLog.occurred_at >= replay_start,
    ).order_by(SentinelEventLog.id.asc()).limit(
        cep_contract.PATTERN_EVENT_BATCH_LIMIT).all()

    earliest = db.query(SentinelEventLog.occurred_at).filter(
        SentinelEventLog.ontology_id == str(ontology_id),
        SentinelEventLog.source == cep_contract.EVENT_SOURCE_ORGANIC,
    ).order_by(SentinelEventLog.id.asc()).first()
    if earliest is None or earliest[0] is None:
        coverage = "empty"
    elif _aware(earliest[0]) > replay_start:
        coverage = "partial"
    else:
        coverage = "full"

    errors: list[str] = []
    states: dict[str, dict] = {}
    changes = group_change_sets(events)
    completions: list[dict] = []
    correlator = None
    if not definition.get("same_instance", True):
        correlator = _link_correlator(
            db, ontology_id,
            [link for link in (getattr(sentinel, "links", None) or [])
             if isinstance(link, dict)])
    instance_cache: dict[str, object] = {}
    for change in changes:
        instance = instance_cache.get(change.instance_id)
        if instance is None:
            instance = db.query(ObjectInstance).filter(
                ObjectInstance.id == change.instance_id,
                ObjectInstance.ontology_id == ontology_id,
            ).first()
            if instance is None:
                continue
            instance_cache[change.instance_id] = instance
        property_values = None
        if definition.get("aggregate"):
            aggregate = definition["aggregate"]
            window_start = (change.occurred_at or now) - timedelta(
                seconds=aggregate["window"])
            sample_rows = [
                event for event in events
                if str(event.instance_id) == change.instance_id
                and str(event.key) == aggregate["property"]
                and int(event.id) <= change.max_event_id
                and (event.occurred_at or now) >= window_start
            ]
            property_values = [row.new_value for row in sample_rows]
        completions.extend(process_change_set(
            definition, change, instance, states, correlator=correlator,
            aggregate_property_values=property_values,
            now=now, errors=errors))
    completions.extend(expire_states(definition, states, now))
    completions = _filter_completions_by_condition(
        db, ontology_id, definition, completions, now, errors)

    samples = []
    for completion in completions[:20]:
        tup = {
            alias: _snapshot_instance(snapshot, ontology_id)
            for alias, snapshot in (completion["snapshots"] or {}).items()
        }
        samples.append({
            "edge": completion["edge"],
            "matchKey": _match_key(completion),
            "matches": {
                alias: instance.id for alias, instance in tup.items()},
        })
    return {
        "passed": not errors,
        "errors": errors[:5],
        "matches": samples,
        "matchCount": len(completions),
        "replayCoverage": coverage,
        "replayWindowSeconds": window,
        "activeStates": len(states),
    }
