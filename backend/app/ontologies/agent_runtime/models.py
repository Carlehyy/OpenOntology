"""
本体智能体 (Ontology Agent) — 数据模型

参考 Palantir AIP 的 agent×ontology 交互机制：agent 不直接访问底层数据库、
不自由扫描 schema，只能在 AgentProfile 声明的授权边界内，通过本体暴露的
对象 / 链接 / 事实 / 动作完成业务交互。

五张表：
  - AgentProfile       每个本体一份的授权边界配置（能看什么、能提什么动作、配额）
  - AgentConversation  对话（按本体 × 用户组织）
  - AgentMessage       消息（含工具调用轨迹 / 引用 / 动作提案，全程可审计）
  - AnalysisReportTemplate  AI 辅助生成、人工编辑、试运行后发布的报告模板
  - AnalysisReportRun       模板在真实数据上的预览/正式运行快照与 HTML 产物
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Text, JSON, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AgentProfile(Base):
    """智能体授权边界 — 每个本体一份。

    白名单语义：None = 全部允许；[] = 全部拒绝；[id...] = 仅列表内允许。
    动作默认 []（默认拒绝）：读默认开放，写必须显式授权 —— 治理优先。
    """
    __tablename__ = "fo_agent_profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    allowed_object_type_ids: Mapped[list] = mapped_column(JSON, nullable=True)   # None=全部
    allowed_link_type_ids: Mapped[list] = mapped_column(JSON, nullable=True)     # None=全部
    allowed_action_ids: Mapped[list] = mapped_column(JSON, nullable=True, default=list)  # 默认拒绝
    # 属性级授权（JSON 可空）：None=现有已声明属性全允许；
    # {object_type_id: None}=该类型全部声明属性、[]=无业务属性、[prop...]=仅列出属性；
    # map 非空时未列出的类型默认无业务属性。系统 id/release/ontology/type 仅作为
    # 必要身份元数据，不视为业务字段授权。
    allowed_properties: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 是否允许 agent 提出动作提案（dry-run 预演）。真实执行永远走用户确认 + HITL 闸门。
    allow_action_proposals: Mapped[bool] = mapped_column(Boolean, default=True)

    max_rows_per_query: Mapped[int] = mapped_column(Integer, default=50)
    max_steps: Mapped[int] = mapped_column(Integer, default=8)

    system_prompt_extra: Mapped[str] = mapped_column(Text, default="")
    default_model_id: Mapped[str] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AgentConversation(Base):
    __tablename__ = "fo_agent_conversations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ontology_release_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    user_id: Mapped[str] = mapped_column(String, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    # 评估沙箱回放产生的会话（试跑草稿配置）：对用户侧列表不可见，评分后即清理
    is_sandbox: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AgentMessage(Base):
    """一条消息。assistant 消息附带完整工具轨迹（steps）——审计的最小单元。"""
    __tablename__ = "fo_agent_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    role: Mapped[str] = mapped_column(String(20), nullable=False)   # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")

    # [{tool, arguments, summary, durationMs, result, error?}]
    steps: Mapped[list] = mapped_column(JSON, default=list)
    # [{instanceId, objectType, label}]
    citations: Mapped[list] = mapped_column(JSON, default=list)
    # [{proposalId, actionId, actionName, parameters, targetInstanceId, validationErrors, effects}]
    proposals: Mapped[list] = mapped_column(JSON, default=list)

    model: Mapped[str] = mapped_column(String(200), nullable=True)
    token_usage: Mapped[dict] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AnalysisReportTemplate(Base):
    """分析报告模板。

    ``revision`` 是发布质量门的并发令牌：模板任何可见内容被修改都会递增，
    已完成的真实数据试运行只对当时 revision 有效。发布后的模板不可原地修改，
    避免自动运行悄悄漂移。
    """
    __tablename__ = "fo_analysis_report_templates"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String, nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    source_prompt: Mapped[str] = mapped_column(Text, default="")
    generation_mode: Mapped[str] = mapped_column(String(20), default="ai")  # ai | fallback | manual
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published
    revision: Mapped[int] = mapped_column(Integer, default=1)

    # [{id,title,goal,visualization,queryPlan:[{tool,arguments}]}]
    sections: Mapped[list] = mapped_column(JSON, default=list)
    style: Mapped[dict] = mapped_column(JSON, default=dict)
    default_model_id: Mapped[str] = mapped_column(String, nullable=True)

    # 最近一次真实数据试运行；仅当 revision 相等且质量门通过时允许发布。
    last_preview_run_id: Mapped[str] = mapped_column(String, nullable=True)
    last_preview_revision: Mapped[int] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AnalysisReportRun(Base):
    """一次分析报告运行。

    保存模板快照、查询结果、质量报告与完整 HTML，使输出可复核、可下载，
    也让后续 n8n 自动触发复用同一套确定性查询计划。
    """
    __tablename__ = "fo_analysis_report_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    template_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ontology_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String, nullable=False, index=True)

    trigger_type: Mapped[str] = mapped_column(String(20), default="preview")  # preview | manual | scheduled
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | succeeded | failed
    template_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    template_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    section_results: Mapped[list] = mapped_column(JSON, default=list)
    quality_report: Mapped[dict] = mapped_column(JSON, default=dict)
    html_content: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
