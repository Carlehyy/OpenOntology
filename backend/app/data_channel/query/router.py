"""Read-only POST query route; mount with data_guard, not write guards."""
from __future__ import annotations

from dataclasses import fields

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.shared.config import settings

from .contracts import TextQueryCursorRequest, QueryError, QueryLimits, TextQueryRequest
from .service import QueryService

router = APIRouter()


def configured_limits() -> QueryLimits:
    return QueryLimits(**{f.name: getattr(settings, "unified_query_" + f.name)
                          for f in fields(QueryLimits)})


@router.post("/query")
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
