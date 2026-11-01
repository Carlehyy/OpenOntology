"""Protect release-gate extraction and historical patch compatibility."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

from app.ontologies.formal_modeling import action_engine
from app.ontologies.versions import release_activation_service
from app.ontologies.versions import release_gate_service
from app.ontologies.versions import release_service
from app.ontologies.versions import router as version_router


_COMPATIBILITY_SIGNATURES = {
    "_raise_publish_errors": ["errors", "message"],
    "_validate_sentinels": [
        "sentinels",
        "object_types",
        "link_types",
        "actions",
    ],
    "_validate_production_mappings": [
        "db",
        "ontology_id",
        "mappings",
        "link_mappings",
        "instances",
        "object_types",
    ],
    "_release_errors": ["db", "ontology_id"],
    "_rebuild_required_query_projections": ["db", "ontology_id", "structure_commit"],
    "_snapshot_sentinel": ["item"],
    "_next_release_activation_number": ["db", "ontology_id"],
    "_snapshot_sentinel_models": ["snapshot"],
    "_invalidate_dynamic_sentinels_for_release": [
        "db",
        "ontology_id",
        "release_id",
    ],
}

_THIN_ADAPTER_TARGETS = {
    "_raise_publish_errors": (
        "release_gate_service",
        "raise_publish_errors",
    ),
    "_validate_sentinels": (
        "release_gate_service",
        "validate_sentinels",
    ),
    "_validate_production_mappings": (
        "release_gate_service",
        "validate_production_mappings",
    ),
    "_release_errors": (
        "release_gate_service",
        "release_errors",
    ),
    "_rebuild_required_query_projections": (
        "release_activation_service",
        "rebuild_required_query_projections",
    ),
    "_snapshot_sentinel": (
        "release_service",
        "snapshot_release_sentinel",
    ),
    "_next_release_activation_number": (
        "release_activation_service",
        "next_release_activation_number",
    ),
    "_snapshot_sentinel_models": (
        "release_service",
        "snapshot_sentinel_models",
    ),
    "_invalidate_dynamic_sentinels_for_release": (
        "release_activation_service",
        "invalidate_dynamic_sentinels_for_release",
    ),
}


def _functions(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    return imported


def _return_call(node: ast.FunctionDef) -> ast.Call:
    returns = [
        statement.value
        for statement in node.body
        if isinstance(statement, ast.Return)
    ]
    assert len(returns) == 1
    assert isinstance(returns[0], ast.Call)
    return returns[0]


def _gate_error_codes(function) -> list[str]:
    tree = ast.parse(inspect.getsource(function))
    codes: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "gate_error"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                codes.append(node.args[0].value)
            self.generic_visit(node)

    Visitor().visit(tree)
    return codes


def test_release_helpers_are_thin_compatibility_adapters():
    functions = _functions(Path(version_router.__file__).resolve())

    for name, parameters in _COMPATIBILITY_SIGNATURES.items():
        assert list(inspect.signature(
            getattr(version_router, name),
        ).parameters) == parameters

        call = _return_call(functions[name])
        assert isinstance(call.func, ast.Attribute)
        assert isinstance(call.func.value, ast.Name)
        expected_module, expected_function = _THIN_ADAPTER_TARGETS[name]
        assert call.func.value.id == expected_module
        assert call.func.attr == expected_function
        assert not any(isinstance(node, ast.For) for node in ast.walk(
            functions[name]
        ))


def test_release_services_use_canonical_dependencies_without_router_cycle():
    gate_imports = _imports(
        Path(release_gate_service.__file__).resolve(),
    )
    activation_imports = _imports(
        Path(release_activation_service.__file__).resolve(),
    )

    for imported in (gate_imports, activation_imports):
        assert "app.ontologies.versions.router" not in imported
        assert all(not module.startswith("app.models") for module in imported)

    assert (
        "app.data_channel.datasets.automation_policy"
        in gate_imports
    )
    assert "app.data_channel.datasets.version_events" not in gate_imports
    assert (
        "app.ontologies.mappings.mapping_service"
        in activation_imports
    )


def test_release_gate_keeps_error_code_order():
    assert _gate_error_codes(
        release_gate_service.validate_production_mappings,
    ) == [
        "production_mapping_required",
        "instance_lake_lineage_missing",
        "instance_object_type_mapping_missing",
        "mapping_not_applied",
        "mapping_dataset_not_found",
        "mapping_dataset_version_missing",
        "mapping_dataset_version_unverifiable",
        "dataset_latest_pointer_stale",
        "latest_dataset_version_not_approved",
        "mapping_curated_automation_not_subscribed",
        "mapping_manual_dataset_not_governed",
        "mapping_manual_automation_not_subscribed",
        "mapping_applied_version_stale",
        "link_mapping_not_active",
        "link_mapping_dataset_missing",
        "link_mapping_dataset_not_found",
        "link_mapping_dataset_version_missing",
        "link_mapping_version_unverifiable",
        "link_mapping_version_not_approved",
        "link_mapping_manual_dataset_not_governed",
        "link_mapping_manual_automation_not_subscribed",
        "link_mapping_latest_pointer_stale",
        "link_mapping_applied_version_stale",
    ]
    assert _gate_error_codes(release_gate_service.release_errors) == [
        "invalid_action_definition",
        "object_type_required",
        "enabled_typescript_function_forbidden",
        "mapping_object_type_not_found",
    ]


def test_release_errors_resolve_settings_and_action_validation_at_call_time(
    monkeypatch,
):
    captured: dict = {}

    def fake_release_errors(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return [{"code": "patched"}]

    dependency_names = (
        "validate_model",
        "validate_expression_function_contract",
        "validate_builtin_sentinel_contract",
        "_snapshot_sentinel",
        "_validate_sentinels",
        "_validate_production_mappings",
        "_gate_error",
    )
    dependencies = {name: object() for name in dependency_names}
    for name, marker in dependencies.items():
        monkeypatch.setattr(version_router, name, marker)

    settings_marker = SimpleNamespace(environment="patched-environment")
    action_validator_marker = object()
    monkeypatch.setattr(version_router, "settings", settings_marker)
    monkeypatch.setattr(
        action_engine,
        "validate_action_definition",
        action_validator_marker,
    )
    monkeypatch.setattr(
        release_gate_service,
        "release_errors",
        fake_release_errors,
    )
    db_marker = object()

    result = version_router._release_errors(db_marker, "ontology-id")

    assert result == [{"code": "patched"}]
    assert captured["args"] == (db_marker, "ontology-id")
    assert captured["kwargs"] == {
        "environment": "patched-environment",
        "action_definition_validator": action_validator_marker,
        "model_validator": dependencies["validate_model"],
        "expression_function_validator": (
            dependencies["validate_expression_function_contract"]
        ),
        "builtin_sentinel_validator": (
            dependencies["validate_builtin_sentinel_contract"]
        ),
        "sentinel_snapshotter": dependencies["_snapshot_sentinel"],
        "sentinel_validator": dependencies["_validate_sentinels"],
        "production_mapping_validator": (
            dependencies["_validate_production_mappings"]
        ),
        "gate_error": dependencies["_gate_error"],
    }


def test_activation_and_snapshot_adapters_forward_current_dependencies(
    monkeypatch,
):
    captured: dict[str, tuple] = {}

    def fake_next(*args, **kwargs):
        captured["next"] = (args, kwargs)
        return "v-marker"

    def fake_models(*args, **kwargs):
        captured["models"] = (args, kwargs)
        return ["sentinel-marker"]

    number_marker = object()
    snapshot_marker = object()
    monkeypatch.setattr(
        version_router,
        "next_release_number",
        number_marker,
    )
    monkeypatch.setattr(
        version_router,
        "complete_snapshot",
        snapshot_marker,
    )
    monkeypatch.setattr(
        release_activation_service,
        "next_release_activation_number",
        fake_next,
    )
    monkeypatch.setattr(
        release_service,
        "snapshot_sentinel_models",
        fake_models,
    )
    db_marker = object()
    snapshot = {"sentinels": []}

    assert version_router._next_release_activation_number(
        db_marker,
        "ontology-id",
    ) == "v-marker"
    assert version_router._snapshot_sentinel_models(
        snapshot,
    ) == ["sentinel-marker"]
    assert captured["next"] == (
        (db_marker, "ontology-id"),
        {"number_allocator": number_marker},
    )
    assert captured["models"] == (
        (snapshot,),
        {"snapshot_completer": snapshot_marker},
    )


def test_projection_rebuild_reports_neo4j_readiness():
    events: list[tuple] = []
    db_marker = object()

    class FakeMappingService:
        def __init__(self, db):
            events.append(("init", db))

        def _rebuild_neo4j_projection(self, ontology_id):
            events.append(("neo4j", ontology_id))
            return True

    result = release_activation_service.rebuild_required_query_projections(
        db_marker,
        "ontology-id",
        mapping_service_factory=FakeMappingService,
    )

    # 实例 ready 语义不变；结构投影是探索视图：假 db 连上下文都无法
    # 解析属 SQL 权威层拒绝（rejected，fence 不动），不是构建失败；
    # 两种形态都不阻断发布主流程。
    assert result["ready"] is True
    assert result["neo4j"] == "ok"
    assert result["structure"]["status"] == "rejected"
    assert result["structure"]["error"]
    assert events == [
        ("init", db_marker),
        ("neo4j", "ontology-id"),
    ]


def test_new_release_services_do_not_own_sql_transactions():
    for module in (
        release_gate_service,
        release_activation_service,
    ):
        tree = ast.parse(
            Path(module.__file__).read_text(encoding="utf-8"),
        )
        transaction_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"commit", "rollback", "flush"}
            )
        }
        assert transaction_calls == set()
