"""A partial mapping must not turn a missing join into a business fact."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.assistant_hub.adapters.ontology_agent import OntologyAgentAdapter
from app.assistant_hub.contract import TurnResult, build_ref
from app.ontologies.agent_query import remote_query, service as query_service
from app.ontologies.agent_runtime import llm_bridge, query_window, run_service
from app.ontologies.agent_runtime.boundary import build_scope
from app.ontologies.agent_runtime.models import AgentMessage
from app.ontologies.agent_runtime.partial_answer import TurnCoverage
from app.ontologies.agent_runtime.query_coverage import coverage_gap, partial_coverage
from app.ontologies.agent_runtime.schemas import ChatRequest
from app.ontologies.agent_runtime.toolkit import ToolRunner
from tests.conftest import TestSession
from tests.ontologies.test_agent_partial_mapping_tools import _freeze_with_dataless_structure
from tests.ontologies.test_agent_remote_aggregation import _SequencePort, _unified_result
from tests.ontologies.test_agent_run_ledger import _add_llm_config
from tests.ontologies.test_agent_runtime import modeled_ontology  # noqa: F401
from tests.ontologies.test_remote_query_declared_relation import _publish


def _group_call():
    return {"id": "join", "name": "query_objects", "arguments": {
        "object_type": "Order",
        "joins": [{"object_type": "Supplier", "link_type": "order_backup_supplier"}],
        "group_by": [{"object_type": "Supplier", "property": "sname"}],
        "aggregates": [{"fn": "count"}],
    }}


def _count_call(**args):
    return {"id": "total", "name": "aggregate_objects", "arguments": {
        "object_type": "Order", "metric": "count", **args,
    }}


def _reply(calls=(), content=None):
    return {"tool_calls": list(calls), "content": content, "usage": {}}


def _patch_model(monkeypatch, *responses):
    # Patch only the model boundary. Tool dispatch, queries and run persistence
    # all execute their production code against the isolated test database.
    remaining = iter(responses)

    def chat(*args):
        return deepcopy(next(remaining))

    def stream(*args):
        result = chat(*args)
        if result["content"]:
            yield {"delta": result["content"]}
        yield {"final": result}

    monkeypatch.setattr(llm_bridge, "chat", chat)
    monkeypatch.setattr(llm_bridge, "chat_stream", stream)


@pytest.mark.parametrize("question,show_total", [
    ("订单总数，并按供应商拆分", True),
    ("只按供应商统计订单", False),
    ("订单总数，并且只算有供应商的", False),
])
def test_native_run_replay_and_hub_report_only_independent_facts(
        db, admin_user, modeled_ontology, monkeypatch, question, show_total):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_dataless_structure(db, oid)
    _add_llm_config(db, admin_user, "partial-mapping")
    grouped = _group_call()
    duplicate = {**grouped, "id": "duplicate", "arguments": dict(
        reversed(list(grouped["arguments"].items())))}
    draft = "共有 2 条，没有任何供应商关联记录。"
    _patch_model(monkeypatch,
                 _reply([_count_call(), grouped], draft),
                 _reply([duplicate], draft),
                 _reply(content=draft))
    tool_calls, queries = [], []
    real_run = ToolRunner.run
    real_execute = query_service.execute_unified_query

    def track_tool(self, name, args):
        tool_calls.append(name)
        return real_run(self, name, args)

    def track_query(*args, **kwargs):
        queries.append(args[2])
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(ToolRunner, "run", track_tool)
    monkeypatch.setattr(query_service, "execute_unified_query", track_query)
    run = run_service.submit_run(db, oid, admin_user, ChatRequest(
        message=question, release_id=release_id, stream=True), dispatch=False)
    run_service.execute_run(run["runId"], db=db, session_factory=TestSession)
    db.expire_all()
    assert sorted(tool_calls) == ["aggregate_objects", "query_objects"]
    assert queries == []  # Missing relationship never reaches the query execution entry.

    events = list(run_service.stream_run_events(db, oid, admin_user, run["runId"]))
    assert str(events[-1]["status"]) == "succeeded", events
    steps = [event for event in events if event["type"] == "step"]
    blocked = [event for event in steps if event["tool"] == "query_objects"]
    assert len(blocked) == 2
    assert blocked[1]["durationMs"] == 0
    for event in blocked:
        assert event["result"]["dataCoverage"]["gaps"][0]["code"] == "relation_data_unmapped"
    assert all(not event.get("content") for event in events if event["type"] == "delta")
    [answer] = [event for event in events if event["type"] == "answer"]
    assert ("订单总数为 2 条" in answer["content"]) is show_total
    assert "备选供应商" in answer["content"] and "未知" in answer["content"]
    assert "没有任何供应商关联记录" not in answer["content"]
    assert "0 条" not in answer["content"]
    [stored] = db.query(AgentMessage).filter_by(
        conversation_id=run["conversationId"], role="assistant").all()
    assert stored.content == answer["content"]

    cursor = blocked[0]["seq"]
    replay = list(run_service.stream_run_events(
        db, oid, admin_user, run["runId"], after_seq=cursor))
    assert replay[1:] == [event for event in events[1:] if event["seq"] > cursor]
    assert replay[0]["snapshot"]["content"] == answer["content"]

    _patch_model(monkeypatch, _reply([_count_call(), grouped]), _reply(content=draft))
    ref = build_ref("ontology_agent", {"ontology_id": oid, "conversation_id": run["conversationId"]})
    delegated = list(OntologyAgentAdapter().run_turn(db, admin_user, ref, question))
    assert isinstance(delegated[-1], TurnResult)
    assert delegated[-1].content == answer["content"]


def test_source_outage_retries_the_same_join_filter_group_and_window(db, admin_user, monkeypatch):
    model = _publish(db, admin_user)
    _add_llm_config(db, admin_user, "coverage-retry")
    clock = datetime(2026, 11, 24, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(query_window, "utc_now", lambda: clock)
    call = {"id": "first", "name": "query_objects", "arguments": {
        "object_type": "orders",
        "joins": [{"object_type": "customers", "link_type": "placed_by"}],
        "filters": [{"property": "note", "op": "eq", "value": "待核对"}],
        "group_by": [{"object_type": "customers", "property": "name"}],
        "aggregates": [{"fn": "count"}],
        "window": {"property": "placed_at", "timezone": "UTC",
                   "last": {"unit": "day", "count": 1}},
    }}
    answer_text = "客户甲有 4 条待核对订单。"
    _patch_model(monkeypatch,
                 _reply([call], "没有匹配的订单。"),
                 _reply([{**call, "id": "retry"}]),
                 _reply(content=answer_text))

    port = _SequencePort([_unified_result([{"g0": "客户甲", "a0": 4}])])
    execute = port.execute

    def execute_after_outage(db, user, plan, **context):
        nonlocal clock
        result = execute(db, user, plan, **context)
        if len(port.executed) == 1:
            clock += timedelta(minutes=5)
            raise HTTPException(503, {"code": "SOURCE_UNAVAILABLE",
                                      "message": "synthetic physical-source diagnostic"})
        return {**result, "releases": plan["releases"],
                "persistence": {"mode": "ephemeral", "classification": "aggregate"}}

    monkeypatch.setattr(port, "execute", execute_after_outage)
    monkeypatch.setattr(remote_query, "_port", port)
    run = run_service.submit_run(db, model.ontology_id, admin_user, ChatRequest(
        message="按客户统计最近一天的待核对订单", release_id=model.release_id, stream=True),
        dispatch=False)
    run_service.execute_run(run["runId"], db=db, session_factory=TestSession)
    db.expire_all()

    first, retry = port.executed
    plan = first["plan"]
    assert plan == retry["plan"]
    assert first["session_id"] == retry["session_id"] == run["conversationId"]
    assert [(join["leftProperty"], join["rightProperty"]) for join in plan["joins"]] == [
        ("customer", "key")]
    assert plan["groupBy"] == [{"source": "s1", "property": "name"}]
    assert plan["where"] == [
        {"source": "s0", "property": "note", "op": "eq", "value": "待核对"},
        {"source": "s0", "property": "placed_at", "op": "gte",
         "value": "2026-11-23T09:00:00+00:00"},
        {"source": "s0", "property": "placed_at", "op": "lt",
         "value": "2026-11-24T09:00:00+00:00"},
    ]

    events = list(run_service.stream_run_events(db, model.ontology_id, admin_user, run["runId"]))
    assert str(events[-1]["status"]) == "succeeded", events
    failed, recovered = [event for event in events if event["type"] == "step"]
    assert failed["result"]["dataCoverage"]["gaps"][0]["code"] == "SOURCE_UNAVAILABLE"
    assert "synthetic physical-source diagnostic" not in repr(events)
    assert recovered["result"]["rows"][0]["count"] == 4
    # Short answers can reach the terminal event before the delta checkpoint.
    assert all(e["content"] == answer_text for e in events
               if e["type"] == "delta" and e.get("content"))
    [answer] = [event for event in events if event["type"] == "answer"]
    assert answer["content"] == answer_text
    [stored] = db.query(AgentMessage).filter_by(
        conversation_id=run["conversationId"], role="assistant").all()
    assert stored.content == answer_text
    replay = list(run_service.stream_run_events(
        db, model.ontology_id, admin_user, run["runId"], after_seq=failed["seq"]))
    assert replay[0]["snapshot"]["content"] == answer_text
    assert replay[1:] == [event for event in events[1:] if event["seq"] > failed["seq"]]


def test_covered_join_with_no_matches_can_answer_zero(db, admin_user, modeled_ontology, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_dataless_structure(db, oid)
    _add_llm_config(db, admin_user, "covered-zero")
    call = {"id": "count", "name": "query_objects", "arguments": {
        "object_type": "Order",
        "joins": [{"object_type": "Supplier", "link_type": "order_supplier"}],
        "filters": [{"property": "order_no", "op": "eq", "value": "SO-NONE"}],
        "aggregates": [{"fn": "count"}],
    }}
    answer_text = "该订单号下有供应商关联的订单为 0 条。"
    _patch_model(monkeypatch, _reply([call]), _reply(content=answer_text))
    run = run_service.submit_run(db, oid, admin_user, ChatRequest(
        message="订单 SO-NONE 有几条记录关联了供应商？", release_id=release_id, stream=True),
        dispatch=False)
    run_service.execute_run(run["runId"], db=db, session_factory=TestSession)
    db.expire_all()

    events = list(run_service.stream_run_events(db, oid, admin_user, run["runId"]))
    assert str(events[-1]["status"]) == "succeeded", events
    [step] = [event["result"] for event in events if event["type"] == "step"]
    assert step["rows"] == [{"count": 0}]
    assert step["exact"] is True and step["completeness"] == "complete"
    assert not step.get("dataCoverage", {}).get("gaps")
    [answer] = [event for event in events if event["type"] == "answer"]
    assert answer["content"] == answer_text
    assert answer["verification"]["passed"] is True


def test_later_complete_success_must_preserve_query_scope(db, modeled_ontology):
    _, _, scope = build_scope(db, modeled_ontology["id"])
    group = _group_call()
    coverage = TurnCoverage(scope, "只按供应商统计订单")
    source_failure = {"error": "source unavailable", **partial_coverage([
        coverage_gap("SOURCE_UNAVAILABLE")])}
    coverage.observe(group, source_failure)
    coverage.observe(_count_call(), {"metric": "count", "value": 2, "exact": True})
    assert coverage.pending and "2 条" not in coverage.render()

    changed = deepcopy(group)
    changed["arguments"]["filters"] = [{"property": "status", "op": "eq", "value": "paid"}]
    coverage.observe(changed, {"rows": [{"count": 1}], "exact": True})
    assert coverage.pending
    coverage.observe(group, {"rows": [{"count": 2}], "exact": True, "hasNext": True})
    assert coverage.pending
    retry = {**group, "id": "retry", "arguments": {**group["arguments"], "page_size": 5}}
    coverage.observe(retry, {"rows": [{"count": 2}], "exact": True})
    assert not coverage.pending
