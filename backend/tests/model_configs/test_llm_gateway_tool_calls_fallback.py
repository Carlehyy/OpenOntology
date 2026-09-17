"""llm_gateway 非流式 tool calling 缺陷两级瀑布降级。

背景：生产内网 GLM MaaS 端点非流式路径在模型决定调工具时丢失
message.tool_calls 字段（content 与 tool_calls 双空），但其流式路径
序列化完好（tool_calls delta 与 usage 均正常）。降级链路：

  非流式(带 tools) 命中缺陷签名
    → 第一级：流式重试（保留工具调用能力）
    → 流式非瞬态失败 → 第二级：去除 tools 重试（保住可用性）
    → 瞬态错误原样上抛，由 chat() 外层 _with_retry 收口

健康端点（tool_calls 正常返回、模型主动不调工具、未传 tools）行为不变。
各级降级事件必须落 warning 日志（运维凭 pm2 日志区分端点缺陷形态）。
"""
from __future__ import annotations

import logging
import sys
import types
from types import SimpleNamespace as NS

from app.model_configs import llm_gateway as gw


class _FakeAPIConnectionError(Exception):
    """挂在 fake openai 模块上，让 _transient_error_types 识别为瞬态。"""


def _install_fake_openai(monkeypatch, create_impl):
    """create_impl(**kwargs) → 响应或异常；调用时逐个记录 kwargs。"""
    calls: list[dict] = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return create_impl(**kwargs)

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = lambda **kw: NS(chat=NS(completions=FakeCompletions()))
    fake_openai.APIConnectionError = _FakeAPIConnectionError
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


def _schunk(content=None, tool_deltas=None, finish_reason=None):
    delta = NS(content=content, tool_calls=tool_deltas)
    return NS(choices=[NS(delta=delta, finish_reason=finish_reason)])


def _stdelta(index, call_id=None, name=None, arguments=None):
    return NS(index=index, id=call_id, function=NS(name=name, arguments=arguments))


def _susage(prompt_tokens=None, completion_tokens=None):
    return NS(choices=[], usage=NS(prompt_tokens=prompt_tokens,
                                   completion_tokens=completion_tokens))


def _defect_resp():
    """GLM MaaS 非流式缺陷形态：finish_reason=tool_calls、字段双空。"""
    return _resp(content=None, tool_calls=None, finish_reason="tool_calls",
                 prompt_tokens=None, completion_tokens=10)


_KWARGS = {"provider": "openai", "api_base": "http://fake/v1", "api_key": "k",
           "model": "m", "model_config_id": None}
_MESSAGES = [{"role": "user", "content": "IT安全事件有哪些实例？"}]
_TOOLS = [{"name": "search_objects", "description": "检索对象实例",
           "parameters": {"type": "object",
                          "properties": {"q": {"type": "string"}}}}]


def test_chat_retries_via_stream_to_restore_tool_calls(monkeypatch, caplog):
    """端点丢失 tool_calls 字段：第二次请求必须转流式且保留 tools，
    工具调用完整恢复（id/name/参数解析），usage 跨形态合并（非流式
    对象 + 流式 dict），降级触发落 warning 日志。"""
    stream_chunks = [
        _schunk(tool_deltas=[_stdelta(0, call_id="call_1",
                                      name="search_objects",
                                      arguments='{"q": "控制目')]),
        _schunk(tool_deltas=[_stdelta(0, arguments='标"}')], finish_reason="tool_calls"),
        _susage(prompt_tokens=3952, completion_tokens=9),
    ]

    def create(**kwargs):
        if kwargs.get("stream"):
            return iter(list(stream_chunks))
        return _defect_resp()

    calls = _install_fake_openai(monkeypatch, create)

    with caplog.at_level(logging.WARNING, logger="app.model_configs.llm_gateway"):
        result = gw.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert len(calls) == 2
    assert not calls[0].get("stream") and "tools" in calls[0]
    assert calls[1].get("stream") is True and "tools" in calls[1]
    assert result["content"] is None
    assert result["tool_calls"] == [
        {"id": "call_1", "name": "search_objects", "arguments": {"q": "控制目标"}}]
    assert result["usage"] == {"inputTokens": 3952, "outputTokens": 19}
    assert any("改用流式重试" in r.message for r in caplog.records)


def test_chat_stream_retry_returns_content_when_model_answers_directly(monkeypatch):
    """流式重试中模型改变主意直接作答：content 原样返回、tool_calls 为空。"""
    stream_chunks = [
        _schunk(content="直接回答"),
        _schunk(finish_reason="stop"),
        _susage(prompt_tokens=30, completion_tokens=5),
    ]

    def create(**kwargs):
        if kwargs.get("stream"):
            return iter(list(stream_chunks))
        return _defect_resp()

    _install_fake_openai(monkeypatch, create)

    result = gw.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert result["content"] == "直接回答"
    assert result["tool_calls"] == []
    assert result["usage"] == {"inputTokens": 30, "outputTokens": 15}


def test_chat_stream_retry_still_empty_warns(monkeypatch, caplog):
    """流式重试后仍无正文与工具调用：结果如实返回空，落 warning 日志。"""
    stream_chunks = [_susage(prompt_tokens=None, completion_tokens=5)]

    def create(**kwargs):
        if kwargs.get("stream"):
            return iter(list(stream_chunks))
        return _defect_resp()

    _install_fake_openai(monkeypatch, create)

    with caplog.at_level(logging.WARNING, logger="app.model_configs.llm_gateway"):
        result = gw.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert result["content"] is None
    assert result["tool_calls"] == []
    assert result["usage"] == {"inputTokens": 0, "outputTokens": 15}
    assert any("流式重试后仍无正文" in r.message for r in caplog.records)


def test_chat_stream_failure_falls_back_to_dropping_tools(monkeypatch, caplog):
    """流式非瞬态失败：先按 _stream_openai 先例去 stream_options 重建流，
    仍失败则第二级瀑布去除 tools 重试拿文字回答；两级事件都落日志。"""
    def create(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("stream rejected by endpoint")
        if "tools" in kwargs:  # 第一次非流式带 tools → 缺陷形态
            return _defect_resp()
        return _resp(content="我无法查询实时数据，请稍后再试。",
                     prompt_tokens=100, completion_tokens=20)

    calls = _install_fake_openai(monkeypatch, create)

    with caplog.at_level(logging.WARNING, logger="app.model_configs.llm_gateway"):
        result = gw.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert len(calls) == 4
    assert calls[1].get("stream") and "stream_options" in calls[1]
    assert calls[2].get("stream") and "stream_options" not in calls[2]
    assert not calls[3].get("stream") and "tools" not in calls[3]
    assert result["content"] == "我无法查询实时数据，请稍后再试。"
    assert result["tool_calls"] == []
    assert result["usage"] == {"inputTokens": 100, "outputTokens": 30}
    assert any("流式重试失败" in r.message for r in caplog.records)


def test_chat_stream_failure_then_no_tools_also_empty(monkeypatch, caplog):
    """两级瀑布走到底仍为空：只重试到第三步，编排器兜底语义保留，
    "降级重试后仍无正文" warning 保留。"""
    def create(**kwargs):
        if kwargs.get("stream"):
            raise RuntimeError("stream rejected by endpoint")
        if "tools" in kwargs:
            return _defect_resp()
        return _resp(content=None, prompt_tokens=None, completion_tokens=5)

    calls = _install_fake_openai(monkeypatch, create)

    with caplog.at_level(logging.WARNING, logger="app.model_configs.llm_gateway"):
        result = gw.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert len(calls) == 4
    assert result["content"] is None
    assert result["tool_calls"] == []
    assert result["usage"] == {"inputTokens": 0, "outputTokens": 15}
    assert any("降级重试后仍无正文" in r.message for r in caplog.records)


def test_chat_stream_retry_transient_error_propagates(monkeypatch):
    """流式重试瞬态错误：原样上抛交给外层 _with_retry，不落第二级瀑布。
    直调 _chat_openai 以绕开 chat() 的外层重试；_stream_openai 建连阶段的
    内部 _with_retry 会尝试 3 次（1 + 3 = 4 次 create）。"""
    monkeypatch.setattr(gw, "_sleep", lambda _s: None)

    def create(**kwargs):
        if kwargs.get("stream"):
            raise _FakeAPIConnectionError("conn reset")
        return _defect_resp()

    calls = _install_fake_openai(monkeypatch, create)

    try:
        gw._chat_openai(_KWARGS, _MESSAGES, _TOOLS)
        raise AssertionError("应当上抛瞬态错误")
    except _FakeAPIConnectionError:
        pass

    assert len(calls) == 4
    assert all(c.get("stream") and "tools" in c for c in calls[1:])
    assert not any("tools" not in c for c in calls)  # 从未落入去 tools 瀑布


def test_chat_keeps_tool_calls_when_endpoint_healthy(monkeypatch):
    """tool_calls 正常返回时不触发降级，单次请求原样解析。"""
    state = {"n": 0}

    def create(**kwargs):
        state["n"] += 1
        return _resp(content=None, tool_calls=[_tool_call()],
                     finish_reason="tool_calls",
                     prompt_tokens=50, completion_tokens=8)

    _install_fake_openai(monkeypatch, create)

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
