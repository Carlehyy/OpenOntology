"""replica_runtime 聚焦测试（真实 SQLite cohort 全生命周期，无源 IO/容器）。

契约被测面：queue_registered 只登记无副本资产；prepare_capture 为同一连接的
queued run 建独立 cohort（私表 lake_ir_*、claim 租约、tables_json 成员），无
成员/未知连接返回 None；abandon_initialization 丢弃丢失的本地快照（绝不 resume
部分行），retry=True 重新排队并进入新代 capture；apply_capture 在同一调用方事务
里对 cohort 全部 run 应用跨表事务，并且只有 ingest 收到受管 PG coverage cut、
全部成员应用到 observed_seq 且游标追平 capture.committed_seq 后才 publish；
snapshot complete 与 heartbeat 都只是活跃/快照事实，绝不单独发布或推进 coverage。
cleanup_relations 只清理终态且不再被引用的 run 物理表与快照回执。
PostgreSQL COPY 真实语义归主会话 gate；未实现的完成通知路径只 probe 现状。
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone
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

COLUMNS = {
    "orders": lambda: [
        {"name": "id", "type": "integer", "nullable": False, "primary_key": True},
        {"name": "label", "type": "string", "nullable": True},
        {"name": "amount", "type": "decimal", "nullable": True,
         "precision": 12, "scale": 4},
        {"name": "doc", "type": "json", "nullable": True, "encoding": "json_text"},
        {"name": "blob", "type": "binary", "nullable": True},
        {"name": "moment", "type": "timestamp", "nullable": True, "timezone": True},
    ],
    "customers": lambda: [
        {"name": "id", "type": "integer", "nullable": False, "primary_key": True},
        {"name": "note", "type": "string", "nullable": True},
    ],
}
FIELDS = {
    "orders": lambda: [
        {"field": "id", "type": "int64", "optional": False},
        {"field": "label", "type": "string", "optional": True},
        {"field": "amount", "name": "io.debezium.data.Decimal", "type": "string",
         "parameters": {"scale": "4"}, "optional": True},
        {"field": "doc", "name": "io.debezium.data.Json", "type": "string",
         "optional": True},
        {"field": "blob", "type": "bytes", "optional": True},
        {"field": "moment", "name": "io.debezium.time.ZonedTimestamp",
         "type": "string", "optional": True},
    ],
    "customers": lambda: [
        {"field": "id", "type": "int64", "optional": False},
        {"field": "note", "type": "string", "optional": True},
    ],
}


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


def snap_record(world, row, *, table, offset=None):
    event = {"op": "r", "ts_ms": TS_MS,
             "source": {"connector": "postgresql", "db": "oodb", "schema": "public",
                        "table": table, "ts_ms": TS_MS, "snapshot": "true"},
             "after": row}
    return transport(event, server=world.server,
                     offset=offset or {"lsn": 100 + row["id"]}, table=table)


def change_record(world, *, table, xid, lsn, ordinal, op, after=None, before=None):
    event = {"op": op, "ts_ms": TS_MS,
             "source": {"connector": "postgresql", "db": "oodb", "schema": "public",
                        "table": table, "lsn": lsn, "txId": int(xid), "ts_ms": TS_MS},
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
    """Persist the token and commit before simulating connector source IO."""
    pending = coverage.prepare_probe(db, capture.id)
    assert pending is not None, "coverage probe requires a completed, non-loading cohort"
    db.commit()
    return pending


def receive_cut(db, capture, server, *, lsn=900, message_first=False):
    """Exercise both real orderings: connector record then message, or reverse."""
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


def snap_row(id_, label="x"):
    return {"id": id_, "label": label, "amount": "123.4500",
            "doc": PRECISION_JSON, "blob": BLOB_B64, "moment": MOMENT}


def make_assets(db, admin_user, *, connection, tables):
    for table in tables:
        manifest = {"columns": COLUMNS[table](), "primary_key": ["id"]}
        dataset = Dataset(id=f"rt-ds-{connection.id}-{table}", name=f"rt {table}",
                          kind="integrated", source_connection_id=connection.id,
                          source_resource=table)
        db.add(dataset)
        db.add(IntegratedAsset(
            id=f"rt-asset-{connection.id}-{table}", dataset_id=dataset.id,
            connection_id=connection.id, name=f"rt {table}", schema_name="public",
            table_name=table, schema_json=manifest, schema_fingerprint=digest(manifest),
            writable=False, created_by=admin_user.id))
    db.commit()


def bind_members(db, capture):
    members = []
    for member in capture.tables_json:
        run = db.get(IntegratedReplicaRun, member["run_id"])
        members.append(SimpleNamespace(
            asset=db.get(IntegratedAsset, member["asset_id"]), run=run,
            replica=db.get(IntegratedReplica, member["asset_id"]),
            schema=store.build_replica_schema(run.id, run.schema_json,
                                              schema_fingerprint=run.schema_fingerprint)))
    return members


def server_of(capture):
    return "oo_replica_" + uuid.UUID(capture.id).hex


def make_cohort(db, admin_user, *, tables=("orders",), connection_id=None):
    suffix = uuid.uuid4().hex[:8]
    connection = Connection(id=connection_id or f"rt-src-{suffix}",
                            name=f"runtime {suffix}", kind="postgres", config={},
                            status="active", created_by=admin_user.id)
    db.add(connection)
    make_assets(db, admin_user, connection=connection, tables=tables)
    runtime.queue_registered(db)
    capture = runtime.prepare_capture(db, connection.id)
    db.commit()
    return SimpleNamespace(connection=connection, capture=capture,
                           members=bind_members(db, capture),
                           server=server_of(capture))


def drop_all(db, world):
    db.rollback()
    for member in world.members:
        store.drop_owned(db.connection(), member.run.id)
    db.commit()


@pytest.fixture
def world(db, admin_user):
    built = make_cohort(db, admin_user)
    try:
        yield built
    finally:
        drop_all(db, built)


def physical_rows(db, member):
    return [row for batch in store.read_batches(db.connection(), member.schema)
            for row in batch]


def member_of(world_, table):
    for member in world_.members:
        if member.asset.table_name == table:
            return member
    raise AssertionError(f"no member for {table}")


def complete_snapshot(db, world_, member, *, confirmed=None):
    control.finish_load(db, member.run.id, member.run.owner_token, completion={
        "complete": True, "row_count": member.run.row_count,
        "expected_rows": member.run.row_count,
        "schema_fingerprint": member.run.schema_fingerprint,
        "snapshot_id": world_.capture.id,
        "source_confirmed_at": confirmed or datetime.now(timezone.utc).isoformat(),
        "offset": {"lsn": 1}})


# ---------------------------------------------------------------------------
# queue_registered / prepare_capture
# ---------------------------------------------------------------------------

def test_queue_registered_enrolls_only_new_assets_and_prepare_builds_cohort(
        db, admin_user):
    connection = Connection(id="rt-conn-a", name="rt a", kind="postgres", config={},
                            status="active", created_by=admin_user.id)
    other = Connection(id="rt-conn-b", name="rt b", kind="postgres", config={},
                       status="active", created_by=admin_user.id)
    db.add_all([connection, other])
    make_assets(db, admin_user, connection=connection, tables=("orders", "customers"))
    make_assets(db, admin_user, connection=other, tables=("orders",))
    runtime.queue_registered(db)
    queued = db.scalars(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.status == "queued")).all()
    assert len(queued) == 3  # 三个资产全部登记，且 queue_registered 幂等
    runtime.queue_registered(db)
    assert db.scalar(sa.select(sa.func.count()).select_from(IntegratedReplicaRun)) == 3

    capture = runtime.prepare_capture(db, connection.id)
    db.commit()
    members = bind_members(db, capture)
    try:
        assert [m.asset.table_name for m in members] == ["customers", "orders"]
        assert capture.status == "starting"
        assert capture.generation == 1
        assert capture.identity_fingerprint == capture.identity_fingerprint
        inspector = sa.inspect(db.connection())
        for member in members:
            assert member.run.status == "loading"
            assert member.run.capture_id == capture.id
            assert member.run.owner_token and member.run.lease_expires_at
            assert inspector.has_table(member.schema.table_name)
            assert member.replica.pending_run_id == member.run.id
        # 同连接无新增 queued run；未知连接 → None
        assert runtime.prepare_capture(db, connection.id) is None
        assert runtime.prepare_capture(db, "missing-connection") is None
        other_capture = runtime.prepare_capture(db, other.id)
        db.commit()
        assert other_capture is not None and other_capture.generation == 1
        assert other_capture.id != capture.id  # 每连接独立 cohort
    finally:
        drop_all(db, SimpleNamespace(members=members))
        db.rollback()
        store.drop_owned(db.connection(), bind_members(db, other_capture)[0].run.id)
        db.commit()


def test_unsupported_table_does_not_block_other_queued_source_tables(db, admin_user):
    connection = Connection(id="rt-invalid-table", name="invalid table fixture",
                            kind="postgres", config={}, status="active",
                            created_by=admin_user.id)
    db.add(connection)
    make_assets(db, admin_user, connection=connection, tables=("orders", "customers"))
    invalid = db.scalar(sa.select(IntegratedAsset).where(
        IntegratedAsset.connection_id == connection.id,
        IntegratedAsset.table_name == "orders"))
    manifest = {"columns": [{"name": "id", "type": "unsupported", "nullable": False,
                              "primary_key": True}], "primary_key": ["id"]}
    invalid.schema_json, invalid.schema_fingerprint = manifest, digest(manifest)
    db.commit()
    runtime.queue_registered(db)
    capture = runtime.prepare_capture(db, connection.id)
    db.commit()
    members = bind_members(db, capture)
    try:
        assert [member.asset.table_name for member in members] == ["customers"]
        replica = db.get(IntegratedReplica, invalid.id)
        assert replica.status == "blocked"
        assert replica.pending_run_id is None
        assert replica.last_error_code is not None
        failed = db.scalar(sa.select(IntegratedReplicaRun).where(
            IntegratedReplicaRun.asset_id == invalid.id))
        assert failed.status == "failed"
        assert failed.last_error_code == replica.last_error_code
        assert runtime.prepare_capture(db, connection.id) is None
    finally:
        drop_all(db, SimpleNamespace(members=members))


def test_abandon_initialization_discards_and_requeues(db, admin_user):
    world = make_cohort(db, admin_user)
    try:
        member = world.members[0]
        runtime.abandon_initialization(db, world.capture.id, code="SNAPSHOT_LOST",
                                       retry=False)
        db.commit()
        assert member.run.status == "failed"
        assert member.run.owner_token is None
        assert db.get(IntegratedReplica, member.asset.id).status == "blocked"
        capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
            IntegratedReplicaCapture.id == world.capture.id)
            .execution_options(populate_existing=True))
        assert capture.status == "draining"
        assert capture.last_error_code == "SNAPSHOT_LOST"
        # 丢弃的本地快照绝不 resume：没有新 queued run，prepare 不会再收编
        assert runtime.prepare_capture(db, world.connection.id) is None
    finally:
        drop_all(db, world)

    # retry=True：丢弃的候选当场重新排队，prepare 收编进新代 cohort
    second = make_cohort(db, admin_user)
    try:
        member = second.members[0]
        runtime.abandon_initialization(db, second.capture.id, code="SOURCE_TIMEOUT",
                                       retry=True)
        db.commit()
        assert member.run.status == "failed"
        pending = db.get(IntegratedReplica, member.asset.id).pending_run_id
        assert pending is not None and pending != member.run.id
        replacement = runtime.prepare_capture(db, second.connection.id)
        db.commit()
        assert replacement is not None
        assert replacement.generation == second.capture.generation + 1
        assert [m["run_id"] for m in replacement.tables_json] == [pending]
        second.members.append(bind_members(db, replacement)[0])
    finally:
        drop_all(db, second)


# ---------------------------------------------------------------------------
# apply_capture：跨表同事务、发布门槛、心跳非覆盖
# ---------------------------------------------------------------------------

def test_cross_table_transaction_applies_and_publishes_cohort_atomically(
        db, admin_user):
    world = make_cohort(db, admin_user, tables=("orders", "customers"))
    try:
        ingest.ingest_batch(db, world.capture.id, [
            snap_record(world, snap_row(1), table="orders"),
            snap_record(world, {"id": 50, "note": "baseline"}, table="customers")])
        for member in world.members:
            complete_snapshot(db, world, member)
        db.commit()
        # 一个源事务同时改两张表：整个 cohort 在同一调用方事务里应用
        ingest.ingest_batch(db, world.capture.id, [
            change_record(world, table="orders", xid=100, lsn=10, ordinal=1, op="u",
                          after=snap_row(1, label="cross")),
            change_record(world, table="customers", xid=100, lsn=20, ordinal=2,
                          op="u", after={"id": 50, "note": "cross"}),
            end_record(world, xid=100, commit_lsn=500, count=2)])
        db.commit()
        # The cut is observed after the cross-table transaction; every member
        # must apply it before the cohort can publish.
        receive_cut(db, world.capture, world.server, lsn=900)
        applied = runtime.apply_capture(db, world.capture.id)
        assert applied == 2
        for member in world.members:
            replica = db.get(IntegratedReplica, member.asset.id)
            assert replica.status == "ready"
            assert replica.active_run_id == member.run.id
            assert member.run.status == "active"
            assert member.run.applied_position["commit_seq"] == \
                db.get(IntegratedReplicaCapture, world.capture.id).committed_seq
        assert {row["label"] for row in physical_rows(db, member_of(world, "orders"))} \
            == {"cross"}
        assert physical_rows(db, member_of(world, "customers"))[0]["note"] == "cross"
        assert db.get(IntegratedReplica, member_of(world, "orders").asset.id) \
            .coverage_confirmed_at is not None  # 发布时由已应用的 coverage cut 确认

        # 调用方回滚即全批蒸发：游标、发布、物理行都不落
        ingest.ingest_batch(db, world.capture.id, [
            change_record(world, table="orders", xid=200, lsn=30, ordinal=1, op="u",
                          after=snap_row(1, label="rollback-me")),
            end_record(world, xid=200, commit_lsn=700, count=1)])
        db.commit()
        assert runtime.apply_capture(db, world.capture.id) == 2  # 两 run 各自追平同一事务
        db.rollback()
        world.members = bind_members(db, world.capture)
        member = member_of(world, "orders")
        assert member.run.status == "active"  # 发布是先前已提交事务的事实
        assert member.run.applied_position["commit_seq"] != 700  # 本批 apply 已回滚
        assert {row["label"] for row in physical_rows(db, member)} == {"cross"}
        # 回滚后重放：两 run 各自补应用该事务，终点正确
        assert runtime.apply_capture(db, world.capture.id) == 2
        db.commit()
        world.members = bind_members(db, world.capture)
        assert {row["label"] for row in physical_rows(db, member_of(world, "orders"))} \
            == {"rollback-me"}
    finally:
        drop_all(db, world)


def test_snapshot_complete_and_heartbeat_without_cut_never_publish(db, admin_user):
    world = make_cohort(db, admin_user)
    try:
        member = world.members[0]
        ingest.ingest_batch(db, world.capture.id, [snap_record(world, snap_row(1),
                                                               table="orders")])
        complete_snapshot(db, world, member)
        db.commit()
        # A completed snapshot alone is not source coverage.
        assert runtime.apply_capture(db, world.capture.id) == 0
        db.commit()
        run = db.get(IntegratedReplicaRun, member.run.id)
        replica = db.get(IntegratedReplica, member.asset.id)
        assert run.status == "catching_up"
        assert replica.active_run_id is None
        assert replica.coverage_confirmed_at is None

        # Heartbeats are liveness only, even after the snapshot is complete.
        ingest.ingest_batch(db, world.capture.id, [heartbeat(world, {"lsn": 800})])
        db.commit()
        assert runtime.apply_capture(db, world.capture.id) == 0
        db.commit()
        run = db.get(IntegratedReplicaRun, member.run.id)
        replica = db.get(IntegratedReplica, member.asset.id)
        assert run.status == "catching_up"
        assert replica.active_run_id is None
        assert replica.coverage_confirmed_at is None

        # The real cut arrives through ingest before the connector records it.
        receive_cut(db, world.capture, world.server, lsn=900, message_first=True)
        assert runtime.apply_capture(db, world.capture.id) == 0
        db.commit()
        run = db.get(IntegratedReplicaRun, member.run.id)
        replica = db.get(IntegratedReplica, member.asset.id)
        assert run.status == "active"
        assert replica.active_run_id == run.id
        assert replica.coverage_confirmed_at is not None
    finally:
        drop_all(db, world)


def test_prior_end_must_apply_before_cut_publishes(db, admin_user):
    world = make_cohort(db, admin_user, tables=("customers",))
    try:
        member = world.members[0]
        ingest.ingest_batch(db, world.capture.id, [
            snap_record(world, {"id": 1, "note": "a"}, table="customers")])
        complete_snapshot(db, world, member)
        for n, (xid, lsn) in enumerate(((100, 10), (200, 20)), start=1):
            ingest.ingest_batch(db, world.capture.id, [
                change_record(world, table="customers", xid=xid, lsn=lsn, ordinal=1,
                              op="u", after={"id": 1, "note": f"v{n}"}),
                end_record(world, xid=xid, commit_lsn=500 + n, count=1)])
            db.commit()
        # The cut is observed after both ENDs; applying only the first
        # transaction must not publish even though the message arrived.
        receive_cut(db, world.capture, world.server, lsn=900)
        assert runtime.apply_capture(db, world.capture.id, max_transactions=1) == 1
        db.commit()
        run = db.get(IntegratedReplicaRun, member.run.id)
        assert run.status == "catching_up"
        assert db.get(IntegratedReplica, member.asset.id).active_run_id is None
        assert runtime.apply_capture(db, world.capture.id) == 1
        db.commit()
        run = db.get(IntegratedReplicaRun, member.run.id)
        assert run.status == "active"
        assert db.get(IntegratedReplica, member.asset.id).active_run_id == run.id
        assert physical_rows(db, member)[0]["note"] == "v2"
    finally:
        drop_all(db, world)


def test_active_heartbeat_apply_never_advances_coverage(db, world):
    member = world.members[0]
    ingest.ingest_batch(db, world.capture.id, [snap_record(world, snap_row(1),
                                                           table="orders")])
    complete_snapshot(db, world, member)
    ingest.ingest_batch(db, world.capture.id, [
        change_record(world, table="orders", xid=100, lsn=10, ordinal=1, op="u",
                      after=snap_row(1, label="v1")),
        end_record(world, xid=100, commit_lsn=500, count=1)])
    db.commit()
    receive_cut(db, world.capture, world.server, lsn=900)
    runtime.apply_capture(db, world.capture.id)
    db.commit()
    run = db.get(IntegratedReplicaRun, member.run.id)
    replica = db.get(IntegratedReplica, member.asset.id)
    published_coverage = replica.coverage_confirmed_at
    assert published_coverage is not None
    assert run.status == "active"
    # 纯心跳 + apply：活跃性更新，coverage 绝不凭接收时间前进
    for _ in range(2):
        ingest.ingest_batch(db, world.capture.id, [heartbeat(world, {"lsn": 900})])
        db.commit()
        assert runtime.apply_capture(db, world.capture.id) == 0
        db.commit()
    replica = db.get(IntegratedReplica, member.asset.id)
    assert replica.coverage_confirmed_at == published_coverage
    assert replica.status == "ready"


# ---------------------------------------------------------------------------
# cleanup_relations：只清终态且不再被引用的 run
# ---------------------------------------------------------------------------

def test_cleanup_relations_drops_retired_tables_and_receipts(db, admin_user):
    world = make_cohort(db, admin_user)
    try:
        member = world.members[0]
        ingest.ingest_batch(db, world.capture.id, [snap_record(world, snap_row(1),
                                                               table="orders")])
        complete_snapshot(db, world, member)
        ingest.ingest_batch(db, world.capture.id, [
            change_record(world, table="orders", xid=100, lsn=10, ordinal=1, op="u",
                          after=snap_row(1, label="v1")),
            end_record(world, xid=100, commit_lsn=500, count=1)])
        db.commit()
        receive_cut(db, world.capture, world.server, lsn=900)
        runtime.apply_capture(db, world.capture.id)
        db.commit()
        first = db.get(IntegratedReplicaRun, member.run.id)
        assert first.status == "active"
        assert len(db.scalars(sa.select(IntegratedReplicaSnapshotBatch).where(
            IntegratedReplicaSnapshotBatch.run_id == first.id)).all()) == 1

        # 重建换代：新 run 发布后旧 run retired，其私表/快照回执可清理
        control.request_rebuild(db, member.asset)
        replacement = runtime.prepare_capture(db, world.connection.id)
        db.commit()
        new_member = bind_members(db, replacement)[0]
        world.members.append(new_member)
        second = SimpleNamespace(capture=replacement, server=server_of(replacement))
        ingest.ingest_batch(db, replacement.id, [snap_record(second, snap_row(9),
                                                            table="orders")])
        complete_snapshot(db, second, new_member)
        ingest.ingest_batch(db, replacement.id, [
            change_record(second, table="orders", xid=300, lsn=40, ordinal=1, op="c",
                          after=snap_row(2)),
            end_record(second, xid=300, commit_lsn=900, count=1)])
        db.commit()
        receive_cut(db, replacement, second.server, lsn=901)
        runtime.apply_capture(db, replacement.id)
        db.commit()
        assert db.get(IntegratedReplicaRun, first.id).status == "retired"

        cleaned = runtime.cleanup_relations(db)
        db.commit()
        assert cleaned >= 1
        assert db.get(IntegratedReplicaRun, first.id).cleaned_at is not None
        assert not sa.inspect(db.connection()).has_table(
            store.replica_table_name(first.id))
        assert db.scalars(sa.select(IntegratedReplicaSnapshotBatch).where(
            IntegratedReplicaSnapshotBatch.run_id == first.id)).all() == []
        # 活跃 run 的表与回执原样保留；重复清理幂等
        assert sa.inspect(db.connection()).has_table(new_member.schema.table_name)
        assert runtime.cleanup_relations(db) == 0
    finally:
        drop_all(db, world)
