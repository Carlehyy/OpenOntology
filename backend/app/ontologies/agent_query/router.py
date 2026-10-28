"""统一关联查询 HTTP 入口 — 同本体多对象与跨本体同一执行核。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.shared.query_control import request_query_control
from app.ontologies.agent_query import schemas as QS
from app.ontologies.agent_query.service import (
    QueryError, execute_unified_query, fetch_unified_query_page,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _query_error(exc: QueryError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail())


@router.post("/agent/query", dependencies=[Depends(request_query_control)])
def run_query(body: QS.QueryRequest | QS.CursorRequest,
              db: Session = Depends(get_db),
              current_user=Depends(get_current_user)):
    """统一关联查询：远程结果仅在内存存活；翻页只传 cursor。"""
    if isinstance(body, QS.CursorRequest):
        try:
            data = fetch_unified_query_page(
                db, current_user, body.cursor,
                session_kind="direct", session_id=str(current_user.id))
        except QueryError as exc:
            raise _query_error(exc) from exc
        return {"data": data}
    try:
        data = execute_unified_query(
            db, current_user, body,
            session_kind="direct", session_id=str(current_user.id))
    except QueryError as exc:
        raise _query_error(exc) from exc
    return {"data": data}
