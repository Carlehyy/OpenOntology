"""业务探索 adapter — 白名单符号直连 exploration 域。

会话创建复用 session_service.create_session（标题带 [委派] 前缀作来源
标记）；归属校验复用 _require_session 的 404/403 语义。探索编排器无
协作取消机制：引擎取消 = 停止等待，子回合在后台线程跑到终态并落库。
"""
from __future__ import annotations

from typing import Any, Iterator, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant_hub.contract import (
    STATUS_ANSWERED,
    STATUS_FAILED,
    AssistantHubError,
    AssistantSpec,
    PermissionDeniedError,
    TurnEvent,
    TurnResult,
    build_ref,
    parse_ref,
)
from app.auth.permissions import user_has_menu_access
from app.exploration.orchestrator import run_exploration_turn
from app.exploration.schemas import SessionCreate
from app.exploration.session_service import _require_session, create_session
from app.ontologies.access import require_ontology_access
from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions.models import OntologyVersion

_KEY = "exploration"


def validate_delegated_binding(db: Session, user, context: dict[str, Any] | None) -> dict[str, str]:
    """Validate and normalize the mandatory binding for delegated exploration.

    Direct UI sessions intentionally keep their historical unbound/current-release
    behavior.  Kernel delegation is a separate contract: it must prove the target
    ontology and an editable draft before a child Run or exploration session is
    created, so a missing prerequisite becomes an input request instead of an
    unbound side effect.
    """
    values = context if isinstance(context, dict) else {}
    ontology_id = str(values.get("ontology_id") or "").strip()
    version_id = str(values.get("draft_version_id") or values.get("ontology_version_id") or "").strip()
    lifecycle = str(values.get("lifecycle") or "").strip()
    permission_hash = str(values.get("write_permission_hash") or "").strip()
    if not ontology_id or not version_id or lifecycle != "editing" or not permission_hash:
        raise ValueError(
            "业务探索委派需要绑定 ontology_id、draft_version_id、editing 和 write_permission_hash"
        )
    project = db.scalar(select(OntologyProject).where(OntologyProject.id == ontology_id))
    if project is None:
        raise ValueError("委派绑定的本体不存在")
    try:
        require_ontology_access(db, ontology_id, user, write=True)
    except HTTPException as exc:
        raise ValueError("委派绑定的本体不可写") from exc
    version = db.scalar(select(OntologyVersion).where(OntologyVersion.id == version_id))
    if (
        version is None
        or str(version.ontology_id) != ontology_id
        or version.node_kind != "draft"
        or version.lifecycle_status != "editing"
    ):
        raise ValueError("委派绑定必须指向该本体的 editing draft 版本")
    return {
        "ontology_id": ontology_id,
        "draft_version_id": version_id,
        "lifecycle": "editing",
        "write_permission_hash": permission_hash,
    }


class ExplorationAdapter:
    def spec(self) -> AssistantSpec:
        return AssistantSpec(
            key=_KEY,
            label="业务探索",
            description=(
                "通过多轮澄清把业务想法沉淀为七类模型画布、需求文档和本体"
                "草稿（草稿落地为本体需用户另行确认）。适合需求梳理、业务"
                "建模、模型盘点类任务。"
            ),
            menu_keys=("explore",),
        )

    # ------------------------------------------------------------------
    def _check_menu(self, db: Session, user) -> None:
        spec = self.spec()
        for menu_key in spec.menu_keys:
            if not user_has_menu_access(db, user, menu_key):
                raise PermissionDeniedError(f"你没有使用{spec.label}的权限")

    def _require_owned_session(self, db: Session, session_id: str, user) -> None:
        try:
            _require_session(db, session_id, user)
        except HTTPException as exc:
            if exc.status_code == 403:
                raise PermissionDeniedError("无权访问他人探索会话") from exc
            raise AssistantHubError(
                "探索会话不存在或已失效；请用 session=new 重新开始"
            ) from exc

    # ------------------------------------------------------------------
    def start(
        self, db: Session, user, *, context: dict[str, Any] | None = None,
    ) -> str:
        self._check_menu(db, user)
        values = context if isinstance(context, dict) else {}
        delegated = bool(values.get("delegated_kernel"))
        delegated_binding = validate_delegated_binding(db, user, values) if delegated else None
        title_hint = str(values.get("title_hint") or "").strip()[:40]
        title = f"[委派] {title_hint}" if title_hint else "[委派] 超级助手探索"
        payload = create_session(
            SessionCreate(
                title=title,
                ontology_id=delegated_binding["ontology_id"] if delegated_binding else None,
                ontology_version_id=delegated_binding["draft_version_id"] if delegated_binding else None,
            ),
            db,
            user,
            ok_fn=lambda data: data,
        )
        session_id = str(payload.get("id") or "")
        if not session_id:
            raise AssistantHubError("创建探索会话失败")
        return build_ref(_KEY, {"session_id": session_id})

    def run_turn(
        self, db: Session, user, conversation_ref: str, message: str, *,
        cancel_event=None,
    ) -> Iterator[TurnEvent | TurnResult]:
        self._check_menu(db, user)
        payload = parse_ref(_KEY, conversation_ref)
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            raise AssistantHubError("会话引用已损坏；请用 session=new 重新开始")
        self._require_owned_session(db, session_id, user)

        answer_content = ""
        error_message: Optional[str] = None
        usage: Optional[dict] = None
        for event in run_exploration_turn(db, session_id, user, message):
            event_type = str(event.get("type") or "")
            if event_type == "answer":
                answer_content = str(event.get("content") or "")
                usage = event.get("usage")
            elif event_type == "error":
                error_message = str(event.get("message") or "探索回合执行失败")
            yield TurnEvent(
                kind=event_type,
                data={key: value for key, value in event.items() if key != "type"},
            )

        if error_message and not answer_content:
            yield TurnResult(
                status=STATUS_FAILED,
                content=error_message,
                conversation_ref=conversation_ref,
            )
        else:
            yield TurnResult(
                status=STATUS_ANSWERED,
                content=answer_content or "（探索助手未给出内容）",
                conversation_ref=conversation_ref,
                usage=usage,
            )
