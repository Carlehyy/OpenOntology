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
                            allowed_property_names=lambda _: {"number"},
                            queryable_property_names=lambda _: None)
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


def test_tool_preview_strips_plan_drilldown_and_chart_before_rows():
    result = {
        "objectType": "订单", "metric": "count", "groupBy": "客户",
        "groups": [{"group": f"客户{i:02d}", "value": i} for i in range(10)],
        "rows": [{"客户": "客户01",
                  "chart": "业务图表属性",
                  "html": "业务HTML属性",
                  "querySpec": "业务计划属性",
                  "drilldown": {"token": "signed-token"}}],
        "querySpec": {"query": {"where": [{"value": 99}]}},
        "chart": {"data": [{"label": f"客户{i:02d}", "value": i} for i in range(10)]},
        "totalGroups": 10,
    }
    preview = json.loads(context_policy.tool_payload(result, cap=2000))
    assert preview["groups"] == result["groups"]
    assert "llmPreviewTruncated" not in preview
    assert "querySpec" not in preview and "chart" not in preview
    assert "drilldown" not in preview["rows"][0]
    assert preview["rows"][0]["chart"] == "业务图表属性"
    assert preview["rows"][0]["html"] == "业务HTML属性"
    assert preview["rows"][0]["querySpec"] == "业务计划属性"


def test_window_distinct_preview_strips_sample_drilldown_not_business_fields():
    result = {
        "kind": "sequence_analysis", "mode": "window_distinct",
        "objectType": "事件", "windowMinutes": 30,
        "windowBucket": "half_hour", "windowTimezone": "Asia/Shanghai",
        "qualifyingWindows": 12, "qualifyingModules": 3, "moduleCount": 3,
        "modules": [{"module": "m1", "qualifyingWindows": 4,
                     "maxDistinctSubjects": 7}],
        "sampleWindows": [
            {"module": "m1", "window": "2026-09-30T16:00:00Z", "users": 7,
             "drilldown": {"token": "signed-token",
                           "description": {"kind": "detail_rows"}}},
            {"module": "m2", "users": 5, "drilldown": "业务字符串"},
            {"module": "m3", "users": 4, "drilldown": {"business": "值"}},
        ],
        "querySpec": {"shape": "objects"},
        "querySpecHash": "primary-hash",
        "moduleQuerySpec": {"shape": "objects", "sources": [{"objectTypeId": "x"}]},
        "moduleQuerySpecHash": "module-hash",
        "exact": True, "partial": False, "completeness": "complete",
    }
    preview = json.loads(context_policy.tool_payload(result, cap=4000))
    assert "drilldown" not in preview["sampleWindows"][0]
    assert preview["sampleWindows"][1]["drilldown"] == "业务字符串"
    assert preview["sampleWindows"][2]["drilldown"] == {"business": "值"}
    assert "querySpec" not in preview and "querySpecHash" not in preview
    assert "moduleQuerySpec" not in preview
    assert "moduleQuerySpecHash" not in preview
    assert preview["qualifyingWindows"] == 12
    assert preview["qualifyingModules"] == 3
    assert preview["moduleCount"] == 3
    assert preview["windowMinutes"] == 30
    assert preview["windowBucket"] == "half_hour"
    assert preview["windowTimezone"] == "Asia/Shanghai"


def test_window_distinct_compressed_metadata_protects_module_top10():
    modules = [{"module": f"module-{index:02d}",
                "qualifyingWindows": index,
                "maxDistinctSubjects": index + 1} for index in range(20)]
    samples = [{"module": f"module-{index:02d}",
                "window": f"2026-09-30T{16 + index // 2:02d}:{index % 2 * 30:02d}:00Z",
                "users": index + 1, "detail": "样本" * 80,
                "drilldown": {"token": "signed-token"}} for index in range(20)]
    result = {
        "kind": "sequence_analysis", "mode": "window_distinct",
        "objectType": "事件", "threshold": 5,
        "thresholdScope": "distinct_subjects_per_fixed_window",
        "windowMinutes": 30, "windowBucket": "half_hour",
        "windowTimezone": "Asia/Shanghai", "qualifyingWindows": 24,
        "qualifyingModules": 20, "moduleCount": 20, "moduleLimit": 200,
        "modules": modules, "modulesTruncated": True,
        "sampleWindows": samples, "sampleWindowsTruncated": True,
        "evaluatedWindows": None, "evaluatedWindowsAvailable": False,
        "limitations": ["evaluated_windows_not_counted"],
        "moduleQuerySpec": {"query": {"sources": [{"objectTypeId": "x"}]}},
        "moduleQuerySpecHash": "module-hash",
        "exact": True, "partial": False, "completeness": "complete",
    }
    payload = context_policy.tool_payload(result, cap=1400)
    preview = json.loads(payload)
    assert len(payload) <= 1400
    assert preview["llmPreviewOmitted"] is True
    assert len(preview["modules"]) == 10
    assert preview["modules"][0]["module"] == "module-00"
    assert preview["qualifyingWindows"] == 24
    assert preview["qualifyingModules"] == 20
    assert preview["moduleCount"] == 20
    assert preview["windowMinutes"] == 30
    assert preview["windowBucket"] == "half_hour"
    assert preview["windowTimezone"] == "Asia/Shanghai"
    assert preview["modulesTruncated"] is True
    assert preview["sampleWindowsTruncated"] is True
    assert preview["threshold"] == 5
    assert preview["evaluatedWindowsAvailable"] is False
    assert preview["limitations"] == ["evaluated_windows_not_counted"]
    assert "moduleQuerySpec" not in preview
    assert "moduleQuerySpecHash" not in preview


def test_evidence_digest_uses_safe_window_preview():
    from app.ontologies.agent_runtime import answer_evidence

    result = {
        "kind": "sequence_analysis", "mode": "window_distinct",
        "objectType": "事件", "windowMinutes": 30,
        "windowBucket": "half_hour", "windowTimezone": "Asia/Shanghai",
        "qualifyingWindows": 12, "qualifyingModules": 3, "moduleCount": 3,
        "modules": [{"module": "m1", "qualifyingWindows": 4}],
        "sampleWindows": [
            {"module": "m1", "users": 7,
             "drilldown": {"token": "signed-token"}},
            {"module": "m2", "users": 5, "drilldown": "业务字符串"},
        ],
        "moduleQuerySpec": {"shape": "objects"},
        "moduleQuerySpecHash": "module-hash",
        "exact": True, "partial": False, "completeness": "complete",
    }
    digest = answer_evidence.evidence_digest(
        [{"tool": "analyze_sequences", "result": result}],
        title="窗口证据", cap=4000)
    assert "signed-token" not in digest
    assert "业务字符串" in digest
    assert "moduleQuerySpec" not in digest
    assert "qualifyingWindows" in digest


def test_tool_preview_keeps_monthly_twelve_and_reports_omissions():
    months = [{"group": f"2026-{month:02d}", "value": month}
              for month in range(1, 13)]
    payload = context_policy.tool_payload({
        "objectType": "订单", "groupBy": "创建时间(月)", "groupBuckets": ["month"],
        "groups": months, "totalGroups": 12,
    }, cap=2000)
    preview = json.loads(payload)
    assert preview["groups"] == months
    assert "llmPreviewTruncated" not in preview

    overflow = context_policy.tool_payload({
        "objectType": "订单", "groupBy": "客户",
        "groups": [{"group": f"客户{i:02d}", "value": i} for i in range(40)],
        "totalGroups": 40, "hasNext": True, "cursor": "cursor-token",
    }, cap=1000)
    omitted = json.loads(overflow)
    assert len(omitted["groups"]) == 10
    assert omitted["omittedItems"] == {"groups": 30}
    assert omitted["llmPreviewOmitted"] is True
    assert omitted["cursor"] == "cursor-token"
    assert len(overflow) <= 1000


def test_tool_preview_never_silently_truncates_pagination_cursor():
    cursor = "c" * 800
    payload = context_policy.tool_payload({
        "objectType": "订单", "groupBy": "客户",
        "groups": [{"group": f"客户{i:02d}", "value": i} for i in range(40)],
        "totalGroups": 40, "hasNext": True, "cursor": cursor,
    }, cap=1000)
    preview = json.loads(payload)
    assert len(payload) <= 1000
    assert preview.get("cursor") == cursor or preview.get("cursorOmitted") is True
    assert preview.get("cursor") != cursor[:512]


def test_history_budget_never_splits_tool_call_pairs():
    rows = [{"name": "订单", "amount": index} for index in range(40)]
    tool_content = json.dumps({"rows": rows, "hasNext": True,
                               "cursor": "cursor-token"}, ensure_ascii=False)
    messages = [
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call-1", "name": "query_objects"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": tool_content},
    ]
    bounded = context_policy.budget_messages(messages, cap=900)
    assert [message["role"] for message in bounded] == ["user", "assistant", "tool"]
    assert bounded[1]["tool_calls"][0]["id"] == "call-1"
    assert bounded[2]["tool_call_id"] == "call-1"
    assert json.loads(bounded[2]["content"])["cursor"] == "cursor-token"


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



def test_prompt_requires_composite_coverage_distinct_rankings_and_time_limits(monkeypatch):
    from app.ontologies.agent_runtime.prompting import _system_prompt
    monkeypatch.setattr(context_policy, "remote_object_type_ids", lambda _: set())
    scope = SimpleNamespace(ontology=SimpleNamespace(name="目录"), release_id="r1",
                            release=None, object_types={}, profile=SimpleNamespace(system_prompt_extra=""),
                            allowed_property_names=lambda _: None,
                            queryable_property_names=lambda _: None)
    text = _system_prompt(scope)
    assert "全部子问题" in text and "逐段回答" in text and "缺失项" in text
    assert "最高月 Top10" in text and "全年 Top10" in text
    assert "缺少的数据及工具限制" in text and "实际时间范围及数据截止日期" in text
    assert "未完成、截断或超预算" in text and "步数预算内继续查询" in text
