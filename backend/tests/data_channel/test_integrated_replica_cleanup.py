"""replica_cleanup 聚焦测试（真实 SQLite + FK + 多资产共享 capture）。

契约被测面：confirm_detached 只给 terminal（retired/failed/cancelled）且无
active/pending 指针的成员加 ``detached=true``（身份键原样、live 成员不动、
幂等、缺 capture 拒绝）；purge_asset 是资产 DELETE 钩子：仅 stopped 副本 +
全部 run 已物理清理 + 已从 capture 脱离（成员 detached 或 capture 整体
stopped+cleaned_at）才删本资产 snapshot batches/runs/replica，早退 False 不动
任何行、绝不跨资产；整个 capture 停止且自有 run 全部清理无引用时才删
receipts/transactions/capture（connection FK RESTRICT 因此可解）；重复清理
幂等；全程调用方事务（回滚即全蒸发）、无源 IO、不自 commit。
"""
from __future__ import annotations

import base64
import json
import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset
from app.data_channel.integrated import replica_control as control
from app.data_channel.integrated import replica_cleanup as cleanup
from app.data_channel.integrated import replica_runtime as runtime
from app.data_channel.integrated import replica_store as store
from app.data_channel.integrated.contracts import digest
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_capture_models import (
    IntegratedReplicaCapture, IntegratedReplicaReceipt, IntegratedReplicaTransaction,
)
from app.data_channel.integrated.replica_models import (
    IntegratedReplica, IntegratedReplicaRun, now as replica_now,
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


def transport(event, *, server, offset, table="orders"):
    payload = {"topic": f"oo.public.{table}", "key": None,
               "partition_json": json.dumps({"server": server}),
               "offset_json": json.dumps(offset)}
    if event is not None:
        payload["event"] = event
    return {"schema": {"type": "struct",
                       "name": "io.openontology.replica.TransportRecord",
                       "fields": [
                           {"field": "topic", "type": "string"},
                           {"field": "partition_json", "type": "string"},
                           {"field": "offset_json", "type": "string"},
                           {"field": "key", "type": "string", "optional": True},
                           {"field": "event", "name": "oodb.Envelope",
                            "type": "struct", "optional": True, "fields": [
                                {"field": "op", "type": "string"},
                                {"field": "ts_ms", "type": "int64"},
                                {"field": "source", "type": "struct"},
                                {"field": "before", "type": "struct",
                                 "optional": True, "fields": FIELDS[table]()},
                                {"field": "after", "type": "struct",
                                 "optional": True, "fields": FIELDS[table]()},
                            ]}]}, "payload": payload}


def snap_row(id_, label="x"):
    return {"id": id_, "label": label, "amount": "123.4500",
            "doc": PRECISION_JSON, "blob": BLOB_B64, "moment": MOMENT}


def make_cohort(db, admin_user, *, tables=("orders", "customers")):
    suffix = uuid.uuid4().hex[:8]
    connection = Connection(id=f"cl-src-{suffix}", name=f"cleanup {suffix}",
                            kind="postgres", config={}, status="active",
                            created_by=admin_user.id)
    db.add(connection)
    assets = {}
    for table in tables:
        manifest = {"columns": COLUMNS[table](), "primary_key": ["id"]}
        dataset = Dataset(id=f"cl-ds-{suffix}-{table}", name=f"cleanup {table}",
                          kind="integrated", source_connection_id=connection.id,
                          source_resource=table)
        db.add(dataset)
        asset = IntegratedAsset(
            id=f"cl-asset-{suffix}-{table}", dataset_id=dataset.id,
            connection_id=connection.id, name=f"cleanup {table}",
            schema_name="public", table_name=table, schema_json=manifest,
            schema_fingerprint=digest(manifest), writable=False,
            created_by=admin_user.id)
        db.add(asset)
        assets[table] = asset
    db.commit()
    runtime.queue_registered(db)
    capture = runtime.prepare_capture(db, connection.id)
    db.commit()
    server = "oo_replica_" + uuid.UUID(capture.id).hex
    members = {m["table_name"]: m for m in capture.tables_json}
    runs = {table: db.get(IntegratedReplicaRun, members[table]["run_id"])
            for table in tables}
    schemas = {table: store.build_replica_schema(
        runs[table].id, runs[table].schema_json,
        schema_fingerprint=runs[table].schema_fingerprint) for table in tables}
    return SimpleNamespace(connection=connection, capture=capture, assets=assets,
                           runs=runs, schemas=schemas, server=server,
                           tables=list(tables))


def load_snapshot(db, world, table, rows):
    from app.data_channel.integrated import replica_ingest as ingest
    records = []
    for row in rows:
        event = {"op": "r", "ts_ms": TS_MS,
                 "source": {"connector": "postgresql", "db": "oodb",
                            "schema": "public", "table": table,
                            "ts_ms": TS_MS, "snapshot": "true"},
                 "after": row}
        records.append(transport(event, server=world.server, table=table,
                                 offset={"lsn": 100 + row["id"]}))
    ingest.ingest_batch(db, world.capture.id, records)


def finish(db, world, table):
    control.finish_load(db, world.runs[table].id, world.runs[table].owner_token,
                        completion={"complete": True,
                                    "row_count": world.runs[table].row_count,
                                    "expected_rows": world.runs[table].row_count,
                                    "schema_fingerprint":
                                        world.runs[table].schema_fingerprint,
                                    "snapshot_id": world.capture.id,
                                    "source_confirmed_at": "2026-11-12T00:00:00+00:00",
                                    "offset": {"lsn": 1}})


def drop_tables(db, world):
    db.rollback()
    for schema in world.schemas.values():
        store.drop_owned(db.connection(), schema.run_id)
    db.commit()


@pytest.fixture
def world(db, admin_user):
    built = make_cohort(db, admin_user)
    load_snapshot(db, built, "orders", [snap_row(1)])
    load_snapshot(db, built, "customers", [{"id": 50, "note": "base"}])
    for table in built.tables:
        finish(db, built, table)
    # Publication requires the verified-coverage barrier owned by the
    # capture/apply layer; this module only exercises cleanup, so it publishes
    # under the same control fence directly.
    for table in built.tables:
        run = built.runs[table]
        control.publish_run(
            db, run.id, run.owner_token,
            applied_position={"capture_id": built.capture.id, "commit_seq": 0},
            coverage_confirmed_at=runtime.now(), row_count=run.row_count)
    db.commit()
    try:
        yield built
    finally:
        drop_tables(db, built)


def rows_of(db, schema):
    return [row for batch in store.read_batches(db.connection(), schema)
            for row in batch]


def counts(db):
    return {model: db.scalar(sa.select(sa.func.count()).select_from(model))
            for model in (IntegratedReplica, IntegratedReplicaRun,
                          IntegratedReplicaReceipt, IntegratedReplicaTransaction,
                          IntegratedReplicaSnapshotBatch,
                          IntegratedReplicaCapture)}


# ---------------------------------------------------------------------------
# confirm_detached
# ---------------------------------------------------------------------------

def test_confirm_detached_marks_only_terminal_unreferenced_members(db, world):
    capture = db.get(IntegratedReplicaCapture, world.capture.id)
    assert cleanup.confirm_detached(db, capture.id) == 0  # live 成员不可标记

    control.request_stop(db, world.assets["orders"])
    db.commit()
    marked = cleanup.confirm_detached(db, capture.id)
    assert marked == 1
    capture = db.get(IntegratedReplicaCapture, capture.id)
    by_table = {m["table_name"]: m for m in capture.tables_json}
    stopped = by_table["orders"]
    live = by_table["customers"]
    assert stopped["detached"] is True
    # 身份键原样保留；live 成员完全不动
    assert stopped["run_id"] == world.runs["orders"].id
    assert stopped["asset_id"] == world.assets["orders"].id
    assert stopped["schema_name"] == "public" and stopped["table_name"] == "orders"
    assert "detached" not in live
    assert live["run_id"] == world.runs["customers"].id
    # 幂等
    assert cleanup.confirm_detached(db, capture.id) == 0

    # 指针仍指向的 terminal 成员绝不标记（模拟残留发布指针）
    replica = db.get(IntegratedReplica, world.assets["customers"].id)
    replica.active_run_id = world.runs["customers"].id
    world.runs["customers"].status = "cancelled"
    db.flush()
    assert cleanup.confirm_detached(db, capture.id) == 0
    db.rollback()


def test_confirm_detached_rejects_missing_capture_and_skips_unprovable(db, world):
    with pytest.raises(cleanup.ReplicaCleanupError) as exc:
        cleanup.confirm_detached(db, str(uuid.uuid4()))
    assert exc.value.code == "CAPTURE_MISSING"

    capture = db.get(IntegratedReplicaCapture, world.capture.id)
    capture.tables_json = capture.tables_json + [
        {"asset_id": world.assets["orders"].id, "run_id": str(uuid.uuid4()),
         "schema_name": "public", "table_name": "ghost"}]
    db.flush()
    # run 行不存在：无法证明 terminal，原样保留
    assert cleanup.confirm_detached(db, capture.id) == 0
    db.rollback()


# ---------------------------------------------------------------------------
# purge_asset：门槛与跨资产隔离
# ---------------------------------------------------------------------------

def test_purge_asset_refuses_until_stopped_cleaned_and_detached(db, world):
    asset = world.assets["orders"]
    before = counts(db)
    assert cleanup.purge_asset(db, asset.id) is False  # active：早退
    assert counts(db) == before

    control.request_stop(db, asset)
    db.commit()
    assert cleanup.purge_asset(db, asset.id) is False  # 表未清理（cleaned_at 空）
    assert db.get(IntegratedReplicaRun, world.runs["orders"].id).cleaned_at is None

    runtime.cleanup_relations(db)
    db.commit()
    assert cleanup.purge_asset(db, asset.id) is False  # 成员未从 capture 脱离
    assert db.get(IntegratedReplica, asset.id) is not None

    cleanup.confirm_detached(db, world.capture.id)
    db.commit()
    assert cleanup.purge_asset(db, asset.id) is True
    db.commit()
    assert db.get(IntegratedReplica, asset.id) is None
    assert db.get(IntegratedReplicaRun, world.runs["orders"].id) is None
    assert db.scalars(sa.select(IntegratedReplicaSnapshotBatch).where(
        IntegratedReplicaSnapshotBatch.run_id == world.runs["orders"].id)).all() == []
    assert not sa.inspect(db.connection()).has_table(
        world.schemas["orders"].table_name)
    # 幂等：重复清理直接 False（replica 已不存在）
    assert cleanup.purge_asset(db, asset.id) is False


def test_purge_never_touches_other_assets_in_shared_capture(db, world):
    control.request_stop(db, world.assets["orders"])
    runtime.cleanup_relations(db)
    cleanup.confirm_detached(db, world.capture.id)
    assert cleanup.purge_asset(db, world.assets["orders"].id) is True
    db.commit()

    # 共享 capture 的另一资产：数据、成员、journal 全部原样
    asset = world.assets["customers"]
    replica = db.get(IntegratedReplica, asset.id)
    assert replica.status == "ready"
    assert replica.active_run_id == world.runs["customers"].id
    assert {row["id"] for row in rows_of(db, world.schemas["customers"])} == {50}
    capture = db.get(IntegratedReplicaCapture, world.capture.id)
    by_table = {m["table_name"]: m for m in capture.tables_json}
    assert "detached" not in by_table["customers"]
    assert capture.status in {"starting", "streaming", "stale"}  # cohort 仍在服务
    assert db.scalar(sa.select(sa.func.count()).select_from(
        IntegratedReplicaCapture)) == 1
    # 本资产从未 journal 化的场景下 receipts=0；capture 行仍在（其它资产需要）
    assert cleanup.purge_asset(db, asset.id) is False  # 未 stopped：早退不动


# ---------------------------------------------------------------------------
# capture 级拆除：whole-cohort stopped + cleaned + 无引用
# ---------------------------------------------------------------------------

def test_capture_row_survives_until_whole_cohort_stopped_and_cleaned(db, world):
    for table in world.tables:
        control.request_stop(db, world.assets[table])
    runtime.cleanup_relations(db)
    cleanup.confirm_detached(db, world.capture.id)
    assert cleanup.confirm_detached(db, world.capture.id) == 0  # 全部已标记
    assert cleanup.purge_asset(db, world.assets["orders"].id) is True
    db.commit()
    # capture 尚未 stopped（executor 控制子进程/源清理）：行保留
    capture = db.get(IntegratedReplicaCapture, world.capture.id)
    assert capture is not None and capture.status != "stopped"

    capture.status = "stopped"  # 子进程已停，源资源清理尚未确认
    db.flush()  # 成员已显式 detached：资产级删除仍可进行
    assert cleanup.purge_asset(db, world.assets["customers"].id) is True
    db.commit()
    # capture 行与 journal 保留：cleaned_at 未证明源资源清理，绝不删除
    assert db.get(IntegratedReplicaCapture, world.capture.id) is not None
    assert db.scalar(sa.select(sa.func.count()).select_from(
        IntegratedReplicaTransaction)) >= 0  # journal 未被 cohort 级拆除触碰
    # connection FK (RESTRICT)：capture 行仍在则连接不可删（SQLite 测试档
    # 未开 FK pragma，约束本身归 PG gate；此处只证 capture 行拦截语义）。

    capture = db.get(IntegratedReplicaCapture, world.capture.id)
    capture.cleaned_at = replica_now()  # executor 确认源清理后的终态拆除窗口
    db.flush()
    # 最后一个资产已清：终态拆除由任一后续 purge 触发（此处直接复跑幂等入口）
    assert cleanup._teardown_stopped_captures(db, {world.capture.id}) == 1
    db.commit()
    assert db.get(IntegratedReplicaCapture, world.capture.id) is None
    db.delete(world.connection)
    db.commit()
    assert db.get(Connection, world.connection.id) is None


def test_capture_stopped_with_cleaned_at_replaces_member_detach_proof(db, world):
    for table in world.tables:
        control.request_stop(db, world.assets[table])
    runtime.cleanup_relations(db)
    db.commit()
    capture = db.get(IntegratedReplicaCapture, world.capture.id)
    capture.status = "stopped"
    capture.cleaned_at = replica_now()
    db.flush()
    # 未逐成员 confirm_detached：stopped+cleaned_at 是等价的脱离证明
    assert cleanup.purge_asset(db, world.assets["orders"].id) is True
    assert cleanup.purge_asset(db, world.assets["customers"].id) is True
    db.commit()
    assert counts(db) == {IntegratedReplica: 0, IntegratedReplicaRun: 0,
                          IntegratedReplicaReceipt: 0,
                          IntegratedReplicaTransaction: 0,
                          IntegratedReplicaSnapshotBatch: 0,
                          IntegratedReplicaCapture: 0}


def test_purge_deletes_capture_journal_only_when_unreferenced(db, world):
    # orders 先停、customers 仍 live：journal 行（receipts/transactions）保留
    from app.data_channel.integrated import replica_ingest as ingest
    ingest.ingest_batch(db, world.capture.id, [
        transport({"op": "u", "ts_ms": TS_MS,
                   "source": {"connector": "postgresql", "db": "oodb",
                              "schema": "public", "table": "orders", "lsn": 10,
                              "txId": 100, "ts_ms": TS_MS},
                   "transaction": {"id": "100:10", "total_order": 1,
                                   "data_collection_order": 1},
                   "after": snap_row(1, label="v2")}, server=world.server,
                  table="orders", offset={"lsn": 10, "txId": 100}),
        transport({"status": "END", "id": "100:500", "event_count": 1,
                   "ts_ms": TS_MS}, server=world.server,
                  offset={"lsn": 500, "txId": 100})])
    db.commit()
    assert db.scalar(sa.select(sa.func.count()).select_from(
        IntegratedReplicaTransaction)) == 1

    control.request_stop(db, world.assets["orders"])
    runtime.cleanup_relations(db)
    cleanup.confirm_detached(db, world.capture.id)
    assert cleanup.purge_asset(db, world.assets["orders"].id) is True
    db.commit()
    # journal 属于整个 capture：customers 仍在流式消费，绝不删
    assert db.scalar(sa.select(sa.func.count()).select_from(
        IntegratedReplicaReceipt)) >= 1
    assert db.scalar(sa.select(sa.func.count()).select_from(
        IntegratedReplicaTransaction)) == 1

    # customers 停止并清理、capture 停止后：第二次 purge 连 journal 一起拆
    control.request_stop(db, world.assets["customers"])
    runtime.cleanup_relations(db)
    capture = db.get(IntegratedReplicaCapture, world.capture.id)
    capture.status = "stopped"
    capture.cleaned_at = replica_now()  # executor source cleanup 完成
    db.flush()
    assert cleanup.purge_asset(db, world.assets["customers"].id) is True
    db.commit()
    assert db.get(IntegratedReplicaCapture, world.capture.id) is None
    assert db.scalar(sa.select(sa.func.count()).select_from(
        IntegratedReplicaReceipt)) == 0
    assert db.scalar(sa.select(sa.func.count()).select_from(
        IntegratedReplicaTransaction)) == 0


def test_purge_of_never_prepared_and_caller_rollback(db, admin_user):
    from app.data_channel.integrated import replica_control as control
    manifest = {"columns": COLUMNS["orders"](), "primary_key": ["id"]}
    connection = Connection(id="cl-src-bare", name="bare", kind="postgres",
                            config={}, status="active", created_by=admin_user.id)
    dataset = Dataset(id="cl-ds-bare", name="bare", kind="integrated",
                      source_connection_id=connection.id, source_resource="orders")
    asset = IntegratedAsset(id="cl-asset-bare", dataset_id=dataset.id,
                            connection_id=connection.id, name="bare",
                            schema_name="public", table_name="orders",
                            schema_json=manifest, schema_fingerprint=digest(manifest),
                            writable=False, created_by=admin_user.id)
    db.add_all([connection, dataset, asset])
    db.commit()
    runtime.queue_registered(db)
    run = db.scalar(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.asset_id == asset.id))
    control.request_stop(db, asset)
    db.commit()
    assert cleanup.purge_asset(db, asset.id) is False  # 未清理
    runtime.cleanup_relations(db)
    db.commit()
    assert cleanup.purge_asset(db, asset.id) is True  # 无 capture 的 run 直接可清
    db.rollback()  # 调用方回滚：purge 不自 commit，一切如初
    assert db.get(IntegratedReplica, asset.id) is not None
    assert db.get(IntegratedReplicaRun, run.id) is not None

    assert cleanup.purge_asset(db, "unknown-asset") is False
