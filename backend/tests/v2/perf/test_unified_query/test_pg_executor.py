"""Opt-in real-engine contracts; full HTTP/RBAC/scale lives in E2E scripts.

Never reads deployment dotenv or default application DSNs. The orchestrator must
explicitly enable these tests and supply its three isolated loopback databases.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.data_channel.query.contracts import (ColumnSpec, TextQueryCursorRequest, QueryError,
                                            QueryLimits, TextQueryRequest, Relation)
from app.data_channel.query.models import UnifiedQueryRow, UnifiedQuerySnapshot
from app.data_channel.query.service import QueryService

pytestmark = pytest.mark.skipif(os.environ.get("INTEGRATED_QUERY_E2E") != "1",
                                reason="explicit isolated real-engine opt-in required")


def _url(name, port, database):
    raw = os.environ.get(name)
    if not raw: pytest.fail(f"{name} is required; no default/production fallback")
    url = sa.engine.make_url(raw)
    if url.host != "127.0.0.1" or url.port != port or url.database != database:
        pytest.fail(f"{name} must point to the owned loopback E2E database")
    return raw


@pytest.fixture
def engine_fixture(request):
    pg = sa.create_engine(_url("INTEGRATED_QUERY_PLATFORM_DSN", 45432, "integrated_metadata"))
    source_pg = sa.create_engine(_url("INTEGRATED_QUERY_POSTGRES_DSN", 45433, "integrated_source"))
    mysql_required = "mysql" in request.node.name
    source_mysql = (sa.create_engine(_url("INTEGRATED_QUERY_MYSQL_DSN", 43306, "integrated_source"))
                    if mysql_required else None)
    source_engines = [source_pg] + ([source_mysql] if source_mysql is not None else [])
    for engine in [pg, *source_engines]: request.addfinalizer(engine.dispose)
    # Test-only DDL for the two fixed snapshot models; production uses Alembic.
    UnifiedQuerySnapshot.__table__.create(pg, checkfirst=True)
    UnifiedQueryRow.__table__.create(pg, checkfirst=True)
    name = "uqe_" + uuid.uuid4().hex
    tables = []
    for engine in source_engines:
        table = sa.Table(name, sa.MetaData(), sa.Column("id", sa.BigInteger, primary_key=True),
                         sa.Column("key", sa.BigInteger), sa.Column("amount", sa.Numeric(30, 9)),
                         sa.Column("note", sa.Text))
        table.create(engine)
        def drop_owned_table(table=table, engine=engine):
            assert table.name == name and name.startswith("uqe_")
            table.drop(engine)
        request.addfinalizer(drop_owned_table)
        tables.append(table)
        with engine.begin() as conn:
            conn.execute(table.insert(), [
                {"id": 1, "key": 9007199254740993, "amount": Decimal("123456789012345.123456789"), "note": None},
                {"id": 2, "key": 9007199254740993, "amount": Decimal("0.000000001"), "note": ""},
                {"id": 3, "key": 9007199254740994, "amount": Decimal("0.000000001"), "note": "中文\\N\t\n"},
            ])
    user = SimpleNamespace(id="uqe-" + uuid.uuid4().hex)
    columns = {"id": ColumnSpec("id", "integer", False), "key": ColumnSpec("key", "integer"),
               "amount": ColumnSpec("amount", "decimal", precision=30, scale=9), "note": ColumnSpec("note", "string")}

    class FixtureService(QueryService):
        def _resolve(self, request, pipeline):
            # pg/other are separate transactions, deliberately distinct source
            # groups, even when both use this owned PG database. This exercises
            # the genuine PostgreSQL COPY/staged executor without substituting
            # it for the separately named MySQL cross-dialect test.
            sources = {"pg": (source_pg, tables[0]), "other": (source_pg, tables[0])}
            if source_mysql is not None: sources["my"] = (source_mysql, tables[1])
            for source, alias in pipeline.sources:
                engine, table = sources[source]
                connection = self.stack.enter_context(engine.connect().execution_options(isolation_level="REPEATABLE READ"))
                self.stack.enter_context(connection.begin())
                bound = table.alias(alias)
                def batches(statement, params, size, connection=connection):
                    result = connection.execution_options(stream_results=True).execute(statement, params).mappings()
                    try:
                        while batch := result.fetchmany(size): yield [dict(row) for row in batch]
                    finally: result.close()
                self.relations[alias] = Relation(alias, bound, columns,
                    {"alias": alias, "kind": "integrated", "id": source, "versionId": None,
                     "asOf": "2026-10-24T00:00:00+00:00", "consistency": "repeatable_read",
                     "completeness": "complete", "dialect": engine.dialect.name},
                    "auth", "schema", source, batches=batches)
                if getattr(self, "fixture_page_cap", None) is not None:
                    self.relations[alias].state["maxPageSize"] = self.fixture_page_cap

    with Session(pg, expire_on_commit=False) as db:
        yield SimpleNamespace(db=db, service=FixtureService, user=user, source_pg=source_pg,
                              source_mysql=source_mysql, tables=tables, platform=pg)
        db.rollback()
        ids = sa.select(UnifiedQuerySnapshot.id).where(UnifiedQuerySnapshot.owner_user_id == user.id)
        db.execute(sa.delete(UnifiedQueryRow).where(UnifiedQueryRow.snapshot_id.in_(ids)))
        db.execute(sa.delete(UnifiedQuerySnapshot).where(UnifiedQuerySnapshot.owner_user_id == user.id))
        db.commit()


def request(query, page_size=100):
    # The engine fixture deliberately owns source resolution. HTTP/RBAC tests
    # use the production registry instead, and are not represented by this test.
    names = {"pg"}
    if "join my" in query: names.add("my")
    if "join other" in query: names.add("other")
    return TextQueryRequest(language="spl", query=query,
                        bindings={n: {"kind": "integrated", "assetId": n} for n in names}, pageSize=page_size)


def service(fixture, **limits):
    return fixture.service(fixture.db, fixture.user, secret="isolated-engine-test-secret",
                           limits=replace(QueryLimits(), **limits))


@pytest.mark.parametrize("right", ["other", "my"], ids=["postgres", "mysql"])
def test_real_cross_source_decimal_bigint_fanout_and_null(engine_fixture, right):
    data = service(engine_fixture).execute(request(
        f"from pg as p | join {right} as m on p.key = m.key type=left "
        "| stats count() as n, count_distinct(p.id) as ids, sum(p.amount) as total"))
    assert data["rows"] == [{"n": "5", "ids": "3", "total": "246913578024690.246913581"}]
    assert data["consistency"] == "cross_source_non_atomic"
    assert data["plan"]["sourceRows"] == {"p": 3, "m": 3}
    assert data["plan"]["stagingBytes"] > 0
    assert data["totalRows"] == 1 and data["resultLimited"] is False


def test_real_pg_copy_null_empty_unicode_and_bigint(engine_fixture):
    data = service(engine_fixture).execute(request(
        "from pg as p | join other as m on p.id=m.id "
        "| fields p.id, p.key, m.note | sort id asc"))
    assert data["rows"] == [
        {"id": "1", "key": "9007199254740993", "note": None},
        {"id": "2", "key": "9007199254740993", "note": ""},
        {"id": "3", "key": "9007199254740994", "note": "中文\\N\t\n"},
    ]
    assert data["plan"]["strategy"] == "postgresql_staged_sql"


def test_real_stable_pages_survive_concurrent_source_mutation(engine_fixture):
    fixture = engine_fixture
    data = service(fixture).execute(request("from pg as p | fields p.id, p.key, p.note | sort id asc", 1))
    assert data["rows"][0]["id"] == "1" and data["rows"][0]["key"] == "9007199254740993"
    with fixture.source_pg.begin() as conn:
        conn.execute(fixture.tables[0].update().where(fixture.tables[0].c.id == 2).values(note="changed"))
    next_page = service(fixture).fetch(TextQueryCursorRequest(cursor=data["page"]["cursor"]))
    assert next_page["rows"] == [{"id": "2", "key": "9007199254740993", "note": ""}]
    assert next_page["totalRows"] == 3 and next_page["resultLimited"] is False


def test_real_pg_profile_output_cap_is_per_page_not_total(engine_fixture):
    fixture = engine_fixture
    class ScopedService(fixture.service):
        fixture_page_cap = 2
    def scoped():
        return ScopedService(fixture.db, fixture.user, secret="page-cap-test-only")
    body = "from pg as p | join other as m on p.key=m.key | fields p.id as pid, m.id as mid | sort pid asc, mid asc"
    with pytest.raises(QueryError) as exc:
        scoped().execute(request(body, 3))
    assert exc.value.code == "query_page_size_exceeds_scope" and exc.value.status == 422
    first = scoped().execute(request(body, 2))
    assert first["totalRows"] == 5 and len(first["rows"]) == 2 and first["resultLimited"] is False
    cursor = first["page"]["cursor"]
    second = scoped().fetch(TextQueryCursorRequest(cursor=cursor))
    replay = scoped().fetch(TextQueryCursorRequest(cursor=cursor))
    assert second["rows"] == replay["rows"] and len(second["rows"]) == 2
    third = scoped().fetch(TextQueryCursorRequest(cursor=second["page"]["cursor"]))
    assert len(third["rows"]) == 1 and third["page"]["cursor"] is None
    rows = first["rows"] + second["rows"] + third["rows"]
    assert [(r["pid"], r["mid"]) for r in rows] == [("1", "1"), ("1", "2"), ("2", "1"), ("2", "2"), ("3", "3")]


def test_real_pg_platform_stream_byte_bound(engine_fixture):
    from types import SimpleNamespace
    compiled = SimpleNamespace(
        statement=sa.select(sa.literal("中" * 250000).label("note")).select_from(sa.func.generate_series(1, 3)),
        params={}, columns=[ColumnSpec("note", "string")])
    reader = service(engine_fixture, result_bytes_limit=1024**2)
    assert [len(batch) for batch in reader._query_rows(compiled, {}, False)] == [1, 1, 1]
    rejecting = service(engine_fixture, result_bytes_limit=1024)
    with pytest.raises(QueryError) as exc:
        list(rejecting._query_rows(compiled, {}, False))
    assert exc.value.code == "query_budget_exceeded"
    assert exc.value.extra["budget"] == "copy_buffer_bytes"
    # The failed byte guard closed its named cursor, leaving the transaction usable.
    assert engine_fixture.db.scalar(sa.select(sa.literal(1))) == 1


def test_real_pg_byte_limited_pages_keep_total_and_replay(engine_fixture):
    import json
    fixture = engine_fixture
    texts = ["中" * 1000, "🙂" * 800, "\\\t\n" * 1000]
    with fixture.source_pg.begin() as conn:
        for index, text in enumerate(texts, 1):
            conn.execute(fixture.tables[0].update().where(fixture.tables[0].c.id == index).values(note=text))
    full = service(fixture).execute(request("from pg as p | fields p.id, p.note | sort id asc", 3))
    snapshot = fixture.db.get(UnifiedQuerySnapshot, full["queryId"])
    first = service(fixture, result_bytes_limit=10000)._page(snapshot, 0)
    assert first["totalRows"] == 3 and len(first["rows"]) == 2
    token = first["page"]["cursor"]
    second = service(fixture, result_bytes_limit=10000).fetch(TextQueryCursorRequest(cursor=token))
    replay = service(fixture, result_bytes_limit=10000).fetch(TextQueryCursorRequest(cursor=token))
    assert second == replay and len(second["rows"]) == 1
    assert second["page"]["hasMore"] is False and second["page"]["cursor"] is None
    assert first["rows"] + second["rows"] == [{"id": str(i), "note": text} for i, text in enumerate(texts, 1)]
    for page in [first, second]:
        assert page["totalRows"] == 3 and page["resultLimited"] is False
        assert len(json.dumps({"data": page}, ensure_ascii=False, separators=(",", ":")).encode()) <= 10000


def test_real_semantic_limit_is_not_page_size(engine_fixture):
    data = service(engine_fixture).execute(request("from pg as p | fields p.id | sort id asc | limit 2", 1))
    assert data["totalRows"] == 2 and data["resultLimited"] is True
    assert data["page"]["hasMore"] is True


def test_real_budget_failure_publishes_no_partial_snapshot(engine_fixture):
    fixture = engine_fixture
    before = fixture.db.scalar(sa.select(sa.func.count()).select_from(UnifiedQuerySnapshot))
    with pytest.raises(QueryError) as exc:
        service(fixture, staging_bytes_limit=1).execute(request(
            "from pg as p | join other as m on p.key=m.key | stats count() as n"))
    assert exc.value.code == "query_budget_exceeded"
    after = fixture.db.scalar(sa.select(sa.func.count()).select_from(UnifiedQuerySnapshot))
    assert after == before
