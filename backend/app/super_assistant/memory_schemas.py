"""跨会话记忆管理 API 的请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MemorySettingsOut(BaseModel):
    enabled: bool
    model_config_id: str | None = None
    web_url: str | None = None


class MemoryConsoleEntryOut(BaseModel):
    # 平台 origin 下的相对中继地址：浏览器经它换取控制面会话 cookie。
    redirect_url: str


class MemorySettingsUpdate(BaseModel):
    enabled: bool | None = None
    model_config_id: str | None = None


class MemoryStatusOut(BaseModel):
    state: Literal["ready", "unconfigured", "unavailable", "syncing", "error"]
    message: str = ""
    pending_count: int = 0
    failed_count: int = 0
    last_synced_at: datetime | None = None


class SourceOut(BaseModel):
    id: str
    role: str
    content: str
    occurred_at: datetime
    conversation_id: str | None = None
    message_id: str | None = None
    explicit: bool = False
    correction_of: str | None = None
    status: str
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SourceListOut(BaseModel):
    items: list[SourceOut]
    total: int


class SourceCreate(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    correction_of: str | None = None


class RecallItemOut(BaseModel):
    id: str
    text: str
    type: str | None = None
    document_id: str | None = None
    occurred_start: str | None = None
    occurred_end: str | None = None
    source_ids: list[str] = Field(default_factory=list)


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


class MemorySearchOut(BaseModel):
    state: str
    message: str | None = None
    items: list[RecallItemOut] = Field(default_factory=list)
