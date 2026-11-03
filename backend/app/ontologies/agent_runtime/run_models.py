"""Durable ontology-assistant run ledger.

The conversation tables remain the audit source for completed turns.  These
tables add the runtime projection the workbench needs: a run can be queued,
watched, cancelled and retried across process restarts, and every observable
event is replayable in order.

Status transitions:
  queued -> running -> succeeded | error | cancelled | interrupted
  queued -> cancelled
  running -> interrupted (lease expiry; never automatically re-run)

Only ``queued`` and ``running`` are active.  A partial unique index enforces
one active run per conversation in both PostgreSQL and SQLite.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


ACTIVE_RUN_STATUSES = ("queued", "running")
TERMINAL_RUN_STATUSES = ("succeeded", "error", "cancelled", "interrupted")
RUN_STATUSES = ACTIVE_RUN_STATUSES + TERMINAL_RUN_STATUSES


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AgentRun(Base):
    __tablename__ = "fo_agent_runs"
    __table_args__ = (
        Index(
            "uq_fo_agent_runs_active_conversation",
            "conversation_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_fo_agent_runs_owner_status", "user_id", "status"),
        Index(
            "ix_fo_agent_runs_ontology_conversation",
            "ontology_id",
            "conversation_id",
        ),
        Index("ix_fo_agent_runs_status_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    release_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    conversation_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    user_message_id: Mapped[str] = mapped_column(String, nullable=False)

    # ``model_id`` is the request/config identity used for retry; ``model`` is
    # the concrete model name reported by the provider meta event.
    model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Legacy ``stream=false`` runs execute without model token streaming; the
    # flag is captured at submit so retries preserve the original request mode.
    stream_model: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    steps: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    citations: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    proposals: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_of: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    last_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False,
    )


class AgentRunEvent(Base):
    __tablename__ = "fo_agent_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_fo_agent_run_events_run_seq"),
        Index(
            "ix_fo_agent_run_events_type_created",
            "type",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column("type", String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
