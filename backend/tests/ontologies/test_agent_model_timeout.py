"""Ontology model requests fail promptly without multiplying provider retries."""
from types import SimpleNamespace

import pytest

from app.ontologies.agent_runtime import llm_bridge
from app.ontologies.agent_runtime.turn_model import model_events


@pytest.mark.parametrize("streaming", [False, True])
def test_agent_model_timeout_policy_is_scoped_and_configurable(monkeypatch, streaming):
    calls = []
    kwargs = {"provider": "openai", "model": "m", "timeout_seconds": 120}
    execution = SimpleNamespace(stream_model=streaming, is_cancelled=lambda: False)
    final = {"content": "ok", "tool_calls": [], "usage": None}

    def chat(options, messages, tools):
        calls.append(dict(options))
        return final

    def stream(options, messages, tools):
        calls.append(dict(options))
        yield {"final": final}

    monkeypatch.setattr(llm_bridge, "chat", chat)
    monkeypatch.setattr(llm_bridge, "chat_stream", stream)
    assert list(model_events(kwargs, [{"role": "user", "content": "hi"}], [], execution))[-1] == {"final": final}
    assert calls[-1]["timeout_seconds"] == 30
    assert calls[-1]["retry_max_attempts"] == 1
    assert kwargs == {"provider": "openai", "model": "m", "timeout_seconds": 120}

    configured = {**kwargs, "ontology_assistant_timeout_seconds": 45}
    list(model_events(configured, [], [], execution))
    assert calls[-1]["timeout_seconds"] == 45
    assert "ontology_assistant_timeout_seconds" not in calls[-1]


@pytest.mark.parametrize("streaming", [False, True])
def test_agent_model_timeout_is_actionable_and_does_not_expose_provider_detail(monkeypatch, streaming):
    execution = SimpleNamespace(stream_model=streaming, is_cancelled=lambda: False)

    def fail(*args):
        raise llm_bridge.LLMError("request timed out: sensitive provider endpoint")

    def stream(*args):
        fail()
        yield

    monkeypatch.setattr(llm_bridge, "chat", fail)
    monkeypatch.setattr(llm_bridge, "chat_stream", stream)
    with pytest.raises(llm_bridge.LLMError) as error:
        list(model_events({"model": "m"}, [], [], execution))
    assert "30 秒" in str(error.value)
    assert "选择其他可用模型" in str(error.value)
    assert "sensitive" not in str(error.value)


def test_agent_model_timeout_uses_registered_environment_limit(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_TIMEOUT_SECONDS", "12")
    observed = []
    monkeypatch.setattr(llm_bridge, "chat", lambda kwargs, *_: observed.append(kwargs) or {})
    list(model_events({}, [], []))
    assert observed[0]["timeout_seconds"] == 12
    assert observed[0]["retry_max_attempts"] == 1


@pytest.mark.parametrize("streaming", [False, True])
def test_real_sdk_times_out_once_without_retrying(monkeypatch, streaming):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    release = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            release.wait(5)
            self.close_connection = True

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AGENT_LLM_TIMEOUT_SECONDS", "1")
    execution = SimpleNamespace(stream_model=streaming, is_cancelled=lambda: False)
    started = time.monotonic()
    try:
        with pytest.raises(llm_bridge.LLMError, match="1 秒内未响应"):
            list(model_events({
                "provider": "openai", "model": "fixture",
                "api_key": "fixture-key",
                "api_base": f"http://127.0.0.1:{server.server_port}/v1",
            }, [{"role": "user", "content": "hi"}], [], execution))
        assert time.monotonic() - started < 4
        assert requests == ["/v1/chat/completions"]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_provider_clients_receive_configured_timeout_and_no_sdk_retries():
    for factory in (llm_bridge._openai_client_kwargs, llm_bridge._anthropic_client_kwargs):
        kwargs = factory({"timeout_seconds": 30, "api_key": "fixture"})
        assert kwargs["timeout"] == 30
        assert kwargs["max_retries"] == 0
