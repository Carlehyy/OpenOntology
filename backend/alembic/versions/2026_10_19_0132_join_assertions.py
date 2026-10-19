"""YIYA-36 P2-1: 受认证用户声明的跨本体关联含义登记（evidenceRefs 核验源）。"""
import sqlalchemy as sa

from alembic import op

revision = "0132_join_assertions"
down_revision = "0131_action_log_operation_lineage"
branch_labels = None
depends_on = None


def _table_names(bind) -> set:
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def upgrade() -> None:
    if "fo_agent_join_assertions" not in _table_names(op.get_bind()):
        op.create_table(
            "fo_agent_join_assertions",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("created_by", sa.String(), nullable=False),
            sa.Column("left_ontology_id", sa.String(), nullable=False),
            sa.Column("left_object_type_id", sa.String(), nullable=False),
            sa.Column("left_property", sa.String(), nullable=False),
            sa.Column("right_ontology_id", sa.String(), nullable=False),
            sa.Column("right_object_type_id", sa.String(), nullable=False),
            sa.Column("right_property", sa.String(), nullable=False),
            sa.Column("key_type", sa.String(length=16), nullable=False),
            sa.Column("business_meaning", sa.String(length=2000),
                      nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_fo_agent_join_assertions_owner",
            "fo_agent_join_assertions", ["created_by", "created_at"])


def downgrade() -> None:
    if "fo_agent_join_assertions" in _table_names(op.get_bind()):
        op.drop_index("ix_fo_agent_join_assertions_owner",
                      table_name="fo_agent_join_assertions")
        op.drop_table("fo_agent_join_assertions")
