#!/usr/bin/env python3
"""Owned PostgreSQL migration and fact-storage probes, NOT full E2E.

Run with the repository venv against a sanitized --tests runtime-copy. Creates
one uniquely named database on the owned metadata container, upgrades the actual
Alembic chain, rolls back to an explicitly reported revision and upgrades again,
then measures 47-property facts through the production bounded fact API. The probe always drops its own
comment-verified database. No source table or shared metadata DB is changed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DSN = "postgresql+psycopg2://integrated_e2e:integrated_e2e_fixture@127.0.0.1:45432/integrated_metadata"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10000, choices=(500, 10000))
    parser.add_argument("--facts", action="store_true")
    parser.add_argument("--rollback-target", choices=("base", "0133_mcp_constrained_flag"), default="base",
                        help="base verifies all history; 0133 limits rollback/reupgrade to this feature's migrations")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    destination = args.report.resolve()
    if not destination.is_relative_to((ROOT / ".artifacts").resolve()):
        parser.error("report must be in this runtime's .artifacts directory")
    if (ROOT / "backend/.env").exists() or (ROOT / ".env").exists():
        parser.error("use a sanitized runtime-copy without dotenv files")
    sys.path.insert(0, str(ROOT / "backend"))
    sys.path.insert(0, str(ROOT / "scripts/e2e"))
    from integrated_stack import require_owned
    require_owned("db")
    import sqlalchemy as sa
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import NullPool

    name = "integrated_probe_" + uuid.uuid4().hex
    assert re.fullmatch(r"integrated_probe_[0-9a-f]{32}", name)
    marker = "integrated-e2e-probe:" + name
    admin = sa.create_engine(FIXTURE_DSN, isolation_level="AUTOCOMMIT", poolclass=NullPool, hide_parameters=True)
    url = make_url(FIXTURE_DSN).set(database=name)
    # This is a direct service/migration test, not an application startup or
    # deep-readiness probe. No n8n/LLM availability is asserted or fabricated.
    os.environ["ENVIRONMENT"] = "test"
    os.environ["DATABASE_URL"] = url.set(drivername="postgresql").render_as_string(hide_password=False)
    os.environ["SECRET_KEY"] = "integrated_e2e_public_fixture_secret_not_production"
    engine = None
    created = False
    report = {"full_e2e_pass": False, "kind": "postgresql_service_probe_not_full_chain",
              "database": name, "cases": {}, "rows": args.rows, "columns": 47,
              "rollback_target": args.rollback_target,
              "all_history_rollback_requested": args.rollback_target == "base",
              "lineage_note": "Capacity probe supplies explicit synthetic release/version IDs; does NOT run promotion or mapping",
              "not_verified": ["dbx", "HTTP registration", "browser", "mapping/trial/promote", "Neo4j", "Sentinel", "genuine assistant"]}
    started = time.monotonic()
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
            created = True
            connection.exec_driver_sql(f"COMMENT ON DATABASE \"{name}\" IS '{marker}'")
        from alembic import command
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        config = Config(str(ROOT / "backend/alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "backend/alembic"))
        config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
        heads = ScriptDirectory.from_config(config).get_heads()
        assert len(heads) == 1, f"expected one Alembic head, observed {heads}"
        report["cases"]["unique_head"] = heads[0]
        command.upgrade(config, "head")
        report["cases"]["fresh_upgrade"] = True
        command.downgrade(config, args.rollback_target)
        report["cases"]["downgrade_" + args.rollback_target] = True
        command.upgrade(config, "head")
        report["cases"]["reupgrade_head"] = True
        engine = sa.create_engine(url, poolclass=NullPool, hide_parameters=True)
        with engine.connect() as connection:
            actual = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            assert actual == heads[0]
            for table in ("v2_integrated_assets", "v2_integrated_mutations", "v2_unified_query_snapshots", "v2_unified_query_rows"):
                assert sa.inspect(connection).has_table(table), table
        if args.facts:
            measure_facts(engine, args.rows, report)
        report["status"] = "passed_service_probe_not_full_e2e"
    except Exception as error:
        report["status"] = "failed"
        report["error_class"] = type(error).__name__
        # SQLAlchemy exception strings may expose SQL params. Keep report safe;
        # the terminal receives a traceback for diagnosis in the isolated probe.
        import traceback
        traceback.print_exc()
    finally:
        if engine is not None:
            engine.dispose()
        configured_database = sys.modules.get("app.shared.database")
        if configured_database is not None:
            configured_database.engine.dispose()
        if created:
            with admin.connect() as connection:
                observed = connection.execute(sa.text("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=:name"), {"name": name}).scalar_one()
                if observed != marker:
                    raise RuntimeError("database ownership marker changed; refuse cleanup")
                connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
            report["database_cleaned"] = True
        admin.dispose()
        report["wall_seconds"] = round(time.monotonic()-started, 3)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed_service_probe_not_full_e2e" else 1


def measure_facts(engine, rows: int, report: dict) -> None:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from app.auth.models import User
    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.formal_modeling.facts import property_fact_batch, record_object_presence, record_property_facts
    from app.ontologies.formal_modeling.models import PropertyFact
    from integrated_stack import COLUMNS

    uid, oid, release, version, run_id, type_id = [str(uuid.uuid4()) for _ in range(6)]
    with Session(engine) as db:
        db.add(User(id=uid, username="capacity-probe", email="capacity-probe@example.invalid", password_hash="not-used", role="admin"))
        db.flush()
        db.add(OntologyProject(id=oid, name="Fact Capacity Probe", domain="synthetic", created_by=uid))
        db.commit()

    def stats(connection):
        return dict(connection.execute(sa.text("""SELECT
            pg_relation_size('fo_property_facts') AS heap_bytes,
            pg_table_size('fo_property_facts') AS table_bytes,
            pg_indexes_size('fo_property_facts') AS index_bytes,
            pg_total_relation_size('fo_property_facts') AS total_bytes,
            pg_current_wal_insert_lsn()::text AS wal_lsn,
            (SELECT coalesce(sum(size),0) FROM pg_ls_waldir()) AS wal_directory_bytes
        """)).mappings().one())

    with engine.connect() as connection:
        before = stats(connection)
        report["fact_indexes"] = sa.inspect(connection).get_indexes("fo_property_facts")
    peak_wal_directory_bytes = int(before["wal_directory_bytes"])
    started = time.monotonic()
    batches = []
    for offset in range(0, rows, 200):
        stop = min(rows, offset+200)
        instance_ids = [str(uuid.uuid5(uuid.UUID(oid), str(i))) for i in range(offset+1, stop+1)]
        with Session(engine) as db:
            with property_fact_batch(db, ontology_id=oid, instance_ids=instance_ids):
                for i, instance_id in zip(range(offset+1, stop+1), instance_ids):
                    values = [i, 9007199254740992+i, i % 100000, i % 2 == 0,
                              "2026-01-01T00:00:00", "postgres", None if i % 11 == 0 else f"合成-{i:010d}",
                              *[f"v{j:02d}-{i % 1000000:06d}" for j in range(1,41)]]
                    props = dict(zip(COLUMNS, values))
                    assert len(props) == 47
                    options = dict(ontology_id=oid, instance_id=instance_id, object_type_id=type_id,
                                   source="pipeline", actor_id=uid, caused_by=run_id,
                                   ontology_version="v-capacity", ontology_release_id=release,
                                   source_dataset_version_id=version)
                    assert record_object_presence(db, **options) is not None
                    assert len(record_property_facts(db, old_props=None, new_props=props, **options)) == 47
            db.commit()
        with engine.connect() as connection:
            point = stats(connection)
            peak_wal_directory_bytes = max(peak_wal_directory_bytes, int(point["wal_directory_bytes"]))
            batches.append({"rows": stop, "relation_bytes": int(point["total_bytes"]), "wal_directory_bytes": int(point["wal_directory_bytes"])})
    with engine.connect() as connection:
        after = stats(connection)
        wal_bytes = int(connection.execute(sa.text("SELECT pg_wal_lsn_diff(:end, :start)"), {"end": after["wal_lsn"], "start": before["wal_lsn"]}).scalar_one())
        observed = connection.execute(sa.text("""SELECT count(*) AS facts,
            count(DISTINCT instance_id) AS objects,
            count(*) FILTER (WHERE kind='property') AS properties,
            count(*) FILTER (WHERE kind='object') AS presences,
            count(*) FILTER (WHERE ontology_release_id IS DISTINCT FROM :release OR source_dataset_version_id IS DISTINCT FROM :version OR actor_id IS DISTINCT FROM :actor OR caused_by IS DISTINCT FROM :run_id OR seq<>1) AS invalid_lineage
            FROM fo_property_facts WHERE ontology_id=:ontology"""), {"release": release, "version": version, "actor": uid, "run_id": run_id, "ontology": oid}).mappings().one()
        assert dict(observed) == {"facts": rows*48, "objects": rows, "properties": rows*47, "presences": rows, "invalid_lineage": 0}
        assert connection.execute(sa.text("SELECT count(*) FROM (SELECT instance_id FROM fo_property_facts WHERE ontology_id=:ontology GROUP BY instance_id HAVING count(*)<>48) invalid"), {"ontology": oid}).scalar_one() == 0
    delta = {key: int(after[key])-int(before[key]) for key in ("heap_bytes", "table_bytes", "index_bytes", "total_bytes")}
    per_fact = {key: round(value/(rows*48), 3) for key, value in delta.items()}
    free = shutil.disk_usage(ROOT).free
    projections = []
    for scale in (2_300_000, 10_000_000):
        estimated = int(delta["total_bytes"]*scale/rows)
        projections.append({"objects_per_source": scale, "facts_per_source": scale*48,
                            "estimated_fact_relation_bytes_per_source": estimated,
                            "estimated_fact_relation_bytes_two_sources": estimated*2,
                            "fact_relation_alone_fits_host_two_sources": estimated*2 < free,
                            "is_linear_estimate_not_scale_evidence": True})
    report["fact_capacity"] = {"before": {k: int(v) if k != "wal_lsn" else v for k,v in before.items()},
                               "after": {k: int(v) if k != "wal_lsn" else v for k,v in after.items()},
                               "delta_bytes": delta, "bytes_per_fact": per_fact,
                               "observed": dict(observed), "wal_generated_bytes": wal_bytes,
                               "sampled_peak_wal_directory_bytes": peak_wal_directory_bytes,
                               "peak_sample_note": "WAL directory sampled after each 200-object commit; not a continuous peak and includes cluster background WAL",
                               "wall_seconds": round(time.monotonic()-started, 3), "host_free_bytes_after": free,
                               "batches": batches, "projections": projections,
                               "excludes": ["WAL retention", "trial copy", "entity projection", "lake", "Neo4j", "index/build temporary space"],
                               "warning": "Linear small-probe estimate only; not large-scale readiness, not a real promoted release"}
    report["cases"]["all_47_properties_and_presence"] = True


if __name__ == "__main__":
    raise SystemExit(main())
