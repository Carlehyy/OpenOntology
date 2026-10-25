"""Regression coverage for production Sentinel matching and parameter safety."""
from datetime import datetime, timezone
from types import SimpleNamespace

from app.models.ontology import OntologyProject
from app.models.ontology_version import OntologyVersion
from app.models.ontology_formal import (
    ActionType,
    LinkInstance,
    LinkType,
    ObjectInstance,
    ObjectType,
    PropertyFact,
)
from app.models.sentinel import Sentinel, SentinelMatchState
from app.ontologies.formal_modeling.action_engine import prepare_action_parameters
from app.ontologies.sentinels import evaluator
from app.ontologies.sentinels.dynamic_service import _dynamic_contract_errors


def _project(db, ontology_id: str) -> None:
    db.add(OntologyProject(
        id=ontology_id,
        name=ontology_id,
        domain="sentinel-tests",
        created_by="tests",
        status="published",
        version="v1.0.0",
    ))


def _object_type(db, ontology_id: str, object_type_id: str) -> ObjectType:
    row = ObjectType(
        id=object_type_id,
        ontology_id=ontology_id,
        name=object_type_id,
        display_name=object_type_id,
        primary_key="id",
        properties=[],
    )
    db.add(row)
    return row


def _sentinel(ontology_id: str, sentinel_id: str, bindings: list[dict],
              *, condition: str = "True", links: list[dict] | None = None,
              trigger_mode: str = "on_enter") -> Sentinel:
    return Sentinel(
        id=sentinel_id,
        ontology_id=ontology_id,
        name=sentinel_id,
        display_name=sentinel_id,
        bindings=bindings,
        links=links or [],
        condition=condition,
        primary_alias=bindings[0]["alias"],
        action_ids=[],
        action_parameters={},
        trigger_mode=trigger_mode,
        on_change=False,
        on_schedule=False,
        muted=False,
        enabled=True,
        status="published",
    )


def test_computed_values_are_visible_to_filters_conditions_and_parameters(db):
    ontology_id = "sentinel-computed-view"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "risk-object")
    object_type.properties = [
        {"id": "id", "name": "id", "type": "string", "required": True},
        {"id": "active", "name": "active", "type": "boolean"},
        {
            "id": "risk-score",
            "name": "risk_score",
            "type": "number",
            "source": "computed",
        },
        {"id": "seen-risk", "name": "seen_risk", "type": "number"},
    ]
    instance = ObjectInstance(
        id="risk-1",
        ontology_id=ontology_id,
        object_type_id=object_type.id,
        properties={"id": "risk-1", "active": True, "seen_risk": 0},
        computed={"risk_score": 91},
    )
    sentinel = _sentinel(
        ontology_id,
        "computed-sentinel",
        [{
            "alias": "a",
            "objectTypeId": object_type.id,
            "filter": "a.risk_score >= 90",
        }],
        condition="a.risk_score == 91",
    )
    action = ActionType(
        id="computed-action",
        ontology_id=ontology_id,
        name="computed_action",
        display_name="computed action",
        object_type_id=object_type.id,
        parameters=[{
            "name": "risk",
            "type": "number",
            "required": True,
        }],
        rules=[{
            "id": "record-risk",
            "type": "update_property",
            "name": "record risk",
            "enabled": True,
            "order": 0,
            "config": {
                "targetProperty": "seen_risk",
                "valueSource": "parameter",
                "value": "risk",
            },
        }],
    )
    sentinel.action_ids = [action.id]
    sentinel.action_parameters = {
        action.id: {
            "risk": {
                "sourceType": "property",
                "alias": "a",
                "property": "risk_score",
            },
        },
    }
    db.add_all([instance, action, sentinel])
    db.commit()

    firing = evaluator.evaluate_sentinel(
        db, ontology_id, sentinel, "manual")

    assert firing.status in {"fired", "skipped"}, (
        firing.error, firing.action_results)
    assert firing.match_count == 1
    assert not firing.error
    assert firing.action_results[0]["status"] == "success"
    db.refresh(instance)
    assert instance.properties["seen_risk"] == 91


def test_expression_error_never_manufactures_leave_or_mutates_match_state(db):
    ontology_id = "sentinel-expression-fail-closed"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "order")
    instance = ObjectInstance(
        id="order-1",
        ontology_id=ontology_id,
        object_type_id=object_type.id,
        properties={"active": True},
    )
    sentinel = _sentinel(
        ontology_id,
        "fail-closed-sentinel",
        [{"alias": "a", "objectTypeId": object_type.id}],
        condition="a.active == True",
        trigger_mode="on_enter_leave",
    )
    db.add_all([instance, sentinel])
    db.commit()

    first = evaluator.evaluate_sentinel(
        db, ontology_id, sentinel, "manual")
    state = db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id).one()
    original_detail = dict(state.match_detail)
    # ``safe_eval`` alone would map the missing key to None and make this
    # expression True. Sentinel runtime must classify it as an invalid
    # observation, not an enter/leave edge.
    sentinel.condition = "a.missing_value != 'closed'"
    db.commit()

    failed = evaluator.evaluate_sentinel(
        db, ontology_id, sentinel, "manual")
    db.refresh(state)

    assert first.status == "skipped"
    assert failed.status == "error"
    assert failed.entered == []
    assert failed.left == []
    assert state.runtime_status == "completed"
    assert state.match_detail == original_detail


def test_runtime_enforces_every_declared_link_constraint(db):
    ontology_id = "sentinel-all-links"
    _project(db, ontology_id)
    for object_type_id in ("type-a", "type-b", "type-c"):
        _object_type(db, ontology_id, object_type_id)
    db.add_all([
        LinkType(
            id="link-ab", ontology_id=ontology_id,
            name="link_ab", display_name="link ab",
            source_object_type_id="type-a",
            target_object_type_id="type-b",
            cardinality="many-to-many",
            properties=[],
        ),
        LinkType(
            id="link-bc", ontology_id=ontology_id,
            name="link_bc", display_name="link bc",
            source_object_type_id="type-b",
            target_object_type_id="type-c",
            cardinality="many-to-many",
            properties=[],
        ),
        LinkType(
            id="link-ac", ontology_id=ontology_id,
            name="link_ac", display_name="link ac",
            source_object_type_id="type-a",
            target_object_type_id="type-c",
            cardinality="many-to-many",
            properties=[],
        ),
    ])
    a = ObjectInstance(
        id="a-1", ontology_id=ontology_id,
        object_type_id="type-a", properties={})
    b = ObjectInstance(
        id="b-1", ontology_id=ontology_id,
        object_type_id="type-b", properties={})
    c = ObjectInstance(
        id="c-1", ontology_id=ontology_id,
        object_type_id="type-c", properties={})
    # AB and BC exist; the declared AC constraint deliberately does not.
    ab = LinkInstance(
        id="ab", ontology_id=ontology_id, link_type_id="link-ab",
        source_object_id=a.id, target_object_id=b.id, properties={})
    bc = LinkInstance(
        id="bc", ontology_id=ontology_id, link_type_id="link-bc",
        source_object_id=b.id, target_object_id=c.id, properties={})
    sentinel = _sentinel(
        ontology_id,
        "all-links-sentinel",
        [
            {"alias": "a", "objectTypeId": "type-a"},
            {"alias": "b", "objectTypeId": "type-b"},
            {"alias": "c", "objectTypeId": "type-c"},
        ],
        links=[
            {"from": "a", "linkTypeId": "link-ab", "to": "b"},
            {"from": "b", "linkTypeId": "link-bc", "to": "c"},
            {"from": "a", "linkTypeId": "link-ac", "to": "c"},
        ],
    )
    db.add_all([a, b, c, ab, bc, sentinel])
    db.commit()

    firing = evaluator.evaluate_sentinel(
        db, ontology_id, sentinel, "manual")

    assert firing.status == "no_match"
    assert firing.match_count == 0


def test_absence_fact_flush_failure_does_not_poison_evaluator_session(
        db, monkeypatch):
    """A best-effort provenance write must leave the outer run committable."""
    ontology_id = "sentinel-absence-savepoint-recovery"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "absence-object")
    sentinel = _sentinel(
        ontology_id,
        "absence-sentinel",
        [{"alias": "a", "objectTypeId": object_type.id}],
        condition="a.active == True",
    )
    db.add(sentinel)
    db.commit()

    def fail_during_flush(fact_db, **_kwargs):
        # The NOT NULL violation is deterministic and exercises SQLAlchemy's
        # failed-flush state inside the same SAVEPOINT used in production.
        fact_db.add(PropertyFact(
            ontology_id=None,
            instance_id=sentinel.id,
            property_name="query_result",
            value={"v": {"empty": True}},
            kind="absence",
            source="test://forced-flush-failure",
        ))
        fact_db.flush()

    monkeypatch.setattr(
        "app.ontologies.formal_modeling.facts.record_absence_fact",
        fail_during_flush,
    )

    firing = evaluator.evaluate_sentinel(
        db, ontology_id, sentinel, "manual")

    assert firing.status == "no_match"
    assert firing.error is None
    assert db.is_active
    assert db.query(PropertyFact).filter_by(
        ontology_id=ontology_id,
        instance_id=sentinel.id,
    ).count() == 0


def test_tuple_cap_is_an_error_and_does_not_consume_edges(db, monkeypatch):
    """The 1000 cap remains a cross-binding guard, not a single-row cap."""
    ontology_id = "sentinel-cap"
    _project(db, ontology_id)
    type_a = _object_type(db, ontology_id, "cap-a")
    type_b = _object_type(db, ontology_id, "cap-b")
    db.add_all([
        ObjectInstance(
            id=f"cap-a-{index}", ontology_id=ontology_id,
            object_type_id=type_a.id, properties={"active": True},
        )
        for index in range(2)
    ] + [
        ObjectInstance(
            id=f"cap-b-{index}", ontology_id=ontology_id,
            object_type_id=type_b.id, properties={"active": True},
        )
        for index in range(2)
    ])
    sentinel = _sentinel(
        ontology_id,
        "cap-sentinel",
        [
            {"alias": "a", "objectTypeId": type_a.id},
            {"alias": "b", "objectTypeId": type_b.id},
        ],
        condition="a.active == True and b.active == True",
    )
    db.add(sentinel)
    db.commit()
    monkeypatch.setattr(evaluator, "MAX_TUPLES", 2)

    firing = evaluator.evaluate_sentinel(
        db, ontology_id, sentinel, "manual")

    assert firing.status == "error"
    assert "安全上限" in (firing.error or "")
    assert firing.entered == []
    assert firing.left == []
    assert db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id).count() == 0


def test_single_binding_runtime_scans_full_release_and_hits_last_row(
        db, monkeypatch):
    ontology_id = "sentinel-single-binding-full"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "single-full")
    db.add_all([
        ObjectInstance(
            id=f"single-full-{index}", ontology_id=ontology_id,
            object_type_id=object_type.id,
            properties={"index": index, "active": True},
        )
        for index in range(2000)
    ])
    sentinel = _sentinel(
        ontology_id,
        "single-full-sentinel",
        [{"alias": "row", "objectTypeId": object_type.id}],
        condition="row.index == 1999",
    )
    db.add(sentinel)
    db.commit()
    scanned: list[int] = []

    def capture_absence(*_args, **kwargs):
        scanned.append(int(kwargs.get("scanned") or 0))

    monkeypatch.setattr(
        "app.ontologies.formal_modeling.facts.record_absence_fact",
        capture_absence,
    )
    firing = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "manual")
    assert firing.error is None, firing.error
    assert firing.match_count == 1
    assert firing.matches == [{"row": "single-full-1999"}]
    assert scanned == [2000]

    sentinel.condition = "row.index < 0"
    db.commit()
    zero = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "manual")
    assert zero.error is None, zero.error
    assert zero.match_count == 0
    assert scanned[-1] == 2000

    sentinel.condition = "row.index == 1999 or row.missing == 1"
    db.commit()
    tail_error = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "manual")
    assert tail_error.status == "error"
    assert tail_error.entered == []
    assert tail_error.left == []
    assert "不存在的属性" in (tail_error.error or "")


def test_single_binding_preview_scans_full_release_and_hits_last_row(db):
    ontology_id = "sentinel-single-preview-full"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "preview-full")
    release = OntologyVersion(
        id="release-preview-full", ontology_id=ontology_id,
        version_number="v1.0.0", version_label="preview full",
        node_kind="release", lifecycle_status="released",
        revision=0, snapshot_formal={"objectTypes": [], "linkTypes": [],
                                     "actions": [], "functions": [],
                                     "sentinels": []},
        snapshot_hash="0" * 64, created_by="tests",
    )
    db.add(release)
    db.add_all([
        ObjectInstance(
            id=f"preview-full-{index}", ontology_id=ontology_id,
            ontology_release_id=release.id,
            object_type_id=object_type.id,
            properties={"index": index, "active": True},
        )
        for index in range(2000)
    ])
    sentinel = _sentinel(
        ontology_id,
        "preview-full-sentinel",
        [{"alias": "row", "objectTypeId": object_type.id}],
        condition="row.index == 1999",
    )
    db.add(sentinel)
    db.commit()

    report = evaluator.preview_sentinel(
        db, ontology_id, sentinel, release.id)
    assert report["errors"] == []
    assert report["candidateCount"] == 2000
    assert report["candidateCapReached"] is False
    assert report["matchCount"] == 1

    sentinel.condition = "row.index < 0"
    db.commit()
    zero = evaluator.preview_sentinel(
        db, ontology_id, sentinel, release.id)
    assert zero["errors"] == []
    assert zero["candidateCount"] == 2000
    assert zero["candidateCapReached"] is False
    assert zero["matchCount"] == 0

    sentinel.condition = "row.index == 1999 or row.missing == 1"
    db.commit()
    tail_error = evaluator.preview_sentinel(
        db, ontology_id, sentinel, release.id)
    assert tail_error["candidateCount"] == 2000
    assert tail_error["candidateCapReached"] is False
    assert tail_error["errors"]
    assert tail_error["matchCount"] == 0


def test_leave_snapshot_restores_unicode_properties_and_event_bindings(db):
    ontology_id = "sentinel-leave-snapshot"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "snapshot-object")
    instance = ObjectInstance(
        id="snapshot-1",
        ontology_id=ontology_id,
        object_type_id=object_type.id,
        properties={"名称": "订单A"},
        computed={"风险值": 88},
    )
    db.add(instance)
    db.commit()
    occurred_at = datetime.now(timezone.utc)
    detail = evaluator._snapshot_match_detail(
        {"a": instance},
        edge="leave",
        match_key=instance.id,
        occurred_at=occurred_at,
    )
    db.delete(instance)
    db.commit()
    restored = evaluator._tuple_from_detail(db, ontology_id, detail)
    action = ActionType(
        id="snapshot-action",
        ontology_id=ontology_id,
        name="snapshot_action",
        display_name="snapshot action",
        parameters=[
            {"name": "message", "type": "string", "required": True},
            {"name": "risk", "type": "number", "required": True},
            {
                "name": "edge",
                "type": "string",
                "required": True,
                "options": ["enter", "leave"],
            },
            {
                "name": "optional",
                "type": "string",
                "required": False,
                "defaultValue": "fallback",
            },
        ],
        rules=[],
    )
    sentinel = _sentinel(
        ontology_id,
        "snapshot-sentinel",
        [{"alias": "a", "objectTypeId": object_type.id}],
    )
    sentinel.action_parameters = {
        action.id: {
            "message": "对象 {{a.名称}} 在 {{event.edge}} 时风险 {{a.风险值}}",
            "risk": {
                "sourceType": "property",
                "alias": "a",
                "property": "风险值",
            },
            "edge": {"sourceType": "event", "property": "edge"},
            "optional": {
                "sourceType": "property",
                "alias": "a",
                "property": "不存在",
            },
        },
    }

    supplied, binding_errors = evaluator._configured_action_parameters(
        sentinel,
        action.id,
        restored,
        "a",
        action=action,
        event={"edge": "leave"},
    )
    prepared, contract_errors = prepare_action_parameters(action, supplied)

    assert binding_errors == []
    assert contract_errors == []
    assert prepared == {
        "message": "对象 订单A 在 leave 时风险 88",
        "risk": 88,
        "edge": "leave",
        "optional": "fallback",
    }


def test_dynamic_template_validation_matches_runtime_unicode_and_event_support():
    definition = {
        "bindings": [{"alias": "a", "objectTypeId": "snapshot-object"}],
        "primaryAlias": "a",
        "condition": None,
        "actionParameters": {
            "notify": {
                "message": "对象 {{a.名称}} 在 {{event.edge}} 时处理",
            },
        },
    }
    models = {
        "objectTypes": [
            SimpleNamespace(
                id="snapshot-object",
                properties=[{"name": "名称"}],
            ),
        ],
    }

    assert _dynamic_contract_errors(definition, models) == []

    definition["actionParameters"]["notify"]["message"] = "{{event.unknown}}"
    errors = _dynamic_contract_errors(definition, models)
    assert [item["code"] for item in errors] == [
        "sentinel_event_property_not_found",
    ]


def test_traverse_dedupes_by_primary_key_not_full_row_distinct(db):
    """回归：PostgreSQL 的 json 类型没有等值操作符，实体级 DISTINCT（SELECT
    全列含 properties/computed）会直接 UndefinedFunction——2026-08-30 云端
    哨兵评估死信事故的根因。join 去重必须落在主键 GROUP BY 上。"""
    from sqlalchemy.dialects import postgresql

    query = evaluator._traverse(db, "ont-x", "lt-x", "inst-x", True, "ot-x")
    sql = str(query.statement.compile(dialect=postgresql.dialect()))
    assert "GROUP BY fo_object_instances.id" in sql
    assert "DISTINCT" not in sql.upper()


def test_traverse_returns_each_instance_once_across_duplicate_links(db):
    """行为回归：主键 GROUP BY 去重后，重复链接仍只产出一行实例。"""
    ontology_id = "ont-traverse-dedupe"
    _project(db, ontology_id)
    db.add(ObjectType(id="ot-p", ontology_id=ontology_id, name="p",
                      display_name="p", primary_key="id", properties=[]))
    db.add(LinkType(
        id="lt-p", ontology_id=ontology_id, name="lt_p", display_name="lt p",
        source_object_type_id="ot-p", target_object_type_id="ot-p",
        cardinality="many-to-many", properties=[],
    ))
    db.add_all([
        ObjectInstance(id="inst-anchor", ontology_id=ontology_id,
                       object_type_id="ot-p", properties={}),
        ObjectInstance(id="inst-target", ontology_id=ontology_id,
                       object_type_id="ot-p", properties={"score": 42}),
        # 两条同向重复链接 + 一条反向链接
        LinkInstance(id="l1", ontology_id=ontology_id, link_type_id="lt-p",
                     source_object_id="inst-anchor", target_object_id="inst-target"),
        LinkInstance(id="l2", ontology_id=ontology_id, link_type_id="lt-p",
                     source_object_id="inst-anchor", target_object_id="inst-target"),
        LinkInstance(id="l3", ontology_id=ontology_id, link_type_id="lt-p",
                     source_object_id="inst-target", target_object_id="inst-anchor"),
    ])
    db.commit()

    forward = [i.id for i in evaluator._traverse(
        db, ontology_id, "lt-p", "inst-anchor", True, "ot-p")]
    assert forward == ["inst-target"]     # 去重：重复链接只产出一行
    backward = [i.id for i in evaluator._traverse(
        db, ontology_id, "lt-p", "inst-target", False, "ot-p")]
    assert backward == ["inst-anchor"]
