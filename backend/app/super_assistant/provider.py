"""Provider adapter owned by the Super Assistant module.

The normalized protocol mirrors Hermes' provider-neutral turn model while
reusing only the platform's model configuration records.
"""
from __future__ import annotations

import importlib
import json
import re
import time
from collections.abc import Callable
from typing import Any

from app.model_configs.llm_gateway import strip_think_content
from app.shared.config import settings


class ProviderError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 瞬态错误重试：openai/anthropic 两个 SDK 均为函数内延迟 import，
# 这里的异常类也只能在命中错误时惰性解析。
# ---------------------------------------------------------------------------

_RETRY_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
_RETRY_AFTER_CAP_SECONDS = 10.0


def _sleep(seconds: float) -> None:
    """独立出来的 sleep seam，测试中替换以避免真实等待。"""
    time.sleep(seconds)


def _transient_error_types() -> tuple[type[BaseException], ...]:
    """惰性收集两个 SDK 的瞬态异常类（限流/连接失败/服务端 5xx/超时）。"""
    types: list[type[BaseException]] = []
    for module_name in ("openai", "anthropic"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for name in ("RateLimitError", "APIConnectionError", "InternalServerError", "APITimeoutError"):
            error_type = getattr(module, name, None)
            if isinstance(error_type, type) and issubclass(error_type, BaseException):
                types.append(error_type)
    return tuple(types)


def _retry_after_seconds(exc: BaseException) -> float | None:
    """读取瞬态错误响应的 Retry-After 头（秒，上限 10s）；缺失或不可解析返回 None。"""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return min(max(seconds, 0.0), _RETRY_AFTER_CAP_SECONDS)


def _with_retry(call: Callable[[], Any]) -> Any:
    """对瞬态 SDK 错误最多尝试 3 次，指数退避 1s/2s/4s，Retry-After 头优先。

    非瞬态错误（含 ProviderError）立即原样抛出，不改变既有错误语义。
    """
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            return call()
        except Exception as exc:
            if attempt + 1 >= _RETRY_MAX_ATTEMPTS or not isinstance(exc, _transient_error_types()):
                raise
            delay = _retry_after_seconds(exc)
            if delay is None:
                delay = _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)]
            _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def chat(call_kwargs: dict[str, Any], messages: list[dict[str, Any]],
         tools: list[dict[str, Any]]) -> dict[str, Any]:
    provider = str(call_kwargs.get("provider") or "openai").lower()
    from app.shared import perf_spans

    span = perf_spans.begin_span(
        "llm",
        name="chat.completions",
        target=f"{provider}/{call_kwargs.get('model') or 'unknown'}",
    )
    status = "success"
    try:
        result = _chat_anthropic(call_kwargs, messages, tools) if provider == "anthropic" else _chat_openai(
            call_kwargs, messages, tools,
        )
    except ProviderError:
        status = "error"
        raise
    except Exception as exc:
        status = "error"
        raise ProviderError(
            f"模型调用失败 ({provider}/{call_kwargs.get('model') or 'unknown'}): {exc}"
        ) from exc
    finally:
        perf_spans.end_span(span, status=status)
    content = result.get("content")
    if isinstance(content, str):
        # think 清洗与 llm_gateway 共用同一语义（含 GLM/mm 系开头残留变体）
        result["content"] = strip_think_content(content)
    return result


def _chat_openai(kw: dict[str, Any], messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]]) -> dict[str, Any]:
    import openai

    client_kwargs: dict[str, Any] = {
        "api_key": kw.get("api_key") or "sk-none",
        "timeout": int(kw.get("timeout_seconds") or 120),
    }
    if kw.get("api_base"):
        client_kwargs["base_url"] = kw["api_base"]
    client = openai.OpenAI(**client_kwargs)

    provider_messages: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "assistant" and message.get("tool_calls"):
            provider_messages.append({
                "role": "assistant",
                "content": message.get("content") or None,
                "tool_calls": [{
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                    },
                } for call in message["tool_calls"]],
            })
        elif message["role"] == "tool":
            provider_messages.append({
                "role": "tool",
                "tool_call_id": message["tool_call_id"],
                "content": message.get("content") or "",
            })
        else:
            provider_messages.append({"role": message["role"], "content": message.get("content") or ""})

    create_kwargs: dict[str, Any] = {
        "model": kw["model"],
        "messages": provider_messages,
        "temperature": 0.2,
    }
    if kw.get("max_output_tokens"):
        create_kwargs["max_tokens"] = int(kw["max_output_tokens"])
    if tools:
        create_kwargs["tools"] = [{
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        } for tool in tools]
    response = _with_retry(lambda: client.chat.completions.create(**create_kwargs))
    if not response.choices:
        raise ProviderError("模型未返回任何候选结果")
    message = response.choices[0].message
    tool_calls = []
    for call in message.tool_calls or []:
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {"_raw": call.function.arguments or ""}
        tool_calls.append({"id": call.id, "name": call.function.name, "arguments": arguments})
    usage = getattr(response, "usage", None)
    return {
        "content": message.content,
        "tool_calls": tool_calls,
        "usage": {
            "inputTokens": getattr(usage, "prompt_tokens", None),
            "outputTokens": getattr(usage, "completion_tokens", None),
        } if usage else {},
    }


def _chat_anthropic(kw: dict[str, Any], messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]]) -> dict[str, Any]:
    import anthropic

    client_kwargs: dict[str, Any] = {
        "api_key": kw.get("api_key") or "",
        "timeout": int(kw.get("timeout_seconds") or 120),
    }
    if kw.get("api_base"):
        client_kwargs["base_url"] = kw["api_base"]
    client = anthropic.Anthropic(**client_kwargs)

    response = _with_retry(
        lambda: client.messages.create(**_anthropic_create_kwargs(kw, messages, tools))
    )
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append({"id": block.id, "name": block.name, "arguments": dict(block.input or {})})
    usage = getattr(response, "usage", None)
    return {
        "content": "\n".join(text_parts) or None,
        "tool_calls": tool_calls,
        "usage": {
            "inputTokens": getattr(usage, "input_tokens", None),
            "outputTokens": getattr(usage, "output_tokens", None),
        } if usage else {},
    }


# ---------------------------------------------------------------------------
# 真流式通道（对标 hermes-llm streaming）：以下均为追加实现，不影响 chat。
# ---------------------------------------------------------------------------

_THINK_PREFIX_LIMIT = 512


class _ThinkPrefixFilter:
    """按增量过滤 ``<think>…</think>`` 前缀，防止推理内容经流式通道泄露。

    前缀窗口（至多 512 字符）内出现 ``<think>`` 则持续缓冲直到 ``</think>``
    出现，之后的增量才开始透传；窗口内没有 ``<think>`` 则冲刷缓冲并透传
    后续全部增量；流结束仍停在 think 段内的内容不透传。GLM/mm 系模型的
    推理体被上游剥离后，开头残留的命名空间闭合变体（``</mm:think>`` 等，
    支持跨增量拼接）同样在 prefix 状态剥离。已知边界：开标签恰好横跨
    512 字符边界、或变体闭合标签被截断在流末尾时可能透出标签片段，
    真实模型的 think 前缀总是从回复开头出现，不受影响。
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"
    _VARIANT_CLOSE_RE = re.compile(r"^\s*</[A-Za-z0-9_.-]+:think>")

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._buffer = ""
        self._state = "prefix"  # prefix -> think/open -> open

    def feed(self, delta: str) -> None:
        if not delta:
            return
        if self._state == "open":
            self._emit(delta)
            return
        self._buffer += delta
        if self._state == "prefix":
            variant = self._VARIANT_CLOSE_RE.match(self._buffer)
            if variant:
                # GLM/mm 系：推理体已被上游剥离，开头残留变体闭合标签
                remainder = self._buffer[variant.end():]
                self._state = "open"
                self._buffer = ""
                if remainder:
                    self._emit(remainder)
                return
            stripped = self._buffer.lstrip()
            if (
                stripped.startswith("</")
                and ">" not in stripped
                and len(stripped) < 64
            ):
                # 可能仍在拼出跨增量的变体闭合标签，继续缓冲
                return
            if self._OPEN in self._buffer:
                self._state = "think"
                self._buffer = self._buffer.split(self._OPEN, 1)[1]
            elif len(self._buffer) >= _THINK_PREFIX_LIMIT:
                self._state = "open"
                self._emit_buffer()
                return
            else:
                return
        if self._state == "think":
            close_at = self._buffer.find(self._CLOSE)
            if close_at == -1:
                # 仅保留可能拼出跨增量闭合标签的尾部，避免长思考占用内存
                keep = len(self._CLOSE) - 1
                if len(self._buffer) > keep:
                    self._buffer = self._buffer[-keep:]
                return
            self._state = "open"
            remainder = self._buffer[close_at + len(self._CLOSE):]
            self._buffer = ""
            if remainder:
                self._emit(remainder)

    def finish(self) -> None:
        """流收尾：前缀阶段冲刷剩余缓冲；think 段内内容丢弃。"""
        if self._state == "prefix":
            self._emit_buffer()
        self._state = "open"
        self._buffer = ""

    def _emit_buffer(self) -> None:
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""


def chat_stream(call_kwargs: dict[str, Any], messages: list[dict[str, Any]],
                tools: list[dict[str, Any]],
                on_delta: Callable[[str], None] | None = None) -> dict[str, Any]:
    """真流式对话：文本增量实时回调 ``on_delta``，返回结构与 chat 相同。

    流式建连/读取抛错时回退非流式调用，成功后把 content 一次性回调；
    ProviderError 语义与 chat 一致。max_tokens 截断导致 tool_calls 参数
    JSON 不完整时，该 call 的 arguments 置 ``{"_truncated": True, "_raw": …}``。
    """
    provider_name = str(call_kwargs.get("provider") or "openai").lower()
    try:
        if provider_name == "anthropic":
            result = _chat_stream_anthropic(call_kwargs, messages, tools, on_delta)
        else:
            result = _chat_stream_openai(call_kwargs, messages, tools, on_delta)
    except Exception:
        result = _chat_fallback(call_kwargs, messages, tools, provider_name)
        fallback_content = result.get("content")
        if isinstance(fallback_content, str):
            fallback_content = strip_think_content(fallback_content)
            result["content"] = fallback_content
        if on_delta and fallback_content:
            on_delta(fallback_content)
        return result
    content = result.get("content")
    if isinstance(content, str):
        result["content"] = strip_think_content(content)
    return result


def _chat_fallback(call_kwargs: dict[str, Any], messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]], provider_name: str) -> dict[str, Any]:
    """流式失败后的非流式回退，ProviderError 语义与 chat 一致。"""
    try:
        return _chat_anthropic(call_kwargs, messages, tools) if provider_name == "anthropic" else _chat_openai(
            call_kwargs, messages, tools,
        )
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(
            f"模型调用失败 ({provider_name}/{call_kwargs.get('model') or 'unknown'}): {exc}"
        ) from exc


def _openai_client(kw: dict[str, Any]) -> Any:
    import openai

    client_kwargs: dict[str, Any] = {
        "api_key": kw.get("api_key") or "sk-none",
        "timeout": int(kw.get("timeout_seconds") or 120),
    }
    if kw.get("api_base"):
        client_kwargs["base_url"] = kw["api_base"]
    return openai.OpenAI(**client_kwargs)


def _openai_messages_payload(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    provider_messages: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "assistant" and message.get("tool_calls"):
            provider_messages.append({
                "role": "assistant",
                "content": message.get("content") or None,
                "tool_calls": [{
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                    },
                } for call in message["tool_calls"]],
            })
        elif message["role"] == "tool":
            provider_messages.append({
                "role": "tool",
                "tool_call_id": message["tool_call_id"],
                "content": message.get("content") or "",
            })
        else:
            provider_messages.append({"role": message["role"], "content": message.get("content") or ""})
    return provider_messages


def _openai_create_kwargs(kw: dict[str, Any], messages: list[dict[str, Any]],
                          tools: list[dict[str, Any]]) -> dict[str, Any]:
    create_kwargs: dict[str, Any] = {
        "model": kw["model"],
        "messages": _openai_messages_payload(messages),
        "temperature": 0.2,
    }
    if kw.get("max_output_tokens"):
        create_kwargs["max_tokens"] = int(kw["max_output_tokens"])
    if tools:
        create_kwargs["tools"] = [{
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        } for tool in tools]
    return create_kwargs


def _chat_stream_openai(kw: dict[str, Any], messages: list[dict[str, Any]],
                        tools: list[dict[str, Any]],
                        on_delta: Callable[[str], None] | None) -> dict[str, Any]:
    client = _openai_client(kw)
    # 不发送 stream_options：部分 OpenAI 兼容网关不识别该字段；usage 在
    # 对端主动给出时顺手采集，否则与非流式一样返回空 dict。
    # 只有建连（create 本身）走重试；迭代中途断线不重试，避免重复产出 delta。
    stream = _with_retry(lambda: client.chat.completions.create(
        **_openai_create_kwargs(kw, messages, tools), stream=True,
    ))
    think_filter = _ThinkPrefixFilter(on_delta or (lambda _delta: None))
    content_parts: list[str] = []
    pending_calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: Any = None
    for chunk in stream:
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage = chunk_usage
        if not getattr(chunk, "choices", None):
            continue
        choice = chunk.choices[0]
        if getattr(choice, "finish_reason", None):
            finish_reason = choice.finish_reason
        delta = choice.delta
        text = getattr(delta, "content", None)
        if text:
            content_parts.append(text)
            think_filter.feed(text)
        for call_delta in getattr(delta, "tool_calls", None) or []:
            slot = pending_calls.setdefault(call_delta.index, {"id": "", "name": "", "arguments": []})
            if getattr(call_delta, "id", None):
                slot["id"] += call_delta.id
            function = getattr(call_delta, "function", None)
            if function is not None:
                if getattr(function, "name", None):
                    slot["name"] += function.name
                if getattr(function, "arguments", None):
                    slot["arguments"].append(function.arguments)
    think_filter.finish()
    tool_calls: list[dict[str, Any]] = []
    for index in sorted(pending_calls):
        slot = pending_calls[index]
        raw = "".join(slot["arguments"])
        try:
            arguments = json.loads(raw or "{}")
        except json.JSONDecodeError:
            arguments = ({"_truncated": True, "_raw": raw} if finish_reason == "length"
                         else {"_raw": raw})
        tool_calls.append({"id": slot["id"], "name": slot["name"], "arguments": arguments})
    usage_payload: dict[str, Any] = ({
        "inputTokens": getattr(usage, "prompt_tokens", None),
        "outputTokens": getattr(usage, "completion_tokens", None),
    } if usage else {})
    cached_tokens = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
    if cached_tokens is not None:
        usage_payload["cacheReadTokens"] = cached_tokens
    return {
        "content": "".join(content_parts) or None,
        "tool_calls": tool_calls,
        "usage": usage_payload,
    }


def _apply_anthropic_cache_breakpoints(create_kwargs: dict[str, Any]) -> None:
    """注入 prompt caching 断点：system 改 text-block 结构、tools 末位加 ephemeral。

    system 为空时不生成空 text block（部分端点会拒绝），tools 为空自然跳过。
    """
    system = create_kwargs.get("system")
    if system:
        create_kwargs["system"] = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]
    tools = create_kwargs.get("tools")
    if tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}


def _anthropic_create_kwargs(kw: dict[str, Any], messages: list[dict[str, Any]],
                             tools: list[dict[str, Any]]) -> dict[str, Any]:
    system = "\n\n".join(message.get("content") or "" for message in messages if message["role"] == "system")
    provider_messages: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "system":
            continue
        if message["role"] == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            blocks.extend({
                "type": "tool_use",
                "id": call["id"],
                "name": call["name"],
                "input": call.get("arguments") or {},
            } for call in message.get("tool_calls") or [])
            provider_messages.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        elif message["role"] == "tool":
            provider_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": message.get("content") or "",
                }],
            })
        else:
            provider_messages.append({"role": "user", "content": message.get("content") or ""})

    create_kwargs: dict[str, Any] = {
        "model": kw["model"],
        "max_tokens": int(kw.get("max_output_tokens") or 4096),
        "temperature": 0.2,
        "system": system,
        "messages": provider_messages,
    }
    if tools:
        create_kwargs["tools"] = [{
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["parameters"],
        } for tool in tools]
    if settings.super_assistant_prompt_cache_enabled:
        _apply_anthropic_cache_breakpoints(create_kwargs)
    return create_kwargs


def _chat_stream_anthropic(kw: dict[str, Any], messages: list[dict[str, Any]],
                           tools: list[dict[str, Any]],
                           on_delta: Callable[[str], None] | None) -> dict[str, Any]:
    import anthropic

    client_kwargs: dict[str, Any] = {
        "api_key": kw.get("api_key") or "",
        "timeout": int(kw.get("timeout_seconds") or 120),
    }
    if kw.get("api_base"):
        client_kwargs["base_url"] = kw["api_base"]
    client = anthropic.Anthropic(**client_kwargs)

    think_filter = _ThinkPrefixFilter(on_delta or (lambda _delta: None))
    text_blocks: dict[int, list[str]] = {}
    tool_blocks: dict[int, dict[str, Any]] = {}
    # __enter__ 才发起 HTTP 请求，即建连阶段，只有它走重试；
    # 迭代中途断线不重试，避免重复产出 delta。
    manager = client.messages.stream(**_anthropic_create_kwargs(kw, messages, tools))
    stream = _with_retry(manager.__enter__)
    try:
        for event in stream:
            event_type = getattr(event, "type", "")
            index = getattr(event, "index", None)
            if event_type == "content_block_start":
                block = event.content_block
                if block.type == "tool_use":
                    tool_blocks[index] = {"id": block.id, "name": block.name, "json": []}
                elif block.type == "text":
                    text_blocks[index] = []
            elif event_type == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta":
                    text_blocks.setdefault(index, []).append(delta.text)
                    think_filter.feed(delta.text)
                elif delta.type == "input_json_delta" and index in tool_blocks:
                    tool_blocks[index]["json"].append(delta.partial_json)
        final_message = stream.get_final_message()
    finally:
        manager.__exit__(None, None, None)
    think_filter.finish()
    stop_reason = getattr(final_message, "stop_reason", None)
    tool_calls: list[dict[str, Any]] = []
    for index in sorted(tool_blocks):
        slot = tool_blocks[index]
        raw = "".join(slot["json"])
        try:
            arguments = json.loads(raw or "{}")
        except json.JSONDecodeError:
            arguments = ({"_truncated": True, "_raw": raw} if stop_reason == "max_tokens"
                         else {"_raw": raw})
        tool_calls.append({"id": slot["id"], "name": slot["name"], "arguments": arguments})
    usage = getattr(final_message, "usage", None)
    usage_payload: dict[str, Any] = ({
        "inputTokens": getattr(usage, "input_tokens", None),
        "outputTokens": getattr(usage, "output_tokens", None),
    } if usage else {})
    if usage:
        cache_creation = getattr(usage, "cache_creation_input_tokens", None)
        if cache_creation is not None:
            usage_payload["cacheCreationTokens"] = cache_creation
        cache_read = getattr(usage, "cache_read_input_tokens", None)
        if cache_read is not None:
            usage_payload["cacheReadTokens"] = cache_read
    return {
        "content": "\n".join("".join(parts) for _index, parts in sorted(text_blocks.items())) or None,
        "tool_calls": tool_calls,
        "usage": usage_payload,
    }
