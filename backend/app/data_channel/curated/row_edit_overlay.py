"""审核行编辑叠加与分批流式读取。

行身份/门禁原语在 review_row_identity；本模块是「已批准编辑如何叠加到
数据行」的唯一实现：全量语义（apply_all_row_edits，含未命中硬失败）供
审核 diff 等全量场景；分批语义（apply_row_edits_to_batch /
iter_rows_with_edits）供预览/导出/映射等分页流式场景。物理湖表版本经
lake_store 分批流式读取，历史 blob 版本保持全量读取 + 全量叠加。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.data_channel.curated.review_row_identity import (
    ReviewApprovalError,
    _coerce_review_value,
    _dataset_schema,
    _version_by_id,
    dataset_pk_columns,
    encode_row_pk,
    latest_dataset_version,
    require_version_approved,
    review_matches_version,
)
from app.data_channel.curated.models import CuratedReview, CuratedRowEdit


def _edits_map_for_version(
    db: Session,
    dataset_id: str,
    version,
    *,
    include_review_id: str | None = None,
) -> dict[str, dict[str, str | None]]:
    """匹配版本的（已批准 + include_review_id 预览）审核行编辑映射。

    正式读取默认只应用已批准审核，避免 pending/rejected 修改泄漏到本体映射；
    审核详情通过 ``include_review_id`` 额外预览当前 pending 审核。历史空版本
    审核仍按创建时间与版本匹配，不能永久污染后续版本。编辑按审核时间升序
    合并，同一 (row_pk, field) 后者覆盖前者。
    """
    reviews = (db.query(CuratedReview)
               .filter(CuratedReview.curated_dataset_id == dataset_id)
               .order_by(CuratedReview.created_at.asc()).all())
    reviews = [review for review in reviews
               if review_matches_version(review, version)
               and (review.status == "approved" or review.id == include_review_id)]
    if not reviews:
        return {}
    edits = (db.query(CuratedRowEdit)
             .filter(CuratedRowEdit.review_id.in_([r.id for r in reviews]))
             .order_by(CuratedRowEdit.edited_at.asc()).all())
    edit_map: dict[str, dict[str, str | None]] = {}
    for edit in edits:
        edit_map.setdefault(edit.row_pk, {})[edit.field_name] = edit.new_value
    return edit_map


def _apply_edit_map(
    db: Session,
    dataset_id: str,
    rows: list[dict],
    edit_map: dict[str, dict[str, str | None]],
) -> tuple[list[dict], set[str]]:
    """把编辑映射叠加到数据行（仅就地改值），返回 (叠加后的行, 命中的 row_pk)。"""
    pk_cols = dataset_pk_columns(db, dataset_id)
    if not pk_cols:
        raise ValueError(
            f"数据集 {dataset_id} 存在行级审核编辑，但 schema 未声明主键；"
            f"系统拒绝猜测 id 或第一列，以免把修改应用到错误行。")
    schema = _dataset_schema(db, dataset_id)
    output: list[dict] = []
    matched_keys: set[str] = set()
    for source_row in rows:
        row_pk = encode_row_pk(source_row, pk_cols, dataset_name=dataset_id)
        patch = edit_map.get(row_pk)
        row = source_row
        if patch:
            matched_keys.add(row_pk)
            row = dict(source_row)
            for field, value in patch.items():
                # NULL 是单元格值，不是“从这一行删除 schema 字段”。
                row[field] = _coerce_review_value(schema, field, value)
        output.append(row)
    return output, matched_keys


def apply_all_row_edits(
    db: Session,
    dataset_id: str,
    rows: list[dict],
    *,
    dataset_version_id: str | None = None,
    include_review_id: str | None = None,
) -> list[dict]:
    """把指定版本的审核编辑叠加到数据行。

    全量语义：任何无法在绑定版本中定位的编辑都会抛错（不静默忽略）。
    分页/流式读取请用 apply_row_edits_to_batch。
    """
    if not rows:
        return rows
    version = _version_by_id(db, dataset_id, dataset_version_id)
    if dataset_version_id and version is None:
        raise ValueError(f"数据集 {dataset_id} 的版本 {dataset_version_id} 不存在")
    edit_map = _edits_map_for_version(
        db, dataset_id, version, include_review_id=include_review_id)
    if not edit_map:
        return rows

    output, matched_keys = _apply_edit_map(db, dataset_id, rows, edit_map)
    unmatched = sorted(set(edit_map) - matched_keys)
    if unmatched:
        raise ValueError(
            f"数据集 {dataset_id} 的审核编辑包含无法在绑定版本中定位的行主键 "
            f"{unmatched[:5]}。请刷新审核页面后按 schema 主键重新提交；"
            f"系统不会把未命中的编辑静默忽略。")
    return output


def apply_row_edits_to_batch(
    db: Session,
    dataset_id: str,
    rows: list[dict],
    *,
    dataset_version_id: str | None = None,
    include_review_id: str | None = None,
) -> list[dict]:
    """apply_all_row_edits 的分批等价物，供分页/流式读取（预览/导出/映射）。

    编辑目标在写入时已对绑定版本做过存在性校验（review_service.
    _assert_row_keys_exist），版本内容不可变，因此本批之外的编辑不算未命中，
    这里不做全量未命中校验。
    """
    if not rows:
        return rows
    version = _version_by_id(db, dataset_id, dataset_version_id)
    if dataset_version_id and version is None:
        raise ValueError(f"数据集 {dataset_id} 的版本 {dataset_version_id} 不存在")
    edit_map = _edits_map_for_version(
        db, dataset_id, version, include_review_id=include_review_id)
    if not edit_map:
        return rows
    output, _ = _apply_edit_map(db, dataset_id, rows, edit_map)
    return output


def iter_rows_with_edits(
    db: Session,
    dataset_id: str,
    *,
    require_approved: bool = False,
    version=None,
    batch_size: int = 5000,
):
    """分批产出叠加了已批准审核编辑的行（生成器）。

    curated 湖表版本经 lake_store.iter_rows_at_version 精确回放 + 批内叠加；历史
    blob 版本保持「全量读取 + 全量叠加（含未命中校验）」后分批产出。
    存储读取、校验或解析失败均抛错。
    """
    from app.data_channel.datasets.service import DatasetService

    version = version or latest_dataset_version(db, dataset_id)
    if version is None:
        if require_approved:
            raise ReviewApprovalError(f"数据集 {dataset_id} 尚无可审批的数据版本")
        return
    if require_approved:
        require_version_approved(db, dataset_id, version)

    from app.data_channel.datasets import lake_store
    from app.data_channel.datasets.models import Dataset

    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if dataset is not None and lake_store.version_uses_lake(dataset, version):
        for batch in lake_store.iter_rows_at_version(
                db, dataset, version.version_no, batch_size=batch_size):
            yield apply_row_edits_to_batch(
                db, dataset_id, batch, dataset_version_id=version.id)
        return
    rows = apply_all_row_edits(
        db, dataset_id,
        DatasetService(db).load_all_rows(dataset_id, version.version_no),
        dataset_version_id=version.id)
    for start in range(0, len(rows), batch_size):
        yield rows[start:start + batch_size]


def load_rows_with_edits(db: Session, dataset_id: str, limit: int = 10000) -> list[dict]:
    """严格读取最新版本并叠加属于该版本的已批准编辑。"""
    version = latest_dataset_version(db, dataset_id)
    if version is None:
        return []
    out: list[dict] = []
    for batch in iter_rows_with_edits(
            db, dataset_id, version=version,
            batch_size=max(1, min(5000, limit))):
        out.extend(batch)
        if len(out) >= limit:
            break
    return out[:limit]


def load_all_rows_with_edits(
    db: Session,
    dataset_id: str,
    *,
    require_approved: bool = False,
    version=None,
) -> list[dict]:
    """严格全量读取指定版本并叠加该版本的已批准审核编辑。

    Mapping/投影等生产消费方必须使用此入口；存储读取、校验或解析失败均抛错。
    """
    return [row for batch in iter_rows_with_edits(
        db, dataset_id, require_approved=require_approved,
        version=version) for row in batch]
