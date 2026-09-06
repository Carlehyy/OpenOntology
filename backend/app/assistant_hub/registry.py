"""平台助手注册表：全平台唯一的助手清单来源。

静态清单 = 平台内置助手（adapters/ + 本文件）；动态清单 = 经
``register_dynamic_provider`` 注入的按用户解析 provider（如超级助手侧
用户自配的远程 agent——配置归 super_assistant 域，hub 仍不持有自有数据
模型，依赖方向保持 super_assistant → hub 单向）。委派引擎与工具 schema
只经本注册表取目录，新增/下线助手对引擎零改动。注册表不得登记超级助手
自身（防自递归委派）。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from app.assistant_hub.adapters import exploration as exploration_adapter
from app.assistant_hub.adapters import ontology_agent as ontology_agent_adapter
from app.assistant_hub.contract import PlatformAssistant
from app.auth.permissions import user_has_menu_access

DELEGATION_TOOL_NAME = "delegate_to_assistant"

# 动态目录 provider：(db, user) -> 该用户可用的额外助手（自行负责归属过滤）
DynamicAssistantsProvider = Callable[[Session, Any], list[PlatformAssistant]]

_REGISTRY: tuple[PlatformAssistant, ...] = (
    ontology_agent_adapter.OntologyAgentAdapter(),
    exploration_adapter.ExplorationAdapter(),
)

_dynamic_providers: list[DynamicAssistantsProvider] = []


def register_dynamic_provider(provider: DynamicAssistantsProvider) -> None:
    """注册动态目录 provider（幂等：重复注册同一函数不叠加）。"""
    if provider not in _dynamic_providers:
        _dynamic_providers.append(provider)


def _dynamic_assistants(db: Optional[Session], user) -> list[PlatformAssistant]:
    if db is None or user is None or not _dynamic_providers:
        return []
    merged: list[PlatformAssistant] = []
    for provider in _dynamic_providers:
        merged.extend(provider(db, user))
    return merged


def list_assistants() -> tuple[PlatformAssistant, ...]:
    """静态清单（平台内置助手）。"""
    return _REGISTRY


def get_assistant(
    key: str, db: Optional[Session] = None, user: Any = None,
) -> PlatformAssistant | None:
    """按键解析助手；动态条目需带 db+user（执行路径总会提供）。"""
    for assistant in _REGISTRY:
        if assistant.spec().key == key:
            return assistant
    for assistant in _dynamic_assistants(db, user):
        if assistant.spec().key == key:
            return assistant
    return None


def permitted_assistants(db: Session, user) -> list[PlatformAssistant]:
    """按用户菜单权限过滤（执行时还会重验，这里同时驱动工具目录可见性）。

    动态条目由 provider 自行按归属（owner）过滤；菜单检查同样适用——
    用户自配远程助手 menu_keys 为空（不映射平台菜单），恒通过。
    """
    static = [
        assistant
        for assistant in _REGISTRY
        if all(
            user_has_menu_access(db, user, menu_key)
            for menu_key in assistant.spec().menu_keys
        )
    ]
    dynamic = [
        assistant
        for assistant in _dynamic_assistants(db, user)
        if all(
            user_has_menu_access(db, user, menu_key)
            for menu_key in assistant.spec().menu_keys
        )
    ]
    return static + dynamic


def delegation_tool_schema(
    permitted: list[PlatformAssistant],
) -> dict[str, Any] | None:
    """按当前用户可委派的助手动态生成工具 schema（枚举来自注册表）。"""
    if not permitted:
        return None
    catalog = "\n".join(
        f"- {assistant.spec().key}（{assistant.spec().label}）："
        f"{assistant.spec().description}"
        + (
            f" 前置条件：{assistant.spec().prerequisites}"
            if assistant.spec().prerequisites
            else ""
        )
        for assistant in permitted
    )
    return {
        "name": DELEGATION_TOOL_NAME,
        "description": (
            "以用户分身身份把任务委派给平台内其他助手：子助手在自己的会话中"
            "执行并返回结果，同一超级会话内再次委派同一助手默认续用上次子会话"
            "（上下文保留，不需要重述背景）。子助手答复中若包含澄清问题：能"
            "依据本会话上下文回答就直接再次委派作答，答不了再转述给用户等待"
            "答复。\n可委派助手目录：\n" + catalog
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "assistant": {
                    "type": "string",
                    "enum": [assistant.spec().key for assistant in permitted],
                    "description": "目标助手 key（见目录）",
                },
                "task": {
                    "type": "string",
                    "description": (
                        "交给目标助手的完整任务描述（含必要背景；"
                        "子助手看不到本会话历史）"
                    ),
                },
                "session": {
                    "type": "string",
                    "enum": ["resume", "new"],
                    "description": (
                        "resume=续用本超级会话内该助手最近的子会话（默认）；"
                        "new=另起一条新子会话"
                    ),
                },
                "context": {
                    "type": "object",
                    "description": "目标助手需要的前置参数（见目录中的前置条件）",
                },
            },
            "required": ["assistant", "task"],
            "additionalProperties": False,
        },
    }
