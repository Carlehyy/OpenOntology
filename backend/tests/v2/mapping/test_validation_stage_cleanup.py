"""Validation TEMP DDL must share the caller's physical SQL transaction."""
from contextlib import nullcontext

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.ontologies.versions.model_validator import _validation_keys
from app.ontologies.mappings.source_staging import MappingStage, managed_stage
from app.ontologies.mappings.formal_projection_staging import ProjectionStage
from app.ontologies.mappings.sql_link_materialization import EdgeRequestStage, RelationStage


@pytest.mark.parametrize("prior_dml", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_validation_stage_is_not_resurrected_by_outer_rollback(prior_dml, fail):
    _assert_stage_cleanup(_validation_keys, prior_dml, fail)


@pytest.mark.parametrize("kind", ["mapping", "projection", "relation", "edge_requests"])
@pytest.mark.parametrize("prior_dml", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_mapping_stage_is_not_resurrected_by_outer_rollback(kind, prior_dml, fail):
    def factory(db):
        constructors = {
            "mapping": lambda: MappingStage(db, ["key"]),
            "projection": lambda: ProjectionStage(db),
            "relation": lambda: RelationStage(db),
            "edge_requests": lambda: EdgeRequestStage(db),
        }
        return managed_stage(constructors[kind]())
    _assert_stage_cleanup(factory, prior_dml, fail)


def _assert_stage_cleanup(factory, prior_dml, fail):
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE unrelated_business (id INTEGER PRIMARY KEY)")
        with Session(engine) as db:
            connection = db.connection()
            if prior_dml:
                db.execute(text("INSERT INTO unrelated_business VALUES (1)"))
            assert connection.connection.driver_connection.in_transaction is prior_dml
            failure = ValueError("original validation failure")
            expected = pytest.raises(ValueError, match="original validation failure") if fail else nullcontext()
            with expected as caught:
                with factory(db) as stage:
                    table = stage if hasattr(stage, "insert") else stage.table
                    assert connection.connection.driver_connection.in_transaction
                    values = {
                        "kind": "object", "id": "object-1", "ordinal": 1, "entity_id": "entity-1",
                        "instance_id": "instance-1", "entity_class": "Item", "object_type_id": "type-1",
                        "source_entity": "source-1", "target_entity": "target-1", "properties": {},
                    }
                    db.execute(table.insert(), [{key: value for key, value in values.items() if key in table.c}])
                    db.execute(text("INSERT INTO unrelated_business VALUES (2)"))
                    if fail:
                        raise failure
            if fail:
                assert caught.value is failure
            def temp_count():
                return db.execute(text("SELECT count(*) FROM sqlite_temp_master WHERE name = :name"),
                                  {"name": table.name}).scalar_one()
            assert temp_count() == 0
            # Stage cleanup never commits or rolls back its caller's work.
            assert db.execute(text("SELECT count(*) FROM unrelated_business")).scalar_one() == (2 if prior_dml else 1)
            db.rollback()
            assert temp_count() == 0
            assert db.execute(text("SELECT count(*) FROM unrelated_business")).scalar_one() == 0
    finally:
        engine.dispose()
