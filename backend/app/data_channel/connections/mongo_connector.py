"""MongoDB Connector — 支持 SNAPSHOT 与增量(基于 _id 水位线)"""
from __future__ import annotations
import logging
from typing import Any
from app.services.connection.base import ConnectorBase

logger = logging.getLogger(__name__)


class MongoConnector(ConnectorBase):
    """
    MongoDB 数据源连接器。

    config 示例:
    {
        "uri": "mongodb://user:pass@host:27017/dbname",
        "database": "mydb",
        "collection": "orders"   # 可选, 未指定时 list_resources() 返回所有集合
    }
    """

    def __init__(self, config: dict):
        self._config = config
        self._client = None
        self._db = None

    def _get_db(self):
        """返回 MongoDB 数据库实例 (延迟初始化)"""
        if self._db is None:
            try:
                from pymongo import MongoClient
                self._client = MongoClient(
                    self._config["uri"],
                    serverSelectionTimeoutMS=5000,
                )
                db_name = self._config.get("database", "")
                if not db_name:
                    # 从 URI 解析数据库名
                    db_name = self._config["uri"].split("/")[-1].split("?")[0] or "test"
                self._db = self._client[db_name]
            except ImportError:
                raise RuntimeError("pymongo 未安装, 请执行 pip install pymongo")
        return self._db

    def test_connection(self) -> bool:
        """连接测试 — 成功返回 True, 失败返回 False (不抛异常)"""
        try:
            db = self._get_db()
            db.list_collection_names()
            return True
        except Exception as e:
            logger.warning(f"MongoDB 连接测试失败: {e}")
            return False

    def list_resources(self) -> list[str]:
        """返回数据库的所有集合名"""
        try:
            return self._get_db().list_collection_names()
        except Exception as e:
            logger.warning(f"MongoDB list_resources 失败: {e}")
            return []

    def pull_sample(self, resource: str, limit: int = 100) -> list[dict]:
        """从集合中查询样本数据"""
        try:
            collection = self._get_db()[resource]
            docs = list(collection.find({}, {"_id": 0}).limit(limit))
            return docs
        except Exception as e:
            logger.warning(f"MongoDB pull_sample 失败: {e}")
            return []

    def pull_full(self, resource: str) -> list[dict]:
        """查询全量数据。_id 转字符串保留——它是本连接器增量的水位线，
        剔除后 engine 取不到水印，APPEND 每次都退化为全量重拉。

        拉取失败必须抛异常：吞成 [] 会被同步链路按「源端 0 行」落一个
        合法空版本，把源端故障记成一次成功同步。"""
        collection = self._get_db()[resource]
        docs = []
        for doc in collection.find({}):
            doc["_id"] = str(doc.get("_id", ""))
            docs.append(doc)
        return docs

    def pull_delta(self, resource: str, since: str | None = None) -> list[dict]:
        """
        增量查询: 以 _id(ObjectId 含插入时间戳)作为水位线。
        since 传入上次同步的最大 _id 字符串。

        失败抛异常，不回退全量——增量故障静默整表重拉既掩盖故障，又会
        把水位线之前的旧数据重新带回。
        """
        if not since:
            return self.pull_full(resource)
        from bson import ObjectId
        collection = self._get_db()[resource]
        docs = []
        for doc in collection.find({"_id": {"$gt": ObjectId(since)}}):
            doc["_id"] = str(doc.get("_id", ""))
            docs.append(doc)
        return docs
