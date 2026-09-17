"""durable source tombstones for kernel context provenance

Revision ID: 0112_super_assistant_context_sources
Revises: 0111_super_assistant_kernel
"""
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.super_assistant.kernel import models as kernel_models

revision = "0112_super_assistant_context_sources"
down_revision = "0111_super_assistant_kernel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa_inspect(bind).get_table_names())
    if "users" not in existing:
        return
    kernel_models.ContextSourceTombstone.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    kernel_models.ContextSourceTombstone.__table__.drop(bind=bind, checkfirst=True)
