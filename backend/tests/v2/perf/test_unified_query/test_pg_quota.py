"""Real PostgreSQL SERIALIZABLE quota race; run only in owned E2E stack."""
from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.data_channel.query.contracts import ColumnSpec, QueryError, TextQueryRequest, Relation
from app.data_channel.query.models import UnifiedQueryRow, UnifiedQuerySnapshot
from app.data_channel.query.service import QueryService

pytestmark = pytest.mark.skipif(os.environ.get("INTEGRATED_QUERY_E2E") != "1",
                                reason="explicit isolated real-PG opt-in required")


def test_real_pg_existing_nine_plus_two_concurrent_requests_never_exceed_ten():
    raw = os.environ.get("INTEGRATED_QUERY_PLATFORM_DSN")
    assert raw, "Explicit owned platform DSN required"
    url = sa.engine.make_url(raw)
    assert (url.host, url.port, url.database) == ("127.0.0.1", 45432, "integrated_metadata")
    engine = sa.create_engine(raw)
    UnifiedQuerySnapshot.__table__.create(engine, checkfirst=True)
    UnifiedQueryRow.__table__.create(engine, checkfirst=True)
    user = SimpleNamespace(id="uq-quota-" + uuid.uuid4().hex)
    now = datetime.now(timezone.utc)
    barrier = threading.Barrier(2)

    class QuotaService(QueryService):
        def _pin(self):
            super()._pin()
            # Force both serializable snapshots to exist before either quota
            # advisory lock can be acquired; exercises the stale-RR race.
            barrier.wait(timeout=20)

        def _resolve(self, request, pipeline):
            table = sa.select(sa.literal(1).label("id")).subquery()
            self.relations["p"] = Relation("p", table, {"id": ColumnSpec("id", "integer", False)},
                {"alias": "p", "kind": "dataset", "id": "fixture", "versionId": "fixture",
                 "asOf": now.isoformat(), "consistency": "immutable_version", "completeness": "complete"},
                "auth", "schema", "platform", platform=True)

    body = TextQueryRequest(language="spl", query="from fixture as p | stats count() as n",
                        bindings={"fixture": {"kind": "dataset", "datasetId": "fixture", "versionId": "fixture"}})
    try:
        with Session(engine) as db:
            for _ in range(9):
                db.add(UnifiedQuerySnapshot(id=str(uuid.uuid4()), owner_user_id=user.id,
                    query_hash="q", auth_fingerprint="a", schema_fingerprint="s", request={}, columns=[],
                    source_states=[], consistency="immutable_versions", plan={}, result_limited=False,
                    total_rows=0, result_bytes=0, page_size=1, created_at=now,
                    expires_at=now + timedelta(seconds=300)))
            # An expired row must not consume the active quota.
            db.add(UnifiedQuerySnapshot(id=str(uuid.uuid4()), owner_user_id=user.id,
                query_hash="q", auth_fingerprint="a", schema_fingerprint="s", request={}, columns=[],
                source_states=[], consistency="immutable_versions", plan={}, result_limited=False,
                total_rows=0, result_bytes=0, page_size=1, created_at=now - timedelta(seconds=400),
                expires_at=now - timedelta(seconds=1)))
            db.commit()

        def worker():
            with Session(engine) as db:
                try:
                    return ("success", QuotaService(db, user, secret="quota-test-only").execute(body))
                except QueryError as exc:
                    return (exc.code, exc.status)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        assert sum(kind == "success" for kind, _ in results) == 1
        assert all(kind in {"success", "query_retry_required", "query_snapshot_quota_exceeded"}
                   for kind, _ in results)
        with Session(engine) as db:
            active = db.scalar(sa.select(sa.func.count()).select_from(UnifiedQuerySnapshot).where(
                UnifiedQuerySnapshot.owner_user_id == user.id,
                UnifiedQuerySnapshot.expires_at > datetime.now(timezone.utc)))
            assert active == 10
    finally:
        with Session(engine) as db:
            ids = sa.select(UnifiedQuerySnapshot.id).where(UnifiedQuerySnapshot.owner_user_id == user.id)
            db.execute(sa.delete(UnifiedQueryRow).where(UnifiedQueryRow.snapshot_id.in_(ids)))
            db.execute(sa.delete(UnifiedQuerySnapshot).where(UnifiedQuerySnapshot.owner_user_id == user.id))
            db.commit()
        engine.dispose()
