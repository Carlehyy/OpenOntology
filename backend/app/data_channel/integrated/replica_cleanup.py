"""Local cleanup lifecycle for stopped replicas; the executor owns source IO.

``request_stop`` (replica_control) fences an asset immediately; the executor
reconciles each capture and calls :func:`confirm_detached` to prove a terminal
member is no longer applied or published before it is detached from the
cohort. Detaching rewrites only the member's own flag inside ``tables_json``
— schema/table/asset/run identities stay, live members of the shared capture
are never touched, and ``capture.stopped`` itself stays with the executor
(child process stop + source cleanup must succeed first).

:func:`purge_asset` is the asset-DELETE hook: it only fires for a stopped
replica whose every run is physically cleaned and detached from its capture
(or the capture itself is stopped and cleaned), then deletes exactly this
asset's snapshot batches, runs and replica row — never another asset's data.
When the whole capture is stopped with its source cleanup confirmed
(``cleaned_at``) and every remaining run is terminal, cleaned and
unreferenced, its receipts/transactions/capture row go too (the connection FK
is RESTRICT, so captures must be deletable or connections never die). No
source IO, no commit: the caller owns the transaction.
"""
from __future__ import annotations

import sqlalchemy as sa

from app.data_channel.integrated.replica_capture_models import (
    IntegratedReplicaCapture, IntegratedReplicaReceipt, IntegratedReplicaTransaction,
)
from app.data_channel.integrated.replica_models import (
    IntegratedReplica, IntegratedReplicaRun,
)
from app.data_channel.integrated.replica_snapshot_models import (
    IntegratedReplicaSnapshotBatch,
)

TERMINAL_RUN_STATUSES = frozenset({"retired", "failed", "cancelled"})


class ReplicaCleanupError(RuntimeError):
    """确定性拒绝（错误码封闭集合）。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _members(capture) -> list[dict]:
    members = capture.tables_json
    if not isinstance(members, list) or not members or any(
            not isinstance(member, dict) for member in members):
        raise ReplicaCleanupError(
            "CAPTURE_TABLES_INVALID", "capture tables_json is not a member list")
    return members


def confirm_detached(db, capture_id: str) -> int:
    """标记 tables_json 中已 terminal 且指针已断的成员 ``detached=true``。

    锁序 replica → run → capture（与 apply/publication 相同）。只有
    retired/failed/cancelled 且没有任何 replica 的 active/pending 指针指向的
    成员才可标记；live 成员与无法证明 terminal 的条目原样保留。幂等，返回
    本次新标记数。绝不改动成员的 schema/table/asset/run 身份键。
    """
    initial = db.get(IntegratedReplicaCapture, str(capture_id))
    if initial is None:
        raise ReplicaCleanupError("CAPTURE_MISSING", "capture row is missing")
    members = _members(initial)
    replicas = {row.asset_id: row for row in db.scalars(sa.select(IntegratedReplica).where(
        IntegratedReplica.asset_id.in_(sorted({m["asset_id"] for m in members
                                               if isinstance(m.get("asset_id"), str)})))
        .order_by(IntegratedReplica.asset_id).with_for_update()
        .execution_options(populate_existing=True))}
    runs = {row.id: row for row in db.scalars(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.id.in_(sorted({m["run_id"] for m in members
                                            if isinstance(m.get("run_id"), str)})))
        .order_by(IntegratedReplicaRun.id).with_for_update()
        .execution_options(populate_existing=True))}
    capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == str(capture_id)).with_for_update()
        .execution_options(populate_existing=True))
    if capture is None:
        raise ReplicaCleanupError("CAPTURE_MISSING", "capture row is missing")
    marked = 0
    updated = []
    for member in _members(capture):
        entry = dict(member)
        run = runs.get(entry.get("run_id"))
        replica = replicas.get(entry.get("asset_id"))
        terminal = run is not None and run.status in TERMINAL_RUN_STATUSES
        unreferenced = replica is None or (
            replica.active_run_id != entry.get("run_id")
            and replica.pending_run_id != entry.get("run_id"))
        if entry.get("detached") is not True and terminal and unreferenced:
            entry["detached"] = True  # 其余成员键（含 schema 元数据）原样保留
            marked += 1
        updated.append(entry)
    if marked:
        capture.tables_json = updated
        db.flush()
    return marked


def _detached_from_capture(capture, run) -> bool:
    """run 已从其 capture 证明脱离：成员显式 detached，或 capture 整体停止
    且已清理（executor 的最终态覆盖逐成员证明）。"""
    if capture is None:
        return True  # capture 行已删除：无 cohort 可脱离
    member = next((entry for entry in _members(capture)
                   if entry.get("run_id") == run.id
                   and entry.get("asset_id") == run.asset_id), None)
    if member is None:
        # capture 整体停止并清理过的替代证明仍然可用，否则 fail closed。
        return capture.status == "stopped" and capture.cleaned_at is not None
    return (member.get("detached") is True
            or (capture.status == "stopped" and capture.cleaned_at is not None))


def purge_asset(db, asset_id: str) -> bool:
    """资产 DELETE 的本地清理钩子；条件不满足时返回 False（不动任何行）。

    仅当 replica stopped、该资产全部 run 已物理清理（cleaned_at）且已从其
    capture 脱离时，删除本资产的 snapshot batches/runs/replica；共享 capture
    里其它资产的数据绝不触碰。整个 capture 停止且自有 run 全部清理、无
    active/pending 引用时，一并删除该 capture 的 receipts/transactions/
    capture 行（connection FK RESTRICT）。调用方持有事务，本模块不 commit。
    """
    replica = db.scalar(sa.select(IntegratedReplica).where(
        IntegratedReplica.asset_id == str(asset_id)).with_for_update()
        .execution_options(populate_existing=True))
    if replica is None or replica.status != "stopped":
        return False
    runs = list(db.scalars(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.asset_id == str(asset_id)).order_by(IntegratedReplicaRun.id)
        .with_for_update().execution_options(populate_existing=True)))
    if any(run.cleaned_at is None for run in runs):
        return False
    capture_ids = sorted({run.capture_id for run in runs if run.capture_id})
    captures = {row.id: row for row in db.scalars(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id.in_(capture_ids)).order_by(IntegratedReplicaCapture.id)
        .with_for_update().execution_options(populate_existing=True))}
    for run in runs:
        if run.capture_id and not _detached_from_capture(
                captures.get(run.capture_id), run):
            return False

    # 只删本资产：snapshot batches → runs → replica（FK RESTRICT 顺序）。
    for run in runs:
        db.execute(sa.delete(IntegratedReplicaSnapshotBatch).where(
            IntegratedReplicaSnapshotBatch.run_id == run.id))
    db.flush()
    for run in runs:
        db.delete(run)
    db.flush()
    db.delete(replica)
    db.flush()

    # Cohort 级拆除：见 _teardown_stopped_captures（cleaned_at 必需）。
    _teardown_stopped_captures(db, set(capture_ids))
    db.flush()
    return True


def _teardown_stopped_captures(db, capture_ids) -> int:
    """删除已停止且源资源清理已确认的 capture 及其 journal。

    仅当 capture ``stopped`` **且** ``cleaned_at`` 确认源清理、剩余 run 全部
    terminal+已物理清理、无 active/pending 引用时执行（journal 绝不为仍需
    消费的 run 删除）。返回删除的 capture 数；条件不满足时原样保留。
    """
    purged = 0
    for capture_id in sorted(capture_ids):
        capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
            IntegratedReplicaCapture.id == capture_id).with_for_update()
            .execution_options(populate_existing=True))
        if (capture is None or capture.status != "stopped"
                or capture.cleaned_at is None):
            continue
        remaining = list(db.scalars(sa.select(IntegratedReplicaRun).where(
            IntegratedReplicaRun.capture_id == capture_id)))
        if any(run.status not in TERMINAL_RUN_STATUSES
               or run.cleaned_at is None for run in remaining):
            continue
        run_ids = [run.id for run in remaining]
        referenced = db.scalar(sa.select(sa.func.count()).select_from(IntegratedReplica).where(
            sa.or_(IntegratedReplica.active_run_id.in_(run_ids),
                   IntegratedReplica.pending_run_id.in_(run_ids))))
        if referenced:
            continue
        db.execute(sa.delete(IntegratedReplicaReceipt).where(
            IntegratedReplicaReceipt.capture_id == capture_id))
        db.execute(sa.delete(IntegratedReplicaTransaction).where(
            IntegratedReplicaTransaction.capture_id == capture_id))
        db.delete(capture)
        purged += 1
    if purged:
        db.flush()
    return purged
