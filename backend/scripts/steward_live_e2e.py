#!/usr/bin/env python3
"""Run a destructive-but-cleaned live E2E against an external n8n and LLM.

Secrets are read from a TTY (or their conventional environment variables),
never accepted as command-line arguments.  The script uses an isolated SQLite
database and workspace, creates a uniquely named remote workflow, executes it,
packages the resulting rows into the conversation workspace, and removes the
remote workflow in ``finally``.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path


def _secret(env_name: str, prompt: str) -> str:
    value = os.getenv(env_name, "").strip()
    return value or getpass.getpass(prompt).strip()


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n8n-api-url", required=True)
    parser.add_argument("--llm-api-base", required=True)
    parser.add_argument(
        "--models", nargs="+", default=["deepseek-v4-pro", "deepseek-v4-flash"]
    )
    parser.add_argument("--agent-model", default="deepseek-v4-pro")
    parser.add_argument(
        "--source-url",
        default="https://jsonplaceholder.typicode.com/posts?_page=1&_limit=3",
    )
    parser.add_argument(
        "--scenario", choices=("generic", "aihot"), default="generic"
    )
    parser.add_argument("--source-user-agent", default="")
    parser.add_argument("--keep-temp", action="store_true")
    return parser.parse_args()


def _configure_process(temp_root: Path) -> None:
    # This live external-service check deliberately uses an isolated SQLite
    # fixture; it is not a normal platform startup or full-stack readiness test.
    os.environ["ENVIRONMENT"] = "test"
    os.environ["DATABASE_URL"] = f"sqlite:///{temp_root / 'platform.db'}"
    os.environ["UPLOADS_DIR"] = str(temp_root / "uploads")
    os.environ["STEWARD_WORKSPACE_ROOT"] = str(temp_root / "sessions")
    os.environ["API_HUB_DATA_DIR"] = str(temp_root / "api-hub")
    os.environ["STORAGE_LOCAL_DIR"] = str(temp_root / "storage")
    os.environ["SECRET_KEY"] = "steward-live-e2e-secret-key-32-bytes-minimum"
    os.environ["FIRST_ADMIN_PASSWORD"] = "steward-live-e2e-admin-password"


def _tool_protocol_probe(model: str, api_base: str, api_key: str) -> dict:
    from app.ontologies.agent_runtime.llm_bridge import chat

    tool = {
        "name": "echo_probe",
        "description": "Return the supplied probe string.",
        "parameters": {
            "type": "object",
            "properties": {"probe": {"type": "string"}},
            "required": ["probe"],
        },
    }
    started = time.monotonic()
    response = chat(
        {
            "provider": "compatible",
            "api_key": api_key,
            "api_base": api_base,
            "model": model,
        },
        [
            {"role": "system", "content": "Call the provided tool exactly once."},
            {
                "role": "user",
                "content": "Call echo_probe with probe set to platform-e2e.",
            },
        ],
        [tool],
    )
    calls = response.get("tool_calls") or []
    ok = (
        len(calls) == 1
        and calls[0].get("name") == "echo_probe"
        and (calls[0].get("arguments") or {}).get("probe") == "platform-e2e"
    )
    return {
        "ok": ok,
        "latencyMs": int((time.monotonic() - started) * 1000),
        "tool": calls[0].get("name") if calls else None,
        "usage": response.get("usage"),
    }


def _workflow_payload(
    workflow: dict,
    source_url: str,
    *,
    scenario: str,
    source_user_agent: str,
) -> tuple[list[dict], dict]:
    """Deterministic repair payload, preserving the generated webhook identity."""
    webhook = next(
        node
        for node in (workflow.get("nodes") or [])
        if node.get("type") == "n8n-nodes-base.webhook"
    )
    if scenario == "aihot":
        header_parameters = {
            "parameters": [
                {"name": "User-Agent", "value": source_user_agent}
            ]
        }
        page1 = {
            "id": str(uuid.uuid4()),
            "name": "AIHOT 第1页",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [280, 300],
            "parameters": {
                "url": source_url,
                "sendHeaders": True,
                "headerParameters": header_parameters,
                "options": {},
            },
        }
        page2 = {
            "id": str(uuid.uuid4()),
            "name": "AIHOT 第2页",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [560, 300],
            "parameters": {
                "url": source_url,
                "sendQuery": True,
                "queryParameters": {
                    "parameters": [
                        {"name": "cursor", "value": "={{ $json.nextCursor }}"}
                    ]
                },
                "sendHeaders": True,
                "headerParameters": header_parameters,
                "options": {},
            },
        }
        code_node = {
            "id": str(uuid.uuid4()),
            "name": "扁平合并",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [840, 300],
            "parameters": {
                "jsCode": (
                    "const firstPage = $('AIHOT 第1页').first().json;\n"
                    "const secondPage = $input.first().json;\n"
                    "const rows = [...(firstPage.items || []), ...(secondPage.items || [])];\n"
                    "return rows.map(row => ({ json: { id: row.id, title: row.title, "
                    "source: row.source, publishedAt: row.publishedAt } }));"
                )
            },
        }
        nodes = [webhook, page1, page2, code_node]
        connections = {
            webhook["name"]: {
                "main": [[{"node": page1["name"], "type": "main", "index": 0}]]
            },
            page1["name"]: {
                "main": [[{"node": page2["name"], "type": "main", "index": 0}]]
            },
            page2["name"]: {
                "main": [[{"node": code_node["name"], "type": "main", "index": 0}]]
            },
        }
        return nodes, connections

    http_node = {
        "id": str(uuid.uuid4()),
        "name": "分页接口取数",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [280, 300],
        "parameters": {"url": source_url, "options": {}},
    }
    code_node = {
        "id": str(uuid.uuid4()),
        "name": "扁平结果",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [560, 300],
        "parameters": {
            "jsCode": (
                "return $input.all().flatMap(item => {\n"
                "  const rows = Array.isArray(item.json) ? item.json : [item.json];\n"
                "  return rows.map(row => ({ json: { id: row.id, userId: row.userId, "
                "title: row.title } }));\n"
                "});"
            )
        },
    }
    nodes = [webhook, http_node, code_node]
    connections = {
        webhook["name"]: {
            "main": [[{"node": http_node["name"], "type": "main", "index": 0}]]
        },
        http_node["name"]: {
            "main": [[{"node": code_node["name"], "type": "main", "index": 0}]]
        },
    }
    return nodes, connections


def main() -> int:
    args = _args()
    if args.agent_model not in args.models:
        raise SystemExit("--agent-model must be included in --models")
    if args.scenario == "aihot" and not args.source_user_agent.strip():
        raise SystemExit("--source-user-agent is required for the aihot scenario")

    n8n_key = _secret("N8N_API_KEY", "N8N_API_KEY: ")
    llm_key = _secret("LLM_API_KEY", "LLM_API_KEY: ")
    temp_root = Path(tempfile.mkdtemp(prefix="ontology-steward-live-e2e-"))
    _configure_process(temp_root)

    # The backend package is a sibling of this script's directory.
    backend_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend_root))

    report: dict = {
        "ok": False,
        "tempRoot": str(temp_root) if args.keep_temp else "removed-after-run",
        "models": {},
        "n8n": {},
        "agent": {},
        "workspace": {},
        "cleanup": {"deletedWorkflowIds": [], "errors": []},
    }
    client = None
    remote_ids: set[str] = set()
    prefix = f"OB-LIVE-E2E-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    try:
        from app.main import _seed_db
        from app.database import SessionLocal
        from app.models.model_config import ModelConfig
        from app.models.user import User
        from app.models.workflow_config import WorkflowConfig
        from app.services.encryption_service import encrypt
        from app.data_channel.steward import service, workspace
        from app.data_channel.steward.models import N8nPipeline
        from app.data_channel.steward.orchestrator import run_steward_turn
        from app.data_channel.steward.runner import trigger_and_collect

        _seed_db()
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.role == "admin").first()
            if user is None:
                raise RuntimeError("isolated database did not seed an admin user")

            workflow_cfg = WorkflowConfig(
                id="default",
                enabled=True,
                api_url=args.n8n_api_url,
                api_key_encrypted=encrypt(n8n_key),
                timeout_seconds=30,
            )
            db.merge(workflow_cfg)

            model_ids: dict[str, str] = {}
            for model in args.models:
                config_id = str(uuid.uuid4())
                model_ids[model] = config_id
                db.add(
                    ModelConfig(
                        id=config_id,
                        name=f"live-e2e-{model}",
                        config_type="llm",
                        provider="compatible",
                        api_base=args.llm_api_base,
                        api_key_encrypted=encrypt(llm_key),
                        models=[model],
                        options={"max_output_tokens": 4096},
                        enabled=True,
                        is_default=model == args.agent_model,
                        created_by=user.id,
                    )
                )
            db.commit()

            client = service.get_n8n_client(db)
            started = time.monotonic()
            client.test_connection()
            report["n8n"]["connection"] = {
                "ok": True,
                "latencyMs": int((time.monotonic() - started) * 1000),
            }

            for model in args.models:
                report["models"][model] = _tool_protocol_probe(
                    model, args.llm_api_base, llm_key
                )

            if args.scenario == "aihot":
                source_design = f"""
数据源是浏览器已确认的 AI HOT 官方公开 JSON API：{args.source_url}
该端点匿名访问，但两个 HTTP Request 都必须设置请求头 User-Agent：{args.source_user_agent}
这是 cursor 分页：第 1 页响应的 nextCursor 必须作为第 2 页 cursor 查询参数，表达式为
={{ $json.nextCursor }}。节点链路必须为：Webhook -> AIHOT 第1页 -> AIHOT 第2页 -> Code。
Code 合并两页 items，并输出 id、title、source、publishedAt 四个扁平字段。
""".strip()
            else:
                source_design = f"""
数据源是已确认的合法公开测试接口：{args.source_url}
目标节点链路：保留平台随机 Webhook -> HTTP Request(GET) -> Code。
Code 将接口结果整理为一行一个 item，只保留 id、userId、title 三个扁平字段。
""".strip()

            prompt = f"""
这是本次端到端测试的正式确认，无需再次询问设计，立即执行工具创建并完善一条未发布流水线。
工作流名称必须严格为：{prefix}
{source_design}
请依次探测 URL、创建流水线、读取骨架、更新工作流并执行静态体检；不要发布或激活。
""".strip()
            started = time.monotonic()
            events = list(
                run_steward_turn(
                    db,
                    user,
                    prompt,
                    model_id=model_ids[args.agent_model],
                )
            )
            report["agent"]["latencyMs"] = int((time.monotonic() - started) * 1000)
            report["agent"]["model"] = args.agent_model
            report["agent"]["steps"] = [
                {
                    "tool": event.get("tool"),
                    "summary": event.get("summary"),
                    "error": event.get("error"),
                }
                for event in events
                if event.get("type") == "step"
            ]
            agent_error = next(
                (event.get("message") for event in events if event.get("type") == "error"),
                None,
            )
            answer = next(
                (event for event in events if event.get("type") == "answer"), {}
            )
            report["agent"]["error"] = agent_error
            report["agent"]["usage"] = answer.get("usage")
            touched = answer.get("touchedPipelineIds") or []

            rec = None
            if touched:
                rec = db.query(N8nPipeline).filter(N8nPipeline.id == touched[-1]).first()
            if rec is None:
                rec = (
                    db.query(N8nPipeline)
                    .filter(N8nPipeline.name == prefix)
                    .order_by(N8nPipeline.created_at.desc())
                    .first()
                )
            if rec is None:
                raise RuntimeError(f"data steward did not create workflow: {agent_error or 'no record'}")
            remote_ids.add(str(rec.n8n_workflow_id))
            report["agent"]["created"] = True
            report["agent"]["recordId"] = rec.id

            runner = __import__(
                "app.data_channel.steward.toolkit", fromlist=["ToolRunner"]
            ).ToolRunner(db, user.id, rec.conversation_id)
            check = runner.run("check_workflow", {"record_id": rec.id})
            report["agent"]["initialCheck"] = {
                "ok": bool(check.get("ok")),
                "issues": check.get("issues") or [],
            }
            current = client.get_workflow(rec.n8n_workflow_id)
            encoded_workflow = json.dumps(current, ensure_ascii=False)
            scenario_shape_ok = args.scenario != "aihot" or (
                sum(
                    node.get("type") == "n8n-nodes-base.httpRequest"
                    for node in current.get("nodes") or []
                ) == 2
                and "nextCursor" in encoded_workflow
                and "User-Agent" in encoded_workflow
                and "cursor" in encoded_workflow
            )
            if not check.get("ok") or not scenario_shape_ok:
                nodes, connections = _workflow_payload(
                    current,
                    args.source_url,
                    scenario=args.scenario,
                    source_user_agent=args.source_user_agent,
                )
                repaired = runner.run(
                    "update_workflow",
                    {
                        "record_id": rec.id,
                        "nodes": nodes,
                        "connections": connections,
                        "settings": current.get("settings") or {},
                    },
                )
                if repaired.get("error"):
                    raise RuntimeError(f"deterministic tool repair failed: {repaired['error']}")
                report["agent"]["deterministicRepair"] = True
                check = runner.run("check_workflow", {"record_id": rec.id})
            report["agent"]["finalCheck"] = {
                "ok": bool(check.get("ok")),
                "issues": check.get("issues") or [],
            }
            if not check.get("ok"):
                raise RuntimeError(f"workflow contract failed: {check.get('issues')}")

            contract = check["managedContract"]
            client.activate_workflow(rec.n8n_workflow_id)
            active = client.get_workflow(rec.n8n_workflow_id)
            if not active.get("active"):
                raise RuntimeError("remote workflow was not active after activation")
            report["n8n"]["activationConfirmed"] = True

            started = time.monotonic()
            rows, execution = trigger_and_collect(
                client,
                rec.n8n_workflow_id,
                contract["webhook_path"],
                payload={"e2e": True},
                wait_seconds=45,
                expected_output_node=contract["output_node_name"],
            )
            report["n8n"]["execution"] = {
                "ok": True,
                "latencyMs": int((time.monotonic() - started) * 1000),
                "executionId": execution.get("execution_id"),
                "status": execution.get("execution_status"),
                "rowCount": len(rows),
                "columns": sorted(rows[0].keys()) if rows else [],
            }
            expected_count = 4 if args.scenario == "aihot" else 3
            if len(rows) != expected_count:
                raise RuntimeError(
                    f"expected {expected_count} rows from paged source, got {len(rows)}"
                )
            expected_columns = (
                {"id", "title", "source", "publishedAt"}
                if args.scenario == "aihot"
                else {"id", "userId", "title"}
            )
            if any(set(row) != expected_columns for row in rows):
                raise RuntimeError("terminal rows are not the expected flat schema")

            artifact = workspace.save_bytes(
                rec.conversation_id,
                "n8n-paged-results.json",
                json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8"),
                source="n8n-e2e",
                mime_type="application/json",
                source_url=args.source_url,
                extract=False,
            )
            archive = workspace.archive_path(rec.conversation_id)
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                manifest = json.loads(bundle.read("manifest.json"))
            report["workspace"] = {
                "conversationId": rec.conversation_id,
                "artifact": artifact["filename"],
                "artifactBytes": artifact["size"],
                "archiveBytes": archive.stat().st_size,
                "archiveEntries": names,
                "manifestFiles": len(manifest),
                "sessionBoundary": str(archive.resolve()).startswith(
                    str(workspace.session_root(rec.conversation_id).resolve()) + os.sep
                ),
            }
            report["ok"] = all(
                item.get("ok") for item in report["models"].values()
            ) and report["agent"]["finalCheck"]["ok"]
        finally:
            db.close()
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            try:
                for workflow in client.list_workflows(limit=250):
                    if workflow.get("name") == prefix:
                        remote_ids.add(str(workflow.get("id")))
            except Exception as exc:
                report["cleanup"]["errors"].append(f"list: {exc}")
            for workflow_id in sorted(remote_ids):
                try:
                    current = client.get_workflow(workflow_id)
                    if current.get("active"):
                        client.deactivate_workflow(workflow_id)
                    client.delete_workflow(workflow_id)
                    report["cleanup"]["deletedWorkflowIds"].append(workflow_id)
                except Exception as exc:
                    report["cleanup"]["errors"].append(
                        f"workflow {workflow_id}: {exc}"
                    )
        report["cleanup"]["ok"] = not report["cleanup"]["errors"]
        if not args.keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] and report["cleanup"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
