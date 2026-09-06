"""本体智能体 API Schemas — 对外 camelCase，复用 formal 的 CamelModel 约定。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import ConfigDict, Field, StrictBool, conint, field_validator, model_validator

from app.ontologies.formal_modeling.schemas import CamelModel


class AgentProfileOut(CamelModel):
    id: str
    ontology_id: str
    enabled: bool
    allowed_object_type_ids: Optional[list[str]] = None
    allowed_link_type_ids: Optional[list[str]] = None
    allowed_action_ids: Optional[list[str]] = None
    allow_action_proposals: bool = True
    max_rows_per_query: int = 50
    max_steps: int = 8
    system_prompt_extra: str = ""
    default_model_id: Optional[str] = None
    updated_at: datetime


class AgentProfileUpdate(CamelModel):
    enabled: Optional[bool] = None
    allowed_object_type_ids: Optional[list[str]] = None
    allowed_link_type_ids: Optional[list[str]] = None
    allowed_action_ids: Optional[list[str]] = None
    allow_action_proposals: Optional[bool] = None
    max_rows_per_query: Optional[int] = None
    max_steps: Optional[int] = None
    system_prompt_extra: Optional[str] = None
    default_model_id: Optional[str] = None
    # 白名单三态（None=全部允许）无法用 exclude_unset 之外的方式表达清空，
    # 所以显式列出本次要「重置为全部允许」的字段名
    reset_to_all: list[str] = Field(default_factory=list)


class ChatRequest(CamelModel):
    message: str
    conversation_id: Optional[str] = None
    model_id: Optional[str] = None
    release_id: Optional[str] = None
    stream: bool = True
    # 客户端生成的回合标识：取消端点与取消注册表用它定位正在执行的回合
    run_id: Optional[str] = None


class ChatCancelRequest(CamelModel):
    run_id: str


class ExecuteProposalRequest(CamelModel):
    action_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    target_instance_id: Optional[str] = None
    release_id: Optional[str] = None


class _StrictDynamicModel(CamelModel):
    """LLM-facing mutation contracts reject, rather than silently discard, input."""

    model_config = ConfigDict(extra="forbid")


class DynamicSentinelBinding(_StrictDynamicModel):
    alias: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    object_type_id: str = Field(min_length=1, max_length=200)
    filter: Optional[str] = Field(default=None, max_length=2000)


class DynamicSentinelLink(_StrictDynamicModel):
    from_alias: str = Field(alias="from", min_length=1, max_length=50)
    link_type_id: str = Field(min_length=1, max_length=200)
    to: str = Field(min_length=1, max_length=50)


class DynamicSentinelDefinition(_StrictDynamicModel):
    name: str = Field(
        min_length=2, max_length=100,
        pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$",
    )
    display_name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    bindings: list[DynamicSentinelBinding] = Field(min_length=1, max_length=12)
    links: list[DynamicSentinelLink] = Field(default_factory=list, max_length=24)
    condition: Optional[str] = Field(default=None, max_length=4000)
    condition_rows: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    condition_logic: str = Field(default="and", pattern=r"^(and|or)$")
    primary_alias: str = Field(min_length=1, max_length=50)
    action_ids: list[str] = Field(default_factory=list, max_length=20)
    action_parameters: dict[str, Any] = Field(default_factory=dict)
    on_change: StrictBool = True
    on_schedule: StrictBool = False
    scan_interval_seconds: conint(strict=True, ge=60, le=86400) = 300
    trigger_mode: str = Field(
        default="on_enter",
        pattern=r"^(on_enter|on_enter_leave|run_on_all|on_pattern)$",
    )
    # CEP 模式定义（triggerMode='on_pattern' 时必填；深度结构由共享
    # validate_sentinels 校验，Pydantic 只做存在性门禁）。
    pattern: Optional[dict[str, Any]] = None
    muted: StrictBool = False

    @field_validator("name", "display_name")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_trigger(self):
        if not self.on_change and not self.on_schedule:
            raise ValueError("动态哨兵至少需要启用变化触发或定时扫描之一")
        aliases = [item.alias for item in self.bindings]
        if len(aliases) != len(set(aliases)):
            raise ValueError("bindings alias 不允许重复")
        if self.primary_alias not in aliases:
            raise ValueError("primaryAlias 必须引用已声明的 binding alias")
        if len(self.action_ids) != len(set(self.action_ids)):
            raise ValueError("actionIds 不允许重复")
        if self.trigger_mode == "on_pattern":
            if not isinstance(self.pattern, dict) or not self.pattern:
                raise ValueError(
                    "triggerMode=on_pattern 时必须携带 pattern 模式定义")
            if not self.on_change or not self.on_schedule:
                raise ValueError(
                    "模式哨兵必须同时开启变化触发与定时扫描"
                    "（onChange/onSchedule）")
        elif self.pattern:
            raise ValueError("pattern 仅在 triggerMode=on_pattern 时允许出现")
        if self.condition_rows:
            raise ValueError("助手动态哨兵只接受 condition 作为唯一执行条件，conditionRows 必须为空")
        return self


class DynamicSentinelCreateRequest(_StrictDynamicModel):
    release_id: str = Field(min_length=1)
    definition: DynamicSentinelDefinition


class DynamicSentinelUpdateRequest(_StrictDynamicModel):
    release_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    definition: DynamicSentinelDefinition


class DynamicSentinelReleaseRequest(_StrictDynamicModel):
    release_id: str = Field(min_length=1)


class DynamicSentinelToggleRequest(DynamicSentinelReleaseRequest):
    enabled: StrictBool
    expected_revision: int = Field(ge=1)


class DynamicSentinelProposalCommand(_StrictDynamicModel):
    operation: str = Field(pattern=r"^(create|update|enable|disable|delete)$")
    release_id: str
    sentinel_id: Optional[str] = None
    expected_revision: Optional[int] = Field(default=None, ge=1)
    definition: Optional[DynamicSentinelDefinition] = None

    @model_validator(mode="after")
    def _validate_operation_payload(self):
        if self.operation == "create" and self.definition is None:
            raise ValueError("创建动态哨兵必须提供 definition")
        if self.operation != "create" and not self.sentinel_id:
            raise ValueError("该操作必须提供 sentinelId")
        if self.operation != "create" and self.expected_revision is None:
            raise ValueError("该操作必须提供 expectedRevision，防止覆盖并发修改")
        if self.operation == "update" and self.definition is None:
            raise ValueError("更新动态哨兵必须提供 definition")
        if self.operation not in {"create", "update"} and self.definition is not None:
            raise ValueError("启停或删除操作不能携带 definition")
        return self


class GraphPathRequest(CamelModel):
    release_id: Optional[str] = None
    source_instance_id: str
    target_instance_id: str
    direction: str = "both"
    max_depth: int = Field(default=5, ge=1, le=6)
    max_paths: int = Field(default=3, ge=1, le=5)


class GraphImpactRequest(CamelModel):
    release_id: Optional[str] = None
    instance_id: str
    property: str
    proposed_value: Any = None
    direction: str = "both"
    max_depth: int = Field(default=3, ge=1, le=4)


class ConversationOut(CamelModel):
    id: str
    title: str
    ontology_release_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class MessageOut(CamelModel):
    id: str
    role: str
    content: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    model: Optional[str] = None
    token_usage: Optional[dict[str, Any]] = None
    created_at: datetime


class ReportTemplateAIDraftRequest(CamelModel):
    brief: str
    model_id: Optional[str] = None
    conversation_id: Optional[str] = None


class ReportTemplateUpdate(CamelModel):
    expected_revision: int
    name: str
    description: str = ""
    sections: list[dict[str, Any]] = Field(default_factory=list)
    style: dict[str, Any] = Field(default_factory=dict)
    default_model_id: Optional[str] = None


class ReportRunRequest(CamelModel):
    model_id: Optional[str] = None


class ReportTemplateOut(CamelModel):
    id: str
    ontology_id: str
    created_by: str
    name: str
    description: str = ""
    source_prompt: str = ""
    generation_mode: str
    status: str
    revision: int
    sections: list[dict[str, Any]] = Field(default_factory=list)
    style: dict[str, Any] = Field(default_factory=dict)
    default_model_id: Optional[str] = None
    last_preview_run_id: Optional[str] = None
    last_preview_revision: Optional[int] = None
    published_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class ReportRunOut(CamelModel):
    id: str
    template_id: str
    ontology_id: str
    created_by: str
    trigger_type: str
    status: str
    template_revision: int
    template_snapshot: dict[str, Any] = Field(default_factory=dict)
    section_results: list[dict[str, Any]] = Field(default_factory=list)
    quality_report: dict[str, Any] = Field(default_factory=dict)
    html_content: str = ""
    error_message: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
