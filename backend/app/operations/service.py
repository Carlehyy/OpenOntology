"""运营报告册写路径与排序。"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.data_channel.datasets.models import Dataset
from app.data_channel.query.report_sql import check_report_sql
from app.operations import markdown
from app.operations import models as m
from app.operations import plan as report_plan
from app.operations.schemas import ArticleCreate, ArticleReorder, ArticleUpdate, ReportCreate, ReportUpdate


def _write_required(user) -> None:
    if str(getattr(user, "role", "viewer") or "viewer") not in {"admin", "editor"}:
        raise HTTPException(403, detail={"code": "REPORT_WRITE_DENIED", "message": "当前角色只能阅读运营报告"})


def _require_report(db: Session, report_id: str, *, for_update: bool = False) -> m.OperationsReport:
    query = db.query(m.OperationsReport).filter(m.OperationsReport.id == report_id)
    if for_update:
        query = query.with_for_update().populate_existing()
    report = query.one_or_none()
    if report is None:
        raise HTTPException(404, detail={"code": "report_not_found", "message": "报告册不存在"})
    return report


def _require_article(db: Session, report_id: str, article_id: str) -> m.OperationsReportArticle:
    article = db.query(m.OperationsReportArticle).filter(
        m.OperationsReportArticle.id == article_id,
        m.OperationsReportArticle.report_id == report_id,
    ).one_or_none()
    if article is None:
        raise HTTPException(404, detail={"code": "article_not_found", "message": "文章不存在"})
    return article


def _require_dataset(db: Session, dataset_id: str) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(404, detail={"code": "dataset_not_found", "message": "数据集不存在"})
    if str(dataset.kind or "").lower() == "unstructured":
        raise HTTPException(409, detail={"code": "report_dataset_kind_unsupported", "message": "运营报告只支持结构化数据集"})
    return dataset


def plan_field_types(db: Session, dataset: Dataset) -> dict[str, str]:
    """计划字段白名单与资产湖字段读取共用同一确定性推导。"""
    declared = report_plan.dataset_field_types(dataset)
    if declared:
        return declared
    from app.data_channel.datasets.query_service import get_schema
    return report_plan.field_types_from_columns(get_schema(dataset.id, db)["columns"])


def _validate_markdown(value: str) -> None:
    result = markdown.validate_markdown(value)
    errors = list(result.errors)
    filter_names = {
        (block.config or {}).get("name", "").strip()
        for block in result.blocks if block.kind == "filter"
    }
    for block in result.blocks:
        if block.kind != "sql":
            continue
        check = check_report_sql(block.body)
        if not check.ok:
            errors.extend(f"查询 {block.name or '（未命名）'}：{error}" for error in check.errors)
        undeclared = set(check.parameters) - filter_names
        if undeclared:
            errors.append(f"查询 {block.name or '（未命名）'} 的参数未声明筛选器：{', '.join(sorted(undeclared))}")
    if errors:
        raise HTTPException(422, detail={"code": "invalid_report_markdown", "message": "；".join(errors), "errors": errors})


def _plan_body(db: Session, dataset: Dataset, plan: report_plan.ReportPlan) -> tuple[dict, str]:
    """结构化计划 →（规范化 plan_json，生成的 markdown）。

    生成的 markdown 仍过 _validate_markdown（含每条 SQL 的 check_report_sql），
    保证计划产物与手写 markdown 走完全相同的保存期安全门。
    """
    try:
        normalized = report_plan.normalize_plan(
            plan, field_types=plan_field_types(db, dataset))
    except report_plan.PlanValidationError as exc:
        raise HTTPException(422, detail={
            "code": "invalid_report_plan",
            "message": "；".join(exc.errors),
            "errors": exc.errors,
        }) from exc
    generated = report_plan.plan_markdown(normalized)
    _validate_markdown(generated)
    return normalized, generated


def _bump_report(report: m.OperationsReport) -> None:
    report.revision = int(report.revision or 0) + 1


def _next_sort_order(max_order: int | None) -> int:
    # 首条记录 max() 返回 None 得 0；不能用 `max_order or -1`，否则
    # 已有 sort_order=0 时 0 为假值，第二条会重复拿到 0。
    return 0 if max_order is None else int(max_order) + 1


def list_reports(db: Session) -> dict:
    reports = db.query(m.OperationsReport).order_by(
        m.OperationsReport.sort_order.asc(),
        m.OperationsReport.updated_at.desc(),
        m.OperationsReport.id.asc(),
    ).all()
    article_counts = dict(db.query(
        m.OperationsReportArticle.report_id,
        func.count(m.OperationsReportArticle.id),
    ).group_by(m.OperationsReportArticle.report_id).all())
    return {"items": [report_summary(row, article_counts.get(row.id, 0)) for row in reports], "total": len(reports)}


def create_report(db: Session, body: ReportCreate, user) -> m.OperationsReport:
    _write_required(user)
    name = body.name.strip()
    if not name:
        raise HTTPException(422, detail={"code": "report_name_required", "message": "报告册名称不能为空"})
    max_order = db.query(func.max(m.OperationsReport.sort_order)).scalar()
    report = m.OperationsReport(name=name, description=body.description.strip(), sort_order=_next_sort_order(max_order))
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


def update_report(db: Session, report_id: str, body: ReportUpdate, user) -> m.OperationsReport:
    _write_required(user)
    report = _require_report(db, report_id, for_update=True)
    if body.expected_revision is not None and body.expected_revision != report.revision:
        raise HTTPException(409, detail={"code": "report_revision_conflict", "message": "报告册已被修改，请刷新后重试", "revision": report.revision})
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, detail={"code": "report_name_required", "message": "报告册名称不能为空"})
        report.name = name
    if body.description is not None:
        report.description = body.description.strip()
    _bump_report(report)
    db.commit()
    db.refresh(report)
    return report


def delete_report(db: Session, report_id: str, user) -> None:
    _write_required(user)
    report = _require_report(db, report_id, for_update=True)
    db.delete(report)
    db.commit()


def create_article(db: Session, report_id: str, body: ArticleCreate, user) -> m.OperationsReportArticle:
    _write_required(user)
    report = _require_report(db, report_id, for_update=True)
    title = body.title.strip()
    if not title:
        raise HTTPException(422, detail={"code": "article_title_required", "message": "文章标题不能为空"})
    dataset = _require_dataset(db, body.dataset_id)
    if body.plan is not None:
        plan_json, markdown_text = _plan_body(db, dataset, body.plan)
    else:
        plan_json, markdown_text = None, body.markdown
        _validate_markdown(markdown_text)
    max_order = db.query(func.max(m.OperationsReportArticle.sort_order)).filter(
        m.OperationsReportArticle.report_id == report_id).scalar()
    article = m.OperationsReportArticle(
        report_id=report.id,
        title=title,
        markdown=markdown_text,
        plan_json=plan_json,
        dataset_id=dataset.id,
        sort_order=_next_sort_order(max_order),
    )
    db.add(article)
    _bump_report(report)
    db.commit()
    db.refresh(article)
    return article


def update_article(db: Session, report_id: str, article_id: str, body: ArticleUpdate, user) -> m.OperationsReportArticle:
    _write_required(user)
    report = _require_report(db, report_id, for_update=True)
    article = _require_article(db, report_id, article_id)
    if body.expected_revision is not None and body.expected_revision != article.revision:
        raise HTTPException(409, detail={"code": "article_revision_conflict", "message": "文章已被修改，请刷新后重试", "revision": article.revision})
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(422, detail={"code": "article_title_required", "message": "文章标题不能为空"})
        article.title = title
    new_dataset = _require_dataset(db, body.dataset_id) if body.dataset_id is not None else None
    if body.plan is not None:
        dataset = new_dataset if new_dataset is not None else _require_dataset(db, article.dataset_id)
        plan_json, markdown_text = _plan_body(db, dataset, body.plan)
        article.plan_json = plan_json
        article.markdown = markdown_text
    elif body.markdown is not None:
        # 手写 markdown 修改后与计划脱钩：plan_json 清空，避免双权威漂移。
        _validate_markdown(body.markdown)
        article.markdown = body.markdown
        article.plan_json = None
    elif new_dataset is not None and article.plan_json:
        # 只换数据集但文章仍是计划形态：字段白名单必须对新数据集继续成立。
        _plan_body(db, new_dataset, report_plan.ReportPlan.model_validate(article.plan_json))
    if new_dataset is not None:
        article.dataset_id = new_dataset.id
    article.revision = int(article.revision or 0) + 1
    _bump_report(report)
    db.commit()
    db.refresh(article)
    return article


def delete_article(db: Session, report_id: str, article_id: str, user) -> None:
    _write_required(user)
    report = _require_report(db, report_id, for_update=True)
    article = _require_article(db, report_id, article_id)
    db.delete(article)
    db.flush()
    _normalize_article_order(db, report_id)
    _bump_report(report)
    db.commit()


def reorder_articles(db: Session, report_id: str, body: ArticleReorder, user) -> list[m.OperationsReportArticle]:
    _write_required(user)
    report = _require_report(db, report_id, for_update=True)
    current = db.query(m.OperationsReportArticle).filter(
        m.OperationsReportArticle.report_id == report_id,
    ).order_by(m.OperationsReportArticle.sort_order.asc()).all()
    current_ids = {row.id for row in current}
    requested = list(body.article_ids)
    if set(requested) != current_ids or len(requested) != len(current):
        raise HTTPException(422, detail={"code": "invalid_article_order", "message": "排序列表必须包含本报告的全部文章且不能重复"})
    # 先移到负数区间，避免唯一排序约束在交换时瞬间冲突。
    for index, row in enumerate(current):
        row.sort_order = -(index + 1)
    db.flush()
    by_id = {row.id: row for row in current}
    for index, article_id in enumerate(requested):
        by_id[article_id].sort_order = index
    _bump_report(report)
    db.commit()
    return sorted(current, key=lambda row: row.sort_order)


def _normalize_article_order(db: Session, report_id: str) -> None:
    rows = db.query(m.OperationsReportArticle).filter(
        m.OperationsReportArticle.report_id == report_id,
    ).order_by(m.OperationsReportArticle.sort_order.asc(), m.OperationsReportArticle.id.asc()).all()
    for index, row in enumerate(rows):
        row.sort_order = -(index + 1)
    db.flush()
    for index, row in enumerate(rows):
        row.sort_order = index


def report_summary(report: m.OperationsReport, article_count: int = 0) -> dict:
    return {
        "id": report.id, "name": report.name, "description": report.description or "",
        "article_count": int(article_count), "revision": int(report.revision or 1),
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "updated_at": report.updated_at.isoformat() if report.updated_at else None,
    }


def article_summary(article: m.OperationsReportArticle, dataset_name: str = "") -> dict:
    return {
        "id": article.id, "report_id": article.report_id, "title": article.title,
        "dataset_id": article.dataset_id, "dataset_name": dataset_name,
        "position": int(article.sort_order), "revision": int(article.revision or 1),
        "plan_json": article.plan_json,
        "updated_at": article.updated_at.isoformat() if article.updated_at else None,
    }
