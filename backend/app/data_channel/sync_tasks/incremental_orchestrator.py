"""增量更新编排器 — 触发链路：Connection→Pipeline→Curated→Ontology"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.ontologies.mappings.application import rebuild_ontology_projection

logger = logging.getLogger(__name__)


class IncrementalOrchestrator:
    """
    管理全链路增量更新触发逻辑：

    1. on_connection_sync  → 检测关联 Pipeline，触发增量运行
    2. on_pipeline_success → 更新 Curated Dataset，发出审核通知
    3. on_review_approved  → 检测关联 Mapping，自动触发增量 Mapping
    """

    def __init__(self, db: Session):
        self._db = db

    def on_dataset_version_published(
        self, dataset_id: str, dataset_version_id: str,
    ) -> dict:
        """Dispatch every downstream consumer of an immutable lake version.

        Ontology projection is an explicit subscription because manual data
        governance and transformation scheduling are independent concerns.
        Pipeline scheduling retains its existing task/connection trigger path;
        this event cannot accidentally enqueue the same PipelineRun twice after
        an outbox retry.  Curated datasets continue to wait for version-bound
        review approval.
        """
        from app.data_channel.datasets.automation_policy import (
            manual_dataset_automation_eligibility,
        )
        from app.data_channel.datasets.models import Dataset, DatasetVersion
        from app.ontologies.mappings.models import OntologyLinkMapping, OntologyMapping

        dataset = self._db.query(Dataset).filter(
            Dataset.id == dataset_id).first()
        version = self._db.query(DatasetVersion).filter(
            DatasetVersion.id == dataset_version_id,
            DatasetVersion.dataset_id == dataset_id,
        ).first()
        if dataset is None or version is None:
            return {"status": "skipped", "reason": "dataset_or_version_not_found"}

        eligible, reason = manual_dataset_automation_eligibility(dataset, version)
        if not eligible:
            return {
                "status": "completed",
                "dataset_id": dataset_id,
                "dataset_version_id": dataset_version_id,
                "manual_mapping": {"status": "skipped", "reason": reason},
            }

        object_mappings = self._db.query(OntologyMapping).filter(
            OntologyMapping.curated_dataset_id == dataset_id,
            OntologyMapping.status != "disabled",
        ).all()
        ontology_ids = {
            mapping.ontology_id for mapping in object_mappings
            if bool((mapping.field_mapping or {}).get("__auto_apply_on_version__"))
        }
        link_mappings = self._db.query(OntologyLinkMapping).filter(
            or_(
                OntologyLinkMapping.src_dataset_id == dataset_id,
                OntologyLinkMapping.tgt_dataset_id == dataset_id,
                OntologyLinkMapping.edge_dataset_id == dataset_id,
            ),
            OntologyLinkMapping.status.in_(("active", "inferred")),
        ).all()
        ontology_ids.update(
            mapping.ontology_id for mapping in link_mappings
            if bool((mapping.field_mapping or {}).get("__auto_apply_on_version__"))
        )

        applied = []
        for ontology_id in sorted(ontology_ids):
            projection = rebuild_ontology_projection(
                self._db,
                ontology_id,
                require_approved=True,
            )
            sentinel_dispatch = projection.get("sentinel_dispatch") or {}
            # A replay after a process crash may find no relational delta even
            # though the previous attempt died before sentinel evaluation.  In
            # that case (or after an immediate dispatch exception), perform a
            # full edge-state-safe evaluation before acknowledging the durable
            # version event.  SentinelMatchState keeps notifications/actions
            # idempotent when the affected CDC run already succeeded.
            if sentinel_dispatch.get("errors"):
                raise RuntimeError(
                    f"本体 {ontology_id} durable Sentinel 级联失败，"
                    "拒绝用一次新的手动扫描覆盖失败证据："
                    f"{sentinel_dispatch['errors']}")
            if not sentinel_dispatch.get("runs"):
                from app.ontologies.sentinels.engine import run_manual
                sentinel_dispatch = run_manual(self._db, ontology_id)
            if sentinel_dispatch.get("errors"):
                raise RuntimeError(
                    f"本体 {ontology_id} 哨兵评估存在 "
                    f"{sentinel_dispatch['errors']} 个错误，拒绝确认数据版本事件")
            applied.append({
                "ontology_id": ontology_id,
                "total_entities": projection.get("total_entities", 0),
                "total_relations": projection.get("total_relations", 0),
                "sentinel_dispatch": sentinel_dispatch,
            })

        return {
            "status": "completed",
            "dataset_id": dataset_id,
            "dataset_version_id": dataset_version_id,
            "manual_mapping": {
                "status": "applied" if applied else "no_subscribers",
                "ontologies": applied,
            },
        }

    # ── 触发点 1：Connection 同步完成 ────────────────────────────────

    def on_connection_sync(self, connection_id: str, dataset_id: str) -> dict:
        """
        数据连接同步完成后：
        - 找到以该 dataset 为输入的所有 Pipeline
        - 若 Pipeline.spec.trigger.on_dataset_version = true，触发增量运行
        """
        from app.data_channel.pipelines.models import Pipeline

        triggered = []
        # 与任务池/链式触发同一道双闸：只有「已发布 + 已启用」的流水线才允许
        # 被自动触发入湖。旧过滤条件 status != "disabled" 是 0008 生命周期收敛
        # 前的遗留值（收敛后恒真），等于草稿/停用流水线也会被自动执行
        pipelines = self._db.query(Pipeline).filter(
            Pipeline.source_dataset_id == dataset_id,
        ).filter(
            Pipeline.status == "published",
            Pipeline.enabled.isnot(False),  # NULL（老数据）视为启用
        ).all()

        for pl in pipelines:
            spec = pl.spec or {}
            trigger_config = spec.get("trigger", {})
            if trigger_config.get("on_dataset_version", False):
                run_id = self._trigger_pipeline(pl.id, mode="incremental")
                if run_id:
                    triggered.append({"pipeline_id": pl.id, "run_id": run_id})
                    logger.info(f"增量触发 Pipeline {pl.id}，run_id={run_id}")

        return {"triggered_pipelines": triggered, "dataset_id": dataset_id}

    # ── 触发点 2：Pipeline 运行成功 ───────────────────────────────────

    def on_pipeline_success(self, pipeline_run_id: str) -> dict:
        """
        Pipeline 运行成功后：
        - 找到关联的 Curated Dataset
        - 将其状态重置为 pending_review（供人工审核）
        - 记录增量标记
        """
        from app.data_channel.pipelines.models import PipelineRun, Pipeline
        from app.data_channel.curated.models import CuratedDataset

        run = self._db.query(PipelineRun).filter(PipelineRun.id == pipeline_run_id).first()
        if not run or run.status != "success":
            return {"status": "skipped", "reason": "run not found or not success"}

        pipeline = self._db.query(Pipeline).filter(Pipeline.id == run.pipeline_id).first()
        if not pipeline:
            return {"status": "skipped", "reason": "pipeline not found"}

        updated_datasets = []
        target_ids = pipeline.target_curated_ids or []

        for ds_id in target_ids:
            ds = self._db.query(CuratedDataset).filter(CuratedDataset.id == ds_id).first()
            if ds and ds.status == "approved":
                # 已审核的数据集有新增量，重置为待审核
                ds.status = "pending_review"
                ds.updated_at = datetime.now(timezone.utc)
                updated_datasets.append(ds_id)
                logger.info(f"Curated Dataset {ds_id} 有新增量，重置为 pending_review")

        self._db.commit()
        return {"updated_datasets": updated_datasets, "pipeline_run_id": pipeline_run_id}

    # ── 触发点 3：审核通过 ─────────────────────────────────────────────

    def on_review_approved(
        self, review_id: str, *, synchronous: bool = False,
    ) -> dict:
        """
        Curated Dataset 审核通过后：
        - 找到显式订阅该 Dataset 审批事件的对象/关系 Mapping
        - 每个本体只触发一次全量 Mapping 对账

        ``__auto_apply_on_review__`` 与 ``__auto_apply_on_version__`` 是两种
        不同的业务事件，不能互相替代：前者消费 curated 审批，后者消费人工
        数据版本发布。无显式审批订阅时保持不触发。
        """
        from app.data_channel.curated.models import CuratedReview
        from app.ontologies.mappings.models import (
            OntologyLinkMapping,
            OntologyMapping,
        )

        review = self._db.query(CuratedReview).filter(CuratedReview.id == review_id).first()
        if not review or review.status != "approved":
            return {"status": "skipped", "reason": "review not found or not approved"}

        dataset_id = review.curated_dataset_id
        mappings = self._db.query(OntologyMapping).filter(
            OntologyMapping.curated_dataset_id == dataset_id,
        ).filter(
            OntologyMapping.status != "disabled",
        ).all()
        link_mappings = self._db.query(OntologyLinkMapping).filter(
            or_(
                OntologyLinkMapping.src_dataset_id == dataset_id,
                OntologyLinkMapping.tgt_dataset_id == dataset_id,
                OntologyLinkMapping.edge_dataset_id == dataset_id,
            ),
            OntologyLinkMapping.status.in_(("active", "inferred")),
        ).all()

        eligible_by_ontology: dict[str, dict[str, list]] = {}
        for mapping in mappings:
            field_map = mapping.field_mapping or {}
            if bool(field_map.get("__auto_apply_on_review__", False)):
                eligible_by_ontology.setdefault(
                    mapping.ontology_id,
                    {"object_mappings": [], "link_mappings": []},
                )["object_mappings"].append(mapping)
        for mapping in link_mappings:
            field_map = mapping.field_mapping or {}
            if bool(field_map.get("__auto_apply_on_review__", False)):
                eligible_by_ontology.setdefault(
                    mapping.ontology_id,
                    {"object_mappings": [], "link_mappings": []},
                )["link_mappings"].append(mapping)

        triggered = []
        for ontology_id in sorted(eligible_by_ontology):
            subscriptions = eligible_by_ontology[ontology_id]
            ontology_mappings = subscriptions["object_mappings"]
            ontology_link_mappings = subscriptions["link_mappings"]
            # The task reconciles the complete ontology, so one subscribed
            # definition is only a validated dispatch anchor. Prefer the
            # historical object anchor and fall back to a link anchor for an
            # edge-dataset-only review subscription.
            trigger_mapping = (
                ontology_mappings[0]
                if ontology_mappings
                else ontology_link_mappings[0]
            )
            trigger_mapping_kind = (
                "object" if ontology_mappings else "link"
            )
            task_id = (
                self._trigger_mapping_apply(
                    trigger_mapping.id, ontology_id, synchronous=True)
                if synchronous
                else self._trigger_mapping_apply(
                    trigger_mapping.id, ontology_id)
            )
            mapping_ids = [item.id for item in ontology_mappings]
            link_mapping_ids = [
                item.id for item in ontology_link_mappings
            ]
            trigger_result = {
                "ontology_id": ontology_id,
                "mapping_id": trigger_mapping.id,
                "mapping_ids": mapping_ids,
                "link_mapping_ids": link_mapping_ids,
                "trigger_mapping_kind": trigger_mapping_kind,
                "task_id": task_id,
            }
            if synchronous:
                trigger_result["dispatch_mode"] = "synchronous"
            triggered.append(trigger_result)
            logger.info(
                "自动触发本体 %s 全量映射对账，对象订阅=%s，关系订阅=%s",
                ontology_id, mapping_ids, link_mapping_ids)

        return {
            "triggered_mappings": triggered,
            "dataset_id": dataset_id,
            "review_id": review_id,
        }

    # ── 幂等性保障 ────────────────────────────────────────────────────

    def _trigger_pipeline(self, pipeline_id: str, mode: str = "incremental") -> str | None:
        """触发 Pipeline 运行，返回 run_id"""
        from app.data_channel.pipelines.models import PipelineRun

        run = PipelineRun(
            pipeline_id=pipeline_id,
            status="pending",
            # 审核事件级联触发：真实列与历史过滤口径一致（stats 缺键 ≡ manual）
            trigger_type="manual",
        )
        self._db.add(run)
        self._db.commit()
        self._db.refresh(run)

        try:
            from app.data_channel.pipeline_tasks.dispatch import (
                PIPELINE_RUN_SUBJECT, dispatch_task,
            )
            dispatch_task(PIPELINE_RUN_SUBJECT, {
                "pipeline_id": pipeline_id, "run_id": run.id})
        except Exception as dispatch_error:
            run.status = "failed"
            run.error_log = "NATS 后台任务服务不可用，Pipeline 未执行"
            try:
                self._db.commit()
            except Exception:  # noqa: BLE001
                self._db.rollback()
            logger.error(
                "Pipeline %s 任务 %s 投递失败；任务未执行（%s）",
                pipeline_id,
                run.id,
                type(dispatch_error).__name__,
            )
            raise RuntimeError(
                "NATS 后台任务服务不可用，Pipeline 未执行"
            ) from dispatch_error

        return run.id

    def _trigger_mapping_apply(
        self,
        mapping_id: str,
        ontology_id: str,
        *,
        synchronous: bool = False,
    ) -> str | None:
        """触发 Mapping Apply 任务"""
        if synchronous:
            from app.tasks.v2.mapping_apply import mapping_apply_task
            task = getattr(mapping_apply_task, "run", mapping_apply_task)
            projection = task(mapping_id, ontology_id)
            sentinel_dispatch = (
                projection.get("sentinel_dispatch", {})
                if isinstance(projection, dict)
                else {}
            )
            if sentinel_dispatch.get("errors"):
                raise RuntimeError(
                    f"本体 {ontology_id} durable Sentinel 级联失败，"
                    f"拒绝确认审核事件：{sentinel_dispatch['errors']}")
            # Mapping may have committed immediately before a process crash,
            # leaving the review event unacknowledged.  Its retry then sees no
            # relational delta and therefore no CDC run.  Perform an edge-safe
            # full evaluation before acknowledging the durable event; match
            # state and action idempotency prevent duplicate side effects.
            if not sentinel_dispatch.get("runs"):
                from app.ontologies.sentinels.engine import run_manual
                sentinel_dispatch = run_manual(self._db, ontology_id)
            if sentinel_dispatch.get("errors"):
                raise RuntimeError(
                    f"本体 {ontology_id} 哨兵评估存在 "
                    f"{sentinel_dispatch['errors']} 个错误，拒绝确认审核事件")
            return f"sync:{mapping_id}"

        try:
            from app.data_channel.pipeline_tasks.dispatch import (
                MAPPING_APPLY_SUBJECT, dispatch_task,
            )
            dispatch_task(MAPPING_APPLY_SUBJECT, {
                "mapping_id": mapping_id, "ontology_id": ontology_id})
            return mapping_id
        except Exception as dispatch_error:
            logger.error(
                "Mapping %s（本体 %s）投递失败；任务未执行（%s）",
                mapping_id,
                ontology_id,
                type(dispatch_error).__name__,
            )
            raise RuntimeError(
                "NATS 后台任务服务不可用，Mapping 未执行"
            ) from dispatch_error
