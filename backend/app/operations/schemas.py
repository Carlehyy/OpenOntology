"""运营报告 HTTP DTO。"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


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


class ArticleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    markdown: str | None = Field(default=None, max_length=700_000)
    dataset_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=1)


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
