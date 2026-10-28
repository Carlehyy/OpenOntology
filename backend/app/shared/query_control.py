"""Request/tool-scoped cancellation for bounded, ephemeral database reads."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable
from uuid import UUID

import anyio
from fastapi import HTTPException, Request


class QueryControl:
    def __init__(self, query_id: str | None = None):
        self.query_id = query_id
        self._cancelled = threading.Event()
        self._lock = threading.RLock()
        self._interrupts: set[Callable[[], None]] = set()

    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled.set()
            for interrupt in tuple(self._interrupts):
                try:
                    interrupt()
                except Exception:
                    # Completion is acknowledged only after the reader exits.
                    pass

    @contextmanager
    def interrupt(self, callback: Callable[[], None]):
        with self._lock:
            if self.cancelled():
                callback()
            self._interrupts.add(callback)
        try:
            yield
        finally:
            with self._lock:
                self._interrupts.discard(callback)


_CURRENT: ContextVar[QueryControl | None] = ContextVar("query_control", default=None)


def current_query_control() -> QueryControl | None:
    return _CURRENT.get()


@contextmanager
def query_control_scope(control: QueryControl):
    token = _CURRENT.set(control)
    try:
        yield control
    finally:
        _CURRENT.reset(token)


@contextmanager
def interruptible_connection(connection, *, mysql_cancel: Callable[[int], None] | None = None):
    control = current_query_control()
    if control is None or connection.dialect.name not in {"postgresql", "mysql"}:
        yield
        return
    driver = connection.connection.driver_connection
    if connection.dialect.name == "postgresql":
        interrupt = driver.cancel
    else:
        if mysql_cancel is None:
            raise ValueError("MySQL reads require a bounded source cancellation connection")
        thread_id = int(driver.thread_id())

        def interrupt():
            # Keep the original connection open until the cancellation command
            # completes; its server id must not be released for reuse first.
            mysql_cancel(thread_id)

    with control.interrupt(interrupt):
        yield


async def request_query_control(request: Request):
    header = request.headers.get("x-query-id")
    query_id = None
    if header is not None:
        try:
            query_id = UUID(header).hex
        except (ValueError, AttributeError):
            raise HTTPException(422, detail={"code": "invalid_query_id",
                                            "message": "X-Query-Id must be a UUID"}) from None
    control = QueryControl(query_id)

    async def watch_disconnect():
        while True:
            if await request.is_disconnected():
                await anyio.to_thread.run_sync(control.cancel)
                return
            await anyio.sleep(0.1)

    failure = None
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(watch_disconnect)
        with query_control_scope(control):
            try:
                yield control
            except BaseException as exc:
                failure = exc
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(control.cancel)
            finally:
                tasks.cancel_scope.cancel()
    if failure is not None:
        raise failure
