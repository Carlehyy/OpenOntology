"""Opt-in real PostgreSQL proof of pinned lake reads versus same-count upsert.

Use only the explicitly owned integrated fixture stack from a sanitized runtime
copy. Each test owns a UUID schema; no shared production relations are touched.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import get_ident

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.database import Base
from app.data_channel.datasets import lake_store
from app.data_channel.datasets.models import Dataset


pytestmark = pytest.mark.skipif(os.environ.get("INTEGRATED_NATIVE_PG_E2E") != "1",
                                reason="requires explicitly owned loopback PostgreSQL fixture stack")


@pytest.mark.parametrize("reader_port", ["iter", "rows", "page"])
def test_pinned_read_blocks_same_count_value_upsert_before_base_mutation(monkeypatch, reader_port):
    token = uuid.uuid4().hex
    schema = "lake_race_" + token
    platform_url = "postgresql+psycopg2://integrated_e2e:integrated_e2e_fixture@127.0.0.1:45432/integrated_metadata"
    root = sa.create_engine(platform_url, hide_parameters=True)
    engine = sa.create_engine(platform_url, hide_parameters=True, connect_args={
        "options": "-csearch_path=" + schema + " -cstatement_timeout=10000",
    })
    created = False
    try:
        with root.begin() as conn:
            conn.execute(sa.schema.CreateSchema(schema))
            created = True
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            dataset = Dataset(id=str(uuid.uuid4()), name="pinned race fixture", kind="curated",
                              schema_json={"primary_key": "id", "columns": ["id", "name"]})
            db.add(dataset)
            db.commit()
            dataset_id = dataset.id
            lake_store.upsert_run(db, dataset, [{"id": "1", "name": "v1"}], "overwrite", ["id"])

        def writer():
            with Session(engine) as db:
                db.execute(sa.text("SET LOCAL lock_timeout = '250ms'"))
                dataset = db.get(Dataset, dataset_id)
                try:
                    lake_store.upsert_run(db, dataset, [{"id": "1", "name": "v2"}], "upsert", ["id"])
                except OperationalError as exc:
                    # DB lock timeout, not a timing guess about a slow thread.
                    return exc.orig.pgcode
                return "published-with-reader-active"

        with Session(engine) as reader, ThreadPoolExecutor(max_workers=1) as pool:
            dataset = reader.get(Dataset, dataset_id)
            attempted = False
            reader_thread = get_ident()
            writer_base_mutations = []

            def capture_writer_mutations(_conn, _cursor, statement, _params, _context, _many):
                verb = statement.lstrip().split(None, 1)[0].upper()
                if (get_ident() != reader_thread and "lake_ds_" in statement
                        and verb in {"INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE", "TRUNCATE"}):
                    writer_base_mutations.append(verb)

            sa.event.listen(engine, "before_cursor_execute", capture_writer_mutations)

            def concurrent_writer_before_base(*_args, **_kwargs):
                nonlocal attempted
                if get_ident() == reader_thread and not attempted:
                    attempted = True
                    assert pool.submit(writer).result(timeout=5) == "55P03"
                    # A late lock at _publish_run also times out, but by then the
                    # mutable base was already changed. It is not a valid fence.
                    assert writer_base_mutations == []

            # Pause after the later-changeset query returned [] and immediately
            # before reading the current table: the exact formerly unsafe window.
            original_readable = lake_store._readable_table

            def paused_readable(*args, **kwargs):
                concurrent_writer_before_base()
                return original_readable(*args, **kwargs)

            monkeypatch.setattr(lake_store, "_readable_table", paused_readable)
            if reader_port == "iter":
                actual = [row for batch in lake_store.iter_rows_at_version(reader, dataset, 1, batch_size=1)
                          for row in batch]
            elif reader_port == "rows":
                actual = lake_store.rows_at_version(reader, dataset, 1)
            else:
                actual, count = lake_store.page_rows_at_version(reader, dataset, 1)
                assert count == 1
            assert attempted and actual == [{"id": "1", "name": "v1"}]
            monkeypatch.setattr(lake_store, "_readable_table", original_readable)
            reader.rollback()  # explicitly release the shared Dataset lock

        # With the reader transaction finished, the same write must succeed.
        with Session(engine) as db:
            dataset = db.get(Dataset, dataset_id)
            version, _ = lake_store.upsert_run(db, dataset, [{"id": "1", "name": "v2"}], "upsert", ["id"])
            assert version.version_no == 2 and version.rowcount == 1
            assert lake_store.page_rows(db, dataset) == [{"id": "1", "name": "v2"}]
            assert [row for batch in lake_store.iter_rows_at_version(db, dataset, 1) for row in batch] == [
                {"id": "1", "name": "v1"}]
    finally:
        engine.dispose()
        assert schema == "lake_race_" + token and uuid.UUID(token).hex == token
        if created:
            with root.begin() as conn:
                conn.execute(sa.schema.DropSchema(schema, cascade=True))
        root.dispose()


def test_integrated_physical_server_cursor_seals_and_reads_wide_rows():
    from app.auth.models import User
    from app.data_channel.connections.models import Connection
    from app.data_channel.datasets import integrated_store as store
    from app.data_channel.integrated.contracts import digest
    from app.data_channel.integrated.models import IntegratedAsset

    token = uuid.uuid4().hex
    schema = "lake_stream_" + token
    platform_url = "postgresql+psycopg2://integrated_e2e:integrated_e2e_fixture@127.0.0.1:45432/integrated_metadata"
    root = sa.create_engine(platform_url, hide_parameters=True)
    engine = sa.create_engine(platform_url, hide_parameters=True,
                              connect_args={"options": "-csearch_path=" + schema})
    created = False
    streams = []

    def verify_cursor(_conn, cursor, _stmt, _params, context, _many):
        if context.execution_options.get("stream_results"):
            assert cursor.name  # psycopg2 named cursor, not client-buffered SELECT
            assert context.execution_options["yield_per"] == 1
            assert context.execution_options["max_row_buffer"] == 1
            streams.append(cursor.name)

    sa.event.listen(engine, "after_cursor_execute", verify_cursor)
    try:
        with root.begin() as conn:
            conn.execute(sa.schema.CreateSchema(schema))
            created = True
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            actor = User(id=str(uuid.uuid4()), username="stream_" + token, email=token + "@example.test",
                         role="admin", is_active=True, password_hash="not-a-login-fixture")
            connection = Connection(id=str(uuid.uuid4()), name="stream fixture", kind="postgres", config={})
            db.add_all([actor, connection])
            db.flush()
            dataset = Dataset(id=str(uuid.uuid4()), name="stream fixture", kind="integrated",
                              source_connection_id=connection.id, source_resource="public.fixture")
            db.add(dataset)
            db.flush()
            columns = [{"name": name, "type": kind, "source_type": source_type, "nullable": False}
                       for name, kind, source_type in [("id", "integer", "BIGINT"), ("payload", "string", "TEXT")]]
            fingerprint = digest({"columns": columns, "primary_key": ["id"]})
            asset = IntegratedAsset(id=str(uuid.uuid4()), dataset_id=dataset.id, connection_id=connection.id,
                                    name="stream fixture", schema_name="public", table_name="fixture",
                                    schema_json={"columns": columns, "primary_key": ["id"]},
                                    schema_fingerprint=fingerprint, writable=False, created_by=actor.id)
            db.add(asset)
            db.commit()
            descriptor = {"asset_id": asset.id, "dataset_id": dataset.id, "connection_id": connection.id,
                          "resource": "public.fixture", "schema": columns, "primary_key": ["id"],
                          "schema_fingerprint": fingerprint, "expected_rows": 1001,
                          "snapshot": {"id": str(uuid.uuid4()), "consistent": True, "kind": "repeatable_read",
                                       "started_at": "2026-10-24T08:00:00+00:00"}}
            run, gate = store.begin_snapshot(db, descriptor)
            checksum = store.snapshot_hasher(run)
            payload = "宽" * 10923  # ~32 KiB UTF-8: a 1000-row batch exceeds 16 MiB
            store.write_snapshot_batch(db, run, gate, [{"id": i, "payload": payload} for i in range(1000)], checksum)
            store.write_snapshot_batch(db, run, gate, [{"id": 1000, "payload": payload}], checksum)
            version = store.seal_snapshot(db, run, {"complete": True, "row_count": 1001,
                "schema_fingerprint": fingerprint, "snapshot_id": run.snapshot_json["id"]}, checksum.hexdigest())
            assert streams  # seal's full ordered checksum uses a named cursor too
            seen = 0
            for batch in store.iter_version_rows(db, dataset.id, version.id):
                assert len(batch) < 1000
                assert sum(store._physical_row_bytes(row) for row in batch) <= store.MAX_BATCH_BYTES
                assert all(row["payload"] == payload for row in batch)
                seen += len(batch)
            assert seen == 1001 and len(streams) >= 2
    finally:
        engine.dispose()
        assert schema == "lake_stream_" + token and uuid.UUID(token).hex == token
        if created:
            with root.begin() as conn:
                conn.execute(sa.schema.DropSchema(schema, cascade=True))
        root.dispose()
