"""Opt-in real PostgreSQL fixtures; each test owns one disposable schema.

AUTOLOOP_TEST_POSTGRES_URL must point to an explicitly reserved loopback database.
AUTOLOOP_TEST_RESOURCE_LEDGER optionally records create/drop evidence outside Git.
No containers, databases, public-schema objects or unrelated resources are changed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import make_url

from app.auth.models import User
from app.shared.database import Base
from app.super_assistant.autoloop.models import AUTOLOOP_TABLES


def _record(schema, operation):
    target = os.getenv("AUTOLOOP_TEST_RESOURCE_LEDGER")
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"schema": schema, "operation": operation}) + "\n")


@pytest.fixture
def pg_schema():
    raw = os.getenv("AUTOLOOP_TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("reserved loopback PostgreSQL not configured")
    url = make_url(raw)
    if (url.get_backend_name() != "postgresql" or url.host not in ("127.0.0.1", "localhost", "::1")
            or not url.database or url.database in ("postgres", "template0", "template1")
            or "options" in url.query):
        pytest.fail("AutoLoop PostgreSQL requires an isolated loopback database without search_path options")
    schema = "al_it_" + uuid4().hex
    admin = create_engine(url, pool_pre_ping=True)
    engine = None
    created = False
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        _record(schema, "created")
        scoped_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
        engine = create_engine(scoped_url, pool_pre_ping=True, connect_args={"connect_timeout": 10})
        with engine.begin() as connection:
            connection.execute(text("SET statement_timeout = '20s'"))
        yield engine, scoped_url, schema
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            _record(schema, "dropped")
        admin.dispose()


@pytest.fixture
def pg_engine(pg_schema):
    engine, _, _ = pg_schema
    # Only the canonical AutoLoop tables and minimal owner table are needed.
    owners = MetaData()
    User.__table__.to_metadata(owners).create(engine)
    Base.metadata.create_all(engine, tables=list(AUTOLOOP_TABLES))
    return engine
