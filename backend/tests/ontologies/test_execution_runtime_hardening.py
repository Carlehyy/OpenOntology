"""Production guardrails for formal actions, sentinels, and Celery workers."""
import json
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import yaml
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.ontology_formal import (
    ActionExecutionLog,
    ActionType,
    LinkInstance,
    LinkType,
    ObjectInstance,
    ObjectType,
    OntologyFunction,
    PropertyFact,
)
from app.models.sentinel import (
    Notification,
    Sentinel,
    SentinelFiring,
    SentinelMatchState,
)
from app.models.ontology import OntologyProject
from app.models.ontology_version import OntologyVersion
from app.models.v2.mapping import OntologyMapping
from app.api_hub import outbound_security
from app.services.formal.action_engine import (
    execute_action,
    validate_action_definition,
)
from app.ontologies.formal_modeling.webhook_dispatcher import (
    WebhookDispatchError,
    preview_webhook,
)
from app.ontologies.formal_modeling.facts import record_decision_fact
from app.ontologies.formal_modeling import router as formal_router
from app.ontologies.sentinels.evaluator import _sentinel_execution_lock
from app.ontologies.sentinels import evaluator as sentinel_evaluator
from app.services.sentinel.evaluator import (
    evaluate_sentinel,
    reject_sentinel_match_claim,
    resume_sentinel_match_claim,
)
from app.ontologies.formal_modeling.router import decide_pending_action, list_pending_actions
from app.ontologies.formal_modeling.action_validation import (
    action_parameter_hints,
    prepare_action_parameters,
)
from app.ontologies.formal_modeling.schemas import ActionTypeCreate, DecisionRequest
from app.ontologies.versions.evolution_service import snapshot_hash
from app.ontologies.versions.router import _snapshot_formal
from fastapi import HTTPException


def test_action_and_sentinel_public_entrypoints_preserve_global_lock_order(
        monkeypatch):
    from app.ontologies.mappings import mapping_service
    from app.ontologies.formal_modeling import action_engine

    events: list[str] = []
    held = {"build": False, "sentinel": False}

    @contextmanager
    def build_lock(_db, ontology_id):
        assert ontology_id == "lock-order-ontology"
        assert held["sentinel"] is False
        held["build"] = True
        events.append("build-enter")
        try:
            yield
        finally:
            events.append("build-exit")
            held["build"] = False

    @contextmanager
    def sentinel_lock(_db, sentinel_id):
        assert sentinel_id == "lock-order-sentinel"
        assert held["build"] is True
        held["sentinel"] = True
        events.append("sentinel-enter")
        try:
            yield
        finally:
            events.append("sentinel-exit")
            held["sentinel"] = False

    monkeypatch.setattr(mapping_service, "_ontology_build_lock", build_lock)
    monkeypatch.setattr(
        sentinel_evaluator, "_sentinel_execution_lock", sentinel_lock)
    monkeypatch.setattr(
        sentinel_evaluator,
        "runtime_release_identity",
        lambda *_args, **_kwargs: SimpleNamespace(id="release-r1", version="v1"),
    )
    monkeypatch.setattr(
        sentinel_evaluator,
        "_reload_executable_sentinel",
        lambda _db, _ontology_id, sentinel, _release_id: sentinel,
    )
    monkeypatch.setattr(
        sentinel_evaluator,
        "_guard_expected_release",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        sentinel_evaluator,
        "_evaluate_inner",
        lambda *_args, **_kwargs: "evaluated",
    )
    sentinel = SimpleNamespace(
        id="lock-order-sentinel",
        name="lock_order",
        display_name="锁序",
    )
    result = evaluate_sentinel(
        SimpleNamespace(),
        "lock-order-ontology",
        sentinel,
        "manual",
        expected_release_id="release-r1",
    )
    assert result == "evaluated"
    assert events == [
        "build-enter", "sentinel-enter", "sentinel-exit", "build-exit",
    ]

    events.clear()
    monkeypatch.setattr(
        action_engine,
        "_execute_action_locked",
        lambda *_args, **_kwargs: {"status": "success"},
    )
    assert action_engine.execute_action(
        None,
        "isolated-trial",
        SimpleNamespace(preview_only=True),
        preview_only=True,
        preview_context={"isolated": True},
    ) == {"status": "success"}
    assert events == []

    assert action_engine.execute_action(
        SimpleNamespace(),
        "lock-order-ontology",
        SimpleNamespace(),
    ) == {"status": "success"}
    assert events == ["build-enter", "build-exit"]

    events.clear()
    monkeypatch.setattr(
        sentinel_evaluator,
        "_resume_sentinel_match_claim_locked",
        lambda *_args, **_kwargs: {"status": "fired"},
    )
    assert resume_sentinel_match_claim(
        SimpleNamespace(),
        "lock-order-ontology",
        "state-1",
    ) == {"status": "fired"}
    assert events == ["build-enter", "build-exit"]


def test_postgres_sentinel_lock_uses_one_dedicated_connection_across_commits():
    calls = []

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def execute(self, statement, params):
            calls.append((self, str(statement), params))

        def close(self):
            self.closed = True

    connection = FakeConnection()

    class FakeEngine:
        dialect = SimpleNamespace(name="postgresql")

        def connect(self):
            return connection

    class FakeSession:
        commits = 0

        def get_bind(self):
            return FakeEngine()

        def execute(self, *_args, **_kwargs):
            raise AssertionError("advisory lock must not use the business Session")

        def commit(self):
            self.commits += 1

    session = FakeSession()
    with _sentinel_execution_lock(session, "dedicated-lock"):
        # This represents action-engine commits that can release/swap the
        # business Session's pooled connection while evaluation is in flight.
        session.commit()
        session.commit()

    assert session.commits == 2
    assert len(calls) == 2
    assert calls[0][0] is connection and calls[1][0] is connection
    assert "pg_advisory_lock" in calls[0][1]
    assert "pg_advisory_unlock" in calls[1][1]
    assert calls[0][2] == calls[1][2] == {"key": "sentinel:dedicated-lock"}
    assert connection.closed is True


def test_release_scoped_tuple_contract_uses_snapshot_link_definition(db):
    """Draft LinkType edits cannot alter published Sentinel traversal."""
    ontology_id = "sentinel-link-snapshot-contract"
    release_id = "sentinel-link-release"
    project = OntologyProject(
        id=ontology_id, name=ontology_id, domain="test",
        created_by="runtime-tests", status="published", version="v1",
        current_release_id=release_id,
    )
    release = OntologyVersion(
        id=release_id, ontology_id=ontology_id,
        version_number="v1", node_kind="release",
        lifecycle_status="released", revision=0,
        snapshot_formal={
            "objectTypes": [],
            "linkTypes": [{
                "id": "published-link",
                "name": "published_link",
                "displayName": "Published link",
                "sourceObjectTypeId": "type-a",
                "targetObjectTypeId": "type-b",
                "cardinality": "many-to-many",
                "properties": [],
            }],
            "actions": [], "functions": [], "sentinels": [],
            "mappings": [], "linkMappings": [],
        },
        created_by="runtime-tests",
    )
    type_a = ObjectType(
        id="type-a", ontology_id=ontology_id,
        name="TypeA", display_name="Type A",
        primary_key="id", properties=[],
    )
    type_b = ObjectType(
        id="type-b", ontology_id=ontology_id,
        name="TypeB", display_name="Type B",
        primary_key="id", properties=[],
    )
    # The live row represents a newer draft and deliberately contradicts the
    # immutable release snapshot.
    draft_link = LinkType(
        id="published-link", ontology_id=ontology_id,
        name="draft_link", display_name="Draft link",
        source_object_type_id="type-b",
        target_object_type_id="type-a",
        cardinality="many-to-many", properties=[],
    )
    a = ObjectInstance(
        id="snapshot-a", ontology_id=ontology_id,
        ontology_release_id=release_id,
        object_type_id=type_a.id, properties={},
    )
    b = ObjectInstance(
        id="snapshot-b", ontology_id=ontology_id,
        ontology_release_id=release_id,
        object_type_id=type_b.id, properties={},
    )
    edge = LinkInstance(
        id="snapshot-edge", ontology_id=ontology_id,
        ontology_release_id=release_id,
        link_type_id=draft_link.id,
        source_object_id=a.id, target_object_id=b.id, properties={},
    )
    sentinel = Sentinel(
        id="snapshot-link-sentinel", ontology_id=ontology_id,
        name="snapshot_link_sentinel",
        display_name="Snapshot link sentinel",
        bindings=[
            {"alias": "a", "objectTypeId": type_a.id},
            {"alias": "b", "objectTypeId": type_b.id},
        ],
        links=[{
            "from": "a", "to": "b", "linkTypeId": draft_link.id,
        }],
        condition="True", primary_alias="a", action_ids=[],
        action_parameters={}, trigger_mode="on_enter",
        on_change=False, on_schedule=False, muted=False,
        enabled=True, status="published",
    )
    db.add_all([
        project, release, type_a, type_b, draft_link, a, b, edge, sentinel,
    ])
    db.commit()

    errors: list[str] = []
    tuples = sentinel_evaluator._resolve_tuples(
        db, ontology_id, sentinel, errors, release_id=release_id)

    assert errors == []
    assert len(tuples) == 1
    assert tuples[0]["a"].id == a.id
    assert tuples[0]["b"].id == b.id


def test_release_fence_error_keeps_captured_release_lineage(db):
    ontology_id = "sentinel-release-error-lineage"
    captured_release_id = "sentinel-release-r1"
    current_release_id = "sentinel-release-r2"
    project = OntologyProject(
        id=ontology_id, name=ontology_id, domain="test",
        created_by="runtime-tests", status="published", version="v2",
        current_release_id=current_release_id,
    )
    empty_snapshot = {
        "objectTypes": [], "linkTypes": [], "actions": [],
        "functions": [], "sentinels": [], "mappings": [],
        "linkMappings": [],
    }
    r1 = OntologyVersion(
        id=captured_release_id, ontology_id=ontology_id,
        version_number="v1", node_kind="release",
        lifecycle_status="released", revision=0,
        snapshot_formal=empty_snapshot, created_by="runtime-tests",
    )
    r2 = OntologyVersion(
        id=current_release_id, ontology_id=ontology_id,
        version_number="v2", node_kind="release",
        lifecycle_status="released", revision=0,
        snapshot_formal=empty_snapshot, created_by="runtime-tests",
    )
    sentinel = Sentinel(
        id="lineage-fence-sentinel", ontology_id=ontology_id,
        name="lineage_fence_sentinel",
        display_name="Lineage fence sentinel",
        bindings=[], links=[], condition=None, primary_alias=None,
        action_ids=[], action_parameters={}, trigger_mode="on_enter",
        on_change=False, on_schedule=False, muted=False,
        enabled=True, status="published",
    )
    db.add_all([project, r1, r2, sentinel])
    db.commit()

    firing = evaluate_sentinel(
        db, ontology_id, sentinel, "manual",
        expected_release_id=captured_release_id,
    )

    assert firing.status == "error"
    assert firing.action_results[0]["validationErrors"] == [
        "release_context_changed",
    ]
    assert firing.ontology_release_id == captured_release_id
    assert firing.ontology_release_id != current_release_id


@pytest.mark.parametrize(
    "delete_live_projection", [False, True],
    ids=["draft-row-present", "draft-row-physically-deleted"],
)
def test_builtin_approval_resume_uses_released_sentinel_definition(
        db, monkeypatch, delete_live_projection):
    ontology_id = "builtin-resume-release-definition"
    release_id = "builtin-resume-r1"
    sentinel_id = "builtin-resume-sentinel"
    released_action_id = "builtin-released-action"
    project = OntologyProject(
        id=ontology_id, name=ontology_id, domain="test",
        created_by="runtime-tests", status="published", version="v1",
        current_release_id=release_id,
    )
    release = OntologyVersion(
        id=release_id, ontology_id=ontology_id,
        version_number="v1", node_kind="release",
        lifecycle_status="released", revision=0,
        snapshot_formal={
            "objectTypes": [], "linkTypes": [], "actions": [],
            "functions": [], "mappings": [], "linkMappings": [],
            "sentinels": [{
                "id": sentinel_id,
                "name": "builtin_resume_sentinel",
                "displayName": "Released sentinel",
                "bindings": [],
                "links": [],
                "condition": None,
                "primaryAlias": None,
                "actionIds": [released_action_id],
                "actionParameters": {},
                "triggerMode": "on_enter",
                "enabled": True,
                "muted": False,
            }],
        },
        created_by="runtime-tests",
    )
    # This is the mutable next-draft projection.  Neither its action chain nor
    # draft enabled bit is allowed to rewrite the pending R1 lifecycle.
    live = Sentinel(
        id=sentinel_id, ontology_id=ontology_id,
        name="draft_resume_sentinel",
        display_name="Draft sentinel",
        bindings=[], links=[], condition=None, primary_alias=None,
        action_ids=["draft-action"], action_parameters={},
        trigger_mode="on_enter", on_change=False, on_schedule=False,
        muted=False, enabled=False, status="draft",
        origin="release_builtin",
    )
    state = SentinelMatchState(
        id="builtin-resume-state", ontology_id=ontology_id,
        sentinel_id=sentinel_id, match_key="released-match",
        match_detail={
            "__snapshots__": {},
            "__event__": {
                "edge": "enter",
                "matchKey": "released-match",
                "ontologyReleaseId": release_id,
                "sentinelOrigin": "release_builtin",
                "sentinelDefinitionRevision": 1,
            },
        },
        runtime_status="pending_enter",
    )
    completed_step = ActionExecutionLog(
        id="builtin-resume-log", ontology_id=ontology_id,
        action_id=released_action_id,
        action_name="Released action", parameters={},
        status="success", dry_run=False,
        sentinel_match_state_id=state.id,
        ontology_version="v1", ontology_release_id=release_id,
    )
    db.add_all([project, release, live, state, completed_step])
    db.commit()
    if delete_live_projection:
        db.delete(live)
        db.commit()
        assert db.query(Sentinel).filter_by(id=sentinel_id).first() is None
    observed: dict = {}

    def run_released_chain(
            _db, _ontology_id, runtime_sentinel, _tup, _primary,
            _edge, _match_key, _state, _results, **_kwargs):
        observed["name"] = runtime_sentinel.display_name
        observed["actionIds"] = list(runtime_sentinel.action_ids)
        observed["enabled"] = runtime_sentinel.enabled
        return True, "success"

    monkeypatch.setattr(
        sentinel_evaluator, "_run_actions", run_released_chain)

    result = resume_sentinel_match_claim(db, ontology_id, state.id)

    assert result["status"] == "fired"
    assert observed == {
        "name": "Released sentinel",
        "actionIds": [released_action_id],
        "enabled": True,
    }


def test_dynamic_approval_resume_requires_live_overlay(db):
    ontology_id = "dynamic-resume-live-overlay"
    release_id = "dynamic-resume-r1"
    sentinel_id = "deleted-dynamic-sentinel"
    project = OntologyProject(
        id=ontology_id, name=ontology_id, domain="test",
        created_by="runtime-tests", status="published", version="v1",
        current_release_id=release_id,
    )
    release = OntologyVersion(
        id=release_id, ontology_id=ontology_id,
        version_number="v1", node_kind="release",
        lifecycle_status="released", revision=0,
        snapshot_formal={
            "objectTypes": [], "linkTypes": [], "actions": [],
            "functions": [], "mappings": [], "linkMappings": [],
            "sentinels": [{
                "id": sentinel_id,
                "name": "same_id_but_builtin",
                "displayName": "Same ID built-in",
                "bindings": [], "links": [], "actionIds": [],
            }],
        },
        created_by="runtime-tests",
    )
    state = SentinelMatchState(
        id="dynamic-resume-state", ontology_id=ontology_id,
        sentinel_id=sentinel_id, match_key="dynamic-match",
        match_detail={
            "__snapshots__": {},
            "__event__": {
                "edge": "enter",
                "matchKey": "dynamic-match",
                "ontologyReleaseId": release_id,
                "sentinelOrigin": "assistant_dynamic",
                "sentinelDefinitionRevision": 3,
            },
        },
        runtime_status="pending_enter",
    )
    db.add_all([project, release, state])
    db.commit()

    assert resume_sentinel_match_claim(
        db, ontology_id, state.id) == {"status": "sentinel_not_found"}


def _seed_object(db, ontology_id="runtime-hardening", *, suffix=""):
    if db.query(OntologyProject).filter_by(id=ontology_id).first() is None:
        db.add(OntologyProject(
            id=ontology_id, name=ontology_id, domain="test",
            created_by="runtime-tests", status="published", version="v1.0.0"))
        db.flush()
    object_type = ObjectType(
        id=f"ot-order{suffix}", ontology_id=ontology_id,
        name=f"Order{suffix}", display_name=f"Order{suffix}",
        primary_key="id", properties=[
            {"id": "id", "name": "id", "type": "string", "required": True},
            {"id": "active", "name": "active", "type": "boolean"},
            {"id": "count", "name": "count", "type": "number"},
            {"id": "status", "name": "status", "type": "string"},
        ],
    )
    instance = ObjectInstance(
        id=f"order-1{suffix}", ontology_id=ontology_id,
        object_type_id=object_type.id,
        properties={
            "id": f"order-1{suffix}",
            "active": True,
            "count": 0,
            "status": "new",
        },
    )
    db.add_all([object_type, instance])
    db.commit()
    return object_type, instance


def _body(action, instance, parameters=None, **extra):
    return SimpleNamespace(
        action_id=action.id,
        target_instance_id=instance.id if instance is not None else None,
        parameters=parameters or {}, dry_run=False, **extra,
    )


def _freeze_runtime_release(db, ontology_id: str) -> str:
    """Publish the current Formal rows as the immutable runtime fixture."""
    project = db.query(OntologyProject).filter_by(id=ontology_id).one()
    release_id = f"{ontology_id}-release-v1"
    snapshot = _snapshot_formal(db, ontology_id)
    release = OntologyVersion(
        id=release_id,
        ontology_id=ontology_id,
        version_number=project.version,
        node_kind="release",
        lifecycle_status="released",
        revision=0,
        snapshot_formal=snapshot,
        snapshot_hash=snapshot_hash(snapshot),
        published_at=datetime.now(timezone.utc),
        created_by="runtime-tests",
    )
    db.add(release)
    project.current_release_id = release_id
    for instance in db.query(ObjectInstance).filter_by(
            ontology_id=ontology_id).all():
        instance.ontology_release_id = release_id
    db.commit()
    return release_id


def _update_rule(property_name: str, *, source="constant", value="\"done\""):
    return {
        "id": f"update-{property_name}", "type": "update_property",
        "name": f"update {property_name}", "enabled": True, "order": 0,
        "config": {
            "targetProperty": property_name,
            "valueSource": source,
            "value": value,
        },
    }


def _resolve_public_outbound(_host, port, **_kwargs):
    """Keep webhook unit tests offline while preserving the SSRF guard."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


def test_webhook_preview_is_network_isolated_and_defers_dns(monkeypatch):
    def unexpected_network(*_args, **_kwargs):
        raise AssertionError("Webhook preview must not perform DNS or HTTP")

    monkeypatch.setattr(
        outbound_security.socket, "getaddrinfo", unexpected_network)
    monkeypatch.setattr(requests.Session, "request", unexpected_network)

    preview = preview_webhook(
        {
            "url": "https://partner.example/hooks/trial",
            "method": "POST",
            "bodyTemplate": '{"id": "{{params.request_id}}"}',
        },
        params={"request_id": "trial-1"},
        object_props={},
    )

    assert preview == {
        "url": "https://partner.example/hooks/trial",
        "method": "POST",
        "hasBody": True,
        "targetValidation": "syntax_only_dns_deferred",
    }


def test_webhook_preview_rejects_literal_private_target_without_dns(
        monkeypatch):
    monkeypatch.setattr(
        outbound_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("literal IP must not use DNS"),
    )

    with pytest.raises(WebhookDispatchError, match="受保护的内网地址"):
        preview_webhook(
            {"url": "http://127.0.0.1/internal", "method": "POST"},
            params={},
            object_props={},
        )


def test_pending_actions_include_real_business_context(db):
    ontology_id = "approval-dashboard-context"
    object_type, instance = _seed_object(db, ontology_id)
    object_type.display_name = "月度运营报告"
    object_type.primary_key = "report-code"
    object_type.properties = [{"id": "report-code", "name": "report_code"}]
    instance.properties = {"report_code": "2026-07"}
    instance.external_id = "report-fallback"
    log = ActionExecutionLog(
        id="approval-dashboard-log",
        ontology_id=ontology_id,
        action_id="refresh-indicators",
        action_name="刷新治理指标",
        object_type_id=object_type.id,
        object_instance_id=instance.id,
        parameters={"scope": "all"},
        status="pending",
        dry_run=False,
        actor_id="runtime-tests",
        ontology_version="v1.0.0",
    )
    recovering = ActionExecutionLog(
        id="approval-dashboard-recovering",
        ontology_id=ontology_id,
        action_id="refresh-indicators",
        action_name="刷新治理指标",
        object_type_id=object_type.id,
        object_instance_id=instance.id,
        parameters={"scope": "all"},
        status="executing",
        dry_run=False,
        actor_id="runtime-tests",
        decided_by="admin",
        decided_at=datetime.now(timezone.utc),
        ontology_version="v1.0.0",
    )
    db.add_all([log, recovering])
    db.commit()

    response = list_pending_actions(ontology_id, db=db, _=None)

    assert {item["id"] for item in response["data"]} == {
        log.id, recovering.id,
    }
    for item in response["data"]:
        assert item["objectTypeName"] == "月度运营报告"
        assert item["objectInstanceLabel"] == "月度运营报告 · 2026-07"
        assert item["triggerSource"] == "manual"


def test_action_parameter_contract_applies_defaults_and_rejects_bad_input(db):
    ontology_id = "action-contract"
    object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id="action-contract", ontology_id=ontology_id,
        name="set_status", display_name="Set status",
        object_type_id=object_type.id,
        parameters=[
            {
                "name": "status", "type": "string", "required": True,
                "defaultValue": "ready",
                "options": [
                    {"label": "Ready", "value": "ready"},
                    {"label": "Done", "value": "done"},
                ],
            },
            {"name": "score", "type": "number", "min": 1, "max": 10},
        ],
        rules=[_update_rule("status", source="parameter", value="status")],
    )
    db.add(action)
    db.commit()

    result = execute_action(db, ontology_id, _body(action, instance))
    db.refresh(instance)
    assert result["status"] == "success"
    assert result["parameters"] == {"status": "ready"}
    assert instance.properties["status"] == "ready"

    wrong_type = execute_action(
        db, ontology_id,
        _body(action, instance, {"status": "ready", "score": "9"}),
    )
    unknown = execute_action(
        db, ontology_id,
        _body(action, instance, {"status": "ready", "extra": True}),
    )
    out_of_range = execute_action(
        db, ontology_id,
        _body(action, instance, {"status": "ready", "score": 11}),
    )
    assert wrong_type["status"] == "failed"
    assert any("类型错误" in item for item in wrong_type["validationErrors"])
    assert unknown["status"] == "failed"
    assert any("未声明参数" in item for item in unknown["validationErrors"])
    assert out_of_range["status"] == "failed"
    assert any("不得大于" in item for item in out_of_range["validationErrors"])

    # Hints are a second projection of the same contract: canonical keys only,
    # aliases resolved exactly as execution resolves them.
    hints = [
        {"name": "status", "type": "string", "required": True,
         "default": "ready", "options": ["ready", "done"]},
        {"name": "score", "type": "number", "required": False, "min": 1, "max": 10},
    ]
    assert action_parameter_hints(action) == hints
    # Published releases hand the assistant snapshot namespaces, not ORM rows.
    snapshot_action = SimpleNamespace(**ActionTypeCreate.model_validate({
        "name": action.name, "displayName": action.display_name,
        "parameters": action.parameters,
    }).model_dump())
    assert action_parameter_hints(snapshot_action) == hints
    assert action_parameter_hints(SimpleNamespace(parameters=None)) == []

    guarded = SimpleNamespace(parameters=[
        {"id": "p1", "name": "priority", "displayName": "Priority",
         "description": "free text", "type": " STRING ",
         "allowedValues": [{"label": "Low", "value": "low"}, "high"],
         "default_value": "low", "minimum": "1.5", "maximum": 3},
        {"name": "note", "min_length": 1, "maxLength": "20", "default": None},
        {"name": "note", "default": "a duplicate definition loses"},
        {"name": "customer", "type": "reference", "defaultValue": "order-1"},
        {"name": "owner", "type": "reference", "options": ["u1", "u2"]},
        {"name": "payload", "type": "object", "defaultValue": {"a": 1}},
        {"name": "api_key", "defaultValue": "demo", "options": ["demo"]},
        {"name": "access_key", "defaultValue": "demo"},
        {"name": "big", "defaultValue": 10 ** 30, "options": [10 ** 30],
         "max": 10 ** 30, "maxLength": 10 ** 30},
        {"name": "nan", "defaultValue": float("nan"), "min": float("inf")},
        {"name": "comment", "defaultValue": "short free text"},
        {"name": "tags", "options": [["a"], "b"]},
        {"name": "region", "options": [f"r{index}" for index in range(21)],
         "defaultValue": "r0"},
        {"name": "broken", "options": []},
        {"name": " "}, "not a mapping",
    ])
    assert action_parameter_hints(guarded) == [
        {"name": "priority", "type": "string", "required": False,
         "default": "low", "options": ["low", "high"], "min": 1.5, "max": 3},
        # A declared null default stays distinct from "no default declared".
        {"name": "note", "type": "string", "required": False, "default": None,
         "minLength": 1, "maxLength": 20},
        {"name": "customer", "type": "reference", "required": False,
         "defaultOmitted": True},
        {"name": "owner", "type": "reference", "required": False,
         "optionsOmitted": True},
        {"name": "payload", "type": "object", "required": False,
         "defaultOmitted": True},
        {"name": "api_key", "type": "string", "required": False, "sensitive": True,
         "defaultOmitted": True, "optionsOmitted": True},
        {"name": "access_key", "type": "string", "required": False,
         "sensitive": True, "defaultOmitted": True},
        # No huge or non-finite number reaches the model, as a value or a bound.
        {"name": "big", "type": "string", "required": False,
         "defaultOmitted": True, "optionsOmitted": True},
        {"name": "nan", "type": "string", "required": False,
         "defaultOmitted": True},
        {"name": "comment", "type": "string", "required": False,
         "defaultOmitted": True},
        {"name": "tags", "type": "string", "required": False,
         "optionsOmitted": True},
        {"name": "region", "type": "string", "required": False,
         "defaultOmitted": True, "optionsOmitted": True},
        {"name": "broken", "type": "string", "required": False,
         "optionsOmitted": True},
    ]

    # Both projections resolve the same aliases; execution keeps enforcing each
    # spelling of every bound.
    lengthy = SimpleNamespace(parameters=[
        {"name": "note", "minLength": 2, "max_length": 5}])
    assert prepare_action_parameters(lengthy, {"note": "a"})[1] == [
        "参数「note」长度不得小于 2"]
    assert prepare_action_parameters(lengthy, {"note": "toolong"})[1] == [
        "参数「note」长度不得大于 5"]
    bounded = SimpleNamespace(parameters=[
        {"name": "level", "type": "number", "minimum": 1, "maximum": 5}])
    assert prepare_action_parameters(bounded, {"level": 0})[1] == [
        "参数「level」不得小于 1"]
    assert prepare_action_parameters(bounded, {"level": 6})[1] == [
        "参数「level」不得大于 5"]


def test_static_action_gate_checks_action_and_rule_validation_function_types():
    object_types = [{
        "id": "ot-static", "name": "Static", "displayName": "Static",
        "properties": [{"id": "id", "name": "id", "type": "string"}],
    }]
    functions = [
        {
            "id": "fn-valid", "name": "valid",
            "functionType": "action_validation",
        },
        {
            "id": "fn-object", "name": "object",
            "functionType": "object",
        },
    ]
    base = {
        "id": "action-static", "name": "static",
        "displayName": "Static action", "objectTypeId": "ot-static",
        "parameters": [],
        "rules": [{
            "id": "notify", "type": "notification",
            "name": "notify", "enabled": True, "order": 1,
            "config": {
                "channel": "internal", "recipientSource": "constant",
                "recipient": "ops", "messageTemplate": "ready",
            },
        }],
    }
    invalid = {
        **base,
        "validationFunctionId": "fn-object",
        "rules": [{
            "id": "validate", "type": "validation",
            "name": "validate", "enabled": True, "order": 0,
            "config": {"functionId": "fn-object"},
        }, *base["rules"]],
    }

    errors = validate_action_definition(
        invalid, object_types, [], functions)
    assert sum("action_validation" in error for error in errors) == 2

    valid = {**base, "validationFunctionId": "fn-valid"}
    assert validate_action_definition(
        valid, object_types, [], functions) == []


def test_action_validation_object_set_is_pinned_to_current_release(db):
    ontology_id = "action-validation-release-scope"
    release_id = "release-current"
    project = OntologyProject(
        id=ontology_id, name="Release scope", domain="test",
        created_by="runtime-tests", status="published",
        version="v2", current_release_id=release_id,
    )
    release = OntologyVersion(
        id=release_id, ontology_id=ontology_id,
        version_number="v2", node_kind="release",
        lifecycle_status="released", revision=0,
        snapshot_formal={
            "objectTypes": [{
                "id": "ot-release-scoped",
                "name": "ReleaseScoped",
                "displayName": "Release scoped",
                "primaryKey": "id",
                "properties": [{
                    "id": "id", "name": "id", "type": "string",
                    "required": True,
                }],
            }],
            "linkTypes": [],
            "functions": [{
                "id": "validate-current-only",
                "name": "validate_current_only",
                "displayName": "Validate current only",
                "functionType": "action_validation",
                "language": "expression",
                "targetObjectTypeId": "ot-release-scoped",
                "parameters": [],
                "returnType": "object",
                "body": (
                    "{'valid': len(objects) == 1, "
                    "'errors': ['release leak']}"),
                "enabled": True,
            }],
            "actions": [{
                "id": "release-scoped-action",
                "name": "release_scoped_action",
                "displayName": "Release scoped action",
                "objectTypeId": "ot-release-scoped",
                "parameters": [],
                "validationFunctionId": "validate-current-only",
                "rules": [{
                    "id": "notify", "type": "notification",
                    "name": "notify", "enabled": True, "order": 0,
                    "config": {
                        "channel": "internal",
                        "recipientSource": "constant",
                        "recipient": "ops",
                        "messageTemplate": "current release only",
                    },
                }],
            }],
            "sentinels": [], "mappings": [], "linkMappings": [],
        },
        created_by="runtime-tests",
    )
    object_type = ObjectType(
        id="ot-release-scoped", ontology_id=ontology_id,
        name="ReleaseScoped", display_name="Release scoped",
        primary_key="id", properties=[
            {"id": "id", "name": "id", "type": "string",
             "required": True},
        ],
    )
    current = ObjectInstance(
        id="current-instance", ontology_id=ontology_id,
        ontology_release_id=release_id,
        object_type_id=object_type.id, properties={"id": "current"},
    )
    stale = ObjectInstance(
        id="stale-instance", ontology_id=ontology_id,
        ontology_release_id="release-old",
        object_type_id=object_type.id, properties={"id": "stale"},
    )
    validator = OntologyFunction(
        id="validate-current-only", ontology_id=ontology_id,
        name="validate_current_only", display_name="Validate current only",
        function_type="action_validation", language="expression",
        target_object_type_id=object_type.id,
        return_type="object",
        body="{'valid': len(objects) == 1, 'errors': ['release leak']}",
        enabled=True,
    )
    action = ActionType(
        id="release-scoped-action", ontology_id=ontology_id,
        name="release_scoped_action", display_name="Release scoped action",
        object_type_id=object_type.id, parameters=[],
        validation_function_id=validator.id,
        rules=[{
            "id": "notify", "type": "notification", "name": "notify",
            "enabled": True, "order": 0,
            "config": {
                "channel": "internal", "recipientSource": "constant",
                "recipient": "ops", "messageTemplate": "current release only",
            },
        }],
    )
    db.add_all([
        project, release, object_type, current, stale, validator, action])
    db.commit()

    # Mutable Formal definitions now represent an unpublished draft. The
    # expected release must continue to use the frozen validator above.
    validator.body = "{'valid': False, 'errors': ['draft validator leaked']}"
    db.commit()
    result = execute_action(
        db, ontology_id, _body(action, current),
        expected_release_id=release_id)

    assert result["status"] == "success", result
    notification = db.query(Notification).filter_by(
        ontology_id=ontology_id).one()
    assert notification.ontology_release_id == release_id
    assert notification.action_id == action.id
    assert notification.action_log_id == result["id"]
    assert notification.sentinel_id is None


def test_approved_sentinel_action_recovers_release_from_durable_lineage(db):
    ontology_id = "approved-sentinel-release-definition"
    release_id = "approved-sentinel-r1"
    action_id = "approved-sentinel-action"
    object_type_id = "approved-sentinel-object"
    project = OntologyProject(
        id=ontology_id, name=ontology_id, domain="test",
        created_by="runtime-tests", status="published", version="v1",
        current_release_id=release_id,
    )
    released_rule = {
        "id": "released-status", "type": "update_property",
        "name": "released status", "enabled": True, "order": 0,
        "config": {
            "targetProperty": "status",
            "valueSource": "constant",
            "value": "\"released\"",
        },
    }
    notification_rule = {
        "id": "notify-owner", "type": "notification",
        "name": "notify owner", "enabled": True, "order": 1,
        "config": {
            "channel": "internal",
            "recipientSource": "constant",
            "recipient": "ops",
            "messageTemplate": "approved={{object.id}}",
        },
    }
    release = OntologyVersion(
        id=release_id, ontology_id=ontology_id,
        version_number="v1", node_kind="release",
        lifecycle_status="released", revision=0,
        snapshot_formal={
            "objectTypes": [{
                "id": object_type_id, "name": "ApprovedObject",
                "displayName": "Approved object", "primaryKey": "id",
                "properties": [
                    {
                        "id": "id", "name": "id", "type": "string",
                        "required": True,
                    },
                    {
                        "id": "status", "name": "status",
                        "type": "string",
                    },
                ],
            }],
            "linkTypes": [], "functions": [],
            "actions": [{
                "id": action_id, "name": "approved_sentinel_action",
                "displayName": "Approved sentinel action",
                "objectTypeId": object_type_id,
                "parameters": [],
                "rules": [released_rule, notification_rule],
                "requiresApproval": True,
            }],
            "sentinels": [], "mappings": [], "linkMappings": [],
        },
        created_by="runtime-tests",
    )
    object_type = ObjectType(
        id=object_type_id, ontology_id=ontology_id,
        name="ApprovedObject", display_name="Approved object",
        primary_key="id", properties=[
            {
                "id": "id", "name": "id", "type": "string",
                "required": True,
            },
            {"id": "status", "name": "status", "type": "string"},
        ],
    )
    instance = ObjectInstance(
        id="approved-target", ontology_id=ontology_id,
        ontology_release_id=release_id,
        object_type_id=object_type_id,
        properties={"id": "approved-target", "status": "new"},
    )
    # Live Formal rows may already carry the next draft.  The approved run must
    # still use the ActionType frozen in R1.
    live_action = ActionType(
        id=action_id, ontology_id=ontology_id,
        name="approved_sentinel_action",
        display_name="Approved sentinel action",
        object_type_id=object_type_id, parameters=[],
        rules=[
            {
                **released_rule,
                "config": {
                    **released_rule["config"],
                    "value": "\"draft\"",
                },
            },
            notification_rule,
        ],
        requires_approval=True,
    )
    state = SentinelMatchState(
        id="approved-sentinel-state", ontology_id=ontology_id,
        sentinel_id="approved-sentinel",
        match_key="approved-target",
        match_detail={}, runtime_status="pending_enter",
    )
    proposal = ActionExecutionLog(
        id="approved-sentinel-proposal",
        ontology_id=ontology_id, action_id=action_id,
        action_name="Approved sentinel action",
        object_type_id=object_type_id,
        object_instance_id=instance.id,
        parameters={}, status="pending", dry_run=False,
        sentinel_match_state_id=state.id,
        ontology_version="v1", ontology_release_id=release_id,
    )
    db.add_all([
        project, release, object_type, instance, live_action, state, proposal,
    ])
    db.commit()

    result = execute_action(
        db, ontology_id,
        _body(
            live_action, instance,
            sentinel_match_state_id=state.id,
            idempotency_key="approval-execution:approved-sentinel-proposal",
        ),
        skip_approval=True,
    )

    db.refresh(instance)
    assert result["status"] == "success", result
    assert result["ontologyReleaseId"] == release_id
    assert instance.properties["status"] == "released"
    notification = db.query(Notification).filter_by(
        ontology_id=ontology_id).one()
    assert notification.sentinel_id == state.sentinel_id
    assert notification.ontology_release_id == release_id
    assert notification.action_log_id == result["id"]


def test_production_action_blocks_during_incomplete_mapping_projection(
        db, monkeypatch):
    from app.config import settings

    ontology_id = "action-projection-fence"
    object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id="projection-fenced-action", ontology_id=ontology_id,
        name="projection_fenced", display_name="Projection fenced",
        object_type_id=object_type.id, parameters=[], rules=[])
    mapping = OntologyMapping(
        id="projection-in-progress", ontology_id=ontology_id,
        entity_class="Order", field_mapping={}, status="projecting")
    db.add_all([action, mapping])
    db.commit()
    monkeypatch.setattr(settings, "environment", "production")

    result = execute_action(db, ontology_id, _body(action, instance))

    assert result["status"] == "failed"
    assert result["validationErrors"] == ["ontology_projection_not_ready"]


def test_action_rejects_bad_target_missing_validation_and_unknown_rule(db):
    ontology_id = "action-fail-closed"
    object_type, instance = _seed_object(db, ontology_id)
    other_type, other = _seed_object(db, ontology_id, suffix="-other")
    action = ActionType(
        id="action-guarded", ontology_id=ontology_id,
        name="guarded", display_name="Guarded",
        object_type_id=object_type.id, parameters=[], rules=[],
        validation_function_id="missing-validation-function",
    )
    db.add(action)
    db.commit()

    missing_validation = execute_action(db, ontology_id, _body(action, instance))
    wrong_target = execute_action(db, ontology_id, _body(action, other))
    assert missing_validation["status"] == "failed"
    assert any("校验函数不存在" in item for item in missing_validation["validationErrors"])
    assert wrong_target["status"] == "failed"
    assert any("类型不匹配" in item for item in wrong_target["validationErrors"])

    action.validation_function_id = None
    action.rules = [{
        "id": "mystery", "type": "silently_do_something",
        "name": "mystery", "enabled": True, "order": 0, "config": {},
    }]
    db.commit()
    unknown_rule = execute_action(db, ontology_id, _body(action, instance))
    assert unknown_rule["status"] == "failed"
    assert any("不支持的动作规则类型" in item
               for item in unknown_rule["validationErrors"])


def test_external_notification_never_reports_fake_success(db):
    rule = {
        "id": "email", "type": "notification", "name": "email",
        "enabled": True, "order": 0,
        "config": {
            "channel": "email", "recipientSource": "constant",
            "recipient": "ops@example.com", "messageTemplate": "hello",
        },
    }
    ontology_id = "delivery-email"
    object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id=f"action-{rule['id']}", ontology_id=ontology_id,
        name=rule["id"], display_name=rule["id"],
        object_type_id=object_type.id, parameters=[], rules=[rule],
    )
    db.add(action)
    db.commit()

    result = execute_action(db, ontology_id, _body(action, instance))
    assert result["status"] == "failed"
    assert "可靠投递器" in result["errorMessage"]
    assert db.query(Notification).filter_by(ontology_id=ontology_id).count() == 0


def test_webhook_dispatches_rendered_json_with_retry_identity(db, monkeypatch):
    ontology_id = "delivery-webhook"
    object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id="action-webhook", ontology_id=ontology_id,
        name="notify_partner", display_name="Notify partner",
        object_type_id=object_type.id,
        parameters=[
            {"name": "request_id", "type": "string", "required": True},
            {"name": "count", "type": "number", "required": True},
            {"name": "labels", "type": "array", "required": True},
        ],
        rules=[{
            "id": "webhook", "type": "webhook", "name": "notify partner",
            "enabled": True, "order": 0,
            "config": {
                "url": "https://partner.example/hooks/ontology",
                "method": "POST",
                "headers": {"X-Partner-Token": "test-token"},
                "bodyTemplate": (
                    '{"requestId":"{{params.request_id}}",'
                    '"count":{{params.count}},"labels":{{params.labels}},'
                    '"state":"{{object.status}}"}'
                ),
            },
        }],
    )
    db.add(action)
    db.commit()
    observed = {}

    def fake_request(session, method, url, **kwargs):
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = kwargs["headers"]
        observed["body"] = json.loads(kwargs["data"].decode("utf-8"))
        response = requests.Response()
        response.status_code = 202
        response.url = url
        response._content = b"accepted"
        return response

    monkeypatch.setattr(
        outbound_security.socket, "getaddrinfo", _resolve_public_outbound
    )
    monkeypatch.setattr(requests.Session, "request", fake_request)
    result = execute_action(
        db, ontology_id,
        _body(action, instance, {
            "request_id": "req-42", "count": 3, "labels": ["urgent", "governance"],
        }, idempotency_key="manual-request-42"),
    )

    assert result["status"] == "success"
    assert observed["method"] == "POST"
    assert observed["url"] == "https://partner.example/hooks/ontology"
    assert observed["body"] == {
        "requestId": "req-42", "count": 3,
        "labels": ["urgent", "governance"], "state": "new",
    }
    assert observed["headers"]["X-Partner-Token"] == "test-token"
    assert observed["headers"]["Content-Type"] == "application/json; charset=utf-8"
    delivery_key = observed["headers"]["Idempotency-Key"]
    assert ":manual-request-42:" in delivery_key
    assert len(delivery_key.rsplit(":", 1)[-1]) == 20
    assert result["effects"][0]["type"] == "webhook"
    assert result["effects"][0]["statusCode"] == 202


def test_webhook_failure_rolls_back_preceding_local_rules(db, monkeypatch):
    ontology_id = "delivery-webhook-rollback"
    object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id="action-webhook-rollback", ontology_id=ontology_id,
        name="update_then_notify", display_name="Update then notify",
        object_type_id=object_type.id, parameters=[],
        rules=[
            _update_rule("status", value='"queued"'),
            {
                "id": "webhook", "type": "webhook", "name": "notify partner",
                "enabled": True, "order": 1,
                "config": {"url": "https://partner.example/hooks/fail", "method": "POST"},
            },
        ],
    )
    db.add(action)
    db.commit()
    calls = []

    def fake_request(session, method, url, **kwargs):
        calls.append((method, url, kwargs["headers"]["Idempotency-Key"]))
        response = requests.Response()
        response.status_code = 503
        response.url = url
        response._content = b"temporarily unavailable"
        return response

    monkeypatch.setattr(
        outbound_security.socket, "getaddrinfo", _resolve_public_outbound
    )
    monkeypatch.setattr(requests.Session, "request", fake_request)
    result = execute_action(db, ontology_id, _body(action, instance))
    db.refresh(instance)

    assert result["status"] == "failed"
    assert "Webhook 投递失败" in result["errorMessage"]
    # Settings defaults retry transient 5xx once; every try carries one key.
    assert len(calls) == 2
    assert calls[0][2] == calls[1][2]
    assert instance.properties["status"] == "new"
    webhook_effect = next(
        effect for effect in result["effects"]
        if effect["type"] == "webhook")
    assert webhook_effect["status"] == "delivery_uncertain"
    assert webhook_effect["externalDeliveryMayHaveOccurred"] is True
    assert webhook_effect["localTransactionCommitted"] is False
    assert webhook_effect["idempotencyKey"] == calls[0][2]
    assert webhook_effect["attempts"] == 2
    assert webhook_effect["url"] == "https://partner.example/hooks/fail"
    assert "按 idempotencyKey 对账" in webhook_effect["description"]


def test_internal_notification_has_a_real_queryable_sink(db):
    ontology_id = "delivery-internal"
    object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id="action-internal", ontology_id=ontology_id,
        name="internal", display_name="Internal",
        object_type_id=object_type.id, parameters=[], rules=[{
            "id": "internal", "type": "notification", "name": "internal",
            "enabled": True, "order": 0,
            "config": {
                "channel": "internal", "recipientSource": "constant",
                "recipient": "ops", "messageTemplate": "object={{object.status}}",
            },
        }],
    )
    db.add(action)
    db.commit()

    result = execute_action(db, ontology_id, _body(action, instance))
    notification = db.query(Notification).filter_by(ontology_id=ontology_id).one()
    assert result["status"] == "success"
    assert notification.status == "delivered"
    assert notification.body == "object=new"


def test_empty_action_and_incomplete_notification_fail_closed(db):
    ontology_id = "action-no-fake-success"
    object_type, instance = _seed_object(db, ontology_id)
    empty = ActionType(
        id="empty-action", ontology_id=ontology_id,
        name="empty", display_name="Empty",
        object_type_id=object_type.id, parameters=[], rules=[],
    )
    incomplete_notification = ActionType(
        id="incomplete-notification", ontology_id=ontology_id,
        name="incomplete_notification", display_name="Incomplete notification",
        object_type_id=object_type.id, parameters=[], rules=[{
            "id": "internal", "type": "notification", "name": "internal",
            "enabled": True, "order": 0,
            "config": {
                "channel": "internal",
                "recipientSource": "constant",
                "recipient": "ops",
                "messageTemplate": "missing={{object.not_declared}}",
            },
        }],
    )
    db.add_all([empty, incomplete_notification])
    db.commit()

    no_effect = execute_action(db, ontology_id, _body(empty, instance))
    missing_value = execute_action(
        db, ontology_id, _body(incomplete_notification, instance))

    assert no_effect["status"] == "failed"
    assert any("可执行副作用规则" in item
               for item in no_effect["validationErrors"])
    assert missing_value["status"] == "failed"
    assert "模板" in missing_value["errorMessage"]
    assert db.query(Notification).filter_by(
        ontology_id=ontology_id).count() == 0


def test_action_expressions_and_templates_reject_missing_runtime_references(db):
    ontology_id = "action-missing-runtime-reference"
    object_type, instance = _seed_object(db, ontology_id)
    invalid_validation = ActionType(
        id="invalid-validation-reference", ontology_id=ontology_id,
        name="invalid_validation_reference",
        display_name="Invalid validation reference",
        object_type_id=object_type.id, parameters=[], rules=[
            {
                "id": "validate", "type": "validation", "name": "validate",
                "enabled": True, "order": 0,
                "config": {
                    "condition": "object.not_declared != 'blocked'",
                },
            },
            _update_rule("status"),
        ],
    )
    invalid_expression = ActionType(
        id="invalid-value-reference", ontology_id=ontology_id,
        name="invalid_value_reference",
        display_name="Invalid value reference",
        object_type_id=object_type.id, parameters=[], rules=[
            _update_rule(
                "count", source="expression",
                value="object.not_declared or 1"),
        ],
    )
    invalid_template = ActionType(
        id="invalid-template-reference", ontology_id=ontology_id,
        name="invalid_template_reference",
        display_name="Invalid template reference",
        object_type_id=object_type.id, parameters=[], rules=[{
            "id": "notify", "type": "notification", "name": "notify",
            "enabled": True, "order": 0,
            "config": {
                "channel": "internal", "recipientSource": "constant",
                "recipient": "ops",
                "messageTemplate": "bad={{unknown.value}}",
            },
        }],
    )
    db.add_all([
        invalid_validation, invalid_expression, invalid_template])
    db.commit()

    validation_result = execute_action(
        db, ontology_id, _body(invalid_validation, instance))
    expression_result = execute_action(
        db, ontology_id, _body(invalid_expression, instance))
    template_result = execute_action(
        db, ontology_id, _body(invalid_template, instance))

    assert validation_result["status"] == "failed"
    assert any("不存在的属性" in error
               for error in validation_result["validationErrors"])
    assert expression_result["status"] == "failed"
    assert "不存在的属性" in expression_result["errorMessage"]
    assert template_result["status"] == "failed"
    assert "无法解析的占位符" in template_result["errorMessage"]
    assert db.query(Notification).filter_by(
        ontology_id=ontology_id).count() == 0


def test_deleted_leave_target_snapshot_survives_hitl_but_cannot_mutate(db):
    ontology_id = "approval-deleted-target-snapshot"
    object_type, instance = _seed_object(db, ontology_id)
    notify = ActionType(
        id="notify-deleted-target", ontology_id=ontology_id,
        name="notify_deleted_target", display_name="Notify deleted target",
        object_type_id=object_type.id, parameters=[], requires_approval=True,
        rules=[{
            "id": "notify", "type": "notification", "name": "notify",
            "enabled": True, "order": 0,
            "config": {
                "channel": "internal", "recipientSource": "constant",
                "recipient": "ops",
                "messageTemplate": (
                    "deleted={{object.id}} status={{object.status}}"),
            },
        }],
    )
    mutate = ActionType(
        id="mutate-deleted-target", ontology_id=ontology_id,
        name="mutate_deleted_target", display_name="Mutate deleted target",
        object_type_id=object_type.id, parameters=[],
        rules=[_update_rule("status", value='"should-not-run"')],
    )
    linked_recipient = ActionType(
        id="notify-deleted-target-via-link", ontology_id=ontology_id,
        name="notify_deleted_target_via_link",
        display_name="Notify deleted target via link",
        object_type_id=object_type.id, parameters=[], rules=[{
            "id": "notify", "type": "notification", "name": "notify",
            "enabled": True, "order": 0,
            "config": {
                "channel": "internal", "recipientSource": "link",
                "linkTypeId": "recipient-link",
                "recipientProperty": "email",
                "messageTemplate": "deleted={{object.id}}",
            },
        }],
    )
    db.add_all([notify, mutate, linked_recipient])
    db.commit()
    release_id = _freeze_runtime_release(db, ontology_id)
    target_snapshot = {
        "id": instance.id,
        "objectTypeId": object_type.id,
        "properties": dict(instance.properties),
        "computed": {},
    }

    pending = execute_action(
        db, ontology_id,
        _body(
            notify, instance,
            idempotency_key="deleted-target-proposal",
            target_snapshot=target_snapshot,
            expected_release_id=release_id,
        ),
    )
    assert pending["status"] == "pending"
    assert pending["targetSnapshot"] == target_snapshot

    db.delete(instance)
    db.commit()
    pending_rows = list_pending_actions(ontology_id, db=db, _=None)["data"]
    assert pending_rows[0]["objectInstanceLabel"] == "Order · order-1"

    admin = SimpleNamespace(id="admin", username="admin", role="admin")
    approved = decide_pending_action(
        ontology_id, pending["id"],
        DecisionRequest(decision="approved", release_id=release_id), db, admin,
    )["data"]
    assert approved["pendingLog"]["status"] == "approved"
    assert approved["executionLog"]["status"] == "success"
    assert approved["executionLog"]["targetSnapshot"] == target_snapshot
    notification = db.query(Notification).filter_by(
        ontology_id=ontology_id).one()
    assert notification.body == "deleted=order-1 status=new"
    assert notification.related_object_id == "order-1"

    rejected_mutation = execute_action(
        db, ontology_id,
        _body(
            mutate, SimpleNamespace(id="order-1"),
            target_snapshot=target_snapshot,
            expected_release_id=release_id,
        ),
    )
    assert rejected_mutation["status"] == "failed"
    assert any("目标实例不存在" in error
               for error in rejected_mutation["validationErrors"])
    rejected_link_lookup = execute_action(
        db, ontology_id,
        _body(
            linked_recipient, SimpleNamespace(id="order-1"),
            target_snapshot=target_snapshot,
            expected_release_id=release_id,
        ),
    )
    assert rejected_link_lookup["status"] == "failed"
    assert any("目标实例不存在" in error
               for error in rejected_link_lookup["validationErrors"])


def test_delete_link_condition_is_applied_per_target(db):
    ontology_id = "conditional-delete-link"
    order_type, order = _seed_object(db, ontology_id)
    target_type = ObjectType(
        id="ot-recipient", ontology_id=ontology_id,
        name="Recipient", display_name="Recipient",
        primary_key="id", properties=[
            {"id": "id", "name": "id", "type": "string", "required": True},
            {"id": "status", "name": "status", "type": "string"},
        ],
    )
    active = ObjectInstance(
        id="recipient-active", ontology_id=ontology_id,
        object_type_id=target_type.id,
        properties={"id": "recipient-active", "status": "active"},
    )
    inactive = ObjectInstance(
        id="recipient-inactive", ontology_id=ontology_id,
        object_type_id=target_type.id,
        properties={"id": "recipient-inactive", "status": "inactive"},
    )
    link_type = LinkType(
        id="lt-recipient", ontology_id=ontology_id,
        name="recipient", display_name="Recipient",
        source_object_type_id=order_type.id,
        target_object_type_id=target_type.id,
        cardinality="many-to-many", properties=[],
    )
    active_link = LinkInstance(
        id="link-active", ontology_id=ontology_id,
        link_type_id=link_type.id,
        source_object_id=order.id, target_object_id=active.id,
        properties={},
    )
    inactive_link = LinkInstance(
        id="link-inactive", ontology_id=ontology_id,
        link_type_id=link_type.id,
        source_object_id=order.id, target_object_id=inactive.id,
        properties={},
    )
    action = ActionType(
        id="delete-inactive-recipient", ontology_id=ontology_id,
        name="delete_inactive", display_name="Delete inactive",
        object_type_id=order_type.id, parameters=[], rules=[{
            "id": "delete", "type": "delete_link", "name": "delete",
            "enabled": True, "order": 0,
            "config": {
                "linkTypeId": link_type.id,
                "condition": "target.status == 'inactive'",
            },
        }],
    )
    db.add_all([
        target_type, active, inactive, link_type,
        active_link, inactive_link, action,
    ])
    db.commit()

    result = execute_action(db, ontology_id, _body(action, order))

    assert result["status"] == "success"
    assert result["effects"][0]["matchedLinkIds"] == [inactive_link.id]
    assert db.query(LinkInstance).filter_by(
        ontology_id=ontology_id, id=active_link.id).count() == 1
    assert db.query(LinkInstance).filter_by(
        ontology_id=ontology_id, id=inactive_link.id).count() == 0


def test_action_mutations_are_blocked_by_formal_contract(db):
    ontology_id = "action-formal-contract"
    object_type, instance = _seed_object(db, ontology_id)
    invalid_update = ActionType(
        id="invalid-update", ontology_id=ontology_id,
        name="invalid_update", display_name="Invalid update",
        object_type_id=object_type.id, parameters=[],
        rules=[_update_rule("count", value='"not-a-number"')],
    )
    db.add(invalid_update)
    db.commit()

    result = execute_action(
        db, ontology_id, _body(invalid_update, instance))
    db.refresh(instance)

    assert result["status"] == "failed"
    assert "对象实例契约校验失败" in result["errorMessage"]
    assert instance.properties["count"] == 0


def test_derived_failure_rolls_back_action_object_notification_and_facts(
        db, monkeypatch):
    ontology_id = "action-derived-rollback"
    object_type, instance = _seed_object(db, ontology_id)
    object_type.properties = [
        *object_type.properties,
        {
            "id": "risk-score", "name": "risk_score", "type": "number",
            "source": "computed", "functionId": "missing-derived-function",
        },
    ]
    action = ActionType(
        id="derived-failure-action", ontology_id=ontology_id,
        name="derived_failure_action",
        display_name="Derived failure action",
        object_type_id=object_type.id, parameters=[], rules=[
            _update_rule("status", value='"updated"'),
            {
                "id": "notify", "type": "notification",
                "name": "notify", "enabled": True, "order": 1,
                "config": {
                    "channel": "internal",
                    "recipientSource": "constant",
                    "recipient": "ops",
                    "messageTemplate": "updated={{object.status}}",
                },
            },
            {
                "id": "webhook", "type": "webhook",
                "name": "webhook", "enabled": True, "order": 2,
                "config": {
                    "url": "https://example.com/derived-failure",
                    "method": "POST",
                    "bodyTemplate": (
                        '{"risk": {{object.risk_score}}}'),
                },
            },
        ],
    )
    db.add(action)
    db.commit()
    webhook_calls = []

    def unexpected_webhook(*args, **kwargs):
        webhook_calls.append((args, kwargs))
        raise AssertionError(
            "派生重算失败后不得尝试不可回滚的外部投递")

    monkeypatch.setattr(requests.Session, "request", unexpected_webhook)

    result = execute_action(db, ontology_id, _body(action, instance))
    db.refresh(instance)

    assert result["status"] == "failed"
    assert "派生" in result["errorMessage"]
    assert instance.properties["status"] == "new"
    assert db.query(Notification).filter_by(
        ontology_id=ontology_id).count() == 0
    assert db.query(PropertyFact).filter_by(
        ontology_id=ontology_id).count() == 0
    assert webhook_calls == []


def test_deferred_webhook_uses_recomputed_authoritative_target_view(
        db, monkeypatch):
    ontology_id = "action-derived-webhook-view"
    object_type, instance = _seed_object(db, ontology_id)
    object_type.properties = [
        *object_type.properties,
        {
            "id": "amount", "name": "amount", "type": "number",
            "required": True,
        },
        {
            "id": "risk", "name": "risk", "type": "number",
            "source": "computed", "functionId": "derive-risk",
        },
    ]
    instance.properties = {
        **dict(instance.properties or {}),
        "amount": 5,
    }
    instance.computed = {"risk": 10}
    derive_risk = OntologyFunction(
        id="derive-risk", ontology_id=ontology_id,
        name="derive_risk", display_name="Derive risk",
        function_type="object", language="expression",
        target_object_type_id=object_type.id,
        parameters=[], return_type="number",
        body='object["amount"] * 2', enabled=True,
    )
    action = ActionType(
        id="update-and-send-risk", ontology_id=ontology_id,
        name="update_and_send_risk",
        display_name="Update and send risk",
        object_type_id=object_type.id, parameters=[], rules=[
            _update_rule("amount", value="9"),
            {
                "id": "send-risk", "type": "webhook",
                "name": "Send risk", "enabled": True, "order": 1,
                "config": {
                    "url": "https://example.com/risk",
                    "method": "POST",
                    "bodyTemplate": (
                        '{"amount": {{object.amount}}, '
                        '"risk": {{object.risk}}}'),
                },
            },
        ],
    )
    db.add_all([derive_risk, action])
    db.commit()
    payloads: list[dict] = []

    def capture_request(session, method, url, **kwargs):
        payloads.append(json.loads(kwargs["data"]))
        response = requests.Response()
        response.status_code = 204
        response.url = url
        response._content = b""
        return response

    monkeypatch.setattr(
        outbound_security.socket, "getaddrinfo", _resolve_public_outbound
    )
    monkeypatch.setattr(requests.Session, "request", capture_request)

    result = execute_action(db, ontology_id, _body(action, instance))

    db.refresh(instance)
    assert result["status"] == "success", result
    assert instance.properties["amount"] == 9
    assert instance.computed["risk"] == 18
    assert payloads == [{"amount": 9, "risk": 18}]


def test_ordered_local_rule_uses_recomputed_authoritative_target_view(db):
    ontology_id = "action-derived-local-view"
    object_type, instance = _seed_object(db, ontology_id)
    object_type.properties = [
        *object_type.properties,
        {
            "id": "amount", "name": "amount", "type": "number",
            "required": True,
        },
        {
            "id": "risk", "name": "risk", "type": "number",
            "source": "computed", "functionId": "derive-local-risk",
        },
    ]
    instance.properties = {
        **dict(instance.properties or {}),
        "amount": 5,
    }
    instance.computed = {"risk": 10}
    derive_risk = OntologyFunction(
        id="derive-local-risk", ontology_id=ontology_id,
        name="derive_local_risk", display_name="Derive local risk",
        function_type="object", language="expression",
        target_object_type_id=object_type.id,
        parameters=[], return_type="number",
        body='object["amount"] * 2', enabled=True,
    )
    action = ActionType(
        id="update-and-notify-risk", ontology_id=ontology_id,
        name="update_and_notify_risk",
        display_name="Update and notify risk",
        object_type_id=object_type.id, parameters=[], rules=[
            _update_rule("amount", value="9"),
            {
                "id": "notify-risk", "type": "notification",
                "name": "Notify risk", "enabled": True, "order": 1,
                "config": {
                    "channel": "internal",
                    "recipientSource": "constant",
                    "recipient": "ops",
                    "messageTemplate": (
                        "amount={{object.amount}} risk={{object.risk}}"),
                },
            },
        ],
    )
    db.add_all([derive_risk, action])
    db.commit()

    dry_result = execute_action(
        db,
        ontology_id,
        SimpleNamespace(
            action_id=action.id,
            target_instance_id=instance.id,
            parameters={},
            dry_run=True,
        ),
    )

    db.refresh(instance)
    assert dry_result["status"] == "success", dry_result
    assert dry_result["effects"][1]["message"] == "amount=9 risk=18"
    assert instance.properties["amount"] == 5
    assert instance.computed["risk"] == 10
    assert db.query(Notification).filter_by(
        ontology_id=ontology_id).count() == 0

    result = execute_action(db, ontology_id, _body(action, instance))

    db.refresh(instance)
    notification = db.query(Notification).filter_by(
        ontology_id=ontology_id).one()
    assert result["status"] == "success", result
    assert result["effects"][1]["message"] == "amount=9 risk=18"
    assert instance.properties["amount"] == 9
    assert instance.computed["risk"] == 18
    assert notification.body == "amount=9 risk=18"
    facts = db.query(PropertyFact).filter_by(
        ontology_id=ontology_id,
        instance_id=instance.id,
    ).all()
    assert {fact.property_name for fact in facts} == {"amount", "risk"}
    amount_fact = next(
        fact for fact in facts if fact.property_name == "amount")
    risk_fact = next(
        fact for fact in facts if fact.property_name == "risk")
    assert risk_fact.derived_from == [amount_fact.id]


def test_each_ordered_update_has_matching_derived_facts_and_live_view(db):
    ontology_id = "action-derived-multi-update"
    object_type, instance = _seed_object(db, ontology_id)
    object_type.properties = [
        *object_type.properties,
        {"id": "amount", "name": "amount", "type": "number"},
        {
            "id": "risk", "name": "risk", "type": "number",
            "source": "computed", "functionId": "derive-multi-risk",
        },
    ]
    instance.properties = {
        **dict(instance.properties or {}),
        "amount": 5,
    }
    instance.computed = {"risk": 10}
    derive_risk = OntologyFunction(
        id="derive-multi-risk", ontology_id=ontology_id,
        name="derive_multi_risk", display_name="Derive multi risk",
        function_type="object", language="expression",
        target_object_type_id=object_type.id,
        parameters=[], return_type="number",
        body='object["amount"] * 2', enabled=True,
    )
    first_update = _update_rule("amount", value="9")
    first_update["order"] = 0
    second_update = _update_rule("amount", value="5")
    second_update["id"] = "restore-amount"
    second_update["order"] = 2
    action = ActionType(
        id="update-consume-restore", ontology_id=ontology_id,
        name="update_consume_restore",
        display_name="Update, consume, restore",
        object_type_id=object_type.id, parameters=[], rules=[
            first_update,
            {
                "id": "notify-intermediate-risk",
                "type": "notification",
                "name": "Notify intermediate risk",
                "enabled": True,
                "order": 1,
                "config": {
                    "channel": "internal",
                    "recipientSource": "constant",
                    "recipient": "ops",
                    "messageTemplate": "intermediate={{object.risk}}",
                },
            },
            second_update,
        ],
    )
    db.add_all([derive_risk, action])
    db.commit()

    result = execute_action(db, ontology_id, _body(action, instance))

    db.refresh(instance)
    assert result["status"] == "success", result
    assert instance.properties["amount"] == 5
    assert instance.computed["risk"] == 10
    assert db.query(Notification).filter_by(
        ontology_id=ontology_id).one().body == "intermediate=18"

    facts = db.query(PropertyFact).filter_by(
        ontology_id=ontology_id,
        instance_id=instance.id,
    ).all()
    amount_facts = sorted(
        (fact for fact in facts if fact.property_name == "amount"),
        key=lambda fact: fact.seq,
    )
    risk_facts = sorted(
        (fact for fact in facts if fact.property_name == "risk"),
        key=lambda fact: fact.seq,
    )
    assert [fact.value["v"] for fact in amount_facts] == [9, 5]
    assert [fact.value["v"] for fact in risk_facts] == [18, 10]
    assert risk_facts[0].derived_from == [amount_facts[0].id]
    assert risk_facts[1].derived_from == [amount_facts[1].id]
    assert amount_facts[1].supersedes_id == amount_facts[0].id
    assert risk_facts[1].supersedes_id == risk_facts[0].id
    assert {
        fact.caused_by for fact in [*amount_facts, *risk_facts]
    } == {result["id"]}


def test_nondeterministic_derived_value_is_evaluated_once_per_mutation(db):
    ontology_id = "action-derived-single-evaluation"
    object_type, instance = _seed_object(db, ontology_id)
    object_type.properties = [
        *object_type.properties,
        {
            "id": "evaluated-at", "name": "evaluated_at", "type": "string",
            "source": "computed", "functionId": "derive-current-time",
        },
    ]
    instance.computed = {"evaluated_at": "old"}
    current_time = OntologyFunction(
        id="derive-current-time", ontology_id=ontology_id,
        name="derive_current_time", display_name="Derive current time",
        function_type="object", language="expression",
        target_object_type_id=object_type.id,
        parameters=[], return_type="string",
        body="now()", enabled=True,
    )
    action = ActionType(
        id="update-and-report-time", ontology_id=ontology_id,
        name="update_and_report_time",
        display_name="Update and report time",
        object_type_id=object_type.id, parameters=[], rules=[
            _update_rule("status", value='"updated"'),
            {
                "id": "notify-time", "type": "notification",
                "name": "Notify time", "enabled": True, "order": 1,
                "config": {
                    "channel": "internal",
                    "recipientSource": "constant",
                    "recipient": "ops",
                    "messageTemplate": "at={{object.evaluated_at}}",
                },
            },
        ],
    )
    db.add_all([current_time, action])
    db.commit()

    result = execute_action(db, ontology_id, _body(action, instance))

    db.refresh(instance)
    derived_fact = db.query(PropertyFact).filter_by(
        ontology_id=ontology_id,
        instance_id=instance.id,
        property_name="evaluated_at",
    ).one()
    value = derived_fact.value["v"]
    assert result["status"] == "success", result
    assert instance.computed["evaluated_at"] == value
    assert db.query(Notification).filter_by(
        ontology_id=ontology_id).one().body == f"at={value}"


def test_failed_action_marks_rolled_back_notification_in_audit(db):
    ontology_id = "action-rollback-audit"
    object_type, instance = _seed_object(db, ontology_id)
    invalid_count_update = _update_rule(
        "count", value='"not-a-number"')
    invalid_count_update["id"] = "invalid-count"
    invalid_count_update["order"] = 2
    action = ActionType(
        id="notify-then-fail", ontology_id=ontology_id,
        name="notify_then_fail", display_name="Notify then fail",
        object_type_id=object_type.id, parameters=[], rules=[
            _update_rule("status", value='"updated"'),
            {
                "id": "notify", "type": "notification",
                "name": "Notify", "enabled": True, "order": 1,
                "config": {
                    "channel": "internal",
                    "recipientSource": "constant",
                    "recipient": "ops",
                    "messageTemplate": "status={{object.status}}",
                },
            },
            invalid_count_update,
        ],
    )
    db.add(action)
    db.commit()

    result = execute_action(db, ontology_id, _body(action, instance))

    db.refresh(instance)
    notification_effect = next(
        effect for effect in result["effects"]
        if effect["type"] == "notification")
    assert result["status"] == "failed"
    assert instance.properties["status"] == "new"
    assert db.query(Notification).filter_by(
        ontology_id=ontology_id).count() == 0
    assert db.query(PropertyFact).filter_by(
        ontology_id=ontology_id).count() == 0
    assert notification_effect["status"] == "rolled_back"
    assert notification_effect["rolledBack"] is True
    assert "未投递" in notification_effect["description"]


def test_dry_run_accumulates_multiple_updates_like_real_execution(db):
    ontology_id = "action-dry-run-cumulative"
    object_type, instance = _seed_object(db, ontology_id)
    object_type.properties = [
        *object_type.properties,
        {"id": "amount", "name": "amount", "type": "number"},
        {"id": "factor", "name": "factor", "type": "number"},
        {
            "id": "risk", "name": "risk", "type": "number",
            "source": "computed", "functionId": "derive-cumulative-risk",
        },
    ]
    instance.properties = {
        **dict(instance.properties or {}),
        "amount": 5,
        "factor": 2,
    }
    instance.computed = {"risk": 10}
    function = OntologyFunction(
        id="derive-cumulative-risk", ontology_id=ontology_id,
        name="derive_cumulative_risk", display_name="Cumulative risk",
        function_type="object", language="expression",
        target_object_type_id=object_type.id,
        parameters=[], return_type="number",
        body='object["amount"] * object["factor"]', enabled=True,
    )
    update_amount = _update_rule("amount", value="9")
    update_amount["order"] = 0
    update_factor = _update_rule("factor", value="3")
    update_factor["order"] = 1
    action = ActionType(
        id="cumulative-update", ontology_id=ontology_id,
        name="cumulative_update", display_name="Cumulative update",
        object_type_id=object_type.id, parameters=[], rules=[
            update_amount,
            update_factor,
            {
                "id": "notify-cumulative", "type": "notification",
                "name": "Notify cumulative", "enabled": True, "order": 2,
                "config": {
                    "channel": "internal",
                    "recipientSource": "constant",
                    "recipient": "ops",
                    "messageTemplate": (
                        "amount={{object.amount}} "
                        "factor={{object.factor}} risk={{object.risk}}"),
                },
            },
        ],
    )
    db.add_all([function, action])
    db.commit()

    dry = execute_action(
        db,
        ontology_id,
        SimpleNamespace(
            action_id=action.id,
            target_instance_id=instance.id,
            parameters={},
            dry_run=True,
        ),
    )

    db.refresh(instance)
    assert dry["status"] == "success", dry
    assert dry["effects"][1]["oldValue"] == 2
    assert dry["effects"][2]["message"] == (
        "amount=9 factor=3 risk=27")
    assert instance.properties["amount"] == 5
    assert instance.properties["factor"] == 2
    assert instance.computed["risk"] == 10

    real = execute_action(db, ontology_id, _body(action, instance))

    db.refresh(instance)
    assert real["status"] == "success", real
    assert real["effects"][2]["message"] == dry["effects"][2]["message"]
    assert instance.properties["amount"] == 9
    assert instance.properties["factor"] == 3
    assert instance.computed["risk"] == 27


def test_noop_update_does_not_invalidate_projection_or_emit_facts(db):
    ontology_id = "action-noop-projection"
    object_type, instance = _seed_object(db, ontology_id)
    object_type.properties = [
        *object_type.properties,
        {
            "id": "manual-score", "name": "manual_score",
            "type": "number", "source": "computed",
        },
    ]
    instance.computed = {"manual_score": 99}
    action = ActionType(
        id="noop-update", ontology_id=ontology_id,
        name="noop_update", display_name="No-op update",
        object_type_id=object_type.id, parameters=[],
        rules=[_update_rule("status", value='"new"')],
    )
    db.add(action)
    db.commit()

    result = execute_action(db, ontology_id, _body(action, instance))

    db.refresh(instance)
    assert result["status"] == "success", result
    assert result["effects"][0]["changed"] is False
    assert result["effects"][0]["inputFactIds"] == []
    assert result["effects"][0]["derivedFactCount"] == 0
    assert instance.computed == {"manual_score": 99}
    assert db.query(PropertyFact).filter_by(
        ontology_id=ontology_id).count() == 0


def test_computed_properties_cannot_be_written_as_stored_action_values(db):
    ontology_id = "action-computed-write"
    object_type, instance = _seed_object(db, ontology_id)
    object_type.properties = [
        *object_type.properties,
        {
            "id": "risk", "name": "risk", "type": "number",
            "source": "computed", "functionId": "derive-write-risk",
        },
    ]
    instance.computed = {"risk": 10}
    function = OntologyFunction(
        id="derive-write-risk", ontology_id=ontology_id,
        name="derive_write_risk", display_name="Derive write risk",
        function_type="object", language="expression",
        target_object_type_id=object_type.id,
        parameters=[], return_type="number",
        body='object["count"] * 2', enabled=True,
    )
    action = ActionType(
        id="write-computed", ontology_id=ontology_id,
        name="write_computed", display_name="Write computed",
        object_type_id=object_type.id, parameters=[],
        rules=[_update_rule("risk", value="999")],
    )
    db.add_all([function, action])
    db.commit()

    runtime = execute_action(db, ontology_id, _body(action, instance))
    static_errors = validate_action_definition(
        action, [object_type], [], [function])

    db.refresh(instance)
    assert runtime["status"] == "failed"
    assert "不能写入派生属性" in runtime["errorMessage"]
    assert any("不能写入派生属性" in error for error in static_errors)
    assert "risk" not in instance.properties
    assert instance.computed == {"risk": 10}
    assert db.query(PropertyFact).filter_by(
        ontology_id=ontology_id).count() == 0


def test_dry_run_tracks_virtual_objects_links_and_link_recipient(db):
    ontology_id = "action-dry-run-virtual-link"
    source_type, source = _seed_object(db, ontology_id)
    recipient_type = ObjectType(
        id="ot-virtual-recipient", ontology_id=ontology_id,
        name="VirtualRecipient", display_name="Virtual recipient",
        primary_key="id", properties=[
            {"id": "id", "name": "id", "type": "string",
             "required": True},
            {"id": "email", "name": "email", "type": "string",
             "required": True},
        ],
    )
    link_type = LinkType(
        id="lt-virtual-recipient", ontology_id=ontology_id,
        name="virtual_recipient", display_name="Virtual recipient",
        source_object_type_id=source_type.id,
        target_object_type_id=recipient_type.id,
        cardinality="many-to-one", properties=[],
    )
    action = ActionType(
        id="create-link-notify", ontology_id=ontology_id,
        name="create_link_notify", display_name="Create link and notify",
        object_type_id=source_type.id, parameters=[], rules=[
            {
                "id": "create-recipient", "type": "create_object",
                "name": "Create recipient", "enabled": True, "order": 0,
                "config": {
                    "targetObjectTypeId": recipient_type.id,
                    "propertyMappings": [{
                        "targetProperty": "email",
                        "sourceType": "constant",
                        "sourceValue": '"ops@example.com"',
                    }],
                },
            },
            {
                "id": "link-recipient", "type": "create_link",
                "name": "Link recipient", "enabled": True, "order": 1,
                "config": {
                    "linkTypeId": link_type.id,
                    "targetSource": "created_object",
                    "targetValue": recipient_type.id,
                },
            },
            {
                "id": "notify-recipient", "type": "notification",
                "name": "Notify recipient", "enabled": True, "order": 2,
                "config": {
                    "channel": "internal",
                    "recipientSource": "link",
                    "linkTypeId": link_type.id,
                    "recipientProperty": "email",
                    "messageTemplate": "virtual link resolved",
                },
            },
        ],
    )
    db.add_all([recipient_type, link_type, action])
    db.commit()

    dry = execute_action(
        db,
        ontology_id,
        SimpleNamespace(
            action_id=action.id,
            target_instance_id=source.id,
            parameters={},
            dry_run=True,
        ),
    )

    assert dry["status"] == "success", dry
    assert dry["effects"][2]["recipient"] == "ops@example.com"
    assert dry["effects"][2]["status"] == "preview"
    assert db.query(ObjectInstance).filter_by(
        ontology_id=ontology_id,
        object_type_id=recipient_type.id,
    ).count() == 0
    assert db.query(LinkInstance).filter_by(
        ontology_id=ontology_id,
        link_type_id=link_type.id,
    ).count() == 0
    assert db.query(Notification).filter_by(
        ontology_id=ontology_id).count() == 0

    real = execute_action(db, ontology_id, _body(action, source))

    assert real["status"] == "success", real
    assert real["effects"][2]["recipient"] == "ops@example.com"
    assert db.query(ObjectInstance).filter_by(
        ontology_id=ontology_id,
        object_type_id=recipient_type.id,
    ).count() == 1
    assert db.query(LinkInstance).filter_by(
        ontology_id=ontology_id,
        link_type_id=link_type.id,
    ).count() == 1
    assert db.query(Notification).filter_by(
        ontology_id=ontology_id).count() == 1


def test_each_webhook_rule_has_a_distinct_stable_delivery_key(db, monkeypatch):
    ontology_id = "webhook-rule-identity"
    object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id="multi-webhook", ontology_id=ontology_id,
        name="multi_webhook", display_name="Multi webhook",
        object_type_id=object_type.id, parameters=[], rules=[
            {
                "id": "partner-a", "type": "webhook", "name": "Partner A",
                "enabled": True, "order": 0,
                "config": {"url": "https://a.example/hooks", "method": "POST"},
            },
            {
                "id": "partner-b", "type": "webhook", "name": "Partner B",
                "enabled": True, "order": 1,
                "config": {"url": "https://b.example/hooks", "method": "POST"},
            },
        ],
    )
    db.add(action)
    db.commit()
    delivery_keys: list[str] = []

    def fake_request(session, method, url, **kwargs):
        delivery_keys.append(kwargs["headers"]["Idempotency-Key"])
        response = requests.Response()
        response.status_code = 204
        response.url = url
        response._content = b""
        return response

    monkeypatch.setattr(
        outbound_security.socket, "getaddrinfo", _resolve_public_outbound
    )
    monkeypatch.setattr(requests.Session, "request", fake_request)
    result = execute_action(
        db, ontology_id,
        _body(action, instance, idempotency_key="logical-step"),
    )

    assert result["status"] == "success"
    assert len(delivery_keys) == 2
    assert delivery_keys[0] != delivery_keys[1]
    assert all(":logical-step:" in key for key in delivery_keys)


def test_approval_execution_has_stable_key_and_failure_is_not_approved(db):
    ontology_id = "approval-execution-identity"
    object_type, instance = _seed_object(db, ontology_id)
    succeeds = ActionType(
        id="approval-succeeds", ontology_id=ontology_id,
        name="approval_succeeds", display_name="Approval succeeds",
        object_type_id=object_type.id, parameters=[],
        rules=[_update_rule("status", value='"approved"')],
        requires_approval=True,
    )
    no_effect = ActionType(
        id="approval-no-effect", ontology_id=ontology_id,
        name="approval_no_effect", display_name="Approval no effect",
        object_type_id=object_type.id, parameters=[], rules=[],
        requires_approval=True,
    )
    db.add_all([succeeds, no_effect])
    db.commit()
    release_id = _freeze_runtime_release(db, ontology_id)
    admin = SimpleNamespace(id="admin", username="admin", role="admin")

    pending_success = execute_action(
        db, ontology_id,
        _body(
            succeeds, instance,
            idempotency_key="proposal-success",
            expected_release_id=release_id,
        ),
    )
    approved = decide_pending_action(
        ontology_id, pending_success["id"],
        DecisionRequest(decision="approved", release_id=release_id), db, admin,
    )["data"]
    assert approved["pendingLog"]["status"] == "approved"
    assert approved["executionLog"]["status"] == "success"
    assert approved["executionLog"]["idempotencyKey"] == (
        f"approval-execution:{pending_success['id']}")

    pending_failure = execute_action(
        db, ontology_id,
        _body(
            no_effect, instance,
            idempotency_key="proposal-failure",
            expected_release_id=release_id,
        ),
    )
    failed = decide_pending_action(
        ontology_id, pending_failure["id"],
        DecisionRequest(decision="approved", release_id=release_id), db, admin,
    )["data"]
    assert failed["pendingLog"]["status"] == "failed"
    assert failed["executionLog"]["status"] == "failed"
    assert failed["pendingLog"]["errorMessage"]


def test_failed_approved_action_cannot_rollback_human_decision_audit(db):
    ontology_id = "approval-decision-isolation"
    _object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id="approval-runtime-failure",
        ontology_id=ontology_id,
        name="approval_runtime_failure",
        display_name="Approval runtime failure",
        object_type_id=instance.object_type_id,
        parameters=[],
        # Static definition is valid, but the resolved string violates the
        # target number property contract during real execution.  This reaches
        # execute_action's rollback path that previously erased the decision.
        rules=[_update_rule("count", value='"not-a-number"')],
        requires_approval=True,
    )
    db.add(action)
    db.commit()
    release_id = _freeze_runtime_release(db, ontology_id)
    admin = SimpleNamespace(id="audit-admin", username="audit-admin", role="admin")

    pending = execute_action(
        db,
        ontology_id,
        _body(
            action, instance,
            idempotency_key="decision-isolation-proposal",
            expected_release_id=release_id,
        ),
    )
    decided = decide_pending_action(
        ontology_id,
        pending["id"],
        DecisionRequest(
            decision="approved", reason="业务确认",
            release_id=release_id,
        ),
        db,
        admin,
    )["data"]

    proposal = db.query(ActionExecutionLog).filter_by(id=pending["id"]).one()
    failed_execution = db.query(ActionExecutionLog).filter_by(
        id=proposal.related_log_id).one()
    decision_facts = db.query(PropertyFact).filter_by(
        ontology_id=ontology_id,
        instance_id=proposal.id,
        property_name="decision",
        kind="decision",
    ).all()
    db.refresh(instance)

    assert proposal.status == "failed"
    assert proposal.decided_by == admin.id
    assert proposal.decided_at is not None
    assert proposal.decision_reason == "业务确认"
    assert failed_execution.status == "failed"
    assert decided["executionLog"]["id"] == failed_execution.id
    assert decided["decisionFactId"] == decision_facts[0].id
    assert len(decision_facts) == 1
    assert instance.properties["count"] == 0


def test_action_idempotency_replays_success_and_pending(db):
    ontology_id = "action-idempotency"
    object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id="increment", ontology_id=ontology_id,
        name="increment", display_name="Increment",
        object_type_id=object_type.id, parameters=[],
        rules=[_update_rule("count", source="expression", value="object.count + 1")],
    )
    pending_action = ActionType(
        id="approval", ontology_id=ontology_id,
        name="approval", display_name="Approval",
        object_type_id=object_type.id, parameters=[], rules=[],
        requires_approval=True,
    )
    db.add_all([action, pending_action])
    db.commit()

    first = execute_action(
        db, ontology_id,
        _body(action, instance, idempotency_key="idem-success",
              sentinel_match_state_id="state-1"),
    )
    second = execute_action(
        db, ontology_id,
        _body(action, instance, idempotency_key="idem-success",
              sentinel_match_state_id="state-1"),
    )
    db.refresh(instance)
    assert first["status"] == "success"
    assert second["status"] == "success" and second["idempotentReplay"] is True
    assert second["id"] == first["id"]
    assert instance.properties["count"] == 1

    pending_one = execute_action(
        db, ontology_id,
        _body(pending_action, instance, idempotency_key="idem-pending",
              sentinel_match_state_id="state-2"),
    )
    pending_two = execute_action(
        db, ontology_id,
        _body(pending_action, instance, idempotency_key="idem-pending",
              sentinel_match_state_id="state-2"),
    )
    assert pending_one["status"] == "pending"
    assert pending_two["id"] == pending_one["id"]
    assert pending_two["idempotentReplay"] is True
    assert db.query(ActionExecutionLog).filter_by(
        ontology_id=ontology_id, idempotency_key="idem-pending").count() == 1


def test_executing_sentinel_approval_replay_is_narrow_and_fail_closed(db):
    ontology_id = "executing-approval-replay-contract"
    object_type, instance = _seed_object(db, ontology_id)
    other = ObjectInstance(
        id="order-2",
        ontology_id=ontology_id,
        object_type_id=object_type.id,
        properties={
            "id": "order-2",
            "active": True,
            "count": 0,
            "status": "new",
        },
    )
    action = ActionType(
        id="approval-replay-action",
        ontology_id=ontology_id,
        name="approval_replay",
        display_name="Approval replay",
        object_type_id=object_type.id,
        parameters=[],
        rules=[_update_rule("status", value="\"approved\"")],
        requires_approval=True,
    )
    owner = ActionExecutionLog(
        id="executing-approval-owner",
        ontology_id=ontology_id,
        action_id=action.id,
        action_name=action.display_name,
        object_type_id=object_type.id,
        object_instance_id=instance.id,
        parameters={},
        status="executing",
        validation_errors=[],
        effects=[],
        dry_run=False,
        decided_by="approval-admin",
        decided_at=datetime.now(timezone.utc),
        idempotency_key="sentinel-approval-key",
        sentinel_match_state_id="approval-match-state",
        ontology_version="v1.0.0",
    )
    db.add_all([other, action, owner])
    db.commit()
    exact_body = _body(
        action,
        instance,
        idempotency_key=owner.idempotency_key,
        sentinel_match_state_id=owner.sentinel_match_state_id,
    )

    # ``executing`` by itself is not trusted as an approval checkpoint.
    missing_fact = execute_action(db, ontology_id, exact_body)
    assert missing_fact["status"] == "failed"
    assert missing_fact["validationErrors"] == ["idempotency_key_conflict"]

    record_decision_fact(
        db,
        ontology_id=ontology_id,
        action_log_id=owner.id,
        decision="APPROVED",
        source="user://approval-admin",
        actor_id="approval-admin",
        ontology_version=owner.ontology_version,
    )
    db.commit()

    replay = execute_action(db, ontology_id, exact_body)
    assert replay["id"] == owner.id
    assert replay["status"] == "pending"
    assert replay["pendingApproval"] is True
    assert replay["approvalExecuting"] is True
    assert replay["approvalLogStatus"] == "executing"
    assert replay["idempotentReplay"] is True
    assert replay["effects"] == []

    # Even a valid approval checkpoint cannot mask reuse by another target.
    mismatch = execute_action(
        db,
        ontology_id,
        _body(
            action,
            other,
            idempotency_key=owner.idempotency_key,
            sentinel_match_state_id=owner.sentinel_match_state_id,
        ),
    )
    assert mismatch["status"] == "failed"
    assert mismatch["validationErrors"] == [
        "idempotency_key_payload_mismatch",
    ]
    db.refresh(owner)
    assert owner.status == "executing"
    assert owner.related_log_id is None


def test_hitl_execution_cdc_reentry_is_pending_without_error_firing(
        db, monkeypatch):
    ontology_id = "hitl-cdc-executing-race"
    object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id="hitl-cdc-action",
        ontology_id=ontology_id,
        name="hitl_cdc_action",
        display_name="HITL CDC action",
        object_type_id=object_type.id,
        parameters=[],
        rules=[_update_rule("status", value="\"approved\"")],
        requires_approval=True,
    )
    sentinel = Sentinel(
        id="hitl-cdc-sentinel",
        ontology_id=ontology_id,
        name="hitl_cdc_sentinel",
        display_name="HITL CDC Sentinel",
        bindings=[{
            "alias": "a",
            "objectTypeId": object_type.id,
        }],
        links=[],
        condition="a.active == True",
        primary_alias="a",
        action_ids=[action.id],
        action_parameters={},
        trigger_mode="on_enter",
        muted=False,
        enabled=True,
        status="published",
    )
    db.add_all([action, sentinel])
    db.commit()
    release_id = _freeze_runtime_release(db, ontology_id)

    initial = evaluate_sentinel(
        db,
        ontology_id,
        sentinel,
        "manual",
        expected_release_id=release_id,
    )
    state = db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id,
    ).one()
    proposal = db.query(ActionExecutionLog).filter_by(
        ontology_id=ontology_id,
        action_id=action.id,
        status="pending",
    ).one()
    assert initial.status == "pending"
    assert state.runtime_status == "pending_enter"

    real_execute_action = formal_router.execute_action
    race_observation = {}
    RaceSession = sessionmaker(
        bind=db.get_bind(),
        expire_on_commit=False,
    )

    def execute_then_reenter_cdc(execution_db, *args, **kwargs):
        execution = real_execute_action(execution_db, *args, **kwargs)
        race_db = RaceSession()
        try:
            race_sentinel = race_db.query(Sentinel).filter_by(
                id=sentinel.id,
                ontology_id=ontology_id,
            ).one()
            firing = evaluate_sentinel(
                race_db,
                ontology_id,
                race_sentinel,
                "change",
                expected_release_id=release_id,
            )
            race_observation.update({
                "status": firing.status,
                "error": firing.error,
                "actionResults": list(firing.action_results or []),
            })
        finally:
            race_db.close()
        return execution

    monkeypatch.setattr(
        formal_router,
        "execute_action",
        execute_then_reenter_cdc,
    )
    admin = SimpleNamespace(
        id="approval-admin",
        username="approval-admin",
        role="admin",
    )
    decided = decide_pending_action(
        ontology_id,
        proposal.id,
        DecisionRequest(
            decision="approved",
            release_id=release_id,
        ),
        db,
        admin,
    )["data"]

    assert race_observation["status"] == "pending"
    assert race_observation["error"] is None
    assert race_observation["actionResults"] == [{
        "actionId": action.id,
        "targetInstanceId": instance.id,
        "edge": "enter",
        "matchKey": instance.id,
        "status": "pending",
        "logId": proposal.id,
        "idempotentReplay": True,
        "effects": [],
        "errorMessage": None,
        "validationErrors": [],
        "pendingApproval": True,
        "approvalExecuting": True,
        "approvalLogStatus": "executing",
    }]
    assert decided["pendingLog"]["status"] == "approved"
    assert decided["executionLog"]["status"] == "success"
    assert decided["sentinelResume"]["status"] == "fired"

    db.refresh(instance)
    state = db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id,
    ).one()
    firings = db.query(SentinelFiring).filter_by(
        ontology_id=ontology_id,
        sentinel_id=sentinel.id,
    ).all()
    conflict_logs = [
        log for log in db.query(ActionExecutionLog).filter_by(
            ontology_id=ontology_id,
        ).all()
        if "idempotency_key_conflict" in (log.validation_errors or [])
    ]
    assert instance.properties["status"] == "approved"
    assert state.runtime_status == "completed"
    assert all(firing.status != "error" for firing in firings)
    assert conflict_logs == []


def test_idempotent_replay_precedes_mutable_business_preconditions(db):
    ontology_id = "action-idempotency-precondition"
    object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id="guarded-increment", ontology_id=ontology_id,
        name="guarded_increment", display_name="Guarded increment",
        object_type_id=object_type.id, parameters=[], rules=[
            {
                "id": "initial-count", "type": "validation",
                "name": "Initial count", "enabled": True, "order": 0,
                "config": {
                    "condition": "object.count == 0",
                    "errorMessage": "count already changed",
                },
            },
            {
                **_update_rule("count", value="1"),
                "order": 1,
            },
        ],
    )
    db.add(action)
    db.commit()
    body = _body(
        action,
        instance,
        idempotency_key="stable-precondition-key",
    )

    first = execute_action(db, ontology_id, body)
    second = execute_action(db, ontology_id, body)

    db.refresh(instance)
    assert first["status"] == "success", first
    assert second["status"] == "success", second
    assert second["idempotentReplay"] is True
    assert second["id"] == first["id"]
    assert instance.properties["count"] == 1
    assert db.query(ActionExecutionLog).filter_by(
        ontology_id=ontology_id).count() == 1


def test_sentinel_multi_action_retry_does_not_repeat_successful_step(db):
    ontology_id = "sentinel-chain"
    object_type, instance = _seed_object(db, ontology_id)
    increment = ActionType(
        id="chain-increment", ontology_id=ontology_id,
        name="increment", display_name="Increment",
        object_type_id=object_type.id, parameters=[],
        rules=[_update_rule("count", source="expression", value="object.count + 1")],
    )
    downstream = ActionType(
        id="chain-downstream", ontology_id=ontology_id,
        name="downstream", display_name="Downstream",
        object_type_id=object_type.id, parameters=[], rules=[{
            "id": "unavailable", "type": "webhook", "name": "unavailable",
            "enabled": True, "order": 0,
            "config": {"url": "https://example.invalid"},
        }],
    )
    sentinel = Sentinel(
        id="sentinel-chain", ontology_id=ontology_id,
        name="chain", display_name="Chain",
        bindings=[{"alias": "a", "objectTypeId": object_type.id}], links=[],
        condition="a.active == True", primary_alias="a",
        action_ids=[increment.id, downstream.id], action_parameters={},
        trigger_mode="on_enter", muted=False, enabled=True, status="published",
    )
    db.add_all([increment, downstream, sentinel])
    db.commit()

    first = evaluate_sentinel(db, ontology_id, sentinel, "manual")
    state = db.query(SentinelMatchState).filter_by(sentinel_id=sentinel.id).one()
    first_state_id = state.id
    db.refresh(instance)
    assert first.status == "error"
    assert state.runtime_status == "failed_enter"
    assert instance.properties["count"] == 1

    downstream.rules = [_update_rule("status")]
    db.commit()
    second = evaluate_sentinel(db, ontology_id, sentinel, "manual")
    state = db.query(SentinelMatchState).filter_by(sentinel_id=sentinel.id).one()
    db.refresh(instance)
    assert second.status == "fired"
    assert state.id == first_state_id and state.runtime_status == "completed"
    assert instance.properties["count"] == 1
    assert instance.properties["status"] == "done"
    assert second.action_results[0]["idempotentReplay"] is True


def test_sentinel_pending_reuses_approval_and_rejection_releases_claim(db):
    ontology_id = "sentinel-pending"
    object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id="pending-action", ontology_id=ontology_id,
        name="pending", display_name="Pending",
        object_type_id=object_type.id, parameters=[], rules=[],
        requires_approval=True,
    )
    sentinel = Sentinel(
        id="sentinel-pending", ontology_id=ontology_id,
        name="pending", display_name="Pending",
        bindings=[{"alias": "a", "objectTypeId": object_type.id}], links=[],
        condition="a.active == True", primary_alias="a",
        action_ids=[action.id], action_parameters={}, trigger_mode="on_enter",
        muted=False, enabled=True, status="published",
    )
    db.add_all([action, sentinel])
    db.commit()

    first = evaluate_sentinel(db, ontology_id, sentinel, "manual")
    second = evaluate_sentinel(db, ontology_id, sentinel, "manual")
    state = db.query(SentinelMatchState).filter_by(sentinel_id=sentinel.id).one()
    pending_logs = db.query(ActionExecutionLog).filter_by(
        ontology_id=ontology_id, status="pending").all()
    assert first.status == "pending" and second.status == "pending"
    assert state.runtime_status == "pending_enter"
    assert len(pending_logs) == 1
    assert second.action_results[0]["idempotentReplay"] is True

    pending_logs[0].status = "rejected"
    pending_logs[0].idempotency_key = None
    db.commit()
    released = reject_sentinel_match_claim(db, ontology_id, state.id)
    assert released["status"] == "released"
    assert db.query(SentinelMatchState).filter_by(sentinel_id=sentinel.id).count() == 0


def test_approved_sentinel_step_resumes_remaining_chain_once(db):
    ontology_id = "sentinel-approval-resume"
    object_type, instance = _seed_object(db, ontology_id)
    increment = ActionType(
        id="approval-increment", ontology_id=ontology_id,
        name="increment", display_name="Increment",
        object_type_id=object_type.id, parameters=[],
        rules=[_update_rule("count", source="expression", value="object.count + 1")],
    )
    approval = ActionType(
        id="approval-gate", ontology_id=ontology_id,
        name="approval", display_name="Approval",
        object_type_id=object_type.id, parameters=[],
        rules=[_update_rule("status", value="\"approved\"")],
        requires_approval=True,
    )
    finish = ActionType(
        id="approval-finish", ontology_id=ontology_id,
        name="finish", display_name="Finish",
        object_type_id=object_type.id, parameters=[],
        rules=[_update_rule("status")],
    )
    sentinel = Sentinel(
        id="sentinel-approval-resume", ontology_id=ontology_id,
        name="approval_resume", display_name="Approval resume",
        bindings=[{"alias": "a", "objectTypeId": object_type.id}], links=[],
        condition="a.active == True", primary_alias="a",
        action_ids=[increment.id, approval.id, finish.id], action_parameters={},
        trigger_mode="on_enter", muted=False, enabled=True, status="published",
    )
    db.add_all([increment, approval, finish, sentinel])
    db.commit()

    firing = evaluate_sentinel(db, ontology_id, sentinel, "manual")
    state = db.query(SentinelMatchState).filter_by(sentinel_id=sentinel.id).one()
    pending = db.query(ActionExecutionLog).filter_by(
        ontology_id=ontology_id, action_id=approval.id, status="pending").one()
    db.refresh(instance)
    assert firing.status == "pending"
    # Approval is a gate for the whole chain: no earlier automatic side effect
    # is committed while a later step is still awaiting a human decision.
    assert instance.properties["count"] == 0
    assert instance.properties["status"] == "new"

    actual = execute_action(
        db, ontology_id,
        _body(
            approval, instance,
            sentinel_match_state_id=state.id,
        ),
        skip_approval=True,
    )
    assert actual["status"] == "success"
    pending.status = "approved"
    pending.related_log_id = actual["id"]
    db.commit()

    resumed = resume_sentinel_match_claim(db, ontology_id, state.id)
    db.refresh(instance)
    state = db.query(SentinelMatchState).filter_by(sentinel_id=sentinel.id).one()
    assert resumed["status"] == "fired"
    assert state.runtime_status == "completed"
    assert instance.properties["count"] == 1
    assert instance.properties["status"] == "done"
    assert [item["idempotentReplay"] for item in resumed["actionResults"][:2]] \
        == [False, True]


def test_match_state_has_database_uniqueness_guard(db):
    one = SentinelMatchState(
        id="state-one", ontology_id="unique", sentinel_id="sentinel",
        match_key="same", match_detail={}, runtime_status="completed",
    )
    two = SentinelMatchState(
        id="state-two", ontology_id="unique", sentinel_id="sentinel",
        match_key="same", match_detail={}, runtime_status="completed",
    )
    db.add(one)
    db.commit()
    db.add(two)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_pending_approval_cannot_cross_ontology_version(db):
    ontology_id = "approval-release-binding"
    object_type, instance = _seed_object(db, ontology_id)
    action = ActionType(
        id="approval-release-action", ontology_id=ontology_id,
        name="approve_me", display_name="Approve me",
        object_type_id=object_type.id, parameters=[], rules=[],
        requires_approval=True,
    )
    db.add(action)
    db.commit()

    pending = execute_action(db, ontology_id, _body(action, instance))
    assert pending["status"] == "pending"
    assert pending["ontologyVersion"] == "v1.0.0"

    project = db.query(OntologyProject).filter_by(id=ontology_id).one()
    project.version = "v2.0.0"
    db.commit()
    admin = SimpleNamespace(id="admin", username="admin", role="admin")

    with pytest.raises(HTTPException) as stale:
        decide_pending_action(
            ontology_id, pending["id"],
            DecisionRequest(decision="approved"), db, admin)
    assert stale.value.status_code == 409
    # Rejection remains possible so stale work can be closed without effects.
    rejected = decide_pending_action(
        ontology_id, pending["id"],
        DecisionRequest(decision="rejected", reason="stale release"), db, admin)
    assert rejected["data"]["status"] == "rejected"


# Celery 退役：任务注册同一性契约测试随 celery_app 删除一并移除。
