"""MCP 工具输入契约 — extra=forbid，只接受结构树，SPL 面恒不可达。

树字段复用 ``agent_query.schemas`` 纯数据模型（约束一致，无第二套 DSL）；
源身份（ontologyId/releaseId）不接受客户端传入，由服务端注入；
subquery 递归为 McpQueryTree，SPL 子查询暗门在递归类型中不存在。
"""
from __future__ import annotations

from typing import Optional

from pydantic import ConfigDict, Field, model_validator

from app.ontologies.agent_query.schemas import (
    MAX_PAGE_SIZE,
    MAX_SELECT_ITEMS,
    MAX_SOURCES,
    AggregateItem,
    FilterNode,
    GroupByItem,
    OrderByItem,
    QueryJoin,
    SelectItem,
    SourceFilter,
)
from app.ontologies.formal_modeling.schemas import CamelModel


class _Strict(CamelModel):
    """MCP 工具面契约：拒绝而非丢弃未知字段。"""

    model_config = ConfigDict(
        alias_generator=CamelModel.model_config["alias_generator"],
        populate_by_name=True,
        extra="forbid",
    )


class McpQuerySource(_Strict):
    """结构树源：叶子写 alias+objectTypeId，派生源写 alias+subquery。

    ontologyId/releaseId 由服务端注入（绑定本体 + 执行前固定 release），
    客户端传入即被 extra=forbid 拒绝 —— 跨本体源在输入层不可表达。
    与 QS.QuerySource 同语义：对象源与派生源互斥、二选一必填。
    """

    alias: str = Field(min_length=1, max_length=32,
                       pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    object_type_id: Optional[str] = Field(default=None, min_length=1,
                                          max_length=200)
    subquery: Optional["McpQueryTree"] = None

    @model_validator(mode="after")
    def validate_source(self):
        if self.subquery is None:
            if not self.object_type_id:
                raise ValueError(
                    "objectTypeId is required for an object source")
        elif self.object_type_id is not None:
            raise ValueError(
                "A derived source cannot also specify an objectTypeId")
        return self


class McpQueryTree(_Strict):
    """结构树请求体：与 QS.QueryRequest 同形，但无 spl/params/semanticBindings。"""

    sources: list[McpQuerySource] = Field(min_length=1, max_length=MAX_SOURCES)
    joins: list[QueryJoin] = Field(default_factory=list, max_length=7)
    source_filters: list[SourceFilter] = Field(default_factory=list, max_length=8)
    where: list[FilterNode] = Field(default_factory=list, max_length=16)
    select: list[SelectItem] = Field(default_factory=list,
                                     max_length=MAX_SELECT_ITEMS)
    group_by: list[GroupByItem] = Field(default_factory=list, max_length=8)
    aggregates: list[AggregateItem] = Field(default_factory=list, max_length=8)
    distinct: bool = False
    order_by: list[OrderByItem] = Field(default_factory=list, max_length=8)
    page_size: Optional[int] = Field(default=None, ge=1, le=MAX_PAGE_SIZE)
    bounded_stats: bool = False


McpQuerySource.model_rebuild()


class McpSchemaInput(_Strict):
    """ontology_schema 输入：无参即可 bootstrap（返回当前版本对象目录页）。"""

    release_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    object_type_id: Optional[str] = Field(default=None, min_length=1,
                                          max_length=200)
    cursor: Optional[str] = Field(default=None, min_length=16, max_length=2048)


class McpQueryInput(_Strict):
    """ontology_query 输入：releaseId 必填（先读 schema），树字段同 QS。"""

    release_id: str = Field(min_length=1, max_length=64)
    query: McpQueryTree


class McpPageInput(_Strict):
    """ontology_query_page 输入：只认上一页返回的 cursor。"""

    cursor: str = Field(min_length=16, max_length=2048)
