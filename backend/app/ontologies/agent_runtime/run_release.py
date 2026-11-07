"""Resolve a new run's release before persisting its conversation or messages."""
from __future__ import annotations

from fastapi import HTTPException

from app.ontologies.agent_runtime.application_errors import conflict, forbidden, not_found
from app.ontologies.agent_runtime.models import AgentConversation
from app.ontologies.release_context import current_release_context


def submission_release_id(db, ontology_id, user_id, body):
    # Explicit release validation remains owned by build_scope; never replace it.
    if body.release_id is not None:
        return body.release_id
    expected = None
    if body.conversation_id:
        conversation = db.query(AgentConversation).filter(
            AgentConversation.id == body.conversation_id,
            AgentConversation.ontology_id == ontology_id,
        ).first()
        if conversation is None:
            raise not_found("会话不存在")
        if conversation.user_id != user_id:
            raise forbidden("无权访问他人会话")
        expected = conversation.ontology_release_id
        if expected is None:
            raise conflict({
                "code": "conversation_release_missing",
                "message": "会话未固定有效发布版本，请基于当前发布版本新建会话。",
            })
    try:
        return current_release_context(
            db, ontology_id, expected_release_id=expected,
        ).id
    except HTTPException as exc:
        if exc.status_code == 404:
            raise not_found("本体不存在") from exc
        detail = dict(exc.detail) if isinstance(exc.detail, dict) else {
            "code": "current_release_invalid", "message": str(exc.detail),
        }
        detail["message"] = (
            "会话固定的发布版本已不可用，请基于当前有效发布版本新建会话。"
            if expected else "本体缺少有效的当前发布版本，请先发布本体后重试。"
        )
        raise conflict(detail) from exc
