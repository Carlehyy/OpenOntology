"""本体助手查询层：相对窗口、去重阈值、有界序列、下钻、query-view 分页。

这些测试刻意把“当前时间”固定为 2026-11-03T04:00Z（Asia/Shanghai 12:00），
覆盖 9/10/11 去重阈值、窗口左闭右开边界、重复事件、多维/NULL 下钻、
时间桶边界、querySpec 篡改拒绝、Web cursor 生命周期和源端失败不返回 0。
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.ontologies.agent_query.service import QueryError, release_query_snapshot
from app.ontologies.agent_runtime import query_window
from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.models import AgentConversation
from app.ontologies.agent_runtime.query_spec import verify_drilldown
from app.ontologies.agent_runtime.query_view_router import (
    QueryViewRequest,
    _handle_query_view,
    delete_query_view,
)
from app.ontologies.agent_runtime.toolkit import ToolRunner
from tests.ontologies.test_agent_runtime import modeled_ontology  # noqa: F401
from tests.ontologies.test_agent_unified_query import _freeze  # noqa: F401
from tests.ontologies.test_agent_remote_aggregation import (  # noqa: F401
    _freeze_with_rich_remote_supplier,
)

FIXED_NOW = datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)
WINDOW_1D = {"property": "occurred_at", "timezone": "Asia/Shanghai",
             "last": {"unit": "day", "count": 1}}
WINDOW_4D = {"property": "occurred_at", "timezone": "Asia/Shanghai",
             "last": {"unit": "day", "count": 4}}


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(query_window, "utc_now", lambda: FIXED_NOW)


def _event(event_id, actor, when, action="register", region="north",
           module="registration"):
    return {
        "id": event_id,
        "objectTypeId": "ot-event",
        "properties": {
            "event_id": event_id,
            "actor": actor,
            "action": action,
            "occurred_at": when.isoformat(),
            "region": region,
            "module": module,
        },
        "computed": {},
    }


@pytest.fixture
def events_ontology(client, auth_headers, db, ontology):
    oid = ontology["id"]
    base = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)  # 窗口 start
    instances = []
    for index in range(1, 11):
        instances.append(_event(
            f"ev-u{index}", f"u{index}", base + timedelta(minutes=index),
            region=None if index == 10 else ("north" if index % 2 else "south")))
    instances.append(_event("ev-u1-dup", "u1", base + timedelta(minutes=1),
                            region="north"))
    # 同组两个片段：0/1/2 分钟 与 30/31/32 分钟；gap 打断后重新开始。
    for offset, actor in ((0, "r1"), (1, "r2"), (2, "r3"),
                          (30, "r4"), (31, "r5"), (32, "r6")):
        instances.append(_event(
            f"ev-reset-{actor}", actor, base + timedelta(minutes=offset),
            module="reset"))
    seq = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
    instances.extend([
        _event("ev-seq-1", "seq_ok", seq, module="seq"),
        _event("ev-seq-2", "seq_ok", seq + timedelta(days=1), module="seq"),
        _event("ev-seq-2-dup", "seq_ok", seq + timedelta(days=1), module="seq"),
        _event("ev-seq-3", "seq_ok", seq + timedelta(days=2), module="seq"),
        _event("ev-seq-approve", "seq_ok", seq + timedelta(days=2, hours=1),
               action="approve", module="seq"),
        _event("ev-bad-1", "seq_bad", seq, module="seq"),
        _event("ev-bad-2", "seq_bad", seq + timedelta(days=2), module="seq"),
        _event("ev-before", "u11", datetime(2026, 11, 2, 3, 59, 59,
                                            tzinfo=timezone.utc),
               module="other"),
        _event("ev-at-end", "u12", FIXED_NOW, module="other"),
    ])
    body = {
        "objectTypes": [{
            "id": "ot-event", "name": "Event", "displayName": "事件",
            "primaryKey": "event_id",
            "properties": [
                {"id": "p1", "name": "event_id", "displayName": "事件号",
                 "type": "string", "required": True},
                {"id": "p2", "name": "actor", "displayName": "登记人",
                 "type": "string"},
                {"id": "p3", "name": "action", "displayName": "动作",
                 "type": "string"},
                {"id": "p4", "name": "occurred_at", "displayName": "发生时间",
                 "type": "datetime"},
                {"id": "p5", "name": "region", "displayName": "区域",
                 "type": "string"},
                {"id": "p6", "name": "module", "displayName": "模块",
                 "type": "string"},
            ],
            "positionX": 0, "positionY": 0,
        }],
        "linkTypes": [], "actions": [], "functions": [],
        "instances": instances, "linkInstances": [],
    }
    r = client.put(f"/api/v2/formal/ontologies/{oid}/full",
                   headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    return _freeze(db, oid)


@pytest.fixture
def view_conversation(db, events_ontology, admin_user):
    conversation = AgentConversation(
        id=f"conv-view-{uuid.uuid4().hex[:8]}",
        ontology_id=events_ontology["ontologyId"],
        ontology_release_id=events_ontology["releaseId"],
        user_id=admin_user.id,
    )
    db.add(conversation)
    db.commit()
    return conversation




def _bulk_events(db, events, count: int, module: str = "bulk"):
    from app.ontologies.formal_modeling.models import ObjectInstance
    start = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    for index in range(count):
        event_id = f"bulk-{module}-{index}"
        db.add(ObjectInstance(
            id=event_id,
            ontology_id=events["ontologyId"],
            ontology_release_id=events["releaseId"],
            object_type_id="ot-event",
            properties={
                "event_id": event_id,
                "actor": f"bulk-user-{index % 10}",
                "action": "register",
                "occurred_at": (start + timedelta(seconds=index)).isoformat(),
                "region": "north",
                "module": module,
            },
            computed={},
        ))
    db.commit()


def _runner(db, events, user, conversation_id="") -> ToolRunner:
    _, _, scope = build_scope(
        db, events["ontologyId"], release_id=events["releaseId"])
    return ToolRunner(db, scope, user=user,
                      query_session_id=conversation_id)


def test_window_boundaries_are_absolute_and_left_closed_right_open(
        db, admin_user, events_ontology, view_conversation):
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    out = runner.run("query_objects", {
        "object_type": "Event",
        "filters": [{"property": "action", "op": "eq", "value": "register"}],
        "select": ["event_id", "occurred_at"],
        "order_by": [{"output": "event_id", "direction": "asc"}],
        "window": WINDOW_1D,
        "page_size": 50,
    })

    ids = {row["event_id"] for row in out["rows"]}
    assert "ev-before" not in ids                  # start - 1s
    assert "ev-at-end" not in ids                  # == end，右开
    assert {"ev-u1", "ev-u1-dup", "ev-u10"} <= ids
    window = out["resolvedWindow"]
    assert window["timezone"] == "Asia/Shanghai"
    assert window["startUtc"] == "2026-11-02T04:00:00+00:00"
    assert window["endUtc"] == "2026-11-03T04:00:00+00:00"
    assert window["exclusiveEnd"] is True
    spec_window = out["querySpec"]["resolvedWindow"]
    assert spec_window["startUtc"] == window["startUtc"]
    assert "last" not in spec_window
    leaves = [(item["op"], item["value"])
              for item in out["querySpec"]["query"]["where"]]
    assert ("gte", "2026-11-02T04:00:00+00:00") in leaves
    assert ("lt", "2026-11-03T04:00:00+00:00") in leaves
    assert out["consistency"]["sameSnapshot"] is True


def test_distinct_threshold_9_10_11_and_duplicate_events(
        db, admin_user, events_ontology, view_conversation):
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    base = {
        "object_type": "Event",
        "mode": "distinct",
        "subject_property": "actor",
        "time_property": "occurred_at",
        "filters": [
            {"property": "action", "op": "eq", "value": "register"},
            {"property": "module", "op": "eq", "value": "registration"},
        ],
        "window": WINDOW_1D,
    }
    outcomes = {}
    for threshold in (9, 10, 11):
        outcomes[threshold] = runner.run(
            "analyze_sequences", {**base, "threshold": threshold})
    assert outcomes[9]["distinctSubjects"] == 10
    assert outcomes[9]["candidateRows"] == 11      # 10 人 + 1 条重复
    assert outcomes[9]["meetsThreshold"] is True
    assert outcomes[10]["meetsThreshold"] is True
    assert outcomes[11]["meetsThreshold"] is False
    assert outcomes[9]["mode"] == "distinct"


def test_distinct_group_by_counts_per_module_generically(
        db, admin_user, events_ontology, view_conversation):
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    out = runner.run("analyze_sequences", {
        "object_type": "Event",
        "mode": "distinct",
        "subject_property": "actor",
        "time_property": "occurred_at",
        "threshold": 3,
        "group_by": [{"property": "region"}],
        "filters": [{"property": "action", "op": "eq", "value": "register"}],
        "window": WINDOW_1D,
    })
    assert out["groupBy"] == ["region"]
    assert out["groups"]
    north = next(group for group in out["groups"]
                 if group["groupValues"] == ["north"])
    assert north["distinctSubjects"] >= 3
    assert north["meetsThreshold"] is True
    assert out["matchingGroups"] >= 1
    assert out["querySpec"]["query"]["groupBy"]
    assert out["querySpec"]["drilldownBase"]


def test_consecutive_sequence_uses_deduped_bounded_rows(
        db, admin_user, events_ontology, view_conversation):
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    out = runner.run("analyze_sequences", {
        "object_type": "Event",
        "mode": "consecutive",
        "subject_property": "actor",
        "time_property": "occurred_at",
        "threshold": 3,
        "max_gap_seconds": 60,
        "group_by": [{"property": "module"}],
        "filters": [
            {"property": "action", "op": "eq", "value": "register"},
            {"property": "module", "op": "eq", "value": "registration"},
        ],
        "window": WINDOW_1D,
    })
    assert out["matchingGroups"] == 1
    group = out["groups"][0]
    assert group["groupValues"] == ["registration"]
    assert group["distinctSubjects"] == 10
    assert group["qualifies"] is True
    assert out["candidateRows"] == 11      # 10 人 + 重复事件
    assert out["duplicateRows"] >= 1
    assert out["exact"] is True
    assert out["consistency"]["sameSnapshot"] is True


def test_key_case_ten_distinct_registrants_in_one_minute_window(
        db, admin_user, events_ontology, view_conversation, monkeypatch):
    """原始泛化案例：窗口内 10 个不同主体在连续片段内，重复事件只计一次。"""
    # 事件发生在 04:01~04:10Z；把服务端 now 固定到 04:15Z，使 60 分钟
    # 窗口覆盖它们，同时 1 天窗口仍包含这些事件。
    monkeypatch.setattr(query_window, "utc_now", lambda: datetime(
        2026, 11, 2, 4, 15, tzinfo=timezone.utc))
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    base = {
        "object_type": "Event",
        "mode": "consecutive",
        "subject_property": "actor",
        "time_property": "occurred_at",
        "max_gap_seconds": 60,
        "group_by": [{"property": "module"}],
        "filters": [
            {"property": "action", "op": "eq", "value": "register"},
            {"property": "module", "op": "eq", "value": "registration"},
        ],
        "window": {"property": "occurred_at", "timezone": "Asia/Shanghai",
                   "last": {"unit": "minute", "count": 30}},
    }
    nine = runner.run("analyze_sequences", {**base, "threshold": 9})
    assert nine["meetsThreshold"] is True
    ten = runner.run("analyze_sequences", {**base, "threshold": 10})
    assert ten["window"]["timezone"] == "Asia/Shanghai"
    assert ten["matchingGroups"] == 1
    assert ten["groups"][0]["distinctSubjects"] == 10
    assert ten["groups"][0]["qualifies"] is True
    assert ten["meetsThreshold"] is True
    eleven = runner.run("analyze_sequences", {**base, "threshold": 11})
    assert eleven["groups"][0]["distinctSubjects"] == 10
    assert eleven["meetsThreshold"] is False


def test_consecutive_segment_resets_after_gap_and_uses_any_segment(
        db, admin_user, events_ontology, view_conversation):
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    base = {
        "object_type": "Event",
        "mode": "consecutive",
        "subject_property": "actor",
        "time_property": "occurred_at",
        "max_gap_seconds": 60,
        "group_by": [{"property": "module"}],
        "filters": [{"property": "module", "op": "eq", "value": "reset"}],
        "window": WINDOW_1D,
    }
    threshold3 = runner.run("analyze_sequences", {**base, "threshold": 3})
    assert threshold3["groups"][0]["distinctSubjects"] == 3
    assert threshold3["groups"][0]["qualifies"] is True
    threshold4 = runner.run("analyze_sequences", {**base, "threshold": 4})
    assert threshold4["groups"][0]["distinctSubjects"] == 3
    assert threshold4["groups"][0]["qualifies"] is False
    assert threshold4["meetsThreshold"] is False


def test_ordered_sequence_requires_stage_order(
        db, admin_user, events_ontology, view_conversation):
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    out = runner.run("analyze_sequences", {
        "object_type": "Event",
        "mode": "ordered",
        "subject_property": "actor",
        "time_property": "occurred_at",
        "sequence": [
            {"filters": [{"property": "action", "op": "eq",
                          "value": "register"}]},
            {"filters": [{"property": "action", "op": "eq",
                          "value": "approve"}]},
        ],
        "threshold": 1,
        "group_by": [{"property": "module"}],
        "filters": [{"property": "module", "op": "eq", "value": "seq"}],
        "window": WINDOW_4D,
    })
    assert out["matchingGroups"] == 1
    assert out["groups"][0]["groupValues"] == ["seq"]
    assert out["groups"][0]["distinctSubjects"] == 1
    assert out["sampleSubjects"] == ["seq_ok"]


def test_sequence_budget_is_rejected_not_truncated(
        db, admin_user, events_ontology, view_conversation, monkeypatch):
    from app.ontologies.agent_runtime import sequence_tools
    monkeypatch.setattr(sequence_tools, "SEQUENCE_ROW_BUDGET", 1)
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    with pytest.raises(ToolError, match="sequence_budget_exceeded"):
        runner.run("analyze_sequences", {
            "object_type": "Event",
            "mode": "consecutive",
            "subject_property": "actor",
            "time_property": "occurred_at",
            "threshold": 2,
            "max_gap_seconds": 60,
            "filters": [{"property": "module", "op": "eq",
                         "value": "registration"}],
            "window": WINDOW_1D,
        })


def test_sequence_fetch_failure_releases_open_handle(
        db, admin_user, events_ontology, view_conversation, monkeypatch):
    from app.ontologies.agent_query import service as query_service
    from app.ontologies.agent_runtime import sequence_tools

    scope = build_scope(
        db, events_ontology["ontologyId"],
        release_id=events_ontology["releaseId"])[2]
    scope.profile.max_rows_per_query = 2
    db.commit()
    released = []
    original = query_service.release_query_snapshot
    monkeypatch.setattr(
        query_service, "release_query_snapshot",
        lambda *args, **kwargs: released.append(args) or original(*args, **kwargs))

    def fail_fetch(*args, **kwargs):
        raise RuntimeError("fetch boom")

    monkeypatch.setattr(query_service, "fetch_unified_query_page", fail_fetch)
    runner = ToolRunner(db, scope, user=admin_user,
                        query_session_id=view_conversation.id)
    with pytest.raises(RuntimeError, match="fetch boom"):
        runner.run("analyze_sequences", {
            "object_type": "Event",
            "mode": "consecutive",
            "subject_property": "actor",
            "time_property": "occurred_at",
            "threshold": 2,
            "max_gap_seconds": 60,
            "filters": [{"property": "module", "op": "eq",
                         "value": "registration"}],
            "window": WINDOW_1D,
        })
    assert released


def test_multidim_null_group_has_structured_values_and_drilldown(
        db, admin_user, events_ontology, view_conversation):
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    out = runner.run("query_objects", {
        "object_type": "Event",
        "group_by": [{"property": "region"}, {"property": "action"}],
        "aggregates": [{"fn": "count"}],
        "page_size": 50,
    })
    null_rows = [row for row in out["rows"] if row["region"] is None]
    assert null_rows and null_rows[0]["groupValues"][0] is None
    assert null_rows[0]["groupValues"][1] == "register"
    assert "drilldown" in null_rows[0]

    payload = verify_drilldown(
        null_rows[0]["drilldown"]["token"],
        owner=str(admin_user.id),
        ontology_id=events_ontology["ontologyId"],
        conversation_id=view_conversation.id)
    assert payload["conditions"][0] == {
        "source": "s0", "property": "region", "op": "is_null"}
    assert payload["conditions"][1]["op"] == "eq"
    assert payload["groupValues"] == [None, "register"]

    result = _handle_query_view(
        QueryViewRequest(conversationId=view_conversation.id,
                         querySpec=out["querySpec"],
                         drilldown=null_rows[0]["drilldown"]["token"]),
        events_ontology["ontologyId"], db, admin_user)
    assert result["rows"]
    assert all(row["region"] is None for row in result["rows"])
    assert result["consistency"]["sameSnapshot"] is False
    assert result["consistency"]["queryFresh"] is True


def test_time_bucket_drilldown_uses_calendar_boundaries_not_string_split():
    from app.ontologies.agent_runtime.query_spec import drilldown_conditions
    item = [{"source": "s0", "property": "occurred_at", "bucket": "month",
             "readable": "occurred_at"}]
    conditions = drilldown_conditions(["2026-10-01"], item)
    assert conditions == [
        {"source": "s0", "property": "occurred_at", "op": "gte",
         "value": "2026-10-01"},
        {"source": "s0", "property": "occurred_at", "op": "lt",
         "value": "2026-11-01"},
    ]
    with pytest.raises(ToolError, match="query_drilldown_unsupported"):
        drilldown_conditions(["2026-10"], item)


def test_query_objects_cursor_only_restores_plan_from_turn_cache(
        db, admin_user, events_ontology, view_conversation):
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    first = runner.run("query_objects", {
        "object_type": "Event",
        "select": ["event_id", "actor"],
        "order_by": [{"output": "event_id", "direction": "asc"}],
        "page_size": 2,
    })
    assert first["hasNext"] is True
    second = runner.run("query_objects", {"cursor": first["cursor"]})
    assert second["objectType"] == "事件"
    assert second["rows"] and "event_id" in second["rows"][0]
    assert second["querySpec"] == first["querySpec"]
    assert second["consistency"]["sameSnapshot"] is True

    grouped = runner.run("query_objects", {
        "object_type": "Event",
        "group_by": [{"property": "region"}],
        "aggregates": [{"fn": "count"}],
        "page_size": 1,
    })
    assert grouped["hasNext"] is True
    grouped_next = runner.run("query_objects", {"cursor": grouped["cursor"]})
    assert grouped_next["objectType"] == "事件"
    assert grouped_next["groupBy"] == ["region"]
    assert grouped_next["rows"][0]["groupValues"]
    assert "drilldown" in grouped_next["rows"][0]
    release_query_snapshot(
        db, admin_user, first["queryId"],
        session_kind="ontology_agent", session_id=view_conversation.id)
    release_query_snapshot(
        db, admin_user, grouped["queryId"],
        session_kind="ontology_agent", session_id=view_conversation.id)


def test_query_view_pagination_uses_real_web_owned_cursor(
        db, admin_user, events_ontology, view_conversation):
    from app.ontologies.agent_query.models import AgentQuerySnapshot
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    first_source = runner.run("query_objects", {
        "object_type": "Event",
        "select": ["event_id", "actor"],
        "order_by": [{"output": "event_id", "direction": "asc"}],
        "page_size": 2,
    })
    assert first_source["hasNext"] is True
    # 工具回合内的句柄不属于 Web；测试直接释放，避免污染快照配额。
    release_query_snapshot(
        db, admin_user, first_source["queryId"],
        session_kind="ontology_agent", session_id=view_conversation.id)

    first = _handle_query_view(
        QueryViewRequest(conversationId=view_conversation.id,
                         querySpec=first_source["querySpec"]),
        events_ontology["ontologyId"], db, admin_user)
    assert first["hasNext"] is True and first["cursor"]
    assert first["consistency"]["sameSnapshot"] is False
    assert first["consistency"]["queryFresh"] is True
    view_snapshots = db.query(AgentQuerySnapshot).filter(
        AgentQuerySnapshot.session_kind == "ontology_agent_query_view").count()
    assert view_snapshots == 1  # 中间页不释放

    rows = list(first["rows"])
    cursor = first["cursor"]
    last = first
    pages = 1
    while cursor:
        pages += 1
        last = _handle_query_view(
            QueryViewRequest(conversationId=view_conversation.id,
                             cursor=cursor,
                             querySpec=first["querySpec"]),
            events_ontology["ontologyId"], db, admin_user)
        rows.extend(last["rows"])
        cursor = last["cursor"] if last["hasNext"] else None
    assert pages > 1
    assert len(rows) == first["totalRowCount"]
    assert len({row["event_id"] for row in rows}) == len(rows)
    assert last["released"] is True
    view_snapshots = db.query(AgentQuerySnapshot).filter(
        AgentQuerySnapshot.session_kind == "ontology_agent_query_view").count()
    assert view_snapshots == 0  # 终页自动释放

    # 终页后 DELETE 幂等
    deleted = delete_query_view(
        events_ontology["ontologyId"],
        type("Body", (), {"conversation_id": view_conversation.id,
                          "cursor": first["cursor"]})(),
        db, admin_user)
    assert deleted["data"]["released"] is False


def test_query_view_cursor_requires_query_spec(
        db, admin_user, events_ontology, view_conversation):
    from fastapi import HTTPException
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    source = runner.run("query_objects", {
        "object_type": "Event", "select": ["event_id"], "page_size": 2})
    release_query_snapshot(
        db, admin_user, source["queryId"],
        session_kind="ontology_agent", session_id=view_conversation.id)
    first = _handle_query_view(
        QueryViewRequest(conversationId=view_conversation.id,
                         querySpec=source["querySpec"]),
        events_ontology["ontologyId"], db, admin_user)
    with pytest.raises(HTTPException) as excinfo:
        _handle_query_view(
            QueryViewRequest(conversationId=view_conversation.id,
                             cursor=first["cursor"]),
            events_ontology["ontologyId"], db, admin_user)
    assert excinfo.value.status_code == 410
    assert excinfo.value.detail["code"] == "query_spec_required"
    delete_query_view(
        events_ontology["ontologyId"],
        type("Body", (), {"conversation_id": view_conversation.id,
                          "cursor": first["cursor"]})(),
        db, admin_user)


def test_query_view_rejects_tampered_scope_and_cursor(
        db, admin_user, events_ontology, view_conversation):
    from fastapi import HTTPException
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    source = runner.run("query_objects", {
        "object_type": "Event", "select": ["event_id"], "page_size": 2})
    release_query_snapshot(
        db, admin_user, source["queryId"],
        session_kind="ontology_agent", session_id=view_conversation.id)
    spec = copy.deepcopy(source["querySpec"])
    spec["query"]["sources"][0]["releaseId"] = "other-release"
    with pytest.raises(HTTPException) as excinfo:
        _handle_query_view(
            QueryViewRequest(conversationId=view_conversation.id,
                             querySpec=spec),
            events_ontology["ontologyId"], db, admin_user)
    assert excinfo.value.status_code == 403

    spec = copy.deepcopy(source["querySpec"])
    spec["query"]["sources"][0]["objectTypeId"] = "ot-hidden"
    with pytest.raises(HTTPException) as excinfo:
        _handle_query_view(
            QueryViewRequest(conversationId=view_conversation.id,
                             querySpec=spec),
            events_ontology["ontologyId"], db, admin_user)
    assert excinfo.value.status_code == 403

    first = _handle_query_view(
        QueryViewRequest(conversationId=view_conversation.id,
                         querySpec=source["querySpec"]),
        events_ontology["ontologyId"], db, admin_user)
    tampered = first["cursor"][:-2] + ("aa" if first["cursor"][-2:] != "aa"
                                        else "bb")
    with pytest.raises(HTTPException) as excinfo:
        _handle_query_view(
            QueryViewRequest(conversationId=view_conversation.id,
                             cursor=tampered),
            events_ontology["ontologyId"], db, admin_user)
    assert excinfo.value.status_code == 403


def test_query_view_after_delete_fetch_is_410_refresh(
        db, admin_user, events_ontology, view_conversation):
    from fastapi import HTTPException
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    source = runner.run("query_objects", {
        "object_type": "Event", "select": ["event_id"], "page_size": 2})
    release_query_snapshot(
        db, admin_user, source["queryId"],
        session_kind="ontology_agent", session_id=view_conversation.id)
    first = _handle_query_view(
        QueryViewRequest(conversationId=view_conversation.id,
                         querySpec=source["querySpec"]),
        events_ontology["ontologyId"], db, admin_user)
    assert first["hasNext"] is True
    deleted = delete_query_view(
        events_ontology["ontologyId"],
        type("Body", (), {"conversation_id": view_conversation.id,
                          "cursor": first["cursor"]})(),
        db, admin_user)
    assert deleted["data"]["released"] is True
    with pytest.raises(HTTPException) as excinfo:
        _handle_query_view(
            QueryViewRequest(conversationId=view_conversation.id,
                             cursor=first["cursor"],
                             querySpec=source["querySpec"]),
            events_ontology["ontologyId"], db, admin_user)
    assert excinfo.value.status_code == 410
    assert excinfo.value.detail["code"] == "query_snapshot_expired"
    assert excinfo.value.detail["reprepareRequired"] is True


def test_query_view_source_failure_is_not_zero(
        db, admin_user, events_ontology, view_conversation, monkeypatch):
    from fastapi import HTTPException
    from app.ontologies.agent_runtime import query_view_service

    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    source = runner.run("query_objects", {
        "object_type": "Event", "select": ["event_id"], "page_size": 2})
    release_query_snapshot(
        db, admin_user, source["queryId"],
        session_kind="ontology_agent", session_id=view_conversation.id)

    def fail(*args, **kwargs):
        raise QueryError(503, "SOURCE_UNAVAILABLE", "源端不可用")

    monkeypatch.setattr(query_view_service, "execute_unified_query", fail)
    with pytest.raises(HTTPException) as excinfo:
        _handle_query_view(
            QueryViewRequest(conversationId=view_conversation.id,
                             querySpec=source["querySpec"]),
            events_ontology["ontologyId"], db, admin_user)
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["code"] == "SOURCE_UNAVAILABLE"


def test_query_objects_source_failure_is_not_zero(
        db, admin_user, events_ontology, monkeypatch):
    from app.ontologies.agent_query import service as query_service
    runner = _runner(db, events_ontology, admin_user, "conv-fail")
    monkeypatch.setattr(
        query_service, "execute_unified_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            QueryError(503, "SOURCE_UNAVAILABLE", "源端不可用")))
    with pytest.raises(ToolError, match="SOURCE_UNAVAILABLE"):
        runner.run("query_objects", {"object_type": "Event"})


def test_spl_query_uses_existing_translation_and_authorization(
        db, admin_user, events_ontology, view_conversation, monkeypatch):
    from app.ontologies.agent_query import remote_query

    captured = {}

    class Port:
        def execute(self, db, user, plan, *, request, session_kind,
                    session_id):
            captured["plan"] = plan
            captured["request"] = request
            captured["session"] = (session_kind, session_id)
            return {
                "queryId": "q-spl-view-1",
                "rows": [{"actor": "u1"}],
                "columns": [{"name": "actor", "type": "string",
                             "nullable": True,
                             "sourceRefs": {"alias": "e"}}],
                "totalRowCount": 1,
                "rowCounting": "complete",
                "hasNext": False,
                "cursor": None,
                "partial": False,
                "truncated": False,
                "exact": True,
                "completeness": "complete",
                "countsScope": "complete_snapshot",
                "observedAt": "2026-11-03T04:00:00+00:00",
                "citations": [],
                "releases": {"e": {"dataMode": "materialized"}},
                "persistence": {"mode": "ephemeral"},
            }

        def fetch(self, *args, **kwargs):
            raise AssertionError("unexpected fetch")

        def cancel(self, db, user, query_id):
            captured["cancelled"] = query_id
            return {}

    monkeypatch.setattr(remote_query, "_port", Port())
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    out = runner.run("query_objects", {
        "spl": "from events as e | where e.action = :action "
               "| fields e.actor as actor",
        "semanticBindings": {"events": {"objectType": "Event"}},
        "params": {"action": "register"},
        "page_size": 5,
    })
    assert out["shape"] == "spl"
    assert out["rows"] == [{"actor": "u1"}]
    assert out["querySpec"]["shape"] == "spl"
    assert out["querySpec"]["semanticBindings"]["events"] == {
        "ontologyId": events_ontology["ontologyId"],
        "releaseId": events_ontology["releaseId"],
        "objectTypeId": "ot-event",
    }
    assert captured["request"]["spl"].startswith("from events")
    plan = captured["plan"]
    source = next(item for item in plan["sources"] if item["alias"] == "e")
    assert source["propertyMap"]["actor"] == "actor"
    assert captured["session"] == ("ontology_agent", view_conversation.id)


def test_spl_binding_outside_scope_is_rejected(
        db, admin_user, events_ontology, view_conversation):
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    with pytest.raises(ToolError, match="不存在或不在授权范围内"):
        runner.run("query_objects", {
            "spl": "from hidden as h | fields h.event_id as id",
            "semanticBindings": {"hidden": {"objectType": "HiddenType"}},
        })
    with pytest.raises(ToolError, match="不能同时携带结构树参数"):
        runner.run("query_objects", {
            "object_type": "Event",
            "spl": "from events as e | fields e.event_id as id",
            "semanticBindings": {"events": {"objectType": "Event"}},
        })


def test_sequence_501_rows_with_cap50_uses_real_page_budget(
        db, admin_user, events_ontology, view_conversation):
    _bulk_events(db, events_ontology, 501)
    scope = build_scope(
        db, events_ontology["ontologyId"],
        release_id=events_ontology["releaseId"])[2]
    assert scope.profile.max_rows_per_query == 50
    runner = ToolRunner(db, scope, user=admin_user,
                        query_session_id=view_conversation.id)
    out = runner.run("analyze_sequences", {
        "object_type": "Event",
        "mode": "consecutive",
        "subject_property": "actor",
        "time_property": "occurred_at",
        "threshold": 1,
        "max_gap_seconds": 60,
        "filters": [{"property": "module", "op": "eq", "value": "bulk"}],
        "window": WINDOW_1D,
    })
    assert out["candidateRows"] == 501
    assert out["matchingGroups"] == 1
    assert out["exact"] is True


def test_sequence_1001_rows_is_rejected_by_budget(
        db, admin_user, events_ontology, view_conversation):
    _bulk_events(db, events_ontology, 1001)
    scope = build_scope(
        db, events_ontology["ontologyId"],
        release_id=events_ontology["releaseId"])[2]
    runner = ToolRunner(db, scope, user=admin_user,
                        query_session_id=view_conversation.id)
    with pytest.raises(ToolError, match="sequence_budget_exceeded"):
        runner.run("analyze_sequences", {
            "object_type": "Event",
            "mode": "consecutive",
            "subject_property": "actor",
            "time_property": "occurred_at",
            "threshold": 1,
            "max_gap_seconds": 60,
            "filters": [{"property": "module", "op": "eq", "value": "bulk"}],
            "window": WINDOW_1D,
        })


def test_external_cursor_without_turn_spec_rejected_before_fetch(
        db, admin_user, events_ontology, view_conversation, monkeypatch):
    from app.ontologies.agent_query import service as query_service
    calls = []
    monkeypatch.setattr(query_service, "fetch_unified_query_page",
                        lambda *args, **kwargs: calls.append(1))
    runner = _runner(db, events_ontology, admin_user, view_conversation.id)
    with pytest.raises(ToolError, match="query_objects_cursor_context_missing"):
        runner.run("query_objects", {
            "object_type": "Event", "cursor": "mem.external-cursor"})
    with pytest.raises(ToolError, match="query_objects_cursor_context_missing"):
        runner.run("query_objects", {
            "spl": "from events as e | fields e.event_id as id",
            "semanticBindings": {"events": {"objectType": "Event"}},
            "cursor": "mem.external-cursor",
        })
    assert calls == []


def test_describe_objects_summary_and_scope(db, admin_user, modeled_ontology):
    _, _, scope = build_scope(db, modeled_ontology["id"])
    runner = ToolRunner(db, scope, user=admin_user)
    summary = runner.run("describe_objects", {})
    assert summary["mode"] == "summary"
    assert {item["name"] for item in summary["objectTypes"]} == {
        "Order", "Supplier"}
    assert all("properties" not in item for item in summary["objectTypes"])
    details = runner.run("describe_objects", {"object_types": ["Order"]})
    order = details["objectTypes"][0]
    assert {item["name"] for item in order["properties"]} == {
        "order_no", "status", "amount"}
    assert any(link["name"] == "order_supplier" for link in order["links"])
    assert details["hasMore"] is False

    profile = scope.profile
    profile.allowed_properties = {"ot-order": ["order_no"]}
    db.commit()
    _, _, restricted = build_scope(db, modeled_ontology["id"])
    restricted_details = ToolRunner(db, restricted, user=admin_user).run(
        "describe_objects", {"object_types": ["Order"]})
    assert [item["name"] for item in
            restricted_details["objectTypes"][0]["properties"]] == ["order_no"]


def test_describe_objects_marks_remote_property_queryability(
        db, admin_user, modeled_ontology):
    """远程类型逐属性标明 queryable 与未映射原因，避免拿不可查询属性试探。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(
        db, oid, unmapped_properties=["internal_note"])
    _, _, scope = build_scope(db, oid, release_id=release_id)
    runner = ToolRunner(db, scope, user=admin_user)

    detail = runner.run("describe_objects", {"object_types": ["Supplier"]})
    supplier = detail["objectTypes"][0]
    assert supplier["remote"] is True
    by_name = {item["name"]: item for item in supplier["properties"]}
    assert by_name["sname"]["queryable"] is True
    assert "unqueryableReason" not in by_name["sname"]
    assert by_name["region"]["queryable"] is True
    assert by_name["internal_note"]["queryable"] is False
    assert by_name["internal_note"]["unqueryableReason"] == \
        "remote_property_unmapped"
    assert supplier["queryablePropertyCount"] == 4
    assert supplier["propertyCount"] == 5
