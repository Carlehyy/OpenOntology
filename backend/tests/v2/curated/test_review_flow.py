"""ReviewService 单元测试。"""
from unittest.mock import MagicMock, patch

from app.models.v2.curated import CuratedDataset, CuratedReview, CuratedRowEdit
from app.models.v2.dataset import Dataset, DatasetVersion
from app.services.v2.curated.review_service import ReviewService


def make_db_with_dataset(status: str = "pending_review"):
    db = MagicMock()
    dataset = Dataset(
        id="ds-1",
        name="test_dataset",
        kind="curated",
        schema_json={"primary_key": "id", "review_status": status},
    )
    version = DatasetVersion(
        id="ver-1", dataset_id="ds-1", version_no=1, rowcount=2)
    review = CuratedReview(
        id="rev-1",
        curated_dataset_id="ds-1",
        dataset_version_id=version.id,
        status="pending",
    )

    def query_side_effect(model):
        query = MagicMock()
        if model == Dataset:
            query.filter.return_value.first.return_value = dataset
            query.filter.return_value.with_for_update.return_value \
                .populate_existing.return_value.first.return_value = dataset
            query.join.return_value.filter.return_value \
                .with_for_update.return_value.populate_existing \
                .return_value.first.return_value = dataset
        elif model == CuratedDataset:
            query.filter.return_value.first.return_value = None
        elif model == CuratedReview:
            query.filter.return_value.first.return_value = review
            query.filter.return_value.with_for_update.return_value \
                .populate_existing.return_value.first.return_value = review
            query.filter.return_value.order_by.return_value.first.return_value = None
        elif model == CuratedRowEdit:
            query.filter.return_value.all.return_value = []
        elif model == DatasetVersion:
            query.filter.return_value.first.return_value = version
            query.filter.return_value.order_by.return_value.first.return_value = version
        return query

    db.query.side_effect = query_side_effect
    db.add = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    return db, dataset, version, review


def test_start_review_creates_version_bound_record():
    db, dataset, _, _ = make_db_with_dataset()
    service = ReviewService(db)

    review = service.start_review("ds-1")

    db.add.assert_called_once()
    db.commit.assert_called()
    assert review.status == "pending"
    assert review.dataset_version_id == "ver-1"
    assert dataset.schema_json["review_status"] == "in_review"


def test_approve_updates_review_and_canonical_status():
    db, dataset, _, review = make_db_with_dataset("in_review")

    result = ReviewService(db).approve("rev-1", notes="数据看起来不错")

    assert result is review
    assert review.status == "approved"
    assert dataset.schema_json["review_status"] == "approved"
    assert review.notes == "数据看起来不错"
    assert review.decided_at is not None


def test_reject_updates_review_and_canonical_status():
    db, dataset, _, review = make_db_with_dataset("in_review")

    ReviewService(db).reject("rev-1", notes="存在数据质量问题")

    assert review.status == "rejected"
    assert dataset.schema_json["review_status"] == "rejected"


def test_approve_records_reviewer_id_at_decision_time():
    """审批决定必须落决定人——reviewer_id 在决定发生时写入"""
    db, _, _, review = make_db_with_dataset("in_review")

    ReviewService(db).approve("rev-1", notes="ok", reviewer_id="user-admin-1")

    assert review.status == "approved"
    assert review.reviewer_id == "user-admin-1"
    assert review.decided_at is not None


def test_reject_records_reviewer_id_at_decision_time():
    """拒绝同样必须落决定人"""
    db, _, _, review = make_db_with_dataset("in_review")

    ReviewService(db).reject("rev-1", notes="不合格", reviewer_id="user-admin-2")

    assert review.status == "rejected"
    assert review.reviewer_id == "user-admin-2"


def test_decision_without_reviewer_keeps_existing_value():
    """未传 reviewer_id（兼容旧调用方）时不覆盖已有记录"""
    db, _, _, review = make_db_with_dataset("in_review")
    review.reviewer_id = "user-originator"

    ReviewService(db).approve("rev-1", notes="ok")

    assert review.reviewer_id == "user-originator"


@patch(
    "app.services.v2.dataset_service.DatasetService.load_all_rows",
    return_value=[{"id": "row-1", "name": "旧名称"}],
)
def test_edit_row_validates_real_primary_key_and_creates_record(_load_rows):
    db, _, _, _ = make_db_with_dataset()

    edit = ReviewService(db).edit_row(
        "rev-1", "row-1", "name", "旧名称", "新名称")

    assert edit.row_pk == "row-1"
    db.add.assert_called()
    db.commit.assert_called()


@patch(
    "app.services.v2.dataset_service.DatasetService.load_all_rows",
    return_value=[{"id": "1", "name": "A"}, {"id": "2", "age": "25"}],
)
def test_batch_edit_rows_validates_all_target_rows(_load_rows):
    db, _, _, _ = make_db_with_dataset()
    edits = [
        {"row_pk": "1", "field_name": "name", "old_value": "A", "new_value": "B"},
        {"row_pk": "2", "field_name": "age", "old_value": "25", "new_value": "26"},
    ]

    results = ReviewService(db).batch_edit_rows("rev-1", edits)

    assert len(results) == 2
    assert db.add.call_count == 2


def test_apply_edits_to_snapshot_uses_declared_primary_key():
    db, _, _, _ = make_db_with_dataset()
    edits = [
        CuratedRowEdit(
            review_id="rev-1", row_pk="1", field_name="name",
            old_value="Alice", new_value="Alice Smith"),
        CuratedRowEdit(
            review_id="rev-1", row_pk="2", field_name="age",
            old_value="25", new_value=None),
    ]
    original_side_effect = db.query.side_effect

    def query_side_effect(model):
        query = original_side_effect(model)
        if model == CuratedRowEdit:
            query.filter.return_value.all.return_value = edits
        return query

    db.query.side_effect = query_side_effect
    original = [
        {"id": "1", "name": "Alice", "age": "30"},
        {"id": "2", "name": "Bob", "age": "25"},
    ]

    result = ReviewService(db).apply_edits_to_snapshot("rev-1", original)

    assert result[0]["name"] == "Alice Smith"
    assert result[1]["age"] is None


def test_get_edits_empty():
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []

    assert ReviewService(db).get_edits("rev-1") == []


def test_apply_edits_no_edits_returns_original():
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    original = [{"id": "1", "name": "Alice"}]

    assert ReviewService(db).apply_edits_to_snapshot("rev-1", original) == original
