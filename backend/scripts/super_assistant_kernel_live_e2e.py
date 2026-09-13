"""Run the kernel.v1 staging prerequisite and contract checks.

This verifier intentionally uses real PostgreSQL, NATS JetStream, MinIO and
Neo4j endpoints configured through the normal environment. It never creates
or mutates business data; it writes a machine-readable report under
``.artifacts/`` and exits non-zero when a required dependency or contract is
not ready. The full synthetic connector matrix can be layered on this stable
probe without changing production code.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _result(name: str, ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, **extra}


def _database_check() -> dict[str, Any]:
    try:
        from sqlalchemy import text
        from app.shared.database import SessionLocal

        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        return _result("postgresql", True, "SELECT 1 succeeded")
    except Exception as exc:  # noqa: BLE001 - report exact staging failure
        return _result("postgresql", False, f"{exc.__class__.__name__}: {exc}")


async def _nats_check() -> dict[str, Any]:
    try:
        import nats
        from app.shared.config import settings
        from app.data_channel.pipeline_tasks.dispatch import (
            EXECUTION_CALL_SUBJECT,
            EXECUTION_RECONCILE_SUBJECT,
            EXECUTION_RUN_SUBJECT,
            EXECUTION_STREAM,
        )

        nc = await nats.connect(settings.nats_url, connect_timeout=5)
        try:
            info = await nc.jetstream().stream_info(EXECUTION_STREAM)
        finally:
            await nc.drain()
        subjects = {str(value) for value in (info.config.subjects or [])}
        required = {EXECUTION_RUN_SUBJECT, EXECUTION_CALL_SUBJECT, EXECUTION_RECONCILE_SUBJECT}
        missing = sorted(required - subjects)
        if missing:
            return _result("nats_jetstream", False, "required subjects are missing", missing=missing)
        return _result("nats_jetstream", True, "SA_EXECUTION_V1 subjects verified", subjects=sorted(subjects))
    except Exception as exc:  # noqa: BLE001
        return _result("nats_jetstream", False, f"{exc.__class__.__name__}: {exc}")


def _minio_check() -> dict[str, Any]:
    try:
        from app.shared.config import settings
        from minio import Minio

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=bool(settings.minio_use_ssl),
        )
        bucket = settings.minio_mcp_bucket
        if not client.bucket_exists(bucket):
            return _result("minio", False, f"bucket {bucket!r} does not exist", bucket=bucket)
        return _result("minio", True, "artifact bucket is reachable", bucket=bucket)
    except Exception as exc:  # noqa: BLE001
        return _result("minio", False, f"{exc.__class__.__name__}: {exc}")


def _neo4j_check() -> dict[str, Any]:
    try:
        from app.shared.config import settings
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
        try:
            with driver.session() as session:
                session.run("RETURN 1").consume()
        finally:
            driver.close()
        return _result("neo4j", True, "RETURN 1 succeeded")
    except Exception as exc:  # noqa: BLE001
        return _result("neo4j", False, f"{exc.__class__.__name__}: {exc}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".artifacts/super-assistant-kernel-live-e2e.json")
    parser.add_argument("--allow-missing", action="store_true", help="write the report but return zero despite missing staging dependencies")
    args = parser.parse_args()
    checks = [_database_check(), await _nats_check(), _minio_check(), _neo4j_check()]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": os.getenv("ENVIRONMENT", ""),
        "checks": checks,
        "ok": all(item["ok"] for item in checks),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] or args.allow_missing else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
