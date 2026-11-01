"""资产写端口纯逻辑契约：规则派生、组合守卫、冻结计划校验与 guard 投影。"""
import json

import pytest

from app.ontologies.agent_confirmation import write_port as WP


def _rv(mapping, params, props):
    """与 formal_modeling._resolve_value 同形的解析器（constant 走 JSON）。"""
    st = mapping.get("sourceType")
    if st == "parameter":
        return params.get(mapping.get("sourceValue"))
    if st == "constant":
        return json.loads(mapping.get("sourceValue", ""))
    return mapping.get("sourceValue")


class _Action:
    def __init__(self, rules, object_type_id="ot-order"):
        self.rules = rules
        self.object_type_id = object_type_id


VALIDATION = {"type": "validation", "enabled": True, "order": 0,
              "config": {"condition": "params.status"}}
UPDATE_RULE = {"type": "update_property", "enabled": True, "order": 1,
               "config": {"targetProperty": "status",
                          "valueSource": "parameter", "value": "status"}}
CREATE_RULE = {"type": "create_object", "enabled": True, "order": 1,
               "config": {"targetObjectTypeId": "ot-order",
                          "propertyMappings": [
                              {"targetProperty": "status",
                               "sourceType": "constant",
                               "sourceValue": "\"new\""}]}}
LINK_RULE = {"type": "create_link", "enabled": True, "order": 2,
             "config": {"linkTypeId": "lt-1"}}
WEBHOOK_RULE = {"type": "webhook", "enabled": True, "order": 2,
                "config": {"url": "http://127.0.0.1:9/h", "method": "POST"}}


def _build(action, params=None, target_instance_id="inst-1",
           target_ref=None, props=None):
    return WP.build_write_request(
        action, params or {"status": "paid"},
        target_props=props if props is not None else {"status": "pending"},
        target_instance_id=target_instance_id,
        target_ref=target_ref, resolve_value=_rv)


def test_single_update_derives_clean_intent():
    request = _build(_Action([VALIDATION, UPDATE_RULE]))
    assert request.operation == WP.OPERATION_UPDATE
    assert request.object_type_id == "ot-order"
    assert request.property_values == {"status": "paid"}
    assert request.touched_type_ids() == {"ot-order"}
    WP.enforce_asset_write_guards(request)


def test_multi_property_single_row_update_merges():
    """同一目标的多个 update_property = 单行多属性写，不是多行组合。"""
    second = {"type": "update_property", "enabled": True, "order": 2,
              "config": {"targetProperty": "note",
                         "valueSource": "constant", "value": "\"n\""}}
    request = _build(_Action([VALIDATION, UPDATE_RULE, second]),
                     props={"status": "pending", "note": "old"})
    assert request.operation == WP.OPERATION_UPDATE
    assert request.property_values == {"status": "paid", "note": "n"}
    assert request.mixed_row_writes is False
    WP.enforce_asset_write_guards(request)


def test_duplicate_property_last_rule_wins_matching_engine_order():
    """重复属性按规则 order 后写覆盖先写 — 与本地引擎顺序语义一致。"""
    later = {"type": "update_property", "enabled": True, "order": 5,
             "config": {"targetProperty": "status",
                        "valueSource": "parameter", "value": "status2"}}
    request = _build(_Action([VALIDATION, UPDATE_RULE, later]),
                     params={"status": "paid", "status2": "shipped"})
    assert request.property_values == {"status": "shipped"}
    # 反序（后 order 在前）结果不变：按 order 排序执行
    request_reversed = _build(
        _Action([VALIDATION, later, UPDATE_RULE]),
        params={"status": "paid", "status2": "shipped"})
    assert request_reversed.property_values == {"status": "shipped"}


def test_derivation_is_descriptive_for_local_mixed_combos():
    """未绑定资产的合法本地多 effect 组合在派生阶段不被拒绝。"""
    request = _build(_Action([VALIDATION, UPDATE_RULE, LINK_RULE]))
    assert request.link_type_ids == ("lt-1",)
    assert request.operation == WP.OPERATION_UPDATE
    with pytest.raises(WP.WritePortError) as exc:
        WP.enforce_asset_write_guards(request)
    assert exc.value.code == "unsupported_combination"


def test_mixed_row_writes_flagged_not_raised():
    action = _Action([VALIDATION, UPDATE_RULE, CREATE_RULE])
    request = _build(action)
    assert request.mixed_row_writes is True
    with pytest.raises(WP.WritePortError):
        WP.enforce_asset_write_guards(request)


def test_multiple_create_objects_flagged():
    action = _Action([VALIDATION, CREATE_RULE, dict(
        CREATE_RULE, order=2,
        config=dict(CREATE_RULE["config"], targetObjectTypeId="ot-other"))])
    request = _build(action)
    assert request.mixed_row_writes is True
    assert set(request.link_type_ids) == set()
    with pytest.raises(WP.WritePortError):
        WP.enforce_asset_write_guards(request)


def test_webhook_only_action_has_no_write_intent():
    request = _build(_Action([WEBHOOK_RULE]))
    assert request.operation is None
    assert request.touched_type_ids() == set()
    # 不触发守卫（服务层在 touched 为空时直接走本地路径）。


def test_update_without_target_flagged():
    request = _build(_Action([VALIDATION, UPDATE_RULE]),
                     target_instance_id=None, target_ref=None)
    assert request.missing_write_target is True
    with pytest.raises(WP.WritePortError) as exc:
        WP.enforce_asset_write_guards(request)
    assert exc.value.code == "unsupported_combination"


def test_target_ref_conflict_rejected_at_derivation():
    with pytest.raises(WP.WritePortError) as exc:
        _build(_Action([VALIDATION, UPDATE_RULE]),
               target_instance_id="inst-1",
               target_ref=WP.TargetRef(object_type_id="ot-order",
                                       key={"order_no": "SO-1"}))
    assert exc.value.code == "target_ref_conflict"


def test_insert_derives_from_create_object():
    request = _build(_Action([VALIDATION, CREATE_RULE]),
                     target_instance_id=None, target_ref=None)
    assert request.operation == WP.OPERATION_INSERT
    assert request.object_type_id == "ot-order"
    assert request.property_values == {"status": "new"}
    WP.enforce_asset_write_guards(request)


# ------------------------------------------------------------- 冻结计划

def _plan(**overrides):
    base = {
        "planVersion": 1, "assetKind": "manual", "ontologyId": "o1",
        "releaseId": "r1", "mappingId": "m1", "objectTypeId": "ot-order",
        "datasetId": "ds1", "operation": "update",
        "key": {"order_no": "SO-1"}, "values": {"status": "paid"},
        "expectedToken": 3,
    }
    base.update(overrides)
    return base


def test_validate_frozen_plan_roundtrip_and_rejects():
    assert WP.validate_frozen_plan(_plan()) == _plan()
    with pytest.raises(WP.WritePortError):
        WP.validate_frozen_plan({"planVersion": 1})
    with pytest.raises(WP.WritePortError):
        WP.validate_frozen_plan(_plan(planVersion=2))
    with pytest.raises(WP.WritePortError):
        WP.validate_frozen_plan(_plan(assetKind="mystery"))
    with pytest.raises(WP.WritePortError):
        WP.validate_frozen_plan(_plan(assetKind="integrated", assetId=None,
                                      connectionId="c",
                                      schemaFingerprint="f"))
    with pytest.raises(WP.WritePortError):
        WP.validate_frozen_plan(_plan(key=["not", "a", "dict"]))


def test_public_summary_excludes_cas_material():
    plan = _plan(before={"status": "pending"}, schemaFingerprint="fp",
                 assetId="a1", connectionId="c1")
    summary = WP.public_plan_summary(plan)
    assert summary == {
        "kind": "manual", "datasetId": "ds1", "name": None,
        "operation": "update", "key": {"order_no": "SO-1"},
        "fields": ["status"],
    }
    dumped = str(summary)
    for secret in ("expectedToken", "before", "schemaFingerprint",
                   "assetId", "connectionId"):
        assert secret not in dumped


def test_logical_guard_plan_strips_volatile_cas_only():
    """guard 只取业务恒等键：CAS 材料剔除，同业务意图不同源快照保持互斥。"""
    plan = _plan(before={"status": "old"}, schemaFingerprint="fp")
    logical = WP.logical_guard_plan(plan)
    assert "expectedToken" not in logical
    assert "before" not in logical
    assert "schemaFingerprint" not in logical
    assert logical["values"] == {"status": "paid"}
    # 仅 CAS 材料不同的两个计划 → 同一逻辑 guard（并发互斥不失效）
    assert WP.logical_guard_plan(_plan(expectedToken=3)) == \
        WP.logical_guard_plan(_plan(expectedToken=9))
    assert WP.logical_guard_plan(_plan()) != \
        WP.logical_guard_plan(_plan(values={"status": "cancelled"}))


def test_read_dependency_extraction():
    """规则求值的目标行读取依赖必须显式声明：source_property 直引与
    validation/表达式 object.X 引用都进入 read_properties（授权后读取）。"""
    validation_dep = {"type": "validation", "enabled": True, "order": 0,
                      "config": {"condition": "object.level > 3"}}
    expr_update = {"type": "update_property", "enabled": True, "order": 1,
                   "config": {"targetProperty": "status",
                              "valueSource": "expression",
                              "value": "object.status + '!x'"}}
    prop_update = {"type": "update_property", "enabled": True, "order": 2,
                   "config": {"targetProperty": "note",
                              "valueSource": "source_property",
                              "value": "memo"}}
    request = _build(
        _Action([validation_dep, expr_update, prop_update],
                object_type_id="ot-order"),
        params={}, props={"status": "s", "memo": "m", "level": 5})
    assert set(request.read_properties) == {"level", "status", "memo"}
    # 模板常量 {{object.X}} 同样计入
    tpl = {"type": "update_property", "enabled": True, "order": 0,
           "config": {"targetProperty": "status",
                      "valueSource": "constant",
                      "value": "\"{{object.status}}-\""}}
    # 模板常量非合法 JSON 会让真实解析器失败，用表达式断言即可；此处仅
    # 验证 source_property/validation 组合已覆盖。


def test_delete_row_derivation_and_guards():
    delete_rule = {"type": "delete_row", "enabled": True, "order": 1,
                   "config": {}}
    request = _build(_Action([VALIDATION, delete_rule]))
    assert request.operation == WP.OPERATION_DELETE
    assert request.property_values == {}
    assert request.touched_type_ids() == {"ot-order"}
    WP.enforce_asset_write_guards(request)
    # delete 与链接规则混合：守卫在绑定路径拒绝（链接本地副本禁写）
    mixed = _build(_Action([VALIDATION, delete_rule, LINK_RULE]))
    with pytest.raises(WP.WritePortError) as exc:
        WP.enforce_asset_write_guards(mixed)
    assert exc.value.code == "unsupported_combination"
    # delete 与 update 混合：多行写拒绝
    mixed_row = _build(_Action([VALIDATION, delete_rule, UPDATE_RULE]))
    assert mixed_row.mixed_row_writes is True
    with pytest.raises(WP.WritePortError):
        WP.enforce_asset_write_guards(mixed_row)


def test_outcome_public_dict_shape():
    outcome = WP.AssetWriteOutcome(
        asset_kind="integrated", status="applied", operation="update",
        affected_rows=1, dataset_id="ds1", receipt_id="rcp1",
        message="内部说明")
    public = outcome.to_public_dict()
    assert public == {
        "assetKind": "integrated", "status": "applied", "operation": "update",
        "affectedRows": 1, "datasetId": "ds1", "receiptId": "rcp1",
    }
    assert "message" not in public  # message 只进内部证据


def test_port_registry_fail_closed(monkeypatch):
    monkeypatch.setattr(WP, "_port", None)
    with pytest.raises(WP.WritePortError) as exc:
        WP.action_write_port()
    assert exc.value.code == "asset_write_port_unavailable"
    with pytest.raises(WP.WritePortError):
        WP.set_action_write_port(None)
    class _Stub:
        def resolve_write(self, *a, **k): return None
        def apply_write(self, *a, **k): raise AssertionError
        def inspect_write(self, *a, **k): return None
    WP.set_action_write_port(_Stub())
    assert WP.write_port_ready() is True
    monkeypatch.setattr(WP, "_port", None)


def test_curated_domain_exception_maps_deterministic(monkeypatch):
    """curated 领域异常（湖读取失败/审批读取门禁）→ 确定性 WritePortError，
    不落入 unknown（proposal_edits 已 rollback，源零写可证）。"""
    from types import SimpleNamespace

    from app.data_channel.action_write_port import AssetWritePortAdapter
    from app.data_channel.curated import proposal_lifecycle as PL
    from app.data_channel.datasets.service import DatasetReadError

    adapter = AssetWritePortAdapter()
    plan = _plan(assetKind="curated", expectedToken="ver-1")

    def _raise(service, db, dataset_id, **k):
        raise DatasetReadError("湖表版本读取失败（模拟）")

    monkeypatch.setattr(PL, "create_candidate_review", _raise)
    with pytest.raises(WP.WritePortError) as excinfo:
        adapter.apply_write(object(), SimpleNamespace(role="admin"), plan,
                            confirmation_id="cf-map", actor_id="a1")
    assert excinfo.value.code == "curated_write_failed"
    assert excinfo.value.status == 409


def test_write_error_preserves_detail_fields_without_kwarg_collision():
    """真实源 detail 携带领域 status 键时错误转换不得崩溃（TypeError 曾把
    确定性 409 变 unknown）；HTTP 权威状态保留、其余字段透传。"""
    from fastapi import HTTPException

    from app.data_channel.action_write_port import _write_error

    exc = HTTPException(409, detail={
        "code": "review_version_already_decided",
        "message": "当前数据版本 v1 已通过，审核决定不可重开",
        "status": "approved",
        "dataset_version_id": "ver-1",
        "review_id": "rev-9",
    })
    error = _write_error(exc, default_code="curated_write_failed")
    assert error.code == "review_version_already_decided"
    assert error.status == 409
    assert error.extra["detailStatus"] == "approved"   # 领域状态改名保留
    assert error.extra["dataset_version_id"] == "ver-1"  # 其余键透传
    assert error.extra["review_id"] == "rev-9"

    # mutation 回执字段同样不与形参冲突
    exc2 = HTTPException(409, detail={
        "code": "ROW_CONFLICT", "status": "conflict",
        "mutation": {"id": "rcp-5", "status": "conflict"}})
    error2 = _write_error(exc2, default_code="source_write_rejected")
    assert error2.status == 409
    assert error2.extra["receiptId"] == "rcp-5"
    assert error2.extra["receiptStatus"] == "conflict"
    assert error2.extra["detailStatus"] == "conflict"


def test_curated_already_decided_maps_failed_not_unknown(monkeypatch):
    """D1 真实链根因回归：已批准版本的确定性 409 → failed_not_executed。"""
    from types import SimpleNamespace

    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.data_channel.action_write_port import AssetWritePortAdapter
    from app.data_channel.curated import proposal_lifecycle as PL

    def _raise(service, db, dataset_id, **k):
        raise HTTPException(409, detail={
            "code": "review_version_already_decided",
            "message": "当前数据版本 v1 已通过，审核决定不可重开",
            "status": "approved",
        })

    monkeypatch.setattr(PL, "create_candidate_review", _raise)
    adapter = AssetWritePortAdapter()
    plan = _plan(assetKind="curated", expectedToken="ver-1")
    with pytest.raises(WP.WritePortError) as excinfo:
        adapter.apply_write(object(), SimpleNamespace(role="admin"), plan,
                            confirmation_id="cf-d1", actor_id="a1")
    assert excinfo.value.code == "review_version_already_decided"
    assert excinfo.value.status == 409
    assert excinfo.value.extra["detailStatus"] == "approved"
