"""AutoLoop facts and mutable projections.

Facts are append-only (also enforced by migration triggers). Network operations
must use a committed ActionStep identity; a lease expiring never proves that the
remote side effect did not happen. Missing workspaces use an owner-scoped key.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Column, DDL, DateTime, ForeignKey,
    Index, Integer, String, Text, UniqueConstraint, event, text,
)

from app.shared.database import Base

UNFINISHED = ("open", "pending_dispatch", "sending", "writeback_unknown", "writeback_failed")
TERMINAL = ("completed", "withdrawn", "handled")
STEP_STATES = ("never_sent", "queued", "sending", "unknown", "failed", "confirmed")
NO_WORKSPACE = "@owner"


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AutoLoopReceipt(Base):
    __tablename__ = "sa_autoloop_receipts"
    id = Column(String(36), primary_key=True, default=new_id)
    # Source and external identity form the authoritative inbound uniqueness key.
    source_kind = Column(String(40), nullable=False)
    source_system = Column(String(255), nullable=False)
    source_event_id = Column(String(512), nullable=False)
    dedup_key = Column(String(64), nullable=False, unique=True)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    ingest_key_id = Column(String(36), nullable=True)
    scheduled_task_id = Column(String(36), nullable=True, index=True)
    scheduled_run_id = Column(String(36), nullable=True, index=True)
    payload = Column(JSON, nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("source_kind", "source_system", "source_event_id", name="uq_sa_al_receipt_source"),
    )


class AutoLoopEvent(Base):
    __tablename__ = "sa_autoloop_events"
    seq = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    id = Column(String(36), nullable=False, default=new_id, unique=True)
    receipt_id = Column(String(36), ForeignKey("sa_autoloop_receipts.id"), nullable=True, index=True)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    type = Column(String(100), nullable=False)
    source_kind = Column(String(40), nullable=False)
    source_system = Column(String(255), nullable=False)
    source_ref = Column(String(512), nullable=True)
    parent_event_id = Column(String(36), nullable=True)
    hops = Column(Integer, nullable=False, default=0)
    happened_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    rule_id = Column(String(36), nullable=True)
    rule_version = Column(Integer, nullable=True)
    rule_snapshot = Column(JSON, nullable=True)
    action = Column(String(60), nullable=False)
    issue_ref = Column(String(255), nullable=True)
    decision_item_id = Column(String(36), nullable=True, index=True)
    action_id = Column(String(36), nullable=True)
    correlation_id = Column(String(255), nullable=True)
    error = Column(Text, nullable=True)
    data = Column(JSON, nullable=False, default=dict)
    __table_args__ = (CheckConstraint("hops >= 0 AND hops <= 8", name="ck_sa_al_event_hops"),)


class AutoLoopDispatch(Base):
    __tablename__ = "sa_autoloop_dispatches"
    id = Column(String(36), primary_key=True, default=new_id)
    event_id = Column(String(36), ForeignKey("sa_autoloop_events.id"), nullable=False, unique=True)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    state = Column(String(30), nullable=False, default="pending")
    version = Column(Integer, nullable=False, default=1)
    attempts = Column(Integer, nullable=False, default=0)
    lease_token = Column(String(36), nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    available_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    error = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("state IN ('pending','claimed','done','failed','awaiting_owner')", name="ck_sa_al_dispatch_state"),
        CheckConstraint("version > 0 AND attempts >= 0", name="ck_sa_al_dispatch_version"),
        Index("ix_sa_al_dispatch_due", "state", "available_at"),
    )


class AutoLoopRule(Base):
    __tablename__ = "sa_autoloop_rules"
    id = Column(String(36), primary_key=True, default=new_id)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    position = Column(Integer, nullable=False, default=0)
    match = Column(JSON, nullable=False, default=dict)
    title_template = Column(Text, nullable=False)
    description_template = Column(Text, nullable=False)
    priority = Column(String(20), nullable=True)
    assignee = Column(String(255), nullable=True)
    binding = Column(JSON, nullable=True)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (CheckConstraint("version > 0", name="ck_sa_al_rule_version"),)


class AutoLoopBinding(Base):
    __tablename__ = "sa_autoloop_bindings"
    id = Column(String(36), primary_key=True, default=new_id)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    workspace_id = Column(String(255), nullable=False, default=NO_WORKSPACE)
    source_scope = Column(String(255), nullable=False)
    source_thread = Column(String(512), nullable=False)
    issue_key = Column(String(255), nullable=False)
    provenance = Column(JSON, nullable=False)
    established_event_id = Column(String(36), ForeignKey("sa_autoloop_events.id"), nullable=False)
    rule_id = Column(String(36), nullable=True)
    rule_version = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("owner_id", "workspace_id", "source_scope", "source_thread", name="uq_sa_al_binding_scope"),
    )


class AutoLoopDecision(Base):
    __tablename__ = "sa_autoloop_decisions"
    id = Column(String(36), primary_key=True, default=new_id)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    workspace_id = Column(String(255), nullable=False, default=NO_WORKSPACE)
    issue_key = Column(String(255), nullable=True)
    source = Column(String(30), nullable=False)
    status = Column(String(30), nullable=False, default="open")
    title = Column(String(500), nullable=False)
    question = Column(Text, nullable=False)
    options = Column(JSON, nullable=False, default=list)
    recommended_option_id = Column(String(100), nullable=True)
    priority = Column(String(10), nullable=False, default="medium")
    content_version = Column(Integer, nullable=False, default=1)
    version = Column(Integer, nullable=False, default=1)
    # need_key identifies the actionable need, not the current model wording or
    # latest message ID. Consumed keys cannot be resurrected by a late scan.
    need_key = Column(String(255), nullable=False)
    evidence_baseline = Column(JSON, nullable=False, default=dict)
    current_evidence = Column(JSON, nullable=False, default=dict)
    origin_event_id = Column(String(36), ForeignKey("sa_autoloop_events.id"), nullable=True)
    submitted_value = Column(JSON, nullable=True)
    submitted_by = Column(String(36), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    closed_by = Column(String(36), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    close_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("source IN ('watchdog','assistant','kernel_fallback')", name="ck_sa_al_decision_source"),
        CheckConstraint("status IN ('open','pending_dispatch','sending','writeback_unknown','writeback_failed','completed','withdrawn','handled')", name="ck_sa_al_decision_state"),
        CheckConstraint("priority IN ('low','medium','high')", name="ck_sa_al_decision_priority"),
        CheckConstraint("content_version > 0 AND version > 0", name="ck_sa_al_decision_version"),
        Index("uq_sa_al_unfinished_issue", "owner_id", "workspace_id", "issue_key", unique=True,
              postgresql_where=text("status IN ('open','pending_dispatch','sending','writeback_unknown','writeback_failed')"),
              sqlite_where=text("status IN ('open','pending_dispatch','sending','writeback_unknown','writeback_failed')")),
        Index("uq_sa_al_unlinked_need", "owner_id", "workspace_id", "need_key", unique=True,
              postgresql_where=text("issue_key IS NULL AND status IN ('open','pending_dispatch','sending','writeback_unknown','writeback_failed')"),
              sqlite_where=text("issue_key IS NULL AND status IN ('open','pending_dispatch','sending','writeback_unknown','writeback_failed')")),
        Index("ix_sa_al_decision_owner_state", "owner_id", "status"),
    )


class AutoLoopDecisionRequest(Base):
    """Immutable accepted endpoint request; identity replay never creates an action."""
    __tablename__ = "sa_autoloop_decision_requests"
    id = Column(String(36), primary_key=True, default=new_id)
    decision_item_id = Column(String(36), ForeignKey("sa_autoloop_decisions.id"), nullable=False)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    category = Column(String(30), nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    actor_id = Column(String(36), nullable=False)
    content_version = Column(Integer, nullable=False)
    value = Column(JSON, nullable=False)
    result = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("decision_item_id", "category", "idempotency_key", name="uq_sa_al_decision_request"),
        CheckConstraint("category IN ('decide','mark_handled')", name="ck_sa_al_request_category"),
    )


class AutoLoopAction(Base):
    __tablename__ = "sa_autoloop_actions"
    id = Column(String(36), primary_key=True, default=new_id)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    event_id = Column(String(36), ForeignKey("sa_autoloop_events.id"), nullable=True)
    decision_item_id = Column(String(36), ForeignKey("sa_autoloop_decisions.id"), nullable=True, unique=True)
    state = Column(String(30), nullable=False, default="pending")
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("state IN ('pending','sending','unknown','failed','partial','confirmed','cancelled')", name="ck_sa_al_action_state"),
        UniqueConstraint("event_id", name="uq_sa_al_action_event"),
    )


class AutoLoopActionStep(Base):
    __tablename__ = "sa_autoloop_action_steps"
    id = Column(String(36), primary_key=True, default=new_id)
    action_id = Column(String(36), ForeignKey("sa_autoloop_actions.id"), nullable=False, index=True)
    kind = Column(String(60), nullable=False)
    ordinal = Column(Integer, nullable=False)
    state = Column(String(30), nullable=False, default="never_sent")
    # A cancelled step remains never_sent/queued. This timestamp is separate
    # because safe retries may requeue an already attempted failed request.
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    ever_sent_at = Column(DateTime(timezone=True), nullable=True)
    remote_request_id = Column(String(36), nullable=False, default=new_id, unique=True)
    request_payload = Column(JSON, nullable=False)
    remote_object_id = Column(String(255), nullable=True)
    remote_result = Column(JSON, nullable=True)
    execution_evidence = Column(JSON, nullable=True)
    reconciliation = Column(JSON, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)
    lease_token = Column(String(36), nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("action_id", "ordinal", name="uq_sa_al_step_ordinal"),
        CheckConstraint("state IN ('never_sent','queued','sending','unknown','failed','confirmed')", name="ck_sa_al_step_state"),
        CheckConstraint("version > 0 AND attempts >= 0", name="ck_sa_al_step_version"),
        CheckConstraint("state NOT IN ('sending','unknown','failed','confirmed') OR ever_sent_at IS NOT NULL", name="ck_sa_al_step_sent"),
        CheckConstraint("cancelled_at IS NULL OR (ever_sent_at IS NULL AND state IN ('never_sent','queued'))", name="ck_sa_al_step_cancel"),
    )


class AutoLoopIssueProgress(Base):
    __tablename__ = "sa_autoloop_issue_progress"
    id = Column(String(36), primary_key=True, default=new_id)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    workspace_id = Column(String(255), nullable=False, default=NO_WORKSPACE)
    issue_key = Column(String(255), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    cursors = Column(JSON, nullable=False, default=dict)
    evidence = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("owner_id", "workspace_id", "issue_key", name="uq_sa_al_issue_progress"),
    )


class AutoLoopEvidence(Base):
    __tablename__ = "sa_autoloop_evidence"
    id = Column(String(36), primary_key=True, default=new_id)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    workspace_id = Column(String(255), nullable=False, default=NO_WORKSPACE)
    issue_key = Column(String(255), nullable=False)
    stream = Column(String(30), nullable=False)
    remote_id = Column(String(255), nullable=False)
    happened_at = Column(DateTime(timezone=True), nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    payload = Column(JSON, nullable=False)
    __table_args__ = (
        UniqueConstraint("owner_id", "workspace_id", "issue_key", "stream", "remote_id", name="uq_sa_al_evidence_identity"),
        CheckConstraint("stream IN ('comment','message','timeline')", name="ck_sa_al_evidence_stream"),
    )


class AutoLoopConsumedNeed(Base):
    __tablename__ = "sa_autoloop_consumed_needs"
    id = Column(String(36), primary_key=True, default=new_id)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    workspace_id = Column(String(255), nullable=False, default=NO_WORKSPACE)
    issue_key = Column(String(255), nullable=False)
    need_key = Column(String(255), nullable=False)
    decision_item_id = Column(String(36), ForeignKey("sa_autoloop_decisions.id"), nullable=False)
    evidence_watermark = Column(JSON, nullable=False)
    reason = Column(String(30), nullable=False)
    consumed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("owner_id", "workspace_id", "issue_key", "need_key", name="uq_sa_al_consumed_need"),
    )


FACT_TABLES = (
    AutoLoopReceipt.__table__, AutoLoopEvent.__table__,
    AutoLoopDecisionRequest.__table__, AutoLoopEvidence.__table__,
    AutoLoopConsumedNeed.__table__,
)
AUTOLOOP_TABLES = (
    AutoLoopReceipt.__table__, AutoLoopEvent.__table__, AutoLoopDispatch.__table__,
    AutoLoopRule.__table__, AutoLoopBinding.__table__, AutoLoopDecision.__table__,
    AutoLoopDecisionRequest.__table__, AutoLoopAction.__table__, AutoLoopActionStep.__table__,
    AutoLoopIssueProgress.__table__, AutoLoopEvidence.__table__, AutoLoopConsumedNeed.__table__,
)


def _immutable_fact(_mapper, _connection, _target):
    raise ValueError("AutoLoop facts are append-only")


for _model in (AutoLoopReceipt, AutoLoopEvent, AutoLoopDecisionRequest, AutoLoopEvidence, AutoLoopConsumedNeed):
    event.listen(_model, "before_update", _immutable_fact)
    event.listen(_model, "before_delete", _immutable_fact)
    for _operation in ("UPDATE", "DELETE"):
        event.listen(_model.__table__, "after_create", DDL(
            f"CREATE TRIGGER {_model.__tablename__}_{_operation.lower()}_immutable "
            f"BEFORE {_operation} ON {_model.__tablename__} "
            "BEGIN SELECT RAISE(ABORT, 'AutoLoop facts are append-only'); END"
        ).execute_if(dialect="sqlite"))
    # Per-table functions keep metadata create/drop scoped to the caller's PG
    # search_path, including concurrent isolated verification schemas.
    _function = f"{_model.__tablename__}_immutable"
    event.listen(_model.__table__, "after_create", DDL(
        f"CREATE OR REPLACE FUNCTION {_function}() RETURNS trigger LANGUAGE plpgsql AS "
        "$$ BEGIN RAISE EXCEPTION 'AutoLoop facts are append-only'; END $$"
    ).execute_if(dialect="postgresql"))
    event.listen(_model.__table__, "after_create", DDL(
        f"CREATE TRIGGER {_function} BEFORE UPDATE OR DELETE ON {_model.__tablename__} "
        f"FOR EACH ROW EXECUTE FUNCTION {_function}()"
    ).execute_if(dialect="postgresql"))
    event.listen(_model.__table__, "after_drop", DDL(
        f"DROP FUNCTION IF EXISTS {_function}()"
    ).execute_if(dialect="postgresql"))
