from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CamelModel(BaseModel):
    """对外 camelCase JSON 的基座（与 formal_modeling 的 CamelModel 同约定）。

    super_assistant 包不依赖 ontologies 域，这里直接用 pydantic 内置的
    to_camel alias 生成器，不做跨域 import。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class ConversationCreate(BaseModel):
    title: str = Field(default="新会话", max_length=200)
    model_config_id: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    model_config_id: str | None = None
    # 归档/恢复：deleted 不走此通道（删除是硬删除端点）
    status: Literal["active", "archived"] | None = None


class ConversationOut(ORMModel):
    id: str
    title: str
    model_config_id: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class SearchMessageHit(CamelModel):
    message_id: str
    role: str
    snippet: str
    created_at: datetime


class SearchConversationHit(CamelModel):
    id: str
    title: str
    status: str
    updated_at: datetime
    title_matched: bool
    message_hits: list[SearchMessageHit]


class SearchResultOut(CamelModel):
    query: str
    conversations: list[SearchConversationHit]


class MessageOut(ORMModel):
    id: str
    conversation_id: str
    role: str
    content: str
    status: str
    steps: list[Any]
    token_usage: dict[str, Any]
    created_at: datetime


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    model_config_id: str | None = None
    agent_mode: bool = False


class ApprovalRequest(BaseModel):
    decision: Literal["approve", "deny"]


class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    description: str = Field(min_length=1, max_length=4000)
    content: str = Field(min_length=1, max_length=500_000)
    enabled: bool = True
    always_active: bool = False


class SkillUpdate(BaseModel):
    enabled: bool | None = None
    always_active: bool | None = None


class SkillOut(ORMModel):
    id: str
    name: str
    description: str
    manifest: list[dict[str, Any]]
    enabled: bool
    always_active: bool
    use_count: int
    last_used_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class SkillFileOut(BaseModel):
    path: str
    size: int
    editable: bool


class SkillFileContent(BaseModel):
    content: str = Field(max_length=5_000_000)


class McpServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    display_name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=500)
    transport: Literal["stdio", "sse", "streamable_http"] = "streamable_http"
    url: str = Field(default="", max_length=1000)
    headers: dict[str, str] = Field(default_factory=dict)
    command: str | None = Field(default=None, max_length=1000)
    args: list[str] = Field(default_factory=list, max_length=100)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    require_confirmation: bool = True

    @field_validator("display_name", "description", mode="before")
    @classmethod
    def strip_display_fields(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("transport", mode="before")
    @classmethod
    def normalize_transport(cls, value: str) -> str:
        return str(value).strip().lower().replace("-", "_").replace(" ", "_")

    @field_validator("headers", "env")
    @classmethod
    def validate_string_map(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 30:
            raise ValueError("配置项不能超过 30 个")
        for key, item in value.items():
            if not key or len(key) > 200 or "\n" in key or "\r" in key:
                raise ValueError("配置项名称无效")
            if len(item) > 8000 or "\n" in item or "\r" in item:
                raise ValueError(f"配置项 {key} 的值无效")
        return value

    @field_validator("args")
    @classmethod
    def validate_args(cls, value: list[str]) -> list[str]:
        if any(len(item) > 8000 or "\x00" in item for item in value):
            raise ValueError("MCP args 包含无效参数")
        return value


class McpServerUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    transport: Literal["stdio", "sse", "streamable_http"] | None = None
    url: str | None = Field(default=None, max_length=1000)
    headers: dict[str, str] | None = None
    command: str | None = Field(default=None, max_length=1000)
    args: list[str] | None = Field(default=None, max_length=100)
    env: dict[str, str] | None = None
    enabled: bool | None = None
    require_confirmation: bool | None = None

    @field_validator("display_name", "description", mode="before")
    @classmethod
    def strip_display_fields(cls, value: str | None) -> str | None:
        return str(value).strip() if value is not None else None

    @field_validator("transport", mode="before")
    @classmethod
    def normalize_transport(cls, value: str | None) -> str | None:
        return McpServerCreate.normalize_transport(value) if value is not None else None

    @field_validator("headers", "env")
    @classmethod
    def validate_string_map(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        return McpServerCreate.validate_string_map(value)

    @field_validator("args")
    @classmethod
    def validate_args(cls, value: list[str] | None) -> list[str] | None:
        return McpServerCreate.validate_args(value) if value is not None else None


class McpServerOut(ORMModel):
    id: str
    name: str
    display_name: str
    description: str
    builtin_key: str | None
    transport: str
    url: str
    header_names: list[str]
    command: str | None
    args: list[str]
    env_names: list[str]
    enabled: bool
    require_confirmation: bool
    tool_manifest: list[dict[str, Any]]
    last_test_status: str | None
    last_test_message: str | None
    last_tested_at: datetime | None
    created_at: datetime
    updated_at: datetime


class McpTestOut(BaseModel):
    ok: bool
    message: str
    tools: list[dict[str, Any]] = Field(default_factory=list)


class MulticaCommandOut(BaseModel):
    command: str
    title: str
    description: str
    usage: str
    write: bool


class MulticaConfigOut(BaseModel):
    configured: bool
    enabled: bool
    base_url: str
    workspace_id: str
    # 工作区显示名（保存/测试连接时回填）：配置弹窗下拉兜底显示名称而非裸 UUID
    workspace_name: str = ""
    token_set: bool
    # 命令目录由后端统一下发：未配置/未启用时为空，前端据此决定
    # 输入框是否展示 /multica: 命令提示（未配置不可用）
    commands: list[MulticaCommandOut] = Field(default_factory=list)
    last_test_status: str | None
    last_test_message: str | None
    last_tested_at: datetime | None


class RemoteAgentOut(BaseModel):
    id: str
    key: str
    label: str
    description: str
    endpoint: str
    token_set: bool
    enabled: bool
    timeout_seconds: int


class RemoteAgentCreate(BaseModel):
    # key 命名空间 remote.* 与平台内置助手隔离（校验见 remote_agent_service）
    key: str = Field(min_length=1, max_length=50)
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    endpoint: str = Field(min_length=1, max_length=1000)
    # token 加密存储、永不回显
    token: str | None = Field(default=None, max_length=2000)
    enabled: bool = True
    timeout_seconds: int = 120


class RemoteAgentUpdate(BaseModel):
    # 全部可选：缺省/None 表示不修改；token 留空表示保留已存凭据
    label: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    endpoint: str | None = Field(default=None, min_length=1, max_length=1000)
    token: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=10, le=600)


class RemoteAgentTestOut(BaseModel):
    ok: bool
    message: str


class MulticaConfigUpdate(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    # token 留空/缺省表示保留已保存凭据（不回显、不覆盖）
    token: str | None = Field(default=None, max_length=500)
    workspace_id: str = Field(min_length=1, max_length=100)
    # 工作区显示名：前端从测试连接返回的工作区列表带回（可选，纯展示用途）
    workspace_name: str | None = Field(default=None, max_length=200)
    enabled: bool = True

    @field_validator("base_url", "workspace_id", mode="before")
    @classmethod
    def strip_required_fields(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("token", mode="before")
    @classmethod
    def strip_token(cls, value: str | None) -> str | None:
        return None if value is None else str(value).strip()

    @field_validator("workspace_name", mode="before")
    @classmethod
    def strip_workspace_name(cls, value: str | None) -> str | None:
        return None if value is None else str(value).strip() or None


class MulticaTestRequest(BaseModel):
    # 草稿测试（保存前）传入表单值；缺省回落到已保存配置
    base_url: str | None = Field(default=None, max_length=500)
    token: str | None = Field(default=None, max_length=500)

    @field_validator("base_url", "token", mode="before")
    @classmethod
    def strip_optional_fields(cls, value: str | None) -> str | None:
        return None if value is None else str(value).strip() or None


class MulticaWorkspaceOut(BaseModel):
    id: str
    name: str
    slug: str


class MulticaTestOut(BaseModel):
    ok: bool
    message: str
    account_name: str | None = None
    workspaces: list[MulticaWorkspaceOut] = Field(default_factory=list)


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    zone: str = Field(default="general", min_length=1, max_length=50)
    pinned: bool = False
    tags: list[str] = Field(default_factory=list, max_length=20)


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=50_000)
    zone: str | None = Field(default=None, min_length=1, max_length=50)
    pinned: bool | None = None
    tags: list[str] | None = Field(default=None, max_length=20)


class MemoryOut(ORMModel):
    id: str
    content: str
    zone: str
    pinned: bool
    confidence: str
    source: str
    tags: list[str]
    supersedes: list[str]
    superseded: bool
    match_count: int
    reference_count: int
    last_accessed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MemoryDistillMember(BaseModel):
    id: str
    content: str
    zone: str
    pinned: bool
    match_count: int
    reference_count: int
    created_at: datetime


class MemoryDistillCluster(BaseModel):
    cluster_key: str
    members: list[MemoryDistillMember]
    survivor_id: str
    protected: bool


class MemoryDistillReport(BaseModel):
    clusters: list[MemoryDistillCluster]


class MemoryDistillRequest(BaseModel):
    member_ids: list[str] = Field(max_length=50)
    merged_content: str | None = Field(default=None, max_length=50_000)
    use_llm: bool = False


class ReflectionCandidateOut(ORMModel):
    id: str
    run_id: str
    conversation_id: str
    kind: str
    status: str
    confidence: str
    payload: dict[str, Any]
    decision: str | None
    created_at: datetime
    decided_at: datetime | None


class ReflectionDecisionRequest(BaseModel):
    decision: str = Field(min_length=1, max_length=30)
    payload: dict[str, Any] | None = None


class ReflectionFullRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=100)


class ReflectionFullAccepted(BaseModel):
    dispatched: bool
    runId: str | None = None


class ReflectionSettingsOut(BaseModel):
    auto_accept_enabled: bool
    palace_index: str | None
    profile: str | None
    memory_count: int
    pending_count: int


class ReflectionSettingsUpdate(BaseModel):
    auto_accept_enabled: bool


class WidgetConfigOut(ORMModel):
    hidden_menu_keys: list[str]
    # 未配置过（无配置行）时为 None，语义等同于空名单
    updated_at: datetime | None


class WidgetConfigUpdate(BaseModel):
    # 隐藏名单语义见 models.SuperAssistantWidgetConfig；菜单键是前端 navigation.ts
    # 的叶子菜单键（如 ontologies、data.pipelines、settings.users）
    hidden_menu_keys: list[str] = Field(max_length=200)

    @field_validator("hidden_menu_keys")
    @classmethod
    def _normalize_keys(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in value:
            key = raw.strip()
            if not key or len(key) > 100:
                raise ValueError("菜单键不能为空且长度不能超过 100")
            if key not in normalized:
                normalized.append(key)
        return normalized
