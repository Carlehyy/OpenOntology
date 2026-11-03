"""Analysis-report queries and lifecycle workflows.

This module owns report transaction ordering.  It deliberately has no FastAPI
dependency; the router translates application failures into HTTP responses.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.ontologies.agent_runtime import schemas as S
from app.ontologies.agent_runtime.application_errors import (
    conflict,
    forbidden,
    invalid,
    not_found,
)
from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.models import (
    AgentMessage,
    AnalysisReportRun,
    AnalysisReportTemplate,
)


def template_out(row: AnalysisReportTemplate) -> dict:
    return S.ReportTemplateOut.model_validate(row).model_dump(by_alias=True)


def run_out(
    row: AnalysisReportRun,
    *,
    include_html: bool = True,
) -> dict:
    data = S.ReportRunOut.model_validate(row).model_dump(by_alias=True)
    if not include_html:
        data["htmlContent"] = ""
        data["sectionResults"] = []
    return data


def require_template(
    db: Session,
    ontology_id: str,
    template_id: str,
    current_user: Any,
) -> AnalysisReportTemplate:
    row = db.query(AnalysisReportTemplate).filter(
        AnalysisReportTemplate.id == template_id,
        AnalysisReportTemplate.ontology_id == ontology_id,
    ).first()
    if not row:
        raise not_found("分析报告模板不存在")
    if (
        row.status != "published"
        and row.created_by != getattr(current_user, "id", None)
        and getattr(current_user, "role", "") != "admin"
    ):
        raise forbidden("无权访问该分析报告模板")
    return row


def require_run(
    db: Session,
    ontology_id: str,
    run_id: str,
    current_user: Any,
) -> AnalysisReportRun:
    row = db.query(AnalysisReportRun).filter(
        AnalysisReportRun.id == run_id,
        AnalysisReportRun.ontology_id == ontology_id,
    ).first()
    if not row:
        raise not_found("分析报告运行记录不存在")
    if (
        row.created_by != getattr(current_user, "id", None)
        and getattr(current_user, "role", "") != "admin"
    ):
        raise forbidden("无权访问该分析报告运行记录")
    return row


def list_templates(
    db: Session,
    ontology_id: str,
    current_user: Any,
) -> list[dict]:
    query = db.query(AnalysisReportTemplate).filter(
        AnalysisReportTemplate.ontology_id == ontology_id,
    )
    if getattr(current_user, "role", "") != "admin":
        query = query.filter(
            or_(
                AnalysisReportTemplate.created_by
                == getattr(current_user, "id", None),
                AnalysisReportTemplate.status == "published",
            )
        )
    rows = (
        query.order_by(AnalysisReportTemplate.updated_at.desc())
        .limit(100)
        .all()
    )
    return [template_out(row) for row in rows]


def create_ai_draft(
    db: Session,
    ontology_id: str,
    body: S.ReportTemplateAIDraftRequest,
    current_user: Any,
    *,
    require_conversation_fn: Callable[..., Any],
    reporting_module: Any,
    tool_error_type: type[Exception] = ToolError,
) -> AnalysisReportTemplate:
    brief = (body.brief or "").strip()
    if len(brief) < 8:
        raise invalid("请用至少 8 个字说明报告面向谁、要回答什么问题")

    context = ""
    if body.conversation_id:
        conversation = require_conversation_fn(
            db,
            ontology_id,
            body.conversation_id,
            current_user,
        )
        messages = (
            db.query(AgentMessage)
            .filter(AgentMessage.conversation_id == conversation.id)
            .order_by(AgentMessage.created_at.asc())
            .limit(30)
            .all()
        )
        context = "\n".join(
            (
                f"{'用户' if item.role == 'user' else '助手'}："
                f"{(item.content or '')[:500]}"
            )
            for item in messages
            if (item.content or "").strip()
        )

    try:
        spec = reporting_module.generate_template_spec(
            db,
            ontology_id,
            brief,
            model_id=body.model_id,
            conversation_context=context,
        )
        sections = reporting_module.normalize_sections(spec["sections"])
    except (tool_error_type, ValueError) as exc:
        raise invalid(str(exc)) from exc

    row = AnalysisReportTemplate(
        ontology_id=ontology_id,
        created_by=getattr(current_user, "id", None),
        name=spec["name"],
        description=spec.get("description") or "",
        source_prompt=brief,
        generation_mode=spec.get("generationMode") or "ai",
        sections=sections,
        style=reporting_module.normalize_style(spec.get("style")),
        default_model_id=body.model_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_template(
    db: Session,
    row: AnalysisReportTemplate,
    body: S.ReportTemplateUpdate,
    *,
    reporting_module: Any,
) -> AnalysisReportTemplate:
    if row.status == "published":
        raise conflict("已发布模板不可原地修改；请基于它创建新草稿版本")
    if body.expected_revision != row.revision:
        raise conflict({
            "code": "report_revision_conflict",
            "message": "模板已在其他页面更新，请刷新后再编辑，避免覆盖较新的修改",
            "currentRevision": row.revision,
        })
    try:
        sections = reporting_module.normalize_sections(body.sections)
    except ValueError as exc:
        raise invalid(str(exc)) from exc
    name = (body.name or "").strip()[:240]
    if not name:
        raise invalid("报告名称不能为空")
    next_state = {
        "name": name,
        "description": (body.description or "").strip()[:5000],
        "sections": sections,
        "style": reporting_module.normalize_style(body.style),
        "default_model_id": body.default_model_id,
    }
    current_state = {key: getattr(row, key) for key in next_state}
    if current_state != next_state:
        for key, value in next_state.items():
            setattr(row, key, value)
        row.revision = (row.revision or 0) + 1
        row.last_preview_run_id = None
        row.last_preview_revision = None
        db.commit()
        db.refresh(row)
    return row


def delete_template(
    db: Session,
    row: AnalysisReportTemplate,
) -> None:
    if row.status == "published":
        raise conflict("已发布模板不可删除，请保留运行审计记录")
    db.query(AnalysisReportRun).filter(
        AnalysisReportRun.template_id == row.id,
    ).delete()
    db.delete(row)
    db.commit()


def preview_template(
    db: Session,
    row: AnalysisReportTemplate,
    body: S.ReportRunRequest,
    current_user: Any,
    *,
    reporting_module: Any,
) -> AnalysisReportRun:
    if row.status == "published":
        raise conflict("已发布模板请使用正式运行入口")
    try:
        return reporting_module.execute_report(
            db,
            row,
            current_user,
            "preview",
            body.model_id,
        )
    except ToolError as exc:
        raise invalid(str(exc)) from exc


def publish_template(
    db: Session,
    row: AnalysisReportTemplate,
    *,
    now_fn: Callable[[], Any],
) -> AnalysisReportTemplate:
    if row.status == "published":
        return row
    if (
        not row.last_preview_run_id
        or row.last_preview_revision != row.revision
    ):
        raise conflict({
            "code": "report_preview_required",
            "message": "模板已变化或尚未试运行，请重新查询真实数据并确认结果后再发布",
        })
    run = db.query(AnalysisReportRun).filter(
        AnalysisReportRun.id == row.last_preview_run_id,
        AnalysisReportRun.template_id == row.id,
    ).first()
    if not run or run.status != "succeeded":
        raise conflict("最近一次真实数据试运行未成功")
    quality = run.quality_report or {}
    if not quality.get("passed"):
        raise invalid({
            "code": "report_quality_gate_blocked",
            "message": (
                quality.get("summary")
                or "报告未达到汇报级发布标准"
            ),
            "quality": quality,
        })
    row.status = "published"
    row.published_at = now_fn()
    db.commit()
    db.refresh(row)
    return row


def run_published(
    db: Session,
    row: AnalysisReportTemplate,
    body: S.ReportRunRequest,
    current_user: Any,
    *,
    reporting_module: Any,
) -> AnalysisReportRun:
    if row.status != "published":
        raise conflict("模板尚未发布，只能先执行真实数据试运行")
    try:
        return reporting_module.execute_report(
            db,
            row,
            current_user,
            "manual",
            body.model_id,
        )
    except ToolError as exc:
        raise invalid(str(exc)) from exc


def list_runs(
    db: Session,
    row: AnalysisReportTemplate,
    current_user: Any,
) -> list[dict]:
    query = db.query(AnalysisReportRun).filter(
        AnalysisReportRun.template_id == row.id,
    )
    if getattr(current_user, "role", "") != "admin":
        query = query.filter(
            AnalysisReportRun.created_by
            == getattr(current_user, "id", None),
        )
    runs = (
        query.order_by(AnalysisReportRun.started_at.desc())
        .limit(50)
        .all()
    )
    return [run_out(run, include_html=False) for run in runs]
