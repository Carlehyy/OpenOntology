"""超级助手委派执行器集成测试。

沿用 test_runtime_integration.py 的隔离 sqlite 手法：runtime 与 delegation
的 SessionLocal 同时指到测试会话；assistant_hub 注册表用假助手替换，
provider 一律伪造，不触网。核心验收：同一超级会话内二次委派默认复用
同一条子会话（引用由委派表独占，不经 LLM 上下文）。
"""
from __future__ import annotations

import json
import threading
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.assistant_hub.contract import (
    STATUS_ANSWERED,
    AssistantSpec,
    TurnEvent,
    TurnResult,
    build_ref,
)
from app.auth.models import RoleMenuPermission, User
from app.model_configs.models import ModelConfig
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import delegation, runtime
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantDelegation,
    SuperAssistantMcpServer,
    SuperAssistantMemory,
    SuperAssistantMemoryProfile,
    SuperAssistantMessage,
    SuperAssistantMulticaConfig,
    SuperAssistantSkill,
    SuperAssistantToolRun,
)

_TABLES = [
    User.__table__, RoleMenuPermission.__table__, ModelConfig.__table__,
    SuperAssistantConversation.__table__, SuperAssistantMessage.__table__,
    SuperAssistantToolRun.__table__, SuperAssistantDelegation.__table__,
    SuperAssistantSkill.__table__, SuperAssistantMcpServer.__table__,
    SuperAssistantMemory.__table__, SuperAssistantMemoryProfile.__table__,
    SuperAssistantMulticaConfig.__table__,
]


class _FakeAssistant:
    """协议桩：记录 start/run_turn 调用，可注入延迟与阻塞门。"""

    def __init__(self, *, key="ontology_agent", delay=0.0, start_error=None,
                 gate: threading.Event | None = None):
        self.key = key
        self.delay = delay
        self.start_error = start_error
        self.gate = gate
        self.start_calls = 0
        self.run_refs: list[str] = []

    def spec(self) -> AssistantSpec:
        return AssistantSpec(
            key=self.key, label="本体助手", description="测试桩",
            menu_keys=("agent",),
        )

    def start(self, db, user, *, context=None):
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        return build_ref(self.key, {"line": self.start_calls})

    def run_turn(self, db, user, conversation_ref, message, *, cancel_event=None):
        self.run_refs.append(conversation_ref)
        if self.gate is not None:
            self.gate.wait(timeout=10)
        if self.delay:
            time.sleep(self.delay)
        yield TurnEvent(kind="meta", data={})
        yield TurnResult(
            status=STATUS_ANSWERED,
            content=f"子助手结论（第 {len(self.run_refs)} 轮）",
            conversation_ref=conversation_ref,
        )


def _seed(tmp_path, monkeypatch, name):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}.db", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_TABLES)
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
        db.commit()
    return TestingSession, {"conversation_id": f"conv-{name}", "owner_id": f"user-{name}"}


def _fake_registry(monkeypatch, fake: _FakeAssistant):
    from app.assistant_hub import registry

    monkeypatch.setattr(registry, "get_assistant",
                        lambda key: fake if key == fake.key else None)
    monkeypatch.setattr(registry, "permitted_assistants",
                        lambda db, user: [fake])


def _fast_polls(monkeypatch):
    monkeypatch.setattr(delegation, "_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(delegation, "_HEARTBEAT_POLLS", 2)


def _drive(generator):
    """推进到 return，返回 (产出片段, 工具结果 JSON)。"""
    chunks: list[str] = []
    while True:
        try:
            chunk = next(generator)
        except StopIteration as stop:
            return "".join(chunks), stop.value
        if chunk:
            chunks.append(chunk)


def _run_once(db, ids, *, arguments, should_cancel=lambda: False):
    return _drive(delegation.run_delegation_tool(
        db,
        owner_id=ids["owner_id"],
        conversation_id=ids["conversation_id"],
        arguments=arguments,
        should_cancel=should_cancel,
    ))


# --------------------------------------------------------------- 基本回路


def test_delegation_answers_and_persists_row(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "basic")
    fake = _FakeAssistant()
    _fake_registry(monkeypatch, fake)

    with TestingSession() as db:
        chunks, output = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "查一下订单量",
        })
        payload = json.loads(output)
        assert payload["status"] == "answered"
        assert payload["assistant"] == "本体助手"
        assert payload["content"] == "子助手结论（第 1 轮）"
        assert payload["resumed"] is False
        assert payload["createdNewConversation"] is False  # 引用由 start 预建
        row = db.query(SuperAssistantDelegation).one()
        assert row.status == "answered"
        assert row.conversation_ref == fake.run_refs[0]
        assert "子助手结论" in row.summary


def test_second_delegation_resumes_same_conversation(tmp_path, monkeypatch):
    """核心验收：同会话二次委派默认续用同一条子会话（start 不再被调）。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "resume")
    fake = _FakeAssistant()
    _fake_registry(monkeypatch, fake)

    with TestingSession() as db:
        _, first = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "第一问",
        })
        _, second = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "再问一句",  # session 缺省 = resume
        })
        assert json.loads(first)["resumed"] is False
        assert json.loads(second)["resumed"] is True
        assert fake.start_calls == 1  # 没有另起子会话
        assert fake.run_refs[0] == fake.run_refs[1]  # 同一条子会话
        rows = db.query(SuperAssistantDelegation).order_by(
            SuperAssistantDelegation.created_at).all()
        assert len(rows) == 2  # 追加式历史
        assert rows[0].conversation_ref == rows[1].conversation_ref


def test_session_new_rotates_to_fresh_conversation(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "rotate")
    fake = _FakeAssistant()
    _fake_registry(monkeypatch, fake)

    with TestingSession() as db:
        _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "第一条线",
        })
        _, second = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "另起一条线", "session": "new",
        })
        payload = json.loads(second)
        assert payload["resumed"] is False
        assert fake.start_calls == 2
        assert fake.run_refs[0] != fake.run_refs[1]
        rows = db.query(SuperAssistantDelegation).all()
        assert len(rows) == 2
        assert rows[0].conversation_ref != rows[1].conversation_ref


# --------------------------------------------------------------- 安全边界


def test_unknown_assistant_key_rejected_without_row(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "unknown")
    _fake_registry(monkeypatch, _FakeAssistant())

    with TestingSession() as db:
        _, output = _run_once(db, ids, arguments={
            "assistant": "no_such", "task": "x",
        })
        assert json.loads(output)["status"] == "failed"
        assert db.query(SuperAssistantDelegation).count() == 0


def test_permission_recheck_at_execution_time(tmp_path, monkeypatch):
    """注入时过滤只是 UX：执行时对自由字符串 assistant 重验菜单权限。"""
    from app.assistant_hub import registry

    TestingSession, ids = _seed(tmp_path, monkeypatch, "perm")
    fake = _FakeAssistant()
    monkeypatch.setattr(registry, "get_assistant", lambda key: fake)
    monkeypatch.setattr(registry, "permitted_assistants", lambda db, user: [])

    with TestingSession() as db:
        _, output = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "越权尝试",
        })
        payload = json.loads(output)
        assert payload["status"] == "failed"
        assert "无权" in payload["error"]
        assert fake.start_calls == 0
        assert db.query(SuperAssistantDelegation).count() == 0


def test_empty_task_rejected(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "empty")
    _fake_registry(monkeypatch, _FakeAssistant())

    with TestingSession() as db:
        _, output = _run_once(db, ids, arguments={"assistant": "ontology_agent", "task": "  "})
        assert json.loads(output)["status"] == "failed"


def test_hub_error_maps_to_failed_result(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "huberr")
    from app.assistant_hub.contract import AssistantHubError

    fake = _FakeAssistant(start_error=AssistantHubError("缺少 ontology_id"))
    _fake_registry(monkeypatch, fake)
    _fast_polls(monkeypatch)

    with TestingSession() as db:
        _, output = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "缺参数",
        })
        payload = json.loads(output)
        assert payload["status"] == "failed"
        assert "ontology_id" in payload["error"]
        row = db.query(SuperAssistantDelegation).one()
        assert row.status == "failed"


# --------------------------------------------------------------- 执行模型


def test_bulkhead_rejects_concurrent_overload(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "bulkhead")
    gate = threading.Event()
    fake = _FakeAssistant(gate=gate)
    _fake_registry(monkeypatch, fake)
    _fast_polls(monkeypatch)
    monkeypatch.setattr(delegation, "_semaphore", threading.Semaphore(1))

    with TestingSession() as db:
        first = delegation.run_delegation_tool(
            db, owner_id=ids["owner_id"], conversation_id=ids["conversation_id"],
            arguments={"assistant": "ontology_agent", "task": "慢任务"},
            should_cancel=lambda: False,
        )
        # 推到第一条心跳：证明第一个委派占住舱壁额度并保活连接
        first_chunk = next(chunk for chunk in _iter_chunks(first) if chunk)
        assert first_chunk == ": ping\n\n"

        _, busy = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "第二个并发",
        })
        payload = json.loads(busy)
        assert payload["status"] == "failed"
        assert "通道忙" in payload["error"]

        gate.set()  # 放行第一个委派并驱动到终态（释放额度）
        chunks, done = _drive(first)
        assert json.loads(done)["status"] == "answered"

        # 额度已回收：新委派可正常执行
        _, again = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "再来一次",
        })
        assert json.loads(again)["status"] == "answered"


def _iter_chunks(generator):
    while True:
        try:
            yield next(generator)
        except StopIteration:
            return


def _wait_row(db, predicate, timeout=5.0):
    """轮询委派行直到谓词成立（工作线程异步收尾）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db.expire_all()
        if predicate(db.query(SuperAssistantDelegation).order_by(
                SuperAssistantDelegation.created_at.desc()).first()):
            return True
        time.sleep(0.05)
    return False


def test_client_disconnect_finalizes_row_and_keeps_channel_usable(tmp_path, monkeypatch):
    """SSE 断开（GeneratorExit）：额度即释、子回合由工作线程收尾、通道不毒化。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "disconnect")
    gate = threading.Event()
    fake = _FakeAssistant(gate=gate)
    _fake_registry(monkeypatch, fake)
    _fast_polls(monkeypatch)
    monkeypatch.setattr(delegation, "_semaphore", threading.Semaphore(1))

    with TestingSession() as db:
        generator = delegation.run_delegation_tool(
            db, owner_id=ids["owner_id"], conversation_id=ids["conversation_id"],
            arguments={"assistant": "ontology_agent", "task": "慢任务"},
            should_cancel=lambda: False,
        )
        next(chunk for chunk in _iter_chunks(generator) if chunk)  # 第一条心跳
        generator.close()  # 模拟客户端断开注入 GeneratorExit
        # 额度已释放：新委派可立即获得额度（不会通道假死）
        gate.set()
        _, output = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "断开后的新委派",
        })
        assert json.loads(output)["status"] == "answered"
        # 断开那次的委派行由工作线程收尾（only-if-running），并回填 ref
        assert _wait_row(
            db,
            lambda r: r is not None and r.conversation_ref is not None,
        )
        rows = db.query(SuperAssistantDelegation).order_by(
            SuperAssistantDelegation.created_at).all()
        assert rows[0].status == "answered"  # 断开行最终也被收尾


def test_stale_running_row_is_reclaimed_before_insert(tmp_path, monkeypatch):
    """进程崩溃残留的 running 行在下次委派前被回收，部分唯一索引不毒化通道。"""
    from datetime import datetime, timedelta, timezone as tz

    TestingSession, ids = _seed(tmp_path, monkeypatch, "stale")
    _fake_registry(monkeypatch, _FakeAssistant())
    _fast_polls(monkeypatch)

    with TestingSession() as db:
        stale = SuperAssistantDelegation(
            owner_id=ids["owner_id"],
            super_conversation_id=ids["conversation_id"],
            assistant_key="ontology_agent",
            conversation_ref=None,
            status="running",
            last_turn_at=datetime.now(tz.utc) - timedelta(hours=1),
        )
        db.add(stale)
        db.commit()

        _, output = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "崩溃后的第一次委派",
        })
        assert json.loads(output)["status"] == "answered"
        db.expire_all()
        rows = db.query(SuperAssistantDelegation).order_by(
            SuperAssistantDelegation.created_at).all()
        assert rows[0].status == "interrupted"
        assert rows[1].status == "answered"


def test_timeout_stops_waiting_and_row_marks_timeout(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "timeout")
    fake = _FakeAssistant(delay=1.5)  # 不响应取消：模拟无协作取消的子助手
    _fake_registry(monkeypatch, fake)
    _fast_polls(monkeypatch)
    monkeypatch.setattr(settings, "super_assistant_delegation_timeout_seconds", 1)

    with TestingSession() as db:
        _, output = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "拖满超时",
        })
        payload = json.loads(output)
        assert payload["status"] == "failed"
        assert "未完成" in payload["error"]
        row = db.query(SuperAssistantDelegation).one()
        assert row.status == "timeout"
        # 后台线程跑完后的 done 转移不得覆盖 timeout 终态（only-if-running），
        # 但 conversation_ref 必须被工作线程按 only-if-NULL 回填——否则下次
        # resume 会静默退化为永远新建子会话
        assert _wait_row(db, lambda r: r.conversation_ref is not None)
        db.expire_all()
        assert db.query(SuperAssistantDelegation).one().status == "timeout"

        # 超时后 resume：续用同一条子会话（start 不再被调、ref 不变）
        fake.delay = 0  # 第二回合不再拖时间，验证 resume 语义本身
        _, second = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "继续刚才的问题",
        })
        second_payload = json.loads(second)
        assert second_payload["resumed"] is True
        assert fake.start_calls == 1
        assert fake.run_refs[0] == fake.run_refs[1]


def test_cancel_stops_waiting_and_marks_cancelled(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "cancel")
    cancel_after = {"calls": 0}
    fake = _FakeAssistant(delay=1.5)
    _fake_registry(monkeypatch, fake)
    _fast_polls(monkeypatch)

    def should_cancel():
        cancel_after["calls"] += 1
        return cancel_after["calls"] > 2  # 前两次轮询未取消，之后置位

    with TestingSession() as db:
        _, output = _run_once(db, ids, arguments={
            "assistant": "ontology_agent", "task": "会被取消",
        }, should_cancel=should_cancel)
        payload = json.loads(output)
        assert payload["status"] == "cancelled"
        row = db.query(SuperAssistantDelegation).one()
        assert row.status == "cancelled"


def test_heartbeat_is_sse_comment_not_event(tmp_path, monkeypatch):
    """心跳必须是 SSE 注释（": ping"），不得触碰固定事件枚举契约。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "beat")
    gate = threading.Event()
    _fake_registry(monkeypatch, _FakeAssistant(gate=gate))
    _fast_polls(monkeypatch)

    with TestingSession() as db:
        generator = delegation.run_delegation_tool(
            db, owner_id=ids["owner_id"], conversation_id=ids["conversation_id"],
            arguments={"assistant": "ontology_agent", "task": "慢"},
            should_cancel=lambda: False,
        )
        chunk = next(chunk for chunk in _iter_chunks(generator) if chunk)
        gate.set()
        list(_iter_chunks(generator))  # 排空到终态
        assert chunk.startswith(":")
        assert "event:" not in chunk


# ------------------------------------------------------- 目录注入与全环


def test_delegation_tools_follow_user_menu_visibility(tmp_path, monkeypatch):
    TestingSession, ids = _seed(tmp_path, monkeypatch, "catalog")

    with TestingSession() as db:
        # editor 无角色授权记录 → 默认菜单含 agent/explore：可委派
        schemas = delegation.delegation_tools(db, ids["owner_id"])
        assert len(schemas) == 1
        assert schemas[0]["name"] == "delegate_to_assistant"
        enum = schemas[0]["parameters"]["properties"]["assistant"]["enum"]
        assert "ontology_agent" in enum and "exploration" in enum

        # custom 角色默认只有 overview：一个都不可委派
        db.add(User(
            id="user-catalog-c", username="catalog-c", email="c2@example.com",
            password_hash="unused", role="custom",
        ))
        db.commit()
        assert delegation.delegation_tools(db, "user-catalog-c") == []


def test_full_stream_delegates_and_integrates_result(tmp_path, monkeypatch):
    """全环：目录注入 → LLM 选委派工具 → 执行器跑子回合 → 结果回灌总结。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "fullloop")
    with TestingSession() as db:
        db.add(SuperAssistantMessage(
            id="msg-user-fullloop", conversation_id=ids["conversation_id"],
            role="user", content="帮我问问本体助手订单量", status="complete",
        ))
        db.add(SuperAssistantMessage(
            id="msg-assistant-fullloop", conversation_id=ids["conversation_id"],
            role="assistant", content="", status="streaming",
        ))
        db.commit()

    fake = _FakeAssistant()
    _fake_registry(monkeypatch, fake)
    captured: dict = {}
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "c1", "name": "delegate_to_assistant",
                "arguments": {"assistant": "ontology_agent", "task": "查订单量"},
            }],
            "usage": {},
        },
        {"content": "已转述子助手结论。", "tool_calls": [], "usage": {}},
    ])

    def _fake_stream(_call_kwargs, messages, tools, on_delta=None):
        captured.setdefault("tools", [t["name"] for t in tools])
        captured.setdefault("system", messages[0]["content"])
        return next(responses)

    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_stream)

    events = "".join(runtime.stream_chat(
        conversation_id=ids["conversation_id"],
        owner_id=ids["owner_id"],
        assistant_message_id="msg-assistant-fullloop",
        requested_model_id=None,
    ))

    # 目录注入与系统提示（分身规则）
    assert "delegate_to_assistant" in captured["tools"]
    assert "分身" in captured["system"]
    # SSE 全环：工具卡 + 成功结果 + 完整收尾
    assert "event: tool_start" in events
    assert "delegate_to_assistant" in events
    assert '\\"status\\": \\"answered\\"' in events
    assert "event: message_end" in events
    # 委派行与工具行落库
    with TestingSession() as db:
        row = db.query(SuperAssistantDelegation).one()
        assert row.status == "answered"
        assert fake.start_calls == 1
        run = db.query(SuperAssistantToolRun).one()
        assert run.tool_name == "delegate_to_assistant"
        assert run.status == "success"
        saved = db.get(SuperAssistantMessage, "msg-assistant-fullloop")
        assert saved.status == "complete"
        assert saved.content == "已转述子助手结论。"
