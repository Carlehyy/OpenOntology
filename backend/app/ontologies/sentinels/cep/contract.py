"""CEP 层共享契约：常量、枚举与边界值。

只放纯常量与纯函数，不 import sentinels 域其他模块（叶子契约，
evaluator 与 cep 内部共同依赖，避免任何方向的反向导入）。
"""
from __future__ import annotations

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

# IN 列表分片上限（SQLite 旧版绑定变量数保守值）。
_ID_CHUNK_SIZE = 500


def chunk_ids(ids):
    """把实例 id 集合切成安全大小的分片，供 IN 查询复用。"""
    ordered = list(ids)
    for start in range(0, len(ordered), _ID_CHUNK_SIZE):
        yield ordered[start:start + _ID_CHUNK_SIZE]
