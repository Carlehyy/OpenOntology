"""integrated 副本（快照+流式复制）方言适配：预检、续流校验、Debezium 配置。

与 Sentinel CDC（``cdc_connector.py``，no_data 起流）互补的另一条 lane：

- 副本以 capture 为 cohort 独立占用源端资源：复制槽 ``ir_<32hex>``、发布
  ``irpub_<32hex>``、话题前缀 ``oo_replica_<32hex>``（32hex =
  ``uuid.UUID(capture_id).hex``，与 ``replica_ingest``/``replica_journal``
  校验的 server 名一致），与 Sentinel 的 ``cdc_``/``cdcpub_`` 命名空间互不
  相交，互不换代、互不影响默认值；
- ``snapshot.mode=initial``：副本从全量快照起流再续流，与 CDC 的 no_data
  覆盖语义不同；全量列取自资产注册时的 full schema（``schema_json``），
  不做 field_map 投影；
- 精确类型：decimal=precise（NUMERIC(65,30) 不经浮点，JSON 里以 Kafka
  Connect Decimal base64 编码，由 ``replica_codec`` 精确解码）、binary=
    bytes（JSON converter 在信封内编码为 base64）、time.precision.mode=adaptive_time_microseconds；
- 对源库不改业务数据、不自动设置源库参数（含 ``track_commit_timestamp``
  等服务端配置；PG 侧提交时间只消费事务元数据通道实际提供的信息，
  也不配置 heartbeat.action.query 写业务库）；
- 连接配置、URL 归一、.properties/SmallRye 转义与预检直接复用
  ``cdc_connector`` 的既有实现；任何失败只携带稳定错误码
  （``ConnectorError``，正文即错误码），绝不把主机/账号/SQL/源值带入
  日志或异常文本；
- 控制面源检查（preflight/validate_resume/cleanup）走专用短超时引擎
  ``_source_check_engine``（connect 3s；MySQL 读写 5s；PG
  statement_timeout=5s/lock_timeout=3s）：与读通道（MySQL read_timeout
  300s）解耦，源端失联时绝不让 supervisor 的关停/协调被长超时挂起。

通知通道：``notification channel sink`` 把 Debezium 通知事件（原生
schema ``io.debezium.connector.common.Notification``）发往
``oo_replica_<captureUUID>.notifications`` 话题；键名为单数
``notification.*``（真实 PG 源探针实测），如需调整只改本文件顶部常量。
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid

import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from app.data_channel.integrated import cdc_connector as connector
from app.data_channel.integrated import cdc_runtime as runtime
from app.data_channel.integrated import native
from app.data_channel.integrated.contracts import JsonText, serialize_source_json

logger = logging.getLogger(__name__)

# 副本自有资源命名（与 Sentinel 的 cdc_/cdcpub_ 命名空间不相交）。
_OWNED_SLOT = re.compile(r"ir_[0-9a-f]{32}")
_OWNED_PUBLICATION = re.compile(r"irpub_[0-9a-f]{32}")
_OWNED_TOPIC_PREFIX = re.compile(r"oo_replica_[0-9a-f]{32}")

# 与 cdc_connector 相同的方言常量/错误类型；副本 lane 不建第二套。
ConnectorError = connector.ConnectorError
KIND_MYSQL = connector.KIND_MYSQL
KIND_POSTGRES = connector.KIND_POSTGRES

# ── 副本 lane 差异化配置常量 ────────────────────────────────────────

SNAPSHOT_MODE = "initial"
# 全量快照读取节奏：每次触达源端的行数，兼顾内存与快照时长。
SNAPSHOT_FETCH_SIZE = 1000
# 流式批量：源端单批事件上限与内部队列容量（快照+流式共用）。
MAX_BATCH_SIZE = 512
MAX_QUEUE_SIZE = 2048
# 副本偏移落盘更密：故障重启后重放窗口小。
OFFSET_FLUSH_INTERVAL_MS = 1000
# 心跳即 active 证据，与 CDC lane 同一间隔（1s），且不写业务库。
HEARTBEAT_INTERVAL_MS = connector.HEARTBEAT_INTERVAL_MS
# 精确类型：NUMERIC(65,30) 等以 Kafka Connect Decimal(base64) 精确传输。
DECIMAL_HANDLING_MODE = "precise"
BINARY_HANDLING_MODE = "bytes"
TIME_PRECISION_MODE = "adaptive_time_microseconds"
# Java 扩展 SMT：为副本传输补 transport 元数据（主会话 runtime 接线）。
TRANSPORT_SMT_ALIAS = "transport"
TRANSPORT_SMT_CLASS = "io.openontology.replica.TransportMetadata"
# 通知走 sink channel，落到副本专属通知话题。
NOTIFICATION_CHANNELS = "sink"
NOTIFICATION_SINK_TOPIC_SUFFIX = ".notifications"
# 键名已经真实 PG 源探针确认：单数 notification.*（经 debezium.source.
# 透传），通知 schema io.debezium.connector.common.Notification。
NOTIFICATION_CHANNELS_KEY = "debezium.source.notification.enabled.channels"
NOTIFICATION_SINK_TOPIC_KEY = "debezium.source.notification.sink.topic.name"


# ── 确定性运行时命名（按 capture cohort） ──────────────────────────

def names(capture_id: str) -> dict:
    """复制槽/发布/话题前缀/MySQL server id 按 capture 派生。

    32hex 即 ``uuid.UUID(capture_id).hex``：话题前缀必须与
    ``replica_ingest``/``replica_journal`` 校验的 ``oo_replica_<hex>``
    server 名逐字一致；server_id 按capture 确定性散列到
    ``[1, 2^32-1]``，同一源上多个 capture 互不冲突。
    """
    try:
        capture_hex = uuid.UUID(str(capture_id)).hex
    except (ValueError, AttributeError, TypeError):
        raise ConnectorError(runtime.CODE_EXECUTOR_INTERNAL) from None
    derived = {
        "slot": "ir_" + capture_hex,
        "publication": "irpub_" + capture_hex,
        "topic_prefix": "oo_replica_" + capture_hex,
        "server_id": int(hashlib.sha256(
            ("replica|" + capture_hex + "|server-id").encode()
        ).hexdigest()[:8], 16) % 4294967294 + 1,
    }
    if not (_OWNED_SLOT.fullmatch(derived["slot"])
            and _OWNED_PUBLICATION.fullmatch(derived["publication"])
            and _OWNED_TOPIC_PREFIX.fullmatch(derived["topic_prefix"])):
        raise ConnectorError(runtime.CODE_EXECUTOR_INTERNAL)
    return derived


def notification_sink_topic(capture_id: str) -> str:
    return names(capture_id)["topic_prefix"] + NOTIFICATION_SINK_TOPIC_SUFFIX


# ── 控制面源检查专用短超时引擎 ──────────────────────────────────────

# 只服务 preflight/validate_resume/cleanup 三类控制 IO：超时上限即
# supervisor 单轮协调的最坏阻塞时长（主会话再做异步化，这里先保证有界）。
CHECK_CONNECT_TIMEOUT_SECONDS = 3
CHECK_MYSQL_READ_TIMEOUT_SECONDS = 5
CHECK_MYSQL_WRITE_TIMEOUT_SECONDS = 5
CHECK_PG_STATEMENT_TIMEOUT_MS = 5000
CHECK_PG_LOCK_TIMEOUT_MS = 3000


def _source_check_engine(connection):
    """URL 归一/序列化器与 ``native.source_engine`` 完全一致，但连接参数
    换成控制面短超时（MySQL 读 300s 的读通道参数绝不带进来）。"""
    try:
        url = native._source_url(connection)
        connect_args = {"connect_timeout": CHECK_CONNECT_TIMEOUT_SECONDS}
        if getattr(connection, "kind", None) == KIND_MYSQL:
            connect_args.update(read_timeout=CHECK_MYSQL_READ_TIMEOUT_SECONDS,
                                write_timeout=CHECK_MYSQL_WRITE_TIMEOUT_SECONDS)
        else:
            connect_args["options"] = (
                f"-c statement_timeout={CHECK_PG_STATEMENT_TIMEOUT_MS} "
                f"-c lock_timeout={CHECK_PG_LOCK_TIMEOUT_MS}")
        return sa.create_engine(
            url, poolclass=NullPool, connect_args=connect_args,
            hide_parameters=True, json_deserializer=JsonText,
            json_serializer=serialize_source_json)
    except Exception:
        raise ConnectorError(runtime.CODE_SOURCE_CONFIG_UNAVAILABLE) from None


# ── 预检：逐项复用 CDC 预检原语（能力 + 精确模式漂移） ──────────────

def preflight(connection, assets) -> None:
    """资产（``IntegratedAsset``）与订阅同形：schema_name/table_name/
    schema_fingerprint 驱动同一套反射指纹漂移校验与方言能力预检。

    与 ``cdc_connector.preflight`` 相同的算法路径（逐项调用其
    方言预检/漂移原语），但跑在本 lane 的短超时控制引擎上；失败只带
    稳定错误码，不输出任何源配置或凭据。"""
    kind = getattr(connection, "kind", None)
    if kind not in (KIND_MYSQL, KIND_POSTGRES):
        raise ConnectorError(runtime.CODE_UNSUPPORTED_SOURCE)
    assets = list(assets or [])
    engine = _source_check_engine(connection)
    try:
        with engine.connect() as conn:
            if kind == KIND_MYSQL:
                connector._preflight_mysql(conn)
            else:
                connector._preflight_postgres(conn, assets)
                _preflight_postgres_coverage(conn)
            for asset in assets:
                connector._check_schema_drift(conn, asset)
    except ConnectorError:
        raise
    except sa.exc.NoSuchTableError:
        raise ConnectorError(runtime.CODE_SCHEMA_DRIFT) from None
    except sa.exc.SQLAlchemyError as exc:
        raise connector._driver_error(exc) from None
    finally:
        engine.dispose()
    logger.info("replica preflight ok kind=%s tables=%s", kind, len(assets))


def _preflight_postgres_coverage(conn):
    version, recovery, read_only, permitted = conn.execute(sa.text(
        "SELECT current_setting('server_version_num')::integer, "
        "pg_is_in_recovery(), current_setting('transaction_read_only'), "
        "has_function_privilege(current_user, "
        "'pg_logical_emit_message(boolean,text,text)', 'EXECUTE')")).one()
    if int(version) < 140000:
        raise ConnectorError("REPLICA_PG_VERSION_UNSUPPORTED")
    if recovery or str(read_only).lower() == "on":
        raise ConnectorError("REPLICA_SOURCE_READ_ONLY")
    if not permitted:
        raise ConnectorError("REPLICA_WAL_MESSAGE_PRIVILEGE_MISSING")


def emit_coverage_probe(connection, capture_id, token):
    """Issue an ordered cut. PG writes only a managed logical WAL message.

    MySQL returns the fixed next-append position and completed GTID set in one
    SHOW result. No business-table writes or source configuration changes occur.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", str(token)):
        raise ConnectorError(runtime.CODE_EXECUTOR_INTERNAL)
    owned = names(capture_id)
    engine = _source_check_engine(connection)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            if connection.kind == KIND_POSTGRES:
                from app.data_channel.integrated.replica_coverage import prefix

                _preflight_postgres_coverage(conn)
                conn.execute(sa.text("SET synchronous_commit = on"))
                position = conn.execute(sa.text(
                    "SELECT pg_wal_lsn_diff(pg_logical_emit_message(false, :prefix, :token), "
                    "'0/0')::bigint"), {"prefix": prefix(capture_id), "token": token}).scalar_one()
                return {"dialect": "postgresql", "lsn": int(position)}
            if connection.kind != KIND_MYSQL:
                raise ConnectorError(runtime.CODE_UNSUPPORTED_SOURCE)
            version = str(conn.execute(sa.text("SELECT VERSION()")).scalar_one())
            match = re.match(r"(\d+)\.(\d+)", version)
            if not match:
                raise ConnectorError(runtime.CODE_UNSUPPORTED_SOURCE)
            statement = "SHOW BINARY LOG STATUS" if tuple(map(int, match.groups())) >= (8, 4) \
                else "SHOW MASTER STATUS"
            row = conn.execute(sa.text(statement)).one()
            gtids = str(row[4] or "") if len(row) > 4 else ""
            return {"dialect": "mysql", "file": str(row[0]), "pos": int(row[1]),
                    "gtids": gtids or None}
    except ConnectorError:
        raise
    except sa.exc.SQLAlchemyError as exc:
        raise connector._driver_error(exc) from None
    finally:
        engine.dispose()


# ── include 描述符：full schema（全部注册列 + 主鍵） ────────────────

def _registered_columns(asset) -> list[str]:
    schema = getattr(asset, "schema_json", None) or {}
    if not isinstance(schema, dict):
        raise ConnectorError(runtime.CODE_EXECUTOR_INTERNAL)
    columns = [entry.get("name") for entry in (schema.get("columns") or [])
               if isinstance(entry, dict)]
    # 主键等一律取 full schema；防御性并集保证键列永不缺席。
    columns += [name for name in (schema.get("primary_key") or [])]
    ordered = [name for name in dict.fromkeys(columns) if name]
    if not ordered:
        raise ConnectorError(runtime.CODE_EXECUTOR_INTERNAL)
    return ordered


def _table_entries(assets) -> list[str]:
    return sorted({
        f"{connector._regex_literal(asset.schema_name)}"
        f"\\.{connector._regex_literal(asset.table_name)}"
        for asset in assets})


def _column_entries(assets) -> list[str]:
    entries: set[str] = set()
    for asset in assets:
        table = (f"{connector._regex_literal(asset.schema_name)}"
                 f"\\.{connector._regex_literal(asset.table_name)}")
        for column in _registered_columns(asset):
            entries.add(f"{table}\\.{connector._regex_literal(column)}")
    return sorted(entries)


# ── 续流校验 / 自有资源清理 ─────────────────────────────────────────

def validate_resume(connection, capture_id: str) -> None:
    """副本续流前的最小源端校验。

    CDC 的 ``assert_log_continuity`` 形参（has_offsets/coverage_exists 与
    ``cdc_`` 槽名白名单）与副本 cohort 不匹配，故此处做最小 PG 槽/gap
    检查：持久化偏移在而自有槽丢失/被回收时，重启会从新槽当前位点静默
    跳过未复制区间，必须显式报 gap。MySQL binlog 保留缺口无法廉价预检，
    由子进程错误分类兜底（与 CDC 一致）。
    """
    owned = names(capture_id)
    if getattr(connection, "kind", None) != KIND_POSTGRES:
        return
    if not _OWNED_SLOT.fullmatch(owned["slot"]):
        raise ConnectorError(runtime.CODE_EXECUTOR_INTERNAL)
    engine = _source_check_engine(connection)
    try:
        with engine.connect() as conn:
            status = conn.execute(sa.text(
                "SELECT wal_status FROM pg_replication_slots "
                "WHERE slot_name = :slot"),
                {"slot": owned["slot"]}).scalar_one_or_none()
    except sa.exc.SQLAlchemyError as exc:
        raise connector._driver_error(exc) from None
    finally:
        engine.dispose()
    if status is None or str(status).lower() == "lost":
        raise ConnectorError(runtime.CODE_SOURCE_LOG_GAP)


def cleanup(connection, capture_id: str) -> bool:
    """删除本副本 capture 名下的复制槽与发布；成功返回 True。

    槽仍处于活跃流（子进程未停净）时拒绝删除返回 False，调用方重试；
    名称先过 ``ir_``/``irpub_`` 正则白名单，绝不删除 Sentinel 或任何
    非本 lane 命名的资源。MySQL 无源端资源（server_id/binlog 订阅不
    落盘），无需清理。
    """
    if getattr(connection, "kind", None) != KIND_POSTGRES:
        return True
    owned = names(capture_id)
    slot, publication = owned["slot"], owned["publication"]
    if not (_OWNED_SLOT.fullmatch(slot) and _OWNED_PUBLICATION.fullmatch(publication)):
        raise ConnectorError(runtime.CODE_EXECUTOR_INTERNAL)
    engine = _source_check_engine(connection)
    try:
        with engine.connect().execution_options(
                isolation_level="AUTOCOMMIT") as conn:
            active = conn.execute(sa.text(
                "SELECT active FROM pg_replication_slots WHERE slot_name = :slot"),
                {"slot": slot}).scalar_one_or_none()
            if active is not None:
                if active:
                    return False
                conn.execute(sa.text("SELECT pg_drop_replication_slot(:slot)"),
                             {"slot": slot})
            # 绑定参数不能作标识符；名称已过 [0-9a-f] 白名单后加引号内联。
            conn.execute(sa.text(f'DROP PUBLICATION IF EXISTS "{publication}"'))
    except sa.exc.SQLAlchemyError as exc:
        raise connector._driver_error(exc) from None
    finally:
        engine.dispose()
    logger.info("replica pg cleanup ok")
    return True


# ── Debezium Server application.properties 生成 ─────────────────────

def build_properties(connection, assets, capture_id: str, secret_b64: str,
                     sink_url: str, quarkus_port: int, state_dir: str) -> str:
    """组装副本 capture 子进程的 application.properties（含密钥，绝不写日志）。"""
    kind = getattr(connection, "kind", None)
    if kind not in (KIND_MYSQL, KIND_POSTGRES):
        raise ConnectorError(runtime.CODE_UNSUPPORTED_SOURCE)
    assets = list(assets or [])
    if not assets:
        raise ConnectorError(runtime.CODE_EXECUTOR_INTERNAL)
    url = connector._source_url(connection)
    host = url.host or "127.0.0.1"
    port = url.port or (5432 if kind == KIND_POSTGRES else 3306)
    database = url.database or ""
    if kind == KIND_MYSQL and not database:
        # MySQL 无库名会匹配到实例内全部库，显式拒绝
        raise ConnectorError(runtime.CODE_SOURCE_CONFIG_INVALID)
    owned = names(capture_id)

    lines = [
        "# generated per replica capture; carries runtime secrets",
        "quarkus.http.host=127.0.0.1",
        f"quarkus.http.port={int(quarkus_port)}",
        "debezium.sink.type=http",
        f"debezium.sink.http.url={connector._properties_value(sink_url)}",
        # 背压：接收端 5xx 时子进程按间隔阻塞重发，绝不丢事件；快照量大，
        # 与 CDC 不同这里启用 HTTP 批量 sink。
        "debezium.sink.http.timeout.ms=30000",
        "debezium.sink.http.retries=100000",
        "debezium.sink.http.retry.interval.ms=2000",
        "debezium.sink.http.batch.enabled=true",
        "debezium.sink.http.authentication.type=standard-webhooks",
        "debezium.sink.http.authentication.webhook.secret=" + connector._config_literal(secret_b64),
        # JSON 信封：schema 启用；decimal=precise 时 JSON 载荷为 Kafka
        # Connect Decimal base64（由 replica_codec 精确解码，不经浮点）。
        "debezium.format.key=json",
        "debezium.format.value=json",
        "debezium.format.key.schemas.enable=true",
        "debezium.format.value.schemas.enable=true",
        # Java 扩展 SMT：副本 transport 元数据。
        f"debezium.source.transforms={TRANSPORT_SMT_ALIAS}",
        f"debezium.source.transforms.{TRANSPORT_SMT_ALIAS}.type=" + TRANSPORT_SMT_CLASS,
        # 通知走 sink channel，落副本专属通知话题（键名形状见文件顶部说明）。
        f"{NOTIFICATION_CHANNELS_KEY}={NOTIFICATION_CHANNELS}",
        f"{NOTIFICATION_SINK_TOPIC_KEY}=" + connector._properties_value(
            owned["topic_prefix"] + NOTIFICATION_SINK_TOPIC_SUFFIX),
    ]
    if kind == KIND_POSTGRES:
        lines.append("debezium.source.connector.class=" + connector.DEBEZIUM_POSTGRES_CONNECTOR)
    else:
        lines.append("debezium.source.connector.class=" + connector.DEBEZIUM_MYSQL_CONNECTOR)
    lines += [
        f"debezium.source.topic.prefix={owned['topic_prefix']}",
        f"debezium.source.database.hostname={connector._config_literal(host)}",
        f"debezium.source.database.port={int(port)}",
        f"debezium.source.database.user={connector._config_literal(url.username or '')}",
        f"debezium.source.database.password={connector._config_literal(url.password or '')}",
        # 副本从全量快照起流（initial），再按持久化偏移续流。
        f"debezium.source.snapshot.mode={SNAPSHOT_MODE}",
        f"debezium.source.snapshot.fetch.size={SNAPSHOT_FETCH_SIZE}",
        # 精确类型契约。
        f"debezium.source.decimal.handling.mode={DECIMAL_HANDLING_MODE}",
        f"debezium.source.binary.handling.mode={BINARY_HANDLING_MODE}",
        f"debezium.source.time.precision.mode={TIME_PRECISION_MODE}",
        # 心跳是 active 证据；不配置 heartbeat.action.query（禁止写业务库）。
        f"debezium.source.heartbeat.interval.ms={HEARTBEAT_INTERVAL_MS}",
        # 事务元数据：副本按源事务分组落账（IntegratedReplicaTransaction）。
        "debezium.source.provide.transaction.metadata=true",
        f"debezium.source.max.batch.size={MAX_BATCH_SIZE}",
        f"debezium.source.max.queue.size={MAX_QUEUE_SIZE}",
        # 偏移/模式历史按 capture 持久化；副本偏移落盘更密。
        "debezium.source.offset.storage=org.apache.kafka.connect.storage.FileOffsetBackingStore",
        f"debezium.source.offset.storage.file.filename={connector._properties_value(str(state_dir) + '/offsets.dat')}",
        f"debezium.source.offset.flush.interval.ms={OFFSET_FLUSH_INTERVAL_MS}",
        "debezium.source.schema.history.internal=io.debezium.storage.file.history.FileSchemaHistory",
        f"debezium.source.schema.history.internal.file.filename={connector._properties_value(str(state_dir) + '/dbhistory.dat')}",
        # include 列表是正则：值必须再过一遍 .properties 转义（同 CDC）；
        # 列集取资产注册的 full schema，不做 field_map 投影。
        "debezium.source.table.include.list=" + connector._properties_value(
            ",".join(_table_entries(assets))),
        "debezium.source.column.include.list=" + connector._properties_value(
            ",".join(_column_entries(assets))),
        # 删除只发 DELETE 事件，不发墓碑空值
        "debezium.source.tombstones.on.delete=false",
    ]
    if kind == KIND_MYSQL:
        # 库过滤：只放行资产所在的库（含连接默认库），防跨库误捕获
        schemas = {asset.schema_name for asset in assets}
        schemas.add(database)
        lines += [
            "debezium.source.database.include.list=" + connector._properties_value(
                ",".join(sorted(connector._regex_literal(name) for name in schemas))),
            f"debezium.source.database.server.id={int(owned['server_id'])}",
            "debezium.source.bigint.unsigned.handling.mode=precise",
            # DDL 文本不进入事件流
            "debezium.source.include.schema.changes=false",
        ]
    else:
        lines += [
            f"debezium.source.database.dbname={connector._properties_value(database)}",
            "debezium.source.plugin.name=pgoutput",
            "debezium.source.message.prefix.include.list=oo_replica_cut_" + uuid.UUID(capture_id).hex,
            f"debezium.source.slot.name={owned['slot']}",
            f"debezium.source.publication.name={owned['publication']}",
            # 只为 include 集内的表维护自有发布
            "debezium.source.publication.autocreate.mode=filtered",
        ]
    return "\n".join(lines) + "\n"
