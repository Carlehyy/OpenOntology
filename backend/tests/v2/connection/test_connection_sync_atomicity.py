import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock

from app.data_channel.connections.router import SyncBody, trigger_sync
from app.models.v2.connection import Connection
from app.models.v2.dataset import Dataset, DatasetVersion
from app.tasks.v2.connection_sync import sync_connection




class _Connector:
    def list_resources(self):
        return ["orders"]

    def pull_full(self, _resource):
        return [{"order_id": "SO-1"}]


class _FailingStorage:
    def put_bytes(self, *_args, **_kwargs):
        raise ConnectionError("object store unavailable")


class _ResourceConnector:
    def list_resources(self):
        return ["orders", "customers"]

    def pull_full(self, resource):
        return [{"resource": resource}]


class _NoResourceConnector:
    def list_resources(self):
        return []


class _PullFailureConnector:
    def list_resources(self):
        return ["orders"]

    def pull_full(self, _resource):
        raise RuntimeError("upstream unavailable")


class _Storage:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, bucket, key, data, *_args, **_kwargs):
        uri = f"s3://{bucket}/{key}"
        self.objects[uri] = data
        return uri

    def delete_object(self, uri):
        self.objects.pop(uri, None)


# Celery 退役：stable task name / worker include 两条契约测试随任务框架
# 一并移除；异步派发契约改由 NATS subject 断言（见下方 dispatched 断言）。


def test_connection_sync_uses_database_when_managed_minio_is_unavailable(
    db, monkeypatch,
):
    connection = Connection(
        id="conn-atomic", name="订单库", kind="rest", config={}, status="inactive",
    )
    db.add(connection)
    db.commit()
    monkeypatch.setattr(
        "app.services.connection.registry.get_connector",
        lambda _kind, _config: _Connector(),
    )
    monkeypatch.setattr(
        "app.data_channel.datasets.service.get_storage_service",
        lambda: _FailingStorage(),
    )

    result = sync_connection(connection.id, db=db)

    assert result["status"] == "ok"
    dataset = db.query(Dataset).filter(
        Dataset.source_connection_id == connection.id
    ).one()
    version = db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == dataset.id).one()
    assert version.storage_uri is None
    assert version.data_size == len(version.data_blob)


def test_connection_resources_have_independent_dataset_identity_and_retry_is_idempotent(
    db, monkeypatch,
):
    connection = Connection(
        id="conn-resources", name="业务库", kind="rest", config={}, status="inactive",
    )
    db.add(connection)
    db.commit()
    connector = _ResourceConnector()
    storage = _Storage()
    monkeypatch.setattr(
        "app.services.connection.registry.get_connector",
        lambda _kind, _config: connector,
    )
    monkeypatch.setattr(
        "app.data_channel.datasets.service.get_storage_service",
        lambda: storage,
    )

    orders_first = sync_connection(connection.id, resource="orders", db=db)
    customers = sync_connection(connection.id, resource="customers", db=db)
    orders_retry = sync_connection(connection.id, resource="orders", db=db)

    assert orders_first["status"] == customers["status"] == orders_retry["status"] == "ok"
    assert orders_first["dataset_id"] != customers["dataset_id"]
    assert orders_retry["dataset_id"] == orders_first["dataset_id"]
    assert orders_first["version_no"] == 1
    assert customers["version_no"] == 1
    assert orders_retry["version_no"] == 2

    datasets = db.query(Dataset).filter(
        Dataset.source_connection_id == connection.id
    ).order_by(Dataset.source_resource).all()
    assert [(dataset.source_resource, dataset.id) for dataset in datasets] == [
        ("customers", customers["dataset_id"]),
        ("orders", orders_first["dataset_id"]),
    ]
    assert db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == orders_first["dataset_id"]
    ).count() == 2
    assert db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == customers["dataset_id"]
    ).count() == 1


@pytest.mark.parametrize(
    ("connector", "expected_error"),
    [
        (_NoResourceConnector(), "did not provide a resource"),
        (_PullFailureConnector(), "upstream unavailable"),
    ],
)
def test_failed_connection_sync_never_creates_empty_success_dataset(
    db, monkeypatch, connector, expected_error,
):
    connection = Connection(
        id=f"conn-failure-{connector.__class__.__name__}",
        name="失败连接",
        kind="rest",
        config={},
        status="inactive",
    )
    db.add(connection)
    db.commit()
    monkeypatch.setattr(
        "app.services.connection.registry.get_connector",
        lambda _kind, _config: connector,
    )

    result = sync_connection(connection.id, db=db)

    assert result["status"] == "error"
    assert expected_error in result["error"]
    db.refresh(connection)
    assert connection.status == "error"
    assert db.query(Dataset).filter(
        Dataset.source_connection_id == connection.id
    ).count() == 0


def test_mongo_connector_pull_failure_never_creates_version(db, monkeypatch):
    """Mongo 连接器拉取失败抛异常后：同步失败、不落数据集与版本。

    负向回归——连接器曾把故障吞成 []，被同步链按「源端 0 行」落成
    合法空版本；空列表本身仍是合法结果（见空版本告警测试），但拉取
    故障必须走 error 分支。"""
    from app.data_channel.connections.mongo_connector import MongoConnector

    connection = Connection(
        id="conn-mongo-pull-failure",
        name="Mongo 失败连接",
        kind="mongo",
        config={},
        status="inactive",
    )
    db.add(connection)
    db.commit()

    connector = MongoConnector(
        {"uri": "mongodb://localhost/testdb", "database": "db"})
    mock_db = MagicMock()
    mock_collection = MagicMock()
    mock_collection.find.side_effect = RuntimeError("mongo unavailable")
    mock_db.list_collection_names.return_value = ["orders"]
    mock_db.__getitem__ = MagicMock(return_value=mock_collection)
    connector._db = mock_db
    monkeypatch.setattr(
        "app.services.connection.registry.get_connector",
        lambda _kind, _config: connector,
    )

    result = sync_connection(connection.id, db=db)

    assert result["status"] == "error"
    assert "mongo unavailable" in result["error"]
    db.refresh(connection)
    assert connection.status == "error"
    assert db.query(Dataset).filter(
        Dataset.source_connection_id == connection.id
    ).count() == 0
    assert db.query(DatasetVersion).join(
        Dataset, DatasetVersion.dataset_id == Dataset.id
    ).filter(Dataset.source_connection_id == connection.id).count() == 0


def test_sync_endpoint_surfaces_connector_failure(db, monkeypatch):
    connection = Connection(
        id="conn-route-failure",
        name="路由失败连接",
        kind="rest",
        config={},
        status="inactive",
    )
    db.add(connection)
    db.commit()
    monkeypatch.setattr(
        "app.tasks.v2.connection_sync.sync_connection",
        lambda *_args, **_kwargs: {
            "status": "error",
            "error": "upstream unavailable",
        },
    )

    with pytest.raises(HTTPException) as exc_info:
        trigger_sync(connection.id, SyncBody(), db)

    assert exc_info.value.status_code == 502
    assert "upstream unavailable" in str(exc_info.value.detail)


def test_async_dispatch_does_not_mark_connection_active_before_completion(
    db, monkeypatch,
):
    connection = Connection(
        id="conn-async-dispatch",
        name="异步连接",
        kind="rest",
        config={},
        status="inactive",
    )
    db.add(connection)
    db.commit()
    from app.data_channel.pipeline_tasks.dispatch import CONNECTION_SYNC_SUBJECT

    dispatched = []
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_task",
        lambda subject, payload: dispatched.append((subject, payload)),
    )

    result = trigger_sync(
        connection.id,
        SyncBody(async_mode=True, resource="/orders"),
        db,
    )

    db.refresh(connection)
    assert result["status"] == "sync_triggered"
    assert connection.status == "inactive"
    assert dispatched == [(CONNECTION_SYNC_SUBJECT, {
        "connection_id": connection.id, "mode": "full", "resource": "/orders"})]


def test_async_dispatch_fails_closed_in_compatibility_mode(
    db, monkeypatch,
):
    connection = Connection(
        id="conn-async-strict",
        name="生产异步连接",
        kind="rest",
        config={},
        status="inactive",
    )
    db.add(connection)
    db.commit()
    dispatched_task = MagicMock()
    dispatched_task.delay.side_effect = ConnectionError("broker secret")
    monkeypatch.setattr(
        "app.tasks.v2.connection_sync.sync_connection",
        dispatched_task,
    )

    with pytest.raises(HTTPException) as exc_info:
        trigger_sync(
            connection.id,
            SyncBody(async_mode=True, resource="/orders"),
            db,
        )

    assert exc_info.value.status_code == 503
    assert "异步同步任务未投递" in str(exc_info.value.detail)
    assert "broker secret" not in str(exc_info.value.detail)
    dispatched_task.assert_not_called()
