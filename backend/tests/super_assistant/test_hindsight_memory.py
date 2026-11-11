"""跨会话持久记忆（Hindsight 集成）业务服务专项测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.super_assistant import hindsight_memory as hm
from app.super_assistant.memory_models import (
    SuperAssistantMemorySetting,
    SuperAssistantMemorySource,
)
from tests.conftest import TestSession


@pytest.fixture(autouse=True)
def _clear_bank_cache():
    hm._BANK_CONFIG_CACHE.clear()
    yield
    hm._BANK_CONFIG_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_platform_hindsight(monkeypatch):
    """默认无平台配置（unconfigured 三态的基线），个别用例显式覆盖。"""
    from app.shared.config import settings as platform_settings

    monkeypatch.setattr(platform_settings, "hindsight_base_url", "", raising=False)
    monkeypatch.setattr(platform_settings, "hindsight_api_key", "", raising=False)
    monkeypatch.setattr(platform_settings, "hindsight_namespace", "openontology", raising=False)
    monkeypatch.setattr(platform_settings, "hindsight_timeout_seconds", 2.0, raising=False)


def _occurred() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=3)


class TestEnqueueMessage:
    def test_idempotent_and_flush_only(self, db, admin_user):
        first = hm.enqueue_message(
            db, owner_id=admin_user.id, conversation_id="c1", message_id="m1",
            role="user", content="我喜欢深色主题", occurred_at=_occurred(),
        )
        second = hm.enqueue_message(
            db, owner_id=admin_user.id, conversation_id="c1", message_id="m1",
            role="user", content="我喜欢深色主题", occurred_at=_occurred(),
        )
        assert first.id == second.id
        assert db.query(SuperAssistantMemorySource).count() == 1
        assert first.status == "pending"
        assert first.role == "user"
        # enqueue 不 commit：回滚后不应有任何持久化行
        db.rollback()
        assert db.query(SuperAssistantMemorySource).count() == 0

    def test_rejects_bad_role_and_empty_content(self, db, admin_user):
        with pytest.raises(ValueError):
            hm.enqueue_message(
                db, owner_id=admin_user.id, conversation_id="c1", message_id="m1",
                role="system", content="x", occurred_at=_occurred(),
            )
        with pytest.raises(ValueError):
            hm.enqueue_message(
                db, owner_id=admin_user.id, conversation_id="c1", message_id="m2",
                role="user", content="  ", occurred_at=_occurred(),
            )

    def test_cross_owner_same_message_id_independent(self, db, admin_user, editor_user):
        a = hm.enqueue_message(
            db, owner_id=admin_user.id, conversation_id="c1", message_id="shared",
            role="user", content="内容", occurred_at=_occurred(),
        )
        b = hm.enqueue_message(
            db, owner_id=editor_user.id, conversation_id="c2", message_id="shared",
            role="user", content="内容", occurred_at=_occurred(),
        )
        assert a.id != b.id
        db.commit()
        assert db.query(SuperAssistantMemorySource).count() == 2

    def test_disabled_setting_blocks_new_enqueue_keeps_existing(self, db, admin_user):
        source = hm.enqueue_message(
            db, owner_id=admin_user.id, conversation_id="c1", message_id="m1",
            role="user", content="已入队", occurred_at=_occurred(),
        )
        db.commit()
        db.add(SuperAssistantMemorySetting(owner_id=admin_user.id, enabled=False))
        db.commit()
        with pytest.raises(hm.MemoryDisabledError):
            hm.enqueue_message(
                db, owner_id=admin_user.id, conversation_id="c1", message_id="m2",
                role="user", content="新消息", occurred_at=_occurred(),
            )
        db.rollback()
        assert db.get(SuperAssistantMemorySource, source.id) is not None  # 既有保留

    def test_context_rejects_long_labels(self, db, admin_user):
        with pytest.raises(ValueError):
            hm.enqueue_message(
                db, owner_id=admin_user.id, conversation_id="c1", message_id="m1",
                role="user", content="内容", occurred_at=_occurred(),
                context="长" * (hm.MAX_CONTEXT_CHARS + 1),
            )


class TestEnqueueExplicit:
    def _message(self, db, admin_user, content="请记住我喜欢深色主题"):
        from app.super_assistant.models import (
            SuperAssistantConversation,
            SuperAssistantMessage,
        )

        conversation = SuperAssistantConversation(owner_id=admin_user.id, title="t")
        db.add(conversation)
        db.flush()
        message = SuperAssistantMessage(
            conversation_id=conversation.id, role="user", content=content,
        )
        db.add(message)
        db.commit()
        return conversation.id, message.id

    def test_reuses_user_source_and_ignores_model_rewrite(self, db, admin_user):
        conversation_id, message_id = self._message(db, admin_user)
        source = hm.enqueue_message(
            db, owner_id=admin_user.id, conversation_id=conversation_id,
            message_id=message_id, role="user",
            content="请记住我喜欢深色主题", occurred_at=_occurred(),
        )
        db.commit()
        explicit = hm.enqueue_explicit(
            db, owner_id=admin_user.id, conversation_id=conversation_id,
            message_id=message_id, content="用户喜欢深色模式（模型转述）",
        )
        assert explicit.id == source.id
        assert explicit.explicit is True
        # 不采纳模型转写：事实仍是用户原文，且不产生第二行
        assert explicit.content == "请记住我喜欢深色主题"
        assert db.query(SuperAssistantMemorySource).count() == 1

    def test_creates_from_user_message_when_missing(self, db, admin_user):
        conversation_id, message_id = self._message(db, admin_user)
        explicit = hm.enqueue_explicit(
            db, owner_id=admin_user.id, conversation_id=conversation_id,
            message_id=message_id, content="whatever",
        )
        assert explicit.explicit is True
        assert explicit.content == "请记住我喜欢深色主题"
        assert explicit.message_id == message_id

    def test_rejects_missing_message(self, db, admin_user):
        with pytest.raises(ValueError):
            hm.enqueue_explicit(
                db, owner_id=admin_user.id, conversation_id="c1",
                message_id="missing", content="x",
            )

    def test_rejects_forgotten_source(self, db, admin_user):
        conversation_id, message_id = self._message(db, admin_user)
        source = hm.enqueue_message(
            db, owner_id=admin_user.id, conversation_id=conversation_id,
            message_id=message_id, role="user",
            content="请记住我喜欢深色主题", occurred_at=_occurred(),
        )
        db.commit()
        hm.forget_source(db, admin_user.id, source.id)
        db.commit()
        with pytest.raises(ValueError):
            hm.enqueue_explicit(
                db, owner_id=admin_user.id, conversation_id=conversation_id,
                message_id=message_id, content="x",
            )


class TestManualSourceAndCorrection:
    def test_manual_source_shape(self, db, admin_user):
        source = hm.create_manual_source(db, owner_id=admin_user.id, content="常驻上海")
        db.commit()
        assert source.explicit is True
        assert source.role == "user"
        assert source.status == "pending"
        assert source.message_id is None
        assert source.source_message_id  # 人工源 uuid

    def test_correction_appends_history(self, db, admin_user):
        first = hm.create_manual_source(db, owner_id=admin_user.id, content="常驻北京")
        db.commit()
        first.status = "synced"
        first.document_id = first.id
        db.commit()
        second = hm.create_manual_source(
            db, owner_id=admin_user.id, content="已搬到上海",
            correction_of=first.id,
        )
        db.commit()
        assert second.correction_of == first.id
        assert second.source_message_id == first.source_message_id
        assert second.version == first.version + 1
        # 旧行保留（历史追加，不覆盖）
        assert db.get(SuperAssistantMemorySource, first.id).status == "synced"

    def test_correction_cross_owner_rejected(self, db, admin_user, editor_user):
        target = hm.create_manual_source(db, owner_id=editor_user.id, content="x")
        db.commit()
        with pytest.raises(ValueError):
            hm.create_manual_source(
                db, owner_id=admin_user.id, content="y", correction_of=target.id,
            )


class TestForgetAndRetry:
    def test_forget_tombstone_clears_lease(self, db, admin_user):
        source = hm.enqueue_message(
            db, owner_id=admin_user.id, conversation_id="c", message_id="m",
            role="user", content="x", occurred_at=_occurred(),
        )
        db.commit()
        source.status = "processing"
        source.lease_token = "worker-a"
        source.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.commit()
        forgotten = hm.forget_source(db, admin_user.id, source.id)
        db.commit()
        assert forgotten.status == "forgetting"
        assert forgotten.lease_token is None
        assert forgotten.lease_expires_at is None
        # 幂等
        assert hm.forget_source(db, admin_user.id, source.id).status == "forgetting"
        # 跨 owner
        assert hm.forget_source(db, "someone-else", source.id) is None

    def test_retry_only_failed(self, db, admin_user):
        source = hm.create_manual_source(db, owner_id=admin_user.id, content="x")
        db.commit()
        with pytest.raises(ValueError):
            hm.retry_source(db, admin_user.id, source.id)
        source.status = "dead"
        source.attempt = 5
        source.operation_id = "op-1"
        db.commit()
        retried = hm.retry_source(db, admin_user.id, source.id)
        db.commit()
        assert retried.status == "pending"
        assert retried.attempt == 0
        assert retried.operation_id == "op-1"  # 完成重放核实依据保留
        assert hm.retry_source(db, "someone-else", source.id) is None


class TestRecall:
    def _synced(self, db, owner_id, content, *, status="synced", document_id=None,
                role="user"):
        source = hm.enqueue_message(
            db, owner_id=owner_id, conversation_id="c", message_id=f"m-{content}-{role}",
            role=role, content=content, occurred_at=_occurred(),
        )
        source.status = status
        source.document_id = document_id or source.id
        db.commit()
        return source

    def _configure(self, monkeypatch, db, owner_id, *, model="mc-1", enabled=True):
        from app.shared.config import settings as platform_settings

        monkeypatch.setattr(
            platform_settings, "hindsight_base_url", "http://hindsight", raising=False,
        )
        db.add(SuperAssistantMemorySetting(
            owner_id=owner_id, enabled=enabled, model_config_id=model,
        ))
        db.commit()

    def test_recall_strict_provenance(self, db, admin_user, monkeypatch):
        self._configure(monkeypatch, db, admin_user.id)
        live = self._synced(db, admin_user.id, "用户偏好深色主题")
        dying = self._synced(db, admin_user.id, "临时偏好红色")
        dying.status = "forgetting"  # tombstone：召回不得泄漏
        db.commit()

        class FakeClient:
            def __init__(self):
                self.payloads = []

            def recall(self, bank_id, payload):
                self.payloads.append((bank_id, payload))
                return {
                    "results": [
                        {"id": "f1", "text": "用户偏好深色主题", "type": "world",
                         "document_id": live.id},
                        {"id": "f2", "text": "临时偏好红色", "type": "world",
                         "document_id": dying.id},
                        # 混合 observation：一个 live + 一个 forgetting → 整条丢弃
                        {"id": "o1", "text": "用户对颜色有偏好（混合）", "type": "observation",
                         "document_id": None, "source_fact_ids": ["f1", "f2"]},
                        {"id": "o2", "text": "已遗忘的观察", "type": "observation",
                         "document_id": None, "source_fact_ids": ["f2"]},
                        # 纯 live observation：保留
                        {"id": "o3", "text": "稳定的颜色偏好", "type": "observation",
                         "document_id": None, "source_fact_ids": ["f1"]},
                        # 未知来源 / 缺映射：丢弃
                        {"id": "f9", "text": "未知来源事实", "type": "world",
                         "document_id": "doc-unknown"},
                        {"id": "o4", "text": "缺映射观察", "type": "observation",
                         "document_id": None, "source_fact_ids": ["f-missing"]},
                        {"id": "f10", "text": "无 provenance", "type": "world",
                         "document_id": None},
                    ],
                    "source_facts": {
                        "f1": {"id": "f1", "document_id": live.id},
                        "f2": {"id": "f2", "document_id": dying.id},
                    },
                }

        fake = FakeClient()
        monkeypatch.setattr(hm, "make_client", lambda timeout=None: fake)
        text, meta = hm.recall_context(db, admin_user.id, "颜色偏好")
        assert meta["state"] == "ready"
        assert "深色主题" in text
        assert "红色" not in text
        assert "混合" not in text
        assert "已遗忘的观察" not in text
        assert "未知来源" not in text
        assert "缺映射" not in text
        assert "无 provenance" not in text
        assert "稳定的颜色偏好" in text
        assert "背景资料" in text  # 非指令措辞
        assert "来源：用户消息" in text  # 来源角色标注
        assert fake.payloads[0][1]["max_tokens"] == hm.RECALL_MAX_TOKENS
        assert fake.payloads[0][1]["include"]["entities"] is None

    def test_recall_marks_assistant_role(self, db, admin_user, monkeypatch):
        self._configure(monkeypatch, db, admin_user.id)
        assistant = self._synced(
            db, admin_user.id, "方案B已确认", role="assistant",
        )

        class FakeClient:
            def recall(self, bank_id, payload):
                return {"results": [{
                    "id": "f1", "text": "方案B已确认", "type": "experience",
                    "document_id": assistant.id,
                }]}

        monkeypatch.setattr(hm, "make_client", lambda timeout=None: FakeClient())
        text, _ = hm.recall_context(db, admin_user.id, "方案")
        assert "助手陈述" in text

    def test_recall_unconfigured_states(self, db, admin_user, monkeypatch):
        text, meta = hm.recall_context(db, admin_user.id, "q")
        assert text == ""
        assert meta["state"] == "unconfigured"  # 平台未配置
        self._configure(monkeypatch, db, admin_user.id, model=None)
        _, meta = hm.recall_context(db, admin_user.id, "q")
        assert meta["state"] == "unconfigured"
        assert meta["message"] == "未选择记忆模型"
        db.query(SuperAssistantMemorySetting).delete()
        db.commit()
        self._configure(monkeypatch, db, admin_user.id, enabled=False)
        _, meta = hm.recall_context(db, admin_user.id, "q")
        assert meta["state"] == "unconfigured"
        assert meta["message"] == "记忆功能未启用"

    def test_recall_no_live_docs_checks_service_health(self, db, admin_user, monkeypatch):
        self._configure(monkeypatch, db, admin_user.id)
        called = []

        class FakeClient:
            def probe(self):
                called.append("probe")
                return {"status": "healthy"}

            def recall(self, bank_id, payload):
                pytest.fail("无已沉淀来源时不召回")

        monkeypatch.setattr(hm, "make_client", lambda timeout=None: FakeClient())
        _, meta = hm.recall_context(db, admin_user.id, "q")
        assert meta["state"] == "ready"
        assert called == ["probe"]

        class DeadClient:
            def probe(self):
                raise hm.HindsightUnavailableError("unreachable")

        monkeypatch.setattr(hm, "make_client", lambda timeout=None: DeadClient())
        _, meta = hm.recall_context(db, admin_user.id, "q")
        assert meta["state"] == "unavailable"

    def test_recall_preserves_uncommitted_caller_writes(self, db, admin_user, monkeypatch):
        self._configure(monkeypatch, db, admin_user.id)
        live = self._synced(db, admin_user.id, "正在实施供应链项目")
        live_id = live.id
        pending = hm.create_manual_source(
            db, owner_id=admin_user.id, content="调用方尚未提交的来源",
        )
        pending_id = pending.id

        class FakeClient:
            def recall(self, bank_id, payload):
                return {"results": [{
                    "id": "f1", "text": "正在实施供应链项目",
                    "document_id": live_id,
                }]}

        monkeypatch.setattr(hm, "make_client", lambda timeout=None: FakeClient())
        text, meta = hm.recall_context(db, admin_user.id, "项目进展")
        assert meta["state"] == "ready"
        assert "供应链项目" in text
        db.commit()
        with TestSession() as session:
            assert session.get(SuperAssistantMemorySource, pending_id) is not None

    def test_forget_during_recall_filters_returned_source(self, db, admin_user, monkeypatch):
        self._configure(monkeypatch, db, admin_user.id)
        live = self._synced(db, admin_user.id, "需要遗忘的旧方案")
        live_id = live.id
        owner_id = admin_user.id

        class FakeClient:
            def recall(self, bank_id, payload):
                with TestSession() as session:
                    hm.forget_source(session, owner_id, live_id)
                    session.commit()
                return {"results": [{
                    "id": "f1", "text": "需要遗忘的旧方案",
                    "document_id": live_id,
                }]}

        monkeypatch.setattr(hm, "make_client", lambda timeout=None: FakeClient())
        text, meta = hm.recall_context(db, owner_id, "方案")
        assert meta["state"] == "ready"
        assert text == ""

    def test_search_returns_recall_items(self, db, admin_user, monkeypatch):
        self._configure(monkeypatch, db, admin_user.id)
        live = self._synced(db, admin_user.id, "用户在做供应链项目")

        class FakeClient:
            def recall(self, bank_id, payload):
                return {
                    "results": [{
                        "id": "f1", "text": "用户在做供应链项目", "type": "experience",
                        "document_id": live.id,
                        "occurred_start": "2026-11-01T10:00:00+00:00",
                        "occurred_end": None,
                    }],
                }

        monkeypatch.setattr(hm, "make_client", lambda timeout=None: FakeClient())
        result = hm.search_memories(db, admin_user.id, "项目")
        assert result["state"] == "ready"
        item = result["items"][0]
        assert item["source_ids"] == [live.id]
        assert item["occurred_start"].startswith("2026-11-01")
        assert item["type"] == "experience"


class TestStatus:
    def test_status_states(self, db, admin_user, monkeypatch):
        from app.shared.config import settings as platform_settings

        # 1) 平台未配置
        status = hm.memory_status(db, admin_user.id)
        assert status["state"] == "unconfigured"
        # 2) 有平台配置但未选模型
        monkeypatch.setattr(
            platform_settings, "hindsight_base_url", "http://hindsight", raising=False,
        )
        status = hm.memory_status(db, admin_user.id)
        assert status["state"] == "unconfigured"
        assert "模型" in status["message"]
        # 3) 选了模型：probe 通过 + 有 pending → syncing
        setting = SuperAssistantMemorySetting(
            owner_id=admin_user.id, enabled=True, model_config_id="mc-1",
        )
        db.add(setting)
        db.commit()

        class FakeClient:
            def probe(self, *, timeout=2.0):
                return {"version": "1.2.0"}

        monkeypatch.setattr(hm, "make_client", lambda timeout=None: FakeClient())
        source = hm.enqueue_message(
            db, owner_id=admin_user.id, conversation_id="c", message_id="m",
            role="user", content="x", occurred_at=_occurred(),
        )
        db.commit()
        status = hm.memory_status(db, admin_user.id)
        assert status["state"] == "syncing"
        assert status["pending_count"] == 1
        # 4) 全部 synced → ready
        with TestSession() as session:
            row = session.get(SuperAssistantMemorySource, source.id)
            row.status = "synced"
            row.last_synced_at = datetime.now(timezone.utc)
            session.commit()
        status = hm.memory_status(db, admin_user.id)
        assert status["state"] == "ready"
        assert status["last_synced_at"] is not None
        # 5) probe 失败 → unavailable
        class DeadClient:
            def probe(self, *, timeout=2.0):
                raise hm.HindsightUnavailableError("boom")

        monkeypatch.setattr(hm, "make_client", lambda timeout=None: DeadClient())
        assert hm.memory_status(db, admin_user.id)["state"] == "unavailable"

    def test_status_failed_counts(self, db, admin_user, monkeypatch):
        from app.shared.config import settings as platform_settings

        monkeypatch.setattr(
            platform_settings, "hindsight_base_url", "http://hindsight", raising=False,
        )
        db.add(SuperAssistantMemorySetting(
            owner_id=admin_user.id, enabled=True, model_config_id="mc-1",
        ))
        source = hm.create_manual_source(db, owner_id=admin_user.id, content="x")
        db.commit()
        with TestSession() as session:
            row = session.get(SuperAssistantMemorySource, source.id)
            row.status = "failed"
            session.commit()

        class FakeClient:
            def probe(self, *, timeout=2.0):
                return {}

        monkeypatch.setattr(hm, "make_client", lambda timeout=None: FakeClient())
        status = hm.memory_status(db, admin_user.id)
        assert status["state"] == "error"
        assert status["failed_count"] == 1


class TestBankAndRetainItem:
    def test_ensure_bank_config(self, db, admin_user, monkeypatch):
        from app.shared.config import settings as platform_settings

        monkeypatch.setattr(
            platform_settings, "hindsight_base_url", "http://hindsight", raising=False,
        )

        class FakeClient:
            def __init__(self):
                self.banks = []
                self.configs = []

            def ensure_bank(self, bank_id, body):
                self.banks.append((bank_id, body))

            def update_bank_config(self, bank_id, updates):
                self.configs.append((bank_id, updates))

        fake = FakeClient()
        hm.ensure_bank(fake, admin_user.id)
        hm.ensure_bank(fake, admin_user.id)  # 缓存命中不再请求
        assert len(fake.banks) == 1
        bank_id = fake.banks[0][0]
        assert bank_id.startswith("openontology-")
        assert "retain_mission" in fake.banks[0][1]
        config = fake.configs[0][1]
        assert "llm_model" not in config  # 固定版本只允许服务级模型配置
        strategy = config["retain_strategies"]["oo-explicit"]
        assert strategy["retain_extraction_mode"] == "custom"
        assert strategy["retain_custom_instructions"]

    def test_build_retain_item(self, db, admin_user):
        source = hm.enqueue_message(
            db, owner_id=admin_user.id, conversation_id="conv-1", message_id="m-1",
            role="assistant", content="已达成：采用方案B", occurred_at=_occurred(),
            context="架构评审",
        )
        item = hm.build_retain_item(source)
        assert item["document_id"] == source.id
        assert item["metadata"]["role"] == "assistant"
        assert item["metadata"]["conversation_id"] == "conv-1"
        assert "助手" in item["context"]
        assert "strategy" not in item  # 非 explicit 用 bank 默认策略
        assert "+00:00" in item["timestamp"]  # ISO UTC

        explicit = hm.create_manual_source(db, owner_id=admin_user.id, content="常驻上海")
        item = hm.build_retain_item(explicit)
        assert item["strategy"] == "oo-explicit"
        assert item["metadata"]["explicit"] == "true"


class TestSettings:
    def test_get_settings_transient(self, db, admin_user):
        setting = hm.get_settings(db, admin_user.id)
        assert setting.enabled is True
        assert setting.model_config_id is None
        db.rollback()
        assert db.query(SuperAssistantMemorySetting).count() == 0  # 读路径不落库

    def test_list_sources_excludes_forgotten(self, db, admin_user):
        kept = hm.create_manual_source(db, owner_id=admin_user.id, content="保留")
        gone = hm.create_manual_source(db, owner_id=admin_user.id, content="已遗忘内容")
        db.commit()
        with TestSession() as session:
            row = session.get(SuperAssistantMemorySource, gone.id)
            row.status = "forgotten"
            session.commit()
        items, total = hm.list_sources(db, admin_user.id)
        assert total == 1
        assert items[0].id == kept.id
        items, _ = hm.list_sources(db, admin_user.id, query="遗忘")
        assert items == []
