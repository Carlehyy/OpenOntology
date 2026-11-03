"""同回合多工具调用的并行执行与用户级远程查询串行化。

并行批次每任务使用独立数据库会话与重取的 ORM 实例；结果按模型发起顺序
返回。远程统一查询共享按用户预留的内存预算（首个查询预留整个 per-user
allowance），因此按用户串行化 —— 与超级助手 owner single-flight 同口径。
"""
import threading
import time

from app.ontologies.agent_runtime import orchestrator
from app.ontologies.agent_runtime.boundary import build_scope
from app.ontologies.agent_runtime.toolkit import ToolRunner
from tests.ontologies.test_agent_unified_query import orders_ontology  # noqa: F401


def _runner(db, orders, user) -> ToolRunner:
    _, _, scope = build_scope(db, orders["ontologyId"],
                              release_id=orders["releaseId"])
    return ToolRunner(db, scope, user=user, query_session_id="s-par")


def _instrument(monkeypatch, sleep=0.35):
    state = {"active": 0, "peak": 0, "guard": threading.Lock()}

    def fake_run(self, name, args):
        with state["guard"]:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(sleep)
        with state["guard"]:
            state["active"] -= 1
        return {"ok": name}

    monkeypatch.setattr(ToolRunner, "run", fake_run)
    return state


def test_read_only_calls_run_in_parallel_and_keep_order(
        db, admin_user, orders_ontology, monkeypatch):
    runner = _runner(db, orders_ontology, admin_user)
    state = _instrument(monkeypatch)
    calls = [{"id": "c1", "name": "list_actions", "arguments": {}},
             {"id": "c2", "name": "list_dynamic_sentinels", "arguments": {}}]

    started = time.time()
    outcomes = orchestrator._execute_tool_calls(runner, calls)
    elapsed = time.time() - started

    assert elapsed < 0.65  # 串行至少 0.7s
    assert state["peak"] == 2  # 真并发
    assert [outcome[0].get("ok") for outcome in outcomes] == [
        "list_actions", "list_dynamic_sentinels"]
    assert all(isinstance(outcome[2], int) for outcome in outcomes)


def test_remote_query_tools_serialize_per_user(
        db, admin_user, orders_ontology, monkeypatch):
    runner = _runner(db, orders_ontology, admin_user)
    state = _instrument(monkeypatch)
    calls = [{"id": "c1", "name": "aggregate_objects", "arguments": {}},
             {"id": "c2", "name": "query_objects", "arguments": {}}]

    orchestrator._execute_tool_calls(runner, calls)
    assert state["peak"] == 1  # 同一用户的远程统一查询不并发


def test_serial_fallback_when_workers_disabled(
        db, admin_user, orders_ontology, monkeypatch):
    monkeypatch.setenv("AGENT_PARALLEL_TOOL_WORKERS", "1")
    runner = _runner(db, orders_ontology, admin_user)
    state = _instrument(monkeypatch, sleep=0.2)
    calls = [{"id": "c1", "name": "list_actions", "arguments": {}},
             {"id": "c2", "name": "list_dynamic_sentinels", "arguments": {}}]

    started = time.time()
    outcomes = orchestrator._execute_tool_calls(runner, calls)
    assert time.time() - started >= 0.38
    assert state["peak"] == 1
    assert len(outcomes) == 2


def test_unsafe_tool_mix_runs_serially(
        db, admin_user, orders_ontology, monkeypatch):
    runner = _runner(db, orders_ontology, admin_user)
    state = _instrument(monkeypatch, sleep=0.2)
    # propose_action 不在只读白名单：整批回退串行
    calls = [{"id": "c1", "name": "list_actions", "arguments": {}},
             {"id": "c2", "name": "propose_action", "arguments": {}}]

    outcomes = orchestrator._execute_tool_calls(runner, calls)
    assert state["peak"] == 1
    assert [outcome[0].get("ok") for outcome in outcomes] == [
        "list_actions", "propose_action"]
