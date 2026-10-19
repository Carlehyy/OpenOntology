"""
正规本体模型 (Formal Ontology Model) — Palantir Foundry 风格

这是平台的核心建模范式，反向参考自前端「图谱编辑页」(palantir-graph)。
取代旧的扁平模型 (Entity / Relation / LogicRule / Action)。

七类一等公民：
  - ObjectType        对象类型：强类型属性 + 主键 + 实现接口
  - LinkType          链接类型：对象之间的关系（含基数 / 角色 / 链接属性）
  - ActionType        动作类型：可执行的业务操作（参数 + 规则 + 校验函数）
  - OntologyFunction  函数：一等公民（派生属性 / 校验 / 查询）
  - ObjectInstance    对象实例：运行时数据（数据采集落地点）
  - LinkInstance      链接实例：运行时关系
  - ActionExecutionLog 动作执行日志

字段命名与 frontend/src/palantir-graph/types/ontology.ts 保持 1:1，
确保前后端类型零摩擦复用。
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    String, DateTime, Float, ForeignKey, Text, JSON, Boolean, Integer,
    Index, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)




class ObjectType(Base):
    """对象类型 — 业务实体定义：强类型属性 + 主键 + 接口"""
    __tablename__ = "fo_object_types"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    icon: Mapped[str] = mapped_column(String(50), nullable=True)
    color: Mapped[str] = mapped_column(String(30), nullable=True)

    primary_key: Mapped[str] = mapped_column(String(100), nullable=True)       # 指向某个 property id

    # properties: Property[]
    properties: Mapped[list] = mapped_column(JSON, default=list)

    # 实现接口 (历史遗留，已废弃但数据库列仍存在)
    interfaces: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")

    # 画布坐标 (可视化用)
    position_x: Mapped[float] = mapped_column(default=0.0)
    position_y: Mapped[float] = mapped_column(default=0.0)

    # 血缘出处（Schema 也是事实）：非手工创建的元素记录来源指针，
    # 如 {"kind": "business_exploration", "sessionId", "documentId", "draftId", "draftKey", "sourceRefs"}
    # 编辑器保存走 FIELDS_* 白名单，不会清洗此列
    source: Mapped[dict] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class LinkType(Base):
    """链接类型 — 对象类型之间的关系（基数 / 角色 / 链接属性）"""
    __tablename__ = "fo_link_types"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)

    source_object_type_id: Mapped[str] = mapped_column(String, nullable=False)
    target_object_type_id: Mapped[str] = mapped_column(String, nullable=False)
    # one-to-one | one-to-many | many-to-one | many-to-many
    cardinality: Mapped[str] = mapped_column(String(20), default="one-to-many")
    source_role: Mapped[str] = mapped_column(String(100), nullable=True)
    target_role: Mapped[str] = mapped_column(String(100), nullable=True)

    properties: Mapped[list] = mapped_column(JSON, default=list)

    # 血缘出处（同 ObjectType.source）
    source: Mapped[dict] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ActionType(Base):
    """动作类型 — 可执行的业务操作（参数 + 规则 + 校验函数）"""
    __tablename__ = "fo_action_types"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)

    object_type_id: Mapped[str] = mapped_column(String, nullable=True)         # 绑定的对象类型
    # parameters: ActionParameter[]
    parameters: Mapped[list] = mapped_column(JSON, default=list)
    # rules: ActionRule[]
    rules: Mapped[list] = mapped_column(JSON, default=list)
    validation_function_id: Mapped[str] = mapped_column(String, nullable=True)
    # HITL 审批闸门：true 时真实执行先落 pending 日志，等人批准/拒绝（决策本身记为 Fact）
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    # 血缘出处（同 ObjectType.source）
    source: Mapped[dict] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class OntologyFunction(Base):
    """函数 — 一等公民：派生属性 / Action 校验 / 自定义查询"""
    __tablename__ = "fo_functions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)

    # object | object_set | action_validation | query
    function_type: Mapped[str] = mapped_column(String(30), default="object")
    # typescript | expression
    language: Mapped[str] = mapped_column(String(20), default="expression")

    target_object_type_id: Mapped[str] = mapped_column(String, nullable=True)
    target_action_id: Mapped[str] = mapped_column(String, nullable=True)

    parameters: Mapped[list] = mapped_column(JSON, default=list)
    return_type: Mapped[str] = mapped_column(String(30), default="string")
    body: Mapped[str] = mapped_column(Text, default="")

    cache_strategy: Mapped[str] = mapped_column(String(20), nullable=True)     # none | ttl | materialized
    cache_ttl: Mapped[int] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # 血缘出处（同 ObjectType.source）
    source: Mapped[dict] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ObjectInstance(Base):
    """对象实例 — 运行时数据（数据采集的落地点）"""
    __tablename__ = "fo_object_instances"
    __table_args__ = (
        Index("ix_fo_object_instances_graph_page", "ontology_id", "object_type_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    # Immutable owner of this materialized runtime row.  NULL is intentionally
    # reserved for legacy/unattributed data and must never be inferred as the
    # project's current release.
    ontology_release_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    object_type_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # properties: Record<string, unknown>
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    # 派生属性计算结果
    computed: Mapped[dict] = mapped_column(JSON, default=dict)

    # 数据采集溯源
    source: Mapped[str] = mapped_column(String(50), nullable=True)             # manual | collector | import
    external_id: Mapped[str] = mapped_column(String(200), nullable=True, index=True)  # 外部源唯一 id (去重)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class LinkInstance(Base):
    """链接实例 — 运行时关系"""
    __tablename__ = "fo_link_instances"
    __table_args__ = (
        Index("ix_fo_link_instances_graph_source", "ontology_id", "link_type_id", "source_object_id"),
        Index("ix_fo_link_instances_graph_target", "ontology_id", "link_type_id", "target_object_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    # See ObjectInstance.ontology_release_id.  This is deliberately nullable
    # for safe migration of historical rows whose release cannot be proven.
    ontology_release_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    link_type_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    source_object_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    target_object_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    # Stable pointer to the relational projection record.  Kept outside business
    # properties so lineage metadata cannot violate LinkType property schemas.
    source_relation_id: Mapped[str] = mapped_column(String, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class PropertyFact(Base):
    """属性事实 — append-only 的属性级变更历史（Fact 溯源层第一步）

    设计原则（对齐"事实流 + 投影"架构）：
      - fo_object_instances.properties 是当前态投影；本表是不可变的历史真理流。
      - 只追加，不更新不删除；新值到达时写新行，supersedes_id 指向被替代的旧事实。
      - 每条事实都带出处：source（editor-save / action://<id> / collector / import）、
        actor_id（有人参与时）、caused_by（因果指针，如 Action 执行日志 id）。
    任意时刻 T 的属性值 = recorded_at ≤ T 且未被 T 之前的事实 supersede 的最新事实。
    """
    __tablename__ = "fo_property_facts"
    __table_args__ = (
        Index(
            "ix_fo_facts_release_coord_order",
            "ontology_id",
            "ontology_release_id",
            "kind",
            "instance_id",
            "property_name",
            "recorded_at",
            "seq",
            "id",
        ),
        Index(
            "ix_fo_facts_instance_coord_order",
            "ontology_id",
            "instance_id",
            "kind",
            "property_name",
            "recorded_at",
            "seq",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    object_type_id: Mapped[str] = mapped_column(String, nullable=True, index=True)
    property_name: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[dict] = mapped_column(JSON, nullable=True)          # {"v": <any>} 包一层，兼容标量

    # property = 存储属性 | derived = 派生重算 | link = 链接存在性 |
    # object = 实例存在性(墓碑) | decision = 人的审批决策 |
    # absence = 缺席事实(常驻查询结果为空/非空的翻转快照)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="property")

    # —— CBox：出处与因果 ——
    source: Mapped[str] = mapped_column(String(200), nullable=False, default="manual")
    actor_id: Mapped[str] = mapped_column(String, nullable=True)
    caused_by: Mapped[str] = mapped_column(String, nullable=True)      # 因果指针（决策事实/动作日志 id）
    supersedes_id: Mapped[str] = mapped_column(String, nullable=True)  # 被替代的旧事实 id
    derived_from: Mapped[list] = mapped_column(JSON, nullable=True)    # 派生事实的输入事实 id 列表
    confidence: Mapped[float] = mapped_column(Float, nullable=True)    # 来源置信度（采集/推理可低于 1）
    valid_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # 业务生效时间（缺省=记录时间）

    # Bind every runtime fact to the immutable release that produced it.  The
    # release id is authoritative; version is retained for display/compatibility.
    # NULL lineage means a legacy record cannot be safely attributed and must
    # stay out of release-scoped governance views.
    ontology_version: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True)
    ontology_release_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True)

    # End-to-end lineage: which immutable lake snapshot (DatasetVersion) the
    # fact was projected from.  NULL for facts not derived from a lake source
    # (editor saves, manual entries, action effects, decisions, absence).
    # Together with DatasetVersion.producer_run_id this closes the loop
    # lake row → lake version → mapping apply → fact → run/task.
    source_dataset_version_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True)

    # 同一 (instance, property) 链内单调递增，供同毫秒事实的确定性排序
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class ActionExecutionLog(Base):
    """动作执行日志"""
    __tablename__ = "fo_action_logs"
    __table_args__ = (
        # NULL 表示普通调用或可重试的失败尝试；非 NULL 只允许一个
        # pending/success owner，防止哨兵重放已完成副作用。
        UniqueConstraint(
            "ontology_id", "idempotency_key",
            name="uq_action_log_ontology_idempotency_key",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False, index=True)

    action_id: Mapped[str] = mapped_column(String, nullable=False)
    action_name: Mapped[str] = mapped_column(String(200), nullable=True)
    object_type_id: Mapped[str] = mapped_column(String, nullable=True)
    object_instance_id: Mapped[str] = mapped_column(String, nullable=True)

    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    # Immutable target evidence used only when an on_leave target has already
    # been deleted.  Runtime restricts it to validation/notification/webhook;
    # local data mutation rules must still resolve a live instance.
    target_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # pending(待审批) | validating | validated | executing | success | failed |
    # rejected(审批拒绝) | rolled_back
    status: Mapped[str] = mapped_column(String(20), default="pending")
    validation_errors: Mapped[list] = mapped_column(JSON, default=list)
    effects: Mapped[list] = mapped_column(JSON, default=list)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)

    # 谁发起的执行（run-action 的当前用户；哨兵触发为空）
    actor_id: Mapped[str] = mapped_column(String, nullable=True)
    # —— HITL 审批（status=pending 的日志经人决策后填写）——
    decided_by: Mapped[str] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    decision_reason: Mapped[str] = mapped_column(Text, nullable=True)
    # 批准后真正执行产生的新日志 id（pending 日志 ↔ 执行日志互链）
    related_log_id: Mapped[str] = mapped_column(String, nullable=True)

    # 哨兵运行时幂等关联。失败/拒绝会清空 idempotency_key 以允许新尝试；
    # pending、success 与 approved-success 保留键并在重试时复用。
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=True)
    sentinel_match_state_id: Mapped[str] = mapped_column(
        String, nullable=True, index=True)
    # Bind an approval/idempotency record to the immutable ontology release that
    # produced it.  A pending v1 action must never execute after v2 is published.
    ontology_version: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True)
    ontology_release_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True)
    # YIYA-36 确认协议血缘：该引擎日志所属的持久 operation（可空 — 既有
    # 哨兵/直连调用无 operation）。恢复按 operation_id 与旧幂等键双向定位。
    operation_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True)

    executed_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
