"""查询句柄生命周期 — 终页回收/主动取消（本地快照与远程 keyset 同一口径）。

叶子模块：只依赖 agent_query 服务，供 unified_query_tools 与 sequence_tools
共用，避免工具模块之间反向 import 形成架构依赖环。
"""
from __future__ import annotations


def _release_terminal_page(db, user, session_id: str, data: dict) -> bool:
    """终页回收：hasNext=false 时释放快照/远程句柄；失败不影响成功查询。"""
    if data.get("hasNext") is not False or not data.get("queryId"):
        return False
    from app.ontologies.agent_query.remote_query import cancel_remote_query
    from app.ontologies.agent_query.service import release_query_snapshot
    try:
        if isinstance(data.get("persistence"), dict):
            cancel_remote_query(db, user, str(data["queryId"]))
        else:
            released = release_query_snapshot(
                db, user, str(data["queryId"]),
                session_kind="ontology_agent", session_id=session_id or "")
            if not released:
                cancel_remote_query(db, user, str(data["queryId"]))
        return True
    except Exception:  # noqa: BLE001 - 回收失败不破坏成功查询
        return False


def _cancel_handle(db, user, data: dict) -> None:
    """主动取消当前执行句柄（中途中止/下钻切换）。"""
    from app.ontologies.agent_query.remote_query import cancel_remote_query
    from app.ontologies.agent_query.service import release_query_snapshot
    query_id = data.get("queryId")
    if not query_id:
        return
    try:
        if isinstance(data.get("persistence"), dict):
            cancel_remote_query(db, user, str(query_id))
        else:
            if not release_query_snapshot(db, user, str(query_id)):
                cancel_remote_query(db, user, str(query_id))
    except Exception:  # noqa: BLE001
        pass
