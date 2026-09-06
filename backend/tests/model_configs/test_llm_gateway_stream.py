"""llm_gateway.chat_stream 回归：增量组装 / think 流式过滤 / 能力降级。"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace as NS

from app.model_configs import llm_gateway as gw


def _chunk(content=None, tool_calls=None, usage=None):
    choices = []
    if content is not None or tool_calls is not None:
        choices = [NS(delta=NS(content=content, tool_calls=tool_calls))]
    return NS(choices=choices, usage=usage)


def _install_fake_openai(monkeypatch, chunks):
    class FakeCompletions:
        def create(self, **kwargs):
            assert kwargs.get("stream") is True
            return iter(chunks)

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = lambda **kw: NS(chat=NS(completions=FakeCompletions()))
    monkeypatch.setitem(sys.modules, "openai", fake_openai)


_STREAM_KWARGS = {
    "provider": "openai", "api_base": "http://fake/v1", "api_key": "k",
    "model": "m", "model_config_id": None,
}


def test_chat_stream_assembles_deltas_tool_calls_and_usage(monkeypatch):
    """text_delta 增量、tool_calls 分片拼装、usage 收集、final 收口。"""
    _install_fake_openai(monkeypatch, [
        _chunk(content="你"),
        _chunk(content="好"),
        _chunk(tool_calls=[NS(index=0, id="call_1", function=NS(
            name="upsert_elements", arguments='{"kind": "object"'))]),
        _chunk(tool_calls=[NS(index=0, function=NS(
            name="", arguments=', "elements": []}'))]),
        _chunk(usage=NS(prompt_tokens=10, completion_tokens=5)),
    ])

    events = list(gw.chat_stream(
        _STREAM_KWARGS, [{"role": "user", "content": "hi"}], []))

    deltas = [e["delta"] for e in events if "delta" in e]
    assert deltas == ["你", "好"]
    final = next(e["final"] for e in events if "final" in e)
    assert final["content"] == "你好"
    assert final["tool_calls"] == [{
        "id": "call_1", "name": "upsert_elements",
        "arguments": {"kind": "object", "elements": []}}]
    assert final["usage"] == {"inputTokens": 10, "outputTokens": 5}


def test_chat_stream_filters_think_block_across_deltas(monkeypatch):
    """think 块跨 delta 拆分时不得外发思考内容，正文增量照常流出。"""
    _install_fake_openai(monkeypatch, [
        _chunk(content="<th"),
        _chunk(content="ink>内部推理</th"),
        _chunk(content="ink>正文开"),
        _chunk(content="始"),
        _chunk(usage=None),
    ])

    events = list(gw.chat_stream(
        _STREAM_KWARGS, [{"role": "user", "content": "hi"}], []))
    deltas = "".join(e["delta"] for e in events if "delta" in e)
    final = next(e["final"] for e in events if "final" in e)
    assert deltas == "正文开始"
    assert "内部推理" not in deltas
    # final 与非流式语义一致：完整内容经 strip_think 清洗
    assert final["content"] == "正文开始"


def test_chat_stream_unsupported_without_api_base(monkeypatch):
    """无自建 api_base（托管端点/测试桩）→ 产出 unsupported 信号而非真流式。"""
    events = list(gw.chat_stream(
        {"provider": "openai", "api_key": "k", "model": "m"},
        [{"role": "user", "content": "hi"}], []))
    assert events == [{"unsupported_stream": True}]
    # anthropic 同样走非流式路径
    events2 = list(gw.chat_stream(
        {**_STREAM_KWARGS, "provider": "anthropic"},
        [{"role": "user", "content": "hi"}], []))
    assert events2 == [{"unsupported_stream": True}]


def test_filter_think_deltas_holds_partial_tags():
    """纯过滤器：开头 think 块跨 delta 拆分时不泄漏思考内容；正文开头非标签则原样放行。"""
    # 场景 1：正文以 <think> 开头（跨 delta 拆成 "<th"/"ink>"），只放行闭标签后的正文
    state: dict = {}
    out: list[str] = []
    for piece in ["<th", "ink>内部推理</th", "ink>正文开", "始"]:
        out.extend(gw._filter_think_deltas(piece, state))
    assert "".join(out) == "正文开始"

    # 场景 2：正文开头是普通文本（含"<"字符但非标签）→ 全部放行
    state2: dict = {}
    out2: list[str] = []
    for piece in ["结论 a<b ", "继续"]:
        out2.extend(gw._filter_think_deltas(piece, state2))
    assert "".join(out2) == "结论 a<b 继续"

    # 场景 3：无 think 的普通文本原样通过
    state3: dict = {}
    assert gw._filter_think_deltas("普通文本。", state3) == ["普通文本。"]
