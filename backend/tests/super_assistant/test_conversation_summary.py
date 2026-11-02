"""会话纪要（/api/v2/super-assistant/conversations/{id}/summary* 与 /summary/settings）。

行为分层验证：
- 拼装：complete 用户消息 + 助手最终回复按【用户消息/超级助手 时间】成文，
  工具调用过程（steps）、非 complete、空内容不进入；助手时间用 completed_at；
  超长转录从最旧处截断；
- 解析：五维微卡片模板宽容解析（标题/状态/标签/五要点，含续行与缺失字段）；
- 生成：默认用会话当前模型、用户级配置覆盖优先生效、每会话仅保留最新一份、
  空会话 400、LLM 失败 502、他人会话一律 404；
- 设置：默认 null、upsert 往返、重置回 null、非法模型 400。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.model_configs.models import ModelConfig
from app.shared.database import Base
from app.super_assistant import conversation_summary
from app.super_assistant.conversation_summary import router as summary_router
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantConversationSummary,
    SuperAssistantMessage,
    SuperAssistantSummarySetting,
)

_TABLES = [
    User.__table__,
    ModelConfig.__table__,
    SuperAssistantConversation.__table__,
    SuperAssistantMessage.__table__,
    SuperAssistantConversationSummary.__table__,
    SuperAssistantSummarySetting.__table__,
]

_SAMPLE_CARD = """### 📋 [查询平台Skill列表]
`状态：已完成` ｜ `标签：#Skill #查询`

- **🎯 核心目标**：帮用户确认平台当前可用的 Skill 清单
- **⚡ 关键动作**：① 查询技能目录 ② 过滤启用项 ③ 汇总回复
- **🚧 踩坑决策**：首次查询超时 → 重试一次成功
- **🏆 最终产出**：确认 web-access 等技能可用并告知调用方式
- **📌 后续待办**：无
"""


def _make_client(tmp_path, monkeypatch, *, current_id="user-1", fake_chat=None):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'summary.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(summary_router, prefix="/api/v2/super-assistant")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: User(
        id=current_id, username="owner", email="owner@example.com",
        password_hash="unused", role="editor",
    )
    if fake_chat is not None:
        monkeypatch.setattr(conversation_summary, "llm_chat", fake_chat)
    with TestingSession() as db:
        for uid in ("user-1", "user-2"):
            db.add(User(
                id=uid, username=uid, email=f"{uid}@example.com",
                password_hash="unused", role="editor",
            ))
        for model_id, name, is_default in (
            ("model-1", "DeepSeek", True),
            ("model-2", "Qwen", False),
        ):
            db.add(ModelConfig(
                id=model_id, name=name, config_type="llm", provider="openai",
                api_base="https://api.example.com", api_key_encrypted=None,
                models=["test-model"], options={}, enabled=True,
                is_default=is_default, created_by="user-1",
            ))
        db.add(SuperAssistantConversation(
            id="conv-1", owner_id="user-1", title="会话一", model_config_id="model-1",
        ))
        db.add(SuperAssistantConversation(
            id="conv-other", owner_id="user-2", title="他人会话", model_config_id="model-1",
        ))
        db.commit()
    return TestClient(app), TestingSession


def _seed_messages(Session, conversation_id: str, rows: list[dict]) -> None:
    with Session() as db:
        for row in rows:
            db.add(SuperAssistantMessage(conversation_id=conversation_id, **row))
        db.commit()


def _row(role, content, status="complete", created_at=None, completed_at=None, steps=None):
    return {
        "role": role,
        "content": content,
        "status": status,
        "created_at": created_at or datetime(2026, 7, 10, 14, 50),
        "completed_at": completed_at,
        "steps": steps or [],
    }


# ---------- 拼装 ----------

def test_build_transcript_formats_complete_messages_only(tmp_path, monkeypatch):
    _, Session = _make_client(tmp_path, monkeypatch)
    _seed_messages(Session, "conv-1", [
        _row("user", "帮我查询有哪些Skill"),
        _row("assistant", "", status="streaming", steps=[{"toolName": "web_access"}]),
        # created_at 显式晚于用户消息：同分钟内插入的行 id 是随机 UUID，
        # (created_at, id) 排序在并列时不确定，需保证时序可复现
        _row("assistant", "当前有web-access技能，还有其它等",
             created_at=datetime(2026, 7, 10, 14, 50, 30),
             completed_at=datetime(2026, 7, 10, 14, 51)),
        _row("assistant", "已停止生成", status="cancelled"),
        _row("user", "可以帮我调用吗？", created_at=datetime(2026, 7, 10, 14, 52)),
        _row("user", "   "),
    ])
    with Session() as db:
        conversation = db.get(SuperAssistantConversation, "conv-1")
        transcript, included = conversation_summary.build_transcript(db, conversation)
    lines = transcript.splitlines()
    assert included == 3
    assert lines == [
        "【用户消息 2026-07-10 14:50】帮我查询有哪些Skill",
        "【超级助手 2026-07-10 14:51】当前有web-access技能，还有其它等",
        "【用户消息 2026-07-10 14:52】可以帮我调用吗？",
    ]


def test_build_transcript_truncates_oldest_when_overlong(tmp_path, monkeypatch):
    _, Session = _make_client(tmp_path, monkeypatch)
    _seed_messages(Session, "conv-1", [
        _row("user", "很早的消息" * 40, created_at=datetime(2026, 7, 10, 9, 0)),
        _row("assistant", "最新回复", completed_at=datetime(2026, 7, 10, 14, 59)),
    ])
    monkeypatch.setattr(conversation_summary, "_MAX_TRANSCRIPT_CHARS", 30)
    with Session() as db:
        conversation = db.get(SuperAssistantConversation, "conv-1")
        transcript, included = conversation_summary.build_transcript(db, conversation)
    assert included == 2
    assert transcript.startswith("（较早的对话内容已省略）")
    assert transcript.endswith("【超级助手 2026-07-10 14:59】最新回复")


# ---------- 解析 ----------

def test_parse_summary_card_extracts_all_fields():
    structured = conversation_summary.parse_summary_card(_SAMPLE_CARD)
    assert structured["title"] == "查询平台Skill列表"
    assert structured["status"] == "已完成"
    assert structured["tags"] == ["Skill", "查询"]
    assert structured["goal"] == "帮用户确认平台当前可用的 Skill 清单"
    assert structured["actions"] == "① 查询技能目录 ② 过滤启用项 ③ 汇总回复"
    assert structured["pitfalls"] == "首次查询超时 → 重试一次成功"
    assert structured["outcome"] == "确认 web-access 等技能可用并告知调用方式"
    assert structured["todos"] == "无"


def test_parse_summary_card_tolerates_missing_fields_and_continuation():
    raw = """### 📋 会话纪要
`状态：进行中` ｜ `标签：#调试`

- **🎯 核心目标**：排查登录失败
  这一行是续行
- **⚡ 关键动作**：① 复现 ② 看日志
"""
    structured = conversation_summary.parse_summary_card(raw)
    assert structured["title"] == "会话纪要"
    assert structured["status"] == "进行中"
    assert structured["tags"] == ["调试"]
    assert structured["goal"] == "排查登录失败 这一行是续行"
    assert structured["actions"] == "① 复现 ② 看日志"
    assert structured["pitfalls"] == ""
    assert structured["todos"] == ""


def test_parse_summary_card_survives_garbage_output():
    structured = conversation_summary.parse_summary_card("模型跑偏了，没有按模板输出。")
    assert structured["title"] == ""
    assert structured["tags"] == []
    assert structured["goal"] == ""


# ---------- 生成 ----------

def test_generate_uses_conversation_model_and_upserts_latest(tmp_path, monkeypatch):
    calls: list[dict] = []

    def fake_chat(call_kwargs, messages, tools):
        calls.append({"kwargs": call_kwargs, "user": messages[1]["content"]})
        return {"content": _SAMPLE_CARD if len(calls) == 1 else
                _SAMPLE_CARD.replace("查询平台Skill列表", "第二轮纪要")}

    client, Session = _make_client(tmp_path, monkeypatch, fake_chat=fake_chat)
    _seed_messages(Session, "conv-1", [
        _row("user", "帮我查询有哪些Skill"),
        _row("assistant", "当前有web-access技能", completed_at=datetime(2026, 7, 10, 14, 51)),
    ])

    first = client.post("/api/v2/super-assistant/conversations/conv-1/summary/generate")
    assert first.status_code == 200, first.text
    assert first.json()["structured"]["title"] == "查询平台Skill列表"
    assert first.json()["model_config_id"] == "model-1"
    assert first.json()["message_count"] == 2

    second = client.post("/api/v2/super-assistant/conversations/conv-1/summary/generate")
    assert second.status_code == 200, second.text
    assert second.json()["structured"]["title"] == "第二轮纪要"

    # 输入正文：拼装格式 + 提示词以 system 注入
    assert "【用户消息 2026-07-10 14:50】帮我查询有哪些Skill" in calls[0]["user"]
    assert "【超级助手 2026-07-10 14:51】当前有web-access技能" in calls[0]["user"]
    assert calls[0]["user"].startswith('# Input Text')
    assert calls[0]["kwargs"]["model_config_id"] == "model-1"

    # 每会话仅保留最新一份
    with Session() as db:
        rows = db.query(SuperAssistantConversationSummary).all()
        assert len(rows) == 1
        assert rows[0].markdown is not None
        assert rows[0].generated_at is not None

    stored = client.get("/api/v2/super-assistant/conversations/conv-1/summary")
    assert stored.status_code == 200
    assert stored.json()["structured"]["title"] == "第二轮纪要"


def test_generate_prefers_user_setting_model(tmp_path, monkeypatch):
    calls: list[dict] = []

    def fake_chat(call_kwargs, messages, tools):
        calls.append(call_kwargs)
        return {"content": _SAMPLE_CARD}

    client, Session = _make_client(tmp_path, monkeypatch, fake_chat=fake_chat)
    put = client.put("/api/v2/super-assistant/summary/settings", json={"model_config_id": "model-2"})
    assert put.status_code == 200, put.text
    _seed_messages(Session, "conv-1", [_row("user", "你好")])

    resp = client.post("/api/v2/super-assistant/conversations/conv-1/summary/generate")
    assert resp.status_code == 200, resp.text
    assert calls[0]["model_config_id"] == "model-2"
    assert resp.json()["model_config_id"] == "model-2"


def test_generate_rejects_empty_and_foreign_conversations(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, fake_chat=lambda *args: {"content": "x"})
    empty = client.post("/api/v2/super-assistant/conversations/conv-1/summary/generate")
    assert empty.status_code == 400
    assert empty.json()["detail"] == "暂无可总结的对话内容"
    foreign = client.post("/api/v2/super-assistant/conversations/conv-other/summary/generate")
    assert foreign.status_code == 404
    foreign_get = client.get("/api/v2/super-assistant/conversations/conv-other/summary")
    assert foreign_get.status_code == 404


def test_generate_maps_llm_failure_to_502(tmp_path, monkeypatch):
    def broken_chat(call_kwargs, messages, tools):
        raise RuntimeError("upstream timeout")

    client, Session = _make_client(tmp_path, monkeypatch, fake_chat=broken_chat)
    _seed_messages(Session, "conv-1", [_row("user", "你好")])
    resp = client.post("/api/v2/super-assistant/conversations/conv-1/summary/generate")
    assert resp.status_code == 502
    assert "会话纪要生成失败" in resp.json()["detail"]


def test_get_summary_returns_null_before_generation(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    resp = client.get("/api/v2/super-assistant/conversations/conv-1/summary")
    assert resp.status_code == 200
    assert resp.json() is None


# ---------- 用户级设置 ----------

def test_settings_default_upsert_reset_and_validation(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    # 未配置：跟随会话模型
    assert client.get("/api/v2/super-assistant/summary/settings").json() is None
    # 保存全局模型
    saved = client.put("/api/v2/super-assistant/summary/settings", json={"model_config_id": "model-2"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["model_config_id"] == "model-2"
    assert saved.json()["updated_at"] is not None
    assert client.get("/api/v2/super-assistant/summary/settings").json()["model_config_id"] == "model-2"
    # 重置回跟随会话模型
    reset = client.put("/api/v2/super-assistant/summary/settings", json={"model_config_id": None})
    assert reset.status_code == 200
    assert reset.json()["model_config_id"] is None
    # 非法模型：不存在 / 非 llm / 未启用 一律 400
    assert client.put(
        "/api/v2/super-assistant/summary/settings", json={"model_config_id": "no-such-model"},
    ).status_code == 400
