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


def test_connection_schedule_retired_returns_410(client, db, auth_headers):
    # 端点已退役：无论连接是否存在、表达式是否合法，一律 410 指引数据任务池
    _route_db_to(db)
    try:
        body = _create(client, auth_headers)
        retired = client.post(
            f"/api/v2/connections/{body['id']}/schedule?cron_expr=not-a-cron",
            headers=auth_headers,
        )
        assert retired.status_code == 410
        assert "数据任务池" in retired.json()["detail"]

        missing = client.post(
            "/api/v2/connections/no-such-connection/schedule?cron_expr=* * * * *",
            headers=auth_headers,
        )
        assert missing.status_code == 410
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


def test_rest_connection_create_validates_config_server_side(
        client, db, auth_headers):
    """D-003 遗留加固：直连 API 持久化坏 REST config 被服务端 400 拦截。"""
    _route_db_to(db)
    # 坏 headers（非法 JSON 字符串）→ 400，不落库
    bad = client.post(
        "/api/v2/connections",
        headers=auth_headers,
        json={"name": "坏请求头连接", "kind": "rest",
              "config": {"url": "https://api.example.com/v1", "headers": "{not-json"}},
    )
    assert bad.status_code == 400, bad.text
    assert "请求头" in bad.json()["detail"]
    assert db.query(Connection).filter(
        Connection.name == "坏请求头连接").count() == 0

    # 非 http(s) URL → 400
    bad_url = client.post(
        "/api/v2/connections",
        headers=auth_headers,
        json={"name": "坏地址连接", "kind": "rest",
              "config": {"url": "ftp://example.com/data"}},
    )
    assert bad_url.status_code == 400, bad_url.text

    # 正常 rest config → 201（页面单 URL 形态也应通过归一化）
    good = client.post(
        "/api/v2/connections",
        headers=auth_headers,
        json={"name": "合法REST连接", "kind": "rest",
              "config": {"url": "https://api.example.com/v1/users",
                         "headers": "{\"X-Token\": \"abc\"}"}},
    )
    assert good.status_code == 201, good.text


# ── 凭据轮换（连接维护）──────────────────────────────────────────────

def test_connection_config_rotation_merges_partial_keys_and_never_echoes(
        client, db, auth_headers):
    """轮换只提交出现的键：服务端合并、加密落库、状态重置；响应/查询永不回填 config。"""
    _route_db_to(db)
    try:
        body = _create(client, auth_headers)
        rotated = client.patch(
            f"/api/v2/connections/{body['id']}/config",
            headers=auth_headers,
            json={"config": {"password": "rotated-secret-456"}},
        )
        assert rotated.status_code == 200, rotated.text
        assert set(rotated.json()) == {"id", "name", "kind", "status"}
        # 配置已变，连通结论作废
        assert rotated.json()["status"] == "inactive"
        assert "rotated-secret-456" not in rotated.text
        assert CONFIG["password"] not in rotated.text

        # 落库仍是密文，解密后为部分键合并结果（旧 host 保留 + 新密码）
        row = db.query(Connection).filter(Connection.id == body["id"]).one()
        assert set(row.config) == {"_encrypted"}
        merged = json.loads(encryption_service.decrypt(row.config["_encrypted"]))
        assert merged == {**CONFIG, "password": "rotated-secret-456"}

        # 任何读路径都不回填配置
        single = client.get(f"/api/v2/connections/{body['id']}", headers=auth_headers)
        assert "config" not in single.json()
        listed = client.get("/api/v2/connections", headers=auth_headers)
        assert all("config" not in item for item in listed.json())
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)


def test_connection_config_rotation_rejects_empty_config_and_unknown_id(
        client, db, auth_headers):
    _route_db_to(db)
    try:
        body = _create(client, auth_headers)
        empty = client.patch(
            f"/api/v2/connections/{body['id']}/config",
            headers=auth_headers, json={"config": {}},
        )
        assert empty.status_code == 400
        assert "至少填写一个" in empty.json()["detail"]

        missing = client.patch(
            "/api/v2/connections/no-such-connection/config",
            headers=auth_headers, json={"config": {"password": "x"}},
        )
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)


def test_connection_rotation_rest_config_validated_and_unchanged_on_reject(
        client, db, auth_headers):
    _route_db_to(db)
    try:
        created = client.post(
            "/api/v2/connections",
            headers=auth_headers,
            json={"name": "REST轮换连接", "kind": "rest",
                  "config": {"url": "https://api.example.com/v1/users"}},
        )
        assert created.status_code == 201, created.text
        connection_id = created.json()["id"]

        bad = client.patch(
            f"/api/v2/connections/{connection_id}/config",
            headers=auth_headers,
            json={"config": {"headers": "{not-json"}},
        )
        assert bad.status_code == 400, bad.text
        row = db.query(Connection).filter(Connection.id == connection_id).one()
        assert json.loads(encryption_service.decrypt(row.config["_encrypted"])) == {
            "url": "https://api.example.com/v1/users"}
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)


# ── 受控注销：结构化 409 明细 ────────────────────────────────────────

def _admin_id(db):
    from app.models.user import User
    return db.query(User).filter(User.username == "admin").one().id


def test_connection_delete_blocked_by_integrated_asset_returns_structured_409(
        client, db, auth_headers):
    """集成资产（connection FK RESTRICT）必须先经资产受控注销，不能 500。"""
    from app.data_channel.datasets.models import Dataset
    from app.data_channel.integrated.models import IntegratedAsset

    _route_db_to(db)
    try:
        body = _create(client, auth_headers)
        dataset = Dataset(id="ds-integrated-blocked", name="集成数据集-验收",
                          kind="integrated", source_connection_id=body["id"],
                          source_resource="public.rows")
        asset = IntegratedAsset(
            id="asset-blocked", dataset_id=dataset.id, connection_id=body["id"],
            name="集成资产-验收", schema_name="public", table_name="rows",
            schema_json={"columns": [], "primary_key": ["id"]},
            schema_fingerprint="fingerprint", writable=False, created_by=_admin_id(db))
        db.add_all([dataset, asset])
        db.commit()

        blocked = client.delete(
            f"/api/v2/connections/{body['id']}", headers=auth_headers)
        assert blocked.status_code == 409
        detail = blocked.json()["detail"]
        assert detail["datasets_total"] == 1
        assert detail["integrated_assets_total"] == 1
        assert detail["integrated_assets"][0]["name"] == "集成资产-验收"
        assert detail["history"] == {"dataset_versions": 0, "sync_runs": 0, "mutations": 0}
        assert detail["total"] == 1  # D-006 兼容语义：同步数据集数
        assert "集成数据集资产" in detail["message"]
        # 拒绝后资产与数据集原样保留
        assert db.get(IntegratedAsset, "asset-blocked") is not None
        assert db.get(Dataset, "ds-integrated-blocked") is not None
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)


def test_connection_delete_blocked_by_history_mapping_and_tasks_keeps_history(
        client, db, auth_headers):
    """零历史/零映射消费引用/零任务才可注销；409 后历史与审计一律保留。"""
    from app.data_channel.datasets.models import Dataset, DatasetVersion
    from app.data_channel.pipeline_tasks.models import PipelineTask
    from app.data_channel.pipelines.models import Pipeline
    from app.data_channel.integrated.models import IntegratedAsset, IntegratedMutation
    from app.ontologies.mappings.models import OntologyMapping
    from app.ontologies.projects.models import OntologyProject

    _route_db_to(db)
    try:
        body = _create(client, auth_headers)
        dataset = Dataset(id="ds-history-blocked", name="同步数据集-历史",
                          kind="structured", source_connection_id=body["id"],
                          source_resource="users")
        version = DatasetVersion(id="version-blocked", dataset_id=dataset.id, version_no=1)
        asset = IntegratedAsset(
            id="asset-history", dataset_id=dataset.id, connection_id=body["id"],
            name="集成资产-历史", schema_name="public", table_name="users",
            schema_json={"columns": [], "primary_key": []},
            schema_fingerprint="fingerprint", writable=False, created_by=_admin_id(db))
        mutation = IntegratedMutation(
            id="mutation-history", asset_id=asset.id, actor_id=_admin_id(db),
            idempotency_key="idem-1", operation="insert", request_hash="h",
            key_hash="k", schema_fingerprint="fingerprint", status="failed")
        project = OntologyProject(id="ontology-blocked", name="映射本体", domain="测试",
                                  created_by=_admin_id(db))
        mapping = OntologyMapping(id="mapping-blocked", ontology_id=project.id,
                                  curated_dataset_id=dataset.id, entity_class="User")
        pipeline = Pipeline(id="pipeline-blocked", name="消费流水线",
                            source_dataset_id=dataset.id, status="published")
        task = PipelineTask(id="task-blocked", name="消费任务", pipeline_id=pipeline.id)
        db.add_all([dataset, version, asset, mutation, project, mapping, pipeline, task])
        db.commit()

        blocked = client.delete(
            f"/api/v2/connections/{body['id']}", headers=auth_headers)
        assert blocked.status_code == 409
        detail = blocked.json()["detail"]
        assert detail["datasets_total"] == 1
        assert detail["history"] == {"dataset_versions": 1, "sync_runs": 0, "mutations": 1}
        assert detail["mapping_references_total"] == 1
        assert detail["mapping_references"][0]["dataset_name"] == "同步数据集-历史"
        assert detail["tasks_total"] == 1
        assert detail["tasks"][0]["name"] == "消费任务"
        for word in ("数据历史", "本体映射绑定", "数据任务"):
            assert word in detail["message"]

        # 历史与审计不被删除：注销拒绝后全部依赖原样保留
        assert db.get(DatasetVersion, "version-blocked") is not None
        assert db.get(IntegratedMutation, "mutation-history") is not None
        assert db.get(OntologyMapping, "mapping-blocked") is not None
        assert db.get(PipelineTask, "task-blocked") is not None
        assert db.get(Dataset, "ds-history-blocked") is not None

        # 解除全部依赖后受控注销恢复 204
        db.delete(task); db.delete(pipeline); db.delete(mapping); db.delete(project)
        db.delete(mutation); db.delete(asset); db.delete(version); db.delete(dataset)
        db.commit()
        ok = client.delete(f"/api/v2/connections/{body['id']}", headers=auth_headers)
        assert ok.status_code == 204
    finally:
        app.dependency_overrides.pop(connections_router.get_db, None)
