"""Fail-closed connectivity checks for required production dependencies."""
from __future__ import annotations

import sys
import logging
from collections.abc import Callable

import psycopg2
import redis
import urllib3
from minio import Minio
from neo4j import GraphDatabase

from app.config import settings


logger = logging.getLogger("app.bootstrap.dependencies")


def probe_postgresql() -> None:
    connection = psycopg2.connect(
        settings.database_url,
        connect_timeout=5,
        application_name="openontology-deploy-check",
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                raise RuntimeError("PostgreSQL readiness query returned an invalid result")
    finally:
        connection.close()


def probe_redis() -> None:
    client = redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    try:
        if client.ping() is not True:
            raise RuntimeError("Redis PING returned an invalid result")
    finally:
        client.close()


def probe_neo4j() -> None:
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=5,
    )
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            record = session.run("RETURN 1 AS ready").single()
            if record is None or record["ready"] != 1:
                raise RuntimeError("Neo4j readiness query returned an invalid result")
    finally:
        driver.close()


def probe_minio() -> None:
    http = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=5, read=5),
        retries=urllib3.Retry(
            total=0,
            connect=0,
            read=0,
            redirect=0,
            status=0,
        ),
    )
    try:
        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_use_ssl,
            http_client=http,
        )
        client.list_buckets()
    finally:
        http.clear()


def probe_n8n() -> None:
    from app.settings.workflows.n8n_client import test_n8n_connection

    result = test_n8n_connection(
        settings.n8n_api_url,
        settings.n8n_api_key,
        timeout_seconds=min(settings.n8n_timeout_seconds, 10),
    )
    if not result.ok:
        raise RuntimeError("n8n readiness returned an invalid result")


def probe_browser() -> None:
    from app.data_channel.steward.browser_runtime import probe_browser_cdp

    result = probe_browser_cdp()
    if not result.get("reachable"):
        raise RuntimeError("Chromium CDP is unavailable")


def probe_weknora() -> None:
    """Probe only the WeKnora HTTP service.

    Models, knowledge bases and MCP endpoints are configured by operators in
    WeKnora's native Web; startup/readiness must not inspect them.
    """
    import urllib.request
    from urllib.parse import urlsplit

    from app.shared.http_transport import open_with_loopback_bypass

    base = str(settings.weknora_base_url or "").strip()
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
    ):
        raise RuntimeError("WEKNORA_BASE_URL is not an absolute HTTP(S) origin")
    request = urllib.request.Request(
        f"{base.rstrip('/')}/health", headers={"Connection": "close"},
    )
    with open_with_loopback_bypass(
        request, timeout=min(settings.weknora_timeout_seconds, 5),
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"WeKnora health returned HTTP {response.status}")
        response.read(4096)


def probe_kaneo() -> None:
    """Probe the Kaneo service through its DB-backed instance status.

    Kaneo's ``/api/health`` is a static 200 that never touches the database,
    so readiness must read ``/api/instance/status`` and validate the payload:
    only a real service+database path answers with the boolean
    ``hasUsers``/``hasAdmin`` fields, and a deployment-ready instance must
    report both true (the bootstrap administrator exists). Projects and
    tasks are configured by operators in Kaneo's native Web; the probe never
    inspects them.
    """
    import json
    import urllib.request
    from urllib.parse import urlsplit

    from app.shared.http_transport import open_with_loopback_bypass

    base = str(settings.kaneo_base_url or "").strip()
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
    ):
        raise RuntimeError("KANEO_BASE_URL is not an absolute HTTP(S) origin")
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/instance/status",
        headers={"Connection": "close"},
    )
    with open_with_loopback_bypass(
        request, timeout=min(settings.kaneo_timeout_seconds, 5),
    ) as response:
        if response.status != 200:
            raise RuntimeError(
                f"Kaneo instance status returned HTTP {response.status}"
            )
        body = response.read(4096)
    try:
        payload = json.loads(body)
    except ValueError:
        raise RuntimeError("Kaneo instance status returned invalid JSON") from None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("hasUsers"), bool)
        or not isinstance(payload.get("hasAdmin"), bool)
    ):
        raise RuntimeError("Kaneo instance status returned an invalid payload")
    if not (payload["hasUsers"] and payload["hasAdmin"]):
        raise RuntimeError("Kaneo instance is not bootstrapped")


def probe_dbx() -> None:
    """Authenticate and verify the pinned dbx release without leaking config."""
    from app.data_channel.integrated.dbx_adapter import DbxMetadataAdapter

    status = DbxMetadataAdapter().status()
    if status.get("configured") is not True:
        raise RuntimeError("DBX_NOT_CONFIGURED")
    if status.get("available") is not True:
        raise RuntimeError(str(status.get("reason") or "DBX_UNAVAILABLE"))


def probe_grafana() -> None:
    """Probe the pinned Grafana via its real client surface.

    Readiness is not a static health ping: ``/api/health`` must report
    ``database=ok`` AND an authenticated basic ``/api/user`` must be the
    shared admin (``isGrafanaAdmin=true``). The probe reuses the pinned
    13.3.1 client, whose errors never expose URLs, credentials, or upstream
    bodies. Missing credentials fail closed here (readiness marks Grafana
    unavailable); importing config without credentials stays legal.
    """
    from app.operations.grafana import client as grafana_client

    client = grafana_client.build_client()
    if not client.health_database_ok():
        raise RuntimeError("Grafana health database is not ok")
    if not client.current_user_is_admin():
        raise RuntimeError("Grafana shared admin check failed")


_MULTICA_DEFAULT_SETTING_KEYS = (
    "multica_default_base_url",
    "multica_default_web_url",
    "multica_default_workspace_id",
    "multica_default_workspace_name",
    "multica_default_pat",
)


def multica_default_config_state() -> str:
    """Classify the five deployment-default Multica values.

    Returns ``complete`` (all five set), ``partial`` (some set), or
    ``disabled`` (all empty). A partial set is never silently healthy: it
    means the deployment intended a default but cannot deliver one, so
    callers must surface it explicitly instead of treating it as
    unconfigured (readiness marks it unavailable).
    """
    values = [
        str(getattr(settings, key) or "").strip()
        for key in _MULTICA_DEFAULT_SETTING_KEYS
    ]
    if all(values):
        return "complete"
    if not any(values):
        return "disabled"
    return "partial"


def probe_multica_default() -> None:
    """Readiness probe for the optional deployment-default Multica service.

    Verifies the same live contract as profile activation: the deployment
    PAT authenticates (``GET /api/me``) and the configured workspace is
    visible to it (``GET /api/workspaces`` membership). Both calls go
    through ``multica_client`` — base URL validated against the independent
    Multica allowlist, redirects never followed so the PAT cannot leak to
    another host, and every request bounded. Non-complete settings (for
    example the PAT-masked env_file services) skip the probe: classifying
    partial/disabled is the caller's job, and an unauthenticated probe is
    meaningless. Failures raise static component text only and cut the
    upstream exception chain, so URLs, PATs, and response bodies can never
    reach logs or readiness output.
    """
    from app.super_assistant import multica_client

    if multica_default_config_state() != "complete":
        return
    base_url = settings.multica_default_base_url.strip()
    token = settings.multica_default_pat.strip()
    workspace_id = settings.multica_default_workspace_id.strip()
    try:
        multica_client.fetch_me(base_url, token)
        workspaces = multica_client.list_workspaces(base_url, token)
    except Exception:
        raise RuntimeError(
            "Multica default service is unavailable"
        ) from None
    if not any(str(item.get("id") or "") == workspace_id for item in workspaces):
        raise RuntimeError(
            "Multica default workspace is not visible to the default PAT"
        )


PROBES: tuple[tuple[str, Callable[[], None]], ...] = (
    ("PostgreSQL", probe_postgresql),
    ("Redis", probe_redis),
    ("Neo4j", probe_neo4j),
    ("MinIO", probe_minio),
    ("n8n", probe_n8n),
    ("WeKnora", probe_weknora),
    ("Kaneo", probe_kaneo),
    ("Grafana", probe_grafana),
    ("Chromium CDP", probe_browser),
    ("Multica default", probe_multica_default),
)
# Grafana is deliberately advisory at startup: a runtime failure marks deep
# readiness 503 while every other service keeps running; deployment-level
# fail-closed startup is owned by the grafana-bootstrap service.
# Multica default never blocks process startup (the bundled stack starts
# after the core deployment), but production readiness gates on it once the
# five-value default is configured: the platform-default profile is
# materialized from these settings and auto-selected as the user default,
# so an unreachable default service is a real configured failure, not an
# optional per-user convenience. Development/test readiness stays advisory.
NON_BLOCKING_PROBES = frozenset({"Chromium CDP", "Grafana", "Multica default"})


def probe_startup_dependencies() -> None:
    """Verify runtime dependencies before any owned worker starts.

    CDP is deliberately advisory: its endpoint must be configured, and deep
    readiness reports it, but a temporarily unavailable browser must not hide
    the API diagnostics needed to repair that browser. n8n gets the same
    advisory treatment outside production so a developer without an n8n
    instance can still boot the API; production keeps n8n fail-closed.
    The optional Multica default integration never blocks startup in any
    environment (the bundled stack is started after the core deployment);
    production readiness still fails closed while a default is configured.
    All other configured services are part of the process-start contract.
    """
    failed: list[str] = []
    for name, probe in PROBES:
        try:
            probe()
        except Exception as exc:
            advisory = name in NON_BLOCKING_PROBES or (
                name == "n8n" and settings.environment != "production"
            )
            if advisory:
                logger.warning(
                    "%s is unavailable during startup (%s); "
                    "API startup will continue but deep readiness will fail",
                    name,
                    type(exc).__name__,
                )
            else:
                failed.append(name)

    if failed:
        raise RuntimeError(
            "Required startup dependencies unavailable: " + ", ".join(failed)
        )


def main() -> int:
    failed: list[str] = []
    probes = list(PROBES)
    if settings.environment == "production":
        # dbx is production-only. Test/development startup keeps its native
        # PostgreSQL fixtures and never needs an external dbx service.
        probes.append(("dbx", probe_dbx))
    for name, probe in probes:
        try:
            probe()
        except Exception as exc:
            # Never render connection strings or exception messages: driver
            # errors may embed credentials. Component and exception type are
            # enough to diagnose the failed boundary without leaking secrets.
            suffix = (
                " advisory; API may start but readiness will fail"
                if name in NON_BLOCKING_PROBES else ""
            )
            print(
                f"{name}: unavailable ({type(exc).__name__}){suffix}",
                file=sys.stderr,
            )
            if name not in NON_BLOCKING_PROBES:
                failed.append(name)
        else:
            print(f"{name}: ok")

    if failed:
        print(
            "Required production dependency preflight failed: "
            + ", ".join(failed),
            file=sys.stderr,
        )
        return 1
    print("Required production dependency preflight succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
