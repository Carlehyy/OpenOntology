"""跨会话记忆管理 API 最小契约测试（owner 隔离/设置可空/遗忘契约）。"""
from __future__ import annotations

import pytest

from app.super_assistant import hindsight_memory as hm
from app.super_assistant import memory_llm
from app.super_assistant.memory_models import (
    SuperAssistantMemorySetting,
    SuperAssistantMemorySource,
)

BASE = "/api/v2/super-assistant/memory"


@pytest.fixture(autouse=True)
def memory_env(monkeypatch):
    from app.shared.config import settings as platform_settings

    monkeypatch.setattr(platform_settings, "hindsight_base_url", "", raising=False)
    monkeypatch.setattr(platform_settings, "hindsight_api_key", "", raising=False)
    hm._BANK_CONFIG_CACHE.clear()
    # LLM 能力探测默认放行；个别用例显式改为失败
    monkeypatch.setattr(
        memory_llm, "validate_memory_model",
        lambda db, model_config_id: {"ok": True, "code": "ok", "message": ""},
    )
    sent: list[tuple[str, str]] = []

    def fake_dispatch(source_id, dispatch_id):
        sent.append((source_id, dispatch_id))

    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_super_assistant_memory_sync",
        fake_dispatch,
    )
    yield sent
    hm._BANK_CONFIG_CACHE.clear()


@pytest.fixture
def llm_config(db, admin_user):
    from app.model_configs.models import ModelConfig

    config = ModelConfig(
        id="mc-openai", name="记忆模型", config_type="llm", provider="openai",
        models=["gpt-4o-mini"], options={}, enabled=True, created_by=admin_user.id,
    )
    db.add(config)
    db.commit()
    return config


def _manual_source(db, owner_id, content="内容"):
    source = hm.create_manual_source(db, owner_id=owner_id, content=content)
    db.commit()
    return source


class TestSettings:
    def test_default_settings(self, client, auth_headers):
        response = client.get(f"{BASE}/settings", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == {"enabled": True, "model_config_id": None}

    def test_patch_model_requires_valid_config(self, client, auth_headers, db):
        response = client.patch(
            f"{BASE}/settings",
            json={"model_config_id": "missing"}, headers=auth_headers,
        )
        assert response.status_code == 400

    def test_patch_model_rejects_wrong_provider(
        self, client, auth_headers, db, admin_user, llm_config,
    ):
        llm_config.provider = "anthropic"
        db.commit()
        response = client.patch(
            f"{BASE}/settings",
            json={"model_config_id": llm_config.id}, headers=auth_headers,
        )
        assert response.status_code == 400

    def test_patch_model_rejects_capability_failure(
        self, client, auth_headers, monkeypatch, llm_config,
    ):
        monkeypatch.setattr(
            memory_llm, "validate_memory_model",
            lambda db, model_config_id: {"ok": False, "code": "probe_failed",
                                         "message": "样例未通过"},
        )
        response = client.patch(
            f"{BASE}/settings",
            json={"model_config_id": llm_config.id}, headers=auth_headers,
        )
        assert response.status_code == 400
        assert "样例未通过" in response.json()["detail"]

    def test_patch_selects_model_and_null_clears(self, client, auth_headers, llm_config):
        response = client.patch(
            f"{BASE}/settings",
            json={"model_config_id": llm_config.id}, headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["model_config_id"] == llm_config.id
        # 显式传 null 清空选择（不回退默认、不猜测）
        response = client.patch(
            f"{BASE}/settings",
            json={"model_config_id": None}, headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["model_config_id"] is None
        assert response.json()["enabled"] is True

    def test_patch_enabled(self, client, auth_headers, llm_config):
        response = client.patch(
            f"{BASE}/settings",
            json={"enabled": False, "model_config_id": llm_config.id},
            headers=auth_headers,
        )
        assert response.json()["enabled"] is False
        assert response.json()["model_config_id"] == llm_config.id


class TestStatus:
    def test_unconfigured_without_platform(self, client, auth_headers):
        response = client.get(f"{BASE}/status", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "unconfigured"
        assert body["pending_count"] == 0 and body["failed_count"] == 0


class TestSources:
    def test_create_dispatches_with_generation_token(self, client, auth_headers, memory_env):
        response = client.post(
            f"{BASE}/sources", json={"content": "常驻上海"}, headers=auth_headers,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "pending"
        assert body["explicit"] is True
        assert len(memory_env) == 1
        assert memory_env[0][0] == body["id"]
        assert isinstance(memory_env[0][1], str) and len(memory_env[0][1]) >= 16  # 代次令牌

    def test_list_and_query(self, client, auth_headers, db, admin_user):
        _manual_source(db, admin_user.id, "用户偏好深色主题")
        _manual_source(db, admin_user.id, "在做供应链项目")
        response = client.get(f"{BASE}/sources", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["total"] == 2
        response = client.get(
            f"{BASE}/sources", params={"query": "供应链"}, headers=auth_headers,
        )
        assert response.json()["total"] == 1
        assert "供应链" in response.json()["items"][0]["content"]

    def test_correction_unknown_target_404_no_leak(self, client, auth_headers):
        response = client.post(
            f"{BASE}/sources",
            json={"content": "更正", "correction_of": "not-exists"},
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_correction_appends_version(self, client, auth_headers, db, admin_user):
        first = _manual_source(db, admin_user.id)
        response = client.post(
            f"{BASE}/sources",
            json={"content": "已更正的事实", "correction_of": first.id},
            headers=auth_headers,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["correction_of"] == first.id
        row = db.get(SuperAssistantMemorySource, body["id"])
        assert row.version == first.version + 1
        assert row.source_message_id == first.source_message_id

    def test_delete_forgets_and_cross_owner_404(
        self, client, auth_headers, db, admin_user, editor_user, memory_env,
    ):
        mine = _manual_source(db, admin_user.id)
        theirs = _manual_source(db, editor_user.id)
        response = client.delete(f"{BASE}/sources/{theirs.id}", headers=auth_headers)
        assert response.status_code == 404  # 跨 owner 不泄露
        response = client.delete(f"{BASE}/sources/{mine.id}", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["status"] == "forgetting"  # 完成前保持 forgetting
        assert len(memory_env) == 1

    def test_retry_contract(self, client, auth_headers, db, admin_user, memory_env):
        source = _manual_source(db, admin_user.id)
        response = client.post(f"{BASE}/sources/{source.id}/retry", headers=auth_headers)
        assert response.status_code == 409  # 非失败来源不可重试
        row = db.get(SuperAssistantMemorySource, source.id)
        row.status = "dead"
        row.attempt = 5
        db.commit()
        response = client.post(f"{BASE}/sources/{source.id}/retry", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        assert len(memory_env) == 1

    def test_owner_isolation_in_list(self, client, auth_headers, db, editor_user):
        # editor 的来源对 admin 不可见
        _manual_source(db, editor_user.id, "他人记忆")
        response = client.get(f"{BASE}/sources", headers=auth_headers)
        assert response.json()["total"] == 0


class TestSearch:
    def test_search_returns_items_with_provenance(
        self, client, auth_headers, db, admin_user, monkeypatch,
    ):
        from app.shared.config import settings as platform_settings

        monkeypatch.setattr(
            platform_settings, "hindsight_base_url", "http://hindsight",
            raising=False,
        )
        db.add(SuperAssistantMemorySetting(
            owner_id=admin_user.id, enabled=True, model_config_id="mc-1",
        ))
        from datetime import datetime, timezone

        source = hm.enqueue_message(
            db, owner_id=admin_user.id, conversation_id="c", message_id="m",
            role="user", content="用户偏好深色主题",
            occurred_at=datetime.now(timezone.utc),
        )
        source.status = "synced"
        source.document_id = source.id
        db.commit()

        class FakeClient:
            def recall(self, bank_id, payload):
                return {"results": [{
                    "id": "f1", "text": "用户偏好深色主题", "type": "world",
                    "document_id": source.id,
                }]}

        monkeypatch.setattr(hm, "make_client", lambda timeout=None: FakeClient())
        response = client.post(
            f"{BASE}/search", json={"query": "偏好"}, headers=auth_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "ready"
        assert body["items"][0]["source_ids"] == [source.id]
