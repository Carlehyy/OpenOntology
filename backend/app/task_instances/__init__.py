"""任务实例（Task Instances）— 独立运行时功能域。

声明式 DAG 模板（YAML WorkflowSpec）→ 可多次实例化执行的任务轨道：
契约验收、双形态人工介入（关口审批 + 人作为生产节点）、条件分流、
驳回-修复环、事件溯源进度与运行中插话。设计方案见
.artifacts/task-instances-design-v1.md（评审版）。
"""
