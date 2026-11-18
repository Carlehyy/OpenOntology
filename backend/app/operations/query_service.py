"""运营报告读路径与执行适配。"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.datasets.models import Dataset, DatasetVersion
from app.data_channel.query.contracts import QueryError
from app.data_channel.query.report_sql import check_report_sql, execute_report_queries
from app.operations import markdown
from app.operations import models as m
from app.operations import plan as report_plan
from app.operations.service import (
    _require_article,
    _require_dataset,
    _require_report,
    article_summary,
    plan_field_types,
    report_summary,
)


def _dataset_name(db: Session, dataset_id: str) -> str:
    dataset = db.get(Dataset, dataset_id)
    return dataset.name if dataset is not None else ""


def report_dataset_schema(db: Session, dataset_id: str) -> dict:
    dataset = _require_dataset(db, dataset_id)
    fields = plan_field_types(db, dataset)
    return {
        "dataset_id": dataset.id,
        "dataset_name": dataset.name,
        "columns": [{"name": name, "type": field_type} for name, field_type in fields.items()],
    }


def article_out(db: Session, article: m.OperationsReportArticle, *, include_markdown: bool) -> dict:
    result = article_summary(article, _dataset_name(db, article.dataset_id))
    if include_markdown:
        result.update({
            "markdown": article.markdown,
            "plan_json": article.plan_json,
            "created_at": article.created_at.isoformat() if article.created_at else None,
        })
    return result


def list_reports(db: Session) -> dict:
    from app.operations.service import list_reports as _list
    return _list(db)


def _schema_columns(dataset: Dataset) -> list[dict]:
    typed = [
        {"name": str(item["name"]), "type": str(item.get("type") or "string")}
        for item in ((dataset.schema_json or {}).get("columns_typed") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    if typed:
        return typed
    return [{"name": str(name), "type": "string"}
            for name in (dataset.schema_json or {}).get("columns") or []
            if isinstance(name, str) and name]


def _report_availability(db: Session, dataset: Dataset, version: DatasetVersion | None) -> tuple[bool, str | None]:
    """三类报告资产（integrated/curated/structured+semi）的可用性探测。

    只复用 report_sql 执行路径里的元数据级检查（封存清单、审批门、湖表存
    在性、版本指针、可查询列），绝不物化数据集内容；判定口径与
    ``report_sql._resolve_dataset_source`` 逐分支对齐，避免「列表说可用、
    执行 409」或反向漂移。探测失败只返回原因，不抛错。
    """
    kind = str(dataset.kind or "").strip().lower()
    if version is None:
        return False, "数据集尚无可用数据版本"
    try:
        if kind == "integrated":
            from app.data_channel.datasets.integrated_store import (
                IntegratedVersionError, get_version_relation,
            )
            try:
                relation = get_version_relation(db, dataset.id, version.id)
            except IntegratedVersionError as exc:
                return False, f"集成数据集当前版本不可用：{exc}"
            # 执行器要求封存关系有列映射（report_sql._resolve_dataset_source）。
            if not getattr(relation, "column_map", None):
                return False, "集成数据集当前版本没有可查询的列"
            return True, None
        if kind == "curated":
            from app.data_channel.curated.approved_version_reader import (
                ReviewApprovalError, has_approved_edits, require_version_approved,
            )
            from app.data_channel.datasets import lake_store
            try:
                require_version_approved(db, dataset.id, version)
            except ReviewApprovalError as exc:
                return False, f"成品数据集当前版本不可用：{exc}"
            if (lake_store.version_uses_lake(dataset, version)
                    and not has_approved_edits(db, dataset.id, version)):
                # 与执行器同口径：湖表直查需要非空 lake_columns 映射 + 物理湖表。
                mapping = (dataset.schema_json or {}).get("lake_columns")
                if (not isinstance(mapping, dict) or not mapping
                        or not lake_store.lake_table_exists(db, dataset.id)):
                    return False, "成品数据集尚无物理湖表，无法执行报告查询"
            return True, None
        if kind in {"structured", "semi"}:
            # 执行器以 schema 声明列为期望列；未声明时按首批数据行推导列
            # （report_sql._materialize），因此「有声明列或有数据行」即可执行。
            if not _schema_columns(dataset) and not int(version.rowcount or 0):
                return False, "数据集版本没有可查询的列"
            return True, None
        return False, f"数据集类型 {kind!r} 不支持报告查询"
    except QueryError as exc:
        return False, exc.message
    except Exception as exc:  # noqa: BLE001 - 列表绝不能因单个资产探测失败而 500
        logging.getLogger(__name__).warning(
            "report dataset availability probe failed dataset=%s type=%s",
            dataset.id, type(exc).__name__)
        return False, "数据集可用性探测失败"


def _current_versions(db: Session, datasets: list[Dataset]) -> dict[str, DatasetVersion | None]:
    """批量解析当前版本：指针命中的按主键一次取回；指针为空的走
    resolve_current_version 的 MAX 兜底（仅迁移前历史数据集）。指针失配
    标记为不可用原因，与执行路径的 dataset_pointer_broken 语义一致。"""
    pointed = {row.id: row.latest_version_id for row in datasets if row.latest_version_id}
    by_id: dict[str, DatasetVersion] = {}
    if pointed:
        by_id = {
            version.id: version
            for version in db.query(DatasetVersion).filter(
                DatasetVersion.id.in_(pointed.values()))
        }
    resolved: dict[str, DatasetVersion | None] = {}
    for row in datasets:
        if row.latest_version_id:
            resolved[row.id] = by_id.get(row.latest_version_id)
            continue
        from app.data_channel.datasets.version_pointer import resolve_current_version
        try:
            resolved[row.id] = resolve_current_version(db, row.id)
        except HTTPException:
            resolved[row.id] = None
    return resolved


def list_report_datasets(db: Session) -> dict:
    rows = db.query(Dataset).filter(Dataset.kind != "unstructured").order_by(
        Dataset.updated_at.desc(), Dataset.name.asc(), Dataset.id.asc(),
    ).all()
    versions = _current_versions(db, rows)
    items = []
    for row in rows:
        version = versions.get(row.id)
        if version is not None:
            reportable, reason = _report_availability(db, row, version)
        else:
            reportable, reason = _report_availability(db, row, None)
        items.append({
            # 旧契约字段原样保留（id/name/kind/updated_at）。
            "id": row.id,
            "name": row.name,
            "kind": row.kind,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "reportable": reportable,
            "status": "ready" if reportable else "unavailable",
            "reason": reason,
            "version": {
                "version_no": int(version.version_no),
                "as_of": version.created_at.isoformat() if version.created_at else None,
                "rowcount": int(version.rowcount) if version.rowcount is not None else None,
            } if version is not None else None,
            "schema": {"columns": _schema_columns(row)},
        })
    return {"items": items, "total": len(items)}


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


def preview_plan(
    db: Session,
    report_id: str,
    *,
    dataset_id: str,
    plan: report_plan.ReportPlan,
    params: dict,
    limit: int,
) -> dict:
    """计划试跑（只读、不落库）：报告册作用域 + 数据集门禁 + 计划校验 →
    编译 SQL 走既有 check_report_sql/execute_report_queries 安全门。"""
    _require_report(db, report_id)
    dataset = _require_dataset(db, dataset_id)
    try:
        normalized = report_plan.normalize_plan(
            plan, field_types=plan_field_types(db, dataset))
    except report_plan.PlanValidationError as exc:
        raise QueryError(422, "invalid_report_plan", "计划未通过校验", errors=exc.errors) from exc
    declared = {item["name"] for item in normalized["filters"]}
    undeclared = set(params or {}) - declared
    if undeclared:
        raise QueryError(
            422, "report_plan_params_undeclared",
            "筛选参数必须先在计划 filters 中声明",
            parameters=sorted(undeclared))
    type_errors = report_plan.validate_plan_params(normalized["filters"], params or {})
    if type_errors:
        raise QueryError(
            422, "report_plan_params_invalid", "筛选参数类型与筛选器声明不符",
            errors=type_errors)
    queries = report_plan.compile_plan_queries(normalized, result_limit=limit)
    results: dict[str, dict] = {}
    if queries:
        try:
            results = execute_report_queries(db, dataset.id, queries, params=params, limit=limit)
        except QueryError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).error(
                "report plan preview failed type=%s", type(exc).__name__)
            raise QueryError(503, "report_execution_failed", "报告查询执行失败") from exc

    blocks: list[dict] = []
    version: dict | None = None
    source: str | None = None
    for block in normalized["blocks"]:
        if block["type"] == "query":
            result = results[block["name"]]
            blocks.append({"type": "query", "name": block["name"], "result": result})
            if version is None:
                meta = result.get("dataset") or {}
                version = {
                    "version_id": meta.get("version_id"),
                    "version_no": meta.get("version_no"),
                    "as_of": meta.get("as_of"),
                }
                source = result.get("source")
        else:
            blocks.append(dict(block))
    return {
        "plan_version": normalized["version"],
        "plan": normalized,
        # 服务端确定性生成的正文：前端「高级 Markdown」切换与保存态共用同一
        # 编译结果，防止前端自编译 SQL 与后端漂移。
        "markdown": report_plan.plan_markdown(normalized),
        "dataset": {"id": dataset.id, "name": dataset.name, "kind": dataset.kind},
        "blocks": blocks,
        "version": version,
        "source": source,
    }
