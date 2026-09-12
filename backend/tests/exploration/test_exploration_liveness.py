"""llm_round 活性心跳：每次 provider 调用前的轻量 heartbeat 事件。

契约（2026-09 加性 SSE 变更）：心跳是独立 {"type": "heartbeat"} 事件，
字段形状与 step 一致（tool/arguments/summary/durationMs）；不是真实工具
步骤 —— 不进持久化 steps、不参与反虚构守卫对证；非流式（stream=false）
聚合响应忽略 heartbeat 类型。思考型模型 think 流被过滤、首轮 TTFT 可达
分钟级，前端靠它在等待期间给出可见反馈。附件索引卡懒生成的进度 step
（tool=attachment_card）同样不进持久化与非流式聚合 steps。

fake LLM 风格与存量测试一致：monkeypatch llm_bridge.chat / chat_stream。
"""
import uuid
from types import SimpleNamespace

from app.exploration import orchestrator as OR
from app.exploration import streaming_service
from app.exploration.canvas import empty_canvas
from app.exploration.models import ExplorationMessage, ExplorationSession


def _make_session(db) -> ExplorationSession:
    session = ExplorationSession(id=str(uuid.uuid4()), title="liveness",
                                 canvas=empty_canvas(), canvas_version=0)
    db.add(session)
    db.commit()
    return session


def _patch_model(monkeypatch):
    monkeypatch.setattr(OR, "select_llm_model_config",
                        lambda db, model_id=None: object())
    monkeypatch.setattr(OR, "llm_call_kwargs", lambda cfg: {"model": "fake"})


def _patch_fallback_chat(monkeypatch):
    monkeypatch.setattr(
        OR.llm_bridge, "chat",
        lambda *_a: {"content": "兜底。", "tool_calls": [], "usage": None})


def test_liveness_step_precedes_each_provider_call_and_skips_persistence(
        db, monkeypatch):
    """两轮回合：每轮调用前一个 llm_round 心跳（heartbeat 类型）；真实 steps
    与持久化不受影响；usage.inputTokens 驱动 tokenCalib EMA（样本 clamp 到
    2.0：1.0→1.3→1.51）。"""
    session = _make_session(db)
    _patch_model(monkeypatch)
    _patch_fallback_chat(monkeypatch)
    calls = {"n": 0}

    def fake_chat_stream(_ck, _messages, _tools):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"final": {
                "content": None,
                "tool_calls": [{"id": "t1", "name": "todo_write",
                                "arguments": {"items": [
                                    {"content": "建对象",
                                     "status": "in_progress"}]}}],
                "usage": {"inputTokens": 1_000_000, "outputTokens": 5}}}
        else:
            yield {"final": {"content": "计划已建，接下来写入画布。",
                             "tool_calls": [],
                             "usage": {"inputTokens": 1_000_000,
                                       "outputTokens": 5}}}

    monkeypatch.setattr(OR.llm_bridge, "chat_stream", fake_chat_stream)

    events = list(OR.run_exploration_turn(
        db, session.id, user=object(), message="建模"))
    step_events = [e for e in events if e.get("type") == "step"]
    liveness = [e for e in events
                if e.get("type") == "heartbeat" and e["tool"] == "llm_round"]
    # 每轮 provider 调用前一个心跳，字段形状与 step 一致
    assert [e["arguments"]["round"] for e in liveness] == [1, 2]
    assert all(e["summary"] == "正在思考与生成…" and e["durationMs"] == 0
               for e in liveness)
    # 心跳先于本回合第一个真实 step；step 事件列表不受心跳污染
    assert events.index(liveness[0]) < events.index(step_events[0])
    assert [e["tool"] for e in step_events] == ["todo_write"]
    # 活性心跳不进入持久化 steps（历史回放只含真实工具步骤）
    row = (db.query(ExplorationMessage)
           .filter_by(session_id=session.id, role="assistant").one())
    assert [s["tool"] for s in row.steps] == ["todo_write"]
    # usage 真实校准：1M/估算 的样本被 clamp 到 2.0，EMA 两步 1.0→1.3→1.51
    stats = db.query(ExplorationSession).filter_by(id=session.id).one().context_stats
    assert stats["tokenCalib"] == {"ratio": 1.51, "samples": 2}
    # 装配发生在样本攒够之前：本回合预算估算照旧（ratio 1.0）
    assert stats["tokenCalibRatio"] == 1.0


def test_liveness_step_not_counted_by_fabrication_guard(db, monkeypatch):
    """零真实工具回合（只有活性心跳）声称写入 → 反虚构守卫照常纠偏重试。"""
    session = _make_session(db)
    _patch_model(monkeypatch)
    _patch_fallback_chat(monkeypatch)
    calls = {"n": 0}

    def fake_chat_stream(_ck, messages, _tools):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"final": {
                "content": "已成功写入 3 个对象（画布 v0 → v1）。",
                "tool_calls": [], "usage": None}}
        else:
            assert "服务端核对" in messages[-1]["content"]
            yield {"final": {"content": "接下来将调用 upsert_elements 写入画布。",
                             "tool_calls": [], "usage": None}}

    monkeypatch.setattr(OR.llm_bridge, "chat_stream", fake_chat_stream)

    events = list(OR.run_exploration_turn(
        db, session.id, user=object(), message="建模"))
    assert calls["n"] == 2
    assert any(e.get("type") == "heartbeat" and e.get("tool") == "llm_round"
               for e in events)
    answer = next(e for e in events if e["type"] == "answer")
    assert answer["content"] == "接下来将调用 upsert_elements 写入画布。"
    row = db.query(ExplorationSession).filter_by(id=session.id).one()
    assert row.context_stats.get("fabricationRetries") == 1
    persisted = (db.query(ExplorationMessage)
                 .filter_by(session_id=session.id, role="assistant").one())
    assert all(s["tool"] != "llm_round" for s in persisted.steps)


def test_non_stream_response_filters_liveness_steps(db):
    """stream=false 聚合响应：heartbeat 类型整体忽略，attachment_card 进度
    step 被过滤，steps 只含真实工具步骤。"""
    def fake_run_turn_fn(*_args, **_kwargs):
        yield {"type": "meta", "sessionId": "s1", "model": "fake"}
        yield {"type": "heartbeat", "tool": "llm_round",
               "arguments": {"round": 1}, "summary": "正在思考与生成…",
               "durationMs": 0}
        yield {"type": "step", "tool": "attachment_card",
               "arguments": {"file": "a.txt", "index": 1},
               "summary": "正在为附件生成索引卡（1/3）…", "durationMs": 0}
        yield {"type": "step", "tool": "todo_write",
               "arguments": {"items": []}, "summary": "更新建模计划（0/1 完成）",
               "durationMs": 3}
        yield {"type": "answer", "content": "好。", "usage": {}}
        yield {"type": "done"}

    body = SimpleNamespace(message="建模", stream=False, model_id=None,
                           web_search=False)
    result = streaming_service.chat(
        "s1", body, db, object(),
        require_session_fn=lambda *_a: None,
        run_turn_fn=fake_run_turn_fn,
        ok_fn=lambda data: data,
    )
    assert [s["tool"] for s in result["steps"]] == ["todo_write"]
    assert all("type" not in s for s in result["steps"])
    assert result["content"] == "好。"
