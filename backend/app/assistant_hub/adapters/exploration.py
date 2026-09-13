"""业务探索 adapter — 白名单符号直连 exploration 域。

会话创建复用 session_service.create_session（标题带 [委派] 前缀作来源
标记）；归属校验复用 _require_session 的 404/403 语义。探索编排器无
协作取消机制：引擎取消 = 停止等待，子回合在后台线程跑到终态并落库。
"""
from __future__ import annotations

from typing import Any, Iterator, Optional

from fastapi import HTTPException
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
from app.exploration.session_service import (
    _require_session,
    create_session,
    validate_delegated_binding,
)
from app.exploration.models import ExplorationSession

_KEY = "exploration"


def ref_matches_delegated_binding(
    db: Session, user, conversation_ref: str, context: dict[str, Any] | None,
) -> bool:
    """Prove that a persisted exploration ref belongs to the frozen binding.

    This is deliberately a strict predicate for Kernel child recovery.  A
    malformed ref, missing session, changed draft, lost write access, or
    stale permission fingerprint is not treated as a resumable session.
    """
    try:
        payload = parse_ref(_KEY, conversation_ref)
    except AssistantHubError:
        return False
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return False
    session = db.query(ExplorationSession).filter(
        ExplorationSession.id == session_id,
    ).first()
    if session is None or (
        session.user_id
        and session.user_id != getattr(user, "id", None)
        and getattr(user, "role", "") != "admin"
    ):
        return False
    try:
        expected = validate_delegated_binding(db, user, context)
    except ValueError:
        return False
    return (
        str(session.ontology_id or "") == expected["ontology_id"]
        and str(session.ontology_version_id or "") == expected["draft_version_id"]
    )


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
