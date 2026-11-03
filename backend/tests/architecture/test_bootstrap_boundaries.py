"""Keep process bootstrap implementation out of the HTTP composition root."""
from __future__ import annotations

import asyncio
import ast
from contextlib import asynccontextmanager
import json
from pathlib import Path
import threading
from unittest.mock import AsyncMock

import httpx
import pytest


BACKEND_DIR = Path(__file__).resolve().parents[2]
MAIN_PATH = BACKEND_DIR / "app" / "main.py"


@pytest.fixture(autouse=True)
def _stub_weknora_platform_install(monkeypatch):
    """Startup ordering tests own no real platform admin or WeKnora state."""
    from app.super_assistant.weknora import mcp_service

    monkeypatch.setattr(
        mcp_service, "install_for_platform_admin", lambda: "stub-server-id",
    )


def _top_level_functions(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }


def test_main_keeps_bootstrap_implementation_out_of_composition_root():
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    functions = _top_level_functions(tree)

    assert "_seed_db" not in functions
    assert "_probe_http_service" not in functions

    health = functions["health"]
    assert len(health.body) == 1
    assert isinstance(health.body[0], ast.Return)

    lifespan = functions["lifespan"]
    assert len(lifespan.body) == 1
    assert isinstance(lifespan.body[0], ast.AsyncWith)

    forbidden_imports = {
        "asyncio",
        "tempfile",
        "app.shared.schema_compat",
    }
    imported_modules = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported_modules.update(
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert forbidden_imports.isdisjoint(imported_modules)
    assert {
        "app.bootstrap.lifecycle",
        "app.bootstrap.seeding",
    }.issubset(imported_modules)


def test_main_bootstrap_compatibility_aliases_keep_object_identity():
    from app import main
    from app.bootstrap import health, seeding

    assert main._seed_db is seeding.seed_database
    assert main._probe_http_service is health.probe_http_service
    assert main.urllib is health.urllib
    assert main.urllib.request is health.urllib.request


def test_health_wrapper_resolves_legacy_probe_alias_at_call_time(monkeypatch):
    from app import main

    patched_probe = object()
    db = object()
    received = {}

    def fake_readiness(current_db, *, probe):
        received["db"] = current_db
        received["probe"] = probe
        return "ready"

    monkeypatch.setattr(main, "_probe_http_service", patched_probe)
    monkeypatch.setattr(
        main.bootstrap_health,
        "readiness_response",
        fake_readiness,
    )

    assert main.health(db) == "ready"
    assert received == {"db": db, "probe": patched_probe}


@pytest.mark.asyncio
async def test_lifespan_wrapper_resolves_legacy_seed_alias_at_call_time(
    monkeypatch,
):
    from app import main

    patched_seed = object()
    received = {}

    @asynccontextmanager
    async def fake_lifespan(current_app, *, seed_database):
        received["app"] = current_app
        received["seed_database"] = seed_database
        yield

    monkeypatch.setattr(main, "_seed_db", patched_seed)
    monkeypatch.setattr(main, "application_lifespan", fake_lifespan)

    async with main.lifespan(main.app):
        received["entered"] = True

    assert received == {
        "app": main.app,
        "seed_database": patched_seed,
        "entered": True,
    }


@pytest.mark.asyncio
async def test_canonical_lifecycle_preserves_startup_and_shutdown_order(
    monkeypatch,
):
    from app.api_hub import db as api_hub_db
    from app.bootstrap import lifecycle
    from app.data_channel.file_assets import service as file_asset_service
    from app.data_channel.steward import browser_runtime
    from app.services import sentinel
    from app.data_channel.sync_tasks import scheduler as sync_scheduler
    from app.ontologies.graph import index_setup
    from app.shared import dependency_probe
    from app.ontologies import projection_state

    events: list[str] = []

    class Manager:
        def __init__(self, name: str):
            self.name = name

        def run(self):
            return self

        async def __aenter__(self):
            events.append(f"{self.name}.enter")

        async def __aexit__(self, *_args):
            events.append(f"{self.name}.exit")

    class Scheduler:
        healthy = True
        last_error = None

        def start(self):
            events.append("data_scheduler.start")

        def shutdown(self):
            events.append("data_scheduler.shutdown")

    scheduler = Scheduler()

    monkeypatch.setattr(lifecycle.settings, "environment", "development")
    monkeypatch.setattr(
        dependency_probe,
        "probe_startup_dependencies",
        lambda: events.append("dependencies.probe"),
    )
    monkeypatch.setattr(
        api_hub_db,
        "init_db",
        lambda: events.append("api_hub_db.init"),
    )
    monkeypatch.setattr(
        sentinel,
        "register_cdc",
        lambda *, start_worker: events.append(
            f"sentinel_cdc.register:{start_worker}"
        ),
    )
    monkeypatch.setattr(
        sentinel,
        "start_scan_worker",
        lambda: events.append("sentinel_scan.start") or True,
    )
    monkeypatch.setattr(
        sentinel,
        "stop_scan_worker",
        lambda: events.append("sentinel_scan.stop"),
    )
    monkeypatch.setattr(
        sentinel,
        "stop_cdc_worker",
        lambda: events.append("sentinel_cdc.stop"),
    )
    monkeypatch.setattr(
        index_setup,
        "setup_indexes",
        lambda: events.append("neo4j_indexes.setup")
        or {"status": "done", "results": []},
    )
    monkeypatch.setattr(
        projection_state,
        "repair_unready_projections",
        lambda: events.append("neo4j_projections.repair") or 0,
    )
    monkeypatch.setattr(
        sync_scheduler,
        "get_sync_scheduler",
        lambda: scheduler,
    )
    async def cleanup_loop():
        await asyncio.Future()

    monkeypatch.setattr(
        file_asset_service,
        "file_asset_cleanup_loop",
        cleanup_loop,
    )
    real_create_task = asyncio.create_task
    task_calls = 0

    def create_task(coro):
        nonlocal task_calls
        task_calls += 1
        events.append(f"task.create.{task_calls}")
        return real_create_task(coro)

    monkeypatch.setattr(lifecycle.asyncio, "create_task", create_task)
    monkeypatch.setattr(
        browser_runtime.browser_manager,
        "close_all",
        lambda: events.append("browser.close_all"),
    )

    async with lifecycle.application_lifespan(
        object(),
        seed_database=lambda: events.append("database.seed"),
    ):
        events.append("application.ready")

    assert events == [
        "database.seed",
        "dependencies.probe",
        "neo4j_indexes.setup",
        "neo4j_projections.repair",
        "api_hub_db.init",
        "sentinel_cdc.register:True",
        "sentinel_scan.start",
        "data_scheduler.start",
        "task.create.1",
        "task.create.2",
        "application.ready",
        "browser.close_all",
        "data_scheduler.shutdown",
        "sentinel_scan.stop",
        "sentinel_cdc.stop",
    ]


@pytest.mark.asyncio
async def test_lifecycle_dependency_failure_starts_no_background_resources(
    monkeypatch,
):
    from app.bootstrap import lifecycle
    from app.services import sentinel
    from app.shared import dependency_probe

    events: list[str] = []

    monkeypatch.setattr(lifecycle.settings, "environment", "development")

    def reject_dependencies():
        events.append("dependencies.probe")
        raise RuntimeError("unavailable")

    monkeypatch.setattr(
        dependency_probe,
        "probe_startup_dependencies",
        reject_dependencies,
    )
    monkeypatch.setattr(
        sentinel,
        "register_cdc",
        lambda **_kwargs: events.append("sentinel.start"),
    )

    with pytest.raises(RuntimeError, match="unavailable"):
        async with lifecycle.application_lifespan(
            object(),
            seed_database=lambda: events.append("database.seed"),
        ):
            pytest.fail("lifespan must not enter")

    assert events == ["database.seed", "dependencies.probe"]


@pytest.mark.asyncio
async def test_lifecycle_partial_startup_failure_cleans_started_resources(
    monkeypatch,
):
    from app.api_hub import db as api_hub_db
    from app.bootstrap import lifecycle
    from app.ontologies import projection_state
    from app.services import sentinel
    from app.ontologies.graph import index_setup
    from app.shared import dependency_probe

    events: list[str] = []

    monkeypatch.setattr(lifecycle.settings, "environment", "production")
    monkeypatch.setattr(
        dependency_probe,
        "probe_startup_dependencies",
        lambda: events.append("dependencies.probe"),
    )
    monkeypatch.setattr(
        index_setup,
        "setup_indexes",
        lambda: events.append("neo4j_indexes.setup")
        or {"status": "done", "results": []},
    )
    monkeypatch.setattr(
        projection_state,
        "repair_unready_projections",
        lambda: events.append("neo4j_projections.repair") or 0,
    )
    monkeypatch.setattr(
        api_hub_db,
        "init_db",
        lambda: events.append("api_hub_db.init"),
    )

    def fail_sentinel(*, start_worker):
        events.append(f"sentinel.register:{start_worker}")
        raise RuntimeError("injected sentinel startup failure")

    monkeypatch.setattr(sentinel, "register_cdc", fail_sentinel)
    monkeypatch.setattr(
        sentinel,
        "stop_scan_worker",
        lambda: events.append("sentinel_scan.stop"),
    )
    monkeypatch.setattr(
        sentinel,
        "stop_cdc_worker",
        lambda: events.append("sentinel_cdc.stop"),
    )

    with pytest.raises(RuntimeError, match="Sentinel engine"):
        async with lifecycle.application_lifespan(
            object(),
            seed_database=lambda: events.append("database.seed"),
        ):
            pytest.fail("lifespan must not enter")

    assert events == [
        "database.seed",
        "dependencies.probe",
        "neo4j_indexes.setup",
        "neo4j_projections.repair",
        "api_hub_db.init",
        "sentinel.register:True",
        "sentinel_scan.stop",
        "sentinel_cdc.stop",
    ]


WEKNORA_BASE_URL = "http://weknora-app:8080"
WEKNORA_MCP_TOKEN = "mcp_synthetic_token_not_a_real_secret"
WEKNORA_MANIFEST = [
    {
        "name": name,
        "description": name,
        "input_schema": {"type": "object", "properties": {}},
    }
    for name in ("list_knowledge_bases", "read_document", "search_knowledge")
]


def _install_weknora_state(tmp_path, monkeypatch):
    from app.super_assistant.weknora import service

    state = {
        "version": 1,
        "verified": True,
        "base_url": WEKNORA_BASE_URL,
        "mcp_endpoint_id": "endpoint-test",
        "mcp_token": WEKNORA_MCP_TOKEN,
        "knowledge_base_id": "kb-test",
        "knowledge_base_name": "共享知识库",
        "tools": WEKNORA_MANIFEST,
    }
    path = tmp_path / "integration.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(service.settings, "weknora_state_path", str(path))
    monkeypatch.setattr(service.settings, "weknora_base_url", WEKNORA_BASE_URL)
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )
    return service


def _apply_weknora_failure(failure, service, monkeypatch):
    if failure == "bad-token":
        monkeypatch.setattr(
            service.mcp_client,
            "discover_tools",
            AsyncMock(side_effect=RuntimeError(
                f"HTTP 401 unauthorized Bearer {WEKNORA_MCP_TOKEN}")),
        )
    elif failure == "knowledge-base-unreachable":
        monkeypatch.setattr(
            service.mcp_client,
            "discover_tools",
            AsyncMock(return_value=list(WEKNORA_MANIFEST)),
        )
        monkeypatch.setattr(
            service.mcp_client,
            "call_tool",
            AsyncMock(side_effect=RuntimeError("connection refused")),
        )
    elif failure == "manifest-fault":
        changed = [
            {
                **WEKNORA_MANIFEST[0],
                "input_schema": {
                    "type": "object",
                    "required": ["unexpected_argument"],
                },
            },
            *WEKNORA_MANIFEST[1:],
        ]
        monkeypatch.setattr(
            service.mcp_client,
            "discover_tools",
            AsyncMock(return_value=changed),
        )
        monkeypatch.setattr(service.mcp_client, "call_tool", AsyncMock())
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(f"unknown failure: {failure}")


def _patch_startup_resources(monkeypatch):
    """Record every owned startup resource so a failed probe can prove none ran."""
    from app.api_hub import db as api_hub_db
    from app.bootstrap import lifecycle
    from app.data_channel.file_assets import service as file_asset_service
    from app.data_channel.steward import browser_runtime
    from app.data_channel.sync_tasks import scheduler as sync_scheduler
    from app.ontologies import projection_state
    from app.ontologies.graph import index_setup
    from app.services import sentinel

    events: list[str] = []

    class Scheduler:
        healthy = True
        last_error = None

        def start(self):
            events.append("data_scheduler.start")

        def shutdown(self):
            events.append("data_scheduler.shutdown")

    scheduler = Scheduler()

    monkeypatch.setattr(
        index_setup,
        "setup_indexes",
        lambda: events.append("neo4j_indexes.setup")
        or {"status": "done", "results": []},
    )
    monkeypatch.setattr(
        projection_state,
        "repair_unready_projections",
        lambda: events.append("projections.repair") or 0,
    )
    monkeypatch.setattr(
        api_hub_db, "init_db", lambda: events.append("api_hub_db.init"),
    )
    monkeypatch.setattr(
        sentinel,
        "register_cdc",
        lambda *, start_worker: events.append(f"sentinel.register:{start_worker}"),
    )
    monkeypatch.setattr(
        sentinel,
        "start_scan_worker",
        lambda: events.append("sentinel_scan.start") or True,
    )
    monkeypatch.setattr(
        sentinel, "stop_scan_worker", lambda: events.append("sentinel_scan.stop"),
    )
    monkeypatch.setattr(
        sentinel, "stop_cdc_worker", lambda: events.append("sentinel_cdc.stop"),
    )
    monkeypatch.setattr(
        sync_scheduler, "get_sync_scheduler", lambda: scheduler,
    )

    async def cleanup_loop():
        await asyncio.Future()

    monkeypatch.setattr(
        file_asset_service, "file_asset_cleanup_loop", cleanup_loop,
    )
    real_create_task = asyncio.create_task

    def create_task(coro):
        events.append("task.create")
        return real_create_task(coro)

    monkeypatch.setattr(lifecycle.asyncio, "create_task", create_task)
    monkeypatch.setattr(
        browser_runtime.browser_manager,
        "close_all",
        lambda: events.append("browser.close_all"),
    )
    return events


@pytest.mark.asyncio
async def test_lifecycle_dependency_probe_runs_off_the_event_loop_thread(
    monkeypatch,
):
    """Regression: WeKnora's asyncio.run must never execute on the loop thread."""
    from app.bootstrap import lifecycle
    from app.ontologies.graph import index_setup
    from app.shared import dependency_probe

    events: list[str] = []
    probe_threads: list[int] = []
    loop_thread = threading.get_ident()

    def record_probe():
        probe_threads.append(threading.get_ident())
        events.append("dependencies.probe")

    monkeypatch.setattr(lifecycle.settings, "environment", "development")
    monkeypatch.setattr(
        dependency_probe, "probe_startup_dependencies", record_probe,
    )
    monkeypatch.setattr(
        index_setup,
        "setup_indexes",
        lambda: (_ for _ in ()).throw(RuntimeError("index setup stopped")),
    )

    with pytest.raises(RuntimeError, match="Neo4j indexes"):
        async with lifecycle.application_lifespan(
            object(), seed_database=lambda: events.append("database.seed"),
        ):
            pytest.fail("lifespan must not enter after index failure")

    assert events == ["database.seed", "dependencies.probe"]
    assert probe_threads and probe_threads[0] != loop_thread


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["bad-token", "knowledge-base-unreachable", "manifest-fault"],
)
async def test_weknora_startup_failure_starts_no_workers(
    failure, tmp_path, monkeypatch,
):
    """A broken token, unreachable KB or changed MCP manifest must fail closed.

    The real WeKnora probe runs (through the real startup probe), and the
    lifecycle must abort before any owned worker, scheduler or task exists.
    """
    from app.bootstrap import lifecycle
    from app.shared import dependency_probe

    service = _install_weknora_state(tmp_path, monkeypatch)
    _apply_weknora_failure(failure, service, monkeypatch)
    events = _patch_startup_resources(monkeypatch)
    monkeypatch.setattr(lifecycle.settings, "environment", "development")
    monkeypatch.setattr(
        dependency_probe,
        "PROBES",
        (("WeKnora", dependency_probe.probe_weknora),),
    )

    with pytest.raises(RuntimeError) as exc_info:
        async with lifecycle.application_lifespan(
            object(), seed_database=lambda: events.append("database.seed"),
        ):
            pytest.fail("WeKnora startup failure must not enter the application")

    assert str(exc_info.value) == (
        "Required startup dependencies unavailable: WeKnora"
    )
    assert WEKNORA_MCP_TOKEN not in str(exc_info.value)
    assert events == ["database.seed"]
    assert not any(
        event.startswith(("sentinel.", "data_scheduler.", "api_hub_db.", "task."))
        for event in events
    )


@pytest.mark.asyncio
async def test_lifecycle_shutdown_survives_remote_agent_task_gc_failure(
    monkeypatch, caplog,
):
    from app.bootstrap import lifecycle
    from app.notifications import delivery_scheduler
    from app.super_assistant import remote_agent_task_gc, scheduled_scheduler

    events = _patch_startup_resources(monkeypatch)
    monkeypatch.setattr(lifecycle.settings, "environment", "test")
    monkeypatch.setattr(
        scheduled_scheduler,
        "shutdown",
        lambda: events.append("scheduled_scheduler.shutdown"),
    )
    monkeypatch.setattr(
        delivery_scheduler,
        "shutdown",
        lambda: events.append("delivery_scheduler.shutdown"),
    )

    def fail_gc_shutdown():
        events.append("remote_agent_task_gc.shutdown")
        raise RuntimeError("injected GC shutdown failure")

    monkeypatch.setattr(
        remote_agent_task_gc, "shutdown", fail_gc_shutdown,
    )

    async with lifecycle.application_lifespan(
        object(), seed_database=lambda: events.append("database.seed"),
    ):
        events.append("application.ready")

    assert events[0] == "database.seed"
    assert "application.ready" in events
    assert events[-3:] == [
        "scheduled_scheduler.shutdown",
        "delivery_scheduler.shutdown",
        "remote_agent_task_gc.shutdown",
    ]
    assert "Remote agent task GC cleanup failed" in caplog.text


@pytest.mark.asyncio
async def test_weknora_platform_install_failure_blocks_workers(monkeypatch):
    """The default admin's WeKnora registration is part of the startup gate."""
    from app.bootstrap import lifecycle
    from app.shared import dependency_probe
    from app.super_assistant.weknora import mcp_service
    from app.super_assistant.weknora.service import WeKnoraError

    events = _patch_startup_resources(monkeypatch)
    loop_thread = threading.get_ident()
    install_threads: list[int] = []
    monkeypatch.setattr(lifecycle.settings, "environment", "development")
    monkeypatch.setattr(
        dependency_probe,
        "PROBES",
        (("WeKnora", lambda: events.append("dependencies.probe")),),
    )

    def fail_install():
        install_threads.append(threading.get_ident())
        raise WeKnoraError("WEKNORA_REGISTRATION_FAILED")

    monkeypatch.setattr(
        mcp_service, "install_for_platform_admin", fail_install,
    )

    with pytest.raises(WeKnoraError, match="WEKNORA_REGISTRATION_FAILED"):
        async with lifecycle.application_lifespan(
            object(), seed_database=lambda: events.append("database.seed"),
        ):
            pytest.fail("WeKnora install failure must not enter the application")

    assert events == ["database.seed", "dependencies.probe"]
    assert install_threads and install_threads[0] != loop_thread
    assert not any(
        event.startswith(("sentinel.", "data_scheduler.", "api_hub_db.", "task."))
        for event in events
    )


@pytest.mark.asyncio
async def test_weknora_platform_install_runs_between_probe_and_workers(monkeypatch):
    from app.bootstrap import lifecycle
    from app.shared import dependency_probe
    from app.super_assistant.weknora import mcp_service

    events = _patch_startup_resources(monkeypatch)
    monkeypatch.setattr(lifecycle.settings, "environment", "development")
    monkeypatch.setattr(
        dependency_probe,
        "PROBES",
        (("WeKnora", lambda: events.append("dependencies.probe")),),
    )
    monkeypatch.setattr(
        mcp_service,
        "install_for_platform_admin",
        lambda: events.append("weknora.install") or "server-id",
    )

    async with lifecycle.application_lifespan(
        object(), seed_database=lambda: events.append("database.seed"),
    ):
        events.append("application.ready")

    assert events[:4] == [
        "database.seed",
        "dependencies.probe",
        "weknora.install",
        "neo4j_indexes.setup",
    ]
    assert "sentinel.register:True" in events
    assert "application.ready" in events
