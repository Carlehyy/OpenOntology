"""Public contract for the retired document-to-ontology subsystem."""

from pathlib import Path

from app.main import app


APP_DIR = Path(__file__).resolve().parents[2] / "app"

RETIRED_OPENAPI_OPERATIONS = {
    ("get", "/api/v1/settings/rules"),
    ("put", "/api/v1/settings/rules"),
    ("get", "/api/v1/prompts"),
    ("post", "/api/v1/prompts"),
    ("get", "/api/v1/prompts/templates"),
    ("get", "/api/v1/prompts/by-domain/{domain}"),
    ("get", "/api/v1/prompts/{prompt_id}"),
    ("put", "/api/v1/prompts/{prompt_id}"),
    ("delete", "/api/v1/prompts/{prompt_id}"),
    ("post", "/api/v1/prompts/generate-template"),
    ("get", "/api/v1/mcp/info"),
    ("get", "/api/v1/mcp/interfaces"),
    ("post", "/api/v1/mcp/interfaces/{operation_id}/open"),
    ("get", "/api/api-hub/mcp/info"),
    ("get", "/api/api-hub/mcp/system/info"),
    ("get", "/api/api-hub/interfaces/{iid}/mcp-contract"),
    ("post", "/api/api-hub/interfaces/{iid}/open"),
    ("get", "/api/v1/ontologies/{ontology_id}/files"),
    ("post", "/api/v1/ontologies/{ontology_id}/files"),
    ("delete", "/api/v1/ontologies/{ontology_id}/files/{file_id}"),
    ("post", "/api/v1/ontologies/{ontology_id}/execute"),
    ("get", "/api/v1/ontologies/{ontology_id}/execute/status"),
    ("post", "/api/v2/ontologies/{ontology_id}/extract"),
    ("post", "/api/v2/ontologies/{ontology_id}/extract/nl-to-cypher"),
    ("post", "/api/v2/ontologies/{ontology_id}/candidates/approve"),
    ("get", "/api/v2/ontologies/{ontology_id}/extraction/status"),
}

PRESERVED_MCP_OPERATIONS = {
    ("get", "/api/v2/community/mcp-servers"),
    ("get", "/api/v2/super-assistant/mcp-servers"),
    ("post", "/api/v2/super-assistant/mcp-servers/platform-minio"),
    ("post", "/api/v2/super-assistant/mcp-servers/platform-api-hub"),
}

RETIRED_MINIO_OPERATION_PREFIXES = (
    "/api/v1/settings/minio-config",
    "/api/v1/settings/minio/",
)


def _openapi_operations() -> set[tuple[str, str]]:
    operations = set()
    for path, path_item in app.openapi()["paths"].items():
        for method in path_item:
            if method.lower() in {
                "get", "post", "put", "patch", "delete", "options", "head",
            }:
                operations.add((method.lower(), path))
    return operations


def test_exactly_26_legacy_openapi_operations_are_retired():
    assert len(RETIRED_OPENAPI_OPERATIONS) == 26
    operations = _openapi_operations()
    assert RETIRED_OPENAPI_OPERATIONS.isdisjoint(operations)


def test_unrelated_mcp_capabilities_remain_published():
    assert PRESERVED_MCP_OPERATIONS <= _openapi_operations()


def test_manual_minio_settings_operations_are_retired():
    operations = _openapi_operations()
    assert not [
        operation
        for operation in operations
        if operation[1].startswith(RETIRED_MINIO_OPERATION_PREFIXES)
    ]


def test_raw_legacy_mcp_and_external_minio_mcp_are_gone(client):
    assert client.post("/mcp").status_code == 404
    assert client.post("/mcp/minio").status_code == 404

    # API-Hub MCP 开放（含 system 端点）已整体退役：中间件不再声明路径。
    assert client.post("/api-hub/mcp").status_code == 404
    assert client.post("/api-hub/mcp/system").status_code == 404


def test_retired_runtime_modules_do_not_return_as_compatibility_facades():
    retired_modules = (
        APP_DIR / "data_channel" / "transforms" / "router.py",
        APP_DIR / "engine" / "post_harness" / "__init__.py",
        APP_DIR / "engine" / "post_harness" / "validator.py",
        APP_DIR / "mcp_server.py",
        APP_DIR / "settings" / "rules" / "rules_service.py",
        APP_DIR / "settings" / "rules" / "models.py",
        APP_DIR / "settings" / "prompts" / "router.py",
        APP_DIR / "settings" / "prompts" / "models.py",
        APP_DIR / "settings" / "prompts" / "schemas.py",
        APP_DIR / "settings" / "prompts" / "templates.py",
        APP_DIR / "settings" / "open_interfaces" / "router.py",
        APP_DIR / "settings" / "open_interfaces" / "models.py",
        APP_DIR / "settings" / "open_interfaces" / "schemas.py",
        APP_DIR / "settings" / "open_interfaces" / "catalog.py",
        APP_DIR / "settings" / "open_interfaces" / "executor.py",
        APP_DIR / "settings" / "open_interfaces" / "server.py",
        APP_DIR / "ontologies" / "files" / "router.py",
        APP_DIR / "ontologies" / "files" / "models.py",
        APP_DIR / "ontologies" / "files" / "schemas.py",
        APP_DIR / "ontologies" / "extraction" / "legacy_bridge.py",
        APP_DIR / "ontologies" / "extraction" / "extraction_service.py",
        APP_DIR / "routers" / "extraction.py",
        APP_DIR / "routers" / "files.py",
        APP_DIR / "routers" / "mcp.py",
        APP_DIR / "routers" / "prompts.py",
        APP_DIR / "routers" / "v2" / "extraction.py",
        APP_DIR / "schemas" / "extraction.py",
        APP_DIR / "schemas" / "file.py",
        APP_DIR / "schemas" / "mcp.py",
        APP_DIR / "schemas" / "prompt.py",
        APP_DIR / "services" / "llm_extraction_service.py",
        APP_DIR / "services" / "v2" / "legacy_extraction_bridge.py",
        APP_DIR / "services" / "mcp_catalog.py",
        APP_DIR / "services" / "mcp_executor.py",
        APP_DIR / "shared" / "post_harness_validator.py",
        APP_DIR / "models" / "extraction_task.py",
        APP_DIR / "models" / "file.py",
        APP_DIR / "models" / "mcp.py",
        APP_DIR / "models" / "prompt.py",
        APP_DIR / "models" / "rules_config.py",
        APP_DIR / "settings" / "agents" / "models.py",
        APP_DIR / "settings" / "agents" / "schemas.py",
        APP_DIR / "ontologies" / "audit" / "models.py",
        APP_DIR / "ontologies" / "audit" / "router.py",
        APP_DIR / "ontologies" / "audit" / "schemas.py",
        APP_DIR / "ontologies" / "audit" / "service.py",
        APP_DIR / "services" / "v2" / "vector" / "__init__.py",
        APP_DIR / "services" / "v2" / "vector" / "chroma_service.py",
    )
    assert not [path for path in retired_modules if path.exists()]
