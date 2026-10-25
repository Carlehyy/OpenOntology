"""In-process dispatch/ACK contracts, not real JetStream or source acceptance."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.data_channel.pipeline_tasks import dispatch, nats_executor


class Message:
    def __init__(self, payload):
        self.data = json.dumps(payload).encode()
        self.acks = 0
        self.naks = []

    async def ack(self):
        self.acks += 1

    async def nak(self, delay=None):
        self.naks.append(delay)

    async def in_progress(self):
        pass


def payload(**overrides):
    return {"run_id": "run-1", "asset_id": "asset-1", "actor_id": "actor-1",
            "batch_size": 500, **overrides}


def test_dispatch_preserves_exact_run_and_deduplication_key(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "_dispatch_sync", lambda *args: calls.append(args))
    dispatch.dispatch_integrated_sync("run-1", "asset-1", "actor-1", 500)
    assert calls == [("integrated.sync", payload(), "integrated.sync:run-1")]
    dispatch.dispatch_integrated_sync("run-1", "asset-1", "actor-1", 500)
    assert calls[0] == calls[1]


@pytest.mark.parametrize("changes", [
    {"run_id": ""}, {"run_id": "  "}, {"asset_id": None}, {"actor_id": 1},
    {"batch_size": True}, {"batch_size": 0}, {"batch_size": 1001}, {"batch_size": "500"},
])
def test_dispatch_rejects_invalid_wakeups_before_publish(monkeypatch, changes):
    def forbidden(*_args):
        pytest.fail("Invalid wakeup reached JetStream dispatch")

    monkeypatch.setattr(dispatch, "_dispatch_sync", forbidden)
    with pytest.raises(ValueError):
        dispatch.dispatch_integrated_sync(**payload(**changes))


def test_registry_retains_integrated_consumer_when_task_worker_is_excluded(monkeypatch):
    monkeypatch.setenv("TASK_INSTANCES_CONTROL_EXCLUDE", "1")
    entries = nats_executor._handler_registry()
    assert [(subject, durable) for subject, durable, _handler in entries
            if subject == "integrated.sync"] == [("integrated.sync", "integrated-sync-executor")]
    assert all(subject != "task_instances.control" for subject, _durable, _handler in entries)
    assert next(handler for subject, _durable, handler in entries
                if subject == "integrated.sync") is nats_executor._run_integrated_sync_message


def test_handler_preserves_actor_batch_and_exact_run(monkeypatch):
    calls = []

    def execute(asset_id, actor_id, batch_size, *, run_id):
        calls.append((asset_id, actor_id, batch_size, run_id))
        return {"status": "sealed"}

    monkeypatch.setattr("app.tasks.v2.integrated_sync.integrated_sync_task", execute)
    asyncio.run(nats_executor._run_integrated_sync_message(payload()))
    assert calls == [("asset-1", "actor-1", 500, "run-1")]


@pytest.mark.parametrize("status", ["starting", "loading", "sealed", "failed"])
def test_durably_resolved_outcomes_are_acked(monkeypatch, status):
    monkeypatch.setattr("app.tasks.v2.integrated_sync.integrated_sync_task",
                        lambda *_args, **_kwargs: {"status": status})
    message = Message(payload())
    asyncio.run(nats_executor.PipelineExecutor()._process_message(
        message, nats_executor._run_integrated_sync_message, "integrated.sync"))
    assert message.acks == 1
    assert message.naks == []


def test_unconfirmed_infrastructure_failure_is_redelivered(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("Platform outcome could not be confirmed")

    monkeypatch.setattr("app.tasks.v2.integrated_sync.integrated_sync_task", unavailable)
    message = Message(payload())
    asyncio.run(nats_executor.PipelineExecutor()._process_message(
        message, nats_executor._run_integrated_sync_message, "integrated.sync"))
    assert message.acks == 0
    assert message.naks == [5]


@pytest.mark.parametrize("invalid", [None, [], {}, {"run_id": "run-1"},
                                     payload(actor_id=" "), payload(batch_size=True),
                                     payload(batch_size=1001)])
def test_malformed_wakeup_is_discarded_without_starting_new_work(monkeypatch, invalid):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Malformed wakeup started a snapshot")

    monkeypatch.setattr("app.tasks.v2.integrated_sync.integrated_sync_task", forbidden)
    message = Message(invalid)
    asyncio.run(nats_executor.PipelineExecutor()._process_message(
        message, nats_executor._run_integrated_sync_message, "integrated.sync"))
    assert message.acks == 1
    assert message.naks == []


def test_model_registry_has_resolvable_foreign_keys():
    from app.database import Base
    from app.model_registry import import_all_models

    import_all_models()
    for table in Base.metadata.tables.values():
        for key in table.foreign_keys:
            assert key.column.table is Base.metadata.tables[key.column.table.name]
    assert "v2_integrated_assets" in Base.metadata.tables
    assert "v2_integrated_sync_runs" in Base.metadata.tables


def test_ontology_provider_is_assembled_without_reverse_domain_import():
    from functools import partial
    from app.data_channel.query.contracts import ColumnSpec, Relation, provider_for
    from app.ontologies.agent_query.read_port import resolve_ontology_relation

    provider = provider_for("ontology")
    assert isinstance(provider, partial)
    assert provider.func is resolve_ontology_relation
    assert provider.keywords == {"column_spec_factory": ColumnSpec, "relation_factory": Relation}


@pytest.mark.parametrize("role,grant_data,query_status,write_status", [
    ("admin", False, 200, 202),
    ("editor", False, 200, 202),
    ("viewer", False, 200, 403),
    ("custom", True, 200, 403),
    ("custom", False, 403, 403),
])
def test_read_post_does_not_inherit_asset_write_guard(
    client, db, monkeypatch, role, grant_data, query_status, write_status,
):
    """Exercise real route/menu dependencies with side-effect-free service fakes."""
    from types import SimpleNamespace
    from app.auth.models import RoleMenuPermission
    from app.data_channel.integrated import service
    from app.data_channel.query.router import QueryService
    from app.deps import get_current_user
    from app.main import app

    user = SimpleNamespace(id="actor-1", role=role)
    if grant_data:
        db.add(RoleMenuPermission(role="custom", menu_keys=["data", "data.structured"]))
        db.commit()
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: user)
    calls = []
    monkeypatch.setattr(QueryService, "execute", lambda self, body: calls.append("read") or {"rows": []})
    monkeypatch.setattr(service, "request_sync", lambda *_args: calls.append("write") or {"status": "queued"})

    result = client.post("/api/v2/query", json={
        "language": "spl", "query": "from x as x | fields x.id",
        "bindings": {"x": {"kind": "integrated", "assetId": "asset-1"}},
    })
    assert result.status_code == query_status
    result = client.post("/api/v2/integrated-datasets/asset-1/sync")
    assert result.status_code == write_status
    assert calls == (["read"] if query_status == 200 else []) + (["write"] if write_status == 202 else [])
