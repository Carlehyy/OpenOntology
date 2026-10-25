"""Transaction-wide CEP merges must not retain each flushed event in Python."""
import gc
import os
import uuid
import weakref
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.ontologies.formal_modeling.models import ObjectInstance, ObjectType
from app.ontologies.projects.models import OntologyProject
from app.ontologies.sentinels import cdc
from app.ontologies.sentinels.cep import event_store
from app.ontologies.sentinels.cep.contract import (
    EVENT_SOURCE_ORGANIC,
    EVENT_SOURCE_RELEASE_ACTIVATION,
)
from app.ontologies.sentinels.models import SentinelEventLog


def _project(db, name):
    project = OntologyProject(
        id=name, name=name, domain="cep-batching", created_by="tests",
        status="published", version="v1.0.0",
    )
    db.add(project)
    db.commit()
    return project


def _capture(db, instance, old, new, *, kind="updated", release="rel-first"):
    event_store.capture_instance_changes(
        db, instance, release_id=release, change_kind=kind,
        changes={"value": (old, new)}, cascade_depth=2, chain_id="first-chain",
    )


def _assert_no_flushed_references(db):
    gc.collect()
    assert db.info[event_store._EVENT_PENDING_KEY]["rows"] == {}
    assert not any(isinstance(row, SentinelEventLog) for row in db.identity_map.values())
    assert not any(isinstance(row, SentinelEventLog) for row in db.new)


@pytest.mark.parametrize("page_count", [2, 12])
def test_cdc_pages_drop_flushed_event_references_and_keep_all_47_keys(
    db, monkeypatch, page_count,
):
    cdc.register_cdc(start_worker=False)
    monkeypatch.setattr(cdc, "AUTO_DISPATCH", False)
    project = _project(db, "cep-pages")
    db.add(ObjectType(
        id="cep-page-type", ontology_id=project.id, name="device",
        display_name="device", primary_key="id", properties=[],
    ))
    db.commit()
    observed = []
    last_refs = []

    def observe_pending(session, _context):
        if session is db:
            rows = [row for row in session.new if isinstance(row, SentinelEventLog)]
            observed.append(len(rows))
            last_refs[:] = [weakref.ref(row) for row in rows]

    sa.event.listen(Session, "after_flush", observe_pending)
    try:
        for page in range(page_count):
            for position in range(16):
                db.add(ObjectInstance(
                    id=f"device-{page:03}-{position:03}", ontology_id=project.id,
                    ontology_release_id="rel-first", object_type_id="cep-page-type",
                    properties={f"field-{key:02}": key for key in range(47)},
                ))
            db.flush()
            _assert_no_flushed_references(db)
            assert all(ref() is None for ref in last_refs)
        assert observed == [16 * 47] * page_count
        # A later page revisits an earlier object, without a Python merge cache.
        first = db.get(ObjectInstance, "device-000-000")
        first.properties = {**first.properties, "field-00": 999, "extra": 1}
        db.flush()
        _assert_no_flushed_references(db)
        db.commit()
    finally:
        sa.event.remove(Session, "after_flush", observe_pending)
    assert event_store._EVENT_PENDING_KEY not in db.info
    assert db.scalar(sa.select(sa.func.count()).select_from(SentinelEventLog)) == page_count * 16 * 47 + 1
    row = db.scalar(sa.select(SentinelEventLog).where(
        SentinelEventLog.instance_id == "device-000-000", SentinelEventLog.key == "field-00",
    ))
    assert (row.old_value, row.new_value, row.change_kind) == (None, 999, "created")


def test_multi_flush_merges_preserve_first_metadata_and_created_priority(db, monkeypatch):
    cdc.register_cdc(start_worker=False)
    monkeypatch.setattr(cdc, "AUTO_DISPATCH", False)
    project = _project(db, "cep-metadata")
    instance = SimpleNamespace(id="same-instance", ontology_id=project.id, object_type_id="device")
    _capture(db, instance, "original", "middle")
    # Both capture calls occur before a flush: a single pending ORM row.
    _capture(db, instance, "middle", "second")
    assert len(db.info[event_store._EVENT_PENDING_KEY]["rows"]) == 1
    db.flush()
    _assert_no_flushed_references(db)
    _capture(db, instance, "second", None, kind="created", release="rel-later")
    db.flush()
    _assert_no_flushed_references(db)
    _capture(db, instance, None, {"nested": [1, None]}, release="rel-last")
    db.flush()
    _assert_no_flushed_references(db)
    db.commit()
    row = db.scalars(sa.select(SentinelEventLog)).one()
    assert row.old_value == "original"
    assert row.new_value == {"nested": [1, None]}
    assert row.change_kind == "created"
    assert row.ontology_release_id == "rel-first"
    assert row.source == EVENT_SOURCE_ORGANIC
    assert (row.cascade_depth, row.chain_id) == (2, "first-chain")


def test_activation_relabels_only_current_transaction_and_target_ontology(db, monkeypatch):
    cdc.register_cdc(start_worker=False)
    monkeypatch.setattr(cdc, "AUTO_DISPATCH", False)
    project = _project(db, "cep-activation-pages")
    other = _project(db, "cep-other-project")
    target = SimpleNamespace(id="old-instance", ontology_id=project.id, object_type_id="device")
    _capture(db, target, "before", "historical")
    db.commit()
    for position in range(40):
        target.id = f"instance-{position}"
        _capture(db, target, "start", "end")
        if position % 8 == 7:
            db.flush()
            _assert_no_flushed_references(db)
    target.id = "pending-instance"
    _capture(db, target, "start", "end")
    _capture(db, SimpleNamespace(id="other-instance", ontology_id=other.id, object_type_id="device"), 0, 1)
    # Exercise the actual CDC pointer-switch hook, not a replacement callback.
    project.current_release_id = "release-activated"
    db.commit()
    rows = db.execute(sa.select(
        SentinelEventLog.instance_id, SentinelEventLog.source,
    )).all()
    assert len(rows) == 43
    assert dict(rows)["old-instance"] == EVENT_SOURCE_ORGANIC
    assert dict(rows)["other-instance"] == EVENT_SOURCE_ORGANIC
    assert all(source == EVENT_SOURCE_RELEASE_ACTIVATION for instance_id, source in rows
               if instance_id not in {"old-instance", "other-instance"})


def test_rollback_and_pooled_connection_reuse_never_merge_previous_transaction(db, monkeypatch):
    cdc.register_cdc(start_worker=False)
    monkeypatch.setattr(cdc, "AUTO_DISPATCH", False)
    project = _project(db, "cep-rollback-anchor")
    target = SimpleNamespace(id="reused-instance", ontology_id=project.id, object_type_id="device")
    _capture(db, target, "a", "committed")
    db.commit()
    _capture(db, target, "committed", "rolled-back")
    db.flush()
    event_store.relabel_pending_as_activation(db, project.id)
    db.rollback()
    event_store.discard_pending(db)
    event_store.discard_pending(db)
    assert event_store._EVENT_PENDING_KEY not in db.info
    _capture(db, target, "committed", "final")
    db.commit()
    # Force the exact pooled connection into a fresh Session as well.
    with Session(db.get_bind()) as reused:
        _capture(reused, target, "final", "another-session")
        reused.commit()
    rows = db.execute(sa.select(
        SentinelEventLog.old_value, SentinelEventLog.new_value, SentinelEventLog.source,
    ).order_by(SentinelEventLog.id)).all()
    assert rows == [("a", "committed", EVENT_SOURCE_ORGANIC),
                    ("committed", "final", EVENT_SOURCE_ORGANIC),
                    ("final", "another-session", EVENT_SOURCE_ORGANIC)]
    assert event_store._ANCHORS.name not in SentinelEventLog.metadata.tables


def test_database_merges_refresh_already_loaded_event_objects(db, monkeypatch):
    cdc.register_cdc(start_worker=False)
    monkeypatch.setattr(cdc, "AUTO_DISPATCH", False)
    project = _project(db, "cep-loaded-events")
    target = SimpleNamespace(id="loaded-instance", ontology_id=project.id, object_type_id="device")
    _capture(db, target, "original", "committed")
    db.commit()
    historical = db.scalars(sa.select(SentinelEventLog)).one()
    _capture(db, target, "committed", "intermediate")
    db.flush()
    current = db.scalar(sa.select(SentinelEventLog).order_by(SentinelEventLog.id.desc()))
    _capture(db, target, "intermediate", None, kind="created")
    assert current.new_value is None
    assert current.change_kind == "created"
    assert current.old_value == "committed"
    event_store.relabel_pending_as_activation(db, project.id)
    assert current.source == EVENT_SOURCE_RELEASE_ACTIVATION
    assert historical.source == EVENT_SOURCE_ORGANIC
    db.rollback()
    assert db.scalar(sa.select(sa.func.count()).select_from(SentinelEventLog)) == 1


@pytest.mark.skipif(os.environ.get("INTEGRATED_NATIVE_PG_E2E") != "1",
                    reason="requires explicitly owned loopback PostgreSQL fixture stack")
def test_real_pg_transaction_anchors_are_bounded_atomic_and_connection_local(monkeypatch):
    """Run only in a sanitized runtime copy; no shared relations are mutated."""
    from app.auth.models import User
    from app.database import Base

    token = uuid.uuid4().hex
    schema = "cep_anchor_" + token
    platform_url = "postgresql+psycopg2://integrated_e2e:integrated_e2e_fixture@127.0.0.1:45432/integrated_metadata"
    root = sa.create_engine(platform_url, hide_parameters=True)
    platform = sa.create_engine(platform_url, hide_parameters=True, pool_size=2, max_overflow=0,
                                connect_args={"options": "-csearch_path=" + schema + " -cstatement_timeout=10000"})
    created = False
    cdc.register_cdc(start_worker=False)
    monkeypatch.setattr(cdc, "AUTO_DISPATCH", False)
    try:
        with root.begin() as conn:
            conn.execute(sa.schema.CreateSchema(schema))
            created = True
        # Private model schema is not a migration/registration acceptance claim.
        Base.metadata.create_all(platform)
        with Session(platform) as db:
            actor = User(id=token, username="cep_" + token, email=token + "@example.test",
                         role="admin", is_active=True, password_hash="not-a-login-fixture")
            db.add(actor)
            db.flush()
            project = OntologyProject(id=token, name="cep fixture", domain="cep", created_by=actor.id)
            db.add(project)
            db.commit()
            for page in range(6):
                for position in range(16):
                    db.add(ObjectInstance(
                        id=f"instance-{page}-{position}", ontology_id=token,
                        ontology_release_id="fixture-release", object_type_id="fixture-type",
                        properties={f"key-{key}": key for key in range(47)},
                    ))
                db.flush()
                _assert_no_flushed_references(db)
            first = db.get(ObjectInstance, "instance-0-0")
            first.properties = {**first.properties, "key-0": 12345}
            db.flush()
            event_store.relabel_pending_as_activation(db, token)
            db.commit()
            assert db.scalar(sa.select(sa.func.count()).select_from(SentinelEventLog)) == 6 * 16 * 47
            row = db.scalar(sa.select(SentinelEventLog).where(
                SentinelEventLog.instance_id == "instance-0-0", SentinelEventLog.key == "key-0"))
            assert (row.old_value, row.new_value, row.change_kind, row.source) == (
                None, 12345, "created", EVENT_SOURCE_RELEASE_ACTIVATION)
            target = SimpleNamespace(id="instance-0-0", ontology_id=token, object_type_id="fixture-type")
            _capture(db, target, 12345, "rolled-back", kind="updated")
            db.flush()
            db.rollback()
            baseline_count = db.scalar(sa.select(sa.func.count()).select_from(SentinelEventLog))
            assert baseline_count == 6 * 16 * 47
            db.rollback()
            # The two simultaneous sessions own different DB connections and
            # identical TEMP table names must not collide or share merge IDs.
            with Session(platform) as other:
                _capture(db, target, 12345, "first-connection")
                db.flush()
                _capture(other, target, 12345, "second-connection")
                other.flush()
                assert db.scalar(sa.select(sa.func.count()).select_from(event_store._ANCHORS)) == 1
                assert other.scalar(sa.select(sa.func.count()).select_from(event_store._ANCHORS)) == 1
                db.commit()
                other.rollback()
            _capture(db, target, "first-connection", "reused-connection")
            db.commit()
            updates = db.execute(sa.select(SentinelEventLog.old_value, SentinelEventLog.new_value).where(
                SentinelEventLog.instance_id == target.id, SentinelEventLog.key == "value",
            ).order_by(SentinelEventLog.id)).all()
            assert updates == [(12345, "first-connection"), ("first-connection", "reused-connection")]
    finally:
        platform.dispose()
        assert schema == "cep_anchor_" + token and uuid.UUID(token).hex == token
        if created:
            with root.begin() as conn:
                conn.execute(sa.schema.DropSchema(schema, cascade=True))
        root.dispose()
