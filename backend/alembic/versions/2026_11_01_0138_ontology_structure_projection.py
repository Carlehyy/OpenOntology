"""Ontology structure projection fence columns.

The instance projection fence (``projection_status``) stays untouched.  These
columns are the durable fence of the rebuildable structure graph
(``OntologyStructureNode``): SQL release snapshots remain the authority and the
graph is an exploration view.  ``empty/building/ready/failed`` are stored
values; ``stale`` is derived at read time by reconciling
``structure_projection_release_id``/``_hash`` against the current release
pointer — a failed or drifted rebuild must stay visible, never fake ready.
"""
from alembic import op
import sqlalchemy as sa


revision = "0138_ontology_structure_projection"
down_revision = "0137_integrated_field_config"
branch_labels = None
depends_on = None


_COLUMNS = (
    sa.Column(
        "structure_projection_status", sa.String(length=20),
        nullable=False, server_default="empty",
    ),
    sa.Column("structure_projection_error", sa.Text(), nullable=True),
    sa.Column("structure_projection_release_id", sa.String(), nullable=True),
    sa.Column("structure_projection_hash", sa.String(length=64), nullable=True),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {
        column["name"]
        for column in inspector.get_columns("ontology_projects")
    }
    for column in _COLUMNS:
        if column.name not in existing:
            op.add_column("ontology_projects", column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {
        column["name"]
        for column in inspector.get_columns("ontology_projects")
    }
    for name in (
        "structure_projection_hash",
        "structure_projection_release_id",
        "structure_projection_error",
        "structure_projection_status",
    ):
        if name in existing:
            op.drop_column("ontology_projects", name)
