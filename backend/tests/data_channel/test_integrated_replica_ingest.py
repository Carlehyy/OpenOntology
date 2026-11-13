"""replica_ingest 聚焦测试（真实 SQLite cohort：queue→prepare→snapshot→stream）。

契约被测面：snapshot r 行按 (schema,table) 路由到 loading run 批量 COPY，
batch 回执（内容 SHA256）+ row_count 与 COPY 同事务（失败整批回滚，无回执
无行数）；相同 HTTP batch 原样重投幂等（accepted 0 / duplicates N，绝不双写）；
heartbeat 是活跃性不是覆盖/乱序证据——纯心跳批推进 source_offset/heartbeat，
不创建/确认 coverage cut，也不发布副本；受管 PG op=m 逻辑消息只在 transport
wrapper 的 MessageValue/base64 token/offset 形状完整时更新 pending cut，随后由
runtime 在全部成员应用后消费；r 在首条 change 之后到达才拒绝
（SNAPSHOT_ORDER_CHANGED）；围栏：租约过期/
资产 schema 漂移 → REPLICA_TARGET_CHANGED，capture gap/blocked →
CAPTURE_NOT_ACCEPTING，源身份漂移 → SOURCE_IDENTITY_CHANGED，未登记表 →
SOURCE_TABLE_CHANGED，快照完成后 r → UNEXPECTED_SNAPSHOT；retired 成员按序
跳过不阻塞同 cohort 其它表；mark_capture_error 只标记仍依赖本 capture 的资产
（独立 active 副本绝不被牵连）。PostgreSQL COPY 真实语义归主会话 gate。
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset
from app.data_channel.integrated import replica_control as control
from app.data_channel.integrated import replica_coverage as coverage
from app.data_channel.integrated import replica_ingest as ingest
from app.data_channel.integrated import replica_runtime as runtime
from app.data_channel.integrated import replica_store as store
from app.data_channel.integrated.contracts import digest
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_capture_models import (
    IntegratedReplicaCapture, IntegratedReplicaTransaction,
)
from app.data_channel.integrated.replica_models import (
    IntegratedReplica, IntegratedReplicaRun,
)
from app.data_channel.integrated.replica_snapshot_models import (
    IntegratedReplicaSnapshotBatch,
)

TS_MS = 1760000000000
PRECISION_JSON = '{"pi": 3.14159265358979323846264338327950288}'
BLOB = b"\x00\xde\xad\xbe\xef"
BLOB_B64 = base64.b64encode(BLOB).decode("ascii")
MOMENT = "2026-11-12T09:30:00.123456+08:00"


# ---------------------------------------------------------------------------
# manifest / envelope 构造（真实 schema digest 资产）
# ---------------------------------------------------------------------------

def orders_columns():
    return [
        {"name": "id", "type": "integer", "nullable": False, "primary_key": True},
        {"name": "label", "type": "string", "nullable": True},
        {"name": "amount", "type": "decimal", "nullable": True,
         "precision": 12, "scale": 4},
        {"name": "doc", "type": "json", "nullable": True, "encoding": "json_text"},
        {"name": "blob", "type": "binary", "nullable": True},
        {"name": "moment", "type": "timestamp", "nullable": True, "timezone": True},
    ]


def customers_columns():
    return [
        {"name": "id", "type": "integer", "nullable": False, "primary_key": True},
        {"name": "note", "type": "string", "nullable": True},
    ]


def orders_fields():
    return [
        {"field": "id", "type": "int64", "optional": False},
        {"field": "label", "type": "string", "optional": True},
        {"field": "amount", "name": "io.debezium.data.Decimal", "type": "string",
         "parameters": {"scale": "4"}, "optional": True},
        {"field": "doc", "name": "io.debezium.data.Json", "type": "string",
         "optional": True},
        {"field": "blob", "type": "bytes", "optional": True},
        {"field": "moment", "name": "io.debezium.time.ZonedTimestamp",
         "type": "string", "optional": True},
    ]


def customers_fields():
    return [
        {"field": "id", "type": "int64", "optional": False},
        {"field": "note", "type": "string", "optional": True},
    ]


FIELDS = {"orders": orders_fields, "customers": customers_fields}
COLUMNS = {"orders": orders_columns, "customers": customers_columns}


def transport_schema(fields):
    return {"type": "struct", "name": "io.openontology.replica.TransportRecord",
            "fields": [
                {"field": "topic", "type": "string"},
                {"field": "partition_json", "type": "string"},
                {"field": "offset_json", "type": "string"},
                {"field": "key", "type": "string", "optional": True},
                {"field": "event", "name": "oodb.public.orders.Envelope",
                 "type": "struct", "optional": True, "fields": [
                     {"field": "op", "type": "string"},
                     {"field": "ts_ms", "type": "int64"},
                     {"field": "source", "type": "struct"},
                     {"field": "transaction", "type": "struct", "optional": True},
                     {"field": "before", "type": "struct", "optional": True,
                      "fields": fields},
                     {"field": "after", "type": "struct", "optional": True,
                      "fields": fields},
                 ]}]}


def transport(event, *, server, offset, table="orders"):
    payload = {"topic": f"oo.public.{table}", "key": None,
               "partition_json": json.dumps({"server": server}),
               "offset_json": json.dumps(offset)}
    if event is not None:
        payload["event"] = event
    return {"schema": transport_schema(FIELDS[table]()), "payload": payload}


def snap_row(id_, label="x"):
    return {"id": id_, "label": label, "amount": "123.4500",
            "doc": PRECISION_JSON, "blob": BLOB_B64, "moment": MOMENT}


def snap_record(world, row, *, table="orders", offset=None):
    event = {"op": "r", "ts_ms": TS_MS,
             "source": {"connector": "postgresql", "db": "oodb",
                        "schema": world.schema_name, "table": table,
                        "ts_ms": TS_MS, "snapshot": "true"},
             "after": row}
    return transport(event, server=world.server,
                     offset=offset or {"lsn": 100 + row["id"]}, table=table)


def heartbeat(world, offset):
    return transport(None, server=world.server, offset=offset)


def cut_schema():
    """Transport wrapper whose event is a real PG logical MessageValue."""
    return {
        "type": "struct",
        "name": "io.openontology.replica.TransportRecord",
        "fields": [
            {"field": "topic", "type": "string"},
            {"field": "partition_json", "type": "string"},
            {"field": "offset_json", "type": "string"},
            {"field": "key", "type": "string", "optional": True},
            {"field": "event",
             "name": "io.debezium.connector.postgresql.MessageValue",
             "type": "struct", "optional": True,
             "fields": [
                 {"field": "op", "type": "string"},
                 {"field": "source", "type": "struct"},
                 {"field": "message", "type": "struct", "optional": True,
                  "fields": [
                      {"field": "prefix", "type": "string"},
                      {"field": "content", "type": "bytes", "optional": True},
                  ]},
             ]},
        ],
    }


def cut_record(capture, server, token, *, lsn=900):
    """A non-transactional PG logical message carrying the durable cut token."""
    event = {
        "op": "m",
        "ts_ms": TS_MS,
        "source": {"connector": "postgresql", "db": "oodb", "schema": "public",
                   "table": "", "lsn": lsn, "ts_ms": TS_MS},
        "message": {"prefix": coverage.prefix(capture.id),
                    "content": base64.b64encode(token.encode("ascii")).decode("ascii")},
    }
    return {
        "schema": cut_schema(),
        "payload": {
            "topic": server + ".message",
            "key": None,
            "partition_json": json.dumps({"server": server}),
            "offset_json": json.dumps({
                "lsn": lsn, "lsn_proc": lsn, "lsn_commit": lsn,
                "messageType": "MESSAGE",
            }),
            "event": event,
        },
    }


def prepare_cut(db, capture):
    pending = coverage.prepare_probe(db, capture.id)
    assert pending is not None, "coverage probe requires a completed, non-loading cohort"
    db.commit()
    return pending


def receive_cut(db, capture, server, *, lsn=900, message_first=False):
    pending = prepare_cut(db, capture)
    record = cut_record(capture, server, pending["id"], lsn=lsn)
    frontier = {"dialect": "postgresql", "lsn": lsn}
    if message_first:
        ingest.ingest_batch(db, capture.id, [record])
        db.commit()
        coverage.record_probe(db, capture.id, pending["id"], frontier)
        db.commit()
    else:
        coverage.record_probe(db, capture.id, pending["id"], frontier)
        db.commit()
        ingest.ingest_batch(db, capture.id, [record])
        db.commit()
    return record


def change_record(world, *, table, xid, lsn, ordinal, op, after=None, before=None):
    event = {"op": op, "ts_ms": TS_MS,
             "source": {"connector": "postgresql", "db": "oodb",
                        "schema": world.schema_name, "table": table,
                        "lsn": lsn, "txId": int(xid), "ts_ms": TS_MS},
             "transaction": {"id": f"{xid}:{lsn}", "total_order": ordinal,
                             "data_collection_order": ordinal}}
    if before is not None:
        event["before"] = before
    if after is not None:
        event["after"] = after
    return transport(event, server=world.server,
                     offset={"lsn": lsn, "txId": int(xid)}, table=table)


def end_record(world, *, xid, commit_lsn, count):
    event = {"status": "END", "id": f"{xid}:{commit_lsn}", "event_count": count,
             "data_collections": [{"event_count": count,
                                   "data_collection": "public.orders"}],
             "ts_ms": TS_MS}
    return transport(event, server=world.server,
                     offset={"lsn": commit_lsn, "txId": int(xid)})


# ---------------------------------------------------------------------------
# cohort 构造：真实 digest 资产 → queue_registered → prepare_capture
# ---------------------------------------------------------------------------

def make_cohort(db, admin_user, *, tables=("orders",), connection_id=None):
    suffix = uuid.uuid4().hex[:8]
    connection = Connection(
        id=connection_id or f"ing-src-{suffix}", name=f"ingest {suffix}",
        kind="postgres", config={}, status="active", created_by=admin_user.id)
    db.add(connection)
    for table in tables:
        manifest = {"columns": COLUMNS[table](), "primary_key": ["id"]}
        dataset = Dataset(id=f"ing-ds-{suffix}-{table}", name=f"ingest {table}",
                          kind="integrated", source_connection_id=connection.id,
                          source_resource=table)
        db.add(dataset)
        db.add(IntegratedAsset(
            id=f"ing-asset-{suffix}-{table}", dataset_id=dataset.id,
            connection_id=connection.id, name=f"ingest {table}", schema_name="public",
            table_name=table, schema_json=manifest, schema_fingerprint=digest(manifest),
            writable=False, created_by=admin_user.id))
    db.commit()
    runtime.queue_registered(db)
    capture = runtime.prepare_capture(db, connection.id)
    db.commit()
    members = []
    for member in capture.tables_json:
        asset = db.get(IntegratedAsset, member["asset_id"])
        run = db.get(IntegratedReplicaRun, member["run_id"])
        members.append(SimpleNamespace(
            asset=asset, run=run, replica=db.get(IntegratedReplica, asset.id),
            schema=store.build_replica_schema(run.id, run.schema_json,
                                              schema_fingerprint=run.schema_fingerprint)))
    return SimpleNamespace(connection=connection, capture=capture, members=members,
                           schema_name="public",
                           server="oo_replica_" + uuid.UUID(capture.id).hex)


@pytest.fixture
def world(db, admin_user):
    built = make_cohort(db, admin_user)
    try:
        yield built
    finally:
        db.rollback()
        for member in built.members:
            store.drop_owned(db.connection(), member.run.id)
        db.commit()


def member_of(world_, table):
    for member in world_.members:
        if member.asset.table_name == table:
            return member
    raise AssertionError(f"no member for table {table}")


def physical_rows(db, member):
    return [row for batch in store.read_batches(db.connection(), member.schema)
            for row in batch]


def receipts(db, run_id):
    return db.scalars(sa.select(IntegratedReplicaSnapshotBatch).where(
        IntegratedReplicaSnapshotBatch.run_id == run_id)).all()


def rejects(db, code, fn):
    with pytest.raises(ingest.ReplicaIngestError) as exc:
        fn()
    assert exc.value.code == code
    return exc.value


# ---------------------------------------------------------------------------
# snapshot 批量 COPY：行、回执、row_count、原子性、幂等重投
# ---------------------------------------------------------------------------

def test_snapshot_batches_copy_rows_with_receipt_and_row_count(db, world):
    member = world.members[0]
    result = ingest.ingest_batch(db, world.capture.id, [
        snap_record(world, snap_row(1)), snap_record(world, snap_row(2, label="two"))])
    db.commit()
    assert result == {"status": "accepted", "accepted": 2, "duplicates": 0}
    result = ingest.ingest_batch(db, world.capture.id, [snap_record(world, snap_row(3))])
    db.commit()
    assert result["accepted"] == 1
    assert member.run.row_count == 3
    assert len(receipts(db, member.run.id)) == 2
    rows = {row["id"]: row for row in physical_rows(db, member)}
    assert set(rows) == {1, 2, 3}
    assert str(rows[1]["amount"]) == "123.4500"
    assert str(rows[1]["doc"]) == PRECISION_JSON
    assert rows[1]["blob"] == BLOB
    assert world.capture.source_offset == {"lsn": 103}


def test_identical_http_batch_redelivery_is_idempotent(db, world):
    member = world.members[0]
    batch = [snap_record(world, snap_row(1)), snap_record(world, snap_row(2))]
    first = ingest.ingest_batch(db, world.capture.id, batch)
    db.commit()
    replay = ingest.ingest_batch(db, world.capture.id, batch)
    db.commit()
    assert first["accepted"] == 2 and first["duplicates"] == 0
    assert replay == {"status": "accepted", "accepted": 0, "duplicates": 2}
    assert member.run.row_count == 2
    assert len(receipts(db, member.run.id)) == 1
    assert len(physical_rows(db, member)) == 2


def test_failed_snapshot_batch_rolls_back_rows_receipt_and_row_count(db, world):
    member = world.members[0]
    good = snap_record(world, snap_row(1))
    broken = snap_record(world, {**snap_row(2), "doc": "{oops"})
    rejects(db, "REPLICA_JSON_INVALID",
            lambda: ingest.ingest_batch(db, world.capture.id, [good, broken]))
    db.rollback()  # 接收端语义：整批回滚，无部分事实
    assert receipts(db, member.run.id) == []
    assert physical_rows(db, member) == []
    fresh = db.scalar(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.id == member.run.id)
        .execution_options(populate_existing=True))
    assert fresh.row_count == 0
    assert fresh.snapshot_proof is None
    assert db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == world.capture.id)
        .execution_options(populate_existing=True)).source_offset is None
    # 同一批修正后可正常重投
    fixed = snap_record(world, snap_row(2))
    assert ingest.ingest_batch(db, world.capture.id, [good, fixed])["accepted"] == 2
    db.commit()


def test_streaming_records_share_the_http_batch_with_snapshot_rows(db, world):
    member = world.members[0]
    result = ingest.ingest_batch(db, world.capture.id, [
        snap_record(world, snap_row(1)),
        change_record(world, table="orders", xid=100, lsn=10, ordinal=1, op="u",
                      after=snap_row(1, label="streamed")),
        end_record(world, xid=100, commit_lsn=500, count=1)])
    db.commit()
    # 快照行 + journal 行 + END 全部受理；change 在 r 之后才合法
    assert result["accepted"] == 3 and result["duplicates"] == 0
    assert member.run.row_count == 1
    txn = db.scalar(sa.select(IntegratedReplicaTransaction).where(
        IntegratedReplicaTransaction.capture_id == world.capture.id,
        IntegratedReplicaTransaction.source_transaction_id == "100"))
    assert txn is not None and txn.commit_seq == 2  # 快照行不占 journal 序号


# ---------------------------------------------------------------------------
# heartbeat：活跃性而非乱序证据
# ---------------------------------------------------------------------------

def test_heartbeats_keep_the_snapshot_window_open_then_changes_close_it(db, world):
    first = ingest.ingest_batch(db, world.capture.id, [
        heartbeat(world, {"lsn": 5}), heartbeat(world, {"lsn": 6}),
        snap_record(world, snap_row(1), offset={"lsn": 7})])
    db.commit()
    assert first["accepted"] == 1  # 心跳不占计数
    ingest.ingest_batch(db, world.capture.id, [heartbeat(world, {"lsn": 8})])
    db.commit()
    # 同批内首条 change 之后：旧快照行绝不允许覆盖已流式化的变更
    rejects(db, "SNAPSHOT_ORDER_CHANGED", lambda: ingest.ingest_batch(
        db, world.capture.id, [
            change_record(world, table="orders", xid=100, lsn=10, ordinal=1, op="c",
                          after=snap_row(9)),
            snap_record(world, snap_row(2)),
            end_record(world, xid=100, commit_lsn=500, count=1)]))
    db.rollback()
    assert len(physical_rows(db, world.members[0])) == 1


def test_heartbeat_only_batch_advances_liveness_not_coverage(db, world):
    member = world.members[0]
    before = member.replica.coverage_confirmed_at
    result = ingest.ingest_batch(db, world.capture.id, [heartbeat(world, {"lsn": 42})])
    db.commit()
    assert result == {"status": "accepted", "accepted": 0, "duplicates": 0}
    capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == world.capture.id)
        .execution_options(populate_existing=True))
    assert capture.source_offset == {"lsn": 42}
    assert capture.heartbeat_at is not None
    replica = db.get(IntegratedReplica, member.asset.id)
    assert replica.heartbeat_at == capture.heartbeat_at
    assert replica.coverage_confirmed_at == before  # 心跳不是覆盖证据


# ---------------------------------------------------------------------------
# 围栏：asset/schema/lease/capture/identity/table
# ---------------------------------------------------------------------------

def test_expired_lease_and_schema_drift_lose_the_target(db, world):
    member = world.members[0]
    member.run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.flush()
    rejects(db, "REPLICA_TARGET_CHANGED", lambda: ingest.ingest_batch(
        db, world.capture.id, [snap_record(world, snap_row(1))]))
    db.rollback()
    member.run.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=90)
    member.asset.schema_fingerprint = "drifted"
    db.flush()
    rejects(db, "REPLICA_TARGET_CHANGED", lambda: ingest.ingest_batch(
        db, world.capture.id, [snap_record(world, snap_row(1))]))
    db.rollback()


def test_capture_status_identity_and_table_gates(db, world):
    records = [snap_record(world, snap_row(1))]
    world.capture.status = "gap"
    db.flush()
    rejects(db, "CAPTURE_NOT_ACCEPTING",
            lambda: ingest.ingest_batch(db, world.capture.id, records))
    db.rollback()
    world.connection.config = {"host": "relocated.example"}
    db.flush()
    rejects(db, "SOURCE_IDENTITY_CHANGED",
            lambda: ingest.ingest_batch(db, world.capture.id, records))
    db.rollback()
    world.connection.config = {}
    db.flush()
    rejects(db, "SOURCE_TABLE_CHANGED", lambda: ingest.ingest_batch(
        db, world.capture.id, [snap_record(world, {"id": 1}, table="customers")]))
    db.rollback()
    wrong_server = transport(None, server="oo_replica_" + "0" * 32,
                             offset={"lsn": 1})
    rejects(db, "SOURCE_PARTITION_CHANGED",
            lambda: ingest.ingest_batch(db, world.capture.id, [wrong_server]))
    db.rollback()
    rejects(db, "INVALID_TRANSPORT_BATCH",
            lambda: ingest.ingest_batch(db, world.capture.id, []))


def test_snapshot_read_after_completion_is_unexpected(db, world):
    member = world.members[0]
    ingest.ingest_batch(db, world.capture.id, [snap_record(world, snap_row(1))])
    control.finish_load(db, member.run.id, member.run.owner_token, completion={
        "complete": True, "row_count": 1, "expected_rows": 1,
        "schema_fingerprint": member.run.schema_fingerprint,
        "snapshot_id": world.capture.id})
    db.commit()
    rejects(db, "UNEXPECTED_SNAPSHOT", lambda: ingest.ingest_batch(
        db, world.capture.id, [snap_record(world, snap_row(2))]))
    db.rollback()


def test_retired_member_is_skipped_without_blocking_the_cohort(db, db_admin_cohort):
    world = db_admin_cohort
    retired = member_of(world, "orders")
    retired.run.status = "retired"
    db.flush()
    result = ingest.ingest_batch(db, world.capture.id, [
        snap_record(world, {"id": 7, "note": "ok"}, table="customers")])
    db.commit()
    assert result["accepted"] == 1
    assert member_of(world, "customers").run.row_count == 1
    # The child's frozen include set still contains this table; skip its late
    # snapshot rows instead of rejecting the batch and blocking active peers.
    result = ingest.ingest_batch(db, world.capture.id, [snap_record(world, snap_row(1))])
    db.commit()
    assert result == {"status": "accepted", "accepted": 0, "duplicates": 0}
    assert retired.run.row_count == 0
    assert physical_rows(db, retired) == []


@pytest.fixture
def db_admin_cohort(db, admin_user):
    built = make_cohort(db, admin_user, tables=("orders", "customers"))
    try:
        yield built
    finally:
        db.rollback()
        for member in built.members:
            store.drop_owned(db.connection(), member.run.id)
        db.commit()


# ---------------------------------------------------------------------------
# initial snapshot 通知链路（STARTED → RESOLVED → r×N → SCAN → COMPLETED）
# ---------------------------------------------------------------------------

NOTIFICATION = "io.debezium.connector.common.Notification"


def notify(world, *, kind, event_id, offset=None, data=None, timestamp=None):
    event = {"id": str(event_id), "type": kind, "aggregate_type": "Initial Snapshot"}
    if timestamp is not None:
        event["timestamp"] = timestamp
    if data is not None:
        event["additional_data"] = data
    schema = {"type": "struct", "name": "io.openontology.replica.TransportRecord",
              "fields": [
                  {"field": "topic", "type": "string"},
                  {"field": "partition_json", "type": "string"},
                  {"field": "offset_json", "type": "string"},
                  {"field": "key", "type": "string", "optional": True},
                  {"field": "event", "name": NOTIFICATION, "type": "struct",
                   "optional": True, "fields": []}]}
    payload = {"topic": "t", "key": None,
               "partition_json": json.dumps({"server": world.server}),
               "offset_json": json.dumps({} if offset is None else offset),
               "event": event}
    return {"schema": schema, "payload": payload}


def notification_batch(world, rows, *, scan_count=None, started_ts=TS_MS,
                       collections=("public.orders",), table="orders",
                       completed_offset=None, snap_ids=None):
    ids = snap_ids or {kind: str(uuid.uuid4()) for kind in
                       ("start", "resolve", "scan", "complete")}
    records = [
        notify(world, kind="STARTED", event_id=ids["start"], timestamp=started_ts),
        notify(world, kind="DATA_COLLECTIONS_RESOLVED", event_id=ids["resolve"],
               data={"data_collections": ",".join(sorted(collections))})]
    records.extend(rows)
    records.append(notify(world, kind="TABLE_SCAN_COMPLETED", event_id=ids["scan"],
                          data={"scanned_collection": f"public.{table}",
                                "total_rows_scanned": str(len(rows) if scan_count is None else scan_count),
                                "status": "SUCCEEDED"}))
    records.append(notify(world, kind="COMPLETED", event_id=ids["complete"],
                          offset=completed_offset or {"lsn": 777}))
    return records, ids


def test_notification_batch_completes_snapshot_with_started_lower_bound(db, world):
    member = world.members[0]
    started_ts = 1760000103000
    records, ids = notification_batch(
        world, [snap_record(world, snap_row(1)), snap_record(world, snap_row(2))],
        started_ts=started_ts)
    result = ingest.ingest_batch(db, world.capture.id, records)
    db.commit()
    assert result == {"status": "accepted", "accepted": 2, "duplicates": 0}
    assert member.run.status == "catching_up"
    assert member.run.row_count == 2
    assert len(physical_rows(db, member)) == 2
    proof = member.run.snapshot_proof
    assert proof["complete"] is True
    assert proof["expected_rows"] == 2
    assert proof["started_id"] == ids["start"]
    # 覆盖确认取 STARTED 时间戳的保守下界，绝不宣称采集结束的当前时刻
    assert proof["source_confirmed_at"] == datetime.fromtimestamp(
        started_ts / 1000, timezone.utc).isoformat()


def test_zero_row_table_completes_via_notifications(db, world):
    member = world.members[0]
    records, _ = notification_batch(world, [], scan_count=0)
    result = ingest.ingest_batch(db, world.capture.id, records)
    db.commit()
    assert result["accepted"] == 0
    assert member.run.status == "catching_up"
    assert member.run.row_count == 0
    assert member.run.snapshot_proof["complete"] is True
    assert physical_rows(db, member) == []


def test_table_scan_completed_may_precede_the_tables_final_row(db, world):
    member = world.members[0]
    ids = {kind: str(uuid.uuid4()) for kind in ("start", "resolve", "scan", "complete")}
    # 同批内 SCAN 排在最后一条 r 之前：r 先整体 COPY，通知按原序落证明
    records = [
        notify(world, kind="STARTED", event_id=ids["start"], timestamp=TS_MS),
        notify(world, kind="DATA_COLLECTIONS_RESOLVED", event_id=ids["resolve"],
               data={"data_collections": "public.orders"}),
        snap_record(world, snap_row(1)),
        notify(world, kind="TABLE_SCAN_COMPLETED", event_id=ids["scan"],
               data={"scanned_collection": "public.orders",
                     "total_rows_scanned": "2", "status": "SUCCEEDED"}),
        snap_record(world, snap_row(2)),
        notify(world, kind="COMPLETED", event_id=ids["complete"],
               offset={"lsn": 778})]
    result = ingest.ingest_batch(db, world.capture.id, records)
    db.commit()
    assert result["accepted"] == 2
    assert member.run.status == "catching_up"
    assert len(physical_rows(db, member)) == 2


def test_completed_count_mismatch_rolls_back_whole_batch(db, world):
    member = world.members[0]
    records, _ = notification_batch(
        world, [snap_record(world, snap_row(1))], scan_count=5)
    rejects(db, "SNAPSHOT_PROOF_INCOMPLETE",
            lambda: ingest.ingest_batch(db, world.capture.id, records))
    db.rollback()
    assert member.run.status == "loading"
    assert member.run.snapshot_proof is None
    assert receipts(db, member.run.id) == []
    assert physical_rows(db, member) == []
    # 修正计数后的同批可正常完成
    fixed, _ = notification_batch(world, [snap_record(world, snap_row(1))],
                                   scan_count=1)
    assert ingest.ingest_batch(db, world.capture.id, fixed)["accepted"] == 1
    db.commit()
    assert member.run.status == "catching_up"


def test_scan_count_change_after_proof_is_rejected(db, world):
    member = world.members[0]
    records, ids = notification_batch(world, [snap_record(world, snap_row(1))],
                                       scan_count=1)
    partial = records[:-1]  # 无 COMPLETED：scan 计数证明先落库
    ingest.ingest_batch(db, world.capture.id, partial)
    db.commit()
    assert member.run.snapshot_proof["expected_rows"] == 1
    drift, _ = notification_batch(world, [], scan_count=2, snap_ids=ids)
    rejects(db, "SNAPSHOT_COUNT_CHANGED",
            lambda: ingest.ingest_batch(db, world.capture.id, drift[:3]))
    db.rollback()


def test_ack_loss_replay_of_identical_notification_batch(db, world):
    member = world.members[0]
    records, _ = notification_batch(
        world, [snap_record(world, snap_row(1)), snap_record(world, snap_row(2))])
    first = ingest.ingest_batch(db, world.capture.id, records)
    db.commit()
    replay = ingest.ingest_batch(db, world.capture.id, records)
    db.commit()
    assert first["accepted"] == 2
    assert replay == {"status": "accepted", "accepted": 0, "duplicates": 2}
    assert member.run.status == "catching_up"
    assert member.run.row_count == 2
    assert len(receipts(db, member.run.id)) == 1
    assert len(physical_rows(db, member)) == 2


def test_new_started_id_midway_is_rejected(db, world):
    records, ids = notification_batch(
        world, [snap_record(world, snap_row(1))], scan_count=1)
    partial = records[:2]  # 仅 STARTED + RESOLVED 落证明
    ingest.ingest_batch(db, world.capture.id, partial)
    db.commit()
    restarted = notification_batch(world, [], snap_ids={
        "start": str(uuid.uuid4()), "resolve": ids["resolve"],
        "scan": ids["scan"], "complete": ids["complete"]})
    rejects(db, "INITIAL_SNAPSHOT_RESTARTED", lambda: ingest.ingest_batch(
        db, world.capture.id, restarted[0][:1]))
    db.rollback()
    assert world.members[0].run.status == "loading"


def test_snapshot_row_after_completed_is_rejected(db, world):
    records, ids = notification_batch(world, [snap_record(world, snap_row(1))],
                                       scan_count=1)
    # r 排在 COMPLETED 通知之后：全局完成栅栏已闭合，旧行绝不覆盖
    late = records[:-1] + [records[-1], snap_record(world, snap_row(2))]
    rejects(db, "SNAPSHOT_ORDER_CHANGED",
            lambda: ingest.ingest_batch(db, world.capture.id, late))
    db.rollback()
    assert physical_rows(db, world.members[0]) == []
    # 完成后的新一批 r 行同样拒绝（loading 已结束）
    done, _ = notification_batch(world, [snap_record(world, snap_row(1))],
                                  scan_count=1)
    ingest.ingest_batch(db, world.capture.id, done)
    db.commit()
    rejects(db, "UNEXPECTED_SNAPSHOT", lambda: ingest.ingest_batch(
        db, world.capture.id, [snap_record(world, snap_row(3))]))
    db.rollback()


def test_full_batch_replay_after_publish_never_rolls_back_values(db, world):
    from app.data_channel.integrated import replica_runtime as runtime
    member = world.members[0]
    records, _ = notification_batch(
        world, [snap_record(world, snap_row(1)), snap_record(world, snap_row(2))])
    ingest.ingest_batch(db, world.capture.id, records)
    db.commit()
    receive_cut(db, world.capture, world.server, lsn=900)
    runtime.apply_capture(db, world.capture.id)  # snapshot complete + cut observed
    db.commit()
    assert member.run.status == "active"
    ingest.ingest_batch(db, world.capture.id, [
        change_record(world, table="orders", xid=100, lsn=10, ordinal=1, op="u",
                      after=snap_row(1, label="streamed")),
        end_record(world, xid=100, commit_lsn=500, count=1)])
    db.commit()
    runtime.apply_capture(db, world.capture.id)
    db.commit()
    rows = {row["id"]: row for row in physical_rows(db, member)}
    assert rows[1]["label"] == "streamed"
    published_count = member.run.row_count
    published_coverage = db.get(IntegratedReplica, member.asset.id).coverage_confirmed_at
    # ACK 丢失重投整批快照：回执命中幂等跳过，绝不回滚已发布后的流式值，
    # 也不得改写已确认的 coverage cut。
    replay = ingest.ingest_batch(db, world.capture.id, records)
    db.commit()
    assert replay == {"status": "accepted", "accepted": 0, "duplicates": 2}
    rows = {row["id"]: row for row in physical_rows(db, member)}
    assert rows[1]["label"] == "streamed"
    assert member.run.row_count == published_count
    assert member.run.status == "active"
    replica = db.get(IntegratedReplica, member.asset.id)
    assert replica.status == "ready"
    assert replica.coverage_confirmed_at == published_coverage
    assert db.get(IntegratedReplicaCapture, world.capture.id).source_offset["lsn"] == 500


def test_established_stream_accepts_skipped_snapshot_on_resume(db, world):
    records, _ = notification_batch(world, [snap_record(world, snap_row(1))])
    ingest.ingest_batch(db, world.capture.id, records)
    db.commit()
    receive_cut(db, world.capture, world.server, lsn=900)
    runtime.apply_capture(db, world.capture.id)
    db.commit()
    assert world.members[0].run.status == "active"
    result = ingest.ingest_batch(db, world.capture.id, [
        notify(world, kind="SKIPPED", event_id=uuid.uuid4(), offset={"lsn": 900})])
    db.commit()
    assert result == {"status": "accepted", "accepted": 0, "duplicates": 0}
    assert world.members[0].run.status == "active"
    assert world.members[0].run.row_count == 1
    for offset in ({}, {"ts_usec": 0}):
        rejects(db, "INITIAL_SNAPSHOT_INTERRUPTED", lambda: ingest.ingest_batch(
            db, world.capture.id, [notify(world, kind="SKIPPED",
                event_id=uuid.uuid4(), offset=offset)]))
        db.rollback()


def test_unfinished_snapshot_cannot_resume_with_skipped_notification(db, world):
    rejects(db, "INITIAL_SNAPSHOT_INTERRUPTED", lambda: ingest.ingest_batch(
        db, world.capture.id, [notify(world, kind="SKIPPED", event_id=uuid.uuid4())]))
    db.rollback()
    assert world.members[0].run.status == "loading"


def test_coverage_message_cut_is_ingested_then_recorded(db, world):
    member = world.members[0]
    records, _ = notification_batch(world, [snap_record(world, snap_row(1))],
                                    scan_count=1)
    ingest.ingest_batch(db, world.capture.id, records)
    db.commit()
    assert member.run.status == "catching_up"

    pending = prepare_cut(db, world.capture)
    record = cut_record(world.capture, world.server, pending["id"], lsn=901)
    result = ingest.ingest_batch(db, world.capture.id, [record])
    db.commit()
    # The logical message is a proof record, not a business row or journal TX.
    assert result == {"status": "accepted", "accepted": 0, "duplicates": 0}
    assert len(physical_rows(db, member)) == 1

    def capture_state():
        return db.scalar(sa.select(IntegratedReplicaCapture).where(
            IntegratedReplicaCapture.id == world.capture.id)
            .execution_options(populate_existing=True))

    capture = capture_state()
    observed = capture.coverage_probe["pending"]
    assert observed["id"] == pending["id"]
    assert observed["frontier"] == {"dialect": "postgresql", "lsn": 901}
    assert observed["observed_position"] == {"lsn": 901}
    assert observed["observed_seq"] == capture.committed_seq
    assert capture.source_offset["lsn"] == 901

    # Connector returns after the message; record_probe must not overwrite the
    # already-observed source position.
    coverage.record_probe(db, world.capture.id, pending["id"],
                          {"dialect": "postgresql", "lsn": 901})
    db.commit()
    capture = capture_state()
    assert capture.coverage_probe["pending"]["observed_position"] == {"lsn": 901}
    assert capture.coverage_probe["pending"]["observed_seq"] == capture.committed_seq

    assert runtime.apply_capture(db, world.capture.id) == 0
    db.commit()
    run = db.get(IntegratedReplicaRun, member.run.id)
    replica = db.get(IntegratedReplica, member.asset.id)
    assert run.status == "active"
    assert replica.status == "ready"
    assert replica.coverage_confirmed_at is not None


# ---------------------------------------------------------------------------
# mark_capture_error：候选失败不牵连独立 active
# ---------------------------------------------------------------------------


def test_late_capture_error_does_not_restore_stopped_member(db, world):
    member = world.members[0]
    control.request_stop(db, member.asset)
    db.commit()
    ingest.mark_capture_error(db, world.capture.id, "SOURCE_TIMEOUT", retryable=True)
    db.commit()
    replica = db.get(IntegratedReplica, member.asset.id)
    assert replica.status == "stopped"
    assert replica.last_error_code == "REPLICA_STOP_REQUESTED"
    assert replica.active_run_id is None
    assert replica.pending_run_id is None


def test_mark_capture_error_spares_independent_active(db, admin_user):
    world = make_cohort(db, admin_user, tables=("orders", "customers"))
    try:
        independent = world.members[0]  # 已发布且 active run 不属于本 capture
        dependent = world.members[1]  # 仍是本 capture 的初始化候选
        independent.replica.active_run_id = str(uuid.uuid4())
        independent.replica.pending_run_id = None
        independent.replica.status = "ready"
        db.flush()
        ingest.mark_capture_error(db, world.capture.id, "SOURCE_LOG_GAP",
                                  retryable=False)
        db.commit()
        fresh_independent = db.get(IntegratedReplica, independent.asset.id)
        assert fresh_independent.status == "ready"
        assert fresh_independent.last_error_code is None
        fresh_dependent = db.get(IntegratedReplica, dependent.asset.id)
        assert fresh_dependent.status == "rebuild_required"
        assert fresh_dependent.last_error_code == "SOURCE_LOG_GAP"
        capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
            IntegratedReplicaCapture.id == world.capture.id)
            .execution_options(populate_existing=True))
        assert capture.status == "blocked"
        assert capture.last_error_code == "SOURCE_LOG_GAP"
    finally:
        db.rollback()
        for member in world.members:
            store.drop_owned(db.connection(), member.run.id)
        db.commit()


def test_mark_capture_error_retryable_marks_stale(db, admin_user):
    world = make_cohort(db, admin_user)
    try:
        member = world.members[0]
        # 本 cohort 发布过的副本（active run 属于本 capture）才可重试降级为 stale
        member.replica.active_run_id = member.run.id
        db.flush()
        ingest.mark_capture_error(db, world.capture.id, "SOURCE_TIMEOUT",
                                  retryable=True)
        db.commit()
        assert db.get(IntegratedReplica, member.asset.id).status == "stale"
        capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
            IntegratedReplicaCapture.id == world.capture.id)
            .execution_options(populate_existing=True))
        assert capture.status == "stale"
    finally:
        db.rollback()
        store.drop_owned(db.connection(), world.members[0].run.id)
        db.commit()
