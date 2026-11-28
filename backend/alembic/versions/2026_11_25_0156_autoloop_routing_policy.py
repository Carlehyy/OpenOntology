"""AutoLoop routing-policy versions.

Revision ID: 0156_autoloop_routing_policy
Revises: 0155_multica_profile_web_url
Create Date: 2026-11-25

One immutable row per owner and version. Version 0 is the built-in default and
is never stored. Rows are append-only: UPDATE and DELETE are refused by a
database trigger, exactly like the 0147 facts.

Historical 0003 creates registered tables from current metadata on a fresh
chain, so this object usually exists before this revision runs. An existing
object is validated instead of being created twice.

Downgrade drops the table and its history. Export it first to recover which
routing policy was in effect for a given version; prepared events keep citing
version numbers that can then no longer be resolved.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "0156_autoloop_routing_policy"
down_revision = "0155_multica_profile_web_url"
branch_labels = None
depends_on = None

# Frozen definitions intentionally do not import application models.
metadata = sa.MetaData()
sa.Table("users", metadata, sa.Column("id", sa.String, primary_key=True))

sa_autoloop_routing_policies = sa.Table(
    "sa_autoloop_routing_policies", metadata,
    sa.Column("owner_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("prompt_text", sa.Text(), nullable=True),
    sa.Column("created_by", sa.String(length=36), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("version > 0", name="ck_sa_al_routing_policy_version"),
    sa.PrimaryKeyConstraint("owner_id", "version"),
)

_TABLE = sa_autoloop_routing_policies.name
# Same text as the model's append-only listeners, so an object pre-created from
# metadata compares equal to the one this revision creates.
_FUNCTION = f"{_TABLE}_immutable"
_BODY = " BEGIN RAISE EXCEPTION 'AutoLoop facts are append-only'; END "


def _ensure_immutable() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            trigger = f"{_TABLE}_{operation.lower()}_immutable"
            sql = (f"CREATE TRIGGER {trigger} BEFORE {operation} ON {_TABLE} "
                   "BEGIN SELECT RAISE(ABORT, 'AutoLoop facts are append-only'); END")
            existing = bind.execute(sa.text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
                {"name": trigger}).scalar()
            if existing is None:
                bind.execute(sa.text(sql))
            elif existing != sql:
                raise RuntimeError(f"Existing trigger {trigger} has incompatible shape")
    elif bind.dialect.name == "postgresql":
        existing = bind.execute(sa.text(
            "SELECT t.tgtype, t.tgenabled, p.proname, p.prosrc "
            "FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid "
            "WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"),
            {"table": _TABLE, "trigger": _FUNCTION}).first()
        if existing is None:
            bind.execute(sa.text(f"CREATE OR REPLACE FUNCTION {_FUNCTION}() RETURNS trigger "
                                 f"LANGUAGE plpgsql AS $${_BODY}$$"))
            bind.execute(sa.text(f"CREATE TRIGGER {_FUNCTION} BEFORE UPDATE OR DELETE ON {_TABLE} "
                                 f"FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()"))
        elif (existing[0] != 27 or existing[1] != "O"
              or existing[2] != _FUNCTION or existing[3] != _BODY):
            raise RuntimeError(f"Existing trigger {_FUNCTION} has incompatible shape")
    else:
        raise RuntimeError("AutoLoop migration supports PostgreSQL and SQLite only")


def upgrade() -> None:
    bind = op.get_bind()
    if sa_inspect(bind).has_table(_TABLE):
        columns = {item["name"] for item in sa_inspect(bind).get_columns(_TABLE)}
        if columns != set(sa_autoloop_routing_policies.c.keys()):
            raise RuntimeError(f"Existing {_TABLE} has incompatible columns")
    else:
        sa_autoloop_routing_policies.create(bind)
    _ensure_immutable()


def downgrade() -> None:
    bind = op.get_bind()
    sa_autoloop_routing_policies.drop(bind, checkfirst=True)
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text(f"DROP FUNCTION IF EXISTS {_FUNCTION}()"))
