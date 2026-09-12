"""历史滚动摘要：LLM 生成、滚动性、持久化注入与失败回退确定性压缩。

fake LLM 风格与存量测试一致：monkeypatch llm_bridge.chat（其模块对象即
app.model_configs.llm_gateway，与 context_builder 的调用点相同）。
"""
import pytest

from app.exploration.canvas import empty_canvas
from app.exploration.models import ExplorationMessage, ExplorationSession
from app.exploration.orchestrator import _prepare_history

_CALL_KWARGS = {"model": "fake-summary", "max_context_tokens": 16_384,
                "max_output_tokens": 2_048}


def _make_session(db, admin_user, message_count: int = 40) -> ExplorationSession:
    session = ExplorationSession(user_id=admin_user.id, title="滚动摘要",
                                 canvas=empty_canvas())
    db.add(session)
    db.commit()
    db.refresh(session)
    for i in range(message_count):
        db.add(ExplorationMessage(
            session_id=session.id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"第 {i + 1} 条业务讨论：订单状态与规则"))
    db.commit()
    return session


def test_llm_rolling_summary_persists_and_injects(db, admin_user, monkeypatch):
    from app.ontologies.agent_runtime import llm_bridge
    session = _make_session(db, admin_user)
    captured = {}

    def fake_chat(call_kwargs, messages, tools):
        captured["kwargs"] = call_kwargs
        captured["messages"] = messages
        captured["tools"] = tools
        return {"content": "滚动摘要：用户确认高风险订单阈值≥88000元，需财务总监审批。",
                "tool_calls": [], "usage": None}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    system, recent = _prepare_history(db, session, dict(_CALL_KWARGS),
                                      "继续讨论", "", {})
    assert len(recent) == 16
    assert session.summary_message_count == 24
    assert session.context_summary == "滚动摘要：用户确认高风险订单阈值≥88000元，需财务总监审批。"
    assert session.context_stats["summaryMode"] == "llm"
    assert session.context_stats["summarizedMessages"] == 24
    # 摘要调用预算收口：输出 ≤800 tokens，不带工具
    assert captured["kwargs"]["max_output_tokens"] == 800
    assert captured["tools"] == []
    # 本轮系统提示即注入压缩摘要块
    assert "# 已压缩的早期会话" in system
    assert "高风险订单阈值≥88000元" in system

    # 下一轮：已持久化的摘要继续注入，不再重复压缩（剩余 16 条 ≤ 触发线）
    system2, recent2 = _prepare_history(db, session, dict(_CALL_KWARGS),
                                        "再讨论", "", {})
    assert "高风险订单阈值≥88000元" in system2
    assert len(recent2) == 16
    assert session.summary_message_count == 24


def test_rolling_summary_input_contains_previous_summary(db, admin_user, monkeypatch):
    from app.ontologies.agent_runtime import llm_bridge
    session = _make_session(db, admin_user, message_count=60)
    session.context_summary = "第一轮摘要：阈值 88000 元已确认"
    session.summary_message_count = 16
    db.commit()

    prompts: list[str] = []

    def fake_chat(call_kwargs, messages, tools):
        prompts.append(messages[-1]["content"])
        return {"content": "第二轮滚动摘要", "tool_calls": [], "usage": None}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    _prepare_history(db, session, dict(_CALL_KWARGS), "继续", "", {})
    # 待压段 = 第 17~44 条（60-16=44 条 pending，保留最近 16 条 → 压 28 条）
    assert session.summary_message_count == 44
    assert session.context_summary == "第二轮滚动摘要"
    # 滚动性：第二轮摘要输入携带第一轮 summary 与待压消息段
    assert "第一轮摘要：阈值 88000 元已确认" in prompts[0]
    assert "第 17 条业务讨论" in prompts[0]
    assert "第 44 条业务讨论" in prompts[0]
    assert "第 45 条业务讨论" not in prompts[0]


def test_rolling_summary_falls_back_to_deterministic_when_llm_fails(
        db, admin_user, monkeypatch):
    from app.ontologies.agent_runtime import llm_bridge
    session = _make_session(db, admin_user)

    def boom(call_kwargs, messages, tools):
        raise llm_bridge.LLMError("provider down")

    monkeypatch.setattr(llm_bridge, "chat", boom)

    # LLM 异常不得炸回合：回退到与引入 LLM 前完全一致的确定性压缩
    system, recent = _prepare_history(db, session, dict(_CALL_KWARGS),
                                      "继续讨论", "", {})
    assert len(recent) == 16
    assert session.summary_message_count == 24
    assert "已压缩消息 1-24" in session.context_summary
    assert "- 用户: 第 1 条业务讨论" in session.context_summary
    assert session.context_stats["summaryMode"] == "deterministic"
    assert "# 已压缩的早期会话" in system


def test_rolling_summary_empty_llm_response_falls_back(db, admin_user, monkeypatch):
    from app.ontologies.agent_runtime import llm_bridge
    session = _make_session(db, admin_user)

    def empty_chat(call_kwargs, messages, tools):
        return {"content": "   ", "tool_calls": [], "usage": None}

    monkeypatch.setattr(llm_bridge, "chat", empty_chat)

    _prepare_history(db, session, dict(_CALL_KWARGS), "继续讨论", "", {})
    assert session.summary_message_count == 24
    assert "已压缩消息 1-24" in session.context_summary
    assert session.context_stats["summaryMode"] == "deterministic"


def test_rolling_summary_skipped_without_llm_config(db, admin_user, monkeypatch):
    from app.ontologies.agent_runtime import llm_bridge
    session = _make_session(db, admin_user)
    calls: list = []
    monkeypatch.setattr(llm_bridge, "chat",
                        lambda *args: calls.append(args))

    # 调用方未给可用模型配置（无 model 键）→ 不调 LLM，直接确定性压缩
    _prepare_history(db, session,
                     {"max_context_tokens": 16_384, "max_output_tokens": 2_048},
                     "继续讨论", "", {})
    assert calls == []
    assert session.summary_message_count == 24
    assert "已压缩消息 1-24" in session.context_summary
