"""integrated CDC 方言适配：预检、前沿捕获、续流校验、Debezium 配置生成。

监督者（``cdc_executor.py``）通过本模块与 MySQL/PostgreSQL 源交互：

- 连接配置与引擎复用 ``native`` 的既有实现（同一条解密与 URL 归一路径），
  任何失败只携带稳定错误码，绝不把主机/账号/SQL/源值带入日志；
- 物理身份摘要归控制面（``cdc_control.source_identity_from_config``）所有，
  本模块不做第二套；凭据轮换/订阅增删不换代（generation），偏移永不静默清零；
- 复制槽/发布/服务 ID/话题前缀按（连接[, 代]）确定性派生；删除资源前按
  正则白名单校验名称，只会删除本执行器自己创建的资源；
- 模式漂移用与资产注册/变更相同的 ``native.reflect`` 指纹做精确比较；
- 生成的 ``application.properties`` 由监督者写入 0600 临时文件（含
  standard-webhooks 密钥与源凭据），进程停止即删除。

首版标识符支持范围：``[A-Za-z0-9_]+`` 的库/表/列（include 列表按正则语义
逐段转义）；需要引号的标识符显式报错拒绝，不做静默支持。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

import sqlalchemy as sa

from app.data_channel.integrated import cdc_runtime as runtime
from app.data_channel.integrated import native

logger = logging.getLogger(__name__)

KIND_MYSQL = "mysql"
KIND_POSTGRES = "postgres"

DEBEZIUM_MYSQL_CONNECTOR = "io.debezium.connector.mysql.MySqlConnector"
DEBEZIUM_POSTGRES_CONNECTOR = "io.debezium.connector.postgresql.PostgresConnector"

# 与 cdc_port/cdc_control 冻结的激活前沿形状一致：
# MySQL {"dialect":"mysql","file":str,"pos":int,"row":int}
# PostgreSQL {"dialect":"postgresql","lsn":int,"data_collection_order":int}
MYSQL_FRONTIER_ROW = 0
POSTGRES_FRONTIER_ORDER = 0

# 心跳即 active 的流证据：1s 间隔让就绪判定在数秒内完成。不配置
# heartbeat.action.query（禁止对业务库写入）；源端流完全空闲时可能暂无
# 心跳，此时如实停留在 starting。
HEARTBEAT_INTERVAL_MS = 1000

_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")
_OWNED_SLOT = re.compile(r"cdc_[0-9a-f]{16}")
_OWNED_PUBLICATION = re.compile(r"cdcpub_[0-9a-f]{12}")


class ConnectorError(RuntimeError):
    """携带稳定错误码（cdc_runtime.CODE_*）的方言错误；正文即错误码。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _driver_error(exc: BaseException) -> ConnectorError:
    """驱动异常只做分类，不记录原文（可能含源值/凭据）。"""
    original = getattr(exc, "orig", None) or exc
    code = runtime.classify_child_output(str(original))
    return ConnectorError(code or runtime.CODE_SOURCE_UNREACHABLE)


def _source_url(connection):
    """复用 native 的解密 + URL 归一（读取引擎 URL 即全部连接要素）。"""
    try:
        engine = native.source_engine(connection)
    except Exception:
        raise ConnectorError(runtime.CODE_SOURCE_CONFIG_UNAVAILABLE) from None
    try:
        return engine.url
    finally:
        engine.dispose()


def _identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(str(value or "")):
        raise ConnectorError(runtime.CODE_IDENTIFIER_UNSUPPORTED)
    return str(value)


# ── 确定性运行时命名 ────────────────────────────────────────────────

def runtime_names(connection_id: str, generation: int) -> dict:
    """复制槽/发布按（连接, 代）派生：换代即新名。

    MySQL server id 按连接派生（跨代稳定，副本身份不变）；话题前缀按
    （连接, 代）派生，避免物理重定向复用旧前缀造成源标识冲突。
    """
    def digest(seed: str) -> str:
        return hashlib.sha256(seed.encode()).hexdigest()

    return {
        "slot": "cdc_" + digest(f"{connection_id}|{generation}|slot")[:16],
        "publication": "cdcpub_" + digest(f"{connection_id}|{generation}|publication")[:12],
        "server_id": int(digest(f"{connection_id}|server-id")[:8], 16) % 4294967294 + 1,
        "topic_prefix": "cdc_" + digest(f"{connection_id}|{generation}|topic")[:12],
    }


# ── 预检：源能力 + 精确模式漂移 ─────────────────────────────────────

def preflight(connection, subscriptions) -> None:
    kind = getattr(connection, "kind", None)
    if kind not in (KIND_MYSQL, KIND_POSTGRES):
        raise ConnectorError(runtime.CODE_UNSUPPORTED_SOURCE)
    try:
        engine = native.source_engine(connection)
    except Exception:
        raise ConnectorError(runtime.CODE_SOURCE_CONFIG_UNAVAILABLE) from None
    try:
        with engine.connect() as conn:
            if kind == KIND_MYSQL:
                _preflight_mysql(conn)
            else:
                _preflight_postgres(conn, subscriptions)
            # 精确漂移：与资产注册相同的 native 反射指纹。
            for subscription in subscriptions:
                _check_schema_drift(conn, subscription)
    except ConnectorError:
        raise
    except sa.exc.NoSuchTableError:
        raise ConnectorError(runtime.CODE_SCHEMA_DRIFT) from None
    except sa.exc.SQLAlchemyError as exc:
        raise _driver_error(exc) from None
    finally:
        engine.dispose()
    logger.info("cdc preflight ok kind=%s tables=%s", kind, len(subscriptions))


def mysql_grant_gap(grants_text: str) -> str | None:
    """MySQL ``no_data`` 快照 + 流式所需权限缺口；返回稳定错误码或 None。

    与 Debezium Server 3.7.1 实际行为对齐（BinlogSnapshotChangeEventSource）：
    全局读锁 ``FLUSH TABLES WITH READ LOCK`` 与表级回退 ``FLUSH TABLES ...
    WITH READ LOCK`` 都要求 RELOAD（或动态权限 FLUSH_TABLES）——仅
    LOCK TABLES 无法通过回退路径；流式要求 REPLICATION SLAVE/CLIENT。
    ``ALL PRIVILEGES ON *.*`` 才等价全局权限，库级 ALL 不含 RELOAD。
    SHOW DATABASES 按官方文档建议提供，但本平台按显式库表寻址，缺失不致命。
    """
    grants = (grants_text or "").upper()
    if "ALL PRIVILEGES ON *.*" in grants or "ALL ON *.*" in grants:
        return None
    if "REPLICATION SLAVE" not in grants:
        return runtime.CODE_REPLICATION_PRIVILEGE_MISSING
    if "REPLICATION CLIENT" not in grants:
        return runtime.CODE_REPLICATION_PRIVILEGE_MISSING
    if "RELOAD" not in grants and "FLUSH_TABLES" not in grants:
        return runtime.CODE_SNAPSHOT_LOCK_PRIVILEGE_MISSING
    return None


def _preflight_mysql(conn) -> None:
    row = conn.execute(sa.text(
        "SELECT @@log_bin, @@binlog_format, @@binlog_row_image")).one()
    if not int(row[0] or 0):
        raise ConnectorError(runtime.CODE_BINLOG_DISABLED)
    if str(row[1] or "").upper() != "ROW":
        raise ConnectorError(runtime.CODE_BINLOG_FORMAT_NOT_ROW)
    if str(row[2] or "").upper() != "FULL":
        raise ConnectorError(runtime.CODE_BINLOG_ROW_IMAGE_NOT_FULL)
    grants = "\n".join(str(item[0]) for item in conn.execute(
        sa.text("SHOW GRANTS FOR CURRENT_USER()")))
    gap = mysql_grant_gap(grants)
    if gap is not None:
        raise ConnectorError(gap)


def _preflight_postgres(conn, subscriptions) -> None:
    wal_level = str(conn.execute(sa.text("SHOW wal_level")).scalar_one()).lower()
    if wal_level != "logical":
        raise ConnectorError(runtime.CODE_WAL_LEVEL_NOT_LOGICAL)
    # 更新事件需要完整前像：REPLICA IDENTITY FULL 强制要求（PK 默认只回放
    # 主键，会丢失非键列的前像谓词）。
    for subscription in subscriptions:
        schema = _identifier(subscription.schema_name, "schema")
        table = _identifier(subscription.table_name, "table")
        replident = conn.execute(sa.text(
            "SELECT c.relreplident FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :table"),
            {"schema": schema, "table": table}).scalar_one_or_none()
        if replident is None:
            raise ConnectorError(runtime.CODE_SCHEMA_DRIFT)
        if str(replident) != "f":
            raise ConnectorError(runtime.CODE_REPLICA_IDENTITY_FULL_REQUIRED)


def _check_schema_drift(conn, subscription) -> None:
    try:
        _, _, fingerprint = native.reflect(conn, subscription)
    except sa.exc.NoSuchTableError:
        raise ConnectorError(runtime.CODE_SCHEMA_DRIFT) from None
    except sa.exc.SQLAlchemyError as exc:
        raise _driver_error(exc) from None
    if fingerprint != subscription.schema_fingerprint:
        raise ConnectorError(runtime.CODE_SCHEMA_DRIFT)


# ── 前沿捕获 / 续流校验 / 自有资源清理 ──────────────────────────────

def capture_frontier(connection) -> dict:
    """订阅激活前沿。

    由监督者在锚定时机调用（``_maybe_anchor``）：新子进程已带期望 include
    集起流、接收器确认过真实流证据且健康探测显式 UP 之后，才捕获当前位点
    并激活 starting 订阅——覆盖从不宣称早于实际起流。不再存在“重启前先捕
    获”的旧顺序。
    """
    kind = getattr(connection, "kind", None)
    try:
        engine = native.source_engine(connection)
    except Exception:
        raise ConnectorError(runtime.CODE_SOURCE_CONFIG_UNAVAILABLE) from None
    try:
        with engine.connect() as conn:
            if kind == KIND_POSTGRES:
                # pg_wal_lsn_diff(当前 LSN, '0/0') = (高 32 位 << 32) + 低 32 位
                lsn = int(conn.execute(sa.text(
                    "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint")
                ).scalar_one())
                return {"dialect": "postgresql", "lsn": lsn,
                        "data_collection_order": POSTGRES_FRONTIER_ORDER}
            try:
                row = conn.execute(sa.text("SHOW BINARY LOG STATUS")).one()
            except sa.exc.DBAPIError:
                # MySQL ≤ 8.3 的语句名；8.4+ 使用上面那条
                row = conn.execute(sa.text("SHOW MASTER STATUS")).one()
            return {"dialect": "mysql", "file": str(row[0]),
                    "pos": int(row[1]), "row": MYSQL_FRONTIER_ROW}
    except ConnectorError:
        raise
    except sa.exc.SQLAlchemyError as exc:
        raise _driver_error(exc) from None
    finally:
        engine.dispose()


def assert_log_continuity(connection, names: dict, *, has_offsets: bool,
                          coverage_exists: bool) -> None:
    """续流前校验：声称过覆盖而本地偏移缺失/为空 = 显式日志缺口。

    任意方言都适用：此时以 no_data 从当前位点重启会静默跳过已声称的覆盖
    区间。PostgreSQL 另外校验自有槽存在且未失效——槽被回收后重启同样会
    从新槽当前位点静默跳过，必须显式报 gap 而不是续接。普通重启（偏移与
    槽俱在）原样续流。MySQL 的 binlog 保留缺口无法廉价预检，由子进程
    错误分类兜底为 gap。
    """
    kind = getattr(connection, "kind", None)
    if coverage_exists and not has_offsets:
        raise ConnectorError(runtime.CODE_SOURCE_LOG_GAP)
    if kind != KIND_POSTGRES or not has_offsets:
        return
    if not _OWNED_SLOT.fullmatch(names.get("slot", "")):
        raise ConnectorError(runtime.CODE_EXECUTOR_INTERNAL)
    try:
        engine = native.source_engine(connection)
    except Exception:
        raise ConnectorError(runtime.CODE_SOURCE_CONFIG_UNAVAILABLE) from None
    try:
        with engine.connect() as conn:
            slot = conn.execute(sa.text(
                "SELECT wal_status FROM pg_replication_slots "
                "WHERE slot_name = :slot"), {"slot": names["slot"]}).scalar_one_or_none()
    except sa.exc.SQLAlchemyError as exc:
        raise _driver_error(exc) from None
    finally:
        engine.dispose()
    if slot is None or str(slot).lower() == "lost":
        raise ConnectorError(runtime.CODE_SOURCE_LOG_GAP)


def drop_owned_pg_resources(connection, slot: str, publication: str) -> bool:
    """删除本执行器派生名下的复制槽与发布；成功返回 True。

    槽仍处于活跃流（子进程未停净）时拒绝删除返回 False，调用方重试；
    名称先过正则白名单，绝不删除任何非本执行器命名的资源。
    """
    if getattr(connection, "kind", None) != KIND_POSTGRES:
        return True
    if not (_OWNED_SLOT.fullmatch(slot) and _OWNED_PUBLICATION.fullmatch(publication)):
        raise ConnectorError(runtime.CODE_EXECUTOR_INTERNAL)
    try:
        engine = native.source_engine(connection)
    except Exception:
        raise ConnectorError(runtime.CODE_SOURCE_CONFIG_UNAVAILABLE) from None
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
        raise _driver_error(exc) from None
    finally:
        engine.dispose()
    logger.info("cdc pg cleanup ok")
    return True


# ── Debezium Server application.properties 生成 ─────────────────────

def desired_digest(*, connection, subscriptions, state_dir, names,
                   quarkus_port: int) -> str:
    """期望态指纹：决定子进程是否需要（同代）重启。

    覆盖源连接要素（含口令——凭据轮换必须重启）、include 集、状态目录与
    方言命名；刻意不含每运行期随机的 sink 密钥。只在进程内计算，不落日志。
    """
    url = _source_url(connection)
    material = {
        "url": f"{url.drivername}://{url.username or ''}:{url.password or ''}"
               f"@{url.host or ''}:{url.port or ''}/{url.database or ''}",
        "tables": _table_entries(subscriptions),
        "columns": _column_entries(subscriptions),
        "state_dir": str(state_dir), "topic_prefix": names["topic_prefix"],
        "slot": names["slot"], "publication": names["publication"],
        "server_id": names["server_id"], "quarkus_port": int(quarkus_port),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True).encode()).hexdigest()


def _properties_value(value: str) -> str:
    """Java ``.properties`` 值转义；输出纯 ASCII，兼容 ISO-8859-1/UTF-8 两种
    加载路径（\\、控制字符、非 ASCII 用 \\uXXXX，含代理对）。"""
    out: list[str] = []
    for index, char in enumerate(str(value)):
        if char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif char == "\f":
            out.append("\\f")
        elif char == " " and index == 0:
            out.append("\\ ")
        elif " " <= char <= "~":
            out.append(char)
        else:
            code = ord(char)
            if code > 0xFFFF:  # 代理对
                code -= 0x10000
                out.append(f"\\u{0xD800 + (code >> 10):04x}")
                out.append(f"\\u{0xDC00 + (code & 0x3FF):04x}")
            else:
                out.append(f"\\u{code:04x}")
    return "".join(out)


def _config_literal(value: str) -> str:
    """凭据等自由文本：先做 SmallRye 表达式转义（``$`` -> ``\\$``），
    再做 .properties 转义（文件中呈现为 ``\\\\$``），防止 ``${...}`` 被配置
    展开引擎吞掉。"""
    return _properties_value(str(value).replace("$", "\\$"))


def _regex_literal(value: str) -> str:
    """include 列表是正则：标识符经白名单后逐字符转义，分隔点转义为字面量。"""
    return re.escape(_identifier(value, "identifier"))


def _table_entries(subscriptions) -> list[str]:
    entries = {
        f"{_regex_literal(item.schema_name)}\\.{_regex_literal(item.table_name)}"
        for item in subscriptions}
    return sorted(entries)


def _column_entries(subscriptions) -> list[str]:
    entries: set[str] = set()
    for item in subscriptions:
        table = (f"{_regex_literal(item.schema_name)}"
                 f"\\.{_regex_literal(item.table_name)}")
        columns = set(item.primary_key_columns or [])
        columns.update((item.field_map or {}).keys())
        for column in columns:
            entries.add(f"{table}\\.{_regex_literal(column)}")
    return sorted(entries)


def build_properties(*, connection, subscriptions, secret_b64: str,
                     sink_url: str, quarkus_port: int, state_dir: str,
                     names: dict) -> str:
    """组装单个子进程的 application.properties（含密钥，绝不写日志）。"""
    kind = getattr(connection, "kind", None)
    if kind not in (KIND_MYSQL, KIND_POSTGRES):
        raise ConnectorError(runtime.CODE_UNSUPPORTED_SOURCE)
    url = _source_url(connection)
    host = url.host or "127.0.0.1"
    port = url.port or (5432 if kind == KIND_POSTGRES else 3306)
    database = url.database or ""
    if kind == KIND_MYSQL and not database:
        # MySQL 无库名会匹配到实例内全部库，显式拒绝
        raise ConnectorError(runtime.CODE_SOURCE_CONFIG_INVALID)

    lines = [
        "# generated per (connection, generation); carries runtime secrets",
        "quarkus.http.host=127.0.0.1",
        f"quarkus.http.port={int(quarkus_port)}",
        "debezium.sink.type=http",
        f"debezium.sink.http.url={_properties_value(sink_url)}",
        # 背压：接收端 5xx 时子进程按间隔阻塞重发，绝不丢事件
        "debezium.sink.http.timeout.ms=30000",
        "debezium.sink.http.retries=100000",
        "debezium.sink.http.retry.interval.ms=2000",
        "debezium.sink.http.batch.enabled=false",
        "debezium.sink.http.authentication.type=standard-webhooks",
        "debezium.sink.http.authentication.webhook.secret=" + _config_literal(secret_b64),
        # JSON 信封：schema 启用 + decimal 字符串化（精确值不经浮点）
        "debezium.format.key=json",
        "debezium.format.value=json",
        "debezium.format.key.schemas.enable=true",
        "debezium.format.value.schemas.enable=true",
    ]
    if kind == KIND_POSTGRES:
        lines.append("debezium.source.connector.class=" + DEBEZIUM_POSTGRES_CONNECTOR)
    else:
        lines.append("debezium.source.connector.class=" + DEBEZIUM_MYSQL_CONNECTOR)
    lines += [
        f"debezium.source.topic.prefix={names['topic_prefix']}",
        f"debezium.source.database.hostname={_config_literal(host)}",
        f"debezium.source.database.port={int(port)}",
        f"debezium.source.database.user={_config_literal(url.username or '')}",
        f"debezium.source.database.password={_config_literal(url.password or '')}",
        # 无历史快照：绝不回填；无偏移从当前位点起流
        "debezium.source.snapshot.mode=no_data",
        "debezium.source.decimal.handling.mode=string",
        # 心跳是 active 证据；不配置 heartbeat.action.query（禁止写业务库）
        f"debezium.source.heartbeat.interval.ms={HEARTBEAT_INTERVAL_MS}",
        "debezium.source.provide.transaction.metadata=true",
        # 偏移/模式历史按（连接, 代）持久化：订阅增删、凭据轮换同代续用
        "debezium.source.offset.storage=org.apache.kafka.connect.storage.FileOffsetBackingStore",
        f"debezium.source.offset.storage.file.filename={_properties_value(str(state_dir) + '/offsets.dat')}",
        "debezium.source.offset.flush.interval.ms=5000",
        "debezium.source.schema.history.internal=io.debezium.storage.file.history.FileSchemaHistory",
        f"debezium.source.schema.history.internal.file.filename={_properties_value(str(state_dir) + '/dbhistory.dat')}",
        # include 列表是正则：值必须再过一遍 .properties 转义，否则文件中的
        # `\.` 会被 Java 加载器吞掉反斜杠，退化成匹配任意字符的 `.`
        "debezium.source.table.include.list=" + _properties_value(
            ",".join(_table_entries(subscriptions))),
        "debezium.source.column.include.list=" + _properties_value(
            ",".join(_column_entries(subscriptions))),
        # 删除只发 DELETE 事件，不发墓碑空值
        "debezium.source.tombstones.on.delete=false",
    ]
    if kind == KIND_MYSQL:
        # 库过滤：只放行订阅所在的库（含连接默认库），防跨库误捕获
        schemas = {sub.schema_name for sub in subscriptions}
        schemas.add(database)
        lines += [
            "debezium.source.database.include.list=" + _properties_value(
                ",".join(sorted(_regex_literal(name) for name in schemas))),
            f"debezium.source.database.server.id={int(names['server_id'])}",
            # DDL 文本不进入事件流
            "debezium.source.include.schema.changes=false",
        ]
    else:
        lines += [
            f"debezium.source.database.dbname={_properties_value(database)}",
            "debezium.source.plugin.name=pgoutput",
            f"debezium.source.slot.name={names['slot']}",
            f"debezium.source.publication.name={names['publication']}",
            # 只为 include 集内的表维护自有发布
            "debezium.source.publication.autocreate.mode=filtered",
        ]
    return "\n".join(lines) + "\n"
