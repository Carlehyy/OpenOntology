"""任务实例 — API schema（pydantic v2，extra=forbid 收紧输入面）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemplateCreate(_Strict):
    spec_yaml: str = Field(min_length=1, max_length=200000)
    note: str | None = Field(default=None, max_length=500)


class TemplateUpdate(_Strict):
    spec_yaml: str = Field(min_length=1, max_length=200000)
    note: str | None = Field(default=None, max_length=500)


class TemplateValidate(_Strict):
    spec_yaml: str = Field(min_length=1, max_length=200000)


class InstanceActivate(_Strict):
    name: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=20000)
    inputs: dict[str, Any] | None = None
    revision_no: int | None = Field(default=None, ge=1)


class ArtifactSubmission(_Strict):
    name: str = Field(min_length=1, max_length=200)
    mime_type: str = Field(default="text/plain", max_length=100)
    content_base64: str = Field(min_length=1, max_length=4 * 1024 * 1024)


class HumanSubmit(_Strict):
    output: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactSubmission] | None = Field(default=None, max_length=8)
    note: str | None = Field(default=None, max_length=2000)


class HumanReject(_Strict):
    reason: str = Field(min_length=1, max_length=4000)
    target_node_id: str | None = Field(default=None, max_length=64)


class ApprovalDecision(_Strict):
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=4000)


class SteeringSubmit(_Strict):
    node_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=65536)


class InstanceCancel(_Strict):
    reason: str = Field(default="用户取消", min_length=1, max_length=2000)
