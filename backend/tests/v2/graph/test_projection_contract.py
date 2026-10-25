from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.models.entity import Entity
from app.models.ontology import OntologyProject
from app.models.ontology_formal import (
    LinkInstance,
    LinkType,
    ObjectInstance,
    ObjectType,
)
from app.models.relation import Relation
from app.ontologies.mappings.mapping_service import MappingService
from app.ontologies.projection_state import (
    ProjectionRebuildError,
    mark_projecting,
    repair_unready_projections,
    rebuild_after_commit,
)


class RecordingNeo4j:
    available = True

    def __init__(self, *, node_delta: int = 0, relation_result: bool = True):
        self.node_delta = node_delta
        self.relation_result = relation_result
        self.deleted: list[str] = []
        self.nodes: list[dict] = []
        self.relations: list[dict] = []
        self.closed = False

    def delete_by_ontology(self, ontology_id: str) -> int:
        self.deleted.append(ontology_id)
        return 0

    def batch_upsert_entities(self, label, rows, key_field="id") -> int:
        assert label == "OntologyEntity"
        assert key_field == "projection_key"
        self.nodes.extend(rows)
        return len(rows) + self.node_delta

    def upsert_relation(
        self,
        src_label,
        src_key,
        tgt_label,
        tgt_key,
        rel_type,
        props=None,
        key_field="id",
    ) -> bool:
        self.relations.append({
            "src_label": src_label,
            "src": src_key,
            "tgt_label": tgt_label,
            "tgt": tgt_key,
            "type": rel_type,
            "props": props or {},
            "key_field": key_field,
        })
        return self.relation_result

    def batch_upsert_relations(
        self, src_label, tgt_label, rel_type, rows, key_field="id",
    ) -> int:
        for row in rows:
            self.upsert_relation(
                src_label, row["src_key"], tgt_label, row["tgt_key"],
                rel_type, props=row["props"], key_field=key_field,
            )
        return len(rows) if self.relation_result else 0

    def close(self) -> None:
        self.closed = True


def _seed_projection_truth(db, admin_user) -> OntologyProject:
    project = OntologyProject(
        id="projection-contract",
        name="Projection contract",
        domain="test",
        created_by=admin_user.id,
        status="draft",
    )
    supplier = ObjectType(
        id="ot-supplier",
        ontology_id=project.id,
        name="Supplier",
        display_name="供应商",
        properties=[],
    )
    order = ObjectType(
        id="ot-order",
        ontology_id=project.id,
        name="Order",
        display_name="订单",
        properties=[],
    )
    owns = LinkType(
        id="lt-owns",
        ontology_id=project.id,
        name="OWNS",
        display_name="拥有",
        source_object_type_id=supplier.id,
        target_object_type_id=order.id,
        properties=[],
    )
    db.add_all([
        project,
        supplier,
        order,
        owns,
        Entity(
            id="entity-supplier",
            ontology_id=project.id,
            name_cn="供应商甲",
            type="Supplier",
            properties={
                "legacy": True,
                "__business_properties__": {
                    "id": "SUPPLIER-001",
                    "name": "业务供应商甲",
                },
            },
        ),
        Entity(
            id="entity-order",
            ontology_id=project.id,
            name_cn="订单一",
            type="Order",
            properties={},
        ),
        ObjectInstance(
            id="formal-supplier",
            ontology_id=project.id,
            object_type_id=supplier.id,
            external_id="entity-supplier",
            properties={"name": "供应商甲", "tier": "A"},
            computed={},
            source="pipeline",
        ),
        ObjectInstance(
            id="formal-only-order",
            ontology_id=project.id,
            object_type_id=order.id,
            # An arbitrary business key is not a graph identity. The same key
            # may legitimately exist in another ontology.
            external_id="shared-business-order-key",
            properties={
                "id": "ORDER-001",
                "name": "手工订单",
                "ontology_id": "business-order-scope",
                "updated_at": "2026-07-01T02:03:04Z",
            },
            computed={"risk": "low"},
            source="manual",
        ),
        Relation(
            id="relation-legacy",
            ontology_id=project.id,
            source_entity="entity-supplier",
            target_entity="entity-order",
            type="SUPPLIES",
            properties={
                "id": "SUPPLIES-001",
                "updated_at": "2026-07-02T02:03:04Z",
            },
        ),
        LinkInstance(
            id="formal-link",
            ontology_id=project.id,
            link_type_id=owns.id,
            source_object_id="formal-supplier",
            target_object_id="formal-only-order",
            properties={
                "id": "OWNS-001",
                "since": 2024,
                "updated_at": "2026-07-03T02:03:04Z",
            },
        ),
    ])
    db.commit()
    return project


def test_rebuild_projects_legacy_and_formal_truth_with_stable_ids(
    db,
    admin_user,
    monkeypatch,
):
    project = _seed_projection_truth(db, admin_user)
    neo = RecordingNeo4j()
    monkeypatch.setattr(
        "app.services.v2.graph.neo4j_service.Neo4jService",
        lambda: neo,
    )

    assert MappingService(db)._rebuild_neo4j_projection(project.id) is True

    by_id = {row["id"]: row for row in neo.nodes}
    assert set(by_id) == {
        "entity-supplier",
        "entity-order",
        "formal-only-order",
    }
    assert by_id["entity-supplier"]["formal_instance_id"] == "formal-supplier"
    assert by_id["entity-supplier"]["business_id"] == "SUPPLIER-001"
    assert by_id["entity-supplier"]["business_name"] == "供应商甲"
    assert by_id["entity-supplier"]["__business_properties_json__"][
        "name"
    ] == "供应商甲"
    assert by_id["formal-only-order"]["risk"] == "low"
    assert by_id["formal-only-order"]["business_id"] == "ORDER-001"
    assert by_id["formal-only-order"]["business_ontology_id"] == (
        "business-order-scope"
    )
    assert by_id["formal-only-order"]["business_updated_at"] == (
        "2026-07-01T02:03:04Z"
    )
    assert by_id["formal-only-order"]["__business_properties_json__"] == {
        "id": "ORDER-001",
        "name": "手工订单",
        "ontology_id": "business-order-scope",
        "updated_at": "2026-07-01T02:03:04Z",
        "risk": "low",
    }
    assert by_id["formal-only-order"]["updated_at"] == db.get(
        ObjectInstance,
        "formal-only-order",
    ).updated_at
    assert "__business_properties__" not in by_id["entity-supplier"]
    assert {(row["src"], row["tgt"], row["props"]["id"])
            for row in neo.relations} == {
        (
            "projection-contract::entity-supplier",
            "projection-contract::entity-order",
            "relation-legacy",
        ),
        (
            "projection-contract::entity-supplier",
            "projection-contract::formal-only-order",
            "formal-link",
        ),
    }
    assert all(row["src_label"] == "OntologyEntity" for row in neo.relations)
    assert all(row["key_field"] == "projection_key" for row in neo.relations)
    assert {row["src"] for row in neo.relations} == {
        "projection-contract::entity-supplier",
    }
    relations = {row["props"]["id"]: row["props"] for row in neo.relations}
    assert relations["relation-legacy"]["business_id"] == "SUPPLIES-001"
    assert relations["relation-legacy"]["business_updated_at"] == (
        "2026-07-02T02:03:04Z"
    )
    assert relations["formal-link"]["business_id"] == "OWNS-001"
    assert relations["formal-link"]["business_updated_at"] == (
        "2026-07-03T02:03:04Z"
    )
    assert all("updated_at" not in props for props in relations.values())
    assert neo.closed is True


def test_formal_external_id_never_mirrors_an_earlier_formal_only_id(
    db,
    admin_user,
    monkeypatch,
):
    project = _seed_projection_truth(db, admin_user)
    db.add(ObjectInstance(
        id="formal-later-order",
        ontology_id=project.id,
        object_type_id="ot-order",
        external_id="formal-only-order",
        properties={"name": "后续订单"},
        computed={},
        source="manual",
    ))
    db.commit()
    neo = RecordingNeo4j()
    monkeypatch.setattr(
        "app.services.v2.graph.neo4j_service.Neo4jService",
        lambda: neo,
    )

    assert MappingService(db)._rebuild_neo4j_projection(project.id) is True
    assert {row["id"] for row in neo.nodes} == {
        "entity-supplier",
        "entity-order",
        "formal-only-order",
        "formal-later-order",
    }


def test_rebuild_rejects_unrelated_legacy_and_formal_node_id_collision(
    db,
    admin_user,
    monkeypatch,
):
    project = _seed_projection_truth(db, admin_user)
    db.add(Entity(
        id="formal-only-order",
        ontology_id=project.id,
        name_cn="冲突旧实体",
        type="Order",
        properties={},
    ))
    db.commit()
    neo = RecordingNeo4j()
    monkeypatch.setattr(
        "app.services.v2.graph.neo4j_service.Neo4jService",
        lambda: neo,
    )

    assert MappingService(db)._rebuild_neo4j_projection(project.id) is False
    assert neo.closed is True


def test_rebuild_rejects_unrelated_legacy_and_formal_relation_id_collision(
    db,
    admin_user,
    monkeypatch,
):
    project = _seed_projection_truth(db, admin_user)
    db.add(Relation(
        id="formal-link",
        ontology_id=project.id,
        source_entity="entity-supplier",
        target_entity="entity-order",
        type="CONFLICTS",
        properties={},
    ))
    db.commit()
    neo = RecordingNeo4j()
    monkeypatch.setattr(
        "app.services.v2.graph.neo4j_service.Neo4jService",
        lambda: neo,
    )

    assert MappingService(db)._rebuild_neo4j_projection(project.id) is False
    assert neo.closed is True


@pytest.mark.parametrize(
    ("node_delta", "relation_result"),
    [(-1, True), (0, False)],
)
def test_rebuild_rejects_partial_neo4j_writes(
    db,
    admin_user,
    monkeypatch,
    node_delta,
    relation_result,
):
    project = _seed_projection_truth(db, admin_user)
    neo = RecordingNeo4j(
        node_delta=node_delta,
        relation_result=relation_result,
    )
    monkeypatch.setattr(
        "app.services.v2.graph.neo4j_service.Neo4jService",
        lambda: neo,
    )

    assert MappingService(db)._rebuild_neo4j_projection(project.id) is False
    assert neo.closed is True


def test_project_fence_is_durable_on_rebuild_failure(db, admin_user):
    project = OntologyProject(
        id="projection-fence-failure",
        name="Projection fence",
        domain="test",
        created_by=admin_user.id,
    )
    db.add(project)
    db.commit()
    mark_projecting(db, project.id)
    db.commit()

    with pytest.raises(ProjectionRebuildError):
        rebuild_after_commit(db, project.id, rebuild=lambda _ontology_id: False)

    db.refresh(project)
    assert project.projection_status == "failed"
    assert "incomplete" in (project.projection_error or "")


def test_project_fence_final_commit_failure_is_durably_failed(
    db,
    admin_user,
    monkeypatch,
):
    project = OntologyProject(
        id="projection-fence-final-commit",
        name="Projection fence final commit",
        domain="test",
        created_by=admin_user.id,
    )
    db.add(project)
    db.commit()
    mark_projecting(db, project.id)
    db.commit()

    original_commit = db.commit
    commit_calls = 0

    def fail_first_commit():
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            raise RuntimeError("injected final commit failure")
        return original_commit()

    monkeypatch.setattr(db, "commit", fail_first_commit)

    with pytest.raises(ProjectionRebuildError, match="finalization failed"):
        rebuild_after_commit(db, project.id, rebuild=lambda _ontology_id: True)

    db.expire_all()
    persisted = db.get(OntologyProject, project.id)
    assert persisted.projection_status == "failed"
    assert "finalization failed" in (persisted.projection_error or "")


@pytest.mark.parametrize("status", ["repair_required", "projecting", "failed"])
def test_startup_repairs_every_non_ready_projection(
    db,
    admin_user,
    status,
):
    from sqlalchemy.orm import sessionmaker

    project = OntologyProject(
        id=f"startup-repair-{status}",
        name="Startup repair",
        domain="test",
        created_by=admin_user.id,
        projection_status=status,
        projection_error="old failure",
    )
    db.add(project)
    db.commit()
    seen: list[str] = []

    repaired = repair_unready_projections(
        session_factory=sessionmaker(bind=db.get_bind()),
        rebuild=lambda _session, ontology_id: seen.append(ontology_id) or True,
    )

    db.expire_all()
    assert repaired == 1
    assert seen == [project.id]
    assert db.get(OntologyProject, project.id).projection_status == "ready"
    assert db.get(OntologyProject, project.id).projection_error is None


def test_startup_repair_failure_remains_durable_and_blocks_startup(
    db,
    admin_user,
):
    from sqlalchemy.orm import sessionmaker

    project = OntologyProject(
        id="startup-repair-fails",
        name="Startup repair fails",
        domain="test",
        created_by=admin_user.id,
        projection_status="repair_required",
    )
    db.add(project)
    db.commit()

    with pytest.raises(ProjectionRebuildError):
        repair_unready_projections(
            session_factory=sessionmaker(bind=db.get_bind()),
            rebuild=lambda _session, _ontology_id: False,
        )

    db.expire_all()
    persisted = db.get(OntologyProject, project.id)
    assert persisted.projection_status == "failed"
    assert "incomplete" in (persisted.projection_error or "")


class FakeNode(dict):
    def __init__(self, properties: dict, *, element_id: str, labels: list[str]):
        super().__init__(properties)
        self.element_id = element_id
        self.labels = set(labels)


class FakeRelationship(dict):
    def __init__(self, properties: dict, *, element_id: str, rel_type: str):
        super().__init__(properties)
        self.element_id = element_id
        self.type = rel_type


def test_graph_data_never_exposes_neo4j_element_ids():
    from app.services.v2.graph.neo4j_service import Neo4jService

    source = FakeNode(
        {"id": "sql-source", "ontology_id": "ont"},
        element_id="neo4j-node-1",
        labels=["OntologyEntity"],
    )
    target = FakeNode(
        {"id": "formal-target", "ontology_id": "ont"},
        element_id="neo4j-node-2",
        labels=["OntologyEntity"],
    )
    relation = FakeRelationship(
        {"id": "sql-relation", "semantic_type": "OWNS"},
        element_id="neo4j-rel-9",
        rel_type="REL_INTERNAL",
    )
    session = MagicMock()
    session.run.side_effect = [
        [{"n": source}, {"n": target}],
        [{"r": relation, "n": source, "m": target}],
    ]
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.session.return_value.__exit__.return_value = False
    service = object.__new__(Neo4jService)
    service._available = True
    service._driver = driver

    result = service.get_graph_data("ont")

    assert {item["id"] for item in result["nodes"]} == {
        "sql-source",
        "formal-target",
    }
    assert result["edges"] == [{
        "id": "sql-relation",
        "source": "sql-source",
        "target": "formal-target",
        "type": "OWNS",
        "properties": {
            "id": "sql-relation",
            "semantic_type": "OWNS",
        },
    }]
    assert "neo4j-node-1" not in repr(result)
    assert "neo4j-rel-9" not in repr(result)
    node_query = session.run.call_args_list[0]
    assert "MATCH (n:OntologyEntity)" in node_query.args[0]
    assert node_query.kwargs["type_filter"] is None


def test_graph_data_filters_by_business_type_property():
    from app.services.v2.graph.neo4j_service import Neo4jService

    session = MagicMock()
    session.run.return_value = []
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.session.return_value.__exit__.return_value = False
    service = object.__new__(Neo4jService)
    service._available = True
    service._driver = driver

    assert service.get_graph_data("ont", label_filter="Supplier") == {
        "nodes": [],
        "edges": [],
    }
    query_call = session.run.call_args_list[0]
    assert "n.type = $type_filter" in query_call.args[0]
    assert query_call.kwargs["type_filter"] == "Supplier"


def test_nested_sql_json_is_round_tripped_through_neo4j_properties():
    from app.services.v2.graph.neo4j_service import (
        _decode_properties,
        _encode_properties,
    )

    original = {
        "id": "stable-id",
        "nested": {"amount": 12, "currency": "CNY"},
        "mixed": [1, "two", {"three": True}],
        "active": True,
        "__ontology_json_fields": ["a legitimate business value"],
        "prefixed": "__ONTOLOGY_JSON_V1__:business text",
    }
    encoded = _encode_properties(original)

    assert isinstance(encoded["nested"], str)
    assert isinstance(encoded["mixed"], str)
    assert isinstance(encoded["__ontology_json_fields"], str)
    assert _decode_properties(encoded) == original


def test_oversized_sql_integer_is_preserved_as_neo4j_safe_text():
    from app.services.v2.graph.neo4j_service import (
        _decode_properties,
        _encode_properties,
    )

    oversized = 1 << 63
    encoded = _encode_properties({"oversized": oversized})

    assert encoded == {"oversized": str(oversized)}
    assert _decode_properties(encoded) == {"oversized": str(oversized)}
