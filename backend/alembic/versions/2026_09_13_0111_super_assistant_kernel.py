"""super assistant kernel.v1 execution facts

Revision ID: 0111_super_assistant_kernel
Revises: 0107_mapping_suggestion_queue
"""

from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.super_assistant.kernel import models as kernel_models


revision = "0111_super_assistant_kernel"
down_revision = "0110_sa_browser_source"
branch_labels = None
depends_on = None


_TABLES = (
    "super_assistant_execution_runs",
    "super_assistant_execution_turns",
    "super_assistant_execution_steps",
    "super_assistant_execution_calls",
    "super_assistant_execution_attempts",
    "super_assistant_execution_events",
    "super_assistant_execution_inbox",
    "super_assistant_execution_approvals",
    "super_assistant_execution_context_snapshots",
    "super_assistant_execution_artifacts",
    "super_assistant_capability_revisions",
    "super_assistant_execution_projection_cursors",
    "super_assistant_execution_dispatch_outbox",
    "super_assistant_execution_commands",
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa_inspect(bind).get_table_names())
    required = {"users", "super_assistant_conversations"}
    if not required <= existing:
        # Keep the same defensive behavior as other migrations used by partial
        # migration tests. A normal application database always has these parents.
        return
    metadata = kernel_models.Base.metadata
    for name in _TABLES:
        metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    metadata = kernel_models.Base.metadata
    for name in reversed(_TABLES):
        metadata.tables[name].drop(bind=bind, checkfirst=True)
