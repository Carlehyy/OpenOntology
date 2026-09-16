"""super_assistant.provider 非流式 tool calling 缺陷降级：finish_reason=
"tool_calls" 但 message.tool_calls 字段缺失时，去掉 tools 重试一次让模型
直接作答。

与 llm_gateway 同款缺陷签名（生产内网 GLM MaaS 非流式路径丢失
message.tool_calls 字段）。run_subagent 是本模块唯一非流式带 tools 的
调用方，缺该降级会静默返回空结论。健康端点（tool_calls 正常返回、模型
主动不调工具、未传 tools）行为不变；降级触发与"重试仍为空"两个事件
必须落 warning 日志（运维凭 pm2 日志定位端点缺陷的唯一信号）。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace as NS

from app.super_assistant import provider


def _install_fake_openai(monkeypatch, create_impl):
    """create_impl(**kwargs) → 响应；调用时逐个记录 kwargs。"""
    calls: list[dict] = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return create_impl(**kwargs)

    import openai
    monkeypatch.setattr(
        openai, "OpenAI",
        lambda **_kw: NS(chat=NS(completions=FakeCompletions())))

    return calls


def _resp(content=None, tool_calls=None, finish_reason="stop",
          prompt_tokens=None, completion_tokens=None):
    return NS(choices=[NS(message=NS(role="assistant", content=content,
                                     tool_calls=tool_calls),
                          finish_reason=finish_reason)],
              usage=NS(prompt_tokens=prompt_tokens,
                       completion_tokens=completion_tokens))


def _tool_call(name="web_search", arguments='{"query": "x"}', call_id="call_1"):
    return NS(id=call_id, function=NS(name=name, arguments=arguments))


_KWARGS = {"provider": "openai", "api_base": "http://fake/v1", "api_key": "k",
           "model": "m"}
_MESSAGES = [{"role": "user", "content": "帮我查一下平台公告"}]
_TOOLS = [{"name": "web_search", "description": "联网搜索",
           "parameters": {"type": "object",
                          "properties": {"query": {"type": "string"}}}}]


def test_chat_drops_tools_and_retries_when_tool_calls_field_missing(monkeypatch, caplog):
    """端点丢失 tool_calls 字段：第二次请求必须去掉 tools，usage 合并两次，
    且降级触发必须落 warning 日志。"""
    responses = [
        _resp(content=None, tool_calls=None, finish_reason="tool_calls",
              prompt_tokens=None, completion_tokens=10),  # MaaS 实测形态：prompt_tokens 为 null
        _resp(content="检索通道暂不可用，请稍后再试。", finish_reason="stop",
              prompt_tokens=100, completion_tokens=20),
    ]
    state = {"n": 0}

    def create(**kwargs):
        resp = responses[state["n"]]
        state["n"] += 1
        return resp

    calls = _install_fake_openai(monkeypatch, create)

    with caplog.at_level(logging.WARNING, logger="app.super_assistant.provider"):
        result = provider.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert state["n"] == 2
    assert "tools" in calls[0] and "tools" not in calls[1]
    assert result["content"] == "检索通道暂不可用，请稍后再试。"
    assert result["tool_calls"] == []
    assert result["usage"] == {"inputTokens": 100, "outputTokens": 30}
    assert any("缺失 tool_calls 字段" in r.message for r in caplog.records)


def test_chat_keeps_tool_calls_when_endpoint_healthy(monkeypatch):
    """tool_calls 正常返回时不触发降级，单次请求原样解析。"""
    state = {"n": 0}

    def create(**kwargs):
        state["n"] += 1
        return _resp(content=None, tool_calls=[_tool_call()],
                     finish_reason="tool_calls",
                     prompt_tokens=50, completion_tokens=8)

    _install_fake_openai(monkeypatch, create)

    result = provider.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert state["n"] == 1
    assert result["tool_calls"] == [
        {"id": "call_1", "name": "web_search", "arguments": {"query": "x"}}]
    assert result["usage"] == {"inputTokens": 50, "outputTokens": 8}


def test_chat_no_degradation_for_plain_answer_with_tools_sent(monkeypatch):
    """模型主动不调工具（finish_reason=stop 且有正文）时不重试。"""
    state = {"n": 0}

    def create(**kwargs):
        state["n"] += 1
        return _resp(content="直接回答", finish_reason="stop",
                     prompt_tokens=30, completion_tokens=5)

    _install_fake_openai(monkeypatch, create)

    result = provider.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert state["n"] == 1
    assert result["content"] == "直接回答"


def test_chat_no_degradation_when_tools_not_sent(monkeypatch):
    """未传 tools 的纯文本调用即使响应形状异常也不重试（无降级对象）。
    usage 存在但字段为 null（GLM MaaS 形态）时按 merged_usage 语义计 0。"""
    state = {"n": 0}

    def create(**kwargs):
        state["n"] += 1
        return _resp(content=None, tool_calls=None, finish_reason="tool_calls")

    _install_fake_openai(monkeypatch, create)

    result = provider.chat(_KWARGS, _MESSAGES, [])

    assert state["n"] == 1
    assert result["content"] is None
    assert result["tool_calls"] == []
    assert result["usage"] == {"inputTokens": 0, "outputTokens": 0}


def test_chat_retry_preserves_tool_history_messages(monkeypatch):
    """多轮工具循环中途命中缺陷：降级重试必须原样保留消息历史（含
    assistant tool_calls 与 tool 结果），仅去掉 tools 参数——锁定"降级
    只降工具、不改写上下文"的契约，防止未来重构在重试时篡改历史导致
    静默答错。"""
    history = [
        {"role": "system", "content": "你是子代理"},
        {"role": "user", "content": "帮我查平台公告"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "name": "web_search",
                         "arguments": {"query": "公告"}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "web_search",
         "content": "{\"results\": []}"},
    ]
    responses = [
        _resp(content=None, tool_calls=None, finish_reason="tool_calls"),
        _resp(content="检索结果为空，无相关公告。", finish_reason="stop",
              prompt_tokens=80, completion_tokens=12),
    ]
    state = {"n": 0}

    def create(**kwargs):
        resp = responses[state["n"]]
        state["n"] += 1
        return resp

    calls = _install_fake_openai(monkeypatch, create)

    result = provider.chat(_KWARGS, history, _TOOLS)

    assert state["n"] == 2
    assert "tools" in calls[0] and "tools" not in calls[1]
    assert calls[0]["messages"] == calls[1]["messages"]
    assert len(calls[1]["messages"]) == 4  # system/user/assistant(tool_calls)/tool 原样
    assert result["content"] == "检索结果为空，无相关公告。"


def test_chat_retry_once_when_retry_also_returns_empty(monkeypatch, caplog):
    """降级重试仍拿不到正文：只尝试两次即返回，且"重试仍为空"必须落
    warning 日志（区分端点整体异常与进程未更新的运维信号）。"""
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

    with caplog.at_level(logging.WARNING, logger="app.super_assistant.provider"):
        result = provider.chat(_KWARGS, _MESSAGES, _TOOLS)

    assert state["n"] == 2
    assert "tools" not in calls[1]
    assert result["content"] is None
    assert result["tool_calls"] == []
    assert result["usage"] == {"inputTokens": 0, "outputTokens": 15}
    assert any("降级重试后仍无正文" in r.message for r in caplog.records)
