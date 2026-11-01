"""审核行身份与版本门禁原语（叶子模块）。

从 approved_version_reader 下沉：行编辑叠加/流式读取（row_edit_overlay）
与既有导入表面（approved_version_reader 组合层）共用同一套身份/门禁口径，
独立成叶以避免读取层内部互相成环。行主键编码是前端契约：
单主键纯文本，复合主键紧凑 JSON 数组（row_pk_encoding='json-array'）。
"""
from __future__ import annotations

import json
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.datasets.lake_gate import split_pk
from app.data_channel.curated.models import CuratedDataset, CuratedReview


class ReviewApprovalError(ValueError):
    """当前数据版本尚未审批，或审批已经因新版本产生而过期。"""


def latest_dataset_version(db: Session, dataset_id: str):
    """curated 域的当前版本入口（委托 datasets 权威指针解析，语义同源）。"""
    from app.data_channel.datasets.version_pointer import (
        resolve_current_version,
    )

    return resolve_current_version(db, dataset_id)


def _as_aware(value):
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def review_matches_version(review: CuratedReview, version) -> bool:
    """判断审核是否属于指定版本，并安全兼容迁移前的空版本记录。

    新审核始终通过 ``dataset_version_id`` 精确绑定。历史空版本审核仅能背书
    审核创建时已存在的版本；版本创建时间晚于审核时，旧审核自动失效。
    """
    if version is None:
        return review.dataset_version_id is None
    if review.dataset_version_id:
        return review.dataset_version_id == version.id
    # 受控动作未绑定候选（pending，dataset_version_id 为空）：精确匹配其
    # base 版本 — 行编辑 overlay 既有通道可为 base 提供 preview；批准回填
    # dataset_version_id 后回到精确匹配，为新版本提供审批背书。
    if review.base_version_id is not None:
        return review.base_version_id == version.id
    review_at = _as_aware(review.created_at)
    version_at = _as_aware(version.created_at)
    return bool(review_at and version_at and review_at >= version_at)


def current_version_review(db: Session, dataset_id: str, *, status: str | None = None):
    """返回当前 DatasetVersion 的最新有效审核；没有则返回 ``None``。"""
    version = latest_dataset_version(db, dataset_id)
    return version_review(db, dataset_id, version, status=status)


def version_review(
    db: Session,
    dataset_id: str,
    version,
    *,
    status: str | None = None,
):
    """返回指定不可变版本的最新审核决定或待办。

    历史部署曾允许同一版本重复发起审核，因此治理读取必须服从时间上最新的
    匹配记录；不能跳过较新的 rejected/pending 去命中更早的 approved。
    """
    reviews = (db.query(CuratedReview)
               .filter(CuratedReview.curated_dataset_id == dataset_id)
               .order_by(CuratedReview.created_at.desc()).all())
    for review in reviews:
        # 未绑定的受控动作候选不遮挡当前版本治理（pending 不得顶掉
        # backing approved；批准后已精确绑定，正常参与）。
        if (review.dataset_version_id is None
                and review.base_version_id is not None):
            continue
        if not review_matches_version(review, version):
            continue
        # ``status`` 是对最新治理记录的断言，不是允许跳过新决定、向历史
        # 搜索某种状态的选择器。
        return review if status is None or review.status == status else None
    return None


def require_current_version_approved(db: Session, dataset_id: str) -> CuratedReview:
    """要求当前数据版本已经审批通过，否则硬失败。"""
    version = latest_dataset_version(db, dataset_id)
    if version is None:
        raise ReviewApprovalError(f"数据集 {dataset_id} 尚无可审批的数据版本")
    return require_version_approved(db, dataset_id, version)


def require_version_approved(db: Session, dataset_id: str, version) -> CuratedReview:
    """要求指定不可变版本已审批，避免检查和读数之间切换版本。"""
    review = version_review(db, dataset_id, version)
    if review is None or review.status != "approved":
        raise ReviewApprovalError(
            f"数据集 {dataset_id} 当前版本 v{version.version_no} 尚未审批通过；"
            f"旧版本审批不会自动继承到新版本")
    return review


def _dataset_schema(db: Session, dataset_id: str) -> dict:
    """读取权威 v2 dataset schema；legacy 表仅作只读兼容回退。"""
    from app.data_channel.datasets.models import Dataset

    dataset = db.query(Dataset).filter(
        Dataset.id == dataset_id, Dataset.kind == "curated").first()
    if dataset:
        return dict(dataset.schema_json or {})
    legacy = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
    return dict(legacy.schema_json or {}) if legacy else {}


def _field_contract(schema: dict, field_name: str) -> dict:
    return next((
        item for item in (schema.get("contract_definitions") or [])
        if str(item.get("field_key") or "") == field_name
    ), {})


def _field_type(schema: dict, field_name: str) -> str:
    contract = _field_contract(schema, field_name)
    if contract.get("field_type"):
        return str(contract["field_type"]).lower()
    typed = next((
        item for item in (schema.get("columns_typed") or [])
        if str(item.get("name") or "") == field_name
    ), {})
    return str(typed.get("type") or "string").lower()


def _coerce_review_value(schema: dict, field_name: str, value):
    """把审核表中的文本值恢复为发布契约声明的运行时类型。"""
    if value is None:
        return None
    expected = _field_type(schema, field_name)
    if expected == "string":
        return str(value)
    if expected == "json":
        return value if isinstance(value, (dict, list)) else json.loads(str(value))
    if expected == "integer":
        return int(str(value).strip().replace(",", ""))
    if expected == "float":
        return float(str(value).strip().replace(",", ""))
    if expected == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "yes", "1"}
    # timestamp 仍以 ISO 文本进入 Parquet/投影，与流水线原始时间列保持一致。
    return value


def dataset_pk_columns(db: Session, dataset_id: str) -> list[str]:
    """审核行身份只认 schema 中固化的真实主键，支持复合主键。"""
    return split_pk(_dataset_schema(db, dataset_id).get("primary_key"))


def encode_row_pk(row: dict, pk_cols: list[str], *, dataset_name: str = "") -> str:
    """把真实主键编码成审核 ``row_pk``。

    单主键沿用纯字符串；复合主键使用紧凑 JSON 数组，避免业务值中的分隔符
    造成碰撞。
    """
    if not pk_cols:
        raise ValueError(
            f"数据集「{dataset_name}」未声明主键，无法安全定位审核编辑行。"
            f"请先在流水线数据契约中声明主键并重新入湖。")
    values: list[str] = []
    for column in pk_cols:
        value = row.get(column)
        if value is None or str(value).strip() == "":
            raise ValueError(
                f"数据集「{dataset_name}」的审核行缺少主键列「{column}」或值为空，"
                f"无法应用行级编辑。")
        values.append(str(value).strip())
    if len(values) == 1:
        return values[0]
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def normalize_row_pk(value, pk_cols: list[str], *, dataset_name: str = "") -> str:
    """规整 API 提交的行键；复合键接受 JSON 数组或按列名给出的对象。"""
    if not pk_cols:
        raise HTTPException(409, detail={
            "code": "review_primary_key_required",
            "message": f"数据集「{dataset_name}」未声明主键，无法安全编辑具体行。"
                       f"请先在流水线数据契约中声明主键并重新入湖。",
        })
    parsed = value
    if isinstance(value, str) and len(pk_cols) > 1:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            raise HTTPException(400, detail={
                "code": "invalid_composite_row_pk",
                "message": f"复合主键 {pk_cols} 的 row_pk 必须是 JSON 数组或对象。",
            }) from None
    if isinstance(parsed, dict):
        row = {column: parsed.get(column) for column in pk_cols}
    elif isinstance(parsed, (list, tuple)):
        if len(parsed) != len(pk_cols):
            raise HTTPException(400, detail={
                "code": "invalid_composite_row_pk",
                "message": f"row_pk 提供了 {len(parsed)} 个值，但主键需要 "
                           f"{len(pk_cols)} 列 {pk_cols}。",
            })
        row = dict(zip(pk_cols, parsed))
    elif len(pk_cols) == 1:
        row = {pk_cols[0]: parsed}
    else:
        raise HTTPException(400, "复合主键 row_pk 格式非法")
    try:
        return encode_row_pk(row, pk_cols, dataset_name=dataset_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


def _version_by_id(db: Session, dataset_id: str, version_id: str | None):
    if not version_id:
        return latest_dataset_version(db, dataset_id)
    from app.data_channel.datasets.models import DatasetVersion

    return db.query(DatasetVersion).filter(
        DatasetVersion.id == version_id,
        DatasetVersion.dataset_id == dataset_id,
    ).first()
