#!/usr/bin/env python3
"""Live DeepSeek -> data steward -> API Hub -> n8n acceptance test.

The script creates only uniquely named temporary remote n8n state.  It uses a
random internal-proxy token, publishes the local proxy through localtunnel,
proves a real Agent can create/call an interface and compile it into n8n, then
activates and executes that workflow.  Remote workflow/credential and all local
state are deleted in ``finally``.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI


def _secret(name: str, prompt: str) -> str:
    return os.getenv(name, "").strip() or getpass.getpass(prompt).strip()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_tunnel_url(process: subprocess.Popen[str], timeout: int = 90) -> str:
    lines: queue.Queue[str] = queue.Queue()

    def reader() -> None:
        if process.stdout:
            for line in process.stdout:
                lines.put(line.strip())

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.time() + timeout
    seen: list[str] = []
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("localtunnel exited early: " + " | ".join(seen[-5:]))
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            continue
        if line:
            seen.append(line)
        if "your url is:" in line.lower():
            return line.split(":", 1)[1].strip().rstrip("/")
    raise RuntimeError("localtunnel did not publish a URL within 90 seconds")


def _configure_process(temp_root: Path, proxy_token: str, credential_name: str) -> None:
    os.environ.update({
        # Explicit test-only SQLite fixture; external n8n/LLM remain real.
        "ENVIRONMENT": "test",
        "DATABASE_URL": f"sqlite:///{temp_root / 'platform.db'}",
        "UPLOADS_DIR": str(temp_root / "uploads"),
        "STEWARD_WORKSPACE_ROOT": str(temp_root / "sessions"),
        "API_HUB_DATA_DIR": str(temp_root / "api-hub"),
        "STORAGE_LOCAL_DIR": str(temp_root / "storage"),
        "API_HUB_INTERNAL_PROXY_TOKEN": proxy_token,
        "SECRET_KEY": "steward-api-hub-live-e2e-secret-key",
        "FIRST_ADMIN_PASSWORD": "steward-api-hub-live-e2e-admin-password",
        "STEWARD_PROXY_CREDENTIAL_NAME": credential_name,
    })


def _tool_probe(model: str, api_base: str, api_key: str) -> dict:
    from app.ontologies.agent_runtime.llm_bridge import chat

    started = time.monotonic()
    response = chat(
        {"provider": "compatible", "api_key": api_key, "api_base": api_base, "model": model},
        [{"role": "system", "content": "Call the tool exactly once."},
         {"role": "user", "content": "Call echo_probe with probe=api-hub-e2e."}],
        [{
            "name": "echo_probe",
            "description": "Echo a probe.",
            "parameters": {
                "type": "object",
                "properties": {"probe": {"type": "string"}},
                "required": ["probe"],
            },
        }],
    )
    calls = response.get("tool_calls") or []
    return {
        "ok": len(calls) == 1
        and calls[0].get("name") == "echo_probe"
        and (calls[0].get("arguments") or {}).get("probe") == "api-hub-e2e",
        "latencyMs": int((time.monotonic() - started) * 1000),
        "usage": response.get("usage"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n8n-api-url", required=True)
    parser.add_argument("--llm-api-base", required=True)
    parser.add_argument("--models", nargs="+", default=["deepseek-v4-pro", "deepseek-v4-flash"])
    parser.add_argument("--agent-model", default="deepseek-v4-pro")
    args = parser.parse_args()
    n8n_key = _secret("N8N_API_KEY", "N8N_API_KEY: ")
    llm_key = _secret("LLM_API_KEY", "LLM_API_KEY: ")
    if not n8n_key or not llm_key:
        raise SystemExit("N8N_API_KEY and LLM_API_KEY are required")

    temp_root = Path(tempfile.mkdtemp(prefix="steward-api-hub-live-e2e-"))
    proxy_token = secrets.token_urlsafe(36)
    suffix = uuid.uuid4().hex[:8]
    prefix = f"OB-STEWARD-API-HUB-E2E-{suffix}"
    credential_name = f"API Hub Internal Proxy E2E {suffix}"
    interface_name = f"{prefix} 接口"
    _configure_process(temp_root, proxy_token, credential_name)
    backend_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend_root))

    report: dict = {
        "ok": False,
        "models": {},
        "agent": {},
        "apiHub": {},
        "n8n": {},
        "cleanup": {"workflow": False, "credential": False, "temp": False, "errors": []},
    }
    server = None
    server_thread = None
    tunnel = None
    client = None
    workflow_id = None
    credential_id = None

    try:
        from app.api_hub.routers.proxy import internal_router

        app = FastAPI()

        app.include_router(internal_router)
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.1)
        if not server.started:
            raise RuntimeError("temporary API Hub server did not start")
        tunnel = subprocess.Popen(
            ["npx", "--yes", "localtunnel", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        public_root = _read_tunnel_url(tunnel)
        os.environ["STEWARD_INTERNAL_PROXY_BASE_URL"] = (
            f"{public_root}/api-hub/internal/interfaces"
        )
        # Importing the proxy also imports encryption support, which initializes
        # shared settings before the tunnel URL exists.  Production receives the
        # URL at process start; this live harness updates the already-created
        # settings object after its ephemeral tunnel is known.
        from app.config import settings as platform_settings
        platform_settings.steward_internal_proxy_base_url = (
            f"{public_root}/api-hub/internal/interfaces"
        )

        from app.main import _seed_db
        from app.database import SessionLocal
        from app.models.model_config import ModelConfig
        from app.models.user import User
        from app.models.workflow_config import WorkflowConfig
        from app.services.encryption_service import encrypt
        from app.data_channel.steward import service
        from app.data_channel.steward.models import N8nPipeline
        from app.data_channel.steward.orchestrator import run_steward_turn
        from app.data_channel.steward.runner import trigger_and_collect
        from app.api_hub import db as hub_db
        from app.api_hub.agent_service import load_interface

        _seed_db()
        hub_db.init_db()
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.role == "admin").first()
            if user is None:
                raise RuntimeError("isolated admin user missing")
            db.merge(WorkflowConfig(
                id="default",
                enabled=True,
                api_url=args.n8n_api_url,
                api_key_encrypted=encrypt(n8n_key),
                timeout_seconds=30,
            ))
            model_ids = {}
            for model in args.models:
                config_id = str(uuid.uuid4())
                model_ids[model] = config_id
                db.add(ModelConfig(
                    id=config_id,
                    name=f"{prefix}-{model}",
                    config_type="llm",
                    provider="compatible",
                    api_base=args.llm_api_base,
                    api_key_encrypted=encrypt(llm_key),
                    models=[model],
                    options={"max_output_tokens": 4096},
                    enabled=True,
                    is_default=model == args.agent_model,
                    created_by=user.id,
                ))
            db.commit()
            client = service.get_n8n_client(db)
            client.test_connection()
            credential = client.request("POST", "/credentials", json={
                "name": credential_name,
                "type": "httpHeaderAuth",
                "data": {
                    "name": "Authorization",
                    "value": f"Bearer {proxy_token}",
                },
            })
            credential_id = str(credential["id"])

            for model in args.models:
                report["models"][model] = _tool_probe(model, args.llm_api_base, llm_key)

            # Keep the upstream independent of the localtunnel carrying the
            # proxy request. Calling the same tunnel recursively would serialize
            # behind the active request and produce a false timeout.
            upstream_url = "https://httpbin.org/anything/{category}"
            prompt = f"""
这是已确认的真机端到端验收，不需要再次询问设计。请严格完成以下操作，不能绕过接口代理：
1. 新建内部接口草稿，名称严格为「{interface_name}」，GET {upstream_url}，默认 query page=1。
2. 参数契约必须包含：category(path,string,required,dynamic)、page(query,integer,dynamic)、X-Tenant(header,string,dynamic)。
3. 读取刚创建的接口并直接调用一次：category=direct，page=2，X-Tenant=agent-test；核对响应成功。
4. 新建未发布流水线，名称严格为「{prefix}」。读取骨架后，把该接口插到 Webhook 之后，节点名「托管接口取数」，凭据名严格为「{credential_name}」。参数绑定为：
   category ={{{{ $node["Webhook"].json.body.category }}}}
   page ={{{{ $node["Webhook"].json.body.page }}}}
   X-Tenant ={{{{ $node["Webhook"].json.body.tenant }}}}
5. 最后执行 check_workflow 静态体检。不要发布、不要永久启用、不要删除任何资产。
""".strip()
            started = time.monotonic()
            events = list(run_steward_turn(
                db, user, prompt, model_id=model_ids[args.agent_model]
            ))
            report["agent"]["latencyMs"] = int((time.monotonic() - started) * 1000)
            report["agent"]["steps"] = [
                {"tool": e.get("tool"), "summary": e.get("summary"), "error": e.get("error")}
                for e in events if e.get("type") == "step"
            ]
            agent_error = next((e.get("message") for e in events if e.get("type") == "error"), None)
            if agent_error:
                raise RuntimeError(f"data steward failed: {agent_error}")
            tool_names = [item["tool"] for item in report["agent"]["steps"]]
            required_tools = {
                "create_proxy_interface", "get_proxy_interface", "call_proxy_interface",
                "create_pipeline", "get_workflow", "orchestrate_proxy_interface", "check_workflow",
            }
            if not required_tools.issubset(tool_names):
                raise RuntimeError(f"Agent missed required tools: {sorted(required_tools - set(tool_names))}")
            if any(item.get("error") for item in report["agent"]["steps"]):
                raise RuntimeError("Agent tool step failed")

            with hub_db.get_conn() as conn:
                row = conn.execute(
                    "SELECT id FROM interfaces WHERE name = ?", (interface_name,)
                ).fetchone()
            if row is None:
                raise RuntimeError("Agent-created interface missing")
            interface = load_interface(int(row["id"]))
            with hub_db.get_conn() as conn:
                direct_run = conn.execute(
                    "SELECT response_body FROM runs WHERE interface_id=? AND source='steward' "
                    "ORDER BY id DESC LIMIT 1",
                    (interface["id"],),
                ).fetchone()
            direct_payload = json.loads(direct_run["response_body"]) if direct_run else {}
            direct_observed = (
                str(direct_payload.get("url") or "").endswith("/direct?page=2")
                and str((direct_payload.get("args") or {}).get("page")) == "2"
                and (direct_payload.get("headers") or {}).get("X-Tenant") == "agent-test"
            )
            report["apiHub"] = {
                "interfaceId": interface["id"],
                "revision": interface["config_revision"],
                "openMcp": interface["open_enabled"],
                "directCallObserved": direct_observed,
            }
            if not report["apiHub"]["directCallObserved"]:
                raise RuntimeError(f"direct Agent parameters were not echoed: {direct_payload}")

            rec = db.query(N8nPipeline).filter(N8nPipeline.name == prefix).first()
            if rec is None:
                raise RuntimeError("Agent-created pipeline missing")
            workflow_id = str(rec.n8n_workflow_id)
            workflow = client.get_workflow(workflow_id)
            encoded = json.dumps(workflow, ensure_ascii=False)
            proxy_node = next(
                node for node in workflow.get("nodes") or []
                if node.get("name") == "托管接口取数"
            )
            compiled_url = str((proxy_node.get("parameters") or {}).get("url") or "")
            report["n8n"]["compiledUrl"] = compiled_url
            if not compiled_url.startswith(public_root + "/"):
                raise RuntimeError(
                    f"compiled proxy URL did not use the live endpoint: {compiled_url}"
                )
            if proxy_token in encoded or "Bearer " in encoded:
                raise RuntimeError("workflow leaked internal proxy token")
            if f"api-hub-interface:{interface['id']}@{interface['config_revision']}" not in encoded:
                raise RuntimeError("workflow did not pin interface revision")

            from app.data_channel.steward.toolkit import ToolRunner
            check = ToolRunner(
                db, user.id, rec.conversation_id, api_hub_allowed=True,
            ).run("check_workflow", {"record_id": rec.id})
            if not check.get("ok"):
                raise RuntimeError(f"workflow contract failed: {check.get('issues')}")
            contract = check["managedContract"]
            client.activate_workflow(workflow_id)
            rows, execution = trigger_and_collect(
                client,
                workflow_id,
                contract["webhook_path"],
                payload={"category": "n8n", "page": 3, "tenant": "pipeline-test"},
                wait_seconds=60,
                expected_output_node=contract["output_node_name"],
            )
            with hub_db.get_conn() as conn:
                audit_rows = conn.execute(
                        "SELECT source, response_body FROM runs WHERE interface_id=? ORDER BY id",
                        (interface["id"],),
                    ).fetchall()
            sources = [item["source"] for item in audit_rows]
            n8n_run = next(
                (item for item in reversed(audit_rows) if item["source"] == "n8n_internal"),
                None,
            )
            n8n_payload = json.loads(n8n_run["response_body"]) if n8n_run else {}
            observed_n8n = (
                str(n8n_payload.get("url") or "").endswith("/n8n?page=3")
                and str((n8n_payload.get("args") or {}).get("page")) == "3"
                and (n8n_payload.get("headers") or {}).get("X-Tenant") == "pipeline-test"
            )
            if not observed_n8n:
                raise RuntimeError(f"n8n parameters were not echoed: {n8n_payload}")
            if "steward" not in sources or "n8n_internal" not in sources:
                raise RuntimeError(f"call audit sources incomplete: {sources}")
            report["n8n"] = {
                "workflowId": workflow_id,
                "executionId": execution.get("execution_id"),
                "status": execution.get("execution_status"),
                "rowCount": len(rows),
                "parameterBindingObserved": observed_n8n,
                "auditSources": sources,
            }
            report["ok"] = all(item.get("ok") for item in report["models"].values())
        finally:
            db.close()
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        if client is not None and workflow_id:
            try:
                executions = client.list_executions(
                    workflow_id=workflow_id, limit=3, include_data=True
                )
                if executions:
                    detail = client.get_execution(
                        str(executions[0]["id"]), include_data=True
                    )
                    result_data = (detail.get("data") or {}).get("resultData") or {}
                    error = result_data.get("error") or {}
                    report["n8nDebug"] = {
                        "status": detail.get("status"),
                        "lastNodeExecuted": result_data.get("lastNodeExecuted"),
                        "errorMessage": error.get("message"),
                        "errorDescription": error.get("description"),
                        "errorNode": (error.get("node") or {}).get("name"),
                    }
            except Exception as debug_exc:
                report["n8nDebug"] = {"error": str(debug_exc)}
    finally:
        if client is not None and workflow_id:
            try:
                current = client.get_workflow(workflow_id)
                if current.get("active"):
                    client.deactivate_workflow(workflow_id)
                client.delete_workflow(workflow_id)
                report["cleanup"]["workflow"] = True
            except Exception as exc:
                report["cleanup"]["errors"].append(f"workflow: {exc}")
        if client is not None and credential_id:
            try:
                client.request("DELETE", f"/credentials/{credential_id}")
                report["cleanup"]["credential"] = True
            except Exception as exc:
                report["cleanup"]["errors"].append(f"credential: {exc}")
        if tunnel is not None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
        if server is not None:
            server.should_exit = True
        if server_thread is not None:
            server_thread.join(timeout=10)
        shutil.rmtree(temp_root, ignore_errors=True)
        report["cleanup"]["temp"] = not temp_root.exists()
        report["cleanup"]["ok"] = not report["cleanup"]["errors"]

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] and report["cleanup"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
