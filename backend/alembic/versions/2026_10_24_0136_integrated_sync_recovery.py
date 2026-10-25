"""Immutable audit receipts for abandoned integrated-sync recovery.

The sync run keeps its owner lease inside the frozen ``snapshot_json`` manifest;
this migration adds the durable recovery ledger written when a provably dead
owner's private relation is disposed.  No source cursor is resumed anywhere.
"""
from alembic import op
import sqlalchemy as sa
import re


def _sql_shape(value):
    # PostgreSQL adds parentheses/casts to numeric defaults and check clauses.
    return re.sub(r"[\s()\"']|::(?:bigint|integer)", "", ("" if value is None else str(value))).lower()


revision = "0136_integrated_sync_recovery"
down_revision = "0135_unified_query_snapshots"
branch_labels = None
depends_on = None


def _create_table(name, *elements):
    """0003 creates current registered models first; accept only that exact shape."""
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(name):
        return op.create_table(name, *elements)
    definition = sa.Table(name, sa.MetaData(), *elements)
    columns = {c["name"]: c for c in inspector.get_columns(name)}
    expected = {c.name: c for c in definition.columns}
    if set(columns) != set(expected):
        raise RuntimeError(f"Existing {name} has incompatible columns")
    dialect = op.get_bind().dialect
    for key, column in expected.items():
        actual = columns[key]
        if (actual["nullable"] != column.nullable
                or str(actual["type"].compile(dialect=dialect)).upper()
                != str(column.type.compile(dialect=dialect)).upper()):
            raise RuntimeError(f"Existing {name}.{key} has incompatible type/nullability")
        default = column.server_default.arg if column.server_default is not None else None
        if _sql_shape(actual.get("default")) != _sql_shape(default):
            raise RuntimeError(f"Existing {name}.{key} has an incompatible server default")
    pk = [c.name for c in expected.values() if c.primary_key]
    if inspector.get_pk_constraint(name)["constrained_columns"] != pk:
        raise RuntimeError(f"Existing {name} has an incompatible primary key")
    uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(name)}
    required_uniques = {(c.name,) for c in expected.values() if c.unique}
    required_uniques |= {
        tuple(c.columns.keys()) for c in elements if isinstance(c, sa.UniqueConstraint)}
    if required_uniques != uniques:
        raise RuntimeError(f"Existing {name} has incompatible uniqueness constraints")
    actual_fks = {(tuple(f["constrained_columns"]), f["referred_table"],
                   tuple(f["referred_columns"]),
                   (f.get("options") or {}).get("ondelete", "NO ACTION").upper())
                  for f in inspector.get_foreign_keys(name)}
    if actual_fks:
        raise RuntimeError(f"Existing {name} has unexpected foreign keys")
    checks = {c["name"]: _sql_shape(c["sqltext"])
              for c in inspector.get_check_constraints(name)}
    required_checks = {c.name: _sql_shape(c.sqltext)
                       for c in elements if isinstance(c, sa.CheckConstraint)}
    if checks != required_checks:
        raise RuntimeError(f"Existing {name} has incompatible check constraints")


def _create_index(name, table, columns):
    existing = {i["name"]: i for i in sa.inspect(op.get_bind()).get_indexes(table)}
    if name in existing:
        index = existing[name]
        options = index.get("dialect_options") or {}
        if (index["column_names"] != columns or index["unique"] or index.get("column_sorting")
                or any(value not in (None, False, [], {}, "btree") for value in options.values())):
            raise RuntimeError(f"Existing index {name} has an incompatible definition")
        return
    op.create_index(name, table, columns)


def upgrade():
    _create_table(
        "v2_integrated_sync_recoveries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("dataset_id", sa.String(), nullable=False),
        sa.Column("original_status", sa.String(20), nullable=False),
        sa.Column("owner_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ownership_method", sa.String(40), nullable=False),
        sa.Column("ownership_proof", sa.JSON(), nullable=True),
        sa.Column("recovered_by", sa.String(200), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("physical_table", sa.String(63), nullable=False),
        sa.Column("table_existed", sa.Boolean(), nullable=False),
        sa.Column("table_dropped", sa.Boolean(), nullable=False),
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", name="uq_integrated_sync_recovery_run"),
    )
    _create_index(
        "ix_integrated_sync_recoveries_recovered_at",
        "v2_integrated_sync_recoveries", ["recovered_at"])


def downgrade():
    # Explicit destructive administration only; the recovery audit is never
    # removed by application cleanup code.
    op.drop_table("v2_integrated_sync_recoveries")
