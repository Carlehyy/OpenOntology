import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, JSON, Float, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class OntologyMapping(Base):
    __tablename__ = "v2_ontology_mappings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False)
    # 数据资产湖的唯一权威表是 v2_datasets（同时承载 curated/manual）。
    curated_dataset_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_datasets.id"), nullable=True)
    entity_class: Mapped[str] = mapped_column(String(200), nullable=False)
    field_mapping: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # 人工绑定：数据灌入到图谱编辑器里已有的对象实体（model-first 流程的核心）。
    # 为空时按 entity_class 名字匹配已有类型，仍无则由投影自建类型（data-first 流程）。
    target_object_type_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class OntologyLinkMapping(Base):
    __tablename__ = "v2_ontology_link_mappings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False)
    src_dataset_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_datasets.id"), nullable=True)
    tgt_dataset_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_datasets.id"), nullable=True)
    relation_type: Mapped[str] = mapped_column(String(100), nullable=False)
    src_key: Mapped[str] = mapped_column(String(100), nullable=False)
    tgt_key: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    # —— 胖关系（LPG 边属性）——
    # 绑定到手绘 LinkType，让边属性名对齐其 properties schema；为空时按 relation_type 名匹配/自建。
    link_type_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # 连接表 / 关系数据集：为空 → 直连外键「瘦关系」(src_key∈src_dataset, tgt_key∈tgt_dataset)；
    # 有值 → 连接表「胖关系」(src_key/tgt_key 为连接表内指向两端主键的外键列，属性列由 field_mapping 采集)。
    edge_dataset_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_datasets.id"), nullable=True)
    # {边属性名: 连接表列名} —— 采集进 LinkInstance.properties（镜像 OntologyMapping.field_mapping）。
    field_mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class MappingKnowledgeEntry(Base):
    """人工确认过的「列→本体属性」映射知识（数据飞轮沉淀层）。

    锚定点用语义名（object_name/property_name）而非本体内部 id，保证跨本体可复用；
    只由人工保存过的映射回流写入，LLM 未确认产出永不入库，防止飞轮被污染。
    """
    __tablename__ = "v2_mapping_knowledge_entries"
    __table_args__ = (
        UniqueConstraint(
            "column_key", "display_name", "col_type",
            "object_name", "property_name",
            name="uq_mapping_knowledge_anchor",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # 归一化列名（小写、驼峰转下划线、去非字母数字）
    column_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    # 中文显示名（可空，匹配时作为第二锚点）
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="", index=True)
    # 归一化类型（string/number/datetime/boolean/array/json）
    col_type: Mapped[str] = mapped_column(String(20), nullable=False, default="string")
    object_name: Mapped[str] = mapped_column(String(200), nullable=False)
    property_name: Mapped[str] = mapped_column(String(200), nullable=False)
    confirm_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class OntologyMappingSuggestion(Base):
    """映射建议的人工确认队列（持久层）。

    探索 Agent（propose_mapping 工具）与 L0-L2 建议流水线共用同一确认纪律：
    建议落库即 pending，确认只发生在映射视图的队列 UI；未确认建议永不进入
    草稿快照 mappings、永不回流知识库（防飞轮污染）。
    """
    __tablename__ = "v2_mapping_suggestions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    # 建议锚定的草稿版本；版本语义（draft/editing）由建议服务在写入时守卫。
    version_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    dataset_id: Mapped[str] = mapped_column(String, ForeignKey("v2_datasets.id"), nullable=False)
    # 目标对象实体：id + 语义名冗余（对齐 OntologyMapping 的 entity_class 口径，
    # 对象改名后仍可追溯建议当时的锚点）。
    object_type_id: Mapped[str] = mapped_column(String, nullable=False)
    entity_class: Mapped[str] = mapped_column(String(200), nullable=False)
    # {数据集列名: 本体属性名} —— 纯净业务映射，主键列单列不混入保留键。
    field_mapping: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    primary_key_column: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # pending（待人工确认）| confirmed | dismissed；Agent 只能写入 pending。
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    # 确认后回填草稿快照中的 mapping 条目 id（幂等回报与审计锚点）。
    confirmed_mapping_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # 驳回原因（dismiss 时由人工填写，可空）。
    status_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 建议来源（agent / pipeline ...），供确认队列区分呈现与审计。
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="agent")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
