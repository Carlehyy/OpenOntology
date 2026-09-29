"""Protect Pipeline Task HTTP, helper, and OpenAPI compatibility."""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path

from fastapi.params import Depends

from app.data_channel.pipeline_tasks import contracts
from app.data_channel.pipeline_tasks import execution_service
from app.data_channel.pipeline_tasks import history_service
from app.data_channel.pipeline_tasks import lifecycle_service
from app.data_channel.pipeline_tasks import query_service
from app.data_channel.pipeline_tasks import router as task_router
from app.data_channel.pipeline_tasks import validation_service


BACKEND_DIR = Path(__file__).resolve().parents[2]
TASK_DIR = BACKEND_DIR / "app" / "data_channel" / "pipeline_tasks"

ROUTE_PARAMETERS = {
    "selectable_pipelines": ("db",),
    "stats_overview": ("db",),
    "create_task": ("body", "db", "current_user"),
    "list_tasks": (
        "search",
        "status",
        "enabled",
        "pipeline_id",
        "page",
        "page_size",
        "db",
    ),
    "pipeline_filter_options": ("db",),
    "list_all_histories": (
        "search",
        "pipeline_id",
        "page",
        "page_size",
        "status",
        "trigger_type",
        "created_from",
        "created_to",
        "db",
    ),
    "get_task": ("task_id", "db"),
    "update_task": ("task_id", "body", "db"),
    "delete_task": ("task_id", "db"),
    "toggle_task": ("task_id", "enabled", "db"),
    "trigger_task": ("task_id", "background", "sync", "db", "full_refresh"),
    "list_histories": (
        "task_id",
        "page",
        "page_size",
        "status",
        "trigger_type",
        "created_from",
        "created_to",
        "db",
    ),
    "run_audit": ("task_id", "run_id", "db"),
}

DELEGATES = {
    "selectable_pipelines": (
        "_query_service",
        "selectable_pipelines",
    ),
    "stats_overview": ("_query_service", "stats_overview"),
    "create_task": ("_lifecycle_service", "create_task"),
    "list_tasks": ("_query_service", "list_tasks"),
    "pipeline_filter_options": (
        "_query_service",
        "pipeline_filter_options",
    ),
    "list_all_histories": (
        "_history_service",
        "list_all_histories",
    ),
    "get_task": ("_query_service", "get_task"),
    "update_task": ("_lifecycle_service", "update_task"),
    "delete_task": ("_lifecycle_service", "delete_task"),
    "toggle_task": ("_lifecycle_service", "toggle_task"),
    "trigger_task": ("_execution_service", "trigger_task"),
    "list_histories": ("_history_service", "list_histories"),
    "run_audit": ("_history_service", "run_audit"),
}

# 经 _cache.cached_call 包装的读接口：处理器仍只做委托，builder 内直接
# 调用目标服务。
CACHED_DELEGATES = {
    "stats_overview",
    "list_tasks",
    "pipeline_filter_options",
}

HELPER_KEYWORDS = {
    "selectable_pipelines": {
        "curated_columns_fn",
        "version_has_content_fn",
    },
    "stats_overview": {
        "now_utc_fn",
        "shanghai_day_start_utc_fn",
        "shanghai_date_fn",
        "utc_iso_fn",
    },
    "create_task": {
        "validate_fn",
        "refresh_scheduler_fn",
        "with_pipeline_info_fn",
    },
    "list_tasks": {"with_pipeline_info_fn"},
    "pipeline_filter_options": set(),
    "list_all_histories": {
        "validate_history_query_fn",
        "apply_history_filters_fn",
        "history_item_fn",
    },
    "get_task": {"with_pipeline_info_fn"},
    "update_task": {
        "validate_fn",
        "refresh_scheduler_fn",
        "with_pipeline_info_fn",
    },
    "delete_task": {"refresh_scheduler_fn"},
    "toggle_task": {"refresh_scheduler_fn"},
    "trigger_task": set(),
    "list_histories": {
        "validate_history_query_fn",
        "apply_history_filters_fn",
        "history_item_fn",
    },
    "run_audit": set(),
}

HELPER_ALIASES = {
    "_validate": validation_service,
    "_refresh_scheduler": lifecycle_service,
    "_now_utc": query_service,
    "_as_utc": query_service,
    "_utc_iso": query_service,
    "_shanghai_day_start_utc": query_service,
    "_shanghai_date": query_service,
    "_live_next_run_map": query_service,
    "_computed_next_run": query_service,
    "_last_impact_map": query_service,
    "_with_pipeline_info": query_service,
    "_curated_columns": query_service,
    "_validate_history_query": history_service,
    "_apply_history_filters": history_service,
    "_history_item": history_service,
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


def test_pipeline_task_router_reexports_contracts_by_identity():
    assert (
        task_router.PipelineTaskCreate
        is contracts.PipelineTaskCreate
    )
    assert (
        task_router.PipelineTaskUpdate
        is contracts.PipelineTaskUpdate
    )
    assert task_router.WRITE_MODES is contracts.WRITE_MODES
    assert task_router.HistoryStatus is contracts.HistoryStatus
    assert (
        task_router.HistoryTriggerType
        is contracts.HistoryTriggerType
    )


def test_pipeline_task_router_reexports_all_private_helpers():
    for name, module in HELPER_ALIASES.items():
        assert getattr(task_router, name) is getattr(module, name)
    assert task_router.SHANGHAI_TZ is query_service.SHANGHAI_TZ


def test_pipeline_task_route_signatures_remain_stable():
    body_types = {
        "create_task": task_router.PipelineTaskCreate,
        "update_task": task_router.PipelineTaskUpdate,
    }
    for name, expected_parameters in ROUTE_PARAMETERS.items():
        parameters = inspect.signature(
            getattr(task_router, name),
            eval_str=True,
        ).parameters
        assert tuple(parameters) == expected_parameters
        assert isinstance(parameters["db"].default, Depends)
        if name in body_types:
            assert parameters["body"].annotation is body_types[name]


def test_pipeline_task_handlers_only_delegate_with_compatibility_helpers():
    path = TASK_DIR / "router.py"
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
        call = statement.value
        if name in CACHED_DELEGATES:
            # 缓存包装形态：_cache.cached_call(键, TTL, lambda: service(...))
            # 处理器仍只做委托——缓存层不承载业务逻辑，builder 内必须
            # 直接调用目标服务并仅传兼容 helper 关键字。
            assert isinstance(call.func, ast.Attribute)
            assert isinstance(call.func.value, ast.Name)
            assert call.func.value.id == "_cache"
            assert call.func.attr == "cached_call"
            assert len(call.args) >= 3
            builder = call.args[2]
            assert isinstance(builder, ast.Lambda), (
                f"{name} 缓存 builder 必须是 lambda 表达式"
            )
            delegate_call = builder.body
            assert isinstance(delegate_call, ast.Call)
            assert isinstance(delegate_call.func, ast.Attribute)
            assert isinstance(delegate_call.func.value, ast.Name)
            assert delegate_call.func.value.id == module_name
            assert delegate_call.func.attr == function_name
            assert {
                keyword.arg
                for keyword in delegate_call.keywords
            } == HELPER_KEYWORDS[name]
            continue
        assert isinstance(call.func, ast.Attribute)
        assert isinstance(call.func.value, ast.Name)
        assert call.func.value.id == module_name
        assert call.func.attr == function_name
        assert {
            keyword.arg
            for keyword in call.keywords
        } == HELPER_KEYWORDS[name]


def test_scheduler_and_clock_patches_resolve_at_request_time(
    monkeypatch,
):
    scheduler_refresh = object()
    now_utc = object()
    created = object()
    stats = object()
    create_call = {}
    stats_call = {}

    def fake_create(*args, **kwargs):
        create_call["args"] = args
        create_call["kwargs"] = kwargs
        return created

    def fake_stats(*args, **kwargs):
        stats_call["args"] = args
        stats_call["kwargs"] = kwargs
        return stats

    monkeypatch.setattr(
        task_router,
        "_refresh_scheduler",
        scheduler_refresh,
    )
    monkeypatch.setattr(task_router, "_now_utc", now_utc)
    monkeypatch.setattr(
        lifecycle_service,
        "create_task",
        fake_create,
    )
    monkeypatch.setattr(
        query_service,
        "stats_overview",
        fake_stats,
    )

    body = object()
    database = object()
    actor = object()
    assert task_router.create_task(
        body,
        db=database,
        current_user=actor,
    ) is created
    assert task_router.stats_overview(db=database) is stats

    assert create_call["args"] == (body, database, actor)
    assert (
        create_call["kwargs"]["refresh_scheduler_fn"]
        is scheduler_refresh
    )
    assert stats_call["args"] == (database,)
    assert stats_call["kwargs"]["now_utc_fn"] is now_utc


def test_fixed_pipeline_task_paths_precede_parameterized_task_path():
    paths = [route.path for route in task_router.router.routes]
    parameterized = paths.index("/{task_id}")
    for fixed in (
        "/selectable-pipelines",
        "/stats",
        "/pipeline-options",
        "/histories",
    ):
        assert paths.index(fixed) < parameterized


def test_pipeline_task_services_do_not_depend_on_http_router():
    for module in (
        contracts,
        execution_service,
        history_service,
        lifecycle_service,
        query_service,
        validation_service,
    ):
        imports = _imports(Path(module.__file__))
        assert (
            "app.data_channel.pipeline_tasks.router"
            not in imports
        )
        assert "fastapi.routing" not in imports


def test_pipeline_task_router_and_services_stay_bounded():
    limits = {
        "router.py": 300,
        "contracts.py": 100,
        "validation_service.py": 150,
        "query_service.py": 650,
        "history_service.py": 380,
        # 上调自 180：_refresh_scheduler 不再静默吞掉调度失败，改为返回
        # 状态字典并在 CRUD 响应呈现「已保存但未注册调度」（含注释+日志）
        "lifecycle_service.py": 220,
        "execution_service.py": 80,
    }
    for filename, maximum in limits.items():
        line_count = len(
            (TASK_DIR / filename)
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert line_count < maximum


def test_pipeline_task_openapi_matches_pre_extraction_baseline():
    from app.main import app

    prefix = "/api/v2/pipeline-tasks"
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
    assert len(paths) == 10
    assert sum(len(item) for item in paths.values()) == 13
    assert hashlib.sha256(payload).hexdigest() == (
        "20275e90c7dd58201e68e49093deb058eeba752e6df212774660c70d90e1c2e4"
    )
