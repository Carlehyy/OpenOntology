from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.shared.config import settings
from app.super_assistant import provider


_CALL_KWARGS = {"provider": "openai", "model": "fake-model", "api_key": "sk-test"}
_MESSAGES = [{"role": "user", "content": "你好"}]


def _chunk(content=None, tool_deltas=None, finish_reason=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_deltas)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


def _tool_delta(index, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _completion(content, tool_calls=None, usage=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class _FakeCompletions:
    def __init__(self, *, stream_chunks=None, stream_error=None, response=None):
        self._stream_chunks = stream_chunks or []
        self._stream_error = stream_error
        self._response = response

    def create(self, **kwargs):
        if kwargs.get("stream"):
            if self._stream_error is not None:
                raise self._stream_error
            return iter(self._stream_chunks)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _patch_openai(monkeypatch, completions: _FakeCompletions) -> None:
    import openai

    monkeypatch.setattr(
        openai, "OpenAI",
        lambda **_kwargs: SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )


def test_openai_stream_aggregates_content_tool_calls_and_usage(monkeypatch):
    _patch_openai(monkeypatch, _FakeCompletions(stream_chunks=[
        _chunk(content="你"),
        _chunk(content="好"),
        _chunk(tool_deltas=[_tool_delta(0, call_id="call-1", name="web_")]),
        _chunk(tool_deltas=[_tool_delta(0, name="search", arguments='{"quer')]),
        _chunk(tool_deltas=[_tool_delta(0, arguments='y": "本体"}')], finish_reason="tool_calls"),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7)),
    ]))
    deltas: list[str] = []
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], deltas.append)
    assert result["content"] == "你好"
    assert result["tool_calls"] == [{
        "id": "call-1", "name": "web_search", "arguments": {"query": "本体"},
    }]
    assert result["usage"] == {"inputTokens": 11, "outputTokens": 7}
    assert "".join(deltas) == "你好"  # 短回复在前缀窗口内，finish 时整体冲刷


def test_openai_stream_filters_think_prefix_across_chunks(monkeypatch):
    _patch_openai(monkeypatch, _FakeCompletions(stream_chunks=[
        _chunk(content="<th"),
        _chunk(content="ink>秘密推理"),
        _chunk(content="</thi"),
        _chunk(content="nk>"),
        _chunk(content="答"),
        _chunk(content="案"),
    ]))
    deltas: list[str] = []
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], deltas.append)
    assert result["content"] == "答案"
    assert deltas == ["答", "案"]


def test_openai_stream_strips_leading_variant_close_across_chunks(monkeypatch):
    """GLM/mm 系：推理体被上游剥离、流首残留 </mm:think> 变体闭合标签。"""
    _patch_openai(monkeypatch, _FakeCompletions(stream_chunks=[
        _chunk(content="</mm:th"),
        _chunk(content="ink>你"),
        _chunk(content="好"),
    ]))
    deltas: list[str] = []
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], deltas.append)
    assert result["content"] == "你好"
    assert "".join(deltas) == "你好"


def test_openai_stream_keeps_midtext_variant_close(monkeypatch):
    """负例：变体闭合标签出现在正文中部时透传（与网关清洗语义一致）。"""
    _patch_openai(monkeypatch, _FakeCompletions(stream_chunks=[
        _chunk(content="闭合标签写作"),
        _chunk(content=" </mm:think> 即可"),
    ]))
    deltas: list[str] = []
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], deltas.append)
    assert result["content"] == "闭合标签写作 </mm:think> 即可"


def test_openai_stream_flushes_buffer_when_no_think_within_prefix(monkeypatch):
    payload = "正文" * 300  # 600 个 CJK 字符，超过 512 前缀窗口
    _patch_openai(monkeypatch, _FakeCompletions(stream_chunks=[_chunk(content=payload)]))
    deltas: list[str] = []
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], deltas.append)
    assert result["content"] == payload
    assert "".join(deltas) == payload


def test_openai_stream_holds_short_reply_until_finish(monkeypatch):
    _patch_openai(monkeypatch, _FakeCompletions(stream_chunks=[
        _chunk(content="你"), _chunk(content="好"),
    ]))
    deltas: list[str] = []
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], deltas.append)
    assert result["content"] == "你好"
    assert "".join(deltas) == "你好"


def test_openai_stream_drops_unclosed_think_content(monkeypatch):
    _patch_openai(monkeypatch, _FakeCompletions(stream_chunks=[
        _chunk(content="<think>只有推理没有结论"),
    ]))
    deltas: list[str] = []
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], deltas.append)
    assert deltas == []
    assert result["content"] == "<think>只有推理没有结论"


def test_openai_stream_marks_truncated_tool_arguments(monkeypatch):
    _patch_openai(monkeypatch, _FakeCompletions(stream_chunks=[
        _chunk(tool_deltas=[_tool_delta(0, call_id="call-1", name="web_search", arguments='{"query": "本')]),
        _chunk(finish_reason="length"),
    ]))
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], None)
    assert result["tool_calls"] == [{
        "id": "call-1", "name": "web_search",
        "arguments": {"_truncated": True, "_raw": '{"query": "本'},
    }]


def test_openai_stream_keeps_raw_arguments_when_not_truncated(monkeypatch):
    _patch_openai(monkeypatch, _FakeCompletions(stream_chunks=[
        _chunk(tool_deltas=[_tool_delta(0, call_id="call-1", name="web_search", arguments="{bad")]),
        _chunk(finish_reason="stop"),
    ]))
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], None)
    assert result["tool_calls"][0]["arguments"] == {"_raw": "{bad"}


def test_openai_stream_falls_back_to_non_streaming(monkeypatch):
    _patch_openai(monkeypatch, _FakeCompletions(
        stream_error=ConnectionError("stream reset"),
        response=_completion("<think>推理</think>完整答复"),
    ))
    deltas: list[str] = []
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], deltas.append)
    assert result["content"] == "完整答复"
    assert deltas == ["完整答复"]


def test_openai_stream_raises_provider_error_when_fallback_also_fails(monkeypatch):
    _patch_openai(monkeypatch, _FakeCompletions(
        stream_error=ConnectionError("stream reset"),
        response=RuntimeError("upstream down"),
    ))
    with pytest.raises(provider.ProviderError, match="模型调用失败"):
        provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], None)


class _FakeAnthropicStream:
    def __init__(self, events, final_message):
        self._events = events
        self._final_message = final_message

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final_message


def _patch_anthropic(monkeypatch, *, events=None, final=None, stream_error=None, create_response=None):
    import anthropic

    def _stream(**_kwargs):
        if stream_error is not None:
            raise stream_error
        return _FakeAnthropicStream(events or [], final)

    def _create(**_kwargs):
        if isinstance(create_response, Exception):
            raise create_response
        return create_response

    monkeypatch.setattr(
        anthropic, "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(stream=_stream, create=_create)),
    )


def _anthropic_kwargs():
    return {"provider": "anthropic", "model": "claude-fake", "api_key": "ak-test"}


def test_anthropic_stream_aggregates_text_tool_use_and_usage(monkeypatch):
    events = [
        SimpleNamespace(type="content_block_start", index=0,
                        content_block=SimpleNamespace(type="text")),
        SimpleNamespace(type="content_block_delta", index=0,
                        delta=SimpleNamespace(type="text_delta", text="你")),
        SimpleNamespace(type="content_block_delta", index=0,
                        delta=SimpleNamespace(type="text_delta", text="好")),
        SimpleNamespace(type="content_block_start", index=1,
                        content_block=SimpleNamespace(type="tool_use", id="toolu_1", name="web_search")),
        SimpleNamespace(type="content_block_delta", index=1,
                        delta=SimpleNamespace(type="input_json_delta", partial_json='{"q": ')),
        SimpleNamespace(type="content_block_delta", index=1,
                        delta=SimpleNamespace(type="input_json_delta", partial_json="1}")),
        SimpleNamespace(type="content_block_stop", index=1),
    ]
    final = SimpleNamespace(
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=9, output_tokens=5),
    )
    _patch_anthropic(monkeypatch, events=events, final=final)
    deltas: list[str] = []
    result = provider.chat_stream(_anthropic_kwargs(), _MESSAGES, [], deltas.append)
    assert result["content"] == "你好"
    assert result["tool_calls"] == [{"id": "toolu_1", "name": "web_search", "arguments": {"q": 1}}]
    assert result["usage"] == {"inputTokens": 9, "outputTokens": 5}
    assert "".join(deltas) == "你好"  # 短回复在前缀窗口内，finish 时整体冲刷


def test_anthropic_stream_filters_think_prefix(monkeypatch):
    events = [
        SimpleNamespace(type="content_block_start", index=0,
                        content_block=SimpleNamespace(type="text")),
        SimpleNamespace(type="content_block_delta", index=0,
                        delta=SimpleNamespace(type="text_delta", text="<think>推")),
        SimpleNamespace(type="content_block_delta", index=0,
                        delta=SimpleNamespace(type="text_delta", text="理</think>结论")),
    ]
    final = SimpleNamespace(stop_reason="end_turn", usage=None)
    _patch_anthropic(monkeypatch, events=events, final=final)
    deltas: list[str] = []
    result = provider.chat_stream(_anthropic_kwargs(), _MESSAGES, [], deltas.append)
    assert result["content"] == "结论"
    assert deltas == ["结论"]


def test_anthropic_stream_marks_truncated_tool_arguments(monkeypatch):
    events = [
        SimpleNamespace(type="content_block_start", index=0,
                        content_block=SimpleNamespace(type="tool_use", id="toolu_1", name="web_fetch")),
        SimpleNamespace(type="content_block_delta", index=0,
                        delta=SimpleNamespace(type="input_json_delta", partial_json='{"url": "ht')),
        SimpleNamespace(type="content_block_stop", index=0),
    ]
    final = SimpleNamespace(stop_reason="max_tokens", usage=None)
    _patch_anthropic(monkeypatch, events=events, final=final)
    result = provider.chat_stream(_anthropic_kwargs(), _MESSAGES, [], None)
    assert result["tool_calls"] == [{
        "id": "toolu_1", "name": "web_fetch",
        "arguments": {"_truncated": True, "_raw": '{"url": "ht'},
    }]


def test_anthropic_stream_falls_back_to_non_streaming(monkeypatch):
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="回退答复")],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
    )
    _patch_anthropic(
        monkeypatch,
        stream_error=RuntimeError("stream broken"),
        create_response=response,
    )
    deltas: list[str] = []
    result = provider.chat_stream(_anthropic_kwargs(), _MESSAGES, [], deltas.append)
    assert result["content"] == "回退答复"
    assert result["usage"] == {"inputTokens": 3, "outputTokens": 2}
    assert deltas == ["回退答复"]


# ---------------------------------------------------------------------------
# 瞬态重试 / prompt caching 断点 / cache usage 采集
# ---------------------------------------------------------------------------


def _status_error(error_cls, status, retry_after=None):
    """构造真实 SDK 异常实例：重试判定走 isinstance，必须用真实异常类。"""
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    request = httpx.Request("POST", "https://api.test/v1/messages")
    response = httpx.Response(status, headers=headers, request=request)
    return error_cls("transient", response=response, body=None)


def _connection_error(error_cls):
    request = httpx.Request("POST", "https://api.test/v1/messages")
    return error_cls(message="connection reset", request=request)


class _ScriptedCompletions:
    """按脚本依次响应 create：Exception 抛出、callable 调用、其余原样返回。"""

    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = 0
        self.stream_calls = 0
        self.stream_kwargs = []

    def create(self, **kwargs):
        if kwargs.get("stream"):
            self.stream_calls += 1
            self.stream_kwargs.append(kwargs)
        else:
            self.calls += 1
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step() if callable(step) else step


def _patch_anthropic_scripted(monkeypatch, *, create_steps=(), stream_steps=()):
    """脚本化 anthropic 客户端：记录每次调用的 kwargs，按脚本抛错/返回。"""
    import anthropic

    recorded = {"create": [], "stream": []}

    def _make(endpoint, steps):
        queue = list(steps)

        def _call(**kwargs):
            recorded[endpoint].append(kwargs)
            step = queue.pop(0)
            if isinstance(step, Exception):
                raise step
            return step() if callable(step) else step

        return _call

    monkeypatch.setattr(
        anthropic, "Anthropic",
        lambda **_kwargs: SimpleNamespace(messages=SimpleNamespace(
            create=_make("create", create_steps),
            stream=_make("stream", stream_steps),
        )),
    )
    return recorded


@pytest.fixture
def sleeps(monkeypatch):
    """替换 provider._sleep，记录退避时长且不做真实等待。"""
    recorded: list[float] = []
    monkeypatch.setattr(provider, "_sleep", recorded.append)
    return recorded


def test_chat_retries_transient_errors_then_succeeds(monkeypatch, sleeps):
    import openai

    completions = _ScriptedCompletions([
        _status_error(openai.RateLimitError, 429),
        _connection_error(openai.APIConnectionError),
        _completion("重试后成功"),
    ])
    _patch_openai(monkeypatch, completions)
    result = provider.chat(_CALL_KWARGS, _MESSAGES, [])
    assert result["content"] == "重试后成功"
    assert completions.calls == 3
    assert sleeps == [1.0, 2.0]


def test_chat_does_not_retry_non_transient_error(monkeypatch, sleeps):
    completions = _ScriptedCompletions([ValueError("bad request")])
    _patch_openai(monkeypatch, completions)
    with pytest.raises(provider.ProviderError, match="模型调用失败"):
        provider.chat(_CALL_KWARGS, _MESSAGES, [])
    assert completions.calls == 1
    assert sleeps == []


def test_chat_prefers_retry_after_header_with_cap(monkeypatch, sleeps):
    import openai

    completions = _ScriptedCompletions([
        _status_error(openai.RateLimitError, 429, retry_after="7"),
        _status_error(openai.InternalServerError, 500, retry_after="30"),
        _completion("ok"),
    ])
    _patch_openai(monkeypatch, completions)
    result = provider.chat(_CALL_KWARGS, _MESSAGES, [])
    assert result["content"] == "ok"
    assert sleeps == [7.0, 10.0]  # Retry-After 优先于指数退避，且封顶 10s


def test_chat_gives_up_after_max_attempts(monkeypatch, sleeps):
    import openai

    completions = _ScriptedCompletions([
        _status_error(openai.RateLimitError, 429),
        _connection_error(openai.APIConnectionError),
        _status_error(openai.InternalServerError, 500),
    ])
    _patch_openai(monkeypatch, completions)
    with pytest.raises(provider.ProviderError, match="模型调用失败"):
        provider.chat(_CALL_KWARGS, _MESSAGES, [])
    assert completions.calls == 3
    assert sleeps == [1.0, 2.0]


def test_anthropic_chat_retries_transient_error(monkeypatch, sleeps):
    import anthropic

    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="anthropic 重试成功")],
        usage=None,
    )
    recorded = _patch_anthropic_scripted(
        monkeypatch,
        create_steps=[_status_error(anthropic.RateLimitError, 429), response],
    )
    result = provider.chat(_anthropic_kwargs(), _MESSAGES, [])
    assert result["content"] == "anthropic 重试成功"
    assert len(recorded["create"]) == 2
    assert sleeps == [1.0]


def test_openai_stream_retries_connect_phase_transient_error(monkeypatch, sleeps):
    import openai

    completions = _ScriptedCompletions([
        _status_error(openai.RateLimitError, 429),
        lambda: iter([_chunk(content="流式成功")]),
    ])
    _patch_openai(monkeypatch, completions)
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], None)
    assert result["content"] == "流式成功"
    assert completions.stream_calls == 2
    assert completions.calls == 0  # 建连重试成功，未触发非流式回退
    assert sleeps == [1.0]


def test_openai_stream_midstream_error_is_not_retried(monkeypatch, sleeps):
    import openai

    def _broken_stream():
        yield _chunk(content="半截")
        raise _connection_error(openai.APIConnectionError)

    completions = _ScriptedCompletions([_broken_stream, _completion("回退内容")])
    _patch_openai(monkeypatch, completions)
    deltas: list[str] = []
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], deltas.append)
    assert result["content"] == "回退内容"
    assert completions.stream_calls == 1  # 迭代中途断线不重试
    assert completions.calls == 1  # 维持既有非流式回退
    assert sleeps == []
    assert deltas == ["回退内容"]  # 半截内容滞留 think 前缀缓冲，未提前透出


def test_openai_stream_requests_usage_via_stream_options(monkeypatch):
    """OpenAI 规范下流式 usage 需显式请求：不发送 stream_options 则对端不给。"""
    completions = _ScriptedCompletions([
        lambda: iter([
            _chunk(content="你好"),
            SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7)),
        ]),
    ])
    _patch_openai(monkeypatch, completions)
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], None)
    assert completions.stream_kwargs[0]["stream_options"] == {"include_usage": True}
    assert result["usage"] == {"inputTokens": 11, "outputTokens": 7}


def test_openai_stream_retries_without_stream_options_when_rejected(monkeypatch):
    """不识别 stream_options 的网关以非瞬态 4xx 拒绝时，去字段重试仍走流式。"""
    import openai

    completions = _ScriptedCompletions([
        _status_error(openai.BadRequestError, 400),
        lambda: iter([_chunk(content="降级后流式成功")]),
    ])
    _patch_openai(monkeypatch, completions)
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], None)
    assert result["content"] == "降级后流式成功"
    assert completions.stream_calls == 2
    assert completions.calls == 0  # 仍走流式，未触发非流式回退
    assert "stream_options" not in completions.stream_kwargs[1]


def test_openai_stream_transient_error_not_retried_without_stream_options(monkeypatch, sleeps):
    """瞬态建连错误只重试带 stream_options 的请求：不触发去字段重试，维持非流式回退。"""
    import openai

    completions = _ScriptedCompletions([
        _status_error(openai.RateLimitError, 429),
        _status_error(openai.RateLimitError, 429),
        _status_error(openai.RateLimitError, 429),
        _completion("回退内容"),
    ])
    _patch_openai(monkeypatch, completions)
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], None)
    assert result["content"] == "回退内容"
    assert completions.stream_calls == 3
    assert all("stream_options" in kwargs for kwargs in completions.stream_kwargs)
    assert completions.calls == 1


_SYSTEM_MESSAGES = [
    {"role": "system", "content": "你是超级助手"},
    {"role": "user", "content": "你好"},
]
_TOOLS = [
    {"name": "web_search", "description": "搜索", "parameters": {"type": "object"}},
    {"name": "web_fetch", "description": "抓取", "parameters": {"type": "object"}},
]


def test_anthropic_chat_injects_cache_breakpoints(monkeypatch):
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="答复")], usage=None,
    )
    recorded = _patch_anthropic_scripted(monkeypatch, create_steps=[response])
    provider.chat(_anthropic_kwargs(), _SYSTEM_MESSAGES, _TOOLS)
    sent = recorded["create"][0]
    assert sent["system"] == [{
        "type": "text", "text": "你是超级助手",
        "cache_control": {"type": "ephemeral"},
    }]
    assert "cache_control" not in sent["tools"][0]
    assert sent["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_chat_omits_cache_breakpoints_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_prompt_cache_enabled", False)
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="答复")], usage=None,
    )
    recorded = _patch_anthropic_scripted(monkeypatch, create_steps=[response])
    provider.chat(_anthropic_kwargs(), _SYSTEM_MESSAGES, _TOOLS)
    sent = recorded["create"][0]
    assert sent["system"] == "你是超级助手"
    assert all("cache_control" not in tool for tool in sent["tools"])


def test_anthropic_stream_injects_cache_breakpoints(monkeypatch):
    final = SimpleNamespace(stop_reason="end_turn", usage=None)
    recorded = _patch_anthropic_scripted(
        monkeypatch,
        stream_steps=[_FakeAnthropicStream([], final)],
    )
    provider.chat_stream(_anthropic_kwargs(), _SYSTEM_MESSAGES, _TOOLS, None)
    sent = recorded["stream"][0]
    assert sent["system"] == [{
        "type": "text", "text": "你是超级助手",
        "cache_control": {"type": "ephemeral"},
    }]
    assert sent["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_stream_collects_cache_usage(monkeypatch):
    final = SimpleNamespace(
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=9, output_tokens=5,
            cache_creation_input_tokens=120, cache_read_input_tokens=340,
        ),
    )
    _patch_anthropic(monkeypatch, events=[], final=final)
    result = provider.chat_stream(_anthropic_kwargs(), _MESSAGES, [], None)
    assert result["usage"] == {
        "inputTokens": 9, "outputTokens": 5,
        "cacheCreationTokens": 120, "cacheReadTokens": 340,
    }


def test_openai_stream_collects_cached_tokens(monkeypatch):
    _patch_openai(monkeypatch, _FakeCompletions(stream_chunks=[
        _chunk(content="你好"),
        SimpleNamespace(choices=[], usage=SimpleNamespace(
            prompt_tokens=11, completion_tokens=7,
            prompt_tokens_details=SimpleNamespace(cached_tokens=6),
        )),
    ]))
    result = provider.chat_stream(_CALL_KWARGS, _MESSAGES, [], None)
    assert result["usage"] == {"inputTokens": 11, "outputTokens": 7, "cacheReadTokens": 6}
