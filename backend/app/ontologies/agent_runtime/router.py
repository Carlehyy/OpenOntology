"""
本体智能体 API — /api/v2/formal/ontologies/{ontology_id}/agent/*

  GET    /agent/profile            边界配置（不存在则按安全默认创建）
  PUT    /agent/profile            更新边界（仅管理员）
  GET    /agent/capabilities       解析后的能力概览 + 技能卡预览（前端边界可视化）
  POST   /agent/chat               对话（默认 SSE 流式；stream=false 同步返回）
  GET    /agent/chat/runs/{run_id} 回合状态轮询（离开页面后恢复「正在处理」展示）
  GET    /agent/conversations      当前用户在该本体下的会话列表
  GET    /agent/conversations/{id} 会话详情（含完整轨迹，审计视图）
  GET    /agent/conversations/{id}/export 完整会话 JSON（不限制消息条数）
  DELETE /agent/conversations/{id}
  POST   /agent/execute-proposal   用户确认后真实执行提案（经动作引擎，HITL 闸门有效）

路由保留协议签名、RBAC、异常映射与响应封装；应用查询和状态迁移由同目录的
高内聚 service/workflow 模块承担。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user, require_admin
from app.models.ontology import OntologyProject
from app.ontologies.access import require_ontology_access
from app.ontologies.agent_runtime import (
    chat_runs,
    chat_service as _chat_service,
    conversation_service as _conversation_service,
    dynamic_workflow as _dynamic_workflow,
    graph_queries as _graph_queries,
    profile_service as _profile_service,
    proposal_service as _proposal_service,
    report_service as _report_service,
    reporting,
    schemas as S,
)
from app.ontologies.agent_runtime.application_errors import AgentRuntimeApplicationError
from app.ontologies.agent_runtime.boundary import (
    ToolError,
    build_scope,
    get_or_create_profile,
)
from app.ontologies.agent_runtime.graph_service import (
    analyze_change_impact,
    build_workspace_graph,
    find_paths,
    get_instance_detail,
)
from app.ontologies.agent_runtime.models import (
    AgentConversation,
    AgentMessage,
    AgentProfile,
    AnalysisReportRun,
    AnalysisReportTemplate,
)
from app.ontologies.agent_runtime.orchestrator import run_agent_turn
from app.ontologies.release_context import current_release_context
from app.ontologies.sentinels import dynamic_service


router = APIRouter()
logger = logging.getLogger(__name__)

# Compatibility names retained for callers and monkeypatches during migration.
_PROFILE_FIELDS = _profile_service.PROFILE_FIELDS
_RESETTABLE = _profile_service.RESETTABLE_FIELDS
_profile_out = _profile_service.profile_out
_template_out = _report_service.template_out
_run_out = _report_service.run_out
_message_out = _conversation_service.message_out

_APPLICATION_STATUS = {
    "not_found": 404,
    "forbidden": 403,
    "conflict": 409,
    "invalid": 422,
}


def _ok(data):
    return {"data": data}


def _application_call(function, /, *args, **kwargs):
    """Translate transport-neutral workflow failures at the HTTP boundary."""
    try:
        return function(*args, **kwargs)
    except AgentRuntimeApplicationError as error:
        raise HTTPException(
            _APPLICATION_STATUS[error.kind], detail=error.detail
        ) from error


def _require_ontology(db: Session, ontology_id: str) -> OntologyProject:
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id
    ).first()
    if not project:
        raise HTTPException(404, "Ontology not found")
    return project


def _require_report_template(db: Session, ontology_id: str, template_id: str,
                             current_user) -> AnalysisReportTemplate:
    return _application_call(
        _report_service.require_template, db, ontology_id, template_id, current_user
    )


def _require_report_run(db: Session, ontology_id: str, run_id: str,
                        current_user) -> AnalysisReportRun:
    return _application_call(
        _report_service.require_run, db, ontology_id, run_id, current_user
    )


def _require_conversation(db: Session, ontology_id: str, conversation_id: str,
                          current_user) -> AgentConversation:
    return _application_call(
        _conversation_service.require_conversation,
        db, ontology_id, conversation_id, current_user,
    )


# ---------------------------------------------------------------- Profile


@router.get("/{ontology_id}/agent/profile")
def get_profile(ontology_id: str, db: Session = Depends(get_db),
                _=Depends(get_current_user)):
    _require_ontology(db, ontology_id)
    return _ok(_profile_out(get_or_create_profile(db, ontology_id)))


@router.put("/{ontology_id}/agent/profile")
def update_profile(ontology_id: str, body: S.AgentProfileUpdate,
                   db: Session = Depends(get_db), _=Depends(require_admin)):
    _require_ontology(db, ontology_id)
    profile = _profile_service.update_profile(
        db, ontology_id, body, get_profile_fn=get_or_create_profile,
        profile_fields=_PROFILE_FIELDS, resettable_fields=_RESETTABLE,
    )
    return _ok(_profile_out(profile))


@router.get("/{ontology_id}/agent/capabilities")
def get_capabilities(ontology_id: str, release_id: str | None = None,
                     db: Session = Depends(get_db),
                     _=Depends(get_current_user)):
    _require_ontology(db, ontology_id)
    try:
        data = _profile_service.capability_summary(
            db, ontology_id, release_id, build_scope_fn=build_scope
        )
    except ToolError as error:
        raise HTTPException(404, str(error)) from error
    return _ok(data)


# ------------------------------------------------------- Dynamic Sentinels


def _dynamic_context_scope(db: Session, ontology_id: str, release_id: str):
    try:
        return _dynamic_workflow.context_scope(
            db, ontology_id, release_id,
            dynamic_service_module=dynamic_service,
            build_scope_fn=build_scope,
        )
    except ToolError as error:
        raise HTTPException(409, str(error)) from error


@router.get("/{ontology_id}/agent/dynamic-sentinels")
def list_dynamic_sentinels(
    ontology_id: str,
    release_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_ontology_access(db, ontology_id, current_user, write=False)
    context, scope = _dynamic_context_scope(db, ontology_id, release_id)
    return _ok(dynamic_service.list_dynamic(db, context, scope))


@router.post("/{ontology_id}/agent/dynamic-sentinels", status_code=201)
def create_dynamic_sentinel(
    ontology_id: str,
    body: S.DynamicSentinelCreateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_ontology_access(db, ontology_id, current_user, write=True)
    context, scope = _dynamic_context_scope(db, ontology_id, body.release_id)
    return _ok(_dynamic_workflow.create(
        db, context, scope, body,
        str(getattr(current_user, "id", "") or "") or None,
        dynamic_service_module=dynamic_service,
    ))


@router.post("/{ontology_id}/agent/dynamic-sentinels/execute-proposal")
def execute_dynamic_sentinel_proposal(
    ontology_id: str,
    body: S.DynamicSentinelProposalCommand,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_ontology_access(db, ontology_id, current_user, write=True)
    context, scope = _dynamic_context_scope(db, ontology_id, body.release_id)
    return _ok(_dynamic_workflow.execute_proposal(
        db, context, scope, body,
        str(getattr(current_user, "id", "") or "") or None,
        dynamic_service_module=dynamic_service,
    ))


@router.put("/{ontology_id}/agent/dynamic-sentinels/{sentinel_id}")
def update_dynamic_sentinel(
    ontology_id: str,
    sentinel_id: str,
    body: S.DynamicSentinelUpdateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_ontology_access(db, ontology_id, current_user, write=True)
    context, scope = _dynamic_context_scope(db, ontology_id, body.release_id)
    return _ok(_dynamic_workflow.update(
        db, context, scope, sentinel_id, body,
        dynamic_service_module=dynamic_service,
    ))


@router.post("/{ontology_id}/agent/dynamic-sentinels/{sentinel_id}/trial")
def trial_dynamic_sentinel(
    ontology_id: str,
    sentinel_id: str,
    body: S.DynamicSentinelReleaseRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_ontology_access(db, ontology_id, current_user, write=True)
    context, scope = _dynamic_context_scope(db, ontology_id, body.release_id)
    return _ok(_dynamic_workflow.trial(
        db, context, scope, sentinel_id, dynamic_service_module=dynamic_service
    ))


@router.post("/{ontology_id}/agent/dynamic-sentinels/{sentinel_id}/enabled")
def toggle_dynamic_sentinel(
    ontology_id: str,
    sentinel_id: str,
    body: S.DynamicSentinelToggleRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_ontology_access(db, ontology_id, current_user, write=True)
    context, scope = _dynamic_context_scope(db, ontology_id, body.release_id)
    return _ok(_dynamic_workflow.toggle(
        db, context, scope, sentinel_id, body,
        dynamic_service_module=dynamic_service,
    ))


@router.delete("/{ontology_id}/agent/dynamic-sentinels/{sentinel_id}")
def delete_dynamic_sentinel(
    ontology_id: str,
    sentinel_id: str,
    release_id: str = Query(...),
    expected_revision: int = Query(..., ge=1),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_ontology_access(db, ontology_id, current_user, write=True)
    context, _ = _dynamic_context_scope(db, ontology_id, release_id)
    return _ok(_dynamic_workflow.retire(
        db, context, sentinel_id, expected_revision,
        dynamic_service_module=dynamic_service,
    ))


# ------------------------------------------------------------- Graph


@router.get("/{ontology_id}/agent/graph")
def get_agent_graph(
    ontology_id: str,
    depth: int = Query(default=2, ge=1, le=3),
    query: str | None = Query(default=None, max_length=200),
    object_type: str | None = Query(default=None, max_length=200),
    focus_instance_id: str | None = Query(default=None, max_length=200),
    limit_per_type: int = Query(default=20, ge=1, le=50),
    release_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """授权范围内的渐进式数据图谱：L1 类型、L2 实例、L3 聚焦属性。"""
    _require_ontology(db, ontology_id)
    try:
        data = _graph_queries.workspace_graph(
            db, ontology_id, release_id, depth=depth, query=query,
            object_type=object_type, focus_instance_id=focus_instance_id,
            limit_per_type=limit_per_type, build_scope_fn=build_scope,
            graph_fn=build_workspace_graph,
        )
    except ToolError as error:
        raise HTTPException(422, str(error)) from error
    return _ok(data)


@router.get("/{ontology_id}/agent/graph/instances/{instance_id}")
def get_agent_graph_instance(
    ontology_id: str,
    instance_id: str,
    release_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    _require_ontology(db, ontology_id)
    try:
        data = _graph_queries.instance_detail(
            db, ontology_id, release_id, instance_id,
            build_scope_fn=build_scope, detail_fn=get_instance_detail,
        )
    except ToolError as error:
        raise HTTPException(404, str(error)) from error
    return _ok(data)


@router.post("/{ontology_id}/agent/graph/paths")
def query_agent_graph_paths(
    ontology_id: str,
    body: S.GraphPathRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    _require_ontology(db, ontology_id)
    try:
        data = _graph_queries.paths(
            db, ontology_id, body,
            build_scope_fn=build_scope, paths_fn=find_paths,
        )
    except ToolError as error:
        raise HTTPException(422, str(error)) from error
    return _ok(data)


@router.post("/{ontology_id}/agent/graph/impact")
def query_agent_graph_impact(
    ontology_id: str,
    body: S.GraphImpactRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    _require_ontology(db, ontology_id)
    try:
        data = _graph_queries.impact(
            db, ontology_id, body,
            build_scope_fn=build_scope, impact_fn=analyze_change_impact,
        )
    except ToolError as error:
        raise HTTPException(422, str(error)) from error
    return _ok(data)


# ----------------------------------------------------------- Reports


@router.get("/{ontology_id}/agent/report-templates")
def list_report_templates(ontology_id: str, db: Session = Depends(get_db),
                          current_user=Depends(get_current_user)):
    _require_ontology(db, ontology_id)
    return _ok(_report_service.list_templates(db, ontology_id, current_user))


@router.post("/{ontology_id}/agent/report-templates/ai-draft", status_code=201)
def create_ai_report_template(ontology_id: str, body: S.ReportTemplateAIDraftRequest,
                              db: Session = Depends(get_db),
                              current_user=Depends(get_current_user)):
    _require_ontology(db, ontology_id)
    row = _application_call(
        _report_service.create_ai_draft, db, ontology_id, body, current_user,
        require_conversation_fn=_require_conversation, reporting_module=reporting,
        tool_error_type=ToolError,
    )
    return _ok(_template_out(row))


@router.get("/{ontology_id}/agent/report-templates/{template_id}")
def get_report_template(ontology_id: str, template_id: str,
                        db: Session = Depends(get_db),
                        current_user=Depends(get_current_user)):
    row = _require_report_template(db, ontology_id, template_id, current_user)
    return _ok(_template_out(row))


@router.put("/{ontology_id}/agent/report-templates/{template_id}")
def update_report_template(ontology_id: str, template_id: str,
                           body: S.ReportTemplateUpdate,
                           db: Session = Depends(get_db),
                           current_user=Depends(get_current_user)):
    row = _require_report_template(db, ontology_id, template_id, current_user)
    row = _application_call(
        _report_service.update_template, db, row, body, reporting_module=reporting
    )
    return _ok(_template_out(row))


@router.delete("/{ontology_id}/agent/report-templates/{template_id}", status_code=204)
def delete_report_template(ontology_id: str, template_id: str,
                           db: Session = Depends(get_db),
                           current_user=Depends(get_current_user)):
    row = _require_report_template(db, ontology_id, template_id, current_user)
    return _application_call(_report_service.delete_template, db, row)


@router.post("/{ontology_id}/agent/report-templates/{template_id}/preview")
def preview_report_template(ontology_id: str, template_id: str,
                            body: S.ReportRunRequest,
                            db: Session = Depends(get_db),
                            current_user=Depends(get_current_user)):
    row = _require_report_template(db, ontology_id, template_id, current_user)
    run = _application_call(
        _report_service.preview_template, db, row, body, current_user,
        reporting_module=reporting,
    )
    return _ok(_run_out(run))


@router.post("/{ontology_id}/agent/report-templates/{template_id}/publish")
def publish_report_template(ontology_id: str, template_id: str,
                            db: Session = Depends(get_db),
                            current_user=Depends(get_current_user)):
    row = _require_report_template(db, ontology_id, template_id, current_user)
    row = _application_call(
        _report_service.publish_template, db, row,
        now_fn=lambda: datetime.now(timezone.utc),
    )
    return _ok(_template_out(row))


@router.post("/{ontology_id}/agent/report-templates/{template_id}/runs")
def run_published_report(ontology_id: str, template_id: str,
                         body: S.ReportRunRequest,
                         db: Session = Depends(get_db),
                         current_user=Depends(get_current_user)):
    row = _require_report_template(db, ontology_id, template_id, current_user)
    run = _application_call(
        _report_service.run_published, db, row, body, current_user,
        reporting_module=reporting,
    )
    return _ok(_run_out(run))


@router.get("/{ontology_id}/agent/report-templates/{template_id}/runs")
def list_report_runs(ontology_id: str, template_id: str,
                     db: Session = Depends(get_db),
                     current_user=Depends(get_current_user)):
    row = _require_report_template(db, ontology_id, template_id, current_user)
    return _ok(_report_service.list_runs(db, row, current_user))


@router.get("/{ontology_id}/agent/report-runs/{run_id}")
def get_report_run(ontology_id: str, run_id: str,
                   db: Session = Depends(get_db),
                   current_user=Depends(get_current_user)):
    return _ok(_run_out(_require_report_run(
        db, ontology_id, run_id, current_user
    )))


@router.get("/{ontology_id}/agent/report-runs/{run_id}/html", response_class=HTMLResponse)
def get_report_html(ontology_id: str, run_id: str,
                    db: Session = Depends(get_db),
                    current_user=Depends(get_current_user)):
    run = _require_report_run(db, ontology_id, run_id, current_user)
    if run.status != "succeeded" or not run.html_content:
        raise HTTPException(409, "该运行尚无可用 HTML 报告")
    return HTMLResponse(content=run.html_content, headers={
        "Content-Disposition": f'inline; filename="analysis-report-{run.id[:8]}.html"',
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "img-src data:; font-src 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
    })


# ----------------------------------------------------- Conversations


@router.post("/{ontology_id}/agent/chat")
def chat(ontology_id: str, body: S.ChatRequest,
         db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_ontology_access(db, ontology_id, current_user, write=False)
    if not (body.message or "").strip():
        raise HTTPException(422, "message 不能为空")
    if not body.stream:
        return _ok(_chat_service.run_sync(
            db, ontology_id, current_user, body, run_turn_fn=run_agent_turn
        ))
    return StreamingResponse(
        _chat_service.stream_events(
            ontology_id, current_user, body, run_turn_fn=run_agent_turn
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{ontology_id}/agent/chat/cancel")
def cancel_chat(ontology_id: str, body: S.ChatCancelRequest,
                db: Session = Depends(get_db),
                current_user=Depends(get_current_user)):
    """取消本进程内正在流式执行的回合（协作式：步间/模型调用间生效）。"""
    require_ontology_access(db, ontology_id, current_user, write=False)
    from app.ontologies.agent_runtime.chat_cancel import chat_cancel_registry

    if not (body.run_id or "").strip():
        raise HTTPException(422, "runId 不能为空")
    cancelled = chat_cancel_registry.request_cancel(body.run_id)
    return _ok({
        "runId": body.run_id,
        "cancelled": cancelled,
        "note": (
            "已发出取消请求，正在执行的回合将在下一个检查点停止。"
            if cancelled
            else "该回合不在本进程执行中（可能已结束或属于其他实例）。"),
    })


@router.get("/{ontology_id}/agent/chat/runs/{run_id}")
def get_chat_run(ontology_id: str, run_id: str,
                 db: Session = Depends(get_db),
                 current_user=Depends(get_current_user)):
    """查询回合状态（run_id 由前端生成，随 chat 请求注册）。

    SSE 推送与执行解耦后，前端在离开页面后凭 run_id 轮询此端点恢复
    「正在处理」的展示（MYW-71）。
    """
    require_ontology_access(db, ontology_id, current_user, write=False)
    return _ok(chat_runs.run_status_payload(run_id, ontology_id))


@router.get("/{ontology_id}/agent/conversations")
def list_conversations(ontology_id: str, db: Session = Depends(get_db),
                       release_id: str | None = Query(default=None),
                       current_user=Depends(get_current_user)):
    _require_ontology(db, ontology_id)
    return _ok(_conversation_service.list_conversations(
        db, ontology_id, release_id, current_user
    ))


@router.get("/{ontology_id}/agent/conversations/{conversation_id}")
def get_conversation(ontology_id: str, conversation_id: str,
                     db: Session = Depends(get_db),
                     current_user=Depends(get_current_user)):
    conversation = _require_conversation(
        db, ontology_id, conversation_id, current_user
    )
    return _ok(_conversation_service.get_conversation(
        db, conversation, message_out_fn=_message_out
    ))


@router.get("/{ontology_id}/agent/conversations/{conversation_id}/export")
def export_conversation(ontology_id: str, conversation_id: str,
                        db: Session = Depends(get_db),
                        current_user=Depends(get_current_user)):
    """导出会话的完整持久化内容；历史 UI 的 200 条回放上限不适用于此接口。"""
    ontology = _require_ontology(db, ontology_id)
    conversation = _require_conversation(
        db, ontology_id, conversation_id, current_user
    )
    return _ok(_conversation_service.export_conversation(
        db, ontology, conversation,
        now_fn=lambda: datetime.now(timezone.utc),
        message_out_fn=_message_out,
    ))


@router.delete("/{ontology_id}/agent/conversations/{conversation_id}", status_code=204)
def delete_conversation(ontology_id: str, conversation_id: str,
                        db: Session = Depends(get_db),
                        current_user=Depends(get_current_user)):
    conversation = _require_conversation(
        db, ontology_id, conversation_id, current_user
    )
    return _conversation_service.delete_conversation(db, conversation)


@router.post("/{ontology_id}/agent/execute-proposal")
def execute_proposal(ontology_id: str, body: S.ExecuteProposalRequest,
                     db: Session = Depends(get_db),
                     current_user=Depends(get_current_user)):
    """用户在界面确认提案后的真实执行。

    仍然只放行授权边界内的动作；执行走动作引擎全套治理（校验 / HITL 审批 /
    事实追加），actor 记为确认执行的用户 —— agent 只提案，人签字。
    """
    # 提案执行是真实写路径：与 dynamic-sentinels 写路由同口径，必须按本体做写权限裁决。
    require_ontology_access(db, ontology_id, current_user, write=True)
    try:
        release, action = _proposal_service.authorize(
            db, ontology_id, body,
            current_release_fn=current_release_context,
            build_scope_fn=build_scope,
        )
    except ToolError as error:
        raise HTTPException(403, str(error)) from error
    log = _proposal_service.execute(
        db, ontology_id, body, current_user, release, action
    )
    return _ok(log)
