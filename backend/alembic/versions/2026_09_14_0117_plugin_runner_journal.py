"""persist external plugin-runner invocation journal

Revision ID: 0117_plugin_runner_journal
Revises: 0116_remote_agent_result_artifacts
"""
from alembic import op

from app.super_assistant import models as super_assistant_models


revision = "0117_plugin_runner_journal"
down_revision = "0116_remote_agent_result_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The ORM table carries the same bounded indexes and JSON defaults used by
    # the service.  checkfirst keeps fresh and already-partially-upgraded
    # deployments safe when an operator retries the migration.
    super_assistant_models.SuperAssistantPluginRunnerJournal.__table__.create(
        bind=op.get_bind(), checkfirst=True,
    )


def downgrade() -> None:
    super_assistant_models.SuperAssistantPluginRunnerJournal.__table__.drop(
        bind=op.get_bind(), checkfirst=True,
    )
