"""Process liveness and dependency-readiness implementation."""
from __future__ import annotations

import urllib.request
from collections.abc import Callable

from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.shared.http_transport import open_with_loopback_bypass


def liveness_payload() -> dict[str, str]:
    return {"status": "ok"}


def probe_http_service(url: str, *, timeout: float = 3.0) -> None:
    """Probe an internal HTTP service without leaving a pooled socket behind.

    A short-lived stdlib request with ``Connection: close`` keeps repeated
    readiness probes bounded and makes ownership of the socket explicit.
    """
    request = urllib.request.Request(url, headers={"Connection": "close"})
    with open_with_loopback_bypass(request, timeout=timeout) as response:
        if response.status >= 400:
            raise RuntimeError(f"health probe returned HTTP {response.status}")
        # Consume a bounded response so the connection can close cleanly.
        response.read(4096)


def readiness_response(
    db: Session,
    *,
    probe: Callable[[str], None],
) -> JSONResponse:
    checks = {
        "status": "ok",
        "db": "unknown",
        "redis": "unknown",
        "neo4j": "unknown",
        "minio": "unknown",
        "object_storage": "unknown",
        "browser": "unknown",
        "n8n": "unknown",
        "sentinel_scheduler": "unknown",
        "sentinel_cdc": "unknown",
        "data_scheduler": "unknown",
        "ontology_projection": "unknown",
    }
    is_production = settings.environment == "production"
    if is_production:
        # dbx metadata is a production-only availability boundary; local and
        # test profiles keep their native-PostgreSQL fixtures without dbx.
        checks["dbx"] = "unknown"

    # PostgreSQL check
    try:
        db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        checks["db"] = "error"

    # Redis/Celery broker check. A TCP accept is not readiness: authenticate
    # and execute PING so a misconfigured or loading Redis fails closed.
    try:
        import redis

        redis_client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
        )
        try:
            if redis_client.ping() is not True:
                raise RuntimeError("Redis PING returned an invalid result")
        finally:
            redis_client.close()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "unavailable"

    # Neo4j check
    driver = None
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        driver.verify_connectivity()
        from app.ontologies.graph.neo4j_service import Neo4jService

        Neo4jService.clear_unavailable_backoff()
        checks["neo4j"] = "ok"
    except Exception:
        checks["neo4j"] = "unavailable"
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass

    # MinIO readiness performs a new bounded, authenticated request on every
    # call. The process-scoped storage client's cached ``available`` flag is
    # deliberately insufficient: MinIO may have failed since it connected.
    # No local filesystem or legacy managed endpoint can satisfy readiness.
    try:
        from app.shared.dependency_probe import probe_minio
        from app.shared.storage import clear_environment_storage_backoff

        probe_minio()
        clear_environment_storage_backoff()
        checks["minio"] = "ok"
        checks["object_storage"] = "minio"
    except Exception:
        checks["minio"] = "unavailable"
        checks["object_storage"] = "unavailable"

    # Data-steward Chromium/CDP readiness. A running container is insufficient:
    # the image may be alive while its internal CDP bridge is misconfigured.
    try:
        from app.data_channel.steward.browser_runtime import probe_browser_cdp

        checks["browser"] = (
            "ok" if probe_browser_cdp()["reachable"] else "unavailable"
        )
    except Exception:
        checks["browser"] = "unavailable"

    # n8n is environment-managed. Readiness uses a dedicated short timeout so
    # a slow business workflow cannot occupy health-check worker threads.
    try:
        from app.shared.dependency_probe import probe_n8n

        probe_n8n()
        checks["n8n"] = "ok"
    except Exception:
        checks["n8n"] = "unavailable"

    if is_production:
        # Reuse the pinned adapter's bounded status probe. It authenticates and
        # verifies the pinned version without exposing URL/password/error text.
        try:
            from app.data_channel.integrated.dbx_adapter import DbxMetadataAdapter

            dbx_status = DbxMetadataAdapter().status()
            if dbx_status.get("configured") is True and dbx_status.get("available") is True:
                checks["dbx"] = "ok"
            else:
                checks["dbx"] = "unavailable"
                checks["dbx_reason"] = str(
                    dbx_status.get("reason") or "DBX_UNAVAILABLE"
                )
        except Exception:
            checks["dbx"] = "unavailable"
            checks["dbx_reason"] = "DBX_UNAVAILABLE"

    try:
        from app.ontologies.sentinels.scan_worker import scan_worker_status

        sentinel_status = scan_worker_status()
        checks["sentinel_scheduler"] = (
            "ok"
            if sentinel_status["alive"] and not sentinel_status["last_error"]
            else "unavailable"
        )
    except Exception:
        checks["sentinel_scheduler"] = "unavailable"

    try:
        from app.ontologies.sentinels.cdc import cdc_dispatch_status

        cdc_status = cdc_dispatch_status(
            session_factory=sessionmaker(
                bind=db.get_bind(),
                expire_on_commit=False,
            )
        )
        checks["sentinel_cdc"] = (
            "ok" if cdc_status["healthy"] else "unavailable"
        )
        checks["sentinel_cdc_detail"] = {
            "quiescent": cdc_status["quiescent"],
            "worker_alive": cdc_status["worker_alive"],
            "queued": cdc_status["queued"],
            "durable": cdc_status["durable"],
            "error_count": len(cdc_status["last_errors"]),
            "error_code": (
                "dispatch_error"
                if cdc_status["last_error"] or cdc_status["last_errors"]
                else None
            ),
        }
    except Exception:
        checks["sentinel_cdc"] = "unavailable"
        checks["sentinel_cdc_detail"] = {
            "quiescent": False,
            "error_code": "status_unavailable",
        }

    try:
        from app.data_channel.sync_tasks.scheduler import get_sync_scheduler

        scheduler = get_sync_scheduler()
        checks["data_scheduler"] = (
            "ok" if scheduler.healthy else "unavailable"
        )
    except Exception:
        checks["data_scheduler"] = "unavailable"

    try:
        from app.models.ontology import OntologyProject
        from app.ontologies.mappings.models import OntologyMapping

        published_ids = [
            item[0]
            for item in db.query(OntologyProject.id)
            .filter(OntologyProject.status == "published")
            .all()
        ]
        unhealthy = 0
        if published_ids:
            unhealthy = db.query(OntologyProject).filter(
                OntologyProject.id.in_(published_ids),
                OntologyProject.projection_status != "ready",
            ).count()
            unhealthy += (
                db.query(OntologyMapping)
                .filter(
                    OntologyMapping.ontology_id.in_(published_ids),
                    OntologyMapping.status != "applied",
                )
                .count()
            )
        checks["ontology_projection"] = (
            "ok" if unhealthy == 0 else "unavailable"
        )
    except Exception:
        checks["ontology_projection"] = "unavailable"

    service_keys = (
        "db",
        "redis",
        "neo4j",
        "minio",
        "browser",
        "n8n",
        *(("dbx",) if is_production else ()),
        "sentinel_scheduler",
        "data_scheduler",
        "sentinel_cdc",
        "ontology_projection",
    )
    unavailable = [name for name in service_keys if checks[name] != "ok"]
    checks["status"] = "ok" if not unavailable else "error"
    checks["unavailable"] = unavailable
    return JSONResponse(
        status_code=503 if unavailable else 200,
        content=checks,
    )
