"""JWT AutoLoop endpoints; mutations commit before workers may send."""
from __future__ import annotations

from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db

from . import schemas, service, store, templates

router = APIRouter(prefix="/autoloop")


@contextmanager
def _errors(db):
    try:
        yield
    except store.StoreConflict as exc:
        db.rollback()
        raise HTTPException(status_code=exc.http_status,
            detail={"code": exc.code, "current": exc.current}) from exc
    except templates.RenderError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    except ValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail={"code": "invalid_rule"}) from exc


@router.get("/rules", response_model=list[schemas.RuleOut])
def list_rules(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return service.rules(db, current_user.id)


@router.post("/rules", response_model=schemas.RuleOut, status_code=201)
def create_rule(body: schemas.RuleCreate, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    with _errors(db):
        result = service.create_rule(db, current_user.id, body)
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
def update_rule(identity: str, body: schemas.RulePatch, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    with _errors(db):
        result = service.patch_rule(db, current_user.id, identity, body)
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
        from app.super_assistant import multica_service
        config = multica_service.active_config(db, current_user.id)
        row, request, replay = store.submit_decision(db, owner_id=current_user.id, decision_id=identity,
            actor_id=current_user.id, expected_content_version=body.expectedContentVersion,
            idempotency_key=body.idempotencyKey, option_id=body.optionId, free_text=body.freeText,
            connector_base_url=config.base_url if config else "")
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
