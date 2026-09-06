"""Connector 抽象基类"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class ConnectorBase(ABC):
    """所有 Connector 必须实现的接口"""

    @abstractmethod
    def test_connection(self) -> bool:
        """连接测试。成功返回 True，失败返回 False 或抛异常。"""
        ...

    @abstractmethod
    def list_resources(self) -> list[str]:
        """可用资源列表 (表名、集合名、端点等)。"""
        ...

    @abstractmethod
    def pull_sample(self, resource: str, limit: int = 100) -> list[dict]:
        """查询样本数据 (最多 limit 行)。"""
        ...

    @abstractmethod
    def pull_full(self, resource: str) -> Any:
        """返回全量数据。大数据量可返回生成器或文件路径。"""
        ...

    def pull_delta(self, resource: str, since: str | None = None) -> Any:
        """增量数据查询。默认实现与 pull_full 相同。"""
        return self.pull_full(resource)

    def introspect_schema(self, resource: str) -> list[dict]:
        """资源列清单元数据：[{name, type(湖词表), source_type, flags}]。

        实现必须基于数据源元数据反射（如 information_schema）而不是值采样；
        失败要抛异常，禁止吞掉后返回空清单——空清单会被上游当成「资源没有
        列」。不支持内省的连接器保持默认 NotImplementedError，由调用方决定
        采样兜底。
        """
        raise NotImplementedError(
            f"{type(self).__name__} 不支持 schema 内省")

    def introspect_primary_key(self, resource: str) -> list[str]:
        """资源主键列清单（复合主键按定义顺序），无主键返回空列表。

        失败必须抛异常；不支持内省的连接器保持 NotImplementedError。
        """
        raise NotImplementedError(
            f"{type(self).__name__} 不支持主键内省")
