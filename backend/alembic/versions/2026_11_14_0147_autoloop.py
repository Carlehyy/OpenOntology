"""AutoLoop durable facts and dispatch projections.

Revision ID: 0147_autoloop
Revises: 0146_integrated_cdc

Frozen definitions intentionally do not import application models. Historical
0003 creates registered tables on a fresh chain; existing objects must therefore
match this revision exactly. Downgrade removes AutoLoop data: pause ingress and
workers, export its facts and restore the backup to recover that history.
"""
from __future__ import annotations

import re

from alembic import op
from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey,
    Index, Integer, MetaData, String, Table, Text, UniqueConstraint, inspect, text,
)

revision = "0147_autoloop"
down_revision = "0146_integrated_cdc"
branch_labels = None
depends_on = None

metadata = MetaData()
Table("users", metadata, Column("id", String, primary_key=True))

sa_autoloop_receipts = Table('sa_autoloop_receipts', metadata,
    Column('id', String(length=36), primary_key=True, nullable=False),
    Column('source_kind', String(length=40), primary_key=False, nullable=False),
    Column('source_system', String(length=255), primary_key=False, nullable=False),
    Column('source_event_id', String(length=512), primary_key=False, nullable=False),
    Column('dedup_key', String(length=64), primary_key=False, nullable=False),
    Column('owner_id', String(length=36), ForeignKey('users.id'), primary_key=False, nullable=True),
    Column('ingest_key_id', String(length=36), primary_key=False, nullable=True),
    Column('scheduled_task_id', String(length=36), primary_key=False, nullable=True),
    Column('scheduled_run_id', String(length=36), primary_key=False, nullable=True),
    Column('payload', JSON(), primary_key=False, nullable=False),
    Column('accepted_at', DateTime(timezone=True), primary_key=False, nullable=False),
    UniqueConstraint('dedup_key', name=None),
    UniqueConstraint('source_kind', 'source_system', 'source_event_id', name='uq_sa_al_receipt_source'),
)
Index('ix_sa_autoloop_receipts_owner_id', sa_autoloop_receipts.c.owner_id, unique=False)
Index('ix_sa_autoloop_receipts_scheduled_run_id', sa_autoloop_receipts.c.scheduled_run_id, unique=False)
Index('ix_sa_autoloop_receipts_scheduled_task_id', sa_autoloop_receipts.c.scheduled_task_id, unique=False)

sa_autoloop_events = Table('sa_autoloop_events', metadata,
    Column('seq', BigInteger().with_variant(Integer, "sqlite"), primary_key=True, nullable=False),
    Column('id', String(length=36), primary_key=False, nullable=False),
    Column('receipt_id', String(length=36), ForeignKey('sa_autoloop_receipts.id'), primary_key=False, nullable=True),
    Column('owner_id', String(length=36), ForeignKey('users.id'), primary_key=False, nullable=True),
    Column('type', String(length=100), primary_key=False, nullable=False),
    Column('source_kind', String(length=40), primary_key=False, nullable=False),
    Column('source_system', String(length=255), primary_key=False, nullable=False),
    Column('source_ref', String(length=512), primary_key=False, nullable=True),
    Column('parent_event_id', String(length=36), primary_key=False, nullable=True),
    Column('hops', Integer(), primary_key=False, nullable=False),
    Column('happened_at', DateTime(timezone=True), primary_key=False, nullable=False),
    Column('rule_id', String(length=36), primary_key=False, nullable=True),
    Column('rule_version', Integer(), primary_key=False, nullable=True),
    Column('rule_snapshot', JSON(), primary_key=False, nullable=True),
    Column('action', String(length=60), primary_key=False, nullable=False),
    Column('issue_ref', String(length=255), primary_key=False, nullable=True),
    Column('decision_item_id', String(length=36), primary_key=False, nullable=True),
    Column('action_id', String(length=36), primary_key=False, nullable=True),
    Column('correlation_id', String(length=255), primary_key=False, nullable=True),
    Column('error', Text(), primary_key=False, nullable=True),
    Column('data', JSON(), primary_key=False, nullable=False),
    UniqueConstraint('id', name=None),
    CheckConstraint('hops >= 0 AND hops <= 8', name='ck_sa_al_event_hops'),
)
Index('ix_sa_autoloop_events_decision_item_id', sa_autoloop_events.c.decision_item_id, unique=False)
Index('ix_sa_autoloop_events_owner_id', sa_autoloop_events.c.owner_id, unique=False)
Index('ix_sa_autoloop_events_receipt_id', sa_autoloop_events.c.receipt_id, unique=False)

sa_autoloop_dispatches = Table('sa_autoloop_dispatches', metadata,
    Column('id', String(length=36), primary_key=True, nullable=False),
    Column('event_id', String(length=36), ForeignKey('sa_autoloop_events.id'), primary_key=False, nullable=False),
    Column('owner_id', String(length=36), ForeignKey('users.id'), primary_key=False, nullable=True),
    Column('state', String(length=30), primary_key=False, nullable=False),
    Column('version', Integer(), primary_key=False, nullable=False),
    Column('attempts', Integer(), primary_key=False, nullable=False),
    Column('lease_token', String(length=36), primary_key=False, nullable=True),
    Column('lease_until', DateTime(timezone=True), primary_key=False, nullable=True),
    Column('available_at', DateTime(timezone=True), primary_key=False, nullable=False),
    Column('error', Text(), primary_key=False, nullable=True),
    Column('updated_at', DateTime(timezone=True), primary_key=False, nullable=False),
    UniqueConstraint('event_id', name=None),
    CheckConstraint("state IN ('pending','claimed','done','failed','awaiting_owner')", name='ck_sa_al_dispatch_state'),
    CheckConstraint('version > 0 AND attempts >= 0', name='ck_sa_al_dispatch_version'),
)
Index('ix_sa_al_dispatch_due', sa_autoloop_dispatches.c.state, sa_autoloop_dispatches.c.available_at, unique=False)
Index('ix_sa_autoloop_dispatches_owner_id', sa_autoloop_dispatches.c.owner_id, unique=False)

sa_autoloop_rules = Table('sa_autoloop_rules', metadata,
    Column('id', String(length=36), primary_key=True, nullable=False),
    Column('owner_id', String(length=36), ForeignKey('users.id'), primary_key=False, nullable=False),
    Column('name', String(length=255), primary_key=False, nullable=False),
    Column('enabled', Boolean(), primary_key=False, nullable=False),
    Column('position', Integer(), primary_key=False, nullable=False),
    Column('match', JSON(), primary_key=False, nullable=False),
    Column('title_template', Text(), primary_key=False, nullable=False),
    Column('description_template', Text(), primary_key=False, nullable=False),
    Column('priority', String(length=20), primary_key=False, nullable=True),
    Column('assignee', String(length=255), primary_key=False, nullable=True),
    Column('binding', JSON(), primary_key=False, nullable=True),
    Column('version', Integer(), primary_key=False, nullable=False),
    Column('created_at', DateTime(timezone=True), primary_key=False, nullable=False),
    Column('updated_at', DateTime(timezone=True), primary_key=False, nullable=False),
    CheckConstraint('version > 0', name='ck_sa_al_rule_version'),
)
Index('ix_sa_autoloop_rules_owner_id', sa_autoloop_rules.c.owner_id, unique=False)

sa_autoloop_bindings = Table('sa_autoloop_bindings', metadata,
    Column('id', String(length=36), primary_key=True, nullable=False),
    Column('owner_id', String(length=36), ForeignKey('users.id'), primary_key=False, nullable=False),
    Column('workspace_id', String(length=255), primary_key=False, nullable=False),
    Column('source_scope', String(length=255), primary_key=False, nullable=False),
    Column('source_thread', String(length=512), primary_key=False, nullable=False),
    Column('issue_key', String(length=255), primary_key=False, nullable=False),
    Column('provenance', JSON(), primary_key=False, nullable=False),
    Column('established_event_id', String(length=36), ForeignKey('sa_autoloop_events.id'), primary_key=False, nullable=False),
    Column('rule_id', String(length=36), primary_key=False, nullable=True),
    Column('rule_version', Integer(), primary_key=False, nullable=True),
    Column('created_at', DateTime(timezone=True), primary_key=False, nullable=False),
    UniqueConstraint('owner_id', 'workspace_id', 'source_scope', 'source_thread', name='uq_sa_al_binding_scope'),
)

sa_autoloop_decisions = Table('sa_autoloop_decisions', metadata,
    Column('id', String(length=36), primary_key=True, nullable=False),
    Column('owner_id', String(length=36), ForeignKey('users.id'), primary_key=False, nullable=False),
    Column('workspace_id', String(length=255), primary_key=False, nullable=False),
    Column('issue_key', String(length=255), primary_key=False, nullable=True),
    Column('source', String(length=30), primary_key=False, nullable=False),
    Column('status', String(length=30), primary_key=False, nullable=False),
    Column('title', String(length=500), primary_key=False, nullable=False),
    Column('question', Text(), primary_key=False, nullable=False),
    Column('options', JSON(), primary_key=False, nullable=False),
    Column('recommended_option_id', String(length=100), primary_key=False, nullable=True),
    Column('priority', String(length=10), primary_key=False, nullable=False),
    Column('content_version', Integer(), primary_key=False, nullable=False),
    Column('version', Integer(), primary_key=False, nullable=False),
    Column('need_key', String(length=255), primary_key=False, nullable=False),
    Column('evidence_baseline', JSON(), primary_key=False, nullable=False),
    Column('current_evidence', JSON(), primary_key=False, nullable=False),
    Column('origin_event_id', String(length=36), ForeignKey('sa_autoloop_events.id'), primary_key=False, nullable=True),
    Column('submitted_value', JSON(), primary_key=False, nullable=True),
    Column('submitted_by', String(length=36), primary_key=False, nullable=True),
    Column('submitted_at', DateTime(timezone=True), primary_key=False, nullable=True),
    Column('closed_by', String(length=36), primary_key=False, nullable=True),
    Column('closed_at', DateTime(timezone=True), primary_key=False, nullable=True),
    Column('close_reason', Text(), primary_key=False, nullable=True),
    Column('created_at', DateTime(timezone=True), primary_key=False, nullable=False),
    Column('updated_at', DateTime(timezone=True), primary_key=False, nullable=False),
    CheckConstraint("priority IN ('low','medium','high')", name='ck_sa_al_decision_priority'),
    CheckConstraint("source IN ('watchdog','assistant','kernel_fallback')", name='ck_sa_al_decision_source'),
    CheckConstraint("status IN ('open','pending_dispatch','sending','writeback_unknown','writeback_failed','completed','withdrawn','handled')", name='ck_sa_al_decision_state'),
    CheckConstraint('content_version > 0 AND version > 0', name='ck_sa_al_decision_version'),
)
Index('ix_sa_al_decision_owner_state', sa_autoloop_decisions.c.owner_id, sa_autoloop_decisions.c.status, unique=False)
Index('uq_sa_al_unlinked_need', sa_autoloop_decisions.c.owner_id, sa_autoloop_decisions.c.workspace_id, sa_autoloop_decisions.c.need_key, unique=True, postgresql_where=text("issue_key IS NULL AND status IN ('open','pending_dispatch','sending','writeback_unknown','writeback_failed')"), sqlite_where=text("issue_key IS NULL AND status IN ('open','pending_dispatch','sending','writeback_unknown','writeback_failed')"))
Index('uq_sa_al_unfinished_issue', sa_autoloop_decisions.c.owner_id, sa_autoloop_decisions.c.workspace_id, sa_autoloop_decisions.c.issue_key, unique=True, postgresql_where=text("status IN ('open','pending_dispatch','sending','writeback_unknown','writeback_failed')"), sqlite_where=text("status IN ('open','pending_dispatch','sending','writeback_unknown','writeback_failed')"))

sa_autoloop_decision_requests = Table('sa_autoloop_decision_requests', metadata,
    Column('id', String(length=36), primary_key=True, nullable=False),
    Column('decision_item_id', String(length=36), ForeignKey('sa_autoloop_decisions.id'), primary_key=False, nullable=False),
    Column('owner_id', String(length=36), ForeignKey('users.id'), primary_key=False, nullable=False),
    Column('category', String(length=30), primary_key=False, nullable=False),
    Column('idempotency_key', String(length=255), primary_key=False, nullable=False),
    Column('actor_id', String(length=36), primary_key=False, nullable=False),
    Column('content_version', Integer(), primary_key=False, nullable=False),
    Column('value', JSON(), primary_key=False, nullable=False),
    Column('result', JSON(), primary_key=False, nullable=False),
    Column('created_at', DateTime(timezone=True), primary_key=False, nullable=False),
    CheckConstraint("category IN ('decide','mark_handled')", name='ck_sa_al_request_category'),
    UniqueConstraint('decision_item_id', 'category', 'idempotency_key', name='uq_sa_al_decision_request'),
)

sa_autoloop_actions = Table('sa_autoloop_actions', metadata,
    Column('id', String(length=36), primary_key=True, nullable=False),
    Column('owner_id', String(length=36), ForeignKey('users.id'), primary_key=False, nullable=False),
    Column('event_id', String(length=36), ForeignKey('sa_autoloop_events.id'), primary_key=False, nullable=True),
    Column('decision_item_id', String(length=36), ForeignKey('sa_autoloop_decisions.id'), primary_key=False, nullable=True),
    Column('state', String(length=30), primary_key=False, nullable=False),
    Column('version', Integer(), primary_key=False, nullable=False),
    Column('created_at', DateTime(timezone=True), primary_key=False, nullable=False),
    Column('updated_at', DateTime(timezone=True), primary_key=False, nullable=False),
    UniqueConstraint('decision_item_id', name=None),
    CheckConstraint("state IN ('pending','sending','unknown','failed','partial','confirmed','cancelled')", name='ck_sa_al_action_state'),
    UniqueConstraint('event_id', name='uq_sa_al_action_event'),
)
Index('ix_sa_autoloop_actions_owner_id', sa_autoloop_actions.c.owner_id, unique=False)

sa_autoloop_action_steps = Table('sa_autoloop_action_steps', metadata,
    Column('id', String(length=36), primary_key=True, nullable=False),
    Column('action_id', String(length=36), ForeignKey('sa_autoloop_actions.id'), primary_key=False, nullable=False),
    Column('kind', String(length=60), primary_key=False, nullable=False),
    Column('ordinal', Integer(), primary_key=False, nullable=False),
    Column('state', String(length=30), primary_key=False, nullable=False),
    Column('cancelled_at', DateTime(timezone=True), primary_key=False, nullable=True),
    Column('ever_sent_at', DateTime(timezone=True), primary_key=False, nullable=True),
    Column('remote_request_id', String(length=36), primary_key=False, nullable=False),
    Column('request_payload', JSON(), primary_key=False, nullable=False),
    Column('remote_object_id', String(length=255), primary_key=False, nullable=True),
    Column('remote_result', JSON(), primary_key=False, nullable=True),
    Column('execution_evidence', JSON(), primary_key=False, nullable=True),
    Column('reconciliation', JSON(), primary_key=False, nullable=True),
    Column('attempts', Integer(), primary_key=False, nullable=False),
    Column('version', Integer(), primary_key=False, nullable=False),
    Column('lease_token', String(length=36), primary_key=False, nullable=True),
    Column('lease_until', DateTime(timezone=True), primary_key=False, nullable=True),
    Column('error', Text(), primary_key=False, nullable=True),
    Column('created_at', DateTime(timezone=True), primary_key=False, nullable=False),
    Column('updated_at', DateTime(timezone=True), primary_key=False, nullable=False),
    UniqueConstraint('remote_request_id', name=None),
    CheckConstraint("cancelled_at IS NULL OR (ever_sent_at IS NULL AND state IN ('never_sent','queued'))", name='ck_sa_al_step_cancel'),
    CheckConstraint("state NOT IN ('sending','unknown','failed','confirmed') OR ever_sent_at IS NOT NULL", name='ck_sa_al_step_sent'),
    CheckConstraint("state IN ('never_sent','queued','sending','unknown','failed','confirmed')", name='ck_sa_al_step_state'),
    CheckConstraint('version > 0 AND attempts >= 0', name='ck_sa_al_step_version'),
    UniqueConstraint('action_id', 'ordinal', name='uq_sa_al_step_ordinal'),
)
Index('ix_sa_autoloop_action_steps_action_id', sa_autoloop_action_steps.c.action_id, unique=False)

sa_autoloop_issue_progress = Table('sa_autoloop_issue_progress', metadata,
    Column('id', String(length=36), primary_key=True, nullable=False),
    Column('owner_id', String(length=36), ForeignKey('users.id'), primary_key=False, nullable=False),
    Column('workspace_id', String(length=255), primary_key=False, nullable=False),
    Column('issue_key', String(length=255), primary_key=False, nullable=False),
    Column('version', Integer(), primary_key=False, nullable=False),
    Column('cursors', JSON(), primary_key=False, nullable=False),
    Column('evidence', JSON(), primary_key=False, nullable=False),
    Column('updated_at', DateTime(timezone=True), primary_key=False, nullable=False),
    UniqueConstraint('owner_id', 'workspace_id', 'issue_key', name='uq_sa_al_issue_progress'),
)

sa_autoloop_evidence = Table('sa_autoloop_evidence', metadata,
    Column('id', String(length=36), primary_key=True, nullable=False),
    Column('owner_id', String(length=36), ForeignKey('users.id'), primary_key=False, nullable=False),
    Column('workspace_id', String(length=255), primary_key=False, nullable=False),
    Column('issue_key', String(length=255), primary_key=False, nullable=False),
    Column('stream', String(length=30), primary_key=False, nullable=False),
    Column('remote_id', String(length=255), primary_key=False, nullable=False),
    Column('happened_at', DateTime(timezone=True), primary_key=False, nullable=True),
    Column('observed_at', DateTime(timezone=True), primary_key=False, nullable=False),
    Column('payload', JSON(), primary_key=False, nullable=False),
    CheckConstraint("stream IN ('comment','message','timeline')", name='ck_sa_al_evidence_stream'),
    UniqueConstraint('owner_id', 'workspace_id', 'issue_key', 'stream', 'remote_id', name='uq_sa_al_evidence_identity'),
)

sa_autoloop_consumed_needs = Table('sa_autoloop_consumed_needs', metadata,
    Column('id', String(length=36), primary_key=True, nullable=False),
    Column('owner_id', String(length=36), ForeignKey('users.id'), primary_key=False, nullable=False),
    Column('workspace_id', String(length=255), primary_key=False, nullable=False),
    Column('issue_key', String(length=255), primary_key=False, nullable=False),
    Column('need_key', String(length=255), primary_key=False, nullable=False),
    Column('decision_item_id', String(length=36), ForeignKey('sa_autoloop_decisions.id'), primary_key=False, nullable=False),
    Column('evidence_watermark', JSON(), primary_key=False, nullable=False),
    Column('reason', String(length=30), primary_key=False, nullable=False),
    Column('consumed_at', DateTime(timezone=True), primary_key=False, nullable=False),
    UniqueConstraint('owner_id', 'workspace_id', 'issue_key', 'need_key', name='uq_sa_al_consumed_need'),
)

FACT_NAMES = (
    'sa_autoloop_receipts', 'sa_autoloop_events',
    'sa_autoloop_decision_requests', 'sa_autoloop_evidence',
    'sa_autoloop_consumed_needs',
)
TABLES = tuple(table for table in metadata.sorted_tables if table.name != 'users')
_PG_CASTS = r'::(?:character varying|varchar|text|integer|bigint|numeric)(?:\[\])?'


def _shape(value) -> str:
    """Normalize PostgreSQL's deparsed IN/ANY without changing literal values."""
    literals = []

    def stash(match):
        literals.append(match.group(0))
        return f'\x00{len(literals) - 1}\x00'

    sql = re.sub(r"'(?:[^']|'')*'", stash, '' if value is None else str(value))
    sql = re.sub(_PG_CASTS, '', sql).lower()
    array = r'\(\s*\(?\s*array\s*\[(.*?)\]\s*\)?\s*\)'
    sql = re.sub(r'=\s*any\s*' + array, r' in (\1)', sql)
    sql = re.sub(r'<>\s*all\s*' + array, r' not in (\1)', sql)
    sql = re.sub(r'[\s()";]', '', sql)
    return re.sub(r'\x00(\d+)\x00', lambda m: literals[int(m.group(1))], sql)


def _ensure_table(table) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(table.name):
        table.create(bind)
        return
    columns = {item['name']: item for item in inspector.get_columns(table.name)}
    if set(columns) != set(table.c.keys()):
        raise RuntimeError(f'Existing {table.name} has incompatible columns')
    for column in table.c:
        actual = columns[column.name]
        expected_type = str(column.type.compile(dialect=bind.dialect)).upper()
        if (str(actual['type'].compile(dialect=bind.dialect)).upper() != expected_type
                or actual['nullable'] != column.nullable):
            raise RuntimeError(f'Existing {table.name}.{column.name} has incompatible type/nullability')
        default = actual.get('default')
        # BIGSERIAL's sequence is the sole server default in this revision.
        if default and not (table.name == 'sa_autoloop_events'
                            and column.name == 'seq' and str(default).startswith('nextval(')):
            raise RuntimeError(f'Existing {table.name}.{column.name} has incompatible default')
    if inspector.get_pk_constraint(table.name)['constrained_columns'] != list(table.primary_key.columns.keys()):
        raise RuntimeError(f'Existing {table.name} has incompatible primary key')
    expected_uniques = {tuple(c.columns.keys()) for c in table.constraints if isinstance(c, UniqueConstraint)}
    actual_uniques = {tuple(c['column_names']) for c in inspector.get_unique_constraints(table.name)}
    if expected_uniques != actual_uniques:
        raise RuntimeError(f'Existing {table.name} has incompatible unique constraints')
    expected_fks = {(tuple(c.column_keys), c.elements[0].target_fullname,
                     (c.ondelete or 'NO ACTION').upper()) for c in table.foreign_key_constraints}
    actual_fks = {(tuple(c['constrained_columns']),
                   f"{c['referred_table']}.{c['referred_columns'][0]}",
                   ((c.get('options') or {}).get('ondelete') or 'NO ACTION').upper())
                  for c in inspector.get_foreign_keys(table.name)}
    if expected_fks != actual_fks:
        raise RuntimeError(f'Existing {table.name} has incompatible foreign keys')
    checks = {c.name: _shape(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)}
    actual_checks = {c['name']: _shape(c['sqltext']) for c in inspector.get_check_constraints(table.name)}
    if checks != actual_checks:
        raise RuntimeError(f'Existing {table.name} has incompatible check constraints')
    indexes = {item['name']: item for item in inspector.get_indexes(table.name)}
    for index in table.indexes:
        actual = indexes.get(index.name)
        if actual is None:
            index.create(bind)
            continue
        options = actual.get('dialect_options') or {}
        actual_where = options.get(f'{bind.dialect.name}_where')
        expected_where = index.dialect_options[bind.dialect.name].get('where')
        if (actual['column_names'] != list(index.columns.keys())
                or bool(actual['unique']) != index.unique
                or _shape(actual_where) != _shape(expected_where)):
            raise RuntimeError(f'Existing index {index.name} has incompatible shape/predicate')


def _ensure_immutable(name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        for operation in ('UPDATE', 'DELETE'):
            trigger = f'{name}_{operation.lower()}_immutable'
            sql = (f'CREATE TRIGGER {trigger} BEFORE {operation} ON {name} '
                   "BEGIN SELECT RAISE(ABORT, 'AutoLoop facts are append-only'); END")
            existing = bind.execute(text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
                                    {'name': trigger}).scalar()
            if existing is None:
                bind.execute(text(sql))
            elif _shape(existing) != _shape(sql):
                raise RuntimeError(f'Existing trigger {trigger} has incompatible shape')
    elif bind.dialect.name == 'postgresql':
        function = f'{name}_immutable'
        body = " BEGIN RAISE EXCEPTION 'AutoLoop facts are append-only'; END "
        existing = bind.execute(text('''
            SELECT t.tgtype, t.tgenabled, p.proname, p.prosrc
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
            WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal
        '''), {'table': name, 'trigger': function}).mappings().first()
        if existing is None:
            bind.execute(text(f'CREATE OR REPLACE FUNCTION {function}() RETURNS trigger '
                              f'LANGUAGE plpgsql AS $$ {body} $$'))
            bind.execute(text(f'CREATE TRIGGER {function} BEFORE UPDATE OR DELETE ON {name} '
                              f'FOR EACH ROW EXECUTE FUNCTION {function}()'))
        elif (existing['tgtype'] != 27 or existing['tgenabled'] != 'O'
              or existing['proname'] != function or _shape(existing['prosrc']) != _shape(body)):
            raise RuntimeError(f'Existing trigger {function} has incompatible shape')
    else:
        raise RuntimeError('AutoLoop migration supports PostgreSQL and SQLite only')


def upgrade() -> None:
    for table in TABLES:
        _ensure_table(table)
    for name in FACT_NAMES:
        _ensure_immutable(name)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(TABLES):
        table.drop(bind, checkfirst=True)
    if bind.dialect.name == 'postgresql':
        for name in FACT_NAMES:
            bind.execute(text(f'DROP FUNCTION IF EXISTS {name}_immutable()'))
