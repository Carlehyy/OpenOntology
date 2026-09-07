#!/usr/bin/env python3
"""Real DeepSeek, loopback-HTTP E2E checks for steward context continuity.

The API key is read with ``getpass`` and injected into the orchestrator only in
memory. It is never placed in argv, environment variables, the temporary
database, model-call logs, reports, or repository files. Before the isolated
temporary directory is removed, every regular file below it is scanned for the
exact API-key bytes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:bearer\s+)?(?:sk|api|token|secret)[-_][A-Za-z0-9._-]{12,}\b"
)


def _safe_error(exc: Exception, api_key: str) -> str:
    value = str(exc).replace(api_key, "***") if api_key else str(exc)
    return _SECRET_PATTERN.sub("***", value)[:800]


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm-api-base", default="https://api.deepseek.com")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--context-tokens", type=int, default=64_000)
    parser.add_argument("--output-tokens", type=int, default=2_048)
    return parser.parse_args()


def _validated_deepseek_base(value: str) -> str:
    """Accept only DeepSeek's HTTPS API origin (optionally with its /v1 alias)."""
    raw = (value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise SystemExit(f"无效的 DeepSeek API 地址: {exc}") from exc
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "api.deepseek.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or path not in ("", "/v1")
    ):
        raise SystemExit(
            "--llm-api-base 必须是 https://api.deepseek.com（可选 /v1，禁止凭据、"
            "查询串、片段或非 443 端口）"
        )
    return urlunsplit(("https", "api.deepseek.com", path, "", ""))


def _configure_isolated_process(root: Path) -> tuple[str, str]:
    admin_user = "steward_context_live_admin"
    admin_password = "Steward-Context-Live-Admin-2026!"
    isolated = {
        "ENVIRONMENT": "test",
        "DATABASE_URL": f"sqlite:///{root / 'live.db'}",
        "UPLOADS_DIR": str(root / "uploads"),
        "STEWARD_WORKSPACE_ROOT": str(root / "workspace"),
        "API_HUB_DATA_DIR": str(root / "api-hub"),
        "STORAGE_LOCAL_DIR": str(root / "storage"),
        "SECRET_KEY": "steward-context-live-e2e-secret-key-32chars",
        "FIRST_ADMIN_USER": admin_user,
        "FIRST_ADMIN_PASSWORD": admin_password,
        # Never let a developer's .env point this test at their Redis/Neo4j.
        # Port 1 on loopback fails locally and cannot reach an external service.
        "REDIS_URL": "redis://127.0.0.1:1/15",
        "NEO4J_URI": "bolt://127.0.0.1:1",
        "NEO4J_USER": "steward-context-live",
        "NEO4J_PASSWORD": "steward-context-live-isolated",
        # This worker has no stop hook. Disable it so the temporary database has
        # no process-global writer after the Uvicorn lifespan shuts down.
        "SENTINEL_SCAN_ENABLED": "0",
    }
    os.environ.update(isolated)
    return admin_user, admin_password


def _positive_usage(response: dict[str, Any]) -> bool:
    usage = response.get("usage") or {}
    return (
        int(usage.get("inputTokens") or 0) > 0
        and int(usage.get("outputTokens") or 0) > 0
    )


def _chat(client: Any, headers: dict[str, str],
          payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/api/v2/steward/chat",
        headers=headers,
        json={**payload, "stream": False},
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()["data"]
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data


def _chat_sse(client: Any, headers: dict[str, str],
              payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Exercise the real StreamingResponse over an actual loopback socket."""
    events: list[dict[str, Any]] = []
    content_type = ""
    with client.stream(
        "POST",
        "/api/v2/steward/chat",
        headers=headers,
        json={**payload, "stream": True},
    ) as response:
        content_type = response.headers.get("content-type", "")
        if response.status_code != 200:
            body = response.read().decode(errors="replace")
            raise RuntimeError(f"SSE HTTP {response.status_code}: {body[:500]}")
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw:
                events.append(json.loads(raw))

    event_types = [str(event.get("type") or "") for event in events]
    error = next((event for event in events if event.get("type") == "error"), None)
    if error:
        raise RuntimeError(str(error.get("message") or "SSE turn failed"))
    meta = next((event for event in events if event.get("type") == "meta"), {})
    answer = next((event for event in reversed(events)
                   if event.get("type") == "answer"), None)
    if (
        not content_type.lower().startswith("text/event-stream")
        or not meta
        or answer is None
    ):
        raise RuntimeError(
            f"SSE 协议不完整: content-type={content_type!r}, events={event_types!r}"
        )
    steps = [
        {key: value for key, value in event.items() if key != "type"}
        for event in events
        if event.get("type") == "step"
    ]
    return {
        "conversationId": meta.get("conversationId"),
        "model": meta.get("model"),
        "steps": steps,
        "content": answer.get("content"),
        "touchedPipelineIds": answer.get("touchedPipelineIds") or [],
        "usage": answer.get("usage"),
        "error": None,
    }, event_types


def _create_conversation(client: Any, headers: dict[str, str],
                         title: str) -> str:
    response = client.post(
        "/api/v2/steward/conversations",
        headers=headers,
        json={"title": title},
    )
    if response.status_code != 201:
        raise RuntimeError(f"创建会话失败: {response.status_code} {response.text[:300]}")
    return str(response.json()["data"]["id"])


def _start_loopback_server(app: Any) -> tuple[Any, threading.Thread, socket.socket, int]:
    import uvicorn

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2048)
    port = int(listener.getsockname()[1])
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="steward-context-live-uvicorn",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 45
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("真实 Uvicorn 服务在启动完成前退出")
        if time.monotonic() >= deadline:
            raise RuntimeError("真实 Uvicorn 服务启动超时")
        time.sleep(0.05)
    return server, thread, listener, port


def _stop_loopback_server(server: Any, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=20)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=5)
    if thread.is_alive():
        raise RuntimeError("真实 Uvicorn 服务未能在超时内停止")


def _files_containing(root: Path, needle: bytes) -> list[str]:
    """Scan regular files without loading large SQLite/storage files at once."""
    if not needle:
        return []
    matches: list[str] = []
    overlap = max(0, len(needle) - 1)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            tail = b""
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    sample = tail + chunk
                    if needle in sample:
                        matches.append(str(path.relative_to(root)))
                        break
                    tail = sample[-overlap:] if overlap else b""
        except OSError:
            matches.append(f"{path.relative_to(root)} (无法扫描)")
    return matches


def main() -> int:
    args = _args()
    api_base = _validated_deepseek_base(args.llm_api_base)
    api_key = getpass.getpass("DeepSeek API key（仅驻留内存）: ").strip()
    if not api_key:
        raise SystemExit("API key 不能为空")
    secret_box = [api_key]
    results: dict[str, Any] = {
        "apiBase": api_base,
        "model": args.model,
        "checks": {},
    }
    exit_code = 1

    with tempfile.TemporaryDirectory(prefix="steward-context-live-") as temp_dir:
        root = Path(temp_dir)
        admin_user, admin_password = _configure_isolated_process(root)
        server = None
        server_thread = None
        listener = None

        try:
            # Imports intentionally happen only after all isolated settings exist.
            import httpx

            from app.main import app
            from app.database import SessionLocal
            from app.data_channel.steward import orchestrator
            from app.data_channel.steward.models import (
                StewardConversation, StewardMessage,
            )
            from app.models.user import User
            from app.ontologies.agent_runtime import llm_bridge

            runtime = {
                "context_tokens": max(8_192, int(args.context_tokens)),
                "output_tokens": max(256, int(args.output_tokens)),
            }

            def in_memory_call_kwargs(_config: Any) -> dict[str, Any]:
                return {
                    "provider": "compatible",
                    "api_key": secret_box[0],
                    "api_base": api_base,
                    "model": args.model,
                    "max_context_tokens": runtime["context_tokens"],
                    "max_output_tokens": runtime["output_tokens"],
                    "timeout_seconds": 120,
                }

            orchestrator.select_llm_model_config = lambda *_args, **_kwargs: object()
            orchestrator.llm_call_kwargs = in_memory_call_kwargs

            # Exact provider output and positive usage are prerequisites for all
            # later semantic checks.
            probe_marker = f"PONG-{secrets.token_hex(4)}"
            probe = llm_bridge.chat(
                in_memory_call_kwargs(None),
                [{
                    "role": "user",
                    "content": f"只输出字符串 {probe_marker}，不得添加空格、标点或解释。",
                }],
                [],
            )
            probe_output = str(probe.get("content") or "").strip()
            provider_ok = probe_output == probe_marker and _positive_usage(probe)
            results["checks"]["provider"] = {
                "passed": provider_ok,
                "exactOutput": probe_output == probe_marker,
                "positiveUsage": _positive_usage(probe),
            }
            if not provider_ok:
                raise RuntimeError("真实模型未通过精确文本/正 token usage 探针")

            # Require one exact call, no direct answer, then feed the actual tool
            # result back through the provider protocol and require exact closure.
            tool_marker = f"TOOL-{secrets.token_hex(4)}"
            continuation_marker = f"CONTINUED-{secrets.token_hex(4)}"
            tool_schema = {
                "name": "remember_marker",
                "description": "记录测试标记",
                "parameters": {
                    "type": "object",
                    "properties": {"marker": {"type": "string"}},
                    "required": ["marker"],
                    "additionalProperties": False,
                },
            }
            tool_user_message = {
                "role": "user",
                "content": (
                    "必须且只能调用一次 remember_marker，参数对象必须精确为 "
                    f'{{"marker":"{tool_marker}"}}；此时不得输出任何文字。'
                    f"工具返回后只输出 {continuation_marker}。"
                ),
            }
            tool_probe = llm_bridge.chat(
                in_memory_call_kwargs(None),
                [tool_user_message],
                [tool_schema],
            )
            tool_calls = tool_probe.get("tool_calls") or []
            no_direct_answer = not str(tool_probe.get("content") or "").strip()
            exact_call = (
                len(tool_calls) == 1
                and tool_calls[0].get("name") == "remember_marker"
                and tool_calls[0].get("arguments") == {"marker": tool_marker}
                and bool(tool_calls[0].get("id"))
            )
            continuation = {"content": None, "tool_calls": []}
            if exact_call and no_direct_answer:
                continuation = llm_bridge.chat(
                    in_memory_call_kwargs(None),
                    [
                        tool_user_message,
                        {
                            "role": "assistant",
                            "content": tool_probe.get("content"),
                            "tool_calls": tool_calls,
                        },
                        {
                            "role": "tool",
                            "tool_call_id": tool_calls[0]["id"],
                            "name": "remember_marker",
                            "content": json.dumps(
                                {"stored": True, "marker": tool_marker},
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    [tool_schema],
                )
            continuation_ok = (
                str(continuation.get("content") or "").strip()
                == continuation_marker
                and not (continuation.get("tool_calls") or [])
                and _positive_usage(continuation)
            )
            tool_ok = exact_call and no_direct_answer and continuation_ok
            results["checks"]["toolProtocol"] = {
                "passed": tool_ok,
                "exactlyOneCall": len(tool_calls) == 1,
                "exactArguments": exact_call,
                "noDirectAnswer": no_direct_answer,
                "continuation": continuation_ok,
            }
            if not tool_ok:
                raise RuntimeError("真实模型未通过严格工具调用及 tool-result continuation 探针")

            server, server_thread, listener, port = _start_loopback_server(app)
            timeout = httpx.Timeout(180.0, connect=10.0)
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                timeout=timeout,
            ) as client:
                live = client.get("/health/live")
                if live.status_code != 200:
                    raise RuntimeError(
                        f"真实 loopback HTTP 服务不可用: {live.status_code} {live.text[:200]}"
                    )
                login = client.post(
                    "/api/v1/auth/login",
                    json={"username": admin_user, "password": admin_password},
                )
                if login.status_code != 200:
                    raise RuntimeError(f"隔离管理员登录失败: {login.text[:300]}")
                token = login.json()["data"]["access_token"]
                headers = {"Authorization": f"Bearer {token}"}

                # Old implementation forgot this first fact at the eighth user
                # turn. The first turn intentionally traverses real HTTP SSE.
                long_conversation = _create_conversation(
                    client, headers, "真实长对话")
                long_marker = f"LONG-{secrets.token_hex(6)}"
                first, sse_event_types = _chat_sse(client, headers, {
                    "conversationId": long_conversation,
                    "message": (
                        f"请在本会话记住项目校验码 {long_marker}。"
                        "只确认已经记住，不调用工具。"
                    ),
                })
                sse_ok = (
                    "meta" in sse_event_types
                    and "answer" in sse_event_types
                    and not first.get("error")
                )
                results["checks"]["sseTransport"] = {
                    "passed": sse_ok,
                    "loopbackSocket": True,
                    "events": sse_event_types,
                }
                if not sse_ok:
                    raise RuntimeError("真实 loopback SSE 回合失败")

                lifetime_usage = {
                    "inputTokens": int(
                        (first.get("usage") or {}).get("inputTokens") or 0),
                    "outputTokens": int(
                        (first.get("usage") or {}).get("outputTokens") or 0),
                }
                for index in range(2, 8):
                    turn = _chat(client, headers, {
                        "conversationId": long_conversation,
                        "message": (
                            f"这是第 {index} 轮占位，只回复 ACK-{index}，不调用工具。"
                        ),
                    })
                    for key in lifetime_usage:
                        lifetime_usage[key] += int(
                            (turn.get("usage") or {}).get(key) or 0)
                recall = _chat(client, headers, {
                    "conversationId": long_conversation,
                    "message": "第八轮：项目校验码是什么？只输出校验码。",
                })
                long_ok = long_marker in str(recall.get("content") or "")
                results["checks"]["longConversation"] = {
                    "passed": long_ok,
                    "turns": 8,
                    "markerSha256": hashlib.sha256(
                        long_marker.encode()).hexdigest(),
                    "usage": lifetime_usage,
                }
                if not long_ok:
                    raise RuntimeError("真实 8 回合测试未召回首轮校验码")

                # Real HTTP upload -> real model tool calls -> persisted
                # observation -> HTTP DELETE -> next HTTP turn.
                file_conversation = _create_conversation(
                    client, headers, "真实工具观察")
                file_marker = f"FILE-{secrets.token_hex(6)}"
                upload = client.post(
                    f"/api/v2/steward/conversations/{file_conversation}/files",
                    headers=headers,
                    files={
                        "file": (
                            "context-marker.txt",
                            f"唯一文件校验码={file_marker}".encode(),
                            "text/plain",
                        ),
                    },
                )
                if upload.status_code != 201:
                    raise RuntimeError(f"文件上传失败: {upload.text[:300]}")
                artifact_id = upload.json()["data"]["id"]
                read_turn = _chat(client, headers, {
                    "conversationId": file_conversation,
                    "message": (
                        "请调用 read_session_file 读取 context-marker.txt；"
                        "如需定位文件可先调用 list_session_files。"
                        "最终只回复“读取完成”，不要复述校验码。"
                    ),
                })
                steps = read_turn.get("steps") or []
                step_names = [step.get("tool") for step in steps]
                read_indexes = [
                    index for index, name in enumerate(step_names)
                    if name == "read_session_file"
                ]
                list_indexes = [
                    index for index, name in enumerate(step_names)
                    if name == "list_session_files"
                ]
                read_succeeded = (
                    bool(read_indexes)
                    and not any(step.get("error") for step in steps)
                )
                optional_list_order_ok = (
                    not list_indexes
                    or list_indexes[0] < read_indexes[0]
                )

                file_url = (
                    f"/api/v2/steward/conversations/{file_conversation}"
                    f"/files/{artifact_id}"
                )
                deleted = client.delete(file_url, headers=headers)
                preview_after_delete = client.get(
                    f"{file_url}/preview", headers=headers)
                download_after_delete = client.get(file_url, headers=headers)
                delete_ok = (
                    deleted.status_code == 204
                    and preview_after_delete.status_code == 404
                    and download_after_delete.status_code == 404
                )
                if not delete_ok:
                    raise RuntimeError(
                        "HTTP 文件删除/404 验证失败: "
                        f"delete={deleted.status_code}, "
                        f"preview={preview_after_delete.status_code}, "
                        f"download={download_after_delete.status_code}"
                    )

                recall_file = _chat(client, headers, {
                    "conversationId": file_conversation,
                    "message": "刚才文件里的唯一文件校验码是什么？只输出校验码。",
                })
                immediate_answer_hid_marker = (
                    file_marker not in str(read_turn.get("content") or "")
                )
                recalled_after_delete = (
                    file_marker in str(recall_file.get("content") or "")
                )
                file_ok = (
                    read_succeeded
                    and optional_list_order_ok
                    and delete_ok
                    and immediate_answer_hid_marker
                    and recalled_after_delete
                )
                results["checks"]["toolObservation"] = {
                    "passed": file_ok,
                    "tools": step_names,
                    "readSucceeded": read_succeeded,
                    "optionalListOrder": optional_list_order_ok,
                    "immediateAnswerHidMarker": immediate_answer_hid_marker,
                    "recalledAfterDelete": recalled_after_delete,
                    "httpDelete": deleted.status_code,
                    "previewAfterDelete": preview_after_delete.status_code,
                    "downloadAfterDelete": download_after_delete.status_code,
                    "markerSha256": hashlib.sha256(
                        file_marker.encode()).hexdigest(),
                }
                if not file_ok:
                    raise RuntimeError("真实工具观察跨轮召回失败")

                # Seed a large audited transcript, then enter through the real
                # HTTP endpoint. This forces real LLM compaction without dozens
                # of paid filler calls and verifies that the audit stays intact.
                compact_conversation = _create_conversation(
                    client, headers, "真实压缩极限")
                compact_marker = f"COMPACT-{secrets.token_hex(6)}"
                runtime["context_tokens"] = 16_384
                with SessionLocal() as db:
                    owner = db.query(User).filter(
                        User.username == admin_user).one()
                    conversation = db.query(StewardConversation).filter(
                        StewardConversation.id == compact_conversation,
                        StewardConversation.user_id == owner.id,
                    ).one()
                    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
                    for index in range(12):
                        constraint = (
                            f"永久校验约束={compact_marker}；"
                            if index == 0 else f"第{index}轮补充；"
                        )
                        db.add(StewardMessage(
                            conversation_id=conversation.id,
                            role="user",
                            content=constraint + "背景资料" * 800,
                            created_at=started + timedelta(seconds=index * 2),
                        ))
                        db.add(StewardMessage(
                            conversation_id=conversation.id,
                            role="assistant",
                            content=f"已记录第{index}轮。" + "分析过程" * 500,
                            created_at=started + timedelta(seconds=index * 2 + 1),
                        ))
                    db.commit()

                compact_recall = _chat(client, headers, {
                    "conversationId": compact_conversation,
                    "message": "永久校验约束是什么？只输出其中的校验码，不调用工具。",
                })
                with SessionLocal() as db:
                    compacted = db.query(StewardConversation).filter(
                        StewardConversation.id == compact_conversation,
                    ).one()
                    stats = dict(compacted.context_stats or {})
                    summarized = int(compacted.summary_message_count or 0)
                    audit_count = db.query(StewardMessage).filter(
                        StewardMessage.conversation_id
                        == compact_conversation,
                    ).count()

                input_budget = int(stats.get("inputBudget") or 0)
                peak_names = (
                    "peakProviderInputTokens",
                    "peakEstimatedInputTokens",
                    "peakCompactionInputTokens",
                    "peakCompactionEstimatedInputTokens",
                )
                peaks = {
                    name: int(stats.get(name) or 0)
                    for name in peak_names
                }
                peaks_ok = (
                    input_budget > 0
                    and all(0 < value <= input_budget
                            for value in peaks.values())
                )
                compact_ok = (
                    compact_marker in str(compact_recall.get("content") or "")
                    and summarized > 0
                    and audit_count == 26
                    and stats.get("lastCompactionMode") == "llm"
                    and peaks_ok
                )
                results["checks"]["compactionExtreme"] = {
                    "passed": compact_ok,
                    "summarizedMessages": summarized,
                    "auditMessages": audit_count,
                    "lastCompactionMode": stats.get("lastCompactionMode"),
                    **peaks,
                    "inputBudget": input_budget,
                    "budgetUtilization": stats.get("budgetUtilization"),
                    "markerSha256": hashlib.sha256(
                        compact_marker.encode()).hexdigest(),
                }
                if not compact_ok:
                    raise RuntimeError("真实压缩极限测试失败")

        except Exception as exc:  # noqa: BLE001
            results["error"] = _safe_error(exc, secret_box[0])
        finally:
            shutdown_error: Exception | None = None
            if server is not None and server_thread is not None:
                try:
                    _stop_loopback_server(server, server_thread)
                except Exception as exc:  # noqa: BLE001
                    shutdown_error = exc
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            if shutdown_error is not None and "error" not in results:
                results["error"] = _safe_error(shutdown_error, secret_box[0])

        # The server is stopped before this scan, so no background writer can
        # place the key on disk after the assertion.
        leak_files = _files_containing(root, secret_box[0].encode())
        secret_absent = not leak_files
        results["checks"]["secretAbsentOnDisk"] = {
            "passed": secret_absent,
            "filesScanned": sum(1 for path in root.rglob("*") if path.is_file()),
            "leakFiles": leak_files,
        }
        if not secret_absent and "error" not in results:
            results["error"] = "检测到 API key 字节被写入隔离临时目录"

        results["passed"] = (
            "error" not in results
            and bool(results["checks"])
            and all(
                bool(item.get("passed"))
                for item in results["checks"].values()
            )
        )
        exit_code = 0 if results["passed"] else 1
        secret_box[0] = ""
        api_key = ""
        print(json.dumps(results, ensure_ascii=False, indent=2))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
