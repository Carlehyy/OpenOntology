"""运营报告 HTTP DTO。"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.operations.plan import ReportPlan


class ReportCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20_000)


class ReportUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20_000)
    expected_revision: int | None = Field(default=None, ge=1)


class ArticleCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    dataset_id: str = Field(min_length=1, max_length=128)
    markdown: str = Field(default="# 新的运营分析\n\n在这里写下分析背景和结论。\n", max_length=700_000)
    # plan 与 markdown 二选一：plan 提供时由服务端生成 markdown。显式同传
    # 两者即拒绝（markdown 有历史默认值，用 model_fields_set 判定显式提供）。
    plan: ReportPlan | None = None

    @model_validator(mode="after")
    def validate_body_source(self):
        provided = self.model_fields_set
        if "plan" in provided and "markdown" in provided:
            raise ValueError("plan 与 markdown 只能二选一")
        return self


class ArticleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    markdown: str | None = Field(default=None, max_length=700_000)
    plan: ReportPlan | None = None
    dataset_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_body_source(self):
        if self.plan is not None and self.markdown is not None:
            raise ValueError("plan 与 markdown 只能二选一")
        return self


class ArticleReorder(BaseModel):
    article_ids: list[str] = Field(min_length=0, max_length=500)


class ArticleExecute(BaseModel):
    sql: str | None = Field(default=None, min_length=1, max_length=20_000)
    queries: dict[str, str] | None = Field(default=None, min_length=1, max_length=20)
    markdown: str | None = Field(default=None, max_length=700_000)
    params: dict[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=64)
    limit: int = Field(default=200, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_queries(self):
        if (self.sql is None) == (self.queries is None):
            raise ValueError("必须且只能提供 sql 或 queries")
        if self.queries is not None:
            if any(not name.strip() or len(name) > 128 or not sql.strip()
                   or len(sql) > 20_000 for name, sql in self.queries.items()):
                raise ValueError("查询名必须是 1..128 字符，SQL 必须是 1..20000 字符")
        return self


class ArticlePlanPreview(BaseModel):
    """计划试跑（只读）：normalize → compile → check_report_sql →
    execute_report_queries，不写文章、不落库。"""

    dataset_id: str = Field(min_length=1, max_length=128)
    plan: ReportPlan
    params: dict[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=64)
    limit: int = Field(default=200, ge=1, le=1000)


class ArticlePlanDesign(BaseModel):
    """AI 设计候选请求；服务端只返回候选，不保存文章。"""

    dataset_id: str = Field(min_length=1, max_length=128)
    brief: str = Field(min_length=1, max_length=4_000)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    current_plan: ReportPlan | None = None
