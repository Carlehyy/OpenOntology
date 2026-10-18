"""统一关联查询请求/响应契约 — extra=forbid，标识符仅从发布定义解析。"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from app.ontologies.formal_modeling.schemas import CamelModel


class _Strict(CamelModel):
    """查询面契约：拒绝而非丢弃未知字段。"""

    model_config = ConfigDict(
        alias_generator=CamelModel.model_config["alias_generator"],
        populate_by_name=True,
        extra="forbid",
    )


class QuerySource(_Strict):
    alias: str = Field(min_length=1, max_length=32,
                       pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    ontology_id: str = Field(min_length=1, max_length=64)
    release_id: str = Field(min_length=1, max_length=64)
    object_type_id: str = Field(min_length=1, max_length=200)


class ExplicitKeyOn(_Strict):
    kind: Literal["explicit_key"]
    left_property: str = Field(min_length=1, max_length=200)
    right_property: str = Field(min_length=1, max_length=200)
    key_type: Literal["string", "number", "uuid"]
    cardinality: Literal["1:1", "1:N", "N:1", "N:M"]
    business_meaning: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class DeclaredRelationOn(_Strict):
    kind: Literal["declared_relation"]
    link_type_id: str = Field(min_length=1, max_length=200)
    direction: Literal["source_to_target", "target_to_source"]


class QueryJoin(_Strict):
    left: str = Field(min_length=1, max_length=32)
    right: str = Field(min_length=1, max_length=32)
    type: Literal["inner", "left"]
    on: Union[ExplicitKeyOn, DeclaredRelationOn] = Field(discriminator="kind")


_FILTER_OPS = (
    "eq", "neq", "gt", "gte", "lt", "lte",
    "contains", "in", "is_null", "is_not_null",
)


class FilterLeaf(_Strict):
    source: str = Field(min_length=1, max_length=32)
    property: str = Field(min_length=1, max_length=200)
    op: Literal["eq", "neq", "gt", "gte", "lt", "lte",
                "contains", "in", "is_null", "is_not_null"]
    value: Optional[Any] = None


class FilterAnd(_Strict):
    and_: list["FilterNode"] = Field(alias="and", min_length=1, max_length=8)


class FilterOr(_Strict):
    or_: list["FilterNode"] = Field(alias="or", min_length=1, max_length=8)


class FilterNot(_Strict):
    not_: "FilterNode" = Field(alias="not")


FilterNode = Union[FilterAnd, FilterOr, FilterNot, FilterLeaf]


class SourceFilter(_Strict):
    source: str = Field(min_length=1, max_length=32)
    filter: FilterNode


class SelectItem(_Strict):
    source: str = Field(min_length=1, max_length=32)
    property: str = Field(min_length=1, max_length=200)
    as_: str = Field(alias="as", min_length=1, max_length=64,
                     pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class GroupByItem(_Strict):
    source: str = Field(min_length=1, max_length=32)
    property: str = Field(min_length=1, max_length=200)


class AggregateItem(_Strict):
    fn: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    source: Optional[str] = Field(default=None, max_length=32)
    property: Optional[str] = Field(default=None, max_length=200)
    as_: str = Field(alias="as", min_length=1, max_length=64,
                     pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class OrderByItem(_Strict):
    output: str = Field(min_length=1, max_length=64)
    direction: Literal["asc", "desc"] = "asc"
    nulls: Literal["first", "last"] = "last"


class QueryRequest(_Strict):
    sources: list[QuerySource] = Field(min_length=1, max_length=4)
    joins: list[QueryJoin] = Field(default_factory=list, max_length=3)
    source_filters: list[SourceFilter] = Field(default_factory=list, max_length=8)
    where: list[FilterNode] = Field(default_factory=list, max_length=16)
    select: list[SelectItem] = Field(default_factory=list, max_length=32)
    group_by: list[GroupByItem] = Field(default_factory=list, max_length=8)
    aggregates: list[AggregateItem] = Field(default_factory=list, max_length=8)
    distinct: bool = False
    order_by: list[OrderByItem] = Field(default_factory=list, max_length=8)
    page_size: int = Field(default=50, ge=1, le=200)


class CursorRequest(_Strict):
    cursor: str = Field(min_length=16, max_length=2048)


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryResponse(_Envelope):
    query_id: str
    schema_version: int
    observed_at: str
    releases: dict[str, dict[str, Any]]
    columns: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    page_row_count: int
    total_row_count: int
    distinct_entity_counts: dict[str, int]
    input_counts: dict[str, int]
    join_row_counts: list[int]
    partial: bool
    truncated: bool
    counts_scope: str
    has_next: bool
    cursor: Optional[str] = None
    expires_at: str
    citations: list[dict[str, Any]]
