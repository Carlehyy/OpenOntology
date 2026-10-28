"""Keep the ontology mapping HTTP adapter thin and contract-compatible."""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path

from fastapi.params import Depends

from app.ontologies.mappings import entity_mapping_workflow
from app.ontologies.mappings import link_mapping_workflow
from app.ontologies.mappings import materialization_workflow
from app.ontologies.mappings import query_service
from app.ontologies.mappings import request_validation
from app.ontologies.mappings import router as mapping_router


BACKEND_DIR = Path(__file__).resolve().parents[2]
MAPPING_DIR = BACKEND_DIR / "app" / "ontologies" / "mappings"

ROUTE_PARAMETERS = {
    "suggest_mapping": ("ontology_id", "body", "db"),
    # 远程绑定（binding_mode=remote）需要认证用户执行服务端冻结/授权验证，
    # 三个定义入口因此带 current_user（HTTP 路径/请求体不变，仅新增鉴权依赖）。
    "create_mapping": ("ontology_id", "body", "db", "current_user"),
    "update_mapping": (
        "ontology_id", "mapping_id", "body", "db", "current_user"),
    "delete_mapping": ("ontology_id", "mapping_id", "db"),
    "list_mappings": ("ontology_id", "db"),
    "apply_mapping": ("ontology_id", "mapping_id", "data", "db"),
    "apply_mapping_from_dataset": ("ontology_id", "mapping_id", "db"),
    "build_all_mappings": ("ontology_id", "db"),
    "create_link_mapping": ("ontology_id", "body", "db", "current_user"),
    "list_link_mappings": ("ontology_id", "db"),
    "update_link_mapping_automation": (
        "ontology_id",
        "link_mapping_id",
        "body",
        "db",
    ),
    "delete_link_mapping": ("ontology_id", "link_mapping_id", "db"),
}

DELEGATES = {
    "suggest_mapping": ("_query_service", "suggest_mapping"),
    "create_mapping": ("_entity_workflow", "create_mapping"),
    "update_mapping": ("_entity_workflow", "update_mapping"),
    "delete_mapping": ("_entity_workflow", "delete_mapping"),
    "list_mappings": ("_query_service", "list_mappings"),
    "apply_mapping": ("_materialization_workflow", "reject_raw_apply"),
    "apply_mapping_from_dataset": (
        "_materialization_workflow",
        "apply_mapping_from_dataset",
    ),
    "build_all_mappings": ("_materialization_workflow", "build_all_mappings"),
    "create_link_mapping": ("_link_workflow", "create_link_mapping"),
    "list_link_mappings": ("_query_service", "list_link_mappings"),
    "update_link_mapping_automation": (
        "_link_workflow",
        "update_link_mapping_automation",
    ),
    "delete_link_mapping": ("_link_workflow", "delete_link_mapping"),
}

VALIDATION_ALIASES = (
    "_assert_client_primary_key_matches",
    "_assert_ignored_fields_do_not_hide_identity",
    "_assert_link_mapping_types_compatible",
    "_assert_mapping_types_compatible",
    "_canonical_primary_key",
    "_dataset_column_types",
    "_lock_ontology",
    "_mapping_types_compatible",
    "_normal_mapping_type",
    "_reject_reserved_mapping_keys",
    "_require_draft_ontology",
    "_validate_link_version_automation_policy",
    "_validate_target_type",
    "_validate_user_field_mapping",
    "_validate_version_automation_policy",
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    return imports


def test_mapping_route_signatures_and_request_models_remain_stable():
    expected_body_types = {
        "suggest_mapping": mapping_router.SuggestRequest,
        "create_mapping": mapping_router.CreateMappingRequest,
        "update_mapping": mapping_router.UpdateMappingRequest,
        "create_link_mapping": mapping_router.LinkMappingCreate,
        "update_link_mapping_automation": (
            mapping_router.LinkMappingPolicyUpdate
        ),
    }
    for name, expected_parameters in ROUTE_PARAMETERS.items():
        parameters = inspect.signature(
            getattr(mapping_router, name),
            eval_str=True,
        ).parameters
        assert tuple(parameters) == expected_parameters
        assert isinstance(parameters["db"].default, Depends)
        if name in expected_body_types:
            assert (
                parameters["body"].annotation
                is expected_body_types[name]
            )


def test_router_keeps_existing_validation_import_contracts():
    for name in VALIDATION_ALIASES:
        assert (
            getattr(mapping_router, name)
            is getattr(request_validation, name)
        )


def test_mapping_http_handlers_only_delegate():
    path = MAPPING_DIR / "router.py"
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


def test_mapping_application_modules_do_not_depend_on_http_router():
    modules = (
        entity_mapping_workflow,
        link_mapping_workflow,
        materialization_workflow,
        query_service,
        request_validation,
    )
    for module in modules:
        imports = _imports(Path(module.__file__))
        assert "app.ontologies.mappings.router" not in imports
        assert "app.routers.v2.mappings" not in imports
        assert "fastapi.routing" not in imports


def test_mapping_router_and_application_modules_stay_bounded():
    limits = {
        "router.py": 330,
        "request_validation.py": 600,
        "query_service.py": 220,
        "entity_mapping_workflow.py": 620,
        "materialization_workflow.py": 150,
        "link_mapping_workflow.py": 500,
    }
    for filename, maximum in limits.items():
        line_count = len(
            (MAPPING_DIR / filename)
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert line_count < maximum


def test_mapping_openapi_contract_matches_pre_extraction_baseline():
    from app.main import app

    prefixes = (
        "/api/v2/ontologies/{ontology_id}/mappings",
        "/api/v2/ontologies/{ontology_id}/link-mappings",
    )
    paths = {
        path: value
        for path, value in app.openapi()["paths"].items()
        if path.startswith(prefixes)
    }
    payload = json.dumps(
        paths,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    assert len(paths) == 11
    assert sum(len(item) for item in paths.values()) == 14
    assert hashlib.sha256(payload).hexdigest() == (
        "9e1986643ee418230351c4721d08c8ae2ac44196cb6047cd67c47f409d6e5c6b"
    )
