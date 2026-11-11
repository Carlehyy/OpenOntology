"""Remote row facts must survive retries and produce real internal notifications."""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.bootstrap.runtime_composition import install_runtime_ports
from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset
from app.data_channel.integrated.cdc_ingest import ingest
from app.data_channel.integrated.cdc_receiver import create_receiver
from app.data_channel.integrated.models import (
    IntegratedAsset, IntegratedCdcSource, IntegratedCdcSubscription,
)
from app.ontologies.formal_modeling.models import ActionType, ObjectInstance, ObjectType
from app.ontologies.projects.models import OntologyProject
from app.ontologies.sentinels import cdc
from app.ontologies.sentinels.cep.event_store import prune_event_log
from app.ontologies.sentinels.external_events import ExternalCdcOntologyAdapter
from app.ontologies.sentinels.models import (
    Notification, Sentinel, SentinelCdcOutbox, SentinelEventLog,
)
from app.ontologies.versions.models import OntologyVersion
from app.ontologies.versions.snapshot_contract import complete_snapshot, snapshot_hash


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def remote_release(db, admin_user, monkeypatch):
    # Keep delivery deterministic: only the test's durable drain may run actions.
    monkeypatch.setattr(cdc, "AUTO_DISPATCH", True)
    db.info[cdc.SUPPRESS_KEY] = True
    object_id = "cdc-flow-inventory-type"
    properties = [
        {"id": "item-id", "name": "item_id", "type": "string", "required": True},
        {"id": "quantity", "name": "quantity", "type": "number"},
        {"id": "state", "name": "state", "type": "string"},
    ]
    action_rules = [{
        "id": "cdc-flow-notification-rule", "name": "Notify operations",
        "type": "notification", "enabled": True,
        "config": {
            "channel": "internal", "recipientSource": "constant",
            "recipient": admin_user.id,
            "messageTemplate": "Inventory requires review",
        },
    }]
    project = OntologyProject(
        id="cdc-flow-ontology", name="CDC flow fixture", domain="synthetic",
        created_by=admin_user.id, status="published", version="v1",
    )
    db.add(project)
    db.flush()
    object_type = ObjectType(
        id=object_id, ontology_id=project.id, name="Inventory",
        display_name="Inventory", primary_key="item-id", properties=properties,
    )
    action = ActionType(
        id="cdc-flow-notify", ontology_id=project.id, name="notify_inventory",
        display_name="Inventory notification", object_type_id=object_id,
        parameters=[], rules=action_rules,
    )
    pattern = {
        "stages": [{
            "alias": "item", "objectTypeId": object_id,
            "filter": "item.quantity == 0 and item.state == 'ready'",
        }],
        "absence": {"enabled": False}, "within": 3600,
    }
    sentinel = Sentinel(
        id="cdc-flow-sentinel", ontology_id=project.id,
        name="inventory_zero", display_name="Inventory zero",
        bindings=[{"alias": "item", "objectTypeId": object_id}],
        links=[], pattern=pattern, condition=None, primary_alias="item",
        action_ids=[action.id], action_parameters={},
        trigger_mode="on_pattern", on_change=True, on_schedule=True,
        scan_interval_seconds=60, enabled=True, muted=False, status="published",
    )
    descriptor = {
        "assetId": "cdc-flow-asset", "datasetId": "cdc-flow-dataset",
        "connectionId": "cdc-flow-source", "schemaName": "public",
        "tableName": "inventory", "schemaFingerprint": _digest("schema"),
        "columns": [
            {"name": "source_id", "type": "string", "nullable": False},
            {"name": "balance", "type": "integer", "nullable": True},
            {"name": "source_status", "type": "string", "nullable": True},
        ],
        "primaryKey": ["source_id"],
    }
    snapshot = complete_snapshot({
        "objectTypes": [{
            "id": object_id, "name": "Inventory", "displayName": "Inventory",
            "primaryKey": "item-id", "properties": properties,
        }],
        "actions": [{
            "id": action.id, "name": action.name, "displayName": action.display_name,
            "objectTypeId": object_id, "parameters": [], "rules": action_rules,
        }],
        "sentinels": [{
            "id": sentinel.id, "name": sentinel.name,
            "displayName": sentinel.display_name, "bindings": sentinel.bindings,
            "links": [], "pattern": pattern, "condition": None,
            "primaryAlias": "item", "actionIds": [action.id],
            "actionParameters": {}, "triggerMode": "on_pattern",
            "onChange": True, "onSchedule": True, "scanIntervalSeconds": 60,
            "enabled": True, "muted": False, "status": "published",
        }],
        "mappings": [{
            "id": "cdc-flow-mapping", "entityClass": "Inventory",
            "targetObjectTypeId": object_id, "curatedDatasetId": "cdc-flow-dataset",
            "status": "active", "bindingMode": "remote", "remote": descriptor,
            "fieldMapping": {
                "source_id": "item_id", "balance": "quantity",
                "source_status": "state", "__primary_key__": "source_id",
                "__binding_mode__": "remote", "__remote__": descriptor,
            },
        }],
    })
    release = OntologyVersion(
        id="cdc-flow-release", ontology_id=project.id, version_number="v1",
        node_kind="release", lifecycle_status="released", revision=0,
        snapshot_formal=snapshot, snapshot_hash=snapshot_hash(snapshot),
        published_at=datetime.now(timezone.utc), created_by=admin_user.id,
    )
    db.add_all([object_type, action, sentinel, release])
    db.flush()
    project.current_release_id = release.id
    db.commit()
    # The seed release is setup, not a notification trigger under test.
    db.query(SentinelCdcOutbox).delete(synchronize_session=False)
    db.commit()
    return project, release, object_type, sentinel


def _event(remote_release, position: str, *, old=100, new=0):
    project, release, object_type, _sentinel = remote_release
    source_id = _digest(position)
    return {
        "ontology_id": project.id, "release_id": release.id,
        "mapping_id": "cdc-flow-mapping", "object_type_id": object_type.id,
        "asset_id": "cdc-flow-asset",
        "instance_id": "remote:" + _digest("cdc-flow-source/asset/item-1"),
        "source_event_id": source_id,
        "event_group_id": _digest(release.id + "/cdc-flow-mapping/" + source_id),
        "change_kind": "updated",
        "changes": {
            "quantity": {"old": old, "new": new},
            "state": {"old": "pending", "new": "ready"},
        },
        "snapshot": {
            "id": "remote:" + _digest("cdc-flow-source/asset/item-1"),
            "objectTypeId": object_type.id,
            "properties": {"item_id": "item-1", "quantity": new, "state": "ready"},
            "computed": {}, "sourcePosition": {"lsn": position},
        },
        # Identical timestamps intentionally cannot act as a dedupe identity.
        "occurred_at": datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc),
    }


def _drain(db):
    return cdc.drain_cdc_outbox(
        session_factory=sessionmaker(bind=db.get_bind()), limit=50)


def test_external_facts_and_outbox_rollback_together(db, remote_release):
    adapter = ExternalCdcOntologyAdapter()
    result = adapter.capture(db, _event(remote_release, "100"))
    assert result["status"] == "captured"
    db.flush()
    assert db.query(SentinelEventLog).count() == 2
    assert db.query(SentinelCdcOutbox).count() == 1
    db.rollback()
    assert db.query(SentinelEventLog).count() == 0
    assert db.query(SentinelCdcOutbox).count() == 0
    assert db.query(Notification).count() == 0


def test_external_row_group_notifies_once_and_replay_is_idempotent(db, remote_release):
    adapter = ExternalCdcOntologyAdapter()
    event = _event(remote_release, "101")
    assert adapter.capture(db, event)["status"] == "captured"
    db.commit()
    result = _drain(db)
    assert result["errors"] == []
    assert db.query(Notification).count() == 1
    assert db.query(ObjectInstance).count() == 0
    assert adapter.capture(db, event)["status"] == "duplicate"
    db.commit()
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 1
    notice = db.query(Notification).one()
    assert notice.ontology_release_id == remote_release[1].id
    assert notice.sentinel_id == remote_release[3].id
    assert notice.action_log_id


def test_same_time_distinct_events_preserve_transient_zero(db, remote_release):
    adapter = ExternalCdcOntologyAdapter()
    for position, old, new in (("201", 100, 0), ("202", 0, 100), ("203", 100, 0)):
        assert adapter.capture(
            db, _event(remote_release, position, old=old, new=new))["status"] == "captured"
    db.commit()
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 2
    assert db.query(SentinelEventLog).count() == 6
    assert db.query(ObjectInstance).count() == 0


def test_multi_property_row_remains_one_event_at_page_boundary(db, remote_release, monkeypatch):
    from app.ontologies.sentinels.cep import contract

    monkeypatch.setattr(contract, "PATTERN_EVENT_BATCH_LIMIT", 1)
    assert ExternalCdcOntologyAdapter().capture(
        db, _event(remote_release, "250"))["status"] == "captured"
    db.commit()
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 1
    assert db.query(SentinelEventLog).count() == 2
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 1


def test_completed_match_suppresses_replay_after_fact_and_outbox_pruning(
        db, remote_release):
    adapter = ExternalCdcOntologyAdapter()
    event = _event(remote_release, "pruned-replay")
    assert adapter.capture(db, event)["status"] == "captured"
    db.commit()
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 1
    db.query(SentinelEventLog).delete(synchronize_session=False)
    db.query(SentinelCdcOutbox).delete(synchronize_session=False)
    db.commit()
    # The completed match receipt outlives the seven-day fact retention.
    assert adapter.capture(db, event)["status"] == "captured"
    db.commit()
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 1


def test_retention_uses_admission_time_for_completed_external_event(
        db, remote_release):
    event = _event(remote_release, "old-source-timestamp")
    event["occurred_at"] = datetime.now(timezone.utc) - timedelta(days=30)
    assert ExternalCdcOntologyAdapter().capture(db, event)["status"] == "captured"
    db.commit()
    assert _drain(db)["errors"] == []
    assert prune_event_log(sessionmaker(bind=db.get_bind())) == 0
    assert db.query(SentinelEventLog).count() == 2


def test_retention_keeps_expired_pending_external_event(db, remote_release):
    assert ExternalCdcOntologyAdapter().capture(
        db, _event(remote_release, "pending-old"))["status"] == "captured"
    old = datetime.now(timezone.utc) - timedelta(days=8)
    db.query(SentinelEventLog).update({
        SentinelEventLog.created_at: old, SentinelEventLog.occurred_at: old,
    }, synchronize_session=False)
    db.commit()
    assert prune_event_log(sessionmaker(bind=db.get_bind())) == 0
    assert db.query(SentinelEventLog).count() == 2
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 1
    assert prune_event_log(sessionmaker(bind=db.get_bind())) == 2
    assert db.query(SentinelEventLog).count() == 0


@pytest.fixture
def remote_ingress(db, remote_release, admin_user):
    install_runtime_ports()
    project, release, object_type, _sentinel = remote_release
    subscription = ExternalCdcOntologyAdapter().subscriptions(db)[0]
    connection = Connection(
        id="cdc-flow-source", name="Synthetic CDC source", kind="postgres",
        config={}, status="active", created_by=admin_user.id,
    )
    dataset = Dataset(
        id="cdc-flow-dataset", name="Synthetic inventory", kind="structured",
        source_connection_id=connection.id, source_resource="public.inventory",
    )
    db.add(connection)
    db.flush()
    db.add(dataset)
    db.flush()
    asset = IntegratedAsset(
        id="cdc-flow-asset", dataset_id=dataset.id,
        connection_id=connection.id, name="Synthetic inventory",
        schema_name="public", table_name="inventory",
        schema_json={"columns": subscription["contract"]["columns"]},
        schema_fingerprint=_digest("schema"), created_by=admin_user.id,
    )
    db.add(asset)
    db.flush()
    source = IntegratedCdcSource(
        connection_id=connection.id, generation=1, status="active",
        heartbeat_at=datetime.now(timezone.utc),
    )
    db.add(source)
    db.add(IntegratedCdcSubscription(
        connection_id=connection.id, asset_id=asset.id,
        ontology_id=project.id, release_id=release.id,
        mapping_id="cdc-flow-mapping", object_type_id=object_type.id,
        field_map=subscription["field_map"],
        primary_key_columns=subscription["primary_key_columns"],
        contract=subscription["contract"], schema_name="public",
        table_name="inventory", schema_fingerprint=_digest("schema"),
        source_generation=1, status="active",
        activation_position={
            "dialect": "postgresql", "lsn": 10, "data_collection_order": 0,
        },
        activated_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return source


@pytest.fixture
def second_remote_release(db, remote_release, remote_ingress):
    original_project, original_release, _object_type, _sentinel = remote_release
    snapshot = deepcopy(original_release.snapshot_formal)
    object_id = "cdc-flow-second-type"
    action_id = "cdc-flow-second-notify"
    sentinel_id = "cdc-flow-second-sentinel"
    mapping_id = "cdc-flow-second-mapping"
    object_type = snapshot["objectTypes"][0]
    object_type["id"] = object_id
    action = snapshot["actions"][0]
    action["id"], action["objectTypeId"] = action_id, object_id
    sentinel = snapshot["sentinels"][0]
    sentinel["id"], sentinel["actionIds"] = sentinel_id, [action_id]
    sentinel["bindings"][0]["objectTypeId"] = object_id
    sentinel["pattern"]["stages"][0]["objectTypeId"] = object_id
    mapping = snapshot["mappings"][0]
    mapping["id"], mapping["targetObjectTypeId"] = mapping_id, object_id
    project = OntologyProject(
        id="cdc-flow-second-ontology", name="Second CDC consumer",
        domain="synthetic", created_by=original_project.created_by,
        status="published", version="v1",
    )
    db.add(project)
    db.flush()
    release = OntologyVersion(
        id="cdc-flow-second-release", ontology_id=project.id,
        version_number="v1", node_kind="release", lifecycle_status="released",
        revision=0, snapshot_formal=snapshot, snapshot_hash=snapshot_hash(snapshot),
        published_at=datetime.now(timezone.utc), created_by=original_release.created_by,
    )
    db.add(release)
    db.flush()
    project.current_release_id = release.id
    original_subscription = db.query(IntegratedCdcSubscription).one()
    db.add(IntegratedCdcSubscription(
        connection_id=remote_ingress.connection_id, asset_id="cdc-flow-asset",
        ontology_id=project.id, release_id=release.id, mapping_id=mapping_id,
        object_type_id=object_id, field_map=original_subscription.field_map,
        primary_key_columns=original_subscription.primary_key_columns,
        contract=original_subscription.contract, schema_name="public",
        table_name="inventory", schema_fingerprint=_digest("schema"),
        source_generation=1, status="active",
        activation_position=original_subscription.activation_position,
        activated_at=datetime.now(timezone.utc),
    ))
    db.commit()
    db.query(SentinelCdcOutbox).delete(synchronize_session=False)
    db.commit()
    return project, release


def _postgres_envelope(lsn, *, old=100, new=0, transaction_id="tx-1", order=1):
    columns = [
        {"field": "source_id", "type": "string", "optional": False},
        {"field": "balance", "type": "int32", "optional": True},
        {"field": "source_status", "type": "string", "optional": True},
    ]
    return {
        "schema": {
            "type": "struct", "name": "cdc_flow.public.inventory.Envelope",
            "fields": [
                {"field": image, "type": "struct", "optional": True,
                 "fields": columns}
                for image in ("before", "after")
            ],
        },
        "payload": {
            "before": {"source_id": "item-1", "balance": old,
                       "source_status": "pending"},
            "after": {"source_id": "item-1", "balance": new,
                      "source_status": "ready"},
            "source": {
                "connector": "postgresql", "name": "cdc_flow_g1",
                "db": "cdc_source", "schema": "public", "table": "inventory",
                "lsn": lsn, "ts_ms": 1794297600000,
            },
            "op": "u", "ts_ms": 1794297600000,
            "transaction": {
                "id": transaction_id, "data_collection_order": order,
                "total_order": order,
            },
        },
    }


def test_official_envelope_ingress_delivers_notification_and_deduplicates(
        db, remote_release, remote_ingress):
    envelope = _postgres_envelope(101)
    assert ingest(db, remote_ingress.connection_id, 1, envelope)["status"] == "captured"
    db.commit()
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 1
    assert db.query(ObjectInstance).count() == 0
    assert db.query(SentinelEventLog).count() == 2
    assert ingest(db, remote_ingress.connection_id, 1, envelope)["status"] == "duplicate"
    db.commit()
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 1


def test_signed_http_delivery_commits_facts_before_ack_and_notifies_once(
        db, remote_release, remote_ingress):
    secret = b"\x11" * 32
    factory = sessionmaker(
        bind=db.get_bind(), info={cdc.SUPPRESS_KEY: True})
    receiver = create_receiver(session_factory=factory, host="127.0.0.1", port=0)
    try:
        registration = receiver.register(remote_ingress.connection_id, 1, secret)
        body = json.dumps(_postgres_envelope(160)).encode()
        for delivery in range(2):
            message_id = f"flow-delivery-{delivery}"
            timestamp = str(int(time.time()))
            signature = base64.b64encode(hmac.new(
                secret, f"{message_id}.{timestamp}.".encode() + body,
                hashlib.sha256).digest()).decode()
            connection = http.client.HTTPConnection(
                "127.0.0.1", receiver.port, timeout=10)
            try:
                connection.request("POST", registration.path, body=body, headers={
                    "Content-Type": "application/json",
                    "webhook-id": message_id, "webhook-timestamp": timestamp,
                    "webhook-signature": "v1," + signature,
                })
                response = connection.getresponse()
                payload = json.loads(response.read())
                assert response.status == 200, payload
                assert payload["status"] == (
                    "captured" if delivery == 0 else "duplicate")
            finally:
                connection.close()
            db.expire_all()
            assert db.query(SentinelEventLog).count() == 2
            assert db.query(SentinelCdcOutbox).count() == 1
        assert _drain(db)["errors"] == []
        assert db.query(Notification).count() == 1
        assert db.query(ObjectInstance).count() == 0
    finally:
        receiver.stop()


def test_same_source_row_fans_out_to_both_released_ontologies(
        db, remote_release, remote_ingress, second_remote_release):
    assert ingest(
        db, remote_ingress.connection_id, 1,
        _postgres_envelope(175))["status"] == "captured"
    db.commit()
    assert db.query(SentinelEventLog).count() == 4
    assert db.query(SentinelCdcOutbox).count() == 2
    assert _drain(db)["errors"] == []
    notices = db.query(Notification).all()
    assert len(notices) == 2
    assert {notice.ontology_release_id for notice in notices} == {
        remote_release[1].id, second_remote_release[1].id,
    }
    assert all(notice.action_log_id for notice in notices)
    assert db.query(ObjectInstance).count() == 0


def test_capacity_rejection_rolls_back_entire_cross_ontology_fanout(
        db, remote_release, remote_ingress, second_remote_release, monkeypatch):
    from app.ontologies.sentinels.cep import contract

    monkeypatch.setattr(contract, "EVENT_LOG_MAX_ROWS", 3)
    assert ingest(
        db, remote_ingress.connection_id, 1,
        _postgres_envelope(176))["status"] == "capacity_exceeded"
    db.rollback()
    assert db.query(SentinelEventLog).count() == 0
    assert db.query(SentinelCdcOutbox).count() == 0
    assert db.query(Notification).count() == 0
    assert db.get(IntegratedCdcSource, "cdc-flow-source").last_position is None


def test_mysql_binlog_rows_preserve_transient_zero_and_replay_once(
        db, remote_release, remote_ingress):
    connection = db.get(Connection, remote_ingress.connection_id)
    connection.kind = "mysql"
    subscription = db.query(IntegratedCdcSubscription).one()
    subscription.activation_position = {
        "dialect": "mysql", "file": "mysql-bin.000001", "pos": 100, "row": 0,
    }
    db.commit()
    envelopes = []
    for row, old, new in ((0, 100, 0), (1, 0, 100), (2, 100, 0)):
        envelope = _postgres_envelope(200, old=old, new=new)
        envelope["schema"]["name"] = "cdc_flow.public.inventory.Envelope"
        envelope["payload"]["source"] = {
            "connector": "mysql", "name": "cdc_flow_g1", "db": "public",
            "table": "inventory", "file": "mysql-bin.000001",
            "pos": 200, "row": row, "ts_ms": 1794297600000,
        }
        assert ingest(db, connection.id, 1, envelope)["status"] == "captured"
        db.commit()
        envelopes.append(envelope)
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 2
    assert db.query(SentinelEventLog).count() == 6
    assert db.query(ObjectInstance).count() == 0
    for envelope in envelopes:
        assert ingest(db, connection.id, 1, envelope)["status"] == "duplicate"
    db.commit()
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 2


def test_ingress_rollback_preserves_source_position_and_has_no_side_effects(
        db, remote_release, remote_ingress):
    assert remote_ingress.last_position is None
    assert ingest(
        db, remote_ingress.connection_id, 1,
        _postgres_envelope(150))["status"] == "captured"
    db.flush()
    assert db.query(SentinelEventLog).count() == 2
    assert db.query(SentinelCdcOutbox).count() == 1
    db.rollback()
    assert db.query(SentinelEventLog).count() == 0
    assert db.query(SentinelCdcOutbox).count() == 0
    assert db.query(Notification).count() == 0
    assert db.get(IntegratedCdcSource, "cdc-flow-source").last_position is None


def test_ingress_preserves_late_committing_postgres_row_with_lower_lsn(
        db, remote_release, remote_ingress):
    for lsn, old, new, txid in (
            (300, 100, 0, "committed-first"),
            (200, 0, 100, "committed-later"),
            (400, 100, 0, "committed-last")):
        assert ingest(
            db, remote_ingress.connection_id, 1,
            _postgres_envelope(lsn, old=old, new=new,
                               transaction_id=txid))["status"] == "captured"
        db.commit()
    assert db.query(SentinelEventLog).count() == 6
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 2
    assert db.query(ObjectInstance).count() == 0


def test_external_capture_rejects_stale_release_without_side_effects(db, remote_release):
    project, release, _object_type, _sentinel = remote_release
    event = _event(remote_release, "301")
    replacement = OntologyVersion(
        id="cdc-flow-new-release", ontology_id=project.id,
        version_number="v2", node_kind="release", lifecycle_status="released",
        snapshot_formal=release.snapshot_formal,
        created_by=release.created_by, published_at=datetime.now(timezone.utc),
    )
    db.add(replacement)
    db.flush()
    project.current_release_id = replacement.id
    db.commit()
    db.query(SentinelCdcOutbox).delete(synchronize_session=False)
    db.commit()
    assert ExternalCdcOntologyAdapter().capture(db, event)["status"] == "stale"
    db.commit()
    assert db.query(SentinelEventLog).count() == 0
    assert db.query(SentinelCdcOutbox).count() == 0
    assert db.query(Notification).count() == 0
