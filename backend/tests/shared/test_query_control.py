"""Cancellation contracts for bounded query execution."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import threading
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.shared.query_control import (
    QueryControl,
    current_query_control,
    interruptible_connection,
    query_control_scope,
    request_query_control,
)


def test_query_control_interrupts_registered_reader_and_restores_registration():
    control = QueryControl("query-1")
    calls = []

    with control.interrupt(lambda: calls.append("interrupted")):
        assert control.cancelled() is False
        control.cancel()
        assert control.cancelled() is True
        assert calls == ["interrupted"]

    control.cancel()
    assert calls == ["interrupted"]


def test_query_control_registers_after_cancel_with_immediate_interrupt():
    control = QueryControl()
    control.cancel()
    calls = []

    with control.interrupt(lambda: calls.append("late")):
        assert calls == ["late"]

    assert calls == ["late"]


def test_query_control_scope_restores_parent_control():
    outer = QueryControl("outer")
    inner = QueryControl("inner")

    assert current_query_control() is None
    with query_control_scope(outer):
        assert current_query_control() is outer
        with query_control_scope(inner):
            assert current_query_control() is inner
        assert current_query_control() is outer
    assert current_query_control() is None


@pytest.mark.asyncio
async def test_request_query_control_rejects_invalid_query_id():
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/v2/query",
        "headers": [(b"x-query-id", b"not-a-uuid")],
    })

    with pytest.raises(HTTPException) as excinfo:
        await anext(request_query_control(request))

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "invalid_query_id"


@pytest.mark.asyncio
async def test_request_query_control_exposes_normalized_query_id_and_scope():
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/v2/query",
        "headers": [(b"x-query-id", b"550e8400-e29b-41d4-a716-446655440000")],
    }, receive=receive)
    dependency = request_query_control(request)
    control = await anext(dependency)
    try:
        assert control.query_id == "550e8400e29b41d4a716446655440000"
        assert current_query_control() is control
    finally:
        await dependency.aclose()

    assert current_query_control() is None


def test_sync_endpoint_receives_query_control_context_from_dependency():
    app = FastAPI()

    @app.get("/probe")
    def probe(control=Depends(request_query_control)):
        current = current_query_control()
        return {
            "dependency_query_id": control.query_id,
            "endpoint_query_id": current.query_id if current is not None else None,
            "same_control": current is control,
        }

    with TestClient(app) as client:
        response = client.get(
            "/probe",
            headers={"X-Query-Id": "550e8400-e29b-41d4-a716-446655440000"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "dependency_query_id": "550e8400e29b41d4a716446655440000",
        "endpoint_query_id": "550e8400e29b41d4a716446655440000",
        "same_control": True,
    }


@pytest.mark.asyncio
async def test_request_query_control_cancels_when_client_disconnects():
    calls = 0

    async def is_disconnected():
        nonlocal calls
        calls += 1
        return calls >= 2

    request = SimpleNamespace(headers={}, is_disconnected=is_disconnected)
    dependency = request_query_control(request)
    control = await anext(dependency)
    interrupted = []
    try:
        with control.interrupt(lambda: interrupted.append("reader")):
            for _ in range(100):
                if control.cancelled():
                    break
                await asyncio.sleep(0.01)
        assert control.cancelled() is True
        assert interrupted == ["reader"]
        assert calls >= 2
    finally:
        await dependency.aclose()


@pytest.mark.parametrize(
    ("dialect", "driver_factory", "expected"),
    [
        (
            "postgresql",
            lambda calls: SimpleNamespace(cancel=lambda: calls.append("cancel")),
            ["cancel"],
        ),
        (
            "mysql",
            lambda calls: SimpleNamespace(thread_id=lambda: 71),
            [71],
        ),
    ],
)
def test_interruptible_connection_registers_native_driver_interrupt(
    dialect, driver_factory, expected
):
    calls = []
    connection = SimpleNamespace(
        dialect=SimpleNamespace(name=dialect),
        connection=SimpleNamespace(driver_connection=driver_factory(calls)),
    )
    control = QueryControl()

    with query_control_scope(control):
        with interruptible_connection(connection, mysql_cancel=calls.append):
            control.cancel()

    assert calls == expected
    control.cancel()
    assert calls == expected

    if dialect == "mysql":
        with query_control_scope(QueryControl()):
            with pytest.raises(ValueError, match="bounded source cancellation"):
                with interruptible_connection(connection):
                    pytest.fail("an uncancellable MySQL read must not start")

        from app.data_channel.integrated import native
        commands = []
        disposed = []

        @contextmanager
        def connect():
            yield SimpleNamespace(exec_driver_sql=commands.append)

        def engine(url, **kwargs):
            assert url.get_backend_name() == "mysql"
            assert kwargs["connect_args"] == {
                "connect_timeout": 2, "read_timeout": 2, "write_timeout": 2,
            }
            assert kwargs["hide_parameters"] is True
            return SimpleNamespace(connect=connect, dispose=lambda: disposed.append(True))

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(native.sa, "create_engine", engine)
            cancel = native._mysql_canceller(SimpleNamespace(kind="mysql", config={}))
            cancel(71)
        assert commands == ["KILL QUERY 71"]
        assert disposed == [True]

        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        control = QueryControl()

        def bounded_cancel(thread_id):
            assert thread_id == 71
            started.set()
            assert release.wait(2)

        def exit_scope():
            with query_control_scope(control):
                with interruptible_connection(connection, mysql_cancel=bounded_cancel):
                    assert started.wait(2)
            finished.set()

        reader = threading.Thread(target=exit_scope)
        canceller = threading.Thread(target=control.cancel)
        reader.start()
        canceller.start()
        try:
            assert started.wait(2)
            assert not finished.wait(.05)
        finally:
            release.set()
            reader.join(2)
            canceller.join(2)
        assert not reader.is_alive() and not canceller.is_alive()
        assert finished.is_set()
