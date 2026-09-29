"""Migration coverage for users.report_token_hash (0120).

重点验证存量回填：密文 → sha256 哈希；解不开的行让迁移失败（约定不提供
可漏跑的手工回填脚本）。
"""

import pytest
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _insert_user(engine, user_id: str, encrypted: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, email, password_hash, role, "
                "is_active, token_version, report_token_encrypted, created_at, updated_at) "
                "VALUES (:id, :username, :email, :pw, 'admin', 1, 0, :enc, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": user_id, "username": user_id, "email": f"{user_id}@t.cn",
             "pw": "x", "enc": encrypted},
        )


def test_upgrade_backfills_report_token_hash(tmp_path, monkeypatch):
    """0120 建列建唯一索引，并把存量密文回填为 sha256 哈希；可干净降级。"""
    from app.auth.crypto import encrypt_value, hash_query_key

    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "report-token-hash-migration.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    # 先升到 0119（0120 的前一版），造两行存量：一行有密文、一行没有。
    command.upgrade(cfg, "0119_sa_scheduled_tasks")
    engine = create_engine(f"sqlite:///{db_path}")
    _insert_user(engine, "u-with-token", encrypt_value("tok-plain-1"))
    _insert_user(engine, "u-without-token", None)
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        hashed = conn.execute(
            text("SELECT report_token_hash FROM users WHERE id = 'u-with-token'")
        ).scalar()
        null_hash = conn.execute(
            text("SELECT report_token_hash FROM users WHERE id = 'u-without-token'")
        ).scalar()
    assert hashed == hash_query_key("tok-plain-1")
    assert null_hash is None

    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("users")}
    assert "report_token_hash" in columns
    indexes = {i["name"]: i for i in inspector.get_indexes("users")}
    # SQLite inspector 对 unique 返回 1/0 而非布尔，按真值断言。
    assert indexes["uq_users_report_token_hash"]["unique"]
    engine.dispose()

    # 降级干净移除列与索引
    command.downgrade(cfg, "0119_sa_scheduled_tasks")
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "report_token_hash" not in {c["name"] for c in inspector.get_columns("users")}
    assert "uq_users_report_token_hash" not in {i["name"] for i in inspector.get_indexes("users")}
    engine.dispose()


def test_upgrade_fails_loudly_on_undecryptable_ciphertext(tmp_path, monkeypatch):
    """存量密文解不开（Fernet 密钥不符/密文损坏）→ 迁移失败，不静默跳过。"""
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "report-token-hash-bad-cipher.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "0119_sa_scheduled_tasks")
    engine = create_engine(f"sqlite:///{db_path}")
    _insert_user(engine, "u-bad", "not-a-fernet-ciphertext")
    engine.dispose()

    with pytest.raises(RuntimeError, match="无法解密"):
        command.upgrade(cfg, "head")
