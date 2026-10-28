"""集成资产受控注销：零历史 / 零映射绑定 / 零流水线与任务才允许。

任何状态的历史（版本、queued/loading/failed 同步运行、pending/failed/unknown
写入回执）都阻断注销且绝不随注销删除；这是审计边界，不是可清障的统计口径。
HTTP 契约：DELETE /api/v2/integrated-datasets/{asset_id} → 204 或结构化 409。
"""
import uuid

import pytest
from fastapi import HTTPException

from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset, DatasetVersion, IntegratedSyncRun
from app.data_channel.integrated import service
from app.data_channel.integrated.models import IntegratedAsset, IntegratedMutation
from app.data_channel.pipeline_tasks.models import PipelineTask
from app.data_channel.pipelines.models import Pipeline
from app.ontologies.mappings.models import OntologyMapping
from app.ontologies.projects.models import OntologyProject


def _make_asset(db, user, *, asset_id="asset-x", dataset_id="ds-x",
                connection_id="conn-x"):
    connection = Connection(id=connection_id, name="源连接", kind="postgres", config={})
    dataset = Dataset(id=dataset_id, name="集成数据集", kind="integrated",
                      source_connection_id=connection_id, source_resource="public.rows")
    asset = IntegratedAsset(
        id=asset_id, dataset_id=dataset_id, connection_id=connection_id,
        name="集成资产", schema_name="public", table_name="rows",
        schema_json={"columns": [], "primary_key": ["id"]},
        schema_fingerprint="fingerprint", writable=False, created_by=user.id)
    db.add_all([connection, dataset, asset])
    db.commit()
    return asset


def test_decommission_requires_owner_or_admin(db, admin_user, editor_user):
    asset = _make_asset(db, admin_user)
    with pytest.raises(HTTPException) as exc:
        service.decommission_asset(db, editor_user, asset.id)
    assert (exc.value.status_code, exc.value.detail["code"]) == (403, "ASSET_OWNER_REQUIRED")
    assert db.get(IntegratedAsset, asset.id) is not None


def test_decommission_blocked_by_history_of_any_status(db, admin_user):
    """queued 运行与 failed 回执都算历史；拒绝后审计记录原样保留。"""
    asset = _make_asset(db, admin_user, asset_id="asset-h", dataset_id="ds-h",
                        connection_id="conn-h")
    run = IntegratedSyncRun(
        id="run-queued", dataset_id="ds-h", asset_id="asset-h", status="queued",
        physical_table="lake_iv_queued", schema_json={"queued": True},
        snapshot_json={"queued": True}, expected_rows=0, row_count=0)
    mutation = IntegratedMutation(
        id="mutation-failed", asset_id="asset-h", actor_id=admin_user.id,
        idempotency_key="idem-history", operation="insert", request_hash="h",
        key_hash="k", schema_fingerprint="fingerprint", status="failed")
    db.add_all([run, mutation])
    db.commit()

    with pytest.raises(HTTPException) as exc:
        service.decommission_asset(db, admin_user, "asset-h")
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["code"] == "ASSET_DECOMMISSION_BLOCKED"
    assert detail["history"] == {"dataset_versions": 0, "sync_runs": 1, "mutations": 1}
    assert "数据历史" in detail["message"]
    # 审计不被删除
    assert db.get(IntegratedSyncRun, "run-queued") is not None
    assert db.get(IntegratedMutation, "mutation-failed") is not None
    assert db.get(IntegratedAsset, "asset-h") is not None


def test_decommission_blocked_by_version_mapping_pipeline_and_task(db, admin_user):
    asset = _make_asset(db, admin_user, asset_id="asset-ref", dataset_id="ds-ref",
                        connection_id="conn-ref")
    version = DatasetVersion(id="version-ref", dataset_id="ds-ref", version_no=1)
    project = OntologyProject(id="ontology-ref", name="映射本体", domain="测试",
                              created_by=admin_user.id)
    mapping = OntologyMapping(id="mapping-ref", ontology_id="ontology-ref",
                              curated_dataset_id="ds-ref", entity_class="User")
    pipeline = Pipeline(id="pipeline-ref", name="消费流水线",
                        source_dataset_id="ds-ref", status="published")
    task = PipelineTask(id="task-ref", name="消费任务", pipeline_id="pipeline-ref")
    db.add_all([version, project, mapping, pipeline, task])
    db.commit()

    with pytest.raises(HTTPException) as exc:
        service.decommission_asset(db, admin_user, "asset-ref")
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["history"]["dataset_versions"] == 1
    assert detail["mapping_references_total"] == 1
    assert detail["pipelines_total"] == 1
    assert detail["tasks_total"] == 1
    for word in ("数据历史", "本体映射绑定", "流水线", "数据任务"):
        assert word in detail["message"]
    for row_id, model in [("version-ref", DatasetVersion), ("mapping-ref", OntologyMapping),
                          ("pipeline-ref", Pipeline), ("task-ref", PipelineTask)]:
        assert db.get(model, row_id) is not None


def test_decommission_zero_dependency_removes_asset_and_dataset_only(db, admin_user):
    asset = _make_asset(db, admin_user, asset_id="asset-clean", dataset_id="ds-clean",
                        connection_id="conn-clean")
    result = service.decommission_asset(db, admin_user, "asset-clean")
    assert result == {"status": "decommissioned", "asset_id": "asset-clean",
                      "dataset_id": "ds-clean"}
    assert db.get(IntegratedAsset, "asset-clean") is None
    assert db.get(Dataset, "ds-clean") is None
    # 连接本体不受资产注销影响；其注销由 DELETE /connections/{id} 独立裁决
    assert db.get(Connection, "conn-clean") is not None


def test_integrated_decommission_http_contract(client, db, auth_headers, admin_user):
    asset = _make_asset(db, admin_user, asset_id="asset-http", dataset_id="ds-http",
                        connection_id="conn-http")
    blocked = client.delete(
        f"/api/v2/integrated-datasets/{asset.id}", headers=auth_headers)
    # 空历史资产：注销成功
    assert blocked.status_code == 204, blocked.text
    assert db.get(IntegratedAsset, "asset-http") is None

    run_asset = _make_asset(db, admin_user, asset_id="asset-run", dataset_id="ds-run",
                            connection_id="conn-run")
    db.add(IntegratedSyncRun(
        id="run-http", dataset_id="ds-run", asset_id="asset-run", status="loading",
        physical_table="lake_iv_http", schema_json={"loading": True},
        snapshot_json={"loading": True}, expected_rows=10, row_count=3))
    db.commit()
    rejected = client.delete(
        f"/api/v2/integrated-datasets/{run_asset.id}", headers=auth_headers)
    assert rejected.status_code == 409
    detail = rejected.json()["detail"]
    assert detail["code"] == "ASSET_DECOMMISSION_BLOCKED"
    assert detail["history"]["sync_runs"] == 1
    assert db.get(IntegratedAsset, "asset-run") is not None


def test_credential_rotation_invalidates_auth_fingerprint_across_sessions(db, admin_user):
    """凭据轮换必须改变 resolve_metadata 的鉴权指纹。

    统一查询 cursor 复核（query_context_changed 409）与内存结果句柄都绑定该
    指纹；轮换后旧句柄/游标必须失效。轮换在另一会话提交，本会话身份映射里
    仍是旧连接行——populate_existing 必须越过缓存取到新配置摘要。
    """
    import sqlalchemy as sa

    from app.data_channel.integrated import native
    from tests.conftest import TestSession

    asset = _make_asset(db, admin_user, asset_id="asset-fp", dataset_id="ds-fp",
                        connection_id="conn-fp")
    first = native.resolve_metadata(db, admin_user, ["asset-fp"])
    assert db.get(Connection, "conn-fp").config == {}  # 身份映射已缓存旧行

    other = TestSession()
    try:
        other.execute(
            sa.update(Connection).where(Connection.id == "conn-fp")
            .values(config={"_encrypted": "rotated-cipher-text"}))
        other.commit()
    finally:
        other.close()

    second = native.resolve_metadata(db, admin_user, ["asset-fp"])
    assert (second["asset-fp"]["auth_fingerprint"]
            != first["asset-fp"]["auth_fingerprint"])
    # 指纹只含单向摘要：轮换后的密文不进入任何元数据输出
    assert "rotated-cipher-text" not in str(second)
    # 其余元数据保持稳定（列/主键/结构指纹不因轮换漂移）
    for key in ("columns", "primary_key", "schema_fingerprint", "dialect"):
        assert second["asset-fp"][key] == first["asset-fp"][key]
