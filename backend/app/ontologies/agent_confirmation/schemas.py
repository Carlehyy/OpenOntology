"""确认协议 HTTP 契约 — extra=forbid；确认后参数替换被拒绝而非忽略。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.ontologies.formal_modeling.schemas import CamelModel


class _Strict(CamelModel):
    model_config = ConfigDict(
        alias_generator=CamelModel.model_config["alias_generator"],
        populate_by_name=True,
        extra="forbid",
    )


class ParameterRef(_Strict):
    query_id: str = Field(min_length=1, max_length=64)
    row_ordinal: int = Field(ge=0)
    column: str = Field(min_length=1, max_length=64)


class TargetRef(_Strict):
    """资产行定位（远程/联邦对象无本地实例）：类型 + 属性空间主键值。

    资产身份（datasetId/assetId）绝不由此提供 — 服务端按 release 快照映射
    派生并验证；主键值本身经服务端读源核对后冻结 CAS token。
    """

    object_type_id: str = Field(min_length=1, max_length=64)
    key: dict[str, Any] = Field(default_factory=dict)


class PrepareProposalRequest(_Strict):
    session_kind: str = Field(min_length=1, max_length=32)
    session_id: str = Field(min_length=1, max_length=64)
    client_submission_id: str = Field(min_length=1, max_length=128)
    action_id: str = Field(min_length=1, max_length=200)
    release_id: str = Field(min_length=1, max_length=64)
    target_instance_id: Optional[str] = Field(default=None, max_length=64)
    target_ref: Optional[TargetRef] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    parameter_refs: dict[str, ParameterRef] = Field(default_factory=dict)


class ConfirmationDecisionRequest(_Strict):
    decision: Literal["approve", "deny", "cancel"]
    session_kind: str = Field(min_length=1, max_length=32)
    session_id: str = Field(min_length=1, max_length=64)
    decision_id: str = Field(min_length=1, max_length=128)
    payload_hash: str = Field(min_length=1, max_length=64)


class AdjudicationRequest(_Strict):
    """unknown 操作的管理员人工 not_executed 裁决（正门，绝不重放）。

    操作归属经 (operation_id, confirmation_id) 强绑定核验，不设装饰性
    session 字段。
    """

    operation_id: str = Field(min_length=1, max_length=64)
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(min_length=1, max_length=16)
    expected_fence_token: int = Field(ge=0)
    expected_guard_hash: str = Field(min_length=64, max_length=64)
    expected_payload_hash: str = Field(min_length=64, max_length=64)


class ExecuteConfirmationRequest(_Strict):
    confirmation_id: str = Field(min_length=1, max_length=64)
    session_kind: str = Field(min_length=1, max_length=32)
    session_id: str = Field(min_length=1, max_length=64)


class ConfirmationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_id: str
    state: str
    payload_hash: str
    display: dict[str, Any]
    expires_at: str
    requires_approval: bool
    replayed: bool = False
    operation_id: Optional[str] = None
    execution_log_id: Optional[str] = None
    approval_log_id: Optional[str] = None
    failure_code: Optional[str] = None
    block_reason: Optional[str] = None
    outcome: Optional[dict[str, Any]] = None
    decided_at: Optional[str] = None
    created_at: Optional[str] = None
