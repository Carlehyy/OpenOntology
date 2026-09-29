"""人工审核服务 — 行级编辑、版本合并、审核状态管理。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.curated.approved_version_reader import (
    ReviewApprovalError,
    _as_aware,
    _coerce_review_value,
    _dataset_schema,
    _field_contract,
    _field_type,
    _version_by_id,
    apply_all_row_edits,
    current_version_review,
    dataset_pk_columns,
    encode_row_pk,
    latest_dataset_version,
    load_all_rows_with_edits,
    load_rows_with_edits,
    normalize_row_pk,
    require_current_version_approved,
    require_version_approved,
    review_matches_version,
    version_review,
)
from app.data_channel.curated.models import CuratedDataset, CuratedReview, CuratedRowEdit

logger = logging.getLogger(__name__)


class ReviewService:

    def __init__(self, db: Session):
        self._db = db

    def start_review(
        self,
        curated_dataset_id: str,
        reviewer_id: str | None = None,
    ) -> CuratedReview:
        """为当前不可变 DatasetVersion 新建或复用 pending 审核。"""
        from app.data_channel.datasets.models import Dataset, DatasetVersion

        # Dataset 是版本发布与审核发起共享的串行点：create_version 更新
        # latest_version_id 时也会锁该行。这样既防止同版本并发创建两条审核，
        # 也不会在新版本恰好发布时为旧版本误开审核。
        dataset = (self._db.query(Dataset).filter(
            Dataset.id == curated_dataset_id,
            Dataset.kind == "curated",
        ).with_for_update(of=Dataset).populate_existing().first())
        if dataset is None:
            # 保留 legacy 资产尚未迁移时原有的、可操作的错误信息。
            self._get_dataset_or_raise(curated_dataset_id)
        version = None
        if dataset is not None and dataset.latest_version_id:
            version = self._db.query(DatasetVersion).filter(
                DatasetVersion.id == dataset.latest_version_id,
                DatasetVersion.dataset_id == curated_dataset_id,
            ).first()
        if version is None:
            version = (self._db.query(DatasetVersion)
                       .filter(DatasetVersion.dataset_id == curated_dataset_id)
                       .order_by(DatasetVersion.version_no.desc()).first())
        if version is None:
            raise HTTPException(409, detail={
                "code": "dataset_has_no_version",
                "message": "该成品数据集尚无可审核版本，请先成功执行流水线入湖。",
            })

        existing = (self._db.query(CuratedReview).filter(
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
            decision_label = "已通过" if terminal.status == "approved" else "已拒绝"
            raise HTTPException(409, detail={
                "code": "review_version_already_decided",
                "message": (
                    f"当前数据版本 v{version.version_no} {decision_label}，审核决定不可重开；"
                    "请由流水线产生新版本后再发起审核。"
                ),
                "dataset_version_id": version.id,
                "review_id": terminal.id,
                "status": terminal.status,
            })
        pending = next((
            review for review in existing if review.status == "pending"
        ), None)
        if pending is not None:
            return pending

        review = CuratedReview(
            curated_dataset_id=curated_dataset_id,
            dataset_version_id=version.id,
            reviewer_id=reviewer_id,
            status="pending",
        )
        self._db.add(review)
        self._set_dataset_status(curated_dataset_id, "in_review")
        self._db.commit()
        self._db.refresh(review)
        return review

    def edit_row(
        self,
        review_id: str,
        row_pk: str,
        field_name: str,
        old_value: str | None,
        new_value: str | None,
    ) -> CuratedRowEdit:
        """记录单行单字段修改，并验证版本、主键及目标行。"""
        review = self._get_review_for_mutation_or_raise(review_id)
        self._ensure_pending(review)
        self._assert_review_version_current(review, bind_legacy=True)
        pk_cols = dataset_pk_columns(self._db, review.curated_dataset_id)
        row_pk = normalize_row_pk(
            row_pk, pk_cols, dataset_name=self._dataset_name(review.curated_dataset_id))
        if field_name in pk_cols:
            raise HTTPException(409, detail={
                "code": "primary_key_edit_forbidden",
                "message": f"列「{field_name}」属于主键 {pk_cols}，审核编辑不能改变行身份。"
                           f"如需变更主键，请修改流水线源数据并重新入湖。",
            })
        rows_by_pk = self._assert_row_keys_exist(review, [row_pk], pk_cols)
        self._validate_edit_contract(
            review, field_name, new_value,
            rows_by_pk.get(row_pk) if rows_by_pk else None,
        )

        edit = CuratedRowEdit(
            review_id=review_id,
            row_pk=row_pk,
            field_name=field_name,
            old_value=old_value,
            new_value=new_value,
        )
        self._db.add(edit)
        self._db.commit()
        self._db.refresh(edit)
        return edit

    def batch_edit_rows(self, review_id: str, edits: list[dict]) -> list[CuratedRowEdit]:
        """原子保存一批行级编辑。"""
        review = self._get_review_for_mutation_or_raise(review_id)
        self._ensure_pending(review)
        self._assert_review_version_current(review, bind_legacy=True)
        pk_cols = dataset_pk_columns(self._db, review.curated_dataset_id)
        dataset_name = self._dataset_name(review.curated_dataset_id)
        prepared: list[tuple[dict, str, str]] = []
        for source in edits:
            field_name = str(source.get("field_name") or "").strip()
            if not field_name:
                raise HTTPException(400, "field_name 不能为空")
            if field_name in pk_cols:
                raise HTTPException(409, detail={
                    "code": "primary_key_edit_forbidden",
                    "message": f"列「{field_name}」属于主键 {pk_cols}，审核编辑不能改变行身份。",
                })
            prepared.append((
                source,
                normalize_row_pk(source.get("row_pk"), pk_cols, dataset_name=dataset_name),
                field_name,
            ))
        rows_by_pk = self._assert_row_keys_exist(
            review, [item[1] for item in prepared], pk_cols)
        for source, row_pk, field_name in prepared:
            self._validate_edit_contract(
                review, field_name, source.get("new_value"),
                rows_by_pk.get(row_pk) if rows_by_pk else None,
            )

        results: list[CuratedRowEdit] = []
        for source, row_pk, field_name in prepared:
            edit = CuratedRowEdit(
                review_id=review_id,
                row_pk=row_pk,
                field_name=field_name,
                old_value=source.get("old_value"),
                new_value=source.get("new_value"),
            )
            self._db.add(edit)
            results.append(edit)
        self._db.commit()
        return results

    def approve(self, review_id: str, notes: str = "",
                reviewer_id: str | None = None) -> CuratedReview:
        """批准当前版本审核，并原子写入下游自动化 outbox。"""
        review = self._get_review_for_mutation_or_raise(review_id)
        self._ensure_pending(review)
        self._assert_review_version_current(review, bind_legacy=True)
        review.status = "approved"
        review.notes = notes
        review.decided_at = datetime.now(timezone.utc)
        # 决定人写在决定当时并覆盖发起人：复用 pending 审核时实际拍板的可能
        # 是另一个人，库里这一列是「谁批的」唯一权威审计记录。
        if reviewer_id is not None:
            review.reviewer_id = reviewer_id
        self._set_dataset_status(review.curated_dataset_id, "approved")
        # 审核决定与下游自动灌入意图必须在同一事务提交。过去这里提交后由
        # Router 直接 ``Celery.delay``；进程在两步之间退出、或 broker 接受
        # 但任务未被 worker 消费时，会留下“已审核”却永远没有本体对账的
        # 不可恢复状态。复用 DatasetVersion 事件 outbox，让调度器在完整
        # Mapping + Sentinel barrier 成功后才确认事件。
        if review.dataset_version_id:
            from app.data_channel.datasets.version_event_outbox import (
                enqueue_curated_review_approved,
            )
            enqueue_curated_review_approved(
                self._db,
                dataset_id=review.curated_dataset_id,
                dataset_version_id=review.dataset_version_id,
            )
        self._db.commit()
        self._db.refresh(review)
        # 数据库列是「谁批的」权威审计记录；info 日志是排障附属（日志会
        # 轮转，不能作为审计依据）。
        logger.info(
            "curated review approved: review=%s dataset=%s reviewer=%s",
            review.id, review.curated_dataset_id, review.reviewer_id,
        )
        return review

    def reject(self, review_id: str, notes: str = "",
               reviewer_id: str | None = None) -> CuratedReview:
        """拒绝当前版本审核。"""
        review = self._get_review_for_mutation_or_raise(review_id)
        self._ensure_pending(review)
        self._assert_review_version_current(review, bind_legacy=True)
        review.status = "rejected"
        review.notes = notes
        review.decided_at = datetime.now(timezone.utc)
        if reviewer_id is not None:
            review.reviewer_id = reviewer_id
        self._set_dataset_status(review.curated_dataset_id, "rejected")
        self._db.commit()
        self._db.refresh(review)
        logger.info(
            "curated review rejected: review=%s dataset=%s reviewer=%s",
            review.id, review.curated_dataset_id, review.reviewer_id,
        )
        return review

    def get_edits(self, review_id: str) -> list[CuratedRowEdit]:
        return self._db.query(CuratedRowEdit).filter(
            CuratedRowEdit.review_id == review_id).all()

    def apply_edits_to_snapshot(
        self,
        review_id: str,
        original_data: list[dict],
    ) -> list[dict]:
        """将审核编辑应用到内存快照，不修改底层版本文件。"""
        review = self._get_review_or_raise(review_id)
        edits = self.get_edits(review_id)
        if not edits:
            return original_data
        pk_cols = dataset_pk_columns(self._db, review.curated_dataset_id)
        if not pk_cols:
            raise HTTPException(409, detail={
                "code": "review_primary_key_required",
                "message": "数据集未声明主键，无法安全应用行级编辑。",
            })

        edit_map: dict[str, dict[str, str | None]] = {}
        for edit in edits:
            edit_map.setdefault(edit.row_pk, {})[edit.field_name] = edit.new_value

        schema = _dataset_schema(self._db, review.curated_dataset_id)
        result: list[dict] = []
        matched_keys: set[str] = set()
        for source_row in original_data:
            try:
                row_pk = encode_row_pk(
                    source_row, pk_cols,
                    dataset_name=self._dataset_name(review.curated_dataset_id))
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            row = source_row
            if row_pk in edit_map:
                matched_keys.add(row_pk)
                row = dict(source_row)
                for field, value in edit_map[row_pk].items():
                    row[field] = _coerce_review_value(schema, field, value)
            result.append(row)

        unmatched = sorted(set(edit_map) - matched_keys)
        if unmatched:
            raise HTTPException(409, detail={
                "code": "review_row_not_found",
                "message": f"审核编辑中的行主键 {unmatched[:5]} 不存在于绑定版本。"
                           f"请刷新审核页面后重新选择行。",
            })
        return result

    def _dataset_name(self, dataset_id: str) -> str:
        dataset = self._get_dataset_or_raise(dataset_id)
        return str(getattr(dataset, "name", dataset_id) or dataset_id)

    @staticmethod
    def _ensure_pending(review: CuratedReview) -> None:
        if review.status != "pending":
            raise HTTPException(409, detail={
                "code": "review_already_decided",
                "message": f"审核已处于 {review.status} 状态，不能继续编辑或重复决定。",
            })

    def _assert_review_version_current(
        self,
        review: CuratedReview,
        *,
        bind_legacy: bool = False,
    ) -> None:
        """阻止用旧版本审核结果背书或修改当前资产。"""
        latest = latest_dataset_version(self._db, review.curated_dataset_id)
        if latest is not None and review_matches_version(review, latest):
            # 迁移前 pending 审核没有不可变版本外键。决定/编辑事务已经先锁住
            # Dataset 行，此处可安全地把它绑定到刚校验的 latest version，确保
            # 批准时一定能原子生成带精确版本身份的自动化 outbox。
            if bind_legacy and review.dataset_version_id is None:
                review.dataset_version_id = latest.id
            return
        raise HTTPException(409, detail={
            "code": "review_version_stale",
            "message": "审核发起后数据集已产生新版本，本次审核仅对应旧版本，"
                       "不能继续保存、批准或拒绝。请为最新版本重新发起审核。",
            "review_dataset_version_id": review.dataset_version_id,
            "latest_dataset_version_id": latest.id if latest else None,
            "latest_version_no": latest.version_no if latest else None,
        })

    def _assert_row_keys_exist(
        self,
        review: CuratedReview,
        row_pks: list[str],
        pk_cols: list[str],
    ) -> dict[str, dict]:
        """验证编辑目标行真实存在于审核绑定版本。"""
        if not row_pks or not review.dataset_version_id:
            return {}
        from app.data_channel.datasets.models import DatasetVersion
        from app.data_channel.datasets.service import DatasetReadError, DatasetService

        version = self._db.query(DatasetVersion).filter(
            DatasetVersion.id == review.dataset_version_id,
            DatasetVersion.dataset_id == review.curated_dataset_id,
        ).first()
        if not version:
            raise HTTPException(409, detail={
                "code": "review_version_unavailable",
                "message": "审核绑定的数据版本已不存在，不能继续提交编辑。",
            })
        try:
            # 湖表版本（且即当前最新版）按主键分批 IN 校验，不再全量物化；
            # blob 版本或历史湖表版本走全量严格读（含变更集回放）兜底
            from app.data_channel.curated.approved_version_reader import (
                latest_dataset_version,
            )
            from app.data_channel.datasets import lake_store
            from app.data_channel.datasets.models import Dataset

            dataset = self._db.query(Dataset).filter(
                Dataset.id == review.curated_dataset_id).first()
            latest = latest_dataset_version(self._db, review.curated_dataset_id)
            # 物理表当前态即审核绑定版本（即最新版）时按主键分批 IN 校验；
            # lake_columns 映射是湖表资产已建表的权威标记（缺失=未建表/异常，
            # 回退全量读由 load_all_rows 的湖表分流兜底）
            use_lake = (
                dataset is not None and latest is not None
                and latest.id == version.id
                and lake_store.version_uses_lake(dataset, version)
                and bool((dataset.schema_json or {}).get("lake_columns")))
            if use_lake:
                existing = lake_store.rows_by_pks(self._db, dataset, list(row_pks))
            else:
                rows = DatasetService(self._db).load_all_rows(
                    review.curated_dataset_id, version.version_no)
                existing = {
                    encode_row_pk(row, pk_cols, dataset_name=review.curated_dataset_id): row
                    for row in rows
                }
        except DatasetReadError as exc:
            raise HTTPException(422, f"审核版本数据读取失败：{exc}") from None
        except lake_store.LakeStoreError as exc:
            raise HTTPException(422, f"审核版本数据读取失败：{exc}") from None
        missing = sorted(set(row_pks) - set(existing))
        if missing:
            raise HTTPException(409, detail={
                "code": "review_row_not_found",
                "message": f"行主键 {missing[:5]} 不存在于审核绑定的数据版本。"
                           f"请刷新审核页面后重新选择行。",
            })
        return existing

    def _validate_edit_contract(
        self,
        review: CuratedReview,
        field_name: str,
        new_value,
        source_row: dict | None,
    ) -> None:
        """审核修改只能改真实列，且新值服从资产湖固化的逻辑类型。"""
        schema = _dataset_schema(self._db, review.curated_dataset_id)
        known = {str(c) for c in (schema.get("columns") or [])}
        if source_row:
            known.update(str(c) for c in source_row.keys())
        if known and field_name not in known:
            raise HTTPException(400, detail={
                "code": "review_unknown_field",
                "message": f"字段「{field_name}」不在审核绑定的数据版本中，不能通过 API 新增任意列。",
            })
        contract = _field_contract(schema, field_name)
        if contract and contract.get("nullable") is False and (
            new_value is None
            or (isinstance(new_value, str) and not new_value.strip())
        ):
            raise HTTPException(400, detail={
                "code": "review_null_forbidden",
                "message": f"字段「{field_name}」的发布契约不允许为空，审核修改不能清空该值。",
            })
        from app.data_channel.datasets.lake_gate import (
            LakeGateError, validate_declared_types)
        try:
            validate_declared_types(
                [{field_name: new_value}], schema.get("columns_typed"),
                dataset_name=self._dataset_name(review.curated_dataset_id),
            )
        except LakeGateError as exc:
            raise HTTPException(400, detail={
                "code": "review_type_mismatch",
                "message": str(exc),
            }) from None

    def _get_dataset_or_raise(self, dataset_id: str):
        """统一资产表是审核唯一权威源；legacy 表仅只读兼容。"""
        from app.data_channel.datasets.models import Dataset

        dataset = self._db.query(Dataset).filter(
            Dataset.id == dataset_id, Dataset.kind == "curated").first()
        if dataset:
            return dataset
        legacy = self._db.query(CuratedDataset).filter(
            CuratedDataset.id == dataset_id).first()
        if legacy:
            raise HTTPException(409, detail={
                "code": "legacy_curated_not_migrated",
                "message": "该成品数据集仍属于旧资产表，尚未迁移到统一数据资产湖，"
                           "不能发起新审核。",
            })
        raise HTTPException(404, f"Curated dataset {dataset_id} not found")

    def _set_dataset_status(self, dataset_id: str, status: str) -> None:
        """审核状态写入统一资产元数据；legacy 表只做兼容同步。"""
        from app.data_channel.datasets.models import Dataset

        dataset = self._db.query(Dataset).filter(
            Dataset.id == dataset_id, Dataset.kind == "curated").first()
        if dataset:
            schema = dict(dataset.schema_json or {})
            schema["review_status"] = status
            dataset.schema_json = schema
        legacy = self._db.query(CuratedDataset).filter(
            CuratedDataset.id == dataset_id).first()
        if legacy:
            legacy.status = status

    def _get_review_or_raise(self, review_id: str) -> CuratedReview:
        review = self._db.query(CuratedReview).filter(
            CuratedReview.id == review_id).first()
        if not review:
            raise HTTPException(404, f"Review {review_id} not found")
        return review

    def _get_review_for_mutation_or_raise(
        self,
        review_id: str,
    ) -> CuratedReview:
        """按 Dataset → Review 的固定顺序锁定审核写事务。

        Dataset.latest_version_id 是版本发布的终局写点。先锁 Dataset，随后锁
        Review，既让审批/拒绝只能有一个终局结果，也保证版本漂移校验与决定
        提交之间不会插入一个新版本。``populate_existing`` 很重要：请求可能
        已在 identity map 缓存 pending 状态，等待锁后必须以数据库最新值覆盖。
        """
        from app.data_channel.datasets.models import Dataset

        dataset = (self._db.query(Dataset)
                   .join(
                       CuratedReview,
                       CuratedReview.curated_dataset_id == Dataset.id,
                   )
                   .filter(
                       CuratedReview.id == review_id,
                       Dataset.kind == "curated",
                   )
                   .with_for_update(of=Dataset)
                   .populate_existing()
                   .first())
        if dataset is None:
            raise HTTPException(404, f"Review {review_id} not found")

        review = (self._db.query(CuratedReview)
                  .filter(CuratedReview.id == review_id)
                  .with_for_update(of=CuratedReview)
                  .populate_existing()
                  .first())
        if review is None:
            raise HTTPException(404, f"Review {review_id} not found")
        return review
