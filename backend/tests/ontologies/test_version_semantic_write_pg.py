"""Opt-in real PostgreSQL concurrency proof for the draft semantic write.

Owns a unique schema in the explicitly authorized integrated fixture stack;
creates all tables there, seeds only synthetic rows, and drops the schema in
finally.  Not run unless INTEGRATED_NATIVE_PG_E2E=1.

Acceptance:
- writer-vs-writer: two real DB sessions race on the same revision:token;
  exactly one commits, the other gets 409, and exactly one semantic/audit row
  exists.
- writer-vs-structure: a real structure-writer session holds project/version
  row locks and advances revision; the semantic writer's old baseRevision waits,
  then gets 409 conflict without overwriting formal.
- real lock_timeout -> 409 write_lock_timeout when a lock is held past the
  canonical 5s bounded-lock window.
"""
from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.auth.models import User
from app.database import Base
from app.exploration import canvas as C
from app.exploration.document import canvas_fingerprint
from app.exploration.semantic_gate import semantic_consistency_issues
from app.model_registry import import_all_models
from app.ontologies.inference.models import AuditLog
from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions import semantic_service
from app.ontologies.versions.models import OntologyVersion
from app.ontologies.versions.snapshot_contract import complete_snapshot, snapshot_hash

pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATED_NATIVE_PG_E2E") != "1",
    reason="requires explicitly owned loopback PostgreSQL fixture stack",
)

PLATFORM_URL = (
    "postgresql+psycopg2://integrated_e2e:integrated_e2e_fixture"
    "@127.0.0.1:45432/integrated_metadata"
)


@pytest.fixture
def pg_schema():
    import_all_models()
    token = uuid.uuid4().hex
    schema = ("semantic_write_race_" + token)[:60]
    root = sa.create_engine(PLATFORM_URL, hide_parameters=True)
    engine = sa.create_engine(
        PLATFORM_URL,
        hide_parameters=True,
        connect_args={"options": (
            f"-csearch_path={schema} -cstatement_timeout=15000")},
    )
    created = False
    try:
        with root.begin() as conn:
            conn.execute(sa.schema.CreateSchema(schema))
            created = True
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        try:
            if created:
                with root.begin() as conn:
                    database = conn.execute(sa.text(
                        "SELECT current_database()")).scalar_one()
                    resolved = conn.execute(sa.text(
                        "SELECT nspname FROM pg_namespace WHERE nspname = :name"),
                        {"name": schema}).scalar_one_or_none()
                    # Only the UUID schema this fixture created, in the owned
                    # metadata database, is dropped.  Never public/other schemas.
                    assert database == "integrated_metadata", database
                    assert resolved == schema, (resolved, schema)
                    assert schema.startswith("semantic_write_race_"), schema
                    conn.execute(sa.schema.DropSchema(
                        schema, cascade=True, if_exists=True))
        finally:
            root.dispose()


def _seed(engine):
    formal = complete_snapshot({})
    with Session(engine) as db:
        user = User(
            id=str(uuid.uuid4()), username="sem_" + uuid.uuid4().hex[:16],
            email=f"sem-{uuid.uuid4().hex}@example.invalid",
            password_hash="synthetic-not-a-login", role="admin",
        )
        project = OntologyProject(
            id=str(uuid.uuid4()), name="semantic write PG fixture",
            domain="synthetic", created_by=user.id,
        )
        draft = OntologyVersion(
            id=str(uuid.uuid4()), ontology_id=project.id,
            version_number="v0.1", version_label="sync draft",
            node_kind="draft", lifecycle_status="editing",
            revision=0, snapshot_formal=formal,
            snapshot_hash=snapshot_hash(formal), created_by=user.id,
        )
        db.add_all([user, project, draft])
        db.commit()
        return {
            "user_id": user.id, "ontology_id": project.id,
            "version_id": draft.id,
            "base_revision": f"{draft.revision}:{draft.snapshot_hash}",
        }


def _body(base_revision: str) -> dict:
    canvas = C.empty_canvas()
    return {
        "baseRevision": base_revision,
        "canvas": canvas,
        "documentMd": "# synthetic semantic document\n\nconcurrency fixture\n",
        "documentTitle": "synthetic semantic concurrency",
    }


def _write(engine, ids, body):
    with Session(engine) as db:
        user = db.get(User, ids["user_id"])
        try:
            result = semantic_service.save_version_semantic(
                db, ids["ontology_id"], ids["version_id"], body, user,
                canvas_fingerprint_fn=canvas_fingerprint,
                semantic_consistency_fn=semantic_consistency_issues,
            )
            return {"ok": True, "data": result["data"]}
        except HTTPException as exc:
            return {"ok": False, "status": exc.status_code, "detail": exc.detail}


def _assert_single_winner(engine, ids):
    with Session(engine) as db:
        draft = db.get(OntologyVersion, ids["version_id"])
        assert draft.revision == 1
        assert draft.snapshot_semantic["semanticRevision"] == 1
        assert db.query(AuditLog).filter_by(
            ontology_id=ids["ontology_id"],
            event_subtype="version_semantic_saved",
        ).count() == 1


def test_real_pg_writer_vs_writer_same_cas(pg_schema):
    ids = _seed(pg_schema)
    body = _body(ids["base_revision"])
    barrier = Barrier(2)

    def race(_index):
        barrier.wait(timeout=10)
        return _write(pg_schema, ids, body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, (0, 1)))

    assert sum(1 for item in results if item["ok"]) == 1, results
    failures = [item for item in results if not item["ok"]]
    assert len(failures) == 1 and failures[0]["status"] == 409, failures
    assert failures[0]["detail"]["code"] == "conflict", failures
    _assert_single_winner(pg_schema, ids)


def test_real_pg_writer_vs_structure_waits_then_conflicts(pg_schema):
    ids = _seed(pg_schema)
    body = _body(ids["base_revision"])

    holder = Session(pg_schema)
    holder.begin()
    project = holder.query(OntologyProject).filter(
        OntologyProject.id == ids["ontology_id"]).with_for_update().one()
    draft = holder.query(OntologyVersion).filter(
        OntologyVersion.id == ids["version_id"]).with_for_update().one()
    formal_before = draft.snapshot_formal
    hash_before = draft.snapshot_hash
    draft.revision += 1
    holder.flush()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_write, pg_schema, ids, body)
        time.sleep(0.7)
        holder.commit()
        result = future.result(timeout=20)
    holder.close()

    assert result["ok"] is False
    assert result["status"] == 409
    assert result["detail"]["code"] == "conflict"
    with Session(pg_schema) as db:
        draft = db.get(OntologyVersion, ids["version_id"])
        assert draft.revision == 1
        assert draft.snapshot_semantic is None
        assert draft.snapshot_formal == formal_before
        assert draft.snapshot_hash == hash_before
        assert db.query(AuditLog).filter_by(
            ontology_id=ids["ontology_id"],
            event_subtype="version_semantic_saved",
        ).count() == 0


def test_real_pg_lock_timeout_maps_to_409(pg_schema):
    ids = _seed(pg_schema)
    body = _body(ids["base_revision"])

    holder = Session(pg_schema)
    holder.begin()
    holder.query(OntologyProject).filter(
        OntologyProject.id == ids["ontology_id"]).with_for_update().one()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_write, pg_schema, ids, body)
        # Canonical _bounded_row_locks sets 5s; hold beyond it.
        time.sleep(6.5)
        result = future.result(timeout=20)
        holder.rollback()
    holder.close()

    assert result["ok"] is False
    assert result["status"] == 409
    assert result["detail"]["code"] == "write_lock_timeout"
    with Session(pg_schema) as db:
        draft = db.get(OntologyVersion, ids["version_id"])
        assert draft.revision == 0
        assert draft.snapshot_semantic is None
        assert db.query(AuditLog).filter_by(
            ontology_id=ids["ontology_id"],
            event_subtype="version_semantic_saved",
        ).count() == 0
