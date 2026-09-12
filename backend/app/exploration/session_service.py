"""Session lifecycle and read-side application services for Exploration."""
from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.exploration import canvas as C
from app.exploration import converter
from app.exploration import readiness as R
from app.exploration import schemas as S
from app.exploration.document import canvas_fingerprint
from app.exploration.diagram import (
    DIAGRAM_KINDS,
    DiagramError,
    build_diagram,
)
from app.exploration.models import (
    ExplorationAttachment,
    ExplorationDocument,
    ExplorationDraft,
    ExplorationMessage,
    ExplorationSession,
)
from app.exploration.reverse_projection import project_snapshot_to_canvas
from app.ontologies.access import require_ontology_access
from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions.models import OntologyVersion


def _ok(data):
    return {"data": data}


def _require_session(
    db: Session,
    session_id: str,
    current_user,
    *,
    session_model=ExplorationSession,
) -> ExplorationSession:
    session = db.query(session_model).filter(
        session_model.id == session_id
    ).first()
    if not session:
        raise HTTPException(404, "探索会话不存在")
    if (
        session.user_id
        and session.user_id != getattr(current_user, "id", None)
        and getattr(current_user, "role", "") != "admin"
    ):
        raise HTTPException(403, "无权访问他人会话")
    return session


def _session_out(
    session: ExplorationSession,
    *,
    schemas_module=S,
) -> dict:
    return schemas_module.SessionOut.model_validate(session).model_dump(
        by_alias=True
    )


def _message_out(
    message: ExplorationMessage,
    canvas: dict,
    *,
    schemas_module=S,
    build_diagram_fn=build_diagram,
    diagram_error_cls=DiagramError,
) -> dict:
    """Rebuild stored diagrams through the current deterministic quality gate."""
    data = schemas_module.MessageOut.model_validate(message).model_dump(
        by_alias=True
    )
    for step in data.get("steps") or []:
        stored = step.get("diagram")
        if not isinstance(stored, dict):
            continue
        arguments = (
            step.get("arguments")
            if isinstance(step.get("arguments"), dict)
            else {}
        )
        kind = str(stored.get("kind") or arguments.get("kind") or "")
        target = arguments.get("target")
        if not target and "·" not in str(stored.get("target") or ""):
            target = stored.get("target")
        try:
            step["diagram"] = build_diagram_fn(
                canvas,
                kind,
                str(target) if target else None,
            )
        except diagram_error_cls as error:
            step.pop("diagram", None)
            step["error"] = f"历史图表已被质量门拦截：{error}"
            step["summary"] = (
                "历史图表不再满足当前画布的质量要求，请让 AI 修复后重新生成"
            )
    return data


def list_sessions(
    db: Session,
    current_user,
    *,
    session_model=ExplorationSession,
    session_out_fn: Callable[[ExplorationSession], dict] = _session_out,
    ok_fn: Callable[[Any], dict] = _ok,
):
    rows = (
        db.query(session_model)
        .filter(
            session_model.user_id
            == getattr(current_user, "id", None)
        )
        .order_by(session_model.updated_at.desc())
        .limit(100)
        .all()
    )
    return ok_fn([session_out_fn(session) for session in rows])


def _bootstrap_canvas(
    version,
    *,
    canvas_module=C,
    project_snapshot_to_canvas_fn=project_snapshot_to_canvas,
) -> dict | None:
    """从绑定版本引导初始画布：语义层画布优先，其次结构快照反向投影。

    返回 None 表示没有可引导内容（保持空画布现状）。语义层画布经深拷贝 +
    _ensure_canvas 归一，避免与版本快照共享可变引用；反向投影同样过
    _ensure_canvas 归一。
    """
    semantic = (
        version.snapshot_semantic
        if isinstance(version.snapshot_semantic, dict) else {}
    )
    raw_canvas = semantic.get("canvas")
    if isinstance(raw_canvas, dict):
        canvas = canvas_module._ensure_canvas(copy.deepcopy(raw_canvas))
        if any(canvas[key] for key in canvas_module.KIND_KEYS.values()):
            return canvas
    formal = version.snapshot_formal
    if isinstance(formal, dict) and any(
        formal.get(key)
        for key in ("objectTypes", "linkTypes", "actions", "functions", "sentinels")
    ):
        return canvas_module._ensure_canvas(project_snapshot_to_canvas_fn(formal))
    return None


def create_session(
    body,
    db: Session,
    current_user,
    *,
    session_model=ExplorationSession,
    canvas_module=C,
    version_model=OntologyVersion,
    require_ontology_access_fn=require_ontology_access,
    project_snapshot_to_canvas_fn=project_snapshot_to_canvas,
    session_out_fn: Callable[[ExplorationSession], dict] = _session_out,
    ok_fn: Callable[[Any], dict] = _ok,
):
    """创建探索会话；可选绑定本体版本锚点（版本业务语义层挂载点）。

    绑定时校验本体写权限（与草稿落地同一访问惯例）并按版本引导初始画布：
    snapshot_semantic.canvas 非空 → 语义层画布；否则 snapshot_formal 结构
    非空 → 反向投影骨架；皆空 → 空画布。画布写库走与 toolkit._commit_canvas
    相同的 CAS 范式（base 从初始值 0 开始，新会话无并发）。
    """
    bound_version = None
    if body.ontology_version_id:
        bound_version = db.query(version_model).filter(
            version_model.id == str(body.ontology_version_id)
        ).first()
        if bound_version is None:
            raise HTTPException(404, "绑定的本体版本不存在")
        if body.ontology_id and str(bound_version.ontology_id) != str(body.ontology_id):
            raise HTTPException(422, "绑定版本不属于指定本体")
        require_ontology_access_fn(
            db, bound_version.ontology_id, current_user, write=True
        )
    elif body.ontology_id:
        project = require_ontology_access_fn(
            db, str(body.ontology_id), current_user, write=True
        )
        release_id = getattr(project, "current_release_id", None)
        bound_version = (
            db.query(version_model).filter(
                version_model.id == release_id,
                version_model.ontology_id == project.id,
            ).first()
            if release_id else None
        )
        if bound_version is None:
            raise HTTPException(409, "本体尚未建立当前发布版本，无法绑定")
    bootstrap_canvas = (
        _bootstrap_canvas(
            bound_version,
            canvas_module=canvas_module,
            project_snapshot_to_canvas_fn=project_snapshot_to_canvas_fn,
        )
        if bound_version is not None else None
    )
    session = session_model(
        user_id=getattr(current_user, "id", None),
        title=(body.title or "").strip() or "新的业务探索",
        canvas=canvas_module.empty_canvas(),
        ontology_id=bound_version.ontology_id if bound_version else None,
        ontology_version_id=bound_version.id if bound_version else None,
    )
    db.add(session)
    db.flush()
    if bootstrap_canvas is not None:
        result = db.execute(
            sa_update(session_model)
            .where(
                session_model.id == session.id,
                session_model.canvas_version == 0,
            )
            .values(canvas=bootstrap_canvas, canvas_version=1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise HTTPException(409, "会话画布初始化冲突，请重试")
    db.commit()
    db.refresh(session)
    return ok_fn(session_out_fn(session))


def get_session(
    session_id: str,
    db: Session,
    current_user,
    *,
    session_model=ExplorationSession,
    message_model=ExplorationMessage,
    require_session_fn=_require_session,
    session_out_fn: Callable[[ExplorationSession], dict] = _session_out,
    message_out_fn: Callable[[ExplorationMessage, dict], dict] = _message_out,
    canvas_module=C,
    readiness_module=R,
    ok_fn: Callable[[Any], dict] = _ok,
):
    session = require_session_fn(db, session_id, current_user)
    messages = (
        db.query(message_model)
        .filter(message_model.session_id == session.id)
        .order_by(message_model.created_at.asc())
        .limit(300)
        .all()
    )
    return ok_fn({
        **session_out_fn(session),
        "canvas": canvas_module._ensure_canvas(session.canvas),
        "completeness": canvas_module.completeness(session.canvas),
        "readiness": readiness_module.evaluate(session.canvas),
        "messages": [
            message_out_fn(message, session.canvas)
            for message in messages
        ],
    })


def delete_session(
    session_id: str,
    db: Session,
    current_user,
    *,
    require_session_fn=_require_session,
    remove_attachment_file_fn: Callable[[str | None], None],
    attachment_model=ExplorationAttachment,
    message_model=ExplorationMessage,
    draft_model=ExplorationDraft,
    document_model=ExplorationDocument,
):
    """Delete a session while preserving the historical cleanup order.

    Physical attachment cleanup intentionally happens before relational deletes
    and the final commit, matching the established endpoint behavior.
    """
    session = require_session_fn(db, session_id, current_user)
    attachments = db.query(attachment_model).filter(
        attachment_model.session_id == session.id
    ).all()
    for attachment in attachments:
        remove_attachment_file_fn(attachment.file_path)
    db.query(attachment_model).filter(
        attachment_model.session_id == session.id
    ).delete()
    db.query(message_model).filter(
        message_model.session_id == session.id
    ).delete()
    db.query(draft_model).filter(
        draft_model.session_id == session.id
    ).delete()
    db.query(document_model).filter(
        document_model.session_id == session.id
    ).delete()
    db.delete(session)
    db.commit()


def get_canvas(
    session_id: str,
    db: Session,
    current_user,
    *,
    require_session_fn=_require_session,
    canvas_module=C,
    readiness_module=R,
    ok_fn: Callable[[Any], dict] = _ok,
):
    session = require_session_fn(db, session_id, current_user)
    return ok_fn({
        "canvas": canvas_module._ensure_canvas(session.canvas),
        "version": session.canvas_version,
        "completeness": canvas_module.completeness(session.canvas),
        "readiness": readiness_module.evaluate(session.canvas),
    })


def get_readiness(
    session_id: str,
    db: Session,
    current_user,
    *,
    require_session_fn=_require_session,
    readiness_module=R,
    ok_fn: Callable[[Any], dict] = _ok,
):
    session = require_session_fn(db, session_id, current_user)
    return ok_fn(readiness_module.evaluate(session.canvas))


def get_ontology_preview(
    session_id: str,
    db: Session,
    current_user,
    *,
    require_session_fn=_require_session,
    canvas_module=C,
    readiness_module=R,
    converter_module=converter,
    canvas_fingerprint_fn=canvas_fingerprint,
    project_model=OntologyProject,
    schemas_module=S,
    ok_fn: Callable[[Any], dict] = _ok,
):
    """画布 → 本体预览投影（只读）：确定性转换 + 与绑定版本基线比对，不落库。

    与草稿生成同一条确定性管线（converter.build_draft，无 LLM）；绑定会话以
    resolve_merge_baseline_snapshot 解析的基线快照为「已有结构」口径
    （绑定草稿版本优先，其次当前发布；都没有时回退 live 表名集合），
    未绑定会话没有比对基线，全部元素按「将新增」处理（应用时新建本体）。
    """
    session = require_session_fn(db, session_id, current_user)
    canvas = canvas_module._ensure_canvas(session.canvas)
    readiness = readiness_module.evaluate(canvas)
    ontology_id = session.ontology_id
    baseline = None
    existing = None
    if ontology_id:
        project = db.query(project_model).filter(
            project_model.id == ontology_id).first()
        baseline = converter_module.resolve_merge_baseline_snapshot(
            db,
            ontology_id,
            bound_version_id=session.ontology_version_id,
            current_release_id=getattr(project, "current_release_id", None),
        )
        existing = converter_module.existing_name_sets(
            db, ontology_id, snapshot=baseline)
    draft, report = converter_module.build_draft(canvas, existing=existing)
    out = schemas_module.OntologyPreviewOut(
        canvas_fingerprint=canvas_fingerprint_fn(canvas),
        canvas_version=int(session.canvas_version or 0),
        bound=bool(ontology_id),
        ontology_id=ontology_id,
        ontology_version_id=session.ontology_version_id,
        readiness=schemas_module.OntologyPreviewReadiness(
            ready=readiness["ready"],
            gates_passed=readiness["gatesPassed"],
            gates_total=readiness["gatesTotal"],
            blocking_count=readiness["blockingCount"],
            advisory_count=readiness["advisoryCount"],
        ),
        projected=converter_module.project_dispositions(draft, baseline),
        semantic_issues=report.get("semanticIssues") or [],
    )
    return ok_fn(out.model_dump(by_alias=True))


def get_diagram(
    session_id: str,
    kind: str,
    target: str | None,
    db: Session,
    current_user,
    *,
    require_session_fn=_require_session,
    build_diagram_fn=build_diagram,
    diagram_error_cls=DiagramError,
    ok_fn: Callable[[Any], dict] = _ok,
):
    session = require_session_fn(db, session_id, current_user)
    try:
        return ok_fn(build_diagram_fn(session.canvas, kind, target))
    except diagram_error_cls as error:
        raise HTTPException(422, str(error)) from error


def list_diagram_kinds(
    session_id: str,
    db: Session,
    current_user,
    *,
    require_session_fn=_require_session,
    diagram_kinds=DIAGRAM_KINDS,
    ok_fn: Callable[[Any], dict] = _ok,
):
    require_session_fn(db, session_id, current_user)
    return ok_fn({
        "kinds": [
            {"kind": kind, "label": label}
            for kind, label in diagram_kinds.items()
        ],
    })


def list_draft_bindable_ontologies(
    db: Session,
    current_user,
    *,
    project_model=OntologyProject,
    version_model=OntologyVersion,
    require_ontology_access_fn=require_ontology_access,
    ok_fn: Callable[[Any], dict] = _ok,
):
    """「业务澄清」分支②入口数据：有编辑中草稿版本、且当前用户可写的本体。

    平台的「草稿态」只存在于版本层（node_kind=draft 且 lifecycle_status=editing），
    合并落地走绑定会话 → 目标草稿版本，因此这里按合并链路的同一写权限口径
    （require_ontology_access write=True）过滤，同一本体只保留最新一个编辑中
    草稿，按草稿创建时间倒序（最近澄清过的在前）。
    """
    rows = (
        db.query(version_model, project_model)
        .join(project_model, version_model.ontology_id == project_model.id)
        .filter(
            version_model.node_kind == "draft",
            version_model.lifecycle_status == "editing",
        )
        .order_by(version_model.created_at.desc(), version_model.id.desc())
        .all()
    )
    items: list[dict] = []
    seen_projects: set[str] = set()
    for version, project in rows:
        if project.id in seen_projects:
            continue
        try:
            require_ontology_access_fn(db, project.id, current_user, write=True)
        except HTTPException:
            continue
        seen_projects.add(project.id)
        items.append({
            "ontologyId": project.id,
            "ontologyName": project.name,
            "domain": project.domain,
            "versionId": version.id,
            "versionNumber": version.version_number,
            "versionLabel": version.version_label or "",
            "draftCreatedAt": (
                version.created_at.isoformat() if version.created_at else None
            ),
        })
    return ok_fn({"items": items})
