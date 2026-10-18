"""超级助手本体工具 — 经白名单门面到达本体域的唯一桥接。

架构边界：本模块是 ``app.super_assistant`` 内唯一允许 import
``app.ontologies.agent_runtime.assistant_facade`` 的文件（架构守卫精确到
模块）；门面只暴露“用”能力（发现/结构/查询/准备/确认执行），没有任何
定义管理入口。会话/意图/来源由执行器（本模块）派生，模型 schema 里不出现。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.user import User
from app.ontologies.agent_runtime.assistant_facade import (
    FacadeError, build_query_request, confirmation_status,
    current_release_map, decide_confirmation, execute_confirmation,
    prepare_action, query, query_page, structure,
    confirmation_owner_lookup,
)
from app.super_assistant.models import (
    SuperAssistantConversation, SuperAssistantMessage, SuperAssistantToolRun,
)

logger = logging.getLogger(__name__)

SESSION_KIND = "super_assistant"

ONTOLOGY_STRUCTURE_TOOL = "ontology_structure"
ONTOLOGY_QUERY_TOOL = "ontology_query"
ONTOLOGY_PREPARE_TOOL = "ontology_prepare_action"
ONTOLOGY_EXECUTE_TOOL = "ontology_execute_action"

ONTOLOGY_TOOL_NAMES = {
    ONTOLOGY_STRUCTURE_TOOL,
    ONTOLOGY_QUERY_TOOL,
    ONTOLOGY_PREPARE_TOOL,
    ONTOLOGY_EXECUTE_TOOL,
}

_MAX_QUERY_SOURCES = 4


def _load_user(db: Session, owner_id: str):
    user = db.query(User).filter(User.id == owner_id).first()
    if user is None:
        return None
    return user


def _root_intent_id(db: Session, conversation_id: str) -> str | None:
    """本轮人类意图 = 会话内最近一条用户消息（受认证用户提交事件）。"""
    message = (
        db.query(SuperAssistantMessage)
        .filter(SuperAssistantMessage.conversation_id == conversation_id,
                SuperAssistantMessage.role == "user")
        .order_by(SuperAssistantMessage.created_at.desc())
        .first())
    return message.id if message else None


def _require_conversation(db: Session, owner_id: str, conversation_id: str):
    conversation = db.query(SuperAssistantConversation).filter(
        SuperAssistantConversation.id == conversation_id,
        SuperAssistantConversation.owner_id == owner_id).first()
    return conversation


# ------------------------------------------------------------- schemas

def ontology_tool_schemas() -> list[dict[str, Any]]:
    filter_leaf = {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "property": {"type": "string"},
            "op": {"type": "string", "enum": [
                "eq", "neq", "gt", "gte", "lt", "lte",
                "contains", "in", "is_null", "is_not_null"]},
            "value": {},
        },
        "required": ["source", "property", "op"],
        "additionalProperties": False,
    }
    filter_node = {
        "anyOf": [
            filter_leaf,
            {"type": "object", "properties": {
                "and": {"type": "array", "items": {"$ref": "#"}}},
             "required": ["and"], "additionalProperties": False},
            {"type": "object", "properties": {
                "or": {"type": "array", "items": {"$ref": "#"}}},
             "required": ["or"], "additionalProperties": False},
            {"type": "object", "properties": {"not": {"$ref": "#"}},
             "required": ["not"], "additionalProperties": False},
        ],
    }
    query_request = {
        "type": "object",
        "properties": {
            "sources": {
                "type": "array", "minItems": 1, "maxItems": _MAX_QUERY_SOURCES,
                "items": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string"},
                        "ontologyId": {"type": "string"},
                        "releaseId": {"type": "string"},
                        "objectTypeId": {"type": "string"},
                    },
                    "required": ["alias", "ontologyId", "releaseId",
                                 "objectTypeId"],
                    "additionalProperties": False,
                },
            },
            "joins": {
                "type": "array", "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "left": {"type": "string"},
                        "right": {"type": "string"},
                        "type": {"type": "string", "enum": ["inner", "left"]},
                        "on": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "kind": {"const": "explicit_key"},
                                        "leftProperty": {"type": "string"},
                                        "rightProperty": {"type": "string"},
                                        "keyType": {"type": "string",
                                                    "enum": ["string", "number",
                                                             "uuid"]},
                                        "cardinality": {
                                            "type": "string",
                                            "enum": ["1:1", "1:N", "N:1",
                                                     "N:M"]},
                                        "businessMeaning": {"type": "string"},
                                        "evidenceRefs": {
                                            "type": "array",
                                            "items": {"type": "string"}},
                                    },
                                    "required": ["kind", "leftProperty",
                                                 "rightProperty", "keyType",
                                                 "cardinality",
                                                 "businessMeaning",
                                                 "evidenceRefs"],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "kind": {"const": "declared_relation"},
                                        "linkTypeId": {"type": "string"},
                                        "direction": {
                                            "type": "string",
                                            "enum": ["source_to_target",
                                                     "target_to_source"]},
                                    },
                                    "required": ["kind", "linkTypeId",
                                                 "direction"],
                                    "additionalProperties": False,
                                },
                            ],
                        },
                    },
                    "required": ["left", "right", "type", "on"],
                    "additionalProperties": False,
                },
            },
            "sourceFilters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "filter": filter_node,
                    },
                    "required": ["source", "filter"],
                    "additionalProperties": False,
                },
            },
            "where": {"type": "array", "items": filter_node},
            "select": {
                "type": "array", "minItems": 1, "maxItems": 32,
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "property": {"type": "string"},
                        "as": {"type": "string"},
                    },
                    "required": ["source", "property", "as"],
                    "additionalProperties": False,
                },
            },
            "groupBy": {
                "type": "array", "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {"source": {"type": "string"},
                                   "property": {"type": "string"}},
                    "required": ["source", "property"],
                    "additionalProperties": False,
                },
            },
            "aggregates": {
                "type": "array", "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "fn": {"type": "string", "enum": [
                            "count", "count_distinct", "sum", "avg", "min",
                            "max"]},
                        "source": {"type": "string"},
                        "property": {"type": "string"},
                        "as": {"type": "string"},
                    },
                    "required": ["fn", "as"],
                    "additionalProperties": False,
                },
            },
            "distinct": {"type": "boolean"},
            "orderBy": {
                "type": "array", "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "output": {"type": "string"},
                        "direction": {"type": "string",
                                      "enum": ["asc", "desc"]},
                        "nulls": {"type": "string",
                                  "enum": ["first", "last"]},
                    },
                    "required": ["output"],
                    "additionalProperties": False,
                },
            },
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["sources", "select"],
        "additionalProperties": False,
    }
    return [
        {
            "name": ONTOLOGY_STRUCTURE_TOOL,
            "description": (
                "按需读取本体当前发布的结构片段（对象类型/属性/关系/动作），"
                "并携带准确 releaseId/version 来源证据；只读，不加载完整 schema。"
                "需要先经 discovery 确定 ontologyId 与 releaseId。"),
            "input_schema": {
                "type": "object",
                "properties": {
                    "ontologyId": {"type": "string"},
                    "releaseId": {"type": "string"},
                    "objectTypeIds": {"type": "array",
                                      "items": {"type": "string"}},
                    "linkTypeIds": {"type": "array",
                                    "items": {"type": "string"}},
                    "actionIds": {"type": "array",
                                  "items": {"type": "string"}},
                },
                "required": ["ontologyId", "releaseId"],
                "additionalProperties": False,
            },
        },
        {
            "name": ONTOLOGY_QUERY_TOOL,
            "description": (
                "统一关联查询：同一本体内多对象类型、或有明确业务键依据的跨本体"
                "关联；服务端完成过滤/投影/聚合/排序与稳定分页。翻页只传 cursor。"
                "字段/类型/基数依据必须来自 ontology_structure 的当前发布结构。"),
            "input_schema": query_request,
        },
        {
            "name": ONTOLOGY_PREPARE_TOOL,
            "description": (
                "持久准备一个业务动作确认请求（无业务效果）：展开默认值并校验"
                "参数后返回 confirmationId 与确认卡载荷。执行必须经"
                " ontology_execute_action 且需要用户二次确认。"),
            "input_schema": {
                "type": "object",
                "properties": {
                    "ontologyId": {"type": "string"},
                    "releaseId": {"type": "string"},
                    "actionId": {"type": "string"},
                    "targetInstanceId": {"type": "string"},
                    "parameters": {"type": "object"},
                    "parameterRefs": {"type": "object"},
                },
                "required": ["ontologyId", "releaseId", "actionId"],
                "additionalProperties": False,
            },
        },
        {
            "name": ONTOLOGY_EXECUTE_TOOL,
            "description": (
                "唤醒一个已获用户确认的本体动作（只传 confirmationId）。"
                "该调用本身需要用户在界面上二次确认；确认前不产生任何业务效果。"),
            "input_schema": {
                "type": "object",
                "properties": {
                    "confirmationId": {"type": "string"},
                },
                "required": ["confirmationId"],
                "additionalProperties": False,
            },
        },
    ]


# ------------------------------------------------------------- executor

def execute_ontology_tool(db: Session, *, owner_id: str, conversation_id: str,
                          name: str, arguments: dict[str, Any]) -> str:
    """内置工具分派入口 — 会话/意图/来源在此派生，不经模型。"""
    user = _load_user(db, owner_id)
    if user is None:
        return json.dumps({"error": "用户不存在"}, ensure_ascii=False)
    if _require_conversation(db, owner_id, conversation_id) is None:
        return json.dumps({"error": "会话不存在"}, ensure_ascii=False)
    try:
        if name == ONTOLOGY_STRUCTURE_TOOL:
            data = structure(
                db, user,
                str(arguments.get("ontologyId") or ""),
                release_id=str(arguments.get("releaseId") or "") or None,
                object_type_ids=arguments.get("objectTypeIds") or None,
                link_type_ids=arguments.get("linkTypeIds") or None,
                action_ids=arguments.get("actionIds") or None)
            return json.dumps(data, ensure_ascii=False, default=str)
        if name == ONTOLOGY_QUERY_TOOL:
            request = build_query_request(arguments)
            data = query(db, user, request,
                                session_kind=SESSION_KIND,
                                session_id=conversation_id)
            return json.dumps(data, ensure_ascii=False, default=str)
        if name == ONTOLOGY_PREPARE_TOOL:
            root_intent_id = _root_intent_id(db, conversation_id)
            if root_intent_id is None:
                return json.dumps(
                    {"error": "会话内没有用户消息，无法建立确认意图"},
                    ensure_ascii=False)
            data = prepare_action(
                db, user,
                str(arguments.get("ontologyId") or ""),
                session_kind=SESSION_KIND,
                session_id=conversation_id,
                root_intent_id=root_intent_id,
                source_call_id=f"{conversation_id}:{root_intent_id}:"
                               f"{arguments.get('actionId')}",
                action_id=str(arguments.get("actionId") or ""),
                release_id=str(arguments.get("releaseId") or ""),
                target_instance_id=arguments.get("targetInstanceId"),
                parameters=arguments.get("parameters") or {},
                parameter_refs=arguments.get("parameterRefs") or None)
            data = dict(data)
            data["next"] = (
                "调用 ontology_execute_action{confirmationId} 交由用户在界面"
                "二次确认；确认前不会执行。")
            return json.dumps(data, ensure_ascii=False, default=str)
        if name == ONTOLOGY_EXECUTE_TOOL:
            cid = str(arguments.get("confirmationId") or "")
            data = execute_confirmation(
                db, user, confirmation_owner_lookup(db, user, cid), cid,
                session_kind=SESSION_KIND, session_id=conversation_id)
            return json.dumps(data, ensure_ascii=False, default=str)
    except FacadeError as exc:
        return json.dumps({"error": exc.message, "code": exc.code,
                           **exc.extra}, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001 - 工具错误不冒泡为对话失败
        logger.warning("ontology tool %s 执行失败", name, exc_info=True)
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    return json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False)


def decide_from_tool_run(db: Session, current_user, tool_run, decision: str):
    """tool-run approve/deny → 同一确认协议的一次用户二次确认。"""
    arguments = tool_run.arguments or {}
    cid = str(arguments.get("confirmationId") or "")
    ontology_id = confirmation_owner_lookup(db, current_user, cid)
    try:
        status = confirmation_status(
            db, current_user, ontology_id, cid,
            session_kind=SESSION_KIND, session_id=tool_run.conversation_id)
        if decision not in ("approve", "deny"):
            raise FacadeError(422, "invalid_parameter", "未知决策类型")
        return decide_confirmation(
            db, current_user, ontology_id, cid,
            "approve" if decision == "approve" else "deny",
            session_kind=SESSION_KIND, session_id=tool_run.conversation_id,
            decision_id=f"tool-run:{tool_run.id}",
            payload_hash=status["payloadHash"])
    except FacadeError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc
