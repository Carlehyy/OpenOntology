"""消息通知 — Pydantic 契约（入参校验；出参由 service 层组装 camelCase dict）"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Priority = Literal["urgent", "high", "normal", "low"]


class NotificationCreate(BaseModel):
    """管理员手动发送消息（= 正式消息，会进入站内并触发渠道转发）。"""

    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=524288)  # 512 KiB 正文上限
    priority: Priority = "normal"


class NotificationStateUpdate(BaseModel):
    """处置状态部分更新：至少携带一个字段，未携带的字段保持原值。"""

    isRead: bool | None = None
    isStarred: bool | None = None
    isArchived: bool | None = None


class NotificationIngestKeyCreate(BaseModel):
    """签发对外投递密钥；明文仅在创建响应中一次性返回。"""

    name: str = Field(min_length=1, max_length=200)
    allowedSourceSystem: str | None = Field(default=None, max_length=200)


class NotificationChannelCreate(BaseModel):
    """新建转发渠道；URL 为 apprise 语法（mailto://、json://、dingtalk://…）。"""

    name: str = Field(min_length=1, max_length=200)
    appriseUrl: str = Field(min_length=1, max_length=2000)
    note: str | None = Field(default=None, max_length=500)


class NotificationChannelUpdate(BaseModel):
    """渠道部分更新：未携带字段保持原值（URL 留空表示不更换）。"""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    appriseUrl: str | None = Field(default=None, max_length=2000)
    note: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None


class NotificationChannelCreateV2(BaseModel):
    """模板化新建渠道：选类型填关键字段；custom 时 params.url 为完整 apprise URL。"""

    name: str = Field(min_length=1, max_length=200)
    template: str = Field(min_length=1, max_length=32)
    params: dict[str, str] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=500)


class NotificationChannelUpdateV2(BaseModel):
    """渠道更新：未携带字段保持原值；params 携带则整体重建 URL。"""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None
    params: dict[str, str] | None = None


class NotificationSmtpUpdate(BaseModel):
    """平台发件邮箱设置；password 留空/缺省 = 保持不变。"""

    host: str = Field(min_length=1, max_length=200)
    port: int = Field(default=465, ge=1, le=65535)
    username: str = Field(max_length=200)
    password: str | None = Field(default=None, max_length=500)
    sender: str = Field(default="", max_length=200)
    useTls: bool = True


class NotificationSmtpTest(BaseModel):
    to: str = Field(min_length=3, max_length=200)


class NotificationForwardRequest(BaseModel):
    """手动转发：单条消息 → 多渠道。"""

    channelIds: list[str] = Field(min_length=1, max_length=50)
