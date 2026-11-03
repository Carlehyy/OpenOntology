"""报告查询面扩展：query_objects 明细/相对窗口可进模板，且参数统一校验。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ontologies.agent_runtime import query_window
from app.ontologies.agent_runtime.models import AnalysisReportTemplate
from app.ontologies.agent_runtime.query_spec import public_query_result
from app.ontologies.agent_runtime.reporting import (
    execute_report, normalize_sections,
)
from tests.ontologies.test_agent_query_view import (  # noqa: F401
    events_ontology,
)

FIXED_NOW = datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(query_window, "utc_now", lambda: FIXED_NOW)


def _sections():
    return [
        {
            "id": "window-detail",
            "title": "近一日登记明细",
            "goal": "核对最近一日登记的明细记录，确认窗口边界与字段口径。",
            "visualization": "table",
            "queryPlan": [{
                "tool": "query_objects",
                "arguments": {
                    "object_type": "Event",
                    "select": ["event_id", "actor", "occurred_at"],
                    "filters": [{"property": "action", "op": "eq",
                                 "value": "register"}],
                    "order_by": [{"output": "event_id", "direction": "asc"}],
                    "window": {
                        "property": "occurred_at",
                        "timezone": "Asia/Shanghai",
                        "last": {"unit": "day", "count": 1},
                    },
                    "page_size": 10,
                },
            }],
        },
        {
            "id": "region-count",
            "title": "区域登记分布",
            "goal": "按区域统计登记数量，识别主要来源与空值分组。",
            "visualization": "table",
            "queryPlan": [{
                "tool": "query_objects",
                "arguments": {
                    "object_type": "Event",
                    "group_by": [{"property": "region"}],
                    "aggregates": [{"fn": "count", "as": "登记数"}],
                    "order_by": [{"output": "登记数", "direction": "desc"}],
                },
            }],
        },
    ]


def test_normalize_sections_accepts_query_objects_window():
    normalized = normalize_sections(_sections())
    arguments = normalized[0]["queryPlan"][0]["arguments"]
    assert arguments["window"] == {
        "property": "occurred_at",
        "timezone": "Asia/Shanghai",
        "last": {"unit": "day", "count": 1},
    }
    assert arguments["page_size"] == 10
    assert normalized[1]["queryPlan"][0]["arguments"]["aggregates"] == [
        {"fn": "count", "as": "登记数"}]


def test_normalize_sections_accepts_minute_window_and_keeps_bucket_contract():
    section = _sections()[0]
    section["queryPlan"][0]["arguments"]["window"] = {
        "property": "occurred_at",
        "timezone": "Asia/Shanghai",
        "last": {"unit": "minute", "count": 30},
    }
    normalized = normalize_sections([section])
    assert normalized[0]["queryPlan"][0]["arguments"]["window"]["last"] == {
        "unit": "minute", "count": 30}

    # group_by 时间桶仍是日历桶，不接受 second/minute/hour。
    bucket_section = _sections()[1]
    bucket_section["queryPlan"][0]["arguments"]["group_by"] = [
        {"property": "occurred_at", "bucket": "minute"}]
    with pytest.raises(ValueError):
        normalize_sections([bucket_section])


@pytest.mark.parametrize("arguments", [
    {"object_type": "Event", "cursor": "engine-cursor"},
    {"object_type": "Event", "spl": "from events as e | fields e.event_id as id",
     "semanticBindings": {"events": {"objectType": "Event"}}},
    {"object_type": "Event", "rawQuery": "select * from events"},
    {"object_type": "Event", "window": {"property": "occurred_at",
                                        "timezone": "Asia/Shanghai",
                                        "now": "2026-11-03"}},
])
def test_normalize_sections_rejects_unvalidated_query_plans(arguments):
    section = _sections()[0]
    section["queryPlan"][0]["arguments"] = arguments
    with pytest.raises(ValueError):
        normalize_sections([section])


def test_report_can_run_query_objects_window_and_keeps_quality_gate(
        db, admin_user, events_ontology, monkeypatch):
    monkeypatch.setattr(query_window, "utc_now", lambda: FIXED_NOW)
    template = AnalysisReportTemplate(
        id="tpl-query-window",
        ontology_id=events_ontology["ontologyId"],
        created_by=admin_user.id,
        name="查询窗口报告",
        description="测试报告",
        status="draft",
        revision=1,
        sections=_sections(),
    )
    db.add(template)
    db.commit()

    run = execute_report(db, template, admin_user, trigger_type="preview")
    assert run.status == "succeeded", run.error_message
    first_query = run.section_results[0]["queries"][0]
    first_result = first_query["result"]
    assert first_result["rows"]
    # 窗口内事实行数大于 page_size：报告不续页，句柄必须在本节立即释放。
    assert first_result["hasNext"] is True
    assert first_query["handleRelease"] == {
        "mode": "snapshot", "released": True}
    assert first_result["resolvedWindow"]["timezone"] == "Asia/Shanghai"
    assert "last" not in first_result["resolvedWindow"]
    grouped_result = run.section_results[1]["queries"][0]["result"]
    assert grouped_result["rows"]
    assert run.quality_report["passed"] is True
    assert "近一日" in run.html_content
    from app.ontologies.agent_query.models import AgentQuerySnapshot
    assert db.query(AgentQuerySnapshot).filter(
        AgentQuerySnapshot.owner_user_id == admin_user.id,
    ).count() == 0


def test_report_handle_release_failure_is_diagnosable(
        db, admin_user, events_ontology, monkeypatch):
    monkeypatch.setattr(query_window, "utc_now", lambda: FIXED_NOW)
    from app.ontologies.agent_query import service as agent_query_service

    def broken_release(*_args, **_kwargs):
        raise RuntimeError("snapshot store offline")

    monkeypatch.setattr(
        agent_query_service, "release_query_snapshot", broken_release)
    template = AnalysisReportTemplate(
        id="tpl-query-release-failure",
        ontology_id=events_ontology["ontologyId"],
        created_by=admin_user.id,
        name="句柄释放失败报告",
        description="测试报告",
        status="draft",
        revision=1,
        sections=_sections(),
    )
    db.add(template)
    db.commit()

    run = execute_report(db, template, admin_user, trigger_type="preview")
    # 数据已取得：释放失败只作为可诊断信息，不推翻报告本身。
    assert run.status == "succeeded", run.error_message
    release = run.section_results[0]["queries"][0]["handleRelease"]
    assert release["mode"] == "snapshot"
    assert release["released"] is False
    assert "snapshot store offline" in release["error"]


def test_report_handle_release_covers_remote_mode(db, admin_user, monkeypatch):
    from app.ontologies.agent_query import remote_query
    from app.ontologies.agent_runtime import reporting

    calls: list[tuple[str, str]] = []

    def fake_cancel(_db, user, query_id):
        calls.append((user.id, query_id))
        return {"queryId": query_id, "cancelled": True}

    monkeypatch.setattr(remote_query, "cancel_remote_query", fake_cancel)
    release = reporting._release_report_query_handle(
        db, admin_user, "", {"queryId": "remote-1",
                             "dataMode": "remote"})
    assert release == {"mode": "remote", "released": True}
    assert calls == [(admin_user.id, "remote-1")]

    monkeypatch.setattr(
        remote_query, "cancel_remote_query",
        lambda *_args: {"queryId": "remote-2", "cancelled": False})
    released_again = reporting._release_report_query_handle(
        db, admin_user, "", {"queryId": "remote-2", "dataMode": "remote"})
    assert released_again == {"mode": "remote", "released": False,
                              "note": "no_live_handle"}


def test_public_query_result_removes_rows_and_engine_handles(
        db, admin_user, events_ontology):
    from app.ontologies.agent_runtime.boundary import build_scope
    from app.ontologies.agent_runtime.toolkit import ToolRunner

    _, _, scope = build_scope(
        db, events_ontology["ontologyId"],
        release_id=events_ontology["releaseId"])
    result = ToolRunner(db, scope, user=admin_user,
                        query_session_id="conv-public").run(
        "query_objects", {"object_type": "Event", "page_size": 2})
    public = public_query_result(result)
    assert "rows" not in public
    assert "cursor" not in public
    assert "queryId" not in public
    assert public["querySpec"] == result["querySpec"]
    assert public["rowsOmitted"] is True
    assert public["hasNext"] is True
