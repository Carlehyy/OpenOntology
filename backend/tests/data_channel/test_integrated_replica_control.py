"""Replica publication fences; source capture ordering has separate real E2E."""
import copy
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated import cdc_control
from app.data_channel.integrated import replica_control as control
from app.data_channel.integrated import replica_runtime as runtime
from app.data_channel.integrated.replica_capture_models import (
    IntegratedReplicaCapture,
)
from app.data_channel.integrated.replica_models import IntegratedReplica, IntegratedReplicaRun


@pytest.fixture
def asset(db, admin_user):
    connection = Connection(id="replica-source", name="replica fixture", kind="postgres", config={})
    dataset = Dataset(id="replica-dataset", name="replica fixture", kind="integrated",
                      source_connection_id=connection.id, source_resource="public.orders")
    row = IntegratedAsset(
        id="replica-asset", dataset_id=dataset.id, connection_id=connection.id,
        name="replica fixture", schema_name="public", table_name="orders",
        schema_json={"columns": [{"name": "id", "type": "integer", "nullable": False}],
                     "primary_key": ["id"]}, schema_fingerprint="schema-one",
        writable=False, created_by=admin_user.id)
    db.add_all([connection, dataset, row])
    db.commit()
    return row


def _loaded(db, asset, *, rows=2):
    run = control.request_rebuild(db, asset)
    token = control.claim_run(db, run.id)
    control.begin_load(db, run.id, token, capture_id=str(uuid.uuid4()),
                       capture_start={"dialect": "postgresql", "lsn": 100})
    control.finish_load(db, run.id, token, completion={
        "complete": True, "row_count": rows, "expected_rows": rows,
        "schema_fingerprint": asset.schema_fingerprint, "snapshot_id": "snapshot-one"})
    return run, token


def _publish(db, run, token, *, rows=2):
    return control.publish_run(
        db, run.id, token, applied_position={"dialect": "postgresql", "commit_lsn": 200},
        coverage_confirmed_at=datetime.now(timezone.utc), row_count=rows)


def test_registration_is_not_a_queryable_replica(db, asset):
    replica = control.ensure_replica(db, asset)
    assert replica.status == "pending"
    assert replica.active_run_id is None
    with pytest.raises(control.ReplicaControlError, match="REPLICA_NOT_READY"):
        control.readable_run(db, asset)
    assert db.get(Dataset, asset.dataset_id).latest_version_id is None


def test_repeated_dispatch_cannot_steal_initialization_owner(db, asset):
    run = control.request_rebuild(db, asset)
    assert control.request_rebuild(db, asset).id == run.id
    token = control.claim_run(db, run.id)
    with pytest.raises(control.ReplicaControlError, match="ALREADY_CLAIMED"):
        control.claim_run(db, run.id)
    with pytest.raises(control.ReplicaControlError, match="OWNER_LOST"):
        control.begin_load(db, run.id, str(uuid.uuid4()), capture_id=str(uuid.uuid4()),
                           capture_start={"lsn": 100})
    assert run.owner_token == token
    assert run.status == "capturing"


def test_incomplete_snapshot_cannot_advance_to_catchup(db, asset):
    run = control.request_rebuild(db, asset)
    token = control.claim_run(db, run.id)
    control.begin_load(db, run.id, token, capture_id=str(uuid.uuid4()), capture_start={"lsn": 100})
    for proof in (
        {"complete": False, "row_count": 2, "expected_rows": 2},
        {"complete": True, "row_count": 1, "expected_rows": 2},
        {"complete": True, "row_count": True, "expected_rows": True},
    ):
        with pytest.raises(control.ReplicaControlError, match="PROOF_INCOMPLETE"):
            control.finish_load(db, run.id, token, completion={
                "schema_fingerprint": asset.schema_fingerprint,
                "snapshot_id": "snapshot-one", **proof})
    assert run.status == "loading"
    assert db.get(IntegratedReplica, asset.id).active_run_id is None


def test_publication_changes_only_current_pointer_and_can_rollback(db, asset):
    run, token = _loaded(db, asset)
    db.commit()
    assert _publish(db, run, token) is None
    assert control.readable_run(db, asset)[1].id == run.id
    assert db.get(Dataset, asset.dataset_id).latest_version_id is None
    db.rollback()
    replica = db.get(IntegratedReplica, asset.id)
    assert replica.active_run_id is None
    assert db.get(IntegratedReplicaRun, run.id).status == "catching_up"


def test_successful_rebuild_retires_previous_without_historical_version(db, asset):
    first, first_token = _loaded(db, asset)
    _publish(db, first, first_token)
    db.commit()
    second, second_token = _loaded(db, asset, rows=3)
    assert control.readable_run(db, asset)[1].id == first.id
    assert _publish(db, second, second_token, rows=3) == first.id
    replica, active = control.readable_run(db, asset)
    assert active.id == second.id
    assert first.status == "retired"
    assert replica.pending_run_id is None
    assert db.get(Dataset, asset.dataset_id).latest_version_id is None


def test_failed_rebuild_keeps_last_complete_replica_queryable_and_stale(db, asset):
    first, token = _loaded(db, asset)
    _publish(db, first, token)
    second = control.request_rebuild(db, asset)
    second_token = control.claim_run(db, second.id)
    control.fail_run(db, second.id, second_token, error_code="SOURCE_UNREACHABLE")
    replica, active = control.readable_run(db, asset)
    assert active.id == first.id
    assert replica.status == "stale"
    assert second.status == "failed"
    assert replica.pending_run_id is None


def test_confirmed_gap_stays_unreadable_during_rebuild(db, asset):
    first, token = _loaded(db, asset)
    _publish(db, first, token)
    control.mark_unavailable(db, asset.id, error_code="SOURCE_LOG_GAP", rebuild_required=True)
    second, token = _loaded(db, asset)
    assert db.get(IntegratedReplica, asset.id).status == "rebuild_required"
    with pytest.raises(control.ReplicaControlError, match="NOT_READY"):
        control.readable_run(db, asset)
    _publish(db, second, token)
    assert control.readable_run(db, asset)[1].id == second.id


def test_expired_owner_and_schema_drift_cannot_publish(db, asset):
    run, token = _loaded(db, asset)
    run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.flush()
    with pytest.raises(control.ReplicaControlError, match="OWNER_LOST"):
        _publish(db, run, token)
    run.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=90)
    asset.schema_fingerprint = "schema-two"
    db.flush()
    with pytest.raises(control.ReplicaControlError, match="SCHEMA_CHANGED"):
        _publish(db, run, token)
    assert db.get(IntegratedReplica, asset.id).active_run_id is None


def test_empty_complete_snapshot_can_publish_after_verified_apply_barrier(db, asset):
    run, token = _loaded(db, asset, rows=0)
    _publish(db, run, token, rows=0)
    _, active = control.readable_run(db, asset)
    assert active.row_count == 0
    assert active.snapshot_proof["complete"] is True


def test_expired_initializer_is_recovered_without_resuming_source_snapshot(db, asset):
    run, token = _loaded(db, asset)
    run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.flush()
    assert control.recover_expired_run(db, run.id)
    assert not control.recover_expired_run(db, run.id)
    assert run.status == "failed"
    assert run.owner_token is None
    assert db.get(IntegratedReplica, asset.id).active_run_id is None
    with pytest.raises(control.ReplicaControlError, match="OWNER_LOST"):
        _publish(db, run, token)
    replacement = control.request_rebuild(db, asset)
    assert replacement.id != run.id
    assert replacement.generation == run.generation + 1


def test_live_initializer_is_not_reclaimed(db, asset):
    run, token = _loaded(db, asset)
    assert not control.recover_expired_run(db, run.id)
    control.renew_run(db, run.id, token)
    assert run.status == "catching_up"
    assert run.owner_token == token


def test_source_outage_preserves_reads_but_not_confirmed_coverage_time(db, asset):
    run, token = _loaded(db, asset)
    _publish(db, run, token)
    replica, _ = control.readable_run(db, asset)
    covered = replica.coverage_confirmed_at
    control.mark_unavailable(db, asset.id, error_code="SOURCE_UNREACHABLE", rebuild_required=False)
    replica, active = control.readable_run(db, asset)
    assert active.id == run.id
    assert replica.status == "stale"
    assert replica.coverage_confirmed_at == covered


def test_request_stop_fences_reads_and_clears_every_run_pointer(db, asset):
    run, token = _loaded(db, asset)
    _publish(db, run, token)
    db.commit()

    replica = control.request_stop(db, asset)
    assert replica.status == "stopped"
    assert replica.active_run_id is None and replica.pending_run_id is None
    stopped = db.get(IntegratedReplicaRun, run.id)
    assert stopped.status == "cancelled"
    assert stopped.owner_token is None and stopped.lease_expires_at is None
    with pytest.raises(control.ReplicaControlError, match="REPLICA_NOT_READY"):
        control.readable_run(db, asset)

    # Idempotent: a repeated stop never resurrects a run or a read pointer.
    assert control.request_stop(db, asset).status == "stopped"
    assert db.get(IntegratedReplicaRun, run.id).status == "cancelled"


def _with_label():
    return {
        "columns": [
            {"name": "id", "type": "integer", "nullable": False},
            {"name": "label", "type": "string", "nullable": True},
        ],
        "primary_key": ["id"],
    }


def test_schema_refresh_supersedes_a_queued_pending_candidate(db, asset):
    first = control.request_rebuild(db, asset)
    replica = db.get(IntegratedReplica, asset.id)
    asset.schema_json = _with_label()
    asset.schema_fingerprint = "schema-two"
    db.flush()

    second = control.request_rebuild(db, asset)
    assert second.id != first.id
    assert first.status == "cancelled"
    assert first.last_error_code == control.SUPERSEDED_RUN_CODE
    assert first.owner_token is None and first.lease_expires_at is None
    assert second.status == "queued"
    assert second.schema_fingerprint == "schema-two"
    assert second.schema_json == _with_label()
    assert replica.generation == first.generation + 1
    assert replica.pending_run_id == second.id


def test_fingerprint_only_refresh_supersedes_a_queued_candidate(db, asset):
    first = control.request_rebuild(db, asset)
    asset.schema_fingerprint = "schema-two"
    db.flush()

    second = control.request_rebuild(db, asset)
    assert second.id != first.id
    assert first.status == "cancelled"
    assert second.schema_json == first.schema_json
    assert second.schema_fingerprint == "schema-two"


def test_schema_refresh_supersedes_only_the_loading_candidate(db, asset):
    active_run, token = _loaded(db, asset)
    _publish(db, active_run, token)
    db.commit()
    connection = db.get(Connection, asset.connection_id)
    capture_id = str(uuid.uuid4())
    capture = IntegratedReplicaCapture(
        id=capture_id, connection_id=connection.id, generation=1,
        identity_fingerprint=cdc_control.source_identity(connection),
        status="streaming", tables_json=[])
    db.add(capture)
    db.flush()

    pending = control.request_rebuild(db, asset)
    pending_token = control.claim_run(db, pending.id)
    control.begin_load(db, pending.id, pending_token, capture_id=capture_id,
                       capture_start={"capture_id": capture_id})
    capture.tables_json = [
        {"asset_id": asset.id, "run_id": active_run.id,
         "schema_name": "public", "table_name": "orders",
         "schema_json": copy.deepcopy(active_run.schema_json),
         "schema_fingerprint": active_run.schema_fingerprint},
        {"asset_id": asset.id, "run_id": pending.id,
         "schema_name": "public", "table_name": "orders",
         "schema_json": copy.deepcopy(pending.schema_json),
         "schema_fingerprint": pending.schema_fingerprint},
    ]
    db.flush()
    before_members = copy.deepcopy(capture.tables_json)
    active_capture_id = active_run.capture_id

    asset.schema_json = _with_label()
    asset.schema_fingerprint = "schema-two"
    db.flush()
    new_run = control.request_rebuild(db, asset)

    assert pending.status == "cancelled"
    assert pending.last_error_code == control.SUPERSEDED_RUN_CODE
    assert pending.owner_token is None and pending.lease_expires_at is None
    assert active_run.status == "active"
    assert active_run.capture_id == active_capture_id
    replica = db.get(IntegratedReplica, asset.id)
    assert replica.active_run_id == active_run.id
    assert replica.pending_run_id == new_run.id
    assert new_run.status == "queued"
    assert new_run.generation == pending.generation + 1
    assert new_run.schema_fingerprint == "schema-two"
    # The capture's other members and lifecycle are untouched; the executor
    # detaches the terminal candidate at its own safe point.
    assert capture.status == "streaming"
    assert capture.tables_json == before_members


def _pending_in_capture(db, asset, *, status, capture_exists=True):
    connection = db.get(Connection, asset.connection_id)
    capture_id = str(uuid.uuid4())
    capture = None
    if capture_exists:
        capture = IntegratedReplicaCapture(
            id=capture_id, connection_id=connection.id, generation=1,
            identity_fingerprint=cdc_control.source_identity(connection),
            status=status, tables_json=[])
        db.add(capture)
        db.flush()
    run = control.request_rebuild(db, asset)
    token = control.claim_run(db, run.id)
    control.begin_load(db, run.id, token, capture_id=capture_id,
                       capture_start={"capture_id": capture_id})
    if capture is not None:
        capture.tables_json = [
            {"asset_id": asset.id, "run_id": run.id,
             "schema_name": "public", "table_name": "orders",
             "schema_json": copy.deepcopy(run.schema_json),
             "schema_fingerprint": run.schema_fingerprint},
        ]
        db.flush()
    return run, capture_id, capture


@pytest.mark.parametrize("status", ["starting", "streaming", "stale"])
def test_explicit_rebuild_keeps_healthy_same_manifest_pending(
        db, asset, status):
    run, capture_id, capture = _pending_in_capture(db, asset, status=status)
    replica = db.get(IntegratedReplica, asset.id)
    generation = replica.generation
    before_members = copy.deepcopy(capture.tables_json)

    again = control.request_rebuild(db, asset)
    assert again.id == run.id
    assert run.status == "loading"
    assert run.capture_id == capture_id
    assert replica.pending_run_id == run.id
    assert replica.generation == generation
    assert capture.tables_json == before_members


@pytest.mark.parametrize("status", ["blocked", "gap", "draining", "stopped"])
def test_explicit_rebuild_supersedes_pending_in_unresumable_capture(
        db, asset, status):
    run, capture_id, capture = _pending_in_capture(db, asset, status=status)
    replica = db.get(IntegratedReplica, asset.id)
    generation = replica.generation
    before_members = copy.deepcopy(capture.tables_json)

    new_run = control.request_rebuild(db, asset)
    assert run.status == "cancelled"
    assert run.last_error_code == control.SUPERSEDED_RUN_CODE
    assert run.owner_token is None and run.lease_expires_at is None
    assert run.capture_id == capture_id  # identity kept for detach/cleanup
    assert new_run.id != run.id
    assert new_run.status == "queued"
    assert new_run.generation == generation + 1
    assert replica.pending_run_id == new_run.id
    # The capture lifecycle and its other members are not rewritten here.
    assert capture.status == status
    assert capture.tables_json == before_members


def test_explicit_rebuild_supersedes_pending_with_missing_capture(db, asset):
    run, capture_id, _ = _pending_in_capture(
        db, asset, status="starting", capture_exists=False)
    new_run = control.request_rebuild(db, asset)
    assert run.status == "cancelled"
    assert run.last_error_code == control.SUPERSEDED_RUN_CODE
    assert run.capture_id == capture_id
    assert new_run.id != run.id
    assert new_run.status == "queued"


def test_explicit_rebuild_supersedes_catching_up_in_gap_capture(db, asset):
    run, _, capture = _pending_in_capture(db, asset, status="gap")
    control.finish_load(db, run.id, run.owner_token, completion={
        "complete": True, "row_count": 0, "expected_rows": 0,
        "schema_fingerprint": run.schema_fingerprint,
        "snapshot_id": capture.id})
    db.flush()
    assert run.status == "catching_up"

    new_run = control.request_rebuild(db, asset)
    assert run.status == "cancelled"
    assert run.last_error_code == control.SUPERSEDED_RUN_CODE
    assert new_run.id != run.id
    assert new_run.status == "queued"


def test_explicit_rebuild_keeps_healthy_catching_up_candidate(db, asset):
    run, _, capture = _pending_in_capture(db, asset, status="streaming")
    control.finish_load(db, run.id, run.owner_token, completion={
        "complete": True, "row_count": 0, "expected_rows": 0,
        "schema_fingerprint": run.schema_fingerprint,
        "snapshot_id": capture.id})
    db.flush()
    assert run.status == "catching_up"
    replica = db.get(IntegratedReplica, asset.id)
    generation = replica.generation

    again = control.request_rebuild(db, asset)
    assert again.id == run.id
    assert run.status == "catching_up"
    assert replica.pending_run_id == run.id
    assert replica.generation == generation


@pytest.mark.parametrize("owner,expired", [(None, False), ("token", True)])
def test_explicit_rebuild_supersedes_expired_pending_owner(
        db, asset, owner, expired):
    run, _, _capture = _pending_in_capture(db, asset, status="streaming")
    run.owner_token = owner
    run.lease_expires_at = (datetime.now(timezone.utc)
                            - timedelta(seconds=1)) if expired else None
    db.flush()

    new_run = control.request_rebuild(db, asset)
    assert run.status == "cancelled"
    assert run.last_error_code == control.SUPERSEDED_RUN_CODE
    assert new_run.id != run.id
    assert new_run.status == "queued"


def test_interrupted_candidate_abandon_queues_a_new_generation_without_autoflush(
        db, asset):
    """Production SessionLocal has autoflush=False.

    abandon_initialization sets the old run failed and pending_run_id None,
    then calls request_rebuild in the same transaction. Without an explicit
    flush, request_rebuild's populate_existing reloads the old loading run and
    returns it; the capture drains/stops and no replacement generation exists.
    """
    sessions = sessionmaker(bind=db.get_bind(), autoflush=False)
    session = sessions()
    try:
        current = session.get(IntegratedAsset, asset.id)
        run, capture_id, _ = _pending_in_capture(
            session, current, status="starting")
        run_id, run_generation = run.id, run.generation
        runtime.abandon_initialization(
            session, capture_id,
            code="INITIAL_SNAPSHOT_INTERRUPTED", retry=True)
        session.commit()
    finally:
        session.close()

    db.expire_all()
    failed = db.get(IntegratedReplicaRun, run_id)
    assert failed.status == "failed"
    assert failed.last_error_code == "INITIAL_SNAPSHOT_INTERRUPTED"
    assert failed.owner_token is None and failed.lease_expires_at is None
    replica = db.get(IntegratedReplica, asset.id)
    assert replica.pending_run_id is not None
    assert replica.pending_run_id != run_id
    replacement = db.get(IntegratedReplicaRun, replica.pending_run_id)
    assert replacement.status == "queued"
    assert replacement.generation == run_generation + 1
    assert replacement.schema_fingerprint == asset.schema_fingerprint
    assert db.get(IntegratedReplicaCapture, capture_id).status == "draining"
