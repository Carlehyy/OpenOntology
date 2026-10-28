"""Deterministic language contracts, not a substitute for real PostgreSQL E2E."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.dialects import mysql, postgresql

from app.data_channel.query.compiler import compile_query
from app.data_channel.query.contracts import (ColumnSpec, TextQueryCursorRequest, QueryError,
                                            QueryLimits, TextQueryRequest, Relation)
from app.data_channel.query.parser import parse
from app.data_channel.query.service import QueryService, _copy_cell, _copy_line, _memory_bytes, encode_value


@pytest.fixture
def relations():
    metadata = sa.MetaData()
    orders = sa.Table("orders", metadata, sa.Column("id", sa.BigInteger),
                      sa.Column("customer_id", sa.BigInteger), sa.Column("amount", sa.Numeric(30, 4)),
                      sa.Column("note", sa.Text), sa.Column("active", sa.Boolean))
    customers = sa.Table("customers", metadata, sa.Column("id", sa.BigInteger), sa.Column("region", sa.Text))
    def relation(alias, table, specs):
        return Relation(alias, table, {spec.name: spec for spec in specs},
                        {"completeness": "complete"}, "auth", "schema", "test", platform=True)
    return {"o": relation("o", orders, [ColumnSpec("id", "integer"), ColumnSpec("customer_id", "integer"),
                                       ColumnSpec("amount", "decimal", precision=30, scale=4),
                                       ColumnSpec("note", "string"), ColumnSpec("active", "boolean")]),
            "c": relation("c", customers, [ColumnSpec("id", "integer"), ColumnSpec("region", "string")])}


def compile_(text, relations, params=None, dialect="sqlite"):
    return compile_query(parse(text), relations, params or {}, dialect=dialect)


@pytest.fixture
def sql_db(relations):
    engine = sa.create_engine("sqlite://")
    list(relations.values())[0].table.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(relations["o"].table.insert(), [
            {"id": 1, "customer_id": 1, "amount": Decimal("1.25"), "note": None, "active": True},
            {"id": 2, "customer_id": 1, "amount": Decimal("2.75"), "note": "", "active": False},
            {"id": 3, "customer_id": 99, "amount": Decimal("9.00"), "note": "中文", "active": True},
        ])
        conn.execute(relations["c"].table.insert(), [
            {"id": 1, "region": "north"}, {"id": 1, "region": "north"},
        ])
        yield conn
    engine.dispose()


def test_pipeline_left_join_fanout_nulls_and_exact_stats(relations, sql_db):
    query = compile_("from orders as o | where o.amount >= :minimum "
                     "| join customers as c on o.customer_id = c.id type=left "
                     "| stats count() as rows, count_distinct(o.id) as orders, sum(o.amount) as total by c.region "
                     "| sort total desc | limit 10", relations, {"minimum": "1.25"})
    rows = sql_db.execute(query.statement, query.params).mappings().all()
    assert rows[0]["region"] is None and rows[0]["rows"] == 1
    assert rows[1]["region"] == "north" and rows[1]["rows"] == 4 and rows[1]["orders"] == 2
    assert rows[1]["total"] == Decimal("8.0000000000")
    assert query.columns[-1].type == "decimal"


def test_left_join_predicate_order_preserved(relations, sql_db):
    pre = compile_("from orders as o | where o.active = true | join customers as c on o.customer_id = c.id type=left "
                   "| stats count() as n", relations)
    post = compile_("from orders as o | join customers as c on o.customer_id = c.id type=left "
                    "| where c.region = \"north\" | stats count() as n", relations)
    assert sql_db.execute(pre.statement).scalar_one() == 3
    assert sql_db.execute(post.statement).scalar_one() == 4


def test_null_is_distinct_from_empty(relations, sql_db):
    query = compile_("from orders as o | where o.note is null or o.note = \"\" | fields o.id | sort id desc", relations)
    assert sql_db.execute(query.statement).scalars().all() == [2, 1]


def test_quoted_physical_names_are_bound_not_raw_sql():
    name = "金额`; DROP TABLE users;--"
    table = sa.table("safe", sa.column(name, sa.Numeric(20, 4)))
    relation = Relation("o", table, {name: ColumnSpec(name, "decimal")}, {}, "a", "s", "p", platform=True)
    text = "from x as o | where o.`金额``; DROP TABLE users;--` >= :v | fields o.`金额``; DROP TABLE users;--` as total"
    query = compile_query(parse(text), {"o": relation}, {"v": "123456789012345.1234"})
    compiled = query.statement.compile(dialect=postgresql.dialect())
    assert '"金额`; DROP TABLE users;--"' in str(compiled)
    assert "123456789012345" not in str(compiled)
    assert query.params == {"qv_0": Decimal("123456789012345.1234")}


@pytest.mark.parametrize("query", [
    "SELECT * FROM orders", "from x as x; DROP TABLE users", "from x as x | eval v=1",
    "from x as x | where x.id = null", "from x as x | join y as x on x.id=x.id",
    "from x as `汉字`", "from `汉字` as x", "from x as x | limit -1", "from x as x | where x.id = 'unapproved'",
])
def test_unapproved_syntax_rejected(query):
    with pytest.raises(QueryError): parse(query)


@pytest.mark.parametrize("query,params,code", [
    ("from orders as o | where o.id = :id", {}, "missing_parameter"),
    ("from orders as o | fields o.id", {"x": 1}, "unused_parameter"),
    ("from orders as o | where o.id = :x", {"x": True}, "invalid_parameter_type"),
    ("from orders as o | where o.amount = :x", {"x": 0.1}, "invalid_parameter_type"),
    ("from orders as o | fields o.missing", {}, "unknown_field"),
    ("from orders as o | fields o.id as n, o.amount as n", {}, "duplicate_output"),
    ("from orders as o | stats sum(o.note) as n", {}, "invalid_aggregate_type"),
    ("from orders as o | limit 2 | where o.id=1", {}, "invalid_operator_order"),
    ("from orders as o | fields o.id | fields o.amount", {}, "invalid_operator_order"),
    ("from orders as o | join customers as c on o.amount=c.id | fields o.id", {}, "join_type_mismatch"),
    ("from orders as o | where o.id = :x", {"x": "9223372036854775808"}, "invalid_parameter_type"),
    ("from orders as o | where o.amount = :x", {"x": "0.00001"}, "invalid_parameter_type"),
    ("from orders as o | fields o.id | sort c.id", {}, "unknown_output"),
])
def test_strict_validation(relations, query, params, code):
    with pytest.raises(QueryError) as exc: compile_(query, relations, params)
    assert exc.value.code == code


@pytest.mark.parametrize("kind", ["timestamp", "time"])
@pytest.mark.parametrize("parameterized", [True, False], ids=["named", "literal"])
@pytest.mark.parametrize("aware", [False, True], ids=["naive", "aware"])
def test_temporal_comparisons_reject_lossy_precision_and_offsets(kind, parameterized, aware):
    from datetime import timedelta
    spec = ColumnSpec("value", kind, timezone=aware)
    table = sa.table("temporal_rows", sa.column("value", spec.sql_type()))
    relation = Relation("t", table, {"value": spec}, {}, "a", "s", "p", platform=True)
    prefix = "2026-10-24T12:34:56" if kind == "timestamp" else "12:34:56"
    suffix = "+08:30" if aware else ""
    def compile_value(raw):
        token, params = (":value", {"value": raw}) if parameterized else (f'"{raw}"', {})
        return compile_(f"from temporal_rows as t | where t.value = {token} | fields t.value",
                        {"t": relation}, params).params["qv_0"]
    for separator in (".", ","):
        for fraction in ("123456", "1234560", "123456000", "0000000"):
            value = compile_value(prefix + separator + fraction + suffix)
            assert value.microsecond == int(fraction[:6])
            assert value.utcoffset() == (timedelta(hours=8, minutes=30) if aware else None)
            if kind == "timestamp":
                assert value.date().isoformat() == "2026-10-24"
            assert (value.hour, value.minute, value.second) == (12, 34, 56)
        for fraction in ("1234561", "0000001", "123456000001", "9999999"):
            with pytest.raises(QueryError) as exc:
                compile_value(prefix + separator + fraction + suffix)
            assert (exc.value.status, exc.value.code) == (422, "invalid_parameter_type")
    if aware:
        for offset in ("+00:00:00.1", "-00:00:00,000001", "+08:00:00.1", "-08:00:00,000001"):
            with pytest.raises(QueryError) as exc:
                compile_value(prefix + ".123456" + offset)
            assert (exc.value.status, exc.value.code) == (422, "invalid_parameter_type")
        for offset in ("Z", "+00:00:00.0000000", "-00:00:00,0000000"):
            value = compile_value(prefix + ".123456000" + offset)
            assert value.microsecond == 123456 and value.utcoffset() == timedelta(0)


def test_big_integer_and_decimal_lossless_encoding():
    assert encode_value(9007199254740993, ColumnSpec("id", "integer")) == "9007199254740993"
    assert encode_value(Decimal("12345678901234567890.123456789"), ColumnSpec("n", "decimal")) == "12345678901234567890.123456789"
    assert encode_value(None, ColumnSpec("note", "string")) is None
    assert encode_value("", ColumnSpec("note", "string")) == ""
    with pytest.raises(QueryError): encode_value(0.1, ColumnSpec("n", "decimal"))


def test_json_null_is_distinct_in_projection_count_predicate_copy_and_wire():
    import json
    from app.data_channel.integrated.contracts import JsonText
    table = sa.Table("json_rows", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True),
                     sa.Column("j", sa.JSON), sa.Column("required", sa.JSON, nullable=False))
    columns = {"id": ColumnSpec("id", "integer", False), "j": ColumnSpec("j", "json"),
               "required": ColumnSpec("required", "json", False)}
    engine = sa.create_engine("sqlite://")
    try:
        table.metadata.create_all(engine)
        with engine.begin() as db:
            db.execute(table.insert().values(id=1, j=sa.null(), required=sa.JSON.NULL))
            db.execute(table.insert().values(id=2, j=sa.JSON.NULL, required={"nested": [None]}))
            db.execute(table.insert().values(id=3, j={"$json": None}, required={"$integer": "12"}))
            def batches(statement, params, size):
                yield [dict(row) for row in db.execute(statement, params).mappings()]
            relation = Relation("o", table, columns, {}, "a", "s", "test", batches=batches, platform=True)
            relations = {"o": relation}
            count = compile_("from x as o | stats count(o.j) as n, count(o.required) as r", relations)
            assert db.execute(count.statement).mappings().one() == {"n": 2, "r": 3}
            nulls = compile_("from x as o | where o.j is null | fields o.id", relations)
            assert db.execute(nulls.statement).scalars().all() == [1]
            compiled = compile_("from x as o | fields o.j, o.required | limit 3", relations)
            assert compiled.columns[0].encoding == "json_text"
            for dialect in [postgresql.dialect(), mysql.dialect()]:
                assert "CAST(" in str(compiled.statement.compile(dialect=dialect))
            service = QueryService(db, SimpleNamespace(id="owner"), secret="test")
            service._platform_budget = lambda: None
            service._deadline = lambda: None
            for native in [True, False]:
                rows = [row for batch in service._query_rows(compiled, relations, native) for row in batch]
                assert rows[0]["j"] is None and isinstance(rows[1]["j"], JsonText) and rows[1]["j"] == "null"
                assert rows[0]["required"] == "null" and isinstance(rows[0]["required"], JsonText)
                assert json.loads(rows[2]["j"]) == {"$json": None}
                encoded = [{spec.name: encode_value(row[spec.name], spec) for spec in compiled.columns} for row in rows]
                assert encoded[0] == {"j": None, "required": "null"}
                assert encoded[1]["j"] == "null" and json.loads(encoded[1]["required"]) == {"nested": [None]}
                assert json.loads(encoded[2]["j"]) == {"$json": None}
                assert json.loads(encoded[2]["required"]) == {"$integer": "12"}
            spec = ColumnSpec("j", "json")
            assert _copy_cell(None, spec) == r"\N"
            assert _copy_cell(JsonText("null"), spec) == "null"
            assert _copy_cell({"nested": [None]}, spec) == '{"nested":[null]}'
            exact = JsonText('{"number":9007199254740993.123456789123456789,"nested":[null],"$json":null}')
            assert encode_value(exact, spec) == exact
            assert _copy_cell(exact, spec) == exact
    finally:
        engine.dispose()


def test_copy_preserves_null_empty_escapes_and_binary():
    spec = ColumnSpec("s", "string")
    assert _copy_cell(None, spec) == r"\N"
    assert _copy_cell("", spec) == ""
    assert _copy_cell("\\N\t\n\r中文", spec) == "\\\\N\\t\\n\\r中文"
    assert _copy_cell(b"\x00\xff", ColumnSpec("b", "binary")) == r"\\x00ff"
    with pytest.raises(QueryError): _copy_cell("bad\x00text", spec)


def test_copy_line_budget_counts_utf8_escapes_and_newline():
    import json
    # Success must cross several encoder chunks, not only exercise early rejects.
    values = [None, "", "中文\t\\N" * 10000, {"payload": "\"中\\\n" * 10000}, b"\x00\xff" * 20000]
    specs = [ColumnSpec(str(i), kind) for i, kind in enumerate(["string", "string", "string", "json", "binary"])]
    def escaped(text):
        return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    expected = "\t".join([r"\N", "", escaped(values[2]),
        escaped(json.dumps(values[3], ensure_ascii=False, separators=(",", ":"))), escaped("\\x" + values[4].hex())]) + "\n"
    size = len(expected.encode("utf-8"))
    assert _copy_line(values, specs, size) == (expected, size)
    assert _copy_line(values, specs, size, size_only=True) == ("", size)
    with pytest.raises(QueryError) as exc:
        _copy_line(values, specs, size - 1)
    assert exc.value.extra == {"budget": "copy_buffer_bytes", "limitBytes": size - 1}


@pytest.mark.parametrize("kind", ["string", "json", "binary"])
def test_copy_rejects_giant_cell_before_full_encoded_allocation(kind):
    import tracemalloc
    value = b"x" * (8 * 1024**2) if kind == "binary" else "中\\\n" * (3 * 1024**2)
    if kind == "json":
        import json
        from app.data_channel.integrated.contracts import JsonText
        value = JsonText(json.dumps({"payload": value}, ensure_ascii=False))
    tracemalloc.start()
    try:
        with pytest.raises(QueryError) as exc:
            _copy_line([value], [ColumnSpec("wide", kind)], 65536)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert exc.value.code == "query_budget_exceeded"
    # The source cell predates tracking. No full JSON/hex/UTF-8 encoded copy of
    # that multi-MiB value is allocated while enforcing this 64KiB budget.
    assert peak < 2 * 1024**2
    if kind == "json":
        # The driver's raw JSON string is unavoidable. Do not allocate another
        # whole-cell copy merely to wrap it in JsonText before checking bytes.
        raw = str(value)
        class Result:
            closed = False
            rows = iter([{"wide": raw}])
            def mappings(self): return self
            def fetchone(self): return next(self.rows, None)
            def close(self): self.closed = True
        result = Result()
        service = QueryService(SimpleNamespace(execute=lambda *args: result),
            SimpleNamespace(id="owner"), secret="test", limits=QueryLimits(result_bytes_limit=65536))
        service._platform_budget = lambda: None
        compiled = SimpleNamespace(statement=sa.select(sa.column("wide")), params={},
                                   columns=[ColumnSpec("wide", "json")])
        tracemalloc.start()
        try:
            with pytest.raises(QueryError) as exc:
                list(service._query_rows(compiled, {}, False))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert exc.value.extra["budget"] == "copy_buffer_bytes"
        assert peak < 2 * 1024**2 and result.closed


@pytest.mark.parametrize("note,expected_sizes", [("中" * 6000, [1, 1, 1]), ("tiny", [3])])
def test_platform_stream_fetches_one_and_bounds_buffered_bytes(note, expected_sizes):
    from types import SimpleNamespace
    class Result:
        closed = False
        rows = iter([{"note": note}] * 3)
        def mappings(self): return self
        def fetchone(self): return next(self.rows, None)
        def close(self): self.closed = True
        def fetchmany(self, *args): pytest.fail("row-count-only prefetch is forbidden")
    result = Result()
    class DB:
        def execute(self, statement, params):
            options = statement.get_execution_options()
            assert options["yield_per"] == options["max_row_buffer"] == 1
            return result
    service = QueryService(DB(), SimpleNamespace(id="owner"), secret="test",
                           limits=QueryLimits(result_bytes_limit=32768))
    service._platform_budget = lambda: None
    compiled = SimpleNamespace(statement=sa.select(sa.column("note")), params={},
                               columns=[ColumnSpec("note", "string")])
    batches = list(service._query_rows(compiled, {}, False))
    assert [len(batch) for batch in batches] == expected_sizes
    assert result.closed is True


def test_byte_limited_pages_keep_all_rows_cursor_ordinals_and_replay():
    from datetime import datetime, timedelta, timezone
    from app.data_channel.query.models import UnifiedQuerySnapshot
    import json
    values = [{"id": str(i), "note": text} for i, text in enumerate([
        "a" * 800, "中" * 300, "\t\\\n" * 160, "🙂" * 200, "z" * 850])]
    class Result:
        def __init__(self, data): self.data, self.closed = iter(data), False
        def fetchone(self):
            value = next(self.data, None)
            return (value,) if value is not None else None
        def close(self): self.closed = True
        def all(self): pytest.fail("transport pages must not prefetch all rows")
    class DB:
        results = []
        def execute(self, statement):
            options = statement.get_execution_options()
            assert options["yield_per"] == options["max_row_buffer"] == 1
            start = statement.compile().params["ordinal_1"]
            result = Result(values[start:start + 4])
            self.results.append(result)
            return result
    db = DB()
    service = QueryService(db, SimpleNamespace(id="owner"), secret="test",
                           limits=QueryLimits(result_bytes_limit=2300))
    snapshot = UnifiedQuerySnapshot(id="snapshot", owner_user_id="owner", query_hash="q", auth_fingerprint="a",
        schema_fingerprint="s", columns=[], source_states=[], consistency="immutable_versions", total_rows=5,
        page_size=4, result_limited=False, plan={}, expires_at=datetime.now(timezone.utc) + timedelta(seconds=300))
    pages, combined, start = [], [], 0
    while True:
        page = service._page(snapshot, start)
        assert len(json.dumps({"data": page}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 2300
        assert page["totalRows"] == 5 and page["resultLimited"] is False
        assert page["rows"] and len(page["rows"]) < 4
        combined.extend(page["rows"])
        pages.append(page)
        if not page["page"]["hasMore"]: break
        start = service._decode(page["page"]["cursor"])["next"]
        assert start == len(combined)
    assert combined == values and len(pages) >= 3
    middle_start = service._decode(pages[0]["page"]["cursor"])["next"]
    assert service._page(snapshot, middle_start) == pages[1]
    assert pages[-1]["page"]["cursor"] is None
    assert all(result.closed for result in db.results)
    values[:] = [{"note": "中" * 3000}]
    with pytest.raises(QueryError) as exc: service._page(snapshot, 0)
    assert exc.value.extra == {"budget": "page_bytes", "limitBytes": 2300}
    assert db.results[-1].closed


def test_syntax_error_has_position():
    with pytest.raises(QueryError) as exc: parse("from x as x\n| frobnicate x")
    assert exc.value.code == "invalid_syntax"
    assert exc.value.extra["position"]["line"] == 2


def test_cursor_hmac_binds_payload_and_does_not_expose_query():
    service = QueryService(None, SimpleNamespace(id="user"), secret="unit-secret")
    payload = {"v": 1, "id": "snapshot", "owner": "user", "next": 3}
    token = service._sign(payload)
    assert service._decode(token) == payload
    with pytest.raises(QueryError) as exc: service._decode(token[:-1] + ("0" if token[-1] != "0" else "1"))
    assert exc.value.code == "invalid_cursor"


@pytest.mark.parametrize("sqlstate,status,code", [
    ("57014", 422, "query_budget_exceeded"),
    ("53400", 422, "query_budget_exceeded"),
    ("53100", 422, "query_budget_exceeded"),
    ("53200", 422, "query_budget_exceeded"),
    ("54000", 422, "query_budget_exceeded"),
    ("40001", 409, "query_retry_required"),
    ("40P01", 409, "query_retry_required"),
    ("22003", 422, "invalid_typed_value"),
    ("22P02", 422, "invalid_typed_value"),
    ("XX000", 503, "query_execution_failed"),
    (None, 503, "query_execution_failed"),
])
def test_cursor_fetch_dbapi_error_maps_and_closes_partial_page(sqlstate, status, code):
    from datetime import timedelta
    from sqlalchemy.exc import DBAPIError
    class DriverError(Exception):
        pgcode = sqlstate
    error = DBAPIError("SELECT result page", {}, DriverError("sensitive driver detail"), False)
    cleanup = []
    class Result:
        fetched = 0
        def fetchone(self):
            self.fetched += 1
            if self.fetched == 1:
                return ({"n": "1"},)
            raise error
        def close(self): cleanup.append("result")
    result = Result()
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    snapshot = SimpleNamespace(id="snapshot", owner_user_id="user", query_hash="q",
        auth_fingerprint="a", schema_fingerprint="s", page_size=2, total_rows=2,
        expires_at=expires, columns=[], source_states=[], consistency="immutable_versions",
        result_limited=False, plan={}, request={"language": "spl", "query": "from x as x",
            "bindings": {"x": {"kind": "integrated", "assetId": "asset"}}})
    db = SimpleNamespace(get=lambda *args: snapshot, execute=lambda *args: result,
                         rollback=lambda: cleanup.append("rollback"))
    service = QueryService(db, SimpleNamespace(id="user"), secret="unit-secret")
    service._pin = lambda: None
    service._resolve = lambda *args, **kwargs: None
    service._fingerprints = lambda: ("a", "s")
    service.stack.callback(lambda: cleanup.append("stack"))
    cursor = service._sign({"v": 1, "id": "snapshot", "owner": "user", "q": "q",
                            "a": "a", "s": "s", "next": 0, "size": 2, "exp": expires.timestamp()})
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=cursor))
    assert (exc.value.status, exc.value.code) == (status, code)
    assert exc.value.__cause__ is error
    assert "sensitive" not in str(exc.value.detail())
    assert result.fetched == 2 and cleanup == ["result", "rollback", "stack"]
    if sqlstate is None:
        from app.data_channel.query.service import _query_dbapi_error
        for mysql_code in (3024, 1969):
            source_error = DBAPIError("SELECT remote", {},
                                     Exception(mysql_code, "sensitive driver detail"), False)
            mapped = _query_dbapi_error(source_error)
            assert (mapped.status, mapped.code) == (422, "query_budget_exceeded")
            assert mapped.extra == {"budget": "statement_timeout_ms"}
            assert "sensitive" not in str(mapped.detail())
        unavailable = DBAPIError("SELECT remote", {}, Exception(2013, "lost connection"), False)
        assert _query_dbapi_error(unavailable).status == 503


@pytest.mark.parametrize("page_failure", ["success", "dbapi", "query_error"])
@pytest.mark.parametrize("cleanup_failure", ["dbapi", "generic"])
def test_cursor_fetch_cleanup_failure_closes_sources_and_preserves_primary(page_failure, cleanup_failure):
    from datetime import timedelta
    from sqlalchemy.exc import DBAPIError
    class DriverError(Exception):
        def __init__(self, code):
            super().__init__("sensitive driver detail")
            self.pgcode = code
    for failed_step in ("rollback", "source"):
        cleanup = []
        page_error = (DBAPIError("SELECT page", {}, DriverError("40001"), False)
                      if page_failure == "dbapi" else QueryError(409, "query_context_changed", "Rerun query"))
        cleanup_error = (DBAPIError("CLEANUP", {}, DriverError("57014"), False)
                         if cleanup_failure == "dbapi" else RuntimeError("cleanup failed"))
        def rollback():
            cleanup.append("rollback")
            if failed_step == "rollback": raise cleanup_error
        def close_source():
            cleanup.append("source")
            if failed_step == "source": raise cleanup_error
        def page(*args):
            if page_failure != "success": raise page_error
            return {"rows": [{"n": "1"}]}
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        snapshot = SimpleNamespace(id="snapshot", owner_user_id="user", query_hash="q",
            auth_fingerprint="a", schema_fingerprint="s", page_size=2, total_rows=2,
            expires_at=expires, request={"language": "spl", "query": "from x as x",
                "bindings": {"x": {"kind": "integrated", "assetId": "asset"}}})
        db = SimpleNamespace(get=lambda *args: snapshot, rollback=rollback)
        service = QueryService(db, SimpleNamespace(id="user"), secret="unit-secret")
        service._pin = lambda: None
        service._resolve = lambda *args, **kwargs: None
        service._fingerprints = lambda: ("a", "s")
        service._page = page
        service.stack.callback(close_source)
        service._decode = lambda token: {"id": "snapshot", "owner": "user", "q": "q", "a": "a",
            "s": "s", "size": 2, "next": 0, "exp": expires.timestamp()}
        expected = RuntimeError if page_failure == "success" and cleanup_failure == "generic" else QueryError
        with pytest.raises(expected) as exc:
            service.fetch(TextQueryCursorRequest(cursor="unit-cursor-token"))
        assert cleanup == ["rollback", "source"] and not service.stack._exit_callbacks
        if page_failure == "query_error":
            assert exc.value is page_error
        elif page_failure == "dbapi":
            assert (exc.value.status, exc.value.code) == (409, "query_retry_required")
            assert exc.value.__cause__ is page_error
        elif cleanup_failure == "dbapi":
            assert (exc.value.status, exc.value.code) == (422, "query_budget_exceeded")
            assert exc.value.__cause__ is cleanup_error
        else:
            assert exc.value is cleanup_error
        if isinstance(exc.value, QueryError):
            assert "sensitive" not in str(exc.value.detail())


def test_page_size_is_transport_not_limit():
    request = TextQueryRequest(language="spl", query="from x as x", bindings={"x": {"kind": "integrated", "assetId": "a"}}, pageSize=1000)
    assert request.page_size == 1000
    with pytest.raises(ValidationError): TextQueryRequest(**{**request.model_dump(), "page_size": True})
    with pytest.raises(ValidationError): TextQueryCursorRequest(cursor="x" * 32, pageSize=100)
    assert QueryLimits().statement_timeout_ms == 300000
    assert QueryLimits().staging_bytes_limit == 16 * 1024**3


@pytest.mark.parametrize("raw,expected", [("16GB", 16 * 1024**3), ("16384MB", 16 * 1024**3), ("4096", 4096 * 1024), ("-1", None), ("0", 0)])
def test_pg_memory_settings(raw, expected): assert _memory_bytes(raw) == expected


def test_physical_column_mapping_does_not_eager_lookup_missing_name():
    table = sa.table("stage", sa.column("c0", sa.BigInteger))
    relation = Relation("o", table, {"id": ColumnSpec("id", "integer")}, {}, "a", "s", "p",
                        expressions={"id": table.c.c0}, platform=True)
    compiled = compile_query(parse("from x as o | fields o.id"), {"o": relation}, {})
    assert "c0" in str(compiled.statement)


def test_mysql_decimal_aggregate_does_not_cast_to_implicit_decimal_scale(relations):
    compiled = compile_("from orders as o | stats sum(o.amount) as n", relations, dialect="mysql")
    sql = str(compiled.statement.compile(dialect=mysql.dialect()))
    assert "CAST" not in sql and "sum(" in sql


def test_source_output_quota_is_explicit_not_input_clipping(relations):
    service = QueryService(None, SimpleNamespace(id="user"), secret="unit-secret")
    service.relations = {"o": relations["o"]}
    relations["o"].state["maxPageSize"] = 2
    service._page_scope(2)
    with pytest.raises(QueryError) as exc: service._page_scope(3)
    assert exc.value.status == 422 and exc.value.code == "query_page_size_exceeds_scope"
    assert exc.value.extra == {"alias": "o", "maxPageSize": 2}


def test_unsigned_integer_contract_uses_exact_numeric():
    table = sa.table("wide", sa.column("id", sa.Numeric(20, 0)))
    relation = Relation("o", table, {"id": ColumnSpec("id", "integer", unsigned=True)}, {}, "a", "s", "p", platform=True)
    compiled = compile_query(parse("from x as o | where o.id=:id | fields o.id"), {"o": relation}, {"id": "18446744073709551615"})
    assert compiled.params == {"qv_0": 18446744073709551615}
    assert isinstance(relation.columns["id"].sql_type(), sa.Numeric)
    assert encode_value(Decimal("18446744073709551615"), relation.columns["id"]) == "18446744073709551615"
    with pytest.raises(QueryError):
        compile_query(parse("from x as o | where o.id=:id"), {"o": relation}, {"id": "18446744073709551616"})


def test_mysql_null_sort_is_portable(relations):
    compiled = compile_("from orders as o | fields o.note | sort note desc nulls first", relations, dialect="mysql")
    sql = str(compiled.statement.compile(dialect=mysql.dialect()))
    assert "NULLS" not in sql and "CASE" in sql and "BINARY" in sql
