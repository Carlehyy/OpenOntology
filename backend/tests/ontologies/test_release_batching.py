"""Bounded fact/release operations retain full cross-page semantics."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.ontologies.formal_modeling.facts import (
    property_fact_batch, record_object_presence, record_object_tombstone,
    record_property_facts,
)
from app.ontologies.formal_modeling.models import PropertyFact


def test_fact_batch_preserves_chain_presence_removal_and_lineage(db, ontology):
    oid = ontology["id"]
    options = dict(ontology_id=oid, instance_id="batch-object", object_type_id="type",
                   source="pipeline", ontology_version="v-test", ontology_release_id="release",
                   source_dataset_version_id="lake-version", actor_id="actor", caused_by="trial")
    with property_fact_batch(db, ontology_id=oid, instance_ids=["batch-object"]):
        presence = record_object_presence(db, **options)
        assert presence.id
        first = record_property_facts(db, old_props=None, new_props={"a": 1, "b": None}, **options)
        assert all(item.id for item in first)
        second = record_property_facts(db, old_props={"a": 1, "b": None}, new_props={"a": 2}, **options)
        a = next(item for item in second if item.property_name == "a")
        b = next(item for item in second if item.property_name == "b")
        assert a.supersedes_id == next(item.id for item in first if item.property_name == "a")
        assert a.seq == b.seq == 2
        assert b.value == {"v": None, "present": False}
        tombstone = record_object_tombstone(db, **options)
        assert tombstone.seq == 2
        assert record_object_tombstone(db, **options) is None
        revived = record_object_presence(db, **options)
        assert revived.seq == 3
        assert revived.supersedes_id == tombstone.id
        creation = record_property_facts(db, old_props=None, new_props={"a": 3}, **options)[0]
        assert creation.seq == 3
        assert creation.supersedes_id is None
    rows = db.query(PropertyFact).filter_by(ontology_id=oid).all()
    assert len(rows) == 8
    assert all(item.ontology_release_id == "release" for item in rows)
    assert all(item.source_dataset_version_id == "lake-version" for item in rows)
    assert all(item.actor_id == "actor" and item.caused_by == "trial" for item in rows)
    assert "ontology_property_fact_batch" not in db.info


def test_fact_batch_queries_heads_once_and_flushes_bounded_rows(db, ontology):
    oid = ontology["id"]
    selects = []

    def count(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT") and "fo_property_facts" in statement:
            selects.append(statement)

    event.listen(db.bind, "before_cursor_execute", count)
    try:
        with property_fact_batch(db, ontology_id=oid, instance_ids=[f"object-{i}" for i in range(200)]):
            for i in range(200):
                record_object_presence(db, ontology_id=oid, instance_id=f"object-{i}",
                                       object_type_id="type", source="pipeline", ontology_version="v")
                facts = record_property_facts(
                    db, ontology_id=oid, instance_id=f"object-{i}", object_type_id="type",
                    old_props=None, new_props={f"column-{j}": j for j in range(47)},
                    source="pipeline", ontology_version="v",
                )
                assert len(facts) == 47
        assert len(selects) == 1
        assert db.query(PropertyFact).filter_by(ontology_id=oid).count() == 200 * 48
    finally:
        event.remove(db.bind, "before_cursor_execute", count)


def test_fact_batch_rejects_unbounded_or_nested_inputs(db, ontology):
    with pytest.raises(ValueError, match="at most"):
        with property_fact_batch(db, ontology_id=ontology["id"], instance_ids=(str(i) for i in range(1001))):
            pass
    with property_fact_batch(db, ontology_id=ontology["id"], instance_ids=[]):
        with pytest.raises(ValueError, match="nested"):
            with property_fact_batch(db, ontology_id=ontology["id"], instance_ids=[]):
                pass


def test_fact_latest_window_honors_timestamp_sequence_id_ties(db, ontology):
    oid = ontology["id"]
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for fact_id, seq in (("a", 1), ("b", 2), ("c", 2)):
        db.add(PropertyFact(id=fact_id, ontology_id=oid, instance_id="one", object_type_id="type",
                            property_name="a", value={"v": seq}, kind="property", source="pipeline",
                            seq=seq, recorded_at=stamp))
    db.flush()
    changed = record_property_facts(db, ontology_id=oid, instance_id="one", object_type_id="type",
                                    old_props={"a": 2}, new_props={"a": 3}, source="pipeline",
                                    ontology_version="v")
    assert changed[0].supersedes_id == "c"
    assert changed[0].seq == 3


def _trial_reader(db, ontology, snapshot, objects, links=()):
    from app.ontologies.versions.evolution_service import complete_snapshot, snapshot_models
    from app.ontologies.versions.models import (
        OntologyTrialLink, OntologyTrialObject, OntologyTrialRun, OntologyVersion,
    )
    from app.ontologies.versions.trial_projection import TrialProjectionReader
    root = db.query(OntologyVersion).filter_by(ontology_id=ontology["id"]).one()
    run = OntologyTrialRun(ontology_id=ontology["id"], version_id=root.id,
                           revision=0, snapshot_hash="test", created_by=root.created_by)
    db.add(run)
    db.flush()
    db.add_all([OntologyTrialObject(trial_run_id=run.id, object_id=item["objectId"],
                                    object_type_id=item["objectTypeId"], properties=item["properties"],
                                    computed=item.get("computed") or {}) for item in objects])
    db.add_all([OntologyTrialLink(trial_run_id=run.id, link_id=item["linkId"],
                                  link_type_id=item["linkTypeId"], source_object_id=item["sourceObjectId"],
                                  target_object_id=item["targetObjectId"], properties=item["properties"])
                for item in links])
    db.flush()
    return TrialProjectionReader(
        db, db.query(OntologyTrialObject).filter_by(trial_run_id=run.id),
        db.query(OntologyTrialLink).filter_by(trial_run_id=run.id),
        snapshot_models(complete_snapshot(snapshot)),
    )


def _reader_snapshot():
    return {"objectTypes": [
        {"id": "order", "name": "Order", "displayName": "Order", "primaryKey": "pk-order",
         "properties": [{"id": "pk-order", "name": "id", "type": "string", "required": True},
                        {"id": "status", "name": "status", "type": "string"}]},
        {"id": "recipient", "name": "Recipient", "displayName": "Recipient", "primaryKey": "pk-recipient",
         "properties": [{"id": "pk-recipient", "name": "id", "type": "string", "required": True},
                        {"id": "email", "name": "email", "type": "string"}]},
        {"id": "audit", "name": "Audit", "displayName": "Audit", "primaryKey": "pk-audit",
         "properties": [{"id": "pk-audit", "name": "id", "type": "string", "required": True}]},
    ], "linkTypes": [
        {"id": "owner", "name": "owner", "displayName": "owner", "sourceObjectTypeId": "order",
         "targetObjectTypeId": "recipient", "cardinality": "many-to-one", "properties": []},
        {"id": "audit-link", "name": "audit_link", "displayName": "audit_link",
         "sourceObjectTypeId": "order", "targetObjectTypeId": "audit",
         "cardinality": "one-to-many", "properties": []},
    ], "actions": [], "functions": [], "sentinels": [], "mappings": [], "linkMappings": []}


def test_persisted_validator_keeps_object_and_link_id_namespaces_separate(db, ontology):
    from app.ontologies.formal_modeling.validation import validate_model
    from app.ontologies.versions.model_validator import validate_persisted_model

    reader = _trial_reader(db, ontology, _reader_snapshot(), [
        {"objectId": "shared-id", "objectTypeId": "order", "properties": {"id": "O-1"}},
        {"objectId": "recipient-1", "objectTypeId": "recipient", "properties": {"id": "R-1"}},
    ], [
        {"linkId": "shared-id", "linkTypeId": "owner", "sourceObjectId": "shared-id",
         "targetObjectId": "recipient-1", "properties": {}},
    ])
    models = reader.models
    expected = validate_model(
        models["objectTypes"], models["linkTypes"], models["actions"], models["functions"],
        list(reader.iter("objects")), list(reader.iter("links")),
    )
    assert expected == []
    assert validate_persisted_model(
        db, ontology["id"], models["objectTypes"], models["linkTypes"], models["actions"],
        models["functions"], object_rows=reader.objects, link_rows=reader.links,
    ) == expected


def test_trial_reader_cross_page_contracts_match_canonical_lists(db, ontology):
    from app.ontologies.formal_modeling.validation import (
        validate_instance_contract, validate_link_instance_contract,
    )
    snapshot = _reader_snapshot()
    objects = [{"objectId": f"o-{i:04}", "objectTypeId": "order", "properties": {"id": f"O-{i}"}}
               for i in range(401)] + [
        {"objectId": "r-1", "objectTypeId": "recipient", "properties": {"id": "R-1"}},
        {"objectId": "r-2", "objectTypeId": "recipient", "properties": {"id": "R-2"}},
    ]
    links = [{"linkId": f"l-{i:04}", "linkTypeId": "owner", "sourceObjectId": f"o-{i:04}",
              "targetObjectId": "r-1", "properties": {}} for i in range(401)]
    reader = _trial_reader(db, ontology, snapshot, objects, links)
    # Lookup at the end of page three and uniqueness outside the first page.
    assert reader.find("objects", "o-0400").properties["id"] == "O-400"
    instances = list(reader.iter("objects"))
    candidate = SimpleNamespace(id="candidate", object_type_id="order", properties={"id": "O-400"}, computed={})
    expected = validate_instance_contract(reader.models["objectTypes"], [*instances, candidate], {candidate.id})
    assert reader.validate_object_candidate(candidate, []) == expected
    assert [item["code"] for item in expected] == ["duplicate_primary_key"]
    # Distinct target cardinality, identical duplicate edge, invalid endpoint and
    # excluded edge all keep their canonical error vocabulary and counts.
    base_links = list(reader.iter("links"))
    for link_id, target, excluded in (
        ("new", "r-2", set()), ("new", "r-1", set()),
        ("new", "missing", set()), ("l-0400", "r-2", {"l-0400"}),
    ):
        candidate = SimpleNamespace(id=link_id, link_type_id="owner", source_object_id="o-0400",
                                    target_object_id=target, properties={})
        expected = validate_link_instance_contract(
            reader.models["linkTypes"], instances,
            [*(item for item in base_links if item.id not in excluded), candidate], {candidate.id})
        assert reader.validate_link_candidate(candidate, [], [], excluded) == expected


def test_trial_reader_actions_use_isolated_targets_links_and_recipients(db, ontology, monkeypatch):
    from app.ontologies.versions.evolution_service import _simulate_sentinels
    snapshot = _reader_snapshot()
    snapshot["actions"] = [{
        "id": "review", "name": "review", "displayName": "review", "objectTypeId": "order",
        "parameters": [], "rules": [
            {"id": "create", "name": "create", "type": "create_object", "order": 0,
             "config": {"targetObjectTypeId": "audit", "propertyMappings": []}},
            {"id": "update", "name": "update", "type": "update_property", "order": 1,
             "config": {"targetProperty": "status", "valueSource": "constant", "value": '"reviewed"'}},
            {"id": "link", "name": "link", "type": "create_link", "order": 2,
             "config": {"linkTypeId": "audit-link", "targetSource": "created_object", "targetValue": "audit"}},
            {"id": "delete", "name": "delete", "type": "delete_link", "order": 3,
             "config": {"linkTypeId": "owner", "condition": "target.email == 'old@example.com'"}},
            {"id": "notify", "name": "notify", "type": "notification", "order": 4,
             "config": {"channel": "internal", "recipientSource": "link", "linkTypeId": "owner",
                        "recipientProperty": "email", "messageTemplate": "{{object.status}}"}},
        ],
    }]
    snapshot["sentinels"] = [{"id": "s", "name": "s", "enabled": True, "bindings": [
        {"alias": "order", "objectTypeId": "order", "filter": "order.id == 'O-400'"}],
        "primaryAlias": "order", "condition": "True", "actionIds": ["review"]}]
    objects = [{"objectId": f"o-{i:04}", "objectTypeId": "order",
                "properties": {"id": f"O-{i}", "status": "new"}} for i in range(401)] + [
        {"objectId": "recipient-old", "objectTypeId": "recipient", "properties": {"id": "old", "email": "old@example.com"}},
        {"objectId": "recipient-new", "objectTypeId": "recipient", "properties": {"id": "new", "email": "new@example.com"}},
    ]
    links = [{"linkId": f"l-{name}", "linkTypeId": "owner", "sourceObjectId": "o-0400",
              "targetObjectId": f"recipient-{name}", "properties": {}} for name in ("old", "new")]
    reader = _trial_reader(db, ontology, snapshot, objects, links)
    original_iter = reader.iter
    calls = []
    def checked_iter(key, **filters):
        if key in ("objects", "links"):
            assert filters, "action reader unexpectedly materialized the full universe"
        calls.append((key, filters))
        yield from original_iter(key, **filters)
    monkeypatch.setattr(reader, "iter", checked_iter)
    statements = []
    def observe(_conn, _cursor, statement, *_args):
        statements.append(statement)
    event.listen(db.bind, "before_cursor_execute", observe)
    try:
        bounded = _simulate_sentinels(snapshot, None, None, reader=reader)[0]
    finally:
        event.remove(db.bind, "before_cursor_execute", observe)
    assert bounded["errors"] == []
    assert bounded["matched"] == 1
    assert bounded["plannedActions"] == 1
    effects = bounded["plannedActionSamples"][0]["effects"]
    assert [item["type"] for item in effects] == ["create_object", "update_property", "create_link", "delete_link", "notification"]
    assert effects[3]["matchedLinkIds"] == ["l-old"]
    assert effects[4]["recipient"] == "new@example.com"
    assert effects[4]["message"] == "reviewed"
    assert any(key == "objects" and values.get("object_type_id") == "order" for key, values in calls)
    assert not any("fo_object_instances" in stmt or "fo_link_instances" in stmt for stmt in statements)
    assert not any(stmt.lstrip().upper().startswith(("INSERT INTO FO_", "UPDATE FO_", "DELETE FROM FO_")) for stmt in statements)
    assert reader.find("objects", "o-0400").properties["status"] == "new"
    assert reader.links.count() == 2
    # List-backed previews remain compatible, including virtual created objects.
    legacy = _simulate_sentinels(snapshot, objects, links)[0]
    assert legacy["errors"] == []
    assert legacy["matched"] == bounded["matched"]
    assert legacy["plannedActionSamples"][0]["effects"][3:] == effects[3:]


def test_validation_errors_merge_exact_counts_without_retaining_every_error():
    from app.ontologies.versions.model_validator import ValidationErrors
    first = ValidationErrors(({"code": "invalid", "id": i} for i in range(10001)))
    assert len(first) == 100
    assert first.total_count == 10001
    assert first.code_counts == {"invalid": 10001}
    merged = ValidationErrors(first, sample_limit=7)
    merged.extend({"code": "missing"} for _ in range(23))
    merged += first
    assert len(merged) == 7
    assert merged.total_count == 20025
    assert merged.code_counts == {"invalid": 20002, "missing": 23}
    assert [item["id"] for item in merged] == list(range(7))


def test_persisted_validator_counts_all_invalid_rows_with_bounded_samples(db, ontology):
    from collections import Counter
    from app.ontologies.formal_modeling.validation import validate_model
    from app.ontologies.versions.model_validator import ValidationErrors, validate_persisted_model
    snapshot = _reader_snapshot()
    # All rows duplicate the PK; the late invalid enum still has to be scanned
    # after the sample report is full, and link fanout crosses the batch boundary.
    snapshot["objectTypes"][0]["properties"][1]["type"] = "boolean"
    objects = [{"objectId": f"o-{i:04}", "objectTypeId": "order",
                "properties": {"id": "O-duplicate", "status": False if i < 600 else "invalid"}}
               for i in range(603)] + [
        {"objectId": "r-1", "objectTypeId": "recipient", "properties": {"id": "R-1"}},
        {"objectId": "r-2", "objectTypeId": "recipient", "properties": {"id": "R-2"}},
    ]
    links = [{"linkId": f"l-{i:04}", "linkTypeId": "owner", "sourceObjectId": "o-0000",
              "targetObjectId": "r-1" if i % 2 else "r-2", "properties": {}} for i in range(603)]
    reader = _trial_reader(db, ontology, snapshot, objects, links)
    model = reader.models
    expected = validate_model(model["objectTypes"], model["linkTypes"], model["actions"], model["functions"],
                              list(reader.iter("objects")), list(reader.iter("links")))
    bounded = validate_persisted_model(db, ontology["id"], model["objectTypes"], model["linkTypes"],
                                       model["actions"], model["functions"],
                                       object_rows=reader.objects, link_rows=reader.links)
    assert isinstance(bounded, ValidationErrors)
    assert bounded.total_count == len(expected)
    assert bounded.code_counts == Counter(item["code"] for item in expected)
    assert bounded.code_counts["cardinality_violation"] == 603
    assert bounded.code_counts["duplicate_primary_key"] == 603
    assert bounded.code_counts["duplicate_link"] == 603
    assert len(bounded) == 100
    assert db.execute(__import__("sqlalchemy").text(
        "SELECT name FROM sqlite_temp_master WHERE type='table' AND name LIKE 'release_validation_%'"
    )).all() == []
