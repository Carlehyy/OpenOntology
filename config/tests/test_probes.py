from __future__ import annotations

from app.models import ConfigProfile, default_profile
from app.probes import (
    PROBES,
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






def test_unknown_probe_fails_without_network() -> None:
    result = run_probe("missing", _profile_with_secrets())

    assert result.ok is False
    assert result.duration_ms == 0
