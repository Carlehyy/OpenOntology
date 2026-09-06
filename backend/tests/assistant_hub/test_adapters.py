"""assistant_hub adapter 单测：权限、归属、引用解析、取消桥接与结果归一。

run_agent_turn / run_exploration_turn 是 adapter 的执行缝隙，测试用假
生成器替换；菜单/归属/本体访问路径走真实域内函数。custom 角色默认菜单
只有 overview，是天然的"无 agent/explore 菜单"用户。
"""
from __future__ import annotations

import threading
import uuid

import pytest

from app.assistant_hub import contract
from app.assistant_hub.adapters import exploration as exploration_adapter
from app.assistant_hub.adapters import ontology_agent as ontology_adapter
from app.assistant_hub.adapters.exploration import ExplorationAdapter
from app.assistant_hub.adapters.ontology_agent import OntologyAgentAdapter
from app.assistant_hub.contract import STATUS_ANSWERED, build_ref
from app.exploration.models import ExplorationSession
from app.ontologies.agent_runtime.chat_cancel import chat_cancel_registry
from app.ontologies.agent_runtime.models import AgentConversation


# ---------------------------------------------------------------- helpers


def _custom_user(db):
    import app.services.auth_service as auth_service

    from app.auth.models import User

    user = User(
        id=str(uuid.uuid4()), username=f"custom-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@test.com",
        password_hash=auth_service.hash_password("x"), role="custom",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _drain(generator):
    """消费到终态，返回 (events, result)。"""
    events = []
    result = None
    for item in generator:
        if isinstance(item, contract.TurnResult):
            result = item
        else:
            events.append(item)
    assert result is not None, "run_turn 必须以 TurnResult 收尾"
    return events, result


# ------------------------------------------------------- ontology agent


@pytest.fixture()
def no_access_guard(monkeypatch):
    """单测默认放行本体访问（真实访问检查单独测）。"""
    monkeypatch.setattr(
        ontology_adapter, "require_ontology_access",
        lambda db, ontology_id, user, *, write: None,
    )


def test_ontology_agent_denies_user_without_agent_menu(db, monkeypatch):
    user = _custom_user(db)

    def _must_not_run(*args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("不应触达子助手")

    monkeypatch.setattr(ontology_adapter, "run_agent_turn", _must_not_run)
    with pytest.raises(contract.PermissionDeniedError):
        OntologyAgentAdapter().start(db, user, context={"ontology_id": "ont-1"})


def test_ontology_agent_requires_ontology_id_guidance(db, admin_user, no_access_guard):
    with pytest.raises(contract.AssistantHubError, match="ontology_id"):
        OntologyAgentAdapter().start(db, admin_user, context={})


def test_ontology_agent_falls_back_to_recent_ontology(db, admin_user, no_access_guard, monkeypatch):
    db.add(AgentConversation(
        id="conv-recent", ontology_id="ont-recent", ontology_release_id="rel-1",
        user_id=admin_user.id, title="旧会话",
    ))
    db.commit()

    captured = {}

    def fake_run_agent_turn(db_, ontology_id, user, question, **kwargs):
        captured.update(kwargs, ontology_id=ontology_id)
        yield {"type": "meta", "conversationId": "conv-new", "releaseId": "rel-1"}
        yield {"type": "answer", "content": "查证结论", "usage": {"inputTokens": 3}}

    monkeypatch.setattr(ontology_adapter, "run_agent_turn", fake_run_agent_turn)
    ref = OntologyAgentAdapter().start(db, admin_user, context={})
    assert contract.parse_ref("ontology_agent", ref)["ontology_id"] == "ont-recent"

    _, result = _drain(OntologyAgentAdapter().run_turn(db, admin_user, ref, "问一下订单量"))
    assert result.status == STATUS_ANSWERED
    assert result.content == "查证结论"
    assert result.created_new_conversation is True
    assert contract.parse_ref("ontology_agent", result.conversation_ref) == {
        "ontology_id": "ont-recent", "conversation_id": "conv-new",
    }
    # 新会话不锚定 release，交给编排器取当前发布版
    assert captured["release_id"] is None
    assert captured["conversation_id"] is None


def test_ontology_agent_resume_pins_conversation_release(db, admin_user, no_access_guard, monkeypatch):
    db.add(AgentConversation(
        id="conv-old", ontology_id="ont-1", ontology_release_id="rel-old",
        user_id=admin_user.id, title="旧会话",
    ))
    db.commit()
    ref = build_ref("ontology_agent", {
        "ontology_id": "ont-1", "conversation_id": "conv-old",
    })

    captured = {}

    def fake_run_agent_turn(db_, ontology_id, user, question, **kwargs):
        captured.update(kwargs)
        yield {"type": "meta", "conversationId": "conv-old"}
        yield {"type": "answer", "content": "续聊结论"}

    monkeypatch.setattr(ontology_adapter, "run_agent_turn", fake_run_agent_turn)
    _, result = _drain(OntologyAgentAdapter().run_turn(db, admin_user, ref, "继续"))
    assert result.status == STATUS_ANSWERED
    assert result.created_new_conversation is False
    # 续聊锚定子会话所在 release，防当前发布版漂移后静默新建
    assert captured["conversation_id"] == "conv-old"
    assert captured["release_id"] == "rel-old"


def test_ontology_agent_resume_missing_conversation_fails_not_silent_new(
    db, admin_user, no_access_guard, monkeypatch,
):
    ref = build_ref("ontology_agent", {
        "ontology_id": "ont-1", "conversation_id": "conv-gone",
    })
    with pytest.raises(contract.AssistantHubError, match="session=new"):
        list(OntologyAgentAdapter().run_turn(db, admin_user, ref, "继续"))


def test_ontology_agent_resume_foreign_conversation_denied(
    db, admin_user, no_access_guard, monkeypatch,
):
    other = _custom_user(db)
    db.add(AgentConversation(
        id="conv-foreign", ontology_id="ont-1", ontology_release_id=None,
        user_id=other.id, title="他人会话",
    ))
    db.commit()
    ref = build_ref("ontology_agent", {
        "ontology_id": "ont-1", "conversation_id": "conv-foreign",
    })
    with pytest.raises(contract.AssistantHubError, match="session=new"):
        list(OntologyAgentAdapter().run_turn(db, admin_user, ref, "偷看"))


def test_ontology_agent_real_ontology_access_check(db, admin_user):
    from app.ontologies.projects.models import OntologyProject

    db.add(OntologyProject(
        id="ont-real", name="真实本体", domain="供应链", created_by=admin_user.id,
    ))
    db.commit()
    # 真实 require_ontology_access：读路径要求本体存在；不存在的本体 → 拒绝
    with pytest.raises(contract.PermissionDeniedError):
        OntologyAgentAdapter().start(db, admin_user, context={"ontology_id": "ont-missing"})
    ref = OntologyAgentAdapter().start(db, admin_user, context={"ontology_id": "ont-real"})
    assert contract.parse_ref("ontology_agent", ref)["ontology_id"] == "ont-real"


def test_ontology_agent_bridges_cancel_via_registry(db, admin_user, no_access_guard, monkeypatch):
    ref = build_ref("ontology_agent", {"ontology_id": "ont-1"})

    def cooperative_run(db_, ontology_id, user, question, **kwargs):
        run_id = kwargs["run_id"]
        chat_cancel_registry.register(run_id)
        try:
            yield {"type": "meta", "conversationId": "conv-c"}
            for _ in range(20):
                if chat_cancel_registry.is_cancelled(run_id):
                    yield {"type": "cancelled"}
                    return
                yield {"type": "step", "tool": "search_objects", "summary": "."}
        finally:
            chat_cancel_registry.unregister(run_id)

    monkeypatch.setattr(ontology_adapter, "run_agent_turn", cooperative_run)

    cancel_event = threading.Event()
    generator = OntologyAgentAdapter().run_turn(
        db, admin_user, ref, "可取消任务", cancel_event=cancel_event,
    )
    events = []
    result = None
    for item in generator:
        if isinstance(item, contract.TurnResult):
            result = item
            break
        events.append(item)
        # 第一个事件后请求取消：adapter 收到下一事件时应已桥接 request_cancel
        cancel_event.set()
    assert result.status == contract.STATUS_CANCELLED
    assert len(events) <= 4  # 协作取消在少数几步内生效


# ------------------------------------------------------------ exploration


def test_exploration_denies_user_without_explore_menu(db, monkeypatch):
    user = _custom_user(db)

    def _must_not_run(*args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("不应触达子助手")

    monkeypatch.setattr(exploration_adapter, "run_exploration_turn", _must_not_run)
    with pytest.raises(contract.PermissionDeniedError):
        ExplorationAdapter().start(db, user, context={"title_hint": "t"})


def test_exploration_start_creates_titled_session(db, admin_user):
    ref = ExplorationAdapter().start(
        db, admin_user, context={"title_hint": "供应链需求梳理"},
    )
    payload = contract.parse_ref("exploration", ref)
    session = db.query(ExplorationSession).filter(
        ExplorationSession.id == payload["session_id"]).first()
    assert session is not None
    assert session.title == "[委派] 供应链需求梳理"
    assert session.user_id == admin_user.id


def test_exploration_run_turn_normalizes_answer(db, admin_user, monkeypatch):
    ref = ExplorationAdapter().start(db, admin_user, context={"title_hint": "t"})
    captured = {}

    def fake_run_exploration_turn(db_, session_id, user, message, **kwargs):
        captured.update(session_id=session_id, message=message)
        yield {"type": "meta", "sessionId": session_id}
        yield {"type": "answer", "content": "画布已更新", "usage": {"inputTokens": 5}}

    monkeypatch.setattr(exploration_adapter, "run_exploration_turn", fake_run_exploration_turn)
    _, result = _drain(ExplorationAdapter().run_turn(db, admin_user, ref, "梳理订单模型"))
    assert result.status == STATUS_ANSWERED
    assert result.content == "画布已更新"
    assert result.conversation_ref == ref  # 引用保持不变（续用语义由委派表保证）
    assert captured["message"] == "梳理订单模型"


def test_exploration_run_turn_maps_error(db, admin_user, monkeypatch):
    ref = ExplorationAdapter().start(db, admin_user, context={"title_hint": "t"})

    def fake_run_exploration_turn(db_, session_id, user, message, **kwargs):
        yield {"type": "error", "message": "尚未配置可用的 LLM"}

    monkeypatch.setattr(exploration_adapter, "run_exploration_turn", fake_run_exploration_turn)
    _, result = _drain(ExplorationAdapter().run_turn(db, admin_user, ref, "q"))
    assert result.status == contract.STATUS_FAILED
    assert "LLM" in result.content


def test_exploration_foreign_session_denied(db, editor_user, monkeypatch):
    # admin 按域内语义可跨会话访问；越权路径用 editor 验证
    other = _custom_user(db)
    db.add(ExplorationSession(
        id="exp-foreign", user_id=other.id, title="他人探索", canvas={},
    ))
    db.commit()
    ref = build_ref("exploration", {"session_id": "exp-foreign"})
    with pytest.raises(contract.PermissionDeniedError):
        list(ExplorationAdapter().run_turn(db, editor_user, ref, "q"))
