from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.probes as probes
from app.models import ConfigProfile, default_profile


def _profile() -> ConfigProfile:
    payload = default_profile().model_dump()
    payload["postgres"]["password"] = "postgres-password"
    payload["redis"]["password"] = "redis-password"
    payload["neo4j"]["password"] = "neo4j-password"
    payload["minio"]["access_key"] = "minio-access"
    payload["minio"]["secret_key"] = "minio-password"
    payload["n8n"]["api_key"] = "n8n-key"
    return ConfigProfile.model_validate(payload)


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_postgres_probe_runs_select_one_and_closes(monkeypatch) -> None:
    events: list[str] = []
    connection_options = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql):
            events.append(sql)

        def fetchone(self):
            return (1,)

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            events.append("closed")

    monkeypatch.setattr(
        probes.psycopg2,
        "connect",
        lambda **kwargs: (
            connection_options.update(kwargs)
            or events.append(f"{kwargs['host']}:{kwargs['port']}")
            or Connection()
        ),
    )

    message, _ = probes.probe_postgres(_profile())

    assert message == "PostgreSQL 连接正常"
    assert events == ["127.0.0.1:5432", "SELECT 1", "closed"]
    assert connection_options["connect_timeout"] == 6
    assert "statement_timeout=6000" in connection_options["options"]


def test_redis_probe_authenticates_pings_and_closes(monkeypatch) -> None:
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def ping(self):
            return True

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(probes.redis_client, "Redis", Client)
    message, _ = probes.probe_redis(_profile())

    assert message == "Redis 连接正常"
    assert seen["password"] == "redis-password"
    assert seen["closed"] is True


class FakeNatsConnection:
    """按 NATS 协议脚本化应答的假 socket，recv 逐段返回预设字节。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.sent = bytearray()
        self.timeout = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        self.sent.extend(data)

    def recv(self, _size):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _install_fake_nats(monkeypatch, chunks) -> dict:
    seen = {}

    def fake_create_connection(address, timeout=None):
        seen["address"] = address
        seen["timeout"] = timeout
        seen["connection"] = FakeNatsConnection(chunks)
        return seen["connection"]

    monkeypatch.setattr(probes.socket, "create_connection", fake_create_connection)
    return seen


def test_nats_probe_pings_jetstream_without_authentication(monkeypatch) -> None:
    seen = _install_fake_nats(
        monkeypatch,
        [b'INFO {"server_id":"NDTEST","jetstream":true}\r\n', b"PONG\r\n"],
    )

    message, detail = probes.probe_nats(_profile())

    assert message == "NATS 连接正常"
    assert "JetStream 已启用" in detail
    assert seen["address"] == ("localhost", 4222)
    assert seen["timeout"] == 3
    assert seen["connection"].timeout == 3
    assert bytes(seen["connection"].sent) == b'CONNECT {"verbose":false}\r\nPING\r\n'


def test_nats_probe_sends_configured_auth_token(monkeypatch) -> None:
    payload = default_profile().model_dump()
    payload["nats"]["token"] = "nats-secret-token"
    profile = ConfigProfile.model_validate(payload)
    seen = _install_fake_nats(
        monkeypatch,
        [b'INFO {"jetstream":true}\r\n', b"PONG\r\n"],
    )

    message, _ = probes.probe_nats(profile)

    assert message == "NATS 连接正常"
    assert bytes(seen["connection"].sent) == (
        b'CONNECT {"verbose":false,"auth_token":"nats-secret-token"}\r\nPING\r\n'
    )


def test_nats_probe_answers_auth_required_even_with_blank_token(monkeypatch) -> None:
    seen = _install_fake_nats(
        monkeypatch,
        [b'INFO {"jetstream":true,"auth_required":true}\r\n', b"PONG\r\n"],
    )

    message, _ = probes.probe_nats(_profile())

    assert message == "NATS 连接正常"
    assert b'"auth_token":""' in bytes(seen["connection"].sent)


def test_nats_probe_requires_jetstream(monkeypatch) -> None:
    _install_fake_nats(
        monkeypatch,
        [b'INFO {"server_id":"NDTEST","jetstream":false}\r\n'],
    )

    with pytest.raises(RuntimeError, match="-js/--jetstream"):
        probes.probe_nats(_profile())


def test_nats_probe_rejects_non_nats_greeting(monkeypatch) -> None:
    _install_fake_nats(monkeypatch, [b"HELLO\r\n"])

    with pytest.raises(RuntimeError, match="INFO"):
        probes.probe_nats(_profile())


def test_nats_probe_rejects_non_pong_reply(monkeypatch) -> None:
    _install_fake_nats(
        monkeypatch,
        [b'INFO {"jetstream":true}\r\n', b"-ERR 'Authorization Violation'\r\n"],
    )

    with pytest.raises(RuntimeError, match="PONG"):
        probes.probe_nats(_profile())


def test_neo4j_probe_verifies_connectivity_and_query(monkeypatch) -> None:
    events: list[str] = []

    class Result:
        def single(self):
            return {"ok": 1}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def run(self, query):
            events.append(str(query))
            assert query.timeout == 6
            return Result()

    class Driver:
        def verify_connectivity(self):
            events.append("verified")

        def session(self):
            return Session()

        def close(self):
            events.append("closed")

    monkeypatch.setattr(
        probes.GraphDatabase,
        "driver",
        lambda *_args, **_kwargs: Driver(),
    )
    message, _ = probes.probe_neo4j(_profile())

    assert message == "Neo4j 连接正常"
    assert events == ["verified", "RETURN 1 AS ok", "closed"]


def test_minio_probe_is_read_only_and_reports_bucket(monkeypatch) -> None:
    seen = {}

    class Client:
        def __init__(self, endpoint, **kwargs):
            seen["endpoint"] = endpoint
            seen.update(kwargs)

        def list_buckets(self):
            return [SimpleNamespace(name="openontology")]

    monkeypatch.setattr(probes, "Minio", Client)
    message, detail = probes.probe_minio(_profile())

    assert message == "MinIO 连接正常"
    assert "当前可见 1 个桶" in detail
    assert seen["endpoint"] == "localhost:9000"
    assert seen["secret_key"] == "minio-password"


def test_browser_probe_requires_websocket_debugger_url(monkeypatch) -> None:
    profile = _profile()
    profile.browser.cdp_url = "http://127.0.0.1:9222/"

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url):
            assert url == "http://127.0.0.1:9222/json/version"
            return FakeResponse(
                {
                    "Browser": "Chrome/140",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/id",
                }
            )

    monkeypatch.setattr(probes.httpx, "Client", Client)
    message, detail = probes.probe_browser(profile)

    assert message == "浏览器控制接口正常"
    assert detail == "已识别 Chrome/140"


def test_n8n_probe_uses_api_key_header(monkeypatch) -> None:
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, *, params, headers):
            seen["url"] = url
            seen["params"] = params
            seen["headers"] = headers
            return FakeResponse({"data": []})

    monkeypatch.setattr(probes.httpx, "Client", Client)
    message, _ = probes.probe_n8n(_profile())

    assert message == "n8n 连接正常"
    assert seen["url"].endswith("/api/v1/workflows")
    assert seen["params"] == {"limit": 1}
    assert seen["headers"]["X-N8N-API-KEY"] == "n8n-key"
    assert seen["client_kwargs"]["mounts"]["all://127.0.0.1"] is None
    assert "trust_env" not in seen["client_kwargs"]


def _install_fake_kaneo(monkeypatch, responses) -> dict:
    seen: dict = {}

    class Client:
        def __init__(self, **kwargs):
            seen["client_kwargs"] = kwargs
            seen["urls"] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url):
            seen["urls"].append(url)
            return responses.pop(0)

    monkeypatch.setattr(probes.httpx, "Client", Client)
    return seen


def test_kaneo_probe_requires_health_ok_and_initialized_instance(
    monkeypatch,
) -> None:
    seen = _install_fake_kaneo(
        monkeypatch,
        [
            FakeResponse({"status": "ok"}),
            FakeResponse({"hasUsers": True, "hasAdmin": True}),
        ],
    )

    message, detail = probes.probe_kaneo(_profile())

    assert message == "Kaneo 服务连接正常"
    assert seen["urls"] == [
        "http://127.0.0.1:18090/api/health",
        "http://127.0.0.1:18090/api/instance/status",
    ]
    assert seen["client_kwargs"]["timeout"] == 15
    assert seen["client_kwargs"]["follow_redirects"] is False
    assert seen["client_kwargs"]["mounts"]["all://127.0.0.1"] is None
    assert "已创建" in detail


def test_kaneo_probe_rejects_health_without_status_ok(monkeypatch) -> None:
    _install_fake_kaneo(
        monkeypatch,
        [
            FakeResponse({"status": "degraded"}),
            FakeResponse({"hasUsers": True, "hasAdmin": True}),
        ],
    )

    with pytest.raises(RuntimeError, match="status=ok"):
        probes.probe_kaneo(_profile())


def test_kaneo_probe_rejects_bare_liveness_without_initialized_account(
    monkeypatch,
) -> None:
    _install_fake_kaneo(
        monkeypatch,
        [
            FakeResponse({"status": "ok"}),
            FakeResponse({"hasUsers": False, "hasAdmin": False}),
        ],
    )

    with pytest.raises(RuntimeError, match="单用户初始化"):
        probes.probe_kaneo(_profile())


def test_kaneo_probe_rejects_adminless_instance(monkeypatch) -> None:
    _install_fake_kaneo(
        monkeypatch,
        [
            FakeResponse({"status": "ok"}),
            FakeResponse({"hasUsers": True, "hasAdmin": False}),
        ],
    )

    with pytest.raises(RuntimeError, match="hasAdmin"):
        probes.probe_kaneo(_profile())


def test_kaneo_probe_rejects_redirect_only_gateway(monkeypatch) -> None:
    seen = _install_fake_kaneo(monkeypatch, [FakeResponse({}, status_code=308)])

    with pytest.raises(RuntimeError, match="8090"):
        probes.probe_kaneo(_profile())
    assert seen["urls"] == ["http://127.0.0.1:18090/api/health"]


def test_kaneo_probe_rejects_unknown_instance_status_shape(monkeypatch) -> None:
    _install_fake_kaneo(
        monkeypatch,
        [
            FakeResponse({"status": "ok"}),
            FakeResponse({"registered": True}),
        ],
    )

    with pytest.raises(RuntimeError, match="instance/status"):
        probes.probe_kaneo(_profile())


def test_hindsight_probe_checks_readiness_without_credentials(monkeypatch) -> None:
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url):
            seen["url"] = url
            return FakeResponse({"status": "ok"})

    monkeypatch.setattr(probes.httpx, "Client", Client)
    message, _ = probes.probe_hindsight(_profile())

    assert message == "Hindsight 服务连接正常"
    assert seen["url"] == "http://127.0.0.1:18888/health/ready"
    assert seen["client_kwargs"]["mounts"]["all://127.0.0.1"] is None


def test_hindsight_probe_rejects_unready_payload(monkeypatch) -> None:
    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url):
            return FakeResponse({}, status_code=503)

    monkeypatch.setattr(probes.httpx, "Client", Client)
    with pytest.raises(RuntimeError):
        probes.probe_hindsight(_profile())
