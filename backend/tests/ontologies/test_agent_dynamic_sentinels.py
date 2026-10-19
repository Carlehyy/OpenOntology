"""Governance contract for assistant-created dynamic Sentinels."""
from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy.exc import IntegrityError


def _fo(ontology_id: str) -> str:
    return f"/api/v2/formal/ontologies/{ontology_id}"


@pytest.fixture
def published_runtime(client, auth_headers, ontology, admin_user, db):
    """A tiny immutable release with one actionable current-release object."""
    ontology_id = ontology["id"]
    release_id = ontology["current_release_id"]
    response = client.put(
        f"{_fo(ontology_id)}/full",
        headers=auth_headers,
        json={
            "objectTypes": [{
                "id": "ot-order", "name": "Order", "displayName": "订单",
                "primaryKey": "order_no", "positionX": 0, "positionY": 0,
                "properties": [
                    {"id": "p-order-no", "name": "order_no", "displayName": "订单号",
                     "type": "string", "required": True},
                    {"id": "p-status", "name": "status", "displayName": "状态",
                     "type": "string", "required": False},
                ],
            }],
            "linkTypes": [],
            "actions": [{
                "id": "act-mark-paid", "name": "mark_paid", "displayName": "标记已支付",
                "objectTypeId": "ot-order", "parameters": [], "requiresApproval": False,
                "rules": [{
                    "type": "update_property", "name": "set-status", "enabled": True,
                    "order": 0, "config": {
                        "targetProperty": "status", "valueSource": "constant",
                        "value": "\"paid\"",
                    },
                }],
            }],
            "functions": [],
            "instances": [{
                "id": "inst-order-1", "objectTypeId": "ot-order",
                "properties": {"order_no": "SO-001", "status": "pending"},
                "computed": {},
            }],
            "linkInstances": [],
        },
    )
    assert response.status_code == 200, response.text

    from app.models.ontology import OntologyProject
    from app.models.ontology_formal import ObjectInstance
    from app.models.ontology_version import OntologyVersion
    from app.models.sentinel import Sentinel
    from app.ontologies.versions.evolution_service import snapshot_hash
    from app.ontologies.versions.router import _snapshot_formal

    # A graph-editor Sentinel is deliberately present to prove the two
    # management surfaces cannot see or mutate each other's definitions.
    builtin = Sentinel(
        id="sentinel-builtin", ontology_id=ontology_id,
        name="builtin_watch", display_name="发布内置哨兵",
        bindings=[{"alias": "o", "objectTypeId": "ot-order", "filter": None}],
        links=[], condition="o.status == 'never'", primary_alias="o",
        action_ids=[], action_parameters={}, enabled=False, status="published",
        origin="release_builtin",
    )
    db.add(builtin)
    db.flush()
    snapshot = _snapshot_formal(db, ontology_id)
    release = db.query(OntologyVersion).filter_by(id=release_id).one()
    release.snapshot_formal = snapshot
    release.snapshot_hash = snapshot_hash(snapshot)
    release.published_at = datetime.now(timezone.utc)
    project = db.query(OntologyProject).filter_by(id=ontology_id).one()
    # v0 is already a released snapshot even though this legacy compatibility
    # field remains draft.  All assistant operations must follow release_id.
    project.status = "draft"
    project.version = release.version_number
    instance = db.query(ObjectInstance).filter_by(id="inst-order-1").one()
    instance.ontology_release_id = release_id
    db.commit()

    grant = client.put(
        f"{_fo(ontology_id)}/agent/profile", headers=auth_headers,
        json={"allowedActionIds": ["act-mark-paid"]},
    )
    assert grant.status_code == 200, grant.text
    return {
        "ontology_id": ontology_id,
        "release_id": release_id,
        "user_id": admin_user.id,
    }


def _definition(*, name: str = "assistant_pending_order", actions=True) -> dict:
    return {
        "name": name,
        "displayName": "待支付订单动态哨兵",
        "description": "由智能助手管理",
        "bindings": [{"alias": "o", "objectTypeId": "ot-order", "filter": None}],
        "links": [],
        "condition": "o.status == 'pending'",
        "conditionRows": [],
        "conditionLogic": "and",
        "primaryAlias": "o",
        "actionIds": ["act-mark-paid"] if actions else [],
        "actionParameters": {},
        "onChange": True,
        "onSchedule": False,
        "scanIntervalSeconds": 300,
        "triggerMode": "on_enter",
        "muted": False,
    }


def _create(client, auth_headers, runtime, definition=None):
    response = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels",
        headers=auth_headers,
        json={
            "releaseId": runtime["release_id"],
            "definition": definition or _definition(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_dynamic_trial_has_zero_side_effects_then_enabled_runtime_matches_builtin(
    client, auth_headers, published_runtime, db,
):
    from app.models.ontology_formal import ActionExecutionLog, ObjectInstance, PropertyFact
    from app.models.sentinel import Sentinel, SentinelFiring, SentinelMatchState

    runtime = published_runtime
    row = _create(client, auth_headers, runtime)
    assert row["origin"] == "assistant_dynamic"
    assert row["enabled"] is False
    assert row["createdBy"] == runtime["user_id"]

    before_facts = db.query(PropertyFact).count()
    trial = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/{row['id']}/trial",
        headers=auth_headers, json={"releaseId": runtime["release_id"]},
    )
    assert trial.status_code == 200, trial.text
    trial_row = trial.json()["data"]
    report = trial_row["lastTrialReport"]
    assert report["passed"] is True
    assert report["matchCount"] == 1
    assert report["plannedActionCount"] == 1
    assert report["sideEffects"] == "none"
    assert report["plannedActions"][0]["status"] == "success"
    assert report["plannedActions"][0]["edge"] == "enter"
    assert report["plannedActions"][0]["effects"][0]["type"] == (
        "update_property")
    assert report["plannedActions"][0]["effects"][0]["status"] == "preview"
    assert report["plannedActions"][0]["effects"][0]["committed"] is False
    assert trial_row["canEnable"] is True
    assert db.query(ActionExecutionLog).count() == 0
    assert db.query(SentinelFiring).count() == 0
    assert db.query(SentinelMatchState).count() == 0
    assert db.query(PropertyFact).count() == before_facts
    assert db.query(ObjectInstance).filter_by(id="inst-order-1").one().properties["status"] == "pending"

    enabled = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/{row['id']}/enabled",
        headers=auth_headers,
        json={
            "releaseId": runtime["release_id"],
            "expectedRevision": row["definitionRevision"],
            "enabled": True,
        },
    )
    assert enabled.status_code == 200, enabled.text

    # The mutable Formal definition tables may already contain the next draft.
    # Runtime must still execute the Action frozen in the current release.
    from app.models.ontology_formal import ActionType
    mutable_action = db.query(ActionType).filter_by(
        id="act-mark-paid").one()
    mutable_action.rules = [{
        "id": "draft-only-rule",
        "type": "update_property",
        "name": "未发布草稿动作",
        "enabled": True,
        "order": 0,
        "config": {
            "targetProperty": "status",
            "valueSource": "constant",
            "value": "\"draft-corruption\"",
        },
    }]
    db.commit()

    run = client.post(
        f"/api/v1/ontologies/{runtime['ontology_id']}/sentinels/run",
        headers=auth_headers,
    )
    assert run.status_code == 200, run.text
    assert run.json()["data"]["fired"] == 1
    db.expire_all()
    assert db.query(ObjectInstance).filter_by(id="inst-order-1").one().properties["status"] == "paid"
    firing = db.query(SentinelFiring).filter_by(sentinel_id=row["id"]).one()
    assert firing.ontology_release_id == runtime["release_id"]
    assert db.query(ActionExecutionLog).filter_by(
        ontology_release_id=runtime["release_id"]).count() == 1
    assert db.query(Sentinel).filter_by(id=row["id"]).one().enabled is True


def test_dynamic_enable_transition_captures_one_generation_and_reenable_is_new(
    client, auth_headers, published_runtime, db, monkeypatch,
):
    from sqlalchemy.orm import sessionmaker

    from app.models.sentinel import Sentinel, SentinelCdcOutbox
    from app.ontologies.sentinels import cdc
    from app.services.sentinel import engine as service_engine

    runtime = published_runtime
    monkeypatch.setattr(cdc, "_enqueue_dispatch", lambda _ids: None)
    created = _create(
        client, auth_headers, runtime,
        _definition(name="activation_generation", actions=False),
    )
    trial = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/"
        f"{created['id']}/trial",
        headers=auth_headers,
        json={"releaseId": runtime["release_id"]},
    ).json()["data"]
    endpoint = (
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/"
        f"{created['id']}/enabled"
    )
    command = {
        "releaseId": runtime["release_id"],
        "expectedRevision": trial["definitionRevision"],
        "enabled": True,
    }

    first = client.post(endpoint, headers=auth_headers, json=command)
    repeated = client.post(endpoint, headers=auth_headers, json=command)
    assert first.status_code == repeated.status_code == 200
    assert first.json()["data"]["enableGeneration"] == 1
    assert repeated.json()["data"]["enableGeneration"] == 1
    db.expire_all()
    stored = db.query(Sentinel).filter_by(id=created["id"]).one()
    first_events = db.query(SentinelCdcOutbox).filter_by(
        event_kind=cdc.DYNAMIC_ACTIVATION,
        sentinel_id=stored.id,
    ).all()
    assert stored.enable_generation == 1
    assert len(first_events) == 1
    first_event_id = first_events[0].id
    assert first_events[0].ontology_release_id == runtime["release_id"]
    assert first_events[0].result_json["control"] == {
        "sentinelId": stored.id,
        "definitionRevision": stored.definition_revision,
        "enableGeneration": 1,
    }

    disabled = client.post(
        endpoint,
        headers=auth_headers,
        json={**command, "enabled": False},
    )
    reenabled = client.post(endpoint, headers=auth_headers, json=command)
    assert disabled.status_code == reenabled.status_code == 200
    assert reenabled.json()["data"]["enableGeneration"] == 2
    db.expire_all()
    events = db.query(SentinelCdcOutbox).filter_by(
        event_kind=cdc.DYNAMIC_ACTIVATION,
        sentinel_id=stored.id,
    ).order_by(SentinelCdcOutbox.created_at.asc()).all()
    assert len(events) == 2
    assert events[0].id == first_event_id
    assert events[0].dedupe_key != events[1].dedupe_key
    assert events[1].result_json["control"]["enableGeneration"] == 2

    calls = []

    def initialize(_db, _ontology_id, sentinel_id, **_kwargs):
        calls.append(sentinel_id)
        return {
            "evaluated": 1, "fired": 0, "errors": 0,
            "firings": [], "runtimeErrors": [],
        }

    monkeypatch.setattr(
        service_engine, "run_dynamic_initialization", initialize)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    drained = cdc.drain_cdc_outbox(
        event_ids={event.id for event in events},
        session_factory=factory,
    )
    db.expire_all()
    durable = {
        event.id: event for event in db.query(SentinelCdcOutbox).filter(
            SentinelCdcOutbox.id.in_([item.id for item in events])).all()
    }
    assert drained["processed"] == 2
    assert drained["stale"] == 1
    assert calls == [stored.id]
    assert durable[first_event_id].result_json["skipped"] == (
        "dynamic_sentinel_enable_changed")


def test_dynamic_activation_executes_existing_trial_match_once(
    client, auth_headers, published_runtime, db, monkeypatch,
):
    from sqlalchemy.orm import sessionmaker

    from app.models.ontology_formal import ActionExecutionLog, ObjectInstance
    from app.models.sentinel import SentinelCdcOutbox, SentinelFiring
    from app.ontologies.sentinels import cdc

    runtime = published_runtime
    monkeypatch.setattr(cdc, "_enqueue_dispatch", lambda _ids: None)
    created = _create(
        client, auth_headers, runtime,
        _definition(name="activation_existing_match"),
    )
    trial = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/"
        f"{created['id']}/trial",
        headers=auth_headers,
        json={"releaseId": runtime["release_id"]},
    ).json()["data"]
    enabled = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/"
        f"{created['id']}/enabled",
        headers=auth_headers,
        json={
            "releaseId": runtime["release_id"],
            "expectedRevision": trial["definitionRevision"],
            "enabled": True,
        },
    )
    assert enabled.status_code == 200, enabled.text
    event = db.query(SentinelCdcOutbox).filter_by(
        event_kind=cdc.DYNAMIC_ACTIVATION,
        sentinel_id=created["id"],
    ).one()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)

    first = cdc.drain_cdc_outbox(
        event_ids={event.id}, session_factory=factory)
    replay = cdc.drain_cdc_outbox(
        event_ids={event.id}, session_factory=factory)

    db.expire_all()
    assert first["processed"] == 1
    assert replay["processed"] == 0
    assert db.query(ObjectInstance).filter_by(
        id="inst-order-1").one().properties["status"] == "paid"
    assert db.query(ActionExecutionLog).filter_by(
        action_id="act-mark-paid").count() == 1
    assert db.query(SentinelFiring).filter_by(
        sentinel_id=created["id"]).count() == 1


def test_scheduled_dynamic_sentinel_uses_v0_release_while_project_is_draft(
    client, auth_headers, published_runtime, db,
):
    from app.models.sentinel import (
        Sentinel,
        SentinelCdcOutbox,
        SentinelFiring,
    )
    from app.ontologies.sentinels import cdc
    from app.ontologies.sentinels.engine import run_scheduled

    runtime = published_runtime
    definition = _definition(name="scheduled_v0_release", actions=False)
    definition.update({"onChange": False, "onSchedule": True})
    row = _create(client, auth_headers, runtime, definition)
    trial = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/{row['id']}/trial",
        headers=auth_headers,
        json={"releaseId": runtime["release_id"]},
    ).json()["data"]
    enabled = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/{row['id']}/enabled",
        headers=auth_headers,
        json={
            "releaseId": runtime["release_id"],
            "expectedRevision": trial["definitionRevision"],
            "enabled": True,
        },
    )
    assert enabled.status_code == 200, enabled.text

    result = run_scheduled(db)
    assert result["evaluated"] == 1
    firing = db.query(SentinelFiring).filter_by(sentinel_id=row["id"]).one()
    assert firing.ontology_release_id == runtime["release_id"]
    live = db.query(Sentinel).filter_by(id=row["id"]).one()
    scheduled = db.query(SentinelCdcOutbox).filter_by(
        event_kind=cdc.SCHEDULED_SCAN,
        sentinel_id=row["id"],
    ).one()
    assert scheduled.result_json["control"]["sentinelOrigin"] == (
        "assistant_dynamic")
    assert scheduled.result_json["control"]["definitionRevision"] == (
        live.definition_revision)
    assert scheduled.result_json["control"]["enableGeneration"] == (
        live.enable_generation)


def test_management_surfaces_are_origin_isolated_and_validation_fails_closed(
    client, auth_headers, published_runtime,
):
    runtime = published_runtime
    dynamic = _create(client, auth_headers, runtime, _definition(actions=False))

    editor_list = client.get(
        f"/api/v1/ontologies/{runtime['ontology_id']}/sentinels/",
        headers=auth_headers,
    )
    assert [item["id"] for item in editor_list.json()["data"]] == ["sentinel-builtin"]
    assert editor_list.json()["data"][0]["origin"] == "release_builtin"

    assistant_list = client.get(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels",
        headers=auth_headers, params={"release_id": runtime["release_id"]},
    )
    assert [item["id"] for item in assistant_list.json()["data"]] == [dynamic["id"]]

    # The immutable release workspace remains the built-in source of truth.
    # Read-only consumers may merge this with the dynamic endpoint for display,
    # but assistant overlays must never leak into the version snapshot itself.
    release_workspace = client.get(
        f"/api/v2/ontologies/{runtime['ontology_id']}/current-release/workspace",
        headers=auth_headers,
    )
    assert release_workspace.status_code == 200, release_workspace.text
    assert [item["id"] for item in release_workspace.json()["data"]["sentinels"]] == [
        "sentinel-builtin",
    ]
    assert client.get(
        f"/api/v1/ontologies/{runtime['ontology_id']}/sentinels/{dynamic['id']}",
        headers=auth_headers,
    ).status_code == 404
    assistant_cannot_update_builtin = client.put(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/sentinel-builtin",
        headers=auth_headers,
        json={
            "releaseId": runtime["release_id"], "expectedRevision": 1,
            "definition": _definition(name="cannot_touch_builtin", actions=False),
        },
    )
    assert assistant_cannot_update_builtin.status_code == 404

    invalid_type = _definition(name="invalid_type", actions=False)
    invalid_type["bindings"][0]["objectTypeId"] = "invented-type"
    invalid_response = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels",
        headers=auth_headers,
        json={"releaseId": runtime["release_id"], "definition": invalid_type},
    )
    assert invalid_response.status_code == 422
    assert "不存在或不在授权范围" in invalid_response.text

    unsafe_expression = _definition(name="unsafe_expression", actions=False)
    unsafe_expression["condition"] = "__import__('os').system('false')"
    unsafe_response = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels",
        headers=auth_headers,
        json={"releaseId": runtime["release_id"], "definition": unsafe_expression},
    )
    assert unsafe_response.status_code == 422
    assert "condition" in unsafe_response.text

    missing_property = _definition(name="missing_property", actions=False)
    missing_property["condition"] = "o.invented_property == 'x'"
    missing_property_response = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels",
        headers=auth_headers,
        json={"releaseId": runtime["release_id"], "definition": missing_property},
    )
    assert missing_property_response.status_code == 422
    assert "发布版本中不存在的属性" in missing_property_response.text

    ambiguous_rows = _definition(name="ambiguous_rows", actions=False)
    ambiguous_rows["conditionRows"] = [{"leftAlias": "o", "leftProp": "status"}]
    ambiguous_rows_response = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels",
        headers=auth_headers,
        json={"releaseId": runtime["release_id"], "definition": ambiguous_rows},
    )
    assert ambiguous_rows_response.status_code == 422
    assert "conditionRows 必须为空" in ambiguous_rows_response.text

    coerced_boolean = _definition(name="coerced_boolean", actions=False)
    coerced_boolean["onChange"] = "false"
    coerced_boolean_response = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels",
        headers=auth_headers,
        json={"releaseId": runtime["release_id"], "definition": coerced_boolean},
    )
    assert coerced_boolean_response.status_code == 422

    unknown_field = _definition(name="unknown_field", actions=False)
    unknown_field["origin"] = "release_builtin"
    unknown_field_response = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels",
        headers=auth_headers,
        json={"releaseId": runtime["release_id"], "definition": unknown_field},
    )
    assert unknown_field_response.status_code == 422

    duplicate = _definition(name="builtin_watch", actions=False)
    duplicate_response = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels",
        headers=auth_headers,
        json={"releaseId": runtime["release_id"], "definition": duplicate},
    )
    assert duplicate_response.status_code == 422
    assert "已存在" in duplicate_response.text


def test_current_trial_is_required_and_release_switch_disables_before_execution(
    client, auth_headers, published_runtime, admin_user, db,
):
    from app.models.ontology import OntologyProject
    from app.models.ontology_formal import ObjectInstance
    from app.models.ontology_version import OntologyVersion
    from app.models.sentinel import Sentinel
    from app.ontologies.versions.evolution_service import snapshot_hash

    runtime = published_runtime
    row = _create(
        client, auth_headers, runtime,
        _definition(name="release_guard", actions=False),
    )
    no_trial = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/{row['id']}/enabled",
        headers=auth_headers,
        json={
            "releaseId": runtime["release_id"],
            "expectedRevision": row["definitionRevision"], "enabled": True,
        },
    )
    assert no_trial.status_code == 409
    assert "全量试跑" in no_trial.text

    trial = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/{row['id']}/trial",
        headers=auth_headers, json={"releaseId": runtime["release_id"]},
    ).json()["data"]
    enable = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/{row['id']}/enabled",
        headers=auth_headers,
        json={
            "releaseId": runtime["release_id"],
            "expectedRevision": trial["definitionRevision"], "enabled": True,
        },
    )
    assert enable.status_code == 200, enable.text

    project = db.query(OntologyProject).filter_by(id=runtime["ontology_id"]).one()
    old_release = project.current_release_id
    old_snapshot = db.query(OntologyVersion).filter_by(id=old_release).one().snapshot_formal
    new_release_id = str(uuid.uuid4())
    new_release = OntologyVersion(
        id=new_release_id, ontology_id=runtime["ontology_id"],
        version_number="v1", version_label="test release switch",
        parent_version_id=old_release, node_kind="release", lifecycle_status="released",
        revision=0, snapshot_formal=old_snapshot, snapshot_hash=snapshot_hash(old_snapshot),
        published_at=datetime.now(timezone.utc), created_by=admin_user.id,
    )
    db.add(new_release)
    db.flush()
    project.current_release_id = new_release_id
    project.version = "v1"
    db.query(ObjectInstance).filter_by(id="inst-order-1").one().ontology_release_id = new_release_id
    db.commit()

    # The execution entry point itself performs reconciliation; opening the
    # assistant drawer is not required for the safety transition.
    run = client.post(
        f"/api/v1/ontologies/{runtime['ontology_id']}/sentinels/run",
        headers=auth_headers,
    )
    assert run.status_code == 200, run.text
    db.expire_all()
    stored = db.query(Sentinel).filter_by(id=row["id"]).one()
    assert stored.enabled is False
    assert stored.bound_release_id == new_release_id
    assert stored.last_trial_report is None
    assert stored.validation_report["compatibility"] == "review_required"


def test_database_rejects_unknown_sentinel_origin(published_runtime, db):
    from app.models.sentinel import Sentinel

    db.add(Sentinel(
        ontology_id=published_runtime["ontology_id"],
        name="bad_origin", display_name="bad origin",
        bindings=[{"alias": "o", "objectTypeId": "ot-order"}],
        links=[], primary_alias="o", action_ids=[],
        origin="forged_external_source",
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_conversation_tool_only_proposes_then_user_confirmation_creates(
    client, auth_headers, published_runtime, db,
):
    from app.models.sentinel import Sentinel
    from app.ontologies.agent_runtime.boundary import build_scope
    from app.ontologies.agent_runtime.toolkit import ToolRunner

    runtime = published_runtime
    _, _, scope = build_scope(
        db, runtime["ontology_id"], release_id=runtime["release_id"])
    runner = ToolRunner(db, scope)
    result = runner.run("propose_dynamic_sentinel_change", {
        "operation": "create",
        "definition": {
            "name": "chat_created_monitor",
            "displayName": "对话创建的监测规则",
            "bindings": [{"alias": "o", "objectType": "订单"}],
            "links": [],
            "condition": "o.status == 'pending'",
            "primaryAlias": "o",
            "actions": [],
            "onChange": True,
            "onSchedule": False,
        },
    })
    proposal = result["proposal"]
    assert proposal["kind"] == "sentinel"
    assert proposal["status"] == "success"
    assert proposal["definition"]["bindings"][0]["objectTypeId"] == "ot-order"
    assert db.query(Sentinel).filter_by(origin="assistant_dynamic").count() == 0

    confirmed = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels/execute-proposal",
        headers=auth_headers,
        json={
            "operation": proposal["operation"],
            "releaseId": proposal["releaseId"],
            "sentinelId": proposal["sentinelId"],
            "expectedRevision": proposal["expectedRevision"],
            "definition": proposal["definition"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    created = confirmed.json()["data"]
    assert created["origin"] == "assistant_dynamic"
    assert created["enabled"] is False
    assert db.query(Sentinel).filter_by(origin="assistant_dynamic").count() == 1


def test_assistant_schema_is_materialized_from_exact_release_snapshot(
    client, auth_headers, published_runtime, db,
):
    """Mutable projection drift must never change the assistant's release view."""
    from app.models.ontology_formal import ActionType, ObjectType
    from app.ontologies.agent_runtime.boundary import ToolError, build_scope

    runtime = published_runtime
    object_type = db.query(ObjectType).filter_by(id="ot-order").one()
    object_type.name = "OrderDraftDrift"
    object_type.display_name = "未发布草稿订单"
    object_type.properties = [{
        "id": "p-draft-only", "name": "draft_only",
        "displayName": "草稿专用字段", "type": "string", "required": False,
    }]
    action = db.query(ActionType).filter_by(id="act-mark-paid").one()
    action.name = "draft_action_drift"
    action.display_name = "未发布草稿动作"
    db.commit()

    response = client.get(
        f"{_fo(runtime['ontology_id'])}/agent/capabilities",
        headers=auth_headers, params={"release_id": runtime["release_id"]},
    )
    assert response.status_code == 200, response.text
    capabilities = response.json()["data"]
    assert capabilities["releaseId"] == runtime["release_id"]
    assert capabilities["objectTypes"][0]["displayName"] == "订单"
    assert capabilities["actions"][0]["displayName"] == "标记已支付"
    assert "订单号" in capabilities["skillCard"]
    assert "草稿专用字段" not in capabilities["skillCard"]
    assert "未发布草稿" not in capabilities["skillCard"]

    _, _, scope = build_scope(
        db, runtime["ontology_id"], release_id=runtime["release_id"])
    assert scope.require_object_type("订单").id == "ot-order"
    assert scope.require_action("标记已支付").id == "act-mark-paid"
    with pytest.raises(ToolError):
        scope.require_object_type("未发布草稿订单")


def test_action_entry_points_execute_the_exact_release_after_live_rule_drift(
    client, auth_headers, published_runtime, db,
):
    """Direct, confirmed and assistant-preview paths must share one release."""
    from app.models.ontology_formal import ActionType, ObjectInstance
    from app.ontologies.agent_runtime.boundary import build_scope
    from app.ontologies.agent_runtime.toolkit import ToolRunner

    runtime = published_runtime
    mutable_action = db.query(ActionType).filter_by(
        id="act-mark-paid").one()
    mutable_action.rules = [{
        "id": "draft-only-rule",
        "type": "update_property",
        "name": "未发布草稿动作",
        "enabled": True,
        "order": 0,
        "config": {
            "targetProperty": "status",
            "valueSource": "constant",
            "value": "\"draft-corruption\"",
        },
    }]
    db.commit()

    _, _, scope = build_scope(
        db, runtime["ontology_id"], release_id=runtime["release_id"])
    proposal = ToolRunner(db, scope).run("propose_action", {
        "action": "act-mark-paid",
        "target_instance_id": "inst-order-1",
        "parameters": {},
    })["proposal"]
    assert proposal["status"] == "success"
    assert proposal["releaseId"] == runtime["release_id"]
    assert proposal["effects"][0]["newValue"] == "paid"

    direct = client.post(
        f"{_fo(runtime['ontology_id'])}/run-action",
        headers=auth_headers,
        json={
            "releaseId": runtime["release_id"],
            "actionId": "act-mark-paid",
            "targetInstanceId": "inst-order-1",
            "dryRun": False,
        },
    )
    assert direct.status_code == 200, direct.text
    assert direct.json()["data"]["status"] == "success"
    db.expire_all()
    instance = db.query(ObjectInstance).filter_by(id="inst-order-1").one()
    assert instance.properties["status"] == "paid"

    instance.properties = {
        **dict(instance.properties or {}),
        "status": "pending",
    }
    db.commit()
    # YIYA-36 确认协议：用户确认执行经持久 confirmationId 完成
    conv = _confirmation_conversation(db, runtime, auth_headers)
    prepared = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/proposals",
        headers=auth_headers,
        json={
            "sessionKind": "ontology_agent", "sessionId": conv.id,
            "clientSubmissionId": "prop-confirm-1",
            "releaseId": runtime["release_id"],
            "actionId": "act-mark-paid",
            "targetInstanceId": "inst-order-1",
            "parameters": {},
        })
    assert prepared.status_code == 201, prepared.text
    cid = prepared.json()["data"]["confirmationId"]
    decided = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/proposals/{cid}/decision",
        headers=auth_headers,
        json={"decision": "approve", "sessionKind": "ontology_agent",
              "sessionId": conv.id, "decisionId": "d1",
              "payloadHash": prepared.json()["data"]["payloadHash"]})
    assert decided.status_code == 200, decided.text
    confirmed = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/execute-proposal",
        headers=auth_headers,
        json={"confirmationId": cid, "sessionKind": "ontology_agent",
              "sessionId": conv.id})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["state"] == "succeeded"
    db.expire_all()
    assert db.query(ObjectInstance).filter_by(
        id="inst-order-1").one().properties["status"] == "paid"


def test_confirmed_assistant_action_cannot_target_another_release(
    client, auth_headers, published_runtime, db,
):
    from app.models.ontology_formal import ObjectInstance

    runtime = published_runtime
    db.add(ObjectInstance(
        id="inst-stale-release", ontology_id=runtime["ontology_id"],
        ontology_release_id="older-release", object_type_id="ot-order",
        properties={"order_no": "SO-OLD", "status": "pending"}, computed={},
    ))
    db.commit()

    # YIYA-36 确认协议：跨 release 目标在 prepare 即被拒（422），不变更业务数据
    conv = _confirmation_conversation(db, runtime, auth_headers)
    response = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/proposals",
        headers=auth_headers,
        json={
            "sessionKind": "ontology_agent", "sessionId": conv.id,
            "clientSubmissionId": "prop-stale-1",
            "releaseId": runtime["release_id"],
            "actionId": "act-mark-paid",
            "targetInstanceId": "inst-stale-release",
            "parameters": {},
        })
    assert response.status_code == 422
    assert "不属于当前发布版本" in response.text
    db.expire_all()
    assert db.query(ObjectInstance).filter_by(
        id="inst-stale-release").one().properties["status"] == "pending"


def _confirmation_conversation(db, runtime, auth_headers):
    """为确认协议测试建立带提案消息的本人会话。"""
    import uuid

    from app.models.user import User
    from app.ontologies.agent_runtime.models import (
        AgentConversation, AgentMessage,
    )

    user = db.query(User).filter_by(role="admin").first()
    conv = AgentConversation(id=f"conv-{uuid.uuid4().hex[:8]}",
                             ontology_id=runtime["ontology_id"],
                             user_id=user.id, title="t")
    db.add(conv)
    db.flush()
    db.add(AgentMessage(id=f"mu-{uuid.uuid4().hex[:6]}",
                        conversation_id=conv.id, role="user", content="go"))
    for submission in ("prop-confirm-1", "prop-stale-1"):
        db.add(AgentMessage(
            id=f"ma-{uuid.uuid4().hex[:6]}", conversation_id=conv.id,
            role="assistant", content="prepared",
            proposals=[{"kind": "action", "proposalId": submission,
                        "actionId": "act-mark-paid", "parameters": {},
                        "targetInstanceId": "inst-order-1",
                        "releaseId": runtime["release_id"],
                        "status": "success", "validationErrors": [],
                        "effects": []}]))
    db.commit()
    return conv
