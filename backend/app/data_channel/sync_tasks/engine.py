"""旧版 DataSyncTask 同步引擎——只剩拒绝 stub。

运行时入湖已切换为 PipelineTask 路径（n8n 流水线执行 + 资产湖物理表
upsert），旧实现的拉取/合并/版本写入链路已随半退役清理删除（需要回溯
见 Git 历史）。本模块保留唯一入口：任何调用都得到明确的「已停用」错误，
不触碰数据库、数据源或资产湖；契约由
tests/v2/pipeline/test_legacy_sync_retirement.py 钉住。
"""
from __future__ import annotations


def execute_sync_task(task_id: str, trigger_type: str = "MANUAL") -> dict:
    """拒绝执行已退休的旧同步任务，不触碰数据库、数据源或资产湖。"""
    return {
        "status": "error",
        "task_id": task_id,
        "error": (
            "旧版 DataSyncTask 已停用；请使用已发布 n8n 流水线的数据任务池"
        ),
    }
