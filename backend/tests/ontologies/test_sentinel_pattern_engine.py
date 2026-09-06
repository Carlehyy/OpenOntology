"""Sentinel CEP 模式哨兵（M2）：序列/缺失/聚合/水位幂等/发布切换防护。

直调 evaluator.evaluate_sentinel（模式分派入口），事件事实直接播种到
sentinel_event_log——这同时验证"水位消费独立于 outbox"的关键设计：
没有 outbox 事件也能推进（编辑器保存路径的等价物）。
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models.ontology import OntologyProject
from app.models.ontology_formal import (
    LinkInstance,
    LinkType,
    ObjectInstance,
    ObjectType,
)
from app.models.sentinel import (
    Sentinel,
    SentinelEventLog,
    SentinelFiring,
    SentinelMatchState,
    SentinelPatternState,
)
from app.ontologies.sentinels import evaluator
from app.ontologies.sentinels.cep import contract as cep_contract
from app.ontologies.sentinels.validation import validate_sentinels


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _project(db, ontology_id: str) -> None:
    db.add(OntologyProject(
        id=ontology_id,
        name=ontology_id,
        domain="sentinel-cep-pattern-tests",
        created_by="tests",
        status="published",
        version="v1.0.0",
    ))


def _object_type(db, ontology_id: str, type_id: str,
                  properties: list[dict]) -> ObjectType:
    row = ObjectType(
        id=type_id, ontology_id=ontology_id, name=type_id,
        display_name=type_id, primary_key="id", properties=properties)
    db.add(row)
    return row


def _instance(ontology_id: str, type_id: str, instance_id: str,
              properties: dict) -> ObjectInstance:
    return ObjectInstance(
        id=instance_id, ontology_id=ontology_id, object_type_id=type_id,
        properties=properties)


def _pattern_sentinel(ontology_id: str, sentinel_id: str, pattern: dict,
                      *, bindings: list[dict] | None = None,
                      links: list[dict] | None = None,
                      scan_interval: int = 60,
                      muted: bool = False) -> Sentinel:
    stages = pattern.get("stages") or []
    return Sentinel(
        id=sentinel_id,
        ontology_id=ontology_id,
        name=sentinel_id,
        display_name=sentinel_id,
        bindings=bindings if bindings is not None else [
            {"alias": stage["alias"], "objectTypeId": stage["objectTypeId"]}
            for stage in stages
        ],
        links=links or [],
        condition=None,
        pattern=pattern,
        primary_alias=stages[0]["alias"] if stages else None,
        action_ids=[],
        action_parameters={},
        trigger_mode=cep_contract.PATTERN_TRIGGER_MODE,
        on_change=True,
        on_schedule=True,
        scan_interval_seconds=scan_interval,
        last_scanned_at=None,
        muted=muted,
        enabled=True,
        status="published",
    )


def _seed_event(db, ontology_id: str, type_id: str, instance_id: str,
                key: str, old, new, *, occurred_at=None,
                source=cep_contract.EVENT_SOURCE_ORGANIC) -> SentinelEventLog:
    row = SentinelEventLog(
        ontology_id=ontology_id,
        ontology_release_id=None,
        object_type_id=type_id,
        instance_id=instance_id,
        change_kind="updated",
        key=key,
        old_value=old,
        new_value=new,
        source=source,
        occurred_at=occurred_at or _now(),
    )
    db.add(row)
    db.commit()
    return row


SEQUENCE_PATTERN = {
    "stages": [
        {"alias": "a", "objectTypeId": "order",
         "filter": "a.status == 'submitted'"},
        {"alias": "b", "objectTypeId": "order",
         "filter": "b.status == 'approved'"},
    ],
    "absence": {"enabled": False},
    "within": 3600,
}


def _order_type_props():
    return [
        {"id": "id", "name": "id", "type": "string", "required": True},
        {"id": "status", "name": "status", "type": "string"},
        {"id": "amount", "name": "amount", "type": "number"},
    ]


# ---------------------------------------------------------------------------
# 序列模式（同实例状态变迁）
# ---------------------------------------------------------------------------

def test_sequence_transitions_fire_once_and_watermark_is_idempotent(db):
    ontology_id = "cep-pattern-sequence"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "order", _order_type_props())
    order = _instance(
        ontology_id, "order", "order-1",
        {"id": "order-1", "status": "approved", "amount": 500})
    sentinel = _pattern_sentinel(
        ontology_id, "seq-sentinel", SEQUENCE_PATTERN)
    db.add_all([order, sentinel])
    db.commit()

    _seed_event(db, ontology_id, "order", "order-1", "status",
                "draft", "submitted", occurred_at=_now() - timedelta(minutes=5))
    first = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert first.status == "no_match"
    assert db.query(SentinelPatternState).filter_by(
        sentinel_id=sentinel.id).count() == 1

    _seed_event(db, ontology_id, "order", "order-1", "status",
                "submitted", "approved", occurred_at=_now() - timedelta(minutes=1))
    second = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert second.status == "fired"
    assert second.match_count == 1
    assert second.matches == [{"a": "order-1", "b": "order-1"}]
    # 完成即清理在途状态；命中结果落 match_state。
    assert db.query(SentinelPatternState).filter_by(
        sentinel_id=sentinel.id).count() == 0
    state = db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id).one()
    assert state.runtime_status == "completed"

    # 水位幂等：无新事件重复评估不再放炮。
    third = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert third.status == "no_match"
    assert db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id).count() == 1
    assert db.query(SentinelFiring).filter_by(
        sentinel_id=sentinel.id).count() == 3


def test_absence_timeout_fires_with_absence_edge(db, monkeypatch):
    ontology_id = "cep-pattern-absence"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "order", _order_type_props())
    order = _instance(ontology_id, "order", "order-2",
                      {"id": "order-2", "status": "submitted", "amount": 10})
    pattern = {
        "stages": [
            {"alias": "a", "objectTypeId": "order",
             "filter": "a.status == 'submitted'"},
            {"alias": "b", "objectTypeId": "order",
             "filter": "b.status == 'approved'"},
        ],
        "absence": {"enabled": True},
        "within": 120,
    }
    sentinel = _pattern_sentinel(ontology_id, "absence-sentinel", pattern)
    db.add_all([order, sentinel])
    db.commit()

    from app.ontologies.sentinels.cep import pattern as cep_pattern
    real_now = _now()
    monkeypatch.setattr(cep_pattern, "_now", lambda: real_now)
    _seed_event(db, ontology_id, "order", "order-2", "status",
                "draft", "submitted", occurred_at=real_now - timedelta(seconds=30))
    first = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "schedule")
    assert first.status == "no_match"

    # 推进受控时钟越过 deadline，下一次唤醒（定时扫描）触发 absence。
    monkeypatch.setattr(
        cep_pattern, "_now", lambda: real_now + timedelta(minutes=10))
    second = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "schedule")
    assert second.status == "fired"
    assert second.match_count == 1
    state = db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id).one()
    event = (state.match_detail or {}).get("__event__") or {}
    assert event.get("edge") == "absence"
    assert db.query(SentinelPatternState).filter_by(
        sentinel_id=sentinel.id).count() == 0


def test_timeout_without_absence_discards_silently(db):
    ontology_id = "cep-pattern-timeout"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "order", _order_type_props())
    order = _instance(ontology_id, "order", "order-3",
                      {"id": "order-3", "status": "submitted", "amount": 10})
    sentinel = _pattern_sentinel(ontology_id, "timeout-sentinel", {
        "stages": SEQUENCE_PATTERN["stages"],
        "absence": {"enabled": False},
        "within": 120,
    })
    db.add_all([order, sentinel])
    db.commit()

    _seed_event(db, ontology_id, "order", "order-3", "status",
                "draft", "submitted", occurred_at=_now() - timedelta(minutes=10))
    evaluator.evaluate_sentinel(db, ontology_id, sentinel, "schedule")
    second = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "schedule")

    assert second.status == "no_match"
    assert db.query(SentinelPatternState).filter_by(
        sentinel_id=sentinel.id).count() == 0
    assert db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id).count() == 0


# ---------------------------------------------------------------------------
# 聚合窗口（count + 滞回）
# ---------------------------------------------------------------------------

AGGREGATE_PATTERN = {
    "stages": [
        {"alias": "a", "objectTypeId": "device",
         "filter": "a.temp > 80"},
    ],
    "aggregate": {
        "property": "temp", "function": "count",
        "window": 300, "threshold": 3, "comparison": "gte",
    },
}


def test_aggregate_count_fires_once_with_hysteresis(db):
    ontology_id = "cep-pattern-aggregate"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "device", [
        {"id": "id", "name": "id", "type": "string", "required": True},
        {"id": "temp", "name": "temp", "type": "number"},
    ])
    device = _instance(ontology_id, "device", "device-1",
                       {"id": "device-1", "temp": 95})
    sentinel = _pattern_sentinel(
        ontology_id, "agg-sentinel", AGGREGATE_PATTERN, scan_interval=60)
    db.add_all([device, sentinel])
    db.commit()

    _seed_event(db, ontology_id, "device", "device-1", "temp", 70, 85)
    first = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert first.status == "no_match"

    _seed_event(db, ontology_id, "device", "device-1", "temp", 85, 90)
    _seed_event(db, ontology_id, "device", "device-1", "temp", 90, 88)
    second = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert second.status == "fired"
    assert second.match_count == 1

    # 滞回：窗口内继续追加满足条件的事件不得重复放炮。
    _seed_event(db, ontology_id, "device", "device-1", "temp", 88, 92)
    third = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert third.status == "no_match"
    assert db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id).count() == 1


def test_aggregate_hysteresis_drop_clears_match_state(db):
    ontology_id = "cep-pattern-aggregate-drop"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "device", [
        {"id": "temp", "name": "temp", "type": "number"},
    ])
    device = _instance(ontology_id, "device", "device-2",
                       {"id": "device-2", "temp": 70})
    sentinel = _pattern_sentinel(
        ontology_id, "agg-drop-sentinel", AGGREGATE_PATTERN, scan_interval=60)
    db.add_all([device, sentinel])
    db.commit()

    now = _now()
    for old, new in ((70, 85), (85, 90), (90, 88)):
        _seed_event(db, ontology_id, "device", "device-2", "temp", old, new,
                    occurred_at=now - timedelta(seconds=400))
    evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id).count() == 1

    # 三个超标读数都滑出窗口，新读数正常 → 回落清理命中行。
    _seed_event(db, ontology_id, "device", "device-2", "temp", 88, 70,
                occurred_at=now)
    evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id).count() == 0


# ---------------------------------------------------------------------------
# 跨对象序列（links 关联）
# ---------------------------------------------------------------------------

def test_cross_object_sequence_requires_link(db):
    ontology_id = "cep-pattern-cross"
    _project(db, ontology_id)
    order_type = _object_type(db, ontology_id, "order", _order_type_props())
    pay_type = _object_type(db, ontology_id, "payment", [
        {"id": "amount", "name": "amount", "type": "number"},
    ])
    db.add(LinkType(
        id="lt-pay", ontology_id=ontology_id, name="pay",
        display_name="pay",
        source_object_type_id=order_type.id,
        target_object_type_id=pay_type.id))
    db.commit()
    order = _instance(ontology_id, "order", "order-9",
                      {"id": "order-9", "status": "submitted", "amount": 100})
    payment = _instance(ontology_id, "payment", "pay-9", {"amount": 100})
    payment_other = _instance(
        ontology_id, "payment", "pay-other", {"amount": 100})
    db.add(LinkInstance(
        id="link-9", ontology_id=ontology_id, link_type_id="lt-pay",
        source_object_id="order-9", target_object_id="pay-9",
        properties={}))
    db.commit()
    pattern = {
        "stages": [
            {"alias": "a", "objectTypeId": "order",
             "filter": "a.status == 'submitted'"},
            {"alias": "b", "objectTypeId": "payment",
             "filter": "b.amount > 0"},
        ],
        "absence": {"enabled": False},
        "within": 3600,
    }
    sentinel = _pattern_sentinel(
        ontology_id, "cross-sentinel", pattern,
        links=[{"from": "a", "linkTypeId": "lt-pay", "to": "b"}])
    db.add_all([order, payment, payment_other, sentinel])
    db.commit()

    _seed_event(db, ontology_id, "order", "order-9", "status",
                "draft", "submitted")
    evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")

    # 无关支付事件：不推进锚为 order-9 的在途状态。
    _seed_event(db, ontology_id, "payment", "pay-other", "amount", 0, 100)
    noise = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert noise.status == "no_match"

    # 关联支付事件：完成模式。
    _seed_event(db, ontology_id, "payment", "pay-9", "amount", 0, 100)
    final = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert final.status == "fired"
    assert final.matches == [{"a": "order-9", "b": "pay-9"}]


# ---------------------------------------------------------------------------
# 事件源与身份防护
# ---------------------------------------------------------------------------

def test_release_activation_events_do_not_advance_pattern(db):
    ontology_id = "cep-pattern-activation"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "order", _order_type_props())
    order = _instance(ontology_id, "order", "order-a",
                      {"id": "order-a", "status": "submitted", "amount": 1})
    sentinel = _pattern_sentinel(ontology_id, "act-sentinel", {
        "stages": SEQUENCE_PATTERN["stages"],
        "absence": {"enabled": False},
        "within": 3600,
    })
    db.add_all([order, sentinel])
    db.commit()

    _seed_event(
        db, ontology_id, "order", "order-a", "status", "draft", "submitted",
        source=cep_contract.EVENT_SOURCE_RELEASE_ACTIVATION)
    firing = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")

    assert firing.status == "no_match"
    assert db.query(SentinelPatternState).filter_by(
        sentinel_id=sentinel.id).count() == 0


def test_pattern_level_condition_filters_completions(db):
    ontology_id = "cep-pattern-condition"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "order", _order_type_props())
    big = _instance(ontology_id, "order", "order-big",
                    {"id": "order-big", "status": "approved", "amount": 2000})
    small = _instance(ontology_id, "order", "order-small",
                      {"id": "order-small", "status": "approved", "amount": 5})
    sentinel = _pattern_sentinel(ontology_id, "cond-sentinel", {
        "stages": SEQUENCE_PATTERN["stages"],
        "absence": {"enabled": False},
        "within": 3600,
        "condition": "a.amount > 1000",
    })
    db.add_all([big, small, sentinel])
    db.commit()

    for order_id in ("order-big", "order-small"):
        _seed_event(db, ontology_id, "order", order_id, "status",
                    "draft", "submitted", occurred_at=_now() - timedelta(minutes=5))
        _seed_event(db, ontology_id, "order", order_id, "status",
                    "submitted", "approved", occurred_at=_now() - timedelta(minutes=1))
    firing = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")

    assert firing.status == "fired"
    assert firing.matches == [{"a": "order-big", "b": "order-big"}]


def test_stale_state_from_old_definition_is_purged(db):
    ontology_id = "cep-pattern-stale"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "order", _order_type_props())
    order = _instance(ontology_id, "order", "order-s",
                      {"id": "order-s", "status": "submitted", "amount": 1})
    sentinel = _pattern_sentinel(ontology_id, "stale-sentinel", {
        "stages": SEQUENCE_PATTERN["stages"],
        "absence": {"enabled": False},
        "within": 3600,
    })
    sentinel.definition_revision = 3
    sentinel.enable_generation = 2
    db.add_all([order, sentinel])
    db.commit()
    # 身份不符的旧在途状态：加载即清理，不得用新定义推进。
    db.add(SentinelPatternState(
        ontology_id=ontology_id, sentinel_id=sentinel.id,
        ontology_release_id=None, definition_revision=1,
        enable_generation=1, correlation_key="order-s", stage_index=1,
        started_at=_now(), stage_entered_at=_now(),
        deadline=_now() + timedelta(hours=1), snapshots={}, status="active"))
    db.commit()

    _seed_event(db, ontology_id, "order", "order-s", "status",
                "submitted", "approved")
    firing = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")

    assert firing.status == "no_match"  # 无新 stage0 事件，不完成
    remaining = db.query(SentinelPatternState).filter(
        SentinelPatternState.sentinel_id == sentinel.id,
        SentinelPatternState.definition_revision == 1,
    ).count()
    assert remaining == 0


def test_invalid_pattern_definition_yields_visible_error(db):
    ontology_id = "cep-pattern-invalid"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "order", _order_type_props())
    sentinel = _pattern_sentinel(ontology_id, "invalid-sentinel", {
        # 多 stage + aggregate：normalize 直接拒绝（聚合只允许单 stage）。
        "stages": SEQUENCE_PATTERN["stages"],
        "aggregate": {
            "property": "temp", "function": "count",
            "window": 300, "threshold": 3},
    })
    db.add(sentinel)
    db.commit()

    firing = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert firing.status == "error"
    assert firing.error and "pattern" in firing.error


def test_muted_pattern_advances_without_actions_or_match_state(db):
    ontology_id = "cep-pattern-muted"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "order", _order_type_props())
    order = _instance(ontology_id, "order", "order-m",
                      {"id": "order-m", "status": "approved", "amount": 1})
    sentinel = _pattern_sentinel(
        ontology_id, "muted-sentinel",
        {"stages": SEQUENCE_PATTERN["stages"], "within": 3600},
        muted=True)
    db.add_all([order, sentinel])
    db.commit()

    _seed_event(db, ontology_id, "order", "order-m", "status",
                "draft", "submitted")
    _seed_event(db, ontology_id, "order", "order-m", "status",
                "submitted", "approved")
    firing = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")

    assert firing.status == "muted"
    assert db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id).count() == 0


def test_single_stage_pattern_fires_per_matching_event(db):
    ontology_id = "cep-pattern-single"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "device", [
        {"id": "temp", "name": "temp", "type": "number"},
    ])
    device = _instance(ontology_id, "device", "device-s",
                       {"id": "device-s", "temp": 90})
    sentinel = _pattern_sentinel(ontology_id, "single-sentinel", {
        "stages": [{"alias": "a", "objectTypeId": "device",
                    "filter": "a.temp > 80"}],
        "within": 3600,
    })
    db.add_all([device, sentinel])
    db.commit()

    _seed_event(db, ontology_id, "device", "device-s", "temp", 70, 90)
    first = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert first.status == "fired"

    _seed_event(db, ontology_id, "device", "device-s", "temp", 90, 85)
    second = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "change")
    assert second.status == "fired"
    # 每个满足条件的事件独立锚定 match_key（事件时刻），互不吸收。
    keys = {row.match_key for row in db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id).all()}
    assert len(keys) == 2


# ---------------------------------------------------------------------------
# 发布门禁：pattern 深度校验
# ---------------------------------------------------------------------------

def _gate_codes(sentinel, object_types, link_types=None, actions=None):
    errors = validate_sentinels(
        [sentinel], object_types, link_types or [], actions or [])
    return {error.get("code") for error in errors}


def test_validation_rejects_pattern_misuse(db):
    ontology_id = "cep-pattern-validate"
    _project(db, ontology_id)
    order_type = _object_type(db, ontology_id, "order", _order_type_props())
    db.commit()
    object_types = [SimpleNamespace(
        id=order_type.id, ontology_id=ontology_id,
        properties=order_type.properties)]

    # 1) stages 与 bindings 不镜像
    sentinel = _pattern_sentinel(ontology_id, "v-mismatch", {
        "stages": SEQUENCE_PATTERN["stages"], "within": 3600,
    }, bindings=[{"alias": "x", "objectTypeId": "order"},
                 {"alias": "y", "objectTypeId": "order"}],
        links=[])
    sentinel.primary_alias = "x"
    codes = _gate_codes(sentinel, object_types)
    assert "sentinel_pattern_bindings_mismatch" in codes, codes

    # 2) 窗口小于扫描间隔
    sentinel2 = _pattern_sentinel(ontology_id, "v-window", {
        "stages": SEQUENCE_PATTERN["stages"], "within": 60,
    }, scan_interval=300)
    codes2 = _gate_codes(sentinel2, object_types)
    assert "sentinel_pattern_window_below_scan" in codes2, codes2

    # 3) 聚合属性不存在
    sentinel3 = _pattern_sentinel(ontology_id, "v-agg", {
        "stages": [{"alias": "a", "objectTypeId": "order"}],
        "aggregate": {"property": "missing", "function": "count",
                      "window": 300, "threshold": 3},
    })
    codes3 = _gate_codes(sentinel3, object_types)
    assert "sentinel_pattern_aggregate_property_not_found" in codes3, codes3

    # 4) stage filter 内使用时间算子
    sentinel4 = _pattern_sentinel(ontology_id, "v-filter-temporal", {
        "stages": [
            {"alias": "a", "objectTypeId": "order",
             "filter": "changed_within('a.status', 300)"},
            {"alias": "b", "objectTypeId": "order"},
        ],
        "within": 3600,
    })
    codes4 = _gate_codes(sentinel4, object_types)
    assert "sentinel_temporal_in_filter_forbidden" in codes4, codes4

    # 5) 非 on_pattern 哨兵携带 pattern
    sentinel5 = _pattern_sentinel(ontology_id, "v-mode", {
        "stages": [{"alias": "a", "objectTypeId": "order"}], "within": 3600})
    sentinel5.trigger_mode = "on_enter"
    sentinel5.on_schedule = False
    codes5 = _gate_codes(sentinel5, object_types)
    assert "invalid_sentinel_pattern_mode" in codes5, codes5

    # 6) 合法序列哨兵零错误
    sentinel6 = _pattern_sentinel(ontology_id, "v-ok", {
        "stages": SEQUENCE_PATTERN["stages"], "within": 3600,
    })
    codes6 = _gate_codes(sentinel6, object_types)
    assert codes6 == set(), codes6


def test_validation_cross_object_requires_links(db):
    ontology_id = "cep-pattern-validate-links"
    _project(db, ontology_id)
    order_type = _object_type(db, ontology_id, "order", _order_type_props())
    pay_type = _object_type(db, ontology_id, "payment", [
        {"id": "amount", "name": "amount", "type": "number"}])
    db.commit()
    object_types = [
        SimpleNamespace(id=t.id, ontology_id=ontology_id,
                        properties=t.properties)
        for t in (order_type, pay_type)
    ]
    sentinel = _pattern_sentinel(ontology_id, "v-links", {
        "stages": [
            {"alias": "a", "objectTypeId": "order"},
            {"alias": "b", "objectTypeId": "payment"},
        ],
        "within": 3600,
    }, links=[])
    codes = _gate_codes(sentinel, object_types)
    assert "sentinel_pattern_link_missing" in codes, codes

def test_absence_condition_referencing_later_stage_skips_not_errors(db, monkeypatch):
    """P1-1 回归：absence 完成只带已进入阶段快照，condition 引用后续
    stage 别名时判否跳过——不得报错死循环，水位必须推进。"""
    from app.ontologies.sentinels.cep import pattern as cep_pattern
    ontology_id = "cep-absence-cond"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "order", _order_type_props())
    order = _instance(ontology_id, "order", "order-ac",
                      {"id": "order-ac", "status": "submitted", "amount": 1})
    sentinel = _pattern_sentinel(ontology_id, "absence-cond-sentinel", {
        "stages": [
            {"alias": "a", "objectTypeId": "order",
             "filter": "a.status == 'submitted'"},
            {"alias": "b", "objectTypeId": "order",
             "filter": "b.status == 'approved'"},
        ],
        "absence": {"enabled": True},
        "within": 120,
        "condition": "b.amount > 0",
    })
    db.add_all([order, sentinel])
    db.commit()

    real_now = _now()
    monkeypatch.setattr(cep_pattern, "_now", lambda: real_now)
    _seed_event(db, ontology_id, "order", "order-ac", "status",
                "draft", "submitted", occurred_at=real_now - timedelta(seconds=30))
    evaluator.evaluate_sentinel(db, ontology_id, sentinel, "schedule")

    monkeypatch.setattr(
        cep_pattern, "_now", lambda: real_now + timedelta(minutes=10))
    second = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "schedule")
    assert second.status == "no_match", (
        f"condition 引用缺失别名必须跳过而非报错: {second.error}")
    assert db.query(SentinelMatchState).filter_by(
        sentinel_id=sentinel.id).count() == 0
    # 水位已推进：再次评估不重放、不重复报错。
    third = evaluator.evaluate_sentinel(db, ontology_id, sentinel, "schedule")
    assert third.status == "no_match"
    assert db.query(SentinelFiring).filter_by(
        sentinel_id=sentinel.id, status="error").count() == 0


def test_preview_aggregate_with_events_does_not_crash_on_sqlite(db):
    """P1-2 回归：聚合模式试跑回放在 SQLite（naive datetime）不崩。"""
    from app.ontologies.sentinels.cep import pattern as cep_pattern
    from app.ontologies.sentinels import evaluator as ev
    ontology_id = "cep-preview-agg"
    _project(db, ontology_id)
    _object_type(db, ontology_id, "device", [
        {"id": "temp", "name": "temp", "type": "number"},
    ])
    device = _instance(ontology_id, "device", "device-pv",
                       {"id": "device-pv", "temp": 90})
    sentinel = _pattern_sentinel(ontology_id, "preview-agg-sentinel", {
        "stages": [{"alias": "a", "objectTypeId": "device",
                    "filter": "a.temp > 80"}],
        "aggregate": {"property": "temp", "function": "count",
                      "window": 300, "threshold": 3, "comparison": "gte"},
    })
    db.add_all([device, sentinel])
    db.commit()
    for old, new in ((70, 85), (85, 90), (90, 88)):
        _seed_event(db, ontology_id, "device", "device-pv", "temp", old, new)

    report = ev.preview_sentinel(db, ontology_id, sentinel, "rel-x")

    assert report["passed"] is True, report["errors"]
    assert report["matchCount"] >= 1
    assert report["replayCoverage"] in {"full", "partial"}


def test_validation_rejects_absence_dead_config(db):
    """P2-2 回归：absence 在单 stage/聚合模式下是死配置，门禁拒绝。"""
    ontology_id = "cep-validate-absence"
    _project(db, ontology_id)
    order_type = _object_type(db, ontology_id, "order", _order_type_props())
    db.commit()
    object_types = [SimpleNamespace(
        id=order_type.id, ontology_id=ontology_id,
        properties=order_type.properties)]
    sentinel = _pattern_sentinel(ontology_id, "v-absence-agg", {
        "stages": [{"alias": "a", "objectTypeId": "order",
                    "filter": "a.status == 'submitted'"}],
        "absence": {"enabled": True},
        "aggregate": {"property": "status", "function": "count",
                      "window": 300, "threshold": 3},
    })
    codes = _gate_codes(sentinel, object_types)
    assert "sentinel_pattern_absence_invalid" in codes, codes


def test_validation_detects_spaced_temporal_call_in_filter(db):
    """P2-5 回归：带空格的时间算子调用也必须被发布门禁识别。"""
    ontology_id = "cep-validate-spaced"
    _project(db, ontology_id)
    order_type = _object_type(db, ontology_id, "order", _order_type_props())
    db.commit()
    object_types = [SimpleNamespace(
        id=order_type.id, ontology_id=ontology_id,
        properties=order_type.properties)]
    sentinel = _pattern_sentinel(ontology_id, "v-spaced", {
        "stages": [
            {"alias": "a", "objectTypeId": "order",
             "filter": "changed_within ( 'a.status', 300 )"},
            {"alias": "b", "objectTypeId": "order"},
        ],
        "within": 3600,
    })
    codes = _gate_codes(sentinel, object_types)
    assert "sentinel_temporal_in_filter_forbidden" in codes, codes

def test_trial_gate_sentinel_models_carry_pattern(db):
    """云端验证回归：试跑门禁经 snapshot_sentinel_models 物化哨兵模型，
    pattern/触发开关/扫描间隔必须随行，否则 on_pattern 哨兵在试跑前
    就被 invalid_sentinel_pattern 拒绝（曾在线上暴露）。"""
    from app.ontologies.versions.evolution_service import complete_snapshot
    from app.ontologies.versions.release_service import (
        snapshot_sentinel_models,
    )
    from app.ontologies.versions.snapshot_contract import snapshot_models

    ontology_id = "cep-trial-gate"
    _project(db, ontology_id)
    order_type = _object_type(db, ontology_id, "order", _order_type_props())
    db.commit()
    snap = complete_snapshot({
        "objectTypes": [{
            "id": "order", "name": "order", "displayName": "订单",
            "primaryKey": "id", "properties": _order_type_props(),
        }],
        "sentinels": [{
            "id": "cep-trial-seq",
            "name": "cep_trial_seq",
            "displayName": "试跑门禁序列",
            "bindings": [
                {"alias": "a", "objectTypeId": "order", "filter": None},
                {"alias": "b", "objectTypeId": "order", "filter": None},
            ],
            "links": [],
            "condition": None,
            "pattern": {
                "stages": [
                    {"alias": "a", "objectTypeId": "order",
                     "filter": "a.status == 'submitted'"},
                    {"alias": "b", "objectTypeId": "order",
                     "filter": "b.status == 'approved'", "within": 3600},
                ],
                "absence": {"enabled": True},
                "within": 3600,
            },
            "primaryAlias": "a",
            "actionIds": [], "actionParameters": {},
            "onChange": True, "onSchedule": True,
            "scanIntervalSeconds": 300,
            "triggerMode": "on_pattern",
        }],
    })
    models = snapshot_sentinel_models(snap)
    assert len(models) == 1
    assert isinstance(models[0].pattern, dict)
    assert models[0].pattern["stages"][0]["alias"] == "a"
    assert models[0].on_change is True and models[0].on_schedule is True
    assert models[0].scan_interval_seconds == 300

    graph = snapshot_models(snap)
    errors = validate_sentinels(
        [models[0]], graph["objectTypes"], graph["linkTypes"],
        graph["actions"])
    codes = {error.get("code") for error in errors}
    assert "invalid_sentinel_pattern" not in codes, codes
    assert codes == set(), codes
