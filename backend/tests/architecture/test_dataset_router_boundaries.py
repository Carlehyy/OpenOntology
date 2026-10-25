"""Protect dataset contracts/services and the thin HTTP-adapter boundary."""
from __future__ import annotations

import ast
from pathlib import Path

from app.data_channel.datasets import (
    manual_contract,
    mutation_service,
    query_service,
    router as dataset_router,
)


BACKEND_DIR = Path(__file__).resolve().parents[2]
DATASET_DIR = BACKEND_DIR / "app" / "data_channel" / "datasets"
ROUTER_PATH = DATASET_DIR / "router.py"
SERVICE_PATHS = (
    DATASET_DIR / "mutation_service.py",
    DATASET_DIR / "query_service.py",
)

CONTRACT_NAMES = (
    "DatasetResponse",
    "ContractRequest",
    "CreateTableRequest",
    "TableColumnDef",
    "RowEditOp",
    "RowEditsRequest",
)

ENDPOINT_NAMES = (
    "upload_dataset",
    "start_dataset_import",
    "get_dataset_import",
    "commit_dataset_import_job",
    "create_online_table",
    "upload_dataset_version",
    "declare_contract",
    "edit_rows",
    "datasets_overview",
    "overview_summary",
    "dataset_consumers",
    "delete_dataset",
    "list_datasets",
    "get_dataset",
    "list_versions",
    "preview_data",
    "get_schema",
    "export_dataset",
    "get_stats",
    "preview_dataset",
)


def _top_level_functions(
    tree: ast.Module,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _non_docstring_body(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.stmt]:
    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def test_dataset_router_reexports_contracts_and_helpers_by_identity():
    for name in CONTRACT_NAMES:
        assert getattr(dataset_router, name) is getattr(
            manual_contract,
            name,
        )

    assert (
        dataset_router.MANUAL_FIELD_KEY_RE
        is manual_contract.MANUAL_FIELD_KEY_RE
    )
    assert (
        dataset_router.MANUAL_FIELD_CONTRACT_VERSION
        == manual_contract.MANUAL_FIELD_CONTRACT_VERSION
    )
    assert (
        dataset_router._build_manual_schema
        is manual_contract.build_manual_schema
    )
    assert (
        dataset_router._serialize_manual_contract_rows
        is manual_contract.serialize_manual_contract_rows
    )
    assert (
        dataset_router._normalize_manual_contract_upload
        is manual_contract.normalize_manual_contract_upload
    )
    assert (
        dataset_router._validate_manual_rows
        is manual_contract.validate_manual_rows
    )
    assert (
        dataset_router._require_manual_dataset
        is manual_contract.require_manual_dataset
    )
    assert (
        dataset_router._check_upload_file
        is mutation_service.check_upload_file
    )
    assert (
        dataset_router._check_manual_import_extension
        is mutation_service.check_manual_import_extension
    )
    assert (
        dataset_router._estimate_rowcount
        is mutation_service.estimate_rowcount
    )
    assert (
        dataset_router._require_curated_preview_approved
        is query_service.require_curated_preview_approved
    )


def test_dispatch_wrapper_resolves_router_logger_and_settings_at_call_time(
    monkeypatch,
):
    from app.config import settings

    patched_logger = object()
    expected = {"execution_mode": "patched"}
    received = {}

    def fake_dispatch(
        job_id,
        **kwargs,
    ):
        received["args"] = (job_id,)
        received["kwargs"] = kwargs
        return expected

    monkeypatch.setattr(dataset_router, "logger", patched_logger)
    monkeypatch.setattr(
        mutation_service,
        "dispatch_dataset_import_task",
        fake_dispatch,
    )

    result = dataset_router._dispatch_dataset_import_task(
        "job-1",
        kind="inspect",
        operation="解析",
    )

    assert result is expected
    assert received["args"] == ("job-1",)
    assert received["kwargs"] == {
        "kind": "inspect",
        "operation": "解析",
        "settings_obj": settings,
        "logger_obj": patched_logger,
    }


def test_persist_wrapper_resolves_router_patch_targets_at_call_time(
    monkeypatch,
):
    estimate = object()
    normalize = object()
    validate = object()
    consumers = object()
    expected = object()
    received = {}

    monkeypatch.setattr(dataset_router, "_estimate_rowcount", estimate)
    monkeypatch.setattr(
        dataset_router,
        "_normalize_manual_contract_upload",
        normalize,
    )
    monkeypatch.setattr(
        dataset_router,
        "_validate_manual_rows",
        validate,
    )
    monkeypatch.setattr(dataset_router, "_dataset_consumers", consumers)

    def fake_persist(db, service, dataset, content, extension, **kwargs):
        received["args"] = (
            db,
            service,
            dataset,
            content,
            extension,
        )
        received["kwargs"] = kwargs
        return expected

    monkeypatch.setattr(
        mutation_service,
        "persist_uploaded_version",
        fake_persist,
    )
    args = (object(), object(), object(), b"content", "csv")

    assert dataset_router._persist_uploaded_version(*args) is expected
    assert received["args"] == args
    assert received["kwargs"] == {
        "estimate_rowcount_fn": estimate,
        "normalize_manual_contract_upload_fn": normalize,
        "validate_manual_rows_fn": validate,
        "dataset_consumers_fn": consumers,
    }


def test_overview_and_delete_wrappers_resolve_router_globals_at_call_time(
    monkeypatch,
):
    consumer_map = object()
    consumers = object()
    patched_logger = object()
    expected_overview = object()
    expected_delete = object()
    received = {}

    monkeypatch.setattr(dataset_router, "_consumer_map", consumer_map)
    monkeypatch.setattr(dataset_router, "_dataset_consumers", consumers)
    monkeypatch.setattr(dataset_router, "logger", patched_logger)

    def fake_overview(*args, **kwargs):
        received["overview"] = (args, kwargs)
        return expected_overview

    def fake_delete(*args, **kwargs):
        received["delete"] = (args, kwargs)
        return expected_delete

    monkeypatch.setattr(
        query_service,
        "datasets_overview",
        fake_overview,
    )
    monkeypatch.setattr(
        mutation_service,
        "delete_dataset",
        fake_delete,
    )
    database = object()

    result = dataset_router.datasets_overview(
        database,
        "manual",
        "orders",
        "created_at",
        2,
        25,
        True,
    )
    assert result is expected_overview
    assert received["overview"] == (
        (
            database,
            "manual",
            "orders",
            "created_at",
            2,
            25,
            True,
        ),
        {"consumer_map_fn": consumer_map},
    )

    result = dataset_router.delete_dataset(
        "dataset-1",
        False,
        database,
    )
    assert result is expected_delete
    assert received["delete"] == (
        ("dataset-1", False, database),
        {
            "dataset_consumers_fn": consumers,
            "logger_obj": patched_logger,
        },
    )


def test_get_db_resolves_router_session_factory_at_call_time(monkeypatch):
    class Database:
        closed = False

        def close(self):
            self.closed = True

    database = Database()
    monkeypatch.setattr(dataset_router, "SessionLocal", lambda: database)

    dependency = dataset_router.get_db()
    assert next(dependency) is database
    dependency.close()
    assert database.closed is True


def test_dataset_router_keeps_business_workflows_as_thin_adapters():
    source = ROUTER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(ROUTER_PATH))
    functions = _top_level_functions(tree)

    assert len(source.splitlines()) < 400
    assert not {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }.intersection(CONTRACT_NAMES)

    for name in ENDPOINT_NAMES:
        body = _non_docstring_body(functions[name])
        assert len(body) == 1
        assert isinstance(body[0], ast.Return)


def test_dataset_services_keep_patchable_runtime_imports_local():
    tree = ast.parse(
        SERVICE_PATHS[0].read_text(encoding="utf-8"),
        filename=str(SERVICE_PATHS[0]),
    )
    functions = _top_level_functions(tree)

    # 解析/提交两个阶段分别以固定 kind 派发，不再持有 Celery 任务对象
    expected_kinds = {
        "start_dataset_import": "inspect",
        "commit_dataset_import_job": "commit",
    }
    for function_name, expected_kind in expected_kinds.items():
        imported_modules = {
            node.module
            for node in ast.walk(functions[function_name])
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "app.tasks.v2.dataset_import" not in imported_modules
        kinds = {
            keyword.value.value
            for node in ast.walk(functions[function_name])
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "kind"
            and isinstance(keyword.value, ast.Constant)
        }
        assert kinds == {expected_kind}

    dispatch_imports = {
        node.module
        for node in ast.walk(functions["dispatch_dataset_import_task"])
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app.data_channel.datasets.import_jobs" in dispatch_imports
    # 导入任务经 NATS 工作队列派发，不再触达 Celery 任务对象
    assert "app.data_channel.pipeline_tasks.dispatch" in dispatch_imports
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "add_task"
        for node in ast.walk(functions["dispatch_dataset_import_task"])
    )


def test_dataset_services_never_import_the_http_router():
    violations: list[str] = []
    for path in SERVICE_PATHS:
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "app.data_channel.datasets.router":
                    violations.append(
                        f"{path.relative_to(BACKEND_DIR)}:{node.lineno}",
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.data_channel.datasets.router":
                        violations.append(
                            f"{path.relative_to(BACKEND_DIR)}:{node.lineno}",
                        )

    assert not violations, (
        "Canonical dataset services must not depend on the HTTP adapter:\n"
        + "\n".join(violations)
    )
