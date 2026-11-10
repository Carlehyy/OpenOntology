"""CDC 运行时共享错误码与子进程输出分类（纯函数，无 IO、无日志）。

``cdc_connector``（方言预检）与 ``cdc_executor``（监督者）共用这些稳定
错误码回报给控制面；放在独立小模块里只是为了避免两者互相 import。
状态词表（starting/active/paused/error/gap/stopped）归控制面所有，这里
不重复定义。
"""
from __future__ import annotations

# 源端日志缺口：记录位点已不在 WAL/binlog 保留范围。必须显式呈现为 gap，
# 绝不以重新快照或静默跳过兜底。
CODE_SOURCE_LOG_GAP = "SOURCE_LOG_GAP"
CODE_SOURCE_AUTH_FAILED = "SOURCE_AUTH_FAILED"
CODE_SOURCE_UNREACHABLE = "SOURCE_UNREACHABLE"
CODE_SOURCE_CONFIG_INVALID = "SOURCE_CONFIG_INVALID"
CODE_SOURCE_CONFIG_UNAVAILABLE = "SOURCE_CONFIG_UNAVAILABLE"
CODE_UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
# 首版只支持 [A-Za-z0-9_]+ 的库/表/列标识符；需要引号的标识符显式不支持。
CODE_IDENTIFIER_UNSUPPORTED = "IDENTIFIER_UNSUPPORTED"
CODE_BINLOG_DISABLED = "BINLOG_DISABLED"
CODE_BINLOG_FORMAT_NOT_ROW = "BINLOG_FORMAT_NOT_ROW"
CODE_BINLOG_ROW_IMAGE_NOT_FULL = "BINLOG_ROW_IMAGE_NOT_FULL"
CODE_REPLICATION_PRIVILEGE_MISSING = "REPLICATION_PRIVILEGE_MISSING"
# MySQL no_data 模式快照的全局读锁（FLUSH TABLES ... WITH READ LOCK）需要
# RELOAD（或动态权限 FLUSH_TABLES）；缺它在 3.7.1 中表现为可重试
# SQLException 的无限内部重试：进程存活、健康 UP、但永远到不了流式阶段。
CODE_SNAPSHOT_LOCK_PRIVILEGE_MISSING = "SNAPSHOT_LOCK_PRIVILEGE_MISSING"
CODE_WAL_LEVEL_NOT_LOGICAL = "WAL_LEVEL_NOT_LOGICAL"
CODE_REPLICA_IDENTITY_FULL_REQUIRED = "REPLICA_IDENTITY_FULL_REQUIRED"
CODE_SCHEMA_DRIFT = "SCHEMA_DRIFT"
CODE_PG_CLEANUP_FAILED = "PG_CLEANUP_FAILED"
CODE_EXECUTOR_CAPACITY = "EXECUTOR_CAPACITY"
CODE_EXECUTOR_CHILD_EXITED = "EXECUTOR_CHILD_EXITED"
CODE_EXECUTOR_NO_EVIDENCE = "EXECUTOR_NO_EVIDENCE"
CODE_EXECUTOR_INTERNAL = "EXECUTOR_INTERNAL"

# 特征串全部取自数据库/Debezium 的错误原文，且不会出现在正常 INFO 日志里
# （例如不匹配宽泛的 "binlog position"，那是 MySQL 连接成功时的常规输出）。
_GAP_SIGNATURES = (
    # Debezium：记录位点已不在源端日志保留范围（MySQL/PostgreSQL 同一措辞）
    "no longer available on the server",
    # PostgreSQL：requested WAL segment ... has already been removed
    "has already been removed",
    # PostgreSQL：槽因超出 max_slot_wal_keep_size 被判失效
    "can no longer get changes from replication slot",
    # MySQL 1236：binlog 已被清理
    "could not find first log file name in binary log index file",
    "purged required binary logs",
)
_AUTH_SIGNATURES = (
    "access denied for user",
    "password authentication failed",
    "no pg_hba.conf entry",
)
_UNREACHABLE_SIGNATURES = (
    "connection refused",
    "unknownhostexception",
    "no route to host",
    "connect timed out",
    "connection timed out",
    "could not translate host name",
)
_CONFIG_SIGNATURES = (
    "unknown database",
    "does not exist",  # database "x" does not exist（槽不存在已在上方按缺口处理）
)

_PRIORITY = (CODE_SOURCE_LOG_GAP, CODE_SOURCE_AUTH_FAILED,
             CODE_SOURCE_CONFIG_INVALID, CODE_SOURCE_UNREACHABLE)


def classify_child_output(text: str) -> str | None:
    """把一段子进程输出/驱动异常文本映射为稳定错误码；无匹配返回 None。

    入参文本只用于匹配，调用方不得把它写入日志（可能含源值或凭据）。
    """
    lowered = (text or "").lower()
    if not lowered.strip():
        return None
    if any(signature in lowered for signature in _GAP_SIGNATURES):
        return CODE_SOURCE_LOG_GAP
    if "replication slot" in lowered and "does not exist" in lowered:
        return CODE_SOURCE_LOG_GAP
    # 权限型拒绝（"Access denied; you need ... privilege"）与登录认证失败
    # （"Access denied for user"）是两种措辞，先判权限型，避免误报认证失败
    if "access denied; you need" in lowered:
        return CODE_REPLICATION_PRIVILEGE_MISSING
    if any(signature in lowered for signature in _AUTH_SIGNATURES):
        return CODE_SOURCE_AUTH_FAILED
    if any(signature in lowered for signature in _UNREACHABLE_SIGNATURES):
        return CODE_SOURCE_UNREACHABLE
    if "database" in lowered and any(
            signature in lowered for signature in _CONFIG_SIGNATURES):
        return CODE_SOURCE_CONFIG_INVALID
    return None


def merge_error_codes(existing: str | None, incoming: str | None) -> str | None:
    """保留优先级最高的分类码：日志缺口不可被后续的网络噪声覆盖。"""
    if incoming is None:
        return existing
    if existing is None:
        return incoming

    def rank(code: str) -> int:
        return _PRIORITY.index(code) if code in _PRIORITY else len(_PRIORITY)

    return min((existing, incoming), key=rank)
