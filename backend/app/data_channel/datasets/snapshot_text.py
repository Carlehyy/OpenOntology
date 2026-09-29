"""湖内快照的单元格文本规范（叶子模块，无应用内依赖）。

从 datasets.service 下沉：lake_store（物理湖表）与 service（快照读写）共用
同一规范化口径，独立成叶以避免存储层与服务层互相成环。所有既有导入方
（merge/测试）继续经 service 的再导出使用同一函数对象。
"""
from __future__ import annotations

import json


def snapshot_cell_text(value) -> str:
    """Canonical text representation used by tabular lake snapshots."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
