"""关系型数据库 Connector — MySQL / PostgreSQL"""
from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, inspect, text

from app.services.connection.base import ConnectorBase


class SQLConnector(ConnectorBase):
    """
    基于 SQLAlchemy 的关系型数据库 Connector。

    支持两种配置格式：
    1. 连接串格式: {"connection_string": "mysql+pymysql://user:pass@host:3306/db"}
    2. 分字段格式: {"host":"...","port":3306,"user":"...","password":"...","database":"..."}
       (仅 MySQL，自动转为 mysql+pymysql 连接串)

    config 示例:
      {
        "connection_string": "postgresql://user:pass@host:5432/db",
        "query": "SELECT * FROM orders",
        "watermark_column": "updated_at"   # APPEND 模式使用
      }
    """

    # 合法表名/标识符（允许 schema.table 与 $）；API 可控的 resource 只能长这样，
    # 否则 f-string 拼进 SELECT 就是现成的注入面
    _IDENT_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_.$]*$")

    def __init__(self, config: dict):
        self._config = config
        self._engine = None

    @classmethod
    def _safe_ident(cls, resource: str) -> str:
        if not cls._IDENT_RE.match(resource or ""):
            raise ValueError(f"非法表名/标识符: {resource!r}")
        return resource

    @staticmethod
    def _build_connection_string(config: dict) -> str:
        """从分字段格式推导连接串。"""
        cs = config.get("connection_string")
        if cs:
            return cs
        # 从 host/port/user/password/database 构建 MySQL 连接串
        host = config.get("host", "localhost")
        port = config.get("port", 3306)
        user = config.get("user", "root")
        password = config.get("password", "")
        database = config.get("database", "")
        driver = config.get("driver", "mysql+pymysql")
        return f"{driver}://{user}:{password}@{host}:{port}/{database}"

    def _get_engine(self):
        if self._engine is None:
            cs = self._build_connection_string(self._config)
            self._engine = create_engine(
                cs,
                pool_pre_ping=True,
                connect_args={"connect_timeout": 10},
            )
        return self._engine

    def test_connection(self) -> bool:
        try:
            with self._get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def list_resources(self) -> list[str]:
        """返回数据库中的表列表"""
        inspector = inspect(self._get_engine())
        return inspector.get_table_names()

    def introspect_schema(self, resource: str) -> list[dict]:
        """基于数据库元数据反射的列清单（MySQL/PostgreSQL 方言归一化）。"""
        from app.data_channel.connections.type_normalization import (
            normalize_sql_column,
        )

        columns = inspect(self._get_engine()).get_columns(self._safe_ident(resource))
        result = [
            normalize_sql_column(column.get("name"), column.get("type"))
            for column in columns
            if column.get("name")
        ]
        if not result:
            raise ValueError(f"资源 {resource!r} 未反射出任何列")
        return result

    def pull_sample(self, resource: str, limit: int = 100) -> list[dict]:
        """从表中查询样本数据"""
        with self._get_engine().connect() as conn:
            result = conn.execute(
                text(f"SELECT * FROM {self._safe_ident(resource)} LIMIT :limit"),
                {"limit": limit},
            )
            cols = list(result.keys())
            return [dict(zip(cols, row)) for row in result]

    def pull_full(self, resource: str) -> list[dict]:
        """查询表全量数据（兼容 SQLAlchemy 2.0 + pymysql）"""
        query = self._config.get("query") or f"SELECT * FROM {self._safe_ident(resource)}"
        with self._get_engine().connect() as conn:
            result = conn.execute(text(query))
            cols = list(result.keys())
            return [dict(zip(cols, row)) for row in result]

    def pull_delta(self, resource: str, since: str | None = None) -> list[dict]:
        """增量数据查询 (基于 watermark_column)"""
        watermark_col = self._config.get("watermark_column")
        if not watermark_col or not since:
            return self.pull_full(resource)

        base_query = self._config.get("query") or f"SELECT * FROM {self._safe_ident(resource)}"
        # 包装为子查询后追加 WHERE 子句（水位列名同样按标识符白名单校验）
        delta_query = f"""
            SELECT * FROM ({base_query}) _t
            WHERE {self._safe_ident(watermark_col)} > :since
        """
        with self._get_engine().connect() as conn:
            result = conn.execute(text(delta_query), {"since": since})
            cols = list(result.keys())
            return [dict(zip(cols, row)) for row in result]
