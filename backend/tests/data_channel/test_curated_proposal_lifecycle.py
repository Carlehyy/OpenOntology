"""成品候选版本生命周期契约（D1 最小 B 合同）。

覆盖：pending/rejected 不动当前指针（旧 approved 值可查询）、approve 前
移指针且新值可见、同 proposal_key 幂等、base CAS（源前移后旧批准失效）、
无版本/无 approved 基线拒绝、可验证拒绝零候选零审核壳、候选不发
version_published 事件、版本号分配覆盖候选。
"""
import pytest

from app.data_channel.curated import proposal_lifecycle as PL
from app.data_channel.curated.approved_version_reader import (
    ReviewApprovalError,
)
from app.data_channel.curated.models import CuratedReview, CuratedRowEdit
from app.data_channel.curated.review_row_identity import (
    latest_dataset_version,
)
from app.data_channel.curated.review_service import ReviewService
from app.data_channel.datasets.models import (
    Dataset, DatasetVersion, DatasetVersionEvent,
)
from app.data_channel.datasets.service import DatasetService, rows_to_csv_bytes

SCHEMA = {
    "columns": ["order_no", "status"],
    "primary_key": "order_no",
}


def _rows_bytes(rows):
    return rows_to_csv_bytes(rows, SCHEMA["columns"])


@pytest.fixture
def curated_approved(db, admin_user):
    """curated v1（含已批准审核背书）数据集。"""
    ds = Dataset(id="ds-c1", name="成品订单", kind="curated",
                 schema_json=dict(SCHEMA))
    db.add(ds)
    db.commit()
    svc = DatasetService(db)
    v1 = svc.create_version("ds-c1", _rows_bytes(
        [{"order_no": "SO-1", "status": "pending"}]), rowcount=1)
    service = ReviewService(db)
    review = service.start_review("ds-c1")
    service.approve(str(review.id))
    db.expire_all()
    return {"dataset": ds, "v1": v1}


def _edits(row_pk="SO-1", field="status", new="shipped"):
    return [{"row_pk": row_pk, "field_name": field,
             "old_value": "pending", "new_value": new}]


def _candidate(db, curated_approved, key="proposal-write:cf-1"):
    service = ReviewService(db)
    return PL.create_candidate_review(
        service, db, "ds-c1",
        base_version_id=str(curated_approved["v1"].id),
        proposal_key=key, edits=_edits(), reviewer_id="u-admin")


def _current_rows(db):
    return DatasetService(db).load_all_rows("ds-c1")


def _version_events(db, event_type):
    return db.query(DatasetVersionEvent).filter(
        DatasetVersionEvent.dataset_id == "ds-c1",
        DatasetVersionEvent.event_type == event_type).count()


def test_pending_keeps_pointer_and_old_values(db, curated_approved):
    review = _candidate(db, curated_approved)
    assert review.status == "pending"
    assert review.base_version_id == str(curated_approved["v1"].id)
    # B'：pending 未绑定版本 — 零 DatasetVersion、零变更集
    assert review.dataset_version_id is None
    assert latest_dataset_version(db, "ds-c1").id == curated_approved["v1"].id
    assert _current_rows(db)[0]["status"] == "pending"
    assert db.query(DatasetVersion).filter_by(
        dataset_id="ds-c1").count() == 1
    assert _version_events(db, "version_published") == 1
    assert db.query(CuratedRowEdit).filter_by(
        review_id=review.id).count() == 1


def test_reject_keeps_old_values(db, curated_approved):
    review = _candidate(db, curated_approved)
    PL.reject_candidate_review(db, review)
    db.expire_all()
    assert latest_dataset_version(db, "ds-c1").id == curated_approved["v1"].id
    assert _current_rows(db)[0]["status"] == "pending"
    assert db.query(CuratedReview).filter_by(id=review.id).one().status == \
        "rejected"
    schema = db.query(Dataset).filter_by(id="ds-c1").one().schema_json
    assert schema.get("review_status") == "approved"


def test_approve_advances_pointer_and_exposes_new_values(db, curated_approved):
    review = _candidate(db, curated_approved)
    PL.approve_candidate_review(db, review)
    db.expire_all()
    # B'：批准时经原生发布路径创建正式版本并绑定
    assert review.dataset_version_id is not None
    assert latest_dataset_version(db, "ds-c1").id == review.dataset_version_id
    assert _current_rows(db)[0]["status"] == "shipped"
    assert db.query(CuratedReview).filter_by(id=review.id).one().status == \
        "approved"
    assert db.query(DatasetVersion).filter_by(
        dataset_id="ds-c1").count() == 2
    # 批准事件与既有 outbox 契约同事务落账（基线批准 1 + 候选批准 1）
    assert _version_events(db, "curated_review_approved") == 2


def test_duplicate_proposal_key_reuses_candidate(db, curated_approved):
    first = _candidate(db, curated_approved, key="proposal-write:cf-dup")
    second = _candidate(db, curated_approved, key="proposal-write:cf-dup")
    assert second.id == first.id
    assert db.query(DatasetVersion).filter_by(
        dataset_id="ds-c1").count() == 1  # 零候选版本


def test_approve_base_superseded_rejects_and_keeps_candidate(
        db, curated_approved):
    review = _candidate(db, curated_approved)
    # 模拟流水线产新版本（正常路径前移指针）
    DatasetService(db).create_version("ds-c1", _rows_bytes(
        [{"order_no": "SO-1", "status": "pipeline-new"}]), rowcount=1)
    from fastapi import HTTPException

    versions_before = db.query(DatasetVersion).filter_by(
        dataset_id="ds-c1").count()
    with pytest.raises(HTTPException) as excinfo:
        PL.approve_candidate_review(db, review)
    assert excinfo.value.detail["code"] == "curated_base_superseded"
    db.expire_all()
    assert db.query(CuratedReview).filter_by(id=review.id).one().status == \
        "pending"
    assert _current_rows(db)[0]["status"] == "pipeline-new"
    # 拒绝前移零版本增量（流水线 1 + 基线 1）
    assert db.query(DatasetVersion).filter_by(
        dataset_id="ds-c1").count() == versions_before


def test_no_version_rejected(db, admin_user):
    db.add(Dataset(id="ds-empty", name="空成品", kind="curated",
                   schema_json=dict(SCHEMA)))
    db.commit()
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        _candidate(db, {"v1": type("V", (), {"id": "missing"})})
    assert excinfo.value.detail["code"] in (
        "dataset_has_no_version", "curated_dataset_not_found")


def test_base_without_approved_review_rejected(db, admin_user):
    ds = Dataset(id="ds-c2", name="未批准成品", kind="curated",
                 schema_json=dict(SCHEMA))
    db.add(ds)
    db.commit()
    DatasetService(db).create_version("ds-c2", _rows_bytes(
        [{"order_no": "SO-1", "status": "pending"}]), rowcount=1)
    service = ReviewService(db)
    with pytest.raises(ReviewApprovalError):
        PL.create_candidate_review(
            service, db, "ds-c2", base_version_id=str(
                latest_dataset_version(db, "ds-c2").id),
            proposal_key="proposal-write:cf-noappr",
            edits=_edits(), reviewer_id="u-admin")
    db.rollback()
    # 零候选零审核壳
    assert db.query(DatasetVersion).filter_by(
        dataset_id="ds-c2").count() == 1
    assert db.query(CuratedReview).filter_by(
        curated_dataset_id="ds-c2").count() == 0


def test_invalid_edit_leaves_zero_shell(db, curated_approved):
    service = ReviewService(db)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        PL.create_candidate_review(
            service, db, "ds-c1",
            base_version_id=str(curated_approved["v1"].id),
            proposal_key="proposal-write:cf-bad",
            edits=_edits(field="not_a_column"), reviewer_id="u-admin")
    assert excinfo.value.detail["code"] in (
        "review_unknown_field", "review_type_mismatch")
    assert db.query(DatasetVersion).filter_by(
        dataset_id="ds-c1").count() == 1  # 零候选版本
    assert db.query(CuratedReview).filter_by(
        proposal_key="proposal-write:cf-bad").count() == 0
    assert _current_rows(db)[0]["status"] == "pending"


def test_failure_after_review_flush_leaves_zero_trace(
        db, curated_approved, monkeypatch):
    """审核行 flush 后、外层 commit 前失败：rollback 撤回（零审核壳零
    编辑零版本——B' pending 本就不建版本）。"""
    from app.data_channel.curated import proposal_lifecycle as PL2
    from app.data_channel.curated import models as CM

    def _boom(*a, **k):
        raise RuntimeError("simulated failure after candidate flush")

    monkeypatch.setattr(
        "app.data_channel.curated.proposal_lifecycle.CuratedRowEdit",
        _boom)
    service = ReviewService(db)
    with pytest.raises(Exception):
        PL2.create_candidate_review(
            service, db, "ds-c1",
            base_version_id=str(curated_approved["v1"].id),
            proposal_key="proposal-write:cf-crash",
            edits=_edits(), reviewer_id="u-admin")
    db.rollback()
    db.expire_all()
    assert db.query(DatasetVersion).filter_by(
        dataset_id="ds-c1").count() == 1  # 零孤儿候选版本
    assert db.query(CM.CuratedReview).filter_by(
        proposal_key="proposal-write:cf-crash").count() == 0
    assert _current_rows(db)[0]["status"] == "pending"


# ───────────────────────────── lake 正门（真实湖表基线） ─────────────────────

LAKE_SCHEMA = {
    "columns": ["ord_no", "stat", "amount", "shipped_at"],
    "primary_key": "ord_no",
    "columns_typed": [
        {"name": "ord_no", "type": "string"},
        {"name": "stat", "type": "string"},
        {"name": "amount", "type": "decimal"},
        {"name": "shipped_at", "type": "date"},
    ],
    "lake_columns": {"ord_no": "c1", "stat": "c2", "amount": "c3",
                     "shipped_at": "c4"},
}


@pytest.fixture
def lake_curated_approved(db, admin_user):
    """真实湖表成品：upsert_run 正门入湖 + 已批准审核背书（typed 含
    NULL/decimal/date）。"""
    import uuid as _uuid

    from app.data_channel.datasets import lake_store
    from app.data_channel.datasets.lock import dataset_write_lock
    from app.data_channel.datasets.models import Dataset

    ds = Dataset(id=f"ds-lake-{_uuid.uuid4().hex[:6]}", name="湖表成品",
                 kind="curated", schema_json=dict(LAKE_SCHEMA))
    db.add(ds)
    db.commit()
    rows = [
        {"ord_no": "L-1", "stat": "pending", "amount": "150.7500",
         "shipped_at": "2026-10-01"},
        {"ord_no": "L-2", "stat": "new", "amount": None,
         "shipped_at": None},
    ]
    with dataset_write_lock(f"dataset::{ds.id}", bind=db.get_bind()):
        v1, _cs = lake_store.upsert_run(db, ds, rows, "overwrite",
                                        ["ord_no"])
    service = ReviewService(db)
    review = service.start_review(ds.id)
    service.approve(str(review.id))
    db.expire_all()
    return {"ds": ds, "v1": v1}


def _lake_rows(db, dataset_id):
    from app.data_channel.datasets.service import DatasetService

    return DatasetService(db).load_all_rows(dataset_id)


def test_lake_candidate_pending_keeps_old_values_and_replay(
        db, lake_curated_approved):
    """pending 候选：指针/物理表/历史回读全部保持旧值（零生效）。"""
    from app.data_channel.datasets.lake_store import rows_at_version

    ds = lake_curated_approved["ds"]
    base = lake_curated_approved["v1"]
    review = PL.create_candidate_review(
        ReviewService(db), db, ds.id, base_version_id=str(base.id),
        proposal_key="proposal-write:cf-lake-1",
        edits=[{"row_pk": "L-1", "field_name": "stat",
                "old_value": "pending", "new_value": "shipped"}],
        reviewer_id="u-admin")
    assert review.status == "pending"
    current = _lake_rows(db, ds.id)
    assert next(r for r in current if r["ord_no"] == "L-1")["stat"] == \
        "pending"
    # 湖表既有口径：NULL 经 snapshot_cell_text 序列化为 ""（平台语义）
    assert next(r for r in current if r["ord_no"] == "L-2")["amount"] == ""
    assert next(r for r in current
                if r["ord_no"] == "L-2")["shipped_at"] == ""
    at_base = rows_at_version(db, ds, base.version_no)
    assert next(r for r in at_base if r["ord_no"] == "L-1")["stat"] == \
        "pending"


def test_lake_candidate_approve_applies_and_backfills_changeset(
        db, lake_curated_approved):
    """approve：物理应用 + 变更集/checksum 回填 + 指针前移，同事务；历史
    回放撤销候选后回到旧值（字符串精确比较）。"""
    from app.data_channel.datasets.lake_store import rows_at_version
    from app.data_channel.datasets.models import (
        DatasetChangeset, DatasetChangesetRow,
    )

    ds = lake_curated_approved["ds"]
    base = lake_curated_approved["v1"]
    review = PL.create_candidate_review(
        ReviewService(db), db, ds.id, base_version_id=str(base.id),
        proposal_key="proposal-write:cf-lake-2",
        edits=[{"row_pk": "L-1", "field_name": "amount",
                "old_value": "150.7500", "new_value": "157.7500"}],
        reviewer_id="u-admin")
    PL.approve_candidate_review(db, review)
    db.expire_all()
    current = _lake_rows(db, ds.id)
    assert next(r for r in current if r["ord_no"] == "L-1")["amount"] == \
        "157.7500"
    candidate = latest_dataset_version(db, ds.id)
    assert candidate.checksum
    # 变更集由原生 upsert_run 产生（updated 计数≥1）
    changeset = db.query(DatasetChangeset).filter_by(
        version_id=candidate.id).one()
    assert changeset.updated_count >= 1
    row = db.query(DatasetChangesetRow).filter_by(
        changeset_id=changeset.id).one()
    assert row.change_type == "updated"
    assert row.old_row["amount"] == "150.7500"
    assert row.new_row["amount"] == "157.7500"
    at_base = rows_at_version(db, ds, base.version_no)
    assert next(r for r in at_base if r["ord_no"] == "L-1")["amount"] == \
        "150.7500"


def test_lake_candidate_reject_keeps_everything(db, lake_curated_approved):
    from app.data_channel.datasets.lake_store import rows_at_version

    ds = lake_curated_approved["ds"]
    base = lake_curated_approved["v1"]
    review = PL.create_candidate_review(
        ReviewService(db), db, ds.id, base_version_id=str(base.id),
        proposal_key="proposal-write:cf-lake-3",
        edits=[{"row_pk": "L-2", "field_name": "stat",
                "old_value": "new", "new_value": "gone"}],
        reviewer_id="u-admin")
    PL.reject_candidate_review(db, review)
    db.expire_all()
    assert latest_dataset_version(db, ds.id).id == base.id
    assert next(r for r in _lake_rows(db, ds.id)
                if r["ord_no"] == "L-2")["stat"] == "new"
    assert next(r for r in rows_at_version(db, ds, base.version_no)
                if r["ord_no"] == "L-2")["stat"] == "new"


def test_lake_same_row_multi_field_accumulates(db, lake_curated_approved):
    """同一行多字段（decimal + string）编辑全部保留；顺序无关。"""
    ds = lake_curated_approved["ds"]
    base = lake_curated_approved["v1"]
    edits = [
        {"row_pk": "L-1", "field_name": "amount",
         "old_value": "150.7500", "new_value": "160.5000"},
        {"row_pk": "L-1", "field_name": "stat",
         "old_value": "pending", "new_value": "shipped"},
    ]
    review = PL.create_candidate_review(
        ReviewService(db), db, ds.id, base_version_id=str(base.id),
        proposal_key="proposal-write:cf-multi-1",
        edits=edits, reviewer_id="u-admin")
    PL.approve_candidate_review(db, review)
    db.expire_all()
    row = next(r for r in _lake_rows(db, ds.id) if r["ord_no"] == "L-1")
    assert row["amount"] == "160.5000"
    assert row["stat"] == "shipped"

    # 新基线上反向顺序再验（链式候选，顺序无关）
    new_base = latest_dataset_version(db, ds.id)
    edits2 = [
        {"row_pk": "L-1", "field_name": "stat",
         "old_value": "shipped", "new_value": "done"},
        {"row_pk": "L-1", "field_name": "amount",
         "old_value": "160.5000", "new_value": "170.2500"},
    ]
    review2 = PL.create_candidate_review(
        ReviewService(db), db, ds.id, base_version_id=str(new_base.id),
        proposal_key="proposal-write:cf-multi-2",
        edits=list(reversed(edits2)), reviewer_id="u-admin")
    PL.approve_candidate_review(db, review2)
    db.expire_all()
    row2 = next(r for r in _lake_rows(db, ds.id) if r["ord_no"] == "L-1")
    assert row2["amount"] == "170.2500"
    assert row2["stat"] == "done"


def test_lake_two_candidates_second_approve_conflicts_zero_increment(
        db, lake_curated_approved):
    """同 base 两个候选：先批者前移指针，后批者 409 且零版本增量。"""
    ds = lake_curated_approved["ds"]
    base = lake_curated_approved["v1"]
    first = PL.create_candidate_review(
        ReviewService(db), db, ds.id, base_version_id=str(base.id),
        proposal_key="proposal-write:cf-two-1",
        edits=[{"row_pk": "L-1", "field_name": "stat",
                "old_value": "pending", "new_value": "a"}],
        reviewer_id="u-admin")
    second = PL.create_candidate_review(
        ReviewService(db), db, ds.id, base_version_id=str(base.id),
        proposal_key="proposal-write:cf-two-2",
        edits=[{"row_pk": "L-1", "field_name": "stat",
                "old_value": "pending", "new_value": "b"}],
        reviewer_id="u-admin")
    PL.approve_candidate_review(db, first)
    db.expire_all()
    versions = db.query(DatasetVersion).filter_by(dataset_id=ds.id).count()
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        PL.approve_candidate_review(db, second)
    assert excinfo.value.detail["code"] == "curated_base_superseded"
    db.expire_all()
    assert db.query(DatasetVersion).filter_by(
        dataset_id=ds.id).count() == versions  # 零增量
    assert db.query(CuratedReview).filter_by(id=second.id).one().status == \
        "pending"
    assert next(r for r in _lake_rows(db, ds.id)
                if r["ord_no"] == "L-1")["stat"] == "a"


def test_lake_pipeline_advance_invalidates_candidate_approval(
        db, lake_curated_approved):
    """pending 期间流水线产新版本（指针前移）→ 候选批准 409 不继承。"""
    from app.data_channel.datasets import lake_store
    from app.data_channel.datasets.lock import dataset_write_lock

    ds = lake_curated_approved["ds"]
    base = lake_curated_approved["v1"]
    review = PL.create_candidate_review(
        ReviewService(db), db, ds.id, base_version_id=str(base.id),
        proposal_key="proposal-write:cf-pipe",
        edits=[{"row_pk": "L-2", "field_name": "stat",
                "old_value": "new", "new_value": "gone"}],
        reviewer_id="u-admin")
    with dataset_write_lock(f"dataset::{ds.id}", bind=db.get_bind()):
        lake_store.upsert_run(db, ds, [
            {"ord_no": "L-1", "stat": "pending", "amount": "150.7500",
             "shipped_at": "2026-10-01"},
            {"ord_no": "L-2", "stat": "new", "amount": "", "shipped_at": ""},
        ], "upsert", ["ord_no"])
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        PL.approve_candidate_review(db, review)
    assert excinfo.value.detail["code"] == "curated_base_superseded"


def test_lake_approved_version_backed_for_reads(db, lake_curated_approved):
    """批准后 version_review 认可新版本：require_current_version_approved
    通过（A-12 批准后查询可见的治理前提）。"""
    from app.data_channel.curated.review_row_identity import (
        require_current_version_approved,
    )

    ds = lake_curated_approved["ds"]
    base = lake_curated_approved["v1"]
    review = PL.create_candidate_review(
        ReviewService(db), db, ds.id, base_version_id=str(base.id),
        proposal_key="proposal-write:cf-readback",
        edits=[{"row_pk": "L-1", "field_name": "stat",
                "old_value": "pending", "new_value": "shipped"}],
        reviewer_id="u-admin")
    PL.approve_candidate_review(db, review)
    db.expire_all()
    approved = require_current_version_approved(db, ds.id)
    assert approved.id == review.id
    assert approved.dataset_version_id == review.dataset_version_id


def test_lake_transaction_failure_no_residue(db, lake_curated_approved,
                                              monkeypatch):
    """approve 物理应用阶段失败：rollback 零版本/零变更集/零审核绑定残留。"""
    from app.data_channel.datasets import lake_store as LS

    ds = lake_curated_approved["ds"]
    base = lake_curated_approved["v1"]
    review = PL.create_candidate_review(
        ReviewService(db), db, ds.id, base_version_id=str(base.id),
        proposal_key="proposal-write:cf-crashlake",
        edits=[{"row_pk": "L-1", "field_name": "stat",
                "old_value": "pending", "new_value": "x"}],
        reviewer_id="u-admin")

    def _boom(*a, **k):
        raise RuntimeError("simulated physical apply failure")

    from app.data_channel.datasets.lake_store import upsert_run as real_upsert

    def failing_upsert(db, dataset, rows, mode, pk, **k):
        # 让正式版本/变更集 flush 后、外层 commit 前失败
        real_upsert(db, dataset, rows, mode, pk, _defer_commit=True)
        raise RuntimeError("simulated post-flush failure")

    monkeypatch.setattr(LS, "upsert_run", failing_upsert)
    with pytest.raises(Exception):
        PL.approve_candidate_review(db, review)
    db.rollback()
    db.expire_all()
    assert db.query(DatasetVersion).filter_by(
        dataset_id=ds.id).count() == 1  # 零版本残留
    assert db.query(CuratedReview).filter_by(id=review.id).one().status == \
        "pending"
    assert review.dataset_version_id is None  # 未绑定
    assert next(r for r in _lake_rows(db, ds.id)
                if r["ord_no"] == "L-1")["stat"] == "pending"


def test_legacy_blob_null_update_rejected_before_review(
        db, curated_approved):
    """迁移前 blob/CSV base 的字段置空编辑：创建候选前 422（零 Review/
    RowEdit），避免批准时才失败。"""
    from fastapi import HTTPException

    service = ReviewService(db)
    with pytest.raises(HTTPException) as excinfo:
        PL.create_candidate_review(
            service, db, "ds-c1",
            base_version_id=str(curated_approved["v1"].id),
            proposal_key="proposal-write:cf-null",
            edits=[{"row_pk": "SO-1", "field_name": "status",
                    "old_value": "pending", "new_value": None}],
            reviewer_id="u-admin")
    assert excinfo.value.detail["code"] == "legacy_null_unrepresentable"
    db.rollback()
    assert db.query(CuratedReview).filter_by(
        proposal_key="proposal-write:cf-null").count() == 0
    assert db.query(CuratedRowEdit).count() == 0
