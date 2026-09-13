"""persisted process plugin manifests and lifecycle state

Revision ID: 0110_super_assistant_process_plugins
Revises: 0109_super_assistant_context_sources
"""
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.super_assistant import models as super_assistant_models

revision = "0110_super_assistant_process_plugins"
down_revision = "0109_super_assistant_context_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "users" not in set(sa_inspect(bind).get_table_names()):
        return
    super_assistant_models.SuperAssistantProcessPlugin.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    super_assistant_models.SuperAssistantProcessPlugin.__table__.drop(bind=bind, checkfirst=True)
