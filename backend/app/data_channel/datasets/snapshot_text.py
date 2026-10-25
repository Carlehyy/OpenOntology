"""湖内快照的单元格文本规范（叶子模块，无应用内依赖）。

从 datasets.service 下沉：lake_store（物理湖表）与 service（快照读写）共用
同一规范化口径，独立成叶以避免存储层与服务层互相成环。所有既有导入方
（merge/测试）继续经 service 的再导出使用同一函数对象。
"""
from __future__ import annotations

import json


MAX_STREAM_BATCH_BYTES = 16 * 1024**2


def canonical_row_bytes(row, *, limit: int = MAX_STREAM_BATCH_BYTES) -> int:
    """Measure canonical TEXT payload in bounded chunks, not one wide copy."""
    size = 0
    for value in row.values():
        if isinstance(value, str):
            for offset in range(0, len(value), 16384):
                size += len(value[offset:offset + 16384].encode("utf-8"))
                if size > limit:
                    raise ValueError("Row exceeds the stream byte budget")
        elif value is not None:
            size += 8  # immutable BIGINT ordinal; snapshot cells are TEXT
    if size > limit:
        raise ValueError("Row exceeds the stream byte budget")
    return size


def snapshot_cell_text(value) -> str:
    """Canonical text representation used by tabular lake snapshots."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
