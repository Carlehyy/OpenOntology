"""HTTP boundary for the ontology assistant query view.

Business rules live in query_view_service; this module only maps DTOs and
transport-neutral QueryViewError to HTTP responses.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.ontologies.access import require_ontology_access
from app.ontologies.agent_runtime.query_view_service import (
    QueryViewError, delete_view, handle_query_view,
)
from app.ontologies.formal_modeling.schemas import CamelModel
from app.shared.query_control import request_query_control

router = APIRouter()


class QueryViewRequest(CamelModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    query_spec: Optional[dict] = None
    cursor: Optional[str] = Field(default=None, max_length=24000)
    drilldown: Optional[str] = Field(default=None, max_length=24000)
    page_size: Optional[int] = Field(default=None, ge=1, le=200)


class QueryViewDeleteRequest(CamelModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    cursor: str = Field(min_length=16, max_length=24000)


def _http(exc: QueryViewError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail())


def _handle_query_view(body: QueryViewRequest, ontology_id: str, db: Session,
                       user) -> dict:
    require_ontology_access(db, ontology_id, user, write=False)
    try:
        return handle_query_view(
            db, ontology_id, user,
            conversation_id=body.conversation_id,
            query_spec=body.query_spec, cursor=body.cursor,
            drilldown=body.drilldown, page_size=body.page_size)
    except QueryViewError as exc:
        raise _http(exc) from exc


@router.post("/{ontology_id}/agent/query-view",
             dependencies=[Depends(request_query_control)])
def query_view(ontology_id: str, body: QueryViewRequest,
               db: Session = Depends(get_db),
               current_user=Depends(get_current_user)):
    return {"data": _handle_query_view(body, ontology_id, db, current_user)}


@router.delete("/{ontology_id}/agent/query-view")
def delete_query_view(ontology_id: str, body: QueryViewDeleteRequest,
                      db: Session = Depends(get_db),
                      current_user=Depends(get_current_user)):
    require_ontology_access(db, ontology_id, current_user, write=False)
    try:
        data = delete_view(
            db, ontology_id, current_user,
            conversation_id=body.conversation_id, cursor=body.cursor)
    except QueryViewError as exc:
        raise _http(exc) from exc
    return {"data": data}
