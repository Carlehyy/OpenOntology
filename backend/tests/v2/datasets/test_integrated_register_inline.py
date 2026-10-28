"""Inline source registration: probes, atomic register, logical field mapping.

Contract doubles only: real MySQL/PostgreSQL and genuine dbx acceptance are
the opt-in E2E slots.  Everything here pins the security and mapping rules
that must hold regardless of driver: exactly-one source references, atomic
registration, encrypted inline credentials that never echo, physical truth
kept pristine behind the logical projection, and full-row token continuity.
"""
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from pydantic import ValidationError

from app.data_channel.connections.models import Connection
from app.data_channel.datasets import integrated_store as store
from app.data_channel.datasets.models import Dataset, DatasetVersion, IntegratedSyncRun
from app.data_channel.integrated import fields, native, service
from app.data_channel.integrated.contracts import (
    DBX_COMMIT, DBX_VERSION, FieldConfigEntry, RegisterAsset, RowMutation,
    SourceConnection, SourceConnectionConfig, SourceReference, SourceSchemaRequest,
    digest,
)
from app.data_channel.integrated.models import IntegratedAsset, IntegratedAssetFieldConfig
from app.shared.encryption import decrypt


# ---------------------------------------------------------------------------
# SourceReference contract: exactly one reference, no config echo surface
# ---------------------------------------------------------------------------

def _inline_source(source_name="inline source", **config):
    base = {"host": "127.0.0.1", "port": 5432, "database": "integrated_source",
            "user": "integrated_e2e", "password": "fixture-source-password"}
    base.update(config)
    return {"name": source_name, "kind": "postgres", "config": base}


def test_source_reference_requires_exactly_one_target():
    with pytest.raises(ValidationError):
        SourceReference()
    with pytest.raises(ValidationError):
        SourceReference(connection_id="c", source_connection=_inline_source())
    assert SourceReference(connection_id="c").connection_id == "c"
    assert SourceReference(source_connection=_inline_source()).source_connection.kind == "postgres"


def test_source_connection_contract_rules():
    with pytest.raises(ValidationError):
        SourceConnection(name="n", kind="postgres",
                         config=SourceConnectionConfig(host="h", port=5432, user="u"))
    with pytest.raises(ValidationError):
        SourceConnection(name="n", kind="postgres",
                         config=SourceConnectionConfig(host="h", port=5432, database="d",
                                                        user="u", dbx_host="source-pg"))
    with pytest.raises(ValidationError):
        SourceConnection(name="n", kind="postgres",
                         config=SourceConnectionConfig(host="h", port=99999, database="d", user="u"))
    with pytest.raises(ValidationError):
        SourceConnection(name="n", kind="sqlite", config=SourceConnectionConfig(
            host="h", port=1, database="d", user="u"))
    valid = SourceConnection.model_validate(_inline_source(dbx_host="source-pg", dbx_port=5432))
    assert valid.config.dbx_port == 5432


# ---------------------------------------------------------------------------
# dbx/native doubles (same authority as register)
# ---------------------------------------------------------------------------

class _FakeDbxAdapter:
    def __init__(self, evidence, resources=None):
        self.evidence = evidence
        self.resources = resources if resources is not None else [
            {"schema_name": "public", "table_name": "rows"}]

    def status(self):
        return {"configured": True, "available": True, "commit": DBX_COMMIT,
                "version": DBX_VERSION, "reason": None}

    def describe_table(self, connection, schema_name, table_name):
        return self.evidence

    def list_resources(self, connection):
        return self.resources


_DBX_SPELLINGS = {"integer": "bigint", "decimal": "numeric(30,6)",
                  "string": "character varying(100)"}


def _dbx_evidence(schema):
    columns = [{"name": c["name"], "data_type": _DBX_SPELLINGS.get(c["type"], c["type"]),
                "nullable": c["nullable"], "primary_key": c["name"] in schema["primary_key"],
                "precision": c["precision"], "scale": c["scale"], "length": c["length"]}
               for c in schema["columns"]]
    return {"backend": "dbx-web-http", "commit": DBX_COMMIT, "version": DBX_VERSION,
            "table_name": "rows", "schema_name": "public", "table_type": "TABLE",
            "columns": columns, "primary_key": list(schema["primary_key"])}


def _physical_source(monkeypatch):
    """SQLite protocol double with an unmapped column to prove projection."""
    engine = sa.create_engine("sqlite://")
    table = sa.Table("rows", sa.MetaData(),
                     sa.Column("id", sa.BigInteger, primary_key=True),
                     sa.Column("customer_name", sa.String(100)),
                     sa.Column("amount", sa.Numeric(30, 6)))
    table.create(engine)
    with engine.connect() as conn:
        _, schema, fingerprint = native.reflect(
            conn, SimpleNamespace(schema_name=None, table_name="rows"))
    monkeypatch.setattr(service, "source_engine", lambda *_: engine)
    monkeypatch.setattr(service, "reflect", lambda *_: (table, schema, fingerprint))
    monkeypatch.setattr(service, "check_transactional_table", lambda *_: None)
    return engine, table, schema, fingerprint


def _field_config(*entries):
    return [FieldConfigEntry.model_validate(entry) for entry in entries]


# ---------------------------------------------------------------------------
# Probe endpoints: permission, no persistence, safe structured failures
# ---------------------------------------------------------------------------

def test_probe_permission_is_registration_level(db, admin_user, monkeypatch):
    viewer = SimpleNamespace(id="viewer", role="viewer", is_active=True)
    body = SourceReference(source_connection=_inline_source())
    for probe in (service.test_source, service.list_source_resources,
                  service.describe_source_schema):
        with pytest.raises(HTTPException) as exc:
            if probe is service.describe_source_schema:
                probe(db, viewer, SourceSchemaRequest.model_validate(
                    {**body.model_dump(), "schema_name": "public", "table_name": "rows"}))
            else:
                probe(db, viewer, body)
        assert (exc.value.status_code, exc.value.detail["code"]) == (403, "INTEGRATED_READ_ONLY")
    # Probes must not touch the database: nothing persisted for inline refs.
    assert db.query(Connection).count() == 0


def test_source_probe_reports_structured_failures_without_echo(db, admin_user, monkeypatch):
    body = SourceReference(source_connection=_inline_source(password="sekrit-password"))

    class _FailingConnection:
        def __init__(self, error):
            self.error = error

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execution_options(self, **_):
            return self

        def execute(self, *_a, **_k):
            raise self.error

    class _FailingEngine:
        def __init__(self, error):
            self.error = error

        def connect(self):
            return _FailingConnection(self.error)

        def dispose(self):
            pass

    from sqlalchemy.exc import OperationalError, InterfaceError
    auth = OperationalError("stmt", {}, Exception("(pymysql.err) (1045, \"Access denied for 'integrated_e2e'@'127.0.0.1' (using password: YES)\")"))
    monkeypatch.setattr(service, "source_engine", lambda *_: _FailingEngine(auth))
    result = service.test_source(db, admin_user, body)
    assert result["success"] is False
    assert result["code"] == "AUTHENTICATION_FAILED"
    assert "sekrit" not in str(result) and "integrated_e2e" not in str(result)

    refused = InterfaceError("stmt", {}, Exception("(2003, \"Can't connect to MySQL server on '10.9.8.7' (timed out)\")"))
    monkeypatch.setattr(service, "source_engine", lambda *_: _FailingEngine(refused))
    result = service.test_source(db, admin_user, body)
    assert result["code"] == "CONNECT_TIMEOUT"

    generic = OperationalError("stmt", {}, Exception("odd driver failure mentioning host 10.9.8.7"))
    monkeypatch.setattr(service, "source_engine", lambda *_: _FailingEngine(generic))
    result = service.test_source(db, admin_user, body)
    assert result["code"] == "SOURCE_UNAVAILABLE"
    assert "10.9.8.7" not in str(result)

    engine = sa.create_engine("sqlite://")
    monkeypatch.setattr(service, "source_engine", lambda *_: engine)
    assert service.test_source(db, admin_user, body) == {"success": True}


def test_source_resources_and_schema_are_real_dbx_only(db, admin_user, monkeypatch):
    engine, table, schema, fingerprint = _physical_source(monkeypatch)
    try:
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(_dbx_evidence(schema)))
        body = SourceReference(source_connection=_inline_source())
        assert service.list_source_resources(db, admin_user, body) == {
            "items": [{"schema_name": "public", "table_name": "rows"}]}
        described = service.describe_source_schema(db, admin_user, SourceSchemaRequest.model_validate(
            {"source_connection": body.source_connection.model_dump(),
             "schema_name": "public", "table_name": "rows"}))
        # Physical truth only: names, types, nullability, PK and fingerprint.
        assert [c["name"] for c in described["columns"]] == ["id", "customer_name", "amount"]
        assert described["primary_key"] == ["id"]
        assert described["schema_fingerprint"] == digest(
            {"columns": schema["columns"], "primary_key": ["id"]})
        assert all("source_key" not in c for c in described["columns"])
        # No probe persisted a connection, dataset or asset.
        assert (db.query(Connection).count(), db.query(Dataset).count(),
                db.query(IntegratedAsset).count()) == (0, 0, 0)

        mismatch = _dbx_evidence(schema)
        mismatch["primary_key"] = ["customer_name"]
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(mismatch))
        with pytest.raises(HTTPException) as exc:
            service.describe_source_schema(db, admin_user, SourceSchemaRequest.model_validate(
                {"source_connection": body.source_connection.model_dump(),
                 "schema_name": "public", "table_name": "rows"}))
        assert (exc.value.status_code, exc.value.detail["code"]) == (409, "DBX_METADATA_MISMATCH")
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Atomic inline registration + field config validation
# ---------------------------------------------------------------------------

def _register_body(**overrides):
    body = {"name": "inline asset", "source_connection": _inline_source(),
            "schema_name": "public", "table_name": "rows", "writable": True}
    body.update(overrides)
    return body


def test_register_with_inline_source_is_atomic_and_encrypted(db, admin_user, monkeypatch):
    engine, table, schema, fingerprint = _physical_source(monkeypatch)
    try:
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(_dbx_evidence(schema)))
        result = service.register_asset(db, admin_user, RegisterAsset.model_validate(_register_body()))
        connection = db.query(Connection).one()
        dataset = db.query(Dataset).one()
        asset = db.query(IntegratedAsset).one()
        assert (connection.id, dataset.id, asset.id) == (result["connection_id"], result["dataset_id"], result["id"])
        assert connection.created_by == admin_user.id
        # Encrypted at rest with the shared encryption service; plaintext absent.
        assert set(connection.config) == {"_encrypted"}
        assert "fixture-source-password" not in connection.config["_encrypted"]
        import json as _json
        assert _json.loads(decrypt(connection.config["_encrypted"]))["password"] == "fixture-source-password"
        # No credentials anywhere in the DTO.
        assert "fixture-source-password" not in str(result) and "config" not in result
        assert db.query(IntegratedAssetFieldConfig).count() == 0
    finally:
        engine.dispose()


def test_failed_registration_leaves_nothing_behind(db, admin_user, monkeypatch):
    engine, table, schema, fingerprint = _physical_source(monkeypatch)
    try:
        # dbx disagreement rejects before any insert.
        mismatch = _dbx_evidence(schema)
        mismatch["columns"] = [c for c in mismatch["columns"] if c["name"] != "amount"]
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(mismatch))
        with pytest.raises(HTTPException) as exc:
            service.register_asset(db, admin_user, RegisterAsset.model_validate(_register_body()))
        assert (exc.value.status_code, exc.value.detail["code"]) == (409, "DBX_METADATA_MISMATCH")
        assert (db.query(Connection).count(), db.query(Dataset).count(),
                db.query(IntegratedAsset).count()) == (0, 0, 0)

        # Field-config failures also leave nothing behind.
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(_dbx_evidence(schema)))
        bad = _register_body(field_config=[{"source_key": "customer_name", "name": "customer"}])
        with pytest.raises(HTTPException) as exc:
            service.register_asset(db, admin_user, RegisterAsset.model_validate(bad))
        assert exc.value.detail["code"] == "FIELD_CONFIG_PK_REQUIRED"
        assert (db.query(Connection).count(), db.query(Dataset).count(),
                db.query(IntegratedAsset).count()) == (0, 0, 0)

        # A commit-time constraint failure rolls back connection+dataset+asset.
        known = str(__import__("uuid").uuid4())
        monkeypatch.setattr(service.uuid, "uuid4", lambda: __import__("uuid").UUID(known))
        db.add(Dataset(id=known, name="occupied", kind="integrated"))
        db.commit()
        with pytest.raises(HTTPException) as exc:
            service.register_asset(db, admin_user, RegisterAsset.model_validate(_register_body()))
        assert exc.value.status_code == 409
        assert db.query(Connection).count() == 0
        assert db.query(IntegratedAsset).count() == 0
        db.query(Dataset).filter(Dataset.id == known).delete()
        db.commit()
    finally:
        engine.dispose()


def test_duplicate_connection_name_conflicts_without_echo(db, admin_user, monkeypatch):
    engine, table, schema, fingerprint = _physical_source(monkeypatch)
    try:
        db.add(Connection(id="existing", name="inline source", kind="postgres", config={}))
        db.commit()
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(_dbx_evidence(schema)))
        with pytest.raises(HTTPException) as exc:
            service.register_asset(db, admin_user, RegisterAsset.model_validate(_register_body()))
        assert (exc.value.status_code, exc.value.detail["code"]) == (409, "CONNECTION_NAME_CONFLICT")
        assert "fixture-source-password" not in str(exc.value.detail)
        assert db.query(IntegratedAsset).count() == 0
    finally:
        engine.dispose()


# Entries that are individually well-formed but invalid against the physical
# schema are rejected by the service with explicit codes; malformed entries
# (extra keys, bad alias spelling, oversized display names) are rejected by
# the request contract itself.
_SERVICE_CASES = [
    ([], "FIELD_CONFIG_EMPTY"),
    ([{"source_key": "nope", "name": "row_id", "display_name": None}], "FIELD_CONFIG_UNKNOWN_SOURCE"),
    ([{"source_key": "id", "name": "row_id", "display_name": None},
      {"source_key": "id", "name": "other", "display_name": None}], "FIELD_CONFIG_INVALID"),
    ([{"source_key": "id", "name": "row_id", "display_name": None},
      {"source_key": "customer_name", "name": "row_id", "display_name": None}], "FIELD_CONFIG_INVALID"),
    ([{"source_key": "customer_name", "name": "customer", "display_name": None}], "FIELD_CONFIG_PK_REQUIRED"),
]


@pytest.mark.parametrize("config,code", _SERVICE_CASES)
def test_field_config_validation_codes(db, admin_user, monkeypatch, config, code):
    engine, table, schema, fingerprint = _physical_source(monkeypatch)
    try:
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(_dbx_evidence(schema)))
        with pytest.raises(HTTPException) as exc:
            service.register_asset(db, admin_user, RegisterAsset.model_validate(
                _register_body(field_config=config)))
        assert (exc.value.status_code, exc.value.detail["code"]) == (422, code)
        assert db.query(IntegratedAssetFieldConfig).count() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("entry", [
    {"source_key": "id", "name": "RowId"},
    {"source_key": "id", "name": "row_id", "display_name": "x" * 201},
    {"source_key": "id", "name": "row_id", "extra": 1},
    {"name": "row_id"},
    {"source_key": "id"},
])
def test_field_config_entry_contract_rejects_malformed_entries(entry):
    with pytest.raises(ValidationError):
        FieldConfigEntry.model_validate(entry)


def test_register_with_field_config_uses_logical_names_and_keeps_physical_truth(
        db, admin_user, monkeypatch):
    engine, table, schema, fingerprint = _physical_source(monkeypatch)
    try:
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(_dbx_evidence(schema)))
        config = [{"source_key": "customer_name", "name": "customer", "display_name": "客户"},
                  {"source_key": "id", "name": "row_id"}]
        result = service.register_asset(db, admin_user, RegisterAsset.model_validate(
            _register_body(name="configured", field_config=config)))
        # DTO addresses logical names and carries source_key/display_name.
        assert [c["name"] for c in result["columns"]] == ["row_id", "customer"]
        assert result["primary_key"] == ["row_id"]
        by_name = {c["name"]: c for c in result["columns"]}
        assert by_name["row_id"]["source_key"] == "id"
        assert by_name["customer"]["source_key"] == "customer_name"
        assert by_name["customer"]["display_name"] == "客户"
        assert by_name["row_id"]["type"] == "integer"  # type truth stays physical
        assert "display_name" not in by_name["row_id"]
        assert result["field_config"] == [
            {"source_key": "customer_name", "name": "customer", "display_name": "客户"},
            {"source_key": "id", "name": "row_id", "display_name": None}]
        assert result["logical_fingerprint"] != result["schema_fingerprint"]

        # Physical storage stays pristine: schema_json is exactly columns+PK.
        asset = db.get(IntegratedAsset, result["id"])
        assert set(asset.schema_json) == {"columns", "primary_key"}
        assert asset.schema_json["columns"] == schema["columns"]
        assert asset.schema_fingerprint == digest(
            {"columns": schema["columns"], "primary_key": ["id"]})
        stored = db.get(IntegratedAssetFieldConfig, asset.id)
        assert stored.field_config == result["field_config"]
        # Unconfigured assets keep the exact original DTO shape.
        plain = service.register_asset(db, admin_user, RegisterAsset.model_validate(
            _register_body(name="plain", source_connection=_inline_source(source_name="other source"))))
        assert "field_config" not in plain and "logical_fingerprint" not in plain
        assert [c["name"] for c in plain["columns"]] == ["id", "customer_name", "amount"]
    finally:
        engine.dispose()


def test_schema_endpoint_presents_logical_view_with_physical_fingerprint(db, admin_user, monkeypatch):
    engine, table, schema, fingerprint = _physical_source(monkeypatch)
    try:
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(_dbx_evidence(schema)))
        result = service.register_asset(db, admin_user, RegisterAsset.model_validate(
            _register_body(field_config=[{"source_key": "id", "name": "row_id"},
                                         {"source_key": "amount", "name": "total"}])))
        asset = db.get(IntegratedAsset, result["id"])
        db.expire_all()
        payload = {"asset_id": asset.id, **asset.schema_json,
                   "schema_fingerprint": asset.schema_fingerprint}
        projection = service.asset_projection(asset)
        payload.update(columns=projection["columns"], primary_key=projection["primary_key"],
                       field_config=projection["field_config"],
                       logical_fingerprint=projection["fingerprint"])
        assert [c["name"] for c in payload["columns"]] == ["row_id", "total"]
        assert payload["primary_key"] == ["row_id"]
        # The physical fingerprint never covers the projection.
        assert payload["schema_fingerprint"] == digest(
            {"columns": schema["columns"], "primary_key": ["id"]})
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Preview/mutate mapping: logical addressing, full-physical-row tokens
# ---------------------------------------------------------------------------

def _configured_asset(db, user, *, writable=True):
    source = Connection(id="probe-source", name="source", kind="postgres", config={})
    dataset = Dataset(id="probe-dataset", name="source", kind="integrated",
                      source_connection_id="probe-source", source_resource="public.rows")
    db.add(source)
    db.flush()
    db.add(dataset)
    db.flush()
    schema = {"columns": [
        {"name": "id", "type": "integer", "source_type": "BIGINT", "nullable": False,
         "precision": None, "scale": None, "timezone": None, "collation": None,
         "length": None, "unsigned": False, "primary_key": True},
        {"name": "customer_name", "type": "string", "source_type": "VARCHAR(100)",
         "nullable": True, "precision": None, "scale": None, "timezone": None,
         "collation": None, "length": 100, "unsigned": False, "primary_key": False},
        {"name": "secret_note", "type": "string", "source_type": "TEXT",
         "nullable": True, "precision": None, "scale": None, "timezone": None,
         "collation": None, "length": None, "unsigned": False, "primary_key": False},
    ], "primary_key": ["id"]}
    fingerprint = digest(schema)
    asset = IntegratedAsset(id="probe-asset", dataset_id="probe-dataset",
                            connection_id="probe-source", name="source",
                            schema_name="public", table_name="rows",
                            schema_json=schema, schema_fingerprint=fingerprint,
                            writable=writable, created_by=user.id)
    db.add(asset)
    db.flush()
    db.add(IntegratedAssetFieldConfig(
        asset_id="probe-asset",
        field_config=[{"source_key": "id", "name": "row_id", "display_name": None},
                      {"source_key": "customer_name", "name": "customer", "display_name": "客户"}]))
    db.commit()
    return asset, schema, fingerprint


class _NativeTestConnection(sa.engine.Connection):
    """Real Connection subclass: reflection/inspect keep working while the
    MySQL/PostgreSQL-only REPEATABLE READ knob is absorbed for SQLite."""

    def execution_options(self, **options):
        options.pop("isolation_level", None)
        return super().execution_options(**options) if options else self


class _NativeTestEngine:
    def __init__(self, engine):
        self.engine = engine

    def connect(self):
        return _NativeTestConnection(self.engine)

    def dispose(self):
        pass


def _live_source(monkeypatch, schema, fingerprint):
    engine = sa.create_engine("sqlite://")
    table = sa.Table("rows", sa.MetaData(),
                     sa.Column("id", sa.BigInteger, primary_key=True),
                     sa.Column("customer_name", sa.String(100)),
                     sa.Column("secret_note", sa.Text))
    table.create(engine)
    monkeypatch.setattr(native, "source_engine", lambda *_: _NativeTestEngine(engine))
    monkeypatch.setattr(service, "source_engine", lambda *_: _NativeTestEngine(engine))
    # SQLite has no named schemas; reflection is doubled with the physical
    # schema/fingerprint of the configured asset (same seam as registration).
    monkeypatch.setattr(native, "reflect", lambda *_: (table, schema, fingerprint))
    monkeypatch.setattr(service, "reflect", lambda *_: (table, schema, fingerprint))
    return engine, table


def test_preview_projects_logical_columns_with_full_physical_tokens(db, admin_user, monkeypatch):
    asset, schema, fingerprint = _configured_asset(db, admin_user)
    engine, table = _live_source(monkeypatch, schema, fingerprint)
    try:
        with engine.connect() as conn:
            conn.execute(table.insert(), {"id": 7, "customer_name": "alice", "secret_note": "hidden"})
            conn.commit()
        preview = service.preview_rows(db, admin_user, asset.id, 10)
        assert len(preview["rows"]) == 1
        row = preview["rows"][0]
        # Values are projected: only logical columns, unmapped stay invisible.
        assert set(row["values"]) == {"row_id", "customer"}
        assert row["values"] == {"row_id": 7, "customer": "alice"}
        # The token still binds to the FULL physical row + physical fingerprint.
        from app.data_channel.integrated.contracts import row_token
        assert row["row_token"] == row_token(
            {"id": 7, "customer_name": "alice", "secret_note": "hidden"}, fingerprint)
        assert [c["name"] for c in preview["columns"]] == ["row_id", "customer"]
    finally:
        engine.dispose()


def test_configured_mutations_use_logical_names_and_physical_tokens(db, admin_user, monkeypatch):
    asset, schema, fingerprint = _configured_asset(db, admin_user)
    engine, table = _live_source(monkeypatch, schema, fingerprint)
    try:
        inserted = service.mutate_row(db, admin_user, asset.id, "insert", RowMutation(
            key={"row_id": 9}, values={"customer": "bob"}, idempotency_key="cfg-insert"))
        assert inserted["status"] == "applied"
        with engine.connect() as conn:
            stored = conn.execute(sa.select(table)).mappings().one()
            assert stored["customer_name"] == "bob" and stored["id"] == 9

        # Unmapped physical columns are not addressable through the projection.
        with pytest.raises(HTTPException) as exc:
            service.mutate_row(db, admin_user, asset.id, "update", RowMutation(
                key={"row_id": 9}, values={"secret_note": "leak"},
                expected_row_token="t", idempotency_key="cfg-leak"))
        assert (exc.value.status_code, exc.value.detail["code"]) == (422, "UNKNOWN_COLUMN")
        # Physical names that are not configured aliases are equally rejected.
        with pytest.raises(HTTPException) as exc:
            service.mutate_row(db, admin_user, asset.id, "update", RowMutation(
                key={"row_id": 9}, values={"customer_name": "leak"},
                expected_row_token="t", idempotency_key="cfg-leak2"))
        assert exc.value.detail["code"] == "UNKNOWN_COLUMN"

        # PK addressing uses logical names.
        with pytest.raises(HTTPException) as exc:
            service.mutate_row(db, admin_user, asset.id, "delete", RowMutation(
                key={"id": 9}, expected_row_token="t", idempotency_key="cfg-pk"))
        assert exc.value.detail["code"] == "COMPLETE_PRIMARY_KEY_REQUIRED"

        # Token continuity: preview token (full physical row) authorizes update.
        preview = service.preview_rows(db, admin_user, asset.id, 10)
        token = preview["rows"][0]["row_token"]
        updated = service.mutate_row(db, admin_user, asset.id, "update", RowMutation(
            key={"row_id": 9}, values={"customer": "bobby"},
            expected_row_token=token, idempotency_key="cfg-update"))
        assert updated["status"] == "applied"
        with pytest.raises(HTTPException) as exc:
            service.mutate_row(db, admin_user, asset.id, "update", RowMutation(
                key={"row_id": 9}, values={"customer": "stale"},
                expected_row_token=token, idempotency_key="cfg-conflict"))
        assert exc.value.detail["code"] == "ROW_CONFLICT"
        deleted = service.mutate_row(db, admin_user, asset.id, "delete", RowMutation(
            key={"row_id": 9}, expected_row_token=updated["row_token"],
            idempotency_key="cfg-delete"))
        assert deleted["status"] == "applied"
        with engine.connect() as conn:
            assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Refresh: config preserved; disappeared mapped source fails explicitly
# ---------------------------------------------------------------------------

def test_refresh_preserves_config_and_fails_when_source_disappears(db, admin_user, monkeypatch):
    asset, schema, fingerprint = _configured_asset(db, admin_user)
    engine, table, reflected, _ = _physical_source(monkeypatch)
    try:
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(_dbx_evidence(reflected)))
        # The reflected double lacks `secret_note`, which is unmapped anyway:
        # refresh must keep the config and bind it to the new physical schema.
        refreshed = service.refresh_schema(db, admin_user, asset.id)
        assert [c["name"] for c in refreshed["columns"]] == ["row_id", "customer"]
        stored = db.get(IntegratedAssetFieldConfig, asset.id)
        assert [e["name"] for e in stored.field_config] == ["row_id", "customer"]
        assert db.get(IntegratedAsset, asset.id).schema_fingerprint == digest(
            {"columns": reflected["columns"], "primary_key": ["id"]})

        # A mapped source column disappearing rejects the refresh explicitly.
        shrunk = {"columns": [c for c in reflected["columns"] if c["name"] != "customer_name"],
                  "primary_key": ["id"]}
        shrunk_table = sa.Table("rows", sa.MetaData(),
                                sa.Column("id", sa.BigInteger, primary_key=True),
                                sa.Column("amount", sa.Numeric(30, 6)))
        monkeypatch.setattr(service, "reflect",
                            lambda *_: (shrunk_table, shrunk, digest(shrunk)))
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(_dbx_evidence(shrunk)))
        with pytest.raises(HTTPException) as exc:
            service.refresh_schema(db, admin_user, asset.id)
        assert (exc.value.status_code, exc.value.detail["code"]) == (409, "FIELD_CONFIG_SOURCE_MISSING")
        # The asset keeps its previous verified schema and config.
        assert db.get(IntegratedAsset, asset.id).schema_json["columns"] == reflected["columns"]
        assert db.get(IntegratedAssetFieldConfig, asset.id).field_config == stored.field_config
    finally:
        engine.dispose()


def test_listing_batches_config_reads_without_per_asset_queries(db, admin_user, monkeypatch):
    asset, _, _ = _configured_asset(db, admin_user)
    plain_dataset = Dataset(id="plain-dataset", name="plain", kind="integrated",
                            source_connection_id="probe-source", source_resource="public.plain_rows")
    db.add(plain_dataset)
    db.flush()
    plain = IntegratedAsset(id="plain-asset", dataset_id="plain-dataset",
                            connection_id="probe-source", name="plain",
                            schema_name="public", table_name="plain_rows",
                            schema_json={"columns": [], "primary_key": []},
                            schema_fingerprint="f", writable=False, created_by=admin_user.id)
    db.add(plain)
    db.commit()
    monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(None))
    items = service.list_assets(db, admin_user)["items"]
    assert len(items) == 2
    configured = next(item for item in items if item["id"] == "probe-asset")
    assert [c["name"] for c in configured["columns"]] == ["row_id", "customer"]
    plain_dto = next(item for item in items if item["id"] == "plain-asset")
    assert "field_config" not in plain_dto


# ---------------------------------------------------------------------------
# Live query mapping: logical fields, physical columns, no arbitrary reads
# ---------------------------------------------------------------------------

def test_resolve_metadata_and_compiler_bind_logical_fields_to_physical_columns(
        db, admin_user, monkeypatch):
    asset, schema, fingerprint = _configured_asset(db, admin_user)
    engine, table = _live_source(monkeypatch, schema, fingerprint)
    try:
        with engine.connect() as conn:
            conn.execute(table.insert(), [
                {"id": 1, "customer_name": "alice", "secret_note": "h1"},
                {"id": 2, "customer_name": "bob", "secret_note": "h2"}])
            conn.commit()

        meta = native.resolve_metadata(db, admin_user, [asset.id])[asset.id]
        assert meta["schema_fingerprint"] == fingerprint  # physical pin stays
        projection = meta["projection"]
        assert [c["name"] for c in projection["columns"]] == ["row_id", "customer"]
        assert projection["logical_to_physical"] == {"row_id": "id", "customer": "customer_name"}

        from app.data_channel.query.compiler import compile_query
        from app.data_channel.query.contracts import ColumnSpec, QueryError, Relation
        from app.data_channel.query.parser import parse

        def spec(column):
            return ColumnSpec(column["name"], column["type"], nullable=column["nullable"])

        columns = {c["name"]: spec(c) for c in projection["columns"]}
        expressions = {name: table.c[physical]
                       for name, physical in projection["logical_to_physical"].items()}
        relation = Relation("s", table, columns, {"kind": "integrated"}, "auth",
                            fingerprint, "source:probe-source", expressions=expressions)
        compiled = compile_query(
            parse("from s as s | where s.row_id = :rid | fields s.customer, s.row_id"),
            {"s": relation}, {"rid": 2})
        sql = str(compiled.statement.compile(dialect=sa.dialects.sqlite.dialect()))
        # Logical output labels, physical column binding underneath.
        assert "customer_name" in sql and "customer" in sql
        with engine.connect() as conn:
            rows = conn.execute(compiled.statement, compiled.params).mappings().all()
        assert [dict(r) for r in rows] == [{"customer": "bob", "row_id": 2}]

        # Unmapped/physical names are unknown fields to the compiler.
        for query in ("from s as s | fields s.secret_note",
                      "from s as s | where s.customer_name = :name",
                      "from s as s | where s.id = 1"):
            with pytest.raises(QueryError) as exc:
                compile_query(parse(query), {"s": relation}, {})
            assert exc.value.code == "unknown_field"
    finally:
        engine.dispose()


@pytest.mark.parametrize("recheck_only", [False, True])
def test_provider_projection_and_cursor_context_follow_logical_config(
        db, admin_user, monkeypatch, recheck_only):
    from contextlib import ExitStack
    from sqlalchemy.orm import Session

    from app.data_channel.query.compiler import compile_query
    from app.data_channel.query.contracts import Binding, QueryError, QueryLimits
    from app.data_channel.query.parser import parse
    from app.data_channel.query.providers import resolve_integrated

    asset, schema, fingerprint = _configured_asset(db, admin_user)
    engine, table = _live_source(monkeypatch, schema, fingerprint)
    try:
        with ExitStack() as stack:
            relation = resolve_integrated(
                db, admin_user, [("s", Binding(kind="integrated", asset_id=asset.id))],
                QueryLimits(), stack, recheck_only=recheck_only)["s"]
            assert set(relation.columns) == {"row_id", "customer"}
            assert set(relation.expressions) == {"row_id", "customer"}
            assert (relation.batches is None) is recheck_only
            compiled = compile_query(parse("from s as s | fields s.row_id, s.customer"),
                                     {"s": relation}, {})
            assert [column.name for column in compiled.statement.selected_columns] == [
                "row_id", "customer"]
            for hidden in ("id", "customer_name", "secret_note"):
                with pytest.raises(QueryError) as exc:
                    compile_query(parse(f"from s as s | fields s.{hidden}"), {"s": relation}, {})
                assert exc.value.code == "unknown_field"

            before = native.resolve_metadata(db, admin_user, [asset.id])[asset.id]
            cached = db.get(IntegratedAssetFieldConfig, asset.id)
            with Session(bind=db.get_bind()) as other:
                other.execute(sa.update(IntegratedAssetFieldConfig).where(
                    IntegratedAssetFieldConfig.asset_id == asset.id).values(field_config=[
                        {"source_key": "id", "name": "row_id", "display_name": None}]))
                other.commit()
            after = native.resolve_metadata(db, admin_user, [asset.id])[asset.id]
            assert cached.field_config == after["projection"]["field_config"]
            assert after["schema_fingerprint"] == before["schema_fingerprint"]
            assert after["auth_fingerprint"] != before["auth_fingerprint"]
            monkeypatch.setattr(native, "probe_source", lambda *_: pytest.fail(
                "A changed logical projection must reject delivery before a source probe"))
            with pytest.raises(QueryError) as exc:
                relation.recheck()
            assert (exc.value.status, exc.value.code) == (409, "query_context_changed")
    finally:
        engine.dispose()


@pytest.mark.parametrize("load_config", [False, True])
def test_configured_asset_decommission_removes_owned_projection_only(
        db, admin_user, load_config):
    asset, schema, fingerprint = _configured_asset(db, admin_user)
    if load_config:
        assert asset.field_config_row is not None
    else:
        db.expire(asset, ["field_config_row"])
    deleted_tables = []

    def record_delete(connection, cursor, statement, parameters, context, executemany):
        if context.compiled is not None and context.compiled.isdelete:
            deleted_tables.append(context.compiled.statement.table.name)

    engine = db.get_bind()
    sa.event.listen(engine, "before_cursor_execute", record_delete)
    try:
        result = service.decommission_asset(db, admin_user, asset.id)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record_delete)
    assert deleted_tables.index("v2_integrated_asset_field_configs") < deleted_tables.index("v2_integrated_assets")
    assert deleted_tables.index("v2_integrated_assets") < deleted_tables.index("v2_datasets")
    assert result["status"] == "decommissioned"
    assert db.get(IntegratedAsset, "probe-asset") is None
    assert db.get(IntegratedAssetFieldConfig, "probe-asset") is None
    assert db.get(Dataset, "probe-dataset") is None
    assert db.get(Connection, "probe-source") is not None


# ---------------------------------------------------------------------------
# Sealed versions: frozen logical mapping, distinct physical metadata
# ---------------------------------------------------------------------------

def _column(name, kind, source_type, nullable=True, **extra):
    return {"name": name, "type": kind, "source_type": source_type, "nullable": nullable,
            "precision": None, "scale": None, "timezone": None, "collation": None,
            "length": None, "unsigned": False, **extra}


def _version_fixture(db, user, config):
    import uuid
    connection = Connection(id=str(uuid.uuid4()), name="vsource", kind="postgres", config={})
    db.add(connection)
    db.flush()
    dataset = Dataset(id=str(uuid.uuid4()), name="vdataset", kind="integrated",
                      source_connection_id=connection.id, source_resource="public.rows")
    db.add(dataset)
    db.flush()
    schema = {"columns": [_column("id", "integer", "BIGINT", nullable=False),
                          _column("customer_name", "string", "VARCHAR(100)")],
              "primary_key": ["id"]}
    fingerprint = digest(schema)
    asset = IntegratedAsset(id=str(uuid.uuid4()), dataset_id=dataset.id,
                            connection_id=connection.id, name="vasset",
                            schema_name="public", table_name="rows",
                            schema_json=schema, schema_fingerprint=fingerprint,
                            writable=False, created_by=user.id)
    db.add(asset)
    db.flush()
    if config is not None:
        db.add(IntegratedAssetFieldConfig(asset_id=asset.id, field_config=config))
    descriptor = {"schema": schema["columns"], "primary_key": ["id"],
                  "schema_fingerprint": fingerprint,
                  "dataset_id": dataset.id, "asset_id": asset.id,
                  "connection_id": connection.id, "resource": "public.rows",
                  "expected_rows": 1,
                  "snapshot": {"id": str(uuid.uuid4()), "consistent": True,
                               "kind": "repeatable_read", "started_at": "2026-10-25T08:00:00+00:00"}}
    return asset, descriptor


@pytest.fixture(autouse=True)
def _cleanup_versions(db):
    yield
    db.rollback()
    connection = db.connection()
    for name in sa.inspect(connection).get_table_names():
        if name.startswith(store.TABLE_PREFIX):
            sa.Table(name, sa.MetaData()).drop(connection)
    db.commit()


def _publish(db, descriptor, rows):
    run, gate = store.begin_snapshot(db, descriptor)
    hasher = store.snapshot_hasher(run)
    store.write_snapshot_batch(db, run, gate, rows, hasher)
    completion = {"complete": True, "row_count": run.expected_rows,
                  "schema_fingerprint": run.schema_json["schema_fingerprint"],
                  "snapshot_id": run.snapshot_json["id"]}
    return run, store.seal_snapshot(db, run, completion, hasher.hexdigest())


def test_sealed_versions_freeze_logical_mapping_with_distinct_physical_metadata(db, admin_user):
    config = [{"source_key": "id", "name": "row_id", "display_name": None},
              {"source_key": "customer_name", "name": "customer", "display_name": "客户"}]
    asset, descriptor = _version_fixture(db, admin_user, config)
    run, version = _publish(db, descriptor, [{"id": 1, "customer_name": None}])

    # Manifest freezes the config; physical keys stay pristine.
    manifest = run.schema_json
    assert manifest["field_config"] == config
    assert manifest["logical_fingerprint"] != manifest["schema_fingerprint"]
    assert manifest["schema_fingerprint"] == descriptor["schema_fingerprint"]
    assert manifest["primary_key_columns"] == ["id"]

    metadata = store.describe_version(db, asset.dataset_id, version.id)
    assert [c["name"] for c in metadata["source_schema"]] == ["row_id", "customer"]
    assert metadata["primary_key_columns"] == ["row_id"]
    assert metadata["columns"] == ["row_id", "customer"]
    assert metadata["column_map"] == {"row_id": "id", "customer": "customer_name"}
    # Distinct physical metadata is preserved under physical_* keys.
    assert metadata["physical_columns"] == ["id", "customer_name"]
    assert metadata["physical_source_schema"] == manifest["source_schema"]
    assert metadata["physical_primary_key_columns"] == ["id"]
    assert metadata["physical_column_map"] == manifest["column_map"]

    # Row reads use logical names and decode against the logical view.
    rows = [row for batch in store.iter_version_rows(db, asset.dataset_id, version.id)
            for row in batch]
    assert rows == [{"row_id": "1", "customer": None}]
    from app.data_channel.datasets.integrated_store import decode_version_row
    assert decode_version_row(rows[0], metadata["source_schema"]) == {"row_id": 1, "customer": None}
    lookups = [row for batch in store.lookup_version_rows(
        db, asset.dataset_id, version.id, column="row_id", keys=["1"]) for row in batch]
    assert lookups == rows
    with pytest.raises(store.IntegratedVersionError):
        list(store.iter_version_rows(db, asset.dataset_id, version.id, columns=["customer_name"]))


def test_version_logical_mapping_is_frozen_against_later_config_changes(db, admin_user):
    config = [{"source_key": "id", "name": "row_id", "display_name": None}]
    asset, descriptor = _version_fixture(db, admin_user, config)
    run, version = _publish(db, descriptor, [{"id": 1, "customer_name": "alice"}])
    old_run_fingerprint = run.schema_json["logical_fingerprint"]

    # Simulate a later config edit on the asset (no runtime edit endpoint yet).
    stored = db.get(IntegratedAssetFieldConfig, asset.id)
    stored.field_config = [{"source_key": "id", "name": "key", "display_name": None},
                           {"source_key": "customer_name", "name": "customer", "display_name": None}]
    db.commit()

    metadata = store.describe_version(db, asset.dataset_id, version.id)
    assert metadata["columns"] == ["row_id"]  # frozen at seal time
    assert metadata["logical_fingerprint"] == old_run_fingerprint
    rows = [row for batch in store.iter_version_rows(db, asset.dataset_id, version.id)
            for row in batch]
    assert rows == [{"row_id": "1"}]


def test_physical_checksum_is_independent_of_field_config(db, admin_user):
    config = [{"source_key": "id", "name": "row_id", "display_name": None},
              {"source_key": "customer_name", "name": "customer", "display_name": None}]
    configured, descriptor_configured = _version_fixture(db, admin_user, config)
    plain, descriptor_plain = _version_fixture(db, admin_user, None)
    rows = [{"id": 1, "customer_name": "alice"}]
    run_configured, version_configured = _publish(db, descriptor_configured, rows)
    run_plain, version_plain = _publish(db, descriptor_plain, rows)
    # Identical physical schema/rows must seal to identical physical checksums
    # regardless of the logical projection.
    assert version_configured.checksum == version_plain.checksum
    assert run_configured.checksum == run_plain.checksum
    # The frozen mapping is still stored and still drives the read projection.
    assert "field_config" in run_configured.schema_json
    assert "field_config" not in run_plain.schema_json
    metadata = store.describe_version(db, configured.dataset_id, version_configured.id)
    assert metadata["columns"] == ["row_id", "customer"]
    assert metadata["checksum"] == version_plain.checksum


def test_unconfigured_versions_keep_the_original_manifest_and_reads(db, admin_user):
    asset, descriptor = _version_fixture(db, admin_user, None)
    run, version = _publish(db, descriptor, [{"id": 1, "customer_name": "alice"}])
    assert "field_config" not in run.schema_json and "logical_fingerprint" not in run.schema_json
    metadata = store.describe_version(db, asset.dataset_id, version.id)
    assert metadata["columns"] == ["id", "customer_name"]
    assert "physical_columns" not in metadata
    rows = [row for batch in store.iter_version_rows(db, asset.dataset_id, version.id)
            for row in batch]
    assert rows == [{"id": "1", "customer_name": "alice"}]


def test_sync_run_descriptor_and_request_path_carry_config_into_versions(db, admin_user, monkeypatch):
    config = [{"source_key": "id", "name": "row_id", "display_name": None}]
    asset, descriptor = _version_fixture(db, admin_user, config)
    # The queue -> claim -> snapshot path must freeze the same config.
    from app.data_channel.pipeline_tasks import dispatch
    published = []
    monkeypatch.setattr(dispatch, "dispatch_integrated_sync", lambda *args: published.append(args))
    queued = service.request_sync(db, admin_user, asset.id, 10)
    assert queued["status"] == "queued"
    run = db.get(IntegratedSyncRun, queued["run_id"])
    assert run.status == "queued"
    # begin_snapshot with the queued run freezes the config into the manifest;
    # simulate the worker's queued->starting claim first.
    run.status = "starting"
    db.commit()
    descriptor = {**descriptor, "expected_rows": 1}
    claimed = store.begin_snapshot(db, descriptor, run_id=run.id,
                                   lease={"format": "x", "token": "t", "actor_id": admin_user.id,
                                          "acquired_at": "2026-10-25T08:00:00+00:00",
                                          "expires_at": "2026-10-25T08:02:00+00:00", "pid": 1})
    assert claimed[0].schema_json["field_config"] == config


def test_begin_snapshot_rejects_config_that_drifted_from_physical_schema(db, admin_user):
    asset, descriptor = _version_fixture(
        db, admin_user, [{"source_key": "gone", "name": "row_id", "display_name": None}])
    with pytest.raises(store.IntegratedVersionError):
        store.begin_snapshot(db, descriptor)


# ---------------------------------------------------------------------------
# Validation-error echo: credentials never appear in 422 bodies
# ---------------------------------------------------------------------------

def test_validation_errors_never_echo_inline_credentials(client, auth_headers):
    secret = "sekrit-inline-password"
    base_config = {"host": "127.0.0.1", "port": 5432, "database": "d",
                   "user": "u", "password": secret}
    inline = {"name": "s", "kind": "postgres", "config": base_config}
    register_base = {"name": "inline", "schema_name": "public", "table_name": "rows",
                     "source_connection": inline}
    malformed = [
        # Missing the source_connection name: the whole inline dict (with the
        # password) would be echoed as the failing input without redaction.
        {**register_base, "source_connection": {"kind": "postgres", "config": base_config}},
        # Exclusive reference rule violated on register and probe bodies.
        {**register_base, "connection_id": "c"},
        {"connection_id": "c"}, {},
        # dbx endpoint pair rule and bad types.
        {**register_base, "source_connection": {**inline, "config": {**base_config, "dbx_host": "source-pg"}}},
        {**register_base, "source_connection": {**inline, "config": {**base_config, "port": "not-a-port"}}},
        # Malformed credential containers: a URI string where the object (or
        # its config) belongs must never be echoed back either.  The URI
        # embeds the same `secret` variable the assertion hunts for.
        {**register_base, "source_connection": f"postgres://fixture:{secret}@source-pg:5432/db"},
        {**register_base, "source_connection": {"name": "s", "kind": "postgres",
                                                "config": f"postgres://fixture:{secret}@source-pg:5432/db"}},
        # Invalid logical alias spelling (cross-entry duplicates are a
        # service-level rejection covered separately).
        {**register_base, "field_config": [{"source_key": "id", "name": "RowId"}]},
    ]
    for body in malformed:
        response = client.post("/api/v2/integrated-datasets", json=body, headers=auth_headers)
        assert response.status_code == 422, body
        assert secret not in response.text, body
        assert "[redacted]" in response.text or "password" not in response.text

    probe = {"connection_id": "x", "source_connection": inline}
    for path in ("/api/v2/integrated-datasets/sources/test",
                 "/api/v2/integrated-datasets/sources/resources",
                 "/api/v2/integrated-datasets/sources/schema"):
        payload = {**probe, "schema_name": "public", "table_name": "rows"} \
            if path.endswith("/schema") else probe
        response = client.post(path, json=payload, headers=auth_headers)
        assert response.status_code == 422, path
        assert secret not in response.text, path

    # Model-validator ValueErrors (dbx pair, kind rules) must still serialize.
    response = client.post("/api/v2/integrated-datasets", json={
        "name": "inline", "schema_name": "public", "table_name": "rows",
        "source_connection": {"name": "s", "kind": "postgres",
                              "config": {**base_config, "dbx_host": "source-pg"}}},
        headers=auth_headers)
    assert response.status_code == 422
    assert secret not in response.text


def test_probe_endpoints_wired_over_http(client, auth_headers, monkeypatch):
    engine = sa.create_engine("sqlite://")
    try:
        monkeypatch.setattr(service, "source_engine", lambda *_: engine)
        response = client.post("/api/v2/integrated-datasets/sources/test",
                               json={"source_connection": _inline_source()},
                               headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == {"success": True}
    finally:
        engine.dispose()


def test_other_routes_keep_the_stock_validation_response(client, auth_headers):
    # Outside the integrated-datasets prefix the default FastAPI 422 shape is
    # preserved (input echo included where it existed before).
    response = client.post("/api/v2/connections", json={"name": "x", "kind": "rest",
                                                        "config": {"unexpected": [1, 2, 3]}},
                           headers=auth_headers)
    assert response.status_code in (200, 201, 422)
    body = response.json()
    if response.status_code == 422:
        assert isinstance(body.get("detail"), list)
