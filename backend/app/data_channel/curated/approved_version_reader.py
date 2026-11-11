"""Approved-version governance reads shared by reviews and projections.

组合层（保持既有导入表面）：行身份/版本门禁原语在
``review_row_identity``，行编辑叠加与分批流式读取在 ``row_edit_overlay``。
本模块的再导出对象身份由架构测试
（tests/architecture/test_mapping_event_dependency_direction.py）固定，
调用方与 patch 点全部保持可用。
"""
from __future__ import annotations

from app.data_channel.curated.review_row_identity import (  # noqa: F401
    ReviewApprovalError,
    _as_aware,
    _coerce_review_value,
    _dataset_schema,
    _field_contract,
    _field_type,
    _version_by_id,
    current_version_review,
    dataset_pk_columns,
    encode_row_pk,
    latest_dataset_version,
    normalize_row_pk,
    require_current_version_approved,
    require_version_approved,
    review_matches_version,
    version_review,
)
from app.data_channel.curated.row_edit_overlay import (  # noqa: F401
    has_approved_edits,
    _apply_edit_map,
    _edits_map_for_version,
    apply_all_row_edits,
    apply_row_edits_to_batch,
    iter_rows_with_edits,
    load_all_rows_with_edits,
    load_rows_with_edits,
)
