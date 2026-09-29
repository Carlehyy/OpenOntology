"""SQLite 持久化（api_hub 私有库，不进平台主库）。

每次操作开一个新连接；生产为单 uvicorn 进程，备份按 docs/operations/
backup-restore.md 连同 api_hub_data 卷整体处理。多进程直写同一文件
不受支持。
"""
import sqlite3

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interfaces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL DEFAULT '未命名接口',
    description   TEXT    NOT NULL DEFAULT '',   -- 给 Agent 看的用途/参数说明（MCP 渐进式披露）
    group_name    TEXT    NOT NULL DEFAULT '',
    method        TEXT    NOT NULL DEFAULT 'GET',
    url           TEXT    NOT NULL DEFAULT '',
    query_params  TEXT    NOT NULL DEFAULT '[]',   -- JSON: [{key,value}]
    headers       TEXT    NOT NULL DEFAULT '[]',   -- JSON: [{key,value}]
    body_type     TEXT    NOT NULL DEFAULT 'none', -- none|json|form|multipart|raw
    body_content  TEXT    NOT NULL DEFAULT '',
    file_fields   TEXT    NOT NULL DEFAULT '[]',   -- JSON: multipart file field definitions
    mcp_enabled   INTEGER NOT NULL DEFAULT 0,
    open_enabled  INTEGER NOT NULL DEFAULT 0,
    http_enabled  INTEGER NOT NULL DEFAULT 0,
    proxy_slug    TEXT    NOT NULL DEFAULT '',
    proxy_query_keys  TEXT NOT NULL DEFAULT '[]',
    proxy_header_keys TEXT NOT NULL DEFAULT '[]',
    proxy_body_enabled INTEGER NOT NULL DEFAULT 0,
    proxy_body_keys TEXT NOT NULL DEFAULT '[]',
    parameter_schema TEXT NOT NULL DEFAULT '[]', -- JSON: [{name,location,type,...}]
    config_revision INTEGER NOT NULL DEFAULT 1,
    created_by      TEXT    NOT NULL DEFAULT '',
    updated_by      TEXT    NOT NULL DEFAULT '',
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    interface_id     INTEGER NOT NULL,
    ok               INTEGER NOT NULL DEFAULT 0,
    status_code      INTEGER,
    elapsed_ms       INTEGER,
    request_snapshot TEXT,
    response_headers TEXT,
    response_body    TEXT,
    error            TEXT,
    relogin          INTEGER NOT NULL DEFAULT 0,
    source            TEXT    NOT NULL DEFAULT 'ui',
    proxy_key_id      INTEGER,
    proxy_key_name    TEXT,
    source_ip         TEXT,
    created_at       TEXT    NOT NULL,
    FOREIGN KEY (interface_id) REFERENCES interfaces(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS proxy_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    key_prefix    TEXT    NOT NULL,
    key_hash      TEXT    NOT NULL UNIQUE,
    enabled       INTEGER NOT NULL DEFAULT 1,
    valid_from    TEXT,
    expires_at    TEXT,
    scope_all     INTEGER NOT NULL DEFAULT 0,
    last_used_at  TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS proxy_key_interfaces (
    key_id        INTEGER NOT NULL,
    interface_id  INTEGER NOT NULL,
    PRIMARY KEY (key_id, interface_id),
    FOREIGN KEY (key_id) REFERENCES proxy_keys(id) ON DELETE CASCADE,
    FOREIGN KEY (interface_id) REFERENCES interfaces(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.DB_PATH,
        timeout=config.SQLITE_BUSY_TIMEOUT_MS / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {config.SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        # WAL lets UI/history reads proceed while a short audit write is in
        # flight.  It remains a single-file SQLite deployment and is therefore
        # deliberately only a bridge until PostgreSQL is configured.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_SCHEMA)
        _migrate(conn)


def _migrate(conn) -> None:
    """对老版本 app.db 做无损升级：缺列就补。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(interfaces)").fetchall()}
    if "mcp_enabled" not in cols:
        conn.execute("ALTER TABLE interfaces ADD COLUMN mcp_enabled INTEGER NOT NULL DEFAULT 0")
    if "description" not in cols:
        conn.execute("ALTER TABLE interfaces ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    if "open_enabled" not in cols:
        conn.execute("ALTER TABLE interfaces ADD COLUMN open_enabled INTEGER NOT NULL DEFAULT 0")
        # 老数据兼容：已有 mcp_enabled=1 的接口，自动设为 open_enabled=1
        conn.execute("UPDATE interfaces SET open_enabled = 1 WHERE mcp_enabled = 1")
    if "http_enabled" not in cols:
        conn.execute("ALTER TABLE interfaces ADD COLUMN http_enabled INTEGER NOT NULL DEFAULT 0")
    if "proxy_slug" not in cols:
        conn.execute("ALTER TABLE interfaces ADD COLUMN proxy_slug TEXT NOT NULL DEFAULT ''")
    if "proxy_query_keys" not in cols:
        conn.execute("ALTER TABLE interfaces ADD COLUMN proxy_query_keys TEXT NOT NULL DEFAULT '[]'")
    if "proxy_header_keys" not in cols:
        conn.execute("ALTER TABLE interfaces ADD COLUMN proxy_header_keys TEXT NOT NULL DEFAULT '[]'")
    if "proxy_body_enabled" not in cols:
        conn.execute(
            "ALTER TABLE interfaces ADD COLUMN proxy_body_enabled INTEGER NOT NULL DEFAULT 0"
        )
    if "proxy_body_keys" not in cols:
        conn.execute(
            "ALTER TABLE interfaces ADD COLUMN proxy_body_keys TEXT NOT NULL DEFAULT '[]'"
        )
    if "file_fields" not in cols:
        conn.execute(
            "ALTER TABLE interfaces ADD COLUMN file_fields TEXT NOT NULL DEFAULT '[]'"
        )
    if "parameter_schema" not in cols:
        conn.execute(
            "ALTER TABLE interfaces ADD COLUMN parameter_schema TEXT NOT NULL DEFAULT '[]'"
        )
    if "config_revision" not in cols:
        conn.execute(
            "ALTER TABLE interfaces ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 1"
        )
    if "created_by" not in cols:
        conn.execute(
            "ALTER TABLE interfaces ADD COLUMN created_by TEXT NOT NULL DEFAULT ''"
        )
    if "updated_by" not in cols:
        conn.execute(
            "ALTER TABLE interfaces ADD COLUMN updated_by TEXT NOT NULL DEFAULT ''"
        )

    run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "source" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN source TEXT NOT NULL DEFAULT 'ui'")
    if "proxy_key_id" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN proxy_key_id INTEGER")
    if "proxy_key_name" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN proxy_key_name TEXT")
    if "source_ip" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN source_ip TEXT")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS proxy_keys (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL,
            key_prefix    TEXT    NOT NULL,
            key_hash      TEXT    NOT NULL UNIQUE,
            enabled       INTEGER NOT NULL DEFAULT 1,
            valid_from    TEXT,
            expires_at    TEXT,
            scope_all     INTEGER NOT NULL DEFAULT 0,
            last_used_at  TEXT,
            created_at    TEXT    NOT NULL,
            updated_at    TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS proxy_key_interfaces (
            key_id        INTEGER NOT NULL,
            interface_id  INTEGER NOT NULL,
            PRIMARY KEY (key_id, interface_id),
            FOREIGN KEY (key_id) REFERENCES proxy_keys(id) ON DELETE CASCADE,
            FOREIGN KEY (interface_id) REFERENCES interfaces(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_interfaces_active_proxy_slug
        ON interfaces(proxy_slug)
        WHERE http_enabled = 1 AND proxy_slug <> '';
        CREATE INDEX IF NOT EXISTS idx_proxy_keys_hash ON proxy_keys(key_hash);
        CREATE INDEX IF NOT EXISTS idx_runs_proxy_key_id ON runs(proxy_key_id);
        """
    )

