"""超级助手会话链路的跨会话记忆入队行为（Hindsight 主链，服务层）。

覆盖 conversation_service.chat 的用户来源入队语义：

- 用户消息与记忆来源同一事务：入队失败整轮回滚，无孤立来源/半截消息；
- 用户关闭记忆后不产生来源，聊天流程照常（消息照落、流照返）；
- 较长用户输入全文入队不截断；用户来源不携带 context，不受
  MAX_CONTEXT_CHARS（120，出处线索上限）影响。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.shared.database import Base
from app.super_assistant import conversation_service
from app.super_assistant.memory_models import (
    SuperAssistantMemorySetting,
    SuperAssistantMemorySource,
)
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMessage,
)
from app.super_assistant.schemas import ChatRequest

_TABLES = [
    User.__table__,
    SuperAssistantConversation.__table__,
    SuperAssistantMessage.__table__,
    # Hindsight 主链：chat() 同事务入队读 settings、写 sources
    SuperAssistantMemorySource.__table__,
    SuperAssistantMemorySetting.__table__,
]


def _make_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'conversation-memory.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_TABLES)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed(db, owner_id="owner-1"):
    db.add(User(
        id=owner_id, username=owner_id, email=f"{owner_id}@example.com",
        password_hash="unused", role="editor",
    ))
    conversation = SuperAssistantConversation(
        id="conv-1", owner_id=owner_id, title="新会话",
    )
    db.add(conversation)
    db.commit()
    return db.get(User, owner_id), conversation


def _fake_stream(observed: dict | None = None):
    def fake(**kwargs):
        if observed is not None:
            observed.update(kwargs)
        return iter(["event: done\ndata: {}\n\n"])

    return fake


def test_chat_enqueues_user_source_in_same_transaction(tmp_path):
    Session = _make_session(tmp_path)
    with Session() as db:
        user, _ = _seed(db)
        observed: dict = {}
        response = conversation_service.chat(
            "conv-1", ChatRequest(message="请记住我喜欢黑咖啡"), db, user,
            stream_chat_fn=_fake_stream(observed),
        )
        assert response.media_type == "text/event-stream"
        assert observed["owner_id"] == "owner-1"

        source = db.query(SuperAssistantMemorySource).one()
        assert source.role == "user"
        assert source.status == "pending"
        assert source.content == "请记住我喜欢黑咖啡"
        assert source.conversation_id == "conv-1"
        # 用户来源不携带出处线索：正文全文即事实，不走 context 通道
        assert source.context == ""
        # 消息与来源同一事务落库，来源指回用户消息
        user_message = (
            db.query(SuperAssistantMessage)
            .filter_by(role="user")
            .one()
        )
        assert source.message_id == user_message.id


def test_chat_enqueue_failure_rolls_back_whole_turn(tmp_path, monkeypatch):
    """入队异常时用户消息一并回滚：无孤立来源，也无半截回合。"""
    from app.super_assistant import hindsight_memory

    Session = _make_session(tmp_path)
    with Session() as db:
        user, _ = _seed(db)

        def failing_enqueue(*_args, **_kwargs):
            raise RuntimeError("入队失败")

        monkeypatch.setattr(hindsight_memory, "enqueue_message", failing_enqueue)
        with pytest.raises(RuntimeError, match="入队失败"):
            conversation_service.chat(
                "conv-1", ChatRequest(message="请记住时区"), db, user,
                stream_chat_fn=_fake_stream(),
            )
        # 模拟请求依赖结束时的会话回滚
        db.rollback()
        assert db.query(SuperAssistantMemorySource).count() == 0
        assert db.query(SuperAssistantMessage).count() == 0


def test_chat_memory_disabled_streams_without_source(tmp_path):
    Session = _make_session(tmp_path)
    with Session() as db:
        user, _ = _seed(db)
        db.add(SuperAssistantMemorySetting(owner_id="owner-1", enabled=False))
        db.commit()
        observed: dict = {}
        response = conversation_service.chat(
            "conv-1", ChatRequest(message="普通问题"), db, user,
            stream_chat_fn=_fake_stream(observed),
        )
        # 聊天照常：流响应返回、本轮 user/assistant 消息均已落库
        assert response.media_type == "text/event-stream"
        assert observed["assistant_message_id"]
        roles = sorted(
            message.role
            for message in db.query(SuperAssistantMessage).all()
        )
        assert roles == ["assistant", "user"]
        assert db.query(SuperAssistantMemorySource).count() == 0


def test_chat_long_user_input_kept_in_full(tmp_path):
    """长用户输入全文入队不截断，也不受 context 120 上限影响。"""
    from app.super_assistant.hindsight_memory import MAX_CONTEXT_CHARS

    Session = _make_session(tmp_path)
    long_input = "我的采购偏好：" + "蓝山咖啡豆、手冲滤纸、电子秤；" * 40
    assert len(long_input) > MAX_CONTEXT_CHARS * 5
    with Session() as db:
        user, _ = _seed(db)
        conversation_service.chat(
            "conv-1", ChatRequest(message=long_input), db, user,
            stream_chat_fn=_fake_stream(),
        )
        source = db.query(SuperAssistantMemorySource).one()
        assert source.content == long_input
        assert source.context == ""
        assert len(source.content) == len(long_input)
