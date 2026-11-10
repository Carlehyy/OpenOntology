"""T3 回归：助手查询状态诚实化、有界恢复、同 owner 单飞、跨轮结构引用投影。

覆盖验收 B1/B3/B4/B6/B7/B8（t3 部分）。全部为确定性 L1/L2 证据：
隔离 sqlite + 伪造 provider/工具执行器，不触网、不依赖真实 PG/SSE 栈。

红阶段：本文件在基线 b7ccf202 上应出现失败/错误（B1 状态仍为 success、
B3 错误壳缺受控字段、B4 无恢复/无停止、B6 无单飞、B7 无投影），
B8 的“0 计数有效”护栏在旧实现即应保持绿。
"""
from __future__ import annotations

import importlib
import json
import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import RoleMenuPermission, User
from app.model_configs.models import ModelConfig
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import ontology_tools, persistence, runtime
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMcpServer,
    SuperAssistantMemory,
    SuperAssistantMemoryProfile,
    SuperAssistantMemorySetting,
    SuperAssistantMemorySource,
    SuperAssistantMessage,
    SuperAssistantMulticaConfig,
    SuperAssistantReflectionCandidate,
    SuperAssistantReflectionRun,
    SuperAssistantSkill,
    SuperAssistantToolRun,
    SuperAssistantToolSetting,
)

_RUNTIME_TABLES = [
    User.__table__, ModelConfig.__table__,
    RoleMenuPermission.__table__,
    SuperAssistantConversation.__table__, SuperAssistantSkill.__table__,
    SuperAssistantMcpServer.__table__, SuperAssistantMessage.__table__,
    SuperAssistantToolRun.__table__, SuperAssistantToolSetting.__table__,
    SuperAssistantMemory.__table__, SuperAssistantMulticaConfig.__table__,
    SuperAssistantMemoryProfile.__table__,
    SuperAssistantMemorySetting.__table__, SuperAssistantMemorySource.__table__, SuperAssistantReflectionRun.__table__,
    SuperAssistantReflectionCandidate.__table__,
]

_SECRET = "SO-1001-TRACE"


# --------------------------------------------------------------- helpers


def _seed(tmp_path, monkeypatch, name, *, user_content="查订单"):
    """隔离 sqlite + 伪造 provider 的会话种子（沿用既有 runtime 测试手法）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / (name + '.db')}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_RUNTIME_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", TestingSession)
    with TestingSession() as db:
        db.add(User(
            id=f"user-{name}", username=name, email=f"{name}@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(ModelConfig(
            id=f"model-{name}", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by=f"user-{name}",
        ))
        db.add(SuperAssistantConversation(
            id=f"conv-{name}", owner_id=f"user-{name}", title=name,
            model_config_id=f"model-{name}",
        ))
        db.add(SuperAssistantMessage(
            id=f"user-msg-{name}", conversation_id=f"conv-{name}",
            role="user", content=user_content, status="complete",
        ))
        db.add(SuperAssistantMessage(
            id=f"assistant-msg-{name}", conversation_id=f"conv-{name}",
            role="assistant", content="", status="streaming",
        ))
        db.commit()
    ids = {
        "conversation_id": f"conv-{name}",
        "owner_id": f"user-{name}",
        "assistant_message_id": f"assistant-msg-{name}",
        "requested_model_id": f"model-{name}",
    }
    return TestingSession, ids


def _stream_args(ids):
    return dict(
        conversation_id=ids["conversation_id"],
        owner_id=ids["owner_id"],
        assistant_message_id=ids["assistant_message_id"],
        requested_model_id=ids["requested_model_id"],
    )


def _fake_chat_stream(responses):
    def _fake(_call_kwargs, _messages, _tools, on_delta=None):
        result = next(responses)
        content = result.get("content")
        if content and on_delta:
            on_delta(content)
        return result

    return _fake


def _text(content, **usage):
    return {"content": content, "tool_calls": [], "usage": usage or {}}


def _tool_round(call_id, name="ontology_query", **arguments):
    return {
        "content": None,
        "tool_calls": [{"id": call_id, "name": name, "arguments": arguments}],
        "usage": {},
    }


def _parse_events(stream_text):
    events = []
    for block in stream_text.split("\n\n"):
        lines = [line for line in block.strip().splitlines() if line]
        if not lines:
            continue
        name = None
        data = None
        for line in lines:
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name is not None:
            events.append((name, data))
    return events


def _events_named(stream_text, event_name):
    return [data for name, data in _parse_events(stream_text) if name == event_name]


def _message_end(stream_text):
    payloads = _events_named(stream_text, "message_end")
    assert payloads, "缺少 message_end"
    return payloads[-1]["message"]


def _scripted_executor(outputs, calls):
    """按调用顺序返回脚本输出；最后一个输出在后续调用中重复。"""
    def _execute(name, arguments, _context):
        calls.append((name, dict(arguments)))
        index = min(len(calls) - 1, len(outputs) - 1)
        return outputs[index]

    return _execute


def _query_error(code, **extra):
    payload = {"error": "工具执行失败", "code": code}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _query_success(**extra):
    payload = {
        "queryId": "qs-1", "schemaVersion": 2,
        "hasNext": False, "released": True,
        "releasedAt": "2026-11-02T10:00:00+00:00",
        "expiresAt": "2026-11-02T10:00:00+00:00",
        "pageSize": 50, "totalRowCount": 0, "pageRowCount": 0,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _module(name):
    return importlib.import_module(f"app.super_assistant.{name}")


def _runs(db, ids):
    return (
        db.query(SuperAssistantToolRun)
        .filter(SuperAssistantToolRun.conversation_id == ids["conversation_id"])
        .order_by(SuperAssistantToolRun.created_at.asc())
        .all()
    )


# ------------------------------------------------------- B1 状态诚实化


def test_b1_builtin_query_error_payload_is_error_status(tmp_path, monkeypatch):
    """内置 ontology_query 受控 JSON error → 真实 error 状态与安全 code。

    SSE tool_result、message_end steps、持久 ToolRun 三处状态一致；
    error 列只落受控 code，不落异常原文。
    """
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b1-status")
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([
            _query_error("memory_reservation_exceeded",
                         quota="memory_per_user_bytes_limit"),
        ], calls),
    )
    responses = iter([_tool_round("c1", n=1), _text("查询暂不可用。")])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))

    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    results = _events_named(events, "tool_result")
    assert results and results[0]["status"] == "error"
    assert set(results[0]) == {"toolRunId", "status", "preview"}
    ending = _message_end(events)
    # 不新增响应字段：message_end.message 键集保持既有契约
    assert set(ending) == {"id", "content", "steps", "tokenUsage"}
    assert ending["steps"][0]["status"] == "error"
    with TestingSession() as db:
        run = _runs(db, ids)[0]
        assert run.status == "error"
        assert run.error == "memory_reservation_exceeded"
        assert _SECRET not in (run.error or "")


def test_b1_query_error_without_code_falls_back_to_controlled_code(
    tmp_path, monkeypatch,
):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b1-nocode")
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([
            json.dumps({"error": f"异常原文 {_SECRET}"}, ensure_ascii=False),
        ], calls),
    )
    responses = iter([_tool_round("c1", n=1), _text("失败。")])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert _events_named(events, "tool_result")[0]["status"] == "error"
    with TestingSession() as db:
        run = _runs(db, ids)[0]
        assert run.error == "ontology_tool_failed"
        assert _SECRET not in (run.error or "")


def test_b1_failure_code_detection_is_builtin_and_controlled():
    execution = _module("query_execution")
    detect = execution.query_failure_code
    assert detect(
        tool_name="ontology_query", is_builtin=True,
        output='{"error": "x", "code": "invalid_parameter"}',
    ) == "invalid_parameter"
    assert detect(
        tool_name="ontology_query", is_builtin=True,
        output='{"error": "x"}',
    ) == "ontology_tool_failed"
    assert detect(
        tool_name="ontology_query", is_builtin=True,
        output='{"error": "x", "code": "bad code with spaces"}',
    ) == "ontology_tool_failed"
    assert detect(
        tool_name="ontology_query", is_builtin=True,
        output=json.dumps({"error": "x", "code": "c" * 300}),
    ) == "ontology_tool_failed"
    # 仅内置 ontology_query：MCP/其他工具/成功/不可解析一律不改状态
    assert detect(
        tool_name="mcp__srv__tool", is_builtin=False,
        output='{"error": "x", "code": "invalid_parameter"}',
    ) is None
    assert detect(
        tool_name="memory_search", is_builtin=True,
        output='{"error": "x"}',
    ) is None
    assert detect(
        tool_name="ontology_query", is_builtin=True,
        output='{"totalRowCount": 0, "released": true}',
    ) is None
    assert detect(
        tool_name="ontology_query", is_builtin=True,
        output='{"rows": [',
    ) is None
    assert detect(
        tool_name="ontology_query", is_builtin=True, output="[1, 2]",
    ) is None


def test_b1_truncated_large_success_is_not_misjudged(tmp_path, monkeypatch):
    """成功大 JSON 被字符截断后不可解析：不得误判为失败。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b1-big")
    monkeypatch.setattr(settings, "super_assistant_tool_result_chars", 30000)
    huge = json.dumps({
        "queryId": "qs-big", "totalRowCount": 1,
        "rows": [{"orderId": "SO-1"}],
        "hugeMeta": "M" * 31000,
    }, ensure_ascii=False)
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([huge], calls),
    )
    responses = iter([_tool_round("c1", n=1), _text("完成。")])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert _events_named(events, "tool_result")[0]["status"] == "success"
    assert _message_end(events)["steps"][0]["status"] == "success"
    with TestingSession() as db:
        run = _runs(db, ids)[0]
        assert run.status == "success"
        assert run.error is None


def test_b1_mcp_error_payload_keeps_existing_status(tmp_path, monkeypatch):
    """MCP 工具返回同类 error JSON：状态语义保持原样（不套用本体查询判别）。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b1-mcp")
    with TestingSession() as db:
        db.add(SuperAssistantMcpServer(
            id="server-minio", owner_id=ids["owner_id"], name="platform_minio",
            builtin_key="minio", transport="streamable_http", url="builtin://minio",
            header_names=[], args=[], env_names=[], enabled=True,
            require_confirmation=False,
            tool_manifest=[{
                "name": "minio_upload_text",
                "description": "上传文本",
                "input_schema": {"type": "object", "properties": {}},
            }],
        ))
        db.commit()
    monkeypatch.setattr(
        runtime, "execute_minio_tool",
        lambda db, name, arguments, **kwargs: json.dumps(
            {"error": f"boom {_SECRET}"}, ensure_ascii=False),
    )
    responses = iter([
        _tool_round("c1", name="mcp__platform_minio__minio_upload_text",
                    bucket="b", key="k", content="x"),
        _text("上传失败。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert _events_named(events, "tool_result")[0]["status"] == "success"
    with TestingSession() as db:
        run = _runs(db, ids)[0]
        assert run.status == "success"
        assert run.error is None


def test_b1_other_builtin_error_payload_keeps_existing_status(
    tmp_path, monkeypatch,
):
    """其他内置工具（memory_search 参数错误）状态语义不变。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b1-other")
    responses = iter([
        _tool_round("c1", name="memory_search", query="   "),
        _text("检索参数不合法。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert _events_named(events, "tool_result")[0]["status"] == "success"
    with TestingSession() as db:
        assert _runs(db, ids)[0].status == "success"


# ------------------------------------------------- B3 错误壳受控字段


def _detail_payload(*, terminal=False, with_reference=True):
    payload = {
        "queryId": "qs-detail",
        "releases": {"o1": {"ontologyId": "o1", "releaseId": "r1",
                            "objectTypeId": "t1", "dataMode": "remote"}},
        "columns": ["orderId"],
        "rows": [{"orderId": _SECRET}],
        "pageRowCount": 1, "totalRowCount": 4,
        "hasNext": not terminal,
        "cursor": None if terminal else "cursor-token-0123456789",
        "pageSize": 1, "expiresAt": "2026-11-02T10:05:00+00:00",
        "persistence": {"mode": "ephemeral", "classification": "detail"},
    }
    if terminal:
        payload["released"] = True
        payload["releasedAt"] = "2026-11-02T10:00:30+00:00"
        payload["expiresAt"] = payload["releasedAt"]
    if with_reference:
        payload["persistence"]["reference"] = {
            "queryId": "qs-detail",
            "expiresAt": payload["expiresAt"],
        }
    return payload


def test_b3_remote_context_error_shell_keeps_only_controlled_fields(
    tmp_path, monkeypatch,
):
    """远程上下文 fail-closed 错误壳：code + 受控字段，无 message/原文/业务值。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b3-shell")
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([
            json.dumps(_detail_payload(), ensure_ascii=False),
            json.dumps({
                "error": f"remote exploded {_SECRET}",
                "code": "memory_quota_exceeded",
                "quota": "max_handles_per_user",
                "retryableAfter": 300,
                "maxPageSize": 50,
                "defaultPageSize": 50,
                "field": "pageSize",
                "message": f"异常原文 {_SECRET}",
                "limitBytes": 123456789,
                "requestedPageSize": 51,
            }, ensure_ascii=False),
        ], calls),
    )
    responses = iter([
        _tool_round("c1", n=1),
        _tool_round("c2", n=2),
        _text("远程查询失败。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))

    with TestingSession() as db:
        runs = _runs(db, ids)
        assert len(runs) == 2
        shell = json.loads(runs[1].result)
        assert shell["error"] == "remote_execution_failed"
        assert shell["code"] == "memory_quota_exceeded"
        assert shell["quota"] == "max_handles_per_user"
        assert shell["retryableAfter"] == 300
        assert shell["maxPageSize"] == 50
        assert shell["defaultPageSize"] == 50
        assert shell["field"] == "pageSize"
        for forbidden in ("message", "limitBytes", "requestedPageSize"):
            assert forbidden not in shell
        assert _SECRET not in runs[1].result
        # 持久错误列只落受控 code（不落异常原文/message）
        assert runs[1].error == "memory_quota_exceeded"
        # 首条 detail 壳仍不含行值；reference 键集保持不变
        first_shell = json.loads(runs[0].result)
        assert _SECRET not in runs[0].result
        assert first_shell["reference"] == {
            "queryId": "qs-detail",
            "expiresAt": "2026-11-02T10:05:00+00:00",
        }
        assert first_shell["pageSize"] == 1


def test_b3_error_shell_drops_invalid_controlled_field_types():
    state = persistence.EphemeralTurnState()
    detail = _detail_payload()
    state.register("ontology_query", "c1", json.dumps(detail, ensure_ascii=False))
    malicious = {
        "error": f"boom {_SECRET}",
        "code": "x" * 300,
        "quota": {"nested": _SECRET},
        "retryableAfter": "soon",
        "maxPageSize": 5000,
        "defaultPageSize": True,
        "field": {"bad": 1},
        "message": f"原文 {_SECRET}",
    }
    persisted = state.register(
        "ontology_query", "c2", json.dumps(malicious, ensure_ascii=False))
    shell = json.loads(persisted)
    assert shell["error"] == "remote_execution_failed"
    assert "code" not in shell
    for key in ("quota", "retryableAfter", "maxPageSize",
                "defaultPageSize", "field", "message"):
        assert key not in shell
    assert _SECRET not in persisted


def test_b3_terminal_detail_shell_has_release_fields_and_no_cursor_note():
    state = persistence.EphemeralTurnState()
    terminal = _detail_payload(terminal=True)
    persisted = state.register(
        "ontology_query", "c1", json.dumps(terminal, ensure_ascii=False))
    shell = json.loads(persisted)
    assert shell["released"] is True
    assert shell["releasedAt"] == "2026-11-02T10:00:30+00:00"
    assert shell["pageSize"] == 1
    assert "cursor" not in shell["note"]
    # 终页但释放未确认（released=false）：不谎称已释放，也不提示续读
    failed_release = _detail_payload(terminal=True)
    failed_release["released"] = False
    failed_shell = json.loads(persistence.EphemeralTurnState().register(
        "ontology_query", "c2", json.dumps(failed_release, ensure_ascii=False)))
    assert "cursor" not in failed_shell["note"]
    assert "释放未确认" in failed_shell["note"]
    # 非终页仍保留续读提示与既有壳语义
    fresh = persistence.EphemeralTurnState()
    middle = _detail_payload(terminal=False)
    middle_shell = json.loads(fresh.register(
        "ontology_query", "c1", json.dumps(middle, ensure_ascii=False)))
    assert "cursor" in middle_shell["note"]


def test_b3_minimal_shell_still_fail_closed_under_cap():
    state = persistence.EphemeralTurnState()
    payload = _detail_payload(terminal=True)
    payload["columns"] = ["col-%04d" % i for i in range(2000)]
    persisted = state.register(
        "ontology_query", "c1", json.dumps(payload), cap=400)
    assert len(persisted) <= 400
    shell = json.loads(persisted)
    assert set(shell) <= {
        "persistence", "ephemeralDetail", "note", "queryId",
        "hasNext", "cursor", "totalRowCount",
    }
    assert "pageSize" not in shell and "released" not in shell
    assert _SECRET not in persisted


# --------------------------------------------- B4 有界恢复（单元/集成）


def test_b4_recovery_classification_matrix():
    recovery = _module("query_recovery")
    cases = [
        ({"error": "x", "code": "memory_quota_exceeded",
          "quota": "max_handles_per_user"}, {}, "resource", False),
        ({"error": "x", "code": "memory_reservation_exceeded"}, {}, "resource", False),
        ({"error": "x", "code": "snapshot_quota"}, {}, "resource", False),
        ({"error": "x", "code": "query_snapshot_quota_exceeded"}, {}, "resource", False),
        ({"error": "x", "code": "query_id_in_use"}, {}, "resource", False),
        ({"error": "x", "code": "SOURCE_UNAVAILABLE"}, {}, "retryable", True),
        ({"error": "x", "code": "query_retry_required"}, {}, "retryable", True),
        ({"error": "x", "code": "SOURCE_UNAVAILABLE"}, {"cursor": "c" * 20},
         "terminal", False),
        ({"error": "x", "code": "invalid_cursor"}, {"cursor": "c" * 20},
         "terminal", False),
        ({"error": "x", "code": "release_context_changed"}, {}, "terminal", False),
        ({"error": "x", "code": "invalid_parameter"}, {}, "correctable", False),
        ({"error": "x", "code": "invalid_request_shape"}, {}, "correctable", False),
        ({"error": "x", "code": "ontology_tool_failed"}, {}, "terminal", False),
        ({"error": "x"}, {}, "terminal", False),
    ]
    for payload, arguments, classification, retryable in cases:
        info = recovery.classify_payload(payload, arguments=arguments)
        assert info is not None
        assert info.classification == classification, payload
        assert info.retryable is retryable, payload
    assert recovery.classify_payload({"totalRowCount": 0}, arguments={}) is None
    assert recovery.classify_payload("not-a-dict", arguments={}) is None


def test_b4_signature_thresholds_are_bounded_and_cursor_scoped():
    recovery = _module("query_recovery")
    state = recovery.QueryRecoveryState()
    cursor_a = {"cursor": "cursor-a-0123456789"}
    cursor_b = {"cursor": "cursor-b-0123456789"}

    assert state.block_reason(cursor_a) is None
    state.record_failure({"error": "x", "code": "invalid_cursor"}, cursor_a)
    assert state.block_reason(cursor_b) is None  # 不同合法续页不合并
    state.record_failure({"error": "x", "code": "invalid_cursor"}, cursor_b)
    assert state.block_reason(cursor_a) is None  # 各 1 次，未达 R1/R4 阈值 2
    state.record_failure({"error": "x", "code": "invalid_cursor"}, cursor_a)
    blocked = state.block_reason(cursor_a)
    assert blocked and blocked["code"] == "query_retry_suppressed"
    assert state.block_reason(cursor_b) is None
    state.record_failure({"error": "x", "code": "invalid_cursor"}, cursor_b)
    assert state.block_reason(cursor_b) is not None

    correctable = {"sources": [{"alias": "s"}]}
    other = recovery.QueryRecoveryState()
    for _ in range(3):
        info = other.record_failure(
            {"error": "x", "code": "invalid_parameter"}, correctable)
    assert info.attempts == 3
    assert other.block_reason(correctable) is not None


def test_b4_success_clears_matching_failure_count_only():
    recovery = _module("query_recovery")
    state = recovery.QueryRecoveryState()
    args = {"cursor": "cursor-x-0123456789"}
    state.record_failure({"error": "x", "code": "invalid_cursor"}, args)
    state.record_failure({"error": "x", "code": "invalid_cursor"}, args)
    assert state.block_reason(args) is not None
    state.record_success(args, {"hasNext": False, "released": True})
    assert state.block_reason(args) is None
    info = state.record_failure({"error": "x", "code": "invalid_cursor"}, args)
    assert info.attempts == 1
    # 不同参数的成功不清除其它请求的失败计数
    state.record_failure({"error": "x", "code": "invalid_cursor"}, args)
    state.record_success({"cursor": "cursor-y-0123456789"}, {})
    assert state.block_reason(args) is not None


def test_b4_handle_saturation_blocks_new_queries_until_release():
    recovery = _module("query_recovery")
    state = recovery.QueryRecoveryState()
    new_query = {"sources": [{"alias": "s"}]}
    cursor = {"cursor": "cursor-1-0123456789"}
    state.record_failure(
        {"error": "x", "code": "memory_quota_exceeded",
         "quota": "max_handles_per_user"}, new_query)
    blocked = state.block_reason({"sources": [{"alias": "s2"}]})
    assert blocked and blocked["code"] == "query_resource_blocked"
    assert blocked["quota"] == "max_handles_per_user"
    assert state.block_reason(cursor) is None  # 已有 cursor 仍可续读
    # 真实终页释放事件才解除
    state.record_success(cursor, {"hasNext": False, "released": True})
    assert state.block_reason({"sources": [{"alias": "s3"}]}) is None


def test_b4_reservation_and_id_conflict_do_not_block_all_work():
    recovery = _module("query_recovery")
    state = recovery.QueryRecoveryState()
    state.record_failure(
        {"error": "x", "code": "memory_reservation_exceeded"}, {"sources": []})
    state.record_failure(
        {"error": "x", "code": "query_id_in_use"}, {"sources": []})
    assert state.block_reason({"sources": [{"alias": "new"}]}) is None


def test_b4_no_progress_counter_bounded_at_eight_rounds():
    recovery = _module("query_recovery")
    state = recovery.QueryRecoveryState()
    state.record_failure(
        {"error": "x", "code": "invalid_parameter"}, {"n": 1})
    for _ in range(7):
        assert state.note_round(had_attempts=True, had_valid_success=False) is False
    assert state.note_round(had_attempts=True, had_valid_success=False) is True
    assert state.stopped
    text = state.honest_failure_text()
    assert "未取得有效成功" in text and "invalid_parameter" in text
    assert "重新" in text
    # 有效成功/零值成功清零；无工具调用的轮次不增加
    fresh = recovery.QueryRecoveryState()
    for _ in range(7):
        fresh.note_round(had_attempts=True, had_valid_success=False)
    fresh.note_round(had_attempts=False, had_valid_success=False)
    assert fresh.note_round(had_attempts=True, had_valid_success=True) is False
    assert fresh.no_progress_rounds == 0


def test_b4_sleep_cancellable_respects_cancellation():
    recovery = _module("query_recovery")
    started = time.monotonic()
    assert recovery.sleep_cancellable(5.0, cancelled=lambda: True) is False
    assert time.monotonic() - started < 1.0
    assert recovery.sleep_cancellable(0.0) is True


def test_b4_handle_saturation_release_recovers_statistics(
    tmp_path, monkeypatch,
):
    """句柄饱和：不持续新建；已有 cursor 续读到终页释放后可完成统计。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b4-handles")
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([
            _query_error("memory_quota_exceeded", quota="max_handles_per_user"),
            _query_success(pageSize=1),
            _query_success(totalRowCount=0, distinctEntityCounts={"o1": 0}),
        ], calls),
    )
    responses = iter([
        _tool_round("c1", n=1),
        _tool_round("c2", n=2),
        _tool_round("c3", cursor="cursor-old-0123456789"),
        _tool_round("c4", n=4),
        _text("统计完成：0 条。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))

    assert [arguments for _name, arguments in calls] == [
        {"n": 1},
        {"cursor": "cursor-old-0123456789"},
        {"n": 4},
    ]
    with TestingSession() as db:
        runs = _runs(db, ids)
        assert len(runs) == 4
        assert runs[0].status == "error"
        suppressed = json.loads(runs[1].result)
        assert suppressed["code"] == "query_resource_blocked"
        assert runs[1].status == "error"
        assert runs[2].status == "success"  # 续页终页
        assert runs[3].status == "success"  # 释放后新统计
    assert _message_end(events)["content"] == "统计完成：0 条。"


def test_b4_transient_resource_conflict_does_not_block_unrelated_queries(
    tmp_path, monkeypatch,
):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b4-conflict")
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([
            _query_error("memory_reservation_exceeded",
                         quota="memory_per_user_bytes_limit"),
            _query_success(totalRowCount=1),
            _query_error("query_id_in_use"),
            _query_success(totalRowCount=2),
        ], calls),
    )
    responses = iter([
        _tool_round("c1", n=1),
        _tool_round("c2", n=2),
        _tool_round("c3", n=3),
        _tool_round("c4", n=4),
        _text("两次统计已完成。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert len(calls) == 4  # 一次并发/字节错误 + ID 冲突都不封锁其他查询
    with TestingSession() as db:
        statuses = [run.status for run in _runs(db, ids)]
        assert statuses == ["error", "success", "error", "success"]
    assert _message_end(events)["content"] == "两次统计已完成。"


def test_b4_logged_out_cursor_is_not_retried_blindly(
    tmp_path, monkeypatch,
):
    """已注销 cursor：不自动重试；同签名阈值后拒绝；新查询重新授权执行。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b4-cursor")
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([
            _query_error("SOURCE_UNAVAILABLE"),
            _query_error("SOURCE_UNAVAILABLE"),
            _query_success(totalRowCount=1),
        ], calls),
    )
    cursor = "cursor-revoked-0123456789"
    responses = iter([
        _tool_round("c1", cursor=cursor),
        _tool_round("c2", cursor=cursor),
        _tool_round("c3", cursor=cursor),
        _tool_round("c4", n=1, releaseId="r-new"),
        _text("已按新发布重新查询。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert [arguments for _name, arguments in calls] == [
        {"cursor": cursor},
        {"cursor": cursor},
        {"n": 1, "releaseId": "r-new"},
    ]
    with TestingSession() as db:
        runs = _runs(db, ids)
        assert runs[0].status == "error"
        assert json.loads(runs[1].result)["code"] == "SOURCE_UNAVAILABLE"
        assert json.loads(runs[2].result)["code"] == "query_retry_suppressed"
        assert runs[3].status == "success"
    assert _message_end(events)["content"] == "已按新发布重新查询。"


def test_b4_short_retry_backoff_is_bounded_and_heartbeats(
    tmp_path, monkeypatch,
):
    """R2：非 cursor 安全只读短故障最多 2 次重试（0.5s/1.5s），保持心跳。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b4-retry")
    recovery = _module("query_recovery")
    delays: list[float] = []

    def fake_sleep(delay, cancelled=None):
        delays.append(delay)
        time.sleep(0.12)  # 覆盖既有 100ms 心跳窗口，不跨真实长等待
        return True

    monkeypatch.setattr(recovery, "sleep_cancellable", fake_sleep)
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([
            _query_error("query_retry_required"),
            _query_error("query_retry_required"),
            _query_success(totalRowCount=0),
        ], calls),
    )
    responses = iter([_tool_round("c1", n=1), _text("重试后完成。")])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))

    assert len(calls) == 3
    assert delays == [0.5, 1.5]
    assert ": tool heartbeat" in events
    with TestingSession() as db:
        runs = _runs(db, ids)
        assert len(runs) == 1  # 重试不新建 ToolRun
        assert runs[0].status == "success"
        assert json.loads(runs[0].result)["totalRowCount"] == 0
    assert _message_end(events)["content"] == "重试后完成。"


def test_b4_short_retry_stops_when_cancelled(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b4-retry-cancel")
    recovery = _module("query_recovery")
    monkeypatch.setattr(recovery, "sleep_cancellable",
                        lambda delay, cancelled=None: False)
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([_query_error("SOURCE_UNAVAILABLE")], calls),
    )
    responses = iter([_tool_round("c1", n=1), _text("查询暂不可用。")])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))
    assert len(calls) == 1
    with TestingSession() as db:
        runs = _runs(db, ids)
        assert runs[0].status == "error"
        assert json.loads(runs[0].result)["code"] == "SOURCE_UNAVAILABLE"


def test_b4_same_signature_stops_after_three_correctable_failures(
    tmp_path, monkeypatch,
):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b4-threshold3")
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([_query_error("invalid_parameter")], calls),
    )
    responses = iter([
        *[_tool_round(f"c{i}", n=1) for i in range(6)],
        _text("参数仍不合法。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert len(calls) == 3  # 同签名第 4 次起拒绝继续执行
    with TestingSession() as db:
        runs = _runs(db, ids)
        assert [run.status for run in runs[:3]] == ["error", "error", "error"]
        assert all(
            json.loads(run.result)["code"] == "query_retry_suppressed"
            for run in runs[3:]
        )
    assert _message_end(events)["content"] == "参数仍不合法。"


def test_b4_same_cursor_stops_after_two_terminal_failures(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b4-threshold2")
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([_query_error("invalid_cursor")], calls),
    )
    cursor = "cursor-bad-0123456789"
    responses = iter([
        *[_tool_round(f"c{i}", cursor=cursor) for i in range(4)],
        _text("cursor 已失效。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))
    assert len(calls) == 2
    with TestingSession() as db:
        runs = _runs(db, ids)
        assert runs[0].status == "error" and runs[1].status == "error"
        assert json.loads(runs[2].result)["code"] == "query_retry_suppressed"
        assert json.loads(runs[3].result)["code"] == "query_retry_suppressed"


def test_b4_success_clears_failure_count_in_sequence(tmp_path, monkeypatch):
    """失败-失败-成功-失败：成功清除对应请求失败计数（第 6 次才拒绝）。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b4-clear")
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([
            _query_error("invalid_parameter"),
            _query_error("invalid_parameter"),
            _query_success(totalRowCount=3),
            _query_error("invalid_parameter"),
            _query_error("invalid_parameter"),
            _query_error("invalid_parameter"),
            _query_error("invalid_parameter"),  # 第 6 次：同签名 3 连败后拒绝
        ], calls),
    )
    responses = iter([
        *[_tool_round(f"c{i}", n=1) for i in range(7)],
        _text("查询成功过，但随后参数又失败。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))
    assert len(calls) == 6
    with TestingSession() as db:
        runs = _runs(db, ids)
        assert runs[2].status == "success"
        assert runs[6].status == "error"
        assert json.loads(runs[6].result)["code"] == "query_retry_suppressed"


def test_b4_no_progress_eight_rounds_stops_with_honest_failure(
    tmp_path, monkeypatch,
):
    """连续 8 轮无有效成功：停止循环，最终答复如实说明且不伪造结果。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b4-noprogress")
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([_query_error("invalid_parameter")], calls),
    )
    responses = iter([
        *[_tool_round(f"c{i}", n=i + 1) for i in range(8)],
        _text("已完成统计：总计 0 条。"),  # 不应被调用/发布
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert len(calls) == 8
    ending = _message_end(events)
    assert "未取得有效成功" in ending["content"]
    assert "invalid_parameter" in ending["content"]
    assert "已完成统计" not in ending["content"]
    assert len(ending["steps"]) == 8
    assert {step["status"] for step in ending["steps"]} == {"error"}
    with TestingSession() as db:
        message = db.get(SuperAssistantMessage, ids["assistant_message_id"])
        assert "未取得有效成功" in (message.content or "")


# ------------------------------------------------------------ B6 单飞


def test_b6_same_owner_query_single_flight_unit():
    execution = _module("query_execution")
    owner = "owner-single"
    events = {"active": [], "max": 0, "started": threading.Event(),
              "release": threading.Event(), "both": threading.Event()}
    lock = threading.Lock()

    def enter(tag):
        with lock:
            events["active"].append(tag)
            events["max"] = max(events["max"], len(events["active"]))

    def leave(tag):
        with lock:
            events["active"].remove(tag)

    def holder():
        with execution.owner_query_single_flight(owner):
            enter("a")
            events["started"].set()
            events["release"].wait(3)
            leave("a")

    def waiter():
        with execution.owner_query_single_flight(owner):
            enter("b")
            leave("b")

    first = threading.Thread(target=holder)
    first.start()
    assert events["started"].wait(3)
    second = threading.Thread(target=waiter)
    second.start()
    time.sleep(0.1)
    assert events["active"] == ["a"]  # 同 owner 第二个等待
    events["release"].set()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert events["max"] == 1
    assert execution.active_owner_count() == 0


def test_b6_different_owners_do_not_serialize():
    execution = _module("query_execution")
    both = threading.Event()
    release = threading.Event()
    state = {"active": 0, "max": 0}
    guard = threading.Lock()

    def hold(owner):
        with execution.owner_query_single_flight(owner):
            with guard:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
                if state["active"] == 2:
                    both.set()
            release.wait(3)
            with guard:
                state["active"] -= 1

    threads = [threading.Thread(target=hold, args=(f"owner-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    assert both.wait(3)  # 不同 owner 可同时进入
    release.set()
    for thread in threads:
        thread.join(3)
    assert state["max"] == 2
    assert execution.active_owner_count() == 0


def test_b6_lock_cleanup_on_exception_and_waiter_cancellation():
    execution = _module("query_execution")
    with pytest.raises(RuntimeError):
        with execution.owner_query_single_flight("owner-exc"):
            raise RuntimeError("boom")
    assert execution.active_owner_count() == 0

    holder_ready = threading.Event()
    release = threading.Event()
    cancel = threading.Event()
    caught: list[BaseException] = []

    def holder():
        with execution.owner_query_single_flight("owner-cancel"):
            holder_ready.set()
            release.wait(3)

    def cancelled_waiter():
        try:
            with execution.owner_query_single_flight(
                "owner-cancel", cancelled=cancel.is_set,
            ):
                pass
        except BaseException as exc:  # noqa: BLE001 - 测试记录取消异常
            caught.append(exc)

    first = threading.Thread(target=holder)
    first.start()
    assert holder_ready.wait(3)
    second = threading.Thread(target=cancelled_waiter)
    second.start()
    time.sleep(0.1)
    cancel.set()
    second.join(3)
    release.set()
    first.join(3)
    assert caught and isinstance(caught[0], execution.QueryWaitCancelled)
    assert not second.is_alive()
    assert execution.active_owner_count() == 0


def test_b6_runtime_query_single_flight_keeps_other_read_only_parallel(
    tmp_path, monkeypatch,
):
    """同 owner 两个 ontology_query 最大并发 1；web_fetch 与首个查询并行。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b6-single")
    state = {
        "active_queries": 0, "max_queries": 0, "overlap": False,
        "fetch_started": threading.Event(), "guard": threading.Lock(),
    }

    def fake_builtin(db, *, name, arguments, **_kwargs):
        if name == "ontology_query":
            with state["guard"]:
                if state["active_queries"]:
                    state["overlap"] = True
                state["active_queries"] += 1
                state["max_queries"] = max(
                    state["max_queries"], state["active_queries"])
            try:
                state["fetch_started"].wait(3)  # 等 web_fetch 开始，证明真并行
                return _query_success(totalRowCount=0)
            finally:
                with state["guard"]:
                    state["active_queries"] -= 1
        if name == "web_fetch":
            state["fetch_started"].set()
            return json.dumps({"results": []}, ensure_ascii=False)
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(runtime, "_execute_builtin_tool", fake_builtin)
    responses = iter([
        {
            "content": None,
            "tool_calls": [
                {"id": "c1", "name": "ontology_query", "arguments": {"sources": []}},
                {"id": "c2", "name": "ontology_query", "arguments": {"sources": []}},
                {"id": "c3", "name": "web_fetch", "arguments": {"url": "https://example.com"}},
            ],
            "usage": {},
        },
        _text("完成。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))
    assert state["overlap"] is False
    assert state["max_queries"] == 1
    with TestingSession() as db:
        runs = {run.call_id: run for run in _runs(db, ids)}
        assert runs["c1"].status == "success"
        assert runs["c2"].status == "success"
        assert runs["c3"].status == "success"
    assert _module("query_execution").active_owner_count() == 0


def test_b6_runtime_query_lock_released_when_execution_raises(
    tmp_path, monkeypatch,
):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b6-exc")

    def fake_builtin(db, *, name, arguments, **_kwargs):
        if arguments.get("fail"):
            raise RuntimeError("query backend crashed")
        return _query_success(totalRowCount=1)

    monkeypatch.setattr(runtime, "_execute_builtin_tool", fake_builtin)
    responses = iter([
        {
            "content": None,
            "tool_calls": [
                {"id": "c1", "name": "ontology_query",
                 "arguments": {"sources": [], "fail": True}},
                {"id": "c2", "name": "ontology_query",
                 "arguments": {"sources": []}},
            ],
            "usage": {},
        },
        _text("一个失败一个成功。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))
    with TestingSession() as db:
        runs = {run.call_id: run for run in _runs(db, ids)}
        assert runs["c1"].status == "error"
        assert runs["c2"].status == "success"
    execution = _module("query_execution")
    assert execution.active_owner_count() == 0


def test_b6_unattended_does_not_reject_read_only_query(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b6-unattended")
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([_query_success(totalRowCount=1)], calls),
    )
    responses = iter([_tool_round("c1", n=1), _text("完成。")])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids), unattended=True))
    assert len(calls) == 1
    with TestingSession() as db:
        assert _runs(db, ids)[0].status == "success"


# --------------------------------------------------- B7 跨轮结构引用投影


def _projection_record(tool_run_id, payload, *, tool="ontology_query",
                       created_at="2026-11-02T10:00:00+00:00"):
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False)
    return {"toolName": tool, "result": payload,
            "toolRunId": tool_run_id, "createdAt": created_at}


def test_b7_projection_is_allow_list_and_bounded():
    refs_module = _module("context_refs")
    records = [
        _projection_record("run-new", {
            "releases": {"a": {"ontologyId": "o1", "releaseId": "r1",
                              "objectTypeId": "t1", "dataMode": "remote",
                              "rows": [{"orderId": _SECRET}]}},
            "rows": [{"orderId": _SECRET}],
            "params": {"filter": _SECRET},
            "citations": [{"title": _SECRET}],
            "cursor": "cursor-secret-0123456789",
            "totalRowCount": 99,
            "_SECRET": _SECRET,
        }),
        _projection_record("run-old", {
            "releases": {"a": {"ontologyId": "o1", "releaseId": "r1",
                              "objectTypeId": "t1"}},
        }),
        _projection_record("run-local", {
            "releases": {"s": {"ontologyId": "o2", "releaseId": "r2",
                              "versionNumber": 3}},
        }),
        _projection_record("run-bad", {
            "releases": {
                "bad": {"ontologyId": {"nested": _SECRET}, "releaseId": "r3"},
                "long": {"ontologyId": "o3", "releaseId": "r" * 500},
                "mode": {"ontologyId": "o4", "releaseId": "r4",
                         "dataMode": "unexpected"},
            },
        }),
        _projection_record("run-structure", {
            "objectTypes": [{"sourceRefs": {"ontologyId": "o5",
                                            "releaseId": "r5",
                                            "objectTypeId": "t5"}}],
            "linkTypes": [{"sourceRefs": {"ontologyId": "o6",
                                          "releaseId": "r6",
                                          "linkTypeId": "l6"}}],
        }, tool="ontology_structure"),
        _projection_record("run-catalog", {
            "ontologyId": "o7", "releaseId": "r7",
            "items": [{"id": "t7"}],
        }, tool="ontology_catalog"),
    ]
    refs = refs_module.project_tool_runs(records)
    assert len(refs) == len({(ref["ontologyId"], ref["releaseId"],
                              ref.get("objectTypeId", "")) for ref in refs})
    keys = {(ref["ontologyId"], ref["releaseId"], ref.get("objectTypeId"))
            for ref in refs}
    assert ("o1", "r1", "t1") in keys
    assert ("o2", "r2", None) in keys
    assert ("o5", "r5", "t5") in keys
    assert ("o6", "r6", None) in keys  # linkTypeId 不在 allow-list，丢字段不丢本体
    assert ("o7", "r7", None) in keys
    for ref in refs:
        if ref["ontologyId"] == "o1":
            assert ref["sourceToolRunId"] == "run-new"
        assert ref["sourceTool"] in {
            "ontology_query", "ontology_structure", "ontology_catalog"}
        assert set(ref) <= {
            "ontologyId", "releaseId", "objectTypeId", "versionNumber",
            "dataMode", "resolvedAt", "sourceTool", "sourceToolRunId",
        }
        assert not any(
            isinstance(value, str) and _SECRET in value for value in ref.values())
    # 去重保留最新（run-new 覆盖 run-old）
    o1 = [ref for ref in refs if ref["ontologyId"] == "o1"]
    assert len(o1) == 1 and o1[0]["sourceToolRunId"] == "run-new"
    # 越界/恶意字段不进入投影
    assert all(ref["ontologyId"] not in {"o3", "o4"} for ref in refs)
    assert "unexpected" not in json.dumps(refs)
    section = refs_module.render_structure_refs(refs)
    assert len(section) <= refs_module.MAX_SECTION_CHARS
    assert _SECRET not in section
    assert "cursor-secret" not in section
    assert "重新" in section


def test_b7_projection_caps_refs_and_section_size():
    refs_module = _module("context_refs")
    records = [
        _projection_record("run-many", {
            "releases": {
                f"a{i}": {"ontologyId": f"o{i}", "releaseId": f"r{i}",
                          "objectTypeId": "t" * 120}
                for i in range(60)
            },
        }),
    ]
    refs = refs_module.project_tool_runs(records)
    assert len(refs) == refs_module.MAX_REFS == 32
    section = refs_module.render_structure_refs(refs)
    assert len(section) <= refs_module.MAX_SECTION_CHARS == 4000
    # 只读最近 20 条 ToolRun（输入按新→旧）；第 21 条起不读
    many_records = [
        _projection_record(f"run-{index}", {
            "releases": {"a": {"ontologyId": f"only-{index}",
                              "releaseId": "r1"}},
        })
        for index in range(25)
    ]
    projected = refs_module.project_tool_runs(many_records)
    ids = {ref["ontologyId"] for ref in projected}
    assert "only-0" in ids and "only-19" in ids and "only-20" not in ids


def test_b7_authorization_probe_is_required_and_marks_stale(
    tmp_path, monkeypatch,
):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b7-auth")
    probe_calls: list[str] = []

    def fake_object_types(db, user, *, ontology_id, release_id=None, cursor=None):
        probe_calls.append(ontology_id)
        if ontology_id == "o-denied":
            raise ontology_tools.FacadeError(403, "profile_disabled", "denied")
        return {"ontologyId": ontology_id, "releaseId": "r-current"}

    monkeypatch.setattr(ontology_tools, "catalog_object_types", fake_object_types)
    monkeypatch.setattr(
        ontology_tools, "current_release_map",
        lambda db, ontology_ids: {oid: "r-current" for oid in ontology_ids},
    )
    refs = [
        {"ontologyId": "o-ok", "releaseId": "r-current"},
        {"ontologyId": "o-stale", "releaseId": "r-old"},
        {"ontologyId": "o-denied", "releaseId": "r-current"},
    ]
    with TestingSession() as db:
        authorized = ontology_tools.authorize_structure_refs(
            db, owner_id=ids["owner_id"],
            conversation_id=ids["conversation_id"], refs=refs)
    by_id = {ref["ontologyId"]: ref for ref in authorized}
    assert set(by_id) == {"o-ok", "o-stale"}  # 撤权/不可访问不注入
    assert by_id["o-ok"]["stale"] is False
    assert by_id["o-stale"]["stale"] is True
    # 未授权时发布比较本身不能授予访问
    monkeypatch.setattr(
        ontology_tools, "current_release_map",
        lambda db, ontology_ids: {oid: "r-current" for oid in ontology_ids},
    )

    def deny_all(db, user, *, ontology_id, release_id=None, cursor=None):
        raise ontology_tools.FacadeError(403, "MENU_ACCESS_DENIED", "denied")

    monkeypatch.setattr(ontology_tools, "catalog_object_types", deny_all)
    with TestingSession() as db:
        assert ontology_tools.authorize_structure_refs(
            db, owner_id=ids["owner_id"],
            conversation_id=ids["conversation_id"],
            refs=[{"ontologyId": "o-ok", "releaseId": "r-current"}]) == []


def test_b7_authorization_requires_current_owner_and_conversation(
    tmp_path, monkeypatch,
):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b7-owner")
    called: list[str] = []

    def fake_object_types(db, user, *, ontology_id, release_id=None, cursor=None):
        called.append(ontology_id)
        return {"ontologyId": ontology_id, "releaseId": "r1"}

    monkeypatch.setattr(ontology_tools, "catalog_object_types", fake_object_types)
    monkeypatch.setattr(ontology_tools, "current_release_map",
                        lambda db, ontology_ids: {oid: "r1" for oid in ontology_ids})
    with TestingSession() as db:
        assert ontology_tools.authorize_structure_refs(
            db, owner_id=ids["owner_id"],
            conversation_id="conversation-other",
            refs=[{"ontologyId": "o1", "releaseId": "r1"}]) == []
        assert called == []


def test_b7_cross_turn_projection_in_system_prompt(tmp_path, monkeypatch):
    """最近会话历史结构引用注入系统提示；他会话/恶意字段/明细原文不注入。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "b7-runtime")
    with TestingSession() as db:
        db.add(User(
            id="user-other", username="other", email="other@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(SuperAssistantConversation(
            id="conv-other", owner_id="user-other", title="other",
            model_config_id=ids["requested_model_id"],
        ))
        db.add(SuperAssistantMessage(
            id="user-msg-other", conversation_id="conv-other",
            role="user", content="其他会话", status="complete",
        ))
        db.add(SuperAssistantMessage(
            id="assistant-msg-other", conversation_id="conv-other",
            role="assistant", content="", status="streaming",
        ))
        db.add_all([
            SuperAssistantToolRun(
                id="run-ok", conversation_id=ids["conversation_id"],
                assistant_message_id=ids["assistant_message_id"],
                call_id="old-1", tool_name="ontology_query", status="success",
                arguments={}, result=json.dumps({
                    "queryId": "qs-old",
                    "releases": {"a": {"ontologyId": "o-ok", "releaseId": "r-1",
                                      "objectTypeId": "t-ok",
                                      "dataMode": "remote"}},
                    "rows": [{"orderId": _SECRET}],
                    "params": {"filter": _SECRET},
                    "_SECRET": _SECRET,
                }, ensure_ascii=False),
            ),
            SuperAssistantToolRun(
                id="run-other-conv", conversation_id="conv-other",
                assistant_message_id="assistant-msg-other",
                call_id="old-2", tool_name="ontology_query", status="success",
                arguments={}, result=json.dumps({
                    "releases": {"b": {"ontologyId": "o-other", "releaseId": "r-2"}},
                }, ensure_ascii=False),
            ),
        ])
        db.commit()

    def fake_object_types(db, user, *, ontology_id, release_id=None, cursor=None):
        return {"ontologyId": ontology_id, "releaseId": "r-1",
                "items": [{"id": "t-ok"}], "hasNext": False, "cursor": None}

    monkeypatch.setattr(ontology_tools, "catalog_object_types", fake_object_types)
    monkeypatch.setattr(ontology_tools, "current_release_map",
                        lambda db, ontology_ids: {oid: "r-1" for oid in ontology_ids})
    captured: list[dict] = []

    def capture(_kwargs, messages, _tools, on_delta=None):
        captured.append({"messages": messages})
        return _text("本轮答复。")

    monkeypatch.setattr(runtime.provider, "chat_stream", capture)
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert _message_end(events)["content"] == "本轮答复。"
    system = captured[0]["messages"][0]["content"]
    assert "历史结构引用" in system
    assert "o-ok" in system and "r-1" in system and "t-ok" in system
    assert "o-other" not in system  # 他会话不注入
    assert _SECRET not in system  # 明细/恶意字段不注入
    section_start = system.index("【本会话历史结构引用")
    section = system[section_start:]
    assert "cursor" not in section.lower()
    assert len(section) <= 4200

    # 投影不写持久面：本轮 assistant 内容只有模型答复
    with TestingSession() as db:
        saved = db.get(SuperAssistantMessage, ids["assistant_message_id"])
        assert saved.content == "本轮答复。"


# ------------------------------------------------ B8 统计正数/正确 0


@pytest.mark.parametrize("total", [0, 3])
def test_b8_zero_and_positive_counts_are_valid_success(tmp_path, monkeypatch, total):
    """确定性 fixture：0 与正数都是有效成功/进展，不触发重试或停止。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, f"b8-{total}")
    calls = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([
            _query_success(totalRowCount=total,
                           distinctEntityCounts={"o1": total}),
        ], calls),
    )
    responses = iter([_tool_round("c1", n=1), _text(f"共 {total} 条。")])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert len(calls) == 1  # 0 值不重试
    assert _events_named(events, "tool_result")[0]["status"] == "success"
    ending = _message_end(events)
    assert ending["content"] == f"共 {total} 条。"
    assert ending["steps"][0]["status"] == "success"
    with TestingSession() as db:
        run = _runs(db, ids)[0]
        assert run.status == "success"
        shell = json.loads(run.result)
        assert shell["totalRowCount"] == total



# ------------------------------------- F2/F3 repair-round-2 回归（t11）


def _multi_tool_round(round_index: int, *, todo: bool = True,
                      think: bool = True) -> dict:
    calls = []
    if todo:
        calls.append({"id": f"todo-{round_index}", "name": "todo_write",
                      "arguments": {"items": [f"step {round_index}"]}})
    if think:
        calls.append({"id": f"think-{round_index}", "name": "think",
                      "arguments": {"thought": f"round {round_index}"}})
    calls.append({"id": f"query-{round_index}", "name": "ontology_query",
                  "arguments": {"sources": [{"alias": "a"}],
                                "round": round_index}})
    return {"content": None, "tool_calls": calls, "usage": {}}


def test_f2_unrelated_tool_success_does_not_reset_query_no_progress(
    tmp_path, monkeypatch,
):
    """think/todo 成功不得清零 ontology_query 的无进展轮次（8 轮停止）。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "f2-unrelated")
    calls: list[tuple[str, dict]] = []

    def executor(name, arguments, _context):
        calls.append((name, dict(arguments or {})))
        if name == "ontology_query":
            return _query_error("invalid_parameter")
        if name == "think":
            return "已记录：无关思考成功"
        if name == "todo_write":
            return "1. step"
        return json.dumps({"ok": True}, ensure_ascii=False)

    monkeypatch.setattr(runtime, "_execute_read_only_tool", executor)
    responses = iter([
        *[_multi_tool_round(index) for index in range(10)],
        _text("模型最终答复：统计 0 条。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids), agent_mode=True))

    query_calls = [item for item in calls if item[0] == "ontology_query"]
    assert len(query_calls) == 8
    assert len([item for item in calls if item[0] == "think"]) == 8
    assert len([item for item in calls if item[0] == "todo_write"]) == 8
    ending = _message_end(events)
    assert "未取得有效成功" in ending["content"]
    assert "统计 0 条" not in ending["content"]
    with TestingSession() as db:
        runs = {run.tool_name: run for run in _runs(db, ids)}
        assert runs["think"].status == "success"
        assert runs["todo_write"].status == "success"


@pytest.mark.parametrize("success_kind", ["zero", "cursor_page"])
def test_f2_valid_query_success_resets_no_progress(
    tmp_path, monkeypatch, success_kind,
):
    """正确零值与合法 cursor 续页都算有效 ontology_query 进展并清零。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, f"f2-{success_kind}")
    calls: list[tuple[str, dict]] = []
    success_payload = (
        _query_success(totalRowCount=0, distinctEntityCounts={"o1": 0})
        if success_kind == "zero" else
        _query_success(hasNext=False, totalRowCount=2, pageSize=2)
    )

    def executor(name, arguments, _context):
        calls.append((name, dict(arguments or {})))
        if name != "ontology_query":
            return json.dumps({"ok": True}, ensure_ascii=False)
        if arguments.get("phase") == "success":
            return success_payload
        return _query_error("invalid_parameter")

    monkeypatch.setattr(runtime, "_execute_read_only_tool", executor)
    responses = iter([
        *[_multi_tool_round(index, todo=False, think=False)
          for index in range(7)],
        {"content": None, "usage": {}, "tool_calls": [{
            "id": "q-success", "name": "ontology_query",
            "arguments": ({"sources": [{"alias": "a"}], "phase": "success"}
                          if success_kind == "zero" else
                          {"cursor": "cursor-page-0123456789",
                           "phase": "success"}),
        }]},
        *[_multi_tool_round(index + 100, todo=False, think=False)
          for index in range(7)],
        _text("后续查询已完成。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    ending = _message_end(events)
    assert ending["content"] == "后续查询已完成。"
    assert "未取得有效成功" not in ending["content"]
    with TestingSession() as db:
        runs = _runs(db, ids)
        assert len(runs) == 15
        assert any(run.status == "success" for run in runs)


def _two_query_round(round_index: int) -> dict:
    return {
        "content": None,
        "usage": {},
        "tool_calls": [
            {"id": f"q{round_index}a", "name": "ontology_query",
             "arguments": {"sources": [{"alias": "a"}],
                           "slot": f"{round_index}a"}},
            {"id": f"q{round_index}b", "name": "ontology_query",
             "arguments": {"sources": [{"alias": "b"}],
                           "slot": f"{round_index}b"}},
        ],
    }


def test_f3_release_after_earlier_failure_clears_block(tmp_path, monkeypatch):
    """真实因果 (a)：较早执行失败 → 较新 released=true 成功解除阻塞。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "f3-failure-release")
    calls: list[tuple[str, dict]] = []
    outputs = [
        _query_error("memory_quota_exceeded", quota="max_handles_per_user"),
        _query_success(pageSize=1),          # cursor 续页终页，真实释放
        _query_success(totalRowCount=0),
    ]
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor(outputs, calls),
    )
    responses = iter([
        _tool_round("c1", n=1),
        _tool_round("c2", cursor="cursor-old-0123456789"),
        _tool_round("c3", n=3),
        _text("统计完成。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert len(calls) == 3
    with TestingSession() as db:
        runs = _runs(db, ids)
        assert runs[0].status == "error"
        assert runs[1].status == "success"
        assert runs[2].status == "success"
    assert _message_end(events)["content"] == "统计完成。"


def test_f3_release_then_genuinely_new_saturation_failure_keeps_block(
    tmp_path, monkeypatch,
):
    """真实因果 (b)：先真实释放，之后新发生的配额失败仍建立阻塞。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "f3-release-failure")
    calls: list[tuple[str, dict]] = []
    outputs = [
        _query_success(pageSize=1),          # cursor 续页终页，真实释放
        _query_error("memory_quota_exceeded", quota="max_handles_per_user"),
    ]
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor(outputs, calls),
    )
    responses = iter([
        _tool_round("c1", cursor="cursor-old-0123456789"),
        _tool_round("c2", n=2),
        _tool_round("c3", n=3),  # 释放之后的新饱和失败 → 被阻塞，不执行
        _text("已按受控错误停止新查询。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))
    assert [arguments for _name, arguments in calls] == [
        {"cursor": "cursor-old-0123456789"},
        {"n": 2},
    ]
    with TestingSession() as db:
        runs = _runs(db, ids)
        assert runs[0].status == "success"
        assert runs[1].status == "error"
        assert json.loads(runs[2].result)["code"] == "query_resource_blocked"
        assert runs[2].status == "error"


def test_f3_result_registration_is_inside_owner_critical_section(
    tmp_path, monkeypatch,
):
    """执行与登记同一临界区：第二次查询必须等第一次登记完成才执行。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "f3-register-lock")
    recovery = _module("query_recovery")
    events: list[dict] = []
    original = recovery.QueryRecoveryState.record_failure

    def slow_record(self, payload, arguments):
        result = original(self, payload, arguments)
        events.append({"kind": "registered", "at": time.monotonic()})
        time.sleep(0.25)  # 若登记在锁外，第二次执行会在此期间开始
        return result

    monkeypatch.setattr(
        recovery.QueryRecoveryState, "record_failure", slow_record)

    def fake_builtin(db, *, name, arguments, **_context):
        events.append({"kind": "executed", "at": time.monotonic()})
        if name != "ontology_query":
            return json.dumps({"ok": True}, ensure_ascii=False)
        if len([e for e in events if e["kind"] == "executed"]) == 1:
            return _query_error("invalid_parameter")  # 非资源类：不触发预检阻塞
        return _query_success()

    monkeypatch.setattr(runtime, "_execute_builtin_tool", fake_builtin)
    responses = iter([_two_query_round(1), _text("done")])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))

    executed = [event for event in events if event["kind"] == "executed"]
    registered = [event for event in events if event["kind"] == "registered"]
    assert len(executed) == 2 and registered
    # 第二次执行必须在第一次失败登记（含人为延迟）完成之后才开始。
    assert executed[1]["at"] >= registered[0]["at"] + 0.2


# --------------------------------- T6-F1/F2/F3 repair（t19，review=t6）


def _oversized_error_envelope(size: int = 30500,
                              code: str = "invalid_parameter") -> str:
    """无记录数组的超长错误信封：legacy 字符截断会破坏 JSON。"""
    return json.dumps({"error": "E" * size, "code": code},
                      ensure_ascii=False)


def test_t19_oversized_error_envelope_keeps_error_status_and_persisted_code(
    tmp_path, monkeypatch,
):
    """T6-F1：错误信封 > cap 不得因截断被误判 success。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "t19-longerr")
    # 超长错误会触发上下文压缩；压缩调用的是外部 provider，测试只 stub 它。
    monkeypatch.setattr(
        runtime, "maybe_compact",
        lambda db, conversation, call_kwargs, messages, **kwargs: messages)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([_oversized_error_envelope()], calls),
    )
    responses = iter([_tool_round("c1", n=1), _text("查询失败，稍后重试。")])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))

    results = _events_named(events, "tool_result")
    assert results and results[0]["status"] == "error"
    ending = _message_end(events)
    assert ending["steps"][0]["status"] == "error"
    assert ending["content"] == "查询失败，稍后重试。"
    with TestingSession() as db:
        run = _runs(db, ids)[0]
        assert run.status == "error"
        assert run.error == "invalid_parameter"
        shell = json.loads(run.result)
        assert shell["code"] == "invalid_parameter"
        assert "E" * 200 not in run.result  # 不落超长异常原文


def test_t19_oversized_error_envelope_counts_as_no_progress(
    tmp_path, monkeypatch,
):
    """T6-F1：超长错误信封仍算查询失败轮次（8 轮停止，不被误清零）。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "t19-longerr-progress")
    # 8 轮超长错误会触发上下文压缩；压缩调用外部 provider，只 stub 外部调用。
    monkeypatch.setattr(
        runtime, "maybe_compact",
        lambda db, conversation, call_kwargs, messages, **kwargs: messages)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([_oversized_error_envelope()], calls),
    )
    responses = iter([
        *[_tool_round(f"c{i}", n=i + 1) for i in range(8)],
        _text("模型最终答复：统计 0 条。"),  # 不应发布
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))

    assert len(calls) == 8
    ending = _message_end(events)
    assert "未取得有效成功" in ending["content"]
    assert "invalid_parameter" in ending["content"]
    assert "统计 0 条" not in ending["content"]
    with TestingSession() as db:
        runs = _runs(db, ids)
        assert len(runs) == 8
        assert {run.status for run in runs} == {"error"}
        assert {json.loads(run.result)["code"] for run in runs} == {
            "invalid_parameter"}


def test_t19_authorize_refs_filters_revoked_object_type(tmp_path, monkeypatch):
    """T6-F2：同 release 下按当前允许对象类型过滤历史 ref，并支持翻页。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "t19-ref-types")
    pages = {
        None: {"ontologyId": "o1", "releaseId": "r1",
               "items": [{"id": "allowed-a"}],
               "hasNext": True, "cursor": "page-2"},
        "page-2": {"ontologyId": "o1", "releaseId": "r1",
                   "items": [{"id": "allowed-b"}],
                   "hasNext": False, "cursor": None},
    }
    seen_cursors: list[str | None] = []

    def fake_object_types(db, user, *, ontology_id, release_id=None, cursor=None):
        seen_cursors.append(cursor)
        return pages[cursor]

    monkeypatch.setattr(ontology_tools, "catalog_object_types", fake_object_types)
    monkeypatch.setattr(ontology_tools, "current_release_map",
                        lambda db, ontology_ids: {oid: "r1" for oid in ontology_ids})
    refs = [
        {"ontologyId": "o1", "releaseId": "r1", "objectTypeId": "allowed-a"},
        {"ontologyId": "o1", "releaseId": "r1", "objectTypeId": "allowed-b"},
        {"ontologyId": "o1", "releaseId": "r1", "objectTypeId": "revoked-c"},
        {"ontologyId": "o1", "releaseId": "r1"},
    ]
    with TestingSession() as db:
        authorized = ontology_tools.authorize_structure_refs(
            db, owner_id=ids["owner_id"],
            conversation_id=ids["conversation_id"], refs=refs)
    types = [ref.get("objectTypeId") for ref in authorized]
    assert types == ["allowed-a", "allowed-b", None]
    assert all(ref["stale"] is False for ref in authorized)
    # 目标类型在第二页：必须沿 hasNext/cursor 翻页才能证明授权
    assert seen_cursors == [None, "page-2"]


def test_t19_authorize_refs_missing_type_scope_fails_closed(
    tmp_path, monkeypatch,
):
    """无法证明对象类型范围（分页异常）时，带类型引用不注入。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "t19-ref-scope-fail")

    def fake_object_types(db, user, *, ontology_id, release_id=None, cursor=None):
        if cursor is None:
            return {"ontologyId": "o1", "releaseId": "r1",
                    "items": [{"id": "allowed-a"}],
                    "hasNext": True, "cursor": "page-2"}
        raise ontology_tools.FacadeError(400, "invalid_catalog_cursor", "bad")

    monkeypatch.setattr(ontology_tools, "catalog_object_types", fake_object_types)
    monkeypatch.setattr(ontology_tools, "current_release_map",
                        lambda db, ontology_ids: {oid: "r1" for oid in ontology_ids})
    with TestingSession() as db:
        authorized = ontology_tools.authorize_structure_refs(
            db, owner_id=ids["owner_id"],
            conversation_id=ids["conversation_id"],
            refs=[{"ontologyId": "o1", "releaseId": "r1",
                   "objectTypeId": "needed-but-unprovable"}])
    assert authorized == []


def test_t19_has_cursor_presence_semantics():
    """T6-F3：cursor 键存在且非 None 即续查（""/0/False 交由工具判 invalid_cursor）。"""
    recovery = _module("query_recovery")
    assert recovery._has_cursor({}) is False
    assert recovery._has_cursor({"cursor": None}) is False
    assert recovery._has_cursor({"cursor": ""}) is True
    assert recovery._has_cursor({"cursor": 0}) is True
    assert recovery._has_cursor({"cursor": False}) is True
    assert recovery._has_cursor({"cursor": "mem.cGFnZQ"}) is True

    state = recovery.QueryRecoveryState()
    state.record_failure(
        {"error": "x", "code": "snapshot_quota"}, {"sources": []})
    assert state.block_reason({"sources": []}) is not None
    assert state.block_reason({"cursor": None}) is not None
    assert state.block_reason({"cursor": ""}) is None
    assert state.block_reason({"cursor": 0}) is None
    assert state.block_reason({"cursor": False}) is None


@pytest.mark.parametrize("cursor_value", ["", 0, False])
def test_t19_invalid_cursor_not_masked_by_release_block(
    tmp_path, monkeypatch, cursor_value,
):
    """T6-F3 runtime：release_block 下空/零 cursor 仍执行工具返回 invalid_cursor。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, f"t19-cursor-{cursor_value!r}")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([
            _query_error("snapshot_quota"),
            _query_error("invalid_cursor"),
        ], calls),
    )
    responses = iter([
        _tool_round("c1", n=1),
        _tool_round("c2", cursor=cursor_value),
        _text("cursor 无效。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))

    assert [arguments for _name, arguments in calls] == [
        {"n": 1}, {"cursor": cursor_value}]
    with TestingSession() as db:
        runs = _runs(db, ids)
        assert runs[0].status == "error"
        assert json.loads(runs[0].result)["code"] == "snapshot_quota"
        assert runs[1].status == "error"
        assert runs[1].error == "invalid_cursor"


def test_t19_valid_cursor_still_allowed_under_release_block(
    tmp_path, monkeypatch,
):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "t19-cursor-valid")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        _scripted_executor([
            _query_error("snapshot_quota"),
            _query_success(pageSize=1),
        ], calls),
    )
    responses = iter([
        _tool_round("c1", n=1),
        _tool_round("c2", cursor="cursor-old-0123456789"),
        _text("续读完成。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))
    assert [arguments for _name, arguments in calls] == [
        {"n": 1}, {"cursor": "cursor-old-0123456789"}]
    with TestingSession() as db:
        assert _runs(db, ids)[1].status == "success"
