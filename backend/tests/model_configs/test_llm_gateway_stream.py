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


def test_chat_stream_openai_streams_without_api_base(monkeypatch):
    """官方 OpenAI（无自建 api_base）同样真流式，不再回退一次性 chat。"""
    captured: dict = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured["create_kwargs"] = kwargs
            return iter([
                _chunk(content="你好"),
                _chunk(usage=NS(prompt_tokens=3, completion_tokens=2)),
            ])

    def fake_client(**kwargs):
        captured["client_kwargs"] = kwargs
        return NS(chat=NS(completions=FakeCompletions()))

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = fake_client
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    events = list(gw.chat_stream(
        {"provider": "openai", "api_key": "k", "model": "gpt-x",
         "model_config_id": None},
        [{"role": "user", "content": "hi"}], []))

    deltas = [e["delta"] for e in events if "delta" in e]
    assert deltas == ["你好"]
    final = next(e["final"] for e in events if "final" in e)
    assert final["content"] == "你好"
    assert final["usage"] == {"inputTokens": 3, "outputTokens": 2}
    assert "base_url" not in captured["client_kwargs"]


def test_chat_stream_unsupported_only_for_unknown_provider():
    """unsupported_stream 只保留给真正未知的 provider 类型。"""
    events = list(gw.chat_stream(
        {"provider": "mystery-llm", "api_key": "k", "model": "m"},
        [{"role": "user", "content": "hi"}], []))
    assert events == [{"unsupported_stream": True}]


# ---------------------------------------------------------------------------
# anthropic 真流式（messages stream）
# ---------------------------------------------------------------------------

_ANTHROPIC_KWARGS = {
    "provider": "anthropic", "api_key": "k", "model": "claude-x",
    "model_config_id": None, "timeout_seconds": 30, "max_output_tokens": 512,
}


def _block_start(index, block):
    return NS(type="content_block_start", index=index, content_block=block)


def _block_delta(index, delta):
    return NS(type="content_block_delta", index=index, delta=delta)


def _install_fake_anthropic(monkeypatch, events, final_message, captured=None):
    class FakeStream:
        def __iter__(self):
            return iter(events)

        def get_final_message(self):
            return final_message

    class FakeManager:
        def __enter__(self):
            return FakeStream()

        def __exit__(self, *args):
            return False

    class FakeMessages:
        def stream(self, **kwargs):
            if captured is not None:
                captured["stream_kwargs"] = kwargs
            return FakeManager()

    def fake_client(**kwargs):
        if captured is not None:
            captured["client_kwargs"] = kwargs
        return NS(messages=FakeMessages())

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = fake_client
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)


def test_chat_stream_anthropic_assembles_deltas_tool_calls_and_usage(monkeypatch):
    """anthropic 流式：delta 序列与 openai 分支一致；final 收口 usage/tool_calls。"""
    captured: dict = {}
    _install_fake_anthropic(monkeypatch, [
        _block_start(0, NS(type="text")),
        _block_delta(0, NS(type="text_delta", text="你")),
        _block_delta(0, NS(type="text_delta", text="好")),
        _block_start(1, NS(type="tool_use", id="toolu_1", name="upsert_elements")),
        _block_delta(1, NS(type="input_json_delta",
                           partial_json='{"kind": "object"')),
        _block_delta(1, NS(type="input_json_delta",
                           partial_json=', "elements": []}')),
        NS(type="message_stop"),
    ], NS(usage=NS(input_tokens=10, output_tokens=5), stop_reason="tool_use"),
        captured)

    events = list(gw.chat_stream(
        _ANTHROPIC_KWARGS, [{"role": "user", "content": "hi"}],
        [{"name": "upsert_elements", "description": "d", "parameters": {}}]))

    deltas = [e["delta"] for e in events if "delta" in e]
    assert deltas == ["你", "好"]
    final = next(e["final"] for e in events if "final" in e)
    assert final["content"] == "你好"
    assert final["tool_calls"] == [{
        "id": "toolu_1", "name": "upsert_elements",
        "arguments": {"kind": "object", "elements": []}}]
    assert final["usage"] == {"inputTokens": 10, "outputTokens": 5}
    # timeout / max_output_tokens 与 chat() 的 anthropic 分支一致 honor
    assert captured["client_kwargs"]["timeout"] == 30
    assert captured["stream_kwargs"]["max_tokens"] == 512
    assert captured["stream_kwargs"]["tools"] == [
        {"name": "upsert_elements", "description": "d", "input_schema": {}}]


def test_chat_stream_anthropic_filters_think_block_across_deltas(monkeypatch):
    """anthropic 流式 think 过滤与 openai 分支同语义：思考块不外发。"""
    _install_fake_anthropic(monkeypatch, [
        _block_start(0, NS(type="text")),
        _block_delta(0, NS(type="text_delta", text="<th")),
        _block_delta(0, NS(type="text_delta", text="ink>内部推理</th")),
        _block_delta(0, NS(type="text_delta", text="ink>正文")),
    ], NS(usage=None, stop_reason="end_turn"))

    events = list(gw.chat_stream(
        {"provider": "anthropic", "api_key": "k", "model": "m",
         "model_config_id": None},
        [{"role": "user", "content": "hi"}], []))

    deltas = "".join(e["delta"] for e in events if "delta" in e)
    final = next(e["final"] for e in events if "final" in e)
    assert deltas == "正文"
    assert "内部推理" not in deltas
    assert final["content"] == "正文"
    assert final["usage"] is None


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
