"""Real PostgreSQL JSON null fidelity; not dbx/browser/full-chain acceptance.

Run only from an immutable sanitized snapshot with the explicit owned-stack
opt-in. ORM registration is deliberate fixture setup, not genuine dbx evidence.
The sync, native and staged query paths below are the actual service adapters.
"""
from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.auth.models import User
from app.database import Base
from app.data_channel.connections.models import Connection
from app.data_channel.datasets import integrated_store
from app.data_channel.datasets.models import Dataset
from app.data_channel.integrated import native, service
from app.data_channel.integrated.contracts import JsonText, RowMutation
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.query.contracts import TextQueryRequest
from app.data_channel.query.service import QueryService
from app.data_channel.sync_tasks.integrated_sync import sync_integrated_asset

pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATED_NATIVE_PG_E2E") != "1",
    reason="requires explicitly owned loopback PostgreSQL fixture stack",
)


def _owned_url(name, port, database):
    raw = os.environ.get(name)
    if not raw:
        pytest.fail(f"{name} is required; no default/production fallback")
    url = sa.engine.make_url(raw)
    if url.host != "127.0.0.1" or url.port != port or url.database != database:
        pytest.fail(f"{name} must point to the owned loopback E2E database")
    return raw


def test_real_pg_json_sql_null_survives_native_seal_staging_and_cas():
    token = uuid.uuid4().hex
    schema = "source_json_" + token
    source_engine = sa.create_engine(
        _owned_url("INTEGRATED_QUERY_POSTGRES_DSN", 45433, "integrated_source"),
        hide_parameters=True,
    )
    platform_url = _owned_url("INTEGRATED_QUERY_PLATFORM_DSN", 45432, "integrated_metadata")
    platform_root = sa.create_engine(platform_url, hide_parameters=True)
    platform = sa.create_engine(
        platform_url, connect_args={"options": "-csearch_path=" + schema}, hide_parameters=True,
    )
    source_created = platform_created = False
    try:
        with source_engine.begin() as conn:
            conn.execute(sa.schema.CreateSchema(schema))
            source_created = True
        with platform_root.begin() as conn:
            conn.execute(sa.schema.CreateSchema(schema))
            platform_created = True
        # Private-schema fixture DDL, not a claim that migrations were exercised.
        Base.metadata.create_all(platform)
        source = sa.Table(
            "rows", sa.MetaData(), sa.Column("id", sa.BigInteger, primary_key=True),
            sa.Column("j", sa.JSON, nullable=True),
            sa.Column("jrequired", sa.JSON, nullable=False), sa.Column("note", sa.Text),
            schema=schema,
        )
        source.create(source_engine)
        collision = {"$json": None}
        precise = "9007199254740993.123456789123456789"
        precise_json = '{"n":' + precise + ',"nested":[null]}'
        with source_engine.begin() as conn:
            # SQL NULL must be explicit: Python None defaults to JSON literal null
            # for SQLAlchemy JSON, which would make this a false-positive fixture.
            conn.execute(source.insert().values(id=1, j=sa.null(), jrequired=sa.JSON.NULL, note="initial"))
            conn.execute(source.insert().values(
                id=2, j=sa.JSON.NULL, jrequired=sa.cast(sa.literal(precise_json), sa.JSON), note="initial",
            ))
            conn.execute(source.insert().values(id=3, j=collision, jrequired=collision, note="initial"))
            assert conn.execute(sa.select(sa.func.count(source.c.j), sa.func.count(source.c.jrequired))).one() == (2, 3)
            assert conn.execute(sa.select(source.c.id).where(source.c.j.is_(None))).scalars().all() == [1]
            raw = conn.execute(sa.select(source.c.id, sa.cast(source.c.j, sa.Text)).order_by(source.c.id)).all()
            assert raw[0] == (1, None) and raw[1] == (2, "null")
            assert json.loads(raw[2][1]) == collision

        with Session(platform) as db:
            actor = User(
                id=str(uuid.uuid4()), username="json_" + token, email=token + "@example.test",
                role="admin", is_active=True, password_hash="not-a-login-fixture",
            )
            db.add(actor)
            db.flush()
            connection = Connection(
                id=str(uuid.uuid4()), name="JSON null fixture", kind="postgres",
                config={"host": "127.0.0.1", "port": 45433, "database": "integrated_source",
                        "user": "integrated_e2e", "password": "integrated_e2e_fixture"},
                created_by=actor.id,
            )
            db.add(connection)
            db.flush()
            dataset = Dataset(
                id=str(uuid.uuid4()), name="JSON null fixture", kind="integrated",
                source_connection_id=connection.id, source_resource=schema + ".rows",
            )
            db.add(dataset)
            db.flush()
            with source_engine.connect() as conn:
                _, columns, fingerprint = native.reflect(conn, SimpleNamespace(schema_name=schema, table_name="rows"))
            specs = {column["name"]: column for column in columns["columns"]}
            assert specs["j"]["nullable"] is True and specs["jrequired"]["nullable"] is False
            asset = IntegratedAsset(
                id=str(uuid.uuid4()), dataset_id=dataset.id, connection_id=connection.id,
                name="JSON null fixture", schema_name=schema, table_name="rows", writable=True,
                schema_json=columns, schema_fingerprint=fingerprint, created_by=actor.id,
            )
            db.add(asset)
            db.commit()
            # Keep scalar identities across QueryService's transaction resets.
            asset_id, dataset_id = asset.id, dataset.id
            with native.open_read_scope(db, actor, [asset_id]) as scope:
                rows = [row for batch in scope.iter_batches(
                    sa.select(scope.tables[asset_id]).order_by(scope.tables[asset_id].c.id), batch_size=1,
                ) for row in batch]
                assert rows[0]["j"] is None
                assert isinstance(rows[1]["j"], JsonText) and rows[1]["j"] == "null"
                assert isinstance(rows[0]["jrequired"], JsonText) and rows[0]["jrequired"] == "null"
                assert precise in rows[1]["jrequired"]
                assert json.loads(rows[1]["jrequired"], parse_float=Decimal) == {
                    "n": Decimal(precise), "nested": [None],
                }
                assert json.loads(rows[2]["j"]) == json.loads(rows[2]["jrequired"]) == collision
                assert scope.completion["complete"] is True
                assert scope.completion["row_count"] == 3
            preview = service.preview_rows(db, actor, asset_id, 10)
            assert preview["source_state"]["complete"] is True
            assert {column["encoding"] for column in preview["columns"] if column["type"] == "json"} == {"json_text"}
            assert preview["rows"][0]["values"]["j"] is None
            assert preview["rows"][1]["values"]["j"] == "null"
            assert preview["rows"][0]["values"]["jrequired"] == "null"
            assert precise in preview["rows"][1]["values"]["jrequired"]
            assert json.loads(preview["rows"][2]["values"]["j"]) == collision
            old_token = preview["rows"][0]["row_token"]

            sealed = sync_integrated_asset(db, asset_id, user=actor, batch_size=1)
            assert sealed["status"] == "sealed" and sealed["complete"] is True
            assert sealed["row_count"] == 3
            version_id = sealed["version_id"]
            canonical = [row for batch in integrated_store.iter_version_rows(
                db, dataset_id, version_id, batch_size=1,
            ) for row in batch]
            assert canonical[0]["j"] is None and canonical[1]["j"] == "null"
            assert canonical[0]["jrequired"] == "null"
            assert json.loads(canonical[2]["j"]) == collision
            assert json.loads(canonical[2]["jrequired"]) == collision
            decoded = [integrated_store.decode_version_row(row, columns["columns"]) for row in canonical]
            assert decoded[0]["j"] is None
            assert isinstance(decoded[1]["j"], JsonText) and decoded[1]["j"] == "null"
            assert decoded[0]["jrequired"] == "null"
            assert precise in decoded[1]["jrequired"]
            assert json.loads(decoded[2]["j"]) == collision

            live = {"kind": "integrated", "assetId": asset_id}
            frozen = {"kind": "dataset", "datasetId": dataset_id, "versionId": version_id}

            def query(text, bindings):
                return QueryService(db, actor, secret="public-json-fixture-secret").execute(
                    TextQueryRequest(language="spl", query=text, bindings=bindings, pageSize=10),
                )

            def assert_json_text(data, nullable_name, required_name):
                output_specs = {column["name"]: column for column in data["columns"]}
                assert output_specs[nullable_name]["encoding"] == "json_text"
                assert output_specs[required_name]["encoding"] == "json_text"
                values = data["rows"]
                assert values[0][nullable_name] is None and values[1][nullable_name] == "null"
                assert values[0][required_name] == "null"
                assert precise in values[1][required_name]
                assert json.loads(values[1][required_name], parse_float=Decimal) == {
                    "n": Decimal(precise), "nested": [None],
                }
                assert json.loads(values[2][nullable_name]) == collision
                assert json.loads(values[2][required_name]) == collision

            # Three independent physical paths: native source SQL; immutable
            # version SQL; and COPY staging a live source joined to that version.
            for binding in (live, frozen):
                count = query("from one as s | stats count() as rows, count(s.j) as present, "
                              "count(s.jrequired) as required", {"one": binding})
                assert count["rows"] == [{"rows": "3", "present": "2", "required": "3"}]
                nulls = query("from one as s | where s.j is null | fields s.id | sort id asc", {"one": binding})
                assert nulls["rows"] == [{"id": "1"}]
                projected = query("from one as s | fields s.id, s.j, s.jrequired | sort id asc", {"one": binding})
                assert_json_text(projected, "j", "jrequired")

            joined = "from live as l | join frozen as f on l.id=f.id "
            bindings = {"live": live, "frozen": frozen}
            count = query(joined + "| stats count() as rows, count(l.j) as live_present, "
                          "count(f.j) as frozen_present, count(l.jrequired) as live_required, "
                          "count(f.jrequired) as frozen_required", bindings)
            assert count["plan"]["strategy"] == "postgresql_staged_sql"
            assert count["rows"] == [{"rows": "3", "live_present": "2", "frozen_present": "2",
                                      "live_required": "3", "frozen_required": "3"}]
            nulls = query(joined + "| where l.j is null and f.j is null | fields l.id | sort id asc", bindings)
            assert nulls["rows"] == [{"id": "1"}]
            projected = query(joined + "| fields l.id, l.j as live_j, f.j as frozen_j, "
                              "l.jrequired as live_required, f.jrequired as frozen_required | sort id asc", bindings)
            assert_json_text(projected, "live_j", "live_required")
            assert_json_text(projected, "frozen_j", "frozen_required")

            # Actual CAS read path: changing only SQL NULL -> JSON literal null
            # must alter the token and reject an otherwise valid stale update.
            with source_engine.begin() as conn:
                conn.execute(source.update().where(source.c.id == 1).values(j=sa.JSON.NULL))
            current = service.preview_rows(db, actor, asset_id, 10)["rows"][0]
            assert current["row_token"] != old_token
            stale = RowMutation(key={"id": 1}, values={"note": "stale"},
                                expected_row_token=old_token, idempotency_key="stale-json-null")
            with pytest.raises(HTTPException) as exc:
                service.mutate_row(db, actor, asset_id, "update", stale)
            assert exc.value.detail["code"] == "ROW_CONFLICT"
            applied = service.mutate_row(db, actor, asset_id, "update", stale.model_copy(update={
                "expected_row_token": current["row_token"], "idempotency_key": "current-json-null",
                "values": {"note": "current"},
            }))
            assert applied["status"] == "applied"
            with source_engine.connect() as conn:
                assert conn.execute(sa.select(sa.func.count(source.c.j))).scalar_one() == 3
                assert conn.execute(sa.select(source.c.note).where(source.c.id == 1)).scalar_one() == "current"

            # Exercise the actual reflected JSON request decoder and driver
            # binder, not just a note update protected by a JSON-aware token.
            # On the wire string "null" is JSON null; None is SQL NULL.
            sql_null = service.mutate_row(db, actor, asset_id, "update", RowMutation(
                key={"id": 1}, values={"j": None},
                expected_row_token=service.preview_rows(db, actor, asset_id, 10)["rows"][0]["row_token"],
                idempotency_key="json-to-sql-null",
            ))
            assert sql_null["status"] == "applied"
            with source_engine.connect() as conn:
                assert conn.execute(sa.select(source.c.id).where(source.c.j.is_(None))).scalars().all() == [1]
                assert conn.execute(sa.select(sa.func.count(source.c.j))).scalar_one() == 2
            json_null = service.mutate_row(db, actor, asset_id, "update", RowMutation(
                key={"id": 1}, values={"j": "null"},
                expected_row_token=service.preview_rows(db, actor, asset_id, 10)["rows"][0]["row_token"],
                idempotency_key="sql-to-json-null",
            ))
            assert json_null["status"] == "applied"
            with source_engine.connect() as conn:
                assert conn.execute(sa.select(source.c.id).where(source.c.j.is_(None))).scalars().all() == []
                assert conn.execute(sa.select(sa.func.count(source.c.j))).scalar_one() == 3
                assert conn.execute(sa.select(sa.cast(source.c.j, sa.Text)).where(source.c.id == 1)).scalar_one() == "null"
            assert service.preview_rows(db, actor, asset_id, 10)["rows"][0]["values"]["j"] == "null"
            still_frozen = [row for batch in integrated_store.iter_version_rows(
                db, dataset_id, version_id, batch_size=1,
            ) for row in batch]
            assert still_frozen == canonical
    finally:
        platform.dispose()
        assert schema == "source_json_" + token and uuid.UUID(token).hex == token
        if source_created:
            with source_engine.begin() as conn:
                conn.execute(sa.schema.DropSchema(schema, cascade=True))
        if platform_created:
            with platform_root.begin() as conn:
                conn.execute(sa.schema.DropSchema(schema, cascade=True))
        source_engine.dispose()
        platform_root.dispose()
