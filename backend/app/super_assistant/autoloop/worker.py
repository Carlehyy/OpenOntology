"""Execute committed steps; ambiguous writes stay unknown until proven."""
from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.auth.models import User
from app.shared.database import SessionLocal
from app.super_assistant import multica_client, multica_service, provider

from . import facts, store
from .models import (
    AutoLoopAction, AutoLoopActionStep, AutoLoopBinding, AutoLoopDecision,
    AutoLoopDispatch, AutoLoopEvent, utcnow,
)

logger = logging.getLogger(__name__)


def _connection(db, owner_id, payload):
    owner = db.get(User, owner_id, populate_existing=True)
    if not owner or not owner.is_active:
        raise ValueError("multica_unavailable")
    # A committed step carries the source it was planned under: the frozen
    # profile revision when present, else its endpoint/workspace evidence
    # resolved to the unique source of that pair. A legacy payload that
    # proves neither is rejected instead of borrowing the active profile:
    # after an A→B switch that guess would retarget an unfinished step
    # planned under A onto B, so it fails closed into the manual not-sent
    # path. New steps always freeze profileRevisionId plus base/workspace.
    revision_id = str(payload.get("profileRevisionId") or "") or None
    base_url = str(payload.get("baseUrl") or "")
    workspace_id = str(payload.get("workspaceId") or "")
    if revision_id:
        config = multica_service.config_for_revision(db, owner_id, revision_id)
    elif base_url and workspace_id:
        config = multica_service.config_for_source(
            db, owner_id, base_url=base_url, workspace_id=workspace_id)
    else:
        raise ValueError("source_unprovable")
    if workspace_id and config.workspace_id != workspace_id:
        raise ValueError("workspace_changed")
    if base_url and config.base_url != base_url:
        raise ValueError("connector_changed")
    return config, multica_service.decrypt_token(config)


def _answered(db, owner_id, step, token):
    payload = step.request_payload
    decision = db.get(AutoLoopDecision, payload["decisionItemId"])
    if not decision or decision.owner_id != owner_id or decision.source == "watchdog":
        raise ValueError("invalid_answer_source")
    event = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.id == step.remote_request_id))
    if event is None:
        origin = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.id == decision.origin_event_id))
        event = store.append_event(db, id=step.remote_request_id, owner_id=owner_id,
            type="decision.answered", source_kind="decision", source_system="openontology",
            source_ref=decision.id, parent_event_id=decision.origin_event_id,
            hops=min(8, (origin.hops if origin else 0) + 1), action="accepted",
            decision_item_id=decision.id, issue_ref=decision.issue_key, correlation_id=step.id,
            data={**payload, "source": decision.source, "actor": decision.submitted_by,
                  "question": decision.question, "untrustedOriginal": decision.current_evidence.get("untrustedOriginal")})
        db.add(AutoLoopDispatch(event_id=event.id, owner_id=owner_id))
    store.finish_step(db, owner_id=owner_id, step_id=step.id, lease_token=token,
        outcome="confirmed", remote_object_id=event.id, remote_result={"eventId": event.id})


def _binding(db, action, payload, issue):
    scope, thread = payload.get("sourceScope"), payload.get("sourceThread")
    if not scope or not thread or not action.event_id:
        return
    query = select(AutoLoopBinding).where(AutoLoopBinding.owner_id == action.owner_id,
        AutoLoopBinding.workspace_id == payload["workspaceId"], AutoLoopBinding.source_scope == scope,
        AutoLoopBinding.source_thread == thread)
    existing = db.scalar(query.with_for_update())
    target = str(issue.get("identifier") or issue["id"])
    if existing:
        # A thread cannot silently switch targets because a late event arrived.
        return
    try:
        with db.begin_nested():
            provenance = {**(payload.get("provenance") or {"kind": "created_issue"}),
                "baseUrl": payload["baseUrl"]}
            if payload.get("profileRevisionId"):
                provenance["profileRevisionId"] = payload["profileRevisionId"]
            db.add(AutoLoopBinding(owner_id=action.owner_id, workspace_id=payload["workspaceId"],
                source_scope=scope, source_thread=thread, issue_key=target,
                provenance=provenance,
                established_event_id=action.event_id, rule_id=payload.get("ruleId"),
                rule_version=payload.get("ruleVersion")))
            db.flush()
    except IntegrityError:
        if db.scalar(query) is None:
            raise


def execute_step(owner_id, step_id, *, session_factory=None):
    provider.ensure_autoloop_available()
    sessions = session_factory or SessionLocal
    with sessions() as db:
        step = store.claim_step(db, owner_id=owner_id, step_id=step_id)
        if step is None:
            return False
        token, kind, payload = step.lease_token, step.kind, dict(step.request_payload)
        db.commit()
    if kind == "decision.answered":
        with sessions() as db:
            step = db.get(AutoLoopActionStep, step_id)
            _answered(db, owner_id, step, token)
            db.commit()
        return True
    outcome, error, proof, remote_id, result, execution = "failed", None, None, None, None, None
    issue = None
    writing = False
    try:
        with sessions() as db:
            config, secret = _connection(db, owner_id, payload)
            base, workspace = config.base_url, config.workspace_id
        if kind == "issue.create":
            writing = True
            issue = multica_client.create_issue(base, secret, workspace, title=payload["title"],
                description=payload.get("description"), priority=payload.get("priority"),
                assignee_id=payload.get("assigneeId"))
            remote_id = str(issue.get("identifier") or issue["id"])
            if secret and secret in remote_id:
                remote_id = None
                raise ValueError("invalid_remote_identity")
            result = {"issue": facts.issue(issue, secret)}
        elif kind == "issue.comment":
            issue = multica_client.get_issue(base, secret, workspace, payload["issueRef"])
            if not issue.get("id") or (issue.get("workspace_id") or issue.get("workspaceId")) != workspace:
                raise ValueError("issue_workspace_unverified")
            if payload["issueRef"] not in {str(issue.get("id")), str(issue.get("identifier"))}:
                raise ValueError("issue_identity_mismatch")
            writing = True
            comment = multica_client.create_comment(base, secret, workspace, payload["issueRef"],
                content=payload.get("answer") or payload["description"])
            remote_id = comment.comment_id
            if secret and secret in remote_id:
                remote_id = None
                raise ValueError("invalid_remote_identity")
            # Persist only the stable response facts needed for reconciliation.
            result = {"comment": facts.comment(comment.comment, secret), "issue": facts.issue(issue, secret)}
            execution = {"state": comment.execution_state, **facts.execution(comment.execution_evidence, secret)}
        else:
            raise ValueError("unsupported_step_kind")
        outcome = "confirmed"
    except provider.DailyLimitExceeded:
        # The committed lease remains recoverable; no further request is sent.
        raise
    except multica_client.MulticaRequestError as exc:
        outcome = exc.outcome if writing else "failed"
        error = "multica_request_unknown" if outcome == "unknown" else "multica_request_rejected"
        if outcome == "failed":
            proof = {"kind": "remote_rejection" if writing else "not_sent", "httpStatus": exc.status_code}
    except Exception:
        outcome = "unknown" if writing else "failed"
        error = "writeback_unknown" if writing else "writeback_validation_failed"
        if not writing:
            proof = {"kind": "not_sent"}
    # If this commit fails, the durable sending lease recovers to unknown. Never
    # send again merely because the successful response could not be committed.
    with sessions() as db:
        finished = store.finish_step(db, owner_id=owner_id, step_id=step_id, lease_token=token,
            outcome=outcome, remote_object_id=remote_id, remote_result=result,
            execution_evidence=execution, error=error, proof=proof)
        if finished and outcome == "confirmed" and issue:
            action = db.get(AutoLoopAction, db.get(AutoLoopActionStep, step_id).action_id)
            _binding(db, action, payload, issue)
        db.commit()
    return finished


def reconcile_unknown(owner_id, step_id, *, session_factory=None):
    """Only authoritative identities confirm an effect; content similarity cannot."""
    provider.ensure_autoloop_available()
    sessions = session_factory or SessionLocal
    with sessions() as db:
        step = db.get(AutoLoopActionStep, step_id)
        if not step or step.state != "unknown":
            return False
        action = db.get(AutoLoopAction, step.action_id)
        if not action or action.owner_id != owner_id:
            return False
        if step.kind == "decision.answered":
            event = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.id == step.remote_request_id,
                AutoLoopEvent.owner_id == owner_id, AutoLoopEvent.type == "decision.answered"))
            outcome = "confirmed" if event else "failed"
            changed = store.reconcile_step(db, owner_id=owner_id, step_id=step_id, outcome=outcome,
                proof={"kind": "local_atomic_event", "eventId": event.id if event else None},
                remote_object_id=event.id if event else None)
            if changed and not event:
                store.retry_failed_step(db, owner_id=owner_id, step_id=step_id)
            db.commit()
            return changed
        try:
            config, secret = _connection(db, owner_id, step.request_payload)
        except (ValueError, multica_service.MulticaServiceError):
            # The step's frozen source is unprovable or unusable; the unknown
            # effect stays visible for manual handling instead of being read
            # through a guessed source.
            return False
        base, workspace = config.base_url, config.workspace_id
        kind, request_id, payload = step.kind, step.remote_request_id, dict(step.request_payload)
    if kind == "issue.comment":
        read = multica_client.list_comments(base, secret, workspace, payload["issueRef"])
    elif kind == "issue.create":
        read = multica_client.list_all_issues(base, secret, workspace)
    else:
        return False
    if not read.complete:
        return False
    matches = [item for item in read.items if item.get("request_id") == request_id
               or item.get("idempotency_key") == request_id]
    # Most deployments do not expose a request correlation field. Absence or
    # matching text is not proof of nonexecution, so unknown remains visible.
    if len(matches) != 1 or not matches[0].get("id"):
        return False
    with sessions() as db:
        changed = store.reconcile_step(db, owner_id=owner_id, step_id=step_id, outcome="confirmed",
            proof={"kind": "remote_request_identity", "requestId": request_id},
            remote_object_id=str(matches[0]["id"]))
        db.commit()
        return changed


def retry_failed(owner_id, step_id, *, session_factory=None):
    """Requeue only an affirmatively unexecuted original step, with bounded attempts."""
    provider.ensure_autoloop_available()
    sessions = session_factory or SessionLocal
    with sessions() as db:
        step = db.get(AutoLoopActionStep, step_id)
        if not step or step.state != "failed" or step.attempts >= 3:
            return False
        proof = step.reconciliation or {}
        if not (proof.get("kind") == "not_sent" or
                proof.get("kind") == "remote_rejection" and proof.get("httpStatus") == 429):
            return False
        if step.kind != "decision.answered":
            try:
                _connection(db, owner_id, step.request_payload)
            except (ValueError, multica_service.MulticaServiceError):
                return False  # unprovable or unusable source: never requeue
        changed = store.retry_failed_step(db, owner_id=owner_id, step_id=step_id)
        db.commit()
        return changed


def scan_actions(*, session_factory=None, limit=50):
    provider.ensure_autoloop_available()
    sessions = session_factory or SessionLocal
    with sessions() as db:
        expired = list(db.execute(select(AutoLoopAction.owner_id, AutoLoopActionStep.id).join(
            AutoLoopActionStep, AutoLoopActionStep.action_id == AutoLoopAction.id).where(
            AutoLoopActionStep.state == "sending", AutoLoopActionStep.lease_until < utcnow())))
    for owner, identity in expired:
        with sessions() as db:
            store.recover_step(db, owner_id=owner, step_id=identity)
            db.commit()
    with sessions() as db:
        rows = []
        # Each category owns a bounded share. Unresolved old effects must not
        # prevent newly queued work, or the other recovery category, from running.
        for state in ("queued", "failed", "unknown"):
            query = select(AutoLoopAction.owner_id, AutoLoopActionStep.id, AutoLoopActionStep.state).join(
                AutoLoopActionStep, AutoLoopActionStep.action_id == AutoLoopAction.id).where(
                    AutoLoopAction.state.not_in(("confirmed", "cancelled")),
                    AutoLoopActionStep.cancelled_at.is_(None), AutoLoopActionStep.state == state)
            if state == "failed":
                query = query.where(AutoLoopActionStep.attempts < 3, or_(
                    AutoLoopActionStep.reconciliation["kind"].as_string() == "not_sent",
                    (AutoLoopActionStep.reconciliation["kind"].as_string() == "remote_rejection")
                    & (AutoLoopActionStep.reconciliation["httpStatus"].as_integer() == 429)))
            order = AutoLoopActionStep.updated_at if state == "unknown" else AutoLoopActionStep.created_at
            rows.extend(db.execute(query.order_by(order, AutoLoopActionStep.ordinal,
                AutoLoopActionStep.id).limit(limit)).all())
    processed = 0
    for owner, identity, state in rows:
        try:
            if state == "failed" and not retry_failed(owner, identity, session_factory=sessions):
                continue
            processed += bool(reconcile_unknown(owner, identity, session_factory=sessions) if state == "unknown"
                              else execute_step(owner, identity, session_factory=sessions))
        except provider.DailyLimitExceeded:
            raise
        except Exception:
            logger.error("AutoLoop step processing failed step=%s", identity)
    return processed
