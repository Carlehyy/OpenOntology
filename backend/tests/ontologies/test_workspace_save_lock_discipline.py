"""草稿保存写阶段锁纪律回归（D-015）。

保存链路改为「无锁读校验 → 短锁 CAS 写」后，必须锁定三件事：
1. 读相位与写相位之间发生并发漂移时，revision:snapshot_hash CAS 复核
   即刻 409，不允许基于过期基线静默覆盖；
2. PostgreSQL 锁等待超时（55P03/57014/40P01）映射为可重试的 409 契约，
   不再无限挂起；无关 DBAPI 错误原样透传；
3. 正常保存行为（revision 推进、幂等回写）不受重构影响。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.ontologies.versions import workspace_service as ws


def _draft(client, headers, ontology_id: str, source_id: str) -> dict:
    response = client.post(
        f"/api/v2/ontologies/{ontology_id}/versions/{source_id}/drafts",
        headers=headers, json={"versionLabel": "锁纪律验证"},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _root_release_id(client, headers, ontology_id: str) -> str:
    detail = client.get(f"/api/v1/ontologies/{ontology_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    return detail.json()["data"]["current_release_id"]


def _workspace(draft: dict) -> dict:
    return {
        "version": draft["version_number"],
        "baseRevision": f"{draft['revision']}:{draft['snapshot_hash']}",
        "objectTypes": [{
            "id": "ot-order", "name": "Order", "displayName": "订单",
            "primaryKey": "p-id", "positionX": 10, "positionY": 20,
            "properties": [
                {"id": "p-id", "name": "id", "displayName": "订单号",
                 "type": "string", "required": True},
                {"id": "p-name", "name": "name", "displayName": "名称",
                 "type": "string", "required": True},
            ],
        }],
        "linkTypes": [], "actions": [], "functions": [],
        "instances": [], "linkInstances": [],
    }


def _bump_revision(db, version_id: str) -> None:
    db.execute(text(
        "UPDATE ontology_versions SET revision = revision + 1 "
        "WHERE id = :id"), {"id": version_id})
    db.commit()


def test_workspace_save_cas_recheck_rejects_read_phase_drift(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    draft = _draft(client, auth_headers, oid, _root_release_id(client, auth_headers, oid))

    original = ws._dynamic_sentinel_id_conflict_errors

    def drifting_hook(db_arg, ontology_id, sentinels):
        # 读相位中模拟并发写者提交：revision 已推进，而请求仍持有旧基线
        _bump_revision(db, draft["id"])
        return original(db_arg, ontology_id, sentinels)

    monkeypatch.setattr(ws, "_dynamic_sentinel_id_conflict_errors", drifting_hook)
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers, json=_workspace(draft),
    )
    assert saved.status_code == 409, saved.text
    detail = saved.json()["detail"]
    assert detail["code"] == "conflict"
    assert detail["currentRevision"].startswith(f"{draft['revision'] + 1}:")


def test_mappings_save_cas_recheck_rejects_read_phase_drift(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    draft = _draft(client, auth_headers, oid, _root_release_id(client, auth_headers, oid))

    def drifting_hook(body):
        _bump_revision(db, draft["id"])

    monkeypatch.setattr(ws, "_validate_workspace_mapping_policy_types", drifting_hook)
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace/mappings",
        headers=auth_headers,
        json={"baseRevision": f"{draft['revision']}:{draft['snapshot_hash']}",
              "mappings": [], "linkMappings": []},
    )
    assert saved.status_code == 409, saved.text
    detail = saved.json()["detail"]
    assert detail["code"] == "conflict"
    assert detail["currentRevision"].startswith(f"{draft['revision'] + 1}:")


def test_workspace_save_advances_revision_without_drift(
        client, auth_headers, ontology):
    oid = ontology["id"]
    draft = _draft(client, auth_headers, oid, _root_release_id(client, auth_headers, oid))
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers, json=_workspace(draft),
    )
    assert saved.status_code == 200, saved.text
    data = saved.json()["data"]
    assert data["revision"].startswith(f"{draft['revision'] + 1}:")
    assert data["snapshotHash"]


class _StubSession:
    def __init__(self, dialect_name: str = "sqlite"):
        self.dialect = SimpleNamespace(name=dialect_name)
        self.executed: list[str] = []
        self.rolled_back = False

    def get_bind(self):
        return SimpleNamespace(dialect=self.dialect)

    def execute(self, statement):
        self.executed.append(str(statement))

    def rollback(self):
        self.rolled_back = True


def _dbapi_error(pgcode: str | None) -> DBAPIError:
    return DBAPIError("SELECT 1", {}, SimpleNamespace(pgcode=pgcode))


def test_bounded_row_locks_maps_pg_lock_wait_to_409():
    stub = _StubSession()
    with pytest.raises(HTTPException) as excinfo:
        with ws._bounded_row_locks(stub):
            raise _dbapi_error("55P03")
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "write_lock_timeout"
    assert stub.rolled_back


def test_bounded_row_locks_reraises_unrelated_dbapi_error():
    stub = _StubSession()
    with pytest.raises(DBAPIError):
        with ws._bounded_row_locks(stub):
            raise _dbapi_error("23505")
    assert not stub.rolled_back


def test_bound_row_lock_wait_sets_pg_local_only():
    stub = _StubSession(dialect_name="postgresql")
    ws._bound_row_lock_wait(stub)  # type: ignore[arg-type]
    assert any("lock_timeout" in sql for sql in stub.executed)

    sqlite_stub = _StubSession(dialect_name="sqlite")
    ws._bound_row_lock_wait(sqlite_stub)  # type: ignore[arg-type]
    assert sqlite_stub.executed == []
