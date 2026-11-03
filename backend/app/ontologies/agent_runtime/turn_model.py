"""Model streaming and cancellation at the turn boundary."""
from __future__ import annotations

from app.ontologies.agent_runtime import llm_bridge
from app.ontologies.agent_runtime.context_policy import budget_messages
from app.ontologies.agent_runtime.limits import limit


def model_events(call_kwargs: dict, messages: list[dict], tools: list[dict], execution=None):
    bounded = budget_messages(messages)
    if sum(len(str(message)) for message in bounded) > limit("context_chars_cap"):
        raise llm_bridge.LLMError("本轮上下文已达到预算，请缩小问题范围或新建会话。")
    if execution is None or not execution.stream_model:
        yield {"final": llm_bridge.chat(call_kwargs, bounded, tools)}
        return
    yield {"type": "delta", "content": "", "reset": True}
    stream = llm_bridge.chat_stream(call_kwargs, bounded, tools)
    try:
        for event in stream:
            if execution.is_cancelled():
                return
            if "delta" in event:
                yield {"type": "delta", "content": event["delta"]}
            elif "final" in event:
                yield {"final": event["final"]}
            elif event.get("unsupported_stream"):
                raise llm_bridge.LLMError("所选模型不支持本体助手的流式协议，请选择支持流式工具调用的模型。")
    finally:
        stream.close()
