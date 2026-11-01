"""受控动作成品行编辑的原子提交策略（确认协议资产写专用）。

与 ``ReviewService.start_review`` + ``batch_edit_rows`` 组合语义一致，但整段
在同一 Dataset 行锁下执行且只在最后提交一次：

- 冻结版本 CAS（expected_version_id 必须等于当前最新版本）；
- 行/字段校验先于任何 CuratedRowEdit 写入执行；
- 复用或新建 pending review 与全部编辑一次提交；
- 任何拒绝路径（版本漂移、行不存在、字段契约不符、终端态审核）最终
  rollback 后抛出 — 事务结束时零持久结果（零 review 壳 / 零编辑 / 零
  状态变化），即使调用方随后的审计落账提交也不会带出半成品。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from fastapi import HTTPException

from app.data_channel.curated.approved_version_reader import (
    normalize_row_pk,
    review_matches_version,
)
from app.data_channel.curated.models import CuratedReview, CuratedRowEdit
from app.data_channel.curated.review_row_identity import dataset_pk_columns
from app.data_channel.datasets import cache as lake_cache


def apply_proposal_edits(
        service, db: Session, curated_dataset_id: str, *,
        expected_version_id: str, edits: list[dict],
        reviewer_id: str | None) -> CuratedReview:
    """见模块 docstring；``service`` 是 ReviewService 实例（复用其助手）。"""
    from app.data_channel.curated.models import (
        PostCommitVerificationError,
    )
    from app.data_channel.datasets.models import Dataset, DatasetVersion

    committed = False
    try:
        dataset = (db.query(Dataset).filter(
            Dataset.id == curated_dataset_id,
            Dataset.kind == "curated",
        ).with_for_update(of=Dataset).populate_existing().first())
        if dataset is None:
            service._get_dataset_or_raise(curated_dataset_id)
        version = None
        if dataset is not None and dataset.latest_version_id:
            version = db.query(DatasetVersion).filter(
                DatasetVersion.id == dataset.latest_version_id,
                DatasetVersion.dataset_id == curated_dataset_id,
            ).first()
        if version is None:
            from app.data_channel.curated.review_row_identity import (
                latest_dataset_version,
            )
            version = latest_dataset_version(db, curated_dataset_id)
        if version is None:
            raise HTTPException(409, detail={
                "code": "dataset_has_no_version",
                "message": "该成品数据集尚无可审核版本，请先成功执行流水线入湖。",
            })
        if str(version.id) != str(expected_version_id):
            raise HTTPException(409, detail={
                "code": "review_version_conflict",
                "message": (
                    f"数据集已更新到 v{version.version_no}（确认冻结的是另一"
                    "版本），编辑未提交；请基于新版本重新准备确认"),
                "current_version_id": version.id,
                "current_version_no": version.version_no,
            })

        existing = (db.query(CuratedReview).filter(
            CuratedReview.curated_dataset_id == curated_dataset_id,
        ).order_by(CuratedReview.created_at.desc()).all())
        existing = [
            review for review in existing
            if review_matches_version(review, version)
        ]
        terminal = next((
            review for review in existing
            if review.status in {"approved", "rejected"}
        ), None)
        if terminal is not None:
            decision_label = (
                "已通过" if terminal.status == "approved" else "已拒绝")
            raise HTTPException(409, detail={
                "code": "review_version_already_decided",
                "message": (
                    f"当前数据版本 v{version.version_no} {decision_label}，"
                    "审核决定不可重开；请由流水线产生新版本后再发起审核。"
                ),
                "dataset_version_id": version.id,
                "review_id": terminal.id,
                "status": terminal.status,
            })
        pending = next((
            review for review in existing if review.status == "pending"
        ), None)
        # 校验用 transient review（未入会话）：全部行/字段校验先于任何
        # 持久化发生；通过后才复用或新建 pending review。
        probe = CuratedReview(
            curated_dataset_id=curated_dataset_id,
            dataset_version_id=version.id,
            reviewer_id=reviewer_id,
            status="pending",
        )
        prepared = _validate_edits(service, probe, edits)
        review = pending
        created = review is None
        if review is None:
            review = probe
            db.add(review)
            db.flush()
        for source, row_pk, field_name in prepared:
            db.add(CuratedRowEdit(
                review_id=review.id,
                row_pk=row_pk,
                field_name=field_name,
                old_value=source.get("old_value"),
                new_value=source.get("new_value"),
            ))
        if created:
            service._set_dataset_status(curated_dataset_id, "in_review")
        db.commit()
        committed = True
        db.refresh(review)
        lake_cache.invalidate_overview()
        return review
    except Exception:
        if not committed:
            # commit 前的可验证拒绝（版本漂移/行不存在/字段契约）一律
            # 回滚：零 review 壳、零编辑、零状态变化。
            db.rollback()
            raise
        # commit 后异常：rollback 无法撤销已提交事务 — 按 pending 版本
        # 只读恢复真实回执；恢复失败按 unknown 上抛，绝不吞成零写。
        try:
            db.rollback()
            recovered = (db.query(CuratedReview).filter(
                CuratedReview.curated_dataset_id == curated_dataset_id,
                CuratedReview.dataset_version_id == version.id,
                CuratedReview.status == "pending",
            ).order_by(CuratedReview.created_at.desc()).first())
        except Exception:  # noqa: BLE001
            recovered = None
        if recovered is not None:
            return recovered
        raise PostCommitVerificationError(
            "行编辑事务已提交但回执读回失败，需人工对账") from None


def _validate_edits(service, review, edits: list[dict]):
    pk_cols = dataset_pk_columns(service._db, review.curated_dataset_id)
    dataset_name = service._dataset_name(review.curated_dataset_id)
    prepared: list[tuple[dict, str, str]] = []
    for source in edits:
        field_name = str(source.get("field_name") or "").strip()
        if not field_name:
            raise HTTPException(400, "field_name 不能为空")
        if field_name in pk_cols:
            raise HTTPException(409, detail={
                "code": "primary_key_edit_forbidden",
                "message": f"列「{field_name}」属于主键 {pk_cols}，"
                           "审核编辑不能改变行身份。",
            })
        prepared.append((
            source,
            normalize_row_pk(source.get("row_pk"), pk_cols,
                             dataset_name=dataset_name),
            field_name,
        ))
    rows_by_pk = service._assert_row_keys_exist(
        review, [item[1] for item in prepared], pk_cols)
    for source, row_pk, field_name in prepared:
        service._validate_edit_contract(
            review, field_name, source.get("new_value"),
            rows_by_pk.get(row_pk) if rows_by_pk else None,
        )
    return prepared
