"""integrated database CDC runtime and external Sentinel facts

Revision ID: 0146_integrated_cdc
Revises: 0145_operations_reports
Create Date: 2026-11-10

Two CDC control-plane tables (per-connection source lifecycle, per released
mapping subscription) plus the remote-fact identity columns on
``sentinel_event_log``.  A fresh chain reaches this revision after migration
0003 already materialized every registered model, so each step either creates
the object or verifies the *exact* existing shape (columns, defaults, keys,
foreign keys, check vocabularies, partial-index predicates) and raises on any
drift instead of silently accepting it.
"""
from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa

revision = "0146_integrated_cdc"
down_revision = "0145_operations_reports"
branch_labels = None
depends_on = None

EVENT_TABLE = "sentinel_event_log"
EVENT_CHECK = "ck_sentinel_event_log_external_identity_scope"
EVENT_CHECK_SQL = (
    "source_event_id IS NULL OR (ontology_release_id IS NOT NULL "
    "AND external_asset_id IS NOT NULL "
    "AND external_mapping_id IS NOT NULL)"
)
SOURCE_STATUS_SQL = (
    "status IN ('starting', 'active', 'paused', 'error', 'gap', 'stopped')")
SUBSCRIPTION_STATUS_SQL = (
    "status IN ('starting', 'active', 'superseded', 'stopped')")


_PG_CASTS = r"::(?:character varying|varchar|text|integer|bigint|numeric)(?:\[\])?"


def _default_shape(value) -> str:
    """Server default without dialect decoration (PG casts, quotes, parens)."""
    text = "" if value is None else str(value)
    text = re.sub(_PG_CASTS, "", text)
    return re.sub(r"[\s()\"']", "", text).lower()


def _check_shape(sqltext) -> str:
    """Dialect-stable *structural* identity of a CHECK expression.

    Column, operator and literal order all participate, so ``NOT IN``, a
    different column, or a changed vocabulary are drift.  PostgreSQL deparses
    ``col IN ('a', 'b')`` as ``col = ANY (ARRAY['a'::character varying, ...])``
    (and ``NOT IN`` as ``<> ALL``); both spellings are folded back to
    ``IN`` / ``NOT IN``.  String literals are protected from the case and
    punctuation folding applied to the rest of the expression.
    """
    literals: list[str] = []

    def _stash(match) -> str:
        literals.append(match.group(0))
        return f"\x00{len(literals) - 1}\x00"

    text = re.sub(r"'(?:[^']|'')*'", _stash, str(sqltext))
    text = re.sub(_PG_CASTS, "", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    array = r"\(\s*\(?\s*array\s*\[(.*?)\]\s*\)?\s*\)"
    text = re.sub(r"=\s*any\s*" + array, r" in (\1)", text)
    text = re.sub(r"<>\s*all\s*" + array, r" not in (\1)", text)
    text = re.sub(r"[\s()\"]", "", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: literals[int(m.group(1))], text)


def _predicate_shape(value) -> str:
    return re.sub(r"[\s()\"']", "", str(value)).lower()


def _verify_column(table: str, actual: dict, column: sa.Column) -> None:
    dialect = op.get_bind().dialect
    if (actual["nullable"] != column.nullable
            or str(actual["type"].compile(dialect=dialect)).upper()
            != str(column.type.compile(dialect=dialect)).upper()):
        raise RuntimeError(
            f"Existing {table}.{column.name} has incompatible type/nullability")
    expected_default = (
        column.server_default.arg if column.server_default is not None else None)
    if _default_shape(actual.get("default")) != _default_shape(expected_default):
        raise RuntimeError(
            f"Existing {table}.{column.name} has an incompatible server default")


def _ensure_table(name: str, *elements) -> None:
    """Create the table, or accept only the exact model-created shape."""
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(name):
        op.create_table(name, *elements)
        return
    definition = sa.Table(name, sa.MetaData(), *elements)
    actual = {column["name"]: column for column in inspector.get_columns(name)}
    expected = {column.name: column for column in definition.columns}
    if set(actual) != set(expected):
        raise RuntimeError(f"Existing {name} has incompatible columns")
    for key, column in expected.items():
        _verify_column(name, actual[key], column)
    expected_pk = [column.name for column in expected.values() if column.primary_key]
    if inspector.get_pk_constraint(name).get("constrained_columns") != expected_pk:
        raise RuntimeError(f"Existing {name} has an incompatible primary key")
    expected_uniques = {
        tuple(element.columns.keys())
        for element in elements if isinstance(element, sa.UniqueConstraint)
    }
    actual_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints(name)
    }
    if expected_uniques != actual_uniques:
        raise RuntimeError(f"Existing {name} has incompatible unique constraints")
    expected_fks = {
        (tuple(element.column_keys), element.elements[0].target_fullname,
         (element.ondelete or "NO ACTION").upper())
        for element in elements if isinstance(element, sa.ForeignKeyConstraint)
    }
    actual_fks = {
        (tuple(item["constrained_columns"]),
         f"{item['referred_table']}.{item['referred_columns'][0]}",
         ((item.get("options") or {}).get("ondelete") or "NO ACTION").upper())
        for item in inspector.get_foreign_keys(name)
    }
    if expected_fks != actual_fks:
        raise RuntimeError(f"Existing {name} has incompatible foreign keys")
    expected_checks = {
        element.name: _check_shape(element.sqltext)
        for element in elements if isinstance(element, sa.CheckConstraint)
    }
    actual_checks = {
        item["name"]: _check_shape(item["sqltext"])
        for item in inspector.get_check_constraints(name)
    }
    if expected_checks != actual_checks:
        raise RuntimeError(f"Existing {name} has incompatible check constraints")


def _ensure_index(name: str, table: str, columns: list[str], *,
                  unique: bool = False, predicate: str | None = None) -> None:
    bind = op.get_bind()
    indexes = {
        item["name"]: item for item in sa.inspect(bind).get_indexes(table)
    }
    if name not in indexes:
        options = {}
        if predicate:
            options = {"postgresql_where": sa.text(predicate),
                       "sqlite_where": sa.text(predicate)}
        op.create_index(name, table, columns, unique=unique, **options)
        return
    existing = indexes[name]
    if (existing.get("column_names") != columns
            or bool(existing.get("unique")) != unique):
        raise RuntimeError(f"Existing index {name} has incompatible shape")
    dialect_options = existing.get("dialect_options") or {}
    where = (dialect_options.get("postgresql_where")
             if "postgresql_where" in dialect_options
             else dialect_options.get("sqlite_where"))
    if _predicate_shape(where if where is not None else "") != _predicate_shape(
            predicate or ""):
        raise RuntimeError(f"Existing index {name} has an incompatible predicate")


def _ensure_column(table: str, column: sa.Column) -> None:
    """Add the column, or accept only the exact model-created shape."""
    inspector = sa.inspect(op.get_bind())
    existing = {item["name"]: item for item in inspector.get_columns(table)}
    if column.name in existing:
        _verify_column(table, existing[column.name], column)
        return
    op.add_column(table, column)


def _ensure_check(table: str, name: str, sqltext: str) -> None:
    checks = {
        item["name"]: item["sqltext"]
        for item in sa.inspect(op.get_bind()).get_check_constraints(table)
    }
    if name in checks:
        if _check_shape(checks[name]) != _check_shape(sqltext):
            raise RuntimeError(f"Existing check {name} has an incompatible definition")
        return
    with op.batch_alter_table(table) as batch:
        batch.create_check_constraint(name, sqltext)


def upgrade() -> None:
    _ensure_table(
        "v2_integrated_cdc_sources",
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(20), server_default="stopped", nullable=False),
        sa.Column("identity_fingerprint", sa.String(64), nullable=True),
        sa.Column("last_position", sa.JSON(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["v2_connections.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("connection_id"),
        sa.CheckConstraint(SOURCE_STATUS_SQL, name="ck_integrated_cdc_source_status"),
        sa.CheckConstraint("generation >= 0", name="ck_integrated_cdc_source_generation"),
    )
    _ensure_table(
        "v2_integrated_cdc_subscriptions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("release_id", sa.String(), nullable=False),
        sa.Column("mapping_id", sa.String(), nullable=False),
        sa.Column("object_type_id", sa.String(), nullable=False),
        sa.Column("field_map", sa.JSON(), nullable=False),
        sa.Column("primary_key_columns", sa.JSON(), nullable=False),
        sa.Column("contract", sa.JSON(), nullable=False),
        sa.Column("schema_name", sa.String(128), nullable=False),
        sa.Column("table_name", sa.String(128), nullable=False),
        sa.Column("schema_fingerprint", sa.String(64), nullable=False),
        sa.Column("logical_fingerprint", sa.String(64), nullable=True),
        sa.Column("activation_position", sa.JSON(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(20), server_default="starting", nullable=False),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["v2_connections.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asset_id"], ["v2_integrated_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ontology_id", "release_id", "mapping_id",
                            name="uq_integrated_cdc_subscription_release_mapping"),
        sa.CheckConstraint(SUBSCRIPTION_STATUS_SQL,
                           name="ck_integrated_cdc_subscription_status"),
    )
    for name, table, columns in (
        ("ix_integrated_cdc_subscriptions_connection",
         "v2_integrated_cdc_subscriptions", ["connection_id", "status"]),
        ("ix_integrated_cdc_subscriptions_asset",
         "v2_integrated_cdc_subscriptions", ["asset_id", "status"]),
    ):
        _ensure_index(name, table, columns)

    # Remote-fact identity on the Sentinel event log (mirrors the Sentinel
    # model; this migration never redefines that contract).
    if not sa.inspect(op.get_bind()).has_table(EVENT_TABLE):
        return
    for column in (
        sa.Column("source_event_id", sa.String(64), nullable=True),
        sa.Column("event_group_id", sa.String(64), nullable=True),
        sa.Column("external_asset_id", sa.String(), nullable=True),
        sa.Column("external_mapping_id", sa.String(), nullable=True),
        sa.Column("event_snapshot", sa.JSON(), nullable=True),
    ):
        _ensure_column(EVENT_TABLE, column)
    _ensure_check(EVENT_TABLE, EVENT_CHECK, EVENT_CHECK_SQL)
    _ensure_index(
        "uq_sentinel_event_log_external", EVENT_TABLE,
        ["ontology_id", "ontology_release_id", "external_asset_id",
         "external_mapping_id", "object_type_id", "source_event_id", "key"],
        unique=True, predicate="source_event_id IS NOT NULL")
    _ensure_index(
        "ix_sentinel_event_log_external_group", EVENT_TABLE,
        ["event_group_id", "id"], predicate="event_group_id IS NOT NULL")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(EVENT_TABLE):
        columns = {item["name"] for item in inspector.get_columns(EVENT_TABLE)}
        if "source_event_id" in columns:
            # Remote facts cannot exist without their identity columns; local
            # facts (source_event_id IS NULL) are preserved untouched.
            bind.execute(sa.text(
                f"DELETE FROM {EVENT_TABLE} WHERE source_event_id IS NOT NULL"))
        index_names = {item["name"] for item in inspector.get_indexes(EVENT_TABLE)}
        for name in ("ix_sentinel_event_log_external_group",
                     "uq_sentinel_event_log_external"):
            if name in index_names:
                op.drop_index(name, table_name=EVENT_TABLE)
        checks = {item["name"] for item in inspector.get_check_constraints(EVENT_TABLE)}
        if EVENT_CHECK in checks:
            with op.batch_alter_table(EVENT_TABLE) as batch:
                batch.drop_constraint(EVENT_CHECK, type_="check")
        for name in ("event_snapshot", "external_mapping_id", "external_asset_id",
                     "event_group_id", "source_event_id"):
            if name in columns:
                op.drop_column(EVENT_TABLE, name)
    inspector = sa.inspect(bind)
    for name, table in (
        ("ix_integrated_cdc_subscriptions_asset", "v2_integrated_cdc_subscriptions"),
        ("ix_integrated_cdc_subscriptions_connection", "v2_integrated_cdc_subscriptions"),
    ):
        if inspector.has_table(table) and name in {
                item["name"] for item in inspector.get_indexes(table)}:
            op.drop_index(name, table_name=table)
    # Subscriptions reference sources' connection only through v2_connections,
    # so drop order is free; subscriptions first keeps the intent obvious.
    for table in ("v2_integrated_cdc_subscriptions", "v2_integrated_cdc_sources"):
        if inspector.has_table(table):
            op.drop_table(table)
