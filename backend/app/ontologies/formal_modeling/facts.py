"""
属性事实记录 — Fact 溯源层的统一写入口

所有对 fo_object_instances.properties 的写路径（编辑器整体保存、实例 CRUD、
Action 执行、采集器）都应经过 record_property_facts，把"改了什么、从哪来、
谁改的、因何而改"追加进 fo_property_facts。投影表照常更新，本模块只负责历史真理流。

链接存在性 / 实例存在性（墓碑）也是事实：
  - record_link_fact:   kind='link'，instance_id=链接实例 id，property_name='exists'
  - record_object_presence: kind='object'，property_name='exists'，value=True
  - record_object_tombstone: kind='object'，property_name='exists'，value=False
决策也是事实（HITL 批准/拒绝）：
  - record_decision_fact: kind='decision'，instance_id=动作日志 id，property_name='decision'
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterable, Optional
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import PropertyFact
from app.ontologies.release_context import (
    runtime_release_identity,
    runtime_release_version,
)


def _wrap(v: Any, *, present: Optional[bool] = None) -> dict:
    """JSON 列统一包一层 ``{"v": ...}``。

    删除属性时 ``v=None`` 无法与“属性仍存在、值为 null”区分，因此 property
    facts 显式传入 ``present=True/False``。其他事实种类不传该参数，继续保持
    既有 ``{"v": ...}`` 结构；所有既有 reader 仍可只读取 ``v``。
    """
    wrapped = {"v": v}
    if present is not None:
        wrapped["present"] = present
    return wrapped


def _runtime_lineage(
    db: Session,
    ontology_id: str,
    ontology_version: Optional[str],
    ontology_release_id: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Resolve lineage without attributing explicit legacy versions by guess.

    Callers forwarding lineage from another record must pass both fields.  If
    they only provide a version, the release id intentionally remains NULL:
    version labels are mutable/reusable and are not an immutable identity.
    """
    if ontology_version is not None:
        return ontology_version, ontology_release_id
    batch = db.info.get(_FACT_BATCH_KEY)
    lineage = batch[3] if batch is not None and batch[0] == ontology_id else None
    if lineage is not None and ontology_release_id in lineage:
        return lineage[ontology_release_id]
    identity = runtime_release_identity(db, ontology_id)
    result = (
        (identity.version, ontology_release_id or identity.id) if identity is not None
        else (runtime_release_version(db, ontology_id), ontology_release_id)
    )
    if lineage is not None:
        lineage[ontology_release_id] = result
    return result


def fact_order_clause():
    """事实的确定性时间序（新→旧）。seq 在同一 (instance, property) 链内单调递增，
    解决同一批次 recorded_at 相同、uuid 主键排序随机的问题。"""
    return (PropertyFact.recorded_at.desc(), PropertyFact.seq.desc(), PropertyFact.id.desc())


_FACT_BATCH_KEY = "ontology_property_fact_batch"
MAX_FACT_BATCH_INSTANCES = 1000


@contextmanager
def property_fact_batch(
    db: Session, *, ontology_id: str, instance_ids: Iterable[str],
):
    """Prefetch one bounded set of fact heads and flush its appends together.

    The ordinary recording functions remain the only writers, including their
    idempotence, removal, lineage and supersedes rules. IDs are allocated before
    flushing, so callers can still attach derived_from to the returned facts.
    The caller owns the transaction and must retain the ontology mutation fence.
    """
    ids = set()
    for instance_id in instance_ids:
        ids.add(str(instance_id))
        if len(ids) > MAX_FACT_BATCH_INSTANCES:
            raise ValueError("Fact batches may contain at most 1000 instances")
    if _FACT_BATCH_KEY in db.info:
        raise ValueError("Fact batches cannot be nested")
    db.flush()
    heads = {}
    # Keep SQLite bind counts bounded as well as PostgreSQL result size. A
    # window returns only heads, never every historical value of each field.
    ordered_ids = sorted(ids)
    for offset in range(0, len(ordered_ids), 300):
        ranked = db.query(
            PropertyFact.id.label("fact_id"),
            func.row_number().over(
                partition_by=(
                    PropertyFact.instance_id, PropertyFact.kind,
                    PropertyFact.property_name,
                ),
                order_by=fact_order_clause(),
            ).label("head_rank"),
        ).filter(
            PropertyFact.ontology_id == ontology_id,
            PropertyFact.instance_id.in_(ordered_ids[offset:offset + 300]),
        ).subquery()
        for fact in db.query(PropertyFact).join(
            ranked, ranked.c.fact_id == PropertyFact.id,
        ).filter(ranked.c.head_rank == 1):
            heads[(fact.instance_id, fact.kind, fact.property_name)] = fact
    db.info[_FACT_BATCH_KEY] = (ontology_id, ids, heads, {})
    try:
        with db.no_autoflush:
            yield
        db.flush()
    finally:
        db.info.pop(_FACT_BATCH_KEY, None)


def _batch_heads(db: Session, ontology_id: str, instance_id: str):
    batch = db.info.get(_FACT_BATCH_KEY)
    if batch is not None and batch[0] == ontology_id and instance_id in batch[1]:
        return batch[2]
    return None


def _append_fact(db: Session, fact: PropertyFact) -> bool:
    """Return whether the caller must flush this append immediately."""
    heads = _batch_heads(db, fact.ontology_id, fact.instance_id)
    if heads is not None:
        fact.id = str(uuid.uuid4())
        heads[(fact.instance_id, fact.kind, fact.property_name)] = fact
    db.add(fact)
    return heads is None


def record_property_facts(
    db: Session,
    *,
    ontology_id: str,
    instance_id: str,
    object_type_id: Optional[str],
    old_props: Optional[dict],
    new_props: dict,
    source: str,
    actor_id: Optional[str] = None,
    caused_by: Optional[str] = None,
    kind: str = "property",
    derived_from: Optional[list[str]] = None,
    confidence: Optional[float] = None,
    ontology_version: Optional[str] = None,
    ontology_release_id: Optional[str] = None,
    source_dataset_version_id: Optional[str] = None,
) -> list[PropertyFact]:
    """对比新旧属性，把发生变化的属性逐条追加为事实。返回新追加的事实列表。

    old_props 为 None 表示实例新建——所有属性都是首个事实（无 supersedes）。
    只追加，不 commit；由调用方的事务统一提交（会 flush 以拿到事实 id）。
    """
    if old_props is None:
        changed = [
            (key, value, True)
            for key, value in (new_props or {}).items()
        ]
    else:
        old_snapshot = dict(old_props or {})
        new_snapshot = dict(new_props or {})
        changed = [
            (key, value, True)
            for key, value in new_snapshot.items()
            if key not in old_snapshot or old_snapshot.get(key) != value
        ]
        changed.extend(
            (key, None, False)
            for key in old_snapshot.keys() - new_snapshot.keys()
        )
    if not changed:
        return []
    release_version, release_id = _runtime_lineage(
        db, ontology_id, ontology_version, ontology_release_id)

    # 每个变更属性当前最新的事实 —— 作为 supersedes 指针与 seq 基准
    changed_keys = [key for key, _, _ in changed]
    heads = _batch_heads(db, ontology_id, instance_id)
    if heads is not None:
        latest = {
            key: heads[(instance_id, kind, key)] for key in changed_keys
            if (instance_id, kind, key) in heads
        }
    else:
        ranked = db.query(
            PropertyFact.id.label("fact_id"),
            func.row_number().over(
                partition_by=PropertyFact.property_name,
                order_by=fact_order_clause(),
            ).label("head_rank"),
        ).filter(
            PropertyFact.ontology_id == ontology_id,
            PropertyFact.instance_id == instance_id,
            PropertyFact.kind == kind,
            PropertyFact.property_name.in_(changed_keys),
        ).subquery()
        latest = {
            fact.property_name: fact
            for fact in db.query(PropertyFact).join(
                ranked, ranked.c.fact_id == PropertyFact.id,
            ).filter(ranked.c.head_rank == 1)
        }

    created: list[PropertyFact] = []
    for key, value, present in changed:
        prev = latest.get(key)
        fact = PropertyFact(
            ontology_id=ontology_id,
            instance_id=instance_id,
            object_type_id=object_type_id,
            property_name=key,
            value=_wrap(value, present=present),
            kind=kind,
            source=source,
            actor_id=actor_id,
            caused_by=caused_by,
            supersedes_id=(prev.id if (prev is not None and old_props is not None) else None),
            derived_from=list(derived_from) if derived_from else None,
            confidence=confidence,
            ontology_version=release_version,
            ontology_release_id=release_id,
            source_dataset_version_id=source_dataset_version_id,
            seq=(prev.seq or 0) + 1 if prev is not None else 1,
        )
        _append_fact(db, fact)
        created.append(fact)
    if created and heads is None:
        db.flush()  # 拿到 id，供派生事实的 derived_from 引用
    return created


def _latest_fact(db: Session, ontology_id: str, instance_id: str,
                 property_name: str,
                 kind: Optional[str] = None) -> Optional[PropertyFact]:
    heads = _batch_heads(db, ontology_id, instance_id)
    if heads is not None and kind is not None:
        return heads.get((instance_id, kind, property_name))
    query = db.query(PropertyFact).filter(
        PropertyFact.ontology_id == ontology_id,
        PropertyFact.instance_id == instance_id,
        PropertyFact.property_name == property_name,
    )
    if kind is not None:
        query = query.filter(PropertyFact.kind == kind)
    return query.order_by(*fact_order_clause()).first()


def record_link_fact(
    db: Session,
    *,
    ontology_id: str,
    link_instance_id: str,
    link_type_id: Optional[str],
    exists: bool,
    source: str,
    actor_id: Optional[str] = None,
    caused_by: Optional[str] = None,
    ontology_version: Optional[str] = None,
    ontology_release_id: Optional[str] = None,
    source_dataset_version_id: Optional[str] = None,
) -> Optional[PropertyFact]:
    """链接存在性事实（kind='link'）。建链 exists=True，删链 exists=False 并
    supersede 建链事实——关系变化从此可时态回放。同值幂等（不重复追加）。"""
    prev = _latest_fact(
        db, ontology_id, link_instance_id, "exists", kind="link")
    if prev is not None and (prev.value or {}).get("v") == exists:
        return None  # 幂等：状态没变不追加
    release_version, release_id = _runtime_lineage(
        db, ontology_id, ontology_version, ontology_release_id)
    fact = PropertyFact(
        ontology_id=ontology_id,
        instance_id=link_instance_id,
        object_type_id=link_type_id,
        property_name="exists",
        value=_wrap(exists),
        kind="link",
        source=source,
        actor_id=actor_id,
        caused_by=caused_by,
        ontology_version=release_version,
        ontology_release_id=release_id,
        source_dataset_version_id=source_dataset_version_id,
        supersedes_id=prev.id if prev is not None else None,
        seq=(prev.seq or 0) + 1 if prev is not None else 1,
    )
    if _append_fact(db, fact):
        db.flush()
    return fact


def record_link_property_facts(
    db: Session,
    *,
    ontology_id: str,
    instance_id: str,
    link_type_id: Optional[str],
    old_props: Optional[dict],
    new_props: dict,
    source: str,
    actor_id: Optional[str] = None,
    caused_by: Optional[str] = None,
    ontology_version: Optional[str] = None,
    ontology_release_id: Optional[str] = None,
    source_dataset_version_id: Optional[str] = None,
) -> list[PropertyFact]:
    """链接属性事实（kind='link' 的属性级变更）。

    与 record_link_fact（kind='link', property_name='exists'）共用
    (instance_id, kind) 空间：exists 事实记存在性，这里记边自身的业务属性
    （如权重、生效日期）。old_props 为 None 表示链接新建。数字孪生对关系的
    时态回放依赖本函数——边属性变化与对象属性变化同等留痕。
    """
    return record_property_facts(
        db,
        ontology_id=ontology_id,
        instance_id=instance_id,
        object_type_id=link_type_id,
        old_props=old_props,
        new_props=new_props,
        source=source,
        actor_id=actor_id,
        caused_by=caused_by,
        kind="link",
        ontology_version=ontology_version,
        ontology_release_id=ontology_release_id,
        source_dataset_version_id=source_dataset_version_id,
    )


def _record_object_existence(
    db: Session,
    *,
    ontology_id: str,
    instance_id: str,
    object_type_id: Optional[str],
    exists: bool,
    source: str,
    actor_id: Optional[str] = None,
    caused_by: Optional[str] = None,
    ontology_version: Optional[str] = None,
    ontology_release_id: Optional[str] = None,
    source_dataset_version_id: Optional[str] = None,
) -> Optional[PropertyFact]:
    """Append an object-existence edge when the state actually changes."""
    prev = _latest_fact(
        db, ontology_id, instance_id, "exists", kind="object")
    if prev is not None and (prev.value or {}).get("v") is exists:
        return None
    release_version, release_id = _runtime_lineage(
        db, ontology_id, ontology_version, ontology_release_id)
    fact = PropertyFact(
        ontology_id=ontology_id,
        instance_id=instance_id,
        object_type_id=object_type_id,
        property_name="exists",
        value=_wrap(exists),
        kind="object",
        source=source,
        actor_id=actor_id,
        caused_by=caused_by,
        ontology_version=release_version,
        ontology_release_id=release_id,
        source_dataset_version_id=source_dataset_version_id,
        supersedes_id=prev.id if prev is not None else None,
        seq=(prev.seq or 0) + 1 if prev is not None else 1,
    )
    if _append_fact(db, fact):
        db.flush()
    return fact


def record_object_presence(
    db: Session,
    *,
    ontology_id: str,
    instance_id: str,
    object_type_id: Optional[str],
    source: str,
    actor_id: Optional[str] = None,
    caused_by: Optional[str] = None,
    ontology_version: Optional[str] = None,
    ontology_release_id: Optional[str] = None,
    source_dataset_version_id: Optional[str] = None,
) -> Optional[PropertyFact]:
    """实例新建或墓碑后重建（kind='object'，exists=True）。"""
    return _record_object_existence(
        db,
        ontology_id=ontology_id,
        instance_id=instance_id,
        object_type_id=object_type_id,
        exists=True,
        source=source,
        actor_id=actor_id,
        caused_by=caused_by,
        ontology_version=ontology_version,
        ontology_release_id=ontology_release_id,
        source_dataset_version_id=source_dataset_version_id,
    )


def record_object_tombstone(
    db: Session,
    *,
    ontology_id: str,
    instance_id: str,
    object_type_id: Optional[str],
    source: str,
    actor_id: Optional[str] = None,
    caused_by: Optional[str] = None,
    ontology_version: Optional[str] = None,
    ontology_release_id: Optional[str] = None,
    source_dataset_version_id: Optional[str] = None,
) -> Optional[PropertyFact]:
    """实例删除墓碑（kind='object'，exists=False）。投影硬删后，
    事实流仍能回答"它存在过、何时被谁删除"。幂等。"""
    return _record_object_existence(
        db,
        ontology_id=ontology_id,
        instance_id=instance_id,
        object_type_id=object_type_id,
        exists=False,
        source=source,
        actor_id=actor_id,
        caused_by=caused_by,
        ontology_version=ontology_version,
        ontology_release_id=ontology_release_id,
        source_dataset_version_id=source_dataset_version_id,
    )


def record_absence_fact(
    db: Session,
    *,
    ontology_id: str,
    subject_id: str,
    empty: bool,
    scanned: int,
    source: str,
    detail: Optional[dict] = None,
    ontology_version: Optional[str] = None,
    ontology_release_id: Optional[str] = None,
) -> Optional[PropertyFact]:
    """缺席事实（kind='absence'）：连"没有"也要溯源。

    对齐演示语义（f008: BackupCrew(CA1234) = ∅, scanned_facts: 42）：
    常驻查询（哨兵）每次评估都会扫描候选集，查询结果为空/非空的**状态翻转**
    被冻结为事实——将来即使数据补上了，也能回放"当时确实查过、确实是空的，
    结论是基于这个缺席推出的"。同状态不重复追加（边沿语义，防止定时扫描刷屏）。

    subject_id 为查询主体（哨兵 id）；value = {empty, scanned, ...detail}。
    """
    prev = _latest_fact(
        db, ontology_id, subject_id, "query_result", kind="absence")
    prev_empty = None
    if prev is not None:
        pv = (prev.value or {}).get("v")
        if isinstance(pv, dict):
            prev_empty = pv.get("empty")
    if prev_empty is not None and prev_empty == empty:
        return None  # 状态未翻转，不追加
    payload: dict = {"empty": empty, "scanned": scanned}
    if detail:
        payload.update(detail)
    release_version, release_id = _runtime_lineage(
        db, ontology_id, ontology_version, ontology_release_id)
    fact = PropertyFact(
        ontology_id=ontology_id,
        instance_id=subject_id,
        object_type_id=None,
        property_name="query_result",
        value=_wrap(payload),
        kind="absence",
        source=source,
        ontology_version=release_version,
        ontology_release_id=release_id,
        supersedes_id=prev.id if prev is not None else None,
        seq=(prev.seq or 0) + 1 if prev is not None else 1,
    )
    if _append_fact(db, fact):
        db.flush()
    return fact


def record_decision_fact(
    db: Session,
    *,
    ontology_id: str,
    action_log_id: str,
    decision: str,
    source: str,
    actor_id: Optional[str],
    reason: Optional[str] = None,
    ontology_version: Optional[str] = None,
    ontology_release_id: Optional[str] = None,
) -> PropertyFact:
    """HITL 决策事实（kind='decision'）：批准与拒绝都要记录、都可回放。
    subject=动作执行日志，predicate=decision，value=APPROVED/REJECTED。"""
    prev = _latest_fact(
        db, ontology_id, action_log_id, "decision", kind="decision")
    release_version, release_id = _runtime_lineage(
        db, ontology_id, ontology_version, ontology_release_id)
    fact = PropertyFact(
        ontology_id=ontology_id,
        instance_id=action_log_id,
        object_type_id=None,
        property_name="decision",
        value=_wrap({"decision": decision, "reason": reason} if reason else decision),
        kind="decision",
        source=source,
        actor_id=actor_id,
        ontology_version=release_version,
        ontology_release_id=release_id,
        supersedes_id=prev.id if prev is not None else None,
        seq=(prev.seq or 0) + 1 if prev is not None else 1,
    )
    if _append_fact(db, fact):
        db.flush()
    return fact
