"""Kaneo service availability requires a bootstrapped, DB-backed instance.

``/api/health`` is a static 200, so the startup/readiness probe must read
``/api/instance/status`` and require ``hasUsers``/``hasAdmin`` to be true.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from app.shared import dependency_probe, http_transport


def _transport(monkeypatch, *, status=200, payload='{"hasUsers":true,"hasAdmin":true}',
               error=None):
    calls = []

    @contextmanager
    def open_request(request, *, timeout):
        calls.append((request.full_url, timeout))
        if error:
            raise error
        class Response:
            def read(self, limit):
                assert limit == 4096
                return payload.encode()
        response = Response()
        response.status = status
        yield response

    monkeypatch.setattr(http_transport, "open_with_loopback_bypass", open_request)
    monkeypatch.setattr(
        dependency_probe.settings, "kaneo_base_url", "http://127.0.0.1:18090",
    )
    return calls


def test_probe_targets_db_backed_instance_status_not_static_health(monkeypatch):
    calls = _transport(monkeypatch)
    asyncio.run(asyncio.to_thread(dependency_probe.probe_kaneo))
    assert calls == [("http://127.0.0.1:18090/api/instance/status", 5)]


@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize(
    ("status", "payload", "failure"),
    [
        (503, '{"hasUsers":true,"hasAdmin":true}', "unhealthy"),
        (200, "not-json", "invalid-json"),
        (200, '{"hasUsers":"yes","hasAdmin":true}', "invalid-payload"),
        (200, '{"hasUsers":false,"hasAdmin":false}', "not-bootstrapped"),
        (200, '{"hasUsers":true,"hasAdmin":false}', "not-bootstrapped"),
        (200, "{}", "invalid-payload"),
    ],
)
def test_kaneo_probe_failure_blocks_startup(
    environment, status, payload, failure, monkeypatch,
):
    if failure == "unhealthy":
        _transport(monkeypatch, status=status)
    else:
        _transport(monkeypatch, status=status, payload=payload)
    monkeypatch.setattr(dependency_probe.settings, "environment", environment)
    monkeypatch.setattr(
        dependency_probe, "PROBES", (("Kaneo", dependency_probe.probe_kaneo),),
    )
    with pytest.raises(RuntimeError) as error:
        dependency_probe.probe_startup_dependencies()
    assert str(error.value) == "Required startup dependencies unavailable: Kaneo"


def test_bootstrapped_instance_probe_passes_startup(monkeypatch):
    calls = _transport(monkeypatch)
    monkeypatch.setattr(
        dependency_probe, "PROBES", (("Kaneo", dependency_probe.probe_kaneo),),
    )
    dependency_probe.probe_startup_dependencies()
    assert [url for url, _ in calls] == [
        "http://127.0.0.1:18090/api/instance/status",
    ]


def test_preflight_never_logs_driver_details(monkeypatch, capsys):
    _transport(monkeypatch, error=ConnectionError("should-never-appear"))
    monkeypatch.setattr(
        dependency_probe, "PROBES", (("Kaneo", dependency_probe.probe_kaneo),),
    )
    assert dependency_probe.main() == 1
    captured = capsys.readouterr()
    assert "Kaneo: unavailable (ConnectionError)" in captured.err
    assert "should-never-appear" not in captured.out + captured.err


def test_probe_rejects_non_origin_base_url(monkeypatch):
    monkeypatch.setattr(
        dependency_probe.settings, "kaneo_base_url", "http://127.0.0.1:18090/api",
    )
    with pytest.raises(RuntimeError, match="absolute HTTP.S. origin"):
        dependency_probe.probe_kaneo()
