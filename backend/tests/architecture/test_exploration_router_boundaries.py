"""Protect Exploration HTTP, workflow, and compatibility boundaries."""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path

from fastapi import HTTPException

from app.exploration import (
    application_service,
    attachment_service,
    canvas,
    converter,
    document,
    document_service,
    draft_service,
    readiness,
    router as exploration_router,
    schemas,
    session_service,
    streaming_service,
    workspace,
)
from app.exploration import orchestrator
from app.model_configs import selector
from app.ontologies import access, release_context
from app.ontologies.versions import release_service


BACKEND_DIR = Path(__file__).resolve().parents[2]
EXPLORATION_DIR = BACKEND_DIR / "app" / "exploration"
ROUTER_PATH = EXPLORATION_DIR / "router.py"
SERVICE_PATHS = (
    EXPLORATION_DIR / "session_service.py",
    EXPLORATION_DIR / "attachment_service.py",
    EXPLORATION_DIR / "streaming_service.py",
    EXPLORATION_DIR / "document_service.py",
    EXPLORATION_DIR / "draft_service.py",
    EXPLORATION_DIR / "application_service.py",
)

DELEGATES = {
    "list_sessions": ("_session_service", "list_sessions"),
    "create_session": ("_session_service", "create_session"),
    "list_draft_ontologies": ("_session_service", "list_draft_bindable_ontologies"),
    "get_session": ("_session_service", "get_session"),
    "delete_session": ("_session_service", "delete_session"),
    "get_canvas": ("_session_service", "get_canvas"),
    "get_readiness": ("_session_service", "get_readiness"),
    "get_ontology_preview": ("_session_service", "get_ontology_preview"),
    "get_diagram": ("_session_service", "get_diagram"),
    "list_diagram_kinds": (
        "_session_service",
        "list_diagram_kinds",
    ),
    "list_attachments": (
        "_attachment_service",
        "list_attachments",
    ),
    "upload_attachment": (
        "_attachment_service",
        "upload_attachment",
    ),
    "create_workspace_text_file": (
        "_attachment_service",
        "create_workspace_text_file",
    ),
    "get_workspace_text_file": (
        "_attachment_service",
        "get_workspace_text_file",
    ),
    "preview_workspace_file": (
        "_attachment_service",
        "preview_workspace_file",
    ),
    "update_workspace_text_file": (
        "_attachment_service",
        "update_workspace_text_file",
    ),
    "download_workspace_file": (
        "_attachment_service",
        "download_workspace_file",
    ),
    "delete_attachment": (
        "_attachment_service",
        "delete_attachment",
    ),
    "chat": ("_streaming_service", "chat"),
    "create_document": (
        "_document_service",
        "create_document",
    ),
    "list_documents": (
        "_document_service",
        "list_documents",
    ),
    "get_document": ("_document_service", "get_document"),
    "create_draft": ("_draft_service", "create_draft"),
    "list_drafts": ("_draft_service", "list_drafts"),
    "get_draft": ("_draft_service", "get_draft"),
    "validate_draft": ("_draft_service", "validate_draft"),
    "apply_draft": (
        "_application_service",
        "apply_draft",
    ),
    "discard_draft": ("_draft_service", "discard_draft"),
}

PRIVATE_SIGNATURES = {
    "_ok": ("data",),
    "_require_session": ("db", "session_id", "current_user"),
    "_session_out": ("s",),
    "_message_out": ("message", "canvas"),
    "_document_out": ("document", "session", "list_item"),
    "_remove_attachment_file": ("path",),
    "_attachment_out": ("a",),
    "_require_document": ("db", "document_id", "current_user"),
    "_require_draft": ("db", "draft_id", "current_user"),
}


def _decorated_handlers(tree: ast.Module) -> dict[str, ast.AST]:
    handlers = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router"
            for decorator in node.decorator_list
        ):
            handlers[node.name] = node
    return handlers


def _single_return_call(node: ast.AST) -> ast.Call:
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    assert len(body) == 1
    assert isinstance(body[0], ast.Return)
    value = body[0].value
    if isinstance(value, ast.Await):
        value = value.value
    assert isinstance(value, ast.Call)
    return value


def test_all_exploration_handlers_are_named_service_adapters():
    source = ROUTER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(ROUTER_PATH))
    handlers = _decorated_handlers(tree)

    assert set(handlers) == set(DELEGATES)
    for name, (module_name, function_name) in DELEGATES.items():
        call = _single_return_call(handlers[name])
        assert isinstance(call.func, ast.Attribute)
        assert isinstance(call.func.value, ast.Name)
        assert (
            call.func.value.id,
            call.func.attr,
        ) == (module_name, function_name)
        assert not any(
            isinstance(descendant, (ast.If, ast.For, ast.While, ast.Try))
            for descendant in ast.walk(handlers[name])
        )

    assert ".query(" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert ".flush(" not in source
    # 尺寸绊线随 ontology-preview 只读路由上调；继续增长应先拆子路由而非再抬线
    assert len(source.splitlines()) <= 640


def test_service_modules_never_import_exploration_router():
    for path in SERVICE_PATHS:
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "app.exploration.router" not in imported


def test_router_keeps_historical_imports_and_private_signatures():
    assert exploration_router.HTTPException is HTTPException
    assert exploration_router.C is canvas
    assert exploration_router.R is readiness
    assert exploration_router.S is schemas
    assert exploration_router.W is workspace
    assert exploration_router.converter is converter
    assert (
        exploration_router.run_exploration_turn
        is orchestrator.run_exploration_turn
    )
    assert (
        exploration_router.select_llm_model_config
        is selector.select_llm_model_config
    )
    assert exploration_router.llm_call_kwargs is selector.llm_call_kwargs
    assert (
        exploration_router.document_source_state
        is document.document_source_state
    )
    assert (
        exploration_router.generate_document
        is document.generate_document
    )
    assert (
        exploration_router.require_ontology_access
        is access.require_ontology_access
    )
    assert (
        exploration_router.require_ontology_create_access
        is access.require_ontology_create_access
    )
    assert (
        exploration_router.create_initial_release
        is release_context.create_initial_release
    )
    assert (
        exploration_router.resolve_current_release
        is release_service.resolve_current_release
    )
    assert (
        exploration_router.collect_publishable_snapshot
        is release_service.collect_publishable_snapshot
    )

    for name, parameters in PRIVATE_SIGNATURES.items():
        helper = getattr(exploration_router, name)
        assert tuple(inspect.signature(helper).parameters) == parameters


def test_compatibility_dependencies_are_resolved_at_call_time(
    monkeypatch,
):
    marker = object()
    monkeypatch.setattr(
        exploration_router,
        "select_llm_model_config",
        marker,
    )
    assert (
        exploration_router._document_dependencies()[
            "select_llm_model_config_fn"
        ]
        is marker
    )

    monkeypatch.setattr(
        exploration_router,
        "document_source_state",
        marker,
    )
    assert (
        exploration_router._draft_dependencies()[
            "document_source_state_fn"
        ]
        is marker
    )

    monkeypatch.setattr(
        exploration_router,
        "resolve_current_release",
        marker,
    )
    assert (
        exploration_router._application_dependencies()[
            "resolve_current_release_fn"
        ]
        is marker
    )


def test_representative_router_adapter_passes_private_patch_seams(
    monkeypatch,
):
    expected = object()
    database = object()
    user = object()

    def list_sessions(db, current_user, **kwargs):
        assert db is database
        assert current_user is user
        assert kwargs["session_out_fn"] is exploration_router._session_out
        assert kwargs["ok_fn"] is exploration_router._ok
        return expected

    monkeypatch.setattr(
        session_service,
        "list_sessions",
        list_sessions,
    )
    assert exploration_router.list_sessions(database, user) is expected


def test_transaction_and_file_cleanup_order_stays_explicit():
    apply_source = inspect.getsource(application_service.apply_draft)
    ordered_apply_steps = (
        "validate_draft_selection(",
        "db.flush()",
        "resolve_current_release_fn(",
        "converter_module.apply_draft(",
        "create_initial_release_fn(",
        'draft.status = "applied"',
        "db.commit()",
    )
    positions = [
        apply_source.index(step)
        for step in ordered_apply_steps
    ]
    assert positions == sorted(positions)
    assert apply_source.count("db.commit()") == 1
    assert "db.rollback()" not in apply_source

    delete_source = inspect.getsource(session_service.delete_session)
    ordered_delete_steps = (
        "remove_attachment_file_fn(attachment.file_path)",
        ").delete()",
        "db.delete(session)",
        "db.commit()",
    )
    positions = [
        delete_source.index(step)
        for step in ordered_delete_steps
    ]
    assert positions == sorted(positions)
    assert delete_source.count("db.commit()") == 1


def test_exploration_openapi_fingerprint_is_stable():
    from app.main import app

    paths = {
        path: item
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/v2/exploration")
    }
    operations = sum(
        method in {"get", "post", "put", "patch", "delete"}
        for item in paths.values()
        for method in item
    )
    payload = json.dumps(
        paths,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    # MYW-68 业务澄清分支②入口：新增 GET /exploration/draft-ontologies（读契约）。
    # 工作台本体预览投影：新增 GET /exploration/sessions/{id}/ontology-preview（只读）。
    assert len(paths) == 23
    assert operations == 28
    assert hashlib.sha256(payload).hexdigest() == (
        "031ee8534714492bb2149c28fc5fc8f577d57482a2f1a960b2e479b6dd34483e"
    )
