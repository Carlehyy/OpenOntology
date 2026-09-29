"""user report token hash

users 表新增 report_token_hash（sha256，唯一索引）：上报端点鉴权由
「全表取出密文逐个 Fernet 解密比对」改为与查询密钥同一模式的 O(1) 查表，
同时消除单行密文损坏拖垮整个上报端点的问题。report_token_encrypted 密文
列保留（下载上报脚本仍需解出明文内嵌），两列自此同步写入。

存量回填：解密每个非空密文并写入哈希。任何一行解不开（Fernet 密钥不符
或密文损坏）即让迁移失败——按约定不提供可漏跑的手工回填脚本，必须先修
复加密配置（ENCRYPTION_KEY）再重新执行迁移。

Revision ID: 0120_user_report_token_hash
Revises: 0119_sa_scheduled_tasks
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0120_user_report_token_hash"
down_revision = "0119_sa_scheduled_tasks"
branch_labels = None
depends_on = None

_COLUMN = "report_token_hash"
_INDEX = "uq_users_report_token_hash"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 与 0102/0109 同一防御口径：部分迁移测试场景只手工建被测表并 stamp
    # 到中间版本，users 表可能不存在；此时跳过 DDL。
    if "users" not in tables:
        return

    columns = {c["name"] for c in inspector.get_columns("users")}
    if _COLUMN not in columns:
        op.add_column("users", sa.Column(_COLUMN, sa.String(length=64), nullable=True))

    indexes = {i["name"] for i in inspector.get_indexes("users")}
    if _INDEX not in indexes:
        op.create_index(_INDEX, "users", [_COLUMN], unique=True)

    # 回填防御：部分迁移测试场景手工建最小 users 表并 stamp 到中间版本
    # （如 multica 用例只建 id/username 两列），report_token_encrypted 列可能
    # 不存在——此时无密文可回填，跳过（与 0088/0094 对前置列的防御同口径）。
    if "report_token_encrypted" not in columns:
        return

    # 回填：解密存量密文 → 写哈希。解不开即失败（见模块 docstring）。
    from app.auth.crypto import decrypt_value, hash_query_key

    rows = bind.execute(
        sa.text(
            "SELECT id, report_token_encrypted FROM users "
            "WHERE report_token_encrypted IS NOT NULL "
            f"AND {_COLUMN} IS NULL"
        )
    ).fetchall()
    for row_id, encrypted in rows:
        try:
            token = decrypt_value(encrypted)
        except Exception as exc:
            raise RuntimeError(
                f"users({row_id}): report_token_encrypted 无法解密，"
                "不能回填 report_token_hash；请先核对 ENCRYPTION_KEY 与生产"
                "密文一致性后重试迁移（本迁移不提供绕过解密的回填路径）"
            ) from exc
        if not token:
            continue
        bind.execute(
            sa.text(f"UPDATE users SET {_COLUMN} = :h WHERE id = :id"),
            {"h": hash_query_key(token), "id": row_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "users" not in set(inspector.get_table_names()):
        return
    indexes = {i["name"] for i in inspector.get_indexes("users")}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name="users")
    columns = {c["name"] for c in inspector.get_columns("users")}
    if _COLUMN in columns:
        # batch 模式兼容 SQLite 的列删除（重建表语义）。
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column(_COLUMN)
