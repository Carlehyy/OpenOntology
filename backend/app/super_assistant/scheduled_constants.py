"""超级助手定时任务的共享超时常量。

收割器（scheduled_service）与重启保护（conversation_service.recover_
interrupted_streams）必须同进退：只改一处会让两处时钟错开。常量独立成
无依赖小模块，两边各自 import——若让 conversation_service 直接 import
scheduled_service，会与 runtime 链形成依赖环，被架构守卫（tests/
architecture/test_ontology_runtime_import_cycles.py，静态扫描含函数内
import）拦截。
"""
from __future__ import annotations

from datetime import timedelta

# 2 小时而非 30 分钟：默认 50 轮 agent 执行的墙钟可超 30 分钟，30 分钟会把
# 还在正常执行的长任务收割成失败；日/周计划的下一槽约一天后，2 小时仍能在
# 下次触发前清掉真正卡死的运行。
STALE_RUNNING = timedelta(hours=2)
