"""时间算子 — ``changed_within`` / ``prev`` 的提取、批量预取与 scope 注入。

实现要点（架构评审 2026-09-06 收敛）：
- 算子只允许出现在哨兵 ``condition``（校验层负责拒绝 filter 内使用）；
- 第二参数必须是数值字面量，静态可提取 → 预取按 (别名, 键, 秒) 一次批量
  查询，杜绝逐元组 N+1；
- 时间基准强制 UTC（由调用方传入评估时钟，禁止使用 safe_eval 的无时区
  ``now()``）；
- 任何运行期未知形态（未预取的组合/别名缺失）按 fail-closed 判否，
  经 safe_eval 的异常包装落入现有 eval_errors 可见性通道。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.ontologies.sentinels.cep.contract import (
    TEMPORAL_FUNCTION_CHANGED_WITHIN,
    TEMPORAL_FUNCTION_PREV,
    TEMPORAL_FUNCTIONS,
    TEMPORAL_WINDOW_MAX_SECONDS,
    TEMPORAL_WINDOW_MIN_SECONDS,
)
from app.ontologies.sentinels.cep import event_store


@dataclass(frozen=True)
class TemporalRef:
    function: str          # changed_within | prev
    alias: str
    key: str
    seconds: int | None    # changed_within 专用


def _split_ref(raw: str) -> tuple[str, str] | None:
    if not isinstance(raw, str) or "." not in raw:
        return None
    alias, _, key = raw.partition(".")
    if not alias or not key or "." in key:
        return None
    return alias, key


def extract_temporal_refs(expr) -> tuple[list[TemporalRef], list[str]]:
    """从 condition AST 提取时间算子引用；形态非法时返回错误描述。

    只接受字符串字面量 ``'alias.prop'`` 与（changed_within 的）数值字面量
    秒数——这是预取批量化与静态校验的共同前提。
    """
    raw = str(expr or "").strip().rstrip(";").strip()
    if not raw:
        return [], []
    try:
        tree = ast.parse(raw, mode="eval")
    except SyntaxError:
        # 语法错误由 validate_safe_expression / safe_eval 拥有 canonical 报错。
        return [], []
    refs: list[TemporalRef] = []
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id in TEMPORAL_FUNCTIONS):
            continue
        name = func.id
        if name == TEMPORAL_FUNCTION_CHANGED_WITHIN:
            if len(node.args) != 2:
                errors.append(
                    "changed_within 需要 2 个参数: "
                    "changed_within('alias.prop', 秒数)")
                continue
            ref_arg, seconds_arg = node.args
            split = (
                _split_ref(ref_arg.value)
                if isinstance(ref_arg, ast.Constant)
                and isinstance(ref_arg.value, str) else None)
            if split is None:
                errors.append(
                    "changed_within 第一参数必须是 'alias.prop' 字符串字面量")
                continue
            if not (isinstance(seconds_arg, ast.Constant)
                    and isinstance(seconds_arg.value, (int, float))
                    and not isinstance(seconds_arg.value, bool)):
                errors.append("changed_within 第二参数必须是数值字面量（秒）")
                continue
            seconds = int(seconds_arg.value)
            refs.append(TemporalRef(
                name, split[0], split[1], seconds))
        else:  # prev
            if len(node.args) != 1:
                errors.append("prev 需要 1 个参数: prev('alias.prop')")
                continue
            ref_arg = node.args[0]
            split = (
                _split_ref(ref_arg.value)
                if isinstance(ref_arg, ast.Constant)
                and isinstance(ref_arg.value, str) else None)
            if split is None:
                errors.append(
                    "prev 参数必须是 'alias.prop' 字符串字面量")
                continue
            refs.append(TemporalRef(name, split[0], split[1], None))
    return refs, errors


def window_seconds_in_range(seconds: int) -> bool:
    return (TEMPORAL_WINDOW_MIN_SECONDS
            <= int(seconds) <= TEMPORAL_WINDOW_MAX_SECONDS)


class TemporalFacts:
    """一次评估的批量预取结果（不可变事实快照）。"""

    __slots__ = ("recent", "previous")

    def __init__(self):
        # (alias, key, seconds) -> 该别名下窗口内发生过 organic 变更的实例 id 集
        self.recent: dict[tuple[str, str, int], set[str]] = {}
        # (instance_id, key) -> 最近一次 organic 变更前的值（缺失=无历史）
        self.previous: dict[tuple[str, str], object] = {}


def prefetch_temporal_facts(
        db: Session, refs: list[TemporalRef], tuples: list[dict],
        *, now: datetime) -> TemporalFacts:
    """为全部候选元组一次性预取时间事实。

    时钟由调用方传入（评估器 UTC 时钟），保证与评估其余部分同源、
    并支持测试注入。
    """
    facts = TemporalFacts()
    if not refs or not tuples:
        return facts
    alias_ids: dict[str, set[str]] = {}
    for tup in tuples:
        for alias, instance in tup.items():
            if instance is not None:
                alias_ids.setdefault(alias, set()).add(str(instance.id))

    recent_keys = sorted({
        (ref.alias, ref.key, int(ref.seconds or 0))
        for ref in refs
        if ref.function == TEMPORAL_FUNCTION_CHANGED_WITHIN
        and ref.alias in alias_ids
    })
    for alias, key, seconds in recent_keys:
        since = now - timedelta(seconds=seconds)
        facts.recent[(alias, key, seconds)] = (
            event_store.instances_with_recent_change(
                db, alias_ids[alias], key, since))

    prev_groups: dict[str, set[str]] = {}
    for ref in refs:
        if ref.function == TEMPORAL_FUNCTION_PREV and ref.alias in alias_ids:
            prev_groups.setdefault(ref.alias, set()).add(ref.key)
    for alias, keys in prev_groups.items():
        previous = event_store.latest_previous_values(
            db, alias_ids[alias], keys)
        facts.previous.update(previous)
    return facts


def build_temporal_scope(refs: list[TemporalRef], facts: TemporalFacts,
                         tup: dict) -> dict:
    """为单个候选元组构造注入 scope 的两个算子闭包。

    未知形态（未预取的窗口秒数/别名不在元组中）在求值时抛错，
    由 safe_eval 包装成 SafeEvalError → 条件 fail-closed 并记录。
    """
    alias_instances = {
        alias: instance for alias, instance in tup.items()
        if instance is not None}

    def _instance_id(ref) -> tuple[str, str, str]:
        split = _split_ref(str(ref))
        if split is None:
            raise ValueError(f"时间算子参数必须是 'alias.prop': {ref}")
        alias, key = split
        if alias not in alias_instances:
            raise ValueError(f"时间算子引用了未知别名: {alias}")
        return alias, key, str(alias_instances[alias].id)

    def changed_within(ref, seconds) -> bool:
        alias, key, instance_id = _instance_id(ref)
        bucket = facts.recent.get((alias, key, int(seconds)))
        if bucket is None:
            raise ValueError(
                f"changed_within 窗口 {seconds}s 未经预取（须为字面量）")
        return instance_id in bucket

    def prev(ref):
        _, key, instance_id = _instance_id(ref)
        return facts.previous.get((instance_id, key))

    return {
        TEMPORAL_FUNCTION_CHANGED_WITHIN: changed_within,
        TEMPORAL_FUNCTION_PREV: prev,
    }


def utc_now() -> datetime:
    """CEP 层统一的 UTC 时钟（独立于 safe_eval 的本地时间 now()）。"""
    return datetime.now(timezone.utc)
