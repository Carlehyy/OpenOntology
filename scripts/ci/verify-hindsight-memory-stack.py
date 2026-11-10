#!/usr/bin/env python
"""Hindsight 记忆链路真实验收脚本（隔离栈 oo-hindsight-acceptance）。

范围（只读生产代码，不修改任何生产/测试文件）：
- 启动受控测试模型 HTTP stub（:8000，承接 Hindsight 回调
  /api/v2/super-assistant/internal/memory/v1/chat/completions，按上游
  FactExtractionResponse {"facts":[{what/when/where/who/why/fact_type}]}
  生成确定性 facts——仅协议/故障/幂等验收，不冒充效果实测）；
- 在隔离 PG(:15432) 预置脱敏 acceptance User/ModelConfig/MemorySetting
  （前缀 hindsight-acc-，跑前清跑后清，只动本脚本 IDs）；
- 真实链路：enqueue → 真实 NATS(:14222) dispatch → durable consumer →
  memory_tasks.process_message → operation completed → recall 命中 →
  forget → document 消失且 recall 无结果；含完成重放不重复 retain。
- 本测试 bank 禁用 observations（stub 不模拟 consolidation），报告中
  明示未覆盖 observation 整合。

结果写 .artifacts/hindsight-memory-acceptance/<ts>.json；exit 0=全过。
运行：cd backend && uv run python ../scripts/ci/verify-hindsight-memory-stack.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))  # 脚本目录不在 backend，补 app 包路径
RUN_TOKEN = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
PREFIX = "hindsight-acc-"
NATS_URL = "nats://localhost:14222"
HINDSIGHT_BASE = "http://localhost:18888"
HINDSIGHT_KEY = "e2e-hindsight-api-key"
SERVICE_TOKEN = "e2e-hindsight-service-token"
STUB_PORT = 8000
NAMESPACE = "oo-acceptance"
PG_URL_CANDIDATES = [
    "postgresql://ontoprompt:ontoprompt@localhost:15432/ontoprompt",
    "postgresql+psycopg://ontoprompt:ontoprompt@localhost:15432/ontoprompt",
]

# 平台进程环境必须在 import app.* 之前就位（进程内生效，不触碰任何 .env）
for _key, _value in {
    "ENVIRONMENT": "test",
    "NATS_URL": NATS_URL,
    "HINDSIGHT_BASE_URL": HINDSIGHT_BASE,
    "HINDSIGHT_API_KEY": HINDSIGHT_KEY,
    "HINDSIGHT_TIMEOUT_SECONDS": "5.0",
    "HINDSIGHT_NAMESPACE": NAMESPACE,
    "SUPER_ASSISTANT_MEMORY_DRAIN_ENABLED": "false",
}.items():
    os.environ.setdefault(_key, _value)


def _pick_pg_url() -> str:
    import sqlalchemy

    for url in PG_URL_CANDIDATES:
        try:
            engine = sqlalchemy.create_engine(url)
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            engine.dispose()
            return url
        except Exception:
            continue
    raise SystemExit("FAIL preflight: 隔离 PG :15432 不可达（stack 未启动？）")


os.environ["DATABASE_URL"] = _pick_pg_url()

RESULTS: list[dict] = []
STUB_CALLS = {"chat": 0, "bodies": [], "lock": threading.Lock()}


def record(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail[:500]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


# ---------------------------------------------------------------------------
# 受控测试模型 stub（Hindsight 回调 + 平台探针共用；确定性 facts）
# ---------------------------------------------------------------------------

_FACTS_PAYLOAD = {
    "facts": [
        {
            "what": "用户偏好深色主题",
            "when": "N/A", "where": "N/A", "who": "用户",
            "why": "用户明确陈述的界面偏好", "fact_type": "world",
            "entities": ["用户"],
        },
        {
            "what": "用户常驻上海",
            "when": "N/A", "where": "上海", "who": "用户",
            "why": "用户明确陈述的常驻城市", "fact_type": "world",
            "entities": ["用户", "上海"],
        },
    ]
}


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默访问日志
        pass

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._send(200, {"status": "healthy"})

    def do_POST(self):  # noqa: N802
        if not self.path.endswith("/chat/completions"):
            self._send(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        with STUB_CALLS["lock"]:
            STUB_CALLS["chat"] += 1
            STUB_CALLS["bodies"].append({
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "body": raw.decode("utf-8", "replace")[:2000],
            })
        model = "oo-memory-default"
        try:
            model = json.loads(raw).get("model") or model
        except Exception:
            pass
        self._send(200, {
            "id": f"chatcmpl-stub-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(_FACTS_PAYLOAD, ensure_ascii=False),
                },
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        })


def main() -> int:
    print(f"== Hindsight 记忆链路验收 run={RUN_TOKEN} ==")
    stub = ok_all = None
    nats_conn = js = sub = None
    import httpx

    from app.database import SessionLocal
    from app.super_assistant import hindsight_memory as hm
    from app.super_assistant import memory_tasks as mt
    from app.super_assistant.hindsight_client import HindsightClient
    from app.super_assistant.memory_models import (
        SuperAssistantMemorySetting,
        SuperAssistantMemorySource,
    )

    try:
        # --- 0. preflight ---
        import socket

        probe = socket.socket()
        probe.settimeout(0.5)
        busy = probe.connect_ex(("127.0.0.1", STUB_PORT)) == 0
        probe.close()
        if busy:
            raise SystemExit(f"FAIL preflight: 端口 {STUB_PORT} 已被占用，拒绝启动 stub")
        stub = ThreadingHTTPServer(("0.0.0.0", STUB_PORT), _StubHandler)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        record("stub-started(:8000)", True)

        client = httpx.Client(
            base_url=HINDSIGHT_BASE,
            headers={"Authorization": f"Bearer {HINDSIGHT_KEY}"},
            timeout=10.0,
        )
        health = client.get("/health")
        record("hindsight-health", health.status_code == 200, f"HTTP {health.status_code}")
        anon = httpx.Client(base_url=HINDSIGHT_BASE, timeout=5.0).get(
            "/v1/default/banks")
        record("hindsight-anon-401", anon.status_code == 401, f"HTTP {anon.status_code}")

        # --- 1. 隔离 PG 预置脱敏数据（幂等：先清旧 acceptance 前缀） ---
        from app.model_configs.models import ModelConfig
        from app.models.user import User
        from app.services.auth_service import hash_password

        owner_id = f"{PREFIX}user-{RUN_TOKEN}"
        model_id = f"{PREFIX}model-{RUN_TOKEN}"
        with SessionLocal() as db:
            db.query(SuperAssistantMemorySource).filter(
                SuperAssistantMemorySource.owner_id.like(f"{PREFIX}%")
            ).delete(synchronize_session=False)
            db.query(SuperAssistantMemorySetting).filter(
                SuperAssistantMemorySetting.owner_id.like(f"{PREFIX}%")
            ).delete(synchronize_session=False)
            db.query(ModelConfig).filter(
                ModelConfig.id.like(f"{PREFIX}%")
            ).delete(synchronize_session=False)
            db.query(User).filter(User.id.like(f"{PREFIX}%")).delete(
                synchronize_session=False
            )
            db.add(User(
                id=owner_id, username=f"acc-{RUN_TOKEN}",
                email=f"acc-{RUN_TOKEN}@acceptance.invalid",
                password_hash=hash_password("acceptance-only"),
                role="admin",
            ))
            db.add(ModelConfig(
                id=model_id, name="acceptance-memory-model", config_type="llm",
                provider="openai",
                api_base=f"http://127.0.0.1:{STUB_PORT}/api/v2/super-assistant/internal/memory/v1",
                api_key_encrypted=None, models=["oo-memory-default"],
                options={}, enabled=True, is_default=False, created_by=owner_id,
            ))
            db.add(SuperAssistantMemorySetting(
                owner_id=owner_id, enabled=True, model_config_id=model_id,
            ))
            db.commit()
        record("pg-seeded-isolated", True, f"owner={owner_id}")

        bank_id = hm.bank_id_for(owner_id)
        hs_client = HindsightClient(HINDSIGHT_BASE, HINDSIGHT_KEY, timeout=20.0)

        # --- 2. 真实 NATS：ensure stream + durable consumer ---
        import asyncio
        import threading as _threading

        import nats

        # 全程单一事件循环线程：conn/js/sub 绑定同一 loop（避免 loop 关闭错乱）
        _loop = asyncio.new_event_loop()
        _threading.Thread(target=_loop.run_forever, daemon=True).start()

        def run_async(coro, timeout: float = 40.0):
            return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)

        async def _nats_setup():
            from app.data_channel.pipeline_tasks.dispatch import (
                PIPELINE_STREAM,
                PIPELINE_STREAM_SUBJECTS,
                ensure_pipeline_stream,
            )
            from nats.js.api import RetentionPolicy, StreamConfig

            conn = await nats.connect(NATS_URL)
            js_ = conn.jetstream()
            try:
                await ensure_pipeline_stream(js_)  # 复用生产配置建流/合并主题
            except Exception:
                # 本脚本历史运行遗留的配置漂移：归一到生产配置（仅隔离栈）
                await js_.update_stream(StreamConfig(
                    name=PIPELINE_STREAM,
                    subjects=sorted(set(PIPELINE_STREAM_SUBJECTS)
                                    | {"super_assistant.memory.sync"}),
                    retention=RetentionPolicy.WORK_QUEUE,
                    max_age=7 * 24 * 3600,
                    duplicate_window=10 * 60,
                ))
                await ensure_pipeline_stream(js_)
            sub_ = await js_.pull_subscribe(
                "super_assistant.memory.sync",
                durable="sa-memory-sync-acceptance",
            )
            return conn, js_, sub_

        nats_conn, js, sub = run_async(_nats_setup())
        record("nats-stream-consumer", True, "durable=sa-memory-sync-acceptance")

        def consume_once(timeout: float = 20.0) -> dict | None:
            async def _one():
                msgs = await sub.fetch(1, timeout=timeout)
                payload = json.loads(msgs[0].data.decode())
                await msgs[0].ack()
                return payload

            try:
                return run_async(_one(), timeout=timeout + 10)
            except Exception:
                return None

        from app.data_channel.pipeline_tasks.dispatch import (
            dispatch_super_assistant_memory_sync,
        )

        # --- 3. enqueue（同事务语义）→ 真实 dispatch → consume → process ---
        with SessionLocal() as db:
            source = hm.enqueue_message(
                db, owner_id=owner_id, conversation_id=f"{PREFIX}conv",
                message_id=f"{PREFIX}msg-1", role="user",
                content="请记住：我喜欢深色主题，并且我常驻上海。",
                occurred_at=datetime.now(timezone.utc),
                context="acceptance",
            )
            db.commit()
            source_id = source.id
            dispatch_token = source.dispatch_id
        record("enqueue-source", True, f"source={source_id[:8]}…")

        # 本测试 bank 关闭 observations（stub 不模拟 consolidation，见脚本头）
        hs_client.update_bank_config(bank_id, {"enable_observations": False})

        dispatch_super_assistant_memory_sync(source_id, dispatch_token)
        payload = consume_once()
        record("nats-roundtrip", isinstance(payload, dict)
               and payload.get("source_id") == source_id, f"payload={payload}")
        mt.process_message(payload or {"source_id": source_id}, poll_budget=25.0)

        status = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                row = db.get(SuperAssistantMemorySource, source_id)
                status = row.status
            if status in ("synced", "failed", "dead", "forgetting", "forgotten"):
                break
            mt.process_message({"source_id": source_id}, poll_budget=25.0)
            time.sleep(2)
        record("retain-completed", status == "synced", f"status={status}")
        with STUB_CALLS["lock"]:
            chat_calls = STUB_CALLS["chat"]
        record("stub-llm-callbacks", chat_calls > 0, f"chat_calls={chat_calls}")
        with SessionLocal() as db:
            row = db.get(SuperAssistantMemorySource, source_id)
            document_id, operation_id = row.document_id, row.operation_id
        record("operation-recorded", bool(operation_id and document_id),
               f"op={operation_id} doc={document_id}")

        # --- 4. recall 命中确定性事实 ---
        with SessionLocal() as db:
            found = hm.search_memories(db, owner_id, "用户的界面偏好 深色 主题")
        hit = any("深色主题" in item["text"] for item in found.get("items") or [])
        record("recall-hit", found.get("state") == "ready" and hit,
               f"state={found.get('state')} items={len(found.get('items') or [])}")

        # --- 5. 完成重放：不重复 retain（幂等） ---
        dispatch_super_assistant_memory_sync(source_id, f"replay-{uuid.uuid4().hex}")
        replay_payload = consume_once()
        if replay_payload:
            mt.process_message(replay_payload, poll_budget=5.0)
        with STUB_CALLS["lock"]:
            calls_after_replay = STUB_CALLS["chat"]
        record("replay-no-retain", calls_after_replay == chat_calls,
               f"chat_calls {chat_calls}->{calls_after_replay}")

        # --- 6. forget：tombstone → op 终态 → 删 doc → 核实 ---
        with SessionLocal() as db:
            hm.forget_source(db, owner_id, source_id)
            db.commit()
        dispatch_super_assistant_memory_sync(
            source_id, f"forget-{uuid.uuid4().hex}")
        forget_payload = consume_once()
        if forget_payload:
            mt.process_message(forget_payload, poll_budget=25.0)
        status = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                status = db.get(SuperAssistantMemorySource, source_id).status
            if status == "forgotten":
                break
            mt.process_message({"source_id": source_id}, poll_budget=10.0)
            time.sleep(2)
        record("forget-completed", status == "forgotten", f"status={status}")

        listing = hs_client.list_documents(bank_id, q=document_id, limit=100)
        leftover = [i for i in listing.get("items") or []
                    if str(i.get("id")) == document_id]
        record("document-absent", not leftover, f"leftover={leftover}")

        with SessionLocal() as db:
            after = hm.search_memories(db, owner_id, "用户的界面偏好 深色 主题")
        leaked = any("深色主题" in item["text"] for item in after.get("items") or [])
        record("recall-empty-after-forget", not leaked,
               f"state={after.get('state')} items={len(after.get('items') or [])}")

        # --- 7. 先撤回尚未受理的 operation，再模拟旧请求延迟到达 ---
        with SessionLocal() as db:
            late_source = hm.enqueue_message(
                db, owner_id=owner_id, conversation_id=f"{PREFIX}conv",
                message_id=f"{PREFIX}msg-late", role="user",
                content="这条旧请求已经撤回，不得在遗忘完成后重新写入。",
                occurred_at=datetime.now(timezone.utc),
            )
            late_id = late_source.id
            late_operation = str(uuid.uuid4())
            late_source.operation_id = late_operation
            late_source.operation_explicit = False
            late_item = hm.build_retain_item(late_source)
            # 与真实 worker 一致：operation 必须先独立提交，遗忘请求才到达。
            db.commit()
        with SessionLocal() as db:
            assert db.get(SuperAssistantMemorySource, late_id).operation_id == late_operation
            hm.forget_source(db, owner_id, late_id)
            db.commit()
        mt.advance_forget(late_id, poll_budget=20.0)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                late_status = db.get(SuperAssistantMemorySource, late_id).status
            if late_status == "forgotten":
                break
            time.sleep(2)
            mt.advance_forget(late_id, poll_budget=10.0)
        record("unknown-operation-forget-fenced", late_status == "forgotten",
               f"status={late_status}")
        with STUB_CALLS["lock"]:
            calls_before_late = STUB_CALLS["chat"]
        hs_client.retain_async(bank_id, [late_item], late_operation)
        time.sleep(2)
        late_listing = hs_client.list_documents(bank_id, q=late_id)
        with STUB_CALLS["lock"]:
            calls_after_late = STUB_CALLS["chat"]
        late_docs = [item for item in late_listing["items"] if item["id"] == late_id]
        record("late-retain-cannot-resurrect", not late_docs
               and calls_after_late == calls_before_late,
               f"documents={len(late_docs)} callbacks={calls_before_late}->{calls_after_late}")
        with SessionLocal() as db:
            late_row = db.get(SuperAssistantMemorySource, late_id)
            record("forgotten-source-erased", late_row.status == "forgotten"
                   and late_row.content == "" and late_row.context == "")

        ok_all = all(r["ok"] for r in RESULTS)
    finally:
        # --- 清理：只动本脚本前缀/bank；临时进程全部终止 ---
        cleanup_notes = []
        try:
            if js is not None:
                async def _teardown():
                    try:
                        await js.delete_consumer(
                            "PIPELINE_TASKS", "sa-memory-sync-acceptance")
                    except Exception:
                        pass
                    if nats_conn is not None:
                        await nats_conn.drain()

                run_async(_teardown())
                _loop.call_soon_threadsafe(_loop.stop)
                cleanup_notes.append("nats consumer 删除/连接 drain/loop 停止")
        except Exception as exc:
            cleanup_notes.append(f"nats 清理异常: {exc}")
        try:
            with SessionLocal() as db:
                db.query(SuperAssistantMemorySource).filter(
                    SuperAssistantMemorySource.owner_id.like(f"{PREFIX}%")
                ).delete(synchronize_session=False)
                db.query(SuperAssistantMemorySetting).filter(
                    SuperAssistantMemorySetting.owner_id.like(f"{PREFIX}%")
                ).delete(synchronize_session=False)
                db.query(ModelConfig).filter(
                    ModelConfig.id.like(f"{PREFIX}%")
                ).delete(synchronize_session=False)
                db.query(User).filter(User.id.like(f"{PREFIX}%")).delete(
                    synchronize_session=False
                )
                db.commit()
            cleanup_notes.append("PG acceptance 前缀行已清")
        except Exception as exc:
            cleanup_notes.append(f"PG 清理异常: {exc}")
        try:
            if client is not None:
                # 只删本脚本命名 bank（namespace=oo-acceptance 派生）
                bank = None
                try:
                    from app.super_assistant import hindsight_memory as _hm

                    bank = _hm.bank_id_for(owner_id)
                except Exception:
                    pass
                if bank:
                    client.delete(f"/v1/default/banks/{bank}")
                cleanup_notes.append(f"hindsight bank 已删({bank})")
        except Exception as exc:
            cleanup_notes.append(f"bank 清理异常: {exc}")
        if stub is not None:
            stub.shutdown()
            stub.server_close()
            cleanup_notes.append("stub(:8000) 已停止")
        print("cleanup:", "; ".join(cleanup_notes))

    artifact = {
        "run": RUN_TOKEN,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "checks": RESULTS,
        "passed": sum(1 for r in RESULTS if r["ok"]),
        "failed": sum(1 for r in RESULTS if not r["ok"]),
        "uncovered": [
            "observation/consolidation（本测试 bank 已禁用；stub 未模拟整合链路）",
            "记忆效果评估（stub facts 为确定性协议样本，非真实抽取质量）",
        ],
        "cleanup": cleanup_notes,
    }
    out_dir = ROOT / ".artifacts" / "hindsight-memory-acceptance"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{RUN_TOKEN}.json"
    out_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"\n== RESULT: {artifact['passed']} passed / {artifact['failed']} failed ==")
    print(f"artifact: {out_path}")
    return 0 if ok_all and artifact["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
