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


class PrepareProposalRequest(_Strict):
    session_kind: str = Field(min_length=1, max_length=32)
    session_id: str = Field(min_length=1, max_length=64)
    client_submission_id: str = Field(min_length=1, max_length=128)
    action_id: str = Field(min_length=1, max_length=200)
    release_id: str = Field(min_length=1, max_length=64)
    target_instance_id: Optional[str] = Field(default=None, max_length=64)
    parameters: dict[str, Any] = Field(default_factory=dict)
    parameter_refs: dict[str, ParameterRef] = Field(default_factory=dict)


class ConfirmationDecisionRequest(_Strict):
    decision: Literal["approve", "deny", "cancel"]
    session_kind: str = Field(min_length=1, max_length=32)
    session_id: str = Field(min_length=1, max_length=64)
    decision_id: str = Field(min_length=1, max_length=128)
    payload_hash: str = Field(min_length=1, max_length=64)


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
    decided_at: Optional[str] = None
    created_at: Optional[str] = None
