"""Protect the versions workspace service and HTTP-adapter boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from app.ontologies.versions import router as version_router
from app.ontologies.versions import workspace_service


_WORKSPACE_EXPORTS = (
    "_MAPPING_AUTOMATION_POLICY_KEYS",
    "_canvas_node_ids",
    "_current_release",
    "_diff_formal",
    "_draft_or_404",
    "_ensure_editable_draft",
    "_json_safe",
    "_mapping_workspace_payload",
    "_snapshot_formal",
    "_trial_payload",
    "_validate_workspace_mapping_policy_types",
    "_validated_canvas_positions",
    "_version_payload",
    "_with_canvas_layout",
    "_workspace_mode",
    "_workspace_payload",
)

_WORKSPACE_ENDPOINTS = (
    "list_versions",
    "get_current_release_workspace",
    "get_current_release_mappings",
    "get_version_tree",
    "create_draft_version",
    "delete_draft_version",
    "get_version_workspace",
    "save_canvas_layout",
    "save_draft_workspace",
    "get_draft_mappings",
    "save_draft_mappings",
    "get_draft_impact",
    "get_version_semantic",
    "save_release_document",
)


def test_versions_router_reexports_workspace_helpers_by_identity():
    for name in _WORKSPACE_EXPORTS:
        assert getattr(version_router, name) is getattr(
            workspace_service,
            name,
        )


def test_workspace_service_never_imports_versions_router():
    service_path = Path(workspace_service.__file__).resolve()
    tree = ast.parse(
        service_path.read_text(encoding="utf-8"),
        filename=str(service_path),
    )
    forbidden = "app.ontologies.versions.router"
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    assert forbidden not in imported


def test_workspace_http_endpoints_remain_thin_named_adapters():
    router_path = Path(version_router.__file__).resolve()
    tree = ast.parse(
        router_path.read_text(encoding="utf-8"),
        filename=str(router_path),
    )
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }

    for name in _WORKSPACE_ENDPOINTS:
        endpoint = getattr(version_router, name)
        assert endpoint.__name__ == name
        node = functions[name]
        executable = [
            statement
            for statement in node.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
        ]
        assert len(executable) == 1, name
        assert isinstance(executable[0], ast.Return), name
        call = executable[0].value
        assert isinstance(call, ast.Call), name
        assert isinstance(call.func, ast.Attribute), name
        assert isinstance(call.func.value, ast.Name), name
        assert call.func.value.id == "workspace_service", name
        assert call.func.attr == name, name
