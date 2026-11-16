"""监控看板卡片 HTTP DTO（冻结契约 §4）。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

# 图标白名单；该顺序同时是前端选择器顺序，activity 为表单初值/模型缺省。
DASHBOARD_ICONS = (
    "activity",
    "gauge",
    "server",
    "database",
    "cpu",
    "hard-drive",
    "network",
    "bell-ring",
    "line-chart",
    "layout-dashboard",
)


class DashboardCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    icon: str = Field(min_length=1, max_length=64)

    @field_validator("name", "description", mode="before")
    @classmethod
    def _trim_metadata(cls, value, info):
        if isinstance(value, str):
            trimmed = value.strip()
            # 空白名称保留给业务层返回 dashboard_name_required。
            if info.field_name == "description" or trimmed:
                return trimmed
        return value


class DashboardUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    icon: str | None = Field(default=None, min_length=1, max_length=64)
    expected_revision: int | None = Field(default=None, ge=1)

    @field_validator("name", "description", mode="before")
    @classmethod
    def _trim_metadata(cls, value, info):
        if isinstance(value, str):
            trimmed = value.strip()
            if info.field_name == "description" or trimmed:
                return trimmed
        return value

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_none(cls, values):
        # 字段缺省 = 不修改；显式 null 是客户端错误，走标准 Pydantic 422
        # 结构，不伪造业务错误码。
        if isinstance(values, dict):
            for key in ("name", "description", "icon", "expected_revision"):
                if key in values and values[key] is None:
                    raise ValueError(f"{key} 不允许为 null，缺省表示不修改")
        return values


class DashboardReorder(BaseModel):
    # 必须是库内全部卡片 ID 的精确排列：长度与集合相等，无重复/未知/遗漏。
    # 全量、不分页：无条数上限（501 张卡片全量重排是验收用例，客户端不得
    # 截断提交）。
    dashboard_ids: list[str] = Field(min_length=0)
