"""远程统一查询的 declared_relation：治理计划与离线键解析共用同一判定。

执行路径（prepare_remote_plan）保留发布指针比较读；离线入口
declared_relation_keys 只读已钉定的冻结快照，不触达数据库与外部数据源。
两者对同一快照必须给出相同的键与相同的拒绝 code。
"""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.ontologies.agent_query import remote_query
from app.ontologies.agent_query.schemas import QueryRequest
from app.ontologies.agent_query.service import QueryError
from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.models import AgentProfile
from app.ontologies.mappings import remote_binding
from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions.models import OntologyVersion
from app.ontologies.versions.snapshot_contract import complete_snapshot, snapshot_hash
from tests.conftest import QueryCounter


def _remote_mapping(asset, columns, field_map):
    return {"id": f"{asset}-mapping", "curatedDatasetId": f"{asset}-dataset",
            "entityClass": asset, "targetObjectTypeId": f"{asset}-type",
            "bindingMode": "remote",
            "remote": {"assetId": asset, "datasetId": f"{asset}-dataset",
                       "connectionId": "sales-connection", "schemaName": "sales",
                       "tableName": asset,
                       "columns": [{"name": column, "type": "string"} for column in columns],
                       "primaryKey": [columns[0]], "schemaFingerprint": "f" * 64},
            "fieldMapping": {**field_map, "__primary_key__": columns[0]}}


_ORDER_MAPPING = _remote_mapping(
    "orders", ["order_id", "customer_id", "note", "placed_at"],
    {"order_id": "key", "customer_id": "customer", "note": "note",
     "placed_at": "placed_at"})
_ORDER_MAPPING["remote"]["columns"][-1]["type"] = "timestamp"
_CUSTOMER_MAPPING = _remote_mapping(
    "customers", ["customer_id", "customer_name"],
    {"customer_id": "key", "customer_name": "name"})
_CUSTOMER_LOCAL = {
    "id": "customers-mapping", "curatedDatasetId": "customers-local",
    "entityClass": "customers", "targetObjectTypeId": "customers-type",
    "fieldMapping": {"customer_id": "key", "__primary_key__": "customer_id"}}

_ORDER_TYPE = {"id": "orders-type", "name": "orders", "displayName": "订单",
               "primaryKey": "key", "properties": [
                   {"id": "p-order-key", "name": "key", "type": "string", "required": True},
                   {"id": "p-customer", "name": "customer", "type": "string"},
                   {"id": "p-note", "name": "note", "type": "string"},
                   {"id": "p-placed-at", "name": "placed_at", "type": "datetime"}]}
_CUSTOMER_TYPE = {"id": "customers-type", "name": "customers", "displayName": "客户",
                  "primaryKey": "key", "properties": [
                      {"id": "p-customer-key", "name": "key", "type": "string",
                       "required": True},
                      {"id": "p-name", "name": "name", "type": "string"}]}

_LINK = {"id": "lt-placed", "name": "placed_by", "displayName": "下单客户",
         "sourceObjectTypeId": "orders-type", "targetObjectTypeId": "customers-type",
         "cardinality": "many-to-one", "properties": []}


def _link_mapping(**changes):
    """已发布 remote 直接键映射（无连接表），订单.customer_id = 客户.customer_id。"""
    return {"id": "lk-placed", "linkTypeId": "lt-placed", "relationType": "placed_by",
            "srcDatasetId": "orders-dataset", "tgtDatasetId": "customers-dataset",
            "srcKey": "customer_id", "tgtKey": "customer_id", "edgeDatasetId": None,
            "status": "active", "fieldMapping": {"__binding_mode__": "remote"},
            "bindingMode": "remote", **changes}


_REJECTIONS = [
    ("no mapping", dict(link_mappings=[]), "remote_relation_unmapped"),
    ("edge dataset",
     dict(link_mappings=[_link_mapping(edgeDatasetId="orders-edge")]),
     "remote_relation_unmapped"),
    ("duplicate mappings",
     dict(link_mappings=[_link_mapping(), _link_mapping(id="lk-placed-2")]),
     "remote_relation_unmapped"),
    ("local-mode mapping",
     dict(link_mappings=[_link_mapping(bindingMode="local", fieldMapping={})]),
     "remote_relation_unmapped"),
    ("key column unmapped",
     dict(link_mappings=[_link_mapping(srcKey="ghost_col")]),
     "remote_relation_unmapped"),
    ("mixed remote/local", dict(target_remote=False), "remote_relation_unsupported"),
    ("invalid cardinality", dict(cardinality="many"), "invalid_parameter"),
]
_CASE_IDS = [case[0] for case in _REJECTIONS]


def _publish(db, user, *, link_mappings=None, cardinality="many-to-one",
             target_remote=True, mappings=()):
    ontology_id, release_id = str(uuid4()), str(uuid4())
    snapshot = complete_snapshot({
        "objectTypes": [_ORDER_TYPE, _CUSTOMER_TYPE],
        "linkTypes": [dict(_LINK, cardinality=cardinality)],
        "mappings": [_ORDER_MAPPING,
                     _CUSTOMER_MAPPING if target_remote else _CUSTOMER_LOCAL, *mappings],
        "linkMappings": [_link_mapping()] if link_mappings is None else link_mappings,
    })
    project = OntologyProject(id=ontology_id, name=f"销售-{ontology_id[:8]}",
                              domain="test", created_by=user.id)
    db.add(project)
    db.flush()
    release = OntologyVersion(id=release_id, ontology_id=ontology_id, version_number="v0",
                              node_kind="release", lifecycle_status="released", revision=0,
                              snapshot_formal=snapshot, snapshot_hash=snapshot_hash(snapshot),
                              created_by=user.id)
    profile = AgentProfile(ontology_id=ontology_id, enabled=True, max_rows_per_query=50)
    db.add_all([release, profile])
    db.flush()
    project.current_release_id = release_id
    db.commit()
    return SimpleNamespace(ontology_id=ontology_id, release_id=release_id, profile=profile)


_TYPE_OF = {"o": "orders-type", "c": "customers-type"}


def _request(order_model, customer_model=None, *, direction="source_to_target",
             left="o", right="c"):
    """o=订单（链接源端），c=客户（链接目标端）；默认 o → c 正向声明关系。"""
    identity = {"o": order_model, "c": customer_model or order_model}
    return QueryRequest.model_validate({
        "sources": [{"alias": alias, "ontologyId": identity[alias].ontology_id,
                     "releaseId": identity[alias].release_id,
                     "objectTypeId": _TYPE_OF[alias]}
                    for alias in (left, right)],
        "joins": [{"left": left, "right": right, "type": "inner", "on": {
            "kind": "declared_relation", "linkTypeId": "lt-placed",
            "direction": direction}}],
        "select": [{"source": "o", "property": "key", "as": "order"},
                   {"source": "c", "property": "name", "as": "customer"}]})


def _scope(db, model):
    _, _, scope = build_scope(db, model.ontology_id, release_id=model.release_id)
    return scope


def _offline_keys(scope, *, reverse=False, link="lt-placed",
                  left=None, right=None):
    ends = ("customers-type", "orders-type") if reverse else ("orders-type", "customers-type")
    return remote_query.declared_relation_keys(
        scope.release.snapshot, scope, link, left or ends[0], right or ends[1], reverse=reverse)


def _forbidden(*args, **kwargs):
    raise AssertionError("离线键解析不得触达数据源验证端口或查询端口")


# ------------------------------------------------- 执行路径（治理查询计划）


def test_plan_joins_on_published_direct_keys(db, admin_user):
    model = _publish(db, admin_user)
    plan = remote_query.prepare_remote_plan(db, admin_user, _request(model))
    assert plan["joins"] == [{
        "left": "o", "right": "c", "type": "inner",
        "leftProperty": "customer", "rightProperty": "key",
        "cardinality": "N:1", "keyType": None,
        "evidence": {"categories": ["published_remote_relation"],
                     "verifiedRefs": [f"relation:{model.release_id}:lt-placed"]}}]
    assert [source["binding"]["kind"] for source in plan["sources"]] == [
        "integrated", "integrated"]
    assert not db.in_transaction()


def test_reverse_direction_swaps_keys_and_cardinality(db, admin_user):
    model = _publish(db, admin_user)
    request = _request(model, direction="target_to_source", left="c", right="o")
    join = remote_query.prepare_remote_plan(db, admin_user, request)["joins"][0]
    assert (join["left"], join["right"]) == ("c", "o")
    assert (join["leftProperty"], join["rightProperty"]) == ("key", "customer")
    assert join["cardinality"] == "1:N"


@pytest.mark.parametrize("name,options,code", _REJECTIONS, ids=_CASE_IDS)
def test_plan_rejects_unresolvable_declared_relation(
        db, admin_user, name, options, code):
    model = _publish(db, admin_user, **options)
    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(db, admin_user, _request(model))
    assert (error.value.status, error.value.code) == (422, code)
    assert not db.in_transaction()


def test_plan_rejects_endpoints_that_do_not_match_the_link_type(db, admin_user):
    model = _publish(db, admin_user)
    # 左侧是链接目标端却声明 source_to_target。
    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(db, admin_user, _request(model, left="c", right="o"))
    assert (error.value.status, error.value.code) == (422, "invalid_parameter")


def test_plan_rejects_declared_relation_across_ontologies(db, admin_user):
    request = _request(_publish(db, admin_user), _publish(db, admin_user))
    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(db, admin_user, request)
    assert (error.value.status, error.value.code) == (422, "invalid_parameter")


def test_unauthorized_link_type_is_capability_forbidden_at_execution(db, admin_user):
    model = _publish(db, admin_user)
    model.profile.allowed_link_type_ids = []
    db.commit()
    # 计划编制阶段保持原样上抛 ToolError，由执行边界归一（既有行为）。
    with pytest.raises(ToolError):
        remote_query.prepare_remote_plan(db, admin_user, _request(model))
    with pytest.raises(QueryError) as error:
        remote_query.execute_remote_query(
            db, admin_user, _request(model), session_kind="direct",
            session_id=admin_user.id)
    assert (error.value.status, error.value.code) == (403, "capability_forbidden")


def test_plan_keeps_property_authorization_on_join_keys(db, admin_user):
    model = _publish(db, admin_user)
    # 关联键 orders.customer 不在属性白名单内：拿到键之后仍由属性授权拒绝。
    model.profile.allowed_properties = {"orders-type": ["key", "note"],
                                        "customers-type": ["key", "name"]}
    db.commit()
    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(db, admin_user, _request(model))
    assert (error.value.status, error.value.code) == (403, "property_forbidden")


def test_execution_path_rechecks_release_pointer(db, admin_user):
    """执行路径对发布指针比较读；离线入口只认调用方给的快照，不做 CAS。"""
    model = _publish(db, admin_user)
    scope = _scope(db, model)
    # 发布指针前移到一条没有关系映射的新 release。
    release_id = str(uuid4())
    snapshot = complete_snapshot({
        "objectTypes": [_ORDER_TYPE, _CUSTOMER_TYPE], "linkTypes": [_LINK],
        "mappings": [_ORDER_MAPPING, _CUSTOMER_MAPPING], "linkMappings": []})
    db.add(OntologyVersion(id=release_id, ontology_id=model.ontology_id,
                           version_number="v1", node_kind="release",
                           lifecycle_status="released", revision=0,
                           snapshot_formal=snapshot, snapshot_hash=snapshot_hash(snapshot),
                           created_by=admin_user.id))
    db.flush()
    db.query(OntologyProject).filter_by(id=model.ontology_id).one().current_release_id = release_id
    db.commit()

    assert _offline_keys(scope) == ("customer", "key")
    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(db, admin_user, _request(model))
    assert (error.value.status, error.value.code) == (409, "release_context_changed")


# ------------------------------------------------------- 离线入口（不查 DB）


@pytest.mark.parametrize("forward", [True, False], ids=["forward", "reverse"])
def test_offline_keys_equal_governance_plan_keys(db, admin_user, forward):
    model = _publish(db, admin_user)
    request = (_request(model) if forward else
               _request(model, direction="target_to_source", left="c", right="o"))
    join = remote_query.prepare_remote_plan(db, admin_user, request)["joins"][0]
    assert _offline_keys(_scope(db, model), reverse=not forward) == (
        join["leftProperty"], join["rightProperty"])


def test_offline_keys_never_touch_the_database(db, admin_user, monkeypatch):
    model = _publish(db, admin_user)
    scope = _scope(db, model)
    monkeypatch.setattr(remote_binding, "_validator", SimpleNamespace(
        accepts=_forbidden, sample=_forbidden))
    monkeypatch.setattr(remote_query, "_port", SimpleNamespace(
        execute=_forbidden, fetch=_forbidden, cancel=_forbidden))
    in_transaction = db.in_transaction()
    with QueryCounter(db) as counter:
        keys = [_offline_keys(scope, reverse=reverse) for reverse in (False, True)]
    assert keys == [("customer", "key"), ("key", "customer")]
    assert counter.count == 0
    assert db.in_transaction() == in_transaction


@pytest.mark.parametrize("name,options,code", _REJECTIONS, ids=_CASE_IDS)
def test_offline_keys_reject_with_the_governance_codes(
        db, admin_user, name, options, code):
    model = _publish(db, admin_user, **options)
    with pytest.raises(QueryError) as error:
        _offline_keys(_scope(db, model))
    assert (error.value.status, error.value.code) == (422, code)


def test_offline_keys_reject_mismatched_endpoints(db, admin_user):
    with pytest.raises(QueryError) as error:
        _offline_keys(_scope(db, _publish(db, admin_user)),
                      left="customers-type", right="orders-type")
    assert (error.value.status, error.value.code) == (422, "invalid_parameter")


def test_offline_keys_do_not_guess_an_ambiguous_remote_binding(db, admin_user):
    model = _publish(db, admin_user, mappings=[
        dict(_CUSTOMER_MAPPING, id="customers-mapping-2")])
    with pytest.raises(QueryError) as error:
        _offline_keys(_scope(db, model))
    assert (error.value.status, error.value.code) == (422, "remote_binding_ambiguous")


def test_offline_keys_require_an_authorized_link_type(db, admin_user):
    model = _publish(db, admin_user)
    scope = _scope(db, model)
    with pytest.raises(QueryError) as error:
        _offline_keys(scope, link="lt-unknown")
    assert (error.value.status, error.value.code) == (403, "capability_forbidden")
    model.profile.allowed_link_type_ids = []
    db.commit()
    with pytest.raises(QueryError) as error:
        _offline_keys(_scope(db, model))
    assert (error.value.status, error.value.code) == (403, "capability_forbidden")


def test_offline_keys_require_a_pinned_release(db, admin_user):
    model = _publish(db, admin_user)
    _, _, scope = build_scope(db, model.ontology_id)
    assert scope.release is None
    with pytest.raises(QueryError) as error:
        remote_query.declared_relation_keys(
            {}, scope, "lt-placed", "orders-type", "customers-type")
    assert (error.value.status, error.value.code) == (422, "invalid_parameter")
