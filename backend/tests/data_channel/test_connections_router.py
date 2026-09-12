"""Connections CRUD HTTP 级测试：响应契约 + 凭据加密落库不变式。

该 router 自带局部 get_db（不经 app.deps.get_db），需单独 override；
此前 8 个端点零 HTTP 覆盖，加密落库/回显路径改坏不会有任何测试报警。
"""
import json

from app.data_channel.connections import router as connections_router
from app.main import app
from app.models.v2.connection import Connection
from app.services import encryption_service

CONFIG = {"host": "db.local", "port": 5432, "user": "svc", "password": "plain-secret-123"}


def _route_db_to(db):
    def override():
        yield db

    app.dependency_overrides[connections_router.get_db] = override


def _create(client, auth_headers, name="生产库"):
    created = client.post(
        "/api/v2/connections",
        headers=auth_headers,
        json={"name": name, "kind": "postgres", "config": CONFIG},
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_connection_crud_never_exposes_config_and_encrypts_at_rest(
    client, db, auth_headers,
):
    _route_db_to(db)
    try:
        body = _create(client, auth_headers)
        assert set(body) == {"id", "name", "kind", "status"}
        assert body["status"] == "inactive"

        # 落库只有密文：明文口令既不出现，也解得回原文（可逆加密、非脱敏）
        row = db.query(Connection).filter(Connection.id == body["id"]).one()
        assert set(row.config) == {"_encrypted"}
        assert CONFIG["password"] not in row.config["_encrypted"]
        assert json.loads(
            encryption_service.decrypt(row.config["_encrypted"])
        ) == CONFIG

        listed = client.get("/api/v2/connections", headers=auth_headers)
        assert listed.status_code == 200
        assert all("config" not in item for item in listed.json())

        single = client.get(f"/api/v2/connections/{body['id']}", headers=auth_headers)
        assert single.status_code == 200
        assert "config" not in single.json()

        deleted = client.delete(f"/api/v2/connections/{body['id']}", headers=auth_headers)
        assert deleted.status_code == 204
        assert client.get(
            f"/api/v2/connections/{body['id']}", headers=auth_headers,
        ).status_code == 404
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)


def test_connection_schedule_rejects_invalid_cron_and_unknown_connection(
    client, db, auth_headers,
):
    _route_db_to(db)
    try:
        body = _create(client, auth_headers)
        bad = client.post(
            f"/api/v2/connections/{body['id']}/schedule?cron_expr=not-a-cron",
            headers=auth_headers,
        )
        assert bad.status_code == 400

        missing = client.post(
            "/api/v2/connections/no-such-connection/schedule?cron_expr=* * * * *",
            headers=auth_headers,
        )
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)


def test_connection_test_config_and_test_report_contract_not_500(
    client, db, auth_headers,
):
    # 无外部基础设施的测试环境：连接测试必须优雅返回 {"success": bool}，而非 500
    _route_db_to(db)
    try:
        probe = client.post(
            "/api/v2/connections/test-config",
            headers=auth_headers,
            json={"type": "file", "config": {"prefix": "uploads/"}},
        )
        assert probe.status_code == 200
        assert isinstance(probe.json()["success"], bool)

        body = _create(client, auth_headers)
        tested = client.post(
            f"/api/v2/connections/{body['id']}/test", headers=auth_headers)
        assert tested.status_code == 200
        assert isinstance(tested.json()["success"], bool)

        missing = client.post(
            "/api/v2/connections/no-such-connection/test", headers=auth_headers)
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)


def test_connection_async_sync_fails_closed_without_dispatchable_task(
    client, db, auth_headers, monkeypatch,
):
    # async_mode=True 且任务不可派发时必须 503 fail-closed，绝不退化为进程内执行
    import app.tasks.v2.connection_sync as sync_module

    monkeypatch.setattr(sync_module, "sync_connection", object())
    _route_db_to(db)
    try:
        body = _create(client, auth_headers)
        response = client.post(
            f"/api/v2/connections/{body['id']}/sync",
            headers=auth_headers,
            json={"async_mode": True},
        )
        assert response.status_code == 503
        assert "未投递" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)


def test_connection_delete_blocked_by_dependent_datasets_returns_409(
    client, db, auth_headers,
):
    # 有同步数据集引用（source_connection_id 外键无级联）时删除必须 409 +
    # 依赖明细，不能 500（商业化审查 D-006）；解除依赖后删除恢复 204
    from app.data_channel.datasets.models import Dataset

    _route_db_to(db)
    try:
        body = _create(client, auth_headers)
        dependent = Dataset(
            id="ds-delete-blocked", name="同步数据集-验收", kind="structured",
            source_connection_id=body["id"], source_resource="users",
        )
        db.add(dependent)
        db.commit()

        blocked = client.delete(
            f"/api/v2/connections/{body['id']}", headers=auth_headers)
        assert blocked.status_code == 409
        detail = blocked.json()["detail"]
        assert detail["total"] == 1
        assert detail["datasets"][0]["name"] == "同步数据集-验收"
        assert "同步数据集" in detail["message"]
        # 连接未被误删
        assert client.get(
            f"/api/v2/connections/{body['id']}", headers=auth_headers,
        ).status_code == 200

        db.delete(dependent)
        db.commit()
        ok = client.delete(
            f"/api/v2/connections/{body['id']}", headers=auth_headers)
        assert ok.status_code == 204
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)


def test_connection_create_rejects_duplicate_name_and_blank_name(
    client, db, auth_headers,
):
    # 同名连接会让依赖连接名的数据集/任务无法区分（商业化审查 D-005）
    _route_db_to(db)
    try:
        _create(client, auth_headers, name="唯一连接")
        duplicate = client.post(
            "/api/v2/connections", headers=auth_headers,
            json={"name": "唯一连接", "kind": "postgres", "config": CONFIG},
        )
        assert duplicate.status_code == 409
        assert "同名连接" in duplicate.json()["detail"]

        blank = client.post(
            "/api/v2/connections", headers=auth_headers,
            json={"name": "   ", "kind": "postgres", "config": CONFIG},
        )
        assert blank.status_code == 400
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)
