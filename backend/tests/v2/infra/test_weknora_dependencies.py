"""WeKnora service availability is independent of user knowledge configuration."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from app.shared import dependency_probe, http_transport


def _transport(monkeypatch, *, status=200, error=None):
    calls = []
    @contextmanager
    def open_request(request, *, timeout):
        calls.append((request.full_url, timeout))
        if error:
            raise error
        class Response:
            def read(self, limit):
                assert limit == 4096
                return b'{"status":"ok"}'
        response = Response()
        response.status = status
        yield response
    monkeypatch.setattr(http_transport, "open_with_loopback_bypass", open_request)
    monkeypatch.setattr(dependency_probe.settings, "weknora_base_url", "http://weknora-app:8080")
    return calls


@pytest.mark.asyncio
async def test_weknora_health_probe_runs_in_lifecycle_worker_thread(monkeypatch):
    calls = _transport(monkeypatch)
    await asyncio.to_thread(dependency_probe.probe_weknora)
    assert calls == [("http://weknora-app:8080/health", 5)]


def test_unconfigured_knowledge_does_not_block_startup(monkeypatch):
    calls = _transport(monkeypatch)
    monkeypatch.setattr(dependency_probe, "PROBES", (("WeKnora", dependency_probe.probe_weknora),))
    dependency_probe.probe_startup_dependencies()
    assert [url for url, _ in calls] == ["http://weknora-app:8080/health"]
    assert not hasattr(dependency_probe.settings, "weknora_state_path")


@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize("failure", ["offline", "unhealthy"])
def test_weknora_service_failure_blocks_startup(environment, failure, monkeypatch):
    _transport(monkeypatch, status=503 if failure == "unhealthy" else 200,
               error=ConnectionError("private service detail") if failure == "offline" else None)
    monkeypatch.setattr(dependency_probe.settings, "environment", environment)
    monkeypatch.setattr(dependency_probe, "PROBES", (("WeKnora", dependency_probe.probe_weknora),))
    with pytest.raises(RuntimeError) as error:
        dependency_probe.probe_startup_dependencies()
    assert str(error.value) == "Required startup dependencies unavailable: WeKnora"


def test_preflight_never_logs_driver_details(monkeypatch, capsys):
    _transport(monkeypatch, error=ConnectionError("should-never-appear"))
    monkeypatch.setattr(dependency_probe, "PROBES", (("WeKnora", dependency_probe.probe_weknora),))
    assert dependency_probe.main() == 1
    captured = capsys.readouterr()
    assert "WeKnora: unavailable (ConnectionError)" in captured.err
    assert "should-never-appear" not in captured.out + captured.err
