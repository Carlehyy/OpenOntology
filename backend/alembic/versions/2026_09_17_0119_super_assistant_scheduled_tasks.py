"""super assistant user-facing scheduled tasks

新增 `super_assistant_scheduled_tasks`（计划定义）与
`super_assistant_scheduled_runs`（单次执行）。纯增量，无数据回填。

Revision ID: 0119_sa_scheduled_tasks
Revises: 0118_plugin_runner_event_replay
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0119_sa_scheduled_tasks"
down_revision = "0118_plugin_runner_event_replay"
branch_labels = None
depends_on = None

_TASKS = "super_assistant_scheduled_tasks"
_RUNS = "super_assistant_scheduled_runs"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    if "users" not in tables:
        return

    if _TASKS not in tables:
        op.create_table(
            _TASKS,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("instruction", sa.Text(), nullable=False),
            sa.Column("schedule_kind", sa.String(length=16), nullable=False),
            sa.Column("timezone", sa.String(length=64), nullable=False),
            sa.Column("run_at", sa.DateTime(), nullable=True),
            sa.Column("hour", sa.Integer(), nullable=True),
            sa.Column("minute", sa.Integer(), nullable=True),
            sa.Column("weekday", sa.Integer(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("next_run_at", sa.DateTime(), nullable=True),
            sa.Column("last_dispatched_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_sa_scheduled_tasks_due", _TASKS, ["enabled", "next_run_at"])
        op.create_index("ix_sa_scheduled_tasks_owner_updated", _TASKS, ["owner_id", "updated_at"])
        op.create_index("ix_super_assistant_scheduled_tasks_owner_id", _TASKS, ["owner_id"])

    tables = set(sa_inspect(bind).get_table_names())
    if (
        _RUNS not in tables
        and _TASKS in tables
        and "super_assistant_conversations" in tables
    ):
        op.create_table(
            _RUNS,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("conversation_id", sa.String(), nullable=True),
            sa.Column("scheduled_for", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("result_summary", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["task_id"], [f"{_TASKS}.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["conversation_id"], ["super_assistant_conversations.id"], ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("task_id", "scheduled_for", name="uq_sa_scheduled_run_slot"),
        )
        op.create_index("ix_sa_scheduled_runs_task_scheduled", _RUNS, ["task_id", "scheduled_for"])
        op.create_index("ix_sa_scheduled_runs_owner_created", _RUNS, ["owner_id", "created_at"])
        op.create_index("ix_super_assistant_scheduled_runs_owner_id", _RUNS, ["owner_id"])
        op.create_index("ix_super_assistant_scheduled_runs_conversation_id", _RUNS, ["conversation_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    if _RUNS in tables:
        op.drop_table(_RUNS)
    if _TASKS in set(sa_inspect(bind).get_table_names()):
        op.drop_table(_TASKS)
