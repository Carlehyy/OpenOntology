"""把 journal 已确认提交的事务应用到一个副本 run 的物理表（调用方事务内）。

围栏与游标：

- 锁序 replica → run → capture（与 replica_control 的 replica→run、journal 的
  capture 一致，无环）。active run 必须仍被 ``replica.active_run_id`` 指向；
  catching_up run 必须仍被 ``pending_run_id`` 指向且 ``owner_token`` 匹配、
  租约未过期（owner_token 为本函数可选参数，catching_up 必传）。
  capture status ∈ {gap, blocked} 拒绝应用。
- 游标 ``run.applied_position = {capture_id, commit_seq, source_position}``，
  只按 capture 侧已通过 END 全序校验的 ``commit_seq`` 单调前进、绝不回退；
  行 WAL LSN/binlog pos 只是事件身份，不构成提交序（交错事务按 END 先后
  应用）。仅 ``commit_seq IS NOT NULL`` 的完整事务会被应用（partial END 不
  apply）。active run 应用后同步 ``replica.applied_position/applied_at``；
  coverage 只由显式确认推进，与本模块无关。
- 一个 capture 服务全部表：仅应用与本 run 资产 ``(schema_name, table_name)``
  匹配的事件，同事务其它表的事件按序跳过。当前链路是 initial snapshot
  （批量 COPY）→ CDC c/u/d 完整事务；snapshot READ 不进入本模块。

值解码与顺序应用：

- receipt.envelope_json 是原 Debezium ``{schema, payload}`` 精确文本。事件头
  与逐列 wire 转换统一走 ``replica_codec``（严格 manifest/信封列 schema 合约
  校验、JSON 原文/精确 decimal/base64/ISO 时间、SQL NULL 与 JSON null 分
  立）；本模块绝不经 ``cdc_envelope.decode_envelope`` 做有损像预解码。
- 每条变更按回执序**逐条**在同一 db 事务内 ``store.apply_changes``：同一事
  务内同一行的连续更新中，TOAST/不可用占位从前一条变更刚落下的本地像补
  unchanged 字段（绝不读事务前旧表）；主键存在性差分（row_count 维护）同样
  基于逐条应用后的真实本地状态，绝不做全表 COUNT。
- 占位补值缺行（或本地 NULL 且 before 像也不可用）时明确报错，绝不回源。
- 主键变更：MySQL 的 d+c 事件对按序自然还原；PG u 事件 before/after 主键
  不一致时以 ``before_key`` 先删旧键再 upsert。
- 事务原子性由调用方事务回滚保证，本模块绝不 commit/rollback。
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import sqlalchemy as sa

from app.data_channel.integrated import replica_codec as codec
from app.data_channel.integrated import replica_store as store
from app.data_channel.integrated.cdc_envelope import TOAST_PLACEHOLDER
from app.data_channel.integrated.contracts import wire_value
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_capture_models import (
    IntegratedReplicaCapture, IntegratedReplicaReceipt, IntegratedReplicaTransaction,
)
from app.data_channel.integrated.replica_codec import ReplicaCodecError
from app.data_channel.integrated.replica_models import (
    IntegratedReplica, IntegratedReplicaRun,
)
from app.data_channel.integrated.replica_store import ReplicaChange, _decode_key


class ReplicaApplyError(RuntimeError):
    """确定性拒绝（错误码封闭集合）；message 绝不携带源行值。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None \
        else value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 本地表访问（同一 db 事务内的当前状态：存在性差分 + TOAST 补值）
# ---------------------------------------------------------------------------

class _Local:
    """副本物理表的当前事务内视图；每个键直接探测，绝不全表 COUNT。"""

    def __init__(self, conn, schema):
        self._conn = conn
        self._schema = schema
        self.table = store.build_table(schema, conn.dialect.name)

    def _conditions(self, key_wire: dict):
        key = _decode_key(self._schema, key_wire, label="change key")
        return [self.table.c[self._schema.column_map[name]] == key[name]
                for name in self._schema.primary_key]

    def exists(self, key_wire: dict) -> bool:
        found = self._conn.execute(sa.select(1).where(
            sa.and_(*self._conditions(key_wire))).limit(1)).scalar()
        return found is not None

    def column_value(self, source_name: str, key_wire: dict):
        """返回 (行存在, 本地值)；json 列取存储原文（CAST AS TEXT）。"""
        column = self._schema.column(source_name)
        expression = self.table.c[self._schema.column_map[source_name]]
        if column.spec.type == "json":
            expression = sa.cast(expression, sa.Text())
        row = self._conn.execute(sa.select(expression).where(
            sa.and_(*self._conditions(key_wire))).limit(1)).first()
        return row is not None, (row[0] if row is not None else None)


# ---------------------------------------------------------------------------
# 单条事件 → ReplicaChange（TOAST 从当前本地像补值）
# ---------------------------------------------------------------------------

def _require_pk(pk_wire: dict) -> None:
    for name, value in pk_wire.items():
        if value is None or value is codec.UNAVAILABLE:
            raise ReplicaApplyError(
                "IMAGE_INCOMPLETE", f"primary key column {name!r} is null or unavailable")


def _fill_unavailable(local: _Local, pk_wire: dict, before_raw, values: dict) -> None:
    for name, value in list(values.items()):
        if value is not codec.UNAVAILABLE:
            continue
        found, current = local.column_value(name, pk_wire)
        if not found:
            raise ReplicaApplyError(
                "TOAST_UNAVAILABLE",
                f"column {name!r} is unchanged (TOAST) but the local row is missing")
        if current is None and isinstance(before_raw, dict) \
                and before_raw.get(name) == TOAST_PLACEHOLDER:
            raise ReplicaApplyError(
                "TOAST_UNAVAILABLE",
                f"column {name!r} is unavailable in both the local value and the before image")
        values[name] = wire_value(current)


def _decode_receipt(raw: str):
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ReplicaApplyError(
            "RECEIPT_INVALID", "receipt envelope is not valid JSON") from exc
    try:
        header = codec.decode_change_header(parsed)
    except ReplicaCodecError as exc:
        raise ReplicaApplyError(exc.code, "receipt header rejected by codec") from exc
    return header, parsed


def _apply_change(conn, schema, manifest, header, envelope) -> int:
    """应用单条匹配事件并返回 row_count 差分（存在性在本次应用前探测）。"""
    local = _Local(conn, schema)
    pk_names = list(schema.primary_key)
    if header.op in {"c", "u"}:
        try:
            values = codec.decode_source_image(
                envelope, manifest, image="after", allow_unavailable=True)
            before_key = None
            if header.op == "u" and isinstance(header.before, dict) \
                    and all(name in header.before for name in pk_names):
                old_raw = {name: header.before[name] for name in pk_names}
                new_raw = {name: header.after[name] for name in pk_names}
                if old_raw != new_raw:
                    before_key = codec.decode_source_image(
                        envelope, manifest, image="before", columns=pk_names)
        except ReplicaCodecError as exc:
            raise ReplicaApplyError(exc.code, "receipt image rejected by codec") from exc
        if not isinstance(values, dict):
            raise ReplicaApplyError(
                "IMAGE_INCOMPLETE", "change event lacks an after image")
        pk_wire = {name: values[name] for name in pk_names}
        _require_pk(pk_wire)
        if before_key is not None:
            _require_pk(before_key)
        # TOAST 补值基于逐条应用后的当前本地像（同事务前一条变更可见）。
        # 主键变更时本地行仍挂在旧键下：未变更的 TOAST 列必须从旧键补，
        # 不能查新键（新键尚未写入）。
        _fill_unavailable(local, before_key if before_key is not None else pk_wire,
                          header.before, values)
        delta = 0
        if before_key is not None and local.exists(before_key):
            delta -= 1
        if not local.exists(pk_wire):
            delta += 1
        store.apply_changes(conn, schema, [
            ReplicaChange(op="upsert", values=values, before_key=before_key)])
        return delta
    # delete：只需要精确主键（PG 默认 replica identity 的 before 像仅含 PK）。
    try:
        key = codec.decode_source_image(
            envelope, manifest, image="before", columns=pk_names)
    except ReplicaCodecError as exc:
        raise ReplicaApplyError(exc.code, "receipt image rejected by codec") from exc
    if not isinstance(key, dict):
        raise ReplicaApplyError(
            "IMAGE_INCOMPLETE", "delete event lacks a before image")
    _require_pk(key)
    delta = -1 if local.exists(key) else 0
    store.apply_changes(conn, schema, [ReplicaChange(op="delete", key=key)])
    return delta


def _apply_transaction(conn, schema, manifest, asset, receipts) -> int:
    """按回执序逐条应用本 run 表的变更；返回整事务 row_count 差分。"""
    delta = 0
    for raw in receipts:
        header, envelope = _decode_receipt(raw)
        if (header.schema_name != asset.schema_name
                or header.table_name != asset.table_name):
            continue  # 同一 capture 的其它表：不是本 run 的变更
        delta += _apply_change(conn, schema, manifest, header, envelope)
    return delta


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _advance(replica, run, transaction, *, active: bool) -> None:
    position = {"capture_id": run.capture_id,
                "commit_seq": transaction.commit_seq,
                "source_position": copy.deepcopy(transaction.source_position)}
    run.applied_position = position
    if active:
        replica.applied_position = copy.deepcopy(position)
        replica.applied_at = _now()


def apply_run(db, run_id: str, *, owner_token: str | None = None,
              max_transactions: int = 100) -> int:
    """按 commit_seq 顺序应用至多 ``max_transactions`` 个完整事务。

    返回实际应用的事务数。全部读写都在 ``db`` 的当前事务内（含每条
    ``store.apply_changes`` 与本地行探测），本模块绝不 commit/rollback；任
    何失败由调用方回滚整体保证原子性。
    """
    if (isinstance(max_transactions, bool) or not isinstance(max_transactions, int)
            or max_transactions < 1):
        raise ReplicaApplyError(
            "INVALID_MAX_TRANSACTIONS", "max_transactions must be a positive integer")
    initial = db.get(IntegratedReplicaRun, str(run_id))
    if initial is None:
        raise ReplicaApplyError("REPLICA_RUN_MISSING", "replica run not found")
    replica = db.scalar(sa.select(IntegratedReplica).where(
        IntegratedReplica.asset_id == initial.asset_id).with_for_update()
        .execution_options(populate_existing=True))
    if replica is None:
        raise ReplicaApplyError(
            "REPLICA_NOT_REGISTERED", "replica row is missing for the run asset")
    run = db.scalar(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.id == str(run_id)).with_for_update()
        .execution_options(populate_existing=True))
    if run.status not in {"active", "catching_up"}:
        raise ReplicaApplyError(
            "REPLICA_RUN_STATUS_INVALID", f"run status {run.status!r} cannot apply")
    if not run.capture_id:
        raise ReplicaApplyError("CAPTURE_MISSING", "run has no bound capture")
    active = run.status == "active"
    if active:
        if replica.active_run_id != run.id:
            raise ReplicaApplyError(
                "ACTIVE_POINTER_LOST", "replica no longer exposes this run")
    elif (replica.pending_run_id != run.id or replica.generation != run.generation
            or not isinstance(owner_token, str) or not owner_token
            or run.owner_token != owner_token or run.lease_expires_at is None
            or _utc(run.lease_expires_at) <= _now()):
        raise ReplicaApplyError(
            "REPLICA_OWNER_LOST", "catching-up run ownership fence failed")
    capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == run.capture_id).with_for_update()
        .execution_options(populate_existing=True))
    if capture is None:
        raise ReplicaApplyError("CAPTURE_MISSING", "capture row is missing")
    if capture.status in {"gap", "blocked"}:
        raise ReplicaApplyError(
            "CAPTURE_NOT_APPLYABLE", f"capture status {capture.status!r} refuses apply")
    asset = db.get(IntegratedAsset, run.asset_id)
    if asset is None:
        raise ReplicaApplyError("REPLICA_ASSET_MISSING", "asset row is missing")
    if asset.schema_fingerprint != run.schema_fingerprint:
        raise ReplicaApplyError(
            "SCHEMA_MISMATCH", "asset schema drifted from the run manifest")
    if capture.connection_id != asset.connection_id:
        raise ReplicaApplyError(
            "CAPTURE_ASSET_MISMATCH", "capture belongs to another connection")

    cursor_seq = 0
    if run.applied_position is not None:
        cursor = run.applied_position
        if (not isinstance(cursor, dict)
                or cursor.get("capture_id") != run.capture_id
                or isinstance(cursor.get("commit_seq"), bool)
                or not isinstance(cursor.get("commit_seq"), int)
                or cursor["commit_seq"] < 0):
            raise ReplicaApplyError(
                "CURSOR_INVALID", "applied_position does not reference this capture")
        cursor_seq = cursor["commit_seq"]

    try:
        # 行绑定必须绑进 run 的冻结指纹：schema_json 与 schema_fingerprint
        # 任何漂移都在应用前拒绝，绝不用漂移 manifest 解 receipt。
        schema = store.build_replica_schema(
            run.id, run.schema_json, schema_fingerprint=run.schema_fingerprint)
    except store.ReplicaStoreError as exc:
        raise ReplicaApplyError("MANIFEST_INVALID", exc.message) from exc
    manifest = run.schema_json
    conn = db.connection()
    transactions = db.scalars(sa.select(IntegratedReplicaTransaction).where(
        IntegratedReplicaTransaction.capture_id == run.capture_id,
        IntegratedReplicaTransaction.commit_seq.is_not(None),
        IntegratedReplicaTransaction.commit_seq > cursor_seq)
        .order_by(IntegratedReplicaTransaction.commit_seq)
        .limit(max_transactions)).all()
    applied = 0
    for transaction in transactions:
        receipts = db.scalars(sa.select(IntegratedReplicaReceipt.envelope_json).where(
            IntegratedReplicaReceipt.transaction_id == transaction.id)
            .order_by(IntegratedReplicaReceipt.ordinal)).all()
        if len(receipts) != transaction.event_count:
            raise ReplicaApplyError(
                "CAPTURE_INCOMPLETE", "receipt count disagrees with the END event_count")
        delta = _apply_transaction(conn, schema, manifest, asset, receipts)
        if run.row_count + delta < 0:
            raise ReplicaApplyError(
                "ROW_COUNT_UNDERFLOW", "differential row_count went negative")
        run.row_count += delta
        _advance(replica, run, transaction, active=active)
        applied += 1
    db.flush()
    return applied
