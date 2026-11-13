"""replica_journal 聚焦测试（真实 SQLite 库；Debezium 实源端到端归并行 live 验证）。

契约被测面：TransportRecord 包装（schema.name=io.openontology.replica.TransportRecord，
fields topic/partition_json/offset_json/event/key，event 内为原 Debezium envelope 的
schema+payload；Initial Snapshot 通知允许空 offset，row/事务/心跳仍强制非空）；
行按源事务分组（PG 行按 ``<xid>:`` 前缀挂到未完事务，xid wrap 复用按行 LSN 分
epoch、重复 END 用 stored commit_lsn 识别），仅当 END 的 event_count 与 1..N 全序
校验通过才推进 committed_seq；精确 JSON 持久回执（Decimal 字面量逐字保存）；
重复 row/END 不回退不重复计数，处理时间戳 ts_ms/ts_us/ts_ns 漂移不误报冲突，
wire 键序漂移同理；MySQL END 顺序只按完整 SourceRecord offset 坐标
(file,pos[,event]) 判定、绝不比较跨 UUID GTID 后缀；回收 prune_before/prune_applied
只在显式 replay floor + 所有关联 run 已 apply 越过时删除，边界相等/事务内 offset
一律保留，floor 持久化于 committed_position["replay_floor"] 且不覆盖 position；
回收后的旧重投绝不倒灌——PG 行一律先缓冲（long TX 行 LSN 可早于 floor.commit_lsn，
绝不按 rowLSN 与全局 floor 过滤），END 的 commit_lsn<=floor 才确认 stale 并原子删
临时 receipt/transaction 扣回 pending_bytes，END>floor 的（含 floor 前开始/后提交
的 long TX）正常保留应用；MySQL binlog 串行，行可按坐标直接安全跳过。
分区/形状/限额/批次越界一律拒绝，整批随调用方事务回滚、journal 自身绝不 commit。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset
from app.data_channel.integrated import replica_journal as journal
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_capture_models import (
    IntegratedReplicaCapture, IntegratedReplicaReceipt, IntegratedReplicaTransaction,
)
from app.data_channel.integrated.replica_journal import ReplicaJournalError, append_batch
from app.data_channel.integrated.replica_models import IntegratedReplicaRun
from app.shared.exact_json import dumps_exact, loads_exact

CAPTURE_ID = "2b31a4c8-6f9d-4e0a-9c2d-7a5e1f0b3c44"
SERVER = "oo_replica_" + uuid.UUID(CAPTURE_ID).hex
TS_MS = 1760000000000
GTID_A = "3e11fa47-71ca-11e1-9e33-c80aa9429562"

EVENT_FIELD = {
    "field": "event",
    "name": "io.debezium.connector.postgresql.Envelope",
    "type": "struct",
    "fields": [
        {"field": "op", "type": "string"},
        {"field": "ts_ms", "type": "int64"},
        {"field": "source", "type": "struct"},
        {"field": "transaction", "type": "struct", "optional": True},
        {"field": "before", "type": "struct", "optional": True},
        {"field": "after", "type": "struct", "optional": True},
    ],
    "optional": True,
}
TRANSPORT_SCHEMA = {
    "type": "struct",
    "name": "io.openontology.replica.TransportRecord",
    "fields": [
        {"field": "topic", "type": "string"},
        {"field": "partition_json", "type": "string"},
        {"field": "offset_json", "type": "string"},
        {"field": "key", "type": "string", "optional": True},
        EVENT_FIELD,
    ],
}


# ---------------------------------------------------------------------------
# 构造器：值经 dumps_exact → journal.loads 往返，等价于接收端 parse_float=Decimal
# 的 HTTP 边界，绝不引入 double 损耗。
# ---------------------------------------------------------------------------

def wire(record):
    return journal.loads(dumps_exact(record))


def transport(event, *, server=SERVER, offset=None, partition=None):
    partition = {"server": server} if partition is None else partition
    payload = {"topic": "oo.public.orders", "key": None,
               "partition_json": dumps_exact(partition),
               "offset_json": dumps_exact({"lsn": 1} if offset is None else offset)}
    if event is not None:
        payload["event"] = event
    return {"schema": TRANSPORT_SCHEMA, "payload": payload}


def pg_event(xid, row_lsn, ordinal, *, op="c", after=None):
    event = {
        "op": op, "ts_ms": TS_MS,
        "source": {"db": "oodb", "schema": "public", "table": "orders",
                   "lsn": row_lsn, "txId": int(xid)},
        "transaction": {"id": f"{xid}:{row_lsn}", "total_order": ordinal,
                        "data_collection_order": ordinal},
    }
    if after is not None:
        event["after"] = after
    return event


def pg_row(xid, row_lsn, ordinal, *, op="c", after=None, server=SERVER, partition=None):
    return transport(
        pg_event(xid, row_lsn, ordinal, op=op, after=after),
        server=server, offset={"lsn": row_lsn, "txId": int(xid)}, partition=partition)


def pg_end(xid, commit_lsn, count, *, server=SERVER):
    event = {"status": "END", "id": f"{xid}:{commit_lsn}", "event_count": count,
             "data_collections": [{"event_count": count,
                                   "data_collection": "public.orders"}],
             "ts_ms": TS_MS}
    return transport(event, server=server, offset={"lsn": commit_lsn, "txId": int(xid)})


def heartbeat(offset):
    return transport(None, offset=offset)


def mysql_row(gtid, ordinal, *, pos, file="mysql-bin.000003", op="c"):
    event = {
        "op": op, "ts_ms": TS_MS,
        "source": {"db": "oodb", "table": "orders", "file": file, "pos": pos,
                   "gtid": gtid},
        "transaction": {"id": gtid, "total_order": ordinal,
                        "data_collection_order": ordinal},
    }
    return transport(event, offset={"file": file, "pos": pos, "gtids": gtid})


def mysql_end(gtid, count, *, pos, file="mysql-bin.000003", event=None):
    payload = {"status": "END", "id": gtid, "event_count": count,
               "data_collections": [{"event_count": count,
                                     "data_collection": "oodb.orders"}],
               "ts_ms": TS_MS}
    offset = {"file": file, "pos": pos, "gtids": gtid}
    if event is not None:
        offset["event"] = event
    return transport(payload, offset=offset)


def initial_notification(note_type, *, offset=None):
    payload = {"id": f"not-{note_type.lower()}", "type": note_type,
               "aggregate_type": "Initial Snapshot",
               "additional_data": {"connector": SERVER}, "timestamp": TS_MS}
    return transport(payload, offset={} if offset is None else offset)


def append(db, records, *, dialect="postgresql", capture_id=CAPTURE_ID):
    return append_batch(db, capture_id, records, dialect=dialect)


def reject(db, records, code, *, dialect="postgresql"):
    with pytest.raises(ReplicaJournalError) as exc:
        append(db, records, dialect=dialect)
    assert exc.value.code == code
    return exc.value


@pytest.fixture
def capture(db):
    db.add(Connection(id="journal-src", name="journal fixture", kind="postgres", config={}))
    db.add(IntegratedReplicaCapture(
        id=CAPTURE_ID, connection_id="journal-src", generation=1,
        status="streaming", identity_fingerprint="f" * 64, tables_json=[]))
    db.commit()
    return fresh_capture(db)


def fresh_capture(db):
    return db.scalar(sa.select(IntegratedReplicaCapture)
                     .where(IntegratedReplicaCapture.id == CAPTURE_ID)
                     .execution_options(populate_existing=True))


def receipt_count(db):
    return db.scalar(sa.select(sa.func.count()).select_from(IntegratedReplicaReceipt))


def transaction_count(db):
    return db.scalar(sa.select(sa.func.count()).select_from(IntegratedReplicaTransaction))


def transaction_by_key(db, txkey):
    return db.scalar(sa.select(IntegratedReplicaTransaction).where(
        IntegratedReplicaTransaction.capture_id == CAPTURE_ID,
        IntegratedReplicaTransaction.source_transaction_id == txkey))


def make_run(db, admin_user, capture_id, *, status, applied_seq):
    """直接落 run 行（不触 runtime）：回收门禁只看 capture 绑定与水位。"""
    suffix = uuid.uuid4().hex[:8]
    db.add(Dataset(id=f"journal-ds-{suffix}", name=f"journal ds {suffix}",
                   kind="integrated", source_connection_id="journal-src",
                   source_resource="orders"))
    db.add(IntegratedAsset(
        id=f"journal-asset-{suffix}", dataset_id=f"journal-ds-{suffix}",
        connection_id="journal-src", name="orders", schema_name="public",
        table_name="orders", schema_json={"columns": []},
        schema_fingerprint="f" * 64, created_by=admin_user.id))
    run = IntegratedReplicaRun(
        id=f"journal-run-{suffix}", asset_id=f"journal-asset-{suffix}",
        generation=1, status=status, schema_json={}, schema_fingerprint="f" * 64,
        capture_id=capture_id,
        applied_position=None if applied_seq is None else {
            "capture_id": capture_id, "commit_seq": applied_seq,
            "source_position": {}})
    db.add(run)
    db.flush()
    return run


# ---------------------------------------------------------------------------
# 完整事务：精确回执 + END 全序校验推进游标
# ---------------------------------------------------------------------------

def test_full_transaction_commits_receipts_and_advances_cursor(db, capture):
    result = append(db, [
        wire(pg_row(100, 10, 1, after={"id": 1, "amount": Decimal("123.4500")})),
        wire(pg_row(100, 20, 2)),
        wire(pg_end(100, 500, 2)),
    ])
    assert result == {"accepted": 3, "duplicates": 0,
                      "received_seq": 3, "committed_seq": 3}
    db.commit()
    capture = fresh_capture(db)
    assert capture.received_seq == capture.committed_seq == 3
    assert capture.committed_position["dialect"] == "postgresql"
    assert capture.committed_position["commit_lsn"] == 500
    assert capture.pending_bytes > 0
    transaction = transaction_by_key(db, "100")  # 首纪元用裸 xid key
    assert transaction.event_count == 2
    assert transaction.commit_seq == 3
    assert transaction.source_position["commit_lsn"] == 500
    # 行 source 无真实时间戳：留空，绝不拿 END processing ts 冒充提交时间
    assert transaction.source_committed_at is None
    assert "source_event_at" not in transaction.source_position
    receipts = db.scalars(sa.select(IntegratedReplicaReceipt)
                          .order_by(IntegratedReplicaReceipt.ordinal)).all()
    assert [r.ordinal for r in receipts] == [1, 2]
    assert [r.sequence for r in receipts] == [1, 2]
    # 精确 JSON 回执：Decimal 字面量逐字保存，字节数与原文一致
    assert "123.4500" in receipts[0].envelope_json
    assert loads_exact(receipts[0].envelope_json)["payload"]["after"]["amount"] \
        == Decimal("123.4500")
    assert receipts[0].size_bytes == len(receipts[0].envelope_json.encode("utf-8"))


def test_source_event_at_derived_from_row_source_ts_not_end_ts(db, capture):
    first = pg_event(100, 10, 1)
    first["source"]["ts_ms"] = TS_MS
    second = pg_event(100, 20, 2)
    second["source"]["ts_us"] = (TS_MS + 1500) * 1000 + 42  # 更晚的行，µs 精度胜出
    end = {"status": "END", "id": "100:500", "event_count": 2,
           "data_collections": [{"event_count": 2, "data_collection": "public.orders"}],
           "ts_ms": TS_MS + 600_000}  # END processing ts 必须被忽略
    append(db, [wire(transport(first, offset={"lsn": 10, "txId": 100})),
                wire(transport(second, offset={"lsn": 20, "txId": 100})),
                wire(transport(end, offset={"lsn": 500, "txId": 100}))])
    db.commit()
    transaction = transaction_by_key(db, "100")
    total_us = (TS_MS + 1500) * 1000 + 42
    seconds, micros = divmod(total_us, 1_000_000)
    expected = datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=micros)
    assert transaction.source_position["source_event_at"] == expected.isoformat()
    assert fresh_capture(db).committed_position["source_event_at"] == expected.isoformat()


def test_interleaved_transactions_commit_in_end_order_not_row_lsn_order(db, capture):
    # A 的行 LSN 更低但提交更晚：行 LSN 只是身份，提交序只由 END 决定
    result = append(db, [
        wire(pg_row(100, 10, 1)),   # A 行
        wire(pg_row(200, 20, 1)),   # B 行（交错）
        wire(pg_row(100, 40, 2)),   # A 行
        wire(pg_end(200, 300, 1)),  # B 先提交
        wire(pg_end(100, 500, 2)),  # A 后提交
    ])
    assert result["accepted"] == 5
    db.commit()
    capture = fresh_capture(db)
    assert capture.committed_seq == 5
    assert capture.committed_position["commit_lsn"] == 500
    assert transaction_by_key(db, "200").commit_seq == 4
    assert transaction_by_key(db, "100").commit_seq == 5


def test_commit_order_regression_rejected_and_batch_rolls_back(db, capture):
    append(db, [wire(pg_row(200, 20, 1)), wire(pg_end(200, 300, 1))])
    db.commit()
    # 新事务的 END 位点不高于已提交位点：源提交序被破坏，拒绝且不留部分事实
    reject(db, [wire(pg_row(300, 50, 1)), wire(pg_end(300, 250, 1))], "COMMIT_ORDER_CHANGED")
    db.rollback()
    capture = fresh_capture(db)
    assert (capture.received_seq, capture.committed_seq) == (2, 2)
    assert capture.committed_position["commit_lsn"] == 300
    assert receipt_count(db) == 1
    assert transaction_count(db) == 1


# ---------------------------------------------------------------------------
# 重复投递：不回退、不重复计数、键序/处理时间戳漂移不误报
# ---------------------------------------------------------------------------

def committed_pair():
    return [wire(pg_row(100, 10, 1, after={"id": 1, "amount": Decimal("5.00")})),
            wire(pg_end(100, 500, 1))]


def test_replayed_batch_is_duplicate_without_regression(db, capture):
    first = append(db, committed_pair())
    db.commit()
    replay = append(db, committed_pair())
    assert replay == {"accepted": 0, "duplicates": 2,
                      "received_seq": first["received_seq"],
                      "committed_seq": first["committed_seq"]}
    db.commit()
    capture = fresh_capture(db)
    assert (capture.received_seq, capture.committed_seq) == (2, 2)  # 不增长
    assert capture.committed_position["commit_lsn"] == 500  # 不回退
    assert receipt_count(db) == 1
    assert transaction_by_key(db, "100").event_count == 1
    assert transaction_by_key(db, "100").commit_seq == 2


def test_duplicate_detection_ignores_wire_key_order(db, capture):
    # 首投：partition 带附加键 + 常规事件键序
    first = wire(pg_row(100, 10, 1, partition={"server": SERVER, "extra": 7}))
    append(db, [first, wire(pg_end(100, 500, 1))])
    db.commit()
    # 重放：partition 键序颠倒 + 事件键序颠倒，语义同一消息（内容不变）
    reordered_event = dict(reversed(list(pg_event(100, 10, 1).items())))
    replay_partition = transport(
        pg_event(100, 10, 1), partition={"extra": 7, "server": SERVER})
    replay_event = transport(reordered_event, partition={"extra": 7, "server": SERVER})
    result = append(db, [replay_partition, replay_event])
    assert result == {"accepted": 0, "duplicates": 2,
                      "received_seq": 2, "committed_seq": 2}
    db.rollback()
    assert receipt_count(db) == 1  # 同一消息绝不产生第二份回执


def test_duplicate_with_processing_ts_drift_is_replay_not_conflict(db, capture):
    # 重启后处理时间戳漂移（含新增 ts_us/ts_ns）：源身份+op+像+schema+总序不变
    append(db, committed_pair())
    db.commit()
    drifted_row = pg_event(100, 10, 1, after={"id": 1, "amount": Decimal("5.00")})
    drifted_row["ts_ms"] = TS_MS + 987_000
    drifted_row["ts_us"] = (TS_MS + 987_000) * 1000 + 7
    drifted_row["ts_ns"] = ((TS_MS + 987_000) * 1000 + 7) * 1000
    drifted_end = {"status": "END", "id": "100:500", "event_count": 1,
                   "data_collections": [{"event_count": 1,
                                         "data_collection": "public.orders"}],
                   "ts_ms": TS_MS + 5_000}
    result = append(db, [
        wire(transport(drifted_row, offset={"lsn": 10, "txId": 100})),
        wire(transport(drifted_end, offset={"lsn": 500, "txId": 100}))])
    assert result == {"accepted": 0, "duplicates": 2,
                      "received_seq": 2, "committed_seq": 2}
    db.rollback()
    assert receipt_count(db) == 1
    assert transaction_by_key(db, "100").event_count == 1


def test_duplicate_with_changed_event_rejected(db, capture):
    append(db, committed_pair())
    db.commit()
    changed = wire(pg_row(100, 10, 1, after={"id": 1, "amount": Decimal("6.00")}))
    reject(db, [changed], "REPLAY_RECORD_CHANGED")
    db.rollback()
    stored = db.scalar(sa.select(IntegratedReplicaReceipt))
    assert "5.00" in stored.envelope_json  # 首见原文不被覆盖
    assert transaction_by_key(db, "100").event_count == 1


def test_late_row_after_committed_transaction_rejected(db, capture):
    append(db, committed_pair())
    db.commit()
    reject(db, [wire(pg_row(100, 60, 2))], "TRANSACTION_REPLAY_INCOMPLETE")
    db.rollback()
    assert receipt_count(db) == 1
    assert transaction_by_key(db, "100").commit_seq == 2


# ---------------------------------------------------------------------------
# PG xid wrap：按行 LSN 分纪元，重复 END 按 stored commit_lsn 识别
# ---------------------------------------------------------------------------

def test_pg_xid_reuse_after_wrap_groups_new_epoch_by_row_lsn(db, capture):
    # 纪元 1：xid 100（裸 key），已提交且保留（未回收）
    append(db, [wire(pg_row(100, 10, 1)), wire(pg_row(100, 20, 2)),
                wire(pg_end(100, 500, 2))])
    db.commit()
    # wrap 复用同 xid：行 LSN 远超旧 commit 记录 → 新纪元不得被拒绝
    result = append(db, [wire(pg_row(100, 9000, 1, after={"id": 9})),
                         wire(pg_end(100, 9500, 1))])
    assert result["accepted"] == 2
    db.commit()
    capture = fresh_capture(db)
    assert capture.committed_position["commit_lsn"] == 9500
    assert {t.source_transaction_id for t in db.scalars(
        sa.select(IntegratedReplicaTransaction))} == {"100", "100:9000"}
    # 旧纪元重投（事务仍在）：record 身份 + commit_lsn 识别为重复，不混入新纪元
    replay = append(db, [wire(pg_row(100, 10, 1)), wire(pg_end(100, 500, 2))])
    assert replay == {"accepted": 0, "duplicates": 2,
                      "received_seq": 5, "committed_seq": 5}
    db.rollback()
    assert transaction_by_key(db, "100").event_count == 2
    assert transaction_by_key(db, "100:9000").event_count == 1


# ---------------------------------------------------------------------------
# END 全序校验：数量不符 / 序号缺口整批拒绝
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("count", [3, 1])  # 多报 / 少报
def test_transaction_count_mismatch_rolls_back_whole_batch(db, capture, count):
    reject(db, [wire(pg_row(100, 10, 1)), wire(pg_row(100, 20, 2)),
                wire(pg_end(100, 500, count))], "TRANSACTION_INCOMPLETE")
    db.rollback()
    capture = fresh_capture(db)
    assert (capture.received_seq, capture.committed_seq) == (0, 0)
    assert capture.committed_position is None
    assert receipt_count(db) == 0
    assert transaction_count(db) == 0


def test_ordinal_gap_rejected_as_incomplete(db, capture):
    # event_count 对得上但 1..N 缺口（2 丢失）：同样拒绝
    reject(db, [wire(pg_row(100, 10, 1)), wire(pg_row(100, 30, 3)),
                wire(pg_end(100, 500, 3))], "TRANSACTION_INCOMPLETE")
    db.rollback()
    assert receipt_count(db) == 0
    assert transaction_count(db) == 0


def test_empty_transaction_end_commits_zero_events(db, capture):
    result = append(db, [wire(pg_end(100, 500, 0))])
    assert result == {"accepted": 1, "duplicates": 0,
                      "received_seq": 1, "committed_seq": 1}
    transaction = transaction_by_key(db, "100")
    assert transaction.event_count == 0
    assert transaction.commit_seq == 1


# ---------------------------------------------------------------------------
# 心跳混合批次
# ---------------------------------------------------------------------------

def test_heartbeats_do_not_consume_sequence_but_refresh_liveness(db, capture):
    result = append(db, [
        wire(heartbeat({"lsn": 5})),
        wire(pg_row(100, 10, 1)),
        wire(heartbeat({"lsn": 15})),
        wire(pg_end(100, 500, 1)),
    ])
    assert result == {"accepted": 2, "duplicates": 0,
                      "received_seq": 2, "committed_seq": 2}
    db.commit()
    capture = fresh_capture(db)
    assert capture.source_offset == {"lsn": 500, "txId": 100}
    assert capture.heartbeat_at is not None
    # 纯心跳批次：不占 received_seq，但推进 source_offset 与心跳时间
    only_beats = append(db, [wire(heartbeat({"lsn": 900}))])
    assert only_beats == {"accepted": 0, "duplicates": 0,
                          "received_seq": 2, "committed_seq": 2}
    db.commit()
    capture = fresh_capture(db)
    assert capture.source_offset == {"lsn": 900}
    assert capture.received_seq == 2


def test_replayed_batch_still_refreshes_heartbeat_liveness(db, capture):
    append(db, committed_pair())
    db.commit()
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    fresh_capture(db).heartbeat_at = stale
    db.commit()
    result = append(db, committed_pair())
    assert result["duplicates"] == 2
    db.commit()
    capture = fresh_capture(db)
    # 全量重放（如 ack 丢失后的 HTTP 重试）也要证明管道活着，不能看起来停摆
    # （SQLite DateTime 列读回 naive，统一按 UTC 比较）
    assert capture.heartbeat_at is not None
    assert capture.heartbeat_at > stale.replace(tzinfo=None)
    assert capture.source_offset == {"lsn": 500, "txId": 100}


# ---------------------------------------------------------------------------
# Initial Snapshot 通知：允许空 offset，row/事务/心跳仍强制非空
# ---------------------------------------------------------------------------

def test_initial_snapshot_notification_with_empty_offset_unpacks(db, capture):
    record = wire(initial_notification("STARTED"))
    partition, offset, envelope = journal.unpack(record)
    assert offset == {}
    assert envelope["payload"]["type"] == "STARTED"
    assert envelope["payload"]["aggregate_type"] == "Initial Snapshot"
    # 通知在 ingest 消化；若泄漏进 append_batch 也不得破坏位点或计数
    result = append(db, [record,
                         wire(initial_notification("DATA_COLLECTIONS_RESOLVED"))])
    assert result == {"accepted": 0, "duplicates": 0,
                      "received_seq": 0, "committed_seq": 0}
    db.commit()
    assert fresh_capture(db).source_offset is None
    # 其它通知带完整 offset：正常放行
    assert journal.unpack(wire(initial_notification(
        "COMPLETED", offset={"lsn": 77})))[1] == {"lsn": 77}


def test_non_notification_empty_offset_still_rejected(db, capture):
    # 行/END/心跳的空 offset 依旧拒绝：通知豁免不得扩散
    reject(db, [wire(heartbeat({}))], "SOURCE_OFFSET_REQUIRED")
    db.rollback()
    event = {"aggregate_type": "Incremental Snapshot", "id": "n", "type": "STARTED",
             "timestamp": TS_MS}
    reject(db, [wire(transport(event, offset={}))], "SOURCE_OFFSET_REQUIRED")
    db.rollback()
    assert fresh_capture(db).received_seq == 0


# ---------------------------------------------------------------------------
# 分区/限额/批次/方言与事务归属
# ---------------------------------------------------------------------------

def test_wrong_or_missing_source_partition_rejected(db, capture):
    reject(db, [wire(pg_row(100, 10, 1, server="oo_replica_" + "0" * 32))],
           "SOURCE_PARTITION_CHANGED")
    db.rollback()
    reject(db, [wire(pg_row(100, 10, 1, partition={"task": 1}))],
           "SOURCE_PARTITION_REQUIRED")
    db.rollback()
    assert receipt_count(db) == 0
    assert fresh_capture(db).received_seq == 0


def test_pending_quota_exceeded_rejects_without_ack(db, capture, monkeypatch):
    monkeypatch.setattr(journal, "MAX_PENDING_BYTES", 64)
    error = reject(db, [wire(pg_row(100, 10, 1, after={"blob": "x" * 200}))],
                   "REPLICA_BACKLOG_CAPACITY")
    assert error.retryable is True
    db.rollback()
    assert receipt_count(db) == 0
    assert fresh_capture(db).received_seq == 0  # 未 ack：重试安全


def test_oversized_record_rejected(db, capture, monkeypatch):
    monkeypatch.setattr(journal, "MAX_RECORD_BYTES", 64)
    error = reject(db, [wire(pg_row(100, 10, 1, after={"blob": "x" * 200}))],
                   "SOURCE_RECORD_TOO_LARGE")
    assert error.retryable is False
    db.rollback()
    assert receipt_count(db) == 0


def test_batch_size_and_dialect_limits(db, capture):
    reject(db, [], "INVALID_TRANSPORT_BATCH")
    db.rollback()
    reject(db, "not-a-list", "INVALID_TRANSPORT_BATCH")
    db.rollback()
    limit = journal.MAX_BATCH_RECORDS
    reject(db, [wire(heartbeat({"lsn": n})) for n in range(limit + 1)],
           "INVALID_TRANSPORT_BATCH")
    db.rollback()
    # 恰好上限（全心跳）可受理
    result = append(db, [wire(heartbeat({"lsn": n})) for n in range(limit)])
    assert result["accepted"] == 0
    db.rollback()
    reject(db, [wire(pg_row(100, 10, 1))], "UNSUPPORTED_SOURCE", dialect="oracle")
    db.rollback()


def test_capture_not_accepting_gate(db, capture):
    capture.status = "blocked"
    db.commit()
    reject(db, committed_pair(), "CAPTURE_NOT_ACCEPTING")
    db.rollback()
    assert receipt_count(db) == 0


def test_caller_rollback_discards_accepted_batch(db, capture):
    # journal 绝不自行 commit：会话回滚即整批蒸发
    append(db, committed_pair())
    db.rollback()
    capture = fresh_capture(db)
    assert (capture.received_seq, capture.committed_seq) == (0, 0)
    assert capture.committed_position is None
    assert receipt_count(db) == 0
    assert transaction_count(db) == 0


# ---------------------------------------------------------------------------
# Transport 形状与事件词表
# ---------------------------------------------------------------------------

def _row_record():
    return wire(pg_row(100, 10, 1))


def _end_record():
    return wire(pg_end(100, 500, 1))


END_SHAPE_CODES = {"TRANSACTION_COUNT_REQUIRED", "COMMIT_POSITION_REQUIRED"}


@pytest.mark.parametrize("mutate,code", [
    (lambda r: r.__setitem__("schema", {"name": "other.Name"}), "TRANSPORT_METADATA_REQUIRED"),
    (lambda r: r.__setitem__("payload", "text"), "TRANSPORT_METADATA_REQUIRED"),
    (lambda r: r["payload"].__delitem__("offset_json"), "INVALID_TRANSPORT_RECORD"),
    (lambda r: r["payload"].__setitem__("offset_json", '{"lsn": NaN}'), "INVALID_TRANSPORT_JSON"),
    (lambda r: r["payload"].__setitem__("partition_json", "{oops"), "INVALID_TRANSPORT_RECORD"),
    (lambda r: r["payload"].__setitem__("offset_json", "{}"), "SOURCE_OFFSET_REQUIRED"),
    (lambda r: r["payload"].__setitem__("event", [1, 2]), "INVALID_TRANSPORT_RECORD"),
    (lambda r: r["payload"]["event"].__setitem__("op", "r"), "UNEXPECTED_SNAPSHOT"),
    (lambda r: r["payload"]["event"].__setitem__("op", "t"), "SOURCE_TRUNCATED"),
    (lambda r: r["payload"]["event"].__setitem__("op", "q"), "SOURCE_OPERATION_UNSUPPORTED"),
    (lambda r: r["payload"]["event"].__delitem__("transaction"), "TRANSACTION_METADATA_REQUIRED"),
    (lambda r: r["payload"]["event"].__setitem__(
        "transaction", {"id": 5, "total_order": 1}), "TRANSACTION_METADATA_REQUIRED"),
    (lambda r: r["payload"]["event"].__setitem__(
        "transaction", {"id": "100:10"}), "TRANSACTION_ORDER_REQUIRED"),
    (lambda r: r["payload"]["event"].__setitem__(
        "transaction", {"id": "100:10", "total_order": "1"}), "TRANSACTION_ORDER_REQUIRED"),
    (lambda r: r["payload"]["event"].__setitem__(
        "transaction", {"id": "100:10", "total_order": 0}), "TRANSACTION_ORDER_REQUIRED"),
    (lambda r: r["payload"]["event"].__delitem__("event_count"), "TRANSACTION_COUNT_REQUIRED"),
    (lambda r: r["payload"]["event"].__setitem__("event_count", "2"), "TRANSACTION_COUNT_REQUIRED"),
    (lambda r: r["payload"]["event"].__setitem__("event_count", -1), "TRANSACTION_COUNT_REQUIRED"),
    (lambda r: r["payload"]["event"].__setitem__("id", "100"), "COMMIT_POSITION_REQUIRED"),
    (lambda r: r["payload"]["event"].__setitem__("id", "100:0x2A"), "COMMIT_POSITION_REQUIRED"),
])


def test_transport_shape_violations_rejected(db, capture, mutate, code):
    record = _end_record() if code in END_SHAPE_CODES else _row_record()
    mutate(record)
    # 位点形状校验要先通过数量/全序校验，前置一条有效行让事务成立
    batch = [wire(pg_row(100, 10, 1)), record] if code == "COMMIT_POSITION_REQUIRED" else [record]
    reject(db, batch, code)
    db.rollback()
    assert receipt_count(db) == 0
    assert transaction_count(db) == 0


# ---------------------------------------------------------------------------
# MySQL：按完整 GTID 字符串分组 + offset(file,pos[,event]) 位点与顺序
# ---------------------------------------------------------------------------

def test_mysql_gtid_grouping_and_position(db, capture):
    gtid = f"{GTID_A}:23"
    result = append(db, [
        wire(mysql_row(gtid, 1, pos=1200)),
        wire(mysql_row(gtid, 2, pos=1250)),
        wire(mysql_end(gtid, 2, pos=1300)),
    ], dialect="mysql")
    assert result == {"accepted": 3, "duplicates": 0,
                      "received_seq": 3, "committed_seq": 3}
    db.commit()
    # GTID 自带冒号，也必须按完整字符串分到同一事务，不拆前缀
    transaction = transaction_by_key(db, gtid)
    assert transaction.event_count == 2
    assert transaction.commit_seq == 3
    position = transaction.source_position
    assert position["dialect"] == "mysql"
    assert position["file"] == "mysql-bin.000003"
    assert position["pos"] == 1300
    assert position["gtids"] == gtid
    assert fresh_capture(db).committed_position["pos"] == 1300


def test_mysql_end_orders_by_offset_not_cross_uuid_gtid_suffix(db, capture):
    gtid_a = f"{GTID_A}:23"
    gtid_b = f"ca11ab1e-71ca-11e1-9e33-c80aa9429562:5"  # 跨 UUID 且后缀更小
    result = append(db, [wire(mysql_row(gtid_a, 1, pos=1200)),
                         wire(mysql_end(gtid_a, 1, pos=1300))], dialect="mysql")
    assert result["accepted"] == 2
    db.commit()
    # 后缀 5 < 23 不是顺序依据：坐标前进即受理
    result = append(db, [wire(mysql_row(gtid_b, 1, pos=1400)),
                         wire(mysql_end(gtid_b, 1, pos=1500))], dialect="mysql")
    assert result["accepted"] == 2
    db.commit()
    capture = fresh_capture(db)
    assert capture.committed_position["pos"] == 1500
    # END 坐标落后于 frontier（旧新混序）：整批拒绝，不留半个事务
    gtid_c = f"{GTID_A}:24"
    reject(db, [wire(mysql_row(gtid_c, 1, pos=1350)),
                wire(mysql_end(gtid_c, 1, pos=1360))],
           "COMMIT_ORDER_CHANGED", dialect="mysql")
    db.rollback()
    assert transaction_count(db) == 2


def test_mysql_end_event_id_breaks_same_position_ties(db, capture):
    gtid = lambda seq: f"{GTID_A}:{seq}"  # noqa: E731
    append(db, [wire(mysql_row(gtid(1), 1, pos=90)),
                wire(mysql_end(gtid(1), 1, pos=100, event=4))], dialect="mysql")
    db.commit()
    # 同 (file,pos) 且双方都带 event：event 前进才算前进
    result = append(db, [wire(mysql_row(gtid(2), 1, pos=95)),
                         wire(mysql_end(gtid(2), 1, pos=100, event=7))], dialect="mysql")
    assert result["accepted"] == 2
    db.commit()
    assert fresh_capture(db).committed_position["offset"]["event"] == 7
    reject(db, [wire(mysql_row(gtid(3), 1, pos=98)),
                wire(mysql_end(gtid(3), 1, pos=100, event=3))],
           "COMMIT_ORDER_CHANGED", dialect="mysql")
    db.rollback()
    # 对侧缺 event 键时同位点不可证明前进：不受理也不得误判（按混序拒绝）
    reject(db, [wire(mysql_row(gtid(4), 1, pos=99)),
                wire(mysql_end(gtid(4), 1, pos=100))],
           "COMMIT_ORDER_CHANGED", dialect="mysql")
    db.rollback()


def test_mysql_duplicate_end_with_changed_count_rejected(db, capture):
    gtid = f"{GTID_A}:23"
    append(db, [wire(mysql_row(gtid, 1, pos=1200)),
                wire(mysql_end(gtid, 1, pos=1300))], dialect="mysql")
    db.commit()
    reject(db, [wire(mysql_end(gtid, 2, pos=1300))], "REPLAY_RECORD_CHANGED",
           dialect="mysql")
    db.rollback()
    assert transaction_by_key(db, gtid).event_count == 1


def test_mysql_late_row_after_committed_transaction_rejected(db, capture):
    gtid = f"{GTID_A}:23"
    append(db, [wire(mysql_row(gtid, 1, pos=1200)),
                wire(mysql_end(gtid, 1, pos=1300))], dialect="mysql")
    db.commit()
    reject(db, [wire(mysql_row(gtid, 2, pos=1250))], "TRANSACTION_REPLAY_INCOMPLETE",
           dialect="mysql")
    db.rollback()
    assert receipt_count(db) == 1


# ---------------------------------------------------------------------------
# 回收：prune_before / prune_applied
# ---------------------------------------------------------------------------

def test_prune_before_deletes_below_floor_and_redelivery_skips(db, capture):
    first = append(db, committed_pair())
    db.commit()
    result = journal.prune_before(db, CAPTURE_ID, commit_seq=first["committed_seq"])
    assert result["pruned_transactions"] == 1
    assert result["pruned_receipts"] == 1
    assert result["released_bytes"] > 0
    assert result["replay_floor"]["commit_seq"] == 2
    assert result["replay_floor"]["position"]["commit_lsn"] == 500
    db.commit()
    capture = fresh_capture(db)
    assert capture.pending_bytes == 0
    assert capture.committed_position["commit_lsn"] == 500  # position 不被覆盖
    assert capture.committed_position["replay_floor"]["commit_seq"] == 2
    assert receipt_count(db) == 0
    assert transaction_count(db) == 0
    # 回收后的旧重投：行先缓冲（PG 绝不按 rowLSN<floor 丢弃），END 确认
    # commit_lsn<=floor 后整段原子回收，pending_bytes 扣回、无残留事实
    replay = append(db, committed_pair())
    assert replay == {"accepted": 1, "duplicates": 1,
                      "received_seq": 3, "committed_seq": 2}
    db.commit()
    assert receipt_count(db) == 0
    assert transaction_count(db) == 0
    assert fresh_capture(db).pending_bytes == 0
    # wrap 新纪元不被 floor 拒绝；裸 key 回收后可复用
    result2 = append(db, [wire(pg_row(100, 9000, 1)), wire(pg_end(100, 9500, 1))])
    assert result2["accepted"] == 2
    db.commit()
    capture = fresh_capture(db)
    assert capture.committed_position["commit_lsn"] == 9500
    # 后续 END 提交不丢 floor
    assert capture.committed_position["replay_floor"]["commit_seq"] == 2
    assert transaction_by_key(db, "100").commit_seq == 5


def test_long_tx_rows_below_floor_are_buffered_and_kept(db, capture):
    # 已确认的 PG 交错事实：long TX 行 LSN 可早于更早提交事务的 commit LSN。
    # 先提交并回收一个事务，floor.commit_lsn=400
    append(db, [wire(pg_row(200, 300, 1)), wire(pg_end(200, 400, 1))])
    db.commit()
    journal.prune_before(db, CAPTURE_ID, commit_seq=2)
    db.commit()
    assert fresh_capture(db).committed_position["replay_floor"]["position"]["commit_lsn"] == 400
    # long TX：行 LSN 100/200 都 < floor.commit_lsn，但提交晚于 floor
    result = append(db, [wire(pg_row(100, 100, 1)), wire(pg_row(100, 200, 2))])
    assert result["accepted"] == 2  # 绝不按 rowLSN 与全局 floor 过滤丢弃
    db.commit()
    assert receipt_count(db) == 2
    assert fresh_capture(db).pending_bytes > 0
    # END 更大：正常提交，所有行保留、可被 apply
    end = append(db, [wire(pg_end(100, 600, 2))])
    assert end["accepted"] == 1
    db.commit()
    transaction = transaction_by_key(db, "100")
    assert transaction.event_count == 2
    assert transaction.commit_seq is not None
    assert [r.ordinal for r in db.scalars(
        sa.select(IntegratedReplicaReceipt).order_by(IntegratedReplicaReceipt.ordinal))] == [1, 2]
    assert fresh_capture(db).committed_position["commit_lsn"] == 600


def test_stale_redelivery_buffered_then_dropped_atomically_at_end(db, capture):
    append(db, [wire(pg_row(100, 300, 1)), wire(pg_end(100, 400, 1))])
    db.commit()
    journal.prune_before(db, CAPTURE_ID, commit_seq=2)
    db.commit()
    assert fresh_capture(db).pending_bytes == 0
    # 真旧事务整段重投：行先缓冲（暂占 pending_bytes），END 确认 stale 后
    # 原子删临时 receipt/transaction 并扣回 pending_bytes，不产生倒灌
    replay = append(db, [wire(pg_row(100, 300, 1)), wire(pg_end(100, 400, 1))])
    assert replay == {"accepted": 1, "duplicates": 1,
                      "received_seq": 3, "committed_seq": 2}
    db.commit()
    assert receipt_count(db) == 0
    assert transaction_count(db) == 0
    assert fresh_capture(db).pending_bytes == 0
    # 仅 END 的旧重投（无行）也安全跳过
    end_only = append(db, [wire(pg_end(100, 400, 1))])
    assert end_only == {"accepted": 0, "duplicates": 1,
                        "received_seq": 3, "committed_seq": 2}
    db.commit()
    assert transaction_count(db) == 0


def test_stale_end_does_not_drop_newer_buffered_transaction(db, capture):
    append(db, [wire(pg_row(100, 300, 1)), wire(pg_end(100, 400, 1))])
    db.commit()
    journal.prune_before(db, CAPTURE_ID, commit_seq=2)
    db.commit()
    # 回收后同 xid 新纪元行（>floor）先缓冲（复用被释放的裸 key）
    append(db, [wire(pg_row(100, 9000, 1))])
    db.commit()
    # 旧纪元 stale END：commit_lsn<=floor 且小于缓冲行 LSN → 不属于该缓冲
    # 事务，只按 stale 跳过，绝不删新事务的行
    stale = append(db, [wire(pg_end(100, 400, 1))])
    assert stale == {"accepted": 0, "duplicates": 1,
                     "received_seq": 3, "committed_seq": 2}
    db.commit()
    assert transaction_by_key(db, "100").event_count == 1
    assert receipt_count(db) == 1
    assert fresh_capture(db).pending_bytes > 0
    # 真实 END 提交，行完整保留
    result = append(db, [wire(pg_end(100, 9500, 1))])
    assert result["accepted"] == 1
    db.commit()
    transaction = transaction_by_key(db, "100")
    assert transaction.event_count == 1
    assert transaction.commit_seq is not None


def test_stale_epoch_row_never_attaches_to_new_epoch_after_prune(db, capture):
    # 旧纪元已提交并回收；新纪元同 xid 行已开始缓冲后，重启重放又送来旧纪元
    # 的行（回执已被回收）。旧行 LSN 证明它不属于新纪元：只能按 stale 跳过，
    # 绝不能挂到新事务上污染 event_count/ordinal。
    append(db, [wire(pg_row(100, 300, 1)), wire(pg_end(100, 400, 1))])
    db.commit()
    journal.prune_before(db, CAPTURE_ID, commit_seq=2)
    db.commit()
    assert receipt_count(db) == 0
    append(db, [wire(pg_row(100, 9000, 1))])
    db.commit()
    stale_row = append(db, [wire(pg_row(100, 300, 1))])
    assert stale_row == {"accepted": 0, "duplicates": 1,
                         "received_seq": 3, "committed_seq": 2}
    db.commit()
    transaction = transaction_by_key(db, "100")
    assert transaction.event_count == 1  # 只含新纪元行
    assert receipt_count(db) == 1
    # 旧 END 仍按 floor 跳过，新 END 的 count 1 与 receipt 全序一致
    assert append(db, [wire(pg_end(100, 400, 1))])["accepted"] == 0
    db.commit()
    result = append(db, [wire(pg_end(100, 9500, 1))])
    assert result["accepted"] == 1
    db.commit()
    transaction = transaction_by_key(db, "100")
    assert transaction.event_count == 1
    assert transaction.commit_seq == 4
    assert fresh_capture(db).committed_position["commit_lsn"] == 9500
    assert receipt_count(db) == 1


def test_duplicate_row_replay_keeps_newer_source_offset(db, capture):
    append(db, committed_pair())
    db.commit()
    assert fresh_capture(db).source_offset == {"lsn": 500, "txId": 100}
    # 重启 offset 内只重放旧行（无 END）：回执命中为重复，source_offset
    # 绝不能被旧行位点回退覆盖；活跃性仍由 heartbeat_at 证明。
    replay = append(db, [committed_pair()[0]])
    assert replay == {"accepted": 0, "duplicates": 1,
                      "received_seq": 2, "committed_seq": 2}
    db.commit()
    assert fresh_capture(db).source_offset == {"lsn": 500, "txId": 100}
    assert receipt_count(db) == 1


def test_ddl_and_schema_change_records_blocked_explicitly(db, capture):
    # MySQL DDL 记录不带 op/status：绝不能当无害 no-op 放行。
    ddl = wire(transport({"databaseName": "oodb",
                          "ddl": "ALTER TABLE orders ADD COLUMN x int",
                          "tableChanges": []}, offset={"lsn": 7}))
    reject(db, [ddl], "SOURCE_SCHEMA_CHANGED")
    db.rollback()
    # 事件 schema 名以 .SchemaChange 结尾同样显式拒绝。
    named = wire(transport({"ts_ms": TS_MS}, offset={"lsn": 8}))
    named["schema"]["fields"][-1]["name"] = "oodb.SchemaChange"
    reject(db, [named], "SOURCE_SCHEMA_CHANGED")
    db.rollback()
    assert receipt_count(db) == 0
    assert fresh_capture(db).received_seq == 0


def test_prune_waits_for_lagging_runs_but_floor_persists(db, capture, admin_user):
    append(db, committed_pair())  # commit_seq 2
    db.commit()
    run = make_run(db, admin_user, CAPTURE_ID, status="active", applied_seq=1)
    db.commit()
    result = journal.prune_before(db, CAPTURE_ID, commit_seq=2)
    assert result["pruned_transactions"] == 0
    assert result["released_bytes"] == 0
    # floor 仍上升：offset 证实不重投；滞后的只是回执保留
    assert result["replay_floor"]["commit_seq"] == 2
    db.commit()
    capture = fresh_capture(db)
    assert capture.pending_bytes > 0
    assert receipt_count(db) == 1
    assert capture.committed_position["replay_floor"]["commit_seq"] == 2
    # run 追上后同一 floor 重放即可回收（floor 不下降、不早退）
    run.applied_position = {"capture_id": CAPTURE_ID, "commit_seq": 2,
                            "source_position": {}}
    db.commit()
    again = journal.prune_before(db, CAPTURE_ID, commit_seq=2)
    assert again["pruned_transactions"] == 1
    assert again["released_bytes"] > 0
    db.commit()
    assert receipt_count(db) == 0
    assert fresh_capture(db).pending_bytes == 0


def test_prune_ignores_retired_failed_cancelled_runs(db, capture, admin_user):
    append(db, committed_pair())
    db.commit()
    make_run(db, admin_user, CAPTURE_ID, status="retired", applied_seq=0)
    db.commit()
    result = journal.prune_before(db, CAPTURE_ID, commit_seq=2)
    assert result["pruned_transactions"] == 1
    db.commit()
    assert receipt_count(db) == 0


def test_prune_rejects_invalid_floor_and_closed_capture(db, capture):
    for bad in (0, -1, "2", True, 2.0, None):
        with pytest.raises(ReplicaJournalError) as exc:
            journal.prune_before(db, CAPTURE_ID, commit_seq=bad)
        assert exc.value.code == "PRUNE_FLOOR_INVALID"
    db.rollback()
    capture.status = "blocked"
    db.commit()
    with pytest.raises(ReplicaJournalError) as exc:
        journal.prune_before(db, CAPTURE_ID, commit_seq=5)
    assert exc.value.code == "CAPTURE_NOT_ACCEPTING"
    db.rollback()


def test_prune_applied_derives_floor_from_confirmed_pg_offset(db, capture):
    append(db, [wire(pg_row(100, 10, 1)), wire(pg_end(100, 500, 1))])   # commit_seq 2
    append(db, [wire(pg_row(200, 20, 1)), wire(pg_end(200, 700, 1))])   # commit_seq 4
    db.commit()
    result = journal.prune_applied(
        db, CAPTURE_ID, confirmed_offset={"lsn": 600, "lsn_commit": 600})
    assert result["pruned_transactions"] == 1  # 只回收 END 严格早于 600 的事务
    assert result["replay_floor"]["position"]["commit_lsn"] == 500
    db.commit()
    assert transaction_by_key(db, "200") is not None
    assert receipt_count(db) == 1
    # 边界相等：END 恰在证实位点 → 整事务保留，绝不回收
    held = journal.prune_applied(
        db, CAPTURE_ID, confirmed_offset={"lsn": 500, "lsn_commit": 500})
    assert held["pruned_transactions"] == 0
    db.rollback()
    # 停在事务内部（lsn 是行 LSN）→ 该事务保留
    inside = journal.prune_applied(
        db, CAPTURE_ID, confirmed_offset={"lsn": 650, "lsn_proc": 650, "lsn_commit": 650})
    assert inside["pruned_transactions"] == 0
    db.rollback()
    assert receipt_count(db) == 1


def test_prune_applied_requires_pg_commit_process_proof(db, capture):
    append(db, committed_pair())
    db.commit()
    # 裸 lsn 可能是事务内行 LSN：缺 lsn_proc/lsn_commit 证据一律不回收
    result = journal.prune_applied(db, CAPTURE_ID, confirmed_offset={"lsn": 900})
    assert result["pruned_transactions"] == 0
    assert result["replay_floor"] is None
    db.commit()
    assert receipt_count(db) == 1
    # marker 取最小值：lsn 落后于 lsn_commit 时按更保守者算
    conservative = journal.prune_applied(
        db, CAPTURE_ID, confirmed_offset={"lsn": 100, "lsn_commit": 900})
    assert conservative["pruned_transactions"] == 0
    db.rollback()
    # 坏形状
    with pytest.raises(ReplicaJournalError) as exc:
        journal.prune_applied(db, CAPTURE_ID, confirmed_offset={"file": "x"})
    assert exc.value.code == "PRUNE_OFFSET_INVALID"
    db.rollback()


def test_prune_applied_mysql_strict_boundary_and_stale_redelivery(db, capture):
    gtid = f"{GTID_A}:23"
    append(db, [wire(mysql_row(gtid, 1, pos=1200)),
                wire(mysql_end(gtid, 1, pos=1300))], dialect="mysql")
    db.commit()
    # 证实位点停在事务内部（行 pos 1200 与 END pos 1300 之间）：保留整事务
    inside = journal.prune_applied(
        db, CAPTURE_ID, confirmed_offset={"file": "mysql-bin.000003", "pos": 1250})
    assert inside["pruned_transactions"] == 0
    db.rollback()
    # 边界相等：同样保留
    equal = journal.prune_applied(
        db, CAPTURE_ID, confirmed_offset={"file": "mysql-bin.000003", "pos": 1300})
    assert equal["pruned_transactions"] == 0
    db.rollback()
    # 严格越过 END 位点才回收
    result = journal.prune_applied(
        db, CAPTURE_ID, confirmed_offset={"file": "mysql-bin.000003", "pos": 1301})
    assert result["pruned_transactions"] == 1
    assert result["replay_floor"]["position"]["pos"] == 1300
    db.commit()
    assert transaction_count(db) == 0
    # 回收后的旧重投安全跳过；floor 之后的新事务正常受理
    replay = append(db, [wire(mysql_row(gtid, 1, pos=1200)),
                         wire(mysql_end(gtid, 1, pos=1300))], dialect="mysql")
    assert replay == {"accepted": 0, "duplicates": 2,
                      "received_seq": 2, "committed_seq": 2}
    db.commit()
    gtid2 = f"{GTID_A}:24"
    fresh = append(db, [wire(mysql_row(gtid2, 1, pos=1400)),
                        wire(mysql_end(gtid2, 1, pos=1500))], dialect="mysql")
    assert fresh["accepted"] == 2
    db.commit()
    assert fresh_capture(db).committed_position["pos"] == 1500


def test_prune_applied_mysql_event_tie_on_equal_position(db, capture):
    gtid = f"{GTID_A}:31"
    append(db, [wire(mysql_row(gtid, 1, pos=1200)),
                wire(mysql_end(gtid, 1, pos=1300, event=9))], dialect="mysql")
    db.commit()
    # 同 (file,pos)、双方带 event：confirmed event 更大 → 严格在前，可回收
    result = journal.prune_applied(
        db, CAPTURE_ID,
        confirmed_offset={"file": "mysql-bin.000003", "pos": 1300, "event": 10})
    assert result["pruned_transactions"] == 1
    db.commit()
    assert transaction_count(db) == 0
