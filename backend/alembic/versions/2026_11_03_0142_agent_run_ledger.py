"""agent run ledger

Durable background turns for the ontology assistant:

- ``fo_agent_runs`` stores the recoverable run projection (status, partial
  answer/steps, lease, idempotency hash, retry lineage);
- ``fo_agent_run_events`` stores the ordered SSE replay ledger with a unique
  ``(run_id, seq)`` key;
- a partial unique index enforces one active (queued/running) run per
  conversation in both PostgreSQL and SQLite.

Revision ID: 0142_agent_run_ledger
Revises: 0141_retire_super_assistant_palace
Create Date: 2026-11-03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0142_agent_run_ledger"
down_revision = "0141_retire_super_assistant_palace"
branch_labels = None
depends_on = None

_RUNS_TABLE = "fo_agent_runs"
_EVENTS_TABLE = "fo_agent_run_events"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if _RUNS_TABLE not in tables:
        op.create_table(
            _RUNS_TABLE,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("ontology_id", sa.String(), nullable=False),
            sa.Column("release_id", sa.String(), nullable=True),
            sa.Column("conversation_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("user_message_id", sa.String(), nullable=False),
            sa.Column("model_id", sa.String(length=200), nullable=True),
            sa.Column("model", sa.String(length=200), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("stream_model", sa.Boolean(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("steps", sa.JSON(), nullable=False),
            sa.Column("citations", sa.JSON(), nullable=False),
            sa.Column("proposals", sa.JSON(), nullable=False),
            sa.Column("usage", sa.JSON(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("retry_of", sa.String(), nullable=True),
            sa.Column("lease_owner", sa.String(length=64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
            sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
            sa.Column("last_seq", sa.Integer(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_fo_agent_runs_ontology_id", _RUNS_TABLE, ["ontology_id"],
        )
        op.create_index(
            "ix_fo_agent_runs_release_id", _RUNS_TABLE, ["release_id"],
        )
        op.create_index(
            "ix_fo_agent_runs_conversation_id", _RUNS_TABLE, ["conversation_id"],
        )
        op.create_index(
            "ix_fo_agent_runs_user_id", _RUNS_TABLE, ["user_id"],
        )
        op.create_index(
            "ix_fo_agent_runs_retry_of", _RUNS_TABLE, ["retry_of"],
        )
        op.create_index(
            "uq_fo_agent_runs_active_conversation",
            _RUNS_TABLE,
            ["conversation_id"],
            unique=True,
            postgresql_where=sa.text("status IN ('queued', 'running')"),
            sqlite_where=sa.text("status IN ('queued', 'running')"),
        )
        op.create_index(
            "ix_fo_agent_runs_owner_status", _RUNS_TABLE,
            ["user_id", "status"],
        )
        op.create_index(
            "ix_fo_agent_runs_ontology_conversation", _RUNS_TABLE,
            ["ontology_id", "conversation_id"],
        )
        op.create_index(
            "ix_fo_agent_runs_status_lease", _RUNS_TABLE,
            ["status", "lease_expires_at"],
        )

    if _EVENTS_TABLE not in tables:
        op.create_table(
            _EVENTS_TABLE,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("type", sa.String(length=20), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "run_id", "seq", name="uq_fo_agent_run_events_run_seq",
            ),
        )
        op.create_index(
            "ix_fo_agent_run_events_run_id", _EVENTS_TABLE, ["run_id"],
        )
        op.create_index(
            "ix_fo_agent_run_events_type_created", _EVENTS_TABLE,
            ["type", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if _EVENTS_TABLE in tables:
        op.drop_table(_EVENTS_TABLE)
    if _RUNS_TABLE in tables:
        op.drop_table(_RUNS_TABLE)
