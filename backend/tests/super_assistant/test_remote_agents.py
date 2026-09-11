"""远程助手（声明式注册）与虚构委派兜底的测试。

远程助手：CRUD/校验、HTTP 契约 adapter（_request 桩）、动态目录合并、
引擎端到端（经 provider 的真实目录→工具执行）。
虚构委派兜底：suspected_fabrication_reminder 判别 + 流内提醒注入。
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.assistant_hub import contract, registry
from app.assistant_hub.contract import STATUS_ANSWERED, STATUS_FAILED, TurnResult
from app.auth.models import RoleMenuPermission, User
from app.model_configs.models import ModelConfig
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import delegation, remote_agent_service, runtime
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantDelegation,
    SuperAssistantMcpServer,
    SuperAssistantMemory,
    SuperAssistantMemoryProfile,
    SuperAssistantMessage,
    SuperAssistantMulticaConfig,
    SuperAssistantRemoteAgent,
    SuperAssistantRemoteAgentTask,
    SuperAssistantSkill,
    SuperAssistantToolRun,
)
from app.super_assistant.schemas import RemoteAgentTaskResultIn

_TABLES = [
    User.__table__, RoleMenuPermission.__table__, ModelConfig.__table__,
    SuperAssistantConversation.__table__, SuperAssistantMessage.__table__,
    SuperAssistantToolRun.__table__, SuperAssistantDelegation.__table__,
    SuperAssistantSkill.__table__, SuperAssistantMcpServer.__table__,
    SuperAssistantMemory.__table__, SuperAssistantMemoryProfile.__table__,
    SuperAssistantMulticaConfig.__table__, SuperAssistantRemoteAgent.__table__,
]


def _seed(tmp_path, monkeypatch, name, *, prior_messages=()):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}.db", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine, tables=_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", TestingSession)
    monkeypatch.setattr(delegation, "SessionLocal", TestingSession)
    with TestingSession() as db:
        db.add(User(
            id=f"user-{name}", username=name, email=f"{name}@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(ModelConfig(
            id=f"model-{name}", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by=f"user-{name}",
        ))
        db.add(SuperAssistantConversation(
            id=f"conv-{name}", owner_id=f"user-{name}", title=name,
            model_config_id=f"model-{name}",
        ))
        for index, (role, content, steps) in enumerate(prior_messages):
            db.add(SuperAssistantMessage(
                id=f"msg-{name}-{index}", conversation_id=f"conv-{name}",
                role=role, content=content, status="complete", steps=steps,
            ))
        db.commit()
    return TestingSession, {
        "conversation_id": f"conv-{name}",
        "owner_id": f"user-{name}",
    }


def _add_agent(db, owner_id, *, key="remote.helper", enabled=True, endpoint="http://127.0.0.1:9103/turn"):
    row = SuperAssistantRemoteAgent(
        owner_id=owner_id, key=key, label="远程帮手",
        description="测试用远程助手", endpoint=endpoint,
        token_encrypted=None, enabled=enabled, timeout_seconds=30,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _fake_response(status_code=200, payload=None):
    return SimpleNamespace(status_code=status_code, json=lambda: payload)


# ------------------------------------------------------------------- CRUD


def test_create_validates_key_namespace_and_endpoint(db, admin_user):
    from app.super_assistant.schemas import RemoteAgentCreate

    with pytest.raises(remote_agent_service.RemoteAgentServiceError, match="remote"):
        remote_agent_service.create_agent(db, admin_user.id, RemoteAgentCreate(
            key="ontology_agent", label="x", endpoint="https://a.example.com/t",
        ))
    with pytest.raises(remote_agent_service.RemoteAgentServiceError):
        remote_agent_service.create_agent(db, admin_user.id, RemoteAgentCreate(
            key="remote.ok", label="x", endpoint="not-a-url",
        ))
    created = remote_agent_service.create_agent(db, admin_user.id, RemoteAgentCreate(
        key="remote.ok", label="帮手", description="会干活",
        endpoint="http://127.0.0.1:9101/turn", token="sec-token",
    ))
    assert created.token_set is True
    # 同 key 重复 → 409
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        remote_agent_service.create_agent(db, admin_user.id, RemoteAgentCreate(
            key="remote.ok", label="又一个", endpoint="http://127.0.0.1:9102/turn",
        ))
    assert exc.value.status_code == 409


def test_update_keeps_token_when_blank(db, admin_user):
    from app.super_assistant.schemas import RemoteAgentCreate, RemoteAgentUpdate

    created = remote_agent_service.create_agent(db, admin_user.id, RemoteAgentCreate(
        key="remote.k", label="k", endpoint="http://127.0.0.1:9101/turn", token="t1",
    ))
    updated = remote_agent_service.update_agent(db, admin_user.id, created.id, RemoteAgentUpdate(
        label="新名字", token="",
    ))
    assert updated.label == "新名字"
    assert updated.token_set is True  # 留空保留
    row = db.query(SuperAssistantRemoteAgent).one()
    from app.shared.encryption import decrypt

    assert decrypt(row.token_encrypted) == "t1"


# ---------------------------------------------------------------- 契约 adapter


def test_adapter_roundtrip_issues_and_resumes_remote_session(db, admin_user, monkeypatch):
    monkeypatch.setattr(remote_agent_service, "validate_mcp_url", lambda url: url)
    row = _add_agent(db, admin_user.id)
    adapter = remote_agent_service.RemoteAgentAdapter(row)
    ref = adapter.start(db, admin_user)
    assert contract.parse_ref("remote.helper", ref)["remote_session"] is None

    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs["json"]))
        if kwargs["json"]["session_ref"] is None:
            return _fake_response(payload={
                "status": "answered", "content": "第一回合答复",
                "session_ref": "rs-1",
            })
        return _fake_response(payload={
            "status": "answered", "content": "续聊答复",
            "session_ref": kwargs["json"]["session_ref"],
        })

    monkeypatch.setattr(remote_agent_service, "_request", fake_request)

    result = None
    for item in adapter.run_turn(db, admin_user, ref, "第一问"):
        result = item
    assert isinstance(result, TurnResult)
    assert result.status == STATUS_ANSWERED
    assert result.content == "第一回合答复"
    assert result.created_new_conversation is True
    resume_ref = result.conversation_ref
    assert contract.parse_ref("remote.helper", resume_ref)["remote_session"] == "rs-1"

    result2 = None
    for item in adapter.run_turn(db, admin_user, resume_ref, "第二问"):
        result2 = item
    assert result2.content == "续聊答复"
    assert result2.conversation_ref == resume_ref
    assert calls[1][2]["session_ref"] == "rs-1"  # 远端会话原样回传


def test_adapter_maps_failures(db, admin_user, monkeypatch):
    monkeypatch.setattr(remote_agent_service, "validate_mcp_url", lambda url: url)
    row = _add_agent(db, admin_user.id)
    adapter = remote_agent_service.RemoteAgentAdapter(row)
    ref = adapter.start(db, admin_user)

    import httpx

    monkeypatch.setattr(remote_agent_service, "_request",
                        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("boom")))
    results = list(adapter.run_turn(db, admin_user, ref, "q"))
    assert results[-1].status == contract.STATUS_FAILED
    assert "连接失败" in results[-1].content

    monkeypatch.setattr(remote_agent_service, "_request",
                        lambda *a, **k: _fake_response(status_code=503))
    assert list(adapter.run_turn(db, admin_user, ref, "q"))[-1].content == "远程助手返回 HTTP 503"

    def _bad_json():
        raise ValueError("Expecting value")

    monkeypatch.setattr(remote_agent_service, "_request",
                        lambda *a, **k: _fake_response(payload=None) if False else SimpleNamespace(status_code=200, json=_bad_json))
    failed = list(adapter.run_turn(db, admin_user, ref, "q"))[-1]
    assert failed.status == contract.STATUS_FAILED
    assert "无法解析" in failed.content


def test_adapter_rejects_private_endpoint_in_production(db, admin_user, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    row = _add_agent(db, admin_user.id, endpoint="http://127.0.0.1:9000/turn")
    adapter = remote_agent_service.RemoteAgentAdapter(row)
    with pytest.raises(contract.AssistantHubError, match="拒绝"):
        list(adapter.run_turn(db, admin_user, adapter.start(db, admin_user), "q"))


# ------------------------------------------------------------ 动态目录合并


def test_registry_merges_dynamic_agents_by_owner(db, admin_user, editor_user):
    _add_agent(db, admin_user.id)
    _add_agent(db, admin_user.id, key="remote.disabled", enabled=False)

    permitted = registry.permitted_assistants(db, admin_user)
    keys = [a.spec().key for a in permitted]
    assert "ontology_agent" in keys and "remote.helper" in keys
    assert "remote.disabled" not in keys  # 停用不进目录
    # 归属隔离：其他用户看不到
    other_keys = [a.spec().key for a in registry.permitted_assistants(db, editor_user)]
    assert "remote.helper" not in other_keys
    # 动态解析（执行路径）：带 db+user 才能取到
    assert registry.get_assistant("remote.helper", db=db, user=admin_user) is not None
    assert registry.get_assistant("remote.helper") is None
    assert registry.get_assistant("remote.helper", db=db, user=editor_user) is None


def test_provider_registration_is_idempotent():
    before = len(registry._dynamic_providers)
    registry.register_dynamic_provider(remote_agent_service.dynamic_assistants)
    assert len(registry._dynamic_providers) == before


# ------------------------------------------------- 引擎端到端（动态目录）


def test_delegation_executes_dynamic_agent_end_to_end(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "remote-e2e")
    with TestingSession() as db:
        _add_agent(db, ids["owner_id"])
        monkeypatch.setattr(remote_agent_service, "validate_mcp_url", lambda url: url)
        monkeypatch.setattr(
            remote_agent_service, "_request",
            lambda *a, **k: _fake_response(payload={
                "status": "answered", "content": "远程答复", "session_ref": "rs-9",
            }),
        )
        chunks = []
        generator = delegation.run_delegation_tool(
            db, owner_id=ids["owner_id"], conversation_id=ids["conversation_id"],
            arguments={"assistant": "remote.helper", "task": "帮忙查点事"},
            should_cancel=lambda: False,
        )
        try:
            while True:
                chunks.append(next(generator))
        except StopIteration as stop:
            output = stop.value
        payload = json.loads(output)
        assert payload["status"] == "answered"
        assert payload["content"] == "远程答复"
        db.expire_all()
        row = db.query(SuperAssistantDelegation).one()
        assert row.status == "answered"
        assert "rs-9" in row.conversation_ref


# ------------------------------------------------- 虚构委派兜底


def test_fabrication_reminder_only_when_claim_without_any_delegation():
    def message(role, content, steps=None):
        return SimpleNamespace(role=role, content=content, steps=steps or [])

    claimed = [message("user", "问"), message("assistant", "本体助手答复说没有数据")]
    assert "从未调用过" in delegation.suspected_fabrication_reminder(claimed)

    # 会话真实委派过：后续引用不是虚构
    delegated = claimed + [message(
        "assistant", "已委派完成",
        [{"toolName": "delegate_to_assistant", "status": "success"}],
    ), message("user", "继续"), message("assistant", "本体助手又说了一件事")]
    assert delegation.suspected_fabrication_reminder(delegated) == ""

    # 没有声称 → 不注入
    quiet = [message("user", "问"), message("assistant", "普通回答")]
    assert delegation.suspected_fabrication_reminder(quiet) == ""
    assert delegation.suspected_fabrication_reminder([]) == ""


def test_stream_injects_reminder_after_suspected_fabrication(tmp_path, monkeypatch):
    prior = [
        ("user", "问问本体助手有没有数据", []),
        ("assistant", "本体助手答复：没有任何数据。", []),
    ]
    TestingSession, ids = _seed(tmp_path, monkeypatch, "fabrication", prior_messages=prior)
    with TestingSession() as db:
        db.add(SuperAssistantMessage(
            id="msg-fab-user", conversation_id=ids["conversation_id"],
            role="user", content="那链接类型呢？", status="complete", steps=[],
        ))
        db.add(SuperAssistantMessage(
            id="msg-fab-assistant", conversation_id=ids["conversation_id"],
            role="assistant", content="", status="streaming", steps=[],
        ))
        db.commit()

    captured: dict = {}

    def _fake(_call_kwargs, messages, _tools, on_delta=None):
        captured["system"] = messages[0]["content"]
        return {"content": "这次老实回答。", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(runtime.provider, "chat_stream", _fake)
    "".join(runtime.stream_chat(
        conversation_id=ids["conversation_id"],
        owner_id=ids["owner_id"],
        assistant_message_id="msg-fab-assistant",
        requested_model_id=None,
    ))
    assert "从未调用过 delegate_to_assistant" in captured["system"]

    # 对照：真实委派过的会话不注入
    prior_ok = prior + [
        ("assistant", "已委派：本体助手答复没有数据。",
         [{"toolName": "delegate_to_assistant", "status": "success"}]),
    ]
    TestingSession2, ids2 = _seed(tmp_path, monkeypatch, "fabrication-ok", prior_messages=prior_ok)
    with TestingSession2() as db:
        db.add(SuperAssistantMessage(
            id="msg-ok-user", conversation_id=ids2["conversation_id"],
            role="user", content="继续", status="complete", steps=[],
        ))
        db.add(SuperAssistantMessage(
            id="msg-ok-assistant", conversation_id=ids2["conversation_id"],
            role="assistant", content="", status="streaming", steps=[],
        ))
        db.commit()
    captured2: dict = {}

    def _fake2(_call_kwargs, messages, _tools, on_delta=None):
        captured2["system"] = messages[0]["content"]
        return {"content": "好。", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(runtime.provider, "chat_stream", _fake2)
    "".join(runtime.stream_chat(
        conversation_id=ids2["conversation_id"],
        owner_id=ids2["owner_id"],
        assistant_message_id="msg-ok-assistant",
        requested_model_id=None,
    ))
    assert "从未调用过" not in captured2["system"]


# ------------------------------------------------------------- 回连模式


def _add_pull_agent(db, owner_id, *, key="remote.pull", timeout_seconds=30):
    row = SuperAssistantRemoteAgent(
        owner_id=owner_id, key=key, label="回连帮手",
        description="测试用回连远程助手", endpoint="",
        token_encrypted=None, enabled=True, timeout_seconds=timeout_seconds,
        mode="pull",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class _FakeAgentSide:
    """模拟回连远端：独立会话轮询认领任务并回传结果（记录收到的载荷）。"""

    def __init__(self, engine, agent_id, *, result="done", session_ref="s-1"):
        self._sessionmaker = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        self._agent_id = agent_id
        self._result = result
        self._session_ref = session_ref
        self.received: list[dict] = []

    def __call__(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with self._sessionmaker() as adb:
                task = remote_agent_service.claim_next_task(adb, self._agent_id)
                if task is None:
                    time.sleep(0.02)
                    continue
                self.received.append({"message": task.message, "session_ref": task.session_ref})
                remote_agent_service.submit_task_result(adb, self._agent_id, task.id, RemoteAgentTaskResultIn(
                    status="answered" if self._result == "done" else "failed",
                    content="pong" if self._result == "done" else "bad",
                    session_ref=self._session_ref,
                ))
                return
            time.sleep(0.02)


def test_pull_roundtrip_answers_and_resumes_session(db, admin_user, monkeypatch):
    monkeypatch.setattr(remote_agent_service, "_TASK_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(remote_agent_service, "_TASK_RESULT_GRACE_SECONDS", 1.0)
    row = _add_pull_agent(db, admin_user.id)
    side = _FakeAgentSide(db.get_bind(), row.id)
    worker = threading.Thread(target=side)
    worker.start()

    adapter = remote_agent_service.RemoteAgentAdapter(row)
    ref = adapter.start(db, None)
    first = [item for item in adapter.run_turn(db, None, ref, "ping") if isinstance(item, TurnResult)]
    worker.join(timeout=8)

    assert first[-1].status == STATUS_ANSWERED
    assert "pong" in first[-1].content
    assert side.received[0]["message"] == "ping"
    assert side.received[0]["session_ref"] is None
    # 回合开始即刷新活动信号（直连/回连共用口径）
    db.refresh(row)
    assert row.last_turn_at is not None
    # 首回合远端签发 session_ref → created_new_conversation 置位（续聊接线同直连）
    assert first[-1].created_new_conversation is True
    from app.assistant_hub.contract import parse_ref
    assert parse_ref(row.key, first[-1].conversation_ref)["remote_session"] == "s-1"

    # 第二轮：上一回合签发的 session_ref 原样带给远端
    side2 = _FakeAgentSide(db.get_bind(), row.id)
    worker2 = threading.Thread(target=side2)
    worker2.start()
    second = [item for item in adapter.run_turn(db, None, first[-1].conversation_ref, "again")
              if isinstance(item, TurnResult)]
    worker2.join(timeout=8)
    assert second[-1].status == STATUS_ANSWERED
    assert side2.received[0]["session_ref"] == "s-1"
    assert second[-1].created_new_conversation is False


def test_pull_timeout_when_agent_offline(db, admin_user, monkeypatch):
    monkeypatch.setattr(remote_agent_service, "_TASK_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(remote_agent_service, "_TASK_RESULT_GRACE_SECONDS", 0.0)
    row = _add_pull_agent(db, admin_user.id, timeout_seconds=1)

    adapter = remote_agent_service.RemoteAgentAdapter(row)
    ref = adapter.start(db, None)
    results = [item for item in adapter.run_turn(db, None, ref, "ping") if isinstance(item, TurnResult)]

    assert results[-1].status == STATUS_FAILED
    assert "超时" in results[-1].content
    task = db.query(SuperAssistantRemoteAgentTask).one()
    assert task.status == "expired"


def test_key_length_capped_to_column_width(db, admin_user):
    # 正则允许的总长上限 49（列宽 50）：50 字符 key 在 PG 上会触发截断 500
    from app.super_assistant.schemas import RemoteAgentCreate

    long_key = "remote." + "a" * 43  # 总长 50
    with pytest.raises(remote_agent_service.RemoteAgentServiceError, match="总长"):
        remote_agent_service.create_agent(db, admin_user.id, RemoteAgentCreate(
            key=long_key, label="x", endpoint="http://127.0.0.1:9101/turn",
        ))
    ok = remote_agent_service.create_agent(db, admin_user.id, RemoteAgentCreate(
        key="remote." + "a" * 42, label="x", endpoint="http://127.0.0.1:9101/turn",
    ))
    assert ok.key == "remote." + "a" * 42


def test_corrupt_token_ciphertext_degrades_instead_of_poisoning(db, admin_user):
    row = _add_agent(db, admin_user.id, key="remote.bad-secret", endpoint="http://127.0.0.1:9103/turn")
    row.token_encrypted = "gibberish-not-fernet"
    db.commit()

    adapter = remote_agent_service.RemoteAgentAdapter(row)
    assert adapter._token == ""  # 解密失败退化为无凭据，不抛异常
    # 动态目录（委派工具 schema 构建）不受坏行影响
    listing = remote_agent_service.dynamic_assistants(db, SimpleNamespace(id=admin_user.id))
    assert any(a.spec().key == "remote.bad-secret" for a in listing)


def test_direct_mode_caps_oversized_session_ref(db, admin_user, monkeypatch):
    monkeypatch.setattr(remote_agent_service, "validate_mcp_url", lambda url: url)
    row = _add_agent(db, admin_user.id, key="remote.long-ref", endpoint="http://127.0.0.1:9103/turn")
    monkeypatch.setattr(
        remote_agent_service, "_request",
        lambda *a, **k: _fake_response(200, {
            "status": "answered", "content": "ok",
            "session_ref": "s" * 900, "note": "n" * 3000,
        }),
    )
    adapter = remote_agent_service.RemoteAgentAdapter(row)
    ref = adapter.start(db, None)
    results = [item for item in adapter.run_turn(db, None, ref, "hi") if isinstance(item, TurnResult)]
    assert results[-1].status == STATUS_ANSWERED
    from app.assistant_hub.contract import parse_ref
    stored = parse_ref(row.key, results[-1].conversation_ref)["remote_session"]
    assert len(stored) == 255  # 截断到列宽，写入委派表不再溢出
    assert len(results[-1].note) == 2000


def test_pull_mode_honors_cancel_event(db, admin_user, monkeypatch):
    monkeypatch.setattr(remote_agent_service, "_TASK_POLL_INTERVAL", 0.02)
    row = _add_pull_agent(db, admin_user.id, timeout_seconds=30)
    cancel = threading.Event()
    cancel.set()
    adapter = remote_agent_service.RemoteAgentAdapter(row)
    ref = adapter.start(db, None)
    results = [item for item in adapter.run_turn(
        db, None, ref, "hi", cancel_event=cancel,
    ) if isinstance(item, TurnResult)]
    assert results[-1].status == STATUS_FAILED
    assert "取消" in results[-1].content
    task = db.query(SuperAssistantRemoteAgentTask).one()
    assert task.status == "expired"


def test_test_agent_fails_fast_for_offline_pull_agent(db, admin_user, monkeypatch):
    monkeypatch.setattr(remote_agent_service, "_TASK_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(remote_agent_service, "_TASK_RESULT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(remote_agent_service, "_TEST_TURN_TIMEOUT_SECONDS", 1)
    row = _add_pull_agent(db, admin_user.id, key="remote.offline", timeout_seconds=600)

    started = time.monotonic()
    result = remote_agent_service.test_agent(db, admin_user.id, row.id)
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert elapsed < 10  # 600 秒超时的离线助手在秒级失败，不再挂满请求


def test_claim_next_task_is_exclusive(db, admin_user):
    row = _add_pull_agent(db, admin_user.id)
    remote_agent_service.enqueue_task(db, row.id, "only-one", None, 60)

    first = remote_agent_service.claim_next_task(db, row.id)
    second = remote_agent_service.claim_next_task(db, row.id)
    assert first is not None and first.message == "only-one"
    assert second is None  # 已认领不再派发


def test_task_gc_prunes_only_past_retention(db, admin_user, monkeypatch):
    from datetime import timedelta

    from app.super_assistant import remote_agent_task_gc
    from app.super_assistant.remote_agent_service import _utcnow

    row = _add_pull_agent(db, admin_user.id)
    stale = _utcnow() - timedelta(days=8)
    # done/expired/孤儿 pending（进程崩溃遗留、过期后对认领不可见）三类同口径回收
    for status in ("done", "expired", "pending"):
        db.add(SuperAssistantRemoteAgentTask(
            agent_id=row.id, status=status, message=f"stale-{status}",
            created_at=stale, expires_at=stale,
        ))
    db.add(SuperAssistantRemoteAgentTask(
        agent_id=row.id, status="done", message="fresh",
        created_at=_utcnow(), expires_at=_utcnow() + timedelta(seconds=60),
    ))
    db.commit()

    from app.super_assistant.models import SuperAssistantRemoteAgentInvite
    old_invite_expiry = _utcnow() - timedelta(days=40)
    db.add(SuperAssistantRemoteAgentInvite(
        owner_id=admin_user.id, token_hash="h-old", token_encrypted="x",
        expires_at=old_invite_expiry, consumed_at=old_invite_expiry,
    ))
    db.add(SuperAssistantRemoteAgentInvite(
        owner_id=admin_user.id, token_hash="h-kept", token_encrypted="y",
        expires_at=_utcnow() + timedelta(hours=23),
    ))
    db.commit()

    testing_session = sessionmaker(bind=db.get_bind(), autocommit=False, autoflush=False)
    monkeypatch.setattr(remote_agent_task_gc, "SessionLocal", testing_session)
    removed = remote_agent_task_gc.prune_once()

    assert removed == 3
    remaining = db.query(SuperAssistantRemoteAgentTask).all()
    assert len(remaining) == 1 and remaining[0].message == "fresh"
    invites = db.query(SuperAssistantRemoteAgentInvite).all()
    assert [i.token_hash for i in invites] == ["h-kept"]  # 过期 30 天外的邀请连同令牌清除
