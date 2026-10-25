# tests/v2/infra/test_health.py
"""
Infrastructure health-check tests for the /health endpoint.

These tests run against the FastAPI TestClient. Required external services that
are absent in a pure-unit context must make readiness fail closed with 503;
process liveness remains dependency-independent.
"""
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
        "browser", "n8n",
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
        "redis", "neo4j", "minio", "browser", "n8n",
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
