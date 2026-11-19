"""merge the operations report and native WeKnora migration branches

Revision ID: 0153_weknora_operations_merge
Revises: 0152_operations_report_plan, 0152_weknora_mcp
Create Date: 2026-11-19

Both 0152 revisions may already exist in deployed databases. Keep their
published identities and converge the two branches with an explicit no-op
merge revision so Alembic has one head without replaying either migration.
"""
from __future__ import annotations

revision = "0153_weknora_operations_merge"
down_revision = ("0152_operations_report_plan", "0152_weknora_mcp")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
