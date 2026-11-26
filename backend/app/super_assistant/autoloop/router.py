"""JWT AutoLoop endpoints; mutations commit before workers may send."""
from __future__ import annotations

import logging
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.super_assistant import multica_service, provider

from . import planner, routing_policy, schemas, service, sourcing, store, templates, trace_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/autoloop")

# Outer bound of a routing-policy request body, enforced before the JSON is
# buffered. It is looser than the field limits on purpose: a client may
# pretty-print or escape its JSON.
POLICY_BODY_MAX_BYTES = 262144

# Planner refusals of a preview, by HTTP status. Anything else is reported as
# invalid model output so unknown text is never echoed to the client.
_PREVIEW_REASONS = {
    "explicit_model_unavailable": 503, "model_unavailable": 503,
    "invalid_model_output": 502, "invalid_readonly_action": 502, "workspace_mismatch": 502,
}


class BoundedBodyRoute(APIRoute):
    """Refuse an oversized body before FastAPI reads it into memory.

    Scoped to the routing-policy routes only; no other endpoint changes. The
    Content-Length header is checked first and the body is then streamed under
    the same cap, so a chunked body or a false header cannot bypass it.
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            raw = request.headers.get("content-length")
            if raw is not None:
                try:
                    length = int(raw)
                except ValueError:
                    raise HTTPException(status_code=400,
                                        detail={"code": "invalid_content_length"}) from None
                if length < 0 or length > POLICY_BODY_MAX_BYTES:
                    raise HTTPException(status_code=413, detail={"code": "payload_too_large"})
            chunks, seen = [], 0
            async for chunk in request.stream():
                seen += len(chunk)
                if seen > POLICY_BODY_MAX_BYTES:
                    raise HTTPException(status_code=413, detail={"code": "payload_too_large"})
                chunks.append(chunk)
            # Starlette's Request.body() returns this cache instead of reading
            # the consumed stream again.
            request._body = b"".join(chunks)
            return await original(request)

        return bounded


@contextmanager
def _errors(db):
    try:
        yield
    except store.StoreConflict as exc:
        db.rollback()
        raise HTTPException(status_code=exc.http_status,
            detail={"code": exc.code, "current": exc.current}) from exc
    except multica_service.MulticaServiceError as exc:
        db.rollback()
        # The card's immutable source is unusable; the answer cannot be bound
        # to a different source, so the submission is refused, not retargeted.
        raise HTTPException(status_code=409,
            detail={"code": "decision_source_unavailable"}) from exc
    except templates.RenderError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    except ValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail={"code": "invalid_rule"}) from exc


@contextmanager
def _preview_failures():
    """Answer a failed assessment with a named reason and no provider text.

    A preview spends real provider quota but never sets the process-wide daily
    limit stop (see routing_policy.preview); once that stop is in force, the
    preview is refused before any call, like every other AutoLoop entry.
    """
    try:
        yield
    except provider.DailyLimitExceeded as exc:
        logger.warning("AutoLoop routing preview hit the provider daily limit")
        raise HTTPException(status_code=429, detail={
            "code": "routing_preview_failed", "reason": "daily_limit_reached"}) from exc
    except planner.PlanningError as exc:
        reason = str(exc) if str(exc) in _PREVIEW_REASONS else "invalid_model_output"
        raise HTTPException(status_code=_PREVIEW_REASONS[reason], detail={
            "code": "routing_preview_failed", "reason": reason}) from exc
    except provider.ProviderError as exc:
        logger.warning("AutoLoop routing preview model call failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail={
            "code": "routing_preview_failed", "reason": "model_call_failed"}) from exc
    except ValueError as exc:
        # An unusable model configuration, such as an API key that no longer
        # decrypts. Its text may name endpoints, so only the class is logged.
        logger.warning("AutoLoop routing preview model unusable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail={
            "code": "routing_preview_failed", "reason": "model_unavailable"}) from exc


@router.get("/rules", response_model=list[schemas.RuleOut])
def list_rules(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return service.rules(db, current_user.id)


@router.post("/rules", response_model=schemas.RuleOut, status_code=201)
def create_rule(body: schemas.RuleCreateRequest, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    with _errors(db):
        result = service.create_rule(db, current_user.id, body,
                                     confirm_all_match=body.confirmAllMatch)
        db.commit()
        return result


@router.post("/rules/preview", response_model=schemas.PreviewOut)
def preview_draft(body: schemas.DraftPreview, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    with _errors(db):
        return service.preview(body.rule, body.sampleEvent)


@router.get("/rules/{identity}", response_model=schemas.RuleOut)
def get_rule(identity: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    with _errors(db):
        return service.get_rule(db, current_user.id, identity)


@router.patch("/rules/{identity}", response_model=schemas.RuleOut)
def update_rule(identity: str, body: schemas.RulePatchRequest, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    with _errors(db):
        result = service.patch_rule(db, current_user.id, identity, body,
            expected_version=body.expectedVersion, confirm_all_match=body.confirmAllMatch)
        db.commit()
        return result


@router.delete("/rules/{identity}", status_code=204)
def delete_rule(identity: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    with _errors(db):
        service.delete_rule(db, current_user.id, identity)
        db.commit()
        return Response(status_code=204)


@router.post("/rules/{identity}/preview", response_model=schemas.PreviewOut)
def preview_stored(identity: str, body: schemas.StoredPreview, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    with _errors(db):
        return service.preview_stored(db, current_user.id, identity, body.sampleEvent)


@router.get("/events", response_model=schemas.Page[schemas.EventOut])
def list_events(source: str | None = None, sourceSystem: str | None = None,
                action: str | None = None, ruleId: str | None = None, status: str | None = None,
                scheduledTaskId: str | None = None, scheduledRunId: str | None = None,
                cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100),
                db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    with _errors(db):
        return service.list_events(db, current_user.id, cursor=cursor, limit=limit,
            source=source, sourceSystem=sourceSystem, action=action, ruleId=ruleId,
            status=status, scheduledTaskId=scheduledTaskId, scheduledRunId=scheduledRunId)


@router.get("/events/{identity}", response_model=schemas.EventDetailOut)
def get_event(identity: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    with _errors(db):
        return service.get_event(db, current_user.id, identity)


@router.get("/event-traces", response_model=schemas.Page[schemas.EventTraceOut])
def list_event_traces(source: str | None = None, sourceSystem: str | None = None,
                      route: str | None = None, action: str | None = None,
                      outcome: str | None = None, ruleId: str | None = None,
                      since: str | None = None, until: str | None = None,
                      scheduledTaskId: str | None = None, scheduledRunId: str | None = None,
                      cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100),
                      db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    with _errors(db):
        return trace_service.list_traces(db, current_user.id, cursor=cursor, limit=limit,
            source=source, sourceSystem=sourceSystem, route=route, action=action,
            outcome=outcome, ruleId=ruleId, since=since, until=until,
            scheduledTaskId=scheduledTaskId, scheduledRunId=scheduledRunId)


@router.get("/event-traces/{eventId}", response_model=schemas.EventTraceDetailOut | None)
def get_event_trace(eventId: str, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    # A legal event without an inbound receipt answers null; the raw event
    # detail stays available on GET /events/{identity}.
    with _errors(db):
        return trace_service.get_trace(db, current_user.id, eventId)


@router.get("/routing-stats", response_model=schemas.RoutingStatsOut)
def routing_stats(days: int = Query(default=7),
                  db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    # Only whole-day 7/30 windows are offered; anything else answers 422 so
    # clients cannot silently compare mismatched aggregation horizons.
    with _errors(db):
        return trace_service.routing_stats(db, current_user.id, days=days)


@router.get("/decision-items", response_model=schemas.Page[schemas.DecisionOut])
def list_decisions(status: str | None = None, cursor: str | None = None,
                   limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    with _errors(db):
        return service.list_decisions(db, current_user.id, status=status, cursor=cursor, limit=limit)


@router.get("/decision-items/summary", response_model=schemas.DecisionSummary)
def summary(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return service.summary(db, current_user.id)


@router.get("/decision-items/{identity}", response_model=schemas.DecisionOut)
def get_decision(identity: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    with _errors(db):
        return service.get_decision(db, current_user.id, identity)


@router.post("/decision-items/{identity}/decide", response_model=schemas.DecisionOut, status_code=202)
def decide(identity: str, body: schemas.Decide, response: Response, db: Session = Depends(get_db),
           current_user: User = Depends(get_current_user)):
    with _errors(db):
        def resolve_source(session, row):
            # The card's frozen source (or its workspace's unique source for
            # legacy cards) is resolved inside submit_decision only after the
            # idempotent replay check, so a replay stays successful even when
            # the source has since become unavailable. Profile reads are pure:
            # materialize deployment-default drift in the same caller-owned
            # write first, so unlinked cards keep finding the default source.
            multica_service.sync_deployment_default(session, current_user.id)
            return sourcing.resolve_decision_source(session, current_user.id, row)

        row, request, replay = store.submit_decision(db, owner_id=current_user.id, decision_id=identity,
            actor_id=current_user.id, expected_content_version=body.expectedContentVersion,
            idempotency_key=body.idempotencyKey, option_id=body.optionId, free_text=body.freeText,
            source_resolver=resolve_source)
        result = service.decision_result(db, row, request, replay=replay)
        db.commit()
        response.status_code = 200 if replay else 202
        return result


@router.post("/decision-items/{identity}/mark-handled", response_model=schemas.DecisionOut)
def mark_handled(identity: str, body: schemas.MarkHandled, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    with _errors(db):
        row, request, replay = store.mark_handled(db, owner_id=current_user.id, decision_id=identity,
            actor_id=current_user.id, expected_content_version=body.expectedContentVersion,
            idempotency_key=body.idempotencyKey, reason=body.reason)
        result = service.decision_result(db, row, request, replay=replay)
        db.commit()
        return result


# The routing-policy routes carry their own body bound; the endpoints above
# keep the platform's default request handling.
policy_router = APIRouter(prefix="/routing-policy", route_class=BoundedBodyRoute)


@policy_router.get("", response_model=schemas.RoutingPolicyOut)
def get_routing_policy(db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    with _errors(db):
        return routing_policy.state(db, current_user.id)


@policy_router.patch("", response_model=schemas.RoutingPolicyOut)
def update_routing_policy(body: schemas.RoutingPolicyPatch, db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    with _errors(db):
        result = routing_policy.update(db, current_user.id, current_user.id, body)
        db.commit()
        return result


@policy_router.post("/preview", response_model=schemas.RoutingPolicyPreviewOut)
def preview_routing_policy(body: schemas.RoutingPolicyPreview, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    # Read-only: nothing is committed, so a preview can never change the
    # policy, an event, a decision or an action.
    with _errors(db), _preview_failures():
        return routing_policy.preview(db, current_user.id, body)


router.include_router(policy_router)
