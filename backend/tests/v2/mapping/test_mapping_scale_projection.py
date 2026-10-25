"""Bounded projection regression tests; not a substitute for real scale E2E."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import event

from app.ontologies.entities.models import Entity
from app.ontologies.formal_modeling.models import (
    LinkInstance, LinkType, ObjectInstance, ObjectType,
)
from app.ontologies.graph.neo4j_service import Neo4jService
from app.ontologies.mappings import projection_rebuild
from app.ontologies.projects.models import OntologyProject


class BoundedProjection:
    available = True

    def __init__(self):
        self.node_batches = []
        self.edge_batches = []
        self.last_node = None
        self.last_edge = None
        self.deleted = []
        self.closed = False

    def delete_by_ontology(self, ontology_id):
        self.deleted.append(ontology_id)

    def batch_upsert_entities(self, label, rows, key_field):
        assert label == "OntologyEntity"
        assert key_field == "projection_key"
        assert len(rows) <= projection_rebuild.PROJECTION_BATCH_SIZE
        self.node_batches.append(len(rows))
        self.last_node = rows[-1]
        return len(rows)

    def batch_upsert_relations(self, source, target, kind, rows, key_field):
        assert source == target == "OntologyEntity"
        assert key_field == "projection_key"
        assert len(rows) <= projection_rebuild.PROJECTION_BATCH_SIZE
        self.edge_batches.append(len(rows))
        self.last_edge = rows[-1]
        return len(rows)

    def close(self):
        self.closed = True


def _seed(db, admin_user, count):
    project = OntologyProject(id="bounded-projection", name="Bounded", domain="test",
                              created_by=admin_user.id, status="draft")
    object_type = ObjectType(id="bounded-type", ontology_id=project.id,
                             name="Object", display_name="Object", properties=[])
    link_type = LinkType(id="bounded-link-type", ontology_id=project.id,
                         name="LINK", display_name="Link",
                         source_object_type_id=object_type.id,
                         target_object_type_id=object_type.id)
    db.add_all([project, object_type, link_type])
    db.flush()
    entities = []
    instances = []
    links = []
    for index in range(count):
        entity_id = f"entity-{index:06d}"
        instance_id = f"instance-{index:06d}"
        entities.append({"id": entity_id, "ontology_id": project.id,
                         "name_cn": str(index), "type": "Object",
                         "properties": {"legacy": index}})
        instances.append({"id": instance_id, "ontology_id": project.id,
                          "object_type_id": object_type.id,
                          "external_id": entity_id, "properties": {"value": index},
                          "computed": {}, "source": "pipeline"})
        links.append({"id": f"link-{index:06d}", "ontology_id": project.id,
                      "link_type_id": link_type.id,
                      "source_object_id": "instance-000000",
                      "target_object_id": instance_id, "properties": {"value": index}})
    db.execute(Entity.__table__.insert(), entities)
    db.execute(ObjectInstance.__table__.insert(), instances)
    db.execute(LinkInstance.__table__.insert(), links)
    db.commit()
    return project.id


def test_projection_pages_every_node_and_edge_without_orm_materialization(
    db, admin_user, monkeypatch,
):
    count = projection_rebuild.PROJECTION_BATCH_SIZE * 2 + 3
    ontology_id = _seed(db, admin_user, count)
    neo = BoundedProjection()
    monkeypatch.setattr("app.ontologies.graph.neo4j_service.Neo4jService", lambda: neo)

    # Reject any unbounded outer SELECT, including any future ORM .all().
    # Nested joins/aggregates may of course inspect the complete SQL relation.
    def bounded_select(connection, clause, multiparams, params, options):
        if getattr(clause, "is_select", False):
            assert clause._limit_clause is not None

    event.listen(db.get_bind(), "before_execute", bounded_select)
    try:
        assert projection_rebuild.rebuild_neo4j_projection(db, ontology_id)
    finally:
        event.remove(db.get_bind(), "before_execute", bounded_select)
    assert neo.node_batches == [500, 500, 3]
    assert neo.edge_batches == [500, 500, 3]
    assert sum(neo.node_batches) == sum(neo.edge_batches) == count
    assert neo.last_node["formal_instance_id"] == "instance-001002"
    assert neo.last_node["value"] == count - 1
    assert neo.last_edge["tgt_key"] == ontology_id + "::entity-001002"
    assert neo.last_edge["props"]["value"] == count - 1
    assert neo.closed


def test_projection_rejects_duplicate_mirror_owners_across_pages(
    db, admin_user, monkeypatch,
):
    ontology_id = _seed(db, admin_user, 1)
    db.add(ObjectInstance(id="zz-last-owner", ontology_id=ontology_id,
                          object_type_id="bounded-type", external_id="entity-000000",
                          properties={}, computed={}, source="manual"))
    db.commit()
    neo = BoundedProjection()
    monkeypatch.setattr("app.ontologies.graph.neo4j_service.Neo4jService", lambda: neo)
    assert not projection_rebuild.rebuild_neo4j_projection(db, ontology_id)
    assert not neo.deleted  # Validate SQL truth before destroying the old graph.
    assert neo.closed


def test_projection_rejects_missing_formal_endpoint(db, admin_user, monkeypatch):
    ontology_id = _seed(db, admin_user, 1)
    db.query(LinkInstance).filter_by(ontology_id=ontology_id).update({
        "target_object_id": "nonexistent",
    })
    db.commit()
    neo = BoundedProjection()
    monkeypatch.setattr("app.ontologies.graph.neo4j_service.Neo4jService", lambda: neo)
    assert not projection_rebuild.rebuild_neo4j_projection(db, ontology_id)
    assert not neo.deleted
    assert neo.closed


def _service():
    service = Neo4jService.__new__(Neo4jService)
    service._available = True
    service._driver = MagicMock()
    session = service._driver.session.return_value.__enter__.return_value
    return service, session


def test_bulk_relations_count_actual_matches_and_encode_values():
    service, session = _service()
    session.run.return_value.single.return_value = {"written": 999}
    relations = [{"src_key": "a", "tgt_key": "b",
                  "props": {"id": str(index), "nested": {"k": index}}}
                 for index in range(1001)]
    # Independent bounded transactions, and no optimistic len(input) result.
    assert service.batch_upsert_relations(
        "OntologyEntity", "OntologyEntity", "LINK", relations,
        key_field="projection_key",
    ) == 1998
    assert session.run.call_count == 2
    assert len(session.run.call_args_list[0].kwargs["batch"]) == 1000
    assert len(session.run.call_args_list[1].kwargs["batch"]) == 1
    query = session.run.call_args.args[0]
    assert "UNWIND $batch" in query
    assert "RETURN count(r) AS written" in query
    assert isinstance(session.run.call_args.kwargs["batch"][0]["props"]["nested"], str)


@pytest.mark.parametrize("written", [0, 1])
def test_bulk_nodes_use_actual_write_count(written):
    service, session = _service()
    session.run.return_value.single.return_value = {"written": written}
    assert service.batch_upsert_entities("OntologyEntity", [{"id": "a"}]) == written
    assert "RETURN count(n) AS written" in session.run.call_args.args[0]


def test_graph_deletion_bounds_edges_and_nodes_separately():
    service, session = _service()
    session.run.return_value.single.side_effect = [
        {"deleted": count} for count in (1000, 7, 0, 1000, 3, 0)
    ]
    assert service.delete_by_ontology("isolated") == 1003
    assert session.run.call_count == 6
    calls = session.run.call_args_list
    assert all(call.kwargs == {"ontology_id": "isolated", "batch_size": 1000}
               for call in calls)
    assert all("LIMIT $batch_size" in call.args[0] for call in calls)
    assert all("DELETE r" in call.args[0] for call in calls[:3])
    assert all("DELETE n" in call.args[0] for call in calls[3:])
    assert all("DETACH" not in call.args[0] for call in calls)


def test_live_formal_projection_visits_every_page_and_keeps_adopted_identity(db, admin_user):
    """Exercise the actual build_all Formal callee, not only a staging helper."""
    from sqlalchemy import delete, inspect
    from app.ontologies.mappings.formal_projection import project_to_formal_ontology
    from app.ontologies.relations.models import Relation

    count = 503
    project = OntologyProject(id="formal-paged", name="Formal paged", domain="test",
                              created_by=admin_user.id, status="draft")
    owner = ObjectType(id="formal-paged-type", ontology_id=project.id, name="Item", display_name="Item",
        primary_key="pk", properties=[{"id": "pk", "name": "item_id", "type": "string"}])
    db.add_all([project, owner])
    db.flush()
    db.execute(Entity.__table__.insert(), [{"id": f"paged-{index:06}", "ontology_id": project.id,
        "type": "Item", "name_cn": str(index), "properties": {"item_id": f"I{index}", "value": index}}
        for index in range(count)])
    db.execute(Relation.__table__.insert(), [{"id": f"relation-{index:06}", "ontology_id": project.id,
        "source_entity": "paged-000000", "target_entity": f"paged-{index:06}", "type": "HAS_ITEM",
        "properties": {}} for index in range(1, count)])
    # This deliberately noncanonical pre-existing identity must be adopted by PK.
    db.add(ObjectInstance(id="legacy-adopted", ontology_id=project.id, object_type_id=owner.id,
        source="pipeline", external_id=None, properties={"item_id": "I502"}, computed={}))
    db.commit()
    meta = {"m": {"entity_class": "Item", "pk_col": "item_id", "target_object_type_id": owner.id}}
    summary = project_to_formal_ontology(db, project.id, meta)
    assert summary["object_instances"] == count - 1
    assert summary["updated_instances"] == 1
    assert summary["link_instances"] == count - 1
    assert db.query(ObjectInstance).filter_by(ontology_id=project.id).count() == count
    adopted = db.get(ObjectInstance, "legacy-adopted")
    assert adopted.external_id == "paged-000502"
    assert adopted.properties["value"] == 502
    tail = db.query(LinkInstance).filter_by(target_object_id=adopted.id).one()
    assert tail.source_relation_id == "relation-000502"
    assert not [name for name in inspect(db.connection()).get_temp_table_names()
                if name.startswith("formal_")]
    # Replay preserves both IDs and facts; stale Relation cleanup is complete.
    assert project_to_formal_ontology(db, project.id, meta).get("updated_instances", 0) == 0
    db.execute(delete(Relation).where(Relation.ontology_id == project.id, Relation.id > "relation-000500"))
    assert project_to_formal_ontology(db, project.id, meta)["removed_link_instances"] == 2


@pytest.mark.parametrize("violation", ["primary_key", "duplicate_link", "cardinality"])
def test_formal_validation_rejects_cross_page_conflicts(db, admin_user, violation):
    from app.ontologies.mappings.formal_projection_staging import validate_projection
    count = 503
    project = OntologyProject(id="global-contract", name="Global contract", domain="test",
                              created_by=admin_user.id, status="draft")
    owner = ObjectType(id="global-type", ontology_id=project.id, name="Global", display_name="Global",
        primary_key="pk", properties=[{"id": "pk", "name": "key", "type": "string"}])
    link_owner = LinkType(id="global-link", ontology_id=project.id, name="GlobalLink", display_name="GlobalLink",
        source_object_type_id=owner.id, target_object_type_id=owner.id,
        cardinality="many-to-one" if violation == "cardinality" else "many-to-many", properties=[])
    db.add_all([project, owner, link_owner])
    db.flush()
    db.execute(ObjectInstance.__table__.insert(), [{"id": f"global-{index:06}", "ontology_id": project.id,
        "object_type_id": owner.id, "properties": {"key": "K0" if violation == "primary_key" and index == 502 else f"K{index}"},
        "computed": {}, "source": "manual"} for index in range(count)])
    if violation != "primary_key":
        db.execute(LinkInstance.__table__.insert(), [{"id": f"global-edge-{index:06}", "ontology_id": project.id,
            "link_type_id": link_owner.id,
            "source_object_id": "global-000000" if index == 502 else f"global-{index:06}",
            "target_object_id": "global-000001" if index == 502 and violation == "cardinality" else "global-000000",
            "properties": {}} for index in range(count)])
    db.commit()
    code = {"primary_key": "duplicate_primary_key", "duplicate_link": "duplicate_link",
            "cardinality": "cardinality_violation"}[violation]
    with pytest.raises(ValueError, match=code):
        validate_projection(db, project.id)
    # Cleanup must remain true after rollback, not only just after DROP: SQLite
    # legacy DDL otherwise resurrects these tables on a pooled connection.
    from sqlalchemy import inspect
    assert not [name for name in inspect(db.connection()).get_temp_table_names()
                if name.startswith("formal_")]
    db.rollback()
    assert not [name for name in inspect(db.connection()).get_temp_table_names()
                if name.startswith("formal_")]
    assert db.query(ObjectInstance).filter_by(ontology_id=project.id).count() == count


@pytest.mark.parametrize("alternate", [False, True])
def test_draft_build_infers_tail_rows_from_complete_stage(db, admin_user, alternate):
    """Both normal FK and tokenized alternate names beyond row 500 survive."""
    from app.ontologies.mappings.mapping_service import MappingService
    from app.ontologies.mappings.models import OntologyMapping
    from app.ontologies.relations.models import Relation
    from app.data_channel.datasets.models import Dataset, DatasetVersion

    project = OntologyProject(id="fk-complete", name="Complete inference", domain="test",
                              created_by=admin_user.id, status="draft")
    suppliers = Dataset(id="fk-suppliers", name="Suppliers", kind="structured",
                        schema_json={"primary_key": "supplier_id"})
    orders = Dataset(id="fk-orders", name="Orders", kind="structured",
                     schema_json={"primary_key": "order_id"})
    db.add_all([project, suppliers, orders])
    db.flush()
    supplier_mapping = OntologyMapping(id="fk-supplier-mapping", ontology_id=project.id,
        curated_dataset_id=suppliers.id, entity_class="Supplier", field_mapping={"__primary_key__": "supplier_id"}, status="draft")
    order_mapping = OntologyMapping(id="fk-order-mapping", ontology_id=project.id,
        curated_dataset_id=orders.id, entity_class="Order", field_mapping={"__primary_key__": "order_id"}, status="draft")
    versions = {dataset.id: DatasetVersion(dataset_id=dataset.id, version_no=1, rowcount=503)
                for dataset in (suppliers, orders)}
    db.add_all([supplier_mapping, order_mapping, *versions.values()])
    db.commit()
    rows = {suppliers.id: [{"supplier_id": f"S{i:06}", "supplier_name": f"VendorCompany{i:06}"} for i in range(503)],
            orders.id: [{"order_id": f"O{i:06}", **({"organizations": f"VendorCompany{i:06}"}
                 if alternate else {"supplier_id": f"S-{i:06}"})} for i in range(503)]}
    with patch("app.ontologies.mappings.mapping_service.load_mapping_source_rows", side_effect=
               lambda _db, mapping, **kwargs: (rows[mapping.curated_dataset_id], versions[mapping.curated_dataset_id])), \
         patch.object(MappingService, "_rebuild_neo4j_projection", return_value=True):
        result = MappingService(db).build_all(project.id)
    source = MappingService(db)._stable_row_id(order_mapping, rows[orders.id][-1], "order_id")
    target = MappingService(db)._stable_row_id(supplier_mapping, rows[suppliers.id][-1], "supplier_id")
    relation = db.query(Relation).filter_by(ontology_id=project.id, source_entity=source,
                                          target_entity=target, type="HAS_SUPPLIER").one()
    assert relation.confidence == (0.75 if alternate else 0.85)
    assert db.query(Relation).filter_by(ontology_id=project.id, type="HAS_SUPPLIER").count() == 503
    assert result["formal_projection"]["object_instances"] == 1006


def test_fk_full_keys_do_not_index_long_payload_or_accept_hash_only_matches(db):
    from sqlalchemy import select, update
    from app.ontologies.mappings.source_staging import mapping_stage, managed_stage
    from app.ontologies.mappings.sql_fk_inference import ColumnStage, _matched_query
    from app.ontologies.mappings.sql_link_materialization import EndpointStage
    huge = ("Long business value " * 1000).strip()
    with mapping_stage(db, ["key"]) as source, mapping_stage(db, ["key"]) as target:
        source.append([{"key": huge}, {"key": huge + "different"}], ["source-a", "source-b"])
        target.append([{"key": huge}, {"key": "other value"}], ["target-a", "target-b"])
        with managed_stage(ColumnStage(db, source, "key")) as left, \
             managed_stage(ColumnStage(db, target, "key")) as right:
            indexed = {column.name for index in right.table.indexes for column in index.columns}
            assert indexed == {"raw_hash", "norm_hash"}
            # Deliberately collide hashes. Equality must still use the full key.
            for stage in (left, right):
                db.execute(update(stage.table).values(raw_hash="same", norm_hash="same"))
            matches = db.execute(_matched_query(left, right).order_by(left.table.c.ordinal)).all()
            assert [(row.src, row.tgt) for row in matches] == [("source-a", "target-a"), ("source-b", None)]
        with managed_stage(EndpointStage(target, "key")) as endpoint:
            assert not any("join_value" in index.columns for index in endpoint.table.indexes)
            matches = db.execute(select(endpoint.table.c.entity_id).where(endpoint.table.c.join_value == huge)).scalars().all()
            assert matches == ["target-a"]


def test_fk_missing_complete_stage_fails_before_reconciliation(db, admin_user):
    from types import SimpleNamespace
    from app.ontologies.mappings.errors import MappingSourceError
    from app.ontologies.mappings.mapping_service import MappingService
    from app.ontologies.mappings.sql_fk_inference import infer_relations
    from app.ontologies.relations.models import Relation
    ontology_id = _seed(db, admin_user, 2)
    db.add(Relation(id="do-not-delete", ontology_id=ontology_id, source_entity="entity-000000",
                    target_entity="entity-000001", type="HAS_OBJECT", properties={"source": "fk_inference"}))
    db.flush()
    with pytest.raises(MappingSourceError, match="Complete mapping stage required"):
        infer_relations(MappingService(db), ontology_id, [SimpleNamespace(id="sampled")],
                        {"sampled": {"rows": [{"key": "a"}], "row_count": 1}})
    assert db.get(Relation, "do-not-delete") is not None


def test_stage_cleanup_never_masks_original_failure():
    from types import SimpleNamespace
    from app.ontologies.mappings.source_staging import managed_stage
    stage = SimpleNamespace(db=SimpleNamespace(is_active=True), close=MagicMock(side_effect=RuntimeError("cleanup")))
    original = ValueError("original")
    with pytest.raises(ValueError) as caught:
        with managed_stage(stage):
            raise original
    assert caught.value is original
    stage.close.assert_called_once()


@pytest.mark.parametrize("released", [False, True])
def test_materializer_uses_frozen_ordered_pk_without_changing_released_mapping(db, released):
    from types import SimpleNamespace
    from app.ontologies.mappings.batched_materialization import materialize_mapping
    from app.ontologies.mappings.mapping_service import MappingService
    from app.ontologies.mappings.models import OntologyMapping
    from app.ontologies.mappings.source_batches import MappingSourceBatches
    service = MappingService(db)
    mapping = OntologyMapping(id="frozen-object", ontology_id="not-persisted", entity_class="Receipt",
        curated_dataset_id="frozen-source", field_mapping={"tenant": "tenant", "edge_number": "edge_number"})
    original_map = dict(mapping.field_mapping)
    rows = [{"tenant": "A", "edge_number": 9007199254740993}, {"tenant": "B", "edge_number": 9007199254740993}]
    source = MappingSourceBatches(SimpleNamespace(id="pinned-object-version"),
        {"columns": ["tenant", "edge_number"], "primary_key_columns": ["tenant", "edge_number"]}, iter([rows]))
    stages = []
    written = []
    with patch.object(service, "_dataset_primary_key", side_effect=AssertionError("must not read mutable Dataset")), \
         patch.object(service, "_adopt_legacy_projection_ownership"), \
         patch.object(service, "_write_v1_entities", side_effect=lambda _mapping, entities: written.extend(entities)), \
         patch.object(service, "_reconcile_mapping_entities", return_value=0):
        try:
            meta, count, _ = materialize_mapping(service, mapping, source, source.version, released=released, stages=stages)
            assert meta["pk_col"] == "tenant,edge_number"
            assert count == len({row["id"] for row in written}) == 2
            assert written[0]["__composite_identity__"] == service._row_identity_value(rows[0], "tenant,edge_number")
            assert meta["stage"].count == 2
            if released:
                assert mapping.field_mapping == original_map
        finally:
            for stage in stages:
                stage.close()


@pytest.mark.parametrize("edge", [False, True])
def test_explicit_nonunique_endpoint_keeps_all_matches_and_bounds_fanout(db, admin_user, edge):
    from types import SimpleNamespace
    from app.ontologies.mappings.mapping_service import MappingService
    from app.ontologies.mappings.source_batches import MappingSourceBatches
    from app.ontologies.mappings.source_staging import mapping_stage
    from app.ontologies.mappings.sql_link_materialization import RelationStage, process_direct_link, process_edge_link
    from app.ontologies.relations.models import Relation
    ontology_id = _seed(db, admin_user, 504)
    service = MappingService(db)
    link = SimpleNamespace(id="all-targets", ontology_id=ontology_id, relation_type="HAS_OBJECT", src_key="key", tgt_key="key",
                           field_mapping={"amount": "amount"}, edge_dataset_id="edge-data")
    page_sizes = []
    append = RelationStage.append

    def bounded_append(stage, rows):
        page_sizes.append(len(rows))
        return append(stage, rows)

    with mapping_stage(db, ["key"]) as source, mapping_stage(db, ["key"]) as target:
        source.append([{"key": "match"}], ["entity-000000"])
        for offset in range(1, 504, 500):
            target.append([{"key": "match"} for _ in range(offset, min(offset + 500, 504))],
                          [f"entity-{i:06}" for i in range(offset, min(offset + 500, 504))])
        src_meta = {"stage": source, "pk_col": "key", "entity_class": "Source"}
        tgt_meta = {"stage": target, "pk_col": "key", "entity_class": "Target"}
        with patch.object(service, "_record_link_mapping_versions"), patch.object(RelationStage, "append", bounded_append):
            if edge:
                prepared = MappingSourceBatches(SimpleNamespace(id="edge-v1"), {"primary_key_columns": ["edge_id"]},
                    iter([[{"edge_id": 9007199254740993, "key": "match", "amount": None}]]))
                with patch("app.ontologies.mappings.source_batches.prepare_mapping_source", return_value=prepared):
                    result = process_edge_link(service, ontology_id, link, src_meta, tgt_meta)
            else:
                result = process_direct_link(service, ontology_id, link, src_meta, tgt_meta)
        assert result["count"] == 503
        assert result["cardinality"] == "one_to_many"
        assert page_sizes == [500, 3]
        assert db.query(Relation).filter_by(ontology_id=ontology_id, source_entity="entity-000000").count() == 503


def test_raw_json_text_keeps_exact_string_or_explicitly_rejects_structured_semantics():
    from app.data_channel.integrated.contracts import JsonText as exported
    from app.shared.value_types import JsonText
    from app.ontologies.mappings.formal_projection import _coerce_props_to_type
    assert exported is JsonText
    payloads = [JsonText('{"n":1.2345678901234567890123456789}'),
                JsonText('{"$type":"decimal","value":"123.00"}'), JsonText("null"),
                JsonText("9007199254740993.123456789")]
    for payload in payloads:
        assert _coerce_props_to_type({"value": payload}, [{"name": "value", "type": "string"}])["value"] is payload
        for structured in ("object", "array", "number", "integer", "float"):
            with pytest.raises(ValueError, match="raw JSON 请映射 string"):
                _coerce_props_to_type({"value": payload}, [{"name": "value", "type": structured}])
    assert _coerce_props_to_type({"value": None}, [{"name": "value", "type": "object"}]) == {"value": None}
    assert _coerce_props_to_type({"value": '{"legacy":true}'}, [{"name": "value", "type": "object"}]) == {"value": {"legacy": True}}
    assert _coerce_props_to_type({"value": "1.25"}, [{"name": "value", "type": "number"}]) == {"value": 1.25}
    assert _coerce_props_to_type({"value": JsonText("false")}, [{"name": "value", "type": "boolean"}]) == {"value": False}


@pytest.mark.parametrize("property_type,payload_text", [
    ("object", '{"n":1.2345678901234567890123456789,"$type":"decimal"}'),
    ("number", "9007199254740993.123456789"),
])
def test_raw_json_guard_precedes_entity_persistence_and_string_roundtrip_is_exact(db, admin_user, property_type, payload_text):
    from types import SimpleNamespace
    from app.shared.value_types import JsonText
    from app.ontologies.formal_modeling.models import ObjectType, ObjectInstance
    from app.ontologies.mappings.mapping_service import MappingService
    from app.ontologies.mappings.models import OntologyMapping
    from app.ontologies.mappings.batched_materialization import materialize_mapping
    from app.ontologies.mappings.source_batches import MappingSourceBatches
    from app.ontologies.mappings.formal_projection import project_to_formal_ontology
    from app.ontologies.entities.models import Entity
    ontology = OntologyProject(name="Raw JSON guard", domain="test", created_by=admin_user.id)
    db.add(ontology)
    db.flush()
    ontology_id = ontology.id
    target = ObjectType(id="raw-type", ontology_id=ontology_id, name="RawObject", display_name="RawObject",
        primary_key="pk", properties=[{"id": "pk", "name": "id", "type": "string", "required": True},
                                      {"id": "value", "name": "value", "type": property_type}])
    mapping = OntologyMapping(id="raw-map", ontology_id=ontology_id, entity_class="RawObject",
        target_object_type_id=target.id, field_mapping={"id": "id", "value": "value", "__primary_key__": "id"})
    db.add_all([target, mapping])
    db.flush()
    payload = JsonText(payload_text)
    def source():
        return MappingSourceBatches(SimpleNamespace(id="raw-v1"), {"columns": ["id", "value"], "primary_key_columns": ["id"]},
                                    iter([[{"id": "R-1", "value": payload}]]))
    service = MappingService(db)
    with pytest.raises(ValueError, match="raw JSON 请映射 string"):
        materialize_mapping(service, mapping, source(), None, released=True, stages=[])
    assert db.query(Entity).filter_by(ontology_id=ontology_id).count() == 0
    target.properties = [dict(prop, type="string") for prop in target.properties]
    db.flush()
    stages = []
    try:
        meta, count, _ = materialize_mapping(service, mapping, source(), None, released=True, stages=stages)
        assert count == 1
        project_to_formal_ontology(db, ontology_id, {mapping.id: meta})
        stored = db.query(ObjectInstance).filter_by(ontology_id=ontology_id).one()
        assert stored.properties["value"] == payload
        assert stored.properties["id"] == "R-1"
    finally:
        for stage in stages:
            stage.close()


def test_integrated_47_field_version_flows_through_governance_neo4j_sentinel_and_assistant(
        client, auth_headers, ontology, db, admin_user, monkeypatch):
    """Sealed 47-column integrated version -> Draft -> Trial -> Impact -> Promote.

    Focused unit/contract evidence only: not a million-row acceptance, not a real
    Neo4j/server/provider session.  Every mapped field, pin, lineage, permission
    and typed value is asserted against the frozen source rows.
    """
    import uuid
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import pytest
    from fastapi import HTTPException
    from app.auth.models import RoleMenuPermission, User
    from app.data_channel.connections.models import Connection
    from app.data_channel.datasets import integrated_store as store
    from app.data_channel.datasets.models import Dataset
    from app.data_channel.integrated.contracts import digest
    from app.data_channel.integrated.models import IntegratedAsset
    from app.models.ontology import OntologyProject
    from app.models.ontology_formal import LinkInstance, ObjectInstance, PropertyFact
    from app.models.ontology_version import OntologyVersion
    from app.ontologies.agent_runtime.boundary import build_scope
    from app.ontologies.agent_runtime.toolkit import ToolRunner
    from app.exploration.document import canvas_fingerprint
    from app.exploration.reverse_projection import project_snapshot_to_canvas

    column_names = ["record_id", "join_key", "amount_cents", "active",
                    "observed_at", "source_tag", "note"]
    column_names += [f"c{index:02d}" for index in range(1, 41)]
    assert len(column_names) == len(set(column_names)) == 47
    schema = [
        {"name": "id", "type": "integer", "source_type": "BIGINT",
         "nullable": False, "precision": None, "scale": None, "timezone": None,
         "collation": None, "length": None, "unsigned": False},
        {"name": "join_key", "type": "integer", "source_type": "BIGINT",
         "nullable": False, "precision": None, "scale": None, "timezone": None,
         "collation": None, "length": None, "unsigned": False},
        {"name": "amount_cents", "type": "integer", "source_type": "BIGINT",
         "nullable": True, "precision": None, "scale": None, "timezone": None,
         "collation": None, "length": None, "unsigned": False},
        {"name": "active", "type": "boolean", "source_type": "BOOLEAN",
         "nullable": True, "precision": None, "scale": None, "timezone": None,
         "collation": None, "length": None, "unsigned": False},
        {"name": "observed_at", "type": "timestamp",
         "source_type": "TIMESTAMP WITH TIME ZONE", "nullable": True,
         "precision": None, "scale": None, "timezone": True,
         "collation": None, "length": None, "unsigned": False},
        {"name": "source_tag", "type": "string", "source_type": "TEXT",
         "nullable": True, "precision": None, "scale": None, "timezone": None,
         "collation": None, "length": None, "unsigned": False},
        {"name": "note", "type": "string", "source_type": "TEXT",
         "nullable": True, "precision": None, "scale": None, "timezone": None,
         "collation": None, "length": None, "unsigned": False},
    ]
    # 38 text payload columns, one extra integer and one boolean remain 47 total.
    for index in range(1, 39):
        schema.append({"name": f"c{index:02d}", "type": "string",
                       "source_type": "TEXT", "nullable": True, "precision": None,
                       "scale": None, "timezone": None, "collation": None,
                       "length": None, "unsigned": False})
    schema.append({"name": "c39", "type": "integer", "source_type": "BIGINT",
                   "nullable": True, "precision": None, "scale": None,
                   "timezone": None, "collation": None, "length": None,
                   "unsigned": False})
    schema.append({"name": "c40", "type": "boolean", "source_type": "BOOLEAN",
                   "nullable": True, "precision": None, "scale": None,
                   "timezone": None, "collation": None, "length": None,
                   "unsigned": False})
    assert len(schema) == 47
    primary_key = ["id"]
    fingerprint = digest({"columns": schema, "primary_key": primary_key})

    connection = Connection(name="chain-fixture", kind="postgres", config={})
    db.add(connection)
    db.flush()
    dataset = Dataset(name="chain fixture", kind="integrated",
                      source_connection_id=connection.id,
                      source_resource="public.chain",
                      schema_json={"columns": schema, "primary_key": ",".join(primary_key)})
    db.add(dataset)
    db.flush()
    asset = IntegratedAsset(
        dataset_id=dataset.id, connection_id=connection.id, name="chain records",
        schema_name="public", table_name="chain", schema_json=dataset.schema_json,
        schema_fingerprint=fingerprint, writable=False, created_by=admin_user.id,
    )
    db.add(asset)
    db.commit()

    observed = datetime(2026, 10, 24, 8, 0, tzinfo=timezone.utc)
    def base_row(identifier, join_key, amount, active, tag, note, c39, c40):
        row = {"id": identifier, "join_key": join_key, "amount_cents": amount,
               "active": active, "observed_at": observed, "source_tag": tag,
               "note": note}
        for index in range(1, 39):
            row[f"c{index:02d}"] = f"{tag}-{index:02d}"
        row["c39"] = c39
        row["c40"] = c40
        return row

    rows = [
        base_row(1, 9007199254740993, 1234567890123, True, "alpha", None, 7, True),
        base_row(2, 9007199254740994, -5, False, "beta", "宽字符 ünïcode", -9, False),
        base_row(3, 9007199254740995, 0, True, "gamma", "n", 0, True),
    ]
    rows[1]["c01"] = ""  # empty text must stay distinct from SQL NULL
    run, gate = store.begin_snapshot(db, {
        "asset_id": asset.id, "dataset_id": dataset.id,
        "connection_id": connection.id, "resource": "public.chain",
        "schema": schema, "primary_key": primary_key,
        "schema_fingerprint": fingerprint, "expected_rows": len(rows),
        "snapshot": {"id": str(uuid.uuid4()), "consistent": True,
                     "kind": "repeatable_read",
                     "started_at": "2026-10-24T08:00:00+00:00"},
    })
    hasher = store.snapshot_hasher(run)
    store.write_snapshot_batch(db, run, gate, rows, hasher)
    version = store.seal_snapshot(db, run, {
        "complete": True, "row_count": len(rows),
        "schema_fingerprint": fingerprint, "snapshot_id": run.snapshot_json["id"],
    }, hasher.hexdigest())

    property_types = {
        "record_id": "number", "join_key": "number", "amount_cents": "number",
        "active": "boolean", "observed_at": "datetime", "source_tag": "string",
        "note": "string", **{f"c{index:02d}": "string" for index in range(1, 39)},
        "c39": "number", "c40": "boolean",
    }
    assert set(property_types) == set(column_names)
    properties = [
        {"id": f"prop-{name}", "name": name, "type": property_types[name],
         "required": name == "record_id"}
        for name in column_names
    ]
    field_mapping = {("id" if name == "record_id" else name): name
                     for name in column_names}
    field_mapping["__primary_key__"] = "id"

    tree = client.get(f"/api/v2/ontologies/{ontology['id']}/version-tree",
                      headers=auth_headers).json()["data"]
    root = next(item for item in tree["versions"] if item["version_number"] == "v0")
    created = client.post(
        f"/api/v2/ontologies/{ontology['id']}/versions/{root['id']}/drafts",
        headers=auth_headers, json={"versionLabel": "47 字段全链路"})
    assert created.status_code == 201, created.text
    draft = created.json()["data"]
    saved = client.put(
        f"/api/v2/ontologies/{ontology['id']}/versions/{draft['id']}/workspace",
        headers=auth_headers, json={
            "baseRevision": f"{draft['revision']}:{draft['snapshot_hash']}",
            "version": draft["version_number"],
            "objectTypes": [{
                "id": "ot-record", "name": "Record", "displayName": "记录",
                "primaryKey": "prop-record_id", "properties": properties,
            }],
            "linkTypes": [{
                "id": "link-self", "name": "related", "displayName": "相关",
                "sourceObjectTypeId": "ot-record",
                "targetObjectTypeId": "ot-record",
                "cardinality": "one-to-one", "properties": [],
            }],
            "actions": [], "functions": [], "instances": [], "linkInstances": [],
        })
    assert saved.status_code == 200, saved.text
    mapped = client.put(
        f"/api/v2/ontologies/{ontology['id']}/versions/{draft['id']}/workspace/mappings",
        headers=auth_headers, json={
            "baseRevision": saved.json()["data"]["revision"],
            "mappings": [{
                "id": "map-record", "curatedDatasetId": dataset.id,
                "entityClass": "Record", "targetObjectTypeId": "ot-record",
                "fieldMapping": field_mapping,
            }],
            "linkMappings": [{
                "id": "map-self", "linkTypeId": "link-self", "relationType": "related",
                "srcDatasetId": dataset.id, "tgtDatasetId": dataset.id,
                "edgeDatasetId": None, "srcKey": "join_key", "tgtKey": "join_key",
                "fieldMapping": {},
            }],
            "sentinels": [{
                "id": "sentinel-record-watch", "name": "record_watch",
                "displayName": "47 字段监控", "description": "",
                "bindings": [{"alias": "record", "objectTypeId": "ot-record",
                              "filter": None}],
                "links": [], "condition": "", "conditionRows": [],
                "conditionLogic": "and", "primaryAlias": "record",
                "actionIds": [], "actionParameters": {}, "onChange": True,
                "onSchedule": False, "scanIntervalSeconds": 300,
                "triggerMode": "on_enter", "muted": False, "enabled": True,
                "status": "draft",
            }],
        })
    assert mapped.status_code == 200, mapped.text

    # The trial semantic gate needs a self-consistent minimal semantic layer.
    version_row = db.query(OntologyVersion).filter_by(id=draft["id"]).one()
    canvas = project_snapshot_to_canvas(version_row.snapshot_formal)
    document = "# 47 字段全链路回归\n\n确定性语义层。\n"
    version_row.snapshot_semantic = {
        "canvas": canvas, "canvasFingerprint": canvas_fingerprint(canvas),
        "documentMd": document, "documentTitle": "47 字段全链路回归",
        "documentFingerprint": __import__("hashlib").sha256(
            document.encode("utf-8")).hexdigest(), "semanticRevision": 1,
    }
    db.commit()

    trial_response = client.post(
        f"/api/v2/ontologies/{ontology['id']}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={})
    assert trial_response.status_code == 201, trial_response.text
    trial = trial_response.json()["data"]
    assert trial["status"] == "passed", trial["result"]
    assert {pin["versionId"] for pin in trial["dataset_versions"]} == {version.id}
    assert trial["result"]["counts"]["objects"] == len(rows)
    assert trial["result"]["counts"]["links"] == len(rows)
    sentinel_result = trial["result"]["sentinels"][0]
    assert sentinel_result["id"] == "sentinel-record-watch"
    assert sentinel_result["activation"] == "active"
    assert sentinel_result["candidateCount"] == sentinel_result["matched"] == len(rows)
    assert sentinel_result["candidateCapReached"] is False
    assert sentinel_result["errors"] == []

    impact = client.get(
        f"/api/v2/ontologies/{ontology['id']}/versions/{draft['id']}/impact",
        headers=auth_headers).json()["data"]
    promoted = client.post(
        f"/api/v2/ontologies/{ontology['id']}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={"trialRunId": trial["id"], "impactHash": impact["impactHash"]})
    assert promoted.status_code == 201, promoted.text
    release_id = promoted.json()["data"]["id"]
    db.expire_all()
    project = db.get(OntologyProject, ontology["id"])
    assert project.current_release_id == release_id
    assert project.projection_status == "ready"

    expected = {}
    for row in rows:
        values = {
            "record_id": row["id"], "join_key": row["join_key"],
            "amount_cents": row["amount_cents"], "active": row["active"],
            "observed_at": observed.isoformat(), "source_tag": row["source_tag"],
            "note": row["note"], "c39": row["c39"], "c40": row["c40"],
            **{f"c{index:02d}": row[f"c{index:02d}"] for index in range(1, 39)},
        }
        expected[row["id"]] = values
    instances = db.query(ObjectInstance).filter_by(
        ontology_id=ontology["id"], ontology_release_id=release_id).all()
    assert len(instances) == len(rows)
    live_by_key = {item.properties["record_id"]: item for item in instances}
    assert set(live_by_key) == set(expected)
    for key, values in expected.items():
        assert live_by_key[key].properties == values
    facts = db.query(PropertyFact).filter_by(
        ontology_id=ontology["id"], kind="property").all()
    assert facts, "live mapping must record property facts"
    assert {fact.source_dataset_version_id for fact in facts} == {version.id}
    assert {fact.ontology_release_id for fact in facts} == {release_id}

    links = db.query(LinkInstance).filter_by(
        ontology_id=ontology["id"], ontology_release_id=release_id).all()
    assert len(links) == len(rows)
    link_facts = db.query(PropertyFact).filter_by(
        ontology_id=ontology["id"], kind="link").all()
    assert link_facts and {fact.source_dataset_version_id for fact in link_facts} == {version.id}
    assert {fact.ontology_release_id for fact in link_facts} == {release_id}

    class FakeNeo4j:
        available = True
        def __init__(self):
            self.nodes, self.edges, self.deleted, self.closed = [], [], [], False
        def delete_by_ontology(self, ontology_id):
            self.deleted.append(ontology_id)
        def batch_upsert_entities(self, label, batch, key_field):
            assert label == "OntologyEntity" and key_field == "projection_key"
            self.nodes.extend(batch)
            return len(batch)
        def batch_upsert_relations(self, source, target, kind, batch, key_field):
            self.edges.extend(batch)
            return len(batch)
        def close(self):
            self.closed = True

    from app.ontologies.mappings import projection_rebuild
    neo = FakeNeo4j()
    monkeypatch.setattr(
        "app.ontologies.graph.neo4j_service.Neo4jService", lambda: neo)
    assert projection_rebuild.rebuild_neo4j_projection(db, ontology["id"]) is True
    assert neo.closed and len(neo.nodes) == len(rows) and len(neo.edges) == len(rows)
    node_by_key = {node["formal_instance_id"]: node for node in neo.nodes}
    for instance in instances:
        node = node_by_key[str(instance.id)]
        assert node["projection_key"] == f"{ontology['id']}::{instance.id}"
        assert {name: node[name] for name in column_names} == expected[instance.properties["record_id"]]
    first_ids = {node["id"] for node in neo.nodes}

    # A repeated replay must keep stable IDs and the same 47 mapped values.
    neo_replay = FakeNeo4j()
    monkeypatch.setattr(
        "app.ontologies.graph.neo4j_service.Neo4jService", lambda: neo_replay)
    assert projection_rebuild.rebuild_neo4j_projection(db, ontology["id"]) is True
    assert {node["id"] for node in neo_replay.nodes} == first_ids

    project, profile, scope = build_scope(
        db, ontology["id"], release_id=release_id)
    runner = ToolRunner(db, scope)
    aggregate = runner.run("aggregate_objects", {
        "object_type": "Record", "metric": "count"})
    assert aggregate["value"] == len(rows)
    assert aggregate["scanned"] == aggregate["matched"] == len(rows)
    assert aggregate["partial"] is False and aggregate["exact"] is True
    assert aggregate["completeness"] == "complete"
    search = runner.run("search_objects", {
        "object_type": "Record", "keyword": "宽"})
    assert search["total"] == search["returned"] == 1 and search["exact"] is True
    beta = runner.run("search_objects", {"object_type": "Record", "keyword": "beta"})
    assert beta["total"] == 1
    beta_properties = beta["items"][0]["properties"]
    assert beta_properties["note"] == "宽字符 ünïcode"
    # >2^53 keys are transported as lossless strings, never floats.
    assert beta_properties["join_key"] == "9007199254740994"
    assert beta_properties["c01"] == ""
    assert beta_properties["note"] != ""  # empty text and NULL never collapse

    # Permission is checked before scope/profile exposure, and returns a 403.
    from app.ontologies.agent_query.read_port import resolve_ontology_relation
    custom = User(id=str(uuid.uuid4()), username="chain-custom",
                  email="chain-custom@test.com", password_hash="x",
                  role="custom", is_active=True)
    db.add(custom)
    db.add(RoleMenuPermission(role="chain-custom-role",
                              menu_keys=["data.structured"]))
    custom.role = "chain-custom-role"
    db.commit()
    binding = SimpleNamespace(ontology_id=ontology["id"], release_id=release_id,
                              object_type_id="ot-record")
    with pytest.raises(HTTPException) as denied:
        resolve_ontology_relation(
            db, custom, "records", binding, None,
            column_spec_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            relation_factory=lambda **kwargs: SimpleNamespace(**kwargs))
    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "MENU_ACCESS_DENIED"
