"""映射消费边界的主键冒号拦截（对抗复核：人工声明入口兜底）。

单列实例身份 f"{col}:{value}" 的冒号分隔歧义可构造跨数据集实例碰撞，
连接同步路径已在写入侧拒绝，本测试钉住消费边界（_canonical_primary_key）
对 schema_json 直接携带冒号主键的统一 fail-closed。
"""
import pytest
from fastapi import HTTPException

from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset
from app.ontologies.mappings.request_validation import _canonical_primary_key


def test_canonical_primary_key_rejects_colon_columns(db, monkeypatch):
    connection = Connection(
        id="conn-colon-pk", name="冒号主键", kind="mysql", config={},
        status="inactive",
    )
    db.add(connection)
    db.commit()
    dataset = Dataset(
        id="ds-colon-pk", name="冒号主键数据集", kind="structured",
        source_connection_id=connection.id, source_resource="orders",
        schema_json={"primary_key": "id:x", "columns": ["id:x"]},
    )
    db.add(dataset)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        _canonical_primary_key(db, dataset.id)

    assert exc_info.value.status_code == 400
    assert "冒号" in str(exc_info.value.detail)
