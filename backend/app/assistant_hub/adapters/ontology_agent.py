"""本体助手 adapter — 白名单符号直连 agent_runtime。

职责：菜单+本体访问检查、建/续子会话引用、跑一个回合（桥接协作取消）、
事件归一。续聊时把 release_id 锚定到子会话所在 release：本体切换当前
发布版后旧子会话仍按原 release 续聊，不会静默漂移成新会话。
"""
from __future__ import annotations

import uuid
from typing import Any, Iterator, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.assistant_hub.contract import (
    STATUS_ANSWERED,
    STATUS_CANCELLED,
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
from app.ontologies.access import require_ontology_access
from app.ontologies.agent_runtime.chat_cancel import chat_cancel_registry
from app.ontologies.agent_runtime.models import AgentConversation
from app.ontologies.agent_runtime.orchestrator import run_agent_turn

_KEY = "ontology_agent"


class OntologyAgentAdapter:
    def spec(self) -> AssistantSpec:
        return AssistantSpec(
            key=_KEY,
            label="本体助手",
            description=(
                "在指定本体的业务数据上查证作答：对象查询/统计/多跳关系/"
                "因果追溯/哨兵解释/决策推演等；数据修改只生成提案，由用户"
                "确认后生效。适合需要基于本体事实回答或分析的问题。"
            ),
            menu_keys=("agent",),
            prerequisites=(
                "context.ontology_id = 目标本体 id；缺省时自动使用该用户"
                "最近一次使用的本体，用户从未使用过则需先向用户询问"
            ),
        )

    # ------------------------------------------------------------------
    def _check_menu(self, db: Session, user) -> None:
        spec = self.spec()
        for menu_key in spec.menu_keys:
            if not user_has_menu_access(db, user, menu_key):
                raise PermissionDeniedError(f"你没有使用{spec.label}的权限")

    def _resolve_ontology_id(self, db: Session, user, context: dict[str, Any] | None) -> str:
        ontology_id = str((context or {}).get("ontology_id") or "").strip()
        if not ontology_id:
            recent = (
                db.query(AgentConversation)
                .filter(AgentConversation.user_id == user.id)
                .order_by(AgentConversation.updated_at.desc())
                .first()
            )
            ontology_id = str(getattr(recent, "ontology_id", "") or "")
        if not ontology_id:
            raise AssistantHubError(
                "缺少目标本体：请在 context.ontology_id 提供本体 id，"
                "或先询问用户要操作哪个本体"
            )
        try:
            require_ontology_access(db, ontology_id, user, write=False)
        except HTTPException as exc:
            raise PermissionDeniedError(f"目标本体不可访问：{exc.detail}") from exc
        return ontology_id

    # ------------------------------------------------------------------
    def start(
        self, db: Session, user, *, context: dict[str, Any] | None = None,
    ) -> str:
        self._check_menu(db, user)
        ontology_id = self._resolve_ontology_id(db, user, context)
        # 首个回合由 run_agent_turn 建会话（避免在这里重复 build_scope）
        return build_ref(_KEY, {"ontology_id": ontology_id})

    def run_turn(
        self, db: Session, user, conversation_ref: str, message: str, *,
        cancel_event=None,
    ) -> Iterator[TurnEvent | TurnResult]:
        self._check_menu(db, user)
        payload = parse_ref(_KEY, conversation_ref)
        ontology_id = str(payload.get("ontology_id") or "")
        conversation_id = payload.get("conversation_id")
        release_id: Optional[str] = None
        if conversation_id:
            conv = (
                db.query(AgentConversation)
                .filter(
                    AgentConversation.id == str(conversation_id),
                    AgentConversation.ontology_id == ontology_id,
                    AgentConversation.user_id == user.id,
                )
                .first()
            )
            if conv is None:
                raise AssistantHubError(
                    "子会话不存在或已失效；请用 session=new 重新开始"
                )
            # 锚定会话所在 release，防 run_agent_turn 按当前 release 静默新建
            release_id = conv.ontology_release_id
        try:
            require_ontology_access(db, ontology_id, user, write=False)
        except HTTPException as exc:
            raise PermissionDeniedError(f"目标本体不可访问：{exc.detail}") from exc

        run_id = str(uuid.uuid4())
        answer_content = ""
        error_message: Optional[str] = None
        cancelled = False
        usage: Optional[dict] = None
        new_conversation_id = str(conversation_id) if conversation_id else None
        for event in run_agent_turn(
            db, ontology_id, user, message,
            conversation_id=conversation_id,
            release_id=release_id,
            run_id=run_id,
        ):
            if cancel_event is not None and cancel_event.is_set():
                chat_cancel_registry.request_cancel(run_id)
            event_type = str(event.get("type") or "")
            if event_type == "meta":
                new_conversation_id = (
                    str(event.get("conversationId")) or new_conversation_id
                )
            elif event_type == "answer":
                answer_content = str(event.get("content") or "")
                usage = event.get("usage")
            elif event_type == "error":
                error_message = str(event.get("message") or "智能体执行失败")
            elif event_type == "cancelled":
                cancelled = True
            yield TurnEvent(
                kind=event_type,
                data={key: value for key, value in event.items() if key != "type"},
            )

        final_ref = build_ref(_KEY, {
            "ontology_id": ontology_id,
            "conversation_id": new_conversation_id,
        })
        created_new = not bool(conversation_id)
        if cancelled:
            yield TurnResult(
                status=STATUS_CANCELLED,
                content="本体助手回合已取消；子会话已保留，可继续委派追问。",
                conversation_ref=final_ref,
                created_new_conversation=created_new,
            )
        elif error_message and not answer_content:
            yield TurnResult(
                status=STATUS_FAILED,
                content=error_message,
                conversation_ref=final_ref,
                created_new_conversation=created_new,
            )
        else:
            yield TurnResult(
                status=STATUS_ANSWERED,
                content=answer_content or "（本体助手未给出内容）",
                conversation_ref=final_ref,
                created_new_conversation=created_new,
                usage=usage,
            )
