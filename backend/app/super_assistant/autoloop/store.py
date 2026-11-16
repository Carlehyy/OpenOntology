"""Transactional AutoLoop operations; callers own the transaction and commit.

No method performs network I/O. Commit the receipt/action/step before publishing
or sending. Row locks serialize worker/close races on PostgreSQL; versioned
UPDATEs remain the final arbiter. Integrity errors are isolated in savepoints so
an inbound notification and its existing fanout remain in the same transaction.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    NO_WORKSPACE, TERMINAL, UNFINISHED, AutoLoopAction, AutoLoopActionStep,
    AutoLoopConsumedNeed, AutoLoopDecision, AutoLoopDecisionRequest,
    AutoLoopDispatch, AutoLoopEvent, AutoLoopEvidence, AutoLoopIssueProgress,
    AutoLoopReceipt, new_id, utcnow,
)


class StoreConflict(RuntimeError):
    def __init__(self, code: str, *, http_status: int = 409, current: Any = None):
        super().__init__(code)
        self.code, self.http_status, self.current = code, http_status, current


def workspace_key(workspace_id: str | None) -> str:
    return str(workspace_id).strip() if workspace_id and str(workspace_id).strip() else NO_WORKSPACE


@contextmanager
def _savepoint(db: Session):
    # sqlite3 legacy transaction mode does not BEGIN for SELECT/SAVEPOINT.
    # Without this, releasing the first savepoint commits caller-owned facts.
    connection = db.connection()
    if (connection.dialect.name == "sqlite"
            and not connection.connection.driver_connection.in_transaction):
        connection.exec_driver_sql("BEGIN")
    with db.begin_nested():
        yield


def append_event(db: Session, **values: Any) -> AutoLoopEvent:
    event = AutoLoopEvent(**values)
    db.add(event)
    db.flush()
    return event


def _projection_fact(db: Session, *, event_type: str, owner_id: str | None,
    state: str, data: dict, origin_event_id: str | None = None,
    decision: AutoLoopDecision | None = None, effect: AutoLoopAction | None = None,
    error: str | None = None,
) -> AutoLoopEvent:
    """Record a transition without turning its audit fact into new routed work."""
    origin = db.scalar(select(AutoLoopEvent).where(
        AutoLoopEvent.id == origin_event_id,
        AutoLoopEvent.owner_id == owner_id)) if origin_event_id else None
    if origin_event_id and origin is None:
        raise StoreConflict("event_not_found", http_status=404)
    return append_event(db, owner_id=owner_id, type=event_type, action=state,
        receipt_id=origin.receipt_id if origin else None,
        source_kind=origin.source_kind if origin else decision.source if decision else "autoloop",
        source_system=origin.source_system if origin else "openontology",
        source_ref=origin.source_ref if origin else decision.id if decision else effect.id if effect else None,
        parent_event_id=origin.id if origin else None, hops=origin.hops if origin else 0,
        rule_id=origin.rule_id if origin else None,
        rule_version=origin.rule_version if origin else None,
        rule_snapshot=origin.rule_snapshot if origin else None,
        issue_ref=decision.issue_key if decision else origin.issue_ref if origin else None,
        decision_item_id=decision.id if decision else None,
        action_id=effect.id if effect else None,
        correlation_id=origin.correlation_id if origin else decision.id if decision else effect.id if effect else None,
        error=error, data=data)


def _decision_fact(db: Session, row: AutoLoopDecision, *, created: bool = False) -> None:
    _projection_fact(db, event_type="decision.created" if created else "decision.updated",
        owner_id=row.owner_id, state=row.status, origin_event_id=row.origin_event_id,
        decision=row, data={"version": row.version, "contentVersion": row.content_version,
            "needKey": row.need_key, "workspaceId": row.workspace_id,
            "source": row.source, "originEventId": row.origin_event_id,
            "title": row.title, "question": row.question, "options": row.options,
            "recommendedOptionId": row.recommended_option_id, "priority": row.priority,
            "evidence": row.current_evidence, "submittedValue": row.submitted_value,
            "submittedBy": row.submitted_by,
            "submittedAt": row.submitted_at.isoformat() if row.submitted_at else None,
            "closedBy": row.closed_by, "closeReason": row.close_reason,
            "closedAt": row.closed_at.isoformat() if row.closed_at else None})


def _action_fact(db: Session, effect: AutoLoopAction, decision: AutoLoopDecision | None,
    event_type: str,
) -> None:
    _projection_fact(db, event_type=event_type, owner_id=effect.owner_id,
        state=effect.state, origin_event_id=effect.event_id or (decision.origin_event_id if decision else None),
        decision=decision, effect=effect, data={"version": effect.version, "steps": [
            {"id": step.id, "kind": step.kind, "ordinal": step.ordinal,
             "state": step.state, "version": step.version, "attempts": step.attempts,
             "remoteRequestId": step.remote_request_id, "remoteObjectId": step.remote_object_id,
             "request": step.request_payload, "result": step.remote_result,
             "executionEvidence": step.execution_evidence, "reconciliation": step.reconciliation,
             "error": step.error,
             "everSentAt": step.ever_sent_at.isoformat() if step.ever_sent_at else None,
             "leaseUntil": step.lease_until.isoformat() if step.lease_until else None,
             "cancelledAt": step.cancelled_at.isoformat() if step.cancelled_at else None}
            for step in _steps(db, effect.id)]})


def _dispatch_fact(db: Session, row: AutoLoopDispatch, event_type: str) -> None:
    _projection_fact(db, event_type=event_type, owner_id=row.owner_id,
        state=row.state, origin_event_id=row.event_id, error=row.error,
        data={"dispatchId": row.id, "version": row.version, "attempts": row.attempts,
              "leaseUntil": row.lease_until.isoformat() if row.lease_until else None})


def accept_receipt(
    db: Session, *, source_kind: str, source_system: str, source_event_id: str,
    owner_id: str | None, payload: dict, ingest_key_id: str | None = None,
    scheduled_task_id: str | None = None, scheduled_run_id: str | None = None,
    parent_event_id: str | None = None, hops: int = 0,
) -> tuple[AutoLoopReceipt, AutoLoopEvent, bool]:
    """Atomically insert receipt, initial fact and dispatch, or return the original."""
    if not 0 <= hops <= 8:
        raise ValueError("AutoLoop hop limit exceeded")
    identity = [source_kind, source_system, source_event_id]
    dedup_key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    query = select(AutoLoopReceipt).where(AutoLoopReceipt.dedup_key == dedup_key)
    existing = db.scalar(query)
    if existing:
        return existing, db.scalar(select(AutoLoopEvent).where(
            AutoLoopEvent.receipt_id == existing.id).order_by(AutoLoopEvent.seq)), False
    try:
        with _savepoint(db):
            receipt = AutoLoopReceipt(
                id=new_id(), source_kind=source_kind, source_system=source_system,
                source_event_id=source_event_id, dedup_key=dedup_key, owner_id=owner_id,
                payload=payload, ingest_key_id=ingest_key_id,
                scheduled_task_id=scheduled_task_id, scheduled_run_id=scheduled_run_id,
            )
            db.add(receipt)
            db.flush()
            event = append_event(db, receipt_id=receipt.id, owner_id=owner_id,
                type=f"{source_kind}.received", source_kind=source_kind,
                source_system=source_system, source_ref=source_event_id,
                parent_event_id=parent_event_id, hops=hops,
                action="accepted" if owner_id else "awaiting_owner", data=payload)
            db.add(AutoLoopDispatch(event_id=event.id, owner_id=owner_id,
                state="pending" if owner_id else "awaiting_owner"))
            db.flush()
        return receipt, event, True
    except IntegrityError:
        existing = db.scalar(query)
        if existing is None:
            raise
        event = db.scalar(select(AutoLoopEvent).where(
            AutoLoopEvent.receipt_id == existing.id).order_by(AutoLoopEvent.seq))
        return existing, event, False


def claim_dispatch(db: Session, dispatch_id: str, *, lease_seconds: int = 120) -> AutoLoopDispatch | None:
    now, token = utcnow(), new_id()
    result = db.execute(update(AutoLoopDispatch).execution_options(synchronize_session="fetch").where(
        AutoLoopDispatch.id == dispatch_id, AutoLoopDispatch.state == "pending",
        AutoLoopDispatch.available_at <= now,
    ).values(state="claimed", lease_token=token,
        lease_until=now + timedelta(seconds=lease_seconds),
        version=AutoLoopDispatch.version + 1, attempts=AutoLoopDispatch.attempts + 1,
        updated_at=now))
    if result.rowcount != 1:
        return None
    row = db.get(AutoLoopDispatch, dispatch_id, populate_existing=True)
    _dispatch_fact(db, row, "dispatch.claimed")
    return row


def finish_dispatch(db: Session, dispatch_id: str, lease_token: str, *, error: str | None = None) -> bool:
    result = db.execute(update(AutoLoopDispatch).execution_options(synchronize_session="fetch").where(
        AutoLoopDispatch.id == dispatch_id, AutoLoopDispatch.state == "claimed",
        AutoLoopDispatch.lease_token == lease_token,
    ).values(state="failed" if error else "done", error=error, lease_token=None,
        lease_until=None, version=AutoLoopDispatch.version + 1, updated_at=utcnow()))
    if result.rowcount != 1:
        return False
    _dispatch_fact(db, db.get(AutoLoopDispatch, dispatch_id, populate_existing=True), "dispatch.finished")
    return True


def recover_dispatches(db: Session) -> int:
    """Requeue routing only; log exactly the rows recovered by this transaction."""
    identities = list(db.scalars(update(AutoLoopDispatch).execution_options(synchronize_session="fetch").where(
        AutoLoopDispatch.state == "claimed", AutoLoopDispatch.lease_until < utcnow(),
    ).values(state="pending", lease_token=None, lease_until=None,
        version=AutoLoopDispatch.version + 1, updated_at=utcnow()).returning(AutoLoopDispatch.id)))
    for identity in identities:
        _dispatch_fact(db, db.get(AutoLoopDispatch, identity, populate_existing=True), "dispatch.recovered")
    return len(identities)


def _decision(db: Session, owner_id: str, decision_id: str) -> AutoLoopDecision:
    initial = db.scalar(select(AutoLoopDecision).where(
        AutoLoopDecision.owner_id == owner_id, AutoLoopDecision.id == decision_id))
    if initial is None:
        raise StoreConflict("decision_not_found", http_status=404)
    _lock_issue_scope(db, owner_id, initial.workspace_id,
                      _need_scope(initial.issue_key, initial.need_key))
    row = db.scalar(select(AutoLoopDecision).where(
        AutoLoopDecision.owner_id == owner_id, AutoLoopDecision.id == decision_id,
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise StoreConflict("decision_not_found", http_status=404)
    return row


def _cas_decision(db: Session, row: AutoLoopDecision, **values: Any) -> None:
    # contentVersion is the public optimistic-lock token for the whole card,
    # including state and operation eligibility, rather than wording alone.
    version, content_version = row.version, row.content_version
    result = db.execute(update(AutoLoopDecision).where(
        AutoLoopDecision.id == row.id, AutoLoopDecision.owner_id == row.owner_id,
        AutoLoopDecision.version == version,
        AutoLoopDecision.content_version == content_version,
    ).values(**values, version=version + 1, content_version=content_version + 1,
             updated_at=utcnow()))
    if result.rowcount != 1:
        raise StoreConflict("state_conflict", current=row.status)
    db.refresh(row)
    _decision_fact(db, row)


def _steps(db: Session, action_id: str) -> list[AutoLoopActionStep]:
    return list(db.scalars(select(AutoLoopActionStep).where(
        AutoLoopActionStep.action_id == action_id).order_by(AutoLoopActionStep.ordinal)
        .execution_options(populate_existing=True)))


def _action(db: Session, decision_id: str) -> AutoLoopAction | None:
    return db.scalar(select(AutoLoopAction).where(AutoLoopAction.decision_item_id == decision_id)
                     .execution_options(populate_existing=True))


def _replay(db: Session, row: AutoLoopDecision, category: str, key: str, *,
    actor_id: str, expected_content_version: int, value: dict,
) -> AutoLoopDecisionRequest | None:
    if not key or len(key) > 255:
        raise StoreConflict("invalid_idempotency_key", http_status=422)
    previous = db.scalar(select(AutoLoopDecisionRequest).where(
        AutoLoopDecisionRequest.decision_item_id == row.id,
        AutoLoopDecisionRequest.category == category,
        AutoLoopDecisionRequest.idempotency_key == key))
    if previous:
        original = previous.value
        same_value = (all(original.get(field) == value.get(field) for field in ("optionId", "freeText"))
                      if category == "decide" else original == value)
        if (previous.actor_id != actor_id or previous.content_version != expected_content_version
                or not same_value):
            action = _action(db, row.id)
            raise StoreConflict("idempotency_key_conflict",
                current=_current_decision(row, _steps(db, action.id) if action else []))
    return previous


def _current_decision(row: AutoLoopDecision, steps: list[AutoLoopActionStep] | None = None) -> dict:
    return {"status": row.status, "contentVersion": row.content_version,
            "writebackSteps": [{"id": step.id, "kind": step.kind, "state": step.state,
                "everSentAt": step.ever_sent_at.isoformat() if step.ever_sent_at else None,
                "error": step.error} for step in (steps or [])]}


def _assert_actionable(row: AutoLoopDecision, expected_content_version: int,
    allowed: tuple[str, ...], steps: list[AutoLoopActionStep] | None = None,
) -> None:
    # Check capability first: an old token must not turn a sending/terminal
    # conflict into a retryable content conflict.
    if row.status not in allowed:
        raise StoreConflict("state_conflict", current=_current_decision(row, steps))
    if any(step.ever_sent_at is not None or step.state not in ("never_sent", "queued")
           for step in (steps or [])):
        raise StoreConflict("side_effect_in_progress", current=_current_decision(row, steps))
    if row.content_version != expected_content_version:
        raise StoreConflict("content_version_conflict", http_status=412,
                            current=_current_decision(row, steps))


def _need_scope(issue_key: str | None, need_key: str) -> str:
    # No-Issue needs still have a stable scope shared by creation and closure.
    return issue_key or f"need:{hashlib.sha256(need_key.encode()).hexdigest()}"


def _consume(db: Session, row: AutoLoopDecision, reason: str) -> None:
    # Decision snapshots are append-only and share the decision's scope lock.
    # Keep the last accepted watermark for *each* need, including needs replaced
    # by a later question. The terminal snapshot supplies the closing watermark
    # for the current need (withdrawal may add external-followup evidence).
    facts = db.scalars(select(AutoLoopEvent).where(
        AutoLoopEvent.owner_id == row.owner_id,
        AutoLoopEvent.decision_item_id == row.id,
        AutoLoopEvent.issue_ref == row.issue_key,
        AutoLoopEvent.type.in_(("decision.created", "decision.updated")),
    ).order_by(AutoLoopEvent.seq))
    watermarks = {}
    for fact in facts:
        if fact.data["workspaceId"] == row.workspace_id:
            watermarks[fact.data["needKey"]] = fact.data["evidence"]
    watermarks[row.need_key] = row.current_evidence
    for need_key, evidence in watermarks.items():
        issue_key = _need_scope(row.issue_key, need_key)
        exists = db.scalar(select(AutoLoopConsumedNeed.id).where(
            AutoLoopConsumedNeed.owner_id == row.owner_id,
            AutoLoopConsumedNeed.workspace_id == row.workspace_id,
            AutoLoopConsumedNeed.issue_key == issue_key,
            AutoLoopConsumedNeed.need_key == need_key))
        if not exists:
            db.add(AutoLoopConsumedNeed(owner_id=row.owner_id, workspace_id=row.workspace_id,
                issue_key=issue_key, need_key=need_key, decision_item_id=row.id,
                evidence_watermark=evidence, reason=reason))


def _lock_issue_scope(db: Session, owner_id: str, workspace_id: str, issue_key: str) -> AutoLoopIssueProgress:
    query = select(AutoLoopIssueProgress).where(AutoLoopIssueProgress.owner_id == owner_id,
        AutoLoopIssueProgress.workspace_id == workspace_id, AutoLoopIssueProgress.issue_key == issue_key)
    progress = db.scalar(query.with_for_update().execution_options(populate_existing=True))
    if progress is None:
        try:
            with _savepoint(db):
                progress = AutoLoopIssueProgress(owner_id=owner_id, workspace_id=workspace_id, issue_key=issue_key)
                db.add(progress)
                db.flush()
        except IntegrityError:
            progress = db.scalar(query.with_for_update().execution_options(populate_existing=True))
            if progress is None:
                raise
    return progress


def create_or_update_decision(db: Session, *, owner_id: str, workspace_id: str | None,
    issue_key: str | None, source: str, need_key: str, title: str, question: str,
    options: list, evidence: dict, recommended_option_id: str | None = None,
    priority: str = "medium", origin_event_id: str | None = None,
) -> tuple[AutoLoopDecision | None, bool]:
    """A consumed need never reopens; the latest open question owns its source.

    An open card accepts new need/source/cause/content in one CAS snapshot. Earlier
    snapshots retain every accepted need and its evidence for terminal consumption.
    Submitted cards freeze their question and action plan; later scans are not
    accepted into that plan or consumed by its closure.
    """
    ws = workspace_key(workspace_id)
    scope = _need_scope(issue_key, need_key)
    _lock_issue_scope(db, owner_id, ws, scope)
    if db.scalar(select(AutoLoopConsumedNeed.id).where(
        AutoLoopConsumedNeed.owner_id == owner_id, AutoLoopConsumedNeed.workspace_id == ws,
        AutoLoopConsumedNeed.issue_key == scope, AutoLoopConsumedNeed.need_key == need_key)):
        return None, False
    query = select(AutoLoopDecision).where(
        AutoLoopDecision.owner_id == owner_id, AutoLoopDecision.workspace_id == ws,
        AutoLoopDecision.issue_key == issue_key, AutoLoopDecision.status.in_(UNFINISHED))
    # An unlinked assistant need must not be merged into a different unlinked need.
    if not issue_key:
        query = query.where(AutoLoopDecision.need_key == need_key)
    row = db.scalar(query.with_for_update().execution_options(populate_existing=True))
    if row is None:
        try:
            with _savepoint(db):
                row = AutoLoopDecision(id=new_id(), owner_id=owner_id, workspace_id=ws,
                    issue_key=issue_key, source=source, need_key=need_key, title=title,
                    question=question, options=options, evidence_baseline=evidence,
                    current_evidence=evidence, recommended_option_id=recommended_option_id,
                    priority=priority, origin_event_id=origin_event_id)
                db.add(row)
                db.flush()
                _decision_fact(db, row, created=True)
            return row, True
        except IntegrityError:
            row = db.scalar(query.with_for_update().execution_options(populate_existing=True))
            if row is None:
                raise
    content = {"need_key": need_key, "source": source, "origin_event_id": origin_event_id,
               "title": title, "question": question, "options": options,
               "recommended_option_id": recommended_option_id, "priority": priority,
               "current_evidence": evidence}
    if row.status == "open" and any(getattr(row, field) != value for field, value in content.items()):
        _cas_decision(db, row, **content)
    return row, False


def submit_decision(db: Session, *, owner_id: str, decision_id: str,
    actor_id: str, expected_content_version: int, idempotency_key: str,
    option_id: str | None = None, free_text: str | None = None,
    connector_base_url: str | None = None,
) -> tuple[AutoLoopDecision, AutoLoopDecisionRequest, bool]:
    row = _decision(db, owner_id, decision_id)
    text_value = (free_text or "").strip()
    previous = _replay(db, row, "decide", idempotency_key, actor_id=actor_id,
        expected_content_version=expected_content_version,
        value={"optionId": option_id, "freeText": text_value or None})
    if previous:
        return row, previous, True
    action = _action(db, row.id)
    _assert_actionable(row, expected_content_version, ("open",),
                       _steps(db, action.id) if action else [])
    chosen = next((item for item in row.options if item.get("id") == option_id), None) if option_id else None
    if (option_id and chosen is None) or (not chosen and not text_value):
        raise StoreConflict("invalid_decision_value", http_status=422)
    if row.source == "watchdog" and not row.issue_key:
        raise StoreConflict("watchdog_issue_required", http_status=422)
    value = {"optionId": option_id, "freeText": text_value or None,
             "answer": text_value or chosen["label"]}
    _cas_decision(db, row, status="pending_dispatch", submitted_value=value,
        submitted_by=actor_id, submitted_at=utcnow())
    action = AutoLoopAction(id=new_id(), owner_id=owner_id, decision_item_id=row.id)
    db.add(action)
    db.flush()
    kinds = ([] if row.source == "watchdog" else ["decision.answered"])
    if row.issue_key:
        kinds.append("issue.comment")
    for ordinal, kind in enumerate(kinds):
        db.add(AutoLoopActionStep(id=new_id(), action_id=action.id, ordinal=ordinal,
            kind=kind, state="queued", remote_request_id=new_id(), request_payload={
                "decisionItemId": row.id, "workspaceId": row.workspace_id,
                "baseUrl": connector_base_url,
                "issueRef": row.issue_key, "answer": value["answer"], "value": value}))
    db.flush()
    _action_fact(db, action, row, "action.created")
    request = AutoLoopDecisionRequest(decision_item_id=row.id, owner_id=owner_id,
        category="decide", idempotency_key=idempotency_key, actor_id=actor_id,
        content_version=expected_content_version, value=value,
        result={"actionId": action.id, "status": "pending_dispatch",
            "contentVersion": row.content_version, "updatedAt": row.updated_at.isoformat(),
            "writebackSteps": [{"id": step.id, "kind": step.kind, "state": step.state,
                "error": step.error} for step in _steps(db, action.id)]})
    db.add(request)
    db.flush()
    return row, request, False


def mark_handled(db: Session, *, owner_id: str, decision_id: str, actor_id: str,
    expected_content_version: int, idempotency_key: str, reason: str | None = None,
) -> tuple[AutoLoopDecision, AutoLoopDecisionRequest, bool]:
    row = _decision(db, owner_id, decision_id)
    reason = (reason or "").strip() or None
    previous = _replay(db, row, "mark_handled", idempotency_key, actor_id=actor_id,
        expected_content_version=expected_content_version, value={"reason": reason})
    if previous:
        return row, previous, True
    action = _action(db, row.id)
    steps = _steps(db, action.id) if action else []
    _assert_actionable(row, expected_content_version,
                       ("open", "pending_dispatch", "writeback_failed"), steps)
    _cas_decision(db, row, status="handled", closed_by=actor_id,
        closed_at=utcnow(), close_reason=reason)
    if action:
        action.state, action.version = "cancelled", action.version + 1
        for step in steps:
            result = db.execute(update(AutoLoopActionStep).execution_options(synchronize_session="fetch").where(
                AutoLoopActionStep.id == step.id, AutoLoopActionStep.version == step.version,
                AutoLoopActionStep.ever_sent_at.is_(None), AutoLoopActionStep.cancelled_at.is_(None),
                AutoLoopActionStep.state.in_(("never_sent", "queued")),
            ).values(cancelled_at=utcnow(), version=step.version + 1))
            if result.rowcount != 1:
                raise StoreConflict("worker_claimed", current=row.status)
        _action_fact(db, action, row, "action.cancelled")
    _consume(db, row, "handled")
    request = AutoLoopDecisionRequest(decision_item_id=row.id, owner_id=owner_id,
        category="mark_handled", idempotency_key=idempotency_key, actor_id=actor_id,
        content_version=expected_content_version, value={"reason": reason},
        result={"status": "handled", "label": "已人工处理",
            "contentVersion": row.content_version, "updatedAt": row.updated_at.isoformat(),
            "writebackSteps": [{"id": step.id, "kind": step.kind, "state": step.state,
                "error": step.error} for step in steps]})
    db.add(request)
    db.flush()
    return row, request, False


def withdraw_decision(db: Session, *, owner_id: str, decision_id: str,
    expected_content_version: int, reason: str, evidence: dict,
) -> AutoLoopDecision:
    row = _decision(db, owner_id, decision_id)
    _assert_actionable(row, expected_content_version, ("open",))
    if _action(db, row.id):
        raise StoreConflict("writeback_started", current=row.status)
    _cas_decision(db, row, status="withdrawn", closed_at=utcnow(),
        close_reason=reason, current_evidence=evidence)
    _consume(db, row, "withdrawn")
    db.flush()
    return row


def persist_action(db: Session, *, owner_id: str, event_id: str,
    steps: list[tuple[str, dict]],
) -> tuple[AutoLoopAction, bool]:
    """Route replay returns the committed stable action instead of inventing IDs."""
    event = db.scalar(select(AutoLoopEvent).where(
        AutoLoopEvent.id == event_id, AutoLoopEvent.owner_id == owner_id))
    if event is None:
        raise StoreConflict("event_not_found", http_status=404)
    query = select(AutoLoopAction).where(AutoLoopAction.event_id == event_id)
    existing = db.scalar(query)
    if existing:
        if existing.owner_id != owner_id:
            raise StoreConflict("action_not_found", http_status=404)
        return existing, False
    try:
        with _savepoint(db):
            action = AutoLoopAction(id=new_id(), owner_id=owner_id, event_id=event_id)
            db.add(action)
            db.flush()
            for ordinal, (kind, payload) in enumerate(steps):
                db.add(AutoLoopActionStep(id=new_id(), action_id=action.id,
                    kind=kind, ordinal=ordinal, state="queued", request_payload=payload,
                    remote_request_id=new_id()))
            db.flush()
            _action_fact(db, action, None, "action.created")
        return action, True
    except IntegrityError:
        existing = db.scalar(query)
        if existing is None or existing.owner_id != owner_id:
            raise
        return existing, False


def _lock_action(db: Session, owner_id: str, action_id: str) -> tuple[AutoLoopAction, AutoLoopDecision | None]:
    initial = db.scalar(select(AutoLoopAction).where(
        AutoLoopAction.id == action_id, AutoLoopAction.owner_id == owner_id))
    if not initial:
        raise StoreConflict("action_not_found", http_status=404)
    decision = _decision(db, owner_id, initial.decision_item_id) if initial.decision_item_id else None
    action = db.scalar(select(AutoLoopAction).where(AutoLoopAction.id == action_id)
                       .with_for_update().execution_options(populate_existing=True))
    return action, decision


def claim_step(db: Session, *, owner_id: str, step_id: str, lease_seconds: int = 120) -> AutoLoopActionStep | None:
    step = db.get(AutoLoopActionStep, step_id)
    if step is None:
        return None
    action, decision = _lock_action(db, owner_id, step.action_id)
    if action.state in ("cancelled", "confirmed") or (decision and decision.status in TERMINAL):
        return None
    steps = _steps(db, action.id)
    step = next(item for item in steps if item.id == step_id)
    # Required steps run in order. A partial/unknown earlier effect cannot be
    # bypassed by a later worker delivery.
    if any(item.ordinal < step.ordinal and item.state != "confirmed" for item in steps):
        return None
    token, now = new_id(), utcnow()
    result = db.execute(update(AutoLoopActionStep).execution_options(synchronize_session="fetch").where(
        AutoLoopActionStep.id == step_id, AutoLoopActionStep.version == step.version,
        AutoLoopActionStep.state == "queued", AutoLoopActionStep.cancelled_at.is_(None),
    ).values(state="sending", ever_sent_at=step.ever_sent_at or now, lease_token=token,
        lease_until=now + timedelta(seconds=lease_seconds), attempts=step.attempts + 1,
        version=step.version + 1, updated_at=now))
    if result.rowcount != 1:
        return None
    action.state, action.version = "sending", action.version + 1
    if decision:
        _cas_decision(db, decision, status="sending")
    _action_fact(db, action, decision, "action.step_claimed")
    db.flush()
    return db.get(AutoLoopActionStep, step_id, populate_existing=True)


def _project_action(db: Session, action: AutoLoopAction, decision: AutoLoopDecision | None) -> None:
    states = [step.state for step in _steps(db, action.id)]
    if states and all(state == "confirmed" for state in states):
        action.state, status = "confirmed", "completed"
    elif "sending" in states:
        action.state, status = "sending", "sending"
    elif "unknown" in states:
        action.state, status = "unknown", "writeback_unknown"
    elif "failed" in states:
        action.state, status = ("partial" if "confirmed" in states else "failed"), "writeback_failed"
    else:
        action.state, status = ("partial" if "confirmed" in states else "pending"), "pending_dispatch"
    action.version, action.updated_at = action.version + 1, utcnow()
    if decision and decision.status not in TERMINAL:
        values = {"status": status}
        if status == "completed":
            values.update(closed_at=utcnow(), close_reason="all_required_steps_confirmed")
        _cas_decision(db, decision, **values)
        if status == "completed":
            _consume(db, decision, "completed")


def finish_step(db: Session, *, owner_id: str, step_id: str, lease_token: str,
    outcome: str, remote_object_id: str | None = None, remote_result: dict | None = None,
    execution_evidence: dict | None = None, error: str | None = None,
    proof: dict | None = None,
) -> bool:
    if outcome not in ("confirmed", "unknown", "failed"):
        raise ValueError("invalid step outcome")
    if outcome == "failed" and not proof:
        raise ValueError("failed requires affirmative proof of nonexecution")
    step = db.get(AutoLoopActionStep, step_id)
    if step is None:
        return False
    action, decision = _lock_action(db, owner_id, step.action_id)
    result = db.execute(update(AutoLoopActionStep).execution_options(synchronize_session="fetch").where(
        AutoLoopActionStep.id == step_id, AutoLoopActionStep.state == "sending",
        AutoLoopActionStep.lease_token == lease_token,
    ).values(state=outcome, remote_object_id=remote_object_id, remote_result=remote_result,
        execution_evidence=execution_evidence, reconciliation=proof, error=error,
        lease_token=None, lease_until=None, version=AutoLoopActionStep.version + 1,
        updated_at=utcnow()))
    if result.rowcount != 1:
        return False
    _project_action(db, action, decision)
    _action_fact(db, action, decision, "action.step_finished")
    db.flush()
    return True


def recover_step(db: Session, *, owner_id: str, step_id: str) -> bool:
    """An expired sending lease is unknown, even if no response reached the worker."""
    step = db.get(AutoLoopActionStep, step_id)
    if step is None:
        return False
    action, decision = _lock_action(db, owner_id, step.action_id)
    result = db.execute(update(AutoLoopActionStep).execution_options(synchronize_session="fetch").where(
        AutoLoopActionStep.id == step_id, AutoLoopActionStep.state == "sending",
        AutoLoopActionStep.lease_until < utcnow(),
    ).values(state="unknown", error="sending_lease_expired", lease_token=None,
        lease_until=None, version=AutoLoopActionStep.version + 1, updated_at=utcnow()))
    if result.rowcount != 1:
        return False
    _project_action(db, action, decision)
    _action_fact(db, action, decision, "action.step_recovered")
    db.flush()
    return True


def reconcile_step(db: Session, *, owner_id: str, step_id: str, outcome: str,
    proof: dict, remote_object_id: str | None = None,
) -> bool:
    if outcome not in ("confirmed", "failed") or not proof:
        raise ValueError("reconciliation requires affirmative evidence")
    step = db.get(AutoLoopActionStep, step_id)
    if step is None:
        return False
    action, decision = _lock_action(db, owner_id, step.action_id)
    result = db.execute(update(AutoLoopActionStep).execution_options(synchronize_session="fetch").where(
        AutoLoopActionStep.id == step_id, AutoLoopActionStep.state == "unknown",
    ).values(state=outcome, reconciliation=proof, remote_object_id=remote_object_id,
        error=None, version=AutoLoopActionStep.version + 1, updated_at=utcnow()))
    if result.rowcount != 1:
        return False
    _project_action(db, action, decision)
    _action_fact(db, action, decision, "action.step_reconciled")
    db.flush()
    return True


def retry_failed_step(db: Session, *, owner_id: str, step_id: str) -> bool:
    step = db.get(AutoLoopActionStep, step_id)
    if step is None:
        return False
    action, decision = _lock_action(db, owner_id, step.action_id)
    db.refresh(step)
    if step.state != "failed" or not step.reconciliation:
        return False
    result = db.execute(update(AutoLoopActionStep).execution_options(synchronize_session="fetch").where(
        AutoLoopActionStep.id == step_id, AutoLoopActionStep.state == "failed",
        AutoLoopActionStep.version == step.version,
    ).values(state="queued", version=step.version + 1, updated_at=utcnow()))
    if result.rowcount != 1:
        return False
    _project_action(db, action, decision)
    _action_fact(db, action, decision, "action.step_requeued")
    db.flush()
    return True


def record_evidence(db: Session, *, owner_id: str, workspace_id: str | None,
    issue_key: str, stream: str, items: list[dict], cursor: dict,
) -> AutoLoopIssueProgress:
    """Persist all stable IDs, including late arrivals; cursors are stream-specific."""
    ws = workspace_key(workspace_id)
    progress = _lock_issue_scope(db, owner_id, ws, issue_key)
    new_ids = []
    for item in items:
        remote_id = str(item.get("id") or "")
        if not remote_id:
            raise ValueError("evidence requires a stable remote ID")
        existing = db.scalar(select(AutoLoopEvidence.id).where(
            AutoLoopEvidence.owner_id == owner_id, AutoLoopEvidence.workspace_id == ws,
            AutoLoopEvidence.issue_key == issue_key, AutoLoopEvidence.stream == stream,
            AutoLoopEvidence.remote_id == remote_id))
        if not existing:
            happened_at = None
            if isinstance(item.get("created_at"), str):
                try:
                    parsed = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                        happened_at = parsed.astimezone(timezone.utc)
                except ValueError:
                    pass  # Retain the original payload; a missing time is unknown.
            db.add(AutoLoopEvidence(owner_id=owner_id, workspace_id=ws, issue_key=issue_key,
                stream=stream, remote_id=remote_id, happened_at=happened_at, payload=item))
            new_ids.append(remote_id)
    version = progress.version
    result = db.execute(update(AutoLoopIssueProgress).where(
        AutoLoopIssueProgress.id == progress.id, AutoLoopIssueProgress.version == version,
    ).values(cursors={**progress.cursors, stream: cursor}, version=version + 1, updated_at=utcnow()))
    if result.rowcount != 1:
        raise StoreConflict("scan_progress_conflict")
    db.flush()
    db.refresh(progress)
    append_event(db, owner_id=owner_id, type="evidence.recorded", action="scanned",
        source_kind="multica", source_system="multica", source_ref=issue_key,
        issue_ref=issue_key, correlation_id=progress.id,
        data={"workspaceId": ws, "stream": stream, "version": progress.version,
              "cursors": progress.cursors, "newRemoteIds": new_ids})
    return progress
