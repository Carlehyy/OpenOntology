"""AssetWritePortAdapter 验收测试：真实适配器 × 真实 data_channel 服务。

与既有两文件分工（均不修改）：
- ``test_action_asset_write_port.py``：write_port 纯协议派生；
- ``test_action_confirmation_asset.py``：确认链路以注入 Fake 端口锚协议语义；
- 本文件：真实 :class:`AssetWritePortAdapter` 的 resolve/apply/inspect。

真实性口径：
- manual / curated 全真实：DatasetService.create_version / load_all_rows /
  edit_rows / ReviewService（SQLite 测试库 + 库内 parquet 版本载荷，不经
  对象存储）；无 Fake 端口、无 Fake storage。
- integrated 仅在外部源 native 边界打桩（open_read_scope 读源引擎、
  mutate_row 写源），authorize / get_asset / 资产行身份与指纹漂移门禁全真实。

必测面：fieldMap 物理列名≠本体语义名；复合 PK 缺部分；schema/config/源
权限撤销；manual 新版本内容与回执；curated 版本绑定（提交未生效/批准/
拒绝/新版本失效）；实际安全行定位。断言按目标语义写死，不迁就当前实现。
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from app.data_channel.action_write_port import AssetWritePortAdapter
from app.ontologies.agent_confirmation import write_port as WP

# ----------------------------------------------------------------- helpers


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def adapter() -> AssetWritePortAdapter:
    return AssetWritePortAdapter()


@pytest.fixture
def user(admin_user):
    """admin 用户（conftest 种子）。"""
    return admin_user


def _viewer(db):
    from app.auth.models import User

    viewer = User(id=_uid("viewer"), username=_uid("v"),
                  email=f"{_uid('v')}@example.invalid",
                  password_hash="x", role="viewer", is_active=True)
    db.add(viewer)
    db.commit()
    return viewer


OT_ORDER = {
    "id": "ot-order", "name": "Order", "displayName": "订单",
    "primaryKey": "order_no",
    "properties": [
        {"id": "p1", "name": "order_no", "displayName": "订单号",
         "type": "string", "required": True},
        {"id": "p2", "name": "status", "displayName": "状态", "type": "string"},
        {"id": "p3", "name": "amount", "displayName": "金额", "type": "number"},
        # 派生属性：写拒绝
        {"id": "p4", "name": "score", "displayName": "评分",
         "type": "number", "source": "computed", "computed": True},
    ],
}
# 物理列名刻意 ≠ 语义属性名：ord_no→order_no / stat→status / amt→amount
FIELD_MAP = {"ord_no": "order_no", "stat": "status", "amt": "amount"}


def _make_dataset(db, *, kind: str, rows: list[dict], pk: str = ""):
    from app.data_channel.datasets.models import Dataset
    from app.data_channel.datasets.service import (
        DatasetService, rows_to_parquet_bytes,
    )

    schema: dict = {"primary_key": pk}
    if rows:
        schema.update({
            "columns": list(rows[0]),
            "columns_typed": [
                {"name": c, "type": (
                    "integer" if isinstance(rows[0][c], int) else "string")}
                for c in rows[0]],
            "field_names": {c: c for c in rows[0]},
        })
    ds = Dataset(name=f"{kind}-{uuid.uuid4().hex[:6]}", kind=kind,
                 schema_json=schema)
    db.add(ds)
    db.commit()
    db.refresh(ds)
    if rows:
        DatasetService(db).create_version(
            ds.id, rows_to_parquet_bytes(rows), rowcount=len(rows))
    return ds


def _release(db, ontology_id: str, mappings: list[dict], *,
             object_types: list[dict] | None = None,
             lifecycle_status: str = "released",
             node_kind: str = "release"):
    from app.ontologies.versions.models import OntologyVersion

    rel = OntologyVersion(
        id=_uid("rel"), ontology_id=ontology_id,
        version_number=f"v{uuid.uuid4().hex[:8]}", node_kind=node_kind,
        lifecycle_status=lifecycle_status,
        snapshot_formal={"objectTypes": object_types or [OT_ORDER],
                         "linkTypes": [], "mappings": mappings},
        created_by="tester")
    db.add(rel)
    db.commit()
    return rel


def _local_mapping(dataset_id: str, *, field_map: dict | None = None,
                   object_type_id: str = "ot-order") -> dict:
    return {
        "id": _uid("map"), "curatedDatasetId": dataset_id,
        "targetObjectTypeId": object_type_id,
        "fieldMapping": dict(field_map if field_map is not None else FIELD_MAP),
        "bindingMode": "local", "status": "confirmed", "confidence": 1.0,
    }


def _instance(db, ontology_id: str, release_id: str, props: dict):
    from app.ontologies.formal_modeling.models import ObjectInstance

    inst = ObjectInstance(
        id=_uid("inst"), ontology_id=ontology_id,
        ontology_release_id=release_id, object_type_id="ot-order",
        properties=props)
    db.add(inst)
    db.commit()
    return inst


def _req(operation="update", values=None, *, target_ref=None,
         instance_id=None, key=None, object_type_id="ot-order"):
    return WP.OntologyWriteRequest(
        operation=operation, object_type_id=object_type_id,
        property_values=dict(values or {}),
        key_values=dict(key if key is not None else
                        (target_ref.key if target_ref else {})),
        target_instance_id=instance_id, target_ref=target_ref)


def _resolve(adapter, db, user, ontology_id, release_id, request):
    return adapter.resolve_write(
        db, user, ontology_id=ontology_id, release_id=release_id,
        request=request)


def _apply(adapter, db, user, plan, *, confirmation_id="cf-1", actor="actor-1"):
    return adapter.apply_write(
        db, user, plan, confirmation_id=confirmation_id, actor_id=actor)


def _err_code(excinfo) -> str:
    return excinfo.value.code


def _rows(db, dataset_id: str, version_no: int | None = None) -> list[dict]:
    from app.data_channel.datasets.service import DatasetService

    return DatasetService(db).load_all_rows(dataset_id, version_no)


MANUAL_ROWS = [
    {"ord_no": "SO-1", "stat": "pending", "amt": 10},
    {"ord_no": "SO-2", "stat": "paid", "amt": 20},
]
CURATED_ROWS = [
    {"ord_no": "SO-1", "stat": "pending"},
    {"ord_no": "SO-2", "stat": "paid"},
]


@pytest.fixture
def manual_env(db, ontology, user, adapter):
    ds = _make_dataset(db, kind="manual", rows=MANUAL_ROWS, pk="ord_no")
    rel = _release(db, ontology["id"], [_local_mapping(ds.id)])
    return SimpleNamespace(ds=ds, release=rel, oid=ontology["id"])


@pytest.fixture
def curated_env(db, ontology, user, adapter):
    ds = _make_dataset(db, kind="curated", rows=CURATED_ROWS, pk="ord_no")
    # D1 候选合同：动作待审变更必须基于已 approved 基线（审核背书）
    from app.data_channel.curated.review_service import ReviewService

    backing = ReviewService(db).start_review(ds.id)
    ReviewService(db).approve(str(backing.id))
    rel = _release(db, ontology["id"], [_local_mapping(ds.id)])
    return SimpleNamespace(ds=ds, release=rel, oid=ontology["id"])


# --------------------------------------------- A. fieldMap 物理≠语义（resolve）


def test_resolve_translates_semantic_to_physical_columns(
        db, user, adapter, manual_env):
    request = _req(values={"status": "paid"},
                   target_ref=WP.TargetRef("ot-order",
                                           {"order_no": "SO-1"}))
    plan = _resolve(adapter, db, user, manual_env.oid,
                    manual_env.release.id, request)
    # key/values 落在物理列空间；语义名绝不直写
    assert plan["key"] == {"ord_no": "SO-1"}
    assert plan["values"] == {"stat": "paid"}
    assert plan["assetKind"] == WP.ASSET_KIND_MANUAL
    assert plan["datasetId"] == manual_env.ds.id
    assert plan["expectedToken"] == 1  # 冻结时最新版本号
    assert plan["before"] is None


def test_resolve_via_local_instance_reads_key_from_properties(
        db, user, adapter, manual_env):
    inst = _instance(db, manual_env.oid, manual_env.release.id,
                     {"order_no": "SO-2", "status": "paid"})
    plan = _resolve(adapter, db, user, manual_env.oid,
                    manual_env.release.id,
                    _req(values={"status": "shipped"},
                         instance_id=inst.id))
    assert plan["key"] == {"ord_no": "SO-2"}
    assert plan["values"] == {"stat": "shipped"}


def test_resolve_rejects_unmapped_semantic_property(
        db, user, adapter, manual_env):
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid, manual_env.release.id,
                 _req(values={"note": "x"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "unknown_write_column"


def test_resolve_rejects_property_mapped_by_multiple_columns(
        db, user, adapter, manual_env):
    rel2 = _release(db, manual_env.oid, [
        _local_mapping(manual_env.ds.id,
                       field_map={**FIELD_MAP, "status": "status"})])
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid, rel2.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "mapping_ambiguous"


def test_resolve_rejects_pk_column_without_property_mapping(
        db, user, adapter, manual_env):
    """复合主键列未映射到本体属性 → 无法经本体定位，拒绝写（读不受影响）。"""
    ds = _make_dataset(db, kind="manual",
                       rows=[{"ord_no": "SO-1", "seq": "1", "stat": "p"}],
                       pk="ord_no,seq")
    rel = _release(db, manual_env.oid, [
        _local_mapping(ds.id,
                       field_map={"ord_no": "order_no", "stat": "status"})])
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid, rel.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "no_stable_addressing"
    assert ei.value.extra["unmappedPrimaryKeyColumns"] == ["seq"]


def test_resolve_rejects_primary_key_and_computed_writes(
        db, user, adapter, manual_env):
    ref = WP.TargetRef("ot-order", {"order_no": "SO-1"})
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid,
                 manual_env.release.id,
                 _req(values={"order_no": "SO-9"}, target_ref=ref))
    assert _err_code(ei) == "primary_key_change_denied"
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid,
                 manual_env.release.id,
                 _req(values={"score": 1}, target_ref=ref))
    assert _err_code(ei) == "computed_property_write_denied"


# ------------------------------------------------- B. 复合 PK / 安全行定位


def test_composite_pk_requires_complete_key_parts(db, user, adapter,
                                                  manual_env):
    ds = _make_dataset(db, kind="manual",
                       rows=[{"ord_no": "SO-1", "seq": "1", "stat": "p"},
                             {"ord_no": "SO-1", "seq": "2", "stat": "q"}],
                       pk="ord_no,seq")
    rel = _release(db, manual_env.oid, [
        _local_mapping(ds.id, field_map={"ord_no": "order_no",
                                         "seq": "seq", "stat": "status"})])
    # 缺 seq → 拒绝并指明期望
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid, rel.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "complete_primary_key_required"
    assert ei.value.extra["expected"] == ["order_no", "seq"]
    # 空值同样拒绝
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid, rel.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef(
                          "ot-order", {"order_no": "SO-1", "seq": " "})))
    assert _err_code(ei) == "complete_primary_key_required"
    # 无任何定位方式 → 拒绝
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid, rel.id,
                 _req(values={"status": "paid"}))
    assert _err_code(ei) == "unsupported_combination"
    # 完整复合键 → 物理列 key 落齐
    plan = _resolve(adapter, db, user, manual_env.oid, rel.id,
                    _req(values={"status": "paid"},
                         target_ref=WP.TargetRef(
                             "ot-order", {"order_no": "SO-1", "seq": "2"})))
    assert plan["key"] == {"ord_no": "SO-1", "seq": "2"}


def test_manual_composite_pk_update_hits_exactly_one_row(
        db, user, adapter, manual_env):
    """复合 PK 实际安全定位：只改命中行，其余行（含同 ord_no 另一 seq）不动。"""
    rows = [{"ord_no": "SO-1", "seq": "1", "stat": "p"},
            {"ord_no": "SO-1", "seq": "2", "stat": "q"},
            {"ord_no": "SO-2", "seq": "1", "stat": "r"}]
    ds = _make_dataset(db, kind="manual", rows=rows, pk="ord_no,seq")
    rel = _release(db, manual_env.oid, [
        _local_mapping(ds.id, field_map={"ord_no": "order_no",
                                         "seq": "seq", "stat": "status"})])
    plan = _resolve(adapter, db, user, manual_env.oid, rel.id,
                    _req(values={"status": "paid"},
                         target_ref=WP.TargetRef(
                             "ot-order", {"order_no": "SO-1", "seq": "2"})))
    outcome = _apply(adapter, db, user, plan)
    assert outcome.status == WP.OUTCOME_APPLIED and \
        outcome.affected_rows == 1
    after = {(r["ord_no"], r["seq"]): r["stat"] for r in _rows(db, ds.id)}
    assert after == {("SO-1", "1"): "p", ("SO-1", "2"): "paid",
                     ("SO-2", "1"): "r"}


def test_manual_update_missing_row_fails_without_new_version(
        db, user, adapter, manual_env):
    ref = WP.TargetRef("ot-order", {"order_no": "SO-999"})
    plan = _resolve(adapter, db, user, manual_env.oid,
                    manual_env.release.id, _req(values={"status": "paid"},
                                                target_ref=ref))
    from app.data_channel.datasets.models import DatasetVersion

    before = db.query(DatasetVersion).filter_by(
        dataset_id=manual_env.ds.id).count()
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, user, plan)
    assert _err_code(ei) in {"row_not_found", "manual_write_failed"}
    after = db.query(DatasetVersion).filter_by(
        dataset_id=manual_env.ds.id).count()
    assert after == before  # 定位失败零版本变更


def test_curated_update_requires_existing_review_row(
        db, user, adapter, curated_env):
    """curated 提交前按当前审核版本定位行：行不存在 → 零审核提交。"""
    from app.data_channel.curated.models import CuratedReview

    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, curated_env.oid,
                 curated_env.release.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-999"})))
    assert _err_code(ei) == "row_not_found"
    # 零新增候选/待审（curated_env 的 approved 背书 review 不计入）
    assert db.query(CuratedReview).filter_by(
        curated_dataset_id=curated_env.ds.id,
        base_version_id=None).filter(
        CuratedReview.status == "pending").count() == 0


# ------------------------------------------- C. schema/config/权限撤销（resolve 门禁）


def test_resolve_gates_release_and_binding_config(
        db, user, adapter, manual_env):
    oid = manual_env.oid
    # 非释放版本 / 错误 release
    draft = _release(db, oid, [], node_kind="draft",
                     lifecycle_status="editing")
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, oid, draft.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "release_not_found"
    # 对象类型不在快照（映射仍指向 ot-order，但快照只含 ot-other）
    other_type = dict(OT_ORDER, id="ot-other", name="Other")
    rel2 = _release(db, oid, [_local_mapping(manual_env.ds.id)],
                    object_types=[other_type])
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, oid, rel2.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "object_type_not_in_release"
    # 同一类型两条映射（配置歧义）
    rel3 = _release(db, oid, [_local_mapping(manual_env.ds.id),
                              _local_mapping(manual_env.ds.id)])
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, oid, rel3.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "mapping_ambiguous"
    # 映射指向已删除数据集（config 撤销）
    rel4 = _release(db, oid, [_local_mapping(_uid("gone"))])
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, oid, rel4.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "mapping_dataset_missing"


def test_resolve_gates_dataset_contract_and_versions(
        db, user, adapter, manual_env):
    # manual 未声明主键契约 → 拒写（读不受影响）
    ds_nopk = _make_dataset(db, kind="manual", rows=MANUAL_ROWS, pk="")
    rel = _release(db, manual_env.oid, [_local_mapping(ds_nopk.id)])
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid, rel.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "no_stable_primary_key"
    # 数据集无任何版本
    ds_empty = _make_dataset(db, kind="manual", rows=[], pk="ord_no")
    rel2 = _release(db, manual_env.oid, [_local_mapping(ds_empty.id)])
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid, rel2.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "dataset_has_no_version"
    # curated 行删除显式不支持
    ds_curated = _make_dataset(db, kind="curated", rows=CURATED_ROWS,
                               pk="ord_no")
    rel3 = _release(db, manual_env.oid, [_local_mapping(ds_curated.id)])
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid, rel3.id,
                 _req(operation="delete",
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "curated_delete_unsupported"


# ------------------------------------------------- D. manual 新版本内容与回执


def test_manual_apply_creates_new_version_with_verified_content(
        db, user, adapter, manual_env):
    ds = manual_env.ds
    plan = _resolve(adapter, db, user, manual_env.oid,
                    manual_env.release.id,
                    _req(values={"status": "paid"},
                         target_ref=WP.TargetRef("ot-order",
                                                 {"order_no": "SO-1"})))
    outcome = _apply(adapter, db, user, plan)
    assert outcome.status == WP.OUTCOME_APPLIED
    assert outcome.affected_rows == 1
    assert outcome.version_id  # 回执指明新版本
    from app.data_channel.datasets.models import DatasetVersion

    v2 = db.query(DatasetVersion).filter_by(id=outcome.version_id).one()
    assert v2.version_no == 2
    # 新版本内容：仅命中行变化（湖读取权威形态为字符串单元格）
    assert _rows(db, ds.id) == [
        {"ord_no": "SO-1", "stat": "paid", "amt": "10"},
        {"ord_no": "SO-2", "stat": "paid", "amt": "20"},
    ]
    # 旧版本不可变
    assert _rows(db, ds.id, version_no=1) == [
        {"ord_no": "SO-1", "stat": "pending", "amt": "10"},
        {"ord_no": "SO-2", "stat": "paid", "amt": "20"},
    ]


def test_manual_apply_stale_base_version_rejected_zero_change(
        db, user, adapter, manual_env):
    from app.data_channel.datasets.models import DatasetVersion
    from app.data_channel.datasets.service import (
        DatasetService, rows_to_parquet_bytes,
    )

    plan = _resolve(adapter, db, user, manual_env.oid,
                    manual_env.release.id,
                    _req(values={"status": "paid"},
                         target_ref=WP.TargetRef("ot-order",
                                                 {"order_no": "SO-1"})))
    # prepare 之后有人先行发布了 v2（并发写）
    DatasetService(db).create_version(
        manual_env.ds.id, rows_to_parquet_bytes(MANUAL_ROWS),
        rowcount=len(MANUAL_ROWS))
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, user, plan)
    assert _err_code(ei) == "version_conflict"
    assert db.query(DatasetVersion).filter_by(
        dataset_id=manual_env.ds.id).count() == 2  # 失败零新增版本


def test_manual_unprovable_receipt_stays_unknown(
        db, user, adapter, manual_env, monkeypatch):
    """执行已发生但回执不可证（changed!=1）→ unknown，不冒充 applied。

    edit_rows 是适配器对 data_channel 的执行边界；此处按授权在该边界
    注入“已响应但不可证”的真实返回形态。
    """
    from app.data_channel.datasets import mutation_service

    def _respond(dataset_id, body, db, *, require_manual_dataset_fn=None):
        return {"dataset_id": dataset_id, "version_no": 99, "rowcount": 1,
                "updated": 0, "inserted": 0, "deleted": 0}

    monkeypatch.setattr(mutation_service, "edit_rows", _respond)
    plan = _resolve(adapter, db, user, manual_env.oid,
                    manual_env.release.id,
                    _req(values={"status": "paid"},
                         target_ref=WP.TargetRef("ot-order",
                                                 {"order_no": "SO-1"})))
    outcome = _apply(adapter, db, user, plan)
    assert outcome.status == WP.OUTCOME_UNKNOWN
    assert outcome.error_code == "execution_result_unknown"


def test_manual_viewer_role_rejected_at_apply_zero_change(
        db, user, adapter, manual_env):
    from app.data_channel.datasets.models import DatasetVersion

    plan = _resolve(adapter, db, user, manual_env.oid,
                    manual_env.release.id,
                    _req(values={"status": "paid"},
                         target_ref=WP.TargetRef("ot-order",
                                                 {"order_no": "SO-1"})))
    viewer = _viewer(db)
    before = db.query(DatasetVersion).filter_by(
        dataset_id=manual_env.ds.id).count()
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, viewer, plan)
    assert _err_code(ei) == "source_write_denied"
    assert ei.value.status == 403
    assert db.query(DatasetVersion).filter_by(
        dataset_id=manual_env.ds.id).count() == before


# ------------------------------------------------ E. integrated（native 边界打桩）


@pytest.fixture
def integrated_env(db, user, monkeypatch):
    from app.data_channel.connections.models import Connection
    from app.data_channel.datasets.models import Dataset
    from app.data_channel.integrated import native
    from app.data_channel.integrated.models import IntegratedAsset

    conn = Connection(id=_uid("conn"), name="ext-mysql", kind="mysql",
                      config={}, status="active", created_by=user.id)
    db.add(conn)
    db.flush()
    ds = Dataset(name=f"integrated-{uuid.uuid4().hex[:6]}",
                 kind="integrated", schema_json={})
    db.add(ds)
    db.flush()
    asset = IntegratedAsset(
        id=_uid("asset"), dataset_id=ds.id, connection_id=conn.id,
        name="goal_orders", schema_name="oo_goal",
        table_name="goal_orders",
        schema_json={"columns": [{"name": "ord_no_col", "type": "varchar"},
                                 {"name": "stat_col", "type": "varchar"}]},
        schema_fingerprint="fp-1", writable=True, created_by=user.id)
    db.add(asset)
    db.commit()

    table = sa.Table(
        "goal_orders", sa.MetaData(),
        sa.Column("ord_no_col", sa.String),
        sa.Column("stat_col", sa.String))

    class _Scope:
        def __init__(self, rows):
            self._rows = rows
            self.meta = {asset.id: {
                "schema_fingerprint": asset.schema_fingerprint,
                "projection": {"logical_to_physical": {
                    "order_no": "ord_no_col", "status": "stat_col"}},
                "connection_id": conn.id,
            }}
            self.tables = {asset.id: table}
            self.statements: list = []

        def iter_batches(self, statement, batch_size=1):
            self.statements.append(statement)
            yield list(self._rows[:batch_size])

    holder = SimpleNamespace(scope=None)

    @contextmanager
    def _open_read_scope(db, user_, asset_ids, *, statement_timeout_ms=300000):
        scope = _Scope(holder.rows if hasattr(holder, "rows") else [])
        holder.scope = scope
        yield scope

    monkeypatch.setattr(native, "open_read_scope", _open_read_scope)
    holder._open = _open_read_scope
    return SimpleNamespace(conn=conn, ds=ds, asset=asset, holder=holder)


def _remote_mapping(env, *, pk=("ord_no_col",)):
    return {
        "id": _uid("mapr"), "targetObjectTypeId": "ot-order",
        "bindingMode": "remote",
        "fieldMapping": {"ord_no_col": "order_no", "stat_col": "status"},
        "remote": {
            "assetId": env.asset.id, "datasetId": env.ds.id,
            "connectionId": env.conn.id,
            "schemaName": "oo_goal", "tableName": "goal_orders",
            "schemaFingerprint": "fp-1",
            "primaryKey": list(pk),
            "columns": [{"name": "ord_no_col", "type": "varchar"},
                        {"name": "stat_col", "type": "varchar"}],
        },
        "status": "confirmed",
    }


def _patch_mutate(monkeypatch, fn):
    from app.data_channel.integrated import service as integrated_service

    monkeypatch.setattr(integrated_service, "mutate_row", fn)


def test_integrated_resolve_physical_columns_and_token(
        db, user, adapter, ontology, integrated_env):
    env = integrated_env
    env.holder.rows = [{"ord_no_col": "SO-1", "stat_col": "pending"}]
    rel = _release(db, ontology["id"], [_remote_mapping(env)])
    plan = _resolve(adapter, db, user, ontology["id"], rel.id,
                    _req(values={"status": "paid"},
                         target_ref=WP.TargetRef("ot-order",
                                                 {"order_no": "SO-1"})))
    # 物理列空间 + 真实 row_token（含源指纹）
    assert plan["assetKind"] == WP.ASSET_KIND_INTEGRATED
    assert plan["key"] == {"ord_no_col": "SO-1"}
    assert plan["values"] == {"stat_col": "paid"}
    assert plan["schemaFingerprint"] == "fp-1"
    assert plan["expectedToken"]
    # 源端定位 SQL 经 logical_to_physical 落到物理列（不拿语义名查源）
    stmt_sql = str(env.holder.scope.statements[0])
    assert "ord_no_col" in stmt_sql and "order_no" not in stmt_sql
    # 远程对象没有本地实例：target_instance 被拒绝
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, ontology["id"], rel.id,
                 _req(values={"status": "paid"}, instance_id="inst-x"))
    assert _err_code(ei) == "target_ref_required"


def test_integrated_row_existence_gates(
        db, user, adapter, ontology, integrated_env):
    env = integrated_env
    rel = _release(db, ontology["id"], [_remote_mapping(env)])
    # update 目标行不存在 → row_not_found
    env.holder.rows = []
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, ontology["id"], rel.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-9"})))
    assert _err_code(ei) == "row_not_found"
    # insert 目标键已存在 → row_already_exists
    env.holder.rows = [{"ord_no_col": "SO-1", "stat_col": "pending"}]
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, ontology["id"], rel.id,
                 _req(operation="insert", values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "row_already_exists"


def test_integrated_writable_revocation_denied_by_real_native(
        db, user, adapter, ontology, integrated_env):
    """源端写使能撤销（asset.writable=False）：真实 authorize/get_asset 拒绝。"""
    from app.data_channel.integrated.models import IntegratedAsset

    env = integrated_env
    env.asset.writable = False
    db.commit()
    rel = _release(db, ontology["id"], [_remote_mapping(env)])
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, ontology["id"], rel.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert ei.value.status == 403
    assert ei.value.code in {"SOURCE_WRITE_DENIED", "source_write_denied"}


def _integrated_plan(db, user, adapter, ontology, env):
    env.holder.rows = [{"ord_no_col": "SO-1", "stat_col": "pending"}]
    rel = _release(db, ontology["id"], [_remote_mapping(env)])
    plan = _resolve(adapter, db, user, ontology["id"], rel.id,
                    _req(values={"status": "paid"},
                         target_ref=WP.TargetRef("ot-order",
                                                 {"order_no": "SO-1"})))
    return plan


def test_integrated_apply_schema_drift_and_asset_removal(
        db, user, adapter, ontology, integrated_env, monkeypatch):
    """prepare 冻结指纹；执行前 schema 漂移 / 资产被删 → 拒绝且零源写。"""
    env = integrated_env

    def _forbidden(*a, **k):
        raise AssertionError("mutate_row must not run on drift/missing")

    _patch_mutate(monkeypatch, _forbidden)
    plan = _integrated_plan(db, user, adapter, ontology, env)
    env.asset.schema_fingerprint = "fp-drifted"
    db.commit()
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, user, plan)
    assert _err_code(ei) == "schema_drift"

    plan = _integrated_plan(db, user, adapter, ontology, env)
    from app.data_channel.integrated.models import IntegratedAsset

    db.delete(db.get(IntegratedAsset, env.asset.id))
    db.commit()
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, user, plan)
    # 资产删除由 apply 侧真实 get_asset 复验（原生 404 家族）或计划身份
    # 门禁拒绝——两者都必须发生在任何源写之前。
    assert _err_code(ei) in {"remote_asset_missing", "ASSET_NOT_FOUND"}
    assert ei.value.status in (404, 409)


def test_integrated_apply_outcomes_receipt_driven(
        db, user, adapter, ontology, integrated_env, monkeypatch):
    env = integrated_env
    plan = _integrated_plan(db, user, adapter, ontology, env)

    def _ok(*a, **k):
        return {"id": "rcp-1", "status": "applied", "affected_rows": 1,
                "replayed": False}

    _patch_mutate(monkeypatch, _ok)
    outcome = _apply(adapter, db, user, plan)
    assert outcome.status == WP.OUTCOME_APPLIED
    assert outcome.receipt_id == "rcp-1"
    assert outcome.message is None

    def _replayed(*a, **k):
        return {"id": "rcp-1", "status": "applied", "affected_rows": 1,
                "replayed": True}

    _patch_mutate(monkeypatch, _replayed)
    outcome = _apply(adapter, db, user, plan)
    assert outcome.status == WP.OUTCOME_APPLIED
    assert "幂等" in (outcome.message or "")

    # 源端 commit unknown → unknown 不自动重放，不冒充成功
    def _unknown(*a, **k):
        raise HTTPException(502, {
            "code": "SOURCE_UNKNOWN", "message": "commit unknown",
            "mutation": {"id": "rcp-2", "status": "unknown"}})

    _patch_mutate(monkeypatch, _unknown)
    outcome = _apply(adapter, db, user, plan)
    assert outcome.status == WP.OUTCOME_UNKNOWN
    assert outcome.receipt_id == "rcp-2"
    assert outcome.error_code == "execution_result_unknown"

    # 确定性失败（token 冲突）→ 拒绝并携带回执
    def _conflict(*a, **k):
        raise HTTPException(409, {
            "code": "ROW_TOKEN_CONFLICT", "message": "token mismatch",
            "mutation": {"id": "rcp-3", "status": "failed"}})

    _patch_mutate(monkeypatch, _conflict)
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, user, plan)
    assert _err_code(ei) == "ROW_TOKEN_CONFLICT"
    assert ei.value.extra.get("receiptId") == "rcp-3"


def test_integrated_viewer_rejected_before_source_write(
        db, user, adapter, ontology, integrated_env, monkeypatch):
    env = integrated_env
    plan = _integrated_plan(db, user, adapter, ontology, env)

    def _forbidden(*a, **k):
        raise AssertionError("viewer must be rejected before mutate_row")

    _patch_mutate(monkeypatch, _forbidden)
    viewer = _viewer(db)
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, viewer, plan)
    assert _err_code(ei) == "source_write_denied"


# ------------------------------------------ F. curated 版本绑定生命周期（真实）


def _curated_plan(db, user, adapter, env, *, values=None, key="SO-1"):
    return _resolve(adapter, db, user, env.oid, env.release.id,
                    _req(values=values or {"status": "paid"},
                         target_ref=WP.TargetRef("ot-order",
                                                 {"order_no": key})))


def test_curated_lifecycle_pending_approve_supersede(
        db, user, adapter, curated_env):
    from app.data_channel.curated.models import CuratedReview, CuratedRowEdit
    from app.data_channel.curated.review_service import ReviewService

    ds = curated_env.ds
    plan = _curated_plan(db, user, adapter, curated_env)
    # prepare 读取当前审核版本行作为 old_value 证据（物理列空间）
    assert plan["before"] == {"stat": "pending"}
    # curated 冻结的 CAS token 是当前审核版本 id
    assert plan["expectedToken"] == ds.latest_version_id

    outcome = _apply(adapter, db, user, plan)
    assert outcome.status == WP.OUTCOME_PENDING_REVIEW
    assert outcome.review_id
    review = db.get(CuratedReview, outcome.review_id)
    assert review.status == "pending"
    # D1：review 绑定候选版本（更高 version_no、指针未前移）
    assert review.dataset_version_id != ds.latest_version_id
    assert review.base_version_id == ds.latest_version_id

    # 提交未生效：版本载荷不变、无新版本
    assert _rows(db, ds.id) == CURATED_ROWS
    edit = db.query(CuratedRowEdit).filter_by(
        review_id=review.id).one()
    assert (edit.field_name, edit.old_value, edit.new_value) == \
        ("stat", "pending", "paid")

    # pending 阶段 inspect 不改判
    assert adapter.inspect_write(
        db, user, plan, evidence={"reviewId": review.id}) is None

    # 批准 → applied；审核叠加读取见到真实新值
    ReviewService(db).approve(review.id, notes="ok")
    outcome = adapter.inspect_write(
        db, user, plan, evidence={"reviewId": review.id})
    assert outcome.status == WP.OUTCOME_APPLIED
    assert outcome.affected_rows == 1
    assert outcome.version_id == review.dataset_version_id
    overlaid = ReviewService(db).apply_edits_to_snapshot(
        review.id, _rows(db, ds.id))
    assert next(r for r in overlaid if r["ord_no"] == "SO-1")["stat"] == \
        "paid"

    # 新版本出现 → 旧批准失效
    from app.data_channel.datasets.service import (
        DatasetService, rows_to_parquet_bytes,
    )

    DatasetService(db).create_version(
        ds.id, rows_to_parquet_bytes(CURATED_ROWS),
        rowcount=len(CURATED_ROWS))
    outcome = adapter.inspect_write(
        db, user, plan, evidence={"reviewId": review.id})
    assert outcome.status == WP.OUTCOME_FAILED
    assert outcome.error_code == "curated_review_superseded"


def test_curated_resolve_rejects_without_approved_base(
        db, user, adapter, ontology):
    """D1 负例：当前版本无 approved 审核背书 → prepare 确定性拒绝。"""
    ds = _make_dataset(db, kind="curated", rows=CURATED_ROWS, pk="ord_no")
    rel = _release(db, ontology["id"], [_local_mapping(ds.id)])
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, ontology["id"], rel.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "curated_base_not_approved"


def test_curated_rejected_never_takes_effect(
        db, user, adapter, curated_env):
    from app.data_channel.curated.review_service import ReviewService

    ds = curated_env.ds
    plan = _curated_plan(db, user, adapter, curated_env)
    outcome = _apply(adapter, db, user, plan)
    review_id = outcome.review_id
    ReviewService(db).reject(review_id, notes="bad")
    result = adapter.inspect_write(db, user, plan,
                                   evidence={"reviewId": review_id})
    assert result.status == WP.OUTCOME_FAILED
    assert result.error_code == "curated_review_rejected"
    # 拒绝后底层版本载荷保持原值
    assert _rows(db, ds.id) == CURATED_ROWS


def test_curated_inspect_ignores_foreign_review(
        db, user, adapter, curated_env):
    """回访按 reviewId 绑定真实 review 且必须属于计划数据集（防串他人审核）。"""
    from app.data_channel.curated.models import CuratedReview

    other = _make_dataset(db, kind="curated", rows=CURATED_ROWS,
                          pk="ord_no")
    plan = _curated_plan(db, user, adapter, curated_env)
    outcome = _apply(adapter, db, user, plan)  # 本数据集 pending review
    foreign = CuratedReview(
        id=_uid("rev"), curated_dataset_id=other.id,
        dataset_version_id=other.latest_version_id, status="approved")
    db.add(foreign)
    db.commit()
    assert adapter.inspect_write(
        db, user, plan, evidence={"reviewId": foreign.id}) is None
    # 不存在的 review / 缺 evidence → None
    assert adapter.inspect_write(
        db, user, plan, evidence={"reviewId": _uid("ghost")}) is None
    assert adapter.inspect_write(db, user, plan, evidence={}) is None


# --------------------------------------- G. 冻结计划 CAS 收口（目标契约，当前实现缺口如实失败）


def test_curated_apply_rejects_when_version_advanced_after_prepare(
        db, user, adapter, curated_env):
    """A-12/A-25 目标语义：prepare 冻结 expectedToken（审核版本 id），
    prepare 之后数据集出新版本，execute 必须按 CAS 拒绝，不得把编辑
    提交到前进后的错误版本上。"""
    from app.data_channel.curated.models import CuratedReview
    from app.data_channel.datasets.service import (
        DatasetService, rows_to_parquet_bytes,
    )

    ds = curated_env.ds
    plan = _curated_plan(db, user, adapter, curated_env)
    frozen_token = plan["expectedToken"]
    # prepare 之后流水线入湖新版本（prepare→execute 窗口内源前进）
    DatasetService(db).create_version(
        ds.id, rows_to_parquet_bytes(CURATED_ROWS + [
            {"ord_no": "SO-3", "stat": "new"}]),
        rowcount=len(CURATED_ROWS) + 1)
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, user, plan)
    assert _err_code(ei) == "review_version_conflict"
    # 拒绝必须发生在任何审核副作用之前：不得产生绑定错误版本的新 review
    assert db.query(CuratedReview).filter(
        CuratedReview.curated_dataset_id == ds.id,
        CuratedReview.dataset_version_id != frozen_token).count() == 0


def test_integrated_apply_rejects_identity_change_beyond_fingerprint(
        db, user, adapter, ontology, integrated_env, monkeypatch):
    """目标语义：schema 指纹相同但资产物理身份（连接/库/表）已变（同
    schema 换库换表）时，execute 必须按计划身份不符拒绝，不得写错库。"""
    env = integrated_env
    plan = _integrated_plan(db, user, adapter, ontology, env)

    def _ok(*a, **k):
        return {"id": "rcp-x", "status": "applied", "affected_rows": 1,
                "replayed": False}

    _patch_mutate(monkeypatch, _ok)
    for field, value in (("table_name", "goal_shipments"),
                         ("schema_name", "oo_other"),
                         ("connection_id", _uid("conn"))):
        fresh_plan = _integrated_plan(db, user, adapter, ontology, env)
        setattr(env.asset, field, value)
        db.commit()
        with pytest.raises(WP.WritePortError) as ei:
            _apply(adapter, db, user, fresh_plan)
        assert _err_code(ei) in {"schema_drift", "remote_asset_identity_changed",
                                 "remote_asset_missing"}, field
        # 还原身份，继续下一个维度
        env.asset.table_name = "goal_orders"
        env.asset.schema_name = "oo_goal"
        env.asset.connection_id = env.conn.id
        env.asset.schema_fingerprint = "fp-1"
        db.commit()
        env.holder.rows = [{"ord_no_col": "SO-1", "stat_col": "pending"}]


def test_integrated_apply_rechecks_source_authorization(
        db, user, adapter, ontology, integrated_env, monkeypatch):
    """目标语义：execute 时源访问授权（menu）被撤销必须拒绝，且发生在
    任何源写之前——即使 mutate 边界本身被替换也不能绕过门禁。"""
    from app.auth.models import RoleMenuPermission, User

    env = integrated_env
    editor = User(id=_uid("editor"), username=_uid("e"),
                  email=f"{_uid('e')}@example.invalid",
                  password_hash="x", role="editor", is_active=True)
    db.add(editor)
    db.add(RoleMenuPermission(role="editor", menu_keys=[], updated_by="t"))
    db.commit()
    plan = _integrated_plan(db, user, adapter, ontology, env)

    def _forbidden(*a, **k):
        raise AssertionError(
            "mutate_row must not run when menu access was revoked")

    _patch_mutate(monkeypatch, _forbidden)
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, editor, plan)
    # 拒绝来自 apply 侧真实 authorize/get_asset 复验（原生 403 家族）；
    # sentinel 已证明 mutate 边界未被触达（零源写）。
    assert ei.value.status == 403
    assert _err_code(ei) in {
        "source_write_denied", "INTEGRATED_ACCESS_DENIED",
        "INTEGRATED_READ_ONLY", "SOURCE_WRITE_DENIED"}


# ---------------------- H. 真实 rule → create_object insert 全链（manual/integrated）


class _Action:
    """与纯协议测试同形的最小真实动作对象（rules + object_type_id）。"""

    def __init__(self, rules, object_type_id="ot-order"):
        self.rules = rules
        self.object_type_id = object_type_id


def _rv(mapping, params, props):
    st = mapping.get("sourceType")
    if st == "parameter":
        return params.get(mapping.get("sourceValue"))
    return mapping.get("sourceValue")


def _create_rule(values: dict[str, str], *, type_id="ot-order"):
    return {"type": "create_object", "enabled": True, "order": 1,
            "config": {"targetObjectTypeId": type_id,
                       "propertyMappings": [
                           {"targetProperty": prop,
                            "sourceType": "constant", "sourceValue": value}
                           for prop, value in values.items()]}}


def _build_from_rules(action, *, target_ref=None, target_instance_id=None,
                      params=None, target_props=None):
    return WP.build_write_request(
        action, params or {}, target_props=target_props,
        target_instance_id=target_instance_id, target_ref=target_ref,
        resolve_value=_rv)


def test_insert_from_real_create_rule_manual_positive_no_shadow(
        db, user, adapter, manual_env):
    """无 targetRef 的 create_object（主键来自 create 映射）→ 真实插入正例。

    源终值=manual 数据集新版本含新行；不得写任何本地 fo 实例影子。
    """
    from app.data_channel.datasets.models import DatasetVersion
    from app.ontologies.formal_modeling.models import ObjectInstance

    request = _build_from_rules(_Action([_create_rule(
        {"order_no": "SO-NEW", "status": "new"})]))
    assert request.operation == WP.OPERATION_INSERT
    plan = _resolve(adapter, db, user, manual_env.oid,
                    manual_env.release.id, request)
    assert plan["key"] == {"ord_no": "SO-NEW"}  # 主键从 create 映射拆出
    assert plan["values"] == {"stat": "new"}
    outcome = _apply(adapter, db, user, plan)
    assert outcome.status == WP.OUTCOME_APPLIED and \
        outcome.affected_rows == 1
    # 源终值：v2 含新行，旧行不动
    rows = {r["ord_no"]: r["stat"] for r in _rows(db, manual_env.ds.id)}
    assert rows == {"SO-1": "pending", "SO-2": "paid", "SO-NEW": "new"}
    assert db.query(DatasetVersion).filter_by(
        dataset_id=manual_env.ds.id).count() == 2
    # 无本地影子：确认链零 fo 实例写入
    assert db.query(ObjectInstance).filter_by(
        ontology_id=manual_env.oid).count() == 0


def test_insert_missing_pk_in_create_mappings_rejected(
        db, user, adapter, manual_env):
    request = _build_from_rules(_Action([_create_rule({"status": "new"})]))
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid,
                 manual_env.release.id, request)
    assert _err_code(ei) == "complete_primary_key_required"
    assert ei.value.extra.get("missing") == ["ord_no"]


def test_insert_target_ref_value_conflict_rejected(
        db, user, adapter, manual_env):
    request = _build_from_rules(
        _Action([_create_rule({"order_no": "SO-9", "status": "new"})]),
        target_ref=WP.TargetRef("ot-order", {"order_no": "SO-NEW"}))
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid,
                 manual_env.release.id, request)
    assert _err_code(ei) == "primary_key_conflict"


def test_insert_composite_pk_from_mappings_manual(
        db, user, adapter, manual_env):
    rows = [{"ord_no": "SO-1", "seq": "1", "stat": "p"}]
    ds = _make_dataset(db, kind="manual", rows=rows, pk="ord_no,seq")
    rel = _release(db, manual_env.oid, [
        _local_mapping(ds.id, field_map={"ord_no": "order_no",
                                         "seq": "seq", "stat": "status"})])
    # 完整复合主键来自 create 映射 → 正例
    request = _build_from_rules(_Action([_create_rule(
        {"order_no": "SO-2", "seq": "5", "status": "ok"})]))
    plan = _resolve(adapter, db, user, manual_env.oid, rel.id, request)
    assert plan["key"] == {"ord_no": "SO-2", "seq": "5"}
    assert plan["values"] == {"stat": "ok"}
    outcome = _apply(adapter, db, user, plan)
    assert outcome.status == WP.OUTCOME_APPLIED
    assert _rows(db, ds.id) == [
        {"ord_no": "SO-1", "seq": "1", "stat": "p"},
        {"ord_no": "SO-2", "seq": "5", "stat": "ok"}]
    # 缺 seq → complete_primary_key_required（missing=seq）
    request = _build_from_rules(_Action([_create_rule(
        {"order_no": "SO-3", "status": "x"})]))
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid, rel.id, request)
    assert _err_code(ei) == "complete_primary_key_required"
    assert ei.value.extra.get("missing") == ["seq"]


def test_manual_insert_duplicate_pk_fails_zero_change(
        db, user, adapter, manual_env):
    from app.data_channel.datasets.models import DatasetVersion

    request = _build_from_rules(_Action([_create_rule(
        {"order_no": "SO-1", "status": "dup"})]))
    plan = _resolve(adapter, db, user, manual_env.oid,
                    manual_env.release.id, request)
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, user, plan)
    assert _err_code(ei) in {"row_already_exists", "manual_write_failed",
                             "primary_key_duplicate"}
    assert db.query(DatasetVersion).filter_by(
        dataset_id=manual_env.ds.id).count() == 1  # 零版本变更
    assert {r["ord_no"] for r in _rows(db, manual_env.ds.id)} == \
        {"SO-1", "SO-2"}  # 源数据不变


def test_insert_from_real_create_rule_integrated_positive(
        db, user, adapter, ontology, integrated_env, monkeypatch):
    """integrated 无 ref 插入正例：主键从 create 映射拆出，物理列 key。"""
    env = integrated_env
    env.holder.rows = []  # 源端无该行
    rel = _release(db, ontology["id"], [_remote_mapping(env)])
    request = _build_from_rules(_Action([_create_rule(
        {"order_no": "SO-NEW", "status": "new"})]))
    plan = _resolve(adapter, db, user, ontology["id"], rel.id, request)
    assert plan["operation"] == WP.OPERATION_INSERT
    assert plan["key"] == {"ord_no_col": "SO-NEW"}
    assert plan["values"] == {"stat_col": "new"}
    assert plan["expectedToken"] is None  # 插入不读行 token

    def _ok(*a, **k):
        return {"id": "rcp-ins", "status": "applied", "affected_rows": 1,
                "replayed": False}

    _patch_mutate(monkeypatch, _ok)
    outcome = _apply(adapter, db, user, plan)
    assert outcome.status == WP.OUTCOME_APPLIED
    assert outcome.receipt_id == "rcp-ins"


# ---------------- I. 三层命名空间（语义/逻辑/物理）× 真实 mutate_row


PHYSICAL_COLUMNS = [{"name": "ord_no_col", "type": "varchar", "length": 16},
                    {"name": "stat_col", "type": "varchar", "length": 16}]


def _configured_env(db, user, monkeypatch):
    """配置了逻辑视图的集成资产：语义 order_no / 逻辑 order_no_l / 物理 ord_no_col。"""
    from app.data_channel.connections.models import Connection
    from app.data_channel.datasets.models import Dataset
    from app.data_channel.integrated import binding_port, fields, native
    from app.data_channel.integrated.models import (
        IntegratedAsset, IntegratedAssetFieldConfig,
    )

    conn = Connection(id=_uid("conn"), name="ext-mysql", kind="mysql",
                      config={}, status="active", created_by=user.id)
    db.add(conn)
    db.flush()
    ds = Dataset(name=f"integrated-{uuid.uuid4().hex[:6]}",
                 kind="integrated", schema_json={})
    db.add(ds)
    db.flush()
    asset = IntegratedAsset(
        id=_uid("asset"), dataset_id=ds.id, connection_id=conn.id,
        name="goal_orders", schema_name="oo_goal",
        table_name="goal_orders",
        schema_json={"columns": PHYSICAL_COLUMNS,
                     "primary_key": ["ord_no_col"]},
        schema_fingerprint="fp-l3", writable=True, created_by=user.id)
    db.add(asset)
    db.add(IntegratedAssetFieldConfig(
        asset_id=asset.id,
        field_config=[
            {"source_key": "ord_no_col", "name": "order_no_l",
             "display_name": "订单键"},
            {"source_key": "stat_col", "name": "status_l",
             "display_name": "状态"}]))
    db.commit()
    projection = fields.projection_of(
        asset.schema_json, [
            {"source_key": "ord_no_col", "name": "order_no_l",
             "display_name": "订单键"},
            {"source_key": "stat_col", "name": "status_l",
             "display_name": "状态"}])
    holder = SimpleNamespace(rows=[], statements=[])

    class _Scope:
        meta = {asset.id: {"schema_fingerprint": asset.schema_fingerprint,
                           "columns": PHYSICAL_COLUMNS,
                           "projection": projection,
                           "connection_id": conn.id}}
        tables = {asset.id: sa.Table(
            "goal_orders", sa.MetaData(),
            sa.Column("ord_no_col", sa.String, primary_key=True),
            sa.Column("stat_col", sa.String))}

        def iter_batches(self, statement, batch_size=1):
            holder.statements.append(statement)
            yield list(holder.rows[:batch_size])

    @contextmanager
    def _open(db_, user_, asset_ids, *, statement_timeout_ms=300000):
        yield _Scope()

    monkeypatch.setattr(native, "open_read_scope", _open)
    monkeypatch.setattr(binding_port, "open_read_scope", _open)
    # canonical descriptor：经真实 validator.accepts 冻结（逻辑列/主键）
    descriptor = binding_port.IntegratedRemoteBindingValidator().accepts(
        db, user, {"datasetId": ds.id})
    return SimpleNamespace(conn=conn, ds=ds, asset=asset, holder=holder,
                           descriptor=descriptor, projection=projection)


def _sqlite_source(db_path):
    """真实 SQLite 引擎作为外部源（仅引擎边界替换；隔离语义由单事务覆盖）。"""
    from sqlalchemy.pool import StaticPool

    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    return engine


class _ConnShim:
    """REPEATABLE READ 在 sqlite 不存在：按方言差异吞掉隔离级别参数。"""

    def __init__(self, real):
        self._real = real

    @property
    def dialect(self):
        return self._real.dialect

    def execution_options(self, **kw):
        return self

    def begin(self):
        return self._real.begin()

    def execute(self, *a, **k):
        return self._real.execute(*a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _EngineShim:
    def __init__(self, engine):
        self._engine = engine

    def connect(self):
        return _ConnShim(self._engine.connect())

    def dispose(self):
        self._engine.dispose()


def _patch_real_source(monkeypatch, db_path):
    from app.data_channel.integrated import service as integrated_service

    engine = _sqlite_source(db_path)
    physical = sa.Table(
        "goal_orders", sa.MetaData(),
        sa.Column("ord_no_col", sa.String, primary_key=True),
        sa.Column("stat_col", sa.String))
    physical.create(engine, checkfirst=True)

    def _source_engine(connection):
        return _EngineShim(engine)

    def _reflect(conn, asset):
        schema = {"columns": PHYSICAL_COLUMNS,
                  "primary_key": ["ord_no_col"]}
        return physical, schema, asset.schema_fingerprint

    monkeypatch.setattr(integrated_service, "source_engine", _source_engine)
    monkeypatch.setattr(integrated_service, "reflect", _reflect)
    return engine


def test_integrated_three_layer_namespace_real_mutate_update_and_insert(
        db, user, adapter, ontology, monkeypatch, tmp_path):
    """语义 order_no / 逻辑 order_no_l / 物理 ord_no_col 三层全链：
    真实 asset_projection + 真实 descriptor 冻结 + 真实 mutate_row（仅源
    引擎/反射边界替换）。配置资产的 key/values 必须走逻辑命名空间，不得
    因物理 key 被 COMPLETE_PRIMARY_KEY_REQUIRED 拒绝。"""
    from app.data_channel.integrated.models import IntegratedMutation

    env = _configured_env(db, user, monkeypatch)
    engine = _patch_real_source(monkeypatch, str(tmp_path / "src.db"))
    with engine.connect() as c:
        physical_src = sa.Table(
            "goal_orders", sa.MetaData(),
            sa.Column("ord_no_col", sa.String, primary_key=True),
            sa.Column("stat_col", sa.String))
        c.execute(sa.insert(physical_src).values(
            ord_no_col="SO-1", stat_col="pending"))
        c.commit()
    env.holder.rows = [{"ord_no_col": "SO-1", "stat_col": "pending"}]

    mapping = {"id": _uid("mapr"), "targetObjectTypeId": "ot-order",
               "bindingMode": "remote",
               "fieldMapping": {"order_no_l": "order_no",
                                "status_l": "status"},
               "remote": env.descriptor, "status": "confirmed"}
    rel = _release(db, ontology["id"], [mapping])

    # --- update：三层命名空间全链 ---
    plan = _resolve(adapter, db, user, ontology["id"], rel.id,
                    _req(values={"status": "shipped"},
                         target_ref=WP.TargetRef(
                             "ot-order", {"order_no": "SO-1"})))
    assert plan["key"] == {"order_no_l": "SO-1"}   # 逻辑层
    assert plan["values"] == {"status_l": "shipped"}
    assert plan["projectionFingerprint"] == env.projection["fingerprint"]
    outcome = _apply(adapter, db, user, plan,
                     confirmation_id="cf-l3-update")
    assert outcome.status == WP.OUTCOME_APPLIED  # 非 COMPLETE_PRIMARY_KEY_REQUIRED
    with engine.connect() as c:
        value = c.execute(sa.select(physical_src.c.stat_col).where(
            physical_src.c.ord_no_col == "SO-1")).scalar_one()
    assert value == "shipped"  # 物理终值经逻辑→物理映射正确落源

    # --- insert：逻辑键新行 ---
    env.holder.rows = []
    request = _build_from_rules(_Action([_create_rule(
        {"order_no": "SO-7", "status": "new"})]))
    plan = _resolve(adapter, db, user, ontology["id"], rel.id, request)
    assert plan["key"] == {"order_no_l": "SO-7"}
    outcome = _apply(adapter, db, user, plan,
                     confirmation_id="cf-l3-insert")
    assert outcome.status == WP.OUTCOME_APPLIED
    with engine.connect() as c:
        row = c.execute(sa.select(physical_src).where(
            physical_src.c.ord_no_col == "SO-7")).mappings().one()
    assert row["stat_col"] == "new"
    # 真实回执已入审计表
    assert db.query(IntegratedMutation).filter_by(
        asset_id=env.asset.id).count() == 2


def test_integrated_projection_change_same_schema_rejected(
        db, user, adapter, ontology, monkeypatch, tmp_path):
    """逻辑视图变更（同物理 schema 指纹）：prepare 冻结 projection 指纹，
    执行前漂移必须拒绝且零源写。"""
    from app.data_channel.integrated.models import IntegratedAssetFieldConfig

    env = _configured_env(db, user, monkeypatch)
    _patch_real_source(monkeypatch, str(tmp_path / "src.db"))
    env.holder.rows = [{"ord_no_col": "SO-1", "stat_col": "pending"}]
    mapping = {"id": _uid("mapr"), "targetObjectTypeId": "ot-order",
               "bindingMode": "remote",
               "fieldMapping": {"order_no_l": "order_no",
                                "status_l": "status"},
               "remote": env.descriptor, "status": "confirmed"}
    rel = _release(db, ontology["id"], [mapping])
    plan = _resolve(adapter, db, user, ontology["id"], rel.id,
                    _req(values={"status": "shipped"},
                         target_ref=WP.TargetRef(
                             "ot-order", {"order_no": "SO-1"})))

    def _forbidden(*a, **k):
        raise AssertionError("mutate_row must not run on projection drift")

    _patch_mutate(monkeypatch, _forbidden)
    cfg = db.query(IntegratedAssetFieldConfig).filter_by(
        asset_id=env.asset.id).one()
    cfg.field_config = [
        {"source_key": "ord_no_col", "name": "key_alias",
         "display_name": "键"},
        {"source_key": "stat_col", "name": "status_l",
         "display_name": "状态"}]
    db.commit()
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, user, plan)
    assert _err_code(ei) == "schema_drift"


# ---------------------------------- J. 连接配置更换 / 描述符缺字段 / pk 语义改名


def test_integrated_same_connection_config_replacement_rejected(
        db, user, adapter, ontology, integrated_env, monkeypatch):
    """同 connectionId 更换配置（冻结 revision）：执行前必须按配置指纹漂移
    拒绝，绝不用新配置连源。"""
    env = integrated_env
    plan = _integrated_plan(db, user, adapter, ontology, env)

    def _forbidden(*a, **k):
        raise AssertionError("mutate_row must not run on config drift")

    _patch_mutate(monkeypatch, _forbidden)
    env.conn.config = {"host": "10.0.0.9", "rotated": True}
    db.commit()
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, user, plan)
    assert _err_code(ei) == "source_config_drift"


def test_integrated_descriptor_missing_identity_field_rejected(
        db, user, adapter, ontology, integrated_env):
    """冻结描述符缺必需身份字段：必须以确定性 WritePortError 拒绝，
    不得让 RemoteBindingError 裸穿确认链。"""
    env = integrated_env
    broken = dict(_remote_mapping(env)["remote"])
    broken["schemaFingerprint"] = ""
    mapping = {"id": _uid("mapr"), "targetObjectTypeId": "ot-order",
               "bindingMode": "remote",
               "fieldMapping": {"ord_no_col": "order_no",
                                "stat_col": "status"},
               "remote": broken, "status": "confirmed"}
    rel = _release(db, ontology["id"], [mapping])
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, ontology["id"], rel.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert ei.value.status in (409, 422)


def test_pk_property_semantic_rename_roundtrip(db, user, adapter,
                                               manual_env):
    """主键属性的语义改名：定位走改名后的属性；旧属性名不再放行。"""
    rel = _release(db, manual_env.oid, [
        _local_mapping(manual_env.ds.id,
                       field_map={"ord_no": "key_no", "stat": "status"})])
    plan = _resolve(adapter, db, user, manual_env.oid, rel.id,
                    _req(values={"status": "paid"},
                         target_ref=WP.TargetRef("ot-order",
                                                 {"key_no": "SO-1"})))
    assert plan["key"] == {"ord_no": "SO-1"}
    outcome = _apply(adapter, db, user, plan)
    assert outcome.status == WP.OUTCOME_APPLIED
    # 旧语义名（未映射属性）不再构成定位
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, manual_env.oid, rel.id,
                 _req(values={"status": "paid"},
                      target_ref=WP.TargetRef("ot-order",
                                              {"order_no": "SO-1"})))
    assert _err_code(ei) == "complete_primary_key_required"


# ------------------- K. curated 拒绝壳原子性（真实 adapter + finalizer commit）


def test_curated_invalid_field_rejection_leaves_no_shell_after_commit(
        db, user, adapter, curated_env):
    """review-curated-rejected-shell 真实链路复现：冻结映射与数据集列漂移
    （fieldMapping 指向不存在的列），apply 确定性拒绝后 caller finalizer
    commit，必须 0 review / 0 edit / 状态不变——不得残留 pending 审核壳。"""
    from app.data_channel.curated.models import CuratedReview, CuratedRowEdit

    rel2 = _release(db, curated_env.oid, [
        _local_mapping(curated_env.ds.id,
                       field_map={"ord_no": "order_no",
                                  "stat_ghost": "status"})])
    plan = _resolve(adapter, db, user, curated_env.oid, rel2.id,
                    _req(values={"status": "paid"},
                         target_ref=WP.TargetRef("ot-order",
                                                 {"order_no": "SO-1"})))
    before_schema = dict(curated_env.ds.schema_json)
    with pytest.raises(WP.WritePortError) as ei:
        _apply(adapter, db, user, plan)
    assert ei.value.status in (400, 409, 422)
    # 模拟确认服务在异常路径后的审计 finalizer 提交
    db.commit()
    assert db.query(CuratedReview).filter_by(
        curated_dataset_id=curated_env.ds.id,
        base_version_id=None).filter(
        CuratedReview.status == "pending").count() == 0
    assert db.query(CuratedRowEdit).count() == 0
    assert curated_env.ds.schema_json == before_schema  # 审核状态零变化
    assert _rows(db, curated_env.ds.id) == CURATED_ROWS  # 版本载荷不变


# ---------- L. targetSnapshot 有界读依赖与隐藏材料授权（正例 + 负例）


def _target_dep_action():
    """真实规则：status ← 目标行 amount（source_property 依赖，defer 求值）。"""
    return _Action([{"type": "update_property", "enabled": True, "order": 0,
                     "config": {"targetProperty": "status",
                                "valueSource": "source_property",
                                "value": "amount"}}])


def test_integrated_target_snapshot_bounded_and_hidden(
        db, user, adapter, ontology, integrated_env):
    """正例：读依赖经授权映射进入有界快照；未映射/未引用列绝不出现；
    隐藏材料（token/快照）不进公开摘要。"""
    env = integrated_env
    env.holder.rows = [{"ord_no_col": "SO-1", "stat_col": "pending",
                        "amt_col": "99", "internal_col": "secret-internal"}]
    mapping = _remote_mapping(env)
    mapping["fieldMapping"] = {"ord_no_col": "order_no",
                               "stat_col": "status", "amt_col": "amount"}
    rel = _release(db, ontology["id"], [mapping])
    request = WP.build_write_request(
        _target_dep_action(), {}, target_props=None,
        target_instance_id=None,
        target_ref=WP.TargetRef("ot-order", {"order_no": "SO-1"}),
        resolve_value=_rv, defer_target_dependent=True)
    assert set(request.pending_value_mappings) == {"status"}
    assert request.read_properties == ("amount",)

    plan = _resolve(adapter, db, user, ontology["id"], rel.id, request)
    # 延迟值不在 values；valueMap 声明属性→列供服务端读后求值
    assert plan["values"] == {}
    assert plan["valueMap"] == {"status": "stat_col"}
    snapshot = plan["targetSnapshot"]
    assert snapshot["order_no"] == "SO-1" and snapshot["amount"] == "99"
    assert set(snapshot) <= {"order_no", "status", "amount"}
    assert "internal_col" not in snapshot          # 未映射源列绝不外借
    assert "secret-internal" not in str(snapshot)  # 未授权值零泄漏
    assert plan["expectedToken"]                    # CAS 材料仅在服务端计划
    assert len(env.holder.scope.statements) == 1    # 单次有界源读
    # 公开摘要只含业务恒等键；token/快照/指纹全部隐藏
    summary = WP.public_plan_summary(plan)
    assert set(summary) == {"kind", "datasetId", "name", "operation",
                            "key", "fields"}
    dumped = str(summary)
    for hidden in ("targetSnapshot", "expectedToken", "before",
                   "schemaFingerprint", "sourceConfigFingerprint",
                   "projectionFingerprint", "secret-internal"):
        assert hidden not in dumped


def test_integrated_unmapped_read_dependency_rejected(
        db, user, adapter, ontology, integrated_env):
    """负例（目标契约）：规则读取依赖引用未映射源属性 → prepare 必须确定性
    拒绝（不得先读后滤、不得静默丢弃依赖后求值成错值），且零源读。"""
    env = integrated_env
    env.holder.rows = [{"ord_no_col": "SO-1", "stat_col": "pending"}]
    rel = _release(db, ontology["id"], [_remote_mapping(env)])
    action = _Action([{"type": "update_property", "enabled": True,
                       "order": 0,
                       "config": {"targetProperty": "status",
                                  "valueSource": "source_property",
                                  "value": "secret_note"}}])
    request = WP.build_write_request(
        action, {}, target_props=None, target_instance_id=None,
        target_ref=WP.TargetRef("ot-order", {"order_no": "SO-1"}),
        resolve_value=_rv, defer_target_dependent=True)
    assert request.read_properties == ("secret_note",)
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, ontology["id"], rel.id, request)
    assert ei.value.status == 422
    assert _err_code(ei) in {"unknown_write_column",
                             "unknown_read_property",
                             "unauthorized_read_property"}
    assert env.holder.scope is None or not env.holder.scope.statements


def test_curated_target_snapshot_bounded_and_hidden(
        db, user, adapter, curated_env):
    """curated 正例：读依赖经映射进入有界快照；old_value 证据仅限写入列；
    before/快照不进公开摘要。"""
    rows = [{"ord_no": "SO-1", "stat": "pending", "amt": "99"},
            {"ord_no": "SO-2", "stat": "paid", "amt": "20"}]
    ds = _make_dataset(db, kind="curated", rows=rows, pk="ord_no")
    from app.data_channel.curated.review_service import ReviewService

    backing = ReviewService(db).start_review(ds.id)
    ReviewService(db).approve(str(backing.id))
    rel = _release(db, curated_env.oid, [
        _local_mapping(ds.id, field_map={"ord_no": "order_no",
                                         "stat": "status",
                                         "amt": "amount"})])
    request = WP.build_write_request(
        _target_dep_action(), {}, target_props=None, target_instance_id=None,
        target_ref=WP.TargetRef("ot-order", {"order_no": "SO-1"}),
        resolve_value=_rv, defer_target_dependent=True)
    plan = _resolve(adapter, db, user, curated_env.oid, rel.id, request)
    # 延迟写入列的当前值必须可从计划回收（before 或有界快照至少其一），
    # 供审核 old_value 证据与 apply 校验——不得静默丢失。
    recovered = (plan["before"] or {}).get(
        "stat", (plan["targetSnapshot"] or {}).get("status"))
    assert recovered == "pending"
    snapshot = plan["targetSnapshot"]
    assert snapshot["order_no"] == "SO-1" and snapshot["amount"] == "99"
    assert set(snapshot) <= {"order_no", "status", "amount"}
    summary = WP.public_plan_summary(plan)
    assert "before" not in str(summary) and "targetSnapshot" not in \
        str(summary) and "expectedToken" not in str(summary)


def test_curated_unmapped_read_dependency_rejected(
        db, user, adapter, curated_env):
    """负例（目标契约）：curated 读依赖引用未映射属性 → prepare 确定性拒绝，
    零审核副作用。"""
    from app.data_channel.curated.models import CuratedReview

    ds = _make_dataset(db, kind="curated",
                       rows=[{"ord_no": "SO-1", "stat": "pending"}],
                       pk="ord_no")
    rel = _release(db, curated_env.oid, [
        _local_mapping(ds.id, field_map={"ord_no": "order_no",
                                         "stat": "status"})])
    action = _Action([{"type": "update_property", "enabled": True,
                       "order": 0,
                       "config": {"targetProperty": "status",
                                  "valueSource": "source_property",
                                  "value": "secret_note"}}])
    request = WP.build_write_request(
        action, {}, target_props=None, target_instance_id=None,
        target_ref=WP.TargetRef("ot-order", {"order_no": "SO-1"}),
        resolve_value=_rv, defer_target_dependent=True)
    with pytest.raises(WP.WritePortError) as ei:
        _resolve(adapter, db, user, curated_env.oid, rel.id, request)
    assert ei.value.status == 422
    assert _err_code(ei) in {"unknown_write_column",
                             "unknown_read_property",
                             "unauthorized_read_property"}
    assert db.query(CuratedReview).filter_by(curated_dataset_id=ds.id) \
        .count() == 0
