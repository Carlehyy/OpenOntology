"""可信联邦 persistence 标记的 runtime 集成测试。

沿用 test_runtime_integration 的隔离 sqlite 手法：provider.chat_stream 伪造、
只读工具执行器按用例替换，不触网。断言落库面（ToolRun.result / steps
preview / assistant content）与 SSE 临时面的分流。
"""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import RoleMenuPermission, User
from app.model_configs.models import ModelConfig
from app.shared.database import Base
from app.super_assistant import runtime
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMcpServer,
    SuperAssistantMemory,
    SuperAssistantMemoryProfile,
    SuperAssistantMulticaConfig,
    SuperAssistantMessage,
    SuperAssistantReflectionCandidate,
    SuperAssistantReflectionRun,
    SuperAssistantSkill,
    SuperAssistantToolRun,
    SuperAssistantToolSetting,
)

_TABLES = [
    User.__table__, ModelConfig.__table__,
    RoleMenuPermission.__table__,
    SuperAssistantConversation.__table__, SuperAssistantSkill.__table__,
    SuperAssistantMcpServer.__table__, SuperAssistantMessage.__table__,
    SuperAssistantToolRun.__table__, SuperAssistantToolSetting.__table__,
    SuperAssistantMemory.__table__,
    SuperAssistantMemoryProfile.__table__, SuperAssistantMulticaConfig.__table__,
    SuperAssistantReflectionRun.__table__,
    SuperAssistantReflectionCandidate.__table__,
]

_SECRET = "SO-1001"


def _seed(tmp_path, monkeypatch, name):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}.db", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_TABLES)
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
            role="user", content="查订单", status="complete",
        ))
        db.add(SuperAssistantMessage(
            id=f"assistant-msg-{name}", conversation_id=f"conv-{name}",
            role="assistant", content="", status="streaming",
        ))
        db.commit()
    return TestingSession, {
        "conversation_id": f"conv-{name}",
        "owner_id": f"user-{name}",
        "assistant_message_id": f"assistant-msg-{name}",
        "requested_model_id": f"model-{name}",
    }


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


def _query_payload(classification=None):
    payload = {
        "queryId": "qs-1",
        "columns": ["orderId", "amount"],
        "rows": [{"orderId": _SECRET, "amount": 120.5}],
        "pageRowCount": 1,
        "totalRowCount": 1,
        "hasNext": False,
        "cursor": None,
        "completeness": "complete",
        "truncated": False,
    }
    if classification is not None:
        payload["persistence"] = {
            "mode": "ephemeral", "classification": classification,
        }
    return json.dumps(payload, ensure_ascii=False)


def _run_and_load(tmp_path, monkeypatch, name, responses, read_only_result):
    _, ids = _seed(tmp_path, monkeypatch, name)
    monkeypatch.setattr(runtime, "_execute_read_only_tool",
                        lambda name_, arguments, context: read_only_result)
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    engine = create_engine(
        f"sqlite:///{tmp_path / name}.db", connect_args={"check_same_thread": False},
    )
    with sessionmaker(bind=engine)() as db:
        message = db.get(SuperAssistantMessage, ids["assistant_message_id"])
        run = db.query(SuperAssistantToolRun).one()
        steps = message.steps or []
    return events, message, run, steps


def test_detail_marker_shells_persisted_surfaces_but_streams_full_answer(
    tmp_path, monkeypatch,
):
    """detail：ToolRun/steps/落库答复只存元信息壳；SSE 临时面保留全文。"""
    final_text = f"订单 {_SECRET} 金额 120.5。"
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "c1", "name": "ontology_query",
                "arguments": {"sources": [{"alias": "s"}]},
            }],
            "usage": {},
        },
        _text(final_text, inputTokens=9, outputTokens=3),
    ])
    events, message, run, steps = _run_and_load(
        tmp_path, monkeypatch, "detail", responses, _query_payload("detail"))

    # 落库面：ToolRun.result / steps.preview / assistant content 均为壳
    assert _SECRET not in (run.result or "")
    assert json.loads(run.result)["ephemeralDetail"] is True
    assert json.loads(run.result)["totalRowCount"] == 1
    assert steps and _SECRET not in steps[0]["preview"]
    assert _SECRET not in (message.content or "")
    assert "远程联邦明细" in message.content
    assert "queryId" in message.content

    # SSE 临时面：实时文本与 tool_result 预览保留明细
    assert _SECRET in events
    assert "ephemeralDetail" in events


def test_aggregate_marker_keeps_old_persistence_behavior(tmp_path, monkeypatch):
    """aggregate：服务端已分类，工具结果与答复按旧行为落库。"""
    final_text = "聚合结论：共 1 单。"
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "c1", "name": "ontology_query",
                "arguments": {"sources": [{"alias": "s"}]},
            }],
            "usage": {},
        },
        _text(final_text),
    ])
    events, message, run, steps = _run_and_load(
        tmp_path, monkeypatch, "aggregate", responses,
        _query_payload("aggregate"))
    assert _SECRET in run.result
    assert _SECRET in steps[0]["preview"]
    assert message.content == final_text


def test_no_marker_keeps_local_old_behavior(tmp_path, monkeypatch):
    """无标记的本地查询响应完全保持旧行为。"""
    final_text = "本地结果答复。"
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "c1", "name": "ontology_query",
                "arguments": {"sources": [{"alias": "s"}]},
            }],
            "usage": {},
        },
        _text(final_text),
    ])
    _, message, run, _steps = _run_and_load(
        tmp_path, monkeypatch, "local", responses, _query_payload(None))
    assert _SECRET in run.result
    assert message.content == final_text


def test_memory_save_blocked_in_detail_turn(tmp_path, monkeypatch):
    """detail 轮 memory_save fail-closed：不产生记忆行/候选。"""
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "c1", "name": "ontology_query",
                "arguments": {"sources": [{"alias": "s"}]},
            }],
            "usage": {},
        },
        {
            "content": None,
            "tool_calls": [{
                "id": "c2", "name": "memory_save",
                "arguments": {"content": f"订单 {_SECRET} 金额机密"},
            }],
            "usage": {},
        },
        _text("已尝试保存。"),
    ])
    _, ids = _seed(tmp_path, monkeypatch, "memsave")
    monkeypatch.setattr(runtime, "_execute_read_only_tool",
                        lambda name_, arguments, context: _query_payload("detail"))
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert "ephemeral_detail_active" in events

    engine = create_engine(
        f"sqlite:///{tmp_path / 'memsave'}.db", connect_args={"check_same_thread": False},
    )
    with sessionmaker(bind=engine)() as db:
        assert db.query(SuperAssistantMemory).count() == 0
        assert db.query(SuperAssistantReflectionCandidate).count() == 0
        runs = db.query(SuperAssistantToolRun).order_by(
            SuperAssistantToolRun.created_at.asc()).all()
        memory_run = next(r for r in runs if r.tool_name == "memory_save")
        assert "ephemeralDetail" in memory_run.result
        assert _SECRET not in json.dumps(memory_run.arguments)
        # 明细查询本体依旧壳化
        query_run = next(r for r in runs if r.tool_name == "ontology_query")
        assert _SECRET not in query_run.result


# ---------------------------------------------------------------------------
# 失败路径与泄漏回归：异常 / marker 丢失 / 截断 / 复述 / 跨轮 / 摘要 /
# 反思 / 导出（不能只测成功路径）
# ---------------------------------------------------------------------------

def _sse_payload(events: str, event: str):
    marker = f"event: {event}\ndata: "
    start = events.index(marker) + len(marker)
    return json.loads(events[start:events.index("\n\n", start)])


def test_message_end_streams_full_answer_while_db_keeps_shell(tmp_path, monkeypatch):
    """SSE message_end（临时面）给模型全文；落库内容为壳。"""
    final_text = f"订单 {_SECRET} 金额 120.5。"
    responses = iter([
        {"content": None, "tool_calls": [{
            "id": "c1", "name": "ontology_query",
            "arguments": {"sources": [{"alias": "s"}]}}],
            "usage": {}},
        _text(final_text),
    ])
    events, message, _run, _steps = _run_and_load(
        tmp_path, monkeypatch, "livefull", responses, _query_payload("detail"))
    payload = _sse_payload(events, "message_end")
    assert _SECRET in payload["message"]["content"]
    assert _SECRET not in (message.content or "")


def test_marker_lost_after_remote_context_shells_second_query(tmp_path, monkeypatch):
    """远程上下文建立后，同轮 marker 丢失的响应按 fail-closed 壳处理。"""
    calls: list[str] = []

    def fake_read_only(name_, arguments, context):
        calls.append(name_)
        if len(calls) == 1:
            return _query_payload("detail")
        # 第二次：本地形状响应（无 persistence 键）
        return json.dumps({
            "queryId": "qs-2", "rows": [{"orderId": "LOCAL-42"}],
            "totalRowCount": 1,
        }, ensure_ascii=False)

    responses = iter([
        {"content": None, "tool_calls": [{
            "id": "c1", "name": "ontology_query",
            "arguments": {"sources": [{"alias": "s"}]}}],
            "usage": {}},
        {"content": None, "tool_calls": [{
            "id": "c2", "name": "ontology_query",
            "arguments": {"cursor": "x" * 20}}],
            "usage": {}},
        _text("完成。"),
    ])
    _, ids = _seed(tmp_path, monkeypatch, "lost")
    monkeypatch.setattr(runtime, "_execute_read_only_tool", fake_read_only)
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))

    engine = create_engine(
        f"sqlite:///{tmp_path / 'lost'}.db", connect_args={"check_same_thread": False},
    )
    with sessionmaker(bind=engine)() as db:
        runs = db.query(SuperAssistantToolRun).order_by(
            SuperAssistantToolRun.created_at.asc()).all()
        first, second = runs
        assert json.loads(first.result)["ephemeralDetail"] is True
        second_shell = json.loads(second.result)
        assert second_shell["ephemeralDetail"] is True
        assert "LOCAL-42" not in second.result


def test_exception_in_remote_context_bounded_no_detail(tmp_path, monkeypatch):
    """远程上下文中的执行异常：result 为错误壳、error 列有界，不落明细。"""
    deep_secret = "P" * 200 + " SECRETXYZ"
    calls: list[str] = []

    def fake_read_only(name_, arguments, context):
        calls.append(name_)
        if len(calls) == 1:
            return _query_payload("detail")
        raise RuntimeError("boom " + deep_secret)

    responses = iter([
        {"content": None, "tool_calls": [{
            "id": "c1", "name": "ontology_query",
            "arguments": {"sources": [{"alias": "s"}]}}],
            "usage": {}},
        {"content": None, "tool_calls": [{
            "id": "c2", "name": "ontology_query",
            "arguments": {"cursor": "x" * 20}}],
            "usage": {}},
        _text("第二次失败。"),
    ])
    _, ids = _seed(tmp_path, monkeypatch, "exc")
    monkeypatch.setattr(runtime, "_execute_read_only_tool", fake_read_only)
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))

    engine = create_engine(
        f"sqlite:///{tmp_path / 'exc'}.db", connect_args={"check_same_thread": False},
    )
    with sessionmaker(bind=engine)() as db:
        runs = db.query(SuperAssistantToolRun).order_by(
            SuperAssistantToolRun.created_at.asc()).all()
        error_run = runs[1]
        assert error_run.status == "error"
        shell = json.loads(error_run.result)
        assert shell["ephemeralDetail"] is True
        assert "SECRETXYZ" not in error_run.result
        assert "SECRETXYZ" not in (error_run.error or "")


def test_json_aware_truncation_keeps_cursor_in_shell(tmp_path, monkeypatch):
    """30k 上限的 JSON 感知截断：模型视图为合法 JSON 保 counts/cursor，
    落库壳同样保留 cursor/totalRowCount 且不含明细行。"""
    from app.shared.config import settings as app_settings

    rows = [{"orderId": f"SO-{i:05d}", "amount": float(i)} for i in range(300)]
    payload = {
        "queryId": "qs-t", "columns": ["orderId", "amount"], "rows": rows,
        "pageRowCount": 300, "totalRowCount": 300, "hasNext": True,
        "cursor": "cursor-" + "z" * 20, "completeness": "complete",
        "truncated": False,
        "persistence": {"mode": "ephemeral", "classification": "detail"},
    }
    responses = iter([
        {"content": None, "tool_calls": [{
            "id": "c1", "name": "ontology_query",
            "arguments": {"sources": [{"alias": "s"}]}}],
            "usage": {}},
        _text("已按分页返回。"),
    ])
    _, ids = _seed(tmp_path, monkeypatch, "truncate")
    monkeypatch.setattr(app_settings, "super_assistant_tool_result_chars", 900)
    monkeypatch.setattr(
        runtime, "_execute_read_only_tool",
        lambda name_, arguments, context: json.dumps(payload, ensure_ascii=False))
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))

    engine = create_engine(
        f"sqlite:///{tmp_path / 'truncate'}.db", connect_args={"check_same_thread": False},
    )
    with sessionmaker(bind=engine)() as db:
        run = db.query(SuperAssistantToolRun).one()
        shell = json.loads(run.result)
        assert shell["cursor"] == "cursor-" + "z" * 20
        assert shell["totalRowCount"] == 300
        assert "rows" not in shell
        assert "SO-00299" not in run.result
        message = db.get(SuperAssistantMessage, ids["assistant_message_id"])
        assert "SO-00299" not in (message.content or "")
    # SSE tool_result 预览（临时面）可见截断后的合法 JSON 且带 truncated 标记
    preview = _sse_payload(events, "tool_result")["preview"]
    assert "truncated" in preview


def test_summary_reflection_export_and_cross_turn_have_no_detail(
    tmp_path, monkeypatch,
):
    """泄漏面回归：压缩摘要 prompt、micro 反思 prompt、会话导出、跨轮
    history 重建都不包含源明细。"""
    from app.shared.config import settings as app_settings
    from app.super_assistant import compaction, conversation_service
    from app.super_assistant import reflection_service

    final_text = f"订单 {_SECRET} 金额 120.5，已答复。"
    compaction_prompts: list[str] = []
    responses = iter([
        {"content": None, "tool_calls": [{
            "id": "c1", "name": "ontology_query",
            "arguments": {"sources": [{"alias": "s"}]}}],
            "usage": {}},
        {"content": None, "tool_calls": [{
            "id": "c2", "name": "think", "arguments": {"thought": f"整理 {_SECRET}"}}],
            "usage": {}},
        {"content": None, "tool_calls": [{
            "id": "c3", "name": "think", "arguments": {"thought": "复核"}}],
            "usage": {}},
        _text(final_text),
    ])
    TestingSession, ids = _seed(tmp_path, monkeypatch, "leak")
    monkeypatch.setattr(app_settings, "super_assistant_context_headroom", 0.999)
    monkeypatch.setattr(app_settings, "super_assistant_compaction_keep_recent", 2)

    original_read_only = runtime._execute_read_only_tool

    def faithful_read_only(name_, arguments, context):
        # 忠实分派：ontology_query 返回 detail 载荷；其余走真实执行器
        if name_ == "ontology_query":
            return _query_payload("detail")
        return original_read_only(name_, arguments, context)

    monkeypatch.setattr(runtime, "_execute_read_only_tool", faithful_read_only)

    def fake_compact_chat(_kwargs, chat_messages, _tools):
        compaction_prompts.append(chat_messages[0]["content"])
        return {"content": "结构化摘要", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(compaction.provider, "chat", fake_compact_chat)
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))

    # 1) 压缩摘要 prompt 与落库 summary 都不含明细
    assert compaction_prompts, "压缩应被触发"
    for prompt in compaction_prompts:
        assert _SECRET not in prompt
    with TestingSession() as db:
        conversation = db.get(
            SuperAssistantConversation, ids["conversation_id"])
        assert _SECRET not in (conversation.summary or "")

        # 2) micro 反思 prompt 不含明细（反思只读落库消息=壳）
        reflect_prompts: list[str] = []

        def fake_reflect_chat(_kwargs, chat_messages, _tools):
            reflect_prompts.append(chat_messages[0]["content"])
            return {"content": '{"memory_candidates": [], "skill_candidates": []}',
                    "tool_calls": [], "usage": {}}

        monkeypatch.setattr(reflection_service.provider, "chat", fake_reflect_chat)
        reflection_service.run_micro_reflection(
            db, ids["owner_id"], ids["conversation_id"], ids["assistant_message_id"])
        assert reflect_prompts
        for prompt in reflect_prompts:
            assert _SECRET not in prompt

        # 3) 会话导出（messages/steps/summary 全量）不含明细
        user = db.get(User, ids["owner_id"])
        exported = conversation_service.export_conversation(
            ids["conversation_id"], db, user)
        assert _SECRET not in json.dumps(exported, ensure_ascii=False, default=str)

    # 4) 跨轮：新一条 assistant 消息重建 history，模型上下文只见壳
    with TestingSession() as db:
        db.add(SuperAssistantMessage(id="assistant-msg-leak-2",
                       conversation_id=ids["conversation_id"],
                       role="assistant", content="", status="streaming"))
        db.commit()
    captured_context: list[dict] = []

    def capture_stream(_kwargs, messages, _tools, on_delta=None):
        captured_context.append(messages)
        return {"content": "第二轮答复。", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(runtime.provider, "chat_stream", capture_stream)
    "".join(runtime.stream_chat(
        conversation_id=ids["conversation_id"],
        owner_id=ids["owner_id"],
        assistant_message_id="assistant-msg-leak-2",
        requested_model_id=ids["requested_model_id"],
    ))
    assert captured_context
    for turn_messages in captured_context:
        for message in turn_messages:
            assert _SECRET not in str(message.get("content") or "")


def test_execute_ontology_tool_dispatch_passes_marker_through(tmp_path, monkeypatch):
    """执行链路核正：execute_ontology_tool 原样透传门面 query 的 JSON
    （含 persistence 标记），runtime register 据此壳化——不造第二执行器。"""
    from app.super_assistant import ontology_tools

    payload = json.loads(_query_payload("detail"))
    monkeypatch.setattr(
        ontology_tools, "query",
        lambda db, user, request, **kwargs: payload)
    monkeypatch.setattr(
        ontology_tools, "build_query_request", lambda arguments: arguments)
    TestingSession, ids = _seed(tmp_path, monkeypatch, "dispatch")
    with TestingSession() as db:
        user = db.query(User).filter(User.id == ids["owner_id"]).first()
        output = ontology_tools.execute_ontology_tool(
            db, owner_id=ids["owner_id"],
            conversation_id=ids["conversation_id"],
            name="ontology_query",
            arguments={"sources": [{"alias": "s"}]})
    assert json.loads(output) == payload  # 标记经真实分派入口原样可达
    state = runtime.persistence.EphemeralTurnState()
    persisted = state.register("ontology_query", "c1", output)
    assert _SECRET not in persisted



def test_think_echo_suppressed_in_detail_turn(tmp_path, monkeypatch):
    """think 回显是持久面：detail 轮内不再回写模型复述内容（防经
    think 把明细改写落库）。"""
    secret_thought = f"客户订单 {_SECRET} 金额 120.5"

    _, ids = _seed(tmp_path, monkeypatch, "thinkgate")
    original_read_only = runtime._execute_read_only_tool

    def faithful_read_only(name_, arguments, context):
        if name_ == "ontology_query":
            return _query_payload("detail")
        return original_read_only(name_, arguments, context)

    responses = iter([
        {"content": None, "tool_calls": [{
            "id": "c1", "name": "ontology_query",
            "arguments": {"sources": [{"alias": "s"}]}}],
            "usage": {}},
        {"content": None, "tool_calls": [{
            "id": "c2", "name": "think", "arguments": {"thought": secret_thought}}],
            "usage": {}},
        _text("完成。"),
    ])
    monkeypatch.setattr(runtime, "_execute_read_only_tool", faithful_read_only)
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))

    engine = create_engine(
        f"sqlite:///{tmp_path / 'thinkgate'}.db", connect_args={"check_same_thread": False},
    )
    with sessionmaker(bind=engine)() as db:
        runs = db.query(SuperAssistantToolRun).order_by(
            SuperAssistantToolRun.created_at.asc()).all()
        think_run = next(r for r in runs if r.tool_name == "think")
        assert _SECRET not in think_run.result
        assert "ephemeralDetail" in think_run.result
        assert _SECRET not in json.dumps(think_run.arguments)


def test_catalog_tool_operation_dispatch_wiring(tmp_path, monkeypatch):
    """ontology_catalog 三模式接线：object_types / get_document / search
    经门面关键字参数正确到达（门面行为由 ontologies 域测试覆盖）。"""
    from app.super_assistant import ontology_tools

    calls: dict[str, tuple] = {}

    def fake_object_types(db, user, *, ontology_id, release_id=None, cursor=None):
        calls["object_types"] = (ontology_id, release_id, cursor)
        return {"items": [], "hasNext": False, "cursor": None}

    def fake_get_document(db, user, *, ontology_id, release_id=None, cursor=None):
        calls["get_document"] = (ontology_id, release_id, cursor)
        return {"documentMd": "x"}

    def fake_search(db, user, *, query=None, cursor=None):
        calls["search"] = (query, cursor)
        return {"items": [], "hasNext": False, "cursor": None}

    facade = "app.ontologies.agent_runtime.assistant_facade"
    monkeypatch.setattr(facade + ".catalog_object_types", fake_object_types)
    monkeypatch.setattr(facade + ".catalog_get_document", fake_get_document)
    monkeypatch.setattr(facade + ".catalog_search", fake_search)

    TestingSession, ids = _seed(tmp_path, monkeypatch, "catalogops")
    with TestingSession() as db:
        user = db.query(User).filter(User.id == ids["owner_id"]).first()

        def run(arguments):
            return json.loads(ontology_tools.execute_ontology_tool(
                db, owner_id=ids["owner_id"],
                conversation_id=ids["conversation_id"],
                name="ontology_catalog", arguments=arguments))

        run({"operation": "object_types", "ontologyId": "ont-1",
             "releaseId": "rel-9", "cursor": "c" * 20})
        assert calls["object_types"] == ("ont-1", "rel-9", "c" * 20)
        run({"ontologyId": "ont-1", "cursor": "d" * 20})
        assert calls["get_document"] == ("ont-1", None, "d" * 20)
        run({"query": "订单"})
        assert calls["search"] == ("订单", None)
