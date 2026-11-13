"""replica_apply 聚焦测试（真实 SQLite journal→apply→store 链路）。

契约被测面：按 commit_seq 逐完整事务应用（交错事务按 END 提交序、行 LSN 只是
身份）、partial END 不 apply、游标只前进不回退、PK 变更（MySQL d+c 事件对与
PG before_key）、JSON 原文精度与 SQL NULL/JSON null 分立、TOAST 占位从本地行
补值、baseline_filter 快照可见性（PG xmin/xmax/xip 与 MySQL 完整 GTID 集合，
跨 UUID 后缀不误跳）、gap/owner/pointer 围栏、row_count 差分（绝不全表 COUNT）。
PostgreSQL COPY/typed 真实语义归主会话 gate。
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
from app.data_channel.integrated import replica_journal as journal
from app.data_channel.integrated import replica_store as store
from app.data_channel.integrated.cdc_envelope import TOAST_PLACEHOLDER
from app.data_channel.integrated.contracts import JsonText, digest
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_apply import ReplicaApplyError, apply_run
from app.data_channel.integrated.replica_capture_models import (
    IntegratedReplicaCapture, IntegratedReplicaTransaction,
)
from app.data_channel.integrated.replica_models import IntegratedReplica

TS_MS = 1760000000000
PRECISION_JSON = '{"pi": 3.14159265358979323846264338327950288}'
BLOB = b"\x00\xde\xad\xbe\xef"
BLOB_B64 = base64.b64encode(BLOB).decode("ascii")
MOMENT = "2026-11-12T09:30:00.123456+08:00"
MOMENT_UTC = datetime(2026, 11, 12, 1, 30, 0, 123456, tzinfo=timezone.utc)
TOAST = TOAST_PLACEHOLDER
MYSQL_FILE = "mysql-bin.000003"
GTID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def columns():
    return [
        {"name": "id", "type": "integer", "nullable": False, "primary_key": True},
        {"name": "label", "type": "string", "nullable": True},
        {"name": "amount", "type": "decimal", "nullable": True,
         "precision": 12, "scale": 4},
        {"name": "doc", "type": "json", "nullable": True, "encoding": "json_text"},
        {"name": "blob", "type": "binary", "nullable": True},
        {"name": "moment", "type": "timestamp", "nullable": True, "timezone": True},
    ]


def manifest():
    return {"columns": columns(), "primary_key": ["id"]}


# ---------------------------------------------------------------------------
# Debezium envelope / transport 构造
# ---------------------------------------------------------------------------

def envelope_fields(mutate=None):
    fields = [
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
    return mutate(fields) if mutate else fields


def transport_schema(event_fields):
    return {
        "type": "struct",
        "name": journal.TRANSPORT_SCHEMA,
        "fields": [
            {"field": "topic", "type": "string"},
            {"field": "partition_json", "type": "string"},
            {"field": "offset_json", "type": "string"},
            {"field": "key", "type": "string", "optional": True},
            {"field": "event", "name": "oodb.public.orders.Envelope", "type": "struct",
             "fields": [
                 {"field": "op", "type": "string"},
                 {"field": "ts_ms", "type": "int64"},
                 {"field": "source", "type": "struct"},
                 {"field": "transaction", "type": "struct", "optional": True},
                 {"field": "before", "type": "struct", "optional": True,
                  "fields": event_fields},
                 {"field": "after", "type": "struct", "optional": True,
                  "fields": event_fields},
             ], "optional": True},
        ],
    }


def transport(event, *, server, offset, fields=None):
    payload = {"topic": "oo.public.orders", "key": None,
               "partition_json": json.dumps({"server": server}),
               "offset_json": json.dumps(offset)}
    if event is not None:
        payload["event"] = event
    return {"schema": transport_schema(envelope_fields() if fields is None else fields),
            "payload": payload}


def source_after(id_, label="x", amount="123.4500", doc=PRECISION_JSON,
                 blob=BLOB_B64, moment=MOMENT):
    after = {"id": id_, "label": label, "doc": doc, "moment": moment}
    after["amount"] = None if amount is None else amount
    after["blob"] = None if blob is None else blob
    return after


def pg_change(world, xid, lsn, ordinal, op, *, after=None, before=None,
              table="orders", fields=None):
    schema_name = world.asset.schema_name
    event = {"op": op, "ts_ms": TS_MS,
             "source": {"connector": "postgresql", "db": "oodb",
                        "schema": schema_name, "table": table, "lsn": lsn,
                        "txId": int(xid), "ts_ms": TS_MS},
             "transaction": {"id": f"{xid}:{lsn}", "total_order": ordinal,
                             "data_collection_order": ordinal}}
    if before is not None:
        event["before"] = before
    if after is not None:
        event["after"] = after
    return transport(event, server=world.server, offset={"lsn": lsn, "txId": int(xid)},
                     fields=fields)


def pg_end(world, xid, commit_lsn, count):
    event = {"status": "END", "id": f"{xid}:{commit_lsn}", "event_count": count,
             "data_collections": [{"event_count": count,
                                   "data_collection": "public.orders"}],
             "ts_ms": TS_MS}
    return transport(event, server=world.server,
                     offset={"lsn": commit_lsn, "txId": int(xid)})


def mysql_change(world, gtid, ordinal, op, *, pos, after=None, before=None):
    event = {"op": op, "ts_ms": TS_MS,
             "source": {"connector": "mysql", "db": world.asset.schema_name,
                        "table": world.asset.table_name, "file": MYSQL_FILE,
                        "pos": pos, "row": 0, "ts_ms": TS_MS},
             "transaction": {"id": gtid, "total_order": ordinal,
                             "data_collection_order": ordinal}}
    if before is not None:
        event["before"] = before
    if after is not None:
        event["after"] = after
    return transport(event, server=world.server,
                     offset={"file": MYSQL_FILE, "pos": pos, "gtids": gtid})


def mysql_end(world, gtid, count, *, pos):
    event = {"status": "END", "id": gtid, "event_count": count,
             "data_collections": [{"event_count": count,
                                   "data_collection": "oodb.orders"}],
             "ts_ms": TS_MS}
    return transport(event, server=world.server,
                     offset={"file": MYSQL_FILE, "pos": pos, "gtids": gtid})


def append(db, world, records, *, dialect="postgresql"):
    return journal.append_batch(db, world.capture.id, records, dialect=dialect)


def reject(db, code, fn):
    with pytest.raises(ReplicaApplyError) as exc:
        fn()
    assert exc.value.code == code
    return exc.value


# ---------------------------------------------------------------------------
# world：connection/dataset/asset/replica/run/capture + 物理表
# ---------------------------------------------------------------------------

def make_world(db, admin_user, *, dialect):
    suffix = uuid.uuid4().hex[:8]
    kind = "postgres" if dialect == "postgresql" else "mysql"
    schema_name = "public" if dialect == "postgresql" else "oodb"
    connection = Connection(id=f"apply-src-{suffix}", name=f"apply {dialect}",
                            kind=kind, config={})
    dataset = Dataset(id=f"apply-ds-{suffix}", name=f"apply {dialect}", kind="integrated",
                      source_connection_id=connection.id, source_resource="orders")
    db_manifest = manifest()
    asset = IntegratedAsset(
        id=f"apply-asset-{suffix}", dataset_id=dataset.id, connection_id=connection.id,
        name=f"apply {dialect}", schema_name=schema_name, table_name="orders",
        schema_json=db_manifest, schema_fingerprint=digest(db_manifest),
        writable=False, created_by=admin_user.id)
    capture_id = str(uuid.uuid4())
    capture = IntegratedReplicaCapture(
        id=capture_id, connection_id=connection.id, generation=1, status="streaming",
        identity_fingerprint="f" * 64, tables_json=[])
    db.add_all([connection, dataset, asset, capture])
    db.commit()

    run = control.request_rebuild(db, asset)
    token = control.claim_run(db, run.id)
    control.begin_load(db, run.id, token, capture_id=capture_id,
                       capture_start={"dialect": dialect, "lsn": 1})
    control.finish_load(db, run.id, token, completion={
        "complete": True, "row_count": 0, "expected_rows": 0,
        "schema_fingerprint": asset.schema_fingerprint, "snapshot_id": "snap-one"})
    schema = store.build_replica_schema(run.id, run.schema_json)
    store.create_table(db.connection(), schema)
    db.commit()
    return SimpleNamespace(
        dialect=dialect, asset=asset, capture=capture, run=run, token=token,
        schema=schema, server="oo_replica_" + uuid.UUID(capture_id).hex)


@pytest.fixture
def world(db, admin_user):
    built = make_world(db, admin_user, dialect="postgresql")
    yield built
    db.rollback()
    store.drop_owned(db.connection(), built.run.id)
    db.commit()


@pytest.fixture
def mysql_world(db, admin_user):
    built = make_world(db, admin_user, dialect="mysql")
    yield built
    db.rollback()
    store.drop_owned(db.connection(), built.run.id)
    db.commit()


def rows(db, world):
    return [row for batch in store.read_batches(db.connection(), world.schema)
            for row in batch]


def commit_seq(db, world, txkey):
    return db.scalar(sa.select(IntegratedReplicaTransaction.commit_seq).where(
        IntegratedReplicaTransaction.capture_id == world.capture.id,
        IntegratedReplicaTransaction.source_transaction_id == str(txkey)))


def apply(db, world, **kwargs):
    return apply_run(db, world.run.id, owner_token=world.token, **kwargs)


# ---------------------------------------------------------------------------
# 完整事务应用：精度、游标、row_count
# ---------------------------------------------------------------------------

def test_full_transactions_apply_with_exact_values_and_cursor(db, world):
    append(db, world, [
        pg_change(world, 100, 10, 1, "c", after=source_after(1, label="one")),
        pg_change(world, 100, 20, 2, "c",
                  after=source_after(2, label="two", amount=None, doc="null")),
        pg_end(world, 100, 500, 2),
    ])
    db.commit()
    assert apply(db, world) == 1
    append(db, world, [
        pg_change(world, 200, 30, 1, "u", after=source_after(1, label="uno")),
        pg_end(world, 200, 700, 1),
    ])
    db.commit()
    assert apply(db, world) == 1
    data = {row["id"]: row for row in rows(db, world)}
    assert set(data) == {1, 2}
    # JSON 原文逐字保留（绝不经浮点）、decimal/二进制/时间精确
    assert data[1]["label"] == "uno"
    assert str(data[1]["amount"]) == "123.4500"
    assert isinstance(data[1]["doc"], JsonText)
    assert str(data[1]["doc"]) == PRECISION_JSON
    assert data[1]["blob"] == BLOB
    assert data[1]["moment"] == MOMENT_UTC
    # SQL NULL 与 JSON null 是两个值
    assert data[2]["amount"] is None
    assert data[2]["doc"] is not None
    assert data[2]["doc"] == JsonText("null")
    assert world.run.row_count == 2
    cursor = world.run.applied_position
    assert cursor["capture_id"] == world.capture.id
    assert cursor["commit_seq"] == commit_seq(db, world, "200")
    assert cursor["source_position"]["commit_lsn"] == 700
    # catching_up：replica 镜像不提前（coverage/applied 归 publish）
    replica = db.get(IntegratedReplica, world.asset.id)
    assert replica.applied_position is None and replica.applied_at is None


def test_interleaved_transactions_apply_in_commit_order_not_row_lsn(db, world):
    # A 的行 LSN 更低但提交更晚：应用序只由 END 决定，A 最终覆盖 B
    append(db, world, [
        pg_change(world, 100, 10, 1, "c", after=source_after(1, label="A")),
        pg_change(world, 200, 20, 1, "c", after=source_after(1, label="B")),
        pg_end(world, 200, 300, 1),
        pg_end(world, 100, 500, 1),
    ])
    db.commit()
    assert apply(db, world) == 2
    data = rows(db, world)
    assert len(data) == 1 and data[0]["label"] == "A"
    assert world.run.row_count == 1
    assert world.run.applied_position["commit_seq"] == commit_seq(db, world, "100")


def test_partial_end_never_applies(db, world):
    append(db, world, [pg_change(world, 100, 10, 1, "c", after=source_after(1))])
    db.commit()
    assert apply(db, world) == 0
    assert world.run.applied_position is None
    assert store.count_rows(db.connection(), world.schema) == 0


def test_replay_is_idempotent_and_cursor_never_regresses(db, world):
    for xid, lsn in ((100, 500), (200, 700)):
        append(db, world, [
            pg_change(world, xid, lsn - 400, 1, "c", after=source_after(1, label=str(xid))),
            pg_end(world, xid, lsn, 1)])
        db.commit()
    assert apply(db, world) == 2
    latest = dict(world.run.applied_position)
    assert apply(db, world) == 0  # 重放：无新增、游标不动
    assert world.run.applied_position == latest
    # 陈旧游标只向前补：重放第二个事务仍幂等，终点回到最新
    world.run.applied_position = {**latest, "commit_seq": latest["commit_seq"] - 2}
    db.flush()
    assert apply(db, world) == 1
    assert world.run.applied_position == latest
    assert rows(db, world)[0]["label"] == "200"


def test_max_transactions_budget(db, world):
    for n, (xid, lsn) in enumerate(((100, 500), (200, 700), (300, 900)), start=1):
        append(db, world, [
            pg_change(world, xid, lsn - 400, 1, "c", after=source_after(n)),
            pg_end(world, xid, lsn, 1)])
        db.commit()
    assert apply(db, world, max_transactions=2) == 2
    assert store.count_rows(db.connection(), world.schema) == 2
    assert world.run.applied_position["commit_seq"] == commit_seq(db, world, "200")
    assert apply(db, world) == 1
    assert world.run.applied_position["commit_seq"] == commit_seq(db, world, "300")


# ---------------------------------------------------------------------------
# 主键变更：MySQL d+c 事件对 / PG before_key
# ---------------------------------------------------------------------------

def test_mysql_pk_change_as_delete_create_pair(db, mysql_world):
    world = mysql_world
    gtid1 = f"{GTID_A}:1"
    append(db, world, [
        mysql_change(world, gtid1, 1, "c", pos=1000, after=source_after(1)),
        mysql_end(world, gtid1, 1, pos=1100)], dialect="mysql")
    db.commit()
    assert apply(db, world) == 1
    gtid2 = f"{GTID_A}:2"
    append(db, world, [
        mysql_change(world, gtid2, 1, "d", pos=1200, before=source_after(1)),
        mysql_change(world, gtid2, 2, "c", pos=1250, after=source_after(2, label="moved")),
        mysql_end(world, gtid2, 2, pos=1300)], dialect="mysql")
    db.commit()
    assert apply(db, world) == 1
    data = rows(db, world)
    assert [row["id"] for row in data] == [2]
    assert data[0]["label"] == "moved"
    assert world.run.row_count == 1  # 差分：旧键删除 + 新键插入


def test_postgres_pk_change_uses_before_key(db, world):
    append(db, world, [
        pg_change(world, 100, 10, 1, "c", after=source_after(1)),
        pg_end(world, 100, 500, 1)])
    db.commit()
    apply(db, world)
    # PG 默认 replica identity：before 像仅携带旧主键
    append(db, world, [
        pg_change(world, 200, 20, 1, "u", before={"id": 1},
                  after=source_after(2, label="moved")),
        pg_end(world, 200, 700, 1)])
    db.commit()
    assert apply(db, world) == 1
    data = rows(db, world)
    assert [row["id"] for row in data] == [2]
    assert data[0]["label"] == "moved"
    assert world.run.row_count == 1


# ---------------------------------------------------------------------------
# TOAST：本地行补值 / 缺行明确报错
# ---------------------------------------------------------------------------

def test_toast_placeholder_fills_from_local_row(db, world):
    store.write_batch(db.connection(), world.schema, [{
        "id": 1, "label": "old", "amount": {"$decimal": "123.4500"},
        "doc": PRECISION_JSON, "blob": {"$binary": BLOB_B64}, "moment": MOMENT}])
    world.run.row_count = 1
    db.commit()
    append(db, world, [
        pg_change(world, 100, 10, 1, "u",
                  after=source_after(1, label="new", amount="9.9999",
                                     blob=TOAST, doc='{"k": 1}')),
        pg_end(world, 100, 500, 1)])
    db.commit()
    assert apply(db, world) == 1
    row = rows(db, world)[0]
    assert row["label"] == "new"
    assert row["blob"] == BLOB  # unchanged 字段从当前本地行补回
    assert str(row["amount"]) == "9.9999"
    assert world.run.row_count == 1  # 更新不改变计数


def test_toast_placeholder_without_local_row_fails_closed(db, world):
    append(db, world, [
        pg_change(world, 100, 10, 1, "u",
                  after=source_after(99, label="x", blob=TOAST)),
        pg_end(world, 100, 500, 1)])
    db.commit()
    reject(db, "TOAST_UNAVAILABLE", lambda: apply(db, world))
    db.rollback()  # 调用方回滚：无部分应用
    assert world.run.applied_position is None
    assert store.count_rows(db.connection(), world.schema) == 0


def test_toast_reads_prior_change_within_same_transaction(db, world):
    # 事务前旧行 blob=BLOB；同事务第一条 u 全像写入 BLOB2；第二条 u 的 TOAST
    # 占位必须补到 BLOB2（前一条刚落下的像），绝不能读事务前的 BLOB。
    other = b"\x11\x22\x33toast-new"
    store.write_batch(db.connection(), world.schema, [{
        "id": 1, "label": "orig", "amount": {"$decimal": "1.0000"},
        "doc": PRECISION_JSON, "blob": {"$binary": BLOB_B64}, "moment": MOMENT}])
    world.run.row_count = 1
    db.commit()
    append(db, world, [
        pg_change(world, 100, 10, 1, "u",
                  after=source_after(1, label="first",
                                     blob=base64.b64encode(other).decode())),
        pg_change(world, 100, 20, 2, "u",
                  after=source_after(1, label="second", blob=TOAST)),
        pg_end(world, 100, 500, 2)])
    db.commit()
    assert apply(db, world) == 1
    row = rows(db, world)[0]
    assert row["label"] == "second"
    assert row["blob"] == other  # 来自同事务前一条变更，不是事务前旧表
    assert world.run.row_count == 1


def test_toast_fills_from_old_key_when_primary_key_changes(db, world):
    # PG 默认 replica identity：before 只含旧主键。主键 1→2 且 blob 未变更
    # （TOAST 占位）时，本地行仍挂在旧键 1 下，补值必须查旧键，不能查新键。
    store.write_batch(db.connection(), world.schema, [{
        "id": 1, "label": "orig", "amount": {"$decimal": "1.0000"},
        "doc": PRECISION_JSON, "blob": {"$binary": BLOB_B64}, "moment": MOMENT}])
    world.run.row_count = 1
    db.commit()
    append(db, world, [
        pg_change(world, 100, 10, 1, "u", before={"id": 1},
                  after=source_after(2, label="moved", blob=TOAST)),
        pg_end(world, 100, 500, 1)])
    db.commit()
    assert apply(db, world) == 1
    data = rows(db, world)
    assert [row["id"] for row in data] == [2]
    assert data[0]["label"] == "moved"
    assert data[0]["blob"] == BLOB  # 旧键本地像补回未变更 TOAST
    assert world.run.row_count == 1


def test_same_transaction_pk_change_then_second_toast_uses_new_local_key(db, world):
    # 同事务两条变更：第一条改主键且 TOAST 从旧键补；第二条已在新键下，
    # TOAST 必须从第一条刚落下的新键行补。
    other = b"\xaa\xbb\xccpk-move"
    store.write_batch(db.connection(), world.schema, [{
        "id": 1, "label": "orig", "amount": {"$decimal": "1.0000"},
        "doc": PRECISION_JSON, "blob": {"$binary": BLOB_B64}, "moment": MOMENT}])
    world.run.row_count = 1
    db.commit()
    append(db, world, [
        pg_change(world, 100, 10, 1, "u", before={"id": 1},
                  after=source_after(2, label="first",
                                     blob=base64.b64encode(other).decode())),
        pg_change(world, 100, 20, 2, "u", before={"id": 2},
                  after=source_after(2, label="second", blob=TOAST)),
        pg_end(world, 100, 500, 2)])
    db.commit()
    assert apply(db, world) == 1
    data = rows(db, world)
    assert [row["id"] for row in data] == [2]
    assert data[0]["label"] == "second"
    assert data[0]["blob"] == other
    assert world.run.row_count == 1


# ---------------------------------------------------------------------------
# 多表事务过滤 / schema 漂移拒绝
# ---------------------------------------------------------------------------

def test_transaction_filters_other_tables_of_same_capture(db, world):
    append(db, world, [
        pg_change(world, 100, 10, 1, "c", after=source_after(1)),
        pg_change(world, 100, 20, 2, "c", after={"id": 500}, table="customers"),
        pg_end(world, 100, 500, 2)])
    db.commit()
    assert apply(db, world) == 1
    data = rows(db, world)
    assert [row["id"] for row in data] == [1]
    assert world.run.row_count == 1


def test_column_type_drift_rejected(db, world):
    def mutate(fields):
        return [dict(entry, type="string", name=None)
                if entry["field"] == "amount" else entry for entry in fields]

    append(db, world, [
        pg_change(world, 100, 10, 1, "c", after=source_after(1),
                  fields=mutate(envelope_fields())),
        pg_end(world, 100, 500, 1)])
    db.commit()
    reject(db, "REPLICA_SCHEMA_DRIFT", lambda: apply(db, world))
    db.rollback()
    assert store.count_rows(db.connection(), world.schema) == 0
    assert world.run.applied_position is None


def test_run_manifest_fingerprint_drift_rejected_before_apply(db, world):
    # schema_json 与冻结指纹不一致时不得用漂移 manifest 解 receipt。
    tampered = json.loads(json.dumps(world.run.schema_json))
    tampered["columns"][1]["nullable"] = False
    world.run.schema_json = tampered
    db.flush()
    reject(db, "MANIFEST_INVALID", lambda: apply(db, world))
    assert rows(db, world) == []


# ---------------------------------------------------------------------------
# 围栏：capture gap / owner / 指针失效
# ---------------------------------------------------------------------------

def test_capture_gap_refuses_apply(db, world):
    world.capture.status = "gap"
    db.commit()
    reject(db, "CAPTURE_NOT_APPLYABLE", lambda: apply(db, world))


def test_owner_fences(db, world):
    reject(db, "REPLICA_OWNER_LOST",
           lambda: apply_run(db, world.run.id))  # catching_up 必须携带 token
    reject(db, "REPLICA_OWNER_LOST",
           lambda: apply_run(db, world.run.id, owner_token="nope"))
    world.run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.flush()
    reject(db, "REPLICA_OWNER_LOST", lambda: apply(db, world))
    db.rollback()
    replica = db.get(IntegratedReplica, world.asset.id)
    replica.pending_run_id = str(uuid.uuid4())
    db.flush()
    reject(db, "REPLICA_OWNER_LOST", lambda: apply(db, world))
    db.rollback()
    reject(db, "REPLICA_RUN_MISSING", lambda: apply_run(db, str(uuid.uuid4())))
    reject(db, "INVALID_MAX_TRANSACTIONS", lambda: apply(db, world, max_transactions=0))


def test_active_pointer_lost_refuses_apply(db, world):
    append(db, world, [
        pg_change(world, 100, 10, 1, "c", after=source_after(1)),
        pg_end(world, 100, 500, 1)])
    db.commit()
    apply(db, world)
    control.publish_run(db, world.run.id, world.token,
                        applied_position=world.run.applied_position,
                        coverage_confirmed_at=datetime.now(timezone.utc),
                        row_count=1)
    db.commit()
    replica = db.get(IntegratedReplica, world.asset.id)
    replica.active_run_id = str(uuid.uuid4())
    db.flush()
    reject(db, "ACTIVE_POINTER_LOST", lambda: apply_run(db, world.run.id))
    db.rollback()


def test_active_apply_mirrors_replica_without_coverage(db, world):
    append(db, world, [
        pg_change(world, 100, 10, 1, "c", after=source_after(1, label="v1")),
        pg_end(world, 100, 500, 1)])
    db.commit()
    apply(db, world)
    coverage = datetime.now(timezone.utc)
    control.publish_run(db, world.run.id, world.token,
                        applied_position=world.run.applied_position,
                        coverage_confirmed_at=coverage, row_count=1)
    db.commit()
    append(db, world, [
        pg_change(world, 300, 20, 1, "u", after=source_after(1, label="v2")),
        pg_end(world, 300, 700, 1)])
    db.commit()
    # active run：无 token，直接 apply 并镜像 replica 水位
    assert apply_run(db, world.run.id) == 1
    replica = db.get(IntegratedReplica, world.asset.id)
    assert replica.applied_position == world.run.applied_position
    assert replica.applied_at is not None
    assert replica.coverage_confirmed_at is not None  # 只由显式确认推进，本次不动
    assert rows(db, world)[0]["label"] == "v2"
    assert world.run.row_count == 1
