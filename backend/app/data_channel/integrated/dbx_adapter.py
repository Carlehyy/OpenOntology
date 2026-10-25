"""Pinned dbx metadata capability lane (never substituted by native SQL).

The pinned upstream is a Rust application (`dbx-web`) shipped as an official
container image.  This adapter speaks only the pinned HTTP surface needed for
authoritative discovery and never accepts SQL from callers:

* ``POST /api/auth/login``            (DBX_PASSWORD -> session cookie)
* ``GET  /api/version``               (release pin handshake)
* ``POST /api/connection/connect``    (transient, per-operation source config)
* ``POST /api/connection/disconnect``
* ``GET  /api/schema/databases``
* ``GET  /api/schema/schemas``
* ``GET  /api/schema/tables``
* ``GET  /api/schema/columns``

Connection configuration is decrypted from the platform's own ``Connection``
row for the duration of one operation and is never persisted in dbx storage.
An optional explicit pair ``dbx_host``/``dbx_port`` in that configuration lets
the platform reach the same physical source from inside the dbx network
(for example a compose service name); the native lane never reads those fields
and there is no implicit host rewrite or fallback.
Snapshot streaming and bound DML remain explicit native capabilities.

The Web API is documented upstream as internal (not a public integration
contract).  That boundary is contained by pinning the official release image
digest, requiring ``GET /api/version`` to equal :data:`DBX_VERSION`, and
validating every response shape before it is trusted.
"""
from __future__ import annotations

from contextlib import contextmanager
import ipaddress
import re
import uuid

import httpx
from sqlalchemy.engine import make_url

from app.config import settings
from app.data_channel.integrated.contracts import DBX_COMMIT, DBX_VERSION, fail


# Pinned first-party surface of the release image.  Nothing else is reachable
# through this adapter, so a caller can never turn dbx into an arbitrary SQL
# or arbitrary-REST proxy.
_ALLOWED_PATHS = frozenset({
    "/api/auth/check",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/version",
    "/api/connection/connect",
    "/api/connection/disconnect",
    "/api/schema/databases",
    "/api/schema/schemas",
    "/api/schema/tables",
    "/api/schema/columns",
})

_PROBE_TIMEOUT = httpx.Timeout(3.0, connect=1.5)
_OPERATION_TIMEOUT = httpx.Timeout(30.0, connect=5.0, write=10.0, pool=5.0)

_MAX_DATABASES = 64
_MAX_SCHEMAS = 128
_MAX_TABLES = 5000
_MAX_COLUMNS = 2000
_PAGE = 500
_TLS_KEYS = ("ssl", "sslmode", "ssl_mode", "ca_cert_path", "client_cert_path", "client_key_path")
_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?"
_HOST_PATTERN = re.compile(rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*\.?")


def _http_client(base_url: str, timeout: httpx.Timeout) -> httpx.Client:
    """Factory kept as a module function so tests can bind a transport double."""
    return httpx.Client(
        base_url=base_url,
        timeout=timeout,
        follow_redirects=False,
        # Only the operator-provided URL may be contacted; inherited proxy
        # environment variables must not silently reroute source metadata.
        trust_env=False,
    )


def _status_reason(exc: BaseException) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        return detail["code"]
    return "DBX_UNAVAILABLE"


class DbxMetadataAdapter:
    @property
    def configured(self) -> bool:
        try:
            self._config()
        except Exception:
            return False
        return True

    def status(self) -> dict:
        result = {"configured": False, "available": False,
                  "commit": DBX_COMMIT, "version": DBX_VERSION, "reason": "DBX_NOT_CONFIGURED"}
        try:
            self._config()
            result["configured"] = True
            result["reason"] = None
        except Exception as exc:
            result["reason"] = _status_reason(exc)
            return result
        client = None
        try:
            client = self._open_client(probe=True)
            result["available"] = True
        except Exception as exc:  # status must never break asset listing
            result["reason"] = _status_reason(exc)
        finally:
            if client is not None:
                self._logout(client)
                try:
                    client.close()
                except Exception:
                    pass
        return result

    # ------------------------------------------------------------------ HTTP
    def _config(self):
        url = str(settings.integrated_dbx_url or "").strip()
        password = str(settings.integrated_dbx_password or "")
        if (not url or not password
                or settings.integrated_dbx_commit != DBX_COMMIT):
            fail(503, "DBX_NOT_CONFIGURED",
                 "Pinned dbx metadata transport requires an explicit URL, password and matching commit pin")
        return self._base_url(url), password

    @staticmethod
    def _base_url(url: str) -> str:
        if len(url) > 2048:
            fail(503, "DBX_CONFIG_INVALID", "dbx URL is invalid")
        try:
            parsed = httpx.URL(url)
        except Exception:
            fail(503, "DBX_CONFIG_INVALID", "dbx URL is invalid")
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            fail(503, "DBX_CONFIG_INVALID", "dbx URL must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            fail(503, "DBX_CONFIG_INVALID", "dbx URL must not embed credentials, a query or a fragment")
        if parsed.path not in {"", "/"}:
            fail(503, "DBX_CONFIG_INVALID",
                 "dbx URL must target the server root; reverse-proxy base paths are unsupported")
        return str(parsed.copy_with(path="/")).rstrip("/")

    @staticmethod
    def _upstream_failure(status: int) -> None:
        if status == 401:
            fail(503, "DBX_AUTH_FAILED", "dbx session authentication failed")
        if status == 403:
            fail(503, "DBX_FORBIDDEN", "dbx rejected the request or has no password configured")
        if status == 404:
            fail(503, "DBX_ENDPOINT_UNAVAILABLE",
                 "Pinned dbx endpoint is unavailable; the running image does not match the pinned release")
        if status == 429:
            fail(503, "DBX_RATE_LIMITED", "dbx rate-limited the metadata request")
        if status >= 500:
            fail(503, "DBX_UPSTREAM_ERROR", "dbx metadata service failed")
        fail(503, "DBX_REQUEST_REJECTED", f"dbx rejected the metadata request ({status})")

    def _json(self, client: httpx.Client, method: str, path: str, *,
              params: dict | None = None, body: dict | None = None):
        if path not in _ALLOWED_PATHS:
            fail(503, "DBX_CONFIG_INVALID", "dbx adapter path is outside the pinned metadata surface")
        try:
            response = client.request(method, path, params=params, json=body)
        except httpx.TimeoutException:
            fail(503, "DBX_TIMEOUT", "dbx metadata service timed out")
        except httpx.HTTPError:
            fail(503, "DBX_UNREACHABLE", "dbx metadata service is unreachable")
        if response.status_code not in {200, 201}:
            self._upstream_failure(response.status_code)
        try:
            return response.json()
        except ValueError:
            fail(503, "DBX_RESPONSE_INVALID", "dbx returned a non-JSON metadata response")

    def _authenticate(self, client: httpx.Client, password: str) -> None:
        check = self._json(client, "GET", "/api/auth/check")
        if not isinstance(check, dict):
            fail(503, "DBX_RESPONSE_INVALID", "dbx auth state is invalid")
        if check.get("setup_required") is True:
            fail(503, "DBX_AUTH_SETUP_REQUIRED", "dbx requires first-run password setup")
        if check.get("required") is False:
            return
        if check.get("required") is not True:
            fail(503, "DBX_RESPONSE_INVALID", "dbx auth state is invalid")
        try:
            response = client.post("/api/auth/login", json={"password": password})
        except httpx.TimeoutException:
            fail(503, "DBX_TIMEOUT", "dbx authentication timed out")
        except httpx.HTTPError:
            fail(503, "DBX_UNREACHABLE", "dbx metadata service is unreachable")
        if response.status_code == 401:
            fail(503, "DBX_AUTH_FAILED", "dbx session authentication failed")
        if response.status_code == 403:
            fail(503, "DBX_AUTH_SETUP_REQUIRED", "dbx requires first-run password setup")
        if response.status_code == 429:
            fail(503, "DBX_AUTH_LOCKED", "dbx locked authentication after repeated failures")
        if response.status_code not in {200, 204}:
            self._upstream_failure(response.status_code)

    @staticmethod
    def _logout(client: httpx.Client) -> None:
        try:
            client.post("/api/auth/logout", timeout=2.0)
        except httpx.HTTPError:
            pass

    @staticmethod
    def _verify_runtime(client: httpx.Client) -> None:
        try:
            response = client.get("/api/version")
        except httpx.TimeoutException:
            fail(503, "DBX_TIMEOUT", "dbx version handshake timed out")
        except httpx.HTTPError:
            fail(503, "DBX_UNREACHABLE", "dbx metadata service is unreachable")
        if response.status_code != 200:
            fail(503, "DBX_ENDPOINT_UNAVAILABLE", "Pinned dbx version endpoint is unavailable")
        try:
            payload = response.json()
        except ValueError:
            fail(503, "DBX_RESPONSE_INVALID", "dbx version response is invalid")
        if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
            fail(503, "DBX_RESPONSE_INVALID", "dbx version response is invalid")
        if payload["version"] != DBX_VERSION:
            fail(503, "DBX_VERSION_MISMATCH",
                 f"dbx runtime version {payload['version']} does not match the pinned release {DBX_VERSION}")
        if payload.get("demoMode") is True:
            fail(503, "DBX_DEMO_MODE_UNSUPPORTED", "dbx demo mode cannot be used for source registration")

    def _open_client(self, *, probe: bool = False) -> httpx.Client:
        base_url, password = self._config()
        client = _http_client(base_url, _PROBE_TIMEOUT if probe else _OPERATION_TIMEOUT)
        try:
            self._authenticate(client, password)
            self._verify_runtime(client)
        except BaseException:
            DbxMetadataAdapter._logout(client)
            client.close()
            raise
        return client

    @contextmanager
    def _runtime(self, connection):
        client = self._open_client()
        config = None
        try:
            config = self._connection_config(connection)
            self._json(client, "POST", "/api/connection/connect", body={"config": config})
            yield client, config
        finally:
            if config is not None:
                try:
                    self._json(client, "POST", "/api/connection/disconnect",
                               body={"connectionId": config["id"]})
                except Exception:
                    # Cleanup must not replace a metadata failure.  The
                    # connection id is unique per operation, so a missed
                    # disconnect cannot be confused with another request.
                    pass
            self._logout(client)
            try:
                client.close()
            except Exception:
                pass

    # ------------------------------------------------------------ connection
    @staticmethod
    def _endpoint_host(value) -> str:
        if not isinstance(value, str):
            fail(422, "DBX_ENDPOINT_INVALID", "dbx_host must be a hostname or IP literal string")
        host = value.strip()
        if not host or len(host) > 253 or "%" in host:
            fail(422, "DBX_ENDPOINT_INVALID", "dbx_host is invalid")
        if any(character.isspace() or character in "/\\@?#"
               or character == "\x00" for character in host):
            fail(422, "DBX_ENDPOINT_INVALID", "dbx_host is invalid")
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass
        if not _HOST_PATTERN.fullmatch(host):
            fail(422, "DBX_ENDPOINT_INVALID", "dbx_host is invalid")
        return host

    @staticmethod
    def _endpoint_port(value) -> int:
        if isinstance(value, bool) or value is None:
            fail(422, "DBX_ENDPOINT_INVALID", "dbx_port must be an integer port")
        if isinstance(value, int):
            port = value
        elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,4}", value):
            port = int(value)
        else:
            fail(422, "DBX_ENDPOINT_INVALID", "dbx_port must be an integer port")
        if not 1 <= port <= 65535:
            fail(422, "DBX_ENDPOINT_INVALID", "dbx_port must be between 1 and 65535")
        return port

    def _dbx_endpoint(self, raw: dict, host: str, port: int) -> tuple[str, int]:
        # Presence, not truthiness: an explicitly empty/null pair must fail
        # rather than silently falling back to the native endpoint.
        has_host = "dbx_host" in raw
        has_port = "dbx_port" in raw
        if not has_host and not has_port:
            return host, port
        if has_host != has_port:
            fail(422, "DBX_ENDPOINT_INCOMPLETE",
                 "dbx_host and dbx_port must be supplied together")
        return self._endpoint_host(raw.get("dbx_host")), self._endpoint_port(raw.get("dbx_port"))

    def _connection_config(self, connection) -> dict:
        from app.data_channel.integrated.native import connection_config

        raw = connection_config(connection)
        if not isinstance(raw, dict):
            fail(422, "DBX_CONNECTION_INCOMPLETE", "Source connection configuration is invalid")
        kind = connection.kind
        if kind not in {"mysql", "postgres"}:
            fail(422, "UNSUPPORTED_SOURCE", "Only MySQL/PostgreSQL integrated sources are supported")
        default_port = 5432 if kind == "postgres" else 3306
        if raw.get("connection_string"):
            try:
                url = make_url(str(raw["connection_string"]))
            except Exception:
                fail(422, "DBX_CONNECTION_INCOMPLETE", "Source connection string is invalid")
            # sqlalchemy.engine.URL has no fragment attribute; only query
            # options (TLS/session settings) are outside the lossless mapping.
            if url.query:
                fail(422, "DBX_CONNECTION_STRING_UNSUPPORTED",
                     "Connection strings with query/TLS options cannot be mapped losslessly to dbx")
            backend = url.get_backend_name()
            if (kind == "postgres" and backend not in {"postgres", "postgresql"}) or (
                    kind == "mysql" and backend != "mysql"):
                fail(422, "SOURCE_DRIVER_MISMATCH", "Connection driver does not match source kind")
            host, port = url.host, url.port or default_port
            username, password = url.username or "", url.password or ""
            database = url.database
        else:
            if any(raw.get(key) for key in _TLS_KEYS):
                fail(422, "DBX_TLS_CONFIG_UNSUPPORTED",
                     "TLS source options cannot be mapped losslessly to dbx")
            host = str(raw.get("host") or "").strip()
            username = str(raw.get("user", raw.get("username")) or "")
            password = str(raw.get("password") or "")
            database = raw.get("database")
            try:
                port = int(raw.get("port") or default_port)
            except (TypeError, ValueError):
                fail(422, "DBX_CONNECTION_INCOMPLETE", "Source port is invalid")
        if not host:
            fail(422, "DBX_CONNECTION_INCOMPLETE", "Source host is required for dbx discovery")
        if not 1 <= port <= 65535:
            fail(422, "DBX_CONNECTION_INCOMPLETE", "Source port is invalid")
        host, port = self._dbx_endpoint(raw, host, port)
        if database is not None:
            database = str(database).strip() or None
        if kind == "postgres" and not database:
            fail(422, "DBX_DATABASE_REQUIRED", "PostgreSQL dbx discovery requires an explicit database")
        return {
            "id": f"platform-{connection.id}-{uuid.uuid4().hex[:12]}",
            "name": str(connection.name or "integrated source")[:200],
            "db_type": "postgres" if kind == "postgres" else "mysql",
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "database": database,
            "connect_timeout_secs": 5,
            "query_timeout_secs": 30,
        }

    # -------------------------------------------------------------- metadata
    @staticmethod
    def _query_database(connection, config, schema_name):
        """Return (database, schema) for pinned dbx schema endpoints."""
        if connection.kind == "postgres":
            return config["database"], schema_name
        return schema_name, ""

    @staticmethod
    def _check_identifier(value, label: str) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            fail(422, "INVALID_RESOURCE", f"A physical {label} name is required")
        return value

    def _tables(self, client, runtime_id: str, database: str, schema: str, *,
                filter_name: str | None = None) -> list[dict]:
        tables: list[dict] = []
        offset = 0
        while True:
            params = {"connection_id": runtime_id, "database": database,
                      "schema": schema, "limit": _PAGE, "offset": offset}
            if filter_name is not None:
                params["filter"] = filter_name
            page = self._json(client, "GET", "/api/schema/tables", params=params)
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                fail(503, "DBX_RESPONSE_INVALID", "dbx table metadata response is invalid")
            if not page:
                return tables
            tables.extend(page)
            if len(tables) > _MAX_TABLES:
                fail(422, "DBX_DISCOVERY_TOO_LARGE",
                     "dbx discovery exceeds the bounded table count; narrow the source scope")
            offset += len(page)

    def _find_table(self, client, runtime_id: str, database: str, schema: str,
                    table_name: str) -> dict:
        for table in self._tables(client, runtime_id, database, schema, filter_name=table_name):
            if table.get("name") == table_name:
                return table
        fail(404, "DBX_TABLE_NOT_FOUND", "dbx does not report this source table")

    def list_resources(self, connection) -> list[dict]:
        with self._runtime(connection) as (client, config):
            runtime_id = config["id"]
            if connection.kind == "postgres":
                database = config["database"]
                schemas = self._json(client, "GET", "/api/schema/schemas",
                                     params={"connection_id": runtime_id, "database": database,
                                             "apply_visible_filter": "true"})
                if not isinstance(schemas, list) or not all(isinstance(name, str) for name in schemas):
                    fail(503, "DBX_RESPONSE_INVALID", "dbx schema metadata response is invalid")
                schemas = [name for name in schemas
                           if name and name not in {"pg_catalog", "information_schema", "pg_toast"}
                           and not name.startswith(("pg_temp_", "pg_toast_temp_"))]
                if len(schemas) > _MAX_SCHEMAS:
                    fail(422, "DBX_DISCOVERY_TOO_LARGE", "dbx discovery exceeds the bounded schema count")
                resources = []
                for schema in schemas:
                    for table in self._tables(client, runtime_id, database, schema):
                        if isinstance(table.get("name"), str) and table["name"]:
                            resources.append({"schema_name": schema, "table_name": table["name"]})
                        if len(resources) > _MAX_TABLES:
                            fail(422, "DBX_DISCOVERY_TOO_LARGE",
                                 "dbx discovery exceeds the bounded table count")
            else:
                database = config["database"]
                if database:
                    databases = [database]
                else:
                    payload = self._json(client, "GET", "/api/schema/databases",
                                         params={"connection_id": runtime_id})
                    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                        fail(503, "DBX_RESPONSE_INVALID", "dbx database metadata response is invalid")
                    databases = [item.get("name") for item in payload]
                    databases = [name for name in databases
                                 if isinstance(name, str) and name
                                 and name not in {"information_schema", "mysql",
                                                  "performance_schema", "sys"}]
                    if len(databases) > _MAX_DATABASES:
                        fail(422, "DBX_DISCOVERY_TOO_LARGE",
                             "dbx discovery exceeds the bounded database count")
                resources = []
                for name in databases:
                    for table in self._tables(client, runtime_id, name, ""):
                        if isinstance(table.get("name"), str) and table["name"]:
                            resources.append({"schema_name": name, "table_name": table["name"]})
                        if len(resources) > _MAX_TABLES:
                            fail(422, "DBX_DISCOVERY_TOO_LARGE",
                                 "dbx discovery exceeds the bounded table count")
        unique = {(item["schema_name"], item["table_name"]): item for item in resources}
        return [unique[key] for key in sorted(unique)]

    def describe_table(self, connection, schema_name: str, table_name: str) -> dict:
        self._check_identifier(schema_name, "schema")
        self._check_identifier(table_name, "table")
        with self._runtime(connection) as (client, config):
            runtime_id = config["id"]
            database, schema = self._query_database(connection, config, schema_name)
            table = self._find_table(client, runtime_id, database, schema, table_name)
            columns = self._json(client, "GET", "/api/schema/columns",
                                 params={"connection_id": runtime_id, "database": database,
                                         "schema": schema, "table": table_name})
            if not isinstance(columns, list) or not columns:
                fail(503, "DBX_METADATA_INCOMPLETE",
                     "dbx did not return authoritative column metadata")
            if len(columns) > _MAX_COLUMNS:
                fail(422, "DBX_DISCOVERY_TOO_LARGE",
                     "dbx discovery exceeds the bounded column count")
            evidence = []
            seen = set()
            for column in columns:
                if not isinstance(column, dict):
                    fail(503, "DBX_RESPONSE_INVALID", "dbx column metadata response is invalid")
                name = column.get("name")
                data_type = column.get("data_type")
                nullable = column.get("is_nullable")
                primary = column.get("is_primary_key")
                if (not isinstance(name, str) or not name or name in seen
                        or not isinstance(data_type, str) or not data_type
                        or not isinstance(nullable, bool) or not isinstance(primary, bool)):
                    fail(503, "DBX_RESPONSE_INVALID", "dbx column metadata response is invalid")
                seen.add(name)
                evidence.append({
                    "name": name, "data_type": data_type, "nullable": nullable,
                    "primary_key": primary,
                    "precision": column.get("numeric_precision"),
                    "scale": column.get("numeric_scale"),
                    "length": column.get("character_maximum_length"),
                    "collation": column.get("collation"),
                })
            return {
                "backend": "dbx-web-http",
                "commit": DBX_COMMIT,
                "version": DBX_VERSION,
                "table_name": table_name,
                "schema_name": schema_name,
                "table_type": table.get("table_type"),
                "columns": evidence,
                "primary_key": [column["name"] for column in evidence if column["primary_key"]],
            }
