"""llm_gateway 非流式 tool calling 缺陷降级：finish_reason="tool_calls" 但
message.tool_calls 字段缺失时，去掉 tools 重试一次让模型直接作答。

背景：生产内网 GLM MaaS 端点非流式路径在模型决定调工具时丢失
message.tool_calls 字段（content 与 tool_calls 双空），网关原样透传会让
编排器落到"（模型未给出回答）"兜底。触发签名精确锚定该协议异常，健康
端点（tool_calls 正常返回、模型主动不调工具、未传 tools）行为不变。
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace as NS

from app.model_configs import llm_gateway as gw


def _install_fake_openai(monkeypatch, create_impl):
    """create_impl(**kwargs) → 响应；调用时逐个记录 kwargs。"""
    calls: list[dict] = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return create_impl(**kwargs)

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = lambda **kw: NS(chat=NS(completions=FakeCompletions()))
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    return calls


def _resp(content=None, tool_calls=None, finish_reason="stop",
          prompt_tokens=None, completion_tokens=None):
    return NS(choices=[NS(message=NS(role="assistant", content=content,
                                     tool_calls=tool_calls),
                          finish_reason=finish_reason)],
              usage=NS(prompt_tokens=prompt_tokens,
                       completion_tokens=completion_tokens))


def _tool_call(name="search_objects", arguments='{"q": "x"}', call_id="call_1"):
    return NS(id=call_id, function=NS(name=name, arguments=arguments))


_KWARGS = {"provider": "openai", "api_base": "http://fake/v1", "api_key": "k",
           "model": "m", "model_config_id": None}
_MESSAGES = [{"role": "user", "content": "IT安全事件有哪些实例？"}]
_TOOLS = [{"name": "search_objects", "description": "检索对象实例",
           "parameters": {"type": "object",
                          "properties": {"q": {"type": "string"}}}}]


def test_chat_drops_tools_and_retries_when_tool_calls_field_missing(monkeypatch):
    """端点丢失 tool_calls 字段：第二次请求必须去掉 tools，usage 合并两次。"""
    responses = [
        _resp(content=None, tool_calls=None, finish_reason="tool_calls",
              prompt_tokens=None, completion_tokens=10),  # MaaS 实测形态：prompt_tokens 为 null
        _resp(content="我无法查询实时数据，请稍后再试。", finish_reason="stop",
              prompt_tokens=100, completion_tokens=20),
    ]
    state = {"n": 0}

    def create(**kwargs):
        resp = responses[state["n"]]
        state["n"] += 1
        return resp

    calls = _install_fake_openai(monkeypatch, create)

    result = gw.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert state["n"] == 2
    assert "tools" in calls[0] and "tools" not in calls[1]
    assert result["content"] == "我无法查询实时数据，请稍后再试。"
    assert result["tool_calls"] == []
    assert result["usage"] == {"inputTokens": 100, "outputTokens": 30}


def test_chat_keeps_tool_calls_when_endpoint_healthy(monkeypatch):
    """tool_calls 正常返回时不触发降级，单次请求原样解析。"""
    state = {"n": 0}

    def create(**kwargs):
        state["n"] += 1
        return _resp(content=None, tool_calls=[_tool_call()],
                     finish_reason="tool_calls",
                     prompt_tokens=50, completion_tokens=8)

    calls = _install_fake_openai(monkeypatch, create)

    result = gw.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert state["n"] == 1
    assert result["tool_calls"] == [
        {"id": "call_1", "name": "search_objects", "arguments": {"q": "x"}}]
    assert result["usage"] == {"inputTokens": 50, "outputTokens": 8}


def test_chat_no_degradation_for_plain_answer_with_tools_sent(monkeypatch):
    """模型主动不调工具（finish_reason=stop 且有正文）时不重试。"""
    state = {"n": 0}

    def create(**kwargs):
        state["n"] += 1
        return _resp(content="直接回答", finish_reason="stop",
                     prompt_tokens=30, completion_tokens=5)

    _install_fake_openai(monkeypatch, create)

    result = gw.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert state["n"] == 1
    assert result["content"] == "直接回答"


def test_chat_no_degradation_when_tools_not_sent(monkeypatch):
    """未传 tools 的纯文本调用即使响应形状异常也不重试（无降级对象）。"""
    state = {"n": 0}

    def create(**kwargs):
        state["n"] += 1
        return _resp(content=None, tool_calls=None, finish_reason="tool_calls")

    _install_fake_openai(monkeypatch, create)

    result = gw.chat(_KWARGS, _MESSAGES, [])

    assert state["n"] == 1
    assert result["content"] is None
    assert result["tool_calls"] == []


def test_chat_retry_once_when_retry_also_returns_empty(monkeypatch):
    """降级重试仍拿不到正文：只尝试两次即返回，编排器兜底语义保留。"""
    responses = [
        _resp(content=None, tool_calls=None, finish_reason="tool_calls",
              prompt_tokens=None, completion_tokens=10),
        _resp(content=None, tool_calls=None, finish_reason="stop",
              prompt_tokens=None, completion_tokens=5),
    ]
    state = {"n": 0}

    def create(**kwargs):
        resp = responses[state["n"]]
        state["n"] += 1
        return resp

    calls = _install_fake_openai(monkeypatch, create)

    result = gw.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert state["n"] == 2
    assert "tools" not in calls[1]
    assert result["content"] is None
    assert result["tool_calls"] == []
    assert result["usage"] == {"inputTokens": 0, "outputTokens": 15}
