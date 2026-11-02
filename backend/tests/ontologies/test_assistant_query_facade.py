"""超级助手专属查询门面契约：终页释放、queryLimits 与受控 pageSize 错误。

缺陷回归（b7ccf202 基线）：
- 门面 query/query_page 完成单页/终页后不释放远程 memory handle 与本地
  AgentQuerySnapshot/AgentQueryRow，连续 11 次单页统计即撞句柄/快照配额；
- structure 工具输出不可发现 queryLimits；pageSize 上限错误不带受控字段；
- build_query_request 对 0/201/非法类型/与 cursor 混用静默忽略或抛
  Pydantic 原始异常（落到 ontology_tool_failed）。

本文件只验证助手门面/agent_query 内部接口；公共直查 HTTP 行为在
test_agent_unified_query.py 中保持并回归。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.data_channel.query.ontology_adapter import _response as remote_response
from app.ontologies.agent_query import remote_query
from app.ontologies.agent_query import schemas as QS
from app.ontologies.agent_query import service as query_service
from app.ontologies.agent_query.models import AgentQueryRow, AgentQuerySnapshot
from app.ontologies.agent_query.service import QueryError
from app.ontologies.agent_runtime import assistant_facade
from app.ontologies.agent_runtime.assistant_facade import FacadeError
from app.super_assistant import ontology_tools
from app.super_assistant.models import SuperAssistantConversation
from tests.ontologies.test_agent_remote_aggregation import (
    _freeze_with_rich_remote_supplier,
)
from tests.ontologies.test_agent_runtime import modeled_ontology
from tests.ontologies.test_agent_unified_query import (  # noqa: F401 - fixtures
    _fo, _register_join_assertion, _single_source, orders_ontology,
    parties_ontology,
)

SESSION_KIND = "super_assistant"


def _conversation(db, owner) -> SuperAssistantConversation:
    conversation = SuperAssistantConversation(owner_id=owner.id, title="t2")
    db.add(conversation)
    db.commit()
    return conversation


def _local_request(orders, *, page_size: int):
    return assistant_facade.build_query_request(
        _single_source(orders, pageSize=page_size))


def _local_count_request(orders, *, page_size: int):
    return assistant_facade.build_query_request({
        "sources": [{"alias": "a", "ontologyId": orders["ontologyId"],
                     "releaseId": orders["releaseId"],
                     "objectTypeId": "ot-order"}],
        "select": [],
        "aggregates": [{"fn": "count", "as": "n"}],
        "pageSize": page_size,
    })


def _assert_released(data: dict) -> None:
    assert data["released"] is True
    released_at = data["releasedAt"]
    parsed = datetime.fromisoformat(released_at)
    assert parsed.utcoffset() == timedelta(0)
    assert data["expiresAt"] == released_at


def _assert_unreleased(data: dict) -> None:
    assert "released" not in data
    assert "releasedAt" not in data


def _snapshot(db, snapshot_id):
    return (db.query(AgentQuerySnapshot)
            .filter(AgentQuerySnapshot.id == snapshot_id).first())


def _rows(db, snapshot_id) -> int:
    return (db.query(AgentQueryRow)
            .filter(AgentQueryRow.snapshot_id == snapshot_id).count())


# ---------------------------------------------------------------- 终页释放


def test_local_single_page_terminal_releases_snapshot_and_rows(
        orders_ontology, admin_user, db):
    data = assistant_facade.query(
        db, admin_user, _local_request(orders_ontology, page_size=10),
        session_kind=SESSION_KIND, session_id="conv-terminal")
    assert data["hasNext"] is False
    assert data["pageSize"] == 10
    _assert_released(data)
    snapshot_id = data["queryId"]
    assert _snapshot(db, snapshot_id) is None
    assert _rows(db, snapshot_id) == 0


def test_local_multi_page_keeps_intermediate_then_releases_terminal(
        orders_ontology, admin_user, db):
    first = assistant_facade.query(
        db, admin_user, _local_request(orders_ontology, page_size=2),
        session_kind=SESSION_KIND, session_id="conv-pages")
    assert first["hasNext"] is True and first["cursor"]
    assert first["pageSize"] == 2
    _assert_unreleased(first)
    snapshot_id = first["queryId"]
    assert _snapshot(db, snapshot_id) is not None
    assert _rows(db, snapshot_id) == 3

    second = assistant_facade.query_page(
        db, admin_user, first["cursor"],
        session_kind=SESSION_KIND, session_id="conv-pages")
    assert second["hasNext"] is False
    assert second["queryId"] == snapshot_id
    assert second["pageSize"] == 2       # 续页页长冻结在快照上
    _assert_released(second)
    assert _snapshot(db, snapshot_id) is None
    assert _rows(db, snapshot_id) == 0

    # 旧 cursor 在终页释放后按既有语义拒绝（不复活已删除快照）。
    with pytest.raises(FacadeError) as error:
        assistant_facade.query_page(
            db, admin_user, first["cursor"],
            session_kind=SESSION_KIND, session_id="conv-pages")
    assert (error.value.status, error.value.code) == (
        404, "snapshot_not_found")


def test_has_next_true_or_missing_never_releases(
        orders_ontology, admin_user, db, monkeypatch):
    first = assistant_facade.query(
        db, admin_user, _local_request(orders_ontology, page_size=2),
        session_kind=SESSION_KIND, session_id="conv-guard")
    assert first["hasNext"] is True
    _assert_unreleased(first)
    snapshot_id = first["queryId"]
    assert _snapshot(db, snapshot_id) is not None

    # hasNext 缺失：只有严格 False 才允许释放。
    fake = {"queryId": snapshot_id, "hasNext": None,
            "expiresAt": first["expiresAt"]}
    monkeypatch.setattr(
        assistant_facade, "fetch_unified_query_page",
        lambda *args, **kwargs: dict(fake))
    page = assistant_facade.query_page(
        db, admin_user, first["cursor"],
        session_kind=SESSION_KIND, session_id="conv-guard")
    _assert_unreleased(page)
    assert _snapshot(db, snapshot_id) is not None


def test_release_failure_is_best_effort_and_keeps_original_expiry(
        orders_ontology, admin_user, db, monkeypatch, caplog):
    first = assistant_facade.query(
        db, admin_user, _local_request(orders_ontology, page_size=2),
        session_kind=SESSION_KIND, session_id="conv-fail")
    snapshot_id = first["queryId"]
    original_expiry = first["expiresAt"]

    def _boom(*args, **kwargs):
        raise RuntimeError("snapshot delete failed")

    monkeypatch.setattr(assistant_facade, "release_query_snapshot", _boom,
                        raising=False)
    monkeypatch.setattr(
        assistant_facade, "cancel_remote_query",
        lambda *args, **kwargs: pytest.fail(
            "本地快照释放异常时不得改走远程取消"), raising=False)
    with caplog.at_level(logging.WARNING):
        second = assistant_facade.query_page(
            db, admin_user, first["cursor"],
            session_kind=SESSION_KIND, session_id="conv-fail")

    assert second["hasNext"] is False
    assert second["pageSize"] == 2
    assert second["released"] is False
    assert "releasedAt" not in second
    assert second["expiresAt"] == original_expiry
    assert _snapshot(db, snapshot_id) is not None
    assert any("release" in record.getMessage().lower()
               for record in caplog.records)


def test_release_is_owner_and_session_scoped(
        orders_ontology, admin_user, editor_user, db):
    first = assistant_facade.query(
        db, admin_user, _local_request(orders_ontology, page_size=2),
        session_kind=SESSION_KIND, session_id="conv-scope")
    snapshot_id = first["queryId"]

    assert query_service.release_query_snapshot(
        db, editor_user, snapshot_id,
        session_kind=SESSION_KIND, session_id="conv-scope") is False
    assert query_service.release_query_snapshot(
        db, admin_user, snapshot_id,
        session_kind="direct", session_id="conv-scope") is False
    assert query_service.release_query_snapshot(
        db, admin_user, snapshot_id,
        session_kind=SESSION_KIND, session_id="other-conversation") is False
    assert _snapshot(db, snapshot_id) is not None

    assert query_service.release_query_snapshot(
        db, admin_user, snapshot_id,
        session_kind=SESSION_KIND, session_id="conv-scope") is True
    assert _snapshot(db, snapshot_id) is None
    assert _rows(db, snapshot_id) == 0
    # 幂等：再次释放返回 False，不抛错。
    assert query_service.release_query_snapshot(
        db, admin_user, snapshot_id,
        session_kind=SESSION_KIND, session_id="conv-scope") is False


def test_eleven_plus_local_single_page_queries_do_not_exhaust_quota(
        orders_ontology, admin_user, db):
    user_id = str(admin_user.id)
    for _ in range(12):
        data = assistant_facade.query(
            db, admin_user, _local_count_request(orders_ontology, page_size=1),
            session_kind=SESSION_KIND, session_id="conv-burst")
        assert data["hasNext"] is False
        assert data["pageSize"] == 1
        assert data["rows"] == [{"n": 3}]
        _assert_released(data)
    assert (db.query(AgentQuerySnapshot)
            .filter(AgentQuerySnapshot.owner_user_id == user_id).count()) == 0


def test_assistant_count_positive_and_zero_are_valid_results(
        orders_ontology, admin_user, db):
    """B8（t2 半边）：门面查询下正数与 0 都是有效成功结果，零值不重试。"""
    orders = orders_ontology
    for value, expected in (("paid", 2), ("no-such-value", 0)):
        request = assistant_facade.build_query_request({
            "sources": [{"alias": "a", "ontologyId": orders["ontologyId"],
                         "releaseId": orders["releaseId"],
                         "objectTypeId": "ot-order"}],
            "select": [],
            "aggregates": [{"fn": "count", "as": "n"}],
            "where": [{"source": "a", "property": "status", "op": "eq",
                       "value": value}],
            "pageSize": 10,
        })
        data = assistant_facade.query(
            db, admin_user, request,
            session_kind=SESSION_KIND, session_id="conv-count")
        assert data["rows"] == [{"n": expected}]
        assert data["hasNext"] is False and data["pageSize"] == 10
        assert data["truncated"] is False and data["partial"] is False
        _assert_released(data)


# ------------------------------------------------------- 远程 memory handle


class _RecordingRemotePort:
    """确定性远程端口：句柄按 owner 记账，配额与 data_channel 一致为 10。"""

    HANDLE_QUOTA = 10

    def __init__(self, *, first_page_has_more: bool = False):
        self.handles: dict[str, dict] = {}
        self.cancelled: list[tuple[str, str]] = []
        self.executed: list[dict] = []
        self.first_page_has_more = first_page_has_more
        self._seq = 0

    def execute(self, db, user, plan, *, request, session_kind, session_id):
        active = [meta for meta in self.handles.values()
                  if meta["owner"] == str(user.id)]
        if len(active) >= self.HANDLE_QUOTA:
            raise QueryError(
                429, "memory_quota_exceeded", "memory handle quota exhausted",
                quota="max_handles_per_user", retryableAfter=300)
        self._seq += 1
        query_id = f"mem-handle-{self._seq}"
        self.handles[query_id] = {
            "owner": str(user.id), "query_id": query_id, "plan": plan,
            "request": request}
        self.executed.append({
            "query_id": query_id, "plan": plan,
            "session_kind": session_kind, "session_id": session_id})
        result = _remote_result(query_id, has_more=self.first_page_has_more)
        return remote_response(result, plan)

    def fetch(self, db, user, token, *, session_kind, session_id, prepare):
        meta = next((item for item in self.handles.values()
                     if item["owner"] == str(user.id)), None)
        if meta is None:
            raise QueryError(404, "snapshot_not_found", "Query handle not found")
        # 与真实 adapter 一致：取页前用同一 canonical 校验重编 plan。
        _, plan = prepare(db, user, meta["request"], serialized=True)
        result = _remote_result(meta["query_id"], has_more=False)
        return remote_response(result, plan)

    def cancel(self, db, user, query_id):
        meta = self.handles.get(query_id)
        self.cancelled.append((str(user.id), query_id))
        if meta is None:
            return {"queryId": query_id, "cancelled": False}
        if meta["owner"] != str(user.id):
            raise QueryError(404, "snapshot_not_found", "Query handle not found")
        self.handles.pop(query_id)
        return {"queryId": query_id, "cancelled": True}

    def drop_without_cancel(self) -> None:
        self.handles.clear()


def _remote_result(query_id: str, *, has_more: bool) -> dict:
    return {
        "queryId": query_id,
        "columns": [{"name": "sname", "type": "string"}],
        "rows": [{"sname": "华南电子"}],
        "totalRows": 1,
        "page": {"hasMore": has_more,
                 "cursor": "mem.next-page" if has_more else None},
        "sourceStates": [{"asOf": "2026-11-02T00:00:00+00:00"}],
        "expiresAt": "2026-11-02T00:05:00+00:00",
        "consistency": "repeatable_read",
        "countsScope": "complete_snapshot",
        "plan": {},
    }


def _remote_identity(ontology_id: str, release_id: str) -> dict:
    return {"ontologyId": ontology_id, "releaseId": release_id,
            "objectTypeId": "ot-supplier"}


def _remote_request(identity: dict, *, page_size: int | None = None):
    arguments = {
        "sources": [{"alias": "objects", **identity}],
        "select": [{"source": "objects", "property": "sname", "as": "sname"}],
    }
    if page_size is not None:
        arguments["pageSize"] = page_size
    return assistant_facade.build_query_request(arguments)


def _remote_count_request(identity: dict, *, page_size: int | None = None):
    arguments = {
        "sources": [{"alias": "objects", **identity}],
        "select": [],
        "aggregates": [{"fn": "count", "as": "n"}],
    }
    if page_size is not None:
        arguments["pageSize"] = page_size
    return assistant_facade.build_query_request(arguments)


def _install_port(monkeypatch, port):
    monkeypatch.setattr(remote_query, "_port", port)
    return port


def test_remote_single_page_terminal_cancels_handle_and_syncs_reference(
        modeled_ontology, admin_user, db, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    port = _install_port(monkeypatch, _RecordingRemotePort())

    data = assistant_facade.query(
        db, admin_user, _remote_request(_remote_identity(oid, release_id)),
        session_kind=SESSION_KIND, session_id="conv-remote")
    assert data["hasNext"] is False
    assert data["pageSize"] == 50        # min(50, cap=50)
    _assert_released(data)
    assert port.cancelled == [(str(admin_user.id), data["queryId"])]
    assert port.handles == {}
    reference = data["persistence"]["reference"]
    assert set(reference) == {"queryId", "expiresAt"}
    assert reference["expiresAt"] == data["releasedAt"]


def test_remote_count_aggregate_terminal_cancels_handle(
        modeled_ontology, admin_user, db, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    port = _install_port(monkeypatch, _RecordingRemotePort())
    data = assistant_facade.query(
        db, admin_user,
        _remote_count_request(_remote_identity(oid, release_id), page_size=1),
        session_kind=SESSION_KIND, session_id="conv-remote-count")
    assert data["hasNext"] is False
    assert data["pageSize"] == 1
    _assert_released(data)
    assert port.cancelled == [(str(admin_user.id), data["queryId"])]
    assert port.handles == {}


def test_remote_multi_page_keeps_intermediate_then_cancels_terminal(
        modeled_ontology, admin_user, db, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    port = _install_port(
        monkeypatch, _RecordingRemotePort(first_page_has_more=True))

    first = assistant_facade.query(
        db, admin_user, _remote_request(_remote_identity(oid, release_id)),
        session_kind=SESSION_KIND, session_id="conv-remote-pages")
    assert first["hasNext"] is True
    assert first["pageSize"] == 50
    _assert_unreleased(first)
    assert list(port.handles) == [first["queryId"]]

    second = assistant_facade.query_page(
        db, admin_user, first["cursor"],
        session_kind=SESSION_KIND, session_id="conv-remote-pages")
    assert second["hasNext"] is False
    assert second["queryId"] == first["queryId"]
    assert second["pageSize"] == 50      # 端口复核 plan 的冻结页长
    _assert_released(second)
    assert port.handles == {}
    assert port.cancelled == [(str(admin_user.id), first["queryId"])]


def test_remote_cancel_failure_is_best_effort(
        modeled_ontology, admin_user, db, monkeypatch, caplog):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    port = _install_port(monkeypatch, _RecordingRemotePort())
    original_expiry = "2026-11-02T00:05:00+00:00"

    def _boom(*args, **kwargs):
        raise RuntimeError("cancel backend down")

    monkeypatch.setattr(port, "cancel", _boom)
    with caplog.at_level(logging.WARNING):
        data = assistant_facade.query(
            db, admin_user, _remote_request(_remote_identity(oid, release_id)),
            session_kind=SESSION_KIND, session_id="conv-remote-fail")

    assert data["hasNext"] is False
    assert data["released"] is False
    assert "releasedAt" not in data
    assert data["expiresAt"] == original_expiry
    assert data["persistence"]["reference"]["expiresAt"] == original_expiry
    assert any("release" in record.getMessage().lower()
               for record in caplog.records)


def test_remote_cancel_without_handle_still_returns_success(
        modeled_ontology, admin_user, db, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    port = _install_port(monkeypatch, _RecordingRemotePort())

    def _already_gone(*args, **kwargs):
        return {"cancelled": False}

    monkeypatch.setattr(port, "cancel", _already_gone)
    data = assistant_facade.query(
        db, admin_user, _remote_request(_remote_identity(oid, release_id)),
        session_kind=SESSION_KIND, session_id="conv-already-gone")

    assert data["hasNext"] is False
    # 已不存在不是失败：不抛错、不加 tombstone，审计按已释放处理。
    _assert_released(data)


def test_eleven_plus_remote_single_page_queries_do_not_exhaust_quota(
        modeled_ontology, admin_user, db, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    port = _install_port(monkeypatch, _RecordingRemotePort())
    request = _remote_count_request(
        _remote_identity(oid, release_id), page_size=1)

    for _ in range(12):
        data = assistant_facade.query(
            db, admin_user, request,
            session_kind=SESSION_KIND, session_id="conv-remote-burst")
        assert data["hasNext"] is False
        assert data["pageSize"] == 1
        _assert_released(data)
        assert port.handles == {}
    assert len(port.cancelled) == 12


# --------------------------------------------------- 直查路径行为保持不变


def test_direct_http_terminal_page_never_triggers_facade_release(
        client, auth_headers, orders_ontology, db):
    response = client.post(
        "/api/v2/formal/agent/query", headers=auth_headers,
        json=_single_source(orders_ontology, pageSize=10))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["hasNext"] is False
    assert "pageSize" not in data        # 公共直查响应零增量
    assert "released" not in data and "releasedAt" not in data
    assert _snapshot(db, data["queryId"]) is not None
    assert _rows(db, data["queryId"]) == 3


def test_direct_http_page_size_cap_detail_is_zero_increment(
        client, auth_headers, modeled_ontology, db):
    """直查（本地/远程）pageSize 越界 detail 恢复基线 {code,message}。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    response = client.post(
        "/api/v2/formal/agent/query", headers=auth_headers,
        json={"sources": [{"alias": "a", "ontologyId": oid,
                          "releaseId": release_id,
                          "objectTypeId": "ot-order"}],
              "select": [{"source": "a", "property": "order_no",
                          "as": "orderNo"}],
              "pageSize": 51})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert set(detail) == {"code", "message"}
    assert detail["code"] == "invalid_parameter"
    for key in ("field", "requestedPageSize", "maxPageSize", "defaultPageSize"):
        assert key not in detail
    # 远程直查同样不泄漏助手私有字段。
    remote = client.post(
        "/api/v2/formal/agent/query", headers=auth_headers,
        json={"sources": [{"alias": "objects",
                          **_remote_identity(oid, release_id)}],
              "select": [{"source": "objects", "property": "sname",
                          "as": "sname"}],
              "pageSize": 51})
    assert remote.status_code == 422, remote.text
    remote_detail = remote.json()["detail"]
    assert set(remote_detail) == {"code", "message"}
    assert remote_detail["code"] == "invalid_parameter"


def test_assistant_facade_keeps_remote_page_size_controlled_fields(
        modeled_ontology, admin_user, db):
    """助手门面仍提供受控字段（本地已另测；补远程门面可见性）。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    with pytest.raises(FacadeError) as error:
        assistant_facade.query(
            db, admin_user,
            _remote_request(_remote_identity(oid, release_id), page_size=51),
            session_kind=SESSION_KIND, session_id="conv-remote-cap")
    assert (error.value.status, error.value.code) == (
        422, "invalid_parameter")
    assert error.value.extra["field"] == "pageSize"
    assert error.value.extra["requestedPageSize"] == 51
    assert error.value.extra["maxPageSize"] == 50
    assert error.value.extra["defaultPageSize"] == 50


def test_direct_http_remote_response_is_zero_increment_and_keeps_handle(
        client, auth_headers, modeled_ontology, db, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    port = _install_port(monkeypatch, _RecordingRemotePort())
    identity = _remote_identity(oid, release_id)
    response = client.post(
        "/api/v2/formal/agent/query", headers=auth_headers,
        json={"sources": [{"alias": "objects", **identity}],
              "select": [{"source": "objects", "property": "sname",
                          "as": "sname"}]})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["hasNext"] is False
    assert "pageSize" not in data        # 公共直查 remote 响应零增量
    assert "released" not in data and "releasedAt" not in data
    assert list(port.handles) == [data["queryId"]]   # 直查不触发释放


# ----------------------------------------------------- queryLimits 可发现性


def test_structure_tool_exposes_query_limits_only_on_facade(
        client, auth_headers, orders_ontology, admin_user, db):
    oid = orders_ontology["ontologyId"]
    release_id = orders_ontology["releaseId"]
    conversation = _conversation(db, admin_user)

    def tool_structure() -> dict:
        return json.loads(ontology_tools.execute_ontology_tool(
            db, owner_id=admin_user.id, conversation_id=conversation.id,
            name="ontology_structure",
            arguments={"ontologyId": oid, "releaseId": release_id,
                       "objectTypeIds": ["ot-order"]}))

    assert tool_structure()["queryLimits"] == {
        "enabled": True, "maxPageSize": 50, "defaultPageSize": 50}

    response = client.put(
        f"{_fo(oid)}/agent/profile", headers=auth_headers,
        json={"maxRowsPerQuery": 200})
    assert response.status_code == 200, response.text
    assert tool_structure()["queryLimits"] == {
        "enabled": True, "maxPageSize": 200, "defaultPageSize": 50}

    response = client.put(
        f"{_fo(oid)}/agent/profile", headers=auth_headers,
        json={"maxRowsPerQuery": 0})
    assert response.status_code == 200, response.text
    assert tool_structure()["queryLimits"] == {
        "enabled": False, "maxPageSize": 0, "defaultPageSize": 0}

    # 公共直查 structure 的响应键集不变：不出现 queryLimits。
    response = client.get(
        f"{_fo(oid)}/agent/structure", headers=auth_headers,
        params={"release_id": release_id,
                "object_type_ids": ["ot-order"]})
    assert response.status_code == 200, response.text
    assert "queryLimits" not in response.json()["data"]


def test_facade_structure_query_limits_track_profile_cap(
        orders_ontology, admin_user, db):
    oid = orders_ontology["ontologyId"]
    limits = assistant_facade.structure(
        db, admin_user, oid, release_id=orders_ontology["releaseId"],
        object_type_ids=["ot-order"])["queryLimits"]
    assert limits == {"enabled": True, "maxPageSize": 50,
                      "defaultPageSize": 50}


# ------------------------------------------------- 受控 pageSize / 上限错误


def test_local_page_size_over_cap_reports_controlled_limits(
        orders_ontology, admin_user, db):
    request = _local_request(orders_ontology, page_size=51)
    with pytest.raises(FacadeError) as error:
        assistant_facade.query(
            db, admin_user, request,
            session_kind=SESSION_KIND, session_id="conv-cap")
    assert (error.value.status, error.value.code) == (
        422, "invalid_parameter")
    assert error.value.extra["field"] == "pageSize"
    assert error.value.extra["requestedPageSize"] == 51
    assert error.value.extra["maxPageSize"] == 50
    assert error.value.extra["defaultPageSize"] == 50
    assert db.query(AgentQuerySnapshot).count() == 0


def test_local_multi_source_cap_is_minimum_of_all_leaf_profiles(
        client, auth_headers, orders_ontology, parties_ontology,
        admin_user, db):
    orders, parties = orders_ontology, parties_ontology
    for identity, cap in ((orders, 200), (parties, 50)):
        response = client.put(
            f"{_fo(identity['ontologyId'])}/agent/profile",
            headers=auth_headers, json={"maxRowsPerQuery": cap})
        assert response.status_code == 200, response.text
    evidence = _register_join_assertion(client, auth_headers, orders, parties)
    payload = {
        "sources": [
            {"alias": "a", "ontologyId": orders["ontologyId"],
             "releaseId": orders["releaseId"], "objectTypeId": "ot-order"},
            {"alias": "b", "ontologyId": parties["ontologyId"],
             "releaseId": parties["releaseId"], "objectTypeId": "ot-party"},
        ],
        "joins": [{
            "left": "a", "right": "b", "type": "inner",
            "on": {"kind": "explicit_key", "leftProperty": "tenant_key",
                   "rightProperty": "party_key", "keyType": "string",
                   "cardinality": "N:1", "businessMeaning": "test",
                   "evidenceRefs": evidence}}],
        "select": [{"source": "a", "property": "order_no", "as": "orderNo"},
                   {"source": "b", "property": "label", "as": "partyLabel"}],
        "orderBy": [{"output": "orderNo"}],
    }
    request = assistant_facade.build_query_request({**payload, "pageSize": 51})
    with pytest.raises(FacadeError) as error:
        assistant_facade.query(
            db, admin_user, request,
            session_kind=SESSION_KIND, session_id="conv-multi")
    assert (error.value.status, error.value.code) == (
        422, "invalid_parameter")
    assert error.value.extra["field"] == "pageSize"
    assert error.value.extra["requestedPageSize"] == 51
    assert error.value.extra["maxPageSize"] == 50
    assert error.value.extra["defaultPageSize"] == 50

    accepted = assistant_facade.build_query_request(
        {**payload, "pageSize": 50})
    data = assistant_facade.query(
        db, admin_user, accepted,
        session_kind=SESSION_KIND, session_id="conv-multi")
    assert data["pageRowCount"] == 3
    assert data["hasNext"] is False
    _assert_released(data)


def test_local_default_page_size_uses_min_of_50_and_cap(
        client, auth_headers, orders_ontology, admin_user, db):
    """未显式给 pageSize 时实际执行页长 = min(50, 有效 cap)。"""
    oid = orders_ontology["ontologyId"]
    response = client.put(
        f"{_fo(oid)}/agent/profile", headers=auth_headers,
        json={"maxRowsPerQuery": 2})
    assert response.status_code == 200, response.text
    request = assistant_facade.build_query_request({
        "sources": [{"alias": "a", "ontologyId": oid,
                     "releaseId": orders_ontology["releaseId"],
                     "objectTypeId": "ot-order"}],
        "select": [{"source": "a", "property": "order_no", "as": "orderNo"}],
        "orderBy": [{"output": "orderNo"}],
    })
    assert request.page_size is None
    data = assistant_facade.query(
        db, admin_user, request,
        session_kind=SESSION_KIND, session_id="conv-default")
    assert data["pageRowCount"] == 2
    assert data["pageSize"] == 2         # min(50, cap=2)
    assert data["totalRowCount"] == 3
    assert data["hasNext"] is True
    _assert_unreleased(data)
    assert _snapshot(db, data["queryId"]) is not None


def test_profile_cap_200_allows_page_size_200(
        client, auth_headers, orders_ontology, admin_user, db):
    oid = orders_ontology["ontologyId"]
    response = client.put(
        f"{_fo(oid)}/agent/profile", headers=auth_headers,
        json={"maxRowsPerQuery": 200})
    assert response.status_code == 200, response.text
    request = _local_request(orders_ontology, page_size=200)
    data = assistant_facade.query(
        db, admin_user, request,
        session_kind=SESSION_KIND, session_id="conv-max")
    assert data["pageSize"] == 200
    assert data["pageRowCount"] == 3
    assert data["hasNext"] is False
    _assert_released(data)


@pytest.mark.parametrize("bad", [0, 201, -1, "201", "abc", True, 1.5])
def test_build_query_request_rejects_schema_level_page_sizes(bad):
    payload = {
        "sources": [{"alias": "a", "ontologyId": "o", "releaseId": "r",
                     "objectTypeId": "t"}],
        "select": [{"source": "a", "property": "p", "as": "p"}],
        "pageSize": bad,
    }
    with pytest.raises(FacadeError) as error:
        assistant_facade.build_query_request(payload)
    assert (error.value.status, error.value.code) == (
        422, "invalid_parameter")
    assert error.value.extra["field"] == "pageSize"
    assert error.value.extra["maxPageSize"] == 200
    # schema 级错误不携带输入值（受控键只有 field/maxPageSize）。
    assert set(error.value.extra) == {"field", "maxPageSize"}
    rendered = json.dumps(error.value.detail(), ensure_ascii=False)
    assert "pageSize" in rendered
    if isinstance(bad, str):
        assert bad not in rendered


def test_build_query_request_rejects_cursor_mixed_arguments():
    cursor = "mem.abcdefghijklmnop"
    mixed = [
        ({"pageSize": 5}, "pageSize"),
        ({"params": {"secret": "value-secret"}}, "params"),
        ({"semanticBindings": {"s": {"ontologyId": "o", "releaseId": "r",
                                     "objectTypeId": "t"}}}, "semanticBindings"),
        ({"queryId": "query-id-secret"}, "queryId"),
    ]
    for extra, field in mixed:
        with pytest.raises(FacadeError) as error:
            assistant_facade.build_query_request({"cursor": cursor, **extra})
        assert (error.value.status, error.value.code) == (
            422, "invalid_parameter")
        assert error.value.extra["field"] == field
        rendered = json.dumps(error.value.detail(), ensure_ascii=False)
        assert "secret" not in rendered

    # cursor 与结构树混形沿用既有 invalid_request_shape（与 spl 混形同口径）。
    with pytest.raises(FacadeError) as error:
        assistant_facade.build_query_request({
            "cursor": cursor,
            "sources": [{"alias": "a", "ontologyId": "o", "releaseId": "r",
                         "objectTypeId": "t"}],
        })
    assert (error.value.status, error.value.code) == (
        422, "invalid_request_shape")


def test_current_release_map_is_metadata_comparison_not_authorization(
        orders_ontology, db):
    mapping = assistant_facade.current_release_map(
        db, [orders_ontology["ontologyId"], "missing-ontology"])
    assert mapping == {orders_ontology["ontologyId"]:
                       orders_ontology["releaseId"]}
    assert "不构成授权" in (assistant_facade.current_release_map.__doc__ or "")


# ------------------------------------------- 工具入口：cursor 混用必须 422

_TOOL_CURSOR = "mem.cursor-token-123456"


def _tool_query(db, user, conversation, arguments, monkeypatch):
    """经真实 execute_ontology_tool 分派；记录门面 query/query_page 调用。"""
    calls = {"query": [], "query_page": []}
    monkeypatch.setattr(
        ontology_tools, "query",
        lambda *args, **kwargs: calls["query"].append((args, kwargs))
        or {"queryId": "should-not-run"})
    monkeypatch.setattr(
        ontology_tools, "query_page",
        lambda *args, **kwargs: calls["query_page"].append((args, kwargs))
        or {"queryId": "should-not-run"})
    output = ontology_tools.execute_ontology_tool(
        db, owner_id=user.id, conversation_id=conversation.id,
        name="ontology_query", arguments=arguments)
    return json.loads(output), calls


@pytest.mark.parametrize("mixed,expected_code", [
    ({"pageSize": 987}, "invalid_parameter"),
    ({"pageSize": 0}, "invalid_parameter"),
    ({"pageSize": ""}, "invalid_parameter"),
    ({"params": {"region": "secret-param-value"}}, "invalid_parameter"),
    ({"semanticBindings": {"s": {
        "ontologyId": "secret-ontology-id", "releaseId": "secret-release-id",
        "objectTypeId": "secret-type-id"}}}, "invalid_parameter"),
    ({"queryId": "secret-query-id"}, "invalid_parameter"),
    ({"sources": [{"alias": "secret_alias", "ontologyId": "secret-ontology",
                   "releaseId": "secret-release", "objectTypeId": "secret-type"}]},
     "invalid_request_shape"),
    ({"spl": "from secret_source as s | fields s.id as id",
      "semanticBindings": {"secret_source": {
          "ontologyId": "secret-ontology", "releaseId": "secret-release",
          "objectTypeId": "secret-type"}}},
     "invalid_request_shape"),
])
def test_tool_entry_rejects_cursor_mixed_arguments_before_query_page(
        orders_ontology, admin_user, db, monkeypatch, mixed, expected_code):
    """T5-F1：工具入口不得在 cursor 分支静默丢弃混用参数。"""
    conversation = _conversation(db, admin_user)
    payload, calls = _tool_query(
        db, admin_user, conversation, {"cursor": _TOOL_CURSOR, **mixed},
        monkeypatch)

    # 关键契约：校验发生在 query_page 之前，两条执行路径都不得被调用。
    assert calls == {"query": [], "query_page": []}
    assert payload.get("code") == expected_code, payload
    assert payload.get("error"), payload
    rendered = json.dumps(payload, ensure_ascii=False)
    assert _TOOL_CURSOR not in rendered
    for secret in ("987", "secret-param-value", "secret-ontology-id",
                   "secret-release-id", "secret-type-id", "secret-query-id",
                   "secret_alias", "secret_source"):
        assert secret not in rendered, (secret, rendered)


def test_tool_entry_cursor_with_null_page_size_uses_builder_absent_rule(
        orders_ontology, admin_user, db, monkeypatch):
    """显式 null 由 builder 的“未提供”规则处理（不静默夹值、不绕校验）。

    pageSize=null 不能作为混用参数被吞掉：它经 build_query_request 后按
    缺省 pageSize 的 cursor-only 续查分派；零/空串已在混用参数用例中拒绝。
    """
    conversation = _conversation(db, admin_user)
    payload, calls = _tool_query(
        db, admin_user, conversation,
        {"cursor": _TOOL_CURSOR, "pageSize": None}, monkeypatch)
    assert payload == {"queryId": "should-not-run"}
    assert len(calls["query_page"]) == 1 and calls["query"] == []


@pytest.mark.parametrize("bad_page_size", [0, 201, "201", "", "abc", True, 1.5])
def test_tool_entry_rejects_bad_page_size_before_dispatch(
        orders_ontology, admin_user, db, monkeypatch, bad_page_size):
    """新查询的 0/201/空串/非法类型在工具入口即 422，不触达 query。"""
    conversation = _conversation(db, admin_user)
    payload, calls = _tool_query(
        db, admin_user, conversation,
        {"sources": [{"alias": "a",
                      "ontologyId": orders_ontology["ontologyId"],
                      "releaseId": orders_ontology["releaseId"],
                      "objectTypeId": "ot-order"}],
         "select": [{"source": "a", "property": "order_no", "as": "orderNo"}],
         "pageSize": bad_page_size},
        monkeypatch)
    assert calls == {"query": [], "query_page": []}
    assert payload.get("code") == "invalid_parameter", payload
    assert payload.get("field") == "pageSize"
    assert payload.get("maxPageSize") == 200
    assert "requestedPageSize" not in payload       # schema 级不回显请求值
    if isinstance(bad_page_size, str) and bad_page_size:
        assert bad_page_size not in json.dumps(payload, ensure_ascii=False)


def test_tool_entry_null_page_size_is_optional_absence(
        orders_ontology, admin_user, db, monkeypatch):
    """显式 null 等同未提供（builder 既有规则），不误判为非法类型。"""
    conversation = _conversation(db, admin_user)
    payload, calls = _tool_query(
        db, admin_user, conversation,
        {"sources": [{"alias": "a",
                      "ontologyId": orders_ontology["ontologyId"],
                      "releaseId": orders_ontology["releaseId"],
                      "objectTypeId": "ot-order"}],
         "select": [{"source": "a", "property": "order_no", "as": "orderNo"}],
         "pageSize": None},
        monkeypatch)
    assert payload == {"queryId": "should-not-run"}
    assert len(calls["query"]) == 1 and calls["query_page"] == []
    request = calls["query"][0][0][2]
    assert request.page_size is None


def test_tool_entry_routes_plain_cursor_to_query_page(
        orders_ontology, admin_user, db, monkeypatch):
    """有效续查仍然只按 cursor 调 query_page（不回归）。"""
    conversation = _conversation(db, admin_user)
    payload, calls = _tool_query(
        db, admin_user, conversation, {"cursor": _TOOL_CURSOR}, monkeypatch)
    assert payload == {"queryId": "should-not-run"}
    assert len(calls["query_page"]) == 1 and calls["query"] == []
    args, kwargs = calls["query_page"][0]
    assert args[2] == _TOOL_CURSOR
    assert kwargs == {"session_kind": SESSION_KIND,
                      "session_id": conversation.id}


# ----------------------------------- queryLimits 负值与 0 的执行语义一致


def test_query_limits_normalizes_negative_profile_cap(
        client, auth_headers, orders_ontology, admin_user, db):
    """T5-F2：负 maxRowsPerQuery 与 0 一致归一为停用，不返回负 cap。"""
    oid = orders_ontology["ontologyId"]
    release_id = orders_ontology["releaseId"]
    expected = {"enabled": False, "maxPageSize": 0, "defaultPageSize": 0}
    for cap in (-5, 0):
        response = client.put(
            f"{_fo(oid)}/agent/profile", headers=auth_headers,
            json={"maxRowsPerQuery": cap})
        assert response.status_code == 200, response.text
        limits = assistant_facade.structure(
            db, admin_user, oid, release_id=release_id,
            object_type_ids=["ot-order"])["queryLimits"]
        assert limits == expected, (cap, limits)

        # 与执行语义一致：<=0 均按既有 403 profile_query_disabled 拒绝。
        request = _local_request(orders_ontology, page_size=1)
        with pytest.raises(FacadeError) as error:
            assistant_facade.query(
                db, admin_user, request,
                session_kind=SESSION_KIND, session_id="conv-negative")
        assert (error.value.status, error.value.code) == (
            403, "profile_query_disabled")


# --------------------------------- 工具入口：畸形 cursor 必须受控可纠正

_MALFORMED_CURSORS = [
    "x",
    "mem.short",
    "",
    "mem." + "a" * 2050,               # 超过 CursorRequest max_length=2048
    12345678901234567890,              # 非字符串，且 str() 后长度合法
    True,
    ["mem.abcdefghijklmnop"],
    {"value": "mem.abcdefghijklmnop"},
]


@pytest.mark.parametrize("cursor", _MALFORMED_CURSORS)
def test_build_query_request_rejects_malformed_cursor_controlled(cursor):
    """T15-F1：cursor 模型校验必须受控为 invalid_cursor，不抛裸 ValidationError。"""
    with pytest.raises(FacadeError) as error:
        assistant_facade.build_query_request({"cursor": cursor})
    assert (error.value.status, error.value.code) == (400, "invalid_cursor")
    assert error.value.extra == {}
    rendered = json.dumps(error.value.detail(), ensure_ascii=False)
    assert "cursor 格式非法" in rendered
    if isinstance(cursor, str) and cursor:
        assert cursor not in rendered
    assert "长度" not in rendered and "2048" not in rendered


@pytest.mark.parametrize("cursor", _MALFORMED_CURSORS)
def test_tool_entry_rejects_malformed_cursor_without_dispatch(
        orders_ontology, admin_user, db, monkeypatch, cursor):
    """T15-F1：工具入口返回受控 invalid_cursor，不触达 query/query_page。"""
    conversation = _conversation(db, admin_user)
    payload, calls = _tool_query(
        db, admin_user, conversation, {"cursor": cursor}, monkeypatch)

    assert calls == {"query": [], "query_page": []}
    assert payload.get("code") == "invalid_cursor", payload
    assert payload.get("error"), payload
    assert set(payload) == {"error", "code"}       # 不回显 cursor/长度/原值
    rendered = json.dumps(payload, ensure_ascii=False)
    if isinstance(cursor, str) and cursor:
        assert cursor not in rendered
    assert "长度" not in rendered and "2048" not in rendered
