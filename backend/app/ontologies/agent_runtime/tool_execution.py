"""Bounded tool execution with independent database sessions."""
from __future__ import annotations

import logging
import threading
import time
from copy import deepcopy
from weakref import WeakValueDictionary


from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.limits import limit
from app.ontologies.agent_runtime.models import AgentProfile
from app.ontologies.agent_runtime.toolkit import ToolRunner

logger = logging.getLogger(__name__)

# 只读工具白名单：同一回合的多个调用可并行执行（每任务独立数据库会话）。
# 涉及提案/推演/世界模型调用的工具保持串行（写状态或独占配额）。
_PARALLEL_SAFE_TOOLS = frozenset({
    "search_objects", "get_object", "traverse_links", "traverse_path", "find_paths",
    "analyze_change_impact", "aggregate_objects", "query_objects",
    "get_data_time_range", "get_object_history", "trace_causal_chain",
    "explain_sentinel_firing", "list_actions", "list_dynamic_sentinels",
    "list_world_model_services",
})
# 远程统一查询工具：统一查询内存预算按用户配额预留（首个查询会预留整个
# per-user allowance），同一用户的并行远程查询会 429；按用户串行化，其余
# 只读工具仍并行（与超级助手 owner single-flight 同口径）。
_REMOTE_QUERY_TOOLS = frozenset({
    "aggregate_objects", "query_objects", "get_data_time_range",
})
_USER_QUERY_LOCKS: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()
_USER_QUERY_LOCKS_GUARD = threading.Lock()


def _user_query_lock(user_id) -> threading.Lock:
    key = str(user_id or "")
    with _USER_QUERY_LOCKS_GUARD:
        lock = _USER_QUERY_LOCKS.get(key)
        if lock is None:
            lock = _USER_QUERY_LOCKS[key] = threading.Lock()
        return lock


def _run_tool_call(runner: ToolRunner, tc: dict) -> tuple[dict, int]:
    started = time.time()
    name = tc["name"]
    lock = (_user_query_lock(getattr(runner.user, "id", None))
            if name in _REMOTE_QUERY_TOOLS else None)
    try:
        if lock is not None:
            with lock:
                result = runner.run(name, tc.get("arguments") or {})
        else:
            result = runner.run(name, tc.get("arguments") or {})
    except ToolError as e:
        result = {"error": str(e)}
    except Exception as e:  # noqa: BLE001 — 工具内部意外不摧毁回合
        logger.exception("工具 %s 执行异常", name)
        result = {"error": f"工具内部错误: {e}"}
    return result, int((time.time() - started) * 1000)


def _parallel_task_runner(runner: ToolRunner, context: dict) -> ToolRunner:
    """并行任务专用的 ToolRunner：独立数据库会话 + 本会话内的 ORM 实例。

    ORM 对象在新会话内重取；授权配置值在派发前复制，保留评估沙箱草稿。
    查询计划由任务独立维护，回收结果时合并，避免跨线程修改共享字典。
    """
    from sqlalchemy.orm import sessionmaker
    from app.ontologies.agent_runtime.boundary import AgentScope
    from app.ontologies.release_context import CurrentReleaseContext
    from app.ontologies.versions.models import OntologyVersion
    from app.ontologies.projects.models import OntologyProject

    session = sessionmaker(bind=runner.db.get_bind())()
    try:
        ontology = session.get(OntologyProject, context["ontology_id"])
        profile = AgentProfile(**deepcopy(context["profile"]))
        from app.auth.models import User
        user = session.get(User, context["user_id"]) if context["user_id"] else None
        release = (session.get(OntologyVersion, context["release_id"])
                   if context["release_id"] else None)
        release_context = (CurrentReleaseContext(
            project=ontology, release=release, snapshot=context["snapshot"])
            if release is not None else None)
        scope = AgentScope(session, ontology, profile, release_context)
        task_runner = ToolRunner(
            session, scope,
            decision_context=deepcopy(context["decision_context"]),
            world_model_context={**context["world_model_context"], "user": user},
            user=user, query_session_id=context["query_session_id"])
        task_runner._query_spec_cache = dict(context["query_spec_cache"])
        return task_runner
    except Exception:
        session.close()
        raise


def _execute_tool_calls(runner: ToolRunner, tool_calls: list[dict]) -> list[tuple[dict, list[dict], int]]:
    """执行一批工具调用：可并行批次用有界线程池，结果按调用顺序返回。"""
    workers = min(limit("parallel_tool_workers"), len(tool_calls))
    parallel = (len(tool_calls) > 1 and workers > 1
                and all(tc["name"] in _PARALLEL_SAFE_TOOLS for tc in tool_calls))
    if not parallel:
        outcomes = []
        for tc in tool_calls:
            result, duration = _run_tool_call(runner, tc)
            outcomes.append((result, [], duration))
        return outcomes

    from concurrent.futures import ThreadPoolExecutor

    context = {
        "ontology_id": runner.scope.ontology.id,
        "release_id": runner.scope.release_id,
        "snapshot": runner.scope.release.snapshot if runner.scope.release else None,
        "profile": {column.key: deepcopy(getattr(runner.scope.profile, column.key))
                    for column in AgentProfile.__table__.columns},
        "user_id": getattr(runner.user, "id", None),
        "decision_context": deepcopy(runner.decision_context),
        "world_model_context": {key: value for key, value in runner.world_model_context.items()
                                if key != "user"},
        "query_session_id": runner.query_session_id,
        "query_spec_cache": dict(runner._query_spec_cache),
    }

    def task(tc: dict):
        task_runner = _parallel_task_runner(runner, context)
        try:
            result, duration = _run_tool_call(task_runner, tc)
            return (result, task_runner.citations, duration,
                    task_runner._query_spec_cache, task_runner._open_queries)
        finally:
            task_runner.db.rollback()
            task_runner.db.close()

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="agent-tools") as pool:
        futures = [pool.submit(task, tc) for tc in tool_calls]
        outcomes = []
        for future in futures:
            result, citations, duration, query_specs, open_queries = future.result()
            runner._query_spec_cache.update(query_specs)
            runner._open_queries.update(open_queries)
            outcomes.append((result, citations, duration))
        return outcomes
