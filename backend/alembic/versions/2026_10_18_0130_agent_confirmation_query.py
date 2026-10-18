"""YIYA-36: agent profile property allowlist, action confirmation ledger, unified query snapshots.

新增（均为增量，不动既有约束）：
- ``fo_agent_profiles.allowed_properties`` JSON 可空：属性级授权（None=全部声明属性）。
- ``fo_action_confirmations`` / ``fo_action_operations`` / ``fo_action_external_attempts``：
  持久确认/操作/外部效果账本（写后不可变载荷、guard 部分唯一、proposal:{cid} 幂等锚）。
- ``fo_agent_query_snapshots`` / ``fo_agent_query_rows``：统一查询有界结果快照（TTL 物理清理）。
"""
from alembic import op
import sqlalchemy as sa

revision = "0130_agent_confirmation_query"
down_revision = "0129_scheduled_task_target"
branch_labels = None
depends_on = None

_ACTIVE_OPERATION_STATES = (
    "('confirmed','dispatch_queued','executing','recovering',"
    "'pending_approval','unknown','compensation_required')"
)


def _table_names(bind) -> set:
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def _columns(bind, table: str) -> set:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _table_names(bind)

    if "fo_agent_profiles" in tables and \
            "allowed_properties" not in _columns(bind, "fo_agent_profiles"):
        op.add_column(
            "fo_agent_profiles",
            sa.Column("allowed_properties", sa.JSON(), nullable=True),
        )

    if "fo_action_confirmations" not in tables:
        op.create_table(
            "fo_action_confirmations",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("actor_user_id", sa.String(), nullable=False),
            sa.Column("session_kind", sa.String(length=32), nullable=False),
            sa.Column("session_id", sa.String(), nullable=False),
            sa.Column("root_intent_id", sa.String(), nullable=False),
            sa.Column("action_request_slot", sa.String(), nullable=False),
            sa.Column("prepare_request_id", sa.String(length=64),
                      nullable=False),
            sa.Column("origin", sa.String(length=32), nullable=False),
            sa.Column("source_call_id", sa.String(), nullable=False),
            sa.Column("ontology_id", sa.String(), nullable=False),
            sa.Column("release_id", sa.String(), nullable=False),
            sa.Column("release_version", sa.String(length=20), nullable=True),
            sa.Column("action_id", sa.String(), nullable=False),
            sa.Column("target_instance_id", sa.String(), nullable=True),
            sa.Column("final_parameters", sa.JSON(), nullable=False),
            sa.Column("parameter_sources", sa.JSON(), nullable=False),
            sa.Column("requires_approval", sa.Boolean(), nullable=False),
            sa.Column("payload_hash", sa.String(length=64), nullable=False),
            sa.Column("display_payload", sa.JSON(), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("row_version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("decided_at", sa.DateTime(), nullable=True),
            sa.Column("decision_event_id", sa.String(), nullable=True),
            sa.Column("operation_id", sa.String(), nullable=True),
            sa.Column("failure_code", sa.String(length=64), nullable=True),
            sa.UniqueConstraint(
                "actor_user_id", "session_kind", "session_id",
                "prepare_request_id",
                name="uq_action_confirmation_prepare_request"),
            sa.UniqueConstraint(
                "actor_user_id", "session_kind", "session_id",
                "root_intent_id", "action_request_slot",
                name="uq_action_confirmation_request_slot"),
            sa.CheckConstraint(
                "state IN ('prepared','confirmed','dispatch_queued',"
                "'executing','pending_approval','succeeded',"
                "'failed_not_executed','approval_rejected','denied',"
                "'cancelled','expired','recovering','unknown',"
                "'compensation_required')",
                name="ck_action_confirmation_state"),
        )
        op.create_index(
            "ix_fo_action_confirmations_owner_created",
            "fo_action_confirmations", ["actor_user_id", "created_at"])
        op.create_index(
            "ix_fo_action_confirmations_ontology_release",
            "fo_action_confirmations", ["ontology_id", "release_id"])

    if "fo_action_operations" not in tables:
        op.create_table(
            "fo_action_operations",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("confirmation_id", sa.String(), nullable=False),
            sa.Column("initial_engine_key", sa.String(length=255),
                      nullable=False),
            sa.Column("approval_log_id", sa.String(), nullable=True),
            sa.Column("technical_engine_key", sa.String(length=255),
                      nullable=True),
            sa.Column("guard_hash", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("queued_at", sa.DateTime(), nullable=True),
            sa.Column("claimed_at", sa.DateTime(), nullable=True),
            sa.Column("engine_entered_at", sa.DateTime(), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
            sa.Column("fence_token", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("worker_ref", sa.String(length=128), nullable=True),
            sa.Column("execution_log_id", sa.String(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("reconcile_evidence", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("confirmation_id",
                                name="uq_action_operation_confirmation"),
            sa.UniqueConstraint("initial_engine_key",
                                name="uq_action_operation_initial_key"),
            sa.CheckConstraint(
                "state IN ('prepared','confirmed','dispatch_queued',"
                "'executing','pending_approval','succeeded',"
                "'failed_not_executed','approval_rejected','denied',"
                "'cancelled','expired','recovering','unknown',"
                "'compensation_required')",
                name="ck_action_operation_state"),
        )
        op.create_index(
            "ix_fo_action_operations_state_claimed",
            "fo_action_operations", ["state", "claimed_at"])
        op.create_index(
            "uq_action_operation_guard_active",
            "fo_action_operations", ["guard_hash"], unique=True,
            postgresql_where=sa.text(
                f"state IN {_ACTIVE_OPERATION_STATES}"),
            sqlite_where=sa.text(
                f"state IN {_ACTIVE_OPERATION_STATES}"),
        )

    if "fo_action_external_attempts" not in tables:
        op.create_table(
            "fo_action_external_attempts",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("operation_id", sa.String(), nullable=False),
            sa.Column("effect_ordinal", sa.Integer(), nullable=False),
            sa.Column("stable_delivery_key", sa.String(length=255),
                      nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("receipt_hash", sa.String(length=128), nullable=True),
            sa.Column("evidence_ref", sa.Text(), nullable=True),
            sa.UniqueConstraint("operation_id", "effect_ordinal",
                                name="uq_action_external_attempt_effect"),
            sa.UniqueConstraint("stable_delivery_key",
                                name="uq_action_external_attempt_delivery_key"),
            sa.CheckConstraint(
                "state IN ('started','acknowledged','unknown',"
                "'proven_not_applied')",
                name="ck_action_external_attempt_state"),
        )
        op.create_index("ix_fo_action_external_attempts_operation",
                        "fo_action_external_attempts", ["operation_id"])

    if "fo_agent_query_snapshots" not in tables:
        op.create_table(
            "fo_agent_query_snapshots",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("owner_user_id", sa.String(), nullable=False),
            sa.Column("session_kind", sa.String(length=32), nullable=False),
            sa.Column("session_id", sa.String(), nullable=False),
            sa.Column("query_hash", sa.String(length=64), nullable=False),
            sa.Column("auth_hash", sa.String(length=64), nullable=False),
            sa.Column("releases", sa.JSON(), nullable=False),
            sa.Column("schema_version", sa.Integer(), nullable=False),
            sa.Column("order_spec", sa.JSON(), nullable=False),
            sa.Column("columns", sa.JSON(), nullable=False),
            sa.Column("counts", sa.JSON(), nullable=False),
            sa.Column("citations", sa.JSON(), nullable=False),
            sa.Column("page_size", sa.Integer(), nullable=False),
            sa.Column("total_row_count", sa.Integer(), nullable=False),
            sa.Column("observed_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_fo_agent_query_snapshots_owner_expires",
            "fo_agent_query_snapshots", ["owner_user_id", "expires_at"])

    if "fo_agent_query_rows" not in tables:
        op.create_table(
            "fo_agent_query_rows",
            sa.Column("snapshot_id", sa.String(), primary_key=True),
            sa.Column("ordinal", sa.Integer(), primary_key=True),
            sa.Column("row", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(
                ["snapshot_id"], ["fo_agent_query_snapshots.id"],
                ondelete="CASCADE"),
            sa.UniqueConstraint("snapshot_id", "ordinal",
                                name="uq_fo_agent_query_row_ordinal"),
        )


def _index_names(bind, table: str) -> set:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table)}


def _drop_index_guarded(table: str, name: str) -> None:
    if name in _index_names(op.get_bind(), table):
        op.drop_index(name, table_name=table)


def downgrade() -> None:
    # 账本表默认前向不 drop（契约 §7）：downgrade 仅在已清空在途数据的
    # 开发隔离库演练时执行；所有对象删除带存在性守卫（表可能由 create_all
    # 建立而未走本迁移的索引命名）。
    bind = op.get_bind()
    tables = _table_names(bind)
    if "fo_agent_query_rows" in tables:
        op.drop_table("fo_agent_query_rows")
    if "fo_agent_query_snapshots" in tables:
        _drop_index_guarded(
            "fo_agent_query_snapshots", "ix_fo_agent_query_snapshots_owner_expires")
        op.drop_table("fo_agent_query_snapshots")
    if "fo_action_external_attempts" in tables:
        _drop_index_guarded(
            "fo_action_external_attempts",
            "ix_fo_action_external_attempts_operation")
        op.drop_table("fo_action_external_attempts")
    if "fo_action_operations" in tables:
        _drop_index_guarded(
            "fo_action_operations", "uq_action_operation_guard_active")
        _drop_index_guarded(
            "fo_action_operations", "ix_fo_action_operations_state_claimed")
        op.drop_table("fo_action_operations")
    if "fo_action_confirmations" in tables:
        _drop_index_guarded(
            "fo_action_confirmations",
            "ix_fo_action_confirmations_ontology_release")
        _drop_index_guarded(
            "fo_action_confirmations", "ix_fo_action_confirmations_owner_created")
        op.drop_table("fo_action_confirmations")
    if "fo_agent_profiles" in tables and \
            "allowed_properties" in _columns(bind, "fo_agent_profiles"):
        op.drop_column("fo_agent_profiles", "allowed_properties")
