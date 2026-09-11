"""0068 超级助手 Skill 治理迁移：always_active / use_count / last_used_at。"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _skill_columns(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        rows = connection.execute(text("PRAGMA table_info(super_assistant_skills)")).all()
    engine.dispose()
    return {row[1] for row in rows}


def test_revision_graph_head_is_single_head(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, tmp_path / "heads.db")

    heads = ScriptDirectory.from_config(cfg).get_heads()

    # 单头门禁：新迁移必须线性追加（当前 head 见 alembic heads 输出）。
    # 0094 palace folders 目录一等公民表为当前 head（0093 为 multica 外部集成）。
    assert len(heads) == 1
    assert heads == ["0103_ontology_readpath_perf_indexes"]


def _create_0067_shape_skills_table(db_path: Path) -> None:
    """手工建 0067 形态的 super_assistant_skills（不含治理列）。

    全新库走全链路时，迁移 0003 会按当前模型 metadata create_all，
    新列已随表建好、0068 的 add_column 分支走不到；只有手工复刻
    0067 形态并 stamp，才能真正验证 0068 的加列与默认值回填语义。
    """
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE super_assistant_skills ("
            " id VARCHAR NOT NULL PRIMARY KEY,"
            " owner_id VARCHAR NOT NULL,"
            " name VARCHAR(100) NOT NULL,"
            " display_name VARCHAR(200) NOT NULL,"
            " description TEXT NOT NULL,"
            " triggers JSON NOT NULL,"
            " folder_path VARCHAR(1000) NOT NULL,"
            " manifest JSON NOT NULL,"
            " enabled BOOLEAN NOT NULL,"
            " revision INTEGER NOT NULL,"
            " created_at DATETIME NOT NULL,"
            " updated_at DATETIME NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO super_assistant_skills VALUES ("
            " 's1', 'u1', 'legacy-skill', 'legacy-skill', '旧技能', '[]',"
            " '/tmp/legacy-skill', '[]', 1, 1,"
            " '2026-08-13 00:00:00', '2026-08-13 00:00:00')"
        ))
    engine.dispose()


def test_upgrade_adds_governance_columns_with_defaults(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "skill-governance.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    _create_0067_shape_skills_table(db_path)
    command.stamp(cfg, "0067_super_assistant_evolution")

    command.upgrade(cfg, "head")

    assert {"always_active", "use_count", "last_used_at"} <= _skill_columns(db_path)
    # 存量行按 server_default 回填：always_active=False、use_count=0、last_used_at=NULL
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT always_active, use_count, last_used_at"
            " FROM super_assistant_skills WHERE id = 's1'"
        )).one()
    engine.dispose()
    assert tuple(row) == (0, 0, None)


def test_upgrade_is_idempotent_when_columns_already_exist(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "skill-governance-idempotent.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    _create_0067_shape_skills_table(db_path)
    # 模拟已有环境被手工补过列：guard 必须跳过而不是重复 add_column 报错
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE super_assistant_skills"
            " ADD COLUMN always_active BOOLEAN NOT NULL DEFAULT 0"
        ))
        connection.execute(text(
            "ALTER TABLE super_assistant_skills"
            " ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0"
        ))
        connection.execute(text(
            "ALTER TABLE super_assistant_skills ADD COLUMN last_used_at DATETIME"
        ))
    engine.dispose()
    command.stamp(cfg, "0067_super_assistant_evolution")

    command.upgrade(cfg, "head")
    assert {"always_active", "use_count", "last_used_at"} <= _skill_columns(db_path)


def test_downgrade_drops_governance_columns(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "skill-governance-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0067_super_assistant_evolution")

    columns = _skill_columns(db_path)
    assert "always_active" not in columns
    assert "use_count" not in columns
    assert "last_used_at" not in columns
