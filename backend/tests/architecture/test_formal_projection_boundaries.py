"""Protect the pure-contract boundary around Formal projection."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.ontologies.mappings import formal_projection as facade
from app.ontologies.mappings import formal_projection_contract as contract
from app.services.v2.mapping import formal_projection as legacy


BACKEND_DIR = Path(__file__).resolve().parents[2]
MAPPING_DIR = BACKEND_DIR / "app" / "ontologies" / "mappings"
FACADE_PATH = MAPPING_DIR / "formal_projection.py"
CONTRACT_PATH = MAPPING_DIR / "formal_projection_contract.py"

EXTRACTED_CONTRACT_FUNCTIONS = {
    "projection_property_mappings",
    "_stable_id",
    "stable_pipeline_entity_id",
    "stable_object_instance_id",
    "stable_pipeline_relation_id",
    "stable_link_instance_id",
    "_infer_property_type",
    "_pick_first",
    "_property_data_binding",
    "_coerce_props_to_type",
    "_display_value",
    "_merge_properties",
    "_build_object_type_properties",
}

PUBLIC_COMPAT_FUNCTIONS = {
    "projection_property_mappings",
    "stable_pipeline_entity_id",
    "stable_object_instance_id",
    "stable_pipeline_relation_id",
    "stable_link_instance_id",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _absolute_imports(path: Path) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
        ):
            imports.add(node.module)
    return imports


def test_formal_projection_reexports_contract_functions_by_identity():
    for name in EXTRACTED_CONTRACT_FUNCTIONS:
        assert getattr(facade, name) is getattr(contract, name)


def test_legacy_projection_facade_preserves_public_patch_seams():
    assert (
        legacy.project_to_formal_ontology
        is facade.project_to_formal_ontology
    )
    for name in PUBLIC_COMPAT_FUNCTIONS:
        assert getattr(legacy, name) is getattr(facade, name)


def test_projection_entrypoint_signature_and_transaction_ownership_stay_stable():
    signature = inspect.signature(facade.project_to_formal_ontology)
    assert tuple(signature.parameters) == (
        "db",
        "ontology_id",
        "mapping_meta",
        "ontology_release_id",
    )
    assert signature.parameters["mapping_meta"].default is None
    release = signature.parameters["ontology_release_id"]
    assert release.kind is inspect.Parameter.KEYWORD_ONLY
    assert release.default is None

    entrypoint = next(
        node for node in _tree(FACADE_PATH).body
        if isinstance(node, ast.FunctionDef)
        and node.name == "project_to_formal_ontology"
    )
    transaction_calls = {
        node.func.attr
        for node in ast.walk(entrypoint)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback"}
    }
    assert not transaction_calls


def test_materializer_keeps_only_the_database_projection_entrypoint():
    direct_functions = {
        node.name for node in _tree(FACADE_PATH).body
        if isinstance(node, ast.FunctionDef)
    }
    contract_functions = {
        node.name for node in _tree(CONTRACT_PATH).body
        if isinstance(node, ast.FunctionDef)
    }
    assert direct_functions == {"project_to_formal_ontology"}
    assert contract_functions == EXTRACTED_CONTRACT_FUNCTIONS


def test_projection_contract_remains_pure_and_dependency_light():
    imports = _absolute_imports(CONTRACT_PATH)
    forbidden_prefixes = (
        "app.models",
        "app.services",
        "app.data_channel",
        "app.ontologies.formal_modeling",
        "app.ontologies.mappings.formal_projection",
        "sqlalchemy",
    )
    violations = sorted(
        module for module in imports
        if any(
            module == prefix or module.startswith(f"{prefix}.")
            for prefix in forbidden_prefixes
        )
    )
    assert not violations


def test_formal_projection_modules_stay_bounded():
    limits = {
        FACADE_PATH: 900,
        CONTRACT_PATH: 400,
    }
    for path, maximum in limits.items():
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count < maximum, (
            f"{path.relative_to(BACKEND_DIR)} has grown to {line_count} lines; "
            "extract a cohesive contract or materialization policy"
        )
