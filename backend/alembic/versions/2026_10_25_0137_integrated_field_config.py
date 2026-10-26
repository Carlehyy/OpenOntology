"""One-to-one logical field configuration for integrated assets.

The asset's ``schema_json``/``schema_fingerprint`` stay the pristine physical
identity; this table carries only the validated logical projection metadata
(``[{source_key, name, display_name}]``) so schema refreshes can replace the
physical schema wholesale without risking config loss.  Physical fingerprints,
drift detection and sealed version checksums never read this table at runtime
for identity decisions.
"""
from alembic import op
import sqlalchemy as sa
import re


def _sql_shape(value):
    # PostgreSQL adds parentheses/casts to numeric defaults and check clauses.
    return re.sub(r"[\s()\"']|::(?:bigint|integer)", "", ("" if value is None else str(value))).lower()


revision = "0137_integrated_field_config"
down_revision = "0136_integrated_sync_recovery"
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
    expected_fks = {((column.name,), fk.target_fullname.rsplit(".", 1)[0],
                     tuple(fk.target_fullname.rsplit(".", 1)[1:]),
                     (fk.ondelete or "NO ACTION").upper())
                    for column in expected.values() for fk in column.foreign_keys}
    actual_fks = {(tuple(f["constrained_columns"]), f["referred_table"],
                   tuple(f["referred_columns"]),
                   (f.get("options") or {}).get("ondelete", "NO ACTION").upper())
                  for f in inspector.get_foreign_keys(name)}
    if actual_fks != expected_fks:
        raise RuntimeError(f"Existing {name} has incompatible foreign keys")
    checks = {c["name"]: _sql_shape(c["sqltext"])
              for c in inspector.get_check_constraints(name)}
    required_checks = {c.name: _sql_shape(c.sqltext)
                       for c in elements if isinstance(c, sa.CheckConstraint)}
    if checks != required_checks:
        raise RuntimeError(f"Existing {name} has incompatible check constraints")


def upgrade():
    _create_table(
        "v2_integrated_asset_field_configs",
        sa.Column("asset_id", sa.String(), sa.ForeignKey(
            "v2_integrated_assets.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("field_config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("v2_integrated_asset_field_configs")
