"""Bounded, expiring unified-query result snapshots and ordinal rows."""
from alembic import op
import sqlalchemy as sa

revision = "0135_unified_query_snapshots"
down_revision = "0134_integrated_assets"
branch_labels = None
depends_on = None


def _create_table(name, *elements):
    # Historical 0001 bootstraps the registered current models on a fresh DB.
    # Accept only the exact frozen schema, never an arbitrary same-named table.
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(name):
        return op.create_table(name, *elements)
    definition = sa.Table(name, sa.MetaData(), *elements)
    actual = {c["name"]: c for c in inspector.get_columns(name)}
    if set(actual) != set(definition.c.keys()):
        raise RuntimeError(f"Existing {name} has incompatible columns")
    dialect = op.get_bind().dialect
    for column in definition.columns:
        if (actual[column.name]["nullable"] != column.nullable
                or str(actual[column.name]["type"].compile(dialect=dialect)).upper() != str(column.type.compile(dialect=dialect)).upper()):
            raise RuntimeError(f"Existing {name}.{column.name} has incompatible type/nullability")
        if actual[column.name].get("default") is not None:
            raise RuntimeError(f"Existing {name}.{column.name} has an incompatible server default")
    if inspector.get_pk_constraint(name)["constrained_columns"] != [c.name for c in definition.primary_key]:
        raise RuntimeError(f"Existing {name} has an incompatible primary key")
    if inspector.get_unique_constraints(name) or inspector.get_check_constraints(name):
        raise RuntimeError(f"Existing {name} has unexpected uniqueness/check constraints")
    fks = {(tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]),
            (f.get("options") or {}).get("ondelete", "NO ACTION").upper())
           for f in inspector.get_foreign_keys(name)}
    required_fks = set()
    for column in definition.columns:
        for fk in column.foreign_keys:
            table, key = fk.target_fullname.rsplit(".", 1)
            required_fks.add(((column.name,), table, (key,), (fk.ondelete or "NO ACTION").upper()))
    if fks != required_fks:
        raise RuntimeError(f"Existing {name} has incompatible foreign keys")


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
        "v2_unified_query_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(128), nullable=False),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("auth_fingerprint", sa.String(64), nullable=False),
        sa.Column("schema_fingerprint", sa.String(64), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("columns", sa.JSON(), nullable=False),
        sa.Column("source_states", sa.JSON(), nullable=False),
        sa.Column("consistency", sa.String(40), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("result_limited", sa.Boolean(), nullable=False),
        sa.Column("total_rows", sa.BigInteger(), nullable=False),
        sa.Column("result_bytes", sa.BigInteger(), nullable=False),
        sa.Column("page_size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    _create_index("ix_v2_unified_query_snapshots_owner_user_id", "v2_unified_query_snapshots", ["owner_user_id"])
    _create_index("ix_v2_unified_query_snapshots_expires_at", "v2_unified_query_snapshots", ["expires_at"])
    _create_table(
        "v2_unified_query_rows",
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("v2_unified_query_snapshots.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ordinal", sa.BigInteger(), primary_key=True),
        sa.Column("row", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("v2_unified_query_rows")
    op.drop_table("v2_unified_query_snapshots")
