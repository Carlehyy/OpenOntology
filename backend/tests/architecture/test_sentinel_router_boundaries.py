"""Protect the Sentinel HTTP adapter and its compatibility seams."""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path

from fastapi.params import Depends

from app.ontologies.sentinels import definition_workflow
from app.ontologies.sentinels import operational_workflow
from app.ontologies.sentinels import project_guard
from app.ontologies.sentinels import query_service
from app.ontologies.sentinels import router as sentinel_router
from app.ontologies.sentinels import skill_export
from app.routers import sentinel as legacy_router


BACKEND_DIR = Path(__file__).resolve().parents[2]
SENTINEL_DIR = BACKEND_DIR / "app" / "ontologies" / "sentinels"

ROUTE_PARAMETERS = {
    "list_sentinels": ("ontology_id", "release_id", "db", "_"),
    "create_sentinel": ("ontology_id", "body", "db", "_"),
    "run": ("ontology_id", "db", "_"),
    "list_firings": (
        "ontology_id",
        "sentinel_id",
        "limit",
        "release_id",
        "include_history",
        "db",
        "_",
    ),
    "list_notifications": (
        "ontology_id",
        "limit",
        "release_id",
        "include_history",
        "db",
        "_",
    ),
    "get_cdc_status": (
        "ontology_id",
        "release_id",
        "include_history",
        "db",
        "_",
    ),
    "update_operational_state": (
        "ontology_id",
        "sentinel_id",
        "body",
        "db",
        "_",
    ),
    "get_sentinel": ("ontology_id", "sentinel_id", "db", "_"),
    "export_sentinel_skill": ("ontology_id", "sentinel_id", "db", "_"),
    "update_sentinel": (
        "ontology_id",
        "sentinel_id",
        "body",
        "db",
        "_",
    ),
    "delete_sentinel": ("ontology_id", "sentinel_id", "db", "_"),
    "toggle_sentinel": ("ontology_id", "sentinel_id", "db", "_"),
}

DELEGATES = {
    "list_sentinels": ("_query_service", "list_sentinels"),
    "create_sentinel": ("_definition_workflow", "create_sentinel"),
    "run": ("_operational_workflow", "run"),
    "list_firings": ("_query_service", "list_firings"),
    "list_notifications": ("_query_service", "list_notifications"),
    "get_cdc_status": ("_operational_workflow", "get_cdc_status"),
    "update_operational_state": (
        "_operational_workflow",
        "update_operational_state",
    ),
    "get_sentinel": ("_query_service", "get_sentinel"),
    "export_sentinel_skill": ("_skill_export", "export_sentinel_skill"),
    "update_sentinel": ("_definition_workflow", "update_sentinel"),
    "delete_sentinel": ("_definition_workflow", "delete_sentinel"),
    "toggle_sentinel": ("_operational_workflow", "toggle_sentinel"),
}

HELPER_KEYWORDS = {
    "list_sentinels": {
        "current_release_context_fn",
        "released_dict_fn",
    },
    "create_sentinel": {"require_draft_fn", "dict_fn"},
    "run": {"run_manual_fn"},
    "list_firings": {"current_release_context_fn"},
    "list_notifications": {"current_release_context_fn"},
    "get_cdc_status": {"project_fn", "sessionmaker_fn"},
    "update_operational_state": {
        "sentinel_write_fence_fn",
        "project_fn",
        "current_release_context_fn",
        "released_dict_fn",
    },
    "get_sentinel": {"dict_fn"},
    "export_sentinel_skill": set(),
    "update_sentinel": {
        "sentinel_write_fence_fn",
        "project_fn",
        "dict_fn",
    },
    "delete_sentinel": {
        "sentinel_write_fence_fn",
        "require_draft_fn",
    },
    "toggle_sentinel": {
        "sentinel_write_fence_fn",
        "project_fn",
    },
}


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    return imports


def test_sentinel_route_signatures_and_request_models_remain_stable():
    body_types = {
        "create_sentinel": sentinel_router.SentinelIn,
        "update_operational_state": (
            sentinel_router.SentinelOperationalUpdate
        ),
        "update_sentinel": sentinel_router.SentinelUpdate,
    }
    for name, expected_parameters in ROUTE_PARAMETERS.items():
        parameters = inspect.signature(
            getattr(sentinel_router, name)
        ).parameters
        assert tuple(parameters) == expected_parameters
        assert isinstance(parameters["db"].default, Depends)
        assert isinstance(parameters["_"].default, Depends)
        if name in body_types:
            assert parameters["body"].annotation is body_types[name]


def test_sentinel_router_reexports_existing_helper_contracts():
    assert sentinel_router._project is project_guard._project
    assert sentinel_router._require_draft is project_guard._require_draft
    assert sentinel_router._dict is query_service._dict
    assert sentinel_router._released_dict is query_service._released_dict


def test_legacy_sentinel_facade_preserves_public_object_identity():
    for name in (
        "router",
        "SentinelIn",
        "SentinelUpdate",
        "SentinelOperationalUpdate",
        *ROUTE_PARAMETERS,
    ):
        assert getattr(legacy_router, name) is getattr(sentinel_router, name)


def test_sentinel_http_handlers_only_delegate_with_compatibility_helpers():
    path = SENTINEL_DIR / "router.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name, (module_name, function_name) in DELEGATES.items():
        function = functions[name]
        executable = [
            statement
            for statement in function.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
        ]
        assert len(executable) == 1
        statement = executable[0]
        assert isinstance(statement, ast.Return)
        assert isinstance(statement.value, ast.Call)
        assert isinstance(statement.value.func, ast.Attribute)
        assert isinstance(statement.value.func.value, ast.Name)
        assert statement.value.func.value.id == module_name
        assert statement.value.func.attr == function_name
        assert {
            keyword.arg
            for keyword in statement.value.keywords
        } == HELPER_KEYWORDS[name]


def test_operational_wrapper_resolves_write_fence_at_request_time(
    monkeypatch,
):
    database = object()
    body = object()
    expected = {"data": {"id": "sentinel-1"}}
    fence = object()
    project = object()
    release_context = object()
    released_dict = object()
    received = {}

    def fake_update(*args, **kwargs):
        received["args"] = args
        received["kwargs"] = kwargs
        return expected

    monkeypatch.setattr(
        sentinel_router,
        "_sentinel_write_fence",
        fence,
    )
    monkeypatch.setattr(sentinel_router, "_project", project)
    monkeypatch.setattr(
        sentinel_router,
        "current_release_context",
        release_context,
    )
    monkeypatch.setattr(
        sentinel_router,
        "_released_dict",
        released_dict,
    )
    monkeypatch.setattr(
        operational_workflow,
        "update_operational_state",
        fake_update,
    )

    result = sentinel_router.update_operational_state(
        "ontology-1",
        "sentinel-1",
        body,
        db=database,
        _=None,
    )

    assert result is expected
    assert received["args"] == (
        "ontology-1",
        "sentinel-1",
        body,
        database,
    )
    assert received["kwargs"] == {
        "sentinel_write_fence_fn": fence,
        "project_fn": project,
        "current_release_context_fn": release_context,
        "released_dict_fn": released_dict,
    }


def test_sentinel_application_modules_do_not_depend_on_http_router():
    for module in (
        definition_workflow,
        operational_workflow,
        project_guard,
        query_service,
        skill_export,
    ):
        imports = _imports(Path(module.__file__))
        assert "app.ontologies.sentinels.router" not in imports
        assert "app.routers.sentinel" not in imports
        assert "fastapi.routing" not in imports


def test_sentinel_router_and_application_modules_stay_bounded():
    limits = {
        "router.py": 330,
        "project_guard.py": 60,
        "query_service.py": 350,
        "definition_workflow.py": 180,
        "operational_workflow.py": 280,
        "skill_export.py": 560,
    }
    for filename, maximum in limits.items():
        line_count = len(
            (SENTINEL_DIR / filename)
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert line_count < maximum


def test_sentinel_openapi_contract_matches_pre_extraction_baseline():
    from app.main import app

    prefix = "/api/v1/ontologies/{ontology_id}/sentinels"
    paths = {
        path: value
        for path, value in app.openapi()["paths"].items()
        if path.startswith(prefix)
    }
    payload = json.dumps(
        paths,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    assert len(paths) == 9
    assert sum(len(item) for item in paths.values()) == 12
    assert hashlib.sha256(payload).hexdigest() == (
        "2204b421275a1055140cb2ca08518fec8f2bd4edf8152d3cca37ff798e00c336"
    )
