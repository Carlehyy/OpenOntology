"""集成 CDC 执行器/连接器的配置与监督者聚焦测试（不依赖真实源端运行时）。

覆盖：
- application.properties 生成（方言差异、正则转义、Java properties/SmallRye
  转义、密钥文件权限）；
- 激活前沿先落库、后拉子进程的顺序；同代扩表不重置偏移目录；
- 休眠清理只删自有名下资源并确认 cleanup_completed；
- 凭据轮换/暂停保留槽与偏移；换代使用新目录并收尾旧代；
- gap/drift/容量的显式状态回报；租约未获取不拉进程；
- 子进程输出分类不误判正常启动日志。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.data_channel.integrated import cdc_connector as connector
from app.data_channel.integrated import cdc_runtime as runtime
from app.data_channel.integrated.cdc_executor import CdcSupervisor, SourceLease

# ── Java .properties 值解码（实现 Java 规范语义，用于往返断言） ──────


def _decode_properties_value(line: str) -> str:
    raw = line.split("=", 1)[1]
    out: list[str] = []
    index = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "f": "\f"}
    while index < len(raw):
        char = raw[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        nxt = raw[index + 1]
        if nxt == "u":
            out.append(chr(int(raw[index + 2:index + 6], 16)))
            index += 6
        else:
            out.append(escapes.get(nxt, nxt))
            index += 2
    return "".join(out)


def _parse_properties(text: str) -> dict[str, str]:
    parsed = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed[key] = _decode_properties_value(line)
        assert parsed[key] is not None
    return parsed


def _subscription(cid="c1", sid="s1", *, schema="public", table="orders",
                  status="starting"):
    return SimpleNamespace(
        id=sid, connection_id=cid, status=status, schema_name=schema,
        table_name=table, field_map={"id": "orderId", "amount": "amountCents"},
        primary_key_columns=["id"], schema_fingerprint="f" * 64)


# ── 配置生成 ──────────────────────────────────────────────────────────

TRICKY_PASSWORD = ' p\\a$s{var}\n中:='


def test_properties_mysql_shape_and_escaping(tmp_path):
    connection = SimpleNamespace(kind="mysql", config={
        "host": "src.example", "port": 3306, "database": "shop",
        "user": "cdc_user", "password": TRICKY_PASSWORD})
    names = connector.runtime_names("c1", 0)
    text = connector.build_properties(
        connection=connection, subscriptions=[_subscription(schema="shop")],
        secret_b64="c2VjcmV0", sink_url="http://127.0.0.1:47000/cdc/c1/0",
        quarkus_port=21024, state_dir=str(tmp_path), names=names)
    assert text.isascii()
    parsed = _parse_properties(text)
    assert parsed["quarkus.http.host"] == "127.0.0.1"
    assert parsed["quarkus.http.port"] == "21024"
    assert parsed["debezium.sink.type"] == "http"
    assert parsed["debezium.sink.http.batch.enabled"] == "false"
    assert parsed["debezium.sink.http.authentication.type"] == "standard-webhooks"
    assert parsed["debezium.sink.http.authentication.webhook.secret"] == "c2VjcmV0"
    assert parsed["debezium.format.value"] == "json"
    assert parsed["debezium.format.value.schemas.enable"] == "true"
    # 单元层只断言到 .properties 解码：值等于“先做 SmallRye 表达式转义”
    # 的形态（$ -> \$）；表达式层还原由固定镜像内 JVM+SmallRye 实测验证。
    assert parsed["debezium.source.database.password"] == TRICKY_PASSWORD.replace(
        "$", "\\$")
    assert parsed["debezium.source.database.user"] == "cdc_user"
    assert parsed["debezium.source.snapshot.mode"] == "no_data"
    assert parsed["debezium.source.decimal.handling.mode"] == "string"
    assert parsed["debezium.source.provide.transaction.metadata"] == "true"
    assert parsed["debezium.source.heartbeat.interval.ms"] == "1000"
    # include 列表按正则语义转义：解码后应是 shop\.orders（字面量点）
    assert parsed["debezium.source.table.include.list"] == "shop\\.orders"
    assert parsed["debezium.source.column.include.list"] == (
        "shop\\.orders\\.amount,shop\\.orders\\.id")
    assert parsed["debezium.source.database.include.list"] == "shop"
    assert parsed["debezium.source.database.server.id"] == str(names["server_id"])
    assert parsed["debezium.source.include.schema.changes"] == "false"
    assert parsed["debezium.source.tombstones.on.delete"] == "false"
    assert "debezium.source.database.dbname" not in parsed
    assert parsed["debezium.source.offset.storage.file.filename"] == (
        str(tmp_path) + "/offsets.dat")
    assert parsed["debezium.source.schema.history.internal.file.filename"] == (
        str(tmp_path) + "/dbhistory.dat")


def test_properties_postgres_shape(tmp_path):
    connection = SimpleNamespace(kind="postgres", config={
        "host": "src.example", "port": 5432, "database": "app",
        "user": "cdc_user", "password": "pw"})
    names = connector.runtime_names("c1", 3)
    text = connector.build_properties(
        connection=connection, subscriptions=[_subscription()],
        secret_b64="c2VjcmV0", sink_url="http://127.0.0.1:47000/cdc/c1/3",
        quarkus_port=21024, state_dir=str(tmp_path), names=names)
    parsed = _parse_properties(text)
    assert parsed["debezium.source.connector.class"] == (
        "io.debezium.connector.postgresql.PostgresConnector")
    assert parsed["debezium.source.database.dbname"] == "app"
    assert parsed["debezium.source.plugin.name"] == "pgoutput"
    assert parsed["debezium.source.slot.name"] == names["slot"]
    assert parsed["debezium.source.publication.name"] == names["publication"]
    assert parsed["debezium.source.publication.autocreate.mode"] == "filtered"
    assert parsed["debezium.source.table.include.list"] == "public\\.orders"
    assert "debezium.source.database.include.list" not in parsed
    assert parsed["debezium.source.topic.prefix"] == names["topic_prefix"]


def test_properties_reject_unsupported_identifiers(tmp_path):
    connection = SimpleNamespace(kind="postgres", config={
        "host": "s", "port": 5432, "database": "d", "user": "u", "password": "p"})
    with pytest.raises(connector.ConnectorError) as excinfo:
        connector.build_properties(
            connection=connection,
            subscriptions=[_subscription(table="or ders")],
            secret_b64="c2VjcmV0", sink_url="http://127.0.0.1:1/cdc/c1/0",
            quarkus_port=21024, state_dir=str(tmp_path),
            names=connector.runtime_names("c1", 0))
    assert excinfo.value.code == runtime.CODE_IDENTIFIER_UNSUPPORTED


def test_runtime_names_deterministic_and_generation_scoped():
    first = connector.runtime_names("c1", 0)
    again = connector.runtime_names("c1", 0)
    second = connector.runtime_names("c1", 1)
    assert first == again
    assert first["slot"] != second["slot"]
    assert first["publication"] != second["publication"]
    assert first["topic_prefix"] != second["topic_prefix"]
    assert 1 <= first["server_id"] <= 2**32 - 1
    assert first["server_id"] == second["server_id"]  # 副本身份跨代稳定
    import re
    assert re.fullmatch(r"cdc_[0-9a-f]{16}", first["slot"])
    assert re.fullmatch(r"cdcpub_[0-9a-f]{12}", first["publication"])


# ── 子进程输出分类 ────────────────────────────────────────────────────

def test_classify_gap_auth_unreachable():
    assert runtime.classify_child_output(
        'ERROR: requested WAL segment 000000010000000000000001 '
        'has already been removed') == runtime.CODE_SOURCE_LOG_GAP
    assert runtime.classify_child_output(
        "... but this is no longer available on the server. "
        "Reconfigure the connector to use a snapshot when needed."
    ) == runtime.CODE_SOURCE_LOG_GAP
    assert runtime.classify_child_output(
        'replication slot "cdc_abc" does not exist') == runtime.CODE_SOURCE_LOG_GAP
    assert runtime.classify_child_output(
        "Access denied for user 'cdc'@'10.0.0.2' (using password: YES)"
    ) == runtime.CODE_SOURCE_AUTH_FAILED
    assert runtime.classify_child_output(
        "Connection refused (Connection refused)") == runtime.CODE_SOURCE_UNREACHABLE


def test_classify_does_not_flag_normal_startup_log():
    line = ("Connected to MySQL binlog at 'src:3306', starting at "
            "binlog position 'mysql-bin.000004:157'")
    assert runtime.classify_child_output(line) is None


def test_classify_privilege_denial_is_not_auth_failure():
    # 真实 canary 形态：权限型拒绝与登录认证失败措辞不同，不得混为认证失败
    denial = ("java.sql.SQLSyntaxErrorException: Access denied; you need (at "
              "least one of) the RELOAD or FLUSH_TABLES privilege(s) for this "
              "operation")
    assert runtime.classify_child_output(denial) == (
        runtime.CODE_REPLICATION_PRIVILEGE_MISSING)
    assert runtime.classify_child_output(
        "Access denied for user 'cdc'@'10.0.0.2' (using password: YES)"
    ) == runtime.CODE_SOURCE_AUTH_FAILED


@pytest.mark.parametrize("grants, expected", [
    ("GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION "
     "CLIENT ON *.* TO `cdc`@`%`", None),
    ("GRANT ALL PRIVILEGES ON *.* TO `root`@`%` WITH GRANT OPTION", None),
    ("GRANT SELECT, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO `cdc`@`%`\n"
     "GRANT FLUSH_TABLES ON *.* TO `cdc`@`%`", None),
    # 真实 canary 失败形态：复制权限齐全但缺 RELOAD/FLUSH_TABLES
    ("GRANT SELECT, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO `cdc`@`%`",
     runtime.CODE_SNAPSHOT_LOCK_PRIVILEGE_MISSING),
    # 仅 LOCK TABLES 过不了 3.7.1 的表级回退（回退同样执行 FLUSH TABLES）
    ("GRANT SELECT, LOCK TABLES, REPLICATION SLAVE, REPLICATION CLIENT ON *.* "
     "TO `cdc`@`%`", runtime.CODE_SNAPSHOT_LOCK_PRIVILEGE_MISSING),
    # 库级 ALL PRIVILEGES 不含全局 RELOAD
    ("GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO `cdc`@`%`\n"
     "GRANT ALL PRIVILEGES ON `shop`.* TO `cdc`@`%`",
     runtime.CODE_SNAPSHOT_LOCK_PRIVILEGE_MISSING),
    ("GRANT SELECT, RELOAD, REPLICATION CLIENT ON *.* TO `cdc`@`%`",
     runtime.CODE_REPLICATION_PRIVILEGE_MISSING),
    ("GRANT SELECT, RELOAD, REPLICATION SLAVE ON *.* TO `cdc`@`%`",
     runtime.CODE_REPLICATION_PRIVILEGE_MISSING),
])
def test_mysql_grant_gap_matches_debezium_snapshot_requirements(grants, expected):
    assert connector.mysql_grant_gap(grants) == expected


def test_merge_keeps_gap_priority():
    merged = runtime.merge_error_codes(
        runtime.CODE_SOURCE_UNREACHABLE, runtime.CODE_SOURCE_LOG_GAP)
    assert merged == runtime.CODE_SOURCE_LOG_GAP


def test_source_lease_non_postgres_always_held():
    lease = SourceLease(sa.create_engine("sqlite://"))
    assert lease.acquire("c1") is True
    assert lease.healthy() is True
    lease.release("c1")
    lease.reset()


def test_dockerfile_ships_self_contained_jre():
    # 打包回归：Red Hat JRE 的绝对符号链接（/etc/java/...、/usr/share/
    # javazi/...）在直接 COPY 后断裂，JVM 完整启动即 InternalError。
    # Dockerfile 必须在 donor 阶段解引用后再拷贝。
    dockerfile = (Path(__file__).resolve().parents[3]
                  / "Dockerfile.cdc").read_text(encoding="utf-8")
    assert "cp -aL /usr/lib/jvm/jre-21 /tmp/portable-jre" in dockerfile
    assert "COPY --from=debezium /tmp/portable-jre /opt/java/openjdk" in dockerfile
    assert "COPY --from=debezium /usr/lib/jvm/jre-21" not in dockerfile


# ── 监督者（全部依赖注入，无真实 IO） ─────────────────────────────────

class FakeDb:
    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def refresh(self, instance):
        pass

    def get_bind(self):
        raise AssertionError("lease must be injected in tests")


class FakeLease:
    def __init__(self, *, allowed=True):
        self.allowed = allowed
        self.acquired: list[str] = []
        self.released: list[str] = []

    def acquire(self, connection_id):
        if self.allowed:
            self.acquired.append(connection_id)
        return self.allowed

    def release(self, connection_id):
        self.released.append(connection_id)

    def healthy(self):
        return True

    def reset(self):
        pass


class FakeChild:
    def __init__(self, args=None, cwd=None, env=None, error_code=None,
                 dead=False):
        self.args, self.cwd, self.env = args, cwd, env
        self.error_code = error_code
        self.dead = dead
        self.stopped = False

    def poll(self):
        return 3 if self.dead else None

    def stop(self, *, grace=15.0):
        self.stopped = True
        self.dead = True

    def terminate(self):
        self.stopped = True
        self.dead = True

    def kill_group(self):
        pass

    def wait(self, timeout):
        return True

    def reap(self):
        pass


class FakeReceiver:
    port = 47000

    def __init__(self):
        self.registered: dict[str, tuple] = {}
        self.accepted: dict[tuple, float] = {}
        self.heartbeat: dict[str, float] = {}
        self.stopped = False

    def register(self, connection_id, generation, secret):
        self.registered[connection_id] = (generation, secret)
        return SimpleNamespace(path=f"/cdc/{connection_id}/{generation}")

    def unregister(self, connection_id):
        self.registered.pop(connection_id, None)

    def last_accepted_at(self, connection_id, generation):
        return self.accepted.get((connection_id, generation))

    def last_heartbeat_at(self, connection_id):
        return self.heartbeat.get(connection_id)

    def stop(self):
        self.stopped = True


class FakeControl:
    SOURCE_STARTING = "starting"
    SOURCE_ACTIVE = "active"
    SOURCE_PAUSED = "paused"
    SOURCE_ERROR = "error"
    SOURCE_GAP = "gap"
    SOURCE_STOPPED = "stopped"
    SUBSCRIPTION_STARTING = "starting"
    SUBSCRIPTION_ACTIVE = "active"
    SUBSCRIPTION_SUPERSEDED = "superseded"
    SUBSCRIPTION_STOPPED = "stopped"

    def __init__(self, sources, subscriptions):
        self.sources = sources
        self.subscriptions = subscriptions
        self.reports: list[dict] = []
        self.activations: list[tuple] = []
        self.order: list[str] = []
        self.cleanup_ids: list = []
        self.clock = time.time

    def reconcile(self, db):
        self.order.append("reconcile")

    def cleanup_sources(self, db):
        return list(self.cleanup_ids)

    def confirm_subscriptions_stopped(self, db, connection_id,
                                      subscription_ids=None):
        count = 0
        for sub in self.subscriptions:
            if sub.connection_id != connection_id:
                continue
            if sub.status != self.SUBSCRIPTION_SUPERSEDED:
                continue
            if subscription_ids is not None and sub.id not in subscription_ids:
                continue
            sub.status = self.SUBSCRIPTION_STOPPED
            count += 1
        return count

    def desired_sources(self, db):
        active = {s.connection_id for s in self.subscriptions
                  if s.status in (self.SUBSCRIPTION_STARTING, self.SUBSCRIPTION_ACTIVE)}
        return [row for row in self.sources
                if row.connection_id in active
                and row.status not in (self.SOURCE_PAUSED, self.SOURCE_STOPPED)]

    def desired_subscriptions(self, db, connection_id):
        return [s for s in self.subscriptions
                if s.connection_id == connection_id
                and s.status in (self.SUBSCRIPTION_STARTING, self.SUBSCRIPTION_ACTIVE)]

    def report_source(self, db, connection_id, *, generation, status,
                      error_code=None, position=None, last_event_at=None,
                      cleanup_completed=False):
        self.reports.append({
            "connection_id": connection_id, "generation": generation,
            "status": status, "error_code": error_code,
            "last_event_at": last_event_at, "cleanup_completed": cleanup_completed})
        self.order.append("report")
        # 与真实控制面一致：观测回报只在代际一致时落到源行，且每次写入都会
        # 推进 updated_at。监督者下一拍读到的正是自己上一拍写回的值——
        # “回显后被当成非外来状态”的缺陷只有这样才能复现。
        for row in self.sources:
            if row.connection_id == connection_id and row.generation == generation:
                row.status = status
                row.last_error_code = error_code
                row.updated_at = datetime.fromtimestamp(
                    self.clock(), tz=timezone.utc)
                if cleanup_completed and status == self.SOURCE_STOPPED:
                    row.cleanup_completed_at = "confirmed"

    def activate_subscription(self, db, subscription_id, *, position):
        self.activations.append((subscription_id, dict(position)))
        self.order.append("activate")
        for sub in self.subscriptions:
            if sub.id == subscription_id:
                sub.status = self.SUBSCRIPTION_ACTIVE
                sub.activation_position = dict(position)


class FakeConnector:
    def __init__(self, connections):
        self.connections = connections
        self.preflight_calls: list[list[str]] = []
        self.preflight_error: str | None = None
        self.drop_calls: list[tuple[str, str]] = []
        self.drop_result = True
        self.properties: list[str] = []
        self.continuity_calls: list[bool] = []
        self.frontier = {"dialect": "postgresql", "lsn": 4242,
                         "data_collection_order": 0}

    def runtime_names(self, connection_id, generation):
        return connector.runtime_names(connection_id, generation)

    def preflight(self, connection, subscriptions):
        self.preflight_calls.append([s.id for s in subscriptions])
        if self.preflight_error:
            raise connector.ConnectorError(self.preflight_error)

    def assert_log_continuity(self, connection, names, *, has_offsets,
                              coverage_exists):
        self.continuity_calls.append((has_offsets, coverage_exists))

    def capture_frontier(self, connection):
        return dict(self.frontier)

    def drop_owned_pg_resources(self, connection, slot, publication):
        self.drop_calls.append((slot, publication))
        return self.drop_result

    def desired_digest(self, **kwargs):
        return connector.desired_digest(**kwargs)

    def build_properties(self, **kwargs):
        text = connector.build_properties(**kwargs)
        self.properties.append(text)
        return text


def _source(cid="c1", generation=0, status="starting"):
    return SimpleNamespace(connection_id=cid, generation=generation,
                           status=status, last_error_code=None,
                           cleanup_completed_at=None,
                           updated_at=datetime.fromtimestamp(
                               time.time(), tz=timezone.utc))


class Harness:
    def __init__(self, tmp_path, *, sources, subscriptions, connections=None,
                 max_sources=8, health=lambda port: None):
        self.control = FakeControl(sources, subscriptions)
        self.receiver = FakeReceiver()
        self.connector_module = FakeConnector(connections or {})
        self.children: list[FakeChild] = []
        self.calls: list[str] = []
        lease = FakeLease()
        connections = connections or {"c1": SimpleNamespace(
            kind="postgres", config={"host": "src", "port": 5432,
                                     "database": "app", "user": "u",
                                     "password": "p"})}

        def spawn(args, cwd, env):
            self.calls.append("spawn")
            self.control.order.append("spawn")
            child = FakeChild(args=args, cwd=cwd, env=env)
            self.children.append(child)
            return child

        self.supervisor = CdcSupervisor(
            session_factory=FakeDb, control=self.control,
            receiver=self.receiver, connector_module=self.connector_module,
            spawn=spawn, lease=lease, state_root=tmp_path / "state",
            run_root=tmp_path / "run", heartbeat_file=tmp_path / "hb",
            max_sources=max_sources, health_probe=health)
        # 假控制面与监督者共用同一时钟（真实部署中两者同处执行器进程）
        self.control.clock = lambda: self.supervisor._wall()
        self.supervisor._load_sources = lambda db: self.control.sources
        self.supervisor._load_connection = (
            lambda db, cid: connections.get(cid))
        self.supervisor._superseded_subscriptions = (
            lambda db, cid: [s for s in self.control.subscriptions
                             if s.connection_id == cid
                             and s.status == self.control.SUBSCRIPTION_SUPERSEDED])

    def tick(self):
        self.supervisor.tick()

    def last_report(self):
        return self.control.reports[-1]

    def config_file(self, index=-1):
        child = self.children[index]
        return Path(child.cwd) / "config" / "application.properties"


def test_activation_waits_for_real_stream_evidence(tmp_path):
    harness = Harness(tmp_path, sources=[_source()],
                      subscriptions=[_subscription()])
    harness.tick()
    # 子进程已带期望 include 集启动，但无流证据绝不锚定、绝不冒充 active
    assert len(harness.children) == 1
    assert harness.control.activations == []
    assert harness.last_report()["status"] == "starting"
    config = harness.config_file()
    assert config.exists()
    assert (config.stat().st_mode & 0o777) == 0o600
    # 子进程环境不含平台密钥
    env = harness.children[0].env
    assert set(env) <= {"PATH", "JAVA_HOME", "LANG", "TZ"}
    assert "io.debezium.server.Main" in harness.children[0].args
    assert harness.connector_module.properties[0].isascii()
    # 流证据 + 健康探测显式 UP 之后才捕获前沿并锚定（晚于子进程启动）
    harness.receiver.accepted[("c1", 0)] = time.time()
    harness.supervisor._health_probe = lambda port: True
    harness.tick()
    order = harness.control.order
    assert order.index("spawn") < order.index("activate")
    assert harness.control.activations[0][1] == {
        "dialect": "postgresql", "lsn": 4242, "data_collection_order": 0}
    assert harness.last_report()["status"] == "active"


def test_active_requires_fresh_stream_evidence(tmp_path):
    harness = Harness(tmp_path, sources=[_source(status="active")],
                      subscriptions=[_subscription(status="active")])
    harness.tick()
    assert harness.last_report()["status"] == "starting"  # 无证据不冒充 active
    clock = time.time()
    harness.receiver.accepted[("c1", 0)] = clock
    probe_results = {"seen": False}

    def probe(port):
        probe_results["seen"] = True
        return True

    harness.supervisor._health_probe = probe
    harness.tick()
    assert probe_results["seen"]
    assert harness.last_report()["status"] == "active"


def test_include_widen_same_generation_keeps_state_dir(tmp_path):
    anchored = {"dialect": "postgresql", "lsn": 1, "data_collection_order": 0}
    first = _subscription(status="active")
    first.activation_position = dict(anchored)
    harness = Harness(tmp_path, sources=[_source(status="active")],
                      subscriptions=[first])
    harness.tick()
    first_child = harness.children[0]
    fresh = _subscription(sid="s2", table="items")
    harness.control.subscriptions.append(fresh)
    harness.tick()  # 旧子进程先停，新子进程带上扩表后的 include 集启动
    assert len(harness.children) == 2
    assert first_child.stopped
    assert harness.children[1].cwd == first_child.cwd  # 同一运行/状态目录
    properties = _parse_properties(harness.connector_module.properties[-1])
    assert "public\\.items" in properties["debezium.source.table.include.list"]
    assert "public\\.orders" in properties["debezium.source.table.include.list"]
    assert harness.connector_module.drop_calls == []  # 扩表绝不清槽
    # 新子进程尚未出现流证据：新表保持未锚定，旧锚定不被改写
    assert harness.control.activations == []
    assert first.activation_position == anchored
    harness.receiver.accepted[("c1", 0)] = time.time()
    harness.supervisor._health_probe = lambda port: True
    harness.tick()
    assert harness.control.activations[0][0] == "s2"
    assert harness.control.activations[0][1]["lsn"] == 4242
    assert first.activation_position == anchored  # 已锚定订阅前沿不重写


def test_dormant_stops_and_cleans_owned_resources(tmp_path):
    subscription = _subscription(status="superseded")
    harness = Harness(tmp_path, sources=[_source(status="active")],
                      subscriptions=[subscription])
    harness.tick()  # 期望集为空 → 直接进入休眠清理
    assert harness.connector_module.drop_calls == [
        tuple(connector.runtime_names("c1", 0)[name]
              for name in ("slot", "publication"))]
    report = harness.last_report()
    assert report["status"] == "stopped"
    assert report["cleanup_completed"] is True
    assert subscription.status == "stopped"  # superseded → stopped 确认
    assert harness.receiver.registered == {}


def test_preflight_drift_pauses_without_spawn(tmp_path):
    harness = Harness(tmp_path, sources=[_source()],
                      subscriptions=[_subscription()])
    harness.connector_module.preflight_error = runtime.CODE_SCHEMA_DRIFT
    harness.tick()
    assert harness.children == []
    assert harness.last_report()["status"] == "paused"
    assert harness.last_report()["error_code"] == runtime.CODE_SCHEMA_DRIFT
    assert harness.connector_module.drop_calls == []


def test_child_gap_is_explicit(tmp_path):
    harness = Harness(tmp_path, sources=[_source()],
                      subscriptions=[_subscription(status="active")])
    dead_child = FakeChild(error_code=runtime.CODE_SOURCE_LOG_GAP, dead=True)
    harness.supervisor._spawn = (
        lambda args, cwd, env: dead_child)
    harness.tick()
    harness.tick()  # 第二拍回收退出码并显式回报 gap
    assert harness.last_report()["status"] == "gap"
    assert harness.last_report()["error_code"] == runtime.CODE_SOURCE_LOG_GAP


def test_generation_change_uses_new_state_dir(tmp_path):
    source = _source(status="active")
    harness = Harness(tmp_path, sources=[source],
                      subscriptions=[_subscription(status="active")])
    harness.tick()
    source.generation = 1
    harness.tick()
    assert "/g1/offsets.dat" in harness.connector_module.properties[-1]
    assert harness.connector_module.drop_calls  # 旧代名下的槽被收尾清理
    assert connector.runtime_names("c1", 0)["slot"] == (
        harness.connector_module.drop_calls[-1][0])


def test_paused_source_holds_slot_without_cleanup(tmp_path):
    harness = Harness(tmp_path, sources=[_source(status="paused")],
                      subscriptions=[_subscription(status="active")])
    harness.tick()
    assert harness.children == []
    assert harness.connector_module.drop_calls == []
    assert all(report["status"] != "stopped" for report in harness.control.reports)


def test_preflight_not_polled_while_waiting_for_evidence(tmp_path):
    harness = Harness(tmp_path, sources=[_source()],
                      subscriptions=[_subscription()])
    harness.tick()
    assert len(harness.connector_module.preflight_calls) == 1
    harness.tick()  # 子进程在流式、订阅仍在等待流证据：不得再次预检源端
    harness.tick()
    assert len(harness.connector_module.preflight_calls) == 1
    assert len(harness.children) == 1


def test_lost_offsets_with_claimed_coverage_is_gap():
    connection = SimpleNamespace(kind="mysql", config={
        "host": "s", "port": 3306, "database": "shop", "user": "u",
        "password": "p"})
    names = connector.runtime_names("c1", 0)
    with pytest.raises(connector.ConnectorError) as excinfo:
        connector.assert_log_continuity(
            connection, names, has_offsets=False, coverage_exists=True)
    assert excinfo.value.code == runtime.CODE_SOURCE_LOG_GAP
    # 首次启动（从未声称覆盖）与普通续流（偏移仍在）都放行
    connector.assert_log_continuity(
        connection, names, has_offsets=False, coverage_exists=False)
    connector.assert_log_continuity(
        connection, names, has_offsets=True, coverage_exists=True)


def test_continuity_receives_claimed_coverage(tmp_path):
    anchored = _subscription(status="active")
    anchored.activation_position = {"dialect": "postgresql", "lsn": 9,
                                    "data_collection_order": 0}
    harness = Harness(tmp_path, sources=[_source(status="active")],
                      subscriptions=[anchored])
    harness.tick()
    # 已锚定订阅 + 本地无偏移文件：续流校验必须拿到 coverage_exists=True
    assert harness.connector_module.continuity_calls == [(False, True)]


def test_config_error_is_stable_code_with_backoff(tmp_path):
    harness = Harness(tmp_path, sources=[_source()],
                      subscriptions=[_subscription(table="or ders")])
    harness.tick()
    harness.tick()
    assert harness.children == []
    codes = {report["error_code"] for report in harness.control.reports}
    assert codes == {runtime.CODE_IDENTIFIER_UNSUPPORTED}
    assert all(report["status"] == "error" for report in harness.control.reports)


def test_receiver_pause_is_never_overwritten_by_fresh_evidence(tmp_path):
    source = _source(status="active")
    harness = Harness(tmp_path, sources=[source],
                      subscriptions=[_subscription(status="active")])
    harness.tick()
    harness.receiver.accepted[("c1", 0)] = time.time()
    harness.supervisor._health_probe = lambda port: True
    original = harness.connector_module.desired_digest

    def racing_digest(**kwargs):
        # 期望集已计算之后，接收器在本拍内把源置为容量暂停
        source.status = "paused"
        source.last_error_code = "EVENT_CAPACITY_EXCEEDED"
        return original(**kwargs)

    harness.connector_module.desired_digest = racing_digest
    harness.tick()
    assert harness.last_report()["status"] == "paused"
    assert harness.last_report()["error_code"] == "EVENT_CAPACITY_EXCEEDED"


def test_stopped_unconfirmed_drains_before_new_epoch(tmp_path):
    source = _source(status="stopped")
    harness = Harness(tmp_path, sources=[source],
                      subscriptions=[_subscription()])
    harness.control.cleanup_ids = [source]
    harness.tick()
    # 停止未确认清理：即使期望订阅重现也先完成清理，绝不 _hold 死锁
    assert harness.children == []
    assert harness.connector_module.drop_calls
    assert harness.last_report()["status"] == "stopped"
    assert harness.last_report()["cleanup_completed"] is True
    # 期望存续订阅绝不被确认停止（新代际由控制面 reconcile 显式给出）
    assert harness.control.subscriptions[0].status == "starting"


def test_no_evidence_startup_eventually_errors(tmp_path):
    harness = Harness(tmp_path, sources=[_source(status="active")],
                      subscriptions=[_subscription(status="active")])
    harness.supervisor._no_evidence_after = 60
    harness.tick()
    assert harness.last_report()["status"] == "starting"
    harness.supervisor._runtimes["c1"].started_wall -= 100
    harness.tick()
    assert harness.last_report()["status"] == "error"
    assert harness.last_report()["error_code"] == runtime.CODE_EXECUTOR_NO_EVIDENCE


def test_capacity_bound_reports_error(tmp_path):
    second_source = _source(cid="c2")
    pg_config = SimpleNamespace(kind="postgres", config={
        "host": "src", "port": 5432, "database": "app", "user": "u",
        "password": "p"})
    harness = Harness(tmp_path, sources=[_source(), second_source],
                      subscriptions=[_subscription(),
                                     _subscription(cid="c2", sid="s2")],
                      connections={"c1": pg_config, "c2": pg_config},
                      max_sources=1)
    harness.tick()
    assert len(harness.children) == 1
    assert any(report["error_code"] == runtime.CODE_EXECUTOR_CAPACITY
               for report in harness.control.reports)


def test_lease_not_acquired_spawns_nothing(tmp_path):
    harness = Harness(tmp_path, sources=[_source()],
                      subscriptions=[_subscription()])
    harness.supervisor._lease = FakeLease(allowed=False)
    harness.tick()
    assert harness.children == []
    assert harness.control.reports == []


def test_shutdown_stops_children_and_receiver(tmp_path):
    harness = Harness(tmp_path, sources=[_source()],
                      subscriptions=[_subscription()])
    harness.tick()
    harness.supervisor.shutdown()
    assert all(child.stopped for child in harness.children)
    assert harness.receiver.stopped is True
    assert not (Path(harness.children[0].cwd)).exists()


# ── 已持久化 error/gap 的保持与恢复（健康口径诚实性） ─────────────────

def _state(harness):
    report = harness.last_report()
    return report["status"], report["error_code"]


def _running(tmp_path):
    """已激活订阅 + 确定性时钟的运行中源（时间只在测试显式推进时前进）。"""
    source = _source(status="active")
    harness = Harness(tmp_path, sources=[source],
                      subscriptions=[_subscription(status="active")])
    harness.clock = {"now": time.time()}
    harness.supervisor._wall = lambda: harness.clock["now"]
    return source, harness


def _advance(harness, seconds):
    harness.clock["now"] += seconds


def _accept(harness, key=("c1", 0)):
    """接收器确认一条事件：时刻严格晚于此前任何一次源行写入。"""
    _advance(harness, 0.5)
    harness.receiver.accepted[key] = harness.clock["now"]
    _advance(harness, 0.5)


def _receiver_error(harness, source, code="INGEST_UNAVAILABLE"):
    """接收器以独立事务持久化瞬态错误（record_source_error permanent=False）。"""
    _advance(harness, 0.5)
    source.status, source.last_error_code = "error", code
    source.updated_at = datetime.fromtimestamp(
        harness.clock["now"], tz=timezone.utc)
    _advance(harness, 0.5)


def test_clean_start_stays_starting_then_becomes_active(tmp_path):
    source, harness = _running(tmp_path)
    for _ in range(3):  # 正常启动：多拍无证据仍是 starting/NULL，不是 error
        harness.tick()
        assert _state(harness) == ("starting", None)
    _accept(harness)
    harness.supervisor._health_probe = lambda port: True
    harness.tick()
    assert _state(harness) == ("active", None)
    assert len(harness.children) == 1


def test_persisted_error_is_held_across_ticks_without_evidence(tmp_path):
    source, harness = _running(tmp_path)
    harness.tick()
    _receiver_error(harness, source)
    for _ in range(4):
        harness.tick()
        # 第二拍起不得因“上一拍自己回显过”而被抹成 starting/NULL
        assert _state(harness) == ("error", "INGEST_UNAVAILABLE")
    assert len(harness.children) == 1  # 保持错误不等于重启子进程


def test_persisted_error_recovers_only_with_fresh_accept_and_health_up(tmp_path):
    source, harness = _running(tmp_path)
    harness.tick()
    _receiver_error(harness, source)
    harness.tick()
    _accept(harness)
    harness.tick()  # 接收证据新鲜且晚于错误，但健康探测未显式 UP：仍保持
    assert _state(harness) == ("error", "INGEST_UNAVAILABLE")
    harness.supervisor._health_probe = lambda port: True
    _accept(harness)
    harness.tick()
    assert _state(harness) == ("active", None)


def test_error_is_not_cleared_by_evidence_older_than_the_error(tmp_path):
    source, harness = _running(tmp_path)
    harness.supervisor._health_probe = lambda port: True
    harness.tick()
    _accept(harness)
    harness.tick()
    assert _state(harness) == ("active", None)
    # 同一新鲜窗口内，错误在上一次成功接收之后落库
    _receiver_error(harness, source)
    for _ in range(2):
        harness.tick()
        # 证据仍在新鲜窗口内、健康仍 UP，但它早于错误记录：不得清除新错误
        assert _state(harness) == ("error", "INGEST_UNAVAILABLE")
    _accept(harness)  # 错误之后的新接收才算恢复证据
    harness.tick()
    assert _state(harness) == ("active", None)


def test_own_transient_error_is_held_after_restart_then_recovers(tmp_path):
    source, harness = _running(tmp_path)
    spawned: list[FakeChild] = []

    def spawn(args, cwd, env):
        child = FakeChild(args=args, cwd=cwd, env=env, dead=not spawned)
        spawned.append(child)
        return child

    harness.supervisor._spawn = spawn
    harness.tick()  # 首个子进程立即退出（无可分类特征）
    assert _state(harness) == ("error", runtime.CODE_EXECUTOR_CHILD_EXITED)
    harness.tick()
    _advance(harness, 300)
    harness.tick()  # 退避期已过：重启成功，子进程存活但尚无流证据
    assert len(spawned) == 2 and spawned[1].poll() is None
    # 监督者自身的临时错误同样保持到出现正向证据，不回落 starting/NULL
    assert _state(harness) == ("error", runtime.CODE_EXECUTOR_CHILD_EXITED)
    harness.supervisor._health_probe = lambda port: True
    _accept(harness)
    harness.tick()  # 新子进程的新鲜接收 + 健康 UP：可恢复，不会被永久卡死
    assert _state(harness) == ("active", None)


def test_gap_is_terminal_until_control_explicitly_recovers(tmp_path):
    source, harness = _running(tmp_path)
    spawned: list[FakeChild] = []

    def spawn(args, cwd, env):
        child = FakeChild(args=args, cwd=cwd, env=env, dead=not spawned,
                          error_code=(None if spawned
                                      else runtime.CODE_SOURCE_LOG_GAP))
        spawned.append(child)
        return child

    harness.supervisor._spawn = spawn
    harness.tick()
    harness.tick()
    assert _state(harness) == ("gap", runtime.CODE_SOURCE_LOG_GAP)
    # 退避期已过，且人为给出“新鲜证据 + 健康 UP”：仍不得自行重启或改写 gap
    _advance(harness, 600)
    harness.supervisor._health_probe = lambda port: True
    for _ in range(3):
        _accept(harness)
        harness.tick()
        assert _state(harness) == ("gap", runtime.CODE_SOURCE_LOG_GAP)
    assert len(spawned) == 1                           # 没有自行重启子进程
    assert "c1" not in harness.supervisor._runtimes     # 不占并发名额
    assert harness.connector_module.drop_calls == []    # 槽/偏移原样保留
    assert harness.control.activations == []


def test_gap_row_stops_a_running_child_and_holds(tmp_path):
    source, harness = _running(tmp_path)
    harness.tick()
    child = harness.children[0]
    _accept(harness)
    harness.supervisor._health_probe = lambda port: True
    source.status, source.last_error_code = "gap", runtime.CODE_SOURCE_LOG_GAP
    harness.tick()
    assert child.stopped
    assert _state(harness) == ("gap", runtime.CODE_SOURCE_LOG_GAP)
    assert len(harness.children) == 1
    assert harness.connector_module.drop_calls == []
