"""task instances runtime domain tables + menu backfill

任务实例（独立运行时功能域）首期八表：模板/版本（canonical_hash 幂等）、
实例（spec 快照 + idempotency）、节点尝试（inputs_hash 派发幂等）、
事件（append-only gap-free seq）、审批、插话、产物。

同时回填菜单权限：任务实例为 admin 向运行时（对齐 api_hub 的 opt-in
定位），不进入 DEFAULT_NON_ADMIN 回退集；为已持有 data.pipelines 的
存量角色补 task_instances key（运维型角色的自然扩展）。

Revision ID: 0125_task_instances
Revises: 0124_notification_channel_templates
Create Date: 2026-10-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0125_task_instances"
down_revision = "0124_notification_channel_templates"
branch_labels = None
depends_on = None

_TEMPLATES = "task_templates"
_REVISIONS = "task_template_revisions"
_INSTANCES = "task_instances"
_NODE_RUNS = "task_node_runs"
_EVENTS = "task_events"
_APPROVALS = "task_approvals"
_STEERING = "task_steering_messages"
_ARTIFACTS = "task_artifacts"

_MENU_KEY = "task_instances"
_BACKFILL_ANCHOR = "data.pipelines"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    if "users" not in tables:
        return

    if _TEMPLATES not in tables:
        op.create_table(
            _TEMPLATES,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            # latest_revision_id 为应用层维护的指针列（无 FK，避免与
            # revisions 循环外键；SQLite 无法 ALTER 补挂约束）
            sa.Column("latest_revision_id", sa.String(), nullable=True),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_task_templates_updated_at", _TEMPLATES, ["updated_at"])

    tables = set(sa_inspect(bind).get_table_names())
    if _REVISIONS not in tables and _TEMPLATES in tables:
        op.create_table(
            _REVISIONS,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("template_id", sa.String(), nullable=False),
            sa.Column("revision_no", sa.Integer(), nullable=False),
            sa.Column("spec_yaml", sa.Text(), nullable=False),
            sa.Column("spec_compiled", sa.JSON(), nullable=True),
            sa.Column("canonical_hash", sa.String(length=64), nullable=False),
            sa.Column("note", sa.String(length=500), nullable=True),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.ForeignKeyConstraint(
                ["template_id"], [f"{_TEMPLATES}.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("template_id", "revision_no",
                                name="uq_task_template_revisions_no"),
            sa.UniqueConstraint("template_id", "canonical_hash",
                                name="uq_task_template_revisions_hash"),
        )
        op.create_index(
            "ix_task_template_revisions_template_id", _REVISIONS, ["template_id"])

    tables = set(sa_inspect(bind).get_table_names())
    if _INSTANCES not in tables and _REVISIONS in tables:
        op.create_table(
            _INSTANCES,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("template_revision_id", sa.String(), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("goal", sa.Text(), nullable=False),
            sa.Column("inputs", sa.JSON(), nullable=True),
            sa.Column("spec_snapshot", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("fail_reason", sa.Text(), nullable=True),
            sa.Column("cancel_reason", sa.Text(), nullable=True),
            sa.Column("idempotency_key", sa.String(length=128), nullable=True),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.ForeignKeyConstraint(
                ["template_revision_id"], [f"{_REVISIONS}.id"],
                ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("idempotency_key",
                                name="uq_task_instances_idem"),
        )
        op.create_index("ix_task_instances_status_created", _INSTANCES,
                        ["status", "created_at"])
        op.create_index("ix_task_instances_created_by", _INSTANCES,
                        ["created_by"])
        op.create_index(
            "ix_task_instances_template_revision_id", _INSTANCES,
            ["template_revision_id"])

    tables = set(sa_inspect(bind).get_table_names())
    if _NODE_RUNS not in tables and _INSTANCES in tables:
        op.create_table(
            _NODE_RUNS,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("instance_id", sa.String(), nullable=False),
            sa.Column("node_id", sa.String(length=64), nullable=False),
            sa.Column("attempt_no", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("inputs", sa.JSON(), nullable=True),
            sa.Column("inputs_hash", sa.String(length=64), nullable=False),
            sa.Column("output", sa.JSON(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("correction_count", sa.Integer(), nullable=False),
            sa.Column("rework_count", sa.Integer(), nullable=False),
            sa.Column("model_config_id", sa.String(), nullable=True),
            sa.Column("lease_owner", sa.String(length=200), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["instance_id"], [f"{_INSTANCES}.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["model_config_id"], ["model_configs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("instance_id", "node_id", "attempt_no",
                                name="uq_task_node_runs_attempt"),
            sa.UniqueConstraint("instance_id", "node_id", "inputs_hash",
                                name="uq_task_node_runs_inputs_hash"),
        )
        op.create_index("ix_task_node_runs_instance_status", _NODE_RUNS,
                        ["instance_id", "status"])
        op.create_index("ix_task_node_runs_lease", _NODE_RUNS,
                        ["lease_expires_at"])

    tables = set(sa_inspect(bind).get_table_names())
    if _EVENTS not in tables and _INSTANCES in tables:
        op.create_table(
            _EVENTS,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("instance_id", sa.String(), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("type", sa.String(length=40), nullable=False),
            sa.Column("node_run_id", sa.String(), nullable=True),
            sa.Column("actor", sa.String(length=200), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["instance_id"], [f"{_INSTANCES}.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["node_run_id"], [f"{_NODE_RUNS}.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("instance_id", "seq", name="uq_task_events_seq"),
        )
        op.create_index("ix_task_events_instance_id", _EVENTS,
                        ["instance_id", "id"])

    tables = set(sa_inspect(bind).get_table_names())
    if _APPROVALS not in tables and _NODE_RUNS in tables:
        op.create_table(
            _APPROVALS,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("instance_id", sa.String(), nullable=False),
            sa.Column("node_run_id", sa.String(), nullable=False),
            sa.Column("proposal", sa.JSON(), nullable=True),
            sa.Column("proposal_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("decided_by", sa.String(length=200), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["instance_id"], [f"{_INSTANCES}.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["node_run_id"], [f"{_NODE_RUNS}.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_task_approvals_instance_status", _APPROVALS,
                        ["instance_id", "status"])
        op.create_index(
            "ix_task_approvals_node_run_id", _APPROVALS, ["node_run_id"])

    tables = set(sa_inspect(bind).get_table_names())
    if _STEERING not in tables and _NODE_RUNS in tables:
        op.create_table(
            _STEERING,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("instance_id", sa.String(), nullable=False),
            sa.Column("node_run_id", sa.String(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("created_by", sa.String(length=200), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("idempotency_key", sa.String(length=128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["instance_id"], [f"{_INSTANCES}.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["node_run_id"], [f"{_NODE_RUNS}.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("idempotency_key",
                                name="uq_task_steering_idem"),
        )
        op.create_index("ix_task_steering_instance", _STEERING, ["instance_id"])
        op.create_index(
            "ix_task_steering_node_run_id", _STEERING, ["node_run_id"])

    tables = set(sa_inspect(bind).get_table_names())
    if _ARTIFACTS not in tables and _NODE_RUNS in tables:
        op.create_table(
            _ARTIFACTS,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("instance_id", sa.String(), nullable=False),
            sa.Column("node_run_id", sa.String(), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("mime_type", sa.String(length=100), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("storage_uri", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["instance_id"], [f"{_INSTANCES}.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["node_run_id"], [f"{_NODE_RUNS}.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_task_artifacts_instance", _ARTIFACTS,
                        ["instance_id"])
        op.create_index("ix_task_artifacts_node_run", _ARTIFACTS,
                        ["node_run_id"])

    _backfill_menu_key(bind)


def _backfill_menu_key(bind) -> None:
    """为已持有 data.pipelines 的存量角色补 task_instances（0065 先例）。"""
    inspector = sa_inspect(bind)
    if "role_menu_permissions" not in set(inspector.get_table_names()):
        return
    role_menu = sa.table(
        "role_menu_permissions",
        sa.column("role", sa.String),
        sa.column("menu_keys", sa.JSON),
    )
    rows = bind.execute(
        sa.select(role_menu.c.role, role_menu.c.menu_keys)
    ).fetchall()
    for role, menu_keys in rows:
        keys = list(menu_keys or [])
        if _BACKFILL_ANCHOR in keys and _MENU_KEY not in keys:
            keys.append(_MENU_KEY)
            bind.execute(
                role_menu.update()
                .where(role_menu.c.role == role)
                .values(menu_keys=keys)
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    for table in (_ARTIFACTS, _STEERING, _APPROVALS, _EVENTS, _NODE_RUNS,
                  _INSTANCES, _REVISIONS, _TEMPLATES):
        if table in tables:
            op.drop_table(table)
        tables = set(sa_inspect(bind).get_table_names())
