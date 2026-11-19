"""Serialize native endpoint writes across API workers without holding a business transaction."""
from contextlib import contextmanager
import threading

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.super_assistant.weknora.client import WeKnoraError


_WRITE_LOCK = threading.Lock()
_LOCK_KEY = "openontology:weknora:mcp-directory"


@contextmanager
def directory_lock(db: Session):
    if not _WRITE_LOCK.acquire(timeout=5):
        raise WeKnoraError("WEKNORA_BUSY", "WeKnora 接入正在处理，请稍后刷新", 409)
    connection = None
    acquired = False
    try:
        bind = db.get_bind()
        if bind.dialect.name == "postgresql":
            # A separately checked-out connection retains ownership across the
            # business Session's commits; it is never returned with a held lock.
            connection = bind.connect()
            acquired = bool(connection.execute(text(
                "SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"
            ), {"key": _LOCK_KEY}).scalar())
            connection.commit()
            if not acquired:
                raise WeKnoraError("WEKNORA_BUSY", "WeKnora 接入正在处理，请稍后刷新", 409)
        yield
    finally:
        if connection is not None:
            try:
                if acquired:
                    connection.execute(text(
                        "SELECT pg_advisory_unlock(hashtextextended(:key, 0))"
                    ), {"key": _LOCK_KEY})
                    connection.commit()
            except Exception:
                connection.invalidate()
            finally:
                connection.close()
        _WRITE_LOCK.release()
