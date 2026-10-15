"""scheduled task trigger target (super_assistant | task_template)

1) super_assistant_scheduled_tasks 新增 target_kind（String(16)，非空，
   server_default 'super_assistant'：存量行与默认创建一律归一为超级助手
   目标，行为不变）与 target_template_id（指向 task_templates.id；模板是
   软删模型且分属任务实例域，不设外键，触发时校验存亡）。
2) super_assistant_scheduled_runs 新增 instance_id（task_template 目标
   触发创建的任务实例 id；超级助手目标恒为空，执行记录「查看实例」
   跳转依据）。

Revision ID: 0129_scheduled_task_target
Revises: 0128_preset_template_metadata
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0129_scheduled_task_target"
down_revision = "0128_preset_template_metadata"
branch_labels = None
depends_on = None

_TASKS = "super_assistant_scheduled_tasks"
_RUNS = "super_assistant_scheduled_runs"


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa_inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if _TASKS in tables:
        columns = _columns(bind, _TASKS)
        if "target_kind" not in columns:
            op.add_column(_TASKS, sa.Column(
                "target_kind", sa.String(16), nullable=False,
                server_default="super_assistant"))
        if "target_template_id" not in columns:
            op.add_column(_TASKS, sa.Column("target_template_id", sa.String(), nullable=True))
    if _RUNS in tables:
        if "instance_id" not in _columns(bind, _RUNS):
            op.add_column(_RUNS, sa.Column("instance_id", sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if _TASKS in tables:
        columns = _columns(bind, _TASKS)
        if "target_kind" in columns:
            op.drop_column(_TASKS, "target_kind")
        if "target_template_id" in columns:
            op.drop_column(_TASKS, "target_template_id")
    if _RUNS in tables:
        if "instance_id" in _columns(bind, _RUNS):
            op.drop_column(_RUNS, "instance_id")
