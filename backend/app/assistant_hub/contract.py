"""平台助手统一委派契约。

TurnResult.status 收敛为 answered | failed | cancelled：没有任何子助手
会发"需要澄清"信号——澄清就是普通回答内容的一部分，"分身自答还是带回
用户"由超级助手 LLM 读 content 后自行判断。会话引用（conversation_ref）
是不透明字符串，完整载荷由各 adapter 自解析；引用绝不进入 LLM 可见
上下文（compaction 会截断工具结果，续用语义只能由代码查表保证）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Protocol, runtime_checkable

TurnStatus = Literal["answered", "failed", "cancelled"]

STATUS_ANSWERED: TurnStatus = "answered"
STATUS_FAILED: TurnStatus = "failed"
STATUS_CANCELLED: TurnStatus = "cancelled"


class AssistantHubError(Exception):
    """adapter 侧可预期失败（参数缺失/会话失效），message 面向用户可读。"""


class PermissionDeniedError(AssistantHubError):
    """菜单权限、会话归属或域内访问检查未通过。"""


@dataclass(frozen=True)
class AssistantSpec:
    """注册表条目：key 稳定不变；description 供超级助手 LLM 路由。"""

    key: str
    label: str
    description: str
    # 全部 menu key 通过才可委派（复数：如数据管家需要 data + data.pipelines）
    menu_keys: tuple[str, ...] = ()
    # 前置条件说明（LLM 据此在调用前准备 context 参数）
    prerequisites: str = ""


@dataclass(frozen=True)
class TurnEvent:
    """回合内进度事件（meta/step 等），仅用于心跳与日志，不进入 LLM 上下文。"""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnResult:
    """回合终态：run_turn 生成器的最后一个产物必须是本类型。"""

    status: TurnStatus
    content: str = ""
    # 本回合结束后的子会话引用（可能因新建/漂移降级与传入引用不同）
    conversation_ref: str | None = None
    created_new_conversation: bool = False
    # 附注（如 release 漂移后降级新建），供超级助手如实转述
    note: str = ""
    usage: dict[str, Any] | None = None


@runtime_checkable
class PlatformAssistant(Protocol):
    def spec(self) -> AssistantSpec: ...

    def start(self, db, user, *, context: dict[str, Any] | None = None) -> str:
        """创建新子会话，返回不透明 conversation_ref。"""

    def run_turn(
        self, db, user, conversation_ref: str, message: str, *,
        cancel_event=None,
    ) -> Iterator[TurnEvent | TurnResult]:
        """跑一个回合：yield TurnEvent 进度，最后 yield 一个 TurnResult。

        cancel_event 是引擎传入的 threading.Event；能协作传导取消的助手
        （如本体助手桥接 chat_cancel_registry）在收到置位后让子回合尽快
        走到终态，不能传导的助手忽略它（引擎停止等待，子回合后台跑完）。
        """


def build_ref(key: str, payload: dict[str, Any]) -> str:
    """构造不透明会话引用：`<key>:<json>`，载荷由对应 adapter 自解析。"""
    return f"{key}:{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"


def parse_ref(key: str, conversation_ref: str) -> dict[str, Any]:
    prefix = f"{key}:"
    if not conversation_ref.startswith(prefix):
        raise AssistantHubError("会话引用不属于该助手，请用 session=new 重新开始")
    try:
        payload = json.loads(conversation_ref[len(prefix):])
    except ValueError:
        raise AssistantHubError("会话引用已损坏，请用 session=new 重新开始") from None
    if not isinstance(payload, dict):
        raise AssistantHubError("会话引用已损坏，请用 session=new 重新开始")
    return payload
