"""Conversation queries, audit export, and deletion workflows."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.models.ontology import OntologyProject
from app.ontologies.agent_runtime import schemas as S
from app.ontologies.agent_runtime.application_errors import forbidden, not_found
from app.ontologies.agent_runtime.models import AgentConversation, AgentMessage
from app.ontologies.agent_runtime.run_conversations import (
    conversation_activity_order,
    detail_with_runs,
    list_with_runs,
)
from app.ontologies.agent_runtime.run_submission import delete_conversation_runs


def conversation_out(conversation: AgentConversation) -> dict:
    return (
        S.ConversationOut.model_validate(conversation)
        .model_dump(by_alias=True)
    )


def require_conversation(
    db: Session,
    ontology_id: str,
    conversation_id: str,
    current_user: Any,
) -> AgentConversation:
    conversation = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.ontology_id == ontology_id,
    ).first()
    if not conversation:
        raise not_found("会话不存在")
    if conversation.is_sandbox:
        # 评估沙箱回放会话对用户侧完全不可见，连按 id 直取也不暴露。
        raise not_found("会话不存在")
    if (
        conversation.user_id
        and conversation.user_id != getattr(current_user, "id", None)
        and getattr(current_user, "role", "") != "admin"
    ):
        raise forbidden("无权访问他人会话")
    return conversation


def message_out(
    message: AgentMessage,
    *,
    display_only: bool = False,
) -> dict:
    data = S.MessageOut.model_validate(message).model_dump(by_alias=True)
    if display_only:
        display_steps = []
        for raw_step in data["steps"]:
            step = dict(raw_step)
            if "displayResult" in step:
                step["result"] = step.pop("displayResult")
            display_steps.append(step)
        data["steps"] = display_steps
    return data


def list_conversations(
    db: Session,
    ontology_id: str,
    release_id: str | None,
    current_user: Any,
) -> list[dict]:
    query = db.query(AgentConversation).filter(
        AgentConversation.ontology_id == ontology_id,
        AgentConversation.user_id == getattr(current_user, "id", None),
        # 评估沙箱回放会话不进入用户侧列表（评分快照已存回评估域）
        AgentConversation.is_sandbox.is_(False),
    )
    if release_id is not None:
        query = query.filter(
            AgentConversation.ontology_release_id == release_id,
        )
    rows = (
        query.order_by(conversation_activity_order().desc())
        .limit(50)
        .all()
    )
    return list_with_runs(db, rows, serialize=conversation_out)


def get_conversation(
    db: Session,
    conversation: AgentConversation,
    *,
    message_out_fn: Callable[..., dict] = message_out,
) -> dict:
    messages = (
        db.query(AgentMessage)
        .filter(AgentMessage.conversation_id == conversation.id)
        .order_by(AgentMessage.created_at.asc())
        .limit(200)
        .all()
    )
    data = {
        **conversation_out(conversation),
        "messages": [
            message_out_fn(message, display_only=True)
            for message in messages
        ],
    }
    return detail_with_runs(db, conversation, data)


def export_conversation(
    db: Session,
    ontology: OntologyProject,
    conversation: AgentConversation,
    *,
    now_fn: Callable[[], Any],
    message_out_fn: Callable[..., dict] = message_out,
) -> dict:
    """Return the complete persisted audit trail without the UI replay cap."""
    messages = (
        db.query(AgentMessage)
        .filter(AgentMessage.conversation_id == conversation.id)
        .order_by(AgentMessage.created_at.asc(), AgentMessage.id.asc())
        .all()
    )

    from app.ontologies.decision_simulation import schemas as DecisionSchemas
    from app.ontologies.decision_simulation.models import DecisionSimulationRun

    decision_runs = (
        db.query(DecisionSimulationRun)
        .filter(DecisionSimulationRun.conversation_id == conversation.id)
        .order_by(
            DecisionSimulationRun.started_at.asc(),
            DecisionSimulationRun.id.asc(),
        )
        .all()
    )
    message_rows = [message_out_fn(message) for message in messages]
    legacy_truncated = sum(
        1
        for message in message_rows
        for step in message.get("steps", [])
        if "displayResult" not in step
        and isinstance(step.get("result"), dict)
        and step["result"].get("_truncated") is True
    )
    tool_steps = sum(
        len(message.get("steps", []))
        for message in message_rows
    )
    input_tokens = sum(
        int((message.get("tokenUsage") or {}).get("inputTokens") or 0)
        for message in message_rows
    )
    output_tokens = sum(
        int((message.get("tokenUsage") or {}).get("outputTokens") or 0)
        for message in message_rows
    )

    return {
        "schemaVersion": "openontology.agent-conversation.v1",
        "exportedAt": now_fn(),
        "ontology": {
            "id": ontology.id,
            "name": ontology.name,
            "domain": ontology.domain,
            "version": ontology.version,
        },
        "conversation": {
            "id": conversation.id,
            "ontologyId": conversation.ontology_id,
            "ontologyReleaseId": conversation.ontology_release_id,
            "userId": conversation.user_id,
            "title": conversation.title,
            "createdAt": conversation.created_at,
            "updatedAt": conversation.updated_at,
        },
        "messages": message_rows,
        "decisionSimulations": [
            (
                DecisionSchemas.DecisionSimulationOut
                .model_validate(run, from_attributes=True)
                .model_dump(by_alias=True)
            )
            for run in decision_runs
        ],
        "summary": {
            "messageCount": len(message_rows),
            "userMessageCount": sum(
                message.get("role") == "user"
                for message in message_rows
            ),
            "assistantMessageCount": sum(
                message.get("role") == "assistant"
                for message in message_rows
            ),
            "toolStepCount": tool_steps,
            "decisionSimulationCount": len(decision_runs),
            "tokenUsage": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
            },
            "contentCompleteness": {
                "messageHistory": "complete",
                "toolResults": (
                    "complete"
                    if legacy_truncated == 0
                    else "contains_legacy_truncation"
                ),
                "legacyTruncatedToolResultCount": legacy_truncated,
            },
        },
    }


def delete_conversation(
    db: Session,
    conversation: AgentConversation,
) -> None:
    """Delete conversation-owned records in the established transaction order."""
    from app.ontologies.decision_simulation.models import DecisionSimulationRun

    delete_conversation_runs(db, conversation.id)

    db.query(DecisionSimulationRun).filter(
        DecisionSimulationRun.conversation_id == conversation.id,
    ).delete()
    db.query(AgentMessage).filter(
        AgentMessage.conversation_id == conversation.id,
    ).delete()
    db.delete(conversation)
    db.commit()
