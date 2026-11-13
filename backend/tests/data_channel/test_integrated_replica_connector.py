"""integrated 副本 lane 连接器配置层测试（不依赖真实源端运行时）。

覆盖：
- names() 按 capture cohort 派生（ir_/irpub_/oo_replica_ + uuid hex，
  server_id 散列区间与稳定性），且与 Sentinel ``cdc_`` 命名空间不相交；
- build_properties 的副本差异化契约：snapshot.mode=initial、
  snapshot.fetch.size、max.batch.size/max.queue.size、事务元数据、
  1s 心跳、offset.flush 1s、precise decimal/base64 binary/
  adaptive_time_microseconds、TransportMetadata SMT、notification sink
  话题、full schema 列集（不做 field_map 投影）；
- Java .properties/SmallRye 转义往返与纯 ASCII 输出；
- 稳定错误码（不含配置/凭据值）；
- preflight 完全委托 CDC 预检；
- validate_resume 的最小 PG 槽/gap 检查（MySQL 不触源）；
- cleanup 只按 ir_/irpub_ 白名单删除自有槽/发布，活跃槽拒绝删除。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from app.data_channel.integrated import cdc_connector as cdc
from app.data_channel.integrated import replica_connector as replica
from app.data_channel.integrated.contracts import JsonText, serialize_source_json


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
        parsed[line.split("=", 1)[0]] = _decode_properties_value(line)
    return parsed


CAPTURE_ID = "7f0f2f1e-6c9e-4f0a-9b6d-1e2f3a4b5c6d"
CAPTURE_HEX = "7f0f2f1e6c9e4f0a9b6d1e2f3a4b5c6d"

TRICKY_PASSWORD = ' p\\a$s{var}\n中:='


def _asset(schema="public", table="orders"):
    return SimpleNamespace(
        schema_name=schema, table_name=table, schema_fingerprint="f" * 64,
        schema_json={
            "columns": [
                {"name": "id", "type": "integer", "nullable": False},
                {"name": "amount", "type": "decimal", "source_type": "NUMERIC(65,30)",
                 "precision": 65, "scale": 30, "nullable": True},
                {"name": "payload", "type": "binary", "nullable": True},
                {"name": "created_at", "type": "timestamp", "nullable": True},
            ],
            "primary_key": ["id"]},
    )


def _pg_connection():
    return SimpleNamespace(kind="postgres", config={
        "host": "src.example", "port": 5432, "database": "appdb",
        "user": "replica_user", "password": TRICKY_PASSWORD})


# ── names() ──────────────────────────────────────────────────────────

def test_names_capture_cohort_derivation():
    derived = replica.names(CAPTURE_ID)
    assert derived["slot"] == "ir_" + CAPTURE_HEX
    assert derived["publication"] == "irpub_" + CAPTURE_HEX
    # 话题前缀必须与 replica_ingest/replica_journal 校验的 server 名一致
    assert derived["topic_prefix"] == "oo_replica_" + CAPTURE_HEX
    assert 1 <= derived["server_id"] <= 4294967295
    # 确定性：同 capture 稳定，异 capture 不冲突
    assert replica.names(CAPTURE_ID) == derived
    other = replica.names("0d0f2f1e-6c9e-4f0a-9b6d-1e2f3a4b5c6d")
    assert other["slot"] != derived["slot"]
    assert other["server_id"] != derived["server_id"]
    # 大写 UUID 归一为小写 hex
    assert replica.names(CAPTURE_ID.upper()) == derived


def test_names_never_collide_with_sentinel_namespace():
    sentinel = cdc.runtime_names("c1", 0)
    for name in (sentinel["slot"], sentinel["publication"], sentinel["topic_prefix"]):
        assert not replica._OWNED_SLOT.fullmatch(name)
        assert not replica._OWNED_PUBLICATION.fullmatch(name)
        assert not replica._OWNED_TOPIC_PREFIX.fullmatch(name)
    assert cdc._OWNED_SLOT.fullmatch(sentinel["slot"])  # Sentinel 白名单原样


def test_names_rejects_non_uuid_with_stable_code():
    with pytest.raises(replica.ConnectorError) as exc:
        replica.names("not-a-uuid")
    assert exc.value.code == "EXECUTOR_INTERNAL"
    assert str(exc.value) == "EXECUTOR_INTERNAL"


def test_notification_sink_topic():
    assert replica.notification_sink_topic(CAPTURE_ID) == (
        "oo_replica_" + CAPTURE_HEX + ".notifications")


# ── build_properties ─────────────────────────────────────────────────

def test_build_properties_postgres_replica_contract(tmp_path):
    text = replica.build_properties(
        _pg_connection(), [_asset()], CAPTURE_ID, "c2VjcmV0",
        "http://127.0.0.1:48000/replica/" + CAPTURE_ID,
        22024, str(tmp_path))
    assert text.isascii()
    parsed = _parse_properties(text)
    derived = replica.names(CAPTURE_ID)
    # 快照：initial 全量起流 + 受控 fetch
    assert parsed["debezium.source.snapshot.mode"] == "initial"
    assert parsed["debezium.source.snapshot.fetch.size"] == "1000"
    # 批量：sink 批量开 + 源端批/队列上限
    assert parsed["debezium.sink.http.batch.enabled"] == "true"
    assert parsed["debezium.source.max.batch.size"] == "512"
    assert parsed["debezium.source.max.queue.size"] == "2048"
    # 事务元数据 + 1s 心跳 + 1s 偏移落盘
    assert parsed["debezium.source.provide.transaction.metadata"] == "true"
    assert parsed["debezium.source.heartbeat.interval.ms"] == "1000"
    assert parsed["debezium.source.offset.flush.interval.ms"] == "1000"
    # 精确类型契约（NUMERIC(65,30) 精确传输）
    assert parsed["debezium.source.decimal.handling.mode"] == "precise"
    assert parsed["debezium.source.binary.handling.mode"] == "bytes"
    assert parsed["debezium.source.time.precision.mode"] == "adaptive_time_microseconds"
    # Java 扩展 SMT 与通知 sink 通道
    assert parsed["debezium.source.transforms"] == "transport"
    assert parsed["debezium.source.transforms.transport.type"] == (
        "io.openontology.replica.TransportMetadata")
    assert parsed[replica.NOTIFICATION_CHANNELS_KEY] == "sink"
    assert parsed[replica.NOTIFICATION_SINK_TOPIC_KEY] == (
        "oo_replica_" + CAPTURE_HEX + ".notifications")
    # capture cohort 资源命名
    assert parsed["debezium.source.topic.prefix"] == derived["topic_prefix"]
    assert parsed["debezium.source.slot.name"] == derived["slot"]
    assert parsed["debezium.source.publication.name"] == derived["publication"]
    assert parsed["debezium.source.publication.autocreate.mode"] == "filtered"
    assert parsed["debezium.source.plugin.name"] == "pgoutput"
    # 偏移/模式历史落在 capture 状态目录
    assert parsed["debezium.source.offset.storage.file.filename"] == str(tmp_path / "offsets.dat")
    assert parsed["debezium.source.schema.history.internal.file.filename"] == str(tmp_path / "dbhistory.dat")
    # full schema：全部注册列，不做 field_map 投影
    assert parsed["debezium.source.column.include.list"] == (
        r"public\.orders\.amount,public\.orders\.created_at,"
        r"public\.orders\.id,public\.orders\.payload")
    assert parsed["debezium.source.table.include.list"] == r"public\.orders"
    assert "debezium.source.database.server.id" not in parsed


def test_build_properties_mysql_shape(tmp_path):
    connection = SimpleNamespace(kind="mysql", config={
        "host": "src.example", "port": 3306, "database": "shop",
        "user": "replica_user", "password": TRICKY_PASSWORD})
    text = replica.build_properties(
        connection, [_asset(schema="shop"), _asset(schema="shop", table="items")],
        CAPTURE_ID, "c2VjcmV0", "http://127.0.0.1:48000/replica",
        22024, str(tmp_path))
    parsed = _parse_properties(text)
    derived = replica.names(CAPTURE_ID)
    assert parsed["debezium.source.database.server.id"] == str(derived["server_id"])
    assert parsed["debezium.source.bigint.unsigned.handling.mode"] == "precise"
    assert parsed["debezium.source.database.include.list"] == "shop"
    assert parsed["debezium.source.include.schema.changes"] == "false"
    assert parsed["debezium.source.snapshot.mode"] == "initial"
    # MySQL 不占源端命名资源
    assert "debezium.source.slot.name" not in parsed
    assert "debezium.source.publication.name" not in parsed
    tables = parsed["debezium.source.table.include.list"].split(",")
    assert sorted(tables) == sorted([r"shop\.items", r"shop\.orders"])


def test_build_properties_escaping_roundtrip(tmp_path):
    text = replica.build_properties(
        _pg_connection(), [_asset()], CAPTURE_ID,
        'sec$ {ret}\t\\x', "http://127.0.0.1:48000/r", 22024, str(tmp_path))
    assert text.isascii()
    parsed = _parse_properties(text)
    # 凭据经 _config_literal：$ 先做 SmallRye 转义，.properties 层解码后
    # 呈现 \$ 字面量（展开引擎再还原为 $），其余字符 Java 语义精确往返。
    assert parsed["debezium.source.database.password"] == ' p\\a\\$s{var}\n中:='
    assert parsed["debezium.sink.http.authentication.webhook.secret"] == 'sec\\$ {ret}\t\\x'
    # SmallRye 展开保护：文件里不得出现裸 ${ 序列
    for line in text.splitlines():
        if "webhook.secret" in line:
            assert "${" not in line


def test_build_properties_stable_errors_without_secrets(tmp_path):
    # 不支持方言
    with pytest.raises(replica.ConnectorError) as exc:
        replica.build_properties(SimpleNamespace(kind="oracle", config={}),
                                 [_asset()], CAPTURE_ID, "s", "u", 1, str(tmp_path))
    assert exc.value.code == "UNSUPPORTED_SOURCE"
    # MySQL 无库名显式拒绝
    with pytest.raises(replica.ConnectorError) as exc:
        replica.build_properties(SimpleNamespace(kind="mysql", config={
            "host": "h", "port": 3306, "user": "u", "password": "p"}),
            [_asset()], CAPTURE_ID, "s", "u", 1, str(tmp_path))
    assert exc.value.code == "SOURCE_CONFIG_INVALID"
    assert "p" not in str(exc.value)  # 错误正文即错误码，绝不携带凭据
    # 空 capture cohort / 无注册列资产是控制面不变量违例
    with pytest.raises(replica.ConnectorError) as exc:
        replica.build_properties(_pg_connection(), [], CAPTURE_ID, "s", "u", 1, str(tmp_path))
    assert exc.value.code == "EXECUTOR_INTERNAL"
    empty = SimpleNamespace(schema_name="public", table_name="orders",
                            schema_fingerprint="f", schema_json={})
    with pytest.raises(replica.ConnectorError) as exc:
        replica.build_properties(_pg_connection(), [empty], CAPTURE_ID, "s", "u", 1, str(tmp_path))
    assert exc.value.code == "EXECUTOR_INTERNAL"


# ── preflight：短超时引擎上逐项复用 CDC 预检原语 ────────────────────

def test_preflight_reuses_cdc_primitives_on_check_engine(monkeypatch):
    calls = []
    engine = _FakeEngine(_FakeConnection([(160000, False, "off", True)]))

    def fake_check_engine(connection):
        calls.append(("engine", connection))
        return engine

    def fake_dialect(conn, assets):
        calls.append(("postgres", conn, list(assets)))

    def fake_drift(conn, asset):
        calls.append(("drift", conn, asset))

    monkeypatch.setattr(replica, "_source_check_engine", fake_check_engine)
    monkeypatch.setattr(cdc, "_preflight_postgres", fake_dialect)
    monkeypatch.setattr(cdc, "_check_schema_drift", fake_drift)
    connection, assets = _pg_connection(), [_asset(), _asset(table="items")]
    replica.preflight(connection, assets)
    assert calls[0] == ("engine", connection)
    assert calls[1] == ("postgres", engine.connection, assets)
    assert [call[:2] for call in calls[2:]] == [
        ("drift", engine.connection), ("drift", engine.connection)]
    assert engine.disposed  # 控制引擎用完即弃


def test_preflight_mysql_uses_mysql_primitive(monkeypatch):
    calls = []
    monkeypatch.setattr(replica, "_source_check_engine",
                        lambda connection: _FakeEngine(_FakeConnection([])))
    monkeypatch.setattr(cdc, "_preflight_mysql",
                        lambda conn: calls.append(conn))
    monkeypatch.setattr(cdc, "_check_schema_drift", lambda conn, asset: None)
    connection = SimpleNamespace(kind="mysql", config={
        "host": "h", "port": 3306, "database": "shop", "user": "u", "password": "p"})
    replica.preflight(connection, [_asset(schema="shop")])
    assert len(calls) == 1


def test_preflight_propagates_stable_codes(monkeypatch):
    monkeypatch.setattr(replica, "_source_check_engine",
                        lambda connection: _FakeEngine(_FakeConnection([(160000, False, "off", True)])))
    # ConnectorError 原样透传
    def raise_drift(conn, asset):
        raise replica.ConnectorError("SCHEMA_DRIFT")
    monkeypatch.setattr(cdc, "_preflight_postgres", lambda conn, assets: None)
    monkeypatch.setattr(cdc, "_check_schema_drift", raise_drift)
    with pytest.raises(replica.ConnectorError) as exc:
        replica.preflight(_pg_connection(), [_asset()])
    assert exc.value.code == "SCHEMA_DRIFT"
    # 驱动异常（含超时）分类为稳定码，不携带异常原文
    def raise_timeout(conn, assets):
        raise sa.exc.OperationalError("stmt", {}, Exception("driver gone"))
    monkeypatch.setattr(cdc, "_preflight_postgres", raise_timeout)
    monkeypatch.setattr(cdc, "_check_schema_drift", lambda conn, asset: None)
    with pytest.raises(replica.ConnectorError) as exc:
        replica.preflight(_pg_connection(), [_asset()])
    assert exc.value.code == "SOURCE_UNREACHABLE"
    # 不支持方言不触源
    with pytest.raises(replica.ConnectorError) as exc:
        replica.preflight(SimpleNamespace(kind="oracle", config={}), [_asset()])
    assert exc.value.code == "UNSUPPORTED_SOURCE"


# ── 控制面短超时引擎 ─────────────────────────────────────────────────

def test_source_check_engine_timeout_knobs(monkeypatch):
    created = {}

    def fake_create_engine(url, **kwargs):
        created[str(url)] = kwargs
        return SimpleNamespace(dispose=lambda: None)

    monkeypatch.setattr(sa, "create_engine", fake_create_engine)
    # MySQL：connect 3s + 读写 5s，绝不带读通道的 read_timeout=300
    replica._source_check_engine(SimpleNamespace(kind="mysql", config={
        "host": "h", "port": 3306, "database": "shop",
        "user": "u", "password": "p"}))
    (kwargs,) = created.values()
    assert kwargs["connect_args"] == {
        "connect_timeout": 3, "read_timeout": 5, "write_timeout": 5}
    assert kwargs["poolclass"] is NullPool
    assert kwargs["hide_parameters"] is True
    assert kwargs["json_deserializer"] is JsonText
    assert kwargs["json_serializer"] is serialize_source_json
    # PostgreSQL：connect 3s + 服务端语句/锁超时
    created.clear()
    replica._source_check_engine(_pg_connection())
    (kwargs,) = created.values()
    assert kwargs["connect_args"] == {
        "connect_timeout": 3,
        "options": "-c statement_timeout=5000 -c lock_timeout=3000"}


def test_source_check_engine_config_failure_is_stable(monkeypatch):
    with pytest.raises(replica.ConnectorError) as exc:
        replica._source_check_engine(SimpleNamespace(kind="postgres", config={
            "connection_string": "not a valid url !!"}))
    assert exc.value.code == "SOURCE_CONFIG_UNAVAILABLE"


# ── 源端 fake 引擎 ───────────────────────────────────────────────────

class _FakeConnection:
    def __init__(self, results):
        self.executed: list[tuple[str, dict | None]] = []
        self._results = list(results)

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        result = self._results.pop(0) if self._results else None
        return SimpleNamespace(
            scalar_one_or_none=lambda: result,
            scalar_one=lambda: result,
            one=lambda: result)

    def execution_options(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeEngine:
    def __init__(self, connection):
        self.connection = connection
        self.disposed = False

    def connect(self):
        return self.connection

    def dispose(self):
        self.disposed = True


# ── validate_resume ──────────────────────────────────────────────────

def _patch_engine(monkeypatch, results):
    engine = _FakeEngine(_FakeConnection(results))
    monkeypatch.setattr(replica, "_source_check_engine", lambda connection: engine)
    return engine


def test_validate_resume_postgres_slot_gap_detection(monkeypatch):
    connection = _pg_connection()
    slot = replica.names(CAPTURE_ID)["slot"]
    # 槽不存在：持久化偏移在而槽丢失，重启会静默跳过区间
    _patch_engine(monkeypatch, [None])
    with pytest.raises(replica.ConnectorError) as exc:
        replica.validate_resume(connection, CAPTURE_ID)
    assert exc.value.code == "SOURCE_LOG_GAP"
    # 槽被回收（lost）同样显式 gap
    _patch_engine(monkeypatch, ["lost"])
    with pytest.raises(replica.ConnectorError) as exc:
        replica.validate_resume(connection, CAPTURE_ID)
    assert exc.value.code == "SOURCE_LOG_GAP"
    # 正常 retained/active 槽续流放行
    _patch_engine(monkeypatch, ["retained"])
    assert replica.validate_resume(connection, CAPTURE_ID) is None


def test_validate_resume_timeout_classifies_to_stable_code(monkeypatch):
    # 控制引擎的语句/驱动超时（含 PG statement_timeout 取消）只分类为
    # 稳定码，异常原文（可能含源信息）绝不进入错误正文。
    class _TimeoutConn:
        def execute(self, stmt, params=None):
            raise sa.exc.OperationalError("stmt", params, Exception("driver gone"))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _TimeoutEngine:
        def connect(self):
            return _TimeoutConn()

        def dispose(self):
            pass

    monkeypatch.setattr(replica, "_source_check_engine", lambda connection: _TimeoutEngine())
    with pytest.raises(replica.ConnectorError) as exc:
        replica.validate_resume(_pg_connection(), CAPTURE_ID)
    assert exc.value.code == "SOURCE_UNREACHABLE"


def test_validate_resume_mysql_never_touches_source(monkeypatch):
    def explode(connection):
        raise AssertionError("mysql resume check must not touch the source")

    monkeypatch.setattr(replica, "_source_check_engine", explode)
    assert replica.validate_resume(
        SimpleNamespace(kind="mysql", config={}), CAPTURE_ID) is None


# ── cleanup ──────────────────────────────────────────────────────────

def test_cleanup_postgres_drops_owned_slot_and_publication(monkeypatch):
    engine = _patch_engine(monkeypatch, [False])  # slot 不活跃
    assert replica.cleanup(_pg_connection(), CAPTURE_ID) is True
    assert engine.disposed
    statements = [stmt for stmt, _ in engine.connection.executed]
    assert any("pg_drop_replication_slot" in stmt
               and engine.connection.executed[0][1] == {"slot": "ir_" + CAPTURE_HEX}
               for stmt in statements)
    assert statements[-1] == f'DROP PUBLICATION IF EXISTS "irpub_{CAPTURE_HEX}"'


def test_cleanup_refuses_active_slot(monkeypatch):
    engine = _patch_engine(monkeypatch, [True])  # slot 活跃：子进程未停净
    assert replica.cleanup(_pg_connection(), CAPTURE_ID) is False
    assert len(engine.connection.executed) == 1  # 仅探测，无删除


def test_cleanup_mysql_has_no_source_resources(monkeypatch):
    def explode(connection):
        raise AssertionError("mysql cleanup must not touch the source")

    monkeypatch.setattr(replica, "_source_check_engine", explode)
    assert replica.cleanup(SimpleNamespace(kind="mysql", config={}), CAPTURE_ID) is True


@pytest.mark.parametrize("result,code", [
    ((130000, False, "off", True), "REPLICA_PG_VERSION_UNSUPPORTED"),
    ((160000, True, "off", True), "REPLICA_SOURCE_READ_ONLY"),
    ((160000, False, "on", True), "REPLICA_SOURCE_READ_ONLY"),
    ((160000, False, "off", False), "REPLICA_WAL_MESSAGE_PRIVILEGE_MISSING"),
])
def test_coverage_preflight_rejects_without_writing_source(result, code):
    connection = _FakeConnection([result])
    with pytest.raises(replica.ConnectorError) as exc:
        replica._preflight_postgres_coverage(connection)
    assert exc.value.code == code
    assert len(connection.executed) == 1
    assert "SELECT" in connection.executed[0][0]
    assert "SELECT pg_logical_emit_message" not in connection.executed[0][0]


def test_postgres_coverage_emits_only_owned_logical_message(monkeypatch):
    engine = _patch_engine(monkeypatch, [(160000, False, "off", True), None, 123456])
    frontier = replica.emit_coverage_probe(_pg_connection(), CAPTURE_ID, "a" * 32)
    assert frontier == {"dialect": "postgresql", "lsn": 123456}
    statements = engine.connection.executed
    assert len(statements) == 3
    assert "pg_logical_emit_message(false, :prefix, :token)" in statements[-1][0]
    assert statements[-1][1] == {"prefix": "oo_replica_cut_" + CAPTURE_HEX,
                                  "token": "a" * 32}
    assert all("INSERT" not in statement and "UPDATE" not in statement
               for statement, _ in statements)
    assert engine.disposed


@pytest.mark.parametrize("version,statement", [
    ("8.4.13", "SHOW BINARY LOG STATUS"), ("8.0.44", "SHOW MASTER STATUS"),
])
def test_mysql_coverage_samples_same_result_file_and_gtids(monkeypatch, version, statement):
    source = SimpleNamespace(kind="mysql", config={})
    engine = _patch_engine(monkeypatch, [version,
        ("mysql-bin.000010", 500, "", "", "a30f5e7e-2ee0-441a-a693-fbdc22cb7c39:1-20")])
    frontier = replica.emit_coverage_probe(source, CAPTURE_ID, "a" * 32)
    assert frontier == {"dialect": "mysql", "file": "mysql-bin.000010", "pos": 500,
                       "gtids": "a30f5e7e-2ee0-441a-a693-fbdc22cb7c39:1-20"}
    assert engine.connection.executed[-1][0] == statement
    assert engine.disposed
