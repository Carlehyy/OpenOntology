"""Curated candidate-review provenance columns.

Action-driven curated proposals are pending ``CuratedReview`` rows with
``dataset_version_id`` NULL (no candidate version exists while pending —
approve creates the real version through the native publish path).
``base_version_id`` is the approved immutable base the proposal targets
(approve-time CAS: the dataset pointer must still sit on the base), and
``proposal_key`` is the stable ``proposal-write:{confirmation_id}`` linkage
that makes confirmation → proposal unique and read-only recoverable.
Existing pipeline reviews keep both columns NULL.
"""
from alembic import op
import sqlalchemy as sa


revision = "0139_curated_candidate_provenance"
down_revision = "0138_ontology_structure_projection"
branch_labels = None
depends_on = None

_TABLE = "v2_curated_reviews"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("base_version_id", sa.String(), nullable=True))
    op.add_column(_TABLE, sa.Column("proposal_key", sa.String(length=255), nullable=True))
    op.create_index(
        "uq_curated_reviews_proposal_key", _TABLE, ["proposal_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_curated_reviews_proposal_key", table_name=_TABLE)
    op.drop_column(_TABLE, "proposal_key")
    op.drop_column(_TABLE, "base_version_id")
