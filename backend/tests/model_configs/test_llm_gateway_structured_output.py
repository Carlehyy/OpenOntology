"""llm_gateway.chat 结构化输出透传扩展（Hindsight 记忆网关消费）。

覆盖：可选 keyword-only 参数（response_format / temperature / max_tokens /
max_completion_tokens / seed / top_p）原样送达上游 create；请求上限与模型
配置上限取更紧者；return_finish_reason 透传上游截断信号（length 不许在
网关层被改写）；默认路径（不传可选参数）与历史行为逐字段一致；anthropic
provider 显式报错而非默默丢字段。openai SDK 全部打桩，无真实请求。
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace as NS

from app.model_configs import llm_gateway as gw


def _install_fake_openai(monkeypatch, create_impl):
    calls: list[dict] = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return create_impl(**kwargs)

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = lambda **kw: NS(chat=NS(completions=FakeCompletions()))
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    return calls


def _resp(content='{"ok": true}', finish_reason="stop", prompt=10, completion=4):
    return NS(choices=[NS(message=NS(role="assistant", content=content,
                                     tool_calls=None),
                          finish_reason=finish_reason)],
              usage=NS(prompt_tokens=prompt, completion_tokens=completion))


_KWARGS = {"provider": "compatible", "api_base": "http://fake/v1", "api_key": "k",
           "model": "m", "model_config_id": None}
_MESSAGES = [{"role": "user", "content": "提取记忆"}]
_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "response",
        "strict": True,
        "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
    },
}


def test_default_call_kwargs_unchanged(monkeypatch):
    """不传可选参数：create 只含历史字段（temperature=0.2，无 response_format）。"""
    calls = _install_fake_openai(monkeypatch, lambda **kw: _resp())
    result = gw.chat(dict(_KWARGS), _MESSAGES, [])
    assert calls[0] == {
        "model": "m",
        "messages": [{"role": "user", "content": "提取记忆"}],
        "temperature": 0.2,
    }
    assert result == {"content": '{"ok": true}', "tool_calls": [],
                      "usage": {"inputTokens": 10, "outputTokens": 4}}


def test_structured_passthrough_fields_verbatim(monkeypatch):
    calls = _install_fake_openai(monkeypatch, lambda **kw: _resp())
    gw.chat(dict(_KWARGS), _MESSAGES, [],
            response_format=_JSON_SCHEMA, temperature=0.7,
            max_tokens=512, seed=4242, top_p=0.9,
            return_finish_reason=True)
    assert calls[0]["response_format"] == _JSON_SCHEMA
    assert calls[0]["temperature"] == 0.7
    assert calls[0]["max_tokens"] == 512
    assert calls[0]["seed"] == 4242
    assert calls[0]["top_p"] == 0.9


def test_max_completion_tokens_keeps_own_param_name(monkeypatch):
    """Hindsight 对推理模型发 max_completion_tokens：键名必须原样保留。"""
    calls = _install_fake_openai(monkeypatch, lambda **kw: _resp())
    gw.chat(dict(_KWARGS), _MESSAGES, [], max_completion_tokens=1024)
    assert calls[0]["max_completion_tokens"] == 1024
    assert "max_tokens" not in calls[0]


def test_request_max_tokens_clamped_by_config_cap(monkeypatch):
    calls = _install_fake_openai(monkeypatch, lambda **kw: _resp())
    kwargs = dict(_KWARGS, max_output_tokens=128)
    gw.chat(kwargs, _MESSAGES, [], max_tokens=512)
    assert calls[0]["max_tokens"] == 128
    gw.chat(kwargs, _MESSAGES, [], max_tokens=64)
    assert calls[-1]["max_tokens"] == 64


def test_config_cap_still_applied_without_request_cap(monkeypatch):
    """历史行为：模型配置 max_output_tokens 在请求未声明上限时照旧生效。"""
    calls = _install_fake_openai(monkeypatch, lambda **kw: _resp())
    gw.chat(dict(_KWARGS, max_output_tokens=256), _MESSAGES, [],
            response_format={"type": "json_object"}, temperature=0.0)
    assert calls[0]["max_tokens"] == 256


def test_finish_reason_passthrough_preserves_length(monkeypatch):
    """上游截断信号 length 必须原样返回，禁止伪装 stop。"""
    _install_fake_openai(monkeypatch, lambda **kw: _resp(finish_reason="length"))
    result = gw.chat(dict(_KWARGS), _MESSAGES, [], return_finish_reason=True)
    assert result["finish_reason"] == "length"


def test_finish_reason_absent_by_default(monkeypatch):
    _install_fake_openai(monkeypatch, lambda **kw: _resp())
    result = gw.chat(dict(_KWARGS), _MESSAGES, [])
    assert "finish_reason" not in result


def test_finish_reason_missing_upstream_returns_none(monkeypatch):
    _install_fake_openai(monkeypatch, lambda **kw: _resp(finish_reason=None))
    result = gw.chat(dict(_KWARGS), _MESSAGES, [], return_finish_reason=True)
    assert result["finish_reason"] is None


def test_structured_params_rejected_for_anthropic():
    """anthropic 分支不消费透传参数：显式报错，绝不默默丢字段。"""
    import pytest
    with pytest.raises(ValueError):
        gw.chat({**_KWARGS, "provider": "anthropic"}, _MESSAGES, [],
                response_format=_JSON_SCHEMA)
    with pytest.raises(ValueError):
        gw.chat({**_KWARGS, "provider": "anthropic"}, _MESSAGES, [],
                return_finish_reason=True)
