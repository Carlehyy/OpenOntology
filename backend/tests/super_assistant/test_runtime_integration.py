"""runtime 流式化与自我进化接入的集成测试。

沿用 test_runtime.py 的隔离 sqlite 手法：每张用例自建引擎与会话，
provider.chat_stream 一律伪造，不触网。
"""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import RoleMenuPermission, User
from app.model_configs.models import ModelConfig
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import runtime
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMcpServer,
    SuperAssistantMemory,
    SuperAssistantMemoryProfile,
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
    RoleMenuPermission.__table__,  # 委派目录注入的菜单权限查询
    SuperAssistantConversation.__table__, SuperAssistantSkill.__table__,
    SuperAssistantMcpServer.__table__, SuperAssistantMessage.__table__,
    SuperAssistantToolRun.__table__, SuperAssistantToolSetting.__table__, SuperAssistantMemory.__table__,
    SuperAssistantMulticaConfig.__table__,
    SuperAssistantMemoryProfile.__table__, SuperAssistantReflectionRun.__table__,
    SuperAssistantReflectionCandidate.__table__,
]


def _seed(tmp_path, monkeypatch, name, *, user_content="你好"):
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
            role="user", content=user_content, status="complete",
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


def test_interim_text_streams_live_and_concatenates(tmp_path, monkeypatch):
    """带 tool_calls 轮次的过程文本同样实时流出，且拼入最终内容。"""
    _, ids = _seed(tmp_path, monkeypatch, "interim")
    responses = iter([
        {
            "content": "先想一下。",
            "tool_calls": [{"id": "c1", "name": "think", "arguments": {"thought": "梳理"}}],
            "usage": {"inputTokens": 5, "outputTokens": 2},
        },
        _text("最终答案。", inputTokens=9, outputTokens=3),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))

    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert events.index("先想一下。") < events.index("event: tool_start") < events.index("最终答案。")

    TestingSession = sessionmaker(bind=create_engine(
        f"sqlite:///{tmp_path / 'interim.db'}", connect_args={"check_same_thread": False},
    ))
    with TestingSession() as db:
        saved = db.get(SuperAssistantMessage, ids["assistant_message_id"])
        assert saved.content == "先想一下。最终答案。"
        think_run = db.query(SuperAssistantToolRun).one()
        assert think_run.tool_name == "think"
        assert think_run.status == "success"
        assert "已记录：梳理" in think_run.result


def test_permission_deny_skips_execution(tmp_path, monkeypatch):
    _, ids = _seed(tmp_path, monkeypatch, "deny")
    monkeypatch.setattr(settings, "super_assistant_tool_deny", "memory_*")
    responses = iter([
        {
            "content": None,
            "tool_calls": [{"id": "c1", "name": "memory_search", "arguments": {"query": "偏好"}}],
            "usage": {},
        },
        _text("无法查询。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert "已被权限规则拒绝" in events

    TestingSession = sessionmaker(bind=create_engine(
        f"sqlite:///{tmp_path / 'deny.db'}", connect_args={"check_same_thread": False},
    ))
    with TestingSession() as db:
        run = db.query(SuperAssistantToolRun).one()
        assert run.status == "denied"
        assert "权限" in run.result
        # 未真正执行：不应产生任何记忆副作用
        assert db.query(SuperAssistantMemory).count() == 0


def test_read_only_tools_run_parallel_and_results_in_call_order(tmp_path, monkeypatch):
    _, ids = _seed(tmp_path, monkeypatch, "parallel")
    executed: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_execute_read_only_tool",
        lambda name, arguments, context: executed.append(name) or json.dumps({"ok": name}),
    )
    responses = iter([
        {
            "content": None,
            "tool_calls": [
                {"id": "c1", "name": "palace_zones", "arguments": {}},
                {"id": "c2", "name": "think", "arguments": {"thought": "x"}},
            ],
            "usage": {},
        },
        _text("完成。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert sorted(executed) == ["palace_zones", "think"]
    # tool_result 严格按 call 顺序（c1 在 c2 前）；SSE data 内嵌 JSON 字符串带转义
    assert events.index('\\"ok\\": \\"palace_zones\\"') < events.index('\\"ok\\": \\"think\\"')


def test_truncated_tool_call_is_not_executed(tmp_path, monkeypatch):
    _, ids = _seed(tmp_path, monkeypatch, "truncated")
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "c1", "name": "think",
                "arguments": {"_truncated": True, "_raw": "{\"thought\":"},
            }],
            "usage": {},
        },
        _text("被截断。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert "工具调用被 max_tokens 截断" in events

    TestingSession = sessionmaker(bind=create_engine(
        f"sqlite:///{tmp_path / 'truncated.db'}", connect_args={"check_same_thread": False},
    ))
    with TestingSession() as db:
        run = db.query(SuperAssistantToolRun).one()
        assert run.status == "error"
        assert "截断" in (run.error or "")


def test_memory_save_auto_accepts_then_stages_conflict(tmp_path, monkeypatch):
    _, ids = _seed(tmp_path, monkeypatch, "save")
    responses = iter([
        {
            "content": None,
            "tool_calls": [
                {"id": "c1", "name": "memory_save", "arguments": {"content": "用户偏好简洁回答", "zone": "core"}},
                {"id": "c2", "name": "memory_save", "arguments": {"content": "用户偏好简洁回答"}},
            ],
            "usage": {},
        },
        _text("已处理。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert '\\"saved\\": true' in events
    assert "已提交审批" in events

    TestingSession = sessionmaker(bind=create_engine(
        f"sqlite:///{tmp_path / 'save.db'}", connect_args={"check_same_thread": False},
    ))
    with TestingSession() as db:
        memory = db.query(SuperAssistantMemory).one()
        assert memory.zone == "core"
        assert memory.source == "reflection"
        candidate = db.query(SuperAssistantReflectionCandidate).one()
        assert candidate.kind == "memory"
        assert candidate.status == "pending"
        run = db.query(SuperAssistantReflectionRun).one()
        assert run.kind == "manual"


def test_memory_save_stages_candidate_when_auto_accept_disabled(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "noauto")
    with TestingSession() as db:
        db.add(SuperAssistantMemoryProfile(
            owner_id=ids["owner_id"], auto_accept_enabled=False,
        ))
        db.commit()
    responses = iter([
        {
            "content": None,
            "tool_calls": [{"id": "c1", "name": "memory_save", "arguments": {"content": "记住一条事实"}}],
            "usage": {},
        },
        _text("好。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert "已提交审批" in events
    with TestingSession() as db:
        assert db.query(SuperAssistantMemory).count() == 0
        assert db.query(SuperAssistantReflectionCandidate).count() == 1


def test_system_prompt_includes_pinned_memory(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "prompt")
    with TestingSession() as db:
        db.add(SuperAssistantMemory(
            id="mem-1", owner_id=ids["owner_id"], content="用户是平台管理员",
            zone="core", pinned=True, confidence="high", source="user",
            tags=[], supersedes=[],
        ))
        db.commit()
    seen: list = []

    def _fake(_call_kwargs, messages, _tools, on_delta=None):
        seen.append(messages[0]["content"])
        return _text("收到。")

    monkeypatch.setattr(runtime.provider, "chat_stream", _fake)
    "".join(runtime.stream_chat(**_stream_args(ids)))
    assert "用户是平台管理员" in seen[0]
    assert "Pinned memories" in seen[0]


def test_micro_reflection_dispatched_on_explicit_intent(tmp_path, monkeypatch):
    _, ids = _seed(tmp_path, monkeypatch, "reflect", user_content="请记住：我喜欢简洁的回答")
    dispatched: list = []
    monkeypatch.setattr(
        runtime, "dispatch_super_assistant_reflection",
        lambda kind, payload: dispatched.append((kind, payload)),
    )
    monkeypatch.setattr(
        runtime.provider, "chat_stream", _fake_chat_stream(iter([_text("好的，已记住。")])),
    )
    events = "".join(runtime.stream_chat(**_stream_args(ids)))
    assert "event: message_end" in events
    assert dispatched == [("micro", {
        "owner_id": ids["owner_id"],
        "conversation_id": ids["conversation_id"],
        "message_id": ids["assistant_message_id"],
    })]


def test_micro_reflection_falls_back_inline_without_nats(tmp_path, monkeypatch):
    _, ids = _seed(tmp_path, monkeypatch, "inline", user_content="请记住：inline 降级")
    monkeypatch.setattr(
        runtime, "dispatch_super_assistant_reflection",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("后台任务派发失败：未配置 NATS_URL")),
    )
    threads: list = []
    real_thread = runtime.threading.Thread

    class _SpyThread(real_thread):
        def __init__(self, *args, **kwargs):
            threads.append(kwargs.get("name") or (args[1].__name__ if len(args) > 1 else None))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(runtime.threading, "Thread", _SpyThread)
    monkeypatch.setattr(
        runtime.provider, "chat_stream", _fake_chat_stream(iter([_text("好的。")])),
    )
    "".join(runtime.stream_chat(**_stream_args(ids)))
    assert "sa-micro-reflect" in threads


def test_compaction_invoked_each_round(tmp_path, monkeypatch):
    _, ids = _seed(tmp_path, monkeypatch, "compact")
    calls: list = []
    original = runtime.maybe_compact

    def _spy(db, conversation, call_kwargs, messages):
        calls.append(len(messages))
        return original.__wrapped__(db, conversation, call_kwargs, messages) if hasattr(original, "__wrapped__") else messages

    monkeypatch.setattr(runtime, "maybe_compact", _spy)
    responses = iter([
        {
            "content": None,
            "tool_calls": [{"id": "c1", "name": "think", "arguments": {"thought": "x"}}],
            "usage": {},
        },
        _text("完成。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    "".join(runtime.stream_chat(**_stream_args(ids)))
    assert len(calls) == 2


def test_cancel_tool_placeholders_pairs_orphan_calls():
    messages = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "name": "think"}, {"id": "c2", "name": "web_fetch"},
    ]}]
    order = [
        {"id": "c1", "name": "think"},
        {"id": "c2", "name": "web_fetch"},
    ]
    executed = {"c1": ("{}", "success", None)}
    runtime._cancel_tool_placeholders(messages, order, executed)
    assert messages[-1] == {
        "role": "tool", "tool_call_id": "c2",
        "name": "web_fetch", "content": "Tool call cancelled",
    }


# ---------------------------------------------------------------------------
# 自主 agent 模式（PLAN→EXECUTE→VERIFY + 目标标记 + todo 清单工具）
# ---------------------------------------------------------------------------


def _agent_stream_args(ids):
    return {**_stream_args(ids), "agent_mode": True}


def _reopen(tmp_path, name):
    return sessionmaker(bind=create_engine(
        f"sqlite:///{tmp_path / name}.db", connect_args={"check_same_thread": False},
    ))


def test_agent_mode_catalog_and_prompt_include_todo_tools(tmp_path, monkeypatch):
    """agent 模式：目录含 todo 工具，system prompt 追加自主模式段，完成标记被剥离。"""
    _, ids = _seed(tmp_path, monkeypatch, "agent-catalog")
    captured: dict = {}

    def _fake(_call_kwargs, messages, tools, on_delta=None):
        captured["tools"] = [tool["name"] for tool in tools]
        captured["system"] = messages[0]["content"]
        return _text("[GOAL_COMPLETE] 目标已达成。")

    monkeypatch.setattr(runtime.provider, "chat_stream", _fake)
    events = "".join(runtime.stream_chat(**_agent_stream_args(ids)))

    assert "todo_write" in captured["tools"]
    assert "todo_read" in captured["tools"]
    assert "自主执行模式" in captured["system"]
    assert "PLAN" in captured["system"]
    assert "[GOAL_COMPLETE]" in captured["system"]
    assert "[GOAL_COMPLETE]" not in events

    with _reopen(tmp_path, "agent-catalog")() as db:
        saved = db.get(SuperAssistantMessage, ids["assistant_message_id"])
        assert saved.status == "complete"
        assert saved.content == "目标已达成。"


def test_agent_mode_todo_write_then_read_shares_state(tmp_path, monkeypatch):
    """todo 清单是本轮 stream_chat 的内存态：todo_write 后 todo_read 同轮次内可读。"""
    _, ids = _seed(tmp_path, monkeypatch, "agent-todo")
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "c1", "name": "todo_write",
                "arguments": {"items": ["收集订单数据", "汇总并给出结论"]},
            }],
            "usage": {},
        },
        {
            "content": None,
            "tool_calls": [{"id": "c2", "name": "todo_read", "arguments": {}}],
            "usage": {},
        },
        _text("[GOAL_COMPLETE] 已完成全部步骤。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_agent_stream_args(ids)))
    assert "1. 收集订单数据" in events
    assert "2. 汇总并给出结论" in events

    with _reopen(tmp_path, "agent-todo")() as db:
        runs = {run.tool_name: run for run in db.query(SuperAssistantToolRun).all()}
        assert runs["todo_write"].status == "success"
        assert runs["todo_read"].status == "success"
        assert "1. 收集订单数据" in runs["todo_read"].result
        saved = db.get(SuperAssistantMessage, ids["assistant_message_id"])
        assert saved.content == "已完成全部步骤。"


def test_agent_mode_uses_agent_iteration_limit(tmp_path, monkeypatch):
    """agent 模式迭代上限取 super_assistant_agent_max_iterations，不被 max_tool_rounds 截断。"""
    _, ids = _seed(tmp_path, monkeypatch, "agent-rounds")
    monkeypatch.setattr(settings, "super_assistant_max_tool_rounds", 8)
    monkeypatch.setattr(settings, "super_assistant_agent_max_iterations", 50)
    responses = iter([
        *(
            {
                "content": None,
                "tool_calls": [{
                    "id": f"c{index}", "name": "think",
                    "arguments": {"thought": f"第 {index} 步"},
                }],
                "usage": {},
            }
            for index in range(12)
        ),
        _text("[GOAL_COMPLETE] 十二步全部完成。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_agent_stream_args(ids)))
    assert "event: message_end" in events

    with _reopen(tmp_path, "agent-rounds")() as db:
        runs = db.query(SuperAssistantToolRun).all()
        assert len(runs) == 12
        assert {run.status for run in runs} == {"success"}
        saved = db.get(SuperAssistantMessage, ids["assistant_message_id"])
        assert saved.status == "complete"
        assert saved.content == "十二步全部完成。"


def test_agent_mode_goal_failed_breaks_loop_and_records_step(tmp_path, monkeypatch):
    """[GOAL_FAILED] 跳出迭代：content 剥离标记，steps 追加 agent failed 记录。"""
    _, ids = _seed(tmp_path, monkeypatch, "agent-failed")
    responses = iter([
        {
            "content": None,
            "tool_calls": [{"id": "c1", "name": "think", "arguments": {"thought": "尝试"}}],
            "usage": {},
        },
        _text("[GOAL_FAILED] 无法完成：缺少订单数据。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    events = "".join(runtime.stream_chat(**_agent_stream_args(ids)))
    # text_delta 是实时增量（协议不变），message_end 落库内容必须已剥离标记
    assert "[GOAL_FAILED]" not in events.split("event: message_end")[-1]

    with _reopen(tmp_path, "agent-failed")() as db:
        saved = db.get(SuperAssistantMessage, ids["assistant_message_id"])
        assert saved.status == "complete"
        assert saved.content == "无法完成：缺少订单数据。"
        assert {"toolName": "agent", "status": "failed"} in saved.steps


def test_non_agent_mode_has_no_todo_tools(tmp_path, monkeypatch):
    """非 agent 模式：目录无 todo 工具，system prompt 不含自主模式段。"""
    _, ids = _seed(tmp_path, monkeypatch, "no-agent")
    captured: dict = {}

    def _fake(_call_kwargs, messages, tools, on_delta=None):
        captured["tools"] = [tool["name"] for tool in tools]
        captured["system"] = messages[0]["content"]
        return _text("普通答复。")

    monkeypatch.setattr(runtime.provider, "chat_stream", _fake)
    "".join(runtime.stream_chat(**_stream_args(ids)))
    assert "todo_write" not in captured["tools"]
    assert "todo_read" not in captured["tools"]
    assert "自主执行模式" not in captured["system"]


def test_non_agent_mode_keeps_configured_round_limit(tmp_path, monkeypatch):
    """非 agent 模式仍以 super_assistant_max_tool_rounds 为上限（走无工具收尾）。"""
    _, ids = _seed(tmp_path, monkeypatch, "no-agent-limit")
    monkeypatch.setattr(settings, "super_assistant_max_tool_rounds", 1)
    monkeypatch.setattr(settings, "super_assistant_agent_max_iterations", 50)
    seen_tool_counts: list[int] = []
    responses = iter([
        {
            "content": None,
            "tool_calls": [{"id": "c1", "name": "think", "arguments": {"thought": "x"}}],
            "usage": {},
        },
        _text("总结答复。"),
    ])

    def _fake(_call_kwargs, _messages, tools, on_delta=None):
        seen_tool_counts.append(len(tools))
        return next(responses)

    monkeypatch.setattr(runtime.provider, "chat_stream", _fake)
    "".join(runtime.stream_chat(**_stream_args(ids)))
    # 第二轮是 for/else 的无工具总结调用：证明 max_tool_rounds=1 已生效
    assert seen_tool_counts[-1] == 0

    with _reopen(tmp_path, "no-agent-limit")() as db:
        saved = db.get(SuperAssistantMessage, ids["assistant_message_id"])
        assert saved.content == "总结答复。"
