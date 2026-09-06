"""
哨兵引擎模型 (Sentinel Engine) — 反应式本体运行时

哨兵 = 跨对象触发条件 → 执行一组动作。独立于动作的一等公民，可引用多个对象类型。
参考 Palantir Foundry 的 Automate 与 ITGC 平台的规则引擎(Skill)。

  - Sentinel         哨兵：监听绑定(可跨对象) + 链接约束 + 条件表达式 → 动作列表
  - SentinelFiring   哨兵触发日志：何时、由谁(手动/变化/定时)、命中了什么、动作结果
  - Notification     通知：动作 notification 副作用的落地点(可查询的内部收件箱)
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    String, DateTime, ForeignKey, Text, JSON, Boolean, Integer, UniqueConstraint,
    CheckConstraint, Index, BigInteger,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Sentinel(Base):
    """哨兵 — 跨对象触发条件 → 执行动作列表。

    bindings 示例(跨对象)：
      [{"alias": "a", "objectTypeId": "<订单>", "filter": "a.status == 'submitted'"},
       {"alias": "b", "objectTypeId": "<商家>", "filter": null}]
    links 示例(绑定间关系约束)：
      [{"from": "a", "linkTypeId": "<归属>", "to": "b"}]
    condition 示例(跨别名最终条件)：
      "a.amount > b.credit_limit"
    命中后依次执行 action_ids；动作作用目标为 primary_alias 对应的对象实例。
    """
    __tablename__ = "sentinels"
    __table_args__ = (
        CheckConstraint(
            "origin IN ('release_builtin', 'assistant_dynamic')",
            name="ck_sentinels_origin",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)

    # —— 监听范围(可跨对象) ——
    bindings: Mapped[list] = mapped_column(JSON, default=list)          # [{alias, objectTypeId, filter}]
    links: Mapped[list] = mapped_column(JSON, default=list)            # [{from, linkTypeId, to}]
    condition: Mapped[str] = mapped_column(Text, nullable=True)         # 跨别名表达式(求值用，前端编译)
    condition_rows: Mapped[list] = mapped_column(JSON, default=list)    # 结构化条件行(回显用) [{left,op,right,rightKind}]
    condition_logic: Mapped[str] = mapped_column(String(8), default="and")  # 行间逻辑 and|or
    primary_alias: Mapped[str] = mapped_column(String(50), nullable=True)  # 动作目标别名(默认首个绑定)

    # —— 命中后执行(可多个动作) ——
    action_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 每个动作的安全参数绑定：{actionId: {parameterName: literal|bindingSpec}}。
    # bindingSpec 只支持 constant / match-property / target-id，不执行任意表达式。
    action_parameters: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")

    # —— 执行入口 ——
    on_change: Mapped[bool] = mapped_column(Boolean, default=True)      # 变化驱动
    on_schedule: Mapped[bool] = mapped_column(Boolean, default=False)   # 定期扫描
    scan_interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    last_scanned_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    # —— 触发语义(边沿触发,参考 Foundry Automate)——
    # on_enter        : 仅"进入集合"时触发(新满足条件)——默认,适合一次性动作
    # on_enter_leave  : 进入 + 离开 都触发(离开=条件消除,供收尾)
    # run_on_all      : 每轮对全部当前命中执行(电平/批量,少数场景)
    trigger_mode: Mapped[str] = mapped_column(String(16), default="on_enter")
    # 静默:仍评估并记录,但不执行动作(用于影子试跑 / 临时停触)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")    # draft | published

    # 管理边界不是展示元数据：origin 由服务端写入且创建后不可变。
    # release_builtin 随本体版本快照发布；assistant_dynamic 是发布版之上的
    # 运行时叠加层，只允许智能助手接口管理，永不进入本体版本快照。
    origin: Mapped[str] = mapped_column(
        String(32), nullable=False, default="release_builtin",
        server_default="release_builtin", index=True,
    )
    bound_release_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    definition_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1")
    # Incremented for every disabled -> enabled transition. A definition can
    # therefore be initialized again without forging a new definition revision.
    enable_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    validation_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_trial_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_trial_release_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_trial_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_trial_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # 血缘出处（Schema 也是事实）：如业务探索草稿生成的影子哨兵
    # {"kind": "business_exploration", "sessionId", "documentId", "draftId", "draftKey", "sourceRefs"}
    source: Mapped[dict] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class SentinelMatchState(Base):
    """哨兵命中状态 — 边沿触发的"上次匹配集"。

    每个哨兵当前命中的对象集合,以 match_key 逐条记录(命中的 primary 实例 id,
    跨对象时为匹配元组签名)。每次评估与当前命中集做差:
      进入 = 当前 − 上次  → 触发动作
      离开 = 上次 − 当前  → (可选)触发收尾
    与哨兵配置数据分离,作为高频读写的运行时状态独立存放。
    """
    __tablename__ = "sentinel_match_state"
    __table_args__ = (
        UniqueConstraint("sentinel_id", "match_key", name="uq_sentinel_match_state_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sentinel_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # 命中键:primary 实例 id(跨对象时为元组签名,如 "a=oid|b=mid")
    match_key: Mapped[str] = mapped_column(String(500), nullable=False)
    # 命中元组明细(回显/证据用)：保留 alias→instanceId 兼容字段，并保存
    # __snapshots__（属性/派生值）与 __event__，使对象删除后的 leave/HITL
    # 仍能确定性恢复参数，而不是读取已不存在或已变化的当前行。
    match_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    # completed 才表示 on_enter 已被消费；processing/pending/failed 都是
    # 可恢复的执行 claim，仍会在后续评估中续跑而不是静默吸收边沿。
    runtime_status: Mapped[str] = mapped_column(
        String(24), default="processing_enter", server_default="completed")
    # run_on_all 每完成一轮递增；崩溃重试沿用当前 epoch，下一轮才生成新键。
    execution_epoch: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SentinelFiring(Base):
    """哨兵触发日志。"""
    __tablename__ = "sentinel_firings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sentinel_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sentinel_name: Mapped[str] = mapped_column(String(200), nullable=True)

    # trigger_source: manual | change | schedule
    trigger_source: Mapped[str] = mapped_column(String(20), nullable=False)
    # 当前命中的对象元组: [{alias: instanceId, ...}]
    matches: Mapped[list] = mapped_column(JSON, default=list)
    match_count: Mapped[int] = mapped_column(Integer, default=0)
    # 边沿:本次新进入 / 离开 的命中(键列表)
    entered: Mapped[list] = mapped_column(JSON, default=list)
    left: Mapped[list] = mapped_column(JSON, default=list)
    # 动作执行结果: [{actionId, targetInstanceId, edge, status, logId, effects}]
    action_results: Mapped[list] = mapped_column(JSON, default=list)

    # fired | pending | no_change | no_match | muted | error | skipped
    status: Mapped[str] = mapped_column(String(20), default="fired")
    error: Mapped[str] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=True)

    # Immutable ontology release that owned this evaluation.  The id is the
    # authoritative scope; version remains useful for display/compatibility.
    ontology_version: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True)
    ontology_release_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Notification(Base):
    """通知 — 动作 notification 副作用的真实落地点(内部收件箱，可查询)。"""
    __tablename__ = "sentinel_notifications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    channel: Mapped[str] = mapped_column(String(20), default="internal")
    recipient: Mapped[str] = mapped_column(String(300), nullable=True, index=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=True)

    related_object_id: Mapped[str] = mapped_column(String, nullable=True, index=True)
    action_id: Mapped[str] = mapped_column(String, nullable=True)
    # Immutable execution provenance. Notifications are effects, not mutable
    # inbox-only rows: operators must be able to trace them back to the exact
    # release, Sentinel and ActionExecutionLog that produced them.
    ontology_release_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True)
    sentinel_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True)
    action_log_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True)

    status: Mapped[str] = mapped_column(String(20), default="delivered")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SentinelCdcOutbox(Base):
    """Durable, atomic hand-off from object/link commits to Sentinel runtime.

    The row is inserted in the same transaction as the business projection.
    Workers claim it with a compare-and-set token; stale claims are recoverable
    after process termination.  ``chain_id`` scopes a synchronous mapping
    barrier to only its own downstream cascade.
    """
    __tablename__ = "sentinel_cdc_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'held','pending','processing','retry','completed','dead',"
            "'cdc_held','cdc_pending','cdc_processing','cdc_retry','cdc_dead'"
            ")",
            name="ck_sentinel_cdc_outbox_status",
        ),
        Index(
            "ix_sentinel_cdc_outbox_ready",
            "status", "available_at", "created_at",
        ),
        Index(
            "ix_sentinel_cdc_outbox_chain",
            "chain_id", "status", "created_at",
        ),
        Index(
            "ix_sentinel_cdc_outbox_release_status",
            "ontology_id", "ontology_release_id", "status", "created_at",
        ),
        Index(
            "ix_sentinel_cdc_outbox_control_ready",
            "event_kind", "sentinel_id", "ontology_release_id",
            "status", "available_at",
        ),
        Index(
            "uq_sentinel_cdc_outbox_dedupe_key",
            "dedupe_key", unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=_uuid)
    chain_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True)
    ontology_id: Mapped[str] = mapped_column(
        String, nullable=False, index=True)
    # Immutable release that owned the changed runtime row when the event was
    # captured.  NULL is retained only for pre-migration/unattributed events;
    # such a row may never be consumed against a later non-NULL release.
    ontology_release_id: Mapped[str | None] = mapped_column(
        String, nullable=True)
    # object_change | link_change | release_activation | scheduled_scan |
    # dynamic_activation | builtin_activation.
    # Explicit control-event metadata keeps release/schedule work out of
    # business object-type identifiers and gives it a durable dedupe identity.
    event_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="object_change",
        server_default="object_change")
    sentinel_id: Mapped[str | None] = mapped_column(
        String, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True)
    object_type_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True)
    changed_keys: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list)
    link_change: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0")
    cascade_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    # Mapping-owned root events carry the exact mapping fence that must reach
    # ``applied`` before a restart recovery may release the held event.
    mapping_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending",
        server_default="pending")
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now,
        onupdate=_now)


class SentinelEventLog(Base):
    """实例级变更事件日志 — 哨兵时间能力（CEP）的唯一事实源。

    与 ``SentinelCdcOutbox`` 的分工：outbox 负责"触发"（按对象类型聚合、
    携带 claim/生命周期，可被合并或废弃），本表负责"事实"（键级行、
    只追加、携带 old→new 值、按保留期裁剪）。两表在业务事务内同点捕获，
    因此 outbox 被消费时明细必然已可见。

    行粒度为"实例 × 属性键"：``prev()`` / ``changed_within()`` / 窗口聚合
    都退化为 (instance_id, key) 上的索引查询，PostgreSQL 与 SQLite 均不
    需要 JSON 方言函数。主键是单调递增大整数——水位消费、``prev()`` 定序
    与 keyset 分批裁剪的共同前提。
    """
    __tablename__ = "sentinel_event_log"
    __table_args__ = (
        Index(
            "ix_sentinel_event_log_timeline",
            "ontology_id", "object_type_id", "instance_id", "id",
        ),
        Index(
            "ix_sentinel_event_log_key",
            "instance_id", "key", "id",
        ),
        Index(
            "ix_sentinel_event_log_occurred",
            "occurred_at", "id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True, autoincrement=True)
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="CASCADE"),
        nullable=False)
    # 事件捕获时该运行时行归属的不可变 release（与 outbox 同一解析规则）。
    ontology_release_id: Mapped[str | None] = mapped_column(
        String, nullable=True)
    object_type_id: Mapped[str] = mapped_column(String, nullable=False)
    instance_id: Mapped[str] = mapped_column(String, nullable=False)
    # created | updated | deleted
    change_kind: Mapped[str] = mapped_column(
        String(12), nullable=False, default="updated",
        server_default="updated")
    # 属性键；删除事件统一落在哨兵保留键 ``__deleted__`` 上。
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    old_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # organic=真实业务变更；release_activation=发布切换事务内的投影重建
    # （不参与时间算子/模式推进，仅供审计与排障）。
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="organic",
        server_default="organic")
    cascade_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    chain_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 事件时间为捕获时刻（UTC）。排序权威是单调主键 id。
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)
