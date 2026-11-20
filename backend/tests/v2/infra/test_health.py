# tests/v2/infra/test_health.py
"""
Infrastructure health-check tests for the /health endpoint.

These tests run against the FastAPI TestClient. Required external services that
are absent in a pure-unit context must make readiness fail closed with 503;
process liveness remains dependency-independent.
"""
import pytest
from fastapi.testclient import TestClient
from app.main import app, _probe_http_service
from app.shared import dependency_probe

client = TestClient(app)

VALID_DB_STATES = ("ok", "error", "unknown")
VALID_SERVICE_STATES = ("ok", "unavailable", "unknown")


def test_liveness_endpoint_is_dependency_independent():
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_endpoint_returns_readiness_status():
    response = client.get("/health")
    assert response.status_code in {200, 503}


def test_health_endpoint_returns_ok():
    response = client.get("/health")
    assert response.status_code in {200, 503}
    data = response.json()
    assert "status" in data
    assert "db" in data


def test_health_db_key_present():
    response = client.get("/health")
    data = response.json()
    assert data["db"] in VALID_DB_STATES


def test_health_all_service_keys_present():
    """Every required dependency and internal runtime key is present."""
    response = client.get("/health")
    data = response.json()
    for key in (
        "db", "redis", "neo4j", "minio", "object_storage",
        "browser", "n8n", "weknora", "kaneo",
        "sentinel_scheduler", "sentinel_cdc", "data_scheduler",
        "ontology_projection",
    ):
        assert key in data, f"Missing key: {key}"


def test_health_service_states_are_valid():
    """Each service reports a known state string."""
    response = client.get("/health")
    data = response.json()
    assert data["db"] in VALID_DB_STATES
    for key in (
        "redis", "neo4j", "minio", "browser", "n8n", "weknora", "kaneo",
        "sentinel_scheduler", "sentinel_cdc", "data_scheduler",
        "ontology_projection",
    ):
        assert data[key] in VALID_SERVICE_STATES, (
            f"{key} has unexpected state: {data[key]}"
        )
    assert data["object_storage"] in ("minio", "unavailable", "unknown")


def test_public_health_does_not_expose_raw_cdc_errors():
    response = client.get("/health")
    detail = response.json()["sentinel_cdc_detail"]

    assert "last_error" not in detail
    assert "last_errors" not in detail
    assert "error" not in detail
    assert "error_code" in detail


def test_minio_outage_fails_object_storage_readiness():
    response = client.get("/health")
    data = response.json()
    if data["minio"] == "unavailable":
        assert data["object_storage"] == "unavailable"
        assert "minio" in data["unavailable"]


def test_minio_readiness_runs_a_fresh_authenticated_probe(monkeypatch):
    calls = []

    def probe_minio():
        calls.append("authenticated")

    monkeypatch.setattr(dependency_probe, "probe_minio", probe_minio)
    monkeypatch.setattr(
        "app.shared.storage.clear_environment_storage_backoff",
        lambda: calls.append("reset-backoff"),
    )

    responses = [client.get("/health/ready"), client.get("/health/ready")]

    assert calls == [
        "authenticated",
        "reset-backoff",
        "authenticated",
        "reset-backoff",
    ]
    assert all(response.json()["minio"] == "ok" for response in responses)
    assert all(
        response.json()["object_storage"] == "minio"
        for response in responses
    )


def test_minio_authenticated_probe_failure_fails_readiness(monkeypatch):
    def reject_credentials():
        raise PermissionError("invalid MinIO credentials")

    monkeypatch.setattr(
        dependency_probe, "probe_minio", reject_credentials,
    )

    response = client.get("/health/ready")
    data = response.json()

    assert response.status_code == 503
    assert data["minio"] == "unavailable"
    assert data["object_storage"] == "unavailable"
    assert "minio" in data["unavailable"]


def test_weknora_probe_failure_fails_readiness_without_leaking_runtime_config(
    monkeypatch,
):
    state_path = "/weknora-state/integration.json"
    mcp_token = "mcp_synthetic_health_token_not_a_secret"
    internal_url = "http://weknora-app:8080"

    def reject_weknora():
        raise RuntimeError(
            f"WEKNORA_MCP_UNAVAILABLE token={mcp_token} "
            f"url={internal_url} state={state_path}"
        )

    monkeypatch.setattr(dependency_probe, "probe_weknora", reject_weknora)

    response = client.get("/health/ready")
    data = response.json()

    assert response.status_code == 503
    assert data["weknora"] == "unavailable"
    assert "weknora" in data["unavailable"]
    for secret in (mcp_token, internal_url, state_path):
        assert secret not in response.text


def test_weknora_probe_success_reports_ok(monkeypatch):
    calls = []

    monkeypatch.setattr(
        dependency_probe, "probe_weknora", lambda: calls.append("probe"),
    )

    response = client.get("/health/ready")
    data = response.json()

    assert calls == ["probe"]
    assert data["weknora"] == "ok"
    assert "weknora" not in data["unavailable"]


def test_kaneo_probe_failure_fails_readiness_without_leaking_runtime_config(
    monkeypatch,
):
    base_url = "http://127.0.0.1:18090"
    bearer_token = "synthetic_kaneo_token_not_a_secret"

    def reject_kaneo():
        raise RuntimeError(
            f"KANEO_DB_UNAVAILABLE token={bearer_token} url={base_url}"
        )

    monkeypatch.setattr(dependency_probe, "probe_kaneo", reject_kaneo)

    response = client.get("/health/ready")
    data = response.json()

    assert response.status_code == 503
    assert data["kaneo"] == "unavailable"
    assert "kaneo" in data["unavailable"]
    for secret in (bearer_token, base_url):
        assert secret not in response.text


def test_kaneo_probe_success_reports_ok(monkeypatch):
    calls = []

    monkeypatch.setattr(
        dependency_probe, "probe_kaneo", lambda: calls.append("probe"),
    )

    response = client.get("/health/ready")
    data = response.json()

    assert calls == ["probe"]
    assert data["kaneo"] == "ok"
    assert "kaneo" not in data["unavailable"]


def test_grafana_probe_failure_fails_readiness_without_leaking_runtime_config(
    monkeypatch,
):
    password = "synthetic_grafana_password_not_a_secret"
    base_url = "http://grafana:3000"

    def reject_grafana():
        raise RuntimeError(f"GRAFANA_DB_UNAVAILABLE password={password} url={base_url}")

    monkeypatch.setattr(dependency_probe, "probe_grafana", reject_grafana)

    response = client.get("/health/ready")
    data = response.json()

    assert response.status_code == 503
    assert data["grafana"] == "unavailable"
    assert "grafana" in data["unavailable"]
    for secret in (password, base_url):
        assert secret not in response.text


def test_grafana_probe_success_reports_ok(monkeypatch):
    calls = []

    monkeypatch.setattr(
        dependency_probe, "probe_grafana", lambda: calls.append("probe"),
    )

    response = client.get("/health/ready")
    data = response.json()

    assert calls == ["probe"]
    assert data["grafana"] == "ok"
    assert "grafana" not in data["unavailable"]


def test_grafana_probe_requires_database_and_admin_not_static_health(monkeypatch):
    """readiness 探针必须校验 database=ok 与共享管理员，不只静态 200。"""
    import httpx

    from app.operations.grafana import client as grafana_client
    from app.shared import config as shared_config

    monkeypatch.setattr(
        shared_config.settings, "grafana_admin_password", "SyntheticAdmin2026!"
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/grafana/api/health":
            return httpx.Response(200, json={"database": "ok", "version": "13.3.1"})
        if request.url.path == "/grafana/api/user":
            return httpx.Response(200, json={"login": "admin", "isGrafanaAdmin": True})
        raise AssertionError(f"unexpected probe call: {request.url.path}")

    probe_client = grafana_client.GrafanaClient(
        base_url="http://grafana-stub", password="SyntheticAdmin2026!",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(
        grafana_client, "build_client", lambda *, transport=None: probe_client
    )
    dependency_probe.probe_grafana()
    assert seen == ["/grafana/api/health", "/grafana/api/user"]

    # database != ok 必须失败（不只静态 200）。
    seen.clear()

    def database_down(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/grafana/api/health":
            return httpx.Response(200, json={"database": "unavailable"})
        raise AssertionError("database check must fail before /user")

    down_client = grafana_client.GrafanaClient(
        base_url="http://grafana-stub", password="SyntheticAdmin2026!",
        transport=httpx.MockTransport(database_down),
    )
    monkeypatch.setattr(
        grafana_client, "build_client", lambda *, transport=None: down_client
    )
    with pytest.raises(RuntimeError, match="health database"):
        dependency_probe.probe_grafana()
    assert seen == ["/grafana/api/health"]


def test_grafana_startup_probe_is_advisory_not_blocking(monkeypatch):
    """Grafana 运行期故障只降级 readiness（503），不阻断 API 启动。"""

    def unavailable():
        raise RuntimeError("grafana down at startup")

    monkeypatch.setattr(dependency_probe, "PROBES", (("Grafana", unavailable),))
    # 不抛异常：其他服务继续运行。
    dependency_probe.probe_startup_dependencies()


def test_grafana_startup_preflight_reports_advisory_without_failing(monkeypatch, capsys):
    def unavailable():
        raise RuntimeError("grafana down")

    monkeypatch.setattr(dependency_probe, "PROBES", (("Grafana", unavailable),))
    assert dependency_probe.main() == 0
    captured = capsys.readouterr()
    assert "Grafana: unavailable (RuntimeError) advisory" in captured.err
    assert "grafana down" not in captured.out + captured.err


def test_health_status_reflects_dependencies():
    """Readiness fails closed in every runtime environment."""
    response = client.get("/health")
    data = response.json()
    assert data["status"] in ("ok", "error")
    assert data["status"] == ("ok" if not data["unavailable"] else "error")
    assert response.status_code == (200 if not data["unavailable"] else 503)


def test_api_readiness_fails_closed():
    response = client.get("/api/health")
    data = response.json()
    if data["unavailable"]:
        assert response.status_code == 503
        assert data["status"] == "error"
    else:
        assert response.status_code == 200
        assert data["status"] == "ok"


def test_http_probe_closes_every_response(monkeypatch):
    """Regression: repeated readiness probes must release every HTTP socket."""
    responses = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def read(self, _limit):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        response = FakeResponse()
        response.closed = False
        response.request = request
        response.timeout = timeout
        responses.append(response)
        return response

    monkeypatch.setattr("app.main.urllib.request.urlopen", fake_urlopen)

    for _ in range(1100):
        _probe_http_service("http://browser:9222/json/version")

    assert len(responses) == 1100
    assert all(response.closed for response in responses)
    assert all(response.timeout == 3.0 for response in responses)
    assert all(response.request.get_header("Connection") == "close"
               for response in responses)


def test_non_production_readiness_does_not_require_dbx(monkeypatch):
    from app.bootstrap import health

    monkeypatch.setattr(health.settings, "environment", "test")
    response = client.get("/health/ready")
    assert "dbx" not in response.json()


def test_production_readiness_requires_dbx(monkeypatch):
    from app.bootstrap import health

    class FakeDbx:
        @staticmethod
        def status():
            return {
                "configured": True,
                "available": False,
                "reason": "DBX_AUTH_FAILED",
            }

    monkeypatch.setattr(health.settings, "environment", "production")
    monkeypatch.setattr(
        "app.data_channel.integrated.dbx_adapter.DbxMetadataAdapter",
        lambda: FakeDbx(),
    )
    response = client.get("/health/ready")
    data = response.json()
    assert data["dbx"] == "unavailable"
    assert data["dbx_reason"] == "DBX_AUTH_FAILED"
    assert "dbx" in data["unavailable"]


def test_production_readiness_reports_dbx_ok(monkeypatch):
    from app.bootstrap import health

    class FakeDbx:
        @staticmethod
        def status():
            return {"configured": True, "available": True, "reason": None}

    monkeypatch.setattr(health.settings, "environment", "production")
    monkeypatch.setattr(
        "app.data_channel.integrated.dbx_adapter.DbxMetadataAdapter",
        lambda: FakeDbx(),
    )
    response = client.get("/health/ready")
    assert response.json()["dbx"] == "ok"


# ---------------------------------------------------------------------------
# Multica deployment-default integration (optional, all-or-nothing)


MULTICA_DEFAULT_COMPLETE = {
    "multica_default_base_url": "http://127.0.0.1:18099",
    "multica_default_web_url": "https://multica.example.com",
    "multica_default_workspace_id": "ws-default",
    "multica_default_workspace_name": "平台默认工作区",
    "multica_default_pat": "synthetic_multica_pat_not_a_secret",
}


def _multica_settings(monkeypatch, **overrides):
    """Point the five deployment-default settings at synthetic values."""
    from app.shared.config import settings as shared_settings

    values = {**MULTICA_DEFAULT_COMPLETE, **overrides}
    for key, value in values.items():
        monkeypatch.setattr(shared_settings, key, value)
    return values


def _multica_clear(monkeypatch):
    """Reset every deployment-default Multica value to the empty default."""
    return _multica_settings(
        monkeypatch,
        multica_default_base_url="",
        multica_default_web_url="",
        multica_default_workspace_id="",
        multica_default_workspace_name="",
        multica_default_pat="",
    )


def _multica_transport(monkeypatch, *, workspaces, me_status=200, seen=None):
    """Stub multica_client's single HTTP seam; no real network is touched."""
    import httpx

    from app.super_assistant import multica_client

    def fake_request(method, url, **kwargs):
        if seen is not None:
            seen.append((method, url, dict(kwargs.get("headers") or {})))
        if url.endswith("/api/me"):
            return httpx.Response(me_status, json={"id": "user-1"})
        if url.endswith("/api/workspaces"):
            return httpx.Response(200, json={"workspaces": workspaces})
        raise AssertionError(f"unexpected multica probe call: {url}")

    monkeypatch.setattr(multica_client, "_request", fake_request)


def test_multica_default_readiness_reports_disabled_when_all_values_empty(
    monkeypatch,
):
    _multica_clear(monkeypatch)

    data = client.get("/health/ready").json()

    assert data["multica_default"] == "disabled"
    assert "multica_default" not in data["unavailable"]


def test_multica_default_probe_verifies_pat_and_workspace_membership(
    monkeypatch,
):
    seen: list = []
    _multica_settings(monkeypatch)
    _multica_transport(
        monkeypatch,
        workspaces=[
            {"id": "ws-other", "name": "其他工作区"},
            {"id": "ws-default", "name": "平台默认工作区"},
        ],
        seen=seen,
    )

    dependency_probe.probe_multica_default()

    assert [url.rsplit("/", 1)[-1] for _method, url, _headers in seen] == [
        "me",
        "workspaces",
    ]
    assert all(
        headers["Authorization"]
        == f"Bearer {MULTICA_DEFAULT_COMPLETE['multica_default_pat']}"
        for _method, _url, headers in seen
    )


@pytest.mark.parametrize(
    "workspaces",
    [
        [{"id": "ws-other", "name": "其他工作区"}],
        [{"name": "无 ID 工作区"}],
        [],
    ],
    ids=["other-workspace", "missing-id", "empty-list"],
)
def test_multica_default_probe_fails_when_workspace_not_visible(
    monkeypatch, workspaces,
):
    _multica_settings(monkeypatch)
    _multica_transport(monkeypatch, workspaces=workspaces)

    with pytest.raises(RuntimeError, match="workspace"):
        dependency_probe.probe_multica_default()


def test_multica_default_probe_failure_cuts_leaky_exception_chain(monkeypatch):
    import httpx

    from app.super_assistant import multica_client

    leaked_url = MULTICA_DEFAULT_COMPLETE["multica_default_base_url"]
    leaked_pat = MULTICA_DEFAULT_COMPLETE["multica_default_pat"]

    def leaky_request(method, url, **kwargs):
        # Transport errors embed the target URL; the probe must never let
        # that (or the PAT) ride the exception chain into logs or readiness.
        raise httpx.ConnectError(f"connect tcp {leaked_url} bearer {leaked_pat}")

    _multica_settings(monkeypatch)
    monkeypatch.setattr(multica_client, "_request", leaky_request)

    with pytest.raises(RuntimeError, match="Multica default service") as info:
        dependency_probe.probe_multica_default()

    assert leaked_url not in str(info.value)
    assert leaked_pat not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_multica_default_probe_reports_http_rejection_without_leaking_status_body(
    monkeypatch,
):
    _multica_settings(monkeypatch)
    _multica_transport(monkeypatch, workspaces=[], me_status=401)

    with pytest.raises(RuntimeError, match="Multica default service"):
        dependency_probe.probe_multica_default()


def test_multica_default_partial_readiness_is_unavailable_without_probing(
    monkeypatch,
):
    from app.super_assistant import multica_client

    def forbidden_request(method, url, **kwargs):
        raise AssertionError("partial settings must not trigger a probe call")

    monkeypatch.setattr(multica_client, "_request", forbidden_request)
    _multica_settings(monkeypatch, multica_default_pat="")

    # The masked-PAT shape (four values set, PAT empty) is the exact state
    # env_file services legitimately see; classification stays explicit.
    assert (
        dependency_probe.multica_default_config_state() == "partial"
    )
    dependency_probe.probe_multica_default()  # returns without network

    data = client.get("/health/ready").json()

    assert data["multica_default"] == "unavailable"
    # Non-production readiness stays advisory for the optional integration.
    assert "multica_default" not in data["unavailable"]


def test_multica_default_complete_readiness_reports_ok(monkeypatch):
    _multica_settings(monkeypatch)
    _multica_transport(
        monkeypatch,
        workspaces=[{"id": "ws-default", "name": "平台默认工作区"}],
    )

    data = client.get("/health/ready").json()

    assert data["multica_default"] == "ok"
    assert "multica_default" not in data["unavailable"]


def test_multica_default_probe_failure_readiness_reports_unavailable(
    monkeypatch,
):
    _multica_settings(monkeypatch)
    _multica_transport(monkeypatch, workspaces=[], me_status=401)

    data = client.get("/health/ready").json()

    assert data["multica_default"] == "unavailable"
    assert "multica_default" not in data["unavailable"]


def _production_multica(monkeypatch, *, settings_overrides=None):
    from app.bootstrap import health
    from app.shared.config import settings as shared_settings

    _multica_settings(monkeypatch, **(settings_overrides or {}))
    # The internal-allowlist entry is exactly how a deployment declares an
    # in-network Multica origin; it also lets the loopback stub pass the
    # production HTTPS/non-global validation in tests.
    monkeypatch.setattr(
        shared_settings,
        "super_assistant_multica_internal_base_urls",
        MULTICA_DEFAULT_COMPLETE["multica_default_base_url"],
    )
    monkeypatch.setattr(health.settings, "environment", "production")


def test_production_readiness_gates_multica_default_when_configured(
    monkeypatch,
):
    _production_multica(monkeypatch)
    _multica_transport(
        monkeypatch,
        workspaces=[{"id": "ws-default", "name": "平台默认工作区"}],
    )

    data = client.get("/health/ready").json()

    assert data["multica_default"] == "ok"
    assert "multica_default" not in data["unavailable"]


def test_production_readiness_fails_when_configured_multica_default_is_down(
    monkeypatch,
):
    _production_multica(monkeypatch)
    _multica_transport(monkeypatch, workspaces=[], me_status=401)

    response = client.get("/health/ready")
    data = response.json()

    assert response.status_code == 503
    assert data["multica_default"] == "unavailable"
    assert "multica_default" in data["unavailable"]


def test_production_readiness_fails_on_partial_multica_default(monkeypatch):
    _production_multica(
        monkeypatch, settings_overrides={"multica_default_pat": ""}
    )
    _multica_transport(monkeypatch, workspaces=[{"id": "ws-default"}])

    response = client.get("/health/ready")
    data = response.json()

    assert response.status_code == 503
    assert data["multica_default"] == "unavailable"
    assert "multica_default" in data["unavailable"]


def test_production_readiness_ignores_unconfigured_multica_default(
    monkeypatch,
):
    from app.bootstrap import health

    _multica_clear(monkeypatch)
    monkeypatch.setattr(health.settings, "environment", "production")

    data = client.get("/health/ready").json()

    assert data["multica_default"] == "disabled"
    assert "multica_default" not in data["unavailable"]


def test_multica_default_startup_probe_is_advisory_not_blocking(monkeypatch):
    """Multica 默认服务运行期故障只降级 readiness，不阻断任何进程启动。"""

    def unavailable():
        raise RuntimeError("multica down at startup")

    monkeypatch.setattr(
        dependency_probe, "PROBES", (("Multica default", unavailable),)
    )
    # 不抛异常：其他服务继续运行。
    dependency_probe.probe_startup_dependencies()


def test_multica_default_startup_preflight_reports_advisory_without_failing(
    monkeypatch, capsys,
):
    def unavailable():
        raise RuntimeError("multica down")

    monkeypatch.setattr(
        dependency_probe, "PROBES", (("Multica default", unavailable),)
    )
    assert dependency_probe.main() == 0
    captured = capsys.readouterr()
    assert "Multica default: unavailable (RuntimeError) advisory" in (
        captured.err
    )
    assert "multica down" not in captured.out + captured.err
