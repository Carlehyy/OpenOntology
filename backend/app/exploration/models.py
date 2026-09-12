"""业务探索 (Business Exploration) — 数据模型

对话式业务建模/需求建模：AI 引导澄清，把已确认的业务知识实时沉淀为
六类结构化模型（对象/主体/行为/事件/规则/场景）组成的「业务画布」，
再从画布生成需求文档，最终转化为本体草稿（人审后落地）。

五张表：
  - ExplorationSession    探索会话（画布挂在会话上，随对话演进）
  - ExplorationMessage    消息（含工具调用轨迹，审计单元）
  - ExplorationDocument   需求文档（画布快照 + markdown，版本递增）
  - ExplorationDraft      本体草稿（转化产物 + 校验报告，应用前的人审对象）
  - ExplorationAttachment 会话附件（上传的参考资料，仅本会话可见，注入对话上下文）
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, String, DateTime, Text, JSON, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExplorationSession(Base):
    __tablename__ = "bx_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200), default="新的业务探索")

    # 业务画布：{"objects": [...], "actors": [...], "behaviors": [...],
    #           "events": [...], "rules": [...], "scenarios": [...]}
    canvas: Mapped[dict] = mapped_column(JSON, default=dict)
    canvas_version: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(20), default="active")

    # 会话绑定的本体与版本锚点（语义层挂载点，应用草稿时回填）
    ontology_id: Mapped[str] = mapped_column(String, nullable=True, index=True)
    ontology_version_id: Mapped[str] = mapped_column(String, nullable=True, index=True)

    # 长会话的确定性压缩状态。summary 覆盖最早的 N 条消息；编排器只把摘要、
    # 画布权威快照与最近消息送入模型，避免简单截断造成“聊几句就忘了”。
    context_summary: Mapped[str] = mapped_column(Text, default="")
    summary_message_count: Mapped[int] = mapped_column(Integer, default=0)
    context_stats: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ExplorationMessage(Base):
    __tablename__ = "bx_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    role: Mapped[str] = mapped_column(String(20), nullable=False)   # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    # [{tool, arguments, summary, durationMs, error?}]
    steps: Mapped[list] = mapped_column(JSON, default=list)

    model: Mapped[str] = mapped_column(String(200), nullable=True)
    token_usage: Mapped[dict] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ExplorationDocument(Base):
    __tablename__ = "bx_documents"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(300), default="业务需求文档")
    content_md: Mapped[str] = mapped_column(Text, default="")
    # 生成时刻的画布快照 —— 草稿转化以此为源，保证文档与草稿同源可追溯
    canvas_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ExplorationAttachment(Base):
    """会话级参考资料。文件上传后确定性转为文本，随每个对话回合注入 LLM 上下文，
    帮助引导师从真实材料中提炼业务模型。严格绑定 session_id —— 跨会话不可见，
    随会话删除一并清理（磁盘文件 + 记录）。"""
    __tablename__ = "bx_attachments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    filename: Mapped[str] = mapped_column(String(400), default="")
    # 会话空间中的逻辑路径；物理路径始终由服务端生成，客户端/Agent 不可指定。
    relative_path: Mapped[str] = mapped_column(String(500), default="")
    mime_type: Mapped[str] = mapped_column(String(200), nullable=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    file_path: Mapped[str] = mapped_column(String, nullable=True)  # 磁盘路径，删除时清理
    sha256: Mapped[str] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(20), default="upload")  # upload | user | agent
    editable: Mapped[bool] = mapped_column(Boolean, default=False)

    # 转换后的文本（注入对话上下文的内容源）+ 原始字符数（截断前）
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    # LLM 懒生成的「索引卡」摘要（≤600 字符）：注入上下文时代替原文全文；
    # NULL=尚未生成。正文被更新（update_text/refresh_binary_metadata）时置空重生成。
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="ready")  # ready | failed
    error: Mapped[str] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ExplorationDraft(Base):
    __tablename__ = "bx_drafts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # None = 应用时新建本体；否则保守合并进该本体（只新增、同名跳过）
    target_ontology_id: Mapped[str] = mapped_column(String, nullable=True)

    # {"objectTypes": [...], "linkTypes": [...], "actions": [...]}，元素带 key/sourceRefs/conflict
    draft: Mapped[dict] = mapped_column(JSON, default=dict)
    # {"warnings": [...], "conflicts": [...], "scenarioCoverage": [...], "llmRefined": bool}
    report: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | applied | discarded
    applied_ontology_id: Mapped[str] = mapped_column(String, nullable=True)
    # 草稿应用后落地的版本（与 applied_ontology_id 配对回填）
    applied_version_id: Mapped[str] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
