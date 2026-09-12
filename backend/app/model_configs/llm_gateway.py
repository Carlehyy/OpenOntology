"""
LLM 桥接 — 把统一的「消息 + 工具」协议翻译成各 provider 的 tool-calling API

中立消息格式（orchestrator 只认识这个）：
  {"role": "system"|"user", "content": str}
  {"role": "assistant", "content": str|None, "tool_calls": [{"id","name","arguments": dict}]}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}

返回：{"content": str|None, "tool_calls": [...], "usage": {"inputTokens","outputTokens"}}

provider 取自模型配置（openai | compatible | anthropic）。DeepSeek / Qwen /
Moonshot 等国内主流服务都兼容 OpenAI tools 协议，走 openai 分支即可。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any


class LLMError(Exception):
    """LLM 调用失败 — 配置错误 / 网络 / provider 不支持工具调用等。"""


_SECRET_ERROR = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{10,}|"
    r"(?:sk|api|token|secret)[-_][A-Za-z0-9._~+/=-]{10,})"
)


def _safe_error_message(value: str | None) -> str | None:
    if value is None:
        return None
    return _SECRET_ERROR.sub("***", str(value))[:2_000]


def _failure_status(exc: Exception) -> str:
    message = str(exc).lower()
    return "timeout" if "timeout" in message or "timed out" in message else "error"


# ---------------------------------------------------------------------------
# 瞬态错误重试：仅覆盖建立连接/发起请求阶段的 429 / 5xx / 超时 / 连接错误；
# 流式已开始产出 delta 后中途失败不重试（避免重复内容），由 except 收口上抛。
# openai/anthropic 两个 SDK 均为函数内延迟 import，异常类只能惰性解析。
# SDK 内建重试一律关闭（client 构造显式 max_retries=0），本模块 _with_retry
# 是唯一重试层，避免「网关 × SDK」重试相乘放大上游压力。
# ---------------------------------------------------------------------------

_RETRY_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (1.0, 2.0)  # 3 次尝试之间只 sleep 两次
_RETRY_AFTER_CAP_SECONDS = 10.0


def _sleep(seconds: float) -> None:
    """独立出来的 sleep seam，测试中替换以避免真实等待。"""
    time.sleep(seconds)


def _transient_error_types() -> tuple[type[BaseException], ...]:
    """惰性收集两个 SDK 的瞬态异常类（限流/连接失败/服务端 5xx/超时）。"""
    import importlib

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


def _with_retry(call):
    """对瞬态 SDK 错误最多尝试 3 次，指数退避 1s/2s，Retry-After 头优先。

    非瞬态错误（含 LLMError）立即原样抛出，不改变既有错误语义。
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


def chat(call_kwargs: dict, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
    provider = (call_kwargs.get("provider") or "openai").lower()
    model_name = call_kwargs.get("model", "unknown")
    model_config_id = call_kwargs.get("model_config_id")
    started = time.monotonic()
    status = "success"
    error_msg = None
    from app.shared import perf_spans

    span = perf_spans.begin_span("llm", name="chat.completions", target=f"{provider}/{model_name}")
    try:
        if provider == "anthropic":
            result = _with_retry(lambda: _chat_anthropic(call_kwargs, messages, tools))
        else:
            result = _with_retry(lambda: _chat_openai(call_kwargs, messages, tools))
        return _strip_think(result)
    except LLMError as e:
        status = _failure_status(e)
        error_msg = str(e)
        raise
    except Exception as e:  # noqa: BLE001 — provider SDK 的异常统一收口
        status = _failure_status(e)
        error_msg = str(e)
        raise LLMError(f"LLM 调用失败({provider}/{call_kwargs.get('model')}): {e}") from e
    finally:
        try:
            latency_ms = int((time.monotonic() - started) * 1000)
            if model_config_id:
                _record_call(
                    model_config_id, model_name, provider, status, latency_ms,
                    _safe_error_message(error_msg),
                )
            perf_spans.end_span(span, status=status)
        except Exception:
            pass  # 统计记录失败不影响主流程


def record_llm_call(model_config_id: str, model_name: str, provider: str,
                    status: str, latency_ms: int,
                    error_message: str | None = None) -> None:
    """公共调用记录入口：供不经 chat() 的直连调用方（如 OpenJudge 官方评分器）复用。"""
    _record_call(model_config_id, model_name, provider, status, latency_ms,
                 _safe_error_message(error_message))


def _record_call(model_config_id: str, model_name: str, provider: str,
                 status: str, latency_ms: int, error_message: str | None) -> None:
    from app.database import SessionLocal
    from app.model_configs.models import ModelCallLog

    db = SessionLocal()
    try:
        log = ModelCallLog(
            model_config_id=model_config_id,
            model_name=model_name,
            provider=provider,
            status=status,
            latency_ms=latency_ms,
            error_message=error_message,
        )
        db.add(log)
        db.commit()
    finally:
        db.close()


# 命名空间闭合变体（GLM/mm 系）：推理体被上游 reasoning 通道剥离后，
# content 开头可能残留 </mm:think> 等标签。仅清开头残留；正文中部出现视为
# 普通文本（教程/JSON 字面量不误伤）；大小写敏感（无大写变体生产证据）
_THINK_CLOSE_VARIANT_RE = re.compile(r"\s*</[A-Za-z0-9_.-]+:think>")


def strip_think_content(content: str) -> str:
    """统一 think 清洗语义（本网关与超级助手 provider 共用同一实现）。

    - 精确 ``</think>``：任意位置出现取其后文本（历史语义，覆盖
      DeepSeek-R1/MiniMax 的完整思考块 ``<think>…</think>正文``）；
    - 命名空间变体（``</mm:think>`` 等）：仅当位于 content 开头时剥离
      残留闭合标签（生产实测形态），正文中部不截断；
    - 其余原样返回。
    """
    if "</think>" in content:
        return content.split("</think>", 1)[1].strip()
    match = _THINK_CLOSE_VARIANT_RE.match(content)
    if match:
        return content[match.end():].strip()
    return content


def _strip_think(result: dict) -> dict:
    """清洗模型返回的 <think>...</think> 标签（MiniMax / DeepSeek-R1 等推理模型）。

    模型有时会在正文前附加思考过程，形如：
      <think>用户说 ping，我应该回 pong</think> Pong! ...

    具体语义见 strip_think_content。
    """
    content = result.get("content")
    if content and isinstance(content, str):
        result["content"] = strip_think_content(content)
    return result


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    """中立消息格式 → OpenAI tools 协议（chat 与 chat_stream 共用）。"""
    oai_msgs = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            oai_msgs.append({
                "role": "assistant",
                "content": m.get("content") or None,
                "tool_calls": [{
                    "id": tc["id"], "type": "function",
                    "function": {"name": tc["name"],
                                 "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)},
                } for tc in m["tool_calls"]],
            })
        elif m["role"] == "tool":
            oai_msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"],
                             "content": m["content"]})
        else:
            oai_msgs.append({"role": m["role"], "content": m.get("content") or ""})
    return oai_msgs


_STREAMABLE_PROVIDERS = ("openai", "compatible", "anthropic")


def chat_stream(call_kwargs: dict, messages: list[dict], tools: list[dict]):
    """流式对话：逐段产出 {"delta": str}，最后产出 {"final": {content, tool_calls, usage}}。

    能力判定：openai / compatible / anthropic 均真流式（anthropic 走 messages
    stream，usage 映射 inputTokens/outputTokens）；仅真正未知的 provider 产出
    {"unsupported_stream": True}，由调用方回退 chat()。瞬态错误只在建连阶段
    重试（指数退避 + Retry-After）；delta 已产出后中途失败直接上抛，不重复内容。
    流中 think 块实时过滤（<think>…</think> 不外发，含跨 delta 的半标签缓冲）。
    """
    provider = (call_kwargs.get("provider") or "openai").lower()
    if provider not in _STREAMABLE_PROVIDERS:
        yield {"unsupported_stream": True}
        return

    model_name = call_kwargs.get("model", "unknown")
    model_config_id = call_kwargs.get("model_config_id")
    started = time.monotonic()
    status = "success"
    error_msg = None
    from app.shared import perf_spans

    span = perf_spans.begin_span(
        "llm", name="chat.completions.stream", target=f"{provider}/{model_name}")
    try:
        if provider == "anthropic":
            yield from _stream_anthropic(call_kwargs, messages, tools)
        else:
            yield from _stream_openai(call_kwargs, messages, tools)
    except LLMError as e:
        status = _failure_status(e)
        error_msg = str(e)
        raise
    except Exception as e:  # noqa: BLE001 — provider SDK 的异常统一收口
        status = _failure_status(e)
        error_msg = str(e)
        raise LLMError(
            f"LLM 流式调用失败({provider}/{model_name}): {e}") from e
    finally:
        try:
            latency_ms = int((time.monotonic() - started) * 1000)
            if model_config_id:
                _record_call(model_config_id, model_name, provider, status,
                             latency_ms, _safe_error_message(error_msg))
            perf_spans.end_span(span, status=status)
        except Exception:
            pass  # 统计记录失败不影响主流程


def _stream_openai(call_kwargs: dict, messages: list[dict], tools: list[dict]):
    """openai 兼容协议真流式：text_delta 增量 + tool_calls 分片组装 + usage。

    usage 经 stream_options.include_usage 请求；端点不支持该字段（非瞬态拒绝）
    时去掉字段重建流。只有建连（create 本身）走重试；迭代中途断线不重试，
    避免重复产出 delta。
    """
    import openai

    client = openai.OpenAI(**_openai_client_kwargs(call_kwargs))

    create_kwargs: dict = {
        "model": call_kwargs["model"],
        "messages": _to_openai_messages(messages),
        "temperature": 0.2,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    max_output = call_kwargs.get("max_output_tokens")
    if max_output:
        create_kwargs["max_tokens"] = int(max_output)
    if tools:
        create_kwargs["tools"] = [
            {"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["parameters"]}}
            for t in tools
        ]
    try:
        stream = _with_retry(lambda: client.chat.completions.create(**create_kwargs))
    except Exception as exc:
        if isinstance(exc, _transient_error_types()):
            raise
        create_kwargs.pop("stream_options", None)  # 部分兼容端点不支持 stream_options
        stream = _with_retry(lambda: client.chat.completions.create(**create_kwargs))

    raw_parts: list[str] = []
    tool_acc: dict[int, dict] = {}
    usage = None
    think_state: dict = {}
    for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        if not getattr(chunk, "choices", None):
            continue
        choice = chunk.choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        piece = getattr(delta, "content", None)
        if piece:
            raw_parts.append(piece)
            for safe in _filter_think_deltas(piece, think_state):
                if safe:
                    yield {"delta": safe}
        for tc in (getattr(delta, "tool_calls", None) or []):
            index = getattr(tc, "index", 0) or 0
            slot = tool_acc.setdefault(
                index, {"id": "", "name": "", "arguments": ""})
            if getattr(tc, "id", None):
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] += fn.name
                if getattr(fn, "arguments", None):
                    slot["arguments"] += fn.arguments

    tool_calls = []
    for index in sorted(tool_acc):
        slot = tool_acc[index]
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {"_raw": slot["arguments"]}
        tool_calls.append({"id": slot["id"] or f"call_{index}",
                           "name": slot["name"], "arguments": args})
    yield {"final": _strip_think({
        "content": "".join(raw_parts) or None,
        "tool_calls": tool_calls,
        "usage": {"inputTokens": getattr(usage, "prompt_tokens", None),
                  "outputTokens": getattr(usage, "completion_tokens", None)}
        if usage else None,
    })}


def _stream_anthropic(call_kwargs: dict, messages: list[dict], tools: list[dict]):
    """anthropic messages stream 真流式：产出序列与 openai 分支完全一致。

    client.messages.stream 的 __enter__ 才发起 HTTP 请求（建连阶段），只有它走
    重试；迭代中途断线不重试，避免重复产出 delta。tool_use 的 input_json_delta
    分片按 index 归槽，收尾时与 openai 分支同规则解析（失败落 {"_raw": …}）。
    """
    import anthropic

    client = anthropic.Anthropic(**_anthropic_client_kwargs(call_kwargs))
    manager = client.messages.stream(
        **_anthropic_create_kwargs(call_kwargs, messages, tools))
    stream = _with_retry(manager.__enter__)
    text_blocks: dict[int, list[str]] = {}
    tool_acc: dict[int, dict] = {}
    think_state: dict = {}
    try:
        for event in stream:
            event_type = getattr(event, "type", "")
            index = getattr(event, "index", None)
            if event_type == "content_block_start":
                block = event.content_block
                block_type = getattr(block, "type", "")
                if block_type == "text":
                    text_blocks.setdefault(index, [])
                elif block_type == "tool_use":
                    tool_acc[index] = {
                        "id": getattr(block, "id", "") or "",
                        "name": getattr(block, "name", "") or "",
                        "arguments": "",
                    }
            elif event_type == "content_block_delta":
                delta = event.delta
                delta_type = getattr(delta, "type", "")
                if delta_type == "text_delta":
                    piece = delta.text
                    if piece:
                        text_blocks.setdefault(index, []).append(piece)
                        for safe in _filter_think_deltas(piece, think_state):
                            if safe:
                                yield {"delta": safe}
                elif delta_type == "input_json_delta" and index in tool_acc:
                    tool_acc[index]["arguments"] += delta.partial_json
        final_message = stream.get_final_message()
    finally:
        manager.__exit__(None, None, None)

    tool_calls = []
    for index in sorted(tool_acc):
        slot = tool_acc[index]
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {"_raw": slot["arguments"]}
        tool_calls.append({"id": slot["id"] or f"call_{index}",
                           "name": slot["name"], "arguments": args})
    usage = getattr(final_message, "usage", None)
    yield {"final": _strip_think({
        "content": "\n".join("".join(parts) for _index, parts in sorted(text_blocks.items())) or None,
        "tool_calls": tool_calls,
        "usage": {"inputTokens": getattr(usage, "input_tokens", None),
                  "outputTokens": getattr(usage, "output_tokens", None)}
        if usage else None,
    })}


# 流式 think 过滤器：与 strip_think_content 同语义 —— 只处理位于正文开头的
# <think>…</think> 完整思考块；正文中的 "<" 一旦确认不是块开头即原样放行。
_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"


def _filter_think_deltas(piece: str, state: dict):
    """增量过滤开头 think 块：产出可安全外发的文本片段。

    state 为 {"mode": "detect"|"think"|"pass", "buf": str}，由调用方跨 delta 保持。
    - detect：正文尚未开始，开头可能构成 <think> 的字符全部滞留直到可判定；
    - think：处于思考块内，等待 </think>（末尾疑似半个闭标签的字符滞留）；
    - pass：已确认非 think 开头，后续一切原样放行。
    开头块之外的 <think> 视为普通文本（与历史语义一致：剥离只针对开头思考块）。
    """
    mode = state.get("mode", "detect")
    buf = state.get("buf", "") + piece
    out: list[str] = []

    while buf:
        if mode == "detect":
            if _THINK_OPEN_TAG.startswith(buf) and len(buf) < len(_THINK_OPEN_TAG):
                break  # 仍是可能的开头前缀，继续滞留等待下一个 delta
            if buf.startswith(_THINK_OPEN_TAG):
                mode = "think"
                buf = buf[len(_THINK_OPEN_TAG):]
                continue
            mode = "pass"  # 开头已不可能构成 <think>
            continue
        if mode == "think":
            idx = buf.find(_THINK_CLOSE_TAG)
            if idx < 0:
                keep = max(0, len(buf) - (len(_THINK_CLOSE_TAG) - 1))
                buf = buf[keep:]
                break  # 滞留可能是半个闭标签的尾巴
            buf = buf[idx + len(_THINK_CLOSE_TAG):]
            mode = "pass"
            continue
        out.append(buf)
        buf = ""

    state["mode"] = mode
    state["buf"] = buf
    return out


def _openai_client_kwargs(kw: dict) -> dict:
    client_kwargs: dict = {
        "api_key": kw.get("api_key") or "sk-none",
        "timeout": int(kw.get("timeout_seconds") or 120),
        "max_retries": 0,  # 关闭 SDK 内建重试，网关 _with_retry 为唯一重试层
    }
    if kw.get("api_base"):
        client_kwargs["base_url"] = kw["api_base"]
    return client_kwargs


def _anthropic_client_kwargs(kw: dict) -> dict:
    client_kwargs: dict = {
        "api_key": kw.get("api_key") or "",
        "timeout": int(kw.get("timeout_seconds") or 120),
        "max_retries": 0,  # 关闭 SDK 内建重试，网关 _with_retry 为唯一重试层
    }
    if kw.get("api_base"):
        client_kwargs["base_url"] = kw["api_base"]
    return client_kwargs


def _chat_openai(kw: dict, messages: list[dict], tools: list[dict]) -> dict:
    import openai

    client = openai.OpenAI(**_openai_client_kwargs(kw))

    create_kwargs: dict = {"model": kw["model"], "messages": _to_openai_messages(messages),
                           "temperature": 0.2}
    max_output = kw.get("max_output_tokens")
    if max_output:  # 用户在模型配置中设置的最大输出上限
        create_kwargs["max_tokens"] = int(max_output)
    if tools:  # 空 tools 列表部分 provider 会直接报错，纯文本调用时省略
        create_kwargs["tools"] = [{"type": "function",
                                   "function": {"name": t["name"], "description": t["description"],
                                                "parameters": t["parameters"]}} for t in tools]
    resp = client.chat.completions.create(**create_kwargs)
    msg = resp.choices[0].message
    tool_calls = []
    for tc in (msg.tool_calls or []):
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {"_raw": tc.function.arguments}
        tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
    usage = getattr(resp, "usage", None)
    return {
        "content": msg.content,
        "tool_calls": tool_calls,
        "usage": {"inputTokens": getattr(usage, "prompt_tokens", None),
                  "outputTokens": getattr(usage, "completion_tokens", None)} if usage else None,
    }


def _anthropic_create_kwargs(kw: dict, messages: list[dict], tools: list[dict]) -> dict:
    """中立消息格式 → Anthropic messages 协议（chat 与 chat_stream 共用）。"""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    aa_msgs: list[dict] = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in (m.get("tool_calls") or []):
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"],
                               "input": tc.get("arguments") or {}})
            aa_msgs.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        elif m["role"] == "tool":
            aa_msgs.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": m["tool_call_id"],
                "content": m["content"],
            }]})
        else:
            aa_msgs.append({"role": "user", "content": m.get("content") or ""})

    max_output = int(kw.get("max_output_tokens") or 4096)  # Anthropic 必填，默认 4096
    create_kwargs: dict = {"model": kw["model"], "max_tokens": max_output, "temperature": 0.2,
                           "system": system, "messages": aa_msgs}
    if tools:  # 纯文本调用时省略 tools
        create_kwargs["tools"] = [{"name": t["name"], "description": t["description"],
                                   "input_schema": t["parameters"]} for t in tools]
    return create_kwargs


def _chat_anthropic(kw: dict, messages: list[dict], tools: list[dict]) -> dict:
    import anthropic

    client = anthropic.Anthropic(**_anthropic_client_kwargs(kw))
    resp = client.messages.create(**_anthropic_create_kwargs(kw, messages, tools))
    text_parts, tool_calls = [], []
    for block in resp.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append({"id": block.id, "name": block.name,
                               "arguments": dict(block.input or {})})
    usage = getattr(resp, "usage", None)
    return {
        "content": "\n".join(text_parts) or None,
        "tool_calls": tool_calls,
        "usage": {"inputTokens": getattr(usage, "input_tokens", None),
                  "outputTokens": getattr(usage, "output_tokens", None)} if usage else None,
    }
