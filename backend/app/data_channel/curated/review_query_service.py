"""Read-model assembly for curated dataset review comparisons.

The HTTP router owns request validation and dependency injection.  This module
owns the version selection, row-edit overlay, delta calculation, and canonical
row-identity rules that form the review-diff business workflow.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.curated.approved_version_reader import (
    apply_all_row_edits,
    apply_row_edits_to_batch,
    encode_row_pk,
    review_matches_version,
)
from app.data_channel.datasets import lake_store
from app.data_channel.datasets.lake_gate import split_pk
from app.data_channel.datasets.models import DatasetChangeset, DatasetChangesetRow
from app.data_channel.pipeline_tasks.merge import _slim_row, compute_lake_impact
from app.data_channel.curated.models import CuratedReview
from app.data_channel.datasets.models import Dataset
from app.data_channel.datasets.service import DatasetReadError, DatasetService


def _delta_from_changeset(
    db: Session,
    version,
    primary_key_columns: list[str],
    *,
    sample_limit: int = 200,
) -> dict | None:
    """湖表版本的 delta 直接取其变更集（计数 + 逐行样本）。

    形状对齐 merge.compute_lake_impact；样本按 row_pk 排序（确定性），slim
    截断沿用 merge._slim_row。变更集缺失（异常状态）返回 None 由调用方回退
    内存计算。
    """
    changeset = db.query(DatasetChangeset).filter(
        DatasetChangeset.version_id == version.id).first()
    if changeset is None:
        return None

    def _sample(change_type: str) -> list:
        return (db.query(DatasetChangesetRow)
                .filter(DatasetChangesetRow.changeset_id == changeset.id,
                        DatasetChangesetRow.change_type == change_type)
                .order_by(DatasetChangesetRow.row_pk)
                .limit(sample_limit)
                .all())

    added = _sample("added")
    updated = _sample("updated")
    deleted = _sample("deleted")
    total_after = int(version.rowcount or 0)
    return {
        "keyed_by": list(primary_key_columns) if primary_key_columns else None,
        "total_before": total_after - changeset.added_count + changeset.deleted_count,
        "total_after": total_after,
        "added_count": changeset.added_count,
        "updated_count": changeset.updated_count,
        "deleted_count": changeset.deleted_count,
        "unchanged_count": max(
            0, total_after - changeset.added_count - changeset.updated_count),
        "added_sample": [_slim_row(r.new_row or {}) for r in added],
        "updated_sample": [{"before": _slim_row(r.old_row or {}),
                            "after": _slim_row(r.new_row or {})}
                           for r in updated],
        "deleted_sample": [_slim_row(r.old_row or {}) for r in deleted],
        "sample_truncated": (changeset.added_count > sample_limit
                             or changeset.updated_count > sample_limit
                             or changeset.deleted_count > sample_limit),
    }


def build_review_diff(
    db: Session,
    dataset_id: str,
    *,
    limit: int,
    offset: int,
    review_id: str | None,
) -> dict:
    """Build the current/previous/delta review views for one curated dataset."""
    dataset = (
        db.query(Dataset)
        .filter(Dataset.id == dataset_id, Dataset.kind == "curated")
        .first()
    )
    if not dataset:
        raise HTTPException(404, "Curated dataset not found")

    schema = dataset.schema_json if isinstance(dataset.schema_json, dict) else {}
    primary_key_columns = split_pk(schema.get("primary_key"))

    dataset_service = DatasetService(db)
    versions = dataset_service.list_versions(dataset_id)
    empty_page = {
        "version_no": None,
        "total": 0,
        "rows": [],
        "offset": offset,
        "limit": limit,
        "has_more": False,
    }
    if not versions:
        return {
            "pk": primary_key_columns,
            "row_pk_encoding": (
                "plain-string" if len(primary_key_columns) == 1 else "json-array"
            ),
            "current_row_pks": [],
            "current": empty_page,
            "previous": empty_page,
            "delta": None,
        }

    selected_review = None
    if review_id:
        selected_review = (
            db.query(CuratedReview)
            .filter(
                CuratedReview.id == review_id,
                CuratedReview.curated_dataset_id == dataset_id,
            )
            .first()
        )
        if not selected_review:
            raise HTTPException(404, "Review not found for this dataset")
    else:
        # Prefer the version bound to the pending review.  Silently switching to
        # a newer snapshot would make an approver review a different data set.
        selected_review = (
            db.query(CuratedReview)
            .filter(
                CuratedReview.curated_dataset_id == dataset_id,
                CuratedReview.status == "pending",
            )
            .order_by(CuratedReview.created_at.desc())
            .first()
        )

    from app.data_channel.datasets.version_pointer import (
        resolve_current_version,
    )

    # 「最新」按权威指针解析：pending 候选以更高 version_no 入库但不前移
    # 指针，不得被审核 diff 元数据当作当前版本。
    latest_version = resolve_current_version(db, dataset_id) or versions[-1]
    current_version = latest_version
    if selected_review and selected_review.dataset_version_id:
        current_version = next(
            (
                version
                for version in versions
                if version.id == selected_review.dataset_version_id
            ),
            None,
        )
        if current_version is None:
            raise HTTPException(
                409,
                detail={
                    "code": "review_version_unavailable",
                    "message": "该审核绑定的数据版本已不存在或不可用，不能继续审核。",
                    "dataset_version_id": selected_review.dataset_version_id,
                },
            )
    elif selected_review:
        # Reviews created before dataset_version_id existed are pinned by their
        # creation time rather than being rebound to today's latest version.
        historical_versions = [
            version
            for version in versions
            if review_matches_version(selected_review, version)
        ]
        if historical_versions:
            current_version = historical_versions[-1]

    current_index = versions.index(current_version)
    previous_version = versions[current_index - 1] if current_index > 0 else None

    current_is_lake = lake_store.version_uses_lake(dataset, current_version)
    pending_review_id = selected_review.id if selected_review else None

    def _overlay_batch(
        rows: list[dict],
        version,
        *,
        include_review_id: str | None,
    ) -> list[dict]:
        """按批叠加审核编辑（窗口语义）：编辑在保存时已校验存在性，且版本
        内容不可变，因此窗口外不做全量未命中校验（与预览/导出同一口径）。"""
        return apply_row_edits_to_batch(
            db,
            dataset_id,
            rows,
            dataset_version_id=version.id,
            include_review_id=include_review_id,
        )

    def _full_rows_with_overlay(
        version,
        *,
        include_review_id: str | None,
    ) -> list[dict]:
        """旧路径整版读取（严格叠加，未命中硬失败）：仅 blob 版本与变更集
        异常兜底使用，湖表正常路径不再为分页整版物化。"""
        if lake_store.version_uses_lake(dataset, version):
            raw = lake_store.rows_at_version(db, dataset, version.version_no)
        else:
            raw = dataset_service.load_all_rows(dataset_id, version.version_no)
        return apply_all_row_edits(
            db,
            dataset_id,
            raw,
            dataset_version_id=version.id,
            include_review_id=include_review_id,
        )

    def _build_paged_view(
        version,
        *,
        is_latest: bool,
        include_review_id: str | None,
    ) -> tuple[int, list[dict]]:
        """返回 (total, 当前窗口行)。湖表版本真分页只物化窗口；blob 版本
        整版读取后切片（保持遗留行为）。"""
        if not lake_store.version_uses_lake(dataset, version):
            full = _full_rows_with_overlay(
                version, include_review_id=include_review_id)
            return len(full), full[offset : offset + limit]
        if is_latest:
            # 最新湖表版本的当前态即物理表：SQL 真分页，O(page)
            total = lake_store.count_rows(db, dataset)
            page = lake_store.page_rows(db, dataset, offset, limit)
        else:
            page, total = lake_store.page_rows_at_version(
                db, dataset, version.version_no, offset, limit)
        page = _overlay_batch(
            page, version, include_review_id=include_review_id)
        return total, page

    try:
        current_total, current_page = _build_paged_view(
            current_version,
            is_latest=(latest_version.id == current_version.id),
            include_review_id=pending_review_id,
        )
        if previous_version is None:
            previous_total, previous_page = 0, []
        else:
            previous_total, previous_page = _build_paged_view(
                previous_version,
                is_latest=False,
                include_review_id=None,
            )

        # 湖表版本的 delta 直接取该版本的变更集（版本间真实差异，O(变化行)）；
        # blob 版本或变更集异常缺失时回退内存对拍，并让全量结果直接充当分页
        # 视图（与旧行为逐字段一致）
        delta = None
        if current_is_lake:
            delta = _delta_from_changeset(
                db, current_version, primary_key_columns)
        if delta is None:
            current_rows = _full_rows_with_overlay(
                current_version, include_review_id=pending_review_id)
            previous_rows = (
                _full_rows_with_overlay(previous_version, include_review_id=None)
                if previous_version
                else []
            )
            delta = compute_lake_impact(
                previous_rows,
                current_rows,
                primary_key_columns,
                sample_limit=200,
            )
            current_total = len(current_rows)
            current_page = current_rows[offset : offset + limit]
            previous_total = len(previous_rows)
            previous_page = previous_rows[offset : offset + limit]
    except DatasetReadError as exc:
        raise HTTPException(422, f"版本数据读取失败：{exc}") from exc
    except lake_store.LakeStoreError as exc:
        raise HTTPException(422, f"版本数据读取失败：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "review_edit_identity_error",
                "message": str(exc),
            },
        ) from exc
    current_row_primary_keys: list[str | None] = []
    if primary_key_columns:
        for row in current_page:
            try:
                # Return the same canonical encoding consumed by review writes;
                # browsers must not reimplement Python value stringification.
                current_row_primary_keys.append(
                    encode_row_pk(
                        row,
                        primary_key_columns,
                        dataset_name=dataset.name,
                    )
                )
            except ValueError:
                # Keep the version visible while disabling edits only for rows
                # whose identity is incomplete.
                current_row_primary_keys.append(None)

    return {
        "pk": primary_key_columns,
        "row_pk_encoding": (
            "plain-string" if len(primary_key_columns) == 1 else "json-array"
        ),
        "current_row_pks": current_row_primary_keys,
        "current": {
            "version_no": current_version.version_no,
            "dataset_version_id": current_version.id,
            "total": current_total,
            "rows": current_page,
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < current_total,
        },
        "previous": {
            "version_no": (
                previous_version.version_no if previous_version else None
            ),
            "total": previous_total,
            "rows": previous_page,
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < previous_total,
        },
        "delta": delta,
        "review": (
            {
                "id": selected_review.id,
                "dataset_version_id": selected_review.dataset_version_id,
                "status": selected_review.status,
                "stale": not review_matches_version(
                    selected_review,
                    latest_version,
                ),
                "latest_dataset_version_id": latest_version.id,
                "latest_version_no": latest_version.version_no,
            }
            if selected_review
            else None
        ),
    }
