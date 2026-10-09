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
