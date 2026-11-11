"""Runtime port composition must be shared by the Web and worker roots."""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
MAIN_PATH = BACKEND_DIR / "app" / "main.py"
WORKER_PATH = BACKEND_DIR / "app" / "data_channel" / "pipeline_tasks" / "nats_executor.py"

_FORBIDDEN_DIRECT_WIRING = {
    "register_provider",
    "set_remote_query_port",
    "set_action_write_port",
    "set_remote_binding_validator",
    "set_cdc_ontology_port",
}


def _call_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _main_function_calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    names: set[str] = set()
    for node in ast.walk(main):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def test_web_and_worker_share_one_port_composition_call():
    text = MAIN_PATH.read_text(encoding="utf-8")
    assert text.count("install_runtime_ports()") == 1
    assert "install_runtime_ports" in _call_names(MAIN_PATH)
    # worker 不经过 app.main 导入链，必须在自己的 bootstrap 中调用同一组成根。
    assert "install_runtime_ports" in _main_function_calls(WORKER_PATH)


def test_only_composition_root_registers_runtime_ports():
    for path in (MAIN_PATH, WORKER_PATH):
        assert _FORBIDDEN_DIRECT_WIRING.isdisjoint(_call_names(path)), path


def test_cdc_process_uses_the_shared_composition_without_http_ingest_route():
    executor = BACKEND_DIR / "app" / "data_channel" / "integrated" / "cdc_executor.py"
    assert "install_runtime_ports" in _call_names(executor)
    assert _FORBIDDEN_DIRECT_WIRING.isdisjoint(_call_names(executor))
    # Source row data has one private loopback receiver in the executor. The
    # application only mounts the authenticated asset-status read endpoint.
    main_tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(main_tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.endswith(".cdc_receiver")
            assert not node.module.endswith(".cdc_ingest")


def test_main_mounts_run_and_query_view_routers_under_agent_guard():
    text = MAIN_PATH.read_text(encoding="utf-8")
    assert "from app.ontologies.agent_runtime import run_router as agent_run_router" in text
    assert "from app.ontologies.agent_runtime import query_view_router as agent_query_view_router" in text
    assert text.count(
        'prefix="/api/v2/formal/ontologies", tags=["ontology-agent"], '
        "dependencies=agent_guard"
    ) >= 4


def test_install_runtime_ports_is_idempotent_and_wires_query_read_port():
    from app.bootstrap.runtime_composition import install_runtime_ports
    from app.data_channel.pipeline_tasks.dispatch import (
        dispatch_ontology_assistant_run,
    )
    from app.data_channel.query.contracts import provider_for
    from app.data_channel.integrated.cdc_port import get_cdc_ontology_port
    from app.ontologies.sentinels.external_events import ExternalCdcOntologyAdapter
    from app.ontologies.agent_confirmation import write_port
    from app.ontologies.agent_query import remote_query
    from app.ontologies.agent_runtime import run_common
    from app.ontologies.agent_runtime import run_service
    from app.ontologies.mappings import remote_binding

    install_runtime_ports()
    install_runtime_ports()

    assert callable(provider_for("ontology"))
    assert remote_query._port is not None
    assert write_port.write_port_ready() is True
    assert remote_binding._validator is not None
    assert isinstance(get_cdc_ontology_port(), ExternalCdcOntologyAdapter)
    assert run_common._dispatch_port is dispatch_ontology_assistant_run
    # 公开 facade 仍暴露注入器（调用方只依赖 run_service）。
    assert run_service.set_dispatch_run_port is run_common.set_dispatch_run_port
