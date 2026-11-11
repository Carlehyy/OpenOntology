"""platform operations report books

Revision ID: 0145_operations_reports
Revises: 0144_hindsight_memory
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "0145_operations_reports"
down_revision = "0144_hindsight_memory"
branch_labels = None
depends_on = None

_REPORTS = "operations_reports"
_ARTICLES = "operations_report_articles"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if _REPORTS not in tables:
        op.create_table(
            _REPORTS,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_operations_reports_updated_at", _REPORTS, ["updated_at"])
        op.create_index("ix_operations_reports_sort_order", _REPORTS, ["sort_order"])
    if _ARTICLES not in tables:
        op.create_table(
            _ARTICLES,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("report_id", sa.String(), nullable=False),
            sa.Column("title", sa.String(length=240), nullable=False),
            sa.Column("markdown", sa.Text(), nullable=False),
            sa.Column("dataset_id", sa.String(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["report_id"], [_REPORTS + ".id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["dataset_id"], ["v2_datasets.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("report_id", "sort_order", name="uq_operations_report_articles_order"),
        )
        op.create_index("ix_operations_report_articles_report_order", _ARTICLES, ["report_id", "sort_order"])
        op.create_index("ix_operations_report_articles_dataset_id", _ARTICLES, ["dataset_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if _ARTICLES in tables:
        op.drop_table(_ARTICLES)
    if _REPORTS in tables:
        op.drop_table(_REPORTS)
