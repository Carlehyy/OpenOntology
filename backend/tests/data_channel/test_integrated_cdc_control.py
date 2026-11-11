"""CDC 控制面生命周期聚焦测试：reconcile、运行时回报、轮换/重定向/注销守卫、
状态读模型与 RBAC。

只覆盖控制面（SQLite 复现）；真实 PostgreSQL/MySQL 复制链路属独立 E2E。
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.data_channel.connections import router as connections_router
from app.data_channel.connections.models import Connection
from app.data_channel.integrated import cdc_control, service
from app.data_channel.integrated import cdc_port
from app.data_channel.integrated.models import (
    IntegratedAsset,
    IntegratedCdcSource,
    IntegratedCdcSubscription,
)
from app.data_channel.datasets.models import Dataset
from app.main import app
from app.ontologies.projects.models import OntologyProject
from app.shared.encryption import decrypt
from tests.conftest import TestSession

CONFIG = {"host": "db.local", "port": 5432, "database": "proddb",
          "user": "svc", "password": "plain-secret-123"}
ROTATED = {**CONFIG, "password": "rotated-secret-456"}
REPOINTED = {**CONFIG, "host": "other-db.local"}

MYSQL_POSITION = {"dialect": "mysql", "file": "bin.000042", "pos": 1234, "row": 7}
PG_POSITION = {"dialect": "postgresql", "lsn": 987654321, "data_collection_order": 0}


class FakePort:
    """组合根注入的 ontologies 适配器替身；capture 系列不得被控制面触碰。"""

    def __init__(self):
        self.entries = []

    def subscriptions(self, db):
        return [dict(entry) for entry in self.entries]

    def capture(self, db, event):
        raise AssertionError("control plane must never capture")

    def capture_many(self, db, events):
        raise AssertionError("control plane must never capture")


@pytest.fixture
def port():
    previous = (cdc_port.get_cdc_ontology_port()
                if cdc_port.cdc_ontology_port_ready() else None)
    fake = FakePort()
    cdc_port.set_cdc_ontology_port(fake)
    try:
        yield fake
    finally:
        if previous is not None:
            cdc_port.set_cdc_ontology_port(previous)
        else:
            cdc_port.clear_cdc_ontology_port()


def _encrypted_config(config):
    from app.shared.encryption import encrypt

    return {"_encrypted": encrypt(json.dumps(config))}


def _seed(db, user, *, suffix="a", connection_id=None, config=None):
    connection_id = connection_id or f"conn-{suffix}"
    connection = Connection(
        id=connection_id, name=f"源连接-{suffix}", kind="postgres",
        config=config if config is not None else _encrypted_config(CONFIG))
    dataset = Dataset(id=f"ds-{suffix}", name=f"集成数据集-{suffix}",
                      kind="integrated", source_connection_id=connection_id,
                      source_resource="public.orders")
    asset = IntegratedAsset(
        id=f"asset-{suffix}", dataset_id=dataset.id, connection_id=connection_id,
        name=f"集成资产-{suffix}", schema_name="public", table_name="orders",
        schema_json={"columns": [{"name": "id"}, {"name": "amount"}],
                     "primary_key": ["id"]},
        schema_fingerprint="fp-1", writable=False, created_by=user.id)
    project = OntologyProject(id=f"onto-{suffix}", name=f"本体-{suffix}",
                              domain="测试", created_by=user.id)
    db.add_all([connection, dataset, asset, project])
    db.commit()
    return asset


def _entry(suffix="a", release="rel-1", mapping="map-1", **overrides):
    entry = {
        "ontology_id": f"onto-{suffix}", "release_id": release,
        "mapping_id": mapping, "object_type_id": "ot-order",
        "asset_id": f"asset-{suffix}", "connection_id": f"conn-{suffix}",
        "schema_name": "public", "table_name": "orders",
        "schema_fingerprint": "fp-1", "logical_fingerprint": None,
        "field_map": {"id": "orderId", "amount": "amount"},
        "primary_key_columns": ["id"],
        "contract": {"columns": [{"name": "id"}, {"name": "amount"}],
                     "object_properties": [{"name": "orderId"},
                                           {"name": "amount"}]},
    }
    entry.update(overrides)
    return entry


def _subscriptions(db, **filters) -> list:
    return list(db.scalars(sa.select(IntegratedCdcSubscription).where(
        *[
            getattr(IntegratedCdcSubscription, key) == value
            for key, value in filters.items()
        ])).all())


def _subscription(db, **filters):
    return db.scalars(sa.select(IntegratedCdcSubscription).where(
        *[
            getattr(IntegratedCdcSubscription, key) == value
            for key, value in filters.items()
        ])).one()


def _source(db, connection_id):
    return db.scalars(sa.select(IntegratedCdcSource).where(
        IntegratedCdcSource.connection_id == connection_id)
        .execution_options(populate_existing=True)).first()


def _go_active(db, port, user, *, suffix="a", release="rel-1",
               position=MYSQL_POSITION):
    """reconcile → 运行时激活 → active 回报（含偏移与心跳）的完整走法。"""
    port.entries = [_entry(suffix, release)]
    cdc_control.reconcile(db)
    db.commit()
    source = _source(db, f"conn-{suffix}")
    for row in cdc_control.desired_subscriptions(db, source.connection_id):
        cdc_control.activate_subscription(db, row.id, position=position)
    cdc_control.report_source(db, source.connection_id,
                              generation=source.generation,
                              status="active", position=position)
    db.commit()
    db.refresh(source)
    return source


# ── 端口 fail-closed ────────────────────────────────────────────────

def test_cdc_port_is_fail_closed_until_composition_injects(db, admin_user):
    cdc_port.clear_cdc_ontology_port()
    with pytest.raises(cdc_port.CdcOntologyPortError):
        cdc_port.get_cdc_ontology_port()
    with pytest.raises(cdc_port.CdcOntologyPortError):
        cdc_control.reconcile(db)
    with pytest.raises(cdc_port.CdcOntologyPortError):
        cdc_port.set_cdc_ontology_port(None)
    assert not cdc_port.cdc_ontology_port_ready()
    cdc_port.set_cdc_ontology_port(FakePort())
    assert cdc_port.cdc_ontology_port_ready()
    cdc_port.clear_cdc_ontology_port()


# ── reconcile 基本契约 ──────────────────────────────────────────────

def test_reconcile_creates_starting_rows_without_commit_or_source_io(
        db, admin_user, port, monkeypatch):
    asset = _seed(db, admin_user)
    port.entries = [_entry()]

    def _no_engine(*args, **kwargs):
        raise AssertionError("reconcile must not touch any source engine")

    monkeypatch.setattr(sa, "create_engine", _no_engine)
    summary = cdc_control.reconcile(db)

    assert summary["created"] and summary["sources_created"] == ["conn-a"]
    assert summary["desired"] == 1 and not summary["skipped"]
    row = _subscription(db, ontology_id="onto-a")
    assert row.status == "starting"
    assert row.activation_position is None and row.activated_at is None
    assert row.field_map == {"id": "orderId", "amount": "amount"}
    assert row.primary_key_columns == ["id"]
    assert row.contract["object_properties"] == [{"name": "orderId"},
                                                 {"name": "amount"}]
    assert row.source_generation == 0
    source = _source(db, "conn-a")
    assert source.status == "starting" and source.generation == 0
    # 物理身份摘要是单向 64-hex，不含任何配置明文/密文片段。
    assert len(source.identity_fingerprint) == 64
    for secret in ("db.local", "proddb", "svc", CONFIG["password"]):
        assert secret not in source.identity_fingerprint

    # 未提交：另一会话必须看不到任何控制行（reconcile 绝不自行 commit）。
    other = TestSession()
    try:
        assert other.scalars(sa.select(IntegratedCdcSource)).first() is None
        assert other.scalars(sa.select(IntegratedCdcSubscription)).first() is None
    finally:
        other.close()
    db.commit()
    other = TestSession()
    try:
        assert other.scalars(sa.select(IntegratedCdcSubscription)).one() is not None
    finally:
        other.close()


def test_reconcile_is_idempotent_and_retains_offsets_across_include_change(
        db, admin_user, port):
    _seed(db, admin_user, suffix="a")
    _seed(db, admin_user, suffix="b")
    source = _go_active(db, port, admin_user, suffix="a")
    anchor = _subscription(db, ontology_id="onto-a")
    assert anchor.status == "active"
    assert anchor.activation_position == MYSQL_POSITION
    assert anchor.activated_at is not None
    assert source.last_position == MYSQL_POSITION

    # 稳定期重复 reconcile：零动作，前沿/偏移/覆盖起点原样保留。
    summary = cdc_control.reconcile(db)
    db.commit()
    assert not any(summary[key] for key in
                   ("created", "replanned", "reactivated", "superseded",
                    "removed", "epochs"))
    db.refresh(anchor)
    assert anchor.activation_position == MYSQL_POSITION
    assert anchor.status == "active" and anchor.activated_at is not None

    # 表集合扩张（连接 b 上新订阅）：不影响既有订阅的前沿与源偏移。
    port.entries.append(_entry("b"))
    summary = cdc_control.reconcile(db)
    db.commit()
    assert summary["created"] and summary["sources_created"] == ["conn-b"]
    db.refresh(anchor)
    source_a = _source(db, "conn-a")
    assert source_a.generation == source.generation
    assert source_a.last_position == MYSQL_POSITION
    assert anchor.activation_position == MYSQL_POSITION
    assert anchor.status == "active"
    assert [row.asset_id for row in cdc_control.desired_subscriptions(db, "conn-a")] \
        == ["asset-a"]


def test_reconcile_validates_and_skips_dead_references(db, admin_user, port):
    _seed(db, admin_user, suffix="a")
    port.entries = [
        _entry("a"),
        _entry("z"),                     # 资产/连接/本体均不存在
        {"ontology_id": "", "release_id": "r"},  # 结构非法
    ]
    with pytest.raises(ValueError):
        cdc_control.reconcile(db)
    port.entries[2] = _entry("a", mapping="map-dup")
    port.entries[1] = _entry("a", mapping="map-dup")  # 重复键
    with pytest.raises(ValueError):
        cdc_control.reconcile(db)
    db.rollback()

    # 引用已被注销的资产：跳过并如实计数，不陷死循环。
    port.entries = [_entry("a"), _entry("z")]
    summary = cdc_control.reconcile(db)
    db.commit()
    assert summary["desired"] == 1
    assert summary["skipped"] == [{
        "ontology_id": "onto-z", "release_id": "rel-1", "mapping_id": "map-1",
        "asset_id": "asset-z", "connection_id": "conn-z",
        "reason": "missing_asset"}]


# ── 发布切换 / 停流确认 / 回滚 ─────────────────────────────────────

def test_release_switch_supersede_stop_ack_and_collection(db, admin_user, port):
    _seed(db, admin_user)
    _go_active(db, port, admin_user, suffix="a", release="rel-1")

    # promote：rel-2 上线，rel-1 失去期望 → superseded（等待停流确认）。
    port.entries = [_entry("a", release="rel-2", mapping="map-2")]
    summary = cdc_control.reconcile(db)
    db.commit()
    old = _subscription(db, release_id="rel-1")
    new = _subscription(db, release_id="rel-2")
    assert old.status == "superseded" and summary["superseded"] == [old.id]
    assert new.status == "starting"

    # 停流确认只触碰 superseded 行，绝不误停新期望。
    assert cdc_control.confirm_subscriptions_stopped(db, "conn-a") == 1
    db.commit()
    db.refresh(old)
    db.refresh(new)
    assert old.status == "stopped" and new.status == "starting"

    # 确认后的旧行是纯控制行：下一拍回收，旧发布订阅不会永久滞留。
    summary = cdc_control.reconcile(db)
    db.commit()
    assert summary["removed"] == [old.id]
    assert _subscriptions(db, release_id="rel-1") == []


def test_confirm_subscriptions_stopped_never_touches_desired_rows(
        db, admin_user, port):
    _seed(db, admin_user)
    port.entries = [_entry("a", release="rel-1"), _entry("a", release="rel-2",
                                                         mapping="map-2")]
    cdc_control.reconcile(db)
    db.commit()
    superseded = _subscription(db, release_id="rel-1")
    superseded.status = "superseded"
    db.commit()

    # 传入混合 id 列表：只有 superseded 行被确认；starting 行原样。
    desired = _subscription(db, release_id="rel-2")
    assert cdc_control.confirm_subscriptions_stopped(
        db, "conn-a", [desired.id, superseded.id]) == 1
    db.commit()
    db.refresh(desired)
    db.refresh(superseded)
    assert desired.status == "starting"
    assert superseded.status == "stopped"


def test_rollback_reactivates_as_explicit_new_coverage(db, admin_user, port):
    _seed(db, admin_user)
    _go_active(db, port, admin_user, suffix="a", release="rel-1")
    old = _subscription(db, release_id="rel-1")
    assert old.activation_position == MYSQL_POSITION

    # promote 后、停流确认前回滚：复活必须是显式新覆盖（重锚），不续旧前沿。
    port.entries = [_entry("a", release="rel-2", mapping="map-2")]
    cdc_control.reconcile(db)
    db.commit()
    assert old.status == "superseded"
    port.entries = [_entry("a", release="rel-1")]
    summary = cdc_control.reconcile(db)
    db.commit()
    db.refresh(old)
    assert old.id in summary["reactivated"]
    assert old.status == "starting"
    assert old.activation_position is None and old.activated_at is None


# ── 停止/清理确认/新代际 ────────────────────────────────────────────

def test_new_generation_requires_cleanup_confirmation(db, admin_user, port):
    _seed(db, admin_user)
    source = _go_active(db, port, admin_user, suffix="a", release="rel-1")
    generation = source.generation
    row = _subscription(db, release_id="rel-1")

    # 发布删除 → 订阅 superseded；运行时停流但尚未确认清理。
    port.entries = []
    cdc_control.reconcile(db)
    db.commit()
    assert row.status == "superseded"
    cdc_control.report_source(db, "conn-a", generation=generation,
                              status="stopped")
    db.commit()
    source = _source(db, "conn-a")
    assert source.status == "stopped"
    assert source.cleanup_completed_at is None  # 未确认清理 → 非休眠

    # 复燃：不升代、不放行激活，源进入 cleanup 列表等待清理确认。
    port.entries = [_entry("a", release="rel-1")]
    summary = cdc_control.reconcile(db)
    db.commit()
    db.refresh(row)
    source = _source(db, "conn-a")
    assert source.generation == generation
    assert source.status == "stopped"
    assert summary["cleanup_pending"] == ["conn-a"]
    assert source not in cdc_control.desired_sources(db)
    assert source in cdc_control.cleanup_sources(db)
    refused = cdc_control.activate_subscription(
        db, row.id, position=PG_POSITION)
    db.commit()
    assert refused.status == "starting"
    assert refused.activation_position is None

    # 确认清理（stopped + cleanup_completed）→ 下一拍显式新代际。
    cdc_control.report_source(db, "conn-a", generation=generation,
                              status="stopped", cleanup_completed=True)
    db.commit()
    summary = cdc_control.reconcile(db)
    db.commit()
    source = _source(db, "conn-a")
    assert source.generation == generation + 1
    assert source.last_position is None
    assert source.cleanup_completed_at is None
    assert source.status == "starting"
    assert summary["epochs"] == [{"connection_id": "conn-a",
                                  "generation": generation + 1}]
    db.refresh(row)
    assert row.status == "starting"
    assert row.source_generation == generation + 1
    assert row.activation_position is None
    # 新代际下激活放行。
    anchored = cdc_control.activate_subscription(db, row.id, position=PG_POSITION)
    db.commit()
    assert anchored.status == "active"
    assert anchored.activation_position == PG_POSITION


def test_report_source_fences_stale_generation_completely(db, admin_user, port):
    _seed(db, admin_user)
    _go_active(db, port, admin_user, suffix="a", release="rel-1")
    source = _source(db, "conn-a")
    assert source.heartbeat_at is not None
    heartbeat_before = source.heartbeat_at
    position_before = source.last_position

    # 旧代回报：整体 no-op——不刷心跳、不改状态、不动偏移。
    result = cdc_control.report_source(
        db, "conn-a", generation=source.generation - 1, status="error",
        error_code="SLOT_MISSING", position=PG_POSITION)
    db.commit()
    assert result is None
    source = _source(db, "conn-a")
    assert source.status == "active"
    assert source.last_error_code is None
    assert source.last_position == position_before
    assert source.heartbeat_at == heartbeat_before

    # 未知回报 id / 非法状态词表。
    assert cdc_control.report_source(db, "conn-nope", generation=0,
                                     status="active") is None
    with pytest.raises(ValueError):
        cdc_control.report_source(db, "conn-a",
                                  generation=source.generation,
                                  status="running")

    # 当代回报：心跳/状态/错误/事件时间落地。
    moment = datetime.now(timezone.utc).replace(microsecond=0)
    result = cdc_control.report_source(
        db, "conn-a", generation=source.generation, status="gap",
        error_code="WAL_GAP", position=PG_POSITION, last_event_at=moment)
    db.commit()
    assert result is not None
    source = _source(db, "conn-a")
    assert source.status == "gap" and source.last_error_code == "WAL_GAP"
    assert source.last_position == PG_POSITION
    assert source.last_event_at is not None
    assert source.heartbeat_at >= heartbeat_before


def test_activate_subscription_refuses_off_generation_or_paused_source(
        db, admin_user, port):
    _seed(db, admin_user)
    port.entries = [_entry("a", release="rel-1")]
    cdc_control.reconcile(db)
    db.commit()
    row = _subscription(db, release_id="rel-1")
    source = _source(db, "conn-a")

    # 代际不一致：拒绝锚定（原样返回 starting）。
    row.source_generation = source.generation + 5
    db.commit()
    refused = cdc_control.activate_subscription(db, row.id, position=PG_POSITION)
    db.commit()
    assert refused.status == "starting"
    assert refused.activation_position is None

    # paused 源（凭据轮换等待重测）：拒绝锚定。
    row.source_generation = source.generation
    source.status = "paused"
    db.commit()
    refused = cdc_control.activate_subscription(db, row.id, position=PG_POSITION)
    db.commit()
    assert refused.status == "starting"

    # 恢复 starting 后锚定成功且幂等（二次调用不重锚 active 行）。
    source.status = "starting"
    db.commit()
    anchored = cdc_control.activate_subscription(db, row.id, position=PG_POSITION)
    db.commit()
    assert anchored.status == "active"
    again = cdc_control.activate_subscription(db, row.id, position=MYSQL_POSITION)
    db.commit()
    assert again.status == "active"
    assert again.activation_position == PG_POSITION  # 未被第二次调用改写


# ── 凭据轮换 / 物理重定向 / 受控注销（HTTP） ────────────────────────

def _route_db_to(db):
    def override():
        yield db

    app.dependency_overrides[connections_router.get_db] = override


def test_credential_rotation_pauses_same_generation_until_retest(
        client, db, auth_headers, admin_user, port, monkeypatch):
    asset = _seed(db, admin_user)
    source = _go_active(db, port, admin_user, suffix="a", release="rel-1")
    generation = source.generation
    row = _subscription(db, release_id="rel-1")
    assert row.status == "active"

    _route_db_to(db)
    try:
        rotated = client.patch(
            f"/api/v2/connections/conn-a/config", headers=auth_headers,
            json={"config": {"password": ROTATED["password"]}})
        assert rotated.status_code == 200, rotated.text
        source = _source(db, "conn-a")
        assert source.status == "paused"          # 暂停等待重测
        assert source.generation == generation    # 仅凭据：同代
        assert source.last_position == MYSQL_POSITION  # 偏移保留
        assert source not in cdc_control.desired_sources(db)
        # 激活被拒绝；既有 active 行保持 active（流暂停，不改判历史状态）。

        # 重测失败：保持暂停。
        class _FailingConnector:
            @staticmethod
            def test_connection():
                return False

        monkeypatch.setattr(connections_router, "get_connector",
                            lambda kind, config: _FailingConnector())
        failed = client.post("/api/v2/connections/conn-a/test", headers=auth_headers)
        assert failed.status_code == 200 and failed.json()["success"] is False
        assert _source(db, "conn-a").status == "paused"

        # 重测成功：同代恢复 starting，偏移仍保留。
        class _PassingConnector:
            @staticmethod
            def test_connection():
                return True

        monkeypatch.setattr(connections_router, "get_connector",
                            lambda kind, config: _PassingConnector())
        passed = client.post("/api/v2/connections/conn-a/test", headers=auth_headers)
        assert passed.status_code == 200 and passed.json()["success"] is True
        source = _source(db, "conn-a")
        assert source.status == "starting"
        assert source.generation == generation
        assert source.last_position == MYSQL_POSITION

        # 轮换后的落库密文可解出合并结果，且任何响应不回显配置。
        assert "rotated-secret-456" not in rotated.text
        connection = db.scalars(sa.select(Connection).where(
            Connection.id == "conn-a").execution_options(
                populate_existing=True)).one()
        assert json.loads(decrypt(connection.config["_encrypted"])) == ROTATED
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)


def test_physical_repoint_blocked_until_dormant_then_new_generation(
        client, db, auth_headers, admin_user, port):
    _seed(db, admin_user)
    _go_active(db, port, admin_user, suffix="a", release="rel-1")

    _route_db_to(db)
    try:
        blocked = client.patch(
            "/api/v2/connections/conn-a/config", headers=auth_headers,
            json={"config": {"host": REPOINTED["host"]}})
        assert blocked.status_code == 409, blocked.text
        detail = blocked.json()["detail"]
        assert detail["code"] == "CDC_REPOINT_BLOCKED"
        assert detail["cdc"]["status"] == "active"
        assert detail["cdc"]["cleanup_completed_at"] is None
        # 拒绝后连接配置未被覆盖（旧端点凭据仍在，槽仍可清理）。
        connection = db.scalars(sa.select(Connection).where(
            Connection.id == "conn-a").execution_options(
                populate_existing=True)).one()
        assert json.loads(decrypt(connection.config["_encrypted"])) == CONFIG

        # 解除订阅并让运行时完成“停止 + 清理确认”（休眠）。
        port.entries = []
        cdc_control.reconcile(db)
        db.commit()
        row = _subscription(db, release_id="rel-1")
        assert row.status == "superseded"
        cdc_control.confirm_subscriptions_stopped(db, "conn-a")
        source = _source(db, "conn-a")
        cdc_control.report_source(db, "conn-a", generation=source.generation,
                                  status="stopped", cleanup_completed=True)
        db.commit()
        source = _source(db, "conn-a")
        old_identity = source.identity_fingerprint
        assert source.cleanup_completed_at is not None

        allowed = client.patch(
            "/api/v2/connections/conn-a/config", headers=auth_headers,
            json={"config": {"host": REPOINTED["host"]}})
        assert allowed.status_code == 200, allowed.text
        source = _source(db, "conn-a")
        assert source.generation == 1              # 显式新代际
        assert source.last_position is None        # 旧偏移不随新端点续用
        assert source.identity_fingerprint != old_identity
        assert source.status == "stopped"          # 休眠保持，复燃交给 reconcile
        assert source.cleanup_completed_at is not None
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)


def test_connection_delete_never_leaves_a_slot_behind(
        client, db, auth_headers, admin_user, port):
    # 资产已注销、仅剩 CDC 控制行的连接：删除裁决完全落在 CDC 守卫上。
    asset = _seed(db, admin_user)
    source = IntegratedCdcSource(
        connection_id="conn-a", generation=0, status="active")
    db.add(source)
    db.commit()
    # 无历史/无订阅的资产可正常注销（控制面零源端 IO 路径）。
    service.decommission_asset(db, admin_user, asset.id)

    _route_db_to(db)
    try:
        active = client.delete("/api/v2/connections/conn-a",
                               headers=auth_headers)
        assert active.status_code == 409
        detail = active.json()["detail"]
        assert detail["code"] == "CONNECTION_CDC_ACTIVE"
        assert detail["cdc"]["status"] == "active"
        # 停止但未确认清理：同样是阻塞项（不得留下无人清理的槽）。
        source = _source(db, "conn-a")
        source.status = "stopped"
        db.commit()
        unconfirmed = client.delete("/api/v2/connections/conn-a",
                                    headers=auth_headers)
        assert unconfirmed.status_code == 409
        assert unconfirmed.json()["detail"]["code"] == "CONNECTION_CDC_ACTIVE"

        # 确认清理后：安全回收控制行，连接可注销。
        source = _source(db, "conn-a")
        cdc_control.report_source(db, "conn-a", generation=source.generation,
                                  status="stopped", cleanup_completed=True)
        db.commit()
        assert client.delete("/api/v2/connections/conn-a",
                             headers=auth_headers).status_code == 204
        assert _source(db, "conn-a") is None
        assert db.get(Connection, "conn-a") is None
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)


def test_asset_decommission_waits_for_stop_ack_then_cleans_control_rows(
        db, admin_user, port):
    asset = _seed(db, admin_user)
    _go_active(db, port, admin_user, suffix="a", release="rel-1")

    # active 订阅：结构化 409（含 cdc 明细），资产保留。
    with pytest.raises(Exception) as exc:
        service.decommission_asset(db, admin_user, asset.id)
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["code"] == "ASSET_DECOMMISSION_BLOCKED"
    assert detail["cdc"]["code"] == "ASSET_CDC_ACTIVE"
    assert detail["cdc"]["total"] == 1
    assert detail["cdc"]["subscriptions"][0]["release_id"] == "rel-1"
    assert db.get(IntegratedAsset, asset.id) is not None

    # superseded（停流未被观测）：仍阻塞。
    port.entries = []
    cdc_control.reconcile(db)
    db.commit()
    with pytest.raises(Exception) as exc:
        service.decommission_asset(db, admin_user, asset.id)
    assert exc.value.status_code == 409

    # 停流确认后：注销成功，控制行随注销回收，不阻塞也不残留。
    cdc_control.confirm_subscriptions_stopped(db, "conn-a")
    db.commit()
    result = service.decommission_asset(db, admin_user, asset.id)
    assert result["status"] == "decommissioned"
    assert db.get(IntegratedAsset, asset.id) is None
    assert _subscriptions(db, asset_id=asset.id) == []


# ── 状态读模型（前端契约）与 RBAC ──────────────────────────────────

def test_asset_status_truthful_across_disabled_starting_active_stale(
        db, admin_user, port):
    asset = _seed(db, admin_user)

    # 禁用：enabled False / stopped / 不健康 / 无订阅 / 无覆盖。
    status = cdc_control.asset_status(db, asset)
    assert status == {
        "asset_id": asset.id, "enabled": False, "status": "stopped",
        "healthy": False, "last_event_at": None, "last_error_code": None,
        "subscriptions": [], "coverage_started_at": None}

    # starting（未锚定）：不算覆盖起点，不健康。
    port.entries = [_entry("a", release="rel-1")]
    cdc_control.reconcile(db)
    db.commit()
    status = cdc_control.asset_status(db, asset)
    assert status["enabled"] is True and status["status"] == "starting"
    assert status["healthy"] is False
    assert status["coverage_started_at"] is None
    assert [item["status"] for item in status["subscriptions"]] == ["starting"]

    # active + 新鲜心跳：健康，覆盖起点 = 真实激活时刻。
    _go_active(db, port, admin_user, suffix="a", release="rel-1")
    source = _source(db, "conn-a")
    row = _subscription(db, release_id="rel-1")
    status = cdc_control.asset_status(db, asset)
    assert status["healthy"] is True
    assert status["last_error_code"] is None
    assert status["coverage_started_at"] is not None
    assert status["coverage_started_at"].replace(tzinfo=None) == \
        row.activated_at.replace(tzinfo=None)
    assert {item["release_id"] for item in status["subscriptions"]} == {"rel-1"}

    # 心跳过期：即使无任何源事件也必须如实不健康。
    source = _source(db, "conn-a")
    source.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    db.commit()
    assert cdc_control.asset_status(db, asset)["healthy"] is False

    # 心跳来自未来（超出容许漂移）：同样不健康。
    source = _source(db, "conn-a")
    source.heartbeat_at = datetime.now(timezone.utc) + timedelta(seconds=120)
    db.commit()
    assert cdc_control.asset_status(db, asset)["healthy"] is False

    # 部分订阅未锚定 / 有错误：不健康。
    source = _source(db, "conn-a")
    source.heartbeat_at = datetime.now(timezone.utc)
    db.commit()
    port.entries.append(_entry("a", release="rel-2", mapping="map-2"))
    cdc_control.reconcile(db)
    db.commit()
    assert cdc_control.asset_status(db, asset)["healthy"] is False
    source = _source(db, "conn-a")
    source.last_error_code = "SLOT_MISSING"
    db.commit()
    status = cdc_control.asset_status(db, asset)
    assert status["healthy"] is False and status["last_error_code"] == "SLOT_MISSING"

    # 失去期望后：superseded/stopped 历史行不参与启用与列表。
    port.entries = []
    cdc_control.reconcile(db)
    cdc_control.confirm_subscriptions_stopped(db, "conn-a")
    db.commit()
    status = cdc_control.asset_status(db, asset)
    assert status["enabled"] is False and status["subscriptions"] == []
    # 响应不含任何配置/凭据/字段映射/偏移。
    assert not (set(status) & {"config", "field_map", "contract", "last_position"})


def test_cdc_status_http_contract_and_rbac(client, db, auth_headers, admin_user, port):
    from app.auth.models import RoleMenuPermission
    from app.models.user import User

    asset = _seed(db, admin_user)
    _go_active(db, port, admin_user, suffix="a", release="rel-1")

    # admin 读取：契约字段精确冻结，无任何原始值/配置泄漏。
    response = client.get(f"/api/v2/integrated-datasets/{asset.id}/cdc",
                          headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"asset_id", "enabled", "status", "healthy",
                         "last_event_at", "last_error_code", "subscriptions",
                         "coverage_started_at"}
    assert body["enabled"] is True and body["healthy"] is True
    assert set(body["subscriptions"][0]) == {
        "ontology_id", "release_id", "mapping_id", "object_type_id", "status",
        "last_error_code"}
    assert body["coverage_started_at"] is not None
    serialized = response.text
    for secret in (CONFIG["password"], CONFIG["host"], "field_map", "last_position",
                   "orderId"):
        assert secret not in serialized

    # editor / viewer（默认菜单含 data.structured）可读。
    admin = db.query(User).filter(User.username == "admin").one()
    for role in ("editor", "viewer"):
        admin.role = role
        db.commit()
        assert client.get(f"/api/v2/integrated-datasets/{asset.id}/cdc",
                          headers=auth_headers).status_code == 200

    # custom（默认菜单仅 overview）：挂载层 menu_guard("data") 结构化 403。
    admin.role = "custom"
    db.commit()
    denied = client.get(f"/api/v2/integrated-datasets/{asset.id}/cdc",
                        headers=auth_headers)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "MENU_ACCESS_DENIED"
    assert denied.json()["detail"]["menu_key"] == "data"

    # 授予 data.structured（归一化带出 data 父键）后恢复可读：挂载菜单门与
    # 端点 authorize 都是菜单口径而非角色名口径。
    db.add(RoleMenuPermission(role="custom", menu_keys=["data.structured"]))
    db.commit()
    assert client.get(f"/api/v2/integrated-datasets/{asset.id}/cdc",
                      headers=auth_headers).status_code == 200
    db.query(RoleMenuPermission).filter(
        RoleMenuPermission.role == "custom").delete()

    # 未认证拒绝（本仓库 get_current_user 对缺失凭据统一 403）；未知资产 404。
    admin.role = "admin"
    db.commit()
    assert client.get(f"/api/v2/integrated-datasets/{asset.id}/cdc"
                      ).status_code == 403
    assert client.get("/api/v2/integrated-datasets/no-such-asset/cdc",
                      headers=auth_headers).status_code == 404
