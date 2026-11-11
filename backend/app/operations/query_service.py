"""运营报告读路径与执行适配。"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.datasets.models import Dataset
from app.data_channel.query.contracts import QueryError
from app.data_channel.query.report_sql import check_report_sql, execute_report_queries
from app.operations import markdown
from app.operations import models as m
from app.operations.service import (
    _require_article,
    _require_report,
    article_summary,
    report_summary,
)


def _dataset_name(db: Session, dataset_id: str) -> str:
    dataset = db.get(Dataset, dataset_id)
    return dataset.name if dataset is not None else ""


def article_out(db: Session, article: m.OperationsReportArticle, *, include_markdown: bool) -> dict:
    result = article_summary(article, _dataset_name(db, article.dataset_id))
    if include_markdown:
        result.update({
            "markdown": article.markdown,
            "created_at": article.created_at.isoformat() if article.created_at else None,
        })
    return result


def list_reports(db: Session) -> dict:
    from app.operations.service import list_reports as _list
    return _list(db)


def list_report_datasets(db: Session) -> dict:
    rows = db.query(Dataset).filter(Dataset.kind != "unstructured").order_by(
        Dataset.updated_at.desc(), Dataset.name.asc(), Dataset.id.asc(),
    ).all()
    return {
        "items": [{
            "id": row.id,
            "name": row.name,
            "kind": row.kind,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        } for row in rows],
        "total": len(rows),
    }


def report_detail(db: Session, report_id: str) -> dict:
    report = _require_report(db, report_id)
    articles = db.query(m.OperationsReportArticle).filter(
        m.OperationsReportArticle.report_id == report.id,
    ).order_by(
        m.OperationsReportArticle.sort_order.asc(),
        m.OperationsReportArticle.id.asc(),
    ).all()
    result = report_summary(report, len(articles))
    result["articles"] = [article_out(db, article, include_markdown=False) for article in articles]
    return result


def article_detail(db: Session, report_id: str, article_id: str) -> dict:
    return article_out(db, _require_article(db, report_id, article_id), include_markdown=True)


def article_schema(db: Session, report_id: str, article_id: str) -> dict:
    article = _require_article(db, report_id, article_id)
    dataset = db.get(Dataset, article.dataset_id)
    if dataset is None:
        raise HTTPException(409, detail={"code": "report_dataset_not_found", "message": "文章绑定的数据集不存在"})
    from app.data_channel.datasets.query_service import get_schema

    schema = get_schema(dataset.id, db)
    columns = [{"name": column["name"], "type": column["type"]}
               for column in schema["columns"]]
    return {"dataset_id": dataset.id, "dataset_name": dataset.name, "columns": columns}


def validate_article_markdown(db: Session, report_id: str, article_id: str) -> dict:
    article = _require_article(db, report_id, article_id)
    result = markdown.validate_markdown(article.markdown)
    return {"ok": result.ok, "errors": list(result.errors)}


def execute_article(
    db: Session,
    report_id: str,
    article_id: str,
    *,
    sql: str | None = None,
    queries: dict[str, str] | None = None,
    draft_markdown: str | None = None,
    params: dict,
    limit: int,
) -> dict:
    article = _require_article(db, report_id, article_id)
    parsed = markdown.validate_markdown(article.markdown if draft_markdown is None else draft_markdown)
    if not parsed.ok:
        raise QueryError(422, "report_markdown_invalid", "文章配置无效", errors=list(parsed.errors))
    selected = queries if queries is not None else {"query": sql}
    filter_names = {
        (block.config or {}).get("name", "").strip()
        for block in parsed.blocks if block.kind == "filter"
    }
    for query_sql in selected.values():
        check = check_report_sql(query_sql)
        if check.ok and set(check.parameters) - filter_names:
            raise QueryError(
                422,
                "report_sql_filter_undeclared",
                "SQL 参数必须先在文章 filter 围栏中声明",
                parameters=sorted(set(check.parameters) - filter_names),
            )
    try:
        results = execute_report_queries(db, article.dataset_id, selected, params=params, limit=limit)
    except QueryError:
        raise
    except Exception as exc:
        # 记录异常类型便于诊断，不把 SQL、参数或驱动错误文本写入响应/日志。
        logging.getLogger(__name__).error("report execution failed type=%s", type(exc).__name__)
        raise QueryError(503, "report_execution_failed", "报告查询执行失败") from exc
    return {"results": results} if queries is not None else results["query"]
