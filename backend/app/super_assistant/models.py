from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SuperAssistantConversation(Base):
    __tablename__ = "super_assistant_conversations"
    __table_args__ = (
        Index("ix_sa_conversations_owner_updated", "owner_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="新会话")
    model_config_id: Mapped[str | None] = mapped_column(String, ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # 上下文压缩：summary 覆盖最旧的 summary_message_count 条 complete 消息
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class SuperAssistantMessage(Base):
    __tablename__ = "super_assistant_messages"
    __table_args__ = (
        Index("ix_sa_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("super_assistant_conversations.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="complete")
    steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    token_usage: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class SuperAssistantToolRun(Base):
    __tablename__ = "super_assistant_tool_runs"
    __table_args__ = (
        Index("ix_sa_tool_runs_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("super_assistant_conversations.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    assistant_message_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("super_assistant_messages.id", ondelete="SET NULL"), nullable=True,
    )
    call_id: Mapped[str] = mapped_column(String(200), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(300), nullable=False)
    server_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("super_assistant_mcp_servers.id", ondelete="SET NULL"), nullable=True,
    )
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SuperAssistantSkill(Base):
    __tablename__ = "super_assistant_skills"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_sa_skill_owner_name"),
        Index("ix_sa_skills_owner_updated", "owner_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    triggers: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    folder_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    manifest: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 常驻技能：SKILL.md 全文直接内联系统提示，跳过 use_skill 渐进披露
    always_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 行内使用统计（同记忆 match/reference 计数思路）：use_skill 成功 +1，
    # 作为 Skill 目录降权排序的信号源，零使用的老技能沉底
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class SuperAssistantMemory(Base):
    """跨会话记忆：对标 hermes 的 memory 模型（zone + pinned + supersedes）。

    效果统计不用独立事件表，直接在行内计数：
    match_count（被检索命中次数）与 reference_count（被实际引用/访问次数）
    共同映射出 [0.5, 1.0] 的效果因子；配合 30 天半衰期时间衰减降权。
    """

    __tablename__ = "super_assistant_memories"
    __table_args__ = (
        Index("ix_sa_memories_owner_zone", "owner_id", "zone"),
        Index("ix_sa_memories_owner_updated", "owner_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 内置约定 zone：core=身份偏好 / work=当前焦点 / episode=会话摘要 /
    # general 默认；project:<name> 预留给项目级记忆
    zone: Mapped[str] = mapped_column(String(50), nullable=False, default="general")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False, default="medium")
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="reflection")
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 本条记忆取代的旧记忆 id 列表；被取代行置 superseded=True（留档审计）
    supersedes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reference_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class SuperAssistantReflectionRun(Base):
    """一次反思执行记录：micro（每轮后）/ full（手动）/ focused（propose_skill）。

    NATS 消费侧的幂等锚点：同一 (kind, message_id) 已有成功记录时跳过。
    """

    __tablename__ = "super_assistant_reflection_runs"
    __table_args__ = (
        Index("ix_sa_reflect_runs_conversation_created", "conversation_id", "created_at"),
        Index("ix_sa_reflect_runs_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("super_assistant_conversations.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    message_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("super_assistant_messages.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SuperAssistantReflectionCandidate(Base):
    """反思产出的待审批候选：memory / skill / conflict。

    payload 结构按 kind 约定：
    - memory: {content, zone, tags, pinned, confidence, supersedes: [memory_id]}
    - skill: {name, display_name, description, triggers, skill_md, files: [{path, content}]}
    - conflict: {memory_id, conflict_kind, explain, options: [...], candidate_id?}
    """

    __tablename__ = "super_assistant_reflection_candidates"
    __table_args__ = (
        Index("ix_sa_reflect_candidates_owner_status", "owner_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("super_assistant_reflection_runs.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("super_assistant_conversations.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    confidence: Mapped[str] = mapped_column(String(10), nullable=False, default="medium")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # 审批动作：accept/reject；冲突候选细化：new_supersedes/keep_old/skip
    decision: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SuperAssistantMemoryProfile(Base):
    """每用户记忆设置与编译产物：palace 索引、LLM 画像、auto-accept 开关。

    注入优先级（对标 hermes）：palace_index > profile > 经典模式
    （pinned 全文 + 索引 + 每轮相关记忆）。
    """

    __tablename__ = "super_assistant_memory_profiles"

    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    palace_index: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_accept_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    compiled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class SuperAssistantMcpServer(Base):
    __tablename__ = "super_assistant_mcp_servers"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_sa_mcp_owner_name"),
        Index("ix_sa_mcp_owner_updated", "owner_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    builtin_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    transport: Mapped[str] = mapped_column(String(30), nullable=False, default="streamable_http")
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    headers_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    header_names: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    command: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    args: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    env_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    env_names: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    require_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tool_manifest: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    last_test_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class SuperAssistantMulticaConfig(Base):
    """每用户一条的 multica 外部集成配置。

    凭据与 SuperAssistantMcpServer 同约定：PAT 加密存储、永不回显
    （token_set 由 token_encrypted 是否存在推导）。未配置（无行）或
    enabled=False 时，multica 工具不进入超级助手工具目录，/multica:
    命令得到确定性引导而非执行。
    """

    __tablename__ = "super_assistant_multica_configs"

    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    workspace_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # 工作区显示名：保存/测试连接时从 multica 回填，配置弹窗下拉兜底显示
    # 名称而非裸 UUID；工作区改名后下次测试连接会自动刷新
    workspace_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_test_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class SuperAssistantPalaceFile(Base):
    """记忆宫殿的用户级文件库（跨会话长期资产，区别于会话附件）。

    文件本体与解析文本存 palace 工作区（SessionWorkspace、独立根目录，
    artifact_id 即工作区清单行 id）；本表持有权威状态：图谱抽取状态机
    pending → building → built/failed 与行内计数。图谱内容在 Neo4j，
    以 (file_id, sha256) 幂等重建。
    """

    __tablename__ = "super_assistant_palace_files"
    __table_args__ = (
        Index("ix_sa_palace_files_owner_updated", "owner_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # 目录层级（"/" 分隔，根目录为空串）：单文件上传落根目录，ZIP 导入以
    # 压缩包名为顶层目录、保留包内相对层级；仅作展示分组，不参与图谱语义
    folder_path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False, default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    extracted_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # pending=待抽取 building=抽取中 built=已建图 failed=失败（可重建）
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class SuperAssistantPalaceBuild(Base):
    """一次记忆宫殿图谱抽取执行记录。

    NATS 消费侧的幂等锚点：同一 (file_id, content_hash) 已有成功记录时
    跳过；running 超过 30 分钟视为进程中断，允许重新领取。
    """

    __tablename__ = "super_assistant_palace_builds"
    __table_args__ = (
        Index("ix_sa_palace_builds_file_created", "file_id", "created_at"),
        Index("ix_sa_palace_builds_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    file_id: Mapped[str] = mapped_column(
        String, ForeignKey("super_assistant_palace_files.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SuperAssistantPalaceFolder(Base):
    """记忆宫殿用户级目录（一等公民：支持空目录常驻与整目录移动/重命名）。

    path 与文件行的 folder_path 同口径："/" 分隔的归一路径，根目录为空串
    且不落行。目录移动/重命名 = 本行 path 连同子孙目录行与文件 folder_path
    的前缀批量重写（见 palace_service.rename_folder）。
    """

    __tablename__ = "super_assistant_palace_folders"
    __table_args__ = (
        UniqueConstraint("owner_id", "path", name="uq_sa_palace_folders_owner_path"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class SuperAssistantToolSetting(Base):
    """每用户一行的内置工具启停设置：只存"禁用名单"。

    disabled_tools 列出被用户禁用的内置工具名（条件性工具如 web_search
    平台未启用时也可预先禁用，配置生效后即被过滤）。未配置（无行）或
    名单为空表示全部启用，与功能上线前行为一致。stream_chat 组装工具
    目录时按名单过滤；执行级对模型幻觉调用已禁用工具再防御性拒绝。
    MCP 工具不在此名单（继续由 server.enabled 管理，避免双控制点）。
    """

    __tablename__ = "super_assistant_tool_settings"

    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    disabled_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class SuperAssistantWidgetConfig(Base):
    """悬浮 AI 助手（迷你超级助手）的页面可见范围配置（平台级单例）。

    只存"隐藏名单"：hidden_menu_keys 列出左导航叶子菜单键，命中这些键的
    页面不渲染右下角悬浮入口；未配置（无行）或名单为空表示全部页面可见，
    与功能上线前的行为保持一致。新增导航页面默认可见，无需回填。
    """

    __tablename__ = "super_assistant_widget_config"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: "default")
    hidden_menu_keys: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class SuperAssistantDelegation(Base):
    """超级会话 → 平台助手子会话的委派映射（追加式历史，latest=active）。

    同一 (super_conversation_id, assistant_key) 允许多行：session=new 轮转
    新线，resume 永远取最近一条。仅对 status='running' 建部分唯一索引，
    防残留 running 行的并发竞态；子会话本体归各助手域（assistant_hub），
    本表只存不透明引用 conversation_ref。
    """

    __tablename__ = "super_assistant_delegations"
    __table_args__ = (
        Index(
            "uq_sa_delegation_running",
            "super_conversation_id", "assistant_key",
            unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "ix_sa_delegations_conv_key_last",
            "super_conversation_id", "assistant_key", "last_turn_at",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    super_conversation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("super_assistant_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    assistant_key: Mapped[str] = mapped_column(String(50), nullable=False)
    # assistant_hub 的不透明会话引用（首回合前为空，start 后回填）
    conversation_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # running / answered / failed / cancelled / timeout
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)
    last_turn_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

class SuperAssistantRemoteAgent(Base):
    """用户自配的远程助手（声明式注册：填 key/名称/描述/端点即入委派目录）。

    远程助手走 OpenOntology 远程助手 HTTP 契约（见 remote_agent_service）：
    单端点回合制，session_ref 由远端签发、经委派表 conversation_ref 透传
    续用。token 加密存储永不回显；menu_keys 为空（不映射平台菜单，归属
    即权限）。key 命名空间 remote.* 与平台内置助手隔离。
    """

    __tablename__ = "super_assistant_remote_agents"
    __table_args__ = (
        UniqueConstraint("owner_id", "key", name="uq_sa_remote_agent_owner_key"),
        Index("ix_sa_remote_agents_owner_enabled", "owner_id", "enabled"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(50), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    endpoint: Mapped[str] = mapped_column(String(1000), nullable=False)
    token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)
