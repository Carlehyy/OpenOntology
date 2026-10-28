"""Read-only POST query route; mount with data_guard, not write guards."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.shared.config import settings
from app.shared.query_control import request_query_control

from .contracts import MemoryCancelRequest, TextQueryCursorRequest, QueryError, TextQueryRequest
from .limits import configured_limits
from .service import QueryService

router = APIRouter()


@router.post("/query", dependencies=[Depends(request_query_control)])
def run_query(body: TextQueryRequest | TextQueryCursorRequest,
              db: Session = Depends(get_db),
              current_user=Depends(get_current_user)):
    service = QueryService(db, current_user, secret=settings.secret_key,
                           limits=configured_limits())
    try:
        data = service.fetch(body) if isinstance(body, TextQueryCursorRequest) else service.execute(body)
    except QueryError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc
    return {"data": data}


@router.post("/query/cancel")
def cancel_query(body: MemoryCancelRequest,
                 db: Session = Depends(get_db),
                 current_user=Depends(get_current_user)):
    service = QueryService(db, current_user, secret=settings.secret_key,
                           limits=configured_limits())
    try:
        data = service.cancel(body.query_id)
    except QueryError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc
    return {"data": data}
