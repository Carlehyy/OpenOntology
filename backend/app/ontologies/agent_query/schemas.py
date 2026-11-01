"""统一关联查询请求/响应契约 — extra=forbid，标识符仅从发布定义解析。"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    ontology_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    release_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    object_type_id: Optional[str] = Field(default=None, min_length=1, max_length=200)
    subquery: Optional["QueryRequest"] = None

    @model_validator(mode="after")
    def validate_source(self):
        identity = (self.ontology_id, self.release_id, self.object_type_id)
        if self.subquery is None:
            if not all(identity):
                raise ValueError("ontologyId/releaseId/objectTypeId are required for an object source")
        elif any(value is not None for value in identity):
            raise ValueError("A derived source cannot also specify an object source")
        return self


class ExplicitKeyOn(_Strict):
    kind: Literal["explicit_key"]
    left_property: str = Field(min_length=1, max_length=200)
    right_property: str = Field(min_length=1, max_length=200)
    key_type: Literal["string", "number", "uuid"]
    cardinality: Literal["1:1", "1:N", "N:1", "N:M"]
    business_meaning: str = Field(min_length=1, max_length=2000)
    # 空证据以带治理指引的错误拒绝（仍是 422，不放松校验）：结构性
    # min_length 报文不含可操作指引，跨本体 explicit_key 必须引导先登记
    # 受治理的业务键声明再重试。
    evidence_refs: list[str] = Field(max_length=8)

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_require_governance(cls, value: list[str]) -> list[str]:
        if not value or not any(str(item).strip() for item in value):
            raise ValueError(
                "explicit_key 关联需要已发布关系或受治理的业务键证据："
                "请先经 ontology_register_join_assertion 工具或 "
                "/api/v2/formal/ontologies/{id}/agent/join-assertions 登记"
                "两端对象与键型一致的声明，再携带 assertion 证据重试")
        return value


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


class SemanticBinding(_Strict):
    """SPL 文本查询的语义叶子绑定：仅发布三元组，拒绝其他身份字段。"""
    ontology_id: str = Field(min_length=1, max_length=64)
    release_id: str = Field(min_length=1, max_length=64)
    object_type_id: str = Field(min_length=1, max_length=200)


class QueryRequest(_Strict):
    sources: list[QuerySource] = Field(default_factory=list, min_length=0,
                                       max_length=4)
    joins: list[QueryJoin] = Field(default_factory=list, max_length=3)
    source_filters: list[SourceFilter] = Field(default_factory=list, max_length=8)
    where: list[FilterNode] = Field(default_factory=list, max_length=16)
    select: list[SelectItem] = Field(default_factory=list, max_length=32)
    group_by: list[GroupByItem] = Field(default_factory=list, max_length=8)
    aggregates: list[AggregateItem] = Field(default_factory=list, max_length=8)
    distinct: bool = False
    order_by: list[OrderByItem] = Field(default_factory=list, max_length=8)
    page_size: Optional[int] = Field(default=None, ge=1, le=200)
    # SPL 文本面：spl 存在时结构树必须为空（两种形态互斥，翻译在
    # remote_query.prepare_spl_plan 中进行，授权/证据核验全部复用结构树路径）。
    spl: Optional[str] = Field(default=None, min_length=1, max_length=32768)
    semantic_bindings: Optional[dict[str, SemanticBinding]] = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_request_shape(self):
        tree_fields = (self.sources, self.joins, self.source_filters,
                       self.where, self.select, self.group_by,
                       self.aggregates, self.order_by)
        tree_used = any(tree_fields) or self.distinct
        if self.spl is not None:
            if tree_used:
                raise ValueError(
                    "spl 文本请求不能同时携带结构树字段（sources/joins/...），两种形态互斥")
            if not self.semantic_bindings:
                raise ValueError("spl 文本请求必须提供 semanticBindings 叶子绑定")
        else:
            if self.semantic_bindings is not None:
                raise ValueError("semanticBindings 仅在 spl 文本请求中有效")
            if self.params:
                raise ValueError("params 仅在 spl 文本请求中有效")
            if not self.sources:
                raise ValueError("sources 至少需要一个对象源")
        return self

    @model_validator(mode="after")
    def validate_plan_budget(self):
        if self.spl is not None:
            return self  # 结构预算在翻译后的结构树上执行
        leaves = 0

        def visit(plan, depth):
            nonlocal leaves
            if depth > 2:
                raise ValueError("Derived query depth exceeds 2")
            for source in plan.sources:
                if source.subquery is None:
                    leaves += 1
                    if leaves > 16:
                        raise ValueError("Query exceeds 16 object sources")
                else:
                    visit(source.subquery, depth + 1)

        visit(self, 0)
        return self


QuerySource.model_rebuild()


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
    # Lazy keyset paging knows the total only when the page probe reports the
    # end of the ordering; while paging it reports null instead of counting
    # the whole source. row_counting states which of the two holds.
    total_row_count: Optional[int] = None
    row_counting: Literal["complete", "page_probe"] = "complete"
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


class JoinAssertionCreate(_Strict):
    left_ontology_id: str = Field(min_length=1, max_length=64)
    left_object_type_id: str = Field(min_length=1, max_length=200)
    left_property: str = Field(min_length=1, max_length=200)
    right_ontology_id: str = Field(min_length=1, max_length=64)
    right_object_type_id: str = Field(min_length=1, max_length=200)
    right_property: str = Field(min_length=1, max_length=200)
    key_type: Literal["string", "number", "uuid"]
    business_meaning: str = Field(min_length=1, max_length=2000)
