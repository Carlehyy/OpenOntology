"""Protect Agent Runtime HTTP contracts and application-service boundaries."""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.params import Depends, Query

from app.ontologies.agent_runtime import (
    application_errors,
    chat_service,
    conversation_service,
    dynamic_workflow,
    graph_queries,
    profile_service,
    proposal_service,
    report_service,
    router as agent_router,
    schemas,
)


AGENT_DIR = Path(agent_router.__file__).resolve().parent

ROUTE_PARAMETERS = {
    "get_profile": ("ontology_id", "db", "_"),
    "update_profile": ("ontology_id", "body", "db", "_"),
    "get_capabilities": ("ontology_id", "release_id", "db", "_"),
    "list_dynamic_sentinels": (
        "ontology_id", "release_id", "db", "current_user",
    ),
    "create_dynamic_sentinel": (
        "ontology_id", "body", "db", "current_user",
    ),
    "execute_dynamic_sentinel_proposal": (
        "ontology_id", "body", "db", "current_user",
    ),
    "update_dynamic_sentinel": (
        "ontology_id", "sentinel_id", "body", "db", "current_user",
    ),
    "trial_dynamic_sentinel": (
        "ontology_id", "sentinel_id", "body", "db", "current_user",
    ),
    "toggle_dynamic_sentinel": (
        "ontology_id", "sentinel_id", "body", "db", "current_user",
    ),
    "delete_dynamic_sentinel": (
        "ontology_id", "sentinel_id", "release_id", "expected_revision",
        "db", "current_user",
    ),
    "get_agent_graph": (
        "ontology_id", "depth", "query", "object_type",
        "focus_instance_id", "limit_per_type", "release_id", "db", "_",
    ),
    "get_agent_graph_instance": (
        "ontology_id", "instance_id", "release_id", "db", "_",
    ),
    "query_agent_graph_paths": ("ontology_id", "body", "db", "_"),
    "query_agent_graph_impact": ("ontology_id", "body", "db", "_"),
    "list_report_templates": ("ontology_id", "db", "current_user"),
    "create_ai_report_template": (
        "ontology_id", "body", "db", "current_user",
    ),
    "get_report_template": (
        "ontology_id", "template_id", "db", "current_user",
    ),
    "update_report_template": (
        "ontology_id", "template_id", "body", "db", "current_user",
    ),
    "delete_report_template": (
        "ontology_id", "template_id", "db", "current_user",
    ),
    "preview_report_template": (
        "ontology_id", "template_id", "body", "db", "current_user",
    ),
    "publish_report_template": (
        "ontology_id", "template_id", "db", "current_user",
    ),
    "run_published_report": (
        "ontology_id", "template_id", "body", "db", "current_user",
    ),
    "list_report_runs": (
        "ontology_id", "template_id", "db", "current_user",
    ),
    "get_report_run": ("ontology_id", "run_id", "db", "current_user"),
    "get_report_html": ("ontology_id", "run_id", "db", "current_user"),
    "chat": ("ontology_id", "body", "db", "current_user"),
    "cancel_chat": ("ontology_id", "body", "db", "current_user"),
    "get_chat_run": ("ontology_id", "run_id", "db", "current_user"),
    "list_conversations": (
        "ontology_id", "db", "release_id", "current_user",
    ),
    "get_conversation": (
        "ontology_id", "conversation_id", "db", "current_user",
    ),
    "export_conversation": (
        "ontology_id", "conversation_id", "db", "current_user",
    ),
    "delete_conversation": (
        "ontology_id", "conversation_id", "db", "current_user",
    ),
    "execute_proposal": ("ontology_id", "body", "db", "current_user"),
}

BODY_TYPES = {
    "update_profile": schemas.AgentProfileUpdate,
    "create_dynamic_sentinel": schemas.DynamicSentinelCreateRequest,
    "execute_dynamic_sentinel_proposal": schemas.DynamicSentinelProposalCommand,
    "update_dynamic_sentinel": schemas.DynamicSentinelUpdateRequest,
    "trial_dynamic_sentinel": schemas.DynamicSentinelReleaseRequest,
    "toggle_dynamic_sentinel": schemas.DynamicSentinelToggleRequest,
    "query_agent_graph_paths": schemas.GraphPathRequest,
    "query_agent_graph_impact": schemas.GraphImpactRequest,
    "create_ai_report_template": schemas.ReportTemplateAIDraftRequest,
    "update_report_template": schemas.ReportTemplateUpdate,
    "preview_report_template": schemas.ReportRunRequest,
    "run_published_report": schemas.ReportRunRequest,
    "chat": schemas.ChatRequest,
    "cancel_chat": schemas.ChatCancelRequest,
    "execute_proposal": schemas.ExecuteProposalRequest,
}


def test_agent_runtime_route_signatures_remain_stable():
    assert len(agent_router.router.routes) == 33
    for name, expected in ROUTE_PARAMETERS.items():
        parameters = inspect.signature(
            getattr(agent_router, name), eval_str=True
        ).parameters
        assert tuple(parameters) == expected
        for dependency_name in ("db", "current_user", "_"):
            if dependency_name in parameters:
                assert isinstance(parameters[dependency_name].default, Depends)
        if name in BODY_TYPES:
            assert parameters["body"].annotation is BODY_TYPES[name]

    assert isinstance(
        inspect.signature(
            agent_router.list_dynamic_sentinels
        ).parameters["release_id"].default,
        Query,
    )
    delete_parameters = inspect.signature(
        agent_router.delete_dynamic_sentinel
    ).parameters
    assert isinstance(delete_parameters["release_id"].default, Query)
    assert isinstance(delete_parameters["expected_revision"].default, Query)


def test_agent_runtime_compatibility_helpers_remain_available():
    assert agent_router._profile_out is profile_service.profile_out
    assert agent_router._template_out is report_service.template_out
    assert agent_router._run_out is report_service.run_out
    assert agent_router._message_out is conversation_service.message_out
    assert agent_router._PROFILE_FIELDS is profile_service.PROFILE_FIELDS
    assert agent_router._RESETTABLE is profile_service.RESETTABLE_FIELDS
    for name in (
        "_ok",
        "_require_ontology",
        "_require_report_template",
        "_require_report_run",
        "_require_conversation",
        "_dynamic_context_scope",
    ):
        assert callable(getattr(agent_router, name))


def test_application_errors_are_mapped_only_at_router_boundary():
    failures = (
        ("not_found", 404, "missing"),
        ("forbidden", 403, "denied"),
        ("conflict", 409, {"code": "conflict"}),
        ("invalid", 422, "invalid"),
    )
    for kind, status, detail in failures:
        def fail(kind=kind, detail=detail):
            raise application_errors.AgentRuntimeApplicationError(kind, detail)

        with pytest.raises(HTTPException) as caught:
            agent_router._application_call(fail)
        assert (caught.value.status_code, caught.value.detail) == (
            status,
            detail,
        )


def test_router_patch_seams_are_resolved_at_request_time(monkeypatch):
    marker = object()
    seen = {}
    profile = object()

    monkeypatch.setattr(agent_router, "_require_ontology", lambda *_: marker)
    # chat/cancel/run 状态/提案执行端点走按本体访问裁决的模块级缝隙。
    monkeypatch.setattr(agent_router, "require_ontology_access", lambda *args, **kwargs: marker)
    monkeypatch.setattr(agent_router, "get_or_create_profile", marker)
    monkeypatch.setattr(agent_router, "_profile_out", lambda row: row)

    def update(db, ontology_id, body, *, get_profile_fn,
               profile_fields, resettable_fields):
        seen["profile"] = (
            db, ontology_id, body, get_profile_fn,
            profile_fields, resettable_fields,
        )
        return profile

    monkeypatch.setattr(profile_service, "update_profile", update)
    body = schemas.AgentProfileUpdate()
    db = object()
    assert agent_router.update_profile("ontology-1", body, db, object()) == {
        "data": profile,
    }
    assert seen["profile"] == (
        db, "ontology-1", body, marker,
        agent_router._PROFILE_FIELDS, agent_router._RESETTABLE,
    )

    turn = object()
    monkeypatch.setattr(agent_router, "run_agent_turn", turn)

    def run_sync(*args, run_turn_fn):
        seen["chat"] = (args, run_turn_fn)
        return {"ok": True}

    monkeypatch.setattr(chat_service, "run_sync", run_sync)
    chat_body = schemas.ChatRequest(message="inspect seams", stream=False)
    agent_router.chat("ontology-1", chat_body, db, object())
    assert seen["chat"][1] is turn


def test_toggle_dynamic_sentinel_resolves_release_context_once(monkeypatch):
    db = object()
    current_user = object()
    context = object()
    scope = object()
    context_calls = []
    toggle_calls = []

    monkeypatch.setattr(
        agent_router,
        "require_ontology_access",
        lambda *_args, **_kwargs: None,
    )

    def context_scope(received_db, ontology_id, release_id):
        context_calls.append((received_db, ontology_id, release_id))
        return context, scope

    def toggle(
        received_db,
        received_context,
        received_scope,
        sentinel_id,
        body,
        *,
        dynamic_service_module,
    ):
        toggle_calls.append((
            received_db,
            received_context,
            received_scope,
            sentinel_id,
            body,
            dynamic_service_module,
        ))
        return {"enabled": body.enabled}

    monkeypatch.setattr(agent_router, "_dynamic_context_scope", context_scope)
    monkeypatch.setattr(dynamic_workflow, "toggle", toggle)
    body = schemas.DynamicSentinelToggleRequest(
        release_id="release-1",
        enabled=True,
        expected_revision=3,
    )

    assert agent_router.toggle_dynamic_sentinel(
        "ontology-1",
        "sentinel-1",
        body,
        db,
        current_user,
    ) == {"data": {"enabled": True}}
    assert context_calls == [(db, "ontology-1", "release-1")]
    assert toggle_calls == [(
        db,
        context,
        scope,
        "sentinel-1",
        body,
        agent_router.dynamic_service,
    )]


def test_service_modules_do_not_depend_on_fastapi():
    modules = (
        application_errors,
        chat_service,
        conversation_service,
        dynamic_workflow,
        graph_queries,
        profile_service,
        proposal_service,
        report_service,
    )
    for module in modules:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(name == "fastapi" or name.startswith("fastapi.") for name in imports)


def test_router_has_no_application_transaction_calls():
    tree = ast.parse(
        (AGENT_DIR / "router.py").read_text(encoding="utf-8")
    )
    forbidden = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "db"
            and node.func.attr in {"add", "commit", "delete", "refresh", "rollback"}
        ):
            forbidden.append(node.func.attr)
    assert forbidden == []


def test_report_delete_preserves_transaction_order():
    order = []
    row = SimpleNamespace(id="template-1", status="draft")

    class Query:
        def filter(self, *_args):
            return self

        def delete(self):
            order.append("runs")

    class Database:
        def query(self, _model):
            return Query()

        def delete(self, value):
            assert value is row
            order.append("template")

        def commit(self):
            order.append("commit")

    report_service.delete_template(Database(), row)
    assert order == ["runs", "template", "commit"]


def test_report_publish_queries_the_matching_preview_once():
    conditions = []
    run = SimpleNamespace(
        status="succeeded",
        quality_report={"passed": True},
    )
    row = SimpleNamespace(
        id="template-1",
        status="draft",
        revision=4,
        last_preview_revision=4,
        last_preview_run_id="run-1",
        published_at=None,
    )

    class Query:
        def filter(self, *received_conditions):
            conditions.extend(received_conditions)
            return self

        def first(self):
            return run

    class Database:
        def query(self, model):
            assert model is report_service.AnalysisReportRun
            return Query()

        def commit(self):
            pass

        def refresh(self, value):
            assert value is row

    published_at = object()
    assert report_service.publish_template(
        Database(),
        row,
        now_fn=lambda: published_at,
    ) is row
    assert len(conditions) == 2
    assert row.status == "published"
    assert row.published_at is published_at


def test_conversation_delete_preserves_transaction_order():
    order = []
    conversation = SimpleNamespace(id="conversation-1")

    class Query:
        def __init__(self, label):
            self.label = label

        def filter(self, *_args):
            return self

        def delete(self):
            order.append(self.label)

    class Database:
        calls = 0

        def query(self, _model):
            self.calls += 1
            return Query("simulations" if self.calls == 1 else "messages")

        def delete(self, value):
            assert value is conversation
            order.append("conversation")

        def commit(self):
            order.append("commit")

    conversation_service.delete_conversation(Database(), conversation)
    assert order == ["simulations", "messages", "conversation", "commit"]


def test_agent_runtime_modules_stay_bounded():
    maximum_lines = {
        "router.py": 620,
        "limits.py": 160,
        "answer_verifier.py": 160,
        "chat_cancel.py": 80,
        "application_errors.py": 60,
        "profile_service.py": 100,
        "graph_queries.py": 130,
        "dynamic_workflow.py": 200,
        # 130: stream_events 桥接执行线程与 SSE 推送（MYW-71 断开不中断回合）
        "chat_service.py": 130,
        "proposal_service.py": 100,
        "report_service.py": 350,
        "conversation_service.py": 240,
    }
    for name, maximum in maximum_lines.items():
        count = len(
            (AGENT_DIR / name).read_text(encoding="utf-8").splitlines()
        )
        assert count <= maximum, f"{name} grew to {count} lines (max {maximum})"


def test_agent_runtime_openapi_contract_is_stable():
    from app.main import app

    def normalize(value):
        if isinstance(value, dict):
            return {
                key: normalize(item)
                for key, item in value.items()
                if key != "operationId"
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    paths = {
        path: operations
        for path, operations in app.openapi()["paths"].items()
        if "/agent/" in path
    }
    payload = json.dumps(
        paths,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert len(paths) == 28
    assert sum(
        method in {"get", "post", "put", "patch", "delete"}
        for operations in paths.values()
        for method in operations
    ) == 36
    assert hashlib.sha256(payload.encode()).hexdigest() == (
        "6d131eb117b8cbdbc1b11de9d055ad1062641854146527205a565e4d9d98457a"
    )
    normalized_payload = json.dumps(
        normalize(paths),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert hashlib.sha256(normalized_payload.encode()).hexdigest() == (
        "23c6afcf09809bb141426a833f858a1d99e9f055537904e50715fdcb712ddb97"
    )
