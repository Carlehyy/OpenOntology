"""Account/Web/MCP canary without model inputs; run only in the disposable CI stack."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.super_assistant import mcp_client
from app.super_assistant.weknora import bootstrap


async def check_platform_mcp(url: str, token: str, rotate_token) -> None:
    from app.auth.models import User
    from app.shared.config import settings
    from app.shared.database import Base
    from app.super_assistant import mcp_server_service as service
    from app.super_assistant.kernel.connectors import McpToolConnector
    from app.super_assistant.kernel.models import CapabilityRevision
    from app.super_assistant.models import SuperAssistantMcpServer
    from app.super_assistant.schemas import McpServerCreate, McpServerUpdate

    settings.environment = "production"
    settings.super_assistant_mcp_internal_base_urls = "http://weknora-app:8080/mcp"
    with TemporaryDirectory(prefix="weknora-mcp-canary-") as directory:
        engine = create_engine(f"sqlite:///{directory}/platform.db")
        try:
            Base.metadata.create_all(engine, tables=[
                User.__table__, SuperAssistantMcpServer.__table__, CapabilityRevision.__table__,
            ])
            with Session(engine, expire_on_commit=False) as db:
                db.add(User(id="canary-admin", username="canary-admin", email="admin@example.invalid",
                            password_hash="unused", role="admin"))
                db.commit()
                server = service.create_mcp_server(db, "canary-admin", McpServerCreate(
                    name="canary_knowledge", transport="streamable_http", url=url,
                    headers={"Authorization": f"Bearer {token}"}, enabled=False,
                ))
                assert token not in server.headers_encrypted
                tested = await service.test_mcp_server(db, "canary-admin", server.id, include_builtins=False)
                assert tested.ok and [tool["name"] for tool in tested.tools] == ["list_knowledge_bases"]
                server = service.update_mcp_server(db, "canary-admin", server.id,
                    McpServerUpdate(constrained=True), include_builtins=False)
                assert server.constrained is True
                server = service.update_mcp_server(db, "canary-admin", server.id,
                    McpServerUpdate(enabled=True), include_builtins=False)
                key = mcp_client.namespaced_tool_name(server.name, "list_knowledge_bases")
                revision = db.query(CapabilityRevision).filter_by(key=key, revision=server.manifest_revision).one()
                assert revision.enabled is True
                connector = McpToolConnector(
                    server_id=server.id, server_name=server.name, tool_name="list_knowledge_bases",
                    transport=server.transport, url=server.url,
                    headers=mcp_client.decrypt_headers(server.headers_encrypted),
                    revision=server.manifest_revision, manifest_hash=server.manifest_hash,
                )
                assert connector.side_effect_class == "external_async"
                output = await connector.invoke(run_id="canary", call_id="canary",
                    input_ref='{"arguments":{}}', deadline=None)
                result = json.loads(output["content"])
                assert result.get("isError") is not True
                payload = result.get("structuredContent")
                if payload is None:
                    payload = json.loads(next(item["text"] for item in result["content"] if item["type"] == "text"))
                assert payload["knowledge_bases"] == [] and payload["total"] == 0

                replacement = rotate_token()
                try:
                    await mcp_client.discover_tools(transport=server.transport, url=server.url,
                        headers={"Authorization": f"Bearer {token}"})
                except mcp_client.McpClientError:
                    pass
                else:
                    raise AssertionError("old native MCP token remained valid")
                server = service.update_mcp_server(db, "canary-admin", server.id,
                    McpServerUpdate(headers={"Authorization": f"Bearer {replacement}"}), include_builtins=False)
                assert server.constrained is False and server.tool_manifest == []
                db.refresh(revision)
                assert revision.enabled is False
                tested = await service.test_mcp_server(db, "canary-admin", server.id, include_builtins=False)
                assert tested.ok
                server = service.update_mcp_server(db, "canary-admin", server.id,
                    McpServerUpdate(constrained=True), include_builtins=False)
                assert server.constrained is True
        finally:
            engine.dispose()


async def check() -> dict:
    if os.environ.get("OPENONTOLOGY_WEKNORA_E2E") != "1":
        raise RuntimeError("explicit canary opt-in required")
    config = bootstrap.build_config(os.environ, for_verify=True)
    if config.base_url != "http://weknora-app:8080":
        raise RuntimeError("canary must use the private Compose app")
    subprocess.run(
        [sys.executable, "scripts/maintenance/retire_palace_storage.py", "--help"],
        check=True, capture_output=True, timeout=15,
    )
    with httpx.Client(base_url=config.base_url, trust_env=False, timeout=30) as client:
        client.get("/health").raise_for_status()
        response = client.post("/api/v1/auth/login", json={
            "email": config.admin_email, "password": config.admin_password,
        })
        response.raise_for_status()
        login = response.json()
        assert login["user"]["is_system_admin"] is True
        headers = {"Authorization": f"Bearer {login['token']}"}
        auth = client.get("/api/v1/auth/config")
        auth.raise_for_status()
        assert auth.json()["registration_mode"] == "invite_only"
        for path in ("models", "knowledge-bases", "mcp-endpoints"):
            response = client.get(f"/api/v1/{path}", headers=headers)
            response.raise_for_status()
            assert response.json()["success"] is True
            assert response.json()["data"] in ([], None)
        ui = client.get("http://weknora-ui/")
        ui.raise_for_status()
        assert "<html" in ui.text.lower()
        endpoint = client.post("/api/v1/mcp-endpoints", headers=headers, json={
            "name": "canary-read-only", "enabled": True,
            "knowledge_base_ids": [], "tools": ["list_knowledge_bases"],
        })
        endpoint.raise_for_status()
        endpoint = endpoint.json()["data"]
        try:
            def rotate_token():
                response = client.post(f"/api/v1/mcp-endpoints/{endpoint['id']}/rotate-token", headers=headers)
                response.raise_for_status()
                return response.json()["data"]["token"]

            await check_platform_mcp(f"{config.base_url}{endpoint['path']}", endpoint["token"], rotate_token)
        finally:
            client.delete(f"/api/v1/mcp-endpoints/{endpoint['id']}", headers=headers).raise_for_status()
    return {
        "result": "pass", "retirement_command_packaged": True,
        "native_web": True, "account_initialized": True,
        "registration_closed": True, "models_created_by_bootstrap": 0,
        "knowledge_bases_created_by_bootstrap": 0,
        "mcp_endpoints_created_by_bootstrap": 0, "native_mcp_list_call": True,
        "platform_mcp_registration": True, "encrypted_mcp_headers": True,
        "token_rotation_revokes_old_token": True, "credential_change_revokes_registration": True,
        "model_inference_tested": False, "document_retrieval_tested": False,
        "synthetic_only": True,
    }


def main():
    report = {"result": "fail", "synthetic_only": True}
    status = 1
    try:
        report = asyncio.run(check())
        status = 0
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    target = Path(os.environ.get("WEKNORA_E2E_REPORT", "/tmp/weknora-canary/report.json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    target.chmod(0o600)
    print(json.dumps(report))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
