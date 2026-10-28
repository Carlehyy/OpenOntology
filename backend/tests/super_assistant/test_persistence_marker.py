"""可信远程联邦响应 persistence 标记的落库边界与 JSON 感知截断测试。

契约（v1）：ontology_query 等可信工具响应顶层携带
``persistence={mode:'ephemeral',classification:'detail'|'aggregate'}``；
detail 只允许进入模型上下文/SSE，所有持久面只存元信息壳；标记缺失的
本地响应完全保持旧行为；标记存在但形状非法 fail-closed 按 detail。
"""
from __future__ import annotations

import json

from app.super_assistant import persistence


def _detail_payload(rows=None, **extra):
    payload = {
        "queryId": "qs-1",
        "schemaVersion": 2,
        "observedAt": "2026-10-26T00:00:00+00:00",
        "releases": {"o1": {"ontologyId": "o1", "releaseId": "r1"}},
        "columns": ["orderId", "amount"],
        "rows": rows if rows is not None else [
            {"orderId": "SO-1001", "amount": 120.5},
            {"orderId": "SO-1002", "amount": 88.0},
        ],
        "pageRowCount": 2,
        "totalRowCount": 57,
        "distinctEntityCounts": {"o1": 57},
        "inputCounts": {"o1": 60},
        "joinRowCounts": [],
        "citations": [{"title": "敏感引用原文", "url": "x"}],
        "sourceStates": [{"lastValue": "机密值"}],
        "params": {"filterValue": "机密值"},
        "partial": False,
        "truncated": False,
        "exact": True,
        "completeness": "complete",
        "countsScope": "complete_snapshot",
        "hasNext": True,
        "cursor": "sig-cursor-token",
        "expiresAt": "2026-10-27T00:00:00+00:00",
        **extra,
    }
    return payload


def _marker(classification="detail", reference=None):
    marker = {"mode": "ephemeral", "classification": classification}
    if reference is not None:
        marker["reference"] = reference
    return marker


# ------------------------------------------------------------ marker 解析


def test_parse_marker_absent_returns_none():
    assert persistence.parse_persistence_marker({"rows": []}) is None


def test_parse_marker_valid_detail_and_aggregate():
    detail = persistence.parse_persistence_marker(
        {"persistence": _marker("detail")})
    assert detail is not None and detail.is_detail and not detail.is_aggregate
    aggregate = persistence.parse_persistence_marker(
        {"persistence": _marker("aggregate")})
    assert aggregate is not None and aggregate.is_aggregate


def test_parse_marker_invalid_shapes_fail_closed_as_detail():
    invalid = [
        {"persistence": {}},
        {"persistence": {"mode": "ephemeral"}},
        {"persistence": {"mode": "ephemeral", "classification": "summary"}},
        {"persistence": {"mode": "persist", "classification": "aggregate"}},
        {"persistence": "not-a-dict"},
        {"persistence": None},
    ]
    for payload in invalid:
        marker = persistence.parse_persistence_marker(payload)
        assert marker is not None and marker.is_detail, payload


# ------------------------------------------------------------ 壳构造


def test_detail_shell_keeps_allow_list_metadata_and_strips_records():
    payload = _detail_payload()
    shell = persistence.detail_shell(
        payload, persistence.PersistenceMarker("detail", {}))
    assert shell["queryId"] == "qs-1"
    assert shell["totalRowCount"] == 57
    assert shell["cursor"] == "sig-cursor-token"
    assert shell["columns"] == ["orderId", "amount"]
    assert shell["redactedRowCount"] == 2
    assert "rows" not in shell
    # 非名单字段（可能携带原值）一律不进壳
    for key in ("citations", "sourceStates", "params"):
        assert key not in shell
    assert shell["persistence"]["mode"] == "ephemeral"
    assert shell["ephemeralDetail"] is True
    text = json.dumps(shell, ensure_ascii=False)
    assert "SO-1001" not in text and "机密值" not in text


def test_detail_shell_reference_only_allows_scalar_metadata():
    payload = _detail_payload()
    payload.pop("persistence", None)
    marker = persistence.PersistenceMarker("detail", {
        "cursor": "ref-cursor",
        "queryId": "qs-1",
        "rows": [{"orderId": "SO-1001"}],
        "params": {"x": "y"},
        "tags": ["a", 1],
    })
    shell = persistence.detail_shell(payload, marker)
    # reference 同样走 allow-list：非名单键（rows/params/tags）一律剔除
    assert shell["reference"] == {"cursor": "ref-cursor", "queryId": "qs-1"}


def test_aggregate_metadata_counts_only():
    payload = _detail_payload(rows=[{"region": "华东", "total": 10}])
    payload["persistence"] = _marker("aggregate")
    marker = persistence.parse_persistence_marker(payload)
    meta = persistence.aggregate_metadata(payload, marker)
    assert meta["totalRowCount"] == 57
    assert "rows" not in meta and "citations" not in meta


# ------------------------------------------------------------ 轮状态裁决


def test_register_local_output_without_marker_keeps_old_behavior():
    state = persistence.EphemeralTurnState()
    output = json.dumps({"hello": "world", "rows": [1, 2]}, ensure_ascii=False)
    assert state.register("ontology_query", "c1", output) == output
    assert not state.remote_seen and not state.detail_seen


def test_register_unhonored_tool_ignores_markers():
    state = persistence.EphemeralTurnState()
    output = json.dumps({"persistence": _marker("detail"), "rows": [1]})
    assert state.register("web_fetch", "c1", output) == output
    assert not state.remote_seen


def test_register_detail_marks_state_and_returns_shell():
    state = persistence.EphemeralTurnState()
    payload = _detail_payload()
    payload["persistence"] = _marker("detail")
    persisted = state.register(
        "ontology_query", "c1", json.dumps(payload, ensure_ascii=False))
    shell = json.loads(persisted)
    assert shell["ephemeralDetail"] is True
    assert "rows" not in shell
    assert state.detail_seen and state.remote_seen
    assert state.redactions["c1"] == persisted
    assert "SO-1001" not in persisted


def test_register_response_shaped_cursor_page_shells_detail_keeps_reference():
    """真实 ontology_adapter._response 形状的续读页（cursor + 受控
    reference）：壳保留 queryId/cursor/hasNext/totalRowCount 与 reference，
    行值与 citations/sourceStates/plan 一律不落。"""
    payload = {
        "queryId": "qs-page",
        "schemaVersion": 2,
        "observedAt": "2026-10-26T00:00:00+00:00",
        "releases": {"o1": {"dataMode": "remote"}},
        "columns": ["orderId", "amount"],
        "rows": [{"orderId": "SYNTHROW-9", "amount": 9.5}],
        "pageRowCount": 1,
        "totalRowCount": 41,
        "countsScope": "authorized_query_scope_at_source_snapshots",
        "hasNext": True,
        "cursor": "mem.cGFnZQ.sig-token",
        "expiresAt": "2026-10-26T00:05:00+00:00",
        "citations": [{"ontologyId": "o1", "sourceAlias": "leaf_0"}],
        "consistency": "cross_source_non_atomic",
        "sourceStates": [{"alias": "leaf_0"}],
        "plan": {"strategy": "bounded_federated_executor"},
        "countAvailability": {"distinctEntityCounts": "not_computed"},
        "persistence": {
            "mode": "ephemeral",
            "classification": "detail",
            "reference": {"queryId": "qs-page",
                          "expiresAt": "2026-10-26T00:05:00+00:00"},
        },
    }
    state = persistence.EphemeralTurnState()
    persisted = state.register(
        "ontology_query", "c1", json.dumps(payload, ensure_ascii=False))
    shell = json.loads(persisted)
    assert shell["ephemeralDetail"] is True
    assert shell["queryId"] == "qs-page"
    assert shell["cursor"] == "mem.cGFnZQ.sig-token"
    assert shell["hasNext"] is True and shell["totalRowCount"] == 41
    assert shell["redactedRowCount"] == 1
    assert shell["reference"] == {"queryId": "qs-page",
                                  "expiresAt": "2026-10-26T00:05:00+00:00"}
    assert "rows" not in shell
    assert "citations" not in shell and "plan" not in shell
    assert "SYNTHROW-9" not in persisted
    assert state.detail_seen and state.remote_seen


def test_register_aggregate_persists_output_and_records_metadata():
    state = persistence.EphemeralTurnState()
    payload = _detail_payload()
    payload["persistence"] = _marker("aggregate")
    output = json.dumps(payload, ensure_ascii=False)
    assert state.register("ontology_query", "c1", output) == output
    assert state.remote_seen and not state.detail_seen
    assert state.aggregates[0]["totalRowCount"] == 57
    assert "c1" not in state.redactions


def test_register_invalid_marker_fails_closed_as_detail():
    state = persistence.EphemeralTurnState()
    payload = _detail_payload()
    payload["persistence"] = {"mode": "ephemeral"}  # classification 缺失
    persisted = state.register(
        "ontology_query", "c1", json.dumps(payload, ensure_ascii=False))
    assert "SO-1001" not in persisted
    assert state.detail_seen and state.remote_seen


def test_register_marker_lost_after_remote_context_fails_closed():
    state = persistence.EphemeralTurnState()
    detail = _detail_payload()
    detail["persistence"] = _marker("detail")
    state.register("ontology_query", "c1", json.dumps(detail, ensure_ascii=False))
    # 同轮后续本地形状响应（无标记）：远程上下文已建立，按丢失处理
    local = json.dumps({"queryId": "qs-2", "rows": [{"orderId": "SO-9999"}]})
    persisted = state.register("ontology_query", "c2", local)
    assert "SO-9999" not in persisted
    assert json.loads(persisted)["ephemeralDetail"] is True


def test_register_broken_json_fail_closed_rules_by_trust():
    state = persistence.EphemeralTurnState()
    broken = '{"rows": [{"orderId": "SO-1"'  # 字符截断坏 JSON
    # 可信联邦工具（ontology_query 执行器只会返回 JSON 对象）的第一个
    # 输出即不可解析：无从证明不含明细，直接壳化，不落截断残文
    persisted = state.register("ontology_query", "c1", broken)
    assert "SO-1" not in persisted
    assert state.redactions["c1"] == persisted
    # 非可信工具（无标记解析权）保持旧行为：未建立远程上下文时原样返回
    fresh = persistence.EphemeralTurnState()
    assert fresh.register("web_fetch", "c1", broken) == broken
    # 远程上下文建立后，可信工具的坏 JSON 同样壳化（既有语义保持）
    detail = _detail_payload()
    detail["persistence"] = _marker("detail")
    state.register("ontology_query", "c2", json.dumps(detail))
    persisted = state.register("ontology_query", "c3", broken)
    assert "SO-1" not in persisted
    assert state.redactions["c3"] == persisted


def test_register_first_truncated_detail_output_fails_closed():
    """首个 ontology_query 输出被字符截断坏（rows 在前、marker 在尾、
    非行元数据超 cap 使 JSON 感知截断退化为 legacy 截断）：register 也
    必须壳化，不得把携带行值的截断残文当作本地输出落库。"""
    payload = {
        "queryId": "qs-t",
        "rows": [{"orderId": "SYNTHROW-1", "amount": 1.0},
                 {"orderId": "SYNTHROW-2", "amount": 2.0}],
        "planTransferMetrics": "M" * 31000,  # 非行元数据超 cap，无法 JSON 收缩
        "persistence": {"mode": "ephemeral", "classification": "detail"},
    }
    truncated = persistence.truncate_tool_output(
        json.dumps(payload, ensure_ascii=False), 30000)
    # 复现机制：legacy 截断破坏 JSON，残文含行值且丢失尾部 marker
    assert truncated.endswith("\n…[结果已截断]")
    assert "SYNTHROW-1" in truncated
    assert "persistence" not in truncated
    state = persistence.EphemeralTurnState()
    persisted = state.register("ontology_query", "c1", truncated, cap=30000)
    assert "SYNTHROW-1" not in persisted
    shell = json.loads(persisted)
    assert shell["ephemeralDetail"] is True
    assert state.detail_seen


def test_register_error_payload_in_remote_context_bounded_no_detail():
    state = persistence.EphemeralTurnState()
    detail = _detail_payload()
    detail["persistence"] = _marker("detail")
    state.register("ontology_query", "c1", json.dumps(detail))
    error_output = json.dumps({
        "error": "boom with SO-1001 row payload " + "x" * 300,
        "code": "remote_failure",
    }, ensure_ascii=False)
    persisted = state.register("ontology_query", "c2", error_output)
    shell = json.loads(persisted)
    assert shell["code"] == "remote_failure"
    assert len(shell["error"]) <= persistence._ERROR_MESSAGE_KEEP_CHARS + 20
    assert "x" * 200 not in persisted
    assert "SO-1001" not in persisted


def test_register_oversized_shell_falls_back_to_minimal_under_cap():
    state = persistence.EphemeralTurnState()
    payload = _detail_payload()
    payload["persistence"] = _marker("detail")
    payload["columns"] = ["col-%04d" % i for i in range(4000)]
    persisted = state.register(
        "ontology_query", "c1", json.dumps(payload), cap=400)
    assert len(persisted) <= 400
    shell = json.loads(persisted)
    assert shell["queryId"] == "qs-1"
    assert "SO-1001" not in persisted


# ------------------------------------------------------------ JSON 感知截断


def test_truncate_small_output_identity():
    assert persistence.truncate_tool_output('{"a": 1}', 100) == '{"a": 1}'


def test_truncate_json_output_keeps_counts_cursor_and_flags():
    rows = [{"orderId": "SO-%04d" % i, "amount": float(i)} for i in range(2000)]
    payload = _detail_payload(rows=rows)
    text = json.dumps(payload, ensure_ascii=False)
    cap = 6000
    trimmed = persistence.truncate_tool_output(text, cap)
    assert len(trimmed) <= cap
    doc = json.loads(trimmed)  # 必须仍是合法 JSON
    assert doc["totalRowCount"] == 57
    assert doc["cursor"] == "sig-cursor-token"
    assert doc["truncated"] is True
    assert doc["completeness"] != "complete"
    assert doc["rowsReturned"] == len(doc["rows"])
    assert doc["rowsOmitted"] == 2000 - len(doc["rows"])
    assert doc["rowsOmitted"] > 0


def test_truncate_non_json_output_keeps_legacy_behavior():
    text = "x" * 50
    out = persistence.truncate_tool_output(text, 10)
    assert out == "x" * 10 + "\n…[结果已截断]"


def test_truncate_json_without_record_arrays_keeps_legacy_behavior():
    text = json.dumps({"values": list(range(10000))})
    out = persistence.truncate_tool_output(text, 100)
    assert out.endswith("\n…[结果已截断]")


def test_bounded_error_text():
    text = "e" * 500
    bounded = persistence.bounded_error_text(text)
    assert bounded == "remote_execution_failed"
    assert persistence.bounded_error_text("short SO-1001") == bounded
    assert persistence.bounded_error_text(None) == bounded


# ------------------------------------------------------------ 答复壳


def test_assistant_answer_shell_contains_refs_and_aggregates_only():
    state = persistence.EphemeralTurnState()
    detail = _detail_payload()
    detail["persistence"] = _marker("detail")
    state.register("ontology_query", "c1", json.dumps(detail))
    aggregate = _detail_payload(rows=[{"region": "华东", "total": 3}])
    aggregate["persistence"] = _marker("aggregate")
    state.register("ontology_query", "c2", json.dumps(aggregate))
    shell_text = persistence.assistant_answer_shell(state)
    assert "远程联邦明细" in shell_text
    assert "qs-1" in shell_text and "totalRowCount" in shell_text
    assert "SO-1001" not in shell_text
