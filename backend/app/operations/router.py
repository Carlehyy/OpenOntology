"""运营报告 HTTP 边界；业务逻辑委托 service/query_service。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.operations import query_service, service
from app.operations.schemas import (
    ArticleCreate,
    ArticleExecute,
    ArticleReorder,
    ArticleUpdate,
    ReportCreate,
    ReportUpdate,
)

router = APIRouter()


def _ok(data):
    return {"data": data}


@router.get("")
def list_reports(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return _ok(query_service.list_reports(db))


@router.post("", status_code=201)
def create_report(
    body: ReportCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    report = service.create_report(db, body, user)
    return _ok(query_service.report_summary(report, 0))


@router.get("/datasets")
def list_report_datasets(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return _ok(query_service.list_report_datasets(db))


@router.get("/{report_id}")
def get_report(report_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    return _ok(query_service.report_detail(db, report_id))


@router.patch("/{report_id}")
def update_report(
    report_id: str,
    body: ReportUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    report = service.update_report(db, report_id, body, user)
    count = len(query_service.report_detail(db, report.id)["articles"])
    return _ok(query_service.report_summary(report, count))


@router.delete("/{report_id}")
def delete_report(
    report_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    service.delete_report(db, report_id, user)
    return _ok({"status": "deleted", "id": report_id})


@router.post("/{report_id}/articles", status_code=201)
def create_article(
    report_id: str,
    body: ArticleCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return _ok(query_service.article_detail(
        db, report_id, service.create_article(db, report_id, body, user).id))


@router.get("/{report_id}/articles/{article_id}")
def get_article(
    report_id: str,
    article_id: str,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return _ok(query_service.article_detail(db, report_id, article_id))


@router.patch("/{report_id}/articles/{article_id}")
def update_article(
    report_id: str,
    article_id: str,
    body: ArticleUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    article = service.update_article(db, report_id, article_id, body, user)
    return _ok(query_service.article_detail(db, report_id, article.id))


@router.delete("/{report_id}/articles/{article_id}")
def delete_article(
    report_id: str,
    article_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    service.delete_article(db, report_id, article_id, user)
    return _ok({"status": "deleted", "id": article_id})


@router.post("/{report_id}/articles/reorder")
def reorder_articles(
    report_id: str,
    body: ArticleReorder,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    rows = service.reorder_articles(db, report_id, body, user)
    return _ok({"items": [query_service.article_out(db, row, include_markdown=False) for row in rows]})


@router.get("/{report_id}/articles/{article_id}/schema")
def get_article_schema(
    report_id: str,
    article_id: str,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return _ok(query_service.article_schema(db, report_id, article_id))


@router.post("/{report_id}/articles/{article_id}/execute")
def execute_article(
    report_id: str,
    article_id: str,
    body: ArticleExecute,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    from app.data_channel.query.contracts import QueryError
    try:
        result = query_service.execute_article(
            db, report_id, article_id,
            sql=body.sql,
            queries=body.queries,
            draft_markdown=body.markdown,
            params=body.params,
            limit=body.limit,
        )
    except QueryError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc
    return _ok(result)
