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


PROBES: tuple[tuple[str, Callable[[], None]], ...] = (
    ("PostgreSQL", probe_postgresql),
    ("Redis", probe_redis),
    ("Neo4j", probe_neo4j),
    ("MinIO", probe_minio),
    ("n8n", probe_n8n),
    ("Chromium CDP", probe_browser),
)
NON_BLOCKING_PROBES = frozenset({"Chromium CDP"})


def probe_startup_dependencies() -> None:
    """Verify runtime dependencies before any owned worker starts.

    CDP is deliberately advisory: its endpoint must be configured, and deep
    readiness reports it, but a temporarily unavailable browser must not hide
    the API diagnostics needed to repair that browser. n8n gets the same
    advisory treatment outside production so a developer without an n8n
    instance can still boot the API; production keeps n8n fail-closed.
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
    for name, probe in PROBES:
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
