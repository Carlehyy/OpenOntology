"""受控动作的成品候选审核生命周期（最小 B' 合同）。

动作基于「已 approved 的不可变 base 版本」提交待审变更：

- pending = 仅 CuratedReview(base_version_id, proposal_key,
  dataset_version_id=NULL) + CuratedRowEdit — 零 DatasetVersion、零变更
  集、零物理湖表变更；当前指针与读值不受影响（preview 经既有行编辑
  overlay 通道叠加在 base 上）；
- ``proposal_key = proposal-write:{confirmation_id}`` 持久唯一关联支撑幂等
  （重复确认只读复用真实终态：pending/approved/rejected 分别映射
  pending_review/applied/failed，不一律当作 pending）与只读恢复；
- approve：dataset_write_lock → Dataset 行锁 → base CAS（当前指针必须仍
  指向 base，源新版本已前移则显式 409、旧审批不继承）→ 复用原生
  upsert_run(_defer_commit=True)（lake 正门：typed 物理应用 + 正式版本 +
  变更集同事务）或 create_version(_defer_commit=True)（迁移前 blob base）→
  review 回填 dataset_version_id、status=approved、指针随发布路径前移、
  既有 curated_review_approved outbox —— 单一 commit 收口；
- reject：仅 review 终态化；不写湖、不动指针、不动版本；资产元数据状态
  按当前版本真实治理回写（version_review 跳过未绑定候选，不被 pending
  糊改）；
- 异常边界以 commit 为界：commit 前可验证拒绝 rollback（零审核壳零编辑
  零版本）；commit 后按 proposal_key 只读恢复真实回执，恢复失败抛
  :class:`PostCommitVerificationError`（上层按 unknown，绝不吞成零写）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.curated.models import (
    CuratedReview, CuratedRowEdit, PostCommitVerificationError,
)
from app.data_channel.curated.review_row_identity import (
    dataset_pk_columns,
    encode_row_pk,
    latest_dataset_version,
    require_version_approved,
)
from app.data_channel.datasets import cache as lake_cache
from app.data_channel.datasets.models import Dataset


def create_candidate_review(
        service, db: Session, dataset_id: str, *,
        base_version_id: str, proposal_key: str, edits: list[dict],
        reviewer_id: str | None) -> CuratedReview:
    """见模块 docstring；``service`` 为 ReviewService（复用其校验助手）。"""
    from app.data_channel.curated.proposal_edits import _validate_edits

    committed = False
    try:
        dataset = (db.query(Dataset).filter(
            Dataset.id == dataset_id, Dataset.kind == "curated",
        ).with_for_update(of=Dataset).populate_existing().first())
        if dataset is None:
            raise HTTPException(404, detail={
                "code": "curated_dataset_not_found",
                "message": f"成品数据集 {dataset_id} 不存在"})

        # 幂等：同一 confirmation 的候选已存在 → 只读复用真实状态。
        existing = db.query(CuratedReview).filter(
            CuratedReview.proposal_key == proposal_key).first()
        if existing is not None:
            if existing.curated_dataset_id != dataset_id:
                raise HTTPException(409, detail={
                    "code": "proposal_key_conflict",
                    "message": "proposal_key 已绑定到其它数据集的候选审核"})
            return existing

        base = latest_dataset_version(db, dataset_id)
        if base is None:
            raise HTTPException(409, detail={
                "code": "dataset_has_no_version",
                "message": "该成品数据集尚无可审核版本，请先成功执行流水线入湖。",
            })
        if str(base.id) != str(base_version_id):
            raise HTTPException(409, detail={
                "code": "review_version_conflict",
                "message": (
                    "数据集当前版本已变化（确认冻结的是另一基线），"
                    "候选未提交；请基于新版本重新准备确认"),
                "current_version_id": base.id,
                "current_version_no": base.version_no,
            })
        require_version_approved(db, dataset_id, base)

        # 校验先行（transient review，零持久痕迹）：行存在/字段契约/主键。
        probe = CuratedReview(
            curated_dataset_id=dataset_id,
            dataset_version_id=None,
            base_version_id=base.id,
            proposal_key=proposal_key,
            reviewer_id=reviewer_id, status="pending")
        prepared = _validate_edits(service, probe, edits)
        if not prepared:
            raise HTTPException(400, "没有任何待审编辑")

        # 迁移前 blob/CSV 形态无法忠实表示字段置空：在创建候选之前显式
        # 拒绝（零 Review/RowEdit），避免完整审核后批准才失败；lake base
        # 的 NULL 更新由原生湖表路径支持。
        from app.data_channel.datasets.lake_store import version_uses_lake

        if not version_uses_lake(dataset, base):
            null_fields = sorted({
                field_name for _src, _pk, field_name in prepared
                if any(e.get("field_name") == field_name
                       and e.get("new_value") is None for e in edits)})
            if null_fields:
                raise HTTPException(422, detail={
                    "code": "legacy_null_unrepresentable",
                    "message": (
                        f"字段 {null_fields} 的置空编辑无法在迁移前的 "
                        "CSV/整份快照形态上忠实表示；请先由流水线将数据"
                        "集入湖表后重试"),
                    "fields": null_fields,
                })

        review = CuratedReview(
            curated_dataset_id=dataset_id,
            dataset_version_id=None,
            base_version_id=base.id,
            proposal_key=proposal_key,
            reviewer_id=reviewer_id, status="pending")
        db.add(review)
        db.flush()
        for source, row_pk, field_name in prepared:
            db.add(CuratedRowEdit(
                review_id=review.id, row_pk=row_pk, field_name=field_name,
                old_value=source.get("old_value"),
                new_value=source.get("new_value")))
        _set_dataset_status(db, dataset_id, "in_review")
        db.commit()
        committed = True
        db.refresh(review)
        lake_cache.invalidate_overview()
        return review
    except Exception:
        if not committed:
            db.rollback()
            raise
        recovered = _recover_by_key(db, proposal_key)
        if recovered is not None:
            return recovered
        raise PostCommitVerificationError(
            "候选审核事务已提交但回执读回失败，需按 proposal_key 人工对账"
        ) from None


def _recover_by_key(db: Session, proposal_key: str) -> CuratedReview | None:
    try:
        db.rollback()
        return db.query(CuratedReview).filter(
            CuratedReview.proposal_key == proposal_key).first()
    except Exception:  # noqa: BLE001 - 恢复失败即 unknown
        return None


def candidate_merged_rows(db, dataset, base, review) -> list[dict]:
    """approve 用：当前物理/base 行 + 候选编辑 → 合并后整行（typed 文本）。"""
    from app.data_channel.datasets.service import DatasetService

    pk_cols = dataset_pk_columns(db, dataset.id)
    rows = DatasetService(db).load_all_rows(dataset.id)
    row_by_pk = {
        encode_row_pk(row, pk_cols,
                      dataset_name=str(dataset.name or dataset.id)): row
        for row in rows}
    edits = db.query(CuratedRowEdit).filter(
        CuratedRowEdit.review_id == review.id).all()
    merged: dict[str, dict] = {}
    for edit in edits:
        old_row = row_by_pk.get(edit.row_pk)
        if old_row is None:
            raise HTTPException(409, detail={
                "code": "row_not_found",
                "message": f"目标行 {edit.row_pk} 不在当前基线中，拒绝前移",
            })
        # 同行多字段累加（setdefault 持久行 dict，逐字段覆写）；行内顺序
        # 稳定，后写覆盖先写与规则 order 语义一致。
        merged.setdefault(edit.row_pk, dict(old_row))[edit.field_name] = \
            edit.new_value
    return list(merged.values())


def approve_candidate_review(db: Session, review: CuratedReview, *,
                             notes: str = "",
                             reviewer_id: str | None = None
                             ) -> CuratedReview:
    """批准候选：base CAS + 原生发布路径单事务收口（见模块 docstring）。"""
    from app.data_channel.datasets.lock import (
        DatasetLockTimeout, dataset_write_lock,
    )

    try:
        # 与 upsert_run 相同临界区纪律：dataset 写锁 → Dataset 行锁。
        with dataset_write_lock(
                f"dataset::{review.curated_dataset_id}",
                bind=db.get_bind(), wait_timeout=30):
            return _approve_candidate_locked(db, review, notes=notes,
                                             reviewer_id=reviewer_id)
    except DatasetLockTimeout as exc:
        raise HTTPException(423, detail={
            "code": "dataset_lock_timeout",
            "message": str(exc)}) from exc


def _approve_candidate_locked(db, review, *, notes, reviewer_id):
    committed = False
    try:
        from app.data_channel.datasets.models import DatasetVersion
        from app.data_channel.datasets.version_event_outbox import (
            enqueue_curated_review_approved,
        )

        dataset = (db.query(Dataset).filter(
            Dataset.id == review.curated_dataset_id,
        ).with_for_update(of=Dataset).populate_existing().first())
        if dataset is None:
            raise HTTPException(404, detail={
                "code": "curated_dataset_not_found",
                "message": "候选审核绑定的成品数据集已不存在"})
        current = latest_dataset_version(db, review.curated_dataset_id)
        if (current is None or not review.base_version_id
                or str(current.id) != str(review.base_version_id)):
            raise HTTPException(409, detail={
                "code": "curated_base_superseded",
                "message": (
                    "数据集当前版本已前移（流水线已产生新版本），"
                    "旧基线上的候选审批不继承；请基于新版本重新发起"),
                "current_version_id": current.id if current else None,
            })
        require_version_approved(db, dataset.id, current)
        if review.status != "pending":
            raise HTTPException(409, detail={
                "code": "review_not_pending",
                "message": f"候选审核已处理（当前状态 {review.status}）"})

        merged_rows = candidate_merged_rows(db, dataset, current, review)
        from app.data_channel.datasets.lake_store import (
            upsert_run, version_uses_lake,
        )

        if version_uses_lake(dataset, current):
            version, _changeset = upsert_run(
                db, dataset, merged_rows, "upsert",
                dataset_pk_columns(db, dataset.id),
                _defer_commit=True)
        else:
            from app.data_channel.datasets.service import (
                DatasetService, rows_to_csv_bytes,
            )

            all_rows = DatasetService(db).load_all_rows(dataset.id)
            pk_cols = dataset_pk_columns(db, dataset.id)
            dataset_name = str(dataset.name or dataset.id)
            merged_by_pk = {
                encode_row_pk(row, pk_cols, dataset_name=dataset_name): row
                for row in merged_rows}
            new_rows = [
                merged_by_pk.get(
                    encode_row_pk(row, pk_cols, dataset_name=dataset_name),
                    row) for row in all_rows]
            columns = list(new_rows[0].keys()) if new_rows else list(
                (dataset.schema_json or {}).get("columns") or [])
            version = DatasetService(db).create_version(
                dataset.id, rows_to_csv_bytes(new_rows, columns),
                rowcount=len(new_rows), _lock_held=True, _defer_commit=True)
        version = db.query(DatasetVersion).filter(
            DatasetVersion.id == version.id).one()

        review.dataset_version_id = version.id
        review.status = "approved"
        review.notes = notes
        review.decided_at = datetime.now(timezone.utc)
        if reviewer_id is not None:
            review.reviewer_id = reviewer_id
        _set_dataset_status(db, dataset.id, "approved")
        enqueue_curated_review_approved(
            db, dataset_id=dataset.id, dataset_version_id=version.id)
        db.commit()
        committed = True
        db.refresh(review)
        lake_cache.invalidate_overview()
        return review
    except Exception:
        if not committed:
            db.rollback()
            raise
        recovered = _recover_by_key(db, review.proposal_key or "")
        if recovered is not None and recovered.status == "approved":
            return recovered
        raise PostCommitVerificationError(
            "候选批准事务已提交但回执读回失败，需按 proposal_key 人工对账"
        ) from None


def reject_candidate_review(db: Session, review: CuratedReview, *,
                            notes: str = "",
                            reviewer_id: str | None = None
                            ) -> CuratedReview:
    """拒绝候选：仅 review 终态；湖表/指针/版本零变更；状态按真实治理回写。"""
    try:
        review.status = "rejected"
        review.notes = notes
        review.decided_at = datetime.now(timezone.utc)
        if reviewer_id is not None:
            review.reviewer_id = reviewer_id
        _sync_review_status_to_dataset(db, review.curated_dataset_id)
        db.commit()
        db.refresh(review)
        lake_cache.invalidate_overview()
        return review
    except Exception:
        db.rollback()
        raise


def _sync_review_status_to_dataset(db: Session, dataset_id: str) -> None:
    """资产元数据状态按当前版本最新治理记录如实回写（未绑定候选被跳过，
    不会被 pending 糊改），不无条件 approved。"""
    from app.data_channel.curated.review_row_identity import version_review

    current = latest_dataset_version(db, dataset_id)
    if current is None:
        return
    review = version_review(db, dataset_id, current)
    if review is None:
        return
    _set_dataset_status(db, dataset_id, str(review.status))


def _set_dataset_status(db: Session, dataset_id: str, status: str) -> None:
    """资产元数据审核状态写入（schema_json.review_status，与 ReviewService
    同一写法；legacy 表仅同步已迁移资产）。"""
    dataset = db.query(Dataset).filter(
        Dataset.id == dataset_id, Dataset.kind == "curated").first()
    if dataset is None:
        return
    schema = dict(dataset.schema_json or {})
    schema["review_status"] = status
    dataset.schema_json = schema
