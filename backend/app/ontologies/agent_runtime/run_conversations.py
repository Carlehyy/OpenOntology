"""Conversation-list projection of active/latest durable runs.

Kept as a leaf module so ``conversation_service`` stays below its size bound and
does not import FastAPI.  The list path performs exactly one batched run query
for all visible conversations (no N+1).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.ontologies.agent_runtime.models import AgentConversation
from app.ontologies.agent_runtime.run_common import iso, snapshot
from app.ontologies.agent_runtime.run_models import (
    ACTIVE_RUN_STATUSES,
    AgentRun,
)


def _run_snapshot(run: AgentRun) -> dict:
    data = snapshot(run)
    data["updatedAt"] = iso(run.updated_at)
    return data


def projection_rows(
    db: Session,
    conversation_ids: list[str],
) -> list[AgentRun]:
    """Return at most latest+active rows per conversation via one window query.

    ``row_number`` partitions by conversation and orders by
    ``created_at DESC, id DESC`` for latest; the active rank orders active
    statuses first so exactly one active row (if any) survives.  No historical
    rows are materialized for the list.
    """
    if not conversation_ids:
        return []
    latest_rn = func.row_number().over(
        partition_by=AgentRun.conversation_id,
        order_by=(AgentRun.created_at.desc(), AgentRun.id.desc()),
    ).label("latest_rn")
    active_priority = case(
        (AgentRun.status.in_(ACTIVE_RUN_STATUSES), 0),
        else_=1,
    )
    active_rn = func.row_number().over(
        partition_by=AgentRun.conversation_id,
        order_by=(
            active_priority,
            AgentRun.created_at.desc(),
            AgentRun.id.desc(),
        ),
    ).label("active_rn")
    ranked = (
        db.query(AgentRun, latest_rn, active_rn)
        .filter(AgentRun.conversation_id.in_(conversation_ids))
        .subquery()
    )
    run_alias = aliased(AgentRun, ranked)
    rows = (
        db.query(run_alias)
        .filter(or_(
            ranked.c.latest_rn == 1,
            (ranked.c.active_rn == 1)
            & run_alias.status.in_(ACTIVE_RUN_STATUSES),
        ))
        .all()
    )
    rows.sort(key=lambda run: (run.created_at, run.id), reverse=True)
    return rows


def run_projections(
    db: Session,
    conversation_ids: list[str],
) -> dict[str, dict]:
    """Return conversation_id -> activeRun/latestRun snapshots."""
    if not conversation_ids:
        return {}
    rows = projection_rows(db, conversation_ids)
    latest: dict[str, AgentRun] = {}
    active: dict[str, AgentRun] = {}
    for run in rows:
        conversation_id = run.conversation_id
        latest.setdefault(conversation_id, run)
        if run.status in ACTIVE_RUN_STATUSES:
            active.setdefault(conversation_id, run)

    projections = {}
    for conversation_id in conversation_ids:
        latest_run = latest.get(conversation_id)
        active_run = active.get(conversation_id)
        projections[conversation_id] = {
            "activeRun": _run_snapshot(active_run) if active_run else None,
            "latestRun": _run_snapshot(latest_run) if latest_run else None,
        }
    return projections


def _effective_updated_at(
    conversation_updated_at: Any,
    projection: dict,
) -> str | None:
    conversation_iso = (
        iso(conversation_updated_at)
        if isinstance(conversation_updated_at, datetime)
        else conversation_updated_at
    )
    run_iso = (projection.get("latestRun") or {}).get("updatedAt")
    values = [value for value in (conversation_iso, run_iso) if value]
    return max(values) if values else None


def _merge_conversation(
    data: dict,
    projection: dict,
) -> dict:
    data.update(projection)
    data["updatedAt"] = _effective_updated_at(data.get("updatedAt"), projection)
    return data


def conversation_activity_order():
    """Order conversations by conversation or latest-run activity.

    Run deltas update ``fo_agent_runs.updated_at``; without this a long-running
    conversation whose messages were last touched at submit time could fall
    out of the top-50 window.  This is part of the same conversation query, so
    the list path still performs exactly one batched run query afterwards.
    """
    latest_run_updated = (
        select(func.max(AgentRun.updated_at))
        .where(AgentRun.conversation_id == AgentConversation.id)
        .correlate(AgentConversation)
        .scalar_subquery()
    )
    return case(
        (latest_run_updated.is_(None), AgentConversation.updated_at),
        (
            AgentConversation.updated_at > latest_run_updated,
            AgentConversation.updated_at,
        ),
        else_=latest_run_updated,
    )


def list_with_runs(
    db: Session,
    conversations: list[Any],
    *,
    serialize: Callable[[Any], dict],
) -> list[dict]:
    """Serialize conversations with run projections and activity ordering."""
    projections = run_projections(db, [conversation.id for conversation in conversations])
    items = [
        _merge_conversation(
            serialize(conversation),
            projections.get(
                conversation.id,
                {"activeRun": None, "latestRun": None},
            ),
        )
        for conversation in conversations
    ]
    items.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
    return items


def detail_with_runs(
    db: Session,
    conversation: Any,
    data: dict,
) -> dict:
    projection = run_projections(db, [conversation.id]).get(
        conversation.id,
        {"activeRun": None, "latestRun": None},
    )
    return _merge_conversation(data, projection)
