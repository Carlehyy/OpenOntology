"""本体动作结果权威守卫：无 execute 的成功陈述不得成为成功。

Finding: `.artifacts/ontology-network-goal/final-canary-three-chain-20261102/
evidence/FINDING-model-false-success-restore-turn1.json`。恢复确认仍为
prepared、当轮没有 ontology_execute_action，模型却输出 succeeded/applied/
affectedRows/receiptId 并在页面显示为成功。

这些测试只构造平台记录（确认 + ToolRun + 消息），不连接外部源、不执行
动作；权威来自持久 ActionConfirmation 与真实 ToolRun，不来自模型话术。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import RoleMenuPermission, User
from app.model_configs.models import ModelConfig
from app.ontologies.agent_confirmation import models as CM
from app.shared.database import Base
from app.super_assistant import result_guard, runtime
from app.super_assistant.kernel.models import CapabilityRevision
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

_TABLES = [
    User.__table__, ModelConfig.__table__,
    RoleMenuPermission.__table__,
    SuperAssistantConversation.__table__, SuperAssistantSkill.__table__,
    SuperAssistantMcpServer.__table__, SuperAssistantMessage.__table__,
    SuperAssistantToolRun.__table__, SuperAssistantToolSetting.__table__,
    SuperAssistantMemory.__table__,
    SuperAssistantMemoryProfile.__table__,
    SuperAssistantMemorySetting.__table__, SuperAssistantMemorySource.__table__, SuperAssistantMulticaConfig.__table__,
    SuperAssistantReflectionRun.__table__,
    SuperAssistantReflectionCandidate.__table__,
    CapabilityRevision.__table__,
    CM.ActionConfirmation.__table__,
]

_FALSE_SUCCESS = (
    "恢复执行成功：状态 succeeded，outcome applied，affectedRows: 1。"
    "receiptId: 4b0a53db-1111-4b0a-8f7e-111111111111，"
    "operationId: 9d4298ee-2222-4c1a-9e4b-222222222222；"
    "s003 已由 6 恢复为 5。"
)
_CONFIRMATION_ID = "97365f42-6da2-41b6-a65b-df89d04f6238"


def _confirmation(
    *, state="prepared", operation_id=None, failure_code=None,
    parameters=None,
):
    return CM.ActionConfirmation(
        id=_CONFIRMATION_ID,
        actor_user_id="user-guard",
        session_kind="super_assistant",
        session_id="conv-guard",
        root_intent_id="user-msg-old",
        action_request_slot="slot-restore",
        prepare_request_id="prep-restore",
        origin="super_assistant",
        source_call_id="call-prepare",
        ontology_id="ont-s003",
        release_id="rel-1",
        action_id="act-restore",
        target_instance_id="s003",
        final_parameters=parameters or {"value": "5"},
        parameter_sources={"value": {"kind": "request"}},
        requires_approval=True,
        payload_hash="hash-restore",
        display_payload={},
        state=state,
        operation_id=operation_id,
        failure_code=failure_code,
        expires_at=(
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(hours=1)
        ),
    )


def _prepare_run(*, result=None):
    return SuperAssistantToolRun(
        id="run-prepare",
        conversation_id="conv-guard",
        assistant_message_id="assistant-msg-old",
        call_id="call-prepare",
        tool_name="ontology_prepare_action",
        arguments={"actionId": "act-restore"},
        result=json.dumps(result or {
            "confirmationId": _CONFIRMATION_ID,
            "state": "prepared",
        }),
        status="success",
        requires_confirmation=False,
    )


def _seed(tmp_path, monkeypatch, name, *, confirmation_state="prepared",
          execute_status=None, user_text="确认执行恢复", action_context=True):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_TABLES)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", Session)
    with Session() as db:
        db.add(User(
            id="user-guard", username="guard", email="guard@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(ModelConfig(
            id="model-guard", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by="user-guard",
        ))
        db.add(SuperAssistantConversation(
            id="conv-guard", owner_id="user-guard", title="guard",
            model_config_id="model-guard",
        ))
        db.add_all([
            SuperAssistantMessage(
                id="user-msg-old", conversation_id="conv-guard", role="user",
                content="把 s003 恢复为 5", status="complete",
            ),
            SuperAssistantMessage(
                id="assistant-msg-old", conversation_id="conv-guard",
                role="assistant", content="已准备恢复动作，等待确认。",
                status="complete",
            ),
            SuperAssistantMessage(
                id="user-msg-current", conversation_id="conv-guard", role="user",
                content=user_text, status="complete",
            ),
            SuperAssistantMessage(
                id="assistant-msg-current", conversation_id="conv-guard",
                role="assistant", content="", status="streaming",
            ),
        ])
        if action_context:
            db.add(_prepare_run())
            db.add(_confirmation(state=confirmation_state))
        if execute_status is not None:
            db.add(SuperAssistantToolRun(
                id="run-execute",
                conversation_id="conv-guard",
                assistant_message_id="assistant-msg-current",
                call_id="call-execute",
                tool_name="ontology_execute_action",
                arguments={"confirmationId": _CONFIRMATION_ID},
                result=json.dumps({"confirmationId": _CONFIRMATION_ID,
                                   "state": confirmation_state}),
                status=execute_status,
                requires_confirmation=True,
            ))
        db.commit()
    return Session


def _stream(Session, monkeypatch, content):
    seen: list[str] = []

    def fake_chat_stream(_call_kwargs, _messages, _tools, on_delta=None):
        if on_delta:
            on_delta(content)
            seen.append(content)
        return {"content": content, "tool_calls": [], "usage": {
            "inputTokens": 10, "outputTokens": 5}}

    monkeypatch.setattr(runtime.provider, "chat_stream", fake_chat_stream)
    events = "".join(runtime.stream_chat(
        conversation_id="conv-guard",
        owner_id="user-guard",
        assistant_message_id="assistant-msg-current",
        requested_model_id="model-guard",
    ))
    with Session() as db:
        message = db.get(SuperAssistantMessage, "assistant-msg-current")
        runs = db.query(SuperAssistantToolRun).order_by(
            SuperAssistantToolRun.created_at.asc()).all()
        confirmation = db.get(CM.ActionConfirmation, _CONFIRMATION_ID)
        return events, message, runs, confirmation, seen


def _sse_payload(events, event):
    marker = f"event: {event}\ndata: "
    start = events.index(marker) + len(marker)
    return json.loads(events[start:events.index("\n\n", start)])


def _guard(db, content, message_id="assistant-msg-current", *, action_turn=True):
    return result_guard.guard_ontology_action_result(
        db,
        owner_id="user-guard",
        conversation_id="conv-guard",
        assistant_message_id=message_id,
        content=content,
        action_turn=action_turn,
    )


def test_prepared_approval_without_execute_never_streams_false_success(
    tmp_path, monkeypatch,
):
    """真实缺陷路径：已批准/prepared + 无 execute + 模型完整成功报告。"""
    Session = _seed(tmp_path, monkeypatch, "no-execute")
    events, message, runs, confirmation, provider_deltas = _stream(
        Session, monkeypatch, _FALSE_SUCCESS)

    # provider 确实产生了增量，但动作对话的最终文本被缓冲到守卫之后。
    assert provider_deltas == [_FALSE_SUCCESS]
    assert "event: text_delta" not in events
    payload = _sse_payload(events, "message_end")
    # 伪造的成功报告（含回执/行数）没有出现在任何 SSE 面。
    assert "恢复执行成功" not in events
    assert "receiptId" not in events
    assert "operationId" not in events
    assert "已由 6 恢复为 5" not in events
    # SSE 最终内容与持久化同源，且如实说明未执行。
    assert payload["message"]["content"] == message.content
    assert "prepared" in message.content
    assert "未执行" in message.content
    assert "业务未生效" not in message.content
    assert "未经平台核验" in message.content
    # 当轮没有 execute ToolRun，确认仍是 prepared（伪造回执未落任何记录）。
    assert [run.tool_name for run in runs] == ["ontology_prepare_action"]
    assert confirmation.state == "prepared"
    assert confirmation.operation_id is None


def test_guarded_action_overrides_ephemeral_shell_with_correction(
    tmp_path, monkeypatch,
):
    """守卫拦截优先于 ephemeral 壳：落库与最终 SSE 都是 corrected。"""
    Session = _seed(tmp_path, monkeypatch, "ephemeral")
    detail = json.dumps({
        "queryId": "qs-1", "columns": ["orderId", "amount"],
        "rows": [{"orderId": "SO-1", "amount": 120.5}],
        "pageRowCount": 1, "totalRowCount": 1, "hasNext": False,
        "cursor": None, "completeness": "complete", "truncated": False,
        "persistence": {"mode": "ephemeral", "classification": "detail"},
    }, ensure_ascii=False)
    responses = iter([
        {"content": None, "tool_calls": [{
            "id": "call-query", "name": "ontology_query",
            "arguments": {"sources": [{"alias": "s"}]}}],
         "usage": {"inputTokens": 10, "outputTokens": 2}},
        {"content": _FALSE_SUCCESS, "tool_calls": [],
         "usage": {"inputTokens": 20, "outputTokens": 8}},
    ])

    def fake_chat_stream(_call_kwargs, _messages, _tools, on_delta=None):
        result = next(responses)
        if result.get("content") and on_delta:
            on_delta(result["content"])
        return result

    monkeypatch.setattr(runtime, "_execute_read_only_tool",
                        lambda name_, arguments, context: detail)
    monkeypatch.setattr(runtime.provider, "chat_stream", fake_chat_stream)
    events = "".join(runtime.stream_chat(
        conversation_id="conv-guard",
        owner_id="user-guard",
        assistant_message_id="assistant-msg-current",
        requested_model_id="model-guard",
    ))
    with Session() as db:
        message = db.get(SuperAssistantMessage, "assistant-msg-current")
        execute_runs = db.query(SuperAssistantToolRun).filter(
            SuperAssistantToolRun.tool_name == "ontology_execute_action",
        ).count()
    payload = _sse_payload(events, "message_end")
    assert payload["message"]["content"] == message.content
    assert "prepared" in message.content
    assert "【本轮答复包含远程联邦明细" not in message.content
    assert "恢复执行成功" not in events
    assert "receiptId" not in events
    assert execute_runs == 0


def test_real_success_without_receipt_is_preserved(tmp_path, monkeypatch):
    """本地动作真实成功可无 receiptId：不因缺回执误杀真实成功。"""
    Session = _seed(
        tmp_path, monkeypatch, "no-receipt",
        confirmation_state="succeeded", execute_status="success",
    )
    content = "动作执行成功，确认状态为 succeeded，业务已生效。"
    events, message, runs, confirmation, _deltas = _stream(
        Session, monkeypatch, content)
    payload = _sse_payload(events, "message_end")
    assert payload["message"]["content"] == content
    assert message.content == content
    assert confirmation.state == "succeeded"
    assert {run.tool_name for run in runs} == {
        "ontology_prepare_action", "ontology_execute_action"}


def test_pending_review_is_not_applied(tmp_path, monkeypatch):
    """succeeded + outcome pending_review 不得展示为 applied。"""
    Session = _seed(tmp_path, monkeypatch, "pending-review")
    authority = {
        "confirmationId": _CONFIRMATION_ID,
        "state": "succeeded",
        "operationId": None,
        "failureCode": None,
        "outcome": {"status": "pending_review", "affectedRows": 0},
    }
    monkeypatch.setattr(
        result_guard, "_load_authority",
        lambda db, user, conversation_id, cid: dict(authority))
    content = (
        f"执行完成，待审核（pending_review），confirmationId: {_CONFIRMATION_ID}，"
        "affectedRows: 1，s003 已由 6 恢复为 5。"
    )
    with Session() as db:
        result = _guard(db, content)
    assert result.blocked is True
    assert result.reason == "pending_review"
    assert "pending_review" in result.content
    assert "尚不能视为已应用" in result.content
    assert "affectedRows" not in result.content


def test_unknown_is_never_success_and_not_replayed(tmp_path, monkeypatch):
    """unknown 只暴露未知状态，不自动重放、不呈现成功。"""
    Session = _seed(tmp_path, monkeypatch, "unknown")
    authority = {
        "confirmationId": _CONFIRMATION_ID,
        "state": "unknown",
        "operationId": "op-unknown",
        "failureCode": None,
        "outcome": {"status": "unknown", "affectedRows": 0},
    }
    monkeypatch.setattr(
        result_guard, "_load_authority",
        lambda db, user, conversation_id, cid: dict(authority))
    with Session() as db:
        result = _guard(
            db,
            "动作执行成功，已写回源库；"
            f"confirmationId: {_CONFIRMATION_ID}。",
        )
        execute_runs = db.query(SuperAssistantToolRun).filter(
            SuperAssistantToolRun.tool_name == "ontology_execute_action",
        ).count()
    assert result.blocked is True
    assert result.reason == "no_execute_authority"
    assert "unknown" in result.content
    assert "不会自动重放" in result.content
    assert "未变更" not in result.content
    assert execute_runs == 0
    # failed 文案同样不能断言源端未变，只能以对账为准。
    failed_authority = {
        "confirmationId": _CONFIRMATION_ID,
        "state": "failed",
        "operationId": "op-failed",
        "failureCode": None,
        "outcome": {"status": "failed", "affectedRows": 0},
    }
    monkeypatch.setattr(
        result_guard, "_load_authority",
        lambda db, user, conversation_id, cid: dict(failed_authority))
    with Session() as db:
        failed = _guard(
            db,
            "动作执行成功，已写回源库；"
            f"confirmationId: {_CONFIRMATION_ID}。",
        )
    assert failed.blocked is True
    assert "执行未完成或失败" in failed.content
    assert "源端最终状态以对账结果为准" in failed.content
    assert "未变更" not in failed.content


def test_query_columns_and_ordinary_success_words_are_not_claims(
    tmp_path, monkeypatch,
):
    """普通查询/解释含 success 词或 affectedRows 列，不得被误改写。"""
    Session = _seed(tmp_path, monkeypatch, "normal-query")
    with Session() as db:
        table_only = (
            "| orderId | affectedRows | receiptId |\n"
            "| --- | --- | --- |\n"
            "| SO-1 | 3 | null |"
        )
        assert _guard(db, table_only, action_turn=False).blocked is False
        explanation = (
            "「执行成功」是动作结果的展示状态；查询语句本身不会修改数据。"
        )
        assert _guard(db, explanation, action_turn=False).blocked is False
        # 旧 prepared 不能让后续普通查询的成功字样被改写成动作状态。
        ordinary = "执行成功，本次只是查询说明，s003 当前为 6。"
        result = _guard(db, ordinary, action_turn=False)
        assert result.blocked is False
        assert result.content == ordinary


def test_historical_success_restatement_uses_durable_authority(
    tmp_path, monkeypatch,
):
    """历史成功可在无新 execute 时复述，但必须有 succeeded 确认；原文不重写。"""
    Session = _seed(
        tmp_path, monkeypatch, "historical",
        confirmation_state="succeeded",
    )
    historical = (
        f"此前恢复动作已生效，s003 已由 6 恢复为 5"
        f"（confirmationId: {_CONFIRMATION_ID}）。"
    )
    with Session() as db:
        old = db.get(SuperAssistantMessage, "assistant-msg-old")
        old.content = historical
        db.commit()
        result = _guard(db, historical)
        db.refresh(old)
        assert result.blocked is False
        assert result.content == historical
        assert old.content == historical


def test_old_succeeded_confirmation_does_not_back_new_action_reply(
    tmp_path, monkeypatch,
):
    """无显式/本轮关联时，旧 succeeded 不能替新回复背书。"""
    Session = _seed(
        tmp_path, monkeypatch, "old-succeeded",
        confirmation_state="succeeded",
    )
    with Session() as db:
        result = _guard(db, "恢复执行成功，s003 已由 6 恢复为 5。")
    assert result.blocked is True
    assert result.reason == "no_execute_authority"
    assert "没有可核验" in result.content


def test_json_success_shape_is_guarded(tmp_path, monkeypatch):
    """纯 JSON / fenced JSON 的 succeeded+applied 不能绕过。"""
    Session = _seed(tmp_path, monkeypatch, "json-shape")
    payload = (
        '{"state":"succeeded","outcome":{"status":"applied"},'
        '"affectedRows":1}'
    )
    fenced = f"```json\n{payload}\n```"
    with Session() as db:
        for content in (payload, fenced):
            result = _guard(db, content)
            assert result.blocked is True
            assert result.reason == "no_execute_authority"
            assert "prepared" in result.content
            assert '"state":"succeeded"' not in result.content


def test_confirmation_ids_decode_from_string_arguments(
    tmp_path, monkeypatch,
):
    """既有 ToolRun.arguments 可能是 JSON 字符串，仍需解码取证。"""
    Session = _seed(tmp_path, monkeypatch, "string-args")
    with Session() as db:
        run = db.get(SuperAssistantToolRun, "run-prepare")
        run.arguments = json.dumps({"confirmationId": _CONFIRMATION_ID})
        run.result = None
        db.commit()
        result = _guard(db, "动作执行成功，已恢复为 5。")
    assert result.blocked is True
    assert "prepared" in result.content
    assert "本体动作执行记录" not in result.content


def test_extra_fabricated_ids_are_replaced_by_authority(
    tmp_path, monkeypatch,
):
    """真实成功背景中混入伪 operation/receipt，不得原样保留。"""
    Session = _seed(
        tmp_path, monkeypatch, "fake-ids",
        confirmation_state="succeeded", execute_status="success",
    )
    fake_operation = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa"
    fake_receipt = "bbbbbbbb-2222-4bbb-8bbb-bbbbbbbbbbbb"
    content = (
        "动作执行成功；"
        f"operationId: {fake_operation}，receiptId: {fake_receipt}。"
    )
    with Session() as db:
        result = _guard(db, content)
    assert result.blocked is True
    assert result.reason == "authority_canonicalized"
    assert fake_operation not in result.content
    assert fake_receipt not in result.content
    assert "state: succeeded" in result.content


def test_old_pending_confirmation_does_not_suppress_normal_query_stream(
    tmp_path, monkeypatch,
):
    """旧 pending 存在时，普通查询仍按原契约流式且正文不被改写。"""
    Session = _seed(
        tmp_path, monkeypatch, "pending-normal-query",
        user_text="s003 当前状态是什么",
    )
    content = "查询执行成功，本次只是查询说明，s003 当前为 6。"
    events, message, _runs, confirmation, _deltas = _stream(
        Session, monkeypatch, content)
    payload = _sse_payload(events, "message_end")
    assert "event: text_delta" in events
    assert payload["message"]["content"] == content
    assert message.content == content
    assert confirmation.state == "prepared"


def test_first_delta_before_prepare_call_is_not_leaked(
    tmp_path, monkeypatch,
):
    """真实时序：同轮先发假成功 delta、再返回 prepare tool_call。"""
    Session = _seed(
        tmp_path, monkeypatch, "delta-before-prepare",
        action_context=False,
    )
    responses = iter([
        {"content": _FALSE_SUCCESS, "tool_calls": [{
            "id": "call-prepare-late", "name": "ontology_prepare_action",
            "arguments": {"actionId": "act-restore", "ontologyId": "ont-s003",
                          "releaseId": "rel-1",
                          "targetInstanceId": "s003",
                          "parameters": {"value": "5"}},
        }], "usage": {"inputTokens": 10, "outputTokens": 5}},
        {"content": "准备动作未完成，平台未执行。", "tool_calls": [],
         "usage": {"inputTokens": 20, "outputTokens": 5}},
    ])
    deltas: list[str] = []

    def fake_chat_stream(_call_kwargs, _messages, _tools, on_delta=None):
        result = next(responses)
        if result.get("content") and on_delta:
            on_delta(result["content"])
            deltas.append(result["content"])
        return result

    monkeypatch.setattr(runtime.provider, "chat_stream", fake_chat_stream)
    events = "".join(runtime.stream_chat(
        conversation_id="conv-guard",
        owner_id="user-guard",
        assistant_message_id="assistant-msg-current",
        requested_model_id="model-guard",
    ))
    assert deltas and deltas[0] == _FALSE_SUCCESS
    assert "event: text_delta" not in events
    assert "恢复执行成功" not in events
    assert "receiptId" not in events
    assert "operationId" not in events
    assert "已由 6 恢复为 5" not in events
