"""0152 WeKnora 原生 MCP 镜像列与每用户集成状态迁移。"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tests.migrations.latest_head import LATEST_HEAD

BACKEND = Path(__file__).resolve().parents[2]

SERVERS = "super_assistant_mcp_servers"
INTEGRATION = "super_assistant_weknora_integrations"
UNIQUE = "uq_sa_mcp_owner_weknora_endpoint"

INTEGRATION_COLUMNS = {
    "owner_id", "origin", "initialized_at", "default_knowledge_base_id",
    "default_endpoint_id", "last_error", "updated_at",
}

# 模型与静态 DDL 的空性契约：origin/owner/updated_at 非空，其余可空
INTEGRATION_NULLABLE = {
    "owner_id": False, "origin": False, "updated_at": False,
    "initialized_at": True, "default_knowledge_base_id": True,
    "default_endpoint_id": True, "last_error": True,
}


def _assert_integration_shape(engine) -> None:
    """fresh create_all 与静态 DDL 两条路径的集成表形态一致性。"""
    inspector = inspect(engine)
    columns = {
        column["name"]: column for column in inspector.get_columns(INTEGRATION)
    }
    assert set(columns) == INTEGRATION_COLUMNS
    for name, nullable in INTEGRATION_NULLABLE.items():
        assert columns[name]["nullable"] is nullable, name
    assert (
        inspector.get_pk_constraint(INTEGRATION)["constrained_columns"]
        == ["owner_id"]
    )


def _config(db_path: Path) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _engine(cfg: Config):
    return create_engine(cfg.get_main_option("sqlalchemy.url"))


def _unique_names(engine, table: str) -> set[str]:
    """与迁移同口径的具名唯一对象视图：SQLite 表内约束走 get_unique_constraints，
    独立唯一索引走 get_indexes，取并集。"""
    inspector = inspect(engine)
    return (
        {idx["name"] for idx in inspector.get_indexes(table)}
        | {uc["name"] for uc in inspector.get_unique_constraints(table)}
    )


def _insert_server(
    connection,
    server_id: str,
    owner_id: str,
    name: str,
    *,
    endpoint_id: str | None = None,
    scope: str | None = None,
    with_weknora: bool = True,
) -> None:
    columns = (
        "id, owner_id, name, display_name, description, transport, url, "
        "header_names, args, env_names, enabled, require_confirmation, "
        "tool_manifest, constrained, created_at, updated_at"
    )
    values = (
        ":id, :owner_id, :name, :name, '', 'streamable_http', "
        "'https://example.invalid/mcp', '[]', '[]', '[]', 1, 1, '[]', 0, "
        "'2026-11-19 00:00:00', '2026-11-19 00:00:00'"
    )
    params: dict[str, object] = {"id": server_id, "owner_id": owner_id, "name": name}
    if with_weknora:
        columns += ", weknora_endpoint_id, weknora_scope"
        values += ", :endpoint_id, :scope"
        params["endpoint_id"] = endpoint_id
        params["scope"] = scope
    connection.execute(
        text(f"INSERT INTO {SERVERS} ({columns}) VALUES ({values})"), params,
    )


def test_upgrade_adds_columns_unique_and_integration_table(tmp_path, monkeypatch):
    """fresh 链（0003 create_all + 0152）收敛出镜像列、具名唯一与集成表。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _config(tmp_path / "weknora-upgrade.db")
    command.upgrade(cfg, "head")

    engine = _engine(cfg)
    inspector = inspect(engine)
    server_columns = {
        column["name"] for column in inspector.get_columns(SERVERS)
    }
    assert "weknora_endpoint_id" in server_columns
    assert "weknora_scope" in server_columns
    # 具名唯一存在且唯一（fresh 路径来自 create_all 表内约束）
    found = [name for name in _unique_names(engine, SERVERS) if name == UNIQUE]
    assert found == [UNIQUE]

    assert INTEGRATION in inspector.get_table_names()
    _assert_integration_shape(engine)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == LATEST_HEAD
    engine.dispose()


def test_owner_endpoint_uniqueness_allows_multi_owner_and_multi_null(
    tmp_path, monkeypatch,
):
    """同 owner 同 endpoint 唯一；多 owner 共用 endpoint、同 owner 多 NULL 行、
    NULL 与非 NULL 混排均允许（NULL 互异语义）。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _config(tmp_path / "weknora-uniqueness.db")
    # 旧版本路径：降到 0151 → 以无 weknora 列的存量形态升级，唯一对象由
    # 0152 以唯一索引落地（SQLite 无 ADD CONSTRAINT）
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0151_autoloop_merge")
    engine = _engine(cfg)
    assert UNIQUE not in _unique_names(engine, SERVERS)
    with engine.begin() as connection:
        _insert_server(
            connection, "legacy-1", "owner-1", "legacy-http",
            with_weknora=False,
        )
    engine.dispose()
    command.upgrade(cfg, "head")

    engine = _engine(cfg)
    with engine.begin() as connection:
        # 多 owner 共用同一 endpoint：允许
        _insert_server(connection, "s1", "owner-1", "mirror-a", endpoint_id="ep-1")
        _insert_server(connection, "s2", "owner-2", "mirror-a", endpoint_id="ep-1")
        # 同 owner 多 NULL：允许（普通 MCP 行不受镜像约束）
        _insert_server(connection, "s3", "owner-1", "plain-1", endpoint_id=None)
        _insert_server(connection, "s4", "owner-1", "plain-2", endpoint_id=None)
        # NULL 与非 NULL 混排：允许
        _insert_server(
            connection, "s5", "owner-2", "plain-3", endpoint_id=None,
            scope='{"knowledge_base_ids": ["kb-1"]}',
        )
    # 同 owner 同 endpoint：第二条被拒
    with pytest.raises(Exception, match="UNIQUE"):
        with engine.begin() as connection:
            _insert_server(
                connection, "s6", "owner-1", "mirror-b", endpoint_id="ep-1",
            )
    # 既有 (owner, name) 唯一约束不受影响
    with pytest.raises(Exception, match="UNIQUE"):
        with engine.begin() as connection:
            _insert_server(
                connection, "s7", "owner-1", "mirror-a", endpoint_id="ep-2",
            )
    engine.dispose()

    # 存量升级产生的唯一索引形态也要能干净降级（batch drop_index 路径）
    command.downgrade(cfg, "0151_autoloop_merge")
    engine = _engine(cfg)
    inspector = inspect(engine)
    server_columns = {column["name"] for column in inspector.get_columns(SERVERS)}
    assert "weknora_endpoint_id" not in server_columns
    assert "weknora_scope" not in server_columns
    names = _unique_names(engine, SERVERS)
    assert UNIQUE not in names
    assert "uq_sa_mcp_owner_name" in names
    with engine.connect() as connection:
        surviving = connection.execute(
            text(f"SELECT COUNT(*) FROM {SERVERS}")
        ).scalar_one()
    assert surviving == 6  # legacy-1 + s1..s5：降级重建保留既有行
    engine.dispose()


def test_legacy_rows_are_not_backfilled(tmp_path, monkeypatch):
    """旧数据不自动收编：存量行升级后 weknora 列保持 NULL，集成表零行。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _config(tmp_path / "weknora-no-backfill.db")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0151_autoloop_merge")
    engine = _engine(cfg)
    with engine.begin() as connection:
        _insert_server(
            connection, "legacy-1", "owner-1", "old-http", with_weknora=False,
        )
        _insert_server(
            connection, "legacy-2", "owner-2", "old-sse", with_weknora=False,
        )
    engine.dispose()
    command.upgrade(cfg, "head")

    engine = _engine(cfg)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"SELECT weknora_endpoint_id, weknora_scope FROM {SERVERS} "
                "ORDER BY id"
            )
        ).all()
    assert rows == [(None, None), (None, None)]
    with engine.connect() as connection:
        count = connection.execute(
            text(f"SELECT COUNT(*) FROM {INTEGRATION}")
        ).scalar_one()
    assert count == 0
    engine.dispose()


def test_integration_state_persists_and_latch_roundtrips(tmp_path, monkeypatch):
    """集成状态行持久化：owner 主键每用户一条，500 汉字 last_error 与
    initialized_at 持久闩原样回读；幂等重跑 upgrade 不动既有状态。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _config(tmp_path / "weknora-integration.db")
    command.upgrade(cfg, "head")

    long_error = "知识库连接失败：" + "检" * 485  # 491 汉字，≤500 字符
    engine = _engine(cfg)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {INTEGRATION} (owner_id, origin, initialized_at, "
                "default_knowledge_base_id, default_endpoint_id, last_error, "
                "updated_at) VALUES ('owner-1', 'https://weknora.example.invalid', "
                "'2026-11-19 08:30:00', 'kb-1', 'ep-1', :last_error, "
                "'2026-11-19 08:31:00')"
            ),
            {"last_error": long_error},
        )
        connection.execute(
            text(
                f"INSERT INTO {INTEGRATION} (owner_id, origin, updated_at) "
                "VALUES ('owner-2', 'https://weknora.example.invalid', "
                "'2026-11-19 09:00:00')"
            ),
        )
    engine.dispose()
    # 已在 head 的幂等重跑不触碰既有状态
    command.upgrade(cfg, "head")

    engine = _engine(cfg)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"SELECT origin, initialized_at, default_knowledge_base_id, "
                f"default_endpoint_id, last_error FROM {INTEGRATION} "
                "WHERE owner_id = 'owner-1'"
            )
        ).one()
        uninitialized = connection.execute(
            text(
                f"SELECT initialized_at, default_knowledge_base_id, "
                f"last_error FROM {INTEGRATION} WHERE owner_id = 'owner-2'"
            )
        ).one()
    assert row.origin == "https://weknora.example.invalid"
    assert row.initialized_at is not None
    assert row.default_knowledge_base_id == "kb-1"
    assert row.default_endpoint_id == "ep-1"
    assert row.last_error == long_error
    # 未初始化用户：闩未置位，可选字段全空（服务层按需回填）
    assert uninitialized.initialized_at is None
    assert uninitialized.default_knowledge_base_id is None
    assert uninitialized.last_error is None
    # 每用户一条：同 owner 第二行被主键拒绝
    with pytest.raises(Exception, match="UNIQUE|PRIMARY"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {INTEGRATION} (owner_id, origin, updated_at) "
                    "VALUES ('owner-1', 'https://weknora.example.invalid', "
                    "'2026-11-19 10:00:00')"
                )
            )
    engine.dispose()


def test_downgrade_removes_all_and_upgrade_restores(tmp_path, monkeypatch):
    """降级剥离列/唯一/集成表且保留既有 (owner, name) 唯一；回升级形态一致。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _config(tmp_path / "weknora-cycle.db")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0151_autoloop_merge")

    engine = _engine(cfg)
    inspector = inspect(engine)
    server_columns = {column["name"] for column in inspector.get_columns(SERVERS)}
    assert "weknora_endpoint_id" not in server_columns
    assert "weknora_scope" not in server_columns
    names = _unique_names(engine, SERVERS)
    assert UNIQUE not in names
    # 既有唯一约束在 SQLite batch 重建后原样保留
    assert "uq_sa_mcp_owner_name" in names
    assert INTEGRATION not in inspector.get_table_names()
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "0151_autoloop_merge"
    engine.dispose()

    command.upgrade(cfg, "head")
    engine = _engine(cfg)
    inspector = inspect(engine)
    server_columns = {column["name"] for column in inspector.get_columns(SERVERS)}
    assert {"weknora_endpoint_id", "weknora_scope"} <= server_columns
    # 静态 DDL 重建的集成表与 fresh create_all 形态一致
    _assert_integration_shape(engine)
    assert [UNIQUE] == [
        name for name in _unique_names(engine, SERVERS) if name == UNIQUE
    ]
    assert "uq_sa_mcp_owner_name" in _unique_names(engine, SERVERS)
    engine.dispose()

    # PostgreSQL reflects the backing index as well as the owning constraint.
    # Dropping that index directly fails with DependentObjectsStillExist.
    spec = importlib.util.spec_from_file_location(
        "weknora_migration", BACKEND / "alembic/versions/2026_11_19_0152_weknora_mcp.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    operations = Mock()
    operations.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    inspector = Mock()
    inspector.get_table_names.return_value = [SERVERS, INTEGRATION]
    inspector.get_indexes.return_value = [{"name": UNIQUE, "duplicates_constraint": UNIQUE}]
    inspector.get_unique_constraints.return_value = [{"name": UNIQUE}]
    inspector.get_columns.return_value = [{"name": "weknora_endpoint_id"}, {"name": "weknora_scope"}]
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "sa_inspect", lambda bind: inspector)
    migration.downgrade()
    operations.drop_constraint.assert_called_once_with(UNIQUE, SERVERS, type_="unique")
    operations.drop_index.assert_not_called()


def test_upgrade_accepts_model_created_table_without_duplicates(
    tmp_path, monkeypatch,
):
    """模型 create_all 先行（运行时已建表）后升级 0152：幂等接受，唯一对象
    不重复（fresh 与迁移两条路径收敛同名同语义）。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_path = tmp_path / "weknora-precreated.db"
    from app.shared.database import Base
    from app.model_registry import import_all_models

    import_all_models()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    cfg = _config(db_path)
    command.stamp(cfg, "0151_autoloop_merge")
    command.upgrade(cfg, "head")

    engine = _engine(cfg)
    inspector = inspect(engine)
    server_columns = {column["name"] for column in inspector.get_columns(SERVERS)}
    assert {"weknora_endpoint_id", "weknora_scope"} <= server_columns
    # 表内约束只此一份：不会因迁移重复建索引或约束
    assert [UNIQUE] == [
        name for name in _unique_names(engine, SERVERS) if name == UNIQUE
    ]
    _assert_integration_shape(engine)
    engine.dispose()


def test_head_is_single_and_pins_weknora_mcp(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from alembic.script import ScriptDirectory

    cfg = _config(tmp_path / "weknora-heads-check.db")
    scripts = ScriptDirectory.from_config(cfg)
    assert scripts.get_heads() == [LATEST_HEAD]
    # WeKnora 合入后的单头继续被后续迁移（multica profiles）线性延伸；
    # WeKnora 合并点自身的身份不受延伸影响，仍在此钉住。
    assert LATEST_HEAD == "0154_multica_profiles"
    assert scripts.get_revision(LATEST_HEAD).down_revision == "0153_weknora_operations_merge"
    assert set(scripts.get_revision("0153_weknora_operations_merge").down_revision) == {
        "0152_operations_report_plan", "0152_weknora_mcp",
    }
