"""Governed integrated sources, durable mutation ledger and sealed sync manifests."""
from alembic import op
import sqlalchemy as sa
import uuid
import re


def _sql_shape(value):
    # PostgreSQL adds parentheses/casts to numeric defaults and check clauses.
    return re.sub(r"[\s()\"']|::(?:bigint|integer)", "", ("" if value is None else str(value))).lower()

revision = "0134_integrated_assets"
down_revision = "0133_mcp_constrained_flag"
branch_labels = None
depends_on = None


def _create_table(name, *elements):
    """0001 creates current registered models on a fresh DB; verify that shape.

    Existing pre-0134 databases instead get the frozen DDL below. An unrelated
    or partially created table is never silently accepted just because it exists.
    """
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
                or str(actual["type"].compile(dialect=dialect)).upper() != str(column.type.compile(dialect=dialect)).upper()):
            raise RuntimeError(f"Existing {name}.{key} has incompatible type/nullability")
        default = column.server_default.arg if column.server_default is not None else None
        if _sql_shape(actual.get("default")) != _sql_shape(default):
            raise RuntimeError(f"Existing {name}.{key} has an incompatible server default")
    pk = [c.name for c in expected.values() if c.primary_key]
    if inspector.get_pk_constraint(name)["constrained_columns"] != pk:
        raise RuntimeError(f"Existing {name} has an incompatible primary key")
    uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(name)}
    required_uniques = {(c.name,) for c in expected.values() if c.unique}
    required_uniques |= {tuple(c.columns.keys()) for c in elements if isinstance(c, sa.UniqueConstraint)}
    if required_uniques != uniques:
        raise RuntimeError(f"Existing {name} has incompatible uniqueness constraints")
    named_uniques = {u["name"]: tuple(u["column_names"]) for u in inspector.get_unique_constraints(name)}
    for constraint in elements:
        if isinstance(constraint, sa.UniqueConstraint) and constraint.name:
            if named_uniques.get(constraint.name) != tuple(constraint.columns.keys()):
                raise RuntimeError(f"Existing {name} has an incompatible named uniqueness constraint")
    actual_fks = {(tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]),
                   (f.get("options") or {}).get("ondelete", "NO ACTION").upper())
                  for f in inspector.get_foreign_keys(name)}
    required_fks = set()
    for column in expected.values():
        for fk in column.foreign_keys:
            table, target = fk.target_fullname.rsplit(".", 1)
            required_fks.add(((column.name,), table, (target,), (fk.ondelete or "NO ACTION").upper()))
    if actual_fks != required_fks:
        raise RuntimeError(f"Existing {name} has incompatible foreign keys")
    checks = {c["name"]: _sql_shape(c["sqltext"]) for c in inspector.get_check_constraints(name)}
    required_checks = {c.name: _sql_shape(c.sqltext) for c in elements if isinstance(c, sa.CheckConstraint)}
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
        "v2_integrated_assets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("dataset_id", sa.String(), sa.ForeignKey("v2_datasets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("connection_id", sa.String(), sa.ForeignKey("v2_connections.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("schema_name", sa.String(128), nullable=False),
        sa.Column("table_name", sa.String(128), nullable=False),
        sa.Column("schema_json", sa.JSON(), nullable=False),
        sa.Column("schema_fingerprint", sa.String(64), nullable=False),
        sa.Column("writable", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("connection_id", "schema_name", "table_name", name="uq_integrated_asset_resource"),
        sa.UniqueConstraint("dataset_id", name="uq_integrated_asset_dataset"),
    )
    _create_index("ix_integrated_assets_connection", "v2_integrated_assets", ["connection_id"])
    _create_table(
        "v2_integrated_mutations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("v2_integrated_assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_id", sa.String(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(10), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("schema_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("affected_rows", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("asset_id", "actor_id", "idempotency_key", name="uq_integrated_mutation_key"),
    )
    _create_index("ix_integrated_mutations_asset_created", "v2_integrated_mutations", ["asset_id", "created_at"])
    _create_table(
        "v2_integrated_sync_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("dataset_id", sa.String(), sa.ForeignKey("v2_datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("v2_integrated_assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("base_version_id", sa.String(), sa.ForeignKey("v2_dataset_versions.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("version_id", sa.String(), sa.ForeignKey("v2_dataset_versions.id", ondelete="RESTRICT"), nullable=True, unique=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("physical_table", sa.String(63), nullable=False, unique=True),
        sa.Column("schema_json", sa.JSON(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("expected_rows", sa.BigInteger(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("expected_rows >= 0", name="ck_integrated_sync_expected_rows"),
        sa.CheckConstraint("row_count >= 0", name="ck_integrated_sync_row_count"),
    )
    _create_index("ix_v2_integrated_sync_runs_dataset_id", "v2_integrated_sync_runs", ["dataset_id"])
    _create_index("ix_v2_integrated_sync_runs_asset_id", "v2_integrated_sync_runs", ["asset_id"])
    _create_index("ix_integrated_sync_runs_dataset_status", "v2_integrated_sync_runs", ["dataset_id", "status"])
    _create_index("ix_integrated_sync_runs_asset_status", "v2_integrated_sync_runs", ["asset_id", "status"])


def downgrade():
    # Downgrade is explicit destructive administration. Only manifest-owned
    # identifiers whose UUID exactly matches the run may be dropped; never use
    # caller-controlled names or a broad prefix sweep.
    bind = op.get_bind()
    runs = bind.execute(sa.text("SELECT id, physical_table FROM v2_integrated_sync_runs")).all()
    for run_id, physical_table in runs:
        token = uuid.UUID(run_id).hex
        if physical_table != "lake_iv_" + token:
            raise RuntimeError("Cannot downgrade an invalid integrated relation manifest")
        if sa.inspect(bind).has_table(physical_table):
            op.drop_table(physical_table)
        if bind.dialect.name == "postgresql":
            function = bind.dialect.identifier_preparer.quote("lake_iv_freeze_" + token)
            bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {function}()")
    op.drop_table("v2_integrated_sync_runs")
    op.drop_table("v2_integrated_mutations")
    op.drop_table("v2_integrated_assets")
