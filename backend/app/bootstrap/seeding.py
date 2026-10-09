"""Database preparation and idempotent startup seeding."""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.shared.schema_compat import (
    assert_critical_schema,
    repair_development_schema,
)


_main_logger = logging.getLogger("app.main")


def seed_database() -> None:
    from app.services.auth_service import seed_admin

    db = SessionLocal()
    try:
        # Import all models to ensure tables are created
        from app.models import user, ontology, model_config, entity, logic as logic_model, action, relation, domain  # noqa: F401
        from app.data_channel.datasets import models as v2_dataset  # noqa: F401
        from app.data_channel.pipelines import models as v2_pipeline  # noqa: F401
        from app.data_channel.connections import models as v2_connection  # noqa: F401
        from app.ontologies.logic.v2_models import OntologyLogicRule, OntologyStateMachine  # noqa: F401
        from app.ontologies.actions.v2_models import OntologyActionType, OntologyActionRun  # noqa: F401
        from app.data_channel.curated.models import CuratedDataset, CuratedReview, CuratedRowEdit  # noqa: F401
        from app.data_channel.sync_tasks.models import DataSyncTask, DataSyncHistory  # noqa: F401
        from app.data_channel.pipeline_tasks.models import PipelineTask  # noqa: F401
        from app.data_channel.datasets.sharing_models import (  # noqa: F401
            ManualDatasetChange, ManualDatasetShare,
        )
        from app.ontologies.mappings.models import OntologyMapping, OntologyLinkMapping  # noqa: F401
        from app.models.ontology_version import OntologyVersion, OntologyChangeLog  # noqa: F401
        from app.models.attribute_schema import AttributeSchema, VocabularyEntry  # noqa: F401
        from app.models.inference import ShadowRun, InferenceRun, InferenceResult, ActionFiring, AuditLog  # noqa: F401
        # 正规本体模型 (Palantir 风格) — 平台核心
        from app.models.ontology_formal import (  # noqa: F401
            ObjectType, LinkType, ActionType, OntologyFunction,
            ObjectInstance, LinkInstance, ActionExecutionLog,
        )
        # 哨兵引擎模型 (反应式运行时)
        from app.models.sentinel import Sentinel, SentinelFiring, Notification, SentinelMatchState  # noqa: F401
        # 本体智能体 (授权边界内的 agent 运行时)
        from app.ontologies.agent_runtime.models import AgentProfile, AgentConversation, AgentMessage  # noqa: F401
        from app.ontologies.decision_simulation.models import DecisionSimulationRun  # noqa: F401
        # 业务探索 (对话式业务建模：画布/文档/本体草稿/会话附件)
        from app.exploration.models import (  # noqa: F401
            ExplorationSession, ExplorationMessage, ExplorationDocument, ExplorationDraft,
            ExplorationAttachment,
        )
        from app.models.workflow_config import WorkflowConfig  # noqa: F401
        from app.settings.object_storage.models import (  # noqa: F401
            MinioOperationAudit,
        )
        # 数据管家 (对话式 n8n 数据流水线：治理记录 + 会话)
        from app.data_channel.steward.models import (  # noqa: F401
            N8nPipeline, StewardConversation, StewardMessage,
        )
        from app.data_channel.file_assets.models import PipelineFileAsset  # noqa: F401
        # 事件登记 (智能助手↔数据通道之间的事件采集入口：主体/附件/审计/第三方密钥)
        from app.events.models import (  # noqa: F401
            RegisteredEvent, EventAttachment, EventAuditLog, EventIngestKey,
        )
        # 超级助手（独立会话、目录型 Skill、MCP 客户端配置）
        from app.super_assistant.models import (  # noqa: F401
            SuperAssistantConversation, SuperAssistantMessage,
            SuperAssistantToolRun, SuperAssistantSkill, SuperAssistantMcpServer,
        )
        from app.inbox.models import (  # noqa: F401
            InboxItem, InboxDelivery, InboxEventReceipt, InboxOutboxEvent,
        )
        # 消息通知（管理员消息总线：消息主体 / 每用户处置状态 / 附件）
        from app.notifications.models import (  # noqa: F401
            NotificationMessage, NotificationMessageState, NotificationAttachment,
            NotificationIngestKey,
        )
        # 三维场景（白模场景管理：主体 / 版本冻结 / 运行日志）
        from app.scenes.models import (  # noqa: F401
            Scene, SceneVersion, SceneRuntimeLog,
            SceneConversation, SceneMessage,
        )
        # 平台概览 · 运行健康度（API 性能监控）
        from app.platform.observability.models import (  # noqa: F401
            ApiPerfMinuteRollup, ApiPerfSlowRequest,
        )
        # Every real runtime schema is owned by Alembic. ``create_all`` would
        # disguise a missed migration and omit data backfills/constraints, so
        # it is reserved for isolated unit tests only.
        if settings.environment == "test":
            Base.metadata.create_all(bind=engine)

        # Lightweight column migrations — create_all skips existing tables
        with engine.connect() as conn:
            if settings.environment == "test":
                # Test SQLite fixtures have no Alembic revision. Keep their
                # narrow additive repair path without weakening real startup.
                repaired = repair_development_schema(conn)
                if repaired:
                    _main_logger.warning(
                        "已修复开发数据库缺失字段: %s", ", ".join(repaired))
                entity_columns = {
                    col["name"] for col in inspect(conn).get_columns("entities")
                }
                if "name_abbr" not in entity_columns:
                    conn.execute(text(
                        "ALTER TABLE entities ADD COLUMN name_abbr VARCHAR(50)"))
                    conn.commit()
                if "snomed_id" not in entity_columns:
                    conn.execute(text(
                        "ALTER TABLE entities ADD COLUMN snomed_id VARCHAR(50)"))
                    conn.commit()
                if "canonical_id" not in entity_columns:
                    conn.execute(text(
                        "ALTER TABLE entities ADD COLUMN canonical_id VARCHAR(200)"))
                    conn.commit()
            # These compatibility DDL statements exist only for test fixtures;
            # every real environment runs ``alembic upgrade head`` beforehand.
            for stmt in ([
                "ALTER TABLE model_configs ADD COLUMN config_type VARCHAR(30) DEFAULT 'llm'",
                "ALTER TABLE model_configs ADD COLUMN options JSON DEFAULT '{}'",
                "ALTER TABLE model_configs ADD COLUMN enabled BOOLEAN DEFAULT TRUE",
                "ALTER TABLE model_configs ADD COLUMN is_default BOOLEAN DEFAULT FALSE",
                "ALTER TABLE ontology_projects ADD COLUMN build_mode VARCHAR(30) DEFAULT 'manual'",
                "ALTER TABLE ontology_projects ADD COLUMN icon VARCHAR(50)",
                "ALTER TABLE ontology_projects ADD COLUMN current_release_id VARCHAR",
                "ALTER TABLE v2_pipelines ADD COLUMN domain VARCHAR(100) DEFAULT '通用'",
                "ALTER TABLE v2_pipelines ADD COLUMN description TEXT DEFAULT ''",
                "ALTER TABLE v2_pipelines ADD COLUMN definition JSON",
                "ALTER TABLE v2_pipelines ADD COLUMN branch VARCHAR(50) DEFAULT 'main'",
                "ALTER TABLE v2_pipelines ADD COLUMN version INTEGER DEFAULT 1",
                "ALTER TABLE logic_rules ADD COLUMN enabled BOOLEAN DEFAULT TRUE",
                "ALTER TABLE logic_rules ADD COLUMN status VARCHAR(20) DEFAULT 'draft'",
                "ALTER TABLE actions ADD COLUMN enabled BOOLEAN DEFAULT TRUE",
                "ALTER TABLE actions ADD COLUMN status VARCHAR(20) DEFAULT 'draft'",
                "ALTER TABLE sentinels ADD COLUMN condition_rows JSON DEFAULT '[]'",
                "ALTER TABLE sentinels ADD COLUMN condition_logic VARCHAR(8) DEFAULT 'and'",
                "ALTER TABLE sentinels ADD COLUMN trigger_mode VARCHAR(16) DEFAULT 'on_enter'",
                "ALTER TABLE sentinels ADD COLUMN muted BOOLEAN DEFAULT FALSE",
                "ALTER TABLE ontology_versions ADD COLUMN snapshot_formal JSON",
                "ALTER TABLE ontology_versions ADD COLUMN parent_version_id VARCHAR",
                "ALTER TABLE ontology_versions ADD COLUMN base_release_id VARCHAR",
                "ALTER TABLE ontology_versions ADD COLUMN promoted_from_id VARCHAR",
                "ALTER TABLE ontology_versions ADD COLUMN node_kind VARCHAR(20) DEFAULT 'release'",
                "ALTER TABLE ontology_versions ADD COLUMN lifecycle_status VARCHAR(20) DEFAULT 'released'",
                "ALTER TABLE ontology_versions ADD COLUMN revision INTEGER DEFAULT 0",
                "ALTER TABLE ontology_versions ADD COLUMN snapshot_hash VARCHAR(64)",
                "ALTER TABLE ontology_versions ADD COLUMN published_at DATETIME",
                "ALTER TABLE fo_property_facts ADD COLUMN kind VARCHAR(16) DEFAULT 'property'",
                "ALTER TABLE fo_property_facts ADD COLUMN derived_from JSON",
                "ALTER TABLE fo_property_facts ADD COLUMN supersedes_id VARCHAR",
                "ALTER TABLE fo_property_facts ADD COLUMN caused_by VARCHAR",
                "ALTER TABLE fo_property_facts ADD COLUMN source VARCHAR(200) DEFAULT 'manual'",
                "ALTER TABLE fo_property_facts ADD COLUMN actor_id VARCHAR",
                # —— 图谱编辑器主链路（缺此列则 GET /full 必 500）——
                "ALTER TABLE fo_object_types ADD COLUMN interfaces JSON DEFAULT '[]'",
                # —— HITL 审批闸门 + 执行溯源 ——
                "ALTER TABLE fo_action_types ADD COLUMN requires_approval BOOLEAN DEFAULT FALSE",
                "ALTER TABLE fo_action_logs ADD COLUMN actor_id VARCHAR",
                "ALTER TABLE fo_action_logs ADD COLUMN decided_by VARCHAR",
                "ALTER TABLE fo_action_logs ADD COLUMN decided_at DATETIME",
                "ALTER TABLE fo_action_logs ADD COLUMN decision_reason TEXT",
                "ALTER TABLE fo_action_logs ADD COLUMN related_log_id VARCHAR",
                # —— 事实层 CBox 补全 + 确定性排序 ——
                "ALTER TABLE fo_property_facts ADD COLUMN confidence FLOAT",
                "ALTER TABLE fo_property_facts ADD COLUMN valid_at DATETIME",
                "ALTER TABLE fo_property_facts ADD COLUMN seq INTEGER DEFAULT 0",
                # —— 同步任务状态归一（引擎曾写大写，与枚举/守卫的小写互不相认）——
                "UPDATE v2_data_sync_tasks SET status = lower(status) WHERE status != lower(status)",
                "UPDATE v2_data_sync_histories SET status = lower(status) WHERE status != lower(status)",
                # —— 映射绑定已有对象实体（model-first：先建模、再灌数据）——
                "ALTER TABLE v2_ontology_mappings ADD COLUMN target_object_type_id VARCHAR",
                # —— 流水线调度任务血缘：run 由哪条任务触发 ——
                "ALTER TABLE v2_pipeline_runs ADD COLUMN task_id VARCHAR",
                # —— 未发布 n8n 执行预览的列样本 → 发布时固化为影子流水线期望列契约 ——
                "ALTER TABLE v2_n8n_pipelines ADD COLUMN last_test_result JSON",
                # —— 流水线启用开关：停用后任务池/链式触发不执行 ——
                "ALTER TABLE v2_pipelines ADD COLUMN enabled BOOLEAN DEFAULT TRUE",
                # —— 本体元素血缘出处（业务探索草稿落地时写入，Schema 也是事实）——
                "ALTER TABLE fo_object_types ADD COLUMN source JSON",
                "ALTER TABLE fo_link_types ADD COLUMN source JSON",
                "ALTER TABLE fo_action_types ADD COLUMN source JSON",
                "ALTER TABLE fo_functions ADD COLUMN source JSON",
                "ALTER TABLE sentinels ADD COLUMN source JSON",
                # —— 胖关系（LPG 边属性）：关系映射支持连接表 + 边属性字段映射 ——
                "ALTER TABLE v2_ontology_link_mappings ADD COLUMN link_type_id VARCHAR",
                "ALTER TABLE v2_ontology_link_mappings ADD COLUMN edge_dataset_id VARCHAR",
                "ALTER TABLE v2_ontology_link_mappings ADD COLUMN field_mapping JSON DEFAULT '{}'",
                # —— 回填历史 NULL latest_version_id（create_version 旧 bug：flush 前取 id）——
                "UPDATE v2_datasets SET latest_version_id = ("
                " SELECT v.id FROM v2_dataset_versions v WHERE v.dataset_id = v2_datasets.id"
                " ORDER BY v.version_no DESC LIMIT 1)"
                " WHERE latest_version_id IS NULL AND EXISTS ("
                " SELECT 1 FROM v2_dataset_versions v2 WHERE v2.dataset_id = v2_datasets.id)",
            ] if settings.environment == "test" else []):
                try:
                    conn.execute(text(stmt))
                    conn.commit()
                except Exception as exc:
                    conn.rollback()  # 清掉 aborted 事务，避免下一个 stmt 全炸
                    msg = str(exc).lower()
                    if "duplicate column" in msg or "already exists" in msg:
                        pass
                    else:
                        _main_logger.error(
                            "schema 迁移失败(将导致该表读写 500): %s -> %s",
                            stmt,
                            exc,
                        )

            # Surface a missed production migration or failed dev repair before
            # an MCP request reaches SQLAlchemy and becomes an opaque HTTP 500.
            assert_critical_schema(conn, Base.metadata)

        seed_admin(db)

        # n8n is a required startup integration but its runtime client reads the
        # encrypted database record. Provision that record idempotently from the
        # environment; LLM providers remain post-start model configuration.
        from app.services.local_config_sync import (
            sync_local_managed_runtime_config,
        )

        if sync_local_managed_runtime_config(db):
            db.commit()

        # Retry domain results that committed just before a previous process
        # stopped, but whose personal inbox projection had not completed yet.
        from app.inbox.service import drain_outbox

        drain_outbox(db, limit=100)

        # 对象存储删除采用 transactional outbox：上次停机/存储故障遗留的任务在
        # 启动时重试。空队列不会初始化 MinIO 客户端，不增加健康检查压力。
        from app.data_channel.file_assets.service import cleanup_expired_assets

        cleanup_expired_assets(db)
        from app.data_channel.datasets.service import drain_storage_deletion_outbox

        drain_storage_deletion_outbox(
            db,
            strict_schema=settings.environment != "test",
        )

        # 回填：存量 n8n 治理记录补建影子流水线行（创建即在列表可见的新规则）
        # 幂等——已有 pipeline_id 的记录 ensure 只做同步，不重复建行
        try:
            from app.data_channel.steward.service import ensure_shadow_pipeline
            from app.data_channel.steward.models import (
                N8nPipeline as _N8nRec,
                STATUS_ARCHIVED as _ARCH,
            )
            from app.data_channel.pipelines.models import Pipeline as _Pipeline

            orphans = db.query(_N8nRec).filter(
                _N8nRec.status != _ARCH,
                _N8nRec.pipeline_id.is_(None),
            ).all()
            for _rec in orphans:
                ensure_shadow_pipeline(db, _rec)
            owner_repairs = 0
            managed = db.query(_N8nRec).filter(
                _N8nRec.status != _ARCH,
                _N8nRec.pipeline_id.is_not(None),
                _N8nRec.created_by.is_not(None),
            ).all()
            for _rec in managed:
                shadow = db.query(_Pipeline).filter(
                    _Pipeline.id == _rec.pipeline_id).first()
                if shadow is not None and not shadow.created_by:
                    shadow.created_by = _rec.created_by
                    owner_repairs += 1
            if orphans or owner_repairs:
                db.commit()
        except Exception:  # noqa: BLE001 — 回填失败不阻塞启动
            db.rollback()
            _main_logger.warning("n8n 影子流水线回填失败", exc_info=True)

    finally:
        db.close()
