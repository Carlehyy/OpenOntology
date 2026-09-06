"""CEP 层共享契约：常量、枚举、边界值与跨模块运行原语。

只放纯常量、纯函数与跨模块共享的运行原语（如 in_sentinel_run
ContextVar——哨兵动作断环标记由 evaluator 定义、cdc 与 cep/pattern 共用，
下沉到叶子契约保证对象身份唯一），不 import sentinels 域其他模块。
"""
from __future__ import annotations

from contextvars import ContextVar

# 执行哨兵动作期间为 True；CDC 用它抑制级联即时再触发（断环）。
# 对象身份唯一：evaluator 再导出本对象，cdc/cep 经任一路径取到同一实例。
in_sentinel_run: ContextVar[bool] = ContextVar(
    "in_sentinel_run", default=False)

# ---------------------------------------------------------------------------
# 时间算子（仅允许出现在哨兵 condition，禁止出现在绑定 filter）
# ---------------------------------------------------------------------------
TEMPORAL_FUNCTION_CHANGED_WITHIN = "changed_within"
TEMPORAL_FUNCTION_PREV = "prev"
TEMPORAL_FUNCTIONS = frozenset({
    TEMPORAL_FUNCTION_CHANGED_WITHIN,
    TEMPORAL_FUNCTION_PREV,
})

# changed_within 窗口秒数边界：1 分钟 ~ 7 天（与事件日志保留期对齐）。
TEMPORAL_WINDOW_MIN_SECONDS = 60
TEMPORAL_WINDOW_MAX_SECONDS = 7 * 24 * 3600

# ---------------------------------------------------------------------------
# 事件日志
# ---------------------------------------------------------------------------
EVENT_KIND_CREATED = "created"
EVENT_KIND_UPDATED = "updated"
EVENT_KIND_DELETED = "deleted"
EVENT_KINDS = frozenset({
    EVENT_KIND_CREATED, EVENT_KIND_UPDATED, EVENT_KIND_DELETED,
})

# 删除事件不携带值差异，统一落在哨兵保留键上。
DELETED_EVENT_KEY = "__deleted__"

# 来源：organic=真实业务变更；release_activation=发布切换事务内的投影
# 重建（全量物化），只作审计，不参与时间算子/模式推进。
EVENT_SOURCE_ORGANIC = "organic"
EVENT_SOURCE_RELEASE_ACTIVATION = "release_activation"
EVENT_SOURCES = frozenset({
    EVENT_SOURCE_ORGANIC, EVENT_SOURCE_RELEASE_ACTIVATION,
})

# 固定保留 7 天（对齐 outbox 的 168h 先例）+ 行数硬上限。窗口上限已在校验
# 层钉死为 7 天，因此固定保留期总能覆盖任何合法窗口。
EVENT_LOG_RETENTION_SECONDS = 7 * 24 * 3600
EVENT_LOG_MAX_ROWS = 1_000_000
EVENT_LOG_PRUNE_BATCH = 5000

# prev() 批量回捞的单次行数上限：超出后未覆盖的引用返回 None
# （fail-closed：条件自然判否，不制造假命中）。
PREVIOUS_VALUES_QUERY_CAP = 20000

# ---------------------------------------------------------------------------
# 模式哨兵（trigger_mode='on_pattern'）
# ---------------------------------------------------------------------------
PATTERN_TRIGGER_MODE = "on_pattern"

# 序列 stage 数边界：1（单 stage+聚合）~ 4。
PATTERN_STAGES_MIN = 1
PATTERN_STAGES_MAX = 4

# stage 间隔窗口与聚合窗口共用边界：1 分钟 ~ 7 天（与保留期对齐）。
PATTERN_WITHIN_MIN_SECONDS = 60
PATTERN_WITHIN_MAX_SECONDS = 7 * 24 * 3600
# 未显式声明 within 时的缺省窗口（1 小时）。
PATTERN_WITHIN_DEFAULT_SECONDS = 3600

# 聚合函数与比较算子白名单。
PATTERN_AGGREGATE_FUNCTIONS = frozenset({
    "count", "avg", "sum", "min", "max",
})
PATTERN_AGGREGATE_COMPARISONS = frozenset({
    "gte", "lte", "gt", "lt",
})

# 模式哨兵强制 on_schedule 的扫描间隔下限：超时判定精度由扫描驱动，
# 窗口必须 ≥ 扫描间隔，否则超时形同虚设。
PATTERN_MIN_SCAN_INTERVAL_SECONDS = 60

# 状态机每次水位推进拉取的事件上限。
PATTERN_EVENT_BATCH_LIMIT = 2000

# in-flight 状态行上限（哨兵×本体级 fail-closed，防状态爆炸）。
PATTERN_MAX_ACTIVE_STATES = 10000

# IN 列表分片上限（SQLite 旧版绑定变量数保守值）。
_ID_CHUNK_SIZE = 500


def chunk_ids(ids):
    """把实例 id 集合切成安全大小的分片，供 IN 查询复用。"""
    ordered = list(ids)
    for start in range(0, len(ordered), _ID_CHUNK_SIZE):
        yield ordered[start:start + _ID_CHUNK_SIZE]
