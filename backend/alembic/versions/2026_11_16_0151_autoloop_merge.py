"""merge AutoLoop with the deployed replica and dashboard migration chain

Revision ID: 0151_autoloop_merge
Revises: 0150_operations_dashboards, 0147_autoloop
Create Date: 2026-11-16

Both branches retain their published revision identities. Alembic applies the
missing branch before this merge, whether a database starts on the deployed
0150 chain or the separately verified AutoLoop 0147 chain.
"""
from __future__ import annotations

revision = "0151_autoloop_merge"
down_revision = ("0150_operations_dashboards", "0147_autoloop")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
