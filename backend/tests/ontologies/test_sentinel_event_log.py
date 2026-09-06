"""Sentinel CEP 事件日志（M1）：采集、查询、裁剪与 changed_within/prev 算子。

覆盖架构评审确定的正确性契约：
- 同事务原子写入 + 多 flush 合并（最早 old / 最新 new / created 优先）；
- 回滚不落事实；无 release 归属的投影不落事实；
- 发布切换窗口内的投影重建标记 release_activation（不参与时间算子）；
- changed_within/prev 只认 organic 事件，时间基准 UTC；
- 时间算子形态非法 → 整轮观察 error（fail-closed 可见）；
- filter 中使用时间算子被发布门禁直接拒绝。
"""
from datetime import datetime, timedelta, timezone

from app.models.ontology import OntologyProject
from app.models.ontology_formal import ObjectInstance, ObjectType
from app.models.sentinel import Sentinel, SentinelEventLog
from app.ontologies.sentinels import cdc
from app.ontologies.sentinels import evaluator
from app.ontologies.sentinels.cep import contract as cep_contract
from app.ontologies.sentinels.cep import event_store
from app.ontologies.sentinels.validation import validate_sentinels


RELEASE_ID = "rel-cep-1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _project(db, ontology_id: str) -> None:
    db.add(OntologyProject(
        id=ontology_id,
        name=ontology_id,
        domain="sentinel-cep-tests",
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


def _instance(ontology_id: str, object_type_id: str, instance_id: str,
              properties: dict) -> ObjectInstance:
    return ObjectInstance(
        id=instance_id,
        ontology_id=ontology_id,
        ontology_release_id=RELEASE_ID,
        object_type_id=object_type_id,
        properties=properties,
    )


def _sentinel(ontology_id: str, sentinel_id: str, bindings: list[dict],
              *, condition: str = "True") -> Sentinel:
    return Sentinel(
        id=sentinel_id,
        ontology_id=ontology_id,
        name=sentinel_id,
        display_name=sentinel_id,
        bindings=bindings,
        links=[],
        condition=condition,
        primary_alias=bindings[0]["alias"],
        action_ids=[],
        action_parameters={},
        trigger_mode="on_enter",
        on_change=False,
        on_schedule=False,
        muted=False,
        enabled=True,
        status="published",
    )


def _events(db, instance_id: str) -> list[SentinelEventLog]:
    return db.query(SentinelEventLog).filter(
        SentinelEventLog.instance_id == instance_id,
    ).order_by(SentinelEventLog.key, SentinelEventLog.id).all()


# ---------------------------------------------------------------------------
# 采集：CDC 同事务钩子
# ---------------------------------------------------------------------------

def test_created_instance_writes_key_level_rows(db):
    cdc.register_cdc(start_worker=False)
    ontology_id = "cep-capture-created"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "device")
    db.add(_instance(
        ontology_id, object_type.id, "device-1",
        {"temp": 42, "humidity": 30}))
    db.commit()

    rows = _events(db, "device-1")
    assert sorted((r.key, r.change_kind) for r in rows) == [
        ("humidity", "created"), ("temp", "created")]
    assert all(r.old_value is None for r in rows)
    by_key = {r.key: r for r in rows}
    assert by_key["temp"].new_value == 42
    assert by_key["temp"].source == cep_contract.EVENT_SOURCE_ORGANIC
    assert by_key["temp"].ontology_release_id == RELEASE_ID


def test_updated_instance_records_old_to_new(db):
    cdc.register_cdc(start_worker=False)
    ontology_id = "cep-capture-updated"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "device")
    instance = _instance(
        ontology_id, object_type.id, "device-2", {"status": "submitted"})
    db.add(instance)
    db.commit()

    # 提交会过期属性；重新加载让 history 拿到已提交基线（与生产更新
    # 路径"加载-修改-提交"一致）。
    db.refresh(instance)
    instance.properties = {"status": "approved"}
    db.commit()

    rows = _events(db, "device-2")
    updates = [r for r in rows if r.change_kind == "updated"]
    assert len(updates) == 1
    assert updates[0].key == "status"
    assert updates[0].old_value == "submitted"
    assert updates[0].new_value == "approved"


def test_deleted_instance_writes_marker_row(db):
    cdc.register_cdc(start_worker=False)
    ontology_id = "cep-capture-deleted"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "device")
    instance = _instance(
        ontology_id, object_type.id, "device-3", {"temp": 1})
    db.add(instance)
    db.commit()

    db.delete(instance)
    db.commit()

    rows = _events(db, "device-3")
    assert any(
        r.change_kind == "deleted"
        and r.key == cep_contract.DELETED_EVENT_KEY for r in rows)


def test_multi_flush_merges_earliest_old_and_latest_new(db):
    cdc.register_cdc(start_worker=False)
    ontology_id = "cep-capture-merge"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "device")
    instance = _instance(
        ontology_id, object_type.id, "device-4", {"status": "a"})
    db.add(instance)
    db.flush()
    instance.properties = {"status": "b", "extra": 1}
    db.flush()
    instance.properties = {"status": "c", "extra": 1}
    db.commit()

    rows = _events(db, "device-4")
    status_rows = [r for r in rows if r.key == "status"]
    assert len(status_rows) == 1, "同事务多 flush 必须合并为一行"
    row = status_rows[0]
    # 最早 old（创建时刻 None）+ 最新 new；change_kind 保持 created。
    assert row.old_value is None
    assert row.new_value == "c"
    assert row.change_kind == "created"


def test_rollback_discards_event_rows(db):
    cdc.register_cdc(start_worker=False)
    ontology_id = "cep-capture-rollback"
    _project(db, ontology_id)
    db.commit()
    object_type = _object_type(db, ontology_id, "device")
    db.add(_instance(ontology_id, object_type.id, "device-5", {"temp": 5}))
    db.flush()
    db.rollback()

    assert _events(db, "device-5") == []


def test_release_switch_window_marks_activation_source(db):
    cdc.register_cdc(start_worker=False)
    ontology_id = "cep-capture-activation"
    _project(db, ontology_id)
    db.commit()
    object_type = _object_type(db, ontology_id, "device")
    db.info.setdefault(
        cdc._RELEASE_SWITCH_SCOPES_KEY, set()).add(ontology_id)
    db.add(_instance(ontology_id, object_type.id, "device-6", {"temp": 6}))
    db.commit()

    rows = _events(db, "device-6")
    assert rows
    assert all(
        r.source == cep_contract.EVENT_SOURCE_RELEASE_ACTIVATION
        for r in rows)


def test_instances_without_release_lineage_are_not_logged(db):
    cdc.register_cdc(start_worker=False)
    ontology_id = "cep-capture-no-release"
    _project(db, ontology_id)
    db.commit()
    object_type = _object_type(db, ontology_id, "device")
    db.add(ObjectInstance(
        id="device-7",
        ontology_id=ontology_id,
        object_type_id=object_type.id,
        properties={"temp": 7},
    ))
    db.commit()

    assert _events(db, "device-7") == []


# ---------------------------------------------------------------------------
# 查询：时间算子预取与幂等定序
# ---------------------------------------------------------------------------

def _seed_event(db, ontology_id: str, object_type_id: str, instance_id: str,
                key: str, old, new, *, occurred_at=None,
                source=cep_contract.EVENT_SOURCE_ORGANIC) -> SentinelEventLog:
    row = SentinelEventLog(
        ontology_id=ontology_id,
        ontology_release_id=RELEASE_ID,
        object_type_id=object_type_id,
        instance_id=instance_id,
        change_kind="updated",
        key=key,
        old_value=old,
        new_value=new,
        source=source,
        occurred_at=occurred_at or _now(),
    )
    db.add(row)
    return row


def test_recent_change_ignores_activation_events_and_old_events(db):
    ontology_id = "cep-query-recent"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "device")
    db.commit()
    _seed_event(db, ontology_id, object_type.id, "d-1", "temp", 1, 2)
    _seed_event(
        db, ontology_id, object_type.id, "d-2", "temp", 1, 2,
        occurred_at=_now() - timedelta(hours=2))
    _seed_event(
        db, ontology_id, object_type.id, "d-3", "temp", 1, 2,
        source=cep_contract.EVENT_SOURCE_RELEASE_ACTIVATION)
    db.commit()

    matched = event_store.instances_with_recent_change(
        db, {"d-1", "d-2", "d-3"}, "temp", _now() - timedelta(minutes=5))
    assert matched == {"d-1"}


def test_previous_value_takes_latest_by_monotonic_id(db):
    ontology_id = "cep-query-prev"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "device")
    db.commit()
    _seed_event(db, ontology_id, object_type.id, "d-9", "status", "a", "b")
    _seed_event(db, ontology_id, object_type.id, "d-9", "status", "b", "c")
    db.commit()

    values = event_store.latest_previous_values(db, {"d-9"}, {"status"})
    assert values[("d-9", "status")] == "b"


# ---------------------------------------------------------------------------
# 评估：changed_within / prev 算子语义
# ---------------------------------------------------------------------------

def test_changed_within_only_fires_for_recent_changes(db):
    ontology_id = "cep-eval-changed-within"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "device")
    object_type.properties = [
        {"id": "id", "name": "id", "type": "string", "required": True},
        {"id": "temp", "name": "temp", "type": "number"},
        {"id": "humidity", "name": "humidity", "type": "number"},
    ]
    # 实例不带 release 血缘：本测试的事件事实全部由 _seed_event 显式
    # 播种（不依赖 CDC 监听器是否已注册，创建时间也可控）。
    fresh = ObjectInstance(
        id="fresh-1", ontology_id=ontology_id, object_type_id=object_type.id,
        properties={"id": "fresh-1", "temp": 90, "humidity": 25})
    stale = ObjectInstance(
        id="stale-1", ontology_id=ontology_id, object_type_id=object_type.id,
        properties={"id": "stale-1", "temp": 90, "humidity": 25})
    sentinel = _sentinel(
        ontology_id, "cep-changed-within",
        [{"alias": "a", "objectTypeId": object_type.id}],
        condition=(
            "a.temp > 80 and a.humidity < 30 "
            "and changed_within('a.temp', 300) "
            "and changed_within('a.humidity', 300)"))
    db.add_all([fresh, stale, sentinel])
    db.commit()
    _seed_event(db, ontology_id, object_type.id, "fresh-1", "temp", 70, 90)
    _seed_event(db, ontology_id, object_type.id, "fresh-1", "humidity", 40, 25)
    _seed_event(
        db, ontology_id, object_type.id, "stale-1", "temp", 70, 90,
        occurred_at=_now() - timedelta(hours=3))
    _seed_event(
        db, ontology_id, object_type.id, "stale-1", "humidity", 40, 25,
        occurred_at=_now() - timedelta(hours=3))
    db.commit()

    firing = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "manual")

    # stale-1 的两个属性都是 3 小时前变更的——时间对齐失败，不得进入命中集。
    assert firing.status in {"fired", "no_change", "skipped"}
    assert firing.match_count == 1
    assert firing.matches == [{"a": "fresh-1"}]
    assert not firing.error


def test_prev_detects_state_transition(db):
    ontology_id = "cep-eval-prev"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "order")
    object_type.properties = [
        {"id": "id", "name": "id", "type": "string", "required": True},
        {"id": "status", "name": "status", "type": "string"},
        {"id": "amount", "name": "amount", "type": "number"},
    ]
    transitioned = _instance(
        ontology_id, object_type.id, "order-1",
        {"id": "order-1", "status": "approved", "amount": 100})
    untouched = _instance(
        ontology_id, object_type.id, "order-2",
        {"id": "order-2", "status": "approved", "amount": 100})
    sentinel = _sentinel(
        ontology_id, "cep-prev-transition",
        [{"alias": "a", "objectTypeId": object_type.id}],
        condition=(
            "prev('a.status') == 'submitted' and a.status == 'approved'"))
    db.add_all([transitioned, untouched, sentinel])
    db.commit()
    _seed_event(
        db, ontology_id, object_type.id, "order-1", "status",
        "submitted", "approved")
    db.commit()

    firing = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "manual")

    # order-2 无历史事件 → prev() 为 None → 条件判否（fail-closed）。
    assert firing.match_count == 1
    assert firing.matches == [{"a": "order-1"}]


def test_malformed_temporal_expression_fails_whole_observation(db):
    ontology_id = "cep-eval-malformed"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "device")
    db.add(_instance(ontology_id, object_type.id, "device-x", {"temp": 1}))
    sentinel = _sentinel(
        ontology_id, "cep-malformed",
        [{"alias": "a", "objectTypeId": object_type.id}],
        # 非字面量窗口 → 静态不可提取 → 整轮观察必须作废。
        condition="changed_within('a.temp', 60 + dynamic)")
    db.add_all([sentinel])
    db.commit()

    firing = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "manual")

    assert firing.status == "error"
    assert "changed_within" in (firing.error or "")


# ---------------------------------------------------------------------------
# 发布门禁：时间算子静态校验
# ---------------------------------------------------------------------------

def _models_for(object_type: ObjectType):
    from types import SimpleNamespace
    return (
        [SimpleNamespace(
            id=object_type.id, ontology_id=object_type.ontology_id,
            properties=object_type.properties)],
        [], [],
    )


def test_validation_accepts_wellformed_temporal_condition(db):
    ontology_id = "cep-validate-ok"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "device")
    object_type.properties = [
        {"id": "temp", "name": "temp", "type": "number"}]
    db.commit()
    sentinel = _sentinel(
        ontology_id, "cep-validate-ok-sentinel",
        [{"alias": "a", "objectTypeId": object_type.id}],
        condition="a.temp > 80 and changed_within('a.temp', 300)")
    db.add(sentinel)
    db.commit()

    object_types, link_types, actions = _models_for(object_type)
    errors = validate_sentinels([sentinel], object_types, link_types, actions)

    assert errors == [], [e.get("message") for e in errors]


def test_validation_rejects_temporal_misuse(db):
    ontology_id = "cep-validate-bad"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "device")
    object_type.properties = [
        {"id": "temp", "name": "temp", "type": "number"},
        {"id": "status", "name": "status", "type": "string"},
    ]
    db.commit()

    cases = {
        "sentinel_temporal_in_filter_forbidden": _sentinel(
            ontology_id, "cep-bad-filter",
            [{"alias": "a", "objectTypeId": object_type.id,
              "filter": "changed_within('a.temp', 300)"}],
            condition="a.temp > 0"),
        "sentinel_temporal_property_not_found": _sentinel(
            ontology_id, "cep-bad-prop",
            [{"alias": "a", "objectTypeId": object_type.id}],
            condition="changed_within('a.missing', 300)"),
        "sentinel_temporal_alias_not_found": _sentinel(
            ontology_id, "cep-bad-alias",
            [{"alias": "a", "objectTypeId": object_type.id}],
            condition="prev('b.status') == 'x'"),
        "sentinel_temporal_window_out_of_range": _sentinel(
            ontology_id, "cep-bad-window",
            [{"alias": "a", "objectTypeId": object_type.id}],
            condition="changed_within('a.temp', 5)"),
        "sentinel_temporal_expression_invalid": _sentinel(
            ontology_id, "cep-bad-shape",
            [{"alias": "a", "objectTypeId": object_type.id}],
            condition="changed_within(a.temp, 300)"),
    }
    object_types, link_types, actions = _models_for(object_type)
    for expected_code, sentinel in cases.items():
        db.add(sentinel)
        db.commit()
        errors = validate_sentinels(
            [sentinel], object_types, link_types, actions)
        codes = {error.get("code") for error in errors}
        assert expected_code in codes, (
            expected_code, codes, sentinel.condition, sentinel.bindings)
        db.delete(sentinel)
        db.commit()


# ---------------------------------------------------------------------------
# 裁剪：固定保留期
# ---------------------------------------------------------------------------

def test_prune_event_log_removes_rows_beyond_retention(db, monkeypatch):
    ontology_id = "cep-prune"
    _project(db, ontology_id)
    object_type = _object_type(db, ontology_id, "device")
    db.commit()
    _seed_event(
        db, ontology_id, object_type.id, "old-1", "temp", 1, 2,
        occurred_at=_now() - timedelta(days=9))
    _seed_event(db, ontology_id, object_type.id, "new-1", "temp", 1, 2)
    db.commit()

    deleted = event_store.prune_event_log(session_factory=lambda: db)

    assert deleted == 1
    remaining = db.query(SentinelEventLog).filter(
        SentinelEventLog.instance_id == "new-1").count()
    assert remaining == 1

def test_promotion_flush_order_relables_events_as_activation(db):
    """P0-1 回归：晋级先重建投影（flush 1）后切指针（flush 2）。

    捕获发生在指针切换前，靠 _merge_pointer_switch_deltas 的回溯改标
    保证激活窗口事件不按 organic 参与时间算子/模式推进。
    """
    cdc.register_cdc(start_worker=False)
    ontology_id = "cep-promotion-order"
    db.add(OntologyProject(
        id=ontology_id, name=ontology_id, domain="d", created_by="t",
        status="published", version="v1", current_release_id="rel-old"))
    db.commit()
    object_type = _object_type(db, ontology_id, "device")
    db.commit()

    # flush 1：新 release 投影物化（实例重建，此时 scope 尚未设置）。
    db.add(_instance(ontology_id, object_type.id, "device-promo",
                     {"temp": 1}))
    db.flush()
    # flush 2：指针切换。
    project = db.query(OntologyProject).filter_by(id=ontology_id).one()
    db.refresh(project)
    project.current_release_id = RELEASE_ID
    db.commit()

    rows = _events(db, "device-promo")
    assert rows, "晋级窗口的实例重建事件必须被捕获"
    assert all(
        row.source == cep_contract.EVENT_SOURCE_RELEASE_ACTIVATION
        for row in rows), [row.source for row in rows]
