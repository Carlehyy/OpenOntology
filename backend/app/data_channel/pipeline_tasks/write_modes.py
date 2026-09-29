"""入库方式（write_mode）词表与软删除打标的共享语义（叶子模块）。

从 pipeline_tasks.merge 下沉：lake_store（物理湖表写入）与 merge（语义权威
参考实现）共用同一份校验/打标函数，独立成叶以避免存储层与合并参考实现
互相成环。merge.py 顶部再导出，既有导入方（pipeline_run/测试）的
``from ...merge import normalize_write_mode / _apply_soft_delete``
全部保持可用、同一函数对象。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


VALID_WRITE_MODES = frozenset({"overwrite", "append", "append_dedup", "upsert"})

# 软删除 truthy 词表：打标（_apply_soft_delete）与湖中候选行预筛
# （lake_store 的 SQL 预筛）必须共用同一常量——单边修改词表会让
# 「闸门打标」与「湖中重评估」口径分裂且没有测试会红。
SOFT_DELETE_TRUTHY = frozenset(
    {"1", "true", "yes", "y", "t", "是", "删除", "已删除"})


def normalize_write_mode(mode: Any) -> str:
    """把历史空值规整为 overwrite，并拒绝任何未知入库方式。

    旧实现把所有未识别值都落入 overwrite 分支。这会让拼写错误或新旧版本
    不兼容的任务静默变成全量覆盖，属于不可接受的数据破坏风险。空值仍按
    历史约定解释为 overwrite，其余值必须属于平台公开支持的模式。
    """
    if mode is None or str(mode).strip() == "":
        return "overwrite"
    normalized = str(mode).strip().lower()
    if normalized not in VALID_WRITE_MODES:
        allowed = ", ".join(sorted(VALID_WRITE_MODES))
        raise ValueError(
            f"不支持的入库方式「{mode}」。允许值：{allowed}；"
            f"为避免误覆盖资产，系统不会把未知方式回退为 overwrite。")
    return normalized


def _apply_soft_delete(rows: list[dict], deleted_col: str) -> list[dict]:
    """软删除标记：deleted_col 为真值的行加 __deleted__，不物理删除"""
    if not deleted_col or not rows:
        return rows
    for r in rows:
        v = r.get(deleted_col)
        is_del = (str(v).strip().lower() in SOFT_DELETE_TRUTHY
                  if v is not None else False)
        if is_del:
            r["__deleted__"] = True
            r["__deleted_at__"] = datetime.utcnow().isoformat()
        else:
            r.pop("__deleted__", None)
            r.pop("__deleted_at__", None)
    return rows
