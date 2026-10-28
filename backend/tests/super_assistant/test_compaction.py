from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import compaction
from app.super_assistant.models import SuperAssistantConversation


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'compaction.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=[SuperAssistantConversation.__table__])
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def conversation(db_session):
    item = SuperAssistantConversation(id="conv-1", owner_id="user-1", title="压缩")
    db_session.add(item)
    db_session.commit()
    return item


def _tight_settings(monkeypatch, keep_recent=2, headroom=0.5):
    monkeypatch.setattr(settings, "super_assistant_compaction_keep_recent", keep_recent)
    monkeypatch.setattr(settings, "super_assistant_context_headroom", headroom)


def _long_messages(non_system_count: int, chars: int = 10) -> list[dict]:
    messages = [{"role": "system", "content": "系统提示"}]
    for index in range(non_system_count):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"第{index}条" + "字" * chars})
    return messages


def test_estimate_tokens_counts_cjk_and_latin():
    assert compaction.estimate_tokens("") == 0
    assert compaction.estimate_tokens("你好世界") == 4
    assert compaction.estimate_tokens("hello") == 2  # ceil(5/4)
    assert compaction.estimate_tokens("你好ab") == 3  # 2 + ceil(2/4)


def test_estimate_messages_includes_tool_call_arguments():
    arguments = {"q": "本体"}
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "name": "web_search", "arguments": arguments}]},
    ]
    expected = compaction.estimate_tokens("你好") + compaction.estimate_tokens(
        json.dumps(arguments, ensure_ascii=False)
    )
    assert compaction.estimate_messages(messages) == expected


def test_maybe_compact_returns_same_object_below_threshold(db_session, conversation):
    messages = [{"role": "system", "content": "系统提示"}, {"role": "user", "content": "短"}]
    result = compaction.maybe_compact(
        db_session, conversation, {"max_context_tokens": 10000}, messages,
    )
    assert result is messages
    assert conversation.summary is None


def test_maybe_compact_requires_enough_non_system_messages(
    db_session, conversation, monkeypatch,
):
    _tight_settings(monkeypatch, keep_recent=2, headroom=0.5)
    messages = _long_messages(non_system_count=4)  # 4 <= keep_recent(2) + 2
    result = compaction.maybe_compact(
        db_session, conversation, {"max_context_tokens": 10}, messages,
    )
    assert result is messages
    assert conversation.summary is None


def test_maybe_compact_summarizes_oldest_and_persists(
    db_session, conversation, monkeypatch,
):
    _tight_settings(monkeypatch, keep_recent=2, headroom=0.5)
    messages = _long_messages(non_system_count=6)
    captured: dict = {}

    def fake_chat(call_kwargs, chat_messages, tools):
        captured["call_kwargs"] = call_kwargs
        captured["messages"] = chat_messages
        captured["tools"] = tools
        return {"content": "压缩后的摘要", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(compaction.provider, "chat", fake_chat)
    result = compaction.maybe_compact(
        db_session, conversation, {"max_context_tokens": 10, "model": "m"}, messages,
    )
    assert result == [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "[早前对话摘要]\n压缩后的摘要"},
        *messages[-2:],
    ]
    assert conversation.summary == "压缩后的摘要"
    assert conversation.summary_message_count == 4  # 6 - keep_recent(2)
    assert captured["call_kwargs"]["max_output_tokens"] == 1024
    assert captured["call_kwargs"]["model"] == "m"  # 其余调用参数原样保留
    assert captured["tools"] == []
    prompt = captured["messages"][0]["content"]
    assert "第0条" in prompt and "第3条" in prompt


def test_maybe_compact_rolls_previous_summary_into_prompt(
    db_session, conversation, monkeypatch,
):
    _tight_settings(monkeypatch, keep_recent=2, headroom=0.5)
    conversation.summary = "旧摘要"
    conversation.summary_message_count = 3
    db_session.commit()
    captured: dict = {}

    def fake_chat(_call_kwargs, chat_messages, _tools):
        captured["prompt"] = chat_messages[0]["content"]
        return {"content": "滚动后的摘要", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(compaction.provider, "chat", fake_chat)
    messages = _long_messages(non_system_count=6)
    result = compaction.maybe_compact(
        db_session, conversation, {"max_context_tokens": 10}, messages,
    )
    assert "旧摘要" in captured["prompt"]
    assert conversation.summary == "滚动后的摘要"
    assert conversation.summary_message_count == 7  # 3 + 4
    assert result[1]["content"] == "[早前对话摘要]\n滚动后的摘要"


def test_maybe_compact_truncates_long_tool_results_in_prompt(
    db_session, conversation, monkeypatch,
):
    _tight_settings(monkeypatch, keep_recent=2, headroom=0.5)
    long_tool_content = "A" * 400 + "中" * 300 + "B" * 200
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "tool", "tool_call_id": "c1", "name": "web_fetch", "content": long_tool_content},
        {"role": "user", "content": "追问一" * 30},
        {"role": "assistant", "content": "回答一" * 30},
        {"role": "user", "content": "最近一"},
        {"role": "assistant", "content": "最近二"},
    ]
    captured: dict = {}

    def fake_chat(_call_kwargs, chat_messages, _tools):
        captured["prompt"] = chat_messages[0]["content"]
        return {"content": "摘要", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(compaction.provider, "chat", fake_chat)
    compaction.maybe_compact(db_session, conversation, {"max_context_tokens": 10}, messages)
    prompt = captured["prompt"]
    assert "A" * 400 in prompt
    assert "B" * 200 in prompt
    assert "中" * 300 not in prompt
    assert "…[中间内容省略]…" in prompt


def test_maybe_compact_returns_original_messages_when_summary_fails(
    db_session, conversation, monkeypatch,
):
    _tight_settings(monkeypatch, keep_recent=2, headroom=0.5)
    messages = _long_messages(non_system_count=6)

    def failing_chat(*_args, **_kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(compaction.provider, "chat", failing_chat)
    result = compaction.maybe_compact(
        db_session, conversation, {"max_context_tokens": 10}, messages,
    )
    assert result is messages
    assert conversation.summary is None
    assert conversation.summary_message_count == 0


def test_maybe_compact_returns_original_messages_when_summary_is_empty(
    db_session, conversation, monkeypatch,
):
    _tight_settings(monkeypatch, keep_recent=2, headroom=0.5)
    messages = _long_messages(non_system_count=6)
    monkeypatch.setattr(
        compaction.provider, "chat",
        lambda *_args, **_kwargs: {"content": "  ", "tool_calls": [], "usage": {}},
    )
    result = compaction.maybe_compact(
        db_session, conversation, {"max_context_tokens": 10}, messages,
    )
    assert result is messages
    assert conversation.summary is None


def test_maybe_compact_redacts_ephemeral_detail_tool_content(
    db_session, conversation, monkeypatch,
):
    """ephemeral 明细红线：命中 redactions 的 tool 内容整体替换为壳，
    不走头尾截断（头 400 字符足以携带多行源明细）。"""
    _tight_settings(monkeypatch, keep_recent=2, headroom=0.5)
    detail_content = json.dumps({
        "rows": [{"orderId": "SO-1001", "secret": "机密" * 100}],
        "totalRowCount": 1,
    }, ensure_ascii=False)
    shell_text = json.dumps({
        "queryId": "qs-1", "totalRowCount": 1, "ephemeralDetail": True,
    }, ensure_ascii=False)
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "tool", "tool_call_id": "c1", "name": "ontology_query",
         "content": detail_content},
        {"role": "user", "content": "追问一" * 30},
        {"role": "assistant", "content": "回答一" * 30},
        {"role": "user", "content": "最近一"},
        {"role": "assistant", "content": "最近二"},
    ]
    captured: dict = {}

    def fake_chat(_call_kwargs, chat_messages, _tools):
        captured["prompt"] = chat_messages[0]["content"]
        return {"content": "摘要", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(compaction.provider, "chat", fake_chat)
    compaction.maybe_compact(
        db_session, conversation, {"max_context_tokens": 10}, messages,
        redactions={"c1": shell_text},
    )
    prompt = captured["prompt"]
    assert "qs-1" in prompt and "ephemeralDetail" in prompt
    assert "SO-1001" not in prompt and "机密" not in prompt


def test_summary_prompt_unredacted_tool_content_keeps_head_tail_behavior(
    db_session, conversation, monkeypatch,
):
    """未命中 redactions 的 tool 内容保持原头尾截断行为（旧行为不变）。"""
    _tight_settings(monkeypatch, keep_recent=2, headroom=0.5)
    long_tool_content = "A" * 400 + "中" * 300 + "B" * 200
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "tool", "tool_call_id": "c-other", "name": "web_fetch",
         "content": long_tool_content},
        {"role": "user", "content": "历史一"},
        {"role": "assistant", "content": "历史二"},
        {"role": "user", "content": "最近一"},
        {"role": "assistant", "content": "最近二"},
    ]
    captured: dict = {}

    def fake_chat(_call_kwargs, chat_messages, _tools):
        captured["prompt"] = chat_messages[0]["content"]
        return {"content": "摘要", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(compaction.provider, "chat", fake_chat)
    compaction.maybe_compact(
        db_session, conversation, {"max_context_tokens": 10}, messages,
        redactions={"c-unrelated": "{}"},
    )
    prompt = captured["prompt"]
    assert "A" * 400 in prompt and "中" * 300 not in prompt
