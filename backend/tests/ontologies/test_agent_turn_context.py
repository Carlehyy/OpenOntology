"""Context budgets preserve query facts and model/tool protocol boundaries."""
from types import SimpleNamespace
import json

import pytest

from tests.ontologies.test_agent_unified_query import orders_ontology  # noqa: F401
from app.ontologies.agent_runtime import context_policy, turn_model


def test_catalog_discovers_only_visible_properties_without_counting():
    item = SimpleNamespace(id="orders", name="Order", display_name="订单",
                           properties=[{"name": "number", "type": "string"},
                                       {"name": "private", "type": "string"}])
    scope = SimpleNamespace(ontology=SimpleNamespace(name="订单本体"), release_id="release-1",
                            object_types={"orders": item},
                            allowed_property_names=lambda _: {"number"})
    original = context_policy.remote_object_type_ids
    try:
        context_policy.remote_object_type_ids = lambda _: set()
        text = context_policy.ontology_catalog(scope)
    finally:
        context_policy.remote_object_type_ids = original
    assert "number:string" in text
    assert "private" not in text
    assert "describe_objects" in text
    assert "实例数=" not in text


def test_tool_preview_remains_json_and_keeps_completeness_separate():
    result = {"rows": [{"name": "订单" * 200, "amount": i} for i in range(50)],
              "totalRowCount": 50, "exact": True, "partial": False,
              "completeness": "complete", "hasNext": False}
    payload = context_policy.tool_payload(result, cap=1200)
    preview = json.loads(payload)
    assert len(payload) <= 1200
    assert preview["llmPreviewTruncated"] is True
    assert preview["partial"] is False
    assert preview["exact"] is True
    assert result["rows"][49]["amount"] == 49


def test_history_budget_drops_whole_older_user_turn():
    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "old" * 200},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "old-call"}]},
        {"role": "tool", "tool_call_id": "old-call", "content": "old result"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "current-call"}]},
        {"role": "tool", "tool_call_id": "current-call", "content": "current result"},
    ]
    bounded = context_policy.budget_messages(messages, cap=500)
    assert [message.get("tool_call_id") for message in bounded
            if message["role"] == "tool"] == ["current-call"]
    assert bounded[1]["content"] == "current question"
    assert len(messages) == 8


def test_streamed_model_keeps_delta_and_tool_final(monkeypatch):
    final = {"content": None, "tool_calls": [{"name": "query_objects", "id": "t1"}],
             "usage": {"inputTokens": 7, "outputTokens": 3}}
    def stream(*_):
        yield {"delta": "正在查询"}
        yield {"final": final}
    monkeypatch.setattr(turn_model.llm_bridge, "chat_stream", stream)
    execution = SimpleNamespace(is_cancelled=lambda: False, stream_model=True)
    events = list(turn_model.model_events({}, [{"role": "user", "content": "查订单"}], [], execution))
    assert events == [{"type": "delta", "content": "", "reset": True},
                      {"type": "delta", "content": "正在查询"}, {"final": final}]


def test_cancelled_model_stream_is_closed_and_emits_no_final(monkeypatch):
    state = {"cancelled": False, "closed": False}
    def stream(*_):
        try:
            yield {"delta": "部分内容"}
            state["cancelled"] = True
            yield {"delta": "不可显示"}
            yield {"final": {"content": "不可执行", "tool_calls": []}}
        finally:
            state["closed"] = True
    monkeypatch.setattr(turn_model.llm_bridge, "chat_stream", stream)
    execution = SimpleNamespace(is_cancelled=lambda: state["cancelled"], stream_model=True)
    events = list(turn_model.model_events({}, [], [], execution))
    assert state["closed"] is True
    assert not any("final" in event for event in events)
    assert [event["content"] for event in events] == ["", "部分内容"]


@pytest.mark.parametrize("completion", ["answer", "error", "disconnect"])
def test_turn_exit_releases_unread_query_pages(
        db, admin_user, orders_ontology, monkeypatch, completion):
    from app.ontologies.agent_query.models import AgentQuerySnapshot
    from app.ontologies.agent_runtime import orchestrator

    monkeypatch.setattr(orchestrator, "select_llm_model_config",
                        lambda *_args, **_kwargs: SimpleNamespace(id="fixture-model"))
    monkeypatch.setattr(orchestrator, "llm_call_kwargs",
                        lambda _cfg: {"model": "fixture-model"})
    calls = 0

    def model(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"content": None, "tool_calls": [{
                "id": "query-first", "name": "query_objects", "arguments": {
                    "object_type": "订单", "select": ["order_no"], "page_size": 1,
                },
            }]}
        if completion == "error":
            raise turn_model.llm_bridge.LLMError("fixture connection failure")
        return {"content": "已完成查询。", "tool_calls": []}

    monkeypatch.setattr(turn_model.llm_bridge, "chat", model)
    events = orchestrator.run_agent_turn(
        db, orders_ontology["ontologyId"], admin_user, "查看订单明细",
        release_id=orders_ontology["releaseId"])
    seen = []
    for event in events:
        seen.append(event)
        if event["type"] == "step":
            assert event["result"]["hasNext"] is True
            assert db.query(AgentQuerySnapshot).count() == 1
            if completion == "disconnect":
                events.close()
                break

    assert db.query(AgentQuerySnapshot).count() == 0
    if completion != "disconnect":
        assert any(event["type"] == completion for event in seen)
