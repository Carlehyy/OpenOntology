from __future__ import annotations

from app.models import ConfigProfile, default_profile
from app.probes import (
    PROBES,
    _validate_weknora_state,
    probe_nats,
    run_probe,
)


def _profile_with_secrets() -> ConfigProfile:
    payload = default_profile().model_dump()
    payload["postgres"]["password"] = "postgres-super-secret"
    payload["redis"]["password"] = "redis-super-secret"
    payload["nats"]["token"] = "nats-super-secret"
    payload["neo4j"]["password"] = "neo4j-super-secret"
    payload["minio"]["access_key"] = "minio-secret-access"
    payload["minio"]["secret_key"] = "minio-super-secret"
    payload["n8n"]["api_key"] = "n8n-super-secret"
    payload["weknora"]["chat_api_key"] = "weknora-chat-super-secret"
    payload["weknora"]["embedding_api_key"] = "weknora-embedding-super-secret"
    return ConfigProfile.model_validate(payload)


def test_probe_errors_redact_every_known_secret(monkeypatch) -> None:
    profile = _profile_with_secrets()

    def fail(current: ConfigProfile):
        raise RuntimeError(
            "bad "
            + " ".join(
                [
                    current.postgres.password,
                    current.redis.password,
                    current.nats.token,
                    current.neo4j.password,
                    current.minio.access_key,
                    current.minio.secret_key,
                    current.n8n.api_key,
                ]
            )
        )

    monkeypatch.setitem(PROBES, "postgres", fail)
    result = run_probe("postgres", profile)

    assert result.ok is False
    assert "***" in result.detail
    assert "super-secret" not in result.detail
    assert profile.minio.access_key not in result.detail


def test_nats_probe_is_registered_as_available_service() -> None:
    assert PROBES["nats"] is probe_nats


def _weknora_state() -> dict:
    return {
        "version": 1,
        "verified": True,
        "base_url": "http://weknora-app:8080",
        "mcp_endpoint_id": "endpoint-1",
        "mcp_token": "mcp_" + "a" * 24,
        "knowledge_base_id": "kb-1",
        "knowledge_base_name": "共享知识库",
        "tools": [
            {
                "name": name,
                "description": f"{name} description",
                "input_schema": {"type": "object"},
            }
            for name in (
                "list_knowledge_bases",
                "search_knowledge",
                "read_document",
            )
        ],
    }


def test_weknora_state_requires_verified_exact_origin_and_tool_schema() -> None:
    state = _weknora_state()
    assert _validate_weknora_state(state, "http://weknora-app:8080") == state
    assert _validate_weknora_state(state, "http://WEKNORA-APP:8080/") == state

    for mutation, message in (
        (lambda value: value.update(verified=False), "verified"),
        (
            lambda value: value.update(base_url="http://127.0.0.1:18080"),
            "origin",
        ),
        (lambda value: value.update(mcp_token="bad-token"), "mcp_token"),
        (lambda value: value.update(tools=[]), "工具清单"),
        (
            lambda value: value["tools"].append(
                {
                    "name": "ask",
                    "description": "write-capable",
                    "input_schema": {"type": "object"},
                }
            ),
            "白名单",
        ),
        (
            lambda value: value["tools"].pop(),
            "必需只读工具",
        ),
        (
            lambda value: value["tools"][0].pop("input_schema"),
            "input_schema",
        ),
    ):
        broken = _weknora_state()
        mutation(broken)
        try:
            _validate_weknora_state(broken, "http://weknora-app:8080")
        except RuntimeError as exc:
            assert message in str(exc)
        else:
            raise AssertionError(f"WeKnora state mutation must fail: {message}")


def test_unknown_probe_fails_without_network() -> None:
    result = run_probe("missing", _profile_with_secrets())

    assert result.ok is False
    assert result.duration_ms == 0
