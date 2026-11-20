"""0154 multica 多 profile 迁移：建表、投影列、存量行回填与可逆性。"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from tests.migrations.latest_head import LATEST_HEAD

_PROFILES = "super_assistant_multica_profiles"
_REVISIONS = "super_assistant_multica_profile_revisions"
_LEGACY = "super_assistant_multica_configs"
_CALLS = "super_assistant_execution_calls"


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _execute(sql: str, db_path: Path, params: dict | None = None) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.execute(text(sql), params or {})
    finally:
        engine.dispose()


def _query(sql: str, db_path: Path, params: dict | None = None):
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            return connection.execute(text(sql), params or {}).all()
    finally:
        engine.dispose()


def _tables(db_path: Path) -> set[str]:
    return {
        row[0] for row in
        _query("SELECT name FROM sqlite_master WHERE type='table'", db_path)
    }


def _columns(db_path: Path, table: str) -> set[str]:
    return {row[1] for row in _query(f"PRAGMA table_info({table})", db_path)}


def _index_names(db_path: Path, table: str) -> dict[str, tuple[bool, bool]]:
    rows = _query(f"PRAGMA index_list({table})", db_path)
    # PRAGMA index_list: seq, name, unique, origin, partial
    return {row[1]: (bool(row[2]), bool(row[4])) for row in rows}


def _schema_snapshot(db_path: Path) -> dict:
    return {
        table: (
            tuple(sorted(_columns(db_path, table))),
            tuple(sorted(_index_names(db_path, table))),
        )
        for table in (_PROFILES, _REVISIONS, _LEGACY, _CALLS)
        if table in _tables(db_path)
    }


def _version(db_path: Path) -> str:
    return _query("SELECT version_num FROM alembic_version", db_path)[0][0]


def _create_users_table(db_path: Path) -> None:
    _execute(
        "CREATE TABLE users ("
        " id VARCHAR NOT NULL PRIMARY KEY,"
        " username VARCHAR NOT NULL)",
        db_path,
    )


def _create_legacy_config_table(db_path: Path) -> None:
    """手工建 0093/0095 形态的旧配置表（不含 0154 投影列）。"""
    _execute(
        "CREATE TABLE super_assistant_multica_configs ("
        " owner_id VARCHAR NOT NULL PRIMARY KEY,"
        " base_url VARCHAR(500) NOT NULL,"
        " workspace_id VARCHAR(100) NOT NULL,"
        " workspace_name VARCHAR(200) NOT NULL DEFAULT '',"
        " token_encrypted TEXT,"
        " enabled BOOLEAN NOT NULL,"
        " last_test_status VARCHAR(20),"
        " last_test_message VARCHAR(500),"
        " last_tested_at DATETIME,"
        " created_at DATETIME,"
        " updated_at DATETIME)",
        db_path,
    )


def _create_execution_calls_table(db_path: Path, *, with_source_column: bool) -> None:
    """手工建含/不含 0154 来源列的最小 execution_calls 表。"""
    source = (
        ", multica_profile_revision_id VARCHAR(255)" if with_source_column else ""
    )
    _execute(
        "CREATE TABLE super_assistant_execution_calls ("
        " id VARCHAR NOT NULL PRIMARY KEY,"
        " status VARCHAR(32) NOT NULL,"
        f" outcome VARCHAR(32) NOT NULL{source})",
        db_path,
    )


def _seed_legacy_row(
    db_path: Path,
    owner_id: str,
    *,
    base_url: str,
    workspace_id: str,
    token_encrypted: str | None,
    enabled: bool,
) -> None:
    _execute(
        "INSERT INTO super_assistant_multica_configs "
        "(owner_id, base_url, workspace_id, workspace_name, token_encrypted, enabled) "
        "VALUES (:owner, :base_url, :ws, :ws_name, :token, :enabled)",
        db_path,
        {
            "owner": owner_id, "base_url": base_url, "ws": workspace_id,
            "ws_name": "My Workspace", "token": token_encrypted, "enabled": enabled,
        },
    )


def _seed_execution_call(
    db_path: Path,
    call_id: str,
    *,
    revision_id: str | None,
    status: str,
    outcome: str,
) -> None:
    _execute(
        "INSERT INTO super_assistant_execution_calls "
        "(id, status, outcome, multica_profile_revision_id) "
        "VALUES (:id, :status, :outcome, :rev)",
        db_path,
        {"id": call_id, "status": status, "outcome": outcome, "rev": revision_id},
    )


def _backend() -> Path:
    return Path(__file__).resolve().parents[2]


def test_upgrade_creates_profile_tables_projection_columns_and_indexes(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "multica-profiles-upgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(_backend(), db_path)
    _create_users_table(db_path)
    _create_legacy_config_table(db_path)
    command.stamp(cfg, "0153_weknora_operations_merge")
    command.upgrade(cfg, "0154_multica_profiles")

    assert _PROFILES in _tables(db_path)
    assert _REVISIONS in _tables(db_path)
    assert {
        "profile_id", "profile_revision_id", "source_kind",
    } <= _columns(db_path, _LEGACY)
    assert {
        "id", "owner_id", "name", "kind", "builtin_key", "is_active", "deleted_at",
    } <= _columns(db_path, _PROFILES)
    assert {
        "profile_id", "revision_no", "base_url", "workspace_id", "workspace_name",
        "token_encrypted", "enabled", "retired_at", "revoked_at",
    } <= _columns(db_path, _REVISIONS)
    profile_indexes = _index_names(db_path, _PROFILES)
    # 每 owner 仅一个 is_active / 一个 platform_default：部分唯一索引，
    # 名称与 ORM 显式索引一致；owner 查询索引为显式命名的
    # ix_sa_multica_profiles_owner_id。
    assert profile_indexes.get("uq_sa_multica_profile_owner_active") == (True, True)
    assert profile_indexes.get("uq_sa_multica_profile_owner_default") == (True, True)
    assert profile_indexes.get("ix_sa_multica_profiles_owner_id") == (False, False)
    assert profile_indexes.get("ix_sa_multica_profiles_owner_kind") == (False, False)
    # 每 profile 仅一个未 retired 的 current revision：部分唯一索引。
    revision_indexes = _index_names(db_path, _REVISIONS)
    assert revision_indexes.get("uq_sa_multica_profile_current_revision") == (True, True)


def test_upgrade_adds_execution_source_column_once_when_missing(tmp_path, monkeypatch):
    db_path = tmp_path / "multica-profiles-calls-column.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(_backend(), db_path)
    _create_users_table(db_path)
    _create_legacy_config_table(db_path)
    _create_execution_calls_table(db_path, with_source_column=False)
    command.stamp(cfg, "0153_weknora_operations_merge")
    command.upgrade(cfg, "0154_multica_profiles")
    assert "multica_profile_revision_id" in _columns(db_path, _CALLS)


def test_upgrade_keeps_existing_execution_source_column(tmp_path, monkeypatch):
    """存量库 execution_calls 已带来源列（如按新模型建出）时不得重复添加。"""
    db_path = tmp_path / "multica-profiles-calls-existing.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(_backend(), db_path)
    _create_users_table(db_path)
    _create_legacy_config_table(db_path)
    _create_execution_calls_table(db_path, with_source_column=True)
    command.stamp(cfg, "0153_weknora_operations_merge")
    command.upgrade(cfg, "0154_multica_profiles")
    # sqlite 本身不允许同名重复列：upgrade 不报错且列仍在即证明 guard 生效。
    assert "multica_profile_revision_id" in _columns(db_path, _CALLS)
    assert _version(db_path) == "0154_multica_profiles"


def test_upgrade_backfills_legacy_rows_and_keeps_disabled_data(tmp_path, monkeypatch):
    db_path = tmp_path / "multica-profiles-backfill.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(_backend(), db_path)
    _create_users_table(db_path)
    _create_legacy_config_table(db_path)
    _seed_legacy_row(db_path, "user-1", base_url="http://127.0.0.1:8080",
                     workspace_id="ws-1", token_encrypted="cipher-1", enabled=True)
    # disabled 但有数据的行：数据必须保留，is_active 回填 false。
    _seed_legacy_row(db_path, "user-2", base_url="http://127.0.0.1:9090",
                     workspace_id="ws-2", token_encrypted="cipher-2", enabled=False)
    # 空桩行（无任何数据）不应迁移
    _seed_legacy_row(db_path, "user-3", base_url="", workspace_id="",
                     token_encrypted=None, enabled=False)
    command.stamp(cfg, "0153_weknora_operations_merge")
    command.upgrade(cfg, "0154_multica_profiles")

    profiles = _query(
        "SELECT id, owner_id, kind, is_active FROM super_assistant_multica_profiles",
        db_path,
    )
    revisions = _query(
        "SELECT id, profile_id, revision_no, base_url, workspace_id,"
        " token_encrypted, enabled FROM super_assistant_multica_profile_revisions",
        db_path,
    )
    legacy = _query(
        "SELECT owner_id, profile_id, profile_revision_id, source_kind,"
        " token_encrypted FROM super_assistant_multica_configs",
        db_path,
    )
    legacy_by_owner = {row[0]: row for row in legacy}

    assert len(profiles) == 2
    by_owner = {row[1]: row for row in profiles}
    enabled_profile = by_owner["user-1"]
    disabled_profile = by_owner["user-2"]
    assert enabled_profile[2] == "custom" and enabled_profile[3] == 1
    assert disabled_profile[2] == "custom" and disabled_profile[3] == 0

    revisions_by_profile = {row[1]: row for row in revisions}
    enabled_revision = revisions_by_profile[enabled_profile[0]]
    disabled_revision = revisions_by_profile[disabled_profile[0]]
    assert (
        enabled_revision[2], enabled_revision[3], enabled_revision[4],
        enabled_revision[5], enabled_revision[6],
    ) == (1, "http://127.0.0.1:8080", "ws-1", "cipher-1", 1)
    # disabled 行数据完整保留，仅生效位为 false。
    assert (
        disabled_revision[2], disabled_revision[3], disabled_revision[4],
        disabled_revision[5], disabled_revision[6],
    ) == (1, "http://127.0.0.1:9090", "ws-2", "cipher-2", 0)

    for owner, profile in (("user-1", enabled_profile), ("user-2", disabled_profile)):
        row = legacy_by_owner[owner]
        assert row[1] == profile[0]
        assert row[2] == revisions_by_profile[profile[0]][0]
        assert row[3] == "custom"
    assert legacy_by_owner["user-1"][4] == "cipher-1"
    assert legacy_by_owner["user-2"][4] == "cipher-2"
    # 空桩行未迁移：无 profile 指针
    assert legacy_by_owner["user-3"][1] is None


def test_upgrade_is_idempotent_with_real_backfilled_row(tmp_path, monkeypatch):
    db_path = tmp_path / "multica-profiles-idempotent.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(_backend(), db_path)
    _create_users_table(db_path)
    _create_legacy_config_table(db_path)
    _seed_legacy_row(db_path, "user-1", base_url="http://127.0.0.1:8080",
                     workspace_id="ws-1", token_encrypted="cipher-1", enabled=True)
    command.stamp(cfg, "0153_weknora_operations_merge")
    command.upgrade(cfg, "0154_multica_profiles")
    first = _query(
        "SELECT profile_id, profile_revision_id FROM super_assistant_multica_configs",
        db_path,
    )
    # 以 stamp 回放模拟重跑：已回填行（profile_id 非空）不得二次迁移。
    command.stamp(cfg, "0153_weknora_operations_merge")
    command.upgrade(cfg, "0154_multica_profiles")
    assert _query(
        "SELECT COUNT(*) FROM super_assistant_multica_profiles", db_path,
    )[0][0] == 1
    assert _query(
        "SELECT COUNT(*) FROM super_assistant_multica_profile_revisions", db_path,
    )[0][0] == 1
    assert _query(
        "SELECT profile_id, profile_revision_id FROM super_assistant_multica_configs",
        db_path,
    ) == first


def test_upgrade_without_users_table_skips_ddl(tmp_path, monkeypatch):
    db_path = tmp_path / "multica-profiles-nousers.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(_backend(), db_path)
    command.stamp(cfg, "0153_weknora_operations_merge")
    command.upgrade(cfg, "0154_multica_profiles")
    tables = _tables(db_path)
    assert _PROFILES not in tables
    assert _REVISIONS not in tables


def test_downgrade_projects_single_backfilled_profile_to_legacy_row(
    tmp_path, monkeypatch,
):
    """标准形态（每 owner 一 profile 一 revision）必须可降级：revision
    投影回旧行，schema 撤除，数据不丢。"""
    db_path = tmp_path / "multica-profiles-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(_backend(), db_path)
    _create_users_table(db_path)
    _create_legacy_config_table(db_path)
    _seed_legacy_row(db_path, "user-1", base_url="http://127.0.0.1:8080",
                     workspace_id="ws-1", token_encrypted="cipher-1", enabled=True)
    _seed_legacy_row(db_path, "user-2", base_url="http://127.0.0.1:9090",
                     workspace_id="ws-2", token_encrypted="cipher-2", enabled=False)
    command.stamp(cfg, "0153_weknora_operations_merge")
    command.upgrade(cfg, "0154_multica_profiles")

    # 模拟升级后用户经新服务改过配置：current revision 携带新值，旧行
    # 尚未同步；降级必须把 revision 的来源/字段/凭据投影回旧行。
    _execute(
        "UPDATE super_assistant_multica_profile_revisions"
        " SET base_url = 'http://10.0.0.8:8080', token_encrypted = 'cipher-1b',"
        " workspace_name = 'Renamed' WHERE revision_no = 1"
        " AND profile_id IN (SELECT profile_id FROM super_assistant_multica_configs"
        " WHERE owner_id = 'user-1')",
        db_path,
    )
    command.downgrade(cfg, "0153_weknora_operations_merge")

    tables = _tables(db_path)
    assert _PROFILES not in tables
    assert _REVISIONS not in tables
    assert not ({"profile_id", "profile_revision_id", "source_kind"} & _columns(db_path, _LEGACY))
    rows = {
        row[0]: row
        for row in _query(
            "SELECT owner_id, base_url, workspace_name, token_encrypted, enabled"
            " FROM super_assistant_multica_configs",
            db_path,
        )
    }
    # user-1：投影取 revision 新值，enabled 语义 = revision.enabled AND
    # profile.is_active（回填时 1 AND 1）。
    assert rows["user-1"][1:] == (
        "http://10.0.0.8:8080", "Renamed", "cipher-1b", 1,
    )
    # user-2：disabled 行数据保留、生效位保持关闭（0 AND 0）。
    assert rows["user-2"][1:] == (
        "http://127.0.0.1:9090", "My Workspace", "cipher-2", 0,
    )
    assert _version(db_path) == "0153_weknora_operations_merge"


def test_downgrade_allows_closed_resolved_bound_calls(tmp_path, monkeypatch):
    """closed/completed 且绑定可解析的调用：来源列随 schema 撤除，不阻塞
    降级（仅未闭合/结局未知/绑定悬空的调用才拒绝）。"""
    db_path = tmp_path / "multica-profiles-downgrade-closed.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(_backend(), db_path)
    _create_users_table(db_path)
    _create_legacy_config_table(db_path)
    _create_execution_calls_table(db_path, with_source_column=False)
    _seed_legacy_row(db_path, "user-1", base_url="http://127.0.0.1:8080",
                     workspace_id="ws-1", token_encrypted="cipher-1", enabled=True)
    command.stamp(cfg, "0153_weknora_operations_merge")
    command.upgrade(cfg, "0154_multica_profiles")
    revision_id = _query(
        "SELECT profile_revision_id FROM super_assistant_multica_configs"
        " WHERE owner_id = 'user-1'",
        db_path,
    )[0][0]
    _seed_execution_call(
        db_path, "call-1", revision_id=revision_id, status="closed", outcome="completed",
    )
    command.downgrade(cfg, "0153_weknora_operations_merge")
    assert _PROFILES not in _tables(db_path)
    assert "multica_profile_revision_id" not in _columns(db_path, _CALLS)


def _upgraded_db_with_backfill(tmp_path: Path, monkeypatch, name: str):
    db_path = tmp_path / name
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(_backend(), db_path)
    _create_users_table(db_path)
    _create_legacy_config_table(db_path)
    _create_execution_calls_table(db_path, with_source_column=False)
    _seed_legacy_row(db_path, "user-1", base_url="http://127.0.0.1:8080",
                     workspace_id="ws-1", token_encrypted="cipher-1", enabled=True)
    command.stamp(cfg, "0153_weknora_operations_merge")
    command.upgrade(cfg, "0154_multica_profiles")
    return cfg, db_path


def _mutation_extra_profile(db_path: Path) -> None:
    _execute(
        "INSERT INTO super_assistant_multica_profiles"
        " (id, owner_id, name, kind, is_active, created_at, updated_at)"
        " VALUES ('profile-extra', 'user-1', 'second', 'custom', 0,"
        " '2026-11-18 00:00:00', '2026-11-18 00:00:00')",
        db_path,
    )


def _mutation_extra_revision(db_path: Path) -> None:
    """模拟服务层追加新 revision：旧 revision retired、新 current。历史
    revision 无法投影进单行 legacy，降级必须拒绝。"""
    _execute(
        "UPDATE super_assistant_multica_profile_revisions"
        " SET retired_at = '2026-11-18 00:00:00'",
        db_path,
    )
    _execute(
        "INSERT INTO super_assistant_multica_profile_revisions"
        " (id, profile_id, revision_no, base_url, workspace_id, workspace_name,"
        " enabled, created_at)"
        " SELECT 'revision-extra', id, 2, 'http://10.0.0.9:8080', 'ws-9',"
        " 'Extra', 1, '2026-11-18 00:00:00'"
        " FROM super_assistant_multica_profiles WHERE owner_id = 'user-1'",
        db_path,
    )


def _mutation_soft_deleted_profile(db_path: Path) -> None:
    _execute(
        "UPDATE super_assistant_multica_profiles"
        " SET deleted_at = '2026-11-18 00:00:00' WHERE owner_id = 'user-1'",
        db_path,
    )


def _mutation_platform_default_profile(db_path: Path) -> None:
    _execute(
        "INSERT INTO super_assistant_multica_profiles"
        " (id, owner_id, name, kind, builtin_key, is_active, created_at, updated_at)"
        " VALUES ('profile-default', 'user-1', 'platform', 'platform_default',"
        " 'default', 0, '2026-11-18 00:00:00', '2026-11-18 00:00:00')",
        db_path,
    )


def _mutation_revoked_revision(db_path: Path) -> None:
    _execute(
        "UPDATE super_assistant_multica_profile_revisions"
        " SET revoked_at = '2026-11-18 00:00:00'",
        db_path,
    )


def _mutation_unresolved_bound_call(db_path: Path) -> None:
    revision_id = _query(
        "SELECT profile_revision_id FROM super_assistant_multica_configs"
        " WHERE owner_id = 'user-1'",
        db_path,
    )[0][0]
    _seed_execution_call(
        db_path, "call-open", revision_id=revision_id,
        status="waiting_external", outcome="remote_running",
    )


def _mutation_dangling_bound_call(db_path: Path) -> None:
    _seed_execution_call(
        db_path, "call-dangling", revision_id="revision-missing",
        status="closed", outcome="completed",
    )


@pytest.mark.parametrize("mutation", [
    _mutation_extra_profile,
    _mutation_extra_revision,
    _mutation_soft_deleted_profile,
    _mutation_platform_default_profile,
    _mutation_revoked_revision,
    _mutation_unresolved_bound_call,
    _mutation_dangling_bound_call,
], ids=[
    "extra-profile",
    "extra-revision",
    "soft-deleted-profile",
    "platform-default-profile",
    "revoked-revision",
    "unresolved-bound-call",
    "dangling-bound-call",
])
def test_downgrade_rejects_unrepresentable_state_without_ddl(
    tmp_path, monkeypatch, mutation,
):
    cfg, db_path = _upgraded_db_with_backfill(
        tmp_path, monkeypatch, f"multica-profiles-reject-{mutation.__name__}.db",
    )
    mutation(db_path)
    before = _schema_snapshot(db_path)

    with pytest.raises(RuntimeError, match="cannot downgrade 0154"):
        command.downgrade(cfg, "0153_weknora_operations_merge")

    # 拒绝发生在任何 DDL 之前：结构、索引与版本零改动。
    assert _schema_snapshot(db_path) == before
    assert _version(db_path) == "0154_multica_profiles"


def test_latest_head_remains_0154(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(_backend(), tmp_path / "head-check.db")
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == [LATEST_HEAD]
    assert LATEST_HEAD == "0154_multica_profiles"
