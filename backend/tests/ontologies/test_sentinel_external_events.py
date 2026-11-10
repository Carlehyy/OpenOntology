"""Remote (external CDC) single-row Sentinels: adapter, evaluation, gates.

Covers the ontology side of external CDC → Sentinel → internal Notification
without a local ObjectInstance: frozen-release subscriptions, transactional
capture, per-source-event evaluation that is independent of the shared pattern
cursor, the single-row restrictions and their execution-time defence.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

from app.ontologies.formal_modeling.models import (
    ActionType, ObjectInstance, ObjectType,
)
from app.ontologies.projects.models import OntologyProject
from app.ontologies.sentinels import cdc, engine, evaluator, external_events
from app.ontologies.sentinels import validation as sentinel_validation
from app.ontologies.sentinels.cep import contract as cep_contract
from app.ontologies.sentinels.cep import event_store, pattern as cep_pattern
from app.ontologies.sentinels.external_events import ExternalCdcOntologyAdapter
from app.ontologies.sentinels.models import (
    Notification, Sentinel, SentinelCdcOutbox, SentinelEventLog,
    SentinelFiring, SentinelMatchState, SentinelPatternCursor,
)
from app.ontologies.versions import trial_service
from app.ontologies.versions.models import OntologyVersion
from app.ontologies.versions.snapshot_contract import (
    complete_snapshot, snapshot_hash,
)

OCCURRED_AT = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)

PROPERTIES = [
    {"id": "p-item", "name": "item_id", "type": "string", "required": True},
    {"id": "p-qty", "name": "quantity", "type": "number"},
    {"id": "p-state", "name": "state", "type": "string"},
    # Declared on the ObjectType but not mapped to any source column.
    {"id": "p-margin", "name": "margin", "type": "number"},
]
NOTIFY_RULES = [{
    "id": "rule-notify", "name": "Notify operations", "type": "notification",
    "enabled": True,
    "config": {
        "channel": "internal", "recipientSource": "constant",
        "recipient": "ops", "messageTemplate": "Inventory requires review",
    },
}]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _ids(key: str) -> dict:
    return {
        "ontology": f"{key}-ontology", "release": f"{key}-release",
        "type": f"{key}-type", "action": f"{key}-action",
        "sentinel": f"{key}-sentinel", "mapping": f"{key}-mapping",
        "asset": f"{key}-asset", "dataset": f"{key}-dataset",
        "connection": f"{key}-connection",
    }


def _descriptor(key: str, *, columns=None, primary_key=None, **extra) -> dict:
    ids = _ids(key)
    return {
        "assetId": ids["asset"], "datasetId": ids["dataset"],
        "connectionId": ids["connection"], "schemaName": "public",
        "tableName": "inventory", "schemaFingerprint": _digest("schema"),
        "columns": columns or [
            {"name": "source_id", "type": "string", "nullable": False},
            {"name": "balance", "type": "integer", "nullable": True},
            {"name": "source_status", "type": "string", "nullable": True},
        ],
        "primaryKey": primary_key or ["source_id"],
        **extra,
    }


def _pattern(key: str, filter_expr="item.quantity == 0 and item.state == 'ready'",
             **extra) -> dict:
    return {
        "stages": [{
            "alias": "item", "objectTypeId": _ids(key)["type"],
            "filter": filter_expr,
        }],
        "absence": {"enabled": False}, "within": 3600, **extra,
    }


def _mapping(key: str, descriptor: dict, field_mapping=None, *,
             mapping_id=None) -> dict:
    ids = _ids(key)
    business = field_mapping or {
        "source_id": "item_id", "balance": "quantity",
        "source_status": "state",
    }
    return {
        "id": mapping_id or ids["mapping"], "entityClass": "Inventory",
        "targetObjectTypeId": ids["type"],
        "curatedDatasetId": ids["dataset"], "status": "active",
        "bindingMode": "remote", "remote": descriptor,
        "fieldMapping": {
            **business, "__primary_key__": descriptor["primaryKey"][0],
            "__binding_mode__": "remote", "__remote__": descriptor,
        },
    }


def _snapshot(key: str, *, descriptor=None, field_mapping=None, rules=None,
              pattern=None, mappings=None, sentinel=None) -> dict:
    ids = _ids(key)
    descriptor = descriptor or _descriptor(key)
    return complete_snapshot({
        "objectTypes": [{
            "id": ids["type"], "name": "Inventory", "displayName": "Inventory",
            "primaryKey": "p-item", "properties": PROPERTIES,
        }],
        "actions": [{
            "id": ids["action"], "name": "notify_inventory",
            "displayName": "Inventory notification",
            "objectTypeId": ids["type"], "parameters": [],
            "rules": NOTIFY_RULES if rules is None else rules,
        }],
        "sentinels": [{
            "id": ids["sentinel"], "name": "inventory_zero",
            "displayName": "Inventory zero",
            "bindings": [{"alias": "item", "objectTypeId": ids["type"]}],
            "links": [], "pattern": pattern or _pattern(key),
            "condition": None, "primaryAlias": "item",
            "actionIds": [ids["action"]], "actionParameters": {},
            "triggerMode": "on_pattern", "onChange": True, "onSchedule": True,
            "scanIntervalSeconds": 60, "enabled": True, "muted": False,
            "status": "published", **(sentinel or {}),
        }],
        "mappings": mappings if mappings is not None else [
            _mapping(key, descriptor, field_mapping)],
    })


def _release(db, admin_user, key: str = "ext", **snapshot_options) -> dict:
    """Persist one project whose current release remote-binds one ObjectType."""
    ids = _ids(key)
    snapshot = _snapshot(key, **snapshot_options)
    project = OntologyProject(
        id=ids["ontology"], name=f"{key} fixture", domain="synthetic",
        created_by=admin_user.id, status="published", version="v1",
    )
    db.add(project)
    db.flush()
    action_entry = snapshot["actions"][0]
    sentinel_entry = snapshot["sentinels"][0]
    db.add_all([
        ObjectType(
            id=ids["type"], ontology_id=project.id, name="Inventory",
            display_name="Inventory", primary_key="p-item",
            properties=PROPERTIES),
        ActionType(
            id=ids["action"], ontology_id=project.id, name="notify_inventory",
            display_name="Inventory notification", object_type_id=ids["type"],
            parameters=[], rules=action_entry["rules"]),
        Sentinel(
            id=ids["sentinel"], ontology_id=project.id, name="inventory_zero",
            display_name="Inventory zero",
            bindings=sentinel_entry["bindings"], links=[],
            pattern=sentinel_entry["pattern"], condition=None,
            primary_alias="item", action_ids=[ids["action"]],
            action_parameters={}, trigger_mode="on_pattern", on_change=True,
            on_schedule=True, scan_interval_seconds=60, enabled=True,
            muted=False, status="published"),
        OntologyVersion(
            id=ids["release"], ontology_id=project.id, version_number="v1",
            node_kind="release", lifecycle_status="released", revision=0,
            snapshot_formal=snapshot, snapshot_hash=snapshot_hash(snapshot),
            published_at=datetime.now(timezone.utc),
            created_by=admin_user.id),
    ])
    db.flush()
    project.current_release_id = ids["release"]
    db.commit()
    # Seeding the release is setup, not a trigger under test.
    db.query(SentinelCdcOutbox).delete(synchronize_session=False)
    db.commit()
    return ids


def _event(ids: dict, position: str, *, quantity=0, state="ready",
           row="item-1", **overrides) -> dict:
    instance_id = "remote:" + _digest(f"{ids['asset']}/{row}")
    source_event_id = _digest(f"{ids['mapping']}/{position}")
    event = {
        "ontology_id": ids["ontology"], "release_id": ids["release"],
        "mapping_id": ids["mapping"], "object_type_id": ids["type"],
        "asset_id": ids["asset"], "instance_id": instance_id,
        "source_event_id": source_event_id,
        # One group id per source row: successive events of a row share it.
        "event_group_id": _digest(f"{ids['mapping']}/{row}"),
        "change_kind": "updated",
        "changes": {
            "quantity": {"old": 100, "new": quantity},
            "state": {"old": "pending", "new": state},
        },
        "snapshot": {
            "id": instance_id, "objectTypeId": ids["type"],
            "properties": {
                "item_id": row, "quantity": quantity, "state": state},
            "computed": {}, "sourcePosition": {"lsn": position},
        },
        "occurred_at": OCCURRED_AT,
    }
    event.update(overrides)
    return event


def _drain(db) -> dict:
    return cdc.drain_cdc_outbox(
        session_factory=sessionmaker(bind=db.get_bind()), limit=50)


@pytest.fixture(autouse=True)
def _isolated_runtime(db, monkeypatch):
    # Only the test's explicit durable drain may run actions.
    monkeypatch.setattr(cdc, "AUTO_DISPATCH", True)
    db.info[cdc.SUPPRESS_KEY] = True
    external_events.clear_subscription_cache()
    yield
    external_events.clear_subscription_cache()


# ---------------------------------------------------------------------------
# Subscriptions: authoritative frozen release, physical addressing
# ---------------------------------------------------------------------------

def test_subscription_exposes_the_frozen_release_binding(db, admin_user):
    ids = _release(db, admin_user)

    [subscription] = ExternalCdcOntologyAdapter().subscriptions(db)

    assert subscription == {
        "ontology_id": ids["ontology"], "release_id": ids["release"],
        "mapping_id": ids["mapping"], "object_type_id": ids["type"],
        "asset_id": ids["asset"], "connection_id": ids["connection"],
        "schema_name": "public", "table_name": "inventory",
        "schema_fingerprint": _digest("schema"), "logical_fingerprint": None,
        "field_map": {
            "source_id": "item_id", "balance": "quantity",
            "source_status": "state",
        },
        "primary_key_columns": ["source_id"],
        "contract": {
            "columns": _descriptor("ext")["columns"],
            "object_properties": PROPERTIES,
        },
    }


def test_subscription_resolves_logical_aliases_to_physical_columns(
        db, admin_user):
    # The asset has a logical field config: descriptor columns are named by
    # their alias and carry the physical column in ``source_key``.
    columns = [
        {"name": "sku", "source_key": "SRC_ID", "type": "string",
         "nullable": False},
        {"name": "on_hand", "source_key": "BAL_QTY", "type": "integer",
         "nullable": True},
        {"name": "lifecycle", "source_key": "STS_CD", "type": "string",
         "nullable": True},
    ]
    descriptor = _descriptor(
        "ext", columns=columns, primary_key=["sku"],
        logicalFingerprint=_digest("logical"))
    _release(
        db, admin_user, descriptor=descriptor,
        field_mapping={
            "sku": "item_id", "on_hand": "quantity", "lifecycle": "state"})

    [subscription] = ExternalCdcOntologyAdapter().subscriptions(db)

    assert subscription["field_map"] == {
        "SRC_ID": "item_id", "BAL_QTY": "quantity", "STS_CD": "state"}
    assert subscription["primary_key_columns"] == ["SRC_ID"]
    assert subscription["logical_fingerprint"] == _digest("logical")
    assert subscription["schema_fingerprint"] == _digest("schema")
    # The frozen columns keep their alias ↔ source_key pairing for the sender.
    assert subscription["contract"]["columns"] == columns


def test_subscription_ignores_mapping_targets_that_are_not_properties(
        db, admin_user):
    _release(db, admin_user, field_mapping={
        "source_id": "item_id", "balance": "quantity",
        "source_status": "not_a_property"})

    [subscription] = ExternalCdcOntologyAdapter().subscriptions(db)

    assert subscription["field_map"] == {
        "source_id": "item_id", "balance": "quantity"}


_ALIASED_COLUMNS = [
    {"name": "sku", "source_key": "SRC_ID", "type": "string", "nullable": False},
    {"name": "on_hand", "source_key": "BAL_QTY", "type": "integer"},
    {"name": "lifecycle", "source_key": "STS_CD", "type": "string"},
]


@pytest.mark.parametrize("case", [
    "two_mappings_for_one_type", "two_columns_for_one_property",
    "one_column_addressed_twice", "key_aliases_share_a_physical_column",
])
def test_subscription_rejects_bindings_it_cannot_address_uniquely(
        db, admin_user, case):
    options: dict = {}
    if case == "two_mappings_for_one_type":
        descriptor = _descriptor("ext")
        options["mappings"] = [
            _mapping("ext", descriptor),
            _mapping("ext", descriptor, mapping_id="ext-mapping-2"),
        ]
    elif case == "two_columns_for_one_property":
        options["field_mapping"] = {
            "source_id": "item_id", "balance": "quantity",
            "source_status": "quantity"}
    elif case == "one_column_addressed_twice":
        # Once by its alias and once by its physical name, to different targets.
        options["descriptor"] = _descriptor(
            "ext", columns=_ALIASED_COLUMNS, primary_key=["sku"])
        options["field_mapping"] = {
            "sku": "item_id", "on_hand": "quantity", "BAL_QTY": "state"}
    else:
        options["descriptor"] = _descriptor("ext", columns=[
            {"name": "sku", "source_key": "SRC_ID", "type": "string"},
            {"name": "sku_copy", "source_key": "SRC_ID", "type": "string"},
            {"name": "on_hand", "source_key": "BAL_QTY", "type": "integer"},
        ], primary_key=["sku", "sku_copy"])
        options["field_mapping"] = {"on_hand": "quantity"}
    _release(db, admin_user, **options)

    assert ExternalCdcOntologyAdapter().subscriptions(db) == []


def test_subscriptions_follow_only_a_valid_current_release(db, admin_user):
    ids = _release(db, admin_user)
    project = db.get(OntologyProject, ids["ontology"])
    draft = OntologyVersion(
        id="ext-draft", ontology_id=project.id, version_number="v1.1",
        node_kind="draft", lifecycle_status="editing",
        snapshot_formal=_snapshot("ext"), created_by=admin_user.id)
    db.add(draft)
    db.flush()
    project.current_release_id = draft.id
    db.commit()

    assert ExternalCdcOntologyAdapter().subscriptions(db) == []


# ---------------------------------------------------------------------------
# Capture: caller-owned transaction, scope fence, idempotency, capacity
# ---------------------------------------------------------------------------

def test_capture_persists_only_mapped_facts_and_a_whitelisted_snapshot(
        db, admin_user):
    ids = _release(db, admin_user)
    event = _event(ids, "10")
    event["changes"]["margin"] = {"old": 1, "new": 2}       # declared, unmapped
    event["changes"]["internal_note"] = {"old": "a", "new": "b"}
    event["snapshot"]["properties"]["internal_note"] = "b"
    event["snapshot"]["rawRow"] = {"SECRET": "value"}

    result = ExternalCdcOntologyAdapter().capture(db, event)
    db.commit()

    assert result["status"] == "captured" and result["facts"] == 2
    facts = db.query(SentinelEventLog).order_by(SentinelEventLog.key).all()
    assert [(fact.key, fact.old_value, fact.new_value) for fact in facts] == [
        ("quantity", 100, 0), ("state", "pending", "ready")]
    assert {fact.source for fact in facts} == {"external"}
    assert {fact.ontology_release_id for fact in facts} == {ids["release"]}
    assert facts[0].event_snapshot == {
        "id": event["instance_id"], "objectTypeId": ids["type"],
        "properties": {"item_id": "item-1", "quantity": 0, "state": "ready"},
        "computed": {},
        "source": {
            "assetId": ids["asset"], "mappingId": ids["mapping"],
            "sourceEventId": event["source_event_id"],
            "eventGroupId": event["event_group_id"], "changeKind": "updated",
        },
        "sourcePosition": {"lsn": "10"},
    }
    outbox = db.query(SentinelCdcOutbox).one()
    assert outbox.event_kind == "external_change"
    assert outbox.ontology_release_id == ids["release"]
    assert outbox.changed_keys == ["quantity", "state"]
    assert len(outbox.dedupe_key) <= 255
    assert outbox.result_json["control"] == {
        "kind": "external_change", "assetId": ids["asset"],
        "mappingId": ids["mapping"], "objectTypeId": ids["type"],
        "sourceEventId": event["source_event_id"],
        "eventGroupId": event["event_group_id"],
    }


def test_capture_of_only_unmapped_columns_writes_nothing(db, admin_user):
    ids = _release(db, admin_user)
    event = _event(ids, "11")
    event["changes"] = {"margin": {"old": 1, "new": 2}}

    result = ExternalCdcOntologyAdapter().capture(db, event)
    db.commit()

    assert result == {"status": "captured", "facts": 0, "outbox_id": None}
    assert db.query(SentinelEventLog).count() == 0
    assert db.query(SentinelCdcOutbox).count() == 0


@pytest.mark.parametrize("field,value", [
    ("mapping_id", "another-mapping"),
    ("asset_id", "another-asset"),
    ("object_type_id", "another-type"),
])
def test_capture_outside_the_release_mapping_scope_is_stale(
        db, admin_user, field, value):
    ids = _release(db, admin_user)

    result = ExternalCdcOntologyAdapter().capture(
        db, _event(ids, "12", **{field: value}))
    db.commit()

    assert result["status"] == "stale"
    assert result["reason"] == "mapping_not_in_release"
    assert db.query(SentinelEventLog).count() == 0
    assert db.query(SentinelCdcOutbox).count() == 0


@pytest.mark.parametrize("mutate,message", [
    (lambda event: event.update(changes={"quantity": (100, 0)}), "'old', 'new'"),
    (lambda event: event.update(occurred_at="2026-11-10T08:00:00Z"), "datetime"),
    (lambda event: event.update(source_event_id="x" * 65), "exceeds"),
    (lambda event: event.update(change_kind="update"), "change_kind"),
    (lambda event: event.pop("instance_id"), "instance_id"),
    (lambda event: event["snapshot"].pop("properties"), "properties"),
])
def test_capture_rejects_malformed_events(db, admin_user, mutate, message):
    ids = _release(db, admin_user)
    event = _event(ids, "13")
    mutate(event)

    with pytest.raises(ValueError, match=message):
        ExternalCdcOntologyAdapter().capture(db, event)
    assert db.query(SentinelEventLog).count() == 0


def test_resent_source_event_with_other_keys_keeps_the_first_admission(
        db, admin_user):
    ids = _release(db, admin_user)
    adapter = ExternalCdcOntologyAdapter()
    assert adapter.capture(db, _event(ids, "14"))["status"] == "captured"
    db.commit()
    resent = _event(ids, "14")
    resent["changes"] = {"item_id": {"old": "a", "new": "b"}}

    assert adapter.capture(db, resent)["status"] == "duplicate"
    db.commit()

    assert sorted(
        fact.key for fact in db.query(SentinelEventLog).all()
    ) == ["quantity", "state"]
    assert db.query(SentinelCdcOutbox).count() == 1


def test_replay_after_its_facts_were_pruned_is_a_duplicate_not_a_key_clash(
        db, admin_user):
    """Facts and their trigger row are pruned on different clocks."""
    ids = _release(db, admin_user)
    adapter = ExternalCdcOntologyAdapter()
    event = _event(ids, "15")
    assert adapter.capture(db, event)["status"] == "captured"
    db.commit()
    assert _drain(db)["errors"] == []
    # Retention removed the completed facts; the completed trigger outlives them
    # and still owns the unique dedupe key.
    db.query(SentinelEventLog).delete(synchronize_session=False)
    db.commit()
    trigger_id = db.query(SentinelCdcOutbox).one().id

    result = adapter.capture(db, event)
    db.commit()

    assert result == {
        "status": "duplicate", "facts": 0, "outbox_id": trigger_id}
    assert db.query(SentinelEventLog).count() == 0
    assert db.query(SentinelCdcOutbox).count() == 1
    assert db.query(Notification).count() == 1


def test_full_event_log_refuses_new_events_but_still_acknowledges_replays(
        db, admin_user, monkeypatch):
    ids = _release(db, admin_user)
    adapter = ExternalCdcOntologyAdapter()
    monkeypatch.setattr(cep_contract, "EVENT_LOG_MAX_ROWS", 3)
    admitted = _event(ids, "20")
    assert adapter.capture(db, admitted)["status"] == "captured"
    db.commit()

    refused = adapter.capture(db, _event(ids, "21"))
    db.commit()

    assert refused["status"] == "capacity_exceeded"
    assert refused["error_code"] == "EVENT_CAPACITY_EXCEEDED"
    assert refused["event_log_rows"] == 2 and refused["event_log_max_rows"] == 3
    # Nothing of the refused event was written; the earlier one is intact.
    assert db.query(SentinelEventLog).count() == 2
    assert db.query(SentinelCdcOutbox).count() == 1
    # A replay must stay acknowledgeable while the log is full.
    assert adapter.capture(db, admitted)["status"] == "duplicate"


def test_capacity_check_counts_the_rows_of_the_event_being_admitted(
        db, admin_user, monkeypatch):
    ids = _release(db, admin_user)
    # An empty log still cannot take an event wider than the cap.
    monkeypatch.setattr(cep_contract, "EVENT_LOG_MAX_ROWS", 1)

    result = ExternalCdcOntologyAdapter().capture(db, _event(ids, "22"))

    assert result["status"] == "capacity_exceeded"
    assert db.query(SentinelEventLog).count() == 0


def test_capture_many_locks_every_project_before_reserving_capacity(
        db, admin_user, monkeypatch):
    first = _release(db, admin_user, "zeta")
    second = _release(db, admin_user, "alpha")
    calls: list[tuple[str, str]] = []
    lock, reserve = (
        external_events._locked_current_release_id,
        external_events._capacity_exceeded)
    monkeypatch.setattr(
        external_events, "_locked_current_release_id",
        lambda session, ontology_id: (
            calls.append(("lock", ontology_id)), lock(session, ontology_id))[1])
    monkeypatch.setattr(
        external_events, "_capacity_exceeded",
        lambda session, rows: (
            calls.append(("capacity", "")), reserve(session, rows))[1])

    results = ExternalCdcOntologyAdapter().capture_many(db, [
        _event(first, "30"), _event(second, "31"), _event(first, "32"),
        _event(second, "33", mapping_id="unknown-mapping"),
    ])
    db.commit()

    assert [item["status"] for item in results] == [
        "captured", "captured", "captured", "stale"]
    # Sorted, one lock per project, and all of them before any reservation.
    assert calls[:2] == [
        ("lock", second["ontology"]), ("lock", first["ontology"])]
    assert [kind for kind, _ in calls[2:]] == ["capacity"] * 3
    assert db.query(SentinelCdcOutbox).count() == 3


def test_capture_many_validates_the_whole_batch_before_writing(db, admin_user):
    ids = _release(db, admin_user)
    broken = _event(ids, "41")
    broken["occurred_at"] = None

    with pytest.raises(ValueError):
        ExternalCdcOntologyAdapter().capture_many(
            db, [_event(ids, "40"), broken])

    assert db.query(SentinelEventLog).count() == 0


def test_external_facts_require_an_upsert_capable_dialect():
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(
        dialect=SimpleNamespace(name="mysql")))

    with pytest.raises(NotImplementedError, match="PostgreSQL or SQLite"):
        event_store.record_external_change(
            session, ontology_id="o", release_id="r", object_type_id="t",
            instance_id="remote:1", source_event_id="s", event_group_id="g",
            asset_id="a", mapping_id="m", change_kind="updated",
            field_changes={"quantity": {"old": 1, "new": 2}}, snapshot={})


# ---------------------------------------------------------------------------
# Evaluation: per source event, cursor independent, snapshot only
# ---------------------------------------------------------------------------

def test_late_visible_source_event_below_the_cursor_is_still_evaluated(
        db, admin_user):
    """Event-log ids are reserved before commit, so a slow source can become
    visible beneath a watermark another source already advanced."""
    ids = _release(db, admin_user)
    adapter = ExternalCdcOntologyAdapter()
    # Source A reserves the lower ids but is not yet visible to the worker.
    assert adapter.capture(db, _event(ids, "50", row="item-a"))["status"] == "captured"
    db.commit()
    slow = db.query(SentinelCdcOutbox).one()
    slow_id = slow.id
    slow.status = "cdc_held"
    db.commit()
    # Source B commits first with higher ids and is processed.
    assert adapter.capture(db, _event(ids, "51", row="item-b"))["status"] == "captured"
    db.commit()
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 1
    # The shared watermark is now beyond every existing fact.
    highest = db.query(func.max(SentinelEventLog.id)).scalar()
    cursor = db.query(SentinelPatternCursor).filter(
        SentinelPatternCursor.sentinel_id == ids["sentinel"]).first()
    if cursor is None:
        db.add(SentinelPatternCursor(
            sentinel_id=ids["sentinel"], ontology_id=ids["ontology"],
            event_id=highest))
    else:
        cursor.event_id = highest
    db.commit()

    db.get(SentinelCdcOutbox, slow_id).status = "cdc_pending"
    db.commit()
    assert _drain(db)["errors"] == []

    assert db.query(Notification).count() == 2
    assert db.query(ObjectInstance).count() == 0


def test_shared_cursor_path_never_reads_external_facts(db, admin_user):
    ids = _release(db, admin_user)
    assert ExternalCdcOntologyAdapter().capture(
        db, _event(ids, "60"))["status"] == "captured"
    db.commit()

    assert event_store.events_after_cursor(
        db, ids["ontology"], [ids["type"]], 0) == []
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 1

    # A cursor-driven evaluation afterwards must not fire the event again.
    summary = engine.run_manual(db, ids["ontology"])
    assert summary["errors"] == 0
    assert db.query(Notification).count() == 1


def test_each_source_event_of_one_row_is_evaluated_on_its_own(db, admin_user):
    ids = _release(db, admin_user)
    adapter = ExternalCdcOntologyAdapter()
    # Same row, same timestamp, same event_group_id; only source ids differ.
    for position, quantity in (("70", 0), ("71", 100), ("72", 0)):
        assert adapter.capture(
            db, _event(ids, position, quantity=quantity))["status"] == "captured"
    db.commit()

    assert _drain(db)["errors"] == []

    keys = sorted(
        state.match_key for state in db.query(SentinelMatchState).all())
    instance_id = _event(ids, "70")["instance_id"]
    assert keys == sorted(
        f"pattern:{instance_id}:src:{_event(ids, position)['source_event_id']}"
        for position in ("70", "72"))
    assert db.query(Notification).count() == 2


def test_deleted_row_event_has_no_post_image_to_match(db, admin_user):
    ids = _release(db, admin_user)
    event = _event(ids, "80", change_kind="deleted")

    assert ExternalCdcOntologyAdapter().capture(db, event)["status"] == "captured"
    db.commit()

    fact = db.query(SentinelEventLog).one()
    assert (fact.key, fact.change_kind) == ("__deleted__", "deleted")
    assert _drain(db)["errors"] == []
    assert db.query(Notification).count() == 0


def test_superseded_release_event_is_dropped_at_execution(db, admin_user):
    ids = _release(db, admin_user)
    assert ExternalCdcOntologyAdapter().capture(
        db, _event(ids, "90"))["status"] == "captured"
    db.commit()
    project = db.get(OntologyProject, ids["ontology"])
    db.add(OntologyVersion(
        id="ext-release-2", ontology_id=project.id, version_number="v2",
        node_kind="release", lifecycle_status="released",
        snapshot_formal=_snapshot("ext"), created_by=admin_user.id,
        published_at=datetime.now(timezone.utc)))
    db.flush()
    project.current_release_id = "ext-release-2"
    db.commit()
    db.query(SentinelCdcOutbox).filter(
        SentinelCdcOutbox.event_kind != "external_change",
    ).delete(synchronize_session=False)
    db.commit()

    assert _drain(db)["errors"] == []

    assert db.query(Notification).count() == 0
    outbox = db.query(SentinelCdcOutbox).filter(
        SentinelCdcOutbox.event_kind == "external_change").one()
    assert outbox.status == "completed"
    assert outbox.result_json["superseded"] is True
    # Identity survives finalization as audit evidence.
    assert outbox.result_json["control"]["kind"] == "external_change"


def test_failed_delivery_keeps_the_event_identity_for_its_retry(
        db, admin_user):
    # A webhook rule must never run for a remote row; the gates reject it and
    # execution refuses it again without any side effect.
    ids = _release(db, admin_user, rules=[{
        "id": "rule-hook", "name": "Call out", "type": "webhook",
        "enabled": True, "config": {"url": "https://example.invalid/hook"},
    }])
    event = _event(ids, "100")
    assert ExternalCdcOntologyAdapter().capture(db, event)["status"] == "captured"
    db.commit()

    result = _drain(db)

    assert len(result["errors"]) == 1
    assert db.query(Notification).count() == 0
    firing = db.query(SentinelFiring).filter(
        SentinelFiring.sentinel_id == ids["sentinel"]).one()
    assert firing.status == "error"
    assert firing.action_results[0]["validationErrors"] == [
        "external_action_not_supported"]
    outbox = db.query(SentinelCdcOutbox).one()
    assert outbox.status == "cdc_retry"
    assert outbox.result_json["control"]["sourceEventId"] == (
        event["source_event_id"])
    assert external_events.external_identity_from_row(outbox) == {
        "ontology_id": ids["ontology"], "release_id": ids["release"],
        "asset_id": ids["asset"], "mapping_id": ids["mapping"],
        "object_type_id": ids["type"],
        "source_event_id": event["source_event_id"],
        "event_group_id": event["event_group_id"],
    }


def test_unsupported_remote_definition_does_not_match_or_loop(db, admin_user):
    ids = _release(db, admin_user, pattern=_pattern(
        "ext", aggregate={
            "property": "quantity", "function": "count", "window": 60,
            "threshold": 1, "comparison": "gte"}))
    assert ExternalCdcOntologyAdapter().capture(
        db, _event(ids, "110"))["status"] == "captured"
    db.commit()

    assert _drain(db)["errors"] == []

    assert db.query(Notification).count() == 0
    assert db.query(SentinelCdcOutbox).one().status == "completed"


def test_external_marker_survives_the_match_snapshot_round_trip():
    change = cep_pattern.ChangeSet(
        instance_id="remote:abc", object_type_id="type-1",
        overlay={"quantity": 0}, source_event_id="src-1",
        event_group_id="grp-1", external_asset_id="asset-1",
        external_mapping_id="map-1", release_id="rel-1",
        event_snapshot={"properties": {"quantity": 0, "state": "ready"}})
    instance = cep_pattern._external_instance(change, "ont-1")

    detail = evaluator._snapshot_match_detail(
        {"item": instance}, edge="enter", match_key="k",
        occurred_at=OCCURRED_AT)
    restored = evaluator._tuple_from_detail(None, "ont-1", detail)["item"]

    # An approval resumed later has no local row to read either.
    assert restored.external_event == {
        "sourceEventId": "src-1", "eventGroupId": "grp-1",
        "assetId": "asset-1", "mappingId": "map-1", "changeKind": "updated"}
    assert restored.properties == {"quantity": 0, "state": "ready"}
    assert restored.id == "remote:abc"


def test_organic_release_builtin_pattern_evaluates_through_release_reload(
        db, admin_user):
    """Regression: the immutable-release reload used to drop ``pattern``.

    Every release-builtin ``on_pattern`` Sentinel — remote or organic — went
    through ``_released_builtin_for_execution`` and error-fired
    "pattern 定义非法" on the production entry points (engine passes
    ``expected_release_id``). This exercises the organic, purely local shape:
    local type, organic fact, notification fired from the frozen release.
    """
    ids = _ids("org")
    snapshot = complete_snapshot({
        "objectTypes": [{
            "id": ids["type"], "name": "Inventory", "displayName": "Inventory",
            "primaryKey": "p-item", "properties": PROPERTIES,
        }],
        "actions": [{
            "id": ids["action"], "name": "notify_inventory",
            "displayName": "Inventory notification",
            "objectTypeId": ids["type"], "parameters": [],
            "rules": NOTIFY_RULES,
        }],
        "sentinels": [{
            "id": ids["sentinel"], "name": "inventory_zero",
            "displayName": "Inventory zero",
            "bindings": [{"alias": "item", "objectTypeId": ids["type"]}],
            "links": [], "pattern": _pattern("org"), "condition": None,
            "primaryAlias": "item", "actionIds": [ids["action"]],
            "actionParameters": {}, "triggerMode": "on_pattern",
            "onChange": True, "onSchedule": True, "scanIntervalSeconds": 60,
            "enabled": True, "muted": False, "status": "published",
        }],
        # A local ontology: no remote mapping anywhere.
        "mappings": [],
    })
    project = OntologyProject(
        id=ids["ontology"], name="organic fixture", domain="synthetic",
        created_by=admin_user.id, status="published", version="v1",
    )
    db.add(project)
    db.flush()
    db.add_all([
        ObjectType(
            id=ids["type"], ontology_id=project.id, name="Inventory",
            display_name="Inventory", primary_key="p-item",
            properties=PROPERTIES),
        ActionType(
            id=ids["action"], ontology_id=project.id, name="notify_inventory",
            display_name="Inventory notification", object_type_id=ids["type"],
            parameters=[], rules=NOTIFY_RULES),
        Sentinel(
            id=ids["sentinel"], ontology_id=project.id, name="inventory_zero",
            display_name="Inventory zero",
            bindings=[{"alias": "item", "objectTypeId": ids["type"]}],
            links=[], pattern=_pattern("org"), condition=None,
            primary_alias="item", action_ids=[ids["action"]],
            action_parameters={}, trigger_mode="on_pattern", on_change=True,
            on_schedule=True, scan_interval_seconds=60, enabled=True,
            muted=False, status="published"),
        OntologyVersion(
            id=ids["release"], ontology_id=project.id, version_number="v1",
            node_kind="release", lifecycle_status="released", revision=0,
            snapshot_formal=snapshot, snapshot_hash=snapshot_hash(snapshot),
            published_at=datetime.now(timezone.utc),
            created_by=admin_user.id),
        ObjectInstance(
            id="org-inst-1", ontology_id=project.id,
            object_type_id=ids["type"], ontology_release_id=ids["release"],
            properties={"item_id": "row-1", "quantity": 0, "state": "ready"}),
        SentinelEventLog(
            ontology_id=project.id, ontology_release_id=ids["release"],
            object_type_id=ids["type"], instance_id="org-inst-1",
            change_kind="updated", key="quantity", old_value=100, new_value=0,
            source="organic", occurred_at=datetime.now(timezone.utc)),
    ])
    db.flush()
    project.current_release_id = ids["release"]
    db.commit()
    db.query(SentinelCdcOutbox).delete(synchronize_session=False)
    db.commit()

    summary = engine.run_manual(db, ids["ontology"])

    assert summary["errors"] == 0, summary
    firing = db.query(SentinelFiring).filter(
        SentinelFiring.sentinel_id == ids["sentinel"]).one()
    assert firing.status == "fired"
    assert firing.error is None
    assert db.query(Notification).count() == 1


# ---------------------------------------------------------------------------
# Retention: unfinished external facts are never trimmed for capacity
# ---------------------------------------------------------------------------

def test_overflow_trim_spares_external_facts_whose_trigger_is_unfinished(
        db, admin_user, monkeypatch, caplog):
    ids = _release(db, admin_user)
    adapter = ExternalCdcOntologyAdapter()
    for position in ("120", "121"):
        assert adapter.capture(db, _event(ids, position))["status"] == "captured"
    db.commit()
    assert _drain(db)["errors"] == []          # both triggers completed
    assert adapter.capture(db, _event(ids, "122"))["status"] == "captured"
    db.commit()                                # third trigger still pending
    pending_source = _event(ids, "122")["source_event_id"]
    monkeypatch.setattr(event_store, "EVENT_LOG_MAX_ROWS", 1)

    deleted = event_store.prune_event_log(
        session_factory=sessionmaker(bind=db.get_bind()))

    db.expire_all()
    remaining = db.query(SentinelEventLog).all()
    assert deleted == 4
    assert {fact.source_event_id for fact in remaining} == {pending_source}
    assert len(remaining) == 2                 # still above the cap of 1
    assert "拒绝继续裁剪" in caplog.text


def test_facts_whose_trigger_was_already_pruned_age_out_on_the_platform_clock(
        db, admin_user):
    """A missing trigger can only mean the event completed and was pruned."""
    ids = _release(db, admin_user)
    adapter = ExternalCdcOntologyAdapter()
    for position in ("130", "131"):
        assert adapter.capture(db, _event(ids, position))["status"] == "captured"
    db.commit()
    assert _drain(db)["errors"] == []
    # The outbox pruner has already removed both completed triggers.
    db.query(SentinelCdcOutbox).delete(synchronize_session=False)
    aged_source = _event(ids, "130")["source_event_id"]
    fresh_source = _event(ids, "131")["source_event_id"]
    db.query(SentinelEventLog).filter(
        SentinelEventLog.source_event_id == aged_source,
    ).update(
        {SentinelEventLog.created_at: datetime.now(timezone.utc) - timedelta(
            seconds=cep_contract.EVENT_LOG_RETENTION_SECONDS + 60)},
        synchronize_session=False)
    db.commit()

    deleted = event_store.prune_event_log(
        session_factory=sessionmaker(bind=db.get_bind()))

    db.expire_all()
    assert deleted == 2
    # Under the cap, a young orphan simply waits for its seven days.
    assert {
        fact.source_event_id for fact in db.query(SentinelEventLog).all()
    } == {fresh_source}


def test_outbox_count_cap_then_fact_prune_converges_and_spares_pending(
        db, admin_user, monkeypatch, caplog):
    """The worker's real order: ``prune_completed_outbox`` then ``prune_event_log``.

    A busy installation exceeds the completed-outbox retain limit long before
    its facts are seven days old, so triggers disappear first.  The fact
    pruner must still be able to bring the log back to its cap — otherwise
    external ingest stays refused forever — and it must never touch the event
    that is still waiting to be processed.
    """
    ids = _release(db, admin_user)
    adapter = ExternalCdcOntologyAdapter()
    for position in ("140", "141", "142"):
        assert adapter.capture(db, _event(ids, position))["status"] == "captured"
    db.commit()
    assert _drain(db)["errors"] == []          # three completed triggers
    assert adapter.capture(db, _event(ids, "143"))["status"] == "captured"
    db.commit()                                # the fourth is still pending
    pending_source = _event(ids, "143")["source_event_id"]
    # Finished long enough ago to leave the one-hour fresh-row protection,
    # yet nowhere near the seven-day retention of either table.
    db.query(SentinelCdcOutbox).filter(
        SentinelCdcOutbox.status == "completed",
    ).update(
        {SentinelCdcOutbox.processed_at: (
            datetime.now(timezone.utc) - timedelta(hours=2))},
        synchronize_session=False)
    db.commit()
    monkeypatch.setattr(cdc, "_COMPLETED_RETAIN_LIMIT", 1)
    monkeypatch.setattr(event_store, "EVENT_LOG_MAX_ROWS", 2)
    factory = sessionmaker(bind=db.get_bind())

    removed_triggers = cdc.prune_completed_outbox(session_factory=factory)
    removed_facts = event_store.prune_event_log(session_factory=factory)

    db.expire_all()
    assert removed_triggers == 2               # by count, not by age
    assert sorted(
        row.status for row in db.query(SentinelCdcOutbox).all()
    ) == ["cdc_pending", "completed"]
    remaining = db.query(SentinelEventLog).all()
    assert removed_facts == 6
    assert {fact.source_event_id for fact in remaining} == {pending_source}
    assert len(remaining) == 2                 # back at the cap: converged
    assert "拒绝继续裁剪" not in caplog.text


# ---------------------------------------------------------------------------
# Gates: single-row restrictions for trial / release / dynamic validation
# ---------------------------------------------------------------------------

def _candidate(key: str = "ext", **overrides) -> SimpleNamespace:
    ids = _ids(key)
    values = dict(
        id=ids["sentinel"], name="inventory_zero",
        display_name="Inventory zero",
        bindings=[{"alias": "item", "objectTypeId": ids["type"]}],
        links=[], pattern=_pattern(key), condition=None,
        primary_alias="item", action_ids=[ids["action"]],
        action_parameters={}, trigger_mode="on_pattern", on_change=True,
        on_schedule=True, scan_interval_seconds=60)
    values.update(overrides)
    return SimpleNamespace(**values)


def _action(key: str = "ext", rules=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=_ids(key)["action"], name="notify_inventory",
        display_name="Inventory notification",
        object_type_id=_ids(key)["type"], parameters=[],
        requires_approval=False,
        rules=NOTIFY_RULES if rules is None else rules)


def _codes(errors: list[dict]) -> set[str]:
    return {error["code"] for error in errors}


def test_remote_gate_admits_a_single_row_notification_sentinel():
    assert sentinel_validation.validate_remote_sentinels(
        [_candidate()], [_action()], _snapshot("ext")) == []


def test_remote_gate_ignores_sentinels_on_local_types():
    local = _candidate(
        bindings=[{"alias": "item", "objectTypeId": "local-type"}],
        trigger_mode="on_enter", pattern=None)

    assert sentinel_validation.validate_remote_sentinels(
        [local], [_action()], _snapshot("ext")) == []


_TYPE = _ids("ext")["type"]


@pytest.mark.parametrize("overrides,code", [
    ({"trigger_mode": "on_enter", "pattern": None},
     "sentinel_remote_trigger_mode_unsupported"),
    ({"trigger_mode": "run_on_all", "pattern": None},
     "sentinel_remote_trigger_mode_unsupported"),
    ({"pattern": {
        "stages": [
            {"alias": "item", "objectTypeId": _TYPE, "filter": "item.quantity == 0"},
            {"alias": "later", "objectTypeId": _TYPE, "filter": "later.quantity > 0"},
        ], "absence": {"enabled": False}, "within": 3600},
      "bindings": [{"alias": "item", "objectTypeId": _TYPE},
                   {"alias": "later", "objectTypeId": _TYPE}]},
     "sentinel_remote_pattern_unsupported"),
    ({"pattern": _pattern("ext", aggregate={
        "property": "quantity", "function": "count", "window": 60,
        "threshold": 1, "comparison": "gte"})},
     "sentinel_remote_pattern_unsupported"),
    ({"pattern": _pattern("ext", absence={"enabled": True})},
     "sentinel_remote_pattern_unsupported"),
    ({"links": [{"from": "item", "linkTypeId": "link-1", "to": "item"}]},
     "sentinel_remote_pattern_unsupported"),
    ({"pattern": _pattern(
        "ext", condition="changed_within('item.quantity', 600)")},
     "sentinel_remote_pattern_unsupported"),
    ({"pattern": _pattern("ext", condition="prev('item.quantity') > 0")},
     "sentinel_remote_pattern_unsupported"),
    ({"pattern": _pattern("ext", "item.margin > 1")},
     "sentinel_remote_property_unmapped"),
    ({"pattern": _pattern("ext", "item['margin'] > 1")},
     "sentinel_remote_property_unmapped"),
    ({"pattern": _pattern("ext", condition="item.margin > 1")},
     "sentinel_remote_property_unmapped"),
    ({"action_parameters": {_ids("ext")["action"]: {"note": "{{item.margin}}"}}},
     "sentinel_remote_property_unmapped"),
    ({"action_parameters": {_ids("ext")["action"]: {
        "note": {"source": "property", "alias": "primary", "property": "margin"}}}},
     "sentinel_remote_property_unmapped"),
])
def test_remote_gate_rejects_what_a_single_row_event_cannot_decide(
        overrides, code):
    errors = sentinel_validation.validate_remote_sentinels(
        [_candidate(**overrides)], [_action()], _snapshot("ext"))

    assert _codes(errors) == {code}
    assert all(error["id"] == _ids("ext")["sentinel"] for error in errors)


def test_remote_gate_allows_mapped_parameter_and_identity_bindings():
    candidate = _candidate(action_parameters={_ids("ext")["action"]: {
        "a": "{{item.quantity}} of {{primary.item_id}}",
        "b": "{{item.id}}", "c": "{{event.matchKey}}",
        "d": {"source": "property", "property": "state"},
        "e": {"source": "target_id"},
    }})

    assert sentinel_validation.validate_remote_sentinels(
        [candidate], [_action()], _snapshot("ext")) == []


@pytest.mark.parametrize("rules", [
    [{"type": "webhook", "config": {"url": "https://example.invalid"}}],
    [{"type": "update_property", "config": {"property": "state"}}],
    [{"type": "create_object", "config": {}}],
    [{"type": "delete_row", "config": {}}],
    [{"type": "validation", "config": {"expression": "True"}}],
    [NOTIFY_RULES[0], {"type": "webhook", "config": {}}],
    [{"type": "notification", "config": {
        "channel": "email", "recipientSource": "constant", "recipient": "x"}}],
    [{"type": "notification", "config": {
        "channel": "internal", "recipientSource": "link"}}],
    [{"type": "notification", "enabled": False, "config": {}}],
    [],
])
def test_remote_gate_only_admits_internal_non_link_notifications(rules):
    errors = sentinel_validation.validate_remote_sentinels(
        [_candidate()], [_action(rules=rules)], _snapshot("ext"))

    assert _codes(errors) == {"sentinel_remote_action_unsupported"}
    assert cep_contract.external_action_rule_violation(rules) is not None


@pytest.mark.parametrize("source", ["constant", "parameter", "property"])
def test_remote_gate_admits_same_row_recipient_sources(source):
    rules = [{"type": "notification", "config": {
        "channel": "internal", "recipientSource": source}}]

    assert cep_contract.external_action_rule_violation(rules) is None


def test_remote_gate_reports_a_type_that_cannot_produce_row_events():
    descriptor = _descriptor("ext")
    snapshot = _snapshot("ext", mappings=[
        _mapping("ext", descriptor),
        _mapping("ext", descriptor, mapping_id="ext-mapping-2"),
    ])

    errors = sentinel_validation.validate_remote_sentinels(
        [_candidate()], [_action()], snapshot)

    assert _codes(errors) == {"sentinel_remote_binding_unavailable"}


def test_structural_gate_keeps_its_signature_and_trial_adds_the_remote_rules():
    snapshot = _snapshot("ext")
    candidate = _candidate(pattern=_pattern("ext", "item.margin > 1"))
    object_types = [SimpleNamespace(id=_TYPE, properties=PROPERTIES)]

    structural = sentinel_validation.validate_sentinels(
        [candidate], object_types, [], [_action()])
    trial = trial_service._remote_sentinel_errors(
        snapshot, {"actions": [_action()]}, [candidate])

    # ``margin`` is a declared property, so only the remote gate objects.
    assert "sentinel_remote_property_unmapped" not in _codes(structural)
    assert _codes(trial) == {"sentinel_remote_property_unmapped"}
    # The injected four-argument gate contract is untouched.
    import inspect
    assert list(inspect.signature(
        sentinel_validation.validate_sentinels).parameters) == [
        "sentinels", "object_types", "link_types", "actions"]
