"""llm_gateway 瞬态错误重试：chat / chat_stream 建连阶段的 429/5xx/超时/连接错误。

仅建连（发起请求）阶段重试，指数退避 + Retry-After；delta 产出后中途失败不重试。
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace as NS

import pytest

from app.model_configs import llm_gateway as gw


class _FakeRateLimitError(Exception):
    def __init__(self, message="429 too many requests", response=None):
        super().__init__(message)
        self.response = response


class _FakeInternalServerError(Exception):
    pass


def _install_fake_openai(monkeypatch, create_impl):
    """create_impl(**kwargs) → 响应或迭代器；调用时逐个记录 kwargs。"""
    calls: list[dict] = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return create_impl(**kwargs)

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = lambda **kw: NS(chat=NS(completions=FakeCompletions()))
    fake_openai.RateLimitError = _FakeRateLimitError
    fake_openai.InternalServerError = _FakeInternalServerError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    return calls


def _record_sleeps(monkeypatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(gw, "_sleep", lambda seconds: sleeps.append(seconds))
    return sleeps


_KWARGS = {"provider": "openai", "api_base": "http://fake/v1", "api_key": "k",
           "model": "m", "model_config_id": None}
_MESSAGES = [{"role": "user", "content": "hi"}]


def test_openai_client_disables_sdk_retries(monkeypatch):
    """SDK 内建重试必须关闭（max_retries=0），网关 _with_retry 是唯一重试层。"""
    client_kwargs_seen: list[dict] = []

    class FakeCompletions:
        def create(self, **kwargs):
            return _chat_response()

    fake_openai = types.ModuleType("openai")
    def _fake_client(**kw):
        client_kwargs_seen.append(kw)
        return NS(chat=NS(completions=FakeCompletions()))
    fake_openai.OpenAI = _fake_client
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    gw.chat(_KWARGS, _MESSAGES, [])

    assert len(client_kwargs_seen) == 1
    assert client_kwargs_seen[0]["max_retries"] == 0


def test_anthropic_client_disables_sdk_retries(monkeypatch):
    client_kwargs_seen: list[dict] = []

    fake_anthropic = types.ModuleType("anthropic")
    def _fake_client(**kw):
        client_kwargs_seen.append(kw)
        return NS(messages=NS(create=lambda **kwargs: NS(
            content=[NS(type="text", text="PONG")],
            usage=NS(input_tokens=3, output_tokens=1))))
    fake_anthropic.Anthropic = _fake_client
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    result = gw.chat({**_KWARGS, "provider": "anthropic"}, _MESSAGES, [])

    assert result["content"] == "PONG"
    assert len(client_kwargs_seen) == 1
    assert client_kwargs_seen[0]["max_retries"] == 0


def _chat_response(content="PONG"):
    return NS(choices=[NS(message=NS(content=content, tool_calls=None))],
              usage=NS(prompt_tokens=3, completion_tokens=1))


def _chunk(content=None, usage=None):
    choices = [NS(delta=NS(content=content, tool_calls=None))] if content is not None else []
    return NS(choices=choices, usage=usage)


# ---------------------------------------------------------------------------
# chat()
# ---------------------------------------------------------------------------

def test_chat_retries_transient_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    def create(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _FakeRateLimitError()
        return _chat_response()

    _install_fake_openai(monkeypatch, create)
    sleeps = _record_sleeps(monkeypatch)

    result = gw.chat(_KWARGS, _MESSAGES, [])

    assert result["content"] == "PONG"
    assert attempts["n"] == 2
    assert sleeps == [1.0]


def test_chat_retry_respects_retry_after_header(monkeypatch):
    attempts = {"n": 0}

    def create(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _FakeRateLimitError(response=NS(headers={"Retry-After": "3"}))
        return _chat_response()

    _install_fake_openai(monkeypatch, create)
    sleeps = _record_sleeps(monkeypatch)

    result = gw.chat(_KWARGS, _MESSAGES, [])

    assert result["content"] == "PONG"
    assert attempts["n"] == 2
    assert sleeps == [3.0]


def test_chat_persistent_server_error_raises_after_max_attempts(monkeypatch):
    attempts = {"n": 0}

    def create(**kwargs):
        attempts["n"] += 1
        raise _FakeInternalServerError("500 internal error")

    _install_fake_openai(monkeypatch, create)
    sleeps = _record_sleeps(monkeypatch)

    with pytest.raises(gw.LLMError):
        gw.chat(_KWARGS, _MESSAGES, [])

    assert attempts["n"] == 3
    assert sleeps == [1.0, 2.0]


def test_chat_non_transient_error_not_retried(monkeypatch):
    attempts = {"n": 0}

    def create(**kwargs):
        attempts["n"] += 1
        raise ValueError("bad request")

    _install_fake_openai(monkeypatch, create)
    sleeps = _record_sleeps(monkeypatch)

    with pytest.raises(gw.LLMError):
        gw.chat(_KWARGS, _MESSAGES, [])

    assert attempts["n"] == 1
    assert sleeps == []


# ---------------------------------------------------------------------------
# chat_stream()：仅建连阶段重试
# ---------------------------------------------------------------------------

def test_chat_stream_retries_connection_phase(monkeypatch):
    attempts = {"n": 0}

    def create(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _FakeRateLimitError()
        return iter([_chunk(content="你好"),
                     _chunk(usage=NS(prompt_tokens=3, completion_tokens=2))])

    _install_fake_openai(monkeypatch, create)
    sleeps = _record_sleeps(monkeypatch)

    events = list(gw.chat_stream(_KWARGS, _MESSAGES, []))

    assert attempts["n"] == 2
    assert sleeps == [1.0]
    assert [e["delta"] for e in events if "delta" in e] == ["你好"]
    final = next(e["final"] for e in events if "final" in e)
    assert final["content"] == "你好"
    assert final["usage"] == {"inputTokens": 3, "outputTokens": 2}


def test_chat_stream_persistent_server_error_raises_after_max_attempts(monkeypatch):
    attempts = {"n": 0}

    def create(**kwargs):
        attempts["n"] += 1
        raise _FakeInternalServerError("500 internal error")

    _install_fake_openai(monkeypatch, create)
    sleeps = _record_sleeps(monkeypatch)

    with pytest.raises(gw.LLMError):
        list(gw.chat_stream(_KWARGS, _MESSAGES, []))

    assert attempts["n"] == 3
    assert sleeps == [1.0, 2.0]


def test_chat_stream_mid_stream_failure_not_retried(monkeypatch):
    """delta 已产出后流中断：不重试（避免重复内容），直接抛 LLMError。"""
    attempts = {"n": 0}

    def broken_stream():
        yield _chunk(content="你")
        raise _FakeRateLimitError()

    def create(**kwargs):
        attempts["n"] += 1
        return broken_stream()

    _install_fake_openai(monkeypatch, create)
    sleeps = _record_sleeps(monkeypatch)

    events = []
    with pytest.raises(gw.LLMError):
        for event in gw.chat_stream(_KWARGS, _MESSAGES, []):
            events.append(event)

    assert events == [{"delta": "你"}]
    assert attempts["n"] == 1
    assert sleeps == []


def test_chat_stream_drops_stream_options_after_non_transient_reject(monkeypatch):
    """端点不支持 stream_options（非瞬态拒绝）时去掉字段重建流，不走退避重试。"""
    def create(**kwargs):
        if "stream_options" in kwargs:
            raise ValueError("unknown field: stream_options")
        return iter([_chunk(content="ok")])

    calls = _install_fake_openai(monkeypatch, create)
    sleeps = _record_sleeps(monkeypatch)

    events = list(gw.chat_stream(_KWARGS, _MESSAGES, []))

    assert len(calls) == 2
    assert "stream_options" in calls[0]
    assert "stream_options" not in calls[1]
    assert sleeps == []
    assert [e["delta"] for e in events if "delta" in e] == ["ok"]


# ---------------------------------------------------------------------------
# chat_stream() anthropic 分支：__enter__ 即建连阶段，走同一套重试
# ---------------------------------------------------------------------------

def test_chat_stream_anthropic_retries_enter_phase(monkeypatch):
    attempts = {"n": 0}

    class FakeStream:
        def __iter__(self):
            return iter([
                NS(type="content_block_start", index=0,
                   content_block=NS(type="text")),
                NS(type="content_block_delta", index=0,
                   delta=NS(type="text_delta", text="你好")),
            ])

        def get_final_message(self):
            return NS(usage=NS(input_tokens=3, output_tokens=2),
                      stop_reason="end_turn")

    class FakeManager:
        def __enter__(self):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _FakeRateLimitError()
            return FakeStream()

        def __exit__(self, *args):
            return False

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = lambda **kw: NS(
        messages=NS(stream=lambda **kwargs: FakeManager()))
    fake_anthropic.RateLimitError = _FakeRateLimitError
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    sleeps = _record_sleeps(monkeypatch)

    events = list(gw.chat_stream(
        {**_KWARGS, "provider": "anthropic"}, _MESSAGES, []))

    assert attempts["n"] == 2
    assert sleeps == [1.0]
    assert [e["delta"] for e in events if "delta" in e] == ["你好"]
    final = next(e["final"] for e in events if "final" in e)
    assert final["usage"] == {"inputTokens": 3, "outputTokens": 2}
