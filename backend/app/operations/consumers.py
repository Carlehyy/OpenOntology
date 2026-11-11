"""数据集被运营报告文章引用的消费者查询——删除数据集前的依赖检查。

文章（OperationsReportArticle.dataset_id）以 RESTRICT 外键持有数据集：
被引用的数据集直接删除会撞外键变成 500，删除守卫必须在删除前显式预检。
"""
from __future__ import annotations

from sqlalchemy.orm import Session


def dataset_report_article_references(db: Session, dataset_id: str) -> list[dict]:
    """返回引用该数据集的运营报告文章（report 外连接，报告行缺失不炸查询）。空 = 无引用。"""
    from app.operations.models import OperationsReport, OperationsReportArticle

    rows = (
        db.query(OperationsReportArticle, OperationsReport)
        .outerjoin(OperationsReport, OperationsReportArticle.report_id == OperationsReport.id)
        .filter(OperationsReportArticle.dataset_id == dataset_id)
        .all()
    )
    return [
        {
            "article_id": article.id,
            "title": article.title,
            "report_id": article.report_id,
            "report_name": report.name if report is not None else "",
        }
        for article, report in rows
    ]
