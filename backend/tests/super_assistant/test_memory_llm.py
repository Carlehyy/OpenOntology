"""Hindsight 记忆网关（memory_llm）专项测试。

覆盖：机器鉴权（token compare_digest/空值禁用）、请求 schema（extra=forbid
不默默丢字段）、alias→平台模型解析（fail-closed 不回退）、usage/finish_reason
透传（截断不得伪装 stop）、并发闸门与脱敏错误，以及 validate_memory_model
能力探针。LLM 网关一律打桩（memory_llm.gateway_chat），不发真实付费请求；
网关自身的结构化透传在 tests/model_configs/ 单独覆盖。
"""
from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm.session import Session

from app.deps import get_db
from app.model_configs.llm_gateway import LLMError
from app.model_configs.models import ModelConfig
from app.services.auth_service import hash_password
from app.auth.models import User
from app.super_assistant import memory_llm
from app.super_assistant.memory_models import SuperAssistantMemorySetting

URL = "/api/v2/super-assistant/internal/memory/v1/chat/completions"
SERVICE_TOKEN = "unit-memory-service-token"
ALIAS = memory_llm.MEMORY_MODEL_ALIAS  # 固定别名 oo-memory-default（compose 值）


# ---------------------------------------------------------------------------
# 挂载契约：main 以 include_router(memory_llm.router,
# prefix="/api/v2/super-assistant") 挂载，router 自带 /internal/memory/v1。
# ---------------------------------------------------------------------------

@pytest.fixture
def memory_client(db: Session) -> TestClient:
    test_app = FastAPI()
    test_app.include_router(memory_llm.router, prefix="/api/v2/super-assistant")
    test_app.dependency_overrides[get_db] = lambda: (yield db)
    with TestClient(test_app) as client:
        yield client


@pytest.fixture(autouse=True)
def _configured_service_token(monkeypatch):
    monkeypatch.setattr(memory_llm.settings, "hindsight_service_token", SERVICE_TOKEN)


# ---------------------------------------------------------------------------
# 数据构造
# ---------------------------------------------------------------------------

def _user(db: Session, username: str = "mem-owner") -> User:
    user = User(id=username, username=username, email=f"{username}@test.com",
                password_hash=hash_password("pw-123456"), role="admin")
    db.add(user)
    db.commit()
    return user


def _model_config(db: Session, **overrides) -> ModelConfig:
    values = dict(
        name="记忆模型",
        config_type="llm",
        provider="compatible",
        api_base="https://upstream.example/v1",
        api_key_encrypted=None,
        models=["deepseek-chat"],
        options={},
        enabled=True,
        created_by="mem-owner",
    )
    values.update(overrides)
    config = ModelConfig(**values)
    db.add(config)
    db.commit()
    return config


def _memory_setting(db: Session, owner_id: str, model_config_id: str | None,
                    enabled: bool = True) -> SuperAssistantMemorySetting:
    setting = SuperAssistantMemorySetting(
        owner_id=owner_id, enabled=enabled, model_config_id=model_config_id)
    db.add(setting)
    db.commit()
    return setting


def _armed(db: Session, *, provider: str = "compatible", **config_overrides):
    """owner + 启用的记忆设置 + 模型配置 三件套。"""
    owner = _user(db)
    config = _model_config(db, provider=provider, **config_overrides)
    _memory_setting(db, owner.id, config.id)
    return owner, config


def _body(model: str, **overrides) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "提取记忆要点"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "strict": True,
                "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            },
        },
        "max_tokens": 512,
        "temperature": 0.1,
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not None or k == "model"}


def _ok_result(content='{"ok": true}', finish_reason="stop",
               prompt=11, completion=7, tool_calls=None) -> dict:
    return {
        "content": content,
        "tool_calls": tool_calls or [],
        "usage": {"inputTokens": prompt, "outputTokens": completion},
        "finish_reason": finish_reason,
    }


class _GatewayStub:
    """记录调用参数的 gateway_chat 桩；按队列依次返回/抛出。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, call_kwargs, messages, tools, **kwargs):
        self.calls.append({"call_kwargs": call_kwargs, "messages": messages,
                           "tools": tools, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# ---------------------------------------------------------------------------
# 挂载契约
# ---------------------------------------------------------------------------

def test_router_prefix_contract():
    """router 自带 /internal/memory/v1 前缀，main 只叠加业务前缀。"""
    assert memory_llm.router.prefix == "/internal/memory/v1"
    paths = {getattr(route, "path", "") for route in memory_llm.router.routes}
    assert "/internal/memory/v1/chat/completions" in paths


# ---------------------------------------------------------------------------
# 机器鉴权
# ---------------------------------------------------------------------------

def test_auth_missing_token_rejected(memory_client, db):
    owner, _ = _armed(db)
    response = memory_client.post(URL, json=_body(ALIAS))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthorized"


def test_auth_wrong_token_rejected(memory_client, db):
    owner, _ = _armed(db)
    response = memory_client.post(
        URL, json=_body(ALIAS),
        headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_auth_empty_bearer_rejected(memory_client, db):
    owner, _ = _armed(db)
    response = memory_client.post(
        URL, json=_body(ALIAS),
        headers={"Authorization": "Bearer "})
    assert response.status_code == 401


def test_auth_service_disabled_when_token_unconfigured(memory_client, db, monkeypatch):
    """空配置 = 服务禁用（503），杜绝空 token 与空配置意外相等。"""
    monkeypatch.setattr(memory_llm.settings, "hindsight_service_token", "")
    response = memory_client.post(
        URL, json=_body("oo-memory-anyone"),
        headers={"Authorization": "Bearer "})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_disabled"


def test_auth_valid_token_passes(memory_client, db):
    owner, _ = _armed(db)
    stub = _GatewayStub([_ok_result()])
    with patch.object(memory_llm, "gateway_chat", stub):
        response = memory_client.post(
            URL, json=_body(ALIAS),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 请求 schema：extra=forbid / 显式拒绝不支持，不默默丢字段
# ---------------------------------------------------------------------------

def test_unknown_top_level_field_rejected(memory_client, db):
    owner, _ = _armed(db)
    for extra in ({"tools": []}, {"n": 1}, {"frequency_penalty": 0.5},
                  {"user": "bank-1"}, {"model_config_id": "intruder"},
                  {"base_url": "https://evil.example"}, {"api_key": "sk-ev"}):
        response = memory_client.post(
            URL, json={**_body(ALIAS), **extra},
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
        assert response.status_code == 422, extra


def test_stream_true_explicitly_rejected(memory_client, db):
    owner, _ = _armed(db)
    response = memory_client.post(
        URL, json=_body(ALIAS, stream=True),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 400
    assert "stream" in response.json()["error"]["message"]


def test_both_max_token_fields_rejected(memory_client, db):
    owner, _ = _armed(db)
    response = memory_client.post(
        URL, json=_body(ALIAS, max_completion_tokens=64),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 400


def test_response_format_unsupported_type_rejected(memory_client, db):
    owner, _ = _armed(db)
    response = memory_client.post(
        URL, json=_body(ALIAS,
                        response_format={"type": "text"}),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 400


def test_response_format_json_object_extra_field_rejected(memory_client, db):
    owner, _ = _armed(db)
    response = memory_client.post(
        URL, json=_body(ALIAS,
                        response_format={"type": "json_object", "schema": {}}),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 400


def test_response_format_json_schema_missing_schema_rejected(memory_client, db):
    owner, _ = _armed(db)
    response = memory_client.post(
        URL, json=_body(ALIAS,
                        response_format={"type": "json_schema",
                                         "json_schema": {"name": "response"}}),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 400


def test_empty_messages_rejected(memory_client, db):
    owner, _ = _armed(db)
    response = memory_client.post(
        URL, json=_body(ALIAS, messages=[]),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 422


def test_oversized_content_rejected(memory_client, db):
    owner, _ = _armed(db)
    response = memory_client.post(
        URL, json=_body(
            ALIAS,
            messages=[{"role": "user", "content": "x" * (memory_llm._MAX_TOTAL_CONTENT_CHARS + 1)}]),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 400


def test_oversized_response_format_counts_toward_limit(memory_client, db):
    """schema 可能巨大：response_format 与 messages 合计受限，不许绕过闸门。"""
    owner, _ = _armed(db)
    response = memory_client.post(
        URL, json=_body(ALIAS, response_format={
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": {
                "x": "y" * memory_llm._MAX_TOTAL_CONTENT_CHARS}},
        }),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# alias → 平台模型解析（fail-closed，不回退默认模型）
# ---------------------------------------------------------------------------

def _resolve_failures(memory_client, db, alias, stub=None):
    stub = stub or _GatewayStub([_ok_result()])
    with patch.object(memory_llm, "gateway_chat", stub):
        response = memory_client.post(
            URL, json=_body(alias),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    return response, stub


def test_non_default_alias_rejected(memory_client, db):
    """仅接受固定别名 oo-memory-default；动态 owner 别名/任意模型名一律 404。"""
    owner, _ = _armed(db)
    for alias in ("gpt-4o", "oo-memory", "oo-memory-alice", "oo-memory-default-x"):
        response, stub = _resolve_failures(memory_client, db, alias)
        assert response.status_code == 404, alias
        assert response.json()["error"]["code"] == "model_not_found"
        assert stub.calls == []


def test_no_eligible_setting_returns_503_unconfigured(memory_client, db):
    """无任何 enabled+已选型设置：503 配置缺失，不任意选、不回退。"""
    _user(db)  # 用户存在但无记忆设置
    response, stub = _resolve_failures(memory_client, db, ALIAS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "memory_model_unconfigured"
    assert stub.calls == []


def test_disabled_or_unselected_settings_not_eligible(memory_client, db):
    """停用 / 未选型的设置不算候选：仍按配置缺失 503 拒绝。"""
    disabled_owner = _user(db, "owner-disabled")
    unselected_owner = _user(db, "owner-unselected")
    config = _model_config(db)
    _memory_setting(db, disabled_owner.id, config.id, enabled=False)
    _memory_setting(db, unselected_owner.id, None)
    response, stub = _resolve_failures(memory_client, db, ALIAS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "memory_model_unconfigured"
    assert stub.calls == []


def test_multiple_eligible_settings_rejected_as_ambiguous(memory_client, db):
    """多条 enabled+已选型设置：503 歧义，绝不任意挑一条。"""
    owner_a = _user(db, "owner-a")
    owner_b = _user(db, "owner-b")
    _memory_setting(db, owner_a.id, _model_config(db, name="模型A").id)
    _memory_setting(db, owner_b.id, _model_config(db, name="模型B").id)
    response, stub = _resolve_failures(memory_client, db, ALIAS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "memory_model_ambiguous"
    assert stub.calls == []


def test_setting_owner_missing_returns_503(memory_client, db):
    """唯一候选设置的 owner 用户不存在：503 配置完整性异常。"""
    config = _model_config(db)
    db.add(SuperAssistantMemorySetting(owner_id="ghost-user", enabled=True,
                                       model_config_id=config.id))
    db.commit()
    response, stub = _resolve_failures(memory_client, db, ALIAS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "memory_owner_missing"
    assert stub.calls == []


def test_missing_model_config_row_rejected(memory_client, db):
    owner = _user(db)
    _memory_setting(db, owner.id, "nonexistent-config")
    response, stub = _resolve_failures(memory_client, db, ALIAS)
    assert response.status_code == 404
    assert stub.calls == []


def test_disabled_model_config_rejected(memory_client, db):
    owner = _user(db)
    config = _model_config(db, enabled=False)
    _memory_setting(db, owner.id, config.id)
    response, stub = _resolve_failures(memory_client, db, ALIAS)
    assert response.status_code == 404
    assert stub.calls == []


def test_anthropic_provider_explicitly_rejected(memory_client, db):
    owner, _ = _armed(db, provider="anthropic")
    response, stub = _resolve_failures(memory_client, db, ALIAS)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "provider_unsupported"
    assert stub.calls == []


def test_model_loop_back_to_memory_endpoint_rejected(memory_client, db):
    owner, _ = _armed(
        db, api_base="http://backend:8000/api/v2/super-assistant/internal/memory/v1")
    response, stub = _resolve_failures(memory_client, db, ALIAS)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_loop_forbidden"
    assert stub.calls == []


def test_undecryptable_api_key_errors_without_fallback(memory_client, db):
    owner = _user(db)
    config = _model_config(db, api_key_encrypted="not-a-fernet-token")
    _memory_setting(db, owner.id, config.id)
    response, stub = _resolve_failures(memory_client, db, ALIAS)
    assert response.status_code == 500
    assert "API Key" in response.json()["error"]["message"]
    assert stub.calls == []


# ---------------------------------------------------------------------------
# 成功路径：结构化参数透传 + 真实 OpenAI 响应形态
# ---------------------------------------------------------------------------

def test_success_response_shape_and_gateway_passthrough(memory_client, db):
    owner, config = _armed(db)
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "response",
            "strict": True,
            "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        },
    }
    stub = _GatewayStub([_ok_result()])
    with patch.object(memory_llm, "gateway_chat", stub):
        response = memory_client.post(
            URL,
            json=_body(ALIAS, response_format=response_format,
                       seed=4242, top_p=0.9),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})

    assert response.status_code == 200
    call = stub.calls[0]
    # 模型解析自平台配置（服务端选择），请求方无法携带 base_url/api_key
    assert call["call_kwargs"]["model"] == "deepseek-chat"
    assert call["call_kwargs"]["api_base"] == "https://upstream.example/v1"
    assert call["call_kwargs"]["model_config_id"] == config.id
    assert call["call_kwargs"]["timeout_seconds"] == memory_llm._CALL_TIMEOUT_SECONDS
    # 网关层单次尝试：Hindsight 侧自持重试预算，避免重试相乘
    assert call["call_kwargs"]["retry_max_attempts"] == 1
    # Hindsight 请求字段原样透传，不默默丢弃
    assert call["response_format"] == response_format
    assert call["temperature"] == 0.1
    assert call["max_tokens"] == 512
    assert call["seed"] == 4242
    assert call["top_p"] == 0.9
    assert call["return_finish_reason"] is True
    assert call["messages"] == [{"role": "user", "content": "提取记忆要点"}]

    body = response.json()
    assert body["id"].startswith("chatcmpl-mem-")
    assert body["object"] == "chat.completion"
    assert isinstance(body["created"], int)
    assert body["model"] == ALIAS  # 回显 alias
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == '{"ok": true}'
    assert choice["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 11, "completion_tokens": 7,
                             "total_tokens": 18}


def test_json_object_response_format_passthrough(memory_client, db):
    owner, _ = _armed(db)
    stub = _GatewayStub([_ok_result()])
    with patch.object(memory_llm, "gateway_chat", stub):
        response = memory_client.post(
            URL,
            json=_body(ALIAS, response_format={"type": "json_object"}),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 200
    assert stub.calls[0]["response_format"] == {"type": "json_object"}


def test_truncation_finish_reason_never_faked_as_stop(memory_client, db):
    """上游 length（截断）必须原样透传，Hindsight 侧据此拆分输入。"""
    owner, _ = _armed(db)
    stub = _GatewayStub([_ok_result(content='{"ok": tr', finish_reason="length")])
    with patch.object(memory_llm, "gateway_chat", stub):
        response = memory_client.post(
            URL, json=_body(ALIAS),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == "length"


def test_missing_finish_reason_defaults_to_stop(memory_client, db):
    owner, _ = _armed(db)
    result = _ok_result()
    result["finish_reason"] = None
    stub = _GatewayStub([result])
    with patch.object(memory_llm, "gateway_chat", stub):
        response = memory_client.post(
            URL, json=_body(ALIAS),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.json()["choices"][0]["finish_reason"] == "stop"


def test_usage_missing_returns_explicit_error_not_fake_zeros(memory_client, db):
    """运行期 usage 缺失是上游异常：显式 502，不伪造成零消耗账目。"""
    owner, _ = _armed(db)
    result = _ok_result()
    result["usage"] = None
    stub = _GatewayStub([result])
    with patch.object(memory_llm, "gateway_chat", stub):
        response = memory_client.post(
            URL, json=_body(ALIAS),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "usage_missing"


def test_usage_non_int_returns_explicit_error(memory_client, db):
    owner, _ = _armed(db)
    result = _ok_result()
    result["usage"] = {"inputTokens": None, "outputTokens": 3}
    stub = _GatewayStub([result])
    with patch.object(memory_llm, "gateway_chat", stub):
        response = memory_client.post(
            URL, json=_body(ALIAS),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "usage_missing"


def test_tool_calls_serialized_openai_style(memory_client, db):
    owner, _ = _armed(db)
    stub = _GatewayStub([_ok_result(tool_calls=[
        {"id": "call_1", "name": "probe", "arguments": {"q": "x"}}])])
    with patch.object(memory_llm, "gateway_chat", stub):
        response = memory_client.post(
            URL, json=_body(ALIAS),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    message = response.json()["choices"][0]["message"]
    assert message["tool_calls"] == [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "probe", "arguments": '{"q": "x"}'},
    }]


# ---------------------------------------------------------------------------
# 上游错误：脱敏 + 502
# ---------------------------------------------------------------------------

def test_llm_error_sanitized_and_returned_as_502(memory_client, db):
    owner, _ = _armed(db)
    stub = _GatewayStub([LLMError(
        "upstream failed bearer sk-abcdefghijklmnop1234567890 timeout")])
    with patch.object(memory_llm, "gateway_chat", stub):
        response = memory_client.post(
            URL, json=_body(ALIAS),
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 502
    message = response.json()["error"]["message"]
    assert "sk-abcdefghijklmnop1234567890" not in message
    assert "***" in message


# ---------------------------------------------------------------------------
# 并发闸门
# ---------------------------------------------------------------------------

def test_concurrency_limit_fast_rejection(memory_client, db, monkeypatch):
    owner, _ = _armed(db)
    monkeypatch.setattr(memory_llm, "_CALL_SEMAPHORE",
                        threading.BoundedSemaphore(0))
    response = memory_client.post(
        URL, json=_body(ALIAS),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "concurrency_limit"


# ---------------------------------------------------------------------------
# validate_memory_model 能力探针
# ---------------------------------------------------------------------------

def test_validate_memory_model_ok(db):
    config = _model_config(db)
    stub = _GatewayStub([_ok_result(prompt=9, completion=3)])
    with patch.object(memory_llm, "gateway_chat", stub):
        result = memory_llm.validate_memory_model(db, config.id)
    assert result["ok"] is True
    assert result["usage"] == {"prompt_tokens": 9, "completion_tokens": 3,
                               "total_tokens": 12}
    call = stub.calls[0]
    assert call["response_format"]["type"] == "json_schema"
    assert call["max_tokens"] == memory_llm._PROBE_MAX_TOKENS
    # 探针预算必须 ≥512：推理模型把思维链计入 max_tokens（glm-5.3 实测
    # reasoning ~112 token），64 会令样例 finish_reason=length 截断误判。
    assert memory_llm._PROBE_MAX_TOKENS >= 512
    assert call["call_kwargs"]["timeout_seconds"] == memory_llm._PROBE_TIMEOUT_SECONDS
    # 探针同样受网关单次尝试与并发闸门约束
    assert call["call_kwargs"]["retry_max_attempts"] == 1


def test_validate_memory_model_missing_config(db):
    result = memory_llm.validate_memory_model(db, "nonexistent")
    assert result["ok"] is False
    assert result["code"] == "MODEL_NOT_FOUND"


def test_validate_memory_model_disabled_config(db):
    config = _model_config(db, enabled=False)
    result = memory_llm.validate_memory_model(db, config.id)
    assert result["ok"] is False
    assert result["code"] == "MODEL_NOT_FOUND"


def test_validate_memory_model_provider_unsupported(db):
    config = _model_config(db, provider="anthropic")
    stub = _GatewayStub([_ok_result()])
    with patch.object(memory_llm, "gateway_chat", stub):
        result = memory_llm.validate_memory_model(db, config.id)
    assert result["ok"] is False
    assert result["code"] == "PROVIDER_UNSUPPORTED"
    assert stub.calls == []


def test_validate_memory_model_loop_back_forbidden(db):
    config = _model_config(db, api_base="http://backend:8000/internal/memory/v1")
    result = memory_llm.validate_memory_model(db, config.id)
    assert result["ok"] is False
    assert result["code"] == "MODEL_LOOP_FORBIDDEN"


def test_validate_memory_model_undecryptable_key(db):
    config = _model_config(db, api_key_encrypted="not-a-fernet-token")
    result = memory_llm.validate_memory_model(db, config.id)
    assert result["ok"] is False
    assert result["code"] == "KEY_DECRYPT_FAILED"
    assert "API Key" in result["message"]


def test_validate_memory_model_structured_output_rejected(db):
    config = _model_config(db)
    stub = _GatewayStub([_ok_result(content="prose, not json")])
    with patch.object(memory_llm, "gateway_chat", stub):
        result = memory_llm.validate_memory_model(db, config.id)
    assert result["ok"] is False
    assert result["code"] == "STRUCTURED_OUTPUT_UNSUPPORTED"


@pytest.mark.parametrize("content", [
    '{"x": 1}',                                  # 合法 JSON 但不合 schema
    'null',                                      # JSON 字面量非对象
    '{"ok": true, "extra": 1}',                  # 带额外键
    '{"ok": false}',                             # ok 不是 true
    '{"ok": "true"}',                            # ok 不是布尔
])
def test_validate_memory_model_requires_exact_probe_shape(db, content):
    """探针样例必须精确返回 {"ok": true}：合 JSON 但不合约束不算能力通过。"""
    config = _model_config(db)
    stub = _GatewayStub([_ok_result(content=content)])
    with patch.object(memory_llm, "gateway_chat", stub):
        result = memory_llm.validate_memory_model(db, config.id)
    assert result["ok"] is False
    assert result["code"] == "STRUCTURED_OUTPUT_INVALID"


def test_validate_memory_model_truncated_probe_fails(db):
    """探测样例被截断（finish_reason=length）不算能力通过。"""
    config = _model_config(db)
    stub = _GatewayStub([_ok_result(content='{"ok": tr', finish_reason="length")])
    with patch.object(memory_llm, "gateway_chat", stub):
        result = memory_llm.validate_memory_model(db, config.id)
    assert result["ok"] is False
    assert result["code"] == "STRUCTURED_OUTPUT_TRUNCATED"


def test_validate_memory_model_shares_concurrency_gate(db, monkeypatch):
    """探针不绕过并发闸门：占满时快速失败且不触达网关。"""
    config = _model_config(db)
    monkeypatch.setattr(memory_llm, "_CALL_SEMAPHORE",
                        threading.BoundedSemaphore(0))
    stub = _GatewayStub([_ok_result()])
    with patch.object(memory_llm, "gateway_chat", stub):
        result = memory_llm.validate_memory_model(db, config.id)
    assert result["ok"] is False
    assert result["code"] == "CONCURRENCY_LIMIT"
    assert stub.calls == []


def test_validate_memory_model_empty_content(db):
    config = _model_config(db)
    stub = _GatewayStub([_ok_result(content="")])
    with patch.object(memory_llm, "gateway_chat", stub):
        result = memory_llm.validate_memory_model(db, config.id)
    assert result["ok"] is False
    assert result["code"] == "EMPTY_CONTENT"


def test_validate_memory_model_usage_missing(db):
    config = _model_config(db)
    result = _ok_result()
    result["usage"] = None
    stub = _GatewayStub([result])
    with patch.object(memory_llm, "gateway_chat", stub):
        outcome = memory_llm.validate_memory_model(db, config.id)
    assert outcome["ok"] is False
    assert outcome["code"] == "USAGE_MISSING"


def test_validate_memory_model_upstream_rejects_response_format(db):
    config = _model_config(db)
    stub = _GatewayStub([LLMError(
        "HTTP 400: response_format type json_schema is unavailable now")])
    with patch.object(memory_llm, "gateway_chat", stub):
        result = memory_llm.validate_memory_model(db, config.id)
    assert result["ok"] is False
    assert result["code"] == "STRUCTURED_OUTPUT_UNSUPPORTED"


def test_validate_memory_model_generic_error_mapped(db):
    config = _model_config(db)
    stub = _GatewayStub([LLMError("connection timed out after 30s")])
    with patch.object(memory_llm, "gateway_chat", stub):
        result = memory_llm.validate_memory_model(db, config.id)
    assert result["ok"] is False
    assert result["code"] == "TIMEOUT"
