"""上下文压缩：估算消息 token，逼近模型上下文上限时把最旧消息滚动摘要。

触发阈值取 ``max_context_tokens * (1 - super_assistant_context_headroom)``；
摘要覆盖非 system 消息中除最近 ``super_assistant_compaction_keep_recent``
条外的全部，摘要文本持久化在会话的 ``summary``/``summary_message_count``
上，支持基于旧摘要滚动更新。
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

from app.shared.config import settings
from app.super_assistant import provider

logger = logging.getLogger(__name__)

# CJK 统一表意文字、扩展 A、兼容表意、日文假名、韩文音节
_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿぀-ヿ가-힯]")

_TOOL_CONTENT_HEAD = 400
_TOOL_CONTENT_TAIL = 200
_SUMMARY_MAX_OUTPUT_TOKENS = 1024


def estimate_tokens(text: str) -> int:
    """粗估 token 数：CJK 字符按 1 token/字，其余按 ceil(len/4)。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return cjk + math.ceil((len(text) - cjk) / 4)


def estimate_messages(messages: list[dict[str, Any]]) -> int:
    """累加各消息 content 与 tool_calls 参数 JSON 的折算 token。"""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        for call in message.get("tool_calls") or []:
            total += estimate_tokens(
                json.dumps(call.get("arguments") or {}, ensure_ascii=False)
            )
    return total


def maybe_compact(db: Any, conversation: Any, call_kwargs: dict[str, Any],
                  messages: list[dict[str, Any]],
                  redactions: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """超过阈值时压缩 messages；未触发或摘要失败时原样返回入参列表。

    ``call_kwargs["max_context_tokens"]`` 由 runtime 保证存在。压缩成功时
    持久化 conversation.summary 并累计 summary_message_count。

    ``redactions`` 是 ephemeral 明细红线（tool_call_id → 持久文本）：
    未落库的远程联邦明细绝不能经摘要 prompt 进入 conversation.summary，
    摘要只能看到 register 裁决后的持久视图。
    """
    context_limit = int(call_kwargs["max_context_tokens"])
    threshold = context_limit * (1 - settings.super_assistant_context_headroom)
    if estimate_messages(messages) <= threshold:
        return messages
    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    keep_recent = settings.super_assistant_compaction_keep_recent
    if len(non_system) <= keep_recent + 2:
        return messages
    old_messages = non_system[:-keep_recent]
    recent_messages = non_system[-keep_recent:]

    summary_kwargs = {**call_kwargs, "max_output_tokens": _SUMMARY_MAX_OUTPUT_TOKENS}
    try:
        result = provider.chat(
            summary_kwargs,
            [{"role": "user", "content": _summary_prompt(
                old_messages, conversation.summary, redactions)}],
            [],
        )
        summary = str(result.get("content") or "").strip()
        if not summary:
            raise ValueError("摘要内容为空")
    except Exception:
        logger.warning("super_assistant 上下文压缩失败，保留原始消息", exc_info=True)
        return messages

    conversation.summary = summary
    conversation.summary_message_count = int(conversation.summary_message_count or 0) + len(old_messages)
    db.commit()
    return (
        system_messages
        + [{"role": "user", "content": "[早前对话摘要]\n" + summary}]
        + recent_messages
    )


def _summary_prompt(old_messages: list[dict[str, Any]], previous_summary: str | None,
                    redactions: dict[str, str] | None = None) -> str:
    """组装摘要 prompt；tool 长结果只保留头 400 + 尾 200 字符。

    命中 ``redactions`` 的 tool 消息整体替换为红线持久文本（元信息壳），
    不走头尾截断——头 400 字符足以携带多行源明细。
    """
    lines = [
        "请把以下对话历史压缩成一段中文摘要，保留关键事实、已做出的决定和"
        "未完成的待办；丢弃寒暄、客套与重复内容。",
    ]
    if previous_summary:
        lines.append(
            "【已有摘要】请在其基础上滚动合并，不要丢失其中仍然有效的信息：\n"
            + previous_summary
        )
    lines.append("【对话历史】")
    for message in old_messages:
        role = str(message.get("role") or "unknown")
        content = str(message.get("content") or "")
        if redactions and role == "assistant":
            content = "[远程明细轮次：模型复述未进入持久摘要]"
        override = None
        if role == "tool":
            override = (redactions or {}).get(
                str(message.get("tool_call_id") or ""))
        if override is not None:
            content = override
        elif role == "tool" and len(content) > _TOOL_CONTENT_HEAD + _TOOL_CONTENT_TAIL:
            content = (
                content[:_TOOL_CONTENT_HEAD]
                + "\n…[中间内容省略]…\n"
                + content[-_TOOL_CONTENT_TAIL:]
            )
        lines.append(f"{role}: {content}")
        for call in message.get("tool_calls") or []:
            lines.append(
                f"{role} 调用工具 {call.get('name')}: "
                + ("[远程明细上下文参数已隐藏]" if redactions
                   else json.dumps(call.get("arguments") or {}, ensure_ascii=False))
            )
    lines.append("请直接输出摘要正文，不要输出额外说明。")
    return "\n".join(lines)
